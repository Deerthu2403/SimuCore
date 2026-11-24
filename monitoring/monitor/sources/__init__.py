"""Where monitoring data comes from."""

from __future__ import annotations

from typing import Protocol

from monitor.config import ServiceSpec, Settings
from monitor.models import Collection
from monitor.sources.direct import DirectSource
from monitor.sources.prometheus import PrometheusSource
from monitor.sources.synthetic import SyntheticSource


class Source(Protocol):
    name: str

    async def collect(self, with_infra: bool) -> Collection: ...

    async def aclose(self) -> None: ...


def make_source(settings: Settings, services: tuple[ServiceSpec, ...]) -> Source:
    kinds = {"prometheus": PrometheusSource, "direct": DirectSource, "synthetic": SyntheticSource}
    return kinds[settings.source](settings, services)
