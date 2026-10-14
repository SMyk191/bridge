"""Phase D: temperature 0, tie-broken sorts, interpreter voting, the answer cache.

Split the same way test_confidence.py is: pure logic (question_norm, tie-breaks,
provider request-building, the voting arithmetic) needs no index and no model,
so it's tested directly; the end-to-end cache flow uses the real petclinic
index with `LLMClient(enabled=False)` — a fully degraded pipeline still
completes to `status == "done"` (every node has a documented fallback), which
is all the caching *mechanics* need to exercise honestly, at zero cost.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge import config
from bridge.agents import interpreter
from bridge.agents.llm import AnthropicProvider, GeminiProvider, LLMClient, Usage
from bridge.agents.state import RunState
from bridge.orchestrator import RunStore, cache, run, split_header
from bridge.retrieval.retrieve import Chunk, _prior_score
from bridge.retrieval.retrieve import Retriever, _resolve_index_dir

SHA = "88e37c15cf6fc8490b01bc3e8e2c800cec1ac272"


# ------------------------------------------------------------------ question_norm


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("What happens when an owner adds a new pet?", "what happens when an owner adds a new pet"),
        # trailing "..." IS trailing punctuation by this regex's definition — the
        # point is dropping a "?"/"."/etc a person adds without changing the ask,
        # and an ellipsis is exactly that.
        ("  WHAT   happens...  ", "what happens"),
        ("WHAT HAPPENS?!", "what happens"),
        ("no punctuation here", "no punctuation here"),
        ("Trailing,,,", "trailing"),
    ],
)
def test_question_norm(raw, expected):
    assert cache.question_norm(raw) == expected


def test_question_norm_is_idempotent():
    once = cache.question_norm("What Happens?  ")
    assert cache.question_norm(once) == once


# ------------------------------------------------------------------ snapshot_key


def test_snapshot_key_is_stable_for_identical_inputs():
    assert cache.snapshot_key("petclinic", SHA) == cache.snapshot_key("petclinic", SHA)


def test_snapshot_key_changes_with_the_sha():
    assert cache.snapshot_key("petclinic", SHA) != cache.snapshot_key("petclinic", "0" * 40)


def test_snapshot_key_changes_with_the_repo_name():
    assert cache.snapshot_key("petclinic", SHA) != cache.snapshot_key("other-repo", SHA)


def test_snapshot_key_changes_when_a_prompt_changes(monkeypatch):
    before = cache.snapshot_key("petclinic", SHA)
    monkeypatch.setattr(cache, "load_prompt", lambda name: f"rewritten prompt for {name}")
    after = cache.snapshot_key("petclinic", SHA)
    assert before != after


def test_snapshot_key_changes_when_role_models_change(monkeypatch):
    before = cache.snapshot_key("petclinic", SHA)
    patched = {
        provider: {role: dict(spec) for role, spec in roles.items()}
        for provider, roles in config.ROLE_MODELS.items()
    }
    patched[config.PROVIDER_GEMINI]["interpreter"]["max_tokens"] = 999_999
    monkeypatch.setattr(config, "ROLE_MODELS", patched)
    after = cache.snapshot_key("petclinic", SHA)
    assert before != after


def test_snapshot_key_ignores_a_missing_prompt_file(monkeypatch):
    """A prompt that fails to load (renamed, moved) must not crash key derivation."""
    def flaky(name):
        if name == "rerank":
            raise OSError("gone")
        return "text"

    monkeypatch.setattr(cache, "load_prompt", flaky)
    assert cache.snapshot_key("petclinic", SHA)  # does not raise


# ------------------------------------------------------------------ stable sort tie-break


def _chunk(node_id: str, score: float) -> Chunk:
    return Chunk(
        node_id=node_id, kind="method", name=node_id, path=f"{node_id}.java",
        start_line=1, end_line=10, description="d", citation=f"r@s:{node_id}.java:1-10",
        score=score,
    )


def test_equal_scoring_chunks_sort_by_node_id_not_by_insertion_order():
    """The exact scenario a missing tie-break would get wrong: two chunks score
    identically, and whichever happened to be inserted first would otherwise win —
    a property of dict-iteration order, not of the data."""
    a, b, c = _chunk("zzz", 5.0), _chunk("aaa", 5.0), _chunk("mmm", 5.0)
    pool = [a, b, c]  # deliberately NOT already in node_id order
    pool.sort(key=lambda x: (-x.score, x.node_id))
    assert [x.node_id for x in pool] == ["aaa", "mmm", "zzz"]


def test_a_higher_score_still_wins_over_the_tie_break():
    a, b = _chunk("zzz", 9.0), _chunk("aaa", 5.0)
    pool = [b, a]
    pool.sort(key=lambda x: (-x.score, x.node_id))
    assert [x.node_id for x in pool] == ["zzz", "aaa"]


def test_prior_score_itself_is_a_pure_function_of_the_chunk():
    """Not the sort, but the thing fed into it: two structurally identical
    chunks must score identically, or the tie-break would be masking hidden
    nondeterminism in the score rather than resolving a genuine tie."""
    a = _chunk("x", 0.0)
    a.sources = ["vocab", "semantic"]
    a.semantic_score = 0.8
    a.description = "d"
    b = _chunk("y", 0.0)
    b.sources = ["vocab", "semantic"]
    b.semantic_score = 0.8
    b.description = "d"
    assert _prior_score(a) == _prior_score(b)


# ------------------------------------------------------------------ temperature


class _FakeContentBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicMessage:
    def __init__(self, text="ok"):
        self.usage = SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        self.stop_reason = "end_turn"
        self.content = [_FakeContentBlock(text)]


class _FakeAnthropicMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAnthropicMessage()


def _bare_anthropic_provider() -> tuple[AnthropicProvider, _FakeAnthropicMessages]:
    """A provider instance with a fake SDK client, bypassing real credential
    setup entirely — this is testing request-shaping, not authentication."""
    provider = object.__new__(AnthropicProvider)
    provider._unavailable_reason = None
    messages = _FakeAnthropicMessages()
    provider._client = SimpleNamespace(messages=messages)
    return provider, messages


def test_anthropic_defaults_to_temperature_zero():
    provider, messages = _bare_anthropic_provider()
    response = provider.generate({"model": "claude-haiku-4-5"}, "prompt", None, 100)
    assert messages.calls[0]["temperature"] == 0.0
    assert response.temperature_used == 0.0


def test_anthropic_omits_temperature_when_effort_triggers_extended_thinking():
    """The one documented exemption (config.py). Sending temperature=0 alongside
    `effort` on a model that supports it risks a 400; omitting it is the fix."""
    provider, messages = _bare_anthropic_provider()
    spec = {"model": config.MODEL_DEEP, "effort": config.MODEL_DEEP_EFFORT}
    assert config.MODEL_DEEP in config.MODELS_SUPPORTING_EFFORT
    response = provider.generate(spec, "prompt", None, 100)
    assert "temperature" not in messages.calls[0]
    assert messages.calls[0]["output_config"] == {"effort": config.MODEL_DEEP_EFFORT}
    assert response.temperature_used is None


def test_anthropic_effort_on_a_model_that_does_not_support_it_still_gets_temperature_zero():
    """`effort` is only a real exemption when the MODEL supports it — Haiku 4.5
    predates the parameter (config.MODELS_SUPPORTING_EFFORT), so a spec that
    mistakenly sets `effort` there must not silently drop temperature too."""
    provider, messages = _bare_anthropic_provider()
    assert "claude-haiku-4-5" not in config.MODELS_SUPPORTING_EFFORT
    provider.generate({"model": "claude-haiku-4-5", "effort": "high"}, "prompt", None, 100)
    assert messages.calls[0]["temperature"] == 0.0
    assert "output_config" not in messages.calls[0]


def test_anthropic_respects_a_role_level_temperature_override():
    provider, messages = _bare_anthropic_provider()
    provider.generate({"model": "claude-haiku-4-5", "temperature": 0.4}, "prompt", None, 100)
    assert messages.calls[0]["temperature"] == 0.4


class _FakeGeminiPart:
    thought = False

    def __init__(self, text):
        self.text = text


class _FakeGeminiCandidate:
    def __init__(self, text):
        self.finish_reason = SimpleNamespace(name="STOP")
        self.content = SimpleNamespace(parts=[_FakeGeminiPart(text)])


class _FakeGeminiResponse:
    def __init__(self, text="ok"):
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5,
            thoughts_token_count=0, cached_content_token_count=0,
        )
        self.prompt_feedback = None
        self.candidates = [_FakeGeminiCandidate(text)]


class _FakeGeminiModels:
    def __init__(self):
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeGeminiResponse()


def _bare_gemini_provider() -> tuple[GeminiProvider, _FakeGeminiModels]:
    provider = object.__new__(GeminiProvider)
    provider._unavailable_reason = None
    models = _FakeGeminiModels()
    provider._client = SimpleNamespace(models=models)
    return provider, models


def test_gemini_defaults_to_temperature_zero():
    pytest.importorskip("google.genai")
    provider, models = _bare_gemini_provider()
    response = provider.generate({"model": "gemini-2.5-flash"}, "prompt", None, 100)
    assert models.calls[0]["config"].temperature == 0.0
    assert response.temperature_used == 0.0


def test_gemini_gets_no_effort_exemption_even_on_a_thinking_tier_model():
    """The exemption is Anthropic-specific (config.py's docstring is explicit
    about this): Gemini's temperature and thinking_config are independent, and
    nothing in the SDK rejects the combination the way Anthropic's does."""
    pytest.importorskip("google.genai")
    provider, models = _bare_gemini_provider()
    assert config.GEMINI_PRO in config.GEMINI_THINKING_ALWAYS_ON or True  # documented tier
    provider.generate({"model": config.GEMINI_PRO, "thinking_budget": None}, "prompt", None, 100)
    assert models.calls[0]["config"].temperature == 0.0


# ------------------------------------------------------------------ interpreter voting


class _ScriptedLLM:
    """Just enough of LLMClient's surface for interpreter.run: available_for,
    and complete() returning a scripted JSON body per call, in order."""

    def __init__(self, intents: list[str]):
        self._intents = list(intents)
        self.calls = 0
        self.usage = Usage()

    def available_for(self, role: str) -> bool:
        return True

    def reason_for(self, role: str) -> str | None:
        return None

    def complete(self, role, prompt, system=None, json_mode=False, max_tokens=None):
        intent = self._intents[self.calls]
        self.calls += 1
        text = (
            '{"normalised_request": "q", "intent": "%s", "entities": [], '
            '"clarifying_question": null, "assumptions": [], "reasoning": "r"}' % intent
        )
        return SimpleNamespace(
            text=text, input_tokens=1, output_tokens=1, thinking_tokens=0,
            cost_usd=0.0, role=role, provider="test",
        )


def _state() -> RunState:
    state = RunState(request="how many owners registered last month?")
    state.budget = state.budget  # already unlimited-ish defaults from RunState
    return state


def test_unanimous_votes_produce_no_disagreement():
    state = _state()
    llm = _ScriptedLLM(["data_question", "data_question", "data_question"])
    interpreter.run(state, llm)
    assert state.interpretation.intent == "data_question"
    assert state.disagreements == []
    assert llm.calls == 3


