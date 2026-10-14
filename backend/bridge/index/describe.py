"""Plain-English descriptions for every describable chunk.

One or two sentences per method / class / screen, written for a product manager,
produced by the model configured for the `describe` role in
:mod:`bridge.config` (currently Gemini 2.5 Flash-Lite) through :mod:`bridge.agents.llm`.

Three properties matter more than the wording:

* **The cache holds model output only.** ``descriptions.sqlite`` is a cache of
  *expensive* results. Deterministic fallbacks are free to recompute, so they are
  never written — otherwise a run that degraded once would poison every later run
  that could have done better.
* **The key covers everything that determines the answer** — the chunk text, the
  model, and the prompt. Editing ``prompts/describe.md`` or switching the
  describer's model invalidates the cache, because either changes the output.
* **Redacted before it leaves the process.** Every chunk goes through
  :mod:`bridge.redact` on its way into the prompt (design law 8), and the counts
  are reported rather than silently dropped.

With no credentials the pass does not invent prose: it falls back to a
deterministic description derived from the node's own signature, returns it in
:attr:`DescribeResult.descriptions`, and leaves the cache untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..agents.llm import LLMClient, load_prompt, wrap_retrieved
from ..redact import RedactionLog

SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptions (
  content_sha256 TEXT PRIMARY KEY,
  node_id        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  description    TEXT NOT NULL,
  source         TEXT NOT NULL,          -- model id, or 'fallback'
  model          TEXT,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL    NOT NULL DEFAULT 0.0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desc_node ON descriptions(node_id);
CREATE INDEX IF NOT EXISTS idx_desc_source ON descriptions(source);
"""

_WORD_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")


@dataclass(slots=True)
class DescribeResult:
    described: int = 0
    from_cache: int = 0
    from_model: int = 0
    from_fallback: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0
    redaction_counts: dict[str, int] = field(default_factory=dict)
    redacted_chunks: int = 0
    llm_available: bool = False
    unavailable_reason: str | None = None
    #: node_id -> description for EVERY described node, model-written or fallback.
    #: This is the complete working set; the sqlite cache holds only the model half.
    descriptions: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- store


