"""Go back for the evidence the first search missed — at most twice, and only if affordable.

The first retrieval answers the question it was handed. It has no way to know
that a *bug* answer needs the write path as well as the display path, or that the
mapping step was about to admit it never saw the persistence layer. Those are
facts about the evidence, and they are only knowable after the cartographer has
looked at it.

So three rules run, in this order, and the first two that fire get a search each:

a. **the cartographer named a gap** — search for what it said was missing;
b. **a bug lane covering only one side of the flow** — search toward the other,
   using the same read/write vocabulary `index/io_roles.py` classifies with;
c. **a change to an entity with no schema or migration file in evidence** —
   search for those paths.

Two ceilings keep this from becoming an open-ended crawl. **At most
`FOLLOWUP_MAX_SHOTS` extra searches**, and each one only if the run still has
`FOLLOWUP_MIN_BUDGET_FRACTION` of its *tightest* ceiling left. The second matters
more than it looks: a follow-up that pushes a run into a budget abort has made
the answer strictly worse, because the writer never runs and the extra chunks are
read by nobody. A shot skipped for budget is recorded and surfaces in the
confidence reasons, so an answer that could have been better says so.

Merging is a union, deduped by node id, rescored by a second rerank pass, and cut
to a top-N that grows with the number of shots — with a floor per shot, so one
productive search cannot take every new slot and starve the other's evidence,
which was answering a different hole entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .. import config
from ..agents import cartographer
from ..agents.llm import LLMClient, Usage
from ..agents.state import RunState
from ..index.model import NodeKind
from ..redact import RedactionLog

#: rule ids, in the order they are considered
RULE_GAPS = "gaps"
RULE_WRITE_SIDE = "write_side"
RULE_READ_SIDE = "read_side"
RULE_SCHEMA = "schema"

#: words too common in a gap sentence to steer a search
_GAP_STOPWORDS = frozenset(
    """a an and are as at be been by for from has have how in into is it its of on or that the
    their there these this to was were what when where which who will with would could should
    not no any some can may might must do does did been being about""".split()
)


@dataclass(slots=True)
class Shot:
    """One planned extra search: why, and what to search for."""

    rule: str
    reason: str
    query: str
    #: identifiers handed to the keyword stage as well as the semantic one
    terms: list[str] = field(default_factory=list)
    #: filled in once it has run
    fired: bool = False
    added: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    skipped_reason: str = ""
    #: node ids this shot contributed, so the merge can guarantee it a share
    node_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "reason": self.reason,
            "query": self.query[:300],
            "fired": self.fired,
            "added": self.added,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 3),
            "skipped_reason": self.skipped_reason,
        }


@dataclass(slots=True)
class FollowupReport:
    shots: list[Shot] = field(default_factory=list)
    merged_chunks: int = 0
    reranked: bool = False

    @property
    def fired(self) -> list[Shot]:
        return [shot for shot in self.shots if shot.fired]

    @property
    def skipped_for_budget(self) -> list[Shot]:
        return [shot for shot in self.shots if not shot.fired and shot.skipped_reason]

    def as_dict(self) -> dict[str, Any]:
        return {
            "shots": [shot.as_dict() for shot in self.shots],
            "fired": len(self.fired),
            "merged_chunks": self.merged_chunks,
            "reranked": self.reranked,
        }


# --------------------------------------------------------------------------- rules


def gap_terms(gaps: list[str], limit: int = 24) -> list[str]:
    """The content words in the cartographer's gap sentences, in order, deduped.

    A gap is written as prose — "Database repository layer or SQL queries to
    verify if the visit data is being persisted" — and handing that whole
    sentence to an embedding works, but the keyword stage needs tokens. Stopwords
    go; everything else keeps its order, so the search reads like the gap did.
    """
    found: list[str] = []
    for gap in gaps:
        for raw in gap.replace("/", " ").replace("(", " ").replace(")", " ").split():
            word = raw.strip(".,;:'\"`-").lower()
            if len(word) < 3 or word in _GAP_STOPWORDS or word in found:
                continue
            found.append(word)
            if len(found) >= limit:
                return found
    return found


def _entity_names(state: RunState) -> list[str]:
    """Product nouns to anchor a follow-up on: mapped entities, then the interpreter's."""
    names: list[str] = []
    impact = state.impact_map
    if impact is not None:
        for entry in impact.entities_tables:
            if entry.name and entry.name not in names:
                names.append(entry.name)
    if state.interpretation:
        for entity in state.interpretation.entities:
            if entity and entity not in names:
                names.append(entity)
    return names[:6]


