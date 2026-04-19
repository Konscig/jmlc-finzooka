"""Reference baselines used for promotion gating (FR-022, R3) and backtest (FR-011).

Baselines are intentionally simple, deterministic, and cheap. They are:

- :class:`NaiveBaseline` — last close repeated for the entire horizon.
  Acts as the promotion floor: any production candidate whose R² falls
  below this baseline fails FR-022 and is marked ``do_not_promote``.

- :class:`OhlcvOnlyBaseline` — linear fit of log returns on 20 recent bars.
  Represents "what OHLCV alone buys us"; the sentiment-augmented model
  must beat it by ≥10% relative MAPE (SC-009).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

from ml_forecast.domain.factor import FactorContribution, FactorStatus
from ml_forecast.domain.forecast import PricePoint
from ml_forecast.models.base import BaseForecaster, ModelFamily

if TYPE_CHECKING:
    import pandas as pd


class NaiveBaseline(BaseForecaster):
    """Repeat the last observed close for every future bar."""

    family = ModelFamily.BASELINE_NAIVE

    def __init__(self) -> None:
        self._last_close: float | None = None
        self._last_ts: object | None = None
        self._bar_seconds: int | None = None

    def fit(self, df: pd.DataFrame) -> NaiveBaseline:
        if df.empty:
            raise ValueError("NaiveBaseline.fit: empty DataFrame")
        self._last_close = float(df["close"].iloc[-1])
        self._last_ts = df.index[-1]
        if len(df.index) >= 2:
            delta = df.index[-1] - df.index[-2]
            self._bar_seconds = int(getattr(delta, "total_seconds", lambda: 0)())
        return self

    def predict(self, df: pd.DataFrame, horizon: int) -> list[PricePoint]:
        if self._last_close is None or self._last_ts is None or self._bar_seconds is None:
            self.fit(df)
        assert self._last_close is not None
        assert self._last_ts is not None
        assert self._bar_seconds is not None
        out: list[PricePoint] = []
        step = timedelta(seconds=self._bar_seconds)
        for i in range(1, horizon + 1):
            mean_price = Decimal(str(self._last_close))
            band = mean_price * Decimal("0.01")  # ± 1% symmetric band
            out.append(
                PricePoint(
                    t=self._last_ts + step * i,  # type: ignore[operator]
                    mean=mean_price,
                    lo=mean_price - band,
                    hi=mean_price + band,
                )
            )
        return out

    def factor_contributions(self, df: pd.DataFrame) -> list[FactorContribution]:
        return [
            FactorContribution(
                name="naive_last_close",
                value=Decimal(str(df["close"].iloc[-1])) if not df.empty else None,
                contribution=Decimal("1.0"),
                source="ohlcv",
                status=FactorStatus.OK,
            )
        ]


class OhlcvOnlyBaseline(BaseForecaster):
    """Linear fit of log returns on a 20-bar trailing window (OHLCV only)."""

    family = ModelFamily.BASELINE_OHLCV_ONLY
    _WINDOW: int = 20

    def __init__(self) -> None:
        self._slope: float = 0.0
        self._intercept: float = 0.0
        self._last_close: float = 0.0
        self._last_ts: object | None = None
        self._bar_seconds: int = 0

    def fit(self, df: pd.DataFrame) -> OhlcvOnlyBaseline:
        if len(df) < self._WINDOW + 1:
            raise ValueError(
                f"OhlcvOnlyBaseline.fit requires at least {self._WINDOW + 1} bars,"
                f" got {len(df)}"
            )
        window = df["close"].iloc[-(self._WINDOW + 1):].astype(float).to_numpy()
        log_returns = np.diff(np.log(window))
        x = np.arange(len(log_returns))
        # simple least-squares (polyfit deg=1)
        slope, intercept = np.polyfit(x, log_returns, deg=1)
        self._slope = float(slope)
        self._intercept = float(intercept)
        self._last_close = float(df["close"].iloc[-1])
        self._last_ts = df.index[-1]
        delta = df.index[-1] - df.index[-2]
        self._bar_seconds = int(getattr(delta, "total_seconds", lambda: 0)())
        return self

    def predict(self, df: pd.DataFrame, horizon: int) -> list[PricePoint]:
        assert self._last_ts is not None
        out: list[PricePoint] = []
        step = timedelta(seconds=self._bar_seconds)
        cumulative_log = 0.0
        for i in range(1, horizon + 1):
            # Extrapolate log-return trend one step at a time.
            r = self._slope * (self._WINDOW - 1 + i) + self._intercept
            cumulative_log += r
            mean = self._last_close * np.exp(cumulative_log)
            band = mean * 0.015  # ± 1.5% — roughly one sigma for blue-chip intraday
            out.append(
                PricePoint(
                    t=self._last_ts + step * i,  # type: ignore[operator]
                    mean=Decimal(str(mean)),
                    lo=Decimal(str(mean - band)),
                    hi=Decimal(str(mean + band)),
                )
            )
        return out

    def factor_contributions(self, df: pd.DataFrame) -> list[FactorContribution]:
        return [
            FactorContribution(
                name="ohlcv_trend_slope",
                value=Decimal(str(self._slope)),
                contribution=Decimal("0.7"),
                source="ohlcv",
                status=FactorStatus.OK,
            ),
            FactorContribution(
                name="ohlcv_last_close",
                value=Decimal(str(self._last_close)),
                contribution=Decimal("0.3"),
                source="ohlcv",
                status=FactorStatus.OK,
            ),
        ]
