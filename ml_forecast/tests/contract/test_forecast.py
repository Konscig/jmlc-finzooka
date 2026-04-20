"""T034-T038 contract tests for the Forecast RPC.

Exercises the servicer directly with a fake :class:`grpc.ServicerContext`
and mocked dependencies (Redis, registry, session_scope). Tests require
the generated proto stubs — they are produced during the Docker image
build, so these tests run inside the container (same pattern as
test_health.py).
"""

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

from ml_forecast.domain.factor import (
    FactorContribution,
    FactorStatus,
    SourceFreshness,
)
from ml_forecast.domain.forecast import PricePoint
from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference.registry import ModelHandle, ModelNotFound
from ml_forecast.models.base import ModelFamily
from ml_forecast.storage.redis_client import OhlcvSnapshot, SentimentSnapshot


# ---------------------------------------------------------------------------
# Fake context
# ---------------------------------------------------------------------------

@dataclass
class FakeContext:
    code: grpc.StatusCode | None = None
    details: str | None = None
    _peer: str = "test"
    metadata: list[tuple[str, str]] = field(default_factory=list)

    def set_code(self, code: grpc.StatusCode) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details

    def peer(self) -> str:
        return self._peer

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self.metadata


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_bars(n: int = 600) -> list[dict[str, Any]]:
    rng = np.random.default_rng(123)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, size=n))
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, size=n)
    low = np.maximum(close - rng.uniform(0.1, 1.0, size=n), 0.5)
    open_ = close + rng.uniform(-0.3, 0.3, size=n)
    low = np.minimum(low, np.minimum(open_, close) - 0.01)
    high = np.maximum(high, np.maximum(open_, close) + 0.01)
    low = np.maximum(low, 0.1)
    volume = rng.integers(100, 10000, size=n).astype(float)
    return [
        {"dt": idx[i].isoformat(), "o": float(open_[i]), "h": float(high[i]),
         "l": float(low[i]), "c": float(close[i]), "v": float(volume[i])}
        for i in range(n)
    ]


def _handle(ticker: str = "SBER", tf: Timeframe = Timeframe.D1) -> ModelHandle:
    return ModelHandle(
        id=42,
        ticker=ticker,
        timeframe=tf,
        model_version="armaexo-2024-01-01-abc123-abcd",
        model_family=ModelFamily.ARMAEXO,
        state=ModelState.PRODUCTION,
        artifact_path="/tmp/dummy.joblib",
        current_mape=Decimal("0.03"),
        feature_set_version="v1",
        promoted_at=datetime.utcnow() - timedelta(days=3),
        created_at=datetime.utcnow() - timedelta(days=3),
    )


def _forecaster_stub(path_len: int = 4) -> MagicMock:
    base = datetime.utcnow()
    m = MagicMock()
    m.predict.return_value = [
        PricePoint(
            t=base + timedelta(days=i + 1),
            mean=Decimal("101.5"),
            lo=Decimal("100.0"),
            hi=Decimal("103.0"),
        )
        for i in range(path_len)
    ]
    m.factor_contributions.return_value = [
        FactorContribution(
            name="rsi_14", value=Decimal("55.0"),
            contribution=Decimal("0.25"), source="ohlcv", status=FactorStatus.OK,
        ),
        FactorContribution(
            name="atr_14", value=Decimal("1.2"),
            contribution=Decimal("0.2"), source="ohlcv", status=FactorStatus.OK,
        ),
        FactorContribution(
            name="macd_line", value=Decimal("0.8"),
            contribution=Decimal("0.15"), source="ohlcv", status=FactorStatus.OK,
        ),
    ]
    return m


def _ohlcv_snap(fresh: bool = True, bars: int = 600) -> OhlcvSnapshot:
    return OhlcvSnapshot(
        bars=_make_bars(bars),
        last_ts=datetime.now(timezone.utc) - (
            timedelta(seconds=1) if fresh else timedelta(days=5)
        ),
        source="test",
        schema_version=1,
    )


def _sentiment_snap(sources: tuple[str, ...] = ("tpulse", "news")) -> SentimentSnapshot:
    return SentimentSnapshot(
        score=0.15, confidence=0.8, sources=list(sources),
        window_seconds=3600,
        last_ts=datetime.now(timezone.utc) - timedelta(seconds=60),
        schema_version=1,
    )


@pytest.fixture
def make_request():
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    def _make(
        ticker: str = "SBER",
        tf: int = pb2.TIMEFRAME_D1,
        horizon: int = 4,
    ):
        return pb2.ForecastRequest(ticker=ticker, timeframe=tf, horizon=horizon)

    return _make


@pytest.fixture
def servicer():
    from ml_forecast.api.forecast_service import ForecastServicer

    return ForecastServicer()


@pytest.fixture(autouse=True)
def _patch_backend_writes():
    """Stub out session_scope + ticker lookups so tests do not need a live DB."""

    from contextlib import contextmanager

    @contextmanager
    def _fake_scope():
        fake_session = MagicMock()
        fake_session.execute.return_value.first.return_value = (1,)
        yield fake_session

    with patch(
        "ml_forecast.api.forecast_service.session_scope",
        side_effect=_fake_scope,
    ), patch(
        "ml_forecast.api.forecast_service._lookup_ticker_id", return_value=1
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_forecast_ok_happy_path(make_request, servicer):
    """T034 — US-1 AC1: all fields present, factors ≥ 3, status OK."""

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
        return_value=_forecaster_stub(),
    ), patch(
        "ml_forecast.api.forecast_service.redis_client.get_ohlcv_last",
        return_value=_ohlcv_snap(),
    ), patch(
        "ml_forecast.api.forecast_service.sentiment_score",
        return_value=__import__(
            "ml_forecast.features.sentiment_features", fromlist=["SentimentFeature"]
        ).SentimentFeature(value=Decimal("0.15"), status=FactorStatus.OK),
    ):
        ctx = FakeContext()
        resp = servicer.Forecast(make_request(horizon=4), ctx)

    assert ctx.code is None  # no abort
    assert resp.request_id
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    assert resp.status == pb2.FORECAST_STATUS_OK
    assert len(resp.predicted_path) == 4
    assert len(resp.factors) >= 3
    assert resp.explanation
    assert resp.model_version == "armaexo-2024-01-01-abc123-abcd"
    assert resp.source_availability.ohlcv == pb2.SOURCE_FRESHNESS_FRESH
    assert resp.source_availability.sentiment == pb2.SOURCE_FRESHNESS_FRESH
    assert resp.model_stale is False
    assert resp.anomalous_last_bar is False


