"""Cartographer — call `retrieve()` and turn the evidence into an impact map.

Two stages, and the split is the point:

* **fan out for reading.** When more than one module is implicated, each module
  is summarised independently by the cheap tier. A module summary only sees its
  own chunks, so no module's evidence can crowd out another's — which is exactly
  what happens when twenty mixed chunks go into one prompt and the biggest module
  wins on volume alone.
* **single-thread the merge.** One deep-tier call sees the request, every module
  summary, and every chunk, and produces the map. Merging is where judgement
  lives, so it happens once, in one place (design law 2).

With one module there is nothing to fan out over and the summary stage is skipped.
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..index.embed import module_of
from ..redact import RedactionLog
from ..retrieval.retrieve import Retriever
from .interpreter import parse_json_object
from .llm import LLMClient, Usage, load_prompt, wrap_retrieved
from .state import ImpactEntry, ImpactMap, RunState, citation_path


def _usage_of(response) -> Usage:
    return Usage(
        calls=1,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        thinking_tokens=response.thinking_tokens,
        cost_usd=response.cost_usd,
        by_role={response.role: 1},
        by_provider={response.provider: 1},
    )


def _render_chunk(chunk, redaction: RedactionLog) -> str:
    history = chunk.history or {}
    churn = (
        f" [{history.get('commits_all_time', 0)} commits, "
        f"{history.get('commits_recent', 0)} recent]"
        if history
        else ""
    )
    return redaction.scrub(
        f"- [{chunk.kind}] {chunk.name}{churn}\n"
        f"  {chunk.description}\n"
        f"  citation: {chunk.citation}"
    )


def _group_by_module(chunks) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for chunk in chunks:
        grouped.setdefault(module_of(chunk.path), []).append(chunk)
    return grouped


#: what a reader is told when the merge step could not run. The provider's own
#: error text is NOT this: a 503's JSON body was reaching the answer through
#: `gaps`, and from there into the "what would make me more sure" line, where a
#: product reader was shown a stack trace as though it were a fact about their
#: codebase. The technical detail belongs in the trace, and stays there.
FALLBACK_GAP = (
    "nothing reasoned about which of these pieces actually matter — the mapping "
    "step did not run, so this is the raw search result rather than an impact map"
)


def _fallback_map(state: RunState, reason: str) -> ImpactMap:
    """Build a map from retrieval alone, with no model.

    Groups by node kind mechanically and says plainly that nothing reasoned about
    relevance — confidence is `low` and the reason is in `gaps`.
    """
    state.env["cartographer_fallback"] = reason
    impact = ImpactMap(
        summary="No model was available to build an impact map; "
        "these are the highest-scoring retrieved chunks, ungrouped by relevance.",
        confidence="low",
        confidence_reasons=[f"no impact map was reasoned ({reason.split(':')[0]})"],
        gaps=[FALLBACK_GAP],
    )
    retrieval = state.retrieval
    if retrieval is None:
        return impact
    buckets = {
        "endpoint": impact.endpoints,
        "entity": impact.entities_tables,
        "screen": impact.screens,
    }
    for chunk in retrieval.chunks:
        entry = ImpactEntry(
            name=chunk.name,
            citation=chunk.citation,
            why=chunk.description or "retrieved for this request",
            node_id=chunk.node_id,
        )
        buckets.get(chunk.kind, impact.classes_methods).append(entry)
    impact.modules = sorted({module_of(c.path) for c in retrieval.chunks})
    return impact


def question_for(state: RunState) -> str:
    """The question every retrieval and every prompt in this run is built from."""
    question = (
        state.interpretation.normalised_request
        if state.interpretation and state.interpretation.normalised_request
        else state.request
    )
    if state.prior_context:
        question = f"{question}\n(Earlier in this thread: {state.prior_context[:400]})"
    return question


def run(state: RunState, llm: LLMClient, retriever: Retriever) -> RunState:
    """Populate ``state.retrieval`` and ``state.impact_map``."""
    question = question_for(state)

    state.retrieval = retriever.retrieve(question, state.budget, llm=llm)
    state.usage.add(
        Usage(cost_usd=state.retrieval.cost_usd, by_role={"rerank": 1 if state.retrieval.reranked else 0})
    )
    return build_map(state, llm, question)


def build_map(
    state: RunState, llm: LLMClient, question: str | None = None, emphasis: str = ""
) -> RunState:
    """Turn whatever is in ``state.retrieval`` into ``state.impact_map``.

    Split out from :func:`run` so a follow-up retrieval can rebuild the map over
    the enlarged evidence (orchestrator/followup.py). Without that, extra chunks
    would sit in `state.retrieval` unread: the architect builds its brief from
    the *map*, and recall is measured on the map's citations, so evidence that
    never reaches the map may as well not have been fetched.
    """
    question = question if question is not None else question_for(state)

    if state.retrieval is None or not state.retrieval.chunks:
        state.impact_map = ImpactMap(
            summary="Nothing in the indexed repository matched this request.",
            confidence="low",
            gaps=["retrieval returned no chunks; the request may be about a different system"],
        )
        return state

    if not llm.available_for("cartographer_merge"):
        state.impact_map = _fallback_map(state, llm.reason_for("cartographer_merge") or "no model")
        return state

    redaction = RedactionLog()
    by_module = _group_by_module(state.retrieval.chunks)

    # -- fan out: one cheap summary per module, only when there is more than one
    module_summaries: dict[str, str] = {}
    if len(by_module) > 1 and llm.available_for("cartographer"):
        system = load_prompt("cartographer_summary")
        for module, chunks in sorted(by_module.items()):
            if state.budget.exceeded(state.usage):
                break
            body = "\n".join(_render_chunk(c, redaction) for c in chunks)
            prompt = (
                f"Request: {question}\n\nModule `{module}`:\n"
                f"{wrap_retrieved(body)}\n\nSummarise this module's role."
            )
            try:
                response = llm.complete("cartographer", prompt, system=system)
                state.usage.add(_usage_of(response))
                if response.text:
                    module_summaries[module] = response.text
            except Exception:
                state.usage.errors += 1

    # -- single-thread the merge
    lines = []
    for module, chunks in sorted(by_module.items()):
        lines.append(f"\n## module: {module}")
        if module in module_summaries:
            lines.append(f"summary: {module_summaries[module]}")
        lines.extend(_render_chunk(c, redaction) for c in chunks)

    # `emphasis` is set by orchestrator/followup.py when a rule found the evidence
    # one-sided and went back for the other half. It is OUR instruction, decided
    # in Python, so it sits outside the <retrieved> block: the merge otherwise has
    # no way to know that a bug answer is judged on covering both sides of a flow,
    # and will map the half the question was phrased around. Observed on
    # `visit-missing`, where the POST handler that saves a visit sat at rank 4 of
    # the merged evidence and was still left out of the map.
    steer = f"\n\n{emphasis}" if emphasis else ""
    prompt = (
        f"Request: {question}\n\n"
        f"Retrieval confidence: {state.retrieval.confidence} "
        f"({state.retrieval.confidence_reason})\n\n"
        f"Evidence:\n{wrap_retrieved(chr(10).join(lines))}{steer}\n\n"
        "Build the impact map."
    )
    try:
        response = llm.complete(
            "cartographer_merge", prompt, system=load_prompt("cartographer_merge"), json_mode=True
        )
        state.usage.add(_usage_of(response))
    except Exception as exc:
        state.impact_map = _fallback_map(state, f"{exc.__class__.__name__}: {exc}")
        state.impact_map.module_summaries = module_summaries
        return state

    parsed = parse_json_object(response.text)
    if parsed is None:
        state.impact_map = _fallback_map(state, "merge reply was not JSON")
        state.impact_map.module_summaries = module_summaries
        return state

    known = {c.citation: c.node_id for c in state.retrieval.chunks}

    def entries(key: str) -> list[ImpactEntry]:
        out: list[ImpactEntry] = []
        for raw in parsed.get(key) or []:
            if not isinstance(raw, dict):
                continue
            citation = str(raw.get("citation") or "").strip()
            # Drop anything whose citation was not in the evidence. A fabricated
            # citation is worse than a missing entry — design law 4.
            if citation not in known:
                continue
            out.append(
                ImpactEntry(
                    name=str(raw.get("name") or ""),
                    citation=citation,
                    why=str(raw.get("why") or ""),
                    node_id=known[citation],
                )
            )
        return out

    # The merge still reports a confidence, and it is still recorded — but it is
    # no longer what the run prints. The level is computed from rules over the
    # evidence (orchestrator/confidence.py); a model grading its own map graded
    # generously, which is the whole reason that module exists.
    model_confidence = str(parsed.get("confidence") or "").lower()
    if model_confidence not in {"high", "medium", "low"}:
        model_confidence = ""

    state.impact_map = ImpactMap(
        summary=str(parsed.get("summary") or ""),
        endpoints=entries("endpoints"),
        classes_methods=entries("classes_methods"),
        entities_tables=entries("entities_tables"),
        screens=entries("screens"),
        hubs_crossed=entries("hubs_crossed"),
        modules=[str(m) for m in (parsed.get("modules") or sorted(by_module))],
        # The base level, straight from the rerank thresholds in config. Only the
        # verifier lowers it further, once the answer exists and its citations
        # have been checked.
        confidence=state.retrieval.confidence,
        confidence_reasons=[
            f"evidence scored {state.retrieval.confidence} on retrieval: "
            f"{state.retrieval.confidence_reason}"
        ],
        model_confidence=model_confidence,
        gaps=[str(g) for g in (parsed.get("gaps") or [])],
        module_summaries=module_summaries,
    )
    return state
