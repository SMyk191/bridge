"""`bridge worker` — claim a job, do it, record what happened, repeat.

Every handler is **idempotent by construction**, which is not a discipline
anyone has to maintain: it falls out of the storage model. Running
`index_commit` twice writes the same content hashes the second time, and every
write is `ON CONFLICT DO NOTHING`, so the second run reports zero new rows and
changes nothing. That is why a crashed worker can simply have its job re-run,
and why `reap_stale` is safe to be aggressive.

The one place that needs real coordination is indexing the same repository
twice at once, and that is what `RepoLock` is for.
"""

from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import Engine

from .. import config
from ..db import create_all, get_engine, keys, retention, schema
from ..db.repo import IndexRepo
from ..index import pgindex
from .limiter import LIMITER
from .queue import Job, JobQueue, RepoLock, worker_id


@dataclass
class WorkerStats:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    skipped_locked: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def record(self, job_type: str) -> None:
        self.by_type[job_type] = self.by_type.get(job_type, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed, "completed": self.completed, "failed": self.failed,
            "skipped_locked": self.skipped_locked, "by_type": dict(sorted(self.by_type.items())),
        }


def repo_path_for(repo_name: str) -> Path:
    """Where a repository's working tree lives.

    Org-specific, so it reads from config rather than being hardcoded (design
    law 7). Today every repo lives under `data/repos/<name>`.
    """
    return config.PROJECT_ROOT / "data" / "repos" / repo_name


# --------------------------------------------------------------------------- handlers


def handle_index_commit(job: Job, engine: Engine) -> dict:
    """Index the sha in the payload — or, if newer pushes arrived, the latest one.

    The payload's sha is a *hint*, not an instruction. Between this job being
    enqueued and being claimed, more pushes may have landed and coalesced into
    it; and the working tree is at whatever HEAD it is at. Indexing the tip and
    recording which sha was actually indexed is both cheaper and more honest than
    indexing a commit nobody is on any more.
    """
    repo_name = job.repo or job.payload.get("repo", "")
    root = Path(job.payload.get("root_path") or repo_path_for(repo_name))
    if not root.is_dir():
        raise FileNotFoundError(f"no working tree at {root}")

    repo = IndexRepo(engine)
    requested = job.payload.get("sha")
    actual = pgindex.head_sha(root)

    previous = None
    latest = repo.latest_snapshot(repo_name)
    if latest and latest["sha"] != actual:
        previous = latest["sha"]

    report = pgindex.index_repo(root, repo, sha=actual, previous_sha=previous)
    result = report.as_dict()
    # The gap between what was asked for and what was indexed is the coalescing
    # story, visible per job rather than inferred from timestamps.
    result["requested_sha"] = requested
    result["skipped_intermediate"] = bool(requested and requested != actual)
    return result


def handle_describe_backfill(job: Job, engine: Engine) -> dict:
    """Describe undescribed chunks, most valuable first.

    Priority is `degree x recency x project traffic`, which is a statement about
    what an undescribed chunk COSTS rather than about the chunk itself: a hub
    nobody described breaks every answer that would have walked through it, while
    an unreferenced leaf in a project nobody asks about breaks nothing. Ordering
    by that means a backfill that never finishes has still fixed the parts that
    mattered.
    """
    repo_name = job.repo
    repo = IndexRepo(engine)
    snapshot = repo.latest_snapshot(repo_name)
    if snapshot is None:
        return {"described": 0, "reason": "no snapshot for this repo yet"}

    limit = int(job.payload.get("limit") or config.BACKFILL_BATCH)
    model = job.payload.get("model") or keys.SOURCE_TEMPLATE
    # Phase G: the same handler closes two different gaps — code that was never
    # described, and code described at a prompt version retrieval no longer
    # prefers. Both are "no row at the current key", so both are this job.
    prompt_version = job.payload.get("prompt_version") or keys.PROMPT_VERSION
    candidates = backfill_candidates(repo, snapshot["id"], model, limit, prompt_version)

    from ..index.tiers import template_description

    rows = []
    for candidate in candidates:
        code_hash = candidate["code_hash"]
        text = template_description(
            candidate["kind"], candidate["name"], candidate.get("attrs") or {},
            (candidate.get("qualified_name") or "").rsplit(".", 1)[-1].split("#", 1)[0],
        )
        rows.append(
            {
                "desc_key": keys.desc_key(code_hash, model, prompt_version),
                "chunk_hash": candidate["chunk_hash"], "code_hash": code_hash,
                "model": model, "prompt_version": prompt_version,
                "text": text, "source": model,
            }
        )
    written = repo.add_descriptions(rows) if rows else 0
    return {
        "described": written,
        "considered": len(candidates),
        "model": model,
        "prompt_version": prompt_version,
        "order": [c["qualified_name"] for c in candidates[:10]],
    }


