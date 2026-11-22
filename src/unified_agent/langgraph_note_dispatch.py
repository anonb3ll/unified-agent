"""One approved, generated progress receipt; uncertain writes are never replayed."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from . import langgraph_dispatch as dispatch
from . import profiles
from .langgraph_pilot import GovernedToolOutcome, InvalidRunError, PilotRun, StaleAuthorityError
from .langgraph_receipt_dispatch import ReceiptDispatchAdapter


class ProgressNoteDispatchAdapter(ReceiptDispatchAdapter):
    """Append only the fixed receipt to the run's own task through wrapped task_update."""

    _effect_noun = "progress"

    def _validate_note_run(self, run: PilotRun, output_digest: str) -> None:
        dispatch._require_identity_profile(run)
        if (
            not isinstance(output_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", output_digest)
            or run.query != output_digest
        ):
            raise InvalidRunError("progress note requires a SHA-256 output digest")
        if "task_update" not in run.required_capabilities or any(
            capability not in self._tool_registry or not profiles.tool_is_allowed(capability)
            for capability in run.required_capabilities
        ):
            raise StaleAuthorityError("progress note requires the task_update capability")

    @staticmethod
    def _key(run: PilotRun) -> str:
        return f"ua-lg:{run.run_id}:governed_tool:progress-note"

    def _envelope(self, run: PilotRun, output_digest: str, project: str) -> dict[str, Any]:
        key = self._key(run)
        return {
            "contract_version": "ua.langgraph.progress-note/v1",
            "run": json.loads(json.dumps(asdict(run))),
            "operation_key": key,
            "tool": "task_update",
            "arguments": {
                "id": run.task_id,
                "append_note": f"LangGraph progress receipt run={run.run_id} output={output_digest}",
                "expected_project": project,
                "expected_updated_at": run.task_updated_at,
                "worktree": False,
                "idempotency_key": f"{key}:task-update",
            },
        }

    @staticmethod
    def _bounded_receipt(run: PilotRun, output_digest: str) -> dict[str, Any]:
        return {
            "task_id": run.task_id,
            "run_id": run.run_id,
            "output_digest": output_digest,
            "note_appended": True,
        }

    def _validate_effect(self, run: PilotRun, **spec: Any) -> None:
        self._validate_note_run(run, spec["output_digest"])

    def _effect_key(self, run: PilotRun, **spec: Any) -> str:
        return self._key(run)

    def _effect_envelope(self, run: PilotRun, project: str, **spec: Any) -> dict[str, Any]:
        return self._envelope(run, spec["output_digest"], project)

    def _effect_receipt(self, run: PilotRun, **spec: Any) -> dict[str, Any]:
        return self._bounded_receipt(run, spec["output_digest"])

    def _after_restoring_pre_execute_approval(self, key: str, envelope: dict[str, Any]) -> None:
        """Retain the prepared claim identified by ``key`` and ``envelope``.

        An ambiguous ledger transition must never make this write automatically
        eligible for another invocation.
        """

    def reconcile_progress_note(
        self, *, run: PilotRun, output_digest: str
    ) -> GovernedToolOutcome | None:
        """Read historical authority only; never invoke or retry a task mutation."""
        return self._reconcile_effect(run=run, output_digest=output_digest)

    def call_progress_note(
        self, *, run: PilotRun, output_digest: str, timeout_s: float = 20
    ) -> GovernedToolOutcome:
        return self._call_effect(run=run, timeout_s=timeout_s, output_digest=output_digest)
