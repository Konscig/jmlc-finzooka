"""SQLAlchemy engine + session factory, shared by ORM and Alembic.

The :class:`Base` declarative class has ``schema=ml`` so every mapped
model lives in the dedicated ``ml`` schema alongside shared Finzooka
tables (``tickers``, ``model_metrics``, ``pipeline_statuses``) in public.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ml_forecast.config import get_settings

SCHEMA: str = "ml"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_engine():  # noqa: ANN201 — engine type is opaque
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""

    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
