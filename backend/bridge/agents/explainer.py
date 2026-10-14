"""Explainer — the question lane's writer. Reads real code and tells the story.

Before this node existed, a `question` produced a categorised inventory: the
cartographer's summary followed by every impact-map entry grouped by kind. That
answers "what does this touch", which is not what anybody asked. "What happens
when an owner adds a new pet" wants a *path* — the screen, the steps in order,
where the data lands — and a path cannot be recovered from a list of names.

Two things make the narrative possible, and both are deliberate:

* **It sees the source.** Every other node reasons over one-sentence descriptions.
  Descriptions are what makes retrieval work (a product question does not match
  Java identifiers), but they are far too thin to explain a flow: the order of the
  steps, what is validated, what is written, only exist in the code. The explainer
  gets the real thing, hydrated by `retrieval/code.py`.
* **It sees the connecting path.** Each chunk carries the `graph_path` retrieval
  recorded when it reached that node, so the model is not left inferring how a
  controller relates to a screen — the structural graph already knows.

Its output is prose, deliberately unvalidated by a schema: an explanation that has
been flattened into JSON stops being an explanation. What *is* enforced in Python
is the honesty contract — citations that were not in the evidence are named as
such (:func:`_check_citations`), and an uncertain answer must carry the line that
says what would settle it.
"""

from __future__ import annotations

from .. import config
from ..redact import RedactionLog
from ..retrieval import code as code_module
from .llm import LLMClient, Usage, load_prompt, wrap_retrieved
from .state import MORE_SURE_PREFIX, ImpactMap, RunState, citations_in

GAPS_HEADING = "**Gaps — what I did not find**"


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


def select_chunks(state: RunState, limit: int = config.EXPLAINER_CHUNKS) -> list:
    """The chunks the explainer reads in full, in reading order. Deterministic.

    Impact-map entries come first, in map order: the cartographer already judged
    those relevant and said why, and its ordering (endpoints, then classes and
    methods, then entities, then screens) happens to be close to the order a
    request actually travels. Everything else is filled in from retrieval by
    score, so a chunk the merge declined to cite can still supply the code for a
    step — it just does not get to set the shape of the answer.
    """
    retrieval = state.retrieval
    if retrieval is None:
        return []
    by_citation = {chunk.citation: chunk for chunk in retrieval.chunks}
    by_node = {chunk.node_id: chunk for chunk in retrieval.chunks}

    chosen: list = []
    seen: set[str] = set()

    def take(chunk) -> None:
        if chunk is not None and chunk.node_id not in seen:
            seen.add(chunk.node_id)
            chosen.append(chunk)

    if state.impact_map is not None:
        for entry in state.impact_map.all_entries():
            take(by_citation.get(entry.citation) or by_node.get(entry.node_id))
    for chunk in retrieval.chunks:
        if len(chosen) >= limit:
            break
        take(chunk)
    return chosen[:limit]


def build_prompt(state: RunState, chunks: list, redaction: RedactionLog | None = None) -> str:
    """Everything the explainer is allowed to reason from, as one string."""
    redaction = redaction or RedactionLog()
    impact: ImpactMap | None = state.impact_map
    question = (
        state.interpretation.normalised_request
        if state.interpretation and state.interpretation.normalised_request
        else state.request
    )

    header = [f"Question: {question}"]
    if state.interpretation and state.interpretation.assumptions:
        header.append(
            "Assumptions already stated to the user:\n"
            + "\n".join(f"- {a}" for a in state.interpretation.assumptions)
        )
    if impact is not None:
        header.append(f"Evidence confidence: {impact.confidence}")
        if impact.gaps:
            header.append(
                "The mapping step could not see:\n" + "\n".join(f"- {g}" for g in impact.gaps)
            )
    if state.prior_context:
        header.append(f"Earlier in this thread:\n{state.prior_context[:600]}")

    volatile = set(state.history.volatile) if state.history else set()
    bodies = []
    for chunk in chunks:
        rendered = code_module.render(chunk)
        if chunk.path in volatile:
            rendered = rendered.replace("citation:", "note: this file changes often\ncitation:", 1)
        bodies.append(rendered)

    return (
        "\n\n".join(header)
        + "\n\nThe code that was found, in the order it is most likely to run:\n"
        + wrap_retrieved(redaction.scrub("\n\n".join(bodies)))
        + "\n\nWrite the walkthrough."
    )


def _check_citations(narrative: str, known: set[str]) -> list[str]:
    """Citations the answer used that were not in its evidence.

    A JSON node can simply drop a fabricated citation (the cartographer does).
    Prose cannot be edited that way without rewriting someone's sentence, so the
    invention is *named* instead — loudly, in the gaps section. Silence is the one
    option design law 4 rules out.
    """
    return [citation for citation in citations_in(narrative) if citation not in known]


def _append_to_gaps(narrative: str, lines: list[str]) -> str:
    """Add ``lines`` under the gaps heading, or start one if the model omitted it."""
    block = "\n".join(f"- {line}" for line in lines)
    if GAPS_HEADING in narrative:
        head, _, tail = narrative.partition(GAPS_HEADING)
        return f"{head}{GAPS_HEADING}\n\n{block}\n{tail.lstrip(chr(10))}"
    return f"{narrative.rstrip()}\n\n{GAPS_HEADING}\n\n{block}"


def run(state: RunState, llm: LLMClient, retriever=None) -> RunState:
    """Populate ``state.narrative``. Caller ensures the lane warrants it."""
    if not llm.available_for("explainer"):
        state.narrative = (
            "_No walkthrough: the explainer model is unavailable "
            f"({llm.reason_for('explainer')})._\n\n"
            "The evidence panel still holds every cited piece this run retrieved."
        )
        return state

    chunks = select_chunks(state)
    if not chunks:
        state.narrative = (
            "Nothing in the indexed repository matched this question closely enough to "
            "explain how it works.\n\n"
            f"{GAPS_HEADING}\n\n- Retrieval returned no usable evidence, so there is no "
            "code to walk through. The question may be about a different system, or about "
            "production data rather than code."
        )
        return state

    if retriever is not None:
        code_module.hydrate(retriever, chunks)

    prompt = build_prompt(state, chunks)
    try:
        response = llm.complete("explainer", prompt, system=load_prompt("explainer"))
        state.usage.add(_usage_of(response))
    except Exception as exc:
        state.narrative = f"_The walkthrough failed: {exc.__class__.__name__}: {exc}_"
        return state

    narrative = (response.text or "").strip()
    if not narrative:
        state.narrative = "_The explainer returned nothing to say about this evidence._"
        return state

    known = {chunk.citation for chunk in chunks}
    if state.impact_map is not None:
        known.update(entry.citation for entry in state.impact_map.all_entries())
    if state.retrieval is not None:
        known.update(chunk.citation for chunk in state.retrieval.chunks)

    invented = _check_citations(narrative, known)
    if invented:
        narrative = _append_to_gaps(
            narrative,
            [
                "**Unverified citation** — this answer cites "
                f"`{citation}`, which was not in the evidence handed to it. "
                "Treat that claim as unsupported."
                for citation in invented
            ],
        )

    confidence = state.impact_map.confidence if state.impact_map else "low"
    if confidence != "high" and MORE_SURE_PREFIX.lower() not in narrative.lower():
        gaps = "; ".join(state.impact_map.gaps) if state.impact_map and state.impact_map.gaps else (
            "the parts of this flow retrieval did not reach"
        )
        narrative += f"\n\n{MORE_SURE_PREFIX} {gaps}"
    state.narrative = narrative
    return state
