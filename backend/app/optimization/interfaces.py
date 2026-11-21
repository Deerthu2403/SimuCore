"""Resource optimisation contract (NSGA-II via pymoo, Phase 5).

Decision vector: per service (replicas, cpu_cores, mem_gb), bounded by constraints.
Objectives (minimise): monthly cost, predicted p95 latency (surrogate / fast SimPy
run), failure risk (business impact from the propagation engine).
Constraints: total budget, min replicas for criticality >= 4, SLA latency.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.domain.contracts import OptimizationProposal
from app.domain.models import NodeState, Topology


class OptimizationRequest(BaseModel):
    objectives: list[str] = Field(default_factory=lambda: ["cost", "p95_latency", "risk"])
    budget: float | None = None
    sla_p95_ms: float | None = 250.0
    population: int = 80
    generations: int = 100
    seed: int = 42


class Optimizer(Protocol):
    def optimize(self, topology: Topology, states: dict[str, NodeState],
                 request: OptimizationRequest) -> OptimizationProposal: ...
