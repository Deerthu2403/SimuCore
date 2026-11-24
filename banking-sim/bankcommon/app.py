"""Application factory shared by every banking microservice."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST

from bankcommon.client import ServiceClient, TransportFactory
from bankcommon.config import Settings, registry
from bankcommon.errors import BankError
from bankcommon.faults import Fault, FaultEngine, FaultRequest
from bankcommon.telemetry import Telemetry, header_bytes

log = logging.getLogger("bank")
QUEUE_TIMEOUT_S = 1.0
OUT_OF_BAND = ("/control", "/metrics", "/telemetry")
_SAFE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class ServiceContext:
    """Everything a service needs at runtime; available as ``request.app.state.ctx``."""

    def __init__(self, name: str, settings: Settings,
                 transport_factory: TransportFactory | None = None) -> None:
        self.name, self.settings = name, settings
        self.info = registry().services[name]
        self.faults = FaultEngine(settings.faults_enabled, settings.seed)
        uses_db = name == "database" or "database" in registry().of(name)
        self.telemetry = Telemetry(name, settings.mem_limit_mb, uses_db)
        self.client = ServiceClient(name, settings.internal_key, transport_factory, self.telemetry)
        self.slots = asyncio.Semaphore(self.info["max_concurrency"])
        self.waiting = 0
        self.started_at = time.time()
        self.instance_id = uuid.uuid4().hex[:12]
        self.shutdown: Callable[[], None] | None = None
        self.self_check: Callable[[], Awaitable[bool]] | None = None
        # Open client connections. Set by the process runner, which owns the sockets.
        self.connections: Callable[[], int] = lambda: 0
        self.in_flight = 0
        t = self.telemetry
        t.gauge("bank_available", "0 while the service refuses all work",
                lambda: 0.0 if self.faults.get("downtime") else 1.0)
        t.gauge("bank_active_connections", "Open inbound TCP connections, idle ones included",
                lambda: self.connections())
        t.gauge("bank_request_queue_length", "Requests waiting for a worker slot",
                lambda: self.waiting)
        t.gauge("bank_in_flight_requests", "Requests being worked on", lambda: self.in_flight)
        self.faults.register("resource_exhaustion", "slots", self._hold_slots)
        self.faults.register("resource_exhaustion", "memory", self._hold_memory)

    async def _hold_slots(self, fault: Fault) -> None:
        async def hold_one() -> None:
            async with self.slots:
                await asyncio.Event().wait()

        n = max(1, round(self.info["max_concurrency"] * fault.req.magnitude))
        tasks = [asyncio.create_task(hold_one()) for _ in range(n)]
        fault.cleanup.extend(t.cancel for t in tasks)

    async def _hold_memory(self, fault: Fault) -> None:
        target = self.settings.mem_limit_mb * 0.98 * fault.req.magnitude
        need = int(max(0.0, target - self.telemetry.memory_mb()))
        ballast = [b"\x01" * 1_048_576 for _ in range(min(need, self.settings.mem_limit_mb))]
        fault.cleanup.append(ballast.clear)

    @property
    def memory_exhausted(self) -> bool:
        return self.telemetry.memory_mb() >= self.settings.mem_limit_mb * 0.95


class SimulationMiddleware:
    """Pure-ASGI: correlation ids, fault effects, concurrency bulkhead and metrics."""

    def __init__(self, app, ctx: ServiceContext) -> None:
        self.app, self.ctx = app, ctx

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(OUT_OF_BAND):
            await self.app(scope, receive, send)
            return
        ctx, start, status = self.ctx, time.perf_counter(), 500
        health = scope["path"].startswith("/health")
        incoming = dict(scope["headers"]).get(b"x-correlation-id", b"").decode("latin-1")
        corr = incoming if _SAFE_ID.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["correlation_id"] = corr
        received, sent = header_bytes(scope["headers"]), 0

        async def tracked_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            return message

        async def tracked_send(message) -> None:
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message["headers"], (b"x-correlation-id", corr.encode())]
                sent += header_bytes(message["headers"])
            sent += len(message.get("body", b""))
            await send(message)

        async def reject(code: str, text: str) -> None:
            body = BankError(503, code, text).body(ctx.name, corr)
            await JSONResponse(body, 503)(scope, receive, tracked_send)

        try:
            if ctx.faults.get("downtime"):
                await reject("SERVICE_DOWN", "Simulated downtime")
            elif health:
                await self.app(scope, receive, tracked_send)
            elif ctx.memory_exhausted:
                await reject("RESOURCE_EXHAUSTED", "Memory budget exhausted")
            elif ctx.waiting >= ctx.info["max_concurrency"]:
                await reject("OVERLOADED", "Request queue is full")
            elif not await self._acquire_slot():
                await reject("OVERLOADED", "No free worker slot")
            else:
                ctx.in_flight += 1
                try:
                    await ctx.faults.delay()
                    await self.app(scope, tracked_receive, tracked_send)
                finally:
                    ctx.in_flight -= 1
                    ctx.slots.release()
        finally:
            if not health:
                route = getattr(scope.get("route"), "path", "unmatched")
                ctx.telemetry.record(scope["method"], route, status, time.perf_counter() - start)
                ctx.telemetry.traffic(received, sent)

    async def _acquire_slot(self) -> bool:
        self.ctx.waiting += 1
        try:
            await asyncio.wait_for(self.ctx.slots.acquire(), QUEUE_TIMEOUT_S)
            return True
        except TimeoutError:
            return False
        finally:
            self.ctx.waiting -= 1


def get_ctx(request: Request) -> ServiceContext:
    return request.app.state.ctx


def cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", "")


def require_internal(request: Request, x_internal_key: str = Header(default="")) -> None:
    expected = request.app.state.ctx.settings.internal_key
    if not hmac.compare_digest(x_internal_key.encode(), expected.encode()):
        raise BankError(401, "INTERNAL_ONLY", "Internal key required")


async def current_customer(request: Request, authorization: str = Header(default="")) -> str:
    """Resolve the bearer token to a customer id through the authentication service."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise BankError(401, "UNAUTHENTICATED", "Bearer token required")
    r = await get_ctx(request).client.call(
        "authentication", "POST", "/internal/verify", json={"token": token},
        correlation_id=cid(request))
    if r.status_code != 200:
        raise BankError(401, "UNAUTHENTICATED", "Invalid or expired token")
    return r.json()["customer_id"]


