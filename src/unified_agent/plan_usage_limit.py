"""Recognize a plan or account usage ceiling in provider diagnostic text.

A plan usage ceiling, as opposed to a transient per-minute rate limit (task
#1841). Pure text matching with no imports from the rest of the package, so any
layer may classify a failure with it (task #2026 moved it out of
``quota_observe``, which still re-exports it).
"""

from __future__ import annotations

import re

_PLAN_USAGE_LIMIT_RE = re.compile(
    r"(?:usage\s+limit\s+(?:has\s+been\s+)?reached|quota\s+exhausted|"
    r"\b(?:\d+[\s-]?hour|session|weekly|daily|opus|sonnet)\s+limit\s+reached|"
    r"hit\s+your\s+(?:usage\s+)?limit)",
    re.IGNORECASE,
)


def is_plan_usage_limit(text: str | None) -> bool:
    """True when ``text`` reports a plan usage ceiling, not a transient rate limit."""
    return bool(text) and bool(_PLAN_USAGE_LIMIT_RE.search(text))
