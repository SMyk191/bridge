"""tree-sitter Java parsing, plus the whole-repo symbol table and call resolver.

This module knows Java and nothing else. No Spring, no JPA, no Thymeleaf — those
live in :mod:`bridge.index.spring` and :mod:`bridge.index.screens`.

The CST is walked with plain recursion rather than tree-sitter queries: the node
type names used here (``method_declaration``, ``field_access``, ...) are stable
across grammar releases in a way that query captures are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from . import ids
from .model import Confidence, Edge, EdgeKind, Unresolved, UnresolvedReason

_LANGUAGE = Language(tree_sitter_java.language())

TYPE_DECL_NODES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

_BODY_NODES = {"class_body", "interface_body", "enum_body", "annotation_type_body", "record_body"}

#: types whose constructor argument is a view name (see StringLit.context)
_VIEW_CARRIER_TYPES = {"ModelAndView"}

#: methods whose argument is a view name
_VIEW_SETTER_METHODS = {"setViewName"}

_JAVA_ESCAPES = {
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    "\\'": "'",
    '\\"': '"',
    "\\\\": "\\",
}


# --------------------------------------------------------------------------- data


@dataclass(slots=True)
class Annotation:
    name: str
    args: dict[str, list[str]] = field(default_factory=dict)
    line: int = 0

    def first(self, *keys: str) -> str | None:
        for key in keys:
            values = self.args.get(key)
            if values:
                return values[0]
        return None

    def all_of(self, *keys: str) -> list[str]:
        out: list[str] = []
        for key in keys:
            out.extend(self.args.get(key, ()))
        return out


@dataclass(slots=True)
class Invocation:
    name: str
    line: int
    arity: int = 0
    receiver: str | None = None
    #: implicit_this | name | field | expr | constructor
    receiver_kind: str = "implicit_this"


@dataclass(slots=True)
class StringLit:
    value: str
    line: int
    #: return | view_ctor | other — drives the confidence of `renders` edges
    context: str = "other"


@dataclass(slots=True)
class JavaField:
    name: str
    type_raw: str
    type_base: str
    type_args: list[str]
    modifiers: list[str]
    annotations: list[Annotation]
    line: int
    constant_value: str | None = None

    def annotation(self, name: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name == name), None)


@dataclass(slots=True)
class JavaMethod:
    name: str
    params: list[tuple[str, str]]  # (base type name, param name)
    return_type: str
    modifiers: list[str]
    annotations: list[Annotation]
    start_line: int
    end_line: int
    is_constructor: bool = False
    invocations: list[Invocation] = field(default_factory=list)
    string_literals: list[StringLit] = field(default_factory=list)
    identifiers: set[str] = field(default_factory=set)
    return_identifiers: set[str] = field(default_factory=set)
    #: param and local variable name -> declared base type name
    local_types: dict[str, str] = field(default_factory=dict)
    #: filled by _assign_method_ids once sibling overloads are known
    node_id: str = ""

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(t for t, _ in self.params)})"

    def annotation(self, name: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name == name), None)

    def has_annotation(self, names: Iterable[str]) -> bool:
        wanted = set(names)
        return any(a.name in wanted for a in self.annotations)


@dataclass(slots=True)
class Supertype:
    """One entry of an ``extends``/``implements`` clause, generics preserved."""

    raw: str
    base: str
    type_args: list[str]
    line: int


@dataclass(slots=True)
class JavaType:
    kind: str  # class | interface | enum | record | annotation
    name: str
    fqn: str
    path: str
    start_line: int
    end_line: int
    modifiers: list[str]
    annotations: list[Annotation]
    extends: list[Supertype] = field(default_factory=list)
    implements: list[Supertype] = field(default_factory=list)
    fields: list[JavaField] = field(default_factory=list)
    methods: list[JavaMethod] = field(default_factory=list)
    #: `static final String NAME = "literal"` -> {"NAME": "literal"}
    constants: dict[str, str] = field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return ids.type_id(self.fqn)

    @property
    def supertypes(self) -> list[Supertype]:
        return [*self.extends, *self.implements]

    def annotation(self, name: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name == name), None)

    def has_annotation(self, names: Iterable[str]) -> bool:
        wanted = set(names)
        return any(a.name in wanted for a in self.annotations)

    def field_named(self, name: str) -> JavaField | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(slots=True)
class ParsedFile:
    path: str  # repo-relative
    package: str
    imports: list[tuple[str, int]]  # (fqn or "pkg.*", line)
    types: list[JavaType]  # top-level and nested, flattened

    @property
    def node_id(self) -> str:
        return ids.file_id(self.path)

    @property
    def wildcard_packages(self) -> list[str]:
        return [imp[:-2] for imp, _ in self.imports if imp.endswith(".*")]


# --------------------------------------------------------------------------- helpers


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _string_value(node: Node, src: bytes) -> str:
    raw = _text(node, src)
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    for escaped, plain in _JAVA_ESCAPES.items():
        raw = raw.replace(escaped, plain)
    return raw


def _base_type_name(node: Node | None, src: bytes) -> str:
    """`List<Pet>` -> `List`, `Pet[]` -> `Pet`, `Map.Entry` -> `Map.Entry`."""
    if node is None:
        return ""
    if node.type == "generic_type":
        inner = node.child_by_field_name("name") or node.children[0] if node.children else None
        return _base_type_name(inner, src) if inner is not None else _text(node, src)
    if node.type == "array_type":
        return _base_type_name(node.child_by_field_name("element"), src)
    if node.type == "annotated_type":
        for child in node.children:
            if child.type != "modifiers":
                return _base_type_name(child, src)
    return _text(node, src).strip()


def _type_arguments(node: Node | None, src: bytes) -> list[str]:
    """Base names of a generic type's arguments: `JpaRepository<Owner, Integer>` -> [Owner, Integer]."""
    if node is None or node.type != "generic_type":
        return []
    args = next((c for c in node.children if c.type == "type_arguments"), None)
    if args is None:
        return []
    return [
        _base_type_name(child, src)
        for child in args.children
        if child.type not in {"<", ">", ",", "wildcard"}
    ]


