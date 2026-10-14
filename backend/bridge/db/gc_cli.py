"""`bridge gc` — the retention sweep, as a command you can run before you trust it.

Dry run by default, and the dry run is the *same transaction* as the real one,
rolled back instead of committed. So `bridge gc` and `bridge gc --apply` do not
merely agree about what would be deleted — the first one actually deleted it and
then changed its mind, which is the only kind of rehearsal worth having for
something irreversible.
"""

from __future__ import annotations

import argparse
import json

from . import retention
from .engine import create_all, get_engine


def gc_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bridge gc",
        description="delete index artifacts no retained snapshot can reach",
    )
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    engine = get_engine()
    create_all(engine)
    report = retention.collect(engine, dry_run=not args.apply)
    payload = report.as_dict()

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    mode = "DRY RUN — nothing was kept deleted" if report.dry_run else "APPLIED"
    print(f"bridge gc  [{mode}]")
    kept = payload["retention"]["kept"]
    dropped = payload["retention"]["dropped"]
    print(f"  snapshots: {kept} retained, {dropped} collected")
    for snapshot in payload["retention"]["dropped_snapshots"][:20]:
        print(f"    - {snapshot}")
    reasons: dict[str, int] = {}
    for entries in payload["retention"]["reasons"].values():
        for reason in entries:
            reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"    kept {count:>4}  because {reason}")
    print(f"\n  {'table':<20} {'before':>10} {'reclaimed':>10}")
    for table, before in sorted(payload["examined"].items()):
        print(f"  {table:<20} {before:>10} {payload['reclaimed'].get(table, 0):>10}")
    print(f"\n  {payload['reclaimed_total']} row(s) reclaimed in total")
    if report.dry_run:
        print("  re-run with --apply to keep it.")
    return 0
