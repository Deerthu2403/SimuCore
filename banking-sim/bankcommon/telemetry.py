"""Per-service metrics: a Prometheus registry plus a rolling window in SimuCore's metric names."""

from __future__ import annotations

import time
from collections import deque

import psutil
from prometheus_client import CollectorRegistry, Counter, Histogram

WINDOW_S = 10.0


class Telemetry:
    def __init__(self, service: str, mem_limit_mb: int) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter("bank_requests", "Business requests",
                                ["service", "method", "route", "status"], registry=self.registry)
        self.latency = Histogram("bank_request_seconds", "Business request latency",
                                 ["service", "route"], registry=self.registry)
        self.service, self.mem_limit_mb = service, mem_limit_mb
        self._window: deque[tuple[float, float, int]] = deque()
        self._proc = psutil.Process()
        self._proc.cpu_percent()
        self._mem_cache = (0.0, 0.0)

    def record(self, method: str, route: str, status: int, seconds: float) -> None:
        self.requests.labels(self.service, method, route, str(status)).inc()
        self.latency.labels(self.service, route).observe(seconds)
        now = time.monotonic()
        self._window.append((now, seconds * 1000, status))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0][0] > WINDOW_S:
            self._window.popleft()

    def memory_mb(self) -> float:
        at, value = self._mem_cache
        now = time.monotonic()
        if now - at > 0.25:
            mi = self._proc.memory_info()
            # Windows trims the working set of idle processes; private bytes do not shrink.
            value = getattr(mi, "private", mi.rss) / 1_048_576
            self._mem_cache = (now, value)
        return value

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
