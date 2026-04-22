"""Shadow prediction mechanics (T058–T060).

Fire-and-forget Celery path:

1. :func:`enqueue_for_request` is called from ForecastServicer **after**
   the production response has been returned to the client so shadow
   work never blocks the critical path (research R7).

2. The Celery worker picks up ``ml_forecast.shadow_predict`` and calls
   :func:`run_shadow_predictions`, which re-fits each shadow-state
   model on the current history snapshot + persists predictions to
   ``ml.shadow_prediction``.

3. :func:`resolve_matured_predictions` runs every 15 min to fill
   ``actual_path`` + ``abs_error_pct`` once the forecast horizon has
   elapsed (spec acceptance of shadow-report comparison).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference import registry
from ml_forecast.storage import redis_client
from ml_forecast.storage.orm import ShadowPrediction
from ml_forecast.storage.postgres import session_scope

log = logging.getLogger(__name__)


def enqueue_for_request(
    request_id: str, ticker: str, timeframe: Timeframe, horizon: int
) -> None:
    """Publish a shadow_predict Celery task. Silently skips when Celery is
    not reachable (e.g. local dev without ml-worker) to avoid breaking
    the main forecast path (Principle V fail-open on observability)."""

    try:
        from ml_forecast.training.scheduler import shadow_predict

        shadow_predict.delay(
            request_id=str(request_id),
            ticker=ticker,
            timeframe=timeframe.value,
            horizon=int(horizon),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("shadow enqueue skipped (%s): %s", type(exc).__name__, exc)


def run_shadow_predictions(
    request_id: str, ticker: str, timeframe: Timeframe, horizon: int
) -> dict[str, Any]:
    """Executed in ml-worker. Runs every SHADOW model for the pair and
    persists a row per model."""

    # Read bars first; if Redis is behind, shadow just bails out.
    try:
        snap = redis_client.get_ohlcv_last(ticker, timeframe)
    except redis_client.RedisKeyMissing:
        log.info("shadow_predict: no OHLCV for %s/%s, skipping", ticker, timeframe.value)
        return {"request_id": request_id, "ran": 0}

    df = _snap_to_df(snap.bars)
    if df is None or len(df) < 220:
        return {"request_id": request_id, "ran": 0}

    shadow_handles = [
        h
        for h in registry.list_models(
            ticker=ticker, timeframe=timeframe, state=ModelState.SHADOW
        )
    ]
    if not shadow_handles:
        return {"request_id": request_id, "ran": 0}

    ran = 0
    for handle in shadow_handles:
        try:
            forecaster = registry.load_forecaster(handle)
            predicted = forecaster.predict(df, horizon=horizon)
            factors = forecaster.factor_contributions(df)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "shadow_predict failed for %s/%s#%s: %s",
                ticker, timeframe.value, handle.model_version, exc,
            )
            continue

        predicted_path = [
            {
                "t": p.t.isoformat(),
                "mean": float(p.mean),
                "lo": float(p.lo),
                "hi": float(p.hi),
            }
            for p in predicted
        ]
        factors_json = [
            {
                "name": f.name,
                "value": float(f.value) if f.value is not None else None,
                "contribution": float(f.contribution),
                "source": f.source,
                "status": f.status.value,
            }
            for f in factors
        ]
        with session_scope() as s:
            s.add(
                ShadowPrediction(
                    request_id=str(request_id),
                    shadow_model_id=handle.id,
                    predicted_path={"rows": predicted_path},
                    factors={"rows": factors_json},
                )
            )
        ran += 1

    return {"request_id": request_id, "ran": ran}


def resolve_matured_predictions() -> dict[str, Any]:
    """Fill actual_path for shadow rows whose last-predicted-t is in the past."""

    resolved = 0
    now = datetime.now(tz=timezone.utc)
    with session_scope() as s:
        rows = list(
            s.execute(
                select(ShadowPrediction)
                .where(ShadowPrediction.resolved_at.is_(None))
                .limit(500)
            ).scalars()
        )
        for row in rows:
            path_rows = (row.predicted_path or {}).get("rows") or []
            if not path_rows:
                continue
            last_t = path_rows[-1].get("t")
            try:
                last_dt = datetime.fromisoformat(str(last_t))
            except ValueError:
                continue
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if last_dt > now:
                continue
            # Production model's Redis cache carries the actual close; we
            # reuse it as the truth source.
            handle = _handle_for(s, row.shadow_model_id)
            if handle is None:
                continue
            try:
                snap = redis_client.get_ohlcv_last(
                    handle.ticker, handle.timeframe
                )
                actual = float(snap.bars[-1]["c"]) if snap.bars else None
            except Exception:  # noqa: BLE001
                actual = None
            if actual is None:
                continue
            predicted_mean = float(path_rows[-1].get("mean") or 0.0)
            abs_err = (
                abs(actual - predicted_mean) / max(actual, 1e-9)
                if predicted_mean
                else None
            )
            s.execute(
                update(ShadowPrediction)
                .where(ShadowPrediction.id == row.id)
                .values(
                    actual_path={"mean": actual},
                    abs_error_pct=Decimal(str(round(abs_err, 6))) if abs_err is not None else None,
                    resolved_at=datetime.utcnow(),
                )
            )
            resolved += 1
    return {"resolved": resolved}


def _snap_to_df(bars):
    import pandas as pd

    if not bars:
        return None
    df = pd.DataFrame(bars)
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.sort_values("dt").set_index("dt")
    return df.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )[["open", "high", "low", "close", "volume"]].astype(float)


def _handle_for(session, model_id: int) -> registry.ModelHandle | None:
    from ml_forecast.storage.orm import ModelRegistry

    row = session.get(ModelRegistry, model_id)
    if row is None:
        return None
    # Best-effort symbol resolution (same pattern as registry._resolve_ticker).
    from sqlalchemy import text

    sym_row = session.execute(
        text("SELECT symbol FROM public.tickers WHERE id = :tid"),
        {"tid": row.ticker_id},
    ).first()
    if sym_row is None:
        return None
    return registry.ModelHandle(
        id=row.id,
        ticker=str(sym_row[0]),
        timeframe=Timeframe(row.timeframe),
        model_version=row.model_version,
        model_family=registry.ModelFamily(row.model_family),
        state=ModelState(row.state),
        artifact_path=row.artifact_path,
        current_mape=row.current_mape,
        feature_set_version=row.feature_set_version,
        promoted_at=row.promoted_at,
        created_at=row.created_at,
    )
