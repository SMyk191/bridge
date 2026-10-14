"""All tunable constants for Bridge live here (CLAUDE.md conventions).

Org-specific values — repo paths, vocab, trackers — belong in ``config/`` or
``adapters/``, never in this file and never inline in a module.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# ---------------------------------------------------------------- environment
# Loaded once, here, at import time — before any provider client is constructed.
# config.py is imported before agents/llm.py in every path into the system, so a
# provider can never be built against a half-populated environment.
#
# `override=False`: a variable already exported in the shell wins over the file.
# The file is the fallback for local development, not an override of deployment
# configuration.
_REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = _REPO_ROOT / ".env"
ENV_FILE_LOADED = load_dotenv(ENV_FILE, override=False) if ENV_FILE.is_file() else False

#: The .env contents, kept separately so a *malformed* shell value cannot shadow
#: a good one in the file. See `provider_key`.
ENV_FILE_VALUES: dict[str, str] = (
    {k: v for k, v in (dotenv_values(ENV_FILE) or {}).items() if v} if ENV_FILE.is_file() else {}
)

#: Shortest plausible credential. Real keys are far longer (Google ~39,
#: Anthropic ~100), so anything below this is a placeholder or a truncated paste.
MIN_KEY_LENGTH = 20

# ---------------------------------------------------------------- providers
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"

# Model names, effort, and provider live here and nowhere else — no agent module
# inlines one, and no caller ever names a model.
MODEL_CHEAP = "claude-haiku-4-5"
MODEL_DEEP = "claude-sonnet-5"
MODEL_DEEP_EFFORT = "high"

# Gemini model tiers.
#
# The 2.5 flash-lite and pro models were requested, but this API key cannot call
# them: the API returns 404 "no longer available to new users" and names its own
# successor. We use the successor it names, keeping the requested *tier*:
#
#   requested                 status              using
#   gemini-2.5-flash-lite     404, gated          gemini-3.5-flash-lite
#   gemini-2.5-flash          works               gemini-2.5-flash  (unchanged)
#   gemini-2.5-pro            404, gated          gemini-3.1-pro-preview
#
# The 2.5 ids are kept below so this is a one-line revert if the account is
# ungated later.
GEMINI_FLASH_LITE = "gemini-3.5-flash-lite"
GEMINI_FLASH = "gemini-2.5-flash"
GEMINI_PRO = "gemini-3.1-pro-preview"

#: requested but 404 on this key — kept for an easy switch back
GEMINI_FLASH_LITE_25 = "gemini-2.5-flash-lite"
GEMINI_PRO_25 = "gemini-2.5-pro"

#: Models that reject `thinking_config.thinking_budget=0`. The 3.x tiers only
#: run in thinking mode, so a short `max_output_tokens` can be consumed entirely
#: by reasoning and return empty text — these roles need generous headroom
#: instead of a disabled budget. Verified against the live API.
GEMINI_THINKING_ALWAYS_ON = frozenset({GEMINI_FLASH_LITE, GEMINI_PRO, "gemini-3.5-flash"})

#: Which provider each role runs on. **This is the switch.** Flip a role back to
#: `PROVIDER_ANTHROPIC` and it uses the Anthropic model configured for it below —
#: no other edit, no code change. Both providers stay fully wired at all times.
#:
#: `judge` (eval/rubric.py) is pinned to Anthropic while every answer-writing
#: role runs on Gemini — deliberately the opposite of the usual "flip if one
#: provider is down" reasoning. A judge grading answers written by its own model
#: family shares that family's blind spots: a hallucinated detail that reads as
#: plausible to the model that invented it tends to read as plausible to a
#: same-family grader too. Cross-provider grading is not a complete defense
#: against that, but it is a cheap and real one.
ROLE_PROVIDER: dict[str, str] = {
    "describe": PROVIDER_GEMINI,
    "rerank": PROVIDER_GEMINI,
    "suggestions": PROVIDER_GEMINI,
    "interpreter": PROVIDER_GEMINI,
    "cartographer": PROVIDER_GEMINI,
    "cartographer_merge": PROVIDER_GEMINI,
    "architect": PROVIDER_GEMINI,
    "explainer": PROVIDER_GEMINI,
    "changelog": PROVIDER_GEMINI,
    "judge": PROVIDER_ANTHROPIC,
}

#: Per-provider, per-role model configuration. A role must appear under every
#: provider it could be switched to, so flipping ROLE_PROVIDER can never land on
#: an undefined model.
ROLE_MODELS: dict[str, dict[str, dict]] = {
    PROVIDER_ANTHROPIC: {
        "describe": {"model": MODEL_CHEAP, "max_tokens": 300},
        "rerank": {"model": MODEL_CHEAP, "max_tokens": 4000},
        "suggestions": {"model": MODEL_CHEAP, "max_tokens": 1000},
        "interpreter": {"model": MODEL_CHEAP, "max_tokens": 2000},
        "cartographer": {"model": MODEL_CHEAP, "max_tokens": 2000},
        "cartographer_merge": {
            "model": MODEL_DEEP,
            "effort": MODEL_DEEP_EFFORT,
            "max_tokens": 8000,
        },
        "architect": {"model": MODEL_DEEP, "effort": MODEL_DEEP_EFFORT, "max_tokens": 16000},
        "explainer": {"model": MODEL_DEEP, "effort": MODEL_DEEP_EFFORT, "max_tokens": 16000},
        "changelog": {"model": MODEL_CHEAP, "max_tokens": 2000},
        # No `effort`, deliberately: extended thinking is the one documented
        # exemption from DEFAULT_TEMPERATURE (below), and a grading role has no
        # need to trade that guarantee for deeper reasoning it does not require.
        "judge": {"model": MODEL_DEEP, "max_tokens": 2000},
    },
    PROVIDER_GEMINI: {
        # `max_tokens` caps thinking AND the answer together. The 3.x tiers cannot
        # disable thinking (a zero budget is a 400), so every ceiling here carries
        # headroom well above the answer length — a describe answer is ~40 tokens,
        # the rest is room for reasoning. Unused headroom costs nothing; too little
        # returns empty text with finish_reason=MAX_TOKENS.
        "describe": {"model": GEMINI_FLASH_LITE, "max_tokens": 2000},
        "rerank": {"model": GEMINI_FLASH_LITE, "max_tokens": 8000},
        "suggestions": {"model": GEMINI_FLASH_LITE, "max_tokens": 3000},
        # 2.5-flash still allows an explicit zero budget.
        "interpreter": {"model": GEMINI_FLASH, "max_tokens": 2000, "thinking_budget": 0},
        # per-subsystem summaries: cheap tier, one call per implicated module
        "cartographer": {"model": GEMINI_FLASH_LITE, "max_tokens": 3000},
        "cartographer_merge": {"model": GEMINI_PRO, "max_tokens": 16000},
        "architect": {"model": GEMINI_PRO, "max_tokens": 32000},
        # the question lane's writer: reads real code, so it gets the deep tier
        "explainer": {"model": GEMINI_PRO, "max_tokens": 32000},
        # one call per changelog group over commit subjects — the cheapest work
        # in the system, and deliberately not on a reasoning tier
        "changelog": {"model": GEMINI_FLASH_LITE, "max_tokens": 4000},
        # 2.5-flash allows an explicit zero thinking budget, same reasoning as
        # `interpreter` above: a grading role should not have its temperature
        # guarantee silently withdrawn by a thinking-tier default.
        "judge": {"model": GEMINI_FLASH, "max_tokens": 3000, "thinking_budget": 0},
    },
}

#: Roles, derived. Kept as a name so existing lookups keep working.
ROLES: dict[str, dict] = {
    role: ROLE_MODELS[provider][role] for role, provider in ROLE_PROVIDER.items()
}

#: `output_config.effort` errors on models that predate the parameter — Haiku 4.5
#: is one, so a role's `effort` is dropped rather than sent to a model that 400s.
MODELS_SUPPORTING_EFFORT = frozenset(
    {"claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5", "claude-fable-5"}
)

#: USD per 1M tokens, (input, output). Used for cost accounting in agents/llm.py.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}

#: Claude Sonnet 5 carries introductory pricing through this date (inclusive).
SONNET_5_INTRO_PRICING = (2.00, 10.00)
SONNET_5_INTRO_UNTIL = "2026-08-31"

#: cache reads bill at ~0.1x input, 5-minute cache writes at ~1.25x input
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

# ---------------------------------------------------------------- gemini pricing
# Paid tier, USD per 1M tokens, (input, output) — ai.google.dev/gemini-api/docs/pricing.
# Gemini bills THINKING TOKENS AT THE OUTPUT RATE, so llm.py adds
# `thoughts_token_count` to output tokens before costing. Under-counting there
# would make a thinking model look far cheaper than it is.
GEMINI_PRICING: dict[str, tuple[float, float]] = {
    # in use
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    # other tiers, for when a role is repointed
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    # gated on this key, kept so a switch back still costs correctly
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

#: Pro tiers bill higher above the long-context threshold.
GEMINI_PRICING_LONG_CONTEXT: dict[str, tuple[float, float]] = {
    "gemini-3.1-pro-preview": (4.00, 18.00),
    "gemini-2.5-pro": (2.50, 15.00),
}
GEMINI_LONG_CONTEXT_THRESHOLD = 200_000

#: Gemini context-cache hits bill at ~0.25x input.
GEMINI_CACHE_READ_MULTIPLIER = 0.25

#: Environment variables consulted for each provider's credentials, in order.
#: The first name is the one a "missing key" error tells the user to set.
PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    PROVIDER_ANTHROPIC: ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    PROVIDER_GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def is_plausible_key(value: str | None) -> bool:
    """Whether a value could be a real credential rather than a placeholder."""
    return bool(value) and len(value.strip()) >= MIN_KEY_LENGTH


def provider_key(provider: str) -> str | None:
    """The credential for ``provider``, or None.

    Environment wins over the ``.env`` file — deployment config should beat a
    local file. The one exception: a shell value that is *obviously not a key*
    (a leftover ``export FOO="…"`` placeholder) must not shadow a real key in
    ``.env``. Silently authenticating with a placeholder produces a confusing
    401 far from its cause.
    """
    for name in PROVIDER_ENV_KEYS.get(provider, ()):
        value = os.environ.get(name)
        if is_plausible_key(value):
            return value
        file_value = ENV_FILE_VALUES.get(name)
        if is_plausible_key(file_value):
            return file_value
    return None


def shadowed_keys(provider: str) -> list[str]:
    """Variables set in the shell to a non-credential value, shadowing ``.env``."""
    return [
        name
        for name in PROVIDER_ENV_KEYS.get(provider, ())
        if os.environ.get(name) and not is_plausible_key(os.environ.get(name))
    ]


def missing_key_message(provider: str) -> str:
    """The error text for an absent credential — always names the variable."""
    names = PROVIDER_ENV_KEYS.get(provider, ())
    primary = names[0] if names else "the provider's API key"
    alternatives = f" (or {', '.join(names[1:])})" if len(names) > 1 else ""
    where = (
        f"Set it in {ENV_FILE} or export it."
        if not ENV_FILE_LOADED
        else f"{ENV_FILE} was loaded but does not define a usable value."
    )
    junk = shadowed_keys(provider)
    if junk:
        where += (
            f" NOTE: {', '.join(junk)} is set in your shell to a value shorter than"
            f" {MIN_KEY_LENGTH} characters — that looks like a leftover placeholder."
            " Remove it (check ~/.zshenv) so the real key is used."
        )
    return f"{provider} is the configured provider but {primary}{alternatives} is not set. {where}"

#: SDK-level retries for 429/5xx/connection errors
LLM_MAX_RETRIES = 4
LLM_TIMEOUT_SECONDS = 120.0

#: default per-run ceiling (design law 6). None on a field means unbounded.
DEFAULT_BUDGET_USD = 5.00
DEFAULT_BUDGET_CALLS = 2000

# ---------------------------------------------------------------- orchestrator
#: Per-run ceilings for an agent run. Hit any one and the run aborts *gracefully*
#: with whatever it has, rather than failing — a partial impact map beats none.
RUN_BUDGET_USD = 0.40
RUN_BUDGET_INPUT_TOKENS = 60_000
#: Raised from 90s in phase A. The question and change lanes gained a writer that
#: reads real source, and a full run measured 98.6s on the widest smoke question
#: (`rename-owner-customer`) — under the old ceiling that run would have aborted
#: at the architect boundary and thrown away a brief it had the money to write.
#: Cost and input tokens were re-measured at the same time and left alone: the
#: most expensive run of the suite was $0.159 of $0.40, and the largest 11.5k of
#: 60k input tokens, so neither is close to binding.
#: Raised again to 240s in phase C: a follow-up search plus the map rebuild it
#: forces adds ~25s, and a bug run measured 202s against the 150s ceiling. The
#: ceiling has to cover the work the pipeline can now choose to do, or the
#: follow-up rule spends the writer's budget and the run aborts before answering.
RUN_BUDGET_SECONDS = 240.0
RUN_BUDGET_CALLS = 40


#: intents the interpreter may return
INTENTS = ("question", "change_request", "bug", "data_question")
#: lanes the orchestrator may route to; architect runs only for these two
ARCHITECT_LANES = ("change_request", "bug")
#: lanes the explainer writes the answer for
EXPLAINER_LANES = ("question",)
#: every lane the orchestrator can end up in
LANES = ("question", "change_request", "bug", "data_question", "changelog")

# ---------------------------------------------------------------- code in prompts
#: (AGENT_CODE_MAX_CHARS is defined with the description constants below, because
#: it is deliberately the same number — see there.)
#: how many chunks the explainer reads in full
EXPLAINER_CHUNKS = 8
#: how many impact-map touch-points the architect reads in full
ARCHITECT_CODE_CHUNKS = 6

# ---------------------------------------------------------------- changelog
#: commits summarised when the ask names no range
CHANGELOG_DEFAULT_COMMITS = 20
#: hard ceiling on a requested range, so one ask cannot walk a whole history
CHANGELOG_MAX_COMMITS = 200
#: areas summarised; the rest are rolled into a deterministic "other areas" line
CHANGELOG_MAX_GROUPS = 8
#: files listed per area in the prompt
CHANGELOG_MAX_FILES_PER_GROUP = 12

#: Where everything that is not a product area goes. Build files, CI, wrappers,
#: and container config all change for reasons a product reader does not care
#: about individually — and left ungrouped they each become their own "area"
#: (a repo's root files have no parent directory to group under), which is how a
#: changelog ends up leading with `build.gradle` instead of with the owner module.
CHANGELOG_BUILD_AREA = "build & tooling"

#: Path fragment -> the area it collapses into, applied before the directory
#: rule. One schema change written three times for h2, mysql and postgres is one
#: product fact, not three areas — and left split it takes three of the eight
#: slots and pushes a real module out of the changelog entirely (observed on
#: petclinic's "unique pet name" commit, which touched all three dialects).
CHANGELOG_MERGED_AREAS: tuple[tuple[str, str], ...] = (("/db/", "database schema"),)
#: path prefixes that are tooling rather than product
CHANGELOG_BUILD_PREFIXES = ("gradle/", ".github/", ".mvn/", "k8s/", "docker/", ".devcontainer/")
#: exact root-level filenames that are tooling
CHANGELOG_BUILD_FILES = frozenset(
    {
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "gradlew", "gradlew.bat", "mvnw", "mvnw.cmd", "Dockerfile", "docker-compose.yml",
        "Makefile", "package.json", "package-lock.json",
    }
)

#: a file touched this many times inside the history window is "volatile"
VOLATILE_COMMITS_IN_WINDOW = 5

# ---------------------------------------------------------------- determinism
# Phase D: three sources of run-to-run variance, addressed in one place each.
#
# 1. sampling — every model call defaults to temperature 0 (below).
# 2. tie-breaking — a sort with ties (equal rerank scores, equal file-history
#    ranking) is only as deterministic as its tie-break. Every score-ordered
#    sort in retrieval/retrieve.py and orchestrator/followup.py breaks ties on
#    `chunk.node_id` ascending, an id that is itself derived from the parsed
#    source and stable across runs of the SAME index build.
# 3. residual model variance — temperature 0 reduces sampling variance but does
#    NOT guarantee bit-for-bit identical output: provider-side request batching
#    and (for MoE models) token-routing are not perfectly associative, so the
#    same prompt can rarely still return a different answer. This is a known,
#    documented limit of every hosted LLM API, not a bug in this client. The
#    interpreter's 3-vote majority (agents/interpreter.py) exists because of
#    it: intent classification sits on a few genuinely ambiguous boundaries
#    (data_question vs question was the one earlier phases kept observing),
#    and voting turns a coin-flip into a rare, *recorded* disagreement instead
#    of a silent, invisible one.

#: Every role, on every provider, calls the model at this temperature unless a
#: spec explicitly overrides it (`ROLE_MODELS[provider][role]["temperature"]`)
#: or the exemption below applies. eval/check_determinism.py and the
#: interpreter's voting are both built on this being the default everywhere.
DEFAULT_TEMPERATURE = 0.0

#: EXEMPTION, and the only one. Anthropic's extended thinking — turned on by
#: sending `output_config.effort` for a model in MODELS_SUPPORTING_EFFORT —
#: forces the model's own internal reasoning temperature and rejects an
#: explicit override in the request. `agents/llm.py::AnthropicProvider.generate`
#: checks for exactly this combination and omits `temperature` from the request
#: entirely rather than sending 0 and risking a 400. Every other role/provider
#: pair in ROLE_MODELS gets DEFAULT_TEMPERATURE with no exception — including
#: every Gemini role, thinking-tier ones included: Gemini's `temperature` and
#: `thinking_config` are independent knobs, and nothing in the SDK rejects the
#: combination the way Anthropic's extended thinking does.
TEMPERATURE_EXEMPT_ANTHROPIC_ROLES_WITH_EFFORT = True

#: independent votes the interpreter takes on `intent` before majority-voting a
#: final answer. Odd, so a simple majority exists among the successful votes
#: except in a genuine 3-way split — which is itself recorded, not silently
#: broken by a coin flip.
INTERPRETER_VOTES = 3

# ---------------------------------------------------------------- answer cache
# A cache entry is (snapshot_key, question_norm) -> a previous run_id whose
# full answer can be replayed for $0 and ~0s. See orchestrator/cache.py for
# `snapshot_key`/`question_norm`, and orchestrator/run.py for where it is
# consulted and written.
#
# Deliberately NOT consulted, and NOT written to, for:
#   - a request carrying the API's `fresh=True` bypass flag — the read is
#     skipped; a fresh run's own completion still REFRESHES the entry, so
#     `fresh` means "force-recompute this once," not "stop caching forever";
#   - any question asked into a thread that already has a prior run —
#     `state.prior_context` is non-empty, and a follow-up's correct answer can
#     depend on that history, which the cache key carries no trace of;
#   - the changelog lane — its evidence is `git log` against the LIVE
#     checkout on disk, not the indexed commit, so a cached changelog answer
#     can go stale the moment someone pushes to the repo, independent of
#     anything `snapshot_key` tracks (which only sees the INDEXED sha).
# A write only ever happens for a run whose final `status` is `"done"` — never
# `aborted`, `needs_clarification`, or anything that errored.
CACHE_ENABLED = True

# ---------------------------------------------------------------- follow-up retrieval
# A first retrieval pass answers the question it was given. These rules notice
# when the *evidence* has a hole in it — a gap the mapping step named, a bug lane
# that never reached the write path — and spend one more search closing it.

#: hard ceiling on extra retrieve() calls in one run
FOLLOWUP_MAX_SHOTS = 2
#: a shot needs this fraction of the run's tightest ceiling still unspent.
#: A follow-up that pushes the run into an abort has made the answer worse, not
#: better: it costs the writer its whole budget to add a few chunks nobody reads.
FOLLOWUP_MIN_BUDGET_FRACTION = 0.30
#: how many extra chunks the merged set may carry per shot fired
FOLLOWUP_EXTRA_PER_SHOT = 6
#: chunks each shot keeps in the merged set even if they score below the cut —
#: without it a single high-scoring shot takes every new slot and the second
#: shot's evidence, which answered a different hole, never reaches the writer
FOLLOWUP_MIN_PER_SHOT = 3

#: words that steer a search toward the code that WRITES (rule b)
FOLLOWUP_WRITE_TERMS = "save update delete repository transaction"
#: and the mirror, toward the code that DISPLAYS
FOLLOWUP_READ_TERMS = "show display page view get find list"
#: path fragments that mark a schema or migration file (rule c)
FOLLOWUP_SCHEMA_PATTERNS = ("db/migration", "schema", "liquibase", "flyway")

# ---------------------------------------------------------------- confidence
#: Fraction of an answer's citations that may fail verification before the run
#: loses a confidence level. One bad citation in a fifteen-citation walkthrough
#: is a slip; a fifth of them failing means the answer was not written from the
#: evidence it claims.
VERIFY_DROP_RATIO_FOR_DOWNGRADE = 0.20
#: Confidence levels, weakest first. `confidence.py` indexes into this, so the
#: order is the definition of "one level down".
CONFIDENCE_ORDER = ("low", "medium", "high")

# ---------------------------------------------------------------- layout
#: project root = the directory containing backend/ and data/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_ROOT = PROJECT_ROOT / "data" / "index"
#: agent-run store (CLAUDE.md: SQLite is the graph *and* the app store)
RUNS_DB = PROJECT_ROOT / "data" / "runs.sqlite"
CONFIG_ROOT = PROJECT_ROOT / "config"
GRAPH_DB_NAME = "graph.db"
MANIFEST_NAME = "manifest.json"
DESCRIPTIONS_DB_NAME = "descriptions.sqlite"
VECTORS_DIR_NAME = "vectors"
VECTORS_TABLE_NAME = "chunks"

# ---------------------------------------------------------------- retention
# Phase G. Content-addressed rows are shared by every snapshot that ever
# contained them, so "is anything still using this?" is the only safe question
# to ask before deleting one. A snapshot is retained if ANY rule keeps it.

#: keep the most recent N snapshots on a repo's default branch
RETENTION_KEEP_LAST_N = 30
#: keep anything indexed within this many days, whatever else is true of it
RETENTION_KEEP_DAYS = 30
#: branch treated as "default" when git cannot be asked
RETENTION_DEFAULT_BRANCHES = ("main", "master")
#: reasons a snapshot survives, reported by the GC so a deletion is explicable
RETAIN_ENVIRONMENT = "an environment points at it"
RETAIN_TAGGED = "a git tag points at it"
RETAIN_RECENT_N = "among the last N on the default branch"
RETAIN_RECENT_DAYS = "indexed within the retention window"

# ---------------------------------------------------------------- vector store
#: rows fetched by the approximate pass before exact rescoring. 300 is well
#: above any final N the system asks for, so rescoring decides the answer and
#: the quantised pass only has to get the right chunks into the candidate set.
VECTOR_RESCORE_CANDIDATES = 300
#: int8 range used by the quantised search column
VECTOR_QUANTISE_LEVELS = 255
#: projects with fewer vectors than this share the default partition rather than
#: getting an HNSW index of their own — a per-partition index over 40 rows costs
#: more to maintain than the scan it replaces
VECTOR_MIN_PARTITION_ROWS = 1000

# ---------------------------------------------------------------- lazy migration
#: A bump here does NOT recompute anything. Descriptions and vectors are keyed by
#: (model, prompt_version, embed_model), so a bump simply means the new key is
#: absent; retrieval falls back to the previous version and `describe_backfill`
#: upgrades by priority. See db/versions.py.
DESCRIPTION_VERSIONS_PREFERRED: tuple[str, ...] = ()
#: how many chunks a shadow index samples per project
SHADOW_INDEX_SAMPLE = 200

# ---------------------------------------------------------------- workers
# Phase F. Indexing stops being something a person runs and becomes something
# that happens: a push arrives, a job is enqueued, a worker picks it up.

#: how long a worker sleeps when the queue is empty
WORKER_POLL_SECONDS = 2.0
#: a claimed index_commit whose repo lock is held elsewhere goes back after this
WORKER_REPO_LOCK_RETRY_SECONDS = 5.0
#: retry backoff is `base * 2**attempts`, capped
WORKER_RETRY_BASE_SECONDS = 10.0
WORKER_RETRY_MAX_SECONDS = 900.0
#: a job may fail this many times before it stops being retried and stays failed
WORKER_MAX_ATTEMPTS = 5
#: a `running` job whose worker vanished is reclaimed after this
WORKER_STALE_AFTER_MINUTES = 30

#: shared per-provider token bucket (work/limiter.py). Sized for a single
#: developer account rather than a production quota — the number that matters is
#: that it EXISTS, because eight workers retrying independently against one
#: account is how a rate limit becomes an outage.
WORKER_RATE_PER_SECOND = 2.0
WORKER_RATE_BURST = 10.0

#: describe_backfill priority = degree x recency x project traffic. The weights
#: are here rather than inline so the ordering can be argued with.
BACKFILL_WEIGHT_DEGREE = 1.0
BACKFILL_WEIGHT_RECENCY = 1.0
BACKFILL_WEIGHT_TRAFFIC = 1.0
#: chunks described per backfill job — bounded so one job cannot run for an hour
BACKFILL_BATCH = 50

#: retrieval may describe an undescribed T2 chunk inline, at most this many per
#: run. Bounded because a question should not turn into an indexing job.
LAZY_DESCRIBE_MAX = 3

#: GitHub webhook HMAC secret, read from .env like every other credential
GITHUB_WEBHOOK_SECRET_ENV = "BRIDGE_GITHUB_WEBHOOK_SECRET"
#: an index older than this many minutes shows a stale badge on /health/index
HEALTH_STALE_AFTER_MINUTES = 60

#: nightly verify / weekly gc, as cron expressions evaluated by the worker loop
CRON_VERIFY_NIGHTLY = "0 3 * * *"
CRON_GC_WEEKLY = "0 4 * * 0"


# ---------------------------------------------------------------- database
# Phase E: one Postgres holds every snapshot of every repo. Three ways to point
# at it, in precedence order (agents/db/engine.py resolves them):
#   1. BRIDGE_DATABASE_URL in the environment  — what a deployment sets
#   2. DATABASE_URL below                      — what docker-compose.yml serves
#   3. "embedded://"                           — a real PG 16.2 started from the
#      venv by `pgserver`, for a machine with no Docker. Same version, same
#      `vector` extension, same SQL; only the DSN differs.
DATABASE_URL_ENV = "BRIDGE_DATABASE_URL"
DATABASE_URL = "postgresql+psycopg://bridge:bridge@localhost:5433/bridge"
#: where `embedded://` puts its data directory
EMBEDDED_PG_ROOT = PROJECT_ROOT / "data" / "pg"


def provider_key_like(name: str) -> str | None:
    """Read a non-provider secret with the same environment-then-.env precedence.

    The GitHub webhook secret is not an LLM credential, but it is a credential
    and it should be found the same way — shell wins over the file, and a
    too-short value is a placeholder rather than a secret (see `provider_key`).
    """
    value = os.environ.get(name)
    if is_plausible_key(value):
        return value
    file_value = ENV_FILE_VALUES.get(name)
    return file_value if is_plausible_key(file_value) else None


def database_url() -> str:
    """The configured DSN. Environment wins, as with provider credentials."""
    return os.environ.get(DATABASE_URL_ENV) or DATABASE_URL


# ---------------------------------------------------------------- chunking
#: A method longer than this is split into overlapping sub-chunks, each carrying
#: its class + method signature as context so it still reads standalone. 120 is
#: roughly the point past which a single description stops being about one thing.
CHUNK_MAX_LINES = 120
CHUNK_OVERLAP_LINES = 15

# ---------------------------------------------------------------- tiering
# Not every chunk earns a model call. A getter and a POST handler cost the same
# today, which is the thing this fixes. index/tiers.py assigns:
TIER_TEMPLATE = "T0"   # description generated from the signature, no LLM
TIER_FILE = "T1"       # one file-level description, chunks inherit it
TIER_FULL = "T2"       # the model actually reads the code
TIERS = (TIER_TEMPLATE, TIER_FILE, TIER_FULL)

#: a body this short cannot carry behaviour worth a model call on its own
TIER_TRIVIAL_MAX_LINES = 6
#: below this many call-outs a method is doing very little
TIER_TRIVIAL_MAX_INVOCATIONS = 1
#: semantic in-degree at or above this pulls a chunk up to T2 regardless of size
TIER_HUB_IN_DEGREE = 8
#: name prefixes that are structurally accessors
TIER_ACCESSOR_PREFIXES = ("get", "set", "is", "has")
#: names that are always boilerplate
TIER_BOILERPLATE_NAMES = frozenset({"toString", "hashCode", "equals", "clone", "builder"})

# ---------------------------------------------------------------- index policy
#: per-repo exclusion policy, alongside vocab.<repo>.yaml (design law 7)
INDEX_POLICY_FILE_TEMPLATE = "index_policy.{repo}.yaml"
INDEX_POLICY_DEFAULT_FILE = "index_policy.yaml"
#: any file larger than this is excluded, whatever it is
INDEX_MAX_FILE_BYTES = 1_000_000
#: lines sniffed at the top of a file for a generated-code marker
INDEX_GENERATED_HEADER_LINES = 5
INDEX_GENERATED_MARKERS = (
    "@generated",
    "code generated by",
    "do not edit",
    "auto-generated",
    "autogenerated",
    "generated by the protocol buffer compiler",
)

# ---------------------------------------------------------------- embeddings
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIMENSIONS = 384
EMBED_BATCH_SIZE = 64
#: bge asks for this prefix on the query side only, never on the documents
EMBED_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# ---------------------------------------------------------------- descriptions
#: node kinds that get a plain-English description
DESCRIBE_KINDS = ("method", "class", "interface", "enum", "record", "screen")
DESCRIBE_CONCURRENCY = 8
#: characters of source code shown to the describer per chunk
DESCRIBE_MAX_SOURCE_CHARS = 4000

#: Characters of real source handed to an *agent* per chunk. Deliberately the
#: same number: the vector table stores each node's code already truncated to
#: DESCRIBE_MAX_SOURCE_CHARS, so a larger ceiling here would silently hand more
#: code to chunks read from disk than to chunks read from the index — the same
#: method would look different depending on which path found it.
AGENT_CODE_MAX_CHARS = DESCRIBE_MAX_SOURCE_CHARS

# ---------------------------------------------------------------- history
HISTORY_RECENT_DAYS = 90

# ---------------------------------------------------------------- retrieval
VOCAB_FILE = "vocab.petclinic.yaml"
RETRIEVE_SEMANTIC_K = 20
RETRIEVE_GRAPH_DEPTH = 3
RETRIEVE_GRAPH_CAP = 150
RETRIEVE_TOP_K = 15
#: rerank scores are 0-10; below these the answer says so instead of guessing.
#: These four are the *base* of the deterministic confidence score — everything
#: after them (orchestrator/confidence.py) can only lower the level, never raise it.
RETRIEVE_LOW_CONFIDENCE_BEST = 6.0
RETRIEVE_LOW_CONFIDENCE_TOP3_MEAN = 5.0
RETRIEVE_HIGH_CONFIDENCE_BEST = 8.0
RETRIEVE_HIGH_CONFIDENCE_TOP3_MEAN = 7.0

# ---------------------------------------------------------------- indexing
#: directories never walked when discovering source files
IGNORE_DIRS = frozenset({".git", "target", "build", "out", "node_modules", ".idea", ".mvn", ".gradle"})

#: path fragments that mark a file as test source (recorded as attrs.is_test)
TEST_PATH_MARKERS = ("src/test/", "/test/java/")

#: roots scanned for server-rendered templates, relative to the repo
TEMPLATE_ROOTS = ("src/main/resources/templates",)
TEMPLATE_EXTENSIONS = (".html", ".htm")

#: view-name prefixes that are control directives, not templates
VIEW_NAME_NON_TEMPLATE_PREFIXES = ("redirect:", "forward:")

# ---------------------------------------------------------------- graph
#: fraction of nodes flagged as hubs, ranked by semantic in-degree
HUB_PERCENTILE = 0.01

#: traversal defaults (graph.traverse)
TRAVERSE_MAX_DEPTH = 4
TRAVERSE_MAX_NODES = 300
TRAVERSE_STOP_AT_HUBS = True

# ---------------------------------------------------------------- spring
SPRING_CONTROLLER_ANNOTATIONS = frozenset({"Controller", "RestController"})

#: annotation name -> HTTP method it implies
SPRING_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,  # method comes from the `method=` element, else ANY
}

#: Spring Data marker interfaces; a repository is any interface transitively extending one
SPRING_REPOSITORY_BASES = frozenset(
    {
        "Repository",
        "CrudRepository",
        "ListCrudRepository",
        "PagingAndSortingRepository",
        "ListPagingAndSortingRepository",
        "JpaRepository",
        "JpaSpecificationExecutor",
        "QueryByExampleExecutor",
    }
)

#: JPA relationship annotations -> the inverse cardinality of that relationship
JPA_ASSOCIATION_INVERSE = {
    "OneToMany": "ManyToOne",
    "ManyToOne": "OneToMany",
    "ManyToMany": "ManyToMany",
    "OneToOne": "OneToOne",
}

#: collection types unwrapped to reach the associated entity type
JPA_COLLECTION_TYPES = frozenset(
    {"List", "Set", "Collection", "Iterable", "SortedSet", "LinkedHashSet", "ArrayList", "HashSet"}
)

#: Spring Data derived-query prefixes (order matters: longest match first)
SPRING_QUERY_PREFIXES = (
    "findDistinct",
    "readDistinct",
    "queryDistinct",
    "getDistinct",
    "searchDistinct",
    "streamDistinct",
    "countDistinct",
    "existsDistinct",
    "deleteDistinct",
    "removeDistinct",
    "find",
    "read",
    "query",
    "get",
    "search",
    "stream",
    "count",
    "exists",
    "delete",
    "remove",
)

# ---------------------------------------------------------------- io roles
# Whether a node READS the world or WRITES to it. Recorded on every node at
# index time (index/io_roles.py) so the orchestrator can ask a deterministic
# question — "does this answer cover both sides of the flow?" — without a model.

IO_READ = "read"
IO_WRITE = "write"
IO_BOTH = "both"
IO_NONE = "none"

#: HTTP methods that only read. HEAD and OPTIONS are here for completeness; a
#: repository indexed by Bridge is unlikely to declare them.
HTTP_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: HTTP methods that change state
HTTP_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

#: Spring Data method-name prefixes, split by what they do. `SPRING_QUERY_PREFIXES`
#: above mixes both because it exists to *strip* a prefix; these two exist to
#: classify one, so `delete` and `remove` sit on the write side where they belong.
REPOSITORY_READ_PREFIXES = (
    "findDistinct", "readDistinct", "queryDistinct", "getDistinct", "searchDistinct",
    "streamDistinct", "countDistinct", "existsDistinct",
    "find", "read", "query", "get", "search", "stream", "count", "exists",
)
REPOSITORY_WRITE_PREFIXES = (
    "deleteDistinct", "removeDistinct",
    "save", "delete", "remove", "insert", "update", "persist", "merge", "flush", "store",
)

#: Accessor naming. Weakest signal of the three, applied last.
ACCESSOR_READ_PREFIXES = ("get", "is", "has")
ACCESSOR_WRITE_PREFIXES = ("set", "add", "remove")

#: Which rule decided a node's io_role, recorded as `attrs.io_source`.
IO_SOURCE_HTTP = "http_method"
IO_SOURCE_ROUTE = "handles_route"
IO_SOURCE_REPOSITORY = "repository_method"
IO_SOURCE_TRANSACTIONAL = "transactional"
IO_SOURCE_ACCESSOR = "accessor"
IO_SOURCE_SCREEN = "screen"
IO_SOURCE_MEMBERS = "members"

#: The write-side sources that count as **persistence**. A setter writes to an
#: object; it does not save anything. `Owner#addPet` mutates a list in memory and
#: `setAllowedFields` configures a form binder — neither is evidence that an
#: answer examined the code that puts data in the database, which is the whole
#: question the bug rule asks. So `accessor` is deliberately absent here.
IO_PERSIST_SOURCES = frozenset(
    {IO_SOURCE_HTTP, IO_SOURCE_ROUTE, IO_SOURCE_REPOSITORY, IO_SOURCE_TRANSACTIONAL}
)

#: `@Transactional` marks a write unless it declares `readOnly = true` — which is
#: why the annotation's *arguments* are indexed, not just its name. Petclinic's
#: only two are `@Transactional(readOnly = true)` on VetRepository finders;
#: calling those writes would put a false write-side tick on a read-only answer.
TRANSACTIONAL_ANNOTATION = "Transactional"
TRANSACTIONAL_READ_ONLY_KEY = "readOnly"

#: derived-query name suffixes stripped when recovering the property being filtered on
SPRING_QUERY_KEYWORDS = (
    "IsStartingWith",
    "IsEndingWith",
    "IsContaining",
    "IsNotNull",
    "IsNotIn",
    "IsNull",
    "StartingWith",
    "EndingWith",
    "NotContaining",
    "Containing",
    "IgnoreCase",
    "OrderBy",
    "GreaterThanEqual",
    "LessThanEqual",
    "GreaterThan",
    "LessThan",
    "NotBetween",
    "Between",
    "NotLike",
    "Like",
    "NotIn",
    "In",
    "IsNot",
    "Not",
    "After",
    "Before",
    "True",
    "False",
    "Is",
    "Equals",
    "Asc",
    "Desc",
)

# ---------------------------------------------------------------- suggestions
#: written once at index time to data/index/<sha>/suggestions.json
SUGGESTIONS_FILE_NAME = "suggestions.json"
SUGGESTIONS_COUNT = 8
#: how many top endpoints/screens (each) are offered to the model as material —
#: more than SUGGESTIONS_COUNT so it has room to choose a varied set
SUGGESTIONS_CANDIDATE_POOL = 12

# ---------------------------------------------------------------- API
API_CORS_ORIGINS = ["http://localhost:5173"]
API_DEFAULT_HOST = "127.0.0.1"
API_DEFAULT_PORT = 8000
#: GET /health's `git rev-parse HEAD` staleness check — bounded so a hung or
#: unreachable repo path can't stall the endpoint indefinitely
HEALTH_CHECK_GIT_TIMEOUT_SECONDS = 5.0
#: GET /threads default page size — also RunStore.all_threads's default
THREADS_DEFAULT_LIMIT = 50

# ---------------------------------------------------------------- eval quality gate
# eval/rubric.py, eval/run_full.py, eval/gate.py — turning "does this still work"
# from a person reading a table into a number a CI step can compare.

#: The rubric's three dimensions, in the order eval/rubric.py asks for them and
#: eval/run_full.py reports them. Named here so both stay in lockstep with the
#: JSON schema in agents/prompts/judge.md — a dimension added to one and not
#: the other silently drops out of every score computed against it.
RUBRIC_DIMENSIONS = ("correctness", "completeness", "actionability")
RUBRIC_SCALE = (1, 5)

#: **Enforced in code, never left to the prompt** (design law 1: Python decides,
#: the model reasons inside the node). If the judge's own JSON lists ANY
#: unsupported claim, `eval/rubric.py::score_answer` overrides whatever
#: correctness number it returned down to this floor — a model that notices a
#: fabrication and still reports a 4 is not treated as an authority on its own
#: score. Not literally zero: the scale is 1-5, and 1 is its floor.
RUBRIC_UNSUPPORTED_FLOOR = 1

#: `eval/gate.py` fails a run against its baseline on any ONE of these —
#: matching the phase's own ask (core recall −5pts, rubric −0.3, any
#: determinism failure). Core-file recall is in **percentage points** (0-100);
#: the rubric mean is on the 1-5 scale rubric scores already use, so a −0.3
#: drop is roughly "one answer in three lost half a point," not a rounding
#: artifact.
GATE_CORE_RECALL_DROP_PTS = 5.0
GATE_RUBRIC_DROP = 0.3
#: baseline report `eval/gate.py` compares against, and writes on `--save-baseline`
GATE_BASELINE_FILE = "eval_baseline.json"

# ---------------------------------------------------------------- rolling quality job
# eval/rolling_quality.py — a weekly sample of REAL traffic (not the fixed
# smoke/full sets, which only ever exercise the questions someone thought to
# write down), rubric-scored and trended, so a quality regression nobody wrote
# a test for still shows up as a line moving the wrong way.

#: how many real runs the weekly job rubric-scores. Stratified by lane so one
#: popular lane cannot crowd out the sample; the ask also names "project" —
#: today's RunStore has exactly one project (spring-petclinic) indexed at a
#: time, so that half of the stratification is a no-op until multi-project
#: history exists, not a stubbed-out feature.
QUALITY_SAMPLE_SIZE = 20
#: a decline is "the weekly mean rubric score dropped for this many
#: consecutive weeks" — one bad week is noise; two in a row is a trend.
QUALITY_DRIFT_WEEKS = 2

# ---------------------------------------------------------------- health alerts
# bridge/work/alerts.py — turns `/health/index` from a page a human has to
# remember to check into pushes that land in Slack (or any webhook that speaks
# the same `{"text": ...}` shape) on their own schedule.

#: lag past this many commits, OR this many minutes since the last index, is
#: worth a page — independent thresholds because a repo with one commit a week
#: and one with a hundred a day need different readings of "behind"
#: (work/health.py already reasons about this the same way for `stale_reasons`).
ALERT_LAG_COMMITS = 20
ALERT_LAG_MINUTES = 24 * 60
#: a parse failure rate above this is treated as the parser silently losing
#: files, not as background noise
ALERT_PARSE_FAILURE_RATE = 0.01
#: a JUMP, not a level — an unresolved rate that has always been 8% is a fact
#: about the codebase; one that just moved from 8% to 14% is a fact about the
#: last commit. `alerts.py` compares against the last-checked rate it stored,
#: not against zero.
ALERT_UNRESOLVED_JUMP_PTS = 5.0
ALERT_QUEUE_DEPTH = 50
#: how the alert is delivered. Slack's incoming-webhook payload shape
#: (`{"text": ...}`) is treated as the lowest common denominator — this same
#: constant name works for a generic webhook receiver too, and no vendor SDK is
#: pulled in for one POST.
ALERT_WEBHOOK_URL_ENV = "BRIDGE_ALERT_WEBHOOK_URL"
#: how often the health-alert job is allowed to fire — hourly, not nightly:
#: a lagging index or a filling queue is worth knowing about same-day
CRON_HEALTH_ALERT = "0 * * * *"

# ---------------------------------------------------------------- quotas
# orchestrator/quotas.py, api/main.py's `/costs`. A "team" here is whatever
# string the caller sends (`RunRequest.team`, default "default") — there is no
# login system in Bridge yet, so this is the honest unit of attribution
# available today, not a stand-in for real multi-tenant auth.

DEFAULT_TEAM = "default"
#: past this, a run is refused BEFORE it starts (429) — known and enforceable
#: pre-run, because the team accompanies the request
QUOTA_TEAM_DAILY_USD_HARD = 20.0
#: past this, the run proceeds but its header carries a warning — a soft quota
#: exists to be seen, not to block someone mid-investigation
QUOTA_TEAM_DAILY_USD_SOFT = 10.0
#: per-lane daily caps, checked only once the lane is known (design law 3: the
#: lane comes from analysis, not from the request) — enforced by tightening
#: THIS run's own budget.max_usd once `state.lane` is set, not by refusing the
#: request up front. Absent lanes have no cap.
QUOTA_LANE_DAILY_USD: dict[str, float] = {
    "change_request": 8.0,  # the architect is the most expensive node in the system
}

# ---------------------------------------------------------------- semantic answer cache
# orchestrator/cache.py. The exact-match cache (Phase D) only ever fires on the
# identical normalised question. Two PMs asking "how does search work" and
# "how does searching work" get two full runs today; this closes that gap.

#: cosine similarity a near-duplicate question must clear to serve a prior
#: run's answer. High on purpose — a near-miss cache hit that answers the
#: wrong question is worse than a cache miss, because a miss is visibly a full
#: run and a bad hit looks exactly like a good one.
SEMANTIC_CACHE_THRESHOLD = 0.95
#: scoped to (snapshot, lane) — a `bug` answer and a `question` answer to
#: similarly-worded asks are not interchangeable, and a stale snapshot's
#: answer must never surface for a fresh one.
SEMANTIC_CACHE_ENABLED = True

# ---------------------------------------------------------------- ACLs
# security/acl.py. `config/acl.<repo>.yaml` mirrors repo read permission from
# the git host — mirrors, in the sense of holding the same shape a real
# integration would populate (principal -> repos), not in the sense of calling
# out to one; see security/acl.py's `GitHostACLProvider` interface for exactly
# what a live GitHub/GitLab integration would have to implement to replace it.
ACL_CONFIG_GLOB = "acl.*.yaml"
#: a repo with no acl.<repo>.yaml at all is open to every principal — an
#: explicit ACL file is what NARROWS access, matching how `config/vocab.*.yaml`
#: being absent just means no vocabulary rather than no access to the repo
ACL_DEFAULT_OPEN = True

# ---------------------------------------------------------------- vocab staleness
# work/vocab_staleness.py — a nightly pass over config/vocab.<repo>.yaml
# checking that every node id a term names still exists in the current graph.

#: replacement candidates are matched by (kind, bare name) elsewhere in the
#: CURRENT graph — a same-name heuristic, not a content-hash-verified rename
#: trace. The real trace (code_hash survives a rename) is possible once
#: retrieval is cut over to the Postgres content-addressed index (Phase E/F/G);
#: today's SQLite graph node ids carry no hash history to walk.
CRON_VOCAB_STALENESS = "0 2 * * *"
