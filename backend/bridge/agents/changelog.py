"""Changelog — "what changed" over a commit range, per product area.

The only lane whose evidence is not retrieval. There is nothing to search for:
the question already names its own evidence, which is a range of commits, so this
node reads `git log` and never touches the vector index, the reranker, or the
impact map.

What the graph *is* used for is turning file paths into product words. A commit
subject says "Enforce unique pet names per owner"; the graph says that
`Pet.java` holds the stored details of a pet and lives in the `owner` module, and
the vocabulary says that module is what a product person calls "pets". Grouping on
those two things is why the output reads as "what changed, per area" rather than
as a list of paths.

**Determinism is the contract.** Everything except the per-group sentence is plain
Python: the range, the commit walk, the grouping, and the order groups appear in.
Two runs over the same range produce the same document with the same areas in the
same order — a changelog that reshuffles itself between runs is not a changelog.

No author names, emails, or per-person counts are read or written, matching
`index/history.py`: the `--pretty` format below carries none.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..index import ids
from ..index.embed import module_of
from ..redact import RedactionLog, redact_text
from ..retrieval.vocab import Vocabulary, default_vocabulary
from .llm import LLMClient, Usage, load_prompt, wrap_retrieved
from .state import citations_in

#: files whose changes are noise in a product changelog
_IGNORED_SUFFIXES = (".lock", ".png", ".jpg", ".gif", ".ico", ".svg", ".woff", ".woff2")
_IGNORED_NAMES = frozenset({".gitignore", ".gitattributes", "LICENSE", "NOTICE.txt"})


@dataclass(slots=True)
class Commit:
    sha: str
    date: str
    subject: str
    files: list[str] = field(default_factory=list)


def area_of(path: str) -> str:
    """The product area a changed file belongs to.

    `module_of` is the right answer for source, but it has no answer for a file
    at the repository root: with no parent directory it returns the filename
    itself, so `README.md` and `build.gradle` each become an "area" and outrank
    a real module that happened to see fewer commits. Everything that is tooling
    rather than product is therefore folded into one bucket, which
    :meth:`Group.sort_key` then keeps at the bottom.
    """
    name = Path(path).name
    if (
        "/" not in path
        or name in config.CHANGELOG_BUILD_FILES
        or path.startswith(config.CHANGELOG_BUILD_PREFIXES)
    ):
        return config.CHANGELOG_BUILD_AREA
    for fragment, area in config.CHANGELOG_MERGED_AREAS:
        if fragment in path:
            return area
    return module_of(path) or config.CHANGELOG_BUILD_AREA


@dataclass(slots=True)
class Group:
    """One product area's slice of the range."""

    area: str
    commits: list[Commit] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    #: vocabulary terms whose nodes live in these files — the product's own words
    terms: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)

    @property
    def is_product(self) -> bool:
        return self.area != config.CHANGELOG_BUILD_AREA

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Product areas first, most-changed first, ties broken by files then name.

        Every component is an integer or a string read off the data, so the order
        is a property of the range rather than of the run. Tooling sorts last
        regardless of volume: a dependency bump touching forty files is still not
        what someone asking "what changed" wants at the top.
        """
        return (0 if self.is_product else 1, -len(self.commits), -len(self.files), self.area)


@dataclass(slots=True)
class ChangelogReport:
    ask: Any
    groups: list[Group] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    skipped_areas: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ask": self.ask.as_dict() if self.ask is not None else None,
            "commits": len(self.commits),
            "areas": [g.area for g in self.groups],
            "skipped_areas": self.skipped_areas,
            "error": self.error,
        }


def _usage_of(response) -> Usage:
    return Usage(
        calls=1,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        thinking_tokens=response.thinking_tokens,
        cost_usd=response.cost_usd,
        by_role={response.role: 1},
        by_provider={response.provider: 1},
    )


def _interesting(path: str) -> bool:
    name = Path(path).name
    return not (name in _IGNORED_NAMES or path.endswith(_IGNORED_SUFFIXES))


# --------------------------------------------------------------------------- git


def read_commits(repo_root: Path, ask) -> list[Commit]:
    """The commits in ``ask``'s range, newest first. One `git log`, no author fields.

    `%x00` delimits commits so a filename can never be mistaken for a header —
    the same parse discipline as `index/history.py`, for the same reason.
    """
    args = ["log", "--name-only", "--no-renames", "--no-merges", "--pretty=format:%x00%H|%cI|%s"]
    if ask.from_sha:
        args.append(f"{ask.from_sha}..{ask.to_sha}")
    else:
        if ask.since:
            args.append(f"--since={ask.since}")
        args.append(f"-n{max(1, min(ask.commits, config.CHANGELOG_MAX_COMMITS))}")
        if ask.to_sha and ask.to_sha != "HEAD":
            args.append(ask.to_sha)

    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git log failed").strip().splitlines()[0])

    commits: list[Commit] = []
    for block in result.stdout.split("\x00"):
        block = block.strip("\n")
        if not block:
            continue
        header, _, files = block.partition("\n")
        parts = header.split("|", 2)
        if len(parts) != 3:
            continue
        sha, iso, subject = parts
        commits.append(
            Commit(
                sha=sha.strip(),
                date=iso.strip()[:10],
                # A commit subject is untrusted text written by anyone with push
                # access; it reaches a prompt, so it is redacted like any other
                # repository content (design law 8).
                subject=redact_text(subject.strip()),
                files=[f.strip() for f in files.splitlines() if f.strip() and _interesting(f.strip())],
            )
        )
    return commits


# --------------------------------------------------------------------------- grouping


def _describe_paths(store, paths: list[str], descriptions: dict[str, str]) -> dict[str, list[str]]:
    """path -> a few plain-English descriptions of what lives in that file."""
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    rows = store.conn.execute(
        f"SELECT path, id, kind FROM nodes WHERE path IN ({placeholders})"
        " ORDER BY path, kind, id",
        tuple(paths),
    ).fetchall()
    found: dict[str, list[str]] = {}
    for row in rows:
        description = descriptions.get(row["id"])
        if not description:
            continue
        bucket = found.setdefault(row["path"], [])
        if len(bucket) < 3 and description not in bucket:
            bucket.append(description)
    return found


def _terms_for_paths(store, paths: list[str], vocabulary: Vocabulary) -> list[str]:
    """Vocabulary terms whose nodes live in ``paths`` — the product's word for this area."""
    if not paths or not len(vocabulary):
        return []
    wanted = {node_id: term.term for term in vocabulary.terms for node_id in term.nodes}
    if not wanted:
        return []
    placeholders = ",".join("?" * len(paths))
    rows = store.conn.execute(
        f"SELECT DISTINCT id FROM nodes WHERE path IN ({placeholders}) ORDER BY id",
        tuple(paths),
    ).fetchall()
    terms = {wanted[row["id"]] for row in rows if row["id"] in wanted}
    return sorted(terms)


