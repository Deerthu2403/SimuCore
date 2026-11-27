"""Features, labels, generator, forest, detector, predictor, measures."""

import json
import re
from pathlib import Path

import numpy as np
import pytest

from simucore_ai import evaluate as ev
from simucore_ai import generator as gen
from simucore_ai import labels
from simucore_ai.calibrate import MAP, MODELS, load_calibration, profiles
from simucore_ai.detector import AnomalyDetector, classify
from simucore_ai.features import (
    FLOOR,
    IDX,
    METRICS,
    RAW,
    SNAPSHOT,
    TEMPORAL,
    WINDOW,
    Profile,
    describe,
    load_services,
    metric_of,
    raw_row,
    snapshot,
    temporal,
)
from simucore_ai.forest import Forest
from simucore_ai.predictor import FailurePredictor, by_metric
from simucore_ai.train import SPLITS, rows

SERVICES = load_services(MAP)
CAL = load_calibration()
PROF = profiles(CAL)
ACC = SERVICES["account"]
FULL = {"cpu_pct": 12.0, "mem_pct": 18.0, "latency_mean_ms": 9.0, "latency_ms": 20.0,
        "error_rate": 0.0, "rps": 12.0, "net_rx_bps": 6000.0, "net_tx_bps": 9000.0,
        "db_response_ms": 5.0, "active_connections": 9.0, "queue_length": 0.0, "in_flight": 1.0}


@pytest.fixture(scope="module")
def episodes():
    return gen.dataset(range(900_000, 900_024), SERVICES, CAL)


@pytest.fixture(scope="module")
def table(episodes):
    return rows(episodes, SERVICES, PROF)


# ------------------------------------------------------------------------- raw rows
def test_the_eight_required_metrics_are_the_inputs():
    assert METRICS == ("cpu", "memory", "latency", "errors", "requests", "network", "database",
                       "connections")
    assert SNAPSHOT[:8] == METRICS and all(f"{m}_now" in TEMPORAL for m in METRICS)
    r = raw_row("up", FULL, ACC)
    assert r[IDX["network"]] == 15000 and r[IDX["latency"]] == 9
    assert np.isfinite(r[: len(METRICS)]).all()


def test_unknown_is_never_zero():
    r = raw_row("up", {"cpu_pct": 1.0, "rps": 0.0, "latency_mean_ms": None}, ACC)
    assert np.isnan(r[IDX["latency"]]) and np.isnan(r[IDX["errors"]]) and r[IDX["requests"]] == 0
    down = raw_row("down", FULL, ACC)
    assert down[IDX["up"]] == 0 and np.isnan(down[: len(METRICS)]).all()


def test_a_profile_centres_normal_operation():
    rng = np.random.default_rng(1)
    raw = np.array([raw_row("up", {**FULL, "cpu_pct": 12 + rng.normal(0, 5)}, ACC)
                    for _ in range(800)])
    p = Profile.fit(raw)
    z = p.z(raw)
    assert abs(np.median(z[:, 0])) < 0.1 and 0.8 < np.std(z[:, 0]) < 1.2
    assert (p.spread >= FLOOR).all()  # a constant metric does not make every change enormous
    again = Profile.from_json(json.loads(json.dumps(p.to_json())))
    assert np.allclose(again.z(raw), z, atol=1e-2, equal_nan=True)


def test_an_idle_service_is_not_unusual_for_lack_of_values():
    idle = raw_row("up", {"cpu_pct": 0.5, "mem_pct": 18.0, "rps": 0.0, "active_connections": 2.0},
                   ACC)
    s = snapshot(idle[None], PROF["account"])[0]
    assert np.isfinite(s).all()
    assert s[METRICS.index("latency")] == 0 and s[METRICS.index("errors")] == 0


# ------------------------------------------------------------------------- no leakage
def test_features_never_look_into_the_future(episodes):
    e = episodes[0]
    for s, spec in SERVICES.items():
        deps = [(e.raw[d], PROF[d]) for d in spec.targets]
        whole = temporal(e.raw[s], PROF[s], spec, deps)
        for t in (20, 77, 150):
            changed = e.raw[s].copy()
            changed[t + 1:] = 1e6 * np.random.default_rng(t).random(changed[t + 1:].shape)
            cut = temporal(changed, PROF[s], spec,
                           [(np.where(np.arange(len(r))[:, None] > t, 1e6, r), p)
                            for r, p in deps])
            assert np.array_equal(whole[: t + 1], cut[: t + 1], equal_nan=True)


