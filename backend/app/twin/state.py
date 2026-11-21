"""Authoritative in-memory state of the digital twin (ADR-004)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.models import HealthStatus, MetricSample, NodeState, Topology
from app.domain.topology import TopologyGraph

H = HealthStatus

# metric -> (degraded threshold, critical threshold); see docs/03-twin-synchronization.md
THRESHOLDS: dict[str, tuple[float, float]] = {
    "cpu_pct": (75.0, 90.0),
    "mem_pct": (80.0, 92.0),
    "error_rate": (0.02, 0.08),
}
LATENCY_FACTORS = (1.8, 3.0)


def classify(metrics: dict[str, float], base_latency_ms: float | None) -> HealthStatus:
    if metrics.get("availability", 1.0) <= 0.0:
        return H.DOWN
    status = H.HEALTHY
    for key, (deg, crit) in THRESHOLDS.items():
        v = metrics.get(key)
        if v is None:
            continue
        if v >= crit:
            return H.CRITICAL
        if v >= deg:
            status = H.DEGRADED
    lat = metrics.get("latency_ms")
    if lat is not None and base_latency_ms:
        if lat >= LATENCY_FACTORS[1] * base_latency_ms:
            return H.CRITICAL
        if lat >= LATENCY_FACTORS[0] * base_latency_ms:
            status = H.DEGRADED
    return status


class TwinState:
    def __init__(self) -> None:
        self.version = 0
        self.topology: Topology | None = None
        self.graph: TopologyGraph | None = None
        self.states: dict[str, NodeState] = {}

    def load_topology(self, topology: Topology) -> None:
        self.graph = TopologyGraph(topology)
        self.topology = topology
        previous = self.states
        self.states = {n.id: previous.get(n.id, NodeState(node_id=n.id)) for n in topology.nodes}
        self.version += 1

    def apply(self, samples: list[MetricSample]) -> list[NodeState]:
        """Apply samples, return states that changed. Unknown node ids are ignored."""
        if self.graph is None:
            raise RuntimeError("Topology not loaded")
        changed: list[NodeState] = []
        for s in samples:
            if s.node_id not in self.states:
                continue
            base = self.graph.node(s.node_id).attributes.get("base_latency_ms")
            new = NodeState(
                node_id=s.node_id,
                status=classify(s.metrics, float(base) if base else None),
                metrics=s.metrics,
                updated_at=s.ts,
            )
            self.states[s.node_id] = new
            changed.append(new)
        if changed:
            self.version += 1
        return changed

    def mark_stale(self, max_age: timedelta, now: datetime | None = None) -> list[NodeState]:
        now = now or datetime.now(UTC)
        stale: list[NodeState] = []
        for nid, st in self.states.items():
            if st.status != H.UNKNOWN and (st.updated_at is None or now - st.updated_at > max_age):
                st = st.model_copy(update={"status": H.UNKNOWN})
                self.states[nid] = st
                stale.append(st)
        if stale:
            self.version += 1
        return stale

    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in H}
        for st in self.states.values():
            counts[st.status.value] += 1
        return counts
