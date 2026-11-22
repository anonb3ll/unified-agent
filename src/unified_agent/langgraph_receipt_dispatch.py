"""Shared receipt ledger for the note and workflow dispatch adapters.

Both adapters append one generated task note through wrapped ``task_update``.
This base owns the duplicated ledger steps: native receipt validation, native
operation-key checks, stale-authority refresh after a confirmed receipt, and
the governed call/reconcile flow, including the gateway invocation scope and
the approval-receipt binding around the write. Subclasses keep run checks,
envelope and receipt shape, error nouns, and whether a failed pre-execute
transition releases the prepared claim.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, gateway, storage
from . import langgraph_dispatch as dispatch
from .langgraph_pilot import GovernedToolOutcome, InvalidRunError, PilotRun, StaleAuthorityError
from .telemetry.redaction import redact_sensitive_content
from .tools.approvals import mark_governed_execution


class ReceiptDispatchAdapter(dispatch.UnifiedAgentDispatchAdapter):
    """Receipt validation, operation-key checks, and stale-authority refresh.

    ``_effect_noun`` is the word embedded in this adapter's ledger errors
    (``progress`` or ``workflow``). It is not a behavior switch.
    """

    _effect_noun: str

    def _validate_effect(self, run: PilotRun, **spec: Any) -> None:
        """Reject a run before any ledger read. ``spec`` is the public call's fields."""
        raise NotImplementedError

    def _effect_key(self, run: PilotRun, **spec: Any) -> str:
        raise NotImplementedError

    def _effect_envelope(self, run: PilotRun, project: str, **spec: Any) -> dict[str, Any]:
        raise NotImplementedError

    def _effect_receipt(self, run: PilotRun, **spec: Any) -> dict[str, Any]:
        raise NotImplementedError

    def _after_restoring_pre_execute_approval(self, key: str, envelope: dict[str, Any]) -> None:
        """Subclass policy for the prepared claim after a failed pre-execute transition."""
        raise NotImplementedError

    def _native_receipt(self, run: PilotRun, envelope: dict[str, Any]) -> None:
        args = envelope["arguments"]
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT tool, result_json FROM idempotency_keys WHERE key=?",
                (storage.idempotency_storage_key(args["idempotency_key"]),),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["tool"] != "task_update":
            raise dispatch.DuplicateDeliveryUncertainError(
                f"native {self._effect_noun} receipt absent or mismatched"
            )
        result = dispatch._decode_effect(row["result_json"])
        notes = result.get("notes")
        if (
            result.get("id") != run.task_id
            or result.get("status") != "doing"
            or result.get("claimed_by") != run.claimed_by
            or result.get("lock_expires_at") != run.claim_expires_at
            or result.get("project") != args["expected_project"]
            or not result.get("updated_at")
            or result.get("updated_at") == run.task_updated_at
            or result.get("worktree") is not None
            or not isinstance(notes, str)
            or not notes.splitlines()
            or notes.splitlines()[-1] != args["append_note"]
            or notes.splitlines().count(args["append_note"]) != 1
        ):
            raise dispatch.DuplicateDeliveryUncertainError(
                f"native {self._effect_noun} receipt failed validation"
            )

    def _require_unused_native_key(self, envelope: dict[str, Any]) -> None:
        key = envelope["arguments"]["idempotency_key"]
        conn = storage.connect()
        try:
            # The raw key counts too: a receipt from before tenant scoping
            # may already record this operation (#1834).
            row = conn.execute(
                "SELECT 1 FROM idempotency_keys WHERE key IN (?, ?)",
                (storage.idempotency_storage_key(key), key),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            raise dispatch.DuplicateDeliveryUncertainError(
                f"native {self._effect_noun} operation key already exists"
            )

    def _refresh_after_receipt(
        self,
        run: PilotRun,
        receipt_line: str,
        *,
        changed_error: str,
        revision_error: str,
    ) -> PilotRun:
        """Adopt this run's own receipt revision. Foreign edits fail closed."""
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, updated_at, claimed_by, lock_expires_at, notes FROM tasks "
                "WHERE id=? AND org_id IS ?",
                (run.task_id, edition.tenant_org_id()),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise StaleAuthorityError("canonical task no longer exists")
        if row["status"] != "doing":
            raise StaleAuthorityError("canonical task is no longer doing")
        if row["claimed_by"] != run.claimed_by:
            raise StaleAuthorityError("canonical task owner changed")
        if row["lock_expires_at"] != run.claim_expires_at:
            raise StaleAuthorityError("canonical claim lease changed")
        notes = row["notes"] or ""
        lines = notes.splitlines()
        if not lines or lines[-1] != receipt_line or lines.count(receipt_line) != 1:
            raise StaleAuthorityError(changed_error)
        if row["updated_at"] == run.task_updated_at:
            raise StaleAuthorityError(revision_error)
        return replace(run, task_updated_at=row["updated_at"])

    def _reconcile_effect(self, *, run: PilotRun, **spec: Any) -> GovernedToolOutcome | None:
        """Read historical authority only; never invoke or retry a task mutation."""
        self._validate_effect(run, **spec)
        key = self._effect_key(run, **spec)
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        noun = self._effect_noun
        if row["tool"] != dispatch._EFFECT_TOOL:
            raise dispatch.DuplicateDeliveryUncertainError(f"{noun} operation key collision")
        record = dispatch._decode_effect(row["result_json"])
        envelope = record.get("envelope", {})
        if not isinstance(envelope, dict) or not isinstance(envelope.get("arguments"), dict):
            raise dispatch.DuplicateDeliveryUncertainError(f"{noun} envelope is corrupt")
        project = envelope["arguments"].get("expected_project")
        if not isinstance(project, str) or envelope != self._effect_envelope(run, project, **spec):
            raise dispatch.DuplicateDeliveryUncertainError(f"{noun} immutable envelope mismatch")
        state = record.get("state")
        approval_id = str(record["approval_id"]) if record.get("approval_id") else None
        if state == "denied":
            return GovernedToolOutcome.denied(record["reason"], approval_id=approval_id)
        if state == "succeeded":
            receipt = record.get("result")
            if receipt != self._effect_receipt(run, **spec):
                raise dispatch.DuplicateDeliveryUncertainError(f"{noun} receipt is corrupt")
            return GovernedToolOutcome.allowed(receipt, approval_id=approval_id)
        if state not in {"executing", "unknown"} or not approval_id:
            raise dispatch.DuplicateDeliveryUncertainError(
                f"{noun} effect did not record a certain outcome; explicit recovery required"
            )
        self._native_receipt(run, envelope)
        receipt = self._effect_receipt(run, **spec)
        dispatch._transition_effect(
            key,
            envelope,
            {"executing", "unknown"},
            {
                "state": "succeeded",
                "approval_id": int(approval_id),
                "result": receipt,
                "result_digest": dispatch._digest_json(receipt),
            },
        )
        dispatch._record_effect_event(run, key, "reconciled", "task_update")
        return GovernedToolOutcome.allowed(receipt, approval_id=approval_id)

    def _call_effect(
        self, *, run: PilotRun, timeout_s: float = 20, **spec: Any
    ) -> GovernedToolOutcome:
        self._validate_effect(run, **spec)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not 0 < timeout_s <= dispatch._MAX_TIMEOUT_S
        ):
            raise InvalidRunError(f"{self._effect_noun} timeout_s must be in (0, 20]")
        historical = self._reconcile_effect(run=run, **spec)
        if historical is not None:
            return historical
        snapshot = self.revalidate(run)
        envelope = self._effect_envelope(run, snapshot.project, **spec)
        args = envelope["arguments"]
        self._require_unused_native_key(envelope)
        # The policy precheck, the adapter approval, and the registered write
        # are one gateway invocation. Its rate slots are spent only once the
        # write is committed to run; every earlier exit refunds them.
        with gateway.invocation("task_update") as gateway_scope:
            try:
                with dispatch.approval_invocation("task_update", args) as receipts:
                    return self._write_effect(
                        run=run,
                        timeout_s=timeout_s,
                        envelope=envelope,
                        args=args,
                        gateway_scope=gateway_scope,
                        receipts=receipts,
                        spec=spec,
                    )
            finally:
                if not gateway_scope.committed:
                    gateway_scope.refund()

    def _write_effect(
        self,
        *,
        run: PilotRun,
        timeout_s: float,
        envelope: dict[str, Any],
        args: dict[str, Any],
        gateway_scope: Any,
        receipts: dict[Any, Any],
        spec: dict[str, Any],
    ) -> GovernedToolOutcome:
        noun = self._effect_noun
        try:
            dispatch._precheck_policy("task_update", args)
        except dispatch.ApprovalPendingError:
            pending_id = dispatch._pending_tool_approval_id("task_update", args)
            if pending_id is None:
                raise dispatch.DuplicateDeliveryUncertainError(
                    "policy approval was queued but its canonical request cannot be established"
                ) from None
            return GovernedToolOutcome.pending(str(pending_id))
        key = self._effect_key(run, **spec)
        existing = dispatch._claim_effect(key, envelope)
        if existing is not None:
            return self._reconcile_effect(run=run, **spec)
        dispatch._record_effect_event(run, key, "prepared", "task_update")
        approval_id = None
        try:
            approval = dispatch.check_approval_gate(
                dispatch._APPROVAL_TOOL,
                envelope,
                requested_by=config.verified_agent_identity(),
                force=True,
            )
            approval_id = approval.get("approval_id")
        except dispatch.ApprovalPendingError:
            dispatch._restore_invocation_receipts(receipts)
            pending_id = dispatch._pending_approval_id(envelope)
            dispatch._release_prepared_effect(key, envelope)
            if pending_id is None:
                raise dispatch.DuplicateDeliveryUncertainError(
                    f"canonical {noun} approval missing"
                ) from None
            return GovernedToolOutcome.pending(str(pending_id))
        except ToolError:
            dispatch._restore_invocation_receipts(receipts)
            denied = dispatch.lookup_denied_approval(dispatch._APPROVAL_TOOL, envelope)
            if denied is None:
                dispatch._release_prepared_effect(key, envelope)
                raise
            denied_id, reason = denied
            reason = redact_sensitive_content(reason, max_len=200)
            dispatch._transition_effect(
                key,
                envelope,
                {"prepared"},
                {"state": "denied", "approval_id": denied_id, "reason": reason},
            )
            return GovernedToolOutcome.denied(reason, approval_id=str(denied_id))
        except Exception:
            dispatch._release_prepared_effect(key, envelope)
            raise

        try:
            self.revalidate(run)
            dispatch._precheck_policy("task_update", args)
            self._require_unused_native_key(envelope)
            dispatch._transition_effect(
                key, envelope, {"prepared"}, {"state": "executing", "approval_id": approval_id}
            )
        except Exception:
            if approval_id is not None:
                dispatch.restore_consumed_approval(int(approval_id))
            self._after_restoring_pre_execute_approval(key, envelope)
            raise
        dispatch._record_effect_event(run, key, "executing", "task_update")
        gateway_scope.commit()
        mark_governed_execution(handoff=True)
        try:
            dispatch._invoke_with_timeout(
                self._tool_registry["task_update"], timeout_s=float(timeout_s), **args
            )
            self._native_receipt(run, envelope)
        except Exception as exc:
            dispatch._transition_effect(
                key,
                envelope,
                {"executing"},
                {
                    "state": "unknown",
                    "approval_id": approval_id,
                    "error": dispatch._bounded_error(exc),
                },
            )
            dispatch._record_effect_event(run, key, "unknown", "task_update")
            raise dispatch.DuplicateDeliveryUncertainError(
                f"{noun} write outcome uncertain; reconcile without retrying the write"
            ) from exc
        receipt = self._effect_receipt(run, **spec)
        try:
            dispatch._transition_effect(
                key,
                envelope,
                {"executing"},
                {
                    "state": "succeeded",
                    "approval_id": approval_id,
                    "result": receipt,
                    "result_digest": dispatch._digest_json(receipt),
                },
            )
        except Exception as exc:
            raise dispatch.DuplicateDeliveryUncertainError(
                f"{noun} outer receipt not committed"
            ) from exc
        dispatch._record_effect_event(run, key, "succeeded", "task_update")
        return GovernedToolOutcome.allowed(receipt, approval_id=str(approval_id))
