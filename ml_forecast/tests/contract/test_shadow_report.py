"""T052 — GetShadowReport RPC contract test (hybrid: real DB + mocked registry)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import grpc
import pytest
from sqlalchemy import text

from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference.registry import ModelHandle
from ml_forecast.models.base import ModelFamily
from ml_forecast.storage.orm import ModelRegistry, ShadowPrediction
from ml_forecast.storage.postgres import session_scope

pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration-style contract test requires live Postgres",
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


@pytest.fixture
def servicer():
    from ml_forecast.api.admin_service import AdminServicer

    return AdminServicer()


def _seed_shadow_and_prod(session) -> tuple[int, int]:
    """Insert one shadow + one production row + 10 resolved shadow
    predictions. Returns (shadow_row_id, prod_row_id)."""

    # Ensure uniqueness across reruns.
    unique_suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")

    prod = ModelRegistry(
        ticker_id=1,
        timeframe="D1",
        model_family="armaexo",
        model_version=f"prod-{unique_suffix}",
        state="production",
        dataset_sha256="p" * 64,
        feature_set_version="v1",
        current_mape=Decimal("0.04"),
        promoted_at=datetime.utcnow() - timedelta(days=3),
    )
    shadow = ModelRegistry(
        ticker_id=1,
        timeframe="D1",
        model_family="armaexo",
        model_version=f"shadow-{unique_suffix}",
        state="shadow",
        dataset_sha256="s" * 64,
        feature_set_version="v1",
        current_mape=Decimal("0.025"),
    )
    session.add_all([prod, shadow])
    session.flush()
    # Back-date the first shadow prediction so trading_sessions_elapsed > 0.
    base_ts = datetime.utcnow() - timedelta(days=7)
    for i in range(10):
        session.add(
            ShadowPrediction(
                request_id=f"00000000-0000-0000-0000-{i:012d}",
                shadow_model_id=shadow.id,
                predicted_path={"rows": []},
                abs_error_pct=Decimal("0.02"),
                generated_at=base_ts + timedelta(hours=i),
            )
        )
    session.flush()
    return shadow.id, prod.id


def test_shadow_report_compares_metrics(servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

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
                "INSERT INTO public.tickers (id, symbol) VALUES (1, 'SBER') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        shadow_id, prod_id = _seed_shadow_and_prod(s)
        shadow_row = s.get(ModelRegistry, shadow_id)
        prod_row = s.get(ModelRegistry, prod_id)
        shadow_version = shadow_row.model_version
        shadow_handle = ModelHandle(
            id=shadow_row.id,
            ticker="SBER",
            timeframe=Timeframe.D1,
            model_version=shadow_row.model_version,
            model_family=ModelFamily.ARMAEXO,
            state=ModelState.SHADOW,
            artifact_path=None,
            current_mape=Decimal("0.025"),
            feature_set_version="v1",
            promoted_at=None,
            created_at=shadow_row.created_at,
        )
        prod_handle = ModelHandle(
            id=prod_row.id,
            ticker="SBER",
            timeframe=Timeframe.D1,
            model_version=prod_row.model_version,
            model_family=ModelFamily.ARMAEXO,
            state=ModelState.PRODUCTION,
            artifact_path=None,
            current_mape=Decimal("0.04"),
            feature_set_version="v1",
            promoted_at=prod_row.promoted_at,
            created_at=prod_row.created_at,
        )

    try:
        with patch(
            "ml_forecast.api.admin_service.registry.list_models",
            return_value=[shadow_handle, prod_handle],
        ):
            ctx = FakeContext()
            req = pb2.ShadowReportRequest(
                ticker="SBER",
                timeframe=pb2.TIMEFRAME_D1,
                shadow_model_version=shadow_version,
            )
            resp = servicer.GetShadowReport(req, ctx)

        assert ctx.code is None
        assert resp.shadow.model_version == shadow_version
        assert resp.production.model_version == prod_row.model_version
        assert resp.shadow_metrics.sample_size == 10
        # shadow abs_error_pct was 0.02 uniformly → reported mape ≈ 0.02
        assert resp.shadow_metrics.mape == pytest.approx(0.02, rel=1e-3)
        assert resp.production_metrics.mape == pytest.approx(0.04)
        assert resp.trading_sessions_elapsed >= 5
    finally:
        with session_scope() as s:
            s.execute(
                text(
                    "DELETE FROM ml.shadow_prediction "
                    "WHERE shadow_model_id IN (:a, :b)"
                ),
                {"a": shadow_id, "b": prod_id},
            )
            s.execute(
                text("DELETE FROM ml.model_registry WHERE id IN (:a, :b)"),
                {"a": shadow_id, "b": prod_id},
            )


def test_shadow_report_missing_shadow_is_not_found(servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    with patch(
        "ml_forecast.api.admin_service.registry.list_models", return_value=[]
    ):
        ctx = FakeContext()
        req = pb2.ShadowReportRequest(
            ticker="SBER",
            timeframe=pb2.TIMEFRAME_D1,
            shadow_model_version="missing",
        )
        servicer.GetShadowReport(req, ctx)
    assert ctx.code is grpc.StatusCode.NOT_FOUND
