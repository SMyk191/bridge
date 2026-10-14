"""The persistent index against a real PostgreSQL 16 + pgvector.

Not a mock and not SQLite-pretending-to-be-Postgres: `pgserver` starts the same
server `docker-compose.yml` runs, from the venv, and these tests talk to it over
the same SQLAlchemy engine the indexer uses. `ON CONFLICT ... RETURNING`, JSONB
and `vector` cosine distance all behave differently (or not at all) on other
engines, and every one of them is load-bearing here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bridge import config
from bridge.db import create_all, get_engine, keys
from bridge.db.repo import IndexRepo
from bridge.index import pgindex


@pytest.fixture(scope="module")
def engine():
    """One embedded Postgres for this module, on its own data directory."""
    pytest.importorskip("pgserver")
    eng = get_engine("embedded://pytest_pg_index")
    create_all(eng)
    with eng.begin() as connection:
        from sqlalchemy import text

        # a module-scoped database that starts clean, so counts are absolute
        # `references` is a reserved SQL keyword, so raw statements must quote
        # it. Every generated query goes through SQLAlchemy, which quotes
        # identifiers itself — these fixtures are the only raw SQL in the phase.
        for table in (
            "snapshot_files", "snapshots", "vectors", "descriptions", "edges",
            "references", "chunks", "blobs", "projects", "environments",
        ):
            connection.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    return eng


@pytest.fixture()
def repo(engine):
    return IndexRepo(engine)


def _blob(repo: IndexRepo, blob="b" * 64, lang="java") -> str:
    repo.add_blobs([{"blob_hash": blob, "lang": lang, "size_bytes": 10, "parsed_json": {}}])
    return blob


def _chunk(blob: str, code="return 1;", name="m", qualified="C#m", start=1) -> dict:
    return {
        "chunk_hash": keys.chunk_hash(blob, "method", qualified, start, start + 3),
        "blob_hash": blob,
        "code_hash": keys.code_hash("method", qualified, code),
        "kind": "method", "name": name, "qualified_name": qualified,
        "breadcrumb": f"demo › {qualified}", "start_line": start, "end_line": start + 3,
        "code": code, "tier": config.TIER_FULL, "project": "demo:.", "attrs": {},
    }


# ------------------------------------------------------------------ writes


def test_inserts_report_rows_that_were_actually_new(repo):
    """`rowcount` is -1 for a multi-row ON CONFLICT DO NOTHING under psycopg, and
    every verification in this phase asserts on these counts — so they come from
    RETURNING instead."""
    blob = _blob(repo, "a" * 64)
    rows = [_chunk(blob, qualified=f"C#m{i}", start=i * 10) for i in range(5)]
    assert repo.add_chunks(rows) == 5
    assert repo.add_chunks(rows) == 0, "re-writing identical content must be free"


def test_writing_the_same_content_twice_is_idempotent(repo):
    """The whole storage model: a hash collision IS the row, so a conflict means
    'already have it', never 'overwrite it'."""
    blob = _blob(repo, "c" * 64)
    row = _chunk(blob, qualified="C#idem")
    repo.add_chunks([row])
    repo.add_chunks([{**row, "code": "TAMPERED"}])
    with repo.engine.connect() as connection:
        from sqlalchemy import select

        from bridge.db import schema

        code = connection.execute(
            select(schema.chunks.c.code).where(schema.chunks.c.chunk_hash == row["chunk_hash"])
        ).scalar_one()
    assert code == "return 1;", "an existing row must not be silently rewritten"


def test_jsonb_round_trips(repo):
    blob = _blob(repo, "d" * 64)
    row = _chunk(blob, qualified="C#json")
    row["attrs"] = {"nested": {"a": [1, 2, 3]}, "flag": True}
    repo.add_chunks([row])
    with repo.engine.connect() as connection:
        from sqlalchemy import select

        from bridge.db import schema

        attrs = connection.execute(
            select(schema.chunks.c.attrs).where(schema.chunks.c.chunk_hash == row["chunk_hash"])
        ).scalar_one()
    assert attrs == {"nested": {"a": [1, 2, 3]}, "flag": True}


# ------------------------------------------------------------------ the reuse question


def test_existing_descriptions_is_the_question_the_indexer_actually_asks(repo):
    """Everything not in this set is a model call someone is about to pay for."""
    code = keys.code_hash("method", "C#x", "return 1;")
    key = keys.desc_key(code, "model-a")
    assert repo.existing_descriptions([key]) == set()
    repo.add_descriptions([{
        "desc_key": key, "chunk_hash": "h" * 64, "code_hash": code,
        "model": "model-a", "prompt_version": keys.PROMPT_VERSION,
        "text": "does a thing", "source": "model-a",
    }])
    assert repo.existing_descriptions([key]) == {key}


def test_a_model_description_wins_over_a_template_one(repo):
    """Both are keyed on the same code_hash by different models. Retrieval must
    read the one a model actually wrote, whichever was stored first."""
    code = keys.code_hash("method", "C#pref", "return 2;")
    repo.add_descriptions([
        {
            "desc_key": keys.desc_key(code, keys.SOURCE_TEMPLATE), "chunk_hash": "h" * 64,
            "code_hash": code, "model": keys.SOURCE_TEMPLATE,
            "prompt_version": keys.PROMPT_VERSION, "text": "Reads the thing.",
            "source": keys.SOURCE_TEMPLATE,
        },
        {
            "desc_key": keys.desc_key(code, "gemini"), "chunk_hash": "h" * 64,
            "code_hash": code, "model": "gemini", "prompt_version": keys.PROMPT_VERSION,
            "text": "Saves a pet to its owner.", "source": "gemini",
        },
    ])
    assert repo.descriptions_for([code])[code]["text"] == "Saves a pet to its owner."


# ------------------------------------------------------------------ snapshots


def test_a_snapshot_is_just_a_path_to_blob_mapping(repo):
    blob = _blob(repo, "e" * 64)
    snapshot = keys.snapshot_id("demo", "sha1")
    repo.create_snapshot(snapshot, "demo", "sha1", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "src/A.java", "blob_hash": blob, "project": "demo:."},
    ])
    assert repo.snapshot_files(snapshot) == {"src/A.java": blob}


def test_the_same_blob_at_two_paths_is_stored_once(repo):
    """Two snapshots, one blob — the property that makes snapshots cheap."""
    blob = _blob(repo, "f" * 64)
    for sha, path in (("sha-a", "old/A.java"), ("sha-b", "new/A.java")):
        snapshot = keys.snapshot_id("demo", sha)
        repo.create_snapshot(snapshot, "demo", sha, {})
        repo.set_snapshot_files(snapshot, [
            {"snapshot_id": snapshot, "path": path, "blob_hash": blob, "project": "demo:."},
        ])
    assert repo.snapshot_files(keys.snapshot_id("demo", "sha-a")) == {"old/A.java": blob}
    assert repo.snapshot_files(keys.snapshot_id("demo", "sha-b")) == {"new/A.java": blob}


def test_re_indexing_a_commit_updates_its_stats_not_its_identity(repo):
    snapshot = keys.snapshot_id("demo", "sha-idem")
    repo.create_snapshot(snapshot, "demo", "sha-idem", {"pass": 1})
    repo.create_snapshot(snapshot, "demo", "sha-idem", {"pass": 2})
    assert repo.snapshot_by_sha("demo", "sha-idem")["stats"] == {"pass": 2}


def test_rewriting_a_snapshot_file_list_does_not_touch_content(repo):
    blob = _blob(repo, "0" * 64)
    snapshot = keys.snapshot_id("demo", "sha-rewrite")
    repo.create_snapshot(snapshot, "demo", "sha-rewrite", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "a.java", "blob_hash": blob, "project": "demo:."}])
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "b.java", "blob_hash": blob, "project": "demo:."}])
    assert repo.snapshot_files(snapshot) == {"b.java": blob}
    assert blob in repo.existing_blobs([blob]), "the blob itself must survive"


def test_an_orphan_blob_reference_is_reported(repo):
    """Should never happen; if it does, something wrote a file list without the
    content it points at, and an answer could cite a blob nobody stored."""
    snapshot = keys.snapshot_id("demo", "sha-orphan")
    repo.create_snapshot(snapshot, "demo", "sha-orphan", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "ghost.java", "blob_hash": "9" * 64, "project": "x"}])
    assert repo.orphan_blobs(snapshot) == ["ghost.java"]


# ------------------------------------------------------------------ vectors


def test_cosine_search_returns_the_nearest_chunk_first(repo):
    """Exercises pgvector's operator, the HNSW index, and the three-way join from
    vectors through descriptions to chunks and out to a snapshot's paths."""
    blob = _blob(repo, "1" * 64)
    snapshot = keys.snapshot_id("demo", "sha-vec")
    repo.create_snapshot(snapshot, "demo", "sha-vec", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "src/V.java", "blob_hash": blob, "project": "demo:."}])

    near = [1.0] + [0.0] * (config.EMBED_DIMENSIONS - 1)
    far = [0.0] * (config.EMBED_DIMENSIONS - 1) + [1.0]
    for label, vector in (("near", near), ("far", far)):
        code = keys.code_hash("method", f"C#{label}", label)
        dk = keys.desc_key(code, "m")
        repo.add_chunks([_chunk(blob, code=label, name=label, qualified=f"C#{label}",
                                start=10 if label == "near" else 40)])
        repo.add_descriptions([{
            "desc_key": dk, "chunk_hash": "h" * 64, "code_hash": code, "model": "m",
            "prompt_version": keys.PROMPT_VERSION, "text": label, "source": "m"}])
        repo.add_vectors([{
            "vec_key": keys.vec_key(dk, "e"), "desc_key": dk, "embed_model": "e",
            "embedding": vector, "project": "demo:."}])

    hits = repo.semantic_search(snapshot, near, k=2)
    assert hits and hits[0]["name"] == "near"
    assert hits[0]["path"] == "src/V.java", "the path comes from snapshot_files, not the chunk"


