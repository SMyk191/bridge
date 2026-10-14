"""Git history per file: churn, recency, and a module-level owner.

Answers "is this area hot, and who should be in the room?" without ever naming a
person. The owner is a **directory** — `owner`, `vet`, `system` — because that is
the unit a product conversation actually routes on, and because a module survives
the person who last touched it.

No author names, emails, or per-person counts are read into the graph or written
to any output. `git log` is invoked with `--format` fields that carry none.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config


@dataclass(slots=True)
class FileHistory:
    path: str
    commits_all_time: int = 0
    commits_recent: int = 0
    last_changed: str | None = None  # ISO date
    days_since_change: int | None = None
    module: str = ""

    def as_attrs(self) -> dict:
        return {
            "commits_all_time": self.commits_all_time,
            "commits_recent": self.commits_recent,
            "recent_window_days": config.HISTORY_RECENT_DAYS,
            "last_changed": self.last_changed,
            "days_since_change": self.days_since_change,
            "module": self.module,
        }


@dataclass(slots=True)
class HistoryResult:
    files: dict[str, FileHistory] = field(default_factory=dict)
    modules: dict[str, dict] = field(default_factory=dict)
    elapsed: float = 0.0
    head_date: str | None = None

    @property
    def hottest(self) -> list[FileHistory]:
        return sorted(
            self.files.values(), key=lambda f: (-f.commits_recent, -f.commits_all_time, f.path)
        )


def module_of(path: str) -> str:
    """The directory a file lives in — the unit an owner is expressed at."""
    parent = Path(path).parent
    return parent.name or str(parent)


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def run(repo_root: Path, paths: list[str], *, recent_days: int = config.HISTORY_RECENT_DAYS,
        progress=None) -> HistoryResult:
    """Collect per-file history for ``paths`` in two `git log` calls, not one per file."""
    started = time.perf_counter()
    result = HistoryResult()
    wanted = set(paths)
    for path in paths:
        result.files[path] = FileHistory(path=path, module=module_of(path))

    # One walk of the whole history: a commit boundary, then its files.
    # %x00 delimits commits so a filename can never be mistaken for a header.
    log = _git(
        repo_root,
        "log",
        "--name-only",
        "--no-renames",
        "--pretty=format:%x00%H|%cI",
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    committed_at: datetime | None = None
    newest: datetime | None = None

    for block in log.split("\x00"):
        block = block.strip("\n")
        if not block:
            continue
        header, _, files = block.partition("\n")
        if "|" not in header:
            continue
        _sha, _, iso = header.partition("|")
        try:
            committed_at = datetime.fromisoformat(iso.strip())
        except ValueError:
            continue
        if newest is None or committed_at > newest:
            newest = committed_at
        is_recent = committed_at >= cutoff

        for name in files.splitlines():
            name = name.strip()
            if not name or name not in wanted:
                continue
            entry = result.files[name]
            entry.commits_all_time += 1
            if is_recent:
                entry.commits_recent += 1
            if entry.last_changed is None:  # log is newest-first
                entry.last_changed = committed_at.date().isoformat()
                entry.days_since_change = (
                    datetime.now(timezone.utc) - committed_at
                ).days

    result.head_date = newest.date().isoformat() if newest else None

    # Module rollup — the owner unit. Still no individuals.
    modules: dict[str, dict] = {}
    for entry in result.files.values():
        bucket = modules.setdefault(
            entry.module,
            {"module": entry.module, "files": 0, "commits_all_time": 0, "commits_recent": 0,
             "last_changed": None},
        )
        bucket["files"] += 1
        bucket["commits_all_time"] += entry.commits_all_time
        bucket["commits_recent"] += entry.commits_recent
        if entry.last_changed and (
            bucket["last_changed"] is None or entry.last_changed > bucket["last_changed"]
        ):
            bucket["last_changed"] = entry.last_changed
    result.modules = modules

    if progress is not None:
        progress(f"history: {len(result.files)} files across {len(modules)} modules")

    result.elapsed = time.perf_counter() - started
    return result


def apply_to_graph(store, history: HistoryResult) -> int:
    """Write history onto file nodes, and propagate to every node in that file.

    A method's churn is its file's churn — Bridge answers questions at the level
    of "this area moves a lot", not per-line blame.
    """
    import json

    updated = 0
    by_path = {path: entry for path, entry in history.files.items()}
    for row in store.conn.execute("SELECT id, path, attrs FROM nodes"):
        entry = by_path.get(row["path"])
        if entry is None:
            continue
        attrs = json.loads(row["attrs"])
        attrs["history"] = entry.as_attrs()
        store.conn.execute(
            "UPDATE nodes SET attrs = ? WHERE id = ?",
            (json.dumps(attrs, sort_keys=True), row["id"]),
        )
        updated += 1
    store.conn.commit()
    return updated


def for_path(store, path: str) -> dict | None:
    """Read back the history attrs recorded for a file."""
    import json

    row = store.conn.execute(
        "SELECT attrs FROM nodes WHERE path = ? AND kind = 'file' LIMIT 1", (path,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["attrs"]).get("history")
