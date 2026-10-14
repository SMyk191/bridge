"""Check every citation an answer makes against the graph it was built from.

Design law 4 says every claim carries a citation. That is only worth something if
a citation can be *wrong* and get caught. Three things can be wrong with one:

* **the commit** — a citation naming a sha this index was not built at describes
  code nobody here has seen;
* **the file** — a path with no node in the graph did not exist at that commit;
* **the range** — `Pet.java:400-450` in a 120-line file is a line number the model
  produced, not one it read.

The third is the one that matters most in practice, and it is the reason this
checks *spans* rather than just paths: a plausible-looking range inside a real
file is exactly what a fabricated citation looks like, and a path-only check
waves it through.

What happens to a failure depends on where it lives. An impact-map entry is a
row and is simply dropped, the way the cartographer already drops entries whose
citation was not in the evidence. Prose cannot be edited that way without
rewriting someone's sentence, so :func:`scrub` removes the citation *string* and
leaves a visible marker: the claim survives, its false support does not.

Citations without a line range and with a sha that is not the indexed one are
**skipped, not failed** — that is the changelog's `repo@<commit>:path` form,
which points at a commit this index deliberately was not built from. Failing
those would mean a changelog could never be verified as anything but broken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .. import config
from ..agents.state import citations_in

#: `repo@sha:path`, `repo@sha:path:45`, `repo@sha:path:12-30`
_CITATION = re.compile(r"^(?P<repo>[^@]+)@(?P<sha>[0-9a-fA-F]+):(?P<rest>.+)$")
_RANGE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")

#: shortest sha prefix accepted as naming the indexed commit
MIN_SHA_PREFIX = 8

REASON_SHA = "names a different commit than the one indexed"
REASON_PATH = "no such file in the graph at this commit"
REASON_RANGE = "the cited lines fall outside every indexed span in that file"

SCRUB_MARKER = "(citation removed — could not be verified)"


@dataclass(slots=True)
class VerifiedCitation:
    """One citation that checked out, plus what the graph says lives there."""

    citation: str
    path: str
    start: int | None = None
    end: int | None = None
    #: the tightest node whose span contains the cited range
    node_id: str = ""
    kind: str = ""
    io_role: str = config.IO_NONE
    io_source: str = ""
    #: every node kind whose span contains the range — a class and an entity can
    #: occupy the same lines, and both facts matter to the coverage rules
    kinds: frozenset[str] = frozenset()
    roles: frozenset[tuple[str, str]] = frozenset()

    @property
    def is_read(self) -> bool:
        return any(role in (config.IO_READ, config.IO_BOTH) for role, _ in self.roles)

    @property
    def is_persisting_write(self) -> bool:
        """A write, and one that actually persists — not a setter (see config)."""
        return any(
            role in (config.IO_WRITE, config.IO_BOTH) and source in config.IO_PERSIST_SOURCES
            for role, source in self.roles
        )


@dataclass(slots=True)
class VerificationResult:
    verified: list[VerifiedCitation] = field(default_factory=list)
    #: (citation, why it failed)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    #: out of scope for this checker — a changelog's commit citations
    skipped: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.verified) + len(self.dropped)

    @property
    def drop_ratio(self) -> float:
        return (len(self.dropped) / self.checked) if self.checked else 0.0

    @property
    def dropped_citations(self) -> list[str]:
        return [citation for citation, _ in self.dropped]

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "verified": len(self.verified),
            "dropped": [{"citation": c, "reason": r} for c, r in self.dropped],
            "skipped": len(self.skipped),
            "drop_ratio": round(self.drop_ratio, 3),
        }


def parse_citation(citation: str) -> tuple[str, str, str, int | None, int | None] | None:
    """`repo@sha:path:12-30` -> (repo, sha, path, 12, 30). None if it is not one."""
    match = _CITATION.match(citation.strip())
    if match is None:
        return None
    rest = match.group("rest")
    ranged = _RANGE.match(rest)
    if ranged is None:
        return match.group("repo"), match.group("sha"), rest, None, None
    start = int(ranged.group("start"))
    end = int(ranged.group("end")) if ranged.group("end") else start
    return match.group("repo"), match.group("sha"), ranged.group("path"), start, end


def _sha_matches(cited: str, index_sha: str) -> bool:
    """A prefix of at least :data:`MIN_SHA_PREFIX` counts — writers shorten shas."""
    if not cited or not index_sha:
        return False
    if len(cited) < MIN_SHA_PREFIX:
        return False
    return index_sha.startswith(cited) or cited.startswith(index_sha)


class SpanIndex:
    """Every node's span, by path. Built once per verification, from the graph."""

    def __init__(self, store) -> None:
        self.by_path: dict[str, list[tuple[int, int, str, str, str, str]]] = {}
        for row in store.conn.execute(
            "SELECT id, kind, path, start_line, end_line, attrs FROM nodes ORDER BY id"
        ):
            attrs = json.loads(row["attrs"])
            self.by_path.setdefault(row["path"], []).append(
                (
                    int(row["start_line"] or 1),
                    int(row["end_line"] or 0),
                    row["id"],
                    row["kind"],
                    attrs.get("io_role") or config.IO_NONE,
                    attrs.get("io_source") or "",
                )
            )

    def containing(self, path: str, start: int | None, end: int | None) -> list[tuple]:
        """Nodes at ``path`` whose span contains the range. All of them, tightest first."""
        spans = self.by_path.get(path)
        if not spans:
            return []
        if start is None:
            # A whole-file citation: the file node is the containing span.
            return sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:1]
        found = [span for span in spans if span[0] <= start and (span[1] or 0) >= (end or start)]
        return sorted(found, key=lambda s: s[1] - s[0])


