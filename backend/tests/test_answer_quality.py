"""Phase A: the answer is a narrative, and the changelog lane exists.

Everything here is deterministic — no model is called. What is pinned is the part
of answer quality that *can* be pinned in Python: which writer runs for which
lane, what the composed answer is and is not allowed to contain, that a
changelog over the same range comes out in the same order twice, and that a
citation an agent was not given never passes as one.
"""

from __future__ import annotations

import subprocess

import pytest

from bridge import config
from bridge.agents import changelog as changelog_agent
from bridge.agents import explainer
from bridge.agents.state import (
    ImpactEntry,
    ImpactMap,
    Interpretation,
    RunState,
    citations_in,
)
from bridge.orchestrator import (
    CHANGELOG_SEQUENCE,
    NODE_SEQUENCE,
    answer_body,
    changelog_trigger,
    compose_answer,
    plan_nodes,
    wants_architect,
    wants_explainer,
)
from bridge.retrieval import code as code_module
from bridge.retrieval.retrieve import Chunk

SHA = "88e37c15cf6fc8490b01bc3e8e2c800cec1ac272"


def make_chunk(name: str, path: str, start: int = 1, end: int = 20, **kwargs) -> Chunk:
    return Chunk(
        node_id=kwargs.pop("node_id", f"java:{name}"),
        kind=kwargs.pop("kind", "method"),
        name=name,
        path=path,
        start_line=start,
        end_line=end,
        description=kwargs.pop("description", f"{name} does a thing"),
        citation=f"petclinic@{SHA}:{path}:{start}-{end}",
        **kwargs,
    )


def make_state(lane="question", *, entries=2, confidence="high", **kwargs) -> RunState:
    state = RunState(request="what happens when an owner adds a pet?", **kwargs)
    state.lane = lane
    state.sha = SHA
    state.env = {"repo": "petclinic", "indexed_at": "2026-08-18"}
    state.interpretation = Interpretation(intent=lane, normalised_request=state.request)
    state.impact_map = ImpactMap(
        summary="Adding a pet goes through the pet controller.",
        confidence=confidence,
        classes_methods=[
            ImpactEntry(f"m{i}", f"petclinic@{SHA}:src/F{i}.java:1-2", "why")
            for i in range(entries)
        ],
    )
    return state


# ----------------------------------------------------------------- lane wiring


def test_the_question_lane_has_a_writer_at_last():
    """The whole point of the phase: a question used to reach no writer at all."""
    assert "explainer" in NODE_SEQUENCE
    assert wants_explainer("question")
    assert not wants_architect("question")


@pytest.mark.parametrize("lane", ["change_request", "bug"])
def test_the_architect_lanes_do_not_also_get_a_walkthrough(lane):
    """One writer per lane — two would produce two answers to the same question."""
    assert wants_architect(lane)
    assert not wants_explainer(lane)


def test_data_question_gets_neither_writer():
    assert not wants_architect("data_question")
    assert not wants_explainer("data_question")


# ----------------------------------------------------------- changelog routing


@pytest.mark.parametrize(
    "request_text,commits,since",
    [
        ("What changed in the last 10 commits?", 10, None),
        ("what changed last week?", config.CHANGELOG_MAX_COMMITS, "1.week.ago"),
        ("what changed in the past 3 weeks", config.CHANGELOG_MAX_COMMITS, "3.weeks.ago"),
        ("what shipped recently", config.CHANGELOG_DEFAULT_COMMITS, None),
        ("give me a changelog", config.CHANGELOG_DEFAULT_COMMITS, None),
        ("release notes please", config.CHANGELOG_DEFAULT_COMMITS, None),
    ],
)
def test_changelog_fires_on_what_changed_plus_a_period(request_text, commits, since):
    ask = changelog_trigger(request_text)
    assert ask is not None, request_text
    assert ask.commits == commits
    assert ask.since == since


@pytest.mark.parametrize(
    "request_text",
    [
        # the trap: change-request words with no period named
        "What changes when I add a weight field to pets?",
        "what changed in the pet form when validation was added",
        "What happens when an owner adds a new pet?",
        "Where are a vet's specialties shown?",
        "How many owners signed up last month?",
    ],
)
def test_changelog_does_not_fire_without_both_halves(request_text):
    assert changelog_trigger(request_text) is None, request_text


