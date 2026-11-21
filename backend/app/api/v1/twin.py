from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_graph, get_state
from app.domain.contracts import Explanation, PropagationRequest, PropagationResult
from app.domain.models import Node, NodeState, Topology
from app.domain.topology import TopologyGraph
from app.explain.explainer import explain_propagation
from app.propagation import engine as propagation
from app.twin.state import TwinState

router = APIRouter(tags=["twin"])


class TwinSnapshot(BaseModel):
    version: int
    summary: dict[str, int]
    states: list[NodeState]


class NodeDetail(BaseModel):
    node: Node
    state: NodeState
    dependencies: list[str]
    dependents: list[str]


class PropagationResponse(BaseModel):
    result: PropagationResult
    explanation: Explanation


@router.get("/topology", response_model=Topology)
async def topology(state: TwinState = Depends(get_state)) -> Topology:
    if state.topology is None:
        raise HTTPException(503, "Twin topology not loaded yet")
    return state.topology


@router.get("/twin/state", response_model=TwinSnapshot)
async def twin_state(state: TwinState = Depends(get_state)) -> TwinSnapshot:
    return TwinSnapshot(version=state.version, summary=state.summary(),
                        states=list(state.states.values()))


@router.get("/twin/nodes/{node_id}", response_model=NodeDetail)
async def node_detail(node_id: str, state: TwinState = Depends(get_state),
                      graph: TopologyGraph = Depends(get_graph)) -> NodeDetail:
    if node_id not in graph:
        raise HTTPException(404, f"Unknown node {node_id!r}")
    return NodeDetail(node=graph.node(node_id), state=state.states[node_id],
                      dependencies=graph.dependencies(node_id),
                      dependents=graph.dependents(node_id))


@router.post("/propagation/analyze", response_model=PropagationResponse)
async def analyze_propagation(req: PropagationRequest,
                              graph: TopologyGraph = Depends(get_graph)) -> PropagationResponse:
    try:
        result = propagation.analyze(graph, req)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PropagationResponse(result=result, explanation=explain_propagation(result))
