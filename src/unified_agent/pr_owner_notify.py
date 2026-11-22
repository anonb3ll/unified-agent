"""Tell a task's claimant when its pull request needs them (task #2396).

``pr_watch`` sees pull requests change, and until now it only wrote notes. This
module turns the changes an owner must act on into one hub message per PR head:
a failed check, a request for changes, a merge, or a close without one.

A task is linked by the PR URL in its ``evidence_ref``. Exactly one live claim
must match; two tasks on one PR are recorded and not guessed between. Every
skip lands once on ``agent.pr.notify`` with a typed reason, and a send lands
once as well, so a head skipped before its task was linked can still read as sent.

Only structured fields reach a message: numbers, short SHAs and cleaned check
names. PR titles, bodies and review text are data and stay out.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from . import edition, events, storage
from .pr_models import (
    REVIEW_HEAD_CHECK_NAME,
    PREvent,
    PRSnapshot,
    prefer_check_state,
    rollup_check_name,
)

logger = logging.getLogger(__name__)

ACTOR = "pr-watch"
CHANNEL = "agent.pr.notify"

CI_FAILED = "ci_failed"
NO_SIGNAL = "no_signal"
NO_OWNER = "no_owner"
AMBIGUOUS = "ambiguous_task"
REFUSED = "send_refused"

_NOTIFY_KINDS = frozenset({CI_FAILED, "merged", "closed", "reviewed"})
_FAILED_STATES = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"})
_MAX_NAMES = 5
_MAX_NAME_CHARS = 60
_UNSAFE_NAME = re.compile(r"[^\w ./()=:-]")
_TERMINAL_STATUSES = ("done", "cancelled")
#: How much of a refusal's first line is kept on the ledger (task #2434).
_DETAIL_CHARS = 200


def failing_checks(rollup: Any) -> tuple[str, ...]:
    """Cleaned names of the checks whose latest state is a failure, sorted."""
    states: dict[str, str] = {}
    for index, check in enumerate(rollup if isinstance(rollup, list) else []):
        if not isinstance(check, dict):
            continue
        name = rollup_check_name(check, index)
        state = check.get("conclusion") or check.get("state") or check.get("status")
        # The review-head check reports on review, not on the code.
        if name and state and REVIEW_HEAD_CHECK_NAME not in name:
            states[name] = prefer_check_state(states.get(name), str(state))
    failed = (name for name, state in states.items() if state.upper() in _FAILED_STATES)
    return tuple(sorted(_UNSAFE_NAME.sub("", name)[:_MAX_NAME_CHARS] for name in failed))


def ci_failures(
    repo: str, gh: Callable[[list[str]], str], current: dict[int, PRSnapshot]
) -> list[PREvent]:
    """One ``ci_failed`` event per open PR whose current head has a failing check."""
    raw = gh(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefOid,statusCheckRollup",
        ]
    )
    found: list[PREvent] = []
    for row in json.loads(raw):
        snapshot = current.get(int(row.get("number", -1)))
        if snapshot is None or snapshot.head_sha != row.get("headRefOid"):
            continue
        names = failing_checks(row.get("statusCheckRollup"))
        if names:
            found.append(PREvent(repo, CI_FAILED, snapshot, rationale=", ".join(names)))
    return found


def _wanted(event: PREvent) -> bool:
    if event.kind == "reviewed":
        return event.snapshot.last_review_state == "CHANGES_REQUESTED"
    return event.kind in _NOTIFY_KINDS


def _key(event: PREvent) -> str:
    snap = event.snapshot
    review = snap.last_review_at if event.kind == "reviewed" else ""
    return f"{event.repo}#{snap.number}:{snap.head_sha}:{event.kind}:{review}"


def _claims(conn: Any, repo: str, number: int) -> list[tuple[int, str]]:
    """Live claims whose ``evidence_ref`` names exactly this PR."""
    from .queue_closure import _PR_URL_RX

    rows = conn.execute(
        "SELECT id, claimed_by, evidence_ref FROM tasks "
        "WHERE claimed_by IS NOT NULL AND status NOT IN (?, ?) AND evidence_ref LIKE ? "
        "AND org_id IS ?",
        (*_TERMINAL_STATUSES, f"%/pull/{int(number)}%", edition.tenant_org_id()),
    ).fetchall()
    wanted = (repo.lower(), int(number))
    return [
        (int(row["id"]), str(row["claimed_by"]))
        for row in rows
        if wanted
        in {(m.group(1).lower(), int(m.group(2))) for m in _PR_URL_RX.finditer(row["evidence_ref"])}
    ]


def _body(event: PREvent, task_id: int) -> str:
    snap = event.snapshot
    where = f"PR #{snap.number} ({event.repo}) at {snap.head_sha[:8]}, task #{task_id}"
    if event.kind == CI_FAILED:
        names = event.rationale.split(", ")
        shown = ", ".join(names[:_MAX_NAMES])
        extra = f" and {len(names) - _MAX_NAMES} more" if len(names) > _MAX_NAMES else ""
        return f"CI failed on {where}: {shown}{extra}."
    if event.kind == "reviewed":
        return f"Changes requested on {where}."
    if event.kind == "merged":
        return f"{where} merged as {snap.merge_sha[:8]}. Close the task once its proof is in."
    return f"{where} closed without merging. Reopen it or release the task."


def _refusal_detail(exc: BaseException) -> str:
    """The first line of a send refusal, bounded, for the ledger (task #2434).

    A refusal names an address or a limit, never a message body, so its first
    line is safe to keep; it is what makes the next refusal diagnosable.
    """
    text = str(exc).strip().splitlines()
    first = text[0] if text else ""
    return f"{type(exc).__name__}: {first}"[:_DETAIL_CHARS]


def _record(
    key: str,
    event: PREvent,
    outcome: str,
    reason: str | None,
    task: int | None,
    detail: str | None = None,
) -> bool:
    """Record one decision; ``False`` if this key was already decided."""
    payload = {
        "repo": event.repo,
        "number": event.snapshot.number,
        "head_sha": event.snapshot.head_sha,
        "kind": event.kind,
        "outcome": outcome,
        "reason": reason,
        "task_id": task,
    }
    if detail:
        payload["detail"] = detail
    return bool(events.record_event_once(CHANNEL, payload, originator=ACTOR, dedupe_key=key)[1])


def _decide(conn: Any, event: PREvent) -> tuple[str | None, int | None, str | None]:
    """``(skip_reason, task_id, owner)``."""
    if not _wanted(event):
        return NO_SIGNAL, None, None
    claims = _claims(conn, event.repo, event.snapshot.number)
    if not claims:
        return NO_OWNER, None, None
    if len(claims) > 1:
        return AMBIGUOUS, None, None
    return None, claims[0][0], claims[0][1]


def notify(pr_events: Iterable[PREvent]) -> dict[str, int]:
    """Decide each event once. Returns newly ``sent`` and ``skipped`` counts."""
    from .messaging.send import send_message

    counts = {"sent": 0, "skipped": 0}
    for event in pr_events:
        key = _key(event)
        conn = storage.connect()
        try:
            skip, task_id, owner = _decide(conn, event)
        finally:
            conn.close()
        detail: str | None = None
        if skip is None:
            try:
                send_message(
                    act="inform",
                    to_agent=owner,
                    body=_body(event, int(task_id)),
                    from_agent=ACTOR,
                    task_id=task_id,
                    message_id="pr-notify-" + re.sub(r"[^\w-]", "-", key)[:180],
                )
            except Exception as exc:  # noqa: BLE001 - one bad recipient must not stop the rest
                detail = _refusal_detail(exc)
                logger.warning("pr notify refused: %s", detail)
                skip = REFUSED
        # A send has its own key: an earlier skip must not hide it from the record.
        record_key = key if skip else f"{key}:sent"
        fresh = _record(record_key, event, "skipped" if skip else "sent", skip, task_id, detail)
        if fresh:
            counts["skipped" if skip else "sent"] += 1
    return counts
