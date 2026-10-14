"""work/alerts.py — the four alert rules, pure, plus IndexRepo's alert_state
round-trip against a real Postgres (the one piece of memory a jump detector
needs). `send()`'s network call is exercised only through a stub, deliberately
— a test that actually hit Slack would be a test of Slack's uptime, not this
module's logic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bridge import config
from bridge.db import create_all, get_engine
from bridge.db.repo import IndexRepo
from bridge.work import alerts


def _health(*, commits=0, minutes=0.0, parse_rate=0.0, unresolved=0.0, queued=0) -> dict:
    return {
        "queue": {"queued": queued},
        "repos": {
            "demo": {
                "lag": {"commits": commits, "minutes": minutes},
                "parse_failure_rate": parse_rate,
                "unresolved_rate": unresolved,
            }
        },
    }


# ------------------------------------------------------------------ evaluate()


def test_a_healthy_snapshot_raises_nothing():
    assert alerts.evaluate(_health(), {}) == []


def test_lag_by_commits_fires_over_the_threshold():
    health = _health(commits=config.ALERT_LAG_COMMITS + 1)
    fired = alerts.evaluate(health, {})
    assert [a.kind for a in fired] == ["lag"]
    assert "commit" in fired[0].message


def test_lag_by_minutes_fires_when_commits_is_zero_but_stale():
    """A repo nobody has pushed to isn't "behind" by commit count, but it can
    still be stale by age — the two lag readings are independent."""
    health = _health(commits=0, minutes=config.ALERT_LAG_MINUTES + 1)
    fired = alerts.evaluate(health, {})
    assert [a.kind for a in fired] == ["lag"]
    assert "minute" in fired[0].message


def test_parse_failure_rate_fires_and_is_critical():
    health = _health(parse_rate=config.ALERT_PARSE_FAILURE_RATE + 0.01)
    fired = alerts.evaluate(health, {})
    assert [a.kind for a in fired] == ["parse_failures"]
    assert fired[0].severity == "critical"


def test_an_unresolved_LEVEL_alone_does_not_fire_without_a_prior_reading():
    """A first-ever check has nothing to compare against — it can only
    establish the baseline, never call a level a jump."""
    health = _health(unresolved=0.40)
    assert alerts.evaluate(health, {}) == []


def test_an_unresolved_jump_fires_only_past_the_threshold():
    previous = {"demo": 0.08}
    small_jump = _health(unresolved=0.08 + (config.ALERT_UNRESOLVED_JUMP_PTS - 1) / 100)
    assert alerts.evaluate(small_jump, previous) == []

    big_jump = _health(unresolved=0.08 + (config.ALERT_UNRESOLVED_JUMP_PTS + 1) / 100)
    fired = alerts.evaluate(big_jump, previous)
    assert [a.kind for a in fired] == ["unresolved_jump"]


def test_unresolved_dropping_is_not_an_alert():
    previous = {"demo": 0.40}
    fired = alerts.evaluate(_health(unresolved=0.05), previous)
    assert fired == []


def test_queue_depth_fires_and_is_repo_agnostic():
    fired = alerts.evaluate(_health(queued=config.ALERT_QUEUE_DEPTH + 1), {})
    assert [a.kind for a in fired] == ["queue_depth"]
    assert fired[0].repo == "*"


def test_multiple_conditions_all_fire_together():
    health = _health(commits=config.ALERT_LAG_COMMITS + 1,
                     parse_rate=config.ALERT_PARSE_FAILURE_RATE + 0.01,
                     queued=config.ALERT_QUEUE_DEPTH + 1)
    fired = alerts.evaluate(health, {})
    assert {a.kind for a in fired} == {"lag", "parse_failures", "queue_depth"}


def test_two_repos_are_checked_independently():
    health = {
        "queue": {"queued": 0},
        "repos": {
            "healthy": {"lag": {"commits": 0, "minutes": 0}, "parse_failure_rate": 0, "unresolved_rate": 0},
            "sick": {"lag": {"commits": config.ALERT_LAG_COMMITS + 1, "minutes": 0},
                     "parse_failure_rate": 0, "unresolved_rate": 0},
        },
    }
    fired = alerts.evaluate(health, {})
    assert [a.repo for a in fired] == ["sick"]


# ------------------------------------------------------------------ format_slack / send


def test_format_slack_names_every_alert():
    fired = [alerts.Alert("demo", "lag", "5 behind"), alerts.Alert("demo", "queue_depth", "80 queued")]
    body = alerts.format_slack(fired)
    assert "2 issue(s)" in body["text"]
    assert "5 behind" in body["text"] and "80 queued" in body["text"]


def test_send_does_nothing_with_no_webhook_configured(monkeypatch):
    monkeypatch.delenv(config.ALERT_WEBHOOK_URL_ENV, raising=False)
    assert alerts.send([alerts.Alert("demo", "lag", "x")], webhook_url=None) is False


def test_send_does_nothing_with_no_alerts():
    assert alerts.send([], webhook_url="https://example.invalid/webhook") is False


def test_send_posts_to_the_configured_url():
    fired = [alerts.Alert("demo", "lag", "x")]
    with patch("bridge.work.alerts.urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.status = 200
        result = alerts.send(fired, webhook_url="https://example.invalid/webhook")
    assert result is True
    request = mock_open.call_args[0][0]
    assert request.full_url == "https://example.invalid/webhook"


def test_send_reports_failure_without_raising_on_a_dead_webhook():
    import urllib.error

    with patch("bridge.work.alerts.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        result = alerts.send([alerts.Alert("demo", "lag", "x")], webhook_url="https://example.invalid/webhook")
    assert result is False


# ------------------------------------------------------------------ alert_state persistence


@pytest.fixture()
def repo():
    pytest.importorskip("pgserver")
    engine = get_engine("embedded://pytest_alerts")
    create_all(engine)
    with engine.begin() as connection:
        from sqlalchemy import text

        connection.execute(text('TRUNCATE TABLE "alert_state" CASCADE'))
    return IndexRepo(engine)


def test_alert_state_is_none_before_the_first_check(repo):
    assert repo.alert_state("demo") is None


def test_alert_state_round_trips(repo):
    repo.set_alert_state("demo", 0.12)
    state = repo.alert_state("demo")
    assert state["unresolved_rate"] == pytest.approx(0.12)


def test_setting_alert_state_twice_updates_rather_than_duplicates(repo):
    repo.set_alert_state("demo", 0.12)
    repo.set_alert_state("demo", 0.30)
    state = repo.alert_state("demo")
    assert state["unresolved_rate"] == pytest.approx(0.30)


def test_run_health_alert_check_updates_state_and_reports_what_it_saw(repo):
    """End to end against a constructed health payload, real Postgres for the
    state it reads back — everything except the network POST."""
    with patch("bridge.work.alerts.index_health", return_value=_health(unresolved=0.5)), \
         patch("bridge.work.alerts.send", return_value=False) as mock_send:
        result = alerts.run_health_alert_check(repo.engine)
    assert result["checked_repos"] == ["demo"]
    assert repo.alert_state("demo")["unresolved_rate"] == pytest.approx(0.5)
    mock_send.assert_called_once()
