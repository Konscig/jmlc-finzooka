"""Expanding-window walk-forward validator (research.md R3).

Split the history into ``n_folds`` consecutive test windows. For each
fold:

1. Train on everything before the test window.
2. For every position ``t`` inside the test window, ask the forecaster
   to predict one bar ahead (horizon=1). Anti look-ahead is enforced
   by our feature layer, so no manual masking is required here.
3. Collect predictions vs actuals per-fold → per-fold metrics
   (MAPE / RMSE / MAE / R² / directional accuracy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

    from ml_forecast.models.base import BaseForecaster


ForecasterFactory = Callable[[], "BaseForecaster"]


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    fold: int
    n_test: int
    mape: float
    rmse: float
    mae: float
    r2: float
    directional_accuracy: float


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    per_fold: list[FoldMetrics]
    aggregate: FoldMetrics  # fold=-1 in the aggregate record

    def as_dict(self) -> dict[str, object]:
        return {
            "per_fold": [f.__dict__ for f in self.per_fold],
            "aggregate": self.aggregate.__dict__,
        }


def _metrics(
    fold: int, y_true: np.ndarray, y_pred: np.ndarray, prev_close: np.ndarray
) -> FoldMetrics:
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err**2)))
    # Guard against zero actuals when computing MAPE.
    nonzero = y_true != 0
    mape = float(np.mean(np.abs(err[nonzero] / y_true[nonzero]))) if nonzero.any() else float("nan")
    ss_res = float(np.sum(err**2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float("nan") if ss_tot == 0 else 1.0 - ss_res / ss_tot
    # Directional accuracy: sign of (prediction - prev_close) vs (actual - prev_close).
    actual_dir = np.sign(y_true - prev_close)
    pred_dir = np.sign(y_pred - prev_close)
    # Treat zero-movement bars as "correct" only when both sides agree.
    correct = (actual_dir == pred_dir).sum()
    dir_acc = float(correct) / len(y_true) if len(y_true) else float("nan")
    return FoldMetrics(
        fold=fold,
        n_test=len(y_true),
        mape=mape,
        rmse=rmse,
        mae=mae,
        r2=r2,
        directional_accuracy=dir_acc,
    )


class WalkForwardValidator:
    def __init__(
        self,
        factory: ForecasterFactory,
        n_folds: int = 5,
        test_size: int | None = None,
        min_train: int = 220,
    ) -> None:
        self.factory = factory
        self.n_folds = n_folds
        self.test_size = test_size
        self.min_train = min_train

    def run(self, df: "pd.DataFrame") -> WalkForwardResult:
        n = len(df)
        if n < self.min_train + self.n_folds * 2:
            raise ValueError(
                f"walk-forward needs ≥{self.min_train + self.n_folds * 2} bars, got {n}"
            )
        test_size = self.test_size or max(1, (n - self.min_train) // self.n_folds)

        per_fold: list[FoldMetrics] = []
        all_true: list[float] = []
        all_pred: list[float] = []
        all_prev: list[float] = []
        start = self.min_train
        for k in range(self.n_folds):
            test_end = min(start + test_size, n - 1)  # leave room for target
            if start >= test_end:
                break
            train = df.iloc[:start]
            fold_true: list[float] = []
            fold_pred: list[float] = []
            fold_prev: list[float] = []
            for t in range(start, test_end):
                forecaster = self.factory().fit(df.iloc[:t])
                prediction = forecaster.predict(df.iloc[:t], horizon=1)[0]
                fold_prev.append(float(df["close"].iloc[t - 1]))
                fold_true.append(float(df["close"].iloc[t]))
                fold_pred.append(float(prediction.mean))
            if fold_true:
                arr_true = np.array(fold_true)
                arr_pred = np.array(fold_pred)
                arr_prev = np.array(fold_prev)
                per_fold.append(_metrics(k, arr_true, arr_pred, arr_prev))
                all_true.extend(fold_true)
                all_pred.extend(fold_pred)
                all_prev.extend(fold_prev)
            start = test_end

        if not per_fold:
            raise RuntimeError("walk-forward produced zero test points")

        aggregate = _metrics(-1, np.array(all_true), np.array(all_pred), np.array(all_prev))
        return WalkForwardResult(per_fold=per_fold, aggregate=aggregate)
