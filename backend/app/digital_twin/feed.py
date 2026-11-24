"""The twin's only window on the real system: a WebSocket from the monitoring service.

It receives and never sends. Everything received is untrusted input and is checked before it becomes
an observation.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import websockets

from app.digital_twin.model import InfraObs, Observation, ServiceObs

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "monitoring"}
MAX_MESSAGE = 2 * 1024 * 1024
SILENCE_S = 10.0  # monitoring speaks every 2 s; longer silence means the connection is dead
STATUSES = {"up", "down", "unresponsive"}
MAX_VALUE = 1e15


class BadMessage(ValueError):
    """Not a usable monitoring message."""


class NotFresh(BadMessage):
    """Monitoring is alive but has no current data itself."""


class MonitoringFeed:
    def __init__(self, url: str) -> None:
        if urlsplit(url).scheme != "ws" or urlsplit(url).hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Refusing {url!r}: not the SimuCore monitoring service")
        self.url = url

    async def messages(self) -> AsyncIterator[str | bytes]:
        """Messages of one connection. Ends or raises when the connection does."""
        async with websockets.connect(self.url, max_size=MAX_MESSAGE, open_timeout=5,
                                      ping_interval=5, ping_timeout=5, close_timeout=2) as ws:
            while True:
                yield await _receive(ws)


async def _receive(ws: Any) -> str | bytes:
    import asyncio

    async with asyncio.timeout(SILENCE_S):
        return await ws.recv()


def parse(raw: str | bytes, known_services: set[str]) -> Observation:
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadMessage("not JSON") from exc
    if not isinstance(m, dict) or m.get("type") not in ("snapshot", "update"):
        raise BadMessage("not a monitoring message")
    if m.get("fresh") is not True:
        raise NotFresh("monitoring has no current data")
    if m.get("origin") not in ("measured", "synthetic"):
        raise BadMessage("unknown origin")
    run_id, sequence = m.get("run_id"), m.get("sequence")
    if not isinstance(run_id, str) or not 0 < len(run_id) <= 64 \
            or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise BadMessage("no run or sequence")
    services: dict[str, ServiceObs] = {}
    for s in _list(m, "services"):
        name = s.get("service")
        if name not in known_services or s.get("status") not in STATUSES:
            continue
        metrics, dropped = _metrics(s.get("metrics"))
        deployment = s.get("deployment")
        services[name] = ServiceObs(
            s["status"], _text(s.get("reason")), metrics,
            deployment if deployment in ("process", "container") else None,
            partial=bool(dropped or s.get("invalid")))
    if not services:
        raise BadMessage("no known service in the message")
    links = {}
    for k in _list(m, "links"):
        if k.get("source") in known_services and k.get("target") in known_services:
            links[(k["source"], k["target"])] = _metrics(k.get("metrics"))[0]
    infra = {}
    for c in _list(m, "infrastructure"):
        if isinstance(c.get("component"), str) and c.get("status") in STATUSES:
            numbers = {k: v for k, v in _metrics(c.get("metrics"))[0].items() if v is not None}
            infra[c["component"][:64]] = InfraObs(c["status"], _text(c.get("reason")),
                                                  _time(c.get("ts")), numbers)
    return Observation(run_id, sequence, _time(m.get("collected_at")), m["origin"], services,
                       links, infra)


def _list(m: dict, key: str) -> list[dict]:
    value = m.get(key, [])
    if not isinstance(value, list) or len(value) > 1000:
        raise BadMessage(f"{key} is not a list")
    return [x for x in value if isinstance(x, dict)]


def _metrics(raw: object) -> tuple[dict[str, float | None], int]:
    """Usable values, and how many had to be dropped."""
    if not isinstance(raw, dict):
        return {}, 1
    out: dict[str, float | None] = {}
    dropped = 0
    for key, v in list(raw.items())[:64]:
        if v is None:
            out[str(key)[:48]] = None
        elif isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) \
                or not 0 <= v <= MAX_VALUE:
            dropped += 1
        else:
            out[str(key)[:48]] = float(v)
    return out, dropped


def _text(value: object) -> str:
    return value[:300] if isinstance(value, str) else ""


def _time(value: object) -> datetime:
    try:
        t = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BadMessage("bad time") from exc
    if t.tzinfo is None:
        raise BadMessage("time without zone")
    return t
