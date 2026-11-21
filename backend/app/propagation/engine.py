"""Dependency-aware failure propagation (reference implementation, Phase 1).

Algorithm (weighted reverse BFS / max-product path):
    impact(origin) = initial_impact
    impact(dependent) = max over failing dependencies d of
                        impact(d) * weight(dependent -> d) * decay
Nodes below ``threshold`` are pruned. Because weights and decay are <= 1 the
best-first expansion below yields the maximum-product path for every node
(Dijkstra on -log weights), so results are deterministic and path-explainable.

Phase 4 adds a Monte-Carlo variant with replica redundancy.
"""

from __future__ import annotations

import heapq

from app.domain.contracts import ImpactedNode, PropagationRequest, PropagationResult
from app.domain.topology import TopologyGraph


def analyze(graph: TopologyGraph, req: PropagationRequest) -> PropagationResult:
    unknown = [n for n in req.failed_nodes if n not in graph]
    if unknown:
        raise KeyError(f"Unknown node(s): {unknown}")

    best: dict[str, ImpactedNode] = {}
    heap: list[tuple[float, str, int, list[str]]] = []
    for origin in dict.fromkeys(req.failed_nodes):
        heapq.heappush(heap, (-req.initial_impact, origin, 0, [origin]))

    while heap:
        neg_impact, node_id, depth, path = heapq.heappop(heap)
        impact = -neg_impact
        if node_id in best:
            continue
        best[node_id] = ImpactedNode(
            node_id=node_id, impact=round(impact, 4), depth=depth, path=path,
            criticality=graph.node(node_id).criticality,
        )
        for dependent in graph.dependents(node_id):
            if dependent in best:
                continue
            nxt = impact * graph.edge(dependent, node_id).weight * req.decay
            if nxt >= req.threshold:
                heapq.heappush(heap, (-nxt, dependent, depth + 1, [*path, dependent]))

    impacted = sorted(best.values(), key=lambda n: (-n.impact, n.depth, n.node_id))
    business = round(sum(n.impact * n.criticality for n in impacted), 3)
    return PropagationResult(origin=list(dict.fromkeys(req.failed_nodes)),
                             impacted=impacted, business_impact=business)
