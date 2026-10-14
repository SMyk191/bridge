"""Phase F's four claims, against a real Postgres queue.

    .venv/bin/python ../eval/check_workers.py

  1. five rapid pushes cost at most two index passes  (coalescing)
  2. /health/index answers with real lag and coverage  (observability)
  3. describe_backfill picks the expensive gaps first  (priority order)
  4. N workers partition the queue without overlap     (no double work)

Exit code is 0 only if all four hold.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from bridge.db import create_all, get_engine, keys, schema  # noqa: E402
from bridge.db.repo import IndexRepo  # noqa: E402
from bridge.index import pgindex  # noqa: E402
from bridge.work.health import index_health  # noqa: E402
from bridge.work.queue import JobQueue, RepoLock  # noqa: E402
from bridge.work.worker import Worker, backfill_candidates  # noqa: E402

SOURCE_REPO = Path("/Users/samyak/bridge/data/repos/spring-petclinic")


def reset(engine) -> None:
    with engine.begin() as connection:
        for table in ("jobs", "snapshot_files", "snapshots", "vectors", "descriptions",
                      "edges", "references", "chunks", "blobs", "projects", "environments"):
            connection.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))


def main() -> int:
    engine = get_engine()
    create_all(engine)
    reset(engine)
    queue = JobQueue(engine)
    repo = IndexRepo(engine)
    failures: list[str] = []

    workspace = Path(tempfile.mkdtemp(prefix="bridge-phase-f-"))
    root = workspace / "spring-petclinic"
    shutil.copytree(SOURCE_REPO, root, symlinks=True)
    repo_name = root.name

    print("=" * 92)
    print("PHASE F — queue, workers, coalescing, health")
    print("=" * 92)

    # ------------------------------------------------------------------ (1)
    print("\n(1) five rapid pushes -> at most two index passes")
    for index in range(5):
        queue.enqueue(
            schema.JOB_INDEX_COMMIT,
            {"sha": f"push{index}", "source": "simulate-push"},
            repo=repo_name, coalesce=True,
        )
    depth = queue.depth(repo_name)
    print(f"  after 5 pushes            queued={depth['queued']}")
    if depth["queued"] > 2:
        failures.append(f"(1) 5 pushes left {depth['queued']} queued jobs, expected <= 2")

    worker = Worker(engine, partition=0, partitions=1)
    # Point the job at the throwaway tree so the handler can actually index.
    # Done with the SQLAlchemy construct rather than raw SQL: `::jsonb` and
    # `:param` are the same character to text(), which turns a cast into a
    # missing bind parameter.
    from sqlalchemy import select, update

    with engine.begin() as connection:
        for row in connection.execute(
            select(schema.jobs.c.id, schema.jobs.c.payload).where(schema.jobs.c.repo == repo_name)
        ).mappings():
            connection.execute(
                update(schema.jobs)
                .where(schema.jobs.c.id == row["id"])
                .values(payload={**(row["payload"] or {}), "root_path": str(root)})
            )
    passes = 0
    while worker.run_one():
        passes += 1
    snapshots = repo.snapshots(repo_name)
    print(f"  worker drained the queue  jobs_run={passes}  snapshots={len(snapshots)}")
    if len(snapshots) > 2:
        failures.append(f"(1) 5 pushes produced {len(snapshots)} snapshots, expected <= 2")
    print(f"      -> 5 pushes became {len(snapshots)} index pass(es)")

    # the repo lock is what stops two workers indexing one repo at once
    lock_a, lock_b = RepoLock(engine, repo_name), RepoLock(engine, repo_name)
    got_a, got_b = lock_a.acquire(), lock_b.acquire()
    print(f"  repo lock is exclusive    first={got_a} second={got_b}")
    if not got_a or got_b:
        failures.append("(1) two workers both acquired the same repo lock")
    lock_a.release()
    lock_b.release()

    # ------------------------------------------------------------------ (2)
    print("\n(2) /health/index")
    health = index_health(engine, repo_root_for=lambda name: root)
    entry = health["repos"].get(repo_name, {})
    print(json.dumps({
        "latest_sha": entry.get("latest_sha", "")[:12],
        "lag": entry.get("lag"),
        "stale": entry.get("stale"),
        "stale_reasons": entry.get("stale_reasons"),
        "tiers": entry.get("tiers"),
        "description_coverage": entry.get("description_coverage"),
        "parse_failure_rate": entry.get("parse_failure_rate"),
        "last_run": entry.get("last_run"),
        "queue": entry.get("queue"),
    }, indent=2))
    for required in ("lag", "description_coverage", "tiers", "stale", "last_run", "queue"):
        if required not in entry:
            failures.append(f"(2) /health/index is missing {required!r}")
    if entry.get("lag", {}).get("commits") != 0:
        failures.append(f"(2) freshly indexed repo reports lag {entry.get('lag')}")

    # ------------------------------------------------------------------ (3)
    print("\n(3) describe_backfill picks the expensive gaps first")
    snapshot = repo.latest_snapshot(repo_name)
    # nothing is described for this model, so everything is a candidate
    candidates = backfill_candidates(repo, snapshot["id"], "some-real-model", limit=10)
    print(f"  {'rank':4s} {'degree':>6s}  chunk")
    for rank, candidate in enumerate(candidates, start=1):
        print(f"  {rank:<4} {candidate['degree']:>6}  {candidate['qualified_name'][:70]}")
    degrees = [c["degree"] for c in candidates]
    if degrees != sorted(degrees, reverse=True):
        failures.append(f"(3) backfill order is not by descending degree: {degrees}")
    if not degrees or degrees[0] == 0:
        failures.append("(3) the highest-priority candidate has degree 0 — ordering did nothing")

    # ------------------------------------------------------------------ (4)
    print("\n(4) four workers partition the queue with no overlap")
    reset(engine)
    for index in range(40):
        queue.enqueue(schema.JOB_GC_WEEKLY, {"n": index}, repo=f"repo{index % 3}")
    claimed: dict[int, list[int]] = {}
    for partition in range(4):
        worker = Worker(engine, partition=partition, partitions=4, types=[schema.JOB_GC_WEEKLY])
        taken = []
        while True:
            job = worker.queue.claim(
                worker.id, types=[schema.JOB_GC_WEEKLY], partition=partition, partitions=4
            )
            if job is None:
                break
            taken.append(job.id)
        claimed[partition] = taken
        print(f"  partition {partition}/4  claimed {len(taken):>3} job(s)")

    everything = [job_id for taken in claimed.values() for job_id in taken]
    if len(everything) != len(set(everything)):
        failures.append("(4) two partitions claimed the same job")
    if len(everything) != 40:
        failures.append(f"(4) 40 jobs enqueued, {len(everything)} claimed across all partitions")
    for partition, taken in claimed.items():
        wrong = [job_id for job_id in taken if job_id % 4 != partition]
        if wrong:
            failures.append(f"(4) partition {partition} claimed jobs outside its share: {wrong[:3]}")
    print(f"      -> {len(everything)} jobs claimed exactly once, none shared")

    print("\n" + "=" * 92)
    if failures:
        print("FAILURES")
        for line in failures:
            print("  -", line)
        return 1
    print("all four hold.")
    print("=" * 92)
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
