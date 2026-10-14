"""The Spring/JPA layer: routes, entities, associations, repositories, hierarchy.

Everything here reads :class:`~bridge.index.parse_java.ParsedFile` /
:class:`~bridge.index.parse_java.TypeIndex` and emits graph rows. Where a
framework convention cannot be pinned down, an UNRESOLVED row is written instead
of a guessed edge (CLAUDE.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import config
from . import ids
from .model import Confidence, Edge, EdgeKind, Node, NodeKind, Unresolved, UnresolvedReason
from .parse_java import JavaType, ParsedFile, TypeIndex

_CRITERIA_SPLIT = re.compile(r"(?:And|Or)(?=[A-Z])")


@dataclass(slots=True)
class SpringResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    #: entity FQN -> entity node id, for other passes
    entity_ids: dict[str, str] = field(default_factory=dict)
    #: FQNs of types annotated @Controller/@RestController
    controller_fqns: set[str] = field(default_factory=set)

    def extend(self, other: "SpringResult") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.unresolved.extend(other.unresolved)
        self.entity_ids.update(other.entity_ids)
        self.controller_fqns |= other.controller_fqns


# --------------------------------------------------------------------------- routes


def join_route(class_path: str | None, method_path: str | None) -> str:
    """`/owners/{ownerId}` + `/pets/new` -> `/owners/{ownerId}/pets/new`."""
    segments = [part.strip("/") for part in (class_path, method_path) if part]
    joined = "/".join(part for part in segments if part)
    return f"/{joined}" if joined else "/"


def _mapping_paths(annotation) -> list[str]:
    paths = annotation.all_of("value", "path")
    return paths or [""]


def _http_methods(annotation) -> list[str]:
    implied = config.SPRING_MAPPING_ANNOTATIONS.get(annotation.name)
    if implied is not None:
        return [implied]
    declared = annotation.all_of("method")
    if not declared:
        return ["ANY"]
    # `RequestMethod.GET` / `{RequestMethod.GET, RequestMethod.POST}`
    return [value.split(".")[-1].strip("{} ") for value in declared]


def extract_endpoints(index: TypeIndex) -> SpringResult:
    result = SpringResult()
    for _parsed, jtype in index.types():
        if not jtype.has_annotation(config.SPRING_CONTROLLER_ANNOTATIONS):
            continue
        result.controller_fqns.add(jtype.fqn)
        class_mapping = jtype.annotation("RequestMapping")
        class_paths = _mapping_paths(class_mapping) if class_mapping else [""]

        for method in jtype.methods:
            for annotation in method.annotations:
                if annotation.name not in config.SPRING_MAPPING_ANNOTATIONS:
                    continue
                for http_method in _http_methods(annotation):
                    for class_path in class_paths:
                        for method_path in _mapping_paths(annotation):
                            route = join_route(class_path, method_path)
                            node_id = ids.endpoint_id(http_method, route)
                            result.nodes.append(
                                Node(
                                    id=node_id,
                                    kind=NodeKind.ENDPOINT,
                                    name=route,
                                    qualified_name=f"{http_method} {route}",
                                    path=jtype.path,
                                    start_line=method.start_line,
                                    end_line=method.end_line,
                                    attrs={
                                        "http_method": http_method,
                                        "route": route,
                                        "class_path": class_path or None,
                                        "method_path": method_path or None,
                                        "handler": method.node_id,
                                        "controller": jtype.fqn,
                                    },
                                )
                            )
                            result.edges.append(
                                Edge(
                                    src=method.node_id,
                                    dst=node_id,
                                    kind=EdgeKind.HANDLES_ROUTE,
                                    confidence=Confidence.HIGH,
                                    path=jtype.path,
                                    line=annotation.line,
                                    attrs={"annotation": annotation.name},
                                )
                            )
    return result


# --------------------------------------------------------------------------- entities


def _table_name(jtype: JavaType) -> str:
    table = jtype.annotation("Table")
    named = table.first("name") if table else None
    return named or jtype.name.lower()


def extract_entities(index: TypeIndex) -> SpringResult:
    """`@Entity` types become entity nodes. `@MappedSuperclass` deliberately does not."""
    result = SpringResult()
    for _parsed, jtype in index.types():
        if jtype.annotation("Entity") is None:
            continue
        node_id = ids.entity_id(jtype.fqn)
        result.entity_ids[jtype.fqn] = node_id
        columns = []
        for jfield in jtype.fields:
            if "static" in jfield.modifiers:
                continue
            column = jfield.annotation("Column")
            columns.append(
                {
                    "field": jfield.name,
                    "column": (column.first("name") if column else None) or jfield.name,
                    "type": jfield.type_raw,
                }
            )
        result.nodes.append(
            Node(
                id=node_id,
                kind=NodeKind.ENTITY,
                name=jtype.name,
                qualified_name=jtype.fqn,
                path=jtype.path,
                start_line=jtype.start_line,
                end_line=jtype.end_line,
                attrs={"table": _table_name(jtype), "columns": columns, "java_type": jtype.fqn},
            )
        )
        result.edges.append(
            Edge(
                src=jtype.node_id,
                dst=node_id,
                kind=EdgeKind.MAPS_TO,
                confidence=Confidence.HIGH,
                path=jtype.path,
                line=jtype.start_line,
                attrs={"table": _table_name(jtype)},
            )
        )
    return result


def _association_target(jfield, parsed: ParsedFile, index: TypeIndex) -> str | None:
    """Unwrap `List<Pet>` / `Set<Visit>` to the associated type's FQN."""
    if jfield.type_base in config.JPA_COLLECTION_TYPES and jfield.type_args:
        candidate = jfield.type_args[0]
    else:
        candidate = jfield.type_base
    return index.resolve_type(candidate, parsed)


