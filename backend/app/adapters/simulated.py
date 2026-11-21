"""Adapter that reads from the simulated bank (Phase 1 default source)."""

from __future__ import annotations

from app.adapters.base import TelemetryAdapter
from app.domain.models import MetricSample, Topology
from app.simulation.infrastructure import SimulatedBankInfrastructure


class SimulatedAdapter(TelemetryAdapter):
    name = "simulated"

    def __init__(self, infrastructure: SimulatedBankInfrastructure) -> None:
        self._infra = infrastructure
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def fetch_topology(self) -> Topology:
        return self._infra.topology.model_copy(deep=True)

    async def poll(self) -> list[MetricSample]:
        if not self._connected:
            raise RuntimeError("Adapter not connected")
        return self._infra.step()

    async def close(self) -> None:
        self._connected = False
