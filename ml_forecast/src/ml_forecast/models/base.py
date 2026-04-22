"""Abstract forecaster interface.

Every concrete model (ARMAExo, LightGBM, baselines) conforms to this
contract so :class:`ml_forecast.training.walkforward.WalkForwardValidator`
and :class:`ml_forecast.backtest.runner.BacktestRunner` can treat them
polymorphically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from ml_forecast.domain.factor import FactorContribution
    from ml_forecast.domain.forecast import PricePoint


class ModelFamily(str, Enum):
    ARMAEXO = "armaexo"
    LIGHTGBM = "lightgbm"
    BASELINE_NAIVE = "baseline_naive"
    BASELINE_OHLCV_ONLY = "baseline_ohlcv_only"


class BaseForecaster(ABC):
    """Polymorphic base for all forecasting models.

    Contract:

    - :meth:`fit` consumes a DataFrame indexed by timestamp with
      OHLCV columns plus any exogenous columns the model expects.
    - :meth:`predict` returns exactly ``horizon`` :class:`PricePoint`
      values; callers must not see fewer or more.
    - :meth:`factor_contributions` returns normalised contributions
      whose absolute values sum to approximately 1. Contributions
      power both `Forecast.factors` (explainability, Principle I)
      and the SHAP/feature-importance view in the admin panel.
    """

    family: ModelFamily

    @abstractmethod
    def fit(self, df: pd.DataFrame) -> BaseForecaster: ...

    @abstractmethod
    def predict(self, df: pd.DataFrame, horizon: int) -> list[PricePoint]: ...

    @abstractmethod
    def factor_contributions(self, df: pd.DataFrame) -> list[FactorContribution]: ...
