"""Operator release of another agent's live task claim (task #1700).

``task_update(release_claim=True)`` lets the claimant release its own claim and
anyone release an *expired* one. It refuses a live claim held by someone else,
which is right for agents but left an operator with only
``agent_terminate``: that releases every claim the agent holds and stops the
agent, which is far broader than handing over one task.

:func:`task_claim_release` is the task-scoped operator path:

* **Authority.** A lifecycle operator (``UA_LIFECYCLE_OPERATORS``, the set
  ``agent_lifecycle`` uses for pause/resume/terminate), or the verified human
  operator over a Control Center recovery channel (:func:`human_operator_release`).
  Nobody else, including the claimant's peers; the claimant itself uses
  ``task_update(release_claim=True)``. It never acts as the claimant.
* **Fencing.** ``expected_claimant`` is required and ``expected_run_id`` is
  optional in MCP. HTTP requires the run key; a null run additionally requires
  ``expected_claimed_at`` and fences the absence of a run (task #2049).
  The supplied expectations are checked up front
  and again inside ``task_update``'s write transaction, before its own run
  fence, so a claim that changed hands, or a run a renewal replaced, loses the
  race as a 409 conflict instead of being released blind. The status and the
  claim generation (``claimed_at``, distinct per fresh claim) read up front are
  re-checked there too, so a drop and re-claim by the same agent also loses
  (task #2003). A done or cancelled task is refused: it has nothing to transfer
  and its ``claimed_by`` is the row's attribution.
* **Effect.** The claim columns are cleared through ``task_update`` itself, so
  the open self-pickup run is closed in the same transaction (the old holder
  cannot renew it with its run token) and queue hygiene has no lease left to
  renew. A ``doing`` task goes back to ``open``; any other status is kept.
  Transferring the task is then an ordinary ``task_claim`` by the new agent.
* **Audit.** One ``agent.task.claim_released`` ledger event, written in the
  release transaction, records the actor, authority, previous claimant and
  run, and the reason. The reason is also appended to the task notes.

Release does not stop a running executor. A live dispatch run on the task is
refused: cancel it first (``dispatch_cancel``). A self-pickup process is not
visible to the hub, so ask it to stop with ``task_update(request_cancel=True)``
before releasing, and pause it (``agent_pause``) if it must not pick the task
up again.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import agent_lifecycle, config, storage
from .tool_errors import ConflictError

CHANNEL = "agent.task.claim_released"
KIND_RELEASED = "claim.operator_released"
MAX_REASON = 500


class ClaimReleaseAuthorityError(ToolError):
    """The caller may not release another agent's claim (HTTP 403)."""


class ClaimReleaseConflict(ConflictError):
    """The claim no longer matches what the caller expected (HTTP 409)."""


# The identity an HTTP route admitted as the verified human operator.
_human_grant: ContextVar[str | None] = ContextVar("claim_release_human_grant", default=None)
# The release in flight; task_update honours it for this task and caller only.
_release_grant: ContextVar[dict[str, Any] | None] = ContextVar("claim_release_grant", default=None)


@contextmanager
def human_operator_release(identity: str) -> Iterator[None]:
    """Let one HTTP request release a claim as the verified human operator.

    A route enters this only after ``routes._operator_recovery`` held for the
    request. It is honoured only while the verified identity is ``identity`` and
    that identity is the operator label, so it never covers an MCP call.
    """
    token = _human_grant.set(identity)
    try:
        yield
    finally:
        _human_grant.reset(token)


def _authority(who: str) -> str:
    # The human grant is checked first: with UA_LIFECYCLE_OPERATORS unset the
    # operator label is a lifecycle operator too, and that path dispatches
    # through the agent tool surface, which the operator profile does not
    # expose (PR #1146 review).
    if _human_grant.get() == who and who == config.operator_label():
        return "human_operator"
    if who in agent_lifecycle.operators():
        return "lifecycle_operator"
    raise ClaimReleaseAuthorityError(
        f"{who!r} may not release another agent's claim: that is an operator action "
        "(UA_LIFECYCLE_OPERATORS, or the verified operator in the Control Center). "
        "A claimant releases its own claim with task_update(release_claim=True)."
    )


def active_grant(task_id: int, updater: str | None) -> dict[str, Any] | None:
    """The release in flight for ``task_id`` by ``updater``, else None.

    Read by ``task_update`` inside its write transaction.
    """
    grant = _release_grant.get()
    if grant is None or not updater:
        return None
    if grant["task_id"] != task_id or grant["actor"] != updater:
        return None
    return grant


def check_in_tx(grant: dict[str, Any], old: Any, conn: Any) -> None:
    """Refuse when the row read under the write lock no longer matches the grant."""
    if not old["claimed_by"]:
        raise ClaimReleaseConflict(f"task {grant['task_id']} has no claim to release")
    if old["status"] in ("done", "cancelled"):
        raise ClaimReleaseConflict(
            f"task {grant['task_id']} is {old['status']}: a terminal task has no claim to "
            "transfer, and its claimed_by records who did the work"
        )
    if old["claimed_by"] != grant["expected_claimant"]:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']} is claimed by {old['claimed_by']!r}, not the expected "
            f"{grant['expected_claimant']!r}; refresh and retry"
        )
    if old["claimed_at"] != grant["claimed_at"]:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']} was re-claimed by {old['claimed_by']!r} while the "
            "release was prepared; refresh and retry"
        )
    expected_run = grant.get("expected_run_id")
    if grant.get("expected_no_run") and old["current_run_id"] is not None:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']}'s claim now has a run; refresh and retry"
        )
    if expected_run is not None and old["current_run_id"] != expected_run:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']}'s current run is {old['current_run_id']!r}, not the "
            f"expected {expected_run!r}; refresh and retry"
        )
    if old["status"] != grant["status_before"]:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']} moved to {old['status']!r} while the release was "
            "prepared; refresh and retry"
        )
    live = conn.execute(
        "SELECT run_id FROM dispatch_runs WHERE task_id=? AND "
        f"({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) LIMIT 1",
        (grant["task_id"],),
    ).fetchone()
    if live is not None:
        raise ClaimReleaseConflict(
            f"task {grant['task_id']} has a live dispatch run {live[0]!r}; releasing the "
            "claim would not stop it. Cancel it first (dispatch_cancel) and retry once "
            "its termination is confirmed"
        )


