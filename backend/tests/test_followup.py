"""Follow-up retrieval: when a run goes back for more evidence, and what it costs.

The rules are pure functions over the state, so almost everything here runs
without an index or a model. The exceptions are the two that make claims about a
real repository — that a bug lane's side-coverage is read off the *impact map*,
and that petclinic genuinely holds no schema files for rule (c) to find.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge import config
from bridge.agents.llm import Budget, Usage
from bridge.agents.state import ImpactEntry, ImpactMap, Interpretation, RunState
from bridge.index.graph import GraphStore
from bridge.orchestrator import confidence as conf
from bridge.orchestrator import followup

SHA = "88e37c15cf6fc8490b01bc3e8e2c800cec1ac272"
PKG = "org.springframework.samples.petclinic"


class Chunk:
    """The fields followup/verify read off a retrieval Chunk."""

    def __init__(self, node_id, path, start=1, end=20, kind="method", name="m", score=5.0):
        self.node_id, self.path, self.kind, self.name = node_id, path, kind, name
        self.start_line, self.end_line, self.score = start, end, score
        self.citation = f"petclinic@{SHA}:{path}:{start}-{end}"
        self.rerank_score, self.history = score, None


class Retrieval:
    def __init__(self, chunks=(), confidence="high"):
        self.chunks = list(chunks)
        self.confidence, self.confidence_reason = confidence, "best 9.0/10"
        self.question, self.cost_usd = "q", 0.0
        self.reranked, self.rerank_unavailable_reason = True, None


def make_state(lane="bug", *, gaps=(), entities=(), chunks=(), map_entries=()) -> RunState:
    state = RunState(request="a visit isn't appearing on the owner page")
    state.lane, state.sha = lane, SHA
    state.interpretation = Interpretation(
        intent=lane, normalised_request=state.request, entities=list(entities)
    )
    state.retrieval = Retrieval(chunks)
    state.impact_map = ImpactMap(summary="s", gaps=list(gaps))
    state.impact_map.classes_methods = list(map_entries)
    return state


# ------------------------------------------------------------------ budget headroom


def test_headroom_is_the_tightest_ceiling_not_the_average():
    """90% of the money and 5% of the clock is 5% of a budget, not 47%."""
    budget = Budget(max_usd=1.0, max_calls=100, max_input_tokens=None, max_seconds=None)
    usage = Usage(calls=95, cost_usd=0.10)
    assert budget.fraction_remaining(usage) == pytest.approx(0.05)
    assert budget.tightest_ceiling(usage) == "calls"


def test_an_unbounded_budget_has_full_headroom():
    assert Budget.unlimited().fraction_remaining(Usage(calls=999)) == 1.0


def test_headroom_never_goes_negative():
    budget = Budget(max_usd=1.0, max_calls=None, max_input_tokens=None, max_seconds=None)
    assert budget.fraction_remaining(Usage(cost_usd=5.0)) == 0.0


# ------------------------------------------------------------------ gap terms


def test_gap_terms_keep_the_content_words_in_order():
    gaps = ["The database repository layer (or SQL queries) that persists the visit data."]
    terms = followup.gap_terms(gaps)
    assert terms[:4] == ["database", "repository", "layer", "sql"]
    assert "the" not in terms and "that" not in terms


def test_gap_terms_dedupe_and_cap():
    terms = followup.gap_terms(["visit visit visit " + " ".join(f"w{i}" for i in range(40))])
    assert terms.count("visit") == 1
    assert len(terms) <= 24


# ------------------------------------------------------------------ rule (a): gaps


def test_a_named_gap_earns_a_search():
    state = make_state("question", gaps=["the persistence layer was not retrieved"])
    shots = followup.plan_shots(state)
    assert [s.rule for s in shots] == [followup.RULE_GAPS]
    assert "persistence" in shots[0].query


def test_no_gap_no_shot():
    assert followup.plan_shots(make_state("question")) == []


# ------------------------------------------------------------------ rule (b): sides


class FakeStore:
    """A graph with one read node and one persisting write node, by path."""

    ROWS = {
        "read.html": (1, 100, "n-read", "screen", config.IO_READ, config.IO_SOURCE_SCREEN),
        "write.java": (1, 100, "n-write", "method", config.IO_WRITE, config.IO_SOURCE_ROUTE),
        "setter.java": (1, 100, "n-set", "method", config.IO_WRITE, config.IO_SOURCE_ACCESSOR),
        "entity.java": (1, 100, "n-ent", "entity", config.IO_NONE, ""),
        "schema.sql": (1, 10, "n-schema", "file", config.IO_NONE, ""),
    }

    def __init__(self, paths=None, has_schema=False):
        self.paths = paths or ["read.html", "write.java", "setter.java", "entity.java"]
        if has_schema:
            self.paths = self.paths + ["db/migration/schema.sql"]
        self._has_schema = has_schema

    class _Conn:
        def __init__(self, outer):
            self.outer = outer

        def execute(self, sql, params=()):
            outer = self.outer

            class _C:
                def fetchone(self_inner):
                    return (1,) if outer._has_schema else None

                def __iter__(self_inner):
                    for path in outer.paths:
                        row = FakeStore.ROWS.get(Path(path).name, (1, 100, "n", "file",
                                                                   config.IO_NONE, ""))
                        yield {
                            "id": row[2], "kind": row[3], "path": path,
                            "start_line": row[0], "end_line": row[1],
                            "attrs": f'{{"io_role": "{row[4]}", "io_source": "{row[5]}"}}',
                        }

            return _C()

    def node(self, node_id):
        return None

    @property
    def conn(self):
        return self._Conn(self)


def _entry(path):
    return ImpactEntry("x", f"petclinic@{SHA}:{path}:1-20", "why")


def test_a_bug_mapping_only_the_display_path_searches_for_the_write_path():
    state = make_state("bug", map_entries=[_entry("read.html")])
    shots = followup.plan_shots(state, FakeStore())
    assert [s.rule for s in shots] == [followup.RULE_WRITE_SIDE]
    for word in config.FOLLOWUP_WRITE_TERMS.split():
        assert word in shots[0].terms


def test_a_bug_mapping_only_the_write_path_searches_the_other_way():
    state = make_state("bug", map_entries=[_entry("write.java")])
    shots = followup.plan_shots(state, FakeStore())
    assert [s.rule for s in shots] == [followup.RULE_READ_SIDE]
    assert "display" in shots[0].reason


def test_a_bug_covering_both_sides_needs_no_shot():
    state = make_state("bug", map_entries=[_entry("read.html"), _entry("write.java")])
    assert followup.plan_shots(state, FakeStore()) == []


def test_a_setter_does_not_satisfy_the_write_side_here_either():
    """Same persistence rule as confidence.py — one definition, applied twice."""
    state = make_state("bug", map_entries=[_entry("read.html"), _entry("setter.java")])
    assert [s.rule for s in followup.plan_shots(state, FakeStore())] == [followup.RULE_WRITE_SIDE]


def test_side_coverage_is_read_off_the_map_not_off_raw_retrieval():
    """The bug this alignment fixes: `processNewVisitForm` sat in retrieval the
    whole time while the map ignored it, so the trigger saw a write and declined
    to fire while the rule judging the answer saw none."""
    state = make_state(
        "bug",
        chunks=[Chunk("n-write", "write.java")],   # retrieval HAS the write side
        map_entries=[_entry("read.html")],          # the map does not
    )
    assert [s.rule for s in followup.plan_shots(state, FakeStore())] == [followup.RULE_WRITE_SIDE]


def test_entity_names_anchor_the_search():
    state = make_state("bug", entities=["visit"], map_entries=[_entry("read.html")])
    state.impact_map.entities_tables = [ImpactEntry("Visit", f"petclinic@{SHA}:entity.java:1-20", "w")]
    shot = followup.plan_shots(state, FakeStore())[0]
    assert "Visit" in shot.terms and "Visit" in shot.query


# ------------------------------------------------------------------ rule (c): schema


def test_an_entity_change_with_no_schema_in_evidence_searches_for_one():
    state = make_state("change_request", chunks=[Chunk("n-ent", "entity.java", kind="entity")])
    shots = followup.plan_shots(state, FakeStore(has_schema=True))
    assert [s.rule for s in shots] == [followup.RULE_SCHEMA]
    assert shots[0].query and not shots[0].skipped_reason


def test_rule_c_does_not_spend_a_search_where_no_schema_file_is_indexed():
    """petclinic's indexer walks .java and .html only, so its db/*/schema.sql are
    not nodes. Searching would find nothing, every time."""
    state = make_state("change_request", chunks=[Chunk("n-ent", "entity.java", kind="entity")])
    shots = followup.plan_shots(state, FakeStore(has_schema=False))
    assert shots[0].skipped_reason
    assert not shots[0].query, "a shot with nothing to look for must not be fired"


def test_rule_c_stays_quiet_when_a_schema_file_is_already_in_evidence():
    state = make_state(
        "change_request",
        chunks=[Chunk("n-ent", "entity.java", kind="entity"),
                Chunk("n-s", "src/main/resources/db/migration/V1__init.sql", kind="file")],
    )
    assert followup.plan_shots(state, FakeStore(has_schema=True)) == []


def test_rule_c_stays_quiet_on_a_change_that_touches_no_entity():
    state = make_state("change_request", chunks=[Chunk("n1", "read.html", kind="screen")])
    assert followup.plan_shots(state, FakeStore(has_schema=True)) == []


def test_rules_are_considered_in_order():
    state = make_state("bug", gaps=["a gap"], map_entries=[_entry("read.html")])
    assert [s.rule for s in followup.plan_shots(state, FakeStore())] == [
        followup.RULE_GAPS, followup.RULE_WRITE_SIDE,
    ]


# ------------------------------------------------------------------ the ceilings


class FakeRetriever:
    def __init__(self, store, per_call=()):
        self.store = store
        self._per_call = list(per_call)
        self.calls = 0

    def retrieve(self, query, budget, llm=None, extra_terms=None):
        self.calls += 1
        chunks = self._per_call[self.calls - 1] if self.calls <= len(self._per_call) else []
        return Retrieval(chunks)

    def rerank(self, question, candidates, llm, redaction):
        return True, None, 0.0


class FakeLLM:
    def available_for(self, role):
        return False

    def reason_for(self, role):
        return "disabled in this test"


def test_a_run_below_the_budget_floor_fires_nothing_and_says_so():
    state = make_state("bug", gaps=["a gap"], map_entries=[_entry("read.html")])
    state.budget = Budget(max_usd=1.0, max_calls=None, max_input_tokens=None, max_seconds=None)
    state.usage = Usage(cost_usd=0.95)  # 5% left, floor is 30%
    followup.run(state, FakeLLM(), FakeRetriever(FakeStore()))

    assert state.followups["fired"] == 0
    for shot in state.followups["shots"]:
        assert "below the" in shot["skipped_reason"]
        assert "cost is tightest" in shot["skipped_reason"]


def test_no_more_than_two_shots_ever_fire(monkeypatch):
    """Asserted against the literal 2, not against the constant — a test that
    reads the same config value it is guarding passes when that value is wrong.

    Four shots have to be injected to test this at all: rules (b) and (c) live on
    mutually exclusive lanes, so the real rule set can plan at most two. The cap
    is a guard against a future rule, not a constraint that binds today.
    """
    planned = [
        followup.Shot(rule=f"r{i}", reason="r", query="q") for i in range(4)
    ]
    monkeypatch.setattr(followup, "plan_shots", lambda state, store=None: planned)
    state = make_state("bug")
    state.budget = Budget.unlimited().start()
    retriever = FakeRetriever(FakeStore(), per_call=[[Chunk(f"x{i}", "read.html")] for i in range(5)])
    followup.run(state, FakeLLM(), retriever)

    assert retriever.calls == 2
    assert sum(1 for shot in planned if shot.fired) == 2
    assert all("2 extra searches" in s.skipped_reason for s in planned if not s.fired)


def test_a_skipped_shot_is_recorded_in_the_confidence_reasons():
    """A run that knew what was missing and could not afford to look is a
    materially different answer from one with no known holes."""
    followups = {
        "shots": [
            {
                "rule": "write_side",
                "reason": "a bug needs both sides of the flow",
                "fired": False,
                "skipped_reason": "12% of the run's budget was left, below the 30% needed",
            }
        ]
    }
    verdict = conf.ConfidenceVerdict(level="high")
    conf._followup_reasons(verdict, followups)
    assert any("could not afford" in item for item in verdict.missing)
    assert any("both sides of the flow" in reason for reason in verdict.reasons)
    assert any("12%" in reason for reason in verdict.reasons)


def test_a_fired_shot_adds_no_confidence_reason():
    followups = {"shots": [{"rule": "gaps", "reason": "r", "fired": True, "skipped_reason": ""}]}
    verdict = conf.ConfidenceVerdict(level="high")
    conf._followup_reasons(verdict, followups)
    assert verdict.reasons == [] and verdict.missing == []


# ------------------------------------------------------------------ the merge


def test_the_merge_unions_dedupes_and_grows_the_cut_per_shot():
    state = make_state("bug")
    state.retrieval = Retrieval([Chunk(f"o{i}", "read.html", score=10 - i) for i in range(15)])
    new = {"write_side": [Chunk(f"n{i}", "write.java", score=1.0) for i in range(10)]}
    retriever = FakeRetriever(FakeStore())
    count, _ = followup.merge(state, new, FakeLLM(), retriever)

    ids = [c.node_id for c in state.retrieval.chunks]
    assert len(ids) == len(set(ids)), "deduped"
    assert count == config.RETRIEVE_TOP_K + config.FOLLOWUP_EXTRA_PER_SHOT


def test_an_already_known_chunk_is_not_added_twice():
    state = make_state("bug")
    shared = Chunk("same", "read.html", score=9.0)
    state.retrieval = Retrieval([shared])
    count, _ = followup.merge(
        state, {"gaps": [Chunk("same", "read.html", score=1.0)]}, FakeLLM(), FakeRetriever(FakeStore())
    )
    assert count == 1 and state.retrieval.chunks[0].score == 9.0


def test_each_shot_keeps_a_share_even_when_it_scores_below_the_cut():
    """Two shots answer two different holes. Letting the stronger one take every
    new slot silently drops the other's answer."""
    state = make_state("bug")
    state.retrieval = Retrieval([Chunk(f"o{i}", "read.html", score=10.0) for i in range(15)])
    new = {
        "gaps": [Chunk(f"g{i}", "read.html", score=9.0) for i in range(10)],
        "write_side": [Chunk(f"w{i}", "write.java", score=0.1) for i in range(10)],
    }
    followup.merge(state, new, FakeLLM(), FakeRetriever(FakeStore()))
    kept = {c.node_id for c in state.retrieval.chunks}
    # 15 chunks at 10.0 and 10 at 9.0 sit above every write chunk, and the cut is
    # 15 + 6*2 = 27, so score alone lets exactly 2 write chunks through. The floor
    # is what makes it 3. Asserted as a literal: reading FOLLOWUP_MIN_PER_SHOT
    # here would make the test agree with whatever the constant happened to say.
    assert sum(1 for i in range(10) if f"w{i}" in kept) == 3


