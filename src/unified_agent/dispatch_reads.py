"""Read-only readers over the dispatch ledger (``dispatch_runs`` and its approvals).

Moved out of ``dispatch.coordinator`` (task #1724) so modules below the runtime
layer (``horizon2``'s dispatch observation, ``delivery_receipts``) can read
dispatch runs without importing the coordinator. Nothing here writes. The
coordinator re-exports every name, so ``coordinator.list_runs`` and the other
old paths keep working.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config, edition, storage

DISPATCH_APPROVAL_TOOL = "dispatch_claim"


APPROVED_MARKER_RE = re.compile(r"\[plan:\s*approved\s+@\s*([^\]]+)\]", re.I)
DENIED_MARKER_RE = re.compile(r"\[plan:\s*denied\s+@", re.I)
EXPIRED_MARKER_RE = re.compile(r"\[plan:\s*expired\s+@", re.I)
# Present but missing the required ``@ <ts>`` token → malformed, not approved.
MALFORMED_APPROVED_RE = re.compile(r"\[plan:\s*approved(?:\s*\])?(?!\s*@)", re.I)


def _operator_notes_for_approval(notes: str, delegation_task_spec: str | None) -> str:
    """Notes region trusted for ``[plan: …]`` markers — never the embedded spec.

    ``delegate()`` copies the caller-controlled ``task_spec`` into ``tasks.notes``
    after a ``task-spec:`` header. A spec that merely *mentions* an approval
    marker must not authorize dispatch. Operator append paths (``append_note``,
    steward edits) land after the embedded spec and are scanned separately.
    """
    text = notes or ""
    spec = (delegation_task_spec or "").strip()
    if not spec:
        return text
    marker = "task-spec:"
    idx = text.lower().find(marker)
    if idx == -1:
        return text
    header = text[:idx]
    after = text[idx + len(marker) :].lstrip("\n")
    if after.startswith(spec):
        appended = after[len(spec) :].strip()
    else:
        appended = ""
    return f"{header}\n{appended}".strip()


def _last_marker_pos(pattern: re.Pattern[str], notes: str) -> int:
    last = -1
    for match in pattern.finditer(notes):
        last = match.start()
    return last


def dispatch_plan_blocker(notes: str, delegation_task_spec: str | None) -> str | None:
    """A later plan refusal remains blocking until the operator revises the plan."""
    notes = _operator_notes_for_approval(notes, delegation_task_spec)
    approved = _last_marker_pos(APPROVED_MARKER_RE, notes)
    if _last_marker_pos(DENIED_MARKER_RE, notes) > approved:
        return "denied"
    if _last_marker_pos(EXPIRED_MARKER_RE, notes) > approved:
        return "expired"
    return None


def dispatch_approval_payload(*, task_id: int, delegation_id: int) -> dict[str, Any]:
    """Canonical payload paired with ``DISPATCH_APPROVAL_TOOL`` one-shot passes."""
    return {
        "action": "dispatch",
        "task_id": task_id,
        "delegation_id": delegation_id,
    }


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _row(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _delegation_attempts(delegation_id: int | None) -> int | None:
    """Retry count for operator telemetry (``delegations.attempts``)."""
    if delegation_id is None:
        return None
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT attempts FROM delegations WHERE id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["attempts"] or 0)
    finally:
        conn.close()


def _approval_row_status(conn, *, task_id: int, delegation_id: int) -> str | None:
    """Return latest matching approval status for the coordinator tool+payload, if any."""
    valid, row = dispatch_approval_selection(conn, task_id=task_id, delegation_id=delegation_id)
    if not valid:
        return "invalid"
    return row["status"] if row else None


def dispatch_approval_row(conn, *, task_id: int, delegation_id: int):
    """Exact tenant-bound row for proof readers; invalid states supply no grant."""
    valid, row = dispatch_approval_selection(conn, task_id=task_id, delegation_id=delegation_id)
    return row if valid else None


def dispatch_approval_selection(conn, *, task_id: int, delegation_id: int):
    """Return (valid authority state, latest row or genuine absence).

    Invalid resource/org/mixed-row state must not masquerade as absence and
    enable legacy marker fallback. The claim consumes this same selection.
    """
    org = edition.tenant_org_id()
    if config.workos_request_is_bound():
        metadata = config.auth_metadata_var.get() or {}
        if not org or metadata.get("workos_organization_id") != org:
            return False, None
    pair = conn.execute(
        "SELECT t.org_id AS task_org, d.org_id AS delegation_org FROM tasks t "
        "JOIN delegations d ON d.task_id=t.id WHERE t.id=? AND d.id=?",
        (task_id, delegation_id),
    ).fetchone()
    if pair is None or pair["task_org"] != org or pair["delegation_org"] != org:
        return False, None
    canon = _canonical_payload(
        dispatch_approval_payload(task_id=task_id, delegation_id=delegation_id)
    )
    rows = conn.execute(
        "SELECT * FROM approvals WHERE tool=? AND payload=? ORDER BY id DESC",
        (DISPATCH_APPROVAL_TOOL, canon),
    ).fetchall()
    if any(row["org_id"] != org for row in rows):
        return False, None
    return True, rows[0] if rows else None


def dispatch_recovery_run(conn, approval):
    """Prove outstanding recovery authority for one consumed dispatch grant.

    No inference from task IDs or historical consumed rows: the existing
    consumption event must bind this exact grant generation to a recoverable
    run. Completed/live effects and unknown cancellation outcomes are excluded.
    """
    if approval["tool"] != DISPATCH_APPROVAL_TOOL or approval["status"] != "consumed":
        return None
    try:
        payload = json.loads(approval["payload"])
        if (
            set(payload) != {"action", "task_id", "delegation_id"}
            or payload["action"] != "dispatch"
        ):
            return None
        if any(type(payload[key]) is not int for key in ("task_id", "delegation_id")):
            return None
        current = dispatch_approval_row(
            conn, task_id=payload["task_id"], delegation_id=payload["delegation_id"]
        )
        if current is None or current["id"] != approval["id"]:
            return None
        event = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.approval.consumed' "
            "AND json_extract(payload, '$.approval_id')=? ORDER BY ROWID DESC LIMIT 1",
            (approval["id"],),
        ).fetchone()
        proof = json.loads(event["payload"]) if event else {}
        if (
            proof.get("source") != "dispatch_claim-v1"
            or proof.get("tool") != DISPATCH_APPROVAL_TOOL
            or proof.get("task_id") != payload["task_id"]
            or proof.get("delegation_id") != payload["delegation_id"]
            or proof.get("org_id") != approval["org_id"]
            or proof.get("generation") != approval["updated_at"]
            or proof.get("grant")
            != {
                field: approval[field]
                for field in ("requested_by", "resolved_by", "resolved_at", "created_at")
            }
        ):
            return None
        run = conn.execute(
            "SELECT * FROM dispatch_runs WHERE run_id=? AND task_id=? AND delegation_id=?",
            (proof.get("run_id"), payload["task_id"], payload["delegation_id"]),
        ).fetchone()
        if run is None or run["state"] not in {"retryable", "failed", "cancelled"}:
            return None
        if run["state"] == "cancelled" and run["termination_outcome"] != "confirmed":
            return None
        if run["failure_reason"] == "lease_expired_outcome_unknown":
            return None
        return run
    except (TypeError, ValueError, KeyError):
        return None


def _retry_count_for_run(transitions: list[dict[str, Any]]) -> int | None:
    """Retry count captured for this run at claim time (not delegation aggregate)."""
    for payload in transitions:
        if payload.get("state") == "claimed":
            retry = payload.get("retry_count")
            if retry is not None:
                return int(retry)
    for payload in transitions:
        retry = payload.get("retry_count")
        if retry is not None:
            return int(retry)
    return None


def list_runs(
    *,
    task_id: int | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    lim = max(1, min(limit, 500))
    clauses: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        clauses.append("task_id=?")
        params.append(task_id)
    if state is not None:
        clauses.append("state=?")
        params.append(state)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(lim)
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM dispatch_runs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()
