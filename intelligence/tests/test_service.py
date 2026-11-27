"""The live engine, the feed, the API."""

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from simucore_ai import generator as gen
from simucore_ai import labels
from simucore_ai.calibrate import MAP, MODELS, load_calibration
from simucore_ai.engine import Engine
from simucore_ai.features import IDX
from simucore_ai.feed import BadMessage, MonitoringFeed, Observation, ServiceObs, parse
from simucore_ai.main import Settings, create_app, load_models
from simucore_ai.store import Store

MODELS_ = load_models(MODELS, MAP)
SERVICES = MODELS_.services
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NORMAL = {
    "cpu_pct": 8.0, "mem_pct": 18.5, "latency_mean_ms": None, "latency_ms": None, "error_rate": 0.0,
    "rps": None, "net_rx_bps": None, "net_tx_bps": None, "db_response_ms": None,
    "active_connections": None, "queue_length": 0.0, "in_flight": 0.0}


class Bank:
    """A bank that behaves as in the recordings until told otherwise."""

    def __init__(self, rate=8.0):
        self.cal = load_calibration()["services"]
        self.rate, self.t = rate, T0
        self.override: dict[str, dict] = {}
        self.status: dict[str, str] = {}
        self.rng = np.random.default_rng(1)

    def metrics(self, s):
        c = self.cal[s]
        rps = self.rate * c["share"]
        j = lambda: float(np.exp(self.rng.normal(0, 0.04)))  # noqa: E731
        b = rps * c["bytes"][0] * j()
        m = {**NORMAL, "cpu_pct": (c["cpu"][0] + c["cpu"][1] * rps) * j(), "mem_pct": c["memory"][0],
             "latency_mean_ms": math.exp(c["latency"][0]) * j(), "rps": rps, "net_rx_bps": b * 0.5,
             "net_tx_bps": b * 0.5, "active_connections":
             float(round(c["connections"][0] + c["connections"][1] * rps)),
             "db_response_ms": math.exp(c["database"][0]) * j() if c["database"] else None}
        return {**m, **self.override.get(s, {})}

    def observe(self, seconds=2.0, origin="measured"):
        self.t += timedelta(seconds=seconds)
        return Observation(self.t, origin, {
            s: ServiceObs(self.status.get(s, "up"),
                          self.metrics(s) if self.status.get(s, "up") == "up" else {})
            for s in SERVICES})


@pytest.fixture
def bank():
    return Bank()


@pytest.fixture
def engine():
    return Engine(MODELS_)


def run(engine, bank, n, **kw):
    return [engine.observe(bank.observe(**kw)) for _ in range(n)]


def all_of(steps, what):
    return [x for s in steps for x in getattr(s, what)]


# ----------------------------------------------------------------------------- calm
def test_normal_operation_is_neither_unusual_nor_at_risk(engine, bank):
    steps = run(engine, bank, 120)
    assert all_of(steps, "anomalies") == [] and all_of(steps, "warnings") == []
    for a in engine.current.values():
        assert a.anomaly["score"] < 0.5 and all(r["level"] == "low" for r in a.risk.values())


def test_no_prediction_without_enough_history(engine, bank):
    run(engine, bank, 10)
    a = engine.current["account"]
    assert a.risk == {} and "30 s" in a.risk_reason and a.anomaly is not None
    run(engine, bank, 10)
    assert set(engine.current["account"].risk) == set(labels.TARGETS)


# -------------------------------------------------------------------------- anomalies
def test_an_anomaly_opens_grows_and_closes(engine, bank):
    run(engine, bank, 40)
    bank.override["account"] = {"error_rate": 0.35}
    steps = run(engine, bank, 4)
    opened = all_of(steps, "anomalies")[0]
    assert opened.service == "account" and opened.condition == "errors"
    assert opened.opened_at >= T0 + timedelta(seconds=82)  # not at the first odd moment
    assert opened.contributions[0]["metric"] == "errors"
    assert [e.service for e in engine.open_anomalies()] == ["account"]
    bank.override["account"] = {"error_rate": 0.45, "latency_mean_ms": 3000.0, "cpu_pct": 99.0}
    run(engine, bank, 4)
    assert opened.level == "abnormal" and opened.closed_at is None
    bank.override.clear()
    steps = run(engine, bank, 8)
    assert opened.closed_at is not None and engine.open_anomalies() == []


def test_one_odd_moment_is_not_an_anomaly(engine, bank):
    run(engine, bank, 40)
    bank.override["account"] = {"error_rate": 0.4}
    steps = run(engine, bank, 1)
    bank.override.clear()
    steps += run(engine, bank, 10)
    assert all_of(steps, "anomalies") == []


