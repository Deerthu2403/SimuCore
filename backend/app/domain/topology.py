"""Graph view over a Topology for dependency queries."""

from __future__ import annotations

import networkx as nx

from app.domain.models import Edge, Node, Topology


class TopologyGraph:
    """Directed graph where an edge u -> v means *u depends on v*."""

    def __init__(self, topology: Topology) -> None:
        self.topology = topology
        self.g = nx.DiGraph()
        for n in topology.nodes:
            self.g.add_node(n.id, node=n)
        for e in topology.edges:
            if e.source not in self.g or e.target not in self.g:
                raise ValueError(f"Edge references unknown node: {e.source} -> {e.target}")
            # Multiple edge kinds between the same pair collapse to the strongest.
            if self.g.has_edge(e.source, e.target):
                if self.g[e.source][e.target]["edge"].weight >= e.weight:
                    continue
            self.g.add_edge(e.source, e.target, edge=e)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.g

    def node(self, node_id: str) -> Node:
        return self.g.nodes[node_id]["node"]

    def dependencies(self, node_id: str) -> list[str]:
        """Nodes that ``node_id`` depends on (1 hop)."""
        return sorted(self.g.successors(node_id))

    def dependents(self, node_id: str) -> list[str]:
        """Nodes that depend on ``node_id`` (1 hop) - i.e. who breaks if it breaks."""
        return sorted(self.g.predecessors(node_id))

    def edge(self, source: str, target: str) -> Edge:
        return self.g[source][target]["edge"]

    def has_cycles(self) -> bool:
        return not nx.is_directed_acyclic_graph(self.g)
