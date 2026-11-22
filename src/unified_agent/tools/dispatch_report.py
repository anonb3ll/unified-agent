"""Hub-mediated self-report for a dispatched agent session (task #623).

The local CLI ``unified-agent-dispatch-scheduler report`` writes the
authoritative DB from the child process. Under a fail-closed Claude sandbox
that denies ``~/.unified_agent``, that path is unavailable — and even when
Bash can run, noninteractive permission prompts previously blocked it
(run-622).

``dispatch_report_self`` is the MCP alternative: the authenticated child calls
the hub with an explicit ``run_id`` (also written to a per-run sidecar under
``data_home/dispatch-runs/`` and injected into the task prompt). The hub verifies:

1. The caller has a verified bearer identity.
2. That identity matches the run's ``agent_label``.
3. The run is still live (or already terminal in the requested state).
4. ``finished`` requires non-empty evidence.

Then it delegates to ``record_run_outcome`` — same contract as the CLI.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, profiles
from ..dispatch import coordinator
from ..dispatch.receipt import DeliveryReceipt, build_delivery_receipt
from ..dispatch.report import report_outcome
from ..dispatch.scheduler import OUTCOME_STATES
from ..dispatch_states import TERMINAL_RUN_STATES

_SELF_REPORT_STATES = frozenset({"finished", "failed", "blocked"})


def dispatch_report_self(
    state: str,
    *,
    run_id: str,
    evidence: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Record a terminal outcome for the caller's own live dispatch run.

    Requires authenticated hub identity matching the run's agent_label.
    ``run_id`` is required (hub process env is not the child's env).
    ``finished`` requires non-empty ``evidence``.
    """
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError(
            "dispatch_report_self requires an authenticated hub identity "
            "(bearer token); anonymous callers cannot bind a run"
        )

    rid = (run_id or "").strip()
    if not rid:
        raise ToolError(
            "run_id is required — pass the value from the dispatch sidecar "
            "(.ua-dispatch.json under the hub data home) or the task prompt"
        )

    st = (state or "").strip()
    if st not in _SELF_REPORT_STATES:
        raise ToolError(f"state must be one of {sorted(_SELF_REPORT_STATES)}; got {state!r}")
    if st not in OUTCOME_STATES:
        raise ToolError(f"unsupported outcome state: {state!r}")

    run = coordinator.get_run(rid)
    if run is None:
        raise ToolError(f"unknown dispatch run: {rid}")

    run_agent = (run.get("agent_label") or "").strip()
    if run_agent != identity:
        raise ToolError(
            f"identity mismatch: verified as {identity!r} but run {rid} belongs to {run_agent!r}"
        )

    evidence_ref = (evidence or "").strip() or None
    if st == "finished" and not evidence_ref:
        raise ToolError(
            "finished requires non-empty evidence (commit SHA, PR URL, or child-authored log path)"
        )

    reason = (failure_reason or "").strip() or None
    if st != "finished" and not reason:
        reason = f"self_report:{st}"

    result = report_outcome(
        st,
        run_id=rid,
        failure_reason=reason,
        evidence_ref=evidence_ref,
        actor=identity,
    )
    return {
        "ok": bool(result.get("ok")),
        "run_id": rid,
        "state": st,
        "agent_label": identity,
        "evidence_ref": evidence_ref,
        "outcome": result,
    }


def dispatch_cancel(*, run_id: str, reason: str = "client cancelled") -> dict[str, Any]:
    """Cancel the caller's live dispatch run through the governed hub path."""
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError("dispatch_cancel requires an authenticated hub identity")
    run = coordinator.get_run((run_id or "").strip())
    if run is None:
        raise ToolError(f"unknown dispatch run: {run_id}")
    if run.get("agent_label") != identity:
        raise ToolError(
            f"identity mismatch: verified as {identity!r} but run belongs to {run.get('agent_label')!r}"
        )
    return coordinator.cancel_run(
        run_id, actor=identity, reason=(reason or "client cancelled").strip()
    )


