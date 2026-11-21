import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.simulation.bank_topology import build_reference_topology
from app.simulation.infrastructure import SimulatedBankInfrastructure


@pytest.fixture
def topology():
    return build_reference_topology()


@pytest.fixture
def infra(topology):
    return SimulatedBankInfrastructure(topology, seed=7, incident_rate=0.0)


@pytest.fixture
def client():
    settings = Settings(telemetry_interval_s=0.05, incident_rate=0.0, random_seed=1)
    with TestClient(create_app(settings)) as c:
        yield c
