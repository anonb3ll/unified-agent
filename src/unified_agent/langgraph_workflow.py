"""Governed plan→build→verify→merge chain; merge is the second mutating hop."""

from __future__ import annotations

import inspect
import re
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from mcp.server.mcpserver.exceptions import ToolError

from . import edition, langgraph_dispatch, langgraph_receipt_dispatch, storage
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
from .langgraph_workflow_dispatch import WorkflowDispatchAdapter
from .telemetry.redaction import redact_sensitive_content

GRAPH_NAME = "governed_workflow"
GRAPH_VERSION = "1.2.0"
STATE_SCHEMA_VERSION = 1
PLAN_STEP_INDEX = {"plan": 0, "build": 1, "verify": 2, "merge": 3}
HOP_BY_INDEX = {idx: hop for hop, idx in PLAN_STEP_INDEX.items()}
PLAN_GOAL_PREFIX = "governed workflow "
_PLAN_GOAL_RE = re.compile(r"governed workflow ([0-9A-HJKMNP-TV-Z]{26})")
ORIGIN_EXPIRED_PREFIX = "origin authority expired"


def origin_authority_lapse(run: PilotRun, now: datetime | None = None) -> str | None:
    """Why ``run``'s origin authority no longer holds, or None while it is live (#1497).

    Deliberately narrower than ``revalidate``: a changed task revision or a
    renewed lease by the same claimant does not count, so a run whose claimant
    still holds a live lease on a ``doing`` task is never treated as dead.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by, lock_expires_at FROM tasks WHERE id=?", (run.task_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "origin task no longer exists"
    if row["status"] != "doing":
        return f"origin task is {row['status']}"
    if row["claimed_by"] != run.claimed_by:
        return "origin task owner changed"
    raw = row["lock_expires_at"]
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
    except ValueError:
        expires = None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires is None or (now or datetime.now(UTC)) >= expires:
        return "origin claim lease expired"
    return None


class WorkflowGateway(Protocol):
    def revalidate(self, run: PilotRun) -> None: ...

    def call_hop(
        self, *, run: PilotRun, hop: str, digest: str, timeout_s: float = 20
    ) -> GovernedToolOutcome: ...

    def reconcile_hop(
        self, *, run: PilotRun, hop: str, digest: str
    ) -> GovernedToolOutcome | None: ...

    def refresh_after_hop(self, run: PilotRun, hop: str, digest: str) -> PilotRun: ...


class WorkflowState(TypedDict, total=False):
    origin_authority: dict[str, Any]
    authority: dict[str, Any]
    input_digest: str
    contract_version: str
    graph_name: str
    graph_version: str
    definition_digest: str
    state_schema_version: int
    status: str
    pending_hop: str | None
    plan_digest: str | None
    verify_digest: str | None
    build_output: dict[str, Any] | None
    merge_output: dict[str, Any] | None
    approval_id: str | None
    denial_reason: str | None
    plan_id: str | None


def _authority_dict(run: PilotRun) -> dict[str, Any]:
    payload = asdict(run)
    payload["required_capabilities"] = list(run.required_capabilities)
    return payload


def _run_from_authority(authority: dict[str, Any]) -> PilotRun:
    try:
        payload = dict(authority)
        payload["required_capabilities"] = tuple(payload["required_capabilities"])
        run = PilotRun(**payload)
        run.validate()
        return run
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidRunError("invalid checkpoint authority") from exc


class WorkflowRunner:
    """Dedicated graph sharing the pilot's secure checkpoint and delivery lock."""

    def __init__(self, checkpoint_path: str | Path | None, gateway: WorkflowGateway) -> None:
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

    def __enter__(self) -> WorkflowRunner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def _build_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("dispatch", self._dispatch_node)
        graph.add_node("plan", self._plan_node)
        graph.add_node("build", self._build_node)
        graph.add_node("build_gate", self._build_gate_node)
        graph.add_node("verify", self._verify_node)
        graph.add_node("merge", self._merge_node)
        graph.add_node("merge_gate", self._merge_gate_node)
        graph.add_edge(START, "dispatch")
        graph.add_conditional_edges(
            "dispatch",
            self._route_from_plan,
            {
                "plan": "plan",
                "build": "build",
                "verify": "verify",
                "merge": "merge",
                "denied": END,
                "terminal": END,
            },
        )
        graph.add_edge("plan", "dispatch")
        graph.add_conditional_edges(
            "build",
            self._route_after_build,
            {"approval": "build_gate", "dispatch": "dispatch", "denied": END},
        )
        graph.add_edge("build_gate", "build")
        graph.add_edge("verify", "dispatch")
        graph.add_conditional_edges(
            "merge",
            self._route_after_merge,
            {"approval": "merge_gate", "terminal": END},
        )
        graph.add_edge("merge_gate", "merge")
        return graph.compile(checkpointer=self._checkpointer, name=GRAPH_NAME)

    def _dispatch_node(self, state: WorkflowState) -> WorkflowState:
        origin = _run_from_authority(state["origin_authority"])
        self._validate_checkpoint(state, origin)
        hydrated = self._hydrate_from_plan(state)
        hop = self._next_hop_from_plan(hydrated)
        if hydrated.get("status") not in {"pending", "denied", "completed"}:
            hydrated["pending_hop"] = hop
            hydrated["status"] = "running"
        return hydrated

    def _route_from_plan(self, state: WorkflowState) -> str:
        if state.get("status") == "denied":
            return "denied"
        if state.get("status") == "completed":
            return "terminal"
        hop = self._next_hop_from_plan(state)
        return hop or "terminal"

    def _route_after_build(self, state: WorkflowState) -> str:
        if state.get("status") == "pending":
            return "approval"
        if state.get("status") == "denied":
            return "denied"
        return "dispatch"

    def _route_after_merge(self, state: WorkflowState) -> str:
        return "approval" if state.get("status") == "pending" else "terminal"

    def _plan_node(self, state: WorkflowState) -> WorkflowState:
        run = _run_from_authority(state["origin_authority"])
        self._validate_checkpoint(state, run)
        self.gateway.revalidate(run)
        self._claim_plan_step(state, "plan", run.claimed_by)
        query = " ".join(run.query.split())
        digest = _digest_json({"query": query})
        self._handoff_plan_step(state, "plan", {"plan_digest": digest}, run.claimed_by)
        return {
            "plan_digest": digest,
            "status": "running",
            "pending_hop": None,
        }

    def _build_node(self, state: WorkflowState) -> WorkflowState:
        origin = _run_from_authority(state["origin_authority"])
        self._validate_checkpoint(state, origin)
        plan_digest = state["plan_digest"]
        if not isinstance(plan_digest, str):
            raise PilotError("build hop requires a plan digest")
        self.gateway.revalidate(origin)
        self._claim_plan_step(state, "build", origin.claimed_by)
        outcome = self.gateway.call_hop(run=origin, hop="build", digest=plan_digest, timeout_s=20)
        updates = self._outcome_state(outcome, hop="build")
        if outcome.status == "allowed":
            refreshed = self.gateway.refresh_after_hop(origin, "build", plan_digest)
            updates["authority"] = _authority_dict(refreshed)
            updates["build_output"] = _bounded_result(outcome.result)
            updates["status"] = "running"
            updates["pending_hop"] = None
            self._handoff_plan_step(
                state,
                "build",
                {
                    "build": _bounded_result(outcome.result),
                    "task_updated_at": refreshed.task_updated_at,
                },
                origin.claimed_by,
            )
        elif outcome.status == "denied":
            self._fail_plan_step(state, "build", origin.claimed_by, outcome.reason)
        return updates

    def _verify_node(self, state: WorkflowState) -> WorkflowState:
        origin = _run_from_authority(state["origin_authority"])
        self._validate_checkpoint(state, origin)
        build_output = state.get("build_output")
        plan_digest = state.get("plan_digest")
        if not build_output or not plan_digest:
            raise PilotError("verify requires a completed build hop")
        current = _run_from_authority(state["authority"])
        self.gateway.revalidate(current)
        self._claim_plan_step(state, "verify", origin.claimed_by)
        digest = _digest_json({"plan_digest": plan_digest, "build": build_output})
        self._handoff_plan_step(state, "verify", {"verify_digest": digest}, origin.claimed_by)
        return {
            "verify_digest": digest,
            "status": "running",
            "pending_hop": None,
        }

    def _merge_node(self, state: WorkflowState) -> WorkflowState:
        origin = _run_from_authority(state["origin_authority"])
        self._validate_checkpoint(state, origin)
        verify_digest = state.get("verify_digest")
        if not isinstance(verify_digest, str) or not state.get("build_output"):
            raise PilotError("merge hop requires a completed verify hop")
        current = _run_from_authority(state["authority"])
        self.gateway.revalidate(current)
        self._claim_plan_step(state, "merge", origin.claimed_by)
        outcome = self.gateway.call_hop(
            run=current, hop="merge", digest=verify_digest, timeout_s=20
        )
        updates = self._outcome_state(outcome, hop="merge")
        if outcome.status == "allowed":
            updates["merge_output"] = _bounded_result(outcome.result)
            updates["status"] = "completed"
            updates["pending_hop"] = None
            self._handoff_plan_step(
                state, "merge", {"merge": _bounded_result(outcome.result)}, origin.claimed_by
            )
        elif outcome.status == "denied":
            self._fail_plan_step(state, "merge", origin.claimed_by, outcome.reason)
        return updates

    def _build_gate_node(self, state: WorkflowState) -> WorkflowState:
        interrupt(self._approval_payload(state, "build"))
        return {"pending_hop": None}

    def _merge_gate_node(self, state: WorkflowState) -> WorkflowState:
        interrupt(self._approval_payload(state, "merge"))
        return {"pending_hop": None}

    def _approval_payload(self, state: WorkflowState, hop: str) -> dict[str, Any]:
        origin = state["origin_authority"]
        return {
            "run_id": origin["run_id"],
            "task_id": origin["task_id"],
            "claimed_by": origin["claimed_by"],
            "node_key": hop,
            "tool_name": "task_update",
            "approval_id": state.get("approval_id"),
            "hop": hop,
        }

    def _bind_plan(self, run: PilotRun) -> str:
        from .tools.plan import plan_create, plan_list

        goal = f"governed workflow {run.run_id}"
        for plan in plan_list(status="all", limit=200)["plans"]:
            if plan.get("goal") == goal:
                return plan["id"]
        created = plan_create(
            goal=goal,
            steps=[
                {"description": "plan", "owner": run.claimed_by},
                {"description": "build", "owner": run.claimed_by, "depends_on": [0]},
                {"description": "verify", "owner": run.claimed_by, "depends_on": [1]},
                {"description": "merge", "owner": run.claimed_by, "depends_on": [2]},
            ],
            author=run.claimed_by,
        )
        return created["plan_id"]

    def _next_hop_from_plan(self, state: WorkflowState) -> str | None:
        from .tools.plan import plan_get

        plan_id = state.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise PilotError("workflow plan_id is missing")
        loaded = plan_get(plan_id)
        if any(row["status"] == "failed" for row in loaded["steps"]):
            return None
        idx = loaded.get("next_step_idx")
        if idx is None:
            return None
        try:
            return HOP_BY_INDEX[idx]
        except KeyError as exc:
            raise PilotError(f"plan {plan_id!r} next step {idx!r} is not a workflow hop") from exc

    def _hydrate_from_plan(self, state: WorkflowState) -> WorkflowState:
        """Copy plan_handoff outputs into scratch state. Plan status wins."""
        from .tools.plan import plan_get

        plan_id = state.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise PilotError("workflow plan_id is missing")
        loaded = plan_get(plan_id)
        steps = {row["step_idx"]: row for row in loaded["steps"]}
        hydrated: WorkflowState = dict(state)

        plan_step = steps.get(0, {})
        if plan_step.get("status") == "done":
            digest = (plan_step.get("outputs") or {}).get("plan_digest")
            if isinstance(digest, str):
                hydrated["plan_digest"] = digest
        else:
            hydrated["plan_digest"] = None

        build_step = steps.get(1, {})
        if build_step.get("status") == "done":
            outputs = build_step.get("outputs") or {}
            build_output = outputs.get("build")
            if isinstance(build_output, dict):
                hydrated["build_output"] = _bounded_result(build_output)
            updated_at = outputs.get("task_updated_at")
            if isinstance(updated_at, str):
                authority = dict(hydrated.get("authority") or hydrated["origin_authority"])
                authority["task_updated_at"] = updated_at
                hydrated["authority"] = authority
        else:
            hydrated["build_output"] = None
            hydrated["authority"] = dict(hydrated["origin_authority"])

        verify_step = steps.get(2, {})
        if verify_step.get("status") == "done":
            digest = (verify_step.get("outputs") or {}).get("verify_digest")
            if isinstance(digest, str):
                hydrated["verify_digest"] = digest
        else:
            hydrated["verify_digest"] = None

        merge_step = steps.get(3, {})
        if merge_step.get("status") == "done":
            merge_output = (merge_step.get("outputs") or {}).get("merge")
            if isinstance(merge_output, dict):
                hydrated["merge_output"] = _bounded_result(merge_output)
        else:
            hydrated["merge_output"] = None

        failed = next((row for row in loaded["steps"] if row["status"] == "failed"), None)
        if failed is not None:
            hydrated["status"] = "denied"
            hydrated["pending_hop"] = HOP_BY_INDEX.get(failed["step_idx"])
            note = failed.get("note")
            hydrated["denial_reason"] = (
                redact_sensitive_content(note, max_len=200) if note else None
            )
        elif merge_step.get("status") == "done" and hydrated.get("merge_output"):
            hydrated["status"] = "completed"
            hydrated["pending_hop"] = None
        elif loaded["plan"].get("status") == "abandoned":
            # A closed plan (task #1497) ends the run as denied, never as success.
            reason = next(
                (
                    (cp.get("context") or {}).get("outputs", {}).get("reason")
                    for cp in reversed(loaded.get("checkpoints") or [])
                    if (cp.get("context") or {}).get("outputs", {}).get("closed_as") == "abandoned"
                ),
                None,
            )
            hydrated["status"] = "denied"
            hydrated["pending_hop"] = None
            hydrated["denial_reason"] = (
                redact_sensitive_content(str(reason), max_len=200) if reason else "plan abandoned"
            )
        elif state.get("status") != "pending":
            hydrated["status"] = "running"
        return hydrated

    def _plan_step(self, state: WorkflowState, hop: str) -> dict[str, Any]:
        from .tools.plan import plan_get

        plan_id = state.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise PilotError("workflow plan_id is missing")
        try:
            step_idx = PLAN_STEP_INDEX[hop]
        except KeyError as exc:
            raise PilotError(f"unknown workflow hop {hop!r}") from exc
        loaded = plan_get(plan_id)
        step = next((row for row in loaded["steps"] if row["step_idx"] == step_idx), None)
        if step is None:
            raise PilotError(f"plan {plan_id!r} has no {hop} step")
        return step

    def _claim_plan_step(self, state: WorkflowState, hop: str, agent: str) -> None:
        from .tools.plan import plan_step_claim

        step = self._plan_step(state, hop)
        if step["status"] == "done":
            return
        if step["status"] == "doing" and step.get("owner") == agent:
            return
        try:
            plan_step_claim(state["plan_id"], PLAN_STEP_INDEX[hop], agent)
        except ToolError as exc:
            raise PilotError(f"plan_step_claim failed for {hop}: {exc}") from exc

    def _handoff_plan_step(
        self, state: WorkflowState, hop: str, outputs: dict[str, Any], agent: str
    ) -> None:
        from .tools.plan import plan_handoff

        step = self._plan_step(state, hop)
        if step["status"] == "done":
            return
        try:
            plan_handoff(state["plan_id"], PLAN_STEP_INDEX[hop], outputs=outputs, agent=agent)
        except ToolError as exc:
            raise PilotError(f"plan_handoff failed for {hop}: {exc}") from exc

    def _fail_plan_step(
        self, state: WorkflowState, hop: str, agent: str, reason: str | None
    ) -> None:
        from .tools.plan import plan_update

        step = self._plan_step(state, hop)
        if step["status"] in {"done", "failed"}:
            return
        try:
            plan_update(
                state["plan_id"],
                PLAN_STEP_INDEX[hop],
                status="failed",
                note=reason,
                owner=agent,
            )
        except ToolError as exc:
            raise PilotError(f"plan_update failed for {hop}: {exc}") from exc

    def _outcome_state(self, outcome: GovernedToolOutcome, hop: str) -> WorkflowState:
        approval_id = outcome.approval_id
        if approval_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", approval_id):
            raise PilotError("invalid workflow approval identifier")
        if outcome.status == "allowed":
            receipt = outcome.result
            if not isinstance(receipt, dict) or receipt.get("hop") != hop:
                raise PilotError("invalid workflow effect receipt")
            if receipt.get("note_appended") is not True:
                raise PilotError("invalid workflow effect receipt")
        if outcome.status == "pending":
            return {
                "status": "pending",
                "pending_hop": hop,
                "approval_id": approval_id,
                "denial_reason": None,
            }
        if outcome.status == "denied":
            return {
                "status": "denied",
                "pending_hop": hop,
                "approval_id": approval_id,
                "denial_reason": (
                    redact_sensitive_content(outcome.reason, max_len=200)
                    if outcome.reason
                    else None
                ),
            }
        return {
            "status": "running",
            "pending_hop": None,
            "approval_id": approval_id,
            "denial_reason": None,
        }

    def _initial_state(self, run: PilotRun) -> WorkflowState:
        authority = _authority_dict(run)
        return {
            "origin_authority": authority,
            "authority": authority,
            "input_digest": run.input_digest,
            "contract_version": CONTRACT_VERSION,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "definition_digest": self.definition_digest,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "status": "created",
            "pending_hop": None,
            "plan_digest": None,
            "verify_digest": None,
            "build_output": None,
            "merge_output": None,
            "approval_id": None,
            "denial_reason": None,
            "plan_id": None,
        }

    def _validate_checkpoint(self, state: WorkflowState, run: PilotRun) -> None:
        if state.get("definition_digest") != self.definition_digest:
            raise DefinitionMismatchError("workflow graph definition changed")
        expected = self._initial_state(run)
        for key in (
            "origin_authority",
            "input_digest",
            "contract_version",
            "graph_name",
            "graph_version",
            "state_schema_version",
        ):
            if state.get(key) != expected[key]:
                raise InvalidRunError("checkpoint conflicts with immutable workflow run")
        current = state.get("authority")
        origin = state.get("origin_authority")
        if not isinstance(current, dict) or not isinstance(origin, dict):
            raise InvalidRunError("checkpoint authority is missing")
        if state.get("build_output"):
            relaxed = dict(origin)
            relaxed["task_updated_at"] = current.get("task_updated_at")
            if current != relaxed or current.get("task_updated_at") == origin.get(
                "task_updated_at"
            ):
                raise InvalidRunError("checkpoint authority did not adopt the build revision")
        elif current != origin:
            raise InvalidRunError("checkpoint authority changed before the build hop")

    def load_run(self, run_id: str) -> PilotRun:
        if not isinstance(run_id, str) or not _ULID_RE.fullmatch(run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("workflow runner is closed")
            snapshot = self._graph.get_state({"configurable": {"thread_id": f"ua-lg:{run_id}"}})
            if not snapshot.values:
                raise InvalidRunError("workflow checkpoint does not exist")
            run = _run_from_authority(snapshot.values["origin_authority"])
            if run.run_id != run_id:
                raise InvalidRunError("checkpoint run_id does not match requested run")
            self._validate_checkpoint(snapshot.values, run)
            return run

    def execute(self, run: PilotRun, *, resume: bool = False) -> dict[str, Any]:
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("workflow runner is closed")
            run.validate()
            cfg = {"configurable": {"thread_id": run.thread_id}}
            snapshot = self._graph.get_state(cfg)
            state = snapshot.values or self._initial_state(run)
            if not state.get("plan_id"):
                state = {**state, "plan_id": self._bind_plan(run)}
            state = self._hydrate_from_plan(state)
            self._validate_checkpoint(state, run)
            recovered = self._recover_historical(cfg, state, snapshot.values is not None)
            if recovered is not None:
                return recovered
            if state.get("status") == "denied":
                return self._result(run, state)
            if state.get("status") == "completed":
                if state.get("merge_output") and state.get("build_output"):
                    return self._result(run, state)
                raise InvalidRunError("completed checkpoint lacks a confirmed merge receipt")
            origin = _run_from_authority(state["origin_authority"])
            if state.get("build_output"):
                self.gateway.revalidate(_run_from_authority(state["authority"]))
            else:
                self.gateway.revalidate(origin)
            if snapshot.interrupts:
                if not resume:
                    return self._result(run, state)
                graph_input: Any = Command(resume=True)
            elif snapshot.values:
                waiting = state.get("status") == "pending"
                if waiting and not resume:
                    return self._result(run, state)
                graph_input = Command(goto="dispatch", update=state)
            else:
                graph_input = state
            self._graph.invoke(graph_input, cfg)
            return self._result(run, self._hydrate_from_plan(self._graph.get_state(cfg).values))

    def _recover_historical(
        self, cfg: dict[str, Any], state: WorkflowState, persisted: bool
    ) -> dict[str, Any] | None:
        origin = _run_from_authority(state["origin_authority"])
        verify_digest = state.get("verify_digest")
        if isinstance(verify_digest, str):
            current = _run_from_authority(state["authority"])
            historical = self.gateway.reconcile_hop(run=current, hop="merge", digest=verify_digest)
            if historical is not None and historical.status in {"allowed", "denied"}:
                recovered_state = self._outcome_state(historical, hop="merge")
                if historical.status == "allowed":
                    merge_output = _bounded_result(historical.result)
                    recovered_state["merge_output"] = merge_output
                    recovered_state["status"] = "completed"
                    recovered_state["pending_hop"] = None
                    # Crash window: merge effect can be durable while the
                    # process dies before plan_handoff. Sync the plan step
                    # before returning completed so the step cannot stay doing.
                    self._claim_plan_step(state, "merge", origin.claimed_by)
                    self._handoff_plan_step(
                        state, "merge", {"merge": merge_output}, origin.claimed_by
                    )
                else:
                    self._fail_plan_step(state, "merge", origin.claimed_by, historical.reason)
                if persisted and state.get("status") not in {"completed", "denied"}:
                    self._graph.update_state(cfg, recovered_state, as_node="merge")
                return self._result(origin, {**state, **recovered_state})
        plan_digest = state.get("plan_digest")
        if isinstance(plan_digest, str) and not state.get("build_output"):
            historical = self.gateway.reconcile_hop(run=origin, hop="build", digest=plan_digest)
            if historical is not None and historical.status == "denied":
                recovered_state = self._outcome_state(historical, hop="build")
                if persisted:
                    self._graph.update_state(cfg, recovered_state, as_node="build")
                return self._result(origin, {**state, **recovered_state})
        return None

    def _result(self, run: PilotRun, state: WorkflowState) -> dict[str, Any]:
        status = state["status"]
        if status not in {"created", "running", "pending", "completed", "denied"}:
            raise InvalidRunError("invalid workflow checkpoint status")
        pending_hop = state.get("pending_hop")
        output = None
        if status == "completed":
            merge_output = state.get("merge_output")
            build_output = state.get("build_output")
            if not merge_output or not build_output:
                raise InvalidRunError("completed workflow is missing hop receipts")
            output = {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "claimed_by": run.claimed_by,
                "plan_digest": state.get("plan_digest"),
                "verify_digest": state.get("verify_digest"),
                "build": _bounded_result(build_output),
                "merge": _bounded_result(merge_output),
            }
        return {
            "status": "interrupted" if status == "pending" else status,
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "claimed_by": run.claimed_by,
            "pending_hop": pending_hop if status == "pending" else None,
            "output": output,
            "interrupted_by": self._approval_payload(state, pending_hop or "build")
            if status == "pending"
            else None,
            "reason": state.get("denial_reason"),
        }

    def progress(self, run_id: str) -> dict[str, Any] | None:
        """The authoritative progress of one run, read from its checkpoints (#2516).

        Read-only: no hop runs, no plan is bound, the gateway is never called. The
        graph state is the run's last checkpoint; each hop's status comes from the
        bound plan, which the run's own hops write (``plan_handoff``), exactly as
        ``execute`` hydrates it. The definition digest is not checked, so a run
        from before a deploy still reads. None when the thread holds no governed
        workflow run with this id.
        """
        from .tools.plan import plan_get

        if not isinstance(run_id, str) or not _ULID_RE.fullmatch(run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("workflow runner is closed")
            cfg = {"configurable": {"thread_id": f"ua-lg:{run_id}"}}
            snapshot = self._graph.get_state(cfg)
            state: WorkflowState = dict(snapshot.values or {})
            origin = state.get("origin_authority")
            if state.get("graph_name") != GRAPH_NAME or not isinstance(origin, dict):
                return None
            if origin.get("run_id") != run_id:
                return None
            checkpoints = sum(1 for _ in self._graph.get_state_history(cfg))
            interrupted = bool(snapshot.interrupts)
            next_nodes = list(snapshot.next or ())
            checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            checkpoint_at = snapshot.created_at
        steps: dict[int, dict[str, Any]] = {}
        plan_status = None
        plan_id = state.get("plan_id")
        if isinstance(plan_id, str) and plan_id:
            try:
                loaded = plan_get(plan_id)
                steps = {row["step_idx"]: row for row in loaded["steps"]}
                plan_status = loaded["plan"].get("status")
                state = self._hydrate_from_plan(state)
            except (ToolError, PilotError, KeyError):
                steps = {}
        status = state.get("status") or "created"
        pending_hop = state.get("pending_hop")
        hops = []
        for hop, idx in PLAN_STEP_INDEX.items():
            step = steps.get(idx) or {}
            hop_status = step.get("status") or "open"
            hops.append(
                {
                    "hop": hop,
                    "step_idx": idx,
                    "status": hop_status,
                    "waiting_approval": status == "pending" and pending_hop == hop,
                    "owner": step.get("owner"),
                    "updated_at": step.get("updated_at"),
                }
            )
        return {
            "run_id": run_id,
            "thread_id": f"ua-lg:{run_id}",
            "task_id": origin.get("task_id"),
            "claimed_by": origin.get("claimed_by"),
            "graph_name": GRAPH_NAME,
            "graph_version": state.get("graph_version"),
            "status": "interrupted" if status == "pending" else status,
            "pending_hop": pending_hop if status in {"pending", "running", "denied"} else None,
            "approval_id": state.get("approval_id") if status == "pending" else None,
            "reason": state.get("denial_reason"),
            "plan_id": plan_id,
            "plan_status": plan_status,
            "hops": hops,
            "interrupted": interrupted,
            "next": next_nodes,
            "checkpoint": {
                "id": checkpoint_id,
                "created_at": checkpoint_at,
                "count": checkpoints,
            },
        }

    def close_if_origin_expired(
        self, run_id: str, *, expected_plan_id: str | None = None
    ) -> dict[str, Any] | None:
        """Close a governed run's bound plan when the run can no longer finish (#1497).

        Returns None, and touches nothing, while the origin authority is live,
        the plan is already terminal, or the run completed with its plan. It
        closes the plan as ``abandoned`` via ``plan_close`` (reason plus run id)
        and records the graph state ``denied`` when the origin authority is gone,
        or when the graph already ended ``denied`` but its plan stayed active.

        It never infers success and never closes an uncertain outcome: a merge
        whose effect may have started (a durable merge operation record exists
        and is not a denial) and a ``completed`` checkpoint whose plan handoff
        is unfinished both raise ``PilotError`` so the sweep reports them as
        unverifiable. A run merely waiting at the merge approval gate has no
        operation record (a pending approval releases it) and is closable.

        ``expected_plan_id`` guards the sweep: plan goals are not unique, so a
        candidate plan that is not the one bound in the checkpoint is refused.

        The checkpoint is read without the definition-digest check: the sweep
        needs only the immutable origin authority, and every deploy changes the
        digest, which would otherwise make every pre-deploy run unclosable.
        """
        from .tools.plan import plan_close, plan_get

        if not isinstance(run_id, str) or not _ULID_RE.fullmatch(run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            if self._closed:
                raise PilotError("workflow runner is closed")
            cfg = {"configurable": {"thread_id": f"ua-lg:{run_id}"}}
            state = self._graph.get_state(cfg).values or {}
            if not state.get("origin_authority"):
                raise InvalidRunError("workflow checkpoint does not exist")
            run = _run_from_authority(state["origin_authority"])
            if run.run_id != run_id:
                raise InvalidRunError("checkpoint run_id does not match requested run")
            plan_id = state.get("plan_id")
            if expected_plan_id is not None and plan_id != expected_plan_id:
                raise PilotError(
                    f"plan {expected_plan_id} is not the plan bound to run {run_id}'s checkpoint"
                )
            if not isinstance(plan_id, str) or not plan_id:
                return None
            if plan_get(plan_id)["plan"]["status"] in {"done", "abandoned"}:
                return None
            status = state.get("status")
            if status == "completed":
                raise PilotError(
                    "checkpoint completed but its plan is still active; "
                    "reconcile the merge handoff instead of closing"
                )
            if status == "denied":
                why = f"workflow denied: {state.get('denial_reason') or 'no reason recorded'}"
            else:
                if (
                    state.get("verify_digest")
                    and not state.get("merge_output")
                    and _merge_effect_started(run)
                ):
                    raise PilotError("merge outcome is uncertain; not closing the plan")
                lapse = origin_authority_lapse(run)
                if lapse is None:
                    return None
                why = f"{ORIGIN_EXPIRED_PREFIX}: {lapse}"
            reason = f"{why} (run {run.run_id})"
            plan_close(
                plan_id,
                reason=reason,
                outputs={"run_id": run.run_id, "task_id": run.task_id, "cause": why},
            )
            if status != "denied":
                self._graph.update_state(
                    cfg,
                    {"status": "denied", "pending_hop": None, "denial_reason": reason},
                    as_node="dispatch",
                )
            return {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "plan_id": plan_id,
                "plan_closed": True,
                "reason": reason,
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
                        self._dispatch_node,
                        self._plan_node,
                        self._build_node,
                        self._build_gate_node,
                        self._verify_node,
                        self._merge_node,
                        self._merge_gate_node,
                        self._route_from_plan,
                        self._route_after_build,
                        self._route_after_merge,
                        self._validate_checkpoint,
                        self._initial_state,
                        self._outcome_state,
                        self._approval_payload,
                        self._bind_plan,
                        self._plan_step,
                        self._claim_plan_step,
                        self._handoff_plan_step,
                        self._fail_plan_step,
                        self._next_hop_from_plan,
                        self._hydrate_from_plan,
                        self.execute,
                        self.load_run,
                        self._result,
                        self._recover_historical,
                    )
                ],
                "sources": [
                    inspect.getsource(obj)
                    for obj in (
                        WorkflowState,
                        PilotRun,
                        GovernedToolOutcome,
                        _authority_dict,
                        _run_from_authority,
                        _bounded_result,
                        _digest_json,
                        _open_checkpoint,
                        WorkflowRunner,
                        WorkflowDispatchAdapter,
                        # The shared base runs approval, receipt validation
                        # and the write path, so its whole module is pinned.
                        langgraph_dispatch,
                        langgraph_receipt_dispatch,
                    )
                ],
            }
        )


