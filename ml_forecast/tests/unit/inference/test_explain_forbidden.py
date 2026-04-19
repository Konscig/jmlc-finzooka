"""T029 — forbidden-phrases filter coverage (FR-019, fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ml_forecast.config import get_settings
from ml_forecast.inference.explain import (
    ExplanationForbiddenPhrase,
    check_forbidden,
    reload_patterns,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reload_patterns()
    get_settings.cache_clear()


def _load_shipped_patterns() -> list[str]:
    path = Path(__file__).resolve().parents[4] / "config" / "forbidden_phrases.yaml"
    raw = yaml.safe_load(path.read_text())
    return [p for p in raw["patterns"] if p]


@pytest.mark.parametrize("phrase", _load_shipped_patterns())
def test_every_shipped_phrase_blocks(phrase: str) -> None:
    sentence = f"Прогноз: {phrase} для SBER по D1."
    with pytest.raises(ExplanationForbiddenPhrase):
        check_forbidden(sentence)


@pytest.mark.parametrize("phrase", _load_shipped_patterns())
def test_case_insensitive_match(phrase: str) -> None:
    sentence = f"{phrase.upper()} prefix suffix"
    with pytest.raises(ExplanationForbiddenPhrase):
        check_forbidden(sentence)


def test_clean_text_passes() -> None:
    # Should not raise.
    check_forbidden(
        "Рост на 0.3% в течение 1 D1-бара. "
        "Основные факторы: rsi_14, atr_14, sentiment_score. "
        "Текущая MAPE модели SBER/D1: 2.10%."
    )


def test_empty_text_passes() -> None:
    check_forbidden("")