def test_an_explicit_range_always_wins_over_the_wording():
    ask = changelog_trigger("anything at all", {"from_sha": "abc1234", "to_sha": "def5678"})
    assert ask is not None
    assert (ask.from_sha, ask.to_sha, ask.source) == ("abc1234", "def5678", "params")
    # a from_sha alone is a range ending at HEAD
    assert changelog_trigger("x", {"from_sha": "abc1234"}).to_sha == "HEAD"


def test_plan_nodes_returns_one_of_exactly_two_sequences():
    assert plan_nodes(make_state()) is NODE_SEQUENCE
    changelog = RunState(request="what changed last week?")
    assert plan_nodes(changelog) is CHANGELOG_SEQUENCE
    # and the changelog lane shares no node with the retrieval pipeline
    assert not set(CHANGELOG_SEQUENCE) & set(NODE_SEQUENCE)


def test_explicit_params_route_a_neutral_question_to_the_changelog():
    state = RunState(request="what's going on", params={"from_sha": "abc1234"})
    assert plan_nodes(state) is CHANGELOG_SEQUENCE


# -------------------------------------------------------------- compose_answer


def test_the_answer_body_is_the_lane_writers_prose():
    state = make_state("question")
    state.narrative = "**Where it starts**\nOn the new pet form."
    assert answer_body(state) == state.narrative

    brief = make_state("change_request")
    brief.spec = "**Current behaviour**\nPets have no weight."
    brief.narrative = "a walkthrough that should not win on this lane"
    assert answer_body(brief) == brief.spec


def test_the_impact_map_is_no_longer_rendered_into_the_answer():
    """The regression this phase exists to prevent."""
    state = make_state("question")
    state.narrative = "**Where it starts**\nOn the new pet form. `petclinic@%s:src/F0.java:1-2`" % SHA
    answer = compose_answer(state)
    for heading in ("Classes / Methods", "Entities / Tables", "Hubs Crossed", "Endpoints"):
        assert heading not in answer, f"the impact map leaked back into the body as {heading!r}"
    assert "m1" not in answer, "an uncited map entry appeared in the answer"


def test_the_header_stamps_lane_confidence_environment_and_cost():
    state = make_state("question", confidence="medium")
    state.narrative = "body"
    state.usage.cost_usd = 0.0461
    header = compose_answer(state).splitlines()[0]
    assert "**question**" in header
    assert "confidence: medium" in header
    assert f"petclinic@{SHA[:12]}" in header
    assert "indexed 2026-08-18" in header
    assert "$0.0461" in header


def test_the_changelog_header_claims_no_confidence_it_did_not_measure():
    state = RunState(request="what changed last week?")
    state.lane, state.narrative, state.impact_map = "changelog", "## What changed", None
    header = compose_answer(state).splitlines()[0]
    assert "evidence: git log" in header
    assert "confidence" not in header


def test_citations_index_the_body_not_the_map():
    state = make_state("question", entries=3)
    used = f"petclinic@{SHA}:src/F1.java:1-2"
    state.narrative = f"1. It saves the pet.\n`{used}`"
    answer = compose_answer(state)
    assert f"- `{used}`" in answer
    assert "src/F0.java" not in answer, "a map entry the answer never cited was listed anyway"


def test_citations_fall_back_to_the_map_when_the_body_cites_nothing():
    state = make_state("question", entries=1)
    state.narrative = "A walkthrough that forgot to cite anything."
    assert f"petclinic@{SHA}:src/F0.java:1-2" in compose_answer(state)


def test_gaps_survive_when_no_writer_ran():
    """Without a writer there is no other place the gaps could be stated."""
    state = make_state("question")
    state.impact_map.gaps = ["the persistence layer was not retrieved"]
    assert "persistence layer" in compose_answer(state)


def test_a_partial_answer_still_says_it_is_partial():
    state = make_state("question")
    state.narrative = "half a walkthrough"
    state.abort_reason = "cost budget exhausted ($0.4100/$0.40)"
    answer = compose_answer(state)
    assert answer.startswith("> **Partial answer**")
    assert "cost budget exhausted" in answer


