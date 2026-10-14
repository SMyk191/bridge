"""eval/rubric.py — span fetching against a real git repo, and the judge's
floor rule enforced in code rather than trusted from the prompt.

Span fetching runs against a real throwaway git repo (never the indexed
target repo — Bridge doesn't write to those, and neither does its eval
tooling) because `git show <sha>:<path>` is exactly the behaviour under test:
whether a citation resolves has to be checked against real git plumbing, not
asserted. The judge call itself is stubbed — a fake with `available_for` and
`complete`, not a real `LLMClient` — because the floor rule
(`RUBRIC_UNSUPPORTED_FLOOR`) is Python deciding over whatever a model reports,
and that decision has to be provably correct independent of whether either
live provider can currently be reached (see PROGRESS.md: neither can, right
now — this is exactly the part of the harness that stays verifiable anyway).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import rubric

from bridge import config
from bridge.agents.llm import LLMResponse
from bridge.orchestrator.verify import parse_citation


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    body = "\n".join(f"line {i}" for i in range(1, 21))
    (root / "Foo.java").write_text(body + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD")
    return root, sha


# ------------------------------------------------------------------ fetch_span


def test_a_real_span_resolves_with_context_lines(repo):
    root, sha = repo
    span = rubric.fetch_span(root, f"demo@{sha}:Foo.java:5-7")
    assert span.resolved
    assert "line 5" in span.code and "line 7" in span.code
    # CONTEXT_LINES padding either side, without running off the file
    assert "line 3" in span.code and "line 9" in span.code


def test_a_span_at_the_very_start_does_not_ask_for_negative_lines(repo):
    root, sha = repo
    span = rubric.fetch_span(root, f"demo@{sha}:Foo.java:1-1")
    assert span.resolved
    assert "line 1" in span.code


def test_a_line_range_past_the_end_of_the_file_does_not_resolve(repo):
    root, sha = repo
    span = rubric.fetch_span(root, f"demo@{sha}:Foo.java:500-510")
    assert not span.resolved
    assert span.problem == rubric.UNRESOLVED_RANGE


def test_a_path_that_never_existed_does_not_resolve(repo):
    root, sha = repo
    span = rubric.fetch_span(root, f"demo@{sha}:Nope.java:1-2")
    assert not span.resolved
    assert span.problem == rubric.UNRESOLVED_PATH


def test_a_citation_at_a_commit_this_repo_never_saw_does_not_resolve(repo):
    root, _sha = repo
    span = rubric.fetch_span(root, f"demo@{'a' * 40}:Foo.java:1-2")
    assert not span.resolved
    assert span.problem == rubric.UNRESOLVED_SHA


def test_a_malformed_citation_is_reported_without_touching_git(repo):
    root, _sha = repo
    span = rubric.fetch_span(root, "not a citation at all")
    assert not span.resolved
    assert span.problem == rubric.UNRESOLVED_FORM


def test_no_repository_on_disk_is_reported_not_crashed(repo):
    _root, sha = repo
    span = rubric.fetch_span(None, f"demo@{sha}:Foo.java:1-2")
    assert not span.resolved
    assert "not on disk" in span.problem


def test_a_changelog_style_citation_with_no_line_range_shows_a_bounded_head(repo):
    root, sha = repo
    span = rubric.fetch_span(root, f"demo@{sha}:Foo.java")
    assert span.resolved
    assert span.start is None and span.end is None
    assert "line 1" in span.code


def test_gather_evidence_preserves_order_and_resolves_independently(repo):
    root, sha = repo
    spans = rubric.gather_evidence(
        [f"demo@{sha}:Foo.java:1-2", f"demo@{sha}:Nope.java:1-2", f"demo@{sha}:Foo.java:5-6"], root,
    )
    assert [s.resolved for s in spans] == [True, False, True]


# ------------------------------------------------------------------ build_prompt


def test_the_prompt_wraps_resolved_and_unresolved_evidence_separately(repo):
    root, sha = repo
    spans = [rubric.fetch_span(root, f"demo@{sha}:Foo.java:1-2"),
             rubric.fetch_span(root, f"demo@{sha}:Nope.java:1-2")]
    prompt = rubric.build_prompt("q", "a", spans)
    assert "EVIDENCE (resolved citations)" in prompt
    assert "CITATIONS THAT FAILED TO RESOLVE" in prompt
    assert "Nope.java" in prompt
    assert "<retrieved>" in prompt  # design law 5


def test_no_resolved_evidence_says_so_plainly(repo):
    root, sha = repo
    spans = [rubric.fetch_span(root, f"demo@{sha}:Nope.java:1-2")]
    prompt = rubric.build_prompt("q", "a", spans)
    assert "none — every citation failed to resolve" in prompt


# ------------------------------------------------------------------ score_answer


@dataclass
class FakeJudge:
    """Duck-types the two calls score_answer makes on an LLMClient — no real
    provider involved, so the floor rule is checked independent of whether one
    is reachable."""

    reply: dict | str
    available: bool = True

    def available_for(self, role: str) -> bool:
        return self.available

    def reason_for(self, role: str) -> str:
        return "stub: not available"

    def complete(self, role, prompt, system=None, *, max_tokens=None, json_mode=False) -> LLMResponse:
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return LLMResponse(
            role=role, model="stub-model", text=text, input_tokens=10, output_tokens=5,
            cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.001, latency_s=0.01,
        )


def test_a_clean_answer_scores_normally(repo):
    root, sha = repo
    judge = FakeJudge({"correctness": 5, "completeness": 4, "actionability": 4,
                       "unsupported_claims": [], "reasoning": "solid"})
    score = rubric.score_answer("q", "a", [f"demo@{sha}:Foo.java:1-2"], root, judge)
    assert score.scored
    assert (score.correctness, score.completeness, score.actionability) == (5, 4, 4)
    assert score.correctness == score.raw_correctness
    assert score.unsupported_claims == []


def test_unsupported_claims_floor_correctness_even_if_the_model_scored_it_high():
    """The rule design law 1 requires: Python decides, the model reasons. A
    judge that lists a fabrication and still reports a 5 does not get to keep
    the 5."""
    judge = FakeJudge({"correctness": 5, "completeness": 4, "actionability": 3,
                       "unsupported_claims": ["claims X does Y, which the evidence contradicts"],
                       "reasoning": "one claim doesn't hold up"})
    score = rubric.score_answer("q", "a", [], None, judge)
    assert score.scored
    assert score.correctness == config.RUBRIC_UNSUPPORTED_FLOOR
    assert score.raw_correctness == 5
    assert score.completeness == 4  # only correctness is floored


def test_a_citation_that_never_resolved_counts_as_unsupported_even_if_the_judge_says_nothing():
    """The judge is never shown a failed citation as evidence — if it doesn't
    independently flag anything, the checker still must not let that citation's
    claim through for free."""
    judge = FakeJudge({"correctness": 5, "completeness": 5, "actionability": 5,
                       "unsupported_claims": [], "reasoning": "looks fine"})
    score = rubric.score_answer("q", "a", ["not a real citation"], None, judge)
    assert score.correctness == config.RUBRIC_UNSUPPORTED_FLOOR
    assert score.unsupported_claims  # synthesised from the unresolved citation
    assert score.unresolved_citations


def test_scores_out_of_range_are_clamped_not_trusted_verbatim():
    judge = FakeJudge({"correctness": 9, "completeness": 0, "actionability": 3,
                       "unsupported_claims": [], "reasoning": "x"})
    score = rubric.score_answer("q", "a", [], None, judge)
    lo, hi = config.RUBRIC_SCALE
    assert score.correctness == hi
    assert score.completeness == lo


def test_a_reply_missing_a_dimension_is_unscored_not_defaulted():
    judge = FakeJudge({"correctness": 4, "completeness": 4, "reasoning": "x"})  # no actionability
    score = rubric.score_answer("q", "a", [], None, judge)
    assert not score.scored
    assert "actionability" in score.error


def test_a_reply_that_is_not_json_is_unscored_not_a_crash():
    judge = FakeJudge("not json at all")
    score = rubric.score_answer("q", "a", [], None, judge)
    assert not score.scored
    assert "not valid JSON" in score.error


def test_no_provider_available_is_unscored_and_says_why():
    judge = FakeJudge({}, available=False)
    score = rubric.score_answer("q", "a", [], None, judge)
    assert not score.scored
    assert "stub: not available" in score.error


def test_empty_answer_text_is_unscored_without_even_asking_the_judge():
    judge = FakeJudge({"correctness": 5, "completeness": 5, "actionability": 5,
                       "unsupported_claims": [], "reasoning": "x"})
    score = rubric.score_answer("q", "   ", [], None, judge)
    assert not score.scored
    assert "no answer text" in score.error


def test_mean_is_none_until_scored():
    assert rubric.RubricScore().mean is None
    assert rubric.RubricScore(correctness=3, completeness=3, actionability=3, scored=True).mean == 3.0
