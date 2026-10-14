"""Description versions, and how a prompt change stops being a rebuild.

A description is keyed by `(code_hash, model, prompt_version)` and its embedding
by `(desc_key, embed_model)`. Change any of those three and every `desc_key` in
the database becomes a *different* key — which is correct, and which is also how
a one-word prompt edit turns into a bill for re-describing an entire monorepo.

So the migration is lazy, and that word has a precise meaning here:

* **Nothing is recomputed when the version changes.** Bumping `PROMPT_VERSION`
  costs zero model calls at the moment of the bump. The old rows stay exactly
  where they are and stay readable.
* **Retrieval prefers the newest version it has, per chunk, and falls back.**
  Not per query and not per repo — per `code_hash`, because a half-migrated index
  is the normal state and the alternative (refuse to answer until migration
  finishes) makes the bump the outage it was supposed to avoid.
* **Backfill closes the gap in the background**, ordered by what leaving a chunk
  on the old version costs, which is the same priority the describe backfill
  already uses.

The consequence to keep in mind: a single answer can cite chunks described at two
different prompt versions. That is a real inconsistency, and it is a smaller one
than either of the alternatives. `coverage()` makes it visible instead of quiet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import Engine, func, select

from .. import config
from . import keys, schema


@dataclass(frozen=True, slots=True)
class IndexVersion:
    """The triple that decides whether a stored description can be reused."""

    model: str
    prompt_version: str
    embed_model: str

    @classmethod
    def current(cls) -> "IndexVersion":
        return cls(
            model=config.ROLES["describe"]["model"],
            prompt_version=keys.PROMPT_VERSION,
            embed_model=config.EMBED_MODEL,
        )

    @property
    def label(self) -> str:
        return f"{self.model}|{self.prompt_version}|{self.embed_model}"

    def desc_key_for(self, code_hash: str) -> str:
        return keys.desc_key(code_hash, self.model, self.prompt_version)

    def vec_key_for(self, desc_key: str) -> str:
        return keys.vec_key(desc_key, self.embed_model)

    def as_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "embed_model": self.embed_model,
        }


def preference_order(engine: Engine) -> list[tuple[str, str]]:
    """`(model, prompt_version)` pairs, best first.

    The current version wins; then anything an operator pinned in
    `DESCRIPTION_VERSIONS_PREFERRED` (the roll-back lever — if a new prompt turns
    out worse, naming the old one here restores it without deleting the new rows);
    then whatever else the database holds, newest first by when it was written.
    """
    current = IndexVersion.current()
    ordered: list[tuple[str, str]] = [(current.model, current.prompt_version)]
    for entry in config.DESCRIPTION_VERSIONS_PREFERRED:
        model, _, prompt_version = entry.partition("|")
        pair = (model, prompt_version or keys.PROMPT_VERSION)
        if pair not in ordered:
            ordered.append(pair)

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                schema.descriptions.c.model,
                schema.descriptions.c.prompt_version,
                func.max(schema.descriptions.c.created_at).label("newest"),
            )
            .group_by(schema.descriptions.c.model, schema.descriptions.c.prompt_version)
            .order_by(func.max(schema.descriptions.c.created_at).desc())
        ).all()
    for model, prompt_version, _newest in rows:
        if (model, prompt_version) not in ordered:
            ordered.append((model, prompt_version))
    return ordered


def resolve(engine: Engine, code_hashes: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The best available description for each code hash.

    One query, then the choice is made in Python against `preference_order`. A
    SQL `DISTINCT ON` with a CASE-built rank would do it in the database, but the
    ranking depends on rows the same query is selecting (which versions exist at
    all), and expressing that in one statement makes it unreadable for no gain at
    this size.
    """
    if not code_hashes:
        return {}
    order = {pair: rank for rank, pair in enumerate(preference_order(engine))}
    fallback_rank = len(order)

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                schema.descriptions.c.code_hash,
                schema.descriptions.c.desc_key,
                schema.descriptions.c.model,
                schema.descriptions.c.prompt_version,
                schema.descriptions.c.source,
                schema.descriptions.c.text,
            ).where(schema.descriptions.c.code_hash.in_(list(code_hashes)))
        ).mappings().all()

    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        rank = order.get((row["model"], row["prompt_version"]), fallback_rank)
        current = best.get(row["code_hash"])
        # ties broken on desc_key so the choice is deterministic (design law:
        # the same question twice must cite the same thing)
        if current is None or (rank, row["desc_key"]) < (current["_rank"], current["desc_key"]):
            best[row["code_hash"]] = {
                "_rank": rank,
                "desc_key": row["desc_key"],
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "source": row["source"],
                "text": row["text"],
                "is_current": rank == 0,
            }
    for entry in best.values():
        entry.pop("_rank", None)
    return best


