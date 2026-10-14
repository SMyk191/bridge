"""Server-rendered templates: `screen` nodes, `renders` and `includes` edges.

Thymeleaf-shaped, but nothing here is Thymeleaf-specific beyond the `th:*`
attribute names — a screen is "a template a handler can return".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import ids
from .model import Confidence, Edge, EdgeKind, Node, NodeKind, Unresolved, UnresolvedReason
from .parse_java import TypeIndex

#: th:replace="~{fragments/layout :: layout (...)}"  /  th:insert="fragments/x :: y"
_TH_INCLUDE = re.compile(r'th:(?:replace|insert|include)\s*=\s*"([^"]*)"')
_TH_FRAGMENT = re.compile(r"th:fragment\s*=")


@dataclass(slots=True)
class ScreensResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    #: logical template name ("owners/ownerDetails") -> screen node id
    screen_ids: dict[str, str] = field(default_factory=dict)


def _logical_name(rel_path: str, template_root: str) -> str:
    relative = rel_path[len(template_root) :].lstrip("/")
    return str(Path(relative).with_suffix(""))


def _include_target(raw: str) -> str | None:
    """`~{fragments/layout :: layout (...)}` -> `fragments/layout`."""
    value = raw.strip()
    if value.startswith("~{"):
        value = value[2:]
    value = value.split("::", 1)[0]
    value = value.split("}", 1)[0].strip()
    if not value or value.startswith("$") or value.startswith("@"):
        return None
    return value


def collect_screens(repo_root: Path) -> ScreensResult:
    """Every template under the configured roots becomes a `screen` node."""
    result = ScreensResult()
    sources: dict[str, tuple[str, str]] = {}  # logical -> (rel_path, text)

    for template_root in config.TEMPLATE_ROOTS:
        root = repo_root / template_root
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in config.TEMPLATE_EXTENSIONS:
                continue
            rel_path = str(path.relative_to(repo_root))
            logical = _logical_name(rel_path, template_root)
            text = path.read_text(encoding="utf-8", errors="replace")
            sources[logical] = (rel_path, text)
            node_id = ids.screen_id(logical)
            result.screen_ids[logical] = node_id
            result.nodes.append(
                Node(
                    id=node_id,
                    kind=NodeKind.SCREEN,
                    name=Path(logical).name,
                    qualified_name=logical,
                    path=rel_path,
                    start_line=1,
                    end_line=text.count("\n") + 1,
                    lang="thymeleaf",
                    attrs={
                        "template_path": rel_path,
                        "is_fragment": logical.startswith("fragments/")
                        or bool(_TH_FRAGMENT.search(text)),
                    },
                )
            )

    for logical, (rel_path, text) in sources.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            for raw in _TH_INCLUDE.findall(line):
                target = _include_target(raw)
                if target is None:
                    continue
                if target not in result.screen_ids:
                    result.unresolved.append(
                        Unresolved(
                            kind="template",
                            symbol=target,
                            reason=UnresolvedReason.NO_TEMPLATE,
                            src=result.screen_ids[logical],
                            path=rel_path,
                            line=line_no,
                        )
                    )
                    continue
                result.edges.append(
                    Edge(
                        src=result.screen_ids[logical],
                        dst=result.screen_ids[target],
                        kind=EdgeKind.INCLUDES,
                        confidence=Confidence.HIGH,
                        path=rel_path,
                        line=line_no,
                    )
                )
    return result


def _is_view_directive(value: str) -> bool:
    return value.startswith(config.VIEW_NAME_NON_TEMPLATE_PREFIXES)


def link_renders(
    index: TypeIndex, screen_ids: dict[str, str], controller_fqns: set[str]
) -> ScreensResult:
    """Java methods -> `renders` -> screens.

    Petclinic hides view names two different ways, so both are followed:
    `return VIEWS_OWNER_CREATE_OR_UPDATE_FORM;` (a constant) and
    `new ModelAndView("owners/ownerDetails")` (a literal that is never returned).

    The two sources get different rules, because they carry different evidence:

    * A **string literal** counts only in a *view position* — a return statement,
      a view-carrier constructor, or a view setter. A literal loose in the body is
      not evidence of rendering: `addFlashAttribute("error", ...)` would otherwise
      manufacture a `renders` edge to `error.html`, and every controller test's
      `view().name("owners/findOwners")` assertion would look like a render.
    * A **constant reference** counts anywhere in the method. `static final String
      VIEWS_X = "owners/x"` is a view name by construction, and it still has to
      match a real template to produce an edge. `high` in a view position,
      `medium` elsewhere.
    """
    result = ScreensResult(screen_ids=dict(screen_ids))
    view_positions = {"return", "view_ctor"}
    for _parsed, jtype in index.types():
        constants = dict(jtype.constants)
        for super_fqn in index.supertype_fqns(jtype.fqn):
            parent = index.by_fqn.get(super_fqn)
            if parent is not None:
                constants.update(parent.constants)

        for method in jtype.methods:
            # (view name, context, via_constant)
            candidates: list[tuple[str, str, str | None]] = [
                (lit.value, lit.context, None)
                for lit in method.string_literals
                if lit.context in view_positions
            ]
            for name in sorted(method.identifiers & constants.keys()):
                context = "return" if name in method.return_identifiers else "other"
                candidates.append((constants[name], context, name))

            emitted: set[str] = set()
            for value, context, via_constant in candidates:
                if _is_view_directive(value):
                    continue
                node_id = screen_ids.get(value)
                if node_id is None:
                    if (
                        context == "return"
                        and jtype.fqn in controller_fqns
                        and method.return_type == "String"
                    ):
                        result.unresolved.append(
                            Unresolved(
                                kind="template",
                                symbol=value,
                                reason=UnresolvedReason.NO_TEMPLATE,
                                src=method.node_id,
                                path=jtype.path,
                                line=method.start_line,
                            )
                        )
                    continue
                if node_id in emitted:
                    continue
                emitted.add(node_id)
                result.edges.append(
                    Edge(
                        src=method.node_id,
                        dst=node_id,
                        kind=EdgeKind.RENDERS,
                        confidence=Confidence.HIGH
                        if context in view_positions
                        else Confidence.MEDIUM,
                        path=jtype.path,
                        line=method.start_line,
                        attrs={
                            "view_name": value,
                            "position": context,
                            "via_constant": via_constant,
                        },
                    )
                )
    return result


def run(repo_root: Path, index: TypeIndex, controller_fqns: set[str]) -> ScreensResult:
    result = collect_screens(repo_root)
    renders = link_renders(index, result.screen_ids, controller_fqns)
    result.edges.extend(renders.edges)
    result.unresolved.extend(renders.unresolved)
    return result
