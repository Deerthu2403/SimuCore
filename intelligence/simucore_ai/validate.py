"""Judge the trained models on recordings of the real simulated bank. Nothing is learned here.

    python -m simucore_ai.validate dev      while developing: shows where synthetic and real differ
    python -m simucore_ai.validate test     once, with the final models; writes results/validation.*

The recordings went through the whole chain (services, Prometheus, monitoring). The test recordings were
made after the models had been frozen: their file times are later than the models' (checked below).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime

import numpy as np

from simucore_ai import evaluate as ev
from simucore_ai import labels
from simucore_ai.calibrate import MAP, MODELS, PKG
from simucore_ai.features import load_services
from simucore_ai.main import load_models
from simucore_ai.recordings import Episode, load_set
from simucore_ai.train import (
    RESULTS,
    alarm_lines,
    describe,
    judge_detector,
    judge_predictor,
    rows,
    select,
    simple_rule,
)

GRADUAL = {"cpu_ramp", "mem_leak", "leak_crash", "latency_ramp", "db_exhaust", "load_ramp",
           "slots_ramp"}


def false_anomalies(detector, r) -> dict:
    """Normal operation before anything was done to the bank: how often is it called unusual?"""
    k = r.up & (r.tick >= 10) & (r.abnormal == 0)
    score = np.full(len(r.snap), np.nan)
    for s in detector.forests:
        m = k & (r.service == s)
        if m.any():
            score[m] = detector.score(s, r.snap[m])
    by = {s: round(float((score[k & (r.service == s)] >= 0.5).mean()), 4)
          for s in detector.forests if (k & (r.service == s)).any()}
    return {"moments": int(k.sum()), "called_unusual": round(float((score[k] >= 0.5).mean()), 4),
            "by_service": by}


def lead_table(models, r, target: str) -> list[dict]:
    """Every failure in the recordings: was it announced, and how long before?"""
    which = labels.TARGETS.index(target)
    prob = alarm_lines(models.predictors[target], r)
    out = []
    start = 0
    for (i, s), c in r.conditions.items():
        n = len(c[0])
        p = prob[start: start + n]
        start += n
        alarm = ev.persistent(p, models.thresholds[target],
                              models.thresholds[target] * models.off)
        for t in labels.onsets(c[which]):
            if t < labels.HORIZON // 4 or c[which][max(0, t - labels.HORIZON // 2): t].any():
                continue
            lo = max(0, t - labels.HORIZON)
            on = np.flatnonzero(alarm[lo: max(lo, t - labels.GAP) + 1]) + lo
            e = r.episodes[i]
            out.append({"recording": e.name, "kind": e.kind, "service": s,
                        "hit_directly": s == e.service,
                        "announced": bool(len(on)),
                        "lead_s": round((t - on[0]) * 2.0, 0) if len(on) else None})
    return out


def validate(name: str) -> dict:
    services = load_services(MAP)
    models = load_models(MODELS, MAP)
    eps: list[Episode] = load_set(PKG / "recordings" / name, services)
    if not eps:
        raise SystemExit(f"no recordings in recordings/{name}")
    r = rows(eps, services, models.profiles)
    frozen = datetime.fromisoformat(models.meta["trained_at"])
    made = [datetime.fromisoformat(e.meta["recorded_at"]) for e in eps]
    out: dict = {"made_at": datetime.now(UTC).isoformat(timespec="seconds"), "set": name,
                 "model": models.version, "model_trained_at": models.meta["trained_at"],
                 "recorded_after_training": all(m > frozen for m in made),
                 "recordings": len(eps),
                 "kinds": dict(Counter(e.kind for e in eps).most_common()), "rows": describe(r),
                 "anomaly": judge_detector(models.detector, r),
                 "normal_operation": false_anomalies(models.detector, r)}
    for t in labels.TARGETS:
        out[t] = judge_predictor(models.predictors[t], r, t, models.thresholds[t])
        k = select(r, t)
        out[t]["baseline_rule_at_same_recall"] = same_recall(
            r.y[t][k], models.predictors[t].probability(r.x[k]), simple_rule(r.x[k]),
            models.thresholds[t])
        out[t]["each_failure"] = lead_table(models, r, t)
        table = out[t]["each_failure"]
        gradual = [x for x in table if x["kind"] in GRADUAL]
        sudden = [x for x in table if x["kind"] not in GRADUAL]
        out[t]["announced_gradual"] = [sum(x["announced"] for x in gradual), len(gradual)]
        out[t]["announced_sudden"] = [sum(x["announced"] for x in sudden), len(sudden)]
    out["summary"] = {
        "made_at": out["made_at"], "recordings": out["recordings"], "model": models.version,
        "anomaly": out["anomaly"]["isolation_forest"],
        **{t: {"rows": out[t]["xgboost"], "events": {
            k: v for k, v in out[t]["events"].items() if k != "by_kind"}}
           for t in labels.TARGETS}}
    return out


def same_recall(y: np.ndarray, p: np.ndarray, rule: np.ndarray, threshold: float) -> dict:
    """The simple rule, tuned in hindsight to find as many as the model: how precise is it then?"""
    found = ((p >= threshold) & (y == 1)).sum()
    if not found:
        return {}
    cut = np.sort(rule[y == 1])[::-1][found - 1]
    r = ev.rates(y, rule >= cut)
    return {"precision": r["precision"], "recall": r["recall"]}


def markdown(v: dict) -> str:
    f = lambda x: "–" if x is None else f"{x:.2f}" if isinstance(x, float) else str(x)  # noqa: E731
    a = v["anomaly"]["isolation_forest"]
    lines = [
        "# Validation on the real simulated bank", "",
        f"Made {v['made_at']} with model `{v['model']}`, trained {v['model_trained_at']} on synthetic "
        f"data only. {v['recordings']} recordings of the running bank, "
        f"{'all made after training' if v['recorded_after_training'] else 'NOT all made after training'}"
        ". Generated by `simucore_ai/validate.py`.", "",
        "## Rows (every service, every 2 s)", "",
        "| Model | Precision | Recall | F1 | ROC-AUC | Average precision | Share of positives |",
        "|---|---|---|---|---|---|---|",
        f"| Isolation Forest, anomaly now | {f(a['precision'])} | {f(a['recall'])} | {f(a['f1'])} | "
        f"{f(a['roc_auc'])} | {f(a['average_precision'])} | {f(a['prevalence'])} |"]
    for t in labels.TARGETS:
        x = v[t]["xgboost"]
        lines.append(f"| XGBoost, {t} within {labels.HORIZON_S} s | {f(x['precision'])} | "
                     f"{f(x['recall'])} | {f(x['f1'])} | {f(x['roc_auc'])} | "
                     f"{f(x['average_precision'])} | {f(x['prevalence'])} |")
    lines += ["", "## Events (what an operator experiences)", "",
              "| Target | Announced | at least 10 s before | at least 30 s | of the gradual ones | of the "
              "sudden ones | Median lead | False alarms | per service-hour |",
              "|---|---|---|---|---|---|---|---|---|"]
    for t in labels.TARGETS:
        e = v[t]["events"]
        g, s = v[t]["announced_gradual"], v[t]["announced_sudden"]
        lines.append(f"| {t} | {e['announced']} of {e['failures']} | {e['announced_10s_before']} | "
                     f"{e['announced_30s_before']} | {g[0]} of {g[1]} | "
                     f"{s[0]} of {s[1]} | {f(e['lead_s_median'])} s | {e['false_alarms']} of "
                     f"{e['alarms']} | {f(e['false_alarms_per_service_hour'])} |")
    n = v["normal_operation"]
    lines += ["", f"Normal operation: {n['called_unusual']:.2%} of {n['moments']} moments were called "
              "unusual.", "", "## Each failure", "",
              "| Recording | What was done | Service | Target | Announced | Lead |",
              "|---|---|---|---|---|---|"]
    for t in labels.TARGETS:
        for x in v[t]["each_failure"]:
            lines.append(f"| {x['recording']} | {x['kind']} | {x['service']}"
                         f"{'' if x['hit_directly'] else ' (affected)'} | {t} | "
                         f"{'yes' if x['announced'] else 'no'} | {f(x['lead_s'])} s |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("set", choices=["dev", "test"])
    a = ap.parse_args()
    v = validate(a.set)
    print(json.dumps({k: v[k] for k in ("recorded_after_training", "kinds", "normal_operation",
                                        "summary")}, indent=1))
    for t in labels.TARGETS:
        print(t, "gradual", v[t]["announced_gradual"], "sudden", v[t]["announced_sudden"])
        for x in v[t]["each_failure"]:
            print("   ", x)
    if a.set == "test":
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / "validation.json").write_text(json.dumps(v, indent=1) + "\n", encoding="utf-8",
                                                 newline="\n")
        (RESULTS / "validation.md").write_text(markdown(v), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
