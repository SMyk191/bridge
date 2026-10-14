"""Which project a file belongs to, from the build files that already say so.

A monorepo is not one thing. "Where do we validate a phone number?" has a
different answer in `services/billing` than in `apps/web`, and an impact map that
mixes them is telling a product person that a change lands in both. Every chunk
therefore carries a `project`, resolved from whichever build file claims its
directory.

Six detectors, all reading files a repository already maintains — nothing here
asks anyone to write new metadata for Bridge's benefit:

    pom.xml <modules>       Maven
    settings.gradle(.kts)   Gradle
    package.json workspaces npm/yarn/pnpm
    go.work                 Go
    BUILD / WORKSPACE       Bazel
    pyproject.toml          Python (per directory)

Resolution is **longest matching root wins**, so a nested module beats the
repository root that contains it. A file under no declared project falls back to
a synthetic root project, because "unassigned" is not a useful answer to give a
reader — but that fallback is visible in the projects table rather than silent.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

MAVEN = "maven"
GRADLE = "gradle"
NPM = "npm"
GO = "go"
BAZEL = "bazel"
PYTHON = "python"
ROOT = "root"

#: `<module>name</module>` — parsed with a regex rather than an XML library on
#: purpose: a pom can carry namespaces, comments and profiles that make a
#: DOM walk fussy, and the only thing wanted here is the module list.
_MAVEN_MODULE = re.compile(r"<module>\s*([^<]+?)\s*</module>", re.IGNORECASE)
#: `include 'a:b'` / `include(":a:b")` in settings.gradle(.kts)
_GRADLE_INCLUDE = re.compile(r"""include\s*[(\s]\s*['"]([^'"]+)['"]""")


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    repo: str
    root_path: str
    name: str
    kind: str

    def as_row(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "root_path": self.root_path,
            "name": self.name,
            "kind": self.kind,
        }


def _make(repo: str, root_path: str, name: str, kind: str) -> Project:
    root_path = root_path.strip("/")
    return Project(
        id=f"{repo}:{root_path or '.'}", repo=repo, root_path=root_path, name=name, kind=kind
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- detectors


def _maven(repo_root: Path, repo: str) -> list[Project]:
    found: list[Project] = []
    for pom in repo_root.rglob("pom.xml"):
        if any(part in {"target", "node_modules", ".git"} for part in pom.parts):
            continue
        rel = str(pom.parent.relative_to(repo_root)).replace("\\", "/")
        rel = "" if rel == "." else rel
        found.append(_make(repo, rel, pom.parent.name, MAVEN))
        # A parent pom's <modules> are directories relative to it; they usually
        # have their own pom.xml and get picked up by the walk anyway, but a
        # module without one still deserves a row.
        for module in _MAVEN_MODULE.findall(_read(pom)):
            child = f"{rel}/{module}".strip("/")
            found.append(_make(repo, child, Path(child).name, MAVEN))
    return found


def _gradle(repo_root: Path, repo: str) -> list[Project]:
    found: list[Project] = []
    for name in ("settings.gradle", "settings.gradle.kts"):
        settings = repo_root / name
        if not settings.is_file():
            continue
        base = str(settings.parent.relative_to(repo_root)).replace("\\", "/")
        base = "" if base == "." else base
        found.append(_make(repo, base, repo_root.name, GRADLE))
        for include in _GRADLE_INCLUDE.findall(_read(settings)):
            # Gradle paths are colon-separated: ":app:core" -> "app/core"
            child = f"{base}/{include.strip(':').replace(':', '/')}".strip("/")
            found.append(_make(repo, child, Path(child).name, GRADLE))
    return found


def _npm(repo_root: Path, repo: str) -> list[Project]:
    found: list[Project] = []
    for manifest in repo_root.rglob("package.json"):
        if "node_modules" in manifest.parts or ".git" in manifest.parts:
            continue
        try:
            data = json.loads(_read(manifest) or "{}")
        except json.JSONDecodeError:
            continue
        rel = str(manifest.parent.relative_to(repo_root)).replace("\\", "/")
        rel = "" if rel == "." else rel
        found.append(_make(repo, rel, str(data.get("name") or manifest.parent.name), NPM))

        workspaces = data.get("workspaces")
        # npm allows both ["packages/*"] and {"packages": ["packages/*"]}
        patterns = workspaces.get("packages", []) if isinstance(workspaces, dict) else (workspaces or [])
        for pattern in patterns:
            for match in sorted(manifest.parent.glob(pattern)):
                if not match.is_dir():
                    continue
                child = str(match.relative_to(repo_root)).replace("\\", "/")
                found.append(_make(repo, child, match.name, NPM))
    return found


def _go(repo_root: Path, repo: str) -> list[Project]:
    work = repo_root / "go.work"
    if not work.is_file():
        return []
    found = [_make(repo, "", repo_root.name, GO)]
    for line in _read(work).splitlines():
        entry = line.strip().strip("()").strip()
        if entry.startswith("./"):
            child = entry[2:].strip()
            if child:
                found.append(_make(repo, child, Path(child).name, GO))
    return found


def _bazel(repo_root: Path, repo: str) -> list[Project]:
    if not any((repo_root / marker).is_file() for marker in ("WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel")):
        return []
    found = [_make(repo, "", repo_root.name, BAZEL)]
    for build in repo_root.rglob("BUILD*"):
        if not build.is_file() or build.name not in {"BUILD", "BUILD.bazel"}:
            continue
        rel = str(build.parent.relative_to(repo_root)).replace("\\", "/")
        if rel != ".":
            found.append(_make(repo, rel, build.parent.name, BAZEL))
    return found


def _python(repo_root: Path, repo: str) -> list[Project]:
    found: list[Project] = []
    for manifest in repo_root.rglob("pyproject.toml"):
        if any(part in {".venv", "node_modules", ".git"} for part in manifest.parts):
            continue
        try:
            data = tomllib.loads(_read(manifest) or "")
        except tomllib.TOMLDecodeError:
            continue
        rel = str(manifest.parent.relative_to(repo_root)).replace("\\", "/")
        rel = "" if rel == "." else rel
        name = (data.get("project") or {}).get("name") or manifest.parent.name
        found.append(_make(repo, rel, str(name), PYTHON))
    return found


_DETECTORS = (_maven, _gradle, _npm, _go, _bazel, _python)


# --------------------------------------------------------------------------- api


def detect(repo_root: Path, repo: str) -> list[Project]:
    """Every project in the repository, deduplicated, deepest root last.

    A directory can be claimed by more than one build system (a Gradle module
    that also has a `package.json` for its front-end assets). The first detector
    to claim a root wins, in `_DETECTORS` order, so the answer is stable rather
    than dependent on filesystem walk order.
    """
    seen: dict[str, Project] = {}
    for detector in _DETECTORS:
        for project in detector(repo_root, repo):
            seen.setdefault(project.root_path, project)
    if not seen:
        # Not an error: plenty of repositories declare nothing. A single root
        # project is the honest description of that, and it keeps `chunks.project`
        # non-empty for every chunk.
        root = _make(repo, "", repo_root.name, ROOT)
        seen[root.root_path] = root
    return sorted(seen.values(), key=lambda p: (len(p.root_path), p.root_path))


class ProjectIndex:
    """Resolves a path to its project. Longest matching root wins."""

    def __init__(self, projects: list[Project]) -> None:
        self.projects = projects
        # longest first, so the first match is the most specific one
        self._by_depth = sorted(projects, key=lambda p: -len(p.root_path))

    def of(self, rel_path: str) -> str:
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        for project in self._by_depth:
            root = project.root_path
            if not root or rel_path == root or rel_path.startswith(root + "/"):
                return project.id
        return self.projects[0].id if self.projects else ""

    def __len__(self) -> int:
        return len(self.projects)
