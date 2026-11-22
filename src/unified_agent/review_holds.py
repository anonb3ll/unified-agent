"""The operator's release or keep decision on a review hold (tasks #2466, #2514).

A review hold is a task the review pipeline parked for a human, read from the
task's notes the way the operator inbox (``needs_operator``) reads it:

``red_tier_hold``
    The notes carry ``[review: red-tier @ ...]``: the change touches red-tier
    files and needs a human approval of that scope.
``held_pr``
    The notes carry ``[waiting-for: human-merge]`` or a ``[review: held-...]``
    marker and no red-tier marker: gates ran (or could not run) and the pull
    request waits for a person.

Before this module the Control Center could only add a task note, which nothing
read. A decision here is one durable row, bound to the exact hold marker it
answers (``hold_ref``), so a new hold (a new head, a new marker) needs a new
decision. Two readers act on it:

* ``needs_operator`` stops listing a hold whose current marker has a decision.
* ``shipping_preflight_cli`` judges each hold on the pull request on its own
  (``standing_keeps``): it refuses the merge while any hold's current decision is
  a ``keep`` recorded at or after the human approval the merge presents. Only a
  release of that same hold, or a fresh approval after the keep, clears it; a
  release of one hold never clears a keep on another. Keeping again after an
  approval superseded a keep records a fresh keep.

``release`` is never an approval. It lifts the operator's own hold and nothing
else: the merge still needs the Approval Ledger's head-bound human approval.
A red-tier hold cannot be released at all, because for that hold "release"
would read as approving red-tier scope. It is lifted only by approving the pull
request's Ledger request (``POST /api/action/approval``); the refusal says so.

Only the verified human operator records a decision
(``POST /api/action/review-hold``). This module is data and rules; the route
does the identity check. Schema: ``ensure_schema``, called from
``storage.init_db``; one self-contained table, so no SCHEMA_VERSION bump.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from typing import Any

from . import edition, events, storage

RED_TIER_HOLD = "red_tier_hold"
HELD_PR = "held_pr"
KINDS = (RED_TIER_HOLD, HELD_PR)
RELEASE = "release"
KEEP = "keep"
DECISIONS = (RELEASE, KEEP)

#: The note markers, as the reviewer writes them.
RED_TIER_MARKER = "[review: red-tier @"
HUMAN_MERGE_MARKER = "[waiting-for: human-merge]"
HELD_MARKER = "[review: held-"
#: The same markers as SQL ``LIKE`` patterns (``needs_operator`` reads with them).
RED_TIER_LIKE = "%[review: red-tier @%"
HUMAN_MERGE_LIKE = "%[waiting-for: human-merge]%"
HELD_LIKE = "%[review: held-%"

MAX_REASON_CHARS = 2_000
CHANNEL = "agent.review_hold.decided"
#: The Approval Ledger's pull-request approval tool (``github_app.TOOL_NAME``; this
#: core module cannot import the github component, a test pins the two together).
LEDGER_TOOL = "github_app.process_pr_event"
#: Where a red-tier hold is lifted instead (the refusal names it).
RED_TIER_APPROVAL_PATH = (
    "approve the pull request's Approval Ledger request at its current head "
    "(Inbox, kind approval; POST /api/action/approval)"
)

_PR_URL_RE = re.compile(r"https://github\.com/([\w.\-]+/[\w.\-]+)/pull/(\d+)")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS review_hold_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  hold_ref TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  repo TEXT,
  pr_number INTEGER,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL
)""",
    # Leading org_id: the tenancy inventory requires it of a partitioned table.
    "CREATE INDEX IF NOT EXISTS idx_review_hold_decisions_task "
    "ON review_hold_decisions(org_id, task_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_review_hold_decisions_pr "
    "ON review_hold_decisions(org_id, repo, pr_number, id)",
)


class HoldError(ValueError):
    """A decision the rules refuse. ``code`` and ``status`` map it to HTTP."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table and its indexes. Idempotent; statement at a time, so the
    caller's transaction is not committed early."""
    for statement in _DDL:
        conn.execute(statement)


def _tenant() -> str | None:
    return edition.tenant_org_id()


def current_hold(notes: str | None) -> dict[str, str] | None:
    """``{kind, marker, hold_ref}`` for the hold the notes carry now, else None.

    A red-tier marker anywhere makes the hold red-tier, as in the operator inbox.
    ``marker`` is the newest note line carrying the hold's marker; ``hold_ref``
    binds a decision to it.
    """
    text = notes or ""
    if RED_TIER_MARKER in text:
        kind, markers = RED_TIER_HOLD, (RED_TIER_MARKER,)
    elif HUMAN_MERGE_MARKER in text or HELD_MARKER in text:
        kind, markers = HELD_PR, (HUMAN_MERGE_MARKER, HELD_MARKER)
    else:
        return None
    line = next(
        (ln for ln in reversed(text.splitlines()) if any(m in ln for m in markers)),
        "",
    ).strip()
    digest = hashlib.sha256(f"{kind}\n{line}".encode()).hexdigest()[:32]
    return {"kind": kind, "marker": line[:300], "hold_ref": digest}


def pull_request(evidence_ref: str | None, notes: str | None) -> tuple[str, int] | None:
    """The task's GitHub pull request ``(owner/name, number)``: evidence first, then
    the newest one the notes name. None when neither names one."""
    for text in (evidence_ref or "", notes or ""):
        found = _PR_URL_RE.findall(text)
        if found:
            repo, number = found[-1]
            return repo, int(number)
    return None


