"""One terminal, approval-governed progress receipt; no task completion effects."""

from __future__ import annotations

import inspect
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from . import langgraph_dispatch, langgraph_receipt_dispatch
from .langgraph_note_dispatch import ProgressNoteDispatchAdapter
from .langgraph_pilot import (
    _DELIVERY_LOCKS,
    _DELIVERY_LOCKS_GUARD,
    _ULID_RE,
    CONTRACT_VERSION,
    DefinitionMismatchError,
    GovernedToolOutcome,
    InvalidRunError,
    PilotError,
    PilotRun,
    _bounded_result,
    _digest_json,
    _open_checkpoint,
    default_checkpoint_path,
)
from .telemetry.redaction import redact_sensitive_content

GRAPH_NAME = "governed_progress_note"
GRAPH_VERSION = "1.0.0"
STATE_SCHEMA_VERSION = 1


class NoteGateway(Protocol):
    def revalidate(self, run: PilotRun) -> None: ...

    def call_progress_note(
        self, *, run: PilotRun, output_digest: str, timeout_s: float = 20
    ) -> GovernedToolOutcome: ...

    def reconcile_progress_note(
        self, *, run: PilotRun, output_digest: str
    ) -> GovernedToolOutcome | None: ...


class NoteState(TypedDict, total=False):
    authority: dict[str, Any]
    input_digest: str
    contract_version: str
    graph_name: str
    graph_version: str
    definition_digest: str
    state_schema_version: int
    status: str
    approval_id: str | None
    denial_reason: str | None
    output: dict[str, Any] | None


def _validate_run(run: PilotRun) -> None:
    run.validate()
    if not isinstance(run.query, str) or not re.fullmatch(r"[0-9a-f]{64}", run.query):
        raise InvalidRunError("progress note input must be a lowercase SHA-256 digest")
    if type(run.task_id) is not int:
        raise InvalidRunError("task_id must be an integer")
    for key, value in asdict(run).items():
        if key in {"task_id", "required_capabilities"}:
            continue
        if not isinstance(value, str) or len(value) > 256:
            raise InvalidRunError("progress note authority fields must be bounded strings")
        if redact_sensitive_content(value, max_len=len(value) + 1) != value:
            raise InvalidRunError("progress note authority contains a recognized secret")
    if len(run.required_capabilities) > 20 or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value)
        for value in run.required_capabilities
    ):
        raise InvalidRunError("progress note capabilities must be bounded names")


