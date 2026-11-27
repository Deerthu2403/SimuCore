"""Record what monitoring reports while the simulated bank degrades, fails and recovers.

    python live/record.py calibration|dev|test [--limit N]

Needs the Docker stack; starts and stops the simulated bank itself. Each run becomes one file in
``recordings/<set>/``: the monitoring messages exactly as the AI service would receive them, plus a
log of what was really done to the bank. Which failures happen and when is decided by the seed, not
by anything a model says.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "banking-sim"
OUT = ROOT / "intelligence" / "recordings"
sys.path.insert(0, str(BANK))

import bankctl  # noqa: E402
import workload as wl  # noqa: E402

MONITORING = "ws://127.0.0.1:8200/ws/metrics"
KEEP = ("cpu_pct", "mem_pct", "latency_ms", "latency_mean_ms", "rps", "error_rate", "net_rx_bps",
        "net_tx_bps", "db_response_ms", "active_connections", "queue_length", "in_flight",
        "db_pool_in_use", "db_pool_size", "db_failure_rate", "availability")


class Varying(wl.Profile):
    """Normal customer behaviour at a rate that can be changed while it runs."""

    current = 8.0

    def rate(self, t: float, duration: float, base: float) -> float:
        return Varying.current


class Lab:
    def __init__(self) -> None:
        self.bank = bankctl.make_bank()
        self.ticks: list[dict] = []
        self.events: list[dict] = []
        self.recording = False
        self.last: dict[str, dict] = {}

    # ------------------------------------------------------------- listening
    async def listen(self) -> None:
        while True:
            try:
                async with websockets.connect(MONITORING, max_size=2**22) as ws:
                    async for raw in ws:
                        self.handle(json.loads(raw))
            except (OSError, websockets.WebSocketException):
                await asyncio.sleep(1)

    def handle(self, m: dict) -> None:
        if not m.get("fresh"):
            return
        self.last = {s["service"]: s for s in m["services"]}
        if self.recording and (not self.ticks or m["sequence"] != self.ticks[-1]["n"]):
            self.ticks.append({
                "n": m["sequence"], "ts": m["collected_at"], "origin": m["origin"],
                "services": {s["service"]: {
                    "status": s["status"], "deployment": s["deployment"],
                    "metrics": {k: s["metrics"][k] for k in KEEP if k in s["metrics"]}}
                    for s in m["services"]}})

    def note(self, what: str, **detail: object) -> None:
        self.events.append({"ts": datetime.now(UTC).isoformat(), "what": what, **detail})

    # ------------------------------------------------------------- acting
    async def do(self, fn, *args, **kw):
        return await asyncio.to_thread(fn, *args, **kw)

    async def fault(self, service: str, type_: str, duration: float, **kw) -> None:
        try:
            await self.do(self.bank.fault, service, type_, duration, **kw)
        except Exception as exc:  # an overwhelmed service may not answer in time
            self.note("fault_not_confirmed", service=service, error=type(exc).__name__)

    async def clear(self) -> None:
        try:
            await self.do(self.bank.clear, None)
        except Exception as exc:
            self.note("clear_not_confirmed", error=type(exc).__name__)

    def up(self, service: str) -> bool:
        return self.last.get(service, {}).get("status") == "up"

    def value(self, service: str, metric: str) -> float:
        return (self.last.get(service, {}).get("metrics") or {}).get(metric) or 0.0

    def failing(self, service: str) -> bool:
        s = self.last.get(service, {})
        return s.get("status") != "up" or self.value(service, "error_rate") >= 0.5


# ------------------------------------------------------------------- what can happen
async def ramp(lab: Lab, rng: random.Random, seconds: float, apply, start: float, end: float,
               stop=lambda: False) -> None:
    """Make something gradually worse. The curve, like in reality, is not always a line."""
    bend = rng.uniform(0.7, 2.0)
    t0 = time.monotonic()
    while (x := (time.monotonic() - t0) / seconds) < 1 and not stop():
        await apply(start + (end - start) * x**bend)
        await asyncio.sleep(rng.uniform(5, 8))
    if not stop():
        await apply(end)


async def hold(lab: Lab, service: str, seconds: float) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds and not lab.failing(service):
        await asyncio.sleep(1)
    await asyncio.sleep(12)


async def cpu_ramp(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(110, 220),
               lambda x: lab.fault(s, "resource_exhaustion", 60, mode="cpu", magnitude=x),
               0.04, rng.uniform(0.75, 1.0))
    await hold(lab, s, 25)


async def mem_leak(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(110, 220),
               lambda x: lab.fault(s, "resource_exhaustion", 60, mode="memory", magnitude=x),
               0.22, 1.0)
    await hold(lab, s, 25)


async def leak_crash(lab: Lab, rng: random.Random, s: str) -> None:
    limit = rng.uniform(82, 93)
    await ramp(lab, rng, rng.uniform(110, 220),
               lambda x: lab.fault(s, "resource_exhaustion", 60, mode="memory", magnitude=x),
               0.22, 1.0, stop=lambda: lab.value(s, "mem_pct") >= limit)
    lab.note("killed", service=s, why="out of memory")
    await lab.do(lab.bank.kill, s)
    await asyncio.sleep(15)


async def latency_ramp(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(110, 220),
               lambda x: lab.fault(s, "latency", 60, latency_ms=x, jitter=0.3),
               10, rng.uniform(2500, 5000))
    await hold(lab, s, 15)


async def db_exhaust(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(110, 200),
               lambda x: lab.fault("database", "db_connection_failure", 60, mode="exhaust",
                                   magnitude=max(0.05, x)), 0.1, 1.0)
    await hold(lab, "database", 20)


async def slots_ramp(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(100, 180),
               lambda x: lab.fault(s, "resource_exhaustion", 60, mode="slots", magnitude=x),
               0.2, 1.0)
    await hold(lab, s, 25)


async def load_ramp(lab: Lab, rng: random.Random, s: str) -> None:
    base, peak = Varying.current, rng.uniform(95, 150)

    async def set_rate(x: float) -> None:
        Varying.current = x
    await ramp(lab, rng, rng.uniform(120, 200), set_rate, base, peak)
    await asyncio.sleep(20)
    Varying.current = base


async def crash(lab: Lab, rng: random.Random, s: str) -> None:
    await asyncio.sleep(rng.uniform(20, 60))
    lab.note("killed", service=s, why="no warning")
    await lab.do(lab.bank.kill, s)
    await asyncio.sleep(18)


async def downtime(lab: Lab, rng: random.Random, s: str) -> None:
    await asyncio.sleep(rng.uniform(20, 60))
    lab.note("downtime", service=s)
    await lab.fault(s, "downtime", 18)
    await asyncio.sleep(18)


# harmless: looks like the beginning of something, and is not
async def calm(lab: Lab, rng: random.Random, s: str) -> None:
    await asyncio.sleep(rng.uniform(100, 150))


async def blip_cpu(lab: Lab, rng: random.Random, s: str) -> None:
    for _ in range(3):
        await lab.fault(s, "resource_exhaustion", rng.uniform(8, 16), mode="cpu",
                        magnitude=rng.uniform(0.08, 0.2))
        await asyncio.sleep(rng.uniform(35, 50))


async def blip_latency(lab: Lab, rng: random.Random, s: str) -> None:
    for _ in range(3):
        await lab.fault(s, "latency", rng.uniform(8, 16), latency_ms=rng.uniform(20, 120))
        await asyncio.sleep(rng.uniform(35, 50))


async def load_wave(lab: Lab, rng: random.Random, s: str) -> None:
    base = Varying.current

    async def set_rate(x: float) -> None:
        Varying.current = x
    await ramp(lab, rng, rng.uniform(50, 80), set_rate, base, rng.uniform(22, 32))
    await asyncio.sleep(rng.uniform(15, 30))
    await ramp(lab, rng, rng.uniform(40, 60), set_rate, Varying.current, base)


async def partial_leak(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(80, 120),
               lambda x: lab.fault(s, "resource_exhaustion", 40, mode="memory", magnitude=x),
               0.22, rng.uniform(0.4, 0.6))
    await lab.clear()
    await asyncio.sleep(25)


async def partial_cpu(lab: Lab, rng: random.Random, s: str) -> None:
    await ramp(lab, rng, rng.uniform(60, 90),
               lambda x: lab.fault(s, "resource_exhaustion", 30, mode="cpu", magnitude=x),
               0.04, rng.uniform(0.15, 0.22))
    await lab.clear()
    await asyncio.sleep(25)


KINDS = {f.__name__: f for f in (
    cpu_ramp, mem_leak, leak_crash, latency_ramp, db_exhaust, slots_ramp, load_ramp, crash,
    downtime, calm, blip_cpu, blip_latency, load_wave, partial_leak, partial_cpu)}
SERVICES = ("authentication", "customer", "account", "transaction", "payment", "fraud",
            "notification", "database")

# set -> (seed, [(kind, how often)])
PLANS = {
    "calibration": (100, [("calm", 8)]),
    "dev": (200, [("cpu_ramp", 3), ("mem_leak", 2), ("leak_crash", 2), ("latency_ramp", 3),
                  ("db_exhaust", 1), ("load_ramp", 1), ("slots_ramp", 1), ("crash", 1),
                  ("calm", 1), ("blip_cpu", 1), ("blip_latency", 1), ("load_wave", 1),
                  ("partial_leak", 1), ("partial_cpu", 1)]),
    "test": (900, [("cpu_ramp", 4), ("mem_leak", 4), ("leak_crash", 3), ("latency_ramp", 3),
                   ("db_exhaust", 2), ("load_ramp", 2), ("slots_ramp", 1), ("crash", 2),
                   ("downtime", 1), ("calm", 2), ("blip_cpu", 1), ("blip_latency", 1),
                   ("load_wave", 2), ("partial_leak", 1), ("partial_cpu", 1)]),
}
# a fault in a service nobody calls has no effect to see; database faults of these kinds hit all
TARGETS = {"cpu_ramp": ("account", "transaction", "fraud", "payment", "customer"),
           "latency_ramp": ("account", "fraud", "payment", "authentication", "customer"),
           "slots_ramp": ("account", "fraud", "payment")}
BASE_RATES = (2.5, 4, 6, 8, 11, 14, 18, 23)


def plan(name: str) -> list[dict]:
    seed, kinds = PLANS[name]
    rng = random.Random(seed)
    runs = []
    for kind, n in kinds:
        choices = list(TARGETS.get(kind, SERVICES))
        rng.shuffle(choices)
        for i in range(n):
            runs.append({"kind": kind, "service": choices[i % len(choices)],
                         "seed": rng.randrange(10**6),
                         "rate": BASE_RATES[i] if name == "calibration"
                         else round(rng.uniform(5, 14), 1)})
    if name != "calibration":
        rng.shuffle(runs)
    return runs


# -------------------------------------------------------------------------- running
async def settle(lab: Lab) -> None:
    await lab.clear()
    await lab.do(lab.bank.up)
    t0 = time.monotonic()
    good = 0
    while good < 8 and time.monotonic() - t0 < 120:
        await asyncio.sleep(2)
        good = good + 1 if all(
            lab.up(s) and lab.value(s, "error_rate") < 0.02 and lab.value(s, "cpu_pct") < 60
            for s in SERVICES) else 0


async def one(lab: Lab, run: dict, path: Path) -> None:
    rng = random.Random(run["seed"])
    Varying.current = run["rate"]
    await settle(lab)
    lab.ticks, lab.events, lab.recording = [], [], True
    lab.note("started")
    await asyncio.sleep(rng.uniform(50, 75))  # normal operation first
    lab.note("onset", kind=run["kind"], service=run["service"])
    await KINDS[run["kind"]](lab, rng, run["service"])
    lab.note("ended")
    await lab.clear()
    await lab.do(lab.bank.up)
    Varying.current = run["rate"]
    await asyncio.sleep(35)
    lab.recording = False
    times = [datetime.fromisoformat(t["ts"]).timestamp() for t in lab.ticks]
    hole = max((b - a for a, b in zip(times, times[1:], strict=False)), default=999)
    if hole > 8:  # the computer slept, or monitoring stopped: worthless, and the load is now wrong
        raise SystemExit(f"{path.name}: {hole:.0f} s without data, discarded; start again")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"run": run, "recorded_at": datetime.now(UTC).isoformat(), "tick_s": 2.0,
           "events": lab.events, "ticks": lab.ticks}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    worst = {s: sum(1 for t in lab.ticks if t["services"][s]["status"] != "up"
                    or (t["services"][s]["metrics"].get("error_rate") or 0) >= 0.5)
             for s in SERVICES}
    print(f"{path.name}  {len(lab.ticks)} ticks  failing ticks "
          f"{ {k: v for k, v in worst.items() if v} }", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=list(PLANS))
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    lab = Lab()
    runs = plan(a.set)[: a.limit]
    listener = asyncio.create_task(lab.listen())
    await lab.do(lab.bank.up)
    w = wl.Workload(lab.bank.key, PLANS[a.set][0])
    await w.warm_up()
    driver = asyncio.create_task(w.drive(Varying(8.0, wl.EVERYDAY), 8.0, 10**7))
    await asyncio.sleep(20)
    try:
        for i, run in enumerate(runs):
            path = OUT / a.set / f"{i:02d}-{run['kind']}-{run['service']}.json.gz"
            if not path.exists():
                await one(lab, run, path)
            w.samples.clear()
            w.acknowledged.clear()
    finally:
        driver.cancel()
        listener.cancel()
        await w.http.aclose()
        await lab.do(lab.bank.down)


if __name__ == "__main__":
    asyncio.run(main())
