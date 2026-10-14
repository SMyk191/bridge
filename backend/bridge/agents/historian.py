"""Historian — attach churn to the impacted files. No LLM.

Deterministic by design. "This file changed 30 times, 5 of them in the last 90
days" is a fact from `git log`; asking a model to restate it would add cost,
latency, and a chance of getting a number wrong.

Reads what `index/history.py` already wrote onto the graph nodes, so a run costs
one SQLite query rather than a git walk.
"""

from __future__ import annotations

import json

from .. import config
from ..index.graph import GraphStore
from .state import FileHistoryNote, HistoryReport, RunState, citation_path


def run(state: RunState, store: GraphStore) -> RunState:
    """Populate ``state.history`` for every file the impact map names."""
    paths: list[str] = []
    if state.impact_map is not None:
        paths = state.impact_map.paths()
    elif state.retrieval is not None:
        for chunk in state.retrieval.chunks:
            if chunk.path not in paths:
                paths.append(chunk.path)

    report = HistoryReport(window_days=config.HISTORY_RECENT_DAYS)
    if not paths:
        state.history = report
        return report_done(state, report)

    placeholders = ",".join("?" * len(paths))
    rows = store.conn.execute(
        f"SELECT DISTINCT path, attrs FROM nodes WHERE path IN ({placeholders})",
        tuple(paths),
    ).fetchall()

    seen: set[str] = set()
    for row in rows:
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        history = json.loads(row["attrs"]).get("history") or {}
        if not history:
            continue
        recent = int(history.get("commits_recent") or 0)
        note = FileHistoryNote(
            path=row["path"],
            commits_all_time=int(history.get("commits_all_time") or 0),
            commits_recent=recent,
            last_changed=history.get("last_changed"),
            module=history.get("module") or "",
            # "volatile" is a merge-conflict warning, not a quality judgement:
            # this area is moving under you while you work on it.
            volatile=recent >= config.VOLATILE_COMMITS_IN_WINDOW,
        )
        report.files.append(note)

    report.files.sort(key=lambda f: (-f.commits_recent, -f.commits_all_time, f.path))
    report.volatile = [f.path for f in report.files if f.volatile]
    report.modules = sorted({f.module for f in report.files if f.module})
    state.history = report
    return report_done(state, report)


def report_done(state: RunState, report: HistoryReport) -> RunState:
    state.history = report
    return state
