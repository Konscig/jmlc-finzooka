"""Deterministic explanation renderer + forbidden-phrases filter.

The renderer (T027) builds a 2-3 sentence human-readable string from
a :class:`Forecast` dataclass and (optionally) model metrics. The
filter (T028) scans the rendered text against a YAML-loaded blocklist
and raises :class:`ExplanationForbiddenPhrase` when any pattern matches —
fail-closed per research.md R10 and Principle VI.

Contribution significance (A1 remediation): a factor counts as
"significant" when ``abs(contribution) > EXPLAIN_MIN_CONTRIBUTION``
AND ``status != UNAVAILABLE``. We demand at least three such factors
or raise :class:`Unexplainable` (SC-006).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ml_forecast.config import get_settings
from ml_forecast.domain.factor import FactorContribution, FactorStatus
from ml_forecast.domain.forecast import Forecast
from ml_forecast.domain.timeframe import Timeframe

EXPLAIN_MIN_CONTRIBUTION: Decimal = Decimal("0.01")
MIN_SIGNIFICANT_FACTORS: int = 3


class Unexplainable(RuntimeError):
    """The forecast lacks enough significant factors to explain (SC-006)."""


class ExplanationForbiddenPhrase(RuntimeError):
    """Rendered explanation matched a forbidden phrase (FR-019)."""


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Extra data not carried in Forecast itself."""

    current_mape: Decimal | None
    last_close: Decimal | None


def _bar_unit_ru(tf: Timeframe) -> str:
    return {
        Timeframe.MN1: "месяц",
        Timeframe.W1: "неделю",
        Timeframe.D1: "день",
        Timeframe.H4: "4 часа",
        Timeframe.H1: "час",
        Timeframe.M30: "30 минут",
        Timeframe.M15: "15 минут",
        Timeframe.M10: "10 минут",
        Timeframe.M5: "5 минут",
    }[tf]


def _significant(factors: tuple[FactorContribution, ...]) -> list[FactorContribution]:
    return [
        f
        for f in factors
        if f.status is not FactorStatus.UNAVAILABLE
        and abs(f.contribution) > EXPLAIN_MIN_CONTRIBUTION
    ]


def render(forecast: Forecast, ctx: RenderContext) -> str:
    """Produce the final `explanation` string or raise.

    The filter is applied at the end so that any phrase produced via
    templates, factor names, or tickers is caught before the string
    leaves this module.
    """

    significant = _significant(forecast.factors)
    if len(significant) < MIN_SIGNIFICANT_FACTORS:
        raise Unexplainable(
            f"need ≥{MIN_SIGNIFICANT_FACTORS} significant factors, got {len(significant)}"
        )

    top = sorted(significant, key=lambda f: abs(f.contribution), reverse=True)[:3]

    last_close = ctx.last_close or Decimal(str(forecast.predicted_path[0].mean))
    target = forecast.predicted_path[-1].mean
    pct = ((target - last_close) / last_close * Decimal(100)).quantize(Decimal("0.01"))
    direction = "Рост" if pct >= 0 else "Падение"
    abs_pct = abs(pct)

    factor_names = ", ".join(f.name for f in top)
    mape_str = (
        f"{(ctx.current_mape * Decimal(100)).quantize(Decimal('0.01'))}%"
        if ctx.current_mape is not None
        else "неизвестна"
    )
    text = (
        f"{direction} на {abs_pct}% в течение {forecast.horizon} "
        f"{_bar_unit_ru(forecast.timeframe)}. "
        f"Основные факторы: {factor_names}. "
        f"Текущая MAPE модели {forecast.ticker}/{forecast.timeframe.value}: {mape_str}."
    )
    check_forbidden(text)
    return text


# ---------------------------------------------------------------------------
# Forbidden-phrases filter (T028)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _load_patterns(path: str) -> tuple[str, ...]:
    raw: Any = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected mapping at top level")
    patterns = raw.get("patterns", [])
    if not isinstance(patterns, list):
        raise ValueError(f"{path}: 'patterns' must be a list")
    return tuple(str(p).strip().lower() for p in patterns if p)


def reload_patterns() -> None:
    """Clear the cache so the next check_forbidden() re-reads the YAML.

    Used by the SIGHUP handler described in docs/runbook_forbidden_phrases.md.
    """

    _load_patterns.cache_clear()


def check_forbidden(text: str) -> None:
    """Raise ExplanationForbiddenPhrase if text contains any forbidden pattern.

    Matching is case-insensitive, substring-based (no regex — patterns stay
    explicit).
    """

    path = str(get_settings().forbidden_phrases_path)
    patterns = _load_patterns(path)
    haystack = text.lower()
    for p in patterns:
        if p and p in haystack:
            raise ExplanationForbiddenPhrase(f"forbidden phrase matched: {p!r}")
