"""Synchronises the twin with its telemetry source and broadcasts deltas."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from prometheus_client import Gauge

from app.adapters.base import TelemetryAdapter
from app.core.events import EventBus, Topics
from app.domain.contracts import TwinDelta
from app.domain.models import STANDARD_METRICS, HealthStatus, NodeState
from app.twin.state import TwinState

log = logging.getLogger(__name__)

# Twin metrics re-exported for Prometheus/Grafana (self-observability + dashboards).
NODE_METRIC = Gauge("simucore_node_metric", "Latest twin metric value", ["node", "metric"])
NODE_STATUS = Gauge("simucore_node_status", "Twin node health severity (-1 unknown .. 3 down)",
                    ["node"])
TWIN_VERSION = Gauge("simucore_twin_version", "Current twin state version")


class TwinSynchronizer:
    def __init__(self, adapter: TelemetryAdapter, state: TwinState, bus: EventBus,
                 interval_s: float = 1.0, stale_after_intervals: int = 3) -> None:
        self.adapter = adapter
        self.state = state
        self.bus = bus
        self.interval_s = interval_s
        self.stale_after = timedelta(seconds=interval_s * stale_after_intervals)
        self.running = False
        self.ticks = 0
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.adapter.connect()
        topology = await self.adapter.fetch_topology()
        self.state.load_topology(topology)
        await self.bus.publish(Topics.TWIN_TOPOLOGY, topology)
        await self.tick()  # populate state immediately
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="twin-sync")
        log.info("Twin sync started with adapter=%s, %d nodes", self.adapter.name,
                 len(topology.nodes))

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.adapter.close()

    async def tick(self) -> list[NodeState]:
        samples = await self.adapter.poll()
        changed = self.state.apply(samples)
        changed += self.state.mark_stale(self.stale_after)
        self.ticks += 1
        if changed:
            self._export(changed)
            await self.bus.publish(Topics.TWIN_DELTA,
                                   TwinDelta(version=self.state.version, states=changed))
        return changed

    async def _loop(self) -> None:
        while self.running:
            await asyncio.sleep(self.interval_s)
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:  # keep the twin alive on adapter hiccups
                self.last_error = repr(exc)
                log.exception("Twin sync tick failed")

    def _export(self, states: list[NodeState]) -> None:
        TWIN_VERSION.set(self.state.version)
        for st in states:
            NODE_STATUS.labels(st.node_id).set(HealthStatus(st.status).severity)
            for m in STANDARD_METRICS:
                if m in st.metrics:
                    NODE_METRIC.labels(st.node_id, m).set(st.metrics[m])
