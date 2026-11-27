"""The opinions of the AI service: received one way, checked, and kept apart from what was measured.

An anomaly or a prediction never changes the status of a component. Statuses rest on measurements;
this is what a model thinks about them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from websockets.exceptions import WebSocketException

from app.digital_twin.feed import BadMessage, MonitoringFeed
from app.digital_twin.model import Change

if TYPE_CHECKING:
    from app.digital_twin.hub import TwinHub
    from app.digital_twin.state import DigitalTwin

log = logging.getLogger(__name__)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "intelligence"}
ANOMALY_LEVELS = ("normal", "unusual", "abnormal")
RISK_LEVELS = ("low", "elevated", "high", "present", "recovering")
TARGETS = ("degradation", "failure")
STALE_AFTER_S = 10.0
BACKOFF_S = (0.5, 1, 2, 4, 5)


@dataclass(frozen=True)
class Contribution:
    metric: str
    share: float
    deviation: float


@dataclass(frozen=True)
class Reason:
    metric: str
    text: str
    contribution: float


@dataclass(frozen=True)
class AnomalyOpinion:
    score: float
    level: str
    condition: str
    since: datetime | None  # when the anomaly was confirmed; None if it is not (yet)
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class RiskOpinion:
    target: str
    probability: float | None
    level: str
    horizon_s: int


@dataclass(frozen=True)
class WarningOpinion:
    id: str
    target: str
    probability: float
    peak: float
    opened_at: datetime
    horizon_s: int


@dataclass(frozen=True)
class Insight:
    assessed_at: datetime
    model: str
    anomaly: AnomalyOpinion | None
    anomaly_reason: str  # why there is no anomaly score
    risks: tuple[RiskOpinion, ...]
    risk_reason: str  # why there is no prediction
    warnings: tuple[WarningOpinion, ...]
    reasons: tuple[Reason, ...]

    @property
    def essence(self) -> tuple:
        """What has to differ for the insight to count as changed."""
        a = self.anomaly
        return (a and (round(a.score, 2), a.level, a.condition, a.since, a.contributions),
                tuple((r.target, None if r.probability is None else round(r.probability, 2),
                       r.level) for r in self.risks),
                tuple((w.id, round(w.peak, 2)) for w in self.warnings), self.anomaly_reason,
                self.risk_reason, self.reasons)


@dataclass(frozen=True)
class InsightMessage:
    assessed_at: datetime
    origin: str
    insights: dict[str, Insight]  # component id -> insight


class InsightFeed(MonitoringFeed):
    hosts = ALLOWED_HOSTS
    what = "the SimuCore AI service"


def parse(raw: str | bytes, services: dict[str, str]) -> InsightMessage:
    """``services``: service name -> component id. Anything unusable raises or is left out."""
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadMessage("not JSON") from exc
    if not isinstance(m, dict) or m.get("type") not in ("snapshot", "update"):
        raise BadMessage("not an AI message")
    if m.get("fresh") is not True:
        raise BadMessage("the AI service has nothing current")
    if m.get("origin") not in ("measured", "synthetic"):
        raise BadMessage("unknown origin")
    at, model = _time(m.get("assessed_at")), _text(m.get("model"), 40)
    listed = m.get("services")
    if not isinstance(listed, list) or len(listed) > 1000:
        raise BadMessage("services is not a list")
    out = {}
    for s in listed:
        if isinstance(s, dict) and (cid := services.get(str(s.get("service")))):
            try:
                out[cid] = _insight(s, at, model)
            except (KeyError, TypeError, ValueError) as exc:
                log.debug("insight for %s refused: %s", cid, exc)
    return InsightMessage(at, m["origin"], out)


def _insight(s: dict, at: datetime, model: str) -> Insight:
    anomaly = None
    if a := s.get("anomaly"):
        opened = s.get("open_anomaly")
        anomaly = AnomalyOpinion(
            _share(a["score"]), _one_of(a["level"], ANOMALY_LEVELS), _text(a["condition"], 40),
            _time(opened["opened_at"]) if opened else None,
            tuple(Contribution(_text(c["metric"], 40), _share(c["share"]), _number(c["deviation"]))
                  for c in a["contributions"][:5]))
    risks = tuple(RiskOpinion(
        t, None if (r := s["risk"][t])["probability"] is None else _share(r["probability"]),
        _one_of(r["level"], RISK_LEVELS), int(_number(r["horizon_s"])))
        for t in TARGETS if t in s["risk"])
    warnings = tuple(WarningOpinion(
        _text(w["id"], 40), _one_of(w["target"], TARGETS), _share(w["probability"]),
        _share(w["peak"]), _time(w["opened_at"]), int(_number(w["horizon_s"])))
        for w in s["warnings"][:4])
    reasons = tuple(Reason(_text(r["metric"], 40), _text(r["text"], 120),
                           _number(r["contribution"])) for r in s["reasons"][:6])
    return Insight(at, model, anomaly, _text(s.get("anomaly_reason"), 120), risks,
                   _text(s.get("risk_reason"), 120), warnings, reasons)


class InsightSync:
    """Keeps the twin informed about what the models think."""

    def __init__(self, twin: DigitalTwin, feed: InsightFeed, hub: TwinHub) -> None:
        self.twin, self.feed, self.hub = twin, feed, hub
        self.connected = False
        self.received = self.applied = self.refused = 0
        self.last_error: str | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._receive(), name="insight-feed"),
                       asyncio.create_task(self._watch(), name="insight-expiry")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _receive(self) -> None:
        attempt = 0
        while True:
            try:
                async for raw in self.feed.messages():
                    if not self.connected:
                        log.info("connected to the AI service at %s", self.feed.url)
                        self.connected, self.last_error, attempt = True, None, 0
                    self.handle(raw)
                error = "connection closed"
            except (OSError, TimeoutError, WebSocketException) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            if self.connected:
                log.warning("AI service connection lost: %s", error)
            self.connected, self.last_error = False, error
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    def handle(self, raw: str | bytes) -> Change | None:
        self.received += 1
        try:
            m = parse(raw, self.twin.blueprint.service_ids)
            change = self.twin.apply_insights(m, datetime.now(UTC))
        except BadMessage:
            self.refused += 1
            return None
        except Exception:
            self.refused += 1
            log.exception("could not apply insights")
            return None
        self.applied += 1
        if change:
            self.hub.publish(change)
        return change

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            change = self.twin.expire_insights(datetime.now(UTC), STALE_AFTER_S)
            if change:
                self.hub.publish(change)

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "url": self.feed.url, "received": self.received,
                "applied": self.applied, "refused": self.refused, "last_error": self.last_error,
                "assessed_at": self.twin.insights_at}


# ----------------------------------------------------------------------------- checks
def _number(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ValueError("not a number")
    return float(v)


def _share(v: object) -> float:
    x = _number(v)
    if not 0 <= x <= 1:
        raise ValueError("not between 0 and 1")
    return x


def _one_of(v: object, allowed: tuple[str, ...]) -> str:
    if v not in allowed:
        raise ValueError("unknown word")
    return str(v)


def _text(v: object, length: int) -> str:
    return v[:length] if isinstance(v, str) else ""


def _time(v: object) -> datetime:
    try:
        t = datetime.fromisoformat(v)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BadMessage("bad time") from exc
    if t.tzinfo is None:
        raise BadMessage("time without zone")
    return t
