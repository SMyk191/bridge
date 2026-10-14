"""eval/rolling_quality.py — rubric-score a weekly sample of REAL traffic.

    .venv/bin/python eval/rolling_quality.py                # this week, sample + trend
    .venv/bin/python eval/rolling_quality.py --trend-only    # print the trend, sample nothing
    .venv/bin/python eval/rolling_quality.py --weeks 8

The fixed question sets (eval/sets/*.yaml) only ever exercise the questions
someone thought to write down. This is the other half: `config.QUALITY_SAMPLE_
SIZE` real runs, stratified by lane so one popular lane can't crowd out the
sample, rubric-scored (eval/rubric.py) the same way any other answer is, and
written into `quality_samples` so a trend table can be read back later without
re-scoring anything. A rubric mean is meaningless as a single week's number —
model variance and question mix both move it — the point is the LINE, and
`config.QUALITY_DRIFT_WEEKS` consecutive weekly declines is what this treats
as worth a human's attention rather than noise.

Every thumbs-down `feedback` row (api/db.py, same `runs.sqlite`) not already
mirrored is copied into `eval_candidates` — the standalone table meant for
exactly this: a queue of "look at this" for a human, independent of whatever
that run's own rubric score happened to say. A complaint the rubric didn't
catch is not a reason to trust the rubric less; it's a reason to have both.

Stratification by *project* is a documented no-op today (config.py): this
phase indexes one project at a time, so there is nothing to stratify by yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rubric  # noqa: E402

from bridge import config  # noqa: E402
from bridge.agents.llm import LLMClient  # noqa: E402
from bridge.agents.state import citations_of  # noqa: E402
from bridge.api import db as feedback_db  # noqa: E402
from bridge.orchestrator import RunStore  # noqa: E402
from bridge.retrieval.retrieve import _resolve_index_dir, repo_root_of  # noqa: E402


def iso_week(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def stratified_sample(rows: list, size: int) -> list:
    """Round-robin across lanes so no one lane can crowd out the sample —
    stable and simple rather than a weighted draw, because the point is
    "every lane gets a fair look," not a statistically precise proportion.

    `rows` is already newest-first (RunStore.done_runs_since); each lane's
    bucket keeps that order, so ties within a lane favour the most recent run.
    """
    by_lane: dict[str, list] = {}
    for row in rows:
        by_lane.setdefault(row["lane"] or "unknown", []).append(row)
    lanes = sorted(by_lane)
    chosen: list = []
    index = 0
    while len(chosen) < size and any(by_lane[lane][index:index + 1] for lane in lanes):
        for lane in lanes:
            if len(chosen) >= size:
                break
            bucket = by_lane[lane]
            if index < len(bucket):
                chosen.append(bucket[index])
        index += 1
    return chosen


def sample_and_score(store: RunStore, *, size: int, week: str, llm_enabled: bool) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    candidates = store.done_runs_since(since)
    already = store.already_sampled([row["run_id"] for row in candidates])
    fresh = [row for row in candidates if row["run_id"] not in already]
    sample = stratified_sample(fresh, size)

    scored, unscored = 0, 0
    for row in sample:
        state = store.state_of(row["run_id"])
        if state is None:
            continue
        repo_root = repo_root_of(_resolve_index_dir(state.get("sha") or None))
        score = rubric.score_answer(
            question=state.get("request") or "", answer=state.get("answer") or "",
            citations=citations_of(state), repo_root=repo_root,
            llm=LLMClient(enabled=llm_enabled),
        )
        store.add_quality_sample({
            "run_id": row["run_id"], "lane": row["lane"], "week": week,
            "correctness": score.correctness, "completeness": score.completeness,
            "actionability": score.actionability, "mean": score.mean, "scored": score.scored,
            "error": score.error,
        })
        scored += int(score.scored)
        unscored += int(not score.scored)

    return {
        "week": week, "pool": len(candidates), "already_sampled": len(already),
        "eligible": len(fresh), "sampled": len(sample), "scored": scored, "unscored": unscored,
        "lanes": sorted({row["lane"] or "unknown" for row in sample}),
    }


def mirror_thumbs_down(store: RunStore) -> int:
    """Every 'down' feedback row not yet in eval_candidates, copied in."""
    connection = feedback_db._connect()
    try:
        feedback_rows = connection.execute(
            "SELECT id, run_id, note, created_at FROM feedback WHERE vote = 'down' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    if not feedback_rows:
        return 0

    existing = store.conn.execute(
        "SELECT candidates FROM eval_candidates WHERE field = 'thumbs_down'"
    ).fetchall()
    already_mirrored: set[int] = set()
    for row in existing:
        try:
            already_mirrored.update(json.loads(row["candidates"]))
        except (json.JSONDecodeError, TypeError):
            continue

    added = 0
    for row in feedback_rows:
        if row["id"] in already_mirrored:
            continue
        store.record_eval_candidate(row["run_id"], {
            "node": "feedback", "field": "thumbs_down", "candidates": [row["id"]],
            "chosen": "down", "is_majority": False,
            "reason": row["note"] or "(no note left)",
        })
        added += 1
    return added


def drift_alert(trend: list[dict[str, Any]]) -> str | None:
    """`config.QUALITY_DRIFT_WEEKS` consecutive weekly declines, on scored
    weeks only — a week with zero scored samples (no judge reachable that
    week) is a gap in the data, not a data point saying quality fell to zero,
    and must not read as one."""
    scored_weeks = [w for w in trend if w["mean_score"] is not None]
    needed = config.QUALITY_DRIFT_WEEKS + 1
    if len(scored_weeks) < needed:
        return None
    tail = scored_weeks[-needed:]
    declines = [tail[i]["mean_score"] < tail[i - 1]["mean_score"] for i in range(1, len(tail))]
    if all(declines):
        line = " -> ".join(f"{w['week']}:{w['mean_score']}" for w in tail)
        return f"{config.QUALITY_DRIFT_WEEKS}-week decline: {line}"
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="eval/rolling_quality.py", description=__doc__)
    parser.add_argument("--weeks", type=int, default=12, help="weeks of trend to print")
    parser.add_argument("--size", type=int, default=config.QUALITY_SAMPLE_SIZE)
    parser.add_argument("--no-llm", action="store_true", help="score with LLMClient(enabled=False) — every sample comes back unscored, on purpose, to exercise the plumbing without a provider")
    parser.add_argument("--trend-only", action="store_true", help="skip sampling; just print the trend")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    with RunStore.open() as store:
        result: dict[str, Any] = {}
        if not args.trend_only:
            week = iso_week()
            result["sample"] = sample_and_score(
                store, size=args.size, week=week, llm_enabled=not args.no_llm
            )
            result["thumbs_down_mirrored"] = mirror_thumbs_down(store)

        trend = store.quality_trend(args.weeks)
        result["trend"] = trend
        alert = drift_alert(trend)
        result["drift_alert"] = alert

    if args.json:
        print(json.dumps(result, indent=2))
        return 1 if alert else 0

    if "sample" in result:
        s = result["sample"]
        print(f"week {s['week']}: pool={s['pool']} already_sampled={s['already_sampled']} "
              f"eligible={s['eligible']} sampled={s['sampled']} "
              f"scored={s['scored']} unscored={s['unscored']} lanes={s['lanes']}")
        print(f"thumbs-down mirrored to eval_candidates: {result['thumbs_down_mirrored']}")
    print("\nweek         total  scored  mean")
    for row in trend:
        mean = row["mean_score"] if row["mean_score"] is not None else "-"
        print(f"{row['week']:12s} {row['total']:>5}  {row['scored']:>6}  {mean}")
    if alert:
        print(f"\nDRIFT ALERT: {alert}")
    return 1 if alert else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
