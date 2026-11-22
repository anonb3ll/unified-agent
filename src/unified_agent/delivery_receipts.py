"""Read-only delivery receipts over dispatch records, oldest run first for audit replay.

Moved out of ``dispatch.receipt`` (task #1724) so the evidence report (a
service) can list a task's receipts without importing the dispatch runtime.
The executor-auth probe stays in ``dispatch.receipt``, which needs the doctor
surface; callers here pass its result in as ``executor_auth``.
``dispatch.receipt`` re-exports every name, so old imports keep working.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from typing_extensions import TypedDict

from . import dispatch_reads, storage
from .dispatch_states import TERMINAL_RUN_STATES


class ExecutorAuthProof(TypedDict):
    status: Literal["LIVE", "DORMANT", "MISCONFIGURED", "UNKNOWN"]
    authenticated: bool | None
    probe_process: Literal["separate", "hub_skipped", "not_run"]


class DeliveryReceipt(TypedDict):
    schema_version: str
    task_id: int
    run_id: str
    delegation_id: int | None
    executor_identity: str
    lease: dict[str, Any]
    retry_count: int | None
    event_cursor: str | None
    policy_outcome: str
    approval_outcome: str
    evidence_ref: str | None
    final_task_state: str | None
    run_state: str
    terminal: bool
    transitions: list[dict[str, Any]]
    executor_auth: ExecutorAuthProof | None


_TERMINAL_RUN_STATES = TERMINAL_RUN_STATES


def _transition_rows(conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ulid, payload FROM event_log "
        "WHERE channel='agent.dispatch.transition' ORDER BY ROWID ASC"
    ).fetchall()
    transitions: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            continue
        transitions.append(
            {
                "ulid": row["ulid"],
                "state": payload.get("state"),
                "timestamp": payload.get("timestamp"),
                "actor": payload.get("actor"),
                "failure_reason": payload.get("failure_reason"),
                "evidence_ref": payload.get("evidence_ref"),
                "retry_count": payload.get("retry_count"),
            }
        )
    return transitions


def _build_receipt(
    run: dict[str, Any],
    *,
    task: dict[str, Any],
    transitions: list[dict[str, Any]],
    conn: Any,
    executor_auth: ExecutorAuthProof | None = None,
) -> DeliveryReceipt:
    delegation_id = run.get("delegation_id")
    approval_status = None
    if delegation_id is not None:
        approval_status = dispatch_reads._approval_row_status(
            conn, task_id=int(run["task_id"]), delegation_id=int(delegation_id)
        )
    claimed = any(item.get("state") in {"claimed", "resumed"} for item in transitions)
    if claimed:
        policy_outcome = "approved"
    elif run.get("state") == "denied":
        policy_outcome = "denied"
    elif transitions and transitions[-1].get("state") == "refused":
        policy_outcome = str(transitions[-1].get("failure_reason") or "refused")
    else:
        policy_outcome = "unknown"

    if approval_status is None and policy_outcome == "approved":
        approval_status = "approved"
    elif approval_status is None and policy_outcome in {"denied", "expired"}:
        approval_status = policy_outcome

    retry_count = dispatch_reads._retry_count_for_run(transitions)
    if retry_count is None and delegation_id is not None:
        retry_count = dispatch_reads._delegation_attempts(int(delegation_id))
    event_cursor = transitions[-1]["ulid"] if transitions else None
    task_state = task.get("status")
    run_state = str(run.get("state") or "unknown")
    return {
        "schema_version": "1.0",
        "task_id": int(run["task_id"]),
        "run_id": str(run["run_id"]),
        "delegation_id": int(delegation_id) if delegation_id is not None else None,
        "executor_identity": str(run.get("agent_label") or "unknown"),
        "lease": {
            "owner": run.get("lease_owner"),
            "expires_at": run.get("lease_expires_at"),
            "active": run_state in {"claimed", "launched", "running"},
        },
        "retry_count": retry_count,
        "event_cursor": event_cursor,
        "policy_outcome": policy_outcome,
        "approval_outcome": str(approval_status or "not_requested"),
        "evidence_ref": run.get("evidence_ref"),
        "final_task_state": task_state,
        "run_state": run_state,
        "terminal": run_state in _TERMINAL_RUN_STATES,
        "transitions": transitions,
        "executor_auth": executor_auth,
    }


def receipts_for_task(task_id: int, *, conn: Any = None) -> list[DeliveryReceipt]:
    """Return every historical run for a task, oldest first for audit replay."""
    close_conn = conn is None
    conn = conn or storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE task_id=? ORDER BY created_at ASC, run_id ASC",
            (task_id,),
        ).fetchall()
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = {key: task_row[key] for key in task_row.keys()} if task_row else {}
        receipts: list[DeliveryReceipt] = []
        for row in rows:
            run = {key: row[key] for key in row.keys()}
            receipts.append(
                _build_receipt(
                    run,
                    task=task,
                    transitions=_transition_rows(conn, str(run["run_id"])),
                    conn=conn,
                )
            )
        return receipts
    finally:
        if close_conn:
            conn.close()
