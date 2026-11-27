"""From monitoring data to model inputs. Training and live scoring use exactly these functions, so
that a model never sees something else in operation than in training.

Everything here looks only at the present and the past: row ``t`` is computed from rows ``<= t``.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

TICK_S = 2.0
#: The eight metrics of the specification, in this order everywhere.
METRICS = ("cpu", "memory", "latency", "errors", "requests", "network", "database", "connections")
LABEL = {"cpu": "CPU utilisation", "memory": "memory utilisation", "latency": "request latency",
         "errors": "error rate", "requests": "requests per second", "network": "network traffic",
         "database": "database response time", "connections": "active connections",
         "cpu_per_request": "CPU per request", "bytes_per_request": "bytes per request"}
#: Columns of a raw row: the eight metrics, then what only the failure predictor uses.
RAW = (*METRICS, "queue", "in_flight", "pool", "up")
DERIVED = ("cpu_per_request", "bytes_per_request")
SNAPSHOT = (*METRICS, *DERIVED)
IDX = {k: i for i, k in enumerate(RAW)}

WINDOW = 30  # ticks the predictor looks back: 60 s
SHORT = 10  # 20 s
MIN_HISTORY = 15  # 30 s: with less there is no prediction


@dataclass(frozen=True)
class Service:
    name: str
    node_id: str
    max_concurrency: float
    baseline_latency_ms: float
    targets: tuple[str, ...]


@lru_cache
def load_services(path: Path) -> dict[str, Service]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {n: Service(n, s["node_id"], float(s["max_concurrency"]),
                       float(s["baseline_latency_ms"]),
                       tuple(d["target"] for d in raw["dependencies"]
                             if d["source"] == n and d["mode"] != "best_effort"))
            for n, s in raw["services"].items()}


# ---------------------------------------------------------------------------- raw rows
def raw_row(status: str, m: Mapping[str, float | None], s: Service) -> np.ndarray:
    """One service at one moment. NaN = not known (idle, down, not delivered); never 0 instead."""
    out = np.full(len(RAW), np.nan)
    out[IDX["up"]] = 1.0 if status == "up" else 0.0
    if status != "up":
        return out

    def get(key: str) -> float:
        v = m.get(key)
        return np.nan if v is None else float(v)

    out[IDX["cpu"]] = get("cpu_pct")
    out[IDX["memory"]] = get("mem_pct")
    out[IDX["latency"]] = get("latency_mean_ms")
    out[IDX["errors"]] = get("error_rate")
    out[IDX["requests"]] = get("rps")
    out[IDX["network"]] = get("net_rx_bps") + get("net_tx_bps")
    out[IDX["database"]] = get("db_response_ms")
    out[IDX["connections"]] = get("active_connections")
    out[IDX["queue"]] = get("queue_length") / s.max_concurrency
    out[IDX["in_flight"]] = get("in_flight") / s.max_concurrency
    size = m.get("db_pool_size")
    if size:
        out[IDX["pool"]] = get("db_pool_in_use") / size
    return out


# ------------------------------------------------------------------------ normalisation
def transform(raw: np.ndarray) -> np.ndarray:
    """Raw rows -> the ten snapshot quantities on scales where 'twice as much' is comparable."""
    cpu, mem, lat, err, rps, net, db, con = (raw[..., IDX[k]] for k in METRICS)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.stack([
            cpu, mem, np.log1p(lat), np.sqrt(err), np.log1p(rps), np.log1p(net / 1000),
            np.log1p(db), np.log1p(con),
            np.log((cpu + 1) / (rps + 1)), np.log((net + 100) / (rps + 0.1)),
        ], axis=-1)


#: Smallest spread per quantity: a metric that never moved in the recordings would otherwise make
#: the tiniest change look enormous.
FLOOR = np.array([2.0, 1.0, 0.12, 0.05, 0.15, 0.15, 0.15, 0.2, 0.15, 0.15])


@dataclass(frozen=True)
class Profile:
    """What is normal for one service: centre and spread of each snapshot quantity."""

    centre: np.ndarray
    spread: np.ndarray

    @classmethod
    def fit(cls, raw: np.ndarray) -> Profile:
        g = transform(raw[raw[:, IDX["up"]] > 0])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # a column without any value
            q = np.nanpercentile(g, [10, 50, 90], axis=0)
        centre = np.nan_to_num(q[1])
        spread = np.nan_to_num((q[2] - q[0]) / 2.563)  # the same for a normal distribution
        return cls(centre, np.maximum(spread, FLOOR))

    def z(self, raw: np.ndarray) -> np.ndarray:
        return np.clip((transform(raw) - self.centre) / self.spread, -30, 30)

    def to_json(self) -> dict:
        return {"centre": [round(float(x), 5) for x in self.centre],
                "spread": [round(float(x), 5) for x in self.spread]}

    @classmethod
    def from_json(cls, d: Mapping) -> Profile:
        return cls(np.array(d["centre"]), np.array(d["spread"]))


def snapshot(raw: np.ndarray, profile: Profile) -> np.ndarray:
    """Input of the anomaly detector: one moment, no history. Unknown counts as ordinary, because a
    service without traffic simply has no latency."""
    return np.nan_to_num(profile.z(raw), nan=0.0)


# --------------------------------------------------------------------- temporal features
def _names() -> tuple[str, ...]:
    out: list[str] = []
    for m in METRICS:
        out += [f"{m}_now", f"{m}_mean_20s", f"{m}_change_20s", f"{m}_trend_60s", f"{m}_spread_60s"]
    out += ["cpu_level", "memory_level", "latency_level", "errors_level", "queue_level",
            "in_flight_level", "pool_level", "memory_seconds_to_limit", "cpu_seconds_to_limit",
            "dependency_latency", "dependency_errors", "dependency_cpu", "dependency_down",
            "dependency_memory", "dependency_memory_trend", "dependency_pool"]
    return tuple(out)


TEMPORAL = _names()


def describe(feature: str) -> str:
    """A feature in words."""
    fixed = {
        "cpu_level": "CPU utilisation", "memory_level": "memory utilisation",
        "latency_level": "latency compared with its limit", "errors_level": "error rate",
        "queue_level": "waiting requests", "in_flight_level": "busy worker slots",
        "pool_level": "database connections in use",
        "memory_seconds_to_limit": "time until memory runs out at this rate",
        "cpu_seconds_to_limit": "time until the CPU is full at this rate",
        "dependency_latency": "latency of a service it depends on",
        "dependency_errors": "errors of a service it depends on",
        "dependency_cpu": "CPU of a service it depends on",
        "dependency_down": "a service it depends on is down",
        "dependency_memory": "memory of a service it depends on",
        "dependency_pool": "free connections of the database it depends on",
        "dependency_memory_trend": "memory trend of a service it depends on",
    }
    if feature in fixed:
        return fixed[feature]
    metric, what = feature.split("_", 1)
    return f"{LABEL[metric]} " + {"now": "now", "mean_20s": "over 20 s", "change_20s": "rising or "
                                  "falling since the last minute", "trend_60s": "trend over 60 s",
                                  "spread_60s": "fluctuation over 60 s"}[what]


def metric_of(feature: str) -> str:
    """Which measurement a feature is about, for showing explanations per metric."""
    head = feature.split("_")[0]
    return {"queue": "queue", "in": "in_flight", "pool": "pool"}.get(head, head)


def _windows(a: np.ndarray, n: int) -> np.ndarray:
    pad = np.full((n - 1, *a.shape[1:]), np.nan)
    return sliding_window_view(np.concatenate([pad, a]), n, axis=0)  # (T, K, n)


def _slope(w: np.ndarray) -> np.ndarray:
    """Least-squares slope per minute over the known values of each window."""
    x = np.arange(w.shape[-1], dtype=float) * TICK_S / 60
    known = np.isfinite(w)
    n = known.sum(-1)
    xk = np.where(known, x, 0.0)
    yk = np.where(known, w, 0.0)
    sx, sy = xk.sum(-1), yk.sum(-1)
    sxx, sxy = (xk * x).sum(-1), (yk * x).sum(-1)
    d = n * sxx - sx**2
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (n * sxy - sx * sy) / d
    out[(n < 5) | (d <= 1e-9)] = np.nan
    return out


def temporal(raw: np.ndarray, profile: Profile, service: Service,
             dependencies: Iterable[tuple[np.ndarray, Profile]] = ()) -> np.ndarray:
    """Input of the failure predictor: the last minute. (T, RAW) -> (T, TEMPORAL)"""
    z = profile.z(raw)[:, : len(METRICS)]
    w = _windows(z, WINDOW)
    r = _windows(raw[:, [IDX["cpu"], IDX["memory"]]], WINDOW)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # windows without any known value
        short = np.nanmean(w[..., -SHORT:], axis=-1)
        before = np.nanmean(w[..., :-SHORT], axis=-1)
        spread = np.nanstd(w, axis=-1)
        recent = np.nanmean(r[..., -SHORT:], axis=-1)
        raw_slope = _slope(r)
    blocks = np.stack([z, short, short - before, _slope(w), spread], axis=-1)  # (T, 8, 5)
    cols = [blocks.reshape(len(raw), -1)]

    def limit(k: int, at: float) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            t = (at - recent[:, k]) / raw_slope[:, k] * 60
        t = np.where(raw_slope[:, k] > 0.5, t, 600.0)
        return np.where(np.isfinite(recent[:, k]), np.clip(t, 0, 600), np.nan)

    level = [raw[:, IDX["cpu"]] / 100, raw[:, IDX["memory"]] / 100,
             raw[:, IDX["latency"]] / service.baseline_latency_ms, raw[:, IDX["errors"]],
             raw[:, IDX["queue"]], raw[:, IDX["in_flight"]], raw[:, IDX["pool"]],
             limit(1, 92.0), limit(0, 90.0)]
    cols.append(np.stack(level, axis=-1))
    dep = np.full((len(raw), 7), np.nan)
    for d_raw, d_profile in dependencies:
        dz = d_profile.z(d_raw)
        memory = d_raw[:, IDX["memory"]]
        now = np.stack([dz[:, 2], d_raw[:, IDX["errors"]], dz[:, 0], 1 - d_raw[:, IDX["up"]],
                        memory / 100, _slope(_windows(memory[:, None], WINDOW))[:, 0],
                        d_raw[:, IDX["pool"]]], -1)
        dep = np.fmax(dep, now)
    cols.append(dep)
    return np.concatenate(cols, axis=-1).astype(np.float32)
