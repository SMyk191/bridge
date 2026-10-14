"""Where the database is, decided in exactly one place.

Three ways to point Bridge at Postgres, in precedence order:

1. ``BRIDGE_DATABASE_URL`` in the environment — what a deployment sets;
2. ``config.DATABASE_URL`` — what `docker-compose.yml` brings up locally;
3. ``embedded://`` — a real PostgreSQL 16.2 started from the venv by `pgserver`,
   with its data directory under ``data/pg/``.

The third exists because this project has to be runnable, and testable, on a
machine with no Docker and no system Postgres. It is the *same* Postgres — same
version, same `vector` extension, same SQL — reached through the same SQLAlchemy
engine, so nothing above this module can tell the difference. That is the point:
if the embedded path and the compose path diverged in behaviour, the tests would
be testing something nobody deploys.
"""

from __future__ import annotations

import atexit
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text

from .. import config

_LOCK = threading.Lock()
_ENGINES: dict[str, Engine] = {}
_EMBEDDED: Any = None


def _start_embedded(data_dir: Path) -> str:
    """Boot (or reuse) the venv-local Postgres and return its DSN.

    `pgserver` reference-counts a server per data directory across processes, so
    repeated calls in one session are cheap and a second process (a test worker,
    say) attaches to the same instance rather than fighting over the port.
    """
    global _EMBEDDED
    import pgserver

    data_dir.mkdir(parents=True, exist_ok=True)
    if _EMBEDDED is None:
        _EMBEDDED = pgserver.get_server(data_dir)
        atexit.register(_shutdown_embedded)
    return _EMBEDDED.get_uri()


def _shutdown_embedded() -> None:
    global _EMBEDDED
    server, _EMBEDDED = _EMBEDDED, None
    if server is not None:
        try:
            server.cleanup()
        except Exception:
            # Interpreter shutdown is not the place to raise about a database
            # we were finished with anyway.
            pass


def _with_psycopg3(url: str) -> str:
    """Pin the driver to psycopg 3.

    A bare ``postgresql://`` DSN — which is what `pgserver` hands back, and what
    most tooling prints — makes SQLAlchemy reach for psycopg2, which this project
    does not install. Naming the driver here means every caller can keep passing
    ordinary DSNs around without knowing or caring which binding is in the venv.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def resolve_url(url: str | None = None) -> str:
    """The DSN to connect to, expanding ``embedded://`` into a live server."""
    url = url or config.database_url()
    if url.startswith("embedded://"):
        # embedded://          -> data/pg
        # embedded://<subdir>  -> data/pg/<subdir>, so tests can have their own
        suffix = url[len("embedded://") :].strip("/")
        root = config.EMBEDDED_PG_ROOT / suffix if suffix else config.EMBEDDED_PG_ROOT
        return _with_psycopg3(_start_embedded(root))
    return _with_psycopg3(url)


def get_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """A pooled engine for ``url``, created once per DSN per process."""
    resolved = resolve_url(url)
    with _LOCK:
        engine = _ENGINES.get(resolved)
        if engine is None:
            engine = create_engine(resolved, future=True, pool_pre_ping=True, **kwargs)
            _ENGINES[resolved] = engine
        return engine


def ensure_extensions(engine: Engine) -> None:
    """`CREATE EXTENSION vector`. Idempotent, and required before any migration."""
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def create_all(engine: Engine) -> None:
    """Create every table directly from the metadata, skipping Alembic.

    For tests and for `embedded://`, where a throwaway database wants the current
    schema and nothing else. Deployments run `alembic upgrade head` instead, so
    that the *history* of the schema is a reviewable artifact rather than
    whatever shape the code happened to have.
    """
    from .schema import metadata
    from .vectorstore import PgVectorStore

    ensure_extensions(engine)
    metadata.create_all(engine)
    # `vectors` is LIST-partitioned, and a partitioned table with no partitions
    # accepts no rows, so the default partition is part of "the schema exists"
    # rather than a tuning step that can be deferred.
    PgVectorStore(engine).ensure_layout()


def dispose_all() -> None:
    with _LOCK:
        for engine in _ENGINES.values():
            engine.dispose()
        _ENGINES.clear()
