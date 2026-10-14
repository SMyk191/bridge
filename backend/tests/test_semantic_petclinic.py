"""Phase-2 assertions: redaction, semantic search, vocabulary, and retrieval.

These reuse the index in ``data/index/<sha>/`` rather than rebuilding it — the
embedding pass takes ~30s and the graph tests already cover construction. Run
``bridge index ./data/repos/spring-petclinic`` first.

Nothing here requires API credentials: the reranker degrades to `confidence:
low`, which is itself asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge import config
from bridge.agents.llm import Budget, LLMClient, Usage, cost_of, price_for
from bridge.index.embed import VectorIndex
from bridge.redact import redact, RedactionLog
from bridge.retrieval.retrieve import Retriever
from bridge.retrieval.vocab import Vocabulary

PKG = "org.springframework.samples.petclinic"


def _latest_index() -> Path | None:
    candidates = [
        path
        for path in config.INDEX_ROOT.glob("*")
        if (path / config.GRAPH_DB_NAME).is_file()
    ]
    return max(candidates, key=lambda p: (p / config.GRAPH_DB_NAME).stat().st_mtime, default=None)


@pytest.fixture(scope="session")
def index_dir() -> Path:
    path = _latest_index()
    if path is None:
        pytest.skip(
            "no index found — run: cd backend && "
            ".venv/bin/python -m bridge.index.build ../data/repos/spring-petclinic"
        )
    return path


@pytest.fixture(scope="session")
def vectors(index_dir: Path) -> VectorIndex:
    vectors_dir = index_dir / config.VECTORS_DIR_NAME
    if not VectorIndex.exists(vectors_dir):
        pytest.skip("no vector table — re-run the index without --no-embed")
    return VectorIndex.open(vectors_dir)


@pytest.fixture(scope="session")
def retriever(index_dir: Path):
    with Retriever.open(index_dir) as opened:
        yield opened


# ------------------------------------------------------------------ redaction (required)


def test_redaction_catches_a_fake_aws_key():
    text = 'String creds = "AKIAIOSFODNN7EXAMPLE"; // rotate me'
    result = redact(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert result.counts["aws_access_key_id"] == 1
    assert result.secrets == 1


def test_redaction_catches_an_indian_phone_number():
    text = "Owner telephone 9876543210, alt +91 9123456780"
    result = redact(text)
    assert "9876543210" not in result.text
    assert "9123456780" not in result.text
    assert result.counts["indian_phone"] == 2
    assert result.pii == 2


def test_redaction_covers_every_declared_rule():
    cases = {
        "private_key_block": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.s1gnatur3",
        "aws_secret_access_key": "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "assigned_secret": 'password = "hunter2000"',
        "email": "reach me at jane.doe@example.com",
        "aadhaar_like": "aadhaar 123456789012",
    }
    for label, text in cases.items():
        result = redact(text)
        assert label in result.counts, f"{label} rule did not fire on {text!r}"


def test_redaction_leaves_ordinary_petclinic_code_alone():
    """A false positive costs readability on every chunk — check the real strings."""
    clean = [
        '@Pattern(regexp = "\\\\d{10}", message = "{telephone.invalid}")',
        'private static final String VIEWS_OWNER_CREATE_OR_UPDATE_FORM = "owners/createOrUpdateOwnerForm";',
        'model.addAttribute("listOwners", listOwners);',
        "public Page<Owner> findByLastNameStartingWith(String lastName, Pageable pageable);",
    ]
    for text in clean:
        assert redact(text).counts == {}, f"false positive on {text!r}"


def test_redaction_log_reports_what_it_scrubbed():
    log = RedactionLog()
    log.scrub("nothing here")
    log.scrub('token = "abcdef123456"')
    assert log.chunks_scanned == 2
    assert log.chunks_redacted == 1
    assert log.total == 1
    assert "assigned_secret" in log.summary()


# ------------------------------------------------------------------ semantic search (required)


def test_semantic_search_finds_the_add_a_pet_path(vectors: VectorIndex):
    """The spec's probe: an owner-adds-a-pet question must surface that flow."""
    hits = vectors.semantic_search("how does an owner add a pet", k=5)
    assert hits, "semantic search returned nothing"

    top5 = [hit.node_id for hit in hits]
    relevant = [
        node_id
        for node_id in top5
        if "OwnerController" in node_id
        or "PetController" in node_id
        or node_id.endswith("Owner#addPet")
        or node_id.endswith("Pet")
    ]
    assert relevant, (
        "no owner/pet-flow chunk in the top 5; got:\n  " + "\n  ".join(top5)
    )
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_semantic_search_excludes_tests_by_default(vectors: VectorIndex):
    hits = vectors.semantic_search("owner controller form handling", k=10)
    assert not any("Tests" in hit.node_id for hit in hits)


