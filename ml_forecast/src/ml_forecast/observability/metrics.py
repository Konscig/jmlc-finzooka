"""Prometheus exporter (T074).

Single process-wide registry — every module that wants to instrument
something imports the concrete metric object from here rather than
re-registering. That avoids duplicate-metric errors when pytest
imports modules multiple times.

Metric families align with the alert rules in
``config/alerts/prometheus_rules.yaml`` (T078).
"""

from __future__ import annotations

import logging
import threading

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from ml_forecast.config import get_settings

log = logging.getLogger(__name__)


REGISTRY = CollectorRegistry()


forecast_latency_seconds = Histogram(
    "ml_forecast_latency_seconds",
    "Forecast RPC latency in seconds, labelled by outcome.",
    labelnames=("status",),
    # Buckets chosen with an eye on SC-004 targets: avg ≤ 15s, p95 ≤ 30s, timeout 60s.
    buckets=(0.1, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60),
    registry=REGISTRY,
)

forecast_total = Counter(
    "ml_forecast_forecast_total",
    "Total number of Forecast RPC calls, labelled by status.",
    labelnames=("status",),
    registry=REGISTRY,
)

train_duration_seconds = Histogram(
    "ml_forecast_train_duration_seconds",
    "Training duration in seconds, labelled by family + promote decision.",
    labelnames=("family", "decision"),
    buckets=(30, 60, 120, 300, 600, 900, 1800),
    registry=REGISTRY,
)

stale_rate = Gauge(
    "ml_forecast_stale_rate",
    "Share of forecast requests with a stale input source over the last window.",
    labelnames=("source",),  # ohlcv | sentiment
    registry=REGISTRY,
)

current_mape = Gauge(
    "ml_forecast_current_mape",
    "Per-(ticker,timeframe) MAPE of the current production model.",
    labelnames=("ticker", "timeframe"),
    registry=REGISTRY,
)

health_probe = Gauge(
    "ml_forecast_health_probe",
    "1 if backing stores are reachable, 0 otherwise, labelled by component.",
    labelnames=("component",),  # postgres | redis
    registry=REGISTRY,
)


_server_lock = threading.Lock()
_server_started = False


def start_exporter_once() -> None:
    """Idempotent Prometheus HTTP exporter bootstrap."""

    global _server_started
    with _server_lock:
        if _server_started:
            return
        port = get_settings().metrics_port
        start_http_server(port, registry=REGISTRY)
        _server_started = True
        log.info("prometheus exporter listening on :%d/metrics", port)
