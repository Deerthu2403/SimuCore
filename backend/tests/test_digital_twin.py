"""The core digital twin: model, health, state, synchronisation, isolation, API."""

import ast
import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.digital_twin as package
from app.core.config import DEFAULT_MAP, Settings
from app.digital_twin import blueprint as bp
from app.digital_twin import schemas
from app.digital_twin.feed import BadMessage, MonitoringFeed, NotFresh, parse
from app.digital_twin.health import Persistence, StatusTracker
from app.digital_twin.hub import TwinHub
from app.digital_twin.model import (
    ComponentType,
    InfraObs,
    Level,
    LinkKind,
    Observation,
    ServiceObs,
    Status,
)
from app.digital_twin.state import DigitalTwin, Rejected
from app.digital_twin.sync import TwinSync
from app.main import create_app

MAP = bp.load_map(DEFAULT_MAP)
NAMES = list(MAP["services"])
EDGES = [(d["source"], d["target"]) for d in MAP["dependencies"]]
T0 = datetime(2026, 5, 1, 12, tzinfo=UTC)
S, L = Status, Level


# ------------------------------------------------------------------------ helpers
class World:
    """Stands in for monitoring: describes a bank and produces observations of it."""

    def __init__(self):
        self.sequence, self.now, self.run, self.origin = 0, T0, "run-1", "measured"
        self.services = {n: {"status": "up", "deployment": "process", "metrics": {
            "cpu_pct": 10.0, "mem_pct": 20.0, "mem_bytes": 5e7, "rps": 10.0, "error_rate": 0.0,
            "latency_ms": MAP["services"][n]["baseline_latency_ms"] * 0.5, "in_flight": 1.0,
            "queue_length": 0.0, "net_rx_bps": 1000.0, "net_tx_bps": 1000.0,
            "active_connections": 4.0, "availability": 1.0, "db_response_ms": 2.0}}
            for n in NAMES}
        self.services["database"]["metrics"].update(
            db_pool_in_use=1.0, db_pool_size=8.0, db_failure_rate=0.0)
        self.links = {e: {"calls_per_s": 5.0, "failure_rate": 0.0, "latency_ms": 4.0,
                          "circuit": 0.0} for e in EDGES}
        self.infra = {"docker-host": ("up", {"cpu_pct": 5.0, "mem_pct": 30.0}),
                      "platform-postgres": ("up", {"connections": 8.0}),
                      "prometheus": ("up", {"targets_up": 13.0})}
        self.infra_age = timedelta(seconds=2)
        self.skip: set[str] = set()

    def m(self, name):
        return self.services[name]["metrics"]

    def down(self, name, reason="not reachable (connection refused)"):
        self.services[name]["status"] = "down"
        for s, t in EDGES:
            if t == name:
                self.links[(s, t)].update(failure_rate=1.0, latency_ms=None, circuit=2.0)

    def up(self, name):
        self.services[name]["status"] = "up"
        for s, t in EDGES:
            if t == name:
                self.links[(s, t)].update(failure_rate=0.0, latency_ms=4.0, circuit=0.0)

    def observe(self) -> Observation:
        self.sequence += 1
        self.now += timedelta(seconds=2)
        services = {}
        for n, s in self.services.items():
            if n in self.skip:
                continue
            down = s["status"] == "down"
            services[n] = ServiceObs(
                s["status"], "not reachable (connection refused)" if down else "",
                {"availability": 0.0} if down else dict(s["metrics"]),
                None if down else s["deployment"])
        links = {e: dict(v) for e, v in self.links.items()
                 if e[0] in services and services[e[0]].status == "up"}
        infra = {n: InfraObs(st, "", self.now - self.infra_age, dict(m))
                 for n, (st, m) in self.infra.items()}
        return Observation(self.run, self.sequence, self.now, self.origin, services, links, infra)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def twin():
    return DigitalTwin(MAP)


def run(twin, world, n=1):
    for _ in range(n):
        change = twin.apply(world.observe(), world.now)
    return change


def status(twin, cid):
    return twin.components[cid].status


# ---------------------------------------------------------------------- structure
def test_every_kind_of_infrastructure_is_represented(twin, world):
    run(twin, world)
    types = {s.type for s in twin.blueprint.components.values()}
    assert types == set(ComponentType)
    by = schemas.summary(twin).by_type
    assert by[ComponentType.MICROSERVICE] == 8 and by[ComponentType.DATABASE] == 2
    assert by[ComponentType.SERVER] == 2 and by[ComponentType.NETWORK] == 1
    calls = [k for k in twin.blueprint.links.values() if k.kind is LinkKind.CALLS]
    assert {(k.source, k.target) for k in calls} == {(f"bank-{s}", f"bank-{t}") for s, t in EDGES}


