"""Smoke evaluation: per-question file recall, confidence, and cost.

    .venv/bin/python ../eval/run_smoke.py            # all questions
    .venv/bin/python ../eval/run_smoke.py add-pet    # one, with the full answer

Recall is measured against the impact map's citations, not against retrieval —
what matters is what the run actually *told the user*, not what it looked at.
`expect` files are required; `also_credit` files count toward a bonus column but
not against recall, because there is usually more than one defensible answer to
"which files matter here".
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import yaml  # noqa: E402

from bridge.agents.state import citation_path  # noqa: E402
from bridge.orchestrator import RunStore, run  # noqa: E402
from bridge.retrieval.retrieve import Retriever, _resolve_index_dir  # noqa: E402

QUESTIONS_FILE = Path(__file__).parent / "smoke_questions.yaml"


def paths_from(state: dict) -> list[str]:
    impact = state.get("impact_map") or {}
    found: list[str] = []
    for kind in ("endpoints", "classes_methods", "entities_tables", "screens", "hubs_crossed"):
        for entry in impact.get(kind) or []:
            path = citation_path(entry.get("citation", ""))
            if path and path not in found:
                found.append(path)
    return found


def evaluate(case: dict, state: dict) -> dict:
    expect = case.get("expect") or []
    bonus = case.get("also_credit") or []
    cited = paths_from(state)
    cited_set = set(cited)

    hit = [p for p in expect if p in cited_set]
    missed = [p for p in expect if p not in cited_set]
    extra_credit = [p for p in bonus if p in cited_set]

    impact = state.get("impact_map") or {}
    usage = state.get("usage") or {}
    lane_ok = True
    if case.get("expect_lane"):
        lane_ok = state.get("lane") == case["expect_lane"]

    # A question can also assert on confidence instead of (or alongside) files —
    # e.g. "this is unanswerable from this codebase, the run must say so rather
    # than guess." `expect_confidence` is a value or list of acceptable values.
    confidence_ok = True
    expected_confidence = case.get("expect_confidence")
    if expected_confidence:
        wanted = (
            {expected_confidence}
            if isinstance(expected_confidence, str)
            else set(expected_confidence)
        )
        confidence_ok = (impact.get("confidence") or "-") in wanted

    ok = lane_ok and confidence_ok

    return {
        "id": case["id"],
        "question": case["question"],
        "recall": (len(hit) / len(expect)) if expect else (1.0 if ok else 0.0),
        "hit": hit,
        "missed": missed,
        "bonus": extra_credit,
        "cited": cited,
        "confidence": impact.get("confidence") or "-",
        "lane": state.get("lane") or "-",
        "expected_lane": case.get("expect_lane"),
        "lane_ok": lane_ok,
        "expected_confidence": expected_confidence,
        "confidence_ok": confidence_ok,
        "cost_usd": usage.get("cost_usd", 0.0),
        "input_tokens": usage.get("input_tokens", 0),
        "elapsed_s": (state.get("budget") or {}).get("elapsed_s", 0.0),
        "status": state.get("status"),
        "run_id": state.get("run_id"),
    }


def main(argv: list[str]) -> int:
    spec = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))
    cases = spec["questions"]
    if argv:
        wanted = set(argv)
        cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            print(f"no question matched {argv}; ids: {[c['id'] for c in spec['questions']]}")
            return 2

    index_dir = _resolve_index_dir()
    print(f"index: {index_dir}")
    print(f"questions: {len(cases)}\n")

    results = []
    started = time.perf_counter()
    # One retriever and one store across the suite — model loading dominates
    # otherwise, and each run gets its own budget anyway.
    with Retriever.open(index_dir) as retriever, RunStore.open() as store:
        for case in cases:
            # fresh=True: the smoke suite exists to measure real behaviour and
            # real cost on every invocation. Letting it silently serve a cached
            # $0 answer from an earlier run would make its recall and cost
            # numbers meaningless the moment the answer cache (phase D) exists.
            state = run(
                case["question"], index_dir=index_dir, retriever=retriever, store=store, fresh=True
            )
            results.append(evaluate(case, state))
            last = results[-1]
            print(
                f"  {last['id']:22s} recall {last['recall']:.0%}  "
                f"conf {last['confidence']:6s}  lane {last['lane']:14s}  "
                f"${last['cost_usd']:.4f}  {last['elapsed_s']:.1f}s"
            )

    print()
    print("=" * 96)
    print(f"{'id':22s} {'recall':>7s} {'hit/exp':>8s} {'bonus':>6s} {'conf':>7s} "
          f"{'lane':>15s} {'cost':>8s} {'tok':>7s} {'s':>5s}")
    print("-" * 96)
    for r in results:
        expected = len(r["hit"]) + len(r["missed"])
        lane = r["lane"] + ("" if r["lane_ok"] else " !")
        confidence = r["confidence"] + ("" if r["confidence_ok"] else " !")
        print(
            f"{r['id']:22s} {r['recall']:>6.0%} {len(r['hit']):>4}/{expected:<3} "
            f"{len(r['bonus']):>6} {confidence:>7s} {lane:>15s} "
            f"${r['cost_usd']:>7.4f} {r['input_tokens']:>7} {r['elapsed_s']:>5.1f}"
        )
    print("-" * 96)

    total_cost = sum(r["cost_usd"] for r in results)
    mean_recall = sum(r["recall"] for r in results) / len(results)
    perfect = sum(1 for r in results if r["recall"] == 1.0)
    lane_fails = [r["id"] for r in results if not r["lane_ok"]]
    confidence_fails = [r["id"] for r in results if not r["confidence_ok"]]
    high = sum(1 for r in results if r["confidence"] == "high")

    print(
        f"{'TOTAL':22s} {mean_recall:>6.0%} {perfect:>4}/{len(results):<3} "
        f"{'':>6} {high:>4}hi {'':>15s} ${total_cost:>7.4f} "
        f"{sum(r['input_tokens'] for r in results):>7} {time.perf_counter()-started:>5.1f}"
    )
    print("=" * 96)
    print(f"mean file recall : {mean_recall:.1%}   ({perfect}/{len(results)} questions at 100%)")
    print(f"high confidence  : {high}/{len(results)}")
    print(f"total cost       : ${total_cost:.4f}   (mean ${total_cost/len(results):.4f}/question)")
    if lane_fails:
        print(f"LANE MISMATCH    : {lane_fails}")
    if confidence_fails:
        print(f"CONFIDENCE MISMATCH : {confidence_fails}")

    misses = [(r["id"], r["missed"]) for r in results if r["missed"]]
    if misses:
        print("\nmissed expectations:")
        for qid, missed in misses:
            for path in missed:
                print(f"  {qid:22s} {path}")

    if len(cases) == 1:
        state = RunStore.open().state_of(results[0]["run_id"])
        print("\n" + "=" * 96)
        print("FULL ANSWER")
        print("=" * 96)
        print(state.get("answer") or "(none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
