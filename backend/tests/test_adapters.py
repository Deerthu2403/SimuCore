import pytest

from app.adapters import ReadOnlyHttpClient, ReadOnlyViolation, SimulatedAdapter, TelemetryAdapter


async def test_simulated_adapter_contract(infra):
    adapter = SimulatedAdapter(infra)
    await adapter.connect()
    topo = await adapter.fetch_topology()
    samples = await adapter.poll()
    assert len(samples) == len(topo.nodes)
    await adapter.close()


def test_adapters_cannot_define_write_methods():
    with pytest.raises(TypeError):
        class Bad(TelemetryAdapter):  # noqa: F811
            name = "bad"

            async def write(self): ...


def test_adapters_cannot_opt_out_of_read_only():
    with pytest.raises(TypeError):
        class Bad(TelemetryAdapter):
            name = "bad"
            read_only = False


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
async def test_http_client_blocks_mutating_verbs(verb):
    client = ReadOnlyHttpClient("http://example.invalid")
    with pytest.raises(ReadOnlyViolation):
        await client.request(verb, "/api")
    await client.aclose()
