"""What is predicted: exact definitions, used for synthetic and for recorded data alike.

A label is about the future of a row, a feature about its past; they never share a tick. Labels come
from the service's own condition, with the limits the digital twin uses for 'overloaded' and 'failed'
(backend/app/digital_twin/health.py), never from the knowledge which fault was injected.

  failure       the service becomes unavailable, or at least half of its requests fail
  degradation   it becomes short of its own resources (CPU, memory, queue, slots, or slow while
                strained), stops answering monitoring, or fails

  y(t) = 1      the condition begins in (t + GAP, t + HORIZON]
  y(t) = 0      it does not begin in (t, t + HORIZON]
  not used      it begins within GAP (too late to be a prediction); it is already present; the last
                COOL_DOWN ticks contain it (recovering); the recording ends within HORIZON
"""

from __future__ import annotations

import numpy as np

from simucore_ai.features import IDX, TICK_S, Service

HORIZON_S = 120
GAP_S = 4
COOL_DOWN_S = 60
HORIZON, GAP, COOL_DOWN = (int(s / TICK_S) for s in (HORIZON_S, GAP_S, COOL_DOWN_S))
PERSIST = 2  # a condition counts from its second consecutive tick, as in the twin
TARGETS = ("degradation", "failure")
MIN_RPS = 0.3


def conditions(raw: np.ndarray, status: np.ndarray, s: Service) -> tuple[np.ndarray, np.ndarray]:
    """(degraded, failed) per tick for one service. ``status``: up | down | unresponsive."""

    def over(key: str, limit: float) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return np.nan_to_num(raw[:, IDX[key]], nan=-1.0) >= limit

    up, down = status == "up", status == "down"
    traffic = over("requests", MIN_RPS)
    failed_now = down | (up & traffic & over("errors", 0.5))
    slow = traffic & over("latency", 3 * s.baseline_latency_ms)
    strained = over("cpu", 60) | over("queue", 1 / s.max_concurrency) | over("in_flight", 0.5)
    overloaded = over("cpu", 90) | over("memory", 92) | over("queue", 0.25) \
        | over("in_flight", 0.9) | (slow & strained)
    degraded_now = failed_now | (up & overloaded) | (status == "unresponsive")
    return persist(degraded_now) | down, persist(failed_now) | down


def persist(a: np.ndarray, n: int = PERSIST) -> np.ndarray:
    """True from the n-th consecutive true tick on."""
    run = 0
    out = np.zeros(len(a), dtype=bool)
    for i, x in enumerate(a):
        run = run + 1 if x else 0
        out[i] = run >= n
    return out


def onsets(condition: np.ndarray) -> np.ndarray:
    """Ticks at which the condition begins."""
    return np.flatnonzero(condition & ~np.concatenate([[False], condition[:-1]]))


def target(condition: np.ndarray, horizon: int = HORIZON, gap: int = GAP) -> np.ndarray:
    """1, 0, or -1 (row not used), per tick."""
    n = len(condition)
    y = np.zeros(n, dtype=np.int8)
    y[max(0, n - horizon):] = -1  # what comes next is not recorded
    for t in onsets(condition):
        y[max(0, t - horizon): max(0, t - gap)] = 1
        y[max(0, t - gap): t] = -1
    recent = np.zeros(n, dtype=bool)
    for t in np.flatnonzero(condition):
        recent[t: t + COOL_DOWN + 1] = True
    y[recent] = -1
    return y
