"""Celery application + beat schedule (T055, T079).

One app instance for the whole service — the ``ml-worker`` container
points to this module (``celery -A ml_forecast.training.scheduler``).

Beat schedule:

- ``retrain_all_production`` runs every Sunday 02:00 MSK (research R9 /
  spec clarification). Each production model gets enqueued into
  ``train_queue`` with concurrency=1 so 4 CPU / 12 GB RAM budget is
  respected.
- ``recompute_daily_metrics`` runs every morning at 04:30 MSK to refresh
  the ``current_mape`` gauge used by ForecastResponse (T076).
- ``health_check`` pings backing stores every minute (T077, FR-017).
- ``shadow_resolve`` fills in actuals for resolved shadow predictions
  every 15 minutes (T059).
- ``artifact_rotation`` prunes old archived artifacts nightly (T094
  retention integration).
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab

from ml_forecast.config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()

celery_app = Celery(
    "ml_forecast",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
)

# Don't let a rogue task lock a worker forever — matches SC-005 train budget.
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Moscow",
    enable_utc=False,
    task_time_limit=_settings.train_max_duration_seconds * 3,
    task_soft_time_limit=_settings.train_max_duration_seconds * 2,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_default_queue="train_queue",
)


# ---------------------------------------------------------------------------
# Tasks — thin wrappers around the sync implementations.
# ---------------------------------------------------------------------------

@celery_app.task(name="ml_forecast.train_pair", bind=True, queue="train_queue")
def train_pair(self, ticker: str, timeframe: str, trigger: str = "scheduled") -> dict:
    """Celery wrapper around :func:`training.pipeline.run`."""

    from ml_forecast.domain.timeframe import Timeframe
    from ml_forecast.training import pipeline

    tf = Timeframe(timeframe)
    log.info("celery train_pair start: %s/%s trigger=%s", ticker, tf.value, trigger)
    result = pipeline.run(ticker=ticker, timeframe=tf, trigger=trigger)
    return {
        "training_run_id": result.training_run_id,
        "model_version": result.model_version,
        "promote_decision": result.promote_decision,
        "aggregate_mape": result.aggregate_metrics.get("mape"),
    }


@celery_app.task(name="ml_forecast.retrain_all_production")
def retrain_all_production() -> dict:
    """Enqueue a train_pair for every production model (beat, weekly)."""

    from ml_forecast.domain.model_state import ModelState
    from ml_forecast.inference import registry

    handles = registry.list_models(state=ModelState.PRODUCTION)
    enqueued: list[dict[str, str]] = []
    for handle in handles:
        train_pair.delay(handle.ticker, handle.timeframe.value, "scheduled")
        enqueued.append(
            {"ticker": handle.ticker, "timeframe": handle.timeframe.value}
        )
    log.info("retrain_all_production enqueued %d jobs", len(enqueued))
    return {"enqueued": enqueued, "count": len(enqueued)}


@celery_app.task(name="ml_forecast.recompute_daily_metrics")
def recompute_daily_metrics() -> dict:
    """T076 — refresh current_mape + model_metrics for every production model."""

    from ml_forecast.observability.jobs import recompute_daily_metrics as _run

    return _run()


@celery_app.task(name="ml_forecast.health_check")
def health_check() -> dict:
    """T077 — probe Postgres + Redis; UPDATE public.pipeline_statuses."""

    from ml_forecast.observability.jobs import health_check as _run

    return _run()


@celery_app.task(name="ml_forecast.shadow_predict")
def shadow_predict(
    request_id: str, ticker: str, timeframe: str, horizon: int
) -> dict:
    """T058 — run every shadow model in parallel with the production response."""

    from ml_forecast.domain.timeframe import Timeframe
    from ml_forecast.inference import shadow

    tf = Timeframe(timeframe)
    return shadow.run_shadow_predictions(
        request_id=request_id, ticker=ticker, timeframe=tf, horizon=horizon
    )


@celery_app.task(name="ml_forecast.shadow_resolve")
def shadow_resolve() -> dict:
    """T059 — fill in actuals for shadow_prediction rows whose horizon elapsed."""

    from ml_forecast.inference import shadow

    return shadow.resolve_matured_predictions()


@celery_app.task(name="ml_forecast.artifact_rotation")
def artifact_rotation() -> dict:
    """T094 — keep the 3 most-recent archived artifacts per (ticker, timeframe)."""

    from ml_forecast.domain.model_state import ModelState
    from ml_forecast.inference import registry
    from ml_forecast.storage import artifact_store

    removed: list[str] = []
    for handle in registry.list_models(state=ModelState.ARCHIVED):
        deleted = artifact_store.rotate_archive(handle.ticker, handle.timeframe, keep=3)
        removed.extend(str(p) for p in deleted)
    return {"removed": removed, "count": len(removed)}


# ---------------------------------------------------------------------------
# Beat schedule
# ---------------------------------------------------------------------------

celery_app.conf.beat_schedule = {
    # Sunday 02:00 МСК — weekly retrain; half the model_stale_days window.
    "retrain-all-production-weekly": {
        "task": "ml_forecast.retrain_all_production",
        "schedule": crontab(hour=2, minute=0, day_of_week="sun"),
    },
    # Every day at 04:30 МСК — daily metric recompute.
    "recompute-daily-metrics": {
        "task": "ml_forecast.recompute_daily_metrics",
        "schedule": crontab(hour=4, minute=30),
    },
    # Every minute — backend probe for FR-017.
    "health-check-minutely": {
        "task": "ml_forecast.health_check",
        "schedule": crontab(minute="*"),
    },
    # Every 15 minutes — shadow resolve.
    "shadow-resolve-15min": {
        "task": "ml_forecast.shadow_resolve",
        "schedule": crontab(minute="*/15"),
    },
    # Nightly 03:30 МСК — rotate archived artifacts.
    "artifact-rotation-nightly": {
        "task": "ml_forecast.artifact_rotation",
        "schedule": crontab(hour=3, minute=30),
    },
}
