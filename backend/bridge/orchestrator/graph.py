"""Node sequence and lane rules. Plain Python — no model decides control flow.

Design law 1: edges are deterministic, nodes are probabilistic. The sequence
below is fixed, and :func:`decide_lane` is a rule table you can read, test, and
argue with.

Design law 3: the lane is set **after** the cartographer, from the impact map —
not from how the request was phrased. The interpreter's `intent` is evidence, not
the verdict. The rule that earns this is the downgrade: an ask that *sounds* like
a change request but produced no impact map cannot be planned, so it becomes a
question instead of a confidently-empty brief.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import config
from ..agents.state import RunState

#: Node order for every lane whose evidence comes from retrieval. The two writers
#: both appear here and both are gated: `architect` runs for config.ARCHITECT_LANES,
#: `explainer` for config.EXPLAINER_LANES. Naming both in the sequence (rather than
#: dispatching to "the writer") keeps the persisted trace honest about which one ran.
#: `followup` sits between the map and everything downstream, because that is the
#: first point at which the *evidence* can be judged incomplete (followup.py).
#: `verifier` runs last and for every lane: it checks the citations the writer
#: made and scores the run's confidence from rules (verify.py, confidence.py).
#: Neither calls a model to decide anything — a node here is a step, not an agent.
NODE_SEQUENCE = (
    "interpreter", "cartographer", "followup", "historian", "architect", "explainer", "verifier",
)

#: The changelog lane's whole sequence. It shares no node with the one above,
#: because it shares no evidence: its facts come from `git log`, not from search.
CHANGELOG_SEQUENCE = ("changelog",)


@dataclass(slots=True)
class LaneDecision:
    lane: str
    reason: str
    #: True when the map was too thin to support the intent the user expressed
    downgraded: bool = False


def decide_lane(state: RunState) -> LaneDecision:
    """Choose the lane from the impact map plus the interpreter's intent."""
    intent = state.interpretation.intent if state.interpretation else "question"
    impact = state.impact_map

    # A data question is about production records, not code. No amount of impact
    # map changes that, so it short-circuits regardless of what was found.
    if intent == "data_question":
        return LaneDecision("data_question", "intent is about production data, not code")

    evidence = impact.total if impact else 0
    confidence = impact.confidence if impact else "low"

    if intent in config.ARCHITECT_LANES:
        if evidence == 0:
            return LaneDecision(
                "question",
                f"intent was {intent!r} but the impact map is empty — "
                "there is nothing to write a brief against",
                downgraded=True,
            )
        if confidence == "low" and evidence < 3:
            return LaneDecision(
                "question",
                f"intent was {intent!r} but the impact map has only {evidence} "
                "entries at low confidence — too thin to plan a change",
                downgraded=True,
            )
        return LaneDecision(intent, f"intent {intent!r} with {evidence} impacted entries")

    # A question stays a question. We deliberately do NOT promote it to a change
    # request just because the map is rich: the user asked to understand, and
    # handing them a work plan they did not ask for is its own failure.
    return LaneDecision("question", f"intent {intent!r}")


def wants_architect(lane: str | None) -> bool:
    return lane in config.ARCHITECT_LANES


def wants_explainer(lane: str | None) -> bool:
    return lane in config.EXPLAINER_LANES


# --------------------------------------------------------------------- changelog


#: "what changed" and its neighbours.
_CHANGE_PHRASES = re.compile(
    r"\b(?:"
    r"what(?:'s|s)?\s+(?:has\s+|have\s+|had\s+)?chang(?:ed|es|ing)"
    r"|what\s+(?:shipped|landed|went\s+out)"
    r"|what\s+did\s+(?:we|the\s+team|you)\s+(?:change|ship|land)"
    r")\b",
    re.IGNORECASE,
)

#: Words that are a release reference *by themselves* — asking for "the changelog"
#: is asking about a range even though no period is named, so these carry the
#: default window rather than being rejected for having no time word.
_SELF_SUFFICIENT = re.compile(r"\b(?:changelog|change\s+log|release\s+notes)\b", re.IGNORECASE)

#: A time or release reference. Required alongside a "what changed" phrase, so
#: "what changes when I add a weight field?" — a change_request wearing the same
#: words — is not mistaken for a request to read history.
_TIME_REFERENCE = re.compile(
    r"\b(?:"
    r"(?:last|past|previous|this)\s+(?:week|month|sprint|quarter|release|version|few\s+\w+)"
    r"|(?:last|past)\s+\d+\s*(?:commits?|days?|weeks?|months?|releases?)"
    r"|since\s+[\w./@-]+"
    r"|in\s+the\s+last\s+\d+"
    r"|recently|lately|yesterday"
    r"|v?\d+\.\d+(?:\.\d+)?"
    r")\b",
    re.IGNORECASE,
)

_COMMIT_COUNT = re.compile(r"\b(?:last|past)\s+(\d{1,3})\s*commits?\b", re.IGNORECASE)

