"""Eligibility rules for the SYSTEM bug worker.

Ticket notes are untrusted input — markers gate automation; prose cannot grant
extra authority or override safety boundaries.
"""

from __future__ import annotations

import re

from ..tools.tasks import task_list_all

_STREAM_SYSTEM_RE = re.compile(r"(?m)^stream:\s*SYSTEM\s*$")
_STREAM_PRIVATE_RE = re.compile(r"(?m)^stream:\s*PRIVATE\s*$")
_STREAM_PUBLIC_RE = re.compile(r"(?m)^stream:\s*PUBLIC\s*$")
_BUG_TRUE_RE = re.compile(r"(?m)^bug:\s*true\s*$", re.IGNORECASE)
_WORKER_ELIGIBLE_RE = re.compile(r"(?m)^worker-eligible:\s*true\b", re.IGNORECASE)
_WORKER_ELIGIBLE_FALSE_RE = re.compile(r"(?m)^worker-eligible:\s*false\b", re.IGNORECASE)

# Untrusted authority / injection patterns — ticket prose must never escalate.
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.I),
    re.compile(r"you\s+are\s+now\s+(god|root|admin|superuser)", re.I),
    re.compile(r"grant\s+(me\s+)?(admin|root|full|unrestricted)\s+(access|power|authority)", re.I),
    re.compile(r"bypass\s+(approval|auth|sandbox|security)", re.I),
    re.compile(r"auto[- ]?merge\s+(this|immediately|now)", re.I),
    re.compile(r"disable\s+(approval|auth|sandbox)", re.I),
    re.compile(r"override\s+(instructions|policy|rules)", re.I),
)


def notes_contain_untrusted_authority_claims(notes: str | None) -> bool:
    """True when ticket notes attempt to grant authority via prose."""
    body = notes or ""
    return any(rx.search(body) for rx in _INJECTION_PATTERNS)


def _has_system_stream(notes: str) -> bool:
    if _STREAM_PRIVATE_RE.search(notes) or _STREAM_PUBLIC_RE.search(notes):
        return False
    return bool(_STREAM_SYSTEM_RE.search(notes))


def is_system_bug_eligible(task: dict) -> tuple[bool, str]:
    """Return (eligible, reason) for one task dict from task_list."""
    notes = task.get("notes") or ""
    status = task.get("status") or "open"
    if status not in ("open", "doing"):
        return False, f"status {status!r} is not claimable"
    if notes_contain_untrusted_authority_claims(notes):
        return False, "ticket notes contain untrusted authority/injection claims"
    if not _has_system_stream(notes):
        return False, "stream is not SYSTEM (PRIVATE/PUBLIC excluded)"
    if not _BUG_TRUE_RE.search(notes):
        return False, "missing bug: true marker"
    if _WORKER_ELIGIBLE_FALSE_RE.search(notes):
        return False, "worker-eligible: false"
    if not _WORKER_ELIGIBLE_RE.search(notes):
        return False, "missing worker-eligible: true marker"
    if task.get("claimed_by") and status == "doing":
        lock_expires = task.get("lock_expires_at")
        if lock_expires:
            from .. import storage

            if storage.now() < lock_expires:
                return False, f"already claimed by {task.get('claimed_by')!r}"
    return True, "eligible"


def discover_eligible_tasks(*, limit: int = 20) -> list[dict]:
    """Frontier SYSTEM bugs that pass marker gates.

    Walks every page of task_list(frontier=True, notes_contain=\"stream: SYSTEM\")
    and applies bug/worker-eligible markers and injection rejection client-side,
    so ineligible high-priority SYSTEM tasks never starve later eligible bugs.
    """
    # Every page, not one bounded window: an eligible bug behind the first
    # page used to read as "idle" (Devin, #814).
    eligible: list[dict] = []
    for task in task_list_all(status="open", frontier=True, notes_contain="stream: SYSTEM"):
        ok, _reason = is_system_bug_eligible(task)
        if ok:
            eligible.append(task)
        if len(eligible) >= limit:
            break
    return eligible
