"""ML module contracts of the Phase 1 design.

The module that was built is the AI service in ``intelligence/`` (docs/12-ai-module.md). These
contracts are kept for the Phase 1 twin (API v1).

* ``AnomalyDetector`` - IsolationForest per node kind, trained on healthy baseline
  windows; z-score fallback while untrained. Emits ``AnomalyEvent``.
* ``FailurePredictor`` - XGBoost binary classifier; label = node reaches
  critical/down within ``horizon_s``. Emits ``FailurePrediction`` whose
  ``top_features`` come from TreeSHAP contributions (``pred_contribs=True``).

Both subscribe to ``twin.delta`` and maintain rolling windows per node.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.domain.contracts import AnomalyEvent, FailurePrediction
from app.domain.models import NodeState

Window = Sequence[NodeState]


class AnomalyDetector(Protocol):
    model_version: str

    def fit(self, baseline: dict[str, Window]) -> None: ...

    def score(self, node_id: str, window: Window) -> AnomalyEvent | None: ...


class FailurePredictor(Protocol):
    model_version: str
    horizon_s: int

    def fit(self, features: object, labels: object) -> dict[str, float]: ...

    def predict(self, node_id: str, window: Window) -> FailurePrediction: ...
