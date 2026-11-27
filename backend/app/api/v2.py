"""Digital twin API, version 2. Read-only."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, WebSocket
from fastapi import WebSocketDisconnect as Disconnect

from app.digital_twin import schemas as v
from app.digital_twin.history import HistoryUnavailable, TwinHistory
from app.digital_twin.hub import MAX_CLIENTS, Client
from app.digital_twin.model import ComponentType, LinkKind, Status
from app.digital_twin.state import DigitalTwin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2/twin", tags=["digital twin v2"])
ws_router = APIRouter()
Id = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,60}$")]
LinkId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,60}->[a-z][a-z0-9-]{1,60}$")]
HEARTBEAT_S = 5.0
SEND_TIMEOUT_S = 10.0


def _now() -> datetime:
    return datetime.now(UTC)


def _twin(request: Request) -> DigitalTwin:
    twin = getattr(request.app.state, "digital_twin", None)
    if twin is None:
        raise HTTPException(503, "The digital twin is disabled")
    return twin


def _history(request: Request) -> TwinHistory:
    _twin(request)
    history = request.app.state.twin_sync.history
    if history is None:
        raise HTTPException(503, "History is disabled: no database is configured")
    return history


def _component(twin: DigitalTwin, component_id: str) -> str:
    if component_id not in twin.components:
        raise HTTPException(404, f"Unknown component {component_id!r}")
    return component_id


def _aware(value: datetime | None, default: datetime) -> datetime:
    if value is None:
        return default
    if value.tzinfo is None:
        raise HTTPException(422, "times must include a time zone, e.g. 2026-01-31T12:00:00Z")
    return value


def _period(start: datetime | None, end: datetime | None, minutes: int) -> tuple:
    until = _aware(end, _now())
    since = _aware(start, until - timedelta(minutes=minutes))
    if since >= until:
        raise HTTPException(422, "'start' must be before 'end'")
    return since, until


async def _read(call) -> Any:
    try:
        return await call
    except HistoryUnavailable as exc:
        raise HTTPException(503, f"History is unavailable: {exc}") from exc


# ---------------------------------------------------------------------- current state
@router.get("", response_model=v.TwinView)
async def complete_twin(request: Request) -> v.TwinView:
    """The complete digital twin: every component and link with status, metrics, dependencies."""
    return v.twin_view(_twin(request), _now())


@router.get("/summary", response_model=v.Summary)
async def summary(request: Request) -> v.Summary:
    return v.summary(_twin(request))


@router.get("/catalogue")
async def catalogue(request: Request) -> dict:
    """Statuses and their meaning, types, layers, units."""
    _twin(request)
    return v.catalogue()


@router.get("/sync")
async def sync_status(request: Request) -> dict:
    """How well the twin is synchronised."""
    _twin(request)
    insights = getattr(request.app.state, "insight_sync", None)
    return {**request.app.state.twin_sync.status(),
            "ai": insights.status() if insights else None}


@router.get("/insights", response_model=dict[str, v.InsightView])
async def insights(request: Request) -> dict[str, v.InsightView]:
    """What the AI service thinks about each component: anomalies (Isolation Forest) and predicted
    degradation or failure (XGBoost). Opinions, not measurements; they never change a status."""
    twin = _twin(request)
    views = {c: v.insight_view(twin, c) for c in twin.insights}
    return {c: i for c, i in views.items() if i is not None}


@router.get("/components", response_model=list[v.ComponentView])
async def components(request: Request, type: ComponentType | None = None,
                     status: Status | None = None) -> list[v.ComponentView]:
    twin = _twin(request)
    return [v.component_view(twin, c) for c, s in twin.components.items()
            if (type is None or twin.blueprint.components[c].type is type)
            and (status is None or s.status is status)]


@router.get("/components/{component_id}", response_model=v.ComponentView)
async def component(request: Request, component_id: Id) -> v.ComponentView:
    twin = _twin(request)
    return v.component_view(twin, _component(twin, component_id))


@router.get("/components/{component_id}/dependencies")
async def dependencies(request: Request, component_id: Id) -> dict:
    """Everything it depends on and everything that depends on it, directly and indirectly."""
    twin = _twin(request)
    _component(twin, component_id)

    def side(direction: str) -> list[dict]:
        return [{"id": c, "steps": n, "status": twin.components[c].status,
                 "type": twin.blueprint.components[c].type}
                for c, n in twin.reachable(component_id, direction).items()]

    return {"id": component_id, "status": twin.components[component_id].status,
            "root_causes": twin.root_causes(component_id), "upstream": side("upstream"),
            "downstream": side("downstream")}


@router.get("/links", response_model=list[v.LinkView])
async def links(request: Request, kind: LinkKind | None = None,
                status: Status | None = None) -> list[v.LinkView]:
    twin = _twin(request)
    return [v.link_view(twin, k) for k, s in twin.links.items()
            if (kind is None or twin.blueprint.links[k].kind is kind)
            and (status is None or s.status is status)]


@router.get("/links/{link_id}", response_model=v.LinkView)
async def link(request: Request, link_id: LinkId) -> v.LinkView:
    twin = _twin(request)
    if link_id not in twin.links:
        raise HTTPException(404, f"Unknown link {link_id!r}")
    return v.link_view(twin, link_id)


@router.get("/structure")
async def structure(request: Request, response: Response) -> Any:
    """Only what exists and how it connects. Rarely changes: supports If-None-Match."""
    twin = _twin(request)
    etag = f'"{twin.blueprint.checksum[:32]}"'
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return {"structure_version": twin.structure_version, "checksum": twin.blueprint.checksum,
            "layers": v.LAYERS,
            "components": [{"id": c.id, "type": c.type, "name": c.name, "layer": c.layer,
                            "group": c.group, "criticality": c.criticality}
                           for c in twin.blueprint.components.values()],
            "links": [{"id": k.id, "source": k.source, "target": k.target, "kind": k.kind,
                       "mode": k.mode, "weight": k.weight}
                      for k in twin.blueprint.links.values()]}


# --------------------------------------------------------------------------- history
@router.get("/history/transitions")
async def transitions(request: Request, object_id: str | None = Query(None, max_length=130),
                      kind: Literal["component", "link"] | None = None,
                      start: datetime | None = None, end: datetime | None = None,
                      minutes: int = Query(60, ge=1, le=60 * 24 * 31),
                      limit: int = Query(200, ge=1, le=2000)) -> list[dict]:
    """Every change of status, newest first."""
    since, until = _period(start, end, minutes)
    return await _read(_history(request).transitions(since, until, object_id, kind, limit))


@router.get("/history/state")
async def state_at(request: Request, at: datetime) -> dict:
    """The complete twin as it was at one moment, with the metrics measured then."""
    moment = _aware(at, _now())
    if moment > _now():
        raise HTTPException(422, "'at' is in the future")
    found = await _read(_history(request).state_at(moment))
    if found is None:
        raise HTTPException(404, "The twin has no record of that moment")
    return found


@router.get("/history/timeline/{component_id}")
async def timeline(request: Request, component_id: Id, start: datetime | None = None,
                   end: datetime | None = None,
                   minutes: int = Query(60, ge=1, le=60 * 24 * 31)) -> dict:
    """Intervals of unchanged status, and the share of time spent in each."""
    twin = _twin(request)
    _component(twin, component_id)
    since, until = _period(start, end, minutes)
    intervals = await _read(_history(request).timeline(component_id, since, until))
    total = sum(x["seconds"] for x in intervals) or 1.0
    share: dict[str, float] = {}
    for x in intervals:
        share[x["status"]] = share.get(x["status"], 0.0) + x["seconds"] / total
    return {"id": component_id, "start": since, "end": until, "intervals": intervals,
            "share": {k: round(s, 4) for k, s in share.items()}}


# ------------------------------------------------------------------------- websocket
@ws_router.websocket("/ws/v2/twin")
async def stream(ws: WebSocket) -> None:
    """Snapshot first, then a delta for every change. Optional requests from the client:

    {"type": "resync"}                                    send a complete snapshot again
    {"type": "subscribe", "components": ["bank-account"]} only these (and links touching them);
                                                          null for everything
    """
    twin: DigitalTwin | None = getattr(ws.app.state, "digital_twin", None)
    hub = getattr(ws.app.state, "twin_hub", None)
    origin = ws.headers.get("origin")
    if twin is None or hub is None:
        await ws.close(1013)
        return
    if origin is not None and origin not in ws.app.state.settings.cors_origins:
        await ws.close(1008)  # another web page must not read this stream
        return
    if len(hub.clients) >= MAX_CLIENTS:
        await ws.close(1013)
        return
    await ws.accept()
    with hub.join() as client:
        async def sender(scope: anyio.CancelScope) -> None:
            try:
                while True:
                    message = _message(twin, client, await client.ready(HEARTBEAT_S))
                    with anyio.fail_after(SEND_TIMEOUT_S):
                        await ws.send_text(message.model_dump_json(exclude_none=False))
            except (Disconnect, RuntimeError, TimeoutError):
                scope.cancel()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(sender, tasks.cancel_scope)
            try:
                while True:
                    _request(await ws.receive_text(), twin, client)
            except (ValueError, KeyError) as exc:
                try:
                    await ws.close(1003, str(exc)[:100])
                except (RuntimeError, Disconnect):
                    pass
            except Disconnect:
                pass
            tasks.cancel_scope.cancel()


def _message(twin: DigitalTwin, client: Client, ready: bool) -> v.TwinMessage:
    now = _now()
    base = client.sent_version
    client.sent_version = twin.version
    if not ready:
        return v.TwinMessage(type="heartbeat", base_version=base, meta=v.meta(twin, now))
    full, components, links, transitions = client.take()
    if full:
        components, links = set(twin.components), set(twin.links)
    keep = client.wanted
    cs = [c for c in twin.components if c in components and (keep is None or c in keep)]
    ks = [k for k in twin.links if k in links and (
        keep is None or {twin.blueprint.links[k].source, twin.blueprint.links[k].target} & keep)]
    return v.TwinMessage(
        type="snapshot" if full else "delta", base_version=None if full else base,
        meta=v.meta(twin, now), summary=v.summary(twin),
        components=[v.component_view(twin, c) for c in cs],
        links=[v.link_view(twin, k) for k in ks],
        transitions=[] if full else [v.transition_view(t) for t in transitions
                                     if keep is None or t.object_id in keep])


def _request(text: str, twin: DigitalTwin, client: Client) -> None:
    if len(text) > 4000:
        raise ValueError("message too long")
    body = json.loads(text)
    kind = body.get("type") if isinstance(body, dict) else None
    if kind == "resync":
        client.resync()
    elif kind == "subscribe":
        wanted = body["components"]
        if wanted is not None and (not isinstance(wanted, list)
                                   or any(w not in twin.components for w in wanted)):
            raise ValueError("unknown component in subscription")
        client.wanted = None if wanted is None else frozenset(wanted)
        client.resync()
    else:
        raise ValueError("expected 'resync' or 'subscribe'")
