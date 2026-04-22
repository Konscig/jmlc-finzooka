"""Admin gRPC RPCs — Promote, Archive, ListModels, GetShadowReport.

All state mutations route through :mod:`ml_forecast.inference.registry`
so the ModelState transition invariants + partial UK are enforced in
exactly one place.
"""

from __future__ import annotations

import logging
from typing import Any

import grpc

from ml_forecast.domain.model_state import ModelState
from ml_forecast.domain.timeframe import Timeframe, from_proto as tf_from_proto
from ml_forecast.inference import registry
from ml_forecast.models.base import ModelFamily

log = logging.getLogger(__name__)


def _to_handle_proto(pb2: Any, handle: registry.ModelHandle) -> Any:
    return pb2.ModelHandle(
        ticker=handle.ticker,
        timeframe=_timeframe_to_proto(pb2, handle.timeframe),
        model_version=handle.model_version,
        family=_family_to_proto(pb2, handle.model_family),
        state=_state_to_proto(pb2, handle.state),
    )


def _timeframe_to_proto(pb2: Any, tf: Timeframe) -> int:
    from ml_forecast.domain.timeframe import to_proto as tf_to_proto

    return int(tf_to_proto(tf))


def _family_to_proto(pb2: Any, family: ModelFamily) -> int:
    mapping = {
        ModelFamily.ARMAEXO: pb2.MODEL_FAMILY_ARMAEXO,
        ModelFamily.LIGHTGBM: pb2.MODEL_FAMILY_LIGHTGBM,
        ModelFamily.BASELINE_NAIVE: pb2.MODEL_FAMILY_BASELINE_NAIVE,
        ModelFamily.BASELINE_OHLCV_ONLY: pb2.MODEL_FAMILY_BASELINE_OHLCV_ONLY,
    }
    return int(mapping.get(family, pb2.MODEL_FAMILY_UNSPECIFIED))


def _state_to_proto(pb2: Any, state: ModelState) -> int:
    mapping = {
        ModelState.TRAINING: pb2.MODEL_STATE_TRAINING,
        ModelState.SHADOW: pb2.MODEL_STATE_SHADOW,
        ModelState.PRODUCTION: pb2.MODEL_STATE_PRODUCTION,
        ModelState.ARCHIVED: pb2.MODEL_STATE_ARCHIVED,
        ModelState.DO_NOT_PROMOTE: pb2.MODEL_STATE_DO_NOT_PROMOTE,
    }
    return int(mapping.get(state, pb2.MODEL_STATE_UNSPECIFIED))