def _merge_effect_started(run: PilotRun) -> bool:
    """Whether a merge effect may have started for ``run`` (#1497).

    The merge hop claims a durable operation record before it executes. A
    pending approval releases that record, so a run waiting at the merge gate
    has none. Any remaining record other than a recorded denial means the
    effect is executing, committed or uncertain.
    """
    from . import langgraph_dispatch

    key = WorkflowDispatchAdapter._key(run, "merge")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    try:
        record = langgraph_dispatch._decode_effect(row["result_json"])
    except Exception:  # noqa: BLE001 - an unreadable record is uncertain
        return True
    return record.get("state") != "denied"


class _SweepGateway:
    """The sweep only reads checkpoints; any hop call is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise PilotError(f"workflow sweep must not call gateway.{name}")


def close_expired_workflow_plans(checkpoint_path: str | Path | None = None) -> dict[str, Any]:
    """Close every governed-workflow plan whose run lost its origin authority (#1497).

    Scans plans still ``active``/``blocked`` whose goal names a governed run,
    loads each run from the checkpoint and closes the ones whose origin task is
    gone, finished, re-owned or past its lease. Live and already-ended runs are
    counted in ``left_alone``; runs that cannot be loaded are reported as
    ``unverifiable``, never closed.
    """
    conn = storage.connect()
    try:
        # The process tenant's plans only (#2042): plan_get/plan_close below are
        # fenced on it too, so a foreign plan could never be closed here anyway.
        rows = conn.execute(
            "SELECT id, goal FROM plans WHERE status IN ('active','blocked') AND goal LIKE ? "
            "AND org_id IS ?",
            (PLAN_GOAL_PREFIX + "%", edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    targets = []
    for row in rows:
        match = _PLAN_GOAL_RE.fullmatch(row["goal"] or "")
        if match:
            targets.append((row["id"], match.group(1)))
    result: dict[str, Any] = {
        "checked": len(targets),
        "closed": [],
        "left_alone": 0,
        "unverifiable": [],
    }
    if not targets:
        return result
    path = Path(checkpoint_path or default_checkpoint_path()).expanduser()
    if not path.is_file():
        result["unverifiable"] = [
            {"plan_id": pid, "run_id": rid, "error": "no checkpoint database"}
            for pid, rid in targets
        ]
        return result
    with WorkflowRunner(path, gateway=_SweepGateway()) as runner:
        for plan_id, run_id in targets:
            try:
                closed = runner.close_if_origin_expired(run_id, expected_plan_id=plan_id)
            except (PilotError, ToolError) as exc:
                result["unverifiable"].append(
                    {"plan_id": plan_id, "run_id": run_id, "error": str(exc)[:200]}
                )
                continue
            if closed is None:
                result["left_alone"] += 1
            else:
                result["closed"].append(closed)
    return result


class _ReadOnlyGateway:
    """A gateway for ``run_progress``: it is never called, and refuses if it is."""

    def __getattr__(self, name: str) -> Any:
        raise PilotError(f"progress reads never call the gateway ({name})")


def run_progress(run_id: str, checkpoint_path: str | Path | None = None) -> dict[str, Any] | None:
    """``WorkflowRunner.progress`` over the checkpoint store, or None (task #2516).

    A hub with no checkpoint store has no runs; this never creates the store.
    """
    path = Path(checkpoint_path or default_checkpoint_path()).expanduser()
    if not path.is_file():
        return None
    with WorkflowRunner(path, gateway=_ReadOnlyGateway()) as runner:
        return runner.progress(run_id)