Lifespan = Callable[[ServiceContext], AbstractAsyncContextManager[None]]


def build_service(name: str, settings: Settings, *, lifespan: Lifespan | None = None,
                  transport_factory: TransportFactory | None = None) -> FastAPI:
    ctx = ServiceContext(name, settings, transport_factory)

    @asynccontextmanager
    async def run(app: FastAPI) -> AsyncIterator[None]:
        try:
            if lifespan:
                async with lifespan(ctx):
                    yield
            else:
                yield
        finally:
            ctx.faults.clear()
            await ctx.client.aclose()

    app = FastAPI(title=ctx.info["title"], lifespan=run, version="0.2.0",
                  description="SimuCore simulated bank. Synthetic data only.")
    app.state.ctx = ctx
    app.add_middleware(SimulationMiddleware, ctx=ctx)

    @app.exception_handler(BankError)
    async def on_bank_error(request: Request, exc: BankError) -> JSONResponse:
        return JSONResponse(exc.body(name, cid(request)), exc.status)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return JSONResponse(BankError(422, "INVALID_REQUEST", detail).body(name, cid(request)), 422)

    # ------------------------------------------------------------------ health
    @app.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"status": "alive", "service": name, "pid": os.getpid(),
                "instance_id": ctx.instance_id,
                "uptime_s": round(time.time() - ctx.started_at, 1)}

    async def readiness() -> dict:
        targets = list(ctx.client.edges)
        ups = await asyncio.gather(*(ctx.client.ping(t) for t in targets))
        deps = {t: {"up": up, "mode": ctx.client.edges[t].mode,
                    "circuit": ctx.client.breakers[t].state}
                for t, up in zip(targets, ups, strict=True)}
        own = await ctx.self_check() if ctx.self_check else True
        blocked = not own or any(not d["up"] and d["mode"] == "required" for d in deps.values())
        degraded = any(not d["up"] for d in deps.values())
        status = "not_ready" if blocked else "degraded" if degraded else "ready"
        return {"status": status, "service": name, "self_ok": own, "dependencies": deps}

    @app.get("/health/ready", tags=["health"])
    async def ready(response: Response) -> dict:
        body = await readiness()
        response.status_code = 503 if body["status"] == "not_ready" else 200
        return body

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {**(await live()), "readiness": await readiness()}

    # --------------------------------------------------------------- telemetry
    @app.get("/metrics", tags=["telemetry"])
    async def metrics() -> Response:
        fault = ctx.faults.get("telemetry_fault")
        mode = fault.req.mode if fault else None
        if mode == "slow":
            await asyncio.sleep(15)
        return Response(ctx.telemetry.exposition(mode), media_type=CONTENT_TYPE_LATEST)

    @app.get("/telemetry", tags=["telemetry"])
    async def telemetry() -> dict:
        return {"service": name, "node_id": ctx.info["node_id"],
                "metrics": ctx.telemetry.standard_metrics(ctx.faults.get("downtime") is None),
                "outbound_calls": dict(ctx.client.calls),
                "outbound_failures": dict(ctx.client.failed),
                "circuits": {t: b.state for t, b in ctx.client.breakers.items()},
                "faults": ctx.faults.describe()}

    # ----------------------------------------------------------------- control
    internal = [Depends(require_internal)]

    @app.post("/control/faults", tags=["control"], dependencies=internal)
    async def inject(req: FaultRequest) -> dict:
        fault = await ctx.faults.inject(req)
        log.warning(json.dumps({"event": "fault_injected", "service": name, **fault.describe()}))
        return fault.describe()

    @app.delete("/control/faults", tags=["control"], dependencies=internal)
    async def clear() -> dict:
        ctx.faults.clear()
        return {"cleared": True}

    @app.get("/control/whoami", tags=["control"], dependencies=internal)
    async def whoami() -> dict:
        return {"service": name, "pid": os.getpid(), "instance_id": ctx.instance_id}

    @app.post("/control/shutdown", tags=["control"], dependencies=internal)
    async def shutdown() -> dict:
        if ctx.shutdown is None:
            raise BankError(409, "NOT_MANAGED", "Process is not managed by bankcommon.run")
        asyncio.get_running_loop().call_later(0.1, ctx.shutdown)
        return {"stopping": True}

    return app
