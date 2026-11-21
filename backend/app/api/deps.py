"""FastAPI dependencies giving routes access to runtime singletons."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.events import EventBus
from app.domain.topology import TopologyGraph
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer


def get_state(request: Request) -> TwinState:
    return request.app.state.twin_state


def get_graph(request: Request) -> TopologyGraph:
    graph = request.app.state.twin_state.graph
    if graph is None:
        raise HTTPException(503, "Twin topology not loaded yet")
    return graph


def get_sync(request: Request) -> TwinSynchronizer:
    return request.app.state.sync


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus
