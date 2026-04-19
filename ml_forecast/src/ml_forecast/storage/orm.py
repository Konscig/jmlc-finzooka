"""SQLAlchemy ORM models for the ``ml`` schema.

One class per owned table described in data-model.md §1. Shared
tables (``tickers``, ``model_metrics``, ``pipeline_statuses`` in
the default ``public`` schema) are referenced but not defined here —
their canonical schema lives in the backend feature.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ml_forecast.storage.postgres import Base


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    model_family: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="training"
    )
    artifact_path: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    dataset_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(16), nullable=False)
    current_mape: Mapped[float | None] = mapped_column(Numeric(6, 4))
    current_win_rate: Mapped[float | None] = mapped_column(Numeric(6, 4))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("uq_model_registry_version", "ticker_id", "timeframe", "model_version", unique=True),
        # partial UK to guarantee exactly one production model per (ticker, timeframe)
        Index(
            "uq_model_registry_production",
            "ticker_id",
            "timeframe",
            unique=True,
            postgresql_where=text("state = 'production'"),
        ),
        Index("idx_model_registry_state", "state"),
    )


class TrainingRun(Base):
    __tablename__ = "training_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ml.model_registry.id", ondelete="SET NULL")
    )
    ticker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    model_family: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="running"
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    hyperparams: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    metrics_fold: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metrics_aggregate: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    comparison_to_prev: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    promote_decision: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("idx_training_run_ticker_tf_time", "ticker_id", "timeframe", "started_at"),
        Index("idx_training_run_status_time", "status", "started_at"),
    )


class InferenceLog(Base):
    __tablename__ = "inference_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        nullable=False,
        server_default=text("gen_random_uuid()"),
        unique=True,
    )
    ticker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    horizon: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ml.model_registry.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    mape_at_generation: Mapped[float | None] = mapped_column(Numeric(6, 4))
    source_availability: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    predicted_path: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    factors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    explanation: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    actual_path: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    abs_error_pct: Mapped[float | None] = mapped_column(Numeric(6, 4))

    __table_args__ = (
        Index("idx_inference_log_ticker_tf_time", "ticker_id", "timeframe", "generated_at"),
        # Used by shadow_resolve / recompute_daily_metrics to find unresolved rows.
        Index(
            "idx_inference_log_unresolved",
            "resolved_at",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("idx_inference_log_status", "status"),
    )


class ShadowPrediction(Base):
    __tablename__ = "shadow_prediction"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    shadow_model_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ml.model_registry.id", ondelete="CASCADE"), nullable=False
    )
    predicted_path: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    factors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    explanation: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    actual_path: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    abs_error_pct: Mapped[float | None] = mapped_column(Numeric(6, 4))

    __table_args__ = (
        Index("idx_shadow_prediction_model_time", "shadow_model_id", "generated_at"),
        Index("idx_shadow_prediction_request", "request_id"),
    )


class BacktestReport(Base):
    __tablename__ = "backtest_report"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ml.model_registry.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    metrics_model: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics_naive: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics_ohlcv_only: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signals_vs_actuals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("idx_backtest_report_model_time", "model_id", "executed_at"),)


class ClassificationRun(Base):
    __tablename__ = "classification_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW()")
    )
    feature_set_version: Mapped[str] = mapped_column(String(16), nullable=False)
    total_tickers: Mapped[int] = mapped_column(Integer, nullable=False)
    blue_chip_recall: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    other_recall: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    confusion_matrix: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    label_predictions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


# Keep __all__ authoritative for alembic autogen discovery.
__all__ = [
    "BacktestReport",
    "ClassificationRun",
    "InferenceLog",
    "ModelRegistry",
    "ShadowPrediction",
    "TrainingRun",
]

# Silence mypy about imports that ARE needed at runtime.
_ = (Boolean,)