def extract_associations(index: TypeIndex, entity_ids: dict[str, str]) -> SpringResult:
    """`@OneToMany`/`@ManyToOne`/`@ManyToMany`/`@OneToOne` -> `associates` edges.

    JPA maps most relationships on one side only (petclinic's `Owner.pets` has no
    `Pet.owner` counterpart), but the foreign key is real in both directions, so
    the inverse edge is emitted too — flagged `direction: "inverse"` so a consumer
    can tell which side actually declared it.
    """
    result = SpringResult()
    for parsed, jtype in index.types():
        if jtype.fqn not in entity_ids:
            continue
        for jfield in jtype.fields:
            association = next(
                (a for a in jfield.annotations if a.name in config.JPA_ASSOCIATION_INVERSE), None
            )
            if association is None:
                continue
            target_fqn = _association_target(jfield, parsed, index)
            if target_fqn is None or target_fqn not in entity_ids:
                result.unresolved.append(
                    Unresolved(
                        kind="type",
                        symbol=jfield.type_raw,
                        reason=UnresolvedReason.NO_CANDIDATE,
                        src=ids.entity_id(jtype.fqn),
                        path=jtype.path,
                        line=jfield.line,
                    )
                )
                continue

            join_column = jfield.annotation("JoinColumn")
            shared = {
                "field": jfield.name,
                "join_column": join_column.first("name") if join_column else None,
                "mapped_by": association.first("mappedBy"),
            }
            result.edges.append(
                Edge(
                    src=entity_ids[jtype.fqn],
                    dst=entity_ids[target_fqn],
                    kind=EdgeKind.ASSOCIATES,
                    confidence=Confidence.HIGH,
                    path=jtype.path,
                    line=jfield.line,
                    attrs={**shared, "cardinality": association.name, "direction": "declared"},
                )
            )
            result.edges.append(
                Edge(
                    src=entity_ids[target_fqn],
                    dst=entity_ids[jtype.fqn],
                    kind=EdgeKind.ASSOCIATES,
                    confidence=Confidence.HIGH,
                    path=jtype.path,
                    line=jfield.line,
                    attrs={
                        **shared,
                        "cardinality": config.JPA_ASSOCIATION_INVERSE[association.name],
                        "direction": "inverse",
                        "via_field": f"{jtype.name}.{jfield.name}",
                    },
                )
            )
    return result


# --------------------------------------------------------------------------- repositories


def parse_derived_query(method_name: str) -> list[str] | None:
    """`findByLastNameStartingWith` -> `["lastName"]`. None if not a derived query."""
    for prefix in config.SPRING_QUERY_PREFIXES:
        if not method_name.startswith(prefix):
            continue
        remainder = method_name[len(prefix) :]
        marker = remainder.find("By")
        if marker < 0:
            return None
        criteria = remainder[marker + 2 :]
        if not criteria:
            return []
        properties = []
        for part in _CRITERIA_SPLIT.split(criteria):
            for keyword in config.SPRING_QUERY_KEYWORDS:
                if part.endswith(keyword) and len(part) > len(keyword):
                    part = part[: -len(keyword)]
                    break
            if part:
                properties.append(part[0].lower() + part[1:])
        return properties
    return None


