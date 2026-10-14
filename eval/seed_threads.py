"""Seed a few real threads so the frontend dashboard isn't empty at demo time.

    .venv/bin/python ../eval/seed_threads.py

Three questions, one per lane that actually produces a rich answer (question,
change_request, bug — data_question redirects to a one-liner and isn't
interesting to look at in a demo). Each becomes its own thread; run directly
through the orchestrator, the same path eval/run_smoke.py uses, so this needs
no running API server.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from bridge.orchestrator import RunStore, run  # noqa: E402
from bridge.retrieval.retrieve import Retriever, _resolve_index_dir  # noqa: E402

SEED_QUESTIONS = [
    "What happens when an owner adds a new pet?",
    "If we add a pet weight field, what's affected?",
    "A visit isn't appearing on the owner page — where should I look?",
]


def main() -> int:
    index_dir = _resolve_index_dir()
    print(f"index: {index_dir}")

    started = time.perf_counter()
    with Retriever.open(index_dir) as retriever, RunStore.open() as store:
        for question in SEED_QUESTIONS:
            t0 = time.perf_counter()
            # fresh=True for the same reason as run_smoke.py: seeding demo
            # threads should exercise the real pipeline, not a cache hit.
            state = run(question, index_dir=index_dir, retriever=retriever, store=store, fresh=True)
            impact = state.get("impact_map") or {}
            print(
                f"  {question[:60]:60s} lane={state.get('lane'):15s} "
                f"conf={impact.get('confidence', '-'):6s} "
                f"thread={state.get('thread_id')} ({time.perf_counter()-t0:.1f}s)"
            )

    print(f"\nseeded {len(SEED_QUESTIONS)} threads in {time.perf_counter()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
