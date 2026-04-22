"""Optuna-powered hyperparameter search (research.md R4).

Search budget:

- ``n_trials`` (default from :class:`Settings`) OR ``time_budget_s`` —
  whichever caps first. Optuna's :class:`~optuna.pruners.MedianPruner`
  cuts off trials whose first fold is already worse than the running
  median (so obviously-bad configurations don't burn the remaining
  walk-forward folds).

- Search space per model family is defined inline — callers pick the
  family via :func:`suggest` which dispatches to the correct handler.

Returns the best :class:`optuna.trial.FrozenTrial` and exposes the
fitted forecaster for downstream persistence in
:mod:`ml_forecast.training.pipeline`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

import optuna

from ml_forecast.config import get_settings
from ml_forecast.models.armaexo import ARMAExoForecaster
from ml_forecast.models.base import BaseForecaster, ModelFamily
from ml_forecast.models.lightgbm_model import LightGBMForecaster
from ml_forecast.training.walkforward import WalkForwardValidator

if TYPE_CHECKING:
    import pandas as pd


# Silence Optuna INFO logging by default — too noisy for training logs.
optuna.logging.set_verbosity(optuna.logging.WARNING)


@dataclass(frozen=True, slots=True)
class TuneResult:
    family: ModelFamily
    best_params: dict[str, Any]
    best_metric: float  # aggregate MAPE (lower is better)
    n_trials: int


FactoryBuilder = Callable[[dict[str, Any]], BaseForecaster]


def _factory_for(family: ModelFamily) -> FactoryBuilder:
    if family is ModelFamily.ARMAEXO:

        def build(p: dict[str, Any]) -> BaseForecaster:
            return ARMAExoForecaster(
                ar_order=int(p["ar_order"]), ma_order=int(p["ma_order"])
            )

        return build

    if family is ModelFamily.LIGHTGBM:

        def build(p: dict[str, Any]) -> BaseForecaster:
            return LightGBMForecaster(
                num_leaves=int(p["num_leaves"]),
                learning_rate=float(p["learning_rate"]),
                n_estimators=int(p["n_estimators"]),
            )

        return build

    raise ValueError(f"No hyperparam search defined for family {family}")


def _suggest_space(trial: optuna.Trial, family: ModelFamily) -> dict[str, Any]:
    if family is ModelFamily.ARMAEXO:
        return {
            "ar_order": trial.suggest_int("ar_order", 1, 5),
            "ma_order": trial.suggest_int("ma_order", 0, 2),
        }
    if family is ModelFamily.LIGHTGBM:
        return {
            "num_leaves": trial.suggest_int("num_leaves", 15, 63),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 100, 500, step=50),
        }
    raise ValueError(f"no suggest-space for {family}")


def tune(
    family: ModelFamily,
    df: "pd.DataFrame",
    n_folds: int | None = None,
    n_trials: int | None = None,
    time_budget_s: int | None = None,
) -> TuneResult:
    settings = get_settings()
    folds = n_folds or settings.walkforward_folds
    trials_cap = n_trials or settings.optuna_trials
    budget = time_budget_s or settings.optuna_timeout_seconds
    factory_builder = _factory_for(family)

    def objective(trial: optuna.Trial) -> float:
        params = _suggest_space(trial, family)

        def make_forecaster() -> BaseForecaster:
            return factory_builder(params)

        validator = WalkForwardValidator(make_forecaster, n_folds=folds)
        result = validator.run(df)
        return float(result.aggregate.mape)

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=3, n_warmup_steps=0),
    )
    study.optimize(objective, n_trials=trials_cap, timeout=budget, show_progress_bar=False)
    best = study.best_trial
    return TuneResult(
        family=family,
        best_params=dict(best.params),
        best_metric=float(best.value),
        n_trials=len(study.trials),
    )
