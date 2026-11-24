"""Live check of the platform stack: PostgreSQL, backend, 3D UI, Prometheus, Grafana.

    backend\\.venv\\Scripts\\python infra/tests/stack_check.py            (from the repository root)
    ... stack_check.py --keep                                           leave it running

Uses its own Compose project, random passwords and volumes, and removes them afterwards.
Ports 5432, 8000, 8080, 9090 and 3000 must be free. Results: infra/results/stack.{json,md}.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import websockets  # noqa: E402
from app.domain import contracts as c  # noqa: E402
from app.domain import models as m  # noqa: E402
from app.explain.explainer import explain_propagation  # noqa: E402
from app.propagation.engine import analyze  # noqa: E402
from app.simulation.bank_services import load_bank_services_topology  # noqa: E402
from app.simulation.bank_topology import build_reference_topology  # noqa: E402
from app.simulation.infrastructure import SimulatedBankInfrastructure  # noqa: E402
from app.twin.state import TwinState  # noqa: E402

PROJECT = "simucore-stack-test"
SERVICES = ["postgres", "backend", "frontend", "prometheus", "grafana", "monitoring"]
UNPUBLISHED = {"node-exporter", "postgres-exporter"}  # no ports on this PC, no health check
SECRETS = {k: secrets.token_hex(16) for k in
           ("POSTGRES_PASSWORD", "GRAFANA_DB_PASSWORD", "GRAFANA_ADMIN_PASSWORD",
            "MONITOR_DB_PASSWORD", "EXPORTER_DB_PASSWORD", "TWIN_DB_PASSWORD")}
ENV = {"POSTGRES_USER": "simucore", "GRAFANA_ADMIN_USER": "admin", **SECRETS}
GRAFANA = "http://127.0.0.1:3000"
CHECKS = []


def check(code: str, title: str):
    def add(fn):
        CHECKS.append((code, title, fn))
        return fn
    return add


class Stack:
    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        # Only the env file may supply values, so a stray shell variable cannot hide a mistake.
        self.environ = {k: v for k, v in os.environ.items()
                        if not k.startswith(("POSTGRES_", "GRAFANA_", "SIMUCORE_"))}

    def docker(self, *args: str, check_rc: bool = True) -> str:
        r = subprocess.run(["docker", *args], cwd=ROOT, env=self.environ, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if check_rc and r.returncode:
            raise RuntimeError(f"docker {' '.join(args[:6])}: {r.stderr.strip()[-600:]}")
        return (r.stdout + ("" if check_rc else r.stderr)).strip()

    def compose(self, *args: str) -> str:
        return self.docker("compose", "-p", PROJECT, "--env-file", str(self.env_file),
                           "-f", "infra/docker-compose.yml", *args)

    def ps(self) -> dict[str, dict]:
        rows = [json.loads(x) for x in self.compose("ps", "-a", "--format", "json").splitlines()]
        return {r["Service"]: r for r in rows}

    def wait_healthy(self, names=SERVICES, timeout_s: float = 150) -> float:
        start = time.monotonic()
        while True:
            state = {s: r["Health"] or r["State"] for s, r in self.ps().items()}
            if all(state.get(n) == "healthy" for n in names):
                return round(time.monotonic() - start, 1)
            if time.monotonic() - start > timeout_s:
                raise AssertionError(f"not healthy: {state}")
            time.sleep(2)

    async def db(self, user: str = "simucore", password: str | None = None):
        return await asyncpg.connect(host="127.0.0.1", port=5432, database="simucore", user=user,
                                     password=password or SECRETS["POSTGRES_PASSWORD"], timeout=8)


@contextlib.asynccontextmanager
async def rolled_back(stack: Stack):
    """A connection whose changes are always undone."""
    conn = await stack.db()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


J = json.dumps


async def insert_topology(conn, version: int, topo: m.Topology) -> None:
    await conn.execute("INSERT INTO topology_version(version,source,checksum) VALUES($1,'t','c')",
                       version)
    for n in topo.nodes:
        await conn.execute(
            "INSERT INTO infra_node(id,topology_version,name,kind,layer,criticality,capacity,"
            "attributes) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", n.id, version, n.name, n.kind.value,
            n.layer.value, n.criticality, J(n.capacity), J(n.attributes))
    for e in topo.edges:
        await conn.execute("INSERT INTO infra_edge(topology_version,source_id,target_id,kind,"
                           "weight) VALUES($1,$2,$3,$4,$5)", version, e.source, e.target,
                           e.kind.value, e.weight)


# ------------------------------------------------------------------------- platform
@check("P01", "All containers run and are healthy; ports are reachable from this PC only")
async def p01(s: Stack) -> dict:
    rows = s.ps()
    assert rows.pop("migrate")["ExitCode"] == 0
    assert set(rows) == {*SERVICES, *UNPUBLISHED}
    for name, r in rows.items():
        if name in UNPUBLISHED:
            assert r["State"] == "running", (name, r["Status"])
            assert not any(p["PublishedPort"] for p in r["Publishers"] or []), name
            continue
        assert r["Health"] == "healthy", (name, r["Status"])
        hosts = {p["URL"] for p in r["Publishers"] if p["PublishedPort"]}
        assert hosts == {"127.0.0.1"}, (name, hosts)
    return {"healthy": len(rows)}


@check("P02", "Passwords come from .env; defaults are refused; none leak to other services")
async def p02(s: Stack) -> dict:
    await (await s.db()).close()
    for user, pw in (("simucore", "simucore"), ("postgres", "postgres")):
        try:
            conn = await s.db(user, pw)
        except asyncpg.InvalidAuthorizationSpecificationError:
            continue
        await conn.close()
        raise AssertionError(f"default password accepted for {user}")
    async with httpx.AsyncClient(trust_env=False) as h:
        codes = {pw_kind: (await h.get(f"{GRAFANA}/api/org", auth=("admin", pw))).status_code
                 for pw_kind, pw in (("default", "admin"),
                                     ("env", SECRETS["GRAFANA_ADMIN_PASSWORD"]))}
        anonymous = (await h.get(f"{GRAFANA}/api/org")).status_code
    assert codes == {"default": 401, "env": 200} and anonymous == 401, (codes, anonymous)
    env = s.compose("exec", "-T", "backend", "env")
    assert SECRETS["GRAFANA_ADMIN_PASSWORD"] not in env
    assert SECRETS["GRAFANA_DB_PASSWORD"] not in env
    mon = s.compose("exec", "-T", "monitoring", "env")
    assert not any(v in mon for k, v in SECRETS.items() if k != "MONITOR_DB_PASSWORD")
    missing = subprocess.run(["docker", "compose", "-f", "infra/docker-compose.yml", "config",
                              "-q"], cwd=ROOT, env=s.environ, capture_output=True)
    assert missing.returncode != 0, "stack must not start without passwords"
    return {"grafana": codes, "without_env_file": "refused"}


# --------------------------------------------------------------------------- schema
@check("P03", "Schema loads without errors")
async def p03(s: Stack) -> dict:
    logs = s.compose("logs", "postgres")
    init = logs.split("PostgreSQL init process complete")[0]
    assert "ERROR" not in init and "FATAL" not in init, init[-800:]
    conn = await s.db()
    n = await conn.fetchval(
        "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace "
        "AND relkind IN ('r','p') AND NOT relispartition")
    await conn.close()
    assert n == 29, n  # 22 of Phase 1, 3 of monitoring, 4 of the digital twin
    return {"tables": n}


@check("P04", "Database enums match the Python models exactly")
async def p04(s: Stack) -> dict:
    conn = await s.db()
    pairs = {"node_kind": m.NodeKind, "node_layer": m.NodeLayer, "edge_kind": m.EdgeKind,
             "health_status": m.HealthStatus, "severity": c.Severity}
    for name, enum in pairs.items():
        values = await conn.fetchval("SELECT array_agg(enumlabel::text) FROM pg_enum "
                                     "WHERE enumtypid=$1::regtype", name)
        assert set(values) == {e.value for e in enum}, (name, values)
    columns = set(await conn.fetchval(
        "SELECT array_agg(column_name::text) FROM information_schema.columns "
        "WHERE table_name='metric_sample'"))
    await conn.close()
    assert set(m.STANDARD_METRICS) <= columns, set(m.STANDARD_METRICS) - columns
    return {"enums": len(pairs), "metric_columns": len(m.STANDARD_METRICS)}


@check("P05", "Both real topologies and live simulated data fit the schema and read back equal")
async def p05(s: Stack) -> dict:
    reference = build_reference_topology()
    infra = SimulatedBankInfrastructure(reference, seed=1)
    state = TwinState()
    state.load_topology(reference)
    samples = 0
    async with rolled_back(s) as conn:
        for v, topo in ((1, reference), (2, load_bank_services_topology())):
            await insert_topology(conn, v, topo)
            rows = await conn.fetch("SELECT * FROM infra_node WHERE topology_version=$1", v)
            assert {r["id"]: json.loads(r["attributes"]) for r in rows} == \
                   {n.id: n.attributes for n in topo.nodes}
            assert await conn.fetchval("SELECT count(*) FROM infra_edge WHERE topology_version=$1",
                                       v) == len(topo.edges)
        for _ in range(20):
            batch = infra.step()
            for x in batch:
                await conn.execute(
                    "INSERT INTO metric_sample(ts,node_id,cpu_pct,mem_pct,latency_ms,error_rate,"
                    "rps,availability) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", x.ts, x.node_id,
                    *(x.metrics[k] for k in m.STANDARD_METRICS))
            for st in state.apply(batch):
                await conn.execute(
                    "INSERT INTO node_state_snapshot VALUES($1,$2,$3,$4,$5)", st.updated_at,
                    state.version, st.node_id, st.status.value, J(st.metrics))
            samples += len(batch)
        assert await conn.fetchval("SELECT count(*) FROM metric_sample "
                                   "WHERE node_id NOT LIKE 'bank-%'") == samples
    return {"nodes": 27 + 8, "samples": samples}


@check("P06", "Every inter-module contract can be stored")
async def p06(s: Stack) -> dict:
    from app.domain.topology import TopologyGraph
    graph = TopologyGraph(build_reference_topology())
    prop = analyze(graph, c.PropagationRequest(failed_nodes=["payments-db"]))
    explanation = explain_propagation(prop)
    anomaly = c.AnomalyEvent(node_id="payments-db", score=0.9, severity=c.Severity.HIGH,
                             contributing_metrics={"cpu_pct": 97.0})
    pred = c.FailurePrediction(node_id="payments-db", horizon_s=900, probability=0.8)
    scen = c.SimulationScenario(name="db crash", faults=[
        c.FaultSpec(node_id="payments-db", type="crash")])
    opt = c.OptimizationProposal(objectives=["cost", "p95_latency", "risk"], pareto_front=[
        c.ParetoSolution(decision={"payments-service": {"replicas": 4}},
                         objectives={"cost": 1.0, "p95_latency": 2.0, "risk": 0.1})])
    cal = c.CalibrationReport(target="failure-xgb", metric_name="brier", metric_before=0.3,
                              metric_after=0.2, action="recalibrate")
    rec = c.Recommendation(source_module="propagation", title="Add a replica",
                           action={"type": "scale"}, target_nodes=["payments-db"], priority=4.0,
                           confidence=0.7, explanation=explanation)
    async with rolled_back(s) as db:
        await db.execute("INSERT INTO anomaly_event(id,ts,node_id,score,severity,"
                         "contributing_metrics) VALUES($1,$2,$3,$4,$5,$6)", anomaly.id, anomaly.ts,
                         anomaly.node_id, anomaly.score, anomaly.severity.value,
                         J(anomaly.contributing_metrics))
        await db.execute("INSERT INTO prediction(id,predicted_at,node_id,horizon_s,probability) "
                         "VALUES($1,$2,$3,$4,$5)", pred.id, pred.predicted_at, pred.node_id,
                         pred.horizon_s, pred.probability)
        await db.execute("INSERT INTO prediction_outcome(prediction_id,actual_event,error) "
                         "VALUES($1,true,0.2)", pred.id)
        await db.execute("INSERT INTO propagation_analysis(origin_nodes,method,impacted,"
                         "business_impact) VALUES($1,$2,$3,$4)", prop.origin, prop.method,
                         J([i.model_dump() for i in prop.impacted]), prop.business_impact)
        sid = await db.fetchval("INSERT INTO simulation_scenario(name,faults,load_multiplier,"
                                "duration_s) VALUES($1,$2,$3,$4) RETURNING id", scen.name,
                                J([f.model_dump() for f in scen.faults]), scen.load_multiplier,
                                scen.duration_s)
        rid = await db.fetchval("INSERT INTO simulation_run(scenario_id,seed) VALUES($1,$2) "
                                "RETURNING id", sid, scen.seed)
        await db.execute("INSERT INTO simulation_metric VALUES($1,1.5,'payments-db','{}')", rid)
        oid = await db.fetchval("INSERT INTO optimization_run(objectives,population,generations) "
                                "VALUES($1,80,100) RETURNING id", opt.objectives)
        for sol in opt.pareto_front:
            await db.execute("INSERT INTO optimization_solution(run_id,rank,decision,"
                             "objective_values) VALUES($1,$2,$3,$4)", oid, sol.rank,
                             J(sol.decision), J(sol.objectives))
        await db.execute("INSERT INTO calibration_run(target,metric_name,metric_before,"
                         "metric_after,drift,action) VALUES($1,$2,$3,$4,$5,$6)", cal.target,
                         cal.metric_name, cal.metric_before, cal.metric_after, J(cal.drift),
                         cal.action)
        await db.execute("INSERT INTO recommendation(id,source_module,title,action,target_nodes,"
                         "priority,confidence) VALUES($1,$2,$3,$4,$5,$6,$7)", rec.id,
                         rec.source_module, rec.title, J(rec.action), rec.target_nodes,
                         rec.priority, rec.confidence)
        await db.execute("INSERT INTO explanation(recommendation_id,summary,factors,evidence,"
                         "method) VALUES($1,$2,$3,$4,$5)", rec.id, explanation.summary,
                         J([f.model_dump() for f in explanation.factors]),
                         J(explanation.evidence), explanation.method)
        await db.execute("INSERT INTO audit_log(actor,action) VALUES('t','adapter.query')")
    return {"contracts": 8}


TV = "INSERT INTO topology_version(version,source,checksum) VALUES(1,'t','c');"
NODE = ("INSERT INTO infra_node(id,topology_version,name,kind,layer,criticality) "
        "VALUES('a',1,'A','service','application',3);")
BAD = {
    "a telemetry source that can write":
        ("INSERT INTO telemetry_source(name,adapter,read_only) VALUES('x','y',false)", "23514"),
    "criticality above 5": (TV + NODE.replace(",3)", ",6)"), "23514"),
    "unknown node kind": (TV + NODE.replace("'service'", "'toaster'"), "22P02"),
    "edge weight above 1": (TV + NODE + "INSERT INTO infra_edge VALUES(1,'a','a','calls',1.5)",
                            "23514"),
    "edge to a missing node": (TV + NODE + "INSERT INTO infra_edge VALUES(1,'a','zz','calls',0.5)",
                               "23503"),
    "duplicate topology version": (TV + TV, "23505"),
    "duplicate metric sample": ("INSERT INTO metric_sample(ts,node_id) VALUES('2026-01-01','a');"
                                "INSERT INTO metric_sample(ts,node_id) VALUES('2026-01-01','a')",
                                "23505"),
    "anomaly score above 1": ("INSERT INTO anomaly_event(node_id,score,severity) "
                              "VALUES('a',1.5,'low')", "23514"),
    "probability above 1": ("INSERT INTO prediction(node_id,horizon_s,probability) "
                            "VALUES('a',60,1.2)", "23514"),
    "outcome without a prediction": ("INSERT INTO prediction_outcome(prediction_id) "
                                     "VALUES(gen_random_uuid())", "23503"),
    "explanation without a recommendation":
        ("INSERT INTO explanation(recommendation_id,summary,method) "
         "VALUES(gen_random_uuid(),'s','m')", "23503"),
    "duplicate username": ("INSERT INTO app_user(username) VALUES('u');"
                           "INSERT INTO app_user(username) VALUES('u')", "23505"),
}


@check("P07", "Bad data is refused, for the right reason")
async def p07(s: Stack) -> dict:
    for what, (sql, expected) in BAD.items():
        async with rolled_back(s) as conn:
            try:
                await conn.execute(sql)
            except asyncpg.PostgresError as exc:
                assert exc.sqlstate == expected, (what, exc.sqlstate, str(exc))
                continue
            raise AssertionError(f"accepted: {what}")
    return {"refused": len(BAD)}


@check("P08", "Deleting a parent removes its children and nothing else")
async def p08(s: Stack) -> dict:
    async with rolled_back(s) as conn:
        await insert_topology(conn, 1, build_reference_topology())
        await insert_topology(conn, 2, load_bank_services_topology())
        await conn.execute("DELETE FROM topology_version WHERE version=1")
        assert await conn.fetchval("SELECT count(*) FROM infra_node") == 8
        assert await conn.fetchval("SELECT count(*) FROM infra_edge") == 14
        sid = await conn.fetchval("INSERT INTO simulation_scenario(name) VALUES('x') RETURNING id")
        rid = await conn.fetchval("INSERT INTO simulation_run(scenario_id,seed) VALUES($1,1) "
                                  "RETURNING id", sid)
        await conn.execute("INSERT INTO simulation_metric VALUES($1,0,'a','{}')", rid)
        await conn.execute("DELETE FROM simulation_scenario WHERE id=$1", sid)
        assert await conn.fetchval("SELECT count(*) FROM simulation_metric") == 0
    return {}


@check("P09", "Monthly partitions work; measured: creating one after its data arrived")
async def p09(s: Stack) -> dict:
    part = ("CREATE TABLE metric_sample_y2031m01 PARTITION OF metric_sample "
            "FOR VALUES FROM ('2031-01-01') TO ('2031-02-01')")
    row = "INSERT INTO metric_sample(ts,node_id,cpu_pct) VALUES('2031-01-15','a',5)"
    async with rolled_back(s) as conn:
        await conn.execute(part)
        await conn.execute(row)
        assert await conn.fetchval(
            "SELECT tableoid::regclass::text FROM metric_sample WHERE node_id = 'a'"
        ) == "metric_sample_y2031m01"
        await conn.execute("DROP TABLE metric_sample_y2031m01")  # retention
        assert await conn.fetchval(
            "SELECT count(*) FROM metric_sample WHERE node_id = 'a'") == 0
    async with rolled_back(s) as conn:
        await conn.execute(row)  # arrives before its partition exists
        try:
            await conn.execute(part)
            late = "works"
        except asyncpg.PostgresError as exc:
            late = f"refused ({exc.sqlstate})"
    return {"partition_created_in_time": "works", "partition_created_after_data": late}


@check("P10", "Grafana's database role can read and do nothing else")
async def p10(s: Stack) -> dict:
    conn = await s.db("grafana_reader", SECRETS["GRAFANA_DB_PASSWORD"])
    assert await conn.fetchval("SELECT count(*) FROM metric_sample") > 0  # the monitor's
    attempts = ["INSERT INTO audit_log(actor,action) VALUES('x','y')", "DELETE FROM audit_log",
                "DROP TABLE audit_log", "CREATE TABLE evil(i int)", "TRUNCATE metric_sample",
                "CREATE ROLE evil", "ALTER ROLE grafana_reader SUPERUSER"]
    for read_only in (True, False):  # also after the session switches its own safety off
        if not read_only:
            await conn.execute("SET default_transaction_read_only = off")
        for sql in attempts:
            try:
                await conn.execute(sql)
            except (asyncpg.ReadOnlySQLTransactionError,
                    asyncpg.InsufficientPrivilegeError):
                continue
            raise AssertionError(f"allowed (read_only={read_only}): {sql}")
    await conn.close()
    return {"refused": len(attempts) * 2}


# ------------------------------------------------------------------------- services
async def ws_messages(url: str, n: int, timeout_s: float = 12) -> list[dict]:
    async with asyncio.timeout(timeout_s), websockets.connect(url, max_size=2**22) as ws:
        return [json.loads(await ws.recv()) for _ in range(n)]


@check("P11", "The 3D UI works through nginx: page, assets, API and live WebSocket")
async def p11(s: Stack) -> dict:
    base = "http://127.0.0.1:8080"
    async with httpx.AsyncClient(trust_env=False) as h:
        page = await h.get(base + "/")
        assert page.status_code == 200 and "SimuCore" in page.text
        assets = [x.split('"')[0] for x in page.text.split('"/assets/')[1:]]
        assert len(assets) >= 2
        for a in assets:
            assert (await h.get(f"{base}/assets/{a}")).status_code == 200, a
        assert (await h.get(base + "/some/deep/link")).status_code == 200
        topo = (await h.get(base + "/api/v1/topology")).json()
        blast = await h.post(base + "/api/v1/propagation/analyze",
                             json={"failed_nodes": ["core-switch"]})
        assert len(topo["nodes"]) == 27 and blast.status_code == 200
    msgs = await ws_messages("ws://127.0.0.1:8080/ws/twin", 4)
    assert [x["type"] for x in msgs] == ["snapshot", "delta", "delta", "delta"]
    versions = [x["version"] for x in msgs]
    assert versions == sorted(versions) and versions[-1] > versions[0]
    user = s.compose("exec", "-T", "frontend", "id", "-u")
    assert user != "0" and s.compose("exec", "-T", "backend", "id", "-u") != "0"
    return {"assets": len(assets), "live_updates": 3, "runs_as_root": False}


@check("P12", "Prometheus scrapes the backend and has every node's metrics")
async def p12(s: Stack) -> dict:
    async with httpx.AsyncClient(trust_env=False, base_url="http://127.0.0.1:9090") as h:
        for _ in range(20):
            targets = (await h.get("/api/v1/targets")).json()["data"]["activeTargets"]
            r = (await h.get("/api/v1/query", params={"query": "count(simucore_node_metric)"}))
            data = r.json()["data"]["result"]
            health = {t["labels"]["job"]: t["health"] for t in targets}
            if health.get("simucore-backend") == "up" and data:
                break
            await asyncio.sleep(1)
    expected = {"prometheus", "simucore-backend", "monitoring", "node", "postgres"}
    assert {j for j, h in health.items() if h == "up"} >= expected, health
    assert data[0]["value"][1] == str(27 * 6), data
    return {"series": int(data[0]["value"][1])}


@check("P13", "Grafana: both data sources connect and every dashboard panel returns data")
async def p13(s: Stack) -> dict:
    auth = ("admin", SECRETS["GRAFANA_ADMIN_PASSWORD"])
    panels = {}
    async with httpx.AsyncClient(trust_env=False, base_url=GRAFANA, auth=auth, timeout=15) as h:
        for uid in ("simucore-prometheus", "simucore-postgres"):
            r = await h.get(f"/api/datasources/uid/{uid}/health")
            assert r.status_code == 200 and r.json()["status"] == "OK", (uid, r.text)
        dash = (await h.get("/api/dashboards/uid/simucore-overview")).json()["dashboard"]
        for p in dash["panels"]:
            expr = p["targets"][0]["expr"].replace("$node", ".*")
            r = await h.post("/api/ds/query", json={"from": "now-5m", "to": "now", "queries": [
                {"refId": "A", "datasource": {"uid": "simucore-prometheus"}, "expr": expr,
                 "instant": True, "range": False}]})
            frames = r.json()["results"]["A"].get("frames", [])
            panels[p["title"]] = len([f for f in frames if f["data"]["values"]])
            assert panels[p["title"]], (p["title"], r.text[:300])
    return {"datasources": 2, "series_per_panel": panels}


# ----------------------------------------------------------------- failure & recovery
@check("P14", "A recreated backend (new address) is found again by nginx without a restart")
async def p14(s: Stack) -> dict:
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    name = f"{PROJECT}-backend-1"
    before = s.docker("inspect", "-f", fmt, name)
    # Occupy the old address so that the new container is forced onto a different one.
    s.compose("rm", "-sf", "backend")
    s.docker("run", "-d", "--rm", "--name", f"{PROJECT}-squatter", "--network",
             f"{PROJECT}_default", "--ip", before, "postgres:16-alpine", "sleep", "120",
             check_rc=False)
    try:
        s.compose("up", "-d", "--no-deps", "backend")
        after = s.docker("inspect", "-f", fmt, name)
        start = time.monotonic()
        async with httpx.AsyncClient(trust_env=False) as h:
            while True:
                with contextlib.suppress(httpx.HTTPError):
                    if (await h.get("http://127.0.0.1:8080/api/v1/ready")).status_code == 200:
                        break
                assert time.monotonic() - start < 40, "nginx never found the new backend"
                await asyncio.sleep(0.5)
        took = round(time.monotonic() - start, 1)
        await ws_messages("ws://127.0.0.1:8080/ws/twin", 2)
    finally:
        s.docker("rm", "-f", f"{PROJECT}-squatter", check_rc=False)
    return {"address_changed": before != after, "recovered_after_s": took}


@check("P15", "PostgreSQL down: the live twin keeps running (it keeps state in memory)")
async def p15(s: Stack) -> dict:
    s.compose("stop", "postgres")
    try:
        msgs = await ws_messages("ws://127.0.0.1:8080/ws/twin", 4)
        assert msgs[-1]["version"] > msgs[0]["version"]
    finally:
        s.compose("start", "postgres")
        s.wait_healthy(["postgres"])
    return {"updates_without_database": 3}


@check("P16", "Data survives a PostgreSQL crash and a full stack down/up")
async def p16(s: Stack) -> dict:
    conn = await s.db()
    await insert_topology(conn, 7, load_bank_services_topology())
    await conn.close()
    s.docker("kill", f"{PROJECT}-postgres-1")
    s.compose("up", "-d")
    s.wait_healthy()
    conn = await s.db()
    assert await conn.fetchval("SELECT count(*) FROM infra_node") == 8
    await conn.close()
    async with httpx.AsyncClient(trust_env=False) as h:
        auth = ("admin", SECRETS["GRAFANA_ADMIN_PASSWORD"])
        q = {"query": "min_over_time(timestamp(simucore_twin_version)[1h:5s])"}
        r = await h.get("http://127.0.0.1:9090/api/v1/query", params=q)
        oldest = float(r.json()["data"]["result"][0]["value"][1])
        folder = await h.post(f"{GRAFANA}/api/folders", auth=auth, json={"title": "survivor"})
        assert folder.status_code == 200, folder.text
        s.compose("down")  # volumes are kept
        down_at = time.time()
        s.compose("up", "-d")
        s.wait_healthy()
        r = await h.get("http://127.0.0.1:9090/api/v1/query", params=q)
        still = float(r.json()["data"]["result"][0]["value"][1])
        folders = (await h.get(f"{GRAFANA}/api/folders", auth=auth)).json()
    conn = await s.db()
    assert await conn.fetchval("SELECT count(*) FROM infra_edge") == 14
    await conn.execute("DELETE FROM topology_version WHERE version=7")
    await conn.close()
    assert still < down_at and abs(still - oldest) < 30, (oldest, still)
    assert "survivor" in {f["title"] for f in folders}
    return {"postgres": "kept", "prometheus_history": "kept", "grafana": "kept"}


# --------------------------------------------------------------------------- runner
async def main() -> int:
    keep = "--keep" in sys.argv
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "stack.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in ENV.items()), encoding="utf-8")
        s = Stack(env_file)
        if s.docker("ps", "-q", "--filter", "name=simucore"):
            sys.exit("A simucore stack is already running; stop it first.")
        try:
            started = time.monotonic()
            s.compose("up", "-d", "--build")
            s.wait_healthy()
            print(f"stack healthy after {time.monotonic() - started:.0f}s", flush=True)
            for code, title, fn in CHECKS:
                t, facts, error = time.monotonic(), {}, None
                try:
                    facts = await fn(s)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:600]}"
                results.append({"code": code, "title": title, "passed": error is None,
                                "seconds": round(time.monotonic() - t, 1), "facts": facts,
                                "error": error})
                print(f"{'PASS' if not error else 'FAIL'} {code} {title}\n     "
                      f"{error or json.dumps(facts)}", flush=True)
        finally:
            if not keep:
                s.compose("down", "--volumes")
    passed = sum(r["passed"] for r in results)
    out = ROOT / "infra" / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    (out / "stack.json").write_text(json.dumps(
        {"run_at": stamp, "passed": passed, "total": len(results), "results": results}, indent=2),
        encoding="utf-8", newline="\n")
    lines = ["# Platform stack results", "", f"Run at {stamp}. **{passed} of {len(results)} "
             "passed.** Generated by `infra/tests/stack_check.py`.", "",
             "| | Check | Result | Measured |", "|---|---|---|---|"]
    lines += [f"| {r['code']} | {r['title']} | {'pass' if r['passed'] else '**FAIL**'} "
              f"| {r['error'] or json.dumps(r['facts'])} |" for r in results]
    (out / "stack.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
