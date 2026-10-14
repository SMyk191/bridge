"""`RunState` — the single object every agent reads and returns.

Each agent is a plain function ``(RunState) -> RunState``. No agent calls another,
and no agent decides what runs next; the orchestrator owns sequencing (design law
1). That is what makes the pipeline inspectable: the whole run is a value you can
serialise, persist, and diff.

Everything here is JSON-round-trippable, because the run is written to SQLite
after every node.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import config
from .llm import Budget, Usage

#: The line an agent must end on when it is not fully sure. Shared by the
#: architect and the explainer so there is exactly one spelling of it in the
#: system — the orchestrator and the tests both key on this exact prefix.
MORE_SURE_PREFIX = "What would make me more sure:"

#: `repo@sha:path:start-end`, or `repo@commit:path` for a changelog claim.
#: Used to pull the citations back **out** of an agent's prose, so a written
#: answer can be checked against the evidence it was given (design law 4) and so
#: the answer's citation block indexes the body rather than repeating the map.
CITATION_RE = re.compile(r"[A-Za-z0-9_.\-]+@[0-9a-fA-F]{6,40}:[^\s`'\"<>,;)\]}]+")


def citations_in(text: str) -> list[str]:
    """Every citation in ``text``, in order of appearance, deduplicated."""
    found: list[str] = []
    for raw in CITATION_RE.findall(text or ""):
        cleaned = raw.rstrip(".,;:")
        if cleaned and cleaned not in found:
            found.append(cleaned)
    return found


def append_more_sure(text: str, items: list[str]) -> str:
    """Ensure ``text`` ends with the more-sure line, carrying ``items``.

    Both writers emit this line themselves when they are told the confidence is
    not high — but the deterministic score (orchestrator/confidence.py) can lower
    the level *after* they have written, and the reasons it lowers for are exactly
    the ones a reader most needs to see. So the orchestrator appends to the line
    rather than leaving the answer claiming more than the score supports.
    """
    items = [item for item in items if item]
    if not items:
        return text
    lowered = text.lower()
    if MORE_SURE_PREFIX.lower() in lowered:
        # extend the existing line rather than opening a second one
        index = lowered.rindex(MORE_SURE_PREFIX.lower())
        head, tail = text[:index], text[index + len(MORE_SURE_PREFIX):]
        existing = tail.strip().rstrip(".")
        joined = "; ".join([existing] + items) if existing else "; ".join(items)
        return f"{head}{MORE_SURE_PREFIX} {joined}."
    return f"{text.rstrip()}\n\n{MORE_SURE_PREFIX} {'; '.join(items)}."


def citations_of(payload: dict[str, Any]) -> list[str]:
    """Citations a persisted run actually used, from its raw JSON dict.

    Same precedence `orchestrator/run.py::compose_answer` used to build the
    citation block: the answer's own body first (what it actually cited),
    falling back to the impact map, then to raw retrieval. Operates on the dict
    a run round-trips through SQLite (`RunStore.state_of`) rather than the live
    dataclass, so it works equally for a freshly-computed run and for a
    cache-served one (`RunStore.save_raw`), which never builds a live RunState
    at all.
    """
    from_answer = citations_in(payload.get("answer") or "")
    if from_answer:
        return from_answer
    impact = payload.get("impact_map") or {}
    found: list[str] = []
    for kind in ImpactMap.KINDS:
        for entry in impact.get(kind) or []:
            citation = entry.get("citation")
            if citation and citation not in found:
                found.append(citation)
    if found:
        return found
    chunks = (payload.get("retrieval") or {}).get("chunks") or []
    fallback: list[str] = []
    for chunk in chunks:
        citation = chunk.get("citation")
        if citation and citation not in fallback:
            fallback.append(citation)
    return fallback


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class TraceEntry:
    """One node's execution, for the persisted trace (design law 6)."""

    node: str
    status: str  # ok | skipped | error | budget_exceeded
    started_at: float
    elapsed_s: float = 0.0
    detail: str = ""
    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "status": self.status,
            "elapsed_s": round(self.elapsed_s, 3),
            "detail": self.detail,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
        }


