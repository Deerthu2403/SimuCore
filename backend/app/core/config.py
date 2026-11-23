"""Typed application settings. All values can be overridden with SIMUCORE_* env vars."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIMUCORE_", env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # Persistence (used from Phase 2)
    database_url: str = "postgresql+asyncpg://simucore:simucore@localhost:5432/simucore"

    # Telemetry / synchronisation
    telemetry_adapter: str = "simulated"
    telemetry_interval_s: float = 1.0
    stale_after_intervals: int = 3
    random_seed: int | None = 42
    # Probability per node per tick of a random synthetic incident.
    # 0.0003 x 27 nodes at 1 s ticks ~= one incident every ~2 minutes.
    incident_rate: float = 0.0003

    # Simulated banking microservices (adapter "bank_services"; read-only)
    bank_url_template: str = "http://127.0.0.1:{port}"

    # Prometheus adapter (future production integration; read-only)
    prometheus_url: str = "http://localhost:9090"


@lru_cache
def get_settings() -> Settings:
    return Settings()
