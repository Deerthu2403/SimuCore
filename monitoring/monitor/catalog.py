"""The metrics SimuCore monitors: names, units, limits and when each one must be present."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from monitor.config import ServiceSpec


@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    unit: str
    description: str
    maximum: float
    needs_traffic: bool = False  # legitimately has no value while the service is idle
    needs_database: bool = False  # only for services that use the database

    def applies_to(self, s: ServiceSpec) -> bool:
        return s.uses_database or not self.needs_database


METRICS: tuple[Metric, ...] = (
    Metric("cpu_pct", "CPU", "%", "Process CPU time, 100 % = one core", 12_800),
    Metric("mem_pct", "Memory", "%", "Process memory as a share of its budget", 1_000),
    Metric("mem_bytes", "Memory used", "B", "Process memory", 1e13),
    Metric("latency_ms", "Latency (p95)", "ms", "95th percentile request latency", 1e6,
           needs_traffic=True),
    Metric("latency_mean_ms", "Latency (mean)", "ms", "Mean request latency", 1e6,
           needs_traffic=True),
    Metric("rps", "Requests per second", "req/s", "Business requests completed", 1e6),
    Metric("error_rate", "Error rate", "ratio", "Share of requests answered with 5xx", 1.0,
           needs_traffic=True),
    Metric("net_rx_bps", "Network received", "B/s",
           "Application-layer bytes received by business traffic", 1e12),
    Metric("net_tx_bps", "Network sent", "B/s",
           "Application-layer bytes sent by business traffic", 1e12),
    Metric("db_response_ms", "Database response time", "ms",
           "Mean response time of database calls made by this service", 1e6,
           needs_traffic=True, needs_database=True),
    Metric("active_connections", "Active connections", "",
           "Open inbound TCP connections, including idle ones and the scraper's own", 1e6),
    Metric("queue_length", "Queue length", "", "Requests waiting for a worker slot", 1e6),
    Metric("in_flight", "In flight", "", "Requests being worked on", 1e6),
    Metric("availability", "Availability", "ratio", "1 = reachable and accepting work", 1.0),
)
BY_KEY = {m.key: m for m in METRICS}


def catalog() -> list[dict]:
    return [asdict(m) for m in METRICS]