#: "last 3 weeks" / "past 10 days" -> a git --since expression
_SINCE_COUNTED = re.compile(
    r"\b(?:last|past|previous)\s+(\d{1,3})\s*(day|week|month|quarter|year)s?\b", re.IGNORECASE
)
#: bare periods, in the order they should be tried (longest phrase first)
_SINCE_WORDS = (
    (re.compile(r"\byesterday\b", re.IGNORECASE), "1.day.ago"),
    (re.compile(r"\b(?:last|past|this)\s+week\b", re.IGNORECASE), "1.week.ago"),
    (re.compile(r"\b(?:last|past|this)\s+month\b", re.IGNORECASE), "1.month.ago"),
    (re.compile(r"\b(?:last|past|this)\s+sprint\b", re.IGNORECASE), "2.weeks.ago"),
    (re.compile(r"\b(?:last|past|this)\s+quarter\b", re.IGNORECASE), "3.months.ago"),
)


def _since_expression(text: str) -> str | None:
    """A `git log --since` value for the period named in ``text``, if any.

    "What changed last week" is a question about a *period*, and answering it with
    "the last 20 commits" would quietly substitute a different question. A sprint
    is taken as two weeks — an assumption, so the answer states the resolved
    window rather than the words it came from.
    """
    counted = _SINCE_COUNTED.search(text)
    if counted:
        amount, unit = counted.group(1), counted.group(2).lower()
        if unit == "quarter":
            return f"{int(amount) * 3}.months.ago"
        return f"{amount}.{unit}s.ago"
    for pattern, expression in _SINCE_WORDS:
        if pattern.search(text):
            return expression
    return None


@dataclass(slots=True)
class ChangelogAsk:
    """A resolved "what changed" request: which commits, and how we decided."""

    from_sha: str | None = None
    to_sha: str = "HEAD"
    commits: int = config.CHANGELOG_DEFAULT_COMMITS
    #: a `git log --since` expression, when the ask named a period rather than a
    #: number of commits. `commits` still caps how many are read.
    since: str | None = None
    #: `params` (explicit shas) or `phrase` (matched the rule below)
    source: str = "phrase"
    reason: str = ""

    @property
    def label(self) -> str:
        if self.from_sha:
            return f"`{self.from_sha[:8]}..{self.to_sha[:8]}`"
        if self.since:
            window = self.since.replace(".ago", "").replace(".", " ")
            # "1 week" -> "week", so the sentence reads "the last week"
            window = window[2:].rstrip("s") if window.startswith("1 ") else window
            return f"the last {window}"
        return f"the last {self.commits} commits"

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_sha": self.from_sha,
            "to_sha": self.to_sha,
            "commits": self.commits,
            "since": self.since,
            "source": self.source,
            "reason": self.reason,
            "label": self.label,
        }


def changelog_trigger(request: str, params: dict[str, Any] | None = None) -> ChangelogAsk | None:
    """Whether this ask is a changelog, and over what range. Rules, not a model.

    Design law 3 says the lane comes from analysis rather than from phrasing, and
    this is the documented exception — with a reason. Every other lane routes off
    the impact map because retrieval is its evidence. A changelog's evidence is a
    commit range: there is no impact map to route from, and running the whole
    retrieval pipeline first would spend a reranker call to discover that the
    question was never about a place in the code. So the decision is made up
    front — but it is still made by this rule table, in Python, and never by a
    model. The rule is deliberately narrow: an explicit range always wins, and
    otherwise BOTH halves must be present.
    """
    params = params or {}
    from_sha = (params.get("from_sha") or "").strip() or None
    to_sha = (params.get("to_sha") or "").strip() or None
    if from_sha or to_sha:
        return ChangelogAsk(
            from_sha=from_sha,
            to_sha=to_sha or "HEAD",
            source="params",
            reason="an explicit commit range was requested",
        )

    text = request or ""
    named_range = _SELF_SUFFICIENT.search(text)
    if not (named_range or (_CHANGE_PHRASES.search(text) and _TIME_REFERENCE.search(text))):
        return None

    count = _COMMIT_COUNT.search(text)
    # An explicit commit count wins over a period: "the last 10 commits" is
    # already exact, and mixing it with --since would silently shrink it.
    if count:
        commits, since = min(int(count.group(1)), config.CHANGELOG_MAX_COMMITS), None
    else:
        since = _since_expression(text)
        commits = config.CHANGELOG_MAX_COMMITS if since else config.CHANGELOG_DEFAULT_COMMITS
    reason = (
        "asked for a changelog"
        if named_range and not _CHANGE_PHRASES.search(text)
        else "asked what changed, over a named period"
    )
    return ChangelogAsk(commits=commits, since=since, source="phrase", reason=reason)


def plan_nodes(state: RunState) -> tuple[str, ...]:
    """The node sequence for this run, chosen before the first node runs.

    Only ever returns one of two fixed tuples. This is the single place where a
    run can take a different shape, and it takes it for exactly one reason.
    """
    if changelog_trigger(state.request, state.params) is not None:
        return CHANGELOG_SEQUENCE
    return NODE_SEQUENCE


DATA_QUESTION_REDIRECT = (
    "This looks like a question about **production data**, not about how the code works.\n\n"
    "Bridge reads a repository — it indexes code, screens, and database *schema*, but it has "
    "no access to a running system or its records, so it cannot count or aggregate anything.\n\n"
    "Two things it can do instead:\n\n"
    "- **Tell you where that number lives.** Ask \"where is X stored?\" or \"which table holds "
    "X?\" and it will point at the entity, the table, and the code that writes to it — with "
    "citations.\n"
    "- **Tell you what would have to change** to start capturing something not recorded today.\n\n"
    "For the number itself you want whoever can query the database or the analytics warehouse."
)
