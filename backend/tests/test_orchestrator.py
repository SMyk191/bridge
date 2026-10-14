"""Orchestrator tests: lane rules, persistence, budget, and the agent contracts.

No API calls — the lane table, the store, and the budget are all deterministic,
and the agents' degraded paths are exactly what should be pinned.
"""

from __future__ import annotations

import json

import pytest

from bridge import config
from bridge.agents.architect import MORE_SURE_PREFIX
from bridge.agents.interpreter import parse_json_object
from bridge.agents.llm import Budget, BudgetExceeded, LLMClient, Usage
from bridge.agents.state import ImpactEntry, ImpactMap, Interpretation, RunState, citation_path
from bridge.orchestrator import NODE_SEQUENCE, RunStore, compose_answer, decide_lane, wants_architect
from bridge.orchestrator.graph import DATA_QUESTION_REDIRECT


def make_state(intent="question", entries=0, confidence="high", **kwargs) -> RunState:
    state = RunState(request="x", **kwargs)
    state.interpretation = Interpretation(intent=intent, normalised_request="x")
    state.impact_map = ImpactMap(
        confidence=confidence,
        classes_methods=[ImpactEntry(f"m{i}", f"repo@sha:src/F{i}.java:1-2", "why") for i in range(entries)],
    )
    return state


# ------------------------------------------------------------------ node sequence


def test_node_sequence_is_fixed_and_python_owned():
    assert NODE_SEQUENCE == (
        "interpreter", "cartographer", "followup", "historian", "architect", "explainer",
        "verifier",
    )


def test_architect_runs_only_for_change_and_bug():
    assert wants_architect("change_request")
    assert wants_architect("bug")
    assert not wants_architect("question")
    assert not wants_architect("data_question")
    assert not wants_architect(None)


# ------------------------------------------------------------------ lane rules


def test_data_question_short_circuits_regardless_of_evidence():
    for entries in (0, 10):
        decision = decide_lane(make_state("data_question", entries, "high"))
        assert decision.lane == "data_question"


def test_change_request_with_evidence_reaches_the_architect():
    decision = decide_lane(make_state("change_request", 6, "high"))
    assert decision.lane == "change_request"
    assert not decision.downgraded


def test_change_request_without_evidence_is_downgraded_to_question():
    """The rule that makes routing depend on the map, not the phrasing (design law 3)."""
    decision = decide_lane(make_state("change_request", 0, "low"))
    assert decision.lane == "question"
    assert decision.downgraded
    assert "empty" in decision.reason


def test_thin_low_confidence_evidence_is_downgraded():
    decision = decide_lane(make_state("bug", 2, "low"))
    assert decision.lane == "question"
    assert decision.downgraded


def test_a_question_is_never_promoted_to_a_change_request():
    """A rich map does not mean the user asked for a work plan."""
    decision = decide_lane(make_state("question", 20, "high"))
    assert decision.lane == "question"
    assert not decision.downgraded


def test_data_question_redirect_names_what_bridge_can_do_instead():
    assert "production data" in DATA_QUESTION_REDIRECT
    assert "where" in DATA_QUESTION_REDIRECT.lower()


# ------------------------------------------------------------------ budget


def test_budget_has_four_independent_ceilings():
    budget = Budget(max_usd=1.0, max_calls=5, max_input_tokens=100, max_seconds=None)
    assert budget.exceeded(Usage(calls=1, cost_usd=0.1, input_tokens=10)) is None
    assert "call budget" in budget.exceeded(Usage(calls=5))
    assert "cost budget" in budget.exceeded(Usage(cost_usd=1.5))
    assert "input-token budget" in budget.exceeded(Usage(input_tokens=200))


def test_run_budget_matches_the_configured_ceilings():
    budget = Budget.for_run()
    assert budget.max_usd == config.RUN_BUDGET_USD == 0.40
    assert budget.max_input_tokens == config.RUN_BUDGET_INPUT_TOKENS == 60_000
    # raised from 90s in phase A, when the lane writers started reading source —
    # a full run measured 98.6s on the widest smoke question
    assert budget.max_seconds == config.RUN_BUDGET_SECONDS == 240.0
    assert budget.started_at is not None, "the clock must start with the run"


