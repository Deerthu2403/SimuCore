"""Notification Service: accepts events, delivers them asynchronously to an in-memory outbox.

Nothing is actually sent. Addresses are ``example.invalid`` and delivery means "recorded".
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from bankcommon.app import ServiceContext, build_service, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError, DependencyUnavailable

router = APIRouter(prefix="/internal/notifications", dependencies=[Depends(require_internal)])
QUEUE_SIZE, REMEMBERED, MAX_ATTEMPTS, WORKERS = 1000, 5000, 3, 4


class Event(BaseModel):
    event_id: str = Field(max_length=128)
    event: str = Field(max_length=64)
    customer_id: str = Field(pattern=r"^CUST\d{5}$")
    amount: int


class Outbox:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.queue: asyncio.Queue[Event] = asyncio.Queue(QUEUE_SIZE)
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.delivered: deque[dict] = deque(maxlen=500)
        self.counts = {"accepted": 0, "duplicate": 0, "delivered": 0, "suppressed": 0,
                       "dead_letter": 0}

    def accept(self, event: Event) -> bool:
        if event.event_id in self.seen:
            self.counts["duplicate"] += 1
            return False
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            raise BankError(503, "OVERLOADED", "notification queue is full") from None
        self.seen[event.event_id] = None
        if len(self.seen) > REMEMBERED:
            self.seen.popitem(last=False)
        self.counts["accepted"] += 1
        return True

    async def worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._deliver(event)
            except Exception:
                self.counts["dead_letter"] += 1

    async def _deliver(self, event: Event) -> None:
        for attempt in range(MAX_ATTEMPTS):
            try:
                r = await self.ctx.client.call(
                    "customer", "GET", f"/internal/customers/{event.customer_id}")
                break
            except DependencyUnavailable:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        if r.status_code != 200:
            raise LookupError(event.customer_id)
        channel = r.json()["contact_pref"]
        if channel == "none":
            self.counts["suppressed"] += 1
            return
        self.delivered.append({**event.model_dump(), "channel": channel})
        self.counts["delivered"] += 1


@asynccontextmanager
async def lifespan(ctx: ServiceContext) -> AsyncIterator[None]:
    ctx.outbox = Outbox(ctx)  # type: ignore[attr-defined]
    workers = [asyncio.create_task(ctx.outbox.worker()) for _ in range(WORKERS)]
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w


@router.post("", status_code=202)
async def notify(event: Event, request: Request) -> dict:
    return {"accepted": get_ctx(request).outbox.accept(event)}


@router.get("/stats")
async def stats(request: Request) -> dict:
    box = get_ctx(request).outbox
    return {**box.counts, "queued": box.queue.qsize()}


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("notification", settings, lifespan=lifespan,
                        transport_factory=transport_factory)
    app.include_router(router)
    return app
