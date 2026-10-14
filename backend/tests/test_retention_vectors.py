"""Phase G: what the GC keeps, how vectors are partitioned, and lazy versioning.

Against a real Postgres, because all three subjects are database behaviour —
LIST partitioning and its pruning, `notin_` reachability over content-addressed
rows, and a transaction that is rolled back to make a dry run truthful. A fake
store would test the parts that were never in doubt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from bridge import config
from bridge.db import create_all, get_engine, keys, retention, schema, versions
from bridge.db.repo import IndexRepo
from bridge.db.vectorstore import (
    ExternalVectorStore,
    PgVectorStore,
    VectorFilter,
    partition_name,
)

TABLES = ("snapshot_files", "snapshots", "vectors", "descriptions", "edges",
          "references", "chunks", "blobs", "projects", "environments", "module_summaries")


@pytest.fixture()
def engine():
    pytest.importorskip("pgserver")
    eng = get_engine("embedded://pytest_phase_g")
    create_all(eng)
    with eng.begin() as connection:
        for table in TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    # Partitions are schema, not data, so TRUNCATE leaves them behind and one
    # test's partition would decide the next test's answer.
    PgVectorStore(eng).drop_empty_partitions()
    return eng


@pytest.fixture()
def repo(engine):
    return IndexRepo(engine)


def seed(repo: IndexRepo, snapshot_id: str, sha: str, *, path: str, body: str,
         project: str = "app", repo_name: str = "demo") -> dict:
    """One snapshot holding one file holding one chunk, described and embedded."""
    blob_hash = keys.blob_hash(body.encode("utf-8"))
    code = keys.code_hash("method", f"X#{path}", body)
    chunk_hash = keys.chunk_hash(blob_hash, "method", f"X#{path}", 1, 3)
    model = config.ROLES["describe"]["model"]
    desc_key = keys.desc_key(code, model)
    vec_key = keys.vec_key(desc_key, "test-embed")

    repo.add_blobs([{"blob_hash": blob_hash, "lang": "java", "size_bytes": len(body)}])
    repo.add_chunks([{
        "chunk_hash": chunk_hash, "blob_hash": blob_hash, "code_hash": code,
        "kind": "method", "name": path, "qualified_name": f"X#{path}",
        "start_line": 1, "end_line": 3, "code": body, "project": project,
    }])
    repo.add_descriptions([{
        "desc_key": desc_key, "chunk_hash": chunk_hash, "code_hash": code,
        "model": model, "prompt_version": keys.PROMPT_VERSION,
        "text": f"does {path}", "source": model,
    }])
    repo.add_vectors([{
        "vec_key": vec_key, "desc_key": desc_key, "embed_model": "test-embed",
        "embedding": [0.0] * config.EMBED_DIMENSIONS, "project": project,
    }])
    repo.create_snapshot(snapshot_id, repo_name, sha, {})
    # `set_snapshot_files` replaces the list wholesale, so seeding a second file
    # into one snapshot has to carry the first one along.
    existing = [
        {"snapshot_id": snapshot_id, "path": known_path, "blob_hash": known_blob,
         "project": project}
        for known_path, known_blob in repo.snapshot_files(snapshot_id).items()
    ]
    repo.set_snapshot_files(snapshot_id, existing + [
        {"snapshot_id": snapshot_id, "path": path, "blob_hash": blob_hash, "project": project},
    ])
    return {"blob": blob_hash, "chunk": chunk_hash, "code": code,
            "desc_key": desc_key, "vec_key": vec_key}


# ------------------------------------------------------------------ retention policy


def test_an_environment_pins_a_snapshot_no_matter_how_old(engine, repo):
    """"What is in staging?" has to stay answerable after the commit falls off
    the end of every other rule."""
    seed(repo, "snap-old", "a" * 40, path="Old.java", body="int a(){return 1;}")
    repo.set_environment("staging", "demo", "a" * 40)
    with engine.begin() as connection:
        connection.execute(
            schema.snapshots.update().values(
                created_at=datetime.now(timezone.utc) - timedelta(days=365)
            )
        )
    decision = retention.decide(engine, repo_root_for=lambda name: "/nonexistent")
    assert decision.keep["snap-old"] == [config.RETAIN_ENVIRONMENT]
    assert decision.drop == []


def test_an_old_unreferenced_snapshot_is_dropped(engine, repo):
    seed(repo, "snap-stale", "b" * 40, path="Stale.java", body="int b(){return 2;}")
    with engine.begin() as connection:
        connection.execute(
            schema.snapshots.update().values(
                created_at=datetime.now(timezone.utc) - timedelta(days=365)
            )
        )
    decision = retention.decide(engine, repo_root_for=lambda name: "/nonexistent")
    assert decision.drop == ["snap-stale"]


def test_a_recent_snapshot_is_kept_without_any_other_reason(engine, repo):
    seed(repo, "snap-new", "c" * 40, path="New.java", body="int c(){return 3;}")
    decision = retention.decide(engine, repo_root_for=lambda name: "/nonexistent")
    assert config.RETAIN_RECENT_DAYS in decision.keep["snap-new"]


def test_every_rule_that_fires_is_reported_not_just_the_first(engine, repo):
    """The report is the point: "kept" without "why" is not a policy anybody can
    tune."""
    seed(repo, "snap-both", "d" * 40, path="Both.java", body="int d(){return 4;}")
    repo.set_environment("prod", "demo", "d" * 40)
    decision = retention.decide(engine, repo_root_for=lambda name: "/nonexistent")
    assert set(decision.keep["snap-both"]) == {
        config.RETAIN_ENVIRONMENT, config.RETAIN_RECENT_DAYS
    }


# ------------------------------------------------------------------ collection


def _age_out(engine, snapshot_id: str, days: int = 365) -> None:
    with engine.begin() as connection:
        connection.execute(
            schema.snapshots.update()
            .where(schema.snapshots.c.id == snapshot_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=days))
        )


def test_a_dry_run_changes_nothing(engine, repo):
    seed(repo, "snap-doomed", "e" * 40, path="Doomed.java", body="int e(){return 5;}")
    _age_out(engine, "snap-doomed")
    report = retention.collect(engine, dry_run=True, repo_root_for=lambda name: "/nonexistent")
    assert report.reclaimed["blobs"] == 1
    with engine.connect() as connection:
        assert retention._counts(connection) == report.examined


def test_apply_deletes_exactly_what_the_dry_run_promised(engine, repo):
    seed(repo, "snap-doomed", "f" * 40, path="Doomed.java", body="int f(){return 6;}")
    _age_out(engine, "snap-doomed")
    dry = retention.collect(engine, dry_run=True, repo_root_for=lambda name: "/nonexistent")
    applied = retention.collect(engine, dry_run=False, repo_root_for=lambda name: "/nonexistent")
    assert dry.reclaimed == applied.reclaimed
    with engine.connect() as connection:
        assert connection.execute(select(schema.blobs)).first() is None


def test_a_blob_two_snapshots_share_survives_one_of_them_being_collected(engine, repo):
    """The whole reason the GC asks about reachability rather than age: content
    addressing means the old snapshot's blob IS the new snapshot's blob."""
    body = "int shared(){return 7;}"
    seed(repo, "snap-old", "0" * 40, path="Shared.java", body=body)
    seed(repo, "snap-new", "1" * 40, path="Shared.java", body=body)
    _age_out(engine, "snap-old")
    report = retention.collect(engine, dry_run=False, repo_root_for=lambda name: "/nonexistent")
    assert report.reclaimed.get("blobs", 0) == 0
    assert report.reclaimed["snapshots"] == 1
    with engine.connect() as connection:
        assert connection.execute(select(schema.descriptions)).first() is not None


def test_a_description_survives_while_any_live_chunk_shares_its_code(engine, repo):
    """A moved file is a new blob and a new chunk with the SAME code_hash. The
    old snapshot going away must not take the description with it."""
    body = "int moved(){return 8;}"
    seed(repo, "snap-old", "2" * 40, path="a/Moved.java", body=body)
    seed(repo, "snap-new", "3" * 40, path="b/Moved.java", body=body)
    _age_out(engine, "snap-old")
    retention.collect(engine, dry_run=False, repo_root_for=lambda name: "/nonexistent")
    with engine.connect() as connection:
        assert connection.execute(select(schema.descriptions)).first() is not None


def test_nothing_to_collect_still_reports_what_it_looked_at(engine, repo):
    seed(repo, "snap-new", "4" * 40, path="New.java", body="int n(){return 9;}")
    report = retention.collect(engine, dry_run=True, repo_root_for=lambda name: "/nonexistent")
    assert report.reclaimed == {}
    assert report.examined["blobs"] == 1, "a GC that examined nothing is not the same as a clean one"


# ------------------------------------------------------------------ vector store


def test_partition_names_are_safe_and_distinguish_similar_projects():
    """Project ids come from build files, so they carry slashes and dots."""
    first = partition_name("backend/service-a")
    second = partition_name("backend.service.a")
    assert first.isidentifier() and second.isidentifier()
    assert first != second, "two projects must not collapse onto one partition"


def test_the_vectors_table_is_partitioned_and_has_a_default(engine):
    store = PgVectorStore(engine)
    assert store.capabilities()["partitioned"] is True
    assert "vectors_default" in store.partitions()


def test_a_project_gets_its_own_partition_and_keeps_its_rows(engine, repo):
    """The detach / create / move / reattach dance is the risky part: a row lost
    there is a chunk that silently stops being retrievable."""
    seed(repo, "snap", "5" * 40, path="P.java", body="int p(){return 1;}", project="alpha")
    store = PgVectorStore(engine)
    assert store.ensure_partition("alpha") is True
    partitions = store.partitions()
    assert partitions[partition_name("alpha")] == 1
    assert partitions["vectors_default"] == 0


def test_creating_a_partition_twice_is_not_an_error(engine, repo):
    seed(repo, "snap", "6" * 40, path="P.java", body="int p(){return 1;}", project="alpha")
    store = PgVectorStore(engine)
    assert store.ensure_partition("alpha") is True
    assert store.ensure_partition("alpha") is False


def test_autopartition_leaves_projects_too_small_to_be_worth_one_alone(engine, repo):
    seed(repo, "snap", "7" * 40, path="P.java", body="int p(){return 1;}", project="tiny")
    store = PgVectorStore(engine)
    assert store.autopartition(minimum=1000) == []
    assert store.autopartition(minimum=1) == ["tiny"]


def test_search_is_scoped_to_the_snapshot_it_was_asked_about(engine, repo):
    seed(repo, "snap-a", "8" * 40, path="A.java", body="int a(){return 1;}")
    seed(repo, "snap-b", "9" * 40, path="B.java", body="int b(){return 2;}")
    store = PgVectorStore(engine)
    hits = store.search(VectorFilter(snapshot_id="snap-a"), [0.0] * config.EMBED_DIMENSIONS, k=10)
    assert {hit.path for hit in hits} == {"A.java"}


def test_search_prunes_to_the_projects_it_was_given(engine, repo):
    seed(repo, "snap", "a" * 40, path="A.java", body="int a(){return 1;}", project="alpha")
    seed(repo, "snap", "a" * 40, path="B.java", body="int b(){return 2;}", project="beta")
    store = PgVectorStore(engine)
    hits = store.search(
        VectorFilter(snapshot_id="snap", projects=["alpha"]),
        [0.0] * config.EMBED_DIMENSIONS, k=10,
    )
    assert {hit.project for hit in hits} == {"alpha"}


def test_capabilities_say_plainly_whether_the_rescoring_pass_does_anything(engine):
    """pgvector below 0.7.0 has no halfvec, so stage 2 reorders nothing. Saying
    "two-stage search" while stage 2 is a no-op would be the lie this reports."""
    caps = PgVectorStore(engine).capabilities()
    assert caps["rescored"] == caps["quantised_column"]
    if not caps["quantised_type"]:
        assert "no halfvec" in caps["note"]


def test_the_external_store_refuses_rather_than_pretending():
    store = ExternalVectorStore(endpoint="https://example.invalid")
    with pytest.raises(NotImplementedError) as error:
        store.search(VectorFilter(snapshot_id="x"), [0.0], 1)
    assert "payload" in str(error.value), "the stub must say what it would take to build"
    assert store.capabilities()["built"] is False


# ------------------------------------------------------------------ lazy versioning


def test_bumping_the_prompt_version_writes_nothing(engine, repo, monkeypatch):
    seed(repo, "snap", "b" * 40, path="V.java", body="int v(){return 1;}")
    with engine.connect() as connection:
        before = connection.execute(select(schema.descriptions)).all()
    monkeypatch.setattr(keys, "PROMPT_VERSION", "99")
    versions.coverage(engine)
    with engine.connect() as connection:
        assert connection.execute(select(schema.descriptions)).all() == before


def test_retrieval_falls_back_to_the_version_that_exists(engine, repo, monkeypatch):
    """A half-migrated index is the normal state. Refusing to answer until the
    backfill finishes would make the bump the outage it exists to avoid."""
    seeded = seed(repo, "snap", "c" * 40, path="V.java", body="int v(){return 1;}")
    monkeypatch.setattr(keys, "PROMPT_VERSION", "99")
    resolved = versions.resolve(engine, [seeded["code"]])
    assert resolved[seeded["code"]]["desc_key"] == seeded["desc_key"]
    assert resolved[seeded["code"]]["is_current"] is False


def test_the_current_version_wins_when_both_exist(engine, repo, monkeypatch):
    seeded = seed(repo, "snap", "d" * 40, path="V.java", body="int v(){return 1;}")
    model = config.ROLES["describe"]["model"]
    stale_key = keys.desc_key(seeded["code"], model, "0")
    repo.add_descriptions([{
        "desc_key": stale_key, "chunk_hash": seeded["chunk"], "code_hash": seeded["code"],
        "model": model, "prompt_version": "0",
        "text": "the old one", "source": model,
    }])
    resolved = versions.resolve(engine, [seeded["code"]])
    assert resolved[seeded["code"]]["text"] == f"does V.java"
    assert resolved[seeded["code"]]["is_current"] is True


def test_coverage_reports_zero_migrated_immediately_after_a_bump(engine, repo, monkeypatch):
    seed(repo, "snap", "e" * 40, path="V.java", body="int v(){return 1;}")
    monkeypatch.setattr(keys, "PROMPT_VERSION", "99")
    coverage = versions.coverage(engine)
    assert coverage["migrated_pct"] == 0.0
    assert coverage["code_hashes_total"] == 1


def test_stale_lists_code_described_at_an_old_version(engine, repo, monkeypatch):
    seeded = seed(repo, "snap", "f" * 40, path="V.java", body="int v(){return 1;}")
    assert versions.stale_code_hashes(engine) == []
    monkeypatch.setattr(keys, "PROMPT_VERSION", "99")
    assert versions.stale_code_hashes(engine) == [seeded["code"]]


def test_preference_order_puts_a_pinned_version_second(engine, repo, monkeypatch):
    """The roll-back lever: a prompt that turned out worse is demoted by naming
    the old one, without deleting the rows the new one wrote."""
    seed(repo, "snap", "0" * 40, path="V.java", body="int v(){return 1;}")
    monkeypatch.setattr(config, "DESCRIPTION_VERSIONS_PREFERRED", ("legacy-model|7",))
    order = versions.preference_order(engine)
    assert order[0] == (config.ROLES["describe"]["model"], keys.PROMPT_VERSION)
    assert order[1] == ("legacy-model", "7")


# ------------------------------------------------------------------ shadow index


def test_shadow_index_compares_against_the_model_the_index_actually_used(engine, repo):
    """The configured describe model and the model a description is attributed
    to can differ — a run with no reachable provider says `template` rather than
    claiming the configured model wrote it. Comparing against the configured one
    would print an empty "before" column and read as "the old prompt produced
    nothing"."""
    from bridge.index.shadow import _resolve_model

    seed(repo, "snap", "1" * 40, path="S.java", body="int s(){return 1;}")
    configured = config.ROLES["describe"]["model"]
    assert _resolve_model(engine, None) == (configured, "")

    with engine.begin() as connection:
        connection.execute(
            schema.descriptions.update().values(model=keys.SOURCE_TEMPLATE)
        )
    model, note = _resolve_model(engine, None)
    assert model == keys.SOURCE_TEMPLATE
    assert configured in note, "the substitution has to say what it did"


def test_shadow_index_writes_only_at_the_shadow_version(engine, repo):
    from bridge.index.shadow import run_shadow_index

    seeded = seed(repo, "snap", "2" * 40, path="S.java", body="int s(){return 1;}")
    report = run_shadow_index("demo", projects=[], prompt_version="shadow-1",
                              sample=10, engine=engine)
    assert report["written"] == 1
    with engine.connect() as connection:
        rows = connection.execute(
            select(schema.descriptions.c.desc_key, schema.descriptions.c.prompt_version)
        ).all()
    assert seeded["desc_key"] in {row[0] for row in rows}, "the live version was disturbed"
    assert "shadow-1" in {row[1] for row in rows}


def test_an_explicit_model_overrides_the_substitution(engine, repo):
    from bridge.index.shadow import _resolve_model

    seed(repo, "snap", "3" * 40, path="S.java", body="int s(){return 1;}")
    assert _resolve_model(engine, "some-other-model") == ("some-other-model", "")