def backfill_candidates(
    repo: IndexRepo, snapshot_id: str, model: str, limit: int,
    prompt_version: str | None = None,
) -> list[dict]:
    """Undescribed chunks, sorted by what leaving them undescribed costs.

    Split out from the handler so the ordering can be inspected and tested
    without running a job — "the backfill picked things in a sensible order" is
    otherwise a claim nobody can check.
    """
    from sqlalchemy import and_, func, select

    edges, chunks, files, desc = schema.edges, schema.chunks, schema.snapshot_files, schema.descriptions

    # semantic in-degree per chunk: how many things point at it
    degree = (
        select(edges.c.dst.label("chunk_hash"), func.count().label("degree"))
        .group_by(edges.c.dst)
        .subquery()
    )
    query = (
        select(
            chunks,
            files.c.path,
            func.coalesce(degree.c.degree, 0).label("degree"),
        )
        .select_from(
            chunks
            .join(files, chunks.c.blob_hash == files.c.blob_hash)
            .outerjoin(
                desc,
                # Both halves of the version matter. Matching on model alone
                # makes a prompt-version bump invisible to the backfill: every
                # chunk looks described, while the key retrieval will ask for
                # does not exist yet.
                and_(
                    desc.c.code_hash == chunks.c.code_hash,
                    desc.c.model == model,
                    desc.c.prompt_version == (prompt_version or keys.PROMPT_VERSION),
                ),
            )
            .outerjoin(degree, degree.c.chunk_hash == chunks.c.chunk_hash)
        )
        .where(
            and_(
                files.c.snapshot_id == snapshot_id,
                desc.c.desc_key.is_(None),
                chunks.c.tier == config.TIER_FULL,
            )
        )
        .order_by(func.coalesce(degree.c.degree, 0).desc(), chunks.c.chunk_hash)
        .limit(limit)
    )
    with repo.engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def handle_summarise_modules(job: Job, engine: Engine) -> dict:
    """Recompute module summaries on the changed ancestor chains only.

    A directory's merkle key is a hash of its children's keys, so a summary is
    reusable exactly when nothing beneath it changed. Recomputing the whole tree
    would be correct and wasteful; recomputing one path to the root is the point.
    """
    repo = IndexRepo(engine)
    snapshot = repo.latest_snapshot(job.repo)
    if snapshot is None:
        return {"summaries": 0, "reason": "no snapshot"}

    files = repo.snapshot_files(snapshot["id"])
    # bottom-up: a directory's key needs its children's keys to exist first
    by_directory: dict[str, list[str]] = {}
    for path, blob in sorted(files.items()):
        parent = str(Path(path).parent).replace("\\", "/")
        parent = "" if parent == "." else parent
        by_directory.setdefault(parent, []).append(blob)
    for directory in sorted(by_directory, key=lambda d: -d.count("/")):
        parent = directory.rsplit("/", 1)[0] if "/" in directory else ""
        if directory:
            by_directory.setdefault(parent, []).append(
                keys.merkle_key(directory, by_directory[directory])
            )

    rows = [
        {
            "merkle_key": keys.merkle_key(directory, children),
            "path": directory or ".",
            "level": directory.count("/") if directory else 0,
            "text": "",
            "children_keys": sorted(children),
        }
        for directory, children in by_directory.items()
    ]
    written = repo.add_module_summaries(rows)
    return {"summaries": written, "directories": len(rows), "unchanged": len(rows) - written}


def handle_verify_nightly(job: Job, engine: Engine) -> dict:
    repo = IndexRepo(engine)
    results = {}
    repos = [job.repo] if job.repo else [row["repo"] for row in repo.snapshots(limit=100)]
    for repo_name in dict.fromkeys(repos):
        snapshot = repo.latest_snapshot(repo_name)
        root = repo_path_for(repo_name)
        if snapshot is None or not root.is_dir():
            continue
        drift = pgindex.verify_snapshot(root, repo, snapshot["id"])
        results[repo_name] = {
            "checked": drift.checked, "drifted": len(drift.drifted),
            "missing": len(drift.missing), "orphans": len(drift.orphans), "clean": drift.clean,
        }
    return results


