"""The orchestrator: run the nodes in order, persist after each, stream events.

`stream()` is the real implementation — an async generator the API can hand
straight to SSE. `run()` is the synchronous convenience wrapper that drains it.

Budget handling is the part worth reading. Every node boundary checks the four
ceilings, and exceeding one is **not** an error: the run stops where it is, marks
itself `aborted` with a reason, and answers from what it already has. A cartographer
that finished before the money ran out still produced an impact map with real
citations, and that is worth returning.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .. import config
from ..agents import architect, cartographer, changelog, explainer, historian, interpreter
from ..agents.llm import Budget, LLMClient, Usage
from ..agents.state import ImpactMap, RunState, citation_path, citations_in, citations_of, new_id
from ..redact import redact_text
from ..retrieval.retrieve import Retriever, _resolve_index_dir
from . import cache, followup, graph, quotas, verifier
from .store import RunStore


async def _run_node(executor: concurrent.futures.Executor | None, fn: Callable, *args: Any) -> Any:
    """Like ``asyncio.to_thread``, but backed by an explicit executor whose
    futures the caller can drain at shutdown. ``asyncio.to_thread``'s futures
    can't be cancelled once the thread has started, so a caller that owns
    long-lived resources (the API's ``app.state.retriever``) needs a real
    completion signal — not just the awaiting coroutine unwinding — before it
    closes those resources out from under an orphaned node thread."""
    if executor is None:
        return await asyncio.to_thread(fn, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(fn, *args))


@dataclass(slots=True)
class Event:
    """A progress event. `kind` is the discriminator the API streams on."""

    kind: str  # run_started | node_started | node_finished | lane_decided | partial | run_finished | error
    node: str = ""
    detail: str = ""
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "node": self.node, "detail": self.detail, "data": self.data or {}}


def _indexed_at(index_dir: Path) -> str:
    """The index's build date, for the answer's environment stamp. "" if unknown."""
    manifest = Path(index_dir) / config.MANIFEST_NAME
    if not manifest.is_file():
        return ""
    try:
        import json

        return str(json.loads(manifest.read_text(encoding="utf-8")).get("indexed_at") or "")[:10]
    except (OSError, ValueError):
        return ""


def _prior_context(store: RunStore, thread_id: str, run_id: str) -> str:
    """The previous run's impact map, summarised, for a follow-up."""
    previous = store.latest_in_thread(thread_id, before_run_id=run_id)
    if not previous:
        return ""
    impact = previous.get("impact_map") or {}
    lines: list[str] = []
    if previous.get("request"):
        lines.append(f"Previous question: {redact_text(previous['request'])}")
    if impact.get("summary"):
        lines.append(impact["summary"])
    for kind in ("endpoints", "classes_methods", "entities_tables", "screens"):
        for entry in (impact.get(kind) or [])[:3]:
            lines.append(f"- {entry.get('name')} ({entry.get('citation')})")
    return "\n".join(lines[:12])


def _apply_lane_cap(state: RunState, store: RunStore) -> None:
    """Tighten `state.budget.max_usd` if this lane is near its daily cap.

    Called right after `state.lane` is set — never before, per design law 3:
    the lane comes from the impact map, so there is nothing to cap until the
    map exists. This can only shrink the ceiling; a run under its lane's cap
    is completely unaffected. A run that DOES get tightened does not error —
    it simply hits `state.budget.exceeded()` sooner, at the next node
    boundary, the same abort-to-partial-answer path any other budget ceiling
    already uses.
    """
    if state.lane is None:
        return
    ceiling = quotas.lane_budget_ceiling(store, state.lane, state.budget.max_usd)
    if ceiling is None:
        return
    state.env["quota_lane_cap"] = {
        "lane": state.lane, "max_usd": round(ceiling, 4),
        "note": f"tightened to the remaining daily budget for lane {state.lane!r}",
    }
    state.budget.max_usd = ceiling


def split_header(answer: str) -> tuple[str, str]:
    """The first line of a composed answer, and everything after it.

    `compose_answer` always begins a non-aborted answer with exactly one
    header line, a blank line, then the body — so this is safe for any
    `answer` this system produced with `status == "done"`, which is the only
    status the cache ever stores (`_maybe_cache`). It is never attempted on a
    partial/aborted answer, which begins with a blockquote instead.
    """
    head, _, rest = answer.partition("\n\n")
    return head, rest


def _relative_age(iso_stamp: str) -> str:
    """`"2h ago"` / `"3d ago"` from a `created_at` string — for the "similar
    question answered <ago>" label, which reads badly as a raw timestamp."""
    try:
        then = datetime.fromisoformat(iso_stamp.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return iso_stamp
    seconds = max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _serve_cached(
    state: RunState, store: RunStore, cached_run_id: str, *, similar_to: str | None = None
) -> dict[str, Any] | None:
    """A copy of ``cached_run_id``'s persisted answer, patched onto this run.

    Returns the new run's payload dict, ready for `store.save_raw` — or `None`
    if the cached row turned out unusable (the source run was deleted, or
    somehow isn't a `"done"` run), in which case the caller falls through to a
    normal, fresh run rather than serving something broken.

    Deliberately does NOT duplicate `retrieval` (the chunk dump, which carries
    full source text) into the new row — copying megabytes of code on every
    cache hit would defeat the point of caching being cheap. `impact_map`,
    `spec`/`narrative`, and the rendered `answer` (with its citations) ARE
    copied in full, so the new run's own evidence and citations are complete;
    only the raw per-chunk dump is left pointing at the source run instead,
    via `env.cache.source_run_id`.

    ``similar_to`` marks a SEMANTIC hit (the source run answered a
    near-duplicate wording, not this exact question, per
    ``config.SEMANTIC_CACHE_THRESHOLD``) — the header says so plainly rather
    than presenting a paraphrase's answer as if it were computed for the
    question actually asked, and `env.cache.similar` carries the distinction
    through to the API/UI, which is expected to offer a one-click re-run
    (`fresh=True`) rather than treat this as equivalent to a real answer.
    """
    cached = store.state_of(cached_run_id)
    if cached is None or cached.get("status") != "done":
        return None

    original_header, body = split_header(cached.get("answer") or "")
    header_state = RunState(request=state.request, sha=state.sha, env=dict(state.env))
    header_state.lane = cached.get("lane")
    impact = cached.get("impact_map") or {}
    if impact:
        header_state.impact_map = ImpactMap(confidence=impact.get("confidence", "low"))
    header_state.verification = cached.get("verification")

    row = store.get(cached_run_id)
    source_stamp = row["created_at"] if row else "an earlier run"
    if similar_to:
        marker = (
            f"similar question answered {_relative_age(source_stamp)} "
            f"(“{similar_to}”, run {cached_run_id[:12]}) — re-run for a fresh answer"
        )
    else:
        marker = f"cached — see run {cached_run_id[:12]}, first computed {source_stamp}"
    header = f"{answer_header(header_state)} · {marker}"

    payload = dict(cached)
    payload.update(
        run_id=state.run_id,
        thread_id=state.thread_id,
        request=state.request,
        sha=state.sha,
        status="done",
        abort_reason=None,
        answer=f"{header}\n\n{body}" if body else header,
        usage=Usage().as_dict(),
        trace=[],
        retrieval=None,
        started_at=state.started_at,
        prior_context=state.prior_context,
        params=state.params,
    )
    payload["env"] = {
        **state.env,
        "cache": {
            "hit": True, "source_run_id": cached_run_id, "source_created_at": source_stamp,
            "similar": bool(similar_to), "source_question": similar_to,
        },
    }
    return payload


def _semantic_cache_lookup(state: RunState, store: RunStore, snapshot: str) -> dict[str, Any] | None:
    """A near-duplicate question already answered under `snapshot`, or None."""
    from ..index.embed import load_encoder

    lane_guess = cache.cheap_lane_guess(state.request)
    candidates = store.semantic_cache_candidates(snapshot, lane_guess)
    if not candidates:
        return None
    encoder = load_encoder(config.EMBED_MODEL)
    embedding = cache.embed_question(state.request, encoder)
    return cache.best_semantic_match(embedding, candidates)


def _maybe_cache(state: RunState, store: RunStore, retriever: Retriever, cache_write_ok: bool) -> None:
    """Write this run's answer into the cache, if this run is the kind that should be.

    ``cache_write_ok`` is the no-follow-up/not-changelog gate — deliberately
    NOT the same flag that gated the read a moment ago in `stream()`. `fresh`
    only ever bypasses the READ: a fresh run still refreshes the entry for next
    time (config.py — "recompute once," not "stop caching forever"), so this
    function's gate must not carry `fresh` at all, only the two conditions that
    make caching WRONG regardless of freshness (a follow-up whose answer can
    depend on thread history; the changelog lane, whose evidence is the live
    checkout, not the indexed commit). The one condition checked HERE rather
    than passed in is status: a run only earns a cache entry by actually
    finishing as `"done"` — never `aborted`, `needs_clarification`, or anything
    that errored along the way.
    """
    if not cache_write_ok or state.status != "done":
        return
    snapshot = cache.snapshot_key(retriever.repo, retriever.sha)
    embedding: list[float] | None = None
    if config.SEMANTIC_CACHE_ENABLED:
        # The REAL lane, now that the run has finished — not the cheap guess,
        # which exists only to filter candidates at LOOKUP time for a question
        # whose real lane isn't known yet. A local embed, no LLM call: this
        # never costs anything beyond what indexing already pays once to load
        # the same encoder.
        from ..index.embed import load_encoder

        embedding = cache.embed_question(state.request, load_encoder(config.EMBED_MODEL))
    store.cache_store(
        snapshot, cache.question_norm(state.request), state.run_id,
        lane=state.lane, embedding=embedding,
    )


async def stream(
    request: str,
    *,
    thread_id: str | None = None,
    params: dict[str, Any] | None = None,
    fresh: bool = False,
    index_dir: str | Path | None = None,
    llm: LLMClient | None = None,
    budget: Budget | None = None,
    store: RunStore | None = None,
    retriever: Retriever | None = None,
    executor: concurrent.futures.Executor | None = None,
) -> AsyncIterator[Event]:
    """Run the pipeline, yielding progress events as each node completes."""
    owns_store = store is None
    owns_retriever = retriever is None
    store = store or RunStore.open()
    index_dir = Path(index_dir) if index_dir else _resolve_index_dir()
    retriever = retriever or Retriever.open(index_dir)
    budget = budget or Budget.for_run()
    llm = llm or LLMClient(budget=budget)

    # Redacted once, here, before interpreter/cartographer/architect ever see
    # it (design law 8) — everything downstream reads state.request, so this
    # is the one place that closes every prompt site and the persisted/
    # prior_context leak in the same move.
    state = RunState(
        request=redact_text(request),
        thread_id=thread_id or new_id("thr"),
        sha=retriever.sha,
        budget=budget,
        params=dict(params or {}),
        env={
            "index_dir": str(index_dir),
            "repo": retriever.repo,
            "indexed_at": _indexed_at(index_dir),
            "providers": llm.status(),
            # Phase M quotas/costs — whatever the caller sent, default team
            # otherwise. There is no login system yet (api/main.py's own
            # docstring: single concurrent user by design), so this is the
            # honest unit of attribution available today, not a stand-in for
            # real multi-tenant auth.
            "team": (params or {}).get("team") or config.DEFAULT_TEAM,
        },
    )
    state.prior_context = _prior_context(store, state.thread_id, state.run_id)

    # The cache is only ever consulted for a fresh, standalone question — see
    # config.py's "answer cache" section for exactly why each of these three
    # gates exists. `changelog_trigger` is cheap (a few regex checks over a
    # short string) and gets called again below regardless, so recomputing it
    # here rather than restructuring the control flow around a shared value is
    # the smaller change.
    cache_payload: dict[str, Any] | None = None
    # Read-side gate: additionally skips a `fresh=True` request. Write-side gate
    # (`cache_write_ok`, used later by `_maybe_cache`) shares the same two
    # structural conditions but deliberately drops `fresh` — see that function's
    # docstring for why the two must not be the same flag.
    cache_write_ok = (
        config.CACHE_ENABLED
        and not state.prior_context
        and graph.changelog_trigger(state.request, state.params) is None
    )
    if cache_write_ok and not fresh:
        snapshot = cache.snapshot_key(retriever.repo, retriever.sha)
        norm = cache.question_norm(state.request)
        cached_run_id = store.cache_lookup(snapshot, norm)
        if cached_run_id:
            cache_payload = _serve_cached(state, store, cached_run_id)
        elif config.SEMANTIC_CACHE_ENABLED:
            # An exact miss. Before paying for a real run, check whether a
            # near-duplicate wording of the same question was already
            # answered — see orchestrator/cache.py's module docstring for why
            # "same lane" here is a cheap keyword guess rather than the real
            # (expensive, not-yet-known) lane.
            match = _semantic_cache_lookup(state, store, snapshot)
            if match is not None:
                cache_payload = _serve_cached(
                    state, store, match["run_id"], similar_to=match["question_norm"]
                )

    try:
        yield Event(
            "run_started",
            detail=request,
            data={
                "run_id": state.run_id,
                "thread_id": state.thread_id,
                "sha": state.sha,
                "follow_up": bool(state.prior_context),
            },
        )

        if cache_payload is not None:
            store.save_raw(cache_payload)
            yield Event(
                "run_finished",
                detail="cache hit",
                data={
                    "status": "done",
                    "lane": cache_payload.get("lane"),
                    "citations": len(citations_of(cache_payload)),
                    "cost_usd": 0.0,
                    "elapsed_s": 0.0,
                    "cache_hit": True,
                    "source_run_id": cache_payload["env"]["cache"]["source_run_id"],
                },
            )
            return

        store.save(state)

        # The one lane decided before any analysis, because its evidence is a
        # commit range rather than a search (see graph.changelog_trigger).
        plan = graph.plan_nodes(state)
        changelog_ask = graph.changelog_trigger(state.request, state.params)
        if plan is graph.CHANGELOG_SEQUENCE:
            state.lane = "changelog"
            _apply_lane_cap(state, store)
            yield Event(
                "lane_decided",
                node="changelog",
                detail="changelog",
                data={
                    "lane": "changelog",
                    "reason": changelog_ask.reason if changelog_ask else "",
                    "downgraded": False,
                    "range": changelog_ask.as_dict() if changelog_ask else None,
                },
            )

        for node in plan:
            # --- budget gate between every node
            reason = state.budget.exceeded(state.usage)
            if reason:
                state.status = "aborted"
                state.abort_reason = reason
                yield Event("error", node=node, detail=f"budget: {reason}")
                break

            if node == "architect" and not graph.wants_architect(state.lane):
                continue
            if node == "explainer" and not graph.wants_explainer(state.lane):
                continue

            yield Event("node_started", node=node)
            entry = state.begin(node)
            before = state.snapshot_usage()

            try:
                if node == "interpreter":
                    state = await _run_node(executor, interpreter.run, state, llm)
                    detail = state.interpretation.intent if state.interpretation else "?"
                    state.end(entry, "ok", detail, before)
                    # A disagreement across the interpreter's votes is written to
                    # its own table (not just kept in this run's JSON) — the
                    # point of collecting these is to mine them ACROSS runs.
                    for item in state.disagreements:
                        store.record_eval_candidate(state.run_id, item)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={
                            "intent": detail,
                            "assumptions": state.interpretation.assumptions if state.interpretation else [],
                            "clarifying_question": (
                                state.interpretation.clarifying_question if state.interpretation else None
                            ),
                            "disagreements": state.disagreements,
                        },
                    )
                    # One clarifying question is allowed to stop the run.
                    if state.interpretation and state.interpretation.clarifying_question:
                        state.status = "needs_clarification"
                        state.answer = state.interpretation.clarifying_question
                        store.save(state)
                        yield Event(
                            "run_finished",
                            detail="needs clarification",
                            data={"status": state.status, "question": state.answer},
                        )
                        return

                elif node == "cartographer":
                    state = await _run_node(executor, cartographer.run, state, llm, retriever)
                    impact = state.impact_map
                    detail = f"{impact.total if impact else 0} impacted, {impact.confidence if impact else 'low'}"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={
                            "impacted": impact.total if impact else 0,
                            "confidence": impact.confidence if impact else "low",
                            "modules": impact.modules if impact else [],
                            "citations": len(state.citations),
                        },
                    )
                    yield Event(
                        "partial",
                        node=node,
                        detail=impact.summary if impact else "",
                        data={"impact_map": impact.as_dict() if impact else None},
                    )

                    # --- lane is decided HERE, from the map (design law 3)
                    decision = graph.decide_lane(state)
                    state.lane = decision.lane
                    _apply_lane_cap(state, store)
                    yield Event(
                        "lane_decided",
                        node=node,
                        detail=decision.lane,
                        data={"lane": decision.lane, "reason": decision.reason,
                              "downgraded": decision.downgraded},
                    )
                    if decision.lane == "data_question":
                        state.answer = graph.DATA_QUESTION_REDIRECT
                        state.status = "done"
                        store.save(state)
                        _maybe_cache(state, store, retriever, cache_write_ok)
                        yield Event(
                            "run_finished",
                            detail="data_question redirect",
                            data={"status": state.status, "lane": state.lane},
                        )
                        return

                elif node == "followup":
                    state = await _run_node(executor, followup.run, state, llm, retriever)
                    report = state.followups or {}
                    detail = (
                        f"{report.get('fired', 0)} of {len(report.get('shots') or [])} shot(s), "
                        f"{report.get('merged_chunks', 0)} chunks"
                    )
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={
                            "fired": report.get("fired", 0),
                            "shots": report.get("shots") or [],
                            "chunks": report.get("merged_chunks", 0),
                        },
                    )

                elif node == "historian":
                    state = await _run_node(executor, historian.run, state, retriever.store)
                    volatile = len(state.history.volatile) if state.history else 0
                    detail = f"{len(state.history.files) if state.history else 0} files, {volatile} volatile"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={
                            "files": len(state.history.files) if state.history else 0,
                            "volatile": state.history.volatile if state.history else [],
                        },
                    )

                elif node == "architect":
                    state = await _run_node(executor, architect.run, state, llm, retriever)
                    detail = f"{len(state.spec or '')} chars"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished", node=node, detail=detail, data={"spec_chars": len(state.spec or "")}
                    )

                elif node == "explainer":
                    state = await _run_node(executor, explainer.run, state, llm, retriever)
                    detail = f"{len(state.narrative or '')} chars"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={"narrative_chars": len(state.narrative or "")},
                    )

                elif node == "verifier":
                    state = await _run_node(executor, verifier.run, state, retriever)
                    report = state.verification or {}
                    level = (report.get("confidence") or {}).get("level", "?")
                    dropped = len((report.get("answer") or {}).get("dropped") or [])
                    detail = f"confidence {level}, {dropped} citation(s) dropped"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={
                            "confidence": level,
                            "reasons": (report.get("confidence") or {}).get("reasons", []),
                            "dropped": dropped,
                        },
                    )

                elif node == "changelog":
                    state = await _run_node(
                        executor, changelog.run, state, llm, retriever, changelog_ask
                    )
                    report = (state.env.get("changelog") or {}).get("report") or {}
                    detail = f"{report.get('commits', 0)} commits, {len(report.get('areas') or [])} areas"
                    state.end(entry, "ok", detail, before)
                    yield Event(
                        "node_finished",
                        node=node,
                        detail=detail,
                        data={"commits": report.get("commits", 0), "areas": report.get("areas") or []},
                    )

            except Exception as exc:  # one node failing must not lose the rest
                state.end(entry, "error", f"{exc.__class__.__name__}: {exc}", before)
                state.usage.errors += 1
                yield Event("error", node=node, detail=f"{exc.__class__.__name__}: {exc}")

            store.save(state)

        if state.status == "running":
            state.status = "done"
        state.answer = state.answer or compose_answer(state)
        store.save(state)
        _maybe_cache(state, store, retriever, cache_write_ok)
        yield Event(
            "run_finished",
            detail=state.status,
            data={
                "status": state.status,
                "lane": state.lane,
                "citations": len(state.citations),
                "cost_usd": round(state.usage.cost_usd, 6),
                "elapsed_s": round(state.budget.elapsed, 2),
                "abort_reason": state.abort_reason,
            },
        )
    finally:
        if owns_retriever:
            retriever.close()
        if owns_store:
            store.close()


