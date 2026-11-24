"""Scrapes the services' /metrics pages itself. Used where Prometheus is not running, and as an
independent second implementation to check the Prometheus pipeline against."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
import psutil
from prometheus_client.parser import text_string_to_metric_families

from monitor.config import ServiceSpec, Settings, check_host
from monitor.models import Collection, InfraSample, LinkSample, Sample, utcnow

MAX_PAGE = 2 * 1024 * 1024
SIMPLE = {"bank_process_cpu_seconds_total", "bank_memory_bytes", "bank_memory_limit_bytes",
          "bank_network_receive_bytes_total", "bank_network_transmit_bytes_total",
          "bank_active_connections", "bank_request_queue_length", "bank_in_flight_requests",
          "bank_available", "bank_service_request_seconds_sum",
          "bank_service_request_seconds_count", "bank_db_response_seconds_sum",
          "bank_db_response_seconds_count", "bank_db_pool_in_use", "bank_db_pool_size",
          "bank_db_query_failures_total"}
LINK_SERIES = {"bank_link_attempts_total", "bank_link_failures_total", "bank_link_seconds_sum",
               "bank_link_seconds_count", "bank_link_circuit"}


@dataclass
class Scrape:
    at: float
    values: dict[str, float] = field(default_factory=dict)
    outcomes: dict[str, float] = field(default_factory=dict)
    buckets: dict[float, float] = field(default_factory=dict)  # latency, cumulative
    links: dict[tuple[str, str], float] = field(default_factory=dict)  # (target, series)
    deployment: str | None = None


def parse(text: str, service: str, at: float) -> Scrape:
    s = Scrape(at)
    for family in text_string_to_metric_families(text):
        for x in family.samples:
            if x.labels.get("service") != service:
                continue
            if x.name in SIMPLE:
                s.values[x.name] = x.value
            elif x.name == "bank_service_requests_total":
                s.outcomes[x.labels["outcome"]] = x.value
            elif x.name == "bank_service_request_seconds_bucket":
                s.buckets[float(x.labels["le"])] = x.value
            elif x.name in LINK_SERIES:
                s.links[(x.labels["target"], x.name)] = x.value
            elif x.name == "bank_info" and x.labels["deployment"] in ("process", "container"):
                s.deployment = x.labels["deployment"]
    if not s.values:
        raise ValueError("no metrics of this service on the page")
    return s


def increase(series: list[float]) -> float:
    """Total growth of a counter, allowing for resets (a restarted process starts at 0)."""
    total = 0.0
    for a, b in zip(series, series[1:], strict=False):
        total += b - a if b >= a else b
    return total


def quantile(q: float, buckets: dict[float, float]) -> float:
    """Same linear interpolation as Prometheus' histogram_quantile."""
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return math.nan
    rank, previous_bound, previous_count = q * total, 0.0, 0.0
    for b in bounds:
        if buckets[b] >= rank:
            if math.isinf(b):
                return previous_bound
            share = (rank - previous_count) / max(buckets[b] - previous_count, 1e-12)
            return previous_bound + (b - previous_bound) * share
        previous_bound, previous_count = b, buckets[b]
    return previous_bound


def compute(history: list[Scrape], sample: Sample) -> None:
    """Fill a sample from the scrapes inside the rate window (newest last)."""
    last, v = history[-1], history[-1].values
    gauges = {"mem_bytes": "bank_memory_bytes", "active_connections": "bank_active_connections",
              "queue_length": "bank_request_queue_length", "in_flight": "bank_in_flight_requests",
              "availability": "bank_available", "db_pool_in_use": "bank_db_pool_in_use",
              "db_pool_size": "bank_db_pool_size"}
    for key, name in gauges.items():
        if name in v:
            sample.put(key, v[name])
    if "bank_memory_bytes" in v and v.get("bank_memory_limit_bytes"):
        sample.put("mem_pct", v["bank_memory_bytes"] / v["bank_memory_limit_bytes"] * 100)

    seconds = last.at - history[0].at
    if len(history) < 2 or seconds <= 0:
        return  # no rates yet

    def grow(name: str) -> float | None:
        if name not in v:
            return None
        return increase([h.values[name] for h in history if name in h.values])

    for key, name, scale in (("cpu_pct", "bank_process_cpu_seconds_total", 100.0),
                             ("net_rx_bps", "bank_network_receive_bytes_total", 1.0),
                             ("net_tx_bps", "bank_network_transmit_bytes_total", 1.0)):
        g = grow(name)
        if g is not None:
            sample.put(key, g / seconds * scale)
    if last.outcomes:
        by = {o: increase([h.outcomes.get(o, 0.0) for h in history]) for o in last.outcomes}
        total = sum(by.values())
        sample.put("rps", total / seconds)
        sample.put("error_rate", by.get("server_error", 0.0) / total if total else None)
    for key, name in (("latency_mean_ms", "bank_service_request_seconds"),
                      ("db_response_ms", "bank_db_response_seconds")):
        total, count = grow(f"{name}_sum"), grow(f"{name}_count")
        if total is not None and count is not None:
            sample.put(key, total / count * 1000 if count else None)
    failed, queries = grow("bank_db_query_failures_total"), grow("bank_db_response_seconds_count")
    if failed is not None and queries is not None:
        sample.put("db_failure_rate", min(1.0, failed / queries) if queries else None)
    if last.buckets:
        grown = {b: increase([h.buckets.get(b, 0.0) for h in history]) for b in last.buckets}
        q = quantile(0.95, grown)
        sample.put("latency_ms", None if math.isnan(q) else q * 1000)


def compute_link(history: list[Scrape], link: LinkSample) -> None:
    last, seconds = history[-1].links, history[-1].at - history[0].at

    def grow(name: str) -> float | None:
        key = (link.target, name)
        if key not in last:
            return None
        return increase([h.links[key] for h in history if key in h.links])

    if (link.target, "bank_link_circuit") in last:
        link.put("circuit", last[(link.target, "bank_link_circuit")])
    if len(history) < 2 or seconds <= 0:
        return
    attempts, failures = grow("bank_link_attempts_total"), grow("bank_link_failures_total")
    if attempts is not None and failures is not None:
        link.put("calls_per_s", attempts / seconds)
        link.put("failure_rate", min(1.0, failures / attempts) if attempts else None)
    total, count = grow("bank_link_seconds_sum"), grow("bank_link_seconds_count")
    if total is not None and count is not None:
        link.put("latency_ms", total / count * 1000 if count else None)


class DirectSource:
    name = "direct"

    def __init__(self, settings: Settings, services: tuple[ServiceSpec, ...],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.services, self.window_s = services, settings.window_s
        self.urls = {s.name: check_host(
            settings.bank_url_template.format(service=s.name, port=s.port), services) + "/metrics"
            for s in services}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(1.0, connect=0.75), trust_env=False,
                                      transport=transport, follow_redirects=False)
        self.history: dict[str, deque[Scrape]] = {s.name: deque() for s in services}
        self._net = None

    async def collect(self, with_infra: bool) -> Collection:
        ts = utcnow()
        samples = await asyncio.gather(*(self._one(s, ts) for s in self.services))
        links = []
        for s, sample in zip(self.services, samples, strict=True):
            if sample.status == "up":
                for target in s.targets:
                    link = LinkSample(s.name, target, ts)
                    compute_link(list(self.history[s.name]), link)
                    links.append(link)
        return Collection(ts, "measured", list(samples), [self._host(ts)] if with_infra else [],
                          links)

    async def _one(self, s: ServiceSpec, ts) -> Sample:
        sample, history = Sample(s.name, s.node_id, ts, "up"), self.history[s.name]
        try:
            async with asyncio.timeout(1.2):
                r = await self.http.get(self.urls[s.name])
            r.raise_for_status()
            if len(r.content) > MAX_PAGE:
                raise ValueError("page too large")
            history.append(parse(r.text, s.name, time.monotonic()))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                httpx.ReadError) as exc:
            history.clear()
            sample.status, sample.reason = "down", f"unreachable ({type(exc).__name__})"
            sample.put("availability", 0.0)
            return sample
        except Exception as exc:  # timeout, HTTP error, unreadable page: state unknown
            sample.status, sample.reason = "unresponsive", f"{type(exc).__name__}: {exc}"[:200]
            return sample
        while len(history) > 2 and history[-1].at - history[1].at >= self.window_s:
            history.popleft()
        compute(list(history), sample)
        sample.deployment = history[-1].deployment
        if sample.metrics.get("availability") == 0:
            sample.status, sample.reason = "down", "service reports itself unavailable"
        return sample

    def _host(self, ts) -> InfraSample:
        """The computer this monitor runs on."""
        net, now = psutil.net_io_counters(), time.monotonic()
        m = {"cpu_pct": psutil.cpu_percent(), "mem_pct": psutil.virtual_memory().percent}
        if self._net:
            then, rx, tx = self._net
            m["net_rx_bps"] = max(0.0, (net.bytes_recv - rx) / max(now - then, 1e-3))
            m["net_tx_bps"] = max(0.0, (net.bytes_sent - tx) / max(now - then, 1e-3))
        self._net = (now, net.bytes_recv, net.bytes_sent)
        return InfraSample("monitor-host", ts, "up", {k: round(v, 2) for k, v in m.items()})

    async def aclose(self) -> None:
        await self.http.aclose()
