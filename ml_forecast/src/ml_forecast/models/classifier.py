"""LiquidityClassifier — US5 blue-chip classifier.

Closes the 0%-recall problem in the notebook (remediation C3 for US5)
by:

1. Stratified train/test split — keeps the minority class in both
   folds.
2. ``class_weight='balanced'`` on the RandomForest — up-weights the
   blue-chip class in loss without touching the data.
3. Optional SMOTE oversampling on the training fold if
   class_weight alone doesn't clear the recall target (research R11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix, recall_score
from sklearn.model_selection import StratifiedShuffleSplit

log = logging.getLogger(__name__)


FEATURE_COLS: tuple[str, ...] = (
    "avg_volume",
    "median_range_pct",
    "volatility_logret",
    "avg_daily_growth_pct",
    "history_days",
)


@dataclass(frozen=True, slots=True)
class ClassificationReport:
    total_tickers: int
    blue_chip_recall: float
    other_recall: float
    confusion_matrix: list[list[int]]
    label_predictions: list[dict[str, Any]]
    used_smote: bool


class LiquidityClassifier:
    def __init__(self, n_estimators: int = 300, random_state: int = 42) -> None:
        self.n_estimators = n_estimators
        self.random_state = random_state

    def fit_and_evaluate(
        self,
        df: pd.DataFrame,
        target_recall: float = 0.80,
    ) -> ClassificationReport:
        if "is_blue_chip" not in df.columns:
            raise ValueError("df must carry an 'is_blue_chip' target column")

        X = df[list(FEATURE_COLS)].to_numpy(dtype=float)
        y = df["is_blue_chip"].to_numpy(dtype=int)

        splitter = StratifiedShuffleSplit(
            n_splits=1, test_size=0.25, random_state=self.random_state
        )
        train_idx, test_idx = next(splitter.split(X, y))
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        used_smote = False
        report = self._train_once(X_train, y_train, X_test, y_test, df, test_idx, used_smote)

        # Promote to SMOTE only if balanced RF alone misses the recall target.
        if report.blue_chip_recall < target_recall:
            try:
                from imblearn.over_sampling import SMOTE

                # SMOTE needs minority class to have at least 2 samples after split;
                # our blue-chip seed of 13 × 0.75 train ≈ 9 — plenty for SMOTE(k=3).
                smote = SMOTE(
                    random_state=self.random_state,
                    k_neighbors=min(3, int(y_train.sum()) - 1),
                )
                X_train_os, y_train_os = smote.fit_resample(X_train, y_train)
                used_smote = True
                log.info(
                    "promoting to SMOTE: balanced-RF recall %.3f < target %.3f",
                    report.blue_chip_recall, target_recall,
                )
                report = self._train_once(
                    X_train_os, y_train_os, X_test, y_test, df, test_idx, used_smote
                )
            except ImportError:
                log.warning("imblearn not installed; keeping balanced-RF result")

        return report

    def _train_once(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test: np.ndarray,
        y_test: np.ndarray,
        df: pd.DataFrame,
        test_idx: np.ndarray,
        used_smote: bool,
    ) -> ClassificationReport:
        clf = RandomForestClassifier(
            n_estimators=self.n_estimators,
            class_weight="balanced",
            random_state=self.random_state,
            n_jobs=1,
        )
        clf.fit(X_train, y_train)
        y_pred = clf.predict(X_test)
        probs = clf.predict_proba(X_test)

        bc_recall = float(
            recall_score(y_test, y_pred, pos_label=1, zero_division=0.0)
        )
        other_recall = float(
            recall_score(y_test, y_pred, pos_label=0, zero_division=0.0)
        )
        cm = confusion_matrix(y_test, y_pred).tolist()

        label_predictions: list[dict[str, Any]] = []
        test_df = df.iloc[test_idx].reset_index(drop=True)
        for i in range(len(test_df)):
            label_predictions.append(
                {
                    "ticker": str(test_df.loc[i, "ticker"]),
                    "actual": int(y_test[i]),
                    "predicted": int(y_pred[i]),
                    "prob_blue_chip": float(probs[i][1]),
                }
            )
        return ClassificationReport(
            total_tickers=len(df),
            blue_chip_recall=bc_recall,
            other_recall=other_recall,
            confusion_matrix=cm,
            label_predictions=label_predictions,
            used_smote=used_smote,
        )
