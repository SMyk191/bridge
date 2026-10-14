"""End-to-end, through the real FastAPI app: an ACL-denied team gets 403
before any retrieval work happens, a quota-exhausted team gets 429, and both
are on the audit/cost record. This is the "no cross-repo leakage" proof the
phase asks for, cashed out honestly for what this architecture actually has:
ONE repo/index live at a time (retrieval has no per-project routing yet —
security/acl.py's module docstring), so "cross-repo" becomes "a principal not
on repo X's list is refused access to repo X, full stop, before a single
chunk is retrieved."

Every test here short-circuits BEFORE `_stream_run` ever constructs the
orchestrator's `StreamingResponse` — a 403 or 429 raises straight out of
`create_run`. That is deliberate, not just convenient: this machine has no
reachable LLM provider (PROGRESS.md), and `asyncio.to_thread` node threads
cannot be cancelled once started (api/main.py's own concurrency note) — a test
that actually let a run start streaming could leave a background thread
retrying against a dead provider for minutes, which `node_executor.shutdown
(wait=True)` would then block the whole test session on at teardown. The ACL
gate's ALLOW path is still proven for real: `test_a_quota_refusal_still_
passed_the_acl_check_first` seeds an explicit allow-list entry and confirms
the audit row says `allowed: True` before the quota check is what stops it —
proof the gate opened, without ever needing the pipeline behind it to run.

Uses the real index under `data/index/` and a real embedded Postgres (skipped
if `pgserver` is not installed) via the app's actual lifespan — the same
startup path `make api` runs, not a mocked one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bridge import config
from bridge.orchestrator import quotas
from bridge.security import acl


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("pgserver")
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DB", tmp_path / "runs.sqlite")
    monkeypatch.setattr(config, "DATABASE_URL", "embedded://pytest_api_acl")
    acl._DEFAULT_PROVIDER = None  # force a fresh StaticACLProvider reading tmp_path

    # The app's lifespan opens a Retriever, which loads the module-level
    # `retrieval.vocab._DEFAULT` singleton on first use — if THIS test happens
    # to be the first thing in the whole session to touch it, it would cache
    # an empty vocabulary read from `tmp_path` (no vocab.<repo>.yaml there) for
    # every OTHER test in the process for good, since the singleton never
    # re-checks `config.CONFIG_ROOT` once set. Reset it on both sides so this
    # test's monkeypatched CONFIG_ROOT can never leak into anything else's
    # vocabulary — found via a real, if flaky-looking, failure in
    # test_semantic_petclinic.py before this reset existed.
    import bridge.retrieval.vocab as vocab_module

    vocab_module._DEFAULT = None

    from bridge.api.main import app

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        acl._DEFAULT_PROVIDER = None
        vocab_module._DEFAULT = None


def _write_acl(tmp_path, repo: str, readable_by: list[str]) -> None:
    (tmp_path / f"acl.{repo}.yaml").write_text(
        f"repo: {repo}\nreadable_by:\n" + "\n".join(f"  - {p}" for p in readable_by) + "\n"
    )


def _repo_name(client: TestClient) -> str:
    return client.app.state.retriever.repo


def test_a_team_not_on_the_acl_is_refused_with_403(client, tmp_path):
    repo = _repo_name(client)
    _write_acl(tmp_path, repo, ["team-allowed"])

    response = client.post("/runs", json={"question": "how does search work?", "team": "team-outsider"})
    assert response.status_code == 403
    assert "team-outsider" in response.json()["detail"]


def test_the_denial_is_on_the_audit_log_with_no_run_created(client, tmp_path):
    repo = _repo_name(client)
    _write_acl(tmp_path, repo, ["team-allowed"])

    client.post("/runs", json={"question": "how does search work?", "team": "team-outsider"})
    audit = client.get("/access-audit", params={"principal": "team-outsider"}).json()["entries"]
    assert len(audit) == 1
    assert audit[0]["allowed"] is False
    assert audit[0]["repo"] == repo

    threads = client.get("/threads").json()["threads"]
    assert not any(t.get("team") == "team-outsider" for t in threads)


def test_a_team_over_its_hard_quota_is_refused_with_429(client):
    repo = _repo_name(client)
    store = client.app.state.run_store
    store.conn.execute(
        "INSERT INTO runs(run_id, thread_id, request, sha, lane, status, cost_usd, state, "
        "repo, team, created_at, updated_at) "
        "VALUES('r1','t1','q','sha1','question','done',?,'{}',?,?, datetime('now'), datetime('now'))",
        (config.QUOTA_TEAM_DAILY_USD_HARD, repo, "team-broke"),
    )
    store.conn.commit()

    response = client.post("/runs", json={"question": "how does search work?", "team": "team-broke"})
    assert response.status_code == 429
    assert "team-broke" in response.json()["detail"]


def test_a_quota_refusal_still_passed_the_acl_check_first(client, tmp_path):
    """Order matters for a correct audit trail: ACL is checked (and logged)
    even for a request that will go on to be quota-blocked."""
    repo = _repo_name(client)
    _write_acl(tmp_path, repo, ["team-broke"])  # explicitly allowed
    store = client.app.state.run_store
    store.conn.execute(
        "INSERT INTO runs(run_id, thread_id, request, sha, lane, status, cost_usd, state, "
        "repo, team, created_at, updated_at) "
        "VALUES('r1','t1','q','sha1','question','done',?,'{}',?,?, datetime('now'), datetime('now'))",
        (config.QUOTA_TEAM_DAILY_USD_HARD, repo, "team-broke"),
    )
    store.conn.commit()

    response = client.post("/runs", json={"question": "how does search work?", "team": "team-broke"})
    assert response.status_code == 429
    audit = client.get("/access-audit", params={"principal": "team-broke"}).json()["entries"]
    assert audit[0]["allowed"] is True  # ACL let it through; the quota is what stopped it


def test_costs_endpoint_reflects_the_seeded_spend(client):
    repo = _repo_name(client)
    store = client.app.state.run_store
    store.conn.execute(
        "INSERT INTO runs(run_id, thread_id, request, sha, lane, status, cost_usd, state, "
        "repo, team, created_at, updated_at) "
        "VALUES('r1','t1','q','sha1','question','done',3.5,'{}',?,'team-x', datetime('now'), datetime('now'))",
        (repo,),
    )
    store.conn.commit()
    costs = client.get("/costs").json()
    assert any(row["team"] == "team-x" and row["cost_usd"] == 3.5 for row in costs["by_team"])