def group_commits(commits: list[Commit], store=None, vocabulary: Vocabulary | None = None) -> list[Group]:
    """Group by module, ordered deterministically. Pure function of the commits."""
    areas: dict[str, Group] = {}
    for commit in commits:
        for path in commit.files:
            area = area_of(path)
            group = areas.setdefault(area, Group(area=area))
            if commit.sha not in {c.sha for c in group.commits}:
                group.commits.append(commit)
            if path not in group.files:
                group.files.append(path)

    for group in areas.values():
        group.files.sort()
        if store is not None:
            group.terms = _terms_for_paths(store, group.files, vocabulary or default_vocabulary())
    return sorted(areas.values(), key=lambda g: g.sort_key)


# --------------------------------------------------------------------------- prompt


def _group_prompt(group: Group, repo: str, descriptions: dict[str, list[str]],
                  redaction: RedactionLog) -> tuple[str, set[str]]:
    """The prompt for one area, and the citations it is allowed to use."""
    known: set[str] = set()
    lines = [f"Area: {group.area}"]
    if group.terms:
        lines.append(f"The product calls this: {', '.join(group.terms)}")
    lines.append("")
    for commit in group.commits:
        touched = [path for path in commit.files if path in set(group.files)]
        lines.append(f"commit {commit.sha[:8]} ({commit.date}): {commit.subject}")
        for path in touched[: config.CHANGELOG_MAX_FILES_PER_GROUP]:
            citation = ids.commit_citation(repo, commit.sha, path)
            known.add(citation)
            lines.append(f"  - {path}   citation: {citation}")
            for description in descriptions.get(path, []):
                lines.append(f"      what lives here: {description}")
        lines.append("")
    body = redaction.scrub("\n".join(lines))
    return (
        f"{wrap_retrieved(body)}\n\nSummarise what changed in this area.",
        known,
    )


