"""Health checks, dependency rules, fault injection and container configuration."""

import ast
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from bankcommon.client import CircuitBreaker
from bankcommon.config import ROOT, check_simulated_url, registry
from bankcommon.errors import UndeclaredDependency
from tests.conftest import Cluster

SERVICES = list(registry().services)
EDGES = {(d.source, d.target) for d in registry().dependencies}


# --------------------------------------------------------------- dependency map
def test_map_contains_the_required_services_and_dependencies():
    assert len(SERVICES) == 8
    assert {("transaction", "account"), ("account", "database"), ("transaction", "fraud"),
            ("transaction", "payment"), ("payment", "notification")} <= EDGES


def test_map_is_acyclic_and_ports_are_unique():
    def visit(node, trail):
        assert node not in trail, f"cycle: {[*trail, node]}"
        for s, t in EDGES:
            if s == node:
                visit(t, [*trail, node])
    for s in SERVICES:
        visit(s, [])
    ports = [v["port"] for v in registry().services.values()]
    assert len(set(ports)) == 8


def test_callers_wait_longer_than_the_call_they_wrap():
    """Otherwise the caller gives up first and the root cause is lost."""
    for outer in registry().dependencies:
        for inner in registry().of(outer.target).values():
            if inner.mode == "required" and inner.target == "database":
                assert outer.timeout_ms > inner.timeout_ms, (outer, inner)


def test_services_never_import_each_other():
    for name in SERVICES:
        for f in (ROOT / "services" / name).glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                        else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for m in mods:
                    if m.startswith("services."):
                        assert m.split(".")[1] == name, f"{f} imports {m}"


async def test_undeclared_call_is_refused(bank):
    with pytest.raises(UndeclaredDependency):
        await bank.ctx("notification").client.call("database", "GET", "/internal/integrity")


@pytest.mark.parametrize("url", ["https://api.realbank.com", "http://10.0.0.5:8101",
                                 "http://bank-account.evil.com", "http://bank-account@evil.com"])
def test_only_simulated_hosts_are_reachable(url):
    with pytest.raises(ValueError):
        check_simulated_url(url)


# ----------------------------------------------------------------------- health
@pytest.mark.parametrize("name", SERVICES)
async def test_every_service_exposes_health_and_telemetry(bank, name):
    live = await bank.http(name, "GET", "/health/live")
    ready = await bank.http(name, "GET", "/health/ready")
    assert live.json()["service"] == name
    assert (ready.status_code, ready.json()["status"]) == (200, "ready")
    assert set(ready.json()["dependencies"]) == {t for s, t in EDGES if s == name}
    assert (await bank.http(name, "GET", "/health")).status_code == 200
    metrics = (await bank.http(name, "GET", "/telemetry")).json()["metrics"]
    assert set(metrics) == {"cpu_pct", "mem_pct", "latency_ms", "error_rate", "rps",
                            "availability"}
    assert "bank_requests" in (await bank.http(name, "GET", "/metrics")).text


@pytest.mark.parametrize("name", SERVICES)
async def test_control_endpoints_need_the_internal_key(bank, name):
    for method, path in (("POST", "/control/faults"), ("DELETE", "/control/faults"),
                         ("POST", "/control/shutdown")):
        r = await bank.http(name, method, path, json={"type": "downtime", "duration_s": 5})
        assert r.status_code == 401, (method, path)
    assert (await bank.http(name, "GET", "/health/live")).status_code == 200


async def test_readiness_reflects_dependency_importance(bank):
    async def status(name):
        return (await bank.http(name, "GET", "/health/ready")).json()["status"]
    bank.down.add("notification")
    assert await status("payment") == "degraded"
    bank.down = {"fraud"}
    assert await status("transaction") == "degraded"
    bank.down = {"account"}
    assert await status("transaction") == "not_ready"
    assert await status("customer") == "ready"
    bank.down = {"database"}
    assert (await bank.http("account", "GET", "/health/live")).status_code == 200
    assert await status("account") == "not_ready"


# --------------------------------------------------------- failure propagation
async def test_failure_path_names_the_root_cause(bank):
    t = await bank.token()
    bank.down.add("database")
    r = await bank.http("account", "GET", "/accounts", token=t)
    assert r.status_code == 503
    assert r.json()["error"]["failure_path"] == ["account", "database"]
    bank.down.clear()
    await bank.fault("database", "db_connection_failure", mode="refuse")
    r = await bank.http("account", "GET", "/accounts", token=t)
    assert r.json()["error"]["failure_path"] == ["account", "database"]


async def test_fraud_outage_degrades_small_and_blocks_large(bank):
    t = await bank.token()
    bank.down.add("fraud")
    small, large = await bank.transfer(t, 1000), await bank.transfer(t, 60_000)
    assert small.status_code == 201
    assert small.json()["detail"]["fraud"] == "skipped_unavailable"
    assert large.status_code == 503
    assert large.json()["error"]["failure_path"] == ["transaction", "fraud"]
    await bank.audit()


async def test_notification_outage_never_fails_a_payment(bank):
    bank.down.add("notification")
    r = await bank.pay(await bank.token())
    assert r.status_code == 201 and r.json()["detail"]["receipt"] == "unavailable"


async def test_payment_outage_stops_payments_but_not_transfers(bank):
    t, before = await bank.token(), (await bank.account())["balance"]
    bank.down.add("payment")
    r = await bank.pay(t)
    assert r.status_code == 503 and r.json()["error"]["failure_path"] == ["transaction", "payment"]
    acct = await bank.account()
    assert (acct["balance"], acct["held"]) == (before, 0)
    assert (await bank.transfer(t)).status_code == 201
    await bank.audit()


