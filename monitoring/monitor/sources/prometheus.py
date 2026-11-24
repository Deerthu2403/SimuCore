"""Reads everything from Prometheus, which scrapes the services. GET requests only."""

from __future__ import annotations

import asyncio
import math
import time

import httpx

from monitor.config import ServiceSpec, Settings, check_host
from monitor.models import (
    Collection,
    InfraSample,
    LinkSample,
    Sample,
    SourceUnavailable,
    utcnow,
)

# One query per metric, answered for all services at once. {w} is the rate window.
SERVICE_QUERIES: dict[str, str] = {
    "cpu_pct": "rate(bank_process_cpu_seconds_total[{w}]) * 100",
    "mem_bytes": "bank_memory_bytes",
    "mem_pct": "bank_memory_bytes / bank_memory_limit_bytes * 100",
    "latency_ms":
        "histogram_quantile(0.95, rate(bank_service_request_seconds_bucket[{w}])) * 1000",
    "latency_mean_ms": "rate(bank_service_request_seconds_sum[{w}]) / "
                       "rate(bank_service_request_seconds_count[{w}]) * 1000",
    "rps": "sum by (service) (rate(bank_service_requests_total[{w}]))",
    "error_rate": 'sum by (service) (rate(bank_service_requests_total{{outcome="server_error"}}'
                  "[{w}])) / sum by (service) (rate(bank_service_requests_total[{w}]))",
    "net_rx_bps": "rate(bank_network_receive_bytes_total[{w}])",
    "net_tx_bps": "rate(bank_network_transmit_bytes_total[{w}])",
    "db_response_ms": "rate(bank_db_response_seconds_sum[{w}]) / "
                      "rate(bank_db_response_seconds_count[{w}]) * 1000",
    "active_connections": "bank_active_connections",
    "queue_length": "bank_request_queue_length",
    "in_flight": "bank_in_flight_requests",
    "db_pool_in_use": "bank_db_pool_in_use",
    "db_pool_size": "bank_db_pool_size",
    "db_failure_rate": "rate(bank_db_query_failures_total[{w}]) / "
                       "rate(bank_db_response_seconds_count{{service=\"database\"}}[{w}])",
    "availability": "bank_available",
}
# One value per (service, target): the network connections.
LINK_QUERIES: dict[str, str] = {
    "calls_per_s": "rate(bank_link_attempts_total[{w}])",
    "failure_rate": "rate(bank_link_failures_total[{w}]) / rate(bank_link_attempts_total[{w}])",
    "latency_ms": "rate(bank_link_seconds_sum[{w}]) / rate(bank_link_seconds_count[{w}]) * 1000",
    "circuit": "bank_link_circuit",
}
# Ratios that are 0/0 while nothing happens. For any other metric NaN is an invalid value.
NAN_MEANS_IDLE = {"latency_ms", "latency_mean_ms", "error_rate", "db_response_ms",
                  "db_failure_rate", "failure_rate"}

INFRA_QUERIES: dict[str, dict[str, str]] = {
    "docker-host": {
        "cpu_pct": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[30s])))',
        "mem_pct": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
        "load1": "node_load1",
        "disk_read_bps": "sum(rate(node_disk_read_bytes_total[30s]))",
        "disk_write_bps": "sum(rate(node_disk_written_bytes_total[30s]))",
        # No network figure: the exporter sees only its own container's network.
    },
    "platform-postgres": {
        "connections": "sum(pg_stat_activity_count)",
        "database_bytes": 'sum(pg_database_size_bytes{datname="simucore"})',
        "commits_per_s": 'sum(rate(pg_stat_database_xact_commit{datname="simucore"}[30s]))',
        "cache_hit_ratio": 'sum(pg_stat_database_blks_hit{datname="simucore"}) / '
                           '(sum(pg_stat_database_blks_hit{datname="simucore"}) + '
                           'sum(pg_stat_database_blks_read{datname="simucore"}))',
    },
    "prometheus": {
        "targets_up": "sum(up)",
        "targets_total": "count(up)",
        "slowest_scrape_s": "max(scrape_duration_seconds)",
        "series": "prometheus_tsdb_head_series",
    },
}
INFRA_UP = {"docker-host": 'up{job="node"}', "platform-postgres": "pg_up",
            "prometheus": 'up{job="prometheus"}'}
STALE_AFTER_S = 5.0