def test_semantic_search_respects_k(vectors: VectorIndex):
    assert len(vectors.semantic_search("pet", k=3)) <= 3


# ------------------------------------------------------------------ vocabulary


def test_vocabulary_loads_the_configured_terms():
    vocabulary = Vocabulary.load()
    assert len(vocabulary) >= 6, "vocab.petclinic.yaml should define 6-8 terms"
    assert vocabulary.repo == "spring-petclinic"


def test_vocabulary_maps_product_words_to_code():
    """`appointment` never appears in petclinic — only the vocabulary bridges it."""
    vocabulary = Vocabulary.load()
    expansion = vocabulary.expand_query("can a customer book an appointment?")
    assert "visit" in expansion.matched_terms
    assert f"entity:{PKG}.owner.Visit" in expansion.node_ids
    assert "appointment" in expansion.expanded
    assert expansion.expanded != expansion.original


def test_vocabulary_alias_matching_is_word_bounded():
    vocabulary = Vocabulary.load()
    assert not vocabulary.expand_query("the visitor logged in").matched_terms


def test_vocabulary_extracts_identifiers_from_a_question():
    vocabulary = Vocabulary.load()
    expansion = vocabulary.expand_query("what calls OwnerRepository.findById in /owners/{ownerId}?")
    assert any("OwnerRepository" in token for token in expansion.identifiers)
    assert any(token.startswith("/owners") for token in expansion.identifiers)


# ------------------------------------------------------------------ retrieval (required)


def test_retrieve_returns_citations_and_a_confidence(retriever):
    result = retriever.retrieve("how does an owner add a pet")

    assert result.chunks, "retrieve returned no chunks"
    assert len(result.citations) >= 1
    assert result.confidence in {"high", "medium", "low"}
    assert result.confidence_reason

    citation = result.chunks[0].citation
    assert citation.startswith(f"{result.repo}@{result.sha}:")
    assert ".java" in citation or ".html" in citation
    # repo@sha:path:start-end
    assert citation.rsplit(":", 1)[-1].count("-") == 1


def test_retrieve_runs_every_stage(retriever):
    result = retriever.retrieve("where do we store vet specialties?")
    for stage in ("vocab", "semantic", "keyword", "graph", "candidates"):
        assert stage in result.stage_counts
    assert result.stage_counts["vocab"] > 0, "vocabulary should have matched 'vet specialties'"
    assert result.stage_counts["semantic"] > 0
    assert result.stage_counts["graph"] > 0


def test_retrieve_respects_the_graph_cap(retriever):
    result = retriever.retrieve("owner")
    assert result.stage_counts["graph"] <= config.RETRIEVE_GRAPH_CAP
    assert len(result.chunks) <= config.RETRIEVE_TOP_K


def test_retrieve_attaches_history_and_graph_paths(retriever):
    result = retriever.retrieve("how does an owner add a pet")
    assert any(chunk.history for chunk in result.chunks), "no chunk carried file history"
    history = next(chunk.history for chunk in result.chunks if chunk.history)
    assert "commits_all_time" in history and "module" in history
    assert history["recent_window_days"] == config.HISTORY_RECENT_DAYS
    assert any(chunk.graph_path for chunk in result.chunks)


def test_history_never_names_a_person(retriever):
    """No author names or emails anywhere in what retrieval hands back."""
    result = retriever.retrieve("who changed the owner form recently?")
    for chunk in result.chunks:
        if not chunk.history:
            continue
        assert set(chunk.history) <= {
            "commits_all_time",
            "commits_recent",
            "recent_window_days",
            "last_changed",
            "days_since_change",
            "module",
        }, f"unexpected history field: {sorted(chunk.history)}"


