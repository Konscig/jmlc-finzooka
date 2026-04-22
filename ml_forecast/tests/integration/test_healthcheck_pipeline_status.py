"""T073 — health_check job updates public.pipeline_statuses."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from sqlalchemy import text

from ml_forecast.observability.jobs import health_check
from ml_forecast.storage.postgres import session_scope

pytestmark = pytest.mark.skipif(
    os.environ.get("ML_DATABASE_URL") is None,
    reason="integration tests require ML_DATABASE_URL",
)


def test_healthy_status_persisted() -> None:
    result = health_check()
    assert result["status"] in {"healthy", "unhealthy"}  # depends on redis

    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT status, last_success_at FROM public.pipeline_statuses "
                "WHERE pipeline_name = 'ml_forecast'"
            )
        ).first()
    assert row is not None
    assert row.status in {"healthy", "unhealthy"}


def test_unhealthy_when_redis_down() -> None:
    with patch(
        "ml_forecast.observability.jobs.redis_client.ping", return_value=False
    ):
        result = health_check()
    assert result["redis_ok"] is False
    assert result["status"] == "unhealthy"

    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT status, last_error FROM public.pipeline_statuses "
                "WHERE pipeline_name = 'ml_forecast'"
            )
        ).first()
    assert row.status == "unhealthy"
    assert row.last_error is not None and "redis" in row.last_error.lower()
