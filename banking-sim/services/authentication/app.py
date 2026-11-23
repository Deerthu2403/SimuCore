"""Authentication Service: login and token verification for synthetic customers.

Tokens are HMAC-signed with a key that exists only in this process, so a restart invalidates
every session, exactly as losing a session store would. Verification needs no database.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from bankcommon.app import ServiceContext, build_service, cid, get_ctx, require_internal
from bankcommon.config import Settings
from bankcommon.errors import BankError

PBKDF2_ROUNDS = 20_000
router = APIRouter()


class Login(BaseModel):
    customer_id: str = Field(pattern=r"^CUST\d{5}$")
    secret: str = Field(min_length=1, max_length=128)


class Verify(BaseModel):
    token: str = Field(max_length=2048)


def _sign(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue(key: bytes, customer_id: str, ttl_s: int) -> str:
    raw = json.dumps({"sub": customer_id, "exp": time.time() + ttl_s}).encode()
    payload = base64.urlsafe_b64encode(raw)
    return f"{payload.decode()}.{_sign(key, payload)}"


def verify(key: bytes, token: str) -> str | None:
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(_sign(key, payload.encode()).encode(), sig.encode()):
        return None
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return claims["sub"] if claims["exp"] > time.time() else None


@asynccontextmanager
async def lifespan(ctx: ServiceContext) -> AsyncIterator[None]:
    ctx.token_key = secrets.token_bytes(32)  # type: ignore[attr-defined]
    yield


@router.post("/login")
async def login(body: Login, request: Request) -> dict:
    ctx = get_ctx(request)
    r = await ctx.client.call(
        "database", "GET", f"/internal/customers/{body.customer_id}/credentials",
        correlation_id=cid(request))
    # Unknown customers cost the same work and get the same answer as a wrong secret.
    cred = r.json() if r.status_code == 200 else {"salt": "none", "hash": ""}
    digest = await asyncio.to_thread(hashlib.pbkdf2_hmac, "sha256", body.secret.encode(),
                                     cred["salt"].encode(), PBKDF2_ROUNDS)
    if not hmac.compare_digest(digest.hex(), cred["hash"]):
        raise BankError(401, "BAD_CREDENTIALS", "Invalid customer or secret")
    ttl = ctx.settings.token_ttl_s
    return {"token": issue(ctx.token_key, body.customer_id, ttl), "expires_in": ttl}


@router.post("/internal/verify", dependencies=[Depends(require_internal)])
async def verify_token(body: Verify, request: Request) -> dict:
    customer_id = verify(get_ctx(request).token_key, body.token)
    if customer_id is None:
        raise BankError(401, "INVALID_TOKEN", "Invalid or expired token")
    return {"customer_id": customer_id}


def create_app(settings: Settings, transport_factory=None) -> FastAPI:
    app = build_service("authentication", settings, lifespan=lifespan,
                        transport_factory=transport_factory)
    app.include_router(router)
    return app