def test_retrieve_abstains_when_it_cannot_rerank(retriever):
    """Design law 4 — weak evidence must say so rather than look confident."""
    result = retriever.retrieve("how does an owner add a pet")
    if not result.reranked:
        assert result.confidence == "low"
        assert result.abstained
        assert result.rerank_unavailable_reason


def test_retrieve_on_an_unrelated_question_is_not_confident(retriever):
    result = retriever.retrieve("how do we process cryptocurrency refunds via SWIFT?")
    assert result.confidence in {"low", "medium"}


# ------------------------------------------------------------------ llm accounting


def test_roles_resolve_to_the_configured_provider_and_model():
    from bridge.agents.llm import resolve_role

    for role, expected_provider in config.ROLE_PROVIDER.items():
        provider, spec = resolve_role(role)
        assert provider == expected_provider
        assert spec["model"] == config.ROLE_MODELS[expected_provider][role]["model"]


def test_every_role_is_defined_under_every_provider():
    """ROLE_PROVIDER is the switch; flipping it must never land on an undefined model."""
    providers = set(config.ROLE_MODELS)
    for role in config.ROLE_PROVIDER:
        for provider in providers:
            assert role in config.ROLE_MODELS[provider], (
                f"role {role!r} has no model under provider {provider!r}; "
                "switching ROLE_PROVIDER would break"
            )
            assert config.ROLE_MODELS[provider][role].get("model")


def test_switching_a_roles_provider_changes_the_resolved_model(monkeypatch):
    """The Anthropic path stays live — proven by switching to it and back."""
    from bridge.agents.llm import resolve_role

    monkeypatch.setitem(config.ROLE_PROVIDER, "describe", config.PROVIDER_ANTHROPIC)
    provider, spec = resolve_role("describe")
    assert provider == config.PROVIDER_ANTHROPIC
    assert spec["model"] == config.MODEL_CHEAP

    monkeypatch.setitem(config.ROLE_PROVIDER, "describe", config.PROVIDER_GEMINI)
    provider, spec = resolve_role("describe")
    assert provider == config.PROVIDER_GEMINI
    assert spec["model"] == config.GEMINI_FLASH_LITE


def test_both_providers_are_constructed_and_checked_up_front():
    """Availability is decided at construction for every provider, not at call time."""
    from bridge.agents.llm import PROVIDERS

    client = LLMClient()
    status = client.status()
    assert set(status) == set(PROVIDERS) == {config.PROVIDER_ANTHROPIC, config.PROVIDER_GEMINI}
    for provider_name, state in status.items():
        assert state, f"{provider_name} reported no availability state"


def test_effort_is_dropped_for_models_that_reject_it():
    """`output_config.effort` 400s on Haiku 4.5 — the client must not send it."""
    assert config.MODEL_CHEAP not in config.MODELS_SUPPORTING_EFFORT
    assert config.MODEL_DEEP in config.MODELS_SUPPORTING_EFFORT


def test_always_thinking_models_are_never_sent_a_zero_budget():
    """The 3.x tiers reject `thinking_budget=0` with a 400 — verified against the API.

    So those roles must instead carry enough `max_tokens` for reasoning *and* the
    answer, or thinking eats the ceiling and the call returns empty text.
    """
    gemini_roles = config.ROLE_MODELS[config.PROVIDER_GEMINI]
    for role, spec in gemini_roles.items():
        if spec["model"] in config.GEMINI_THINKING_ALWAYS_ON:
            assert "thinking_budget" not in spec, (
                f"role {role!r} sends thinking_budget to {spec['model']}, which rejects it"
            )
            assert spec["max_tokens"] >= 2000, (
                f"role {role!r} caps output at {spec['max_tokens']} on a model that always "
                "thinks; reasoning can consume the whole budget and return no text"
            )


def test_gated_models_are_not_routed_to():
    """2.5 flash-lite and pro 404 on this key; roles must point at the successors."""
    gemini_roles = config.ROLE_MODELS[config.PROVIDER_GEMINI]
    gated = {config.GEMINI_FLASH_LITE_25, config.GEMINI_PRO_25}
    for role, spec in gemini_roles.items():
        assert spec["model"] not in gated, f"role {role!r} points at gated {spec['model']}"


