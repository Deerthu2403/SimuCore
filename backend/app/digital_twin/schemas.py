"""The public format of the digital twin (API version 2), made for the interactive 3D frontend.

Conventions: every object has a stable ``id``; times are ISO 8601 in UTC; a metric that has no value
is absent, never 0; numbers carry the unit given in the catalogue; ``display`` holds hints that save
the frontend from knowing thresholds, and no colours or coordinates.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.digital_twin.model import (
    ComponentType,
    LinkKind,
    ObjectState,
    Status,
    Transition,
)
from app.digital_twin.state import DigitalTwin

API_VERSION = "2.0"
LAYERS = ["edge", "application", "data", "network", "compute", "platform"]
SEVERITY = [Status.HEALTHY, Status.UNKNOWN, Status.RECOVERING, Status.WARNING, Status.OVERLOADED,
            Status.FAILED]


class FindingView(BaseModel):
    level: Literal["warning", "overload", "failure"]
    rule: str
    message: str
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None


class DependencyRef(BaseModel):
    id: str = Field(description="The other component")
    link_id: str
    kind: LinkKind
    mode: str = Field(description="required, degradable, best_effort or structural")
    status: Status = Field(description="Status of the other component")
    link_status: Status


class Dependencies(BaseModel):
    depends_on: list[DependencyRef]
    dependents: list[DependencyRef]


class ComponentDisplay(BaseModel):
    layer: str = Field(description="Suggested horizontal plane, see catalogue.layers")
    group: str = Field(description="Components that belong together: bank or platform")
    order: int = Field(description="Stable position within the layer")
    size: int = Field(ge=1, le=5, description="Importance")
    load: float | None = Field(default=None, ge=0, le=1, description="How busy; null if unknown")
    attention: bool = Field(description="Should draw the eye")


class LinkDisplay(BaseModel):
    flow: float = Field(ge=0, le=1, description="Traffic on a logarithmic scale; 0 = none")
    attention: bool


class ComponentView(BaseModel):
    id: str
    type: ComponentType
    name: str
    description: str
    criticality: int = Field(ge=1, le=5)
    status: Status
    status_since: datetime | None
    previous_status: Status | None
    reason: str = Field(description="Why it is in this status; empty when healthy")
    findings: list[FindingView]
    root_causes: list[str] = Field(description="Failed components that explain the trouble")
    metrics: dict[str, float]
    data_quality: Literal["complete", "partial", "derived", "none"]
    attributes: dict[str, Any]
    dependencies: Dependencies
    display: ComponentDisplay


class LinkView(BaseModel):
    id: str
    source: str = Field(description="The component that depends")
    target: str = Field(description="The component depended on")
    kind: LinkKind
    mode: str
    weight: float
    purpose: str
    status: Status
    status_since: datetime | None
    reason: str
    active: bool = Field(description="Traffic was seen in the last seconds")
    metrics: dict[str, float]
    data_quality: Literal["complete", "partial", "derived", "none"]
    display: LinkDisplay


class Summary(BaseModel):
    overall: Status = Field(description="Worst status among the bank's components")
    components: int
    by_status: dict[Status, int]
    by_type: dict[ComponentType, int]
    links: int
    links_failed: int
    needs_attention: list[str] = Field(description="Ids, most serious first")


class TwinMeta(BaseModel):
    api_version: str = API_VERSION
    twin_id: str = "simucore-bank"
    run_id: str = Field(description="Changes when the twin process restarts")
    version: int = Field(description="Grows with every change")
    structure_version: int = Field(description="Grows when components or links are added/removed")
    sync: Literal["live", "stale", "waiting"]
    origin: Literal["measured", "synthetic"] | None
    last_observation_at: datetime | None = Field(description="When the values shown were measured")
    generated_at: datetime


class TwinView(BaseModel):
    meta: TwinMeta
    summary: Summary
    components: list[ComponentView]
    links: list[LinkView]


class TransitionView(BaseModel):
    ts: datetime
    version: int
    object_kind: Literal["component", "link"]
    object_id: str
    from_status: Status | None
    to_status: Status
    reason: str
    root_causes: list[str]
    origin: str | None


class TwinMessage(BaseModel):
    """Every WebSocket message. ``snapshot`` is complete; ``delta`` holds whole changed objects, to
    be applied on top of ``base_version``; ``heartbeat`` says that nothing changed."""

    type: Literal["snapshot", "delta", "heartbeat"]
    base_version: int | None = None
    meta: TwinMeta
    summary: Summary | None = None
    components: list[ComponentView] = []
    links: list[LinkView] = []
    transitions: list[TransitionView] = []


# ----------------------------------------------------------------------- building views
def component_view(twin: DigitalTwin, cid: str) -> ComponentView:
    spec, s = twin.blueprint.components[cid], twin.components[cid]
    same_layer = sorted(c for c, x in twin.blueprint.components.items() if x.layer == spec.layer)

    def ref(link_id: str, other: str) -> DependencyRef:
        k = twin.blueprint.links[link_id]
        return DependencyRef(id=other, link_id=link_id, kind=k.kind, mode=k.mode,
                             status=twin.components[other].status,
                             link_status=twin.links[link_id].status)

    return ComponentView(
        id=cid, type=spec.type, name=spec.name, description=spec.description,
        criticality=spec.criticality, status=s.status, status_since=s.since,
        previous_status=s.previous, reason=s.reason, findings=_findings(s),
        root_causes=twin.root_causes(cid), metrics=dict(s.metrics), data_quality=s.quality,
        attributes=dict(spec.attributes),
        dependencies=Dependencies(
            depends_on=[ref(k.id, k.target) for k in twin.blueprint.outgoing[cid]],
            dependents=[ref(k.id, k.source) for k in twin.blueprint.incoming[cid]]),
        display=ComponentDisplay(
            layer=spec.layer, group=spec.group, order=same_layer.index(cid), size=spec.criticality,
            load=_load(spec.attributes, s),
            attention=s.status in (Status.FAILED, Status.OVERLOADED, Status.RECOVERING)))


def link_view(twin: DigitalTwin, lid: str) -> LinkView:
    k, s = twin.blueprint.links[lid], twin.links[lid]
    calls = s.metrics.get("calls_per_s", 0.0)
    return LinkView(
        id=lid, source=k.source, target=k.target, kind=k.kind, mode=k.mode, weight=k.weight,
        purpose=k.purpose, status=s.status, status_since=s.since, reason=s.reason,
        active=calls > 0, metrics=dict(s.metrics), data_quality=s.quality,
        display=LinkDisplay(flow=round(min(1.0, math.log10(1 + calls) / 2), 3),
                            attention=s.status is Status.FAILED))


def summary(twin: DigitalTwin) -> Summary:
    specs = twin.blueprint.components
    by_status = dict.fromkeys(Status, 0)
    by_type = dict.fromkeys(ComponentType, 0)
    for cid, s in twin.components.items():
        by_status[s.status] += 1
        by_type[specs[cid].type] += 1
    bank = [s.status for c, s in twin.components.items() if specs[c].group == "bank"]
    trouble = [c for c, s in twin.components.items()
               if s.status not in (Status.HEALTHY, Status.UNKNOWN)]
    trouble.sort(key=lambda c: (-SEVERITY.index(twin.components[c].status), -specs[c].criticality,
                                c))
    return Summary(
        overall=max(bank, key=SEVERITY.index), components=len(specs), by_status=by_status,
        by_type=by_type, links=len(twin.links),
        links_failed=sum(s.status is Status.FAILED for s in twin.links.values()),
        needs_attention=trouble)


def meta(twin: DigitalTwin, now: datetime) -> TwinMeta:
    sync = "live" if twin.synchronised else "waiting" if twin.last_applied_at is None else "stale"
    return TwinMeta(run_id=str(twin.run_id), version=twin.version,
                    structure_version=twin.structure_version, sync=sync, origin=twin.origin,
                    last_observation_at=twin.last_observed_at, generated_at=now)


def twin_view(twin: DigitalTwin, now: datetime) -> TwinView:
    return TwinView(meta=meta(twin, now), summary=summary(twin),
                    components=[component_view(twin, c) for c in twin.components],
                    links=[link_view(twin, k) for k in twin.links])


def transition_view(t: Transition) -> TransitionView:
    return TransitionView(ts=t.ts, version=t.version, object_kind=t.object_kind,
                          object_id=t.object_id, from_status=t.from_status,
                          to_status=t.to_status, reason=t.reason,
                          root_causes=list(t.root_causes), origin=t.origin)


def _findings(s: ObjectState) -> list[FindingView]:
    return [FindingView(level=f.level.name.lower(), rule=f.rule, message=f.message,
                        metric=f.metric, value=f.value, threshold=f.threshold)
            for f in s.findings]


def _load(attributes: dict[str, Any], s: ObjectState) -> float | None:
    """The busiest of its resources, 0 to 1."""
    m, shares = s.metrics, []
    if "cpu_pct" in m:
        shares.append(m["cpu_pct"] / 100)
    if "in_flight" in m and attributes.get("max_concurrency"):
        shares.append(m["in_flight"] / attributes["max_concurrency"])
    if m.get("db_pool_size"):
        shares.append(m.get("db_pool_in_use", 0.0) / m["db_pool_size"])
    return round(min(1.0, max(shares)), 3) if shares else None


METRIC_UNITS = {
    "cpu_pct": "%", "mem_pct": "%", "mem_bytes": "B", "latency_ms": "ms", "latency_mean_ms": "ms",
    "rps": "req/s", "error_rate": "ratio", "net_rx_bps": "B/s", "net_tx_bps": "B/s",
    "db_response_ms": "ms", "active_connections": "", "queue_length": "", "in_flight": "",
    "availability": "ratio", "db_pool_in_use": "", "db_pool_size": "", "db_failure_rate": "ratio",
    "calls_per_s": "calls/s", "failure_rate": "ratio", "circuit": "0 closed, 1 half open, 2 open",
    "connections": "", "connections_failed": "", "bytes_per_s": "B/s", "load1": "",
    "disk_read_bps": "B/s", "disk_write_bps": "B/s", "database_bytes": "B",
    "commits_per_s": "commits/s", "cache_hit_ratio": "ratio", "targets_up": "",
    "targets_total": "", "slowest_scrape_s": "s", "series": "", "hosted_services": "",
    "hosted_running": "", "ingress_rps": "req/s", "routes": "", "routes_failed": "",
    "hosted_cpu_pct": "%", "hosted_mem_bytes": "B",
}
STATUS_MEANING = {
    Status.HEALTHY: "Works normally.",
    Status.WARNING: "Works, but something needs attention: slower, some errors, or it depends on "
                    "something that has failed.",
    Status.OVERLOADED: "Short of its own resources: CPU, memory, worker slots or connections.",
    Status.FAILED: "Not reachable, or at least half of its requests fail.",
    Status.RECOVERING: "Works again after a failure; becomes healthy once it has been stable.",
    Status.UNKNOWN: "Nothing trustworthy is known. Not a guess in either direction.",
}


def catalogue() -> dict[str, Any]:
    return {"api_version": API_VERSION, "layers": LAYERS,
            "statuses": [{"status": s, "severity": SEVERITY.index(s),
                          "meaning": STATUS_MEANING[s]} for s in SEVERITY],
            "component_types": list(ComponentType), "link_kinds": list(LinkKind),
            "metric_units": METRIC_UNITS}
