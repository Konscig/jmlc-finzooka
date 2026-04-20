"""ARMAExo — port of the ML_ТМБ.ipynb forecaster, cleaned up.

The notebook fit a custom AR(p=300) + MA + exogenous-features model on
raw close prices over a 365-bar window. That implementation had:

1. R² = -1.15 on GAZP_D1 — worse than a mean baseline (overfit).
2. Look-ahead bias — exogenous features computed using the full
   series before the train/test split.
3. Hand-picked p=365/q=300 without any search.

This module replaces it with a cleaner and standards-aligned
equivalent:

- **Endogenous target**: one-step log return  ``r_t = log(c_t / c_{t-1})``.
  Differencing a log series is near-stationary for equity prices;
  SARIMAX handles it directly and we convert back to price levels in
  :meth:`predict`.
- **Exogenous matrix**: the 14 OHLCV-derived features from
  :mod:`ml_forecast.features.ohlcv_features` (sentiment slot is filled
  by the caller when available — see ForecastServicer in T045).
- **Anti-lookahead**: feature values at bar ``t`` are computed from
  rows 0..t only (property-tested in T024); the feature matrix is
  shifted by one bar so row ``t`` of X aligns with target ``r_{t+1}``.
- **Parameters**: ``(ar_order, 0, ma_order)`` with Optuna-tunable
  ``ar_order ∈ [1..5]``, ``ma_order ∈ [0..2]`` — search runs in
  :mod:`ml_forecast.training.hyperparam`.
- **Explainability**: :meth:`factor_contributions` normalises the
  fitted SARIMAX exogenous coefficients against the recent feature
  magnitudes so each row in ``factors[]`` carries both the raw value
  and its relative influence.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

from ml_forecast.domain.factor import FactorContribution, FactorStatus
from ml_forecast.domain.forecast import PricePoint
from ml_forecast.features.ohlcv_features import FEATURE_FNS
from ml_forecast.models.base import BaseForecaster, ModelFamily

if TYPE_CHECKING:
    import pandas as pd


# Minimum history to safely fit ARMAExo: 200 bars to prime close_to_ema_200,
# plus a small buffer for the walk-forward validator (research R3).
_MIN_HISTORY: int = 220


class ARMAExoForecaster(BaseForecaster):
    """ARMA + exogenous features forecaster backed by statsmodels SARIMAX."""

    family = ModelFamily.ARMAEXO

    def __init__(
        self,
        ar_order: int = 3,
        ma_order: int = 1,
        feature_names: list[str] | None = None,
    ) -> None:
        self.ar_order = ar_order
        self.ma_order = ma_order
        self.feature_names: list[str] = list(feature_names or FEATURE_FNS.keys())
        self._result = None
        self._bar_seconds: int = 0
        self._last_ts: object | None = None
        self._last_close: float = 0.0
        # Feature-level stats captured at fit time so :meth:`factor_contributions`
        # can normalise contributions without peeking at runtime data.
        self._feature_std: np.ndarray = np.array([])

    # ------------------------------------------------------------------
    # fit / predict
    # ------------------------------------------------------------------

    def fit(self, df: "pd.DataFrame") -> "ARMAExoForecaster":
        if len(df) < _MIN_HISTORY:
            raise ValueError(
                f"ARMAExo.fit requires ≥{_MIN_HISTORY} bars, got {len(df)}"
            )
        close = df["close"].to_numpy(dtype=float)
        log_returns = np.diff(np.log(close))  # length = N-1

        X = self._build_feature_matrix(df)  # length = N
        # Align X[t] with target r_{t+1}: drop the last row of X, use it only
        # for prediction later.
        X_fit = X[:-1]
        assert len(X_fit) == len(log_returns)

        # Drop initial rows where some features are NaN (insufficient history
        # for EMA-200 etc.). SARIMAX cannot handle NaN in exog.
        valid_mask = ~np.isnan(X_fit).any(axis=1)
        y = log_returns[valid_mask]
        X_fit = X_fit[valid_mask]

        from statsmodels.tsa.statespace.sarimax import SARIMAX  # lazy, heavy import

        self._result = SARIMAX(
            endog=y,
            exog=X_fit,
            order=(self.ar_order, 0, self.ma_order),
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=200)

        # Cache what :meth:`predict` needs.
        delta = df.index[-1] - df.index[-2]
        self._bar_seconds = int(getattr(delta, "total_seconds", lambda: 0)())
        self._last_ts = df.index[-1]
        self._last_close = float(close[-1])
        # Per-feature std on the fit window — used to normalise contributions.
        self._feature_std = np.std(X_fit, axis=0, ddof=0)
        # Avoid division-by-zero when a feature is constant on the window.
        self._feature_std = np.where(self._feature_std == 0, 1.0, self._feature_std)
        return self

    def predict(self, df: "pd.DataFrame", horizon: int) -> list[PricePoint]:
        if self._result is None:
            self.fit(df)
        assert self._result is not None

        X_all = self._build_feature_matrix(df)
        last_row = X_all[-1:]
        if np.isnan(last_row).any():
            raise ValueError(
                "ARMAExo.predict: last-row features contain NaN — "
                "insufficient history for some feature at prediction time"
            )
        exog_future = np.tile(last_row, (horizon, 1))

        fc = self._result.get_forecast(steps=horizon, exog=exog_future)
        mean_ret = np.asarray(fc.predicted_mean, dtype=float)
        se = np.asarray(fc.se_mean, dtype=float)

        # Cumulative log-returns into predicted close prices.
        cum = np.cumsum(mean_ret)
        means = self._last_close * np.exp(cum)
        # 68% confidence interval (±1σ) on the log-return path.
        ci_lo = self._last_close * np.exp(cum - np.cumsum(se))
        ci_hi = self._last_close * np.exp(cum + np.cumsum(se))

        step = timedelta(seconds=self._bar_seconds)
        out: list[PricePoint] = []
        for i in range(horizon):
            out.append(
                PricePoint(
                    t=self._last_ts + step * (i + 1),  # type: ignore[operator]
                    mean=Decimal(str(float(means[i]))),
                    lo=Decimal(str(float(ci_lo[i]))),
                    hi=Decimal(str(float(ci_hi[i]))),
                )
            )
        return out

    # ------------------------------------------------------------------
    # explainability
    # ------------------------------------------------------------------

    def factor_contributions(self, df: "pd.DataFrame") -> list[FactorContribution]:
        if self._result is None:
            raise RuntimeError("ARMAExo.factor_contributions called before fit")

        X_all = self._build_feature_matrix(df)
        last_row = X_all[-1]
        params = np.asarray(self._result.params, dtype=float)
        # SARIMAX layout: AR_i..., exog_i..., MA_i..., sigma2. The exog block
        # starts after the AR coefficients.
        exog_start = self.ar_order
        exog_end = exog_start + len(self.feature_names)
        exog_coefs = params[exog_start:exog_end]

        # Standardised contribution = coef * (value - 0) / std_on_fit_window.
        # Replace NaNs in last_row with 0 so stale features contribute 0.
        safe_row = np.nan_to_num(last_row, nan=0.0)
        raw_contrib = exog_coefs * safe_row / self._feature_std
        # Normalise so sum(|c|) == 1 (or stays 0 when everything is zero).
        abs_sum = float(np.sum(np.abs(raw_contrib)))
        if abs_sum == 0.0:
            norm = raw_contrib
        else:
            norm = raw_contrib / abs_sum

        contributions: list[FactorContribution] = []
        for name, value, contrib in zip(self.feature_names, last_row, norm, strict=True):
            if np.isnan(value):
                contributions.append(
                    FactorContribution(
                        name=name,
                        value=None,
                        contribution=Decimal(str(float(contrib))),
                        source="ohlcv",
                        status=FactorStatus.UNAVAILABLE,
                    )
                )
                continue
            contributions.append(
                FactorContribution(
                    name=name,
                    value=Decimal(str(float(value))),
                    contribution=Decimal(str(float(contrib))),
                    source="ohlcv",
                    status=FactorStatus.OK,
                )
            )
        return contributions

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_feature_matrix(self, df: "pd.DataFrame") -> np.ndarray:
        """Build the (N × F) feature matrix. Rows with insufficient history
        contain NaN — the caller is responsible for masking/dropping them."""

        n = len(df)
        f = len(self.feature_names)
        matrix = np.full((n, f), np.nan, dtype=float)
        for j, name in enumerate(self.feature_names):
            fn = FEATURE_FNS[name]
            for i in range(n):
                try:
                    matrix[i, j] = float(fn(df, i))  # type: ignore[operator]
                except Exception:
                    # InsufficientHistory → leave NaN; anything else we treat
                    # defensively as NaN to avoid corrupting the fit.
                    pass
        return matrix