def _clean_bullets(text: str, known: set[str]) -> list[str]:
    """Bullets whose citation was in the evidence. The rest are dropped.

    Same rule the cartographer applies to impact-map entries: a fabricated
    citation is worse than a missing line (design law 4). Here it can be enforced
    exactly, because the model was told to put one citation on each bullet.
    """
    kept: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith(("- ", "* ", "• ")):
            continue
        line = "- " + line[2:].strip()
        cited = citations_in(line)
        if cited and all(citation in known for citation in cited):
            kept.append(line)
    return kept[:3]


# --------------------------------------------------------------------------- node


def render(report: ChangelogReport, repo: str, sha_label: str) -> str:
    """The answer body. Deterministic given the report."""
    ask = report.ask
    if report.error:
        return (
            f"I could not read the history for {ask.label}: {report.error}\n\n"
            "**Gaps — what I did not find**\n\n"
            "- The indexed repository was not readable from here, so no commits could be "
            "listed. Nothing below is inferred from anywhere else."
        )
    if not report.commits:
        return (
            f"No commits touched this repository in {ask.label}.\n\n"
            "**Gaps — what I did not find**\n\n"
            f"- The range was read from `{repo}` at {sha_label} and came back empty. If you "
            "expected changes there, the index may be pointing at a different checkout."
        )

    if ask.from_sha:
        window = f"{len(report.commits)} commits in {ask.label}"
    elif ask.since:
        window = f"{len(report.commits)} commits from {ask.label}"
    else:
        # The ask named a count; report what git actually returned, which is
        # smaller when the repository is younger than the window requested.
        window = f"The last {len(report.commits)} commits"
    parts = [
        "## What changed, per area",
        f"{window}, grouped by the part of the product they touched. Product areas come "
        f"first, ordered by how much changed in them; build and tooling last.",
    ]
    for group in report.groups:
        heading = f"### {group.area}"
        if group.terms:
            heading += f" — {', '.join(group.terms)}"
        body = group.bullets or [
            f"- {len(group.commits)} commit(s) touched {len(group.files)} file(s) here; no "
            "product-level summary could be written for them. "
            f"`{ids.commit_citation(repo, group.commits[0].sha, group.files[0])}`"
        ]
        parts.append(heading + "\n" + "\n".join(body))

    if report.skipped_areas:
        parts.append(
            "**Other areas touched, not summarised** — "
            + ", ".join(f"`{area}`" for area in report.skipped_areas)
        )
    return "\n\n".join(parts)


def run(state, llm: LLMClient, retriever, ask) -> Any:
    """Populate ``state.narrative`` from the commit range in ``ask``."""
    report = ChangelogReport(ask=ask)
    state.env["changelog"] = ask.as_dict()

    repo_root = getattr(retriever, "repo_root", None)
    if repo_root is None:
        report.error = (
            "the repository is not on disk at the path this index was built from, and "
            "commit history is not stored in the index"
        )
        state.narrative = render(report, retriever.repo, retriever.sha[:12])
        state.env["changelog"]["report"] = report.as_dict()
        return state

    try:
        report.commits = read_commits(Path(repo_root), ask)
    except (OSError, RuntimeError) as exc:
        report.error = str(exc)
        state.narrative = render(report, retriever.repo, retriever.sha[:12])
        state.env["changelog"]["report"] = report.as_dict()
        return state

    groups = group_commits(report.commits, retriever.store, retriever.vocabulary)
    report.groups = groups[: config.CHANGELOG_MAX_GROUPS]
    report.skipped_areas = [g.area for g in groups[config.CHANGELOG_MAX_GROUPS:]]

    if llm.available_for("changelog"):
        system = load_prompt("changelog_group")
        redaction = RedactionLog()
        for group in report.groups:
            if state.budget.exceeded(state.usage):
                break
            descriptions = _describe_paths(retriever.store, group.files, retriever.descriptions)
            prompt, known = _group_prompt(group, retriever.repo, descriptions, redaction)
            try:
                response = llm.complete("changelog", prompt, system=system)
                state.usage.add(_usage_of(response))
                group.bullets = _clean_bullets(response.text, known)
            except Exception:
                state.usage.errors += 1

    state.narrative = render(report, retriever.repo, retriever.sha[:12])
    state.env["changelog"]["report"] = report.as_dict()
    return state
