"""WebSocket gateway: streams the twin to the 3D UI."""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.events import Topics
from app.domain.contracts import TwinDelta

router = APIRouter()
log = logging.getLogger(__name__)


@router.websocket("/ws/twin")
async def twin_stream(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    state = app.state.twin_state
    with app.state.bus.subscribe(Topics.TWIN_DELTA) as queue:
        await ws.send_json({
            "type": "snapshot",
            "version": state.version,
            "source": app.state.sync.adapter.name,
            "origin": app.state.sync.adapter.origin,
            "topology": state.topology.model_dump(mode="json") if state.topology else None,
            "states": [s.model_dump(mode="json") for s in state.states.values()],
        })
        try:
            while True:
                delta: TwinDelta = await queue.get()
                await ws.send_json({"type": "delta", **delta.model_dump(mode="json")})
        except WebSocketDisconnect:
            log.debug("WS client disconnected")