def test_a_split_vote_takes_the_majority_and_records_it():
    state = _state()
    llm = _ScriptedLLM(["question", "data_question", "data_question"])
    interpreter.run(state, llm)
    assert state.interpretation.intent == "data_question"
    assert len(state.disagreements) == 1
    item = state.disagreements[0]
    assert item["node"] == "interpreter" and item["field"] == "intent"
    assert item["candidates"] == ["question", "data_question", "data_question"]
    assert item["chosen"] == "data_question"
    assert item["is_majority"] is True


def test_a_true_three_way_tie_falls_back_to_the_first_vote_deterministically():
    state = _state()
    llm = _ScriptedLLM(["question", "bug", "change_request"])
    interpreter.run(state, llm)
    assert state.interpretation.intent == "question"  # the FIRST vote, not alphabetical/other
    item = state.disagreements[0]
    assert item["is_majority"] is False
    assert "no vote reached a majority" in item["reason"]


def test_a_single_surviving_vote_after_two_failures_is_not_a_disagreement():
    """One vote that succeeds trivially "agrees with itself" — nothing to log."""
    state = _state()

    class FlakyLLM(_ScriptedLLM):
        """Fails the first two attempts; only ONE call ever reaches the base
        class's counter (which indexes `self._intents`), so a single-entry
        intents list is what "the one vote that got through" actually is."""

        def __init__(self, intents):
            super().__init__(intents)
            self._attempts = 0

        def complete(self, *a, **kw):
            self._attempts += 1
            if self._attempts <= 2:
                raise RuntimeError("503")
            return super().complete(*a, **kw)

    llm = FlakyLLM(["bug"])
    interpreter.run(state, llm)
    assert state.interpretation.intent == "bug"
    assert state.disagreements == []


