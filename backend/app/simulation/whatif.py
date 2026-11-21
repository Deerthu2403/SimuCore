"""What-if simulation engine (SimPy) - contract defined in Phase 1, built in Phase 4.

Planned model:
  * every service = ``simpy.Resource`` with capacity = replicas x per-replica concurrency,
  * service times drawn from a calibrated log-normal (params from the Calibrator),
  * requests enter at gateways, fan out along ``calls`` edges with edge weight as
    call probability,
  * ``FaultSpec`` changes capacity (crash/saturation), service time (latency) or
    connectivity (partition) during its window,
  * KPIs: p50/p95/p99 latency, error %, failed transactions/min, SLA breaches.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.contracts import SimulationResult, SimulationScenario
from app.domain.models import Topology


class WhatIfEngine(Protocol):
    def run(self, topology: Topology, scenario: SimulationScenario) -> SimulationResult: ...


class SimPyWhatIfEngine:
    """Placeholder implementation; see module docstring for the Phase 4 design."""

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}

    def run(self, topology: Topology, scenario: SimulationScenario) -> SimulationResult:
        raise NotImplementedError("SimPy what-if engine is scheduled for Phase 4")
