"""End-to-end index assertions against spring-petclinic.

The repo must be cloned first::

    git clone https://github.com/spring-projects/spring-petclinic data/repos/spring-petclinic

The index is built once per session into a pytest tmp dir, so these tests never
depend on (or disturb) whatever is in ``data/index/``.
"""

from __future__ import annotations

import json

import pytest

from bridge import config
from bridge.index import ids
from bridge.index.build import build
from bridge.index.graph import GraphStore
from bridge.index.model import SEMANTIC_EDGE_KINDS, EdgeKind, NodeKind, TYPE_KINDS

REPO = config.PROJECT_ROOT / "data" / "repos" / "spring-petclinic"

PKG = "org.springframework.samples.petclinic"
OWNER_CONTROLLER = ids.type_id(f"{PKG}.owner.OwnerController")
OWNER_REPOSITORY = ids.type_id(f"{PKG}.owner.OwnerRepository")
VET_REPOSITORY = ids.type_id(f"{PKG}.vet.VetRepository")
PROCESS_CREATION_FORM = ids.method_id(f"{PKG}.owner.OwnerController", "processCreationForm")

OWNER = ids.entity_id(f"{PKG}.owner.Owner")
PET = ids.entity_id(f"{PKG}.owner.Pet")
VISIT = ids.entity_id(f"{PKG}.owner.Visit")
PET_TYPE = ids.entity_id(f"{PKG}.owner.PetType")
VET = ids.entity_id(f"{PKG}.vet.Vet")
SPECIALTY = ids.entity_id(f"{PKG}.vet.Specialty")


@pytest.fixture(scope="session")
def result(tmp_path_factory):
    if not REPO.is_dir():
        pytest.skip(
            f"{REPO} is missing — clone it first:\n"
            "  git clone https://github.com/spring-projects/spring-petclinic "
            "data/repos/spring-petclinic"
        )
    return build(REPO, out_root=tmp_path_factory.mktemp("index"))


@pytest.fixture(scope="session")
def store(result):
    with GraphStore.open(result.db_path) as opened:
        yield opened


def attrs_of(row) -> dict:
    return json.loads(row["attrs"])


