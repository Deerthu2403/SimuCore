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
    only: str | None = None  # only for this service

    def applies_to(self, s: ServiceSpec) -> bool:
        if self.only:
            return s.name == self.only
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
    Metric("db_pool_in_use", "Database connections in use", "",
           "Connections of the bank's database pool that are busy", 1e6, only="database"),
    Metric("db_pool_size", "Database pool size", "",
           "Connections in the bank's database pool", 1e6, only="database"),
    Metric("db_failure_rate", "Database failure rate", "ratio", "Share of queries that got no "
           "connection or no answer", 1.0, needs_traffic=True, only="database"),
    Metric("availability", "Availability", "ratio", "1 = reachable and accepting work", 1.0),
)
BY_KEY = {m.key: m for m in METRICS}

#: Metrics of a network connection between two services: key -> largest possible value.
LINK_METRICS = {"calls_per_s": 1e6, "failure_rate": 1.0, "latency_ms": 1e6, "circuit": 2.0}


def catalog() -> list[dict]:
    return [asdict(m) for m in METRICS]
