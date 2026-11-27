"""Reading recordings of the simulated bank, and the episodes the generator makes: the same shape."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from simucore_ai.features import IDX, Service, raw_row


@dataclass
class Episode:
    """One continuous stretch of time for the whole bank."""

    name: str
    kind: str  # what happens in it
    service: str  # to which service
    raw: dict[str, np.ndarray]  # service -> (T, RAW)
    status: dict[str, np.ndarray]  # service -> (T,) up | down | unresponsive
    #: service -> (T,): is something wrong with this very service, according to what was done to it?
    #: 1 yes, 0 no, -1 cannot be said (recovering, or affected by another service)
    abnormal: dict[str, np.ndarray]
    onset: int = -1  # tick at which the disturbance begins, -1 if there is none
    origin: str = "measured"
    meta: dict = field(default_factory=dict)

    @property
    def ticks(self) -> int:
        return len(next(iter(self.raw.values())))


#: kinds in which nothing is wrong with any service
HARMLESS = {"calm", "load_wave"}


def load(path: Path, services: dict[str, Service]) -> Episode:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    run = doc["run"]
    times = np.array([datetime.fromisoformat(t["ts"]).timestamp() for t in doc["ticks"]])
    n = len(times)
    raw = {s: np.full((n, len(IDX)), np.nan) for s in services}
    status = {s: np.full(n, "unresponsive", dtype="<U12") for s in services}
    for i, t in enumerate(doc["ticks"]):
        for s, spec in services.items():
            v = t["services"].get(s)
            if v:
                raw[s][i] = raw_row(v["status"], v["metrics"], spec)
                status[s][i] = v["status"]
    at = {}
    for e in doc["events"]:
        at.setdefault(e["what"], datetime.fromisoformat(e["ts"]).timestamp())
    begin = next((at[k] for k in ("onset",) if k in at), None)
    end = at.get("ended")
    onset = int(np.searchsorted(times, begin)) if begin else -1
    stop = int(np.searchsorted(times, end)) if end else n
    abnormal = {}
    for s in services:
        a = np.zeros(n, dtype=np.int8)
        if onset >= 0 and run["kind"] not in HARMLESS:
            a[onset:] = -1  # others may suffer, and everything recovers afterwards
            # the weakest beginning of a disturbance cannot be called abnormal yet: first 20 s
            if s == run["service"] or (run["kind"] == "db_exhaust" and s == "database"):
                a[min(n, onset + 10): stop] = 1
        abnormal[s] = a
    if run["kind"] in ("blip_cpu", "blip_latency", "crash", "downtime"):
        # active only now and then: when exactly is not in the record
        for a in abnormal.values():
            a[a == 1] = -1
    if run["kind"] in HARMLESS and run["kind"] == "load_wave":
        for a in abnormal.values():
            a[onset:] = -1  # unusual traffic, but nothing wrong: neither right nor wrong to notice
    return Episode(path.stem.split(".")[0], run["kind"], run["service"], raw, status, abnormal,
                   onset, "measured", {**run, "events": doc["events"],
                                       "recorded_at": doc["recorded_at"]})


def load_set(folder: Path, services: dict[str, Service]) -> list[Episode]:
    return [load(p, services) for p in sorted(folder.glob("*.json.gz"))]
