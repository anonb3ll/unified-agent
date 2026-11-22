"""MCP tools exposing the LangGraph execution plane under Unified-Agent authority."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, langgraph_resume, profiles, storage
from ..langgraph_dispatch import (
    DispatchOutcome,
    UnifiedAgentDispatchAdapter,
)
from ..langgraph_pilot import (
    CONTRACT_VERSION,
    GRAPH_NAME,
    GRAPH_VERSION,
    CheckpointMissingError,
    DefinitionMismatchError,
    DeniedByAuthorityError,
    FenceLostError,
    PilotError,
    PilotEvent,
    PilotResult,
    PilotRun,
    PilotRunner,
    StaleAuthorityError,
    ToolProposal,
    default_checkpoint_path,
)
from ..telemetry.redaction import redact_sensitive_content

logger = logging.getLogger(__name__)

_DEFAULT_PLANNER_DIGEST = hashlib.sha256(b"default-research-planner-v1").hexdigest()


class DefaultResearchPlanner:
    """Deterministic default proposal generator for governed research runs."""

    @property
    def definition_digest(self) -> str:
        return _DEFAULT_PLANNER_DIGEST

    def propose(self, query: str) -> ToolProposal:
        return ToolProposal("web_search", {"query": query, "max_results": 2})


_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_STATUS_BY_OUTCOME = {
    "run_completed": "completed",
    "approval_interrupted": "interrupted",
    "run_denied": "denied",
    "run_failed": "failed",
    "run_conflict": "conflict",
}


def _research_budget_revision(task_id: int) -> str:
    """Pin the hub's autonomy limits; a resume compares, it never refreshes (#2522)."""
    from ..langgraph_pilot import _digest_json

    return _digest_json(
        {"task_id": task_id, "graph": GRAPH_NAME, "limits": config.autonomy_budget_defaults()}
    )