def test_training_and_live_scoring_see_the_same(episodes):
    """The live engine only has the last minute; the features must not differ."""
    e = episodes[1]
    for s in ("account", "transaction", "database"):
        spec = SERVICES[s]
        whole = temporal(e.raw[s], PROF[s], spec, [(e.raw[d], PROF[d]) for d in spec.targets])
        for t in (40, 120, 200):
            part = slice(t - WINDOW + 1, t + 1)
            live = temporal(e.raw[s][part], PROF[s], spec,
                            [(e.raw[d][part], PROF[d]) for d in spec.targets])[-1]
            assert np.allclose(whole[t], live, equal_nan=True, atol=1e-4)


def test_the_sets_share_no_episode():
    ranges = {k: range(a, a + n) for k, (a, n) in SPLITS.items()}
    names = list(ranges)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not set(ranges[a]) & set(ranges[b]), (a, b)


def test_labels_are_not_among_the_features():
    assert not [f for f in TEMPORAL if re.search("label|target|fault|onset|kind|status", f)]
    source = (Path(gen.__file__).parent / "labels.py").read_text(encoding="utf-8")
    assert "abnormal" not in source  # labels come from numbers, not from what was injected


# ---------------------------------------------------------------------------- labels
def cond(n: int, *spans) -> np.ndarray:
    c = np.zeros(n, dtype=bool)
    for a, b in spans:
        c[a:b] = True
    return c


def test_target_windows_exactly():
    y = labels.target(cond(300, (200, 210)))
    h, g, cool = labels.HORIZON, labels.GAP, labels.COOL_DOWN
    assert (y[: 200 - h] == 0).all()
    assert (y[200 - h: 200 - g] == 1).all()
    assert (y[200 - g: 200] == -1).all()  # too late to be a prediction
    assert (y[200: 210 + cool] == -1).all()  # present, then recovering
    assert (y[300 - h:] == -1).all()  # the future is not recorded
    assert h * 2 == 120 and g * 2 == 4


def test_the_end_of_a_recording_is_not_a_negative_example():
    y = labels.target(cond(100))
    assert (y[:40] == 0).all() and (y[40:] == -1).all()
    y = labels.target(cond(100, (90, 95)))
    assert (y[30:88] == 1).all()  # known to be positive although the end is near


def test_conditions_follow_the_rules_of_the_twin():
    n = 12
    up = np.full(n, "up", dtype="<U12")
    base = np.repeat(raw_row("up", FULL, ACC)[None], n, axis=0)

    def check(status=up, **change):
        raw = base.copy()
        for k, v in change.items():
            raw[4:, IDX[k]] = v
        return labels.conditions(raw, status, ACC)

    d, f = check()
    assert not d.any() and not f.any()
    d, f = check(errors=0.6)
    assert f[5:].all() and not f[:5].any() and d[5:].all()  # the second tick counts
    d, f = check(errors=0.3)
    assert not d.any()
    d, f = check(cpu=95)
    assert d[5:].all() and not f.any()
    d, f = check(memory=93)
    assert d[5:].all()
    d, f = check(latency=500)
    assert not d.any()  # slow alone: waiting for something else
    d, f = check(latency=500, cpu=70)
    assert d[5:].all()
    down = up.copy()
    down[6:] = "down"
    d, f = check(status=down)
    assert f[6:].all() and d[6:].all()  # at once
    silent = up.copy()
    silent[6:] = "unresponsive"
    d, f = check(status=silent)
    assert d[7:].all() and not f.any()


def test_the_limits_are_those_of_the_digital_twin():
    """labels.py restates rules of the backend. If those change, this must fail."""
    health = (MAP.parents[1] / "backend/app/digital_twin/health.py").read_text(encoding="utf-8")
    for rule in ('if errors >= 0.5:', '"cpu_pct", "CPU", " %", 75, 90',
                 '"mem_pct", "memory", " %", 80, 92', "max(1.0, 0.25 * slots)", "busy >= 0.9",
                 "(m.get(\"cpu_pct\") or 0) >= 60 or queue >= 1 or busy >= 0.5",
                 "strained and latency >= 3 * base", "MIN_RPS = 0.3"):
        assert rule in health, rule


