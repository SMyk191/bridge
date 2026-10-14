"""Semantic search over the graph: BAAI/bge-small-en-v1.5 vectors in LanceDB.

The text that gets embedded is a **breadcrumb plus the plain-English
description**, not the source code::

    spring-petclinic › owner › OwnerController.java › OwnerController › processCreationForm
    Saves a newly filled-in owner form and sends the user to that owner's page.

That shape is deliberate. A product question ("how does someone add a pet?") is
written in product language, and matching it against Java identifiers is a losing
game. The breadcrumb carries the location vocabulary; the description carries the
behaviour. The raw code stays in the payload as a citation target — it is never
what the query is compared to.

Vectors live in ``data/index/<sha>/vectors``. Everything is redacted on the way in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..redact import RedactionLog, redact_text

_MODEL_CACHE: dict[str, Any] = {}


@dataclass(slots=True)
class EmbedResult:
    chunks: int = 0
    dimensions: int = 0
    elapsed: float = 0.0
    table_path: str = ""
    model: str = config.EMBED_MODEL
    redaction_counts: dict[str, int] = field(default_factory=dict)
    redacted_chunks: int = 0
    skipped_no_description: int = 0


@dataclass(slots=True)
class SearchHit:
    node_id: str
    kind: str
    name: str
    path: str
    start_line: int
    end_line: int
    description: str
    score: float
    breadcrumb: str
    code: str = ""

    @property
    def lines(self) -> str:
        return f"{self.start_line}-{self.end_line}"


def load_encoder(model_name: str = config.EMBED_MODEL):
    """Load (and memoise) the sentence-transformers encoder."""
    if model_name not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer

        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


# --------------------------------------------------------------------------- text


def module_of(path: str) -> str:
    """`src/main/java/org/.../owner/OwnerController.java` -> `owner`."""
    parts = Path(path).parts
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def breadcrumb(repo_name: str, row, owner_name: str | None = None) -> str:
    """`<repo> › <module> › <file> › <class> › <name>` — the location half of the text."""
    path = row["path"]
    trail = [repo_name, module_of(path), Path(path).name]
    if owner_name and owner_name != row["name"]:
        trail.append(owner_name)
    trail.append(row["name"])
    seen: list[str] = []
    for part in trail:
        if part and (not seen or seen[-1] != part):
            seen.append(part)
    return " › ".join(seen)


def embed_text(crumb: str, description: str) -> str:
    return f"{crumb}\n{description}".strip()


# --------------------------------------------------------------------------- build


def _owner_names(store) -> dict[str, str]:
    """method node id -> its declaring type's simple name."""
    owners: dict[str, str] = {}
    for row in store.conn.execute(
        "SELECT e.dst AS method, n.name AS owner FROM edges e"
        " JOIN nodes n ON n.id = e.src WHERE e.kind = 'contains' AND n.kind IN"
        " ('class','interface','enum','record')"
    ):
        owners[row["method"]] = row["owner"]
    return owners


