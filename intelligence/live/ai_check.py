"""Checks the complete AI pipeline on the running stack, with real faults in the simulated bank.

    python live/ai_check.py [A01 A05 ...]

Needs the Docker stack; starts and stops the simulated bank itself. Results: results/pipeline.{json,md}.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import psutil
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "intelligence"))
sys.path.insert(0, str(ROOT / "intelligence" / "live"))

import record  # noqa: E402
import workload as wl  # noqa: E402

from simucore_ai import labels  # noqa: E402

AI, TWIN, MON = "http://127.0.0.1:8300", "http://127.0.0.1:8000", "http://127.0.0.1:8200"
DOCKER = os.environ.get("DOCKER", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
CHECKS: list = []


def check(code: str, title: str):
    def add(f):
        CHECKS.append((code, title, f))
        return f
    return add


class Lab(record.Lab):
    def __init__(self) -> None:
        super().__init__()
        self.http = httpx.AsyncClient(trust_env=False, timeout=8)

    async def get(self, url: str, **params):
        r = await self.http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def insights(self) -> dict:
        return await self.get(f"{AI}/api/v1/ai/insights")

    async def opinion(self, service: str) -> dict | None:
        m = await self.insights()
        return next((s for s in m["services"] if s["service"] == service), None) if m["fresh"] else None

    async def component(self, service: str) -> dict:
        return await self.get(f"{TWIN}/api/v2/twin/components/bank-{service}")

    async def until(self, what: str, f, seconds: float, every: float = 0.3) -> float:
        t0 = time.monotonic()
        last: object = None
        while time.monotonic() - t0 < seconds:
            try:
                if last := await f():
                    return round(time.monotonic() - t0, 1)
            except (httpx.HTTPError, KeyError, StopIteration) as exc:
                last = repr(exc)
            await asyncio.sleep(every)
        raise AssertionError(f"not within {seconds} s: {what} (last: {str(last)[:200]})")

    def docker(self, *args: str) -> str:
        r = subprocess.run([DOCKER, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", check=True)
        return r.stdout.strip()

    async def db(self, role: str) -> asyncpg.Connection:
        env = dict(line.split("=", 1) for line in
                   (ROOT / ".env").read_text(encoding="utf-8").splitlines()
                   if "=" in line and not line.startswith("#"))
        user, password = {"ai": ("ai_writer", env["AI_DB_PASSWORD"]),
                          "owner": (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"]),
                          "twin": ("twin_writer", env["TWIN_DB_PASSWORD"])}[role]
        return await asyncpg.connect(host="127.0.0.1", user=user, password=password,
                                     database="simucore")

    async def calm(self, seconds: float = 120) -> None:
        """Everything works, every opinion is normal, and predictions are possible again."""
        await self.clear()
        await self.do(self.bank.up)

        async def quiet() -> bool:
            m = await self.insights()
            return m["fresh"] and len(m["services"]) == 8 and all(
                s["anomaly"] and not s["open_anomaly"] and not s["warnings"] and len(s["risk"]) == 2
                and all(r["probability"] is not None and r["level"] == "low"
                        for r in s["risk"].values()) for s in m["services"])
        try:
            await self.until("calm", quiet, 75, 1.0)
        except AssertionError:
            # After an overload a service may keep more memory for good: unusual, rightly. The next
            # check needs a bank that is normal, so it gets a fresh one.
            await self.do(self.bank.down)
            await self.do(self.bank.up)
            await self.until("calm after a restart", quiet, seconds, 1.0)


async def leak(lab: Lab, service: str, seconds: float, stop, top: float = 1.0) -> None:
    """Memory grows steadily until ``stop`` says so."""
    t0 = time.monotonic()
    while not await stop():
        x = min(1.0, (time.monotonic() - t0) / seconds)
        await lab.fault(service, "resource_exhaustion", 90, mode="memory",
                        magnitude=0.22 + (top - 0.22) * x)
        await asyncio.sleep(3)
        if time.monotonic() - t0 > seconds + 40:
            return


# ------------------------------------------------------------------------------- checks
@check("A01", "Every service is assessed within seconds, and the twin shows the same opinions")
async def a01(lab: Lab) -> dict:
    await lab.calm(180)
    same = 0
    for _ in range(6):
        m = await lab.insights()
        await asyncio.sleep(0.5)
        t = await lab.get(f"{TWIN}/api/v2/twin/insights")
        if {datetime.fromisoformat(v["assessed_at"]) for v in t.values()} != {
                datetime.fromisoformat(m["assessed_at"])}:
            await asyncio.sleep(0.4)
            continue
        assert len(t) == 8
        for s in m["services"]:
            v = t[s["node_id"]]
            assert v["anomaly"]["score"] == s["anomaly"]["score"], s["service"]
            for r in v["risks"]:
                assert r["probability"] == s["risk"][r["target"]]["probability"]
                same += 1
    assert same >= 16, same
    age = (datetime.now(UTC) - datetime.fromisoformat((await lab.insights())["assessed_at"]))
    st = await lab.get(f"{AI}/api/v1/ai/status")
    return {"services": 8, "values_compared": same, "data_age_s": round(age.total_seconds(), 1),
            "scoring_ms": st["last_scoring_ms"], "model": st["model"]}


@check("A02", "Normal operation for 5 minutes: no predictive warning; false anomalies are counted")
async def a02(lab: Lab) -> dict:
    """A warning in normal operation would be wrong. An anomaly may be right or wrong: in a bank that
    has run for a while, some moments really are unlike the calibration. They are counted, not hidden."""
    await lab.calm()
    start = datetime.now(UTC)
    await asyncio.sleep(300)
    found = await lab.get(f"{AI}/api/v1/ai/anomalies", start=start.isoformat())
    warned = await lab.get(f"{AI}/api/v1/ai/warnings", start=start.isoformat())
    assert not warned, warned
    seconds = sum((datetime.fromisoformat(a["closed_at"] or a["last_seen"])
                   - datetime.fromisoformat(a["opened_at"])).total_seconds() for a in found)
    # Measured, not judged: after hours of running some services really differ from the
    # calibration (docs/12-ai-module.md, "Drift"). The rate is reported in the results.
    share = seconds / (300 * 8)
    return {"seconds": 300, "warnings": 0, "anomalies": len(found),
            "per_service_hour": round(len(found) / (300 * 8 / 3600), 2),
            "share_of_time": round(share, 4),
            "which": [(a["node_id"], a["condition"], round(a["score"], 2)) for a in found]}


@check("A03", "Memory leak: anomaly, then a warning before the failure; the outcome is recorded")
async def a03(lab: Lab) -> dict:
    await lab.calm()
    start, out = datetime.now(UTC), {}
    seen: dict[str, float] = {}
    t0 = time.monotonic()

    async def watch() -> bool:
        s = await lab.opinion("account")
        c = await lab.component("account")
        now = round(time.monotonic() - t0, 1)
        if s:
            if s["open_anomaly"]:
                seen.setdefault("anomaly", now)
                if c["insight"] and c["insight"]["anomaly"] and c["insight"]["anomaly"]["since"]:
                    seen.setdefault("anomaly_in_twin", now)
            for w in s["warnings"]:
                if f"warned_{w['target']}" not in seen:
                    seen[f"warned_{w['target']}"] = now
                    out[f"reasons_{w['target']}"] = [r["text"] for r in w["reasons"][:3]]
                    out[f"status_when_warned_{w['target']}"] = c["status"]
        if c["insight"] and c["insight"]["warnings"]:
            seen.setdefault("warning_in_twin", now)
        if c["status"] == "failed":
            seen.setdefault("failed", now)
        return "failed" in seen
    await leak(lab, "account", 150, watch)
    assert "failed" in seen, seen
    assert seen["anomaly"] < seen["failed"], seen
    assert "warned_failure" in seen or "warned_degradation" in seen, seen
    first = min(v for k, v in seen.items() if k.startswith("warned"))
    assert first < seen["failed"] - 10, seen
    assert out.get("status_when_warned_failure", "healthy") != "failed"
    await lab.clear()

    async def outcome() -> bool:
        w = await lab.get(f"{AI}/api/v1/ai/warnings", node_id="bank-account",
                          start=start.isoformat())
        out["outcomes"] = [(x["target"], x["outcome"], x["lead_s"]) for x in w]
        return any(x["outcome"] == "came_true" for x in w)
    await lab.until("outcome stored", outcome, 20)
    anomalies = await lab.get(f"{AI}/api/v1/ai/anomalies", node_id="bank-account",
                              start=start.isoformat())
    # once memory runs out requests fail, so the condition may end up named "errors"
    assert anomalies and anomalies[-1]["condition"] in ("resources", "errors"), anomalies
    assert "memory" in [c["metric"] for c in anomalies[-1]["contributions"][:2]], anomalies
    return {**seen, "lead_s": round(seen["failed"] - first, 1), **out}


@check("A04", "Crash without any sign: not announced (nobody could), and no opinion about a dead service")
async def a04(lab: Lab) -> dict:
    await lab.calm()
    start = datetime.now(UTC)
    await lab.do(lab.bank.kill, "notification")

    async def gone() -> bool:
        s = await lab.opinion("notification")
        return bool(s) and s["anomaly"] is None and s["risk"] == {} and "down" in s["risk_reason"]
    took = await lab.until("no opinion", gone, 15)
    w = await lab.get(f"{AI}/api/v1/ai/warnings", node_id="bank-notification",
                      start=start.isoformat())
    assert w == [], w
    await lab.do(lab.bank.start, "notification")

    async def recovering() -> bool:
        s = await lab.opinion("notification")
        return bool(s) and bool(s["risk"]) and s["risk"]["failure"]["level"] in ("recovering", "present")
    await lab.until("recovering, no probability", recovering, 60)
    return {"opinion_withdrawn_after_s": took, "warnings_before": 0}


@check("A05", "A leak that is stopped in time: the warning ends and is recorded as not having happened")
async def a05(lab: Lab) -> dict:
    await lab.calm(240)
    start = datetime.now(UTC)

    async def warned() -> bool:
        s = await lab.opinion("payment")
        return bool(s and s["warnings"])
    await leak(lab, "payment", 160, warned)
    assert await warned(), "no warning"
    await lab.clear()

    async def ended() -> bool:
        s = await lab.opinion("payment")
        return bool(s) and not s["warnings"]
    over = await lab.until("warning ends", ended, 90)
    c = await lab.component("payment")
    assert c["status"] != "failed"

    async def judged() -> bool:
        w = await lab.get(f"{AI}/api/v1/ai/warnings", node_id="bank-payment", start=start.isoformat())
        return bool(w) and all(x["outcome"] == "did_not_happen" for x in w)
    took = await lab.until("judged", judged, 150, 2)
    return {"warning_ended_after_s": over, "judged_after_s": took}


@check("A06", "Anomalies of other kinds are found and named: latency, errors, a surge of traffic")
async def a06(lab: Lab) -> dict:
    out = {}

    async def named(service: str, want: set[str]) -> bool:
        s = await lab.opinion(service)
        if s and s["open_anomaly"]:
            out.setdefault(service, []).append(s["open_anomaly"]["condition"])
            return s["open_anomaly"]["condition"] in want
        return False
    await lab.calm(240)
    await lab.fault("fraud", "latency", 60, latency_ms=900, jitter=0.2)
    out["latency_s"] = await lab.until("latency", lambda: named("fraud", {"latency"}), 40)
    await lab.calm(240)
    await lab.fault("database", "db_connection_failure", 40, mode="refuse")
    out["errors_s"] = await lab.until("errors", lambda: named("account", {"errors"}), 40)
    await lab.calm(240)
    record.Varying.current = 70
    try:
        # a surge shows first where most requests go; overload may also make requests fail
        surge = {"traffic", "resources", "latency", "connections", "errors"}
        out["traffic_s"] = await lab.until(
            "traffic", lambda: named("account", surge) or named("transaction", surge), 90)
    finally:
        record.Varying.current = 8
    return out


@check("A07", "The status of a component is never changed by an opinion")
async def a07(lab: Lab) -> dict:
    await lab.calm(240)
    compared = 0

    async def flagged() -> bool:
        s = await lab.opinion("customer")
        return bool(s and (s["open_anomaly"] or s["warnings"]))

    async def stop() -> bool:
        nonlocal compared
        c = await lab.component("customer")
        m = c["metrics"]
        if m.get("mem_pct", 0) < 78 and m.get("error_rate", 0) < 0.02 \
                and all(f["metric"] in (None, "latency_ms", "latency_mean_ms") for f in c["findings"]):
            assert c["status"] in ("healthy", "warning", "recovering"), c["status"]
            compared += await flagged()
        return m.get("mem_pct", 0) >= 70
    await leak(lab, "customer", 120, stop)
    assert compared >= 2, "the service was never flagged while its measurements were fine"
    await lab.clear()
    return {"moments_flagged_by_ai_with_healthy_measurements": compared}


@check("A08", "Database outage: scoring and the twin go on, nothing is lost")
async def a08(lab: Lab) -> dict:
    await lab.calm(240)
    before = (await lab.get(f"{AI}/api/v1/ai/status"))["store"]["written_rows"]
    start = datetime.now(UTC)
    lab.docker("stop", "simucore-postgres-1")
    try:
        await asyncio.sleep(25)
        st = await lab.get(f"{AI}/api/v1/ai/status")
        assert st["state"] == "ok" and not st["store"]["connected"], st
        waiting = st["store"]["buffered_rows"]
        assert waiting > 5, st
        assert (await lab.http.get(f"{AI}/api/v1/ai/anomalies")).status_code == 503
        assert (await lab.component("account"))["insight"]
    finally:
        lab.docker("start", "simucore-postgres-1")

    async def written() -> bool:
        s = (await lab.get(f"{AI}/api/v1/ai/status"))["store"]
        return s["connected"] and s["buffered_rows"] < 3 and s["written_rows"] > before
    took = await lab.until("written", written, 60)
    rows = await lab.get(f"{AI}/api/v1/ai/predictions/bank-account", start=start.isoformat())
    times = sorted({datetime.fromisoformat(r["ts"]).timestamp() for r in rows})
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert gaps and max(gaps) < 15, gaps
    s = (await lab.get(f"{AI}/api/v1/ai/status"))["store"]
    assert s["dropped_rows"] == 0
    return {"rows_waiting": waiting, "written_after_s": took, "largest_gap_s": round(max(gaps), 1)}


@check("A09", "AI service stops: the twin says so, statuses go on; it comes back by itself")
async def a09(lab: Lab) -> dict:
    await lab.calm(240)
    lab.docker("stop", "simucore-intelligence-1")
    try:
        async def absent() -> bool:
            t = await lab.get(f"{TWIN}/api/v2/twin/summary")
            return t["ai"] == "absent" and t["by_status"]["healthy"] >= 14
        took = await lab.until("absent", absent, 20)
        assert (await lab.component("account"))["insight"] is None
    finally:
        lab.docker("start", "simucore-intelligence-1")

    async def back() -> bool:
        return (await lab.get(f"{TWIN}/api/v2/twin/summary"))["ai"] == "live"
    return {"noticed_after_s": took, "back_after_s": await lab.until("back", back, 60)}


@check("A10", "Monitoring stops: opinions expire instead of growing old")
async def a10(lab: Lab) -> dict:
    await lab.calm(240)
    lab.docker("stop", "simucore-monitoring-1")
    try:
        async def silent() -> bool:
            m = await lab.insights()
            return not m["fresh"] and m["services"] == []
        took = await lab.until("no opinions", silent, 25)

        async def twin_too() -> bool:
            return (await lab.get(f"{TWIN}/api/v2/twin/insights")) == {}
        await lab.until("none in the twin", twin_too, 6)
    finally:
        lab.docker("start", "simucore-monitoring-1")

    async def back() -> bool:
        return (await lab.insights())["fresh"]
    return {"expired_after_s": took, "back_after_s": await lab.until("back", back, 60)}


@check("A11", "A restart does not pretend to know: open warnings become 'not known'")
async def a11(lab: Lab) -> dict:
    await lab.calm(240)

    async def warned() -> bool:
        s = await lab.opinion("fraud")
        return bool(s and s["warnings"])
    start = datetime.now(UTC)
    await leak(lab, "fraud", 150, warned)
    assert await warned()
    await asyncio.sleep(2)
    lab.docker("kill", "simucore-intelligence-1")
    await lab.clear()
    lab.docker("start", "simucore-intelligence-1")

    async def unresolved() -> bool:
        w = await lab.get(f"{AI}/api/v1/ai/warnings", node_id="bank-fraud", start=start.isoformat())
        return bool(w) and all(x["outcome"] != "open" for x in w) \
            and any(x["outcome"] == "unresolved" for x in w)
    return {"resolved_after_s": await lab.until("unresolved", unresolved, 60)}


@check("A12", "Stored predictions: complete, every one with its outcome after the horizon, scored honestly")
async def a12(lab: Lab) -> dict:
    db = await lab.db("owner")
    try:
        rows = await db.fetch(
            "SELECT p.target, count(*) n, count(o.prediction_id) done, "
            "avg((p.probability - o.actual_event::int)^2)::float8 brier, "
            "avg(o.error)::float8 stored FROM prediction p LEFT JOIN prediction_outcome o "
            "ON o.prediction_id = p.id WHERE p.predicted_at > now() - interval '40 minutes' "
            "GROUP BY 1")
        doubles = await db.fetchval("SELECT count(*) - count(DISTINCT (predicted_at, node_id, "
                                    "target)) FROM prediction")
        model = await db.fetchrow("SELECT * FROM ml_model WHERE fingerprint = $1",
                                  (await lab.get(f"{AI}/api/v1/ai/status"))["model"])
    finally:
        await db.close()
    assert doubles == 0 and model and json.loads(model["metrics"])["failure"]["xgboost"]["roc_auc"]
    out = {}
    for r in rows:
        assert r["done"] > 100 and abs(r["brier"] - r["stored"]) < 1e-3, dict(r)
        out[r["target"]] = {"stored": r["n"], "with_outcome": r["done"], "brier": round(r["brier"], 4)}
    assert set(out) == set(labels.TARGETS)
    p = await lab.get(f"{AI}/api/v1/ai/performance", minutes=40)
    assert p["history"]["failure"]["predictions"] > 100
    return out


@check("A13", "Its database role can add its own rows and nothing else")
async def a13(lab: Lab) -> dict:
    db = await lab.db("ai")
    refused = []
    try:
        for sql in ("DELETE FROM prediction", "UPDATE prediction SET probability = 0",
                    "DELETE FROM anomaly_event", "UPDATE prediction_outcome SET actual_event = true",
                    "SELECT * FROM metric_sample LIMIT 1", "SELECT * FROM twin_transition LIMIT 1",
                    "INSERT INTO metric_sample(ts, node_id) VALUES (now(), 'x')",
                    "DROP TABLE prediction", "UPDATE ml_model SET metrics = '{}'",
                    "CREATE TABLE x (a int)", "TRUNCATE prediction_warning"):
            with contextlib.suppress(asyncpg.PostgresError):
                await db.execute(sql)
                raise AssertionError(f"allowed: {sql}")
            refused.append(sql)
        bad = 0
        for sql in ("INSERT INTO prediction_warning (id, node_id, target, opened_at, horizon_s, "
                    "probability, peak) VALUES (gen_random_uuid(), 'x', 'failure', now(), 120, 7, 1)",
                    "INSERT INTO prediction_warning (id, node_id, target, opened_at, horizon_s, "
                    "probability, peak) VALUES (gen_random_uuid(), 'x', 'lunch', now(), 120, 0.1, 1)",
                    "INSERT INTO anomaly_event (node_id, score, severity) VALUES ('x', 2, 'high')"):
            with contextlib.suppress(asyncpg.IntegrityConstraintViolationError):
                await db.execute(sql)
                raise AssertionError(f"accepted: {sql}")
            bad += 1
    finally:
        await db.close()
    return {"refused": len(refused), "impossible_rows_refused": bad}


@check("A14", "Delay: from the measurement to the opinion in the twin")
async def a14(lab: Lab) -> dict:
    delays = []
    async with websockets.connect("ws://127.0.0.1:8000/ws/v2/twin", max_size=2**23) as ws:
        seen = set()
        t0 = time.monotonic()
        while len(delays) < 12 and time.monotonic() - t0 < 60:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            for c in m["components"]:
                i = c["insight"]
                if i and i["assessed_at"] not in seen:
                    seen.add(i["assessed_at"])
                    delays.append((datetime.now(UTC) - datetime.fromisoformat(
                        i["assessed_at"])).total_seconds())
                    break
    assert len(delays) >= 6, delays
    delays.sort()
    assert delays[len(delays) // 2] < 1.5, delays
    return {"median_s": round(delays[len(delays) // 2], 2), "worst_s": round(delays[-1], 2)}


@check("A15", "40 listeners and nonsense do not disturb it")
async def a15(lab: Lab) -> dict:
    async def listen(n: int) -> int:
        async with websockets.connect("ws://127.0.0.1:8300/ws/insights", max_size=2**22) as ws:
            if n % 4 == 0:
                await ws.send("{nonsense")
            got = 0
            with contextlib.suppress(TimeoutError, websockets.ConnectionClosed):
                async with asyncio.timeout(14):
                    while True:
                        await ws.recv()
                        got += 1
            return got
    got = await asyncio.gather(*(listen(i) for i in range(40)))
    assert min(got) >= 4, got
    with contextlib.suppress(Exception):
        async with websockets.connect("ws://127.0.0.1:8300/ws/insights",
                                      additional_headers={"Origin": "http://evil.example"}) as ws:
            await ws.recv()
            raise AssertionError("another web page may listen")
    st = await lab.get(f"{AI}/api/v1/ai/status")
    assert st["last_scoring_ms"] < 500, st
    for method in ("POST", "PUT", "DELETE"):
        r = await lab.http.request(method, f"{AI}/api/v1/ai/insights")
        assert r.status_code == 405
    return {"listeners": 40, "fewest_messages": min(got), "scoring_ms": st["last_scoring_ms"]}


@check("A16", "Hung service: no score, no prediction, no false alarm about it")
async def a16(lab: Lab) -> dict:
    await lab.calm(240)
    start = datetime.now(UTC)
    pid = next(r["pid"] for r in await lab.do(lab.bank.status) if r["service"] == "notification")
    psutil.Process(pid).suspend()
    try:
        async def none() -> bool:
            s = await lab.opinion("notification")
            return bool(s) and s["anomaly"] is None and s["risk"] == {}
        took = await lab.until("no opinion", none, 20)
        await asyncio.sleep(6)
        found = await lab.get(f"{AI}/api/v1/ai/anomalies", node_id="bank-notification",
                              start=start.isoformat())
        assert found == [], found
    finally:
        psutil.Process(pid).resume()
    return {"withdrawn_after_s": took}


async def main() -> None:
    wanted = set(sys.argv[1:])
    lab = Lab()
    if any(r["live"] != "stopped" for r in lab.bank.status()):
        sys.exit("The bank is running. Stop it first: bankctl.py down")
    await lab.do(lab.bank.up)
    listener = asyncio.create_task(lab.listen())
    record.Varying.current = 8
    w = wl.Workload(lab.bank.key, 5)
    await w.warm_up()
    driver = asyncio.create_task(w.drive(record.Varying(8.0, wl.EVERYDAY), 8.0, 10**7))
    results = []
    try:
        await asyncio.sleep(40)
        for code, title, f in CHECKS:
            if wanted and code not in wanted:
                continue
            t0 = time.monotonic()
            error, measured = None, None
            try:
                measured = await f(lab)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:600]
                record.Varying.current = 8
                await lab.clear()
            results.append({"code": code, "title": title, "passed": error is None, "error": error,
                            "measured": measured, "seconds": round(time.monotonic() - t0)})
            print(f"{'pass' if not error else 'FAIL'}  {code}  {title}\n      "
                  f"{error or json.dumps(measured)}", flush=True)
            w.samples.clear()
            w.acknowledged.clear()
    finally:
        driver.cancel()
        listener.cancel()
        await w.http.aclose()
        await lab.http.aclose()
        await lab.do(lab.bank.down)
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if not wanted:
        out = ROOT / "intelligence" / "results"
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        (out / "pipeline.json").write_text(json.dumps(
            {"run_at": stamp, "passed": passed, "total": len(results), "results": results},
            indent=1) + "\n", encoding="utf-8", newline="\n")
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
        (out / "pipeline.md").write_text("\n".join([
            "# AI pipeline: results on the running system", "",
            f"Run at {stamp} on the Docker stack and the real simulated bank. **{passed} of "
            f"{len(results)} passed.** Generated by `live/ai_check.py`.", "",
            "| | Check | Result | Measured |", "|---|---|---|---|",
            *(f"| {r['code']} | {cell(r['title'])} | {'pass' if r['passed'] else 'FAIL'} | "
              f"{cell(r['error'] or json.dumps(r['measured']))} |" for r in results), ""]),
            encoding="utf-8", newline="\n")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
