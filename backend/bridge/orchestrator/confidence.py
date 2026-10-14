"""Confidence, decided by rules over the evidence — not by asking a model.

Until now the number came from the cartographer: the merge prompt asked for
`high | medium | low` and the run printed whatever came back, capped at the
retrieval level. That is a model grading its own work, and it graded generously —
a bug answer that never touched the code which *saves* the thing could still come
back `high`, because nothing checked.

So the level is now computed here, from four facts that are all checkable:

1. **how good the retrieved evidence was** — the rerank thresholds in `config`
   (`RETRIEVE_*_CONFIDENCE_*`). This is the *base*, and it is the ceiling;
2. **what the mapping step admitted it could not see** — any gap caps `medium`;
3. **whether the citations cover the path the lane needs** — a bug answer needs
   both the read side and the write side, and a change to an entity needs the
   entity, a handler, and the surface a person touches;
4. **whether the citations survived verification** (`verify.py`).

The one invariant worth stating twice: **levels only ever go down.** Every rule
below is a cap or a demotion. Nothing here can promote a run above what its
evidence scored, which is what makes the number safe to print next to an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .. import config
from ..index.model import NodeKind

ORDER = config.CONFIDENCE_ORDER
LOW, MEDIUM, HIGH = ORDER


def _rank(level: str) -> int:
    try:
        return ORDER.index(level)
    except ValueError:
        return 0


def lower_of(*levels: str) -> str:
    return ORDER[min(_rank(level) for level in levels)]


def one_level_down(level: str) -> str:
    return ORDER[max(0, _rank(level) - 1)]


@dataclass(slots=True)
class ConfidenceVerdict:
    """The level, why it is that, and what would raise it."""

    level: str = LOW
    #: every rule that fired, in the order it fired — the audit trail
    reasons: list[str] = field(default_factory=list)
    #: phrases for the answer's "What would make me more sure" line
    missing: list[str] = field(default_factory=list)

    def cap(self, level: str, reason: str) -> None:
        """Lower to ``level`` if it is lower. A cap that does not bite is not recorded."""
        if _rank(level) < _rank(self.level):
            self.level = level
            self.reasons.append(reason)

    def demote(self, reason: str) -> None:
        self.level = one_level_down(self.level)
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {"level": self.level, "reasons": self.reasons, "missing": self.missing}


# --------------------------------------------------------------------------- rules


def _graph_has(store, kind: str) -> bool:
    if store is None:
        return False
    row = store.conn.execute(
        "SELECT 1 FROM nodes WHERE kind = ? LIMIT 1", (kind,)
    ).fetchone()
    return row is not None


def _bug_rule(verdict: ConfidenceVerdict, verified: Sequence) -> None:
    """A bug answer that saw only one side of the flow cannot be `high`.

    "A visit isn't appearing on the owner page" has two candidate faults: nothing
    is being *saved*, or what is saved is not being *shown*. An answer that cites
    only the display path has not ruled out the first, and an answer that cites
    only the write path has not ruled out the second. Either way half the flow is
    unexamined — and saying so is more useful than a confident guess about which
    half.
    """
    has_read = any(citation.is_read for citation in verified)
    has_write = any(citation.is_persisting_write for citation in verified)
    if has_read and has_write:
        return
    if not has_read and not has_write:
        verdict.cap(
            MEDIUM,
            "a bug needs both sides of the flow and the answer cites neither the code that "
            "displays this nor the code that saves it",
        )
        verdict.missing.append(
            "the code that saves this and the code that displays it — the answer cites neither, "
            "so neither side of the flow has been ruled out"
        )
        return
    if has_write:
        verdict.cap(
            MEDIUM,
            "a bug needs both sides of the flow and the answer cites no read/display path",
        )
        verdict.missing.append(
            "the screen or read handler that displays this — the answer only examined the code "
            "that writes it, so a display-side fault has not been ruled out"
        )
    else:
        verdict.cap(
            MEDIUM,
            "a bug needs both sides of the flow and the answer cites no write/persist path",
        )
        verdict.missing.append(
            "the endpoint or repository call that saves this — the answer only examined the code "
            "that displays it, so a fault in what gets written has not been ruled out"
        )


def _change_request_rule(verdict: ConfidenceVerdict, verified: Sequence, store) -> None:
    """A change to a stored thing lands in three places, or it is not fully mapped.

    Add a field to an entity and you touch the entity, the handler that accepts
    it, and the surface a person types it into. An impact map missing one of the
    three has not shown the blast radius — so the level is capped and the missing
    layer is named. The screen/endpoint half is only required **when the graph has
    any**: a headless service has no screens, and demanding one would punish an
    answer for a layer that does not exist.
    """
    touches_entity = any(NodeKind.ENTITY in citation.kinds for citation in verified)
    if not touches_entity:
        return  # not an entity-shaped change; this rule has nothing to say

    has_handler = any(
        citation.io_source == config.IO_SOURCE_ROUTE for citation in verified
    )
    has_surface = any(
        NodeKind.SCREEN in citation.kinds or NodeKind.ENDPOINT in citation.kinds
        for citation in verified
    )
    surface_exists = _graph_has(store, NodeKind.SCREEN) or _graph_has(store, NodeKind.ENDPOINT)

    if not has_handler:
        verdict.cap(
            MEDIUM,
            "a change to a stored entity needs the handler that accepts it, and none is cited",
        )
        verdict.missing.append(
            "the request handler that accepts this field — without it the map cannot show where "
            "the new value enters the system"
        )
    if surface_exists and not has_surface:
        verdict.cap(
            MEDIUM,
            "a change to a stored entity needs the screen or endpoint a person touches, and "
            "none is cited",
        )
        verdict.missing.append(
            "the screen or endpoint a person would use to enter this — the graph has them, so "
            "one should have been reachable"
        )


def _question_rule(verdict: ConfidenceVerdict, impact_map) -> None:
    """A walkthrough with a hole in it is not a `high`-confidence walkthrough.

    The gap cap below does the work for every lane; this states it for the
    question lane explicitly, because "no gap on the main narrative path" is the
    whole bar a walkthrough has to clear.
    """
    gaps = list(getattr(impact_map, "gaps", None) or [])
    if gaps:
        verdict.cap(
            MEDIUM,
            f"the walkthrough's path has {len(gaps)} unexplained gap(s)",
        )


# --------------------------------------------------------------------------- entry


def _followup_reasons(verdict: ConfidenceVerdict, followups) -> None:
    """Say when the run knew what was missing but could not afford to go get it.

    A shot skipped for budget is a specific, actionable thing: the rules spotted
    a hole, named it, and ran out of room. That is materially different from an
    answer with no known holes, and a reader deciding whether to trust the level
    should be told which one they have.
    """
    if not followups:
        return
    for shot in followups.get("shots") or []:
        if shot.get("fired") or not shot.get("skipped_reason"):
            continue
        verdict.reasons.append(
            f"a follow-up search was warranted but not run — {shot['reason']}: "
            f"{shot['skipped_reason']}"
        )
        verdict.missing.append(
            f"the search this run could not afford: {shot['reason']}"
        )


def score_confidence(
    lane: str | None,
    retrieval,
    impact_map,
    verified_citations: Sequence,
    *,
    verification=None,
    store=None,
    followups=None,
) -> ConfidenceVerdict:
    """The run's confidence level, and every reason it is not higher.

    ``verified_citations`` are :class:`verify.VerifiedCitation` values — what the
    answer actually cited *and* the graph confirmed. Anything the answer claimed
    that did not verify is not evidence, so it is not counted here; the size of
    that shortfall arrives separately as ``verification``.
    """
    base = getattr(retrieval, "confidence", None) or LOW
    reason = getattr(retrieval, "confidence_reason", "") or "no retrieval was performed"
    verdict = ConfidenceVerdict(level=base if base in ORDER else LOW)
    verdict.reasons.append(f"evidence scored {verdict.level} on retrieval: {reason}")

    # --- nothing checkable at all
    if not verified_citations:
        # Two different failures, and a reader deserves to know which. An answer
        # that cited nothing has broken the format; one whose every citation
        # failed has cited code that is not there. Both are `low`: the promise of
        # this system is a claim you can check, and neither delivers one.
        attempted = getattr(verification, "checked", 0)
        if attempted:
            verdict.cap(
                LOW,
                f"none of the {attempted} citations in this answer could be verified "
                "against the indexed commit",
            )
            verdict.missing.append(
                "a citation that resolves to real code — nothing this answer pointed at could "
                "be checked against the index"
            )
        else:
            verdict.cap(
                LOW,
                "the answer carries no citations, so none of it could be checked",
            )
            verdict.missing.append(
                "citations in the answer itself — it names files but never in the "
                "`repo@sha:path:start-end` form, so no claim in it can be checked"
            )
        _followup_reasons(verdict, followups)
        return verdict

    # --- what the mapping step admitted it could not see, for every lane
    gaps = list(getattr(impact_map, "gaps", None) or [])
    if gaps:
        verdict.cap(MEDIUM, f"the mapping step reported {len(gaps)} gap(s): {gaps[0]}")
        # A gap *is* the thing that would raise the level, so it belongs in the
        # answer's own "what would make me more sure" line and not only in the
        # trace. Two at most: they are sentences, not labels.
        verdict.missing.extend(gap.rstrip(".") for gap in gaps[:2])

    # --- lane-specific path coverage
    if lane == "bug":
        _bug_rule(verdict, verified_citations)
    elif lane == "change_request":
        _change_request_rule(verdict, verified_citations, store)
    elif lane == "question":
        _question_rule(verdict, impact_map)

    # --- citations that did not survive verification
    if verification is not None and verification.dropped:
        dropped = len(verification.dropped)
        if verification.drop_ratio > config.VERIFY_DROP_RATIO_FOR_DOWNGRADE:
            verdict.demote(
                f"{dropped} of {verification.checked} citations could not be verified "
                f"({verification.drop_ratio:.0%} of them) and were removed"
            )
        else:
            verdict.reasons.append(
                f"{dropped} citation(s) could not be verified and were removed, below the "
                f"{config.VERIFY_DROP_RATIO_FOR_DOWNGRADE:.0%} threshold that costs a level"
            )
        verdict.missing.append(
            f"a check on {dropped} citation(s) this answer made that do not resolve to code at "
            f"the indexed commit"
        )

    _followup_reasons(verdict, followups)
    return verdict


def removal_note(verification) -> str:
    """The sentence appended to an answer whose citations were removed."""
    dropped = len(verification.dropped)
    return (
        f"{dropped} citation{'s' if dropped != 1 else ''} could not be verified "
        f"and {'were' if dropped != 1 else 'was'} removed."
    )