def edges(store, *, src=None, dst=None, kind=None) -> list:
    clauses, params = [], []
    for column, value in (("src", src), ("dst", dst), ("kind", kind)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return store.conn.execute(f"SELECT * FROM edges{where}", params).fetchall()


# ------------------------------------------------------------------ required by spec


def test_every_controller_has_at_least_one_endpoint(store):
    """1.d.i — @Controller classes must yield endpoint nodes via handles_route."""
    controllers = [
        row
        for row in store.conn.execute("SELECT * FROM nodes WHERE kind IN ('class', 'interface')")
        if not attrs_of(row).get("is_test")
        and {"Controller", "RestController"} & set(attrs_of(row).get("annotations", []))
    ]
    assert controllers, "no @Controller classes found — the annotation pass is broken"

    without_routes = []
    for controller in controllers:
        routed = store.conn.execute(
            "SELECT COUNT(*) AS c FROM edges "
            "  JOIN edges AS owns ON owns.dst = edges.src AND owns.kind = ? "
            " WHERE owns.src = ? AND edges.kind = ?",
            (EdgeKind.CONTAINS, controller["id"], EdgeKind.HANDLES_ROUTE),
        ).fetchone()["c"]
        if routed == 0:
            without_routes.append(controller["id"])

    assert not without_routes, f"controllers with no endpoint: {without_routes}"
    assert len(controllers) == 6, f"expected petclinic's 6 controllers, got {len(controllers)}"


def test_owner_repository_reads_owner_entity(store):
    """1.d.ii — the repository's domain type comes from `extends JpaRepository<Owner, Integer>`."""
    found = edges(store, src=OWNER_REPOSITORY, dst=OWNER, kind=EdgeKind.READS_ENTITY)
    assert found, "OwnerRepository has no reads_entity edge to the Owner entity"
    assert attrs_of(found[0])["domain_type"] == f"{PKG}.owner.Owner"


def test_derived_query_methods_read_the_entity_with_parsed_properties(store):
    find_by_last_name = ids.method_id(
        f"{PKG}.owner.OwnerRepository", "findByLastNameStartingWith"
    )
    found = edges(store, src=find_by_last_name, dst=OWNER, kind=EdgeKind.READS_ENTITY)
    assert found, "findByLastNameStartingWith has no reads_entity edge"
    attrs = attrs_of(found[0])
    assert attrs["query_kind"] == "derived"
    assert attrs["properties"] == ["lastName"]


def test_a_renders_edge_links_a_controller_method_to_a_screen(store):
    """1.e — controller method -> the template it returns."""
    controller_fqns = {
        row["qualified_name"]
        for row in store.conn.execute("SELECT * FROM nodes WHERE kind IN ('class', 'interface')")
        if {"Controller", "RestController"} & set(attrs_of(row).get("annotations", []))
    }

    linked = []
    for edge in store.edges_of_kind(EdgeKind.RENDERS):
        src = store.node(edge["src"])
        dst = store.node(edge["dst"])
        assert src["kind"] == NodeKind.METHOD
        assert dst["kind"] == NodeKind.SCREEN
        if attrs_of(src)["owner"] in controller_fqns:
            linked.append((edge["src"], edge["dst"]))

    assert linked, "no renders edge connects a @Controller method to a screen"
    assert (
        ids.method_id(f"{PKG}.system.WelcomeController", "welcome"),
        ids.screen_id("welcome"),
    ) in linked


def test_renders_follows_a_view_name_hidden_in_a_constant(store):
    """`return VIEWS_OWNER_CREATE_OR_UPDATE_FORM;` — a return-only scan would miss this."""
    found = edges(
        store,
        src=ids.method_id(f"{PKG}.owner.OwnerController", "initCreationForm"),
        dst=ids.screen_id("owners/createOrUpdateOwnerForm"),
        kind=EdgeKind.RENDERS,
    )
    assert found
    assert attrs_of(found[0])["via_constant"] == "VIEWS_OWNER_CREATE_OR_UPDATE_FORM"


def test_renders_follows_a_view_name_hidden_in_a_constructor(store):
    """`new ModelAndView("owners/ownerDetails")` — never returned as a string."""
    found = edges(
        store,
        src=ids.method_id(f"{PKG}.owner.OwnerController", "showOwner"),
        dst=ids.screen_id("owners/ownerDetails"),
        kind=EdgeKind.RENDERS,
    )
    assert found
    assert attrs_of(found[0])["position"] == "view_ctor"


def test_traversal_from_owner_controller_reaches_the_owner_entity(store):
    """1.g — default (semantic) edge kinds, default depth."""
    walk = store.traverse(OWNER_CONTROLLER, "out", max_depth=4)
    assert walk.reaches(OWNER), (
        "traversal from OwnerController did not reach the Owner entity; "
        f"reached {len(walk.node_ids)} nodes"
    )
    path = walk.path_to(OWNER)
    assert path.depth <= 4
    assert path.hops[0].node_id == OWNER_CONTROLLER
    assert path.hops[-1].node_id == OWNER


def test_traversal_needs_member_expansion_to_leave_a_type_node(store):
    """Why `include_members` exists: `contains` is structural and excluded by default,
    so without member expansion a class node is a dead end."""
    with_members = store.traverse(OWNER_CONTROLLER, "out", max_depth=4)
    without = store.traverse(OWNER_CONTROLLER, "out", max_depth=4, include_members=False)
    assert len(without.node_ids) == 1
    assert len(with_members.node_ids) > 1
    assert not without.reaches(OWNER)


def test_traversal_respects_its_caps(store):
    capped = store.traverse(OWNER_CONTROLLER, "out", max_depth=4, max_nodes=5)
    assert len(capped.node_ids) <= 5
    assert capped.truncated_by_nodes

    shallow = store.traverse(OWNER_CONTROLLER, "out", max_depth=1)
    assert all(path.depth <= 1 for path in shallow.paths)


# ------------------------------------------------------------------ associations


def test_pet_associates_to_owner_visit_and_pet_type(store):
    outgoing = {
        edge["dst"]: attrs_of(edge)
        for edge in edges(store, src=PET, kind=EdgeKind.ASSOCIATES)
    }
    assert set(outgoing) == {OWNER, VISIT, PET_TYPE}
    assert all(
        edge["confidence"] == "high" for edge in edges(store, src=PET, kind=EdgeKind.ASSOCIATES)
    )

    # Owner.pets is mapped on Owner's side only, so Pet -> Owner is the derived inverse.
    assert outgoing[OWNER]["direction"] == "inverse"
    assert outgoing[OWNER]["cardinality"] == "ManyToOne"
    assert outgoing[OWNER]["via_field"] == "Owner.pets"
    # Pet declares both of these itself.
    assert outgoing[VISIT]["direction"] == "declared"
    assert outgoing[VISIT]["cardinality"] == "OneToMany"
    assert outgoing[PET_TYPE]["direction"] == "declared"
    assert outgoing[PET_TYPE]["cardinality"] == "ManyToOne"


def test_association_edges_are_exactly_four_pairs(store):
    """Pet's three outgoing edges leave Owner->Pet, Visit->Pet, PetType->Pet and the
    whole Vet<->Specialty pair untested; the count closes that gap."""
    all_edges = store.edges_of_kind(EdgeKind.ASSOCIATES)
    assert len(all_edges) == 8

    pairs = {(edge["src"], edge["dst"]) for edge in all_edges}
    assert pairs == {
        (OWNER, PET), (PET, OWNER),
        (PET, VISIT), (VISIT, PET),
        (PET, PET_TYPE), (PET_TYPE, PET),
        (VET, SPECIALTY), (SPECIALTY, VET),
    }
    directions = [attrs_of(edge)["direction"] for edge in all_edges]
    assert directions.count("declared") == 4
    assert directions.count("inverse") == 4


def test_mapped_superclasses_are_not_entities(store):
    """BaseEntity/Person/NamedEntity are @MappedSuperclass, not @Entity."""
    entity_ids = {row["id"] for row in store.nodes_of_kind(NodeKind.ENTITY)}
    assert len(entity_ids) == 6
    for name in ("BaseEntity", "Person", "NamedEntity"):
        assert ids.entity_id(f"{PKG}.model.{name}") not in entity_ids


# ------------------------------------------------------------------ honesty (design law 4)


def test_repository_save_is_recorded_as_unresolved(store):
    """`this.owners.save(owner)` — the receiver type is in-repo but `save` is declared
    on the external JpaRepository, so the method target is logged, not guessed."""
    rows = store.conn.execute(
        "SELECT * FROM unresolved WHERE kind = 'call' AND symbol = 'save' AND src = ?",
        (PROCESS_CREATION_FORM,),
    ).fetchall()
    assert rows, "no UNRESOLVED row for owners.save(...)"
    assert rows[0]["reason"] == "external_supertype_method"
    assert rows[0]["path"].endswith("OwnerController.java")
    assert rows[0]["line"] > 0


def test_repository_save_call_edge_is_low_confidence_with_a_reason(store):
    """The type dependency is a fact (the receiver was written); the exact method is not."""
    found = edges(store, src=PROCESS_CREATION_FORM, dst=OWNER_REPOSITORY, kind=EdgeKind.CALLS)
    assert found, "no calls edge from processCreationForm to OwnerRepository"
    edge = found[0]
    assert edge["confidence"] == "low"
    attrs = attrs_of(edge)
    assert attrs["reason"] == "external_supertype_method"
    assert attrs["unresolved_method"] == "save"


def test_unqualified_external_calls_produce_no_self_edge(store):
    """A statically imported `given(...)` must not become `TestClass -> TestClass`:
    the receiver was never written, so there is no dependency to assert."""
    self_edges = [
        edge
        for edge in store.edges_of_kind(EdgeKind.CALLS)
        if edge["src"].startswith(f"{edge['dst']}#")
    ]
    assert not self_edges, f"{len(self_edges)} calls edges point at their own declaring type"

    unqualified = store.conn.execute(
        "SELECT COUNT(*) AS c FROM unresolved WHERE reason = 'unqualified_external'"
    ).fetchone()["c"]
    assert unqualified > 0, "static imports should still be recorded as UNRESOLVED"


def test_unresolved_calls_are_counted_and_categorised(store):
    by_reason = store.unresolved_by_reason()
    assert store.unresolved_count("call") > 0
    assert set(by_reason) <= {
        "external_receiver",
        "external_supertype_method",
        "unqualified_external",
        "no_candidate",
        "ambiguous",
        "no_template",
    }


# ------------------------------------------------------------------ contract guards


def test_head_sha_is_recorded_and_drives_the_output_path(store, result):
    assert store.get_meta("head_sha") == result.head_sha
    assert len(result.head_sha) == 40
    assert result.out_dir.name == result.head_sha
    assert (result.out_dir / config.MANIFEST_NAME).is_file()


def test_the_specified_method_id_shape_exists(store):
    """The exact id from the task spec."""
    assert PROCESS_CREATION_FORM == (
        "java:org.springframework.samples.petclinic.owner.OwnerController#processCreationForm"
    )
    assert store.node(PROCESS_CREATION_FORM) is not None


def test_overloaded_methods_get_distinct_ids(store):
    """VetRepository declares findAll() and findAll(Pageable)."""
    no_args = ids.method_id(f"{PKG}.vet.VetRepository", "findAll", [])
    pageable = ids.method_id(f"{PKG}.vet.VetRepository", "findAll", ["Pageable"])
    assert no_args != pageable
    assert store.node(no_args) is not None
    assert store.node(pageable) is not None
    # the bare form must NOT exist, or the two would have silently merged
    assert store.node(ids.method_id(f"{PKG}.vet.VetRepository", "findAll")) is None


def test_redirect_view_names_do_not_become_screens(store):
    screen_names = {row["qualified_name"] for row in store.nodes_of_kind(NodeKind.SCREEN)}
    assert screen_names, "no screens indexed"
    assert not any(name.startswith(("redirect:", "forward:")) for name in screen_names)
    for edge in store.edges_of_kind(EdgeKind.RENDERS):
        assert not attrs_of(edge)["view_name"].startswith(("redirect:", "forward:"))


def test_screens_and_endpoints_match_the_repository_contents(store):
    assert len(store.nodes_of_kind(NodeKind.SCREEN)) == 12
    assert len(store.endpoints()) == 17
    assert store.node(ids.screen_id("fragments/layout"))["attrs"]
    assert json.loads(store.node(ids.screen_id("fragments/layout"))["attrs"])["is_fragment"]


def test_degrees_separate_structural_from_semantic(store):
    """in_degree counts every edge; semantic_in_degree counts only behavioural ones."""
    for row in store.conn.execute("SELECT * FROM nodes"):
        all_in = len(store.in_edges(row["id"]))
        semantic_in = sum(
            1 for edge in store.in_edges(row["id"]) if edge["kind"] in SEMANTIC_EDGE_KINDS
        )
        assert row["in_degree"] == all_in
        assert row["semantic_in_degree"] == semantic_in

    # and the two genuinely differ somewhere, or the distinction would be vacuous
    assert store.conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE in_degree > semantic_in_degree"
    ).fetchone()["c"] > 0


