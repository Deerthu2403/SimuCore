"""Synthetic workload generator. Talks only to the simulated services (see check_simulated_url).

Open-loop Poisson arrivals: requests are sent on schedule even when the bank is slow, because
real customers do not wait for each other. Results separate three outcomes:

  ok        the request did what was asked
  rejected  a correct business refusal (bad credentials, insufficient funds, fraud, not found...)
  failed    the system let the customer down (5xx, timeout, connection refused)
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass

import httpx

from bankcommon.config import registry, service_url

MAX_IN_FLIGHT = 400
REJECTED = {400, 401, 402, 403, 404, 409, 422}


@dataclass(frozen=True)
class Profile:
    rps: float
    mix: dict[str, float]

    def rate(self, t: float, duration: float, base: float) -> float:
        return base


class PeakProfile(Profile):
    """Ramps up to full load and back down (salary day / festival sale)."""

    def rate(self, t: float, duration: float, base: float) -> float:
        return base * (0.35 + 0.65 * math.sin(math.pi * min(t / duration, 1.0)))


class BurstProfile(Profile):
    """Irregular: short 3x bursts on top of a low base rate."""

    def rate(self, t: float, duration: float, base: float) -> float:
        return base * (3.0 if int(t) % 5 == 0 else 0.6)


EVERYDAY = {"account_query": 50, "customer_query": 10, "transfer": 18, "payment": 12,
            "fraud_check": 7, "login": 3}
PROFILES: dict[str, Profile] = {
    "normal": Profile(10, EVERYDAY),
    "peak": PeakProfile(40, {**EVERYDAY, "transfer": 24, "payment": 18}),
    "abnormal": BurstProfile(15, {
        "account_query": 18, "transfer": 10, "payment": 8,
        "bad_login": 14, "bad_token": 10, "foreign_account": 12, "overdraft": 8,
        "fraud_large": 8, "fraud_mule": 5, "declined_payment": 4, "malformed": 3}),
}


class Workload:
    def __init__(self, internal_key: str, seed: int, customers: int = 200) -> None:
        self.rng = random.Random(seed)
        self.customers = customers
        self.urls = {n: service_url(n) for n in registry().services}
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=1.0), trust_env=False,
            limits=httpx.Limits(max_connections=MAX_IN_FLIGHT, max_keepalive_connections=100))
        self.internal = {"X-Internal-Key": internal_key}
        self.tokens: dict[int, str] = {}
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.outcomes: dict[str, Counter] = defaultdict(Counter)
        self.codes: Counter[str] = Counter()
        self.paths: Counter[str] = Counter()
        self.timeline: dict[int, Counter] = defaultdict(Counter)
        self.acknowledged: list[str] = []
        self.dropped = 0

    # ------------------------------------------------------------------- users
    async def login(self, n: int) -> httpx.Response:
        cust = f"CUST{n:05d}"
        r = await self.http.post(f"{self.urls['authentication']}/login",
                                 json={"customer_id": cust, "secret": f"synthetic-{cust}"})
        if r.status_code == 200:
            self.tokens[n] = r.json()["token"]
        return r

    async def warm_up(self) -> None:
        gate = asyncio.Semaphore(8)

        async def one(n: int) -> None:
            async with gate:
                await self.login(n)

        await asyncio.gather(*(one(n) for n in range(1, self.customers + 1)))

    def _auth(self, n: int) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens.get(n, 'none')}"}

    async def _as_customer(self, n: int, send) -> httpx.Response:
        """Send; on 401 log in again once (tokens die when authentication restarts)."""
        r = await send(self._auth(n))
        if r.status_code == 401 and (await self.login(n)).status_code == 200:
            r = await send(self._auth(n))
        return r

    # -------------------------------------------------------------- operations
    async def op(self, kind: str) -> httpx.Response:
        rng, u, h = self.rng, self.urls, self.http
        n, other = rng.randint(1, self.customers), rng.randint(1, self.customers)
        mine, theirs = f"SIM-{n:05d}-01", f"SIM-{other:05d}-02"
        amount = min(int(rng.lognormvariate(8.6, 1.0)) + 100, 200_000)
        key = {"Idempotency-Key": uuid.uuid4().hex}
        if kind == "login":
            return await self.login(n)
        if kind == "bad_login":
            return await h.post(f"{u['authentication']}/login", json={
                "customer_id": f"CUST{n:05d}", "secret": "synthetic-wrong"})
        if kind == "bad_token":
            return await h.get(f"{u['account']}/accounts",
                               headers={"Authorization": "Bearer forged.0000"})
        if kind == "account_query":
            path = rng.choice(["/accounts", f"/accounts/{mine}"])
            return await self._as_customer(
                n, lambda a: h.get(f"{u['account']}{path}", headers=a))
        if kind == "foreign_account":
            victim = n % self.customers + 1
            return await self._as_customer(
                n, lambda a: h.get(f"{u['account']}/accounts/SIM-{victim:05d}-01", headers=a))
        if kind == "customer_query":
            return await self._as_customer(
                n, lambda a: h.get(f"{u['customer']}/customers/me", headers=a))
        if kind == "fraud_check":
            return await h.post(f"{u['fraud']}/fraud/checks", headers=self.internal, json={
                "customer_id": f"CUST{n:05d}", "amount": amount, "destination": theirs})
        if kind in ("payment", "declined_payment", "fraud_mule"):
            tag = {"payment": "SHOP", "declined_payment": "DECLINE", "fraud_mule": "MULE"}[kind]
            body = {"source": mine, "beneficiary": f"EXT-{tag}-{rng.randint(1, 50)}",
                    "amount": amount}
            return await self._as_customer(n, lambda a: h.post(
                f"{u['transaction']}/payments", headers={**a, **key}, json=body))
        body = {"source": mine, "destination": theirs, "amount": amount}
        if kind == "overdraft":
            r = await self._as_customer(
                n, lambda a: h.get(f"{u['account']}/accounts/{mine}", headers=a))
            if r.status_code != 200:
                return r
            body["amount"] = r.json()["available"] + 1
        elif kind == "fraud_large":
            body["amount"] = rng.randint(2_000_000, 9_000_000)
        elif kind == "malformed":
            body["amount"] = -5
        return await self._as_customer(n, lambda a: h.post(
            f"{u['transaction']}/transfers", headers={**a, **key}, json=body))

    async def fire(self, kind: str, second: int) -> None:
        start = time.perf_counter()
        try:
            r = await self.op(kind)
            code = str(r.status_code)
            if r.status_code < 400:
                outcome = "ok"
                if r.status_code == 201:
                    self.acknowledged.append(r.json()["id"])
            elif r.status_code in REJECTED:
                outcome = "rejected"
                code = f"{code} {r.json()['error']['code']}"
            else:
                outcome = "failed"
                err = r.json()["error"]
                code = f"{code} {err['code']}"
                self.paths[" -> ".join(err["failure_path"])] += 1
        except httpx.HTTPError as exc:
            outcome, code = "failed", type(exc).__name__
            self.paths[f"client:{code}"] += 1
        except (ValueError, KeyError, TypeError):
            outcome, code = "failed", "unreadable error body"
        self.samples[kind].append((time.perf_counter() - start) * 1000)
        self.outcomes[kind][outcome] += 1
        self.codes[code] += 1
        self.timeline[second][outcome] += 1

    async def drive(self, profile: Profile, rps: float, duration_s: float) -> dict:
        kinds, weights = list(profile.mix), list(profile.mix.values())
        tasks: set[asyncio.Task] = set()
        start, t = time.perf_counter(), 0.0
        while True:
            t += self.rng.expovariate(max(profile.rate(t, duration_s, rps), 0.1))
            if t >= duration_s:
                break
            await asyncio.sleep(max(0.0, start + t - time.perf_counter()))
            if len(tasks) >= MAX_IN_FLIGHT:
                self.dropped += 1
                continue
            kind = self.rng.choices(kinds, weights)[0]
            task = asyncio.create_task(self.fire(kind, int(t)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.wait(tasks)
        return self.report(time.perf_counter() - start)

    # ------------------------------------------------------------------ report
    def report(self, elapsed: float) -> dict:
        def stats(xs: list[float]) -> dict:
            s = sorted(xs)
            pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))], 1)  # noqa: E731
            return {"count": len(s), "p50_ms": pick(0.5), "p95_ms": pick(0.95),
                    "p99_ms": pick(0.99), "max_ms": round(s[-1], 1)}

        everything = [x for xs in self.samples.values() for x in xs]
        total = Counter()
        for c in self.outcomes.values():
            total.update(c)
        n = sum(total.values())
        return {
            "requests": n, "elapsed_s": round(elapsed, 2), "rps": round(n / elapsed, 1),
            "dropped_by_generator": self.dropped,
            "outcomes": {k: total[k] for k in ("ok", "rejected", "failed")},
            "failure_rate": round(total["failed"] / n, 4) if n else None,
            "latency": stats(everything) if everything else None,
            "operations": {k: {**stats(self.samples[k]), **self.outcomes[k]}
                           for k in sorted(self.samples)},
            "codes": dict(self.codes.most_common()),
            "failure_paths": dict(self.paths.most_common(12)),
        }


async def run(profile: str, internal_key: str, *, rps: float | None = None,
              duration_s: float = 20, seed: int = 42, customers: int = 200,
              mix: dict[str, float] | None = None) -> dict:
    p = PROFILES[profile]
    if mix:
        p = type(p)(p.rps, mix)
    w = Workload(internal_key, seed, customers)
    try:
        await w.warm_up()
        report = await w.drive(p, rps or p.rps, duration_s)
        return {"profile": profile, "offered_rps": rps or p.rps, **report,
                **({"acknowledged_transactions": w.acknowledged} if mix else {})}
    finally:
        await w.http.aclose()
