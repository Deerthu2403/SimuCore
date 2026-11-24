"""Synthetic monitoring data, for development, demos and training sets when no bank is running.

Always labelled origin="synthetic". The same seed and time give the same values, so a live run and
a backfill of the same period agree. Incidents are part of the pattern: each service has quiet
periods, overloads and outages, with metrics that move together the way real ones do.
"""

from __future__ import annotations

import math
import random
from datetime import datetime

from monitor.config import ServiceSpec, Settings
from monitor.models import Collection, InfraSample, Sample, utcnow

BASE_RPS = {"authentication": 22, "database": 60, "account": 18, "transaction": 4, "customer": 5,
            "payment": 2, "fraud": 4, "notification": 2}
SLOT_S = 300  # each service draws one condition per five minutes
CONDITIONS = (("normal", 0.93), ("overload", 0.04), ("errors", 0.02), ("outage", 0.01))


class SyntheticSource:
    name = "synthetic"

    def __init__(self, settings: Settings, services: tuple[ServiceSpec, ...]) -> None:
        self.services, self.seed = services, settings.seed

    async def collect(self, with_infra: bool) -> Collection:
        return self.at(utcnow(), with_infra)

    def at(self, ts: datetime, with_infra: bool = True) -> Collection:
        infra = []
        if with_infra:
            rng = self._rng("host", int(ts.timestamp()))
            infra = [InfraSample("synthetic-host", ts, "up", {
                "cpu_pct": round(25 + 20 * self._daily(ts) + rng.gauss(0, 2), 2),
                "mem_pct": round(50 + rng.gauss(0, 1), 2)})]
        return Collection(ts, "synthetic", [self._sample(s, ts) for s in self.services], infra)

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(f"{self.seed}|{'|'.join(map(str, parts))}")

    @staticmethod
    def _daily(ts: datetime) -> float:
        """0 at night to 1 in the early afternoon (UTC)."""
        hour = ts.hour + ts.minute / 60
        return 0.5 - 0.5 * math.cos((hour - 2) / 24 * 2 * math.pi)

    def condition(self, service: str, ts: datetime) -> str:
        rng = self._rng("condition", service, int(ts.timestamp()) // SLOT_S)
        names, weights = zip(*CONDITIONS, strict=True)
        return rng.choices(names, weights)[0]

    def _sample(self, s: ServiceSpec, ts: datetime) -> Sample:
        condition = self.condition(s.name, ts)
        sample = Sample(s.name, s.node_id, ts, "up")
        if condition == "outage":
            sample.status, sample.reason = "down", "synthetic outage"
            sample.put("availability", 0.0)
            return sample
        rng = self._rng("noise", s.name, int(ts.timestamp()))
        load = {"overload": 2.5}.get(condition, 1.0)
        rps = BASE_RPS.get(s.name, 5) * (0.3 + 0.7 * self._daily(ts)) * load
        rps = max(0.0, rps * rng.gauss(1, 0.08))
        mean = s.baseline_latency_ms * 0.4 * (load**1.8) * max(0.2, rng.gauss(1, 0.1))
        error = {"errors": 0.15, "overload": 0.03}.get(condition, 0.0) * rng.uniform(0.7, 1.3)
        in_flight = rps * mean / 1000
        values = {
            "cpu_pct": min(100.0, 2 + rps * 0.8 + rng.gauss(0, 0.5)),
            "mem_bytes": (48 + in_flight + rng.gauss(0, 0.5)) * 1_048_576,
            "latency_ms": mean * 2.2, "latency_mean_ms": mean, "rps": rps, "error_rate": error,
            "net_rx_bps": rps * 900, "net_tx_bps": rps * 1100,
            "active_connections": 1 + round(rps / 4), "in_flight": round(in_flight),
            "queue_length": float(rng.randint(1, 20)) if condition == "overload" else 0.0,
            "availability": 1.0,
        }
        values["mem_pct"] = values["mem_bytes"] / (256 * 1_048_576) * 100
        if s.uses_database:
            values["db_response_ms"] = 1.5 * load * max(0.2, rng.gauss(1, 0.2))
        for k, v in values.items():
            sample.put(k, max(0.0, v))
        return sample

    async def aclose(self) -> None:
        return None
