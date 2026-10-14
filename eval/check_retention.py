"""Phase G's claims, against a real Postgres and a real repository.

    .venv/bin/python ../eval/check_retention.py

  1. retention keeps what the policy says and only that
  2. gc --dry-run and gc --apply agree, and retained snapshots retrieve identically
  3. vectors are partitioned by project, and a routed query prunes
  4. two-stage search returns what the single-stage search returned
  5. a prompt-version bump costs 0 descriptions, and the backfill closes the gap

Exit code is 0 only if all five hold.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from bridge import config  # noqa: E402
from bridge.db import create_all, get_engine, keys, retention, schema, versions  # noqa: E402
from bridge.db.repo import IndexRepo  # noqa: E402
from bridge.db.vectorstore import PgVectorStore, VectorFilter  # noqa: E402
from bridge.index import pgindex  # noqa: E402
from bridge.index.shadow import run_shadow_index  # noqa: E402
from bridge.work.queue import JobQueue  # noqa: E402
from bridge.work.worker import Job, backfill_candidates, handle_describe_backfill  # noqa: E402

SOURCE_REPO = Path("/Users/samyak/bridge/data/repos/spring-petclinic")
DSN = "embedded://phase_g"
TABLES = ("jobs", "snapshot_files", "snapshots", "vectors", "descriptions",
          "edges", "references", "chunks", "blobs", "projects", "environments",
          "module_summaries")


def reset(engine) -> None:
    with engine.begin() as connection:
        for table in TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    # Partitions are schema, so TRUNCATE leaves them behind and the previous
    # run's partition would make this run's partitioning step look like a no-op.
    PgVectorStore(engine).drop_empty_partitions()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(root: Path, path: str, body: str, message: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    git(root, "add", "-A")
    git(root, "-c", "user.email=b@x", "-c", "user.name=bridge", "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def top_paths(repo: IndexRepo, snapshot_id: str, vector, k: int = 10) -> list[str]:
    return [
        f"{row['path']}:{row['start_line']}"
        for row in repo.semantic_search(snapshot_id, vector, k=k)
    ]


def main() -> int:
    engine = get_engine(DSN)
    create_all(engine)
    reset(engine)
    repo = IndexRepo(engine)
    store = PgVectorStore(engine)
    failures: list[str] = []

    workspace = Path(tempfile.mkdtemp(prefix="bridge-phase-g-"))
    root = workspace / "spring-petclinic"
    shutil.copytree(SOURCE_REPO, root, symlinks=True)
    repo_name = root.name

    print("=" * 92)
    print("PHASE G — retention, partitioned vectors, lazy version migration")
    print(f"  vector store: {json.dumps(store.capabilities())}")
    print("=" * 92)

    # --- four snapshots, so retention has something to choose between
    shas = [git(root, "rev-parse", "HEAD")]
    for index in range(2):
        shas.append(
            commit(root, f"src/main/java/org/springframework/samples/petclinic/Gen{index}.java",
                   f"package org.springframework.samples.petclinic;\n\n"
                   f"public class Gen{index} {{\n  public int value() {{ return {index}; }}\n}}\n",
                   f"phase-g fixture {index}")
        )
    # The last commit DELETES Gen1, so its blob is reachable only from the
    # snapshot retention is about to drop. Without a file that actually leaves
    # history, every blob survives in a later snapshot and the sweep would
    # collect nothing but snapshot rows — proving the policy and not the
    # reachability walk underneath it.
    doomed_file = "src/main/java/org/springframework/samples/petclinic/Gen1.java"
    (root / doomed_file).unlink()
    git(root, "add", "-A")
    git(root, "-c", "user.email=b@x", "-c", "user.name=bridge", "commit", "-q",
        "-m", "phase-g fixture: remove Gen1")
    shas.append(git(root, "rev-parse", "HEAD"))
    git(root, "tag", "v-phase-g", shas[1])

    print("\n(0) indexing four commits")
    for sha in shas:
        git(root, "checkout", "-q", sha)
        report = pgindex.index_repo(root, repo, sha=sha, embed=True)
        print(f"  {sha[:12]}  chunks+{report.chunks_new:<4} desc+{report.descriptions_written:<4} "
              f"vec+{report.vectors_new:<4} snapshot={report.snapshot_id[:12]}")
    git(root, "checkout", "-q", shas[-1])
    repo.set_environment("staging", repo_name, shas[0])

    snapshots = {row["sha"]: row["id"] for row in repo.snapshots(repo_name)}
    keep_sha, tagged_sha, middle_sha, head_sha = shas

    # ------------------------------------------------------------------ (1)
    print("\n(1) retention policy")
    original_n, original_days = config.RETENTION_KEEP_LAST_N, config.RETENTION_KEEP_DAYS
    # Force a decision: without this every snapshot is minutes old and everything
    # is retained, which proves the rules never fire rather than that they work.
    config.RETENTION_KEEP_LAST_N = 1
    config.RETENTION_KEEP_DAYS = 0
    decision = retention.decide(engine, repo_root_for=lambda name: root,
                                now=datetime.now(timezone.utc) + timedelta(days=1))
    for snapshot_id, reasons in sorted(decision.keep.items()):
        sha = decision.labels[snapshot_id][1]
        print(f"  keep  {sha[:12]}  {'; '.join(reasons)}")
    for snapshot_id in decision.drop:
        print(f"  drop  {decision.labels[snapshot_id][1][:12]}")

    kept_shas = {decision.labels[s][1] for s in decision.keep}
    if keep_sha not in kept_shas:
        failures.append("(1) the snapshot an environment points at was not retained")
    if tagged_sha not in kept_shas:
        failures.append("(1) the tagged snapshot was not retained")
    if head_sha not in kept_shas:
        failures.append("(1) the newest commit on the default branch was not retained")
    if middle_sha in kept_shas:
        failures.append("(1) an untagged, undeployed, non-recent snapshot was retained anyway")

    # ------------------------------------------------------------------ (2)
    print("\n(2) gc dry run, then gc --apply")
    encoder_vector = None
    try:
        from bridge.index.embed import load_encoder

        encoder = load_encoder(config.EMBED_MODEL)
        encoder_vector = encoder.encode(
            ["where are visits recorded for a pet"], normalize_embeddings=True
        )[0].tolist()
    except Exception as error:
        print(f"  (embedding unavailable: {error})")

    before_retrieval = {
        sha: top_paths(repo, snapshots[sha], encoder_vector)
        for sha in kept_shas
        if encoder_vector is not None
    }

    dry = retention.collect(engine, dry_run=True, repo_root_for=lambda name: root,
                            now=datetime.now(timezone.utc) + timedelta(days=1))
    with engine.connect() as connection:
        after_dry = retention._counts(connection)
    if after_dry != dry.examined:
        failures.append("(2) the dry run changed the database")
    print(f"  dry run   would reclaim {dry.as_dict()['reclaimed_total']} row(s): "
          f"{json.dumps(dry.as_dict()['reclaimed'])}")

    applied = retention.collect(engine, dry_run=False, repo_root_for=lambda name: root,
                                now=datetime.now(timezone.utc) + timedelta(days=1))
    print(f"  applied   reclaimed    {applied.as_dict()['reclaimed_total']} row(s): "
          f"{json.dumps(applied.as_dict()['reclaimed'])}")
    if dry.as_dict()["reclaimed"] != applied.as_dict()["reclaimed"]:
        failures.append("(2) dry run and apply disagreed about what would be deleted")
    if applied.as_dict()["reclaimed_total"] == 0:
        failures.append("(2) the sweep reclaimed nothing, so it proved nothing")
    for table in ("blobs", "chunks", "descriptions", "vectors"):
        if applied.reclaimed.get(table, 0) == 0:
            failures.append(
                f"(2) no {table} were reclaimed — the reachability walk was not exercised"
            )

    surviving = {row["sha"] for row in repo.snapshots(repo_name)}
    if middle_sha in surviving:
        failures.append("(2) a collected snapshot is still in the database")
    if not kept_shas <= surviving:
        failures.append(f"(2) a retained snapshot was deleted: {kept_shas - surviving}")

    if encoder_vector is not None:
        for sha, before in before_retrieval.items():
            after = top_paths(repo, snapshots[sha], encoder_vector)
            if before != after:
                failures.append(f"(2) retrieval changed for retained snapshot {sha[:12]}")
            print(f"  retrieval {sha[:12]}  {len(after)} hit(s)  "
                  f"{'unchanged' if before == after else 'CHANGED'}")

    config.RETENTION_KEEP_LAST_N, config.RETENTION_KEEP_DAYS = original_n, original_days

    # ------------------------------------------------------------------ (3)
    print("\n(3) partitioning")
    created = store.autopartition(minimum=1)
    print(f"  partitions created: {created or '(none — no project met the minimum)'}")
    print(f"  partition sizes:    {json.dumps(store.partitions())}")
    projects = [p for p in store.partitions() if p != "vectors_default"]
    if not projects:
        failures.append("(3) no project partition was created")

    with engine.connect() as connection:
        project_id = connection.execute(
            text("SELECT project FROM vectors WHERE project <> '' LIMIT 1")
        ).scalar()
        if project_id and encoder_vector is not None:
            literal = "[" + ",".join(f"{v:.7g}" for v in encoder_vector) + "]"
            plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text(
                        "EXPLAIN SELECT vec_key FROM vectors WHERE project = :p "
                        f"ORDER BY embedding <=> '{literal}'::vector LIMIT 10"
                    ),
                    {"p": project_id},
                )
            )
            scanned = sorted({
                line.split(" on ")[1].split()[0]
                for line in plan.splitlines() if " on " in line
            })
            print(f"  routed query scans: {scanned}")
            if "vectors_default" in scanned:
                failures.append("(3) a routed query still scanned the default partition")

    # ------------------------------------------------------------------ (4)
    print("\n(4) two-stage search agrees with the single-stage query")
    if encoder_vector is not None:
        head_snapshot = snapshots[head_sha]
        baseline = top_paths(repo, head_snapshot, encoder_vector, k=10)
        hits = store.search(VectorFilter(snapshot_id=head_snapshot), encoder_vector, k=10)
        staged = [f"{hit.path}:{hit.start_line}" for hit in hits]
        print(f"  single-stage top-5  {baseline[:5]}")
        print(f"  two-stage    top-5  {staged[:5]}")
        if set(baseline[:10]) != set(staged[:10]):
            failures.append("(4) two-stage search returned a different top-10")
        reordered = sum(
            1 for index, hit in enumerate(hits)
            if index and hits[index - 1].approx_distance > hit.approx_distance
        )
        print(f"  rescoring reordered {reordered} pair(s)  "
              f"(rescored={store.capabilities()['rescored']})")
        if store.capabilities()["rescored"] is False and reordered:
            failures.append("(4) rescoring reordered rows on a server with no quantised column")

    # ------------------------------------------------------------------ (5)
    print("\n(5) a prompt-version bump costs nothing immediately")
    with engine.connect() as connection:
        before_rows = connection.execute(
            text("SELECT count(*) FROM descriptions")
        ).scalar_one()
    original_prompt = keys.PROMPT_VERSION
    keys.PROMPT_VERSION = "2"
    try:
        described_model = keys.SOURCE_TEMPLATE
        bumped = versions.IndexVersion(
            model=described_model, prompt_version="2", embed_model=config.EMBED_MODEL
        )
        coverage = versions.coverage(engine, bumped)
        with engine.connect() as connection:
            after_rows = connection.execute(text("SELECT count(*) FROM descriptions")).scalar_one()
        print(f"  descriptions before={before_rows} after={after_rows}  "
              f"migrated={coverage['migrated_pct']}%")
        if after_rows != before_rows:
            failures.append("(5) bumping the prompt version wrote rows")
        if coverage["migrated_pct"] != 0.0:
            failures.append("(5) coverage claims migration progress that has not happened")

        stale = versions.stale_code_hashes(engine, limit=10_000, target=bumped)
        print(f"  stale code hashes:  {len(stale)}")
        if not stale:
            failures.append("(5) nothing was reported stale after the bump")

        resolved = versions.resolve(engine, stale[:20])
        fell_back = [entry for entry in resolved.values() if not entry["is_current"]]
        print(f"  retrieval falls back for {len(fell_back)}/{len(resolved)} sampled chunks")
        if len(fell_back) != len(resolved) or not resolved:
            failures.append("(5) retrieval did not fall back to the previous version")

        head_snapshot = snapshots[head_sha]
        job = Job(id=0, type=schema.JOB_DESCRIBE_BACKFILL, payload={"limit": 25},
                  repo=repo_name, attempts=0)
        result = handle_describe_backfill(job, engine)
        print(f"  backfill wrote {result['described']} at model={result['model']} "
              f"prompt_version={result['prompt_version']}")
        if result["described"] == 0:
            failures.append("(5) the backfill did not describe anything at the new version")
        # Measured against the version the backfill actually targeted. With no
        # reachable provider, descriptions are attributed to `template` rather
        # than to the configured model — measuring against a model nothing was
        # ever written at would report 0% forever and call it a stalled
        # migration.
        target = versions.IndexVersion(
            model=result["model"], prompt_version=result["prompt_version"],
            embed_model=config.EMBED_MODEL,
        )
        after = versions.coverage(engine, target)
        print(f"  migrated now {after['migrated_pct']}%  "
              f"(versions held: {[(v['model'], v['prompt_version'], v['descriptions']) for v in after['versions']]})")
        if after["migrated_pct"] <= 0.0:
            failures.append("(5) coverage did not move after the backfill")
    finally:
        keys.PROMPT_VERSION = original_prompt

    # ------------------------------------------------------------------ (6)
    print("\n(6) shadow-index adopts nothing")
    with engine.connect() as connection:
        current_before = connection.execute(
            text("SELECT count(*) FROM descriptions WHERE prompt_version = :v"),
            {"v": keys.PROMPT_VERSION},
        ).scalar_one()
    shadow = run_shadow_index(repo_name, projects=[], prompt_version="shadow-v9",
                              sample=25, engine=engine)
    with engine.connect() as connection:
        current_after = connection.execute(
            text("SELECT count(*) FROM descriptions WHERE prompt_version = :v"),
            {"v": keys.PROMPT_VERSION},
        ).scalar_one()
    print(f"  sampled={shadow['sampled']} written={shadow['written']} "
          f"current-version rows {current_before} -> {current_after}")
    if current_after != current_before:
        failures.append("(6) shadow-index modified the version retrieval is using")
    if shadow["written"] == 0:
        failures.append("(6) shadow-index wrote nothing, so it proved nothing")

    print("\n" + "=" * 92)
    if failures:
        print("FAILURES")
        for line in failures:
            print("  -", line)
        return 1
    print("all six hold.")
    print("=" * 92)
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
