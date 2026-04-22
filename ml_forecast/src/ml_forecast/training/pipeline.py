"""Training pipeline — the single entry point for ``train(ticker, timeframe)``.

Responsibilities:

1. Load + validate archive CSV (via :mod:`features.validators`).
2. Run walk-forward hyperparameter search for ARMAExo (primary) via
   :mod:`training.hyperparam`.
3. **Promotion gating (FR-022)**: fit NaiveBaseline on the same
   walk-forward splits; reject candidate if ``new_r2 < naive_r2``
   or ``new_r2 < 0``.
4. **Family switch (FR-025)**: if ARMAExo misses SC-001 thresholds,
   run the same search for LightGBM; promote whichever has the
   lower aggregate MAPE.
5. Persist artifact + TrainingRun row via ModelRegistry.
6. Return :class:`TrainingResult` with metrics + promote-decision.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import insert, update

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.features.ohlcv_features import FEATURE_SET_VERSION
from ml_forecast.features.validators import validate_ohlcv_csv
from ml_forecast.inference import registry
from ml_forecast.models.armaexo import ARMAExoForecaster
from ml_forecast.models.base import BaseForecaster, ModelFamily
from ml_forecast.models.baselines import NaiveBaseline
from ml_forecast.models.lightgbm_model import LightGBMForecaster
from ml_forecast.storage.orm import TrainingRun
from ml_forecast.storage.postgres import session_scope
from ml_forecast.training.hyperparam import TuneResult, tune
from ml_forecast.training.walkforward import WalkForwardValidator

log = logging.getLogger(__name__)


# Thresholds per SC-001 (spec) — aggregate MAPE targets for primary family.
_SC_TARGET: dict[Timeframe, float] = {
    Timeframe.D1: 0.03,
    Timeframe.M15: 0.05,
    # Other timeframes fall back to the D1 target in practice until
    # v1.1 extends SC-001.
}

# Path template for archive CSV, relative to archive_dir.
_ARCHIVE_PATH = "{timeframe}/{ticker}_{timeframe}.csv"


@dataclass(frozen=True, slots=True)
class TrainingResult:
    training_run_id: int
    ticker: str
    timeframe: Timeframe
    family: ModelFamily
    best_params: dict[str, Any]
    aggregate_metrics: dict[str, float]
    naive_metrics: dict[str, float]
    comparison_to_prev: dict[str, float] | None
    promote_decision: str  # shadow | do_not_promote | family_switch_shadow
    model_version: str | None
    duration_seconds: float
    error: str | None = None
    per_fold: list[dict[str, float]] = field(default_factory=list)


def _dataset_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_archive_csv(ticker: str, timeframe: Timeframe) -> Path:
    root = get_settings().archive_dir
    return root / _ARCHIVE_PATH.format(timeframe=timeframe.value, ticker=ticker)


def _evaluate_naive(df: pd.DataFrame, n_folds: int) -> dict[str, float]:
    validator = WalkForwardValidator(NaiveBaseline, n_folds=n_folds)
    result = validator.run(df)
    return {
        "mape": result.aggregate.mape,
        "rmse": result.aggregate.rmse,
        "mae": result.aggregate.mae,
        "r2": result.aggregate.r2,
        "directional_accuracy": result.aggregate.directional_accuracy,
    }


def _evaluate_candidate(
    df: pd.DataFrame,
    family: ModelFamily,
    params: dict[str, Any],
    n_folds: int,
) -> tuple[dict[str, float], list[dict[str, float]]]:
    """Re-run walk-forward with the tuned params to collect per-fold + agg metrics."""

    def factory() -> BaseForecaster:
        if family is ModelFamily.ARMAEXO:
            return ARMAExoForecaster(
                ar_order=int(params["ar_order"]), ma_order=int(params["ma_order"])
            )
        return LightGBMForecaster(
            num_leaves=int(params["num_leaves"]),
            learning_rate=float(params["learning_rate"]),
            n_estimators=int(params["n_estimators"]),
        )

    result = WalkForwardValidator(factory, n_folds=n_folds).run(df)
    agg = {
        "mape": result.aggregate.mape,
        "rmse": result.aggregate.rmse,
        "mae": result.aggregate.mae,
        "r2": result.aggregate.r2,
        "directional_accuracy": result.aggregate.directional_accuracy,
    }
    per_fold = [
        {
            "fold": f.fold,
            "mape": f.mape,
            "rmse": f.rmse,
            "mae": f.mae,
            "r2": f.r2,
            "directional_accuracy": f.directional_accuracy,
        }
        for f in result.per_fold
    ]
    return agg, per_fold


def _build_forecaster(family: ModelFamily, params: dict[str, Any]) -> BaseForecaster:
    if family is ModelFamily.ARMAEXO:
        return ARMAExoForecaster(
            ar_order=int(params["ar_order"]), ma_order=int(params["ma_order"])
        )
    return LightGBMForecaster(
        num_leaves=int(params["num_leaves"]),
        learning_rate=float(params["learning_rate"]),
        n_estimators=int(params["n_estimators"]),
    )


def _previous_production_metrics(ticker: str, timeframe: Timeframe) -> dict[str, float] | None:
    try:
        handle = registry.get_production(ticker, timeframe)
    except registry.ModelNotFound:
        return None
    if handle.current_mape is None:
        return None
    return {"mape": float(handle.current_mape)}


def _resolve_ticker_id(ticker: str) -> int:
    from sqlalchemy import text

    with session_scope() as s:
        row = s.execute(
            text("SELECT id FROM public.tickers WHERE symbol = :s"),
            {"s": ticker},
        ).first()
        if row is None:
            raise LookupError(f"ticker {ticker!r} not registered in public.tickers")
        return int(row[0])


def _insert_training_run_start(
    *,
    ticker_id: int,
    timeframe: Timeframe,
    family: ModelFamily,
    trigger: str,
) -> int:
    with session_scope() as s:
        result = s.execute(
            insert(TrainingRun)
            .values(
                ticker_id=ticker_id,
                timeframe=timeframe.value,
                model_family=family.value,
                status="running",
                trigger=trigger,
            )
            .returning(TrainingRun.id)
        )
        return int(result.scalar_one())


def _finish_training_run(
    *,
    training_run_id: int,
    status: str,
    promote_decision: str,
    aggregate: dict[str, float] | None,
    per_fold: list[dict[str, float]],
    comparison_to_prev: dict[str, float] | None,
    hyperparams: dict[str, Any],
    model_id: int | None,
    duration_seconds: float,
    error: str | None,
) -> None:
    with session_scope() as s:
        s.execute(
            update(TrainingRun)
            .where(TrainingRun.id == training_run_id)
            .values(
                status=status,
                finished_at=datetime.utcnow(),
                metrics_aggregate=aggregate,
                metrics_fold=per_fold or None,
                comparison_to_prev=comparison_to_prev,
                promote_decision=promote_decision,
                hyperparams=hyperparams,
                model_id=model_id,
                duration_seconds=int(duration_seconds),
                error=error,
            )
        )


def run(
    ticker: str,
    timeframe: Timeframe,
    trigger: str = "manual",
) -> TrainingResult:
    """Train a model for ``(ticker, timeframe)`` from the archive CSV."""

    started = time.perf_counter()
    csv_path = _resolve_archive_csv(ticker, timeframe)
    df = validate_ohlcv_csv(csv_path)
    dataset_sha = _dataset_sha256(csv_path)
    ticker_id = _resolve_ticker_id(ticker)

    n_folds = get_settings().walkforward_folds
    run_id = _insert_training_run_start(
        ticker_id=ticker_id,
        timeframe=timeframe,
        family=ModelFamily.ARMAEXO,
        trigger=trigger,
    )

    try:
        # 1) Tune ARMAExo first.
        primary: TuneResult = tune(ModelFamily.ARMAEXO, df, n_folds=n_folds)
        primary_agg, primary_folds = _evaluate_candidate(
            df, ModelFamily.ARMAEXO, primary.best_params, n_folds
        )

        # 2) Establish a naive baseline on the same folds for FR-022 gating.
        naive_metrics = _evaluate_naive(df, n_folds)

        # 3) Compare to the current production model (if any) for FR-009.
        prev_metrics = _previous_production_metrics(ticker, timeframe)
        comparison: dict[str, float] = {
            "vs_naive_r2_delta": primary_agg["r2"] - naive_metrics["r2"],
            "vs_naive_mape_delta": primary_agg["mape"] - naive_metrics["mape"],
        }
        if prev_metrics is not None:
            comparison["vs_previous_mape_delta"] = (
                primary_agg["mape"] - prev_metrics["mape"]
            )

        # 4) Gate: R² must beat naive AND be positive.
        if primary_agg["r2"] < naive_metrics["r2"] or primary_agg["r2"] < 0:
            log.warning(
                "ARMAExo for %s/%s failed naive R² gate "
                "(new=%.4f, naive=%.4f); marking do_not_promote",
                ticker, timeframe.value, primary_agg["r2"], naive_metrics["r2"],
            )
            _finish_training_run(
                training_run_id=run_id,
                status="succeeded",
                promote_decision="do_not_promote",
                aggregate=primary_agg,
                per_fold=primary_folds,
                comparison_to_prev=comparison,
                hyperparams=primary.best_params,
                model_id=None,
                duration_seconds=time.perf_counter() - started,
                error=None,
            )
            return TrainingResult(
                training_run_id=run_id,
                ticker=ticker,
                timeframe=timeframe,
                family=ModelFamily.ARMAEXO,
                best_params=primary.best_params,
                aggregate_metrics=primary_agg,
                naive_metrics=naive_metrics,
                comparison_to_prev=comparison,
                promote_decision="do_not_promote",
                model_version=None,
                duration_seconds=time.perf_counter() - started,
                per_fold=primary_folds,
            )

        # 5) Family switch if SC-001 miss.
        sc_target = _SC_TARGET.get(timeframe, _SC_TARGET[Timeframe.D1])
        chosen_family = ModelFamily.ARMAEXO
        chosen_params = primary.best_params
        chosen_agg = primary_agg
        chosen_folds = primary_folds
        if primary_agg["mape"] > sc_target:
            log.info(
                "ARMAExo for %s/%s MAPE=%.4f > SC-001 target %.4f — trying LightGBM",
                ticker, timeframe.value, primary_agg["mape"], sc_target,
            )
            fallback = tune(ModelFamily.LIGHTGBM, df, n_folds=n_folds)
            fb_agg, fb_folds = _evaluate_candidate(
                df, ModelFamily.LIGHTGBM, fallback.best_params, n_folds
            )
            if fb_agg["mape"] < chosen_agg["mape"]:
                chosen_family = ModelFamily.LIGHTGBM
                chosen_params = fallback.best_params
                chosen_agg = fb_agg
                chosen_folds = fb_folds
                comparison["family_switch"] = {  # type: ignore[assignment]
                    "from": "armaexo",
                    "to": "lightgbm",
                    "delta_mape": primary_agg["mape"] - fb_agg["mape"],
                }
                trigger = "family_switch"

        # 6) Fit the chosen configuration on the full dataset and register.
        final_forecaster = _build_forecaster(chosen_family, chosen_params).fit(df)
        handle = registry.register_shadow(
            registry.RegistrationInput(
                ticker_id=ticker_id,
                ticker=ticker,
                timeframe=timeframe,
                model_family=chosen_family,
                model=final_forecaster,
                dataset_sha256=dataset_sha,
                feature_set_version=FEATURE_SET_VERSION,
                metrics_aggregate=chosen_agg,
                metrics_fold=chosen_folds,
                comparison_to_prev=comparison,
            )
        )
        _finish_training_run(
            training_run_id=run_id,
            status="succeeded",
            promote_decision="auto_shadow",
            aggregate=chosen_agg,
            per_fold=chosen_folds,
            comparison_to_prev=comparison,
            hyperparams=chosen_params,
            model_id=handle.id,
            duration_seconds=time.perf_counter() - started,
            error=None,
        )
        return TrainingResult(
            training_run_id=run_id,
            ticker=ticker,
            timeframe=timeframe,
            family=chosen_family,
            best_params=chosen_params,
            aggregate_metrics=chosen_agg,
            naive_metrics=naive_metrics,
            comparison_to_prev=comparison,
            promote_decision="auto_shadow",
            model_version=handle.model_version,
            duration_seconds=time.perf_counter() - started,
            per_fold=chosen_folds,
        )

    except Exception as exc:  # noqa: BLE001
        log.exception("training pipeline failed for %s/%s", ticker, timeframe.value)
        _finish_training_run(
            training_run_id=run_id,
            status="failed",
            promote_decision="",
            aggregate=None,
            per_fold=[],
            comparison_to_prev=None,
            hyperparams={},
            model_id=None,
            duration_seconds=time.perf_counter() - started,
            error=str(exc),
        )
        raise
