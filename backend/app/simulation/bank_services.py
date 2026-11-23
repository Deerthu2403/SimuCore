"""Loads the simulated microservices' dependency map as a twin topology.

The map is the one the running services enforce (banking-sim/dependency_map.json), so the twin
and the bank can never disagree about structure.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.models import Edge, EdgeKind, Node, NodeKind, NodeLayer, Topology

DEFAULT_MAP = Path(__file__).resolve().parents[3] / "banking-sim" / "dependency_map.json"


def load_bank_services_topology(path: Path = DEFAULT_MAP) -> Topology:
    raw = json.loads(path.read_text(encoding="utf-8"))
    services = raw["services"]
    nodes = [
        Node(
            id=s["node_id"],
            name=s["title"],
            kind=NodeKind.DATABASE if name == "database" else NodeKind.SERVICE,
            layer=NodeLayer.DATA if name == "database" else NodeLayer.APPLICATION,
            criticality=s["criticality"],
            capacity={"max_concurrency": s["max_concurrency"]},
            attributes={"service": name, "port": s["port"]},
        )
        for name, s in services.items()
    ]
    edges = [
        Edge(
            source=services[d["source"]]["node_id"],
            target=services[d["target"]]["node_id"],
            kind=EdgeKind.READS_WRITES if d["target"] == "database" else EdgeKind.CALLS,
            weight=d["weight"],
        )
        for d in raw["dependencies"]
    ]
    return Topology(nodes=nodes, edges=edges)
