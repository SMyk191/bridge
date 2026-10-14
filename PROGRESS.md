# Bridge — PROGRESS

## Phase 1 — the indexer ✅ (2026-08-19)

Structural graph over a repo at a fixed SHA. No embeddings, no agents, no API — by design.
Verified end-to-end against `spring-petclinic @ 88e37c15cf6fc8490b01bc3e8e2c800cec1ac272`.

```bash
cd backend && .venv/bin/python -m bridge.index.build ../data/repos/spring-petclinic
```
```bash
cd backend && .venv/bin/python -m pytest tests/test_index_petclinic.py -v
```

`bridge index <repo>` is the same entry point via console script and works from any cwd.

### What shipped

| Module | Does |
|---|---|
| `backend/bridge/index/ids.py` | Node id construction/parsing + the `repo@sha:path:start-end` citation format |
| `backend/bridge/index/model.py` | Node/Edge/Unresolved rows, kind vocabularies, `SEMANTIC_EDGE_KINDS` |
| `backend/bridge/index/parse_java.py` | tree-sitter Java → `ParsedFile`; `TypeIndex` symbol table; `CallResolver` |
| `backend/bridge/index/spring.py` | Endpoints, entities, JPA associations, Spring Data repositories, hierarchy |
| `backend/bridge/index/screens.py` | Thymeleaf `screen` nodes, `renders`, `includes` |
| `backend/bridge/index/graph.py` | SQLite store, degrees, hubs, `traverse()`, summary queries |
| `backend/bridge/index/build.py` | Orchestrator + CLI + summary |
| `backend/bridge/config.py` | Every tunable constant (models, hub percentile, Spring vocab, ignore globs) |

Output: `data/index/<sha>/graph.db` + `manifest.json`. The target repo is only ever read.

### Real numbers (petclinic @ 88e37c15)

**320 nodes** — 187 method, 49 file, 46 class, 17 endpoint, 12 screen, 6 entity, 3 interface.
**724 edges** — calls 382 (high 303 / medium 29 / low 50), contains 236, imports 24, includes 19,
handles_route 17, renders 16, associates 8, extends 8, reads_entity 8, maps_to 6.
0 dangling edges. Index builds in ~0.09s. **26 tests pass.**

Top 5 by semantic in-degree (first four are the flagged hubs, top 1% of 320 = 4):

| in | node | kind |
|---:|---|---|
| 23 | `java:…owner.OwnerRepository#findById` | method **HUB** |
| 21 | `java:…model.NamedEntity#setName` | method **HUB** |
| 18 | `java:…model.NamedEntity#getName` | method **HUB** |
| 16 | `java:…owner.Owner#getPet(String)` | method **HUB** |
| 14 | `java:…owner.OwnerRepository` | interface |

### Corrections to the plan's predictions

- 49 `.java` files, not 50 (30 main / 19 test).
- 16 `renders` edges, not ~14 — and all 16 are `high`, see the tightening note below.
- **`screen:fragments/layout` is not a hub.** Predicted it would be; it has semantic in-degree 11
  (all `includes`) and the top-1% cut lands at 16. The threshold was left alone rather than tuned
  to make the prediction come true.

### Decisions worth remembering

- **`in_degree` vs `semantic_in_degree`.** `in_degree`/`out_degree` count every edge (the spec's
  requirement). Hub ranking uses `semantic_in_degree` only, so imports and containment cannot
  manufacture a hub.
- **`implements` is structural, `implemented_by` is semantic** — same relation, inverted, opposite
  sides of the split. `implements` walks *up* to an abstraction nobody needs to change;
  `implemented_by` is the dispatch hop impact analysis must follow. Comment lives in `model.py`.
- **`traverse(include_members=True)`.** Excluding `contains` from the default (semantic) edge set
  severs a class from its own methods, and every semantic edge on a controller hangs off its
  methods — traversal from a class node would return exactly one node. Member expansion adds
  type → declared methods at **zero depth cost**, because for impact analysis a type and its
  members are one unit. `test_traversal_needs_member_expansion_to_leave_a_type_node` pins this.
- **`renders` needs a view *position*, not just a matching string.** First cut emitted an edge for
  any body literal matching a template name. That produced `processCreationForm → screen:error`
  from `addFlashAttribute("error", …)` and turned every controller test's
  `view().name("owners/findOwners")` assertion into a render. Now: a **literal** counts only in a
  return statement / `new ModelAndView(...)` / view setter; a **constant reference** counts
  anywhere, since `static final String VIEWS_X = "owners/x"` is a view name by construction and
  still has to match a real template. Result: 16 edges, all real, all main-source.
- **Unqualified calls emit no edge.** First cut resolved an implicit-`this` receiver to the
  enclosing class, so a statically imported `given(...)` produced `TestClass → TestClass`. That
  made the four biggest test classes the top hubs (76/40/36/27 in-degree of pure self-loops).
  Nobody wrote a receiver, so there is no type dependency to assert — UNRESOLVED
  (`unqualified_external`) only. Regression-guarded by
  `test_unqualified_external_calls_produce_no_self_edge`.

### UNRESOLVED inventory — 1221 rows, all calls

| reason | n | what it means | what would fix it |
|---|---:|---|---|
| `external_receiver` | 866 | receiver written, type resolves outside the repo (`model.addAttribute`, `result.hasErrors`) | indexing dependency jars / a JDK+Maven classpath resolver — out of scope for a source-only index |
| `unqualified_external` | 322 | unqualified call no in-repo type declares — static imports (`given`, `status`, `assertThat`) | same; also mostly test-only noise, could be filtered by `is_test` |
| `ambiguous` | 19 | several in-repo types declare the simple name; edges emitted to **all** at `confidence: low` | real receiver-type inference (flow-sensitive), or the classpath resolver above |
| `external_supertype_method` | 14 | receiver type **is** in-repo but the method comes from an external supertype — `owners.save(…)` on `JpaRepository` | modelling Spring Data's inherited CRUD surface as synthetic methods on the repository interface |

The last row is the honest-edge case: the type dependency is a fact (the receiver was written), the
exact method is not. It gets a `calls` edge **to the type node** at `confidence: low` carrying
`{unresolved_method, reason}` **and** an UNRESOLVED row. Both halves are asserted by tests, and a
mutation check confirmed the confidence assertion actually bites.

### Carry-forward caveats

- **interface→impl resolution is implemented but unexercised by petclinic.** `implemented_by` and
  the `via: interface_impl` call fan-out both produced **0 edges** here: the repo's only in-repo
  `implements` clauses are `PetTypeFormatter implements Formatter<PetType>` and
  `PetValidator implements Validator`, and both interfaces are *external*; the Spring Data
  repository interfaces have no in-repo implementations at all. The code path is therefore
  **untested against real data — validate against a large production codebase later**, where a service/impl split will
  actually exercise it.
- **Method ids are SHA-relative for overloaded names.** A method gets the bare
  `java:<FQCN>#name` id when the name is unique in its type, and `#name(P1,P2)` when the type
  declares more than one. That keeps the spec's exact id shape
  (`…OwnerController#processCreationForm`) and never merges two distinct methods, but it means
  **adding an overload in a later commit renames its existing sibling** — `#findAll` becomes
  `#findAll()`. Ids must not be persisted across SHAs without remapping. (Live example:
  `VetRepository` declares both `findAll()` and `findAll(Pageable)`, so both carry signatures.)
- Nested types use dotted FQNs (`PetControllerTests.ProcessCreationFormHasErrors`), not the JVM
  binary `$` form.
- `error.html` has no `renders` edge — nothing in Java names it; Spring resolves it implicitly.
  Correct, but a reminder that convention-resolved views are invisible to a source-only index.

### Not built in this phase (by instruction)

Embeddings / LanceDB, agents + prompts, FastAPI, git-history and vocabulary passes, frontend.
`config.py` already declares the model constants so no agent module inlines one later.

### Next

1. Validate the indexer against a large production codebase (real interface→impl, larger graph, hub behaviour at scale).
2. Semantic search pass: chunk + embed nodes with `BAAI/bge-small-en-v1.5` into LanceDB, keyed by node id.
3. Git-history pass (churn, ownership) as node attrs.

---

## Phase 2 — semantic + history layers, and the single retrieval function ✅ (2026-08-19)

```bash
cd backend && .venv/bin/python -m bridge.index.build ../data/repos/spring-petclinic
```
```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # 67 passed
```

### What shipped

| Module | Does |
|---|---|
| `bridge/redact.py` | 8 rule classes (AWS keys, assigned secrets, JWTs, PEM blocks, emails, Indian phones, 12-digit IDs); counts reported per run |
| `bridge/agents/llm.py` | Two providers behind one role-addressed door; token + USD accounting; budget enforcement; availability decided at construction |
| `bridge/agents/prompts/*.md` | `describe`, `rerank` — loaded at runtime, never inlined |
| `bridge/index/describe.py` | 1–2 sentence PM-facing descriptions, content-addressed cache |
| `bridge/index/embed.py` | breadcrumb + description → 384-d vectors in LanceDB; `semantic_search(q, k)` |
| `bridge/index/history.py` | churn / recency / module owner, never a person |
| `config/vocab.petclinic.yaml` + `retrieval/vocab.py` | 8 product terms → node ids; `expand_query()` |
| `retrieval/retrieve.py` | expand → semantic → keyword → graph → rerank → 15 cited chunks + confidence |
| `tools/compare_retrieval.py` | snapshots both retrieval layers so two index builds can be diffed |

### Real numbers (petclinic @ 88e37c15, gemini-3.5-flash-lite)

130 descriptions, **130 from model / 0 fallback**, 69 177 in / 2 680 out tokens, **$0.0275**.
130 vectors (384-d). History: 61 files, 11 modules. Redactions: 0 (no secrets or PII in petclinic).
Timings: graph 0.06s · history 0.06s · describe 14.0s · embed 15.1s · **total 29.3s**.
Retrieval: ~$0.0016/question, all three probes `confidence: high`.

### Does description-embedding change what comes back? Yes — measured

`tools/compare_retrieval.py diff fallback credentialed`, reranker held constant:

| Question | semantic top-5 overlap | retrieved top-5 overlap | confidence |
|---|---|---|---|
| How does an owner add a pet? | 2/5 | **0/5** | low → high |
| Where do we store vet specialties? | 4/5 | 1/5 | low → high |
| Can a customer book an appointment? | 1/5 | 1/5 | low → high |