def _annotation_values(node: Node, src: bytes) -> list[str]:
    if node.type == "string_literal":
        return [_string_value(node, src)]
    if node.type == "element_value_array_initializer":
        out: list[str] = []
        for child in node.children:
            if child.type not in {"{", "}", ","}:
                out.extend(_annotation_values(child, src))
        return out
    return [_text(node, src).strip()]


def _parse_annotation(node: Node, src: bytes) -> Annotation:
    name = _base_type_name(node.child_by_field_name("name"), src).split(".")[-1]
    ann = Annotation(name=name, line=_line(node))
    arg_list = node.child_by_field_name("arguments")
    if arg_list is None:
        return ann
    for child in arg_list.children:
        if child.type in {"(", ")", ","}:
            continue
        if child.type == "element_value_pair":
            key_node = child.child_by_field_name("key")
            val_node = child.child_by_field_name("value")
            if key_node is not None and val_node is not None:
                ann.args.setdefault(_text(key_node, src), []).extend(
                    _annotation_values(val_node, src)
                )
        else:
            ann.args.setdefault("value", []).extend(_annotation_values(child, src))
    return ann


def _parse_modifiers(node: Node | None, src: bytes) -> tuple[list[str], list[Annotation]]:
    if node is None:
        return [], []
    words: list[str] = []
    annotations: list[Annotation] = []
    for child in node.children:
        if child.type in {"marker_annotation", "annotation"}:
            annotations.append(_parse_annotation(child, src))
        else:
            words.append(_text(child, src))
    return words, annotations


def _modifiers_node(node: Node) -> Node | None:
    return next((c for c in node.children if c.type == "modifiers"), None)


