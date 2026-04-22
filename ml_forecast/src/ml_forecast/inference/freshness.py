"""Data-freshness guards for live inference (FR-002b).

Rules:

- OHLCV: age of the last bar must be ≤ ``ohlcv_max_age_multiplier ×
  timeframe.seconds`` (default multiplier 1.0). Stale OHLCV is a hard
  reject (gRPC FAILED_PRECONDITION, detail ``stale_ohlcv``).

- Sentiment: age of the aggregate must be ≤
  ``sentiment_max_age_seconds`` (default 3600). Stale sentiment causes
  a soft fallback — ``FactorStatus.STALE`` / ``SourceFreshness.STALE``
  and ``ForecastStatus.DEGRADED``, but the inference still proceeds on
  OHLCV-only features.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ml_forecast.config import get_settings
from ml_forecast.domain.factor import SourceFreshness
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.storage import redis_client


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_ohlcv(
    ticker: str, timeframe: Timeframe, now: datetime | None = None
) -> SourceFreshness:
    """Return FRESH / STALE / UNAVAILABLE for the OHLCV key."""

    now = _as_utc(now or _now_utc())
    try:
        snap = redis_client.get_ohlcv_last(ticker, timeframe)
    except redis_client.RedisKeyMissing:
        return SourceFreshness.UNAVAILABLE

    settings = get_settings()
    max_age = settings.ohlcv_max_age_multiplier * float(timeframe.seconds)
    age = (now - _as_utc(snap.last_ts)).total_seconds()
    if age > max_age:
        return SourceFreshness.STALE
    return SourceFreshness.FRESH


def check_sentiment(ticker: str, now: datetime | None = None) -> SourceFreshness:
    """Return FRESH / STALE / UNAVAILABLE for the sentiment aggregate."""

    now = _as_utc(now or _now_utc())
    try:
        snap = redis_client.get_sentiment_agg(ticker)
    except redis_client.RedisKeyMissing:
        return SourceFreshness.UNAVAILABLE

    settings = get_settings()
    age = (now - _as_utc(snap.last_ts)).total_seconds()
    if age > settings.sentiment_max_age_seconds:
        return SourceFreshness.STALE
    return SourceFreshness.FRESH
