"""FastAPI app exposing the orchestrator over HTTP.

    POST /runs                    -> text/event-stream: progress | finding | answer | error | done
    GET  /runs/{run_id}           -> full persisted run
    GET  /threads                 -> recent threads, newest first
    GET  /threads/{thread_id}     -> runs in one thread (summaries; full detail via GET /runs/{id})
    POST /runs/{run_id}/feedback  -> {vote: up|down, note?}
    POST /vocab/proposals         -> {term, entities[], note?} — pending until approved by hand
    GET  /vocab/proposals         -> the review queue (hand-submitted + nightly-mined)
    POST /vocab/proposals/{id}/review -> {status: approved|rejected}
    GET  /suggestions             -> 8 questions generated once at index time
    GET  /health                  -> index sha, node/edge counts, staleness
    GET  /costs                   -> today's spend by repo/lane/team (Phase M quotas)
    GET  /access-audit            -> every access decision made (Phase M ACLs)

This file is the only place in Bridge that writes anything reachable from outside
the process. It still never writes to the *target* repository — every write here
lands in `config.RUNS_DB`, Bridge's own app store (CLAUDE.md's first design law).

**Concurrency note.** `app.state.retriever` and `app.state.run_store` are shared,
long-lived objects (loading the embedding model per request would cost seconds
each time). Orchestrator nodes run under `asyncio.to_thread`, but only ever touch
`retriever`'s GraphStore connection there (see the to_thread call sites in
orchestrator/run.py — they pass `llm`, `retriever`, or `retriever.store`, never
`store`). That GraphStore connection is the one that genuinely crosses threads and
isn't safe for simultaneous access. `run_store`'s reads and writes never leave the
event-loop thread: GET handlers below are `async def` (Starlette only threadpools
plain `def` routes), and `RunStore.save`/`latest_in_thread` are called synchronously
inline inside `stream()`, never wrapped in `to_thread` — so they can't interleave
mid-statement with a concurrent GET.
Rather than add connection pooling for a local, single-frontend dev tool,
`run_lock` fully serializes `POST /runs` — a second run waits for the first to
finish, which is what actually protects the GraphStore connection across concurrent
POSTs. That is a real limitation, not a hidden one: it belongs on the list of
things to revisit if Bridge grows beyond one concurrent user.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__, config
from ..agents.llm import Budget, LLMClient
from ..orchestrator import RunStore

# `orchestrator/__init__.py` re-exports the `run()` convenience function under the
# name `run`, which shadows the `orchestrator.run` *submodule* on the package
# object. Two consequences, both verified rather than assumed:
#   - `import bridge.orchestrator.run as x` does NOT work here — that form's last
#     step is `getattr(bridge.orchestrator, "run")`, which returns the function.
#   - `from bridge.orchestrator.run import stream` DOES work — that form resolves
#     the submodule via sys.modules and reads the attribute off the submodule
#     object itself, never touching the package's (shadowed) namespace.
from ..orchestrator.run import stream as orchestrator_stream
from ..orchestrator import quotas
from ..agents.state import citations_of
from ..retrieval.retrieve import Retriever, _resolve_index_dir
from ..security import acl
from . import db

# --------------------------------------------------------------------------- request bodies


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None
    # An explicit commit range routes the run to the changelog lane outright —
    # see orchestrator/graph.changelog_trigger. Loose validation on purpose: git
    # accepts a sha, a tag, or a ref, and this is handed to `git log` which is
    # the authority on what resolves. The character class is what keeps it from
    # being a shell/argument injection vector.
    from_sha: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_./~^@{}-]+$")
    to_sha: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_./~^@{}-]+$")
    # Bypass the answer cache and force this question to actually run. A fresh
    # run still REFRESHES the cache entry on completion — this is "recompute
    # once," not "stop caching this question forever."
    fresh: bool = False
    # Phase M quotas/costs: whoever is asking, for cost attribution and quota
    # enforcement (orchestrator/quotas.py). Bridge has no login system, so this
    # is caller-declared, not verified — the honest unit available today.
    team: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")

    def params(self) -> dict[str, str]:
        return {
            k: v for k, v in (
                ("from_sha", self.from_sha), ("to_sha", self.to_sha), ("team", self.team),
            ) if v
        }


class FeedbackRequest(BaseModel):
    vote: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=2000)


class VocabProposalRequest(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    entities: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=200
    )
    note: str | None = Field(default=None, max_length=2000)


class VocabReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


# --------------------------------------------------------------------------- lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    index_dir = _resolve_index_dir()
    app.state.index_dir = index_dir
    app.state.retriever = Retriever.open(index_dir)
    app.state.run_store = RunStore.open()
    app.state.run_lock = asyncio.Lock()
    app.state.node_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge-node")
    # Phase F: the queue and the index live in Postgres; the webhook and health
    # routes need an engine and should not each make their own.
    from ..db import create_all, get_engine

    app.state.db_engine = get_engine()
    try:
        create_all(app.state.db_engine)
    except Exception as exc:  # a missing database must not stop the answer API
        app.state.db_error = f"{exc.__class__.__name__}: {exc}"
    try:
        yield
    finally:
        # asyncio.to_thread futures cannot be cancelled once the thread has
        # started — a client disconnect or --reload restart cancels the
        # awaiting coroutine in _stream_run, but the node keeps running on its
        # thread against app.state.retriever/run_store. shutdown(wait=True)
        # blocks until every submitted node call actually finishes, so the
        # close()s below never race a thread still mid-`conn.execute`.
        app.state.node_executor.shutdown(wait=True)
        app.state.retriever.close()
        app.state.run_store.close()


app = FastAPI(title="Bridge", version=__version__, lifespan=lifespan)

from ..work.webhooks import router as work_router

app.include_router(work_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.API_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- shared helpers


def _read_manifest(index_dir: Path) -> dict[str, Any]:
    path = index_dir / config.MANIFEST_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _env_label(sha: str, index_dir: Path) -> str:
    manifest = _read_manifest(index_dir)
    indexed_at = manifest.get("indexed_at") or ""
    date_part = indexed_at.split("T", 1)[0] if indexed_at else "unknown date"
    short_sha = sha[:12] if sha else "unknown sha"
    return f"indexed at {short_sha}, {date_part}"


def _citations_of(state: dict[str, Any]) -> list[str]:
    """Thin alias for the shared implementation (agents/state.py::citations_of).

    Kept as a name in this module because it's the one every route here already
    calls; the logic itself moved so `RunStore.save_raw` (a cache-served run,
    which never builds a live RunState) can compute the same citation count
    without importing from the API layer.
    """
    return citations_of(state)


def _run_not_found(run_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no run {run_id!r}")


def _get_run_row(request: Request, run_id: str):
    row = request.app.state.run_store.get(run_id)
    if row is None:
        raise _run_not_found(run_id)
    return row


# --------------------------------------------------------------------------- SSE translation


def _sse_frame(event_type: str, data: dict[str, Any]) -> str:
    """One `text/event-stream` frame. `type` rides inside the JSON body too, so a
    client reading raw text (not using `EventSource.addEventListener`) still gets
    the discriminator without parsing the `event:` line."""
    payload = {"type": event_type, **data}
    body = json.dumps(payload, default=str)
    return f"event: {event_type}\ndata: {body}\n\n"


async def _stream_run(
    question: str,
    thread_id: str | None,
    app_state: Any,
    base_url: str,
    params: dict[str, str] | None = None,
    fresh: bool = False,
    quota_warning: str | None = None,
) -> AsyncIterator[str]:
    """Translate the orchestrator's node-level events into the API's 5-type contract.

    The orchestrator's event vocabulary (run_started, node_started, node_finished,
    lane_decided, partial, run_finished, error) is deliberately more granular than
    what a client needs — that granularity is for `data/runs.sqlite`'s trace, not
    for a UI. This function is the one place that translation happens, so the
    contract is defined once, not re-derived at every call site.
    """
    ctx: dict[str, Any] = {"run_id": None, "thread_id": thread_id}
    try:
        async with app_state.run_lock:
            budget = Budget.for_run()
            llm = LLMClient(budget=budget)
            async for event in orchestrator_stream(
                question,
                thread_id=thread_id,
                params=params,
                fresh=fresh,
                index_dir=app_state.index_dir,
                llm=llm,
                budget=budget,
                store=app_state.run_store,
                retriever=app_state.retriever,
                executor=app_state.node_executor,
            ):
                if event.kind == "run_started":
                    ctx["run_id"] = event.data.get("run_id")
                    ctx["thread_id"] = event.data.get("thread_id")
                    yield _sse_frame("progress", {"node": event.node, "detail": event.detail, **event.data})

                elif event.kind in ("node_started", "node_finished", "lane_decided"):
                    yield _sse_frame(
                        "progress", {"node": event.node, "detail": event.detail, **(event.data or {})}
                    )

                elif event.kind == "partial":
                    yield _sse_frame(
                        "finding", {"node": event.node, "detail": event.detail, **(event.data or {})}
                    )

                elif event.kind == "error":
                    yield _sse_frame("error", {"node": event.node, "detail": event.detail})

                elif event.kind == "run_finished":
                    run_id = ctx.get("run_id")
                    # Read the answer back from what was actually persisted, not
                    # from event.data — the three code paths that can trigger
                    # run_finished (normal completion, needs_clarification,
                    # data_question redirect) carry different data shapes, but
                    # they all leave a consistent state row behind.
                    state = (app_state.run_store.state_of(run_id) if run_id else None) or {}
                    citations = _citations_of(state)
                    yield _sse_frame(
                        "answer",
                        {
                            "run_id": run_id,
                            "answer": state.get("answer", ""),
                            "citations": citations,
                            "confidence": (state.get("impact_map") or {}).get("confidence"),
                        },
                    )
                    cache_info = state.get("env", {}).get("cache") or {}
                    yield _sse_frame(
                        "done",
                        {
                            "run_id": run_id,
                            "thread_id": state.get("thread_id") or ctx.get("thread_id"),
                            "status": state.get("status"),
                            "abort_reason": state.get("abort_reason"),
                            "lane": state.get("lane"),
                            "citations": len(citations),
                            "cost_usd": round((state.get("usage") or {}).get("cost_usd", 0.0), 6),
                            "elapsed_s": (state.get("budget") or {}).get("elapsed_s"),
                            "permalink": f"{base_url}/runs/{run_id}" if run_id else None,
                            "cache_hit": bool(cache_info.get("hit")),
                            # A semantic hit answered a DIFFERENT (near-duplicate)
                            # question — the UI is expected to label it "similar
                            # question answered <ago>" and offer fresh=True as a
                            # one-click re-run, not present it as a normal cache hit.
                            "cache_similar": bool(cache_info.get("similar")),
                            "cache_source_question": cache_info.get("source_question"),
                            "quota_warning": quota_warning,
                        },
                    )
    except Exception as exc:  # a bug here must still end the stream cleanly
        yield _sse_frame("error", {"node": "api", "detail": f"{exc.__class__.__name__}: {exc}"})
        yield _sse_frame(
            "done",
            {"run_id": ctx.get("run_id"), "thread_id": ctx.get("thread_id"), "status": "error"},
        )


# --------------------------------------------------------------------------- POST /runs


@app.post("/runs")
async def create_run(payload: RunRequest, request: Request):
    # The same caller-declared string is BOTH the quota's "team" and the ACL's
    # "principal" — see security/acl.py's module docstring for why there is
    # only one identity concept in a system with no login yet.
    team = payload.team or config.DEFAULT_TEAM
    repo_name = request.app.state.retriever.repo

    acl_decision = await run_in_threadpool(
        acl.check_access, acl.default_provider(), team, repo_name
    )
    await run_in_threadpool(
        request.app.state.run_store.record_access,
        principal=team, repo=repo_name, question=payload.question,
        sha=request.app.state.retriever.sha, allowed=acl_decision.allowed, reason=acl_decision.reason,
    )
    if not acl_decision.allowed:
        # A logged denial, not a silent one — the row above already exists
        # before this raises.
        raise HTTPException(status_code=403, detail=acl_decision.reason)

    # The one quota check that can refuse a run outright, and the only one
    # that can: a team is known before anything runs; a lane is not (design
    # law 3), so a lane's cap can only tighten a run already in progress
    # (orchestrator/run.py::_apply_lane_cap), never block one from starting.
    decision = await run_in_threadpool(quotas.check_team_quota, request.app.state.run_store, team)
    if decision.blocked:
        raise HTTPException(
            status_code=429,
            detail=f"team {team!r} has reached its daily quota (${decision.hard_limit:.2f}); "
                   f"${decision.spent_today:.2f} spent today",
        )

    base_url = str(request.base_url).rstrip("/")
    generator = _stream_run(
        payload.question, payload.thread_id, request.app.state, base_url,
        payload.params(), payload.fresh, decision.warning,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- GET /runs/{id}


@app.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    row = _get_run_row(request, run_id)
    state = json.loads(row["state"])
    sha = state.get("sha", "")
    index_dir = Path(((state.get("env") or {}).get("index_dir")) or request.app.state.index_dir)

    return {
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "request": state.get("request"),
        "status": state.get("status"),
        "lane": state.get("lane"),
        "abort_reason": state.get("abort_reason"),
        "answer": state.get("answer"),
        "impact_map": state.get("impact_map"),
        "spec": state.get("spec"),
        # the lane writer's prose — explainer (question) or changelog. `answer`
        # is this plus the header and citation block; this is the body alone.
        "narrative": state.get("narrative"),
        # how the confidence level was arrived at, and what happened to the
        # answer's citations when they were checked (orchestrator/verify.py)
        "verification": state.get("verification"),
        # evidence chunks WITH code text — the retrieval pass already scrubs and
        # caches `code` per chunk (see retrieve.py's Chunk.as_dict), so this is a
        # read of what was persisted, not a fresh repo access.
        "evidence": (state.get("retrieval") or {}).get("chunks", []),
        "trace": state.get("trace", []),
        "usage": state.get("usage"),
        "sha": sha,
        "env_label": _env_label(sha, index_dir),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------- threads


@app.get("/threads")
async def list_threads(request: Request, limit: int = config.THREADS_DEFAULT_LIMIT):
    return {"threads": request.app.state.run_store.all_threads(limit=limit)}


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request):
    rows = request.app.state.run_store.thread(thread_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
    return {
        "thread_id": thread_id,
        "runs": [
            # summary only — the `state` blob is the heavy part; fetch it via
            # GET /runs/{id} when a specific run's full detail is wanted.
            {key: row[key] for key in row.keys() if key != "state"}
            for row in rows
        ],
    }


# --------------------------------------------------------------------------- feedback


@app.post("/runs/{run_id}/feedback")
async def submit_feedback(run_id: str, payload: FeedbackRequest, request: Request):
    _get_run_row(request, run_id)  # 404s if the run does not exist
    # db.record_feedback does blocking sqlite I/O — offloaded so it can't stall
    # the event loop (and every other in-flight request, including a streaming
    # POST /runs) for the duration of the write.
    return await run_in_threadpool(db.record_feedback, run_id, payload.vote, payload.note)


# --------------------------------------------------------------------------- vocab proposals


@app.post("/vocab/proposals")
async def propose_vocab(payload: VocabProposalRequest):
    return await run_in_threadpool(
        db.record_vocab_proposal, payload.term, payload.entities, payload.note
    )


@app.get("/vocab/proposals")
async def get_vocab_proposals(status: str | None = None):
    """The review queue — both hand-submitted proposals and the ones
    work/vocab_staleness.py's nightly pass writes on its own."""
    return {"proposals": await run_in_threadpool(db.list_vocab_proposals, status)}


