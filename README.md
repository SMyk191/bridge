# Bridge

> Product managers ask "where does this happen in the code?" and wait days for an engineer. Bridge answers in about a minute, cites the exact file and lines, and says so when the evidence is too thin to answer.
>
> **Eval on spring-petclinic:** 100% file recall across 12 questions, about $0.04 per question.

Bridge is a read-only, codebase-grounded translation layer between Product and Engineering. It
indexes a repository — a structural graph of classes, endpoints, entities, and screens; a
semantic index over what each piece of code actually does; git history; and an org-specific
product vocabulary — then answers product-language questions with `repo@sha:path:start-end`
citations, produces impact maps, and drafts technical specs for changes and bugs. It never writes
to the repository it indexes; the only things it writes are its own opinion about itself
(feedback, vocabulary proposals, cost/access records) in its own local app store, and the
persistent index it builds in Postgres.

Under the hood it is a fixed pipeline, not an open-ended agent: a Python orchestrator decides
which node runs next (interpreter → cartographer → historian → explainer/architect/changelog →
verifier), and each node is a plain function that reasons once and returns. The lane a question
takes — a narrative explanation, an impact map with a technical brief, a changelog, or a redirect
for a data question — is decided *after* the evidence comes back, from what the evidence actually
shows, never from how the question was phrased. When the evidence is thin, the answer says so and
abstains rather than guessing; every claim it does make carries a citation back to the exact lines
that support it, and that citation is independently checked before the answer is shown
(`orchestrator/verify.py`) — a citation that doesn't resolve is dropped, not trusted.

## Design laws

1. **Deterministic edges, probabilistic nodes** — Python decides which agent runs next; LLMs only reason inside a node.
2. **Fan out for reading, single-thread for writing** — no writing exists in this phase.
3. **Route after analysis** — the lane is derived from the impact map by rules, not from how the user phrased the ask.
4. **Every claim carries a citation** `repo@sha:path:start-end`. If retrieval confidence is low, the answer says so and abstains rather than guesses.
5. **Retrieved text is data, never instruction.** Wrapped in `<retrieved>` tags in prompts, and the prompt says so.
6. **Every run has a token/cost budget and a persisted trace.**
7. **All org-specific things** (vocab, repo paths, trackers, ACLs) live in `config/` or adapters, never hardcoded in Python.
8. **Secrets/PII are redacted** before any text reaches an LLM.

## Architecture (v3): two stores, one still catching up to the other

Bridge grew a permanent, content-addressed index (Postgres + pgvector — hash-chained blobs,
chunks, descriptions, and vectors that every snapshot of every repo shares, so a second commit
costs only what actually changed) alongside the original one-directory-per-commit SQLite +
LanceDB index. **The answer pipeline still reads the original SQLite index** — the Postgres side
is fully real (incremental indexing, a job queue with coalescing workers, retention/GC, partitioned
vectors, health alerts, module summarisation) but the retrieval cutover onto it hasn't landed yet.
This is stated plainly rather than glossed over because it shapes what a few features below can
promise: anything scoped to "a project" is a no-op until that cutover gives a chunk a project to
scope by. See [PROGRESS.md](PROGRESS.md) for exactly which phase did what.

```
question ──▶ interpreter ──▶ retrieve (SQLite graph + LanceDB vectors + vocab)
                                  │
                                  ▼
                            cartographer ──▶ [lane decided from the impact map]
                                  │                    │
                         ┌────────┴─────────┬──────────┴───────┐
                         ▼                  ▼                  ▼
                     explainer          architect          changelog
                    (question)      (bug/change_request)  (git log, no
                         │                  │              retrieval at all)
                         └────────┬─────────┘
                                  ▼
                              verifier ──▶ answer (citations checked, confidence scored)
```

Separately, `bridge index` / `bridge worker` / `bridge gc` operate the **persistent** Postgres
index: incremental indexing keyed on a content hash chain (`blob → code → chunk → description →
vector`, each hash-chained so a rename or a pure move costs zero model calls), a job queue with
per-repo coalescing (`FOR UPDATE SKIP LOCKED`), retention + garbage collection, and vectors
partitioned by project with a two-stage (quantised-then-rescored) search — real on a
docker-compose Postgres 0.7+, degraded to a correct-but-unquantised path on the embedded
pgserver's 0.6.2, which the code detects rather than assumes.

## Quickstart

Requires Python 3.12, Node 18+, and a `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`, per
`backend/bridge/config.py::ROLE_PROVIDER`) in a `.env` file at the repo root — without one, the
API fails loudly naming the missing variable rather than degrading silently.

