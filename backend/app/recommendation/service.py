"""Recommendation service (Phase 5).

Subscribes to prediction / propagation / optimisation / simulation events,
de-duplicates by (target_nodes, action type) within a time window, scores
``priority = probability x business_impact``, attaches an ``Explanation`` and
publishes ``recommendation.created``. Recommendations are advisory only.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.contracts import Recommendation


class RecommendationService(Protocol):
    async def run(self) -> None: ...

    def open_recommendations(self) -> list[Recommendation]: ...