def _sides_covered(state: RunState, store) -> tuple[bool, bool]:
    """(has read side, has persisting write side) — measured over the **impact map**.

    Measured over the map rather than over raw retrieval, and that distinction is
    the whole rule. Observed on `visit-missing`: `processNewVisitForm`, the POST
    handler that saves a visit, was sitting in retrieval the entire time — but the
    merge did not cite it, so the answer never mentioned the write path and
    `confidence.py` capped the run at `medium` for exactly that. A trigger reading
    raw retrieval saw a write and declined to act, while the rule judging the
    answer saw none. The two now ask the same question of the same evidence.

    A shot in that state is still worth firing: the merge gets a second look at a
    deliberately write-heavy evidence set, which is a better chance than the first
    one had.
    """
    from . import verify

    if store is None:
        return True, True
    citations = []
    if state.impact_map is not None:
        citations = [entry.citation for entry in state.impact_map.all_entries()]
    if not citations:
        if state.retrieval is None:
            return True, True
        citations = [chunk.citation for chunk in state.retrieval.chunks]

    index = verify.SpanIndex(store)
    result = verify.verify_citations(citations, store, state.sha, span_index=index)
    has_read = any(citation.is_read for citation in result.verified)
    has_write = any(citation.is_persisting_write for citation in result.verified)
    return has_read, has_write


def _touches_entity(state: RunState) -> bool:
    impact = state.impact_map
    if impact is not None and impact.entities_tables:
        return True
    if state.retrieval is None:
        return False
    return any(chunk.kind == NodeKind.ENTITY for chunk in state.retrieval.chunks)


def _schema_files_in(paths) -> bool:
    return any(
        pattern in path.lower() for path in paths for pattern in config.FOLLOWUP_SCHEMA_PATTERNS
    )


def _graph_has_schema_files(store) -> bool:
    """Whether the index holds any schema/migration file at all.

    Rule (c) is pointless where the answer cannot exist. petclinic's indexer walks
    `.java` and `.html` only, so its `db/{h2,mysql,postgres}/schema.sql` are not
    nodes — searching for them would spend a shot to find nothing, every time.
    Checking first turns that into a recorded reason instead of a wasted call.
    """
    if store is None:
        return False
    for pattern in config.FOLLOWUP_SCHEMA_PATTERNS:
        row = store.conn.execute(
            "SELECT 1 FROM nodes WHERE path LIKE ? LIMIT 1", (f"%{pattern}%",)
        ).fetchone()
        if row is not None:
            return True
    return False


def plan_shots(state: RunState, store=None) -> list[Shot]:
    """Which extra searches this run warrants, in rule order. Pure; no I/O but the graph."""
    question = cartographer.question_for(state)
    impact = state.impact_map
    shots: list[Shot] = []

    # (a) the mapping step named something it could not see
    gaps = list(impact.gaps) if impact is not None else []
    if gaps:
        terms = gap_terms(gaps)
        if terms:
            shots.append(
                Shot(
                    rule=RULE_GAPS,
                    reason=f"the mapping step named {len(gaps)} gap(s); searching for what it missed",
                    query=f"{question}\n{' '.join(terms)}",
                    terms=terms,
                )
            )

    # (b) a bug that has only seen one half of the flow
    if state.lane == "bug":
        has_read, has_write = _sides_covered(state, store)
        entities = _entity_names(state)
        if not has_write:
            shots.append(
                Shot(
                    rule=RULE_WRITE_SIDE,
                    reason="a bug needs both sides of the flow and nothing retrieved writes; "
                    "searching for the code that saves",
                    query=" ".join([state.request, config.FOLLOWUP_WRITE_TERMS, *entities]),
                    terms=[*config.FOLLOWUP_WRITE_TERMS.split(), *entities],
                )
            )
        elif not has_read:
            shots.append(
                Shot(
                    rule=RULE_READ_SIDE,
                    reason="a bug needs both sides of the flow and nothing retrieved displays; "
                    "searching for the code that shows",
                    query=" ".join([state.request, config.FOLLOWUP_READ_TERMS, *entities]),
                    terms=[*config.FOLLOWUP_READ_TERMS.split(), *entities],
                )
            )

    # (c) a change to a stored thing, with no schema or migration in sight
    if state.lane == "change_request" and _touches_entity(state):
        paths = [chunk.path for chunk in (state.retrieval.chunks if state.retrieval else [])]
        if not _schema_files_in(paths):
            entities = _entity_names(state)
            if _graph_has_schema_files(store):
                shots.append(
                    Shot(
                        rule=RULE_SCHEMA,
                        reason="a change to a stored entity with no schema or migration file in "
                        "evidence; searching for one",
                        query=" ".join(
                            [state.request, *config.FOLLOWUP_SCHEMA_PATTERNS, *entities]
                        ),
                        terms=[*config.FOLLOWUP_SCHEMA_PATTERNS, *entities],
                    )
                )
            else:
                shots.append(
                    Shot(
                        rule=RULE_SCHEMA,
                        reason="a change to a stored entity with no schema or migration file in "
                        "evidence",
                        query="",
                        skipped_reason="this index holds no schema or migration files at all — "
                        "the indexer walks source and templates, so there is nothing to find",
                    )
                )

    return shots


