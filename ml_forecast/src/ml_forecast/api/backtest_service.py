"""gRPC Backtest RPC."""

from __future__ import annotations

import logging
from typing import Any

import grpc

from ml_forecast.backtest import runner
from ml_forecast.domain.timeframe import from_proto as tf_from_proto
from ml_forecast.inference import registry

log = logging.getLogger(__name__)


def _to_metrics_bundle(pb2: Any, m: runner.BacktestMetrics) -> Any:
    return pb2.MetricsBundle(
        mape=float(_finite(m.mape)),
        rmse=float(_finite(m.rmse)),
        mae=float(_finite(m.mae)),
        r_squared=float(_finite(m.r2)),
        directional_accuracy=float(_finite(m.directional_accuracy)),
        win_rate=float(_finite(m.win_rate)),
        sample_size=int(m.sample_size),
    )


def _finite(value: float) -> float:
    import math

    return value if math.isfinite(value) else 0.0


class BacktestServicer:
    """Concrete Backtest RPC (FR-011)."""

    def Backtest(self, request: Any, context: grpc.ServicerContext) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        ticker = str(request.ticker).upper().strip()
        try:
            timeframe = tf_from_proto(int(request.timeframe))
        except KeyError:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("timeframe_unspecified")
            return pb2.BacktestResponse()

        if not request.HasField("period_start") or not request.HasField("period_end"):
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("period_start and period_end required")
            return pb2.BacktestResponse()

        period_start = request.period_start.ToDatetime().date()
        period_end = request.period_end.ToDatetime().date()
        if period_end <= period_start:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("period_end must be strictly after period_start")
            return pb2.BacktestResponse()

        model_version = str(request.model_version).strip() or None

        try:
            result = runner.run(
                ticker=ticker,
                timeframe=timeframe,
                period_start=period_start,
                period_end=period_end,
                model_version=model_version,
            )
        except registry.ModelNotFound as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return pb2.BacktestResponse()
        except ValueError as exc:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(str(exc))
            return pb2.BacktestResponse()
        except FileNotFoundError as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"archive CSV missing: {exc}")
            return pb2.BacktestResponse()
        except Exception as exc:  # noqa: BLE001
            log.exception("backtest failed for %s/%s", ticker, timeframe.value)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"backtest_failed: {exc}")
            return pb2.BacktestResponse()

        return pb2.BacktestResponse(
            backtest_report_id=result.backtest_report_id,
            model_metrics=_to_metrics_bundle(pb2, result.model_metrics),
            naive_baseline_metrics=_to_metrics_bundle(pb2, result.naive_metrics),
            ohlcv_only_baseline_metrics=_to_metrics_bundle(pb2, result.ohlcv_only_metrics),
            signal_count=int(result.model_metrics.sample_size),
        )
