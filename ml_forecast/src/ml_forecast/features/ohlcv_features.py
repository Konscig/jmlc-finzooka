"""14 OHLCV-derived features (items 1-14 of research.md R5).

Every function takes the full DataFrame and a positional target index ``t``,
and returns the feature value **computed strictly from rows 0..t** —
i.e. no future leak. Callers that need the sentiment feature (item 15
in R5) use :mod:`ml_forecast.features.sentiment_features` directly.

Raises :class:`InsufficientHistory` when the required look-back window
is not yet available at ``t``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class InsufficientHistory(ValueError):
    """Not enough prior bars to compute the feature at this index."""


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _window(df: pd.DataFrame, t: int, n: int) -> pd.DataFrame:
    if t < n:
        raise InsufficientHistory(
            f"need {n} prior bars at index {t}, have {t + 1}"
        )
    return df.iloc[t - n + 1 : t + 1]


def _closes(df: pd.DataFrame, t: int, n: int) -> np.ndarray:
    return _window(df, t, n)["close"].to_numpy(dtype=float)


# ----------------------------------------------------------------------
# returns (1,5,20)
# ----------------------------------------------------------------------

def return_log_1(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 2)
    return float(np.log(c[-1] / c[0]))


def return_log_5(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 6)
    return float(np.log(c[-1] / c[0]))


def return_log_20(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 21)
    return float(np.log(c[-1] / c[0]))


# ----------------------------------------------------------------------
# momentum: RSI-14, MACD (12/26, signal 9)
# ----------------------------------------------------------------------

def rsi_14(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 15)
    diffs = np.diff(c)
    gains = np.clip(diffs, 0, None)
    losses = -np.clip(diffs, None, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _ema(series: np.ndarray, span: int) -> float:
    """Scalar EMA over the last N values; simple Pandas EWM equivalent."""

    alpha = 2.0 / (span + 1.0)
    ema = series[0]
    for x in series[1:]:
        ema = alpha * x + (1.0 - alpha) * ema
    return float(ema)


def macd_line(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 26)
    return _ema(c[-12:], 12) - _ema(c, 26)


def macd_signal(df: pd.DataFrame, t: int) -> float:
    # 9-period EMA of MACD values for last 9 bars
    if t < 26 + 9 - 1:
        raise InsufficientHistory(
            f"need {26 + 9 - 1} prior bars at index {t} for MACD signal"
        )
    macd_values = np.array(
        [macd_line(df, i) for i in range(t - 8, t + 1)], dtype=float
    )
    return _ema(macd_values, 9)


# ----------------------------------------------------------------------
# volatility: Bollinger bands, ATR-14
# ----------------------------------------------------------------------

def _bollinger(df: pd.DataFrame, t: int) -> tuple[float, float]:
    c = _closes(df, t, 20)
    mean = float(np.mean(c))
    std = float(np.std(c, ddof=0))
    return mean + 2 * std, mean - 2 * std


def bb_upper_20(df: pd.DataFrame, t: int) -> float:
    return _bollinger(df, t)[0]


def bb_lower_20(df: pd.DataFrame, t: int) -> float:
    return _bollinger(df, t)[1]


def atr_14(df: pd.DataFrame, t: int) -> float:
    win = _window(df, t, 15)
    highs = win["high"].to_numpy(dtype=float)
    lows = win["low"].to_numpy(dtype=float)
    closes = win["close"].to_numpy(dtype=float)
    prev_close = closes[:-1]
    high = highs[1:]
    low = lows[1:]
    tr = np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )
    return float(np.mean(tr))


# ----------------------------------------------------------------------
# volume
# ----------------------------------------------------------------------

def volume_zscore_20(df: pd.DataFrame, t: int) -> float:
    win = _window(df, t, 20)
    v = win["volume"].to_numpy(dtype=float)
    mean = float(np.mean(v))
    std = float(np.std(v, ddof=0))
    if std == 0:
        return 0.0
    return (float(v[-1]) - mean) / std


def vwap_20(df: pd.DataFrame, t: int) -> float:
    win = _window(df, t, 20)
    typical = (
        win["high"].to_numpy(dtype=float)
        + win["low"].to_numpy(dtype=float)
        + win["close"].to_numpy(dtype=float)
    ) / 3.0
    vol = win["volume"].to_numpy(dtype=float)
    if vol.sum() == 0:
        return float(np.mean(typical))
    return float(np.sum(typical * vol) / np.sum(vol))


# ----------------------------------------------------------------------
# range
# ----------------------------------------------------------------------

def high_low_range_1(df: pd.DataFrame, t: int) -> float:
    win = _window(df, t, 1)
    return float(win["high"].iloc[0] - win["low"].iloc[0])


# ----------------------------------------------------------------------
# trend
# ----------------------------------------------------------------------

def close_to_ema_50(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 50)
    ema = _ema(c, 50)
    return float(c[-1] / ema - 1.0)


def close_to_ema_200(df: pd.DataFrame, t: int) -> float:
    c = _closes(df, t, 200)
    ema = _ema(c, 200)
    return float(c[-1] / ema - 1.0)


FEATURE_FNS: dict[str, object] = {
    "return_log_1": return_log_1,
    "return_log_5": return_log_5,
    "return_log_20": return_log_20,
    "rsi_14": rsi_14,
    "macd_line": macd_line,
    "macd_signal": macd_signal,
    "bb_upper_20": bb_upper_20,
    "bb_lower_20": bb_lower_20,
    "atr_14": atr_14,
    "volume_zscore_20": volume_zscore_20,
    "vwap_20": vwap_20,
    "high_low_range_1": high_low_range_1,
    "close_to_ema_50": close_to_ema_50,
    "close_to_ema_200": close_to_ema_200,
}

FEATURE_SET_VERSION = "v1"


def extract_at(df: pd.DataFrame, t: int) -> dict[str, float]:
    """Compute all 14 features at a single index, skipping those without
    enough look-back (returns partial dict)."""

    out: dict[str, float] = {}
    for name, fn in FEATURE_FNS.items():
        try:
            out[name] = fn(df, t)  # type: ignore[operator]
        except InsufficientHistory:
            continue
    return out
