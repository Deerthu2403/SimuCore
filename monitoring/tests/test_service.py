"""Quality events, the collector loop, the API and the WebSocket."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from monitor.catalog import METRICS
from monitor.collector import Collector
from monitor.config import Settings, load_services
from monitor.hub import QUEUE, Hub
from monitor.main import create_app
from monitor.models import Collection, InfraSample, SourceUnavailable, utcnow
from monitor.quality import EventBook, Issue
from monitor.sources.synthetic import SyntheticSource
from monitor.store import Store

SERVICES = load_services(Settings().services_file)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class ScriptedSource:
    """Healthy synthetic values presented as measured, with scripted trouble."""

    name = "scripted"

    def __init__(self):
        self.inner = SyntheticSource(Settings(), SERVICES)
        self.fail: Exception | None = None
        self.edit = lambda c: None
        self.delay = 0.0

    async def collect(self, with_infra: bool) -> Collection:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        noon = T0.replace(hour=12)
        while any(self.inner.condition(s.name, noon) != "normal" for s in SERVICES):
            noon += timedelta(minutes=5)
        c = self.inner.at(noon, with_infra)
        now = utcnow()
        c = Collection(now, "measured", c.samples, c.infra, c.links)
        for s in (*c.samples, *c.links):
            s.ts = now
        self.edit(c)
        return c

    async def aclose(self):
        pass


def make(source, **kw) -> Collector:
    return Collector(Settings(tick_s=0.05, infra_every=1, **kw), SERVICES, source, None, Hub())


def service(c: Collection, name: str):
    return next(s for s in c.samples if s.service == name)


# ------------------------------------------------------------------ events
def test_events_need_persistence_to_open_and_to_close():
    book, scope = EventBook(), {"metric_missing"}
    issue = Issue("metric_missing", "gone", "account", "cpu_pct")
    for n in range(3):
        assert book.update([issue], T0 + timedelta(seconds=n), scope) == []
    opened = book.update([issue], T0, scope)
    assert len(opened) == 1 and opened[0].closed_at is None
    assert book.update([issue], T0, scope) == []  # no repetition
    assert book.update([], T0, scope) == []  # one good tick is not enough
    assert book.update([issue], T0, scope) == []  # flapping stays one event
    book.update([], T0, scope)
    closed = book.update([], T0 + timedelta(seconds=9), scope)
    assert closed[0].id == opened[0].id and closed[0].closed_at
    assert book.open == {}


def test_a_brief_glitch_raises_nothing():
    book, scope = EventBook(), {"metric_missing"}
    issue = Issue("metric_missing", "gone", "account", "cpu_pct")
    for _ in range(10):
        assert book.update([issue], T0, scope) == [] and book.update([], T0, scope) == []


def test_kinds_out_of_scope_are_left_untouched():
    book = EventBook()
    issue = Issue("service_down", "refused", "account")
    book.update([issue], T0, {"service_down"})
    for _ in range(10):
        book.update([], T0, {"source_unavailable"})  # Prometheus is away: nothing is known
    assert len(book.open) == 1


# ------------------------------------------------------------------ collector
async def test_missing_metric_is_detected_and_clears():
    src = ScriptedSource()
    col = make(src)
    src.edit = lambda c: service(c, "account").metrics.pop("cpu_pct")
    for _ in range(4):
        await col.tick()
    assert [(e.issue.kind, e.issue.service, e.issue.metric) for e in col.book.open.values()] == \
        [("metric_missing", "account", "cpu_pct")]
    src.edit = lambda c: None
    for _ in range(2):
        await col.tick()
    assert col.book.open == {}


async def test_database_metric_is_not_expected_where_there_is_no_database():
    col = make(ScriptedSource())
    for _ in range(6):
        await col.tick()
    assert "db_response_ms" not in service(col.current, "notification").metrics
    assert col.book.open == {}


async def test_unmeasured_connection_is_detected():
    src = ScriptedSource()
    col = make(src)

    def edit(c):
        c.links = [k for k in c.links if k.id != "account->database"]
        next(k for k in c.links if k.id == "payment->database").put("latency_ms", -3)
    src.edit = edit
    for _ in range(4):
        await col.tick()
    found = {(e.issue.kind, e.issue.service, e.issue.metric) for e in col.book.open.values()}
    assert found == {("metric_missing", "account", "link:database"),
                     ("metric_invalid", "payment", "link:database")}


async def test_a_silent_caller_says_nothing_about_its_connections():
    src = ScriptedSource()
    col = make(src)

    def edit(c):
        service(c, "account").status = "unresponsive"
        c.links = [k for k in c.links if k.source != "account"]
    src.edit = edit
    for _ in range(5):
        await col.tick()
    assert {e.issue.kind for e in col.book.open.values()} == {"service_unresponsive"}


async def test_message_identifies_the_run_and_the_moment():
    a, b = make(ScriptedSource()), make(ScriptedSource())
    await a.tick()
    m = a.message("update")
    assert m["collected_at"] == a.current.ts.isoformat() and a.run_id != b.run_id
    assert len(m["links"]) == 14 and {"deployment"} <= m["services"][0].keys()


async def test_source_failure_keeps_last_data_and_marks_it_stale():
    src = ScriptedSource()
    col = make(src)
    await col.tick()
    src.fail = SourceUnavailable("Prometheus: refused")
    for _ in range(3):
        await col.tick()
    assert {e.issue.kind for e in col.book.open.values()} == {"source_unavailable"}
    assert col.status_view()["source_error"] == "Prometheus: refused"
    col.current.ts = utcnow() - timedelta(minutes=1)
    assert not col.fresh and col.status_view()["state"] == "failed"
    assert col.message("x")["fresh"] is False
    assert col.twin_view()["samples"] == []  # the twin never gets old data as if it were new


async def test_infrastructure_failure_is_an_event():
    src = ScriptedSource()
    col = make(src)
    src.edit = lambda c: c.infra.append(InfraSample("platform-postgres", c.ts, "down",
                                                    reason="exporter reports it down"))
    for _ in range(2):
        await col.tick()
    assert [(e.issue.kind, e.issue.service) for e in col.book.open.values()] == \
        [("component_down", "platform-postgres")]


async def test_slow_collection_is_an_event():
    src = ScriptedSource()
    src.delay = 0.08
    col = make(src)
    for _ in range(3):
        await col.tick()
    assert "collector_slow" in {e.issue.kind for e in col.book.open.values()}


async def test_loop_survives_unexpected_errors():
    src = ScriptedSource()
    col = make(src)
    src.fail = ZeroDivisionError("bug")
    await col.start()
    await asyncio.sleep(0.3)
    assert col.failed_ticks >= 2
    src.fail = None
    await asyncio.sleep(0.2)
    assert col.fresh
    await col.stop()


async def test_twin_view_contains_only_knowledge():
    src = ScriptedSource()

    def edit(c):
        service(c, "account").status = "down"
        service(c, "fraud").status = "unresponsive"
        service(c, "payment").metrics.update(latency_ms=None, cpu_pct=250.0)
    src.edit = edit
    col = make(src)
    await col.tick()
    view = col.twin_view()
    by = {s["node_id"]: s["metrics"] for s in view["samples"]}
    assert by["bank-account"] == {"availability": 0.0}
    assert "bank-fraud" not in by
    assert "latency_ms" not in by["bank-payment"] and by["bank-payment"]["cpu_pct"] == 100
    assert view["origin"] == "measured" and view["fresh"]


async def test_overview_weights_errors_by_traffic():
    src = ScriptedSource()

    def edit(c):
        for s in c.samples:
            s.metrics.update(rps=0.0, error_rate=None)
        service(c, "account").metrics.update(rps=90.0, error_rate=0.0)
        service(c, "payment").metrics.update(rps=10.0, error_rate=0.5)
    src.edit = edit
    col = make(src)
    await col.tick()
    assert col.overview()["total_rps"] == 100 and col.overview()["error_rate"] == 0.05


# ------------------------------------------------------------------ store buffer
def test_buffer_is_bounded_and_losses_are_counted():
    per_tick = 8 + sum(len(s.targets) for s in SERVICES)
    store = Store("postgresql://nowhere", "x", buffer_rows=30, retention_days=30)
    src = SyntheticSource(Settings(), SERVICES)
    for i in range(5):
        store.add(src.at(T0 + timedelta(seconds=i), with_infra=False))
    assert len(store.buffer) == 30 and store.dropped == 5 * per_tick - 30
    assert store.buffer[-1][1][0] == T0 + timedelta(seconds=4)  # the newest are kept


async def test_database_outage_is_an_event_and_does_not_stop_collection():
    store = Store("postgresql://x:y@127.0.0.1:9/none", "x", 1000, 30)
    col = Collector(Settings(tick_s=0.05), SERVICES, ScriptedSource(), store, Hub())
    await col.start()

    def as_expected() -> bool:
        return (col.fresh and store.pool is None and len(store.buffer) >= 8
                and "store_failed" in {e.issue.kind for e in col.book.open.values()}
                and col.status_view()["state"] == "degraded")

    for _ in range(100):  # a busy machine may need longer; the condition stays the same
        if as_expected():
            break
        await asyncio.sleep(0.1)
    assert as_expected()
    await col.stop()


# ------------------------------------------------------------------ hub
def test_slow_client_loses_old_updates_only():
    hub = Hub()
    with hub.join() as slow, hub.join() as quick:
        for i in range(QUEUE + 5):
            hub.publish({"n": i})
            quick.queue.get_nowait()
        assert slow.skipped == 5 and quick.skipped == 0
        assert slow.queue.get_nowait()["n"] == 5
    assert hub.clients == set()


# ------------------------------------------------------------------ API
@pytest.fixture
def client():
    app = create_app(Settings(tick_s=0.1, source="synthetic"), ScriptedSource())
    with TestClient(app) as c:
        time.sleep(0.4)
        yield c


def test_status_and_health(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").json()["ready"] is True
    s = client.get("/api/v1/monitoring/status").json()
    assert s["state"] == "ok" and s["origin"] == "measured" and s["ticks"] >= 2


def test_current_metrics(client):
    d = client.get("/api/v1/metrics/current").json()
    assert len(d["services"]) == 8 and d["fresh"] and d["overview"]["up"] == 8
    one = client.get("/api/v1/metrics/current/account").json()
    assert set(one["metrics"]) == {m.key for m in METRICS if not m.only}
    links = client.get("/api/v1/links/current").json()
    assert len(links["links"]) == 14 and links["origin"] == "measured"
    assert client.get("/api/v1/metrics/current/nobody").status_code == 404
    assert client.get("/api/v1/infrastructure/current").json()["components"]


def test_catalog_covers_everything_that_was_asked_for(client):
    keys = {m["key"] for m in client.get("/api/v1/metrics/catalog").json()["metrics"]}
    assert {"cpu_pct", "mem_pct", "latency_ms", "rps", "error_rate", "net_rx_bps", "net_tx_bps",
            "db_response_ms", "active_connections", "availability", "queue_length"} <= keys


@pytest.mark.parametrize("path", [
    "/api/v1/metrics/history/account?metrics=secret",
    "/api/v1/metrics/history/account?metrics=cpu_pct;drop",
    "/api/v1/metrics/history/account?metrics=",
    "/api/v1/metrics/history/account?start=2026-01-01T00:00:00",
    "/api/v1/metrics/history/account?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z",
    "/api/v1/metrics/history/account?minutes=40000&step=1",
    "/api/v1/metrics/history/account?minutes=0",
    "/api/v1/metrics/history/account?origin=made-up",
    "/api/v1/metrics/history/..%2Fetc",
    "/api/v1/events/history?limit=99999",
])
def test_bad_queries_are_refused(client, path):
    assert client.get(path).status_code in (404, 422), path


def test_history_without_a_database_says_so(client):
    r = client.get("/api/v1/metrics/history/account")
    assert r.status_code == 503 and "database" in r.json()["detail"]


def test_api_is_read_only(client):
    for route in client.app.routes:
        assert not set(getattr(route, "methods", None) or ()) & {"POST", "PUT", "PATCH", "DELETE"}


def test_own_metrics(client):
    assert "monitor_up 1.0" in client.get("/metrics").text


# ------------------------------------------------------------------ websocket
def test_websocket_sends_snapshot_then_continuous_updates(client):
    with client.websocket_connect("/ws/metrics") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and len(first["services"]) == 8
        seq = [ws.receive_json()["sequence"] for _ in range(4)]
        assert seq == sorted(seq) and len(set(seq)) >= 3


def test_websocket_subscription(client):
    with client.websocket_connect("/ws/metrics") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "services": ["account", "fraud"]})
        for _ in range(6):
            m = ws.receive_json()
            if len(m["services"]) == 2:
                break
        assert {s["service"] for s in m["services"]} == {"account", "fraud"}
        assert {k["source"] for k in m["links"]} == {"account", "fraud"}


@pytest.mark.parametrize("text", ["nonsense", "[]", '{"type":"subscribe","services":["x"]}',
                                  '{"type":"write"}', "x" * 5000])
def test_websocket_refuses_bad_messages(client, text):
    with client.websocket_connect("/ws/metrics") as ws:
        ws.send_text(text)
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(20):
                ws.receive_json()
        assert exc.value.code == 1003


def test_websocket_refuses_other_web_pages(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/metrics", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_json()
    with client.websocket_connect("/ws/metrics",
                                  headers={"origin": "http://localhost:8080"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_websocket_announces_events(client):
    col = client.app.state.collector
    with client.websocket_connect("/ws/metrics") as ws:
        assert ws.receive_json()["events"] == []
        col.source.edit = lambda c: setattr(service(c, "account"), "status", "down")
        for _ in range(30):
            m = ws.receive_json()
            if m["type"] == "update" and m["events"]:
                break
        assert m["events"][0]["kind"] == "service_down"
        assert m["events"][0]["service"] == "account"
