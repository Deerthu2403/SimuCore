"""Database Service: the only component that touches storage. Internal callers only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from bankcommon.app import ServiceContext, build_service, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError
from bankcommon.faults import Fault
from services.database import store

log = logging.getLogger("bank.database")


class ConnectionPool:
    """A real bounded pool: when every connection is busy, acquiring waits and then fails."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.path = ctx.settings.data_dir / "bank.sqlite3"
        self.idle: asyncio.Queue[sqlite3.Connection] = asyncio.Queue()
        self.all: list[sqlite3.Connection] = []
        self.refusing = False

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        first = store.connect(self.path)
        created = store.seed(first, self.ctx.settings.seed, self.ctx.settings.customers)
        log.info("database %s at %s", "seeded" if created else "reopened", self.path)
        self.all = [first] + [store.connect(self.path)
                              for _ in range(self.ctx.settings.db_pool_size - 1)]
        for c in self.all:
            self.idle.put_nowait(c)

    def close(self) -> None:
        for c in self.all:
            c.close()

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a query. The measured response time includes waiting for a connection."""
        started = time.perf_counter()
        try:
            return await self._run(fn, *args)
        finally:
            self.ctx.telemetry.db_response(time.perf_counter() - started)

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self.refusing:
            raise BankError(503, "DB_CONNECTION_FAILED", "connection refused")
        try:
            conn = await asyncio.wait_for(self.idle.get(), self.ctx.settings.db_acquire_timeout_s)
        except TimeoutError:
            raise BankError(503, "DB_POOL_EXHAUSTED", "no free connection") from None
        try:
            return await asyncio.to_thread(self._clean_call, conn, fn, *args)
        except sqlite3.OperationalError as exc:
            raise BankError(503, "DB_BUSY", str(exc)) from exc
        finally:
            self.idle.put_nowait(conn)

    @staticmethod
    def _clean_call(conn: sqlite3.Connection, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(conn, *args)
        finally:
            if conn.in_transaction:  # e.g. COMMIT itself failed: never pool a dirty connection
                conn.rollback()

    # ------------------------------------------------------------------ faults
    async def refuse(self, fault: Fault) -> None:
        self.refusing = True
        fault.cleanup.append(lambda: setattr(self, "refusing", False))

    async def exhaust(self, fault: Fault) -> None:
        async def hog() -> None:
            conn = await self.idle.get()
            try:
                await asyncio.Event().wait()
            finally:
                self.idle.put_nowait(conn)

        n = max(1, round(len(self.all) * fault.req.magnitude))
        tasks = [asyncio.create_task(hog()) for _ in range(n)]
        fault.cleanup.extend(t.cancel for t in tasks)


@asynccontextmanager
async def lifespan(ctx: ServiceContext) -> AsyncIterator[None]:
    pool = ConnectionPool(ctx)
    pool.open()
    ctx.pool = pool  # type: ignore[attr-defined]
    ctx.telemetry.gauge("bank_db_pool_size", "Database connections in the pool",
                        lambda: len(pool.all))
    ctx.telemetry.gauge("bank_db_pool_in_use", "Database connections in use",
                        lambda: len(pool.all) - pool.idle.qsize())
    ctx.faults.register("db_connection_failure", "refuse", pool.refuse)
    ctx.faults.register("db_connection_failure", "exhaust", pool.exhaust)

    async def can_query() -> bool:
        try:
            await pool.run(lambda c: c.execute("SELECT 1").fetchone())
            return True
        except BankError:
            return False

    async def reconciler() -> None:
        while True:
            await asyncio.sleep(min(5.0, ctx.settings.hold_ttl_s))
            try:
                result = await pool.run(store.reconcile, ctx.settings.hold_ttl_s)
                if any(result.values()):
                    log.info("reconciled %s", result)
            except BankError as exc:
                log.warning("reconcile skipped: %s", exc.message)
            except Exception:
                log.exception("reconcile failed")

    ctx.self_check = can_query
    task = asyncio.create_task(reconciler())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pool.close()


router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal)])


async def db(request: Request, fn: Callable[..., Any], *args: Any) -> Any:
    return await get_ctx(request).pool.run(fn, *args)  # type: ignore[attr-defined]


class OpenTransaction(BaseModel):
    id: str
    customer_id: str
    idempotency_key: str
    fingerprint: str
    kind: Literal["transfer", "payment"]
    source: str
    destination: str
    amount: int = Field(gt=0)


class FinishTransaction(BaseModel):
    status: Literal["completed", "failed"]
    detail: dict = {}


class PlaceHold(BaseModel):
    txn_id: str
    account_id: str
    amount: int = Field(gt=0)


class CaptureHold(BaseModel):
    destination: str


class SavePayment(BaseModel):
    txn_id: str
    customer_id: str
    beneficiary: str
    amount: int = Field(gt=0)
    status: Literal["authorized", "declined", "captured", "voided"]


@router.get("/customers/{customer_id}")
async def customer(customer_id: str, request: Request) -> dict:
    return await db(request, store.get_customer, customer_id)


@router.get("/customers/{customer_id}/credentials")
async def credentials(customer_id: str, request: Request) -> dict:
    return await db(request, store.get_credentials, customer_id)


@router.get("/customers/{customer_id}/accounts")
async def accounts(customer_id: str, request: Request) -> list[dict]:
    return await db(request, store.list_accounts, customer_id)


@router.get("/customers/{customer_id}/velocity")
async def velocity(customer_id: str, request: Request, window_s: float = 60) -> dict:
    return await db(request, store.velocity, customer_id, window_s)


@router.get("/accounts/{account_id}")
async def account(account_id: str, request: Request) -> dict:
    return await db(request, store.get_account, account_id)


@router.post("/transactions")
async def open_transaction(body: OpenTransaction, request: Request) -> dict:
    return await db(request, store.open_transaction, body.model_dump())


@router.get("/transactions/{txn_id}")
async def transaction(txn_id: str, request: Request) -> dict:
    return await db(request, store.get_transaction, txn_id)


@router.post("/transactions/{txn_id}/finish")
async def finish(txn_id: str, body: FinishTransaction, request: Request) -> dict:
    return await db(request, store.finish_transaction, txn_id, body.status, body.detail)


@router.post("/holds")
async def hold(body: PlaceHold, request: Request) -> dict:
    return await db(request, store.place_hold, body.txn_id, body.account_id, body.amount)


@router.post("/holds/{txn_id}/capture")
async def capture(txn_id: str, body: CaptureHold, request: Request) -> dict:
    return await db(request, store.capture_hold, txn_id, body.destination)


@router.post("/holds/{txn_id}/release")
async def release(txn_id: str, request: Request) -> dict:
    return await db(request, store.release_hold, txn_id)


@router.put("/payments")
async def save_payment(body: SavePayment, request: Request) -> dict:
    return await db(request, store.save_payment, body.model_dump())


@router.get("/payments/{txn_id}")
async def payment(txn_id: str, request: Request) -> dict:
    return await db(request, store.get_payment, txn_id)


@router.get("/integrity")
async def integrity(request: Request) -> dict:
    return await db(request, store.integrity)


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("database", settings, lifespan=lifespan,
                        transport_factory=transport_factory)
    app.include_router(router)
    return app
