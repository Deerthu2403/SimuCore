"""Synthetic, labelled monitoring data: the bank as a small queueing model with things going wrong.

The normal numbers come from recordings of the real simulated bank (calibrate.py). What goes wrong is
drawn at random, in four groups that keep the prediction task honest:

  gradual    something gets worse until the service degrades or fails    (can be predicted)
  sudden     a crash or an outage out of nothing                         (cannot be predicted)
  recovering something gets worse, then better again by itself           (looks like the first, is not)
  harmless   short disturbances, waves of customers, or nothing at all

The labels are not made here: labels.py derives them from the resulting numbers, exactly as it does for
recorded data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from simucore_ai.features import IDX, RAW, TICK_S, Service
from simucore_ai.recordings import Episode

GRADUAL = ("cpu", "leak", "leak_crash", "slow", "pool", "load", "errors", "slots")
SUDDEN = ("crash", "outage")
RECOVERING = ("part_cpu", "part_leak", "part_slow", "part_pool", "part_errors")
BENIGN = ("calm", "blip_cpu", "blip_slow", "wave", "restart")
WINDOW = 5  # monitoring averages rates over 10 s


@dataclass(frozen=True)
class Shape:
    """The range of worlds episodes are drawn from. ``SHIFTED`` lies outside of it."""

    ramp_s: tuple[float, float] = (60, 320)
    rate: tuple[float, float] = (2.5, 20)
    noise: tuple[float, float] = (0.7, 1.4)
    scale: tuple[float, float] = (0.75, 1.3)  # multiplies CPU cost and latency of every service


SHIFTED = Shape(ramp_s=(40, 420), rate=(2, 26), noise=(1.4, 1.8), scale=(1.3, 1.55))
#: share of episodes per group
MIX = {"gradual": 0.5, "sudden": 0.08, "recovering": 0.2, "harmless": 0.22}


@dataclass
class _Trouble:
    kind: str
    service: str
    begin: int
    length: int  # ticks until it is at its worst
    end: int
    peak: float
    bend: float

    def level(self, t: int) -> float:
        """0 = nothing, 1 = at its worst."""
        if t < self.begin or t >= self.end:
            return 0.0
        x = (t - self.begin) / max(1, self.length)
        if self.kind.startswith("part_") and x > 1:  # gets better again
            return max(0.0, self.peak * (1 - (x - 1) * 1.5))
        return self.peak * min(1.0, x) ** self.bend


def _pick(rng: np.random.Generator) -> str:
    group = rng.choice(list(MIX), p=list(MIX.values()))
    pool = {"gradual": GRADUAL, "sudden": SUDDEN, "recovering": RECOVERING, "harmless": BENIGN}
    return str(rng.choice(pool[group]))


def _trouble(rng: np.random.Generator, names: list[str], ticks: int, shape: Shape) -> _Trouble:
    kind = _pick(rng)
    service = "database" if kind in ("pool", "part_pool") else str(rng.choice(names))
    begin = int(rng.integers(45, 90))
    length = int(rng.uniform(*shape.ramp_s) / TICK_S)
    if kind.startswith("blip"):
        length = 1
        end = begin + int(rng.integers(4, 10))
    elif kind in SUDDEN:
        begin = int(rng.integers(45, 140))
        length = 1
        end = begin + int(rng.integers(5, 25))
    elif kind.startswith("part_"):
        end = begin + 2 * length + 5
    else:
        end = begin + length + int(rng.integers(10, 30))
    end = min(end, ticks - 20)
    peak = float(rng.uniform(0.25, 0.6)) if kind.startswith("part_") else 1.0
    return _Trouble(kind, service, begin, length, end, peak, float(rng.uniform(0.6, 2.2)))


def episode(seed: int, services: dict[str, Service], calibration: dict,
            shape: Shape | None = None) -> Episode:
    shape = shape or Shape()
    rng = np.random.default_rng(seed)
    noise, scale = float(rng.uniform(*shape.noise)), float(rng.uniform(*shape.scale))
    names = list(services)
    cal = calibration["services"]
    ticks = int(rng.integers(220, 300))
    tr = _trouble(rng, names, ticks, shape)
    order = _dependencies_first(services)

    # customers: a rate that wanders, sometimes in a wave
    rate = rng.uniform(*shape.rate)
    wander = np.cumsum(rng.normal(0, 0.012, ticks))
    customers = rate * np.exp(wander - wander.mean())
    if tr.kind == "wave" or rng.random() < 0.15:
        at, width = rng.uniform(60, ticks - 40), rng.uniform(15, 45)
        customers *= 1 + rng.uniform(0.6, 2.0) * np.exp(-((np.arange(ticks) - at) / width) ** 2)
    if tr.kind == "load":
        top = rng.uniform(6, 16)
        customers *= np.array([1 + top * tr.level(t) for t in range(ticks)])

    # every machine is a little different, and noise belongs to it
    cost = {s: np.exp(rng.normal(0, 0.12)) * scale for s in names}
    memory = {s: cal[s]["memory"][0] + rng.normal(0, 0.6) for s in names}
    drift = {s: rng.normal(0, 0.002) for s in names}
    max_latency = {s: np.exp(rng.uniform(np.log(800), np.log(6000))) for s in names}
    pool_need = rng.uniform(0.001, 0.003)
    queue = dict.fromkeys(names, 0.0)
    silent_for = dict.fromkeys(names, 0)
    down_until = dict.fromkeys(names, -1)
    crash_memory = rng.uniform(80, 93) if tr.kind == "leak_crash" else 1e9

    raw = {s: np.full((ticks, len(RAW)), np.nan) for s in names}
    status = {s: np.full(ticks, "up", dtype="<U12") for s in names}
    n_requests = {s: np.zeros(ticks) for s in names}
    lat_sum = {s: np.zeros(ticks) for s in names}
    err = {s: np.zeros(ticks) for s in names}
    db_ms = {s: np.full(ticks, np.nan) for s in names}

    for t in range(ticks):
        level = tr.level(t)
        latency: dict[str, float] = {}
        failing: dict[str, float] = {}
        for s in order:
            spec, c = services[s], cal[s]
            mine = level if s == tr.service else 0.0
            kind = tr.kind.removeprefix("part_")
            if tr.kind in SUDDEN and s == tr.service and tr.begin <= t < tr.end:
                status[s][t] = "down"
            elif t < down_until[s]:
                status[s][t] = "down"
            if status[s][t] == "down":
                latency[s], failing[s] = 2.0, 1.0
                continue
            # ------------------------------------------------ work
            arrivals = rng.poisson(max(0.0, customers[t] * c["share"] * TICK_S))
            rps = arrivals / TICK_S
            extra = 0.0
            if kind == "cpu":
                extra = 130 * mine
            elif tr.kind == "blip_cpu" and s == tr.service and level > 0:
                extra = 35.0
            elif tr.kind == "restart" and s == tr.service and level > 0 and t - tr.begin < 4:
                extra = 45.0
            c0, c1, csd = c["cpu"]
            used = (c0 + c1 * rps) * cost[s] + rng.normal(0, 0.5 + 0.6 * csd) * noise
            cpu = used + extra
            capacity = max(1.0, (100 - c0) / max(0.4, c1 * cost[s])) * max(0.05, 1 - extra / 110)
            load = min(1.5, rps / capacity)
            if kind == "slots":
                load = max(load, (mine - 0.75) * 4.2 if mine > 0.75 else 0.0)
            # ------------------------------------------------ memory
            mem = memory[s] + drift[s] * t + queue[s] * 0.01 + rng.normal(0, 0.05 + c["memory"][1] * 0.3)
            if kind == "leak" or kind == "leak_crash":
                mem += (99 - memory[s]) * mine
            if mem >= crash_memory and s == tr.service:
                down_until[s] = t + int(rng.integers(6, 14))
                tr.end = t  # the new process starts with empty memory
                status[s][t] = "down"
                latency[s], failing[s] = 2.0, 1.0
                continue
            mem = min(mem, 99.5)
            # ------------------------------------------------ dependencies
            own = np.exp(c["latency"][0] + rng.normal(0, c["latency"][1] * 0.8) * noise) * cost[s]
            waiting, lost = 0.0, 0.0
            for d in spec.targets:
                weight = min(1.0, cal[d]["share"] / max(0.05, c["share"]))
                weight = 1.0 if d == "database" else max(0.35, min(1.0, weight))
                normal = np.exp(cal[d]["latency"][0])
                more = max(0.0, latency[d] - normal * 1.3)
                waiting += weight * min(more, 1500.0)
                timeout = 1 / (1 + np.exp(-(latency[d] - 1400) / 180))
                lost = 1 - (1 - lost) * (1 - weight * max(failing[d], timeout))
            # ------------------------------------------------ its own trouble
            slow = 0.0
            if kind == "slow":
                slow = max_latency[s] * mine
            elif tr.kind == "blip_slow" and s == tr.service and level > 0:
                slow = 90.0
            e = 0.0
            if kind == "errors":
                e = 0.95 * mine
            if mem >= 95:
                e = 1.0
            share_pool = np.nan
            if s == "database":
                base = min(0.5, pool_need * rps)
                share_pool = min(1.0, round((base + (mine if kind == "pool" else 0.0)) * 8) / 8)
                free = 1 - share_pool
                if free <= 0.01:
                    slow += 900
                    e = max(e, 0.85)
                elif free < 0.3:
                    slow += 60 * (0.3 - free) / 0.3 * rng.uniform(0.5, 1.5)
            # ------------------------------------------------ queueing
            stretch = 1 / max(0.06, 1 - min(load, 0.98)) ** 0.8
            queue[s] = max(0.0, queue[s] + (load - 1.0) * spec.max_concurrency * 0.35) if load > 1 \
                else queue[s] * 0.5
            full = queue[s] >= spec.max_concurrency
            queue[s] = min(queue[s], spec.max_concurrency)
            if full:
                e = max(e, min(0.9, (load - 1) * 1.5 + 0.2))
            starving = max(0.0, (cpu - 85) / 15)
            total = own * stretch + slow + waiting + queue[s] * 12 + own * 14 * starving**2
            # ------------------------------------------------ result
            latency[s] = float(total)
            failing[s] = float(1 - (1 - e) * (1 - lost))
            n_requests[s][t] = arrivals
            lat_sum[s][t] = total * arrivals
            err[s][t] = rng.binomial(arrivals, min(1.0, failing[s])) if arrivals else 0
            if rng.random() < 0.0008:  # a stray error in normal operation
                err[s][t] = min(arrivals, err[s][t] + 1)
            r = raw[s][t]
            r[IDX["cpu"]] = float(np.clip(cpu, 0, 104 + rng.normal(0, 1)))
            r[IDX["memory"]] = mem
            in_flight = rps * total / 1000
            r[IDX["in_flight"]] = min(1.0, rng.poisson(min(400.0, in_flight)) / spec.max_concurrency)
            r[IDX["queue"]] = round(queue[s]) / spec.max_concurrency
            r[IDX["pool"]] = share_pool
            k0, k1, ksd = c["connections"]
            busy = min(40.0, in_flight)
            r[IDX["connections"]] = max(0, round(k0 + k1 * rps + 0.7 * max(0.0, busy - 1)
                                                 + rng.normal(0, 0.4 * ksd + 0.2)))
            r[IDX["up"]] = 1.0
            if c["database"]:
                d_ms = np.exp(c["database"][0] + rng.normal(0, c["database"][1] * 0.8))
                d_more = latency.get("database", 0) - np.exp(cal["database"]["latency"][0]) * 1.3 \
                    if s != "database" else slow
                db_ms[s][t] = (d_ms * cost[s] + max(0.0, min(d_more, 1500))) * arrivals
            # ------------------------------------------------ too busy to answer the monitor
            if cpu > 108 and load > 0.4 and rng.random() < 0.35:
                silent_for[s] = int(rng.integers(2, 6))
            if silent_for[s] > 0:
                silent_for[s] -= 1
                status[s][t] = "unresponsive"

    for s in names:
        _window(raw[s], status[s], n_requests[s], lat_sum[s], err[s], db_ms[s], cal[s], rng)
    return Episode(f"syn-{seed}", tr.kind, tr.service, raw, status,
                   _abnormal(tr, names, services, ticks), tr.begin, "synthetic",
                   {"seed": seed, "rate": rate})


def _dependencies_first(services: dict[str, Service]) -> list[str]:
    out: list[str] = []

    def visit(s: str) -> None:
        if s in out:
            return
        for d in services[s].targets:
            visit(d)
        out.append(s)

    for s in services:
        visit(s)
    return out


def _window(raw: np.ndarray, status: np.ndarray, n: np.ndarray, lat: np.ndarray, err: np.ndarray,
            db: np.ndarray, c: dict, rng: np.random.Generator) -> None:
    """Rates over the last 10 s, as monitoring computes them; no values without requests or while
    the service is down or silent."""
    b, bsd = c["bytes"]
    for t in range(len(raw) - 1, -1, -1):
        lo = max(0, t - WINDOW + 1)
        if status[t] != "up":
            raw[t] = np.nan
            raw[t, IDX["up"]] = 0.0
            continue
        total = n[lo: t + 1].sum()
        raw[t, IDX["requests"]] = total / ((t - lo + 1) * TICK_S)
        raw[t, IDX["network"]] = raw[t, IDX["requests"]] * b * np.exp(rng.normal(0, 0.4 * bsd))
        raw[t, IDX["cpu"]] = np.nanmean(raw[lo: t + 1, IDX["cpu"]])
        if total > 0:
            raw[t, IDX["latency"]] = lat[lo: t + 1].sum() / total
            raw[t, IDX["errors"]] = err[lo: t + 1].sum() / total
            if c["database"]:
                raw[t, IDX["database"]] = np.nansum(db[lo: t + 1]) / total
    for t in np.flatnonzero(rng.random(len(raw)) < 0.004):  # a scrape that did not get through
        if status[t] == "up":
            raw[t, : len(IDX) - 1] = np.where(rng.random(len(IDX) - 1) < 0.3, np.nan,
                                              raw[t, : len(IDX) - 1])


def _abnormal(tr: _Trouble, names: list[str], services: dict[str, Service],
              ticks: int) -> dict[str, np.ndarray]:
    """Where something is really wrong, for judging the anomaly detector. See recordings.Episode."""
    out = {s: np.zeros(ticks, dtype=np.int8) for s in names}
    if tr.kind in ("calm", "wave"):
        if tr.kind == "wave":
            for a in out.values():
                a[:] = -1
        return out
    for s, a in out.items():
        a[tr.begin:] = -1
        if s == tr.service:
            for t in range(tr.begin, min(ticks, tr.end)):
                # below a quarter of its strength it is hidden in the noise
                strong = tr.level(t) >= 0.25 or tr.kind.startswith("blip") or tr.kind in SUDDEN
                a[t] = 1 if strong else -1
    if tr.kind == "load":
        for a in out.values():
            a[tr.begin:] = -1
            for t in range(tr.begin, min(ticks, tr.end)):
                if tr.level(t) >= 0.25:
                    a[t] = 1
    if tr.kind == "restart":
        for a in out.values():
            a[:] = np.where(a == 1, -1, a)
    return out


def dataset(seeds: range, services: dict[str, Service], calibration: dict,
            shape: Shape | None = None) -> list[Episode]:
    return [episode(s, services, calibration, shape) for s in seeds]
