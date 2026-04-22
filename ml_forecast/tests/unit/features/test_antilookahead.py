"""T024 — anti look-ahead property test (FR-021, SC-007).

Invariant: the feature value computed at bar ``t`` MUST NOT change if we
mutate any bar at index > t. If this property fails for any feature,
the training pipeline would leak future information and any MAPE we
report is a mirage (exactly the notebook's R² = -1.15 pathology).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ml_forecast.features.ohlcv_features import FEATURE_FNS, InsufficientHistory


def _make_df(length: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Random-walk close prices, strictly positive.
    close = 100.0 + np.cumsum(rng.normal(0, 1.0, size=length))
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, size=length)
    low = np.maximum(close - rng.uniform(0.1, 1.0, size=length), 0.5)
    open_ = close + rng.uniform(-0.5, 0.5, size=length)
    # enforce ordering low <= min(o,c) and high >= max(o,c)
    low = np.minimum(low, np.minimum(open_, close) - 0.01)
    high = np.maximum(high, np.maximum(open_, close) + 0.01)
    low = np.maximum(low, 0.1)
    volume = rng.integers(100, 10_000, size=length).astype(float)
    idx = pd.date_range("2024-01-01", periods=length, freq="D")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@settings(max_examples=20, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    length=st.integers(min_value=220, max_value=260),
    t_frac=st.floats(min_value=0.6, max_value=0.9),
)
@pytest.mark.parametrize("feature_name", list(FEATURE_FNS.keys()))
def test_feature_ignores_future(
    feature_name: str, seed: int, length: int, t_frac: float
) -> None:
    df = _make_df(length, seed)
    t = int(length * t_frac)
    fn = FEATURE_FNS[feature_name]
    try:
        baseline = fn(df, t)  # type: ignore[operator]
    except InsufficientHistory:
        return

    # Mutate every bar after t with a large, deterministic perturbation.
    tampered = df.copy()
    future_slice = tampered.iloc[t + 1 :]
    tampered.iloc[t + 1 :, tampered.columns.get_loc("close")] = (
        future_slice["close"].to_numpy() * 1000.0
    )
    tampered.iloc[t + 1 :, tampered.columns.get_loc("volume")] = (
        future_slice["volume"].to_numpy() * 1000.0
    )

    again = fn(tampered, t)  # type: ignore[operator]
    assert baseline == pytest.approx(again, rel=1e-12, abs=1e-12), (
        f"{feature_name} at t={t} leaked future information: "
        f"{baseline} -> {again}"
    )
