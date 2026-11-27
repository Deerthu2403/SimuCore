"""Receives what monitoring reports. One way: nothing is ever sent, and everything received is
untrusted input until it has been checked."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import websockets

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "monitoring"}
MAX_MESSAGE = 2 * 1024 * 1024
SILENCE_S = 10.0
STATUSES = {"up", "down", "unresponsive"}


class BadMessage(ValueError):
    """Not usable."""


@dataclass(frozen=True)
class ServiceObs:
    status: str
    metrics: dict[str, float | None]


@dataclass(frozen=True)
class Observation:
    at: datetime
    origin: str
    services: dict[str, ServiceObs]


class MonitoringFeed:
    def __init__(self, url: str) -> None:
        if urlsplit(url).scheme != "ws" or urlsplit(url).hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Refusing {url!r}: not the SimuCore monitoring service")
        self.url = url

    async def messages(self) -> AsyncIterator[str | bytes]:
        async with websockets.connect(self.url, max_size=MAX_MESSAGE, open_timeout=5,
                                      ping_interval=5, ping_timeout=5, close_timeout=2) as ws:
            while True:
                async with asyncio.timeout(SILENCE_S):
                    yield await ws.recv()


def parse(raw: str | bytes, known: set[str]) -> Observation:
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadMessage("not JSON") from exc
    if not isinstance(m, dict) or m.get("type") not in ("snapshot", "update"):
        raise BadMessage("not a monitoring message")
    if m.get("fresh") is not True:
        raise BadMessage("monitoring has no current data")
    if m.get("origin") not in ("measured", "synthetic"):
        raise BadMessage("unknown origin")
    try:
        at = datetime.fromisoformat(m["collected_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadMessage("bad time") from exc
    if at.tzinfo is None:
        raise BadMessage("time without zone")
    listed = m.get("services")
    if not isinstance(listed, list) or len(listed) > 1000:
        raise BadMessage("services is not a list")
    services = {}
    for s in listed:
        if not isinstance(s, dict) or s.get("service") not in known \
                or s.get("status") not in STATUSES:
            continue
        services[s["service"]] = ServiceObs(s["status"], _metrics(s.get("metrics")))
    if not services:
        raise BadMessage("no known service in the message")
    return Observation(at, m["origin"], services)


def _metrics(raw: object) -> dict[str, float | None]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float | None] = {}
    for k, v in list(raw.items())[:64]:
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) \
                or not 0 <= v <= 1e15:
            out[str(k)[:48]] = None
        else:
            out[str(k)[:48]] = float(v)
    return out
