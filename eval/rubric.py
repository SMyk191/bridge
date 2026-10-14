"""eval/rubric.py — an independent LLM judge for one answer.

    .venv/bin/python eval/rubric.py <run_id>          # score a persisted run
    .venv/bin/python eval/rubric.py <run_id> --json

Scores `correctness` / `completeness` / `actionability`, 1-5 each, and lists any
claim in the answer the evidence does not support. "Evidence" is not the model's
memory of what it read — it is the actual source text at every citation the
answer made, fetched **independently** here, from the repository, at the commit
the answer names (`git show <sha>:<path>`, not the current working tree, which
may have moved on). A citation this fetch cannot resolve — wrong commit, no such
file, a line range past the end of it — is never shown to the judge as evidence
at all; it is listed up front as a citation that failed to check out, which the
judge is told counts as unsupported, not as evidence for anything.

**The floor is enforced here, in code, not requested in the prompt** (design law
1: Python decides, the model reasons inside the node). If the judge's own JSON
lists any unsupported claim, `score_answer` overrides whatever correctness
number it returned down to `config.RUBRIC_UNSUPPORTED_FLOOR` — a model that
notices a fabrication and still reports a 4 is not treated as an authority on
its own score.

The judge runs on a different provider than the answer-writer (`config.py`'s
`ROLE_PROVIDER["judge"]`) so grading does not share the writer's blind spots.
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
from bridge.agents.interpreter import parse_json_object  # noqa: E402
from bridge.agents.llm import LLMClient, load_prompt, wrap_retrieved  # noqa: E402
from bridge.agents.state import citations_of  # noqa: E402
from bridge.orchestrator import RunStore  # noqa: E402
from bridge.orchestrator.verify import parse_citation  # noqa: E402
from bridge.retrieval.retrieve import _resolve_index_dir, repo_root_of  # noqa: E402

#: lines of surrounding context shown either side of a cited range — a citation
#: proves a claim about a method, and a method usually needs its signature and
#: closing brace to read as more than a fragment
CONTEXT_LINES = 2

UNRESOLVED_SHA = "names a commit `git show` at this repo root could not read"
UNRESOLVED_PATH = "no such file at that commit"
UNRESOLVED_RANGE = "the cited lines fall outside the file"
UNRESOLVED_FORM = "is not a citation this system could have written"


@dataclass(slots=True)
class EvidenceSpan:
    citation: str
    path: str
    start: int | None
    end: int | None
    code: str | None = None
    #: set instead of `code` when the citation did not resolve
    problem: str | None = None

    @property
    def resolved(self) -> bool:
        return self.code is not None


@dataclass(slots=True)
class RubricScore:
    correctness: int | None = None
    completeness: int | None = None
    actionability: int | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    reasoning: str = ""
    unresolved_citations: list[tuple[str, str]] = field(default_factory=list)  # (citation, why)
    raw_correctness: int | None = None  # before the floor was applied, for audit
    model: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    #: False when no provider could be reached, or the reply was not usable —
    #: an unscored run must never silently read as a passing (or failing) one
    scored: bool = False
    error: str | None = None

    @property
    def mean(self) -> float | None:
        if not self.scored:
            return None
        return round((self.correctness + self.completeness + self.actionability) / 3, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "error": self.error,
            "correctness": self.correctness,
            "completeness": self.completeness,
            "actionability": self.actionability,
            "mean": self.mean,
            "raw_correctness": self.raw_correctness,
            "unsupported_claims": self.unsupported_claims,
            "reasoning": self.reasoning,
            "unresolved_citations": [
                {"citation": c, "reason": r} for c, r in self.unresolved_citations
            ],
            "model": self.model,
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _git_show(repo_root: Path, sha: str, path: str) -> str | None:
    """The file's content at `sha`, or None. Reads history, not the checkout —
    a working tree can be anywhere by the time a run is graded."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{sha}:{path}"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def fetch_span(repo_root: Path | None, citation: str) -> EvidenceSpan:
    """Resolve one citation against the repository, independently of anything
    the run itself claimed about it."""
    parsed = parse_citation(citation)
    if parsed is None:
        return EvidenceSpan(citation, path="", start=None, end=None, problem=UNRESOLVED_FORM)
    _repo, sha, path, start, end = parsed

    if repo_root is None or not repo_root.is_dir():
        return EvidenceSpan(citation, path, start, end, problem="the indexed repository is not on disk")

    content = _git_show(repo_root, sha, path)
    if content is None:
        # Could be the sha (a citation naming a commit this repo never had) or
        # the path (a real commit, but no such file in it). Distinguish by
        # checking the sha's own validity, not by re-probing at HEAD — HEAD may
        # have since renamed or deleted the file, which would misreport a
        # perfectly good historical citation as a bad one.
        sha_is_real = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, check=False,
        ).returncode == 0
        return EvidenceSpan(
            citation, path, start, end,
            problem=UNRESOLVED_PATH if sha_is_real else UNRESOLVED_SHA,
        )

    if start is None:
        # A changelog-style whole-file/commit citation: show a short head of the
        # file rather than the whole thing, so evidence stays bounded either way.
        lines = content.splitlines()
        return EvidenceSpan(citation, path, None, None, code="\n".join(lines[:40]))

    lines = content.splitlines()
    if start > len(lines):
        return EvidenceSpan(citation, path, start, end, problem=UNRESOLVED_RANGE)
    lo = max(0, start - 1 - CONTEXT_LINES)
    hi = min(len(lines), (end or start) + CONTEXT_LINES)
    return EvidenceSpan(citation, path, start, end, code="\n".join(lines[lo:hi]))


