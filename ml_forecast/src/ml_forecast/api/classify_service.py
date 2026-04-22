"""gRPC ClassifyLiquidity RPC (US5)."""

from __future__ import annotations

import logging
from typing import Any

import grpc

from ml_forecast.config import get_settings
from ml_forecast.features.ticker_features import extract_from_archive
from ml_forecast.models.classifier import LiquidityClassifier
from ml_forecast.storage.orm import ClassificationRun
from ml_forecast.storage.postgres import session_scope

log = logging.getLogger(__name__)


class ClassifyServicer:
    def ClassifyLiquidity(
        self, request: Any, context: grpc.ServicerContext
    ) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        feature_set_version = str(request.feature_set_version or "v1")

        archive_d1 = get_settings().archive_dir / "D1"
        try:
            df = extract_from_archive(archive_d1)
        except FileNotFoundError as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"archive missing: {exc}")
            return pb2.ClassifyLiquidityResponse()

        clf = LiquidityClassifier()
        try:
            report = clf.fit_and_evaluate(df)
        except ValueError as exc:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(str(exc))
            return pb2.ClassifyLiquidityResponse()
        except Exception as exc:  # noqa: BLE001
            log.exception("classifier training failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"classifier_failed: {exc}")
            return pb2.ClassifyLiquidityResponse()

        with session_scope() as s:
            row = ClassificationRun(
                feature_set_version=feature_set_version,
                total_tickers=report.total_tickers,
                blue_chip_recall=report.blue_chip_recall,
                other_recall=report.other_recall,
                confusion_matrix={"matrix": report.confusion_matrix},
                label_predictions={"rows": report.label_predictions},
            )
            s.add(row)
            s.flush()
            run_id = int(row.id)

        return pb2.ClassifyLiquidityResponse(
            classification_run_id=run_id,
            blue_chip_recall=report.blue_chip_recall,
            other_recall=report.other_recall,
            total_tickers=report.total_tickers,
        )
