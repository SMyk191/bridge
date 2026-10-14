"""Access control: does this principal get to ask questions about this repo.

`config/acl.<repo>.yaml` **mirrors** repo read permission from the git host —
mirrors in the sense of holding the same shape a real integration would
populate (`readable_by: [team-a, team-b]`), not in the sense of calling one.
:class:`GitHubACLProvider` below is the documented seam for a live
integration; :class:`StaticACLProvider` is what actually runs today, and is
not a placeholder to be embarrassed about — a config file a person edits is a
perfectly real access-control mechanism, just not a self-updating one.

**"Principal" is the same string as `orchestrator/quotas.py`'s "team"**,
deliberately. Bridge has no login system (api/main.py's own docstring: a
single-frontend dev tool), so there is exactly one caller-declared identity
concept in the whole system today, and inventing a second name for it would
imply a distinction that doesn't exist yet. `RunRequest.team` is that identity
for both cost attribution and access control.

**Routing only over readable projects** (the phase's own wording) is, today,
routing only over readable REPOS: the legacy SQLite retrieval path (still
what answers every question — see PROGRESS.md's running Phase E/F/G
carry-forward) has no per-project scoping at all, one index directory per
commit, no `project` column on a chunk. So repo-level is the finest grain
enforcement can actually mean anything at, and it is enforced at the one
correct point: before a run starts (`api/main.py::create_run`), not
statistically-probably-correct filtering applied after the fact.
:func:`filter_unauthorized_chunks` exists anyway, doing nothing today, so the
enforcement POINT for a future per-project ACL is already in the call graph
once Phase H's routing gives chunks a project to check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config


@dataclass(slots=True)
class AclPolicy:
    repo: str
    readable_by: list[str] = field(default_factory=list)

    def allows(self, principal: str) -> bool:
        return principal in self.readable_by


@dataclass(slots=True)
class AccessDecision:
    allowed: bool
    principal: str
    repo: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "principal": self.principal,
            "repo": self.repo, "reason": self.reason,
        }


class GitHostACLProvider(ABC):
    """What a live integration would have to implement.

    A real one calls the git host's own permission API — GitHub's
    `GET /repos/{owner}/{repo}/collaborators/{username}/permission`, or a team
    membership check — and should CACHE the result with a short TTL rather
    than calling it on every question; nothing here prescribes that caching,
    but any implementation MUST have one, or access control becomes the
    slowest and most rate-limited part of every run.
    """

    @abstractmethod
    def can_read(self, principal: str, repo: str) -> bool | None:
        """True/False when this provider has an opinion; None when it has no
        knowledge of `repo` at all — callers treat None as "defer to the
        default policy" (`config.ACL_DEFAULT_OPEN`), never as False."""


class GitHubACLProvider(GitHostACLProvider):
    """Not implemented. Documents exactly what plugging in the real thing
    needs, the same way `db/vectorstore.py::ExternalVectorStore` does for a
    dedicated vector engine.
    """

    REQUIREMENTS = (
        "a GitHub App installation token or PAT with `repo` (or read-only "
        "equivalent) scope for the org being indexed",
        "GET /repos/{owner}/{repo}/collaborators/{principal}/permission (or, "
        "for team-based access, GET /orgs/{org}/teams/{team}/repos/{owner}/{repo}) "
        "mapped onto `principal` — GitHub has no built-in concept of the "
        "caller-declared `team` string this system uses today, so a real "
        "integration also needs a principal -> GitHub identity mapping",
        "a short-TTL cache in front of the API call — this is asked on every "
        "run, and GitHub's permission endpoints are neither free nor fast "
        "enough to call uncached per question",
    )

    def can_read(self, principal: str, repo: str) -> bool | None:
        raise NotImplementedError(
            "GitHubACLProvider is a stub. To implement it: " + "; ".join(self.REQUIREMENTS)
        )


class StaticACLProvider(GitHostACLProvider):
    """`config/acl.<repo>.yaml`, loaded and cached per repo name.

    A repo with NO acl file is open to every principal (`config.
    ACL_DEFAULT_OPEN`) — matching how `config/vocab.<repo>.yaml` being absent
    means no vocabulary rather than no access; an ACL file's role is to
    NARROW access, not to be a precondition for a repo answering questions at
    all.
    """

    def __init__(self, config_root: Path | None = None) -> None:
        self.config_root = config_root or config.CONFIG_ROOT
        self._cache: dict[str, AclPolicy | None] = {}

    def _load(self, repo: str) -> AclPolicy | None:
        if repo in self._cache:
            return self._cache[repo]
        import yaml

        path = self.config_root / f"acl.{repo}.yaml"
        policy = None
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            policy = AclPolicy(repo=data.get("repo", repo), readable_by=list(data.get("readable_by") or []))
        self._cache[repo] = policy
        return policy

    def can_read(self, principal: str, repo: str) -> bool | None:
        policy = self._load(repo)
        if policy is None:
            return None  # no file at all — defer to the default policy
        return policy.allows(principal)


def check_access(provider: GitHostACLProvider, principal: str, repo: str) -> AccessDecision:
    """The one function callers use — folds a provider's True/False/None
    answer together with `config.ACL_DEFAULT_OPEN` into a single decision with
    a human-readable reason, so the audit log and a 403 body always say WHY."""
    verdict = provider.can_read(principal, repo)
    if verdict is None:
        allowed = config.ACL_DEFAULT_OPEN
        reason = (
            f"no ACL policy for {repo!r}; default is "
            f"{'open' if config.ACL_DEFAULT_OPEN else 'closed'}"
        )
    elif verdict:
        allowed = True
        reason = f"{principal!r} is listed in the ACL policy for {repo!r}"
    else:
        allowed = False
        reason = f"{principal!r} is not listed in the ACL policy for {repo!r}"
    return AccessDecision(allowed=allowed, principal=principal, repo=repo, reason=reason)


def filter_unauthorized_chunks(chunks: list[Any], allowed_projects: set[str] | None) -> list[Any]:
    """Defense in depth for a per-PROJECT ACL — a no-op today.

    `allowed_projects=None` means "no project-level restriction is known,"
    which is the only case this can ever be called with right now: the legacy
    SQLite retrieval path chunks carry no `project` attribute at all (one
    index directory per commit, not per project — see the module docstring).
    Once Phase H's routing tags a chunk with the project it came from, this
    becomes a real filter with no other code needing to change; that is the
    whole reason it exists ahead of having anything to do.
    """
    if allowed_projects is None:
        return chunks
    return [c for c in chunks if getattr(c, "project", None) in allowed_projects]


_DEFAULT_PROVIDER: StaticACLProvider | None = None


def default_provider() -> StaticACLProvider:
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = StaticACLProvider()
    return _DEFAULT_PROVIDER