@app.post("/vocab/proposals/{proposal_id}/review")
async def review_vocab_proposal(proposal_id: int, payload: VocabReviewRequest):
    result = await run_in_threadpool(db.review_vocab_proposal, proposal_id, payload.status)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    return result


# --------------------------------------------------------------------------- suggestions


@app.get("/suggestions")
async def get_suggestions(request: Request):
    path = request.app.state.index_dir / config.SUGGESTIONS_FILE_NAME
    if not path.is_file():
        return {
            "sha": request.app.state.retriever.sha,
            "questions": [],
            "source": None,
            "note": "no suggestions.json for this index — re-run `bridge index` to generate one",
        }
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- health


@app.get("/health")
async def health(request: Request):
    index_dir: Path = request.app.state.index_dir
    manifest = _read_manifest(index_dir)
    sha = manifest.get("head_sha", "")
    repo_path = manifest.get("repo_path")

    current_head_sha: str | None = None
    stale = False
    if repo_path and Path(repo_path).is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=config.HEALTH_CHECK_GIT_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                current_head_sha = result.stdout.strip()
                stale = bool(current_head_sha) and current_head_sha != sha
        except (OSError, subprocess.TimeoutExpired):
            pass  # repo unreachable — staleness simply can't be determined

    indexed_at = manifest.get("indexed_at")
    age_hours: float | None = None
    if indexed_at:
        try:
            indexed_dt = datetime.fromisoformat(indexed_at)
            age_hours = round((datetime.now(timezone.utc) - indexed_dt).total_seconds() / 3600, 1)
        except ValueError:
            pass

    counts = manifest.get("counts", {})
    return {
        "status": "ok",
        "repo_name": manifest.get("repo_name"),
        "sha": sha,
        "indexed_at": indexed_at,
        "age_hours": age_hours,
        "nodes": counts.get("nodes", {}),
        "edges": counts.get("edges", {}),
        "stale": stale,
        "current_head_sha": current_head_sha,
    }


