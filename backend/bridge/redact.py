"""Secret and PII redaction — applied to every chunk before it is embedded or sent to an LLM.

Design law 8. This runs on the last mile: `describe.py` and `retrieve.py` call
:func:`redact` on chunk text immediately before it enters a prompt, and
`embed.py` calls it before the text reaches the encoder. Nothing downstream is
trusted to remember.

The rules are deliberately blunt. A false positive costs one unreadable token in
a description; a false negative leaks a credential to a third party. Counts are
returned so a run can report exactly what it scrubbed rather than claiming
"nothing found" by silence.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Pattern

#: (label, compiled pattern, replacement). Order matters — the earlier rules
#: consume larger structures so later, narrower rules cannot shred them.
_RULES: list[tuple[str, Pattern[str], str]] = [
    # -- secrets -----------------------------------------------------------
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
        "[REDACTED:JWT]",
    ),
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AIDA|AGPA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b"),
        "[REDACTED:AWS_KEY]",
    ),
    (
        "aws_secret_access_key",
        # the 40-char secret only when it is on an obviously-AWS assignment
        re.compile(
            r"(?i)\baws[_-]?secret[_-]?access[_-]?key\b\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?"
        ),
        "aws_secret_access_key=[REDACTED:AWS_SECRET]",
    ),
    (
        "assigned_secret",
        # key=..., token: "...", password = '...'  — the value, not the name
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|secret|access[_-]?token|auth[_-]?token"
            r"|token|password|passwd|passphrase|client[_-]?secret|private[_-]?key)\b"
            r"(\s*[:=]\s*)"
            r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;)}\]]{4,})"
        ),
        r"\1\2[REDACTED:SECRET]",
    ),
    # -- PII ---------------------------------------------------------------
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:EMAIL]",
    ),
    (
        "aadhaar_like",
        # any bare 12-digit run; checked before the phone rule so it wins the span
        re.compile(r"(?<![\d.])\d{12}(?![\d.])"),
        "[REDACTED:ID12]",
    ),
    (
        "indian_phone",
        re.compile(r"(?<![\d.])(?:\+?91[-\s]?)?[6-9]\d{9}(?![\d.])"),
        "[REDACTED:PHONE]",
    ),
]

#: rule labels that mark a secret rather than personal data
SECRET_LABELS = frozenset(
    {
        "private_key_block",
        "jwt",
        "aws_access_key_id",
        "aws_secret_access_key",
        "assigned_secret",
    }
)
PII_LABELS = frozenset({"email", "aadhaar_like", "indian_phone"})


@dataclass(slots=True)
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def secrets(self) -> int:
        return sum(n for label, n in self.counts.items() if label in SECRET_LABELS)

    @property
    def pii(self) -> int:
        return sum(n for label, n in self.counts.items() if label in PII_LABELS)


def redact(text: str) -> Redaction:
    """Scrub secrets and PII, returning the cleaned text and per-rule hit counts."""
    if not text:
        return Redaction(text=text or "", counts={})

    counts: dict[str, int] = {}
    for label, pattern, replacement in _RULES:
        cleaned, hits = pattern.subn(replacement, text)
        if hits:
            counts[label] = counts.get(label, 0) + hits
            text = cleaned
    return Redaction(text=text, counts=counts)


def redact_text(text: str) -> str:
    """:func:`redact` when only the cleaned string is wanted."""
    return redact(text).text


class RedactionLog:
    """Accumulates redaction counts across a run so the summary can report them."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._chunks = 0
        self._chunks_hit = 0

    def scrub(self, text: str) -> str:
        result = redact(text)
        self._chunks += 1
        if result.counts:
            self._chunks_hit += 1
            self._counts.update(result.counts)
        return result.text

    def scrub_all(self, texts: Iterable[str]) -> list[str]:
        return [self.scrub(text) for text in texts]

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    @property
    def chunks_scanned(self) -> int:
        return self._chunks

    @property
    def chunks_redacted(self) -> int:
        return self._chunks_hit

    def summary(self) -> str:
        if not self._counts:
            return f"{self._chunks} chunks scanned, 0 redactions"
        detail = ", ".join(f"{label} {n}" for label, n in sorted(self._counts.items()))
        return (
            f"{self._chunks} chunks scanned, {self.total} redactions "
            f"in {self._chunks_hit} chunks ({detail})"
        )
