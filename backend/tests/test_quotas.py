"""orchestrator/quotas.py, and the RunStore cost columns/queries it's built on.

Against a real `runs.sqlite` (tmp_path) — `cost_today`/`costs_by` are SQL,
and the migration path (an existing file without `repo`/`team` gaining them
via `ALTER TABLE`) is exactly the kind of thing worth proving against a real
file rather than asserting from reading the code.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from bridge import config
from bridge.orchestrator import quotas
from bridge.orchestrator.store import RunStore


@pytest.fixture()
def store(tmp_path):
    with RunStore.open(tmp_path / "runs.sqlite") as s:
        yield s


_run_ids = itertools.count()


def _seed(store: RunStore, *, repo="demo", lane="question", team="default", cost=1.0) -> None:
    store.conn.execute(
        "INSERT INTO runs(run_id, thread_id, request, sha, lane, status, cost_usd, state, "
        "repo, team, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))",
        (f"r-{next(_run_ids)}", "t1", "q", "sha1", lane, "done", cost, "{}", repo, team),
    )
    store.conn.commit()


# ------------------------------------------------------------------ migration


def test_a_runs_sqlite_file_from_before_phase_m_gets_the_new_columns(tmp_path):
    """A pre-existing file (the old CREATE TABLE, no repo/team) must not error
    on open — it must be migrated in place."""
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, request TEXT NOT NULL,
          sha TEXT, lane TEXT, status TEXT NOT NULL, confidence TEXT,
          cost_usd REAL NOT NULL DEFAULT 0.0, input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, elapsed_s REAL NOT NULL DEFAULT 0.0,
          citations INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    connection.execute(
        "INSERT INTO runs(run_id, thread_id, request, status, state, created_at, updated_at) "
        "VALUES('r1','t1','q','done','{}', datetime('now'), datetime('now'))"
    )
    connection.commit()
    connection.close()

    with RunStore.open(path) as store:
        row = store.get("r1")
        assert row["team"] == "default"  # the column's own DEFAULT, via ALTER
        assert row["repo"] is None
        store.conn.execute("UPDATE runs SET team = 'alpha', repo = 'demo' WHERE run_id = 'r1'")
        store.conn.commit()
        assert store.cost_today(team="alpha") == 0.0  # cost_usd defaulted to 0.0


def test_opening_an_already_migrated_file_twice_does_not_error(tmp_path):
    path = tmp_path / "runs.sqlite"
    RunStore.open(path).close()
    RunStore.open(path).close()  # would raise "duplicate column" if not swallowed


# ------------------------------------------------------------------ RunStore.cost_today / costs_by


def test_cost_today_sums_only_todays_rows(store):
    _seed(store, team="alpha", cost=3.0)
    store.conn.execute("UPDATE runs SET created_at = datetime('now', '-2 days')")
    store.conn.commit()
    assert store.cost_today(team="alpha") == 0.0


def test_cost_today_scopes_by_team_and_by_lane_independently(store):
    _seed(store, team="alpha", lane="question", cost=2.0)
    _seed(store, team="beta", lane="bug", cost=5.0)
    assert store.cost_today(team="alpha") == pytest.approx(2.0)
    assert store.cost_today(team="beta") == pytest.approx(5.0)
    assert store.cost_today(lane="bug") == pytest.approx(5.0)
    assert store.cost_today() == pytest.approx(7.0)


def test_costs_by_ranks_by_spend_descending(store):
    _seed(store, team="alpha", cost=1.0)
    _seed(store, team="beta", cost=9.0)
    rows = store.costs_by("team")
    assert [r["team"] for r in rows] == ["beta", "alpha"]
    assert rows[0]["cost_usd"] == pytest.approx(9.0)
    assert rows[0]["runs"] == 1


def test_costs_by_rejects_an_unknown_dimension(store):
    with pytest.raises(ValueError):
        store.costs_by("not_a_real_column")


def test_costs_by_respects_the_days_window(store):
    _seed(store, team="alpha", cost=4.0)
    store.conn.execute("UPDATE runs SET created_at = datetime('now', '-10 days')")
    store.conn.commit()
    assert store.costs_by("team", since_days=1) == []
    assert len(store.costs_by("team", since_days=30)) == 1


# ------------------------------------------------------------------ check_team_quota


def test_a_team_under_both_limits_gets_no_warning(store):
    decision = quotas.check_team_quota(store, "alpha")
    assert not decision.blocked
    assert decision.warning is None


def test_a_team_over_soft_but_under_hard_warns_without_blocking(store):
    midpoint = (config.QUOTA_TEAM_DAILY_USD_SOFT + config.QUOTA_TEAM_DAILY_USD_HARD) / 2
    _seed(store, team="alpha", cost=midpoint)
    decision = quotas.check_team_quota(store, "alpha")
    assert not decision.blocked
    assert decision.warning is not None
    assert "alpha" in decision.warning


def test_a_team_at_or_over_hard_is_blocked_with_no_warning(store):
    _seed(store, team="alpha", cost=config.QUOTA_TEAM_DAILY_USD_HARD)
    decision = quotas.check_team_quota(store, "alpha")
    assert decision.blocked
    assert decision.warning is None  # blocked is a refusal, not a warning on a run that proceeded


def test_teams_are_independent(store):
    _seed(store, team="alpha", cost=config.QUOTA_TEAM_DAILY_USD_HARD)
    assert quotas.check_team_quota(store, "alpha").blocked
    assert not quotas.check_team_quota(store, "beta").blocked


# ------------------------------------------------------------------ lane_budget_ceiling


def test_a_lane_with_no_configured_cap_is_never_tightened(store):
    assert quotas.lane_budget_ceiling(store, "question", current_max_usd=5.0) is None


def test_a_lane_under_its_cap_is_not_tightened(store):
    lane = next(iter(config.QUOTA_LANE_DAILY_USD))
    cap = config.QUOTA_LANE_DAILY_USD[lane]
    _seed(store, lane=lane, cost=cap / 4)
    # remaining (~0.75 * cap) is bigger than a small current ceiling, so nothing shrinks
    assert quotas.lane_budget_ceiling(store, lane, current_max_usd=0.01) is None


def test_a_lane_near_its_cap_is_tightened_to_the_remainder(store):
    lane = next(iter(config.QUOTA_LANE_DAILY_USD))
    cap = config.QUOTA_LANE_DAILY_USD[lane]
    spent = cap - 0.5
    _seed(store, lane=lane, cost=spent)
    ceiling = quotas.lane_budget_ceiling(store, lane, current_max_usd=100.0)
    assert ceiling == pytest.approx(0.5)


def test_a_lane_already_over_its_cap_is_tightened_to_zero_not_negative(store):
    lane = next(iter(config.QUOTA_LANE_DAILY_USD))
    cap = config.QUOTA_LANE_DAILY_USD[lane]
    _seed(store, lane=lane, cost=cap + 10)
    ceiling = quotas.lane_budget_ceiling(store, lane, current_max_usd=100.0)
    assert ceiling == 0.0


def test_the_ceiling_never_widens_an_already_tighter_budget(store):
    lane = next(iter(config.QUOTA_LANE_DAILY_USD))
    assert quotas.lane_budget_ceiling(store, lane, current_max_usd=0.0001) is None


def test_a_run_with_no_existing_ceiling_still_gets_tightened(store):
    lane = next(iter(config.QUOTA_LANE_DAILY_USD))
    cap = config.QUOTA_LANE_DAILY_USD[lane]
    _seed(store, lane=lane, cost=cap - 1.0)
    assert quotas.lane_budget_ceiling(store, lane, current_max_usd=None) == pytest.approx(1.0)
