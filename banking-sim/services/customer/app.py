"""Customer Management Service: synthetic customer profiles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Path, Request

from bankcommon.app import build_service, cid, current_customer, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError

router = APIRouter()


async def _profile(request: Request, customer_id: str) -> dict:
    r = await get_ctx(request).client.call("database", "GET", f"/internal/customers/{customer_id}",
                                           correlation_id=cid(request))
    if r.status_code != 200:
        raise BankError(404, "NOT_FOUND", "customer not found")
    return r.json()


@router.get("/customers/me")
async def me(request: Request, customer_id: str = Depends(current_customer)) -> dict:
    return await _profile(request, customer_id)


@router.get("/internal/customers/{customer_id}", dependencies=[Depends(require_internal)])
async def lookup(request: Request,
                 customer_id: str = Path(pattern=r"^CUST\d{5}$")) -> dict:
    return await _profile(request, customer_id)


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("customer", settings, transport_factory=transport_factory)
    app.include_router(router)
    return app