def gather_evidence(citations: list[str], repo_root: Path | None) -> list[EvidenceSpan]:
    return [fetch_span(repo_root, citation) for citation in citations]


def build_prompt(question: str, answer: str, spans: list[EvidenceSpan]) -> str:
    resolved = [s for s in spans if s.resolved]
    unresolved = [s for s in spans if not s.resolved]

    parts = [
        wrap_retrieved(f"QUESTION:\n{question}"),
        wrap_retrieved(f"ANSWER:\n{answer}"),
    ]
    if resolved:
        evidence_text = "\n\n".join(
            f"--- {span.citation} ---\n{span.code}" for span in resolved
        )
        parts.append(wrap_retrieved(f"EVIDENCE (resolved citations):\n{evidence_text}"))
    else:
        parts.append("EVIDENCE (resolved citations): none — every citation failed to resolve.")
    if unresolved:
        lines = "\n".join(f"- {span.citation}: {span.problem}" for span in unresolved)
        parts.append(
            "CITATIONS THAT FAILED TO RESOLVE (treat any claim relying only on one of "
            f"these as unsupported):\n{lines}"
        )
    return "\n\n".join(parts)


def score_answer(
    question: str, answer: str, citations: list[str], repo_root: Path | None, llm: LLMClient,
) -> RubricScore:
    if not answer.strip():
        return RubricScore(scored=False, error="the run produced no answer text to grade")
    if not llm.available_for("judge"):
        return RubricScore(scored=False, error=f"no judge provider available: {llm.reason_for('judge')}")

    spans = gather_evidence(citations, repo_root)
    unresolved = [(s.citation, s.problem or "") for s in spans if not s.resolved]
    prompt = build_prompt(question, answer, spans)

    try:
        response = llm.complete("judge", prompt, system=load_prompt("judge"), json_mode=True)
    except Exception as exc:  # a dead provider must produce an unscored result, not a crash
        return RubricScore(
            scored=False, error=f"{exc.__class__.__name__}: {exc}",
            unresolved_citations=unresolved,
        )

    parsed = parse_json_object(response.text)
    if parsed is None:
        return RubricScore(
            scored=False, error="judge reply was not valid JSON", model=response.model,
            cost_usd=response.cost_usd, input_tokens=response.input_tokens,
            output_tokens=response.output_tokens, unresolved_citations=unresolved,
        )

    def _score(key: str) -> int | None:
        value = parsed.get(key)
        if not isinstance(value, (int, float)):
            return None
        lo, hi = config.RUBRIC_SCALE
        return max(lo, min(hi, round(value)))

    scores = {dim: _score(dim) for dim in config.RUBRIC_DIMENSIONS}
    if any(v is None for v in scores.values()):
        missing = [dim for dim, v in scores.items() if v is None]
        return RubricScore(
            scored=False, error=f"judge reply was missing or non-numeric: {missing}",
            model=response.model, cost_usd=response.cost_usd,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            unresolved_citations=unresolved,
        )

    unsupported = [str(c) for c in (parsed.get("unsupported_claims") or []) if str(c).strip()]
    # citations that never resolved are themselves unsupported evidence, whether
    # or not the judge's own prose happened to mention them
    if unresolved and not unsupported:
        unsupported = [f"relies on citation {c}, which did not resolve ({why})" for c, why in unresolved]

    raw_correctness = scores["correctness"]
    correctness = (
        min(raw_correctness, config.RUBRIC_UNSUPPORTED_FLOOR) if unsupported else raw_correctness
    )

    return RubricScore(
        correctness=correctness,
        completeness=scores["completeness"],
        actionability=scores["actionability"],
        unsupported_claims=unsupported,
        reasoning=str(parsed.get("reasoning") or ""),
        unresolved_citations=unresolved,
        raw_correctness=raw_correctness,
        model=response.model,
        cost_usd=response.cost_usd,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        scored=True,
    )


def score_run(run_id: str, store: RunStore | None = None, llm: LLMClient | None = None) -> RubricScore:
    """Score a persisted run by id — the standalone entry point."""
    owned_store = store is None
    store = store or RunStore.open()
    try:
        state = store.state_of(run_id)
        if state is None:
            return RubricScore(scored=False, error=f"no such run: {run_id!r}")
        llm = llm or LLMClient()
        repo_root = repo_root_of(_resolve_index_dir(state.get("sha") or None))
        return score_answer(
            question=state.get("request") or "",
            answer=state.get("answer") or "",
            citations=citations_of(state),
            repo_root=repo_root,
            llm=llm,
        )
    finally:
        if owned_store:
            store.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="eval/rubric.py", description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    score = score_run(args.run_id)
    if args.json:
        print(json.dumps(score.as_dict(), indent=2))
        return 0 if score.scored else 1

    if not score.scored:
        print(f"UNSCORED: {score.error}")
        return 1
    print(f"correctness   {score.correctness}/5" + (f"  (raw {score.raw_correctness}, "
          f"floored for {len(score.unsupported_claims)} unsupported claim(s))"
          if score.raw_correctness != score.correctness else ""))
    print(f"completeness  {score.completeness}/5")
    print(f"actionability {score.actionability}/5")
    print(f"mean          {score.mean}")
    if score.unsupported_claims:
        print("\nunsupported claims:")
        for claim in score.unsupported_claims:
            print(f"  - {claim}")
    if score.unresolved_citations:
        print("\ncitations that failed to resolve:")
        for citation, why in score.unresolved_citations:
            print(f"  - {citation}: {why}")
    print(f"\nreasoning: {score.reasoning}")
    print(f"judge: {score.model}  ${score.cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