class PrometheusSource:
    name = "prometheus"

    def __init__(self, settings: Settings, services: tuple[ServiceSpec, ...],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.services = services
        self.window = f"{settings.window_s:g}s"
        self.http = httpx.AsyncClient(
            base_url=check_host(settings.prometheus_url, services),
            timeout=httpx.Timeout(1.5, connect=0.75), trust_env=False, transport=transport)

    async def collect(self, with_infra: bool) -> Collection:
        try:
            async with asyncio.timeout(3.0):
                return await self._collect(with_infra)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise SourceUnavailable(f"Prometheus: {type(exc).__name__} {exc}".strip()) from exc
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise SourceUnavailable(f"Prometheus answered nonsense: {exc!r}") from exc

    async def _collect(self, with_infra: bool) -> Collection:
        keys, link_keys = list(SERVICE_QUERIES), list(LINK_QUERIES)
        now, up, age, errors, deployment, *values = await asyncio.gather(
            self._scalar("time()"),
            self._vector('up{job="bank"}'),
            self._vector('time() - timestamp(up{job="bank"})'),
            self._target_errors(),
            self._deployments(),
            *(self._vector(SERVICE_QUERIES[k].format(w=self.window)) for k in keys),
            *(self._pairs(LINK_QUERIES[k].format(w=self.window)) for k in link_keys))
        values, link_values = values[:len(keys)], values[len(keys):]
        if not up:
            raise SourceUnavailable("Prometheus has no targets in job 'bank'")
        skew = time.time() - now
        ts = utcnow()
        samples = [self._sample(s, ts, up, age, errors, dict(zip(keys, values, strict=True)))
                   for s in self.services]
        for s in samples:
            s.deployment = deployment.get(s.service)
        links = self._links(ts, {s.service for s in samples if s.status == "up"},
                            dict(zip(link_keys, link_values, strict=True)))
        infra = await self._infra(ts) if with_infra else []
        return Collection(ts, "measured", samples, infra, links, round(skew, 2))

    def _sample(self, s: ServiceSpec, ts, up, age, errors, values) -> Sample:
        sample = Sample(s.name, s.node_id, ts, "up")
        if s.name not in up:
            sample.status, sample.reason = "unresponsive", "not a Prometheus target"
            return sample
        if age.get(s.name, math.inf) > STALE_AFTER_S:
            sample.status, sample.reason = "unresponsive", "Prometheus stopped scraping it"
            return sample
        if up[s.name] != 1:
            error = errors.get(s.name, "scrape failed")
            # Refused or cut off: nothing is listening. Anything else (timeout, unreadable page)
            # leaves open whether the service works, so it is not reported as down.
            gone = any(x in error for x in ("refused", "EOF", "reset by peer", "no such host"))
            why = error.rsplit(": ", 1)[-1]
            sample.status = "down" if gone else "unresponsive"
            sample.reason = f"not reachable ({why})" if gone else f"no usable metrics ({why})"
            if gone:
                sample.put("availability", 0.0)
            return sample
        for key, vector in values.items():
            if s.name not in vector:
                continue  # absent series: the quality check reports it
            v = vector[s.name]
            sample.put(key, None if math.isnan(v) and key in NAN_MEANS_IDLE else v)
        if sample.metrics.get("availability") == 0:
            sample.status, sample.reason = "down", "service reports itself unavailable"
        return sample

    def _links(self, ts, reporting: set[str], values) -> list[LinkSample]:
        """Connections of the services that report; a silent caller says nothing about its own."""
        out = []
        for s in self.services:
            for target in s.targets if s.name in reporting else ():
                link = LinkSample(s.name, target, ts)
                for key, vector in values.items():
                    if (s.name, target) in vector:
                        v = vector[(s.name, target)]
                        link.put(key, None if math.isnan(v) and key in NAN_MEANS_IDLE else v)
                out.append(link)
        return out

    async def _infra(self, ts) -> list[InfraSample]:
        out = []
        for component, queries in INFRA_QUERIES.items():
            up, *values = await asyncio.gather(
                self._single(INFRA_UP[component]), *(self._single(q) for q in queries.values()))
            s = InfraSample(component, ts, "up")
            if up is None:
                s.status, s.reason = "unresponsive", "its exporter delivers nothing"
            elif up != 1:
                s.status, s.reason = "down", "its exporter reports it down"
            else:
                s.metrics = {k: round(v, 4) for k, v in zip(queries, values, strict=True)
                             if v is not None and math.isfinite(v)}
            out.append(s)
        return out

    # -------------------------------------------------------------------- HTTP
    async def _query(self, q: str) -> dict:
        r = await self.http.get("/api/v1/query", params={"query": q})
        r.raise_for_status()
        body = r.json()
        if body["status"] != "success":
            raise ValueError(body.get("error", "query failed"))
        return body["data"]

    async def _vector(self, q: str) -> dict[str, float]:
        data = await self._query(q)
        return {x["metric"]["service"]: float(x["value"][1]) for x in data["result"]
                if "service" in x["metric"]}

    async def _pairs(self, q: str) -> dict[tuple[str, str], float]:
        data = await self._query(q)
        return {(x["metric"]["service"], x["metric"]["target"]): float(x["value"][1])
                for x in data["result"] if {"service", "target"} <= x["metric"].keys()}

    async def _deployments(self) -> dict[str, str]:
        data = await self._query("bank_info")
        return {x["metric"]["service"]: x["metric"]["deployment"] for x in data["result"]
                if x["metric"].get("deployment") in ("process", "container")}

    async def _scalar(self, q: str) -> float:
        return float((await self._query(q))["result"][1])

    async def _single(self, q: str) -> float | None:
        result = (await self._query(q))["result"]
        return float(result[0]["value"][1]) if result else None

    async def _target_errors(self) -> dict[str, str]:
        r = await self.http.get("/api/v1/targets", params={"state": "active"})
        r.raise_for_status()
        return {t["labels"]["service"]: t["lastError"] for t in r.json()["data"]["activeTargets"]
                if "service" in t["labels"] and t["lastError"]}

    async def aclose(self) -> None:
        await self.http.aclose()
