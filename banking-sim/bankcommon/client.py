"""Service-to-service HTTP client: declared edges only, timeouts, retry, circuit breaker."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from bankcommon.config import registry, service_url
from bankcommon.errors import DependencyUnavailable, UndeclaredDependency
from bankcommon.telemetry import Telemetry, header_bytes

TransportFactory = Callable[[str], httpx.AsyncBaseTransport | None]

# A pooled connection that died because the peer restarted. Every endpoint is idempotent, so
# one retry on a fresh connection is safe. Timeouts and refused connections are not retried.
STALE_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.cooldown_s, self.clock = threshold, cooldown_s, clock
        self.failures = 0
        self.opened_at: float | None = None
        self.last_path: list[str] | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "half_open" and not self._probing:
            self._probing = True  # exactly one probe at a time
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at, self._probing, self.last_path = 0, None, False, None

    def failure(self, path: list[str]) -> None:
        self.failures += 1
        self.last_path = path
        if self._probing or self.failures >= self.threshold:
            self.opened_at = self.clock()
        self._probing = False

    def abandon(self) -> None:
        """The call was cancelled: neither success nor failure, but free the probe slot."""
        self._probing = False


class ServiceClient:
    def __init__(self, service: str, internal_key: str,
                 transport_factory: TransportFactory | None = None,
                 telemetry: Telemetry | None = None) -> None:
        self.service, self.telemetry = service, telemetry
        self.edges = registry().of(service)
        self.calls: Counter[str] = Counter()
        self.failed: Counter[str] = Counter()
        self.breakers = {t: CircuitBreaker() for t in self.edges}
        if telemetry:
            for t, b in self.breakers.items():
                telemetry.declare_link(t, lambda b=b: b.state)
        self._clients = {
            t: httpx.AsyncClient(
                base_url=service_url(t),
                timeout=httpx.Timeout(d.timeout_ms / 1000, connect=min(0.5, d.timeout_ms / 1000)),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                headers={"X-Internal-Key": internal_key, "X-Caller": service},
                transport=transport_factory(t) if transport_factory else None,
                trust_env=False,
            )
            for t, d in self.edges.items()
        }

    async def call(self, target: str, method: str, path: str, *, correlation_id: str = "",
                   json: Any = None, params: dict | None = None) -> httpx.Response:
        """Return the response for any status < 500; raise DependencyUnavailable otherwise."""
        if target not in self.edges:
            raise UndeclaredDependency(f"{self.service} -> {target} is not in dependency_map.json")
        breaker = self.breakers[target]
        if not breaker.allow():
            if self.telemetry:
                self.telemetry.link(target, ok=False, seconds=None)
            raise DependencyUnavailable(self.service, target, "circuit open", breaker.last_path)
        self.calls[target] += 1
        started = time.perf_counter()
        try:
            resp, reason = await self._send(target, method, path, json, params, correlation_id)
        except BaseException:
            breaker.abandon()
            raise
        ok = resp is not None and resp.status_code < 500
        self._measure(target, resp, time.perf_counter() - started, ok)
        if resp is not None and ok:
            breaker.success()
            return resp
        nested = [target]
        if resp is not None:
            nested, reason = _nested_failure(resp, target)
        breaker.failure(nested)
        self.failed[target] += 1
        raise DependencyUnavailable(self.service, target, reason, nested)

    async def _send(self, target: str, method: str, path: str, json: Any, params: dict | None,
                    correlation_id: str) -> tuple[httpx.Response | None, str]:
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        budget = self.edges[target].timeout_ms / 1000
        reason = ""
        for _attempt in range(2):
            try:
                # httpx timeouts are per phase; this bounds the whole call.
                async with asyncio.timeout(budget):
                    return await self._clients[target].request(
                        method, path, json=json, params=params, headers=headers), ""
            except (httpx.ConnectError, httpx.ConnectTimeout):
                return None, "unreachable"
            except (httpx.TimeoutException, TimeoutError):
                return None, "timeout"
            except STALE_CONNECTION as exc:
                reason = f"connection lost ({type(exc).__name__})"
        return None, reason

    def _measure(self, target: str, resp: httpx.Response | None, seconds: float,
                 ok: bool) -> None:
        if self.telemetry is None:
            return
        self.telemetry.link(target, ok, seconds)
        if target == "database":  # timeouts and refusals count: that is the response time felt
            self.telemetry.db_response(seconds)
        if resp is not None:
            req = resp.request
            self.telemetry.traffic(
                sent=header_bytes(req.headers.raw) + len(req.content),
                received=header_bytes(resp.headers.raw) + len(resp.content))

    async def ping(self, target: str) -> bool:
        """Liveness probe used by readiness checks. Bypasses the circuit breaker."""
        try:
            r = await self._clients[target].get("/health/live", timeout=0.8)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()


def _nested_failure(resp: httpx.Response, target: str) -> tuple[list[str], str]:
    try:
        err = resp.json()["error"]
        return (err.get("failure_path") or [target]), err.get("code", f"HTTP {resp.status_code}")
    except (ValueError, KeyError, TypeError, AttributeError):
        return [target], f"HTTP {resp.status_code}"
