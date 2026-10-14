"""Determinism check: the same question, asked cold three times, must cite the
same evidence every time.

    .venv/bin/python ../eval/check_determinism.py             # the default set
    .venv/bin/python ../eval/check_determinism.py add-pet      # one question

"Cold" means `fresh=True` on every one of the three runs — the whole point is
to exercise the real pipeline three separate times, never the answer cache
(orchestrator/cache.py). A cache hit would trivially "pass" this check by
returning the exact same answer for the wrong reason.

What is compared is the SET of citations the answer actually made (parsed out
of the rendered text, the same way `orchestrator/run.py::compose_answer` builds
its own citation block) — not the prose. Temperature 0 (config.py) plus stable,
explicitly tie-broken sorts (retrieve.py, followup.py) make the EVIDENCE a run
selects deterministic; the exact wording a model produces around that evidence
is not something this system claims to control, and checking prose equality
would fail this check on harmless paraphrasing while missing the thing that
actually matters: did two runs end up standing behind the same facts?

This is the artifact meant to run in CI. Bridge has no CI pipeline configured
yet (the repo isn't even under git as of this phase — see PROGRESS.md), so
there is no workflow file to point at; `make determinism` is the hook a real
one would call. Exit code is 0 on success, 1 on any mismatch, so it slots into
any runner without extra parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import yaml  # noqa: E402

from bridge.agents.state import citations_of  # noqa: E402
from bridge.orchestrator import RunStore, run  # noqa: E402
from bridge.retrieval.retrieve import Retriever, _resolve_index_dir  # noqa: E402

QUESTIONS_FILE = Path(__file__).parent / "smoke_questions.yaml"
#: three cold runs per question by default — matches the phase's own ask
ATTEMPTS = 3
#: a representative slice: one of each lane this repo's smoke suite reaches,
#: not all twelve — this check costs `ATTEMPTS`x a normal run, per question
DEFAULT_IDS = ("add-pet", "pet-weight", "visit-missing")


def citation_set(state: dict) -> frozenset[str]:
    return frozenset(citations_of(state))


def check_one(
    question: str, index_dir: Path, retriever: Retriever, store: RunStore
) -> tuple[bool, list[frozenset[str]]]:
    sets = []
    for _ in range(ATTEMPTS):
        state = run(question, index_dir=index_dir, retriever=retriever, store=store, fresh=True)
        sets.append(citation_set(state))
    return len(set(sets)) == 1, sets


def main(argv: list[str]) -> int:
    spec = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in spec["questions"]}
    ids = argv or [i for i in DEFAULT_IDS if i in by_id]
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"no such question id(s): {missing}; ids: {sorted(by_id)}")
        return 2

    index_dir = _resolve_index_dir()
    print(f"index: {index_dir}")
    print(f"{ATTEMPTS} cold runs each, for: {ids}\n")

    failures = []
    with Retriever.open(index_dir) as retriever, RunStore.open() as store:
        for qid in ids:
            question = by_id[qid]["question"]
            stable, sets = check_one(question, index_dir, retriever, store)
            status = "PASS" if stable else "FAIL"
            print(f"  [{status}] {qid:22s} {len(sets[0])} citations, "
                  f"{'identical' if stable else 'DIFFERED'} across {ATTEMPTS} runs")
            if not stable:
                failures.append((qid, sets))

    if failures:
        print("\n" + "=" * 96)
        print("DETERMINISM FAILURES")
        print("=" * 96)
        for qid, sets in failures:
            print(f"\n{qid}:")
            for i, citation_set_i in enumerate(sets, start=1):
                print(f"  run {i}: {sorted(citation_set_i)}")
        return 1

    print("\nall citation sets identical across all cold runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