def test_every_component_has_what_the_brief_requires(twin, world):
    run(twin, world)
    view = schemas.twin_view(twin, T0)
    ids = [c.id for c in view.components] + [k.id for k in view.links]
    assert len(ids) == len(set(ids))
    for c in view.components:
        assert c.id and c.type and c.name and c.status in Status
        assert isinstance(c.metrics, dict) and c.dependencies is not None
        assert c.status is not S.UNKNOWN, (c.id, c.reason)
        assert c.metrics or c.id == bp.MONITOR
    known = {c.id for c in view.components}
    for k in view.links:
        assert {k.source, k.target} <= known
        source = next(c for c in view.components if c.id == k.source)
        target = next(c for c in view.components if c.id == k.target)
        assert k.id in {d.link_id for d in source.dependencies.depends_on}
        assert k.id in {d.link_id for d in target.dependencies.dependents}


def test_structure_follows_where_services_run(twin, world):
    run(twin, world)
    assert bp.BANK_HOST in twin.components
    v, checksum = twin.structure_version, twin.blueprint.checksum
    for s in world.services.values():
        s["deployment"] = "container"
    change = run(twin, world)
    assert change.structure_changed and twin.structure_version == v + 1
    assert bp.BANK_HOST not in twin.components and twin.blueprint.checksum != checksum
    hosts = {k.target for k in twin.blueprint.links.values()
             if k.kind is LinkKind.HOSTED_ON and k.source.startswith("bank-")}
    assert hosts == {bp.DOCKER_HOST}
    assert status(twin, "bank-account") is S.HEALTHY  # state survives restructuring


def test_a_silent_service_keeps_its_place(twin, world):
    run(twin, world)
    v = twin.structure_version
    world.down("account")
    assert not run(twin, world).structure_changed and twin.structure_version == v


def test_cycles_are_refused():
    broken = json.loads(json.dumps(MAP))
    broken["dependencies"].append({**broken["dependencies"][0], "source": "database",
                                   "target": "authentication"})
    with pytest.raises(ValueError, match="cycle"):
        bp.build(broken, {})


# ------------------------------------------------------------------ state machine
def levels(tracker, *seq):
    return [tracker.observe(x) for x in seq]


def test_tracker_adopts_the_first_observation_at_once():
    for level, expected in ((L.OK, S.HEALTHY), (L.WARNING, S.WARNING), (L.OVERLOAD, S.OVERLOADED),
                            (L.FAILURE, S.FAILED)):
        assert StatusTracker().observe(level) is expected


def test_brief_spikes_change_nothing():
    t = StatusTracker()
    seq = levels(t, L.OK, L.OVERLOAD, L.OK, L.WARNING, L.OK, L.FAILURE, L.OK)
    assert set(seq) == {S.HEALTHY}


def test_persistent_changes_go_only_as_far_as_all_observations_support():
    t = StatusTracker()
    assert levels(t, L.OK, L.OVERLOAD, L.WARNING)[-1] is S.WARNING
    assert levels(t, L.OVERLOAD, L.OVERLOAD)[-1] is S.OVERLOADED
    assert levels(t, L.OK, L.WARNING, L.OK)[-1] is S.WARNING  # back down, cautiously
    assert levels(t, L.OK, L.OK, L.OK)[-1] is S.HEALTHY


def test_certain_failure_is_immediate_uncertain_needs_two():
    t = StatusTracker()
    t.observe(L.OK)
    assert t.observe(L.FAILURE) is S.HEALTHY
    assert t.observe(L.FAILURE) is S.FAILED
    t2 = StatusTracker()
    t2.observe(L.OK)
    assert t2.observe(L.FAILURE, immediate=True) is S.FAILED


def test_recovery_takes_time_and_relapse_is_immediate():
    t = StatusTracker(Persistence(recovery=5))
    t.observe(L.FAILURE)
    assert levels(t, L.OK, L.OK, L.OK) == [S.RECOVERING] * 3
    assert t.observe(L.FAILURE) is S.FAILED
    assert levels(t, L.OK, L.OK, L.OK, L.OK) == [S.RECOVERING] * 4
    assert t.observe(L.OK) is S.HEALTHY


def test_recovery_can_end_in_warning():
    t = StatusTracker()
    t.observe(L.FAILURE)
    assert levels(t, *[L.WARNING] * 5)[-1] is S.WARNING


def test_losing_contact_is_not_a_cure():
    t = StatusTracker()
    t.observe(L.FAILURE)
    assert t.observe(None) is S.UNKNOWN
    assert t.observe(None) is S.UNKNOWN
    assert t.observe(L.OK) is S.RECOVERING
    t2 = StatusTracker()
    t2.observe(L.OK)
    t2.observe(None)
    assert t2.observe(L.OK) is S.HEALTHY
    t3 = StatusTracker()
    levels(t3, L.FAILURE, L.OK, None)  # lost while recovering
    assert t3.observe(L.OK) is S.RECOVERING


