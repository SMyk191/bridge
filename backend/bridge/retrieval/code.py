"""Fill in the real source for chunks an agent is about to reason about.

Retrieval populates ``Chunk.code`` only for chunks the **semantic** stage found —
that is where a LanceDB row (and its stored source slice) comes from. A chunk
that arrived via the vocabulary, the keyword stage, or graph expansion carries an
empty ``code``, and those are frequently the ones that matter: an entity, a
screen reached from a controller, a repository named by a curated term.

Worse, some node kinds are never embedded at all — only the kinds in
``config.DESCRIBE_KINDS`` get a description, and only a described node gets a
vector row. `entity`, `endpoint`, and `file` nodes therefore have no stored code
anywhere, so the index alone cannot answer "show me this".

So this module fills the gap in two passes:

1. the vector table, by node id — one scan, already redacted at embed time;
2. the repository on disk, for whatever the table did not have.

Pass 2 is the only place outside the indexer that reads the target repository, and
it only ever *reads* — Bridge never writes to a repo (CLAUDE.md). It is also the
only path where secrets have not already been scrubbed, so it redacts explicitly
rather than trusting that the file is clean.

What it will not do is return a silent empty string. A chunk whose source could
not be read carries :data:`UNAVAILABLE` into the prompt, so an agent can name it
as a gap instead of describing code nobody handed it (design law 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .. import config
from ..redact import redact_text

#: what a prompt sees when the source genuinely could not be read
UNAVAILABLE = "(source unavailable — not in the index and the repository is not readable here)"


def source_slice(repo_root: Path, path: str, start: int | None, end: int | None,
                 limit_chars: int = config.AGENT_CODE_MAX_CHARS) -> str:
    """Lines ``start``-``end`` of ``repo_root/path``, or "" if unreadable.

    Mirrors ``index/embed._source_slice`` — same 1-based inclusive line window and
    same character cap — so a chunk's code is byte-identical whether it was read
    here or written into the vector table at index time.
    """
    try:
        lines = (repo_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    first = max(0, (start or 1) - 1)
    last = min(len(lines), end or len(lines))
    return "\n".join(lines[first:last])[:limit_chars]


def hydrate(retriever, chunks: Sequence, *, limit_chars: int = config.AGENT_CODE_MAX_CHARS) -> int:
    """Fill ``chunk.code`` in place for every chunk missing it. Returns how many were filled.

    ``retriever`` is a :class:`~bridge.retrieval.retrieve.Retriever`; only its
    ``vectors`` and ``repo_root`` are used, so a test can pass any object with
    those two attributes.
    """
    missing = [chunk for chunk in chunks if not chunk.code]
    if not missing:
        return 0

    filled = 0
    vectors = getattr(retriever, "vectors", None)
    if vectors is not None:
        try:
            rows = vectors.by_node_ids(chunk.node_id for chunk in missing)
        except Exception:  # a broken vector table must not sink the run
            rows = {}
        for chunk in missing:
            row = rows.get(chunk.node_id)
            code = (row or {}).get("code") or ""
            if code:
                chunk.code = code[:limit_chars]
                filled += 1

    repo_root = getattr(retriever, "repo_root", None)
    still_missing = [chunk for chunk in missing if not chunk.code]
    if still_missing and repo_root is not None:
        for chunk in still_missing:
            code = source_slice(Path(repo_root), chunk.path, chunk.start_line, chunk.end_line,
                                limit_chars)
            if code:
                # Disk is the one path embed-time redaction never touched.
                chunk.code = redact_text(code)
                filled += 1
    return filled


def render(chunk, *, history: dict | None = None) -> str:
    """One chunk as an agent sees it: what it is, where it is, how it was reached, its code."""
    lines = [f"### {chunk.name}  [{chunk.kind}]", f"citation: {chunk.citation}"]
    if chunk.description:
        lines.append(f"what it does: {chunk.description}")
    if chunk.graph_path:
        lines.append(f"reached by: {chunk.graph_path}")
    note = history if history is not None else (chunk.history or {})
    if note:
        churn = (
            f"history: {note.get('commits_all_time', 0)} commits all time, "
            f"{note.get('commits_recent', 0)} in the last {note.get('recent_window_days', '?')} days"
        )
        if note.get("last_changed"):
            churn += f", last changed {note['last_changed']}"
        lines.append(churn)
    lines.append("```")
    lines.append(chunk.code or UNAVAILABLE)
    lines.append("```")
    return "\n".join(lines)


def render_all(chunks: Iterable) -> str:
    return "\n\n".join(render(chunk) for chunk in chunks)
