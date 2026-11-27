"""Failure prediction with XGBoost: how likely does the service degrade or fail within two minutes?

Supervised: it learned from labelled examples of what happened afterwards. One model per target. Its
probabilities are calibrated on synthetic data, so they are right for a world like the synthetic one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import xgboost as xgb

from simucore_ai.features import TEMPORAL, describe, metric_of

PARAMS = {"objective": "binary:logistic", "eval_metric": "aucpr", "eta": 0.05, "max_depth": 5,
          "min_child_weight": 20, "subsample": 0.8, "colsample_bytree": 0.6, "lambda": 2.0,
          "tree_method": "hist", "max_bin": 128}
ROUNDS = 800
STOP_AFTER = 40


@dataclass(frozen=True)
class Reason:
    feature: str
    metric: str
    text: str
    value: float | None
    contribution: float  # log-odds; positive = raises the risk


class FailurePredictor:
    def __init__(self, booster: xgb.Booster, x: np.ndarray, y: np.ndarray) -> None:
        """``x`` -> ``y``: knots of the calibration curve from raw to calibrated probability."""
        self.booster, self.knots = booster, (np.asarray(x, float), np.asarray(y, float))

    @classmethod
    def fit(cls, train: tuple[np.ndarray, np.ndarray], valid: tuple[np.ndarray, np.ndarray],
            seed: int = 7, params: dict | None = None, rounds: int = ROUNDS) -> FailurePredictor:
        """``valid`` decides only when to stop."""
        dt = xgb.DMatrix(train[0], train[1], feature_names=list(TEMPORAL))
        dv = xgb.DMatrix(valid[0], valid[1], feature_names=list(TEMPORAL))
        b = xgb.train({**PARAMS, "seed": seed, "nthread": 6, **(params or {})}, dt, rounds,
                      evals=[(dv, "valid")], early_stopping_rounds=STOP_AFTER, verbose_eval=False)
        return cls(b[: b.best_iteration + 1], np.array([0.0, 1.0]), np.array([0.0, 1.0]))

    def raw(self, x: np.ndarray) -> np.ndarray:
        return self.booster.predict(xgb.DMatrix(x, feature_names=list(TEMPORAL)))

    def calibrate(self, x: np.ndarray, y: np.ndarray) -> None:
        """Isotonic regression on rows that neither trained the trees nor stopped them."""
        from sklearn.isotonic import IsotonicRegression  # only for training

        iso = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(self.raw(x), y)
        self.knots = (iso.X_thresholds_.astype(float), iso.y_thresholds_.astype(float))

    def probability(self, x: np.ndarray) -> np.ndarray:
        # Never exactly 0 or 1: no finite amount of data supports certainty.
        return np.clip(np.interp(self.raw(x), *self.knots), 0.001, 0.999)

    def explain(self, row: np.ndarray, top: int = 6) -> list[Reason]:
        """Exact contributions of the features to this one prediction (TreeSHAP)."""
        d = xgb.DMatrix(row[None], feature_names=list(TEMPORAL))
        c = self.booster.predict(d, pred_contribs=True)[0][:-1]
        order = np.argsort(-np.abs(c))[:top]
        return [Reason(TEMPORAL[i], metric_of(TEMPORAL[i]), describe(TEMPORAL[i]),
                       None if np.isnan(row[i]) else round(float(row[i]), 4), round(float(c[i]), 4))
                for i in order if abs(c[i]) > 1e-6]

    def importance(self) -> dict[str, float]:
        """Share of the total improvement (gain) each feature brought in training."""
        g = self.booster.get_score(importance_type="total_gain")
        total = sum(g.values()) or 1.0
        return dict(sorted(((k, round(v / total, 5)) for k, v in g.items()), key=lambda kv: -kv[1]))

    def to_state(self) -> dict[str, Any]:
        return {"booster": self.booster.save_raw("json").decode(),
                "knots": [list(map(float, k)) for k in self.knots]}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> FailurePredictor:
        b = xgb.Booster()
        b.load_model(bytearray(d["booster"], "utf-8"))
        return cls(b, *map(np.array, d["knots"]))


def by_metric(reasons: list[Reason]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in reasons:
        out[r.metric] = out.get(r.metric, 0.0) + r.contribution
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1])))
