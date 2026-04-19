"""Sentiment feature — #15 of research R5.

The ML service is a READ consumer of the shared Redis keys owned by
Sentiment Pipeline. The single-source guard (spec FR-014 / Principle III)
rejects sentiment whose ``sources[]`` contains fewer than 2 contributors,
even if the key is fresh — we prefer a known UNAVAILABLE fallback over a
monosource signal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from ml_forecast.domain.factor import FactorStatus
from ml_forecast.storage import redis_client

_MIN_SOURCES: int = 2


class SentimentFeature(NamedTuple):
    value: Decimal | None
    status: FactorStatus


def sentiment_score(ticker: str) -> SentimentFeature:
    """Fetch sentiment aggregate from Redis; apply multi-source guard."""

    try:
        snap = redis_client.get_sentiment_agg(ticker)
    except redis_client.RedisKeyMissing:
        return SentimentFeature(value=None, status=FactorStatus.UNAVAILABLE)

    if len(snap.sources) < _MIN_SOURCES:
        # Guard per FR-014 + Principle III: monosource sentiment is not trusted.
        return SentimentFeature(value=None, status=FactorStatus.UNAVAILABLE)

    return SentimentFeature(
        value=Decimal(str(snap.score)), status=FactorStatus.OK
    )
