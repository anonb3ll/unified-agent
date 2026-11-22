"""Governed adapter between the LangGraph pilot and Unified-Agent authority.

The adapter is intentionally a hub-side library, not an MCP tool or daemon. It
uses the authenticated request context, the existing profile/policy-wrapped tool
registry, the canonical approval queue, the task/delegation lifecycle, and the
shared idempotency store. LangGraph remains responsible only for execution and
checkpoint state.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, gateway, profiles, safety, storage
from .langgraph_pilot import (
    GovernedGateway,
    GovernedToolOutcome,
    InvalidRunError,
    PilotError,
    PilotResult,
    PilotRun,
    StaleAuthorityError,
    ToolProposal,
    TransientGatewayError,
)
from .policy import PolicyViolation
from .telemetry.redaction import redact_sensitive_content
from .tools.approvals import (
    ApprovalPendingError,
    approval_invocation,
    canonical_approval_payload,
    check_approval_gate,
    lookup_denied_approval,
    mark_governed_execution,
    restore_consumed_approval,
)

_EFFECT_TOOL = "langgraph_governed_effect"
_APPROVAL_TOOL = "langgraph_governed_call"
_OUTCOME_REASON_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DENIED_RE = re.compile(
    r"approval request (\d+) for .+ was DENIED: (.+)\. Do not retry",
    re.DOTALL,
)
_MAX_TIMEOUT_S = 20.0
_EXPECTED_OPERATION = "read-evidence"


class DuplicateDeliveryUncertainError(PilotError):
    """A replay collided with an operation whose durable outcome is uncertain."""


class OutcomeRecordingError(PilotError):
    """The canonical task outcome could not be recorded safely."""


@dataclass(frozen=True)
class AuthoritySnapshot:
    """Fresh canonical authority observed by the hub-side adapter."""

    task_id: int
    task_updated_at: str
    claimed_by: str
    claim_expires_at: str
    project: str
    profile: str
    identity: str
    budget_revision: str


@dataclass(frozen=True)
class DispatchOutcome:
    """Bounded task-lifecycle projection of one graph terminal condition."""

    status: Literal["completed", "blocked", "failed"]
    output_digest: str | None = None
    reason_code: str | None = None

    def validate(self) -> None:
        if self.status == "completed":
            if not re.fullmatch(r"[0-9a-f]{64}", self.output_digest or ""):
                raise OutcomeRecordingError("completed outcome requires a SHA-256 output_digest")
            if self.reason_code is not None:
                raise OutcomeRecordingError("completed outcome cannot carry a reason_code")
            return
        if not _OUTCOME_REASON_RE.fullmatch(self.reason_code or ""):
            raise OutcomeRecordingError(
                "blocked or failed outcome requires a bounded machine reason_code"
            )
        if self.output_digest is not None:
            raise OutcomeRecordingError("non-completed outcome cannot carry an output_digest")

    @classmethod
    def completed(cls, result: PilotResult) -> DispatchOutcome:
        return cls(status="completed", output_digest=_digest_json(result.output))


class UnifiedAgentDispatchAdapter(GovernedGateway):
    """Concrete governed gateway and task-outcome recorder for the pilot.

    ``tool_registry`` must contain the same wrapped callables exposed by the
    authenticated hub. ``budget_revision`` must read the canonical budget
    source; there is deliberately no checkpoint-derived or static fallback.
    """

    def __init__(
        self,
        *,
        budget_revision: Callable[[int], str],
        tool_registry: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> None:
        self._budget_revision = budget_revision
        self._tool_registry = tool_registry or _default_tool_registry()

    def revalidate(self, run: PilotRun) -> AuthoritySnapshot:
        """Refresh identity, profile, task, capabilities, lease, and budget."""
        run.validate()
        identity, profile = _require_identity_profile(run)

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT id, status, updated_at, claimed_by, lock_expires_at, project "
                "FROM tasks WHERE id=? AND org_id IS ?",
                (run.task_id, edition.tenant_org_id()),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise StaleAuthorityError("canonical task no longer exists")
        if row["status"] != "doing":
            raise StaleAuthorityError("canonical task is no longer doing")
        if row["updated_at"] != run.task_updated_at:
            raise StaleAuthorityError("canonical task revision changed")
        if row["claimed_by"] != run.claimed_by:
            raise StaleAuthorityError("canonical task owner changed")
        if row["lock_expires_at"] != run.claim_expires_at:
            raise StaleAuthorityError("canonical claim lease changed")
        claim_expiry = _parse_timestamp(row["lock_expires_at"])
        if claim_expiry is None or datetime.now(UTC) >= claim_expiry:
            raise StaleAuthorityError("canonical claim lease is expired or invalid")

        missing = [
            capability
            for capability in run.required_capabilities
            if capability not in self._tool_registry or not profiles.tool_is_allowed(capability)
        ]
        if missing:
            raise StaleAuthorityError(
                "required capabilities are no longer exposed: " + ", ".join(sorted(missing))
            )
        try:
            current_budget = self._budget_revision(run.task_id)
        except Exception as exc:  # noqa: BLE001 - authority providers fail closed
            raise StaleAuthorityError("canonical budget revision is unavailable") from exc
        if not current_budget or current_budget != run.budget_revision:
            raise StaleAuthorityError("canonical budget revision changed")

        return AuthoritySnapshot(
            task_id=row["id"],
            task_updated_at=row["updated_at"],
            claimed_by=row["claimed_by"],
            claim_expires_at=row["lock_expires_at"],
            project=row["project"],
            profile=profile,
            identity=identity,
            budget_revision=current_budget,
        )

    def call_read_tool(
        self,
        *,
        run: PilotRun,
        proposal: ToolProposal,
        idempotency_key: str,
        timeout_s: float,
    ) -> GovernedToolOutcome:
        """Run one approved read through the hub registry exactly once."""
        # Both prechecks and the registered call are one gateway invocation:
        # one rate slot, returned if the read never crosses the boundary.
        with gateway.invocation(proposal.normalized().tool_name) as gateway_scope:
            try:
                return self._call_read_tool(
                    run=run,
                    proposal=proposal,
                    idempotency_key=idempotency_key,
                    timeout_s=timeout_s,
                    gateway_scope=gateway_scope,
                )
            finally:
                if not gateway_scope.committed:
                    gateway_scope.refund()

    def _call_read_tool(
        self,
        *,
        run: PilotRun,
        proposal: ToolProposal,
        idempotency_key: str,
        timeout_s: float,
        gateway_scope: Any,
    ) -> GovernedToolOutcome:
        self.revalidate(run)
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise InvalidRunError("timeout_s must be a positive number")
        if not 0 < float(timeout_s) <= _MAX_TIMEOUT_S:
            raise InvalidRunError(f"timeout_s must be in (0, {_MAX_TIMEOUT_S}]")
        normalized = proposal.normalized()
        expected_key = f"ua-lg:{run.run_id}:governed_tool:{_EXPECTED_OPERATION}"
        if idempotency_key != expected_key:
            raise InvalidRunError("idempotency_key does not match the immutable pilot operation")
        if normalized.tool_name not in run.required_capabilities:
            raise StaleAuthorityError("proposed tool is outside the run capability precondition")

        envelope = {
            "contract_version": "ua.langgraph.run/v1",
            "run_id": run.run_id,
            "task_id": run.task_id,
            "operation_key": _EXPECTED_OPERATION,
            "idempotency_key": idempotency_key,
            "timeout_s": float(timeout_s),
            "tool_name": normalized.tool_name,
            "arguments": dict(normalized.arguments),
            "payload_digest": normalized.payload_digest,
        }
        existing = _claim_effect(idempotency_key, envelope)
        if existing is not None:
            if existing.get("state") == "denied":
                _record_effect_event(run, idempotency_key, "duplicate-denied", normalized.tool_name)
                return GovernedToolOutcome.denied(
                    str(existing["reason"]),
                    approval_id=(
                        str(existing["approval_id"]) if existing.get("approval_id") else None
                    ),
                )
            _record_effect_event(run, idempotency_key, "duplicate-succeeded", normalized.tool_name)
            return GovernedToolOutcome.allowed(
                existing["result"], approval_id=str(existing["approval_id"])
            )
        _record_effect_event(run, idempotency_key, "prepared", normalized.tool_name)

        with approval_invocation(normalized.tool_name, dict(normalized.arguments)) as receipts:
            try:
                _precheck_policy(normalized.tool_name, dict(normalized.arguments))
            except ApprovalPendingError:
                _release_prepared_effect(idempotency_key, envelope)
                approval_id = _pending_tool_approval_id(
                    normalized.tool_name, dict(normalized.arguments)
                )
                if approval_id is None:
                    raise DuplicateDeliveryUncertainError(
                        "policy approval was queued but its canonical request cannot be established"
                    ) from None
                _record_effect_event(
                    run, idempotency_key, f"pending-approval:{approval_id}", normalized.tool_name
                )
                return GovernedToolOutcome.pending(str(approval_id))
            except ToolError as exc:
                _transition_effect(
                    idempotency_key,
                    envelope,
                    {"prepared"},
                    {"state": "failed", "error": _bounded_error(exc), "envelope": envelope},
                )
                _record_effect_event(
                    run, idempotency_key, "failed-before-approval", normalized.tool_name
                )
                raise

            try:
                approval = check_approval_gate(
                    _APPROVAL_TOOL,
                    envelope,
                    requested_by=config.verified_agent_identity(),
                    force=True,
                )
            except ApprovalPendingError:
                _restore_invocation_receipts(receipts)
                approval_id = _pending_approval_id(envelope)
                _release_prepared_effect(idempotency_key, envelope)
                if approval_id is None:
                    raise DuplicateDeliveryUncertainError(
                        "approval was queued but its canonical request cannot be established"
                    ) from None
                _record_effect_event(
                    run, idempotency_key, f"pending-approval:{approval_id}", normalized.tool_name
                )
                return GovernedToolOutcome.pending(str(approval_id))
            except ToolError as exc:
                _restore_invocation_receipts(receipts)
                denied = lookup_denied_approval(_APPROVAL_TOOL, envelope)
                if denied is None:
                    denied = _parse_denial(str(exc))
                if denied is None:
                    _transition_effect(
                        idempotency_key,
                        envelope,
                        {"prepared"},
                        {"state": "failed", "error": _bounded_error(exc), "envelope": envelope},
                    )
                    _record_effect_event(
                        run, idempotency_key, "failed-before-execution", normalized.tool_name
                    )
                    raise
                denied_id, reason = denied
                record = {
                    "state": "denied",
                    "approval_id": denied_id,
                    "reason": redact_sensitive_content(reason, max_len=200),
                    "envelope": envelope,
                }
                _transition_effect(idempotency_key, envelope, {"prepared"}, record)
                _record_effect_event(run, idempotency_key, "denied", normalized.tool_name)
                return GovernedToolOutcome.denied(record["reason"], approval_id=str(denied_id))
            except Exception as exc:
                _restore_invocation_receipts(receipts)
                _transition_effect(
                    idempotency_key,
                    envelope,
                    {"prepared"},
                    {"state": "failed", "error": _bounded_error(exc), "envelope": envelope},
                )
                _record_effect_event(
                    run, idempotency_key, "failed-before-execution", normalized.tool_name
                )
                raise

            approval_id = approval.get("approval_id")

            # Approval consumption is not execution authority on its own. Refresh
            # every precondition once more before crossing the governed boundary.
            try:
                self.revalidate(run)
                _precheck_policy(normalized.tool_name, dict(normalized.arguments))
            except (StaleAuthorityError, PolicyViolation, ToolError) as exc:
                if approval_id is not None:
                    restore_consumed_approval(int(approval_id))
                _restore_invocation_receipts(receipts)
                _transition_effect(
                    idempotency_key,
                    envelope,
                    {"prepared"},
                    {"state": "failed", "error": _bounded_error(exc), "envelope": envelope},
                )
                _record_effect_event(
                    run, idempotency_key, "stale-before-execution", normalized.tool_name
                )
                raise
            except sqlite3.Error:
                if approval_id is not None:
                    restore_consumed_approval(int(approval_id))
                _restore_invocation_receipts(receipts)
                _release_prepared_effect(idempotency_key, envelope)
                raise
            except Exception:
                if approval_id is not None:
                    restore_consumed_approval(int(approval_id))
                _restore_invocation_receipts(receipts)
                _release_prepared_effect(idempotency_key, envelope)
                raise
            _transition_effect(
                idempotency_key,
                envelope,
                {"prepared"},
                {
                    "state": "executing",
                    "approval_id": approval_id,
                    "envelope": envelope,
                },
            )
            _record_effect_event(run, idempotency_key, "executing", normalized.tool_name)
            gateway_scope.commit()
            mark_governed_execution(handoff=True)
            try:
                result = _invoke_with_timeout(
                    self._tool_registry[normalized.tool_name],
                    timeout_s=float(timeout_s),
                    **dict(normalized.arguments),
                )
            except TransientGatewayError:
                _abort_executing_effect(
                    idempotency_key,
                    envelope,
                    approval_id=approval_id,
                )
                _record_effect_event(run, idempotency_key, "transient-retry", normalized.tool_name)
                raise
            except Exception as exc:
                if _is_retryable_web_search_failure(normalized.tool_name, exc):
                    _abort_executing_effect(
                        idempotency_key,
                        envelope,
                        approval_id=approval_id,
                    )
                    _record_effect_event(
                        run, idempotency_key, "transient-retry", normalized.tool_name
                    )
                    raise TransientGatewayError(_bounded_error(exc)) from exc
                # Task #1221: same requested/confirmed/unknown vocabulary as
                # dispatch.supervise — "executing" was requested, and a raised
                # exception (including the attempt-deadline TimeoutError from
                # _invoke_with_timeout below, whose isolated thread may still be
                # running) means the effect's outcome is unconfirmed, not failed.
                # This ledger state is exactly that "unknown" claim; do not
                # collapse it into "failed" or treat the raise as proof the
                # governed call never took effect.
                _transition_effect(
                    idempotency_key,
                    envelope,
                    {"executing"},
                    {
                        "state": "unknown",
                        "approval_id": approval_id,
                        "error": _bounded_error(exc),
                        "envelope": envelope,
                    },
                )
                _record_effect_event(run, idempotency_key, "unknown", normalized.tool_name)
                raise

            record = {
                "state": "succeeded",
                "approval_id": approval_id,
                "result": result,
                "result_digest": _digest_json(result),
                "envelope": envelope,
            }
            try:
                _transition_effect(idempotency_key, envelope, {"executing"}, record)
            except Exception as exc:
                _record_effect_event(
                    run, idempotency_key, "unknown-after-result", normalized.tool_name
                )
                raise DuplicateDeliveryUncertainError(
                    "tool returned but the control-plane effect ledger was not committed"
                ) from exc
            _record_effect_event(run, idempotency_key, "succeeded", normalized.tool_name)
            return GovernedToolOutcome.allowed(result, approval_id=str(approval_id))

    def record_task_outcome(self, run: PilotRun, outcome: DispatchOutcome) -> dict[str, Any]:
        """Project a terminal graph condition into the canonical task lifecycle."""
        outcome.validate()
        _require_identity_profile(run)

        operation_key = f"ua-lg:{run.run_id}:task_outcome:record"
        envelope = {
            "contract_version": "ua.langgraph.run/v1",
            "run_id": run.run_id,
            "task_id": run.task_id,
            "operation_key": operation_key,
            "outcome": outcome.status,
            "output_digest": outcome.output_digest,
            "reason_code": outcome.reason_code,
        }
        existing = _claim_effect(operation_key, envelope)
        if existing is not None:
            _record_effect_event(run, operation_key, "duplicate-succeeded", "task_update")
            return dict(existing["result"])
        _record_effect_event(run, operation_key, "prepared", "task_update")

        try:
            snapshot = self.revalidate(run)
            if "task_update" not in self._tool_registry or not profiles.tool_is_allowed(
                "task_update"
            ):
                raise StaleAuthorityError("task_update is not exposed by the active profile")
        except Exception as exc:
            _transition_effect(
                operation_key,
                envelope,
                {"prepared"},
                {"state": "failed", "error": _bounded_error(exc), "envelope": envelope},
            )
            _record_effect_event(run, operation_key, "stale-before-outcome", "task_update")
            raise

        note = f"[langgraph-run: {run.run_id}] outcome={outcome.status} trace={run.trace_id}"
        if outcome.output_digest:
            note += f" output={outcome.output_digest}"
        if outcome.reason_code:
            note += f" reason={outcome.reason_code}"
        task_status = "done" if outcome.status == "completed" else "blocked"
        _transition_effect(
            operation_key,
            envelope,
            {"prepared"},
            {"state": "executing", "envelope": envelope},
        )
        _record_effect_event(run, operation_key, "executing", "task_update")
        try:
            result = self._tool_registry["task_update"](
                id=run.task_id,
                status=task_status,
                append_note=note,
                worktree=False,
                expected_project=snapshot.project,
                expected_updated_at=run.task_updated_at,
                idempotency_key=f"{operation_key}:task-update",
                actor=run.claimed_by,
            )
        except Exception as exc:
            _transition_effect(
                operation_key,
                envelope,
                {"executing"},
                {"state": "unknown", "error": _bounded_error(exc), "envelope": envelope},
            )
            _record_effect_event(run, operation_key, "unknown", "task_update")
            raise OutcomeRecordingError(
                "canonical task outcome is uncertain; reconcile before retrying"
            ) from exc
        if result.get("id") != run.task_id or result.get("status") != task_status:
            _transition_effect(
                operation_key,
                envelope,
                {"executing"},
                {"state": "unknown", "error": "unexpected task result", "envelope": envelope},
            )
            raise OutcomeRecordingError(
                "canonical task outcome returned an unexpected result; reconcile before retrying"
            )
        _transition_effect(
            operation_key,
            envelope,
            {"executing"},
            {"state": "succeeded", "result": result, "envelope": envelope},
        )
        _record_effect_event(run, operation_key, "succeeded", "task_update")
        return result


def _precheck_policy(tool_name: str, arguments: dict[str, Any]) -> None:
    """Run the hub policy gate before consuming a one-shot approval pass."""
    safety.check_policy_gate(tool_name, arguments)


_TRANSIENT_HTTP_STATUS = frozenset({429, 502, 503, 504})


def _walk_exception_chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_retryable_web_search_failure(tool_name: str, exc: BaseException) -> bool:
    """Classify production web_search transport failures for pilot transient retries."""
    if tool_name != "web_search":
        return False
    if isinstance(exc, TimeoutError) and "attempt deadline" in str(exc):
        return False
    for err in _walk_exception_chain(exc):
        if isinstance(err, httpx.TimeoutException):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            if err.response.status_code in _TRANSIENT_HTTP_STATUS:
                return True
        if isinstance(err, httpx.TransportError):
            return True
        if isinstance(err, OSError):
            return True
        if isinstance(err, ToolError):
            lowered = str(err).lower()
            if any(
                token in lowered
                for token in (
                    "connecterror",
                    "timeout",
                    "timed out",
                    "rate limit",
                    "429",
                    "502",
                    "503",
                    "504",
                )
            ):
                return True
    return False


def _invoke_with_timeout(
    callable_fn: Callable[..., dict[str, Any]],
    *,
    timeout_s: float,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute a governed registry call with a hard attempt deadline.

    Task #1221: raising ``TimeoutError`` here only means *this caller stopped
    waiting* — a requested outcome, not a confirmed one. ``cancel_futures``
    only drops futures that have not started; the isolated callable already
    running on the pool thread keeps executing after this function returns
    and may still complete (or mutate state) later. The caller
    (``GovernedGateway`` above) correctly treats this as the "unknown" leg of
    the same requested/confirmed/unknown vocabulary ``dispatch.supervise``
    uses for process termination — never as "failed", and never as proof the
    call had no effect.
    """
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ctx.run, functools.partial(callable_fn, **kwargs))
    timed_out = False
    try:
        result = future.result(timeout=timeout_s)
    except TimeoutError as exc:
        # Deadline expiry only. The worker may still be running, so do not
        # join it. timed_out keeps the finally block from shutting down again.
        timed_out = True
        pool.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(
            f"governed tool call exceeded the {timeout_s}s attempt deadline"
        ) from exc
    finally:
        if not timed_out:
            pool.shutdown(wait=True)
    return result


