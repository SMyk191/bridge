"""Capture retrieval results for a fixed question set, so two index builds can be diffed.

Usage::

    .venv/bin/python tools/compare_retrieval.py capture fallback
    #   ... rebuild the index with a working API key ...
    .venv/bin/python tools/compare_retrieval.py capture credentialed
    .venv/bin/python tools/compare_retrieval.py diff fallback credentialed

Two layers are recorded per question, because they answer different things:

* ``semantic`` — raw `semantic_search` top-5. This is the *pure* embedding signal,
  and the only place a change in description text shows up unmediated.
* ``retrieved`` — final `retrieve()` top-5 after graph expansion and reranking.
  A reranker can mask an embedding change, so this alone would not settle the
  question.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bridge import config
from bridge.index.embed import VectorIndex
from bridge.retrieval.retrieve import Retriever

QUESTIONS = [
    "How does an owner add a pet?",
    "Where do we store vet specialties?",
    "Can a customer book an appointment, and what does that page look like?",
]

OUT_DIR = Path(__file__).parent / "retrieval-snapshots"


def _index_dir() -> Path:
    candidates = [p for p in config.INDEX_ROOT.glob("*") if (p / config.GRAPH_DB_NAME).is_file()]
    if not candidates:
        raise SystemExit("no index found — run `bridge index ./data/repos/spring-petclinic`")
    return max(candidates, key=lambda p: (p / config.GRAPH_DB_NAME).stat().st_mtime)


def short(node_id: str) -> str:
    body = node_id.split(":", 1)[-1]
    if "#" in body:
        owner, _, member = body.partition("#")
        return f"{owner.rsplit('.', 1)[-1]}#{member}"
    return body.rsplit(".", 1)[-1] if node_id.startswith(("java:", "entity:")) else body


def capture(label: str) -> dict:
    index_dir = _index_dir()
    vectors = VectorIndex.open(index_dir / config.VECTORS_DIR_NAME)

    # Which description text is actually in the vectors — the thing under test.
    rows = vectors.table.to_arrow().to_pylist()
    sample = next((r for r in rows if r["node_id"].endswith("PetController#processCreationForm")), rows[0])

    snapshot = {
        "label": label,
        "index_dir": str(index_dir),
        "chunks": len(rows),
        "sample_description": {"node_id": sample["node_id"], "text": sample["description"]},
        "questions": [],
    }

    with Retriever.open(index_dir) as retriever:
        for question in QUESTIONS:
            semantic = vectors.semantic_search(question, k=5)
            result = retriever.retrieve(question)
            snapshot["questions"].append(
                {
                    "question": question,
                    "semantic_top5": [
                        {"node_id": h.node_id, "short": short(h.node_id), "score": round(h.score, 4),
                         "kind": h.kind, "description": h.description}
                        for h in semantic
                    ],
                    "retrieved_top5": [
                        {"node_id": c.node_id, "short": short(c.node_id), "kind": c.kind,
                         "score": round(c.score, 3), "rerank_score": c.rerank_score,
                         "citation": c.citation, "description": c.description,
                         "sources": c.sources}
                        for c in result.chunks[:5]
                    ],
                    "confidence": result.confidence,
                    "confidence_reason": result.confidence_reason,
                    "reranked": result.reranked,
                    "rerank_unavailable_reason": result.rerank_unavailable_reason,
                    "cost_usd": round(result.cost_usd, 6),
                    "elapsed_s": round(result.elapsed, 2),
                    "stage_counts": result.stage_counts,
                }
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return snapshot


def render(snapshot: dict) -> None:
    print("=" * 78)
    print(f"{snapshot['label'].upper()}  ({snapshot['chunks']} chunks)")
    print("=" * 78)
    print(f"sample description [{short(snapshot['sample_description']['node_id'])}]:")
    print(f"  {snapshot['sample_description']['text']!r}")
    for entry in snapshot["questions"]:
        print()
        print(f"Q: {entry['question']}")
        print(f"   confidence={entry['confidence']}  cost=${entry['cost_usd']:.6f}  "
              f"reranked={entry['reranked']}  ({entry['elapsed_s']}s)")
        print(f"   why: {entry['confidence_reason']}")
        print("   semantic top-5 (pure embedding):")
        for i, hit in enumerate(entry["semantic_top5"], 1):
            print(f"     {i}. {hit['score']:.3f}  {hit['short'][:52]:52s} [{hit['kind']}]")
        print("   retrieve() top-5 (after graph + rerank):")
        for i, chunk in enumerate(entry["retrieved_top5"], 1):
            rr = f"rr={chunk['rerank_score']}" if chunk["rerank_score"] is not None else "rr=-"
            print(f"     {i}. {chunk['score']:6.2f} {rr:>7s}  {chunk['short'][:44]:44s} [{chunk['kind']}]")


def diff(left_label: str, right_label: str) -> None:
    left = json.loads((OUT_DIR / f"{left_label}.json").read_text())
    right = json.loads((OUT_DIR / f"{right_label}.json").read_text())

    print("=" * 78)
    print(f"DIFF  {left_label}  ->  {right_label}")
    print("=" * 78)
    for a, b in zip(left["questions"], right["questions"]):
        print()
        print(f"Q: {a['question']}")
        a_sem = [h["short"] for h in a["semantic_top5"]]
        b_sem = [h["short"] for h in b["semantic_top5"]]
        a_ret = [c["short"] for c in a["retrieved_top5"]]
        b_ret = [c["short"] for c in b["retrieved_top5"]]

        overlap_sem = len(set(a_sem) & set(b_sem))
        overlap_ret = len(set(a_ret) & set(b_ret))
        print(f"  semantic top-5 overlap : {overlap_sem}/5   "
              f"(entered: {sorted(set(b_sem) - set(a_sem))})")
        print(f"  retrieved top-5 overlap: {overlap_ret}/5   "
              f"(entered: {sorted(set(b_ret) - set(a_ret))})")
        print(f"  confidence : {a['confidence']} -> {b['confidence']}")
        print(f"  cost/call  : ${a['cost_usd']:.6f} -> ${b['cost_usd']:.6f}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "capture":
        render(capture(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)
