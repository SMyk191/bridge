"""Nightly: does `config/vocab.<repo>.yaml` still point at real code?

A vocab term's `nodes` are seeds — exact graph ids the term resolves to
(`retrieval/vocab.py`). A node id is nothing but a coordinate; the moment the
code it names is renamed, moved, or deleted, the term silently stops doing
anything, and nobody notices because a missing seed just means one less
signal in retrieval, not an error. This turns that silence into a flagged row
in `vocab_proposals`.

**Detection** is exact: `GraphStore.node(node_id) is None` in the CURRENT
index means the term is stale, full stop.

**Replacement mining is a heuristic, named as one.** The honest version — "this
exact code moved from A to B" — needs a `code_hash` that survives a rename, and
that only exists on the Postgres content-addressed index (Phase E onward); the
legacy SQLite graph node ids carry no history to walk. So a candidate here is
"a live node of the same kind with the same bare name" — real signal (a class
or method rarely changes its own name on a move) but not proof, which is
exactly why it becomes a `vocab_proposals` row for a human to confirm rather
than an automatic rewrite of the vocab file. `config.py` documents the upgrade
path once retrieval is cut over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..index import ids
from ..retrieval.vocab import Vocabulary


@dataclass(slots=True)
class StaleTerm:
    term: str
    dead_nodes: list[str] = field(default_factory=list)
    #: dead node id -> live node ids proposed as its replacement (possibly empty)
    candidates: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "dead_nodes": self.dead_nodes, "candidates": self.candidates}


def _bare_name(node_id: str) -> tuple[str, str] | None:
    """`(kind, simple name)` used to search for a same-name replacement, or
    None for a scheme this heuristic has no sensible name for."""
    scheme = ids.scheme_of(node_id)
    rest = node_id.split(":", 1)[1] if ":" in node_id else ""
    if scheme == ids.JAVA:
        owner_or_method = rest.rsplit("#", 1)
        if len(owner_or_method) == 2:
            method = owner_or_method[1].split("(", 1)[0]
            return "method", method
        return "type", rest.rsplit(".", 1)[-1]
    if scheme == ids.ENTITY:
        return "entity", rest.rsplit(".", 1)[-1]
    if scheme == ids.SCREEN:
        return "screen", rest.rsplit("/", 1)[-1]
    if scheme == ids.FILE:
        return "file", rest.rsplit("/", 1)[-1]
    return None


def find_candidates(store, dead_node_id: str, exclude: str) -> list[str]:
    """Live nodes with the same (kind bucket, bare name) as the dead one.

    `nodes.name` already holds the bare simple name at index time (index/model.py's
    `Node.name` vs `Node.qualified_name`), so this is a direct column match —
    the parsing above exists only to get a name to match FOR a dead id that no
    longer has its own row to read `name` off of.
    """
    parsed = _bare_name(dead_node_id)
    if parsed is None:
        return []
    _bucket, name = parsed
    rows = store.conn.execute(
        "SELECT id FROM nodes WHERE name = ? AND id != ? ORDER BY id", (name, exclude)
    ).fetchall()
    return [row["id"] for row in rows]


def stale_terms(vocabulary: Vocabulary, store) -> list[StaleTerm]:
    found: list[StaleTerm] = []
    for term in vocabulary.terms:
        dead = [node_id for node_id in term.nodes if store.node(node_id) is None]
        if not dead:
            continue
        found.append(StaleTerm(
            term=term.term, dead_nodes=dead,
            candidates={node_id: find_candidates(store, node_id, node_id) for node_id in dead},
        ))
    return found


def check(vocabulary: Vocabulary, store, repo_name: str) -> dict[str, Any]:
    """The job body: find stale terms, write one `vocab_proposals` row per dead
    node that has at least one replacement candidate (a dead node with NO
    candidate is still reported, just not proposed — there is nothing to
    propose)."""
    from ..api import db as vocab_db

    stale = stale_terms(vocabulary, store)
    proposed = 0
    for entry in stale:
        for dead_node, candidates in entry.candidates.items():
            if not candidates:
                continue
            note = (
                f"vocab term {entry.term!r} names {dead_node!r}, which no longer exists in "
                f"{repo_name}'s current index. Same-name candidate(s) found: {candidates}. "
                "Heuristic match (same kind + bare name), not a verified rename — confirm "
                "before approving."
            )
            vocab_db.record_vocab_proposal(entry.term, candidates, note)
            proposed += 1
    return {
        "repo": repo_name,
        "terms_checked": len(vocabulary.terms),
        "stale_terms": len(stale),
        "dead_nodes": sum(len(e.dead_nodes) for e in stale),
        "proposals_written": proposed,
        "details": [e.as_dict() for e in stale],
    }
