"""Not every chunk earns a model call.

Today a getter and a POST handler cost exactly the same: one description call
each. On petclinic that is 130 calls and nobody notices. On a real repository it
is the dominant cost of indexing, and most of it buys nothing — "Returns the
owner's city" is not an insight, and no product question is ever answered by it.

Three tiers, assigned from five signals the index already has:

    T0  template     a getter, a setter, a constructor, `toString`, a two-line
                     body. Description generated deterministically FROM THE
                     SIGNATURE. No model call, ever.
    T1  file-summary a file with no individually-interesting members. One
                     description for the file; its chunks inherit it.
    T2  full         endpoints, route handlers, hubs, entities, persisting
                     writes, anything the vocabulary names, anything long or
                     branchy enough to carry real behaviour. The model reads it.

The signals are role (io_role and kind), size, a complexity proxy, graph degree,
and vocabulary hits. Two of those — degree and io_role — only exist once edges
do, which is why this runs as a post-pass in the same slot as
`io_roles.apply_to_graph` rather than at parse time.

**The bias is deliberate and one-directional: when a signal is ambiguous, tier
up.** A chunk wrongly left at T0 is a hole in the index that no retrieval can
recover from — the description is a stub forever, and the reader is never told.
A chunk wrongly promoted to T2 costs a fraction of a cent. Those are not
symmetric mistakes, so the rules are not symmetric either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import config
from .model import NodeKind

T0 = config.TIER_TEMPLATE
T1 = config.TIER_FILE
T2 = config.TIER_FULL

#: kinds that are always worth a real description — they ARE the product surface
_ALWAYS_FULL_KINDS = frozenset({NodeKind.ENDPOINT, NodeKind.ENTITY, NodeKind.SCREEN})


@dataclass(slots=True)
class TierResult:
    counts: dict[str, int] = field(default_factory=dict)
    #: node id -> tier, for the nodes that got one
    tiers: dict[str, str] = field(default_factory=dict)
    #: node id -> the rule that decided it, for the summary and for arguing with
    reasons: dict[str, str] = field(default_factory=dict)

    def record(self, node_id: str, tier: str, reason: str) -> None:
        self.tiers[node_id] = tier
        self.reasons[node_id] = reason
        self.counts[tier] = self.counts.get(tier, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def distribution(self) -> dict[str, int]:
        return {tier: self.counts.get(tier, 0) for tier in config.TIERS}


def is_accessor(name: str) -> bool:
    """`getName`, `setCity`, `isNew`, `hasErrors` — structurally, not by convention.

    Requires a capital after the prefix so that `getting`, `settle` and `island`
    are not swept up by a `startswith` that means well.
    """
    for prefix in config.TIER_ACCESSOR_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix) and name[len(prefix)].isupper():
            return True
    return False


def complexity_proxy(attrs: dict) -> int:
    """How much is going on in here, without building a control-flow graph.

    Call-outs plus distinct identifiers touched. A method that calls three things
    and names twelve variables is doing work; one that returns a field is not.
    tree-sitter could give a real cyclomatic count, but this is computed from
    what the parser already stored and is enough to separate the two ends.
    """
    invocations = int(attrs.get("invocation_count") or 0)
    identifiers = int(attrs.get("identifier_count") or 0)
    return invocations * 2 + identifiers


def tier_for(
    *,
    kind: str,
    name: str,
    attrs: dict,
    line_count: int,
    semantic_in_degree: int,
    io_role: str,
    io_source: str,
    vocab_hit: bool,
) -> tuple[str, str]:
    """``(tier, reason)`` for one chunk. Pure, so it can be argued with in a test."""
    # --- promotions first: anything here is worth a real description no matter
    # how small or simple it looks.
    if kind in _ALWAYS_FULL_KINDS:
        return T2, f"{kind} is part of the product surface"
    if vocab_hit:
        return T2, "the product vocabulary names this"
    if io_role in (config.IO_WRITE, config.IO_BOTH) and io_source in config.IO_PERSIST_SOURCES:
        return T2, "it persists data"
    if io_source == config.IO_SOURCE_ROUTE:
        return T2, "it handles a route"
    if semantic_in_degree >= config.TIER_HUB_IN_DEGREE:
        return T2, f"{semantic_in_degree} semantic callers — a hub"

    # --- demotions: only reached when none of the above fired.
    if name in config.TIER_BOILERPLATE_NAMES:
        return T0, "boilerplate method name"
    if attrs.get("is_constructor"):
        return T0, "constructor"
    if is_accessor(name) and line_count <= config.TIER_TRIVIAL_MAX_LINES:
        return T0, "short accessor"

    invocations = int(attrs.get("invocation_count") or 0)
    if (
        line_count <= config.TIER_TRIVIAL_MAX_LINES
        and invocations <= config.TIER_TRIVIAL_MAX_INVOCATIONS
        and semantic_in_degree == 0
    ):
        return T0, f"{line_count} lines, {invocations} call(s), nothing calls it"

    return T2, "carries behaviour"


# --------------------------------------------------------------------------- pass


def _vocab_names(vocabulary) -> set[str]:
    """Bare names the vocabulary points at, for a cheap membership test."""
    names: set[str] = set()
    for term in getattr(vocabulary, "terms", []) or []:
        for node_id in term.nodes:
            # java:com.foo.Owner#addPet -> {"Owner", "addPet"}; screen:owners/x -> {"x"}
            tail = node_id.split(":", 1)[-1]
            symbol, _, member = tail.partition("#")
            names.add(symbol.rsplit(".", 1)[-1])
            if member:
                names.add(member.split("(", 1)[0])
    return names


def apply_to_graph(store, vocabulary=None) -> TierResult:
    """Write ``attrs.tier`` onto every node, and report the distribution.

    A type whose every member landed at T0 is demoted as a unit to T1: describing
    six trivial accessors separately is six calls that say nothing, whereas one
    sentence about the class they belong to is genuinely useful. This is the only
    place T1 is assigned, because it is a statement about a *file*, which no
    individual chunk can make about itself.
    """
    from ..retrieval.vocab import default_vocabulary

    vocabulary = vocabulary if vocabulary is not None else default_vocabulary()
    vocab_names = _vocab_names(vocabulary)
    result = TierResult()

    rows = store.conn.execute(
        "SELECT id, kind, name, path, start_line, end_line, semantic_in_degree, attrs"
        " FROM nodes ORDER BY id"
    ).fetchall()

    for row in rows:
        attrs = json.loads(row["attrs"])
        lines = max(0, (row["end_line"] or 0) - (row["start_line"] or 0) + 1)
        tier, reason = tier_for(
            kind=row["kind"],
            name=row["name"],
            attrs=attrs,
            line_count=lines,
            semantic_in_degree=int(row["semantic_in_degree"] or 0),
            io_role=attrs.get("io_role") or config.IO_NONE,
            io_source=attrs.get("io_source") or "",
            vocab_hit=row["name"] in vocab_names,
        )
        result.record(row["id"], tier, reason)

    # --- T1: a type whose members are all T0 becomes one file-level description
    members: dict[str, list[str]] = {}
    for row in store.conn.execute(
        "SELECT e.src AS owner, e.dst AS member FROM edges e JOIN nodes n ON n.id = e.src"
        " WHERE e.kind = 'contains' AND n.kind IN ('class','interface','enum','record')"
    ):
        members.setdefault(row["owner"], []).append(row["member"])

    for owner, member_ids in members.items():
        if not member_ids or owner not in result.tiers:
            continue
        if all(result.tiers.get(m) == T0 for m in member_ids) and result.tiers.get(owner) != T2:
            for node_id in [owner, *member_ids]:
                if result.tiers.get(node_id) == T2:
                    continue
                previous = result.tiers.get(node_id)
                if previous:
                    result.counts[previous] -= 1
                result.record(node_id, T1, "every member of this type is trivial")

    for row in rows:
        attrs = json.loads(row["attrs"])
        attrs["tier"] = result.tiers.get(row["id"], T2)
        attrs["tier_reason"] = result.reasons.get(row["id"], "")
        store.conn.execute(
            "UPDATE nodes SET attrs = ? WHERE id = ?",
            (json.dumps(attrs, sort_keys=True), row["id"]),
        )
    store.conn.commit()
    return result


# --------------------------------------------------------------------------- T0 text


def template_description(kind: str, name: str, attrs: dict, owner: str = "") -> str:
    """A T0 description, generated from the signature. Deterministic, free, honest.

    It says exactly what the signature says and claims nothing more. That is the
    whole contract of T0: this chunk was judged not to need a model, so the text
    it gets must not pretend a model looked at it.
    """
    where = f" on {owner}" if owner else ""
    if attrs.get("is_constructor"):
        return f"Creates a {owner or name}."
    for prefix in ("get", "is", "has"):
        if name.startswith(prefix) and len(name) > len(prefix) and name[len(prefix)].isupper():
            field_name = _humanise(name[len(prefix) :])
            return f"Reads the {field_name}{where}."
    if name.startswith("set") and len(name) > 3 and name[3].isupper():
        return f"Sets the {_humanise(name[3:])}{where}."
    if name in config.TIER_BOILERPLATE_NAMES:
        return f"Standard {name}{where}."
    return f"{_humanise(name).capitalize()}{where}."


def _humanise(camel: str) -> str:
    """`lastName` -> `last name`, so a template description reads like English."""
    out: list[str] = []
    for index, char in enumerate(camel):
        if char.isupper() and index:
            out.append(" ")
        out.append(char.lower())
    return "".join(out).strip()
