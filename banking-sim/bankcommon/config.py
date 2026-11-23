"""Service registry (from dependency_map.json) and per-process settings (from BANK_* env vars)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Dependency:
    source: str
    target: str
    mode: str  # required | degradable | best_effort
    timeout_ms: int
    weight: float
    purpose: str


@dataclass(frozen=True)
class Registry:
    services: dict[str, dict]
    dependencies: tuple[Dependency, ...]

    def of(self, source: str) -> dict[str, Dependency]:
        return {d.target: d for d in self.dependencies if d.source == source}


@lru_cache
def registry() -> Registry:
    raw = json.loads((ROOT / "dependency_map.json").read_text(encoding="utf-8"))
    return Registry(raw["services"], tuple(Dependency(**d) for d in raw["dependencies"]))


def check_simulated_url(url: str) -> str:
    """Only loopback and the simulated services' own container names are reachable."""
    host = urlsplit(url).hostname
    allowed = LOOPBACK_HOSTS | {f"bank-{s}" for s in registry().services}
    if host not in allowed:
        raise ValueError(f"Refusing non-simulated host {host!r}: this bank stays in the sandbox")
    return url.rstrip("/")


def service_url(name: str) -> str:
    default = f"http://127.0.0.1:{registry().services[name]['port']}"
    return check_simulated_url(os.environ.get(f"BANK_URL_{name.upper()}", default))


@dataclass(frozen=True)
class Settings:
    internal_key: str
    data_dir: Path = field(default_factory=lambda: ROOT / ".runtime" / "data")
    seed: int = 42
    customers: int = 200
    db_pool_size: int = 8
    db_acquire_timeout_s: float = 0.75
    hold_ttl_s: float = 60.0
    mem_limit_mb: int = 256
    token_ttl_s: int = 900
    fraud_fallback_limit: int = 50_000  # minor units allowed unscreened when fraud is down
    faults_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        key = os.environ.get("BANK_INTERNAL_KEY", "")
        if len(key) < 16:
            raise RuntimeError("BANK_INTERNAL_KEY must be set (16+ characters)")
        env = os.environ.get
        return cls(
            internal_key=key,
            data_dir=Path(env("BANK_DATA_DIR", str(ROOT / ".runtime" / "data"))),
            seed=int(env("BANK_SEED", "42")),
            customers=int(env("BANK_CUSTOMERS", "200")),
            db_pool_size=int(env("BANK_DB_POOL_SIZE", "8")),
            hold_ttl_s=float(env("BANK_HOLD_TTL_S", "60")),
            mem_limit_mb=int(env("BANK_MEM_LIMIT_MB", "256")),
            faults_enabled=env("BANK_FAULTS_ENABLED", "true").lower() == "true",
        )
