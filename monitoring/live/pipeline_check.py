"""Live test of every metric pipeline and every monitoring failure.

    cd monitoring
    .venv\\Scripts\\python live\\pipeline_check.py               everything, about 25 minutes
    .venv\\Scripts\\python live\\pipeline_check.py M02 F05       a subset (results are only printed)

Real bank, real Prometheus, real PostgreSQL. A second monitor reads the bank directly, so every
metric is checked through two independent implementations against a truth that comes from neither:
operating-system counters, exact request counts, byte sizes measured by the client, open sockets.

BANKCTL_MODE=docker runs the bank as containers. The stack uses its own Compose project, random
passwords and volumes, and is removed afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import psutil
import websockets

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
BANK = ROOT / "banking-sim"
BANK_PY = BANK / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
DOCKER = os.environ.get("BANKCTL_MODE") == "docker"
PROJECT = "simucore-mon-test"
PROM, DIRECT, BACKEND = "http://127.0.0.1:8200", "http://127.0.0.1:8201", "http://127.0.0.1:8000"
PIPES = {"prometheus": PROM, "direct": DIRECT}
SERVICES = list(json.loads((BANK / "dependency_map.json").read_text(encoding="utf-8"))["services"])
PORT = {n: s["port"] for n, s in json.loads(
    (BANK / "dependency_map.json").read_text(encoding="utf-8"))["services"].items()}
REQUIRED = ["cpu_pct", "mem_pct", "latency_ms", "rps", "error_rate", "net_rx_bps", "net_tx_bps",
            "db_response_ms", "active_connections", "availability", "queue_length"]
PW = {k: secrets.token_hex(16) for k in (
    "POSTGRES_PASSWORD", "GRAFANA_DB_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "MONITOR_DB_PASSWORD",
    "EXPORTER_DB_PASSWORD", "TWIN_DB_PASSWORD", "AI_DB_PASSWORD")}
WINDOW = 12.0  # rate window plus a tick: how long a steady state needs to fill the window
CHECKS: list[tuple] = []


def check(code: str, title: str):
    def add(fn):
        CHECKS.append((code, title, fn))
        return fn
    return add


# ------------------------------------------------------------------------- environment
class Lab:
    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        self.environ = {k: v for k, v in os.environ.items() if not k.startswith(
            ("POSTGRES_", "GRAFANA_", "SIMUCORE_", "MONITOR_", "EXPORTER_"))}
        self.http = httpx.AsyncClient(timeout=10, trust_env=False,
                                      limits=httpx.Limits(max_connections=300))
        self.key = ""

    def docker(self, *a: str, ok: bool = True) -> str:
        r = subprocess.run(["docker", *a], cwd=ROOT, env=self.environ, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if ok and r.returncode:
            raise RuntimeError(f"docker {' '.join(a[:5])}: {r.stderr.strip()[-500:]}")
        return r.stdout.strip()

    def compose(self, *a: str) -> str:
        return self.docker("compose", "-p", PROJECT, "--env-file", str(self.env_file), "-f",
                           "infra/docker-compose.yml", *a)

    def container(self, service: str) -> str:
        return f"{PROJECT}-{service}-1"

    def ctl(self, *a: str) -> str:
        r = subprocess.run([str(BANK_PY), "bankctl.py", *a], cwd=BANK, capture_output=True,
                           text=True, timeout=400)
        if r.returncode:
            raise RuntimeError(f"bankctl {' '.join(a)}: {(r.stderr or r.stdout).strip()}")
        return r.stdout

    def load_key(self) -> None:
        if DOCKER:
            text = (BANK / ".env").read_text(encoding="utf-8")
            self.key = next(x for x in text.splitlines()
                            if x.startswith("BANK_INTERNAL_KEY=")).split("=", 1)[1].strip()
        else:
            self.key = (BANK / ".runtime" / "internal.key").read_text(encoding="utf-8").strip()

    @property
    def internal(self) -> dict[str, str]:
        return {"X-Internal-Key": self.key}

    def bank(self, service: str, path: str = "") -> str:
        return f"http://127.0.0.1:{PORT[service]}{path}"

    async def fault(self, service: str, type_: str, duration: float = 120, **kw) -> None:
        r = await self.http.post(self.bank(service, "/control/faults"), headers=self.internal,
                                 json={"type": type_, "duration_s": duration, **kw})
        assert r.status_code == 200, r.text

    async def clear(self) -> None:
        for s in SERVICES:
            with contextlib.suppress(httpx.HTTPError):
                await self.http.delete(self.bank(s, "/control/faults"), headers=self.internal)

    async def get(self, url: str, **kw):
        r = await self.http.get(url, **kw)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        return r.json()

    async def db(self, user: str = "simucore", key: str = "POSTGRES_PASSWORD"):
        return await asyncpg.connect(host="127.0.0.1", port=5432, database="simucore", user=user,
                                     password=PW[key], timeout=8)

    # ---------------------------------------------------------------- observing
    async def service(self, pipe: str, service: str) -> dict:
        return await self.get(f"{PIPES[pipe]}/api/v1/metrics/current/{service}")

    async def watch(self, service: str, metric: str, seconds: float = 5) -> dict[str, float]:
        """Median of a metric over a few ticks, through each pipeline."""
        seen: dict[str, list[float]] = {p: [] for p in PIPES}
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for p in PIPES:
                v = (await self.service(p, service))["metrics"].get(metric)
                if v is not None:
                    seen[p].append(v)
            await asyncio.sleep(0.8)
        assert all(seen.values()), f"no {metric} of {service}: {seen}"
        return {p: round(statistics.median(v), 4) for p, v in seen.items()}

    async def until(self, what: str, fn, timeout: float = 30) -> float:
        start = time.monotonic()
        last = None
        while True:
            with contextlib.suppress(httpx.HTTPError, AssertionError):
                last = await fn()
                if last:
                    return round(time.monotonic() - start, 1)
            if time.monotonic() - start > timeout:
                raise AssertionError(f"{what}: not within {timeout}s (last: {last})")
            await asyncio.sleep(0.4)

    async def open_events(self, pipe: str = "prometheus") -> set[tuple]:
        return {(e["kind"], e["service"], e["metric"])
                for e in await self.get(f"{PIPES[pipe]}/api/v1/events/open")}

    async def quiet(self, timeout: float = 60) -> None:
        """Healthy baseline: everything up, nothing open, in both monitors."""
        async def ok():
            for p in PIPES.values():
                d = await self.get(f"{p}/api/v1/metrics/current")
                if not d["fresh"] or d["overview"]["up"] != 8 or d["open_events"]:
                    return False
            return True
        await self.until("quiet baseline", ok, timeout)


def near(measured: float, truth: float, rel: float, floor: float = 0.0) -> bool:
    return abs(measured - truth) <= max(abs(truth) * rel, floor)


def expect(values: dict[str, float], truth: float, rel: float, floor: float = 0.0) -> dict:
    for pipe, v in values.items():
        assert near(v, truth, rel, floor), f"{pipe}: {v} instead of {truth:.4g} (+-{rel:.0%})"
    return {"truth": round(truth, 3), **values}


@contextlib.asynccontextmanager
async def steady(lab: Lab, make, rps: float):
    """Send requests at an exact rate in the background; yields the list of results."""
    results: list = []
    stop = asyncio.Event()

    async def one(i: int) -> None:
        t = time.perf_counter()
        with contextlib.suppress(httpx.HTTPError):
            r = await make(i)
            results.append((time.perf_counter() - t, r))

    async def run() -> None:
        start, i, tasks = time.perf_counter(), 0, set()
        while not stop.is_set():
            task = asyncio.create_task(one(i))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            i += 1
            await asyncio.sleep(max(0.0, start + i / rps - time.perf_counter()))
        if tasks:
            await asyncio.wait(tasks)

    job = asyncio.create_task(run())
    try:
        yield results
    finally:
        stop.set()
        await job


def lookup(lab: Lab, service: str = "customer"):
    """A request to the customer service that reads the database once."""
    return lambda i: lab.http.get(
        lab.bank(service, f"/internal/customers/CUST{i % 200 + 1:05d}"), headers=lab.internal)


def bank_pid(lab: Lab, service: str) -> int:
    rows = json.loads(lab.ctl("status", "--json"))
    return next(r["pid"] for r in rows if r["service"] == service)


def os_cpu_seconds(lab: Lab, service: str, pid: int) -> float:
    if DOCKER:
        text = lab.docker("exec", f"bank-{service}", "cat", f"/proc/{pid}/stat")
        fields = text.rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / 100
    t = psutil.Process(pid).cpu_times()
    return t.user + t.system


def os_memory(lab: Lab, service: str, pid: int) -> float:
    if DOCKER:
        text = lab.docker("exec", f"bank-{service}", "cat", f"/proc/{pid}/status")
        return int(next(x for x in text.splitlines() if x.startswith("VmRSS")).split()[1]) * 1024
    mi = psutil.Process(pid).memory_info()
    return getattr(mi, "private", mi.rss)


# ----------------------------------------------------------------------- metric accuracy
@check("M01", "Every required metric of every service arrives through both pipelines")
async def m01(lab: Lab) -> dict:
    await asyncio.to_thread(lab.ctl, "workload", "normal", "--rps", "15", "--duration", "14")
    out = {}
    for p in PIPES:
        d = await lab.get(f"{PIPES[p]}/api/v1/metrics/current")
        values = 0
        for s in d["services"]:
            need = [m for m in REQUIRED
                    if not (m == "db_response_ms" and s["service"] == "notification")]
            absent = [m for m in need if m not in s["metrics"]]
            assert s["status"] == "up" and not absent and not s["invalid"], (p, s)
            values += sum(s["metrics"][m] is not None for m in need)
        out[p] = {"services": len(d["services"]), "values": values}
        assert d["origin"] == "measured" and values >= 80
    return out


@check("M02", "Requests per second: exact request rate")
async def m02(lab: Lab) -> dict:
    async with steady(lab, lookup(lab), 20):
        await asyncio.sleep(WINDOW)
        customer = await lab.watch("customer", "rps", 6)
        database = await lab.watch("database", "rps", 4)
    return {"customer": expect(customer, 20, 0.06), "database": expect(database, 20, 0.06)}


@check("M03", "Request latency: compared with the client's own stopwatch")
async def m03(lab: Lab) -> dict:
    await lab.fault("customer", "latency", latency_ms=120, jitter=0.3)
    async with steady(lab, lookup(lab), 15) as results:
        await asyncio.sleep(WINDOW)
        mark = len(results)
        mean = await lab.watch("customer", "latency_mean_ms", 6)
        p95 = await lab.watch("customer", "latency_ms", 4)
        seconds = sorted(x[0] for x in results[mark - 100:])
    await lab.clear()
    truth_mean = statistics.mean(seconds) * 1000
    truth_p95 = seconds[int(0.95 * len(seconds))] * 1000
    return {"mean_ms": expect(mean, truth_mean, 0.12), "p95_ms": expect(p95, truth_p95, 0.20)}


@check("M04", "Error rate: exactly one request in four fails")
async def m04(lab: Lab) -> dict:
    await lab.fault("database", "db_connection_failure", mode="refuse")

    async def mixed(i: int):
        if i % 4 == 0:  # 503: needs the database
            return await lookup(lab)(i)
        return await lab.http.get(lab.bank("customer", "/customers/me"))  # 401: does not

    async with steady(lab, mixed, 20) as results:
        await asyncio.sleep(WINDOW)
        mark = len(results)
        rate = await lab.watch("customer", "error_rate", 6)
        codes = [r.status_code for _, r in results[mark:]]
    await lab.clear()
    truth = sum(x >= 500 for x in codes) / len(codes)
    assert set(codes) == {401, 503}, set(codes)  # client errors must not count as errors
    return expect(rate, truth, 0.10, 0.02)


@check("M05", "CPU: compared with the operating system's counter")
async def m05(lab: Lab) -> dict:
    pid = await asyncio.to_thread(bank_pid, lab, "fraud")
    out = {}
    for label, magnitude in (("light", 0.25), ("heavy", 0.75)):
        await lab.fault("fraud", "resource_exhaustion", mode="cpu", magnitude=magnitude)
        await asyncio.sleep(WINDOW)
        a, ta = os_cpu_seconds(lab, "fraud", pid), time.monotonic()
        seen = await lab.watch("fraud", "cpu_pct", 8)
        b, tb = os_cpu_seconds(lab, "fraud", pid), time.monotonic()
        await lab.clear()
        out[label] = expect(seen, (b - a) / (tb - ta) * 100, 0.15, 6)
        await asyncio.sleep(1)
    return out


@check("M06", "Memory: compared with the operating system's counter")
async def m06(lab: Lab) -> dict:
    pid = await asyncio.to_thread(bank_pid, lab, "account")
    await asyncio.sleep(3)
    before = expect(await lab.watch("account", "mem_bytes", 3), os_memory(lab, "account", pid), .05)
    await lab.fault("account", "resource_exhaustion", mode="memory")
    await asyncio.sleep(4)
    full = expect(await lab.watch("account", "mem_bytes", 3), os_memory(lab, "account", pid), 0.05)
    pct = await lab.watch("account", "mem_pct", 3)
    await lab.clear()
    assert all(v >= 95 for v in pct.values()), pct
    return {"normal_bytes": before, "full_bytes": full, "full_pct": pct}


@check("M07", "Network traffic: compared with bytes counted by the client")
async def m07(lab: Lab) -> dict:
    def size(headers) -> int:
        return sum(len(k) + len(v) + 4 for k, v in headers)

    def verify(i: int):  # needs no other service, so every byte is the client's own
        return lab.http.post(lab.bank("authentication", "/internal/verify"), headers=lab.internal,
                             json={"token": "x" * 1500})

    async with steady(lab, verify, 12) as results:
        await asyncio.sleep(WINDOW)
        rx = await lab.watch("authentication", "net_rx_bps", 5)
        tx = await lab.watch("authentication", "net_tx_bps", 5)
        r = results[-1][1]
    request = size(r.request.headers.raw) + len(r.request.content)
    response = size(r.headers.raw) + len(r.content)
    # uvicorn adds its "date" and "server" headers after the application has been measured.
    response -= sum(len(k) + len(v) + 4 for k, v in r.headers.raw
                    if k.lower() in (b"date", b"server"))
    return {"received": expect(rx, request * 12, 0.06), "sent": expect(tx, response * 12, 0.06)}


@check("M08", "Database response time: caller's view and database's own view are distinct")
async def m08(lab: Lab) -> dict:
    await lab.fault("database", "latency", latency_ms=100, jitter=0)
    async with steady(lab, lookup(lab), 10):
        await asyncio.sleep(WINDOW)
        caller = await lab.watch("customer", "db_response_ms", 5)
        own = await lab.watch("database", "db_response_ms", 4)
        times = []
        for _ in range(15):
            t = time.perf_counter()
            await lab.http.get(lab.bank("database", "/internal/customers/CUST00001"),
                               headers=lab.internal)
            times.append((time.perf_counter() - t) * 1000)
    await lab.clear()
    truth = statistics.median(times)
    assert all(v < 15 for v in own.values()), f"the query itself is fast: {own}"
    out = {"slow_network_caller": expect(caller, truth, 0.15, 10), "database_itself": own}
    await asyncio.sleep(3)
    await lab.fault("database", "db_connection_failure", mode="exhaust")
    async with steady(lab, lookup(lab, "database"), 5):
        await asyncio.sleep(WINDOW)
        waiting = await lab.watch("database", "db_response_ms", 5)
    await lab.clear()
    out["pool_exhausted"] = expect(waiting, 750, 0.10)  # the pool's acquire timeout
    return out


@check("M09", "Active connections: 25 real sockets are opened and closed")
async def m09(lab: Lab) -> dict:
    before = await lab.watch("notification", "active_connections", 3)
    socks = [await asyncio.open_connection("127.0.0.1", PORT["notification"]) for _ in range(25)]
    await asyncio.sleep(4)
    during = await lab.watch("notification", "active_connections", 3)
    for _, w in socks:
        w.close()
    await asyncio.sleep(4)
    after = await lab.watch("notification", "active_connections", 3)
    for p in PIPES:
        assert abs(during[p] - before[p] - 25) <= 2, (before, during)
        assert abs(after[p] - before[p]) <= 2, (before, after)
    return {"before": before, "with_25_more": during, "after": after}


@check("M10", "Request queue length: 12 requests are kept waiting")
async def m10(lab: Lab) -> dict:
    await lab.fault("payment", "resource_exhaustion", mode="slots")
    stop = asyncio.Event()

    async def keep_waiting(i: int):
        # Staggered: the service gives up on a waiting request after 1 s, and if all gave up at
        # the same moment the true queue would drop to 0 in step with the 1 s scrapes.
        await asyncio.sleep(i / 12)
        while not stop.is_set():
            with contextlib.suppress(httpx.HTTPError):
                await lab.http.get(lab.bank("payment", "/internal/payments/none"),
                                   headers=lab.internal)

    jobs = [asyncio.create_task(keep_waiting(i)) for i in range(12)]
    await asyncio.sleep(5)
    queue = await lab.watch("payment", "queue_length", 6)
    stop.set()
    await asyncio.gather(*jobs)
    await lab.clear()
    out = expect(queue, 12, 0.10, 1)
    await asyncio.sleep(4)
    assert set((await lab.watch("payment", "queue_length", 3)).values()) == {0.0}
    return out


@check("M11", "Service availability: crash, soft downtime, hang and recovery")
async def m11(lab: Lab) -> dict:
    out: dict = {}

    def is_status(service: str, want: str):
        async def probe():
            return all([(await lab.service(p, service))["status"] == want for p in PIPES])
        return probe

    await asyncio.to_thread(lab.ctl, "kill", "account")
    out["crash_seen_after_s"] = await lab.until("account down", is_status("account", "down"), 12)
    for p in PIPES:
        s = await lab.service(p, "account")
        assert s["metrics"] == {"availability": 0.0}, s
    await asyncio.to_thread(lab.ctl, "start", "account")
    out["recovery_seen_after_s"] = await lab.until("account up", is_status("account", "up"), 15)

    await lab.fault("fraud", "downtime", 60)
    out["downtime_seen_after_s"] = await lab.until("fraud down", is_status("fraud", "down"), 12)
    await lab.clear()
    await lab.until("fraud up", is_status("fraud", "up"), 12)

    pid = await asyncio.to_thread(bank_pid, lab, "notification")

    def freeze(on: bool) -> None:
        if DOCKER:
            lab.docker("pause" if on else "unpause", "bank-notification")
        elif on:
            psutil.Process(pid).suspend()
        else:
            psutil.Process(pid).resume()
    freeze(True)
    try:
        out["hang_seen_after_s"] = await lab.until(
            "notification unresponsive", is_status("notification", "unresponsive"), 15)
    finally:
        freeze(False)
    await lab.until("notification up", is_status("notification", "up"), 15)
    return out


@check("M12", "An idle bank is healthy: zero traffic, no latency, no alarms")
async def m12(lab: Lab) -> dict:
    await asyncio.sleep(16)
    for p in PIPES:
        for s in (await lab.get(f"{PIPES[p]}/api/v1/metrics/current"))["services"]:
            m = s["metrics"]
            assert m["rps"] == 0 and m["latency_ms"] is None and m["error_rate"] is None, (p, s)
            assert m["cpu_pct"] is not None and m["mem_pct"] > 1
        assert await lab.open_events(p) == set()
    return {"open_events": 0}


@check("M13", "Infrastructure level: PostgreSQL, the Docker machine and Prometheus")
async def m13(lab: Lab) -> dict:
    async def infra() -> dict:
        d = await lab.get(f"{PROM}/api/v1/infrastructure/current")
        return {c["component"]: c for c in d["components"]}

    base = await infra()
    assert set(base) == {"docker-host", "platform-postgres", "prometheus"}, set(base)
    assert all(c["status"] == "up" for c in base.values()), base
    conns = [await lab.db() for _ in range(7)]
    try:
        # Others (the monitor's own pool, Grafana) come and go too, so the truth is PostgreSQL's
        # own count at the same moment, not "seven more than before".
        counts: dict[str, float] = {}

        async def matches_postgres():
            counts["measured"] = (await infra())["platform-postgres"]["metrics"]["connections"]
            counts["truth"] = await conns[0].fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname IS NOT NULL")
            return abs(counts["measured"] - counts["truth"]) <= 1 and counts["truth"] >= 9
        took = await lab.until("database connections", matches_postgres, 30)
    finally:
        for c in conns:
            await c.close()
    total = int(lab.docker("info", "--format", "{{.MemTotal}}"))
    text = lab.docker("exec", lab.container("node-exporter"), "cat", "/proc/meminfo")
    kb = {x.split(":")[0]: int(x.split()[1]) for x in text.splitlines()}
    truth = 100 * (1 - kb["MemAvailable"] / kb["MemTotal"])
    host = (await infra())["docker-host"]["metrics"]
    assert near(host["mem_pct"], truth, 0.10, 2) and near(kb["MemTotal"] * 1024, total, 0.02)
    prom = base["prometheus"]["metrics"]
    assert prom["targets_up"] == prom["targets_total"] == 13, prom
    return {"postgres_connections": {**counts, "tracked_after_s": took},
            "host_memory_pct": {"truth": round(truth, 1), "measured": host["mem_pct"]},
            "scrape_targets": prom["targets_up"]}


@check("M14", "Both pipelines agree with each other on every metric under a mixed workload")
async def m14(lab: Lab) -> dict:
    load = asyncio.create_task(asyncio.to_thread(
        lab.ctl, "workload", "normal", "--rps", "12", "--duration", "40"))
    await asyncio.sleep(16)
    series: dict[tuple, dict[str, list]] = {}
    for _ in range(10):
        for p in PIPES:
            for s in (await lab.get(f"{PIPES[p]}/api/v1/metrics/current"))["services"]:
                for k, v in s["metrics"].items():
                    if v is not None:
                        series.setdefault((s["service"], k), {q: [] for q in PIPES})[p].append(v)
        await asyncio.sleep(2)
    await load
    floor = {"cpu_pct": 4, "rps": 1.0, "net_rx_bps": 600, "net_tx_bps": 600, "latency_ms": 12,
             "latency_mean_ms": 8, "db_response_ms": 4, "active_connections": 4, "in_flight": 2,
             "queue_length": 1, "error_rate": 0.02, "mem_bytes": 3e6, "mem_pct": 1}
    bad, compared = [], 0
    for (service, k), v in series.items():
        if all(v.values()):
            a, b = (statistics.median(v[p]) for p in PIPES)
            compared += 1
            if not near(a, b, 0.15, floor.get(k, 0)):
                bad.append((service, k, round(a, 3), round(b, 3)))
    assert not bad, bad
    return {"metrics_compared": compared, "disagreements": 0}


@check("M17", "Network connections: exact call rate, stopwatch latency, failures, open circuit")
async def m17(lab: Lab) -> dict:
    async def link(pipe: str, a: str, b: str) -> dict:
        d = await lab.get(f"{PIPES[pipe]}/api/v1/links/current")
        assert len(d["links"]) == 14 and d["origin"] == "measured"
        return next(k for k in d["links"] if (k["source"], k["target"]) == (a, b))["metrics"]

    # (+4 s: the direct pipeline keeps one scrape beyond its window, and data is up to a tick old)
    await asyncio.sleep(WINDOW + 4)  # an earlier check's traffic must have left the window
    async with steady(lab, lookup(lab), 12):
        await asyncio.sleep(WINDOW + 4)
        rates = {p: (await link(p, "customer", "database"))["calls_per_s"] for p in PIPES}
        quiet = {p: await link(p, "transaction", "payment") for p in PIPES}
        await lab.fault("database", "latency", latency_ms=150, jitter=0)
        await asyncio.sleep(WINDOW + 4)
        slow = {p: (await link(p, "customer", "database"))["latency_ms"] for p in PIPES}
        times = []
        for _ in range(10):
            t = time.perf_counter()
            await lab.http.get(lab.bank("database", "/internal/customers/CUST00001"),
                               headers=lab.internal)
            times.append((time.perf_counter() - t) * 1000)
        await lab.clear()
        await asyncio.sleep(2)
        await lab.fault("database", "db_connection_failure", mode="refuse")
        await asyncio.sleep(WINDOW + 4)
        broken = {p: await link(p, "customer", "database") for p in PIPES}
        await lab.clear()
    for p in PIPES:
        assert quiet[p]["calls_per_s"] == 0 and quiet[p]["latency_ms"] is None, quiet[p]
        assert broken[p]["failure_rate"] >= 0.98 and broken[p]["circuit"] in (1, 2), broken[p]
    return {"calls_per_s": expect(rates, 12, 0.06),
            "latency_ms": expect(slow, statistics.median(times), 0.12, 10),
            "failure_rate": {p: broken[p]["failure_rate"] for p in PIPES}}


@check("M18", "Bank database pool: 8 connections, 7 held, all refused")
async def m18(lab: Lab) -> dict:
    size = await lab.watch("database", "db_pool_size", 3)
    await lab.fault("database", "db_connection_failure", mode="exhaust", magnitude=0.875)
    await asyncio.sleep(5)
    used = await lab.watch("database", "db_pool_in_use", 3)
    await lab.clear()
    await asyncio.sleep(1)
    await lab.fault("database", "db_connection_failure", mode="refuse")
    await asyncio.sleep(WINDOW + 4)
    failing = await lab.watch("database", "db_failure_rate", 4)
    await lab.clear()
    assert all(v >= 0.98 for v in failing.values()), failing
    where = {(await lab.service(p, s))["deployment"] for p in PIPES for s in SERVICES}
    assert where == {"container" if DOCKER else "process"}, where
    return {"size": expect(size, 8, 0), "in_use": expect(used, 7, 0.0, 0.5), "failing": failing}


# -------------------------------------------------------------- history, streaming, scale
@check("M15", "History: every tick is stored, without gaps, and equals what was streamed")
async def m15(lab: Lab) -> dict:
    streamed: dict[str, dict] = {}
    async with websockets.connect("ws://127.0.0.1:8200/ws/metrics", max_size=2**23) as ws:
        async with steady(lab, lookup(lab), 8):
            end = time.monotonic() + 30
            while time.monotonic() < end:
                m = json.loads(await ws.recv())
                for s in m["services"]:
                    streamed.setdefault(s["ts"], {})[s["node_id"]] = s
    stamps = sorted(streamed)[1:-1]
    await asyncio.sleep(4)
    conn = await lab.db()
    rows = await conn.fetch("SELECT * FROM metric_sample WHERE ts >= $1 AND ts <= $2 AND "
                            "origin='measured'", datetime.fromisoformat(stamps[0]),
                            datetime.fromisoformat(stamps[-1]))
    await conn.close()
    stored = {(r["ts"].isoformat(), r["node_id"]): r for r in rows}
    assert len(stored) == len(stamps) * 8, (len(stored), len(stamps) * 8)
    compared = 0
    for ts in stamps:
        for node, s in streamed[ts].items():
            row = stored[(ts, node)]
            for k, v in s["metrics"].items():
                assert (row[k] is None) == (v is None), (ts, node, k)
                assert v is None or near(row[k], v, 1e-3, 1e-3), (node, k, row[k], v)
                compared += 1
    gaps = [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
            for a, b in zip(stamps, stamps[1:], strict=False)]
    assert max(gaps) < 3.0, gaps
    h = await lab.get(f"{PROM}/api/v1/metrics/history/customer", params={
        "metrics": "rps,cpu_pct", "start": stamps[0], "end": stamps[-1], "step": 10})
    mine = [r for (ts, n), r in stored.items() if n == "bank-customer"
            and ts != stamps[-1] and r["rps"] is not None]
    overall = sum(p["rps"]["avg"] * p["samples"] for p in h["points"] if p["rps"]) / \
        sum(p["samples"] for p in h["points"] if p["rps"])
    assert near(overall, statistics.mean(r["rps"] for r in mine), 0.03), overall
    av = await lab.get(f"{PROM}/api/v1/metrics/availability",
                       params={"start": stamps[0], "end": stamps[-1]})
    assert {s["availability"] for s in av["services"]} == {1.0} and len(av["services"]) == 8
    return {"ticks": len(stamps), "rows": len(stored), "values_compared": compared,
            "longest_interval_s": round(max(gaps), 2)}


@check("M16", "WebSocket: 60 clients at once, one of them not reading; updates never stop")
async def m16(lab: Lab) -> dict:
    url = "ws://127.0.0.1:8200/ws/metrics"
    stuck = await asyncio.open_connection("127.0.0.1", 8200)
    stuck[1].write(b"GET /ws/metrics HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: "
                   b"Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                   b"Sec-WebSocket-Version: 13\r\n\r\n")  # and then never reads

    async def client(i: int) -> list[tuple[float, int]]:
        async with websockets.connect(url, max_size=2**23) as ws:
            if i % 2:
                await ws.send(json.dumps({"type": "subscribe", "services": ["account"]}))
            got, end = [], time.monotonic() + 14
            while time.monotonic() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), 6))
                got.append((time.monotonic(), m["sequence"]))
            if i % 2:
                assert {s["service"] for s in m["services"]} == {"account"}
            return got

    everything = await asyncio.gather(*(client(i) for i in range(60)))
    stuck[1].close()
    worst = 0.0
    for got in everything:
        seq = [s for _, s in got]
        assert seq == sorted(seq) and len(set(seq)) >= 5, seq
        worst = max(worst, *(b[0] - a[0] for a, b in zip(got, got[1:], strict=False)))
    status = await lab.get(f"{PROM}/api/v1/monitoring/status")
    assert worst < 4 and status["last_tick_ms"] < 1000, (worst, status["last_tick_ms"])
    return {"clients": 60, "longest_pause_s": round(worst, 2),
            "tick_ms": status["last_tick_ms"]}


# ------------------------------------------------------------------- monitoring failures
def exactly(lab: Lab, want: set[tuple], pipes=tuple(PIPES)):
    async def probe():
        return all([await lab.open_events(p) == want for p in pipes])
    return probe


@check("F01", "Missing metrics: exactly the lost ones are reported, then cleared")
async def f01(lab: Lab) -> dict:
    await lab.fault("account", "telemetry_fault", mode="missing")
    want = {("metric_missing", "account", m)
            for m in ("cpu_pct", "mem_bytes", "mem_pct", "queue_length")}
    seen = await lab.until("missing metrics reported", exactly(lab, want), 30)
    s = await lab.service("prometheus", "account")
    assert s["status"] == "up" and s["metrics"]["availability"] == 1  # the service itself works
    await lab.clear()
    return {"reported_after_s": seen,
            "cleared_after_s": await lab.until("cleared", exactly(lab, set()), 25)}


@check("F02", "Impossible values are reported and never stored")
async def f02(lab: Lab) -> dict:
    await lab.fault("payment", "telemetry_fault", mode="invalid")
    want = {("metric_invalid", "payment", m)
            for m in ("mem_bytes", "mem_pct", "active_connections")}
    seen = await lab.until("invalid metrics reported", exactly(lab, want), 25)
    since = datetime.now(UTC)
    await asyncio.sleep(6)
    conn = await lab.db()
    n = await conn.fetchval("SELECT count(*) FROM metric_sample WHERE node_id='bank-payment' AND "
                            "ts > $1 AND (mem_bytes IS NOT NULL OR active_connections IS NOT NULL)",
                            since)
    rows = await conn.fetchval("SELECT count(*) FROM metric_sample WHERE "
                               "node_id='bank-payment' AND ts > $1", since)
    await conn.close()
    assert n == 0 and rows >= 2, (n, rows)
    await lab.clear()
    await lab.until("cleared", exactly(lab, set()), 25)
    return {"reported_after_s": seen, "bad_values_stored": n}


@check("F03", "Unreadable or hanging metrics pages: that service is unknown, others unaffected")
async def f03(lab: Lab) -> dict:
    out = {}
    for mode in ("garbage", "slow"):
        await lab.fault("fraud", "telemetry_fault", mode=mode)
        want = {("service_unresponsive", "fraud", None)}
        out[mode] = await lab.until(f"{mode} reported", exactly(lab, want), 25)
        for p in PIPES:
            d = await lab.get(f"{PIPES[p]}/api/v1/metrics/current")
            assert d["overview"]["up"] == 7 and d["overview"]["down"] == 0, d["overview"]
        await lab.clear()
        await lab.until("cleared", exactly(lab, set()), 30)
    return {"reported_after_s": out}


@check("F04", "Prometheus stopped or frozen: one alarm, no false service alarms, stale data marked")
async def f04(lab: Lab) -> dict:
    out = {}
    for label, off, on in (("stopped", ("stop", "-t", "2"), ("start",)),
                           ("frozen", ("pause",), ("unpause",))):
        await asyncio.to_thread(lab.docker, *off, lab.container("prometheus"))
        try:
            want = {("source_unavailable", None, None)}
            out[f"{label}_reported_after_s"] = await lab.until(
                label, exactly(lab, want, ("prometheus",)), 25)

            async def stale():
                d = await lab.get(f"{PROM}/api/v1/metrics/current")
                return d["fresh"] is False
            await lab.until("marked stale", stale, 20)
            assert await lab.open_events("prometheus") == want
            assert await lab.open_events("direct") == set()  # the bank is fine, and it shows
            tw = await lab.get(f"{PROM}/api/v1/twin/samples")
            assert tw["samples"] == [] and tw["fresh"] is False
        finally:
            await asyncio.to_thread(lab.docker, *on, lab.container("prometheus"))
        out[f"{label}_recovered_after_s"] = await lab.until(
            "recovered", exactly(lab, set(), ("prometheus",)), 60)
        await lab.quiet()
    return out


@check("F05", "Database stopped for 25 s: monitoring continues and no row is lost")
async def f05(lab: Lab) -> dict:
    begin = datetime.now(UTC)
    await asyncio.sleep(3)
    await asyncio.to_thread(lab.docker, "stop", "-t", "3", lab.container("postgres"))
    try:
        seen = await lab.until("store_failed", lambda: _has(lab, "store_failed"), 25)
        await asyncio.sleep(20)
        st = await lab.get(f"{PROM}/api/v1/monitoring/status")
        assert st["data_age_s"] < 5 and st["history"]["buffered_rows"] > 50, st
        r = await lab.http.get(f"{PROM}/api/v1/metrics/history/account")
        assert r.status_code == 503, r.status_code
        buffered = st["history"]["buffered_rows"]
    finally:
        await asyncio.to_thread(lab.docker, "start", lab.container("postgres"))

    async def flushed():
        h = (await lab.get(f"{PROM}/api/v1/monitoring/status"))["history"]
        return h["connected"] and h["buffered_rows"] < 20
    await lab.until("buffer written", flushed, 60)
    await asyncio.sleep(4)
    conn = await lab.db()
    rows = await conn.fetch("SELECT DISTINCT ts FROM metric_sample WHERE ts > $1 AND "
                            "origin='measured' ORDER BY ts", begin)
    await conn.close()
    gaps = [(b["ts"] - a["ts"]).total_seconds() for a, b in zip(rows, rows[1:], strict=False)]
    st = (await lab.get(f"{PROM}/api/v1/monitoring/status"))["history"]
    assert max(gaps) < 3.5 and st["dropped_rows"] == 0 and st["rejected_rows"] == 0, (max(gaps), st)
    await lab.quiet()
    return {"reported_after_s": seen, "rows_buffered": buffered, "rows_lost": 0,
            "longest_gap_s": round(max(gaps), 2)}


async def _has(lab: Lab, kind: str) -> bool:
    return any(k == kind for k, _, _ in await lab.open_events("prometheus"))


@check("F06", "Monitoring service killed: history survives and its absence is visible")
async def f06(lab: Lab) -> dict:
    await lab.fault("account", "downtime", 120)
    await lab.until("account down", lambda: _has(lab, "service_down"), 15)
    await asyncio.sleep(3)
    conn = await lab.db()
    before = await conn.fetchval("SELECT count(*) FROM metric_sample")
    killed = datetime.now(UTC)
    await asyncio.to_thread(lab.docker, "kill", lab.container("monitoring"))
    await asyncio.sleep(6)
    state = lab.docker("inspect", "-f", "{{.State.Status}} {{.RestartCount}}",
                       lab.container("monitoring"))

    async def prometheus_notices():
        r = await lab.get("http://127.0.0.1:9090/api/v1/query",
                          params={"query": 'up{job="monitoring"}'})
        return r["data"]["result"][0]["value"][1] == "0"
    noticed = await lab.until("Prometheus sees the monitor is gone", prometheus_notices, 25)
    await lab.clear()
    await asyncio.to_thread(lab.compose, "up", "-d", "monitoring")
    await lab.quiet(60)
    after = await conn.fetchval("SELECT count(*) FROM metric_sample")
    row = await conn.fetchrow("SELECT * FROM monitoring_event WHERE kind='service_down' AND "
                              "service='account' ORDER BY opened_at DESC LIMIT 1")
    gap = await conn.fetchval("SELECT min(ts) FROM metric_sample WHERE ts > $1", killed)
    still_open = await conn.fetchval("SELECT count(*) FROM monitoring_event "
                                     "WHERE closed_at IS NULL")
    await conn.close()
    assert after > before and row["closed_at"] and "restarted" in row["message"], dict(row)
    assert still_open == 0
    return {"state_6s_after_kill": state, "prometheus_noticed_after_s": round(noticed + 6, 1),
            "history_kept": True,
            "gap_s": round((gap - killed).total_seconds(), 1)}


@check("F07", "All services crash together and come back")
async def f07(lab: Lab) -> dict:
    await asyncio.to_thread(lab.ctl, "down")
    want = {("service_down", s, None) for s in SERVICES}
    seen = await lab.until("all down", exactly(lab, want), 25)
    await asyncio.to_thread(lab.ctl, "up")
    start = time.monotonic()
    await lab.quiet(90)
    return {"all_reported_after_s": seen, "all_clear_after_s": round(time.monotonic() - start, 1)}


@check("F08", "Digital twin: monitoring output drives node states; loss of it means unknown")
async def f08(lab: Lab) -> dict:
    async def twin(service: str) -> dict:
        return (await lab.get(f"{BACKEND}/api/v1/twin/nodes/bank-{service}"))["state"]

    ready = await lab.get(f"{BACKEND}/api/v1/ready")
    assert ready["adapter"] == "monitoring", ready

    def is_status(service: str, want: str):
        async def probe():
            return (await twin(service))["status"] == want
        return probe

    async with steady(lab, lookup(lab), 8):
        await asyncio.sleep(WINDOW)
        m = (await twin("customer"))["metrics"]
        assert {"net_rx_bps", "db_response_ms", "active_connections", "queue_length",
                "latency_ms"} <= set(m), m
        assert near(m["rps"], 8, 0.15), m["rps"]
    async with websockets.connect("ws://127.0.0.1:8080/ws/twin", max_size=2**22) as ws:
        snap = json.loads(await ws.recv())
    assert snap["source"] == "monitoring" and snap["origin"] == "measured"
    async with websockets.connect("ws://127.0.0.1:8080/monitoring/ws/metrics", max_size=2**23,
                                  origin="http://localhost:8080") as ws:
        assert json.loads(await ws.recv())["overview"]["services"] == 8  # what the browser uses

    await asyncio.to_thread(lab.ctl, "kill", "payment")
    crash = await lab.until("twin shows payment down", is_status("payment", "down"), 15)
    await asyncio.to_thread(lab.ctl, "start", "payment")
    await lab.until("twin shows payment healthy", is_status("payment", "healthy"), 20)

    await asyncio.to_thread(lab.docker, "stop", "-t", "2", lab.container("monitoring"))
    try:
        gone = await lab.until("twin shows unknown", is_status("account", "unknown"), 20)
        states = (await lab.get(f"{BACKEND}/api/v1/twin/state"))["states"]
        assert {s["status"] for s in states} == {"unknown"}
    finally:
        await asyncio.to_thread(lab.docker, "start", lab.container("monitoring"))
    back = await lab.until("twin is live again", is_status("account", "healthy"), 40)
    return {"crash_in_twin_after_s": crash, "monitor_lost_unknown_after_s": gone,
            "live_again_after_s": back}


@check("F09", "The monitoring service's database role can append and nothing else")
async def f09(lab: Lab) -> dict:
    conn = await lab.db("monitor_writer", "MONITOR_DB_PASSWORD")
    attempts = ["UPDATE metric_sample SET cpu_pct = 0", "DELETE FROM metric_sample",
                "TRUNCATE metric_sample", "DELETE FROM monitoring_event", "DROP TABLE app_user",
                "SELECT * FROM app_user", "INSERT INTO audit_log(actor, action) VALUES ('x','y')",
                "CREATE TABLE evil (i int)", "ALTER TABLE metric_sample ADD COLUMN evil int",
                "SELECT drop_old_metric_partitions(0)", "SELECT ensure_metric_partitions(999)"]
    for sql in attempts:
        try:
            await conn.execute(sql)
        except (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError):
            continue
        raise AssertionError(f"allowed: {sql}")
    await conn.close()
    conn = await lab.db("metrics_exporter", "EXPORTER_DB_PASSWORD")
    for sql in ("SELECT * FROM metric_sample", "SELECT ensure_metric_partitions(1)"):
        try:
            await conn.execute(sql)
        except asyncpg.InsufficientPrivilegeError:
            continue
        raise AssertionError(f"exporter allowed: {sql}")
    await conn.close()
    return {"refused": len(attempts) + 2}


@check("F10", "Partitions: late ones are repaired, old ones removed, recent ones protected")
async def f10(lab: Lab) -> dict:
    conn = await lab.db()
    month = (datetime.now(UTC).replace(day=15) + timedelta(days=31 * 6)).replace(
        day=3, hour=0, minute=0, second=0, microsecond=0)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("INSERT INTO metric_sample(ts, node_id, cpu_pct) VALUES($1,'late',7)",
                           month)
        assert await conn.fetchval("SELECT count(*) FROM metric_sample_default") == 1
        await conn.fetchval("SELECT ensure_metric_partitions(8)")
        where = await conn.fetchval("SELECT tableoid::regclass::text FROM metric_sample "
                                    "WHERE node_id='late'")
        assert where == f"metric_sample_y{month:%Y}m{month:%m}", where
        assert await conn.fetchval("SELECT count(*) FROM metric_sample_default") == 0
        await conn.execute("CREATE TABLE metric_sample_y2020m01 PARTITION OF metric_sample "
                           "FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')")
        await conn.execute("INSERT INTO metric_sample(ts, node_id) VALUES('2020-01-05','old')")
        now = await conn.fetchval("SELECT count(*) FROM metric_sample WHERE node_id LIKE 'bank-%'")
        dropped = await conn.fetchval("SELECT drop_old_metric_partitions(30)")
        assert dropped == 1 and await conn.fetchval(
            "SELECT count(*) FROM metric_sample WHERE node_id='old'") == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM metric_sample WHERE node_id LIKE 'bank-%'") >= now
    finally:
        await tx.rollback()
        await conn.close()
    st = await lab.get(f"{PROM}/api/v1/monitoring/status")
    assert st["history"]["rows_without_partition"] == 0
    return {"late_rows_moved": 1, "old_partitions_dropped": 1}


@check("F11", "Migration: repeatable, and upgrades a Phase 1 database without losing data")
async def f11(lab: Lab) -> dict:
    again = await asyncio.to_thread(lab.compose, "run", "--rm", "migrate")
    assert "already applied 003_monitoring.sql" in again and "complete" in again, again
    name, net = f"{PROJECT}-oldpg", f"{PROJECT}_default"
    await asyncio.to_thread(
        lab.docker, "run", "-d", "--rm", "--name", name, "--network", net,
        "-e", "POSTGRES_USER=simucore", "-e", f"POSTGRES_PASSWORD={PW['POSTGRES_PASSWORD']}",
        "-e", "POSTGRES_DB=simucore", "-e", f"GRAFANA_DB_PASSWORD={PW['GRAFANA_DB_PASSWORD']}",
        "-v", f"{ROOT / 'database' / 'schema'}:/docker-entrypoint-initdb.d:ro",
        "postgres:16-alpine")
    try:
        psql = ("exec", name, "psql", "-h", "127.0.0.1", "-U", "simucore", "-d", "simucore",
                "-v", "ON_ERROR_STOP=1", "-tAc")

        async def up():
            return lab.docker(*psql, "SELECT 1", ok=False) == "1"
        await lab.until("old database is up", up, 40)
        lab.docker(*psql, "INSERT INTO metric_sample(ts,node_id,cpu_pct) SELECT now() - "
                   "make_interval(secs => i), 'old-node', i FROM generate_series(1,500) i")
        out = await asyncio.to_thread(lab.compose, "run", "--rm", "-e", f"PGHOST={name}", "migrate")
        assert "applying 003_monitoring.sql" in out
        lab.docker(*psql, "SELECT ensure_metric_partitions(1)")
        kept = lab.docker(*psql, "SELECT count(*) || ' ' || sum(cpu_pct) || ' ' || "
                          "count(*) FILTER (WHERE origin='measured') FROM metric_sample")
        left = lab.docker(*psql, "SELECT count(*) FROM metric_sample_default WHERE ts >= "
                          "date_trunc('month', now())")
        assert kept == "500 125250 500" and left == "0", (kept, left)
    finally:
        lab.docker("rm", "-f", name, ok=False)
    return {"old_rows_kept": 500}


@check("F12", "Synthetic data: clearly labelled, repeatable, never mixed into measured history")
async def f12(lab: Lab) -> dict:
    env = {**lab.environ, "MONITOR_DATABASE_URL":
           f"postgresql://monitor_writer:{PW['MONITOR_DB_PASSWORD']}@127.0.0.1:5432/simucore"}
    until = datetime.now(UTC).replace(microsecond=0).isoformat()

    def backfill() -> str:
        r = subprocess.run([sys.executable, "-m", "monitor.backfill", "--hours", "6", "--step",
                            "20", "--until", until], cwd=HERE, env=env, capture_output=True,
                           text=True)
        assert r.returncode == 0, r.stderr[-400:]
        return r.stdout

    conn = await lab.db()
    q = "SELECT count(*) FROM metric_sample WHERE origin = $1"
    await asyncio.to_thread(backfill)
    first = await conn.fetchval(q, "synthetic")
    await asyncio.to_thread(backfill)
    second = await conn.fetchval(q, "synthetic")
    assert first == second == 8 * 6 * 180, (first, second)
    url = f"{PROM}/api/v1/metrics/history/account"
    p = {"minutes": 360, "step": 600, "metrics": "rps"}
    synth = await lab.get(url, params={**p, "origin": "synthetic"})
    real = await lab.get(url, params=p)
    n_real = sum(x["samples"] for x in real["points"])
    in_db = await conn.fetchval("SELECT count(*) FROM metric_sample WHERE origin='measured' AND "
                                "node_id='bank-account'")
    await conn.close()
    assert synth["origin"] == "synthetic" and sum(x["samples"] for x in synth["points"]) >= 1070
    assert real["origin"] == "measured" and n_real <= in_db + 2, (n_real, in_db)
    proc = await start_local(lab, 8202, "synthetic")
    try:
        d = await lab.get("http://127.0.0.1:8202/api/v1/metrics/current")
        tw = await lab.get("http://127.0.0.1:8202/api/v1/twin/samples")
        assert d["origin"] == tw["origin"] == "synthetic" and len(d["services"]) == 8
    finally:
        proc.terminate()
    return {"synthetic_rows": first, "added_by_second_run": second - first,
            "mixed_into_measured": 0}


@check("F13", "Soak: 5 minutes of continuous monitoring under load, no gap, no false alarm")
async def f13(lab: Lab) -> dict:
    begin = datetime.now(UTC)
    s0 = await lab.get(f"{PROM}/api/v1/monitoring/status")
    mem0 = lab.docker("stats", "--no-stream", "--format", "{{.MemUsage}}",
                      lab.container("monitoring"))
    load = asyncio.create_task(asyncio.to_thread(
        lab.ctl, "workload", "normal", "--rps", "8", "--duration", "290"))
    alarms: set[tuple] = set()
    worst = 0.0
    end = time.monotonic() + 300
    while time.monotonic() < end:
        for p in PIPES:
            alarms |= {(p, *e) for e in await lab.open_events(p)}
        worst = max(worst, (await lab.get(f"{PROM}/api/v1/monitoring/status"))["data_age_s"])
        await asyncio.sleep(2)
    await load
    await asyncio.sleep(3)
    s1 = await lab.get(f"{PROM}/api/v1/monitoring/status")
    mem1 = lab.docker("stats", "--no-stream", "--format", "{{.MemUsage}}",
                      lab.container("monitoring"))
    conn = await lab.db()
    rows = await conn.fetch("SELECT ts, count(*) n FROM metric_sample WHERE ts > $1 AND "
                            "origin='measured' GROUP BY ts ORDER BY ts", begin)
    await conn.close()
    gaps = [(b["ts"] - a["ts"]).total_seconds() for a, b in zip(rows, rows[1:], strict=False)]
    ticks, failed = s1["ticks"] - s0["ticks"], s1["failed_ticks"] - s0["failed_ticks"]
    facts = {"ticks": ticks, "failed_ticks": failed, "stored_ticks": len(rows),
             "longest_gap_s": round(max(gaps), 2), "oldest_data_s": worst,
             "alarms": sorted(map(str, alarms)), "memory": f"{mem0} -> {mem1}"}
    assert ticks >= 145 and failed == 0 and max(gaps) < 3.5 and worst < 5, facts
    assert all(r["n"] == 8 for r in rows) and not alarms, facts
    return facts


# --------------------------------------------------------------------------------- runner
async def start_local(lab: Lab, port: int, source: str) -> subprocess.Popen:
    log = (HERE / f"live_{source}.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "monitor.main:app", "--port", str(port), "--log-level",
         "warning"], cwd=HERE, env={**lab.environ, "MONITOR_SOURCE": source}, stdout=log,
        stderr=log)

    async def up():
        return (await lab.get(f"http://127.0.0.1:{port}/api/v1/monitoring/status"))["ticks"] > 1
    await lab.until(f"{source} monitor", up, 30)
    return proc


async def main() -> int:
    only = set(sys.argv[1:])
    unknown = only - {c for c, _, _ in CHECKS}
    if unknown:
        sys.exit(f"unknown check(s): {sorted(unknown)}")
    results, direct = [], None
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "mon.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in {
            "POSTGRES_USER": "simucore", "GRAFANA_ADMIN_USER": "admin",
            "SIMUCORE_TELEMETRY_ADAPTER": "monitoring", **PW}.items()), encoding="utf-8")
        lab = Lab(env_file)
        if any(r["live"] != "stopped" for r in json.loads(lab.ctl("status", "--json"))):
            sys.exit("The bank is running. Stop it first: bankctl.py down")
        if lab.docker("ps", "-q", "--filter", "publish=8200"):
            sys.exit("A monitoring stack is already running; stop it first.")
        try:
            lab.compose("up", "-d", "--build")
            await asyncio.to_thread(lab.ctl, "up")
            lab.load_key()
            direct = await start_local(lab, 8201, "direct")
            await lab.quiet(120)
            print("environment ready", flush=True)
            for code, title, fn in CHECKS:
                if only and code not in only:
                    continue
                t, facts, error = time.monotonic(), {}, None
                try:
                    await lab.quiet(60)
                    facts = await fn(lab)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:700]}"
                    if not isinstance(exc, AssertionError):
                        traceback.print_exc()
                    await lab.clear()
                    with contextlib.suppress(RuntimeError):
                        await asyncio.to_thread(lab.ctl, "up")
                results.append({"code": code, "title": title, "passed": error is None,
                                "seconds": round(time.monotonic() - t, 1), "facts": facts,
                                "error": error})
                print(f"{'PASS' if not error else 'FAIL'} {code} {title}\n     "
                      f"{error or json.dumps(facts)}", flush=True)
        finally:
            if direct:
                direct.terminate()
            await lab.http.aclose()
            lab.compose("down", "--volumes")
            lab.ctl("down")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if not only:
        write(results, passed)
    return 0 if passed == len(results) else 1


def write(results: list[dict], passed: int) -> None:
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    name = "pipelines_docker" if DOCKER else "pipelines"
    where = "Docker containers" if DOCKER else "local processes"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    (out / f"{name}.json").write_text(json.dumps(
        {"run_at": stamp, "bank": where, "passed": passed, "total": len(results),
         "results": results}, indent=2), encoding="utf-8", newline="\n")
    lines = ["# Monitoring pipeline results", "",
             f"Run at {stamp}, bank as {where}. **{passed} of {len(results)} passed.** Generated "
             "by `live/pipeline_check.py`.", "",
             "`truth` is measured independently of the monitoring system; `prometheus` and "
             "`direct` are its two pipelines.", "", "| | Check | Result | Measured |",
             "|---|---|---|---|"]
    lines += [f"| {r['code']} | {r['title']} | {'pass' if r['passed'] else '**FAIL**'} | "
              f"{(r['error'] or json.dumps(r['facts'])).replace('|', '/')} |" for r in results]
    (out / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if not shutil.which("docker"):
        sys.exit("docker is not on the PATH")
    sys.exit(asyncio.run(main()))
