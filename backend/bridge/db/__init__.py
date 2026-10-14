"""The persistent index: content-addressed Postgres storage (phase E)."""

from .engine import create_all, dispose_all, ensure_extensions, get_engine, resolve_url
from .keys import (
    PROMPT_VERSION,
    SOURCE_FALLBACK,
    SOURCE_INHERITED,
    SOURCE_TEMPLATE,
    blob_hash,
    chunk_hash,
    code_hash,
    desc_key,
    merkle_key,
    normalise_code,
    snapshot_id,
    vec_key,
)
from .schema import IMMUTABLE_TABLES, metadata

__all__ = [
    "create_all", "dispose_all", "ensure_extensions", "get_engine", "resolve_url",
    "PROMPT_VERSION", "SOURCE_FALLBACK", "SOURCE_INHERITED", "SOURCE_TEMPLATE",
    "blob_hash", "chunk_hash", "code_hash", "desc_key", "merkle_key",
    "normalise_code", "snapshot_id", "vec_key",
    "IMMUTABLE_TABLES", "metadata",
]