def _supertypes_from(node: Node | None, src: bytes) -> list[Supertype]:
    """Read a `superclass` / `super_interfaces` / `extends_interfaces` clause."""
    if node is None:
        return []
    out: list[Supertype] = []
    candidates: list[Node] = []
    for child in node.children:
        if child.type == "type_list":
            candidates.extend(c for c in child.children if c.type != ",")
        elif child.type not in {"extends", "implements", "permits"}:
            candidates.append(child)
    for cand in candidates:
        base = _base_type_name(cand, src)
        if base:
            out.append(
                Supertype(
                    raw=_text(cand, src),
                    base=base.split(".")[-1],
                    type_args=_type_arguments(cand, src),
                    line=_line(cand),
                )
            )
    return out


# --------------------------------------------------------------------------- body walk


def _walk_method_body(node: Node, src: bytes, method: JavaMethod, ctx: str = "other") -> None:
    """Collect invocations, string literals, identifiers and local types from a method body."""
    kind = node.type

    if kind == "return_statement":
        ctx = "return"
    elif kind == "object_creation_expression":
        created = _base_type_name(node.child_by_field_name("type"), src)
        args = node.child_by_field_name("arguments")
        method.invocations.append(
            Invocation(
                name=created.split(".")[-1],
                line=_line(node),
                arity=sum(1 for c in args.children if c.type not in {"(", ")", ","}) if args else 0,
                receiver=created.split(".")[-1],
                receiver_kind="constructor",
            )
        )
        if created.split(".")[-1] in _VIEW_CARRIER_TYPES:
            ctx = "view_ctor"

    elif kind == "method_invocation":
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        args = node.child_by_field_name("arguments")
        receiver: str | None = None
        receiver_kind = "implicit_this"
        if obj is not None:
            if obj.type == "identifier":
                receiver, receiver_kind = _text(obj, src), "name"
            elif obj.type == "field_access":
                target = obj.child_by_field_name("object")
                field_node = obj.child_by_field_name("field")
                if target is not None and target.type == "this" and field_node is not None:
                    receiver, receiver_kind = _text(field_node, src), "field"
                else:
                    receiver, receiver_kind = _text(obj, src), "expr"
            elif obj.type == "this":
                receiver, receiver_kind = None, "implicit_this"
            else:
                receiver, receiver_kind = _text(obj, src), "expr"
        if name_node is not None:
            invoked = _text(name_node, src)
            method.invocations.append(
                Invocation(
                    name=invoked,
                    line=_line(node),
                    arity=sum(1 for c in args.children if c.type not in {"(", ")", ","})
                    if args
                    else 0,
                    receiver=receiver,
                    receiver_kind=receiver_kind,
                )
            )
            if invoked in _VIEW_SETTER_METHODS:
                ctx = "view_ctor"

    elif kind == "local_variable_declaration":
        declared = _base_type_name(node.child_by_field_name("type"), src)
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    method.local_types[_text(name_node, src)] = declared

    elif kind == "string_literal":
        method.string_literals.append(
            StringLit(value=_string_value(node, src), line=_line(node), context=ctx)
        )
        return

    elif kind == "identifier":
        name = _text(node, src)
        method.identifiers.add(name)
        if ctx == "return":
            method.return_identifiers.add(name)
        return

    for child in node.children:
        _walk_method_body(child, src, method, ctx)


def _parse_formal_parameters(node: Node | None, src: bytes) -> list[tuple[str, str]]:
    if node is None:
        return []
    params: list[tuple[str, str]] = []
    for child in node.children:
        if child.type not in {"formal_parameter", "spread_parameter", "receiver_parameter"}:
            continue
        type_node = child.child_by_field_name("type")
        name_node = child.child_by_field_name("name")
        if type_node is None:
            # spread_parameter has no `type` field; its first non-modifier child is the type
            type_node = next((c for c in child.children if c.type != "modifiers"), None)
        base = _base_type_name(type_node, src)
        name = _text(name_node, src) if name_node is not None else ""
        if not name:
            declarator = next((c for c in child.children if c.type == "variable_declarator"), None)
            if declarator is not None:
                inner = declarator.child_by_field_name("name")
                name = _text(inner, src) if inner is not None else ""
        params.append((base, name))
    return params


