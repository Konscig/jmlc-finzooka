"""T081 — LiquidityClassifier meets SC-010 recall targets.

Generated on synthetic data with a clear feature gap between classes
so the balanced-RF reliably clears the threshold. Real-data assertion
over the 249-ticker archive lives in T096.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ml_forecast.models.classifier import (
    ClassificationReport,
    FEATURE_COLS,
    LiquidityClassifier,
)


def _synth_dataset(n_blue: int = 13, n_other: int = 236, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    blue = {
        "ticker": [f"BC{i:02d}" for i in range(n_blue)],
        "is_blue_chip": [1] * n_blue,
        "avg_volume": rng.uniform(5e6, 5e7, n_blue),
        "median_range_pct": rng.uniform(0.005, 0.02, n_blue),
        "volatility_logret": rng.uniform(0.01, 0.025, n_blue),
        "avg_daily_growth_pct": rng.uniform(-0.001, 0.002, n_blue),
        "history_days": rng.integers(3000, 6000, n_blue),
    }
    other = {
        "ticker": [f"OT{i:03d}" for i in range(n_other)],
        "is_blue_chip": [0] * n_other,
        "avg_volume": rng.uniform(5e3, 5e5, n_other),
        "median_range_pct": rng.uniform(0.02, 0.08, n_other),
        "volatility_logret": rng.uniform(0.03, 0.08, n_other),
        "avg_daily_growth_pct": rng.uniform(-0.005, 0.005, n_other),
        "history_days": rng.integers(200, 3000, n_other),
    }
    return pd.concat([pd.DataFrame(blue), pd.DataFrame(other)], ignore_index=True)


def test_classifier_meets_recall_targets() -> None:
    df = _synth_dataset()
    clf = LiquidityClassifier(random_state=11)
    report: ClassificationReport = clf.fit_and_evaluate(df, target_recall=0.80)

    assert report.total_tickers == len(df)
    assert report.blue_chip_recall >= 0.80, (
        f"SC-010 fragile: blue-chip recall {report.blue_chip_recall:.3f} < 0.80"
    )
    assert report.other_recall >= 0.90
    # Confusion matrix shape [[TN, FP], [FN, TP]].
    assert len(report.confusion_matrix) == 2
    # Feature columns used by the fitter align with the documented contract.
    assert set(FEATURE_COLS).issubset(df.columns)
