"""`bridge simulate-push` — the webhook, without the webhook.

Coalescing is the kind of behaviour that only shows itself under a burst, and a
burst is exactly what is hard to produce by hand. This enqueues `index_commit`
jobs the same way `POST /webhooks/github` does — same type, same payload shape,
same `coalesce=True` — so what gets exercised is the real path and not a
test-only one.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..db import create_all, get_engine, schema
from ..db.repo import IndexRepo
from .queue import JobQueue


def simulate_push_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bridge simulate-push",
        description="enqueue index_commit jobs as though pushes had arrived",
    )
    parser.add_argument("repo", help="repository name (or a path to its working tree)")
    parser.add_argument("--count", type=int, default=1, help="how many pushes to simulate")
    parser.add_argument("--sha", action="append", help="explicit sha (repeatable)")
    parser.add_argument("--interval", type=float, default=0.0, help="seconds between pushes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_name = Path(args.repo).name
    engine = get_engine()
    create_all(engine)
    queue = JobQueue(engine)

    shas = args.sha or [f"simulated{index:04d}" for index in range(args.count)]
    job_ids: list[int | None] = []
    for index, sha in enumerate(shas):
        job_ids.append(
            queue.enqueue(
                schema.JOB_INDEX_COMMIT,
                {"sha": sha, "source": "simulate-push", "push": index},
                repo=repo_name,
                coalesce=True,
            )
        )
        if args.interval and index < len(shas) - 1:
            time.sleep(args.interval)

    distinct = sorted({job_id for job_id in job_ids if job_id is not None})
    payload = {
        "repo": repo_name,
        "pushes": len(shas),
        "job_ids": job_ids,
        "distinct_jobs": len(distinct),
        "coalesced_into": distinct,
        "queue": queue.depth(repo_name),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"{len(shas)} push(es) for {repo_name} -> {len(distinct)} queued job(s) "
            f"{distinct}\n  queue: {payload['queue']}"
        )
    return 0
