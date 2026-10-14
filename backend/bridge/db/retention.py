"""What to keep, and what a weekly GC is allowed to delete.

Phase F's `gc_weekly` counted unreferenced rows and deleted nothing, deliberately:
a content-addressed row is shared by every snapshot that ever contained it, and
deleting one because *today's* snapshot does not point at it throws away the
history that makes an old sha answerable. That is still the right instinct — it
was just too blunt. The fix is not "delete more bravely", it is to decide which
snapshots are worth keeping and then delete only what **no retained snapshot
references**.

A snapshot survives if ANY of these hold, and the GC reports which:

    an environment points at it      staging/production must stay answerable
    a git tag points at it           releases are what people ask about later
    among the last N on the branch   recent history is the working set
    indexed within N days            nothing new is ever collected

Everything else is fair game — but "fair game" still means *reachability*, not
age: a blob from a deleted snapshot that a retained snapshot also contains is
retained, because content-addressing means they are the same row.

Two things are never collected. `runs` and `feedback` hold **copied evidence** —
an answer's citations and its impact map are denormalised into the run's JSON, so
a run stays readable and auditable even after the snapshot it was computed
against is gone. That is the whole reason the answer is copied rather than
joined.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, delete, func, select

from .. import config
from . import schema


# --------------------------------------------------------------------------- git


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def tagged_shas(root: Path) -> set[str]:
    """Every commit a tag points at. Empty when git cannot be asked."""
    if not root.is_dir():
        return set()
    output = _git(root, "for-each-ref", "--format=%(objectname)", "refs/tags")
    return {line.strip() for line in output.splitlines() if line.strip()}


def default_branch_shas(root: Path, limit: int = 500) -> list[str]:
    """Commits on the default branch, newest first.

    Tries the remote HEAD first (what the host thinks the default is), then the
    conventional names. A repo with neither is not an error — it just means the
    "last N on the branch" rule cannot fire, and the other three still can.
    """
    if not root.is_dir():
        return []
    head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    candidates = [head.rsplit("/", 1)[-1]] if head else []
    candidates += list(config.RETENTION_DEFAULT_BRANCHES)
    for branch in candidates:
        if not branch:
            continue
        output = _git(root, "rev-list", f"--max-count={limit}", branch)
        shas = [line.strip() for line in output.splitlines() if line.strip()]
        if shas:
            return shas
    return []


# --------------------------------------------------------------------------- policy


@dataclass(slots=True)
class RetentionDecision:
    """Which snapshots stay, and the rule that saved each one."""

    keep: dict[str, list[str]] = field(default_factory=dict)   # snapshot_id -> reasons
    drop: list[str] = field(default_factory=list)              # snapshot_id
    #: snapshot_id -> (repo, sha), for readable reporting
    labels: dict[str, tuple[str, str]] = field(default_factory=dict)

    def retain(self, snapshot_id: str, reason: str) -> None:
        self.keep.setdefault(snapshot_id, [])
        if reason not in self.keep[snapshot_id]:
            self.keep[snapshot_id].append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept": len(self.keep),
            "dropped": len(self.drop),
            "reasons": {
                snapshot_id: reasons for snapshot_id, reasons in sorted(self.keep.items())
            },
            "dropped_snapshots": [
                f"{self.labels.get(s, ('?', '?'))[0]}@{self.labels.get(s, ('?', '?'))[1][:12]}"
                for s in self.drop
            ],
        }


def decide(engine: Engine, repo_root_for=None, now: datetime | None = None) -> RetentionDecision:
    """Apply the four rules across every repo with snapshots."""
    from ..work.worker import repo_path_for

    repo_root_for = repo_root_for or repo_path_for
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.RETENTION_KEEP_DAYS)
    decision = RetentionDecision()

    with engine.connect() as connection:
        snapshots = [
            dict(row)
            for row in connection.execute(
                select(schema.snapshots).order_by(
                    schema.snapshots.c.repo, schema.snapshots.c.created_at.desc()
                )
            ).mappings()
        ]
        environment_shas = {
            (row["repo"], row["sha"])
            for row in connection.execute(select(schema.environments)).mappings()
        }

    by_repo: dict[str, list[dict]] = {}
    for snapshot in snapshots:
        decision.labels[snapshot["id"]] = (snapshot["repo"], snapshot["sha"])
        by_repo.setdefault(snapshot["repo"], []).append(snapshot)

    for repo_name, rows in by_repo.items():
        root = Path(repo_root_for(repo_name))
        tags = tagged_shas(root)
        branch = default_branch_shas(root)
        # rank on the default branch: 0 is the tip
        rank = {sha: index for index, sha in enumerate(branch)}

        for snapshot in rows:
            sha = snapshot["sha"]
            created = snapshot.get("created_at")
            if isinstance(created, datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)

            if (repo_name, sha) in environment_shas:
                decision.retain(snapshot["id"], config.RETAIN_ENVIRONMENT)
            if sha in tags:
                decision.retain(snapshot["id"], config.RETAIN_TAGGED)
            position = rank.get(sha)
            if position is not None and position < config.RETENTION_KEEP_LAST_N:
                decision.retain(snapshot["id"], config.RETAIN_RECENT_N)
            if isinstance(created, datetime) and created >= cutoff:
                decision.retain(snapshot["id"], config.RETAIN_RECENT_DAYS)

            if snapshot["id"] not in decision.keep:
                decision.drop.append(snapshot["id"])
    return decision


# --------------------------------------------------------------------------- collection


@dataclass(slots=True)
class GcReport:
    dry_run: bool = True
    retention: dict[str, Any] = field(default_factory=dict)
    #: table -> rows that would be (or were) deleted
    reclaimed: dict[str, int] = field(default_factory=dict)
    #: rows examined, so "0 reclaimed" is distinguishable from "nothing looked at"
    examined: dict[str, int] = field(default_factory=dict)
    #: vector partitions that outlived their rows
    partitions_dropped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "retention": self.retention,
            "reclaimed": dict(sorted(self.reclaimed.items())),
            "reclaimed_total": sum(self.reclaimed.values()),
            "examined": dict(sorted(self.examined.items())),
            "partitions_dropped": self.partitions_dropped,
        }


def collect(
    engine: Engine, *, dry_run: bool = True, repo_root_for=None, now: datetime | None = None
) -> GcReport:
    """Delete every artifact no retained snapshot can reach.

    The order is the reverse of the reference direction, so nothing is ever
    orphaned mid-transaction: vectors before descriptions, snapshot_files before
    snapshots, edges/references before the chunks they name, chunks before blobs.

    It all runs in ONE transaction. A GC that half-ran would leave an index that
    looks intact and is not, which is worse than one that did nothing. A dry run
    is the same transaction rolled back rather than committed, so its counts are
    what really happened rather than an estimate of what might.
    """
    decision = decide(engine, repo_root_for=repo_root_for, now=now)
    report = GcReport(dry_run=dry_run, retention=decision.as_dict())
    doomed = list(decision.drop)

    connection = engine.connect()
    transaction = connection.begin()
    try:
        report.examined = _counts(connection)
        if not doomed:
            # "nothing to collect" and "the GC never ran" look identical without
            # the examined counts, so they are reported either way.
            transaction.rollback()
            return report

        retained_blobs = (
            select(schema.snapshot_files.c.blob_hash)
            .where(schema.snapshot_files.c.snapshot_id.notin_(doomed))
            .distinct()
        )
        dead_blobs = [
            row[0]
            for row in connection.execute(
                select(schema.blobs.c.blob_hash).where(
                    schema.blobs.c.blob_hash.notin_(retained_blobs)
                )
            )
        ]
        dead_chunks = (
            [
                row[0]
                for row in connection.execute(
                    select(schema.chunks.c.chunk_hash).where(
                        schema.chunks.c.blob_hash.in_(dead_blobs)
                    )
                )
            ]
            if dead_blobs
            else []
        )

        # A description dies only when NO surviving chunk shares its code_hash.
        # Content-addressing again: two snapshots hold different chunk rows for
        # the same code, and the description belongs to the code, not the row.
        if dead_chunks:
            live_codes = (
                select(schema.chunks.c.code_hash)
                .where(schema.chunks.c.chunk_hash.notin_(dead_chunks))
                .distinct()
            )
            dead_desc = [
                row[0]
                for row in connection.execute(
                    select(schema.descriptions.c.desc_key).where(
                        schema.descriptions.c.code_hash.notin_(live_codes)
                    )
                )
            ]
        else:
            dead_desc = []

        if dead_desc:
            report.reclaimed["vectors"] = _delete(
                connection, schema.vectors, schema.vectors.c.desc_key.in_(dead_desc)
            )
            report.reclaimed["descriptions"] = _delete(
                connection, schema.descriptions, schema.descriptions.c.desc_key.in_(dead_desc)
            )
        if dead_chunks:
            report.reclaimed["edges"] = _delete(
                connection,
                schema.edges,
                schema.edges.c.src.in_(dead_chunks) | schema.edges.c.dst.in_(dead_chunks),
            )
            report.reclaimed["references"] = _delete(
                connection, schema.references, schema.references.c.chunk_hash.in_(dead_chunks)
            )
        report.reclaimed["snapshot_files"] = _delete(
            connection, schema.snapshot_files, schema.snapshot_files.c.snapshot_id.in_(doomed)
        )
        report.reclaimed["snapshots"] = _delete(
            connection, schema.snapshots, schema.snapshots.c.id.in_(doomed)
        )
        if dead_chunks:
            report.reclaimed["chunks"] = _delete(
                connection, schema.chunks, schema.chunks.c.chunk_hash.in_(dead_chunks)
            )
        if dead_blobs:
            report.reclaimed["blobs"] = _delete(
                connection, schema.blobs, schema.blobs.c.blob_hash.in_(dead_blobs)
            )
        report.reclaimed["module_summaries"] = _collect_summaries(connection, set(dead_blobs))

        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    finally:
        connection.close()

    if not dry_run:
        # After the rows are gone, an empty partition is a relation and an HNSW
        # index that every plan still pays to consider. Outside the transaction
        # because it is DDL against a table the transaction just wrote to.
        from .vectorstore import PgVectorStore

        report.partitions_dropped = PgVectorStore(engine).drop_empty_partitions()
    return report


def _collect_summaries(connection, dead_blobs: set[str]) -> int:
    """Drop module summaries that describe content which no longer exists.

    A summary's `children_keys` are blob hashes at the leaves and child merkle
    keys above, so death propagates upward: a directory whose file is gone has
    the wrong merkle key, and so does every directory above it. Iterating to a
    fixpoint is how the upward propagation happens without needing a parent
    pointer the table does not have.
    """
    rows = {
        row["merkle_key"]: list(row["children_keys"] or [])
        for row in connection.execute(
            select(schema.module_summaries.c.merkle_key, schema.module_summaries.c.children_keys)
        ).mappings()
    }
    dead: set[str] = set(dead_blobs)
    while True:
        newly = {
            key for key, children in rows.items()
            if key not in dead and any(child in dead for child in children)
        }
        if not newly:
            break
        dead |= newly

    doomed = [key for key in rows if key in dead]
    if not doomed:
        return 0
    return _delete(connection, schema.module_summaries, schema.module_summaries.c.merkle_key.in_(doomed))


def _delete(connection, table, condition) -> int:
    return connection.execute(delete(table).where(condition)).rowcount or 0


def _counts(connection) -> dict[str, int]:
    return {
        table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
        for table in (
            schema.blobs, schema.chunks, schema.descriptions, schema.vectors,
            schema.edges, schema.references, schema.snapshots, schema.snapshot_files,
        )
    }
