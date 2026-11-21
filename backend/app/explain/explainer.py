"""Explanation builders. Every recommendation must carry an ``Explanation``.

Phase 1 provides the propagation-path explainer (no ML needed). TreeSHAP and
Pareto trade-off explainers are added in Phases 3 and 5.
"""

from __future__ import annotations

from app.domain.contracts import Explanation, ExplanationFactor, PropagationResult


def explain_propagation(result: PropagationResult, top_k: int = 5) -> Explanation:
    downstream = [n for n in result.impacted if n.depth > 0]
    top = sorted(downstream, key=lambda n: -(n.impact * n.criticality))[:top_k]
    origin = ", ".join(result.origin)
    summary = (
        f"Failure of {origin} impacts {len(downstream)} dependent component(s); "
        f"business impact score {result.business_impact:.2f}."
    )
    if top:
        summary += f" Most exposed: {top[0].node_id} via {' -> '.join(top[0].path)}."
    return Explanation(
        summary=summary,
        method="propagation_path",
        factors=[ExplanationFactor(name=n.node_id, value=n.impact,
                                   contribution=round(n.impact * n.criticality, 3)) for n in top],
        evidence=[{"path": n.path, "impact": n.impact} for n in top],
        confidence=None,
    )
