"""T071 — N Forecast calls → N inference_log rows with correct fields."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import grpc
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select, text

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
from ml_forecast.storage.orm import InferenceLog
from ml_forecast.storage.postgres import session_scope
from ml_forecast.storage.redis_client import OhlcvSnapshot

pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration tests require ML_DATABASE_URL",
)


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


def _bars(n: int = 600) -> list[dict[str, Any]]:
    rng = np.random.default_rng(19)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    return [
        {
            "dt": idx[i].isoformat(),
            "o": float(close[i]), "h": float(close[i] + 1),
            "l": float(close[i] - 1), "c": float(close[i]),
            "v": float(rng.integers(100, 10000)),
        }
        for i in range(n)
    ]


def _ensure_model_row(ticker_id: int) -> int:
    """Create a real ml.model_registry row so inference_log FK passes."""

    from ml_forecast.storage.orm import ModelRegistry

    with session_scope() as s:
        existing = s.execute(
            text(
                "SELECT id FROM ml.model_registry "
                "WHERE ticker_id = :tid AND model_version = 'test-v1'"
            ),
            {"tid": ticker_id},
        ).first()
        if existing:
            return int(existing[0])
        row = ModelRegistry(
            ticker_id=ticker_id,
            timeframe="D1",
            model_family="armaexo",
            model_version="test-v1",
            state="shadow",  # not production to avoid clashing with other tests
            dataset_sha256="a" * 64,
            feature_set_version="v1",
        )
        s.add(row)
        s.flush()
        return int(row.id)


def _handle(model_id: int) -> ModelHandle:
    return ModelHandle(
        id=model_id,
        ticker="LOGSE",
        timeframe=Timeframe.D1,
        model_version="test-v1",
        model_family=ModelFamily.ARMAEXO,
        state=ModelState.PRODUCTION,
        artifact_path=None,
        current_mape=Decimal("0.03"),
        feature_set_version="v1",
        promoted_at=datetime.utcnow() - timedelta(days=1),
        created_at=datetime.utcnow() - timedelta(days=1),
    )


def _forecaster_stub():
    from unittest.mock import MagicMock

    m = MagicMock()
    base = datetime.utcnow()
    m.predict.return_value = [
        PricePoint(t=base + timedelta(days=i + 1),
                   mean=Decimal("101"), lo=Decimal("100"), hi=Decimal("102"))
        for i in range(4)
    ]
    m.factor_contributions.return_value = [
        FactorContribution(name=f"feat_{i}", value=Decimal("0.5"),
                           contribution=Decimal("0.3"),
                           source="ohlcv", status=FactorStatus.OK)
        for i in range(3)
    ]
    return m


def test_n_forecast_calls_write_n_rows():
    from ml_forecast.api.forecast_service import ForecastServicer
    from ml_forecast.features.sentiment_features import SentimentFeature
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    # Ensure target ticker exists so inference_log ticker_id resolves.
    with session_scope() as s:
        s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.tickers "
                "(id BIGSERIAL PRIMARY KEY, symbol VARCHAR(20) UNIQUE NOT NULL, "
                "is_blue_chip BOOLEAN DEFAULT true, is_active BOOLEAN DEFAULT true)"
            )
        )
        s.execute(
            text(
                "INSERT INTO public.tickers (symbol) VALUES ('LOGSE') "
                "ON CONFLICT (symbol) DO NOTHING"
            )
        )
    tid = _ticker_id_for("LOGSE")
    model_id = _ensure_model_row(tid)

    with patch(
        "ml_forecast.api.forecast_service.freshness.check_ohlcv",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.freshness.check_sentiment",
        return_value=SourceFreshness.FRESH,
    ), patch(
        "ml_forecast.api.forecast_service.registry.get_production",
        return_value=_handle(model_id),
    ), patch(
        "ml_forecast.api.forecast_service.registry.load_forecaster",
        return_value=_forecaster_stub(),
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
        before = _count_logs("LOGSE")
        for _ in range(5):
            servicer.Forecast(
                pb2.ForecastRequest(
                    ticker="LOGSE", timeframe=pb2.TIMEFRAME_D1, horizon=4
                ),
                FakeContext(),
            )
        after = _count_logs("LOGSE")

    assert after - before == 5

    # Fields must be populated on the most recent row.
    with session_scope() as s:
        latest = list(
            s.execute(
                select(InferenceLog)
                .where(InferenceLog.ticker_id == _ticker_id_for("LOGSE"))
                .order_by(InferenceLog.generated_at.desc())
                .limit(1)
            ).scalars()
        )[0]
    assert latest.status == "ok"
    assert latest.model_id == model_id
    assert latest.latency_ms >= 0
    assert latest.mape_at_generation is not None
    assert latest.source_availability["ohlcv"] == "fresh"


def _count_logs(ticker: str) -> int:
    tid = _ticker_id_for(ticker)
    with session_scope() as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM ml.inference_log WHERE ticker_id = :t"),
                {"t": tid},
            ).scalar_one()
        )


def _ticker_id_for(ticker: str) -> int:
    with session_scope() as s:
        row = s.execute(
            text("SELECT id FROM public.tickers WHERE symbol = :s"),
            {"s": ticker},
        ).first()
        return int(row[0]) if row else 0
