"""Canonical MOEX timeframes.

Enum values match the ``archive/<TIMEFRAME>/`` layout and the
``Timeframe`` enum in finzooka.ml.v1.ml_forecast (proto). The two
worlds are kept in sync via :func:`from_proto` / :func:`to_proto`
so no string parsing leaks into domain code.
"""

from __future__ import annotations

from enum import Enum


class Timeframe(str, Enum):
    MN1 = "MN1"
    W1 = "W1"
    D1 = "D1"
    H4 = "H4"
    H1 = "H1"
    M30 = "M30"
    M15 = "M15"
    M10 = "M10"
    M5 = "M5"

    @property
    def seconds(self) -> int:
        return _SECONDS[self]


_SECONDS: dict[Timeframe, int] = {
    Timeframe.M5: 5 * 60,
    Timeframe.M10: 10 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.M30: 30 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
    # Week is fixed to 7 calendar days, month to 30 for comparison purposes.
    # Real calendar-aware math happens at bar-boundary level via pandas.
    Timeframe.W1: 7 * 24 * 60 * 60,
    Timeframe.MN1: 30 * 24 * 60 * 60,
}


# Proto <-> domain helpers (imports deferred to avoid circular deps with grpc_gen).
def from_proto(value: int) -> Timeframe:
    """Convert proto Timeframe integer tag to domain enum."""

    return _PROTO_TO_DOMAIN[value]


def to_proto(tf: Timeframe) -> int:
    """Return proto Timeframe integer tag for domain enum."""

    return _DOMAIN_TO_PROTO[tf]


# Tags mirror enum values in proto/finzooka/ml/v1/ml_forecast.proto.
_PROTO_TO_DOMAIN: dict[int, Timeframe] = {
    1: Timeframe.MN1,
    2: Timeframe.W1,
    3: Timeframe.D1,
    4: Timeframe.H4,
    5: Timeframe.H1,
    6: Timeframe.M30,
    7: Timeframe.M15,
    8: Timeframe.M10,
    9: Timeframe.M5,
}
_DOMAIN_TO_PROTO: dict[Timeframe, int] = {v: k for k, v in _PROTO_TO_DOMAIN.items()}