@dataclass(slots=True)
class Interpretation:
    normalised_request: str = ""
    intent: str = "question"
    entities: list[str] = field(default_factory=list)
    clarifying_question: str | None = None
    assumptions: list[str] = field(default_factory=list)
    reasoning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalised_request": self.normalised_request,
            "intent": self.intent,
            "entities": self.entities,
            "clarifying_question": self.clarifying_question,
            "assumptions": self.assumptions,
            "reasoning": self.reasoning,
        }


@dataclass(slots=True)
class ImpactEntry:
    name: str
    citation: str
    why: str
    node_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "citation": self.citation, "why": self.why,
                "node_id": self.node_id}


@dataclass(slots=True)
class ImpactMap:
    summary: str = ""
    endpoints: list[ImpactEntry] = field(default_factory=list)
    classes_methods: list[ImpactEntry] = field(default_factory=list)
    entities_tables: list[ImpactEntry] = field(default_factory=list)
    screens: list[ImpactEntry] = field(default_factory=list)
    hubs_crossed: list[ImpactEntry] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    #: the run's confidence — computed by orchestrator/confidence.py from rules,
    #: never taken from a model. Only ever lowered after it is first set.
    confidence: str = "low"
    #: every rule that decided the level, in the order it fired
    confidence_reasons: list[str] = field(default_factory=list)
    #: what the cartographer said about itself, kept for the trace only. Nothing
    #: reads it to make a decision — that is the point of this phase.
    model_confidence: str = ""
    gaps: list[str] = field(default_factory=list)
    #: per-module summaries, when more than one module was implicated
    module_summaries: dict[str, str] = field(default_factory=dict)

    KINDS = ("endpoints", "classes_methods", "entities_tables", "screens", "hubs_crossed")

    def all_entries(self) -> list[ImpactEntry]:
        return [entry for kind in self.KINDS for entry in getattr(self, kind)]

    @property
    def total(self) -> int:
        return len(self.all_entries())

    def paths(self) -> list[str]:
        """Repo-relative paths named by the map, from its citations."""
        found: list[str] = []
        for entry in self.all_entries():
            path = citation_path(entry.citation)
            if path and path not in found:
                found.append(path)
        return found

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            **{kind: [e.as_dict() for e in getattr(self, kind)] for kind in self.KINDS},
            "modules": self.modules,
            "confidence": self.confidence,
            "confidence_reasons": self.confidence_reasons,
            "model_confidence": self.model_confidence,
            "gaps": self.gaps,
            "module_summaries": self.module_summaries,
            "total": self.total,
        }

    def brief(self, limit: int = 8) -> str:
        """A compact form for feeding a follow-up run as prior context."""
        lines = [self.summary] if self.summary else []
        for entry in self.all_entries()[:limit]:
            lines.append(f"- {entry.name} ({entry.citation}): {entry.why}")
        return "\n".join(lines)


#: the `:12-30` (or `:45`) a citation ends with, when it names lines at all
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


def citation_path(citation: str) -> str:
    """`repo@sha:path:12-30` -> `path`. Also `:45`, and no suffix at all.

    The single-line form matters more than it looks. `ids.citation` emits
    `path:45` — not `path:45-45` — for a node whose start and end line are the
    same, and the first cut of this function only stripped a suffix containing a
    dash. So a one-line node's citation kept its line number in the "path", which
    silently broke every path comparison downstream: the historian looked up
    `OwnerRepository.java:45` and found no file, the answer never flagged it
    volatile, and the smoke suite scored a correctly-cited file as a miss
    (`find-owner`, where `OwnerRepository#findByLastNameStartingWith` is one line).
    """
    if ":" not in citation:
        return ""
    return _LINE_SUFFIX.sub("", citation.split(":", 1)[1])


@dataclass(slots=True)
class FileHistoryNote:
    path: str
    commits_all_time: int = 0
    commits_recent: int = 0
    last_changed: str | None = None
    module: str = ""
    volatile: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "commits_all_time": self.commits_all_time,
            "commits_recent": self.commits_recent,
            "last_changed": self.last_changed,
            "module": self.module,
            "volatile": self.volatile,
        }


@dataclass(slots=True)
class HistoryReport:
    files: list[FileHistoryNote] = field(default_factory=list)
    volatile: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    window_days: int = config.HISTORY_RECENT_DAYS

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": [f.as_dict() for f in self.files],
            "volatile": self.volatile,
            "modules": self.modules,
            "window_days": self.window_days,
        }


