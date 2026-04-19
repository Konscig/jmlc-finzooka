"""Factor-contribution + source-availability value objects.

Serialised to proto `FactorContribution` / `SourceAvailability` at the
gRPC boundary and into ``inference_log.factors`` JSONB for observability.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal


class FactorStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class SourceFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


FactorSource = Literal["ohlcv", "sentiment", "technical"]


@dataclass(frozen=True, slots=True)
class FactorContribution:
    """One line-item in the `factors[]` array of a forecast."""

    name: str
    value: Decimal | None
    contribution: Decimal
    source: FactorSource
    status: FactorStatus

    @property
    def is_significant(self) -> bool:
        """True if contribution magnitude clears the render threshold.

        The actual threshold value lives in :mod:`ml_forecast.inference.explain`
        (``EXPLAIN_MIN_CONTRIBUTION``) — this method just checks non-zero.
        """

        return self.contribution != Decimal(0)


@dataclass(frozen=True, slots=True)
class SourceAvailability:
    ohlcv: SourceFreshness
    sentiment: SourceFreshness


# Proto <-> domain helpers.
def factor_status_from_proto(value: int) -> FactorStatus:
    return _FS_P2D[value]


def factor_status_to_proto(s: FactorStatus) -> int:
    return _FS_D2P[s]


def source_freshness_from_proto(value: int) -> SourceFreshness:
    return _SF_P2D[value]


def source_freshness_to_proto(s: SourceFreshness) -> int:
    return _SF_D2P[s]


_FS_P2D: dict[int, FactorStatus] = {
    1: FactorStatus.OK,
    2: FactorStatus.STALE,
    3: FactorStatus.UNAVAILABLE,
}
_FS_D2P: dict[FactorStatus, int] = {v: k for k, v in _FS_P2D.items()}
_SF_P2D: dict[int, SourceFreshness] = {
    1: SourceFreshness.FRESH,
    2: SourceFreshness.STALE,
    3: SourceFreshness.UNAVAILABLE,
}
_SF_D2P: dict[SourceFreshness, int] = {v: k for k, v in _SF_P2D.items()}