def test_citations_are_parsed_out_of_prose_in_order():
    text = (
        f"first `petclinic@{SHA}:src/A.java:1-2`, then petclinic@abcdef12:src/B.html. "
        f"again `petclinic@{SHA}:src/A.java:1-2`"
    )
    assert citations_in(text) == [
        f"petclinic@{SHA}:src/A.java:1-2",
        "petclinic@abcdef12:src/B.html",
    ]


# ------------------------------------------------------------- code hydration


class FakeVectors:
    def __init__(self, rows):
        self._rows = rows

    def by_node_ids(self, node_ids):
        wanted = set(node_ids)
        return {k: v for k, v in self._rows.items() if k in wanted}


class FakeRetriever:
    def __init__(self, vectors=None, repo_root=None):
        self.vectors = vectors
        self.repo_root = repo_root


def test_code_is_hydrated_from_the_index_first():
    chunk = make_chunk("processCreationForm", "src/PetController.java")
    retriever = FakeRetriever(FakeVectors({chunk.node_id: {"code": "void save() {}"}}))
    assert code_module.hydrate(retriever, [chunk]) == 1
    assert chunk.code == "void save() {}"


def test_code_falls_back_to_disk_for_nodes_that_were_never_embedded(tmp_path):
    """Entities and endpoints have no description, so they have no vector row."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Pet.java").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    chunk = make_chunk("Pet", "src/Pet.java", start=2, end=3, kind="entity")
    retriever = FakeRetriever(FakeVectors({}), repo_root=tmp_path)
    assert code_module.hydrate(retriever, [chunk]) == 1
    assert chunk.code == "line2\nline3"


def test_unreadable_source_is_named_rather_than_left_blank():
    """A silent empty code block reads as "this method is empty" (design law 4)."""
    chunk = make_chunk("Gone", "src/Gone.java")
    code_module.hydrate(FakeRetriever(FakeVectors({})), [chunk])
    assert chunk.code == ""
    assert code_module.UNAVAILABLE in code_module.render(chunk)


def test_an_already_hydrated_chunk_is_not_refetched():
    chunk = make_chunk("kept", "src/A.java", code="original")
    assert code_module.hydrate(FakeRetriever(FakeVectors({chunk.node_id: {"code": "other"}})),
                               [chunk]) == 0
    assert chunk.code == "original"


def test_the_rendered_chunk_carries_what_a_claim_needs_to_be_checked():
    chunk = make_chunk("save", "src/A.java", graph_path="PetController -[contains]-> save",
                       code="void save() {}")
    chunk.history = {"commits_all_time": 30, "commits_recent": 5, "recent_window_days": 90}
    rendered = code_module.render(chunk)
    assert chunk.citation in rendered
    assert "PetController -[contains]-> save" in rendered
    assert "30 commits all time" in rendered
    assert "void save() {}" in rendered


# ------------------------------------------------------------------ explainer


def test_the_explainer_reads_the_map_in_map_order_then_fills_from_retrieval():
    state = make_state("question", entries=0)
    mapped = make_chunk("second", "src/B.java")
    unmapped = make_chunk("first", "src/A.java")

    class Retrieval:
        chunks = [unmapped, mapped]

    state.retrieval = Retrieval()
    state.impact_map.classes_methods = [ImpactEntry("second", mapped.citation, "why")]
    assert [c.name for c in explainer.select_chunks(state)] == ["second", "first"]


def test_the_explainer_chunk_budget_is_config_owned():
    state = make_state("question", entries=0)

    class Retrieval:
        chunks = [make_chunk(f"m{i}", f"src/F{i}.java", node_id=f"n{i}") for i in range(30)]

    state.retrieval = Retrieval()
    assert len(explainer.select_chunks(state)) == config.EXPLAINER_CHUNKS


def test_a_citation_the_explainer_was_not_given_is_called_out():
    known = {f"petclinic@{SHA}:src/A.java:1-2"}
    narrative = f"1. It saves. `petclinic@{SHA}:src/Invented.java:9-9`"
    invented = explainer._check_citations(narrative, known)
    assert invented == [f"petclinic@{SHA}:src/Invented.java:9-9"]
    flagged = explainer._append_to_gaps(narrative, ["**Unverified citation** — nope"])
    assert explainer.GAPS_HEADING in flagged
    assert "Unverified citation" in flagged


def test_gaps_are_added_under_an_existing_heading_not_after_the_trailing_line():
    narrative = f"body\n\n{explainer.GAPS_HEADING}\n\n- an existing gap\n\nWhat would make me more sure: x"
    merged = explainer._append_to_gaps(narrative, ["a new gap"])
    assert merged.count(explainer.GAPS_HEADING) == 1
    assert merged.index("a new gap") < merged.index("What would make me more sure")


def test_the_explainer_says_so_when_there_is_nothing_to_explain():
    from bridge.agents.llm import LLMClient

    state = make_state("question", entries=0)
    state.retrieval = None
    explainer.run(state, LLMClient(enabled=False))
    assert "unavailable" in (state.narrative or "").lower()


# ------------------------------------------------------------------ changelog


@pytest.mark.parametrize(
    "path,area",
    [
        ("README.md", config.CHANGELOG_BUILD_AREA),
        ("build.gradle", config.CHANGELOG_BUILD_AREA),
        ("gradle/wrapper/gradle-wrapper.properties", config.CHANGELOG_BUILD_AREA),
        ("k8s/petclinic.yml", config.CHANGELOG_BUILD_AREA),
        ("src/main/resources/db/postgres/schema.sql", "database schema"),
        ("src/main/resources/db/h2/schema.sql", "database schema"),
        ("src/main/java/org/x/owner/Pet.java", "owner"),
        ("src/main/resources/messages/messages_hi.properties", "messages"),
    ],
)
def test_a_root_level_build_file_is_not_a_product_area(path, area):
    assert changelog_agent.area_of(path) == area


def _commits():
    return [
        changelog_agent.Commit("a" * 40, "2026-07-30", "one", ["src/main/java/x/vet/Vet.java"]),
        changelog_agent.Commit("b" * 40, "2026-07-29", "two", ["src/main/java/x/owner/Pet.java"]),
        changelog_agent.Commit(
            "c" * 40, "2026-07-28", "three",
            ["src/main/java/x/owner/Owner.java", "build.gradle"],
        ),
    ]


def test_areas_are_ordered_by_volume_with_tooling_last():
    groups = changelog_agent.group_commits(_commits())
    assert [g.area for g in groups] == ["owner", "vet", config.CHANGELOG_BUILD_AREA]


def test_the_same_range_groups_the_same_way_twice():
    """A changelog that reshuffles between runs is not a changelog."""
    first = [g.area for g in changelog_agent.group_commits(_commits())]
    second = [g.area for g in changelog_agent.group_commits(list(reversed(_commits())))]
    assert first == second


def test_a_bullet_citing_something_it_was_not_given_is_dropped():
    known = {"petclinic@abcdef12:src/A.java"}
    text = (
        "- real change `petclinic@abcdef12:src/A.java`\n"
        "- invented change `petclinic@99999999:src/Nope.java`\n"
        "- no citation at all\n"
    )
    assert changelog_agent._clean_bullets(text, known) == [
        "- real change `petclinic@abcdef12:src/A.java`"
    ]


def test_at_most_three_bullets_survive_per_area():
    known = {"petclinic@abcdef12:src/A.java"}
    text = "\n".join("- change `petclinic@abcdef12:src/A.java`" for _ in range(9))
    assert len(changelog_agent._clean_bullets(text, known)) == 3


def test_read_commits_parses_a_real_git_log(tmp_path):
    """The `%x00` framing exists so a filename can never be read as a header."""
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "first commit | with a pipe")
    (tmp_path / "b.txt").write_text("2", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "second")

    ask = changelog_trigger("what changed in the last 5 commits")
    commits = changelog_agent.read_commits(tmp_path, ask)
    assert [c.subject for c in commits] == ["second", "first commit | with a pipe"]
    assert commits[0].files == ["b.txt"]
    assert len(commits[0].sha) == 40


def test_a_missing_repository_is_reported_not_guessed():
    from bridge.agents.llm import LLMClient

    state = RunState(request="what changed last week?")
    ask = changelog_trigger(state.request)
    changelog_agent.run(state, LLMClient(enabled=False), FakeRetrieverWithIndex(), ask)
    assert "not on disk" in state.narrative
    assert "Gaps" in state.narrative


class FakeRetrieverWithIndex(FakeRetriever):
    repo = "petclinic"
    sha = SHA
    descriptions: dict = {}
    store = None
    vocabulary = None

    def __init__(self):
        super().__init__(vectors=None, repo_root=None)
