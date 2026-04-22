"""gRPC Train + GetTrainingRun RPCs.

MVP note: Train is synchronous — we block the RPC for the duration of
the pipeline (can reach minutes on SBER/D1). Celery enqueue lands in
T055; the contract is forward-compatible because the response already
carries ``training_run_id`` for later polling via GetTrainingRun.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

import grpc
from sqlalchemy import text as sqltext

from ml_forecast.domain.timeframe import Timeframe, from_proto as tf_from_proto
from ml_forecast.storage.orm import TrainingRun
from ml_forecast.storage.postgres import session_scope
from ml_forecast.training import pipeline

log = logging.getLogger(__name__)

# Advisory-lock guard per (ticker, timeframe). Simple in-process lock for
# MVP; when Celery is wired in (T055) this moves to pg_advisory_xact_lock
# which also covers multiple worker processes.
_LOCAL_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_MUTEX = threading.Lock()


def _lock_for(ticker: str, timeframe: Timeframe) -> threading.Lock:
    key = (ticker, timeframe.value)
    with _LOCKS_MUTEX:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
    return lock


def _is_blue_chip(ticker: str) -> bool:
    with session_scope() as s:
        row = s.execute(
            sqltext("SELECT is_blue_chip FROM public.tickers WHERE symbol = :s"),
            {"s": ticker},
        ).first()
        return bool(row and row[0])


class TrainServicer:
    """Concrete Train + GetTrainingRun RPCs."""

    def Train(self, request: Any, context: grpc.ServicerContext) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        ticker = str(request.ticker).upper().strip()
        try:
            timeframe = tf_from_proto(int(request.timeframe))
        except KeyError:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("timeframe_unspecified")
            return pb2.TrainResponse()
        force = bool(getattr(request, "force", False))

        # (a) blue-chip guard (FR-012). Skip if ticker is unregistered — let
        # the deeper lookup in pipeline.run surface a clearer error.
        if not _is_blue_chip(ticker):
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("non_blue_chip_mvp")
            return pb2.TrainResponse()

        # (b) concurrent-train guard.
        lock = _lock_for(ticker, timeframe)
        if not lock.acquire(blocking=force):
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("already_running")
            return pb2.TrainResponse()

        try:
            log.info(
                "training start: ticker=%s timeframe=%s trigger=%s",
                ticker, timeframe.value,
                pb2.TrainTrigger.Name(int(request.trigger)),
            )
            result = pipeline.run(
                ticker=ticker,
                timeframe=timeframe,
                trigger=_trigger_name(pb2, int(request.trigger)),
            )
        except pipeline.LookupError as exc:  # ticker missing
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return pb2.TrainResponse()
        except Exception as exc:  # noqa: BLE001
            log.exception("training failed for %s/%s", ticker, timeframe.value)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"training_failed: {exc}")
            return pb2.TrainResponse()
        finally:
            lock.release()

        from google.protobuf import timestamp_pb2

        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(datetime.utcnow())
        return pb2.TrainResponse(
            training_run_id=result.training_run_id,
            enqueued_at=ts,
            queue="sync",  # will be "train_queue" once T055 wires Celery
        )

    def GetTrainingRun(self, request: Any, context: grpc.ServicerContext) -> Any:
        from google.protobuf import timestamp_pb2

        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        run_id = int(request.training_run_id)
        with session_scope() as s:
            row = s.get(TrainingRun, run_id)
            if row is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(f"training_run {run_id} not found")
                return pb2.GetTrainingRunResponse()

            resp = pb2.GetTrainingRunResponse(
                training_run_id=row.id,
                status=row.status,
                trigger=_trigger_enum(pb2, row.trigger),
                promote_decision=row.promote_decision or "",
                error=row.error or "",
            )
            if row.started_at:
                ts = timestamp_pb2.Timestamp()
                ts.FromDatetime(row.started_at)
                resp.started_at.CopyFrom(ts)
            if row.finished_at:
                ts = timestamp_pb2.Timestamp()
                ts.FromDatetime(row.finished_at)
                resp.finished_at.CopyFrom(ts)
            if row.metrics_aggregate:
                resp.aggregate_metrics.CopyFrom(_to_metrics_bundle(pb2, row.metrics_aggregate))
            if row.metrics_fold:
                for fold in row.metrics_fold:
                    resp.fold_metrics.append(_to_metrics_bundle(pb2, fold))
            return resp


def _trigger_name(pb2: Any, trigger_enum: int) -> str:
    name = pb2.TrainTrigger.Name(trigger_enum)
    # Map TRAIN_TRIGGER_MANUAL → "manual", etc.
    return name.removeprefix("TRAIN_TRIGGER_").lower()


def _trigger_enum(pb2: Any, trigger_name: str) -> int:
    upper = f"TRAIN_TRIGGER_{trigger_name.upper()}"
    try:
        return int(pb2.TrainTrigger.Value(upper))
    except ValueError:
        return int(pb2.TRAIN_TRIGGER_UNSPECIFIED)


def _to_metrics_bundle(pb2: Any, d: dict[str, Any]) -> Any:
    bundle = pb2.MetricsBundle(
        mape=float(d.get("mape") or 0.0),
        rmse=float(d.get("rmse") or 0.0),
        mae=float(d.get("mae") or 0.0),
        r_squared=float(d.get("r2") or 0.0),
        directional_accuracy=float(d.get("directional_accuracy") or 0.0),
        win_rate=float(d.get("win_rate") or 0.0),
        sample_size=int(d.get("sample_size") or 0),
    )
    return bundle
