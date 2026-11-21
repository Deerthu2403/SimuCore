"""Synthetic banking infrastructure that *behaves* like a real estate.

This plays the role of "production" while SimuCore runs on simulated data. It
is deliberately separate from the adapter: the adapter only *reads* from it,
exactly as a production adapter would only read from Prometheus.

Behaviour model (per tick):
  * load follows a diurnal curve + noise,
  * utilisation drives CPU and a non-linear latency curve,
  * random incidents (cpu spike, memory leak, latency, crash) occur at a
    configurable rate, or can be injected explicitly,
  * degradation of a dependency leaks into its dependents (weighted), giving
    realistic correlated failures for the ML phase.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime

import networkx as nx

from app.domain.models import MetricSample, NodeKind, Topology
from app.domain.topology import TopologyGraph

INCIDENT_TYPES = ("cpu_spike", "memory_leak", "latency", "crash")
#: Relative likelihood of each random incident type (hard crashes are rarer).
INCIDENT_WEIGHTS = (0.35, 0.25, 0.30, 0.10)


@dataclass
class Incident:
    type: str
    remaining_ticks: int
    magnitude: float = 1.0


class SimulatedBankInfrastructure:
    def __init__(self, topology: Topology, seed: int | None = 42,
                 incident_rate: float = 0.0003, tick_s: float = 1.0) -> None:
        self.topology = topology
        self.graph = TopologyGraph(topology)
        self.rng = random.Random(seed)
        self.incident_rate = incident_rate
        self.tick_s = tick_s
        self.t = 0
        self.incidents: dict[str, Incident] = {}
        self._mem: dict[str, float] = {n.id: self.rng.uniform(35, 55) for n in topology.nodes}
        # Dependencies are evaluated before dependents.
        try:
            self._order = list(reversed(list(nx.topological_sort(self.graph.g))))
        except nx.NetworkXUnfeasible:  # cyclic topology - fall back to declaration order
            self._order = [n.id for n in topology.nodes]

    # ------------------------------------------------------------------ control
    def inject_fault(self, node_id: str, type_: str = "crash", duration_ticks: int = 30,
                     magnitude: float = 1.0) -> None:
        if node_id not in self.graph:
            raise KeyError(node_id)
        if type_ not in INCIDENT_TYPES:
            raise ValueError(f"Unknown incident type {type_!r}")
        self.incidents[node_id] = Incident(type_, duration_ticks, magnitude)

    # -------------------------------------------------------------------- tick
    def step(self) -> list[MetricSample]:
        self.t += 1
        now = datetime.now(UTC)
        # Diurnal factor: one simulated "day" = 1440 ticks.
        diurnal = 0.75 + 0.35 * math.sin(2 * math.pi * (self.t % 1440) / 1440 - math.pi / 2) + 0.1
        out: dict[str, dict[str, float]] = {}

        for node_id in self._order:
            node = self.graph.node(node_id)
            self._maybe_start_incident(node_id)
            inc = self.incidents.get(node_id)

            base_rps = float(node.attributes.get("base_rps", 300))
            base_lat = float(node.attributes.get("base_latency_ms", 20))
            max_rps = float(node.capacity.get("max_rps", 1000))

            rps = max(0.0, base_rps * diurnal * self.rng.gauss(1.0, 0.05))
            util = min(rps / max_rps, 1.5)
            cpu = 12 + 70 * util + self.rng.gauss(0, 2.5)
            mem = self._mem[node_id] + self.rng.gauss(0, 0.4)
            latency = base_lat * (1 + 2.0 * util**3) * self.rng.gauss(1.0, 0.04)
            error_rate = max(0.0, self.rng.gauss(0.002, 0.001))
            availability = 1.0

            # Upstream contagion: dependents suffer from unhealthy dependencies.
            for dep in self.graph.dependencies(node_id):
                d = out.get(dep)
                if not d:
                    continue
                w = self.graph.edge(node_id, dep).weight
                dep_lat_ratio = d["latency_ms"] / max(
                    float(self.graph.node(dep).attributes.get("base_latency_ms", 20)), 0.1)
                if d["availability"] < 1.0:
                    error_rate += 0.25 * w
                    latency *= 1 + 1.5 * w
                elif dep_lat_ratio > 2.0:
                    latency *= 1 + 0.15 * w * min(dep_lat_ratio, 6)
                error_rate += d["error_rate"] * 0.5 * w

            if inc:
                cpu, mem, latency, error_rate, availability, rps = self._apply_incident(
                    inc, cpu, mem, latency, error_rate, availability, rps)
                if inc.type == "memory_leak":
                    self._mem[node_id] = min(self._mem[node_id] + 0.6 * inc.magnitude, 99)
                inc.remaining_ticks -= 1
                if inc.remaining_ticks <= 0:
                    del self.incidents[node_id]
                    if inc.type == "memory_leak":
                        self._mem[node_id] = self.rng.uniform(35, 55)  # restart clears leak

            if node.kind in (NodeKind.SWITCH, NodeKind.FIREWALL, NodeKind.LOAD_BALANCER):
                mem = min(mem, 60)

            out[node_id] = {
                "cpu_pct": _clip(cpu, 0, 100),
                "mem_pct": _clip(mem, 0, 100),
                "latency_ms": round(max(latency, 0.1), 2),
                "error_rate": round(_clip(error_rate, 0, 1), 4),
                "rps": round(rps, 1),
                "availability": availability,
            }

        return [MetricSample(node_id=nid, ts=now, metrics=m) for nid, m in out.items()]

    # ----------------------------------------------------------------- helpers
    def _maybe_start_incident(self, node_id: str) -> None:
        if node_id in self.incidents or self.rng.random() >= self.incident_rate:
            return
        self.incidents[node_id] = Incident(
            type=self.rng.choices(INCIDENT_TYPES, weights=INCIDENT_WEIGHTS)[0],
            remaining_ticks=self.rng.randint(15, 90),
            magnitude=self.rng.uniform(0.6, 1.0),
        )

    @staticmethod
    def _apply_incident(inc: Incident, cpu: float, mem: float, latency: float,
                        error_rate: float, availability: float, rps: float):
        m = inc.magnitude
        if inc.type == "cpu_spike":
            cpu = max(cpu, 85 + 14 * m)
            latency *= 1 + 2 * m
        elif inc.type == "latency":
            latency *= 3 + 4 * m
            error_rate += 0.02 * m
        elif inc.type == "crash":
            availability = 0.0
            error_rate = 1.0
            rps = 0.0
            cpu = 0.0
        return cpu, mem, latency, error_rate, availability, rps


def _clip(v: float, lo: float, hi: float) -> float:
    return round(min(max(v, lo), hi), 2)
