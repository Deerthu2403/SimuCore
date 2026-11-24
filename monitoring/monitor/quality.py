"""Finds missing metrics and monitoring failures, and turns them into events.

An event opens only after the problem has persisted for a few ticks and closes only after it has
been gone for a few, so that a single lost scrape neither raises nor clears an alarm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from monitor.catalog import LINK_METRICS, METRICS
from monitor.config import ServiceSpec
from monitor.models import Collection

#: kind -> (severity, ticks before it opens)
KINDS: dict[str, tuple[str, int]] = {
    "service_down": ("critical", 1),
    "service_unresponsive": ("high", 2),
    "metric_missing": ("medium", 4),  # rates need a few scrapes after a start
    "metric_invalid": ("medium", 2),
    "component_down": ("high", 2),
    "source_unavailable": ("critical", 2),
    "clock_skew": ("medium", 5),
    "store_failed": ("high", 2),
    "samples_dropped": ("high", 1),
    "collector_slow": ("medium", 3),
}
CLOSE_AFTER = 2
MAX_SKEW_S = 5.0

Key = tuple[str, str, str]


@dataclass(frozen=True)
class Issue:
    kind: str
    message: str
    service: str = ""
    metric: str = ""

    @property
    def key(self) -> Key:
        return (self.kind, self.service, self.metric)


@dataclass
class Event:
    issue: Issue
    opened_at: datetime
    last_seen: datetime
    closed_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> dict[str, Any]:
        return {"id": str(self.id), "kind": self.issue.kind,
                "severity": KINDS[self.issue.kind][0], "service": self.issue.service or None,
                "metric": self.issue.metric or None, "message": self.issue.message,
                "opened_at": self.opened_at.isoformat(), "last_seen": self.last_seen.isoformat(),
                "closed_at": self.closed_at.isoformat() if self.closed_at else None}


def inspect(collection: Collection, services: tuple[ServiceSpec, ...]) -> list[Issue]:
    """Problems visible in one collection."""
    issues = []
    by_name = {s.service: s for s in collection.samples}
    for spec in services:
        sample = by_name.get(spec.name)
        if sample is None or sample.status == "unresponsive":
            why = sample.reason if sample else "the source delivered nothing for it"
            issues.append(Issue("service_unresponsive", why, spec.name))
            continue
        if sample.status == "down":  # one event says it all; its metrics are naturally gone
            issues.append(Issue("service_down", sample.reason, spec.name))
            continue
        for m in METRICS:
            if m.key in sample.invalid:
                issues.append(Issue("metric_invalid",
                                    f"{m.title} is impossible: {sample.invalid[m.key]}",
                                    spec.name, m.key))
            elif m.applies_to(spec) and m.key not in sample.metrics:
                issues.append(Issue("metric_missing", f"{m.title} is not being delivered",
                                    spec.name, m.key))
    up = {s.service for s in collection.samples if s.status == "up"}
    links = {(k.source, k.target): k for k in collection.links}
    for spec in services:
        for target in spec.targets if spec.name in up else ():
            link, name = links.get((spec.name, target)), f"link:{target}"
            if link and link.invalid:
                issues.append(Issue("metric_invalid", f"connection to {target} has impossible "
                                    f"values: {link.invalid}", spec.name, name))
            elif link is None or not LINK_METRICS.keys() <= link.metrics.keys():
                issues.append(Issue("metric_missing", f"connection to {target} is not being "
                                    "measured", spec.name, name))
    for c in collection.infra:
        if c.status != "up":
            issues.append(Issue("component_down", c.reason, c.component))
    if abs(collection.clock_skew_s) > MAX_SKEW_S:
        issues.append(Issue("clock_skew", "the clocks of the monitor and its source differ by "
                            f"{collection.clock_skew_s:.0f} s"))
    return issues


class EventBook:
    """Open events, with hysteresis in both directions."""

    def __init__(self) -> None:
        self.open: dict[Key, Event] = {}
        self._seen: dict[Key, int] = {}
        self._gone: dict[Key, int] = {}

    def update(self, issues: list[Issue], now: datetime, scope: set[str]) -> list[Event]:
        """Report the current issues of the kinds in ``scope``. Returns events that changed.

        Kinds outside the scope were not examined this time, so they neither open nor close:
        when Prometheus is unreachable, nothing is known about any service.
        """
        changed: list[Event] = []
        current = {i.key: i for i in issues if i.kind in scope}
        for key, issue in current.items():
            self._seen[key] = self._seen.get(key, 0) + 1
            self._gone.pop(key, None)
            if key in self.open:
                self.open[key].last_seen = now
            elif self._seen[key] >= KINDS[issue.kind][1]:
                self.open[key] = Event(issue, now, now)
                changed.append(self.open[key])
        for key in [k for k in self._seen if k[0] in scope and k not in current]:
            if key not in self.open:
                del self._seen[key]
                continue
            self._gone[key] = self._gone.get(key, 0) + 1
            if self._gone[key] >= CLOSE_AFTER:
                event = self.open.pop(key)
                event.closed_at = now
                changed.append(event)
                del self._seen[key], self._gone[key]
        return changed
