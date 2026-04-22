"""Periodic observability jobs invoked from Celery beat."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from ml_forecast.domain.model_state import ModelState
from ml_forecast.inference import registry
from ml_forecast.observability import metrics as obs_metrics
from ml_forecast.storage import redis_client
from ml_forecast.storage.orm import InferenceLog
from ml_forecast.storage.postgres import get_engine, session_scope

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# T076 — recompute_daily_metrics
# ---------------------------------------------------------------------------

def _resolve_mature_inferences() -> int:
    """Fill actual_path / abs_error_pct for inference rows whose horizon has
    elapsed. Uses the archive/live close as truth.

    Returns the number of rows resolved.
    """

    resolved = 0
    with session_scope() as s:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        rows = list(
            s.execute(
                select(InferenceLog)
                .where(InferenceLog.resolved_at.is_(None))
                .where(InferenceLog.predicted_path.isnot(None))
                .where(InferenceLog.generated_at < cutoff)
                .limit(500)
            ).scalars()
        )
        for row in rows:
            path = row.predicted_path or []
            if not path:
                continue
            last_predicted = path[-1]
            predicted_mean = float(last_predicted.get("mean") or 0.0)
            if predicted_mean <= 0:
                continue
            # Cheap proxy: last close in Redis (production only has fresh bars).
            try:
                from ml_forecast.domain.timeframe import Timeframe

                snap = redis_client.get_ohlcv_last(
                    ticker=_ticker_symbol(s, row.ticker_id),
                    timeframe=Timeframe(row.timeframe),
                )
                actual_close = float(snap.bars[-1]["c"]) if snap.bars else None
            except Exception:  # noqa: BLE001
                actual_close = None
            if actual_close is None:
                continue
            abs_err = abs(actual_close - predicted_mean) / max(actual_close, 1e-9)
            row.actual_path = [{"mean": actual_close}]
            row.abs_error_pct = Decimal(str(round(abs_err, 6)))
            row.resolved_at = datetime.utcnow()
            resolved += 1
    return resolved


def _ticker_symbol(session, ticker_id: int) -> str:
    row = session.execute(
        text("SELECT symbol FROM public.tickers WHERE id = :tid"),
        {"tid": ticker_id},
    ).first()
    if row is None:
        raise LookupError(f"unknown ticker_id={ticker_id}")
    return str(row[0])


def recompute_daily_metrics() -> dict[str, Any]:
    """T076 — per production model: refresh current_mape from last 30
    resolved inferences + INSERT a row in public.model_metrics.

    Idempotent per (ticker_id, period_date) via ON CONFLICT DO UPDATE so
    re-runs during the day just overwrite the last snapshot.
    """

    _resolve_mature_inferences()
    today = date.today()
    updated: list[dict[str, Any]] = []
    engine = get_engine()
    with engine.begin() as conn:
        _ensure_model_metrics_table(conn)

    with session_scope() as s:
        for handle in registry.list_models(state=ModelState.PRODUCTION):
            rows = list(
                s.execute(
                    select(InferenceLog.abs_error_pct, InferenceLog.status)
                    .where(InferenceLog.model_id == handle.id)
                    .where(InferenceLog.resolved_at.isnot(None))
                    .where(InferenceLog.abs_error_pct.isnot(None))
                    .order_by(InferenceLog.generated_at.desc())
                    .limit(30)
                )
            )
            if not rows:
                continue
            mape = float(sum(float(r.abs_error_pct) for r in rows) / len(rows))
            profitable = sum(1 for r in rows if r.status in {"ok", "degraded"})
            win_rate = profitable / len(rows) if rows else 0.0
            registry.update_current_mape(handle.id, mape)
            obs_metrics.current_mape.labels(
                ticker=handle.ticker, timeframe=handle.timeframe.value
            ).set(mape)
            s.execute(
                text(
                    """
                    INSERT INTO public.model_metrics
                        (ticker_id, mape, win_rate, total_signals,
                         profitable_signals, avg_inference_ms, period_date,
                         calculated_at)
                    VALUES (:tid, :mape, :win, :total, :prof, 0, :d, NOW())
                    ON CONFLICT (ticker_id, period_date)
                    DO UPDATE SET mape = :mape, win_rate = :win,
                                  total_signals = :total,
                                  profitable_signals = :prof,
                                  calculated_at = NOW()
                    """
                ),
                {
                    "tid": _lookup_ticker_id(s, handle.ticker),
                    "mape": mape,
                    "win": win_rate,
                    "total": len(rows),
                    "prof": profitable,
                    "d": today,
                },
            )
            updated.append(
                {
                    "ticker": handle.ticker,
                    "timeframe": handle.timeframe.value,
                    "mape": mape,
                    "win_rate": win_rate,
                    "n": len(rows),
                }
            )
    log.info("recompute_daily_metrics: updated %d models", len(updated))
    return {"updated": updated, "count": len(updated)}


def _lookup_ticker_id(session, ticker: str) -> int:
    row = session.execute(
        text("SELECT id FROM public.tickers WHERE symbol = :s"),
        {"s": ticker},
    ).first()
    return int(row[0]) if row else 0


def _ensure_model_metrics_table(conn) -> None:
    """Create public.model_metrics on the fly so the job doesn't depend on
    the backend feature's migration order.
    """

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS public.model_metrics (
                id BIGSERIAL PRIMARY KEY,
                ticker_id BIGINT NOT NULL,
                mape NUMERIC(6, 4),
                win_rate NUMERIC(6, 4),
                total_signals INTEGER NOT NULL DEFAULT 0,
                profitable_signals INTEGER NOT NULL DEFAULT 0,
                avg_inference_ms INTEGER NOT NULL DEFAULT 0,
                period_date DATE NOT NULL,
                calculated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (ticker_id, period_date)
            )
            """
        )
    )