def test_tracker_never_leaves_the_allowed_transitions():
    allowed = {
        S.UNKNOWN: set(S), S.HEALTHY: {S.WARNING, S.OVERLOADED, S.FAILED, S.UNKNOWN},
        S.WARNING: {S.HEALTHY, S.OVERLOADED, S.FAILED, S.UNKNOWN},
        S.OVERLOADED: {S.HEALTHY, S.WARNING, S.FAILED, S.UNKNOWN},
        S.FAILED: {S.RECOVERING, S.UNKNOWN},
        S.RECOVERING: {S.HEALTHY, S.WARNING, S.OVERLOADED, S.FAILED, S.UNKNOWN}}
    rng = random.Random(1)
    for _ in range(300):
        t, previous = StatusTracker(), S.UNKNOWN
        for _ in range(80):
            now = t.observe(rng.choice([None, *L, *L]), rng.random() < 0.3)
            assert now is previous or now in allowed[previous], (previous, now)
            previous = now


# ------------------------------------------------------------- the five conditions
def test_healthy(twin, world):
    run(twin, world, 3)
    assert {s.status for s in twin.components.values()} == {S.HEALTHY}
    assert {s.status for s in twin.links.values()} == {S.HEALTHY}
    assert all(s.reason == "" for s in twin.components.values())


@pytest.mark.parametrize(("change", "rule"), [
    ({"cpu_pct": 80.0}, "cpu_pct_high"), ({"mem_pct": 85.0}, "mem_pct_high"),
    ({"error_rate": 0.1}, "errors"), ({"latency_ms": 1000.0}, "slow"),
    ({"queue_length": 2.0}, "queue")])
def test_warning(twin, world, change, rule):
    run(twin, world)
    world.m("account").update(change)
    run(twin, world, 2)
    c = schemas.component_view(twin, "bank-account")
    assert c.status is S.WARNING and rule in {f.rule for f in c.findings} and c.reason


@pytest.mark.parametrize("change", [
    {"cpu_pct": 95.0}, {"mem_pct": 97.0}, {"queue_length": 40.0}, {"in_flight": 63.0},
    {"latency_ms": 1000.0, "cpu_pct": 65.0}])
def test_overloaded(twin, world, change):
    run(twin, world)
    world.m("account").update(change)
    run(twin, world, 2)
    assert status(twin, "bank-account") is S.OVERLOADED


def test_slow_because_of_others_is_not_overloaded(twin, world):
    run(twin, world)
    world.m("account")["latency_ms"] = 5000.0  # huge, but no strain of its own
    run(twin, world, 4)
    assert status(twin, "bank-account") is S.WARNING


def test_few_requests_are_judged_by_their_mean(twin, world):
    run(twin, world)
    world.m("account").update(rps=1.0, latency_ms=300.0, latency_mean_ms=20.0)
    run(twin, world, 4)
    assert status(twin, "bank-account") is S.HEALTHY  # one slow request out of ten
    world.m("account").update(latency_mean_ms=200.0)
    run(twin, world, 2)
    assert status(twin, "bank-account") is S.WARNING  # they are slow on average
    world.m("account").update(rps=10.0, latency_mean_ms=20.0)
    run(twin, world, 4)
    assert status(twin, "bank-account") is S.WARNING  # many requests: the percentile counts


def test_failed_when_unreachable_at_once(twin, world):
    run(twin, world)
    world.down("payment")
    change = run(twin, world)
    assert status(twin, "bank-payment") is S.FAILED
    assert [t.object_id for t in change.transitions if t.object_kind == "component"][0] \
        == "bank-payment"


def test_failed_when_most_requests_fail(twin, world):
    run(twin, world)
    world.m("account")["error_rate"] = 0.8
    run(twin, world, 2)
    assert status(twin, "bank-account") is S.FAILED


def test_error_rate_without_traffic_means_nothing(twin, world):
    run(twin, world)
    world.m("account").update(error_rate=1.0, rps=0.1, latency_ms=9999.0)
    run(twin, world, 5)
    assert status(twin, "bank-account") is S.HEALTHY


def test_recovering_then_healthy(twin, world):
    run(twin, world)
    world.down("payment")
    run(twin, world, 3)
    world.up("payment")
    seen = []
    for _ in range(6):
        run(twin, world)
        seen.append(status(twin, "bank-payment"))
    assert seen == [S.RECOVERING] * 4 + [S.HEALTHY] * 2
    assert twin.components["bank-payment"].previous is S.RECOVERING


def test_hung_service_is_unknown_not_failed(twin, world):
    run(twin, world)
    world.services["fraud"]["status"] = "unresponsive"
    run(twin, world)
    c = schemas.component_view(twin, "bank-fraud")
    assert c.status is S.UNKNOWN and c.metrics == {} and c.data_quality == "none"
    assert twin.links["bank-fraud->bank-database"].status is S.UNKNOWN


