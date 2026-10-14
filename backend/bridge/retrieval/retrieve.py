"""`retrieve(question, budget)` — the one function every agent calls to get evidence.

Five stages, deterministic in sequence (design law 1 — Python decides the order,
the LLM only reasons inside one node):

1. **expand** — product words to code words via the configured vocabulary
2. **semantic** — k=20 nearest chunks to the expanded query
3. **keyword** — exact matches on node names and paths for identifiers in the
   question, so `OwnerController` is never missed because an embedding shrugged
4. **graph_expand** — walk the structural graph from those seeds (depth ≤ 3,
   stop at hubs, cap 150), which is what turns a hit into an *impact map*
5. **rerank** — one call on the `rerank` role's model, scoring every candidate 0-10

Every returned chunk carries a citation `repo@sha:path:start-end` (design law 4),
its per-stage scores, the graph path that reached it, and the churn of its file.
When the evidence is weak the result says so — `confidence: low` — rather than
handing an agent a confident-looking pile of noise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..agents.llm import Budget, LLMClient, load_prompt, wrap_retrieved
from ..index import ids
from ..index.embed import SearchHit, VectorIndex
from ..index.graph import GraphStore
from ..redact import RedactionLog
from .vocab import ExpandedQuery, Vocabulary, default_vocabulary

#: how each stage's presence contributes when no reranker is available
_STAGE_WEIGHTS = {"vocab": 1.0, "keyword": 0.8, "semantic": 0.7, "graph": 0.4}


@dataclass(slots=True)
class Chunk:
    """One piece of evidence, with everything needed to cite and trust it."""

    node_id: str
    kind: str
    name: str
    path: str
    start_line: int
    end_line: int
    description: str
    citation: str
    #: which stages surfaced this chunk
    sources: list[str] = field(default_factory=list)
    semantic_score: float | None = None
    keyword_score: float | None = None
    graph_depth: int | None = None
    rerank_score: float | None = None
    score: float = 0.0
    #: how this chunk was reached from a seed, e.g. "OwnerController -[contains]-> ..."
    graph_path: str | None = None
    #: churn for the file this chunk lives in
    history: dict[str, Any] | None = None
    code: str = ""
    breadcrumb: str = ""

    def as_dict(self, *, with_code: bool = True) -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "citation": self.citation,
            "description": self.description,
            "score": round(self.score, 3),
            "rerank_score": self.rerank_score,
            "semantic_score": round(self.semantic_score, 3) if self.semantic_score else None,
            "keyword_score": self.keyword_score,
            "graph_depth": self.graph_depth,
            "sources": self.sources,
            "graph_path": self.graph_path,
            "history": self.history,
        }
        # `code` is the largest field by far and is already redacted at embed time
        # (embed.py scrubs it before it ever reaches LanceDB) — safe to include,
        # but callers building compact lists (e.g. the rerank prompt) skip it.
        if with_code:
            payload["code"] = self.code
        return payload


@dataclass(slots=True)
class RetrievalResult:
    question: str
    expanded_query: str
    chunks: list[Chunk] = field(default_factory=list)
    confidence: str = "low"  # high | medium | low
    confidence_reason: str = ""
    matched_terms: list[str] = field(default_factory=list)
    matched_node_ids: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    #: candidates considered per stage, before the top-k cut
    stage_counts: dict[str, int] = field(default_factory=dict)
    reranked: bool = False
    rerank_unavailable_reason: str | None = None
    cost_usd: float = 0.0
    elapsed: float = 0.0
    redaction_counts: dict[str, int] = field(default_factory=dict)
    repo: str = ""
    sha: str = ""
    #: keyword seeds a caller supplied on top of what the question itself named
    extra_terms: list[str] = field(default_factory=list)

    @property
    def citations(self) -> list[str]:
        return [chunk.citation for chunk in self.chunks]

    @property
    def abstained(self) -> bool:
        """True when the evidence is too weak to answer from (design law 4)."""
        return self.confidence == "low"

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "matched_terms": self.matched_terms,
            "stage_counts": self.stage_counts,
            "reranked": self.reranked,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed": round(self.elapsed, 3),
            "chunks": [chunk.as_dict() for chunk in self.chunks],
        }


# --------------------------------------------------------------------------- retriever


class Retriever:
    """Holds the open index; :func:`retrieve` is the one-shot convenience form."""

    def __init__(
        self,
        store: GraphStore,
        vectors: VectorIndex | None,
        descriptions: dict[str, str],
        repo: str,
        sha: str,
        vocabulary: Vocabulary | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.descriptions = descriptions
        self.repo = repo
        self.sha = sha
        self.vocabulary = vocabulary or default_vocabulary()
        #: where the indexed repository lives, if it is still there. Read-only,
        #: and optional: an index is self-contained, so a moved or deleted repo
        #: degrades what agents can read (retrieval/code.py) rather than failing.
        self.repo_root = repo_root

    @classmethod
    def open(cls, index_dir: str | Path, vocabulary: Vocabulary | None = None) -> "Retriever":
        from ..index.describe import DescriptionStore

        index_dir = Path(index_dir)
        store = GraphStore.open(index_dir / config.GRAPH_DB_NAME)
        vectors_dir = index_dir / config.VECTORS_DIR_NAME
        vectors = VectorIndex.open(vectors_dir) if VectorIndex.exists(vectors_dir) else None

        # The vector table carries a description for every embedded node —
        # model-written or fallback — so it is the complete set. The cache is
        # model-only by design, and is layered on top as the authority for the
        # rows it does have.
        descriptions: dict[str, str] = {}
        if vectors is not None:
            descriptions = {
                row["node_id"]: row["description"]
                for row in vectors.table.to_arrow().to_pylist()
                if row.get("description")
            }
        descriptions_path = index_dir / config.DESCRIPTIONS_DB_NAME
        if descriptions_path.is_file():
            with DescriptionStore.open(descriptions_path) as store_d:
                descriptions.update(store_d.all_descriptions())
        return cls(
            store=store,
            vectors=vectors,
            descriptions=descriptions,
            repo=store.get_meta("repo_name") or index_dir.name,
            sha=store.get_meta("head_sha") or index_dir.name,
            vocabulary=vocabulary,
            repo_root=repo_root_of(index_dir),
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Retriever":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- stage 3: keyword ---------------------------------------------------

    def keyword_search(self, terms: Sequence[str], limit: int = 30) -> dict[str, float]:
        """Exact-ish matches on node name, qualified name, and path.

        The embedding index can quietly miss a literal identifier; this stage
        cannot. Scores are 1.0 for an exact name match, 0.6 for a substring.
        """
        scores: dict[str, float] = {}
        for term in terms:
            if len(term) < 3:
                continue
            simple = term.rsplit(".", 1)[-1].rstrip("()")
            like = f"%{simple}%"
            rows = self.store.conn.execute(
                "SELECT id, name, qualified_name, path FROM nodes"
                " WHERE name = ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE"
                "    OR qualified_name LIKE ? COLLATE NOCASE OR path LIKE ? COLLATE NOCASE"
                " LIMIT ?",
                (simple, like, like, like, limit),
            ).fetchall()
            for row in rows:
                exact = (row["name"] or "").lower() == simple.lower()
                score = 1.0 if exact else 0.6
                scores[row["id"]] = max(scores.get(row["id"], 0.0), score)
        return scores

    # -- stage 4: graph -----------------------------------------------------

    def graph_expand(
        self,
        seeds: Iterable[str],
        max_depth: int = config.RETRIEVE_GRAPH_DEPTH,
        cap: int = config.RETRIEVE_GRAPH_CAP,
    ) -> dict[str, tuple[int, str]]:
        """Walk out from ``seeds``; returns node id -> (depth, readable path)."""
        reached: dict[str, tuple[int, str]] = {}
        for seed in seeds:
            if len(reached) >= cap:
                break
            if self.store.node(seed) is None:
                continue
            walk = self.store.traverse(
                seed,
                direction="both",
                max_depth=max_depth,
                max_nodes=max(1, cap - len(reached)),
                stop_at_hubs=True,
            )
            for path in walk.paths:
                node_id = path.terminal
                depth = path.depth
                if node_id in reached and reached[node_id][0] <= depth:
                    continue
                reached[node_id] = (depth, _render_path(path))
                if len(reached) >= cap:
                    break
        return reached

    # -- stage 5: rerank ----------------------------------------------------

    def rerank(
        self, question: str, candidates: list[Chunk], llm: LLMClient, redaction: RedactionLog
    ) -> tuple[bool, str | None, float]:
        """Score every candidate 0-10 in a single call. Returns (ran, reason, cost)."""
        if not candidates:
            return False, "no candidates", 0.0
        if not llm.available_for("rerank"):
            return False, llm.reason_for("rerank"), 0.0

        lines = []
        for index, chunk in enumerate(candidates, start=1):
            body = redaction.scrub(
                f"{index}. [{chunk.kind}] {chunk.breadcrumb or chunk.name}\n"
                f"   {chunk.description}\n"
                f"   {chunk.path}:{chunk.start_line}-{chunk.end_line}"
            )
            lines.append(body)

        prompt = (
            f"Question: {question}\n\n"
            f"Candidates:\n{wrap_retrieved(chr(10).join(lines))}\n\n"
            f"Score all {len(candidates)} candidates."
        )
        try:
            response = llm.complete("rerank", prompt, system=load_prompt("rerank"))
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {exc}", 0.0
        if response.refused or not response.text:
            return False, "model declined to score", response.cost_usd

        for index, score in _parse_scores(response.text).items():
            if 1 <= index <= len(candidates):
                candidates[index - 1].rerank_score = score
        return True, None, response.cost_usd

    # -- the pipeline -------------------------------------------------------

    def retrieve(
        self,
        question: str,
        budget: Budget | None = None,
        *,
        llm: LLMClient | None = None,
        k: int = config.RETRIEVE_TOP_K,
        semantic_k: int = config.RETRIEVE_SEMANTIC_K,
        include_tests: bool = False,
        extra_terms: Sequence[str] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        redaction = RedactionLog()
        llm = llm if llm is not None else LLMClient(budget=budget or Budget())

        expansion: ExpandedQuery = self.vocabulary.expand_query(question)
        result = RetrievalResult(
            question=question,
            expanded_query=expansion.expanded,
            matched_terms=expansion.matched_terms,
            matched_node_ids=expansion.node_ids,
            identifiers=expansion.identifiers,
            repo=self.repo,
            sha=self.sha,
        )

        candidates: dict[str, Chunk] = {}

        def ensure(node_id: str) -> Chunk | None:
            if node_id in candidates:
                return candidates[node_id]
            row = self.store.node(node_id)
            if row is None:
                return None
            chunk = Chunk(
                node_id=node_id,
                kind=row["kind"],
                name=row["name"],
                path=row["path"],
                start_line=row["start_line"] or 0,
                end_line=row["end_line"] or 0,
                description=self.descriptions.get(node_id, ""),
                citation=ids.citation(
                    self.repo, self.sha, row["path"], row["start_line"], row["end_line"]
                ),
            )
            candidates[node_id] = chunk
            return chunk

        # 1 — vocabulary seeds (exact, so they enter as candidates outright)
        for node_id in expansion.node_ids:
            chunk = ensure(node_id)
            if chunk is not None and "vocab" not in chunk.sources:
                chunk.sources.append("vocab")
        result.stage_counts["vocab"] = len(expansion.node_ids)

        # 2 — semantic
        semantic_hits: list[SearchHit] = []
        if self.vectors is not None:
            semantic_hits = self.vectors.semantic_search(
                expansion.expanded, k=semantic_k, include_tests=include_tests
            )
            for hit in semantic_hits:
                chunk = ensure(hit.node_id)
                if chunk is None:
                    continue
                chunk.semantic_score = hit.score
                chunk.breadcrumb = hit.breadcrumb
                chunk.code = hit.code
                if not chunk.description:
                    chunk.description = hit.description
                if "semantic" not in chunk.sources:
                    chunk.sources.append("semantic")
        result.stage_counts["semantic"] = len(semantic_hits)

        # 3 — keyword over names and paths
        keyword_terms = list(expansion.identifiers)
        keyword_terms.extend(alias for alias in expansion.matched_aliases if " " not in alias)
        # A caller that already knows what it is looking for can say so directly.
        # `identifiers_in` only recognises code-*shaped* tokens (CamelCase, dotted
        # paths), so a follow-up steering toward "save … repository … Visit"
        # would reach the embedding and never the keyword stage — where the
        # exact-name match it actually needs lives (orchestrator/followup.py).
        keyword_terms.extend(extra_terms or ())
        keyword_scores = self.keyword_search(keyword_terms)
        for node_id, score in keyword_scores.items():
            chunk = ensure(node_id)
            if chunk is None:
                continue
            chunk.keyword_score = score
            if "keyword" not in chunk.sources:
                chunk.sources.append("keyword")
        result.stage_counts["keyword"] = len(keyword_scores)
        result.extra_terms = list(extra_terms or ())

        # 4 — graph expansion from everything found so far
        seeds = list(candidates.keys())
        reached = self.graph_expand(seeds)
        for node_id, (depth, path_text) in reached.items():
            chunk = ensure(node_id)
            if chunk is None:
                continue
            if chunk.graph_depth is None or depth < chunk.graph_depth:
                chunk.graph_depth = depth
                chunk.graph_path = path_text
            if "graph" not in chunk.sources:
                chunk.sources.append("graph")
        result.stage_counts["graph"] = len(reached)
        result.stage_counts["candidates"] = len(candidates)

        # fill descriptions and drop test chunks unless asked for
        pool = []
        for chunk in candidates.values():
            if not include_tests and _is_test(self.store, chunk.node_id):
                continue
            if not chunk.description:
                chunk.description = self.descriptions.get(chunk.node_id, "")
            pool.append(chunk)

        # pre-rank so the reranker sees a sensible, bounded shortlist. Tie-break
        # on node_id: two chunks can score identically (most often before a
        # reranker has run at all, when the score is pure `_prior_score`
        # arithmetic), and Python's sort is stable but the *input* order here is
        # dict-iteration order over what several stages inserted into — not a
        # property anyone should have to reason about to know a run is
        # deterministic. An explicit key removes the question entirely.
        for chunk in pool:
            chunk.score = _prior_score(chunk)
        pool.sort(key=lambda c: (-c.score, c.node_id))
        shortlist = pool[: max(k * 3, 30)]

        # Vocabulary seeds are a human-curated, high-precision signal — stage 1's
        # comment above already claims they "enter as candidates outright", but a
        # bare entity node (no description, no semantic/keyword hit of its own)
        # can still rank below the cutoff on _prior_score alone and never reach
        # the reranker at all. Found via species-list in eval/smoke_questions.yaml:
        # entity:...PetType was seeded by the "pet type" vocab term but ranked
        # #51/107 on prior score, entirely below a 45-candidate cutoff, so the
        # reranker never got a chance to judge it. A curated match should always
        # get that judgment, so it's added back regardless of prior score.
        shortlisted_ids = {c.node_id for c in shortlist}
        vocab_dropped = [
            c for c in pool if c.node_id not in shortlisted_ids and "vocab" in c.sources
        ]
        shortlist = shortlist + vocab_dropped

        # 5 — rerank
        ran, reason, cost = self.rerank(question, shortlist, llm, redaction)
        result.reranked = ran
        result.rerank_unavailable_reason = reason
        result.cost_usd += cost

        if ran:
            for chunk in shortlist:
                if chunk.rerank_score is None:
                    chunk.rerank_score = 0.0
                chunk.score = chunk.rerank_score + 0.1 * _prior_score(chunk)
        shortlist.sort(key=lambda c: (-c.score, c.node_id))
        result.chunks = shortlist[:k]

        # history per chunk
        for chunk in result.chunks:
            chunk.history = _history_for(self.store, chunk.node_id)

        result.confidence, result.confidence_reason = _confidence(result.chunks, ran, reason)
        result.redaction_counts = redaction.counts
        result.elapsed = time.perf_counter() - started
        return result


# --------------------------------------------------------------------------- helpers


def _render_path(path) -> str:
    parts = []
    for hop in path.hops:
        label = hop.name or hop.node_id
        parts.append(label if hop.via_edge is None else f"-[{hop.via_edge}]-> {label}")
    return " ".join(parts)


def _is_test(store: GraphStore, node_id: str) -> bool:
    import json

    row = store.node(node_id)
    return bool(row and json.loads(row["attrs"]).get("is_test"))


def _history_for(store: GraphStore, node_id: str) -> dict[str, Any] | None:
    import json

    row = store.node(node_id)
    if row is None:
        return None
    return json.loads(row["attrs"]).get("history")


def _prior_score(chunk: Chunk) -> float:
    """Pre-rerank ordering: how many stages found it, and how strongly."""
    score = 0.0
    for source in chunk.sources:
        score += _STAGE_WEIGHTS.get(source, 0.0)
    if chunk.semantic_score is not None:
        score += chunk.semantic_score
    if chunk.keyword_score is not None:
        score += chunk.keyword_score * 0.5
    if chunk.graph_depth is not None:
        score += max(0.0, 0.4 - 0.1 * chunk.graph_depth)
    if chunk.description:
        score += 0.2
    return score


_SCORE_LINE = re.compile(r"^\s*(\d+)\s*[:.)-]\s*(\d+(?:\.\d+)?)\s*$")


def _parse_scores(text: str) -> dict[int, float]:
    scores: dict[int, float] = {}
    for line in text.splitlines():
        match = _SCORE_LINE.match(line)
        if match:
            scores[int(match.group(1))] = max(0.0, min(10.0, float(match.group(2))))
    return scores


def _confidence(chunks: list[Chunk], reranked: bool, reason: str | None) -> tuple[str, str]:
    """`low` when the best evidence is weak — the caller must abstain (design law 4)."""
    if not chunks:
        return "low", "no candidates matched the question"
    if not reranked:
        return (
            "low",
            f"not reranked ({reason or 'reranker unavailable'}); "
            "scores are lexical and graph priors only",
        )

    scored = [chunk.rerank_score or 0.0 for chunk in chunks]
    best = max(scored)
    top3 = scored[:3]
    top3_mean = sum(top3) / len(top3)

    if best < config.RETRIEVE_LOW_CONFIDENCE_BEST:
        return "low", f"best relevance {best:.1f}/10 is below {config.RETRIEVE_LOW_CONFIDENCE_BEST}"
    if top3_mean < config.RETRIEVE_LOW_CONFIDENCE_TOP3_MEAN:
        return (
            "low",
            f"top-3 mean {top3_mean:.1f}/10 is below {config.RETRIEVE_LOW_CONFIDENCE_TOP3_MEAN}",
        )
    if (
        best >= config.RETRIEVE_HIGH_CONFIDENCE_BEST
        and top3_mean >= config.RETRIEVE_HIGH_CONFIDENCE_TOP3_MEAN
    ):
        return "high", f"best {best:.1f}/10, top-3 mean {top3_mean:.1f}/10"
    return "medium", f"best {best:.1f}/10, top-3 mean {top3_mean:.1f}/10"


def retrieve(
    question: str,
    budget: Budget | None = None,
    *,
    index_dir: str | Path | None = None,
    sha: str | None = None,
    **kwargs,
) -> RetrievalResult:
    """Retrieve evidence for ``question`` from an index directory.

    ``index_dir`` defaults to ``data/index/<sha>``; with neither argument the most
    recently written index is used.
    """
    if index_dir is None:
        index_dir = _resolve_index_dir(sha)
    with Retriever.open(index_dir) as retriever:
        return retriever.retrieve(question, budget, **kwargs)


def repo_root_of(index_dir: str | Path) -> Path | None:
    """The indexed repository's working tree, from its manifest — or None.

    The manifest records `repo_path` at index time. It can be stale (the repo
    moved, or this index was copied to another machine), so the path is checked
    before it is handed out: callers treat None as "the code is only whatever the
    index already stored", never as an error.
    """
    manifest_path = Path(index_dir) / config.MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        import json

        repo_path = json.loads(manifest_path.read_text(encoding="utf-8")).get("repo_path")
    except (OSError, ValueError):
        return None
    if not repo_path:
        return None
    root = Path(repo_path)
    return root if root.is_dir() else None


def _resolve_index_dir(sha: str | None = None) -> Path:
    if sha:
        return config.INDEX_ROOT / sha
    candidates = [
        path
        for path in config.INDEX_ROOT.glob("*")
        if (path / config.GRAPH_DB_NAME).is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no index found under {config.INDEX_ROOT} — run `bridge index <repo>` first"
        )
    return max(candidates, key=lambda path: (path / config.GRAPH_DB_NAME).stat().st_mtime)
