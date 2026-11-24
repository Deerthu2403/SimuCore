"""SimuCore monitoring service.

    python -m uvicorn monitor.main:app --port 8200        (from the monitoring/ directory)

The API is read-only. Every answer that contains data says where the data comes from.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import anyio
from fastapi import FastAPI, HTTPException, Path, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from monitor.catalog import BY_KEY, catalog
from monitor.collector import Collector, event_kinds, parse_time
from monitor.config import Settings, load_services
from monitor.hub import MAX_CLIENTS, Hub
from monitor.models import utcnow
from monitor.sources import Source, make_source
from monitor.store import MAX_POINTS, Store, StoreUnavailable

Origin = Literal["measured", "synthetic"]
Name = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,40}$")]
WS_SEND_TIMEOUT_S = 10


def create_app(settings: Settings | None = None, source: Source | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel("WARNING")
    services = load_services(settings.services_file)
    by_name = {s.name: s for s in services}
    hub = Hub()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        src = source or make_source(settings, services)
        store = Store(settings.database_url, src.name, settings.buffer_rows,
                      settings.retention_days) if settings.database_url else None
        app.state.collector = Collector(settings, services, src, store, hub)
        await app.state.collector.start()
        try:
            yield
        finally:
            await app.state.collector.stop()

    app = FastAPI(title="SimuCore Monitoring", version="0.3.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.ws_origins, allow_methods=["GET"])

    def col() -> Collector:
        return app.state.collector

    def store() -> Store:
        if col().store is None:
            raise HTTPException(503, "History is disabled: no database is configured")
        return col().store

    def period(start: datetime | None, end: datetime | None, minutes: int) -> tuple:
        try:
            until = parse_time(end, utcnow())
            since = parse_time(start, until - timedelta(minutes=minutes))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if since >= until:
            raise HTTPException(422, "'start' must be before 'end'")
        return since, until

    def steps(since: datetime, until: datetime, step: int | None) -> int:
        seconds = (until - since).total_seconds()
        step = step or max(1, round(seconds / 300))
        if seconds / step > MAX_POINTS:
            raise HTTPException(422, f"more than {MAX_POINTS} points: use a larger step")
        return step

    async def query(call) -> Any:
        try:
            return await call
        except StoreUnavailable as exc:
            raise HTTPException(503, f"History is unavailable: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # -------------------------------------------------------------------- health
    @app.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    async def ready(response: Response) -> dict:
        """Ready while the collector loop is turning. Problems it has found are in /status."""
        stalled = time.monotonic() - col().last_tick_mono > 5 * settings.tick_s + 5
        response.status_code = 503 if stalled else 200
        return {"ready": not stalled, **col().status_view()}

    @app.get("/api/v1/monitoring/status", tags=["monitoring"])
    async def status() -> dict:
        return col().status_view()

    # ------------------------------------------------------------------- current
    @app.get("/api/v1/metrics/catalog", tags=["metrics"])
    async def metric_catalog() -> dict:
        return {"metrics": catalog(), "event_kinds": event_kinds(),
                "services": [{"service": s.name, "title": s.title, "node_id": s.node_id,
                              "uses_database": s.uses_database} for s in services]}

    @app.get("/api/v1/metrics/current", tags=["metrics"])
    async def current() -> dict:
        return col().message("current")

    @app.get("/api/v1/metrics/current/{service}", tags=["metrics"])
    async def current_one(service: Name) -> dict:
        if service not in by_name:
            raise HTTPException(404, f"Unknown service {service!r}")
        found = [s for s in col().services_view() if s["service"] == service]
        if not found:
            raise HTTPException(503, "Nothing has been collected yet")
        return {"fresh": col().fresh, "origin": col().current.origin, **found[0]}

    @app.get("/api/v1/infrastructure/current", tags=["infrastructure"])
    async def infra_current() -> dict:
        return {"fresh": col().fresh, "overview": col().overview(),
                "components": col().infra_view()}

    # ------------------------------------------------------------------- history
    @app.get("/api/v1/metrics/history/{service}", tags=["metrics"])
    async def history(service: Name, metrics: str = "cpu_pct,mem_pct,latency_ms,rps,error_rate",
                      start: datetime | None = None, end: datetime | None = None,
                      minutes: int = Query(15, ge=1, le=60 * 24 * 31),
                      step: int | None = Query(None, ge=1, le=86_400),
                      origin: Origin = "measured") -> dict:
        if service not in by_name:
            raise HTTPException(404, f"Unknown service {service!r}")
        keys = list(dict.fromkeys(k.strip() for k in metrics.split(",") if k.strip()))
        if not keys or any(k not in BY_KEY for k in keys):
            raise HTTPException(422, f"metrics must be among {sorted(BY_KEY)}")
        since, until = period(start, end, minutes)
        size = steps(since, until, step)
        points = await query(store().history(by_name[service].node_id, keys, since, until, size,
                                             origin))
        return {"service": service, "origin": origin, "start": since.isoformat(),
                "end": until.isoformat(), "step_s": size, "points": points}

    @app.get("/api/v1/infrastructure/history/{component}", tags=["infrastructure"])
    async def infra_history(component: Name, start: datetime | None = None,
                            end: datetime | None = None,
                            minutes: int = Query(15, ge=1, le=60 * 24 * 31),
                            step: int | None = Query(None, ge=1, le=86_400),
                            origin: Origin = "measured") -> dict:
        since, until = period(start, end, minutes)
        size = steps(since, until, step)
        points = await query(store().infra_history(component, since, until, size, origin))
        return {"component": component, "origin": origin, "step_s": size, "points": points}

    @app.get("/api/v1/metrics/availability", tags=["metrics"])
    async def availability(start: datetime | None = None, end: datetime | None = None,
                           minutes: int = Query(60, ge=1, le=60 * 24 * 31),
                           origin: Origin = "measured") -> dict:
        since, until = period(start, end, minutes)
        rows = await query(store().availability(since, until, origin))
        return {"origin": origin, "start": since.isoformat(), "end": until.isoformat(),
                "services": rows}

    # -------------------------------------------------------------------- events
    @app.get("/api/v1/events/open", tags=["events"])
    async def open_events() -> list[dict]:
        return [e.to_dict() for e in col().book.open.values()]

    @app.get("/api/v1/events/history", tags=["events"])
    async def event_history(start: datetime | None = None, end: datetime | None = None,
                            minutes: int = Query(60, ge=1, le=60 * 24 * 31),
                            service: str | None = Query(None, pattern=r"^[a-z][a-z0-9-]{0,40}$"),
                            limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
        since, until = period(start, end, minutes)
        return await query(store().events(since, until, service, limit))

    # ---------------------------------------------------------------------- twin
    @app.get("/api/v1/twin/samples", tags=["digital twin"])
    async def twin_samples() -> dict:
        return col().twin_view()

    # ----------------------------------------------------------------- websocket
    @app.websocket("/ws/metrics")
    async def stream(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin is not None and origin not in settings.ws_origins:
            await ws.close(1008)  # another web page must not read this stream
            return
        if len(hub.clients) >= MAX_CLIENTS:
            await ws.close(1013)
            return
        await ws.accept()
        with hub.join() as client:
            client.offer(col().message("snapshot"))

            async def sender(scope: anyio.CancelScope) -> None:
                try:
                    while True:
                        message = client.filtered(await client.queue.get())
                        with anyio.fail_after(WS_SEND_TIMEOUT_S):
                            await ws.send_json({**message, "skipped_updates": client.skipped})
                except (WebSocketDisconnect, RuntimeError, TimeoutError):
                    scope.cancel()  # the client is gone or does not read

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(sender, tasks.cancel_scope)
                try:
                    while True:
                        text = await ws.receive_text()
                        client.services = _subscription(text, by_name)
                        client.offer(col().message("snapshot"))
                except (ValueError, KeyError) as exc:  # KeyError: a binary frame
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await ws.close(1003, str(exc)[:100])
                except WebSocketDisconnect:
                    pass
                tasks.cancel_scope.cancel()

    # ------------------------------------------------------ monitoring the monitor
    @app.get("/metrics", tags=["monitoring"])
    async def own_metrics() -> Response:
        c, r = col(), CollectorRegistry()
        v = c.status_view()
        for name, value in {
            "monitor_up": 1.0 if v["state"] != "failed" else 0.0,
            "monitor_data_age_seconds": v["data_age_s"] if v["data_age_s"] is not None else -1,
            "monitor_tick_milliseconds": v["last_tick_ms"],
            "monitor_ticks": v["ticks"], "monitor_failed_ticks": v["failed_ticks"],
            "monitor_open_events": v["open_events"],
            "monitor_websocket_clients": v["websocket_clients"],
            "monitor_buffered_rows": len(c.store.buffer) if c.store else 0,
            "monitor_written_rows": c.store.written if c.store else 0,
            "monitor_dropped_rows": c.store.dropped if c.store else 0,
        }.items():
            Gauge(name, name.replace("_", " "), registry=r).set(value)
        return Response(generate_latest(r), media_type=CONTENT_TYPE_LATEST)

    return app


def _subscription(text: str, known: dict) -> set[str] | None:
    """{"type": "subscribe", "services": ["account", ...]}; null means all services."""
    if len(text) > 2000:
        raise ValueError("message too long")
    try:
        body = json.loads(text)
        kind, wanted = body["type"], body["services"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("expected a subscribe message") from exc
    if kind != "subscribe":
        raise ValueError("expected a subscribe message")
    if wanted is None:
        return None
    if not isinstance(wanted, list) or any(w not in known for w in wanted):
        raise ValueError("unknown service in subscription")
    return set(wanted)


def __getattr__(name: str):
    """``monitor.main:app`` for uvicorn, without building an app on import."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
