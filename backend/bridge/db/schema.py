"""The persistent index, as SQLAlchemy Core tables.

Everything above `snapshots` is **immutable and content-addressed**: a row is
identified by a hash of what is in it, so two snapshots of the same repo — or two
different repos that happen to contain the same file — share the row rather than
each paying for their own copy. Snapshots are then cheap: a snapshot is a *list
of (path, blob)* and nothing more.

The hash chain is the whole design, and it is worth reading before the tables:

    blob_hash  = sha256(file bytes)
    code_hash  = sha256(kind | qualified_name | normalised source)   <-- the reuse key
    chunk_hash = sha256(blob_hash | kind | qualified_name | start | end)
    desc_key   = sha256(code_hash | model | prompt_version)
    vec_key    = sha256(desc_key | embed_model)

`code_hash` carries **no line numbers and no blob**, and that is what makes an
incremental index possible. Move a method forty lines down without editing it:
the file's bytes change, so `blob_hash` changes, so `chunk_hash` changes and a new
chunk row appears — but `code_hash` is untouched, so `desc_key` resolves to the
description that already exists, and `vec_key` to the embedding that already
exists. Zero model calls, zero embeddings, for a commit that only moved things
around. The same property makes a pure rename free.

`chunk_hash` *does* include the blob and the line numbers, deliberately. It means
a chunk row is true forever: its `start_line`/`end_line` are correct for its blob,
not "correct as of whenever we first saw this code". That is what lets
`bridge verify` re-hash a blob and assert the index has not drifted.

Note what is NOT on `chunks`: a path. The same blob can live at several paths and
move between them, so path is a property of *(snapshot, blob)* and lives in
`snapshot_files`. Retrieval joins through it.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from .. import config

metadata = MetaData()

#: JSONB on Postgres, plain JSON elsewhere — the tables are declared once and the
#: dialect decides. Keeps the SQLite fallback backend readable from the same defs.
_JSON = JSON().with_variant(JSONB(), "postgresql")

# --------------------------------------------------------------------------- content


blobs = Table(
    "blobs",
    metadata,
    # sha256 of the file's raw bytes. Not of its text: an encoding change is a
    # content change, and pretending otherwise would silently reuse stale chunks.
    Column("blob_hash", String(64), primary_key=True),
    Column("lang", String(32), nullable=False),
    Column("size_bytes", Integer, nullable=False, server_default="0"),
    # the parser's own output, kept so a re-chunk (new chunking rules, say) does
    # not have to re-read a repository that may no longer be on this disk
    Column("parsed_json", _JSON, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


chunks = Table(
    "chunks",
    metadata,
    Column("chunk_hash", String(64), primary_key=True),
    Column("blob_hash", String(64), ForeignKey("blobs.blob_hash"), nullable=False),
    # The reuse key. Deliberately not unique: many chunk rows (one per blob the
    # code has ever appeared in) share one code_hash, which is exactly the point.
    Column("code_hash", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("name", String(512), nullable=False),
    Column("qualified_name", String(1024)),
    # built from repo + qualified name, NOT from path — a moved file must not
    # invalidate the text that gets embedded
    Column("breadcrumb", Text, nullable=False, server_default=""),
    Column("start_line", Integer, nullable=False, server_default="0"),
    Column("end_line", Integer, nullable=False, server_default="0"),
    Column("code", Text, nullable=False, server_default=""),
    # {read, write, both, none} — index/io_roles.py
    Column("io_role", String(8), nullable=False, server_default=config.IO_NONE),
    Column("io_source", String(32), nullable=False, server_default=""),
    # T0 template | T1 file-summary | T2 full — index/tiers.py
    Column("tier", String(2), nullable=False, server_default="T2"),
    Column("project", String(256), nullable=False, server_default=""),
    Column("attrs", _JSON, nullable=False, server_default="{}"),
    Index("idx_chunks_blob", "blob_hash"),
    Index("idx_chunks_code", "code_hash"),
    Index("idx_chunks_name", "name"),
    Index("idx_chunks_qualified", "qualified_name"),
    Index("idx_chunks_kind", "kind"),
    Index("idx_chunks_tier", "tier"),
)


descriptions = Table(
    "descriptions",
    metadata,
    Column("desc_key", String(64), primary_key=True),
    # Provenance, not identity: the chunk that first caused this description to
    # exist. Every other chunk with the same code_hash reads the same row.
    Column("chunk_hash", String(64), nullable=False),
    Column("code_hash", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    Column("text", Text, nullable=False),
    # model id | 'template' (T0, generated from the signature) | 'inherited'
    # (T1, the file's summary) | 'fallback' (the model was unavailable)
    Column("source", String(32), nullable=False),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_desc_code", "code_hash"),
    Index("idx_desc_source", "source"),
)


#: Partitioned by project (Phase G). Postgres requires the partition key to be
#: part of every unique constraint, so the primary key is `(vec_key, project)`
#: rather than `vec_key` alone.
#:
#: The trade-off is worth stating: `vec_key` is content-addressed and therefore
#: project-independent, so code that genuinely appears in two projects now stores
#: two rows instead of one. That is rare (identical code, identical description,
#: two build roots) and it buys the thing Phase H needs — partition **pruning**,
#: so a routed query scans the selected projects' indexes instead of scanning a
#: global index and filtering afterwards. Post-filtering an ANN index is not a
#: cheaper version of the same query; it is a different, worse one, because the
#: index returns its k nearest and the filter then throws most of them away.
vectors = Table(
    "vectors",
    metadata,
    Column("vec_key", String(64), primary_key=True),
    Column("desc_key", String(64), ForeignKey("descriptions.desc_key"), nullable=False),
    Column("embed_model", String(128), nullable=False),
    Column("embedding", Vector(config.EMBED_DIMENSIONS), nullable=False),
    Column("project", String(256), primary_key=True, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_vectors_desc", "desc_key"),
    postgresql_partition_by="LIST (project)",
)


edges = Table(
    "edges",
    metadata,
    # Endpoints are chunk hashes, so an edge is content-addressed too: if both
    # endpoints survive into the next snapshot unchanged, so does the edge.
    Column("src", String(64), nullable=False),
    Column("dst", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("confidence", String(8), nullable=False, server_default="high"),
    # where the claim can be checked — design law 4, at the storage layer
    Column("witness_path", Text),
    Column("witness_line", Integer, nullable=False, server_default="0"),
    Column("attrs", _JSON, nullable=False, server_default="{}"),
    UniqueConstraint("src", "dst", "kind", "witness_line", name="pk_edges"),
    Index("idx_edges_src", "src", "kind"),
    Index("idx_edges_dst", "dst", "kind"),
)


references = Table(
    "references",
    metadata,
    # "this chunk mentions this bare symbol name". The incremental indexer reads
    # it backwards: when a symbol changes, these are the chunks whose edges have
    # to be recomputed even though their own files did not change.
    Column("symbol_simple_name", String(256), nullable=False),
    Column("chunk_hash", String(64), nullable=False),
    UniqueConstraint("symbol_simple_name", "chunk_hash", name="pk_references"),
    Index("idx_references_symbol", "symbol_simple_name"),
    Index("idx_references_chunk", "chunk_hash"),
)


module_summaries = Table(
    "module_summaries",
    metadata,
    # merkle_key = hash of the children's keys, so a summary is reusable exactly
    # when nothing beneath it changed, and a change re-summarises only the
    # ancestors on its own path to the root.
    Column("merkle_key", String(64), primary_key=True),
    Column("path", Text, nullable=False),
    Column("level", Integer, nullable=False, server_default="0"),
    Column("text", Text, nullable=False, server_default=""),
    Column("children_keys", _JSON, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_module_summaries_path", "path"),
)


# --------------------------------------------------------------------------- snapshots


projects = Table(
    "projects",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("repo", String(256), nullable=False),
    Column("root_path", Text, nullable=False),
    Column("name", String(256), nullable=False),
    # maven | gradle | npm | go | bazel | python
    Column("kind", String(32), nullable=False),
    Index("idx_projects_repo", "repo"),
)


snapshots = Table(
    "snapshots",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("repo", String(256), nullable=False),
    Column("sha", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    # what the indexer did to produce it — counts, timings, exclusions
    Column("stats", _JSON, nullable=False, server_default="{}"),
    UniqueConstraint("repo", "sha", name="uq_snapshots_repo_sha"),
    Index("idx_snapshots_repo", "repo", "created_at"),
)


snapshot_files = Table(
    "snapshot_files",
    metadata,
    # The whole of what a snapshot *is*: which path held which blob. A rename is
    # a new row here and nothing else in the database.
    Column("snapshot_id", String(64), ForeignKey("snapshots.id"), nullable=False),
    Column("path", Text, nullable=False),
    Column("blob_hash", String(64), nullable=False),
    Column("project", String(256), nullable=False, server_default=""),
    UniqueConstraint("snapshot_id", "path", name="pk_snapshot_files"),
    Index("idx_snapshot_files_blob", "blob_hash"),
    Index("idx_snapshot_files_snapshot", "snapshot_id"),
)


environments = Table(
    "environments",
    metadata,
    Column("env", String(64), primary_key=True),
    Column("repo", String(256), primary_key=True),
    Column("sha", String(64), nullable=False),
    Column("deployed_at", DateTime(timezone=True), server_default=func.now()),
)


#: Phase M. What `work/alerts.py` last saw, per repo — the one piece of memory
#: a stateless health check needs to tell a LEVEL ("unresolved is 14%") from a
#: JUMP ("unresolved just moved from 8% to 14%"), which is the fact actually
#: worth paging someone about.
alert_state = Table(
    "alert_state",
    metadata,
    Column("repo", String(256), primary_key=True),
    Column("unresolved_rate", Float, nullable=False, server_default="0"),
    Column("checked_at", DateTime(timezone=True), server_default=func.now()),
)


# --------------------------------------------------------------------------- app tables
# Migrated as-is from data/runs.sqlite. Bridge's own opinion about itself — runs,
# feedback, disagreements, the answer cache. Same columns, same meanings; only
# the engine underneath them changes.


runs = Table(
    "runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("thread_id", String(64), nullable=False),
    Column("request", Text, nullable=False),
    Column("sha", String(64)),
    Column("lane", String(32)),
    Column("status", String(32), nullable=False),
    Column("confidence", String(16)),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("elapsed_s", Float, nullable=False, server_default="0"),
    Column("citations", Integer, nullable=False, server_default="0"),
    Column("state", _JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_runs_thread", "thread_id", "created_at"),
    Index("idx_runs_status", "status"),
)


feedback = Table(
    "feedback",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", String(64), nullable=False),
    Column("vote", String(8), nullable=False),
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_feedback_run", "run_id"),
)


vocab_proposals = Table(
    "vocab_proposals",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("term", String(256), nullable=False),
    Column("entities", _JSON, nullable=False, server_default="[]"),
    Column("note", Text),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_vocab_status", "status"),
)


eval_candidates = Table(
    "eval_candidates",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", String(64), nullable=False),
    Column("node", String(64), nullable=False),
    Column("field", String(64), nullable=False),
    Column("candidates", _JSON, nullable=False),
    Column("chosen", Text, nullable=False),
    Column("is_majority", Integer, nullable=False, server_default="0"),
    Column("reason", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_eval_candidates_run", "run_id"),
    Index("idx_eval_candidates_field", "node", "field"),
)


answer_cache = Table(
    "answer_cache",
    metadata,
    Column("snapshot_key", String(64), primary_key=True),
    Column("question_norm", Text, primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_answer_cache_run", "run_id"),
)


# --------------------------------------------------------------------------- work
# Phase F. Everything above describes what the index IS; this describes what is
# being done to it. A queue in the same database as the data it operates on, so
# "enqueue this work" and "record the result of that work" are one transaction
# rather than two systems that can disagree.


jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # index_commit | describe_backfill | summarise_modules | verify_nightly | gc_weekly
    Column("type", String(32), nullable=False),
    Column("payload", _JSON, nullable=False, server_default="{}"),
    # queued | running | done | failed | cancelled
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # when this job becomes eligible — the backoff mechanism, and the schedule
    # mechanism, are the same column
    Column("run_after", DateTime(timezone=True), server_default=func.now()),
    Column("repo", String(256), nullable=False, server_default=""),
    # which worker holds it, for the partitioning story and for finding orphans
    Column("locked_by", String(128)),
    Column("locked_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("result", _JSON),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    # The claim query orders by (run_after, id) filtered on status — this index is
    # what keeps SKIP LOCKED from degenerating into a sequential scan once the
    # table has a few hundred thousand finished rows in it.
    Index("idx_jobs_claim", "status", "run_after", "id"),
    Index("idx_jobs_repo", "repo", "type", "status"),
)


#: Job types. Named here rather than as free strings at call sites, because a
#: typo'd type is a job that is enqueued, never claimed, and never noticed.
JOB_INDEX_COMMIT = "index_commit"
JOB_DESCRIBE_BACKFILL = "describe_backfill"
JOB_SUMMARISE_MODULES = "summarise_modules"
JOB_VERIFY_NIGHTLY = "verify_nightly"
JOB_GC_WEEKLY = "gc_weekly"
JOB_HEALTH_ALERT = "health_alert"
JOB_VOCAB_STALENESS = "vocab_staleness"
JOB_TYPES = (
    JOB_INDEX_COMMIT, JOB_DESCRIBE_BACKFILL, JOB_SUMMARISE_MODULES,
    JOB_VERIFY_NIGHTLY, JOB_GC_WEEKLY, JOB_HEALTH_ALERT, JOB_VOCAB_STALENESS,
)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


#: Tables whose rows are content-addressed and therefore never updated in place.
#: `bridge verify` re-derives the hash of every row in these and reports drift.
IMMUTABLE_TABLES = ("blobs", "chunks", "descriptions", "vectors", "edges")
