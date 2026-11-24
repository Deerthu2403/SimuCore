"""Read-only adapter over the simulated banking microservices (banking-sim).

Each tick it issues one GET /telemetry per service, in parallel. What it reports:

  answered               the service's own metrics
  connection refused     availability 0, so the twin shows the node as down
  slow or unreadable     nothing, so the node ages to "unknown": the twin does not guess

It never sends the bank's internal key, so it could not reach a control endpoint even by mistake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.adapters.base import ReadOnlyHttpClient, TelemetryAdapter
from app.domain.models import STANDARD_METRICS, MetricSample, Node, Topology
from app.simulation.bank_services import DEFAULT_MAP, load_bank_services_topology

log = logging.getLogger(__name__)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 64 * 1024
LIMITS = {"cpu_pct": 100.0, "mem_pct": 100.0, "error_rate": 1.0, "availability": 1.0}


class BankServicesAdapter(TelemetryAdapter):
    name = "bank_services"
    origin = "measured"

    def __init__(self, url_template: str = "http://127.0.0.1:{port}",
                 map_path: Path = DEFAULT_MAP, timeout_s: float = 1.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._topology = load_bank_services_topology(map_path)
        self._nodes: dict[str, Node] = {str(n.attributes["service"]): n
                                        for n in self._topology.nodes}
        self._urls = {s: self._checked(url_template.format(service=s, port=n.attributes["port"]))
                      for s, n in self._nodes.items()}
        self._timeout_s, self._transport = timeout_s, transport
        self._clients: dict[str, ReadOnlyHttpClient] = {}
        self._last: dict[str, str] = {}

    def _checked(self, url: str) -> str:
        host = urlsplit(url).hostname
        if host not in LOOPBACK_HOSTS | {f"bank-{s}" for s in self._nodes}:
            raise ValueError(f"Refusing host {host!r}: only the simulated bank is allowed")
        return url

    async def connect(self) -> None:
        # Succeeds with every service down: that is a valid state to show.
        timeout = httpx.Timeout(self._timeout_s, connect=min(0.5, self._timeout_s))
        self._clients = {
            s: ReadOnlyHttpClient(url, timeout=timeout, trust_env=False, follow_redirects=False,
                                  **({"transport": self._transport} if self._transport else {}))
            for s, url in self._urls.items()}

    async def fetch_topology(self) -> Topology:
        return self._topology.model_copy(deep=True)

    async def poll(self) -> list[MetricSample]:
        if not self._clients:
            raise RuntimeError("Adapter not connected")
        found = await asyncio.gather(*(self._read(s) for s in self._nodes))
        return [s for s in found if s is not None]

    async def _read(self, service: str) -> MetricSample | None:
        node_id = self._nodes[service].id
        try:
            # httpx limits each phase separately; this bounds the whole exchange.
            async with asyncio.timeout(self._timeout_s):
                r = await self._clients[service].get("/telemetry")
            r.raise_for_status()
            metrics = self._parse(service, r.content)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # On Windows a closed local port shows up as a connect timeout.
            self._log(service, "unreachable")
            return MetricSample(node_id=node_id, metrics={"availability": 0.0})
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            self._log(service, f"no usable telemetry ({type(exc).__name__}: {exc})")
            return None
        self._log(service, "ok")
        return MetricSample(node_id=node_id, metrics=metrics)

    @staticmethod
    def _parse(service: str, content: bytes) -> dict[str, float]:
        """Telemetry is untrusted input: check identity, names, types and ranges."""
        if len(content) > MAX_BODY:
            raise ValueError("response too large")
        body = json.loads(content)
        if not isinstance(body, dict) or body.get("service") != service:
            raise ValueError("a different service answered")
        raw = body.get("metrics")
        if not isinstance(raw, dict) or "availability" not in raw:
            raise ValueError("metrics missing")
        out: dict[str, float] = {}
        for key in STANDARD_METRICS:
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{key} is not a number")
            if not math.isfinite(value) or not 0 <= value <= LIMITS.get(key, 1e9):
                raise ValueError(f"{key} is out of range")
            out[key] = float(value)
        return out

    def _log(self, service: str, state: str) -> None:
        if self._last.get(service) != state:  # only transitions, not once per second
            if state != "ok" or service in self._last:
                log.warning("bank service %s: %s", service, state)
            self._last[service] = state

    async def close(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients = {}
