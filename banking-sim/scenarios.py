"""Live scenario tests: eight real processes, real sockets, real kills.

    python scenarios.py             run everything, write results/scenarios.{json,md}
    python scenarios.py S04 S09     run a subset (results are printed, not written)

Uses its own data directory and refuses to run while the bank is already up.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import platform
import shutil
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

import workload
from bankcommon.config import ROOT, registry
from bankctl import START_ORDER, Bank

HOLD_TTL_S = 6
EDGES = {(d.source, d.target) for d in registry().dependencies}
SCENARIOS: list[tuple[str, str, Callable]] = []


def scenario(code: str, title: str):
    def add(fn):
        SCENARIOS.append((code, title, fn))
        return fn
    return add


class Lab:
    def __init__(self) -> None:
        self.bank = Bank(ROOT / ".runtime-scenarios", {"BANK_HOLD_TTL_S": str(HOLD_TTL_S)})
        # One shared client: building a client (and TLS context) per call costs ~250 ms and
        # would dominate every latency measured here.
        self.http = httpx.Client(timeout=12, trust_env=False)
        self.facts: dict = {}
        self.observed: set[tuple[str, str]] = set()

    # ------------------------------------------------------------- probes
    def note(self, **kw) -> None:
        self.facts.update(kw)

    def load(self, profile: str = "normal", rps: float = 10, secs: float = 6, **kw) -> dict:
        return asyncio.run(workload.run(profile, self.bank.key, rps=rps, duration_s=secs, **kw))

    async def load_async(self, during, rps: float, secs: float, mix: dict) -> dict:
        """Run a workload while ``during`` (blocking) breaks something in the middle."""
        job = asyncio.create_task(workload.run("normal", self.bank.key, rps=rps, duration_s=secs,
                                               mix=mix))
        await asyncio.sleep(secs * 0.4)
        await asyncio.to_thread(during)
        return await job

    def login(self, n: int = 1) -> dict:
        c = f"CUST{n:05d}"
        r = self.http.post(f"{self.bank.url('authentication')}/login",
                           json={"customer_id": c, "secret": f"synthetic-{c}"})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def get(self, service: str, path: str, auth: dict) -> httpx.Response:
        return self.http.get(self.bank.url(service) + path, headers=auth)

    def transfer(self, auth: dict, amount: int = 1000, n: int = 1) -> httpx.Response:
        return self._post("/transfers", auth, {"source": f"SIM-{n:05d}-01", "amount": amount,
                                               "destination": "SIM-00150-02"})

    def pay(self, auth: dict, amount: int = 1000, n: int = 1) -> httpx.Response:
        return self._post("/payments", auth, {"source": f"SIM-{n:05d}-01", "amount": amount,
                                              "beneficiary": "EXT-SHOP-1"})

    def _post(self, path: str, auth: dict, body: dict) -> httpx.Response:
        return self.http.post(self.bank.url("transaction") + path, json=body,
                              headers={**auth, "Idempotency-Key": uuid.uuid4().hex})

    def pids(self) -> dict[str, int]:
        out = {}
        for s in START_ORDER:
            info = self.bank.live(s)
            out[s] = info["pid"] if info else 0
        return out

    def collect_edges(self) -> None:
        for s in START_ORDER:
            try:
                for t in self.bank.telemetry(s)["outbound_calls"]:
                    self.observed.add((s, t))
            except (httpx.HTTPError, ValueError):
                pass

    def audit(self, settle: bool = False) -> dict:
        if settle:
            time.sleep(HOLD_TTL_S + 6)
        a = self.bank.integrity()
        assert a["ok"], f"money invariant broken: {a['problems']}"
        if settle:
            assert a["info"]["open_holds"] == 0, a["info"]
            assert a["info"]["transactions_processing"] == 0, a["info"]
            assert a["info"]["payments_authorized_not_settled"] == 0, a["info"]
        return a["info"]

    def recover(self) -> None:
        """Back to a healthy baseline: faults cleared, everything up, circuits closed."""
        self.bank.clear()
        self.bank.up()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            rows = self.bank.status()
            # Real traffic is what closes a half-open circuit, so send some.
            try:
                auth = self.login(199)
                ok = (self.get("customer", "/customers/me", auth).status_code == 200
                      and self.transfer(auth, 100, 199).status_code == 201
                      and self.pay(auth, 100, 199).status_code == 201)
            except httpx.HTTPError:
                ok = False
            if ok and all(r["readiness"] == "ready" for r in rows):
                return
            time.sleep(0.5)
        raise AssertionError(f"bank did not recover: {self.bank.status()}")


def path_of(r: httpx.Response) -> list[str]:
    return r.json()["error"]["failure_path"]


# ------------------------------------------------------------------ normal operation
@scenario("S01", "Health checks: all eight services live and ready")
def s01(lab: Lab):
    rows = lab.bank.status()
    assert len(rows) == 8 and all(r["readiness"] == "ready" for r in rows), rows
    lab.note(ready=8)


@scenario("S02", "Normal workload succeeds completely")
def s02(lab: Lab):
    r = lab.load("normal", rps=10, secs=15)
    lab.note(requests=r["requests"], outcomes=r["outcomes"], p95_ms=r["latency"]["p95_ms"])
    assert r["outcomes"]["failed"] == 0, r["failure_paths"]
    assert r["outcomes"]["rejected"] == 0, r["codes"]


@scenario("S03", "Peak workload (within capacity) is slower but does not fail")
def s03(lab: Lab):
    r = lab.load("peak", rps=workload.PROFILES["peak"].rps, secs=15)
    lab.note(requests=r["requests"], outcomes=r["outcomes"], p95_ms=r["latency"]["p95_ms"])
    assert r["failure_rate"] <= 0.02, r["failure_paths"]


@scenario("S04", "Overload beyond capacity is shed, money stays correct, service recovers")
def s04(lab: Lab):
    r = lab.load("peak", rps=120, secs=12)
    lab.note(requests=r["requests"], outcomes=r["outcomes"], p95_ms=r["latency"]["p95_ms"],
             overloaded=r["codes"].get("503 OVERLOADED", 0))
    assert r["outcomes"]["failed"] > 0 and r["outcomes"]["ok"] > 0
    lab.note(after=lab.audit(settle=True))


@scenario("S05", "Abnormal workload is refused by business rules, not by crashes")
def s05(lab: Lab):
    r = lab.load("abnormal", rps=12, secs=15)
    lab.note(outcomes=r["outcomes"], codes=r["codes"])
    assert r["outcomes"]["failed"] == 0, r["failure_paths"]
    for code in ("401 BAD_CREDENTIALS", "401 UNAUTHENTICATED", "404 NOT_FOUND", "403 FRAUD_DENY"):
        assert r["codes"].get(code), f"never saw {code}"
    ops = r["operations"]
    for kind in ("bad_login", "bad_token", "foreign_account", "fraud_large", "fraud_mule",
                 "overdraft", "declined_payment", "malformed"):
        assert kind not in ops or not ops[kind].get("ok"), f"{kind} was accepted: {ops[kind]}"


# -------------------------------------------------------------------------- downtime
@scenario("S06", "Crash of Account Management: transactions stop, customers and login work")
def s06(lab: Lab):
    auth = lab.login()
    lab.bank.kill("account")
    r = lab.transfer(auth)
    assert r.status_code == 503 and path_of(r) == ["transaction", "account"], r.text
    assert lab.get("customer", "/customers/me", auth).status_code == 200
    assert lab.login(2)
    lab.note(failure_path=path_of(r))


@scenario("S07", "Soft downtime of Fraud Detection: small transfers degrade, large ones stop")
def s07(lab: Lab):
    auth = lab.login()
    lab.bank.fault("fraud", "downtime", 60)
    assert lab.bank.live("fraud")["status"] == "down"
    small, large = lab.transfer(auth, 1000), lab.transfer(auth, 90_000)
    assert small.status_code == 201 and small.json()["detail"]["fraud"] == "skipped_unavailable"
    assert large.status_code == 503 and path_of(large) == ["transaction", "fraud"], large.text
    lab.note(small="completed unscreened", large="refused 503")


@scenario("S08", "Payment Gateway stopped: payments fail with funds released, transfers work")
def s08(lab: Lab):
    auth = lab.login(3)
    before = lab.get("account", "/accounts/SIM-00003-01", auth).json()
    lab.bank.stop("payment")
    r = lab.pay(auth, 5000, 3)
    assert r.status_code == 503 and path_of(r) == ["transaction", "payment"], r.text
    assert lab.get("account", "/accounts/SIM-00003-01", auth).json() == before
    assert lab.transfer(auth, 100, 3).status_code == 201
    lab.note(failure_path=path_of(r))


@scenario("S09", "Notification Service down: payments still complete")
def s09(lab: Lab):
    auth = lab.login(4)
    lab.bank.kill("notification")
    r = lab.pay(auth, 700, 4)
    assert r.status_code == 201 and r.json()["detail"]["receipt"] == "unavailable", r.text
    lab.note(receipt="unavailable", payment="completed")


@scenario("S10", "Authentication down: every customer request is blocked")
def s10(lab: Lab):
    auth = lab.login()
    lab.bank.stop("authentication")
    paths = {}
    for service, path in (("customer", "/customers/me"), ("account", "/accounts")):
        r = lab.get(service, path, auth)
        assert r.status_code == 503, r.text
        paths[service] = path_of(r)
    r = lab.transfer(auth)
    assert r.status_code == 503
    paths["transaction"] = path_of(r)
    assert all(p[-1] == "authentication" for p in paths.values()), paths
    lab.note(failure_paths=paths)


# --------------------------------------------------------------------------- latency
@scenario("S11", "Moderate database latency slows everything but nothing fails")
def s11(lab: Lab):
    base = lab.load("normal", rps=5, secs=6, seed=1)
    lab.bank.fault("database", "latency", 60, latency_ms=150, jitter=0.1)
    slow = lab.load("normal", rps=5, secs=6, seed=1)
    lab.note(baseline_p50_ms=base["latency"]["p50_ms"], slow_p50_ms=slow["latency"]["p50_ms"],
             failed=slow["outcomes"]["failed"])
    assert slow["outcomes"]["failed"] == 0, slow["failure_paths"]
    assert slow["latency"]["p50_ms"] > base["latency"]["p50_ms"] + 100


@scenario("S12", "Severe database latency becomes timeouts, then open circuits that fail fast")
def s12(lab: Lab):
    auth = lab.login()
    lab.bank.fault("database", "latency", 60, latency_ms=3000, jitter=0)
    times = []
    for _ in range(7):
        t = time.perf_counter()
        r = lab.get("account", "/accounts", auth)
        times.append(round((time.perf_counter() - t) * 1000))
        assert r.status_code == 503 and path_of(r) == ["account", "database"], r.text
    circuit = lab.bank.telemetry("account")["circuits"]["database"]
    lab.note(first_ms=times[0], last_ms=times[-1], circuit=circuit)
    assert times[0] > 900 and times[-1] < 150 and circuit == "open", (times, circuit)


# --------------------------------------------------------------- resource exhaustion
@scenario("S13", "CPU exhaustion in Transaction Processing: moderate slows it, severe stops it")
def s13(lab: Lab):
    auths = {n: lab.login(n) for n in range(20, 40)}
    customer = itertools.cycle(auths)

    def phase(magnitude: float | None, n: int) -> dict:
        if magnitude:
            lab.bank.fault("transaction", "resource_exhaustion", 60, mode="cpu",
                           magnitude=magnitude)
        lab.bank.telemetry("transaction")  # start a fresh CPU measurement interval
        times, codes = [], []
        for _ in range(n):
            t = time.perf_counter()
            codes.append(lab.transfer(auths[c := next(customer)], 100, c).status_code)
            times.append((time.perf_counter() - t) * 1000)
        return {"median_ms": round(sorted(times)[n // 2]), "ok": codes.count(201),
                "refused_503": codes.count(503),
                "cpu_pct": lab.bank.telemetry("transaction")["metrics"]["cpu_pct"]}

    phase(None, 10)  # warm up
    base = phase(None, 30)
    moderate = phase(0.95, 30)
    severe = phase(1.0, 8)
    lab.note(baseline=base, moderate=moderate, severe=severe)
    assert severe["ok"] + severe["refused_503"] == 8, severe
    assert base["ok"] == 30 and moderate["ok"] + moderate["refused_503"] == 30
    assert moderate["ok"] >= 24, "a moderate burn should slow the service, not stop it"
    assert moderate["median_ms"] > base["median_ms"] * 1.2
    # Refused requests give up early, so severe is judged by refusals, not by its median.
    assert severe["cpu_pct"] > 70 and severe["refused_503"] >= 1


# --- memory and worker slots
@scenario("S14", "Memory exhaustion in Account Management sheds requests, then recovers")
def s14(lab: Lab):
    auth = lab.login()
    lab.bank.fault("account", "resource_exhaustion", 60, mode="memory")
    time.sleep(0.5)
    r = lab.get("account", "/accounts", auth)
    mem = lab.bank.telemetry("account")["metrics"]["mem_pct"]
    assert r.status_code == 503 and r.json()["error"]["code"] == "RESOURCE_EXHAUSTED", r.text
    assert mem >= 95
    assert lab.bank.live("account")["status"] == "alive"
    lab.bank.clear("account")
    time.sleep(0.6)
    assert lab.get("account", "/accounts", auth).status_code == 200
    lab.note(mem_pct=mem, mem_after=lab.bank.telemetry("account")["metrics"]["mem_pct"])


@scenario("S15", "Worker-slot exhaustion in the Payment Gateway sheds payments")
def s15(lab: Lab):
    auth = lab.login(6)
    lab.bank.fault("payment", "resource_exhaustion", 60, mode="slots")
    r = lab.pay(auth, 100, 6)
    assert r.status_code == 503 and path_of(r) == ["transaction", "payment"], r.text
    lab.note(message=r.json()["error"]["message"])


# --------------------------------------------------------------- database connections
@scenario("S16", "Database refuses connections: root cause visible, token checks still work")
def s16(lab: Lab):
    auth = lab.login()
    lab.bank.fault("database", "db_connection_failure", 60, mode="refuse")
    r = lab.get("account", "/accounts", auth)
    assert r.status_code == 503 and path_of(r) == ["account", "database"], r.text
    assert "DB_CONNECTION_FAILED" in r.json()["error"]["message"]
    v = httpx.post(f"{lab.bank.url('authentication')}/internal/verify", trust_env=False,
                   headers=lab.bank.headers, json={"token": auth["Authorization"][7:]})
    assert v.status_code == 200
    lab.note(message=r.json()["error"]["message"])


@scenario("S17", "Database connection pool exhausted: queries time out waiting")
def s17(lab: Lab):
    auth = lab.login()
    lab.bank.fault("database", "db_connection_failure", 60, mode="exhaust")
    r = lab.get("customer", "/customers/me", auth)
    assert r.status_code == 503 and path_of(r) == ["customer", "database"], r.text
    assert "DB_POOL_EXHAUSTED" in r.json()["error"]["message"], r.text
    ready = httpx.get(f"{lab.bank.url('database')}/health/ready", trust_env=False, timeout=5)
    assert ready.status_code == 503
    lab.note(message=r.json()["error"]["message"])


# ---------------------------------------------------- independence and durability
@scenario("S18", "Every service can be stopped, started and restarted alone")
def s18(lab: Lab):
    modes = {}
    for s in START_ORDER:
        before = lab.pids()
        modes[s] = lab.bank.stop(s)
        assert lab.bank.live(s) is None, f"{s} still answers"
        during = lab.pids()
        assert {k: v for k, v in during.items() if k != s} == \
               {k: v for k, v in before.items() if k != s}, f"stopping {s} disturbed others"
        lab.bank.start(s)
        started = lab.pids()[s]
        lab.bank.restart(s)
        after = lab.pids()
        assert len({before[s], started, after[s]}) == 3, f"{s} kept its process"
        assert {k: v for k, v in after.items() if k != s} == \
               {k: v for k, v in before.items() if k != s}
    lab.note(stop_modes=modes)
    assert set(modes.values()) == {"graceful"}, modes


@scenario("S19", "Services start in reverse order, before their dependencies exist")
def s19(lab: Lab):
    lab.bank.down()
    for s in reversed(START_ORDER):
        lab.bank.start(s)
    lab.note(order=list(reversed(START_ORDER)))


@scenario("S20", "Balances survive a database crash and restart")
def s20(lab: Lab):
    auth = lab.login(7)
    assert lab.transfer(auth, 4321, 7).status_code == 201
    before = lab.get("account", "/accounts", auth).json()
    lab.bank.kill("database")
    lab.bank.start("database")
    deadline = time.monotonic() + 15
    while (r := lab.get("account", "/accounts", auth)).status_code != 200:
        assert time.monotonic() < deadline, r.text
        time.sleep(0.5)
    assert r.json() == before
    lab.note(balances_identical=True)


MONEY_MIX = {"transfer": 50, "payment": 50}


def check_acknowledged(lab: Lab, report: dict, committed: int) -> None:
    """Every transaction a customer was told succeeded must be in the ledger."""
    acked = report["acknowledged_transactions"]
    missing = []
    for txn_id in acked:
        r = lab.http.get(f"{lab.bank.url('database')}/internal/transactions/{txn_id}",
                         headers=lab.bank.headers)
        if r.status_code != 200 or r.json()["status"] != "completed":
            missing.append(txn_id)
    lab.note(acknowledged=len(acked), acknowledged_but_missing=len(missing),
             committed_without_acknowledgement=committed - len(acked))
    assert acked and not missing, missing


@scenario("S21", "Transaction Processing killed mid-load: no money lost or created")
def s21(lab: Lab):
    def crash():
        lab.bank.kill("transaction")
        lab.bank.start("transaction")
    start = lab.audit()["transactions_completed"]
    r = asyncio.run(lab.load_async(crash, rps=12, secs=10, mix=MONEY_MIX))
    info = lab.audit(settle=True)
    check_acknowledged(lab, r, info["transactions_completed"] - start)
    lab.note(outcomes=r["outcomes"], after=info)
    assert r["outcomes"]["failed"] > 0 and r["outcomes"]["ok"] > 0


@scenario("S22", "Database killed mid-load: no money lost or created")
def s22(lab: Lab):
    def crash():
        lab.bank.kill("database")
        lab.bank.start("database")
    start = lab.audit()["transactions_completed"]
    r = asyncio.run(lab.load_async(crash, rps=12, secs=12, mix=MONEY_MIX))
    info = lab.audit(settle=True)
    check_acknowledged(lab, r, info["transactions_completed"] - start)
    lab.note(outcomes=r["outcomes"], after=info)
    assert r["outcomes"]["failed"] > 0 and r["outcomes"]["ok"] > 0


@scenario("S23", "Observed service calls match the declared dependency map exactly")
def s23(lab: Lab):
    lab.load("normal", rps=10, secs=8)
    lab.collect_edges()
    lab.note(observed=len(lab.observed), declared=len(EDGES))
    assert lab.observed == EDGES, {"undeclared": sorted(lab.observed - EDGES),
                                   "never_used": sorted(EDGES - lab.observed)}


# --------------------------------------------------------------------------- runner
def run(selected: set[str]) -> list[dict]:
    lab = Lab()
    if any(lab.bank.live(s) for s in START_ORDER):
        sys.exit("The bank is already running. Stop it first: python bankctl.py down")
    shutil.rmtree(lab.bank.runtime, ignore_errors=True)
    lab.bank.runtime.mkdir()
    results = []
    try:
        lab.bank.up()
        for code, title, fn in SCENARIOS:
            if selected and code not in selected:
                continue
            lab.facts, start, error = {}, time.perf_counter(), None
            try:
                lab.recover()
                fn(lab)
                lab.collect_edges()
                lab.recover()
                lab.audit()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            results.append({"code": code, "title": title, "passed": error is None,
                            "seconds": round(time.perf_counter() - start, 1),
                            "facts": lab.facts, "error": error})
            print(f"{'PASS' if error is None else 'FAIL'} {code} {title} "
                  f"{json.dumps(lab.facts)[:260]}", flush=True)
    finally:
        lab.bank.down()
    return results


def write(results: list[dict]) -> None:
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    passed = sum(r["passed"] for r in results)
    (out / "scenarios.json").write_text(json.dumps(
        {"run_at": stamp, "platform": platform.platform(), "python": platform.python_version(),
         "mode": "local processes", "passed": passed, "total": len(results),
         "results": results}, indent=2), encoding="utf-8", newline="\n")
    lines = ["# Live scenario results", "",
             f"Run at {stamp} on {platform.platform()}, Python {platform.python_version()}, as "
             "local processes (not containers).", "",
             f"**{passed} of {len(results)} passed.** Generated by `python scenarios.py`.", "",
             "| | Scenario | Result | Time | Measured |", "|---|---|---|---|---|"]
    for r in results:
        facts = r["error"] or "; ".join(f"{k}={json.dumps(v)}" for k, v in r["facts"].items())
        lines.append(f"| {r['code']} | {r['title']} | {'pass' if r['passed'] else '**FAIL**'} "
                     f"| {r['seconds']} s | {facts.replace('|', '/')} |")
    (out / "scenarios.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    chosen = set(sys.argv[1:])
    unknown = chosen - {c for c, _, _ in SCENARIOS}
    if unknown:
        sys.exit(f"unknown scenario(s): {sorted(unknown)}")
    done = run(chosen)
    if not chosen:
        write(done)
    print(f"\n{sum(r['passed'] for r in done)}/{len(done)} passed")
    sys.exit(0 if all(r["passed"] for r in done) else 1)
