"""Telemetry adapters registry."""

from app.adapters.bank_services import BankServicesAdapter
from app.adapters.base import ReadOnlyHttpClient, ReadOnlyViolation, TelemetryAdapter
from app.adapters.monitoring import MonitoringAdapter
from app.adapters.prometheus import PrometheusAdapter
from app.adapters.simulated import SimulatedAdapter

ADAPTERS: dict[str, type[TelemetryAdapter]] = {
    SimulatedAdapter.name: SimulatedAdapter,
    PrometheusAdapter.name: PrometheusAdapter,
    BankServicesAdapter.name: BankServicesAdapter,
    MonitoringAdapter.name: MonitoringAdapter,
}

__all__ = [
    "ADAPTERS",
    "BankServicesAdapter",
    "MonitoringAdapter",
    "PrometheusAdapter",
    "ReadOnlyHttpClient",
    "ReadOnlyViolation",
    "SimulatedAdapter",
    "TelemetryAdapter",
]