# ------------------------------------------------------------------------- generator
def test_the_generator_is_reproducible_and_seeds_differ():
    a, b, c = (gen.episode(s, SERVICES, CAL) for s in (5, 5, 6))
    for s in SERVICES:
        assert np.array_equal(a.raw[s], b.raw[s], equal_nan=True)
    assert not np.array_equal(a.raw["account"][:200], c.raw["account"][:200], equal_nan=True)


def test_episodes_are_physically_possible(episodes):
    for e in episodes:
        for s, raw in e.raw.items():
            up = e.status[s] == "up"
            assert (raw[~up, 0] != raw[~up, 0]).all()  # no values while down or silent
            v = raw[up]
            assert np.nanmin(v) >= 0
            assert np.nanmax(v[:, IDX["cpu"]]) <= 110 and np.nanmax(v[:, IDX["memory"]]) <= 100
            assert np.nanmax(v[:, IDX["errors"]]) <= 1
            idle = v[:, IDX["requests"]] == 0
            assert np.isnan(v[idle, IDX["latency"]]).all()


def test_normal_synthetic_data_resembles_the_recordings():
    calm = [e for e in gen.dataset(range(910_000, 910_060), SERVICES, CAL) if e.kind == "calm"]
    assert calm
    for s in SERVICES:
        z = np.concatenate([PROF[s].z(e.raw[s]) for e in calm])
        middle = np.nanmedian(z, axis=0)
        assert (np.abs(np.nan_to_num(middle)) < 2.0).all(), (s, middle.round(2))


def test_calm_and_waves_do_not_fail_and_gradual_troubles_do():
    bad, good = [], []
    for e in gen.dataset(range(920_000, 920_120), SERVICES, CAL):
        failed = any(labels.conditions(e.raw[s], e.status[s], spec)[0].any()
                     for s, spec in SERVICES.items())
        if e.kind in ("calm", "wave"):
            bad.append(failed)
        if e.kind in ("leak", "leak_crash", "cpu", "pool", "crash", "outage"):
            good.append(failed)
    assert not any(bad) and len(bad) >= 5
    assert np.mean(good) > 0.9


def test_sudden_failures_have_no_precursor():
    """Otherwise predicting them would be made possible by the generator, not by the model."""
    diffs = []
    for e in gen.dataset(range(930_000, 930_160), SERVICES, CAL):
        if e.kind in gen.SUDDEN:
            z = PROF[e.service].z(e.raw[e.service])
            diffs.append(np.nanmean(z[e.onset - 10: e.onset], axis=0)
                         - np.nanmean(z[e.onset - 40: e.onset - 10], axis=0))
    assert len(diffs) >= 8
    d = np.array(diffs)
    assert (np.abs(np.nan_to_num(np.nanmean(d, axis=0))) < 0.5).all(), d.mean(axis=0)


# ---------------------------------------------------------------------------- forest
def test_the_stored_forest_gives_exactly_the_scores_of_scikit_learn(tmp_path):
    from sklearn.ensemble import IsolationForest

    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, (4000, len(SNAPSHOT))).astype(np.float32)
    model = IsolationForest(n_estimators=60, max_samples=256, random_state=1).fit(x)
    f = Forest.from_sklearn(model)
    probe = np.concatenate([x[:500], x[:500] * 4, rng.normal(0, 8, (300, len(SNAPSHOT)))])
    assert np.allclose(f.score(probe), -model.score_samples(probe.astype(np.float32)), atol=1e-9)
    big = np.tile(probe, (5, 1))
    assert np.allclose(f.score(big)[: len(probe)], f.score(probe))
    np.savez_compressed(tmp_path / "f.npz", **f.arrays())
    with np.load(tmp_path / "f.npz", allow_pickle=False) as z:
        again = Forest.from_arrays(dict(z))
    assert np.array_equal(again.score(probe), f.score(probe))


