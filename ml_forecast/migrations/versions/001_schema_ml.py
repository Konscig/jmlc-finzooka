"""initial ml schema — model_registry, training_run, inference_log,
shadow_prediction, backtest_report, classification_run.

Revision ID: 001_schema_ml
Revises:
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_schema_ml"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ml")
    # Ensure pgcrypto for gen_random_uuid() server-default on request_id.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---- ml.model_registry -------------------------------------------------
    op.create_table(
        "model_registry",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ticker_id", sa.BigInteger(), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("model_family", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="training",
        ),
        sa.Column("artifact_path", sa.Text()),
        sa.Column("artifact_sha256", sa.CHAR(length=64)),
        sa.Column("dataset_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("feature_set_version", sa.String(length=16), nullable=False),
        sa.Column("current_mape", sa.Numeric(6, 4)),
        sa.Column("current_win_rate", sa.Numeric(6, 4)),
        sa.Column("promoted_at", sa.DateTime()),
        sa.Column("archived_at", sa.DateTime()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")
        ),
        schema="ml",
    )
    op.create_index(
        "uq_model_registry_version",
        "model_registry",
        ["ticker_id", "timeframe", "model_version"],
        unique=True,
        schema="ml",
    )
    # Partial UK: ровно одна production-модель на пару (ticker, timeframe).
    op.create_index(
        "uq_model_registry_production",
        "model_registry",
        ["ticker_id", "timeframe"],
        unique=True,
        schema="ml",
        postgresql_where=sa.text("state = 'production'"),
    )
    op.create_index(
        "idx_model_registry_state", "model_registry", ["state"], schema="ml"
    )

    # ---- ml.training_run ---------------------------------------------------
    op.create_table(
        "training_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "model_id",
            sa.BigInteger(),
            sa.ForeignKey("ml.model_registry.id", ondelete="SET NULL"),
        ),
        sa.Column("ticker_id", sa.BigInteger(), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("model_family", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column(
            "hyperparams",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("metrics_fold", postgresql.JSONB()),
        sa.Column("metrics_aggregate", postgresql.JSONB()),
        sa.Column("comparison_to_prev", postgresql.JSONB()),
        sa.Column("promote_decision", sa.String(length=24)),
        sa.Column("error", sa.Text()),
        sa.Column("duration_seconds", sa.Integer()),
        schema="ml",
    )
    op.create_index(
        "idx_training_run_ticker_tf_time",
        "training_run",
        ["ticker_id", "timeframe", "started_at"],
        schema="ml",
    )
    op.create_index(
        "idx_training_run_status_time",
        "training_run",
        ["status", "started_at"],
        schema="ml",
    )

    # ---- ml.inference_log --------------------------------------------------
    op.create_table(
        "inference_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
            unique=True,
        ),
        sa.Column("ticker_id", sa.BigInteger(), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("horizon", sa.SmallInteger(), nullable=False),
        sa.Column(
            "model_id",
            sa.BigInteger(),
            sa.ForeignKey("ml.model_registry.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("mape_at_generation", sa.Numeric(6, 4)),
        sa.Column(
            "source_availability",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("predicted_path", postgresql.JSONB()),
        sa.Column("factors", postgresql.JSONB()),
        sa.Column("explanation", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("actual_path", postgresql.JSONB()),
        sa.Column("abs_error_pct", sa.Numeric(6, 4)),
        schema="ml",
    )
    op.create_index(
        "idx_inference_log_ticker_tf_time",
        "inference_log",
        ["ticker_id", "timeframe", "generated_at"],
        schema="ml",
    )
    op.create_index(
        "idx_inference_log_unresolved",
        "inference_log",
        ["resolved_at"],
        schema="ml",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "idx_inference_log_status", "inference_log", ["status"], schema="ml"
    )

    # ---- ml.shadow_prediction ---------------------------------------------
    op.create_table(
        "shadow_prediction",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "shadow_model_id",
            sa.BigInteger(),
            sa.ForeignKey("ml.model_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicted_path", postgresql.JSONB(), nullable=False),
        sa.Column("factors", postgresql.JSONB()),
        sa.Column("explanation", sa.Text()),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("actual_path", postgresql.JSONB()),
        sa.Column("abs_error_pct", sa.Numeric(6, 4)),
        schema="ml",
    )
    op.create_index(
        "idx_shadow_prediction_model_time",
        "shadow_prediction",
        ["shadow_model_id", "generated_at"],
        schema="ml",
    )
    op.create_index(
        "idx_shadow_prediction_request",
        "shadow_prediction",
        ["request_id"],
        schema="ml",
    )

    # ---- ml.backtest_report ------------------------------------------------
    op.create_table(
        "backtest_report",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "model_id",
            sa.BigInteger(),
            sa.ForeignKey("ml.model_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "executed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("metrics_model", postgresql.JSONB(), nullable=False),
        sa.Column("metrics_naive", postgresql.JSONB(), nullable=False),
        sa.Column("metrics_ohlcv_only", postgresql.JSONB(), nullable=False),
        sa.Column("signals_vs_actuals", postgresql.JSONB(), nullable=False),
        schema="ml",
    )
    op.create_index(
        "idx_backtest_report_model_time",
        "backtest_report",
        ["model_id", "executed_at"],
        schema="ml",
    )

    # ---- ml.classification_run --------------------------------------------
    op.create_table(
        "classification_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "executed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("feature_set_version", sa.String(length=16), nullable=False),
        sa.Column("total_tickers", sa.Integer(), nullable=False),
        sa.Column("blue_chip_recall", sa.Numeric(6, 4), nullable=False),
        sa.Column("other_recall", sa.Numeric(6, 4), nullable=False),
        sa.Column("confusion_matrix", postgresql.JSONB(), nullable=False),
        sa.Column("label_predictions", postgresql.JSONB(), nullable=False),
        schema="ml",
    )


def downgrade() -> None:
    op.drop_table("classification_run", schema="ml")
    op.drop_table("backtest_report", schema="ml")
    op.drop_table("shadow_prediction", schema="ml")
    op.drop_table("inference_log", schema="ml")
    op.drop_table("training_run", schema="ml")
    op.drop_table("model_registry", schema="ml")
    op.execute("DROP SCHEMA IF EXISTS ml")
