"""The opinions of the AI service in the digital twin."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.digital_twin import schemas
from app.digital_twin.feed import BadMessage
from app.digital_twin.insights import InsightFeed, parse
from app.digital_twin.model import Status
from app.digital_twin.state import DigitalTwin
from app.main import create_app
from tests.test_digital_twin import MAP, World, message, run

NOW = datetime(2026, 1, 1, tzinfo=UTC)
IDS = {"account": "bank-account", "payment": "bank-payment"}


def opinion(service="account", score=0.2, level="normal", p=0.02, risk="low", since=None,
            warnings=(), **kw):
    return {
        "service": service, "node_id": f"bank-{service}",
        "anomaly": {"score": score, "level": level, "condition": "resources" if since else "normal",
                    "contributions": [{"metric": "cpu", "share": 0.8, "deviation": 6.0}]
                    if since else []},
        "open_anomaly": {"opened_at": since} if since else None, "anomaly_reason": "",
        "risk": {t: {"probability": p, "level": risk, "horizon_s": 120}
                 for t in ("degradation", "failure")},
        "risk_reason": "", "warnings": list(warnings),
        "reasons": [{"metric": "cpu", "text": "CPU trend over 60 s", "contribution": 1.2}], **kw}


def warning(target="failure", p=0.8):
    return {"id": "w1", "target": target, "probability": p, "peak": p, "horizon_s": 120,
            "opened_at": "2026-01-01T00:00:00+00:00"}


def ai(*opinions, at=None, **kw):
    return json.dumps({"type": "update", "fresh": True, "origin": "measured", "model": "abc",
                       "assessed_at": (at or NOW).isoformat(),
                       "services": list(opinions) or [opinion()], **kw})


@pytest.fixture
def world():
    return World()


@pytest.fixture
def twin(world):
    t = DigitalTwin(MAP)
    run(t, world, 3)
    return t


def tell(twin, *opinions, **kw):
    return twin.apply_insights(parse(ai(*opinions, **kw), IDS), NOW)


def test_opinions_reach_the_component_and_nothing_else(twin):
    before = {c: s.status for c, s in twin.components.items()}
    change = tell(twin, opinion(score=0.9, level="abnormal", since="2026-01-01T00:00:00+00:00",
                                p=0.85, risk="high", warnings=[warning()]))
    assert change.components == {"bank-account"} and not change.transitions
    view = schemas.component_view(twin, "bank-account")
    assert view.insight.anomaly.score == 0.9 and view.insight.anomaly.contributions[0].share == 0.8
    assert {r.target for r in view.insight.risks} == {"degradation", "failure"}
    assert view.insight.warnings[0].probability == 0.8
    assert schemas.component_view(twin, "bank-payment").insight is None
    # an opinion is not a measurement
    assert {c: s.status for c, s in twin.components.items()} == before
    assert view.status is Status.HEALTHY and view.findings == []


def test_summary_counts_confirmed_anomalies_and_warnings(twin):
    assert schemas.summary(twin).ai == "absent"
    tell(twin, opinion(score=0.7, level="unusual"),  # unusual, but not confirmed yet
         opinion("payment", warnings=[warning()]))
    s = schemas.summary(twin)
    assert s.ai == "live" and s.anomalies == [] and s.predicted == ["bank-payment"]
    assert s.needs_attention == []  # statuses only


def test_an_unchanged_opinion_is_no_change(twin):
    assert tell(twin, opinion())
    v = twin.version
    assert not tell(twin, opinion(score=0.2004), at=NOW + timedelta(seconds=2))
    assert twin.version == v and twin.insights_at == NOW + timedelta(seconds=2)
    assert tell(twin, opinion(score=0.3), at=NOW + timedelta(seconds=4))
    assert twin.version == v + 1


def test_old_opinions_are_ignored_and_expire(twin):
    tell(twin, opinion(score=0.9), at=NOW)
    assert not tell(twin, opinion(score=0.1), at=NOW - timedelta(seconds=10))
    assert twin.insights["bank-account"].anomaly.score == 0.9
    assert not twin.expire_insights(NOW + timedelta(seconds=9), 10)
    change = twin.expire_insights(NOW + timedelta(seconds=11), 10)
    assert change.components == {"bank-account"} and twin.insights == {}
    assert schemas.summary(twin).ai == "absent"


def test_opinions_about_other_data_are_not_accepted(twin):
    assert not tell(twin, opinion(), origin="synthetic") and twin.insights == {}


def test_without_measurements_there_are_no_opinions(twin):
    tell(twin, opinion())
    twin.expire(datetime.now(UTC) + timedelta(days=400))
    tell(twin, opinion())
    assert twin.insights == {}


def test_a_clone_has_its_own_opinions(twin):
    tell(twin, opinion())
    copy = twin.clone()
    tell(copy, opinion(score=0.99))
    assert twin.insights["bank-account"].anomaly.score == 0.2


@pytest.mark.parametrize("bad", [
    "{", "[]", ai(type="x"), ai(fresh=False), ai(origin="guess"), ai(assessed_at="now"),
    ai(assessed_at="2026-01-01T00:00:00"), ai(services="x")])
def test_unusable_messages_are_refused(bad):
    with pytest.raises(BadMessage):
        parse(bad, IDS)


@pytest.mark.parametrize("change", [
    {"anomaly": {"score": 7, "level": "normal", "condition": "", "contributions": []}},
    {"anomaly": {"score": 0.5, "level": "catastrophic", "condition": "", "contributions": []}},
    {"risk": {"failure": {"probability": "high", "level": "low", "horizon_s": 120}}},
    {"risk": {"failure": {"probability": -1, "level": "low", "horizon_s": 120}}},
    {"warnings": [{"id": "x"}]}, {"reasons": [{"metric": "cpu"}]}, {"warnings": 5}])
def test_an_impossible_opinion_is_left_out_the_others_stay(change):
    m = parse(ai({**opinion(), **change}, opinion("payment"), opinion("stranger")), IDS)
    assert set(m.insights) == {"bank-payment"}


def test_texts_are_cut():
    m = parse(ai(opinion(reasons=[{"metric": "x" * 999, "text": "y" * 9999, "contribution": 1}])),
              IDS)
    r = m.insights["bank-account"].reasons[0]
    assert len(r.metric) == 40 and len(r.text) == 120


@pytest.mark.parametrize("url", ["ws://example.com/x", "http://intelligence:8300/x",
                                 "ws://monitoring:8200/x"])
def test_only_the_ai_service_is_listened_to(url):
    with pytest.raises(ValueError):
        InsightFeed(url)


# -------------------------------------------------------------------------------- API
@pytest.fixture
def api():
    app = create_app(Settings(twin_monitoring_ws="ws://127.0.0.1:9/ws",
                              twin_ai_ws="ws://127.0.0.1:9/ws", telemetry_interval_s=5))
    with TestClient(app) as c:
        c.world = World()
        c.world.now = datetime.now(UTC) - timedelta(seconds=1)
        c.portal.call(app.state.twin_sync.handle, json.dumps(message(c.world)))
        c.think = lambda *o: c.portal.call(app.state.insight_sync.handle,
                                           ai(*o, at=datetime.now(UTC)))
        yield c


def test_api_shows_opinions(api):
    assert api.get("/api/v2/twin/insights").json() == {}
    api.think(opinion(p=0.7, risk="high", warnings=[warning()]))
    body = api.get("/api/v2/twin/insights").json()
    assert body["bank-account"]["warnings"][0]["target"] == "failure"
    assert api.get("/api/v2/twin").json()["summary"]["predicted"] == ["bank-account"]
    assert api.get("/api/v2/twin/sync").json()["ai"]["applied"] == 1


def test_stream_carries_opinions(api):
    with api.websocket_connect("/ws/v2/twin") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        api.think(opinion(score=0.9, level="abnormal", since="2026-01-01T00:00:00+00:00"))
        m = ws.receive_json()
        assert m["type"] == "delta" and [c["id"] for c in m["components"]] == ["bank-account"]
        assert m["components"][0]["insight"]["anomaly"]["level"] == "abnormal"
        assert m["summary"]["anomalies"] == ["bank-account"] and m["transitions"] == []


def test_nonsense_from_the_ai_service_changes_nothing(api):
    api.think(opinion())
    before = api.get("/api/v2/twin/insights").json()
    api.portal.call(api.app.state.insight_sync.handle, "{}")
    assert api.get("/api/v2/twin/insights").json() == before
    assert api.get("/api/v2/twin/sync").json()["ai"]["refused"] == 1
