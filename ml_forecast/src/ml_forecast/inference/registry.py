"""Model registry — owner of the Model Artifact state machine.

The registry is the only module that writes to ``ml.model_registry``.
It enforces three invariants:

1. **State transitions** follow :data:`ModelState.ALLOWED_TRANSITIONS`
   at the application level. The database partial-UK
   ``uq_model_registry_production`` is a backstop — if the app tries
   to have two production rows, the DB rejects the second INSERT/UPDATE.

2. **Promote is atomic**: within a single SQL transaction we move the
   current production row to ``archived`` AND update the symlink on
   disk. If either step fails, the whole promotion aborts.

3. **Archive is lossless**: physical ``.joblib`` + ``metadata.json``
   are moved to ``.../archive/``, not deleted. Retention is handled
   by :func:`artifact_rotation` (T094).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ml_forecast.domain.model_state import (
    IllegalStateTransition,
    ModelState,
    assert_transition,
)
from ml_forecast.domain.timeframe import Timeframe
from ml_forecast.models.base import BaseForecaster, ModelFamily
from ml_forecast.storage import artifact_store
from ml_forecast.storage.orm import ModelRegistry as ModelRow
from ml_forecast.storage.postgres import session_scope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class ModelNotFound(LookupError):
    """No registry row matches the requested (ticker, timeframe[, version])."""


class PromoteFailed(RuntimeError):
    """Promote/Archive detected an inconsistency and aborted."""


@dataclass(frozen=True, slots=True)
class ModelHandle:
    id: int
    ticker: str
    timeframe: Timeframe
    model_version: str
    model_family: ModelFamily
    state: ModelState
    artifact_path: str | None
    current_mape: Decimal | None
    feature_set_version: str
    promoted_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RegistrationInput:
    ticker_id: int
    ticker: str
    timeframe: Timeframe
    model_family: ModelFamily
    model: BaseForecaster
    dataset_sha256: str
    feature_set_version: str
    metrics_aggregate: dict[str, float]
    metrics_fold: list[dict[str, float]] = field(default_factory=list)
    comparison_to_prev: dict[str, float] | None = None


def make_model_version(family: ModelFamily, dataset_sha256: str) -> str:
    """Deterministic model_version — family + date + short dataset hash.

    Keeps the format human-readable for admin UIs while guaranteeing
    uniqueness within a (ticker, timeframe) pair via the UK.
    """

    today = datetime.utcnow().strftime("%Y-%m-%d")
    short = dataset_sha256[:6]
    salt = hashlib.sha1(
        f"{family.value}:{dataset_sha256}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:4]
    return f"{family.value}-{today}-{short}-{salt}"


def _row_to_handle(row: ModelRow, ticker_str: str) -> ModelHandle:
    return ModelHandle(
        id=row.id,
        ticker=ticker_str,
        timeframe=Timeframe(row.timeframe),
        model_version=row.model_version,
        model_family=ModelFamily(row.model_family),
        state=ModelState(row.state),
        artifact_path=row.artifact_path,
        current_mape=row.current_mape,
        feature_set_version=row.feature_set_version,
        promoted_at=row.promoted_at,
        created_at=row.created_at,
    )


def _resolve_ticker(session: "Session", ticker: str) -> int:
    """Look up tickers.id from the shared backend table.

    The ``tickers`` table is owned by the backend feature and lives in
    the default ``public`` schema. Using a raw SELECT keeps us free of
    coupling against a backend ORM model.
    """

    from sqlalchemy import text

    row = session.execute(
        text("SELECT id FROM public.tickers WHERE symbol = :s"),
        {"s": ticker},
    ).first()
    if row is None:
        raise ModelNotFound(f"unknown ticker symbol: {ticker}")
    return int(row[0])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_production(ticker: str, timeframe: Timeframe) -> ModelHandle:
    with session_scope() as s:
        ticker_id = _resolve_ticker(s, ticker)
        row = s.execute(
            select(ModelRow)
            .where(ModelRow.ticker_id == ticker_id)
            .where(ModelRow.timeframe == timeframe.value)
            .where(ModelRow.state == ModelState.PRODUCTION.value)
        ).scalar_one_or_none()
        if row is None:
            raise ModelNotFound(
                f"no production model for {ticker}/{timeframe.value}"
            )
        return _row_to_handle(row, ticker)


def list_models(
    ticker: str | None = None,
    timeframe: Timeframe | None = None,
    state: ModelState | None = None,
) -> list[ModelHandle]:
    with session_scope() as s:
        stmt = select(ModelRow)
        ticker_str = ticker
        if ticker:
            ticker_id = _resolve_ticker(s, ticker)
            stmt = stmt.where(ModelRow.ticker_id == ticker_id)
        if timeframe:
            stmt = stmt.where(ModelRow.timeframe == timeframe.value)
        if state:
            stmt = stmt.where(ModelRow.state == state.value)
        rows = list(s.execute(stmt.order_by(ModelRow.created_at.desc())).scalars())
        # The ticker→symbol translation is only easy when the caller filtered
        # by ticker; otherwise we fall back to ticker_id stringified.
        return [
            _row_to_handle(row, ticker_str or f"#{row.ticker_id}")
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def register_shadow(reg_input: RegistrationInput) -> ModelHandle:
    """Persist a freshly trained model in state=shadow.

    The artifact has already been saved to disk by the caller; this
    method just records it in the DB.
    """

    model_version = make_model_version(
        reg_input.model_family, reg_input.dataset_sha256
    )
    artifact_path = artifact_store.save(
        reg_input.model,
        ticker=reg_input.ticker,
        timeframe=reg_input.timeframe,
        model_version=model_version,
        metadata=artifact_store.ArtifactMetadata.new(
            ticker=reg_input.ticker,
            timeframe=reg_input.timeframe,
            model_version=model_version,
            model_family=reg_input.model_family.value,
            feature_set_version=reg_input.feature_set_version,
            dataset_sha256=reg_input.dataset_sha256,
            metrics=reg_input.metrics_aggregate,
        ),
    )
    sha = artifact_store.artifact_sha256(
        reg_input.ticker, reg_input.timeframe, model_version
    )

    with session_scope() as s:
        row = ModelRow(
            ticker_id=reg_input.ticker_id,
            timeframe=reg_input.timeframe.value,
            model_family=reg_input.model_family.value,
            model_version=model_version,
            state=ModelState.SHADOW.value,
            artifact_path=str(artifact_path),
            artifact_sha256=sha,
            dataset_sha256=reg_input.dataset_sha256,
            feature_set_version=reg_input.feature_set_version,
            current_mape=Decimal(str(reg_input.metrics_aggregate.get("mape", 0.0))),
        )
        s.add(row)
        s.flush()
        return _row_to_handle(row, reg_input.ticker)


def register_do_not_promote(
    reg_input: RegistrationInput, reason: str
) -> ModelHandle:
    """Persist a trained model in state=do_not_promote (FR-022 / FR-025).

    No artifact-store.save; we still record the row for auditability
    but skip filesystem-side persistence to avoid clutter.
    """

    model_version = make_model_version(
        reg_input.model_family, reg_input.dataset_sha256
    )
    with session_scope() as s:
        row = ModelRow(
            ticker_id=reg_input.ticker_id,
            timeframe=reg_input.timeframe.value,
            model_family=reg_input.model_family.value,
            model_version=model_version,
            state=ModelState.DO_NOT_PROMOTE.value,
            artifact_path=None,
            dataset_sha256=reg_input.dataset_sha256,
            feature_set_version=reg_input.feature_set_version,
        )
        s.add(row)
        s.flush()
        return _row_to_handle(row, reg_input.ticker)


def promote(ticker: str, timeframe: Timeframe, model_version: str) -> tuple[ModelHandle, ModelHandle | None]:
    """Move ``model_version`` from shadow to production atomically.

    Returns ``(new_production, previous_production_or_None)``.
    Raises :class:`PromoteFailed` if the shadow row is missing or if
    the DB partial-UK fires.
    """

    with session_scope() as s:
        ticker_id = _resolve_ticker(s, ticker)
        new_row = s.execute(
            select(ModelRow)
            .where(ModelRow.ticker_id == ticker_id)
            .where(ModelRow.timeframe == timeframe.value)
            .where(ModelRow.model_version == model_version)
            .with_for_update()
        ).scalar_one_or_none()
        if new_row is None:
            raise ModelNotFound(
                f"{ticker}/{timeframe.value} has no version {model_version}"
            )
        try:
            assert_transition(ModelState(new_row.state), ModelState.PRODUCTION)
        except IllegalStateTransition as exc:
            raise PromoteFailed(str(exc)) from exc

        previous_handle: ModelHandle | None = None
        prev_row = s.execute(
            select(ModelRow)
            .where(ModelRow.ticker_id == ticker_id)
            .where(ModelRow.timeframe == timeframe.value)
            .where(ModelRow.state == ModelState.PRODUCTION.value)
            .with_for_update()
        ).scalar_one_or_none()
        if prev_row is not None:
            # Archive the incumbent. Do this FIRST to keep the partial UK
            # satisfied at every point in the transaction.
            s.execute(
                update(ModelRow)
                .where(ModelRow.id == prev_row.id)
                .values(
                    state=ModelState.ARCHIVED.value,
                    archived_at=datetime.utcnow(),
                )
            )
            previous_handle = _row_to_handle(prev_row, ticker)

        s.execute(
            update(ModelRow)
            .where(ModelRow.id == new_row.id)
            .values(
                state=ModelState.PRODUCTION.value,
                promoted_at=datetime.utcnow(),
            )
        )
        try:
            s.flush()
        except IntegrityError as exc:
            raise PromoteFailed(
                "partial UK fired — another production row already exists"
            ) from exc

        # Switch the symlink on disk. Only reached when the DB transaction
        # has already succeeded in flush; the commit happens after
        # session_scope exits normally.
        try:
            artifact_store.set_production(ticker, timeframe, model_version)
        except FileNotFoundError as exc:
            raise PromoteFailed(f"artifact missing on disk: {exc}") from exc

        s.refresh(new_row)
        new_handle = _row_to_handle(new_row, ticker)
        return new_handle, previous_handle


def archive(ticker: str, timeframe: Timeframe, model_version: str) -> ModelHandle:
    """Move any non-archived row to state=archived + move artifact to archive/."""

    with session_scope() as s:
        ticker_id = _resolve_ticker(s, ticker)
        row = s.execute(
            select(ModelRow)
            .where(ModelRow.ticker_id == ticker_id)
            .where(ModelRow.timeframe == timeframe.value)
            .where(ModelRow.model_version == model_version)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise ModelNotFound(
                f"{ticker}/{timeframe.value}#{model_version}: no such row"
            )
        try:
            assert_transition(ModelState(row.state), ModelState.ARCHIVED)
        except IllegalStateTransition as exc:
            raise PromoteFailed(str(exc)) from exc
        s.execute(
            update(ModelRow)
            .where(ModelRow.id == row.id)
            .values(state=ModelState.ARCHIVED.value, archived_at=datetime.utcnow())
        )
        artifact_store.archive(ticker, timeframe, model_version)
        s.refresh(row)
        return _row_to_handle(row, ticker)


def update_current_mape(model_id: int, mape: float) -> None:
    """Called by the daily recompute_daily_metrics Celery task (T076)."""

    with session_scope() as s:
        s.execute(
            update(ModelRow)
            .where(ModelRow.id == model_id)
            .values(current_mape=Decimal(str(mape)), updated_at=datetime.utcnow())
        )


def load_forecaster(handle: ModelHandle) -> BaseForecaster:
    if handle.artifact_path is None:
        raise PromoteFailed(
            f"{handle.ticker}/{handle.timeframe.value}#{handle.model_version} "
            "has no artifact on disk"
        )
    from pathlib import Path

    forecaster = artifact_store.load(Path(handle.artifact_path))
    if not isinstance(forecaster, BaseForecaster):
        raise PromoteFailed(
            f"artifact at {handle.artifact_path} is not a BaseForecaster"
        )
    return forecaster
