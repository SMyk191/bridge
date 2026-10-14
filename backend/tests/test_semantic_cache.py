"""orchestrator/cache.py's semantic layer, and the RunStore machinery under
it. `best_semantic_match`/`_cosine` are pure — tested with plain vectors, no
encoder needed. `embed_question` is tested with a fake encoder (duck-typing
sentence-transformers' `.encode()`), since loading the real bge model is
exactly the cost this layer exists to avoid paying twice in a test suite.
`semantic_cache_candidates`/`cache_store` run against a real sqlite file — the
migration path (an `answer_cache` table from before the semantic cache exists)
is exactly the kind of thing worth proving for real.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bridge.orchestrator import cache
from bridge.orchestrator.store import RunStore


class FakeEncoder:
    """Returns whatever vector was registered for a given (normalised) text,
    so a test can construct exact similarity relationships instead of hoping
    a real model happens to produce them."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def encode(self, texts, normalize_embeddings=True):
        import numpy as np

        return np.array([self.vectors[t] for t in texts])


# ------------------------------------------------------------------ cheap_lane_guess


def test_cheap_lane_guess_matches_the_interpreter_fallback():
    assert cache.cheap_lane_guess("How many owners signed up last month?") == "data_question"
    assert cache.cheap_lane_guess("The visit isn't showing up, it's broken") == "bug"
    assert cache.cheap_lane_guess("Can we add a weight field?") == "change_request"
    assert cache.cheap_lane_guess("How does search work?") == "question"


# ------------------------------------------------------------------ embed_question


def test_embed_question_normalises_before_encoding():
    """Two questions differing only in case/punctuation/whitespace must embed
    identically — question_norm runs before the encoder ever sees the text."""
    encoder = FakeEncoder({"how does search work": [1.0, 0.0, 0.0]})
    a = cache.embed_question("How does search work?", encoder)
    b = cache.embed_question("  how does search work  ", encoder)
    assert a == b == [1.0, 0.0, 0.0]


# ------------------------------------------------------------------ best_semantic_match


def test_an_identical_vector_matches_at_similarity_one():
    candidates = [{"run_id": "r1", "embedding": [1.0, 0.0]}]
    match = cache.best_semantic_match([1.0, 0.0], candidates, threshold=0.95)
    assert match["run_id"] == "r1"
    assert match["similarity"] == pytest.approx(1.0)


def test_an_orthogonal_vector_never_matches():
    candidates = [{"run_id": "r1", "embedding": [0.0, 1.0]}]
    assert cache.best_semantic_match([1.0, 0.0], candidates, threshold=0.95) is None


def test_below_threshold_is_not_a_match():
    # cos(30deg) ~= 0.866, below a 0.95 threshold
    import math

    candidates = [{"run_id": "r1", "embedding": [math.cos(math.radians(30)), math.sin(math.radians(30))]}]
    assert cache.best_semantic_match([1.0, 0.0], candidates, threshold=0.95) is None


def test_the_closest_of_several_candidates_wins():
    candidates = [
        {"run_id": "far", "embedding": [0.96, 0.28]},
        {"run_id": "closer", "embedding": [0.999, 0.045]},
    ]
    match = cache.best_semantic_match([1.0, 0.0], candidates, threshold=0.95)
    assert match["run_id"] == "closer"


def test_no_candidates_means_no_match():
    assert cache.best_semantic_match([1.0, 0.0], [], threshold=0.95) is None


def test_a_zero_vector_scores_zero_rather_than_dividing_by_zero():
    """A degenerate embedding must not raise — it just never clears a real
    threshold, since cosine similarity against a zero vector is undefined and
    0.0 is the honest "no similarity" answer, not a crash."""
    candidates = [{"run_id": "r1", "embedding": [0.0, 0.0]}]
    assert cache.best_semantic_match([1.0, 0.0], candidates, threshold=0.95) is None


# ------------------------------------------------------------------ RunStore persistence


@pytest.fixture()
def store(tmp_path):
    with RunStore.open(tmp_path / "runs.sqlite") as s:
        yield s


def test_cache_store_without_lane_or_embedding_still_works_for_the_exact_cache(store):
    """The pre-existing exact-cache call sites never pass lane/embedding —
    they must keep working exactly as before."""
    store.cache_store("snap1", "q1", "run1")
    assert store.cache_lookup("snap1", "q1") == "run1"
    assert store.semantic_cache_candidates("snap1", "question") == []


def test_cache_store_with_lane_and_embedding_is_a_semantic_candidate(store):
    store.cache_store("snap1", "how does search work", "run1", lane="question",
                      embedding=[1.0, 0.0, 0.0])
    candidates = store.semantic_cache_candidates("snap1", "question")
    assert len(candidates) == 1
    assert candidates[0]["run_id"] == "run1"
    assert candidates[0]["embedding"] == [1.0, 0.0, 0.0]


def test_semantic_candidates_are_scoped_to_snapshot_and_lane(store):
    store.cache_store("snap1", "q1", "run1", lane="question", embedding=[1.0, 0.0])
    store.cache_store("snap1", "q2", "run2", lane="bug", embedding=[1.0, 0.0])
    store.cache_store("snap2", "q3", "run3", lane="question", embedding=[1.0, 0.0])
    candidates = store.semantic_cache_candidates("snap1", "question")
    assert [c["run_id"] for c in candidates] == ["run1"]


def test_updating_a_cache_entry_replaces_its_lane_and_embedding(store):
    store.cache_store("snap1", "q1", "run1", lane="question", embedding=[1.0, 0.0])
    store.cache_store("snap1", "q1", "run2", lane="bug", embedding=[0.0, 1.0])
    assert store.cache_lookup("snap1", "q1") == "run2"
    assert store.semantic_cache_candidates("snap1", "question") == []
    candidates = store.semantic_cache_candidates("snap1", "bug")
    assert candidates[0]["run_id"] == "run2"


def test_an_answer_cache_table_from_before_the_semantic_layer_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE answer_cache (
          snapshot_key TEXT NOT NULL, question_norm TEXT NOT NULL,
          run_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (snapshot_key, question_norm)
        );
    """)
    connection.execute(
        "INSERT INTO answer_cache VALUES ('snap1', 'q1', 'run1', datetime('now'))"
    )
    connection.commit()
    connection.close()

    with RunStore.open(path) as store:
        assert store.cache_lookup("snap1", "q1") == "run1"
        # the migrated row has no embedding yet — never a semantic candidate
        assert store.semantic_cache_candidates("snap1", "question") == []
        store.cache_store("snap1", "q2", "run2", lane="question", embedding=[1.0])
        assert len(store.semantic_cache_candidates("snap1", "question")) == 1


def test_opening_an_already_migrated_answer_cache_twice_does_not_error(tmp_path):
    path = tmp_path / "runs.sqlite"
    RunStore.open(path).close()
    RunStore.open(path).close()