def test_service_absent_from_the_message_is_unknown(twin, world):
    run(twin, world)
    world.skip.add("customer")
    run(twin, world)
    assert status(twin, "bank-customer") is S.UNKNOWN


# --------------------------------------------------- databases, servers, network, links
def test_bank_database_is_distinct_from_its_service(twin, world):
    run(twin, world)
    world.m("database").update(db_failure_rate=1.0)
    run(twin, world, 2)
    assert status(twin, bp.BANK_STORE) is S.FAILED
    assert status(twin, "bank-database") is S.HEALTHY
    run(twin, world, 2)  # its failure now shows as a dependency problem of the service
    assert status(twin, "bank-database") is S.WARNING
    assert twin.root_causes("bank-database") == [bp.BANK_STORE]


def test_bank_database_pool_exhaustion_is_overload(twin, world):
    run(twin, world)
    world.m("database").update(db_pool_in_use=8.0)
    run(twin, world, 2)
    assert status(twin, bp.BANK_STORE) is S.OVERLOADED


def test_slow_queries_matter_only_under_load(twin, world):
    run(twin, world)
    world.m("database").update(db_response_ms=80.0, rps=0.0)
    run(twin, world, 4)
    assert status(twin, bp.BANK_STORE) is S.HEALTHY
    world.m("database").update(rps=20.0)
    run(twin, world, 2)
    assert status(twin, bp.BANK_STORE) is S.WARNING


def test_bank_database_is_unknown_while_its_service_is_down(twin, world):
    run(twin, world)
    world.down("database")
    run(twin, world)
    assert status(twin, bp.BANK_STORE) is S.UNKNOWN and status(twin, "bank-database") is S.FAILED


def test_measured_server_and_platform_database(twin, world):
    run(twin, world)
    world.infra["docker-host"] = ("up", {"cpu_pct": 97.0, "mem_pct": 40.0})
    world.infra["platform-postgres"] = ("down", {})
    run(twin, world, 2)
    assert status(twin, bp.DOCKER_HOST) is S.OVERLOADED
    assert status(twin, bp.PLATFORM_STORE) is S.FAILED
    assert schemas.summary(twin).overall is S.HEALTHY  # the platform is not the bank


def test_unmeasured_server_is_known_only_through_what_it_hosts(twin, world):
    run(twin, world)
    host = schemas.component_view(twin, bp.BANK_HOST)
    assert host.status is S.HEALTHY and host.data_quality == "derived"
    assert host.metrics["hosted_running"] == 8 and host.metrics["hosted_cpu_pct"] == 80
    for n in NAMES:
        world.down(n)
    run(twin, world)
    assert status(twin, bp.BANK_HOST) is S.UNKNOWN  # all silent: cannot tell, does not guess


def test_old_infrastructure_data_is_not_trusted(twin, world):
    run(twin, world)
    world.infra_age = timedelta(minutes=1)
    run(twin, world)
    assert status(twin, bp.DOCKER_HOST) is S.UNKNOWN


def test_connection_states(twin, world):
    run(twin, world)
    key, lid = ("transaction", "account"), "bank-transaction->bank-account"
    world.links[key].update(failure_rate=0.1)
    run(twin, world, 2)
    assert twin.links[lid].status is S.WARNING
    world.links[key].update(failure_rate=0.0, latency_ms=1500.0)  # timeout is 2000
    run(twin, world, 2)
    assert twin.links[lid].status is S.WARNING and "1500 ms" in twin.links[lid].reason
    world.links[key].update(latency_ms=4.0, circuit=2.0)
    run(twin, world, 2)
    assert twin.links[lid].status is S.FAILED
    world.links[key].update(circuit=0.0)
    run(twin, world)
    assert twin.links[lid].status is S.RECOVERING


def test_network_is_blamed_only_for_unexplained_failures(twin, world):
    run(twin, world)
    world.down("account")
    run(twin, world, 4)
    assert status(twin, bp.BANK_NET) is S.HEALTHY  # the dead service explains it
    world.up("account")
    run(twin, world, 8)
    for e in EDGES:
        world.links[e].update(failure_rate=1.0)  # everything fails, every service is fine
    run(twin, world, 4)
    assert status(twin, bp.BANK_NET) is S.FAILED
    assert schemas.component_view(twin, bp.BANK_NET).metrics["connections_failed"] == len(EDGES)


# ----------------------------------------------------------------- dependencies
def test_dependency_failure_warns_and_names_the_root_cause(twin, world):
    run(twin, world)
    world.down("database")
    run(twin, world, 3)
    for name in ("account", "customer", "authentication", "payment", "fraud", "transaction"):
        assert status(twin, f"bank-{name}") is S.WARNING, name
        assert twin.root_causes(f"bank-{name}") == ["bank-database"], name
    assert status(twin, "bank-notification") is S.HEALTHY  # depends on it only indirectly


