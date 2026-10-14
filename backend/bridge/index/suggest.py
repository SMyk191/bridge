"""Suggested questions — generated once at index time, served by `GET /suggestions`.

The candidate pool is mechanical (top endpoints and screens by semantic in-degree —
Python decides, per design law 1); phrasing them as natural product-language
questions is the one place a model earns its keep, because "what does
PetController#processCreationForm do" is not a question a product manager would
ever type.

Runs after the describe pass so the model has real behavioural descriptions to
write from, not bare identifiers. Falls back to a deterministic template when no
model is available — worse phrasing, but never nothing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..agents.interpreter import parse_json_object
from ..agents.llm import LLMClient, load_prompt, wrap_retrieved
from ..redact import RedactionLog


@dataclass(slots=True)
class Suggestion:
    question: str
    kind: str | None = None
    node_id: str | None = None

    def as_dict(self) -> dict:
        return {"question": self.question, "kind": self.kind, "node_id": self.node_id}


@dataclass(slots=True)
class SuggestResult:
    questions: list[Suggestion] = field(default_factory=list)
    source: str = "fallback"  # a model id, or 'fallback'
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "questions": [q.as_dict() for q in self.questions],
            "source": self.source,
            "cost_usd": round(self.cost_usd, 6),
        }


def _candidates(store) -> list:
    """Top endpoints and screens by semantic in-degree — the parts of the product
    most other code depends on, which tend to be the ones worth asking about."""
    rows = []
    for kind in ("endpoint", "screen"):
        rows.extend(
            store.conn.execute(
                "SELECT * FROM nodes WHERE kind = ? "
                "ORDER BY semantic_in_degree DESC, id LIMIT ?",
                (kind, config.SUGGESTIONS_CANDIDATE_POOL),
            ).fetchall()
        )
    return rows


def _fallback_question(row, descriptions: dict[str, str]) -> str:
    attrs = json.loads(row["attrs"])
    if row["kind"] == "endpoint":
        method = attrs.get("http_method", "").strip()
        route = attrs.get("route", row["name"])
        return f"What happens when someone sends a {method} request to {route}?".strip()
    description = descriptions.get(row["id"], "")
    if description:
        return f"What does the {row['name']} screen do — {description[0].lower()}{description[1:]}"
    return f"What is the {row['name']} screen for?"


def _fallback(candidates, descriptions: dict[str, str]) -> list[Suggestion]:
    return [
        Suggestion(
            question=_fallback_question(row, descriptions),
            kind=row["kind"],
            node_id=row["id"],
        )
        for row in candidates[: config.SUGGESTIONS_COUNT]
    ]


def run(store, llm: LLMClient, descriptions: dict[str, str], *, progress=None) -> SuggestResult:
    started = time.perf_counter()
    result = SuggestResult()
    candidates = _candidates(store)
    if not candidates:
        result.elapsed = time.perf_counter() - started
        return result

    if not llm.available_for("suggestions"):
        result.questions = _fallback(candidates, descriptions)
        result.source = "fallback"
        if progress is not None:
            progress(f"suggestions: {len(result.questions)} from fallback ({llm.reason_for('suggestions')})")
        result.elapsed = time.perf_counter() - started
        return result

    redaction = RedactionLog()
    lines = []
    for row in candidates:
        description = redaction.scrub(descriptions.get(row["id"], ""))
        lines.append(f"- [{row['kind']}] {row['name']}: {description}")

    prompt = (
        f"{wrap_retrieved(chr(10).join(lines))}\n\n"
        f"Write exactly {config.SUGGESTIONS_COUNT} suggested questions."
    )
    try:
        response = llm.complete(
            "suggestions", prompt, system=load_prompt("suggestions"), json_mode=True
        )
    except Exception:
        result.questions = _fallback(candidates, descriptions)
        result.source = "fallback"
        result.elapsed = time.perf_counter() - started
        return result

    parsed = parse_json_object(response.text)
    questions = (parsed or {}).get("questions") if parsed else None
    if not questions or response.refused:
        result.questions = _fallback(candidates, descriptions)
        result.source = "fallback"
        result.elapsed = time.perf_counter() - started
        return result

    # Model-authored questions blend material from multiple candidates, so they
    # are not attributed to one node — unlike the fallback path, which can.
    result.questions = [
        Suggestion(question=str(q).strip())
        for q in questions[: config.SUGGESTIONS_COUNT]
        if str(q).strip()
    ]
    result.source = response.model
    result.cost_usd = response.cost_usd
    result.input_tokens = response.input_tokens
    result.output_tokens = response.output_tokens
    if progress is not None:
        progress(f"suggestions: {len(result.questions)} from {response.model}")
    result.elapsed = time.perf_counter() - started
    return result


def write(out_dir: Path, sha: str, result: SuggestResult) -> Path:
    path = out_dir / config.SUGGESTIONS_FILE_NAME
    payload = {"sha": sha, **result.as_dict()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
