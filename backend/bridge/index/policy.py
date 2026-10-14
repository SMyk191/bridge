"""What earns a place in the index, and — recorded just as carefully — what does not.

petclinic is 49 Java files, so today's indexer walks everything and gets away
with it. A real repository is mostly things nobody will ever ask a product
question about: `node_modules`, vendored dependencies, generated protobuf stubs,
minified bundles, lockfiles, snapshot fixtures. Indexing them costs parse time,
description money, embedding space, and — worse — dilutes retrieval with matches
nobody wants.

The policy is per repo and lives in `config/index_policy.<repo>.yaml` alongside
`vocab.<repo>.yaml` (design law 7: org-specific things live in config/).

**Every exclusion carries a reason, and the reasons are counted and printed.**
That is not decoration. An excluded file and a file the parser silently failed on
look identical from the outside — both are simply absent — and the only thing
that distinguishes "we chose not to index this" from "we have a bug" is a number
somebody can read at the end of a run.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import config

# --------------------------------------------------------------------------- reasons

NOT_IN_SOURCE_ROOT = "not under an allowed source root"
DENYLISTED = "matched a denylist pattern"
TOO_LARGE = "larger than the size cap"
GENERATED = "generated-code header"
UNREADABLE = "could not be read"
NOT_A_SOURCE_FILE = "not a language Bridge parses"


@dataclass(slots=True)
class Policy:
    """One repo's answer to "what should be indexed?"."""

    #: path prefixes that may be indexed. Empty means "the whole repo".
    source_roots: tuple[str, ...] = ()
    #: glob patterns, matched against the repo-relative path AND its basename
    deny: tuple[str, ...] = ()
    #: file extensions Bridge has a parser for
    extensions: tuple[str, ...] = (".java", ".html")
    max_bytes: int = config.INDEX_MAX_FILE_BYTES
    generated_markers: tuple[str, ...] = config.INDEX_GENERATED_MARKERS
    header_lines: int = config.INDEX_GENERATED_HEADER_LINES
    repo: str = ""

    @classmethod
    def default(cls, repo: str = "") -> "Policy":
        """The denylist worth having before anyone writes a config file.

        These are conventions, not preferences: `node_modules/` is never product
        behaviour, and a `*.min.js` has no readable structure to extract.
        """
        return cls(
            deny=(
                "node_modules/**", "**/node_modules/**",
                "vendor/**", "**/vendor/**",
                "generated/**", "**/generated/**",
                "**/__snapshots__/**",
                "**/fixtures/**",
                "*.min.*", "**/*.min.*",
                "*.pb.*", "**/*.pb.*",
                "*.lock", "**/*.lock",
                "**/target/**", "**/build/**", "**/dist/**", "**/.git/**",
            ),
            repo=repo,
        )

    @classmethod
    def load(cls, repo: str, config_root: Path | None = None) -> "Policy":
        """`config/index_policy.<repo>.yaml`, then `index_policy.yaml`, then defaults.

        A missing file is not an error — the defaults above are a working policy,
        and demanding a config file before a repo can be indexed at all would be
        friction with no safety benefit.
        """
        import yaml

        root = config_root or config.CONFIG_ROOT
        candidates = [
            root / config.INDEX_POLICY_FILE_TEMPLATE.format(repo=repo),
            root / config.INDEX_POLICY_DEFAULT_FILE,
        ]
        for path in candidates:
            if path.is_file():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                base = cls.default(repo)
                return cls(
                    source_roots=tuple(data.get("source_roots") or ()),
                    # a config's denylist EXTENDS the defaults rather than
                    # replacing them: nobody wants to re-type node_modules to add
                    # one project-specific pattern
                    deny=tuple(base.deny) + tuple(data.get("deny") or ()),
                    extensions=tuple(data.get("extensions") or base.extensions),
                    max_bytes=int(data.get("max_bytes") or base.max_bytes),
                    generated_markers=tuple(
                        data.get("generated_markers") or base.generated_markers
                    ),
                    header_lines=int(data.get("header_lines") or base.header_lines),
                    repo=repo,
                )
        return cls.default(repo)

    # -- the decision ------------------------------------------------------

    def _denied_by(self, rel_path: str) -> str | None:
        name = Path(rel_path).name
        for pattern in self.deny:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
                return pattern
        return None

    def _in_source_root(self, rel_path: str) -> bool:
        if not self.source_roots:
            return True
        return any(
            rel_path == root or rel_path.startswith(root.rstrip("/") + "/")
            for root in self.source_roots
        )

    def looks_generated(self, head: str) -> bool:
        lowered = head.lower()
        return any(marker in lowered for marker in self.generated_markers)

    def decide(self, rel_path: str, size_bytes: int, head: str = "") -> str | None:
        """``None`` to index it, otherwise the reason it was excluded.

        Ordered cheapest-check-first: extension and path tests are string work,
        the size check needs a stat, and the generated-header sniff needs to have
        read the file. Callers pass ``head`` only when they already have it.
        """
        if not rel_path.endswith(tuple(self.extensions)):
            return NOT_A_SOURCE_FILE
        if not self._in_source_root(rel_path):
            return NOT_IN_SOURCE_ROOT
        if self._denied_by(rel_path):
            return DENYLISTED
        if size_bytes > self.max_bytes:
            return TOO_LARGE
        if head and self.looks_generated(head):
            return GENERATED
        return None


@dataclass(slots=True)
class PolicyResult:
    included: list[str] = field(default_factory=list)
    #: reason -> how many files it excluded
    excluded: dict[str, int] = field(default_factory=dict)
    #: reason -> a few examples, so a surprising count can be investigated
    examples: dict[str, list[str]] = field(default_factory=dict)

    def exclude(self, rel_path: str, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1
        shown = self.examples.setdefault(reason, [])
        if len(shown) < 3:
            shown.append(rel_path)

    @property
    def total_excluded(self) -> int:
        return sum(self.excluded.values())

    def as_dict(self) -> dict:
        return {
            "included": len(self.included),
            "excluded": dict(sorted(self.excluded.items())),
            "excluded_total": self.total_excluded,
            "examples": {k: list(v) for k, v in sorted(self.examples.items())},
        }


def apply(repo_root: Path, paths: Iterable[str], policy: Policy) -> PolicyResult:
    """Run ``policy`` over ``paths`` (repo-relative), reading heads only when needed."""
    result = PolicyResult()
    for rel_path in sorted(paths):
        full = repo_root / rel_path
        try:
            size = full.stat().st_size
        except OSError:
            result.exclude(rel_path, UNREADABLE)
            continue

        reason = policy.decide(rel_path, size)
        if reason is not None:
            result.exclude(rel_path, reason)
            continue

        # Only now is it worth opening the file — the header sniff is the one
        # check that costs I/O, so it runs last and only on survivors.
        head = ""
        try:
            with full.open("r", encoding="utf-8", errors="replace") as handle:
                head = "".join(next(handle, "") for _ in range(policy.header_lines))
        except OSError:
            result.exclude(rel_path, UNREADABLE)
            continue

        if policy.looks_generated(head):
            result.exclude(rel_path, GENERATED)
            continue
        result.included.append(rel_path)
    return result
