"""T066 — Backtest RPC contract test.

Patches the runner so the test doesn't need the 3 GB archive/ dataset.
The integration test (T067) exercises the real pipeline on synthetic
data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import patch

import grpc
import pytest

from ml_forecast.backtest.runner import BacktestMetrics, BacktestResult


@dataclass
class FakeContext:
    code: grpc.StatusCode | None = None
    details: str | None = None
    metadata: list[tuple[str, str]] = field(default_factory=list)

    def set_code(self, code: grpc.StatusCode) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self.metadata


@pytest.fixture
def servicer():
    from ml_forecast.api.backtest_service import BacktestServicer

    return BacktestServicer()


@pytest.fixture
def make_request():
    from google.protobuf import timestamp_pb2

    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    def _make(
        ticker: str = "SBER",
        tf: int = pb2.TIMEFRAME_D1,
        start: date = date(2024, 1, 1),
        end: date = date(2024, 6, 1),
    ) -> Any:
        req = pb2.BacktestRequest(ticker=ticker, timeframe=tf)
        ts_s = timestamp_pb2.Timestamp()
        ts_s.FromDatetime(__import__("datetime").datetime.combine(start, __import__("datetime").time()))
        ts_e = timestamp_pb2.Timestamp()
        ts_e.FromDatetime(__import__("datetime").datetime.combine(end, __import__("datetime").time()))
        req.period_start.CopyFrom(ts_s)
        req.period_end.CopyFrom(ts_e)
        return req

    return _make


def _metrics(mape: float) -> BacktestMetrics:
    return BacktestMetrics(
        mape=mape, rmse=0.1, mae=0.05, r2=0.2,
        directional_accuracy=0.6, win_rate=0.55, sample_size=100,
    )


def test_backtest_ok(make_request, servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    result = BacktestResult(
        backtest_report_id=42,
        model_metrics=_metrics(0.025),
        naive_metrics=_metrics(0.04),
        ohlcv_only_metrics=_metrics(0.035),
        signals_vs_actuals=[
            {"t": "2024-01-02", "predicted": 100.1, "actual": 100.3, "prev_close": 100.0},
        ],
    )
    with patch(
        "ml_forecast.api.backtest_service.runner.run",
        return_value=result,
    ):
        ctx = FakeContext()
        resp = servicer.Backtest(make_request(), ctx)

    assert ctx.code is None
    assert resp.backtest_report_id == 42
    assert resp.model_metrics.mape == pytest.approx(0.025)
    assert resp.naive_baseline_metrics.mape == pytest.approx(0.04)
    assert resp.ohlcv_only_baseline_metrics.mape == pytest.approx(0.035)
    assert resp.signal_count == 100
    # Model must beat both baselines for the SC-009 narrative.
    assert resp.model_metrics.mape < resp.naive_baseline_metrics.mape
    assert resp.model_metrics.mape < resp.ohlcv_only_baseline_metrics.mape


def test_backtest_invalid_period(make_request, servicer):
    """period_end before period_start → INVALID_ARGUMENT."""

    req = make_request(start=date(2024, 6, 1), end=date(2024, 1, 1))
    ctx = FakeContext()
    servicer.Backtest(req, ctx)
    assert ctx.code is grpc.StatusCode.INVALID_ARGUMENT


def test_backtest_model_not_found(make_request, servicer):
    """runner raises ModelNotFound → gRPC NOT_FOUND."""

    from ml_forecast.inference.registry import ModelNotFound

    with patch(
        "ml_forecast.api.backtest_service.runner.run",
        side_effect=ModelNotFound("no production"),
    ):
        ctx = FakeContext()
        servicer.Backtest(make_request(), ctx)
    assert ctx.code is grpc.StatusCode.NOT_FOUND


def test_backtest_archive_missing(make_request, servicer):
    """FileNotFoundError from runner → NOT_FOUND with detail."""

    with patch(
        "ml_forecast.api.backtest_service.runner.run",
        side_effect=FileNotFoundError("no such CSV"),
    ):
        ctx = FakeContext()
        servicer.Backtest(make_request(), ctx)
    assert ctx.code is grpc.StatusCode.NOT_FOUND
    assert "archive CSV" in (ctx.details or "")
