"""Self-calibration contract (Phase 5).

The calibrator closes the loop between what the twin *predicted* and what
*actually* happened:

1. ``record_prediction`` stores each forecast (ML probability or SimPy KPI).
2. ``resolve`` pairs it with the observed outcome once the horizon has elapsed
   (rows in ``prediction_outcome``).
3. ``evaluate`` computes Brier score / reliability (classifiers), MAE / MAPE
   (regression, simulation) and drift (PSI, KS) over a sliding window.
4. When thresholds are breached it acts: isotonic/Platt recalibration, SimPy
   service-time re-fit, or a retraining request - and publishes
   ``calibration.updated`` with a ``CalibrationReport``.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.contracts import CalibrationReport, FailurePrediction, SimulationResult


class Calibrator(Protocol):
    def record_prediction(self, prediction: FailurePrediction | SimulationResult) -> None: ...

    def resolve(self, prediction_id: str, actual_event: bool | None,
                actual_value: float | None) -> None: ...

    def evaluate(self, target: str) -> CalibrationReport: ...
