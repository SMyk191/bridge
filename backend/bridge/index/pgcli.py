"""`bridge index`, `bridge snapshots`, `bridge verify` — the Postgres index's CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config
from ..db import create_all, get_engine, keys
from ..db.repo import IndexRepo
from . import pgindex


def _repo(engine=None) -> IndexRepo:
    engine = engine or get_engine()
    create_all(engine)
    return IndexRepo(engine)


# --------------------------------------------------------------------------- index


def index_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bridge index", description="index a repository")
    parser.add_argument("repo", type=Path, help="path to the repository working tree")
    parser.add_argument("--sha", help="commit to index (default: HEAD)")
    parser.add_argument(
        "--from-sha",
        help="index incrementally from this commit (default: the latest snapshot for this repo)",
    )
    parser.add_argument("--full", action="store_true", help="ignore any previous snapshot")
    parser.add_argument("--no-embed", action="store_true", help="skip embeddings")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    repo_root = args.repo.resolve()
    if not repo_root.is_dir():
        print(f"bridge index: not a directory: {repo_root}", file=sys.stderr)
        return 2

    repo = _repo()
    sha = args.sha or pgindex.head_sha(repo_root)
    previous = None
    if not args.full:
        previous = args.from_sha
        if previous is None:
            latest = repo.latest_snapshot(repo_root.name)
            # Re-indexing the same commit is a full pass, not a no-op diff
            # against itself — that would report "nothing changed" for a
            # snapshot that may never have been written.
            if latest and latest["sha"] != sha:
                previous = latest["sha"]

    report = pgindex.index_repo(
        repo_root, repo, sha=sha, previous_sha=previous, embed=not args.no_embed,
        progress=None if args.json else (lambda message: print(f"  {message}")),
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    print(format_report(report))
    return 0


def format_report(report: pgindex.IndexReport) -> str:
    lines = [
        "=" * 78,
        f"INDEX  {report.repo}@{report.sha[:12]}  ({report.mode})",
        "=" * 78,
        f"  files       {report.files_indexed} indexed of {report.files_seen} tracked",
    ]
    if report.mode == "incremental":
        lines.append(
            f"              +{report.files_added} added  ~{report.files_modified} modified  "
            f"-{report.files_deleted} deleted  ->{report.files_renamed} renamed"
        )
    lines += [
        "",
        "  NEW THIS RUN  (everything else was already content-addressed)",
        f"    blobs        {report.blobs_new}",
        f"    chunks       {report.chunks_new}",
        f"    descriptions {report.descriptions_scheduled} scheduled, "
        f"{report.descriptions_written} written "
        f"({report.descriptions_from_template} from template, no model)",
        f"    vectors      {report.vectors_new}",
        f"    edges        {report.edges_new}"
        + (f"  ({report.edges_dropped} dropped)" if report.edges_dropped else ""),
    ]
    if report.tiers:
        total = sum(report.tiers.values()) or 1
        lines += ["", "  TIERS"]
        for tier in config.TIERS:
            count = report.tiers.get(tier, 0)
            lines.append(f"    {tier}  {count:>5}  {count / total:>5.1%}")
    if report.excluded:
        lines += ["", f"  EXCLUDED  ({sum(report.excluded.values())} files)"]
        for reason, count in sorted(report.excluded.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {count:>5}  {reason}")
    if report.parse_failures:
        lines += ["", f"  PARSE FAILURES  {len(report.parse_failures)}"]
        for path in report.parse_failures[:5]:
            lines.append(f"    {path}")
    lines += ["", f"  {report.elapsed_s:.2f}s", "=" * 78]
    return "\n".join(lines)


# --------------------------------------------------------------------------- snapshots


def snapshots_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bridge snapshots", description="list snapshots")
    parser.add_argument("--repo", help="only this repository")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo = _repo()
    rows = repo.snapshots(args.repo, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if not rows:
        print("no snapshots yet — run `bridge index <repo>`")
        return 0

    environments = {}
    for env in repo.environments(args.repo):
        environments.setdefault((env["repo"], env["sha"]), []).append(env["env"])

    print(f"{'repo':22s} {'sha':12s} {'created':20s} {'chunks':>7s} {'descr':>7s}  env")
    print("-" * 88)
    for row in rows:
        stats = row.get("stats") or {}
        new = stats.get("new") or {}
        envs = ",".join(environments.get((row["repo"], row["sha"]), []))
        print(
            f"{row['repo'][:22]:22s} {row['sha'][:12]:12s} "
            f"{str(row['created_at'])[:19]:20s} "
            f"{new.get('chunks', 0):>7} {new.get('descriptions_written', 0):>7}  {envs}"
        )
    return 0


# --------------------------------------------------------------------------- verify


def verify_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bridge verify",
        description="re-hash every blob in a snapshot and report drift",
    )
    parser.add_argument("repo", type=Path, help="path to the repository working tree")
    parser.add_argument("--sha", help="snapshot to verify (default: the latest for this repo)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_root = args.repo.resolve()
    repo = _repo()
    repo_name = repo_root.name

    snapshot = (
        repo.snapshot_by_sha(repo_name, args.sha) if args.sha else repo.latest_snapshot(repo_name)
    )
    if snapshot is None:
        print(f"bridge verify: no snapshot for {repo_name}" + (f"@{args.sha}" if args.sha else ""),
              file=sys.stderr)
        return 2

    report = pgindex.verify_snapshot(repo_root, repo, snapshot["id"])
    payload = {
        "repo": repo_name,
        "sha": snapshot["sha"],
        "checked": report.checked,
        "drifted": {p: {"stored": a, "actual": b} for p, (a, b) in report.drifted.items()},
        "missing": report.missing,
        "orphans": report.orphans,
        "clean": report.clean,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if report.clean else 1

    print(f"verify {repo_name}@{snapshot['sha'][:12]}: {report.checked} blobs re-hashed")
    if report.clean:
        print("  zero drift — every stored hash matches the bytes on disk")
        return 0
    for path, (stored, actual) in report.drifted.items():
        print(f"  DRIFT   {path}\n          stored {stored[:16]}  actual {actual[:16]}")
    for path in report.missing:
        print(f"  MISSING {path}")
    for path in report.orphans:
        print(f"  ORPHAN  {path} (snapshot references a blob that was never stored)")
    return 1
