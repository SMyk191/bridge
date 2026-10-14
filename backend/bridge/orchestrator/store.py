"""Run persistence — `data/runs.sqlite`.

State is written after **every** node, not once at the end. A run that aborts on
budget or crashes mid-way still leaves everything it had established, which is
what makes the partial answer possible and the trace worth keeping (design law 6).

Threads are the follow-up mechanism: `latest_in_thread` gives the next run its
predecessor's impact map summary.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import config

#: Split from the rest of the schema on purpose. `repo`/`team` (Phase M) are
#: added to an EXISTING `runs.sqlite` by an ad hoc `ALTER TABLE` in `open()`,
#: because `CREATE TABLE IF NOT EXISTS` never adds a column to a table that's
#: already there — but the indexes that reference those columns can only be
#: created once they exist, so this table has to be created (or, on an old
#: file, migrated) BEFORE `SCHEMA` below runs.
RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  request     TEXT NOT NULL,
  sha         TEXT,
  lane        TEXT,
  status      TEXT NOT NULL,
  confidence  TEXT,
  cost_usd    REAL NOT NULL DEFAULT 0.0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  elapsed_s   REAL NOT NULL DEFAULT 0.0,
  citations   INTEGER NOT NULL DEFAULT 0,
  state       TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  repo        TEXT,
  team        TEXT NOT NULL DEFAULT 'default'
);
"""

#: Columns `RUNS_TABLE` may be missing on a file created before Phase M.
RUNS_TABLE_MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN repo TEXT",
    "ALTER TABLE runs ADD COLUMN team TEXT NOT NULL DEFAULT 'default'",
)

#: Columns `answer_cache` may be missing on a file created before the semantic
#: cache. Run only after `SCHEMA` (below) has ensured the table itself exists.
ANSWER_CACHE_MIGRATIONS = (
    "ALTER TABLE answer_cache ADD COLUMN lane TEXT",
    "ALTER TABLE answer_cache ADD COLUMN question_embedding TEXT",
)

SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_team_created ON runs(team, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_lane_created ON runs(lane, created_at);

-- Multi-vote disagreements a node recorded about itself — today only the
-- interpreter's intent vote (agents/interpreter.py). A standalone table rather
-- than something buried in each run's own JSON blob, because the point of
-- collecting these is to mine them ACROSS runs later: which phrasings are
-- still ambiguous at temperature 0, how often, and whether a prompt change
-- moves the number.
CREATE TABLE IF NOT EXISTS eval_candidates (
  id          INTEGER PRIMARY KEY,
  run_id      TEXT NOT NULL,
  node        TEXT NOT NULL,
  field       TEXT NOT NULL,
  candidates  TEXT NOT NULL,   -- JSON array of the raw vote values, in vote order
  chosen      TEXT NOT NULL,
  is_majority INTEGER NOT NULL DEFAULT 0,
  reason      TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_candidates_run ON eval_candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_candidates_field ON eval_candidates(node, field);

-- (snapshot_key, question_norm) -> the run_id whose answer can be replayed.
-- See orchestrator/cache.py for how both halves of the key are computed, and
-- config.py's "answer cache" section for exactly when this is and is not
-- consulted.
CREATE TABLE IF NOT EXISTS answer_cache (
  snapshot_key   TEXT NOT NULL,
  question_norm  TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (snapshot_key, question_norm)
);
CREATE INDEX IF NOT EXISTS idx_answer_cache_run ON answer_cache(run_id);
-- `lane`/`question_embedding` (Phase M semantic cache, orchestrator/cache.py)
-- are added to an existing file by ANSWER_CACHE_MIGRATIONS below, same reason
-- and same pattern as `runs.repo`/`runs.team`.

-- eval/rolling_quality.py's weekly sample of REAL traffic, rubric-scored
-- (eval/rubric.py) and trended. `run_id` is UNIQUE so a job that runs twice in
-- the same week (a retry, a manual re-trigger) cannot double-count a run into
-- its own trend line. `week` is an ISO year-week ('2026-W37') rather than a
-- date range, so "this week's mean" is a stable bucket to group and compare
-- across runs of the job, not a window that shifts under it.
CREATE TABLE IF NOT EXISTS quality_samples (
  id            INTEGER PRIMARY KEY,
  run_id        TEXT NOT NULL UNIQUE,
  lane          TEXT,
  correctness   INTEGER,
  completeness  INTEGER,
  actionability INTEGER,
  mean          REAL,
  scored        INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  week          TEXT NOT NULL,
  sampled_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_samples_week ON quality_samples(week);

-- security/acl.py. Every access decision, allowed or denied — a denial is
-- what makes this an audit log rather than a login log; "who tried to read
-- what, and was refused" is the row someone actually goes looking for.
CREATE TABLE IF NOT EXISTS access_audit (
  id         INTEGER PRIMARY KEY,
  principal  TEXT NOT NULL,
  repo       TEXT NOT NULL,
  question   TEXT NOT NULL,
  sha        TEXT,
  allowed    INTEGER NOT NULL,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_audit_repo ON access_audit(repo, created_at);
CREATE INDEX IF NOT EXISTS idx_access_audit_principal ON access_audit(principal, created_at);
"""


class RunStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str | Path | None = None) -> "RunStore":
        path = Path(path or config.RUNS_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(sqlite3.connect(path, check_same_thread=False))
        store.conn.executescript(RUNS_TABLE)
        store._migrate(RUNS_TABLE_MIGRATIONS)
        store.conn.executescript(SCHEMA)
        store._migrate(ANSWER_CACHE_MIGRATIONS)  # answer_cache exists only after SCHEMA runs
        return store

    def _migrate(self, statements: tuple[str, ...]) -> None:
        for statement in statements:
            try:
                self.conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    def save(self, state) -> None:
        """Upsert the whole run. Called after every node."""
        payload = state.as_dict()
        confidence = (
            state.impact_map.confidence
            if state.impact_map is not None
            else (state.retrieval.confidence if state.retrieval is not None else None)
        )
        self._upsert_run(
            run_id=state.run_id,
            thread_id=state.thread_id,
            request=state.request,
            sha=state.sha,
            lane=state.lane,
            status=state.status,
            confidence=confidence,
            cost_usd=state.usage.cost_usd,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
            elapsed_s=state.budget.elapsed,
            citations=len(state.citations),
            payload=payload,
            repo=state.env.get("repo"),
            team=state.env.get("team") or config.DEFAULT_TEAM,
        )

    def save_raw(self, payload: dict[str, Any]) -> None:
        """Upsert a run row from an already-serialized payload dict.

        Used only for a cache-served run (orchestrator/run.py::_serve_cached),
        which patches a handful of top-level fields onto a copy of a PREVIOUS
        run's persisted JSON rather than producing a fresh one by running the
        pipeline. There is no live `RunState` in that path — `save()` needs one
        (it calls `state.as_dict()`); this needs only the dict itself, which is
        already exactly the shape `save()` would have written.
        """
        from ..agents.state import citations_of

        usage = payload.get("usage") or {}
        impact = payload.get("impact_map") or {}
        confidence = impact.get("confidence") or (payload.get("retrieval") or {}).get("confidence")
        self._upsert_run(
            run_id=payload["run_id"],
            thread_id=payload["thread_id"],
            request=payload["request"],
            sha=payload.get("sha"),
            lane=payload.get("lane"),
            status=payload.get("status", "done"),
            confidence=confidence,
            cost_usd=usage.get("cost_usd", 0.0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            elapsed_s=(payload.get("budget") or {}).get("elapsed_s", 0.0),
            citations=len(citations_of(payload)),
            payload=payload,
            repo=(payload.get("env") or {}).get("repo"),
            team=(payload.get("env") or {}).get("team") or config.DEFAULT_TEAM,
        )

    def _upsert_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request: str,
        sha: str | None,
        lane: str | None,
        status: str,
        confidence: str | None,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        elapsed_s: float,
        citations: int,
        payload: dict[str, Any],
        repo: str | None = None,
        team: str = config.DEFAULT_TEAM,
    ) -> None:
        """The one INSERT ... ON CONFLICT both `save` and `save_raw` share.

        `repo`/`team` are written on INSERT only, never in the ON CONFLICT
        UPDATE clause — both are set once, from the request that started the
        run, and `save()` is called again after every later node with the same
        values it would just be echoing back.
        """
        try:
            self.conn.execute(
                "INSERT INTO runs(run_id, thread_id, request, sha, lane, status, confidence,"
                " cost_usd, input_tokens, output_tokens, elapsed_s, citations, state,"
                " repo, team, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))"
                " ON CONFLICT(run_id) DO UPDATE SET"
                "   lane=excluded.lane, status=excluded.status, confidence=excluded.confidence,"
                "   cost_usd=excluded.cost_usd, input_tokens=excluded.input_tokens,"
                "   output_tokens=excluded.output_tokens, elapsed_s=excluded.elapsed_s,"
                "   citations=excluded.citations, state=excluded.state,"
                "   updated_at=datetime('now')",
                (
                    run_id, thread_id, request, sha, lane, status, confidence,
                    round(cost_usd, 6), input_tokens, output_tokens, round(elapsed_s, 3),
                    citations, json.dumps(payload, default=str), repo, team,
                ),
            )
            self.conn.commit()
        except Exception:
            # A failed write must not leave the long-lived connection sitting on
            # an open RESERVED/EXCLUSIVE transaction — api/db.py opens fresh
            # connections to this same file per request and would block behind it.
            self.conn.rollback()
            raise

    # -- eval candidates ------------------------------------------------------

    def record_eval_candidate(self, run_id: str, item: dict[str, Any]) -> None:
        """One disagreement row — see `agents/interpreter.py`'s vote and
        `RunState.disagreements` for the shape of ``item``."""
        self.conn.execute(
            "INSERT INTO eval_candidates(run_id, node, field, candidates, chosen,"
            " is_majority, reason, created_at) VALUES(?,?,?,?,?,?,?, datetime('now'))",
            (
                run_id,
                item["node"],
                item["field"],
                json.dumps(item["candidates"]),
                item["chosen"],
                int(bool(item.get("is_majority"))),
                item.get("reason"),
            ),
        )
        self.conn.commit()

    def eval_candidates_for(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM eval_candidates WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()

    # -- answer cache ---------------------------------------------------------

    def cache_lookup(self, snapshot_key: str, question_norm: str) -> str | None:
        row = self.conn.execute(
            "SELECT run_id FROM answer_cache WHERE snapshot_key = ? AND question_norm = ?",
            (snapshot_key, question_norm),
        ).fetchone()
        return row["run_id"] if row else None

    def cache_store(
        self, snapshot_key: str, question_norm: str, run_id: str,
        *, lane: str | None = None, embedding: list[float] | None = None,
    ) -> None:
        """`lane`/`embedding` are optional so the exact-cache path (which never
        needed them) keeps working unchanged; the semantic layer
        (orchestrator/cache.py, `run.py::_maybe_cache`) is what supplies them."""
        self.conn.execute(
            "INSERT INTO answer_cache(snapshot_key, question_norm, run_id, lane, "
            "question_embedding, created_at)"
            " VALUES(?,?,?,?,?, datetime('now'))"
            " ON CONFLICT(snapshot_key, question_norm) DO UPDATE SET"
            "   run_id=excluded.run_id, lane=excluded.lane,"
            "   question_embedding=excluded.question_embedding, created_at=datetime('now')",
            (snapshot_key, question_norm, run_id, lane,
             json.dumps(embedding) if embedding is not None else None),
        )
        self.conn.commit()

    def semantic_cache_candidates(self, snapshot_key: str, lane: str) -> list[dict[str, Any]]:
        """Every cached entry for this snapshot and (guessed) lane that has an
        embedding — rows written before the semantic cache existed have none,
        and are silently not candidates rather than a query error."""
        rows = self.conn.execute(
            "SELECT run_id, question_norm, created_at, question_embedding FROM answer_cache "
            "WHERE snapshot_key = ? AND lane = ? AND question_embedding IS NOT NULL",
            (snapshot_key, lane),
        ).fetchall()
        return [
            {
                "run_id": row["run_id"], "question_norm": row["question_norm"],
                "created_at": row["created_at"],
                "embedding": json.loads(row["question_embedding"]),
            }
            for row in rows
        ]

    # -- reads --------------------------------------------------------------

    def get(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def state_of(self, run_id: str) -> dict[str, Any] | None:
        row = self.get(run_id)
        return json.loads(row["state"]) if row else None

    def latest_in_thread(self, thread_id: str, before_run_id: str | None = None) -> dict | None:
        """The most recent completed run in a thread — the follow-up context source."""
        query = (
            "SELECT state FROM runs WHERE thread_id = ? AND status IN ('done','aborted')"
        )
        params: list[Any] = [thread_id]
        if before_run_id:
            query += " AND run_id != ?"
            params.append(before_run_id)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        return json.loads(row["state"]) if row else None

    def thread(self, thread_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at, rowid", (thread_id,)
        ).fetchall()

    def all_threads(self, limit: int = config.THREADS_DEFAULT_LIMIT) -> list[dict]:
        """One row per thread: its most recent run's summary, newest thread first.

        A plain `GROUP BY thread_id` picks an arbitrary row per group in SQLite,
        so this pulls the latest `run_id` per thread first, then joins back to get
        that run's full summary columns — deterministic, not "whichever SQLite
        happened to keep."
        """
        rows = self.conn.execute(
            """
            SELECT r.*, t.run_count FROM runs r
            JOIN (
                SELECT thread_id, MAX(created_at) AS latest, COUNT(*) AS run_count
                FROM runs GROUP BY thread_id
            ) t ON t.thread_id = r.thread_id AND t.latest = r.created_at
            ORDER BY r.created_at DESC, r.rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "thread_id": row["thread_id"],
                "run_count": row["run_count"],
                "latest_run_id": row["run_id"],
                "latest_request": row["request"],
                "latest_status": row["status"],
                "latest_lane": row["lane"],
                "latest_confidence": row["confidence"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self.recent(1000))

    # -- quotas and costs (orchestrator/quotas.py, `/costs`) -----------------

    def cost_today(self, *, team: str | None = None, lane: str | None = None) -> float:
        """Cost so far today (local date, by `created_at`'s own clock), scoped
        by whichever of `team`/`lane` is given. Neither given sums everything.

        `date('now')` rather than a rolling 24h window: "today" resetting at
        midnight is what a daily cap means to whoever reads the number, and it
        is also why a quota can be probabilistically over for the first minute
        of a new day after being pinned at the ceiling the day before — a
        rolling window would avoid that at the cost of "daily" meaning
        something nobody asked for.
        """
        clauses, params = ["date(created_at) = date('now')"], []
        if team is not None:
            clauses.append("team = ?")
            params.append(team)
        if lane is not None:
            clauses.append("lane = ?")
            params.append(lane)
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM runs WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return float(row["total"])

    def costs_by(self, dimension: str, *, since_days: int = 1) -> list[dict[str, Any]]:
        """Cost grouped by `repo`, `lane`, or `team`, over the last `since_days`
        days — the `/costs` endpoint's one query, parameterised on which column
        rather than three near-identical methods."""
        if dimension not in ("repo", "lane", "team"):
            raise ValueError(f"costs_by: unknown dimension {dimension!r}")
        rows = self.conn.execute(
            f"SELECT COALESCE({dimension}, '(none)') AS key, COUNT(*) AS runs, "
            f"SUM(cost_usd) AS cost_usd FROM runs "
            f"WHERE created_at >= datetime('now', ?) GROUP BY key ORDER BY cost_usd DESC",
            (f"-{since_days} days",),
        ).fetchall()
        return [
            {dimension: row["key"], "runs": row["runs"], "cost_usd": round(row["cost_usd"] or 0.0, 6)}
            for row in rows
        ]

    # -- access audit (security/acl.py) --------------------------------------

    def record_access(
        self, *, principal: str, repo: str, question: str, sha: str | None,
        allowed: bool, reason: str,
    ) -> None:
        """One row per access DECISION, not per run — called before a run
        starts, so a denial that never became a run is still on the record."""
        self.conn.execute(
            "INSERT INTO access_audit(principal, repo, question, sha, allowed, reason, created_at) "
            "VALUES(?,?,?,?,?,?, datetime('now'))",
            (principal, repo, question, sha, int(allowed), reason),
        )
        self.conn.commit()

    def access_audit(
        self, *, repo: str | None = None, principal: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if principal is not None:
            clauses.append("principal = ?")
            params.append(principal)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM access_audit {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()

    # -- rolling quality (eval/rolling_quality.py) ---------------------------

    def done_runs_since(self, since_iso: str) -> list[sqlite3.Row]:
        """Completed runs from `since_iso` onward, oldest exclusions aside —
        the pool `rolling_quality.py` stratifies by lane and samples from.
        `aborted` is excluded: a run that stopped on budget has no real answer
        to grade, and scoring it would blend "ran out of money" into a
        correctness trend meant to track answer quality."""
        return self.conn.execute(
            "SELECT * FROM runs WHERE status = 'done' AND created_at >= ? "
            "ORDER BY created_at DESC, rowid DESC",
            (since_iso,),
        ).fetchall()

    def already_sampled(self, run_ids: Sequence[str]) -> set[str]:
        if not run_ids:
            return set()
        placeholders = ",".join("?" for _ in run_ids)
        rows = self.conn.execute(
            f"SELECT run_id FROM quality_samples WHERE run_id IN ({placeholders})",
            list(run_ids),
        ).fetchall()
        return {row["run_id"] for row in rows}

    def add_quality_sample(self, row: dict[str, Any]) -> None:
        """Record one rubric score against real traffic. `INSERT OR IGNORE` on
        the unique `run_id` — a job re-run in the same week must not double-
        count a run it already scored into that week's mean."""
        self.conn.execute(
            "INSERT OR IGNORE INTO quality_samples"
            "(run_id, lane, correctness, completeness, actionability, mean, scored, error,"
            " week, sampled_at) VALUES(?,?,?,?,?,?,?,?,?, datetime('now'))",
            (
                row["run_id"], row.get("lane"), row.get("correctness"), row.get("completeness"),
                row.get("actionability"), row.get("mean"), int(bool(row.get("scored"))),
                row.get("error"), row["week"],
            ),
        )
        self.conn.commit()

    def quality_trend(self, weeks: int = 12) -> list[dict[str, Any]]:
        """One row per week, oldest first — a trend table reads left-to-right
        as time passing, not newest-first the way `recent()` does."""
        rows = self.conn.execute(
            """
            SELECT week,
                   COUNT(*) AS total,
                   SUM(scored) AS scored,
                   AVG(CASE WHEN scored THEN mean END) AS mean_score
            FROM quality_samples
            GROUP BY week
            ORDER BY week DESC
            LIMIT ?
            """,
            (weeks,),
        ).fetchall()
        return [
            {
                "week": row["week"], "total": row["total"], "scored": row["scored"],
                "mean_score": round(row["mean_score"], 3) if row["mean_score"] is not None else None,
            }
            for row in reversed(rows)
        ]
