"""The SQLite graph store: schema, writes, degrees, hubs, traversal, summary queries."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from .model import SEMANTIC_EDGE_KINDS, Edge, Node, Unresolved
from .model import TYPE_KINDS, EdgeKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id                 TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  name               TEXT NOT NULL,
  qualified_name     TEXT,
  path               TEXT NOT NULL,
  start_line         INTEGER,
  end_line           INTEGER,
  lang               TEXT,
  in_degree          INTEGER NOT NULL DEFAULT 0,
  out_degree         INTEGER NOT NULL DEFAULT 0,
  semantic_in_degree INTEGER NOT NULL DEFAULT 0,
  is_hub             INTEGER NOT NULL DEFAULT 0,
  attrs              TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_hub  ON nodes(is_hub, semantic_in_degree DESC);

CREATE TABLE IF NOT EXISTS edges (
  id         INTEGER PRIMARY KEY,
  src        TEXT NOT NULL REFERENCES nodes(id),
  dst        TEXT NOT NULL REFERENCES nodes(id),
  kind       TEXT NOT NULL,
  confidence TEXT NOT NULL DEFAULT 'high',
  path       TEXT,
  line       INTEGER,
  attrs      TEXT NOT NULL DEFAULT '{}',
  UNIQUE(src, dst, kind, line)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind);

CREATE TABLE IF NOT EXISTS unresolved (
  id     INTEGER PRIMARY KEY,
  kind   TEXT NOT NULL,
  src    TEXT,
  symbol TEXT NOT NULL,
  path   TEXT,
  line   INTEGER,
  reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unresolved_reason ON unresolved(reason);
"""


@dataclass(slots=True)
class Hop:
    node_id: str
    kind: str
    name: str
    path: str
    start_line: int | None
    end_line: int | None
    via_edge: str | None = None
    confidence: str | None = None
    is_hub: bool = False


@dataclass(slots=True)
class TraversalPath:
    hops: list[Hop]

    @property
    def terminal(self) -> str:
        return self.hops[-1].node_id

    @property
    def depth(self) -> int:
        return sum(1 for hop in self.hops[1:] if hop.via_edge != _MEMBER_EDGE)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return " -> ".join(
            hop.node_id if hop.via_edge is None else f"[{hop.via_edge}] {hop.node_id}"
            for hop in self.hops
        )


@dataclass(slots=True)
class TraversalResult:
    root: str
    paths: list[TraversalPath] = field(default_factory=list)
    stopped_at_hubs: list[str] = field(default_factory=list)
    truncated_by_nodes: bool = False
    truncated_by_depth: bool = False

    @property
    def node_ids(self) -> set[str]:
        return {path.terminal for path in self.paths}

    def reaches(self, node_id: str) -> bool:
        return node_id in self.node_ids

    def path_to(self, node_id: str) -> TraversalPath | None:
        return next((p for p in self.paths if p.terminal == node_id), None)


#: label used for the zero-cost type -> member expansion (see GraphStore.traverse)
_MEMBER_EDGE = f"{EdgeKind.CONTAINS}(member)"