def answer_header(state: RunState) -> str:
    """One line saying what this answer is and what it cost.

    Lane, confidence, the exact commit it was read at, and the money — so a reader
    can tell a `high`-confidence walkthrough from a `low`-confidence one, and can
    tell whether the answer is about the code they are looking at today.
    """
    bits = [f"**{state.lane or 'question'}**"]
    if state.impact_map is not None:
        # A level the verifier never got to is the retrieval base and nothing
        # more: no coverage rule ran, no citation was checked. A run that aborts
        # on its time budget still prints a level, and printing it bare would let
        # the weakest run in the suite make the strongest-looking claim.
        checked = state.verification is not None
        suffix = "" if checked else " (unverified)"
        bits.append(f"confidence: {state.impact_map.confidence}{suffix}")
    elif state.lane == "changelog":
        # Nothing here was ranked or reranked; the commits either are in the range
        # or are not. Printing a confidence level would invent an uncertainty.
        bits.append("evidence: git log")
    repo = (state.env.get("repo") or "").strip()
    sha = (state.sha or "")[:12]
    stamp = f"{repo}@{sha}" if repo and sha else (sha or repo or "unknown index")
    indexed = state.env.get("indexed_at")
    if indexed:
        stamp += f", indexed {indexed}"
    bits.append(stamp)
    bits.append(f"${state.usage.cost_usd:.4f}")
    return " · ".join(bits)