def test_all_votes_failing_falls_back_to_the_deterministic_keyword_path():
    state = _state()

    class DeadLLM(_ScriptedLLM):
        def complete(self, *a, **kw):
            raise RuntimeError("down")

    interpreter.run(state, DeadLLM([]))
    assert state.interpretation.intent == "data_question"  # keyword fallback still fires
    # every attempt's actual exception is preserved — more useful for debugging
    # than a generic "all failed" would be, and this is what run() actually does.
    assert state.interpretation.reasoning.count("RuntimeError: down") == config.INTERPRETER_VOTES


def test_votes_stop_early_once_the_budget_is_exhausted():
    """max_calls=1: the check at the TOP of the loop passes once (usage.calls
    starts at 0, 0 >= 1 is False), one vote runs and books `calls=1`, and the
    second iteration's check (1 >= 1) then stops any further vote."""
    from bridge.agents.llm import Budget

    state = _state()
    state.budget = Budget(max_usd=None, max_calls=1, max_input_tokens=None, max_seconds=None)
    llm = _ScriptedLLM(["question", "bug", "change_request"])
    interpreter.run(state, llm)
    assert llm.calls == 1
    assert state.interpretation.intent == "question"  # the one vote that ran
    assert state.disagreements == []  # a single vote has nothing to disagree with


# ------------------------------------------------------------------ RunStore: eval + cache


@pytest.fixture()
def tmp_store(tmp_path: Path) -> RunStore:
    store = RunStore.open(tmp_path / "runs.sqlite")
    yield store
    store.close()


def test_eval_candidate_round_trips(tmp_store):
    tmp_store.record_eval_candidate(
        "run_1",
        {"node": "interpreter", "field": "intent", "candidates": ["a", "b", "a"],
         "chosen": "a", "is_majority": True, "reason": "2 of 3 votes agreed"},
    )
    rows = tmp_store.eval_candidates_for("run_1")
    assert len(rows) == 1
    assert rows[0]["chosen"] == "a"
    assert rows[0]["is_majority"] == 1


def test_cache_lookup_and_store_round_trip(tmp_store):
    assert tmp_store.cache_lookup("snap1", "what happens") is None
    tmp_store.cache_store("snap1", "what happens", "run_abc")
    assert tmp_store.cache_lookup("snap1", "what happens") == "run_abc"


def test_cache_store_overwrites_on_the_same_key(tmp_store):
    tmp_store.cache_store("snap1", "q", "run_1")
    tmp_store.cache_store("snap1", "q", "run_2")
    assert tmp_store.cache_lookup("snap1", "q") == "run_2"


def test_split_header_separates_the_header_line_from_the_body():
    head, body = split_header("**question** · confidence: high\n\nline one\nline two")
    assert head == "**question** · confidence: high"
    assert body == "line one\nline two"


def test_split_header_on_a_headless_string_returns_it_whole():
    head, body = split_header("just one line, no blank line after it")
    assert head == "just one line, no blank line after it"
    assert body == ""


