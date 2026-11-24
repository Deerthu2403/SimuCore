"""WebSocket fan-out. A slow client loses old updates; it never slows the collector or others."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

MAX_CLIENTS = 200
QUEUE = 10


class Client:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(QUEUE)
        self.services: set[str] | None = None  # None = all
        self.skipped = 0

    def offer(self, message: dict) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.skipped += 1
        self.queue.put_nowait(message)

    def filtered(self, message: dict) -> dict:
        if self.services is None or "services" not in message:
            return message
        return {**message, "services": [s for s in message["services"]
                                        if s["service"] in self.services]}


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()

    def publish(self, message: dict) -> None:
        for c in self.clients:
            c.offer(message)

    @contextmanager
    def join(self):
        client = Client()
        self.clients.add(client)
        try:
            yield client
        finally:
            self.clients.discard(client)
