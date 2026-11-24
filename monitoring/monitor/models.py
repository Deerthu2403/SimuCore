"""What a source delivers each tick, independent of where it came from."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from monitor.catalog import BY_KEY, LINK_METRICS

Status = Literal["up", "down", "unresponsive"]
Origin = Literal["measured", "synthetic"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceUnavailable(RuntimeError):
    """The monitoring source itself failed; nothing is known about the services."""


@dataclass
class Sample:
    """One service at one moment.

    ``metrics`` holds a number, or None for "legitimately no value now" (an idle service has no
    latency). A key that is absent was expected but not delivered: that is a monitoring problem.
    """

    service: str
    node_id: str
    ts: datetime
    status: Status
    metrics: dict[str, float | None] = field(default_factory=dict)
    invalid: dict[str, str] = field(default_factory=dict)  # metric -> what was wrong with it
    reason: str = ""
    deployment: str | None = None  # "process" or "container"

    def put(self, key: str, value: float | None) -> None:
        """Store a value, or reject it as impossible."""
        _put(self, key, value, BY_KEY[key].maximum)


@dataclass
class LinkSample:
    """One network connection between two services, as its caller measured it."""

    source: str
    target: str
    ts: datetime
    metrics: dict[str, float | None] = field(default_factory=dict)
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}->{self.target}"

    def put(self, key: str, value: float | None) -> None:
        _put(self, key, value, LINK_METRICS[key])


def _put(sample: Sample | LinkSample, key: str, value: float | None, maximum: float) -> None:
    if value is None:
        sample.metrics[key] = None
    elif not math.isfinite(value) or not -1e-9 <= value <= maximum:
        sample.invalid[key] = repr(value)
    else:
        sample.metrics[key] = max(0.0, round(value, 4))


@dataclass
class InfraSample:
    component: str
    ts: datetime
    status: Status
    metrics: dict[str, float] = field(default_factory=dict)
    reason: str = ""


@dataclass
class Collection:
    ts: datetime
    origin: Origin
    samples: list[Sample]
    infra: list[InfraSample] = field(default_factory=list)
    links: list[LinkSample] = field(default_factory=list)
    clock_skew_s: float = 0.0
