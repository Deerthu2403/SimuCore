"""From metrics to findings, and from findings over time to a status."""

from __future__ import annotations

from dataclasses import dataclass

from app.digital_twin.model import (
    LEVEL_STATUS,
    STATUS_LEVEL,
    ComponentSpec,
    Finding,
    InfraObs,
    Level,
    LinkSpec,
    ServiceObs,
    Status,
)

L = Level
MIN_RPS = 0.3  # below three requests per window, ratios and percentiles mean little
MIN_CALLS = 0.3
WINDOW_S = 10.0  # over which monitoring computes rates and percentiles
MIN_SAMPLES = 20


def _limits(metrics: dict[str, float | None], key: str, label: str, unit: str,
            warning: float, overload: float | None) -> list[Finding]:
    v = metrics.get(key)
    if v is None:
        return []
    for level, limit in ((L.OVERLOAD, overload), (L.WARNING, warning)):
        if limit is not None and v >= limit:
            return [Finding(level, f"{key}_high", f"{label} is {v:.4g}{unit} (limit {limit:.4g}"
                            f"{unit})", key, v, limit)]
    return []


# ------------------------------------------------------------------------- rules
def service_findings(spec: ComponentSpec, obs: ServiceObs) -> list[Finding]:
    """Own condition of a microservice, without looking at its dependencies."""
    if obs.status == "down":
        return [Finding(L.FAILURE, "unavailable", obs.reason or "not reachable", immediate=True)]
    m = obs.metrics
    out: list[Finding] = []
    traffic = (m.get("rps") or 0.0) >= MIN_RPS
    errors = m.get("error_rate")
    if traffic and errors is not None:
        if errors >= 0.5:
            out.append(Finding(L.FAILURE, "errors", f"{errors:.0%} of requests fail", "error_rate",
                               errors, 0.5))
        elif errors >= 0.02:
            out.append(Finding(L.WARNING, "errors", f"{errors:.1%} of requests fail", "error_rate",
                               errors, 0.02))
    out += _limits(m, "cpu_pct", "CPU", " %", 75, 90)
    out += _limits(m, "mem_pct", "memory", " %", 80, 92)
    slots = float(spec.attributes["max_concurrency"])
    queue = m.get("queue_length") or 0.0
    if queue >= max(1.0, 0.25 * slots):
        out.append(Finding(L.OVERLOAD, "queue", f"{queue:.0f} requests are waiting",
                           "queue_length", queue, 0.25 * slots))
    elif queue >= 1:
        out.append(Finding(L.WARNING, "queue", f"{queue:.0f} request(s) are waiting",
                           "queue_length", queue, 1))
    busy = (m.get("in_flight") or 0.0) / slots
    if busy >= 0.9:
        out.append(Finding(L.OVERLOAD, "slots", f"{busy:.0%} of worker slots are busy",
                           "in_flight", m["in_flight"], 0.9 * slots))
    # A percentile of a handful of requests is just the slowest of them: then use the mean.
    few = (m.get("rps") or 0.0) * WINDOW_S < MIN_SAMPLES
    key = "latency_mean_ms" if few else "latency_ms"
    latency, base = m.get(key), float(spec.attributes["baseline_latency_ms"])
    if traffic and latency is not None and latency >= 1.8 * base:
        # Slow alone is not overloaded: it may be waiting for something slow. It is overloaded
        # if it is also short of its own resources.
        strained = (m.get("cpu_pct") or 0) >= 60 or queue >= 1 or busy >= 0.5
        level = L.OVERLOAD if strained and latency >= 3 * base else L.WARNING
        out.append(Finding(level, "slow", f"{'mean' if few else 'p95'} latency is {latency:.0f} "
                           f"ms, normal is up to {base:.0f} ms", key, latency,
                           (3 if level is L.OVERLOAD else 1.8) * base))
    return out


def store_findings(m: dict[str, float | None]) -> list[Finding]:
    """The bank's database, seen through the Database Service's connection pool."""
    out: list[Finding] = []
    failing = m.get("db_failure_rate")
    if failing is not None and failing >= 0.5:
        out.append(Finding(L.FAILURE, "connections", f"{failing:.0%} of queries get no connection",
                           "db_failure_rate", failing, 0.5))
    elif failing is not None and failing >= 0.01:
        out.append(Finding(L.WARNING, "connections", f"{failing:.1%} of queries get no connection",
                           "db_failure_rate", failing, 0.01))
    size, used = m.get("db_pool_size"), m.get("db_pool_in_use")
    if size and used is not None:
        share = used / size
        if share >= 0.95:
            out.append(Finding(L.OVERLOAD, "pool", f"all {size:.0f} connections are in use",
                               "db_pool_in_use", used, 0.95 * size))
        elif share >= 0.75:
            out.append(Finding(L.WARNING, "pool", f"{used:.0f} of {size:.0f} connections are in "
                               "use", "db_pool_in_use", used, 0.75 * size))
    if (m.get("rps") or 0.0) >= MIN_RPS:  # without requests there is only housekeeping
        out += _limits(m, "db_response_ms", "query time", " ms", 50, 250)
    return out


