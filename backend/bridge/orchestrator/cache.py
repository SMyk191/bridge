"""The answer cache's pure functions: what counts as "the same question", what
counts as "the same conditions" to answer it under, and — the semantic layer —
what counts as "close enough to answer with a prior run instead."

Neither the exact key-derivation nor the semantic matching touches SQLite;
`RunStore.cache_lookup`/`cache_store`/`semantic_cache_candidates` do that,
keyed on what this module produces. Keeping the logic here and the storage in
`store.py` means either can be tested, or changed, without touching the other.

**The semantic layer's "same lane" requirement is approximated, honestly.** A
question's real lane is only known after interpretation AND cartography
(design law 3) — which is exactly the cost a cache exists to avoid paying. So
`cheap_lane_guess` reuses `agents/interpreter.py`'s keyword fallback (the same
classifier the pipeline itself falls back to when no model is reachable) as a
zero-cost proxy at lookup time, scoped narrowly to "would this obviously be a
data_question / bug / change_request / question", not to the finer rules a
real cartographer applies. Combined with a high similarity threshold
(`config.SEMANTIC_CACHE_THRESHOLD`, default 0.95), a coarse lane match is
enough: a near-duplicate wording of the same underlying question rarely
crosses one of these four coarse buckets.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Sequence

from .. import config
from ..agents.interpreter import guess_intent
from ..agents.llm import load_prompt

#: every prompt whose text can change what an answer says. Deliberately
#: inclusive rather than minimal — `describe`/`suggestions` shape the INDEX
#: rather than a per-question answer, but hashing a couple of extra files that
#: rarely change costs nothing, while missing one that does would serve a
#: stale answer under a prompt that has since been rewritten.
_PROMPT_NAMES = (
    "interpreter",
    "cartographer_summary",
    "cartographer_merge",
    "architect",
    "explainer",
    "changelog_group",
    "describe",
    "rerank",
    "suggestions",
)

#: a small set of trailing punctuation marks that don't change what was asked
_TRAILING_PUNCTUATION = re.compile(r"[.?!,;:]+$")
_WHITESPACE = re.compile(r"\s+")


def question_norm(question: str) -> str:
    """lowercase, collapse internal whitespace, drop trailing punctuation.

    Two questions a person would call "the same ask" — differing only in case,
    doubled spaces, or a trailing "?" — must land on the same cache key, or the
    cache never hits on the exact repeats it exists to catch. Nothing more
    aggressive than that: word order, synonyms, and internal punctuation are
    left alone, so this is a normaliser, not a paraphrase matcher.
    """
    text = _WHITESPACE.sub(" ", question.strip().lower())
    return _TRAILING_PUNCTUATION.sub("", text).strip()


def snapshot_key(repo: str, sha: str) -> str:
    """What this answer was computed against — changing any of it invalidates
    every cached entry automatically, with no explicit cache-clear step.

    Three things go in: the indexed commit, every prompt file's actual text,
    and the model/temperature configuration for every role. All three are
    hashed by VALUE. A prompt edited in place, a role repointed to a different
    model, or a rebuild against a new commit each produce a different key on
    the very next request — nobody has to remember to bump a version number.
    """
    hasher = hashlib.sha256()
    hasher.update(repo.encode("utf-8"))
    hasher.update(b"@")
    hasher.update(sha.encode("utf-8"))
    for name in _PROMPT_NAMES:
        try:
            hasher.update(load_prompt(name).encode("utf-8"))
        except OSError:
            continue
    hasher.update(json.dumps(config.ROLE_MODELS, sort_keys=True, default=str).encode("utf-8"))
    hasher.update(json.dumps(config.ROLE_PROVIDER, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()[:24]


# --------------------------------------------------------------------------- semantic layer


def cheap_lane_guess(question: str) -> str:
    """A zero-cost stand-in for `state.lane`, for cache lookup only — never
    used to decide how a real (non-cached) run is routed. See the module
    docstring for why an approximation is the honest choice here."""
    return guess_intent(question)


def embed_question(question: str, encoder) -> list[float]:
    """The same local model (`index/embed.py::load_encoder`) that embeds
    chunks at index time — no LLM call, no per-question cost, which is what
    makes a semantic cache LOOKUP cheap even on a miss."""
    vector = encoder.encode([question_norm(question)], normalize_embeddings=True)[0]
    return [float(x) for x in vector]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both vectors are already normalised at encode time (`embed_question`),
    so this is a plain dot product — but computed without that assumption, so
    a candidate embedded before normalisation was standard practice, or by a
    different caller entirely, still gets a correct similarity rather than a
    silently wrong one."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def best_semantic_match(
    embedding: Sequence[float], candidates: list[dict], threshold: float = config.SEMANTIC_CACHE_THRESHOLD
) -> dict | None:
    """The closest candidate at or above `threshold`, or None.

    `candidates` are same-snapshot, same-(guessed)-lane rows already —
    `RunStore.semantic_cache_candidates` does that filtering in SQL; this
    function only ever compares embeddings, so it is testable with plain
    dicts and no database at all.
    """
    best: dict | None = None
    best_score = threshold
    for candidate in candidates:
        score = _cosine(embedding, candidate["embedding"])
        if score >= best_score:
            best, best_score = {**candidate, "similarity": score}, score
    return best
