"""T048 / T050 / T051 — contract tests for Train + Promote/Archive RPCs.

Patches :mod:`ml_forecast.training.pipeline` + the registry so tests
stay independent of the archive CSV and the live DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import grpc
import pytest

from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.inference.registry import ModelHandle, ModelNotFound, PromoteFailed
from ml_forecast.models.base import ModelFamily
from ml_forecast.training.pipeline import TrainingResult


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
def train_servicer():
    from ml_forecast.api.train_service import TrainServicer

    return TrainServicer()


@pytest.fixture
def admin_servicer():
    from ml_forecast.api.admin_service import AdminServicer

    return AdminServicer()


@pytest.fixture
def make_train_request():
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    def _make(
        ticker: str = "SBER",
        tf: int = pb2.TIMEFRAME_D1,
        trigger: int = pb2.TRAIN_TRIGGER_MANUAL,
    ) -> Any:
        return pb2.TrainRequest(ticker=ticker, timeframe=tf, trigger=trigger)

    return _make


@pytest.fixture
def make_promote_request():
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    def _make(
        ticker: str = "SBER",
        tf: int = pb2.TIMEFRAME_D1,
        version: str = "armaexo-2024-01-01-abc123-abcd",
    ) -> Any:
        return pb2.PromoteRequest(ticker=ticker, timeframe=tf, model_version=version)

    return _make


def _handle(state: ModelState = ModelState.PRODUCTION) -> ModelHandle:
    return ModelHandle(
        id=77,
        ticker="SBER",
        timeframe=Timeframe.D1,
        model_version="armaexo-2024-01-01-abc123-abcd",
        model_family=ModelFamily.ARMAEXO,
        state=state,
        artifact_path="/tmp/dummy.joblib",
        current_mape=Decimal("0.03"),
        feature_set_version="v1",
        promoted_at=datetime.utcnow(),
        created_at=datetime.utcnow() - timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# T048 — Train RPC happy path
# ---------------------------------------------------------------------------

def test_train_blue_chip_guard_rejects_non_chip(make_train_request, train_servicer):
    with patch(
        "ml_forecast.api.train_service._is_blue_chip", return_value=False
    ):
        ctx = FakeContext()
        resp = train_servicer.Train(make_train_request(ticker="NONAME"), ctx)
    assert ctx.code is grpc.StatusCode.FAILED_PRECONDITION
    assert "non_blue_chip_mvp" in (ctx.details or "")


def test_train_synchronous_ok_returns_run_id(make_train_request, train_servicer):
    result = TrainingResult(
        training_run_id=42,
        ticker="SBER",
        timeframe=Timeframe.D1,
        family=ModelFamily.ARMAEXO,
        best_params={"ar_order": 3, "ma_order": 1},
        aggregate_metrics={"mape": 0.025},
        naive_metrics={"mape": 0.05},
        comparison_to_prev={"vs_naive_r2_delta": 0.05},
        promote_decision="auto_shadow",
        model_version="armaexo-2024-01-01-abcdef-abcd",
        duration_seconds=12.3,
    )
    with patch(
        "ml_forecast.api.train_service._is_blue_chip", return_value=True
    ), patch(
        "ml_forecast.api.train_service.pipeline.run", return_value=result
    ):
        ctx = FakeContext()
        resp = train_servicer.Train(make_train_request(), ctx)
    assert ctx.code is None
    assert resp.training_run_id == 42


# ---------------------------------------------------------------------------
# T050 — do_not_promote when naive R² gate fails inside pipeline
# ---------------------------------------------------------------------------

def test_train_does_not_raise_when_pipeline_marks_do_not_promote(
    make_train_request, train_servicer
):
    """Pipeline internally marks do_not_promote and returns normally — the
    RPC should treat that as a successful call and return training_run_id;
    polling GetTrainingRun reveals the do_not_promote state."""

    result = TrainingResult(
        training_run_id=43,
        ticker="SBER",
        timeframe=Timeframe.D1,
        family=ModelFamily.ARMAEXO,
        best_params={"ar_order": 1, "ma_order": 0},
        aggregate_metrics={"mape": 0.5, "r2": -1.0},
        naive_metrics={"mape": 0.1, "r2": 0.3},
        comparison_to_prev={"vs_naive_r2_delta": -1.3},
        promote_decision="do_not_promote",
        model_version=None,
        duration_seconds=5.0,
    )
    with patch(
        "ml_forecast.api.train_service._is_blue_chip", return_value=True
    ), patch(
        "ml_forecast.api.train_service.pipeline.run", return_value=result
    ):
        ctx = FakeContext()
        resp = train_servicer.Train(make_train_request(), ctx)
    assert ctx.code is None
    assert resp.training_run_id == 43


# ---------------------------------------------------------------------------
# T051 — Promote + Archive admin RPCs
# ---------------------------------------------------------------------------

def test_promote_transitions_shadow_to_production(
    make_promote_request, admin_servicer
):
    new = _handle(state=ModelState.PRODUCTION)
    prev = _handle(state=ModelState.ARCHIVED)
    with patch(
        "ml_forecast.api.admin_service.registry.promote",
        return_value=(new, prev),
    ):
        ctx = FakeContext()
        resp = admin_servicer.Promote(make_promote_request(), ctx)
    assert ctx.code is None
    assert resp.new_production.model_version == new.model_version
    assert resp.previous_production.model_version == prev.model_version


def test_promote_missing_version_is_not_found(
    make_promote_request, admin_servicer
):
    with patch(
        "ml_forecast.api.admin_service.registry.promote",
        side_effect=ModelNotFound("no version"),
    ):
        ctx = FakeContext()
        admin_servicer.Promote(make_promote_request(), ctx)
    assert ctx.code is grpc.StatusCode.NOT_FOUND


def test_promote_illegal_transition_surfaces_failed_precondition(
    make_promote_request, admin_servicer
):
    with patch(
        "ml_forecast.api.admin_service.registry.promote",
        side_effect=PromoteFailed("illegal transition archived -> production"),
    ):
        ctx = FakeContext()
        admin_servicer.Promote(make_promote_request(), ctx)
    assert ctx.code is grpc.StatusCode.FAILED_PRECONDITION