def test_forecast_insufficient_history(make_request, servicer):
    """T035 — US-1 AC2: <500 bars → FAILED_PRECONDITION/insufficient_history."""

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
        return_value=_forecaster_stub(),
    ), patch(
        "ml_forecast.api.forecast_service.redis_client.get_ohlcv_last",
        return_value=_ohlcv_snap(bars=100),
    ):
        ctx = FakeContext()
        servicer.Forecast(make_request(), ctx)

    assert ctx.code is grpc.StatusCode.FAILED_PRECONDITION
    assert "insufficient_history" in (ctx.details or "").lower() or "500" in (ctx.details or "")


def test_forecast_horizon_out_of_range(make_request, servicer):
    """T036 — US-1 AC3: horizon > trained max → FAILED_PRECONDITION/horizon_out_of_range."""

    ctx = FakeContext()
    servicer.Forecast(make_request(horizon=100), ctx)
    assert ctx.code is grpc.StatusCode.FAILED_PRECONDITION
    assert "horizon" in (ctx.details or "").lower()


def test_forecast_degraded_sentiment(make_request, servicer):
    """T037 — US-1 AC4: stale sentiment → DEGRADED, sentiment factor STALE."""

    from ml_forecast.features.sentiment_features import SentimentFeature

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.freshness.check_sentiment",
        return_value=SourceFreshness.STALE,
    ), patch(
        "ml_forecast.api.forecast_service.registry.get_production",
        return_value=_handle(),
    ), patch(
        "ml_forecast.api.forecast_service.registry.load_forecaster",
        return_value=_forecaster_stub(),
    ), patch(
        "ml_forecast.api.forecast_service.redis_client.get_ohlcv_last",
        return_value=_ohlcv_snap(),
    ), patch(
        "ml_forecast.api.forecast_service.sentiment_score",
        return_value=SentimentFeature(value=None, status=FactorStatus.UNAVAILABLE),
    ):
        ctx = FakeContext()
        resp = servicer.Forecast(make_request(), ctx)

    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    assert ctx.code is None
    assert resp.status == pb2.FORECAST_STATUS_DEGRADED
    assert resp.source_availability.sentiment == pb2.SOURCE_FRESHNESS_STALE
    # sentiment factor should appear, either UNAVAILABLE or STALE.
    sentiment_factor = next(f for f in resp.factors if f.name == "sentiment_score")
    assert sentiment_factor.status in (
        pb2.FACTOR_STATUS_STALE,
        pb2.FACTOR_STATUS_UNAVAILABLE,
    )


def test_forecast_stale_ohlcv(make_request, servicer):
    """T038 — stale OHLCV → FAILED_PRECONDITION/stale_ohlcv."""

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.STALE,
    ):
        ctx = FakeContext()
        servicer.Forecast(make_request(), ctx)

    assert ctx.code is grpc.StatusCode.FAILED_PRECONDITION
    assert "stale_ohlcv" in (ctx.details or "").lower() or "stale" in (ctx.details or "").lower()


def test_forecast_model_not_found(make_request, servicer):
    """US-1 AC: NOT_FOUND when no production model registered."""

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.freshness.check_sentiment",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.registry.get_production",
        side_effect=ModelNotFound("no production model"),
    ):
        ctx = FakeContext()
        servicer.Forecast(make_request(), ctx)

    assert ctx.code is grpc.StatusCode.NOT_FOUND


def test_forecast_model_stale_flag(make_request, servicer):
    """T095 fragment — model_stale=true when promoted_at > 14 days ago."""

    old_handle = _handle()
    old_handle = ModelHandle(
        id=old_handle.id,
        ticker=old_handle.ticker,
        timeframe=old_handle.timeframe,
        model_version=old_handle.model_version,
        model_family=old_handle.model_family,
        state=old_handle.state,
        artifact_path=old_handle.artifact_path,
        current_mape=old_handle.current_mape,
        feature_set_version=old_handle.feature_set_version,
        promoted_at=datetime.utcnow() - timedelta(days=30),
        created_at=old_handle.created_at,
    )
    from ml_forecast.features.sentiment_features import SentimentFeature

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.freshness.check_sentiment",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.registry.get_production",
        return_value=old_handle,
    ), patch(
        "ml_forecast.api.forecast_service.registry.load_forecaster",
        return_value=_forecaster_stub(),
    ), patch(
        "ml_forecast.api.forecast_service.redis_client.get_ohlcv_last",
        return_value=_ohlcv_snap(),
    ), patch(
        "ml_forecast.api.forecast_service.sentiment_score",
        return_value=SentimentFeature(value=Decimal("0.0"), status=FactorStatus.OK),
    ):
        ctx = FakeContext()
        resp = servicer.Forecast(make_request(), ctx)

    assert ctx.code is None
    assert resp.model_stale is True
