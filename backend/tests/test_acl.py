"""security/acl.py — the static provider, the default-open policy, and the
audit log it feeds. `GitHubACLProvider` is asserted to be exactly what it
claims to be: not implemented, with a clear list of what implementing it
needs.
"""

from __future__ import annotations

import pytest

from bridge import config
from bridge.orchestrator.store import RunStore
from bridge.security import acl


@pytest.fixture()
def config_root(tmp_path):
    return tmp_path


def _write_acl(config_root, repo: str, readable_by: list[str]) -> None:
    (config_root / f"acl.{repo}.yaml").write_text(
        f"repo: {repo}\nreadable_by:\n" + "\n".join(f"  - {p}" for p in readable_by) + "\n"
    )


# ------------------------------------------------------------------ AclPolicy / StaticACLProvider


def test_a_repo_with_no_acl_file_defers_to_the_default_policy(config_root):
    provider = acl.StaticACLProvider(config_root)
    assert provider.can_read("anyone", "demo") is None


def test_a_listed_principal_is_allowed(config_root):
    _write_acl(config_root, "demo", ["team-a", "team-b"])
    provider = acl.StaticACLProvider(config_root)
    assert provider.can_read("team-a", "demo") is True


def test_an_unlisted_principal_is_denied(config_root):
    _write_acl(config_root, "demo", ["team-a"])
    provider = acl.StaticACLProvider(config_root)
    assert provider.can_read("team-b", "demo") is False


def test_two_repos_have_independent_policies(config_root):
    _write_acl(config_root, "demo", ["team-a"])
    _write_acl(config_root, "other", ["team-b"])
    provider = acl.StaticACLProvider(config_root)
    assert provider.can_read("team-a", "demo") is True
    assert provider.can_read("team-a", "other") is False


def test_the_policy_is_cached_after_first_load(config_root):
    _write_acl(config_root, "demo", ["team-a"])
    provider = acl.StaticACLProvider(config_root)
    assert provider.can_read("team-a", "demo") is True
    (config_root / "acl.demo.yaml").unlink()
    # still True — the cache, not a fresh disk read, answers the second call
    assert provider.can_read("team-a", "demo") is True


# ------------------------------------------------------------------ check_access


def test_no_policy_open_default_allows_everyone(config_root, monkeypatch):
    monkeypatch.setattr(config, "ACL_DEFAULT_OPEN", True)
    provider = acl.StaticACLProvider(config_root)
    decision = acl.check_access(provider, "anyone", "demo")
    assert decision.allowed
    assert "default is open" in decision.reason


def test_no_policy_closed_default_denies_everyone(config_root, monkeypatch):
    monkeypatch.setattr(config, "ACL_DEFAULT_OPEN", False)
    provider = acl.StaticACLProvider(config_root)
    decision = acl.check_access(provider, "anyone", "demo")
    assert not decision.allowed
    assert "default is closed" in decision.reason


def test_an_explicit_policy_always_wins_over_the_default(config_root, monkeypatch):
    monkeypatch.setattr(config, "ACL_DEFAULT_OPEN", True)
    _write_acl(config_root, "demo", ["team-a"])
    provider = acl.StaticACLProvider(config_root)
    decision = acl.check_access(provider, "team-b", "demo")
    assert not decision.allowed
    assert "not listed" in decision.reason


def test_decision_reasons_name_the_principal_and_repo(config_root):
    _write_acl(config_root, "demo", ["team-a"])
    provider = acl.StaticACLProvider(config_root)
    decision = acl.check_access(provider, "team-a", "demo")
    assert "team-a" in decision.reason and "demo" in decision.reason


# ------------------------------------------------------------------ GitHubACLProvider


def test_github_provider_is_an_honest_stub():
    provider = acl.GitHubACLProvider()
    with pytest.raises(NotImplementedError) as error:
        provider.can_read("team-a", "demo")
    assert "GitHub App" in str(error.value) or "PAT" in str(error.value)
    assert "cache" in str(error.value).lower()


# ------------------------------------------------------------------ filter_unauthorized_chunks


class _Chunk:
    def __init__(self, project: str) -> None:
        self.project = project


def test_no_project_restriction_passes_everything_through():
    chunks = [_Chunk("a"), _Chunk("b")]
    assert acl.filter_unauthorized_chunks(chunks, None) == chunks


def test_a_project_restriction_filters_to_the_allowed_set():
    chunks = [_Chunk("a"), _Chunk("b")]
    kept = acl.filter_unauthorized_chunks(chunks, {"a"})
    assert kept == [chunks[0]]


def test_a_chunk_with_no_project_attribute_is_excluded_under_a_restriction():
    class NoProject:
        pass

    kept = acl.filter_unauthorized_chunks([NoProject()], {"a"})
    assert kept == []


# ------------------------------------------------------------------ RunStore audit log


@pytest.fixture()
def store(tmp_path):
    with RunStore.open(tmp_path / "runs.sqlite") as s:
        yield s


def test_record_access_writes_a_row_readable_back(store):
    store.record_access(principal="team-a", repo="demo", question="how does X work?",
                        sha="abc123", allowed=True, reason="listed")
    rows = store.access_audit()
    assert len(rows) == 1
    assert rows[0]["principal"] == "team-a"
    assert bool(rows[0]["allowed"]) is True


def test_a_denial_is_recorded_too(store):
    store.record_access(principal="team-b", repo="demo", question="q", sha=None,
                        allowed=False, reason="not listed")
    rows = store.access_audit()
    assert bool(rows[0]["allowed"]) is False


def test_access_audit_filters_by_repo_and_principal(store):
    store.record_access(principal="a", repo="r1", question="q", sha=None, allowed=True, reason="x")
    store.record_access(principal="b", repo="r2", question="q", sha=None, allowed=True, reason="x")
    assert len(store.access_audit(repo="r1")) == 1
    assert len(store.access_audit(principal="b")) == 1
    assert len(store.access_audit()) == 2


def test_access_audit_is_newest_first(store):
    store.record_access(principal="a", repo="r1", question="first", sha=None, allowed=True, reason="x")
    store.record_access(principal="a", repo="r1", question="second", sha=None, allowed=True, reason="x")
    rows = store.access_audit()
    assert rows[0]["question"] == "second"
