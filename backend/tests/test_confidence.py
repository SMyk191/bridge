"""Confidence is a rule table now, and citations get checked. Both are pinned here.

Two halves. The first is pure — `verify.py`'s parsing and `confidence.py`'s rules
run against hand-built values, so every branch is reachable without an index. The
second uses the real petclinic graph, because the claims worth making about
`io_role` ("a POST handler is a persisting write; `@Transactional(readOnly=true)`
is not a write at all") are claims about a real repository, and a fixture that
agreed with the classifier by construction would prove nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge import config
from bridge.agents.state import (
    ImpactEntry,
    ImpactMap,
    Interpretation,
    RunState,
    append_more_sure,
)
from bridge.index import io_roles
from bridge.index.graph import GraphStore
from bridge.orchestrator import confidence as conf
from bridge.orchestrator import verify as verify_mod
from bridge.orchestrator import verifier as verifier_node

SHA = "88e37c15cf6fc8490b01bc3e8e2c800cec1ac272"
PKG = "org.springframework.samples.petclinic"


def cite(path: str, start: int | None = 1, end: int | None = 20, sha: str = SHA) -> str:
    if start is None:
        return f"petclinic@{sha}:{path}"
    return f"petclinic@{sha}:{path}:{start}-{end}"


class Retrieval:
    """Just the two fields `score_confidence` reads off a RetrievalResult."""

    def __init__(self, confidence="high", reason="best 9.0/10, top-3 mean 8.3/10"):
        self.confidence = confidence
        self.confidence_reason = reason


def verified(*, kinds=("method",), roles=(("read", "handles_route"),), path="src/A.java"):
    return verify_mod.VerifiedCitation(
        citation=cite(path),
        path=path,
        start=1,
        end=20,
        node_id="n1",
        kind=kinds[0],
        io_role=roles[0][0],
        io_source=roles[0][1],
        kinds=frozenset(kinds),
        roles=frozenset(roles),
    )


READ_NODE = dict(kinds=("screen",), roles=(("read", "screen"),))
WRITE_NODE = dict(kinds=("method",), roles=(("write", "handles_route"),))
SETTER_NODE = dict(kinds=("method",), roles=(("write", "accessor"),))
ENTITY_NODE = dict(kinds=("entity", "class"), roles=(("none", ""),))
HANDLER_NODE = dict(kinds=("method",), roles=(("read", "handles_route"),))


# ------------------------------------------------------------------ io roles (pure)


@pytest.mark.parametrize(
    "method,role",
    [("GET", "read"), ("HEAD", "read"), ("POST", "write"), ("DELETE", "write"),
     ("ANY", "both"), (None, "none")],
)
def test_http_method_decides_the_role(method, role):
    assert io_roles.role_of_http_method(method) == role


@pytest.mark.parametrize(
    "name,role",
    [
        ("findByLastName", "read"),
        ("countByOwner", "read"),
        ("existsByName", "read"),
        ("save", "write"),
        ("deleteById", "write"),
        ("removeAll", "write"),
        # longest prefix wins: this is `deleteDistinct`, not `delete` + noise
        ("deleteDistinctByOwner", "write"),
        ("frobnicate", "none"),
    ],
)
def test_repository_method_names_are_classified(name, role):
    assert io_roles.role_of_repository_method(name) == role


def test_transactional_read_only_is_a_read_not_a_write():
    """Petclinic's only two `@Transactional` methods are finders. Calling them
    writes would put a false write-side tick on a read-only answer."""
    write = {"annotations": ["Transactional"]}
    read = {"annotations": ["Transactional"], "annotation_args": {"Transactional": {"readOnly": ["true"]}}}
    assert io_roles.role_of_transactional(write) == "write"
    assert io_roles.role_of_transactional(read) == "read"
    assert io_roles.role_of_transactional({"annotations": []}) == "none"


def test_roles_combine_as_a_union():
    assert io_roles.combine("read", "write") == "both"
    assert io_roles.combine("read", "read") == "read"
    assert io_roles.combine("read", "none", None) == "read"
    assert io_roles.combine("none", None) == "none"


# ------------------------------------------------------------------ verify (pure)


@pytest.mark.parametrize(
    "citation,expected",
    [
        (f"petclinic@{SHA}:src/A.java:12-30", ("petclinic", SHA, "src/A.java", 12, 30)),
        (f"petclinic@{SHA}:src/A.java:45", ("petclinic", SHA, "src/A.java", 45, 45)),
        (f"petclinic@{SHA}:src/A.java", ("petclinic", SHA, "src/A.java", None, None)),
        ("not a citation", None),
    ],
)
def test_citation_parsing(citation, expected):
    assert verify_mod.parse_citation(citation) == expected


def test_scrub_removes_the_pointer_and_leaves_the_sentence():
    bad = cite("src/A.java", 400, 450)
    text = f"1. It saves the pet.\n`{bad}`\n2. Then redirects."
    scrubbed = verify_mod.scrub(text, [bad])
    assert bad not in scrubbed
    assert verify_mod.SCRUB_MARKER in scrubbed
    assert "It saves the pet." in scrubbed, "the claim survives; only its false support goes"
    assert "Then redirects." in scrubbed


def test_drop_ratio_is_over_what_was_checked_not_what_was_skipped():
    result = verify_mod.VerificationResult(
        verified=[verified()], dropped=[("x", "y")], skipped=["a", "b", "c"]
    )
    assert result.checked == 2
    assert result.drop_ratio == 0.5


# ------------------------------------------------------------------ confidence rules


def test_the_base_is_the_retrieval_score():
    verdict = conf.score_confidence("question", Retrieval("high"), ImpactMap(), [verified()])
    assert verdict.level == "high"
    assert "evidence scored high on retrieval" in verdict.reasons[0]


def test_any_cartographer_gap_caps_medium():
    gapped = ImpactMap(gaps=["the persistence layer was not retrieved"])
    verdict = conf.score_confidence("question", Retrieval("high"), gapped, [verified()])
    assert verdict.level == "medium"
    assert "gap" in verdict.reasons[1]
    # the gap is also what would raise it, so it reaches the answer's own line
    assert any("persistence layer" in item for item in verdict.missing)


def test_nothing_verifiable_means_low():
    verdict = conf.score_confidence("question", Retrieval("high"), ImpactMap(), [])
    assert verdict.level == "low"
    assert verdict.missing


# -- bug lane


def test_a_bug_answer_citing_only_the_display_path_is_capped_and_told_why():
    verdict = conf.score_confidence("bug", Retrieval("high"), ImpactMap(), [verified(**READ_NODE)])
    assert verdict.level == "medium"
    assert any("write/persist" in reason for reason in verdict.reasons)
    assert any("saves this" in item for item in verdict.missing)


def test_a_bug_answer_citing_only_the_write_path_is_capped_the_other_way():
    verdict = conf.score_confidence("bug", Retrieval("high"), ImpactMap(), [verified(**WRITE_NODE)])
    assert verdict.level == "medium"
    assert any("read/display" in reason for reason in verdict.reasons)
    assert any("displays this" in item for item in verdict.missing)


def test_a_bug_answer_covering_both_sides_keeps_its_base_level():
    both = [verified(**READ_NODE), verified(**WRITE_NODE)]
    assert conf.score_confidence("bug", Retrieval("high"), ImpactMap(), both).level == "high"


def test_a_setter_is_not_the_write_side_of_a_bug():
    """`Owner#addPet` mutates a list in memory. Nothing has been saved, so the
    persistence half of the flow is still unexamined."""
    citations = [verified(**READ_NODE), verified(**SETTER_NODE)]
    verdict = conf.score_confidence("bug", Retrieval("high"), ImpactMap(), citations)
    assert verdict.level == "medium"
    assert any("write/persist" in reason for reason in verdict.reasons)


# -- change_request lane


class FakeStore:
    def __init__(self, kinds=("screen", "endpoint")):
        self._kinds = set(kinds)

    class _Conn:
        def __init__(self, kinds):
            self._kinds = kinds

        def execute(self, _sql, params=()):
            class _Cursor:
                def __init__(self, hit):
                    self._hit = hit

                def fetchone(self):
                    return (1,) if self._hit else None

            return _Cursor(params and params[0] in self._kinds)

    @property
    def conn(self):
        return self._Conn(self._kinds)


def test_an_entity_change_without_a_handler_is_capped():
    citations = [verified(**ENTITY_NODE), verified(kinds=("screen",), roles=(("read", "screen"),))]
    verdict = conf.score_confidence(
        "change_request", Retrieval("high"), ImpactMap(), citations, store=FakeStore()
    )
    assert verdict.level == "medium"
    assert any("handler that accepts it" in reason for reason in verdict.reasons)


def test_an_entity_change_without_a_surface_is_capped_when_the_graph_has_one():
    citations = [verified(**ENTITY_NODE), verified(**HANDLER_NODE)]
    verdict = conf.score_confidence(
        "change_request", Retrieval("high"), ImpactMap(), citations, store=FakeStore()
    )
    assert verdict.level == "medium"
    assert any("screen or endpoint" in reason for reason in verdict.reasons)


def test_a_headless_service_is_not_punished_for_having_no_screens():
    """"when they exist in the graph" — demanding a layer the repo lacks would
    make every answer about a headless service permanently medium."""
    citations = [verified(**ENTITY_NODE), verified(**HANDLER_NODE)]
    verdict = conf.score_confidence(
        "change_request", Retrieval("high"), ImpactMap(), citations, store=FakeStore(kinds=())
    )
    assert verdict.level == "high"


def test_all_three_layers_present_keeps_the_base_level():
    citations = [
        verified(**ENTITY_NODE),
        verified(**HANDLER_NODE),
        verified(kinds=("screen",), roles=(("read", "screen"),)),
    ]
    verdict = conf.score_confidence(
        "change_request", Retrieval("high"), ImpactMap(), citations, store=FakeStore()
    )
    assert verdict.level == "high"


def test_the_entity_rule_stays_quiet_on_a_change_that_touches_no_entity():
    verdict = conf.score_confidence(
        "change_request", Retrieval("high"), ImpactMap(), [verified(**READ_NODE)],
        store=FakeStore(),
    )
    assert verdict.level == "high"


@pytest.mark.parametrize(
    "lane,impact,citations",
    [
        # each row is a case where a cap *would* fire — the point is that a cap
        # on a run already at `low` cannot lift it to the cap's level
        ("question", ImpactMap(gaps=["a gap"]), [verified()]),
        ("bug", ImpactMap(), [verified(**READ_NODE)]),
        ("change_request", ImpactMap(), [verified(**ENTITY_NODE)]),
    ],
)
def test_a_cap_can_only_lower_never_raise(lane, impact, citations):
    """`cap(MEDIUM, ...)` on a run scored `low` must leave it `low`.

    Written this way deliberately: an earlier version passed inputs where no cap
    fired at all, so it asserted nothing about the direction of travel and stayed
    green when `cap` was mutated into a plain assignment.
    """
    verdict = conf.score_confidence(
        lane, Retrieval("low", "best 3/10"), impact, citations, store=FakeStore()
    )
    assert verdict.level == "low", lane


def test_a_cap_that_does_not_bite_is_not_recorded_as_a_reason():
    verdict = conf.ConfidenceVerdict(level="medium")
    verdict.cap("high", "should not apply")
    assert verdict.level == "medium"
    assert verdict.reasons == []


# -- verification penalty


def _verification(dropped: int, verified_count: int):
    return verify_mod.VerificationResult(
        verified=[verified() for _ in range(verified_count)],
        dropped=[(f"bad{i}", "reason") for i in range(dropped)],
    )


def test_more_than_a_fifth_unverifiable_costs_a_level():
    result = _verification(dropped=2, verified_count=4)  # 33%
    verdict = conf.score_confidence(
        "question", Retrieval("high"), ImpactMap(), result.verified, verification=result
    )
    assert verdict.level == "medium"
    assert any("could not be verified" in reason for reason in verdict.reasons)


def test_a_single_slip_costs_a_reason_but_not_a_level():
    result = _verification(dropped=1, verified_count=9)  # 10%
    verdict = conf.score_confidence(
        "question", Retrieval("high"), ImpactMap(), result.verified, verification=result
    )
    assert verdict.level == "high"
    assert any("below the" in reason for reason in verdict.reasons)


def test_the_removal_note_counts_correctly():
    assert conf.removal_note(_verification(1, 3)) == (
        "1 citation could not be verified and was removed."
    )
    assert "2 citations" in conf.removal_note(_verification(2, 3))


def test_more_sure_line_is_extended_not_duplicated():
    text = "body\n\nWhat would make me more sure: the schema."
    merged = append_more_sure(text, ["the write path"])
    assert merged.lower().count("what would make me more sure") == 1
    assert "the schema; the write path" in merged


# ------------------------------------------------------------------ against the real graph


def _latest_index() -> Path | None:
    candidates = [p for p in config.INDEX_ROOT.glob("*") if (p / config.GRAPH_DB_NAME).is_file()]
    return max(candidates, key=lambda p: (p / config.GRAPH_DB_NAME).stat().st_mtime, default=None)


@pytest.fixture(scope="module")
def store():
    path = _latest_index()
    if path is None:
        pytest.skip("no index — run: .venv/bin/python -m bridge.index.build ../data/repos/spring-petclinic")
    graph = GraphStore.open(path / config.GRAPH_DB_NAME)
    row = graph.conn.execute(
        "SELECT attrs FROM nodes WHERE json_extract(attrs, '$.io_role') IS NOT NULL LIMIT 1"
    ).fetchone()
    if row is None:
        graph.close()
        pytest.skip("index predates io_role — re-run the indexer")
    yield graph
    graph.close()


@pytest.fixture(scope="module")
def sha(store) -> str:
    return store.get_meta("head_sha") or ""


def _role(store, node_id: str) -> tuple[str, str]:
    import json

    row = store.node(node_id)
    assert row is not None, node_id
    attrs = json.loads(row["attrs"])
    return attrs.get("io_role"), attrs.get("io_source")


def test_a_post_handler_is_a_persisting_write(store):
    assert _role(store, f"java:{PKG}.owner.PetController#processCreationForm") == (
        "write", "handles_route",
    )


def test_a_read_only_transactional_finder_is_not_a_write(store):
    role, _source = _role(store, f"java:{PKG}.vet.VetRepository#findAll()")
    assert role == "read", "@Transactional(readOnly = true) is a read"


def test_a_screen_is_the_read_side(store):
    assert _role(store, "screen:owners/ownerDetails")[0] == "read"


def test_a_setter_is_a_write_but_not_a_persisting_one(store):
    role, source = _role(store, f"java:{PKG}.owner.Owner#setCity")
    assert (role, source) == ("write", "accessor")
    assert source not in config.IO_PERSIST_SOURCES


def test_a_real_citation_verifies_and_carries_its_role(store, sha):
    citation = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    result = verify_mod.verify_citations([citation], store, sha)
    assert not result.dropped
    assert result.verified[0].is_read
    assert "screen" in result.verified[0].kinds


def test_a_fabricated_line_range_in_a_real_file_is_dropped(store, sha):
    """The failure mode that matters: a plausible range inside a file that exists."""
    path = "src/main/java/org/springframework/samples/petclinic/owner/Pet.java"
    result = verify_mod.verify_citations([f"spring-petclinic@{sha}:{path}:4000-4050"], store, sha)
    assert not result.verified
    assert result.dropped[0][1] == verify_mod.REASON_RANGE


def test_a_file_that_never_existed_is_dropped(store, sha):
    result = verify_mod.verify_citations(
        [f"spring-petclinic@{sha}:src/main/java/org/x/Invented.java:1-10"], store, sha
    )
    assert result.dropped[0][1] == verify_mod.REASON_PATH


def test_a_citation_at_another_commit_is_dropped(store, sha):
    path = "src/main/java/org/springframework/samples/petclinic/owner/Pet.java"
    result = verify_mod.verify_citations([f"spring-petclinic@{'a' * 40}:{path}:44-85"], store, sha)
    assert result.dropped[0][1] == verify_mod.REASON_SHA


def test_a_changelog_commit_citation_is_skipped_not_failed(store, sha):
    """`repo@<commit>:path` points at another commit *on purpose*."""
    result = verify_mod.verify_citations(
        ["spring-petclinic@bb37aad8:src/main/java/x/OwnerController.java"], store, sha
    )
    assert result.skipped and not result.dropped and not result.verified


def test_a_provider_error_never_reaches_the_answer_as_a_gap():
    """A 503's JSON body was appearing in the "what would make me more sure"
    line, where a product reader saw a stack trace presented as a fact about
    their codebase. The detail belongs in the trace."""
    from bridge.agents.cartographer import FALLBACK_GAP, _fallback_map

    state = RunState(request="x")
    raw = "ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'high demand'}}"
    impact = _fallback_map(state, raw)
    assert impact.gaps == [FALLBACK_GAP]
    assert "503" not in " ".join(impact.gaps + impact.confidence_reasons)
    assert state.env["cartographer_fallback"] == raw, "the detail is kept, just not shown"

    verdict = conf.score_confidence("question", Retrieval("high"), impact, [verified()])
    assert "503" not in " ".join(verdict.reasons + verdict.missing)


# ------------------------------------------------------------------ the verifier node


class FakeRetriever:
    def __init__(self, store):
        self.store = store


def _run_state(store, sha, narrative, *, lane="question", gaps=()) -> RunState:
    state = RunState(request="how does adding a pet work?")
    state.lane = lane
    state.sha = sha
    state.narrative = narrative
    state.retrieval = Retrieval("high")
    state.interpretation = Interpretation(intent=lane, normalised_request=state.request)
    state.impact_map = ImpactMap(summary="s", confidence="high", gaps=list(gaps))
    return state


def test_a_fabricated_citation_is_removed_from_the_answer_and_costs_a_level(store, sha):
    """End to end: three good citations and two invented ones (40% > the 20%
    threshold), so the answer loses the pointers, gains the note, and drops a level."""
    good = "src/main/java/org/springframework/samples/petclinic/owner/Pet.java"
    real = f"spring-petclinic@{sha}:{good}:44-85"
    screen = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    form = f"spring-petclinic@{sha}:src/main/resources/templates/pets/createOrUpdatePetForm.html:1-34"
    fake_range = f"spring-petclinic@{sha}:{good}:4000-4050"
    fake_file = f"spring-petclinic@{sha}:src/main/java/org/x/Nope.java:1-9"

    narrative = (
        f"1. The form is shown.\n`{form}`\n"
        f"2. The pet is stored.\n`{real}`\n"
        f"3. It appears on the page.\n`{screen}`\n"
        f"4. An audit record is written.\n`{fake_range}`\n"
        f"5. A notification is queued.\n`{fake_file}`\n"
    )
    state = _run_state(store, sha, narrative)
    verifier_node.run(state, FakeRetriever(store))

    assert fake_range not in state.narrative and fake_file not in state.narrative
    assert real in state.narrative, "verified citations are untouched"
    assert state.narrative.count(verify_mod.SCRUB_MARKER) == 2
    assert "2 citations could not be verified and were removed." in state.narrative
    assert "A notification is queued." in state.narrative, "the claim stays, the pointer goes"

    assert state.verification["answer"]["dropped"] and state.verification["answer"]["verified"] == 3
    assert state.impact_map.confidence == "medium", "40% unverifiable costs a level"
    assert any("could not be verified" in r for r in state.impact_map.confidence_reasons)


def test_an_answer_of_bare_file_paths_is_uncheckable_and_scores_low(store, sha):
    """Observed in a real bug-lane run: the architect listed each file as a bare
    backticked path with no sha and no line range. Nothing in it can be checked,
    so it cannot be `high` — and it must not borrow the impact map's citations to
    look like it can."""
    spec = (
        "**Files to change or inspect**\n"
        "- `src/main/java/org/springframework/samples/petclinic/owner/VisitController.java`"
        " - Check the validation block.\n"
        "- `src/main/resources/templates/owners/ownerDetails.html` - Inspect the loop.\n"
    )
    state = _run_state(store, sha, None, lane="bug")
    state.spec = spec
    good = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    state.impact_map.screens = [ImpactEntry("ownerDetails", good, "why")]
    verifier_node.run(state, FakeRetriever(store))

    assert state.verification["answer"]["checked"] == 0
    assert state.verification["impact_map"]["verified"] == 1, "the map itself was fine"
    assert state.impact_map.confidence == "low", "a good map cannot rescue an uncited answer"
    assert any("no citations" in r for r in state.impact_map.confidence_reasons)
    assert "repo@sha:path:start-end" in state.spec


def test_a_clean_answer_keeps_its_level_and_is_left_alone(store, sha):
    screen = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    narrative = f"1. It appears on the page.\n`{screen}`\n"
    state = _run_state(store, sha, narrative)
    verifier_node.run(state, FakeRetriever(store))
    assert state.narrative == narrative
    assert state.impact_map.confidence == "high"
    assert verify_mod.SCRUB_MARKER not in state.narrative


def test_the_verifier_drops_bad_rows_from_the_impact_map(store, sha):
    good = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    bad = f"spring-petclinic@{sha}:src/main/java/org/x/Nope.java:1-9"
    state = _run_state(store, sha, f"body `{good}`")
    state.impact_map.screens = [ImpactEntry("ownerDetails", good, "why")]
    state.impact_map.classes_methods = [ImpactEntry("Nope", bad, "why")]
    verifier_node.run(state, FakeRetriever(store))
    assert [e.citation for e in state.impact_map.all_entries()] == [good]


def test_the_bug_rule_fires_against_the_real_graph(store, sha):
    """Only the owner-details screen cited: the display side, and nothing else."""
    screen = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    state = _run_state(store, sha, f"Look at the page.\n`{screen}`", lane="bug")
    verifier_node.run(state, FakeRetriever(store))
    assert state.impact_map.confidence == "medium"
    assert any("write/persist" in r for r in state.impact_map.confidence_reasons)
    assert "What would make me more sure:" in state.narrative
    assert "saves this" in state.narrative


def test_the_bug_rule_is_satisfied_by_a_real_post_handler(store, sha):
    screen = f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96"
    handler = (
        f"spring-petclinic@{sha}:src/main/java/org/springframework/samples/petclinic/"
        "owner/VisitController.java:97-112"
    )
    state = _run_state(store, sha, f"Page.\n`{screen}`\nSave.\n`{handler}`", lane="bug")
    verifier_node.run(state, FakeRetriever(store))
    assert state.impact_map.confidence == "high"


def test_an_aborted_run_does_not_claim_a_confidence_it_never_checked():
    """A run that hits its time budget stops before the verifier. It still has a
    level — the retrieval base — but no coverage rule ran and no citation was
    checked, so the header must not present it as a scored one. Observed live:
    a 237s bug run aborted at the budget and printed a bare `confidence: high`."""
    from bridge.orchestrator import answer_header, compose_answer, confidence_note

    state = RunState(request="a visit isn't appearing")
    state.lane, state.sha = "bug", SHA
    state.env = {"repo": "petclinic", "indexed_at": "2026-09-07"}
    state.spec = "a partial brief"
    state.impact_map = ImpactMap(summary="s", confidence="high")
    state.abort_reason = "time budget exhausted (237.5s/150s)"

    assert "confidence: high (unverified)" in answer_header(state)
    assert "stopped before its citations were checked" in confidence_note(state)
    assert "Partial answer" in compose_answer(state)

    state.verification = {"answer": {"checked": 1}, "confidence": {"level": "high"}}
    assert "(unverified)" not in answer_header(state)
