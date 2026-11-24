import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import MonitoringAdapter
from app.core.config import Settings
from app.core.events import EventBus
from app.domain.models import HealthStatus
from app.main import create_app
from app.simulation.bank_services import load_bank_services_topology
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer

NODES = [n.id for n in load_bank_services_topology().nodes]
GOOD = {"cpu_pct": 5.0, "mem_pct": 20.0, "latency_ms": 5.0, "error_rate": 0.0, "rps": 3.0,
        "availability": 1.0, "net_rx_bps": 900.0, "net_tx_bps": 1200.0, "db_response_ms": 2.0,
        "active_connections": 4.0, "queue_length": 0.0}


def answer(**change):
    body = {"fresh": True, "origin": "measured", "source": "prometheus", "age_s": 0.5,
            "samples": [{"node_id": n, "metrics": dict(GOOD)} for n in NODES]}
    body.update(change)
    return body


class FakeMonitoring:
    def __init__(self):
        self.reply = answer()
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        if isinstance(self.reply, httpx.Response):
            return self.reply
        return httpx.Response(200, json=self.reply)


@pytest.fixture
def fake():
    return FakeMonitoring()


@pytest.fixture
async def twin(fake):
    adapter = MonitoringAdapter(transport=httpx.MockTransport(fake))
    state = TwinState()
    sync = TwinSynchronizer(adapter, state, EventBus())
    await adapter.connect()
    state.load_topology(await adapter.fetch_topology())
    yield sync
    await adapter.close()


def statuses(sync):
    return {s.status for s in sync.state.states.values()}


async def test_twin_receives_all_metrics_with_one_read(fake, twin):
    await twin.tick()
    assert statuses(twin) == {HealthStatus.HEALTHY}
    assert twin.state.states["bank-account"].metrics == GOOD
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/api/v1/twin/samples")]
    assert twin.adapter.origin == "measured"


async def test_down_and_absent_services(fake, twin):
    fake.reply = answer(samples=[
        {"node_id": "bank-account", "metrics": {"availability": 0.0}},
        {"node_id": "bank-payment", "metrics": GOOD},
        {"node_id": "bank-unheard-of", "metrics": GOOD}])
    twin.stale_after = timedelta(0)
    await twin.tick()
    st = twin.state.states
    assert st["bank-account"].status in (HealthStatus.DOWN, HealthStatus.UNKNOWN)
    assert st["bank-fraud"].status == HealthStatus.UNKNOWN
    assert "bank-unheard-of" not in st


@pytest.mark.parametrize("reply", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(502),
    httpx.Response(200, text="<html>"), httpx.Response(200, json=[]),
    httpx.Response(200, content=b"{" + b" " * 300_000 + b"}"),
    httpx.Response(302, headers={"location": "http://evil.example"}),
    answer(fresh=False), answer(age_s=120), answer(age_s=None), answer(origin="invented")])
async def test_unusable_answers_never_update_the_twin(fake, twin, reply):
    await twin.tick()
    before = dict(twin.state.states)
    fake.reply = reply
    twin.stale_after = timedelta(hours=1)
    await twin.tick()
    assert twin.state.states == before
    twin.stale_after = timedelta(0)
    await twin.tick()
    assert statuses(twin) == {HealthStatus.UNKNOWN}


@pytest.mark.parametrize("bad", [{"cpu_pct": "x"}, {"cpu_pct": True}, {"error_rate": 2},
                                 {"rps": -1}, {"latency_ms": float("nan")}])
async def test_one_bad_node_does_not_spoil_the_others(fake, twin, bad):
    body = answer()
    body["samples"][0]["metrics"].update(bad)
    fake.reply = httpx.Response(200, content=json.dumps(body).encode())
    await twin.tick()
    states = twin.state.states
    assert states[NODES[0]].status == HealthStatus.UNKNOWN
    assert all(states[n].status == HealthStatus.HEALTHY for n in NODES[1:])


async def test_synthetic_data_is_recognisable(fake, twin):
    fake.reply = answer(origin="synthetic")
    await twin.tick()
    assert twin.adapter.origin == "synthetic"


@pytest.mark.parametrize("url", ["https://api.realbank.com", "http://10.0.0.1:8200",
                                 "http://monitoring.evil.com"])
def test_only_the_monitoring_service_is_read(url):
    with pytest.raises(ValueError):
        MonitoringAdapter(url)


def test_backend_runs_without_the_monitoring_service():
    settings = Settings(telemetry_adapter="monitoring", telemetry_interval_s=0.05,
                        monitoring_url="http://127.0.0.1:9")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/ready").json()["adapter"] == "monitoring"
        with client.websocket_connect("/ws/twin") as ws:
            snap = ws.receive_json()
        assert snap["source"] == "monitoring" and snap["origin"] is None
        assert {s["status"] for s in snap["states"]} == {"unknown"}
