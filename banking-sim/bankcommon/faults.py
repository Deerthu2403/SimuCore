"""Fault injection. Every fault is bounded and expires on its own.

  downtime             the service answers 503 to everything, health checks included
  latency              adds latency_ms (+/- jitter) to every business request
  resource_exhaustion  mode=cpu     real CPU burn inside the process, graded by magnitude
                       mode=memory  real allocation up to the memory budget; requests are shed
                       mode=slots   occupies concurrency slots so requests queue and are shed
  db_connection_failure (database service only)
                       mode=refuse  connections fail at once
                       mode=exhaust holds real pool connections so acquires time out

A hard crash is not a fault type: use ``bankctl kill``, which kills the process.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from bankcommon.errors import BankError

MAX_DURATION_S = 600
MAX_LATENCY_MS = 10_000
MAX_CPU_THREADS = 4
CPU_SLICE_S = 0.1


class FaultRequest(BaseModel):
    type: Literal["downtime", "latency", "resource_exhaustion", "db_connection_failure"]
    mode: str | None = None
    duration_s: float = Field(gt=0, le=MAX_DURATION_S)
    latency_ms: float = Field(default=0, ge=0, le=MAX_LATENCY_MS)
    jitter: float = Field(default=0.2, ge=0, le=1)
    magnitude: float = Field(default=1.0, gt=0, le=1)


@dataclass
class Fault:
    req: FaultRequest
    expires_at: float
    cleanup: list[Callable[[], Any]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None

    def describe(self) -> dict:
        return {**self.req.model_dump(),
                "remaining_s": round(max(0.0, self.expires_at - time.monotonic()), 1)}


Hook = Callable[[Fault], Awaitable[Any]]


class FaultEngine:
    def __init__(self, enabled: bool = True, seed: int = 42) -> None:
        self.enabled = enabled
        self.rng = random.Random(seed)
        self._active: dict[str, Fault] = {}
        # Fault types that take a mode, and the hook that applies each one. Plain types
        # (downtime, latency) have no hook: the middleware reads them directly.
        self._hooks: dict[str, dict[str, Hook]] = {"resource_exhaustion": {"cpu": self._burn_cpu}}

    def register(self, type_: str, mode: str, hook: Hook) -> None:
        self._hooks.setdefault(type_, {})[mode] = hook

    def get(self, type_: str) -> Fault | None:
        f = self._active.get(type_)
        return f if f and time.monotonic() < f.expires_at else None

    async def inject(self, req: FaultRequest) -> Fault:
        if not self.enabled:
            raise BankError(403, "FAULTS_DISABLED", "Fault injection is disabled")
        hooks = self._hooks.get(req.type, {})
        if req.type == "db_connection_failure" and not hooks:
            raise BankError(400, "UNSUPPORTED_FAULT", "Only the database service has connections")
        if hooks and req.mode not in hooks:
            raise BankError(400, "UNSUPPORTED_FAULT", f"mode must be one of {sorted(hooks)}")
        self.clear(req.type)
        fault = Fault(req, time.monotonic() + req.duration_s)
        self._active[req.type] = fault
        fault.timer = asyncio.get_running_loop().call_later(
            req.duration_s, self._end, req.type, fault)
        if hooks:
            try:
                await hooks[req.mode or ""](fault)
            except BaseException:
                self._end(req.type, fault)
                raise
        return fault

    def clear(self, type_: str | None = None) -> None:
        for t in [type_] if type_ else list(self._active):
            if f := self._active.get(t):
                self._end(t, f)

    def _end(self, type_: str, fault: Fault) -> None:
        if fault.timer:
            fault.timer.cancel()
        if self._active.get(type_) is fault:
            del self._active[type_]
        fault.expires_at = 0.0
        while fault.cleanup:
            fault.cleanup.pop()()

    def describe(self) -> list[dict]:
        return [f.describe() for t, f in self._active.items() if self.get(t)]

    async def delay(self) -> None:
        if f := self.get("latency"):
            spread = f.req.jitter
            ms = f.req.latency_ms * self.rng.uniform(1 - spread, 1 + spread)
            await asyncio.sleep(ms / 1000)

    async def _burn_cpu(self, fault: Fault) -> None:
        stop = threading.Event()
        fault.cleanup.append(stop.set)

        # Magnitude sets both the thread count and each thread's duty cycle: one spinning
        # thread alone starves the event loop, because it competes for the interpreter lock.
        duty = fault.req.magnitude

        def spin() -> None:
            x = 0
            while not stop.is_set() and time.monotonic() < fault.expires_at:
                # Long slices: Windows cannot sleep for less than about 15 ms, so short ones
                # would make the real duty cycle much lower there than on Linux.
                until = time.perf_counter() + CPU_SLICE_S * duty
                while time.perf_counter() < until:
                    for i in range(5_000):
                        x = (x * 31 + i) & 0xFFFFFFFF
                if duty < 1:
                    stop.wait(CPU_SLICE_S * (1 - duty))

        for _ in range(max(1, round(MAX_CPU_THREADS * duty))):
            threading.Thread(target=spin, daemon=True, name="fault-cpu").start()
