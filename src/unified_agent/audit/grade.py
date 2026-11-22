"""Stable A–F grade for an audit summary.

High is this engine's top severity (there is no separate ``critical`` band).
Truncation is graded E when nothing high was found: the scan did not finish,
so the letter is not a clean bill of health.
"""

from __future__ import annotations

from .rules import SEVERITIES

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}
_GRADE_RANK = {letter: i for i, letter in enumerate("ABCDEF")}


def grade_from_summary(summary: dict, truncated: bool = False) -> str:
    """Map ``{high, medium, low, info}`` (+ optional truncation) to A–F."""
    high = int(summary.get("high") or 0)
    medium = int(summary.get("medium") or 0)
    low = int(summary.get("low") or 0)
    info = int(summary.get("info") or 0)
    if high:
        return "F"
    if truncated:
        return "E"
    if medium:
        return "D"
    if low:
        return "C"
    if info:
        return "B"
    return "A"


def worst_grade(grades: list[str]) -> str:
    if not grades:
        return "A"
    return max(grades, key=lambda g: _GRADE_RANK.get(g, -1))


def gated(findings: list[dict], fail_on: str) -> bool:
    """True when any finding is at least ``fail_on`` severe."""
    if fail_on == "none":
        return False
    gate = _SEV_RANK[fail_on]
    return any(_SEV_RANK.get(fd.get("severity"), 99) <= gate for fd in findings)