def _default_tool_registry() -> Mapping[str, Callable[..., dict[str, Any]]]:
    # The governed surface, without making the pilot or adapter a server entrypoint.
    from . import tool_surface

    return tool_surface.registry()


def _require_identity_profile(run: PilotRun) -> tuple[str, str]:
    """Verify the authenticated session without treating cached task state as authority."""
    run.validate()
    identity = config.verified_agent_identity()
    if not config.enforce_auth() or not identity or not profiles._current_session_id():
        raise StaleAuthorityError("governed dispatch requires an authenticated HTTP hub session")
    if identity != run.claimed_by:
        raise StaleAuthorityError("verified identity no longer matches the task owner")
    profile = profiles.current()
    if profile != run.expected_profile:
        raise StaleAuthorityError("active profile no longer matches the run precondition")
    return identity, profile


def _claim_effect(operation_key: str, envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Atomically claim an effect key or return its committed result."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (operation_key,)
        ).fetchone()
        if row is not None:
            if row["tool"] != _EFFECT_TOOL:
                raise DuplicateDeliveryUncertainError(
                    "operation key is already owned by another control-plane effect"
                )
            record = _decode_effect(row["result_json"])
            if record.get("envelope") != envelope:
                raise DuplicateDeliveryUncertainError(
                    "operation key was reused with a different immutable envelope"
                )
            if record.get("state") == "succeeded":
                conn.commit()
                return record
            if record.get("state") == "denied":
                conn.commit()
                return record
            if record.get("state") == "failed":
                raise PilotError(
                    "the governed effect previously failed; create a new authorized run"
                )
            raise DuplicateDeliveryUncertainError(
                f"duplicate delivery found effect state {record.get('state', 'invalid')!r}; "
                "reconcile before retrying"
            )
        record = {"state": "prepared", "envelope": envelope}
        conn.execute(
            "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?)",
            (operation_key, _EFFECT_TOOL, _encode_effect(record), storage.now()),
        )
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _transition_effect(
    operation_key: str,
    envelope: dict[str, Any],
    expected_states: set[str],
    record: dict[str, Any],
) -> None:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (operation_key,)
        ).fetchone()
        if row is None or row["tool"] != _EFFECT_TOOL:
            raise DuplicateDeliveryUncertainError("control-plane effect claim disappeared")
        current = _decode_effect(row["result_json"])
        if current.get("envelope") != envelope or current.get("state") not in expected_states:
            raise DuplicateDeliveryUncertainError(
                "control-plane effect changed while its outcome was being recorded"
            )
        record["envelope"] = envelope
        conn.execute(
            "UPDATE idempotency_keys SET result_json=? WHERE key=? AND tool=?",
            (_encode_effect(record), operation_key, _EFFECT_TOOL),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_prepared_effect(operation_key: str, envelope: dict[str, Any]) -> None:
    """Release a pre-execution claim when the canonical approval is pending."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (operation_key,)
        ).fetchone()
        if row is None or row["tool"] != _EFFECT_TOOL:
            raise DuplicateDeliveryUncertainError("control-plane effect claim disappeared")
        record = _decode_effect(row["result_json"])
        if record.get("state") != "prepared" or record.get("envelope") != envelope:
            raise DuplicateDeliveryUncertainError(
                "control-plane effect changed before the approval interrupt"
            )
        conn.execute(
            "DELETE FROM idempotency_keys WHERE key=? AND tool=?",
            (operation_key, _EFFECT_TOOL),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _abort_executing_effect(
    operation_key: str,
    envelope: dict[str, Any],
    *,
    approval_id: int | None = None,
) -> None:
    """Release an executing effect so transient gateway retries can reclaim it."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (operation_key,)
        ).fetchone()
        if row is None or row["tool"] != _EFFECT_TOOL:
            raise DuplicateDeliveryUncertainError("control-plane effect claim disappeared")
        record = _decode_effect(row["result_json"])
        if record.get("state") != "executing" or record.get("envelope") != envelope:
            raise DuplicateDeliveryUncertainError(
                "control-plane effect changed before transient retry release"
            )
        conn.execute(
            "DELETE FROM idempotency_keys WHERE key=? AND tool=?",
            (operation_key, _EFFECT_TOOL),
        )
        if approval_id is not None:
            conn.execute(
                "UPDATE approvals SET status='approved', updated_at=? "
                "WHERE id=? AND status='consumed'",
                (storage.now(), approval_id),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _pending_approval_id(envelope: dict[str, Any]) -> int | None:
    canon = canonical_approval_payload(envelope)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id FROM approvals WHERE tool=? AND payload=? AND status='pending' "
            "AND org_id IS ? ORDER BY id LIMIT 1",
            (_APPROVAL_TOOL, canon, edition.tenant_org_id()),
        ).fetchone()
        return int(row["id"]) if row is not None else None
    finally:
        conn.close()


