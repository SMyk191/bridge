"""eval/gate.py — pass/fail a report against a stored baseline.

    .venv/bin/python eval/gate.py                              # run the suite, gate it
    .venv/bin/python eval/gate.py --report report.json          # gate an existing report
    .venv/bin/python eval/gate.py --save-baseline                # run the suite, STORE it
    .venv/bin/python eval/gate.py --report report.json --save-baseline

Three ways to fail, matching the phase's own ask exactly:

  1. core-file recall dropped by more than `config.GATE_CORE_RECALL_DROP_PTS`
     percentage points
  2. the rubric mean dropped by more than `config.GATE_RUBRIC_DROP`
  3. any question that was determinism-stable in the baseline is not stable now

Only ONE needs to fire. `compare()` is pure — a dict in, a dict out, no I/O —
so it is unit-tested directly against synthetic reports rather than only ever
being exercised by a real (and, on this machine, currently un-runnable — see
PROGRESS.md — LLM-dependent) end-to-end pass.

The baseline is a full report, not a trimmed summary: `--save-baseline`
overwrites it with whatever report was just produced, so the next `gate` run
compares against the exact numbers a human looked at and accepted, not a
recomputed subset that could quietly drift from what "the baseline" means.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from bridge import config  # noqa: E402

DEFAULT_BASELINE = Path(__file__).parent / config.GATE_BASELINE_FILE


@dataclass(slots=True)
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    #: every regression, even ones that alone wouldn't fail the gate — useful
    #: signal a run is trending worse before it's bad enough to block anything
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons, "notes": self.notes}


def compare(current: dict, baseline: dict) -> GateResult:
    result = GateResult(passed=True)
    cur, base = current["summary"], baseline["summary"]

    # -- (1) core-file recall -------------------------------------------------
    if cur.get("core_recall_pct") is not None and base.get("core_recall_pct") is not None:
        drop = base["core_recall_pct"] - cur["core_recall_pct"]
        if drop > config.GATE_CORE_RECALL_DROP_PTS:
            result.passed = False
            result.reasons.append(
                f"core-file recall dropped {drop:.1f}pts "
                f"({base['core_recall_pct']:.1f}% -> {cur['core_recall_pct']:.1f}%), "
                f"over the {config.GATE_CORE_RECALL_DROP_PTS}pt threshold"
            )
        elif drop > 0:
            result.notes.append(
                f"core-file recall down {drop:.1f}pts (within the "
                f"{config.GATE_CORE_RECALL_DROP_PTS}pt threshold)"
            )
    elif base.get("core_recall_pct") is not None and cur.get("core_recall_pct") is None:
        result.passed = False
        result.reasons.append(
            "core-file recall was measured in the baseline and is not measured now "
            "(no question in this run defines core_files, or none has one)"
        )

    # -- (2) rubric mean -------------------------------------------------------
    if cur.get("rubric_mean") is not None and base.get("rubric_mean") is not None:
        drop = base["rubric_mean"] - cur["rubric_mean"]
        if drop > config.GATE_RUBRIC_DROP:
            result.passed = False
            result.reasons.append(
                f"rubric mean dropped {drop:.3f} ({base['rubric_mean']} -> {cur['rubric_mean']}), "
                f"over the {config.GATE_RUBRIC_DROP} threshold"
            )
        elif drop > 0:
            result.notes.append(
                f"rubric mean down {drop:.3f} (within the {config.GATE_RUBRIC_DROP} threshold)"
            )
    elif base.get("rubric_mean") is not None and cur.get("rubric_mean") is None:
        # Not a silent pass: a baseline with a real rubric mean and a current
        # run with none (no judge reachable, or --no-rubric) cannot prove the
        # rubric didn't regress — it can only fail to check. Reported as a
        # note, not a failure, because "unmeasured" and "regressed" are
        # different claims and a gate should never conflate them into a red X
        # for a reason that isn't actually a quality regression.
        result.notes.append(
            f"rubric was {base['rubric_mean']} in the baseline and is unmeasured now "
            "(no judge provider reachable, or --no-rubric) — this run cannot confirm "
            "the rubric held, only that nothing else regressed"
        )

    # -- (3) determinism ---------------------------------------------------
    base_by_id = {r["id"]: r for r in baseline.get("results", [])}
    newly_unstable = []
    for row in current.get("results", []):
        prior = base_by_id.get(row["id"])
        if prior is None:
            continue
        was_stable = prior.get("determinism", {}).get("checked") and prior["determinism"]["stable"]
        is_checked = row.get("determinism", {}).get("checked")
        is_stable = row.get("determinism", {}).get("stable")
        if was_stable and is_checked and not is_stable:
            newly_unstable.append(row["id"])
    if newly_unstable:
        result.passed = False
        result.reasons.append(
            f"determinism regressed on {len(newly_unstable)} question(s) that were stable "
            f"in the baseline: {newly_unstable}"
        )

    return result


def _run_suite(extra_args: list[str]) -> dict:
    """Shell out to run_full.py rather than importing it — a subprocess means
    `gate.py`'s own exit code reflects only the comparison, never a crash deep
    in the orchestrator, and the printed per-question table still reaches the
    terminal instead of being swallowed."""
    out_path = Path(__file__).parent / "_gate_run.json"
    command = [sys.executable, str(Path(__file__).parent / "run_full.py"), *extra_args,
               "--json", str(out_path)]
    subprocess.run(command, check=True)
    report = json.loads(out_path.read_text(encoding="utf-8"))
    out_path.unlink(missing_ok=True)
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="eval/gate.py", description=__doc__)
    parser.add_argument("--report", metavar="PATH", help="use an existing report instead of running the suite")
    parser.add_argument("--baseline", metavar="PATH", default=str(DEFAULT_BASELINE))
    parser.add_argument("--save-baseline", action="store_true", help="store the current report as the new baseline")
    parser.add_argument("run_full_args", nargs=argparse.REMAINDER,
                        help="passed through to run_full.py when --report is not given "
                             "(e.g. -- --no-llm --no-rubric)")
    args = parser.parse_args(argv)

    passthrough = [a for a in args.run_full_args if a != "--"]
    current = json.loads(Path(args.report).read_text(encoding="utf-8")) if args.report else _run_suite(passthrough)

    baseline_path = Path(args.baseline)
    if args.save_baseline:
        baseline_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(f"baseline saved: {baseline_path}  "
              f"(core recall {current['summary'].get('core_recall_pct')}%, "
              f"rubric mean {current['summary'].get('rubric_mean')})")
        return 0

    if not baseline_path.is_file():
        print(f"no baseline at {baseline_path} — run with --save-baseline first")
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    result = compare(current, baseline)

    print("=" * 80)
    print(f"GATE: {'PASS' if result.passed else 'FAIL'}")
    print(f"  baseline: {baseline.get('generated_at', '?')}   current: {current.get('generated_at', '?')}")
    if result.reasons:
        print("\nfailing reasons:")
        for reason in result.reasons:
            print(f"  - {reason}")
    if result.notes:
        print("\nnotes (did not fail the gate):")
        for note in result.notes:
            print(f"  - {note}")
    print("=" * 80)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