class AdminServicer:
    """Concrete Promote / Archive / ListModels / GetShadowReport RPCs."""

    def Promote(self, request: Any, context: grpc.ServicerContext) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        ticker = str(request.ticker).upper().strip()
        try:
            timeframe = tf_from_proto(int(request.timeframe))
        except KeyError:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("timeframe_unspecified")
            return pb2.PromoteResponse()
        model_version = str(request.model_version).strip()
        if not model_version:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("model_version required")
            return pb2.PromoteResponse()

        try:
            new_handle, previous = registry.promote(ticker, timeframe, model_version)
        except registry.ModelNotFound as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return pb2.PromoteResponse()
        except registry.PromoteFailed as exc:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(str(exc))
            return pb2.PromoteResponse()

        log.info(
            "promoted %s/%s#%s (previous=%s)",
            ticker, timeframe.value, model_version,
            previous.model_version if previous else "none",
        )
        resp = pb2.PromoteResponse(new_production=_to_handle_proto(pb2, new_handle))
        if previous is not None:
            resp.previous_production.CopyFrom(_to_handle_proto(pb2, previous))
        return resp

    def Archive(self, request: Any, context: grpc.ServicerContext) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        ticker = str(request.ticker).upper().strip()
        try:
            timeframe = tf_from_proto(int(request.timeframe))
        except KeyError:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("timeframe_unspecified")
            return pb2.ArchiveResponse()
        model_version = str(request.model_version).strip()

        try:
            handle = registry.archive(ticker, timeframe, model_version)
        except registry.ModelNotFound as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return pb2.ArchiveResponse()
        except registry.PromoteFailed as exc:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(str(exc))
            return pb2.ArchiveResponse()

        return pb2.ArchiveResponse(archived=_to_handle_proto(pb2, handle))

    def ListModels(self, request: Any, context: grpc.ServicerContext) -> Any:
        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2

        ticker = str(request.ticker).upper().strip() or None
        try:
            tf_enum = int(request.timeframe)
            timeframe = tf_from_proto(tf_enum) if tf_enum != pb2.TIMEFRAME_UNSPECIFIED else None
        except KeyError:
            timeframe = None
        state_enum = int(request.state)
        state_filter: ModelState | None = None
        if state_enum != pb2.MODEL_STATE_UNSPECIFIED:
            state_map = {
                pb2.MODEL_STATE_TRAINING: ModelState.TRAINING,
                pb2.MODEL_STATE_SHADOW: ModelState.SHADOW,
                pb2.MODEL_STATE_PRODUCTION: ModelState.PRODUCTION,
                pb2.MODEL_STATE_ARCHIVED: ModelState.ARCHIVED,
                pb2.MODEL_STATE_DO_NOT_PROMOTE: ModelState.DO_NOT_PROMOTE,
            }
            state_filter = state_map.get(state_enum)

        handles = registry.list_models(
            ticker=ticker, timeframe=timeframe, state=state_filter
        )
        resp = pb2.ListModelsResponse()
        for handle in handles:
            resp.models.append(_to_handle_proto(pb2, handle))
        return resp

    def GetShadowReport(self, request: Any, context: grpc.ServicerContext) -> Any:
        """Compare shadow vs production metrics + elapsed trading sessions (T063)."""

        from datetime import datetime as _dt

        from sqlalchemy import select

        from ml_forecast.grpc_gen.finzooka.ml.v1 import ml_forecast_pb2 as pb2
        from ml_forecast.storage.orm import ShadowPrediction
        from ml_forecast.storage.postgres import session_scope

        ticker = str(request.ticker).upper().strip()
        try:
            timeframe = tf_from_proto(int(request.timeframe))
        except KeyError:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("timeframe_unspecified")
            return pb2.ShadowReportResponse()
        shadow_version = str(request.shadow_model_version).strip()

        try:
            all_handles = registry.list_models(ticker=ticker, timeframe=timeframe)
        except registry.ModelNotFound as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return pb2.ShadowReportResponse()

        shadow_handle = next(
            (h for h in all_handles if h.model_version == shadow_version), None
        )
        prod_handle = next(
            (h for h in all_handles if h.state == ModelState.PRODUCTION), None
        )
        if shadow_handle is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"no shadow {ticker}/{timeframe.value}#{shadow_version}")
            return pb2.ShadowReportResponse()

        # Aggregate abs_error_pct across resolved shadow_prediction rows.
        with session_scope() as s:
            rows = list(
                s.execute(
                    select(ShadowPrediction.abs_error_pct)
                    .where(ShadowPrediction.shadow_model_id == shadow_handle.id)
                    .where(ShadowPrediction.abs_error_pct.isnot(None))
                )
            )
            earliest = s.execute(
                select(ShadowPrediction.generated_at)
                .where(ShadowPrediction.shadow_model_id == shadow_handle.id)
                .order_by(ShadowPrediction.generated_at.asc())
                .limit(1)
            ).scalar_one_or_none()

        shadow_mape = (
            float(sum(float(r.abs_error_pct) for r in rows) / len(rows))
            if rows
            else 0.0
        )
        trading_sessions = _count_trading_sessions_since(earliest)

        shadow_bundle = pb2.MetricsBundle(
            mape=shadow_mape, sample_size=len(rows)
        )
        prod_bundle = pb2.MetricsBundle(
            mape=float(prod_handle.current_mape or 0.0)
            if prod_handle is not None
            else 0.0,
            sample_size=0,
        )
        resp = pb2.ShadowReportResponse(
            shadow=_to_handle_proto(pb2, shadow_handle),
            shadow_metrics=shadow_bundle,
            production_metrics=prod_bundle,
            trading_sessions_elapsed=int(trading_sessions),
        )
        if prod_handle is not None:
            resp.production.CopyFrom(_to_handle_proto(pb2, prod_handle))
        return resp


def _count_trading_sessions_since(since: Any) -> int:
    if since is None:
        return 0
    from datetime import datetime as _dt

    end = _dt.utcnow()
    sessions = 0
    day = since.date() if hasattr(since, "date") else since
    while day <= end.date():
        if day.weekday() < 5:
            sessions += 1
        day += __import__("datetime").timedelta(days=1)
    return sessions
