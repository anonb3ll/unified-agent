"""Two approved task-note hops; uncertain writes are never replayed."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Literal

from . import langgraph_dispatch as dispatch
from . import profiles
from .langgraph_pilot import GovernedToolOutcome, InvalidRunError, PilotRun, StaleAuthorityError
from .langgraph_receipt_dispatch import ReceiptDispatchAdapter

Hop = Literal["build", "merge"]
_HOPS = frozenset({"build", "merge"})
_DIGEST_FIELD = {"build": "plan", "merge": "verify"}


class WorkflowDispatchAdapter(ReceiptDispatchAdapter):
    """Append generated build then merge receipts through wrapped task_update."""

    _effect_noun = "workflow"

    def _validate_hop_run(self, run: PilotRun, hop: str, digest: str) -> None:
        dispatch._require_identity_profile(run)
        if hop not in _HOPS:
            raise InvalidRunError("workflow hop must be build or merge")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InvalidRunError("workflow hop requires a SHA-256 digest")
        if "task_update" not in run.required_capabilities or any(
            capability not in self._tool_registry or not profiles.tool_is_allowed(capability)
            for capability in run.required_capabilities
        ):
            raise StaleAuthorityError("workflow hops require the task_update capability")

    @staticmethod
    def _key(run: PilotRun, hop: str) -> str:
        return f"ua-lg:{run.run_id}:governed_tool:workflow-{hop}"

    @staticmethod
    def _receipt_line(run: PilotRun, hop: str, digest: str) -> str:
        field = _DIGEST_FIELD[hop]
        return f"LangGraph workflow {hop} run={run.run_id} agent={run.claimed_by} {field}={digest}"

    def _envelope(self, run: PilotRun, hop: str, digest: str, project: str) -> dict[str, Any]:
        key = self._key(run, hop)
        return {
            "contract_version": "ua.langgraph.workflow/v1",
            "run": json.loads(json.dumps(asdict(run))),
            "operation_key": key,
            "tool": "task_update",
            "hop": hop,
            "arguments": {
                "id": run.task_id,
                "append_note": self._receipt_line(run, hop, digest),
                "expected_project": project,
                "expected_updated_at": run.task_updated_at,
                "worktree": False,
                "idempotency_key": f"{key}:task-update",
            },
        }

    @staticmethod
    def _bounded_receipt(run: PilotRun, hop: str, digest: str) -> dict[str, Any]:
        return {
            "task_id": run.task_id,
            "run_id": run.run_id,
            "claimed_by": run.claimed_by,
            "hop": hop,
            "digest": digest,
            "note_appended": True,
        }

    def _validate_effect(self, run: PilotRun, **spec: Any) -> None:
        self._validate_hop_run(run, spec["hop"], spec["digest"])

    def _effect_key(self, run: PilotRun, **spec: Any) -> str:
        return self._key(run, spec["hop"])

    def _effect_envelope(self, run: PilotRun, project: str, **spec: Any) -> dict[str, Any]:
        return self._envelope(run, spec["hop"], spec["digest"], project)

    def _effect_receipt(self, run: PilotRun, **spec: Any) -> dict[str, Any]:
        return self._bounded_receipt(run, spec["hop"], spec["digest"])

    def _after_restoring_pre_execute_approval(self, key: str, envelope: dict[str, Any]) -> None:
        dispatch._release_prepared_effect(key, envelope)

    def reconcile_hop(self, *, run: PilotRun, hop: str, digest: str) -> GovernedToolOutcome | None:
        """Read historical authority only; never invoke or retry a task mutation."""
        return self._reconcile_effect(run=run, hop=hop, digest=digest)

    def refresh_after_hop(self, run: PilotRun, hop: str, digest: str) -> PilotRun:
        """Adopt the task revision created by this run's own hop; foreign edits fail closed."""
        self._validate_hop_run(run, hop, digest)
        return self._refresh_after_receipt(
            run,
            self._receipt_line(run, hop, digest),
            changed_error="canonical task changed after the workflow hop",
            revision_error="expected a new task revision after the workflow hop",
        )

    def call_hop(
        self, *, run: PilotRun, hop: str, digest: str, timeout_s: float = 20
    ) -> GovernedToolOutcome:
        return self._call_effect(run=run, timeout_s=timeout_s, hop=hop, digest=digest)
