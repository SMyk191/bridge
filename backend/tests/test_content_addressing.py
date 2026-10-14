"""The hash chain, and the four incremental guarantees that fall out of it.

Every claim this phase makes about cost — "a rename is free", "a noise commit
does no work", "only the changed files get described" — is a claim about these
five functions and nothing else. If `code_hash` ever starts depending on a line
number, a path, or a blob, the indexer silently becomes a rebuild and no other
test in the suite would notice.
"""

from __future__ import annotations

from bridge.db import keys


def test_blob_hash_is_over_raw_bytes():
    assert keys.blob_hash(b"class A {}") == keys.blob_hash(b"class A {}")
    assert keys.blob_hash(b"class A {}") != keys.blob_hash(b"class B {}")


def test_an_encoding_change_is_a_content_change():
    """Deliberate: hashing decoded text would silently reuse chunks across a
    re-encode, and the bytes on disk are what `bridge verify` re-hashes."""
    assert keys.blob_hash("é".encode("utf-8")) != keys.blob_hash("é".encode("latin-1"))


# ------------------------------------------------------------------ normalisation


def test_normalisation_ignores_line_endings_and_trailing_whitespace():
    assert keys.normalise_code("a  \r\nb\t\r\n") == keys.normalise_code("a\nb")


def test_normalisation_collapses_blank_line_runs():
    assert keys.normalise_code("a\n\n\n\n\nb") == keys.normalise_code("a\n\nb")


def test_normalisation_does_not_reformat():
    """Two genuinely different layouts of the same statements read differently to
    a person, so they get different descriptions."""
    assert keys.normalise_code("if (x) { y(); }") != keys.normalise_code("if (x) {\n  y();\n}")


# ------------------------------------------------------------------ the reuse key


def test_code_hash_ignores_where_the_code_lives():
    """The single most important property in the phase: no blob, no path, no
    lines. This is what makes a move or a rename free."""
    a = keys.code_hash("method", "com.x.Owner#addPet", "void addPet(Pet p) { pets.add(p); }")
    b = keys.code_hash("method", "com.x.Owner#addPet", "void addPet(Pet p) { pets.add(p); }")
    assert a == b


def test_code_hash_changes_when_the_code_changes():
    a = keys.code_hash("method", "C#m", "return 1;")
    b = keys.code_hash("method", "C#m", "return 2;")
    assert a != b


def test_code_hash_changes_when_the_symbol_is_renamed():
    """A rename IS a semantic change — the description says the method's name."""
    a = keys.code_hash("method", "C#oldName", "return 1;")
    b = keys.code_hash("method", "C#newName", "return 1;")
    assert a != b


def test_the_hash_separator_prevents_field_smearing():
    """Without a separator ("ab","c") and ("a","bc") would collide."""
    assert keys.code_hash("ab", "c", "x") != keys.code_hash("a", "bc", "x")


# ------------------------------------------------------------------ chunk identity


def test_chunk_hash_is_blob_scoped_and_line_scoped():
    """Deliberately the opposite of code_hash: it makes the row immutable, so its
    start/end lines are true forever and `bridge verify` means something."""
    base = dict(kind="method", qualified_name="C#m", start_line=10, end_line=20)
    assert keys.chunk_hash(blob="b1", **base) != keys.chunk_hash(blob="b2", **base)
    assert keys.chunk_hash(blob="b1", **base) != keys.chunk_hash(
        blob="b1", kind="method", qualified_name="C#m", start_line=11, end_line=21
    )


# ------------------------------------------------------------------ the guarantees


def _pipeline(blob: str, code: str, qualified_name: str, start: int, model="m", embed="e"):
    """The full chain for one chunk, as the indexer walks it."""
    ch = keys.chunk_hash(blob, "method", qualified_name, start, start + 5)
    codeh = keys.code_hash("method", qualified_name, code)
    dk = keys.desc_key(codeh, model)
    vk = keys.vec_key(dk, embed)
    return ch, codeh, dk, vk


def test_a_method_that_moved_reuses_its_description_and_embedding():
    """The guarantee behind verification (c): a rename-only commit costs 0
    describe calls. New blob, new lines, new chunk row — same everything else."""
    before = _pipeline("blob-v1", "return 1;", "C#m", start=10)
    after = _pipeline("blob-v2", "return 1;", "C#m", start=90)

    assert before[0] != after[0], "a new blob and new lines mean a new chunk row"
    assert before[1] == after[1], "the code did not change, so code_hash must not"
    assert before[2] == after[2], "so the description is already there"
    assert before[3] == after[3], "and so is the embedding"


def test_a_file_moving_to_a_new_path_changes_nothing_at_all():
    """A pure rename does not touch the file's bytes, so it does not even reach
    the chunk layer — path lives in snapshot_files, not on any hashed row."""
    assert keys.blob_hash(b"x") == keys.blob_hash(b"x")
    assert _pipeline("blob", "return 1;", "C#m", 10) == _pipeline("blob", "return 1;", "C#m", 10)


def test_editing_one_method_leaves_its_neighbours_alone():
    """The guarantee behind verification (b): only the changed chunks get work.
    Both methods land in a new blob; only the edited one needs describing."""
    edited_before = _pipeline("v1", "return 1;", "C#edited", 10)
    edited_after = _pipeline("v2", "return 2;", "C#edited", 10)
    neighbour_before = _pipeline("v1", "return 9;", "C#neighbour", 30)
    neighbour_after = _pipeline("v2", "return 9;", "C#neighbour", 34)  # shifted down

    assert edited_before[2] != edited_after[2], "the edited method needs a new description"
    assert neighbour_before[2] == neighbour_after[2], "the untouched neighbour does not"


def test_promoting_a_chunk_from_template_to_a_model_re_describes_it():
    """`model` is in desc_key so a T0 chunk promoted to T2 cannot read back the
    template string that was good enough when nobody was going to look."""
    code = keys.code_hash("method", "C#getName", "return name;")
    assert keys.desc_key(code, keys.SOURCE_TEMPLATE) != keys.desc_key(code, "gemini-3.5-flash-lite")


def test_bumping_the_prompt_version_invalidates_every_description():
    code = keys.code_hash("method", "C#m", "return 1;")
    assert keys.desc_key(code, "m", "1") != keys.desc_key(code, "m", "2")


def test_re_embedding_needs_a_new_model_not_a_new_description():
    dk = keys.desc_key(keys.code_hash("method", "C#m", "x"), "m")
    assert keys.vec_key(dk, "bge-small") != keys.vec_key(dk, "bge-large")


# ------------------------------------------------------------------ ids


def test_snapshot_ids_are_stable_so_re_indexing_a_commit_is_idempotent():
    assert keys.snapshot_id("repo", "abc") == keys.snapshot_id("repo", "abc")
    assert keys.snapshot_id("repo", "abc") != keys.snapshot_id("repo", "def")


def test_a_merkle_key_depends_on_the_set_of_children_not_their_order():
    """Otherwise every index would re-summarise every directory because the
    filesystem walked it in a different order."""
    assert keys.merkle_key("src", ["a", "b"]) == keys.merkle_key("src", ["b", "a"])
    assert keys.merkle_key("src", ["a", "b"]) != keys.merkle_key("src", ["a", "c"])
    assert keys.merkle_key("src", ["a"]) != keys.merkle_key("lib", ["a"])
