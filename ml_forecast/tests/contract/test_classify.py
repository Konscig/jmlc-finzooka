"""T080 — ClassifyLiquidity RPC contract test on mocked feature extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import grpc
import pandas as pd
import pytest

from ml_forecast.models.classifier import ClassificationReport


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
    from ml_forecast.api.classify_service import ClassifyServicer

    return ClassifyServicer()


@pytest.fixture
def make_request():
    from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

    def _make(version: str = "v1") -> Any:
        return pb2.ClassifyLiquidityRequest(feature_set_version=version)

    return _make


@pytest.fixture(autouse=True)
def _stub_db_write():
    """Bypass session_scope so the contract test does not need a live DB.

    When the servicer ``.add(row)``s a ClassificationRun, we mimic the
    DB auto-generated PK by assigning ``row.id`` on the fly — otherwise
    the subsequent ``int(row.id)`` would choke on MagicMock's None.
    """

    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def _scope():
        s = MagicMock()

        def _add(obj):
            if getattr(obj, "id", None) is None:
                obj.id = 1
            return None

        s.add.side_effect = _add
        yield s

    with patch("ml_forecast.api.classify_service.session_scope", side_effect=_scope):
        yield


def test_classify_ok(make_request, servicer):
    report = ClassificationReport(
        total_tickers=249,
        blue_chip_recall=0.85,
        other_recall=0.93,
        confusion_matrix=[[45, 2], [1, 12]],
        label_predictions=[
            {"ticker": "SBER", "actual": 1, "predicted": 1, "prob_blue_chip": 0.95},
        ],
        used_smote=False,
    )
    fake_df = pd.DataFrame({"ticker": ["SBER"], "is_blue_chip": [1]})

    with patch(
        "ml_forecast.api.classify_service.extract_from_archive",
        return_value=fake_df,
    ), patch(
        "ml_forecast.api.classify_service.LiquidityClassifier.fit_and_evaluate",
        return_value=report,
    ):
        # session_scope stub loses the auto-generated PK, so pre-empt the
        # id assignment via a side-effect on `add`.
        ctx = FakeContext()
        resp = servicer.ClassifyLiquidity(make_request(), ctx)

    assert ctx.code is None
    assert resp.total_tickers == 249
    assert resp.blue_chip_recall == pytest.approx(0.85)
    assert resp.other_recall == pytest.approx(0.93)


def test_classify_archive_missing(make_request, servicer):
    with patch(
        "ml_forecast.api.classify_service.extract_from_archive",
        side_effect=FileNotFoundError("archive missing"),
    ):
        ctx = FakeContext()
        servicer.ClassifyLiquidity(make_request(), ctx)
    assert ctx.code is grpc.StatusCode.NOT_FOUND
