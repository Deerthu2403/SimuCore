"""Read-only Prometheus adapter (skeleton - completed in Phase 6).

Topology cannot be derived from Prometheus alone; in production it comes from a
CMDB / service-mesh adapter. Here we accept a static topology and map PromQL
query results onto STANDARD_METRICS.
"""

from __future__ import annotations

from app.adapters.base import ReadOnlyHttpClient, TelemetryAdapter
from app.domain.models import MetricSample, Topology

#: PromQL templates per standard metric. ``{node}`` is the node id label value.
DEFAULT_QUERIES: dict[str, str] = {
    "cpu_pct": 'avg(rate(process_cpu_seconds_total{{service="{node}"}}[1m])) * 100',
    "latency_ms": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{service="{node}"}}[1m])) by (le)) * 1000',
    "error_rate": 'sum(rate(http_requests_total{{service="{node}",code=~"5.."}}[1m])) / sum(rate(http_requests_total{{service="{node}"}}[1m]))',
    "rps": 'sum(rate(http_requests_total{{service="{node}"}}[1m]))',
    "availability": 'min(up{{service="{node}"}})',
}


class PrometheusAdapter(TelemetryAdapter):
    name = "prometheus"

    def __init__(self, base_url: str, topology: Topology,
                 queries: dict[str, str] | None = None) -> None:
        self._base_url = base_url
        self._topology = topology
        self._queries = queries or DEFAULT_QUERIES
        self._http: ReadOnlyHttpClient | None = None

    async def connect(self) -> None:
        self._http = ReadOnlyHttpClient(self._base_url)
        r = await self._http.get("/-/ready")
        r.raise_for_status()

    async def fetch_topology(self) -> Topology:
        return self._topology

    async def poll(self) -> list[MetricSample]:
        # Phase 6: batch queries, map results, handle NaN / missing series,
        # rate-limit + circuit-break, write audit_log entries.
        raise NotImplementedError("Prometheus polling is scheduled for Phase 6")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
