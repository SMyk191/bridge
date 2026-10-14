"""`GET /health/index` — is the index actually current, and can you trust it?

`/health` already answers "is the process up". This answers the question that
matters once indexing is asynchronous: **how far behind is the index, and how
much of it is real?** Both halves matter, and neither is visible from the other:

* an index that is 3 commits behind but fully described is usable;
* an index that is exactly at HEAD with 40% of its T2 chunks undescribed will
  answer questions confidently and wrongly, because an undescribed chunk is
  invisible to semantic search however cleanly it parsed.

So the payload reports lag two ways (commits AND minutes — a repo with one commit
a week and one with a hundred a day need different readings of "3 behind"),
description coverage per tier, the queue depth, the last run's cost and duration,
the parse-failure rate, and a stale badge with the reason it is stale.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from .. import config
from ..db import schema
from ..db.repo import IndexRepo
from .queue import JobQueue


def _commits_between(root: Path, old: str, new: str) -> int | None:
    """How many commits the index is behind. ``None`` when git cannot say."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", f"{old}..{new}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _head_of(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def index_health(engine: Engine, repo_root_for=None) -> dict[str, Any]:
    """Per-repo index health. Never raises: a health endpoint that 500s is useless.

    Anything it cannot determine (the working tree is gone, git will not answer)
    comes back as `null` with the reason recorded, rather than as a number that
    looks fine.
    """
    from .worker import repo_path_for

    repo_root_for = repo_root_for or repo_path_for
    repo = IndexRepo(engine)
    queue = JobQueue(engine)
    now = datetime.now(timezone.utc)

    with engine.connect() as connection:
        repos = [
            row[0]
            for row in connection.execute(
                select(schema.snapshots.c.repo).distinct().order_by(schema.snapshots.c.repo)
            )
        ]

    environments: dict[str, dict[str, str]] = {}
    for row in repo.environments():
        environments.setdefault(row["repo"], {})[row["env"]] = row["sha"]

    payload: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "queue": queue.depth(),
        # Phase G. Both of these are index-wide rather than per-repo: the vector
        # store is partitioned by project (which spans repos) and the description
        # version is global, so hanging them off one repo would be a lie.
        "vectors": _vector_health(engine),
        "descriptions": _version_health(engine),
        "repos": {},
    }

    for repo_name in repos:
        latest = repo.latest_snapshot(repo_name)
        if latest is None:
            continue
        stats = latest.get("stats") or {}
        indexed_at = latest.get("created_at")
        if isinstance(indexed_at, datetime) and indexed_at.tzinfo is None:
            indexed_at = indexed_at.replace(tzinfo=timezone.utc)

        age_minutes = (
            round((now - indexed_at).total_seconds() / 60, 1)
            if isinstance(indexed_at, datetime)
            else None
        )

        root = Path(repo_root_for(repo_name))
        head = _head_of(root) if root.is_dir() else None
        lag_commits = (
            _commits_between(root, latest["sha"], head)
            if head and head != latest["sha"]
            else (0 if head else None)
        )

        coverage = repo.description_coverage(latest["id"])
        tiers = repo.tier_distribution(latest["id"])
        files = stats.get("files") or {}
        indexed_files = files.get("indexed") or 0
        parse_failures = len(stats.get("parse_failures") or [])

        # A repo is stale for a REASON, and the reason is what a reader acts on:
        # "3 commits behind" is a queue problem, "62 minutes old" may be fine for
        # a repo nobody has pushed to.
        stale_reasons = []
        if lag_commits:
            stale_reasons.append(f"{lag_commits} commit(s) behind HEAD")
        if age_minutes is not None and age_minutes > config.HEALTH_STALE_AFTER_MINUTES:
            stale_reasons.append(f"last indexed {age_minutes:.0f} minutes ago")
        if head is None:
            stale_reasons.append("working tree not readable — lag cannot be determined")

        payload["repos"][repo_name] = {
            "latest_sha": latest["sha"],
            "head_sha": head,
            "indexed_at": indexed_at.isoformat() if isinstance(indexed_at, datetime) else None,
            "environments": environments.get(repo_name, {}),
            "lag": {"commits": lag_commits, "minutes": age_minutes},
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "last_run": {
                "mode": stats.get("mode"),
                "duration_s": stats.get("elapsed_s"),
                # descriptions are the only thing an index run spends money on
                "descriptions_written": (stats.get("new") or {}).get("descriptions_written", 0),
                "cost_usd": (stats.get("new") or {}).get("cost_usd", 0.0),
            },
            "parse_failure_rate": (
                round(parse_failures / indexed_files, 4) if indexed_files else 0.0
            ),
            "unresolved_rate": stats.get("unresolved_rate", 0.0),
            "tiers": tiers,
            "description_coverage": {
                tier: {
                    **numbers,
                    "pct": (
                        round(numbers["described"] / numbers["total"], 4)
                        if numbers["total"] else 1.0
                    ),
                }
                for tier, numbers in sorted(coverage.items())
            },
            "queue": queue.depth(repo_name),
        }
    return payload


def _vector_health(engine: Engine) -> dict[str, Any]:
    """Partition sizes and what the engine can actually do. Never raises."""
    from ..db.vectorstore import PgVectorStore

    try:
        store = PgVectorStore(engine)
        return {"capabilities": store.capabilities(), "partitions": store.partitions()}
    except Exception as error:  # pragma: no cover - a health endpoint must not 500
        return {"error": f"{type(error).__name__}: {error}"}


def _version_health(engine: Engine) -> dict[str, Any]:
    """How far the lazy prompt-version migration has got. Never raises."""
    from ..db import versions

    try:
        return versions.coverage(engine)
    except Exception as error:  # pragma: no cover
        return {"error": f"{type(error).__name__}: {error}"}