def run(
    store,
    repo_root: Path,
    repo_name: str,
    vectors_dir: Path,
    descriptions: dict[str, str],
    *,
    model_name: str = config.EMBED_MODEL,
    progress=None,
) -> EmbedResult:
    """Embed every node that has a description and write the LanceDB table."""
    import lancedb
    import pyarrow as pa

    started = time.perf_counter()
    result = EmbedResult(model=model_name, table_path=str(vectors_dir))
    redaction = RedactionLog()
    owners = _owner_names(store)

    records: list[dict[str, Any]] = []
    texts: list[str] = []
    for row in store.conn.execute("SELECT * FROM nodes ORDER BY id"):
        description = descriptions.get(row["id"])
        if not description:
            result.skipped_no_description += 1
            continue
        attrs = json.loads(row["attrs"])
        crumb = breadcrumb(repo_name, row, owners.get(row["id"]))
        text = redaction.scrub(embed_text(crumb, description))
        texts.append(text)
        records.append(
            {
                "node_id": row["id"],
                "kind": row["kind"],
                "name": row["name"],
                "qualified_name": row["qualified_name"] or "",
                "path": row["path"],
                "start_line": int(row["start_line"] or 0),
                "end_line": int(row["end_line"] or 0),
                "breadcrumb": crumb,
                "description": description,
                "text": text,
                "is_test": bool(attrs.get("is_test")),
                "is_hub": bool(row["is_hub"]),
                "code": redaction.scrub(
                    _source_slice(repo_root, row["path"], row["start_line"], row["end_line"])
                ),
            }
        )

    result.redaction_counts = redaction.counts
    result.redacted_chunks = redaction.chunks_redacted

    if not records:
        result.elapsed = time.perf_counter() - started
        return result

    if progress is not None:
        progress(f"embed: encoding {len(records)} chunks with {model_name}")

    encoder = load_encoder(model_name)
    vectors = encoder.encode(
        texts,
        batch_size=config.EMBED_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    dimensions = int(vectors.shape[1])
    for record, vector in zip(records, vectors):
        record["vector"] = vector.tolist()

    vectors_dir.parent.mkdir(parents=True, exist_ok=True)
    database = lancedb.connect(str(vectors_dir))
    schema = pa.schema(
        [
            pa.field("vector", pa.list_(pa.float32(), dimensions)),
            pa.field("node_id", pa.string()),
            pa.field("kind", pa.string()),
            pa.field("name", pa.string()),
            pa.field("qualified_name", pa.string()),
            pa.field("path", pa.string()),
            pa.field("start_line", pa.int32()),
            pa.field("end_line", pa.int32()),
            pa.field("breadcrumb", pa.string()),
            pa.field("description", pa.string()),
            pa.field("text", pa.string()),
            pa.field("is_test", pa.bool_()),
            pa.field("is_hub", pa.bool_()),
            pa.field("code", pa.string()),
        ]
    )
    database.create_table(config.VECTORS_TABLE_NAME, data=records, schema=schema, mode="overwrite")

    result.chunks = len(records)
    result.dimensions = dimensions
    result.elapsed = time.perf_counter() - started
    return result


def _source_slice(repo_root: Path, path: str, start: int | None, end: int | None) -> str:
    try:
        lines = (repo_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    first = max(0, (start or 1) - 1)
    last = min(len(lines), end or len(lines))
    return "\n".join(lines[first:last])[: config.DESCRIBE_MAX_SOURCE_CHARS]


# --------------------------------------------------------------------------- search


class VectorIndex:
    """Read side of the LanceDB table."""

    def __init__(self, table, model_name: str = config.EMBED_MODEL) -> None:
        self.table = table
        self.model_name = model_name

    @classmethod
    def open(cls, vectors_dir: str | Path, model_name: str = config.EMBED_MODEL) -> "VectorIndex":
        import lancedb

        database = lancedb.connect(str(vectors_dir))
        return cls(database.open_table(config.VECTORS_TABLE_NAME), model_name)

    @classmethod
    def exists(cls, vectors_dir: str | Path) -> bool:
        return (Path(vectors_dir) / f"{config.VECTORS_TABLE_NAME}.lance").exists()

    def __len__(self) -> int:
        return self.table.count_rows()

    def semantic_search(
        self,
        query: str,
        k: int = config.RETRIEVE_SEMANTIC_K,
        *,
        include_tests: bool = False,
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """Nearest ``k`` chunks to ``query`` by cosine similarity.

        The query gets bge's retrieval prefix; documents deliberately do not —
        that asymmetry is what the model was trained for.
        """
        encoder = load_encoder(self.model_name)
        vector = encoder.encode(
            config.EMBED_QUERY_PREFIX + query, normalize_embeddings=True
        ).tolist()

        # over-fetch so post-filtering can still return k
        search = self.table.search(vector).metric("cosine").limit(max(k * 4, k + 20))
        rows = search.to_list()

        wanted = set(kinds) if kinds else None
        hits: list[SearchHit] = []
        for row in rows:
            if not include_tests and row.get("is_test"):
                continue
            if wanted is not None and row["kind"] not in wanted:
                continue
            hits.append(
                SearchHit(
                    node_id=row["node_id"],
                    kind=row["kind"],
                    name=row["name"],
                    path=row["path"],
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    description=row["description"],
                    # LanceDB returns cosine *distance*; convert to similarity
                    score=1.0 - float(row.get("_distance", 0.0)),
                    breadcrumb=row["breadcrumb"],
                    code=row.get("code", ""),
                )
            )
            if len(hits) >= k:
                break
        return hits

    def get(self, node_id: str) -> dict[str, Any] | None:
        rows = (
            self.table.search()
            .where(f"node_id = '{node_id}'".replace("\\", ""), prefilter=True)
            .limit(1)
            .to_list()
        )
        return rows[0] if rows else None

    def by_node_ids(self, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = set(node_ids)
        if not wanted:
            return {}
        return {
            row["node_id"]: row
            for row in self.table.to_arrow().to_pylist()
            if row["node_id"] in wanted
        }


def semantic_search(
    vectors_dir: str | Path, query: str, k: int = config.RETRIEVE_SEMANTIC_K, **kwargs
) -> list[SearchHit]:
    """Convenience wrapper: open the index and search it in one call."""
    return VectorIndex.open(vectors_dir).semantic_search(query, k, **kwargs)