```bash
cd backend && uv venv .venv --python 3.12 && uv pip install -e ".[dev]"
cd frontend && npm install
make index    # build the structural + semantic index for spring-petclinic (~5 min, one-time)
make api      # terminal 1 — FastAPI on http://127.0.0.1:8000
make ui       # terminal 2 — Vite dev server on http://localhost:5173
```

Optional, once both servers are up: `make seed` populates three demo threads so the dashboard
isn't empty on first look, and `make smoke` re-runs the evaluation suite below against whatever
index is currently built.

### The persistent (Postgres) index, and docker-compose

`bridge index`/`worker`/`gc` (the content-addressed index and its job queue, Phases E–G) need a
real Postgres 16 + pgvector. Two ways to get one, and the code is identical either way — only the
DSN differs:

```bash
# with Docker (real pgvector 0.7+, real int8-quantised vector search):
docker compose up -d
cd backend && .venv/bin/alembic upgrade head
export BRIDGE_DATABASE_URL="postgresql+psycopg://bridge:bridge@localhost:5433/bridge"

# without Docker (a venv-local Postgres 16.2 + pgvector 0.6.2, no int8 column —
# the code detects this at runtime and runs the unquantised-but-correct path instead):
export BRIDGE_DATABASE_URL="embedded://"
```

Then:

```bash
.venv/bin/python -m bridge.cli index data/repos/spring-petclinic   # full, then incremental
.venv/bin/python -m bridge.cli worker --partition 0/1              # index_commit/gc/alerts/vocab jobs
.venv/bin/python -m bridge.cli gc --apply                          # retention sweep (dry-run without --apply)
```

`GET /health/index` reports lag, description coverage by tier, parse-failure and unresolved
rates, and queue depth for this side of the system; `bridge worker` also runs an hourly health
alert and a nightly vocab-staleness pass on its own (below).

## Model table

Every LLM call goes through `agents/llm.py`, addressed by **role**, never by model name at the
call site — flipping `config.py::ROLE_PROVIDER[role]` between `anthropic`/`gemini` is the entire
mechanism for moving a role to the other provider, no code change. Both providers stay fully wired
at all times; whichever is unreachable degrades to a deterministic, keyword-based fallback rather
than failing the run (`agents/interpreter.py::guess_intent`, `_fallback`).

| role | provider (today) | model | why |
|---|---|---|---|
| `interpreter` | Gemini | `gemini-2.5-flash` | 3-vote majority at temp 0; needs a model that allows an explicit zero thinking budget |
| `cartographer` | Gemini | `gemini-3.5-flash-lite` | one call per implicated module — cheap tier |
| `cartographer_merge` | Gemini | `gemini-3.1-pro-preview` | merging module summaries into one impact map is the one cheap-tier step promoted to deep |
| `architect` | Gemini | `gemini-3.1-pro-preview` | the change_request/bug lane's writer — reads real code |
| `explainer` | Gemini | `gemini-3.1-pro-preview` | the question lane's writer — reads real code |
| `changelog` | Gemini | `gemini-3.5-flash-lite` | one call per product-area group over commit subjects — the cheapest work in the system |
| `describe`/`rerank`/`suggestions` | Gemini | `gemini-3.5-flash-lite` | index-time and retrieval-time utility calls |
| `judge` | **Anthropic** | `claude-sonnet-5` | eval/rubric.py's independent grader — pinned to the *other* provider from every answer-writing role above, deliberately, so a grader doesn't share the writer's blind spots |

Temperature is 0 for every role on every provider by default (`config.DEFAULT_TEMPERATURE`
— the one documented exception is Anthropic's extended thinking, which rejects an explicit
temperature and is omitted from those requests rather than risking a 400).

## Costs, quotas, and access control

Every run is attributed to a **team** — a caller-declared string (`POST /runs {"team": "..."}`,
default `"default"`); Bridge has no login system, so this is the honest unit of attribution
available today, not a stand-in for real multi-tenant auth, and it doubles as the ACL principal
below.

- **`GET /costs?days=N`** — spend grouped by repo, lane, and team, plus the configured quota
  numbers, from `data/runs.sqlite`.