def test_hubs_are_the_top_percentile_by_semantic_in_degree(store):
    hubs = store.hubs()
    assert hubs, "no hubs flagged"
    assert all(row["semantic_in_degree"] > 0 for row in hubs)

    total = store.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    assert len(hubs) <= max(1, round(config.HUB_PERCENTILE * total) + 1)

    lowest_hub = min(row["semantic_in_degree"] for row in hubs)
    highest_other = store.conn.execute(
        "SELECT COALESCE(MAX(semantic_in_degree), 0) AS m FROM nodes WHERE is_hub = 0"
    ).fetchone()["m"]
    assert lowest_hub >= highest_other, "a non-hub outranks a hub — wrong ranking column"


def test_every_edge_endpoint_exists(store, result):
    assert result.dangling_edges == [], "edges were emitted for nodes that were never declared"
    orphans = store.conn.execute(
        "SELECT COUNT(*) AS c FROM edges "
        "WHERE src NOT IN (SELECT id FROM nodes) OR dst NOT IN (SELECT id FROM nodes)"
    ).fetchone()["c"]
    assert orphans == 0


def test_nodes_carry_citable_locations(store):
    """Design law 4: every claim needs `repo@sha:path:start-end`, so every node needs lines."""
    missing = store.conn.execute(
        "SELECT id FROM nodes WHERE path IS NULL OR path = '' OR start_line IS NULL"
    ).fetchall()
    assert not [row["id"] for row in missing]

    node = store.node(PROCESS_CREATION_FORM)
    assert ids.citation(
        "spring-petclinic", store.get_meta("head_sha"), node["path"], node["start_line"], node["end_line"]
    ).startswith("spring-petclinic@")


def test_type_nodes_use_the_expected_kinds(store):
    kinds = {row["kind"] for row in store.conn.execute("SELECT DISTINCT kind FROM nodes")}
    assert kinds <= TYPE_KINDS | {
        NodeKind.FILE,
        NodeKind.METHOD,
        NodeKind.ENDPOINT,
        NodeKind.ENTITY,
        NodeKind.SCREEN,
    }
    assert store.node(OWNER_REPOSITORY)["kind"] == NodeKind.INTERFACE
    assert store.node(OWNER_CONTROLLER)["kind"] == NodeKind.CLASS
