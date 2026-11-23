"""Fraud Detection Service: transparent rule-based scoring (ML arrives in a later phase)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from bankcommon.app import build_service, cid, get_ctx, require_internal
from bankcommon.config import Settings

router = APIRouter(dependencies=[Depends(require_internal)])
VELOCITY_WINDOW_S = 60
DENY_AT, REVIEW_AT = 80, 50


class Check(BaseModel):
    customer_id: str = Field(pattern=r"^CUST\d{5}$")
    amount: int = Field(gt=0)
    destination: str = Field(max_length=64)


def score(amount: int, destination: str, count: int, total: int) -> tuple[int, list[str]]:
    points, reasons = 0, []
    if amount >= 2_000_000:
        points, reasons = points + 80, [*reasons, "very_large_amount"]
    elif amount >= 500_000:
        points, reasons = points + 30, [*reasons, "large_amount"]
    if count > 20:
        points, reasons = points + 80, [*reasons, "extreme_velocity"]
    elif count > 8:
        points, reasons = points + 25, [*reasons, "high_velocity"]
    if total >= 3_000_000:
        points, reasons = points + 25, [*reasons, "high_cumulative_amount"]
    if "MULE" in destination:
        points, reasons = points + 80, [*reasons, "watchlisted_destination"]
    return min(points, 100), reasons


@router.post("/fraud/checks")
async def check(body: Check, request: Request) -> dict:
    r = await get_ctx(request).client.call(
        "database", "GET", f"/internal/customers/{body.customer_id}/velocity",
        params={"window_s": VELOCITY_WINDOW_S}, correlation_id=cid(request))
    v = r.json()
    points, reasons = score(body.amount, body.destination, v["count"], v["total"])
    decision = "deny" if points >= DENY_AT else "review" if points >= REVIEW_AT else "allow"
    return {"decision": decision, "score": points, "reasons": reasons}


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("fraud", settings, transport_factory=transport_factory)
    app.include_router(router)
    return app