def test_search_is_scoped_to_one_snapshot(repo):
    """Vectors are global — an embedding is shared by every snapshot whose code
    hashes the same — so the scoping has to happen in the join or a search would
    return code from a commit the asker is not looking at."""
    empty = keys.snapshot_id("demo", "sha-empty")
    repo.create_snapshot(empty, "demo", "sha-empty", {})
    repo.set_snapshot_files(empty, [])
    assert repo.semantic_search(empty, [1.0] + [0.0] * (config.EMBED_DIMENSIONS - 1)) == []


# ------------------------------------------------------------------ coverage


def test_description_coverage_reports_the_gap_per_tier(repo):
    """The number `/health/index` serves: an undescribed chunk is invisible to
    semantic search however cleanly it parsed."""
    blob = _blob(repo, "2" * 64)
    snapshot = keys.snapshot_id("demo", "sha-cov")
    repo.create_snapshot(snapshot, "demo", "sha-cov", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "src/C.java", "blob_hash": blob, "project": "demo:."}])

    described = _chunk(blob, code="described", qualified="C#described", start=100)
    undescribed = _chunk(blob, code="undescribed", qualified="C#undescribed", start=200)
    repo.add_chunks([described, undescribed])
    repo.add_descriptions([{
        "desc_key": keys.desc_key(described["code_hash"], "m"), "chunk_hash": described["chunk_hash"],
        "code_hash": described["code_hash"], "model": "m", "prompt_version": keys.PROMPT_VERSION,
        "text": "t", "source": "m"}])

    coverage = repo.description_coverage(snapshot)
    assert coverage[config.TIER_FULL]["missing"] >= 1


