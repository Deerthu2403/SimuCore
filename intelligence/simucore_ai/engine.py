"""Scores the live stream: keeps the last minutes of every service, and turns scores into events.

Two opinions per service and tick, never mixed:

  anomaly   Isolation Forest: is this moment unusual?              (says nothing about the future)
  risk      XGBoost: how likely does it degrade or fail in 2 min?  (calibrated on synthetic data)

The engine only observes. Nothing it concludes can act on the bank, and nothing it says changes the
status of a component in the digital twin, which rests on measurements alone.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from simucore_ai import labels
from simucore_ai.detector import AnomalyDetector
from simucore_ai.features import (
    IDX,
    MIN_HISTORY,
    TICK_S,
    WINDOW,
    Profile,
    Service,
    raw_row,
    snapshot,
    temporal,
)
from simucore_ai.feed import Observation
from simucore_ai.predictor import FailurePredictor, by_metric

KEEP = max(WINDOW, labels.COOL_DOWN) + 5
MAX_GAP_S = 3 * TICK_S  # a longer hole in the data: the past does not join the present any more
OPEN_AFTER, OF_LAST = 2, 3  # an anomaly opens when 2 of the last 3 moments are unusual ...
CLOSE_AFTER = 4  # ... and closes after 4 ordinary ones
STORE_EVERY_S = 10.0
RESOLVE_SLACK_S = 6.0


@dataclass(frozen=True)
class Models:
    detector: AnomalyDetector
    predictors: dict[str, FailurePredictor]
    thresholds: dict[str, float]
    off: float
    profiles: dict[str, Profile]
    services: dict[str, Service]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return str(self.meta.get("version", "unknown"))


@dataclass
class AnomalyEvent:
    id: str
    service: str
    node_id: str
    opened_at: datetime
    last_seen: datetime
    closed_at: datetime | None
    score: float  # highest so far
    level: str
    condition: str
    contributions: list[dict]

    @property
    def severity(self) -> str:
        return "high" if self.level == "abnormal" else "medium"

    def view(self) -> dict:
        return {"id": self.id, "service": self.service, "node_id": self.node_id,
                "opened_at": self.opened_at.isoformat(), "last_seen": self.last_seen.isoformat(),
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "score": round(self.score, 4), "level": self.level, "condition": self.condition,
                "severity": self.severity, "contributions": self.contributions}


@dataclass
class Warning_:
    id: str
    service: str
    node_id: str
    target: str
    opened_at: datetime
    closed_at: datetime | None
    horizon_s: int
    probability: float
    peak: float
    reasons: list[dict]
    outcome: str = "open"  # open | came_true | did_not_happen | unresolved
    lead_s: float | None = None

    def view(self) -> dict:
        return {"id": self.id, "service": self.service, "node_id": self.node_id,
                "target": self.target, "opened_at": self.opened_at.isoformat(),
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "horizon_s": self.horizon_s, "probability": round(self.probability, 4),
                "peak": round(self.peak, 4), "reasons": self.reasons, "outcome": self.outcome,
                "lead_s": self.lead_s}


@dataclass
class Prediction:
    id: str
    ts: datetime
    service: str
    node_id: str
    target: str
    probability: float
    reasons: list[dict]
    outcome: bool | None = None

    def view(self) -> dict:
        return {"id": self.id, "ts": self.ts.isoformat(), "service": self.service,
                "node_id": self.node_id, "target": self.target,
                "probability": round(self.probability, 4), "reasons": self.reasons,
                "outcome": self.outcome}


@dataclass
class Assessment:
    """What can be said about one service now."""

    service: str
    node_id: str
    anomaly: dict | None = None
    risk: dict[str, dict] = field(default_factory=dict)
    anomaly_reason: str = ""  # why there is no anomaly score
    risk_reason: str = ""  # why there is no prediction
    reasons: list[dict] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def view(self, anomaly: AnomalyEvent | None, warnings: list[Warning_]) -> dict:
        return {"service": self.service, "node_id": self.node_id, "anomaly": self.anomaly,
                "risk": self.risk, "anomaly_reason": self.anomaly_reason,
                "risk_reason": self.risk_reason, "reasons": self.reasons, "metrics": self.metrics,
                "open_anomaly": anomaly.view() if anomaly else None,
                "warnings": [w.view() for w in warnings]}


@dataclass
class _State:
    raw: deque = field(default_factory=lambda: deque(maxlen=KEEP))
    status: deque = field(default_factory=lambda: deque(maxlen=KEEP))
    scores: deque = field(default_factory=lambda: deque(maxlen=OF_LAST))
    ordinary: int = 0
    anomaly: AnomalyEvent | None = None
    run: dict[str, int] = field(default_factory=lambda: dict.fromkeys(labels.TARGETS, 0))
    warning: dict[str, Warning_ | None] = field(
        default_factory=lambda: dict.fromkeys(labels.TARGETS))
    #: since when a condition is present, per target; None while it is not
    since: dict[str, datetime | None] = field(default_factory=lambda: dict.fromkeys(labels.TARGETS))
    pending: list[Prediction] = field(default_factory=list)
    waiting: list[Warning_] = field(default_factory=list)  # closed, but the horizon is not over


@dataclass
class Step:
    """What one observation changed."""

    anomalies: list[AnomalyEvent] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)  # to be stored
    outcomes: list[Prediction] = field(default_factory=list)  # whose result is known now


class Engine:
    def __init__(self, models: Models) -> None:
        self.m = models
        self.states = {s: _State() for s in models.services}
        self.current: dict[str, Assessment] = {}
        self.at: datetime | None = None
        self.origin: str | None = None
        self.last_stored: datetime | None = None
        self.scored = 0
        #: what became of the predictions since the start: per target (n, sum of squared errors)
        self.score_card = {t: [0, 0.0, 0] for t in labels.TARGETS}

    # -------------------------------------------------------------------- one observation
    def observe(self, obs: Observation) -> Step:
        step = Step()
        if self.origin is not None and obs.origin != self.origin:
            self._forget(step, obs.at)  # measured and synthetic must not flow into each other
        if self.at is not None and obs.at <= self.at:
            return step
        if self.at is not None and (obs.at - self.at).total_seconds() > MAX_GAP_S:
            self._forget(step, obs.at)
        self.at, self.origin = obs.at, obs.origin
        store = self.last_stored is None or \
            (obs.at - self.last_stored).total_seconds() >= STORE_EVERY_S - 0.5
        if store:
            self.last_stored = obs.at
        # a monitor that sees nobody says nothing about the services
        blind = all(o.status == "unresponsive" for o in obs.services.values())
        for s, spec in self.m.services.items():
            st, o = self.states[s], obs.services.get(s)
            status = "unresponsive" if o is None or blind else o.status
            st.raw.append(raw_row(status, o.metrics if o else {}, spec))
            st.status.append(status)
        for s, spec in self.m.services.items():
            a = Assessment(s, spec.node_id)
            st = self.states[s]
            if blind:
                a.anomaly_reason = a.risk_reason = "monitoring sees no service"
                st.scores.clear()
                self._give_up(st, step, obs.at)
            else:
                self._conditions(s, st, obs.at, step)
                self._anomaly(s, st, a, obs.at, step)
                self._risk(s, st, a, obs.at, step, store)
            self.current[s] = a
        self.scored += 1
        return step

    # ---------------------------------------------------------------------- what happened
    def _conditions(self, s: str, st: _State, now: datetime, step: Step) -> None:
        raw, status = np.array(st.raw), np.array(st.status)
        for target, c in zip(labels.TARGETS, labels.conditions(raw, status, self.m.services[s]),
                             strict=True):
            began = bool(c[-1]) and st.since[target] is None
            st.since[target] = (st.since[target] or now) if c[-1] else None
            if began:
                for w in self._warnings_of(st, target):
                    self._resolve(st, w, "came_true", now, step)
            for p in [p for p in st.pending if p.target == target]:
                age = (now - p.ts).total_seconds()
                if began and age > labels.GAP_S - 0.5:
                    self._outcome(st, p, True, step)
                elif began:  # too late to have been a prediction
                    st.pending.remove(p)
                elif age > labels.HORIZON_S + RESOLVE_SLACK_S:
                    self._outcome(st, p, False, step)
        for w in list(st.waiting):
            if (now - w.opened_at).total_seconds() > w.horizon_s + RESOLVE_SLACK_S:
                self._resolve(st, w, "did_not_happen", now, step)

    def _warnings_of(self, st: _State, target: str) -> list[Warning_]:
        out = [w for w in st.waiting if w.target == target]
        if (w := st.warning[target]) is not None:
            out.append(w)
        return out

    def _outcome(self, st: _State, p: Prediction, happened: bool, step: Step) -> None:
        st.pending.remove(p)
        p.outcome = happened
        card = self.score_card[p.target]
        card[0] += 1
        card[1] += (p.probability - float(happened)) ** 2
        card[2] += happened
        step.outcomes.append(p)

    def _resolve(self, st: _State, w: Warning_, outcome: str, now: datetime, step: Step) -> None:
        w.outcome = outcome
        if outcome == "came_true":
            w.lead_s = round((now - w.opened_at).total_seconds(), 1)
        w.closed_at = w.closed_at or now
        if st.warning[w.target] is w:
            st.warning[w.target] = None
        if w in st.waiting:
            st.waiting.remove(w)
        step.warnings.append(w)

    # --------------------------------------------------------------------------- anomaly
    def _anomaly(self, s: str, st: _State, a: Assessment, now: datetime, step: Step) -> None:
        if st.status[-1] != "up":
            a.anomaly_reason = "the service is down" if st.status[-1] == "down" \
                else "the service does not answer monitoring"
            st.scores.clear()
            if st.anomaly:  # there is nothing to compare any more
                self._close_anomaly(st, now, step)
            return
        row = snapshot(st.raw[-1][None], self.m.profiles[s])[0]
        found = self.m.detector.assess(s, row)
        a.anomaly = {"score": round(found.score, 4), "level": found.level,
                     "condition": found.condition,
                     "contributions": [{"metric": k, "share": share, "deviation": z}
                                       for k, share, z in found.contributions[:5]]}
        st.scores.append(found.score)
        unusual = found.level != "normal"
        st.ordinary = 0 if unusual else st.ordinary + 1
        if st.anomaly is None:
            if unusual and sum(x >= 0.5 for x in st.scores) >= OPEN_AFTER:
                st.anomaly = AnomalyEvent(str(uuid.uuid4()), s, a.node_id, now, now, None,
                                          found.score, found.level, found.condition,
                                          a.anomaly["contributions"])
                step.anomalies.append(st.anomaly)
            return
        e = st.anomaly
        if unusual:
            e.last_seen = now
            if found.score > e.score + 0.02 or (found.level, e.level) == ("abnormal", "unusual"):
                e.score, e.level = max(e.score, found.score), found.level
                e.condition, e.contributions = found.condition, a.anomaly["contributions"]
                step.anomalies.append(e)
        elif st.ordinary >= CLOSE_AFTER:
            self._close_anomaly(st, now, step)

    def _close_anomaly(self, st: _State, now: datetime, step: Step) -> None:
        if st.anomaly:
            st.anomaly.closed_at = now
            step.anomalies.append(st.anomaly)
            st.anomaly = None

    # ------------------------------------------------------------------------------ risk
    def _risk(self, s: str, st: _State, a: Assessment, now: datetime, step: Step,
              store: bool) -> None:
        raw = np.array(st.raw)
        if st.status[-1] != "up":
            a.risk_reason = "the service is down: there is nothing left to predict" \
                if st.status[-1] == "down" else "the service does not answer monitoring"
        elif int((raw[:, IDX["up"]] > 0).sum()) < MIN_HISTORY or len(raw) < MIN_HISTORY:
            a.risk_reason = f"needs {MIN_HISTORY * TICK_S:.0f} s of data first"
        if a.risk_reason:
            for t in labels.TARGETS:
                self._fade(st, t, now, step)
            return
        spec = self.m.services[s]
        deps = [(np.array(self.states[d].raw), self.m.profiles[d]) for d in spec.targets]
        x = temporal(raw[-WINDOW:], self.m.profiles[s], spec,
                     [(d[-WINDOW:], p) for d, p in deps])[-1:]
        status = np.array(st.status)
        present = dict(zip(labels.TARGETS, labels.conditions(raw, status, spec), strict=True))
        shown: list[tuple[float, list]] = []
        for target, p in self.m.predictors.items():
            if present[target][-labels.COOL_DOWN - 1:].any():
                a.risk[target] = {"probability": None, "level": "present" if present[target][-1]
                                  else "recovering", "horizon_s": labels.HORIZON_S}
                self._fade(st, target, now, step)
                continue
            prob = float(p.probability(x)[0])
            limit = self.m.thresholds[target]
            level = "high" if prob >= limit else "elevated" if prob >= limit * 0.4 else "low"
            a.risk[target] = {"probability": round(prob, 4), "level": level,
                              "threshold": round(limit, 4), "horizon_s": labels.HORIZON_S}
            reasons = p.explain(x[0]) if level != "low" or store else []
            if level != "low":
                shown.append((prob, reasons))
            told = [{"feature": r.feature, "metric": r.metric, "text": r.text, "value": r.value,
                     "contribution": r.contribution} for r in reasons]
            self._warn(st, s, target, prob, limit, told, now, step)
            if store:
                rec = Prediction(str(uuid.uuid4()), now, s, a.node_id, target, prob,
                                 told[:4] if level != "low" else [])
                st.pending.append(rec)
                step.predictions.append(rec)
        if shown:
            best = max(shown, key=lambda v: v[0])[1]
            a.reasons = [{"feature": r.feature, "metric": r.metric, "text": r.text,
                          "value": r.value, "contribution": r.contribution} for r in best]
            a.metrics = {k: round(v, 4) for k, v in by_metric(best).items()}

    def _warn(self, st: _State, s: str, target: str, prob: float, limit: float,
              reasons: list[dict], now: datetime, step: Step) -> None:
        w = st.warning[target]
        if w is None:
            st.run[target] = st.run[target] + 1 if prob >= limit else 0
            if st.run[target] >= 2:
                w = Warning_(str(uuid.uuid4()), s, self.m.services[s].node_id, target, now, None,
                             labels.HORIZON_S, prob, prob, reasons)
                st.warning[target] = w
                st.run[target] = 0
                step.warnings.append(w)
        elif prob < limit * self.m.off:
            self._fade(st, target, now, step)
        elif prob > w.peak + 0.02:
            w.peak, w.reasons = prob, reasons
            step.warnings.append(w)

    def _fade(self, st: _State, target: str, now: datetime, step: Step) -> None:
        """The warning is no longer shown. Whether it was right is known when its time is over."""
        st.run[target] = 0
        if (w := st.warning[target]) is not None:
            w.closed_at = now
            st.warning[target] = None
            st.waiting.append(w)
            step.warnings.append(w)

    # ------------------------------------------------------------------------ interruptions
    def _give_up(self, st: _State, step: Step, now: datetime) -> None:
        """Contact is lost: what is open can not be judged any more."""
        self._close_anomaly(st, now, step)
        for t in labels.TARGETS:
            for w in self._warnings_of(st, t):
                self._resolve(st, w, "unresolved", now, step)
            st.since[t] = None
            st.run[t] = 0
        st.pending.clear()

    def _forget(self, step: Step, now: datetime) -> None:
        for st in self.states.values():
            self._give_up(st, step, now)
            st.raw.clear()
            st.status.clear()
            st.scores.clear()
            st.ordinary = 0

    def expire(self, now: datetime, after_s: float) -> Step | None:
        """No data for a while: say nothing rather than repeat old opinions."""
        if self.at is None or now - self.at <= timedelta(seconds=after_s) or not self.current:
            return None
        step = Step()
        self._forget(step, now)
        self.current = {}
        return step

    # ------------------------------------------------------------------------------- views
    def open_anomalies(self) -> list[AnomalyEvent]:
        return [st.anomaly for st in self.states.values() if st.anomaly]

    def open_warnings(self) -> list[Warning_]:
        return [w for st in self.states.values() for w in st.warning.values() if w]

    def view(self) -> list[dict]:
        return [a.view(self.states[s].anomaly,
                       [w for w in self.states[s].warning.values() if w])
                for s, a in self.current.items()]

    def performance(self) -> dict[str, dict]:
        """How the predictions of this run turned out. Small numbers prove nothing."""
        return {t: {"resolved": n, "events": k, "brier": round(e / n, 4) if n else None}
                for t, (n, e, k) in self.score_card.items()}