def _parse_method(node: Node, src: bytes, owner_name: str) -> JavaMethod:
    is_ctor = node.type == "constructor_declaration"
    modifiers, annotations = _parse_modifiers(_modifiers_node(node), src)
    name_node = node.child_by_field_name("name")
    params = _parse_formal_parameters(node.child_by_field_name("parameters"), src)
    method = JavaMethod(
        name=_text(name_node, src) if name_node is not None else owner_name,
        params=params,
        return_type="" if is_ctor else _base_type_name(node.child_by_field_name("type"), src),
        modifiers=modifiers,
        annotations=annotations,
        start_line=_line(node),
        end_line=node.end_point[0] + 1,
        is_constructor=is_ctor,
    )
    method.local_types.update({name: base for base, name in params if name})
    body = node.child_by_field_name("body")
    if body is not None:
        _walk_method_body(body, src, method)
    return method


def _parse_fields(node: Node, src: bytes, jtype: JavaType) -> None:
    modifiers, annotations = _parse_modifiers(_modifiers_node(node), src)
    type_node = node.child_by_field_name("type")
    type_raw = _text(type_node, src) if type_node is not None else ""
    type_base = _base_type_name(type_node, src)
    type_args = _type_arguments(type_node, src)
    is_constant = "static" in modifiers and "final" in modifiers and type_base == "String"
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, src)
        value_node = child.child_by_field_name("value")
        constant_value = None
        if is_constant and value_node is not None and value_node.type == "string_literal":
            constant_value = _string_value(value_node, src)
            jtype.constants[name] = constant_value
        jtype.fields.append(
            JavaField(
                name=name,
                type_raw=type_raw,
                type_base=type_base,
                type_args=type_args,
                modifiers=modifiers,
                annotations=annotations,
                line=_line(node),
                constant_value=constant_value,
            )
        )


def _assign_method_ids(jtype: JavaType) -> None:
    """Bare `#name` when unique in this type; `#name(P1,P2)` for every overload."""
    counts: dict[str, int] = {}
    for method in jtype.methods:
        counts[method.name] = counts.get(method.name, 0) + 1
    for method in jtype.methods:
        if counts[method.name] == 1:
            method.node_id = ids.method_id(jtype.fqn, method.name)
        else:
            method.node_id = ids.method_id(
                jtype.fqn, method.name, [t for t, _ in method.params]
            )


def _parse_type(node: Node, src: bytes, path: str, prefix: str, out: list[JavaType]) -> None:
    kind = TYPE_DECL_NODES[node.type]
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "<anonymous>"
    modifiers, annotations = _parse_modifiers(_modifiers_node(node), src)
    jtype = JavaType(
        kind=kind,
        name=name,
        fqn=f"{prefix}.{name}" if prefix else name,
        path=path,
        start_line=_line(node),
        end_line=node.end_point[0] + 1,
        modifiers=modifiers,
        annotations=annotations,
    )
    superclass = next((c for c in node.children if c.type == "superclass"), None)
    jtype.extends.extend(_supertypes_from(superclass, src))
    if kind == "interface":
        jtype.extends.extend(
            _supertypes_from(
                next((c for c in node.children if c.type == "extends_interfaces"), None), src
            )
        )
    jtype.implements.extend(
        _supertypes_from(next((c for c in node.children if c.type == "super_interfaces"), None), src)
    )

    body = next((c for c in node.children if c.type in _BODY_NODES), None)
    out.append(jtype)
    if body is not None:
        for child in body.children:
            if child.type == "field_declaration":
                _parse_fields(child, src, jtype)
            elif child.type in {"method_declaration", "constructor_declaration"}:
                jtype.methods.append(_parse_method(child, src, name))
            elif child.type in TYPE_DECL_NODES:
                _parse_type(child, src, path, jtype.fqn, out)
    _assign_method_ids(jtype)


