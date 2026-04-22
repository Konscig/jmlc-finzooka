"""LightGBM fallback forecaster (invoked by :mod:`training.pipeline` when
ARMAExo fails SC-001 — see FR-025).

Design mirrors :class:`ARMAExoForecaster`: same feature matrix, same
``horizon``-step roll-out at predict time. Explainability uses SHAP
``TreeExplainer`` to produce per-feature contributions (Principle I).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np

from ml_forecast.domain.factor import FactorContribution, FactorStatus
from ml_forecast.domain.forecast import PricePoint
from ml_forecast.features.ohlcv_features import FEATURE_FNS
from ml_forecast.models.base import BaseForecaster, ModelFamily

if TYPE_CHECKING:
    import pandas as pd


_MIN_HISTORY: int = 220


class LightGBMForecaster(BaseForecaster):
    """One-step-ahead log-return regressor; recursive roll-out for horizon > 1."""

    family = ModelFamily.LIGHTGBM

    def __init__(
        self,
        num_leaves: int = 31,
        learning_rate: float = 0.05,
        n_estimators: int = 300,
        feature_names: list[str] | None = None,
    ) -> None:
        self.num_leaves = num_leaves
        self.learning_rate = learning_rate
        self.n_estimators = n_estimators
        self.feature_names: list[str] = list(feature_names or FEATURE_FNS.keys())
        self._model: lgb.LGBMRegressor | None = None
        self._explainer: object | None = None  # shap.TreeExplainer, typed via Any
        self._residual_std: float = 0.0
        self._bar_seconds: int = 0
        self._last_ts: object | None = None
        self._last_close: float = 0.0

    def fit(self, df: "pd.DataFrame") -> "LightGBMForecaster":
        if len(df) < _MIN_HISTORY:
            raise ValueError(
                f"LightGBMForecaster.fit requires ≥{_MIN_HISTORY} bars, got {len(df)}"
            )
        close = df["close"].to_numpy(dtype=float)
        log_returns = np.diff(np.log(close))

        X = self._build_feature_matrix(df)[:-1]  # align rows with y
        valid_mask = ~np.isnan(X).any(axis=1)
        y = log_returns[valid_mask]
        X = X[valid_mask]

        self._model = lgb.LGBMRegressor(
            num_leaves=self.num_leaves,
            learning_rate=self.learning_rate,
            n_estimators=self.n_estimators,
            random_state=42,
            verbose=-1,
        )
        self._model.fit(X, y)

        residuals = y - self._model.predict(X)
        self._residual_std = float(np.std(residuals, ddof=0))

        import shap  # lazy import — shap pulls in a lot

        self._explainer = shap.TreeExplainer(self._model)

        delta = df.index[-1] - df.index[-2]
        self._bar_seconds = int(getattr(delta, "total_seconds", lambda: 0)())
        self._last_ts = df.index[-1]
        self._last_close = float(close[-1])
        return self

    def predict(self, df: "pd.DataFrame", horizon: int) -> list[PricePoint]:
        if self._model is None:
            self.fit(df)
        assert self._model is not None

        last_row = self._build_feature_matrix(df)[-1:]
        if np.isnan(last_row).any():
            raise ValueError(
                "LightGBMForecaster.predict: last-row features contain NaN"
            )

        # Recursive single-step roll-out: we do not update features between
        # steps (they would need future bars we do not have). This is a known
        # simplification; horizon > 1 carries compounding uncertainty that we
        # reflect in the ±σ bands below.
        per_step_ret = float(self._model.predict(last_row)[0])
        cum_ret = 0.0
        cum_var = 0.0
        step = timedelta(seconds=self._bar_seconds)
        out: list[PricePoint] = []
        for i in range(horizon):
            cum_ret += per_step_ret
            cum_var += self._residual_std ** 2
            sigma = float(np.sqrt(cum_var))
            mean_price = self._last_close * float(np.exp(cum_ret))
            lo_price = self._last_close * float(np.exp(cum_ret - sigma))
            hi_price = self._last_close * float(np.exp(cum_ret + sigma))
            out.append(
                PricePoint(
                    t=self._last_ts + step * (i + 1),  # type: ignore[operator]
                    mean=Decimal(str(mean_price)),
                    lo=Decimal(str(lo_price)),
                    hi=Decimal(str(hi_price)),
                )
            )
        return out

    def factor_contributions(self, df: "pd.DataFrame") -> list[FactorContribution]:
        if self._model is None or self._explainer is None:
            raise RuntimeError(
                "LightGBMForecaster.factor_contributions called before fit"
            )
        last_row = self._build_feature_matrix(df)[-1:]

        # SHAP returns (1, F) for a single sample; we use absolute SHAP values
        # normalised to sum(|c|) == 1.
        shap_values = np.asarray(
            self._explainer.shap_values(np.nan_to_num(last_row, nan=0.0))  # type: ignore[attr-defined]
        ).reshape(-1)
        abs_sum = float(np.sum(np.abs(shap_values)))
        norm = shap_values / abs_sum if abs_sum > 0 else shap_values

        contributions: list[FactorContribution] = []
        for name, value, contrib in zip(
            self.feature_names, last_row[0], norm, strict=True
        ):
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

    def _build_feature_matrix(self, df: "pd.DataFrame") -> np.ndarray:
        n = len(df)
        f = len(self.feature_names)
        matrix = np.full((n, f), np.nan, dtype=float)
        for j, name in enumerate(self.feature_names):
            fn = FEATURE_FNS[name]
            for i in range(n):
                try:
                    matrix[i, j] = float(fn(df, i))  # type: ignore[operator]
                except Exception:
                    pass
        return matrix
