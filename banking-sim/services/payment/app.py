"""Payment Gateway Service: authorise, capture and void payments to synthetic beneficiaries.

The external card/clearing network is simulated in-process. Nothing leaves the sandbox.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from bankcommon.app import build_service, cid, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError, DependencyUnavailable

log = logging.getLogger("bank.payment")
router = APIRouter(prefix="/internal/payments", dependencies=[Depends(require_internal)])


class Authorize(BaseModel):
    txn_id: str = Field(max_length=64)
    customer_id: str = Field(max_length=32)
    beneficiary: str = Field(pattern=r"^EXT-[A-Z0-9-]{1,40}$")
    amount: int = Field(gt=0)


async def _save(request: Request, payment: dict, status: str) -> dict:
    keys = ("txn_id", "customer_id", "beneficiary", "amount")
    r = await get_ctx(request).client.call(
        "database", "PUT", "/internal/payments", correlation_id=cid(request),
        json={**{k: payment[k] for k in keys}, "status": status})
    if r.status_code != 200:
        raise BankError(502, "PAYMENT_STORE_REJECTED", "payment could not be stored")
    return r.json()


async def _load(request: Request, txn_id: str) -> dict:
    r = await get_ctx(request).client.call("database", "GET", f"/internal/payments/{txn_id}",
                                           correlation_id=cid(request))
    if r.status_code != 200:
        raise BankError(404, "NOT_FOUND", "payment not found")
    return r.json()


async def _notify(request: Request, payment: dict, event: str) -> str:
    """Best effort: a missing receipt must never fail a payment."""
    try:
        r = await get_ctx(request).client.call(
            "notification", "POST", "/internal/notifications", correlation_id=cid(request),
            json={"event_id": f"{payment['txn_id']}:{event}", "event": event,
                  "customer_id": payment["customer_id"], "amount": payment["amount"]})
        return "queued" if r.status_code == 202 else "rejected"
    except DependencyUnavailable as exc:
        log.warning("receipt not sent for %s: %s", payment["txn_id"], exc.message)
        return "unavailable"


@router.post("/authorize")
async def authorize(body: Authorize, request: Request) -> dict:
    outcome = "declined" if body.beneficiary.startswith("EXT-DECLINE") else "authorized"
    return await _save(request, body.model_dump(), outcome)


@router.post("/{txn_id}/capture")
async def capture(txn_id: str, request: Request) -> dict:
    payment = await _save(request, await _load(request, txn_id), "captured")
    if payment["status"] != "captured":
        raise BankError(409, "NOT_CAPTURABLE", f"payment is {payment['status']}")
    return {**payment, "receipt": await _notify(request, payment, "payment.captured")}


@router.post("/{txn_id}/void")
async def void(txn_id: str, request: Request) -> dict:
    return await _save(request, await _load(request, txn_id), "voided")


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("payment", settings, transport_factory=transport_factory)
    app.include_router(router)
    return app