def test_a_service_that_is_down_is_not_scored(engine, bank):
    run(engine, bank, 40)
    bank.status["payment"] = "down"
    run(engine, bank, 2)
    a = engine.current["payment"]
    assert a.anomaly is None and "down" in a.anomaly_reason
    assert a.risk == {} and "nothing left to predict" in a.risk_reason


def test_an_open_anomaly_ends_when_the_service_goes_down(engine, bank):
    run(engine, bank, 40)
    bank.override["account"] = {"error_rate": 0.4}
    run(engine, bank, 4)
    assert engine.open_anomalies()
    bank.status["account"] = "down"
    run(engine, bank, 1)
    assert engine.open_anomalies() == []


# -------------------------------------------------------------------------- prediction
def leak(engine, bank, service="account", until=80.0, rate=0.9):
    steps, mem = run(engine, bank, 45), 18.5
    mine = lambda: [w for w in engine.open_warnings() if w.service == service]  # noqa: E731
    while mem < until and not mine():
        mem += rate
        bank.override[service] = {"mem_pct": mem}
        steps += run(engine, bank, 1)
    return steps, mem


def test_a_leak_is_announced_before_the_failure_and_the_warning_comes_true(engine, bank):
    steps, mem = leak(engine, bank)
    mine = [w for w in engine.open_warnings() if w.service == "account"]
    assert mine, "no warning although memory was running out"
    w = mine[0]
    assert w.probability >= MODELS_.thresholds[w.target]
    assert any(r["metric"] == "memory" and r["contribution"] > 0 for r in w.reasons)
    at = engine.at
    while mem < 99:
        mem += 0.9
        bank.override["account"] = {"mem_pct": min(99, mem)}
        steps += run(engine, bank, 1)
    bank.status["account"] = "down"
    steps += run(engine, bank, 3)
    done = [x for x in all_of(steps, "warnings") if x.outcome == "came_true"]
    assert done and all(x.lead_s > 0 for x in done) and engine.at > at
    assert not [x for x in engine.open_warnings() if x.service == "account"]


def test_a_warning_that_does_not_come_true_is_recorded_as_such(engine, bank):
    leak(engine, bank)
    w = next(w for w in engine.open_warnings() if w.service == "account")
    bank.override.clear()  # freed
    steps = run(engine, bank, 100)
    assert w.outcome == "did_not_happen" and w.lead_s is None and w.closed_at is not None
    assert w in all_of(steps, "warnings")


def test_nothing_is_predicted_while_it_is_present_or_just_over(engine, bank):
    run(engine, bank, 45)
    bank.override["account"] = {"error_rate": 0.8}
    run(engine, bank, 4)
    assert engine.current["account"].risk["failure"]["level"] == "present"
    assert engine.current["account"].risk["failure"]["probability"] is None
    bank.override.clear()
    run(engine, bank, 4)
    assert engine.current["account"].risk["failure"]["level"] == "recovering"
    run(engine, bank, 40)
    assert engine.current["account"].risk["failure"]["probability"] is not None


def test_every_stored_prediction_gets_its_outcome(engine, bank):
    steps = run(engine, bank, 50)
    stored = all_of(steps, "predictions")
    assert stored and len({p.id for p in stored}) == len(stored)
    per_ten_seconds = len(stored) / 50 / 2 * 10
    assert per_ten_seconds < 8 * 2 * 1.1
    bank.status["fraud"] = "down"
    steps += run(engine, bank, 3)
    failed = [p for p in all_of(steps, "outcomes") if p.service == "fraud"]
    assert failed and all(p.outcome for p in failed)
    steps += run(engine, bank, 70)
    quiet = [p for p in all_of(steps, "outcomes") if p.service == "customer"]
    assert quiet and not any(p.outcome for p in quiet)
    assert engine.performance()["failure"]["resolved"] > 0


def test_predictions_made_too_late_do_not_count(engine, bank):
    run(engine, bank, 46)
    pending = {p.id for p in engine.states["fraud"].pending
               if (engine.at - p.ts).total_seconds() <= 1}
    bank.status["fraud"] = "down"
    step = engine.observe(bank.observe())
    assert not pending & {p.id for p in step.outcomes}


# ------------------------------------------------------------------------ interruptions
def test_a_hole_in_the_data_makes_it_forget(engine, bank):
    leak(engine, bank)
    w = engine.open_warnings()[0]
    run(engine, bank, 1, seconds=60)
    assert w.outcome == "unresolved" and engine.open_warnings() == []
    assert engine.current["account"].risk == {}  # needs history again


