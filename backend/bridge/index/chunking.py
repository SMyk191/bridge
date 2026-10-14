"""Turning parsed files into the units that get described, embedded and cited.

Today the unit is "one node = one method or one class", and the code handed to
the describer is a 4000-character slice (`config.DESCRIBE_MAX_SOURCE_CHARS`).
Three things go wrong with that on real code:

1. **A 400-line method becomes one chunk, then gets truncated.** Whatever happens
   after character 4000 is invisible to the description *and* to the embedding —
   silently, so nobody knows the second half of the method is not in the index.
2. **A class has no chunk of its own.** "What does OwnerController do?" has to be
   answered by reading its methods one at a time, when the class declaration plus
   its fields plus a list of what it can do is exactly the summary that answers
   it.
3. **A template is one blob.** A 900-line `ownerDetails.html` is one description
   for a page that has five independent sections.

So: long methods are split into overlapping windows that each carry their
enclosing signature as context; every type gets a header chunk; templates split
on their top-level sections. Overlap exists because a window boundary that lands
mid-statement produces two chunks that each read like a fragment —
`CHUNK_OVERLAP_LINES` of shared context is cheap insurance against that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config

#: chunk kinds this module can produce, beyond the node kinds already in model.py
KIND_METHOD_PART = "method_part"
KIND_TYPE_HEADER = "type_header"
KIND_TEMPLATE_SECTION = "template_section"


@dataclass(slots=True)
class Chunk:
    """A unit of code that will be described, embedded, and cited on its own."""

    kind: str
    name: str
    qualified_name: str
    code: str
    start_line: int
    end_line: int
    #: node id this chunk belongs to, so chunks and the graph stay joined up
    node_id: str = ""
    attrs: dict = field(default_factory=dict)

    @property
    def line_count(self) -> int:
        return max(0, self.end_line - self.start_line + 1)


def _lines_of(source: str) -> list[str]:
    return source.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def slice_lines(source: str, start_line: int, end_line: int) -> str:
    """1-based, inclusive — the same convention as every citation in the system."""
    lines = _lines_of(source)
    first = max(0, start_line - 1)
    last = min(len(lines), end_line)
    return "\n".join(lines[first:last])


# --------------------------------------------------------------------------- methods


def split_method(
    *,
    source: str,
    name: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    node_id: str = "",
    owner: str = "",
    signature: str = "",
    max_lines: int = config.CHUNK_MAX_LINES,
    overlap: int = config.CHUNK_OVERLAP_LINES,
) -> list[Chunk]:
    """One chunk if the method is short enough, otherwise overlapping windows.

    Every window past the first is prefixed with a context comment naming the
    class and method it came from, because a window starting at line 240 of a
    file is otherwise an anonymous block of statements — the describer cannot
    tell what it belongs to, and neither can a reader who gets it back as a
    citation.

    Line numbers stay absolute and real: a window's citation points at the lines
    it actually covers, so `bridge verify` and every `repo@sha:path:start-end` in
    an answer still resolve.
    """
    total = max(0, end_line - start_line + 1)
    body = slice_lines(source, start_line, end_line)
    if total <= max_lines:
        return [
            Chunk(
                kind="method",
                name=name,
                qualified_name=qualified_name,
                code=body,
                start_line=start_line,
                end_line=end_line,
                node_id=node_id,
                attrs={"part": 0, "parts": 1},
            )
        ]

    # stride < max_lines is what creates the overlap; guard against a config that
    # would make stride <= 0 and loop forever
    stride = max(1, max_lines - overlap)
    windows: list[tuple[int, int]] = []
    cursor = start_line
    while cursor <= end_line:
        windows.append((cursor, min(end_line, cursor + max_lines - 1)))
        if windows[-1][1] >= end_line:
            break
        cursor += stride

    header = f"// in {owner}.{signature or name} — part {{part}} of {len(windows)}"
    chunks: list[Chunk] = []
    for index, (first, last) in enumerate(windows):
        text = slice_lines(source, first, last)
        if index:
            text = header.format(part=index + 1) + "\n" + text
        chunks.append(
            Chunk(
                kind=KIND_METHOD_PART if index else "method",
                name=name if not index else f"{name} (part {index + 1})",
                # Each part needs its OWN qualified name, or every part hashes to
                # the same code_hash-adjacent identity and they collide.
                qualified_name=qualified_name if not index else f"{qualified_name}#part{index + 1}",
                code=text,
                start_line=first,
                end_line=last,
                node_id=node_id,
                attrs={"part": index, "parts": len(windows)},
            )
        )
    return chunks


# --------------------------------------------------------------------------- types


def type_header_chunk(
    *,
    source: str,
    type_name: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    fields: list[str],
    method_names: list[str],
    node_id: str = "",
    annotations: list[str] | None = None,
) -> Chunk:
    """The class as a thing in itself: what it is, what it holds, what it can do.

    Not a slice of the file — a *synthesised* header. A real slice would be the
    first N lines, which is imports and a licence comment. What answers "what is
    this class for" is the declaration, the fields, and the index of methods, so
    that is what gets built and described.

    The line span still points at the real declaration, so the citation resolves
    to something a reader can open.
    """
    parts: list[str] = []
    for annotation in annotations or []:
        parts.append(f"@{annotation}")
    parts.append(f"class {type_name} {{")
    if fields:
        parts.extend(f"    {field_text};" for field_text in fields)
        parts.append("")
    if method_names:
        parts.append(f"    // {len(method_names)} methods:")
        parts.extend(f"    {method};" for method in method_names)
    parts.append("}")

    return Chunk(
        kind=KIND_TYPE_HEADER,
        name=type_name,
        qualified_name=qualified_name,
        code="\n".join(parts),
        start_line=start_line,
        end_line=end_line,
        node_id=node_id,
        attrs={"fields": len(fields), "methods": len(method_names), "synthesised": True},
    )


# --------------------------------------------------------------------------- templates


#: tags that open a section of a server-rendered page worth describing alone
_SECTION_TAGS = (
    "<section", "<main", "<article", "<form", "<table", "<nav", "<header", "<footer", "<dialog",
)


def split_template(
    *,
    source: str,
    name: str,
    qualified_name: str,
    node_id: str = "",
    max_lines: int = config.CHUNK_MAX_LINES,
) -> list[Chunk]:
    """A page by its top-level sections, falling back to the whole file.

    A form and the table below it are two different answers to two different
    questions; describing them together produces a sentence that is true of
    neither. Sections are found by scanning for structural tags at the shallowest
    indentation they appear at, which is crude compared to parsing the DOM but
    does not require a second parser and degrades safely: when nothing is found,
    the file stays one chunk, exactly as it is today.
    """
    lines = _lines_of(source)
    if len(lines) <= max_lines:
        return [
            Chunk(
                kind="screen", name=name, qualified_name=qualified_name,
                code=source, start_line=1, end_line=len(lines), node_id=node_id,
                attrs={"section": 0, "sections": 1},
            )
        ]

    starts: list[int] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if any(stripped.startswith(tag) for tag in _SECTION_TAGS):
            starts.append(index)

    if not starts:
        return [
            Chunk(
                kind="screen", name=name, qualified_name=qualified_name,
                code=source, start_line=1, end_line=len(lines), node_id=node_id,
                attrs={"section": 0, "sections": 1},
            )
        ]

    # everything before the first section is the page's own head/preamble
    boundaries = ([1] if starts[0] > 1 else []) + starts
    chunks: list[Chunk] = []
    for index, first in enumerate(boundaries):
        last = (boundaries[index + 1] - 1) if index + 1 < len(boundaries) else len(lines)
        if last < first:
            continue
        chunks.append(
            Chunk(
                kind=KIND_TEMPLATE_SECTION if index else "screen",
                name=name if not index else f"{name} (section {index + 1})",
                qualified_name=qualified_name if not index else f"{qualified_name}#section{index + 1}",
                code=slice_lines(source, first, last),
                start_line=first,
                end_line=last,
                node_id=node_id,
                attrs={"section": index, "sections": len(boundaries)},
            )
        )
    return chunks
