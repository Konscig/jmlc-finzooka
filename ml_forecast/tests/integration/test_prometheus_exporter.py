"""T072 — Prometheus metrics populated after Forecast calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import numpy as np
import pandas as pd
import pytest
from prometheus_client import generate_latest

from ml_forecast.domain.factor import (
    FactorContribution,
    FactorStatus,
    SourceFreshness,
)
from ml_forecast.domain.forecast import PricePoint
from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference.registry import ModelHandle
from ml_forecast.models.base import ModelFamily
from ml_forecast.observability import metrics as obs_metrics
from ml_forecast.storage.redis_client import OhlcvSnapshot


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


def _bars(n: int = 600):
    rng = np.random.default_rng(31)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    return [
        {"dt": idx[i].isoformat(), "o": float(close[i]), "h": float(close[i] + 1),
         "l": float(close[i] - 1), "c": float(close[i]),
         "v": float(rng.integers(100, 10_000))}
        for i in range(n)
    ]


def _handle() -> ModelHandle:
    return ModelHandle(
        id=1, ticker="MTRC", timeframe=Timeframe.D1, model_version="v1",
        model_family=ModelFamily.ARMAEXO, state=ModelState.PRODUCTION,
        artifact_path=None, current_mape=Decimal("0.03"),
        feature_set_version="v1",
        promoted_at=datetime.utcnow() - timedelta(days=1),
        created_at=datetime.utcnow() - timedelta(days=1),
    )


def _forecaster():
    m = MagicMock()
    base = datetime.utcnow()
    m.predict.return_value = [
        PricePoint(t=base + timedelta(days=i + 1),
                   mean=Decimal("100"), lo=Decimal("99"), hi=Decimal("101"))
        for i in range(3)
    ]
    m.factor_contributions.return_value = [
        FactorContribution(name=f"f{i}", value=Decimal("1"),
                           contribution=Decimal("0.33"),
                           source="ohlcv", status=FactorStatus.OK)
        for i in range(3)
    ]
    return m


@pytest.fixture(autouse=True)
def _stub_db_write():
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        s = MagicMock()
        s.execute.return_value.first.return_value = (1,)
        yield s

    with patch(
        "ml_forecast.api.forecast_service.session_scope", side_effect=_scope
    ), patch(
        "ml_forecast.api.forecast_service._lookup_ticker_id", return_value=1
    ):
        yield


def test_metrics_counters_advance_after_forecasts():
    from ml_forecast.api.forecast_service import ForecastServicer
    from ml_forecast.features.sentiment_features import SentimentFeature
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    before = float(obs_metrics.forecast_total.labels(status="ok")._value.get())

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.freshness.check_sentiment",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.registry.get_production",
        return_value=_handle(),
    ), patch(
        "ml_forecast.api.forecast_service.registry.load_forecaster",
        return_value=_forecaster(),
    ), patch(
        "ml_forecast.api.forecast_service.redis_client.get_ohlcv_last",
        return_value=OhlcvSnapshot(
            bars=_bars(), last_ts=datetime.now(tz=timezone.utc),
            source="test", schema_version=1,
        ),
    ), patch(
        "ml_forecast.api.forecast_service.sentiment_score",
        return_value=SentimentFeature(value=Decimal("0.0"), status=FactorStatus.OK),
    ), patch(
        "ml_forecast.api.forecast_service.shadow.enqueue_for_request",
        return_value=None,
    ):
        servicer = ForecastServicer()
        for _ in range(3):
            servicer.Forecast(
                pb2.ForecastRequest(
                    ticker="MTRC", timeframe=pb2.TIMEFRAME_D1, horizon=3
                ),
                FakeContext(),
            )

    after = float(obs_metrics.forecast_total.labels(status="ok")._value.get())
    assert after - before == 3

    # /metrics payload contains the expected families.
    payload = generate_latest(obs_metrics.REGISTRY).decode("utf-8")
    for fam in (
        "ml_forecast_forecast_total",
        "ml_forecast_latency_seconds",
        "ml_forecast_current_mape",
    ):
        assert fam in payload
