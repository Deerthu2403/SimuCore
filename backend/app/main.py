"""SimuCore FastAPI application entrypoint.

Run:  uvicorn app.main:app --reload   (from the backend/ directory)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from app import __version__
from app.adapters import BankServicesAdapter, SimulatedAdapter, TelemetryAdapter
from app.api.v1 import api_router
from app.api.v1.ws import router as ws_router
from app.core.config import Settings, get_settings
from app.core.events import EventBus
from app.core.logging import configure_logging
from app.simulation.bank_topology import build_reference_topology
from app.simulation.infrastructure import SimulatedBankInfrastructure
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer


def build_adapter(
    settings: Settings,
) -> tuple[TelemetryAdapter, SimulatedBankInfrastructure | None]:
    if settings.telemetry_adapter == "simulated":
        infra = SimulatedBankInfrastructure(
            build_reference_topology(), seed=settings.random_seed,
            incident_rate=settings.incident_rate, tick_s=settings.telemetry_interval_s)
        return SimulatedAdapter(infra), infra
    if settings.telemetry_adapter == "bank_services":
        return BankServicesAdapter(settings.bank_url_template), None
    raise ValueError(f"Adapter {settings.telemetry_adapter!r} is not available")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus = EventBus()
        state = TwinState()
        adapter, infra = build_adapter(settings)
        sync = TwinSynchronizer(adapter, state, bus, settings.telemetry_interval_s,
                                settings.stale_after_intervals)
        app.state.bus, app.state.twin_state, app.state.sync = bus, state, sync
        app.state.simulated_infra = infra
        await sync.start()
        try:
            yield
        finally:
            await sync.stop()

    app = FastAPI(title="SimuCore API", version=__version__, lifespan=lifespan,
                  description="Self-calibrating 3D digital twin for banking infrastructure")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(ws_router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
