from app.domain.models import STANDARD_METRICS, HealthStatus
from app.twin.state import TwinState, classify


def test_infrastructure_emits_all_standard_metrics(infra, topology):
    samples = infra.step()
    assert {s.node_id for s in samples} == {n.id for n in topology.nodes}
    for s in samples:
        assert set(STANDARD_METRICS) <= set(s.metrics)


def test_seeded_simulation_is_deterministic(topology):
    from app.simulation.infrastructure import SimulatedBankInfrastructure

    a = SimulatedBankInfrastructure(topology, seed=3).step()
    b = SimulatedBankInfrastructure(topology, seed=3).step()
    assert [s.metrics for s in a] == [s.metrics for s in b]


def test_crash_is_detected_and_contagion_reaches_dependents(infra, topology):
    state = TwinState()
    state.load_topology(topology)
    state.apply(infra.step())
    assert state.states["payments-service"].status == HealthStatus.HEALTHY

    infra.inject_fault("payments-db", "crash", duration_ticks=5)
    state.apply(infra.step())
    assert state.states["payments-db"].status == HealthStatus.DOWN
    # payments-service depends on payments-db with weight 1.0 -> errors spike
    assert state.states["payments-service"].status in (HealthStatus.CRITICAL, HealthStatus.DOWN)


def test_classify_thresholds():
    assert classify({"cpu_pct": 50, "availability": 1}, 20) == HealthStatus.HEALTHY
    assert classify({"cpu_pct": 80}, 20) == HealthStatus.DEGRADED
    assert classify({"error_rate": 0.1}, 20) == HealthStatus.CRITICAL
    assert classify({"latency_ms": 70}, 20) == HealthStatus.CRITICAL
    assert classify({"availability": 0}, 20) == HealthStatus.DOWN