def test_measured_and_synthetic_do_not_mix(engine, bank):
    leak(engine, bank)
    run(engine, bank, 1, origin="synthetic")
    assert engine.open_warnings() == [] and engine.current["account"].risk == {}


def test_a_blind_monitor_says_nothing_about_the_services(engine, bank):
    run(engine, bank, 40)
    for s in SERVICES:
        bank.status[s] = "unresponsive"
    steps = run(engine, bank, 5)
    assert all_of(steps, "anomalies") == [] and all_of(steps, "outcomes") == []
    assert all("sees no service" in a.risk_reason for a in engine.current.values())


def test_old_opinions_expire(engine, bank):
    run(engine, bank, 40)
    assert engine.expire(bank.t + timedelta(seconds=5), 8) is None
    assert engine.expire(bank.t + timedelta(seconds=9), 8) is not None
    assert engine.current == {}


def test_repeated_and_older_observations_change_nothing(engine, bank):
    run(engine, bank, 20)
    obs = bank.observe()
    engine.observe(obs)
    n = engine.scored
    assert engine.observe(obs).anomalies == [] and engine.scored == n


def test_the_engine_agrees_with_the_training_tables():
    """The same episode, once tick by tick and once as in training: the same probabilities."""
    from simucore_ai.train import rows

    e = gen.episode(950_001, SERVICES, load_calibration())
    table = rows([e], SERVICES, MODELS_.profiles)
    engine, bank = Engine(MODELS_), Bank()
    keys = {"cpu": "cpu_pct", "memory": "mem_pct", "latency": "latency_mean_ms",
            "errors": "error_rate", "requests": "rps", "network": "net_rx_bps",
            "database": "db_response_ms", "connections": "active_connections"}
    for t in range(140):
        bank.t += timedelta(seconds=2)
        services = {}
        for s, spec in SERVICES.items():
            r = e.raw[s][t]
            m = {v: (None if np.isnan(r[IDX[k]]) else float(r[IDX[k]])) for k, v in keys.items()}
            m["net_tx_bps"] = 0.0
            m["queue_length"] = None if np.isnan(r[IDX["queue"]]) else r[IDX["queue"]] * \
                spec.max_concurrency
            m["in_flight"] = None if np.isnan(r[IDX["in_flight"]]) else r[IDX["in_flight"]] * \
                spec.max_concurrency
            if not np.isnan(r[IDX["pool"]]):
                m |= {"db_pool_size": 8.0, "db_pool_in_use": r[IDX["pool"]] * 8}
            services[s] = ServiceObs(str(e.status[s][t]), m)
        engine.observe(Observation(bank.t, "synthetic", services))
        if t in (40, 80, 120, 139):
            for s, a in engine.current.items():
                for target, v in a.risk.items():
                    if v["probability"] is None:
                        continue
                    k = (table.service == s) & (table.tick == t)
                    want = MODELS_.predictors[target].probability(table.x[k])[0]
                    assert v["probability"] == pytest.approx(want, abs=2e-3), (s, target, t)


# ------------------------------------------------------------------------------- feed
def message(**kw):
    return {"type": "update", "fresh": True, "origin": "measured",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "services": [{"service": "account", "status": "up", "metrics": {"cpu_pct": 1.0}}], **kw}


def test_a_message_is_read():
    o = parse(json.dumps(message()), set(SERVICES))
    assert o.services["account"].metrics == {"cpu_pct": 1.0} and o.origin == "measured"


@pytest.mark.parametrize("bad", [
    "{", "[]", json.dumps(message(type="x")), json.dumps(message(fresh=False)),
    json.dumps(message(origin="guess")), json.dumps(message(collected_at="yesterday")),
    json.dumps(message(collected_at="2026-01-01T00:00:00")), json.dumps(message(services={})),
    json.dumps(message(services=[{"service": "stranger", "status": "up"}]))])
def test_unusable_messages_are_refused(bad):
    with pytest.raises(BadMessage):
        parse(bad, set(SERVICES))


@pytest.mark.parametrize("value", ["9", True, -1, 1e300, float("nan"), None, [], {}])
def test_impossible_values_become_unknown(value):
    raw = json.dumps(message(services=[{"service": "account", "status": "up",
                                        "metrics": {"cpu_pct": value}}]))
    assert parse(raw, set(SERVICES)).services["account"].metrics["cpu_pct"] is None


@pytest.mark.parametrize("url", ["ws://example.com/x", "http://127.0.0.1:8200/x", "wss://bank/x"])
def test_only_the_monitoring_service_is_listened_to(url):
    with pytest.raises(ValueError):
        MonitoringFeed(url)


