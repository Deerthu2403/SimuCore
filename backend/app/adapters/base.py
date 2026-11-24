"""Telemetry adapter contract - READ-ONLY by construction (ADR-005).

An adapter connects SimuCore to a source of truth (simulated bank, Prometheus,
OpenTelemetry, CMDB). It may only *read*. There is intentionally no method for
writing, and HTTP-based adapters must use :class:`ReadOnlyHttpClient`, which
refuses every verb except GET/HEAD.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from app.domain.models import MetricSample, Topology


class ReadOnlyViolation(RuntimeError):
    """Raised when code attempts a mutating operation through an adapter."""


class TelemetryAdapter(ABC):
    name: ClassVar[str]
    read_only: ClassVar[bool] = True
    #: Where the data originally comes from: "measured", "synthetic", or None if not known (yet).
    origin: str | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "read_only", True):
            raise TypeError(f"{cls.__name__}: SimuCore adapters must be read-only")
        forbidden = {"write", "put", "post", "delete", "patch", "execute", "apply"}
        clash = forbidden & {m.lower() for m in vars(cls)}
        if clash:
            raise TypeError(f"{cls.__name__} defines mutating method(s): {sorted(clash)}")

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def fetch_topology(self) -> Topology: ...

    @abstractmethod
    async def poll(self) -> list[MetricSample]:
        """Return the latest samples (one call per sync tick)."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class ReadOnlyHttpClient:
    """Thin httpx wrapper that only allows safe HTTP methods."""

    SAFE_METHODS = frozenset({"GET", "HEAD"})

    def __init__(self, base_url: str, timeout: float = 5.0, **kwargs: Any) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if method.upper() not in self.SAFE_METHODS:
            raise ReadOnlyViolation(f"HTTP {method} is not permitted for telemetry adapters")
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
