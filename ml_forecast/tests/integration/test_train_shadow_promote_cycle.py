"""T053 — full lifecycle integration: train → shadow → predict → promote.

Uses NaiveBaseline as the concrete forecaster so the test runs in
seconds; the business-grade ARMAExo + walk-forward + Optuna variant is
exercised indirectly via T054's pipeline in the SC-001 regression
gate (T096).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ml_forecast.config import get_settings
from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference import registry
from ml_forecast.models.base import ModelFamily
from ml_forecast.models.baselines import NaiveBaseline

pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration tests require ML_DATABASE_URL",
)


def _seed_db(tmp_path: Path) -> tuple[int, str]:
    get_settings.cache_clear()
    os.environ["ML_ARTIFACT_DIR"] = str(tmp_path / "models")
    get_settings.cache_clear()
    ticker = "CYCLE"
    from ml_forecast.storage.postgres import session_scope

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
    return ticker_id, ticker


def _synthetic_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(55)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=idx,
    )
    return df


def test_train_shadow_promote_cycle(tmp_path: Path) -> None:
    from ml_forecast.storage.postgres import session_scope

    ticker_id, ticker = _seed_db(tmp_path)
    df = _synthetic_df()

    first = registry.register_shadow(
        registry.RegistrationInput(
            ticker_id=ticker_id,
            ticker=ticker,
            timeframe=Timeframe.D1,
            model_family=ModelFamily.BASELINE_NAIVE,
            model=NaiveBaseline().fit(df),
            dataset_sha256="a" * 64,
            feature_set_version="v1",
            metrics_aggregate={"mape": 0.03},
        )
    )
    new, prev = registry.promote(ticker, Timeframe.D1, first.model_version)
    assert new.state == ModelState.PRODUCTION
    assert prev is None

    # Train a second candidate → shadow.
    second = registry.register_shadow(
        registry.RegistrationInput(
            ticker_id=ticker_id,
            ticker=ticker,
            timeframe=Timeframe.D1,
            model_family=ModelFamily.BASELINE_NAIVE,
            model=NaiveBaseline().fit(df.iloc[:-10]),
            dataset_sha256="b" * 64,
            feature_set_version="v1",
            metrics_aggregate={"mape": 0.025},
        )
    )
    assert second.state == ModelState.SHADOW
    # Promoting second must archive first atomically.
    new2, prev2 = registry.promote(ticker, Timeframe.D1, second.model_version)
    assert new2.state == ModelState.PRODUCTION
    assert prev2 is not None
    assert prev2.model_version == first.model_version
    archived = [
        h
        for h in registry.list_models(ticker=ticker, timeframe=Timeframe.D1)
        if h.state == ModelState.ARCHIVED
    ]
    assert any(h.model_version == first.model_version for h in archived)

    # Cleanup.
    with session_scope() as s:
        s.execute(
            text("DELETE FROM ml.model_registry WHERE ticker_id = :tid"),
            {"tid": ticker_id},
        )
        s.execute(text("DELETE FROM public.tickers WHERE symbol = :s"), {"s": ticker})
    shutil.rmtree(tmp_path, ignore_errors=True)
