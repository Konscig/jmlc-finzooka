"""T033 — health RPC contract test.

Spins a real gRPC server on an ephemeral port and probes the standard
``grpc.health.v1.Health/Check`` endpoint. Postgres and Redis are
monkey-patched so the test does not require live backends.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from ml_forecast.main import build_server_for_test


@pytest.fixture
def server_port() -> Generator[int, None, None]:
    # Postgres + Redis reachable.
    with patch(
        "ml_forecast.api.health.HealthServicer._postgres_ok", return_value=True
    ), patch("ml_forecast.api.health.redis_client.ping", return_value=True):
        server = build_server_for_test()
        port = server.add_insecure_port("localhost:0")
        server.start()
        try:
            yield port
        finally:
            server.stop(0)


def test_health_serving_when_backends_up(server_port: int) -> None:
    with grpc.insecure_channel(f"localhost:{server_port}") as channel:
        client = health_pb2_grpc.HealthStub(channel)
        resp = client.Check(health_pb2.HealthCheckRequest(service=""))
    assert resp.status == health_pb2.HealthCheckResponse.SERVING


def test_health_not_serving_when_redis_down() -> None:
    with patch(
        "ml_forecast.api.health.HealthServicer._postgres_ok", return_value=True
    ), patch("ml_forecast.api.health.redis_client.ping", return_value=False):
        server = build_server_for_test()
        port = server.add_insecure_port("localhost:0")
        server.start()
        try:
            with grpc.insecure_channel(f"localhost:{port}") as channel:
                client = health_pb2_grpc.HealthStub(channel)
                resp = client.Check(health_pb2.HealthCheckRequest(service=""))
            assert resp.status == health_pb2.HealthCheckResponse.NOT_SERVING
        finally:
            server.stop(0)


def test_health_not_serving_when_postgres_down() -> None:
    with patch(
        "ml_forecast.api.health.HealthServicer._postgres_ok", return_value=False
    ), patch("ml_forecast.api.health.redis_client.ping", return_value=True):
        server = build_server_for_test()
        port = server.add_insecure_port("localhost:0")
        server.start()
        try:
            with grpc.insecure_channel(f"localhost:{port}") as channel:
                client = health_pb2_grpc.HealthStub(channel)
                resp = client.Check(health_pb2.HealthCheckRequest(service=""))
            assert resp.status == health_pb2.HealthCheckResponse.NOT_SERVING
        finally:
            server.stop(0)