def confidence_note(state: RunState) -> str:
    """Why the level is not higher, in one line, under the header.

    The score is computed from rules (orchestrator/confidence.py), so it can be
    shown — and a level a reader cannot interrogate is barely better than the
    model's own guess it replaced. Only the rules that actually *lowered* it are
    printed; the base line ("evidence scored high on retrieval") is the first
    reason and is not news when the level is still that.
    """
    impact = state.impact_map
    if impact is None:
        return ""
    if state.verification is None:
        return (
            "_Unverified: the run stopped before its citations were checked, so this level is "
            "the strength of the retrieved evidence only — no coverage rule ran._"
        )
    if impact.confidence == "high":
        return ""
    caps = list(impact.confidence_reasons or [])[1:]
    if not caps:
        return ""
    return f"_Why {impact.confidence}: " + "; ".join(caps) + "._"


def answer_body(state: RunState) -> str:
    """Whatever the lane's writer produced.

    Exactly one node writes the body: the explainer for a question, the architect
    for a change request or a bug, the changelog agent for a range. There is no
    fallback that assembles prose from the impact map — that assembly *was* the
    problem this phase set out to fix, so when no writer ran the answer says so
    and points at the evidence panel instead of imitating an explanation.
    """
    if graph.wants_architect(state.lane) and state.spec:
        return state.spec
    if state.narrative:
        return state.narrative
    if state.spec:
        return state.spec
    impact = state.impact_map
    if impact is not None and (impact.summary or impact.gaps):
        # No writer ran (a failed node, or a lane whose writer was unavailable).
        # The map's own summary and gaps are all there is — and the gaps in
        # particular must survive, since nothing else in the answer would then be
        # saying what the run could not see.
        parts = [impact.summary] if impact.summary else []
        parts.append(
            "_No walkthrough was written for this run — the evidence panel holds what it "
            "was based on._"
        )
        if impact.gaps:
            parts.append("**Gaps**\n" + "\n".join(f"- {gap}" for gap in impact.gaps))
        return "\n\n".join(parts)
    return "No answer could be produced."


