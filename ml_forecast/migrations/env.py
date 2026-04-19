"""Alembic environment — online migrations against the shared Finzooka DB.

Reads DB URL from ML_DATABASE_URL (preferred) or sqlalchemy.url in alembic.ini.
Target metadata is imported lazily so missing ORM modules fail early with a
clear message instead of an opaque SQLAlchemy error.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

db_url = os.getenv("ML_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not db_url:
    raise RuntimeError(
        "ML_DATABASE_URL is not set and sqlalchemy.url is empty in alembic.ini"
    )
config.set_main_option("sqlalchemy.url", db_url)

try:
    from ml_forecast.storage.postgres import Base  # noqa: WPS433 — lazy by design
except ModuleNotFoundError:
    # Happens before T015 lands; allow `alembic revision --autogenerate` to fail
    # loudly rather than silently skip metadata.
    target_metadata = None
else:
    target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="ml",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.execute(  # type: ignore[call-arg]
            __import__("sqlalchemy").text("CREATE SCHEMA IF NOT EXISTS ml")
        )
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="ml",
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
