"""Health alerts: turn `/health/index` from a page someone has to remember to
check into a push that lands in Slack (or any webhook that accepts a plain
`{"text": ...}` POST) on its own.

Four conditions, matching the phase's own ask exactly:

  1. lag           — commits OR minutes past config.ALERT_LAG_*
  2. parse failures — rate above config.ALERT_PARSE_FAILURE_RATE
  3. unresolved rate — JUMPED more than config.ALERT_UNRESOLVED_JUMP_PTS since
     the last check (a level is a fact about the codebase; a jump is a fact
     about the last commit — `alert_state` (db/schema.py) is the one piece of
     memory that tells them apart)
  4. queue depth    — above config.ALERT_QUEUE_DEPTH

`evaluate()` is pure — a health payload and a previous-state dict in, a list of
`Alert` out — so every rule is unit-tested against constructed payloads rather
than only ever firing for real against a live index at 3am. `send()` is the one
function that touches the network, and it is the only thing this module does
that a test has to mock rather than call directly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import config
from ..db.repo import IndexRepo
from .health import index_health


@dataclass(slots=True)
class Alert:
    repo: str
    kind: str  # lag | parse_failures | unresolved_jump | queue_depth
    message: str
    severity: str = "warning"  # warning | critical

    def as_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "kind": self.kind, "message": self.message, "severity": self.severity}


def _check_repo(repo_name: str, entry: dict[str, Any], previous_rate: float | None) -> list[Alert]:
    alerts: list[Alert] = []
    lag = entry.get("lag") or {}
    commits, minutes = lag.get("commits"), lag.get("minutes")
    if commits is not None and commits > config.ALERT_LAG_COMMITS:
        alerts.append(Alert(repo_name, "lag", f"{commits} commit(s) behind HEAD"))
    elif minutes is not None and minutes > config.ALERT_LAG_MINUTES:
        alerts.append(Alert(repo_name, "lag", f"last indexed {minutes:.0f} minute(s) ago"))

    parse_rate = entry.get("parse_failure_rate") or 0.0
    if parse_rate > config.ALERT_PARSE_FAILURE_RATE:
        alerts.append(Alert(
            repo_name, "parse_failures",
            f"parse failure rate {parse_rate:.1%}, over {config.ALERT_PARSE_FAILURE_RATE:.0%}",
            severity="critical",
        ))

    unresolved_rate = entry.get("unresolved_rate") or 0.0
    if previous_rate is not None:
        jump_pts = (unresolved_rate - previous_rate) * 100
        if jump_pts > config.ALERT_UNRESOLVED_JUMP_PTS:
            alerts.append(Alert(
                repo_name, "unresolved_jump",
                f"unresolved rate jumped {jump_pts:.1f}pts "
                f"({previous_rate:.1%} -> {unresolved_rate:.1%})",
                severity="critical",
            ))

    return alerts


def evaluate(health: dict[str, Any], previous_rates: dict[str, float]) -> list[Alert]:
    """`health` is `work.health.index_health()`'s payload; `previous_rates` is
    repo -> the unresolved rate `alert_state` held before this check."""
    alerts: list[Alert] = []
    queue_depth = (health.get("queue") or {}).get("queued", 0)
    if queue_depth > config.ALERT_QUEUE_DEPTH:
        alerts.append(Alert("*", "queue_depth", f"{queue_depth} job(s) queued"))
    for repo_name, entry in (health.get("repos") or {}).items():
        alerts.extend(_check_repo(repo_name, entry, previous_rates.get(repo_name)))
    return alerts


def format_slack(alerts: list[Alert]) -> dict[str, Any]:
    """A Slack incoming-webhook body. Plain `{"text": ...}` — no blocks, no
    attachments — so the same payload also works against a generic webhook
    receiver that just logs what it's sent."""
    lines = [f"*Bridge health alert* — {len(alerts)} issue(s)"]
    for alert in alerts:
        marker = ":red_circle:" if alert.severity == "critical" else ":warning:"
        lines.append(f"{marker} `{alert.repo}` {alert.kind}: {alert.message}")
    return {"text": "\n".join(lines)}


def send(alerts: list[Alert], webhook_url: str | None = None) -> bool:
    """POST to the configured webhook. Never raises — a dead webhook must not
    be the reason the health job itself fails and stops checking."""
    if not alerts:
        return False
    webhook_url = webhook_url or os.environ.get(config.ALERT_WEBHOOK_URL_ENV)
    if not webhook_url:
        return False
    body = json.dumps(format_slack(alerts)).encode("utf-8")
    request = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def run_health_alert_check(engine, repo_root_for=None) -> dict[str, Any]:
    """The job body: compute health, diff against last-seen, send, remember."""
    repo = IndexRepo(engine)
    health = index_health(engine, repo_root_for=repo_root_for)
    previous = {
        repo_name: state["unresolved_rate"]
        for repo_name in health.get("repos", {})
        if (state := repo.alert_state(repo_name)) is not None
    }
    alerts = evaluate(health, previous)
    sent = send(alerts)
    for repo_name, entry in health.get("repos", {}).items():
        repo.set_alert_state(repo_name, entry.get("unresolved_rate") or 0.0)
    return {"alerts": [a.as_dict() for a in alerts], "sent": sent, "checked_repos": list(health.get("repos", {}))}