def ledger_payload(grant: dict[str, Any], old: Any, status_after: str) -> dict[str, Any]:
    return {
        "kind": KIND_RELEASED,
        "ref_id": str(grant["task_id"]),
        "task_id": grant["task_id"],
        "previous_claimant": old["claimed_by"],
        "previous_run_id": old["current_run_id"],
        "previous_lock_expires_at": old["lock_expires_at"],
        "status_before": old["status"],
        "status_after": status_after,
        "reason": grant["reason"],
        "actor": grant["actor"],
        "authority": grant["authority"],
        "at": storage.now(),
    }


def _clean_reason(reason: str | None) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        raise ToolError("a reason is required to release another agent's claim")
    if len(text) > MAX_REASON:
        raise ToolError(f"reason must be at most {MAX_REASON} characters")
    return text


def task_claim_release(
    task_id: int,
    expected_claimant: str,
    reason: str,
    expected_run_id: str | None = None,
    expected_claimed_at: str | None = None,
) -> dict[str, Any]:
    """Release one task's live claim held by another agent; operators only.

    ``expected_claimant`` must name the current holder and ``expected_run_id``,
    when given, its current run. ``expected_claimed_at`` fences the exact claim
    generation; with a null run it also requires the claim to remain unbound.
    A ``doing`` task returns to ``open``; a done or
    cancelled task is refused. Release does not stop a running executor: a live
    dispatch run is refused (``dispatch_cancel`` it first), and a self-pickup
    holder should get ``task_update(request_cancel=True)`` first. Returns
    ``{"task": <updated task>, "released": {...}}``; ``released`` mirrors the
    ``agent.task.claim_released`` ledger event and carries its ``event_ulid``.
    """
    from . import tool_surface
    from .tools import tasks

    who = config.verified_agent_identity()
    if not who:
        raise ClaimReleaseAuthorityError(
            "releasing another agent's claim requires an authenticated identity"
        )
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ToolError("task_id must be a positive integer")
    claimant = " ".join(str(expected_claimant or "").split())
    if not claimant:
        raise ToolError("expected_claimant is required: name the agent whose claim you release")
    authority = _authority(who)
    text = _clean_reason(reason)
    if expected_claimed_at is not None and (
        not isinstance(expected_claimed_at, str) or not expected_claimed_at.strip()
    ):
        raise ToolError("expected_claimed_at must be a non-empty claim generation")

    conn = storage.connect()
    try:
        row = tasks._visible_task(
            conn, task_id, "status, claimed_by, claimed_at, current_run_id, lock_expires_at"
        )
        if row is None:
            raise ToolError(f"no task with id {task_id}")
        grant: dict[str, Any] = {
            "task_id": task_id,
            "actor": who,
            "authority": authority,
            "expected_claimant": claimant,
            "expected_run_id": expected_run_id,
            "status_before": row["status"],
            "claimed_at": expected_claimed_at
            if expected_claimed_at is not None
            else row["claimed_at"],
            "expected_no_run": expected_claimed_at is not None and expected_run_id is None,
            "reason": text,
        }
        check_in_tx(grant, row, conn)
    finally:
        conn.close()
    new_status = "open" if row["status"] == "doing" else None
    note = (
        f"[claim-released] {who} ({authority}) released "
        f"{claimant}'s claim (run {row['current_run_id'] or '-'}): {text}"
    )
    token = _release_grant.set(grant)
    try:
        # The grant exempts only the ownership check. All task-write admission
        # still applies. Keep the approval payload stable across retries; the
        # ledger event records the release timestamp inside the transaction.
        arguments = dict(
            id=task_id,
            status=new_status,
            release_claim=True,
            expected_run_id=expected_run_id,
            append_note=note,
            worktree=False,
        )
        if authority == "human_operator":
            # The recovery route authorizes this one release even though the
            # human's read/approval profile has no general task_update tool.
            # Recovery retains the access exception, but never bypasses task
            # policy, trust presets, rate limits or required approval.
            from . import agent_access, gateway, safety, trust_presets
            from .tools import approvals

            agent_access.enforce("task_update", read_only=False, human_recovery=True)
            trust_presets.check_call("task_update", read_only=False, payload=arguments)
            with (
                gateway.invocation("task_update"),
                approvals.approval_invocation(restore_unstarted=True) as receipts,
            ):
                safety.check_policy_gate("task_update", arguments, originator=who)
                if not any(
                    isinstance(r, dict) and r.get("approval_id") is not None
                    for r in receipts.values()
                ):
                    approvals.check_approval_gate("task_update", arguments, requested_by=who)
                result = tasks.task_update(**arguments)
        else:
            result = tool_surface.registry()["task_update"](**arguments)
    finally:
        _release_grant.reset(token)
    released = dict(grant.get("event") or {})
    released["event_ulid"] = grant.get("event_ulid")
    return {"task": result, "released": released}
