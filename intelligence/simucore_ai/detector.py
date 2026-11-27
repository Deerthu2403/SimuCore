"""Anomaly detection with Isolation Forest: is this moment unlike what the service normally does?

It needs no labels to learn, knows nothing about failures and says nothing about the future. A high
score means 'unusual', not 'about to fail'.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from simucore_ai.features import SNAPSHOT
from simucore_ai.forest import Forest

#: score 0..1 with 0.5 exactly at the threshold
UNUSUAL, ABNORMAL = 0.5, 0.75
CONDITIONS = {
    "normal": "nothing unusual",
    "traffic": "unusual amount of traffic",
    "resources": "unusual use of CPU or memory",
    "latency": "unusually slow or fast answers",
    "errors": "unusual number of errors",
    "database": "unusual database response time",
    "connections": "unusual number of connections",
}
GROUP = {"cpu": "resources", "memory": "resources", "cpu_per_request": "resources",
         "latency": "latency", "errors": "errors", "requests": "traffic", "network": "traffic",
         "bytes_per_request": "traffic", "database": "database", "connections": "connections"}


@dataclass(frozen=True)
class Anomaly:
    score: float  # 0..1
    level: str  # normal | unusual | abnormal
    condition: str
    contributions: tuple[tuple[str, float, float], ...]  # (metric, share 0..1, z), largest first


def augment(rows: np.ndarray) -> np.ndarray:
    """Two more columns: the largest deviation and the typical one.

    A forest looks at one random quantity per split, so a moment in which a single one of ten is
    extreme would look almost ordinary to it. These columns show it what all of them together say."""
    size = np.abs(rows)
    return np.column_stack([rows, size.max(axis=1), np.sqrt((rows**2).mean(axis=1))])


class AnomalyDetector:
    """One forest per service."""

    def __init__(self, forests: dict[str, Forest], threshold: dict[str, float],
                 width: dict[str, float]) -> None:
        self.forests, self.threshold, self.width = forests, threshold, width

    @classmethod
    def fit(cls, x: dict[str, np.ndarray], seed: int = 7, trees: int = 100,
            samples: int = 256) -> AnomalyDetector:
        from sklearn.ensemble import IsolationForest  # only for training

        forests = {}
        for s, rows in x.items():
            forests[s] = Forest.from_sklearn(
                IsolationForest(n_estimators=trees, max_samples=min(samples, len(rows)),
                                random_state=seed, n_jobs=1).fit(augment(rows).astype(np.float32)))
        return cls(forests, dict.fromkeys(x, 0.6), dict.fromkeys(x, 0.05))

    def raw(self, service: str, rows: np.ndarray) -> np.ndarray:
        """The forest's own measure: larger = easier to isolate = more unusual."""
        return self.forests[service].score(augment(rows))

    def calibrate(self, service: str, normal: np.ndarray, false_alarms: float) -> None:
        """Set the threshold on validation data so that this share of normal moments exceeds it."""
        r = self.raw(service, normal)
        self.threshold[service] = float(np.quantile(r, 1 - false_alarms))
        self.width[service] = float(max(1e-3, (self.threshold[service] - np.median(r)) / 3))

    def score(self, service: str, rows: np.ndarray) -> np.ndarray:
        x = (self.raw(service, rows) - self.threshold[service]) / self.width[service]
        return 1 / (1 + np.exp(-np.clip(x, -30, 30)))

    def explain(self, service: str, row: np.ndarray) -> list[tuple[str, float, float]]:
        """How much less unusual the moment would be if this quantity were normal."""
        base = self.raw(service, row[None])[0]
        probes = np.repeat(row[None], len(SNAPSHOT), axis=0)
        np.fill_diagonal(probes, 0.0)
        drop = np.maximum(0.0, base - self.raw(service, probes))
        # what is not unusual itself is not a reason, whatever the forest makes of it
        drop = np.where(np.abs(row) < 1.5, 0.0, drop)
        total = drop.sum()
        if total <= 0:
            k = int(np.argmax(np.abs(row)))
            drop[k] = total = 1.0
        order = np.argsort(-drop)
        return [(SNAPSHOT[i], round(float(drop[i] / total), 4), round(float(row[i]), 2))
                for i in order if drop[i] > 0]

    def assess(self, service: str, row: np.ndarray, score: float | None = None) -> Anomaly:
        if score is None:
            score = float(self.score(service, row[None])[0])
        level = "abnormal" if score >= ABNORMAL else "unusual" if score >= UNUSUAL else "normal"
        if level == "normal":
            return Anomaly(score, level, "normal", ())
        why = self.explain(service, row)
        return Anomaly(score, level, classify(why), tuple(why))

    def save(self, path: Path) -> None:
        out: dict[str, np.ndarray] = {}
        for s, f in self.forests.items():
            out |= {f"{s}/{k}": v for k, v in f.arrays().items()}
            out[f"{s}/scale"] = np.array([self.threshold[s], self.width[s]])
        np.savez_compressed(path, **out)

    @classmethod
    def load(cls, path: Path) -> AnomalyDetector:
        with np.load(path, allow_pickle=False) as z:
            names = sorted({k.split("/")[0] for k in z.files})
            forests = {s: Forest.from_arrays({k.split("/")[1]: z[k] for k in z.files
                                              if k.startswith(f"{s}/")}) for s in names}
            scale = {s: z[f"{s}/scale"] for s in names}
        return cls(forests, {s: float(v[0]) for s, v in scale.items()},
                   {s: float(v[1]) for s, v in scale.items()})


def classify(contributions: list[tuple[str, float, float]]) -> str:
    """The kind of condition: the group of quantities that contributes most."""
    share: dict[str, float] = {}
    for metric, part, _ in contributions:
        share[GROUP[metric]] = share.get(GROUP[metric], 0.0) + part
    return max(share, key=lambda k: share[k]) if share else "normal"