def test_best_effort_dependencies_do_not_warn(twin, world):
    run(twin, world)
    world.down("notification")
    run(twin, world, 4)
    assert status(twin, "bank-payment") is S.HEALTHY


def test_chain_of_failures_has_one_root_cause(twin, world):
    run(twin, world)
    world.down("database")
    for n in ("account", "transaction"):
        world.m(n)["error_rate"] = 1.0
    run(twin, world, 3)
    assert status(twin, "bank-transaction") is S.FAILED
    assert twin.root_causes("bank-transaction") == ["bank-database"]
    s = schemas.summary(twin)
    assert s.overall is S.FAILED and s.needs_attention[0] in ("bank-database", "bank-account",
                                                              "bank-transaction")


def test_two_independent_causes_are_both_named(twin, world):
    run(twin, world)
    world.down("fraud")
    world.down("payment")
    run(twin, world, 3)
    assert twin.root_causes("bank-transaction") == ["bank-fraud", "bank-payment"]


def test_reachability(twin, world):
    run(twin, world)
    down = twin.reachable("bank-database", "downstream")
    assert down["bank-account"] == 1 and down["bank-notification"] == 2
    up = twin.reachable("bank-transaction", "upstream")
    assert up["bank-account"] == 1 and up[bp.BANK_STORE] == 2


# -------------------------------------------------------------- state management
def test_version_grows_and_changes_are_reported_exactly(twin, world):
    run(twin, world)
    v = twin.version
    change = run(twin, world)
    assert not change and twin.version == v == change.version  # nothing changed
    world.m("account")["cpu_pct"] = 11.0
    change = run(twin, world)
    assert twin.version == v + 1
    # the account itself, and the host whose total is derived from it
    assert change.components == {"bank-account", bp.BANK_HOST}


def test_old_and_repeated_observations_are_rejected(twin, world):
    first = world.observe()
    twin.apply(first, T0)
    second = world.observe()
    twin.apply(second, T0)
    before = schemas.twin_view(twin, T0).model_dump()
    for old in (first, second):
        with pytest.raises(Rejected):
            twin.apply(old, T0)
    assert schemas.twin_view(twin, T0).model_dump() == before


def test_a_restarted_monitor_is_accepted(twin, world):
    run(twin, world, 5)
    world.run, world.sequence = "run-2", 0
    run(twin, world)
    assert twin.synchronised


def test_without_data_everything_becomes_unknown_and_failed_things_recover_later(twin, world):
    run(twin, world)
    world.down("payment")
    run(twin, world)
    known = sum(s.status is not S.UNKNOWN for g in (twin.components, twin.links)
                for s in g.values())
    assert not twin.expire(world.now + timedelta(seconds=5))
    change = twin.expire(world.now + timedelta(seconds=9))
    assert {s.status for s in twin.components.values()} == {S.UNKNOWN}
    assert len(change.transitions) == known >= 45
    assert not twin.synchronised and not twin.expire(world.now + timedelta(seconds=20))
    world.up("payment")
    run(twin, world)
    assert status(twin, "bank-payment") is S.RECOVERING
    assert status(twin, "bank-account") is S.HEALTHY and twin.synchronised


def test_measured_and_synthetic_states_never_mix(twin, world):
    run(twin, world)
    world.down("payment")
    run(twin, world, 2)
    world.up("payment")
    world.origin, world.run, world.sequence = "synthetic", "run-9", 0
    run(twin, world)
    assert twin.origin == "synthetic"
    assert status(twin, "bank-payment") is S.HEALTHY  # not "recovering" from a measured failure


# ------------------------------------------------------------------------ separation
def test_the_twin_is_isolated_from_its_input(twin, world):
    obs = world.observe()
    twin.apply(obs, T0)
    before = schemas.twin_view(twin, T0).model_dump()
    for s in obs.services.values():  # somebody changes the data afterwards
        s.metrics.clear()
        s.metrics["cpu_pct"] = 100.0
    for m in obs.links.values():
        m.clear()
    assert schemas.twin_view(twin, T0).model_dump() == before


def test_the_twin_is_isolated_from_its_output(twin, world):
    run(twin, world)
    view = schemas.twin_view(twin, T0)
    before = view.model_dump()
    for c in view.components:
        c.metrics.clear()
        c.attributes.clear()
        c.status = S.FAILED
    assert schemas.twin_view(twin, T0).model_dump() == before


def test_a_clone_is_independent(twin, world):
    run(twin, world)
    clone = twin.clone()
    other = World()
    other.sequence = 50
    other.down("database")
    run(clone, other, 4)
    assert status(clone, "bank-database") is S.FAILED
    assert status(twin, "bank-database") is S.HEALTHY
    assert clone.run_id != twin.run_id and clone.version != twin.version


