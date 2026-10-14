"""work/vocab_staleness.py — dead-node detection against a real GraphStore, and
the same-name candidate heuristic named honestly as one. `check()`'s write into
`vocab_proposals` is exercised against a real sqlite file (api/db.py), not
mocked — "does INSERT actually leave a readable row" is exactly the behaviour
worth proving.
"""

from __future__ import annotations

import json

import pytest

from bridge.api import db as vocab_db
from bridge.index.graph import GraphStore
from bridge.index.model import Node
from bridge.retrieval.vocab import Vocabulary, VocabTerm
from bridge.work import vocab_staleness


@pytest.fixture()
def store(tmp_path):
    with GraphStore.open(tmp_path / "graph.db", fresh=True) as s:
        yield s


def _node(id_: str, kind: str, name: str, path: str = "Foo.java") -> Node:
    return Node(id=id_, kind=kind, name=name, path=path, qualified_name=id_.split(":", 1)[1])


def test_a_term_naming_a_live_node_is_not_stale(store):
    store.add_nodes([_node("java:pkg.Owner", "class", "Owner")])
    vocab = Vocabulary([VocabTerm(term="owner", aliases=["owner"], nodes=["java:pkg.Owner"])])
    assert vocab_staleness.stale_terms(vocab, store) == []


def test_a_term_naming_a_missing_node_is_stale(store):
    vocab = Vocabulary([VocabTerm(term="owner", aliases=["owner"], nodes=["java:pkg.Owner"])])
    stale = vocab_staleness.stale_terms(vocab, store)
    assert len(stale) == 1
    assert stale[0].dead_nodes == ["java:pkg.Owner"]


def test_a_term_with_several_nodes_only_some_dead_reports_only_the_dead_ones(store):
    store.add_nodes([_node("java:pkg.Owner", "class", "Owner")])
    vocab = Vocabulary([VocabTerm(term="owner stuff", aliases=["owner stuff"],
                                 nodes=["java:pkg.Owner", "java:pkg.OwnerGone"])])
    stale = vocab_staleness.stale_terms(vocab, store)
    assert stale[0].dead_nodes == ["java:pkg.OwnerGone"]


# ------------------------------------------------------------------ _bare_name / candidates


@pytest.mark.parametrize("node_id,expected", [
    ("java:pkg.Owner", ("type", "Owner")),
    ("java:pkg.Owner#findById", ("method", "findById")),
    ("java:pkg.Owner#findById(Integer)", ("method", "findById")),
    ("entity:pkg.Owner", ("entity", "Owner")),
    ("screen:owners/ownerDetails", ("screen", "ownerDetails")),
    ("file:src/main/Owner.java", ("file", "Owner.java")),
])
def test_bare_name_parses_every_scheme(node_id, expected):
    assert vocab_staleness._bare_name(node_id) == expected


def test_bare_name_is_none_for_an_unrecognised_scheme():
    assert vocab_staleness._bare_name("weird:thing") is None


def test_a_same_name_live_node_is_proposed_as_a_candidate(store):
    """The heuristic this module is honest about: same kind of id, same bare
    name, elsewhere in the CURRENT graph — not a verified rename."""
    store.add_nodes([_node("java:pkg.owner.OwnerController", "class", "OwnerController",
                          path="new/OwnerController.java")])
    candidates = vocab_staleness.find_candidates(
        store, "java:pkg.legacy.OwnerController", exclude="java:pkg.legacy.OwnerController"
    )
    assert candidates == ["java:pkg.owner.OwnerController"]


def test_no_same_name_node_means_no_candidates(store):
    assert vocab_staleness.find_candidates(store, "java:pkg.Nothing", exclude="java:pkg.Nothing") == []


def test_a_dead_node_cannot_be_its_own_candidate(store):
    """If the dead id happens to still match a live row by pure name collision,
    excluding by id (not by row identity) keeps this honest — but the dead id
    itself never appears in its own candidate list by construction."""
    store.add_nodes([_node("java:pkg.Owner", "class", "Owner")])
    candidates = vocab_staleness.find_candidates(store, "java:pkg.Owner", exclude="java:pkg.Owner")
    assert "java:pkg.Owner" not in candidates


# ------------------------------------------------------------------ check() end to end


@pytest.fixture()
def vocab_store(tmp_path, monkeypatch):
    from bridge import config

    monkeypatch.setattr(config, "RUNS_DB", tmp_path / "runs.sqlite")
    return None


def test_check_writes_a_proposal_only_when_a_candidate_exists(store, vocab_store):
    store.add_nodes([_node("java:pkg.owner.OwnerController", "class", "OwnerController",
                          path="new/OwnerController.java")])
    vocab = Vocabulary([
        VocabTerm(term="owner page", aliases=["owner page"],
                 nodes=["java:pkg.legacy.OwnerController"]),
        VocabTerm(term="dead end", aliases=["dead end"], nodes=["java:pkg.NothingLike"]),
    ])
    result = vocab_staleness.check(vocab, store, "demo")
    assert result["stale_terms"] == 2
    assert result["dead_nodes"] == 2
    assert result["proposals_written"] == 1  # only "owner page" had a candidate

    proposals = vocab_db.list_vocab_proposals()
    assert len(proposals) == 1
    assert proposals[0]["term"] == "owner page"
    assert proposals[0]["entities"] == ["java:pkg.owner.OwnerController"]
    assert proposals[0]["status"] == "pending"
    assert "Heuristic match" in proposals[0]["note"]


def test_check_writes_nothing_when_nothing_is_stale(store, vocab_store):
    store.add_nodes([_node("java:pkg.Owner", "class", "Owner")])
    vocab = Vocabulary([VocabTerm(term="owner", aliases=["owner"], nodes=["java:pkg.Owner"])])
    result = vocab_staleness.check(vocab, store, "demo")
    assert result["stale_terms"] == 0
    assert result["proposals_written"] == 0
    assert vocab_db.list_vocab_proposals() == []


def test_review_endpoint_logic_approves_a_written_proposal(store, vocab_store):
    store.add_nodes([_node("java:pkg.owner.OwnerController", "class", "OwnerController")])
    vocab = Vocabulary([VocabTerm(term="owner page", aliases=["owner page"],
                                 nodes=["java:pkg.legacy.OwnerController"])])
    vocab_staleness.check(vocab, store, "demo")
    proposal_id = vocab_db.list_vocab_proposals()[0]["id"]

    updated = vocab_db.review_vocab_proposal(proposal_id, "approved")
    assert updated["status"] == "approved"
    assert vocab_db.review_vocab_proposal(999999, "approved") is None
