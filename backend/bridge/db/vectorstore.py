"""The seam between Bridge and whatever actually stores its vectors.

Everything above this module asks one question — *which chunks are nearest this
embedding, inside this snapshot, in these projects* — and that question should
not change if the vectors move to a dedicated engine. So it is an interface with
two implementations: `PgVectorStore`, which is what runs, and
`ExternalVectorStore`, which is a stub that documents exactly what a second
implementation would have to answer and refuses to pretend it can.

Two design points are worth stating, because both are easy to get subtly wrong.

**Partitioning, not post-filtering.** `vectors` is LIST-partitioned by project.
A routed query (Phase H picks the projects) then reads only those partitions'
indexes. The tempting alternative — one global index, `WHERE project IN (...)`
afterwards — is not the same query more cheaply: an approximate index returns its
own k nearest and the filter then discards most of them, so the *effective* k
collapses and recall silently drops. Pruning happens before the search or it does
not help.

**Two-stage search.** Stage 1 is an approximate pass over a quantised copy of the
embedding, over-fetching `VECTOR_RESCORE_CANDIDATES` rows; stage 2 rescores those
candidates against the full float32 vectors and truncates to k. The quantised
column is created only when the server can store one — `halfvec` arrived in
pgvector 0.7.0, and the embedded server here is 0.6.2 — so `capabilities()` is
checked at runtime and reported. On a server without it, stage 1 runs on the
float32 column and **stage 2 is a no-op**: same column, same metric, same order.
That is reported as `rescored: false` rather than presented as a win, because a
rescoring pass that reorders nothing is not a rescoring pass.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import Engine, func, select, text
from .. import config
from . import schema

#: Quantised element type, when the server has one. `halfvec` (float16) is
#: pgvector's own answer and needs no scale factors; a hand-rolled int8 column
#: would need per-vector scales stored alongside, and would still be re-read as
#: float32 for the rescoring pass, so it buys storage and nothing else.
QUANTISED_TYPE = "halfvec"
QUANTISED_COLUMN = "embedding_q"
DEFAULT_PARTITION = "vectors_default"


def _sql_literal(value: str) -> str:
    """A quoted SQL string literal, for the two places DDL forbids a bind param.

    Project ids come from build files on disk, not from anything a user types,
    but doubling the quote is a one-line cost and the alternative is a rule that
    holds only as long as nobody adds a new source of project ids.
    """
    return "'" + value.replace("'", "''") + "'"


def partition_name(project: str) -> str:
    """A safe, stable table name for a project.

    Project ids come from build files, so they contain slashes and dots. The
    hash suffix keeps two projects that normalise to the same slug apart.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", project.lower()).strip("_") or "unnamed"
    import hashlib

    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:8]
    return f"vectors_p_{slug[:40]}_{digest}"


@dataclass(slots=True)
class VectorFilter:
    """Everything that narrows a search before the distance metric is consulted."""

    snapshot_id: str
    #: partition pruning. Empty means every partition, which is what an unrouted
    #: query does and what Phase H exists to stop doing.
    projects: Sequence[str] = ()
    kinds: Sequence[str] = ()
    embed_model: str | None = None


@dataclass(slots=True)
class VectorHit:
    chunk_hash: str
    code_hash: str
    path: str
    kind: str
    name: str
    qualified_name: str | None
    start_line: int
    end_line: int
    tier: str
    project: str
    description: str
    distance: float
    #: distance from the approximate pass, kept so a rescoring that changed the
    #: order can be seen to have changed it
    approx_distance: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_hash": self.chunk_hash, "code_hash": self.code_hash, "path": self.path,
            "kind": self.kind, "name": self.name, "qualified_name": self.qualified_name,
            "start_line": self.start_line, "end_line": self.end_line, "tier": self.tier,
            "project": self.project, "description": self.description,
            "distance": self.distance, "approx_distance": self.approx_distance,
        }