# --------------------------------------------------------------------------- merge


def merge(state: RunState, new_chunks_by_shot: dict[str, list], llm: LLMClient,
          retriever) -> tuple[int, bool]:
    """Union, dedupe, rescore, cut. Returns (final chunk count, whether rerank-2 ran).

    The cut grows by `FOLLOWUP_EXTRA_PER_SHOT` per shot rather than staying at
    `RETRIEVE_TOP_K`: a follow-up that fetches evidence the writer never sees has
    achieved nothing. Each shot is then guaranteed `FOLLOWUP_MIN_PER_SHOT` of its
    own chunks, because the shots answer *different* holes — letting a strong one
    take every new slot would silently drop the other's answer.
    """
    result = state.retrieval
    existing = {chunk.node_id: chunk for chunk in result.chunks}
    added_ids: dict[str, list[str]] = {}

    for rule, chunks in new_chunks_by_shot.items():
        kept: list[str] = []
        for chunk in chunks:
            if chunk.node_id in existing:
                continue
            existing[chunk.node_id] = chunk
            kept.append(chunk.node_id)
        added_ids[rule] = kept

    pool = list(existing.values())

    # rerank-2: one pass over the union, so old and new chunks are scored against
    # each other rather than carrying scores from two different prompts.
    reranked = False
    if len(pool) > len(result.chunks):
        ran, _reason, cost = retriever.rerank(result.question, pool, llm, RedactionLog())
        result.cost_usd += cost
        state.usage.add(Usage(cost_usd=cost, by_role={"rerank": 1 if ran else 0}))
        reranked = ran
        if ran:
            for chunk in pool:
                chunk.score = (chunk.rerank_score or 0.0)

    # Same tie-break as retrieve.py's two score sorts, and for the same reason:
    # a follow-up shot's chunks and the original pool are two DIFFERENT dicts'
    # worth of insertion order glued together here, so an explicit (score,
    # node_id) key is what makes the merged order a property of the data rather
    # than of which dict happened to iterate first.
    pool.sort(key=lambda c: (-c.score, c.node_id))
    limit = config.RETRIEVE_TOP_K + config.FOLLOWUP_EXTRA_PER_SHOT * max(1, len(added_ids))
    merged = pool[:limit]
    chosen = {chunk.node_id for chunk in merged}

    for rule, ids in added_ids.items():
        quota = [chunk for chunk in pool if chunk.node_id in set(ids)][
            : config.FOLLOWUP_MIN_PER_SHOT
        ]
        for chunk in quota:
            if chunk.node_id not in chosen:
                merged.append(chunk)
                chosen.add(chunk.node_id)

    merged.sort(key=lambda c: (-c.score, c.node_id))
    result.chunks = merged
    from ..retrieval.retrieve import _confidence, _history_for

    for chunk in result.chunks:
        if chunk.history is None:
            # Churn is decoration on a chunk, not a reason to lose the merge —
            # a node the graph cannot resolve simply carries no history.
            try:
                chunk.history = _history_for(retriever.store, chunk.node_id)
            except Exception:
                chunk.history = None
    result.confidence, result.confidence_reason = _confidence(
        result.chunks, reranked or result.reranked, result.rerank_unavailable_reason
    )
    return len(result.chunks), reranked


