"""Per-service metrics: Prometheus instrumentation plus a rolling window in SimuCore's names.

Every series a monitor depends on exists from the first scrape, with value 0. An absent series
therefore always means that collection is broken, never that the service has been idle.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable

import psutil
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

WINDOW_S = 10.0
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.015, 0.025, 0.04, 0.06, 0.08, 0.1, 0.15, 0.2, 0.3,
                   0.5, 0.75, 1, 1.5, 2.5, 5, 10)
OUTCOMES = ("ok", "client_error", "server_error")


def header_bytes(headers) -> int:
    """Size of HTTP headers on the wire: ``name: value\\r\\n`` each."""
    return sum(len(k) + len(v) + 4 for k, v in headers)


class Telemetry:
    def __init__(self, service: str, mem_limit_mb: int, uses_database: bool = False) -> None:
        self.service, self.mem_limit_mb = service, mem_limit_mb
        self.registry = r = CollectorRegistry()
        one = ["service"]
        # Detailed, per route: for dashboards. Series appear as routes are used.
        self.requests = Counter("bank_requests", "Business requests by route",
                                ["service", "method", "route", "status"], registry=r)
        self.latency = Histogram("bank_request_seconds", "Business request latency by route",
                                 ["service", "route"], registry=r)
        # Service level: the monitoring contract.
        outcome = Counter("bank_service_requests", "Business requests by outcome",
                          ["service", "outcome"], registry=r)
        self._outcome = {o: outcome.labels(service, o) for o in OUTCOMES}
        self._seconds = Histogram("bank_service_request_seconds", "Business request latency", one,
                                  buckets=LATENCY_BUCKETS, registry=r).labels(service)
        self._rx = Counter("bank_network_receive_bytes", "Application-layer bytes received by "
                           "business traffic", one, registry=r).labels(service)
        self._tx = Counter("bank_network_transmit_bytes", "Application-layer bytes sent by "
                           "business traffic", one, registry=r).labels(service)
        self._db = Histogram("bank_db_response_seconds", "Database response time as seen by this "
                             "service", one, buckets=LATENCY_BUCKETS,
                             registry=r).labels(service) if uses_database else None

        if service == "database":
            self._db_failures = Counter("bank_db_query_failures", "Queries that got no connection "
                                        "or no answer", one, registry=r).labels(service)
        # Network connections to other services.
        pair = ["service", "target"]
        self._link_attempts = Counter("bank_link_attempts", "Attempts to call another service, "
                                      "including those an open circuit refused", pair, registry=r)
        self._link_failures = Counter("bank_link_failures", "Attempts that failed or were refused",
                                      pair, registry=r)
        self._link_seconds = Histogram("bank_link_seconds", "Duration of calls actually made", pair,
                                       buckets=LATENCY_BUCKETS, registry=r)
        self._link_circuit = Gauge("bank_link_circuit", "0 closed, 1 half open, 2 open", pair,
                                   registry=r)
        Gauge("bank_info", "Where the service runs", ["service", "deployment"], registry=r).labels(
            service, "container" if os.path.exists("/.dockerenv") else "process").set(1)

        self._proc = psutil.Process()
        self._proc.cpu_percent()
        self._mem_cache = (0.0, 0.0)
        self._window: deque[tuple[float, float, int]] = deque()
        self._one = one
        # A counter, so that any number of scrapers get the right rate independently.
        self.gauge("bank_process_cpu_seconds_total", "CPU time used by the process",
                   self._cpu_seconds)
        self.gauge("bank_memory_bytes", "Memory used", lambda: self.memory_mb() * 1_048_576)
        self.gauge("bank_memory_limit_bytes", "Memory budget", lambda: mem_limit_mb * 1_048_576)

    def gauge(self, name: str, doc: str, fn: Callable[[], float]) -> None:
        """Expose a value read at scrape time."""
        g = Gauge(name, doc, self._one, registry=self.registry)
        g.labels(self.service).set_function(fn)

    def _cpu_seconds(self) -> float:
        t = self._proc.cpu_times()
        return t.user + t.system

    # ---------------------------------------------------------------- recording
    def record(self, method: str, route: str, status: int, seconds: float) -> None:
        self.requests.labels(self.service, method, route, str(status)).inc()
        self.latency.labels(self.service, route).observe(seconds)
        kind = "server_error" if status >= 500 else "client_error" if status >= 400 else "ok"
        self._outcome[kind].inc()
        self._seconds.observe(seconds)
        now = time.monotonic()
        self._window.append((now, seconds * 1000, status))
        self._trim(now)

    def traffic(self, received: int = 0, sent: int = 0) -> None:
        self._rx.inc(received)
        self._tx.inc(sent)

    def declare_link(self, target: str, circuit: Callable[[], str]) -> None:
        """Create a connection's series up front; ``circuit`` reads its breaker's state."""
        for m in (self._link_attempts, self._link_failures, self._link_seconds):
            m.labels(self.service, target)
        code = {"closed": 0, "half_open": 1, "open": 2}
        self._link_circuit.labels(self.service, target).set_function(lambda: code[circuit()])

    def link(self, target: str, ok: bool, seconds: float | None) -> None:
        self._link_attempts.labels(self.service, target).inc()
        if not ok:
            self._link_failures.labels(self.service, target).inc()
        if seconds is not None:
            self._link_seconds.labels(self.service, target).observe(seconds)

    def db_failure(self) -> None:
        self._db_failures.inc()

    def db_response(self, seconds: float) -> None:
        if self._db is not None:
            self._db.observe(seconds)

    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0][0] > WINDOW_S:
            self._window.popleft()

    def memory_mb(self, fresh: bool = False) -> float:
        at, value = self._mem_cache
        now = time.monotonic()
        if fresh or now - at > 0.25:
            mi = self._proc.memory_info()
            # Windows trims the working set of idle processes; private bytes do not shrink.
            value = getattr(mi, "private", mi.rss) / 1_048_576
            self._mem_cache = (now, value)
        return value

    # ----------------------------------------------------------------- exposing
    def exposition(self, fault_mode: str | None = None) -> bytes:
        """Prometheus text. ``fault_mode`` is the simulated telemetry fault, if any."""
        text = generate_latest(self.registry)
        if fault_mode == "garbage":
            return b"<html>this is not a metrics page</html>"
        if fault_mode == "missing":
            lost = (b"bank_process_cpu_seconds", b"bank_memory_bytes", b"bank_request_queue_length")
            return b"\n".join(x for x in text.split(b"\n")
                              if not x.removeprefix(b"# HELP ").removeprefix(b"# TYPE ")
                              .startswith(lost))
        if fault_mode == "invalid":
            text = re.sub(rb"(?m)^(bank_memory_bytes\{[^}]*\}) .*$", rb"\1 -5.0", text)
            return re.sub(rb"(?m)^(bank_active_connections\{[^}]*\}) .*$", rb"\1 NaN", text)
        return text

    def standard_metrics(self, available: bool) -> dict[str, float]:
        self._trim(time.monotonic())
        lat = sorted(x[1] for x in self._window)
        n = len(lat)
        return {
            "cpu_pct": round(min(self._proc.cpu_percent(), 100.0), 2),
            "mem_pct": round(min(self.memory_mb() / self.mem_limit_mb * 100, 100.0), 2),
            "latency_ms": round(lat[min(n - 1, int(0.95 * n))], 2) if n else 0.0,
            "error_rate": round(sum(x[2] >= 500 for x in self._window) / n, 4) if n else 0.0,
            "rps": round(n / WINDOW_S, 2),
            "availability": 1.0 if available else 0.0,
        }
