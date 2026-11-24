"""Live test of the digital twin core: are changes in the real (simulated) bank reflected correctly?

    cd monitoring
    .venv/Scripts/python ../backend/live/twin_core_check.py             about 25 minutes
    .venv/Scripts/python ../backend/live/twin_core_check.py D04 D12     a subset

Real bank, real Prometheus, real monitoring, real PostgreSQL; the twin runs in its container.
BANKCTL_MODE=docker runs the bank as containers. Uses the monitoring environment and its Lab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import psutil
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "monitoring"))

from live.pipeline_check import (  # noqa: E402
    BACKEND,
    DOCKER,
    PROM,
    PW,
    SERVICES,
    Lab,
    bank_pid,
    lookup,
    steady,
)

V2 = f"{BACKEND}/api/v2/twin"
WS = "ws://127.0.0.1:8080/ws/v2/twin"  # through nginx, as the 3D frontend will
CHECKS: list[tuple] = []
EDGES = [(d["source"], d["target"]) for d in json.loads(
    (ROOT / "banking-sim/dependency_map.json").read_text(encoding="utf-8"))["dependencies"]]


def check(code: str, title: str):
    def add(fn):
        CHECKS.append((code, title, fn))
        return fn
    return add


# ------------------------------------------------------------------------------ helpers
class Mirror:
    """Rebuilds the twin only from WebSocket messages, as the 3D frontend will."""

    def __init__(self) -> None:
        self.components: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.meta: dict = {}
        self.transitions: list[dict] = []
        self.deltas = self.lost = 0
        self.sizes: list[int] = []

    def receive(self, raw: str) -> dict:
        m = json.loads(raw)
        if m["type"] == "snapshot":
            self.components = {c["id"]: c for c in m["components"]}
            self.links = {k["id"]: k for k in m["links"]}
        else:
            self.lost += m["base_version"] != self.meta["version"]
            self.components.update({c["id"]: c for c in m["components"]})
            self.links.update({k["id"]: k for k in m["links"]})
            self.transitions += m["transitions"]
            if m["type"] == "delta":
                self.deltas += 1
                self.sizes.append(len(raw))
        self.meta = m["meta"]
        return m

    async def follow(self) -> None:
        async with websockets.connect(WS, max_size=2**23, origin="http://localhost:8080") as ws:
            async for raw in ws:
                self.receive(raw)


async def twin(lab: Lab) -> dict:
    return await lab.get(V2)


async def comp(lab: Lab, cid: str) -> dict:
    return await lab.get(f"{V2}/components/{cid}")


async def link(lab: Lab, a: str, b: str) -> dict:
    return await lab.get(f"{V2}/links/bank-{a}->bank-{b}")


def is_in(lab: Lab, cid: str, *wanted: str):
    async def probe():
        return (await comp(lab, cid))["status"] in wanted
    return probe


async def settled(lab: Lab, timeout: float = 90) -> None:
    """Every component healthy, the twin live, monitoring without open events."""
    blocking: list = []

    async def ok():
        t = await twin(lab)
        m = await lab.get(f"{PROM}/api/v1/metrics/current")
        blocking[:] = [(c["id"], c["status"], c["reason"]) for c in t["components"]
                       if c["status"] != "healthy"]
        blocking.extend((k["id"], k["status"], k["reason"]) for k in t["links"]
                        if k["status"] == "failed")
        blocking.extend((e["kind"], e["service"], e["metric"], e["message"])
                        for e in m["events"])
        if t["meta"]["sync"] != "live" or not m["fresh"]:
            blocking.append((t["meta"]["sync"], m["fresh"]))
        return not blocking
    try:
        await lab.until("settled twin", ok, timeout)
    except AssertionError as exc:
        raise AssertionError(f"not settled within {timeout}s: {blocking[:6]}") from exc


async def ctl(lab: Lab, *a: str) -> str:
    return await asyncio.to_thread(lab.ctl, *a)


async def traffic(lab: Lab, seconds: float, rps: float = 10):
    return asyncio.create_task(ctl(lab, "workload", "normal", "--rps", str(rps), "--duration",
                                   str(seconds)))


async def transitions(lab: Lab, cid: str, since: datetime) -> list[dict]:
    rows = await lab.get(f"{V2}/history/transitions", params={
        "object_id": cid, "start": since.isoformat(), "limit": 500})
    return rows[::-1]


def freeze(lab: Lab, service: str, pid: int, on: bool) -> None:
    if DOCKER:
        lab.docker("pause" if on else "unpause", f"bank-{service}")
    elif on:
        psutil.Process(pid).suspend()
    else:
        psutil.Process(pid).resume()


# -------------------------------------------------------------------------- the model
@check("D01", "The complete infrastructure is represented, and every part is known")
async def d01(lab: Lab) -> dict:
    t = await twin(lab)
    ids = [c["id"] for c in t["components"]] + [k["id"] for k in t["links"]]
    assert len(ids) == len(set(ids))
    for c in t["components"]:
        assert c["status"] == "healthy" and c["type"] and c["name"], c
        assert c["metrics"] or c["id"] == "mon-service", c["id"]
        assert c["dependencies"]["depends_on"] or c["dependencies"]["dependents"], c["id"]
    calls = [k for k in t["links"] if k["kind"] == "calls"]
    assert {(k["source"], k["target"]) for k in calls} == {
        (f"bank-{a}", f"bank-{b}") for a, b in EDGES}
    hosts = Counter(k["target"] for k in t["links"] if k["kind"] == "hosted_on"
                    and k["source"].startswith("bank-"))
    assert hosts == {"server-docker" if DOCKER else "server-bank-host": 8}, hosts
    return {"components": len(t["components"]), "by_type": t["summary"]["by_type"],
            "links": len(t["links"]), "bank_runs_on": next(iter(hosts))}


@check("D02", "Twin metrics equal the monitoring data they came from, value by value")
async def d02(lab: Lab) -> dict:
    load = await traffic(lab, 24, 12)
    await asyncio.sleep(4)
    compared = moments = 0
    while moments < 8:
        a = await lab.get(f"{PROM}/api/v1/metrics/current")
        t = await twin(lab)
        if t["meta"]["last_observation_at"] is None or datetime.fromisoformat(
                t["meta"]["last_observation_at"]) != datetime.fromisoformat(a["collected_at"]):
            await asyncio.sleep(0.2)
            continue
        moments += 1
        have = {c["id"]: c["metrics"] for c in t["components"]}
        for s in a["services"]:
            expected = {k: v for k, v in s["metrics"].items() if v is not None}
            assert have[s["node_id"]] == expected, s["service"]
            compared += len(expected)
        links = {k["id"]: k["metrics"] for k in t["links"]}
        for k in a["links"]:
            expected = {n: v for n, v in k["metrics"].items() if v is not None}
            assert links[f"bank-{k['source']}->bank-{k['target']}"] == expected, k
            compared += len(expected)
        await asyncio.sleep(1.9)
    await load
    return {"moments": moments, "values_compared": compared, "differences": 0}


@check("D03", "Network connections: calls, latency and failures match what was really sent")
async def d03(lab: Lab) -> dict:
    async with steady(lab, lookup(lab), 15):
        await asyncio.sleep(14)
        rates = []
        for _ in range(5):
            rates.append((await link(lab, "customer", "database"))["metrics"]["calls_per_s"])
            await asyncio.sleep(1)
        fine = await link(lab, "customer", "database")
        idle = await link(lab, "transaction", "payment")
        await lab.fault("database", "latency", latency_ms=200, jitter=0)
        await asyncio.sleep(14)
        slow = await link(lab, "customer", "database")
        await lab.clear()
    rate = statistics.median(rates)
    assert abs(rate - 15) <= 1 and fine["active"] and fine["display"]["flow"] > 0.5, rates
    assert 195 <= slow["metrics"]["latency_ms"] <= 230, slow["metrics"]
    assert not idle["active"] and idle["display"]["flow"] == 0 and idle["status"] == "healthy"
    return {"calls_per_s": {"truth": 15, "twin": rate}, "normal_ms": fine["metrics"]["latency_ms"],
            "with_200ms_delay": slow["metrics"]["latency_ms"]}


# ----------------------------------------------------------------------- five conditions
@check("D04", "Healthy stays healthy: no status change in 60 s of normal traffic")
async def d04(lab: Lab) -> dict:
    warm = await traffic(lab, 14)
    await warm
    await settled(lab)
    begin = datetime.now(UTC)
    await (await traffic(lab, 60))
    await asyncio.sleep(3)
    rows = await lab.get(f"{V2}/history/transitions", params={"start": begin.isoformat()})
    assert rows == [], rows[:5]
    return {"seconds": 60, "status_changes": 0}


@check("D05", "Failed and recovering: every service is killed and restarted once")
async def d05(lab: Lab) -> dict:
    failed, recovering, healthy = {}, {}, {}
    for s in SERVICES:
        await settled(lab)
        cid, begin = f"bank-{s}", datetime.now(UTC)
        await ctl(lab, "kill", s)
        failed[s] = await lab.until(f"{s} failed", is_in(lab, cid, "failed"), 15)
        t = await twin(lab)
        also = [c["id"] for c in t["components"] if c["status"] == "failed" and c["id"] != cid]
        assert not also, f"killing {s} also failed {also}"
        assert (await comp(lab, cid))["metrics"] == {"availability": 0.0}
        await ctl(lab, "start", s)
        recovering[s] = await lab.until(f"{s} recovering", is_in(lab, cid, "recovering"), 15)
        healthy[s] = await lab.until(f"{s} healthy", is_in(lab, cid, "healthy"), 40)
        await asyncio.sleep(2)
        path = [x["to_status"] for x in await transitions(lab, cid, begin)]
        assert path == ["failed", "recovering", "healthy"], (s, path)
    return {"slowest_failed_s": max(failed.values()),
            "slowest_recovering_s": max(recovering.values()),
            "recovery_phase_s": [min(healthy.values()), max(healthy.values())]}


@check("D06", "Failed: soft downtime, and most requests failing")
async def d06(lab: Lab) -> dict:
    out = {}
    await lab.fault("fraud", "downtime", 60)
    out["downtime_s"] = await lab.until("fraud failed", is_in(lab, "bank-fraud", "failed"), 12)
    await lab.clear()
    await lab.until("fraud healthy", is_in(lab, "bank-fraud", "healthy"), 40)
    await lab.fault("database", "db_connection_failure", mode="refuse")
    async with steady(lab, lookup(lab), 8):
        out["errors_s"] = await lab.until("customer failed",
                                          is_in(lab, "bank-customer", "failed"), 30)
        c = await comp(lab, "bank-customer")
    await lab.clear()
    assert any(f["rule"] == "errors" for f in c["findings"]), c["findings"]
    assert c["root_causes"] == ["store-bank"], c["root_causes"]
    return out | {"root_cause": c["root_causes"]}


@check("D07", "Warning: slower than normal because of something else, not overloaded")
async def d07(lab: Lab) -> dict:
    await lab.fault("customer", "latency", latency_ms=150, jitter=0)
    async with steady(lab, lookup(lab), 8):
        took = await lab.until("warning", is_in(lab, "bank-customer", "warning"), 30)
        await asyncio.sleep(8)
        c = await comp(lab, "bank-customer")
    await lab.clear()
    assert c["status"] == "warning" and c["findings"][0]["rule"] == "slow", c
    back = await lab.until("healthy", is_in(lab, "bank-customer", "healthy"), 60)
    return {"seen_after_s": took, "reason": c["reason"], "healthy_again_s": back}


@check("D08", "Overloaded: CPU, memory, and requests piling up")
async def d08(lab: Lab) -> dict:
    out = {}
    for label, service, kw, rule in (
            ("cpu", "fraud", {"mode": "cpu", "magnitude": 1.0}, "cpu_pct_high"),
            ("memory", "account", {"mode": "memory"}, "mem_pct_high")):
        await settled(lab)
        await lab.fault(service, "resource_exhaustion", **kw)
        cid = f"bank-{service}"
        out[f"{label}_s"] = await lab.until(label, is_in(lab, cid, "overloaded"), 40)
        c = await comp(lab, cid)
        assert rule in {f["rule"] for f in c["findings"]}, c["findings"]
        assert label != "cpu" or c["display"]["load"] >= 0.9
        await lab.clear()
        await lab.until("healthy", is_in(lab, cid, "healthy"), 60)
    await lab.fault("payment", "resource_exhaustion", mode="slots")
    stop = asyncio.Event()

    async def waiting(i: int):
        await asyncio.sleep(i / 16)
        while not stop.is_set():
            with contextlib.suppress(httpx.HTTPError):
                await lab.http.get(lab.bank("payment", "/internal/payments/none"),
                                   headers=lab.internal)
    jobs = [asyncio.create_task(waiting(i)) for i in range(16)]

    async def queue_reported():  # shed requests also make it fail; the queue must still be named
        c = await comp(lab, "bank-payment")
        return "queue" in {f["rule"] for f in c["findings"]} and c["status"] in (
            "overloaded", "failed")
    out["queue_s"] = await lab.until("queue", queue_reported, 30)
    stop.set()
    await asyncio.gather(*jobs)
    await lab.clear()
    return out


@check("D09", "Unknown: a hung service is neither failed nor healthy")
async def d09(lab: Lab) -> dict:
    begin = datetime.now(UTC)
    pid = await asyncio.to_thread(bank_pid, lab, "notification")
    freeze(lab, "notification", pid, True)
    try:
        took = await lab.until("unknown", is_in(lab, "bank-notification", "unknown"), 20)
        await asyncio.sleep(6)
        c = await comp(lab, "bank-notification")
        assert c["status"] == "unknown" and c["metrics"] == {}, c
        out = await link(lab, "notification", "customer")
        assert out["status"] == "unknown"
    finally:
        freeze(lab, "notification", pid, False)
    await lab.until("healthy", is_in(lab, "bank-notification", "healthy"), 30)
    await asyncio.sleep(2)
    path = [x["to_status"] for x in await transitions(lab, "bank-notification", begin)]
    assert "failed" not in path, path
    return {"unknown_after_s": took, "path": path}


# ------------------------------------------------------ databases, dependencies, network
@check("D10", "Bank database: its failure and exhaustion are distinct from the service in front")
async def d10(lab: Lab) -> dict:
    out = {}
    await lab.fault("database", "db_connection_failure", mode="refuse")
    out["refused_s"] = await lab.until("store failed", is_in(lab, "store-bank", "failed"), 30)
    svc = await comp(lab, "bank-database")
    assert svc["status"] != "failed" or svc["root_causes"] == ["store-bank"], svc["status"]
    await lab.clear()
    await settled(lab)
    await lab.fault("database", "db_connection_failure", mode="exhaust", magnitude=0.9)
    out["pool_7_of_8_s"] = await lab.until("pool warning",
                                           is_in(lab, "store-bank", "warning", "overloaded"), 30)
    c = await comp(lab, "store-bank")
    assert c["metrics"]["db_pool_in_use"] >= 7 and c["metrics"]["db_pool_size"] == 8, c["metrics"]
    await lab.clear()
    await settled(lab)
    await ctl(lab, "kill", "database")
    await lab.until("service failed", is_in(lab, "bank-database", "failed"), 15)
    out["store_while_service_is_down"] = (await comp(lab, "store-bank"))["status"]
    await ctl(lab, "start", "database")
    assert out["store_while_service_is_down"] == "unknown"
    return out


@check("D11", "Dependencies: one crash under load, one root cause, network not blamed")
async def d11(lab: Lab) -> dict:
    load = await traffic(lab, 40)
    await asyncio.sleep(15)
    await ctl(lab, "kill", "database")
    await asyncio.sleep(12)
    t = await twin(lab)
    by = {c["id"]: c for c in t["components"]}
    affected = {c: by[c]["status"] for c in by if c.startswith("bank-") and c != "bank-database"
                and by[c]["status"] != "healthy"}
    causes = {tuple(by[c]["root_causes"]) for c in affected}
    assert causes == {("bank-database",)}, {c: by[c]["root_causes"] for c in affected}
    expected = {f"bank-{a}" for a, b in EDGES if b == "database"}
    assert expected <= set(affected), (expected, affected)
    assert by["net-bank"]["status"] in ("healthy", "unknown"), by["net-bank"]["reason"]
    dead = [k["id"] for k in t["links"] if k["target"] == "bank-database" and k["kind"] == "calls"
            and k["status"] == "failed"]
    assert t["summary"]["overall"] == "failed"
    dep = await lab.get(f"{V2}/components/bank-transaction/dependencies")
    assert dep["root_causes"] == ["bank-database"]
    await ctl(lab, "start", "database")
    await load
    await settled(lab, 120)
    return {"affected": affected, "root_cause": "bank-database", "failed_connections": len(dead),
            "network": by["net-bank"]["status"]}


@check("D12", "Connections fail and recover with their target; the circuit breaker is visible")
async def d12(lab: Lab) -> dict:
    load = await traffic(lab, 45)
    await asyncio.sleep(15)
    begin = datetime.now(UTC)
    await ctl(lab, "kill", "payment")

    async def failed():
        return (await link(lab, "transaction", "payment"))["status"] == "failed"
    took = await lab.until("link failed", failed, 25)
    k = await link(lab, "transaction", "payment")
    await ctl(lab, "start", "payment")

    async def fine():
        return (await link(lab, "transaction", "payment"))["status"] == "healthy"
    back = await lab.until("link healthy", fine, 50)
    await load
    await asyncio.sleep(2)
    path = [x["to_status"] for x in await transitions(
        lab, "bank-transaction->bank-payment", begin)]
    # Failures fill the 10 s window gradually, so it may pass through "warning" on the way.
    assert path.index("failed") < path.index("recovering") and path[-1] == "healthy", path
    return {"failed_after_s": took, "circuit": k["metrics"].get("circuit"), "path": path,
            "healthy_again_s": back}


# --------------------------------------------------------------------- synchronisation
@check("D13", "Chaos: random faults against a known truth; the twin must agree every time")
async def d13(lab: Lab) -> dict:
    rng = random.Random(2026)
    truth = dict.fromkeys(SERVICES, "up")
    pids: dict[str, int] = {}
    mismatches, checks, actions = [], 0, Counter()
    for step in range(22):
        s = rng.choice(SERVICES)
        if truth[s] == "up":
            action = rng.choice(["crash", "downtime", "hang"])
            if action == "crash":
                await ctl(lab, "kill", s)
            elif action == "downtime":
                await lab.fault(s, "downtime", 300)
            else:
                pids[s] = await asyncio.to_thread(bank_pid, lab, s)
                freeze(lab, s, pids[s], True)
            truth[s] = action
        else:
            action = "repair"
            if truth[s] == "crash":
                await ctl(lab, "start", s)
            elif truth[s] == "downtime":
                await lab.http.delete(lab.bank(s, "/control/faults"), headers=lab.internal)
            else:
                freeze(lab, s, pids[s], False)
            truth[s] = "up"
        actions[action] += 1
        await asyncio.sleep(7)
        by = {c["id"]: c for c in (await twin(lab))["components"]}
        for n, real in truth.items():
            seen = by[f"bank-{n}"]["status"]
            ok = {"crash": seen == "failed", "downtime": seen == "failed", "hang":
                  seen == "unknown", "up": seen not in ("failed", "unknown")}[real]
            checks += 1
            if not ok:
                mismatches.append((step, n, real, seen, by[f"bank-{n}"]["reason"]))
    for s, real in truth.items():
        if real == "crash":
            await ctl(lab, "start", s)
        elif real == "hang":
            freeze(lab, s, pids[s], False)
    await lab.clear()
    assert not mismatches, mismatches
    await settled(lab, 150)
    return {"actions": dict(actions), "comparisons": checks, "mismatches": 0}


@check("D14", "WebSocket: 40 clients rebuild the twin from updates alone, during faults")
async def d14(lab: Lab) -> dict:
    mirrors = [Mirror() for _ in range(40)]
    tasks = [asyncio.create_task(m.follow()) for m in mirrors]
    load = await traffic(lab, 40)
    await asyncio.sleep(8)
    await ctl(lab, "kill", "account")
    await asyncio.sleep(8)
    await ctl(lab, "start", "account")
    await lab.fault("fraud", "downtime", 6)
    await load
    await asyncio.sleep(8)
    for _ in range(40):  # compare at a moment when every mirror has the same version
        t = await twin(lab)
        if all(m.meta.get("version") == t["meta"]["version"] for m in mirrors):
            break
        await asyncio.sleep(0.2)
    for m in mirrors:
        assert m.meta["version"] == t["meta"]["version"]
        assert m.components == {c["id"]: c for c in t["components"]}
        assert m.links == {k["id"]: k for k in t["links"]}
        assert m.lost == 0 and m.deltas >= 12
        assert {"failed", "recovering"} <= {x["to_status"] for x in m.transitions
                                            if x["object_id"] == "bank-account"}
    for x in tasks:
        x.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return {"clients": len(mirrors), "updates_each": mirrors[0].deltas, "lost": 0,
            "median_update_kb": round(statistics.median(mirrors[0].sizes) / 1024, 1)}


@check("D15", "Update delay: from measurement to twin")
async def d15(lab: Lab) -> dict:
    before = await lab.get(f"{V2}/sync")
    lags, start = [], time.monotonic()
    while time.monotonic() - start < 30:
        lags.append((await lab.get(f"{V2}/sync"))["lag_s"])
        await asyncio.sleep(0.7)
    after = await lab.get(f"{V2}/sync")
    applied = after["observations_applied"] - before["observations_applied"]
    received = after["messages_received"] - before["messages_received"]
    assert 13 <= applied <= 17 and applied == received, (applied, received)
    assert max(lags) < 1.0 and after["messages_rejected"].get("error", 0) == 0, after
    return {"median_s": statistics.median(lags), "worst_s": max(lags),
            "observations_in_30_s": applied}


@check("D16", "Monitoring stopped: everything becomes unknown; afterwards nothing is made up")
async def d16(lab: Lab) -> dict:
    await ctl(lab, "kill", "payment")
    await lab.until("payment failed", is_in(lab, "bank-payment", "failed"), 15)
    mirror = Mirror()
    task = asyncio.create_task(mirror.follow())
    await asyncio.sleep(1)
    await asyncio.to_thread(lab.docker, "stop", "-t", "2", lab.container("monitoring"))
    try:
        async def blind():
            t = await twin(lab)
            return t["meta"]["sync"] == "stale" and all(
                c["status"] == "unknown" for c in t["components"])
        took = await lab.until("everything unknown", blind, 25)
        await ctl(lab, "start", "payment")
    finally:
        await asyncio.to_thread(lab.docker, "start", lab.container("monitoring"))
    back = await lab.until("payment recovering", is_in(lab, "bank-payment", "recovering"), 40)
    assert (await comp(lab, "bank-account"))["status"] == "healthy"
    await settled(lab)
    await asyncio.sleep(1)
    task.cancel()
    seen = [x["to_status"] for x in mirror.transitions if x["object_id"] == "bank-payment"]
    assert seen[:3] == ["unknown", "recovering", "healthy"] and mirror.lost == 0, seen
    return {"unknown_after_s": took, "live_again_s": back, "payment": seen}


@check("D17", "Prometheus stopped: monitoring is alive but blind, and the twin knows")
async def d17(lab: Lab) -> dict:
    await asyncio.to_thread(lab.docker, "stop", "-t", "2", lab.container("prometheus"))
    try:
        async def blind():
            t = await twin(lab)
            s = await lab.get(f"{V2}/sync")
            return t["meta"]["sync"] == "stale" and s["connected"] \
                and s["messages_rejected"].get("not_fresh", 0) > 0
        took = await lab.until("blind", blind, 40)
    finally:
        await asyncio.to_thread(lab.docker, "start", lab.container("prometheus"))
    await settled(lab)
    return {"unknown_after_s": took}


# ----------------------------------------------------------------------------- history
@check("D18", "History: past states are reconstructed exactly, with their metrics")
async def d18(lab: Lab) -> dict:
    load = await traffic(lab, 60)
    await asyncio.sleep(14)
    moments = []

    async def note(label: str) -> None:
        t = await twin(lab)
        moments.append((label, datetime.fromisoformat(t["meta"]["generated_at"]),
                        {c["id"]: c["status"] for c in t["components"]},
                        {k["id"]: k["status"] for k in t["links"]}))
    await note("before")
    begin = moments[0][1]
    await ctl(lab, "kill", "fraud")
    await asyncio.sleep(9)
    await note("failed")
    await ctl(lab, "start", "fraud")
    await lab.until("recovering", is_in(lab, "bank-fraud", "recovering"), 15)
    await asyncio.sleep(1)
    await note("recovering")
    await lab.until("healthy", is_in(lab, "bank-fraud", "healthy"), 40)
    await load
    await asyncio.sleep(3)
    for label, at, components, links in moments:
        past = await lab.get(f"{V2}/history/state", params={"at": at.isoformat()})
        assert {c["id"]: c["status"] for c in past["components"]} == components, label
        assert {k["id"]: k["status"] for k in past["links"]} == links, label
        assert past["origin"] == "measured"
        account = next(c for c in past["components"] if c["id"] == "bank-account")
        assert account["metrics"]["rps"] > 0 and "cpu_pct" in account["metrics"], label
    line = await lab.get(f"{V2}/history/timeline/bank-fraud",
                         params={"start": begin.isoformat()})
    path = [x["status"] for x in line["intervals"]]
    assert path[:4] == ["healthy", "failed", "recovering", "healthy"], path
    span = (datetime.fromisoformat(line["end"]) - begin).total_seconds()
    assert abs(sum(x["seconds"] for x in line["intervals"]) - span) < 1
    assert abs(sum(line["share"].values()) - 1) < 0.01
    ancient = await lab.http.get(f"{V2}/history/state", params={"at": "2020-01-01T00:00:00Z"})
    assert ancient.status_code == 404
    return {"moments_reconstructed": len(moments), "timeline": path,
            "seconds": [x["seconds"] for x in line["intervals"][:4]]}


@check("D19", "Database stopped for 25 s: the twin continues and no history is lost")
async def d19(lab: Lab) -> dict:
    begin = datetime.now(UTC)
    await asyncio.to_thread(lab.docker, "stop", "-t", "3", lab.container("postgres"))
    try:
        await asyncio.sleep(4)
        await lab.fault("fraud", "downtime", 8)  # happens while nothing can be stored
        took = await lab.until("fraud failed", is_in(lab, "bank-fraud", "failed"), 15)
        r = await lab.http.get(f"{V2}/history/transitions")
        assert r.status_code == 503, r.status_code
        await asyncio.sleep(18)
        s = await lab.get(f"{V2}/sync")
        assert s["state"] == "live" and s["history"]["buffered_rows"] > 0, s["history"]
    finally:
        await asyncio.to_thread(lab.docker, "start", lab.container("postgres"))

    async def written():
        h = (await lab.get(f"{V2}/sync"))["history"]
        return h["connected"] and h["buffered_rows"] == 0
    await lab.until("history written", written, 60)
    path = [x["to_status"] for x in await transitions(lab, "bank-fraud", begin)]
    h = (await lab.get(f"{V2}/sync"))["history"]
    assert path[:2] == ["failed", "recovering"] and not h["dropped_rows"], (path, h)
    assert not h["rejected_rows"], h
    await settled(lab, 120)
    return {"noticed_without_database_s": took, "recorded_afterwards": path, "lost": 0}


@check("D20", "Twin restarted: it comes back by itself, and its absence is visible in history")
async def d20(lab: Lab) -> dict:
    old = (await twin(lab))["meta"]
    stopped = datetime.now(UTC)
    await asyncio.to_thread(lab.docker, "stop", "-t", "5", lab.container("backend"))
    await ctl(lab, "kill", "fraud")  # changes while the twin is away
    await asyncio.sleep(85)
    await asyncio.to_thread(lab.docker, "start", lab.container("backend"))
    start = time.monotonic()
    back = await lab.until("fraud failed after restart", is_in(lab, "bank-fraud", "failed"), 40)
    new = (await twin(lab))["meta"]
    assert new["run_id"] != old["run_id"] and new["sync"] == "live"
    await ctl(lab, "start", "fraud")
    await settled(lab)
    await asyncio.sleep(2)
    middle = stopped + timedelta(seconds=82)
    r = await lab.http.get(f"{V2}/history/state", params={"at": middle.isoformat()})
    assert r.status_code == 404, "the twin was off: it must not claim to know"
    line = await lab.get(f"{V2}/history/timeline/bank-fraud", params={
        "start": (stopped - timedelta(seconds=20)).isoformat()})
    path = [x["status"] for x in line["intervals"]]
    assert "untracked" in path and path.index("untracked") < path.index("failed"), path
    before = await lab.get(f"{V2}/history/state", params={
        "at": (stopped - timedelta(seconds=5)).isoformat()})
    assert before["run_id"] == old["run_id"]
    return {"synchronised_after_restart_s": back, "timeline": path,
            "total_s": round(time.monotonic() - start, 1)}


# -------------------------------------------------------------------------- separation
@check("D21", "Separation: the twin holds no key, changes nothing, and never calls the bank")
async def d21(lab: Lab) -> dict:
    env = lab.docker("exec", lab.container("backend"), "env")
    assert lab.key not in env
    for name, pw in PW.items():
        assert (pw in env) == (name == "TWIN_DB_PASSWORD"), name
    refused = 0
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("", "/components/bank-account", "/links/bank-account->bank-database", "/sync"):
            r = await lab.http.request(method, V2 + path, json={"status": "failed"})
            assert r.status_code == 405, (method, path, r.status_code)
            refused += 1
    async with websockets.connect(WS, max_size=2**23, origin="http://localhost:8080") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "set", "components": ["bank-account"],
                                  "status": "failed"}))
        with contextlib.suppress(websockets.ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 1003
    assert (await comp(lab, "bank-account"))["status"] == "healthy"
    # With nobody else using the bank, any request it sees would be the twin's.
    await asyncio.sleep(14)
    a = {}
    for s in SERVICES:
        text = (await lab.http.get(lab.bank(s, "/metrics"))).text
        a[s] = sum(float(x.rsplit(" ", 1)[1]) for x in text.splitlines()
                   if x.startswith("bank_service_requests_total"))
    await asyncio.sleep(20)
    for s in SERVICES:
        text = (await lab.http.get(lab.bank(s, "/metrics"))).text
        b = sum(float(x.rsplit(" ", 1)[1]) for x in text.splitlines()
                if x.startswith("bank_service_requests_total"))
        assert b == a[s], f"{s} received {b - a[s]} requests while nobody used the bank"
    conn = await lab.db("twin_writer", "TWIN_DB_PASSWORD")
    attempts = ["UPDATE twin_transition SET reason = ''", "DELETE FROM twin_snapshot",
                "TRUNCATE twin_transition", "INSERT INTO metric_sample(ts,node_id) VALUES "
                "(now(),'x')", "UPDATE metric_sample SET cpu_pct = 0", "SELECT * FROM app_user",
                "CREATE TABLE evil(i int)", "SELECT prune_twin_history(0)",
                "SELECT ensure_metric_partitions(1)"]
    for sql in attempts:
        try:
            await conn.execute(sql)
        except (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError):
            continue
        raise AssertionError(f"allowed: {sql}")
    await conn.close()
    return {"write_requests_refused": refused, "requests_sent_to_bank": 0,
            "database_writes_refused": len(attempts)}


@check("D22", "The twin sees the bank; the bank does not notice the twin")
async def d22(lab: Lab) -> dict:
    """Stopping or overloading the twin must have no effect on the real system."""
    async with steady(lab, lookup(lab), 10) as results:
        await asyncio.sleep(6)
        n = len(results)
        await asyncio.to_thread(lab.docker, "kill", lab.container("backend"))
        await asyncio.sleep(6)
        during = results[n:]
        await asyncio.to_thread(lab.compose, "up", "-d", "backend")
        await asyncio.sleep(4)
    assert all(r.status_code == 200 for _, r in results), "a bank request failed"
    assert len(during) >= 50
    await settled(lab)
    return {"bank_requests_while_twin_was_dead": len(during), "failed": 0}


# ------------------------------------------------------------------------------ runner
async def main() -> int:
    only = set(sys.argv[1:])
    unknown = only - {c for c, _, _ in CHECKS}
    if unknown:
        sys.exit(f"unknown check(s): {sorted(unknown)}")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "twin.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in {
            "POSTGRES_USER": "simucore", "GRAFANA_ADMIN_USER": "admin", **PW}.items()),
            encoding="utf-8")
        lab = Lab(env_file)
        if any(r["live"] != "stopped" for r in json.loads(lab.ctl("status", "--json"))):
            sys.exit("The bank is running. Stop it first: bankctl.py down")
        if lab.docker("ps", "-q", "--filter", "publish=8200"):
            sys.exit("A stack is already running; stop it first.")
        try:
            lab.compose("up", "-d", "--build")
            await asyncio.to_thread(lab.ctl, "up")
            lab.load_key()
            await settled(lab, 150)
            print("environment ready", flush=True)
            for code, title, fn in CHECKS:
                if only and code not in only:
                    continue
                t, facts, error = time.monotonic(), {}, None
                try:
                    await settled(lab, 120)
                    facts = await fn(lab)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:900]}"
                    if not isinstance(exc, AssertionError):
                        traceback.print_exc()
                    await lab.clear()
                    with contextlib.suppress(RuntimeError):
                        lab.compose("up", "-d")
                        await asyncio.to_thread(lab.ctl, "up")
                results.append({"code": code, "title": title, "passed": error is None,
                                "seconds": round(time.monotonic() - t, 1), "facts": facts,
                                "error": error})
                print(f"{'PASS' if not error else 'FAIL'} {code} {title}\n     "
                      f"{error or json.dumps(facts)}", flush=True)
        finally:
            await lab.http.aclose()
            lab.compose("down", "--volumes")
            lab.ctl("down")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if not only:
        out = ROOT / "backend" / "results"
        name = "twin_core_docker" if DOCKER else "twin_core"
        where = "Docker containers" if DOCKER else "local processes"
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        (out / f"{name}.json").write_text(json.dumps(
            {"run_at": stamp, "bank": where, "passed": passed, "total": len(results),
             "results": results}, indent=2), encoding="utf-8", newline="\n")
        lines = ["# Digital twin core results", "", f"Run at {stamp}, bank as {where}. **{passed} "
                 f"of {len(results)} passed.** Generated by `live/twin_core_check.py`.", "",
                 "| | Check | Result | Measured |", "|---|---|---|---|"]
        lines += [f"| {r['code']} | {r['title']} | {'pass' if r['passed'] else '**FAIL**'} | "
                  f"{(r['error'] or json.dumps(r['facts'])).replace('|', '/')} |" for r in results]
        (out / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    if not shutil.which("docker"):
        sys.exit("docker is not on the PATH")
    sys.exit(asyncio.run(main()))
