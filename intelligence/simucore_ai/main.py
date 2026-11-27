"""SimuCore AI service.

    python -m uvicorn simucore_ai.main:app --port 8300        (from the intelligence/ directory)

Reads the monitoring stream, publishes anomalies and predictions, stores them. Read-only towards the
bank, and its API is read-only too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from websockets.exceptions import WebSocketException

from simucore_ai import __version__, labels
from simucore_ai.calibrate import MAP, MODELS, load_calibration, profiles
from simucore_ai.detector import CONDITIONS, AnomalyDetector
from simucore_ai.engine import Engine, Models, Step
from simucore_ai.features import SNAPSHOT, TEMPORAL, load_services
from simucore_ai.features import describe as in_words
from simucore_ai.feed import BadMessage, MonitoringFeed, parse
from simucore_ai.predictor import FailurePredictor
from simucore_ai.store import Store, StoreUnavailable

log = logging.getLogger("ai")
Origin = Literal["measured", "synthetic"]
Node = Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9-]{0,60}$")]
Minutes = Annotated[int, Query(ge=1, le=60 * 24 * 31)]
Limit = Annotated[int, Query(ge=1, le=2000)]
BACKOFF_S = (0.5, 1, 2, 4, 5)
STALE_AFTER_S = 8.0
MAX_CLIENTS = 100


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AI_", extra="ignore")

    monitoring_ws: str = "ws://127.0.0.1:8200/ws/metrics"
    services_file: Path = MAP
    models_dir: Path = MODELS
    results_dir: Path = MODELS.parent / "results"
    database_url: str | None = None
    retention_days: int = Field(default=30, ge=7)
    ws_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173",
                             "http://localhost:8080", "http://127.0.0.1:8080"]


def load_models(folder: Path, services_file: Path) -> Models:
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    if tuple(meta["features"]) != TEMPORAL or tuple(meta["snapshot"]) != SNAPSHOT:
        raise RuntimeError("the models were trained with other features; train them again")
    return Models(
        AnomalyDetector.load(folder / "anomaly.npz"),
        {t: FailurePredictor.from_state(
            json.loads((folder / f"{t}.json").read_text(encoding="utf-8")))
         for t in labels.TARGETS},
        {t: float(meta["thresholds"][t]) for t in labels.TARGETS}, float(meta["off"]),
        profiles(load_calibration(folder / "calibration.json")), load_services(services_file),
        meta)


class Service:
    """The running whole: feed, engine, store, listeners."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.models = load_models(settings.models_dir, settings.services_file)
        self.engine = Engine(self.models)
        self.feed = MonitoringFeed(settings.monitoring_ws)
        self.store = Store(settings.database_url, self.models.meta, settings.retention_days) \
            if settings.database_url else None
        self.run_id = uuid4().hex[:12]
        self.sequence = 0
        self.started_at = datetime.now(UTC)
        self.connected = False
        self.received = self.refused = 0
        self.last_error: str | None = None
        self.last_ms = 0.0
        self.clients: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._receive(), name="feed"),
                       asyncio.create_task(self._watch(), name="expiry")]
        if self.store:
            self._tasks.append(asyncio.create_task(self.store.writer(), name="store"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.store:
            await self.store.aclose()

    async def _receive(self) -> None:
        attempt = 0
        while True:
            try:
                async for raw in self.feed.messages():
                    self.connected, self.last_error, attempt = True, None, 0
                    self.handle(raw)
                error = "connection closed"
            except (OSError, TimeoutError, WebSocketException) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            self.connected, self.last_error = False, error
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    def handle(self, raw: str | bytes) -> Step | None:
        self.received += 1
        try:
            obs = parse(raw, set(self.models.services))
        except BadMessage:
            self.refused += 1
            return None
        began = asyncio.get_running_loop().time()
        try:
            step = self.engine.observe(obs)
        except Exception:  # a bug must not end scoring
            self.refused += 1
            log.exception("could not score an observation")
            return None
        self.last_ms = (asyncio.get_running_loop().time() - began) * 1000
        self._publish(step)
        return step

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            step = self.engine.expire(datetime.now(UTC), STALE_AFTER_S)
            if step:
                self._publish(step)

    def _publish(self, step: Step) -> None:
        for e in step.anomalies:
            if e.closed_at or e.opened_at == self.engine.at:
                log.info("anomaly %s: %s, %s (score %.2f)",
                         "over" if e.closed_at else "found", e.service, e.condition, e.score)
        for w in step.warnings:
            if w.outcome != "open" or w.opened_at == self.engine.at:
                log.info("warning %s: %s, %s (%.0f %%)", w.outcome, w.service, w.target,
                         w.probability * 100)
        if self.store:
            self.store.add(step, self.engine.origin or "measured", labels.HORIZON_S)
        self.sequence += 1
        message = self.message("update", step)
        for q in list(self.clients):
            if q.full():
                q.get_nowait()  # only the newest picture matters
            q.put_nowait(message)

    @property
    def fresh(self) -> bool:
        return bool(self.engine.current) and self.engine.at is not None and \
            datetime.now(UTC) - self.engine.at <= timedelta(seconds=STALE_AFTER_S)

    def message(self, kind: str, step: Step | None = None) -> dict[str, Any]:
        step = step or Step(anomalies=self.engine.open_anomalies(),
                            warnings=self.engine.open_warnings())
        return {"type": kind, "run_id": self.run_id, "sequence": self.sequence,
                "sent_at": datetime.now(UTC).isoformat(),
                "assessed_at": self.engine.at.isoformat() if self.engine.at else None,
                "fresh": self.fresh, "origin": self.engine.origin,
                "model": self.models.version, "horizon_s": labels.HORIZON_S,
                "services": self.engine.view(),
                "anomalies": [e.view() for e in step.anomalies],
                "warnings": [w.view() for w in step.warnings]}

    def status(self) -> dict[str, Any]:
        return {"state": "ok" if self.fresh else "waiting", "connected": self.connected,
                "error": self.last_error, "origin": self.engine.origin,
                "assessed_at": self.engine.at, "model": self.models.version,
                "trained_at": self.models.meta["trained_at"],
                "trained_on": self.models.meta["trained_on"], "run_id": self.run_id,
                "started_at": self.started_at, "messages": self.received,
                "refused": self.refused, "scored": self.engine.scored,
                "last_scoring_ms": round(self.last_ms, 1),
                "open_anomalies": len(self.engine.open_anomalies()),
                "open_warnings": len(self.engine.open_warnings()),
                "websocket_clients": len(self.clients),
                "store": self.store.status() if self.store else None}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = Service(settings)
        await app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.stop()

    app = FastAPI(title="SimuCore AI", version=__version__, lifespan=lifespan,
                  description="Anomaly detection and failure prediction. Read-only.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.ws_origins, allow_methods=["GET"])

    def service() -> Service:
        return app.state.service

    def store() -> Store:
        if service().store is None:
            raise HTTPException(503, "History is disabled: no database is configured")
        return service().store

    def period(start: datetime | None, end: datetime | None, minutes: int) -> tuple:
        for t in (start, end):
            if t is not None and t.tzinfo is None:
                raise HTTPException(422, "times must include a time zone")
        until = end or datetime.now(UTC)
        since = start or until - timedelta(minutes=minutes)
        if since >= until:
            raise HTTPException(422, "'start' must be before 'end'")
        return since, until

    async def query(call) -> Any:
        try:
            return await call
        except StoreUnavailable as exc:
            raise HTTPException(503, f"History is unavailable: {exc}") from exc

    @app.get("/health/live")
    async def live() -> dict:
        return {"live": True}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict:
        s = service().status()
        if not s["connected"]:
            response.status_code = 503
        return {"ready": s["connected"], **s}

    @app.get("/api/v1/ai/status")
    async def status() -> dict:
        return service().status()

    @app.get("/api/v1/ai/model")
    async def model() -> dict:
        """What the models are, what they were tested on, and how well they did there."""
        m = service().models
        validation = settings.results_dir / "validation.json"
        return {**m.meta, "conditions": CONDITIONS,
                "feature_descriptions": {f: in_words(f) for f in TEMPORAL},
                "anomaly_detection": "Isolation Forest, unsupervised: finds unusual moments, "
                                     "predicts nothing",
                "failure_prediction": "XGBoost, supervised: probability that the service degrades "
                                      f"or fails within {labels.HORIZON_S} s",
                "validation": json.loads(validation.read_text(encoding="utf-8"))["summary"]
                if validation.exists() else None}

    @app.get("/api/v1/ai/insights")
    async def insights() -> dict:
        return service().message("snapshot")

    @app.get("/api/v1/ai/anomalies")
    async def anomalies(node_id: Node = None, start: datetime | None = None,
                        end: datetime | None = None, minutes: Minutes = 120, limit: Limit = 200,
                        origin: Origin = "measured") -> list[dict]:
        since, until = period(start, end, minutes)
        return await query(store().anomalies(since, until, node_id, origin, limit))

    @app.get("/api/v1/ai/warnings")
    async def warnings(node_id: Node = None,
                       target: Literal["degradation", "failure"] | None = None,
                       start: datetime | None = None, end: datetime | None = None,
                       minutes: Minutes = 120, limit: Limit = 200,
                       origin: Origin = "measured") -> list[dict]:
        since, until = period(start, end, minutes)
        return await query(store().warnings(since, until, node_id, target, origin, limit))

    @app.get("/api/v1/ai/predictions/{node_id}")
    async def predictions(node_id: str, target: Literal["degradation", "failure"] | None = None,
                          start: datetime | None = None, end: datetime | None = None,
                          minutes: Minutes = 60, limit: Limit = 1000,
                          origin: Origin = "measured") -> list[dict]:
        if node_id not in {s.node_id for s in service().models.services.values()}:
            raise HTTPException(404, f"Unknown component {node_id!r}")
        since, until = period(start, end, minutes)
        return await query(store().predictions(since, until, node_id, target, origin, limit))

    @app.get("/api/v1/ai/performance")
    async def performance(start: datetime | None = None, end: datetime | None = None,
                          minutes: Minutes = 60 * 24, origin: Origin = "measured") -> dict:
        since, until = period(start, end, minutes)
        return {"start": since, "end": until,
                "history": await query(store().performance(since, until, origin)),
                "this_run": service().engine.performance()}

    @app.websocket("/ws/insights")
    async def stream(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin is not None and origin not in settings.ws_origins:
            await ws.close(1008)
            return
        if len(service().clients) >= MAX_CLIENTS:
            await ws.close(1013)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(3)
        q.put_nowait(service().message("snapshot"))
        service().clients.add(q)

        async def send() -> None:
            while True:
                async with asyncio.timeout(30):
                    message = await q.get()
                async with asyncio.timeout(10):
                    await ws.send_json(message)

        sender = asyncio.create_task(send())
        try:
            while True:  # nothing is expected from the client; this notices that it leaves
                reader = asyncio.create_task(ws.receive_text())
                done, _ = await asyncio.wait({reader, sender}, return_when="FIRST_COMPLETED")
                if sender in done:
                    reader.cancel()
                    break
                reader.result()
        except (WebSocketDisconnect, RuntimeError, KeyError):
            pass
        finally:
            service().clients.discard(q)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, RuntimeError,
                                     WebSocketDisconnect):
                await sender
            with contextlib.suppress(RuntimeError):
                await ws.close()

    @app.get("/metrics")
    async def own_metrics() -> Response:
        s, r = service(), CollectorRegistry()
        for name, value in {
            "ai_up": 1.0 if s.fresh else 0.0, "ai_scored_observations": s.engine.scored,
            "ai_scoring_milliseconds": s.last_ms, "ai_refused_messages": s.refused,
            "ai_open_anomalies": len(s.engine.open_anomalies()),
            "ai_open_warnings": len(s.engine.open_warnings()),
            "ai_buffered_rows": len(s.store.buffer) if s.store else 0,
        }.items():
            Gauge(name, name.replace("_", " "), registry=r).set(value)
        scores = Gauge("ai_anomaly_score", "0..1, 0.5 is the threshold", ["service"], registry=r)
        risk = Gauge("ai_risk_probability", "within the horizon", ["service", "target"],
                     registry=r)
        for name, a in s.engine.current.items():
            if a.anomaly:
                scores.labels(name).set(a.anomaly["score"])
            for t, v in a.risk.items():
                if v["probability"] is not None:
                    risk.labels(name, t).set(v["probability"])
        return Response(generate_latest(r), media_type=CONTENT_TYPE_LATEST)

    return app


def __getattr__(name: str):
    """``simucore_ai.main:app`` for uvicorn, without building an app on import."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