def entry_findings(m: dict[str, float], failed_routes: int, routes: int) -> list[Finding]:
    """The client entry: can customers get in, and do their requests succeed?"""
    out: list[Finding] = []
    if routes and failed_routes == routes:
        out.append(Finding(L.FAILURE, "no_route", "no service that customers use is working",
                           "routes_failed", failed_routes, routes, immediate=True))
    errors = m.get("error_rate")
    if m.get("ingress_rps", 0.0) >= MIN_RPS and errors is not None:
        for level, limit in ((L.FAILURE, 0.5), (L.WARNING, 0.02)):
            if errors >= limit:
                out.append(Finding(level, "errors", f"{errors:.1%} of customer requests fail",
                                   "error_rate", errors, limit))
                break
    return out


def infra_findings(obs: InfraObs) -> list[Finding]:
    if obs.status == "down":
        return [Finding(L.FAILURE, "unavailable", obs.reason or "not reachable", immediate=True)]
    out = _limits(obs.metrics, "cpu_pct", "CPU", " %", 75, 90)
    out += _limits(obs.metrics, "mem_pct", "memory", " %", 85, 95)
    out += _limits(obs.metrics, "connections", "connections", "", 70, 90)  # PostgreSQL: max 100
    return out


def link_findings(spec: LinkSpec, m: dict[str, float | None]) -> list[Finding]:
    out: list[Finding] = []
    active = (m.get("calls_per_s") or 0.0) >= MIN_CALLS
    failing = m.get("failure_rate")
    if m.get("circuit") == 2:
        out.append(Finding(L.FAILURE, "circuit_open", "the caller stopped trying (circuit open)",
                           "circuit", 2, 2))
    if active and failing is not None:
        if failing >= 0.5:
            out.append(Finding(L.FAILURE, "calls_fail", f"{failing:.0%} of calls fail",
                               "failure_rate", failing, 0.5))
        elif failing >= 0.02:
            out.append(Finding(L.WARNING, "calls_fail", f"{failing:.1%} of calls fail",
                               "failure_rate", failing, 0.02))
    latency = m.get("latency_ms")
    if active and latency is not None and spec.timeout_ms and latency >= 0.5 * spec.timeout_ms:
        out.append(Finding(L.WARNING, "slow_calls", f"calls take {latency:.0f} ms of the "
                           f"{spec.timeout_ms} ms allowed", "latency_ms", latency,
                           0.5 * spec.timeout_ms))
    return out


def worst(findings: list[Finding]) -> Level:
    return max((f.level for f in findings), default=L.OK)


# ------------------------------------------------------------------ state machine
@dataclass(frozen=True)
class Persistence:
    """How many consecutive observations each kind of change needs."""

    escalate: int = 2
    deescalate: int = 3
    recovery: int = 5


class StatusTracker:
    """Turns a stream of levels into a stable status.

    healthy <-> warning <-> overloaded   each change must persist for a few observations
    any -> failed                        at once if certain, else after ``escalate`` observations
    failed -> recovering                 with the first observation that is not a failure
    recovering -> healthy/warning/...    after ``recovery`` observations without a failure
    recovering -> failed                 at once
    unknown -> recovering                if it had failed before contact was lost
    """

    def __init__(self, persistence: Persistence | None = None) -> None:
        self.p = persistence or Persistence()
        self.status = Status.UNKNOWN
        self._failed_before = False  # failed or recovering when contact was lost
        self._streak = 0
        self._direction = 0
        self._bound = L.OK
        self._good = 0

    def observe(self, level: Level | None, immediate: bool = False) -> Status:
        if level is None:
            self._failed_before |= self.status in (Status.FAILED, Status.RECOVERING)
            self._set(Status.UNKNOWN)
        elif self.status is Status.UNKNOWN:
            if level is L.FAILURE or not self._failed_before:
                self._set(LEVEL_STATUS[level])
            else:
                self._begin_recovery()
            self._failed_before = False
        elif self.status is Status.FAILED:
            if level is not L.FAILURE:
                self._begin_recovery()
        elif self.status is Status.RECOVERING:
            if level is L.FAILURE:
                self._set(Status.FAILED)
            else:
                self._good += 1
                if self._good >= self.p.recovery:
                    self._set(LEVEL_STATUS[level])
        else:
            self._persist(level, immediate)
        return self.status

    def _persist(self, level: Level, immediate: bool) -> None:
        current = STATUS_LEVEL[self.status]
        if level is L.FAILURE and immediate:
            self._set(Status.FAILED)
            return
        direction = (level > current) - (level < current)
        if direction == 0:
            self._streak = self._direction = 0
            return
        if direction != self._direction:
            self._direction, self._streak, self._bound = direction, 0, level
        self._streak += 1
        # Go only as far as every observation of the streak supports.
        self._bound = min(self._bound, level) if direction > 0 else max(self._bound, level)
        if self._streak >= (self.p.escalate if direction > 0 else self.p.deescalate):
            self._set(LEVEL_STATUS[self._bound])

    def _begin_recovery(self) -> None:
        self._set(Status.RECOVERING)
        self._good = 1

    def _set(self, status: Status) -> None:
        self.status = status
        self._streak = self._direction = 0
        self._good = 0
