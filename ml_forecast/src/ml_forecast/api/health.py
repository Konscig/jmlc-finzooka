"""gRPC health check per grpc.health.v1 with backend probes.

We report SERVING only when **both** Redis and Postgres are reachable —
anything less would mask degradations that Principle V demands be
alerted on. The ``ml-worker`` container (Celery) has its own
ping/health mechanism and is out of scope here.
"""

from __future__ import annotations

import logging
from grpc_health.v1 import health_pb2, health_pb2_grpc

from ml_forecast.storage import redis_client
from ml_forecast.storage.postgres import get_engine

log = logging.getLogger(__name__)


class HealthServicer(health_pb2_grpc.HealthServicer):
    """Answer Health/Check based on backing-store reachability."""

    def Check(
        self, request: health_pb2.HealthCheckRequest, context: object
    ) -> health_pb2.HealthCheckResponse:
        if not self._postgres_ok():
            log.warning("health: postgres probe failed")
            return health_pb2.HealthCheckResponse(
                status=health_pb2.HealthCheckResponse.NOT_SERVING
            )
        if not redis_client.ping():
            log.warning("health: redis probe failed")
            return health_pb2.HealthCheckResponse(
                status=health_pb2.HealthCheckResponse.NOT_SERVING
            )
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING
        )

    def Watch(
        self, request: health_pb2.HealthCheckRequest, context: object
    ):  # noqa: ANN201
        # MVP: return one Check result and close the stream.
        yield self.Check(request, context)

    @staticmethod
    def _postgres_ok() -> bool:
        try:
            from sqlalchemy import text as _text

            engine = get_engine()
            with engine.connect() as conn:
                conn.execute(_text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 — health code swallows errors intentionally
            log.warning("postgres probe error: %s", exc)
            return False