def _latest(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_hold_decisions WHERE org_id IS ? AND task_id=? "
        "ORDER BY id DESC LIMIT 1",
        (_tenant(), task_id),
    ).fetchone()


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "task_id", "kind", "hold_ref", "decision", "reason", "repo", "pr_number")
    out = {key: row[key] for key in keys}
    out.update(decided_by=row["decided_by"], decided_at=row["decided_at"])
    return out


def decided(conn: sqlite3.Connection, task_id: int, notes: str | None) -> dict[str, Any] | None:
    """The decision on the task's current hold, or None (no hold, or undecided)."""
    hold = current_hold(notes)
    if hold is None:
        return None
    row = _latest(conn, task_id)
    if row is None or row["hold_ref"] != hold["hold_ref"]:
        return None
    return _public(row)


def record(task_id: int, decision: str, *, actor: str, reason: str | None = None) -> dict:
    """Record ``release`` or ``keep`` on the task's current hold, as ``actor``.

    The caller has already verified ``actor`` is the human operator. Raises
    ``HoldError``: 404 for an unknown task, 409 ``no_hold`` when the task is not
    held, 409 ``red_tier_approval_required`` for a release of a red-tier hold.
    Returns the decision row with ``hold`` and ``changed`` (False when the same
    decision was already the current one; nothing new is recorded then).
    """
    if decision not in DECISIONS:
        raise HoldError(f"decision must be one of {', '.join(DECISIONS)}", code="", status=400)
    text = (reason or "").strip()
    if len(text) > MAX_REASON_CHARS:
        raise HoldError(f"reason is longer than {MAX_REASON_CHARS} characters", code="", status=400)
    published = None
    with closing(storage.connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT id, notes, evidence_ref, status FROM tasks WHERE id=? AND org_id IS ?",
                (task_id, _tenant()),
            ).fetchone()
            if task is None:
                raise HoldError(f"unknown task {task_id}", code="not_found", status=404)
            hold = current_hold(task["notes"])
            if hold is None or task["status"] in ("done", "cancelled"):
                raise HoldError(
                    f"task #{task_id} has no review hold to decide",
                    code="no_hold",
                    status=409,
                )
            if hold["kind"] == RED_TIER_HOLD and decision == RELEASE:
                raise HoldError(
                    f"task #{task_id} is held for red-tier scope; releasing it would approve "
                    f"red-tier changes. Record keep, or {RED_TIER_APPROVAL_PATH}.",
                    code="red_tier_approval_required",
                    status=409,
                )
            latest = _latest(conn, task_id)
            if (
                latest is not None
                and latest["hold_ref"] == hold["hold_ref"]
                and latest["decision"] == decision
                and not (decision == KEEP and _superseded(conn, latest))
            ):
                conn.rollback()
                return {**_public(latest), "hold": hold, "changed": False}
            pr = pull_request(task["evidence_ref"], task["notes"])
            now = storage.now()
            cur = conn.execute(
                "INSERT INTO review_hold_decisions (org_id, task_id, kind, hold_ref, decision, "
                "reason, repo, pr_number, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _tenant(),
                    task_id,
                    hold["kind"],
                    hold["hold_ref"],
                    decision,
                    text or None,
                    pr[0] if pr else None,
                    pr[1] if pr else None,
                    actor,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM review_hold_decisions WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            _, _, published = events.record_event_once_on_connection(
                conn,
                CHANNEL,
                {
                    "decision_id": int(cur.lastrowid),
                    "task_id": task_id,
                    "kind": hold["kind"],
                    "hold_ref": hold["hold_ref"],
                    "decision": decision,
                    "repo": pr[0] if pr else None,
                    "pr_number": pr[1] if pr else None,
                },
                originator=actor,
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    if published is not None:
        events.publish(published)
    return {**_public(row), "hold": hold, "changed": True}


def _superseded(conn: sqlite3.Connection, decision: sqlite3.Row) -> bool:
    """Whether a Ledger approval of the decision's pull request was resolved after it.

    The merge preflight treats such an approval as superseding a keep, so a repeated
    keep must be recorded again rather than answered with the stale row.
    """
    if not decision["repo"] or decision["pr_number"] is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM approvals WHERE tool=? AND org_id IS ? "
        "AND json_extract(payload, '$.repo')=? AND json_extract(payload, '$.number')=? "
        "AND status IN ('approved', 'consumed') AND resolved_at > ? LIMIT 1",
        (
            LEDGER_TOOL,
            _tenant(),
            decision["repo"],
            int(decision["pr_number"]),
            decision["decided_at"],
        ),
    ).fetchone()
    return row is not None


def standing_keeps(
    conn: sqlite3.Connection, repo: str, number: int, approved_at: str | None
) -> list[dict[str, Any]]:
    """Every hold on one pull request whose current decision is a keep the approval
    does not supersede, oldest first.

    Each hold (task and hold marker) is judged on its own: its newest decision
    counts, so a release of one hold never clears a keep on another. A keep is
    superseded only by an approval resolved after it (``approved_at`` later than
    ``decided_at``). Read on the caller's connection (the merge preflight opens the
    store read-only). A store without the table has recorded no decision.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_hold_decisions'"
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute(
        "SELECT d.* FROM review_hold_decisions d JOIN ("
        "SELECT MAX(id) AS id FROM review_hold_decisions WHERE org_id IS ? AND repo=? "
        "AND pr_number=? GROUP BY task_id, hold_ref) newest ON newest.id = d.id "
        "WHERE d.decision=? AND d.decided_at >= ? ORDER BY d.id",
        (_tenant(), repo, number, KEEP, str(approved_at or "")),
    ).fetchall()
    return [_public(row) for row in rows]