@dataclass(slots=True)
class RunState:
    """Everything one run knows. Persisted after every node."""

    request: str
    thread_id: str = field(default_factory=lambda: new_id("thr"))
    run_id: str = field(default_factory=lambda: new_id("run"))
    sha: str = ""
    #: set AFTER cartographer, by rules in orchestrator/graph.py (design law 3)
    lane: str | None = None
    interpretation: Interpretation | None = None
    retrieval: Any = None  # RetrievalResult; kept loose to avoid a circular import
    impact_map: ImpactMap | None = None
    history: HistoryReport | None = None
    #: the technical brief — written by the architect, for change_request and bug
    spec: str | None = None
    #: the answer body in prose — written by the explainer (question lane) or the
    #: changelog agent. `spec` and `narrative` are separate fields rather than one
    #: because they are different deliverables: a brief is a plan to act on, a
    #: narrative is an explanation to read, and a run should be able to show which
    #: of the two it actually produced.
    narrative: str | None = None
    #: what orchestrator/verify.py made of the answer's citations
    verification: dict[str, Any] | None = None
    #: extra retrieval passes: what was planned, what fired, what was skipped
    followups: dict[str, Any] | None = None
    #: multi-vote disagreements a node recorded about itself (today: only the
    #: interpreter's intent vote). The orchestrator writes each of these into
    #: the standalone `eval_candidates` table (orchestrator/store.py) — kept
    #: here too so a single run's own JSON shows its own disagreements without
    #: a second query.
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget.for_run)
    usage: Usage = field(default_factory=Usage)
    env: dict[str, Any] = field(default_factory=dict)
    #: prior run in this thread, summarised
    prior_context: str = ""
    #: structured arguments that came with the request rather than inside it —
    #: today only the changelog lane's {from_sha, to_sha}. Kept separate from
    #: `request` because they are caller-supplied facts, not something a model
    #: parsed out of prose.
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "running"  # running | done | aborted | needs_clarification
    abort_reason: str | None = None
    answer: str = ""
    started_at: float = field(default_factory=time.time)

    # -- trace ----------------------------------------------------------

    def begin(self, node: str) -> TraceEntry:
        entry = TraceEntry(node=node, status="running", started_at=time.perf_counter())
        self.trace.append(entry)
        return entry

    def end(self, entry: TraceEntry, status: str, detail: str = "", usage_before: Usage | None = None) -> None:
        entry.status = status
        entry.detail = detail
        entry.elapsed_s = time.perf_counter() - entry.started_at
        if usage_before is not None:
            entry.calls = self.usage.calls - usage_before.calls
            entry.cost_usd = self.usage.cost_usd - usage_before.cost_usd
            entry.input_tokens = self.usage.input_tokens - usage_before.input_tokens

    def snapshot_usage(self) -> Usage:
        return Usage(
            calls=self.usage.calls,
            cost_usd=self.usage.cost_usd,
            input_tokens=self.usage.input_tokens,
        )

    # -- citations ------------------------------------------------------

    @property
    def citations(self) -> list[str]:
        if self.impact_map is not None:
            return [e.citation for e in self.impact_map.all_entries()]
        if self.retrieval is not None:
            return list(self.retrieval.citations)
        return []

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "request": self.request,
            "sha": self.sha,
            "lane": self.lane,
            "status": self.status,
            "abort_reason": self.abort_reason,
            "interpretation": self.interpretation.as_dict() if self.interpretation else None,
            "retrieval": self.retrieval.as_dict() if self.retrieval is not None else None,
            "impact_map": self.impact_map.as_dict() if self.impact_map else None,
            "history": self.history.as_dict() if self.history else None,
            "spec": self.spec,
            "narrative": self.narrative,
            "verification": self.verification,
            "followups": self.followups,
            "disagreements": self.disagreements,
            "answer": self.answer,
            "trace": [t.as_dict() for t in self.trace],
            "usage": self.usage.as_dict(),
            "budget": {
                "max_usd": self.budget.max_usd,
                "max_input_tokens": self.budget.max_input_tokens,
                "max_seconds": self.budget.max_seconds,
                "elapsed_s": round(self.budget.elapsed, 2),
            },
            "prior_context": self.prior_context,
            "params": self.params,
            "env": self.env,
            "started_at": self.started_at,
        }