def test_the_service_cannot_act_on_the_bank():
    """It has no HTTP client, imports nothing of the bank, and never sends on a WebSocket feed."""
    from pathlib import Path

    import simucore_ai

    for f in Path(simucore_ai.__file__).parent.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "import httpx" not in text and "import requests" not in text, f
        assert "bankcommon" not in text and "bankctl" not in text, f
        assert "pickle.load" not in text and "allow_pickle=True" not in text, f
    feed = (Path(simucore_ai.__file__).parent / "feed.py").read_text(encoding="utf-8")
    assert ".send(" not in feed


# -------------------------------------------------------------------------------- API
@pytest.fixture
def api():
    app = create_app(Settings(monitoring_ws="ws://127.0.0.1:9/x", database_url=None))
    with TestClient(app) as c:
        c.service = app.state.service
        c.bank = Bank()

        def feed(n=1):
            for i in range(n):
                o = c.bank.observe()
                at = datetime.now(UTC) - timedelta(seconds=2 * (n - i) - 1)
                c.portal.call(c.service.handle, json.dumps({
                    "type": "update", "fresh": True, "origin": "measured",
                    "collected_at": at.isoformat(),
                    "services": [{"service": s, "status": v.status, "metrics": v.metrics}
                                 for s, v in o.services.items()]}))
        c.feed = feed
        yield c


def test_api_before_data(api):
    assert api.get("/health/live").status_code == 200
    assert api.get("/health/ready").status_code == 503
    body = api.get("/api/v1/ai/insights").json()
    assert body["fresh"] is False and body["services"] == []


def test_api_insights_keep_the_two_methods_apart(api):
    api.feed(30)
    body = api.get("/api/v1/ai/insights").json()
    assert body["fresh"] and len(body["services"]) == 8
    s = body["services"][0]
    assert set(s["risk"]) == {"degradation", "failure"} and "score" in s["anomaly"]
    assert "probability" not in s["anomaly"] and "score" not in s["risk"]["failure"]
    m = api.get("/api/v1/ai/model").json()
    assert "unsupervised" in m["anomaly_detection"] and "supervised" in m["failure_prediction"]
    assert m["trained_on"] == "synthetic data" and m["test"]["failure"]["xgboost"]["roc_auc"]


def test_api_history_without_a_database_says_so(api):
    for path in ("anomalies", "warnings", "predictions/bank-account", "performance"):
        assert api.get(f"/api/v1/ai/{path}").status_code == 503, path


@pytest.mark.parametrize("query", ["node_id=../etc", "minutes=0", "limit=99999", "origin=x",
                                   "start=2026-01-01T00:00:00",
                                   "start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z"])
def test_api_refuses_bad_questions(api, query):
    assert api.get(f"/api/v1/ai/anomalies?{query}").status_code in (422, 503)
    if "start" not in query:
        assert api.get(f"/api/v1/ai/anomalies?{query}").status_code == 422


def test_api_is_read_only(api):
    for path in api.app.openapi()["paths"].values():
        assert set(path) == {"get"}


def test_stream_snapshot_then_updates(api):
    api.feed(20)
    with api.websocket_connect("/ws/insights") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and len(first["services"]) == 8
        api.feed(1)
        second = ws.receive_json()
        assert second["type"] == "update" and second["sequence"] > first["sequence"]


def test_stream_refuses_other_web_pages(api):
    with pytest.raises(Exception):  # noqa: B017
        with api.websocket_connect("/ws/insights", headers={"origin": "http://evil.example"}):
            pass


def test_own_metrics(api):
    api.feed(30)
    text = api.get("/metrics").text
    assert 'ai_anomaly_score{service="account"}' in text
    assert 'ai_risk_probability{service="account",target="failure"}' in text


# ------------------------------------------------------------------------------- store
def test_rows_wait_for_the_database_and_the_oldest_are_dropped(engine, bank, monkeypatch):
    import simucore_ai.store as store_module

    monkeypatch.setattr(store_module, "BUFFER", 40)
    s = Store("postgresql://x", MODELS_.meta, 30)
    for step in run(engine, bank, 60):
        s.add(step, "measured", 120)
    assert len(s.buffer) == 40 and s.dropped > 0


def test_stored_rows_are_marked_with_model_and_origin(engine, bank):
    s = Store("postgresql://x", MODELS_.meta, 30)
    s.model_id = "the-model"
    for step in run(engine, bank, 30):
        s.add(step, "synthetic", 120)
    sql, row = next((q, r) for q, r in s.buffer if "INTO prediction " in q)
    filled = s._filled(sql, row)
    assert filled[7] == "the-model" and filled[8] == "synthetic" and len(filled) == len(row)
