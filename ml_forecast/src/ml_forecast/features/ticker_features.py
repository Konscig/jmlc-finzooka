"""Ticker-level aggregate features for the liquidity classifier (US5).

For every CSV under ``archive/D1/``, compute a small, stable set of
aggregate statistics that correlate with the "blue-chip" designation:
average daily volume, volatility of log returns, median daily range,
history length. The classifier then learns which combinations best
separate the seed blue-chip list from the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from ml_forecast.features.validators import InvalidOhlcvCsv, validate_ohlcv_csv

log = logging.getLogger(__name__)


# Seed list fixed at 13 unique tickers per spec clarification (remediation D1).
BLUE_CHIPS_SEED: frozenset[str] = frozenset(
    {
        "SBER", "GAZP", "LKOH", "GMKN", "ROSN",
        "NVTK", "TATN", "MGNT", "YNDX", "MTSS",
        "VTBR", "ALRS", "PLZL",
    }
)


@dataclass(frozen=True, slots=True)
class TickerFeatures:
    ticker: str
    is_blue_chip: int              # 1 / 0 — target label
    avg_volume: float
    median_range_pct: float        # (high - low) / close, median
    volatility_logret: float       # std of daily log returns
    avg_daily_growth_pct: float    # mean of daily (close - prev_close) / prev_close
    history_days: int              # number of bars


def _extract_one(csv_path: Path, ticker: str) -> TickerFeatures | None:
    try:
        df = validate_ohlcv_csv(csv_path)
    except InvalidOhlcvCsv as exc:
        log.warning("skipping %s: %s", ticker, exc)
        return None
    if len(df) < 30:
        return None
    close = df["close"].to_numpy(dtype=float)
    log_ret = np.diff(np.log(close))
    return TickerFeatures(
        ticker=ticker,
        is_blue_chip=int(ticker in BLUE_CHIPS_SEED),
        avg_volume=float(df["volume"].mean()),
        median_range_pct=float(np.median((df["high"] - df["low"]) / df["close"])),
        volatility_logret=float(np.std(log_ret, ddof=0)),
        avg_daily_growth_pct=float(
            np.mean(np.diff(close) / close[:-1])
        ),
        history_days=int(len(df)),
    )


def extract_from_archive(archive_d1_dir: Path) -> pd.DataFrame:
    """Walk ``archive/D1/`` and return a DataFrame with one row per ticker."""

    if not archive_d1_dir.is_dir():
        raise FileNotFoundError(f"missing archive D1 directory: {archive_d1_dir}")

    rows: list[TickerFeatures] = []
    for path in sorted(archive_d1_dir.glob("*_D1.csv")):
        ticker = path.stem.replace("_D1", "")
        feat = _extract_one(path, ticker)
        if feat is not None:
            rows.append(feat)
    if not rows:
        raise FileNotFoundError(
            f"no valid *_D1.csv files under {archive_d1_dir}"
        )
    return pd.DataFrame([f.__dict__ for f in rows])


__all__ = ["BLUE_CHIPS_SEED", "TickerFeatures", "extract_from_archive"]
