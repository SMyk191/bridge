"""The only place that writes SQL against the persistent index.

Two rules hold this together:

* **Writes are upserts on the content hash, never updates.** Every immutable
  table is written with `ON CONFLICT DO NOTHING`, because a row that already
  exists is by definition identical to the one being written — that is what
  content-addressing means. A second indexer racing the first cannot corrupt
  anything, and re-running an index is free rather than destructive.
* **Existence checks are batched.** The incremental indexer's central question is
  "which of these two thousand keys do I already have?", and asking it two
  thousand times is the difference between a fast index and a slow one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy import Engine, and_, delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema

#: how many parameters to put in one statement — Postgres caps at 65535 and
#: psycopg has to build every one of them, so batches stay well under it
BATCH = 500


def _chunked(items: Sequence[Any], size: int = BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(slots=True)
class WriteCounts:
    """What a write actually did — the numbers the verification scenarios assert on."""

    blobs: int = 0
    chunks: int = 0
    descriptions: int = 0
    vectors: int = 0
    edges: int = 0
    references: int = 0
    summaries: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "blobs": self.blobs, "chunks": self.chunks, "descriptions": self.descriptions,
            "vectors": self.vectors, "edges": self.edges, "references": self.references,
            "summaries": self.summaries,
        }


class IndexRepo:
    """Read/write access to one Postgres index."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- existence, in batches ---------------------------------------------

    def _existing(self, table, column, values: Sequence[str]) -> set[str]:
        if not values:
            return set()
        found: set[str] = set()
        unique = list(dict.fromkeys(values))
        with self.engine.connect() as connection:
            for batch in _chunked(unique):
                rows = connection.execute(select(column).where(column.in_(list(batch))))
                found.update(row[0] for row in rows)
        return found

    def existing_blobs(self, hashes: Sequence[str]) -> set[str]:
        return self._existing(schema.blobs, schema.blobs.c.blob_hash, hashes)

    def existing_chunks(self, hashes: Sequence[str]) -> set[str]:
        return self._existing(schema.chunks, schema.chunks.c.chunk_hash, hashes)

    def existing_descriptions(self, keys: Sequence[str]) -> set[str]:
        """**The question the incremental indexer exists to ask.** Everything not
        in this set is a model call somebody is about to pay for."""
        return self._existing(schema.descriptions, schema.descriptions.c.desc_key, keys)

    def existing_vectors(self, keys: Sequence[str]) -> set[str]:
        return self._existing(schema.vectors, schema.vectors.c.vec_key, keys)

    # -- writes -------------------------------------------------------------

    def _insert_ignore(self, table, rows: Sequence[dict], conflict: Sequence[str]) -> int:
        """Upsert-ignore, returning how many rows were ACTUALLY new.

        `RETURNING` rather than `rowcount`: with `ON CONFLICT DO NOTHING`,
        psycopg reports -1 for a multi-row statement, and "-1 new chunks" in an
        index report is worse than useless — every verification in this phase
        asserts on these numbers, so they have to be real counts of rows that
        did not exist a moment ago.
        """
        if not rows:
            return 0
        primary = list(table.primary_key.columns) or [table.c[conflict[0]]]
        written = 0
        with self.engine.begin() as connection:
            for batch in _chunked(rows):
                statement = (
                    pg_insert(table)
                    .values(list(batch))
                    .on_conflict_do_nothing(index_elements=list(conflict))
                    .returning(primary[0])
                )
                written += len(connection.execute(statement).fetchall())
        return written

    def add_blobs(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.blobs, rows, ["blob_hash"])

    def add_chunks(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.chunks, rows, ["chunk_hash"])

    def add_descriptions(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.descriptions, rows, ["desc_key"])

    def add_vectors(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.vectors, rows, ["vec_key", "project"])

    def add_edges(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.edges, rows, ["src", "dst", "kind", "witness_line"])

    def add_references(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.references, rows, ["symbol_simple_name", "chunk_hash"])

    def add_module_summaries(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.module_summaries, rows, ["merkle_key"])

    def add_projects(self, rows: Sequence[dict]) -> int:
        return self._insert_ignore(schema.projects, rows, ["id"])

    # -- snapshots ----------------------------------------------------------

    def create_snapshot(self, snapshot_id: str, repo: str, sha: str, stats: dict) -> None:
        """Idempotent: re-indexing the same commit updates its stats, not its identity."""
        with self.engine.begin() as connection:
            statement = pg_insert(schema.snapshots).values(
                id=snapshot_id, repo=repo, sha=sha, stats=stats
            ).on_conflict_do_update(index_elements=["id"], set_={"stats": stats})
            connection.execute(statement)

    def set_snapshot_files(self, snapshot_id: str, rows: Sequence[dict]) -> int:
        """Replace a snapshot's file list wholesale.

        The only destructive write in the class, and safely so: a snapshot's file
        list is derived entirely from one commit, so rewriting it cannot lose
        information that is not immediately re-derived. Nothing content-addressed
        is touched — the blobs those paths point at stay exactly where they are.
        """
        with self.engine.begin() as connection:
            connection.execute(
                delete(schema.snapshot_files).where(
                    schema.snapshot_files.c.snapshot_id == snapshot_id
                )
            )
            written = 0
            for batch in _chunked(rows):
                if batch:
                    written += connection.execute(
                        insert(schema.snapshot_files), list(batch)
                    ).rowcount or 0
            return written

    def latest_snapshot(self, repo: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(schema.snapshots)
                .where(schema.snapshots.c.repo == repo)
                .order_by(schema.snapshots.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            return dict(row) if row else None

    def snapshot_by_sha(self, repo: str, sha: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(schema.snapshots).where(
                    and_(schema.snapshots.c.repo == repo, schema.snapshots.c.sha == sha)
                )
            ).mappings().first()
            return dict(row) if row else None

    def snapshots(self, repo: str | None = None, limit: int = 50) -> list[dict]:
        query = select(schema.snapshots).order_by(schema.snapshots.c.created_at.desc()).limit(limit)
        if repo:
            query = query.where(schema.snapshots.c.repo == repo)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def snapshot_files(self, snapshot_id: str) -> dict[str, str]:
        """path -> blob_hash. The whole of what a snapshot is."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(schema.snapshot_files.c.path, schema.snapshot_files.c.blob_hash).where(
                    schema.snapshot_files.c.snapshot_id == snapshot_id
                )
            )
            return {row[0]: row[1] for row in rows}

    def set_environment(self, env: str, repo: str, sha: str) -> None:
        with self.engine.begin() as connection:
            statement = pg_insert(schema.environments).values(env=env, repo=repo, sha=sha)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["env", "repo"],
                    set_={"sha": sha, "deployed_at": func.now()},
                )
            )

    def environments(self, repo: str | None = None) -> list[dict]:
        query = select(schema.environments)
        if repo:
            query = query.where(schema.environments.c.repo == repo)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    # -- alerts (work/alerts.py) ---------------------------------------------

    def alert_state(self, repo: str) -> dict | None:
        """The last unresolved rate `alerts.py` saw for this repo, or None the
        first time — a first check has nothing to compare against, so it can
        only establish the baseline, never fire a jump alert."""
        with self.engine.connect() as connection:
            row = connection.execute(
                select(schema.alert_state).where(schema.alert_state.c.repo == repo)
            ).mappings().first()
        return dict(row) if row else None

    def set_alert_state(self, repo: str, unresolved_rate: float) -> None:
        with self.engine.begin() as connection:
            statement = pg_insert(schema.alert_state).values(
                repo=repo, unresolved_rate=unresolved_rate
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["repo"],
                    set_={"unresolved_rate": unresolved_rate, "checked_at": func.now()},
                )
            )

    # -- reads for retrieval -------------------------------------------------

    def chunks_in_snapshot(self, snapshot_id: str, kinds: Sequence[str] | None = None) -> list[dict]:
        """Every chunk visible in a snapshot, with the path it is visible AT.

        The join through `snapshot_files` is what makes one shared chunk table
        answer "where does this code live" differently for every snapshot.
        """
        query = (
            select(
                schema.chunks,
                schema.snapshot_files.c.path,
            )
            .select_from(
                schema.chunks.join(
                    schema.snapshot_files,
                    schema.chunks.c.blob_hash == schema.snapshot_files.c.blob_hash,
                )
            )
            .where(schema.snapshot_files.c.snapshot_id == snapshot_id)
        )
        if kinds:
            query = query.where(schema.chunks.c.kind.in_(list(kinds)))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def undescribed_chunks(
        self, snapshot_id: str, model: str, prompt_version: str, tier: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Chunks in a snapshot with no description for (model, prompt_version).

        Left join rather than a NOT IN subquery: the description table is the
        biggest in the database and `NOT IN` over it degrades badly once it stops
        fitting in memory.
        """
        desc = schema.descriptions
        query = (
            select(schema.chunks, schema.snapshot_files.c.path)
            .select_from(
                schema.chunks
                .join(
                    schema.snapshot_files,
                    schema.chunks.c.blob_hash == schema.snapshot_files.c.blob_hash,
                )
                .outerjoin(
                    desc,
                    and_(
                        desc.c.code_hash == schema.chunks.c.code_hash,
                        desc.c.model == model,
                        desc.c.prompt_version == prompt_version,
                    ),
                )
            )
            .where(
                and_(
                    schema.snapshot_files.c.snapshot_id == snapshot_id,
                    desc.c.desc_key.is_(None),
                )
            )
            .limit(limit)
        )
        if tier:
            query = query.where(schema.chunks.c.tier == tier)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def descriptions_for(self, code_hashes: Sequence[str]) -> dict[str, dict]:
        """code_hash -> the best description we have. Model beats template."""
        if not code_hashes:
            return {}
        found: dict[str, dict] = {}
        unique = list(dict.fromkeys(code_hashes))
        with self.engine.connect() as connection:
            for batch in _chunked(unique):
                rows = connection.execute(
                    select(schema.descriptions).where(
                        schema.descriptions.c.code_hash.in_(list(batch))
                    )
                ).mappings()
                for row in rows:
                    current = found.get(row["code_hash"])
                    # a real model description always wins over a template or a
                    # fallback, whichever happened to be written first
                    if current is None or (
                        current["source"] in ("template", "inherited", "fallback")
                        and row["source"] not in ("template", "inherited", "fallback")
                    ):
                        found[row["code_hash"]] = dict(row)
        return found

    def semantic_search(
        self, snapshot_id: str, embedding: Sequence[float], k: int = 20,
        kinds: Sequence[str] | None = None,
    ) -> list[dict]:
        """Nearest chunks by cosine distance, scoped to one snapshot.

        The vector table is global (an embedding is shared by every snapshot whose
        code hashes to the same thing), so the snapshot scoping happens in the
        join, not in the index.
        """
        distance = schema.vectors.c.embedding.cosine_distance(list(embedding))
        query = (
            select(
                schema.chunks,
                schema.snapshot_files.c.path,
                schema.descriptions.c.text.label("description"),
                distance.label("distance"),
            )
            .select_from(
                schema.vectors
                .join(schema.descriptions, schema.vectors.c.desc_key == schema.descriptions.c.desc_key)
                .join(schema.chunks, schema.chunks.c.code_hash == schema.descriptions.c.code_hash)
                .join(
                    schema.snapshot_files,
                    schema.chunks.c.blob_hash == schema.snapshot_files.c.blob_hash,
                )
            )
            .where(schema.snapshot_files.c.snapshot_id == snapshot_id)
            .order_by(distance)
            .limit(k)
        )
        if kinds:
            query = query.where(schema.chunks.c.kind.in_(list(kinds)))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def chunks_referencing(self, symbols: Sequence[str]) -> set[str]:
        """Chunks that mention any of these bare symbol names.

        Read backwards by the incremental indexer: when a symbol changes, these
        are the chunks whose edges must be recomputed even though their own files
        were untouched by the diff.
        """
        if not symbols:
            return set()
        found: set[str] = set()
        with self.engine.connect() as connection:
            for batch in _chunked(list(dict.fromkeys(symbols))):
                rows = connection.execute(
                    select(schema.references.c.chunk_hash).where(
                        schema.references.c.symbol_simple_name.in_(list(batch))
                    )
                )
                found.update(row[0] for row in rows)
        return found

    # -- counts, coverage, verification -------------------------------------

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self.engine.connect() as connection:
            for table in (
                schema.blobs, schema.chunks, schema.descriptions, schema.vectors,
                schema.edges, schema.references, schema.projects, schema.snapshots,
                schema.snapshot_files, schema.module_summaries,
            ):
                out[table.name] = connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
        return out

    def tier_distribution(self, snapshot_id: str | None = None) -> dict[str, int]:
        query = select(schema.chunks.c.tier, func.count()).group_by(schema.chunks.c.tier)
        if snapshot_id:
            query = query.select_from(
                schema.chunks.join(
                    schema.snapshot_files,
                    schema.chunks.c.blob_hash == schema.snapshot_files.c.blob_hash,
                )
            ).where(schema.snapshot_files.c.snapshot_id == snapshot_id)
        with self.engine.connect() as connection:
            return {row[0]: row[1] for row in connection.execute(query)}

    def description_coverage(self, snapshot_id: str) -> dict[str, dict[str, int]]:
        """Per tier: how many chunks have a description, and how many do not.

        The number `/health/index` reports, and the number that says whether the
        index is actually usable — an undescribed chunk is invisible to semantic
        search however well it was parsed.
        """
        desc = schema.descriptions
        query = (
            select(
                schema.chunks.c.tier,
                func.count(func.distinct(schema.chunks.c.chunk_hash)).label("total"),
                func.count(func.distinct(desc.c.code_hash)).label("described"),
            )
            .select_from(
                schema.chunks
                .join(
                    schema.snapshot_files,
                    schema.chunks.c.blob_hash == schema.snapshot_files.c.blob_hash,
                )
                .outerjoin(desc, desc.c.code_hash == schema.chunks.c.code_hash)
            )
            .where(schema.snapshot_files.c.snapshot_id == snapshot_id)
            .group_by(schema.chunks.c.tier)
        )
        with self.engine.connect() as connection:
            return {
                row[0]: {"total": row[1], "described": row[2], "missing": max(0, row[1] - row[2])}
                for row in connection.execute(query)
            }

    def blobs_for_verify(self, snapshot_id: str) -> list[dict]:
        """(path, blob_hash) for every file in a snapshot — what `verify` re-hashes."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    schema.snapshot_files.c.path,
                    schema.snapshot_files.c.blob_hash,
                    schema.blobs.c.size_bytes,
                )
                .select_from(
                    schema.snapshot_files.join(
                        schema.blobs, schema.blobs.c.blob_hash == schema.snapshot_files.c.blob_hash
                    )
                )
                .where(schema.snapshot_files.c.snapshot_id == snapshot_id)
            ).mappings()
            return [dict(row) for row in rows]

    def orphan_blobs(self, snapshot_id: str) -> list[str]:
        """Paths in a snapshot pointing at a blob that is not in `blobs`.

        Should always be empty; a non-empty answer means something wrote a
        snapshot file list without writing the content it refers to.
        """
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(schema.snapshot_files.c.path)
                .select_from(
                    schema.snapshot_files.outerjoin(
                        schema.blobs, schema.blobs.c.blob_hash == schema.snapshot_files.c.blob_hash
                    )
                )
                .where(
                    and_(
                        schema.snapshot_files.c.snapshot_id == snapshot_id,
                        schema.blobs.c.blob_hash.is_(None),
                    )
                )
            )
            return [row[0] for row in rows]
