"""Graph vocabulary: node kinds, edge kinds, confidence levels, and the row dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class NodeKind:
    FILE = "file"
    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    RECORD = "record"
    ANNOTATION = "annotation"
    METHOD = "method"
    ENDPOINT = "endpoint"
    ENTITY = "entity"
    SCREEN = "screen"


TYPE_KINDS = frozenset(
    {NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM, NodeKind.RECORD, NodeKind.ANNOTATION}
)


class EdgeKind:
    # --- structural: declaration facts ---
    CONTAINS = "contains"
    IMPORTS = "imports"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    # --- semantic: behaviour / product-visible relations ---
    CALLS = "calls"
    HANDLES_ROUTE = "handles_route"
    READS_ENTITY = "reads_entity"
    RENDERS = "renders"
    INCLUDES = "includes"
    IMPLEMENTED_BY = "implemented_by"
    ASSOCIATES = "associates"
    MAPS_TO = "maps_to"


# implements (impl -> iface) is STRUCTURAL: it states a declaration fact, and following it
# during impact analysis walks *up* to an abstraction nobody needs to change.
# implemented_by (iface -> impl) is SEMANTIC: it is the dispatch hop. A call resolves to an
# interface method, and the code that actually runs is the impl. Traversal must follow it to
# answer "what breaks if this route changes?". Same edge, inverted; different downstream job.
SEMANTIC_EDGE_KINDS = frozenset(
    {
        EdgeKind.CALLS,
        EdgeKind.HANDLES_ROUTE,
        EdgeKind.READS_ENTITY,
        EdgeKind.RENDERS,
        EdgeKind.INCLUDES,
        EdgeKind.IMPLEMENTED_BY,
        EdgeKind.ASSOCIATES,
        EdgeKind.MAPS_TO,
    }
)

STRUCTURAL_EDGE_KINDS = frozenset(
    {EdgeKind.CONTAINS, EdgeKind.IMPORTS, EdgeKind.EXTENDS, EdgeKind.IMPLEMENTS}
)


class Confidence:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class UnresolvedReason:
    #: receiver was written but its type does not resolve to an in-repo type
    EXTERNAL_RECEIVER = "external_receiver"
    #: receiver's type IS in this repo but the method comes from an external supertype
    EXTERNAL_SUPERTYPE_METHOD = "external_supertype_method"
    #: unqualified call (`foo(...)`) that no in-repo type declares — a static import
    #: or a method inherited from an external supertype. No receiver was written, so
    #: there is no type dependency to record: UNRESOLVED only, never an edge.
    UNQUALIFIED_EXTERNAL = "unqualified_external"
    #: no in-repo type declares a method with this simple name
    NO_CANDIDATE = "no_candidate"
    #: several in-repo types declare it; edges emitted to all of them at low confidence
    AMBIGUOUS = "ambiguous"
    #: a view name that looks like a template but has no matching file
    NO_TEMPLATE = "no_template"


@dataclass(slots=True)
class Node:
    id: str
    kind: str
    name: str
    path: str
    qualified_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    lang: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Edge:
    src: str
    dst: str
    kind: str
    confidence: str = Confidence.HIGH
    path: str | None = None
    line: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Unresolved:
    kind: str  # call | type | template | route
    symbol: str
    reason: str
    src: str | None = None
    path: str | None = None
    line: int | None = None
