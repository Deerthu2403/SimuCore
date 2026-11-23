"""Transaction Processing Service: orchestrates transfers and external payments.

Steps: verify token -> open transaction (idempotent) -> check ownership -> fraud decision ->
hold funds -> [authorise payment] -> CAPTURE (commit point) -> [settle payment].

Every step is idempotent by transaction id, so retrying with the same Idempotency-Key is always
safe. If anything fails before the commit point, the hold is released. When the outcome of the
capture is unknown, the release call itself reports whether money actually moved.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bankcommon.app import build_service, cid, current_customer, get_ctx
from bankcommon.config import Settings
from bankcommon.errors import BankError, DependencyUnavailable

log = logging.getLogger("bank.transaction")
router = APIRouter()
CLEARING = "SIM-CLEARING"
MAX_AMOUNT = 10_000_000
KEY = r"^[A-Za-z0-9_-]{8,64}$"
CUSTOMER_ACCOUNT = r"^SIM-\d{5}-\d{2}$"


class Transfer(BaseModel):
    source: str = Field(pattern=CUSTOMER_ACCOUNT)
    destination: str = Field(pattern=CUSTOMER_ACCOUNT)
    amount: int = Field(gt=0, le=MAX_AMOUNT)


class Payment(BaseModel):
    source: str = Field(pattern=CUSTOMER_ACCOUNT)
    beneficiary: str = Field(pattern=r"^EXT-[A-Z0-9-]{1,40}$")
    amount: int = Field(gt=0, le=MAX_AMOUNT)


class Flow:
    """One transaction's journey through its dependencies."""

    def __init__(self, request: Request, customer_id: str, kind: str, source: str,
                 destination: str, amount: int, key: str) -> None:
        self.request, self.ctx = request, get_ctx(request)
        self.customer_id, self.kind, self.key = customer_id, kind, key
        self.source, self.destination, self.amount = source, destination, amount
        self.id = f"TXN-{uuid.uuid4().hex}"
        self.detail: dict[str, Any] = {}

    async def call(self, target: str, method: str, path: str, json: Any = None):
        return await self.ctx.client.call(target, method, f"/internal{path}", json=json,
                                          correlation_id=cid(self.request))

    async def run(self) -> JSONResponse:
        existing = await self._open()
        if existing:
            return existing
        try:
            await self._check_accounts()
            await self._screen()
            await self._hold()
            if self.kind == "payment":
                await self._authorize()
            await self._capture()
        except BankError as exc:
            if not await self._abort(exc):
                raise
            # else: the capture did commit even though its response was lost
        if self.kind == "payment":
            await self._settle()
        return await self._complete()

    # ------------------------------------------------------------------- steps
    async def _open(self) -> JSONResponse | None:
        terms = f"{self.kind}|{self.source}|{self.destination}|{self.amount}"
        fingerprint = hashlib.sha256(terms.encode()).hexdigest()
        r = await self.call("database", "POST", "/transactions", {
            "id": self.id, "customer_id": self.customer_id, "idempotency_key": self.key,
            "fingerprint": fingerprint, "kind": self.kind, "source": self.source,
            "destination": self.destination, "amount": self.amount})
        if r.status_code != 200:
            raise BankError(502, "TRANSACTION_STORE_REJECTED", "could not open transaction")
        txn = r.json()["transaction"]
        if txn["fingerprint"] != fingerprint:
            raise BankError(409, "IDEMPOTENCY_CONFLICT", "key was used for a different request")
        self.id = txn["id"]  # resume an earlier, unfinished attempt
        if txn["status"] == "completed":
            return self._response(txn, replayed=True)
        if txn["status"] == "failed":
            raise BankError(409, "ALREADY_FAILED",
                            f"transaction failed earlier: {txn['detail'].get('code', 'unknown')}")
        return None

    async def _check_accounts(self) -> None:
        r = await self.call("account", "GET", f"/accounts/{self.source}")
        if r.status_code != 200 or r.json()["customer_id"] != self.customer_id:
            raise BankError(404, "NOT_FOUND", "source account not found")
        if self.kind == "transfer":
            if self.destination == self.source:
                raise BankError(422, "SAME_ACCOUNT", "source and destination are the same")
            r = await self.call("account", "GET", f"/accounts/{self.destination}")
            if r.status_code != 200:
                raise BankError(404, "NOT_FOUND", "destination account not found")

    async def _screen(self) -> None:
        try:
            r = await self.ctx.client.call(
                "fraud", "POST", "/fraud/checks", correlation_id=cid(self.request),
                json={"customer_id": self.customer_id, "amount": self.amount,
                      "destination": self.destination})
        except DependencyUnavailable:
            if self.amount > self.ctx.settings.fraud_fallback_limit:
                raise
            self.detail["fraud"] = "skipped_unavailable"
            return
        if r.status_code != 200:
            raise BankError(502, "FRAUD_CHECK_INVALID", "fraud service rejected the request")
        result = r.json()
        self.detail["fraud"] = result["decision"]
        if result["decision"] != "allow":
            raise BankError(403, f"FRAUD_{result['decision'].upper()}", ",".join(result["reasons"]))

    async def _hold(self) -> None:
        self._raise_for(await self.call("account", "POST", "/holds", {
            "txn_id": self.id, "account_id": self.source, "amount": self.amount}))

    async def _authorize(self) -> None:
        r = await self.call("payment", "POST", "/payments/authorize", {
            "txn_id": self.id, "customer_id": self.customer_id,
            "beneficiary": self.destination, "amount": self.amount})
        self._raise_for(r)
        if r.json()["status"] != "authorized":
            raise BankError(402, "PAYMENT_DECLINED", f"payment is {r.json()['status']}")

    async def _capture(self) -> None:
        target = CLEARING if self.kind == "payment" else self.destination
        self._raise_for(await self.call("account", "POST", f"/holds/{self.id}/capture",
                                        {"destination": target}))

    async def _settle(self) -> None:
        """After the commit point: money has moved, so a failure here only delays settlement."""
        try:
            r = await self.call("payment", "POST", f"/payments/{self.id}/capture")
            self.detail["settlement"] = "settled" if r.status_code == 200 else "pending"
            if r.status_code == 200:
                self.detail["receipt"] = r.json()["receipt"]
        except DependencyUnavailable:
            self.detail["settlement"] = "pending"

    async def _complete(self) -> JSONResponse:
        try:
            r = await self.call("database", "POST", f"/transactions/{self.id}/finish",
                                {"status": "completed", "detail": self.detail})
            if r.status_code == 200:
                return self._response(r.json())
        except DependencyUnavailable:
            pass
        # Committed in the ledger; the reconciler will mark the record.
        return JSONResponse({"id": self.id, "status": "completed", "amount": self.amount,
                             "detail": {**self.detail, "record": "pending"}}, 201)

    # ----------------------------------------------------------------- failure
    async def _abort(self, cause: BankError) -> bool:
        """Undo. Returns True if it turns out the money had already moved."""
        try:
            r = await self.call("account", "POST", f"/holds/{self.id}/release")
            if r.status_code == 200 and r.json()["status"] == "captured":
                return True
            if r.status_code == 200:
                await self.call("database", "POST", f"/transactions/{self.id}/finish", {
                    "status": "failed", "detail": {**self.detail, "code": cause.code}})
        except DependencyUnavailable as exc:
            # Record stays "processing": a retry resumes it, else the reconciler closes it.
            log.warning("%s left for reconciliation: %s", self.id, exc.message)
        if self.kind == "payment":
            try:
                await self.call("payment", "POST", f"/payments/{self.id}/void")
            except DependencyUnavailable:
                log.warning("void not delivered for %s", self.id)
        return False

    def _raise_for(self, r) -> None:
        if r.status_code >= 400:
            err = r.json()["error"]
            raise BankError(r.status_code, err["code"], err["message"])

    def _response(self, txn: dict, replayed: bool = False) -> JSONResponse:
        return JSONResponse({"id": txn["id"], "status": txn["status"], "amount": txn["amount"],
                             "detail": txn["detail"], "replayed": replayed},
                            200 if replayed else 201)