def compose_answer(state: RunState) -> str:
    """The human-facing answer: a header, the lane writer's prose, its citations.

    Deliberately *not* a renderer of the impact map. The map is a structured
    artefact for the evidence panel (it is persisted in full and the API serves
    it separately); printing it into the answer produced a categorised inventory
    where a person had asked a question. The only structure added here is the
    citation index, and that is built by reading the body — so it lists what the
    answer actually cited rather than everything the run happened to look at.
    """
    parts: list[str] = []

    if state.abort_reason:
        parts.append(
            f"> **Partial answer** — the run stopped early ({state.abort_reason}). "
            "Everything below was established before it stopped."
        )

    parts.append(answer_header(state))
    why = confidence_note(state)
    if why:
        parts.append(why)
    body = answer_body(state)
    parts.append(body)

    if state.interpretation and state.interpretation.assumptions:
        parts.append(
            "**Assumptions**\n"
            + "\n".join(f"- {a}" for a in state.interpretation.assumptions)
        )

    cited = citations_in(body) or state.citations
    if cited:
        volatile = set(state.history.volatile) if state.history else set()
        lines = ["---", "**Citations**"]
        for citation in cited:
            flag = " `volatile`" if citation_path(citation) in volatile else ""
            lines.append(f"- `{citation}`{flag}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def run(
    request: str,
    *,
    thread_id: str | None = None,
    params: dict[str, Any] | None = None,
    fresh: bool = False,
    index_dir: str | Path | None = None,
    llm: LLMClient | None = None,
    budget: Budget | None = None,
    store: RunStore | None = None,
    retriever: Retriever | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> RunState:
    """Synchronous wrapper: drain :func:`stream` and return the final state."""
    captured: dict[str, Any] = {}

    async def drive() -> None:
        async for event in stream(
            request,
            thread_id=thread_id,
            params=params,
            fresh=fresh,
            index_dir=index_dir,
            llm=llm,
            budget=budget,
            store=store,
            retriever=retriever,
        ):
            if on_event is not None:
                on_event(event)
            if event.kind == "run_started":
                captured["run_id"] = event.data["run_id"]
                captured["thread_id"] = event.data["thread_id"]

    asyncio.run(drive())

    # Read the final persisted state back, so `run()` returns exactly what was saved.
    owned = store is None
    store = store or RunStore.open()
    try:
        payload = store.state_of(captured.get("run_id", ""))
    finally:
        if owned:
            store.close()
    return payload  # type: ignore[return-value]
