"""T049 — GetTrainingRun RPC contract test."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest


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
    from ml_forecast.api.train_service import TrainServicer

    return TrainServicer()


def _make_row(status: str, trigger: str = "manual"):
    row = MagicMock()
    row.id = 42
    row.status = status
    row.trigger = trigger
    row.started_at = datetime.utcnow()
    row.finished_at = datetime.utcnow() if status != "running" else None
    row.metrics_aggregate = (
        {"mape": 0.03, "r2": 0.2, "directional_accuracy": 0.6}
        if status == "succeeded"
        else None
    )
    row.metrics_fold = (
        [
            {"mape": 0.03, "r2": 0.2, "directional_accuracy": 0.6}
            for _ in range(5)
        ]
        if status == "succeeded"
        else None
    )
    row.promote_decision = "auto_shadow" if status == "succeeded" else ""
    row.error = None
    return row


def test_get_training_run_running(servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        s = MagicMock()
        s.get.return_value = _make_row("running")
        yield s

    with patch("ml_forecast.api.train_service.session_scope", side_effect=_scope):
        ctx = FakeContext()
        req = pb2.GetTrainingRunRequest(training_run_id=42)
        resp = servicer.GetTrainingRun(req, ctx)

    assert ctx.code is None
    assert resp.training_run_id == 42
    assert resp.status == "running"


def test_get_training_run_succeeded_has_metrics(servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        s = MagicMock()
        s.get.return_value = _make_row("succeeded")
        yield s

    with patch("ml_forecast.api.train_service.session_scope", side_effect=_scope):
        ctx = FakeContext()
        req = pb2.GetTrainingRunRequest(training_run_id=42)
        resp = servicer.GetTrainingRun(req, ctx)

    assert ctx.code is None
    assert resp.status == "succeeded"
    assert resp.aggregate_metrics.mape == pytest.approx(0.03)
    assert len(resp.fold_metrics) >= 5
    assert resp.promote_decision == "auto_shadow"


def test_get_training_run_missing(servicer):
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        s = MagicMock()
        s.get.return_value = None
        yield s

    with patch("ml_forecast.api.train_service.session_scope", side_effect=_scope):
        ctx = FakeContext()
        req = pb2.GetTrainingRunRequest(training_run_id=9999)
        servicer.GetTrainingRun(req, ctx)

    assert ctx.code is grpc.StatusCode.NOT_FOUND