@router.post("/transfers", status_code=201)
async def transfer(body: Transfer, request: Request, customer_id: str = Depends(current_customer),
                   idempotency_key: str = Header(pattern=KEY)) -> JSONResponse:
    return await Flow(request, customer_id, "transfer", body.source, body.destination,
                      body.amount, idempotency_key).run()


@router.post("/payments", status_code=201)
async def payment(body: Payment, request: Request, customer_id: str = Depends(current_customer),
                  idempotency_key: str = Header(pattern=KEY)) -> JSONResponse:
    return await Flow(request, customer_id, "payment", body.source, body.beneficiary,
                      body.amount, idempotency_key).run()


@router.get("/transactions/{txn_id}")
async def show(request: Request, txn_id: str = Path(pattern=r"^TXN-[0-9a-f]{32}$"),
               customer_id: str = Depends(current_customer)) -> dict:
    r = await get_ctx(request).client.call("database", "GET", f"/internal/transactions/{txn_id}",
                                           correlation_id=cid(request))
    if r.status_code != 200 or r.json()["customer_id"] != customer_id:
        raise BankError(404, "NOT_FOUND", "transaction not found")
    t = r.json()
    return {k: t[k] for k in ("id", "kind", "source", "destination", "amount", "status", "detail")}


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("transaction", settings, transport_factory=transport_factory)
    app.include_router(router)
    return app
