"""A job queue in the database the jobs operate on.

Postgres rather than Redis or SQS for one reason that matters more than any
feature comparison: **enqueueing work and recording its result are the same
transaction as the data change that caused them.** A webhook that writes a job
and a worker that writes a snapshot are touching one system, so there is no state
where the queue believes something the index does not.

Three mechanisms carry the whole design:

`SELECT ... FOR UPDATE SKIP LOCKED`
    The claim. Ten workers run the same query concurrently and each gets a
    different row, with no coordinator and no leases to expire, because the
    database's own row locks are the arbitration. A worker that dies mid-job
    releases its lock when its connection drops.

`pg_try_advisory_lock`
    The coalescing. Indexing is one-per-repo at a time — two indexers racing on
    the same repository would duplicate every parse and interleave two snapshot
    writes. The lock is held for the *duration of the work*, not the claim, and
    it is `try_` rather than blocking: a worker that cannot get it leaves the job
    for later rather than sitting on a connection waiting.

`run_after`
    Both the retry backoff and the schedule. A failed job is not deleted and
    re-enqueued; it stays put with `run_after` pushed into the future, so its
    attempt count and its last error travel with it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Engine, and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .. import config
from ..db import schema


def worker_id(partition: int | None = None, total: int | None = None) -> str:
    """Who holds a job — host, pid, and partition. Human-readable on purpose:
    this string is what someone reads when a job has been `running` for an hour."""
    base = f"{socket.gethostname()}/{os.getpid()}"
    return f"{base}#{partition}of{total}" if total else base


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Job:
    id: int
    type: str
    payload: dict
    repo: str
    attempts: int
    status: str = "running"

    @classmethod
    def from_row(cls, row) -> "Job":
        payload = row["payload"]
        return cls(
            id=row["id"], type=row["type"],
            payload=json.loads(payload) if isinstance(payload, str) else (payload or {}),
            repo=row["repo"] or "", attempts=row["attempts"], status=row["status"],
        )


class JobQueue:
    """Enqueue, claim, finish. Every method is safe to call from many processes."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- enqueue ------------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        payload: dict | None = None,
        *,
        repo: str = "",
        run_after: datetime | None = None,
        coalesce: bool = False,
    ) -> int | None:
        """Add a job. With ``coalesce``, collapse into an existing queued one.

        Coalescing is what makes five rapid pushes cost fewer than five index
        passes: an `index_commit` for a repo that already has one waiting does
        not add a second row, it **overwrites the waiting row's payload with the
        newer sha**. The queue then holds one job pointing at the latest commit,
        which is the only commit anybody wants indexed — the intermediate shas
        become snapshots lazily, and only if an environment ever points at one.
        """
        if job_type not in schema.JOB_TYPES:
            raise ValueError(f"unknown job type {job_type!r}")
        payload = payload or {}

        with self.engine.begin() as connection:
            if coalesce:
                existing = connection.execute(
                    select(schema.jobs.c.id, schema.jobs.c.payload)
                    .where(
                        and_(
                            schema.jobs.c.type == job_type,
                            schema.jobs.c.repo == repo,
                            schema.jobs.c.status == schema.STATUS_QUEUED,
                        )
                    )
                    .order_by(schema.jobs.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).mappings().first()
                if existing is not None:
                    connection.execute(
                        update(schema.jobs)
                        .where(schema.jobs.c.id == existing["id"])
                        .values(payload=payload, updated_at=func.now())
                    )
                    return existing["id"]

            row = connection.execute(
                pg_insert(schema.jobs)
                .values(
                    type=job_type, payload=payload, repo=repo,
                    status=schema.STATUS_QUEUED, run_after=run_after or _now(),
                )
                .returning(schema.jobs.c.id)
            ).first()
            return row[0] if row else None

    # -- claim --------------------------------------------------------------

    def claim(
        self,
        worker: str,
        *,
        types: Iterable[str] | None = None,
        partition: int | None = None,
        partitions: int | None = None,
    ) -> Job | None:
        """Take the next eligible job, or ``None``.

        `FOR UPDATE SKIP LOCKED` inside one transaction is the entire
        concurrency story: rows another worker has locked are invisible to this
        query, so N workers polling simultaneously each get a different job and
        none of them block.

        Partitioning is `id % partitions = partition`, applied in the same
        WHERE clause. It is a *filter*, not a separate queue — so a partition
        with no work does not starve while another has a backlog it cannot share,
        and stopping a worker does not strand its jobs.
        """
        conditions = [
            schema.jobs.c.status == schema.STATUS_QUEUED,
            schema.jobs.c.run_after <= func.now(),
        ]
        if types:
            conditions.append(schema.jobs.c.type.in_(list(types)))
        if partitions and partitions > 1:
            conditions.append(schema.jobs.c.id % partitions == (partition or 0))

        with self.engine.begin() as connection:
            row = connection.execute(
                select(schema.jobs)
                .where(and_(*conditions))
                .order_by(schema.jobs.c.run_after, schema.jobs.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).mappings().first()
            if row is None:
                return None
            connection.execute(
                update(schema.jobs)
                .where(schema.jobs.c.id == row["id"])
                .values(
                    status=schema.STATUS_RUNNING,
                    attempts=schema.jobs.c.attempts + 1,
                    locked_by=worker,
                    locked_at=func.now(),
                    updated_at=func.now(),
                )
            )
            return Job.from_row(row)

    # -- finish -------------------------------------------------------------

    def complete(self, job_id: int, result: dict | None = None) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(schema.jobs).where(schema.jobs.c.id == job_id).values(
                    status=schema.STATUS_DONE, result=result or {},
                    locked_by=None, updated_at=func.now(),
                )
            )

    def fail(self, job_id: int, error: str, *, retry_in: timedelta | None = None) -> None:
        """Record a failure. Retryable failures go back to `queued` with backoff.

        The job row is reused rather than replaced so `attempts` and `last_error`
        accumulate on the thing that is failing — a job on its fourth attempt with
        four different errors is a very different problem from four jobs that each
        failed once, and a queue that deletes and re-inserts cannot tell them apart.
        """
        values: dict[str, Any] = {
            "last_error": error[:2000], "locked_by": None, "updated_at": func.now(),
        }
        if retry_in is not None:
            values["status"] = schema.STATUS_QUEUED
            values["run_after"] = _now() + retry_in
        else:
            values["status"] = schema.STATUS_FAILED
        with self.engine.begin() as connection:
            connection.execute(
                update(schema.jobs).where(schema.jobs.c.id == job_id).values(**values)
            )

    def release(self, job_id: int, *, retry_in: timedelta | None = None) -> None:
        """Put a claimed job back without counting it as a failure.

        Used when a worker claims an `index_commit` it cannot get the repo lock
        for: nothing went wrong, somebody else is simply already indexing that
        repository, and this job should be picked up shortly.
        """
        with self.engine.begin() as connection:
            connection.execute(
                update(schema.jobs).where(schema.jobs.c.id == job_id).values(
                    status=schema.STATUS_QUEUED,
                    run_after=_now() + (retry_in or timedelta(seconds=5)),
                    locked_by=None, updated_at=func.now(),
                )
            )

    # -- introspection ------------------------------------------------------

    def depth(self, repo: str | None = None) -> dict[str, int]:
        """Queued/running/failed counts — the number `/health/index` reports."""
        query = select(schema.jobs.c.status, func.count()).group_by(schema.jobs.c.status)
        if repo:
            query = query.where(schema.jobs.c.repo == repo)
        with self.engine.connect() as connection:
            counts = {row[0]: row[1] for row in connection.execute(query)}
        return {
            "queued": counts.get(schema.STATUS_QUEUED, 0),
            "running": counts.get(schema.STATUS_RUNNING, 0),
            "failed": counts.get(schema.STATUS_FAILED, 0),
            "done": counts.get(schema.STATUS_DONE, 0),
        }

    def recent(self, limit: int = 20, repo: str | None = None) -> list[dict]:
        query = select(schema.jobs).order_by(schema.jobs.c.id.desc()).limit(limit)
        if repo:
            query = query.where(schema.jobs.c.repo == repo)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def reap_stale(self, older_than: timedelta = timedelta(minutes=30)) -> int:
        """Return jobs whose worker died mid-run to the queue.

        A crashed process releases its row lock immediately, but the row it had
        already flipped to `running` stays that way forever — it is nobody's job
        and nobody is doing it. This is the only thing that notices.
        """
        cutoff = _now() - older_than
        with self.engine.begin() as connection:
            result = connection.execute(
                update(schema.jobs)
                .where(
                    and_(
                        schema.jobs.c.status == schema.STATUS_RUNNING,
                        schema.jobs.c.locked_at < cutoff,
                    )
                )
                .values(
                    status=schema.STATUS_QUEUED, locked_by=None,
                    last_error="reclaimed: worker went away mid-job", updated_at=func.now(),
                )
                .returning(schema.jobs.c.id)
            )
            return len(result.fetchall())


# --------------------------------------------------------------------------- advisory locks


#: One namespace per lock kind, so an index lock and (later) some other
#: per-repo lock cannot collide on the same 64-bit key space.
LOCK_NAMESPACE_INDEX = 0x8121


class RepoLock:
    """One indexer per repository, enforced by Postgres, released on disconnect.

    Session-scoped rather than transaction-scoped (`pg_try_advisory_lock`, not
    `..._xact_lock`) because the work it guards spans many transactions: parse,
    write chunks, write descriptions, write the snapshot. A transaction-scoped
    lock would let go after the first of those.
    """

    def __init__(self, engine: Engine, repo: str) -> None:
        self.engine = engine
        self.repo = repo
        # a stable 32-bit key from the repo name; the pair form takes two int4s
        self.key = abs(hash(repo)) % (2**31)
        self._connection = None

    def acquire(self) -> bool:
        connection = self.engine.connect()
        got = connection.execute(
            text("SELECT pg_try_advisory_lock(:ns, :key)"),
            {"ns": LOCK_NAMESPACE_INDEX, "key": self.key},
        ).scalar_one()
        if got:
            self._connection = connection
            return True
        connection.close()
        return False

    def release(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:ns, :key)"),
                {"ns": LOCK_NAMESPACE_INDEX, "key": self.key},
            )
        finally:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