def coverage(engine: Engine, target: IndexVersion | None = None) -> dict[str, Any]:
    """How far the lazy migration has actually got.

    Reported per version rather than as one percentage, because "83% migrated"
    and "83% described" are different facts and conflating them hides a stalled
    backfill behind a healthy-looking number.

    `target` overrides which version counts as "migrated to". It exists because
    the configured describe model and the model a description was actually
    attributed to can differ — Phase E attributes deterministic T0 descriptions
    to `template`, and a run with no reachable provider attributes its fallbacks
    honestly rather than claiming the configured model wrote them. Measuring a
    backfill against a version nothing was ever written at reports 0% forever,
    which looks like a stalled migration and is really a mislabelled question.
    """
    current = target or IndexVersion.current()
    with engine.connect() as connection:
        per_version = [
            {
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "descriptions": row["n"],
                "code_hashes": row["distinct_code"],
                "is_current": (row["model"], row["prompt_version"])
                == (current.model, current.prompt_version),
            }
            for row in connection.execute(
                select(
                    schema.descriptions.c.model,
                    schema.descriptions.c.prompt_version,
                    func.count().label("n"),
                    func.count(func.distinct(schema.descriptions.c.code_hash)).label("distinct_code"),
                )
                .group_by(schema.descriptions.c.model, schema.descriptions.c.prompt_version)
                .order_by(func.count().desc())
            ).mappings()
        ]
        total_code = connection.execute(
            select(func.count(func.distinct(schema.chunks.c.code_hash)))
        ).scalar_one()
        at_current = connection.execute(
            select(func.count(func.distinct(schema.descriptions.c.code_hash))).where(
                schema.descriptions.c.model == current.model,
                schema.descriptions.c.prompt_version == current.prompt_version,
            )
        ).scalar_one()

    return {
        "current": current.as_dict(),
        "code_hashes_total": total_code,
        "code_hashes_at_current_version": at_current,
        "migrated_pct": round(100.0 * at_current / total_code, 1) if total_code else 0.0,
        "versions": per_version,
        "preference_order": [
            {"model": model, "prompt_version": prompt_version}
            for model, prompt_version in preference_order(engine)
        ],
    }


def stale_code_hashes(
    engine: Engine, limit: int = 1000, target: IndexVersion | None = None
) -> list[str]:
    """Code that has *a* description but not one at the current version.

    This is the backfill's work list, and it is deliberately not "code with no
    description at all" — that is the describe backfill's job and it is a
    different urgency. Something described at an old prompt version is answerable
    today; something undescribed is not.
    """
    current = target or IndexVersion.current()
    at_current = select(schema.descriptions.c.code_hash).where(
        schema.descriptions.c.model == current.model,
        schema.descriptions.c.prompt_version == current.prompt_version,
    )
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                select(schema.descriptions.c.code_hash)
                .where(schema.descriptions.c.code_hash.notin_(at_current))
                .distinct()
                .order_by(schema.descriptions.c.code_hash)
                .limit(limit)
            )
        ]
