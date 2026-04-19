"""T021 — corrupted-CSV matrix (SC-007)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ml_forecast.features.validators import InvalidOhlcvCsv, validate_ohlcv_csv


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def test_empty_file(tmp_path: Path) -> None:
    p = _write(tmp_path, "empty.csv", "")
    with pytest.raises(InvalidOhlcvCsv, match="empty file"):
        validate_ohlcv_csv(p)


def test_missing_column(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "no_close.csv",
        "datetime,open,high,low,volume\n2024-01-01,1,2,0.5,100\n",
    )
    with pytest.raises(InvalidOhlcvCsv, match="missing columns"):
        validate_ohlcv_csv(p)


def test_non_monotonic_datetime(tmp_path: Path) -> None:
    # Duplicates trip a dedicated check even before monotonic.
    p = _write(
        tmp_path,
        "dup.csv",
        "datetime,open,high,low,close,volume\n"
        "2024-01-02,1,2,0.5,1.5,100\n"
        "2024-01-02,1,2,0.5,1.5,100\n",
    )
    with pytest.raises(InvalidOhlcvCsv, match="duplicate datetime"):
        validate_ohlcv_csv(p)


def test_negative_close(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "neg.csv",
        "datetime,open,high,low,close,volume\n"
        "2024-01-01,1,2,0.5,1.5,100\n"
        "2024-01-02,1,2,0.5,-0.1,100\n",
    )
    with pytest.raises(InvalidOhlcvCsv, match="negative values in column close"):
        validate_ohlcv_csv(p)


def test_ohlc_ordering_violated(tmp_path: Path) -> None:
    # close > high
    p = _write(
        tmp_path,
        "ohlc.csv",
        "datetime,open,high,low,close,volume\n"
        "2024-01-01,1,2,0.5,3.0,100\n",
    )
    with pytest.raises(InvalidOhlcvCsv, match="OHLC ordering"):
        validate_ohlcv_csv(p)


def test_valid_csv(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "ok.csv",
        "datetime,open,high,low,close,volume\n"
        "2024-01-01,1.0,1.5,0.9,1.4,100\n"
        "2024-01-02,1.4,1.6,1.3,1.5,120\n",
    )
    df = validate_ohlcv_csv(p)
    assert df.shape == (2, 5)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