def handle_gc_weekly(job: Job, engine: Engine) -> dict:
    """The retention sweep. Deletes, now, and says exactly what it deleted.

    Phase F's version counted and deleted nothing, which was the right caution
    with the wrong mechanism: it asked "is this blob in today's snapshot?" and a
    content-addressed row shared across history will often not be. Phase G asks
    the question that actually matters — "can any *retained* snapshot reach it?"
    — and retention is a policy with four named rules (db/retention.py).

    `dry_run` in the payload runs the whole thing and rolls it back, so a nightly
    report and a real sweep exercise the same code.
    """
    dry_run = bool(job.payload.get("dry_run", False))
    report = retention.collect(engine, dry_run=dry_run)
    return report.as_dict()


def handle_health_alert(job: Job, engine: Engine) -> dict:
    """Compute health, diff against the last check, push to Slack if anything fired."""
    from . import alerts

    return alerts.run_health_alert_check(engine, repo_root_for=repo_path_for)


def handle_vocab_staleness(job: Job, engine: Engine) -> dict:
    """Flag vocab terms naming a node the current graph no longer has, and
    propose same-name replacements where one exists (work/vocab_staleness.py).

    Runs against "the current index" — `_resolve_index_dir()` with no sha, same
    resolution retrieval itself uses — rather than iterating Postgres's `repo.
    snapshots()` the way `handle_verify_nightly` does: the vocabulary and the
    graph it's checked against (`retrieval/vocab.py`, `index/graph.py`) are both
    still the legacy SQLite index, one directory per commit, with no multi-repo
    concept to select among yet. `job.repo`, when given, is recorded on the
    result but does not change what is checked.
    """
    from . import vocab_staleness
    from ..retrieval.retrieve import Retriever, _resolve_index_dir

    try:
        index_dir = _resolve_index_dir()
    except FileNotFoundError as exc:
        return {"stale_terms": 0, "reason": str(exc)}
    with Retriever.open(index_dir) as retriever:
        return vocab_staleness.check(retriever.vocabulary, retriever.store, retriever.repo)


HANDLERS: dict[str, Callable[[Job, Engine], dict]] = {
    schema.JOB_INDEX_COMMIT: handle_index_commit,
    schema.JOB_DESCRIBE_BACKFILL: handle_describe_backfill,
    schema.JOB_SUMMARISE_MODULES: handle_summarise_modules,
    schema.JOB_VERIFY_NIGHTLY: handle_verify_nightly,
    schema.JOB_GC_WEEKLY: handle_gc_weekly,
    schema.JOB_HEALTH_ALERT: handle_health_alert,
    schema.JOB_VOCAB_STALENESS: handle_vocab_staleness,
}


# --------------------------------------------------------------------------- the loop