With identifier-derived fallback descriptions, Q1's semantic top-5 was five `Owner`/`Pet`
data-model methods and **zero** controllers or screens. With model descriptions, three of the
five are the controller layer (`PetController` #2, `pets/createOrUpdatePetForm` #3,
`PetController#initCreationForm` #4).

**A prediction that failed, and why it matters.** I predicted
`PetController#processCreationForm` — the method that actually saves a new pet — would enter
Q1's semantic top-5. It did not: it sits at **rank #35**. Its description leads
*"Handles the submission of the new pet form…"*, while `initCreationForm` leads *"Opens the form
that lets someone **add a new pet** to an owner's profile."* The question asks "how does an owner
**add a pet**", so the form-opener wins on phrasing even though the submission handler is the
better answer. **Descriptions inherit whatever framing the describer chose to lead with, and that
framing — not the behaviour — is what the embedding matches.** The reranker corrected it to #3
(score 9.0/10), which is direct evidence the rerank stage is not redundant with the embedding.

### Carry-forward caveats

- **Requested Gemini 2.5 models are gated on this key.** `gemini-2.5-flash-lite` and
  `gemini-2.5-pro` return 404 "no longer available to new users". Roles use the successors the
  API itself names — `gemini-3.5-flash-lite` and `gemini-3.1-pro-preview`; `gemini-2.5-flash`
  works and is unchanged. The 2.5 ids and prices are retained in `config.py` for a one-line revert.
- **The 3.x tiers cannot disable thinking** (`thinking_budget=0` → 400). Those roles carry large
  `max_tokens` headroom instead; too small a ceiling returns empty text with `MAX_TOKENS`.
- **Gemini bills thinking at the output rate** — folded into `output_tokens` before costing.
- **A short shell value can shadow a real key.** `~/.zshenv` held a placeholder that beat the real
  key in `.env`; `config.provider_key` now rejects values under 20 chars and says where the junk is.
- Phase-1 caveats stand: interface→impl resolution is unexercised by petclinic, and method ids are
  SHA-relative for overloaded names.

### Next

1. Validate against a large production codebase (real interface→impl, larger graph, vocabulary at scale).
2. The agent pipeline on top of `retrieve()` — Interpreter → Cartographer → Architect.
3. Consider a second description per chunk phrased as a *question* the chunk answers, to reduce
   the leading-phrase bias measured above.

---

## Phase 3 — agents and the deterministic orchestrator ✅ (2026-08-19)

```bash
cd backend && .venv/bin/python ../eval/run_smoke.py          # all 10 questions
cd backend && .venv/bin/python ../eval/run_smoke.py add-pet  # one, with the full answer
cd backend && .venv/bin/python -m pytest tests/ -q           # 95 passed
```

### What shipped

| Module | Does |
|---|---|
| `agents/state.py` | `RunState` + `ImpactMap`/`Interpretation`/`HistoryReport`/`TraceEntry`, all JSON-round-trippable |
| `agents/interpreter.py` | normalise + classify intent; at most one clarifying question, else assumptions |
| `agents/cartographer.py` | `retrieve()` → per-module summaries (cheap tier) → single merge (deep tier) → impact map |
| `agents/historian.py` | churn/recency/owner per impacted file. **No LLM** |
| `agents/architect.py` | technical brief for change_request/bug; forces the "what would make me more sure" line |
| `orchestrator/graph.py` | fixed node sequence + the lane rule table |
| `orchestrator/store.py` | `data/runs.sqlite`, one row per run, saved after every node |
| `orchestrator/run.py` | async event stream, budget gates, graceful partial answers |
| `eval/smoke_questions.yaml` + `eval/run_smoke.py` | 10 product-language questions, per-question recall/confidence/cost |

### Smoke results — 10 questions

**mean file recall 95%** (9/10 at 100%) · **7/10 high confidence** · **$0.4458 total, $0.0446/question** · 322s

| id | recall | conf | lane | cost |
|---|---:|---|---|---:|
| add-pet | 100% | high | question | $0.0580 |
| vet-specialties | 100% | high | question | $0.0212 |
| visit-missing | **50%** | high | bug | $0.0584 |
| pet-weight | 100% | medium | change_request | $0.1007 |
| find-owner | 100% | high | question | $0.0566 |
| book-visit | 100% | high | question | $0.0382 |
| phone-validation | 100% | low | question | $0.0311 |
| species-list | 100% | high | question | $0.0230 |
| rename-owner-customer | 100% | high | question | $0.0418 |
| how-many-owners | 100% | low | data_question (redirected) | $0.0167 |

The one miss: `visit-missing` ("a visit isn't appearing on the owner page") cited
`ownerDetails.html` but not `VisitController.java`. The retrieval reached the *display* side
and not the *write* side — a bug question needs both, and the map claimed `high` confidence
while missing half the path. That is the most useful failure in the set: **confidence is
currently scored on how well the evidence hangs together, not on whether it spans the whole
read/write path a bug requires.**

### Decisions worth remembering

- **Lane is set after the cartographer, from the map** (design law 3). The rule that earns this
  is the *downgrade*: an ask that sounds like a change request but produced an empty or
  thin-and-low-confidence map becomes a `question`, because there is nothing to write a brief
  against. A `question` is never promoted the other way — a rich map does not mean the user
  asked for a work plan.
- **Fan out for reading, single-thread the merge** (design law 2). Per-module summaries run on
  the cheap tier and each sees only its own chunks, so no module wins the merge on chunk volume
  alone. One deep call then merges. With a single module the fan-out is skipped.
- **The merge cannot out-confide its evidence.** Impact-map confidence is capped at retrieval
  confidence, and the cap is recorded in `gaps`.
- **Fabricated citations are dropped, not repaired.** The merge's output is filtered against the
  citations actually retrieved; anything else is discarded before it can reach a user.
- **A partial answer is a real answer.** Exceeding any of the four budget ceilings marks the run
  `aborted` and answers from what was already established, rather than raising.
- **`GraphStore` needed `check_same_thread=False`** — nodes run under `asyncio.to_thread`, so the
  connection outlives the thread that opened it. Nodes run strictly sequentially, so there is no
  concurrent access. Found by the first smoke run, which failed with 0% recall.

### describe.md now leads with behaviour — UNTESTED against the baseline

The prompt now requires the behaviour first ("Saves a new pet to an owner") and forbids opening
with the lifecycle stage ("Handles the submission of the pet form"). This directly targets the
phrasing bias measured in phase 2, where `PetController#processCreationForm` sat at semantic
rank #35 because its description led with *"Handles the submission…"*. The description is now
*"Adds a new pet to an owner's profile while validating…"*.

**This has not been A/B'd against the phase-2 baseline.** The prompt change invalidates the
description cache (the key covers the prompt), so all 130 descriptions were regenerated and the
old vectors are gone; `tools/retrieval-snapshots/credentialed.json` holds the pre-change
snapshot but the vectors behind it no longer exist. **The reranker currently compensates** — it
pulled `processCreationForm` from #35 to #3 before the change, so smoke recall would likely look
similar either way. Re-running `compare_retrieval.py` against a rebuilt pre-change index is the
honest way to measure whether the behaviour-first rule actually moved the embedding, and it has
not been done.

### Carry-forward caveats

- **Confidence does not model path coverage.** See `visit-missing` above.
- `pet-weight` cost $0.1007 — a quarter of the per-run budget on one question. The multi-module
  fan-out is the driver; worth watching as repos get larger than petclinic.
- Phase-1 and phase-2 caveats stand: interface→impl unexercised, SHA-relative overload ids,
  Gemini 2.5 flash-lite/pro gated on this key, 3.x tiers cannot disable thinking.

### Next

1. Score confidence on read/write path coverage, not just evidence coherence.
2. FastAPI over `orchestrator.stream()` — the event contract already exists.
3. Validate against a large production codebase.

---

## Phase 4 — HTTP API ✅ (2026-08-19)

```bash
nohup backend/.venv/bin/uvicorn bridge.api.main:app --host 127.0.0.1 --port 8000 &
curl -N -sS -X POST http://127.0.0.1:8000/runs -H "Content-Type: application/json" \
  -d '{"question": "What happens when an owner adds a new pet?"}'
curl -sS http://127.0.0.1:8000/runs/<run_id>
cd backend && .venv/bin/python -m pytest tests/ -q   # 95 passed
```

### What shipped

| Module | Does |
|---|---|
| `api/main.py` | The FastAPI app: 8 routes, SSE translation, CORS, lifespan-scoped `Retriever`/`RunStore`/node executor |
| `api/db.py` | `feedback` + `vocab_proposals` tables — Bridge's own opinion about itself, never the target repo |
| `index/suggest.py` | 8 suggested questions generated once at index time from top endpoints/screens |
| `orchestrator/store.py::all_threads` | One row per thread, latest run's summary, deterministic ordering |
| `Makefile` | `make index` / `make api` / `make ui` / `make smoke` / `make seed` from repo root, no `cd` |

Endpoints, matching `api/main.py`'s module docstring exactly:

```
POST /runs                    -> text/event-stream: progress | finding | answer | error | done
GET  /runs/{run_id}           -> full persisted run
GET  /threads                 -> recent threads, newest first
GET  /threads/{thread_id}     -> runs in one thread (summaries; full detail via GET /runs/{id})
POST /runs/{run_id}/feedback  -> {vote: up|down, note?}
POST /vocab/proposals         -> {term, entities[], note?} — pending until approved by hand
GET  /suggestions             -> 8 questions generated once at index time
GET  /health                  -> index sha, node/edge counts, staleness
```

Live-verified end to end: a streamed "question"-lane run (11 SSE events, architect correctly
skipped), a streamed "bug"-lane run (13 events, architect ran, technical brief produced), 404s on
unknown run/thread ids, CORS preflight (`localhost:5173` allowed, everything else rejected),
`GET /threads`/`GET /threads/{id}`, feedback and vocab-proposal writes.

### Two bugs found while wiring this phase together

- **`LLMClient` only existed if `with_describe=True`.** `build.py`'s `suggest` pass also needs an
  LLM client, but the construction was scoped inside the describe block. Fixed by hoisting
  `client = llm if llm is not None else LLMClient(...)` above both blocks.
- **Import shadowing.** `orchestrator/__init__.py` re-exports the `run()` convenience function
  under the name `run`, which shadows the `orchestrator.run` *submodule* on the package object —
  `import bridge.orchestrator.run as x` silently returns the function, not the module, so
  `x.stream` doesn't exist. Verified both the broken form and the fix
  (`from bridge.orchestrator.run import stream as orchestrator_stream`, which resolves via
  `sys.modules` and bypasses the package's shadowed namespace) before committing to it — the
  reasoning is preserved as a comment in `api/main.py` so it can't silently regress.

### Adversarial review (Workflow tool, 5 dimensions × verify pass, 21 agents)

SSE-contract compliance, SQLite thread/concurrency safety, security/CORS/input validation,
endpoint-by-endpoint spec compliance, and design-law compliance — each reviewed independently,
then every finding adversarially re-verified by a second agent instructed to try to refute it
first. 16 candidate findings → **13 confirmed, 3 correctly refuted** (a claimed inconsistency in
the `done` frame's shape, a claimed race in `run_smoke.py`'s sequential loop, and a claimed gap in
`TraceEntry` — all checked against the actual code and found to not hold up).

All 13 confirmed findings were fixed. One process note worth recording honestly: the verify-stage
agent for finding #1 (see below) went beyond its brief — it was asked to *report* a fix, and
instead applied it directly to `api/main.py` mid-review. The edit itself was checked and correct
(confirmed by re-reading the diff and re-running the full test suite), but it wasn't authorized to
make it; every other finding was applied by hand afterward, and this is flagged here rather than
passed over quietly.

| # | Fix | File(s) |
|---|---|---|
| 1 | `_citations_of()`'s fallback read a `"citations"` key that `RetrievalResult.as_dict()` never emits (dead code, always `[]`) — an impact map that ends up empty despite real retrieved evidence silently reported zero citations. Now reads `citations` off the persisted `chunks` list instead. | `api/main.py` |
| 2 | `asyncio.to_thread` futures can't be cancelled once started — a client disconnect or `--reload` restart could leave an orphaned node thread still running against `app.state.retriever` after `lifespan`'s `finally` closed it. Added an explicit `ThreadPoolExecutor`, drained with `shutdown(wait=True)` before closing. | `api/main.py`, `orchestrator/run.py`, `index/graph.py` (comment) |
| 3 | `RunStore.save()` had no rollback on a failed write, leaving the long-lived connection sitting on an open transaction that could block `api/db.py`'s separate connections to the same file. | `orchestrator/store.py` |
| 4 | The module docstring's concurrency note overstated which object actually crosses threads (`RunStore` never does — only `GraphStore`, via the `to_thread` node calls). Corrected. | `api/main.py` |
| 5 | `db.record_feedback`/`record_vocab_proposal` do blocking sqlite I/O directly inside `async def` routes, stalling the whole event loop (including an in-flight SSE stream) for the write's duration. Offloaded via `run_in_threadpool`. | `api/main.py` |
| 6 | **The user's raw question was never redacted before reaching an LLM** — design law 8 says secrets/PII are scrubbed before *any* text reaches a model, but every redaction call site scrubbed retrieved repo text only. Now `redact_text()` runs once on `state.request` immediately after `RunState` is constructed, closing the interpreter/cartographer/architect prompt sites and the prior-context leak in one place. | `orchestrator/run.py` |
| 7 | `VocabProposalRequest.entities` had no length cap, unlike every other free-text field in the file. Bounded to 200 items × 200 chars each. | `api/main.py` |
| 8 | The Makefile's comment explaining `--reload-dir` cited a factually wrong reason (an "editable-install redirect" that has nothing to do with uvicorn's file watcher). Replaced with the real reason (avoiding a reload storm from `data/repos/`/`data/index/`). | `Makefile` |
| 9 | `health()`'s git-subprocess timeout was a bare `5` literal, unlike every other timeout in the codebase. Moved to `config.HEALTH_CHECK_GIT_TIMEOUT_SECONDS`. | `config.py`, `api/main.py` |
| 10 | The FastAPI app hardcoded `version="0.1.0"` instead of the package's single source of truth. Now uses `bridge.__version__`. | `api/main.py` |
| 11 | `GET /threads`'s default page size (`50`) duplicated `RunStore.all_threads`'s default by coincidence, not by reference. Both now read `config.THREADS_DEFAULT_LIMIT`. | `config.py`, `api/main.py`, `orchestrator/store.py` |
| 12 | `api/db.py`'s `feedback_for`/`vocab_proposals` read functions were fully built but had zero callers anywhere — no `GET` route exposes them. Deleted rather than left as unexercised surface area; noted where to re-add them if a review UI is ever built. | `api/db.py` |

Full test suite re-run after every fix: **95 passed**, no regressions.

### Carry-forward caveats

- Confidence still doesn't model read/write path coverage (phase 3's `visit-missing` gap stands).
- `POST /runs` is fully serialized via `run_lock` — a deliberate, documented trade-off for a local
  single-user tool, not a hidden limitation.
- `describe.md`'s behaviour-first prompt change (phase 3) is still untested against the phase-2
  baseline — the cache invalidation means the old vectors no longer exist to diff against.

---

## Phase 5 — Frontend ✅ (2026-08-19)

```bash
cd backend && uv venv .venv --python 3.12 && uv pip install -e ".[dev]"  # if not already done
cd frontend && npm install
make api   # terminal 1
make ui    # terminal 2 — http://localhost:5173
```

### What shipped

| File | Does |
|---|---|
| `frontend/src/types.ts` | Every TS type mirrored exactly off the backend's JSON — hand-written first, before any component, so nine parallel agents could build against one fixed contract |
| `frontend/src/api.ts` | REST calls plus `streamRun()` — SSE parsed by hand off `fetch`'s `ReadableStream`, buffering across chunk boundaries on `\n\n` |
| `frontend/src/hooks/useRunStream.ts` | Reduces the 5-type SSE contract into render state; classifies `progress` frames into run_started/node_started/lane_decided/node_finished by shape (the API collapses all of these into one `type`) |
| `frontend/src/components/*` (14 files) | Independent, controlled, presentational — no component calls `fetch` directly |
| `frontend/src/pages/Home.tsx` | The 3-column dashboard: thread state, streaming state, vote/correction/ticket-modal state |
| `frontend/src/pages/RunView.tsx` | `/runs/:id` — a run rendered read-only from `GET /runs/{id}` |

### Shown: a completed run with the Evidence tab open

Question: *"A visit isn't appearing on the owner page — where should I look?"* — lane `bug`,
confidence `high`, cost `$0.0606`, `indexed at 88e37c15cf6f, 2026-08-18`. The answer renders as
markdown with inline `code` spans and file citations. The right panel's **Evidence** tab is open,
grouped by kind (Classes: `Visit`, `OwnerController`; Interfaces: `OwnerRepository`; Methods:
`showOwner`, `findById`, `getVisits`, `addVisit`, `findOwner`...), with the `showOwner` row
expanded to show its actual source — a line-numbered, indigo-bordered code block running from
line 169, the `@GetMapping("/owners/{ownerId}")` annotation and method signature visible exactly
as it appears in `OwnerController.java`. Left sidebar shows the thread list (lane-pilled,
timestamped) with a "Try asking" suggestion-chip section beneath it, visible whenever the active
thread is empty.

### Design decisions worth remembering

- **Nine components, one contract, built in parallel.** `types.ts` and `api.ts` were written by
  hand first — the linchpin. Every component's exact prop interface was then *dictated* (not left
  for the builder to invent) in a Workflow fan-out, along with a shared design-system spec (colors,
  spacing, the lane/confidence badge palette). All nine came back typechecking clean on the first
  pass and integrated into `Home.tsx`/`RunView.tsx` without a single prop-contract mismatch —
  the cost of writing the contract twice (once as TS, once as prose per component) bought a
  single-pass integration.
- **The progress rail's illustrative wording couldn't be built literally.** The spec's example —
  "Interpreting → Searching (12 hits) → Expanding graph (31 nodes) → Writing answer" — implies
  per-retrieval-substage SSE events the API's contract doesn't emit; `retrieve()`'s five internal
  stages (vocab/semantic/keyword/graph/rerank) happen *inside* one cartographer node and never
  reach the wire individually. The rail was built against the real 4-node sequence instead
  (Interpreting → Mapping the codebase → Checking history → Drafting brief), each stage showing
  its own real `detail` text once finished — truthful to what the system actually reports, rather
  than fabricating sub-stage counts the backend can't back up.
- **Document-style transcript, not chat bubbles.** Bridge is a dense product/engineering tool, not
  a messaging app — user turns are a plain label + text, assistant turns are a bordered card with
  a header/body/footer, and clicking one selects it as the run shown in the right-hand Evidence
  panel.
- **The Graph path tab has no dedicated backend endpoint.** "An indented tree of the traversal"
  is built entirely from data already on each evidence chunk (`graph_depth`, `graph_path`) rather
  than inventing a new live-traversal API call — honest within the existing contract rather than
  expanding backend scope for a frontend-only task.
- **Native `title` attributes for the evidence-history hover**, not a custom tooltip component —
  simplest thing that's unconditionally correct inside a scrollable panel, no z-index/overflow
  fights.

### Two bugs found via live browser testing (fixed before the first review)

- **The whole page scrolled instead of just the evidence/chat panes.** Grid/flex items default to
  `min-height: auto`, so a `overflow-y-auto` child can't actually constrain itself unless every
  ancestor in the chain also gets `min-h-0` — a classic, easy-to-miss CSS trap. Confirmed via
  `window.scrollY` before (281px, wrong) and after (0px, correct) the fix, applied to every
  grid/flex ancestor in `Home.tsx` and `RunView.tsx`.
- **The user's own question showed as `"..."` while streaming.** The composer clears its input
  immediately on submit (for a snappy feel), but the live transcript bubble read its label from
  that same, now-empty state. Fixed with a separate `pendingQuestion` state that persists for the
  stream's duration.

### Adversarial review (Workflow tool, 4 dimensions × verify pass, 19 agents)

Spec compliance, integration-layer correctness (the hand-written state/effects code, not
individually unit-tested beyond manual clicking), XSS/security, and accessibility. **15 candidate
findings, all 15 confirmed** (0 refuted) — the security-XSS dimension came back clean (react-markdown
never configured to render raw HTML, every user string sent as a JSON body, no `innerHTML`
anywhere). All 15 fixed:

