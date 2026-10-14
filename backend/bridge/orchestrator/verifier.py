"""The verifier step: check the answer's citations, then score its confidence.

Runs last, for every retrieval lane, and calls no model — like the historian, it
is a *step* rather than an agent. It is a node anyway so that the persisted trace
records it, because what it does is consequential: it can remove citations from a
finished answer and lower the level printed next to it.

Order matters, and it is the order below:

1. verify what the writer cited (`verify.py`);
2. drop the failures — from the impact map as rows, from the prose as strings;
3. score the level from rules over what survived (`confidence.py`);
4. write the reasons back into the answer's "what would make me more sure" line,
   so a reader sees *why* it is not higher rather than only that it is not.

Step 4 is the part that makes the score useful rather than merely honest. "This
is medium" tells a product person nothing; "this is medium because nothing in the
answer touches the code that saves a visit" tells them what to ask for next.
"""

from __future__ import annotations

from ..agents.state import RunState, append_more_sure
from . import confidence as confidence_module
from . import graph
from . import verify as verify_module


def _writer_field(state: RunState) -> str | None:
    """Which field holds the answer body — the one whose citations get checked.

    Deliberately the same precedence as `run.answer_body`: verifying one field
    while the reader is shown another would scrub citations out of text nobody
    sees and leave the displayed ones unchecked.
    """
    if graph.wants_architect(state.lane) and state.spec:
        return "spec"
    if state.narrative:
        return "narrative"
    if state.spec:
        return "spec"
    return None


def run(state: RunState, retriever) -> RunState:
    """Verify citations, drop failures, and set the deterministic confidence."""
    store = getattr(retriever, "store", None)
    if store is None or state.impact_map is None:
        return state

    span_index = verify_module.SpanIndex(store)
    field = _writer_field(state)
    body = getattr(state, field) if field else ""

    # The answer's own citations are what the level is scored on: the impact map
    # is what the run *looked at*, the body is what it *claimed*.
    result = verify_module.verify_text(body, store, state.sha, span_index=span_index)

    # The map is checked too, but separately — its entries are rows and can simply
    # be dropped, and a bad row there is a different failure from a bad claim.
    map_result = verify_module.verify_citations(
        [entry.citation for entry in state.impact_map.all_entries()],
        store,
        state.sha,
        span_index=span_index,
    )
    bad_in_map = set(map_result.dropped_citations)
    if bad_in_map:
        for kind in state.impact_map.KINDS:
            setattr(
                state.impact_map,
                kind,
                [e for e in getattr(state.impact_map, kind) if e.citation not in bad_in_map],
            )

    # Scored on what the ANSWER cited, never on what the map found. An earlier
    # cut fell back to the map when the body yielded no citations, on the theory
    # that a good map was better evidence than nothing. It hid a real defect: a
    # bug-lane run whose architect wrote bare file paths instead of citations
    # scored `high` off a map the reader never sees, while the answer in front of
    # them was uncheckable. Design law 4 is about the answer, so the score is too.
    verdict = confidence_module.score_confidence(
        state.lane,
        state.retrieval,
        state.impact_map,
        result.verified,
        verification=result,
        store=store,
        followups=state.followups,
    )

    if field and body:
        updated = body
        if result.dropped:
            updated = verify_module.scrub(updated, result.dropped_citations)
            updated = (
                f"{updated.rstrip()}\n\n> {confidence_module.removal_note(result)}"
            )
        updated = append_more_sure(updated, verdict.missing) if verdict.level != "high" else updated
        setattr(state, field, updated)

    state.impact_map.confidence = verdict.level
    state.impact_map.confidence_reasons = verdict.reasons
    state.verification = {
        "answer": result.as_dict(),
        "impact_map": map_result.as_dict(),
        "confidence": verdict.as_dict(),
    }
    return state