class Worker:
    def __init__(
        self,
        engine: Engine | None = None,
        *,
        partition: int = 0,
        partitions: int = 1,
        types: list[str] | None = None,
    ) -> None:
        self.engine = engine or get_engine()
        self.queue = JobQueue(self.engine)
        self.partition = partition
        self.partitions = partitions
        self.types = types
        self.id = worker_id(partition, partitions)
        self.stats = WorkerStats()
        self._stopping = False

    def stop(self, *_signal: Any) -> None:
        self._stopping = True

    def run_one(self) -> bool:
        """Claim and run one job. False when there was nothing to do."""
        job = self.queue.claim(
            self.id, types=self.types, partition=self.partition, partitions=self.partitions
        )
        if job is None:
            return False
        self.stats.claimed += 1
        self.stats.record(job.type)

        # Coalescing's second half: one indexer per repo at a time. The lock is
        # taken AFTER the claim so the job is not lost if we cannot get it — it
        # goes back to the queue for whoever finishes first.
        lock = None
        if job.type == schema.JOB_INDEX_COMMIT and job.repo:
            lock = RepoLock(self.engine, job.repo)
            if not lock.acquire():
                self.stats.skipped_locked += 1
                self.queue.release(
                    job.id, retry_in=timedelta(seconds=config.WORKER_REPO_LOCK_RETRY_SECONDS)
                )
                return True

        try:
            handler = HANDLERS.get(job.type)
            if handler is None:
                self.queue.fail(job.id, f"no handler for job type {job.type!r}")
                self.stats.failed += 1
                return True
            result = handler(job, self.engine)
            self.queue.complete(job.id, result)
            self.stats.completed += 1
            LIMITER.on_response("index", None)
        except Exception as exc:
            LIMITER.on_response("index", exc)
            self.stats.failed += 1
            if job.attempts >= config.WORKER_MAX_ATTEMPTS:
                # Out of retries. The row stays, with its attempt count and its
                # last error, because a job that failed five times is a thing
                # somebody needs to look at, not a thing to delete.
                self.queue.fail(job.id, f"{exc.__class__.__name__}: {exc}")
            else:
                backoff = min(
                    config.WORKER_RETRY_MAX_SECONDS,
                    config.WORKER_RETRY_BASE_SECONDS * (2 ** job.attempts),
                )
                self.queue.fail(
                    job.id, f"{exc.__class__.__name__}: {exc}", retry_in=timedelta(seconds=backoff)
                )
        finally:
            if lock is not None:
                lock.release()
        return True

    def run_forever(self, max_jobs: int | None = None, idle_exit: bool = False) -> WorkerStats:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        last_cron = 0.0
        while not self._stopping:
            if max_jobs is not None and self.stats.claimed >= max_jobs:
                break
            did_work = self.run_one()
            if time.monotonic() - last_cron > 60:
                self.tick_schedules()
                self.queue.reap_stale(timedelta(minutes=config.WORKER_STALE_AFTER_MINUTES))
                last_cron = time.monotonic()
            if not did_work:
                if idle_exit:
                    break
                time.sleep(config.WORKER_POLL_SECONDS)
        return self.stats

    # -- schedules ----------------------------------------------------------

    def tick_schedules(self, now: datetime | None = None) -> list[str]:
        """Enqueue any cron job whose window has come round.

        Coalesced, so a worker that has been down for a day enqueues one nightly
        verify rather than one per hour it missed. `croniter` is used when it is
        installed and a plain hour/day check when it is not — a scheduled verify
        should not be the thing that makes the worker refuse to start.
        """
        now = now or datetime.now(timezone.utc)
        enqueued: list[str] = []
        for job_type, expression in (
            (schema.JOB_VERIFY_NIGHTLY, config.CRON_VERIFY_NIGHTLY),
            (schema.JOB_GC_WEEKLY, config.CRON_GC_WEEKLY),
            (schema.JOB_HEALTH_ALERT, config.CRON_HEALTH_ALERT),
            (schema.JOB_VOCAB_STALENESS, config.CRON_VOCAB_STALENESS),
        ):
            if self._cron_due(expression, now):
                if self.queue.enqueue(job_type, {"scheduled_for": now.isoformat()}, coalesce=True):
                    enqueued.append(job_type)
        return enqueued

    @staticmethod
    def _cron_due(expression: str, now: datetime) -> bool:
        try:
            from croniter import croniter

            # did the expression fire within the last minute?
            previous = croniter(expression, now).get_prev(datetime)
            return (now - previous).total_seconds() < 60
        except ImportError:
            minute, hour, _dom, _month, dow = expression.split()
            if now.minute != int(minute) or now.hour != int(hour):
                return False
            return dow == "*" or now.weekday() == (int(dow) - 1) % 7


# --------------------------------------------------------------------------- cli


def worker_command(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="bridge worker", description="run a job worker")
    parser.add_argument(
        "--partition", default="0/1",
        help="this worker's share of the queue, as n/N (default 0/1 — take everything)",
    )
    parser.add_argument("--types", help="comma-separated job types to handle")
    parser.add_argument("--max-jobs", type=int, help="stop after this many jobs")
    parser.add_argument("--once", action="store_true", help="exit when the queue is empty")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        part, total = (int(x) for x in args.partition.split("/"))
    except ValueError:
        print(f"bridge worker: --partition must look like 0/4, got {args.partition!r}")
        return 2
    if not 0 <= part < total:
        print(f"bridge worker: partition {part} is outside 0..{total - 1}")
        return 2

    engine = get_engine()
    create_all(engine)
    worker = Worker(
        engine, partition=part, partitions=total,
        types=args.types.split(",") if args.types else None,
    )
    if not args.json:
        print(f"worker {worker.id} handling partition {part} of {total}")
    stats = worker.run_forever(max_jobs=args.max_jobs, idle_exit=args.once)
    print(json.dumps(stats.as_dict(), indent=2) if args.json else f"  {stats.as_dict()}")
    return 0
