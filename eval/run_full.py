"""eval/run_full.py — the whole picture, per question and rolled up.

    .venv/bin/python eval/run_full.py                        # eval/sets/*.yaml
    .venv/bin/python eval/run_full.py eval/sets/petclinic.yaml
    .venv/bin/python eval/run_full.py --no-llm                # fast, degraded pass
    .venv/bin/python eval/run_full.py --no-rubric              # skip the judge
    .venv/bin/python eval/run_full.py --no-determinism         # one pass, not two
    .venv/bin/python eval/run_full.py --json report.json       # for eval/gate.py

Per question: file precision/recall, core-file recall, a rubric score (three
1-5 dimensions plus any unsupported claims — eval/rubric.py), confidence, lane,
cost, latency, and — for standalone questions — determinism (the question run
twice, cold, as two SEPARATE threads; a citation-set mismatch is a failure,
matching eval/check_determinism.py's method exactly). Rolled up: means and
totals, plus every failure named rather than folded into an average that could
hide it.

A thread (`thread:` in the YAML) runs its turns through ONE `thread_id`, in
file order — the first turn's returned `thread_id` is threaded into every
later turn sharing its key, the same mechanism a real follow-up question uses.
A threaded turn is reported as determinism-`checked: false` rather than
compared: a fair repeat of a follow-up means replaying its whole prior thread
into a scratch thread first (otherwise the repeat's own "prior run in this
thread" is the first attempt at the very turn being checked, not a clean
rerun of it), and that thread-cloning is out of scope here.

**Recall is measured against what the answer actually cited**, not against
retrieval — `citations_of()` (agents/state.py) reads the answer's own citation
block first, falling back to the impact map and then raw retrieval only for a
run that produced neither. What matters is what the run told the user.

**`--no-llm` is not a shortcut around real numbers, it is a different real
number.** It runs `LLMClient(enabled=False)` — no network call, and therefore
none of the retry/backoff delay a dead provider imposes — so interpretation,
cartography, and the rubric all take their documented no-model fallback path
deterministically and instantly. Recall against raw retrieval, cost ($0),
latency, and determinism of the pipeline machinery are exercised for real in
this mode; lane classification, confidence beyond the retrieval base, and the
rubric are not, and the report says so rather than presenting a degraded run
as a normal one.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # eval/rubric.py, as a sibling import

import yaml  # noqa: E402

import rubric  # noqa: E402

from bridge.agents.llm import LLMClient  # noqa: E402
from bridge.agents.state import citation_path, citations_of  # noqa: E402
from bridge.orchestrator import RunStore, run  # noqa: E402
from bridge.retrieval.retrieve import Retriever, _resolve_index_dir, repo_root_of  # noqa: E402

DEFAULT_SETS = sorted(glob.glob(str(Path(__file__).parent / "sets" / "*.yaml")))


def paths_of(state: dict) -> list[str]:
    """Bare paths the answer actually cited — lane-agnostic (works for the
    changelog lane's `repo@commit:path` citations too, which carry no impact
    map at all)."""
    found: list[str] = []
    for citation in citations_of(state):
        path = citation_path(citation)
        if path and path not in found:
            found.append(path)
    return found


def evaluate_files(case: dict, cited: list[str]) -> dict:
    expected = case.get("expected_files") or []
    core = case.get("core_files") or []
    bonus = case.get("also_credit") or []
    cited_set = set(cited)

    hit = [p for p in expected if p in cited_set]
    missed = [p for p in expected if p not in cited_set]
    core_hit = [p for p in core if p in cited_set]
    core_missed = [p for p in core if p not in cited_set]
    extra = [p for p in cited if p not in expected and p not in bonus]

    recall = (len(hit) / len(expected)) if expected else None
    precision = (len(cited) - len(extra)) / len(cited) if cited else None
    core_recall = (len(core_hit) / len(core)) if core else None

    return {
        "expected_files": expected, "core_files": core,
        "hit": hit, "missed": missed, "core_hit": core_hit, "core_missed": core_missed,
        "bonus": [p for p in bonus if p in cited_set], "extra": extra,
        "recall": recall, "precision": precision, "core_recall": core_recall,
        "cited": cited,
    }


def evaluate_expectations(case: dict, state: dict) -> dict:
    lane_ok = state.get("lane") == case["expect_lane"] if case.get("expect_lane") else True
    impact = state.get("impact_map") or {}
    confidence = impact.get("confidence") or "-"
    expected_confidence = case.get("expect_confidence")
    confidence_ok = True
    if expected_confidence:
        wanted = {expected_confidence} if isinstance(expected_confidence, str) else set(expected_confidence)
        confidence_ok = confidence in wanted
    min_citations = case.get("expect_min_citations")
    citations_ok = len(citations_of(state)) >= min_citations if min_citations else True
    return {
        "lane": state.get("lane") or "-", "expected_lane": case.get("expect_lane"), "lane_ok": lane_ok,
        "confidence": confidence, "expected_confidence": expected_confidence, "confidence_ok": confidence_ok,
        "citations_ok": citations_ok,
    }


def check_determinism(question: str, thread_id: str | None, index_dir: Path,
                       retriever: Retriever, store: RunStore, llm_enabled: bool) -> dict:
    """A second cold run of the same question, compared by citation SET — the
    exact method eval/check_determinism.py uses. Threaded questions repeat
    inside the SAME thread (a fresh sibling run, not a mutation of the first),
    since a follow-up's determinism claim is about that exact context.

    A FRESH `LLMClient` for this call, deliberately — not the primary run's
    client. Every `run()` call in this file gets its own client for the same
    reason: `LLMClient.budget` defaults to `Budget()` (config's flat call/cost
    ceiling), not `Budget.for_run()`, and it is checked against that ONE
    client's cumulative `.usage`. Share a client across many questions and the
    ceiling stops being per-run — it becomes a suite-wide ceiling that a real,
    LLM-enabled 20-question pass would eventually trip, aborting later
    questions for a budget the run they're actually in never came close to.
    """
    second = run(question, thread_id=thread_id, index_dir=index_dir,
                 retriever=retriever, store=store, fresh=True,
                 llm=LLMClient(enabled=llm_enabled))
    return {"second_run_id": second.get("run_id"), "second_citations": sorted(citations_of(second))}


def run_question(
    case: dict, thread_ids: dict[str, str], index_dir: Path, retriever: Retriever,
    store: RunStore, llm_enabled: bool, *, score_rubric: bool, check_determinism_: bool,
) -> dict:
    thread_key = case.get("thread")
    thread_id = thread_ids.get(thread_key) if thread_key else None

    state = run(case["question"], thread_id=thread_id, index_dir=index_dir,
               retriever=retriever, store=store, fresh=True,
               llm=LLMClient(enabled=llm_enabled))
    if thread_key:
        thread_ids[thread_key] = state.get("thread_id")

    cited = paths_of(state)
    result: dict[str, Any] = {
        "id": case["id"], "question": case["question"], "thread": thread_key,
        "run_id": state.get("run_id"), "thread_id": state.get("thread_id"),
        "status": state.get("status"), "abort_reason": state.get("abort_reason"),
        "cost_usd": (state.get("usage") or {}).get("cost_usd", 0.0),
        "input_tokens": (state.get("usage") or {}).get("input_tokens", 0),
        "elapsed_s": (state.get("budget") or {}).get("elapsed_s", 0.0),
        **evaluate_files(case, cited),
        **evaluate_expectations(case, state),
    }

    if score_rubric:
        repo_root = repo_root_of(index_dir)
        result["rubric"] = rubric.score_answer(
            question=case["question"], answer=state.get("answer") or "",
            citations=citations_of(state), repo_root=repo_root,
            llm=LLMClient(enabled=llm_enabled),
        ).as_dict()
    else:
        result["rubric"] = None

    if not check_determinism_:
        result["determinism"] = {"checked": False, "stable": None}
    elif thread_key:
        # A comparable repeat of a follow-up means replaying the WHOLE prior
        # thread into a scratch thread first — reusing the production
        # thread_id would make the repeat see the first attempt at THIS turn
        # as new prior context, comparing a follow-up against something that
        # was never a fair rerun of it. That clone-the-thread machinery is out
        # of scope here; a threaded turn is honestly reported as unchecked
        # rather than checked against a comparison that quietly isn't fair.
        result["determinism"] = {
            "checked": False, "stable": None,
            "reason": "threaded follow-up — see run_full.py's note on why only "
                      "standalone questions are determinism-checked",
        }
    else:
        second = check_determinism(case["question"], None, index_dir, retriever, store, llm_enabled)
        stable = sorted(citations_of(state)) == second["second_citations"]
        result["determinism"] = {"checked": True, "stable": stable, **second}

    return result


def summarise(results: list[dict]) -> dict:
    with_recall = [r for r in results if r["recall"] is not None]
    with_precision = [r for r in results if r["precision"] is not None]
    with_core = [r for r in results if r["core_files"]]
    scored = [r for r in results if r.get("rubric") and r["rubric"]["scored"]]

    core_hit_total = sum(len(r["core_hit"]) for r in with_core)
    core_total = sum(len(r["core_files"]) for r in with_core)

    lane_fails = [r["id"] for r in results if not r["lane_ok"]]
    confidence_fails = [r["id"] for r in results if not r["confidence_ok"]]
    citation_count_fails = [r["id"] for r in results if not r["citations_ok"]]
    determinism_checked = [r for r in results if r["determinism"]["checked"]]
    determinism_fails = [r["id"] for r in determinism_checked if not r["determinism"]["stable"]]
    core_recall_fails = [r["id"] for r in with_core if r["core_recall"] < 1.0]

    return {
        "questions": len(results),
        "mean_recall": (sum(r["recall"] for r in with_recall) / len(with_recall)) if with_recall else None,
        "mean_precision": (sum(r["precision"] for r in with_precision) / len(with_precision))
                          if with_precision else None,
        "core_recall_pct": round(100.0 * core_hit_total / core_total, 2) if core_total else None,
        "core_recall_questions": len(with_core),
        "core_recall_fails": core_recall_fails,
        "rubric_scored_count": len(scored),
        "rubric_total": len(results),
        "rubric_mean": round(sum(r["rubric"]["mean"] for r in scored) / len(scored), 3) if scored else None,
        "rubric_unsupported_total": sum(len(r["rubric"]["unsupported_claims"]) for r in scored),
        "lane_fails": lane_fails,
        "confidence_fails": confidence_fails,
        "citation_count_fails": citation_count_fails,
        "determinism_checked": len(determinism_checked),
        "determinism_fails": determinism_fails,
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "total_elapsed_s": round(sum(r["elapsed_s"] for r in results), 2),
    }


def load_cases(paths: list[str]) -> list[tuple[str, dict]]:
    cases = []
    for path in paths:
        spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        for case in spec["questions"]:
            cases.append((Path(path).stem, case))
    return cases


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="eval/run_full.py", description=__doc__)
    parser.add_argument("sets", nargs="*", default=DEFAULT_SETS, help="YAML question sets")
    parser.add_argument("--no-llm", action="store_true", help="LLMClient(enabled=False): fast, degraded")
    parser.add_argument("--no-rubric", action="store_true", help="skip the LLM judge")
    parser.add_argument("--no-determinism", action="store_true", help="one pass per question, not two")
    parser.add_argument("--only", action="append", help="run only this question id (repeatable)")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON to PATH")
    args = parser.parse_args(argv)

    by_file = load_cases(args.sets)
    if args.only:
        wanted = set(args.only)
        by_file = [(f, c) for f, c in by_file if c["id"] in wanted]

    index_dir = _resolve_index_dir()
    llm_enabled = not args.no_llm
    print(f"index: {index_dir}")
    print(f"llm  : {'disabled (--no-llm)' if args.no_llm else LLMClient(enabled=True).status()}")
    print(f"questions: {len(by_file)}\n")

    results_by_file: dict[str, list[dict]] = {}
    started = time.perf_counter()
    with Retriever.open(index_dir) as retriever, RunStore.open() as store:
        # Warm the process before anything is timed or scored. The embedding
        # encoder and the retriever's caches are loaded lazily on first use, so
        # the very first `run()` call in a fresh process pays a one-time cost
        # (~10s measured, cold) that every later call does not. That is not
        # just slow — Phase C's follow-up shots only fire when the run's OWN
        # wall-clock budget still has >30% left, so a cold-start question can
        # legitimately take a different follow-up decision than a warm re-run
        # of the exact same question, and look like a determinism failure that
        # is really a caching artifact of this harness, not of the orchestrator.
        run("warm-up: what does this repository do?", index_dir=index_dir,
            retriever=retriever, store=store, fresh=True, llm=LLMClient(enabled=False))

        thread_ids: dict[str, str] = {}
        for file_stem, case in by_file:
            result = run_question(
                case, thread_ids, index_dir, retriever, store, llm_enabled,
                score_rubric=not args.no_rubric, check_determinism_=not args.no_determinism,
            )
            results_by_file.setdefault(file_stem, []).append(result)

            rub = result["rubric"]
            rub_str = f"{rub['mean']}/5" if rub and rub["scored"] else ("skip" if args.no_rubric else "unscored")
            det = result["determinism"]
            det_str = "-" if not det["checked"] else ("stable" if det["stable"] else "DIFFERED")
            recall_str = f"{result['recall']:.0%}" if result["recall"] is not None else "n/a"
            print(f"  {result['id']:24s} recall {recall_str:>5s}  rubric {rub_str:>8s}  "
                  f"det {det_str:9s}  conf {result['confidence']:7s}  lane {result['lane']:13s}  "
                  f"${result['cost_usd']:.4f}  {result['elapsed_s']:.1f}s")

    all_results = [r for rows in results_by_file.values() for r in rows]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_dir": str(index_dir),
        "llm_enabled": not args.no_llm,
        "rubric_enabled": not args.no_rubric,
        "determinism_enabled": not args.no_determinism,
        "sets": {stem: summarise(rows) for stem, rows in results_by_file.items()},
        "summary": summarise(all_results),
        "results": all_results,
    }

    print()
    print("=" * 100)
    summary = report["summary"]
    print(f"mean file recall   : {summary['mean_recall']:.1%}" if summary["mean_recall"] is not None else "mean file recall   : n/a")
    print(f"mean precision     : {summary['mean_precision']:.1%}" if summary["mean_precision"] is not None else "mean precision     : n/a")
    print(f"core-file recall   : {summary['core_recall_pct']}%  ({summary['core_recall_questions']} questions with core_files)"
          if summary["core_recall_pct"] is not None else "core-file recall   : n/a (no question in this run defines core_files)")
    print(f"rubric mean        : {summary['rubric_mean']}  ({summary['rubric_scored_count']}/{summary['rubric_total']} scored)"
          if summary["rubric_mean"] is not None else
          f"rubric mean        : n/a  (0/{summary['rubric_total']} scored — {'--no-rubric' if args.no_rubric else 'no judge provider reachable'})")
    print(f"determinism        : {summary['determinism_checked'] - len(summary['determinism_fails'])}/"
          f"{summary['determinism_checked']} stable")
    print(f"total cost         : ${summary['total_cost_usd']:.4f}   total time: {summary['total_elapsed_s']:.1f}s"
          f"  (wall: {time.perf_counter()-started:.1f}s)")
    if summary["lane_fails"]:
        print(f"LANE MISMATCH      : {summary['lane_fails']}")
    if summary["confidence_fails"]:
        print(f"CONFIDENCE MISMATCH: {summary['confidence_fails']}")
    if summary["citation_count_fails"]:
        print(f"TOO FEW CITATIONS  : {summary['citation_count_fails']}")
    if summary["core_recall_fails"]:
        print(f"CORE FILE MISSED   : {summary['core_recall_fails']}")
    if summary["determinism_fails"]:
        print(f"DETERMINISM FAILED : {summary['determinism_fails']}")
    print("=" * 100)

    if args.no_llm:
        print("\n--no-llm: interpretation/cartography/rubric all took the documented no-model "
              "fallback. Recall-against-retrieval, cost, latency, and pipeline determinism are "
              "real; lane/confidence/rubric numbers above are not representative of a live run.")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