def dispatch_resume(*, run_id: str) -> dict[str, Any]:
    """Resume a cancelled or retryable dispatch run through the governed hub path."""
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError("dispatch_resume requires an authenticated hub identity")
    run = coordinator.get_run((run_id or "").strip())
    if run is None:
        raise ToolError(f"unknown dispatch run: {run_id}")
    if run.get("agent_label") != identity:
        raise ToolError(
            f"identity mismatch: verified as {identity!r} but run belongs to {run.get('agent_label')!r}"
        )
    result = coordinator.resume_run(run_id, actor=identity)
    if not result.get("ok"):
        raise ToolError(f"dispatch resume failed: {result.get('reason')}")
    return result


def _operator_identity(tool_name: str) -> str:
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError(f"{tool_name} requires an authenticated hub identity")
    if profiles.current() != profiles.OPERATOR:
        raise ToolError(f"{tool_name} requires the authenticated operator profile")
    return identity


def dispatch_retry_cancellation(*, run_id: str) -> dict[str, Any]:
    """Retry termination for an operator-visible unconfirmed cancellation."""
    identity = _operator_identity("dispatch_retry_cancellation")
    result = coordinator.retry_cancellation(run_id, actor=identity)
    if not result.get("ok"):
        raise ToolError(f"dispatch cancellation retry failed: {result.get('reason')}")
    return result


def dispatch_confirm_cancellation(
    *, run_id: str, note: str = "operator_confirmed"
) -> dict[str, Any]:
    """Confirm a cancellation after an operator verified the target is gone."""
    identity = _operator_identity("dispatch_confirm_cancellation")
    result = coordinator.confirm_cancellation(run_id, actor=identity, note=note)
    if not result.get("ok"):
        raise ToolError(f"dispatch cancellation confirmation failed: {result.get('reason')}")
    return result


def dispatch_delivery_receipt(*, run_id: str) -> DeliveryReceipt:
    """Return the read-only accountability receipt for one dispatch run."""
    receipt = build_delivery_receipt(run_id)
    if receipt is None:
        raise ToolError(f"unknown dispatch run: {run_id}")
    return receipt


# Fields from the dispatch_runs ledger the summary list surfaces (task #1182).
# Deliberately excludes nothing secret — this table stores no credentials —
# but stays an explicit allowlist anyway so a future column addition can't
# silently widen what the Control Center displays without a review.
_DISPATCH_RECEIPT_SUMMARY_FIELDS = (
    "run_id",
    "task_id",
    "delegation_id",
    "agent_label",
    "state",
    "actor",
    "lease_expires_at",
    "failure_reason",
    "failure_class",
    "evidence_ref",
    "termination_outcome",
    "created_at",
    "updated_at",
)

_TERMINAL_RECEIPT_STATES = TERMINAL_RUN_STATES


def dispatch_delivery_receipts_list(
    *,
    task_id: int | None = None,
    state: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent dispatch runs as delivery-receipt summaries (task #1182).

    Read-only projection of the existing dispatch run ledger (``dispatch_runs``
    via ``coordinator.list_runs``) — deliberately not a second ledger. Each
    summary row carries enough to see delivery outcome (state, evidence
    presence, termination outcome) at a glance; call
    ``dispatch_delivery_receipt(run_id=...)`` for the full accountability
    receipt (transitions, policy/approval outcome, executor auth proof) on any
    one run. Never mutates a run and never weakens an evidence gate.

    Args:
        task_id: Optional filter to one task's runs.
        state: Optional filter to one dispatch run state.
        limit: Max rows to return, newest first (default 20, max 200).

    Returns:
        {receipts: [...], count, terminal_count}
    """
    lim = max(1, min(limit, 200))
    rows = coordinator.list_runs(task_id=task_id, state=state, limit=lim)
    receipts = [
        {field: row.get(field) for field in _DISPATCH_RECEIPT_SUMMARY_FIELDS}
        | {
            "terminal": row.get("state") in _TERMINAL_RECEIPT_STATES,
            "has_evidence": bool(row.get("evidence_ref")),
        }
        for row in rows
    ]
    return {
        "receipts": receipts,
        "count": len(receipts),
        "terminal_count": sum(1 for r in receipts if r["terminal"]),
    }
