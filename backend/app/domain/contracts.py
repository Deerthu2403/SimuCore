"""Inter-module contracts. These are the ONLY shapes modules exchange on the
event bus and through the API (see docs/04-module-communication.md)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.domain.models import NodeState, utcnow


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# --------------------------------------------------------------------- twin
class TwinDelta(BaseModel):
    version: int
    states: list[NodeState]
    origin: str | None = None  # "measured" or "synthetic"


# ----------------------------------------------------------- explainability
class ExplanationFactor(BaseModel):
    name: str
    value: float | str | None = None
    contribution: float = Field(description="Signed contribution to the decision")


class Explanation(BaseModel):
    summary: str
    method: str  # 'tree_shap' | 'propagation_path' | 'pareto_tradeoff' | 'rule'
    factors: list[ExplanationFactor] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


# ---------------------------------------------------------- ML / prediction
class AnomalyEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ts: datetime = Field(default_factory=utcnow)
    node_id: str
    score: float = Field(ge=0, le=1)
    severity: Severity
    contributing_metrics: dict[str, float] = Field(default_factory=dict)
    model_version: str | None = None


class FailurePrediction(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    predicted_at: datetime = Field(default_factory=utcnow)
    node_id: str
    horizon_s: int
    probability: float = Field(ge=0, le=1)
    top_features: list[ExplanationFactor] = Field(default_factory=list)
    model_version: str | None = None


# ------------------------------------------------------------- propagation
class ImpactedNode(BaseModel):
    node_id: str
    impact: float = Field(ge=0, le=1)
    depth: int
    path: list[str] = Field(description="Propagation path from origin to this node")
    criticality: int


class PropagationRequest(BaseModel):
    failed_nodes: list[str] = Field(min_length=1)
    initial_impact: float = Field(default=1.0, ge=0, le=1)
    decay: float = Field(default=0.9, gt=0, le=1)
    threshold: float = Field(default=0.05, ge=0, le=1)


class PropagationResult(BaseModel):
    origin: list[str]
    method: str = "weighted_bfs"
    impacted: list[ImpactedNode]
    business_impact: float = Field(description="Sum of impact x criticality over impacted nodes")


# -------------------------------------------------------------- simulation
class FaultSpec(BaseModel):
    node_id: str
    type: str  # 'crash' | 'latency' | 'saturation' | 'partition'
    start_s: float = 0
    duration_s: float = 60
    magnitude: float = 1.0


class SimulationScenario(BaseModel):
    name: str
    faults: list[FaultSpec] = Field(default_factory=list)
    load_multiplier: float = 1.0
    duration_s: int = 600
    seed: int = 42


class SimulationResult(BaseModel):
    scenario: SimulationScenario
    kpis: dict[str, float]
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    params_version: int | None = None


# ------------------------------------------------------------ optimisation
class ParetoSolution(BaseModel):
    decision: dict[str, dict[str, float]]  # {service_id: {replicas, cpu, mem}}
    objectives: dict[str, float]  # {cost, p95_latency, risk}
    rank: int = 0


class OptimizationProposal(BaseModel):
    objectives: list[str]
    pareto_front: list[ParetoSolution]
    recommended_index: int | None = None


# ------------------------------------------------------------- calibration
class CalibrationReport(BaseModel):
    target: str
    metric_name: str
    metric_before: float | None
    metric_after: float | None
    drift: dict[str, float] = Field(default_factory=dict)
    action: str  # 'none' | 'recalibrate' | 'retrain' | 'refit_params'
    new_version: str | None = None


# ---------------------------------------------------------- recommendation
class Recommendation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utcnow)
    source_module: str
    title: str
    action: dict[str, Any]
    target_nodes: list[str]
    priority: float
    confidence: float | None = None
    explanation: Explanation