def test_a_damaged_forest_is_refused():
    f = AnomalyDetector.load(MODELS / "anomaly.npz").forests["account"].arrays()
    with pytest.raises(ValueError):
        Forest.from_arrays({**f, "left": f["left"] + 10**6})
    with pytest.raises(ValueError):
        Forest.from_arrays({**f, "threshold": f["threshold"][:-1]})


# -------------------------------------------------------------------------- detector
@pytest.fixture(scope="module")
def detector():
    return AnomalyDetector.load(MODELS / "anomaly.npz")


def normal_rows(s="account", n=200):
    e = next(e for e in (gen.episode(i, SERVICES, CAL) for i in range(940_000, 940_400))
             if e.kind == "calm")
    return e.raw[s][20: 20 + n]


@pytest.mark.parametrize(("metric", "value", "condition"), [
    ("cpu", 96.0, "resources"), ("memory", 88.0, "resources"), ("errors", 0.6, "errors"),
    ("latency", 2500.0, "latency"), ("database", 1500.0, "database")])
def test_each_kind_of_anomaly_is_found_and_named(detector, metric, value, condition):
    raw = normal_rows()[50].copy()
    raw[IDX[metric]] = value
    a = detector.assess("account", snapshot(raw[None], PROF["account"])[0])
    assert a.level != "normal" and a.score >= 0.5, a
    assert a.condition == condition, a
    assert a.contributions[0][1] >= a.contributions[-1][1]
    assert sum(c[1] for c in a.contributions) == pytest.approx(1, abs=0.01)


def test_normal_moments_are_rarely_unusual(detector):
    rates_ = []
    for s in SERVICES:
        score = detector.score(s, snapshot(normal_rows(s), PROF[s]))
        assert ((score >= 0) & (score <= 1)).all()
        rates_.append((score >= 0.5).mean())
    assert np.mean(rates_) < 0.05


def test_explanations_name_only_what_is_unusual(detector):
    raw = normal_rows()[60].copy()
    raw[IDX["errors"]] = 0.7
    why = detector.assess("account", snapshot(raw[None], PROF["account"])[0]).contributions
    assert why[0][0] == "errors" and all(abs(z) >= 1.5 for _, _, z in why)
    assert classify([("cpu", 0.3, 3.0), ("memory", 0.3, 3.0), ("latency", 0.4, 5.0)]) == "resources"


def test_the_threshold_keeps_the_promised_false_alarm_rate():
    rng = np.random.default_rng(2)
    x = {"account": rng.normal(0, 1, (5000, len(SNAPSHOT)))}
    d = AnomalyDetector.fit(x)
    fresh = rng.normal(0, 1, (20_000, len(SNAPSHOT)))
    d.calibrate("account", fresh[:10_000], 0.01)
    assert 0.005 < (d.score("account", fresh[10_000:]) >= 0.5).mean() < 0.02


# ------------------------------------------------------------------------- predictor
@pytest.fixture(scope="module")
def small(table):
    """A small model, trained here: proves the procedure, not the quality."""
    y = table.y["failure"]
    k = np.flatnonzero(y >= 0)
    even = np.isin(table.episode[k], range(0, 24, 2))
    tr, va = k[even], k[~even]
    p = FailurePredictor.fit((table.x[tr], y[tr]), (table.x[va], y[va]), rounds=40)
    return p, va, y


def test_explanations_add_up_to_the_prediction(small, table):
    import xgboost as xgb

    p, va, _ = small
    d = xgb.DMatrix(table.x[va[:50]], feature_names=list(TEMPORAL))
    total = p.booster.predict(d, pred_contribs=True).sum(axis=1)
    assert np.allclose(total, p.booster.predict(d, output_margin=True), atol=1e-3)
    reasons = p.explain(table.x[va[0]])
    assert reasons == sorted(reasons, key=lambda r: -abs(r.contribution))
    assert set(by_metric(reasons)) == {r.metric for r in reasons}


def test_probabilities_are_calibrated_and_never_certain(small, table):
    p, va, y = small
    p.calibrate(table.x[va], y[va])
    prob = p.probability(table.x[va])
    assert prob.min() >= 0.001 and prob.max() <= 0.999
    assert abs(prob.mean() - y[va].mean()) < 0.01
    order = np.argsort(p.raw(table.x[va]))
    assert (np.diff(prob[order]) >= -1e-9).all()  # more risk never means less probability


