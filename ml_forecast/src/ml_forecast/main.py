"""gRPC server bootstrap for ml-forecast.

Phase-2 scaffolding: health RPC is wired in and the server listens, but
the MlForecast servicer itself is a placeholder — US-1..US-5 fill in
Forecast/Train/Backtest/Admin RPCs in subsequent phases.

Call as::

    python -m ml_forecast.main
"""

from __future__ import annotations

import logging
import signal
from concurrent import futures
from types import FrameType
from typing import Optional

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from ml_forecast.api.health import HealthServicer
from ml_forecast.config import get_settings

log = logging.getLogger("ml_forecast.main")


def _build_server() -> grpc.Server:
    settings = get_settings()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.grpc_max_workers),
        options=[("grpc.so_reuseport", 0)],
    )
    # Health is always available, even when the domain servicer is not yet wired.
    health_pb2_grpc.add_HealthServicer_to_server(HealthServicer(), server)
    return server


def serve() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    server = _build_server()
    listen_addr = f"[::]:{settings.grpc_port}"
    server.add_insecure_port(listen_addr)
    server.start()
    log.info("gRPC server listening on %s", listen_addr)

    stopped = False

    def _stop(signum: int, _frame: Optional[FrameType]) -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        log.info("received signal %s — shutting down", signum)
        server.stop(grace=15)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    server.wait_for_termination()


if __name__ == "__main__":
    serve()


# Convenience helper for the contract test (does not start its own thread).
def build_server_for_test() -> grpc.Server:
    """Construct a server identical to :func:`serve` but without signalling.

    The test fixture calls :meth:`grpc.Server.add_insecure_port` with an
    ephemeral port and drives shutdown itself.
    """

    return _build_server()


_ = health_pb2  # make linter see the explicit dependency
