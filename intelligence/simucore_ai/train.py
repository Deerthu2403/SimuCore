"""Train both models on synthetic data, choose thresholds, and measure them on data they never saw.

    python -m simucore_ai.train [--quick]

Five sets of generated episodes, none of which shares an episode (or a random seed) with another:

  train   trees and forests learn from it
  valid   decides when boosting stops, and where the anomaly threshold lies
  calib   calibrates probabilities, decides where the warning threshold lies
  test    the numbers while developing; the final numbers come from fresh episodes (final.py)
  shifted like test, but from a world with other noise, costs, loads and speeds

Nothing is ever split by row: neighbouring rows are almost equal, and would leak between the sets.
Recordings of the real simulated bank are judged separately (validate.py) and are not trained on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from simucore_ai import evaluate as ev
from simucore_ai import labels
from simucore_ai.calibrate import MAP, MODELS, PKG, load_calibration, profiles
from simucore_ai.detector import AnomalyDetector, classify
from simucore_ai.features import (
    IDX,
    SNAPSHOT,
    TEMPORAL,
    Profile,
    Service,
    load_services,
    snapshot,
    temporal,
)
from simucore_ai.generator import SHIFTED, dataset
from simucore_ai.predictor import PARAMS, FailurePredictor
from simucore_ai.recordings import Episode

RESULTS = PKG / "results"
SPLITS = {"train": (0, 700), "valid": (100_000, 140), "calib": (200_000, 220),
          "test": (300_000, 260), "shifted": (400_000, 160)}
FALSE_ALARMS = 0.004  # share of normal moments that may look anomalous
MIN_PRECISION = 0.8  # of a warning, row by row, on the calibration set
OFF = 0.6  # an alarm ends when the probability falls below this share of the threshold
#: which condition the detector should name for each kind of trouble
EXPECTED = {"cpu": {"resources", "latency"}, "leak": {"resources", "errors"},
            "leak_crash": {"resources"}, "slow": {"latency", "connections"},
            "pool": {"database", "latency", "errors"}, "errors": {"errors"},
            "load": {"traffic", "resources", "connections", "latency"}}


@dataclass
class Rows:
    """Everything the models see of one set."""

    episodes: list[Episode]
    x: np.ndarray  # temporal features
    snap: np.ndarray  # snapshot features
    service: np.ndarray
    episode: np.ndarray
    tick: np.ndarray
    up: np.ndarray
    abnormal: np.ndarray
    y: dict[str, np.ndarray]
    conditions: dict[tuple[int, str], tuple[np.ndarray, np.ndarray]]


def rows(episodes: list[Episode], services: dict[str, Service],
         prof: dict[str, Profile]) -> Rows:
    parts: dict[str, list[np.ndarray]] = {k: [] for k in (
        "x", "snap", "service", "episode", "tick", "up", "abnormal", *labels.TARGETS)}
    cond = {}
    for i, e in enumerate(episodes):
        for s, spec in services.items():
            raw, status = e.raw[s], e.status[s]
            n = len(raw)
            degraded, failed = labels.conditions(raw, status, spec)
            cond[i, s] = (degraded, failed)
            parts["x"].append(temporal(raw, prof[s], spec,
                                       [(e.raw[d], prof[d]) for d in spec.targets]))
            parts["snap"].append(snapshot(raw, prof[s]))
            parts["service"].append(np.full(n, s, dtype="<U16"))
            parts["episode"].append(np.full(n, i))
            parts["tick"].append(np.arange(n))
            parts["up"].append(raw[:, IDX["up"]] > 0)
            parts["abnormal"].append(e.abnormal[s])
            for t, c in zip(labels.TARGETS, (degraded, failed), strict=True):
                y = labels.target(c)
                y[raw[:, IDX["up"]] < 1] = -1
                y[:14] = -1  # not enough history yet
                parts[t].append(y)
    joined = {k: np.concatenate(v) for k, v in parts.items()}
    return Rows(episodes, joined["x"], joined["snap"], joined["service"], joined["episode"],
                joined["tick"], joined["up"], joined["abnormal"],
                {t: joined[t] for t in labels.TARGETS}, cond)


# -------------------------------------------------------------------------- anomalies
def train_detector(train: Rows, valid: Rows, names: list[str]) -> tuple[AnomalyDetector, dict]:
    """Learns from a mixture: mostly normal moments, and some abnormal ones, unlabelled."""
    rng = np.random.default_rng(11)
    candidates, compared = {}, {}
    for share in (0.0, 0.05, 0.1):
        x = {}
        for s in names:
            k = (train.service == s) & train.up & (train.tick >= 5)
            normal = np.flatnonzero(k & (train.abnormal == 0))
            odd = np.flatnonzero(k & (train.abnormal == 1))
            normal = rng.choice(normal, min(len(normal), 30_000), replace=False)
            odd = rng.choice(odd, min(len(odd), int(len(normal) * share)), replace=False)
            x[s] = train.snap[np.concatenate([normal, odd])]
        d = AnomalyDetector.fit(x)
        score = np.full(len(valid.snap), np.nan)
        for s in names:
            k = (valid.service == s) & valid.up
            d.calibrate(s, valid.snap[k & (valid.abnormal == 0) & (valid.tick >= 5)], FALSE_ALARMS)
            score[k] = d.score(s, valid.snap[k])
        ok = valid.up & (valid.abnormal >= 0) & (valid.tick >= 5)
        r = ev.ranking(valid.abnormal[ok], score[ok])
        compared[f"{share:.0%} abnormal"] = r["average_precision"]
        candidates[share] = d
    best = max(candidates, key=lambda c: compared[f"{c:.0%} abnormal"])
    return candidates[best], {"tried": compared, "chosen": f"{best:.0%} abnormal"}


def judge_detector(d: AnomalyDetector, r: Rows) -> dict:
    score = np.full(len(r.snap), np.nan)
    for s in d.forests:
        k = (r.service == s) & r.up
        if k.any():
            score[k] = d.score(s, r.snap[k])
    ok = r.up & (r.abnormal >= 0) & (r.tick >= 5)
    y = r.abnormal[ok]
    simple = np.abs(r.snap).max(axis=1)
    out = {"isolation_forest": ev.ranking(y, score[ok]) | ev.rates(y, score[ok] >= 0.5),
           "baseline_largest_deviation": ev.ranking(y, simple[ok])}
    kinds = np.array([e.kind for e in r.episodes])[r.episode]
    found = {}
    for kind in sorted(set(kinds)):
        k = ok & (kinds == kind) & (r.abnormal == 1)
        if k.any():
            found[kind] = {"moments": int(k.sum()),
                           "noticed": round(float((score[k] >= 0.5).mean()), 3)}
    # the first moments of something gradual are hard, its worst must not be missed: an event counts
    # as noticed if the detector would have confirmed an anomaly at any time during it
    events_ = {}
    for i, e in enumerate(r.episodes):
        if e.service not in d.forests:
            continue
        b = _block(r, i, e.service)
        odd = (r.abnormal[b] == 1) & r.up[b]
        if odd.sum() >= 3:
            hit = np.nan_to_num(score[b]) >= 0.5
            confirmed = (np.convolve(hit & odd, [1, 1, 1])[: len(hit)] >= 2).any()
            n = events_.setdefault(e.kind, [0, 0])
            n[0] += int(confirmed)
            n[1] += 1
    out["disturbances_noticed"] = {k: {"noticed": v[0], "of": v[1]}
                                   for k, v in sorted(events_.items())}
    out["noticed_by_kind"] = found
    # the named condition
    rng = np.random.default_rng(3)
    right = total = 0
    confusion: dict[str, Counter] = {}
    for kind, want in EXPECTED.items():
        k = np.flatnonzero(ok & (kinds == kind) & (r.abnormal == 1) & (score >= 0.5))
        for i in rng.choice(k, min(len(k), 120), replace=False):
            named = classify(d.explain(str(r.service[i]), r.snap[i]))
            confusion.setdefault(kind, Counter())[named] += 1
            right += named in want
            total += 1
    out["condition_named_correctly"] = round(right / total, 3) if total else None
    out["named"] = {k: dict(v.most_common()) for k, v in confusion.items()}
    return out


# ------------------------------------------------------------------------- prediction
def select(r: Rows, target: str, step: int = 1) -> np.ndarray:
    return np.flatnonzero((r.y[target] >= 0) & (r.tick % step == 0))


def simple_rule(x: np.ndarray) -> np.ndarray:
    """What an operator without a model would use: how close is the worst value to its limit now?"""
    c = {n: x[:, i] for i, n in enumerate(TEMPORAL)}
    return np.nanmax(np.stack([np.nan_to_num(c["cpu_level"]) / 0.9,
                               np.nan_to_num(c["memory_level"]) / 0.92,
                               np.nan_to_num(c["latency_level"]) / 3,
                               np.nan_to_num(c["errors_level"]) / 0.5,
                               np.nan_to_num(c["queue_level"]) / 0.25,
                               np.nan_to_num(c["in_flight_level"]) / 0.9]), axis=0)


def logistic(train: tuple[np.ndarray, np.ndarray]):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(1)
    k = rng.choice(len(train[0]), min(len(train[0]), 120_000), replace=False)
    clean = np.nan_to_num(train[0][k], nan=0.0)
    m = make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=400))
    m.fit(clean, train[1][k])
    return lambda x: m.predict_proba(np.nan_to_num(x, nan=0.0))[:, 1]


def judge_predictor(p: FailurePredictor, r: Rows, target: str, threshold: float,
                    others: dict | None = None) -> dict:
    k = select(r, target)
    y = r.y[target][k]
    prob = p.probability(r.x[k])
    out = {"xgboost": ev.ranking(y, prob) | ev.rates(y, prob >= threshold)
           | {"brier": ev.brier(y, prob),
              "brier_of_always_saying_the_average": ev.brier(y, np.full(len(y), y.mean()))},
           "reliability": ev.reliability(y, prob),
           "baseline_rule": ev.ranking(y, simple_rule(r.x[k]))}
    for name, f in (others or {}).items():
        out[f"baseline_{name}"] = ev.ranking(y, f(r.x[k]))
    out["events"] = events(p, r, target, threshold).result()
    return out


def alarm_lines(p: FailurePredictor, r: Rows) -> np.ndarray:
    prob = p.probability(r.x)
    prob[~r.up | (r.tick < 14)] = np.nan
    return prob


def events(p: FailurePredictor, r: Rows, target: str, threshold: float) -> ev.Events:
    prob = alarm_lines(p, r)
    out = ev.Events()
    which = labels.TARGETS.index(target)
    for (i, s), c in r.conditions.items():
        k = _block(r, i, s)
        e = r.episodes[i]
        # the service something was done to, and those that suffer with it, are different tasks
        kind = e.kind if s == e.service or e.kind == "load" else f"{e.kind} (affected)"
        out.add(f"{e.name}/{s}", kind, c[which], r.y[target][k],
                ev.persistent(prob[k], threshold, threshold * OFF))
    return out


def _block(r: Rows, i: int, s: str) -> slice:
    """Rows of one service in one episode: they are contiguous."""
    cache = r.__dict__.setdefault("_blocks", {})
    if not cache:
        start = 0
        for (a, b) in r.conditions:
            n = len(r.conditions[a, b][0])
            cache[a, b] = slice(start, start + n)
            start += n
    return cache[i, s]


# ------------------------------------------------------------------------------ main
def make(split: str, services: dict, cal: dict, prof: dict, quick: bool) -> Rows:
    start, n = SPLITS[split]
    n = max(12, n // 10) if quick else n
    eps = dataset(range(start, start + n), services, cal, SHIFTED if split == "shifted" else None)
    return rows(eps, services, prof)


def describe(r: Rows) -> dict:
    return {"episodes": len(r.episodes),
            "kinds": dict(Counter(e.kind for e in r.episodes).most_common()),
            "rows": len(r.x),
            **{t: {"usable": int((r.y[t] >= 0).sum()), "positive": int((r.y[t] == 1).sum())}
               for t in labels.TARGETS}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="a tenth of the data, for trying out")
    ap.add_argument("--out", type=Path, default=MODELS)
    a = ap.parse_args()
    t0 = time.time()
    services = load_services(MAP)
    cal = load_calibration()
    prof = profiles(cal)
    names = list(services)
    say = lambda *x: print(f"[{time.time() - t0:5.0f} s]", *x, flush=True)  # noqa: E731
    sets = {}
    for split in ("train", "valid", "calib"):
        sets[split] = make(split, services, cal, prof, a.quick)
        say(split, json.dumps(describe(sets[split])))
    train, valid, calib = sets["train"], sets["valid"], sets["calib"]

    detector, how = train_detector(train, valid, names)
    say("isolation forest", how)
    predictors, thresholds, baselines, sanity = {}, {}, {}, {}
    for target in labels.TARGETS:
        kt, kv, kc = (select(sets[s], target, step) for s, step in
                      (("train", 2), ("valid", 2), ("calib", 1)))
        data = (train.x[kt], train.y[target][kt])
        p = FailurePredictor.fit(data, (valid.x[kv], valid.y[target][kv]))
        p.calibrate(calib.x[kc], calib.y[target][kc])
        predictors[target] = p
        thresholds[target] = ev.best_threshold(calib.y[target][kc], p.probability(calib.x[kc]),
                                               MIN_PRECISION)
        baselines[target] = {"logistic_regression": logistic(data)}
        # If labels are shuffled, nothing can be learned; anything above chance would be leakage.
        rng = np.random.default_rng(5)
        fake = []
        for _ in range(5):
            f = FailurePredictor.fit((data[0], rng.permutation(data[1])),
                                     (valid.x[kv], valid.y[target][kv]), rounds=40)
            # judged on a set that played no part in stopping it either
            fake.append(ev.ranking(calib.y[target][kc], f.raw(calib.x[kc]))["roc_auc"])
        sanity[target] = {
            "auc_after_shuffling_labels": {"each": fake, "mean": round(float(np.mean(fake)), 4)},
            "auc_with_true_labels": ev.ranking(calib.y[target][kc],
                                               p.raw(calib.x[kc]))["roc_auc"]}
        say(target, f"{p.booster.num_boosted_rounds()} trees, threshold {thresholds[target]:.3f}",
            sanity[target])

    # ------------------------------------------------------ only now: the test sets
    report: dict = {"made_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "targets": {"horizon_s": labels.HORIZON_S, "gap_s": labels.GAP_S,
                                "cool_down_s": labels.COOL_DOWN_S},
                    "sets": {k: describe(v) for k, v in sets.items()},
                    "anomaly_training": how, "thresholds": thresholds, "sanity": sanity,
                    "parameters": {**PARAMS, "trees": {t: p.booster.num_boosted_rounds()
                                                       for t, p in predictors.items()}},
                    "importance": {t: p.importance() for t, p in predictors.items()}}
    for split in ("test", "shifted"):
        r = make(split, services, cal, prof, a.quick)
        report["sets"][split] = describe(r)
        report[split] = {
            "anomaly": judge_detector(detector, r),
            **{t: judge_predictor(predictors[t], r, t, thresholds[t], baselines[t])
               for t in labels.TARGETS}}
        say(split, json.dumps({t: {k: report[split][t]["xgboost"][k] for k in
                                   ("precision", "recall", "f1", "roc_auc")}
                               for t in labels.TARGETS}))
        say(split, json.dumps(report[split]["anomaly"]["isolation_forest"]))
        for t in labels.TARGETS:
            say(split, t, json.dumps(report[split][t]["events"]))

    a.out.mkdir(parents=True, exist_ok=True)
    detector.save(a.out / "anomaly.npz")
    for t, p in predictors.items():
        (a.out / f"{t}.json").write_text(json.dumps(p.to_state()), encoding="utf-8", newline="\n")
    digest = hashlib.sha256()
    for f in ("anomaly.npz", "degradation.json", "failure.json", "calibration.json"):
        digest.update((a.out / f).read_bytes() if (a.out / f).exists()
                      else (MODELS / f).read_bytes())
    meta = {"version": digest.hexdigest()[:12], "trained_at": report["made_at"],
            "trained_on": "synthetic data", "features": list(TEMPORAL),
            "snapshot": list(SNAPSHOT), "thresholds": thresholds, "off": OFF,
            "horizon_s": labels.HORIZON_S, "gap_s": labels.GAP_S,
            "test": {"anomaly": report["test"]["anomaly"]["isolation_forest"],
                     **{t: {"xgboost": report["test"][t]["xgboost"],
                            "events": {k: v for k, v in report["test"][t]["events"].items()
                                       if k != "by_kind"}} for t in labels.TARGETS}},
            "importance": {t: dict(list(report["importance"][t].items())[:15])
                           for t in labels.TARGETS}}
    (a.out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8",
                                     newline="\n")
    if not a.quick:
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / "synthetic.json").write_text(json.dumps(report, indent=1) + "\n",
                                                encoding="utf-8", newline="\n")
    say("done, model", meta["version"])


if __name__ == "__main__":
    main()
