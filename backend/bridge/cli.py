"""``bridge`` console entry point — a thin dispatcher over the subcommand modules."""

from __future__ import annotations

import sys

USAGE = """usage: bridge <command> [options]

commands:
  index <repo-path>     index a repository into Postgres (incremental after the first run)
  snapshots             list snapshots, newest first
  verify <repo-path>    re-hash every blob in a snapshot and report drift
  worker                run a job worker: --partition n/N, --once, --max-jobs
  simulate-push <repo>  enqueue an index_commit as though a push arrived
  gc [--apply]          retention sweep: report, or delete, unreachable artifacts
  shadow-index <repo>   describe a sample at a new --prompt-version, adopting nothing
  index-sqlite <repo>   the legacy SQLite/LanceDB index (phase 1-D)

The database is chosen by BRIDGE_DATABASE_URL, then config.DATABASE_URL, then
`embedded://` (a PostgreSQL 16 started from the venv). See bridge/db/engine.py.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]

    if command == "index":
        from .index.pgcli import index_command

        return index_command(rest)
    if command == "snapshots":
        from .index.pgcli import snapshots_command

        return snapshots_command(rest)
    if command == "verify":
        from .index.pgcli import verify_command

        return verify_command(rest)
    if command == "worker":
        from .work.worker import worker_command

        return worker_command(rest)
    if command == "simulate-push":
        from .work.simulate import simulate_push_command

        return simulate_push_command(rest)
    if command == "gc":
        from .db.gc_cli import gc_command

        return gc_command(rest)
    if command == "shadow-index":
        from .index.shadow import shadow_index_command

        return shadow_index_command(rest)
    if command == "index-sqlite":
        from .index.build import main as index_main

        return index_main(rest)

    print(f"bridge: unknown command {command!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
