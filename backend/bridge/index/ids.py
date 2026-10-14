"""Node id construction and parsing.

Deliberately imports nothing from its siblings so every other index module can
depend on it without a cycle.

Id grammar::

    file:<repo-relative path>
    java:<fully.qualified.TypeName>
    java:<fully.qualified.TypeName>#<method>
    java:<fully.qualified.TypeName>#<method>(<ParamType,ParamType>)
    endpoint:<HTTP_METHOD> <route>
    entity:<fully.qualified.EntityName>
    screen:<template path without extension>

The two method forms exist because a bare ``#name`` cannot distinguish overloads.
See :func:`method_id` for when each is used.
"""

from __future__ import annotations

FILE = "file"
JAVA = "java"
ENDPOINT = "endpoint"
ENTITY = "entity"
SCREEN = "screen"


def file_id(rel_path: str) -> str:
    return f"{FILE}:{rel_path}"


def type_id(fqn: str) -> str:
    return f"{JAVA}:{fqn}"


def method_id(owner_fqn: str, name: str, param_types: list[str] | None = None) -> str:
    """Id for a method.

    ``param_types`` is passed only when the owning type declares more than one
    method with this name; the caller decides, because only it can see the
    sibling declarations. When passed, the erased simple type names are appended
    so that ``findAll()`` and ``findAll(Pageable)`` stay distinct nodes.

    Note this makes the id shape SHA-relative for overloaded names: adding an
    overload in a later commit changes the existing sibling's id from
    ``#findAll`` to ``#findAll()``. Recorded in PROGRESS.md.
    """
    base = f"{JAVA}:{owner_fqn}#{name}"
    if param_types is None:
        return base
    return f"{base}({','.join(param_types)})"


def endpoint_id(http_method: str, route: str) -> str:
    return f"{ENDPOINT}:{http_method} {route}"


def entity_id(fqn: str) -> str:
    return f"{ENTITY}:{fqn}"


def screen_id(logical_name: str) -> str:
    return f"{SCREEN}:{logical_name}"


def scheme_of(node_id: str) -> str:
    """The prefix before the first ``:`` — ``file``, ``java``, ``entity``, ..."""
    return node_id.split(":", 1)[0]


def owner_type_of(method_node_id: str) -> str | None:
    """``java:a.b.C#m(X)`` -> ``java:a.b.C``. None if not a method id."""
    if not method_node_id.startswith(f"{JAVA}:") or "#" in method_node_id[:5]:
        return None
    if "#" not in method_node_id:
        return None
    return method_node_id.split("#", 1)[0]


def citation(repo: str, sha: str, path: str, start: int | None, end: int | None) -> str:
    """The citation format every downstream claim must carry: ``repo@sha:path:start-end``."""
    if start is None:
        return f"{repo}@{sha}:{path}"
    if end is None or end == start:
        return f"{repo}@{sha}:{path}:{start}"
    return f"{repo}@{sha}:{path}:{start}-{end}"


def commit_citation(repo: str, commit_sha: str, path: str, *, short: int = 8) -> str:
    """A changelog claim's citation: ``repo@<commit>:path``.

    Same shape as :func:`citation` minus the line range, and deliberately so — a
    changelog claim is about *a commit touching a file*, not about lines that are
    still there. The sha names the commit that made the change, not the indexed
    HEAD, so the two citation kinds are never confused for each other.
    """
    return f"{repo}@{commit_sha[:short]}:{path}"
