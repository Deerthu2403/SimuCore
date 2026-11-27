"""How good are the models? Rows, events and lead times; always against something simpler."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

from simucore_ai.features import TICK_S
from simucore_ai.labels import GAP, HORIZON, onsets


def rates(y: np.ndarray, p: np.ndarray) -> dict[str, float | int | None]:
    """Precision, recall, F1 of yes/no answers."""
    tp, fp = int((p & (y == 1)).sum()), int((p & (y == 0)).sum())
    fn, tn = int((~p & (y == 1)).sum()), int((~p & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {"precision": _r(precision), "recall": _r(recall), "f1": _r(f1),
            "false_positive_rate": _r(fp / (fp + tn)) if fp + tn else None,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def ranking(y: np.ndarray, score: np.ndarray) -> dict[str, float | int | None]:
    """Independent of any threshold."""
    if len(np.unique(y)) < 2:
        return {"roc_auc": None, "average_precision": None, "positives": int(y.sum()),
                "rows": len(y)}
    return {"roc_auc": _r(roc_auc_score(y, score)),
            "average_precision": _r(average_precision_score(y, score)),
            "positives": int(y.sum()), "rows": len(y), "prevalence": _r(float(y.mean()))}


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return _r(float(np.mean((p - y) ** 2)))


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """When it says 30 %, does it happen in 30 % of the cases?"""
    out = []
    for lo in np.arange(bins) / bins:
        k = (p >= lo) & (p < lo + 1 / bins) if lo < 1 - 1 / bins else p >= lo
        if k.sum():
            out.append({"said": _r(float(p[k].mean())), "happened": _r(float(y[k].mean())),
                        "rows": int(k.sum())})
    return out


def best_threshold(y: np.ndarray, p: np.ndarray, min_precision: float) -> float:
    """The smallest threshold with at least this precision; if impossible, the best by F1."""
    order = np.argsort(-p)
    tp = np.cumsum(y[order] == 1)
    precision = tp / np.arange(1, len(y) + 1)
    recall = tp / max(1, tp[-1])
    ok = np.flatnonzero(precision >= min_precision)
    k = ok[-1] if len(ok) else int(np.argmax(2 * precision * recall / (precision + recall + 1e-9)))
    return float(p[order[k]])


# ------------------------------------------------------------------------ events
@dataclass
class Events:
    """What an operator experiences: were failures announced, and how often was it a false alarm?"""

    onsets: int = 0
    warned: int = 0
    leads: list[float] = field(default_factory=list)
    alarms: int = 0
    false_alarms: int = 0
    hours: float = 0.0  # service-hours in which a warning was possible
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # kind -> [warned, all]
    missed: list[str] = field(default_factory=list)

    def add(self, name: str, kind: str, condition: np.ndarray, y: np.ndarray, alarm: np.ndarray,
            unknown_after: int | None = None) -> None:
        """``alarm`` per tick as it would be shown. ``y`` = -1 where nothing can be judged."""
        n = len(condition)
        usable = y >= 0
        self.hours += float(usable.sum()) * TICK_S / 3600
        starts = [t for t in onsets(condition) if t >= HORIZON // 4]
        begun = _begins(alarm)
        for t in starts:
            # only onsets out of a good state: not the second dip of the same incident
            if condition[max(0, t - HORIZON // 2): t].any():
                continue
            self.onsets += 1
            # an alarm that was on at least GAP before, and is not older than the horizon
            lead = None
            k = t - GAP
            if k >= 0 and alarm[max(0, t - HORIZON): k + 1].any():
                on = np.flatnonzero(alarm[max(0, t - HORIZON): k + 1]) + max(0, t - HORIZON)
                first = on[-1]
                while first - 1 in on:
                    first -= 1
                lead = (t - first) * TICK_S
            entry = self.by_kind.setdefault(kind, [0, 0])
            entry[1] += 1
            if lead is not None:
                self.warned += 1
                self.leads.append(lead)
                entry[0] += 1
            else:
                self.missed.append(f"{name}@{t}")
        for b in begun:
            if not usable[b]:
                continue
            self.alarms += 1
            if not condition[b: b + HORIZON + 1].any() and b + HORIZON < n:
                self.false_alarms += 1

    def result(self) -> dict:
        lead = np.array(self.leads)
        return {
            "failures": self.onsets, "announced": self.warned,
            "announced_share": _r(self.warned / self.onsets) if self.onsets else None,
            "announced_10s_before": int((lead >= 10).sum()),
            "announced_30s_before": int((lead >= 30).sum()),
            "lead_s_median": _r(float(np.median(lead)), 1) if len(lead) else None,
            "lead_s_p10": _r(float(np.percentile(lead, 10)), 1) if len(lead) else None,
            "alarms": self.alarms, "false_alarms": self.false_alarms,
            "alarm_precision": _r(1 - self.false_alarms / self.alarms) if self.alarms else None,
            "false_alarms_per_service_hour": _r(self.false_alarms / self.hours)
            if self.hours else None,
            "service_hours": _r(self.hours, 2),
            "by_kind": {k: {"announced": v[0], "of": v[1]}
                        for k, v in sorted(self.by_kind.items())},
        }


def _begins(a: np.ndarray) -> np.ndarray:
    return np.flatnonzero(a & ~np.concatenate([[False], a[:-1]]))


def persistent(p: np.ndarray, on: float, off: float, n: int = 2) -> np.ndarray:
    """An alarm begins after n ticks above ``on`` and ends below ``off``: no flicker."""
    out = np.zeros(len(p), dtype=bool)
    run, active = 0, False
    for i, x in enumerate(p):
        if np.isnan(x):
            run, active = 0, False
            continue
        if active:
            active = x >= off
            run = 0
        else:
            run = run + 1 if x >= on else 0
            active = run >= n
        out[i] = active
    return out


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), digits)