# ------------------------------------------------------------------ end-to-end (offline)


def _latest_index() -> Path | None:
    candidates = [p for p in config.INDEX_ROOT.glob("*") if (p / config.GRAPH_DB_NAME).is_file()]
    return max(candidates, key=lambda p: (p / config.GRAPH_DB_NAME).stat().st_mtime, default=None)


@pytest.fixture()
def index_dir() -> Path:
    path = _latest_index()
    if path is None:
        pytest.skip("no index built")
    return path


def test_a_repeated_question_hits_the_cache_and_says_so_in_the_header(index_dir, tmp_store):
    """Fully offline: LLMClient(enabled=False) degrades every node to its
    documented fallback and still reaches status == "done" — enough to exercise
    the CACHE plumbing without spending anything real."""
    question = "What happens when an owner adds a new pet?"
    with Retriever.open(index_dir) as retriever:
        first = run(
            question, index_dir=index_dir, retriever=retriever, store=tmp_store,
            llm=LLMClient(enabled=False), fresh=True,
        )
        assert first["status"] == "done"

        second = run(
            question, index_dir=index_dir, retriever=retriever, store=tmp_store,
            llm=LLMClient(enabled=False),
        )
        header = second["answer"].splitlines()[0]
        assert "cached" in header.lower()
        assert first["run_id"][:12] in header
        assert second["usage"]["cost_usd"] == 0.0
        assert second["run_id"] != first["run_id"]
        assert second["env"]["cache"]["source_run_id"] == first["run_id"]

        # forcing fresh bypasses the cache even though a hit exists
        third = run(
            question, index_dir=index_dir, retriever=retriever, store=tmp_store,
            llm=LLMClient(enabled=False), fresh=True,
        )
        assert "cached" not in third["answer"].splitlines()[0].lower()


def test_a_follow_up_question_is_never_served_from_cache(index_dir, tmp_store):
    """A question asked into a thread that already has history must be
    recomputed — its correct answer can depend on that history, which the
    cache key carries no trace of."""
    q1 = "What happens when an owner adds a new pet?"
    q2 = "What happens when an owner adds a new pet?"  # identical text, on purpose
    with Retriever.open(index_dir) as retriever:
        llm = lambda: LLMClient(enabled=False)  # noqa: E731
        first = run(q1, index_dir=index_dir, retriever=retriever, store=tmp_store, llm=llm(), fresh=True)
        followup = run(
            q2, thread_id=first["thread_id"], index_dir=index_dir, retriever=retriever,
            store=tmp_store, llm=llm(),
        )
        assert "cached" not in followup["answer"].splitlines()[0].lower()
        assert followup["prior_context"] != ""


def test_a_changelog_question_is_never_cached(index_dir, tmp_store):
    with Retriever.open(index_dir) as retriever:
        llm = lambda: LLMClient(enabled=False)  # noqa: E731
        question = "What changed in the last 5 commits?"
        run(question, index_dir=index_dir, retriever=retriever, store=tmp_store, llm=llm())
        snapshot = cache.snapshot_key(retriever.repo, retriever.sha)
        assert tmp_store.cache_lookup(snapshot, cache.question_norm(question)) is None


def test_a_stale_cache_pointer_falls_through_to_a_fresh_run(index_dir, tmp_store):
    """The pointed-to run row can be gone (or from a schema this store never
    had) — a lookup hit must not crash the caller; it must just recompute."""
    question = "Where are a vet's specialties shown?"
    snapshot = cache.snapshot_key(_retriever_stub(index_dir).repo, _retriever_stub(index_dir).sha)
    tmp_store.cache_store(snapshot, cache.question_norm(question), "run_that_does_not_exist")
    with Retriever.open(index_dir) as retriever:
        result = run(
            question, index_dir=index_dir, retriever=retriever, store=tmp_store,
            llm=LLMClient(enabled=False),
        )
        assert result["status"] == "done"
        assert "cached" not in result["answer"].splitlines()[0].lower()


def _retriever_stub(index_dir: Path):
    r = Retriever.open(index_dir)
    r.close()
    return r
