"""eval/rolling_quality.py — stratified sampling, drift detection, and the
RunStore machinery both are built on. No LLM involved: sampling and drift
detection are pure functions over rows this test constructs directly, and the
`quality_samples`/`eval_candidates` persistence is exercised against a real
throwaway sqlite file (RunStore.open already needs one; there is no fake
substitute for "does INSERT OR IGNORE actually dedupe" worth building).
"""

from __future__ import annotations

import json

import pytest
import rolling_quality

from bridge.orchestrator import RunStore


@pytest.fixture()
def store(tmp_path):
    with RunStore.open(tmp_path / "runs.sqlite") as s:
        yield s


def _row(run_id: str, lane: str) -> dict:
    return {"run_id": run_id, "lane": lane}


# ------------------------------------------------------------------ iso_week


def test_iso_week_format():
    import datetime as dt
    week = rolling_quality.iso_week(dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc))
    assert week.startswith("2026-W")


# ------------------------------------------------------------------ stratified_sample


def test_sampling_spreads_across_lanes_before_taking_a_second_from_any_one():
    rows = [_row(f"q{i}", "question") for i in range(10)] + [_row(f"b{i}", "bug") for i in range(10)]
    chosen = rolling_quality.stratified_sample(rows, size=4)
    lanes = [r["lane"] for r in chosen]
    assert lanes.count("question") == 2
    assert lanes.count("bug") == 2


def test_a_lane_with_fewer_rows_than_its_share_does_not_block_the_others():
    rows = [_row("only-bug", "bug")] + [_row(f"q{i}", "question") for i in range(10)]
    chosen = rolling_quality.stratified_sample(rows, size=5)
    assert len(chosen) == 5
    assert sum(1 for r in chosen if r["lane"] == "bug") == 1


def test_sampling_never_returns_more_than_the_pool_has():
    rows = [_row("a", "question"), _row("b", "bug")]
    chosen = rolling_quality.stratified_sample(rows, size=10)
    assert len(chosen) == 2


def test_a_run_with_no_lane_is_its_own_bucket_not_dropped():
    rows = [_row("a", None), _row("b", "question")]
    chosen = rolling_quality.stratified_sample(rows, size=2)
    assert len(chosen) == 2


def test_sampling_is_deterministic_for_the_same_input():
    rows = [_row(f"q{i}", "question") for i in range(6)]
    assert rolling_quality.stratified_sample(rows, 3) == rolling_quality.stratified_sample(rows, 3)


# ------------------------------------------------------------------ drift_alert


def _week(week: str, mean, scored=5, total=5) -> dict:
    return {"week": week, "mean_score": mean, "scored": scored, "total": total}


def test_no_alert_with_too_little_history():
    trend = [_week("2026-W01", 4.0), _week("2026-W02", 3.9)]
    assert rolling_quality.drift_alert(trend) is None


def test_two_consecutive_declines_alert(monkeypatch):
    from bridge import config
    monkeypatch.setattr(config, "QUALITY_DRIFT_WEEKS", 2)
    trend = [_week("2026-W01", 4.5), _week("2026-W02", 4.0), _week("2026-W03", 3.5)]
    alert = rolling_quality.drift_alert(trend)
    assert alert is not None
    assert "2026-W01" in alert and "2026-W03" in alert


def test_a_recovery_in_the_middle_does_not_alert(monkeypatch):
    from bridge import config
    monkeypatch.setattr(config, "QUALITY_DRIFT_WEEKS", 2)
    trend = [_week("2026-W01", 3.5), _week("2026-W02", 4.5), _week("2026-W03", 3.0)]
    assert rolling_quality.drift_alert(trend) is None


def test_an_unscored_gap_week_does_not_count_as_zero(monkeypatch):
    """A week with no judge reachable is a gap in the data, not evidence
    quality fell to zero — it must not manufacture a decline."""
    from bridge import config
    monkeypatch.setattr(config, "QUALITY_DRIFT_WEEKS", 2)
    trend = [_week("2026-W01", 4.0), _week("2026-W02", None, scored=0),
             _week("2026-W03", 4.0)]
    assert rolling_quality.drift_alert(trend) is None


def test_flat_scores_are_not_a_decline():
    trend = [_week("2026-W01", 4.0), _week("2026-W02", 4.0), _week("2026-W03", 4.0)]
    assert rolling_quality.drift_alert(trend) is None


# ------------------------------------------------------------------ RunStore persistence


def test_a_quality_sample_round_trips_through_the_trend(store):
    store.add_quality_sample({"run_id": "r1", "lane": "question", "week": "2026-W01",
                              "correctness": 4, "completeness": 4, "actionability": 4,
                              "mean": 4.0, "scored": True, "error": None})
    trend = store.quality_trend(weeks=5)
    assert trend[-1] == {"week": "2026-W01", "total": 1, "scored": 1, "mean_score": 4.0}


def test_resampling_the_same_run_id_does_not_double_count(store):
    for _ in range(2):
        store.add_quality_sample({"run_id": "r1", "lane": "question", "week": "2026-W01",
                                  "correctness": 5, "completeness": 5, "actionability": 5,
                                  "mean": 5.0, "scored": True, "error": None})
    trend = store.quality_trend(weeks=1)
    assert trend[0]["total"] == 1


def test_unscored_samples_count_toward_total_but_not_the_mean(store):
    store.add_quality_sample({"run_id": "r1", "lane": "question", "week": "2026-W01",
                              "correctness": None, "completeness": None, "actionability": None,
                              "mean": None, "scored": False, "error": "no provider"})
    trend = store.quality_trend(weeks=1)
    assert trend[0] == {"week": "2026-W01", "total": 1, "scored": 0, "mean_score": None}


def test_already_sampled_reports_only_run_ids_actually_present(store):
    store.add_quality_sample({"run_id": "r1", "lane": "q", "week": "2026-W01",
                              "correctness": 3, "completeness": 3, "actionability": 3,
                              "mean": 3.0, "scored": True, "error": None})
    assert store.already_sampled(["r1", "r2"]) == {"r1"}
    assert store.already_sampled([]) == set()


def test_trend_is_oldest_first_for_reading_as_a_timeline(store):
    for week in ("2026-W03", "2026-W01", "2026-W02"):
        store.add_quality_sample({"run_id": f"r-{week}", "lane": "q", "week": week,
                                  "correctness": 3, "completeness": 3, "actionability": 3,
                                  "mean": 3.0, "scored": True, "error": None})
    trend = store.quality_trend(weeks=10)
    assert [row["week"] for row in trend] == ["2026-W01", "2026-W02", "2026-W03"]


# ------------------------------------------------------------------ thumbs-down mirroring


def test_mirroring_is_idempotent_across_two_calls(store, tmp_path, monkeypatch):
    from bridge import config
    from bridge.api import db as feedback_db

    monkeypatch.setattr(config, "RUNS_DB", store.conn.execute("PRAGMA database_list").fetchone()[2])
    connection = feedback_db._connect()
    connection.execute(
        "INSERT INTO feedback(run_id, vote, note, created_at) VALUES('r1','down','too vague',datetime('now'))"
    )
    connection.commit()
    connection.close()

    first = rolling_quality.mirror_thumbs_down(store)
    second = rolling_quality.mirror_thumbs_down(store)
    assert first == 1
    assert second == 0
    rows = store.eval_candidates_for("r1")
    assert len(rows) == 1
    assert json.loads(rows[0]["candidates"])
