"""Failure propagation in the running twin: which failures are real, what they may cause, and later,
what they did cause.

* The **origins** are the confirmed failures that nothing else explains (the twin's root causes).
* Every other component gets the impact the engine estimates. If its own measurements show trouble
  too, the impact is **confirmed**; otherwise it is **predicted**.
* A new origin opens a **propagation event**. Sixty seconds later the event is closed with its
  outcome: which predicted components really showed trouble in their own measurements, which did
  not, and what was not predicted. The rule 'depends on a failed component' does not count as
  evidence: it is the twin repeating the dependency map, not a measurement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.digital_twin.model import Level, Status
from app.digital_twin.propagation import Analysis, DependencyGraph, analyse, report

if TYPE_CHECKING:
    from app.digital_twin.state import DigitalTwin

WINDOW = timedelta(seconds=60)
PREDICTED = ("severe", "high", "moderate")  # what counts as 'predicted to be affected'
KEEP = 200


@dataclass
class Event:
    id: str
    opened_at: datetime
    origins: tuple[str, ...]
    new: tuple[str, ...]  # origins that were not there before
    report: dict
    predicted: tuple[str, ...]
    observed: set[str] = field(default_factory=set)
    closed_at: datetime | None = None
    outcome: dict | None = None

    def view(self) -> dict:
        return {"id": self.id, "opened_at": self.opened_at, "origins": list(self.origins),
                "new_origins": list(self.new), "predicted": list(self.predicted),
                "closed_at": self.closed_at, "outcome": self.outcome, "report": self.report}


def measured_trouble(twin: DigitalTwin, cid: str) -> bool:
    """Its own measurements show trouble: not only the rule that a dependency has failed."""
    s = twin.components[cid]
    if s.status is Status.FAILED:
        return True
    return any(f.level >= Level.WARNING and f.rule != "dependency_failed" for f in s.findings)


class ImpactTracker:
    def __init__(self) -> None:
        self.graph: DependencyGraph | None = None
        self._checksum: str | None = None
        self.analysis: Analysis | None = None
        self.since: datetime | None = None
        self.open: list[Event] = []
        self.events: list[Event] = []  # most recent last

    def graph_of(self, twin: DigitalTwin) -> DependencyGraph:
        """Rebuilt whenever the structure of the twin changes."""
        if self.graph is None or self._checksum != twin.blueprint.checksum:
            self.graph = DependencyGraph.from_blueprint(twin.blueprint)
            self._checksum = twin.blueprint.checksum
        return self.graph

    def origins(self, twin: DigitalTwin) -> tuple[str, ...]:
        failed = [c for c, s in twin.components.items() if s.status is Status.FAILED]
        roots = {r for c in failed for r in twin.root_causes(c)}
        return tuple(sorted(roots))

    def update(self, twin: DigitalTwin, now: datetime) -> list[Event]:
        """After every change of the twin. Returns the events that were opened or closed."""
        touched: list[Event] = []
        graph = self.graph_of(twin)
        origins = self.origins(twin) if twin.synchronised else ()
        before = self.analysis.origins if self.analysis else ()
        if origins != before or (self.analysis and self.analysis.graph != graph.checksum):
            self.analysis = analyse(graph, origins) if origins else None
            if not origins:
                self.since = None
            elif not before:
                self.since = now
            new = tuple(o for o in origins if o not in before)
            if new:
                e = Event(str(uuid.uuid4()), now, origins, new, self.report(twin),
                          tuple(i.id for i in self.analysis.affected if i.level in PREDICTED))
                self.open.append(e)
                self.events = (self.events + [e])[-KEEP:]
                touched.append(e)
        for e in list(self.open):
            for cid in twin.components:
                if cid not in e.origins and measured_trouble(twin, cid):
                    e.observed.add(cid)
            if now - e.opened_at >= WINDOW or not twin.synchronised:
                self._close(e, now, twin.synchronised)
                touched.append(e)
        return touched

    def _close(self, e: Event, now: datetime, judged: bool) -> None:
        self.open.remove(e)
        e.closed_at = now
        predicted, observed = set(e.predicted), e.observed
        hits = sorted(predicted & observed)
        e.outcome = {
            "judged": judged,  # False: contact was lost, nothing can be said
            "observed": sorted(observed), "hits": hits,
            "missed": sorted(observed - predicted),
            "not_observed": sorted(predicted - observed),
            "precision": round(len(hits) / len(predicted), 3) if predicted else None,
            "recall": round(len(hits) / len(observed), 3) if observed else None,
        }

    def report(self, twin: DigitalTwin, analysis: Analysis | None = None) -> dict:
        a = analysis or self.analysis
        assert a is not None
        graph = self.graph_of(twin)
        calls = {k: s.metrics.get("calls_per_s") for k, s in twin.links.items()
                 if "calls_per_s" in s.metrics}
        return report(graph, a, {c: s.status.value for c, s in twin.components.items()}, calls)

    def role(self, twin: DigitalTwin, cid: str) -> str | None:
        """origin | confirmed | predicted, or None if not affected."""
        a = self.analysis
        if a is None or cid not in a.impacts:
            return None
        i = a.impacts[cid]
        if i.failed:
            return "origin"
        if i.level == "none":
            return None
        return "confirmed" if measured_trouble(twin, cid) else "predicted"

    def key(self, twin: DigitalTwin, cid: str) -> tuple | None:
        """What has to differ for the view of a component to change."""
        a = self.analysis
        if a is None or cid not in a.impacts:
            return None
        i = a.impacts[cid]
        return (i.impact, i.level, self.role(twin, cid), i.via[0].path if i.via else ())

    def link_key(self, lid: str) -> tuple | None:
        return self.analysis.links.get(lid) if self.analysis else None
