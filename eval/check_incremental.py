"""Phase E's four incremental claims, checked against a real Postgres and real git.

    .venv/bin/python ../eval/check_incremental.py

Every scenario runs on a **copy** of the repository under /tmp, because it makes
commits, and Bridge never writes to a repository it indexes (CLAUDE.md, line one).

What each scenario is really asserting:

  (b) a 3-file change describes only what changed — the incremental indexer works
  (c) a rename-only commit costs 0 describe calls — `code_hash` excludes location
  (d) a noise-only commit costs 0 of everything — the policy excludes lockfiles
  (e) verify reports zero drift — content-addressing is true, not aspirational

Exit code is 0 only if all four hold.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from bridge.db import create_all, get_engine  # noqa: E402
from bridge.db.repo import IndexRepo  # noqa: E402
from bridge.index import pgindex  # noqa: E402

SOURCE_REPO = Path("/Users/samyak/bridge/data/repos/spring-petclinic")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def clone(destination: Path) -> Path:
    """A throwaway copy with its own history we are allowed to add to."""
    shutil.copytree(SOURCE_REPO, destination, symlinks=True)
    git(destination, "config", "user.email", "phase-e@example.com")
    git(destination, "config", "user.name", "phase E")
    return destination


def index(root: Path, repo: IndexRepo, previous: str | None) -> pgindex.IndexReport:
    return pgindex.index_repo(root, repo, previous_sha=previous)


def show(title: str, report: pgindex.IndexReport) -> None:
    new = report.as_dict()["new"]
    print(
        f"  {title:<34} blobs={new['blobs']:<4} chunks={new['chunks']:<4} "
        f"describe={new['descriptions_scheduled']:<4} vectors={new['vectors']:<4} "
        f"edges={new['edges']}"
    )


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="bridge-phase-e-"))
    root = clone(workspace / "spring-petclinic")
    engine = get_engine()
    create_all(engine)
    repo = IndexRepo(engine)
    failures: list[str] = []

    print("=" * 92)
    print(f"PHASE E — incremental verification   repo copy: {root}")
    print("=" * 92)

    # ---------------------------------------------------------------- baseline
    base_sha = pgindex.head_sha(root)
    baseline = index(root, repo, None)
    print("\nbaseline (full index)")
    show("full", baseline)

    # ---------------------------------------------------------------- (b)
    print("\n(b) three files edited -> only the affected chunks are described")
    targets = [
        "src/main/java/org/springframework/samples/petclinic/owner/PetValidator.java",
        "src/main/java/org/springframework/samples/petclinic/vet/VetController.java",
        "src/main/java/org/springframework/samples/petclinic/system/WelcomeController.java",
    ]
    for rel in targets:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        # Add a real METHOD, not a comment. An earlier version of this check
        # inserted a comment just inside the class brace and saw describe=0 —
        # which was the system working correctly (no code_hash changed, so
        # nothing needed re-describing) but proved nothing, because the check
        # would equally have passed had description been broken to never fire.
        # A new method is a new code_hash that MUST be described, while every
        # existing method in the same file must NOT be.
        addition = (
            "\n\tpublic String phaseEProbe" + str(targets.index(rel)) + "() {\n"
            "\t\treturn \"probe\" + System.currentTimeMillis();\n\t}\n"
        )
        closing = text.rindex("}")
        path.write_text(text[:closing] + addition + text[closing:], encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "edit three files")
    edited_sha = pgindex.head_sha(root)
    edited = index(root, repo, base_sha)
    show("3 files edited", edited)
    if edited.files_modified != 3:
        failures.append(f"(b) expected 3 modified files, saw {edited.files_modified}")
    if edited.blobs_new != 3:
        failures.append(f"(b) expected 3 new blobs, saw {edited.blobs_new}")
    # The claim, stated as two halves that must BOTH hold:
    #   > 0  — the three genuinely new methods were described
    #   << full index — nothing else was
    if edited.descriptions_scheduled == 0:
        failures.append("(b) three new methods scheduled 0 describe calls — nothing was described")
    if edited.descriptions_scheduled > 12:
        failures.append(
            f"(b) {edited.descriptions_scheduled} descriptions for 3 new methods — too many; "
            "unchanged code is being re-described"
        )
    print(
        f"      -> {edited.descriptions_scheduled} of {baseline.descriptions_scheduled} "
        f"descriptions ({edited.descriptions_scheduled / baseline.descriptions_scheduled:.1%} "
        "of a full index)"
    )

    # ---------------------------------------------------------------- (c)
    print("\n(c) rename-only commit -> 0 describe calls")
    old_path = "src/main/java/org/springframework/samples/petclinic/system/WelcomeController.java"
    new_path = "src/main/java/org/springframework/samples/petclinic/system/GreetingController.java"
    git(root, "mv", old_path, new_path)
    git(root, "commit", "-qm", "rename WelcomeController file")
    renamed_sha = pgindex.head_sha(root)
    renamed = index(root, repo, edited_sha)
    show("file renamed", renamed)
    if renamed.descriptions_scheduled != 0:
        failures.append(
            f"(c) a rename scheduled {renamed.descriptions_scheduled} describe calls, expected 0"
        )
    if renamed.vectors_new != 0:
        failures.append(f"(c) a rename embedded {renamed.vectors_new} chunks, expected 0")
    if renamed.blobs_new != 0:
        failures.append(f"(c) a rename stored {renamed.blobs_new} blobs, expected 0")

    # ---------------------------------------------------------------- (d)
    print("\n(d) noise-only commit (a lockfile) -> no work at all")
    (root / "yarn.lock").write_text("# lockfile\n" + "x\n" * 500, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add a lockfile")
    noise_sha = pgindex.head_sha(root)
    noise = index(root, repo, renamed_sha)
    show("lockfile added", noise)
    if not noise.did_no_work:
        failures.append(f"(d) a lockfile commit did work: {noise.as_dict()['new']}")

    # ---------------------------------------------------------------- (e)
    print("\n(e) verify -> zero drift")
    snapshot = repo.snapshot_by_sha(root.name, noise_sha)
    drift = pgindex.verify_snapshot(root, repo, snapshot["id"])
    print(
        f"  {'re-hashed every blob':<34} checked={drift.checked} drifted={len(drift.drifted)} "
        f"missing={len(drift.missing)} orphans={len(drift.orphans)}"
    )
    if not drift.clean:
        failures.append(f"(e) drift: {drift.drifted or drift.missing or drift.orphans}")

    # a deliberately corrupted working tree MUST be caught, or (e) proves nothing
    victim = root / "src/main/java/org/springframework/samples/petclinic/owner/Pet.java"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8")
    caught = pgindex.verify_snapshot(root, repo, snapshot["id"])
    print(
        f"  {'after corrupting one file':<34} drifted={len(caught.drifted)} "
        f"(a clean report here would mean verify checks nothing)"
    )
    if len(caught.drifted) != 1:
        failures.append(f"(e) verify missed a corrupted file: {len(caught.drifted)} drifted")

    # ---------------------------------------------------------------- report
    print("\n" + "=" * 92)
    if failures:
        print("FAILURES")
        for line in failures:
            print("  -", line)
        return 1
    print("all four incremental claims hold.")
    print("=" * 92)
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