def test_every_routed_model_has_a_price():
    """A model with no price silently costs $0.00 and breaks budget enforcement."""
    for provider, roles in config.ROLE_MODELS.items():
        for role, spec in roles.items():
            model = spec["model"]
            priced = model in config.GEMINI_PRICING or model in config.MODEL_PRICING
            assert priced, f"{provider}:{role} uses {model}, which has no pricing entry"


def test_cost_accounting_matches_the_price_table():
    from datetime import date

    cost = cost_of("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(6.00)  # $1 in + $5 out

    intro = price_for("claude-sonnet-5", on=date(2026, 8, 19))
    after = price_for("claude-sonnet-5", on=date(2026, 9, 1))
    assert intro == config.SONNET_5_INTRO_PRICING
    assert after == config.MODEL_PRICING["claude-sonnet-5"]


def test_gemini_cost_accounting():
    """Prices are per-model, taken from ai.google.dev/gemini-api/docs/pricing."""
    # gemini-3.5-flash-lite: $0.30 in / $2.50 out
    assert cost_of("gemini-3.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(2.80)
    # gemini-2.5-flash: $0.30 in / $2.50 out
    assert cost_of("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(2.80)
    # gemini-3.1-pro-preview under the threshold: $2.00 in / $12.00 out
    assert cost_of("gemini-3.1-pro-preview", 100_000, 1_000_000) == pytest.approx(12.20)
    # the gated 2.5 tiers keep their own prices for a switch back
    assert cost_of("gemini-2.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(0.50)


def test_gemini_long_context_tier_applies_above_the_threshold():
    """2.5-pro doubles its input rate and raises output above 200k prompt tokens."""
    below = price_for(config.GEMINI_PRO, input_tokens=config.GEMINI_LONG_CONTEXT_THRESHOLD)
    above = price_for(config.GEMINI_PRO, input_tokens=config.GEMINI_LONG_CONTEXT_THRESHOLD + 1)
    assert below == config.GEMINI_PRICING[config.GEMINI_PRO]
    assert above == config.GEMINI_PRICING_LONG_CONTEXT[config.GEMINI_PRO]
    # flash has no tiering — same price at any prompt size
    assert price_for(config.GEMINI_FLASH, input_tokens=5_000_000) == config.GEMINI_PRICING[
        config.GEMINI_FLASH
    ]


def test_gemini_thinking_tokens_bill_at_the_output_rate():
    """Google bills thinking as output. Counting it free would understate cost ~4x."""
    from bridge.agents.llm import ProviderResponse

    # a response that spent 900 tokens thinking and 100 answering
    answer_only = cost_of(config.GEMINI_FLASH_LITE, 1000, 100)
    with_thinking = cost_of(config.GEMINI_FLASH_LITE, 1000, 100 + 900)
    assert with_thinking > answer_only
    assert with_thinking == pytest.approx(cost_of(config.GEMINI_FLASH_LITE, 1000, 1000))

    # the provider folds thoughts into output_tokens, and reports them separately
    response = ProviderResponse(
        text="x", model=config.GEMINI_FLASH_LITE, input_tokens=1000,
        output_tokens=1000, thinking_tokens=900,
    )
    assert response.output_tokens == 1000
    assert response.thinking_tokens == 900


def test_anthropic_path_is_still_wired_and_unchanged():
    """Switching back must not require code changes — the provider stays registered."""
    from bridge.agents.llm import PROVIDERS, AnthropicProvider

    assert PROVIDERS[config.PROVIDER_ANTHROPIC] is AnthropicProvider
    anthropic_roles = config.ROLE_MODELS[config.PROVIDER_ANTHROPIC]
    assert anthropic_roles["describe"]["model"] == config.MODEL_CHEAP
    assert anthropic_roles["rerank"]["model"] == config.MODEL_CHEAP
    assert anthropic_roles["architect"]["model"] == config.MODEL_DEEP
    assert anthropic_roles["architect"]["effort"] == config.MODEL_DEEP_EFFORT


def test_budget_stops_a_run_before_it_overspends():
    from bridge.agents.llm import BudgetExceeded

    budget = Budget(max_usd=0.01, max_calls=None)
    usage = Usage(calls=3, cost_usd=0.02)
    with pytest.raises(BudgetExceeded):
        budget.check(usage)
    assert budget.remaining_usd(usage) == 0.0


def test_llm_client_reports_unavailability_instead_of_pretending():
    client = LLMClient(enabled=False)
    assert not client.available
    assert client.unavailable_reason
    with pytest.raises(RuntimeError):
        client.complete("describe", "hello")


# ------------------------------------------------------------------ descriptions store


def test_descriptions_are_cached_by_content_hash(index_dir: Path):
    from bridge.index.describe import DescriptionStore, content_key

    path = index_dir / config.DESCRIPTIONS_DB_NAME
    if not path.is_file():
        pytest.skip("no descriptions store — re-run the index without --no-describe")
    with DescriptionStore.open(path) as store:
        assert content_key("abc") == content_key("abc")
        assert content_key("abc") != content_key("abd")


def test_cache_key_covers_model_and_prompt_not_just_code():
    """Editing the prompt or switching models must invalidate, or edits do nothing."""
    from bridge.index.describe import content_key

    base = content_key("chunk", "claude-haiku-4-5", "prompt v1")
    assert base != content_key("chunk", "claude-sonnet-5", "prompt v1"), "model not in key"
    assert base != content_key("chunk", "claude-haiku-4-5", "prompt v2"), "prompt not in key"
    assert base != content_key("other", "claude-haiku-4-5", "prompt v1"), "text not in key"
    assert base == content_key("chunk", "claude-haiku-4-5", "prompt v1")


def test_fallback_descriptions_are_never_persisted(tmp_path):
    """A degraded run must not poison the cache for a later, working one.

    The cache exists to avoid re-paying for model output. A deterministic stub
    costs nothing to recompute, so storing one only creates a trap: the next run
    — with credentials — would serve the stub from cache and never call the model.
    """
    from bridge.index.describe import DescriptionStore

    store_path = tmp_path / "descriptions.sqlite"
    with DescriptionStore.open(store_path) as store:
        store.put("key-model", "java:A#m", "method", "A real description.", "claude-haiku-4-5",
                  "claude-haiku-4-5")
        # simulate a legacy/degraded row written by an older build
        store.put("key-fallback", "java:B#m", "method", "The m step of B.", "fallback")

        assert store.get("key-model") is not None
        assert store.get("key-fallback") is None, "a fallback row was served as a cache hit"
        assert "java:B#m" not in store.all_descriptions()
        assert "java:A#m" in store.all_descriptions()


def test_live_cache_holds_no_fallback_rows(index_dir: Path):
    """The store on disk must contain model output only."""
    import sqlite3

    path = index_dir / config.DESCRIPTIONS_DB_NAME
    if not path.is_file():
        pytest.skip("no descriptions store")
    connection = sqlite3.connect(path)
    stored = connection.execute(
        "SELECT COUNT(*) FROM descriptions WHERE source = 'fallback'"
    ).fetchone()[0]
    connection.close()
    assert stored == 0, f"{stored} fallback rows persisted; they would block regeneration"


def test_embedded_text_contains_the_description_not_just_code(vectors: VectorIndex):
    """What is embedded is breadcrumb + description — the description must be in it."""
    rows = vectors.table.to_arrow().to_pylist()
    checked = 0
    for row in rows:
        if not row["description"].strip():
            continue
        assert row["description"] in row["text"], (
            f"embedded text for {row['node_id']} does not contain its description"
        )
        assert row["breadcrumb"] in row["text"]
        checked += 1
    assert checked > 0


def test_vector_table_is_rebuilt_not_incrementally_cached():
    """There is no embedding cache: the table is overwritten every run.

    That is what guarantees vectors can never lag their descriptions — pinned
    here so a future 'optimisation' that adds an incremental path has to
    consciously replace this invariant.
    """
    from pathlib import Path as _Path

    source = (_Path(__file__).parent.parent / "bridge" / "index" / "embed.py").read_text()
    assert 'mode="overwrite"' in source


def test_every_embedded_chunk_has_a_description(vectors: VectorIndex):
    rows = vectors.table.to_arrow().to_pylist()
    assert rows
    assert all(row["description"].strip() for row in rows)
    assert all(len(row["vector"]) == config.EMBED_DIMENSIONS for row in rows)
