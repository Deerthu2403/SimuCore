from fastapi import APIRouter, Depends, Response

from app import __version__
from app.api.deps import get_state, get_sync
from app.twin.state import TwinState
from app.twin.sync import TwinSynchronizer

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(response: Response, sync: TwinSynchronizer = Depends(get_sync),
                state: TwinState = Depends(get_state)) -> dict:
    ok = sync.running and state.topology is not None and sync.last_error is None
    if not ok:
        response.status_code = 503
    return {
        "ready": ok,
        "adapter": sync.adapter.name,
        "ticks": sync.ticks,
        "twin_version": state.version,
        "last_error": sync.last_error,
    }
