import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import BankServicesAdapter
from app.core.config import Settings
from app.core.events import EventBus
from app.domain.models import HealthStatus
from app.main import create_app
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer

GOOD = {"cpu_pct": 10.0, "mem_pct": 20.0, "latency_ms": 5.0, "error_rate": 0.0, "rps": 3.0,
        "availability": 1.0}
PORTS = {8101: "authentication", 8102: "customer", 8103: "account", 8104: "transaction",
         8105: "payment", 8106: "fraud", 8107: "notification", 8108: "database"}


class FakeBank:
    """Answers like the eight services; individual services can be overridden."""

    def __init__(self):
        self.override = {}
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        service = PORTS[request.url.port]
        if service in self.override:
            result = self.override[service]
            if isinstance(result, Exception):
                raise result
            return result
        return httpx.Response(200, json={"service": service, "metrics": GOOD})


@pytest.fixture
def fake():
    return FakeBank()


@pytest.fixture
async def twin(fake):
    adapter = BankServicesAdapter(transport=httpx.MockTransport(fake))
    state = TwinState()
    sync = TwinSynchronizer(adapter, state, EventBus())
    await adapter.connect()
    state.load_topology(await adapter.fetch_topology())
    yield sync
    await adapter.close()


def status(sync, service):
    return sync.state.states[f"bank-{service}"].status


async def test_healthy_bank_is_healthy_and_only_read(fake, twin):
    await twin.tick()
    assert {s.status for s in twin.state.states.values()} == {HealthStatus.HEALTHY}
    assert len(fake.requests) == 8
    for r in fake.requests:
        assert (r.method, r.url.path) == ("GET", "/telemetry")
        assert "x-internal-key" not in r.headers and "authorization" not in r.headers


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("closed")])
async def test_refused_connection_means_down_and_others_unaffected(fake, twin, error):
    fake.override["account"] = error
    await twin.tick()
    assert status(twin, "account") == HealthStatus.DOWN
    assert sum(s.status == HealthStatus.HEALTHY for s in twin.state.states.values()) == 7
    del fake.override["account"]
    await twin.tick()
    assert status(twin, "account") == HealthStatus.HEALTHY


async def test_soft_downtime_means_down(fake, twin):
    fake.override["fraud"] = httpx.Response(
        200, json={"service": "fraud", "metrics": {**GOOD, "availability": 0.0}})
    await twin.tick()
    assert status(twin, "fraud") == HealthStatus.DOWN


BAD_BODIES = [
    b"not json", b"[]", b"{}", b"x" * 70_000,
    json.dumps({"service": "payment", "metrics": GOOD}).encode(),  # impostor
    json.dumps({"service": "account", "metrics": {}}).encode(),
    json.dumps({"service": "account", "metrics": {**GOOD, "cpu_pct": "high"}}).encode(),
    json.dumps({"service": "account", "metrics": {**GOOD, "cpu_pct": True}}).encode(),
    json.dumps({"service": "account", "metrics": {**GOOD, "error_rate": 1.5}}).encode(),
    json.dumps({"service": "account", "metrics": {**GOOD, "rps": -1}}).encode(),
    json.dumps({"service": "account", "metrics": {**GOOD, "latency_ms": float("nan")}}).encode(),
]


@pytest.mark.parametrize("body", BAD_BODIES)
async def test_unusable_telemetry_becomes_unknown_never_a_guess(fake, twin, body):
    await twin.tick()
    fake.override["account"] = httpx.Response(200, content=body)
    twin.stale_after = timedelta(0)
    await twin.tick()
    assert status(twin, "account") == HealthStatus.UNKNOWN


@pytest.mark.parametrize("answer", [
    httpx.ReadTimeout("slow"), httpx.Response(500), httpx.Response(302, headers={
        "location": "http://elsewhere.example/telemetry"})])
async def test_slow_or_broken_service_becomes_unknown(fake, twin, answer):
    await twin.tick()
    fake.override["database"] = answer
    twin.stale_after = timedelta(0)
    await twin.tick()
    assert status(twin, "database") == HealthStatus.UNKNOWN
    assert all(r.url.host == "127.0.0.1" for r in fake.requests)  # redirect not followed


async def test_whole_bank_down_is_a_valid_state(fake, twin):
    fake.override = {s: httpx.ConnectError("refused") for s in PORTS.values()}
    await twin.tick()
    assert {s.status for s in twin.state.states.values()} == {HealthStatus.DOWN}


def test_latency_is_judged_against_each_services_measured_baseline():
    state = TwinState()
    state.load_topology(BankServicesAdapter()._topology)
    for n in state.topology.nodes:
        assert n.attributes["base_latency_ms"] >= 25
    base = {n.attributes["service"]: n.attributes["base_latency_ms"] for n in state.topology.nodes}
    assert base["transaction"] > base["account"]  # it orchestrates ten calls


@pytest.mark.parametrize("template", ["https://api.realbank.com/{port}", "http://10.1.2.3:{port}",
                                      "http://bank-{service}.evil.com:{port}"])
def test_only_the_simulated_bank_is_reachable(template):
    with pytest.raises(ValueError):
        BankServicesAdapter(template)


def test_container_hostnames_are_allowed():
    BankServicesAdapter("http://bank-{service}:{port}")


def test_api_reports_its_source_and_survives_a_stopped_bank():
    # Port 9 is the discard port: nothing listens there.
    settings = Settings(telemetry_adapter="bank_services", telemetry_interval_s=0.05,
                        bank_url_template="http://127.0.0.1:9")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/ready").json()["adapter"] == "bank_services"
        with client.websocket_connect("/ws/twin") as ws:
            snap = ws.receive_json()
        assert snap["source"] == "bank_services"
        assert len(snap["topology"]["nodes"]) == 8
        assert {s["status"] for s in snap["states"]} == {"down"}


def test_unknown_adapter_is_refused():
    with pytest.raises(ValueError), TestClient(create_app(Settings(telemetry_adapter="nope"))):
        pass


async def test_down_service_stops_exporting_old_metrics(fake, twin):
    from prometheus_client import generate_latest
    cpu = b'simucore_node_metric{metric="cpu_pct",node="bank-account"}'
    await twin.tick()
    assert cpu in generate_latest()
    fake.override["account"] = httpx.ConnectError("refused")
    await twin.tick()
    assert cpu not in generate_latest()
    assert b'simucore_node_status{node="bank-account"} 3.0' in generate_latest()
