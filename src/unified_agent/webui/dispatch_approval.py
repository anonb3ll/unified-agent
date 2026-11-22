"""Request existing-child execution authority without creating or launching work.

The payload is the coordinator's existing exact task/delegation approval contract.
Human resolution, pass lifetime, revocation and atomic consumption remain owned by
the existing approval store and dispatcher; this adapter only queues a request.
"""

from __future__ import annotations

from .. import config, edition, object_model, storage
from ..dispatch import coordinator
from ..dispatch_reads import DISPATCH_APPROVAL_TOOL, _canonical_payload, dispatch_approval_payload


def request_existing_dispatch_approval(payload: dict, *, requested_by: str) -> dict:
    if requested_by != config.verified_agent_identity():
        raise ValueError("execution approval request requires the verified requester")
    if (
        set(payload) != {"action", "task_id", "delegation_id"}
        or payload.get("action") != "dispatch"
    ):
        raise ValueError("dispatch approval requires exactly action, task_id and delegation_id")
    for field in ("task_id", "delegation_id"):
        if type(payload[field]) is not int or payload[field] < 1:
            raise ValueError(f"{field} must be a positive integer")
    canonical = dispatch_approval_payload(
        task_id=payload["task_id"], delegation_id=payload["delegation_id"]
    )
    org = edition.tenant_org_id()
    if config.workos_request_is_bound():
        metadata = config.auth_metadata_var.get() or {}
        if not org or metadata.get("workos_organization_id") != org:
            raise ValueError("request organization does not match the configured organization")
    conn = storage.connect()
    try:
        task = conn.execute(
            "SELECT org_id FROM tasks WHERE id=?", (canonical["task_id"],)
        ).fetchone()
        delegation = conn.execute(
            "SELECT task_id, org_id FROM delegations WHERE id=?", (canonical["delegation_id"],)
        ).fetchone()
        if (
            task is None
            or delegation is None
            or delegation["task_id"] != canonical["task_id"]
            or task["org_id"] != org
            or delegation["org_id"] != org
        ):
            raise ValueError("existing task/delegation pair is unavailable in this organization")
        # The legacy gate indexes by exact tool/payload. Refuse any ambiguous
        # pre-existing row rather than reuse a foreign/null tenant's authority.
        rows = conn.execute(
            "SELECT org_id FROM approvals WHERE tool=? AND payload=?",
            (DISPATCH_APPROVAL_TOOL, _canonical_payload(canonical)),
        ).fetchall()
        if any(row["org_id"] != org for row in rows):
            raise ValueError("existing execution approval requires organization reconciliation")
    finally:
        conn.close()
    verdict = coordinator.evaluate_ready(
        task_id=canonical["task_id"], delegation_id=canonical["delegation_id"]
    )
    if verdict["reason"] not in {
        "ok",
        "missing_approval_marker",
        "unapproved",
        "denied",
        "expired",
    }:
        raise ValueError(
            f"existing delegation cannot request execution approval: {verdict['reason']}"
        )
    return object_model.by_kind("approval").writer("request")(
        DISPATCH_APPROVAL_TOOL,
        canonical,
        requested_by=requested_by,
        force=True,
        request_only=True,
    )
