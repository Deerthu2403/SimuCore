"""Core domain models of the digital twin. Pure data - no I/O.

Edge convention (ADR-002): ``Edge(source, target)`` means *source depends on
target*. Failures therefore propagate from target to source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class NodeKind(str, Enum):
    GATEWAY = "gateway"
    SERVICE = "service"
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"
    SERVER = "server"
    LOAD_BALANCER = "load_balancer"
    FIREWALL = "firewall"
    SWITCH = "switch"
    EXTERNAL = "external"


class NodeLayer(str, Enum):
    EDGE = "edge"
    APPLICATION = "application"
    DATA = "data"
    COMPUTE = "compute"
    NETWORK = "network"


class EdgeKind(str, Enum):
    CALLS = "calls"
    READS_WRITES = "reads_writes"
    PUBLISHES = "publishes"
    HOSTED_ON = "hosted_on"
    ROUTES_THROUGH = "routes_through"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    DOWN = "down"
    UNKNOWN = "unknown"

    @property
    def severity(self) -> int:
        return _STATUS_SEVERITY[self]


_STATUS_SEVERITY = {
    HealthStatus.UNKNOWN: -1,
    HealthStatus.HEALTHY: 0,
    HealthStatus.DEGRADED: 1,
    HealthStatus.CRITICAL: 2,
    HealthStatus.DOWN: 3,
}

#: Normalised metric keys every adapter must map to.
STANDARD_METRICS: tuple[str, ...] = (
    "cpu_pct",
    "mem_pct",
    "latency_ms",
    "error_rate",
    "rps",
    "availability",
)


class Node(BaseModel):
    id: str
    name: str
    kind: NodeKind
    layer: NodeLayer
    criticality: int = Field(ge=1, le=5, description="Business criticality, 5 = highest")
    capacity: dict[str, float] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    source: str = Field(description="Dependent node")
    target: str = Field(description="Dependency")
    kind: EdgeKind
    weight: float = Field(ge=0.0, le=1.0, description="Dependency strength / coupling")


class Topology(BaseModel):
    version: int = 1
    nodes: list[Node]
    edges: list[Edge]


class MetricSample(BaseModel):
    node_id: str
    ts: datetime = Field(default_factory=utcnow)
    metrics: dict[str, float]


class NodeState(BaseModel):
    node_id: str
    status: HealthStatus = HealthStatus.UNKNOWN
    metrics: dict[str, float] = Field(default_factory=dict)
    updated_at: datetime | None = None
