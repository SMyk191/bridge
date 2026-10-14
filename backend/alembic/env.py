"""Alembic environment — reads the DSN from bridge.db.engine, never from alembic.ini."""

from __future__ import annotations

from alembic import context

from bridge.db import engine as db_engine
from bridge.db.schema import metadata

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_engine.resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = db_engine.get_engine()
    # `vector` has to exist before a migration can declare a Vector column.
    db_engine.ensure_extensions(connectable)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
