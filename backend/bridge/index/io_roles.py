"""Does this node read the world, or write to it? Recorded on every node at index time.

Confidence used to be whatever the cartographer said it was. Making it
deterministic (orchestrator/confidence.py) needs one fact the graph did not
carry: whether a cited piece of code is on the **read** side of a flow or the
**write** side. "A visit isn't appearing on the owner page" cannot be answered
`high` from the display path alone — if nothing in the answer touches the code
that *saves* a visit, half the flow is unexamined, and that is a property of the
citations, not a judgement call.

The role is derived, in this precedence order, from the strongest evidence
available:

1. **an endpoint's HTTP method** — `POST /owners/{id}/pets/new` writes, full stop;
2. **the route a method handles** — a handler inherits its endpoint's role, which
   is why `processCreationForm` is a write even though its name says nothing;
3. **a Spring Data method name** — `findByLastName` reads, `deleteById` writes;
4. **`@Transactional`** — a write *unless* it declares `readOnly = true`;
5. **accessor naming** — `getName` reads, `setName` writes. Weakest, so last.

A type's role is the union of its declared methods'. That is deliberately
conservative for Spring Data: petclinic's repository interfaces declare only
finders and inherit `save` from `JpaRepository`, which is external and therefore
invisible to a source-only index — so `OwnerRepository` classifies as `read`.
Claiming otherwise would put a write-side tick on evidence that does not show a
write.

Nothing here guesses. A node with no signal gets ``none``, and the confidence
rules treat that as "this citation says nothing about coverage" rather than as
either side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import config
from .model import NodeKind

READ = config.IO_READ
WRITE = config.IO_WRITE
BOTH = config.IO_BOTH
NONE = config.IO_NONE


@dataclass(slots=True)
class IoRoleResult:
    counts: dict[str, int] = field(default_factory=dict)
    #: node id -> role, for the nodes that got a non-`none` one
    roles: dict[str, str] = field(default_factory=dict)
    #: node id -> the rule that decided it
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def combine(*roles: str | None) -> str:
    """Union of roles. read + write = both; anything + none = anything."""
    seen = {role for role in roles if role and role != NONE}
    if not seen:
        return NONE
    if seen == {READ}:
        return READ
    if seen == {WRITE}:
        return WRITE
    return BOTH


def role_of_http_method(http_method: str | None) -> str:
    """`GET` -> read, `POST` -> write, `ANY` -> both (it accepts either)."""
    method = (http_method or "").upper()
    if method in config.HTTP_READ_METHODS:
        return READ
    if method in config.HTTP_WRITE_METHODS:
        return WRITE
    return BOTH if method == "ANY" else NONE


def _prefix_role(name: str, read_prefixes, write_prefixes) -> str:  # noqa: D401
    """Longest prefix wins, so `deleteDistinctBy...` is not read as `delete`+noise."""
    best_role, best_length = NONE, 0
    for prefixes, role in ((read_prefixes, READ), (write_prefixes, WRITE)):
        for prefix in prefixes:
            if len(prefix) > best_length and name.startswith(prefix):
                best_role, best_length = role, len(prefix)
    return best_role


def role_of_repository_method(name: str) -> str:
    return _prefix_role(name, config.REPOSITORY_READ_PREFIXES, config.REPOSITORY_WRITE_PREFIXES)


def role_of_accessor(name: str) -> str:
    return _prefix_role(name, config.ACCESSOR_READ_PREFIXES, config.ACCESSOR_WRITE_PREFIXES)


def role_of_transactional(attrs: dict) -> str:
    """`@Transactional` -> write; `@Transactional(readOnly = true)` -> read.

    Absent the annotation, ``none`` — never a guess.
    """
    if config.TRANSACTIONAL_ANNOTATION not in (attrs.get("annotations") or []):
        return NONE
    args = (attrs.get("annotation_args") or {}).get(config.TRANSACTIONAL_ANNOTATION) or {}
    values = args.get(config.TRANSACTIONAL_READ_ONLY_KEY) or []
    if any(str(value).strip().lower() == "true" for value in values):
        return READ
    return WRITE


# --------------------------------------------------------------------------- pass


def _handler_roles(store) -> dict[str, str]:
    """method node -> the role of the route it handles, unioned over its routes."""
    roles: dict[str, str] = {}
    for row in store.conn.execute(
        "SELECT e.src AS handler, n.attrs AS attrs FROM edges e JOIN nodes n ON n.id = e.dst"
        " WHERE e.kind = 'handles_route'"
    ):
        attrs = json.loads(row["attrs"])
        roles[row["handler"]] = combine(
            roles.get(row["handler"]), role_of_http_method(attrs.get("http_method"))
        )
    return roles


def _repository_types(store) -> set[str]:
    """Interfaces (and classes) that reach an entity — Spring Data repositories.

    Identified from the graph rather than re-derived from the source: `spring.py`
    already decided what a repository is when it emitted `reads_entity`, and two
    definitions of "repository" that could drift is one too many.
    """
    return {
        row["src"]
        for row in store.conn.execute(
            "SELECT DISTINCT e.src AS src FROM edges e JOIN nodes n ON n.id = e.src"
            " WHERE e.kind = 'reads_entity' AND n.kind IN ('interface', 'class')"
        )
    }


def apply_to_graph(store) -> IoRoleResult:
    """Write ``attrs.io_role`` onto every node. Returns the counts per role."""
    result = IoRoleResult()
    handler_roles = _handler_roles(store)
    repository_types = _repository_types(store)

    # methods declared by a repository type, via the contains edge
    repository_methods: set[str] = set()
    if repository_types:
        placeholders = ",".join("?" * len(repository_types))
        repository_methods = {
            row["dst"]
            for row in store.conn.execute(
                f"SELECT dst FROM edges WHERE kind = 'contains' AND src IN ({placeholders})",
                tuple(sorted(repository_types)),
            )
        }

    rows = store.conn.execute("SELECT id, kind, name, attrs FROM nodes ORDER BY id").fetchall()
    roles: dict[str, str] = {}
    sources: dict[str, str] = {}
    for row in rows:
        attrs = json.loads(row["attrs"])
        kind, name = row["kind"], row["name"]

        source = config.IO_SOURCE_MEMBERS
        if kind == NodeKind.ENDPOINT:
            role, source = role_of_http_method(attrs.get("http_method")), config.IO_SOURCE_HTTP
        elif kind == NodeKind.SCREEN:
            # A screen displays. A form on it is still the read side: the write
            # happens in the handler the form posts to.
            role, source = READ, config.IO_SOURCE_SCREEN
        elif kind == NodeKind.METHOD:
            role, source = handler_roles.get(row["id"], NONE), config.IO_SOURCE_ROUTE
            if role == NONE and row["id"] in repository_methods:
                role, source = role_of_repository_method(name), config.IO_SOURCE_REPOSITORY
            if role == NONE:
                role, source = role_of_transactional(attrs), config.IO_SOURCE_TRANSACTIONAL
            if role == NONE and not attrs.get("is_constructor"):
                role, source = role_of_accessor(name), config.IO_SOURCE_ACCESSOR
        else:
            role = NONE
        roles[row["id"]] = role
        sources[row["id"]] = source if role != NONE else ""

    # A type is what its methods do. Second pass, because it reads the first.
    member_roles: dict[str, str] = {}
    for row in store.conn.execute(
        "SELECT e.src AS owner, e.dst AS member FROM edges e JOIN nodes n ON n.id = e.src"
        " WHERE e.kind = 'contains' AND n.kind IN ('class', 'interface', 'enum', 'record')"
    ):
        member_roles[row["owner"]] = combine(
            member_roles.get(row["owner"]), roles.get(row["member"])
        )
    for node_id, role in member_roles.items():
        if roles.get(node_id) in (None, NONE):
            roles[node_id] = role
            sources[node_id] = config.IO_SOURCE_MEMBERS if role != NONE else ""

    for row in rows:
        role = roles.get(row["id"], NONE)
        attrs = json.loads(row["attrs"])
        attrs["io_role"] = role
        #: which rule decided it — the confidence rules ask, because a setter
        #: writing to an object is not the same claim as a POST handler saving one
        attrs["io_source"] = sources.get(row["id"], "")
        store.conn.execute(
            "UPDATE nodes SET attrs = ? WHERE id = ?",
            (json.dumps(attrs, sort_keys=True), row["id"]),
        )
        result.counts[role] = result.counts.get(role, 0) + 1
        if role != NONE:
            result.roles[row["id"]] = role
            result.sources[row["id"]] = attrs["io_source"]
    store.conn.commit()
    return result
