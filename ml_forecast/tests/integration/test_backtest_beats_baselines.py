"""T067 — integration test: backtest runs end-to-end and reports all three
metric sets.

Exact SC-009 regression gate (model beats baselines by ≥ 10%) lives
in T096 on real blue-chip archive data. This test only verifies the
pipeline wires together: synthetic random walk gives all three
engines roughly similar MAPE, so we assert "metrics are reported and
non-NaN" rather than a direction of improvement.
"""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference import registry
from ml_forecast.models.base import ModelFamily
from ml_forecast.models.baselines import NaiveBaseline

pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration tests require ML_DATABASE_URL (run inside docker-compose)",
)


def _synthetic_csv(path: Path, n: int = 400) -> None:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, size=n))
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, size=n)
    low = np.maximum(close - rng.uniform(0.1, 1.0, size=n), 0.5)
    open_ = close + rng.uniform(-0.3, 0.3, size=n)
    low = np.minimum(low, np.minimum(open_, close) - 0.01)
    high = np.maximum(high, np.maximum(open_, close) + 0.01)
    low = np.maximum(low, 0.1)
    volume = rng.integers(100, 10000, size=n).astype(float)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "datetime": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    ).to_csv(path, index=False)


def test_backtest_runs_end_to_end(tmp_path: Path) -> None:
    from ml_forecast.backtest import runner
    from ml_forecast.storage.postgres import session_scope

    get_settings.cache_clear()
    os.environ["ML_ARTIFACT_DIR"] = str(tmp_path / "models")
    os.environ["ML_ARCHIVE_DIR"] = str(tmp_path / "archive")
    get_settings.cache_clear()

    ticker = "BTEST"
    timeframe = Timeframe.D1

    _synthetic_csv(
        Path(os.environ["ML_ARCHIVE_DIR"]) / timeframe.value / f"{ticker}_{timeframe.value}.csv"
    )

    with session_scope() as s:
        s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.tickers "
                "(id BIGSERIAL PRIMARY KEY, symbol VARCHAR(20) UNIQUE NOT NULL, "
                "is_blue_chip BOOLEAN DEFAULT true, is_active BOOLEAN DEFAULT true)"
            )
        )
        s.execute(
            text(
                "INSERT INTO public.tickers (symbol) VALUES (:s) "
                "ON CONFLICT (symbol) DO NOTHING"
            ),
            {"s": ticker},
        )
        ticker_id = int(
            s.execute(
                text("SELECT id FROM public.tickers WHERE symbol = :s"),
                {"s": ticker},
            ).scalar_one()
        )

    # Register NaiveBaseline as production so backtest has something to pick up.
    df = pd.read_csv(
        Path(os.environ["ML_ARCHIVE_DIR"]) / timeframe.value / f"{ticker}_{timeframe.value}.csv",
        parse_dates=["datetime"],
    ).set_index("datetime")
    model = NaiveBaseline().fit(df)
    handle = registry.register_shadow(
        registry.RegistrationInput(
            ticker_id=ticker_id,
            ticker=ticker,
            timeframe=timeframe,
            model_family=ModelFamily.BASELINE_NAIVE,
            model=model,
            dataset_sha256="b" * 64,
            feature_set_version="v1",
            metrics_aggregate={"mape": 0.02},
        )
    )
    registry.promote(ticker, timeframe, handle.model_version)

    result = runner.run(
        ticker=ticker,
        timeframe=timeframe,
        period_start=date(2024, 9, 1),
        period_end=date(2024, 12, 1),
    )

    # All three engines produce finite metrics with a non-zero sample size.
    for mset in (result.model_metrics, result.naive_metrics, result.ohlcv_only_metrics):
        assert mset.sample_size > 0
        assert np.isfinite(mset.mape)
        assert np.isfinite(mset.rmse)
        assert np.isfinite(mset.directional_accuracy)

    assert result.backtest_report_id > 0
    assert len(result.signals_vs_actuals) == result.model_metrics.sample_size

    # Cleanup to keep the DB clean for re-runs.
    with session_scope() as s:
        s.execute(text("DELETE FROM ml.backtest_report WHERE model_id = :m"), {"m": handle.id})
        s.execute(
            text("DELETE FROM ml.model_registry WHERE ticker_id = :tid"),
            {"tid": ticker_id},
        )
        s.execute(text("DELETE FROM public.tickers WHERE symbol = :s"), {"s": ticker})
    shutil.rmtree(tmp_path, ignore_errors=True)
