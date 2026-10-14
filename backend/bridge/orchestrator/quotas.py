"""Cost quotas: a team spending too much, or a lane costing too much, on a
given day.

Two different enforcement points, because the two things being capped become
knowable at two different moments:

* **Team** accompanies the request — `check_team_quota` runs BEFORE anything is
  spent, and a hard-exceeded team is refused outright (api/main.py returns
  429). This is the only quota check that can ever *stop* a run before it
  starts.
* **Lane** is the one thing design law 3 says must never be known before
  retrieval and analysis have happened — routing is a consequence of the
  impact map, not of how the question was phrased. So a lane cap cannot be a
  precondition; it can only tighten the run's OWN budget once the lane is
  decided (`apply_lane_cap`, called from `orchestrator/run.py` right after
  `state.lane` is set), reusing the existing budget/abort machinery (design
  law 6) rather than inventing a second way for a run to stop early. A run
  that would blow its lane's daily cap does not error — it aborts partway
  with a partial answer, exactly like any other budget-exhausted run.

Both are soft-then-hard for the team dimension (a warning most of the day, a
wall at the ceiling) and hard-only for lanes (there is no such thing as a
"soft" tighter budget — the run either fits or it doesn't, same as any other
budget ceiling).
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import config


@dataclass(slots=True)
class QuotaDecision:
    team: str
    spent_today: float
    soft_limit: float
    hard_limit: float

    @property
    def blocked(self) -> bool:
        return self.spent_today >= self.hard_limit

    @property
    def warning(self) -> str | None:
        if self.blocked:
            return None  # blocked is a refusal, not a warning on a run that proceeded
        if self.spent_today >= self.soft_limit:
            return (
                f"team {self.team!r} has spent ${self.spent_today:.2f} today, "
                f"over its ${self.soft_limit:.2f} soft quota "
                f"(${self.hard_limit:.2f} hard limit)"
            )
        return None

    def as_dict(self) -> dict:
        return {
            "team": self.team, "spent_today": round(self.spent_today, 6),
            "soft_limit": self.soft_limit, "hard_limit": self.hard_limit,
            "blocked": self.blocked, "warning": self.warning,
        }


def check_team_quota(store, team: str) -> QuotaDecision:
    spent = store.cost_today(team=team)
    return QuotaDecision(
        team=team, spent_today=spent,
        soft_limit=config.QUOTA_TEAM_DAILY_USD_SOFT, hard_limit=config.QUOTA_TEAM_DAILY_USD_HARD,
    )


def lane_budget_ceiling(store, lane: str, current_max_usd: float | None) -> float | None:
    """The tightened `max_usd` for THIS run, or None if the lane has no cap or
    the existing ceiling is already tighter.

    Only ever tightens. A lane with room left under its daily cap must never
    let a run spend MORE than its own budget already allowed — this function
    hands back a number strictly meant to shrink `state.budget.max_usd`, never
    grow it.
    """
    cap = config.QUOTA_LANE_DAILY_USD.get(lane)
    if cap is None:
        return None
    spent = store.cost_today(lane=lane)
    remaining = max(0.0, cap - spent)
    if current_max_usd is not None and current_max_usd <= remaining:
        return None
    return remaining
