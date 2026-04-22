"""CSV + anomaly validators.

Two entry points:

- :func:`validate_ohlcv_csv` — schema check called by ``TrainPipeline``
  before any feature extraction. Fails fast with a detailed
  :class:`InvalidOhlcvCsv`; guarantees SC-007 coverage.

- :func:`detect_anomalous_bar` — splits / corporate events detector
  fired at inference time. The caller sets
  ``ForecastResponse.anomalous_last_bar`` from its return value
  (spec Edge Case, proto field 14).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED_COLUMNS: tuple[str, ...] = ("datetime", "open", "high", "low", "close", "volume")


class InvalidOhlcvCsv(ValueError):
    """Raised when the OHLCV CSV fails any schema/value invariant."""


def validate_ohlcv_csv(path: Path) -> pd.DataFrame:
    """Load, validate, and return a normalised OHLCV DataFrame."""

    if not path.exists():
        raise InvalidOhlcvCsv(f"file not found: {path}")
    if path.stat().st_size == 0:
        raise InvalidOhlcvCsv(f"empty file: {path}")

    try:
        df = pd.read_csv(path, parse_dates=["datetime"])
    except (ValueError, pd.errors.ParserError) as exc:
        raise InvalidOhlcvCsv(f"{path}: parse error: {exc}") from exc

    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise InvalidOhlcvCsv(
            f"{path}: missing columns {sorted(missing)}; got {list(df.columns)}"
        )

    if df.empty:
        raise InvalidOhlcvCsv(f"{path}: zero rows")

    if df["datetime"].isna().any():
        raise InvalidOhlcvCsv(f"{path}: null datetime values")

    df = df.sort_values("datetime").reset_index(drop=True)
    if not df["datetime"].is_monotonic_increasing:
        raise InvalidOhlcvCsv(f"{path}: non-monotonic datetime after sort")
    if df["datetime"].duplicated().any():
        raise InvalidOhlcvCsv(f"{path}: duplicate datetime values")

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise InvalidOhlcvCsv(f"{path}: column {col} is not numeric")
        if df[col].isna().any():
            raise InvalidOhlcvCsv(f"{path}: null values in column {col}")
        if (df[col] < 0).any():
            raise InvalidOhlcvCsv(f"{path}: negative values in column {col}")

    # OHLC sanity: low <= min(open, close) <= max(open, close) <= high
    if not ((df["low"] <= df[["open", "close"]].min(axis=1)).all()
            and (df["high"] >= df[["open", "close"]].max(axis=1)).all()):
        raise InvalidOhlcvCsv(f"{path}: OHLC ordering violated on at least one row")

    df = df.set_index("datetime")
    return df


# ---------------------------------------------------------------------------
# Anomaly detection — advisory flag only, never mutates predicted_path.
# ---------------------------------------------------------------------------

_ROLLING_WINDOW: int = 20
_PRICE_SIGMA_THRESHOLD: float = 3.0
_VOLUME_RATIO_THRESHOLD: float = 10.0


def detect_anomalous_bar(df: pd.DataFrame, t: int) -> bool:
    """Flag bar at positional index ``t`` as anomalous.

    Heuristic:

    - absolute close-to-close return at ``t`` exceeds 3× rolling std
      over the prior :data:`_ROLLING_WINDOW` bars; OR
    - volume at ``t`` exceeds 10× the rolling mean volume over the
      same prior window.

    Both signals commonly co-occur on splits and corporate events.
    Both are false-positive-prone in isolation, hence OR not AND —
    we prefer to warn rather than miss.
    """

    if t <= _ROLLING_WINDOW:
        # Not enough history to compute a stable baseline; assume normal.
        return False

    closes = df["close"].to_numpy()
    volumes = df["volume"].to_numpy()

    prior_closes = closes[t - _ROLLING_WINDOW : t]
    prior_volumes = volumes[t - _ROLLING_WINDOW : t]

    prior_returns = np.diff(np.log(prior_closes + 1e-12))
    sigma = float(np.std(prior_returns)) if len(prior_returns) > 1 else 0.0
    last_return = abs(float(np.log((closes[t] + 1e-12) / (closes[t - 1] + 1e-12))))

    price_anomaly = sigma > 0 and last_return > _PRICE_SIGMA_THRESHOLD * sigma

    volume_mean = float(np.mean(prior_volumes)) if prior_volumes.size else 0.0
    volume_anomaly = volume_mean > 0 and float(volumes[t]) > _VOLUME_RATIO_THRESHOLD * volume_mean

    return bool(price_anomaly or volume_anomaly)
