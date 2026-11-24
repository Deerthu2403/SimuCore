"""The continuous loop: collect, check, store, publish."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from monitor.config import ServiceSpec, Settings
from monitor.hub import Hub
from monitor.models import Collection, InfraSample, Sample, SourceUnavailable, utcnow
from monitor.quality import KINDS, Event, EventBook, Issue, inspect
from monitor.sources import Source
from monitor.store import UNAVAILABLE, Store, StoreUnavailable

log = logging.getLogger("monitor.collector")
PIPELINE_KINDS = {"source_unavailable", "store_failed", "samples_dropped", "collector_slow"}
SERVICE_KINDS = {"service_down", "service_unresponsive", "metric_missing", "metric_invalid",
                 "clock_skew"}
MAINTAIN_EVERY_S = 3600
#: Metrics the digital twin understands, and the limit it accepts for each.
TWIN_METRICS = {"cpu_pct": 100.0, "mem_pct": 100.0, "latency_ms": 1e9, "error_rate": 1.0,
                "rps": 1e9, "availability": 1.0, "net_rx_bps": 1e12, "net_tx_bps": 1e12,
                "db_response_ms": 1e9, "active_connections": 1e9, "queue_length": 1e9}


class Collector:
    def __init__(self, settings: Settings, services: tuple[ServiceSpec, ...], source: Source,
                 store: Store | None, hub: Hub) -> None:
        self.settings, self.services, self.source = settings, services, source
        self.store, self.hub = store, hub
        self.book = EventBook()
        self.started_at = utcnow()
        self.sequence = 0
        self.ticks = self.failed_ticks = 0
        self.last_error: str | None = None
        self.last_tick_mono = time.monotonic()
        self.tick_ms = 0.0
        self.current: Collection | None = None
        self.infra: dict[str, InfraSample] = {}
        self._dropped_reported = 0
        self._last_maintenance = time.monotonic()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------- running
    async def start(self) -> None:
        if self.store:
            self._tasks.append(asyncio.create_task(self.store.writer(), name="writer"))
        self._tasks.append(asyncio.create_task(self._loop(), name="collector"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.store:
            await self.store.aclose()
        await self.source.aclose()

    async def _loop(self) -> None:
        next_at = time.monotonic()
        while True:
            try:
                await self.tick()
            except Exception:  # a bug in one tick must not end monitoring
                self.failed_ticks += 1
                log.exception("tick failed")
            next_at += self.settings.tick_s
            now = time.monotonic()
            if next_at < now:  # overran: do not try to catch up with a burst
                next_at = now
            await asyncio.sleep(next_at - now)

    async def tick(self) -> None:
        started = time.monotonic()
        with_infra = self.ticks % self.settings.infra_every == 0
        self.ticks += 1
        issues: list[Issue] = []
        scope = set(PIPELINE_KINDS)
        collection: Collection | None = None
        try:
            collection = await self.source.collect(with_infra)
            self.last_error = None
        except SourceUnavailable as exc:
            self.failed_ticks += 1
            self.last_error = str(exc)
            issues.append(Issue("source_unavailable", str(exc)))
        if collection:
            scope |= SERVICE_KINDS
            if with_infra:
                scope.add("component_down")
            issues += inspect(collection, self.services)
            self.current = collection
            self.infra.update({i.component: i for i in collection.infra})
            if self.store:
                self.store.add(collection)
        issues += self._pipeline_issues(time.monotonic() - started)
        events = self.book.update(issues, utcnow(), scope)
        self._report(events)
        self.sequence += 1
        self.hub.publish(self.message("update", events))
        self.tick_ms = (time.monotonic() - started) * 1000
        self.last_tick_mono = time.monotonic()
        await self._maintain()

    def _pipeline_issues(self, seconds: float) -> list[Issue]:
        issues = []
        if seconds > self.settings.tick_s:
            issues.append(Issue("collector_slow", f"collecting took {seconds:.1f} s, longer than "
                                f"the {self.settings.tick_s:g} s between updates"))
        if self.store:
            if self.store.error or self.store.pool is None:
                why = self.store.error or "not connected to the database yet"
                issues.append(Issue("store_failed", f"history is not being saved: {why}"))
            if self.store.dropped > self._dropped_reported:
                issues.append(Issue("samples_dropped", f"{self.store.dropped} rows were lost "
                                    "because the database was away too long"))
                self._dropped_reported = self.store.dropped
        return issues

    def _report(self, events: list[Event]) -> None:
        for e in events:
            verb = "closed" if e.closed_at else "opened"
            log.warning("event %s: %s %s %s: %s", verb, e.issue.kind, e.issue.service,
                        e.issue.metric, e.issue.message)
            if self.store:
                self.store.add_event(e)

    async def _maintain(self) -> None:
        if self.store and time.monotonic() - self._last_maintenance > MAINTAIN_EVERY_S:
            self._last_maintenance = time.monotonic()
            try:
                await self.store.maintain()
            except (*UNAVAILABLE, StoreUnavailable) as exc:
                log.warning("partition maintenance postponed: %s", exc)

    # --------------------------------------------------------------------- views
    @property
    def age_s(self) -> float | None:
        return (utcnow() - self.current.ts).total_seconds() if self.current else None

    @property
    def fresh(self) -> bool:
        return self.age_s is not None and self.age_s <= 3 * self.settings.tick_s + 1

    def service_view(self, s: Sample) -> dict[str, Any]:
        return {"service": s.service, "node_id": s.node_id, "status": s.status,
                "reason": s.reason or None, "ts": s.ts.isoformat(), "metrics": s.metrics,
                "invalid": s.invalid}

    def services_view(self) -> list[dict]:
        return [self.service_view(s) for s in self.current.samples] if self.current else []

    def infra_view(self) -> list[dict]:
        return [{"component": i.component, "status": i.status, "reason": i.reason or None,
                 "ts": i.ts.isoformat(), "metrics": i.metrics} for i in self.infra.values()]

    def overview(self) -> dict[str, Any]:
        """The bank as a whole."""
        samples = self.current.samples if self.current else []
        values = [s.metrics for s in samples if s.status == "up"]
        rps = sum(v.get("rps") or 0.0 for v in values)
        errors = sum((v.get("rps") or 0.0) * (v.get("error_rate") or 0.0) for v in values)
        return {"services": len(self.services),
                "up": sum(s.status == "up" for s in samples),
                "down": sum(s.status == "down" for s in samples),
                "unresponsive": sum(s.status == "unresponsive" for s in samples),
                "total_rps": round(rps, 2),
                "error_rate": round(errors / rps, 4) if rps else None,
                "total_net_bps": round(sum((v.get("net_rx_bps") or 0) + (v.get("net_tx_bps") or 0)
                                           for v in values), 1)}

    def status_view(self) -> dict[str, Any]:
        critical = [e for e in self.book.open.values() if e.issue.kind in PIPELINE_KINDS]
        state = "failed" if not self.fresh else "degraded" if critical else "ok"
        store = None
        if self.store:
            store = {"connected": self.store.pool is not None and not self.store.error,
                     "error": self.store.error, "buffered_rows": len(self.store.buffer),
                     "written_rows": self.store.written, "dropped_rows": self.store.dropped,
                     "rejected_rows": self.store.rejected,
                     "rows_without_partition": self.store.default_rows}
        return {"state": state, "source": self.source.name,
                "origin": self.current.origin if self.current else None,
                "source_error": self.last_error, "data_age_s": _round(self.age_s),
                "tick_s": self.settings.tick_s, "last_tick_ms": round(self.tick_ms, 1),
                "ticks": self.ticks, "failed_ticks": self.failed_ticks,
                "started_at": self.started_at.isoformat(), "history": store,
                "websocket_clients": len(self.hub.clients),
                "open_events": len(self.book.open)}

    def message(self, kind: str, changed: list[Event] | None = None) -> dict[str, Any]:
        """One WebSocket message: the complete current picture plus the events that changed."""
        return {"type": kind, "sequence": self.sequence, "sent_at": utcnow().isoformat(),
                "fresh": self.fresh, "origin": self.current.origin if self.current else None,
                "overview": self.overview(), "services": self.services_view(),
                "infrastructure": self.infra_view(),
                "events": [e.to_dict() for e in (changed if changed is not None
                                                 else self.book.open.values())],
                "open_events": len(self.book.open)}

    def twin_view(self) -> dict[str, Any]:
        """For the digital twin: only what is known, in the twin's own terms."""
        samples = []
        if self.current and self.fresh:
            for s in self.current.samples:
                if s.status == "unresponsive":
                    continue  # unknown is not a measurement; the twin ages the node itself
                metrics = {"availability": 0.0} if s.status == "down" else {
                    k: min(s.metrics[k], limit) for k, limit in TWIN_METRICS.items()
                    if s.metrics.get(k) is not None}
                samples.append({"node_id": s.node_id, "ts": s.ts.isoformat(), "metrics": metrics})
        return {"fresh": self.fresh, "origin": self.current.origin if self.current else None,
                "source": self.source.name, "age_s": _round(self.age_s), "samples": samples}


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def event_kinds() -> dict[str, dict]:
    return {k: {"severity": sev, "opens_after_ticks": n} for k, (sev, n) in KINDS.items()}


def parse_time(value: datetime | None, default: datetime) -> datetime:
    """Accept only times with a zone, so that nobody queries the wrong hours by accident."""
    if value is None:
        return default
    if value.tzinfo is None:
        raise ValueError("times must include a time zone, e.g. 2026-01-31T12:00:00Z")
    return value
