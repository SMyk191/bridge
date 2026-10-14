"""Product vocabulary: the bridge between what someone asks and what the code calls it.

A product manager asks about "appointments"; the code says `Visit`. No embedding
model closes that gap reliably, because the words never co-occur in the
repository. The vocabulary closes it by hand, per org, from
``config/vocab.<repo>.yaml`` (design law 7 — never hardcoded).

:func:`expand_query` does two separable things, and callers use both:

* returns an **expanded query string** — the user's words plus the engineering
  nouns a matched term implies — for semantic search;
* returns the **node ids** those terms name, which enter retrieval as graph seeds
  rather than as search results, because they are exact, not ranked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config


@dataclass(slots=True)
class VocabTerm:
    term: str
    aliases: list[str]
    nodes: list[str]
    expand: str = ""
    pattern: re.Pattern[str] | None = None


@dataclass(slots=True)
class ExpandedQuery:
    original: str
    expanded: str
    #: node ids named by the matched terms — exact seeds, not ranked hits
    node_ids: list[str] = field(default_factory=list)
    #: which vocabulary terms fired
    matched_terms: list[str] = field(default_factory=list)
    #: the literal phrases in the question that matched
    matched_aliases: list[str] = field(default_factory=list)
    #: bare identifiers found in the question (CamelCase, snake_case, dotted, /routes)
    identifiers: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.matched_terms)


#: things that look like code in a question: CamelCase, snake_case, a.b.C, /a/b, foo()
_IDENTIFIER_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b"),      # camelCase / CamelCase tail
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b"),      # PascalCase
    re.compile(r"\b[a-z_][a-z0-9_]*_[a-z0-9_]+\b"),            # snake_case
    re.compile(r"\b[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+\b"),    # dotted.path
    re.compile(r"/[a-zA-Z0-9_{}/.-]+"),                        # /routes/{id}
)

#: words that look like identifiers but are just English
_IDENTIFIER_STOPWORDS = frozenset(
    {"e.g", "i.e", "etc", "vs", "a.k.a", "someone", "something", "anything"}
)


class Vocabulary:
    """Loaded terms plus the matching logic."""

    def __init__(self, terms: list[VocabTerm], repo: str = "") -> None:
        self.repo = repo
        self.terms = terms
        for term in self.terms:
            # longest alias first so "owner search" wins over "owner"
            alternatives = sorted((re.escape(a) for a in term.aliases), key=len, reverse=True)
            term.pattern = re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.IGNORECASE)

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Vocabulary":
        import yaml

        path = Path(path) if path else config.CONFIG_ROOT / config.VOCAB_FILE
        if not path.is_file():
            return cls([], repo="")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        terms = [
            VocabTerm(
                term=entry["term"],
                aliases=list(entry.get("aliases") or [entry["term"]]),
                nodes=list(entry.get("nodes") or []),
                expand=(entry.get("expand") or "").strip(),
            )
            for entry in (data.get("terms") or [])
        ]
        return cls(terms, repo=data.get("repo", ""))

    @classmethod
    def empty(cls) -> "Vocabulary":
        return cls([])

    def __len__(self) -> int:
        return len(self.terms)

    # -- matching -----------------------------------------------------------

    def identifiers_in(self, question: str) -> list[str]:
        """Code-shaped tokens in the question — the keyword-search seeds."""
        found: list[str] = []
        for pattern in _IDENTIFIER_PATTERNS:
            for match in pattern.findall(question):
                token = match.strip(".,;:!?)")
                if (
                    len(token) > 2
                    and token.lower() not in _IDENTIFIER_STOPWORDS
                    and token not in found
                ):
                    found.append(token)
        return found

    def expand_query(self, question: str) -> ExpandedQuery:
        """Match vocabulary terms and fold their engineering nouns into the query."""
        result = ExpandedQuery(original=question, expanded=question)
        additions: list[str] = []

        for term in self.terms:
            assert term.pattern is not None
            hits = term.pattern.findall(question)
            if not hits:
                continue
            result.matched_terms.append(term.term)
            result.matched_aliases.extend(sorted({hit.lower() for hit in hits}))
            for node_id in term.nodes:
                if node_id not in result.node_ids:
                    result.node_ids.append(node_id)
            if term.expand:
                additions.append(term.expand)

        if additions:
            result.expanded = f"{question}\n{' '.join(additions)}"
        result.identifiers = self.identifiers_in(question)
        return result


_DEFAULT: Vocabulary | None = None


def default_vocabulary() -> Vocabulary:
    """The repo's configured vocabulary, loaded once."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Vocabulary.load()
    return _DEFAULT


def expand_query(question: str, vocabulary: Vocabulary | None = None) -> ExpandedQuery:
    """Expand ``question`` using the configured vocabulary."""
    return (vocabulary or default_vocabulary()).expand_query(question)