def test_undescribed_chunks_finds_work_for_the_backfill(repo):
    blob = _blob(repo, "3" * 64)
    snapshot = keys.snapshot_id("demo", "sha-undesc")
    repo.create_snapshot(snapshot, "demo", "sha-undesc", {})
    repo.set_snapshot_files(snapshot, [
        {"snapshot_id": snapshot, "path": "src/U.java", "blob_hash": blob, "project": "demo:."}])
    repo.add_chunks([_chunk(blob, code="never described", qualified="C#never", start=300)])
    found = repo.undescribed_chunks(snapshot, model="no-such-model", prompt_version="1")
    assert any(row["qualified_name"] == "C#never" for row in found)


def test_references_are_read_backwards_by_the_incremental_indexer(repo):
    """When a symbol changes, these are the chunks whose edges must be recomputed
    even though their own files were untouched by the diff."""
    repo.add_references([
        {"symbol_simple_name": "OwnerRepository", "chunk_hash": "aa" * 32},
        {"symbol_simple_name": "OwnerRepository", "chunk_hash": "bb" * 32},
        {"symbol_simple_name": "Unrelated", "chunk_hash": "cc" * 32},
    ])
    assert repo.chunks_referencing(["OwnerRepository"]) == {"aa" * 32, "bb" * 32}