#: What the rebuilt map is told, per rule that fired. Without this the merge maps
#: the half of the flow the question was phrased around and the follow-up's
#: evidence sits unused in the pile — measured, not assumed.
_EMPHASIS = {
    RULE_WRITE_SIDE: (
        "This request is a bug report. A bug is only answerable from BOTH sides of the "
        "flow: the code that displays the thing and the code that writes or saves it. "
        "The evidence above was deliberately re-searched for the write side. If any "
        "endpoint, handler, or repository method that saves, updates or deletes this "
        "data is present, it MUST appear in the map. Do not invent one that is absent."
    ),
    RULE_READ_SIDE: (
        "This request is a bug report. A bug is only answerable from BOTH sides of the "
        "flow: the code that writes the thing and the code that displays it. The "
        "evidence above was deliberately re-searched for the read side. If any screen, "
        "GET handler, or query that shows this data is present, it MUST appear in the "
        "map. Do not invent one that is absent."
    ),
    RULE_SCHEMA: (
        "The evidence above was re-searched for schema or migration files. If one is "
        "present, it MUST appear in the map — a change to a stored field lands there too."
    ),
    RULE_GAPS: (
        "The evidence above was re-searched for what an earlier pass reported it could "
        "not see. Where that search found something, map it and drop the corresponding "
        "gap; where it did not, keep the gap and say so."
    ),
}


def _emphasis(report: FollowupReport) -> str:
    return "\n".join(
        _EMPHASIS[shot.rule] for shot in report.fired if shot.rule in _EMPHASIS
    )


# --------------------------------------------------------------------------- node


def run(state: RunState, llm: LLMClient, retriever) -> RunState:
    """Plan, afford, fire, merge, and rebuild the map. Deterministic except the searches."""
    report = FollowupReport()
    store = getattr(retriever, "store", None)
    report.shots = plan_shots(state, store)[: config.FOLLOWUP_MAX_SHOTS + 2]

    new_by_shot: dict[str, list] = {}
    fired = 0
    for shot in report.shots:
        if shot.skipped_reason:
            continue  # a rule that fired but has nothing to search for
        if fired >= config.FOLLOWUP_MAX_SHOTS:
            shot.skipped_reason = (
                f"only {config.FOLLOWUP_MAX_SHOTS} extra searches are allowed per run"
            )
            continue
        fraction = state.budget.fraction_remaining(state.usage)
        if fraction < config.FOLLOWUP_MIN_BUDGET_FRACTION:
            shot.skipped_reason = (
                f"{fraction:.0%} of the run's budget was left "
                f"({state.budget.tightest_ceiling(state.usage)} is tightest), below the "
                f"{config.FOLLOWUP_MIN_BUDGET_FRACTION:.0%} an extra search needs"
            )
            continue

        entry = state.begin(f"followup:{shot.rule}")
        before = state.snapshot_usage()
        started = time.perf_counter()
        try:
            extra = retriever.retrieve(
                shot.query, state.budget, llm=llm, extra_terms=shot.terms
            )
            state.usage.add(
                Usage(cost_usd=extra.cost_usd, by_role={"rerank": 1 if extra.reranked else 0})
            )
            shot.cost_usd = extra.cost_usd
            known = {chunk.node_id for chunk in state.retrieval.chunks}
            found = [chunk for chunk in extra.chunks if chunk.node_id not in known]
            new_by_shot[shot.rule] = found
            shot.node_ids = [chunk.node_id for chunk in found]
            shot.added = len(found)
            shot.fired = True
            fired += 1
            shot.elapsed_s = time.perf_counter() - started
            state.end(entry, "ok", f"{shot.reason} — +{shot.added} chunks", before)
        except Exception as exc:
            shot.elapsed_s = time.perf_counter() - started
            shot.skipped_reason = f"{exc.__class__.__name__}: {exc}"
            state.usage.errors += 1
            state.end(entry, "error", shot.skipped_reason, before)

    if any(chunks for chunks in new_by_shot.values()):
        report.merged_chunks, report.reranked = merge(state, new_by_shot, llm, retriever)
        # The map is what the architect reads and what recall is measured on, so
        # evidence that does not reach it has not arrived. Rebuilding costs one
        # deep call, and is skipped if that would break the same budget rule.
        if state.budget.fraction_remaining(state.usage) >= config.FOLLOWUP_MIN_BUDGET_FRACTION:
            entry = state.begin("followup:remap")
            before = state.snapshot_usage()
            try:
                state = cartographer.build_map(state, llm, emphasis=_emphasis(report))
                state.end(entry, "ok", f"map rebuilt over {report.merged_chunks} chunks", before)
            except Exception as exc:
                state.end(entry, "error", f"{exc.__class__.__name__}: {exc}", before)
                state.usage.errors += 1
        else:
            report.shots.append(
                Shot(
                    rule="remap",
                    reason="rebuilding the impact map over the new evidence",
                    query="",
                    skipped_reason="not enough budget left to rebuild the map, so the new "
                    "evidence reached the answer but not the impact map",
                )
            )
    else:
        report.merged_chunks = len(state.retrieval.chunks) if state.retrieval else 0

    state.followups = report.as_dict()
    return state
