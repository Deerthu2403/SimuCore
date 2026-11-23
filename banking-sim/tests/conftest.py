"""Runs all eight real applications in one process, wired together over in-memory HTTP."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest

from bankcommon.config import Settings, registry

KEY = "unit-test-internal-key"


class Wire(httpx.AsyncBaseTransport):
    """Transport to one service. Tests can take it down or intercept individual calls."""

    def __init__(self, cluster: Cluster, target: str) -> None:
        self.cluster, self.target = cluster, target

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.target in self.cluster.down:
            raise httpx.ConnectError("refused", request=request)
        app = self.cluster.apps[self.target]
        inner = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        response = await inner.handle_async_request(request)
        if self.cluster.after_response:
            self.cluster.after_response(self.target, request)
        return response


class Cluster:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.down: set[str] = set()
        self.after_response: Callable[[str, httpx.Request], None] | None = None
        self.apps = {
            name: importlib.import_module(f"services.{name}.app").create_app(
                settings, transport_factory=lambda t: Wire(self, t))
            for name in registry().services
        }
        self._running: list = []
        self.clients = {n: httpx.AsyncClient(transport=Wire(self, n), base_url=f"http://{n}",
                                             timeout=10) for n in self.apps}

    async def __aenter__(self) -> Cluster:
        for app in self.apps.values():
            ctx = app.router.lifespan_context(app)
            await ctx.__aenter__()
            self._running.append(ctx)
        return self

    async def __aexit__(self, *exc) -> None:
        for c in self.clients.values():
            await c.aclose()
        for ctx in reversed(self._running):
            await ctx.__aexit__(None, None, None)

    # ----------------------------------------------------------------- helpers
    def ctx(self, name: str):
        return self.apps[name].state.ctx

    async def http(self, service: str, method: str, path: str, *, internal: bool = False,
                   token: str | None = None, key: str | None = None, **kw) -> httpx.Response:
        headers = {}
        if internal:
            headers["X-Internal-Key"] = KEY
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        return await self.clients[service].request(method, path, headers=headers, **kw)

    async def token(self, n: int = 1) -> str:
        c = f"CUST{n:05d}"
        r = await self.http("authentication", "POST", "/login",
                            json={"customer_id": c, "secret": f"synthetic-{c}"})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    async def transfer(self, token: str, amount: int = 1000, *, key: str | None = None,
                       source: str = "SIM-00001-01",
                       destination: str = "SIM-00002-01") -> httpx.Response:
        return await self.http("transaction", "POST", "/transfers", token=token,
                               key=key or uuid.uuid4().hex,
                               json={"source": source, "destination": destination,
                                     "amount": amount})

    async def pay(self, token: str, amount: int = 1000, *, beneficiary: str = "EXT-SHOP-1",
                  key: str | None = None) -> httpx.Response:
        return await self.http("transaction", "POST", "/payments", token=token,
                               key=key or uuid.uuid4().hex,
                               json={"source": "SIM-00001-01", "beneficiary": beneficiary,
                                     "amount": amount})

    async def account(self, account_id: str = "SIM-00001-01") -> dict:
        r = await self.http("database", "GET", f"/internal/accounts/{account_id}", internal=True)
        return r.json()

    async def fault(self, service: str, type_: str, **kw) -> httpx.Response:
        return await self.http(service, "POST", "/control/faults", internal=True,
                               json={"type": type_, "duration_s": 30, **kw})

    async def clear(self, service: str) -> None:
        await self.http(service, "DELETE", "/control/faults", internal=True)

    async def audit(self) -> dict:
        r = await self.http("database", "GET", "/internal/integrity", internal=True)
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["ok"], report
        return report


@pytest.fixture
def settings(tmp_path) -> Settings:
    # The memory budget is large because all services share the pytest process here.
    return Settings(internal_key=KEY, data_dir=tmp_path, customers=5, mem_limit_mb=4096)


@pytest.fixture
async def bank(settings):
    async with Cluster(settings) as cluster:
        yield cluster


@pytest.fixture
async def fast_expiry_bank(settings):
    async with Cluster(replace(settings, hold_ttl_s=0.3)) as cluster:
        yield cluster