# ------------------------------------------------------------------ end to end


@pytest.fixture(scope="module")
def petclinic() -> Path:
    root = config.PROJECT_ROOT / "data" / "repos" / "spring-petclinic"
    if not (root / ".git").is_dir():
        pytest.skip("spring-petclinic checkout not present")
    return root


def test_a_full_index_reproduces_the_sqlite_structure(engine, petclinic):
    """Parity with the SQLite build, stated exactly.

    320 nodes and 724 edges was the SQLite baseline. Postgres holds 271 chunks
    and 651 edges, and the whole difference is one modelling decision: a FILE is
    no longer a chunk, it is a blob plus a `snapshot_files` row. That removes 49
    file nodes, the 49 file->class `contains` edges and the 24 file->type
    `imports` edges — every one of which had a file as an endpoint. Nothing else
    moved.
    """
    from sqlalchemy import text

    eng = get_engine("embedded://pytest_parity")
    create_all(eng)
    with eng.begin() as connection:
        for table in ("snapshot_files", "snapshots", "vectors", "descriptions", "edges",
                      "references", "chunks", "blobs", "projects"):
            connection.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))

    pg_repo = IndexRepo(eng)
    report = pgindex.index_repo(petclinic, pg_repo, embed=False)

    assert report.chunks_new == 271
    assert 271 + 49 == 320, "271 chunks + 49 file nodes = the SQLite node count"
    assert report.edges_new == 651
    assert 651 + 49 + 24 == 724, "651 edges + 49 file contains + 24 imports = SQLite's 724"
    assert report.edges_dropped == 0, "an edge pointing at no chunk is an edge lost silently"

    with eng.connect() as connection:
        kinds = dict(connection.execute(text("SELECT kind, count(*) FROM chunks GROUP BY kind")).all())
    # the Spring layer survives the move: these are what product questions are about
    assert kinds["endpoint"] == 17
    assert kinds["entity"] == 6
    assert kinds["method"] == 187
    assert kinds["screen"] == 12
    assert kinds["type_header"] == 49  # 46 classes + 3 interfaces


def test_the_tier_split_is_reported_and_non_trivial(engine, petclinic):
    """A tiering pass that put everything in one tier would be indistinguishable
    from no tiering at all."""
    pg_repo = IndexRepo(get_engine("embedded://pytest_parity"))
    snapshot = pg_repo.latest_snapshot(petclinic.name)
    distribution = pg_repo.tier_distribution(snapshot["id"])
    assert distribution.get(config.TIER_TEMPLATE, 0) > 0
    assert distribution.get(config.TIER_FULL, 0) > 0
    assert sum(distribution.values()) == 271