def test_the_dependency_map_is_copied(world):
    source = json.loads(json.dumps(MAP))
    twin = DigitalTwin(source)
    source["services"].clear()
    run(twin, world)
    assert len(twin.components) == 15


def test_twin_code_cannot_reach_the_real_system():
    """No imports of the bank or the monitor, no HTTP client, no process control, no sending."""
    forbidden = {"httpx", "requests", "subprocess", "socket", "bankcommon", "bankctl", "services",
                 "monitor", "psutil", "app.adapters", "app.simulation"}
    for f in Path(package.__file__).parent.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert not any(n == x or n.startswith(x + ".") for x in forbidden), (f.name, n)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("send", "send_text", "send_json", "post", "put"), \
                    (f.name, node.lineno)
        if f.name in ("model.py", "blueprint.py", "health.py", "state.py"):
            assert "import asyncio" not in f.read_text(encoding="utf-8"), f.name  # pure logic


def test_api_is_read_only():
    app = create_app(Settings(twin_enabled=False))
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/v2"):
            assert route.methods <= {"GET", "HEAD"}, route.path


# ------------------------------------------------------------------------------- feed
def message(world=None, **change) -> dict:
    world = world or World()
    obs = world.observe()
    body = {
        "type": "update", "sequence": obs.sequence, "run_id": obs.run_id, "fresh": True,
        "origin": obs.origin, "collected_at": obs.observed_at.isoformat(),
        "sent_at": obs.observed_at.isoformat(), "overview": {}, "events": [],
        "services": [{"service": n, "status": s.status, "reason": s.reason, "metrics": s.metrics,
                      "invalid": {}, "deployment": s.deployment} for n, s in obs.services.items()],
        "links": [{"source": a, "target": b, "metrics": m} for (a, b), m in obs.links.items()],
        "infrastructure": [{"component": n, "status": i.status, "reason": None,
                            "ts": i.observed_at.isoformat(), "metrics": i.metrics}
                           for n, i in obs.infra.items()]}
    body.update(change)
    return body


def test_feed_parses_a_monitoring_message():
    obs = parse(json.dumps(message()), set(NAMES))
    assert len(obs.services) == 8 and len(obs.links) == 14 and len(obs.infra) == 3
    assert obs.services["account"].deployment == "process"


@pytest.mark.parametrize("bad", [
    "nonsense", "[]", "null", b"\xff\xfe", json.dumps(message(type="other")),
    json.dumps(message(origin="invented")), json.dumps(message(run_id="")),
    json.dumps(message(sequence=-1)), json.dumps(message(sequence=True)),
    json.dumps(message(sequence="5")), json.dumps(message(services=[])),
    json.dumps(message(services="x")), json.dumps(message(collected_at="yesterday")),
    json.dumps(message(collected_at="2026-01-01T00:00:00")),
    json.dumps(message(collected_at=None)), json.dumps(message(links={"a": 1}))])
def test_feed_refuses_unusable_messages(bad):
    with pytest.raises(BadMessage):
        parse(bad, set(NAMES))


def test_feed_knows_when_monitoring_itself_is_blind():
    with pytest.raises(NotFresh):
        parse(json.dumps(message(fresh=False)), set(NAMES))


def test_feed_drops_impossible_values_and_strangers():
    body = message()
    account = next(s for s in body["services"] if s["service"] == "account")
    account["metrics"].update(cpu_pct=float("nan"), mem_pct=-3, rps=True, queue_length="many")
    body["services"].append({"service": "intruder", "status": "up", "metrics": {}})
    body["links"].append({"source": "account", "target": "intruder", "metrics": {}})
    obs = parse(json.dumps(body), set(NAMES))
    m = obs.services["account"].metrics
    assert not {"cpu_pct", "mem_pct", "rps", "queue_length"} & set(m)
    assert obs.services["account"].partial and "intruder" not in obs.services
    assert len(obs.links) == 14


@pytest.mark.parametrize("url", ["ws://evil.example/ws", "http://localhost:8200/ws",
                                 "wss://api.realbank.com/ws", "ws://monitoring.evil.com/ws"])
def test_feed_connects_only_to_the_monitoring_service(url):
    with pytest.raises(ValueError):
        MonitoringFeed(url)


def make_sync():
    return TwinSync(DigitalTwin(MAP), MonitoringFeed("ws://127.0.0.1:9/ws"), TwinHub())


