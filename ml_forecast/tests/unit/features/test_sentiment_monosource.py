"""T023 multi-source guard — closes remediation finding C3."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ml_forecast.domain.factor import FactorStatus
from ml_forecast.features.sentiment_features import sentiment_score
from ml_forecast.storage.redis_client import (
    RedisKeyMissing,
    SentimentSnapshot,
)


def _snap(sources: list[str]) -> SentimentSnapshot:
    return SentimentSnapshot(
        score=0.42,
        confidence=0.8,
        sources=sources,
        window_seconds=3600,
        last_ts=datetime.now(timezone.utc),
        schema_version=1,
    )


def test_returns_unavailable_when_key_missing() -> None:
    with patch(
        "ml_forecast.features.sentiment_features.redis_client.get_sentiment_agg",
        side_effect=RedisKeyMissing,
    ):
        feat = sentiment_score("SBER")
    assert feat.value is None
    assert feat.status is FactorStatus.UNAVAILABLE


def test_rejects_single_source_even_when_fresh() -> None:
    with patch(
        "ml_forecast.features.sentiment_features.redis_client.get_sentiment_agg",
        return_value=_snap(["tpulse"]),
    ):
        feat = sentiment_score("SBER")
    assert feat.status is FactorStatus.UNAVAILABLE
    assert feat.value is None, "monosource sentiment must NOT be trusted"


def test_accepts_multi_source() -> None:
    with patch(
        "ml_forecast.features.sentiment_features.redis_client.get_sentiment_agg",
        return_value=_snap(["tpulse", "news_headlines"]),
    ):
        feat = sentiment_score("SBER")
    assert feat.status is FactorStatus.OK
    assert feat.value is not None
    assert feat.value == pytest.approx(0.42)