def verify_citations(
    citations: Iterable[str], store, sha: str, *, span_index: SpanIndex | None = None
) -> VerificationResult:
    """Check each citation against the graph. Order of ``citations`` is preserved."""
    result = VerificationResult()
    index = span_index if span_index is not None else SpanIndex(store)

    for citation in citations:
        parsed = parse_citation(citation)
        if parsed is None:
            result.dropped.append((citation, "is not a citation this system could have written"))
            continue
        _repo, cited_sha, path, start, end = parsed

        if not _sha_matches(cited_sha, sha):
            # A rangeless citation at another sha is the changelog's commit form:
            # it points at a commit on purpose, and this checker has no business
            # with it. A *ranged* one at another sha is a real failure.
            if start is None:
                result.skipped.append(citation)
            else:
                result.dropped.append((citation, REASON_SHA))
            continue

        containing = index.containing(path, start, end)
        if not containing:
            reason = REASON_PATH if path not in index.by_path else REASON_RANGE
            result.dropped.append((citation, reason))
            continue

        tightest = containing[0]
        result.verified.append(
            VerifiedCitation(
                citation=citation,
                path=path,
                start=start,
                end=end,
                node_id=tightest[2],
                kind=tightest[3],
                io_role=tightest[4],
                io_source=tightest[5],
                kinds=frozenset(span[3] for span in containing),
                roles=frozenset((span[4], span[5]) for span in containing),
            )
        )
    return result


def verify_text(text: str, store, sha: str, **kwargs) -> VerificationResult:
    """Verify every citation appearing in ``text``, in order of appearance."""
    return verify_citations(citations_in(text), store, sha, **kwargs)


def scrub(text: str, dropped: Sequence[str], marker: str = SCRUB_MARKER) -> str:
    """Replace failed citations in prose with a visible marker.

    Not a deletion of the sentence around it. The model's claim may well be true;
    what has been established is only that the pointer it offered does not resolve,
    and a reader is better served by seeing that than by seeing a tidy paragraph.
    """
    for citation in dropped:
        for form in (f"`{citation}`", citation):
            if form in text:
                text = text.replace(form, marker)
                break
    return text
