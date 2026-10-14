"""The indexer that writes into Postgres — full on the first run, incremental after.

The two paths are the same code. A full index is just an incremental index whose
"what changed" answer is "everything", which is why there is one `index_repo()`
and not two: a bug that only shows up on the incremental path would be a bug that
only shows up in production, and the way to avoid that is to have no separate
path to be wrong in.

What a run actually does:

    files            = policy.apply(...)              # what earns indexing
    changed          = git diff prev..new  (or all)   # what to look at
    blobs            = sha256 of each changed file    # what to store
    chunks           = parse + chunk each new blob    # what to describe
    desc_keys        = which of those are NEW         # <-- the only spend
    vec_keys         = which descriptions are new     # <-- the only compute
    snapshot_files   = every path -> blob, always     # what the snapshot IS

Note the last line. `snapshot_files` is rewritten in full on every run even when
nothing changed, because it is cheap (one row per file, no content) and it is the
only structure that makes a rename free: a renamed file writes one new row here
and touches nothing else in the database.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..db import keys
from ..db.repo import IndexRepo
from ..redact import RedactionLog
from . import chunking, policy as policy_module, projects as projects_module
from . import screens as screens_module, spring as spring_module, tiers as tiers_module
from .parse_java import CallResolver, TypeIndex, parse_source


@dataclass(slots=True)
class IndexReport:
    """What one index run did. Every number here is asserted on by a verification."""

    repo: str = ""
    sha: str = ""
    snapshot_id: str = ""
    previous_sha: str | None = None
    mode: str = "full"  # full | incremental

    files_seen: int = 0
    files_indexed: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_renamed: int = 0

    blobs_new: int = 0
    chunks_new: int = 0
    #: descriptions that did NOT exist and had to be generated — the spend
    descriptions_scheduled: int = 0
    descriptions_written: int = 0
    descriptions_from_template: int = 0
    vectors_new: int = 0
    edges_new: int = 0
    #: edges whose endpoint was excluded by policy — dropped rather than dangling
    edges_dropped: int = 0

    excluded: dict[str, int] = field(default_factory=dict)
    tiers: dict[str, int] = field(default_factory=dict)
    projects: int = 0
    parse_failures: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def did_no_work(self) -> bool:
        """True when a commit cost nothing but a snapshot row — verification (d)."""
        return (
            self.blobs_new == 0
            and self.chunks_new == 0
            and self.descriptions_scheduled == 0
            and self.vectors_new == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "sha": self.sha, "snapshot_id": self.snapshot_id,
            "previous_sha": self.previous_sha, "mode": self.mode,
            "files": {
                "seen": self.files_seen, "indexed": self.files_indexed,
                "added": self.files_added, "modified": self.files_modified,
                "deleted": self.files_deleted, "renamed": self.files_renamed,
            },
            "new": {
                "blobs": self.blobs_new, "chunks": self.chunks_new,
                "descriptions_scheduled": self.descriptions_scheduled,
                "descriptions_written": self.descriptions_written,
                "descriptions_from_template": self.descriptions_from_template,
                "vectors": self.vectors_new, "edges": self.edges_new,
                "edges_dropped": self.edges_dropped,
            },
            "excluded": self.excluded,
            "tiers": self.tiers,
            "projects": self.projects,
            "parse_failures": self.parse_failures,
            "elapsed_s": round(self.elapsed_s, 3),
        }


# --------------------------------------------------------------------------- git


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git failed").strip().splitlines()[0])
    return result.stdout


def head_sha(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD").strip()


def list_files(repo_root: Path, sha: str | None = None) -> list[str]:
    """Every tracked file. `git ls-files` rather than a filesystem walk, so
    ignored and untracked junk never reaches the policy in the first place."""
    args = ["ls-tree", "-r", "--name-only", sha] if sha else ["ls-files"]
    return [line for line in _git(repo_root, *args).splitlines() if line]


@dataclass(slots=True)
class Diff:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    #: (old_path, new_path)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def touched(self) -> list[str]:
        """Paths whose CONTENT may have changed. A rename is deliberately absent:
        its bytes are identical, so it needs a snapshot row and nothing else."""
        return [*self.added, *self.modified]


def diff_names(repo_root: Path, previous: str, current: str) -> Diff:
    """`git diff --name-status`, with renames detected rather than shown as add+delete."""
    diff = Diff()
    output = _git(
        repo_root, "diff", "--name-status", "--find-renames", f"{previous}..{current}"
    )
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            diff.renamed.append((parts[1], parts[2]))
        elif status.startswith("A") and len(parts) >= 2:
            diff.added.append(parts[1])
        elif status.startswith("M") and len(parts) >= 2:
            diff.modified.append(parts[1])
        elif status.startswith("D") and len(parts) >= 2:
            diff.deleted.append(parts[1])
        elif len(parts) >= 2:
            # C (copy), T (type change) — treat as modified, which is the safe side
            diff.modified.append(parts[-1])
    return diff


# --------------------------------------------------------------------------- chunking


def _lang_of(path: str) -> str:
    return "java" if path.endswith(".java") else "thymeleaf" if path.endswith((".html", ".htm")) else "text"


def chunks_for_file(rel_path: str, source: str, blob: str) -> tuple[list[chunking.Chunk], dict]:
    """Parse one file into chunks. Returns (chunks, parsed_json).

    A parse failure produces a single whole-file chunk rather than nothing: a file
    Bridge cannot parse is still a file somebody may ask about, and an empty
    result would make it invisible with no trace.
    """
    if rel_path.endswith(".java"):
        try:
            parsed = parse_source(rel_path, source.encode("utf-8"))
        except Exception as exc:  # a broken file must not sink the run
            return (
                [
                    chunking.Chunk(
                        kind="file", name=Path(rel_path).name, qualified_name=rel_path,
                        code=source, start_line=1, end_line=source.count("\n") + 1,
                        attrs={"parse_failed": f"{exc.__class__.__name__}: {exc}"},
                    )
                ],
                {"parse_failed": True},
            )

        chunks: list[chunking.Chunk] = []
        for jtype in parsed.types:
            chunks.append(
                chunking.type_header_chunk(
                    source=source,
                    type_name=jtype.name,
                    qualified_name=jtype.fqn,
                    start_line=jtype.start_line,
                    end_line=jtype.end_line,
                    fields=[f"{f.type_base} {f.name}" for f in getattr(jtype, "fields", [])],
                    method_names=[m.signature for m in jtype.methods],
                    annotations=[a.name for a in jtype.annotations],
                    node_id=jtype.node_id,
                )
            )
            for method in jtype.methods:
                chunks.extend(
                    chunking.split_method(
                        source=source,
                        name=method.name,
                        qualified_name=f"{jtype.fqn}#{method.signature}",
                        start_line=method.start_line,
                        end_line=method.end_line,
                        node_id=method.node_id,
                        owner=jtype.name,
                        signature=method.signature,
                    )
                )
        return chunks, {"types": len(parsed.types), "package": parsed.package}

    # The logical name is the path relative to the template root, without the
    # extension — `owners/ownerDetails`, not `ownerDetails`. It has to match what
    # screens.py emits as a node id, or every `renders` and `includes` edge
    # points at a chunk that does not exist.
    logical = rel_path
    for root in config.TEMPLATE_ROOTS:
        prefix = root.rstrip("/") + "/"
        if logical.startswith(prefix):
            logical = logical[len(prefix) :]
            break
    for extension in config.TEMPLATE_EXTENSIONS:
        if logical.endswith(extension):
            logical = logical[: -len(extension)]
            break
    node_id = f"screen:{logical}"
    return (
        chunking.split_template(
            source=source, name=logical, qualified_name=node_id, node_id=node_id
        ),
        {"template": True, "logical": logical},
    )


def _symbols_in(chunk: chunking.Chunk) -> set[str]:
    """Bare identifiers a chunk mentions, for the `references` table.

    Deliberately crude — a superset is correct here. `references` is used to
    decide which chunks to *re-examine* when a symbol changes; over-including
    costs a little recomputation, under-including means a stale edge nobody
    notices.
    """
    import re

    return {
        token
        for token in re.findall(r"\b[A-Z][A-Za-z0-9_]{2,}\b", chunk.code)
    }


def structural_nodes_and_edges(repo_root: Path, java_paths: list[str]):
    """Run the existing Spring/screen/call extractors once over the whole set.

    Returns `(nodes, edges)` in the extractors' own vocabulary — node ids, not
    chunk hashes. The nodes matter as much as the edges: an `endpoint`, an
    `entity` and a `screen` are real things a product question is about, and if
    they get no chunk then every edge pointing at one is dropped and the impact
    map loses the whole Spring layer. (Measured: exactly 74 edges — every
    handles_route, maps_to, reads_entity, associates, renders and includes.)
    """
    parsed_files = []
    for rel_path in java_paths:
        try:
            parsed_files.append(parse_source(rel_path, (repo_root / rel_path).read_bytes()))
        except Exception:
            continue
    if not parsed_files:
        return [], []

    index = TypeIndex(parsed_files)
    nodes, edges = [], []

    # `contains` (type -> its methods) is emitted by build.py's row builder
    # rather than by any extractor, so it has to be rebuilt here. It is not
    # decoration: retrieval's member expansion walks it, and without it a
    # traversal that starts at a class returns exactly one node
    # (retrieve.py::graph_expand, and the phase-1 note that pinned it).
    from .model import Edge, EdgeKind

    for parsed in parsed_files:
        for jtype in parsed.types:
            for method in jtype.methods:
                edges.append(
                    Edge(
                        src=jtype.node_id, dst=method.node_id, kind=EdgeKind.CONTAINS,
                        path=jtype.path, line=method.start_line,
                    )
                )

    call_edges, _unresolved = CallResolver(index).run()
    edges.extend(call_edges)
    spring_result = spring_module.run(index)
    nodes.extend(spring_result.nodes)
    edges.extend(spring_result.edges)
    screens_result = screens_module.run(repo_root, index, spring_result.controller_fqns)
    nodes.extend(screens_result.nodes)
    edges.extend(screens_result.edges)
    return nodes, edges


def build_edges(
    repo_root: Path, java_paths: list[str], node_to_chunk: dict[str, str]
) -> tuple[list[dict], int]:
    """Every edge the existing extractors find, translated into chunk hashes.

    Call resolution is cross-file by nature — `owners.findById(...)` can only be
    resolved by knowing what `owners` is declared as, which lives in a different
    file. So this parses the WHOLE indexable set even on an incremental run. That
    is the honest cost of correct edges, and it is parse-only: no descriptions, no
    embeddings, no model calls.

    Edges whose endpoints have no chunk (a call into a type that exists but was
    excluded by policy, say) are dropped rather than pointed at nothing. The count
    of those is returned, because silently losing edges is exactly the kind of
    thing that makes an impact map quietly wrong.
    """
    _nodes, raw = structural_nodes_and_edges(repo_root, java_paths)
    return _translate_edges(raw, node_to_chunk)


def _translate_edges(raw, node_to_chunk: dict[str, str]) -> tuple[list[dict], int]:
    """Node ids -> chunk hashes. Edges with an unmapped endpoint are dropped and
    counted, never written pointing at nothing."""
    rows: list[dict] = []
    dropped = 0
    for edge in raw:
        src = node_to_chunk.get(edge.src)
        dst = node_to_chunk.get(edge.dst)
        if not src or not dst:
            dropped += 1
            continue
        rows.append(
            {
                "src": src,
                "dst": dst,
                "kind": edge.kind,
                "confidence": edge.confidence,
                "witness_path": edge.path,
                "witness_line": edge.line or 0,
                "attrs": edge.attrs or {},
            }
        )
    return rows, dropped


def semantic_degrees(edge_rows: list[dict]) -> dict[str, int]:
    """Semantic in-degree per chunk — one of tiering's five inputs.

    Computed here, in memory, BEFORE any chunk is written. Tier lives on the
    chunk row, and chunk rows are immutable, so the tier has to be right the
    first time; updating it afterwards would be exactly the mutation this
    storage model exists to avoid.
    """
    from .model import SEMANTIC_EDGE_KINDS

    degrees: dict[str, int] = {}
    for row in edge_rows:
        if row["kind"] in SEMANTIC_EDGE_KINDS:
            degrees[row["dst"]] = degrees.get(row["dst"], 0) + 1
    return degrees


# --------------------------------------------------------------------------- the run


def index_repo(
    repo_root: Path,
    repo_repo: IndexRepo,
    *,
    sha: str | None = None,
    previous_sha: str | None = None,
    policy: policy_module.Policy | None = None,
    embed: bool = True,
    describe_model: str = keys.SOURCE_TEMPLATE,
    progress=None,
) -> IndexReport:
    """Index one commit. Full when there is no previous snapshot, incremental after.

    ``describe_model`` names the model descriptions will be attributed to; it is
    part of `desc_key`, so passing the template source produces a run that spends
    nothing and still populates a complete, honest index.
    """
    started = time.perf_counter()
    repo_name = repo_root.name
    sha = sha or head_sha(repo_root)
    report = IndexReport(repo=repo_name, sha=sha, previous_sha=previous_sha)
    report.snapshot_id = keys.snapshot_id(repo_name, sha)
    policy = policy or policy_module.Policy.load(repo_name)
    from ..retrieval.vocab import default_vocabulary

    vocabulary = default_vocabulary()

    # --- which files exist, and which of them earn indexing
    all_files = list_files(repo_root, sha if previous_sha else None)
    report.files_seen = len(all_files)
    allowed = policy_module.apply(repo_root, all_files, policy)
    report.excluded = dict(allowed.excluded)
    report.files_indexed = len(allowed.included)
    indexable = set(allowed.included)

    # --- projects
    found_projects = projects_module.detect(repo_root, repo_name)
    project_index = projects_module.ProjectIndex(found_projects)
    repo_repo.add_projects([p.as_row() for p in found_projects])
    report.projects = len(found_projects)

    # --- what to look at
    if previous_sha:
        report.mode = "incremental"
        diff = diff_names(repo_root, previous_sha, sha)
        report.files_added = len(diff.added)
        report.files_modified = len(diff.modified)
        report.files_deleted = len(diff.deleted)
        report.files_renamed = len(diff.renamed)
        # A rename's bytes are unchanged, so it needs no parse — only the new
        # path row, which `snapshot_files` picks up below for free.
        to_parse = [p for p in diff.touched if p in indexable]
    else:
        report.mode = "full"
        report.files_added = len(indexable)
        to_parse = sorted(indexable)

    if progress:
        progress(f"{report.mode}: {len(to_parse)} file(s) to parse of {len(indexable)} indexable")

    # --- read + hash every file in the snapshot (cheap: no parse, no model)
    redaction = RedactionLog()
    snapshot_rows: list[dict] = []
    blob_of_path: dict[str, str] = {}
    source_of_blob: dict[str, str] = {}
    for rel_path in sorted(indexable):
        try:
            data = (repo_root / rel_path).read_bytes()
        except OSError:
            report.parse_failures.append(rel_path)
            continue
        blob = keys.blob_hash(data)
        blob_of_path[rel_path] = blob
        snapshot_rows.append(
            {
                "snapshot_id": report.snapshot_id,
                "path": rel_path,
                "blob_hash": blob,
                "project": project_index.of(rel_path),
            }
        )
        if rel_path in set(to_parse):
            source_of_blob[blob] = redaction.scrub(data.decode("utf-8", errors="replace"))

    # --- only blobs we have never seen need storing or chunking
    unseen = repo_repo.existing_blobs(list(source_of_blob))
    new_blobs = [b for b in source_of_blob if b not in unseen]

    blob_rows: list[dict] = []
    chunk_rows: list[dict] = []
    reference_rows: list[dict] = []
    chunk_objects: dict[str, chunking.Chunk] = {}
    #: the existing extractors speak node ids; the storage layer speaks chunk
    #: hashes. This is the only place the two vocabularies meet.
    node_to_chunk: dict[str, str] = {}
    path_of_blob = {blob: path for path, blob in blob_of_path.items()}

    for blob in new_blobs:
        rel_path = path_of_blob.get(blob, "")
        source = source_of_blob[blob]
        chunks, parsed_json = chunks_for_file(rel_path, source, blob)
        if parsed_json.get("parse_failed"):
            report.parse_failures.append(rel_path)
        blob_rows.append(
            {
                "blob_hash": blob,
                "lang": _lang_of(rel_path),
                "size_bytes": len(source.encode("utf-8")),
                "parsed_json": parsed_json,
            }
        )
        project = project_index.of(rel_path)
        for chunk in chunks:
            code_hash = keys.code_hash(chunk.kind, chunk.qualified_name, chunk.code)
            chunk_hash = keys.chunk_hash(
                blob, chunk.kind, chunk.qualified_name, chunk.start_line, chunk.end_line
            )
            chunk_objects[chunk_hash] = chunk
            if chunk.node_id:
                node_to_chunk[chunk.node_id] = chunk_hash
            chunk_rows.append(
                {
                    "chunk_hash": chunk_hash,
                    "blob_hash": blob,
                    "code_hash": code_hash,
                    "kind": chunk.kind,
                    "name": chunk.name,
                    "qualified_name": chunk.qualified_name,
                    # built from repo + qualified name, never the path, so a move
                    # does not change the text that gets embedded
                    "breadcrumb": f"{repo_name} › {chunk.qualified_name}",
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "code": chunk.code,
                    # placeholder — the real tier is decided below, once degrees
                    # exist, and BEFORE the row is written
                    "tier": config.TIER_FULL,
                    "project": project,
                    "attrs": chunk.attrs,
                }
            )
            reference_rows.extend(
                {"symbol_simple_name": symbol, "chunk_hash": chunk_hash}
                for symbol in _symbols_in(chunk)
            )

    # --- the Spring layer: endpoints and entities are nodes the extractors
    # synthesise rather than parse, so nothing above produced a chunk for them.
    # Without one, every edge into the Spring layer is dropped and the impact map
    # silently loses routes and tables — the layer product questions are ABOUT.
    java_paths = sorted(p for p in indexable if p.endswith(".java"))
    structural_nodes, raw_edges = structural_nodes_and_edges(repo_root, java_paths)
    for node in structural_nodes:
        blob = blob_of_path.get(node.path)
        if blob is None or node.id in node_to_chunk:
            continue
        code = "\n".join(
            f"{key}: {value}" for key, value in sorted((node.attrs or {}).items())
        ) or node.name
        chunk_hash = keys.chunk_hash(
            blob, node.kind, node.qualified_name or node.id,
            node.start_line or 0, node.end_line or 0,
        )
        node_to_chunk[node.id] = chunk_hash
        if blob not in new_blobs:
            # its file was unchanged, so the chunk is already stored
            continue
        chunk_rows.append(
            {
                "chunk_hash": chunk_hash,
                "blob_hash": blob,
                "code_hash": keys.code_hash(node.kind, node.qualified_name or node.id, code),
                "kind": node.kind,
                "name": node.name,
                "qualified_name": node.qualified_name or node.id,
                "breadcrumb": f"{repo_name} › {node.qualified_name or node.id}",
                "start_line": node.start_line or 0,
                "end_line": node.end_line or 0,
                "code": code,
                "tier": config.TIER_FULL,
                "project": project_index.of(node.path),
                "attrs": node.attrs or {},
            }
        )

    # --- edges, then degrees, then tiers, then the write. This order is forced:
    # `tier` lives on an immutable chunk row, so it has to be right before the
    # row exists, and one of its inputs is how many things point at the chunk.
    edge_rows, edges_dropped = _translate_edges(raw_edges, node_to_chunk)
    degrees = semantic_degrees(edge_rows)
    report.edges_dropped = edges_dropped

    vocab_names = tiers_module._vocab_names(vocabulary)
    for row in chunk_rows:
        attrs = row["attrs"] or {}
        lines = max(0, row["end_line"] - row["start_line"] + 1)
        tier, reason = tiers_module.tier_for(
            kind=row["kind"],
            name=row["name"],
            attrs=attrs,
            line_count=lines,
            semantic_in_degree=degrees.get(row["chunk_hash"], 0),
            io_role=attrs.get("io_role") or config.IO_NONE,
            io_source=attrs.get("io_source") or "",
            vocab_hit=row["name"] in vocab_names,
        )
        row["tier"] = tier
        row["attrs"] = {**attrs, "tier_reason": reason}

    report.blobs_new = repo_repo.add_blobs(blob_rows)
    report.chunks_new = repo_repo.add_chunks(chunk_rows)
    repo_repo.add_references(reference_rows)
    report.edges_new = repo_repo.add_edges(edge_rows)

    # --- the snapshot itself: always rewritten, always cheap
    repo_repo.create_snapshot(report.snapshot_id, repo_name, sha, {})
    repo_repo.set_snapshot_files(report.snapshot_id, snapshot_rows)

    # --- descriptions: the ONLY place money is spent, so the count is the report
    wanted: dict[str, dict] = {}
    for row in chunk_rows:
        key = keys.desc_key(row["code_hash"], describe_model)
        wanted.setdefault(key, row)
    already = repo_repo.existing_descriptions(list(wanted))
    missing = {k: v for k, v in wanted.items() if k not in already}
    report.descriptions_scheduled = len(missing)

    description_rows: list[dict] = []
    for key, row in missing.items():
        chunk = chunk_objects.get(row["chunk_hash"])
        text = _describe(chunk, row, describe_model)
        description_rows.append(
            {
                "desc_key": key,
                "chunk_hash": row["chunk_hash"],
                "code_hash": row["code_hash"],
                "model": describe_model,
                "prompt_version": keys.PROMPT_VERSION,
                "text": text,
                "source": describe_model,
            }
        )
    report.descriptions_written = repo_repo.add_descriptions(description_rows)
    report.descriptions_from_template = sum(
        1 for row in description_rows if row["source"] == keys.SOURCE_TEMPLATE
    )

    # --- embeddings: local, so real even with no API budget
    if embed and description_rows:
        report.vectors_new = _embed(repo_repo, description_rows, chunk_rows)

    report.tiers = repo_repo.tier_distribution(report.snapshot_id)
    report.elapsed_s = time.perf_counter() - started
    repo_repo.create_snapshot(report.snapshot_id, repo_name, sha, report.as_dict())
    return report


def _describe(chunk, row: dict, model: str) -> str:
    """A description for one chunk. Template tier needs no model and never will."""
    from .tiers import template_description

    if model == keys.SOURCE_TEMPLATE:
        owner = (row.get("qualified_name") or "").rsplit(".", 1)[-1].split("#", 1)[0]
        return template_description(row["kind"], row["name"], (chunk.attrs if chunk else {}), owner)
    return row.get("name") or ""


def _embed(repo_repo: IndexRepo, description_rows: list[dict], chunk_rows: list[dict]) -> int:
    """Embed the descriptions that are new. Local model, so no API budget involved."""
    from .embed import load_encoder

    by_code = {row["code_hash"]: row for row in chunk_rows}
    pending = [
        row for row in description_rows
        if keys.vec_key(row["desc_key"], config.EMBED_MODEL)
        not in repo_repo.existing_vectors(
            [keys.vec_key(r["desc_key"], config.EMBED_MODEL) for r in description_rows]
        )
    ]
    if not pending:
        return 0

    encoder = load_encoder(config.EMBED_MODEL)
    texts = []
    for row in pending:
        chunk = by_code.get(row["code_hash"], {})
        texts.append(f"{chunk.get('breadcrumb', '')}\n{row['text']}".strip())
    embeddings = encoder.encode(
        texts, batch_size=config.EMBED_BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
    )
    return repo_repo.add_vectors(
        [
            {
                "vec_key": keys.vec_key(row["desc_key"], config.EMBED_MODEL),
                "desc_key": row["desc_key"],
                "embed_model": config.EMBED_MODEL,
                "embedding": vector.tolist(),
                "project": by_code.get(row["code_hash"], {}).get("project", ""),
            }
            for row, vector in zip(pending, embeddings)
        ]
    )


# --------------------------------------------------------------------------- verify


@dataclass(slots=True)
class DriftReport:
    checked: int = 0
    #: path -> (stored hash, actual hash)
    drifted: dict[str, tuple[str, str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.drifted and not self.missing and not self.orphans


def verify_snapshot(repo_root: Path, repo_repo: IndexRepo, snapshot_id: str) -> DriftReport:
    """Re-hash every blob in a snapshot and compare against what was stored.

    This is the check that makes "immutable and content-addressed" a claim rather
    than an aspiration. If a stored blob hash no longer matches the bytes at that
    path, either the working tree moved under us or something wrote a row it
    should not have — and both are worth knowing before an answer cites it.
    """
    report = DriftReport()
    report.orphans = repo_repo.orphan_blobs(snapshot_id)
    for row in repo_repo.blobs_for_verify(snapshot_id):
        report.checked += 1
        full = repo_root / row["path"]
        try:
            actual = keys.blob_hash(full.read_bytes())
        except OSError:
            report.missing.append(row["path"])
            continue
        if actual != row["blob_hash"]:
            report.drifted[row["path"]] = (row["blob_hash"], actual)
    return report
