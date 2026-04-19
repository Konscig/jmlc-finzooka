"""Thin Redis reader for live OHLCV + sentiment feeds.

Contract (research.md R2):

- ``ohlcv:<ticker>:<timeframe>:last``      — JSON blob with recent bars
- ``ohlcv:<ticker>:<timeframe>:last:ts``   — ISO-8601 timestamp of the
                                             newest bar in the blob
- ``sentiment:<ticker>:agg``               — JSON aggregate
- ``sentiment:<ticker>:agg:ts``            — ISO-8601 timestamp of the
                                             aggregate computation

This service is a READ-ONLY consumer. Data Collector and
Sentiment Pipeline own these keys (see C4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

import redis
from dateutil.parser import isoparse

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe


class RedisKeyMissing(RuntimeError):
    """Raised when a required key is absent in Redis (e.g. Data Collector behind)."""


@dataclass(frozen=True, slots=True)
class OhlcvSnapshot:
    bars: list[dict[str, Any]]
    last_ts: datetime
    source: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class SentimentSnapshot:
    score: float
    confidence: float
    sources: list[str]
    window_seconds: int
    last_ts: datetime
    schema_version: int


def _ohlcv_key(ticker: str, timeframe: Timeframe) -> str:
    return f"ohlcv:{ticker}:{timeframe.value}:last"


def _sentiment_key(ticker: str) -> str:
    return f"sentiment:{ticker}:agg"


@lru_cache(maxsize=1)
def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def ping() -> bool:
    try:
        return bool(_client().ping())
    except redis.RedisError:
        return False


def get_ohlcv_last(ticker: str, timeframe: Timeframe) -> OhlcvSnapshot:
    c = _client()
    key = _ohlcv_key(ticker, timeframe)
    blob = c.get(key)
    ts = c.get(f"{key}:ts")
    if blob is None or ts is None:
        raise RedisKeyMissing(f"OHLCV key missing for {ticker}/{timeframe.value}")
    payload = json.loads(blob)
    return OhlcvSnapshot(
        bars=payload["bars"],
        last_ts=isoparse(ts),
        source=payload.get("source", "unknown"),
        schema_version=int(payload.get("schema_version", 1)),
    )


def get_sentiment_agg(ticker: str) -> SentimentSnapshot:
    c = _client()
    key = _sentiment_key(ticker)
    blob = c.get(key)
    ts = c.get(f"{key}:ts")
    if blob is None or ts is None:
        raise RedisKeyMissing(f"sentiment key missing for {ticker}")
    payload = json.loads(blob)
    return SentimentSnapshot(
        score=float(payload["score"]),
        confidence=float(payload.get("confidence", 0.0)),
        sources=list(payload.get("sources", [])),
        window_seconds=int(payload.get("window_seconds", 3600)),
        last_ts=isoparse(ts),
        schema_version=int(payload.get("schema_version", 1)),
    )
