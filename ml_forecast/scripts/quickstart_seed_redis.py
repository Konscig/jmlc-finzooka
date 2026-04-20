"""Seed Redis with the last N OHLCV bars + a stub sentiment aggregate.

Only for local smoke-tests — in production the real Data Collector and
Sentiment Pipeline own these keys. Usage from inside the ml container::

    python -m scripts.quickstart_seed_redis SBER D1 --bars 600

The script works out of the box against ``ml_forecast/docker-compose.yaml``
(``ML_REDIS_URL`` and ``ML_ARCHIVE_DIR`` come in via env vars).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.features.validators import validate_ohlcv_csv


def _seed_ohlcv(redis_client, ticker: str, timeframe: Timeframe, bars: int) -> None:
    csv = get_settings().archive_dir / timeframe.value / f"{ticker}_{timeframe.value}.csv"
    if not csv.exists():
        sys.exit(f"missing archive CSV: {csv}")
    df = validate_ohlcv_csv(csv).tail(bars).reset_index()
    payload = {
        "bars": [
            {
                "dt": row["datetime"].isoformat(),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]),
            }
            for _, row in df.iterrows()
        ],
        "source": "quickstart_seed",
        "schema_version": 1,
    }
    last_ts = df["datetime"].iloc[-1]
    last_iso = pd.Timestamp(last_ts).tz_localize("UTC").isoformat() \
        if pd.Timestamp(last_ts).tzinfo is None \
        else pd.Timestamp(last_ts).isoformat()

    key = f"ohlcv:{ticker}:{timeframe.value}:last"
    redis_client.set(key, json.dumps(payload))
    # `:ts` carries the freshness signal that the service reads without
    # parsing the full blob.
    redis_client.set(f"{key}:ts", last_iso)
    print(f"seeded {len(payload['bars'])} bars → {key} (last_ts={last_iso})")


def _seed_sentiment(redis_client, ticker: str) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "score": 0.0,
        "confidence": 0.5,
        "sources": ["tpulse", "news_headlines"],  # ≥ 2 for Principle III
        "window_seconds": 3600,
        "schema_version": 1,
    }
    key = f"sentiment:{ticker}:agg"
    redis_client.set(key, json.dumps(payload))
    redis_client.set(f"{key}:ts", now)
    print(f"seeded sentiment → {key} (last_ts={now})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Redis for ml-forecast quickstart")
    parser.add_argument("ticker", help="e.g. SBER")
    parser.add_argument("timeframe", help="one of D1 / M15 / M5 / ...")
    parser.add_argument("--bars", type=int, default=600, help="how many recent bars to push")
    args = parser.parse_args()

    timeframe = Timeframe(args.timeframe.upper())
    import redis
    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    _seed_ohlcv(client, args.ticker.upper(), timeframe, args.bars)
    _seed_sentiment(client, args.ticker.upper())
    print("done.")


if __name__ == "__main__":
    main()
