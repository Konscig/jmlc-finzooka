"""gRPC ForecastServicer — orchestrates inference.

Flow (FR-001, FR-003, FR-004, FR-005, FR-015):

1. Validate request args (ticker/timeframe/horizon; ticker resolvable;
   horizon ∈ [1, max]; history ≥ 500 bars).
2. Freshness guards — reject on stale OHLCV; fallback path on stale
   sentiment.
3. Load current production model from the registry.
4. Fetch bars from Redis, assemble the feature slice.
5. Call ``model.predict(df, horizon)`` and ``model.factor_contributions``.
6. Compute SL/TP from ATR-14 (1.5× / 3×).
7. Render explanation (raises Unexplainable / ForbiddenPhrase).
8. Compute advisory flags (model_stale, outside_trading_hours,
   anomalous_last_bar).
9. Persist :class:`InferenceLog` row.
10. Return :class:`ForecastResponse`.

All exceptional paths set a gRPC status code + detail string that
matches the contract comments in ml_forecast.proto.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TYPE_CHECKING

import grpc
import pandas as pd
import yaml
from sqlalchemy import text as sqltext

from ml_forecast.config import get_settings
from ml_forecast.domain.factor import (
    FactorContribution,
    FactorStatus,
    SourceAvailability,
    SourceFreshness,
)
from ml_forecast.domain.forecast import AdvisoryFlags, ForecastStatus, PricePoint
from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.features.ohlcv_features import atr_14 as atr_14_fn
from ml_forecast.features.sentiment_features import sentiment_score
from ml_forecast.features.validators import detect_anomalous_bar
from ml_forecast.inference import freshness, registry, shadow
from ml_forecast.inference.explain import (
    ExplanationForbiddenPhrase,
    RenderContext,
    Unexplainable,
    render,
)
from ml_forecast.observability import metrics as obs_metrics
from ml_forecast.storage import redis_client
from ml_forecast.storage.orm import InferenceLog
from ml_forecast.storage.postgres import session_scope

if TYPE_CHECKING:
    from ml_forecast.grpc_gen.finzooka.ml.v1 import (
        ml_forecast_pb2,
        ml_forecast_pb2_grpc,
    )


log = logging.getLogger(__name__)

_MIN_HISTORY: int = 500
_MAX_HORIZON: int = 14
_ATR_STOP_LOSS_MULT: float = 1.5
_ATR_TAKE_PROFIT_MULT: float = 3.0


class _RejectForecast(Exception):
    """Raised inside the happy path to short-circuit with a gRPC status."""

    def __init__(self, code: grpc.StatusCode, detail: str, status_key: str) -> None:
        self.code = code
        self.detail = detail
        self.status_key = status_key
        super().__init__(detail)


def _proto_modules() -> tuple[Any, Any]:
    """Import generated protobuf + gRPC stubs lazily.

    The grpc_gen package is populated by ``make proto`` (build time);
    importing at module load time would break tests that run before
    codegen has been executed.
    """

    from ml_forecast.grpc_gen.finzooka.ml.v1 import (  # noqa: WPS433
        ml_forecast_pb2,
        ml_forecast_pb2_grpc,
    )

    return ml_forecast_pb2, ml_forecast_pb2_grpc


# ---------------------------------------------------------------------------
# Advisory flags helpers
# ---------------------------------------------------------------------------

def _load_holidays() -> tuple[set[date], tuple[int, int, int, int]]:
    """Return (holiday-set, (session_start_h, start_m, end_h, end_m))."""

    path = Path(str(get_settings().moex_holidays_path))
    if not path.exists():
        # Test / container-without-holiday-file: assume no holidays, default session.
        return set(), (10, 0, 18, 50)
    raw = yaml.safe_load(path.read_text())
    holidays = {date.fromisoformat(str(d)) for d in raw.get("holidays", [])}
    start_h, start_m = map(int, str(raw.get("session_start", "10:00")).split(":"))
    end_h, end_m = map(int, str(raw.get("session_end", "18:50")).split(":"))
    return holidays, (start_h, start_m, end_h, end_m)


def _outside_trading_hours(now_msk: datetime, holidays: set[date], session: tuple[int, int, int, int]) -> bool:
    if now_msk.weekday() >= 5:  # Saturday / Sunday
        return True
    if now_msk.date() in holidays:
        return True
    start_h, start_m, end_h, end_m = session
    start = now_msk.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now_msk.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return not (start <= now_msk <= end)


def _msk_now() -> datetime:
    # Europe/Moscow is UTC+3 with no DST since 2014.
    from datetime import timedelta as _td

    return datetime.now(tz=timezone.utc).astimezone(timezone(_td(hours=3))).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Servicer
# ---------------------------------------------------------------------------

class ForecastServicer:  # inherits MlForecastServicer via monkey-patch in main
    """Concrete implementation of ``finzooka.ml.v1.MlForecast/Forecast``."""

    def Forecast(self, request: Any, context: grpc.ServicerContext) -> Any:
        pb2, _ = _proto_modules()
        request_id = uuid.uuid4()
        started = time.perf_counter()

        ticker = str(request.ticker).upper().strip()
        horizon = int(request.horizon)
        try:
            timeframe = Timeframe(_timeframe_name(request.timeframe))
        except (KeyError, ValueError):
            return _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                "timeframe_unspecified",
                pb2,
                request_id,
                ticker,
                "",
                horizon,
                started,
            )

        try:
            response = self._do_forecast(
                pb2=pb2,
                context=context,
                request_id=request_id,
                ticker=ticker,
                timeframe=timeframe,
                horizon=horizon,
                started=started,
            )
            latency = time.perf_counter() - started
            outcome = "ok" if response.status == pb2.FORECAST_STATUS_OK else "degraded"
            obs_metrics.forecast_total.labels(status=outcome).inc()
            obs_metrics.forecast_latency_seconds.labels(status=outcome).observe(latency)
            return response
        except _RejectForecast as exc:
            latency = time.perf_counter() - started
            obs_metrics.forecast_total.labels(status=exc.status_key).inc()
            obs_metrics.forecast_latency_seconds.labels(status=exc.status_key).observe(latency)
            return _abort(
                context,
                exc.code,
                exc.status_key,
                pb2,
                request_id,
                ticker,
                timeframe.value,
                horizon,
                started,
                error_detail=exc.detail,
            )

    # ------------------------------------------------------------------
    # main pipeline
    # ------------------------------------------------------------------

    def _do_forecast(
        self,
        *,
        pb2: Any,
        context: grpc.ServicerContext,
        request_id: uuid.UUID,
        ticker: str,
        timeframe: Timeframe,
        horizon: int,
        started: float,
    ) -> Any:
        if horizon <= 0 or horizon > _MAX_HORIZON:
            raise _RejectForecast(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"horizon {horizon} outside supported range [1, {_MAX_HORIZON}]",
                "horizon_out_of_range",
            )

        # 1) freshness guards ----------------------------------------------------
        ohlcv_status = freshness.check_ohlcv(ticker, timeframe)
        if ohlcv_status is SourceFreshness.UNAVAILABLE:
            raise _RejectForecast(
                grpc.StatusCode.UNAVAILABLE,
                f"OHLCV key missing for {ticker}/{timeframe.value}",
                "source_unavailable",
            )
        if ohlcv_status is SourceFreshness.STALE:
            raise _RejectForecast(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"OHLCV bar stale for {ticker}/{timeframe.value}",
                "stale_ohlcv",
            )
        sentiment_status = freshness.check_sentiment(ticker)

        # 2) production model ---------------------------------------------------
        try:
            handle = registry.get_production(ticker, timeframe)
        except registry.ModelNotFound as exc:
            raise _RejectForecast(
                grpc.StatusCode.NOT_FOUND,
                str(exc),
                "model_not_found",
            ) from exc
        forecaster = registry.load_forecaster(handle)

        # 3) load recent bars from Redis ----------------------------------------
        snap = redis_client.get_ohlcv_last(ticker, timeframe)
        df = _bars_to_df(snap.bars)
        if len(df) < _MIN_HISTORY:
            raise _RejectForecast(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"only {len(df)} bars available, need ≥{_MIN_HISTORY}",
                "insufficient_history",
            )

        # 4) predict ----------------------------------------------------------
        predicted_path = forecaster.predict(df, horizon=horizon)
        raw_factors = forecaster.factor_contributions(df)
        # Overlay sentiment into factors[] so multi-factor compliance is visible.
        sentiment_feat = sentiment_score(ticker)
        sentiment_factor = FactorContribution(
            name="sentiment_score",
            value=sentiment_feat.value,
            contribution=Decimal("0.0"),  # magnitude known only with a model trained on sentiment
            source="sentiment",
            status=sentiment_feat.status
            if sentiment_status is not SourceFreshness.STALE
            else FactorStatus.STALE,
        )
        factors: list[FactorContribution] = list(raw_factors) + [sentiment_factor]

        # 5) SL/TP from ATR-14 ------------------------------------------------
        atr = atr_14_fn(df, len(df) - 1)
        last_close = float(df["close"].iloc[-1])
        suggested_stop = last_close - _ATR_STOP_LOSS_MULT * atr
        suggested_take = last_close + _ATR_TAKE_PROFIT_MULT * atr

        # 6) explanation + forbidden filter -----------------------------------
        ctx = RenderContext(
            current_mape=handle.current_mape,
            last_close=Decimal(str(last_close)),
        )
        tentative_forecast = _DummyForecast(
            ticker=ticker,
            timeframe=timeframe,
            horizon=horizon,
            predicted_path=tuple(predicted_path),
            factors=tuple(factors),
        )
        try:
            explanation = render(tentative_forecast, ctx)  # type: ignore[arg-type]
        except Unexplainable as exc:
            raise _RejectForecast(
                grpc.StatusCode.FAILED_PRECONDITION, str(exc), "unexplainable"
            ) from exc
        except ExplanationForbiddenPhrase as exc:
            raise _RejectForecast(
                grpc.StatusCode.INTERNAL, str(exc), "forbidden_phrase"
            ) from exc

        # 7) advisory flags (I1/I2/I3) ----------------------------------------
        holidays, session = _load_holidays()
        now_msk = _msk_now()
        model_age_days = (
            (datetime.utcnow() - handle.promoted_at).days if handle.promoted_at else 0
        )
        advisory = AdvisoryFlags(
            model_stale=model_age_days > get_settings().model_stale_days,
            outside_trading_hours=_outside_trading_hours(now_msk, holidays, session),
            anomalous_last_bar=detect_anomalous_bar(df, len(df) - 1),
        )

        # 8) overall response status ------------------------------------------
        response_status = (
            ForecastStatus.DEGRADED
            if sentiment_status is not SourceFreshness.FRESH
            else ForecastStatus.OK
        )
        availability = SourceAvailability(
            ohlcv=ohlcv_status, sentiment=sentiment_status
        )

        # 9) persist inference_log -------------------------------------------
        latency_ms = int((time.perf_counter() - started) * 1000)
        _write_inference_log(
            request_id=request_id,
            ticker_id=_lookup_ticker_id(ticker),
            timeframe=timeframe,
            horizon=horizon,
            model_id=handle.id,
            status="ok" if response_status is ForecastStatus.OK else "degraded",
            latency_ms=latency_ms,
            mape_at_generation=handle.current_mape,
            source_availability={
                "ohlcv": ohlcv_status.value,
                "sentiment": sentiment_status.value,
                "model_stale": advisory.model_stale,
                "outside_trading_hours": advisory.outside_trading_hours,
                "anomalous_last_bar": advisory.anomalous_last_bar,
            },
            predicted_path=predicted_path,
            factors=factors,
            explanation=explanation,
        )

        # 10) enqueue shadow predictions (fire-and-forget, T060) ------------
        shadow.enqueue_for_request(
            request_id=str(request_id),
            ticker=ticker,
            timeframe=timeframe,
            horizon=horizon,
        )

        # 11) build ForecastResponse ----------------------------------------
        return _build_response(
            pb2=pb2,
            request_id=request_id,
            status=response_status,
            predicted_path=predicted_path,
            suggested_stop_loss=suggested_stop,
            suggested_take_profit=suggested_take,
            factors=factors,
            explanation=explanation,
            mape_at_generation=handle.current_mape,
            model_version=handle.model_version,
            generated_at=datetime.utcnow(),
            source_availability=availability,
            advisory=advisory,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timeframe_name(value: int) -> str:
    from ml_forecast.domain import timeframe as tf_module

    return tf_module.from_proto(int(value)).value


def _bars_to_df(bars: list[dict[str, Any]]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(bars)
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.sort_values("dt").set_index("dt")
    return df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[
        ["open", "high", "low", "close", "volume"]
    ].astype(float)


def _lookup_ticker_id(ticker: str) -> int:
    with session_scope() as s:
        row = s.execute(
            sqltext("SELECT id FROM public.tickers WHERE symbol = :s"),
            {"s": ticker},
        ).first()
        return int(row[0]) if row else 0


def _build_response(
    *,
    pb2: Any,
    request_id: uuid.UUID,
    status: ForecastStatus,
    predicted_path: list[PricePoint],
    suggested_stop_loss: float,
    suggested_take_profit: float,
    factors: list[FactorContribution],
    explanation: str,
    mape_at_generation: Decimal | None,
    model_version: str,
    generated_at: datetime,
    source_availability: SourceAvailability,
    advisory: AdvisoryFlags,
) -> Any:
    from google.protobuf import timestamp_pb2

    from ml_forecast.domain.factor import (
        factor_status_to_proto,
        source_freshness_to_proto,
    )

    def _ts(dt: datetime) -> timestamp_pb2.Timestamp:
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(dt)
        return ts

    resp = pb2.ForecastResponse(
        request_id=str(request_id),
        status=pb2.FORECAST_STATUS_OK
        if status is ForecastStatus.OK
        else pb2.FORECAST_STATUS_DEGRADED,
        suggested_stop_loss=float(suggested_stop_loss),
        suggested_take_profit=float(suggested_take_profit),
        explanation=explanation,
        mape_at_generation=float(mape_at_generation) if mape_at_generation is not None else 0.0,
        model_version=model_version,
        generated_at=_ts(generated_at),
        source_availability=pb2.SourceAvailability(
            ohlcv=source_freshness_to_proto(source_availability.ohlcv),
            sentiment=source_freshness_to_proto(source_availability.sentiment),
        ),
        model_stale=advisory.model_stale,
        outside_trading_hours=advisory.outside_trading_hours,
        anomalous_last_bar=advisory.anomalous_last_bar,
    )
    for point in predicted_path:
        pp = resp.predicted_path.add()
        pp.t.FromDatetime(point.t)
        pp.mean = float(point.mean)
        pp.lo = float(point.lo)
        pp.hi = float(point.hi)
    for factor in factors:
        fc = resp.factors.add()
        fc.name = factor.name
        fc.has_value = factor.value is not None
        fc.value = float(factor.value) if factor.value is not None else 0.0
        fc.contribution = float(factor.contribution)
        fc.source = factor.source
        fc.status = factor_status_to_proto(factor.status)
    return resp


def _write_inference_log(
    *,
    request_id: uuid.UUID,
    ticker_id: int,
    timeframe: Timeframe,
    horizon: int,
    model_id: int | None,
    status: str,
    latency_ms: int,
    mape_at_generation: Decimal | None,
    source_availability: dict[str, Any],
    predicted_path: list[PricePoint] | None = None,
    factors: list[FactorContribution] | None = None,
    explanation: str | None = None,
    error: str | None = None,
) -> None:
    path_json = (
        [
            {
                "t": p.t.isoformat(),
                "mean": float(p.mean),
                "lo": float(p.lo),
                "hi": float(p.hi),
            }
            for p in predicted_path
        ]
        if predicted_path
        else None
    )
    factors_json = (
        [
            {
                "name": f.name,
                "value": float(f.value) if f.value is not None else None,
                "contribution": float(f.contribution),
                "source": f.source,
                "status": f.status.value,
            }
            for f in factors
        ]
        if factors
        else None
    )
    try:
        with session_scope() as s:
            entry = InferenceLog(
                request_id=str(request_id),
                ticker_id=ticker_id,
                timeframe=timeframe.value,
                horizon=horizon,
                model_id=model_id,
                status=status,
                latency_ms=latency_ms,
                mape_at_generation=mape_at_generation,
                source_availability=source_availability,
                predicted_path=path_json,
                factors=factors_json,
                explanation=explanation,
                error=error,
            )
            s.add(entry)
    except Exception as exc:  # noqa: BLE001 — never fail inference because logging failed
        log.warning("failed to write inference_log row: %s", exc)


def _abort(
    context: grpc.ServicerContext,
    code: grpc.StatusCode,
    status_key: str,
    pb2: Any,
    request_id: uuid.UUID,
    ticker: str,
    timeframe_value: str,
    horizon: int,
    started: float,
    error_detail: str | None = None,
) -> Any:
    latency_ms = int((time.perf_counter() - started) * 1000)
    # Best-effort inference_log write; if ticker_id lookup fails (bad ticker
    # string), we still record the row with ticker_id=0 so observability
    # sees the attempt.
    _write_inference_log(
        request_id=request_id,
        ticker_id=_lookup_ticker_id(ticker) if ticker else 0,
        timeframe=Timeframe(timeframe_value) if timeframe_value else Timeframe.D1,
        horizon=horizon,
        model_id=None,
        status=status_key,
        latency_ms=latency_ms,
        mape_at_generation=None,
        source_availability={"reject_reason": status_key},
        error=error_detail,
    )
    context.set_code(code)
    context.set_details(error_detail or status_key)
    return pb2.ForecastResponse()


# A thin stand-in for Forecast used by render(); we only need the fields that
# the renderer reads (ticker / timeframe / horizon / predicted_path / factors).
class _DummyForecast:
    def __init__(
        self,
        *,
        ticker: str,
        timeframe: Timeframe,
        horizon: int,
        predicted_path: tuple[PricePoint, ...],
        factors: tuple[FactorContribution, ...],
    ) -> None:
        self.ticker = ticker
        self.timeframe = timeframe
        self.horizon = horizon
        self.predicted_path = predicted_path
        self.factors = factors
