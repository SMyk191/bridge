"""The hash chain. Five functions, and every incremental guarantee rests on them.

Read `db/schema.py`'s module docstring for the shape; this is the implementation
and the normalisation rules, which are where the guarantees are actually won or
lost.

The rule that matters: **`code_hash` must not depend on anything but the code
itself.** Not the file it came from, not the line it started on, not the path.
Everything that changes when a file is edited elsewhere, or moved, or renamed,
has to be excluded here — otherwise every commit invalidates every description in
the file it touched, and the index is a rebuild wearing an incremental costume.
"""

from __future__ import annotations

import hashlib
import re

#: Bump when a prompt changes in a way that should invalidate stored
#: descriptions. It is part of `desc_key`, so bumping it re-describes everything
#: on the next index without anyone having to delete a row.
PROMPT_VERSION = "1"

#: source of a description that was generated, not asked for
SOURCE_TEMPLATE = "template"
SOURCE_INHERITED = "inherited"
SOURCE_FALLBACK = "fallback"

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def _sha(*parts: str | bytes) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part if isinstance(part, bytes) else part.encode("utf-8"))
        # a separator, so ("ab","c") and ("a","bc") cannot collide
        hasher.update(b"\x00")
    return hasher.hexdigest()


def normalise_code(code: str) -> str:
    """Strip the differences that are not differences.

    Trailing whitespace, `\\r\\n` line endings, runs of blank lines, and leading or
    trailing blank space around the whole chunk. Deliberately NOT reformatting:
    two genuinely different formattings of the same statements are different code
    to a reader, so they get different descriptions.
    """
    text = code.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def blob_hash(data: bytes) -> str:
    """sha256 of a file's raw bytes."""
    return hashlib.sha256(data).hexdigest()


def code_hash(kind: str, qualified_name: str, code: str) -> str:
    """**The reuse key.** No blob, no path, no line numbers — by construction.

    Two chunks anywhere in history with the same kind, same qualified name and
    the same normalised source share this hash, and therefore share a
    description and an embedding. That is what makes a move or a rename free.
    """
    return _sha(kind, qualified_name, normalise_code(code))


def chunk_hash(blob: str, kind: str, qualified_name: str, start_line: int, end_line: int) -> str:
    """Identity of a chunk *as it appears in one particular blob*.

    Includes the lines on purpose: it makes the row immutable and its
    `start_line`/`end_line` permanently true for that blob, which is what
    `bridge verify` checks.
    """
    return _sha(blob, kind, qualified_name, str(start_line), str(end_line))


def desc_key(code: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    """A description belongs to code, a model, and a prompt — never to a location.

    ``code`` here is a `code_hash`, not source. Including `model` means a chunk
    promoted from T0 (template) to T2 (a real model) gets a different key and is
    described properly, instead of quietly reading back the template string that
    was good enough when it was T0.
    """
    return _sha(code, model, prompt_version)


def vec_key(description: str, embed_model: str) -> str:
    """An embedding belongs to a description and the model that embedded it."""
    return _sha(description, embed_model)


def snapshot_id(repo: str, sha: str) -> str:
    """Stable id for (repo, sha), so re-indexing the same commit is idempotent."""
    return _sha(repo, sha)[:32]


def merkle_key(path: str, children: list[str]) -> str:
    """A directory's key is its children's keys.

    Sorted, so the key depends on the set of children rather than on the order a
    filesystem happened to walk them in — otherwise a summary would be recomputed
    on every index for no reason at all.
    """
    return _sha(path, *sorted(children))
