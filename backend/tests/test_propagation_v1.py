import pytest

from app.domain.contracts import PropagationRequest
from app.domain.topology import TopologyGraph
from app.explain.explainer import explain_propagation
from app.propagation.engine import analyze


def test_core_switch_failure_reaches_channels(topology):
    res = analyze(TopologyGraph(topology), PropagationRequest(failed_nodes=["core-switch"]))
    by_id = {n.node_id: n for n in res.impacted}
    assert by_id["core-switch"].impact == 1.0 and by_id["core-switch"].depth == 0
    assert "mobile-gateway" in by_id  # via firewall -> load balancer
    assert by_id["mobile-gateway"].path[0] == "core-switch"
    # impact never increases along a path
    for n in res.impacted:
        assert 0 <= n.impact <= 1
    assert res.business_impact > 0


def test_max_product_path_and_threshold(topology):
    g = TopologyGraph(topology)
    res = analyze(g, PropagationRequest(failed_nodes=["payments-db"], decay=1.0, threshold=0.5))
    by_id = {n.node_id: n for n in res.impacted}
    assert by_id["payments-service"].impact == 1.0
    assert by_id["partner-gateway"].impact == pytest.approx(0.8)
    assert all(n.impact >= 0.5 for n in res.impacted)
    assert "notification-service" not in by_id  # not dependent on payments-db


def test_unknown_node_raises(topology):
    with pytest.raises(KeyError):
        analyze(TopologyGraph(topology), PropagationRequest(failed_nodes=["nope"]))


def test_explanation_has_paths(topology):
    res = analyze(TopologyGraph(topology), PropagationRequest(failed_nodes=["ledger-db"]))
    exp = explain_propagation(res)
    assert exp.method == "propagation_path"
    assert exp.factors and exp.evidence[0]["path"][0] == "ledger-db"