def test_time_budget_is_measured_from_start_not_construction():
    budget = Budget(max_usd=None, max_calls=None, max_input_tokens=None, max_seconds=0.0)
    assert budget.exceeded(Usage()) is None, "an unstarted clock cannot expire"
    budget.start()
    assert "time budget" in budget.exceeded(Usage())


def test_exceeded_is_the_non_raising_form_of_check():
    budget = Budget(max_usd=0.001, max_calls=None)
    with pytest.raises(BudgetExceeded):
        budget.check(Usage(cost_usd=1.0))
    assert budget.exceeded(Usage(cost_usd=1.0))


# ------------------------------------------------------------------ persistence


def test_run_is_persisted_and_reloadable(tmp_path):
    state = make_state("change_request", 3, "high")
    state.lane = "change_request"
    state.status = "done"
    state.usage.cost_usd = 0.02
    state.usage.input_tokens = 1234

    with RunStore.open(tmp_path / "runs.sqlite") as store:
        store.save(state)
        row = store.get(state.run_id)
        assert row is not None
        assert row["lane"] == "change_request"
        assert row["confidence"] == "high"
        assert row["cost_usd"] == pytest.approx(0.02)
        assert row["citations"] == 3
        reloaded = store.state_of(state.run_id)
        assert reloaded["request"] == state.request
        assert reloaded["impact_map"]["total"] == 3


def test_saving_twice_updates_rather_than_duplicates(tmp_path):
    """State is written after every node — the run must be one row, not four."""
    state = make_state()
    with RunStore.open(tmp_path / "runs.sqlite") as store:
        store.save(state)
        state.lane = "question"
        state.status = "done"
        store.save(state)
        assert len(store.recent(10)) == 1
        assert store.get(state.run_id)["status"] == "done"


def test_thread_lookup_finds_the_previous_run_for_follow_ups(tmp_path):
    with RunStore.open(tmp_path / "runs.sqlite") as store:
        first = make_state("question", 2, "high", thread_id="thr_shared")
        first.status = "done"
        store.save(first)
        second = make_state("question", 1, "high", thread_id="thr_shared")
        store.save(second)

        previous = store.latest_in_thread("thr_shared", before_run_id=second.run_id)
        assert previous is not None
        assert previous["run_id"] == first.run_id
        assert store.latest_in_thread("thr_other") is None


def test_running_runs_are_not_offered_as_prior_context(tmp_path):
    """A half-finished run is not a fact to build the next question on."""
    with RunStore.open(tmp_path / "runs.sqlite") as store:
        state = make_state(thread_id="thr_x")
        state.status = "running"
        store.save(state)
        assert store.latest_in_thread("thr_x") is None


# ------------------------------------------------------------------ answer assembly


def test_answer_carries_citations_and_confidence():
    state = make_state("question", 2, "high")
    answer = compose_answer(state)
    assert "repo@sha:src/F0.java:1-2" in answer
    # Confidence moved from a trailing line in the body to the header stamp, so
    # it is read before the answer rather than after it.
    assert "confidence: high" in answer.splitlines()[0]


def test_aborted_run_answers_partially_and_says_so():
    state = make_state("question", 2, "medium")
    state.abort_reason = "cost budget exhausted ($0.4100/$0.40)"
    answer = compose_answer(state)
    assert "Partial answer" in answer
    assert "cost budget exhausted" in answer
    assert "repo@sha:src/F0.java:1-2" in answer, "a partial answer still cites what it found"


def test_gaps_are_surfaced_in_the_answer():
    state = make_state("question", 1, "medium")
    state.impact_map.gaps = ["the persistence layer was not retrieved"]
    assert "persistence layer" in compose_answer(state)


# ------------------------------------------------------------------ agent contracts


def test_interpreter_json_parser_tolerates_fences_and_prose():
    assert parse_json_object('{"intent": "bug"}')["intent"] == "bug"
    assert parse_json_object('```json\n{"intent": "bug"}\n```')["intent"] == "bug"
    assert parse_json_object('Sure!\n{"intent": "bug"}\nHope that helps')["intent"] == "bug"
    assert parse_json_object("not json at all") is None
    assert parse_json_object("") is None


