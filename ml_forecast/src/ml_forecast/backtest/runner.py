"""Backtest runner — sliding one-step evaluation with 2 baselines.

For every bar ``t`` in the period, we:

1. Refit the production candidate on ``df.iloc[:t]``.
2. Produce ``horizon=1`` prediction.
3. Parallel-run :class:`NaiveBaseline` and :class:`OhlcvOnlyBaseline`
   on the same window — we want a like-for-like comparison (FR-011).
4. Record (predicted_mean, actual_close, timestamp) per engine.

Aggregate metrics match the walk-forward metric definitions
(:mod:`training.walkforward`) so admins can compare pipelines cleanly.

Anti-lookahead is inherited from the feature layer — no manual
masking needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from sqlalchemy import insert

from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference import registry
from ml_forecast.models.base import BaseForecaster
from ml_forecast.models.baselines import NaiveBaseline, OhlcvOnlyBaseline
from ml_forecast.storage.orm import BacktestReport
from ml_forecast.storage.postgres import session_scope

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    mape: float
    rmse: float
    mae: float
    r2: float
    directional_accuracy: float
    win_rate: float
    sample_size: int

    def as_dict(self) -> dict[str, float]:
        return {
            "mape": self.mape,
            "rmse": self.rmse,
            "mae": self.mae,
            "r2": self.r2,
            "directional_accuracy": self.directional_accuracy,
            "win_rate": self.win_rate,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    backtest_report_id: int
    model_metrics: BacktestMetrics
    naive_metrics: BacktestMetrics
    ohlcv_only_metrics: BacktestMetrics
    signals_vs_actuals: list[dict[str, float | str]]


def _compute_metrics(
    y_true: np.ndarray, y_pred: np.ndarray, prev_close: np.ndarray
) -> BacktestMetrics:
    if len(y_true) == 0:
        return BacktestMetrics(
            mape=float("nan"), rmse=float("nan"), mae=float("nan"),
            r2=float("nan"), directional_accuracy=float("nan"),
            win_rate=float("nan"), sample_size=0,
        )
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err**2)))
    nonzero = y_true != 0
    mape = (
        float(np.mean(np.abs(err[nonzero] / y_true[nonzero])))
        if nonzero.any()
        else float("nan")
    )
    ss_res = float(np.sum(err**2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float("nan") if ss_tot == 0 else 1.0 - ss_res / ss_tot

    pred_dir = np.sign(y_pred - prev_close)
    act_dir = np.sign(y_true - prev_close)
    dir_acc = float(np.mean(pred_dir == act_dir))

    # "Win" = predicted direction matched AND magnitude covered at least half
    # of what we predicted. Excludes cases where we predicted tiny moves.
    predicted_magnitude = np.abs(y_pred - prev_close)
    actual_move_in_pred_dir = (y_true - prev_close) * pred_dir
    wins = (pred_dir != 0) & (actual_move_in_pred_dir >= 0.5 * predicted_magnitude)
    win_rate = float(np.mean(wins)) if len(wins) else float("nan")

    return BacktestMetrics(
        mape=mape, rmse=rmse, mae=mae, r2=r2,
        directional_accuracy=dir_acc, win_rate=win_rate,
        sample_size=len(y_true),
    )


def run(
    ticker: str,
    timeframe: Timeframe,
    period_start: date,
    period_end: date,
    model_version: str | None = None,
) -> BacktestResult:
    """Execute a backtest over the given period and persist the report."""

    from ml_forecast.features.validators import validate_ohlcv_csv
    from ml_forecast.training.pipeline import _resolve_archive_csv

    # Resolve candidate model.
    if model_version is None:
        handle = registry.get_production(ticker, timeframe)
    else:
        handles = registry.list_models(
            ticker=ticker, timeframe=timeframe
        )
        handle = next(
            (h for h in handles if h.model_version == model_version), None
        )
        if handle is None:
            raise registry.ModelNotFound(
                f"{ticker}/{timeframe.value}#{model_version}"
            )

    csv_path = _resolve_archive_csv(ticker, timeframe)
    df = validate_ohlcv_csv(csv_path)
    # Timezone-naive daily bar timestamps are compared against date-typed
    # period bounds via .date().
    start_idx = df.index.searchsorted(pd.Timestamp(period_start), side="left")
    end_idx = df.index.searchsorted(pd.Timestamp(period_end), side="right")
    if start_idx <= 0 or end_idx <= start_idx:
        raise ValueError(
            f"backtest period {period_start}..{period_end} is outside the "
            f"archive range for {ticker}/{timeframe.value}"
        )

    def _evaluate(factory) -> tuple[BacktestMetrics, list[dict[str, float | str]]]:
        preds: list[float] = []
        actuals: list[float] = []
        prevs: list[float] = []
        rows: list[dict[str, float | str]] = []
        for t in range(start_idx, end_idx):
            history = df.iloc[:t]
            if len(history) < 220:
                continue
            forecaster: BaseForecaster = factory().fit(history)
            prediction = forecaster.predict(history, horizon=1)[0]
            prev_close = float(df["close"].iloc[t - 1])
            actual = float(df["close"].iloc[t])
            pred_mean = float(prediction.mean)
            preds.append(pred_mean)
            actuals.append(actual)
            prevs.append(prev_close)
            rows.append(
                {
                    "t": str(df.index[t]),
                    "predicted": pred_mean,
                    "actual": actual,
                    "prev_close": prev_close,
                }
            )
        metrics = _compute_metrics(
            np.array(actuals), np.array(preds), np.array(prevs)
        )
        return metrics, rows

    log.info("backtest %s/%s period %s..%s", ticker, timeframe.value, period_start, period_end)

    def _model_factory() -> BaseForecaster:
        return registry.load_forecaster(handle)

    model_metrics, model_rows = _evaluate(_model_factory)
    naive_metrics, _ = _evaluate(NaiveBaseline)
    ohlcv_metrics, _ = _evaluate(OhlcvOnlyBaseline)

    # Persist.
    with session_scope() as s:
        row = BacktestReport(
            model_id=handle.id,
            period_start=period_start,
            period_end=period_end,
            metrics_model=model_metrics.as_dict(),
            metrics_naive=naive_metrics.as_dict(),
            metrics_ohlcv_only=ohlcv_metrics.as_dict(),
            signals_vs_actuals={"rows": model_rows},
        )
        s.add(row)
        s.flush()
        report_id = row.id

    return BacktestResult(
        backtest_report_id=int(report_id),
        model_metrics=model_metrics,
        naive_metrics=naive_metrics,
        ohlcv_only_metrics=ohlcv_metrics,
        signals_vs_actuals=model_rows,
    )
