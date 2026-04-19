"""Model artifact lifecycle states and allowed transitions.

Enforced at two levels:

1. Application layer — this module's :data:`ALLOWED_TRANSITIONS` is
   consulted before every state mutation; illegal transitions raise
   :class:`IllegalStateTransition`.
2. Database layer — a partial unique index on ``model_registry``
   guarantees exactly one ``production`` row per (ticker, timeframe).

See data-model.md §1.1 for the state diagram.
"""

from __future__ import annotations

from enum import Enum


class ModelState(str, Enum):
    TRAINING = "training"
    SHADOW = "shadow"
    PRODUCTION = "production"
    ARCHIVED = "archived"
    DO_NOT_PROMOTE = "do_not_promote"


class IllegalStateTransition(RuntimeError):
    """Raised when a caller attempts a transition outside the allowed graph."""


# Terminal states (ARCHIVED) are absent as keys — no onward moves permitted.
ALLOWED_TRANSITIONS: dict[ModelState, frozenset[ModelState]] = {
    ModelState.TRAINING: frozenset(
        {ModelState.SHADOW, ModelState.DO_NOT_PROMOTE}
    ),
    ModelState.SHADOW: frozenset(
        {ModelState.PRODUCTION, ModelState.ARCHIVED}
    ),
    ModelState.PRODUCTION: frozenset({ModelState.ARCHIVED}),
    # do_not_promote → {shadow, archived} only via explicit admin action;
    # left unlinked here to force the caller to go through an admin RPC.
    ModelState.DO_NOT_PROMOTE: frozenset(
        {ModelState.SHADOW, ModelState.ARCHIVED}
    ),
}


def assert_transition(src: ModelState, dst: ModelState) -> None:
    """Validate that ``src → dst`` is allowed; raise otherwise."""

    allowed = ALLOWED_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise IllegalStateTransition(
            f"illegal model transition: {src.value} -> {dst.value}"
        )


def is_terminal(state: ModelState) -> bool:
    return state is ModelState.ARCHIVED


# Proto <-> domain helpers.
def from_proto(value: int) -> ModelState:
    return _PROTO_TO_DOMAIN[value]


def to_proto(state: ModelState) -> int:
    return _DOMAIN_TO_PROTO[state]


_PROTO_TO_DOMAIN: dict[int, ModelState] = {
    1: ModelState.TRAINING,
    2: ModelState.SHADOW,
    3: ModelState.PRODUCTION,
    4: ModelState.ARCHIVED,
    5: ModelState.DO_NOT_PROMOTE,
}
_DOMAIN_TO_PROTO: dict[ModelState, int] = {v: k for k, v in _PROTO_TO_DOMAIN.items()}