class ProgressNoteRunner:
    """Dedicated graph sharing the pilot's secure checkpoint and delivery lock."""

    def __init__(self, checkpoint_path: str | Path | None, gateway: NoteGateway) -> None:
        self.checkpoint_path = Path(checkpoint_path or default_checkpoint_path()).expanduser()
        with _DELIVERY_LOCKS_GUARD:
            self._delivery_lock = _DELIVERY_LOCKS.setdefault(
                str(self.checkpoint_path.resolve(strict=False)), threading.RLock()
            )
        self.gateway = gateway
        self.definition_digest = self._definition_digest()
        self._closed = False
        self._conn = _open_checkpoint(self.checkpoint_path)
        try:
            self._checkpointer = SqliteSaver(
                self._conn,
                serde=JsonPlusSerializer(
                    pickle_fallback=False,
                    allowed_json_modules=None,
                    allowed_msgpack_modules=None,
                ),
            )
            self._checkpointer.setup()
            self._graph = self._build_graph()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ProgressNoteRunner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def _build_graph(self):
        graph = StateGraph(NoteState)
        graph.add_node("progress_note", self._note_node)
        graph.add_node("approval_gate", self._approval_node)
        graph.add_edge(START, "progress_note")
        graph.add_conditional_edges(
            "progress_note", self._route, {"pending": "approval_gate", "terminal": END}
        )
        graph.add_edge("approval_gate", "progress_note")
        return graph.compile(checkpointer=self._checkpointer, name=GRAPH_NAME)

    def _route(self, state: NoteState) -> str:
        return "pending" if state["status"] == "pending" else "terminal"

    def _note_node(self, state: NoteState) -> NoteState:
        run = self._run_from_state(state)
        self._validate_checkpoint(state, run)
        self.gateway.revalidate(run)
        # No retry policy: the gateway quarantines an uncertain write outcome.
        outcome = self.gateway.call_progress_note(run=run, output_digest=run.query, timeout_s=20)
        return self._outcome_state(outcome, run)

    def _approval_node(self, state: NoteState) -> NoteState:
        interrupt(self._approval_payload(state))
        # Resume values are transport signals, never approval or run authority.
        return {}

    def _approval_payload(self, state: NoteState) -> dict[str, Any]:
        return {
            "run_id": state["authority"]["run_id"],
            "task_id": state["authority"]["task_id"],
            "tool_name": "task_update",
            "approval_id": state.get("approval_id"),
            "output_digest": state["authority"]["query"],
        }

    def _outcome_state(self, outcome: GovernedToolOutcome, run: PilotRun) -> NoteState:
        approval_id = outcome.approval_id
        if approval_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", approval_id):
            raise PilotError("invalid progress note approval identifier")
        if outcome.status == "allowed" and outcome.result != {
            "task_id": run.task_id,
            "run_id": run.run_id,
            "output_digest": run.query,
            "note_appended": True,
        }:
            raise PilotError("invalid progress note effect receipt")
        return {
            "status": "completed" if outcome.status == "allowed" else outcome.status,
            "approval_id": approval_id,
            "denial_reason": (
                redact_sensitive_content(outcome.reason, max_len=200) if outcome.reason else None
            ),
            "output": _bounded_result(outcome.result) if outcome.status == "allowed" else None,
        }

    def _initial_state(self, run: PilotRun) -> NoteState:
        authority = asdict(run)
        authority["required_capabilities"] = list(run.required_capabilities)
        return {
            "authority": authority,
            "input_digest": run.input_digest,
            "contract_version": CONTRACT_VERSION,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "definition_digest": self.definition_digest,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "status": "created",
        }

    def _run_from_state(self, state: NoteState) -> PilotRun:
        try:
            authority = dict(state["authority"])
            authority["required_capabilities"] = tuple(authority["required_capabilities"])
            run = PilotRun(**authority)
            _validate_run(run)
            return run
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidRunError("invalid checkpoint authority") from exc

    def _validate_checkpoint(self, state: NoteState, run: PilotRun) -> None:
        if state.get("definition_digest") != self.definition_digest:
            raise DefinitionMismatchError("progress note graph definition changed")
        expected = self._initial_state(run)
        if any(state.get(key) != value for key, value in expected.items() if key != "status"):
            raise InvalidRunError("checkpoint conflicts with immutable progress note run")

    def load_run(self, run_id: str) -> PilotRun:
        if not isinstance(run_id, str) or not _ULID_RE.fullmatch(run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("progress note runner is closed")
            snapshot = self._graph.get_state({"configurable": {"thread_id": f"ua-lg:{run_id}"}})
            if not snapshot.values:
                raise InvalidRunError("progress note checkpoint does not exist")
            run = self._run_from_state(snapshot.values)
            if run.run_id != run_id:
                raise InvalidRunError("checkpoint run_id does not match requested run")
            self._validate_checkpoint(snapshot.values, run)
            return run

    def execute(self, run: PilotRun, *, resume: bool = False) -> dict[str, Any]:
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("progress note runner is closed")
            _validate_run(run)
            cfg = {"configurable": {"thread_id": run.thread_id}}
            snapshot = self._graph.get_state(cfg)
            state = snapshot.values or self._initial_state(run)
            self._validate_checkpoint(state, run)
            historical = self.gateway.reconcile_progress_note(run=run, output_digest=run.query)
            # A persisted denial never becomes executable on duplicate delivery.
            if state.get("status") == "denied":
                return self._result(run, state)
            if historical is not None and historical.status in {"allowed", "denied"}:
                recovered_state = self._outcome_state(historical, run)
                if snapshot.values and state.get("status") not in {"completed", "denied"}:
                    self._graph.update_state(cfg, recovered_state, as_node="progress_note")
                return self._result(run, {**state, **recovered_state})
            if state.get("status") == "completed":
                raise InvalidRunError("completed checkpoint lacks a confirmed effect receipt")
            self.gateway.revalidate(run)
            if snapshot.values:
                if not resume:
                    return self._result(run, state)
                # Exceptions leave a scheduled node, whereas interrupts require Command.
                graph_input = Command(resume=True) if snapshot.interrupts else None
            else:
                graph_input = state
            self._graph.invoke(graph_input, cfg)
            return self._result(run, self._graph.get_state(cfg).values)

    def _result(self, run: PilotRun, state: NoteState) -> dict[str, Any]:
        status = state["status"]
        if status not in {"created", "pending", "completed", "denied"}:
            raise InvalidRunError("invalid progress note checkpoint status")
        # Validate persisted result fields just as strictly as fresh gateway data.
        if status == "completed":
            clean = self._outcome_state(
                GovernedToolOutcome.allowed(
                    state.get("output"), approval_id=state.get("approval_id")
                ),
                run,
            )
        elif status == "pending":
            clean = self._outcome_state(
                GovernedToolOutcome.pending(state.get("approval_id") or ""), run
            )
        elif status == "denied":
            clean = self._outcome_state(
                GovernedToolOutcome.denied(
                    state.get("denial_reason") or "Denied", approval_id=state.get("approval_id")
                ),
                run,
            )
        else:
            clean = {"output": None, "denial_reason": None}
        return {
            "status": "interrupted" if status == "pending" else status,
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "output": clean.get("output"),
            "interrupted_by": self._approval_payload(state)
            if state["status"] == "pending"
            else None,
            "reason": clean.get("denial_reason"),
        }

    def _definition_digest(self) -> str:
        return _digest_json(
            {
                "graph": GRAPH_NAME,
                "version": GRAPH_VERSION,
                "schema_version": STATE_SCHEMA_VERSION,
                "contract": CONTRACT_VERSION,
                "executable_sources": [
                    inspect.getsource(method.__func__)
                    for method in (
                        self._build_graph,
                        self._note_node,
                        self._approval_node,
                        self._route,
                        self._run_from_state,
                        self._validate_checkpoint,
                        self._initial_state,
                        self._outcome_state,
                        self.execute,
                        self.load_run,
                        self._result,
                        self._approval_payload,
                    )
                ],
                "sources": [
                    inspect.getsource(obj)
                    for obj in (
                        NoteState,
                        PilotRun,
                        GovernedToolOutcome,
                        _validate_run,
                        _bounded_result,
                        _digest_json,
                        _open_checkpoint,
                        ProgressNoteRunner,
                        ProgressNoteDispatchAdapter,
                        # The shared base runs approval, receipt validation
                        # and the write path, so its whole module is pinned.
                        langgraph_dispatch,
                        langgraph_receipt_dispatch,
                    )
                ],
            }
        )