def _restore_invocation_receipts(receipts: dict[Any, Any] | None) -> None:
    """Return consumed one-shot passes when the adapter parks before execution."""
    if not receipts:
        return
    for receipt in receipts.values():
        if not isinstance(receipt, dict):
            continue
        approval_id = receipt.get("approval_id")
        if approval_id is not None and not receipt.get("_recovered"):
            restore_consumed_approval(int(approval_id))


def _pending_tool_approval_id(tool: str, payload: dict[str, Any]) -> int | None:
    """Find a pending policy-approval row for the bare tool, not the adapter gate."""
    canon = canonical_approval_payload(payload)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id FROM approvals WHERE tool=? AND payload=? AND status='pending' "
            "AND org_id IS ? ORDER BY id LIMIT 1",
            (tool, canon, edition.tenant_org_id()),
        ).fetchone()
        return int(row["id"]) if row is not None else None
    finally:
        conn.close()


def _record_effect_event(run: PilotRun, operation_key: str, state: str, tool: str) -> None:
    try:
        storage.record_agent_event(
            "langgraph_effect",
            ref_id=operation_key,
            agent=config.verified_agent_identity(),
            summary=f"{state}: task #{run.task_id} {tool}",
        )
    except Exception:
        # The effect ledger is authoritative. The activity index is a bounded,
        # best-effort projection with the same failure policy as task_update.
        pass


def _decode_effect(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DuplicateDeliveryUncertainError("control-plane effect record is corrupt") from exc
    if not isinstance(value, dict):
        raise DuplicateDeliveryUncertainError("control-plane effect record is not an object")
    return value


def _encode_effect(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise DuplicateDeliveryUncertainError(
            "control-plane effect result is not JSON-serializable"
        ) from exc


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bounded_error(exc: Exception) -> str:
    text = redact_sensitive_content(str(exc), max_len=200)
    return f"{type(exc).__name__}: {text}"


def _parse_denial(message: str) -> tuple[int, str] | None:
    match = _DENIED_RE.search(message)
    if match is None:
        return None
    return int(match.group(1)), match.group(2).strip()


def _digest_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise PilotError("governed adapter values must be JSON-serializable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
