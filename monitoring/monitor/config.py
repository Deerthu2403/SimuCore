"""Settings (MONITOR_* environment variables) and the list of monitored services."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", extra="ignore")

    source: Literal["prometheus", "direct", "synthetic"] = "direct"
    tick_s: float = Field(default=2.0, gt=0)
    window_s: float = Field(default=10.0, gt=0)  # period over which rates are computed
    infra_every: int = Field(default=3, ge=1)  # infrastructure is read every n-th tick
    prometheus_url: str = "http://127.0.0.1:9090"
    bank_url_template: str = "http://127.0.0.1:{port}"
    services_file: Path = ROOT / "banking-sim" / "dependency_map.json"
    database_url: str | None = None  # without it there is live data but no history
    retention_days: int = Field(default=30, ge=7)
    buffer_rows: int = 120_000
    seed: int = 42
    ws_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173",
                             "http://localhost:8080", "http://127.0.0.1:8080"]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    title: str
    node_id: str
    port: int
    uses_database: bool
    baseline_latency_ms: float
    targets: tuple[str, ...] = ()  # services it is declared to call


@lru_cache
def load_services(path: Path) -> tuple[ServiceSpec, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    uses_db = {d["source"] for d in raw["dependencies"] if d["target"] == "database"}
    return tuple(
        ServiceSpec(name, s["title"], s["node_id"], s["port"],
                    name == "database" or name in uses_db, float(s["baseline_latency_ms"]),
                    tuple(d["target"] for d in raw["dependencies"] if d["source"] == name))
        for name, s in raw["services"].items())


def check_host(url: str, services: tuple[ServiceSpec, ...]) -> str:
    """The monitor reads only the simulated bank and its own Prometheus, nothing else."""
    allowed = {"127.0.0.1", "localhost", "::1", "host.docker.internal", "prometheus",
               *(f"bank-{s.name}" for s in services)}
    host = urlsplit(url).hostname
    if host not in allowed:
        raise ValueError(f"Refusing host {host!r}: only the simulated bank is monitored")
    return url.rstrip("/")
