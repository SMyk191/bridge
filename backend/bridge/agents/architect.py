"""Architect — the technical brief. Deep tier, and only for change requests and bugs.

Runs last, sees everything, and writes prose rather than JSON: the output is for
a human to read, and forcing it through a schema would flatten exactly the
hedging that makes it trustworthy.

The "what would make me more sure" line is required whenever confidence is not
high. That is design law 4 expressed as a deliverable: an uncertain brief that
names what would resolve the uncertainty is useful; one that hides the
uncertainty behind confident prose is worse than nothing.
"""

from __future__ import annotations

from .. import config
from ..retrieval import code as code_module
from .llm import LLMClient, Usage, load_prompt, wrap_retrieved
from .state import MORE_SURE_PREFIX, RunState, citation_path

__all__ = ["MORE_SURE_PREFIX", "render_impact_map", "run", "touch_point_code"]


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


def render_impact_map(state: RunState) -> str:
    impact = state.impact_map
    if impact is None:
        return "(no impact map)"
    volatile = set(state.history.volatile) if state.history else set()
    churn = {f.path: f for f in (state.history.files if state.history else [])}

    lines = [f"summary: {impact.summary}", f"confidence: {impact.confidence}"]
    if impact.gaps:
        lines.append("gaps: " + "; ".join(impact.gaps))
    for kind in impact.KINDS:
        group = getattr(impact, kind)
        if not group:
            continue
        lines.append(f"\n{kind.replace('_', ' / ')}:")
        for entry in group:
            path = citation_path(entry.citation)
            note = churn.get(path)
            flags = " [volatile]" if path in volatile else ""
            history = (
                f" ({note.commits_all_time} commits, {note.commits_recent} in "
                f"{state.history.window_days}d)"
                if note
                else ""
            )
            lines.append(f"  - {entry.name}{flags}{history}\n    {entry.citation}\n    {entry.why}")
    return "\n".join(lines)


def touch_point_code(state: RunState, retriever=None,
                     limit: int = config.ARCHITECT_CODE_CHUNKS) -> str:
    """The real source of the impact map's first ``limit`` touch-points.

    The brief used to be written entirely from one-sentence descriptions, which is
    how you get "add a field to the entity and update the form" for every change
    request ever asked. Sizing, risk, and the test plan are all claims about what
    the code actually does — validation that is already there, a hub every flow
    passes through — and none of that is visible in a description.

    Ordering follows the map, so the appendix reads in the same order as the list
    above it. Chunks are matched to map entries by citation, and anything the map
    named that retrieval cannot supply is simply absent — the prompt tells the
    model to treat a file it was not handed as "inspect", never to characterise it.
    """
    if state.impact_map is None or state.retrieval is None:
        return ""
    by_citation = {chunk.citation: chunk for chunk in state.retrieval.chunks}
    chosen: list = []
    seen: set[str] = set()
    for entry in state.impact_map.all_entries():
        chunk = by_citation.get(entry.citation)
        if chunk is not None and chunk.node_id not in seen:
            seen.add(chunk.node_id)
            chosen.append(chunk)
        if len(chosen) >= limit:
            break
    if not chosen:
        return ""
    if retriever is not None:
        code_module.hydrate(retriever, chosen)
    return code_module.render_all(chosen)


def run(state: RunState, llm: LLMClient, retriever=None) -> RunState:
    """Populate ``state.spec``. Caller ensures the lane warrants it."""
    if not llm.available_for("architect"):
        state.spec = (
            "_No technical brief: the architect model is unavailable "
            f"({llm.reason_for('architect')})._\n\n"
            "The impact map above is the evidence a brief would have been built from."
        )
        return state

    interpretation = state.interpretation
    assumptions = ""
    if interpretation and interpretation.assumptions:
        assumptions = "\nAssumptions made:\n" + "\n".join(
            f"- {a}" for a in interpretation.assumptions
        )

    evidence = render_impact_map(state)
    appendix = touch_point_code(state, retriever)
    if appendix:
        evidence += "\n\nThe source of the main touch-points:\n\n" + appendix

    prompt = (
        f"Request: {interpretation.normalised_request if interpretation else state.request}\n"
        f"Lane: {state.lane}{assumptions}\n\n"
        f"Impact map, history, and code:\n{wrap_retrieved(evidence)}\n\n"
        "Write the brief."
    )
    try:
        response = llm.complete("architect", prompt, system=load_prompt("architect"))
        state.usage.add(_usage_of(response))
    except Exception as exc:
        state.spec = f"_Technical brief failed: {exc.__class__.__name__}: {exc}_"
        return state

    spec = response.text.strip()
    confidence = state.impact_map.confidence if state.impact_map else "low"
    if confidence != "high" and MORE_SURE_PREFIX.lower() not in spec.lower():
        # The prompt requires it; append it rather than silently shipping a brief
        # that reads more certain than the evidence supports.
        gaps = "; ".join(state.impact_map.gaps) if state.impact_map and state.impact_map.gaps else (
            "coverage of the parts of this flow that retrieval did not reach"
        )
        spec += f"\n\n{MORE_SURE_PREFIX} {gaps}"
    state.spec = spec
    return state