class GraphStore:
    """Reads and writes ``data/index/<sha>/graph.db``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, db_path: str | Path, *, fresh: bool = False) -> "GraphStore":
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and db_path.exists():
            db_path.unlink()
        # check_same_thread=False: the orchestrator runs each node in a worker
        # thread via asyncio.to_thread, so the connection outlives the thread that
        # opened it. Nodes run strictly one at a time, so there is no concurrent
        # access to guard against — except at shutdown/cancellation, where a
        # thread already started can outlive the coroutine awaiting it; the API
        # (api/main.py's lifespan) drains its node executor before closing this
        # connection so that race can't reach here either.
        store = cls(sqlite3.connect(db_path, check_same_thread=False))
        store.conn.executescript(SCHEMA)
        return store

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def add_nodes(self, nodes: Iterable[Node]) -> int:
        rows = [
            (
                n.id,
                n.kind,
                n.name,
                n.qualified_name,
                n.path,
                n.start_line,
                n.end_line,
                n.lang,
                json.dumps(n.attrs, sort_keys=True),
            )
            for n in nodes
        ]
        self.conn.executemany(
            "INSERT INTO nodes(id, kind, name, qualified_name, path, start_line, end_line, lang, attrs) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            rows,
        )
        return len(rows)

    def add_edges(self, edges: Sequence[Edge]) -> tuple[int, list[Edge]]:
        """Insert edges, skipping any whose endpoints are not nodes.

        A dangling edge means a pass emitted an id nothing else declared; the
        count is surfaced in the summary rather than silently swallowed.
        """
        known = {row["id"] for row in self.conn.execute("SELECT id FROM nodes")}
        keep, dangling = [], []
        for edge in edges:
            if edge.src in known and edge.dst in known:
                keep.append(edge)
            else:
                dangling.append(edge)
        self.conn.executemany(
            "INSERT INTO edges(src, dst, kind, confidence, path, line, attrs) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(src, dst, kind, line) DO NOTHING",
            [
                (
                    e.src,
                    e.dst,
                    e.kind,
                    e.confidence,
                    e.path,
                    e.line,
                    json.dumps(e.attrs, sort_keys=True),
                )
                for e in keep
            ],
        )
        return len(keep), dangling

    def add_unresolved(self, rows: Iterable[Unresolved]) -> int:
        payload = [(r.kind, r.src, r.symbol, r.path, r.line, r.reason) for r in rows]
        self.conn.executemany(
            "INSERT INTO unresolved(kind, src, symbol, path, line, reason) VALUES(?,?,?,?,?,?)",
            payload,
        )
        return len(payload)

    # -- degrees and hubs ---------------------------------------------------

    def compute_degrees(self) -> None:
        """`in_degree`/`out_degree` count every edge; `semantic_in_degree` counts only
        the behavioural ones, so imports and containment cannot manufacture a hub."""
        placeholders = ",".join("?" * len(SEMANTIC_EDGE_KINDS))
        self.conn.execute(
            f"""
            UPDATE nodes SET
              in_degree          = (SELECT COUNT(*) FROM edges WHERE edges.dst = nodes.id),
              out_degree         = (SELECT COUNT(*) FROM edges WHERE edges.src = nodes.id),
              semantic_in_degree = (SELECT COUNT(*) FROM edges
                                    WHERE edges.dst = nodes.id AND edges.kind IN ({placeholders}))
            """,
            tuple(sorted(SEMANTIC_EDGE_KINDS)),
        )

    def flag_hubs(self, percentile: float = config.HUB_PERCENTILE) -> list[str]:
        """Flag the top ``percentile`` of nodes by semantic in-degree."""
        self.conn.execute("UPDATE nodes SET is_hub = 0")
        total = self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        if not total:
            return []
        limit = max(1, math.ceil(percentile * total))
        rows = self.conn.execute(
            "SELECT id FROM nodes WHERE semantic_in_degree > 0 "
            "ORDER BY semantic_in_degree DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        hub_ids = [row["id"] for row in rows]
        self.conn.executemany("UPDATE nodes SET is_hub = 1 WHERE id = ?", [(h,) for h in hub_ids])
        return hub_ids

    # -- reads --------------------------------------------------------------

    def node(self, node_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()

    def nodes_of_kind(self, kind: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()

    def edges_of_kind(self, kind: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM edges WHERE kind = ? ORDER BY src, dst", (kind,)
        ).fetchall()

    def edges_between(self, src: str, dst: str, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self.conn.execute(
                "SELECT * FROM edges WHERE src = ? AND dst = ?", (src, dst)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM edges WHERE src = ? AND dst = ? AND kind = ?", (src, dst, kind)
        ).fetchall()

    def out_edges(self, node_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM edges WHERE src = ?", (node_id,)).fetchall()

    def in_edges(self, node_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM edges WHERE dst = ?", (node_id,)).fetchall()

    # -- traversal ----------------------------------------------------------

    def traverse(
        self,
        node_id: str,
        direction: str = "out",
        max_depth: int = config.TRAVERSE_MAX_DEPTH,
        max_nodes: int = config.TRAVERSE_MAX_NODES,
        stop_at_hubs: bool = config.TRAVERSE_STOP_AT_HUBS,
        edge_kinds: Iterable[str] | None = None,
        include_members: bool = True,
    ) -> TraversalResult:
        """Breadth-first walk from ``node_id``, returning the shortest path to each node reached.

        ``edge_kinds`` defaults to :data:`~bridge.index.model.SEMANTIC_EDGE_KINDS` —
        behaviour, not declaration structure. Pass an explicit set to widen it.

        ``include_members`` expands a type node to the methods it declares at *zero
        depth cost*. This is needed because `contains` is structural and therefore
        excluded by default, which would otherwise sever a class from its own
        methods: a traversal starting at `java:...OwnerController` would find
        nothing, since every semantic edge hangs off its methods. For impact
        analysis a type and its members are one unit, so the expansion should not
        consume a semantic hop.

        ``stop_at_hubs`` includes a hub in the result but does not expand through it,
        so one over-connected node cannot flood the walk.
        """
        kinds = frozenset(edge_kinds) if edge_kinds is not None else SEMANTIC_EDGE_KINDS
        root = self.node(node_id)
        result = TraversalResult(root=node_id)
        if root is None:
            return result

        def hop_of(row: sqlite3.Row, via: str | None, confidence: str | None) -> Hop:
            return Hop(
                node_id=row["id"],
                kind=row["kind"],
                name=row["name"],
                path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                via_edge=via,
                confidence=confidence,
                is_hub=bool(row["is_hub"]),
            )

        start = TraversalPath(hops=[hop_of(root, None, None)])
        queue: deque[tuple[str, TraversalPath, int]] = deque([(node_id, start, 0)])
        seen = {node_id}
        result.paths.append(start)

        while queue:
            current_id, path, depth = queue.popleft()
            current = self.node(current_id)
            if current is None:
                continue

            if stop_at_hubs and current["is_hub"] and current_id != node_id:
                result.stopped_at_hubs.append(current_id)
                continue
            if depth >= max_depth:
                result.truncated_by_depth = True
                continue

            neighbours: list[tuple[str, str, str | None, int]] = []

            if include_members and current["kind"] in TYPE_KINDS:
                for row in self.conn.execute(
                    "SELECT dst FROM edges WHERE src = ? AND kind = ?",
                    (current_id, EdgeKind.CONTAINS),
                ):
                    neighbours.append((row["dst"], _MEMBER_EDGE, None, depth))

            clause = ",".join("?" * len(kinds))
            if direction in {"out", "both"}:
                for row in self.conn.execute(
                    f"SELECT dst, kind, confidence FROM edges WHERE src = ? AND kind IN ({clause})",
                    (current_id, *sorted(kinds)),
                ):
                    neighbours.append((row["dst"], row["kind"], row["confidence"], depth + 1))
            if direction in {"in", "both"}:
                for row in self.conn.execute(
                    f"SELECT src, kind, confidence FROM edges WHERE dst = ? AND kind IN ({clause})",
                    (current_id, *sorted(kinds)),
                ):
                    neighbours.append((row["src"], row["kind"], row["confidence"], depth + 1))

            for neighbour_id, via, confidence, next_depth in neighbours:
                if neighbour_id in seen:
                    continue
                if len(seen) >= max_nodes:
                    result.truncated_by_nodes = True
                    return result
                neighbour = self.node(neighbour_id)
                if neighbour is None:
                    continue
                seen.add(neighbour_id)
                extended = TraversalPath(hops=[*path.hops, hop_of(neighbour, via, confidence)])
                result.paths.append(extended)
                queue.append((neighbour_id, extended, next_depth))

        return result

    # -- summary ------------------------------------------------------------

    def count_by(self, table: str, column: str) -> dict[str, int]:
        rows = self.conn.execute(
            f"SELECT {column} AS key, COUNT(*) AS c FROM {table} GROUP BY {column} ORDER BY c DESC, key"
        ).fetchall()
        return {row["key"]: row["c"] for row in rows}

    def edge_confidence_breakdown(self) -> dict[str, dict[str, int]]:
        rows = self.conn.execute(
            "SELECT kind, confidence, COUNT(*) AS c FROM edges GROUP BY kind, confidence"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(row["kind"], {})[row["confidence"]] = row["c"]
        return out

    def endpoints(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE kind = 'endpoint' "
            "ORDER BY json_extract(attrs, '$.route'), json_extract(attrs, '$.http_method')"
        ).fetchall()

    def hubs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE is_hub = 1 ORDER BY semantic_in_degree DESC, id"
        ).fetchall()

    def in_degree_breakdown(self, node_id: str, semantic_only: bool = True) -> dict[str, int]:
        """Per-edge-kind in-degree for one node — what actually makes it a hub."""
        if semantic_only:
            clause = ",".join("?" * len(SEMANTIC_EDGE_KINDS))
            rows = self.conn.execute(
                f"SELECT kind, COUNT(*) AS c FROM edges WHERE dst = ? AND kind IN ({clause}) "
                "GROUP BY kind ORDER BY c DESC, kind",
                (node_id, *sorted(SEMANTIC_EDGE_KINDS)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT kind, COUNT(*) AS c FROM edges WHERE dst = ? GROUP BY kind ORDER BY c DESC, kind",
                (node_id,),
            ).fetchall()
        return {row["kind"]: row["c"] for row in rows}

    def top_nodes(self, limit: int = 5, by: str = "semantic_in_degree") -> list[sqlite3.Row]:
        if by not in {"in_degree", "out_degree", "semantic_in_degree"}:
            raise ValueError(f"cannot rank by {by!r}")
        return self.conn.execute(
            f"SELECT * FROM nodes ORDER BY {by} DESC, id ASC LIMIT ?", (limit,)
        ).fetchall()

    def unresolved_by_reason(self) -> dict[str, int]:
        return self.count_by("unresolved", "reason")

    def unresolved_count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM unresolved").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM unresolved WHERE kind = ?", (kind,)
            ).fetchone()
        return row["c"]
