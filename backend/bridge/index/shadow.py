"""`bridge shadow-index` — try a prompt version before adopting it.

Because `desc_key = sha256(code_hash | model | prompt_version)`, descriptions
written at a new prompt version do not collide with, overwrite, or hide the ones
already there. Two versions of the same chunk's description live side by side and
`db/versions.py` decides which one retrieval prefers. That property is what makes
this command possible at all: a shadow index is not a copy of the database, it is
*more rows in the same tables*, and there is nothing to tear down afterwards.

What it is for: a prompt edit is a change to every answer the system will ever
give, and the only way to judge it is to read what it produces on code you know.
So this describes a bounded sample — the highest-degree chunks in the named
projects, because those are the ones retrieval actually surfaces — at the new
version, and prints the old and new descriptions next to each other.

What it deliberately does NOT do: adopt the version. Nothing routes to the shadow
rows until `PROMPT_VERSION` (or `DESCRIPTION_VERSIONS_PREFERRED`) names them, and
until then the only thing they cost is disk.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import and_, func, select

from .. import config
from ..db import create_all, get_engine, keys, schema
from ..db.repo import IndexRepo
from ..db.versions import IndexVersion


def sample_chunks(
    repo: IndexRepo, snapshot_id: str, projects: list[str], limit: int
) -> list[dict]:
    """The chunks whose descriptions matter most, in the named projects.

    Ordered by semantic in-degree: a chunk many things point at is one retrieval
    keeps returning, so a prompt change shows its effect there first. Ties break
    on `chunk_hash` so two runs of the same command sample the same code.
    """
    chunks, files, edges = schema.chunks, schema.snapshot_files, schema.edges
    degree = (
        select(edges.c.dst.label("chunk_hash"), func.count().label("degree"))
        .group_by(edges.c.dst)
        .subquery()
    )
    query = (
        select(chunks, files.c.path, func.coalesce(degree.c.degree, 0).label("degree"))
        .select_from(
            chunks
            .join(files, and_(chunks.c.blob_hash == files.c.blob_hash,
                              files.c.snapshot_id == snapshot_id))
            .outerjoin(degree, degree.c.chunk_hash == chunks.c.chunk_hash)
        )
        .order_by(func.coalesce(degree.c.degree, 0).desc(), chunks.c.chunk_hash)
        .limit(limit)
    )
    if projects:
        query = query.where(chunks.c.project.in_(projects))
    with repo.engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def _resolve_model(engine, requested: str | None) -> tuple[str, str]:
    """Which model the shadow descriptions are attributed to.

    The configured describe model by default — but if the index holds nothing at
    that model, comparing against it prints an empty "before" column for every
    row, which looks like the old prompt produced nothing rather than like the
    comparison being against the wrong thing. So it falls back to whichever model
    the index actually used, and says so.
    """
    if requested:
        return requested, ""
    configured = config.ROLES["describe"]["model"]
    with engine.connect() as connection:
        held = connection.execute(
            select(schema.descriptions.c.model, func.count().label("n"))
            .group_by(schema.descriptions.c.model)
            .order_by(func.count().desc())
        ).all()
    models = {model for model, _ in held}
    if configured in models or not held:
        return configured, ""
    actual = held[0][0]
    return actual, (
        f"the index holds no descriptions at the configured model {configured!r}; "
        f"comparing against {actual!r}, which is what it was actually indexed with"
    )


def run_shadow_index(
    repo_name: str,
    *,
    projects: list[str],
    prompt_version: str,
    model: str | None = None,
    sample: int | None = None,
    engine=None,
    write: bool = True,
) -> dict[str, Any]:
    from . import pgindex

    engine = engine or get_engine()
    create_all(engine)
    repo = IndexRepo(engine)
    sample = config.SHADOW_INDEX_SAMPLE if sample is None else sample
    model, model_note = _resolve_model(engine, model)
    current = IndexVersion(
        model=model, prompt_version=IndexVersion.current().prompt_version,
        embed_model=config.EMBED_MODEL,
    )

    snapshot = repo.latest_snapshot(repo_name)
    if snapshot is None:
        return {"error": f"no snapshot for {repo_name!r}", "described": 0}

    rows = sample_chunks(repo, snapshot["id"], projects, sample)
    shadow_keys = {
        keys.desc_key(row["code_hash"], model, prompt_version): row for row in rows
    }
    already = repo.existing_descriptions(list(shadow_keys))
    missing = {key: row for key, row in shadow_keys.items() if key not in already}

    written: list[dict] = []
    for key, row in missing.items():
        text = pgindex._describe(None, row, model)
        written.append(
            {
                "desc_key": key,
                "chunk_hash": row["chunk_hash"],
                "code_hash": row["code_hash"],
                "model": model,
                "prompt_version": prompt_version,
                "text": text,
                "source": model,
            }
        )
    stored = repo.add_descriptions(written) if write else 0

    # what retrieval would show today, for the same chunks, so the diff is real
    current_keys = {
        keys.desc_key(row["code_hash"], current.model, current.prompt_version): row
        for row in rows
    }
    with engine.connect() as connection:
        existing_text = {
            entry["desc_key"]: entry["text"]
            for entry in connection.execute(
                select(schema.descriptions.c.desc_key, schema.descriptions.c.text).where(
                    schema.descriptions.c.desc_key.in_(list(current_keys) + list(shadow_keys))
                )
            ).mappings()
        }

    comparisons = []
    for row in rows:
        before_key = keys.desc_key(row["code_hash"], current.model, current.prompt_version)
        after_key = keys.desc_key(row["code_hash"], model, prompt_version)
        comparisons.append(
            {
                "qualified_name": row.get("qualified_name") or row["name"],
                "path": row["path"],
                "degree": row["degree"],
                "before": existing_text.get(before_key, ""),
                "after": existing_text.get(after_key, ""),
                "changed": existing_text.get(before_key, "") != existing_text.get(after_key, ""),
            }
        )

    return {
        "repo": repo_name,
        "model_note": model_note,
        "snapshot": snapshot["id"],
        "sha": snapshot["sha"],
        "projects": projects or ["<all>"],
        "shadow_version": {"model": model, "prompt_version": prompt_version},
        "current_version": current.as_dict(),
        "sampled": len(rows),
        "already_shadowed": len(already),
        "scheduled": len(missing),
        "written": stored,
        "changed": sum(1 for entry in comparisons if entry["changed"]),
        "comparisons": comparisons,
    }


def shadow_index_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bridge shadow-index",
        description="describe a sample of chunks at a new prompt version, without adopting it",
    )
    parser.add_argument("repo", help="repository name")
    parser.add_argument("--projects", default="", help="comma-separated project ids")
    parser.add_argument("--prompt-version", required=True, help="the version to try, e.g. v2")
    parser.add_argument("--model", default=None, help="override the describe model")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be described, write nothing"
    )
    args = parser.parse_args(argv)

    projects = [p.strip() for p in args.projects.split(",") if p.strip()]
    report = run_shadow_index(
        args.repo,
        projects=projects,
        prompt_version=args.prompt_version,
        model=args.model,
        sample=args.sample,
        write=not args.dry_run,
    )
    if "error" in report:
        print(report["error"])
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"shadow index  {report['repo']}@{report['sha'][:12]}  "
          f"projects={','.join(report['projects'])}")
    print(f"  current version   {report['current_version']['model']} "
          f"prompt={report['current_version']['prompt_version']}")
    print(f"  shadow version    {report['shadow_version']['model']} "
          f"prompt={report['shadow_version']['prompt_version']}")
    if report.get("model_note"):
        print(f"  note: {report['model_note']}")
    print(f"  sampled={report['sampled']}  already={report['already_shadowed']}  "
          f"scheduled={report['scheduled']}  written={report['written']}  "
          f"changed={report['changed']}")
    for entry in report["comparisons"][:10]:
        print(f"\n  {entry['qualified_name']}  (degree {entry['degree']})")
        print(f"    before: {(entry['before'] or '—')[:110]}")
        print(f"    after : {(entry['after'] or '—')[:110]}")
    print("\n  nothing routes to these rows until PROMPT_VERSION or "
          "DESCRIPTION_VERSIONS_PREFERRED names them.")
    return 0