async def test_token_checks_survive_a_database_outage(bank):
    t = await bank.token()
    bank.down.add("database")
    r = await bank.http("authentication", "POST", "/internal/verify", internal=True,
                        json={"token": t})
    assert r.status_code == 200


# --------------------------------------------------------------------- faults
async def test_downtime_fault_hides_health_and_recovers(bank):
    await bank.fault("customer", "downtime", duration_s=0.4)
    assert (await bank.http("customer", "GET", "/health/live")).status_code == 503
    tele = (await bank.http("customer", "GET", "/telemetry")).json()
    assert tele["metrics"]["availability"] == 0.0 and tele["faults"][0]["type"] == "downtime"
    await asyncio.sleep(0.6)  # expires by itself
    assert (await bank.http("customer", "GET", "/health/live")).status_code == 200


async def test_latency_fault_slows_but_succeeds_then_times_out(bank):
    t = await bank.token()
    await bank.fault("database", "latency", latency_ms=300, jitter=0)
    start = time.perf_counter()
    r = await bank.http("account", "GET", "/accounts", token=t)
    assert r.status_code == 200 and time.perf_counter() - start >= 0.3
    await bank.fault("database", "latency", latency_ms=1500, jitter=0)
    r = await bank.http("account", "GET", "/accounts", token=t)
    assert r.status_code == 503 and "timeout" in r.json()["error"]["message"]


async def test_slot_exhaustion_sheds_load_and_recovers(bank):
    t = await bank.token()
    await bank.fault("account", "resource_exhaustion", mode="slots")
    await asyncio.sleep(0.05)
    r = await bank.http("account", "GET", "/accounts", token=t)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "OVERLOADED")
    await bank.clear("account")
    await asyncio.sleep(0.05)
    assert (await bank.http("account", "GET", "/accounts", token=t)).status_code == 200


async def test_memory_exhaustion_sheds_load(settings):
    async with Cluster(replace(settings, customers=2)) as bank:
        ctx = bank.ctx("customer")
        ctx.settings = replace(settings, mem_limit_mb=int(ctx.telemetry.memory_mb()) + 60)
        t = await bank.token()
        await bank.fault("customer", "resource_exhaustion", mode="memory")
        await asyncio.sleep(0.3)
        r = await bank.http("customer", "GET", "/customers/me", token=t)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "RESOURCE_EXHAUSTED")


async def test_pool_exhaustion_is_real_and_recovers(bank):
    t = await bank.token()
    await bank.fault("database", "db_connection_failure", mode="exhaust")
    await asyncio.sleep(0.05)
    assert (await bank.http("database", "GET", "/health/ready")).status_code == 503
    r = await bank.http("database", "GET", "/internal/customers/CUST00001", internal=True)
    assert r.json()["error"]["code"] == "DB_POOL_EXHAUSTED"
    await bank.clear("database")
    await asyncio.sleep(0.05)
    assert bank.ctx("database").pool.idle.qsize() == bank.settings.db_pool_size
    assert (await bank.http("account", "GET", "/accounts", token=t)).status_code == 200


async def test_fault_requests_are_bounded_and_validated(bank):
    for service, body in [
        ("account", {"type": "db_connection_failure", "mode": "refuse", "duration_s": 5}),
        ("account", {"type": "resource_exhaustion", "mode": "disk", "duration_s": 5}),
        ("account", {"type": "latency", "latency_ms": 99_999, "duration_s": 5}),
        ("account", {"type": "downtime", "duration_s": 99_999}),
        ("account", {"type": "meltdown", "duration_s": 5}),
    ]:
        r = await bank.http(service, "POST", "/control/faults", internal=True, json=body)
        assert r.status_code in (400, 422), body


async def test_faults_can_be_disabled(settings):
    async with Cluster(replace(settings, faults_enabled=False)) as bank:
        assert (await bank.fault("account", "downtime")).status_code == 403


def test_circuit_breaker_opens_probes_once_and_closes():
    now = [0.0]
    b = CircuitBreaker(threshold=2, cooldown_s=3, clock=lambda: now[0])
    for _ in range(2):
        assert b.allow()
        b.failure(["x"])
    assert b.state == "open" and not b.allow()
    now[0] = 3.1
    assert b.allow() and not b.allow()  # a single probe
    b.failure(["x"])
    assert b.state == "open"
    now[0] = 6.3
    assert b.allow()
    b.abandon()
    assert b.allow()  # a cancelled probe does not jam the breaker
    b.success()
    assert b.state == "closed"


# ----------------------------------------------------------------- containers
def test_compose_matches_the_dependency_map():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(c["services"]) == set(SERVICES)
    for name, spec in c["services"].items():
        port = registry().services[name]["port"]
        assert spec["build"]["args"]["SERVICE"] == name
        assert spec["container_name"] == f"bank-{name}"
        assert spec["ports"] == [f"127.0.0.1:{port}:{port}"]  # never exposed to the network
        assert "depends_on" not in spec  # independently startable
        env = spec["environment"]
        for other in SERVICES:
            assert check_simulated_url(env[f"BANK_URL_{other.upper()}"])
            assert str(registry().services[other]["port"]) in env[f"BANK_URL_{other.upper()}"]
        assert "changeme" not in json.dumps(env).lower()


def test_every_dockerfile_source_exists():
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY"):
            for name in SERVICES:
                src = line.split()[1].replace("${SERVICE}", name)
                assert (Path(ROOT) / src).exists(), src