class VectorStore(ABC):
    """What the rest of Bridge is allowed to ask of a vector engine."""

    @abstractmethod
    def search(self, filter: VectorFilter, vector: Sequence[float], k: int) -> list[VectorHit]:
        """The k nearest chunks to `vector` that `filter` admits."""

    @abstractmethod
    def upsert(self, rows: Sequence[dict]) -> int:
        """Insert vectors, ignoring ones already stored. Returns rows written."""

    @abstractmethod
    def ensure_partition(self, project: str) -> bool:
        """Give `project` its own partition and index. True if one was created."""

    @abstractmethod
    def partitions(self) -> dict[str, int]:
        """Partition name -> row count, including the default partition."""

    @abstractmethod
    def drop_empty_partitions(self) -> list[str]:
        """Remove partitions that outlived their rows. Returns what was dropped."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """What this engine can actually do, checked rather than assumed."""


# --------------------------------------------------------------------------- pgvector


class PgVectorStore(VectorStore):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._caps: dict[str, Any] | None = None

    # -- capability detection ------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        if self._caps is not None:
            return self._caps
        with self.engine.connect() as connection:
            version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            has_quantised = bool(
                connection.execute(
                    text("SELECT 1 FROM pg_type WHERE typname = :t"), {"t": QUANTISED_TYPE}
                ).scalar()
            )
            column_present = bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'vectors' AND column_name = :c"
                    ),
                    {"c": QUANTISED_COLUMN},
                ).scalar()
            )
            # 'p' is a partitioned table, 'r' an ordinary one. A database created
            # before Phase G's migration is still perfectly usable — it just gets
            # one index instead of one per project — so this is reported rather
            # than enforced.
            partitioned = (
                connection.execute(
                    text("SELECT relkind FROM pg_class WHERE relname = 'vectors'")
                ).scalar()
                == "p"
            )
        self._caps = {
            "engine": "pgvector",
            "extension_version": version,
            "partitioned": partitioned,
            "quantised_type": QUANTISED_TYPE if has_quantised else None,
            "quantised_column": column_present,
            # the honest headline: is stage 2 doing anything?
            "rescored": bool(column_present),
            "note": (
                ""
                if has_quantised
                else f"pgvector {version} has no {QUANTISED_TYPE}; stage 1 runs on float32 "
                "and the rescoring pass reorders nothing"
            ),
        }
        return self._caps

    # -- layout --------------------------------------------------------------

    def ensure_layout(self) -> None:
        """The default partition, and the quantised column if the server has one.

        A LIST-partitioned table with no partition accepts no rows at all, so the
        default partition is not an optimisation — without it the first insert
        fails. Per-project partitions are carved out of it later.
        """
        caps = self.capabilities()
        if not caps["partitioned"]:
            # An older schema. Nothing to lay out, and nothing broken.
            return
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {DEFAULT_PARTITION} "
                    "PARTITION OF vectors DEFAULT"
                )
            )
        if caps["quantised_type"] and not caps["quantised_column"]:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE vectors ADD COLUMN IF NOT EXISTS {QUANTISED_COLUMN} "
                        f"{QUANTISED_TYPE}({config.EMBED_DIMENSIONS})"
                    )
                )
            self._caps = None
        self.ensure_index(DEFAULT_PARTITION)

    def ensure_index(self, partition: str) -> None:
        """HNSW on one partition.

        Per partition rather than on the parent because that is what makes the
        index sizes independent: a project nobody queries does not make every
        other project's graph deeper.
        """
        column = QUANTISED_COLUMN if self.capabilities()["quantised_column"] else "embedding"
        ops = "halfvec_cosine_ops" if column == QUANTISED_COLUMN else "vector_cosine_ops"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {partition}_hnsw ON {partition} "
                    f"USING hnsw ({column} {ops})"
                )
            )

    def ensure_partition(self, project: str) -> bool:
        """Move one project's rows out of the default partition into their own.

        Postgres will not let a partition be created for values that already sit
        in the default partition, so the sequence is detach / create / move /
        reattach — in one transaction, because a detached default partition is a
        window in which inserts for every *other* unpartitioned project fail.
        """
        if not project or not self.capabilities()["partitioned"]:
            return False
        name = partition_name(project)
        with self.engine.connect() as connection:
            if connection.execute(
                text("SELECT 1 FROM pg_class WHERE relname = :n"), {"n": name}
            ).scalar():
                return False

        with self.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE vectors DETACH PARTITION {DEFAULT_PARTITION}"))
            # DDL takes no bind parameters — Postgres cannot infer a type for a
            # placeholder in a partition bound — so the value is quoted here
            # instead. `_sql_literal` is the only place in this module that
            # builds SQL from a string, and it is why it exists.
            connection.execute(
                text(f"CREATE TABLE {name} PARTITION OF vectors "
                     f"FOR VALUES IN ({_sql_literal(project)})")
            )
            connection.execute(
                text(
                    f"WITH moved AS (DELETE FROM {DEFAULT_PARTITION} WHERE project = :p RETURNING *) "
                    f"INSERT INTO {name} SELECT * FROM moved"
                ),
                {"p": project},
            )
            connection.execute(
                text(f"ALTER TABLE vectors ATTACH PARTITION {DEFAULT_PARTITION} DEFAULT")
            )
        self.ensure_index(name)
        return True

    def autopartition(self, minimum: int | None = None) -> list[str]:
        """Give a partition to every project big enough to be worth one.

        Below the threshold a partition is pure overhead: another relation,
        another HNSW graph, another planner decision, for a handful of rows that
        a sequential scan answers faster than an index would.
        """
        minimum = config.VECTOR_MIN_PARTITION_ROWS if minimum is None else minimum
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(schema.vectors.c.project, func.count().label("n"))
                .group_by(schema.vectors.c.project)
                .having(func.count() >= minimum)
                .order_by(schema.vectors.c.project)
            ).all()
        created = []
        for project, _count in rows:
            if project and self.ensure_partition(project):
                created.append(project)
        return created

    def drop_empty_partitions(self) -> list[str]:
        """Remove project partitions that hold nothing.

        A partition outlives its rows: the GC deletes vectors, and what is left
        is an empty relation with an HNSW index on it that every plan still has
        to consider. `vectors_default` is never dropped — it is what makes the
        table writable at all.
        """
        dropped = []
        for name, count in self.partitions().items():
            if name == DEFAULT_PARTITION or count:
                continue
            with self.engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE vectors DETACH PARTITION {name}"))
                connection.execute(text(f"DROP TABLE {name}"))
            dropped.append(name)
        return dropped

    def partitions(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            names = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_inherits i ON i.inhrelid = c.oid "
                        "JOIN pg_class p ON p.oid = i.inhparent "
                        "WHERE p.relname = 'vectors' ORDER BY c.relname"
                    )
                )
            ]
            return {
                name: connection.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
                for name in names
            }

    # -- writes --------------------------------------------------------------

    def upsert(self, rows: Sequence[dict]) -> int:
        from .repo import IndexRepo

        written = IndexRepo(self.engine).add_vectors(rows)
        if self.capabilities()["quantised_column"] and written:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE vectors SET {QUANTISED_COLUMN} = embedding::{QUANTISED_TYPE} "
                        f"WHERE {QUANTISED_COLUMN} IS NULL"
                    )
                )
        return written

    # -- reads ---------------------------------------------------------------

    def search(self, filter: VectorFilter, vector: Sequence[float], k: int) -> list[VectorHit]:
        """Approximate pass, then exact rescoring, then k.

        The over-fetch is `max(k, VECTOR_RESCORE_CANDIDATES)` and it is doing two
        jobs at once: it is the rescoring window, and it is the slack that lets
        the snapshot join throw rows away without emptying the result. Those are
        different concerns and it would be cleaner to have two numbers, but they
        pull in the same direction and one is easier to reason about.
        """
        caps = self.capabilities()
        column = QUANTISED_COLUMN if caps["quantised_column"] else "embedding"
        over_fetch = max(k, config.VECTOR_RESCORE_CANDIDATES)
        literal = "[" + ",".join(f"{value:.7g}" for value in vector) + "]"

        where = ["TRUE"]
        params: dict[str, Any] = {"q": literal, "n": over_fetch, "snap": filter.snapshot_id, "k": k}
        if filter.projects:
            where.append("v.project = ANY(:projects)")
            params["projects"] = list(filter.projects)
        if filter.embed_model:
            where.append("v.embed_model = :embed_model")
            params["embed_model"] = filter.embed_model
        kind_clause = ""
        if filter.kinds:
            kind_clause = "AND c.kind = ANY(:kinds)"
            params["kinds"] = list(filter.kinds)

        cast = f"::{QUANTISED_TYPE}({config.EMBED_DIMENSIONS})" if column == QUANTISED_COLUMN else ""
        statement = text(
            f"""
            WITH approx AS (
                SELECT v.vec_key, v.desc_key, v.project,
                       v.{column} <=> (:q)::vector({config.EMBED_DIMENSIONS}){cast} AS approx_distance
                FROM vectors v
                WHERE {' AND '.join(where)}
                ORDER BY approx_distance
                LIMIT :n
            ),
            joined AS (
                SELECT DISTINCT ON (c.chunk_hash)
                       c.chunk_hash, c.code_hash, c.kind, c.name, c.qualified_name,
                       c.start_line, c.end_line, c.tier, a.project, sf.path,
                       d.text AS description, a.approx_distance,
                       v.embedding <=> (:q)::vector({config.EMBED_DIMENSIONS}) AS distance
                FROM approx a
                JOIN vectors v ON v.vec_key = a.vec_key AND v.project = a.project
                JOIN descriptions d ON d.desc_key = a.desc_key
                JOIN chunks c ON c.code_hash = d.code_hash
                JOIN snapshot_files sf ON sf.blob_hash = c.blob_hash
                WHERE sf.snapshot_id = :snap {kind_clause}
                ORDER BY c.chunk_hash, distance
            )
            SELECT * FROM joined ORDER BY distance, chunk_hash LIMIT :k
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement, params).mappings().all()
        return [
            VectorHit(
                chunk_hash=row["chunk_hash"], code_hash=row["code_hash"], path=row["path"],
                kind=row["kind"], name=row["name"], qualified_name=row["qualified_name"],
                start_line=row["start_line"], end_line=row["end_line"], tier=row["tier"],
                project=row["project"], description=row["description"] or "",
                distance=float(row["distance"]), approx_distance=float(row["approx_distance"]),
            )
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        return {"capabilities": self.capabilities(), "partitions": self.partitions()}


# --------------------------------------------------------------------------- stub


class ExternalVectorStore(VectorStore):
    """A placeholder for a dedicated engine (Qdrant, Vespa, Turbopuffer, ...).

    It exists to hold the shape of the problem, not to hide it. Every method
    raises, with the specific thing that implementation would have to solve —
    because the hard part of moving vectors out of Postgres is not the ANN
    search, it is that `search()` currently answers a question spanning four
    tables (`vectors`, `descriptions`, `chunks`, `snapshot_files`), and an
    external engine sees only the first. Either the snapshot scope becomes a
    payload filter it can evaluate itself, or every query becomes a round trip
    out and back, and that choice is the whole design.
    """

    def __init__(self, endpoint: str = "", collection: str = "bridge") -> None:
        self.endpoint = endpoint
        self.collection = collection

    def _unbuilt(self, what: str) -> NotImplementedError:
        return NotImplementedError(
            f"ExternalVectorStore.{what} is a stub. To implement it: {self.REQUIREMENTS[what]}"
        )

    REQUIREMENTS = {
        "search": (
            "carry snapshot_id, project, kind and tier as indexed payload fields so the "
            "snapshot scope is a filter the engine applies during the ANN walk, not a join "
            "Bridge performs afterwards on rows the walk already truncated"
        ),
        "upsert": (
            "write vec_key as the point id so it stays content-addressed and re-indexing the "
            "same code is idempotent without a read-modify-write"
        ),
        "ensure_partition": (
            "map a project onto whatever the engine calls a partition (collection, shard key, "
            "namespace) and keep that mapping out of the caller's vocabulary"
        ),
        "partitions": "report row counts per partition for /health/index",
        "drop_empty_partitions": (
            "let the retention sweep reclaim an emptied namespace, so a project that "
            "was fully collected stops costing anything"
        ),
        "capabilities": "report the engine's quantisation and filtering support honestly",
    }

    def search(self, filter: VectorFilter, vector: Sequence[float], k: int) -> list[VectorHit]:
        raise self._unbuilt("search")

    def upsert(self, rows: Sequence[dict]) -> int:
        raise self._unbuilt("upsert")

    def ensure_partition(self, project: str) -> bool:
        raise self._unbuilt("ensure_partition")

    def partitions(self) -> dict[str, int]:
        raise self._unbuilt("partitions")

    def drop_empty_partitions(self) -> list[str]:
        raise self._unbuilt("drop_empty_partitions")

    def capabilities(self) -> dict[str, Any]:
        return {"engine": "external", "endpoint": self.endpoint, "built": False}


def get_vector_store(engine: Engine) -> VectorStore:
    """The one place that decides which store is in use."""
    return PgVectorStore(engine)
