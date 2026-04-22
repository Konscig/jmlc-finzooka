"""Forecast value objects — immutable snapshots returned by the model layer.

Named tuples are avoided on purpose: Decimal fields need explicit typing,
and frozen dataclasses play nicely with mypy strict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from ml_forecast.domain.factor import FactorContribution, SourceAvailability
from ml_forecast.domain.timeframe import Timeframe


class ForecastStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One future bar the model predicts."""

    t: datetime
    mean: Decimal
    lo: Decimal
    hi: Decimal


@dataclass(frozen=True, slots=True)
class AdvisoryFlags:
    """Caller-visible flags that do not alter `predicted_path` values.

    See spec.md Edge Cases and contracts/ml_forecast.proto fields 12–14.
    """

    model_stale: bool = False
    outside_trading_hours: bool = False
    anomalous_last_bar: bool = False


@dataclass(frozen=True, slots=True)
class Forecast:
    request_id: UUID
    ticker: str
    timeframe: Timeframe
    horizon: int
    predicted_path: tuple[PricePoint, ...]
    suggested_stop_loss: Decimal
    suggested_take_profit: Decimal
    factors: tuple[FactorContribution, ...]
    explanation: str
    mape_at_generation: Decimal | None
    model_version: str
    generated_at: datetime
    source_availability: SourceAvailability
    status: ForecastStatus = ForecastStatus.OK
    advisory: AdvisoryFlags = field(default_factory=AdvisoryFlags)
