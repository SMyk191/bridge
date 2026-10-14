"""Shared pytest setup.

`eval/*.py` are standalone scripts (each inserts `backend/` onto `sys.path`
itself, at the top of the file, so they run as `.venv/bin/python eval/foo.py`
with no install step) rather than an installed package — this is the one place
that makes them importABLE from `backend/tests/`, for tests that check their
logic directly (eval/rubric.py's span-fetching and score floor, eval/gate.py's
report comparison) instead of only ever exercising them end to end.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