- **Quotas** (`orchestrator/quotas.py`) — a team past its soft daily cap (`config.
  QUOTA_TEAM_DAILY_USD_SOFT`) gets a warning on its answer header; past the hard cap (`_HARD`) a
  run is refused outright, before anything is spent, with a `429`. A **lane** cap
  (`config.QUOTA_LANE_DAILY_USD`) can only ever tighten a run already in progress, once its lane
  is known (design law 3 says the lane can't be known any earlier) — it reuses the ordinary
  budget-exhausted → partial-answer path, never a separate hard stop.
- **Private repos** (`security/acl.py`) — `config/acl.<repo>.yaml` lists which teams may read a
  repo (`readable_by: [team-a, team-b]`); a repo with **no** file is open to everyone
  (`config.ACL_DEFAULT_OPEN`), the same convention `config/vocab.<repo>.yaml`'s absence already
  uses. Every decision — allowed or denied — is written to `access_audit`
  (`GET /access-audit?repo=&principal=`) before the request is allowed to proceed any further.
  `StaticACLProvider` (the config file) is what runs; `GitHubACLProvider` is a documented, honest
  stub for a live git-host permission check, naming exactly what implementing it needs.
- **Health alerts** (`work/alerts.py`) — an hourly job compares `/health/index` against the last
  check and pushes to a Slack-compatible webhook (`BRIDGE_ALERT_WEBHOOK_URL`) on index lag, a
  parse-failure rate over 1%, an unresolved-rate **jump** of more than 5 points since the last
  check (a level is a fact about the codebase; a jump is a fact about the last commit), or a
  queue backing up.

## Evaluation

Two tiers. `eval/smoke_questions.yaml` (12 questions) is the fast CI hook `make smoke`/`make
determinism` run against. `eval/sets/petclinic.yaml` (20 questions — multi-hop, three
independently-phrased traps, two changelog asks, two real thread follow-ups) is the full picture:

```bash
make full            # eval/run_full.py: recall, core-file recall, rubric, confidence, lane,
                      # cost, latency, determinism — per question and rolled up
make gate             # eval/gate.py: run the suite, fail on regression vs the stored baseline
make gate-baseline    # store the current report as the new baseline (do this deliberately)
make quality          # eval/rolling_quality.py: rubric-score a sample of REAL traffic, print
                      # the trend, mirror thumbs-down feedback into eval_candidates
```

`eval/rubric.py` is an independent LLM judge (Anthropic, cross-provider from the Gemini-based
writers — see the model table) that scores correctness/completeness/actionability 1–5 against
evidence it fetches itself (`git show <sha>:<path>`, not the model's say-so), and floors
correctness in code — never in the prompt — when it finds a claim the evidence doesn't support.
`eval/gate.py` fails a run against its baseline on a core-recall drop past 5 points, a rubric-mean
drop past 0.3, or any question that regressed from determinism-stable to unstable.

```
mean file recall : 100.0%   (12/12 questions at 100%)
high confidence   : 7/12   (the other 5 are the two data-question traps, the unanswerable
                             question, and two questions whose evidence is real but thin —
                             low/medium confidence is the correct, honest answer there, not a gap)
total cost        : $0.4677   (mean $0.0390/question)
```

Run it yourself: `make smoke`, or `cd backend && .venv/bin/python ../eval/run_smoke.py
<question-id>` for one question with the full printed answer.

## What's stubbed or honestly incomplete

This is a working core, not a finished product.

- **Retrieval hasn't been cut over to the Postgres index.** Every answer today is computed against
  the legacy SQLite/LanceDB index; the persistent index (Phases E–G) is fully built and tested on
  its own, but nothing routes a real question through it yet. Anything described as "per project"
  is a documented no-op until this lands (a chunk has no `project` column in the path that's
  actually live).
- **The semantic answer cache's "same lane" gate is a cheap keyword guess**
  (`orchestrator/cache.py::cheap_lane_guess`), not the real cartographer-derived lane — computing
  the real lane is exactly the cost a cache lookup exists to avoid paying.
- **`GitHubACLProvider` is not implemented** — `config/acl.<repo>.yaml` (a config file a person
  edits) is what runs; the stub documents exactly what a live git-host permission check needs.
- **Health alerts, once configured, have not been verified against a real Slack workspace** — only
  against a stubbed HTTP call in tests.
- **No JIRA integration** — "Draft ticket" formats the architect's brief as a ticket in a modal;
  nothing pushes it anywhere.
- **A runtime layer** — Bridge reasons entirely from static analysis and git history. It has no
  visibility into what actually runs in production: no traces, no error rates, no feature flags.
- **A merge/CI gate** — `make gate` can fail a build; nothing wires that into a PR check yet.

See [PROGRESS.md](PROGRESS.md) for the phase-by-phase build log, every bug found and how it was
fixed, and the caveats each phase left open.

## Author

Built by [Samyak Jain](https://github.com/SMyk191). Released under the MIT License.
