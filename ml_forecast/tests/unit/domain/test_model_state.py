"""T012 unit tests: ModelState transition rules match data-model.md §1.1."""

from __future__ import annotations

import pytest

from ml_forecast.domain.model_state import (
    ALLOWED_TRANSITIONS,
    IllegalStateTransition,
    ModelState,
    assert_transition,
    is_terminal,
)


def test_training_can_move_to_shadow_or_do_not_promote() -> None:
    assert_transition(ModelState.TRAINING, ModelState.SHADOW)
    assert_transition(ModelState.TRAINING, ModelState.DO_NOT_PROMOTE)


def test_shadow_can_move_to_production_or_archived() -> None:
    assert_transition(ModelState.SHADOW, ModelState.PRODUCTION)
    assert_transition(ModelState.SHADOW, ModelState.ARCHIVED)


def test_production_can_only_move_to_archived() -> None:
    assert_transition(ModelState.PRODUCTION, ModelState.ARCHIVED)
    with pytest.raises(IllegalStateTransition):
        assert_transition(ModelState.PRODUCTION, ModelState.SHADOW)


def test_archived_is_terminal() -> None:
    assert is_terminal(ModelState.ARCHIVED)
    # Archived has no allowed onward transitions.
    assert ALLOWED_TRANSITIONS.get(ModelState.ARCHIVED, frozenset()) == frozenset()


def test_do_not_promote_can_recover_to_shadow_or_be_archived() -> None:
    assert_transition(ModelState.DO_NOT_PROMOTE, ModelState.SHADOW)
    assert_transition(ModelState.DO_NOT_PROMOTE, ModelState.ARCHIVED)
    with pytest.raises(IllegalStateTransition):
        assert_transition(ModelState.DO_NOT_PROMOTE, ModelState.PRODUCTION)


def test_training_cannot_skip_to_production() -> None:
    with pytest.raises(IllegalStateTransition):
        assert_transition(ModelState.TRAINING, ModelState.PRODUCTION)