def repository_domain_type(jtype: JavaType, index: TypeIndex) -> str | None:
    """The entity type argument of the Spring Data base interface, if this is a repository.

    Looks at *declared* supertype names rather than resolved ones: `JpaRepository`
    lives outside the repo, so it never appears in the in-repo supertype closure.
    """
    for candidate in (jtype.fqn, *index.supertype_fqns(jtype.fqn)):
        current = index.by_fqn.get(candidate)
        if current is None:
            continue
        for supertype in current.supertypes:
            if supertype.base in config.SPRING_REPOSITORY_BASES and supertype.type_args:
                return supertype.type_args[0]
    return None


def extract_repositories(index: TypeIndex, entity_ids: dict[str, str]) -> SpringResult:
    result = SpringResult()
    for parsed, jtype in index.types():
        if jtype.kind != "interface":
            continue
        domain_name = repository_domain_type(jtype, index)
        if domain_name is None:
            continue
        domain_fqn = index.resolve_type(domain_name, parsed)
        if domain_fqn is None or domain_fqn not in entity_ids:
            result.unresolved.append(
                Unresolved(
                    kind="type",
                    symbol=domain_name,
                    reason=UnresolvedReason.NO_CANDIDATE,
                    src=jtype.node_id,
                    path=jtype.path,
                    line=jtype.start_line,
                )
            )
            continue
        entity_node = entity_ids[domain_fqn]

        result.edges.append(
            Edge(
                src=jtype.node_id,
                dst=entity_node,
                kind=EdgeKind.READS_ENTITY,
                confidence=Confidence.HIGH,
                path=jtype.path,
                line=jtype.start_line,
                attrs={"domain_type": domain_fqn, "via": "repository_generic"},
            )
        )

        for method in jtype.methods:
            properties = parse_derived_query(method.name)
            if method.annotation("Query") is not None:
                query_kind = "query_annotation"
            elif properties is not None:
                query_kind = "derived"
            else:
                query_kind = "inherited_name"
            result.edges.append(
                Edge(
                    src=method.node_id,
                    dst=entity_node,
                    kind=EdgeKind.READS_ENTITY,
                    confidence=Confidence.HIGH,
                    path=jtype.path,
                    line=method.start_line,
                    attrs={
                        "domain_type": domain_fqn,
                        "query_kind": query_kind,
                        "properties": properties or [],
                        "repository": jtype.fqn,
                    },
                )
            )
    return result


# --------------------------------------------------------------------------- hierarchy


def extract_hierarchy(index: TypeIndex) -> SpringResult:
    """`extends` / `implements` for in-repo supertypes, plus the `implemented_by` inverse."""
    result = SpringResult()
    for parsed, jtype in index.types():
        for supertype, kind in (
            *((s, EdgeKind.EXTENDS) for s in jtype.extends),
            *((s, EdgeKind.IMPLEMENTS) for s in jtype.implements),
        ):
            resolved = index.resolve_type(supertype.base, parsed)
            if resolved is None:
                continue  # external supertype: a fact about the framework, not a gap
            result.edges.append(
                Edge(
                    src=jtype.node_id,
                    dst=ids.type_id(resolved),
                    kind=kind,
                    confidence=Confidence.HIGH,
                    path=jtype.path,
                    line=supertype.line,
                    attrs={"raw": supertype.raw},
                )
            )

        for super_fqn in index.supertype_fqns(jtype.fqn):
            parent = index.by_fqn.get(super_fqn)
            if parent is None or parent.kind != "interface" or jtype.kind == "interface":
                continue
            result.edges.append(
                Edge(
                    src=parent.node_id,
                    dst=jtype.node_id,
                    kind=EdgeKind.IMPLEMENTED_BY,
                    confidence=Confidence.HIGH,
                    path=jtype.path,
                    line=jtype.start_line,
                    attrs={"impl": jtype.fqn},
                )
            )
    return result


def run(index: TypeIndex) -> SpringResult:
    """All Spring/JPA passes, in dependency order."""
    result = SpringResult()
    result.extend(extract_endpoints(index))
    entities = extract_entities(index)
    result.extend(entities)
    result.extend(extract_associations(index, entities.entity_ids))
    result.extend(extract_repositories(index, entities.entity_ids))
    result.extend(extract_hierarchy(index))
    return result