# ---------------------------------------------------------------------------
# T077 — health_check
# ---------------------------------------------------------------------------

def health_check() -> dict[str, Any]:
    """Ping Postgres + Redis, update public.pipeline_statuses."""

    postgres_ok = False
    redis_ok = False
    error_detail: str | None = None
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        postgres_ok = True
    except Exception as exc:  # noqa: BLE001
        error_detail = f"postgres: {exc}"
        log.error("health_check postgres failed: %s", exc)

    try:
        redis_ok = redis_client.ping()
        if not redis_ok:
            error_detail = (error_detail or "") + "; redis not reachable"
    except Exception as exc:  # noqa: BLE001
        error_detail = (error_detail or "") + f"; redis: {exc}"

    obs_metrics.health_probe.labels(component="postgres").set(1 if postgres_ok else 0)
    obs_metrics.health_probe.labels(component="redis").set(1 if redis_ok else 0)

    status = "healthy" if (postgres_ok and redis_ok) else "unhealthy"
    if postgres_ok:
        try:
            _upsert_pipeline_status(status, error_detail)
        except Exception as exc:  # noqa: BLE001
            log.error("health_check pipeline_status upsert failed: %s", exc)

    return {"status": status, "postgres_ok": postgres_ok, "redis_ok": redis_ok}


def _upsert_pipeline_status(status: str, error_detail: str | None) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.pipeline_statuses (
                    id BIGSERIAL PRIMARY KEY,
                    pipeline_name VARCHAR(255) UNIQUE NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    last_success_at TIMESTAMP,
                    last_failure_at TIMESTAMP,
                    last_error TEXT,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        if status == "healthy":
            conn.execute(
                text(
                    """
                    INSERT INTO public.pipeline_statuses
                        (pipeline_name, status, last_success_at, updated_at)
                    VALUES ('ml_forecast', 'healthy', NOW(), NOW())
                    ON CONFLICT (pipeline_name) DO UPDATE SET
                        status = 'healthy',
                        last_success_at = NOW(),
                        last_error = NULL,
                        updated_at = NOW()
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO public.pipeline_statuses
                        (pipeline_name, status, last_failure_at, last_error,
                         updated_at)
                    VALUES ('ml_forecast', :status, NOW(), :err, NOW())
                    ON CONFLICT (pipeline_name) DO UPDATE SET
                        status = :status,
                        last_failure_at = NOW(),
                        last_error = :err,
                        updated_at = NOW()
                    """
                ),
                {"status": status, "err": error_detail},
            )