def parse_source(path: str, source: bytes) -> ParsedFile:
    """Parse one ``.java`` file into a :class:`ParsedFile`."""
    tree = Parser(_LANGUAGE).parse(source)
    root = tree.root_node
    package = ""
    imports: list[tuple[str, int]] = []
    types: list[JavaType] = []

    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in {"scoped_identifier", "identifier"}:
                    package = _text(sub, source)
        elif child.type == "import_declaration":
            parts = [
                _text(sub, source)
                for sub in child.children
                if sub.type in {"scoped_identifier", "identifier", "asterisk"}
            ]
            if parts:
                fqn = ".".join(parts) if len(parts) > 1 else parts[0]
                if any(sub.type == "asterisk" for sub in child.children) and not fqn.endswith("*"):
                    fqn = f"{fqn}.*"
                imports.append((fqn, _line(child)))
        elif child.type in TYPE_DECL_NODES:
            _parse_type(child, source, path, package, types)

    return ParsedFile(path=path, package=package, imports=imports, types=types)


def parse_file(repo_root, rel_path: str) -> ParsedFile:
    from pathlib import Path

    return parse_source(rel_path, Path(repo_root, rel_path).read_bytes())


# --------------------------------------------------------------------------- symbol table


class TypeIndex:
    """Whole-repo Java symbol table: name resolution and supertype walks."""

    def __init__(self, files: list[ParsedFile]) -> None:
        self.files = files
        self.by_fqn: dict[str, JavaType] = {}
        self.by_simple: dict[str, list[str]] = {}
        self.file_of: dict[str, ParsedFile] = {}
        self.methods_by_name: dict[str, list[tuple[JavaType, JavaMethod]]] = {}

        for parsed in files:
            for jtype in parsed.types:
                self.by_fqn[jtype.fqn] = jtype
                self.by_simple.setdefault(jtype.name, []).append(jtype.fqn)
                self.file_of[jtype.fqn] = parsed
                for method in jtype.methods:
                    self.methods_by_name.setdefault(method.name, []).append((jtype, method))

    # -- resolution ---------------------------------------------------------

    def resolve_type(self, name: str, parsed: ParsedFile | None) -> str | None:
        """Resolve a source-level type name to an in-repo FQN, or None if external."""
        if not name:
            return None
        name = name.split("<")[0].replace("[]", "").strip()
        if name in self.by_fqn:
            return name
        simple = name.split(".")[-1]
        if parsed is not None:
            for imported, _ in parsed.imports:
                if imported.endswith(f".{simple}") and imported in self.by_fqn:
                    return imported
            same_package = f"{parsed.package}.{name}" if parsed.package else name
            if same_package in self.by_fqn:
                return same_package
            for jtype in parsed.types:  # nested type referenced by simple name
                nested = f"{jtype.fqn}.{name}"
                if nested in self.by_fqn:
                    return nested
            for package in parsed.wildcard_packages:
                candidate = f"{package}.{name}"
                if candidate in self.by_fqn:
                    return candidate
        candidates = self.by_simple.get(simple, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    def supertype_fqns(self, fqn: str) -> list[str]:
        """Transitive in-repo supertypes of ``fqn``, nearest first."""
        seen: list[str] = []
        queue = [fqn]
        while queue:
            current = queue.pop(0)
            jtype = self.by_fqn.get(current)
            if jtype is None:
                continue
            parsed = self.file_of.get(current)
            for supertype in jtype.supertypes:
                resolved = self.resolve_type(supertype.base, parsed)
                if resolved and resolved not in seen and resolved != fqn:
                    seen.append(resolved)
                    queue.append(resolved)
        return seen

    def subtype_fqns(self, fqn: str) -> list[str]:
        """Transitive in-repo subtypes — the impls of an interface."""
        return [other for other in self.by_fqn if fqn in self.supertype_fqns(other)]

    def find_method(self, fqn: str, name: str) -> tuple[JavaType, JavaMethod] | None:
        """Look for ``name`` on ``fqn`` then on its in-repo supertypes."""
        for candidate in (fqn, *self.supertype_fqns(fqn)):
            jtype = self.by_fqn.get(candidate)
            if jtype is None:
                continue
            method = next((m for m in jtype.methods if m.name == name), None)
            if method is not None:
                return jtype, method
        return None

    def types(self) -> Iterator[tuple[ParsedFile, JavaType]]:
        for parsed in self.files:
            for jtype in parsed.types:
                yield parsed, jtype


# --------------------------------------------------------------------------- call resolution


class CallResolver:
    """Turns method invocations into `calls` edges, honestly labelled.

    Design law 4: an edge is emitted only for what is actually known. When the
    receiver's type is known but the method is not (it came from an external
    supertype such as ``JpaRepository``), a type-level edge is emitted at low
    confidence *and* an UNRESOLVED row is written — the dependency is real, the
    exact target is not.
    """

    def __init__(self, index: TypeIndex) -> None:
        self.index = index
        self.edges: list[Edge] = []
        self.unresolved: list[Unresolved] = []

    def run(self) -> tuple[list[Edge], list[Unresolved]]:
        for parsed, jtype in self.index.types():
            for method in jtype.methods:
                for call in method.invocations:
                    self._resolve(parsed, jtype, method, call)
        return self.edges, self.unresolved

    # -- internals ----------------------------------------------------------

    def _receiver_fqn(
        self, parsed: ParsedFile, jtype: JavaType, method: JavaMethod, call: Invocation
    ) -> str | None:
        if call.receiver_kind == "constructor":
            return self.index.resolve_type(call.name, parsed)
        if call.receiver_kind == "implicit_this":
            return jtype.fqn
        if call.receiver is None:
            return None
        if call.receiver_kind == "name":
            local = method.local_types.get(call.receiver)
            if local:
                return self.index.resolve_type(local, parsed)
            declared = jtype.field_named(call.receiver)
            if declared is not None:
                return self.index.resolve_type(declared.type_base, parsed)
            return self.index.resolve_type(call.receiver, parsed)  # static call on a type
        if call.receiver_kind == "field":
            declared = jtype.field_named(call.receiver)
            if declared is not None:
                return self.index.resolve_type(declared.type_base, parsed)
        return None

    def _emit(self, src: str, dst: str, path: str, line: int, confidence: str, **attrs) -> None:
        self.edges.append(
            Edge(
                src=src,
                dst=dst,
                kind=EdgeKind.CALLS,
                confidence=confidence,
                path=path,
                line=line,
                attrs=attrs,
            )
        )

    def _resolve(
        self, parsed: ParsedFile, jtype: JavaType, method: JavaMethod, call: Invocation
    ) -> None:
        src_id = method.node_id
        receiver_fqn = self._receiver_fqn(parsed, jtype, method, call)

        if receiver_fqn is not None:
            target_name = "<init>" if call.receiver_kind == "constructor" else call.name
            if call.receiver_kind == "constructor":
                found = next(
                    (
                        (self.index.by_fqn[receiver_fqn], m)
                        for m in self.index.by_fqn[receiver_fqn].methods
                        if m.is_constructor
                    ),
                    None,
                )
                if found is None:
                    # no declared constructor: the type itself is the honest target
                    self._emit(
                        src_id,
                        ids.type_id(receiver_fqn),
                        jtype.path,
                        call.line,
                        Confidence.HIGH,
                        via="implicit_constructor",
                    )
                    return
            else:
                found = self.index.find_method(receiver_fqn, call.name)

            if found is not None:
                owner, target = found
                self._emit(src_id, target.node_id, jtype.path, call.line, Confidence.HIGH)
                self._fan_out_to_impls(src_id, owner, target, jtype.path, call.line)
                return

            if call.receiver_kind == "implicit_this":
                # Nobody wrote a receiver — the type was inferred as the enclosing
                # class. If it declares no such method, this is a static import
                # (`given(...)`, `status()`) or an external inherited method. An edge
                # to the enclosing class would assert that a method depends on its
                # own class: vacuous, and it made every test class a fake hub.
                self.unresolved.append(
                    Unresolved(
                        kind="call",
                        symbol=call.name,
                        reason=UnresolvedReason.UNQUALIFIED_EXTERNAL,
                        src=src_id,
                        path=jtype.path,
                        line=call.line,
                    )
                )
                return

            # Receiver type is in-repo, method is not: it comes from an external
            # supertype (e.g. `owners.save(...)` on JpaRepository). The type
            # dependency is a fact because the receiver was written; the exact
            # method is not. Emit low + record UNRESOLVED.
            self._emit(
                src_id,
                ids.type_id(receiver_fqn),
                jtype.path,
                call.line,
                Confidence.LOW,
                unresolved_method=target_name,
                reason=UnresolvedReason.EXTERNAL_SUPERTYPE_METHOD,
            )
            self.unresolved.append(
                Unresolved(
                    kind="call",
                    symbol=call.name,
                    reason=UnresolvedReason.EXTERNAL_SUPERTYPE_METHOD,
                    src=src_id,
                    path=jtype.path,
                    line=call.line,
                )
            )
            return

        # Receiver type unknown — fall back to a global lookup by simple name.
        candidates = [
            (owner, target)
            for owner, target in self.index.methods_by_name.get(call.name, [])
            if not target.is_constructor
        ]
        if not candidates:
            self.unresolved.append(
                Unresolved(
                    kind="call",
                    symbol=call.name,
                    reason=UnresolvedReason.EXTERNAL_RECEIVER,
                    src=src_id,
                    path=jtype.path,
                    line=call.line,
                )
            )
            return

        if len(candidates) == 1:
            owner, target = candidates[0]
            self._emit(
                src_id, target.node_id, jtype.path, call.line, Confidence.MEDIUM, via="simple_name"
            )
            self._fan_out_to_impls(src_id, owner, target, jtype.path, call.line)
            return

        for owner, target in candidates:
            self._emit(
                src_id,
                target.node_id,
                jtype.path,
                call.line,
                Confidence.LOW,
                via="simple_name",
                reason=UnresolvedReason.AMBIGUOUS,
                candidates=len(candidates),
            )
        self.unresolved.append(
            Unresolved(
                kind="call",
                symbol=call.name,
                reason=UnresolvedReason.AMBIGUOUS,
                src=src_id,
                path=jtype.path,
                line=call.line,
            )
        )

    def _fan_out_to_impls(
        self, src_id: str, owner: JavaType, target: JavaMethod, path: str, line: int
    ) -> None:
        """A call landing on an interface method also reaches every in-repo impl.

        Unexercised by spring-petclinic (its repository interfaces have no in-repo
        implementations); kept because it is the dispatch hop real codebases need.
        """
        if owner.kind != "interface":
            return
        for impl_fqn in self.index.subtype_fqns(owner.fqn):
            impl = self.index.by_fqn[impl_fqn]
            if impl.kind == "interface":
                continue
            override = next((m for m in impl.methods if m.name == target.name), None)
            if override is None:
                continue
            self._emit(
                src_id,
                override.node_id,
                path,
                line,
                Confidence.MEDIUM,
                via="interface_impl",
                interface=owner.fqn,
            )