def langgraph_run(
    task_id: int | None = None,
    query: str | None = None,
    auto_record: bool = True,
    run_id: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Execute, or resume, a governed LangGraph research run for a task.

    The run executes durably against the dedicated SQLite checkpoint store
    (/langgraph/checkpoints.sqlite3) with all read tools and side-effects
    governed by Unified-Agent policy, capabilities, and approval gates.

    To continue a run paused for approval, pass its ``run_id`` with
    ``resume=True`` (``task_id`` and ``query`` are optional and, when given, must
    match the run). The run is rebuilt from its latest checkpoint with its
    original claim, task revision, profile, budget and trace; only the run's
    owner or the verified operator (from the Control Center) may resume it, and
    every authority check and approval gate runs again.

    Args:
        task_id: Active task ID in 'doing' status (required to start a run).
        query: Research query or input instruction for the graph (required to start).
        auto_record: Whether to record a completed outcome to the task notes.
        run_id: The paused run to continue (with resume=True).
        resume: Continue ``run_id`` from its checkpoint instead of starting a run.

    Returns:
        Dict with run_id, thread_id, trace_id, budget_revision, status, outcome
        (run_completed, approval_interrupted, run_denied, run_failed or
        run_conflict), code and reason for a refusal, events, and output (set
        only for run_completed).
    """
    if run_id is not None and not resume:
        raise ToolError("run_id continues a paused run: pass resume=True with it")
    if resume:
        if not isinstance(run_id, str) or not run_id:
            raise ToolError("resume=True needs the run_id of the paused run")
        if not _ULID.fullmatch(run_id):
            raise ToolError("run_id must be a 26-character Crockford ULID")
        if task_id is not None and (isinstance(task_id, bool) or task_id <= 0):
            raise ToolError("task_id must be a positive integer")
        return _resume_research_run(run_id, task_id, query, auto_record)
    if task_id is None or isinstance(task_id, bool) or task_id <= 0:
        raise ToolError("task_id must be a positive integer")
    clean_query = (query or "").strip()
    if not clean_query:
        raise ToolError("query must not be empty")

    conn = storage.connect()
    try:
        task = conn.execute(
            "SELECT id, status, updated_at, claimed_by, lock_expires_at, project FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    if task is None:
        raise ToolError(f"task #{task_id} not found")
    if task["status"] != "doing":
        raise ToolError(f"task #{task_id} is in status '{task['status']}' — must be 'doing' to run")
    if not task["claimed_by"]:
        raise ToolError(f"task #{task_id} is unclaimed — claim the task before running LangGraph")

    new_run_id = events.new_ulid()
    run = PilotRun(
        run_id=new_run_id,
        task_id=task_id,
        task_updated_at=task["updated_at"],
        claimed_by=task["claimed_by"],
        claim_expires_at=task["lock_expires_at"] or storage.now(),
        expected_profile=profiles.current(),
        required_capabilities=("web_search",),
        budget_revision=_research_budget_revision(task_id),
        trace_id=f"trace-lg-{new_run_id}",
        query=clean_query,
    )
    adapter = UnifiedAgentDispatchAdapter(budget_revision=_research_budget_revision)
    try:
        with PilotRunner(
            default_checkpoint_path(), planner=DefaultResearchPlanner(), gateway=adapter
        ) as runner:
            return _execute(runner, adapter, run, resume=False, auto_record=auto_record)
    except PilotError as exc:
        raise ToolError(f"LangGraph execution failed: {exc}") from exc


def _resume_research_run(
    run_id: str, task_id: int | None, query: str | None, auto_record: bool
) -> dict[str, Any]:
    """Continue a paused research run from its validated checkpoint (task #2522)."""
    identity = config.verified_agent_identity()

    def refuse(outcome: str, code: str, reason: str) -> dict[str, Any]:
        return _result(
            run_id=run_id, task_id=task_id, outcome=outcome, code=code, reason=reason, resumed=True
        )

    if not (config.enforce_auth() and identity and profiles._current_session_id()):
        return refuse("run_denied", "unauthenticated", "resume requires an authenticated session")
    checkpoint_path = default_checkpoint_path()
    if not checkpoint_path.is_file():
        return refuse("run_failed", "checkpoint_missing", "no checkpoint exists for this run")
    # One gateway for the owner and the operator: the run always executes under
    # its owner's saved profile and current ceiling (langgraph_resume).
    adapter = langgraph_resume.ResearchResumeAdapter(budget_revision=_research_budget_revision)
    try:
        runner = PilotRunner(checkpoint_path, planner=DefaultResearchPlanner(), gateway=adapter)
    except (sqlite3.DatabaseError, PilotError) as exc:
        return refuse("run_failed", "checkpoint_corrupt", _reason(exc))
    with runner:
        try:
            # The lease comes before the load: loading waits on any in-process
            # delivery of this checkpoint, and a second resume must be refused,
            # not queued. The runner's writes are fenced on the lease from here.
            with langgraph_resume.resume_lease(runner, run_id, holder=str(identity)):
                try:
                    loaded = runner.load_run(run_id)
                except CheckpointMissingError as exc:
                    return refuse("run_failed", "checkpoint_missing", _reason(exc))
                except DefinitionMismatchError as exc:
                    return refuse("run_failed", "checkpoint_incompatible", _reason(exc))
                except PilotError as exc:
                    return refuse("run_failed", "checkpoint_corrupt", _reason(exc))
                run = loaded.run
                # Tenant and ownership first: nothing, not even a stored outcome,
                # is returned to a caller who may not read this run.
                denied = langgraph_resume.check_run_access(run)
                if denied is not None:
                    return refuse("run_denied", *denied)
                if (task_id is not None and task_id != run.task_id) or (
                    query is not None and query.strip() and query.strip() != run.query
                ):
                    raise ToolError("resume conflicts with the original task or query")
                if loaded.terminal:
                    stored = runner.terminal_event(run)
                    return _from_events(runner, run, [stored] if stored else [], resumed=True)
                return _execute(
                    runner,
                    adapter,
                    run,
                    resume=True,
                    auto_record=auto_record and identity == run.claimed_by,
                )
        except langgraph_resume.ResumeInFlightError as exc:
            return refuse("run_conflict", "resume_in_flight", _reason(exc))


def _execute(
    runner: PilotRunner,
    adapter: UnifiedAgentDispatchAdapter,
    run: PilotRun,
    *,
    resume: bool,
    auto_record: bool,
) -> dict[str, Any]:
    """Stream the graph and turn its events into one typed result."""
    collected: list[PilotEvent] = []
    try:
        for event in runner.stream(run, resume={"resume": True} if resume else None):
            collected.append(event)
    except (StaleAuthorityError, DeniedByAuthorityError) as exc:
        return _result(
            run=run,
            outcome="run_denied",
            code="stale_authority",
            reason=_reason(exc),
            events=collected,
            resumed=resume,
        )
    except FenceLostError as exc:
        # The lease expired and another resume took it: this holder's write was
        # refused in its own transaction, so it stops and records nothing.
        return _result(
            run=run,
            outcome="run_conflict",
            code="lease_lost",
            reason=_reason(exc),
            events=collected,
            resumed=resume,
        )
    except PilotError as exc:
        return _result(
            run=run,
            outcome="run_failed",
            code="run_error",
            reason=_reason(exc),
            events=collected,
            resumed=resume,
        )
    result = _from_events(runner, run, collected, resumed=resume)
    if result["outcome"] == "run_completed" and not result["duplicate"] and auto_record:
        try:
            adapter.record_task_outcome(
                run,
                DispatchOutcome.completed(
                    PilotResult(
                        status="completed",
                        output=result["output"],
                        duplicate=False,
                        recovered=resume,
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the run's own outcome stands
            logger.warning("Could not auto-record task outcome for run %s: %s", run.run_id, exc)
    return result


def _from_events(
    runner: PilotRunner, run: PilotRun, collected: list[PilotEvent], *, resumed: bool
) -> dict[str, Any]:
    """``run_completed`` only with a real output; every other end is typed."""
    last = collected[-1] if collected else None
    kind = last.kind if last is not None else None
    payload = dict(last.payload) if last is not None else {}
    common: dict[str, Any] = {
        "run": run,
        "events": collected,
        "resumed": resumed,
        "duplicate": bool(payload.get("duplicate", False)),
    }
    if kind == "approval_interrupted":
        return _result(outcome="approval_interrupted", interrupted_by=payload, **common)
    if kind == "run_completed":
        output = runner.output(run)
        if output:
            return _result(outcome="run_completed", output=output, **common)
        return _result(
            outcome="run_failed",
            code="no_output",
            reason="the run completed without an output",
            **common,
        )
    if kind == "run_denied":
        return _result(
            outcome="run_denied",
            code="approval_denied",
            reason=str(payload.get("reason") or "denied"),
            **common,
        )
    if kind == "run_failed":
        return _result(
            outcome="run_failed",
            code="gateway_failed",
            reason=str(payload.get("reason") or "failed"),
            **common,
        )
    return _result(
        outcome="run_failed",
        code="no_outcome",
        reason="the run ended without a terminal outcome",
        **common,
    )


def _result(
    *,
    outcome: str,
    run: PilotRun | None = None,
    run_id: str | None = None,
    task_id: int | None = None,
    code: str | None = None,
    reason: str | None = None,
    events: list[PilotEvent] | tuple[PilotEvent, ...] = (),
    interrupted_by: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    resumed: bool = False,
    duplicate: bool = False,
) -> dict[str, Any]:
    recorded = [
        {
            "sequence": index,
            "kind": event.kind,
            "node_key": event.node_key,
            "payload": event.payload,
        }
        for index, event in enumerate(events, start=1)
    ]
    rid = run.run_id if run is not None else run_id
    return {
        "contract_version": CONTRACT_VERSION,
        "graph_name": GRAPH_NAME,
        "graph_version": GRAPH_VERSION,
        "run_id": rid,
        "thread_id": f"ua-lg:{rid}",
        "task_id": run.task_id if run is not None else task_id,
        "trace_id": run.trace_id if run is not None else None,
        "budget_revision": run.budget_revision if run is not None else None,
        "status": _STATUS_BY_OUTCOME[outcome],
        "outcome": outcome,
        "code": code,
        "reason": reason,
        "resumed": resumed,
        "duplicate": duplicate,
        "events_count": len(recorded),
        "events": recorded,
        "interrupted_by": interrupted_by,
        "output": output if outcome == "run_completed" else None,
    }


def _reason(exc: BaseException) -> str:
    return redact_sensitive_content(str(exc) or type(exc).__name__, max_len=200)


def langgraph_checkpoints(
    run_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Inspect durable LangGraph checkpoints in SQLite.

    Args:
        run_id: Optional ULID to filter checkpoints for a specific run.
        limit: Maximum number of checkpoint records to return (default: 20).

    Returns:
        Dict with checkpoint count, database path, and checkpoint metadata list.
    """
    ckpt_path = default_checkpoint_path()
    if not ckpt_path.is_file():
        return {
            "checkpoint_db": str(ckpt_path),
            "exists": False,
            "checkpoints": [],
        }

    limit = max(1, min(limit, 100))
    thread_filter = f"ua-lg:{run_id.strip()}" if run_id and run_id.strip() else None

    conn = sqlite3.connect(ckpt_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type "
            "FROM checkpoints "
        )
        params: list[Any] = []
        if thread_filter:
            query += "WHERE thread_id = ? "
            params.append(thread_filter)
        query += "ORDER BY checkpoint_id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        checkpoints = [
            {
                "thread_id": r["thread_id"],
                "checkpoint_ns": r["checkpoint_ns"],
                "checkpoint_id": r["checkpoint_id"],
                "parent_checkpoint_id": r["parent_checkpoint_id"],
                "type": r["type"],
            }
            for r in rows
        ]
    except Exception as exc:
        raise ToolError(f"Failed to query checkpoints database: {exc}") from exc
    finally:
        conn.close()

    return {
        "checkpoint_db": str(ckpt_path),
        "exists": True,
        "count": len(checkpoints),
        "checkpoints": checkpoints,
    }


def _progress_note_budget_revision(task_id: int) -> str:
    """Read the hub budget configuration again at each authority boundary."""
    from ..langgraph_pilot import _digest_json

    # This graph has one local write and no model/provider spending. Pin the
    # canonical budget configuration, not a constant captured by the wrapper.
    return _digest_json({"task_id": task_id, "limits": config.autonomy_budget_defaults()})


def _workflow_budget_revision(task_id: int) -> str:
    """Pin hub autonomy limits; this graph has two local writes and no model spend."""
    from ..langgraph_pilot import _digest_json

    return _digest_json(
        {
            "task_id": task_id,
            "graph": "governed_workflow",
            "limits": config.autonomy_budget_defaults(),
        }
    )


def langgraph_progress_note(
    task_id: int,
    output_digest: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Append one approved generated progress receipt to the run's own task.

    Supply a lowercase SHA-256 output digest. The task remains doing; no free
    text or other task fields are accepted. Every first write requires a human
    decision in the canonical langgraph_governed_call approval queue.

    To resume after that decision, repeat task_id and output_digest with the
    returned run_id. The original checkpoint authority is retained and checked;
    resume never refreshes a stale grant. Unknown outcomes are reconciled from
    the native receipt or quarantined, never automatically retried.
    """
    from ..langgraph_note import ProgressNoteRunner
    from ..langgraph_note_dispatch import ProgressNoteDispatchAdapter

    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ToolError("task_id must be a positive integer")
    if not isinstance(output_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", output_digest):
        raise ToolError("output_digest must be a lowercase SHA-256 digest")
    if run_id is not None and (
        not isinstance(run_id, str) or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", run_id)
    ):
        raise ToolError("run_id must be a 26-character Crockford ULID")
    if not (
        config.enforce_auth()
        and config.verified_agent_identity()
        and profiles._current_session_id()
    ):
        raise ToolError("progress notes require an authenticated HTTP hub session")
    if not profiles.tool_is_allowed("task_update"):
        raise ToolError("task_update is not exposed by the active profile")

    checkpoint_path = default_checkpoint_path()
    if run_id is not None and not checkpoint_path.is_file():
        raise ToolError("cannot resume a progress note without a checkpoint")
    adapter = ProgressNoteDispatchAdapter(budget_revision=_progress_note_budget_revision)
    try:
        with ProgressNoteRunner(checkpoint_path, gateway=adapter) as runner:
            if run_id is not None:
                run = runner.load_run(run_id)
                if run.task_id != task_id or run.query != output_digest:
                    raise ToolError("resume conflicts with the original task or output digest")
            else:
                conn = storage.connect()
                try:
                    task = conn.execute(
                        "SELECT status, updated_at, claimed_by, lock_expires_at "
                        "FROM tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if task is None:
                    raise ToolError(f"task #{task_id} not found")
                if task["status"] != "doing" or not task["claimed_by"]:
                    raise ToolError("progress notes require a claimed task in doing status")
                new_run_id = events.new_ulid()
                run = PilotRun(
                    run_id=new_run_id,
                    task_id=task_id,
                    task_updated_at=task["updated_at"],
                    claimed_by=task["claimed_by"],
                    claim_expires_at=task["lock_expires_at"] or "",
                    expected_profile=profiles.current(),
                    required_capabilities=("task_update",),
                    budget_revision=_progress_note_budget_revision(task_id),
                    trace_id=f"trace-lg-{new_run_id}",
                    query=output_digest,
                )
            return runner.execute(run, resume=run_id is not None)
    except PilotError as exc:
        raise ToolError(f"LangGraph progress note halted: {exc}") from exc


def langgraph_workflow_run(
    task_id: int,
    query: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the governed plan→build→verify→merge chain for a claimed task.

    Plan and verify are attributed local hops. Build and merge each append one
    generated receipt through wrapped ``task_update``. Merge is the second
    mutating node and always waits on ``langgraph_governed_call``. This tool
    does not call ``git_merge_task`` and does not dispatch agents.

    Resume after a gate by repeating ``task_id`` and ``query`` with the returned
    ``run_id``. Original origin authority is retained; the build hop's own task
    revision is adopted only after that receipt is confirmed.
    """
    from ..langgraph_workflow import GRAPH_NAME, GRAPH_VERSION, WorkflowRunner
    from ..langgraph_workflow_dispatch import WorkflowDispatchAdapter

    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ToolError("task_id must be a positive integer")
    clean_query = (query or "").strip()
    if not clean_query:
        raise ToolError("query must not be empty")
    if run_id is not None and (
        not isinstance(run_id, str) or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", run_id)
    ):
        raise ToolError("run_id must be a 26-character Crockford ULID")
    if not (
        config.enforce_auth()
        and config.verified_agent_identity()
        and profiles._current_session_id()
    ):
        raise ToolError("governed workflow requires an authenticated HTTP hub session")
    if not profiles.tool_is_allowed("task_update"):
        raise ToolError("task_update is not exposed by the active profile")

    checkpoint_path = default_checkpoint_path()
    if run_id is not None and not checkpoint_path.is_file():
        raise ToolError("cannot resume a workflow without a checkpoint")
    adapter = WorkflowDispatchAdapter(budget_revision=_workflow_budget_revision)
    try:
        from ..langgraph_workflow import PLAN_STEP_INDEX
        from ..mcp_progress import report_progress

        report_progress(0, 4, "starting governed workflow")
        with WorkflowRunner(checkpoint_path, gateway=adapter) as runner:
            if run_id is not None:
                loaded = runner.load_run(run_id)
                if loaded.task_id != task_id or loaded.query != clean_query:
                    raise ToolError("resume conflicts with the original task or query")
                try:
                    result = runner.execute(loaded, resume=True)
                except StaleAuthorityError as exc:
                    # Task #1497: a refused resume must not leave the bound plan
                    # 'active' forever. Close it only if origin authority is gone.
                    closed = None
                    try:
                        closed = runner.close_if_origin_expired(run_id)
                    except Exception as close_exc:  # noqa: BLE001 - never mask the refusal
                        logger.warning("could not close plan for run %s: %s", run_id, close_exc)
                    suffix = f"; bound plan {closed['plan_id']} closed" if closed else ""
                    raise ToolError(f"LangGraph workflow halted: {exc}{suffix}") from exc
            else:
                conn = storage.connect()
                try:
                    task = conn.execute(
                        "SELECT status, updated_at, claimed_by, lock_expires_at "
                        "FROM tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if task is None:
                    raise ToolError(f"task #{task_id} not found")
                if task["status"] != "doing" or not task["claimed_by"]:
                    raise ToolError("governed workflow requires a claimed task in doing status")
                new_run_id = events.new_ulid()
                run = PilotRun(
                    run_id=new_run_id,
                    task_id=task_id,
                    task_updated_at=task["updated_at"],
                    claimed_by=task["claimed_by"],
                    claim_expires_at=task["lock_expires_at"] or "",
                    expected_profile=profiles.current(),
                    required_capabilities=("task_update",),
                    budget_revision=_workflow_budget_revision(task_id),
                    trace_id=f"trace-lg-{new_run_id}",
                    query=clean_query,
                )
                result = runner.execute(run, resume=False)
            result["contract_version"] = CONTRACT_VERSION
            result["graph_name"] = GRAPH_NAME
            result["graph_version"] = GRAPH_VERSION
            pending = result.get("pending_hop")
            status = result.get("status")
            if status in {"completed", "denied"}:
                report_progress(4, 4, str(status))
            elif pending in PLAN_STEP_INDEX:
                report_progress(PLAN_STEP_INDEX[pending], 4, f"pending {pending}")
            else:
                report_progress(4, 4, str(status or "returned"))
            return result
    except PilotError as exc:
        raise ToolError(f"LangGraph workflow halted: {exc}") from exc