# ------------------------------------------------------------------ against the real graph


def _latest_index() -> Path | None:
    candidates = [p for p in config.INDEX_ROOT.glob("*") if (p / config.GRAPH_DB_NAME).is_file()]
    return max(candidates, key=lambda p: (p / config.GRAPH_DB_NAME).stat().st_mtime, default=None)


@pytest.fixture(scope="module")
def store():
    path = _latest_index()
    if path is None:
        pytest.skip("no index built")
    graph = GraphStore.open(path / config.GRAPH_DB_NAME)
    yield graph
    graph.close()


def test_petclinic_really_has_no_indexed_schema_files(store):
    """The finding rule (c) is gated on: the indexer walks `.java` and `.html`,
    so `src/main/resources/db/{h2,mysql,postgres}/schema.sql` are invisible — and
    "the schema migration" is the single most common gap the cartographer reports."""
    assert not followup._graph_has_schema_files(store)
    suffixes = {row["path"].rsplit(".", 1)[-1] for row in store.conn.execute(
        "SELECT DISTINCT path FROM nodes")}
    assert suffixes == {"java", "html"}


def test_the_write_side_rule_fires_on_the_real_visit_flow(store):
    """Only the owner-details screen mapped: the display half of the flow."""
    state = make_state("bug", map_entries=[
        ImpactEntry(
            "ownerDetails",
            f"spring-petclinic@{store.get_meta('head_sha')}"
            ":src/main/resources/templates/owners/ownerDetails.html:1-96",
            "shows the page",
        )
    ])
    state.sha = store.get_meta("head_sha")
    assert [s.rule for s in followup.plan_shots(state, store)] == [followup.RULE_WRITE_SIDE]


def test_the_real_post_handler_satisfies_the_rule(store):
    sha = store.get_meta("head_sha")
    state = make_state("bug", map_entries=[
        ImpactEntry("ownerDetails",
                    f"spring-petclinic@{sha}:src/main/resources/templates/owners/ownerDetails.html:1-96",
                    "w"),
        ImpactEntry("processNewVisitForm",
                    f"spring-petclinic@{sha}:src/main/java/org/springframework/samples/"
                    "petclinic/owner/VisitController.java:97-112", "w"),
    ])
    state.sha = sha
    assert followup.plan_shots(state, store) == []
