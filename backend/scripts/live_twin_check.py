"""End-to-end check of the live twin: real bank processes, real backend, real WebSocket.

    cd backend
    .venv\\Scripts\\python scripts\\live_twin_check.py      # the bank must be down

Writes results to backend/results/live_twin.{json,md}. Starts its own backend on port 8010.
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

import httpx
import websockets

BACKEND = Path(__file__).resolve().parents[1]
BANK = BACKEND.parent / "banking-sim"
BANK_PY = BANK / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
PORT = 8010
API = f"http://127.0.0.1:{PORT}/api/v1"
SERVICES = ["authentication", "customer", "account", "transaction", "payment", "fraud",
            "notification", "database"]
BAD = {"degraded", "critical"}


def ctl(*args: str) -> str:
    r = subprocess.run([str(BANK_PY), "bankctl.py", *args], cwd=BANK, capture_output=True,
                       text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"bankctl {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def bank_pid(service: str) -> int:
    rows = json.loads(ctl("status", "--json"))
    return next(r["pid"] for r in rows if r["service"] == service)


def set_frozen(pid: int, frozen: bool) -> None:
    """Suspend or resume a process (psutil lives in the bank's environment)."""
    verb = "suspend" if frozen else "resume"
    subprocess.run([str(BANK_PY), "-c", f"import psutil;psutil.Process({pid}).{verb}()"],
                   check=True)


class Twin:
    """A WebSocket client that keeps the current state and everything that changed."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.history: list[tuple[float, str, str]] = []
        self.snapshot: dict = {}
        self.changed = asyncio.Event()

    async def follow(self) -> None:
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/twin", max_size=2**22) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "snapshot":
                    self.snapshot = msg
                for s in msg["states"]:
                    name = s["node_id"].removeprefix("bank-")
                    self.state[name] = s["status"]
                    self.history.append((time.monotonic(), name, s["status"]))
                self.changed.set()

    async def until(self, want, timeout_s: float, what: str) -> float:
        """Seconds until ``want(self.state)`` holds."""
        start = time.monotonic()
        while not want(self.state):
            left = timeout_s - (time.monotonic() - start)
            if left <= 0:
                raise AssertionError(f"{what}: not within {timeout_s}s, state={self.state}")
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), left)
        return round(time.monotonic() - start, 2)

    async def all_healthy(self, timeout_s: float = 30) -> float:
        return await self.until(
            lambda st: len(st) == 8 and set(st.values()) == {"healthy"}, timeout_s, "all healthy")

    def seen(self, since: float) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {s: set() for s in SERVICES}
        for t, name, status in self.history:
            if t >= since:
                out[name].add(status)
        return out


async def load(seconds: float, rps: float = 10) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        str(BANK_PY), "bankctl.py", "workload", "normal", "--rps", str(rps), "--duration",
        str(seconds), cwd=BANK, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)


async def call(*args: str) -> str:
    return await asyncio.to_thread(ctl, *args)


# ------------------------------------------------------------------------------ checks
async def c_source(tw: Twin) -> dict:
    await tw.all_healthy()
    assert tw.snapshot["source"] == "bank_services"
    assert len(tw.snapshot["topology"]["edges"]) == 14
    return {"nodes": 8, "edges": 14, "source": tw.snapshot["source"]}


async def c_false_alarms(tw: Twin) -> dict:
    """Normal traffic must not turn anything yellow or red."""
    warm = await load(15)
    await warm.wait()
    start = time.monotonic()
    job = await load(60)
    await job.wait()
    ticks = [(n, s) for t, n, s in tw.history if t >= start]
    alarms = [(n, s) for n, s in ticks if s != "healthy"]
    facts = {"node_ticks": len(ticks), "false_alarms": len(alarms),
             "by_node": {n: sum(a[0] == n for a in alarms) for n in {a[0] for a in alarms}}}
    assert len(alarms) <= 0.02 * len(ticks), facts
    return facts


async def c_kill_each(tw: Twin) -> dict:
    down, up = {}, {}
    for s in SERVICES:
        await tw.all_healthy()
        mark = time.monotonic()
        kill = asyncio.create_task(call("kill", s))
        down[s] = await tw.until(lambda st, s=s: st[s] == "down", 8, f"{s} down")
        await kill
        others = {n: v for n, v in tw.seen(mark).items() if n != s and "down" in v}
        assert not others, f"killing {s} also marked {others} down"
        await call("start", s)
        up[s] = await tw.until(lambda st, s=s: st[s] == "healthy", 8, f"{s} healthy")
    return {"seconds_to_down_including_the_kill_command": down, "slowest_down": max(down.values()),
            "slowest_recovery": max(up.values())}


async def c_latency_of_detection(tw: Twin) -> dict:
    """Pure detection time: the clock starts when the process dies, not when a command starts."""
    times = []
    for _ in range(5):
        await tw.all_healthy()
        pid = await asyncio.to_thread(bank_pid, "customer")
        os.kill(pid, 9)
        times.append(await tw.until(lambda st: st["customer"] == "down", 5, "customer down"))
        await call("start", "customer")
    assert max(times) <= 3.0, times
    return {"seconds": times, "target": 3.0}


async def c_soft_downtime(tw: Twin) -> dict:
    await tw.all_healthy()
    await call("fault", "fraud", "downtime", "--duration", "20")
    t = await tw.until(lambda st: st["fraud"] == "down", 4, "fraud down")
    await call("clear", "fraud")
    return {"down_after": t, "healthy_after": await tw.all_healthy(8)}


async def c_frozen(tw: Twin) -> dict:
    """A hung process still accepts connections. The twin must say unknown, not down."""
    await tw.all_healthy()
    pid, mark = await asyncio.to_thread(bank_pid, "notification"), time.monotonic()
    set_frozen(pid, True)
    try:
        t = await tw.until(lambda st: st["notification"] == "unknown", 10, "unknown")
        assert "down" not in tw.seen(mark)["notification"]
        assert all(v == "healthy" for n, v in tw.state.items() if n != "notification"), tw.state
    finally:
        set_frozen(pid, False)
    return {"unknown_after": t, "healthy_after": await tw.all_healthy(10)}


async def c_db_latency(tw: Twin) -> dict:
    await tw.all_healthy()
    job = await load(22, rps=6)
    await asyncio.sleep(6)
    await call("fault", "database", "latency", "--latency-ms", "400", "--duration", "14")
    t = await tw.until(lambda st: st["database"] in BAD and st["account"] in BAD, 12,
                       "database and account slow")
    facts = {"visible_after": t, "state": dict(tw.state)}
    await job.wait()
    await call("clear")
    await tw.all_healthy(30)
    return facts


async def c_bank_down(tw: Twin) -> dict:
    await tw.all_healthy()
    await call("down")
    await tw.until(lambda st: set(st.values()) == {"down"}, 8, "everything down")
    async with httpx.AsyncClient(trust_env=False) as h:
        ready = (await h.get(f"{API}/ready")).json()
    assert ready["ready"], ready
    await call("up")
    return {"backend_still_ready": ready["ready"], "recovered_after": await tw.all_healthy(20)}


TRIALS, SUSTAINED_TICKS = 3, 2


async def observe(tw: Twin, origin: str | None) -> set[str]:
    """One trial: steady traffic, kill ``origin`` (or nothing), see who suffers."""
    await tw.all_healthy(60)
    job = await load(27)
    await asyncio.sleep(14)  # the generator's login wave has left every 10 s window
    if origin:
        await call("kill", origin)
    mark = time.monotonic()
    await asyncio.sleep(9)
    ticks = [(n, s) for t, n, s in tw.history if t >= mark]
    if origin:
        await call("start", origin)
    await job.wait()
    return {n for n in SERVICES if n != origin
            and sum(1 for m, s in ticks if m == n and s in BAD) >= SUSTAINED_TICKS}


async def c_predicted_vs_observed(tw: Twin) -> dict:
    """Measurement, not a pass/fail gate: does the propagation model match reality?"""
    async def rates(origin: str | None) -> dict[str, float]:
        seen = [await observe(tw, origin) for _ in range(TRIALS)]
        counts = {n: sum(n in x for x in seen) for n in SERVICES}
        return {n: round(c / TRIALS, 2) for n, c in counts.items() if c}

    out: dict = {"trials": TRIALS, "control_no_fault": await rates(None)}
    hits = missed = false = 0
    async with httpx.AsyncClient(trust_env=False) as h:
        for origin in ("database", "authentication", "account", "payment", "fraud",
                       "notification"):
            r = await h.post(f"{API}/propagation/analyze",
                             json={"failed_nodes": [f"bank-{origin}"]})
            predicted = {n["node_id"].removeprefix("bank-"): n["impact"]
                         for n in r.json()["result"]["impacted"] if n["depth"]}
            rate = await rates(origin)
            observed = {n for n, v in rate.items() if v > 0.5}
            likely = {n for n, v in predicted.items() if v >= 0.5}
            hits += len(observed & likely)
            missed += len(observed - likely)
            false += len(likely - observed)
            out[origin] = {"predicted_impact": predicted, "observed_rate": rate,
                           "missed": sorted(observed - likely), "false": sorted(likely - observed)}
    out["total"] = {"hits": hits, "missed": missed, "false": false,
                    "precision": round(hits / max(hits + false, 1), 2),
                    "recall": round(hits / max(hits + missed, 1), 2)}
    return out


CHECKS = [
    ("T1", "Twin shows the eight services and fourteen dependencies", c_source),
    ("T2", "Normal traffic raises (almost) no false alarms", c_false_alarms),
    ("T3", "Killing each service marks exactly that node down, then it recovers", c_kill_each),
    ("T4", "A crash is visible within 3 seconds (5 tries)", c_latency_of_detection),
    ("T5", "Soft downtime is shown as down", c_soft_downtime),
    ("T6", "A frozen process becomes unknown, never falsely down", c_frozen),
    ("T7", "Slow database is visible on the database and its callers", c_db_latency),
    ("T8", "Whole bank down is shown and the backend keeps running", c_bank_down),
    ("T9", "Measurement: predicted versus observed blast radius", c_predicted_vs_observed),
]


async def main() -> int:
    only = set(sys.argv[1:])
    if any(r["live"] != "stopped" for r in json.loads(ctl("status", "--json"))):
        sys.exit("The bank is running. Stop it first: bankctl.py down")
    env = {**os.environ, "SIMUCORE_TELEMETRY_ADAPTER": "bank_services"}
    log = (BACKEND / "live_twin_backend.log").open("wb")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port",
                               str(PORT), "--log-level", "warning"], cwd=BACKEND, env=env,
                              stdout=log, stderr=log)
    results, follower, tw = [], None, Twin()
    try:
        await call("up")
        async with httpx.AsyncClient(trust_env=False) as h:
            for _ in range(60):
                with contextlib.suppress(httpx.HTTPError):
                    if (await h.get(f"{API}/ready")).json()["adapter"] == "bank_services":
                        break
                await asyncio.sleep(0.25)
        follower = asyncio.create_task(tw.follow())
        for code, title, fn in CHECKS:
            if only and code not in only:
                continue
            start, facts, error = time.monotonic(), {}, None
            try:
                facts = await fn(tw)
                assert not follower.done(), "WebSocket connection was lost"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                await call("clear")
                await call("up")
            results.append({"code": code, "title": title, "passed": error is None,
                            "seconds": round(time.monotonic() - start, 1), "facts": facts,
                            "error": error})
            print(f"{'PASS' if not error else 'FAIL'} {code} {title}\n     "
                  f"{error or json.dumps(facts)}", flush=True)
    finally:
        if follower:
            follower.cancel()
        server.terminate()
        server.wait(10)
        log.close()
        await call("down")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if not only:
        out = BACKEND / "results"
        out.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        (out / "live_twin.json").write_text(json.dumps(
            {"run_at": stamp, "passed": passed, "total": len(results), "results": results},
            indent=2), encoding="utf-8", newline="\n")
        lines = ["# Live twin results", "",
                 f"Run at {stamp}. **{passed} of {len(results)} passed.**",
                 "Generated by `scripts/live_twin_check.py`.", "",
                 "| | Check | Result | Measured |", "|---|---|---|---|"]
        for r in results:
            lines.append(f"| {r['code']} | {r['title']} | {'pass' if r['passed'] else '**FAIL**'} "
                         f"| {r['error'] or json.dumps(r['facts'])} |")
        (out / "live_twin.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
