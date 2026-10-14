"""eval/gate.py::compare — pure report-dict comparison, no orchestrator, no LLM.

This is deliberately synthetic. Both providers this machine has credentials
for are currently unable to complete a call (Gemini: monthly spend cap;
Anthropic: credit balance too low — both verified live, PROGRESS.md), so a
real "answer got worse, judge caught it, gate failed" demonstration cannot be
produced end to end in this session. What CAN be verified for real, without a
model, is the thing that actually decides pass/fail: the comparison itself.
These tests build the two reports gate.py would be handed by a real run — one
healthy, one regressed in each of the three documented ways — and assert the
verdict, so the regression-detection logic is proven even though no live
answer was scored to produce it.
"""

from __future__ import annotations

import gate
from bridge import config


def _report(*, core_recall=90.0, rubric_mean=4.0, results=None, generated_at="t") -> dict:
    return {
        "generated_at": generated_at,
        "summary": {"core_recall_pct": core_recall, "rubric_mean": rubric_mean},
        "results": results or [],
    }


def _det(id_: str, checked: bool, stable: bool | None) -> dict:
    return {"id": id_, "determinism": {"checked": checked, "stable": stable}}


# ------------------------------------------------------------------ the healthy case


def test_an_identical_rerun_passes_with_no_notes():
    baseline = _report()
    current = _report()
    result = gate.compare(current, baseline)
    assert result.passed
    assert result.reasons == []


def test_a_small_recall_dip_within_threshold_passes_but_is_noted():
    baseline = _report(core_recall=90.0)
    current = _report(core_recall=90.0 - config.GATE_CORE_RECALL_DROP_PTS + 0.1)
    result = gate.compare(current, baseline)
    assert result.passed
    assert result.notes and "core-file recall down" in result.notes[0]


def test_an_improvement_passes_with_no_notes_at_all():
    baseline = _report(core_recall=80.0, rubric_mean=3.5)
    current = _report(core_recall=95.0, rubric_mean=4.2)
    result = gate.compare(current, baseline)
    assert result.passed
    assert result.notes == []


# ------------------------------------------------------------------ (1) core recall


def test_a_core_recall_drop_past_the_threshold_fails():
    baseline = _report(core_recall=90.0)
    current = _report(core_recall=90.0 - config.GATE_CORE_RECALL_DROP_PTS - 0.1)
    result = gate.compare(current, baseline)
    assert not result.passed
    assert any("core-file recall dropped" in r for r in result.reasons)


def test_exactly_at_the_threshold_still_passes():
    """The stated rule is a drop OVER the threshold — exactly on the line is
    not yet a failure, and a boundary this exact is worth pinning directly."""
    baseline = _report(core_recall=90.0)
    current = _report(core_recall=90.0 - config.GATE_CORE_RECALL_DROP_PTS)
    result = gate.compare(current, baseline)
    assert result.passed


def test_core_recall_going_unmeasured_fails_rather_than_passing_silently():
    """A baseline that measured core recall and a current run that cannot
    (no question defines core_files) has not proven anything — passing it
    quietly would let a gutted question set sail through."""
    baseline = _report(core_recall=90.0)
    current = _report(core_recall=None)
    result = gate.compare(current, baseline)
    assert not result.passed
    assert any("not measured now" in r for r in result.reasons)


# ------------------------------------------------------------------ (2) rubric mean


def test_a_rubric_drop_past_the_threshold_fails():
    baseline = _report(rubric_mean=4.0)
    current = _report(rubric_mean=4.0 - config.GATE_RUBRIC_DROP - 0.01)
    result = gate.compare(current, baseline)
    assert not result.passed
    assert any("rubric mean dropped" in r for r in result.reasons)


def test_a_rubric_drop_within_threshold_passes_but_is_noted():
    baseline = _report(rubric_mean=4.0)
    current = _report(rubric_mean=4.0 - config.GATE_RUBRIC_DROP + 0.01)
    result = gate.compare(current, baseline)
    assert result.passed
    assert any("rubric mean down" in n for n in result.notes)


def test_rubric_going_unmeasured_is_a_note_not_a_failure():
    """Unmeasured is a different claim from regressed — a run with no judge
    reachable (or --no-rubric) cannot confirm quality held, but that is not
    itself evidence that it didn't. Conflating the two would fail --no-llm
    verification runs for a reason that has nothing to do with quality."""
    baseline = _report(rubric_mean=4.0)
    current = _report(rubric_mean=None)
    result = gate.compare(current, baseline)
    assert result.passed
    assert any("unmeasured now" in n for n in result.notes)


# ------------------------------------------------------------------ (3) determinism


def test_a_question_that_was_stable_and_is_not_anymore_fails():
    baseline = _report(results=[_det("add-pet", True, True)])
    current = _report(results=[_det("add-pet", True, False)])
    result = gate.compare(current, baseline)
    assert not result.passed
    assert any("determinism regressed" in r and "add-pet" in r for r in result.reasons)


def test_a_question_that_was_never_checked_cannot_regress():
    """A threaded question is determinism-`checked: false` by design
    (run_full.py) — the gate must not manufacture a failure out of a
    comparison that was never meaningful."""
    baseline = _report(results=[_det("thread-visit-2", False, None)])
    current = _report(results=[_det("thread-visit-2", False, None)])
    result = gate.compare(current, baseline)
    assert result.passed


def test_a_question_missing_from_the_baseline_is_not_penalised():
    """A newly added question has no baseline row to regress against."""
    current = _report(results=[_det("brand-new-question", True, False)])
    baseline = _report(results=[])
    result = gate.compare(current, baseline)
    assert result.passed


def test_multiple_failures_are_all_reported_not_just_the_first():
    baseline = _report(core_recall=90.0, rubric_mean=4.0,
                       results=[_det("add-pet", True, True)])
    current = _report(
        core_recall=90.0 - config.GATE_CORE_RECALL_DROP_PTS - 5,
        rubric_mean=4.0 - config.GATE_RUBRIC_DROP - 1,
        results=[_det("add-pet", True, False)],
    )
    result = gate.compare(current, baseline)
    assert not result.passed
    assert len(result.reasons) == 3
