"""Index a repository into ``data/index/<sha>/graph.db``.

    python -m bridge.index.build ./data/repos/spring-petclinic

The pipeline is deterministic and read-only with respect to the target repo: it
never writes to it, only reads the checked-out tree at HEAD.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__, config
from ..agents.llm import Budget, LLMClient, resolve_role
from . import describe as describe_pass
from . import embed as embed_pass
from . import history as history_pass
from . import io_roles as io_roles_pass
from . import suggest as suggest_pass
from . import ids, screens, spring
from .graph import GraphStore
from .model import Edge, EdgeKind, Node, NodeKind, Unresolved
from .parse_java import CallResolver, ParsedFile, TypeIndex, parse_source


@dataclass(slots=True)
class BuildResult:
    repo_name: str
    repo_path: Path
    head_sha: str
    out_dir: Path
    db_path: Path
    java_files: int
    test_files: int
    template_files: int
    dangling_edges: list[Edge] = field(default_factory=list)
    elapsed: float = 0.0
    #: wall-clock per phase, in run order
    timings: dict[str, float] = field(default_factory=dict)
    describe: describe_pass.DescribeResult | None = None
    embed: embed_pass.EmbedResult | None = None
    history: history_pass.HistoryResult | None = None
    io_roles: io_roles_pass.IoRoleResult | None = None
    suggest: suggest_pass.SuggestResult | None = None


# --------------------------------------------------------------------------- git


def head_sha(repo_path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - env dependent
        raise SystemExit(f"cannot read HEAD of {repo_path}: {exc}") from exc
    return completed.stdout.strip()


# --------------------------------------------------------------------------- discovery


def discover_java(repo_path: Path) -> list[str]:
    found: list[str] = []
    for path in repo_path.rglob("*.java"):
        relative = path.relative_to(repo_path)
        if any(part in config.IGNORE_DIRS for part in relative.parts):
            continue
        found.append(str(relative))
    return sorted(found)


def is_test_path(rel_path: str) -> bool:
    return any(marker in f"/{rel_path}" for marker in config.TEST_PATH_MARKERS)


# --------------------------------------------------------------------------- java -> rows


def java_rows(
    repo_path: Path, parsed_files: list[ParsedFile], index: TypeIndex
) -> tuple[list[Node], list[Edge]]:
    nodes: list[Node] = []
    edges: list[Edge] = []

    for parsed in parsed_files:
        is_test = is_test_path(parsed.path)
        line_count = (repo_path / parsed.path).read_text(
            encoding="utf-8", errors="replace"
        ).count("\n") + 1
        nodes.append(
            Node(
                id=parsed.node_id,
                kind=NodeKind.FILE,
                name=Path(parsed.path).name,
                qualified_name=parsed.path,
                path=parsed.path,
                start_line=1,
                end_line=line_count,
                lang="java",
                attrs={"package": parsed.package, "is_test": is_test},
            )
        )

        for imported, line in parsed.imports:
            target = imported[:-2] if imported.endswith(".*") else imported
            if imported.endswith(".*") or target not in index.by_fqn:
                continue  # external, or a wildcard that pins no single type
            edges.append(
                Edge(
                    src=parsed.node_id,
                    dst=ids.type_id(target),
                    kind=EdgeKind.IMPORTS,
                    path=parsed.path,
                    line=line,
                )
            )

        for jtype in parsed.types:
            nodes.append(
                Node(
                    id=jtype.node_id,
                    kind=jtype.kind,
                    name=jtype.name,
                    qualified_name=jtype.fqn,
                    path=jtype.path,
                    start_line=jtype.start_line,
                    end_line=jtype.end_line,
                    lang="java",
                    attrs={
                        "package": parsed.package,
                        "modifiers": jtype.modifiers,
                        "annotations": [a.name for a in jtype.annotations],
                        "extends": [s.raw for s in jtype.extends],
                        "implements": [s.raw for s in jtype.implements],
                        "is_test": is_test,
                    },
                )
            )
            # nested types hang off their enclosing type, top-level off the file
            parent_fqn = jtype.fqn.rsplit(".", 1)[0] if "." in jtype.fqn else ""
            parent_id = (
                ids.type_id(parent_fqn) if parent_fqn in index.by_fqn else parsed.node_id
            )
            edges.append(
                Edge(
                    src=parent_id,
                    dst=jtype.node_id,
                    kind=EdgeKind.CONTAINS,
                    path=jtype.path,
                    line=jtype.start_line,
                )
            )

            for method in jtype.methods:
                nodes.append(
                    Node(
                        id=method.node_id,
                        kind=NodeKind.METHOD,
                        name=method.name,
                        qualified_name=f"{jtype.fqn}#{method.signature}",
                        path=jtype.path,
                        start_line=method.start_line,
                        end_line=method.end_line,
                        lang="java",
                        attrs={
                            "signature": method.signature,
                            "return_type": method.return_type,
                            "modifiers": method.modifiers,
                            # Complexity inputs for index/tiers.py. The parser
                            # already collected both; only the counts are kept,
                            # because the tier decision needs "how much is going
                            # on here", not the contents.
                            "invocation_count": len(method.invocations),
                            "identifier_count": len(method.identifiers),
                            "annotations": [a.name for a in method.annotations],
                            # Arguments, not just names. `@Transactional` and
                            # `@Transactional(readOnly = true)` mean opposite
                            # things to io_roles.py, and the name alone cannot
                            # tell them apart. Only annotations that actually
                            # carry arguments are stored.
                            "annotation_args": {
                                a.name: a.args for a in method.annotations if a.args
                            },
                            "is_constructor": method.is_constructor,
                            "owner": jtype.fqn,
                            "is_test": is_test,
                        },
                    )
                )
                edges.append(
                    Edge(
                        src=jtype.node_id,
                        dst=method.node_id,
                        kind=EdgeKind.CONTAINS,
                        path=jtype.path,
                        line=method.start_line,
                    )
                )
    return nodes, edges


# --------------------------------------------------------------------------- pipeline


def build(
    repo_path: Path,
    out_root: Path | None = None,
    fresh: bool = True,
    *,
    with_describe: bool = True,
    with_embed: bool = True,
    with_history: bool = True,
    with_suggest: bool = True,
    llm: LLMClient | None = None,
    budget: Budget | None = None,
    progress=None,
) -> BuildResult:
    started = time.perf_counter()
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise SystemExit(f"not a directory: {repo_path}")

    sha = head_sha(repo_path)
    out_dir = (out_root or config.INDEX_ROOT) / sha
    db_path = out_dir / config.GRAPH_DB_NAME
    timings: dict[str, float] = {}
    graph_started = time.perf_counter()

    java_paths = discover_java(repo_path)
    parsed_files = [
        parse_source(rel_path, (repo_path / rel_path).read_bytes()) for rel_path in java_paths
    ]
    index = TypeIndex(parsed_files)

    nodes, edges = java_rows(repo_path, parsed_files, index)
    unresolved: list[Unresolved] = []

    call_edges, call_unresolved = CallResolver(index).run()
    edges.extend(call_edges)
    unresolved.extend(call_unresolved)

    spring_result = spring.run(index)
    nodes.extend(spring_result.nodes)
    edges.extend(spring_result.edges)
    unresolved.extend(spring_result.unresolved)

    screens_result = screens.run(repo_path, index, spring_result.controller_fqns)
    nodes.extend(screens_result.nodes)
    edges.extend(screens_result.edges)
    unresolved.extend(screens_result.unresolved)

    store = GraphStore.open(db_path, fresh=fresh)
    store.add_nodes(nodes)
    _, dangling = store.add_edges(edges)
    store.add_unresolved(unresolved)
    store.compute_degrees()
    store.flag_hubs()
    # Read side or write side, per node. Part of the graph stage because it is a
    # property of the structure, not of any question asked about it later.
    io_role_result = io_roles_pass.apply_to_graph(store)
    timings["graph"] = time.perf_counter() - graph_started

    test_files = sum(1 for path in java_paths if is_test_path(path))
    result = BuildResult(
        repo_name=repo_path.name,
        repo_path=repo_path,
        head_sha=sha,
        out_dir=out_dir,
        db_path=db_path,
        java_files=len(java_paths),
        test_files=test_files,
        template_files=len(screens_result.screen_ids),
        dangling_edges=dangling,
        timings=timings,
        io_roles=io_role_result,
    )

    store.set_meta("repo_name", result.repo_name)
    store.set_meta("repo_path", str(repo_path))
    store.set_meta("head_sha", sha)
    store.set_meta("indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.set_meta("bridge_version", __version__)
    store.set_meta("file_count", result.java_files)
    store.set_meta("template_count", result.template_files)
    store.conn.commit()

    # ---------------------------------------------------------------- history
    # Runs first so churn is on the nodes before anything reads them.
    if with_history:
        phase = time.perf_counter()
        indexed_paths = sorted(
            {row["path"] for row in store.conn.execute("SELECT DISTINCT path FROM nodes")}
        )
        result.history = history_pass.run(repo_path, indexed_paths, progress=progress)
        history_pass.apply_to_graph(store, result.history)
        timings["history"] = time.perf_counter() - phase

    # LLMClient is shared by describe and suggest, so it's built once regardless
    # of which of those two passes actually run — either can need it standalone
    # (e.g. `--no-describe` with suggest still on, reading a prior run's cache).
    client = llm if llm is not None else LLMClient(budget=budget or Budget())

    # ---------------------------------------------------------------- describe
    if with_describe:
        phase = time.perf_counter()
        result.describe = describe_pass.run(
            store,
            repo_path,
            out_dir / config.DESCRIPTIONS_DB_NAME,
            client,
            progress=progress,
        )
        timings["describe"] = time.perf_counter() - phase

    # ---------------------------------------------------------------- embed
    if with_embed:
        phase = time.perf_counter()
        if result.describe is not None:
            # The complete working set — model descriptions plus any fallbacks.
            # Re-reading the cache here would silently drop every fallback,
            # since the cache deliberately holds model output only.
            texts = result.describe.descriptions
        else:
            with describe_pass.DescriptionStore.open(
                out_dir / config.DESCRIPTIONS_DB_NAME
            ) as descriptions:
                texts = descriptions.all_descriptions()
        result.embed = embed_pass.run(
            store,
            repo_path,
            result.repo_name,
            out_dir / config.VECTORS_DIR_NAME,
            texts,
            progress=progress,
        )
        timings["embed"] = time.perf_counter() - phase


    # ---------------------------------------------------------------- suggest
    # Runs after describe so the model has real behavioural text to write
    # product-language questions from, not bare identifiers.
    if with_suggest:
        phase = time.perf_counter()
        if result.describe is not None:
            texts = result.describe.descriptions
        elif (out_dir / config.DESCRIPTIONS_DB_NAME).is_file():
            with describe_pass.DescriptionStore.open(
                out_dir / config.DESCRIPTIONS_DB_NAME
            ) as descriptions:
                texts = descriptions.all_descriptions()
        else:
            texts = {}
        result.suggest = suggest_pass.run(store, client, texts, progress=progress)
        suggest_pass.write(out_dir, sha, result.suggest)
        timings["suggest"] = time.perf_counter() - phase

    store.conn.commit()
    result.elapsed = time.perf_counter() - started
    _write_manifest(store, result)
    store.close()
    return result


def _write_manifest(store: GraphStore, result: BuildResult) -> None:
    manifest = {
        "repo_name": result.repo_name,
        "repo_path": str(result.repo_path),
        "head_sha": result.head_sha,
        "indexed_at": store.get_meta("indexed_at"),
        "bridge_version": __version__,
        "graph_db": config.GRAPH_DB_NAME,
        "counts": {
            "java_files": result.java_files,
            "test_files": result.test_files,
            "templates": result.template_files,
            "nodes": store.count_by("nodes", "kind"),
            "edges": store.count_by("edges", "kind"),
            "unresolved": store.unresolved_by_reason(),
        },
        "dangling_edges": len(result.dangling_edges),
        "timings": {phase: round(seconds, 3) for phase, seconds in result.timings.items()},
    }
    if result.describe is not None:
        manifest["descriptions"] = {
            "store": config.DESCRIPTIONS_DB_NAME,
            "described": result.describe.described,
            "from_cache": result.describe.from_cache,
            "from_model": result.describe.from_model,
            "from_fallback": result.describe.from_fallback,
            "cost_usd": round(result.describe.cost_usd, 6),
            "input_tokens": result.describe.input_tokens,
            "output_tokens": result.describe.output_tokens,
            "llm_available": result.describe.llm_available,
            "redactions": result.describe.redaction_counts,
        }
    if result.embed is not None:
        manifest["vectors"] = {
            "dir": config.VECTORS_DIR_NAME,
            "model": result.embed.model,
            "chunks": result.embed.chunks,
            "dimensions": result.embed.dimensions,
            "redactions": result.embed.redaction_counts,
        }
    if result.history is not None:
        manifest["history"] = {
            "files": len(result.history.files),
            "modules": len(result.history.modules),
            "recent_window_days": config.HISTORY_RECENT_DAYS,
            "head_date": result.history.head_date,
        }
    if result.io_roles is not None:
        manifest["io_roles"] = dict(sorted(result.io_roles.counts.items()))
    if result.suggest is not None:
        manifest["suggestions"] = {
            "file": config.SUGGESTIONS_FILE_NAME,
            "count": len(result.suggest.questions),
            "source": result.suggest.source,
            "cost_usd": round(result.suggest.cost_usd, 6),
        }
    (result.out_dir / config.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- summary


def _short(node_id: str) -> str:
    """`java:a.b.c.OwnerController#showOwner` -> `OwnerController#showOwner`."""
    if not node_id.startswith("java:"):
        return node_id
    body = node_id[len("java:") :]
    owner, _, member = body.partition("#")
    short_owner = owner.rsplit(".", 1)[-1]
    return f"{short_owner}#{member}" if member else short_owner


def format_summary(store: GraphStore, result: BuildResult) -> str:
    lines: list[str] = []
    rule = "=" * 78

    lines.append(rule)
    lines.append(f"Bridge index — {result.repo_name} @ {result.head_sha[:12]}")
    lines.append(rule)
    lines.append(f"  repo          {result.repo_path}")
    lines.append(f"  head sha      {result.head_sha}")
    lines.append(f"  output        {result.db_path}")
    lines.append(
        f"  java files    {result.java_files} "
        f"({result.java_files - result.test_files} main, {result.test_files} test)"
    )
    lines.append(f"  templates     {result.template_files}")
    lines.append(f"  elapsed       {result.elapsed:.2f}s")
    if result.dangling_edges:
        lines.append(f"  DANGLING      {len(result.dangling_edges)} edges dropped (missing endpoint)")

    node_counts = store.count_by("nodes", "kind")
    lines.append("")
    lines.append(f"NODES  ({sum(node_counts.values())})")
    for kind, count in sorted(node_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {kind:<16} {count:>5}")

    edge_counts = store.count_by("edges", "kind")
    breakdown = store.edge_confidence_breakdown()
    lines.append("")
    lines.append(f"EDGES  ({sum(edge_counts.values())})")
    for kind, count in sorted(edge_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        confidences = breakdown.get(kind, {})
        detail = "  ".join(
            f"{level} {confidences[level]}"
            for level in ("high", "medium", "low")
            if level in confidences
        )
        lines.append(f"  {kind:<16} {count:>5}   {detail}")

    endpoints = store.endpoints()
    lines.append("")
    lines.append(f"ENDPOINTS  ({len(endpoints)})")
    for row in endpoints:
        attrs = json.loads(row["attrs"])
        handler = _short(attrs.get("handler", ""))
        lines.append(
            f"  {attrs['http_method']:<6} {attrs['route']:<46} -> {handler}  "
            f"({row['path'].split('/')[-1]}:{row['start_line']}-{row['end_line']})"
        )

    hubs = store.hubs()
    total_nodes = sum(node_counts.values())
    lines.append("")
    lines.append(
        f"HUBS  (top {config.HUB_PERCENTILE:.0%} of {total_nodes} nodes by semantic in-degree, "
        f"{len(hubs)} flagged)"
    )
    for row in hubs:
        parts = store.in_degree_breakdown(row["id"])
        detail = ", ".join(f"{kind}: {count}" for kind, count in parts.items())
        lines.append(
            f"  {row['id']:<52} in={row['semantic_in_degree']:<4} "
            f"(all-edge in={row['in_degree']})  {{{detail}}}"
        )

    lines.append("")
    lines.append("TOP 5 NODES BY SEMANTIC IN-DEGREE")
    for row in store.top_nodes(5, by="semantic_in_degree"):
        lines.append(
            f"  {row['semantic_in_degree']:>4}  {row['id']:<58} "
            f"[{row['kind']}]{'  HUB' if row['is_hub'] else ''}"
        )
    lines.append("")
    lines.append("TOP 5 NODES BY RAW IN-DEGREE (all edge kinds)")
    for row in store.top_nodes(5, by="in_degree"):
        lines.append(f"  {row['in_degree']:>4}  {row['id']:<58} [{row['kind']}]")

    by_reason = store.unresolved_by_reason()
    by_kind = store.count_by("unresolved", "kind")
    lines.append("")
    lines.append(f"UNRESOLVED  ({sum(by_reason.values())} total, {store.unresolved_count('call')} calls)")
    lines.append("  by kind:")
    for kind, count in by_kind.items():
        lines.append(f"    {kind:<14} {count:>5}")
    lines.append("  by reason:")
    for reason, count in by_reason.items():
        lines.append(f"    {reason:<30} {count:>5}")

    lines.extend(_semantic_summary(result))
    lines.append(rule)
    return "\n".join(lines)


def _semantic_summary(result: BuildResult) -> list[str]:
    """Descriptions, vectors, history, redaction counts, timings, and cost."""
    lines: list[str] = []
    redactions: dict[str, int] = {}

    if result.describe is not None:
        described = result.describe
        for label, count in described.redaction_counts.items():
            redactions[label] = redactions.get(label, 0) + count
        provider, spec = resolve_role("describe")
        lines.append("")
        lines.append(f"DESCRIPTIONS  ({described.described})")
        lines.append(f"  role model      {provider}:{spec['model']}")
        lines.append(f"  from cache      {described.from_cache:>5}")
        lines.append(f"  from model      {described.from_model:>5}")
        lines.append(f"  from fallback   {described.from_fallback:>5}")
        if described.from_model:
            lines.append(
                f"  tokens          {described.input_tokens:>5} in / "
                f"{described.output_tokens} out"
            )
        if not described.llm_available:
            lines.append(f"  NO LLM          {described.unavailable_reason}")

    if result.embed is not None:
        embedded = result.embed
        for label, count in embedded.redaction_counts.items():
            redactions[label] = redactions.get(label, 0) + count
        lines.append("")
        lines.append(f"VECTORS  ({embedded.chunks} chunks, {embedded.dimensions}d)")
        lines.append(f"  model           {embedded.model}")
        lines.append(f"  store           {embedded.table_path}")

    if result.history is not None:
        history = result.history
        hottest = [entry for entry in history.hottest if entry.commits_all_time][:5]
        lines.append("")
        lines.append(
            f"HISTORY  ({len(history.files)} files, {len(history.modules)} modules, "
            f"last {config.HISTORY_RECENT_DAYS}d window)"
        )
        for entry in hottest:
            lines.append(
                f"  {entry.path.split('/')[-1]:<34} {entry.commits_all_time:>3} commits "
                f"({entry.commits_recent} recent)  last {entry.last_changed}  "
                f"module={entry.module}"
            )

    lines.append("")
    lines.append("REDACTION")
    if redactions:
        for label, count in sorted(redactions.items()):
            lines.append(f"  {label:<24} {count:>5}")
    else:
        lines.append("  no secrets or PII matched in any chunk")

    if result.timings:
        lines.append("")
        lines.append("TIMINGS")
        for phase, seconds in result.timings.items():
            lines.append(f"  {phase:<16} {seconds:>7.2f}s")
        lines.append(f"  {'total':<16} {result.elapsed:>7.2f}s")

    cost = result.describe.cost_usd if result.describe else 0.0
    lines.append("")
    lines.append(f"COST  ${cost:.4f} total for descriptions")
    return lines


def summary_json(store: GraphStore, result: BuildResult) -> dict:
    return {
        "repo_name": result.repo_name,
        "head_sha": result.head_sha,
        "db_path": str(result.db_path),
        "java_files": result.java_files,
        "templates": result.template_files,
        "nodes": store.count_by("nodes", "kind"),
        "edges": store.count_by("edges", "kind"),
        "edge_confidence": store.edge_confidence_breakdown(),
        "endpoints": [row["id"] for row in store.endpoints()],
        "hubs": [
            {
                "id": row["id"],
                "semantic_in_degree": row["semantic_in_degree"],
                "in_degree": row["in_degree"],
                "breakdown": store.in_degree_breakdown(row["id"]),
            }
            for row in store.hubs()
        ],
        "unresolved": store.unresolved_by_reason(),
    }


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge index",
        description="Index a repository into a Bridge structural graph (read-only).",
    )
    parser.add_argument("repo", type=Path, help="path to a git checkout to index")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"index root (default: {config.INDEX_ROOT}); the graph lands in <out>/<sha>/",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="reuse an existing graph.db for this sha instead of rebuilding it",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip model calls; descriptions fall back to deterministic text",
    )
    parser.add_argument("--no-describe", action="store_true", help="skip the description pass")
    parser.add_argument("--no-embed", action="store_true", help="skip the embedding pass")
    parser.add_argument("--no-history", action="store_true", help="skip the git history pass")
    parser.add_argument("--no-suggest", action="store_true", help="skip generating suggested questions")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=config.DEFAULT_BUDGET_USD,
        help=f"cost ceiling for this run (default ${config.DEFAULT_BUDGET_USD:.2f}); 0 = unlimited",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    budget = Budget(
        max_usd=args.budget_usd if args.budget_usd > 0 else None,
        max_calls=config.DEFAULT_BUDGET_CALLS,
    )
    client = LLMClient(budget=budget, enabled=not args.no_llm)
    if not args.json:
        for provider_name, state in client.status().items():
            print(f"  provider {provider_name:<10} {state}", flush=True)

    result = build(
        args.repo,
        out_root=args.out,
        fresh=not args.keep,
        with_describe=not args.no_describe,
        with_embed=not args.no_embed,
        with_history=not args.no_history,
        with_suggest=not args.no_suggest,
        llm=client,
        progress=(lambda message: print(f"  {message}", flush=True)) if not args.json else None,
    )
    with GraphStore.open(result.db_path) as store:
        if args.json:
            print(json.dumps(summary_json(store, result), indent=2, sort_keys=True))
        else:
            print(format_summary(store, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