def test_sync_survives_any_message():
    sync, rng = make_sync(), random.Random(3)
    good = json.dumps(message())
    assert sync.handle(good)
    version = sync.twin.version
    for _ in range(400):
        raw = list(good)
        for _ in range(rng.randint(1, 60)):
            raw[rng.randrange(len(raw))] = chr(rng.randint(32, 126))
        sync.handle("".join(raw))
    assert "error" not in sync.rejected, sync.rejected
    assert sync.twin.version >= version and len(sync.twin.components) == 15
    for c in schemas.twin_view(sync.twin, T0).components:
        assert all(math.isfinite(x) for x in c.metrics.values())


def test_sync_counts_what_it_rejects():
    sync, body = make_sync(), json.dumps(message())
    sync.handle(body)
    sync.handle(body)
    sync.handle("x")
    sync.handle(json.dumps(message(fresh=False)))
    assert sync.applied == 1
    assert sync.rejected == {"outdated": 1, "unusable": 1, "not_fresh": 1}


# ------------------------------------------------------------------------------- API
@pytest.fixture
def api():
    settings = Settings(twin_monitoring_ws="ws://127.0.0.1:9/ws", telemetry_interval_s=5)
    app = create_app(settings)
    with TestClient(app) as client:
        client.world = World()
        client.sync = app.state.twin_sync

        def feed(n=1):
            for _ in range(n):
                client.world.now = datetime.now(UTC) - timedelta(seconds=2)
                client.portal.call(client.sync.handle, json.dumps(message(client.world)))
        client.feed = feed
        yield client


def test_api_before_any_data(api):
    body = api.get("/api/v2/twin").json()
    assert body["meta"]["sync"] == "waiting" and body["meta"]["origin"] is None
    assert {c["status"] for c in body["components"]} == {"unknown"}
    assert api.get("/api/v2/twin/sync").json()["state"] == "waiting"


def test_api_complete_twin(api):
    api.feed(2)
    body = api.get("/api/v2/twin").json()
    assert body["meta"]["sync"] == "live" and body["meta"]["api_version"] == "2.0"
    assert len(body["components"]) == 15 and len(body["links"]) == 35
    schemas.TwinView.model_validate(body)
    assert api.get("/api/v2/twin/summary").json() == body["summary"]


def test_api_parts(api):
    api.world.down("payment")
    api.feed(3)
    get = api.get
    failed = get("/api/v2/twin/components?status=failed").json()
    assert [c["id"] for c in failed] == ["bank-payment"]
    assert len(get("/api/v2/twin/components?type=microservice").json()) == 8
    assert get("/api/v2/twin/components/bank-payment").json()["findings"][0]["level"] == "failure"
    dep = get("/api/v2/twin/components/bank-transaction/dependencies").json()
    assert dep["root_causes"] == ["bank-payment"] and dep["upstream"] and dep["downstream"] == []
    assert len(get("/api/v2/twin/links?kind=calls").json()) == 14
    link = get("/api/v2/twin/links/bank-transaction->bank-payment").json()
    assert link["status"] == "failed" and link["mode"] == "required"
    cat = get("/api/v2/twin/catalogue").json()
    assert {s["status"] for s in cat["statuses"]} == {s.value for s in Status}
    units = set(cat["metric_units"])
    body = get("/api/v2/twin").json()
    for obj in body["components"] + body["links"]:
        assert set(obj["metrics"]) <= units, obj["id"]


def test_api_structure_is_cacheable(api):
    api.feed()
    r = api.get("/api/v2/twin/structure")
    again = api.get("/api/v2/twin/structure", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    for s in api.world.services.values():
        s["deployment"] = "container"
    api.feed()
    changed = api.get("/api/v2/twin/structure", headers={"If-None-Match": r.headers["etag"]})
    assert changed.status_code == 200


@pytest.mark.parametrize(("path", "code"), [
    ("/components/nobody", 404), ("/components/Bank_1", 422), ("/components?type=toaster", 422),
    ("/links/a->b", 422), ("/links/bank-account->bank-nobody", 404),
    ("/history/transitions", 503), ("/history/state?at=2026-01-01T00:00:00Z", 503),
    ("/history/state", 422), ("/history/timeline/bank-account", 503)])
def test_api_refuses(api, path, code):
    assert api.get("/api/v2/twin" + path).status_code == code, path


def test_api_disabled_twin():
    with TestClient(create_app(Settings(twin_enabled=False))) as client:
        assert client.get("/api/v2/twin").status_code == 503
        assert client.get("/api/v1/topology").status_code == 200


# -------------------------------------------------------------------------- websocket
class Mirror:
    """A client that rebuilds the twin only from messages, as the 3D frontend will."""

    def __init__(self):
        self.components, self.links, self.version, self.transitions = {}, {}, None, []

    def receive(self, m):
        if m["type"] == "snapshot":
            self.components = {c["id"]: c for c in m["components"]}
            self.links = {k["id"]: k for k in m["links"]}
        else:
            assert m["base_version"] == self.version, "a message was lost"
            self.components.update({c["id"]: c for c in m["components"]})
            self.links.update({k["id"]: k for k in m["links"]})
            self.transitions += m["transitions"]
        self.version = m["meta"]["version"]


def test_client_mirror_equals_the_twin_after_random_changes(api):
    rng = random.Random(11)
    with api.websocket_connect("/ws/v2/twin") as ws:
        mirror = Mirror()
        mirror.receive(ws.receive_json())
        for step in range(60):
            w = api.world
            name = rng.choice(NAMES)
            action = rng.random()
            if action < 0.25:
                w.down(name)
            elif action < 0.5:
                w.up(name)
            elif action < 0.7:
                w.m(name)["cpu_pct"] = rng.choice([10.0, 80.0, 95.0])
            elif action < 0.85:
                w.links[rng.choice(EDGES)]["failure_rate"] = rng.choice([0.0, 0.1, 1.0])
            elif action < 0.93:
                w.services[name]["status"] = rng.choice(["up", "unresponsive"])
            else:
                w.services[name]["deployment"] = rng.choice(["process", "container"])
            api.feed(rng.randint(1, 3))
            target = api.sync.twin.version
            while mirror.version < target:
                mirror.receive(ws.receive_json())
            truth = api.get("/api/v2/twin").json()
            assert mirror.components == {c["id"]: c for c in truth["components"]}, step
            assert mirror.links == {k["id"]: k for k in truth["links"]}, step
        assert mirror.transitions


def test_slow_client_gets_one_correct_message_instead_of_many(api):
    hub: TwinHub = api.sync.hub
    api.feed()
    with hub.join() as client:
        client.take()
        api.world.down("payment")
        api.feed(50)
        full, components, links, transitions = client.take()
        assert not full and "bank-payment" in components
        assert any(t.object_id == "bank-payment" for t in transitions)


def test_websocket_resync_and_subscription(api):
    api.feed(2)
    with api.websocket_connect("/ws/v2/twin") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "subscribe", "components": ["bank-payment"]})
        m = ws.receive_json()
        assert m["type"] == "snapshot" and [c["id"] for c in m["components"]] == ["bank-payment"]
        assert all("bank-payment" in (k["source"], k["target"]) for k in m["links"])
        ws.send_json({"type": "subscribe", "components": None})
        assert len(ws.receive_json()["components"]) == 15
        ws.send_json({"type": "resync"})
        assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize("text", ["nonsense", "[]", '{"type":"set","status":"healthy"}',
                                  '{"type":"subscribe","components":["x"]}',
                                  '{"type":"subscribe"}', "x" * 9000])
def test_websocket_refuses_everything_else(api, text):
    with api.websocket_connect("/ws/v2/twin") as ws:
        ws.receive_json()
        ws.send_text(text)
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(5):
                ws.receive_json()
        assert exc.value.code == 1003


def test_websocket_refuses_other_web_pages(api):
    with pytest.raises(WebSocketDisconnect), api.websocket_connect(
            "/ws/v2/twin", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_json()


def test_websocket_announces_loss_of_synchronisation(api):
    api.feed()
    with api.websocket_connect("/ws/v2/twin") as ws:
        ws.receive_json()
        api.sync.twin.last_applied_at = datetime.now(UTC) - timedelta(minutes=1)
        m = ws.receive_json()
        while m["type"] == "heartbeat":
            m = ws.receive_json()
        assert m["meta"]["sync"] == "stale"
        assert {c["status"] for c in m["components"]} == {"unknown"}


def test_response_format_matches_the_published_schema():
    """The frontend is built against this file: changing the format must be a decision."""
    published = Path(__file__).resolve().parents[2] / "docs/api/twin-v2.schema.json"
    current = {m.__name__: m.model_json_schema() for m in (schemas.TwinView, schemas.TwinMessage)}
    assert json.loads(published.read_text(encoding="utf-8")) == current, \
        "run: python scripts/export_twin_schema.py"


def test_applying_is_fast_enough(twin, world):
    run(twin, world)
    observations = [world.observe() for _ in range(50)]
    start = time.perf_counter()
    for o in observations:
        twin.apply(o, o.observed_at)
    assert (time.perf_counter() - start) / 50 < 0.05


def test_removed_components_disappear_from_clients(api):
    api.feed()
    with api.websocket_connect("/ws/v2/twin") as ws:
        mirror = Mirror()
        mirror.receive(ws.receive_json())
        assert bp.BANK_HOST in mirror.components
        for s in api.world.services.values():
            s["deployment"] = "container"
        api.feed()
        m = ws.receive_json()
        assert m["type"] == "snapshot"
        mirror.receive(m)
        truth = api.get("/api/v2/twin").json()
        assert set(mirror.components) == {c["id"] for c in truth["components"]}
        assert set(mirror.links) == {k["id"] for k in truth["links"]}
        assert bp.BANK_HOST not in mirror.components