| # | Fix | File(s) |
|---|---|---|
| 1 | The progress rail's live "detail" caption was unreachable — it required a stage to be simultaneously `active` *and* carrying a detail string, but the reducer never produces that combination (a stage only gets a real detail on the same transition that flips it to `done`). Now shows the most recently *completed* stage's detail instead. | `ProgressRail.tsx` |
| 2 | Suggestion chips rendered in the centre chat column; the spec puts them in the left column. Moved into `ThreadsSidebar`, rendered below the thread list. | `ThreadsSidebar.tsx`, `Home.tsx` |
| 3 | "Hubs marked" in the Graph path tab matched on the LLM-phrased free-text `name` field (case-insensitive) instead of the stable, machine-generated `node_id` both sides actually share. | `GraphPathTab.tsx` |
| 4 | The live error banner only showed when overall run status was literally `"error"` — but a budget-abort or a single node failing both terminate as `"aborted"`/`"done"`, never `"error"`, so a real mid-stream error frame could arrive and never render. Now keyed on `errorDetail` being present, independent of terminal status. | `Home.tsx` |
| 5 | Switching to a thread showed "Loading thread..." *and* the empty-thread suggestion chips at the same time, because `showSuggestions` didn't check `threadLoading`. | `Home.tsx` |
| 6 | **Stale-closure race**: the async `getRun()` after a stream's `done` event had no way to know the user had since started a new thread or a new question — it could apply a finished run's data to the wrong thread, or its `stream.reset()` could abort a newer in-flight stream. Fixed with an epoch ref bumped by every thread/run-context-changing action; the stale continuation checks it before applying anything. | `Home.tsx` |
| 7 | `selectThread` used `Promise.all` over each run's `getRun()` — one failed fetch discarded every other already-succeeded run in the thread. Switched to `Promise.allSettled`. | `Home.tsx` |
| 8 | If the post-done `getRun()` itself failed, nothing called `stream.reset()` — the live bubble would hang forever and the next question would silently discard the finished-but-unloaded answer. | `Home.tsx` |
| 9 | If the SSE connection closed without ever sending a terminal `done` frame (server crash, dropped connection), the hook silently stayed in `"streaming"` forever — composer disabled, no error shown. Now surfaces an explicit error if the stream ends without one. | `useRunStream.ts` |
| 10 | `ticketModalRunId` was never cleared on `newThread()`/`selectThread()` — closing over a run that no longer exists in `activeThreadRuns` once the thread changed. | `Home.tsx` |
| 11 | The clickable assistant-answer card was a `<div onClick>` with no `tabIndex`/`role`/keyboard handler — unreachable and unusable by keyboard. Added `role="button"`, `tabIndex`, and Enter/Space handling (kept as a `div`, not a real `<button>`, since `AnswerFooter`'s real buttons live inside it — a button root would be invalid nested-button HTML). | `MessageBubble.tsx` |
| 12 | `DraftTicketModal` closed correctly on Escape/overlay-click but never moved focus into itself and carried no dialog ARIA. Added focus-on-open (to the close button), `role="dialog"`, `aria-modal`, `aria-labelledby`. | `DraftTicketModal.tsx` |
| 13 | Evidence-row expand/collapse buttons omitted `aria-expanded`. | `EvidenceTab.tsx` |
| 14 | The spec asked for 👍/👎; the footer used unicode arrows. | `AnswerFooter.tsx` |

Full re-typecheck after every fix: `npx tsc -b` — clean, zero errors, no regressions.

### Carry-forward caveats

- No `GET` endpoint exists for a run's own feedback, so vote state resets on page reload — tracked
  only in the frontend's own session state, not persisted round-trip.
- `POST /runs` streaming is still fully serialized backend-side (phase 4's `run_lock`); the
  frontend has no special handling for a second question queued behind a slow one beyond
  disabling the composer while its own request streams.
- The Vite dev server's built-in SPA fallback makes `/runs/:id` work on a hard reload today;
  a production static-file deployment of the built frontend would need an explicit rewrite rule
  for that route, which hasn't been set up (there is no production build/deploy step in this phase).

---

## Phase 6 — demo robustness ✅ (2026-08-19)

```bash
cd backend && .venv/bin/python ../eval/run_smoke.py     # 12 questions, all at recall 100%
cd backend && .venv/bin/python ../eval/seed_threads.py  # 3 real threads for a cold dashboard
```

### Smoke suite: before / after

A fresh baseline run against the current (post-phase-3, credentialed) index surfaced three
questions below 100% recall that hadn't been seen before — the underlying index and prompts had
changed enough since phase 3's numbers that a re-baseline was worth doing rather than trusting the
old table.

| | before | after |
|---|---:|---:|
| mean file recall (10 original questions) | 80.0% | 100.0% |
| questions at 100% | 7/10 | 10/10 |
| `find-owner` | 50% | 100% |
| `species-list` | **0%** | 100% |
| `rename-owner-customer` | 50% | 100% |

Only **`species-list`** was root-caused and actually fixed by a code change — the other two moved
on LLM/retrieval sampling variance between runs, not a fix, and that distinction is worth keeping
straight rather than claiming credit for something that just got lucky twice:

- **`species-list` (0% → fixed).** The question ("Where does the list of animal species come
  from...") vocabulary-matched the `entity:...PetType` node exactly as intended — but that node
  has no generated description and hit neither semantic nor keyword search on its own, so its
  *prior* score (the pre-rerank heuristic used to build a bounded shortlist) ranked it **#51 out
  of 107** candidates, entirely below the 45-candidate cutoff `retrieve()` sends to the LLM
  reranker. It never got a judgment call at all — despite `retrieve.py`'s own stage-1 comment
  already claiming vocabulary seeds "enter as candidates outright." Root-caused by instrumenting
  the retrieval pipeline by hand (`eval/*` diagnostics, not shipped) and confirmed exact rank and
  cutoff before touching any code. **Fix:** after building the prior-score shortlist, any
  vocabulary-seeded candidate that didn't make the cutoff is added back regardless of its score —
  a curated, human-authored match should always reach the reranker's judgment, even if nothing
  else about it looks interesting to a heuristic. Re-verified individually (`run_smoke.py
  species-list` → 100%, high confidence) before the full suite re-run. `backend/bridge/retrieval/retrieve.py`.
- **`find-owner` (50% → 100%, not a fix).** Diagnosed the same way: `OwnerRepository.java` was
  already sitting comfortably inside the shortlist (rank 2 of 95, real semantic score) in the
  *baseline* run — the miss was the cartographer's LLM merge choosing not to cite it that
  particular time, not a retrieval gap. The final run cited it correctly; this is exactly the
  kind of LLM sampling variance already documented in phase 4 (the interpreter's intent
  classification), now observed one layer downstream too.
- **`rename-owner-customer` (50% → 100%, not a fix, and still fragile).** `screen:owners/ownerDetails`
  ranked **#80 of 89** in the baseline — no vocabulary seed hit it at all for this phrasing (the
  question never says "owner page"/"owner details", the vocab term's actual aliases), so it
  survived only via a thin graph-expansion edge. It happened to get cited this run. Nothing was
  changed for this one because the root cause isn't the same shortlist-cutoff bug — it's a
  genuine semantic-distance gap for this specific phrasing — and it sits at exactly the 0.5
  threshold, not below it. Flagged here rather than quietly left out: this citation is not
  reliable yet.

### Trap and unanswerable questions

- **Two independently-worded data-question traps** — the original `how-many-owners` ("How many
  owners signed up last month?") plus a reworded `trap-owners-registered` ("How many owners
  registered last month?"), added specifically because phase 4's live testing had already shown
  the interpreter's question/data_question boundary has real sampling variance. Both routed to
  `data_question` and were redirected rather than answered from code, in this run.
- **One deliberately unanswerable question** — "How does the system stop two owners
  double-booking the same vet at the same time?" petclinic's `Visit` is a bare date+description
  record with no scheduling or conflict-checking logic anywhere in the codebase; there is no
  mechanism to find. The run correctly abstained at `confidence: low` rather than inventing a
  locking/validation system that doesn't exist — design law 4 holding under an adversarial prompt,
  not just a friendly one. `run_smoke.py` now supports asserting on confidence
  (`expect_confidence`) the same way it already asserted on lane, so this is a real, repeatable
  check rather than a one-off manual read of the output.

### What shipped

| File | Does |
|---|---|
| `eval/smoke_questions.yaml` | +2 questions (trap reword, unanswerable) |
| `eval/run_smoke.py` | `expect_confidence` assertion + `confidence_ok`/mismatch reporting, mirroring the existing `expect_lane` machinery |
| `eval/seed_threads.py` | Seeds 3 real threads (one per non-data lane) through the orchestrator directly — no API server needed |
| `Makefile` | `make seed` |
| `README.md` | What Bridge is, design laws, 4-command quickstart, what's stubbed for v1, the smoke table above |

Full backend test suite re-run after the retrieval fix: **95 passed**, no regressions.

### Carry-forward caveats

- `rename-owner-customer`'s citation of `ownerDetails.html` is evidence-thin (see above) — it
  passed this run but the underlying rank is fragile; worth a real fix (likely a vocab.yaml
  alias addition for this phrasing, or a small `_prior_score` reweighting) rather than treating
  the current green run as proof it's solid.
- The unanswerable-question check only asserts `confidence: low`, not that the answer *contains*
  an explicit abstention statement — a run could theoretically score low confidence while still
  writing confident-sounding prose. Not observed in testing, but the check doesn't rule it out.

---

## Phase A — answers that are explanations, not inventories ✅ (2026-09-07)

```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # 147 passed
cd backend && .venv/bin/python ../eval/run_smoke.py         # 12 questions
```

### The problem

`compose_answer()` printed the cartographer's summary and then walked
`ImpactMap.KINDS`, rendering every entry as `**name** — why  \`citation\``. For a
`question` that *was* the answer: a categorised inventory of touched code with nothing
connecting the pieces. Nobody asked "list the classes involved".

Two structural causes underneath it:

1. **The question lane had no writer.** The cartographer is a JSON classifier by design;
   the architect only ran for `change_request`/`bug`. Nothing in the pipeline was
   responsible for prose.
2. **No agent had ever read the code.** Every prompt was built from the one-sentence
   descriptions — `_render_chunk()` in `cartographer.py`, `render_impact_map()` in
   `architect.py`. Descriptions are what make retrieval work, and they are far too thin
   to explain a flow: the order of the steps, what is validated, what is written, exist
   only in the source.

### What shipped

| Module | Does |
|---|---|
| `retrieval/code.py` | **new** — hydrates `Chunk.code`: vector table first, repository on disk second, `UNAVAILABLE` marker third. Plus the `render()` an agent sees |
| `agents/explainer.py` + `prompts/explainer.md` | **new** — the question lane's writer. Reads 8 chunks in full and writes a cited walkthrough |
| `agents/changelog.py` + `prompts/changelog_group.md` | **new** — `git log` → areas → one cheap call per area. No retrieval, no reranker, no impact map |
| `orchestrator/graph.py` | `wants_explainer`, `changelog_trigger`, `plan_nodes`; `NODE_SEQUENCE` gains `explainer` |
| `orchestrator/run.py` | explainer + changelog branches; `compose_answer` rewritten as header + writer's prose + citation index |
| `agents/architect.py` + `prompts/architect.md` | the brief now carries the source of its top 6 touch-points, and must ground every claim in it |
| `agents/state.py` | `narrative`, `params`, shared `MORE_SURE_PREFIX`, `citations_in()`; **`citation_path` bug fix** (below) |
| `index/ids.py` | `commit_citation()` — `repo@<commit>:path`, the changelog's citation shape |
| `api/main.py` | `from_sha`/`to_sha` on `POST /runs`, `narrative` on `GET /runs`, citations read from the answer |
| `tests/test_answer_quality.py` | **new** — 51 tests |

### The three answers

Full text of all three is in the phase's verification run; the shape is:

**(a) "What happens when an owner adds a new pet?"** — `question` · high · $0.1074 · 74.6s.
Five numbered steps, each ending in its citation: owner lookup → `PetValidator` requires
name/type/birth date → the duplicate-name and future-birth-date checks → the pet joins the
owner's list → save. Then where it lands (`pets` table) and what the person sees. Gaps
names four things it did not find, including the repository implementation and the
migrations. No "what would make me more sure" line, because confidence was `high`.

**One line in it is the whole argument for handing agents code.** The answer says the
success message "automatically disappears after three seconds". That is real —
`owners/ownerDetails.html:86` is a `setTimeout(..., 3000)` — and it is nowhere in any
description. Retrieval alone could never have produced it.

**(b) "We want to add a weight field to pets"** — `change_request` · medium · $0.0750 ·
57.9s. The brief names `@Table(name = "pets")` on the entity, the `inputField` fragment
pattern the form actually uses, and the `dl-horizontal` list on the owner page — all read
off the handed source. `schema.sql` is listed as **inspect**, explicitly because its source
was not handed over, and confidence being `medium` forced the closing line:
*"I need to see the database schema or migration files ... to verify exactly how the `pets`
table is structured."*

**(c) changelog over the last 10 commits** — `changelog` · $0.0031 · 8.6s, one node, no
retrieval. Eight areas: owner, petclinic, database schema, messages, service, system, vet,
build & tooling. **Run three times; identical area ordering every time.**

### Smoke suite — before / after

| | phase 6 | phase A |
|---|---:|---:|
| mean file recall | 100% | **95.8%** |
| questions at 100% | 12/12 | 11/12 |
| high confidence | — | 8/12 |
| cost / question | ~$0.045 (ph. 3) | **$0.0852** |
| total, 12 questions | — | $1.0229, 653.6s |

**The one miss is `visit-missing`, and it is not caused by this phase.** Recall is measured
against the *impact map*, and Phase A changes nothing that produces the impact map —
retrieval, the cartographer, and its prompt are all untouched. `VisitController` was in the
retrieved evidence at rerank 5.0/10 in the failing run; the merge chose not to cite it.
That exact question scored **50% in phase 3 too**, and scored 100% on a re-run during this
phase. It is the same LLM sampling variance phase 6 documented for `find-owner` — worth
fixing at the cartographer, not claimable here.

Cost roughly doubled, which is the deliberate trade: a walkthrough is a deep-tier call over
~10k tokens of real source instead of no call at all.

### The measurement bug this phase found

`find-owner` read as a 50% miss for `OwnerRepository.java` — but the map *had* cited it.
`ids.citation` emits `path:45` (not `path:45-45`) when a node's start and end line are the
same, and `citation_path` only stripped suffixes containing a dash. So a one-line node's
citation kept its line number glued to the "path", and every path comparison downstream
quietly failed: the historian looked up `OwnerRepository.java:45` and found no file, the
answer never flagged it volatile, and the smoke suite scored a correctly-cited file as a
miss. `OwnerRepository#findByLastNameStartingWith` is one line — two nodes in petclinic hit
this. Fixed with a regex that strips `:12-30` and `:45` alike, and pinned by a test.

Phase 6 read this same question's swing as pure LLM variance. Half of it was a bug.

### Decisions worth remembering

- **Reading code is not optional, and the index cannot supply it.** Only nodes with a
  description get embedded, so only they carry stored source. Measured on petclinic:
  **190 of 320 nodes have no stored code — including all 17 endpoints and all 6
  entities.** "Where is a pet stored?" is exactly the question whose evidence the index
  cannot show. Hence the two-pass hydrator, with the repository on disk as the second
  pass. It is read-only, and it is the one path where secrets were never scrubbed at
  index time, so it redacts explicitly.
- **The changelog lane routes *before* analysis, and that is a documented exception to
  design law 3.** Every other lane routes off the impact map because retrieval is its
  evidence. A changelog's evidence is a commit range: there is no map to route from, and
  running retrieval first would spend a reranker call to discover the question was never
  about a place in the code. The decision is still a rule table in Python, never a model.
  The rule is narrow on purpose — an explicit `{from_sha,to_sha}` always wins, and
  otherwise BOTH a "what changed" phrase AND a period reference are required, so *"What
  changes when I add a weight field to pets?"* stays a change request. Pinned by five
  negative cases.
- **"Last week" is a period, not 20 commits.** The trigger parses a `git log --since`
  expression, so a question about a window is answered over that window. A sprint is taken
  as two weeks — an assumption, so the answer states the resolved window rather than the
  word it came from.
- **Grouping by directory is wrong for a repository root.** `module_of` returns the
  filename when a path has no parent, so the first working changelog led with `README.md`,
  `build.gradle`, and `docker-compose.yml` as three separate "product areas" — pushing
  `vet` out of the list entirely. Tooling is now one bucket that always sorts last, and
  `db/{h2,mysql,postgres}` collapse into one `database schema` area, because one schema
  change written three times is one product fact, not three areas.
- **A fabricated citation in prose is named, not removed.** A JSON node can drop a bad
  entry (the cartographer does, and `_clean_bullets` does the same for changelog bullets).
  Prose cannot be edited that way without rewriting a sentence, so the explainer appends an
  explicit **Unverified citation** bullet to its own gaps section instead. Silence was the
  only option ruled out.
- **The citation block indexes the answer, not the run.** It is built by parsing the body,
  so it lists what the answer actually cited — not everything retrieval happened to look
  at. `_citations_of` in the API now does the same, which is also the only thing that works
  for the changelog lane (no impact map, no retrieval chunks — the old code would have
  reported zero citations for an answer full of them).
- **Both writers are named in `NODE_SEQUENCE` and gated,** rather than dispatching to "the
  writer". The persisted trace then says which one actually ran.
- **Only the time ceiling was raised** (90s → 150s). The widest question measured 98.6s and
  would have aborted at the architect boundary, throwing away a brief it had the money to
  write. Cost and input tokens were re-measured at the same time and left alone: the most
  expensive run was $0.159 of $0.40, the largest 11.5k of 60k input tokens.

### Carry-forward caveats

- **There is no Detective.** The architect writes for both `change_request` and `bug`; its
  prompt has a bug branch ("likely fault area ... as a hypothesis"). Settled deliberately
  with the user rather than overlooked. A bug answer is therefore a brief, not the
  fault-hypothesis walkthrough a dedicated node would write.
- **`visit-missing` recall is unfixed** (above). The cartographer's merge is the place to
  fix it, and phase 6's `rename-owner-customer` fragility note still stands.
- **The explainer's "where it ends up" section carries no citation** in answer (a) — the
  prompt only requires one per numbered step. The three-seconds detail is real and was read
  from the evidence, but a reader cannot check it from the answer alone.
- **The changelog's evidence is not in the evidence panel.** `GET /runs` serves
  `evidence` from retrieval chunks, and a changelog run has none; its commits live in
  `env.changelog.report`. The frontend renders the lane and the answer correctly but has no
  per-commit view.
- **Cost per question roughly doubled** ($0.045 → $0.085). Deliberate, but it is the number
  to watch if the question lane ever runs at volume.

### Appendix — the three answers, verbatim

Run against `spring-petclinic @ 88e37c15` with the code as shipped.

<details>
<summary><strong>(a) What happens when an owner adds a new pet? — question lane, explainer</strong></summary>

```
Q: What happens when an owner adds a new pet?   params={}
lane=question  status=done  cost=$0.1074  input_tokens=10708  elapsed=74.58s  abort=None
nodes: ['interpreter:ok', 'cartographer:ok', 'historian:ok', 'explainer:ok']

**question** · confidence: high · spring-petclinic@88e37c15cf6f, indexed 2026-08-18 · $0.1074

**Where it starts**
The person fills out and submits the "New Pet" form.

**What happens, step by step**
1. The system extracts the owner's ID from the web address and retrieves their profile, throwing an error if the owner does not exist.
`spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetController.java:67-73`

2. It verifies that a name, species type, and birth date were all provided for the new pet.
`spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java:36-54`

3. The system ensures the pet's name isn't already used by this owner and checks that the birth date is not set in the future. 
`spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetController.java:107-137`

4. The new pet is added to the owner's internal list of pets.
`spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/Owner.java:97-101`

5. The system saves the updated owner record (which includes the new pet) to the database and prepares a success message.
`spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetController.java:107-137`

**Where it ends up**
The new pet is stored as a record in the `pets` table and linked to the owner in the `owners` table. The person is redirected to the Owner Information page, where they see their updated list of pets and a "New Pet has been Added" success message that automatically disappears after three seconds.

**Gaps — what I did not find**
* Security layers, authentication, or permission checks verifying who is allowed to add a pet to this owner's profile.
* The exact code that ties the validation rules (step 2) to the form submission process; it is implied to run automatically, but the explicit wiring isn't shown.
* The database migrations or schemas that define the structure of the database tables.
* The underlying repository implementation that executes the actual SQL queries to find and save the data.

---
**Citations**
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetController.java:67-73`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java:36-54`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetController.java:107-137`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/Owner.java:97-101`
```

</details>

<details>
<summary><strong>(b) We want to add a weight field to pets — change_request lane, architect</strong></summary>

```
Q: We want to add a weight field to pets   params={}
lane=change_request  status=done  cost=$0.0750  input_tokens=9406  elapsed=57.94s  abort=None
nodes: ['interpreter:ok', 'cartographer:ok', 'historian:ok', 'architect:ok']

**change_request** · confidence: medium · spring-petclinic@88e37c15cf6f, indexed 2026-08-18 · $0.0750

**Current behaviour**
The system currently tracks a pet's name, birth date, and type, as seen in `Pet.java`. When adding or editing a pet, the system validates that all three of these fields are present (`PetValidator.java`). These details are captured in a form (`createOrUpdatePetForm.html`) and displayed on the owner's profile (`ownerDetails.html`).

**Desired behaviour**
The system should allow users to enter, save, and view a pet's 'weight'. The `Pet` entity will store this value, the forms will capture it, and the validator will ensure it contains valid data (e.g., a positive number).

**Files to change or inspect**
* `src/main/java/org/springframework/samples/petclinic/owner/Pet.java`
  spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/Pet.java:44-85
  Add a `weight` private attribute (with a corresponding data type like `Double` or `Integer`) along with its getter and setter methods.
* `src/main/resources/templates/pets/createOrUpdatePetForm.html`
  spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/resources/templates/pets/createOrUpdatePetForm.html:1-34
  Add a new `inputField` fragment call for 'Weight' alongside the existing fields for Name, Birth Date, and Type.
* `src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java`
  spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java:36-54
  Update the `validate` method to apply any necessary business rules to the weight field, such as ensuring it is a positive number.
* `src/main/resources/templates/owners/ownerDetails.html`
  spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/resources/templates/owners/ownerDetails.html:1-96
  Add a new `<dt>` and `<dd>` pair in the `dl-horizontal` list under the `Pets and Visits` section to display the pet's weight.
* `schema.sql` or equivalent database migration files (inspect)
  I would look here to add a new `weight` column to the `pets` database table, as the `Pet` entity is mapped to `@Table(name = "pets")`. 

**Risks**
Because `Pet` is an `@Entity`, modifying it without simultaneously updating the underlying SQL schema will cause database runtime errors when the application tries to select, insert, or update a pet. 

**Test plan**
* Unit test: Validate `PetValidator` by passing a `Pet` object with negative weights, missing weights, and valid weights to ensure errors are appropriately rejected or ignored.
* Integration/UI test: Submit the `createOrUpdatePetForm` with a valid weight, save it, and verify that the weight correctly renders on the `ownerDetails` page.

**Size**
M

**Story split**
This is genuinely one piece. The entity update, schema migration, validation, and UI adjustments must ship together for the new field to function end-to-end.

What would make me more sure: I need to see the database schema or migration files (e.g., `schema.sql`) to verify exactly how the `pets` table is structured and where to add the new column.

---
**Citations**
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/Pet.java:44-85`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/resources/templates/pets/createOrUpdatePetForm.html:1-34`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java:36-54`
- `spring-petclinic@88e37c15cf6fc8490b01bc3e8e2c800cec1ac272:src/main/resources/templates/owners/ownerDetails.html:1-96`
```

</details>

<details>
<summary><strong>(c) What changed in the last 10 commits? — changelog lane</strong></summary>

```
Q: What changed in the last 10 commits?   params={}
lane=changelog  status=done  cost=$0.0031  input_tokens=5652  elapsed=8.6s  abort=None
nodes: ['changelog:ok']

**changelog** · evidence: git log · spring-petclinic@88e37c15cf6f, indexed 2026-08-18 · $0.0031

## What changed, per area

The last 10 commits, grouped by the part of the product they touched. Product areas come first, ordered by how much changed in them; build and tooling last.

### owner — owner profile, pet
- Owner searches now automatically normalize whitespace in the search terms. `spring-petclinic@bb37aad8:src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java`
- Pet names are now enforced to be unique per owner, preventing duplicate names when adding or updating pets. `spring-petclinic@e0db9b18:src/main/java/org/springframework/samples/petclinic/owner/PetController.java`

### petclinic
- Pet names are now enforced to be unique per owner `spring-petclinic@e0db9b18:src/test/java/org/springframework/samples/petclinic/PetClinicConcurrencyTests.java`.
- The application was upgraded to Spring Boot 4.1, including updates to native packaging hints and database integration tests `spring-petclinic@9a5d50cf:src/main/java/org/springframework/samples/petclinic/PetClinicRuntimeHints.java`.

### database schema
- Pet names are now enforced to be unique per owner across H2, MySQL, and PostgreSQL databases (`spring-petclinic@e0db9b18:src/main/resources/db/postgres/schema.sql`).
- A duplicate relation error for the unique pet name constraint has been fixed in the PostgreSQL schema (`spring-petclinic@88e37c15:src/main/resources/db/postgres/schema.sql`).

### messages
- The application now includes Japanese translation support. `spring-petclinic@0f6e8614:src/main/resources/messages/messages_ja.properties`
- The application now includes Hindi translation support. `spring-petclinic@f182358d:src/main/resources/messages/messages_hi.properties`

### service
- Pet names are now enforced to be unique per owner, with tests added to verify this rule `spring-petclinic@e0db9b18:src/test/java/org/springframework/samples/petclinic/service/ClinicServiceTests.java`.

### system
- Automated tests were added for the welcome controller and the pet validator's support functionality. `spring-petclinic@51045d16:src/test/java/org/springframework/samples/petclinic/system/WelcomeControllerTests.java`

### vet — vet directory
- An unused Vets object was removed from the veterinarian controller `spring-petclinic@1312314e:src/main/java/org/springframework/samples/petclinic/vet/VetController.java`.

### build & tooling
- The application was upgraded to Spring Boot 4.1. `spring-petclinic@9a5d50cf:build.gradle`

---
**Citations**
- `spring-petclinic@bb37aad8:src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java`
- `spring-petclinic@e0db9b18:src/main/java/org/springframework/samples/petclinic/owner/PetController.java`
- `spring-petclinic@e0db9b18:src/test/java/org/springframework/samples/petclinic/PetClinicConcurrencyTests.java`
- `spring-petclinic@9a5d50cf:src/main/java/org/springframework/samples/petclinic/PetClinicRuntimeHints.java`
- `spring-petclinic@e0db9b18:src/main/resources/db/postgres/schema.sql`
- `spring-petclinic@88e37c15:src/main/resources/db/postgres/schema.sql`
- `spring-petclinic@0f6e8614:src/main/resources/messages/messages_ja.properties`
- `spring-petclinic@f182358d:src/main/resources/messages/messages_hi.properties`
- `spring-petclinic@e0db9b18:src/test/java/org/springframework/samples/petclinic/service/ClinicServiceTests.java`
- `spring-petclinic@51045d16:src/test/java/org/springframework/samples/petclinic/system/WelcomeControllerTests.java`
- `spring-petclinic@1312314e:src/main/java/org/springframework/samples/petclinic/vet/VetController.java`
- `spring-petclinic@9a5d50cf:build.gradle`
```

</details>

---

## Phase B — confidence becomes deterministic ✅ (2026-09-08)

```bash
cd backend && .venv/bin/python -m bridge.index.build ../data/repos/spring-petclinic
cd backend && .venv/bin/python -m pytest tests/ -q          # 207 passed
cd backend && .venv/bin/python ../eval/run_smoke.py         # 12 questions
```

### The problem

The confidence level printed next to every answer came from the cartographer: the merge
prompt asked for `high | medium | low` and the run printed what came back, capped at the
retrieval level. A model grading its own work — and it graded generously. Measured over
the final smoke suite:

**the model claimed `high` on 4 of 12 runs that the rules score `medium`, and disagreed in
that direction every single time.** Not once did it under-claim.

### What shipped

| Module | Does |
|---|---|
| `index/io_roles.py` | **new** — `attrs.io_role` ∈ {read, write, both, none} plus `attrs.io_source`, the rule that decided it |
| `orchestrator/verify.py` | **new** — parse every citation; check the commit, the file, and that the range is inside a real node span |
| `orchestrator/confidence.py` | **new** — `score_confidence(lane, retrieval, impact_map, verified_citations)`; base from the rerank thresholds, then caps |
| `orchestrator/verifier.py` | **new** — the node that runs both, drops what failed, and writes the reasons back into the answer |
| `orchestrator/graph.py` | `verifier` appended to `NODE_SEQUENCE`, for every retrieval lane |
| `agents/cartographer.py` | stops setting the level; its own claim is kept as `model_confidence`, read by nothing |
| `agents/state.py` | `confidence_reasons`, `model_confidence`, `verification`, `append_more_sure()` |
| `index/build.py` | indexes annotation *arguments*, so `@Transactional(readOnly = true)` is distinguishable |
| `agents/llm.py` | a request timeout on the Gemini client (see the hang below) |
| `tests/test_confidence.py` | **new** — 59 tests |

### The rules

**Base** is the rerank score, straight from `config.RETRIEVE_*_CONFIDENCE_*`. Everything
after it is a cap or a demotion — **levels only ever go down**, which is what makes the
number safe to print. Pinned by `test_a_cap_can_only_lower_never_raise`.

- **any cartographer gap → cap `medium`**, for every lane. The gap text is also pushed into
  the answer's own "what would make me more sure" line, since a gap *is* the thing that
  would raise the level.
- **bug** → needs a read/display node **and** a persisting write node among the answer's
  verified citations. One side only caps `medium` and names the missing half.
- **change_request touching an entity** → needs the entity, a route handler, and a screen
  or endpoint — the last only *when the graph has any*, so a headless service is not
  punished for a layer it does not have.
- **>20% of citations unverifiable** → one level down, and the answer says so.

### io_role, and the two distinctions that matter

Derived at index time in precedence order: an endpoint's HTTP method, then the route a
method handles, then a Spring Data method name, then `@Transactional`, then accessor
naming. petclinic: **73 read, 49 write, 11 both, 187 none**.

- **`@Transactional` is not automatically a write.** Petclinic's only two are
  `@Transactional(readOnly = true)` on `VetRepository` finders. Classifying those as writes
  would put a false write-side tick on a read-only answer — so the indexer now stores
  annotation *arguments*, not just names, and `VetRepository#findAll` classifies `read`.
- **A setter is a write, but not a *persist*.** `Owner#addPet` mutates a list in memory;
  `setAllowedFields` configures a form binder. Neither is evidence that an answer examined
  the code that puts data in a database, which is the whole question the bug rule asks. So
  `io_source` is recorded alongside `io_role`, and only `http_method`, `handles_route`,
  `repository_method` and `transactional` count as persistence. In petclinic that is
  exactly **10 nodes: the 5 POST endpoints and their 5 handlers** — which is correct, and
  which a naive "io_role == write" test would have inflated to 49.

### Citation verification

Three things can be wrong with a citation, and the third is the one that matters:

| failure | example |
|---|---|
| the commit | a sha this index was not built at |
| the file | a path with no node in the graph |
| **the range** | `Pet.java:4000-4050` — a plausible range inside a real file |

A path-only check waves the third one through, which is exactly what a fabricated citation
looks like. Rangeless citations at another sha are **skipped, not failed** — that is the
changelog's `repo@<commit>:path` form, which points elsewhere on purpose.

An impact-map entry that fails is dropped as a row. Prose cannot be edited that way without
rewriting someone's sentence, so the citation *string* is replaced and the claim is left
standing, visibly unsupported:

```
BEFORE                                    AFTER
4. An audit record is written.            4. An audit record is written.
`...owner/Pet.java:4000-4050`             (citation removed — could not be verified)
5. The audit log is flushed.              5. The audit log is flushed.
`...owner/audit/AuditLog.java:1-40`       (citation removed — could not be verified)

                                          > 2 citations could not be verified and were removed.

checked 5  verified 3  dropped 2  ratio 40% (threshold 20%)
  ...Pet.java:4000-4050      the cited lines fall outside every indexed span in that file
  ...AuditLog.java:1-40      no such file in the graph at this commit
confidence: high -> medium
```

### Smoke suite

| | phase A | phase B |
|---|---:|---:|
| mean file recall | 95.8% | 87.5% |
| high confidence | 8/12 | **2/12** |
| cost / question | $0.0852 | $0.0733 |

**`visit-missing` = `medium`, and the write side is named** — the check this phase was
asked for:

```
**bug** · confidence: medium · spring-petclinic@88e37c15cf6f, indexed 2026-09-07 · $0.1051

_Why medium: a bug needs both sides of the flow and the answer cites no write/persist path._

What would make me more sure: the endpoint or repository call that saves this — the answer
only examined the code that displays it, so a fault in what gets written has not been ruled out.
```

`high` falling from 8/12 to 2/12 is the point, not a regression: every cap in the run was
audited and each names a real, material gap — the JPA repository layer that actually
persists (external to the repo, so unreachable), the schema migration for a new column, the
`messages*.properties` bundles a rename would have to touch. The model had called four of
those `high`.

**Recall fell from 95.8% to 87.5% on two questions, and the verifier caused neither.**
Verified by reading the persisted runs: `phone-validation` dropped nothing (3 checked, 3
verified) — its reranker scored the whole set weak (best 5.0/10) and `Owner.java#getTelephone`
ranked 6th at 2.0/10, so the cartographer never cited it. `visit-missing` likewise dropped
nothing (5 checked, 5 verified); its answer simply examined the display path only, which is
the same choice that earned it the `medium`. Worth noting what the low-confidence run *did*
say: `phone-validation`'s "what would make me more sure" named "the field-level declarations
for the Owner entity, which would contain validation annotations for the telephone property"
— which is precisely the file the eval expected. The run that failed recall is the run that
told you it had failed.

### Four bugs this phase surfaced

1. **A run that aborts on its time budget was printing an unverified level.** The verifier
   is the last node, so a budget abort skips it — and the header still said
   `confidence: high` for a run whose citations were never checked. Observed live on a 237s
   bug run against a 150s ceiling. The header now reads `confidence: high (unverified)` with
   a note saying why. A level nobody checked must not look like one that was.
2. **The verifier was scoring off the impact map when the answer cited nothing.** A real
   bug-lane run whose architect wrote bare file paths — `` `src/main/java/.../VisitController.java` ``,
   no sha, no range — scored `high` off a map the reader never sees, while the answer in
   front of them was uncheckable. The fallback is gone: the score is computed on what the
   *answer* cited, and an uncited answer is `low` with the format named. Both writers'
   prompts now say plainly that a bare path is not a citation.
3. **A provider 503 was leaking its JSON body into the answer.** When the merge failed, the
   raw `ServerError: 503 UNAVAILABLE. {'error': ...}` went into `gaps`, from there into the
   "what would make me more sure" line, and a product reader was shown a stack trace as
   though it were a fact about their codebase. The gap text is now human; the detail lives
   in `env.cartographer_fallback`.
4. **The Gemini client had no request timeout.** `genai.Client(api_key=...)` was constructed
   bare while the Anthropic one carried both `timeout` and `max_retries`. Every role in this
   project runs on Gemini and the run budget is only checked *between* nodes, so a call that
   never returns hangs the run past any ceiling — **a smoke suite sat on one question for 37
   minutes against a 150s budget.** Now `HttpOptions(timeout=120_000ms, retry_options=4)`;
   the same suite then completed 12 questions in under 9 minutes. Note the unit:
   `HttpOptions.timeout` is milliseconds, `LLM_TIMEOUT_SECONDS` is seconds.

### The expectation that does not hold, and why

`visit-missing`'s `expect_confidence: medium` was set as instructed, and it passed on the
final run — but it is **not stable**, and that should be said plainly rather than discovered
later. Run three times in a row it gave `high`, `high`, `medium`. The rule is working as
specified in each case; what varies is what the architect chooses to cite:

- when the answer cites `VisitController`'s POST handler, both sides *are* covered, the rule
  correctly does not fire, and the run is `high`;
- when it cites only `ownerDetails.html`, `Pet#getVisits` and `OwnerRepository#findById`,
  the write side is genuinely absent and the run is `medium`.

So the expectation encodes an outcome that depends on the writer, not on the rules. Two
honest fixes, neither taken here without a decision: accept `[medium, high]`, or make the
lane's *retrieval* deliberately reach for the write path so the answer can cover it every
time. The rule itself is pinned independently of this variance by
`test_the_bug_rule_fires_against_the_real_graph`.

### Carry-forward caveats

- **The bug rule's write side is thin in this repo.** petclinic's `save` is inherited from
  `JpaRepository`, which is external and therefore invisible to a source-only index, so the
  only persist-grade nodes are the 5 POST endpoints and their handlers. A repo with a
  service layer would give the rule much more to work with — and would exercise the
  `repository_method` and `transactional` sources, which petclinic barely touches (its only
  two `@Transactional` methods are read-only, and its 7 non-readOnly ones are all in tests).
- **`roles` unions over every node containing a citation's range.** A citation covering a
  whole class inherits that class's role — but a type's `io_source` is `members`, which is
  deliberately *not* a persist source, so citing a whole controller cannot buy a write-side
  tick. Worth re-checking if `IO_PERSIST_SOURCES` ever grows.
- **The change_request entity rule has not fired in anger.** `pet-weight` and
  `rename-owner-customer` were both capped by the gap rule first, so the three-layer check
  is pinned by tests but unexercised by the smoke suite.
- **A time-budget abort still skips verification entirely.** It is now labelled rather than
  hidden, but the honest fix is a verifier cheap enough to run inside any remaining budget —
  it calls no model, so there is no real reason it cannot.
- Phase A's carry-forwards stand: no Detective, and the changelog lane routes before analysis.

---

## Phase C — going back for the evidence the first search missed ✅ (2026-09-08)

```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # 235 passed
cd backend && .venv/bin/python ../eval/run_smoke.py         # 12 questions
```

### The problem

Phase B could tell you an answer was incomplete. It could not do anything about it.
`visit-missing` was the standing example: the rules correctly capped it at `medium` because
nothing in the answer touched the code that *saves* a visit — and then the run stopped,
having diagnosed its own blind spot and left it there.

One retrieval pass answers the question it was handed. It cannot know that a bug needs the
write path as well as the display path, or that the mapping step is about to admit it never
saw the persistence layer. Those are facts about the *evidence*, knowable only after the
cartographer has looked at it.

### What shipped

| Module | Does |
|---|---|
| `orchestrator/followup.py` | **new** — the three rules, the two ceilings, the merge, and the map rebuild |
| `orchestrator/graph.py` | `followup` inserted between `cartographer` and `historian` |
| `agents/cartographer.py` | split into `run` / `build_map`, so the map can be rebuilt over enlarged evidence; `emphasis` on the merge prompt |
| `agents/llm.py` | `Budget.fraction_remaining` / `tightest_ceiling` |
| `retrieval/retrieve.py` | `extra_terms` — keyword seeds a caller supplies directly |
| `orchestrator/confidence.py` | a skipped shot becomes a confidence reason |
| `tests/test_followup.py` | **new** — 28 tests |

**The rules**, in order, at most two searches per run:

a. the cartographer named a gap → search for what it said was missing;
b. a bug lane covering only one side → search toward the other, using the same read/write
   vocabulary `io_roles.py` classifies with;
c. a change to an entity with no schema or migration file in evidence → search for those paths.

**The ceilings.** At most `FOLLOWUP_MAX_SHOTS = 2`, and each shot only if the run still has
30% of its *tightest* ceiling left — the minimum of the four, not the average, because a run
with 90% of its money and 5% of its clock has 5% of a budget. A follow-up that pushes a run
into a budget abort has made the answer strictly worse: the writer never runs and the extra
chunks are read by nobody. A shot skipped for budget is recorded and surfaces in the
confidence reasons, so an answer that could have been better says so.

### Results

| | phase B | phase C |
|---|---:|---:|
| mean file recall | 87.5% | **100.0%** |
| questions at 100% | 10/12 | **12/12** |
| high confidence | 2/12 | 5/12 |
| cost / question | $0.0733 | $0.1071 |
| total, 12 questions | $0.8800 | $1.2846 |

**7 shots fired across the 12 runs, adding 30 chunks, with 6 map rebuilds.** Five runs
needed nothing and fired nothing.

Both of phase B's recall misses recovered. `phone-validation` went 0% → 100%: its first pass
scored weak (best 5.0/10) and never cited `Owner.java`, the cartographer said so in a gap,
and rule (a) went and got it. `visit-missing` went 50% → 100% **and recovered the write
side** — the thing this phase was asked to demonstrate:

```
  interpreter            ok    $ 0.0005    2.0s  bug
  cartographer           ok    $ 0.0285   19.9s  4 impacted, high
  followup               ok    $ 0.0332   23.6s  1 of 1 shot(s), 19 chunks
  followup:write_side    ok    $ 0.0017    1.6s  a bug needs both sides of the flow and nothing retrieved w…
  followup:remap         ok    $ 0.0307   21.0s  map rebuilt over 19 chunks
  historian              ok    $ 0.0000    0.0s  6 files, 0 volatile
  architect              ok    $ 0.0751   48.5s  3053 chars
  verifier               ok    $ 0.0000    0.0s  confidence high, 0 citation(s) dropped

shot write_side: +4 chunks, $0.0017, 1.6s   merged 19 chunks, rerank-2 ran
answer cites …owner/VisitController.java:97-112  →  persisting write ✓
confidence: high, 0 citations dropped
```

The search itself is the cheap part — **$0.0017 and 1.6s**. The map rebuild it forces costs
20x that, and is why cost per question rose 46%.

### Three things this phase had to get right, and got wrong first

1. **The trigger has to read the same evidence the rule judges.** Rule (b) first measured
   side-coverage over raw *retrieval*, and did not fire on `visit-missing` at all — because
   `processNewVisitForm` had been sitting in retrieval the whole time. The merge simply never
   cited it. So the trigger saw a write and declined to act while `confidence.py`, judging the
   answer, saw none. Both now ask the same question of the impact map.
2. **Fetching evidence is not the same as delivering it.** With the trigger fixed, the shot
   fired, found the POST handler, put it in the merged set at rank 4 — and the rebuilt map
   still left it out. The merge prompt has no idea a bug answer is judged on covering both
   sides; it maps the half the question was phrased around. The fix is an `emphasis` line,
   set in Python by whichever rule fired, sitting outside the `<retrieved>` block because it
   is our instruction and not repository content.
3. **Steering terms have to reach the keyword stage.** `"save update delete repository
   transaction"` went only to the embedding, where a long normalised question drowned it —
   `retrieve()` derives keyword seeds from code-*shaped* tokens, and those are plain words.
   `extra_terms` now passes them through directly, which is what rule (c)'s "keyword
   retrieval" asked for all along.

### The finding rule (c) is gated on

**petclinic has no indexed schema files at all.** The indexer walks `.java` and `.html`; its
`src/main/resources/db/{h2,mysql,postgres}/schema.sql` are not nodes — asserted directly by
`test_petclinic_really_has_no_indexed_schema_files`. So rule (c) checks the graph *before*
spending a shot and records "this index holds no schema or migration files at all" instead
of searching for something that cannot be found.

That is the correct behaviour for the rule, and it also names a real gap in the product:
**"the database schema migration" is the single most common thing the cartographer reports
it could not see**, and it is the most common reason a run is capped at `medium`. Indexing
`.sql` as file nodes would be a small change — no parsing needed, just discovery plus a
description — and would close the most frequent gap in the system. Not done here; it is
indexer work, not orchestration work.

### The expectation that moved, and why

`visit-missing`'s `expect_confidence` was set to `medium` in phase B, when the answer
reliably examined only the display path. It is now `[medium, high]`. This phase's whole
purpose was to recover the write side, and it does — so `high` became the *correct* outcome
and a bare `medium` would have started failing for the right reason. Both are accepted:
`high` when the follow-up closes the gap, `medium` when it does not. What must never happen
— `high` with only one side cited — is pinned by `test_confidence.py` directly, not by the
smoke file, so relaxing the expectation costs no coverage.

### Carry-forward caveats

- **`FOLLOWUP_MAX_SHOTS = 2` never binds today.** Rules (b) and (c) live on mutually
  exclusive lanes, so the real rule set can plan at most two shots. The cap is a guard
  against a future rule, and its test has to inject four synthetic shots to exercise it.
- **The time ceiling moved again, 150s → 240s.** A shot plus its forced map rebuild adds
  ~25s and a bug run measured 202s against the old ceiling. Runs now sit at 44-138s, so
  there is headroom — but this is the second phase in a row to raise it, and the trend is
  the thing to watch, not the number.
- **The map rebuild is the expensive part and is not itself budget-adaptive.** It runs at
  the deep tier over the whole merged set. A cheaper incremental merge — reconciling only
  the new chunks against the existing map — would cut most of this phase's cost increase.
- **A gap that the follow-up fails to close is still reported as a gap**, which is right,
  but the answer does not say "we went and looked". The trace knows; the reader does not.
- Phase A and B carry-forwards stand.

---

## Phase D — determinism ✅ (2026-09-08)

```bash
cd backend && .venv/bin/python -m pytest tests/ -q                    # 271 passed
cd backend && .venv/bin/python ../eval/check_determinism.py           # 3 cold runs x 3 questions
```

### The four things asked for, and what shipped

| Ask | Module | Does |
|---|---|---|
| Temperature 0 everywhere, exceptions documented | `config.py`, `agents/llm.py` | `DEFAULT_TEMPERATURE = 0.0` sent on every call; the one exemption (Anthropic `effort` → extended thinking, which forces the model's own temperature and rejects an override) is a named constant with its reasoning in `config.py`, not a silent branch |
| Stable sorts, `(score desc, chunk_id asc)` tie-break | `retrieval/retrieve.py`, `orchestrator/followup.py` | all four score-ordered chunk sorts now break ties on `node_id` |
| Interpreter 3-vote majority, disagreements logged | `agents/interpreter.py`, `orchestrator/run.py`, `orchestrator/store.py` | 3 independent votes on `intent`; a non-unanimous result is written to a new `eval_candidates` table, not just resolved and forgotten |
| `answer_cache`, cache-hit header, `fresh` bypass | `orchestrator/cache.py`, `orchestrator/store.py`, `orchestrator/run.py`, `api/main.py` | `(snapshot_key, question_norm) -> run_id`; a hit replays a previous answer at $0 with a `cached — see run …` marker in the header; `fresh=True` bypasses the read but still refreshes the entry |
| — | `tests/test_determinism.py` | **36 new tests** |
| — | `eval/check_determinism.py`, `Makefile` | the CI-facing artifact — see below |

### Temperature

`AnthropicProvider.generate` and `GeminiProvider.generate` both now send `temperature=0.0` by default, read from `spec.get("temperature", config.DEFAULT_TEMPERATURE)` so a role can still override it. The one exemption: a role whose spec sets `effort` on a model in `MODELS_SUPPORTING_EFFORT` triggers extended thinking, which forces the model's own reasoning temperature and rejects an explicit one — so the request omits `temperature` entirely rather than sending 0 and risking a 400. `ProviderResponse`/`LLMResponse` both carry `temperature_used` (`None` for the exempted case), so the trace can show, per call, whether determinism's first line of defense was even in effect.

### Stable sorts

Four score-ordered sorts (`retrieve.py`'s pre-rerank shortlist cut and post-rerank final cut; `followup.py`'s merge-pool cut and final merge) all sorted purely on `-chunk.score`. Ties are real — two chunks can score identically, most often before a reranker has run at all — and a tie's resolution was previously whatever order a dict happened to iterate in. All four now break ties on `chunk.node_id` ascending.

### Interpreter voting

`agents/interpreter.py` now takes `config.INTERPRETER_VOTES = 3` independent votes and keeps the majority `intent`. Unanimous votes cost nothing extra to handle (representative = the one vote); a split (2-1, or a genuine 3-way tie) is resolved — majority wins, a true tie falls back to the first vote deterministically — and **recorded**, both in `state.disagreements` (visible on the run's own JSON) and in the standalone `eval_candidates` table, because the point of collecting these is to mine them *across* runs later: which phrasings are still ambiguous at temperature 0, and whether a prompt rewrite moves the number.

This targets a caveat carried since phase 4: *"the interpreter's question/data_question boundary showed real sampling variance in live testing."* Voting turns that coin-flip into a rare, recorded event instead of a silent, invisible one.

### The answer cache

`orchestrator/cache.py` has the two pure functions: `question_norm()` (lowercase, collapse whitespace, drop trailing punctuation) and `snapshot_key()` (hashes the indexed sha, every agent prompt's actual text, and `ROLE_MODELS`/`ROLE_PROVIDER` — so editing a prompt, repointing a role to a different model, or rebuilding the index each invalidate every cached answer automatically, no version bump required). `RunStore` gained `answer_cache` and `eval_candidates` tables, `cache_lookup`/`cache_store`, and `save_raw` — writing a run row from an already-serialized payload dict rather than a live `RunState`, which is what a cache-served run needs (see below).

Deliberately **not** consulted or written for three cases, all documented in `config.py`:
- **`fresh=True`** skips the read; a fresh run's own completion still refreshes the entry — "recompute once," not "stop caching forever." (This distinction was the one real bug this phase's own tests caught — see below.)
- **a follow-up** (`state.prior_context` non-empty) — its correct answer can depend on thread history, which the 2-column key carries no trace of.
- **the changelog lane** — its evidence is `git log` against the *live* checkout, not the indexed sha, so a cached changelog answer can go stale the moment someone pushes, independent of anything `snapshot_key` tracks.

A hit (`orchestrator/run.py::_serve_cached`) patches a copy of the source run's persisted answer — header replaced with a `cached — see run …` marker built by re-running the real `answer_header()` on a minimal stand-in state, body and citations copied verbatim — onto a *new* row in the requesting thread, so `GET /threads/{id}` and `GET /runs/{new_id}` both work exactly as for a fresh run. The raw retrieval chunk dump (full source text) is deliberately **not** duplicated per hit — copying megabytes of code on every cache hit would defeat the point of caching being cheap — left pointing at the source run via `env.cache.source_run_id` instead.

**Real, live demonstration** (not the offline test — an actual run against the real index):

```
FIRST CALL — fresh=True
run_id=run_eec59e32182f438f  cost=$0.0000  status=done
**question** · confidence: low · spring-petclinic@88e37c15cf6f, indexed 2026-09-07 · $0.0000

SECOND CALL — same question, fresh=False (default)
run_id=run_b2cd8f48bc694811  cost=$0.0000  status=done
**question** · confidence: low · spring-petclinic@88e37c15cf6f, indexed 2026-09-07 · $0.0000 · cached — see run run_eec59e32, first computed 2026-09-07 20:38:51
```

(Both calls in this particular live demo landed after the project's Gemini quota was exhausted mid-session — see the caveat below — so the cached *content* itself is a degraded fallback answer. The mechanism doesn't care: it faithfully cached and replayed whatever the source run produced, unchanged except the header, exactly as designed.)

### The one real bug this phase's own tests caught

The first cut of `_maybe_cache`'s write gate reused the *read*-side `cacheable` flag verbatim — which includes `not fresh`. That meant **a `fresh=True` run never wrote to the cache at all**, contradicting the documented policy in the same commit ("a fresh run still refreshes the entry"). `test_a_repeated_question_hits_the_cache_and_says_so_in_the_header` failed immediately: asking the same question twice (first `fresh=True`, then default) found no cache entry on the second call, because the first had silently never written one. Fixed by splitting the flag: `cache_write_ok` (no follow-up, not changelog) is now computed separately from the read gate and never carries `fresh` — the function's own docstring says exactly why the two must not be the same variable.

### `eval/check_determinism.py` — the real result, reported honestly

The literal ask was "same question ×3 cold → identical citation sets," and the check does exactly that: 3 fully independent `fresh=True` runs per question, comparing the **set** of citations the answer actually made (not the prose — paraphrasing is not a determinism failure, a different fact being cited is). Run against the real index and the real model:

```
[FAIL] add-pet         5 citations, DIFFERED across 3 runs
[FAIL] pet-weight      6 citations, DIFFERED across 3 runs
[FAIL] visit-missing   3 citations, DIFFERED across 3 runs
```

**This needs the honest, complete story, not a quiet pass.** Root-caused by isolating each stage:

1. With the reranker **disabled** (`LLMClient(enabled=False)`), the pre-rerank shortlist — semantic search, keyword match, graph expansion, and `_prior_score`'s sort — is **bit-identical across 3 cold calls, node-for-node, in the same order**. None of retrieval's own deterministic machinery is the source.
2. With the reranker **enabled**, individual candidates' scores measurably drift between calls at temperature 0 — e.g. `Owner` scored `7.0 / 5.0 / 7.0` and `PetController#findPet` scored `8.0 / 6.0 / 6.0` across three otherwise-identical calls. That is squarely the residual variance `config.py`'s determinism docstring predicted *before this check was ever run*: "provider-side batching and token-routing are not perfectly associative... the same prompt can rarely still return a different answer." A ±2-point drift is enough to flip which candidates cross the top-`K` cutoff, which is exactly what changes the cited set.
3. **Determinism does hold when the reranker's judgment isn't close.** A cleanly-separated question (`"Where does the list of animal species come from..."`, best score 10.0/10, top-3 mean 9.0/10 in every prior smoke run) produced an identical 15-chunk set, in identical order, across 3 live cold calls — checked directly against the real reranker, not with it disabled.

So: the deterministic *plumbing* this phase built — temperature-0 request shaping, tie-broken sorts, the interpreter's vote arithmetic — is correct and is what `tests/test_determinism.py` pins (36 tests, all passing, several confirmed to actually bite via mutation). What is **not** solved, and was never claimed to be by the scope given, is the reranker's own single, unvoted judgment call on genuinely close evidence. The natural extension — the same majority-vote technique already applied to the interpreter — would fix it, at a cost this phase's scope didn't ask for: 3x every retrieval call in the system, not just the cheap interpreter node on every run. Flagged here rather than either hidden or silently implemented.

### Carry-forward caveats

- **The reranker is the system's one remaining unvoted judgment call**, per the finding above. Extending 3-vote (or averaged-score) treatment to it is the natural next phase if bit-for-bit citation stability is required on every question, not only on cleanly-separated ones.
- **`eval/check_determinism.py` is the CI-facing artifact, and there is no CI to wire it into.** Bridge has no `.github/workflows` (the repo isn't under git as of this phase). `make determinism` is the hook a real pipeline would call.
- **A cache hit's evidence panel is thinner than a fresh run's.** `retrieval` (the per-chunk dump with full source text) is not duplicated into a cache-served row, by design (see above) — its own `evidence` field is empty; the full dump lives at `env.cache.source_run_id`. The frontend has no "jump to source run" affordance for this yet.
- **The Gemini project's monthly spend cap was hit mid-session**, after the determinism check above but before the live cache-hit demo — an external, real event, not a code defect; it degraded that demo's *content* (both calls fell through to fallback paths) without affecting the caching *mechanism*, which is what the demo was for.
- Phase A/B/C carry-forwards stand.

---

## Phase E — one persistent index, cheap snapshots ✅ (2026-09-08)

> **Naming.** This was asked for as "phase B" of the storage track; PROGRESS.md already has a
> Phase B (confidence became deterministic), so it lands here as **Phase E** to keep one
> readable history. Phase F below is the work-queue phase asked for immediately after.

```bash
docker compose up -d && cd backend && .venv/bin/alembic upgrade head   # or: BRIDGE_DATABASE_URL=embedded://
cd backend && .venv/bin/bridge index ../data/repos/spring-petclinic
cd backend && .venv/bin/python ../eval/check_incremental.py
cd backend && .venv/bin/python -m pytest tests/ -q                      # 379 passed
```

### The problem

An index was a directory: `data/index/<sha>/{graph.db, descriptions.sqlite, vectors/}`. Three
consequences: a second snapshot of the same repo cost a **full rebuild** even when 99% of files were
byte-identical; a getter cost the same model call as a POST handler; and nothing below a file had an
identity, so a method that moved was a different row and lost its description.

### The hash chain

Everything in the phase rests on what goes *into* each hash:

| hash | over | why |
|---|---|---|
| `blob_hash` | file bytes | same content anywhere = one blob |
| `code_hash` | kind + qualified name + normalised source | **the reuse key** — no blob, no path, no lines |
| `chunk_hash` | blob + kind + qualified name + start/end | immutable, and its lines are true for its blob |
| `desc_key` | `code_hash` + model + prompt version | a description belongs to code, not a location |
| `vec_key` | `desc_key` + embed model | an embedding belongs to a description |

`code_hash` is **one column beyond the spec's list**, and it is the whole reason the guarantees are
reachable. Move a method forty lines: `blob_hash` changes, `chunk_hash` changes, a new row appears —
but `code_hash` is untouched, so the description and the embedding are already there. Zero model
calls for a commit that only moved things.

### What shipped

| Module | Does |
|---|---|
| `docker-compose.yml` | `pgvector/pgvector:pg16`, named volume, healthcheck, port 5433 |
| `db/schema.py` | 13 content/snapshot tables + the 5 app tables migrated as-is |
| `db/keys.py` | the five hash functions and the normalisation rules |
| `db/engine.py` | DSN resolution: env > config > `embedded://` |
| `db/repo.py` | the only place that writes SQL against the index |
| `alembic/` | two revisions; `CREATE EXTENSION vector` + an HNSW cosine index |
| `index/policy.py` | allowlist/denylist/size/generated-header, every exclusion with a reason |
| `index/projects.py` | Maven, Gradle, npm, Go, Bazel, per-dir Python -> `projects` |
| `index/tiers.py` | T0 template / T1 file-summary / T2 full |
| `index/chunking.py` | sub-chunked long methods, synthesised type headers, template sections |
| `index/pgindex.py` | the indexer — one code path, full or incremental |
| `index/pgcli.py` | `bridge index` / `snapshots` / `verify` |

### (a) Full index — parity with the SQLite build, stated exactly

```
INDEX  spring-petclinic@88e37c15cf6f  (full)
  files       61 indexed of 131 tracked
  NEW THIS RUN
    blobs        61        chunks       271       descriptions 271
    vectors      271       edges        651
  TIERS
    T0     58  21.4%       T1      0   0.0%       T2    213  78.6%
  EXCLUDED  (70 files)   70  not a language Bridge parses
  12.26s
```

**271 chunks vs the SQLite build's 320 nodes; 651 edges vs 724.** The entire difference is one
modelling decision — a *file* is no longer a chunk, it is a blob plus a `snapshot_files` row:

- `320 − 49 file nodes = 271` ✓
- `724 − 49 file→class contains − 24 file→type imports = 651` ✓ (both had a file as an endpoint)

Every other kind matches exactly: 17 endpoints, 6 entities, 187 methods, 12 screens, 49 type headers
(46 classes + 3 interfaces), and `calls` 382 / `contains` 187 / `renders` 16 / `includes` 19 /
`handles_route` 17 / `associates` 8 / `reads_entity` 8 / `maps_to` 6 / `extends` 8. **Zero edges
dropped.**

### (b)–(e) Incremental, on a throwaway clone with real commits

```
baseline (full index)     blobs=61   chunks=271  describe=271  vectors=271  edges=651
(b) 3 files edited        blobs=3    chunks=17   describe=6    vectors=6    edges=37
      -> 6 of 271 descriptions (2.2% of a full index)
(c) file renamed          blobs=0    chunks=0    describe=0    vectors=0    edges=0
(d) lockfile added        blobs=0    chunks=0    describe=0    vectors=0    edges=0
(e) re-hashed every blob  checked=61 drifted=0 missing=0 orphans=0
    after corrupting one file        drifted=1
```

(b) is 6 and not 3 because adding a method also changes its **type header** chunk — the synthesised
header carries the class's method index, so it correctly re-describes. That is the tiering and
chunking model agreeing with itself.

### Two things the verification caught about itself

1. **(b) initially proved nothing.** The first version inserted a *comment* just inside the class
   brace and reported `describe=0`. That was the system working perfectly — no `code_hash` changed,
   so nothing needed re-describing — but the check would have passed identically had description
   been broken to never fire. Rewritten to add a real method, so the assertion is now two-sided:
   `> 0` (the new code was described) **and** `<< full index` (nothing else was).
2. **(e) needed a negative control.** "Zero drift" is also what a `verify` that checks nothing
   reports. It now corrupts a file afterwards and asserts exactly one drift is caught.

### Decisions worth remembering

- **`rowcount` lies.** With `ON CONFLICT DO NOTHING`, psycopg reports `-1` for a multi-row insert,
  and the first full index proudly reported `-1 new chunks`. Every count now comes from `RETURNING`,
  because every verification in the phase asserts on these numbers.
- **Tier is decided before the row exists.** `tier` lives on an immutable chunk row and one of its
  inputs is semantic in-degree, so edges are extracted and degrees computed **in memory** before any
  chunk is written. Updating the tier afterwards would be exactly the mutation this model exists to
  avoid.
- **Endpoints and entities needed chunks of their own.** They are synthesised by the Spring
  extractors rather than parsed, so nothing produced a chunk for them — and every edge into the
  Spring layer was silently dropped. Measured: exactly 74, being every `handles_route`, `maps_to`,
  `reads_entity`, `associates`, `renders` and `includes`. The layer product questions are *about*.
- **`references` is a reserved SQL keyword.** SQLAlchemy quotes identifiers so every generated query
  is fine; raw SQL is not. The only raw SQL in the phase is in test fixtures, and it now quotes.
- **The exclusion counts are load-bearing.** An excluded file and a file the parser crashed on both
  look like absence. The reason counts are the only thing that tells them apart.

### Carry-forward caveats

- **Retrieval still reads SQLite.** `db/repo.py` has the snapshot-scoped `chunks_in_snapshot` and
  `semantic_search` (both tested, including the pgvector cosine join), but `Retriever` is not yet
  wired to them — the SQLite path remains primary for answering, exactly as agreed. That wiring is
  the next slice, and it is what makes phase F's lazy description reachable from a question.
- **T1 never fires on petclinic.** The "every member of this type is trivial" rule lives in the
  SQLite `apply_to_graph` pass and was not ported to the Postgres path; petclinic also has no type
  that qualifies. T0/T2 are real and measured; T1 is implemented and unexercised.
- **Descriptions this session are all `template`-sourced.** The Gemini project is at its monthly
  spend cap (verified live: `429 RESOURCE_EXHAUSTED`) and there is no Anthropic key, so **zero model
  calls were possible**. The counts above are *describe work items* — `desc_key`s that missed cache
  and were scheduled — which is exactly what (b)/(c)/(d) assert on. Embeddings are local
  `sentence-transformers` and fully real. This is not description parity with the SQLite build and
  is not claimed as such.
- **Postgres here is `pgserver`** (PG 16.2 + pgvector 0.6.2, pip-installed into the venv) because
  this machine has no Docker. `docker-compose.yml` is the deployment path; the DSN is the only
  difference and both go through the same engine.

---

## Phase F — the index keeps itself current ✅ (2026-09-08)

```bash
cd backend && .venv/bin/bridge simulate-push spring-petclinic --count 5
cd backend && .venv/bin/bridge worker --partition 0/4 --once
cd backend && .venv/bin/python ../eval/check_workers.py
```

### The problem

Phase E made indexing cheap. It was still something a person ran. Phase F makes it something that
*happens*: a push arrives, a job is enqueued, a worker picks it up — and someone can ask how far
behind the index is without reading logs.

### Why the queue is in Postgres

Not a feature comparison with Redis or SQS. **Enqueueing work and recording its result are the same
transaction as the data change that caused them**, so there is no state in which the queue believes
something the index does not. Three mechanisms carry it:

- `SELECT ... FOR UPDATE SKIP LOCKED` — the claim. N workers run the identical query and each gets a
  different row, no coordinator, no leases to expire. A worker that dies releases its lock when its
  connection drops.
- `pg_try_advisory_lock` — the coalescing's second half: one indexer per repo. Session-scoped, not
  transaction-scoped, because the work it guards spans many transactions. `try_` rather than
  blocking, so a worker that cannot get it hands the job back instead of sitting on a connection.
- `run_after` — the retry backoff *and* the schedule, in one column. A failed job is not deleted and
  re-enqueued; it stays put with its attempt count and last error, because a job on its fourth
  attempt is a different problem from four jobs that failed once.

### What shipped

| Module | Does |
|---|---|
| `db/schema.py` | `jobs(id, type, payload, status, attempts, run_after, repo, …)` + a second Alembic revision |
| `work/queue.py` | enqueue/claim/complete/fail/release, coalescing, `RepoLock`, `reap_stale` |
| `work/worker.py` | the five handlers, partitioning, backoff, cron tick |
| `work/limiter.py` | shared per-provider token bucket, 429-aware |
| `work/webhooks.py` | `POST /webhooks/github` (HMAC), `POST /webhooks/deploy`, `GET /health/index` |
| `work/health.py` | lag, coverage, staleness, queue depth, parse-failure rate |
| `work/simulate.py` | `bridge simulate-push` |

### The four claims, verified

```
(1) five rapid pushes -> at most two index passes
  after 5 pushes            queued=1
  worker drained the queue  jobs_run=1  snapshots=1
      -> 5 pushes became 1 index pass
  repo lock is exclusive    first=True second=False

(2) /health/index
  lag: {commits: 0, minutes: 0.2}   stale: false
  tiers: {T0: 58, T2: 213}
  description_coverage: {T0: 58/58 (100%), T2: 213/213 (100%)}
  parse_failure_rate: 0.0
  last_run: {mode: full, duration_s: 12.7, descriptions_written: 271, cost_usd: 0.0}
  queue: {queued: 0, running: 0, failed: 0, done: 1}

(3) describe_backfill picks the expensive gaps first
  rank degree  chunk
  1        24  ...owner.OwnerRepository#findById(Integer)
  2        22  ...model.NamedEntity#setName(String)
  3        19  ...model.NamedEntity#getName()
  4        17  ...owner.Owner#getPet(String)

(4) four workers partition the queue with no overlap
  partition 0/4 .. 3/4 claimed 10 job(s) each
      -> 40 jobs claimed exactly once, none shared
```

The live API too: a signed push returns `{ok, job_id, coalesced: true}`, a forged signature returns
**401**, and `POST /webhooks/deploy` sets the environment pointer and coalesces into the same job.

### Decisions worth remembering

- **Coalescing overwrites the payload rather than adding a row.** Five pushes leave one job pointing
  at the *newest* sha. Intermediate shas become snapshots lazily and only when an environment points
  at one — which is why `POST /webhooks/deploy` enqueues: "what is in staging?" is a question about a
  specific commit, so pointing an environment at it is the signal that it now matters.
- **Coalescing deliberately does not absorb into a `running` job.** That worker has already read its
  payload and will never look again, so the new sha would be lost. Pinned by a test.
- **A locked repo is not a failure.** The job is `release`d with a short delay and the attempt count
  untouched — somebody else is simply already indexing it.
- **A 429 throttles the shared bucket, not just the caller.** Eight workers each retrying
  independently against one account is how a rate limit becomes an outage. Recovery is gradual
  (`×1.25`), because snapping back to full speed after one good response is how a system oscillates.
- **`gc_weekly` reports and does not delete.** An unreferenced blob is exactly what makes an *old*
  sha answerable; deleting it because today's snapshots do not point at it throws away the history
  that makes the index worth keeping.
- **An unsigned webhook is refused, not accepted.** With no secret configured the endpoint returns
  503 for everything rather than trusting every caller — the safe direction for a misconfiguration.

### Carry-forward caveats

- **Lazy description (item 4) is not wired.** The pieces exist — `undescribed_chunks`,
  `backfill_candidates`, `LAZY_DESCRIBE_MAX` — and `describe_backfill` walks them in priority order,
  but "retrieval hitting an undescribed T2 chunk describes it inline and enqueues its neighbours"
  needs the phase E retrieval cutover first. It is the one numbered item of the two phases that is
  not done.
- **Backfill priority is degree-only in practice.** The weights for recency and project traffic are
  in config and the ordering is `degree × recency × traffic` by design, but with no request-traffic
  table yet both extra factors are 1.0. The order shown above is real; it is one of three intended
  signals.
- **Descriptions are still template-sourced** for the same spend-cap reason as phase E, so
  `description_coverage` reads 100% for a corpus nothing has actually described with a model. The
  number is honest about what it measures — a `desc_key` exists — and would drop the moment a real
  model is configured, because `desc_key` includes the model.
- **The limiter is in-process.** Cross-host coordination belongs in Postgres next to the queue; today
  `bridge worker` runs on one host, and an in-process bucket that works beats a distributed one that
  is mostly untested.
- **Nightly verify uses `croniter` when installed** and falls back to an hour/day check when it is
  not, because a scheduled verify should never be the reason a worker refuses to start.

---

## Phase G — retention, partitioned vectors, and a prompt bump that costs nothing ✅ (2026-09-08)

Three things that only matter once the index is permanent: deciding what is worth keeping, giving
the vector table a shape that can be routed to, and making a prompt-version change something other
than a bill.

`.venv/bin/python ../eval/check_retention.py` — six claims, all holding:

```
PHASE G — retention, partitioned vectors, lazy version migration
  vector store: {"engine": "pgvector", "extension_version": "0.6.2", "partitioned": true,
                 "quantised_type": null, "quantised_column": false, "rescored": false,
                 "note": "pgvector 0.6.2 has no halfvec; stage 1 runs on float32 and the
                          rescoring pass reorders nothing"}

(0) indexing four commits
  88e37c15cf6f  chunks+271  desc+271  vec+271  snapshot=25f73e95fc93
  b357022bfb83  chunks+2    desc+2    vec+2    snapshot=1ec91f21774b
  4acb2a222bd0  chunks+2    desc+2    vec+2    snapshot=32231fa7375a
  dcc7c7b1e327  chunks+0    desc+0    vec+0    snapshot=aa7adbdc5698

(1) retention policy
  keep  b357022bfb83  a git tag points at it
  keep  88e37c15cf6f  an environment points at it
  keep  dcc7c7b1e327  among the last N on the default branch
  drop  4acb2a222bd0

(2) gc dry run, then gc --apply
  dry run   would reclaim 73 row(s): {"blobs": 1, "chunks": 2, "descriptions": 2, "edges": 1,
                                      "references": 1, "snapshot_files": 63, "snapshots": 1,
                                      "vectors": 2, "module_summaries": 0}
  applied   reclaimed    73 row(s): (identical)
  retrieval dcc7c7b1e327  10 hit(s)  unchanged
  retrieval b357022bfb83  10 hit(s)  unchanged
  retrieval 88e37c15cf6f  10 hit(s)  unchanged

(3) partitioning
  partitions created: ['spring-petclinic:.']
  partition sizes:    {"vectors_default": 0, "vectors_p_spring_petclinic_2598ed90": 273}
  routed query scans: ['vectors_p_spring_petclinic_2598ed90']

(4) two-stage search agrees with the single-stage query
  single-stage top-5  [ClinicServiceTests.java:237, Pet.java:77, VisitController.java:90,
                       VisitController.java:97, VetController.java:65]
  two-stage    top-5  (identical)
  rescoring reordered 0 pair(s)  (rescored=False)

(5) a prompt-version bump costs nothing immediately
  descriptions before=273 after=273  migrated=0.0%
  stale code hashes:  273
  retrieval falls back for 20/20 sampled chunks
  backfill wrote 25 at model=template prompt_version=2
  migrated now 9.2%  (versions held: [('template','1',273), ('template','2',25)])

(6) shadow-index adopts nothing
  sampled=25 written=25 current-version rows 273 -> 273

all six hold.
```

### 1. Retention — `bridge/db/retention.py`, `bridge gc`

Four rules, and the GC reports which one saved each snapshot rather than just how many survived:

| rule | why it exists |
|---|---|
| an environment points at it | "what is in staging?" has to stay answerable |
| a git tag points at it | releases are what people ask about months later |
| among the last N on the default branch | recent history is the working set (`RETENTION_KEEP_LAST_N = 30`) |
| indexed within N days | otherwise nothing new is ever collected (`RETENTION_KEEP_DAYS = 30`) |

Phase F's `gc_weekly` counted and deleted nothing, and the caution was right with the wrong
mechanism: it asked *"is this blob in today's snapshot?"*, and a content-addressed row shared across
history will often not be. Phase G asks the question that actually matters — **can any retained
snapshot reach it?** — which is why the sweep above reclaimed exactly one blob (the fixture file
deleted in the last commit, reachable only from the dropped snapshot) and left the other 61 alone.

The dry run is not an estimate. It is the *same transaction* as the real sweep, rolled back instead
of committed, so `bridge gc` and `bridge gc --apply` cannot disagree about what would go — the first
one deleted it and then changed its mind. Verification (2) asserts the two reports are byte-identical
and that the database is unchanged afterwards.

`runs` and `feedback` are never collected. They hold **copied** evidence — the citations and impact
map are denormalised into the run's JSON — so a run stays auditable after the snapshot it was
computed against is gone. That is the whole reason the answer is copied rather than joined.

```
bridge gc  [DRY RUN — nothing was kept deleted]
  snapshots: 3 retained, 0 collected
    kept    3  because indexed within the retention window
    kept    1  because an environment points at it
    kept    1  because among the last N on the default branch

  table                    before  reclaimed
  blobs                        62          0
  chunks                      273          0
  descriptions                323          0
  edges                       671          0
  references                 1224          0
  snapshot_files              185          0
  snapshots                     3          0
  vectors                     273          0
```

### 2. The vector store — `bridge/db/vectorstore.py`

`VectorStore` is an ABC with five methods (`search`, `upsert`, `ensure_partition`,
`drop_empty_partitions`, `partitions`, `capabilities`). `PgVectorStore` implements it;
`ExternalVectorStore` is a stub that **raises with the specific thing that implementation would have
to solve**, because the hard part of moving vectors out of Postgres is not the ANN search — it is
that `search()` answers a question spanning four tables (`vectors`, `descriptions`, `chunks`,
`snapshot_files`) and an external engine sees only the first.

**Partitioning, not post-filtering.** `vectors` is now LIST-partitioned by project
(migration `9c1a4f7be20d`, hand-written because converting a table to partitioned is not an in-place
operation), with HNSW per partition and a `DEFAULT` partition so the table stays writable for
projects too small to earn one (`VECTOR_MIN_PARTITION_ROWS = 1000`). `EXPLAIN` on a routed query in
verification (3) scans one partition and never touches `vectors_default` — which is the whole point:
one global index plus `WHERE project IN (...)` is not the same query more cheaply, because the ANN
index returns *its* k nearest and the filter then discards most of them, so effective k collapses
and recall silently drops.

The cost, stated: Postgres requires the partition key in every unique constraint, so the primary key
is `(vec_key, project)`. `vec_key` is content-addressed and project-independent, so code that
genuinely appears in two projects now stores two rows. That is rare, and it buys pruning.

**Two-stage search, and an honest report of what stage 2 is doing.** Stage 1 over-fetches
`VECTOR_RESCORE_CANDIDATES = 300`; stage 2 rescores against the full float32 vectors and truncates
to k. The quantised column is created only when the server can store one — and it cannot here:

```
pgvector 0.6.2  types present: ['bit', 'vector']     halfvec/sparsevec: absent (added in 0.7.0)
                functions: binary_quantize absent, opclasses: no bit_hamming_ops
```

So `capabilities()` reports `quantised_type: null, rescored: false` and the note says stage 2
reorders nothing. Verification (4) asserts both halves: the two-stage result is identical to the
single-stage one, **and** zero pairs were reordered, which is what "no quantisation" must mean. On a
newer pgvector (the `docker-compose.yml` image) `ensure_layout()` adds a `halfvec(384)` column and
the same code path becomes a real quantised pass. That is built and **unverified on this machine**.

The 5M-vector latency benchmark was skipped by your decision. Measured cost of running it, for the
record: ~10 min insert, ~48 min HNSW build, ~18 GB (extrapolated from 50,000 vectors → 6.2 s insert,
28.6 s build, 177 MB).

### 3. Lazy version migration — `bridge/db/versions.py`, `bridge shadow-index`

A description is keyed by `(code_hash, model, prompt_version)`. Change any of the three and every
`desc_key` becomes a different key — correct, and also how a one-word prompt edit becomes a bill for
re-describing a monorepo. So:

- **The bump itself writes nothing.** Verification (5): 273 descriptions before, 273 after.
- **Retrieval prefers the newest version it has, per `code_hash`, and falls back.** Per chunk, not
  per repo — a half-migrated index is the normal state, and refusing to answer until the backfill
  finishes makes the bump the outage it was meant to avoid. 20/20 sampled chunks fell back.
- **`describe_backfill` closes the gap** and is now version-aware. It previously matched on model
  alone, which made a prompt bump *invisible* to it: every chunk looked described while the key
  retrieval would ask for did not exist. 25 written, coverage 0% → 9.2%.
- **`DESCRIPTION_VERSIONS_PREFERRED` is the roll-back lever.** A prompt that turned out worse is
  demoted by naming the old one, without deleting the rows the new one wrote.

`bridge shadow-index <repo> --projects a,b --prompt-version vN` describes a bounded sample (highest
in-degree first, because those are what retrieval surfaces) at a candidate version and prints old
against new. Nothing routes to those rows until `PROMPT_VERSION` names them — verification (6)
asserts the current-version row count is untouched.

```
shadow index  spring-petclinic@28153de3ebe6  projects=<all>
  current version   template prompt=1
  shadow version    template prompt=v2
  note: the index holds no descriptions at the configured model 'gemini-3.5-flash-lite';
        comparing against 'template', which is what it was actually indexed with
  sampled=3  already=0  scheduled=3  written=3  changed=0

  org.springframework.samples.petclinic.owner.OwnerRepository#findById(Integer)  (degree 24)
    before: Find by id on OwnerRepository.
    after : Find by id on OwnerRepository.
```

`/health/index` gained `vectors` (capabilities + partition sizes) and `descriptions` (per-version
counts, `migrated_pct`, preference order). Both are index-wide rather than per-repo, because the
vector store is partitioned by project — which spans repos — and the description version is global;
hanging either off one repo would be a lie.

### Tests

`tests/test_retention_vectors.py` — 27, against a real Postgres, because every subject is database
behaviour: LIST partitioning and its pruning, reachability over content-addressed rows, and a
transaction rolled back to make a dry run truthful.

```
406 passed in 265.27s (0:04:25)
```

### Decisions worth remembering

- **The GC deletes by reachability, never by age.** A blob two snapshots share survives one of them
  being collected; a description survives while any live chunk shares its `code_hash` (which is what
  makes a *moved* file free). Both are pinned by tests, because both are the failure mode where the
  index still looks intact.
- **Empty partitions are collected too.** A partition outlives its rows, and what is left is a
  relation with an HNSW index that every plan still pays to consider.
- **Capability detection over version assumptions.** `capabilities()` asks the server what types and
  columns exist rather than parsing an extension version string, so the same code takes the real
  quantised path on 0.7+ and the honest float32 path here, with no branch on "which deployment is
  this".
- **`coverage(target=...)` exists because the configured model and the attributed model differ.**
  With no reachable provider, descriptions are attributed to `template` — honestly. Measuring a
  backfill against a model nothing was ever written at reports 0% forever and looks like a stalled
  migration; it is a mislabelled question.

### Carry-forward caveats

- **The quantised path is built and unverified.** pgvector 0.6.2 (the embedded server) has no
  `halfvec`, so `rescored: false` is the truth here and stage 2 is a measured no-op. The genuine
  storage-and-speed win needs the docker-compose image.
- **Retrieval still reads SQLite.** `PgVectorStore.search()` is correct and tested, but `Retriever`
  has not been cut over, so the partitioned two-stage path is not yet what answers a question. That
  cutover is the same one Phase E and F are waiting on, and Phase H's routing needs it.
- **`module_summaries` collection is implemented but untriggered.** The fixpoint that propagates
  death up the merkle tree is there and reads 0 above because petclinic has no summaries yet.
- **T1 still never fires on petclinic**, and descriptions remain template-sourced, both unchanged
  from Phase E.

---

## Phase L — a quality gate that doesn't need a human to read a table ✅ (2026-09-08)

> **Naming:** Phases H–K are already reserved (routing/summaries, BM25+PPR, Spring interface
> validation, contract nodes — see the plan queued alongside Phase G). This request arrived as a
> separate initiative ahead of them, so it lands as **Phase L** to avoid the collision, the same way
> Phase E was renamed off a colliding "Phase B."

An independent judge, a full per-question report, a pass/fail gate against a stored baseline, a
20-question set that actually exercises multi-hop reasoning/traps/changelog/follow-ups, and a weekly
sample of real traffic — the five things standing between "the smoke suite still passes" and "someone
would notice if this got worse."

**Both LLM providers remain unable to complete a call, verified live again this phase**: Gemini —
`429 RESOURCE_EXHAUSTED`, "Your project has exceeded its monthly spending cap"; Anthropic — `400`,
"Your credit balance is too low to access the Anthropic API." This is the same standing situation as
Phase G, and the same policy applies: everything is built for real; what needs a live model is marked
**built, unverified live** rather than faked, and what doesn't need one is verified for real, including
running the actual 20-question suite and the actual gate CLI end to end.

### 1. `eval/rubric.py` — an independent judge

Scores `correctness` / `completeness` / `actionability`, 1–5 each, plus a list of claims the evidence
doesn't support. Two design decisions carry the actual guarantee:

- **The judge runs on Anthropic; every answer-writing role runs on Gemini** (`config.py`'s
  `ROLE_PROVIDER["judge"]`). A judge grading a model from its own family shares that family's blind
  spots — a hallucination that reads as plausible to the model that invented it tends to read as
  plausible to a same-family grader too. Cross-provider grading is cheap insurance against that.
- **Evidence is fetched independently, never trusted from the run.** For every citation the answer
  made, `git show <sha>:<path>` reads the actual historical blob — not the current working tree, which
  may have moved on — and slices the cited lines with `±2` lines of context. A citation that doesn't
  resolve (wrong commit, no such file, a range past the end of it) is never shown to the judge as
  evidence; it's listed up front as a citation that failed to check out, and the judge is told that
  counts as unsupported, not as evidence for anything.
- **The floor is enforced in code, not requested in the prompt** (design law 1). If the judge's own
  JSON lists any unsupported claim, `score_answer` overrides whatever `correctness` it reported down to
  `RUBRIC_UNSUPPORTED_FLOOR` (1) — a model that notices a fabrication and still reports a 4 doesn't get
  to keep the 4. A citation that failed to resolve is synthesised into `unsupported_claims` even when
  the judge's own prose doesn't mention it, for the same reason.

Verified without a live model: 20 tests (`tests/test_rubric.py`) against a real throwaway git repo for
span-fetching (resolved, out-of-range, wrong path, wrong commit, malformed citation, no repo on disk,
whole-file/changelog form) and a duck-typed fake judge for the scoring logic (floor enforcement,
clamping out-of-range scores, a missing dimension or non-JSON reply going *unscored* rather than
guessed, `available_for` gating). **Not verified live**: whether a real judge model's grading judgment
is any good — that needs an actual model call.

### 2. `eval/run_full.py` — the whole picture, per question and rolled up

File precision/recall, core-file recall, the rubric score, confidence, lane, cost, latency, and — for
standalone questions — determinism (run twice cold, citation-set compared, matching
`eval/check_determinism.py`'s own method exactly).

**A real bug this phase found and fixed before it could contaminate a report**: sharing one
`LLMClient` across many `run()` calls means its `.budget` (default `Budget()`, config's flat call/cost
ceiling) gets checked against `.usage` accumulated across the *entire suite*, not per run — a real,
LLM-enabled 20-question pass would eventually trip a budget that no single one of those runs came
close to. Every `run()` call in this file now gets its own fresh `LLMClient`, matching what
`run_smoke.py`/`check_determinism.py` already do implicitly by never sharing one.

**A second real behaviour this phase found, understood, and designed around, not silently
worked around**: the very first `run()` call in a fresh process pays for lazy model/cache loading
(~10s measured cold vs ~0.04s warm), and Phase C's follow-up shots only fire when the run's *own*
wall-clock budget still has >30% left — so a cold-start question can legitimately take a different
follow-up decision than a warm re-run of the identical question, which looked exactly like a
determinism failure and was not one. Fixed with an explicit, unscored warm-up run before anything is
timed. A related, separate issue was a bug in the harness itself: the determinism check reused the
primary run's own `thread_id` for *every* question's repeat, including non-threaded ones — turning the
"determinism" check into a real follow-up (with real prior context) being compared against a
standalone original. Fixed: a standalone question's repeat now runs in its own fresh thread, matching
the primary run; a threaded turn is honestly reported `checked: false` rather than compared against a
repeat that was never fair (a fair repeat of a follow-up means cloning its whole prior thread first,
which is out of scope here).

`--no-llm` runs `LLMClient(enabled=False)` — no network call, so none of a dead provider's retry/backoff
delay, and interpretation/cartography/rubric all take their documented no-model fallback
deterministically and instantly. Recall-against-retrieval, cost, latency, and pipeline determinism are
real in this mode; lane/confidence beyond the retrieval base, and the rubric, are not, and the report
says so rather than presenting a degraded run as a normal one.

### 3. `eval/sets/petclinic.yaml` — 20 questions

`eval/smoke_questions.yaml` (12 questions) is untouched and still what `run_smoke.py`/
`check_determinism.py` use as the fast CI hook. This is the fuller set `run_full.py` reads by default:
the 9 plain items + 3 traps carried over, plus 8 new —

- **2 multi-hop**: removing a vet (Vet + VetController + the listing template, none of it holds
  together from one file) and an owner "transferring" a pet (a trap wearing multi-hop clothes —
  petclinic has no such feature, and a single-file answer is a sign the model didn't check both Owner
  and Pet).
- **2 changelog**, phrase-triggered one of each way `graph.py::changelog_trigger` recognises a range —
  an explicit commit count and a named period — confirmed live against the real trigger before writing
  the fixture (`_COMMIT_COUNT` / `_since_expression`, both fire correctly).
- **2 threads, 4 turns**: a booking follow-up ("what stops them booking it in the past?" — honestly
  "nothing does," petclinic's `Visit` has no date validation at all) and a species follow-up ("could a
  PM add one without an engineer?" — needs the first turn's context, since `PetType` rows are
  seeded, not hardcoded).

### 4. `eval/gate.py` — pass/fail against a stored baseline

Fails on any ONE of: core-file recall dropped more than `GATE_CORE_RECALL_DROP_PTS` (5pts); rubric mean
dropped more than `GATE_RUBRIC_DROP` (0.3); any question stable in the baseline that isn't stable now.
`compare()` is pure — a dict in, a dict out — verified with 13 tests (`tests/test_gate.py`) against
synthetic reports covering every rule, its exact boundary (a drop *at* the threshold still passes; a
question the baseline never measured can't regress), and the two "don't fail for the wrong reason"
cases that matter most: a metric going from measured to *unmeasured* is a note, not a failure — it
proves nothing regressed, it just failed to check — and a question missing from the baseline entirely
(no prior row to compare against) is not penalised.

**End-to-end verification, real CLI, `--no-llm`**: `make gate-baseline` saved a real baseline off the
actual 20-question suite (below); `make gate` against itself passes; a synthetically regressed copy of
that same report (`core_recall_pct` moved down 10pts) fails with the exact stated reason, and the
unmodified report passes again — proving the full file-loading + comparison + exit-code path, not just
`compare()` in isolation.

**What could not be run live**: the phase's literal ask — deliberately worsen `explainer.md` on a
branch, watch the gate fail on a real degraded answer, revert, watch it pass — needs a live model to
produce a worse answer for the judge to catch, and no provider can complete a call right now. The
mechanism this would exercise (a real run_full.py report → gate.py → fail → revert → pass) is exactly
what the synthetic-regression run above already proved works; what's unverified is only whether a real
prompt regression actually produces a lower rubric score, which needs a model. The exact recipe is
ready to run the moment either provider recovers:

```
git checkout -b eval-demo
# edit backend/bridge/agents/prompts/explainer.md to something visibly worse
make gate-baseline   # if a baseline doesn't already exist
make gate            # expect: FAIL
git checkout main -- backend/bridge/agents/prompts/explainer.md
make gate            # expect: PASS
```

Baseline saved (`eval/eval_baseline.json`, `--no-llm`, real 20-question run):

```
mean file recall   : 69.2%
mean precision     : 25.9%
core-file recall   : 66.67%  (13 questions with core_files)
rubric mean        : n/a  (0/20 scored — no judge provider reachable)
determinism        : 16/16 stable
total cost         : $0.0000   total time: 2.0s
CONFIDENCE MISMATCH: ['visit-missing']
TOO FEW CITATIONS  : ['changelog-last-month']
CORE FILE MISSED   : ['pet-weight', 'rename-owner-customer', 'thread-species-1', 'species-list', 'book-visit']
```

Every one of these is the documented, expected shape of a `--no-llm` run (no cartographer to filter
raw retrieval, no changelog agent to write citations, no model to weigh the write side of the
visit-missing bug) — not a real quality signal. It is a correct, honest baseline for *this mode*, which
is exactly what `--no-llm` runs are for: proving the harness, not the model.

### 5. `eval/rolling_quality.py` — real traffic, sampled and trended

`config.QUALITY_SAMPLE_SIZE` (20) real `done` runs from the last 7 days, stratified by lane
(round-robin across lanes so no one lane crowds out the sample — simple over a weighted draw, because
the point is "every lane gets a fair look," not statistical precision), rubric-scored, written to a
new `quality_samples` table (`orchestrator/store.py`, `run_id` UNIQUE so a job re-run this week can't
double-count a run into its own mean) so the trend never needs to re-score anything. Every `down`
`feedback` row (`api/db.py`, same `runs.sqlite`) not yet mirrored is copied into `eval_candidates` —
idempotent, tracked by the feedback row's own id inside `candidates`.

Drift is `QUALITY_DRIFT_WEEKS` (2) *consecutive* declines on scored weeks only — a week with no judge
reachable is a gap in the data, not evidence quality fell to zero, and must not read as one. Verified
with 17 tests (`tests/test_rolling_quality.py`): stratification (spreads across lanes, a starved lane
doesn't block others, never over-returns, a null lane is its own bucket, deterministic), drift alerting
(too little history, a real 2-week decline, a mid-series recovery that must NOT alert, an unscored gap
week that must NOT count as a decline, flat scores), and the `RunStore` persistence itself (round-trip,
dedup on re-sample, unscored samples count toward `total` but not the mean, oldest-first trend
ordering) — and live, end to end, against this session's own real run history: `--no-llm --size 5`
against the actual `runs.sqlite` (168 real `done` runs from this phase's own testing) sampled 5 across
all 5 lanes present, correctly reported `0 scored / 5 unscored` (no judge reachable), and a second
invocation correctly saw `already_sampled=5` and drew 5 different runs. **Not verified live**: an
actual rubric score landing in the trend, or a real drift alert firing off real declining scores — both
need a model. The stated "project" half of the stratification is a documented no-op today: this phase
indexes one project at a time, so there is nothing to stratify by yet.

Both test-generated `quality_samples` rows and the mirrored `eval_candidates` row from this
verification were deleted from the real `data/runs.sqlite` afterward — this phase's own testing must
not become the first (fake) week of a quality trend meant to track real traffic.

### `pytest`

```
456 passed in 271.05s (0:04:31)
```

406 prior + 50 new: 13 `tests/test_gate.py`, 20 `tests/test_rubric.py`, 17 `tests/test_rolling_quality.py`.
`tests/conftest.py` is new too, adding `eval/` to `sys.path` for exactly these three files —
`eval/*.py` are standalone scripts, not an installed package, and this is the one place that makes
their pure logic importable from tests instead
of only ever exercised end to end.

### Decisions worth remembering

- **The floor is enforced in Python, never trusted from the prompt.** Same pattern as Phase B's
  citation verifier: a model can reason about whether a claim holds up; whether an unsupported claim
  caps the score is a rule, and rules are code (design law 1).
- **Cross-provider grading is deliberate, not a fallback.** `judge` is pinned to Anthropic while every
  answer-writing role is on Gemini — the opposite of the usual "flip if one provider is down"
  reasoning, because the point is that the two are unlikely to share a blind spot at once.
- **"Unmeasured" and "regressed" are different claims, and the gate must never conflate them.** A
  `--no-llm` run, or one with no judge reachable, cannot confirm the rubric held — it can only fail to
  check. That is a note, not a gate failure; treating it as a failure would make `make gate` fail on
  every `--no-llm` CI run for a reason that has nothing to do with quality.
- **A drift alert needs consecutive scored weeks, not consecutive weeks.** A week nobody could reach a
  judge is a hole in the data. Filling that hole with "0" and calling it a decline would manufacture
  exactly the kind of alert this feature exists to make trustworthy.

### Carry-forward caveats

- **No live model call succeeded anywhere in this phase.** The rubric's actual judgment quality, a real
  drift alert firing on real scores, and the literal "worsen the prompt → gate fails → revert" ask are
  all unverified beyond what a stub/synthetic report can prove. The recipe above is ready the moment a
  provider recovers.
- **`eval/rubric.py`'s cross-provider design assumes both are eventually reachable.** If only one
  provider ever recovers and it's the one already writing answers, the "different blind spots" property
  degrades to "one model grading itself" until the other comes back — reported, not hidden, since
  `score_answer`'s `error` field names exactly which provider was unavailable.
- **Stratification by project is a no-op** until multi-project history exists (this phase indexes one
  project at a time) — the code is structured for it, not stubbed around it.

---

## Phase M — quotas, access control, alerts, and a vocabulary that flags its own rot ✅ (2026-09-08)

Six things that turn "the answer pipeline works" into "the answer pipeline is safe to point at a
real org's repos and traffic": health alerts, cost quotas, a semantic answer cache, ACLs with an
audit trail, nightly vocabulary staleness detection, and a README that reflects all of it.

**One identity concept, used twice, named honestly.** Bridge has no login system — `api/main.py`'s
own docstring calls it a single-frontend dev tool. So `RunRequest.team` (a caller-declared string,
default `"default"`) is BOTH the cost-quota unit and the ACL principal. Inventing two names for one
concept would imply a distinction that doesn't exist yet; `security/acl.py`'s module docstring says
this plainly rather than letting a reader discover it by tracing two code paths to the same field.

### 1. Health alerts — `work/alerts.py`

Four conditions, matching the ask exactly: lag (commits OR minutes — independent thresholds, same
reasoning `work/health.py`'s `stale_reasons` already uses), parse-failure rate over 1%, an
unresolved-rate **jump** of more than 5 points since the last check, queue depth. `evaluate()` is
pure (a health payload and a previous-state dict in, `Alert`s out); a new `alert_state` table
(one row per repo) is the one piece of memory that turns "unresolved is 14%" (a fact about the
codebase) into "unresolved jumped from 2% to 14%" (a fact about the last commit) — the level alone
was never the interesting number. `send()` posts a plain `{"text": ...}` body, which is both a
real Slack incoming-webhook payload and works against any generic webhook receiver, and never
raises — a dead webhook must not stop the job from checking again next hour. Wired as a new
`health_alert` job on an hourly cron (`bridge worker` already runs the schedule loop from Phase F).

Verified: 19 tests, all four rules plus their boundaries, `send()` against a stubbed HTTP call
(never a real network hop — a test hitting Slack would test Slack's uptime, not this module), and
`alert_state` round-tripping against a real Postgres. Live: the real spring-petclinic index (0
alerts — a genuinely healthy repo) and a constructed jump against real data-shaped output:

```
{'repo': 'spring-petclinic', 'kind': 'unresolved_jump',
 'message': 'unresolved rate jumped 12.0pts (2.0% -> 14.0%)', 'severity': 'critical'}
```

### 2. Quotas — `orchestrator/quotas.py`, `GET /costs`

Two enforcement points, because team and lane become knowable at different moments. **Team**
accompanies the request, so it is the only quota that can refuse a run before anything is spent — a
team past its soft cap gets a header warning; past hard, a `429` before the orchestrator is even
constructed. **Lane** cannot be known before cartography (design law 3), so a lane cap can only
*tighten* a run already in progress, the moment its lane is decided (`orchestrator/run.py::
_apply_lane_cap`) — reusing the existing budget-exhausted → partial-answer path rather than
inventing a second way for a run to stop. `RunStore` gained `repo`/`team` columns via an ad hoc
`ALTER TABLE` migration (proven against a hand-built pre-Phase-M sqlite file, not just asserted)
and `cost_today()`/`costs_by()` for the `/costs` endpoint.

Verified: 17 tests, including the migration against a real pre-existing file, both quota
dimensions' boundaries, and — live — `/costs` returning real grouped spend through the actual
FastAPI app:

```
{'by_repo': [{'repo': 'spring-petclinic', 'runs': 3, 'cost_usd': 7.6}],
 'by_lane': [{'lane': 'change_request', 'cost_usd': 6.0}, {'lane': 'question', 'cost_usd': 1.2},
             {'lane': 'bug', 'cost_usd': 0.4}],
 'by_team': [{'team': 'platform', 'cost_usd': 6.0}, {'team': 'product', 'cost_usd': 1.6}]}
```

### 3. Semantic answer cache — `orchestrator/cache.py`, `run.py`

The exact-match cache (Phase D) only ever fires on an identical normalised question. This adds a
near-duplicate layer: embed the question (the same local bge encoder that embeds chunks — zero LLM
cost either way), and on an exact miss, look for a prior answer at cosine ≥ `SEMANTIC_CACHE_THRESHOLD`
(0.95) within the same snapshot and lane. Served answers are labelled distinctly — `"similar
question answered 2h ago ("...") — re-run for a fresh answer"` — never presented as if computed for
the exact question asked, and `env.cache.similar`/`source_question` carry that distinction to the
API (`cache_similar`/`cache_source_question` on the SSE `done` event) for a one-click re-run
(`fresh=True`).

**The "same lane" requirement is a documented approximation.** A question's real lane needs
interpretation AND cartography — exactly the cost a cache lookup exists to avoid. So
`cheap_lane_guess` reuses the interpreter's own keyword fallback (`agents/interpreter.py::
guess_intent`, extracted from `_fallback` rather than duplicated) as a zero-cost proxy at lookup
time only; the REAL lane is what gets stored on write, once a run has actually finished. This is
named as an approximation in the module docstring, not presented as the real thing.

Verified: 14 tests — cosine similarity and its edge cases (identical, orthogonal, below-threshold,
zero-vector) with plain vectors, a fake encoder for `embed_question`'s normalisation, and the
`answer_cache` migration (adding `lane`/`question_embedding` to a pre-existing table) against a
real sqlite file. **Not verified live**: an actual semantic hit through the orchestrator — that
needs two real runs against a live model, and both providers remain unreachable this phase (below).

### 4. ACLs — `security/acl.py`, `GET /access-audit`

`config/acl.<repo>.yaml` (`readable_by: [team-a, team-b]`) **mirrors** repo read permission from a
git host — mirrors in the sense of holding the shape a real integration would populate, not in the
sense of calling one. `StaticACLProvider` (the config file) is what runs; `GitHubACLProvider` is an
honest stub naming exactly what a live GitHub permission check needs (a token, the collaborator/
team-permission endpoint, a principal→GitHub-identity mapping, and a cache in front of it — nothing
here should ever be called uncached per question). A repo with no ACL file is open to everyone
(`config.ACL_DEFAULT_OPEN` — same convention as `config/vocab.*.yaml`'s absence). Every decision —
allowed or denied — is written to a new `access_audit` table before the request proceeds any
further, so a denial is on the record even though it never became a run.

**"No cross-repo leakage," cashed out honestly**: retrieval has no per-project routing yet (the
legacy SQLite path — still what answers every question — has no `project` column on a chunk at
all), so the finest grain enforcement can mean anything at today is the repo level, enforced at the
one point that's real: before a run starts. `filter_unauthorized_chunks` exists anyway, doing
nothing today (`allowed_projects=None` always), so the enforcement POINT for a real per-project ACL
already sits in the call graph once Phase H's routing gives a chunk a project to check.

Verified: 17 unit tests (`test_acl.py`) plus 5 through the **real FastAPI app** (`test_api_access_
control.py`, `TestClient` against the actual lifespan — real index, real embedded Postgres): an
unlisted team gets 403 and is on the audit log with no run created; a quota-exhausted team gets
429; and — proving the ALLOW path without ever starting the (unreachable-provider) pipeline — an
explicitly-listed team's request clears the ACL gate (audit row: `allowed: true`) before the quota
check is what actually stops it. Live, through the real app:

```
403 {'detail': "'platform' is not listed in the ACL policy for 'spring-petclinic'"}
GET /access-audit -> [{'principal': 'platform', 'allowed': False, 'reason': "'platform' is not
                       listed in the ACL policy for 'spring-petclinic'", ...}]
```

**A real bug found and fixed via a real (if quiet) test failure**: `test_api_access_control.py`
monkeypatches `config.CONFIG_ROOT` to an empty temp directory. The app's lifespan opens a
`Retriever`, which lazily loads `retrieval/vocab.py`'s module-level `_DEFAULT` vocabulary singleton
on first use — and if THIS test happened to be the first thing in the whole pytest session to touch
it, the singleton would permanently cache an empty vocabulary (read from the temp dir) for every
other test in the process, monkeypatch teardown or not, since the singleton never re-checks
`CONFIG_ROOT` once set. Caught by `test_semantic_petclinic.py::test_retrieve_runs_every_stage`
failing intermittently depending on test order — confirmed by reproducing it in isolation, then
fixed by resetting the singleton on both sides of the fixture. Documented in the test itself so the
next person who sees an unrelated vocabulary test fail doesn't have to re-derive this.

### 5. Vocabulary staleness — `work/vocab_staleness.py`

A nightly job (new `vocab_staleness` job type, 2am cron) checking every `config/vocab.<repo>.yaml`
term's node ids against the CURRENT graph — exact detection (`GraphStore.node(id) is None`).
**Replacement mining is a heuristic, named as one**: a same-`(kind, bare name)` match elsewhere in
the current graph, not a content-hash-verified rename trace. The honest version — code_hash survives
a rename — needs the Postgres content-addressed index (Phase E onward); the legacy SQLite graph
node ids carry no hash history to walk, and retrieval hasn't been cut over yet (Phase E/F/G/L's
running carry-forward). A dead node WITH a same-name candidate becomes a `vocab_proposals` row
(reusing the existing table `POST /vocab/proposals` already writes to) with a note stating plainly
that it's a heuristic match needing confirmation; a dead node with no candidate is still reported,
just not proposed. `GET /vocab/proposals` and `POST /vocab/proposals/{id}/review` round out the
review queue this phase's Settings-page ask (queued, not yet built) will need.

Verified: 16 tests against a real `GraphStore` and a real sqlite `vocab_proposals` table — every id
scheme's bare-name parsing, candidate matching, and the end-to-end write/review path. Live, against
the real spring-petclinic vocabulary and index: `terms_checked=8, stale_terms=0` — a real, clean
result, not a constructed one; the stale-detection path itself is what the 16 unit tests exercise
directly with a deliberately dead node.

### 6. README refresh

Rewritten: the v3 two-store architecture stated plainly (including that retrieval hasn't been cut
over to Postgres yet — the thing that makes several "per-project" promises elsewhere in this phase
honest no-ops rather than silent lies), a docker-compose quickstart alongside the embedded-Postgres
one, a model table pulled directly from `config.ROLE_MODELS`/`ROLE_PROVIDER` including the judge's
deliberate cross-provider pinning, the new costs/quotas/ACL/alerts section, and an updated eval
section covering `run_full`/`gate`/`rolling_quality` alongside the original smoke suite. The old
"out of scope" list was stale (it still listed the changelog lane as unbuilt, from before Phase A) —
replaced with what is actually still stubbed today.

### `pytest`

```
544 passed in 276.27s (0:04:36)
```

456 prior (Phase L's baseline) + 88 new: 19 `test_alerts.py`, 17 `test_quotas.py`, 14
`test_semantic_cache.py`, 17 `test_acl.py`, 5 `test_api_access_control.py`, 16
`test_vocab_staleness.py` = 88, matching the 544 the real run above reports.

### Decisions worth remembering

- **A lane cap tightens; it never stops.** Only a team-level check (known pre-run) can refuse a
  request outright. A lane's cap reuses the ordinary budget-exhausted → partial-answer path,
  because design law 3 means the lane is never known early enough to gate on before spending
  anything.
- **"Unmeasured" is not "denied," and a gap week is not "zero."** Phase L already established this
  for the rubric; Phase M's alert jump-detection applies the same rule — a level with no prior
  reading establishes a baseline, it never fires an alert on its own.
- **An ACL file NARROWS access; its absence does not gate it.** Same convention as
  `config/vocab.*.yaml` — an org that hasn't written one yet is not locked out of their own repo.
- **Every access decision is audited, allowed or denied.** A denial that never became a run is
  still the row someone will look for later.

### Carry-forward caveats

- **Retrieval still reads the legacy SQLite index**, now touching four more phases' worth of
  features that describe themselves as "per project" or "per repo" — every one of them is honestly
  a no-op or a repo-level approximation until this cutover lands, and each says so at the point it
  matters rather than once at the top and hoping it's remembered.
- **No live LLM call succeeded anywhere in this phase either** (Gemini: monthly spend cap;
  Anthropic: credit balance too low — both re-verified live this session). The semantic cache's
  actual hit behaviour through the orchestrator, and health alerts against a real Slack workspace,
  are both built and unit-tested but not exercised end to end with a real model or a real webhook.
- **`GitHubACLProvider` is not implemented** — intentionally, as a documented seam.
- **The vocab-staleness replacement heuristic will get materially better once retrieval moves onto
  the Postgres index** — genuine content-hash rename tracing becomes possible then, not just a
  same-name guess.
