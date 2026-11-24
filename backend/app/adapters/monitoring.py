"""Read-only adapter over the monitoring service (the recommended source for the live twin).

One GET per tick. The monitoring service already separates "down" from "unknown": services it
knows nothing about are simply absent, so their nodes age to unknown. Old data is refused.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.adapters.base import ReadOnlyHttpClient, TelemetryAdapter
from app.domain.models import OPTIONAL_METRICS, STANDARD_METRICS, MetricSample, Topology
from app.simulation.bank_services import DEFAULT_MAP, load_bank_services_topology

log = logging.getLogger(__name__)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "monitoring"}
LIMITS = {"cpu_pct": 100.0, "mem_pct": 100.0, "error_rate": 1.0, "availability": 1.0}
MAX_BODY = 256 * 1024
MAX_AGE_S = 15.0


class MonitoringAdapter(TelemetryAdapter):
    name = "monitoring"

    def __init__(self, url: str = "http://127.0.0.1:8200", map_path: Path = DEFAULT_MAP,
                 timeout_s: float = 1.5,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if urlsplit(url).hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Refusing {url!r}: not the SimuCore monitoring service")
        self._topology = load_bank_services_topology(map_path)
        self._known = {n.id for n in self._topology.nodes}
        self._url, self._timeout_s, self._transport = url, timeout_s, transport
        self._http: ReadOnlyHttpClient | None = None
        self._problem: str | None = None
        self.origin: str | None = None  # "measured" or "synthetic", as the service reports it

    async def connect(self) -> None:
        self._http = ReadOnlyHttpClient(
            self._url, timeout=self._timeout_s, trust_env=False, follow_redirects=False,
            **({"transport": self._transport} if self._transport else {}))

    async def fetch_topology(self) -> Topology:
        return self._topology.model_copy(deep=True)

    async def poll(self) -> list[MetricSample]:
        if self._http is None:
            raise RuntimeError("Adapter not connected")
        try:
            r = await self._http.get("/api/v1/twin/samples")
            r.raise_for_status()
            samples = self._parse(r.content)
        except (httpx.HTTPError, ValueError) as exc:
            self._log(f"{type(exc).__name__}: {exc}"[:200])
            return []  # nothing is known: nodes age to unknown
        self._log(None)
        return samples

    def _parse(self, content: bytes) -> list[MetricSample]:
        if len(content) > MAX_BODY:
            raise ValueError("response too large")
        body = json.loads(content)
        if not isinstance(body, dict) or body.get("origin") not in ("measured", "synthetic"):
            raise ValueError("no data yet, or not a monitoring answer")
        age = body.get("age_s")
        if body.get("fresh") is not True or not isinstance(age, int | float) or age > MAX_AGE_S:
            raise ValueError("the monitoring data is out of date")
        self.origin = body["origin"]
        out = []
        for item in body.get("samples") or []:
            node_id = item.get("node_id") if isinstance(item, dict) else None
            if node_id not in self._known:
                continue
            metrics = self._metrics(item.get("metrics"))
            if metrics is None:
                self._log(f"unusable metrics for {node_id}")
                continue
            out.append(MetricSample(node_id=node_id, metrics=metrics))
        return out

    @staticmethod
    def _metrics(raw: object) -> dict[str, float] | None:
        """Known metrics with possible values; None if the node's data cannot be trusted."""
        if not isinstance(raw, dict) or "availability" not in raw:
            return None
        out: dict[str, float] = {}
        for key in (*STANDARD_METRICS, *OPTIONAL_METRICS):
            if key not in raw:
                continue
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) \
                    or not 0 <= v <= LIMITS.get(key, 1e13):
                return None
            out[key] = float(v)
        return out

    def _log(self, problem: str | None) -> None:
        if problem != self._problem:
            log.warning("monitoring service: %s", problem or "ok")
            self._problem = problem

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
