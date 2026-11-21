"""In-process asynchronous event bus.

Modules communicate by publishing typed contracts to topics. The interface is
deliberately small so it can later be backed by Redis Streams or NATS without
changing producers/consumers (ADR-001).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)


class Topics:
    TWIN_DELTA = "twin.delta"
    TWIN_TOPOLOGY = "twin.topology"
    ANOMALY_DETECTED = "anomaly.detected"
    PREDICTION_CREATED = "prediction.created"
    PROPAGATION_COMPLETED = "propagation.completed"
    SIMULATION_COMPLETED = "simulation.completed"
    OPTIMIZATION_COMPLETED = "optimization.completed"
    CALIBRATION_UPDATED = "calibration.updated"
    RECOMMENDATION_CREATED = "recommendation.created"


class EventBus:
    """Fan-out pub/sub. Each subscriber gets its own bounded queue.

    Slow subscribers never block producers: when a queue is full the oldest
    message is dropped (twin deltas are superseded by newer ones anyway).
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._subs: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)
        self._max_queue = max_queue

    async def publish(self, topic: str, payload: Any) -> None:
        for q in list(self._subs.get(topic, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race
                    pass
            q.put_nowait(payload)

    @contextmanager
    def subscribe(self, topic: str) -> Iterator[asyncio.Queue[Any]]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._max_queue)
        self._subs[topic].add(q)
        try:
            yield q
        finally:
            self._subs[topic].discard(q)

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, ()))