def test_a_saved_predictor_answers_the_same(small, table):
    p, va, _ = small
    again = FailurePredictor.from_state(json.loads(json.dumps(p.to_state())))
    assert np.allclose(again.probability(table.x[va[:200]]), p.probability(table.x[va[:200]]))


def test_missing_values_are_accepted(small):
    p, _, _ = small
    x = np.full((1, len(TEMPORAL)), np.nan, dtype=np.float32)
    assert 0 < p.probability(x)[0] < 1


def test_shuffled_labels_teach_nothing(table):
    """If anything could be learned from labels that mean nothing, the data would leak."""
    rng = np.random.default_rng(4)
    y = table.y["failure"]
    k = np.flatnonzero(y >= 0)
    even = np.isin(table.episode[k], range(0, 24, 2))
    tr, va = k[even], k[~even]
    fake = FailurePredictor.fit((table.x[tr], rng.permutation(y[tr])), (table.x[va], y[va]),
                                rounds=25)
    auc = ev.ranking(y[va], fake.raw(table.x[va]))["roc_auc"]
    real = FailurePredictor.fit((table.x[tr], y[tr]), (table.x[va], y[va]), rounds=25)
    better = ev.ranking(y[va], real.raw(table.x[va]))["roc_auc"]
    assert abs(auc - 0.5) < 0.2 and better > auc


def test_every_feature_can_be_explained_in_words():
    words = [describe(f) for f in TEMPORAL]
    assert len(set(words)) == len(words) and all(len(w) > 4 for w in words)
    assert all(metric_of(f) for f in TEMPORAL)


# --------------------------------------------------------------------------- measures
def test_precision_recall_f1():
    y = np.array([1, 1, 1, 0, 0, 0, 0, 0])
    r = ev.rates(y, np.array([1, 1, 0, 1, 0, 0, 0, 0], bool))
    assert (r["precision"], r["recall"], r["f1"]) == (0.6667, 0.6667, 0.6667)
    assert r["false_positive_rate"] == 0.2
    nothing = ev.rates(y, np.zeros(8, bool))
    assert nothing["precision"] is None and nothing["recall"] == 0 and nothing["f1"] == 0


def test_ranking_without_positives_gives_no_number():
    assert ev.ranking(np.zeros(5), np.arange(5))["roc_auc"] is None


def test_threshold_for_a_precision():
    y = np.array([1, 1, 0, 1, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    assert ev.best_threshold(y, p, 0.75) == 0.6
    assert ev.best_threshold(y, p, 0.99) == 0.8


def test_alarms_do_not_flicker():
    p = np.array([0.1, 0.9, 0.1, 0.9, 0.9, 0.6, 0.55, 0.3, 0.9, np.nan, 0.9, 0.9])
    a = ev.persistent(p, 0.8, 0.5)
    assert a.tolist() == [False] * 4 + [True] * 3 + [False] * 4 + [True]


def test_events_count_leads_misses_and_false_alarms():
    n = 400
    c = cond(n, (200, 220))
    y = labels.target(c)
    alarm = np.zeros(n, bool)
    alarm[170:200] = True  # 60 s before
    alarm[20:30] = True  # nothing followed
    e = ev.Events()
    e.add("a", "leak", c, y, alarm)
    e.add("b", "crash", c, y, np.zeros(n, bool))
    r = e.result()
    assert (r["failures"], r["announced"], r["lead_s_median"]) == (2, 1, 60.0)
    assert (r["alarms"], r["false_alarms"]) == (2, 1)
    assert r["by_kind"] == {"crash": {"announced": 0, "of": 1}, "leak": {"announced": 1, "of": 1}}


def test_an_alarm_in_the_last_moment_is_no_prediction():
    c = cond(300, (200, 220))
    alarm = np.zeros(300, bool)
    alarm[199:205] = True
    e = ev.Events()
    e.add("a", "x", c, labels.target(c), alarm)
    assert e.result()["announced"] == 0


def test_the_models_belong_to_these_features():
    meta = json.loads((MODELS / "meta.json").read_text(encoding="utf-8"))
    assert tuple(meta["features"]) == TEMPORAL and tuple(meta["snapshot"]) == SNAPSHOT
    assert meta["trained_on"] == "synthetic data"
    assert len(RAW) == 12
