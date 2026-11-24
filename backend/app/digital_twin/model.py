"""The vocabulary of the digital twin. Pure data: no I/O, no knowledge of the real system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any


class ComponentType(str, Enum):
    MICROSERVICE = "microservice"
    DATABASE = "database"
    SERVER = "server"
    NETWORK = "network"
    MONITORING = "monitoring"


class Status(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    OVERLOADED = "overloaded"
    FAILED = "failed"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"  # nothing trustworthy is known: never a guess


class Level(IntEnum):
    """What one observation says, before persistence is taken into account."""

    OK = 0
    WARNING = 1
    OVERLOAD = 2
    FAILURE = 3


LEVEL_STATUS = {Level.OK: Status.HEALTHY, Level.WARNING: Status.WARNING,
                Level.OVERLOAD: Status.OVERLOADED, Level.FAILURE: Status.FAILED}
STATUS_LEVEL = {s: v for v, s in LEVEL_STATUS.items()}


class LinkKind(str, Enum):
    CALLS = "calls"  # network connection between two services
    STORES_IN = "stores_in"  # a service and the database behind it
    HOSTED_ON = "hosted_on"
    CONNECTED_TO = "connected_to"  # attached to a network
    MONITORS = "monitors"


@dataclass(frozen=True)
class Finding:
    """One reason why an object is not simply healthy."""

    level: Level
    rule: str
    message: str
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    immediate: bool = False  # certain enough to act on a single observation


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    type: ComponentType
    name: str
    description: str
    layer: str
    group: str
    criticality: int
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LinkSpec:
    """``source`` depends on ``target``."""

    id: str
    source: str
    target: str
    kind: LinkKind
    mode: str  # required | degradable | best_effort | structural
    weight: float
    purpose: str = ""
    timeout_ms: int | None = None

    @property
    def propagates(self) -> bool:
        """Does a failure of the target put the source at risk?"""
        return self.mode in ("required", "degradable")


# --------------------------------------------------------------------- observations
# Immutable copies of what monitoring reported. The twin never sees anything else.


@dataclass(frozen=True)
class ServiceObs:
    status: str  # up | down | unresponsive
    reason: str
    metrics: dict[str, float | None]
    deployment: str | None = None
    partial: bool = False  # some metrics were missing or impossible


@dataclass(frozen=True)
class InfraObs:
    status: str
    reason: str
    observed_at: datetime
    metrics: dict[str, float]


@dataclass(frozen=True)
class Observation:
    run_id: str
    sequence: int
    observed_at: datetime
    origin: str  # measured | synthetic
    services: dict[str, ServiceObs]
    links: dict[tuple[str, str], dict[str, float | None]]
    infra: dict[str, InfraObs]


# ------------------------------------------------------------------------ state
@dataclass
class ObjectState:
    """The changing part of a component or link."""

    status: Status = Status.UNKNOWN
    since: datetime | None = None
    previous: Status | None = None
    reason: str = "no data yet"
    findings: tuple[Finding, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    quality: str = "none"  # complete | partial | derived | none


@dataclass(frozen=True)
class Transition:
    ts: datetime
    version: int
    object_kind: str  # component | link
    object_id: str
    from_status: Status | None
    to_status: Status
    reason: str
    root_causes: tuple[str, ...]
    origin: str | None


@dataclass(frozen=True)
class Change:
    """What one synchronisation step did."""

    version: int
    components: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset()
    transitions: tuple[Transition, ...] = ()
    structure_changed: bool = False

    def __bool__(self) -> bool:
        return bool(self.components or self.links or self.structure_changed)
