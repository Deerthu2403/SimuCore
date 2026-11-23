"""Account Management Service: balances for customers; holds and postings for transactions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Path, Request
from pydantic import BaseModel, Field

from bankcommon.app import build_service, cid, current_customer, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError

router = APIRouter()
internal = APIRouter(prefix="/internal", dependencies=[Depends(require_internal)])
ACCOUNT_ID = r"^SIM-[A-Z0-9-]{1,20}$"


class Hold(BaseModel):
    txn_id: str = Field(max_length=64)
    account_id: str = Field(pattern=ACCOUNT_ID)
    amount: int = Field(gt=0)


class Capture(BaseModel):
    destination: str = Field(pattern=ACCOUNT_ID)


async def _db(request: Request, method: str, path: str, json: Any = None) -> Any:
    r = await get_ctx(request).client.call("database", method, f"/internal{path}", json=json,
                                           correlation_id=cid(request))
    if r.status_code >= 400:
        err = r.json()["error"]
        raise BankError(r.status_code, err["code"], err["message"])
    return r.json()


def _view(a: dict) -> dict:
    return {"id": a["id"], "kind": a["kind"], "currency": "XTS", "balance": a["balance"],
            "available": a["balance"] - a["held"]}


@router.get("/accounts")
async def my_accounts(request: Request, customer_id: str = Depends(current_customer)) -> list[dict]:
    return [_view(a) for a in await _db(request, "GET", f"/customers/{customer_id}/accounts")]


@router.get("/accounts/{account_id}")
async def my_account(request: Request, account_id: str = Path(pattern=ACCOUNT_ID),
                     customer_id: str = Depends(current_customer)) -> dict:
    acct = await _db(request, "GET", f"/accounts/{account_id}")
    if acct["customer_id"] != customer_id:  # same answer as a missing account
        raise BankError(404, "NOT_FOUND", "account not found")
    return _view(acct)


@internal.get("/accounts/{account_id}")
async def lookup(request: Request, account_id: str = Path(pattern=ACCOUNT_ID)) -> dict:
    acct = await _db(request, "GET", f"/accounts/{account_id}")
    return {**_view(acct), "customer_id": acct["customer_id"]}


@internal.post("/holds")
async def hold(body: Hold, request: Request) -> dict:
    return await _db(request, "POST", "/holds", body.model_dump())


@internal.post("/holds/{txn_id}/capture")
async def capture(txn_id: str, body: Capture, request: Request) -> dict:
    return await _db(request, "POST", f"/holds/{txn_id}/capture", body.model_dump())


@internal.post("/holds/{txn_id}/release")
async def release(txn_id: str, request: Request) -> dict:
    return await _db(request, "POST", f"/holds/{txn_id}/release")


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("account", settings, transport_factory=transport_factory)
    app.include_router(router)
    app.include_router(internal)
    return app
