"""Interpreter — normalise the request and classify intent.

The cheapest node, and the only one allowed to stop the run by asking a question.
It is told to prefer proceeding with a stated assumption: a clarifying question
costs a whole round trip with the human, and most ambiguity in a product request
is resolvable by saying what you assumed.

Its `intent` is an *input* to routing, not the routing decision. The lane is set
after the cartographer, from the impact map (design law 3).

**Voting.** Temperature 0 (config.DEFAULT_TEMPERATURE) removes most sampling
variance, not all of it — and `intent` sits on a few genuinely ambiguous
boundaries (earlier phases kept observing the same one: "how many owners
registered last month" landing on `question` some of the time when it should
redirect as `data_question`). So this node takes `config.INTERPRETER_VOTES`
independent votes and keeps the majority `intent`, rather than trusting whichever
single call happened to run. A vote that does not unanimously agree with the
others is recorded — not silently resolved and forgotten — because a disagreement
here is exactly the signal worth mining later: which phrasings are still
ambiguous even at temperature 0.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .. import config
from .llm import LLMClient, Usage, load_prompt, wrap_retrieved
from .state import Interpretation, RunState

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from a model reply, tolerating code fences and prose.

    Structured output is requested, but a model can still wrap or preface it —
    and a run should not fail on punctuation.
    """
    if not text:
        return None
    cleaned = _FENCE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, depth = None, 0
    for index, char in enumerate(cleaned):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(cleaned[start : index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    start = None
    return None


def guess_intent(text: str) -> str:
    """Keyword-based, deliberately crude — the classification `_fallback` uses
    when no model is available, and also `orchestrator/cache.py`'s cheap proxy
    for "which lane would this land in" at semantic-cache lookup time, before
    paying for the real interpreter+cartographer pass that would answer that
    properly. Good enough for a coarse same-lane gate; not a substitute for
    the real classification anywhere an actual answer is being written.
    """
    text = text.lower()
    if any(word in text for word in ("how many", "average", "count of", "total number", "last month")):
        return "data_question"
    if any(word in text for word in ("isn't", "is not", "broken", "wrong", "fails", "bug", "error")):
        return "bug"
    if any(word in text for word in ("add ", "we add", "change", "remove", "introduce", "support ")):
        return "change_request"
    return "question"


def _fallback(state: RunState, reason: str) -> Interpretation:
    """Deterministic reading when the model is unavailable or unparseable.

    Keyword-based and deliberately crude — it exists so the pipeline degrades to
    *something honest* rather than dying, and it records why in `reasoning`.
    """
    intent = guess_intent(state.request)
    return Interpretation(
        normalised_request=state.request,
        intent=intent,
        reasoning=f"keyword fallback ({reason})",
        assumptions=[f"intent inferred without a model: {reason}"],
    )


def _vote_once(state: RunState) -> str:
    """The prompt every vote sends — identical across votes, since the only
    thing that should vary between them is the model's own sampling."""
    context = ""
    if state.prior_context:
        context = (
            "\nEarlier in this thread we established:\n"
            f"{wrap_retrieved(state.prior_context)}\n"
        )
    return f"Request:\n{state.request}\n{context}\nClassify and normalise it."


def _cast_interpretation(parsed: dict[str, Any], state: RunState) -> Interpretation:
    intent = str(parsed.get("intent", "question")).strip()
    if intent not in config.INTENTS:
        intent = "question"
    question = parsed.get("clarifying_question")
    if isinstance(question, str) and not question.strip():
        question = None
    return Interpretation(
        normalised_request=str(parsed.get("normalised_request") or state.request),
        intent=intent,
        entities=[str(e) for e in (parsed.get("entities") or [])],
        clarifying_question=question,
        assumptions=[str(a) for a in (parsed.get("assumptions") or [])],
        reasoning=str(parsed.get("reasoning") or ""),
    )


def run(state: RunState, llm: LLMClient) -> RunState:
    """Populate ``state.interpretation`` from up to ``config.INTERPRETER_VOTES`` votes."""
    if not llm.available_for("interpreter"):
        state.interpretation = _fallback(state, llm.reason_for("interpreter") or "no model")
        return state

    prompt = _vote_once(state)
    system = load_prompt("interpreter")
    votes: list[Interpretation] = []
    errors: list[str] = []

    for _ in range(config.INTERPRETER_VOTES):
        if state.budget.exceeded(state.usage):
            break
        try:
            response = llm.complete("interpreter", prompt, system=system, json_mode=True)
            state.usage.add(_usage_of(response))
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")
            continue
        parsed = parse_json_object(response.text)
        if parsed is None:
            errors.append("model reply was not JSON")
            continue
        votes.append(_cast_interpretation(parsed, state))

    if not votes:
        state.interpretation = _fallback(state, "; ".join(errors) or "all interpreter votes failed")
        return state

    distinct = {vote.intent for vote in votes}
    if len(distinct) == 1:
        state.interpretation = votes[0]
        return state

    # A real disagreement: more than one intent survived. Majority wins — e.g.
    # [question, question, bug] chooses `question`, 2 of 3. A true tie (no
    # intent strictly ahead — three distinct votes, or an even split after a
    # vote failed) falls back to the FIRST vote, deterministically, rather than
    # to any other tie-break that would need its own justification. Either way
    # the disagreement is recorded, not silently resolved and forgotten — the
    # orchestrator writes `state.disagreements` to the `eval_candidates` table
    # (orchestrator/run.py).
    counts = Counter(vote.intent for vote in votes)
    ranked = counts.most_common()
    top_intent, top_count = ranked[0]
    tied = len(ranked) > 1 and ranked[1][1] == top_count
    chosen_intent = votes[0].intent if tied else top_intent

    state.interpretation = next(vote for vote in votes if vote.intent == chosen_intent)
    state.disagreements.append(
        {
            "node": "interpreter",
            "field": "intent",
            "candidates": [vote.intent for vote in votes],
            "chosen": chosen_intent,
            "is_majority": not tied,
            "reason": (
                "no vote reached a majority; the first vote's intent was used"
                if tied
                else f"{top_count} of {len(votes)} votes agreed"
            ),
        }
    )
    return state


def _usage_of(response) -> Any:
    return Usage(
        calls=1,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        thinking_tokens=response.thinking_tokens,
        cost_usd=response.cost_usd,
        by_role={response.role: 1},
        by_provider={response.provider: 1},
    )
