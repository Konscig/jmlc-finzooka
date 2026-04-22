"""T039 — end-to-end smoke: train a real forecaster, save via registry,
fetch via get_production, predict, assert schema.

Uses :class:`NaiveBaseline` as the stand-in forecaster — the goal here
is to verify that the persistence layer + registry + forecast pipeline
wire together on a real Postgres schema, not to validate any specific
model's accuracy. SC-001/002/003 regression-gate testing lives in
T096 (tests/integration/test_blue_chips_sc_targets.py).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.models.baselines import NaiveBaseline
from ml_forecast.models.base import ModelFamily


# Integration tests require a live Postgres + Redis; pytest will skip them
# cleanly when the backing services are unreachable (e.g. running outside
# docker-compose).
pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration tests require ML_DATABASE_URL set (run inside docker-compose)",
)


def _synthetic_df(n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(7)
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
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_train_register_fetch_predict(tmp_path: Path) -> None:
    from sqlalchemy import text

    from ml_forecast.inference import registry
    from ml_forecast.storage.postgres import session_scope

    # Point artifact_dir at a scratch directory to keep the workspace clean.
    get_settings.cache_clear()
    os.environ["ML_ARTIFACT_DIR"] = str(tmp_path)
    get_settings.cache_clear()

    # Seed a synthetic ticker into the public.tickers table that registry
    # _resolve_ticker looks up. The table is owned by the backend feature,
    # so we create it here if missing (safe — public.tickers already exists
    # in backend deployments).
    with session_scope() as s:
        s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.tickers "
                "(id BIGSERIAL PRIMARY KEY, symbol VARCHAR(20) UNIQUE NOT NULL, "
                "is_blue_chip BOOLEAN DEFAULT true, is_active BOOLEAN DEFAULT true)"
            )
        )
        s.execute(
            text("INSERT INTO public.tickers (symbol) VALUES ('TEST') "
                 "ON CONFLICT (symbol) DO NOTHING")
        )

    df = _synthetic_df()

    # Train the smoke-test forecaster.
    forecaster = NaiveBaseline().fit(df)

    # Register as shadow.
    with session_scope() as s:
        ticker_id = int(
            s.execute(
                text("SELECT id FROM public.tickers WHERE symbol = 'TEST'")
            ).scalar_one()
        )

    handle = registry.register_shadow(
        registry.RegistrationInput(
            ticker_id=ticker_id,
            ticker="TEST",
            timeframe=Timeframe.D1,
            model_family=ModelFamily.BASELINE_NAIVE,
            model=forecaster,
            dataset_sha256="a" * 64,
            feature_set_version="v1",
            metrics_aggregate={"mape": 0.02, "rmse": 0.05, "directional_accuracy": 0.55},
        )
    )

    # Promote to production.
    new_handle, previous = registry.promote("TEST", Timeframe.D1, handle.model_version)
    assert new_handle.state.value == "production"
    assert previous is None  # no incumbent

    # Fetch back from registry; load artifact; predict.
    loaded_handle = registry.get_production("TEST", Timeframe.D1)
    assert loaded_handle.model_version == handle.model_version

    loaded_forecaster = registry.load_forecaster(loaded_handle)
    predictions = loaded_forecaster.predict(df, horizon=4)
    assert len(predictions) == 4
    for p in predictions:
        assert p.mean > 0
        assert p.lo <= p.mean <= p.hi

    # Archive + verify state transition.
    archived = registry.archive("TEST", Timeframe.D1, handle.model_version)
    assert archived.state.value == "archived"

    # Cleanup (so repeated runs work).
    with session_scope() as s:
        s.execute(text("DELETE FROM ml.model_registry WHERE ticker_id = :tid"),
                  {"tid": ticker_id})
        s.execute(text("DELETE FROM public.tickers WHERE symbol = 'TEST'"))
