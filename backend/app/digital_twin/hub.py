"""WebSocket fan-out for twin updates.

A state stream must not lose anything, so nothing is queued and dropped. Each client only remembers
*which* objects changed since its last message; when it is ready, it gets their current state. A
slow client therefore gets fewer, larger messages, never wrong ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from app.digital_twin.model import Change, Transition

MAX_CLIENTS = 200
MAX_TRANSITIONS = 500


class Client:
    def __init__(self) -> None:
        self.components: set[str] = set()
        self.links: set[str] = set()
        self.transitions: list[Transition] = []
        self.full = True  # needs a complete snapshot
        self.wanted: frozenset[str] | None = None  # None = all components
        self.sent_version = -1
        self._event = asyncio.Event()
        self._event.set()

    def offer(self, change: Change) -> None:
        if change.structure_changed:
            self.full = True
        self.components |= change.components
        self.links |= change.links
        self.transitions += change.transitions
        if len(self.transitions) > MAX_TRANSITIONS:
            self.full, self.transitions = True, []
        self._event.set()

    def resync(self) -> None:
        self.full = True
        self._event.set()

    async def ready(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await self._event.wait()
            return True
        except TimeoutError:
            return False

    def take(self) -> tuple[bool, set[str], set[str], list[Transition]]:
        """What to send now. Clears it."""
        self._event.clear()
        taken = (self.full, self.components, self.links, self.transitions)
        self.full, self.components, self.links, self.transitions = False, set(), set(), []
        return taken


class TwinHub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()

    def publish(self, change: Change) -> None:
        if change:
            for c in self.clients:
                c.offer(change)

    @contextmanager
    def join(self) -> Iterator[Client]:
        client = Client()
        self.clients.add(client)
        try:
            yield client
        finally:
            self.clients.discard(client)
