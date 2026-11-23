import asyncio
from datetime import timedelta

from app.adapters import SimulatedAdapter
from app.core.events import EventBus
from app.domain.contracts import PropagationRequest
from app.domain.models import HealthStatus
from app.domain.topology import TopologyGraph
from app.propagation.engine import analyze
from app.simulation.bank_services import load_bank_services_topology
from app.simulation.infrastructure import SimulatedBankInfrastructure
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer


def impacts(*failed: str) -> dict[str, float]:
    graph = TopologyGraph(load_bank_services_topology())
    result = analyze(graph, PropagationRequest(failed_nodes=[f"bank-{f}" for f in failed]))
    return {n.node_id.removeprefix("bank-"): n.impact for n in result.impacted if n.depth}


def test_bank_map_is_a_valid_twin_topology():
    topo = load_bank_services_topology()
    assert len(topo.nodes) == 8 and not TopologyGraph(topo).has_cycles()


def test_predicted_failures_match_the_live_scenarios():
    """Who fails, not who slows down: see doc 08 section 8 for what the model misses."""
    assert set(impacts("account")) == {"transaction"}  # S06
    assert impacts("fraud")["transaction"] < 0.5  # S07: degradable, not fatal
    assert max(impacts("notification").values()) < 0.1  # S09: best effort
    assert min(impacts("authentication")[s] for s in ("customer", "account", "transaction")) > 0.6
    assert len(impacts("database")) == 7  # everything, directly or indirectly


async def test_silent_adapter_makes_nodes_stale(topology):
    """Phase 1 bug: staleness was only evaluated after a successful poll."""
    class Dying(SimulatedAdapter):
        name = "dying"
        dead = False

        async def poll(self):
            if self.dead:
                raise ConnectionError("source is gone")
            return await super().poll()

    adapter = Dying(SimulatedBankInfrastructure(topology, incident_rate=0))
    state = TwinState()
    sync = TwinSynchronizer(adapter, state, EventBus(), interval_s=0.02)
    sync.stale_after = timedelta(seconds=0)
    await adapter.connect()
    state.load_topology(await adapter.fetch_topology())
    adapter.dead = True
    for s in state.states.values():
        s.status = HealthStatus.HEALTHY
    sync.running = True

    task = asyncio.create_task(sync._loop())
    await asyncio.sleep(0.1)
    sync.running = False
    await task
    assert {s.status for s in state.states.values()} == {HealthStatus.UNKNOWN}
    assert "source is gone" in sync.last_error