class DescriptionStore:
    """`data/index/<sha>/descriptions.sqlite` — the content-addressed cache."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str | Path) -> "DescriptionStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(sqlite3.connect(path, check_same_thread=False))
        store.conn.executescript(SCHEMA)
        return store

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "DescriptionStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get(self, content_sha: str) -> sqlite3.Row | None:
        """A cached *model* description, or None.

        Fallback rows are excluded at the read, not merely avoided at the write.
        A legacy store written before that rule — or one where a transient API
        error degraded a few chunks — must not be able to serve a deterministic
        stub as though the model had produced it.
        """
        return self.conn.execute(
            "SELECT * FROM descriptions WHERE content_sha256 = ? AND source != 'fallback'",
            (content_sha,),
        ).fetchone()

    def put(
        self,
        content_sha: str,
        node_id: str,
        kind: str,
        description: str,
        source: str,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO descriptions(content_sha256, node_id, kind, description, source, model,"
            " input_tokens, output_tokens, cost_usd, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?, datetime('now'))"
            " ON CONFLICT(content_sha256) DO UPDATE SET"
            "   node_id=excluded.node_id, description=excluded.description,"
            "   source=excluded.source, model=excluded.model",
            (
                content_sha,
                node_id,
                kind,
                description,
                source,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )

    def for_node(self, node_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT description FROM descriptions WHERE node_id = ? ORDER BY created_at DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        return row["description"] if row else None

    def all_descriptions(self) -> dict[str, str]:
        """Every cached model description. Fallbacks are not stored, so not returned."""
        return {
            row["node_id"]: row["description"]
            for row in self.conn.execute(
                "SELECT node_id, description FROM descriptions"
                " WHERE source != 'fallback' ORDER BY created_at"
            )
        }

    def count_by_source(self) -> dict[str, int]:
        return {
            row["source"]: row["c"]
            for row in self.conn.execute(
                "SELECT source, COUNT(*) AS c FROM descriptions GROUP BY source"
            )
        }


# --------------------------------------------------------------------------- chunking


def humanise(name: str) -> str:
    """`processCreationForm` -> `process creation form`."""
    return " ".join(part for part in _WORD_SPLIT.split(name) if part).lower()


def source_slice(repo_root: Path, path: str, start: int | None, end: int | None) -> str:
    """The node's own lines, capped so one huge class cannot dominate a prompt."""
    try:
        lines = (repo_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    first = max(0, (start or 1) - 1)
    last = min(len(lines), end or len(lines))
    return "\n".join(lines[first:last])[: config.DESCRIBE_MAX_SOURCE_CHARS]


def chunk_text(row, repo_root: Path) -> str:
    """The exact text a describer sees — and the thing the cache key hashes."""
    attrs = json.loads(row["attrs"])
    header = [
        f"kind: {row['kind']}",
        f"name: {row['name']}",
        f"location: {row['path']}:{row['start_line']}-{row['end_line']}",
    ]
    if row["qualified_name"]:
        header.append(f"qualified name: {row['qualified_name']}")
    if attrs.get("signature"):
        header.append(f"signature: {attrs['signature']}")
    if attrs.get("annotations"):
        header.append(f"annotations: {', '.join(attrs['annotations'])}")
    if attrs.get("table"):
        header.append(f"database table: {attrs['table']}")
    if attrs.get("route"):
        header.append(f"route: {attrs.get('http_method', '')} {attrs['route']}")
    if attrs.get("is_test"):
        header.append("this is test code")

    body = source_slice(repo_root, row["path"], row["start_line"], row["end_line"])
    return "\n".join(header) + ("\n\n" + body if body else "")


def content_key(text: str, model: str = "", prompt: str = "") -> str:
    """Cache key over everything that determines the description.

    Hashing the chunk text alone is not enough: the same code produces a
    different description under a different prompt or a different model, so a
    key that ignores them serves stale output after an edit that was meant to
    change it. The fields are NUL-joined so no combination can collide.
    """
    payload = "\x00".join((model, prompt, text))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fallback_description(row) -> str:
    """A deterministic description when no model is available.

    Derived from the node's own name and annotations — never presented as model
    output, and stored with ``source='fallback'`` so it can be found and replaced.
    """
    attrs = json.loads(row["attrs"])
    name = humanise(row["name"])
    kind = row["kind"]

    if kind == "endpoint":
        return f"The {attrs.get('http_method', '')} {attrs.get('route', '')} web address.".strip()
    if kind == "entity":
        return f"Stored {name} records, kept in the {attrs.get('table', name)} table."
    if kind == "screen":
        return f"The {name} page."
    if kind == "method":
        owner = humanise((attrs.get("owner") or "").rsplit(".", 1)[-1])
        return f"The {name} step of {owner}." if owner else f"The {name} step."
    return f"The {name} {kind}."


# --------------------------------------------------------------------------- pass


def run(
    store,
    repo_root: Path,
    db_path: Path,
    llm: LLMClient,
    *,
    kinds: tuple[str, ...] = config.DESCRIBE_KINDS,
    include_tests: bool = False,
    concurrency: int = config.DESCRIBE_CONCURRENCY,
    progress=None,
) -> DescribeResult:
    """Describe every node of ``kinds``, caching by content hash.

    ``store`` is an open :class:`~bridge.index.graph.GraphStore`.
    """
    started = time.perf_counter()
    # Fail before the work, not after: a missing key is a misconfiguration, and a
    # run that quietly produced 130 deterministic stubs would look like a success.
    llm.require("describe")
    result = DescribeResult(
        llm_available=llm.available_for("describe"),
        unavailable_reason=llm.reason_for("describe"),
    )
    system = load_prompt("describe")
    model_id = config.ROLES["describe"]["model"]
    redaction = RedactionLog()

    placeholders = ",".join("?" * len(kinds))
    rows = [
        row
        for row in store.conn.execute(
            f"SELECT * FROM nodes WHERE kind IN ({placeholders}) ORDER BY id", tuple(kinds)
        )
        if include_tests or not json.loads(row["attrs"]).get("is_test")
    ]

    # Build every chunk first: redaction happens here, once, before anything leaves.
    prepared: list[tuple] = []
    for row in rows:
        raw = chunk_text(row, repo_root)
        clean = redaction.scrub(raw)
        prepared.append((row, clean, content_key(clean, model_id, system)))

    result.redaction_counts = redaction.counts
    result.redacted_chunks = redaction.chunks_redacted

    with DescriptionStore.open(db_path) as descriptions:
        pending: list[tuple] = []
        for row, clean, key in prepared:
            cached = descriptions.get(key)  # model rows only
            if cached is not None:
                result.from_cache += 1
                result.described += 1
                result.descriptions[row["id"]] = cached["description"]
                # keep node_id current even when the body moved
                descriptions.put(
                    key,
                    row["id"],
                    row["kind"],
                    cached["description"],
                    cached["source"],
                    cached["model"],
                )
                continue
            pending.append((row, clean, key))

        if progress is not None:
            progress(
                f"describe: {result.from_cache} cached, {len(pending)} to generate"
                + ("" if result.llm_available else f" (no LLM: {result.unavailable_reason})")
            )

        if pending and result.llm_available:
            prompts = [
                f"{wrap_retrieved(clean)}\n\nDescribe this for a product manager."
                for _row, clean, _key in pending
            ]
            responses = llm.complete_many(
                "describe", prompts, system=system, concurrency=concurrency
            )
            for (row, _clean, key), response in zip(pending, responses):
                if response is None or response.refused or not response.text:
                    # Degraded, so NOT cached — a later run with a working key
                    # must get a real description rather than this stub.
                    result.descriptions[row["id"]] = fallback_description(row)
                    result.from_fallback += 1
                    result.failed += 1
                else:
                    descriptions.put(
                        key,
                        row["id"],
                        row["kind"],
                        response.text,
                        response.model,
                        response.model,
                        response.input_tokens,
                        response.output_tokens,
                        response.cost_usd,
                    )
                    result.descriptions[row["id"]] = response.text
                    result.from_model += 1
                    result.cost_usd += response.cost_usd
                    result.input_tokens += response.input_tokens
                    result.output_tokens += response.output_tokens
                result.described += 1
        elif pending:
            for row, _clean, _key in pending:
                result.descriptions[row["id"]] = fallback_description(row)
                result.from_fallback += 1
                result.described += 1

        descriptions.conn.commit()

    result.elapsed = time.perf_counter() - started
    return result
