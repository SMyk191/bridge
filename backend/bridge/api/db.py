"""Feedback and vocabulary-proposal storage — the API's own write surface.

Bridge never writes to the target repository (CLAUDE.md's first line). These two
tables are not that: they are Bridge's own opinion about itself — did an answer
help, should a term be added to the vocabulary — kept in the same app-store file
as `runs` (`config.RUNS_DB`), never touching `data/index/` or the checked-out repo.

Each request opens and closes its own connection. These are small, infrequent
writes on a local single-process dev server; a per-request connection is the
simplest thing that is unconditionally correct, and it sidesteps the SQLite
thread-affinity trap that `RunStore` already had to work around once for the
orchestrator's `asyncio.to_thread` nodes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
  id         INTEGER PRIMARY KEY,
  run_id     TEXT NOT NULL,
  vote       TEXT NOT NULL CHECK (vote IN ('up', 'down')),
  note       TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_run ON feedback(run_id);

CREATE TABLE IF NOT EXISTS vocab_proposals (
  id         INTEGER PRIMARY KEY,
  term       TEXT NOT NULL,
  entities   TEXT NOT NULL,   -- JSON array of node ids
  note       TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending until approved by hand
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vocab_status ON vocab_proposals(status);
"""


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.RUNS_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def record_feedback(run_id: str, vote: str, note: str | None = None) -> dict[str, Any]:
    """Insert one feedback row. Caller has already checked ``run_id`` exists."""
    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO feedback(run_id, vote, note, created_at) "
            "VALUES(?, ?, ?, datetime('now'))",
            (run_id, vote, note),
        )
        connection.commit()
        return {
            "id": cursor.lastrowid,
            "run_id": run_id,
            "vote": vote,
            "note": note,
        }
    finally:
        connection.close()


def record_vocab_proposal(term: str, entities: list[str], note: str | None = None) -> dict[str, Any]:
    """Insert one proposal, `status='pending'`. Approval is a manual, out-of-band step."""
    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO vocab_proposals(term, entities, note, status, created_at) "
            "VALUES(?, ?, ?, 'pending', datetime('now'))",
            (term, json.dumps(entities), note),
        )
        connection.commit()
        return {
            "id": cursor.lastrowid,
            "term": term,
            "entities": entities,
            "note": note,
            "status": "pending",
        }
    finally:
        connection.close()


def list_vocab_proposals(status: str | None = None) -> list[dict[str, Any]]:
    """Newest first. `status=None` returns every status — the review queue
    needs to show what was already decided, not just what's pending."""
    connection = _connect()
    try:
        if status:
            rows = connection.execute(
                "SELECT * FROM vocab_proposals WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM vocab_proposals ORDER BY id DESC").fetchall()
        return [
            {**dict(row), "entities": json.loads(row["entities"])}
            for row in rows
        ]
    finally:
        connection.close()


def review_vocab_proposal(proposal_id: int, status: str) -> dict[str, Any] | None:
    """Approve or reject a pending proposal. Returns None if it doesn't exist.

    A human confirms before anything the vocab-staleness job proposes takes
    effect — the job writes rows, it never edits `config/vocab.*.yaml` itself
    (design law 7: org-specific vocabulary is a file a person owns).
    """
    connection = _connect()
    try:
        cursor = connection.execute(
            "UPDATE vocab_proposals SET status = ? WHERE id = ?", (status, proposal_id)
        )
        connection.commit()
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM vocab_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return {**dict(row), "entities": json.loads(row["entities"])}
    finally:
        connection.close()