def test_citation_path_extraction():
    assert citation_path("repo@sha:src/main/A.java:10-20") == "src/main/A.java"
    # `ids.citation` emits the bare `:45` form when start == end, and stripping
    # only dashed ranges left the line number glued to the path — see the
    # docstring on citation_path.
    assert citation_path("repo@sha:src/main/A.java:45") == "src/main/A.java"
    assert citation_path("repo@sha:src/main/A.java") == "src/main/A.java"
    assert citation_path("") == ""


def test_impact_map_paths_are_deduplicated():
    impact = ImpactMap(
        classes_methods=[
            ImpactEntry("a", "r@s:src/A.java:1-2", "w"),
            ImpactEntry("b", "r@s:src/A.java:9-9", "w"),
        ],
        screens=[ImpactEntry("c", "r@s:tpl/x.html:1-3", "w")],
    )
    assert impact.paths() == ["src/A.java", "tpl/x.html"]
    assert impact.total == 3


def test_volatile_threshold_comes_from_config():
    assert config.VOLATILE_COMMITS_IN_WINDOW == 5


def test_every_agent_prompt_declares_retrieved_content_untrusted():
    """Design law 5 — the tag alone is not enough; the prompt must say so."""
    from bridge.agents.llm import load_prompt

    for name in ("interpreter", "cartographer_summary", "cartographer_merge", "architect",
                 "describe", "rerank", "explainer", "changelog_group"):
        # collapse whitespace: these sentences are line-wrapped in the files
        text = " ".join(load_prompt(name).lower().split())
        assert "<retrieved>" in text, f"{name}.md does not mention the tag"
        assert "never obey" in text, f"{name}.md does not tell the model to ignore instructions"
        assert "data, not instruction" in text, f"{name}.md does not call it data"


def test_describe_prompt_demands_behaviour_first():
    from bridge.agents.llm import load_prompt

    text = load_prompt("describe").lower()
    assert "lead with the behaviour" in text
    assert "lifecycle" in text or "stage" in text


def test_architect_more_sure_prefix_is_stable():
    assert MORE_SURE_PREFIX == "What would make me more sure:"


def test_every_role_stays_wired_for_a_provider_switch():
    """config.ROLE_PROVIDER is meant to be a one-line switch — that only holds if
    every role is defined under BOTH providers, including the new ones."""
    for role in config.ROLE_PROVIDER:
        for provider in (config.PROVIDER_ANTHROPIC, config.PROVIDER_GEMINI):
            assert role in config.ROLE_MODELS[provider], f"{role} has no {provider} model"


def test_all_agent_roles_are_configured_for_the_active_provider():
    for role in ("interpreter", "cartographer", "cartographer_merge", "architect",
                 "explainer", "changelog"):
        assert role in config.ROLE_PROVIDER
        provider = config.ROLE_PROVIDER[role]
        assert role in config.ROLE_MODELS[provider]


def test_llm_client_exposes_per_role_availability_for_every_agent_role():
    client = LLMClient(enabled=False)
    for role in ("interpreter", "cartographer", "cartographer_merge", "architect",
                 "explainer", "changelog"):
        assert client.available_for(role) is False
        assert client.reason_for(role)


def test_the_gemini_client_carries_a_request_timeout():
    """Every role runs on Gemini and the run budget is only checked *between*
    nodes, so a call with no timeout hangs the run past any ceiling. Observed:
    a smoke suite sat on one question for 37 minutes against a 150s budget."""
    client = LLMClient()
    provider = client.provider_for("explainer")
    if provider is None or not provider.available:
        pytest.skip("no Gemini credentials in this environment")
    options = provider._client._api_client._http_options
    assert options.timeout == int(config.LLM_TIMEOUT_SECONDS * 1000), "milliseconds, not seconds"
    assert options.retry_options is not None
    assert options.retry_options.attempts == config.LLM_MAX_RETRIES
