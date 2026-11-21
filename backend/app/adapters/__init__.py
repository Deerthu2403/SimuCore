"""Telemetry adapters registry."""

from app.adapters.base import ReadOnlyHttpClient, ReadOnlyViolation, TelemetryAdapter
from app.adapters.prometheus import PrometheusAdapter
from app.adapters.simulated import SimulatedAdapter

ADAPTERS: dict[str, type[TelemetryAdapter]] = {
    SimulatedAdapter.name: SimulatedAdapter,
    PrometheusAdapter.name: PrometheusAdapter,
}

__all__ = [
    "ADAPTERS",
    "PrometheusAdapter",
    "ReadOnlyHttpClient",
    "ReadOnlyViolation",
    "SimulatedAdapter",
    "TelemetryAdapter",
]
