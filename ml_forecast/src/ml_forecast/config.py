"""Service configuration loaded from environment (ML_* prefix).

All runtime knobs live here so tests can pin them without touching code.
Freshness thresholds and train budgets are surfaced as spec-visible
acceptance criteria — see spec.md FR-002b, FR-005, SC-005.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ML_",
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Infrastructure ----------------------------------------------------
    database_url: str = Field(
        default="postgresql://finzooka:finzooka@postgres:5432/finzooka",
        description="SQLAlchemy URL for the shared Finzooka PostgreSQL.",
    )
    redis_url: str = Field(default="redis://redis:6379/0")

    # ---- Paths -------------------------------------------------------------
    artifact_dir: Path = Field(default=Path("/data/models"))
    archive_dir: Path = Field(default=Path("/data/archive"))
    forbidden_phrases_path: Path = Field(
        default=Path("/app/config/forbidden_phrases.yaml")
    )
    moex_holidays_path: Path = Field(
        default=Path("/app/config/moex_holidays_2026.yaml")
    )

    # ---- Ports -------------------------------------------------------------
    grpc_port: int = Field(default=50051, ge=1, le=65535)
    metrics_port: int = Field(default=9100, ge=1, le=65535)
    grpc_max_workers: int = Field(default=8, ge=1, le=64)

    # ---- Logging -----------------------------------------------------------
    log_level: str = Field(default="INFO")

    # ---- Freshness (FR-002b) ----------------------------------------------
    ohlcv_max_age_multiplier: float = Field(default=1.0, gt=0.0)
    sentiment_max_age_seconds: int = Field(default=3600, gt=0)

    # ---- Training ----------------------------------------------------------
    train_max_duration_seconds: int = Field(default=600, gt=0)
    optuna_trials: int = Field(default=50, ge=1)
    optuna_timeout_seconds: int = Field(default=300, gt=0)
    walkforward_folds: int = Field(default=5, ge=2)

    # ---- Advisory flags ---------------------------------------------------
    model_stale_days: int = Field(default=14, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cleared in tests via ``get_settings.cache_clear()``.
    """

    return Settings()