# --------------------------------------------------------------------------- costs


@app.get("/costs")
async def get_costs(request: Request, days: int = 1):
    """Spend by repo, lane, and team, over the last `days` days.

    Three separate GROUP BYs rather than one row per (repo, lane, team) triple:
    a settings page asking "who is expensive" wants each dimension's own
    ranking, and collapsing them into one cross-product table would bury that
    under cells nobody is looking at.
    """
    store = request.app.state.run_store
    by_repo, by_lane, by_team = await asyncio.gather(
        run_in_threadpool(store.costs_by, "repo", since_days=days),
        run_in_threadpool(store.costs_by, "lane", since_days=days),
        run_in_threadpool(store.costs_by, "team", since_days=days),
    )
    return {
        "days": days,
        "by_repo": by_repo,
        "by_lane": by_lane,
        "by_team": by_team,
        "quotas": {
            "team_daily_soft_usd": config.QUOTA_TEAM_DAILY_USD_SOFT,
            "team_daily_hard_usd": config.QUOTA_TEAM_DAILY_USD_HARD,
            "lane_daily_usd": config.QUOTA_LANE_DAILY_USD,
        },
    }


# --------------------------------------------------------------------------- access audit


@app.get("/access-audit")
async def get_access_audit(request: Request, repo: str | None = None, principal: str | None = None):
    """Every access decision this process has made (security/acl.py) — who,
    what repo, what question, allowed or denied, and why."""
    rows = await run_in_threadpool(
        request.app.state.run_store.access_audit, repo=repo, principal=principal
    )
    return {
        "entries": [
            {
                "principal": row["principal"], "repo": row["repo"], "question": row["question"],
                "sha": row["sha"], "allowed": bool(row["allowed"]), "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }
