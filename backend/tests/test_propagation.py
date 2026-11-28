"""Failure propagation: the engine on many graphs, and in the running twin."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.digital_twin import blueprint as bp
from app.digital_twin import schemas
from app.digital_twin.impact import WINDOW
from app.digital_twin.model import Status
from app.digital_twin.propagation import (
    THRESHOLD,
    DependencyGraph,
    Node,
    analyse,
    critical_components,
    level,
    markdown,
    report,
    strength,
)
from app.digital_twin.state import DigitalTwin
from tests.test_digital_twin import MAP, NAMES, World, run
from tests.test_digital_twin import api as api  # noqa: F401  (fixture)


def graph(edges, crit=None, group="bank"):
    """``edges``: (a, b, strength-ish weight) meaning a depends on b, required."""
    ids = sorted({x for e in edges for x in e[:2]} | set(crit or {}))
    nodes = [Node(i, i.upper(), "microservice", (crit or {}).get(i, 3), group) for i in ids]
    return DependencyGraph.from_parts(nodes, [(a, b, "calls", "required", w) for a, b, w in edges])


def impacts(g, *failed):
    a = analyse(g, failed)
    return {i: round(x.impact, 4) for i, x in a.impacts.items() if not x.failed}


# ---------------------------------------------------------------------------- strengths
@pytest.mark.parametrize(("kind", "mode", "weight", "want"), [
    ("calls", "required", 0.9, 0.9), ("calls", "degradable", 0.5, 0.25),
    ("calls", "best_effort", 1.0, 0.1), ("stores_in", "required", 1.0, 1.0),
    ("hosted_on", "required", 0.8, 1.0), ("connected_to", "structural", 1.0, 0.7),
    ("monitors", "degradable", 0.5, 0.25), ("routes_to", "degradable", 0.8, 0.4),
    ("calls", "required", 7.0, 1.0), ("calls", "required", -1.0, 0.0)])
def test_strength_of_a_dependency(kind, mode, weight, want):
    assert strength(kind, mode, weight) == want


@pytest.mark.parametrize(("impact", "want"), [
    (1.0, "severe"), (0.75, "severe"), (0.6, "high"), (0.4, "high"), (0.2, "moderate"),
    (0.15, "moderate"), (0.1, "low"), (THRESHOLD, "low"), (0.01, "none"), (0.0, "none")])
def test_levels(impact, want):
    assert level(impact) == want


# ----------------------------------------------------------------------------- structures
def test_a_chain_weakens_step_by_step():
    g = graph([("b", "a", 0.8), ("c", "b", 0.5), ("d", "c", 0.5)])
    assert impacts(g, "a") == {"b": 0.8, "c": 0.4, "d": 0.2}
    a = analyse(g, ["a"])
    assert [(i.id, i.steps) for i in a.affected] == [("b", 1), ("c", 2), ("d", 3)]
    assert a.impacts["d"].via[0].path == ("a", "b", "c", "d")
    assert a.impacts["d"].via[0].links == ("b->a", "c->b", "d->c")


def test_only_dependents_are_affected_never_what_it_depends_on():
    g = graph([("b", "a", 1.0), ("a", "c", 1.0), ("d", "c", 1.0)])
    assert impacts(g, "a") == {"b": 1.0}
    assert set(analyse(g, ["a"]).reachable) == {"b"}


def test_the_strongest_path_wins_in_a_diamond():
    #   d -> b -> a  (0.9 x 0.9 = 0.81)      d -> c -> a  (0.5 x 1.0)
    g = graph([("b", "a", 0.9), ("c", "a", 1.0), ("d", "b", 0.9), ("d", "c", 0.5)])
    got = analyse(g, ["a"]).impacts["d"]
    assert got.impact == 0.81 and got.via[0].path == ("a", "b", "d")


def test_fewer_steps_win_when_equally_strong():
    g = graph([("b", "a", 1.0), ("c", "b", 1.0), ("c", "a", 1.0)])
    assert analyse(g, ["a"]).impacts["c"].via[0].path == ("a", "c")


def test_a_cycle_ends():
    g = graph([("b", "a", 1.0), ("c", "b", 1.0), ("a", "c", 1.0), ("d", "c", 0.5)])
    assert impacts(g, "a") == {"b": 1.0, "c": 1.0, "d": 0.5}


def test_weak_paths_end_below_the_threshold_but_stay_reachable():
    g = graph([("b", "a", 0.2), ("c", "b", 0.2), ("d", "c", 1.0)])
    a = analyse(g, ["a"])
    assert a.impacts["c"].impact == 0.04 and a.impacts["c"].level == "none"
    assert "d" not in a.impacts  # not followed from below the threshold
    assert [i.id for i in a.affected] == ["b"] and a.reachable == ["b", "c"]


def test_a_dependency_of_strength_zero_carries_nothing():
    g = graph([("b", "a", 0.0)])
    assert impacts(g, "a") == {}


def test_isolated_and_disconnected_parts_are_not_affected():
    g = graph([("b", "a", 1.0), ("y", "x", 1.0)], crit={"z": 5})
    assert impacts(g, "a") == {"b": 1.0}
    assert impacts(g, "z") == {}


def test_fan_out_affects_everyone_who_needs_it():
    g = graph([(f"s{i}", "core", 0.1 * i) for i in range(1, 11)])
    a = analyse(g, ["core"])
    assert a.count("low") == 10 - 0 and a.count() == 9  # 0.1 is low, 0.2.. at least moderate
    assert a.expected_affected() == 5.5


def test_two_failures_combine_as_independent_causes():
    g = graph([("c", "a", 0.5), ("c", "b", 0.5)])
    got = analyse(g, ["a", "b"]).impacts["c"]
    assert got.impact == 0.75 and {r.origin for r in got.via} == {"a", "b"}


def test_a_failed_component_reached_by_another_failure_stays_an_origin():
    g = graph([("b", "a", 1.0), ("c", "b", 1.0)])
    a = analyse(g, ["a", "b"])
    assert a.impacts["b"].failed and a.impacts["b"].level == "failed"
    assert [i.id for i in a.affected] == ["c"]


def test_criticality_weighs_the_score_not_the_impact():
    g = graph([("b", "a", 0.5), ("c", "a", 0.5)], crit={"b": 5, "c": 1})
    a = analyse(g, ["a"])
    assert a.impacts["b"].impact == a.impacts["c"].impact == 0.5
    assert (a.impacts["b"].score, a.impacts["c"].score) == (2.5, 0.5)
    assert a.business_impact() == 3.0


def test_unknown_origins_are_refused():
    with pytest.raises(KeyError):
        analyse(graph([("b", "a", 1.0)]), ["nobody"])


def test_an_edge_to_nowhere_is_refused():
    DependencyGraph({"a": Node("a", "A", "x", 3)}, {})  # fine
    with pytest.raises(ValueError):
        DependencyGraph.from_parts([Node("a", "A", "x", 3)],
                                   [("a", "ghost", "calls", "required", 1)])


def test_the_same_graph_always_gives_the_same_answer():
    edges = [("b", "a", 0.7), ("c", "a", 0.7), ("d", "b", 0.5), ("d", "c", 0.5), ("e", "d", 1)]
    shuffled = edges[:]
    random.Random(3).shuffle(shuffled)
    a, b = analyse(graph(edges), ["a"]), analyse(graph(shuffled), ["a"])
    assert a.impacts == b.impacts and a.links == b.links


@pytest.mark.parametrize("seed", range(12))
def test_random_graphs_against_brute_force(seed):
    """BFS with relaxation must find what trying every simple path finds."""
    rng = random.Random(seed)
    names = [f"n{i}" for i in range(9)]
    edges = {(a, b): round(rng.uniform(0.05, 1.0), 2) for a in names for b in names
             if a != b and rng.random() < 0.22}
    g = graph([(a, b, w) for (a, b), w in edges.items()])
    origin = rng.choice([n for n in names if n in g.nodes])
    best: dict[str, float] = {}

    def walk(node, impact, seen):
        for e in g.dependents[node]:
            if e.source in seen:
                continue
            x = impact * e.strength
            if x > best.get(e.source, 0):
                best[e.source] = x
            if x >= THRESHOLD:
                walk(e.source, x, seen | {e.source})
    walk(origin, 1.0, {origin})
    got = {i: x.impact for i, x in analyse(g, [origin]).impacts.items() if not x.failed}
    want = {k: round(v, 4) for k, v in best.items()}
    assert got == want


# ------------------------------------------------------------------- the bank, any shape
@pytest.fixture
def bank():
    return DependencyGraph.from_blueprint(bp.build(MAP, dict.fromkeys(NAMES, "process")))


def test_the_example_transaction_processing(bank):
    """It depends on Payment, Account and Fraud: they are not affected by its failure."""
    a = analyse(bank, ["bank-transaction"])
    affected = {i.id for i in a.affected}
    assert not affected & {"bank-payment", "bank-account", "bank-fraud", "bank-notification"}
    assert affected == {"lb-clients"}  # customers lose the payments and transfers
    r = report(bank, a, calls={"bank-transaction->bank-account": 4.2})
    lose = {x["id"]: x["loses_calls_per_s"] for x in r["not_affected_dependencies"]}
    assert lose == {"bank-account": 4.2, "bank-authentication": None, "bank-database": None,
                    "bank-fraud": None, "bank-payment": None}
    assert "not affected, because the failed component depends on them" in markdown(r).lower()


@pytest.mark.parametrize(("origin", "must", "never"), [
    ("bank-database", {"bank-account", "bank-customer", "bank-payment", "bank-transaction",
                       "bank-fraud", "bank-authentication"}, {"bank-notification"}),
    ("bank-authentication", {"bank-customer", "bank-account", "bank-transaction"},
     {"bank-database", "bank-payment", "bank-fraud"}),
    ("bank-payment", {"bank-transaction"}, {"bank-notification", "bank-database", "gw-external"}),
    ("bank-notification", set(), {"bank-payment", "bank-customer"}),
    ("bank-fraud", {"bank-transaction"}, {"bank-database"}),
    ("store-bank", {"bank-database", "bank-account"}, {"store-platform"}),
    ("server-bank-host", {f"bank-{n}" for n in NAMES}, {"server-docker"}),
    ("gw-external", {"bank-payment"}, {"bank-notification"}),
])
def test_failure_locations_in_the_bank(bank, origin, must, never):
    a = analyse(bank, [origin])
    affected = {i.id for i in a.affected if i.level in ("severe", "high", "moderate")}
    assert must <= affected, must - affected
    assert not never & affected, never & affected


def test_notification_is_only_weakly_needed(bank):
    """Payment needs notification best effort: its failure barely matters."""
    a = analyse(bank, ["bank-notification"])
    assert a.overall() in ("none", "low")


def test_critical_components_of_the_bank(bank):
    ranked = critical_components(bank)
    order = [c.id for c in ranked]
    assert order[0] == "server-bank-host" and order.index("bank-database") < 4
    assert order.index("bank-database") < order.index("bank-notification")
    spof = {c.id for c in ranked if c.single_point}
    assert {"bank-database", "server-bank-host", "bank-authentication"} <= spof
    assert "bank-notification" not in spof


def test_the_graph_follows_the_structure_of_the_twin():
    """In containers the bank host disappears; the engine knows only what the twin has."""
    processes = DependencyGraph.from_blueprint(bp.build(MAP, dict.fromkeys(NAMES, "process")))
    containers = DependencyGraph.from_blueprint(bp.build(MAP, dict.fromkeys(NAMES, "container")))
    assert "server-bank-host" in processes.nodes and "server-bank-host" not in containers.nodes
    assert processes.checksum != containers.checksum
    docker = analyse(containers, ["server-docker"])
    assert {f"bank-{n}" for n in NAMES} <= {i.id for i in docker.affected}


def test_a_different_bank_works_the_same():
    """No name of the simulated bank is built in: another map, another graph."""
    other = {"services": {
        "web": {"title": "Web", "port": 1, "max_concurrency": 8, "criticality": 4,
                "node_id": "x-web", "baseline_latency_ms": 10},
        "ledger": {"title": "Ledger", "port": 2, "max_concurrency": 8, "criticality": 5,
                   "node_id": "x-ledger", "baseline_latency_ms": 10},
        "audit": {"title": "Audit", "port": 3, "max_concurrency": 8, "criticality": 2,
                  "node_id": "x-audit", "baseline_latency_ms": 10},
        "database": {"title": "Db", "port": 4, "max_concurrency": 8, "criticality": 5,
                     "node_id": "x-db", "baseline_latency_ms": 10}},
        "dependencies": [
            {"source": "web", "target": "ledger", "mode": "required", "weight": 0.7, "purpose": "",
             "timeout_ms": 100},
            {"source": "ledger", "target": "audit", "mode": "degradable", "weight": 0.6,
             "purpose": "", "timeout_ms": 100},
            {"source": "ledger", "target": "database", "mode": "required", "weight": 1.0,
             "purpose": "", "timeout_ms": 100}]}
    g = DependencyGraph.from_blueprint(bp.build(other, dict.fromkeys(other["services"],
                                                                     "process")))
    got = impacts(g, "x-audit")
    assert got["x-ledger"] == 0.3 and got["x-web"] == 0.21


# ------------------------------------------------------------------------ in the twin
@pytest.fixture
def world():
    return World()


@pytest.fixture
def twin(world):
    t = DigitalTwin(MAP)
    run(t, world, 3)
    return t


def view(twin, cid):
    return schemas.component_view(twin, cid).impact


def test_nothing_failed_nothing_to_show(twin):
    assert twin.impact.analysis is None
    assert all(view(twin, c) is None for c in twin.components)
    assert schemas.summary(twin).propagation.level == "none"


def test_a_crash_is_an_origin_and_its_dependents_are_affected(twin, world):
    world.down("payment")
    change = run(twin, world)
    assert view(twin, "bank-payment").role == "origin"
    tx = view(twin, "bank-transaction")
    assert tx.level == "high" and tx.chain == ["bank-payment", "bank-transaction"]
    assert tx.links == ["bank-transaction->bank-payment"]
    # the twin's own rule 'depends on a failed component' is not a measurement
    assert tx.role == "predicted"
    s = schemas.summary(twin).propagation
    assert s.origins == ["bank-payment"] and s.affected >= 1 and s.event_id
    assert len(change.events) == 1 and change.events[0].new == ("bank-payment",)
    k = schemas.link_view(twin, "bank-transaction->bank-payment").display
    assert k.propagation == 0.6 and k.propagation_step == 1
    assert schemas.component_view(twin, "bank-payment").status is Status.FAILED


def test_measured_trouble_makes_an_impact_confirmed(twin, world):
    world.down("payment")
    run(twin, world)
    world.m("transaction")["error_rate"] = 0.3
    run(twin, world, 2)
    assert view(twin, "bank-transaction").role == "confirmed"


def test_a_cascade_is_traced_to_its_root(twin, world):
    world.down("database")
    for n in ("account", "customer"):
        world.m(n)["error_rate"] = 0.9
    run(twin, world, 3)
    assert twin.impact.analysis.origins == ("bank-database",)
    acc = view(twin, "bank-account")
    assert acc.role == "confirmed" and acc.chain[0] == "bank-database"


def test_an_event_is_judged_after_a_minute(twin, world):
    world.down("payment")
    run(twin, world)
    world.m("transaction")["error_rate"] = 0.25  # measured trouble where it was predicted
    world.m("fraud")["error_rate"] = 0.25  # and where it was not
    for _ in range(31):
        world.now += timedelta(seconds=2)
        change = run(twin, world)
    closed = [e for e in change.events if e.closed_at] or \
        [e for e in twin.impact.events if e.closed_at]
    e = closed[0]
    assert "bank-transaction" in e.outcome["hits"] and "bank-fraud" in e.outcome["missed"]
    assert e.outcome["judged"] and 0 < e.outcome["precision"] <= 1
    assert e.closed_at - e.opened_at >= WINDOW
    assert twin.impact.open == []


def test_a_quiet_twin_still_closes_its_events(twin, world):
    world.down("payment")
    run(twin, world)
    assert twin.impact.open
    later = world.now + timedelta(seconds=61)
    change = twin.tick(later)
    assert change and change.events[0].closed_at == later and not twin.impact.open


def test_recovery_ends_the_propagation(twin, world):
    world.down("payment")
    run(twin, world)
    world.up("payment")
    run(twin, world, 8)
    assert twin.impact.analysis is None and view(twin, "bank-transaction") is None
    assert schemas.link_view(twin, "bank-transaction->bank-payment").display.propagation == 0


def test_a_second_failure_opens_a_second_event(twin, world):
    world.down("payment")
    run(twin, world)
    world.down("fraud")
    change = run(twin, world)
    assert [e.new for e in change.events] == [("bank-fraud",)]
    tx = view(twin, "bank-transaction")
    assert set(tx.origins) == {"bank-payment", "bank-fraud"} and tx.impact == 0.7


def test_losing_contact_ends_events_without_judging_them(twin, world):
    world.down("payment")
    run(twin, world)
    twin.expire(datetime.now(UTC) + timedelta(days=1))
    e = twin.impact.events[-1]
    assert e.outcome["judged"] is False and twin.impact.analysis is None


def test_a_change_of_structure_rebuilds_the_graph(twin, world):
    before = twin.impact.graph_of(twin).checksum
    for n in NAMES:
        world.services[n]["deployment"] = "container"
    run(twin, world)
    world.down("payment")
    run(twin, world)
    assert twin.impact.graph_of(twin).checksum != before
    assert "server-bank-host" not in twin.impact.graph_of(twin).nodes
    assert view(twin, "bank-transaction").level == "high"


# -------------------------------------------------------------------------------- API
def test_api_what_if_changes_nothing(api):
    before = api.get("/api/v2/twin").json()["meta"]["version"]
    r = api.get("/api/v2/twin/propagation/analyze?failed=bank-database").json()
    assert r["what_if"] and r["level"] == "severe" and r["affected"] >= 6
    assert r["links"]["bank-account->bank-database"] == {"impact": 1.0, "step": 1}
    assert api.get("/api/v2/twin").json()["meta"]["version"] == before
    md = api.get("/api/v2/twin/propagation/analyze?failed=bank-database&format=markdown")
    assert md.headers["content-type"].startswith("text/markdown") and "| Account" in md.text


@pytest.mark.parametrize("bad", ["", "nobody", "bank-a,,b", "a;drop", ",".join(["bank-a"] * 21)])
def test_api_refuses_bad_lists(api, bad):
    assert api.get(f"/api/v2/twin/propagation/analyze?failed={bad}").status_code in (404, 422)


def test_api_live_critical_graph_events(api):
    assert api.get("/api/v2/twin/propagation").json()["report"] is None
    api.world.down("payment")
    api.feed(2)
    live = api.get("/api/v2/twin/propagation").json()
    assert live["origins"] == ["bank-payment"] and live["report"]["impacts"]
    crit = api.get("/api/v2/twin/propagation/critical").json()["components"]
    assert crit[0]["id"] == "server-bank-host" and crit[0]["single_point"]
    g = api.get("/api/v2/twin/propagation/graph").json()
    body = api.get("/api/v2/twin").json()
    assert len(g["nodes"]) == len(body["components"]) == 17
    assert len(g["edges"]) == len(body["links"])
    assert api.get("/api/v2/twin/propagation/events/live").json()[0]["origins"] == ["bank-payment"]
    assert api.get("/api/v2/twin/propagation/events").status_code == 503  # no database here
    body = api.get("/api/v2/twin").json()
    assert body["summary"]["propagation"]["origins"] == ["bank-payment"]
    tx = next(c for c in body["components"] if c["id"] == "bank-transaction")
    assert tx["impact"]["role"] == "predicted"


def test_api_stream_carries_the_propagation(api):
    with api.websocket_connect("/ws/v2/twin") as ws:
        ws.receive_json()
        api.world.down("payment")
        api.feed()
        m = ws.receive_json()
        ids = {c["id"] for c in m["components"]}
        assert {"bank-payment", "bank-transaction", "lb-clients"} <= ids
        links = {k["id"]: k["display"] for k in m["links"]}
        assert links["bank-transaction->bank-payment"]["propagation"] == 0.6


def test_report_json_is_serialisable(bank):
    r = report(bank, analyse(bank, ["bank-database", "bank-fraud"]))
    assert json.loads(json.dumps(r))["affected"] == r["affected"]
    assert r["impacts"][0]["chain"][0] in ("Database Service", "Fraud Detection Service")
