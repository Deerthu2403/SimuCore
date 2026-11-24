"""Keeps the twin synchronised: every monitoring message becomes a new twin state."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from websockets.exceptions import WebSocketException

from app.digital_twin.feed import BadMessage, MonitoringFeed, NotFresh, parse
from app.digital_twin.history import TwinHistory
from app.digital_twin.hub import TwinHub
from app.digital_twin.model import Change
from app.digital_twin.state import DigitalTwin, Rejected

log = logging.getLogger(__name__)
BACKOFF_S = (0.5, 1, 2, 4, 5)


class TwinSync:
    def __init__(self, twin: DigitalTwin, feed: MonitoringFeed, hub: TwinHub,
                 history: TwinHistory | None = None) -> None:
        self.twin, self.feed, self.hub, self.history = twin, feed, hub, history
        self.connected = False
        self.received = self.applied = self.reconnects = 0
        self.rejected: dict[str, int] = {}
        self.last_error: str | None = None
        self.last_lag_s: float | None = None
        self.last_message_at: datetime | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._receive(), name="twin-feed"),
                       asyncio.create_task(self._watch(), name="twin-expiry")]
        if self.history:
            self._tasks.append(asyncio.create_task(self.history.writer(), name="twin-history"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.history:
            self.history.snapshot(self.twin)
            await self.history.aclose()

    # ------------------------------------------------------------------- receiving
    async def _receive(self) -> None:
        attempt = 0
        while True:
            try:
                async for raw in self.feed.messages():
                    if not self.connected:
                        log.info("connected to monitoring at %s", self.feed.url)
                        self.connected, self.last_error, attempt = True, None, 0
                    self.handle(raw)
                error = "connection closed"
            except (OSError, TimeoutError, WebSocketException) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            if self.connected or self.last_error is None:
                log.warning("monitoring connection lost: %s", error)
            self.connected, self.last_error = False, error
            self.reconnects += 1
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    def handle(self, raw: str | bytes) -> Change | None:
        """One message. Whatever is wrong with it, the twin stays intact."""
        now = datetime.now(UTC)
        self.received += 1
        self.last_message_at = now
        try:
            obs = parse(raw, set(self.twin.blueprint.service_ids))
            change = self.twin.apply(obs, now)
        except (BadMessage, Rejected) as exc:
            kind = "not_fresh" if isinstance(exc, NotFresh) else \
                "outdated" if isinstance(exc, Rejected) else "unusable"
            self.rejected[kind] = self.rejected.get(kind, 0) + 1
            return None
        except Exception:  # a bug must not end synchronisation
            self.rejected["error"] = self.rejected.get("error", 0) + 1
            log.exception("could not apply an observation")
            return None
        self.applied += 1
        self.last_lag_s = round((now - obs.observed_at).total_seconds(), 3)
        self._publish(change)
        return change

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                change = self.twin.expire()
                if change:
                    self._publish(change)
                if self.history:
                    self.history.keep_alive(self.twin)
            except Exception:
                log.exception("expiry failed")

    def _publish(self, change: Change) -> None:
        for t in change.transitions:
            if t.object_kind == "component":
                log.info("%s: %s -> %s (%s)", t.object_id,
                         t.from_status.value if t.from_status else "new", t.to_status.value,
                         t.reason)
        if self.history:
            self.history.record(self.twin, change)
        self.hub.publish(change)

    # ------------------------------------------------------------------- reporting
    def status(self) -> dict[str, Any]:
        t = self.twin
        state = "live" if t.synchronised else "waiting" if t.last_applied_at is None else "stale"
        return {
            "state": state, "connected": self.connected, "monitoring_url": self.feed.url,
            "origin": t.origin, "version": t.version, "run_id": str(t.run_id),
            "last_observation_at": t.last_observed_at, "last_applied_at": t.last_applied_at,
            "last_message_at": self.last_message_at, "lag_s": self.last_lag_s,
            "stale_after_s": t.stale_after.total_seconds(),
            "messages_received": self.received, "observations_applied": self.applied,
            "messages_rejected": dict(self.rejected), "reconnects": self.reconnects,
            "last_error": self.last_error, "websocket_clients": len(self.hub.clients),
            "history": self.history.status() if self.history else None}
