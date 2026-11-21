from app.domain.models import NodeLayer
from app.domain.topology import TopologyGraph


def test_reference_topology_is_consistent(topology):
    ids = [n.id for n in topology.nodes]
    assert len(ids) == len(set(ids)) == 27
    graph = TopologyGraph(topology)  # raises on dangling edges
    assert not graph.has_cycles()
    assert {n.layer for n in topology.nodes} == set(NodeLayer)


def test_dependency_direction(topology):
    g = TopologyGraph(topology)
    assert "payments-db" in g.dependencies("payments-service")
    assert "payments-service" in g.dependents("payments-db")
