"""Checkpoint, restart, and attribution tests for the governed workflow graph."""

from dataclasses import replace

import pytest

pytest.importorskip("langgraph")
from unified_agent.langgraph_pilot import (  # noqa: E402
    GovernedToolOutcome,
    InvalidRunError,
    PilotError,
    PilotRun,
    StaleAuthorityError,
)
from unified_agent.langgraph_workflow import WorkflowRunner  # noqa: E402
from unified_agent.tools.plan import plan_get  # noqa: E402

pytestmark = pytest.mark.usefixtures("isolated_env")


@pytest.fixture
def run():
    return PilotRun(
        run_id="01JQ9Z6Y5K7M2N4P6R8T0V2X4Z",
        task_id=777,
        task_updated_at="2026-09-06T01:00:00Z",
        claimed_by="Desktop_Agent4",
        claim_expires_at="2026-09-06T23:00:00Z",
        expected_profile="developer",
        required_capabilities=("task_update",),
        budget_revision="v1",
        trace_id="trace-777",
        query="plan then merge",
    )


class Gateway:
    build_status = "pending"
    merge_status = "pending"
    calls: list[str]
    checks = 0
    stale = False
    refreshed_at = "2026-09-06T12:00:00Z"

    def __init__(self) -> None:
        self.calls = []

    def revalidate(self, run):
        from langsmith.run_helpers import get_tracing_context

        assert get_tracing_context()["enabled"] is False
        self.checks += 1
        if self.stale:
            raise StaleAuthorityError("stale")

    def reconcile_hop(self, *, run, hop, digest):
        return None

    def refresh_after_hop(self, run, hop, digest):
        assert hop == "build"
        return replace(run, task_updated_at=self.refreshed_at)

    def call_hop(self, *, run, hop, digest, timeout_s):
        self.calls.append(hop)
        assert timeout_s == 20
        assert len(digest) == 64
        status = self.build_status if hop == "build" else self.merge_status
        if status == "pending":
            return GovernedToolOutcome.pending(f"approval-{hop}")
        if status == "denied":
            return GovernedToolOutcome.denied(f"denied-{hop}")
        return GovernedToolOutcome.allowed(
            {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "claimed_by": run.claimed_by,
                "hop": hop,
                "digest": digest,
                "note_appended": True,
            }
        )


def test_two_gates_then_completed_with_attribution(tmp_path, run):
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    with WorkflowRunner(path, gateway) as runner:
        first = runner.execute(run)
        assert first["status"] == "interrupted"
        assert first["pending_hop"] == "build"
        assert first["interrupted_by"]["claimed_by"] == "Desktop_Agent4"
        assert first["interrupted_by"]["hop"] == "build"
        plan_id = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
            "plan_id"
        ]
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert steps[0]["status"] == "done"
        assert steps[1]["status"] == "doing"
        assert steps[1]["owner"] == "Desktop_Agent4"
        assert runner.execute(run, resume=True)["status"] == "interrupted"
    with WorkflowRunner(path, gateway) as runner:
        assert runner.load_run(run.run_id) == run
        gateway.build_status = "allowed"
        second = runner.execute(run, resume=True)
        assert second["status"] == "interrupted"
        assert second["pending_hop"] == "merge"
        assert second["interrupted_by"]["hop"] == "merge"
        assert second["interrupted_by"]["claimed_by"] == "Desktop_Agent4"
        gateway.merge_status = "allowed"
        result = runner.execute(run, resume=True)
        assert result["status"] == "completed"
        assert result["claimed_by"] == "Desktop_Agent4"
        assert result["output"]["build"]["hop"] == "build"
        assert result["output"]["merge"]["hop"] == "merge"
        assert result["output"]["plan_digest"]
        assert result["output"]["verify_digest"]
        assert gateway.calls == ["build", "build", "build", "merge", "merge"]
        plan_id = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
            "plan_id"
        ]
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert [steps[i]["status"] for i in range(4)] == ["done", "done", "done", "done"]
        assert steps[1]["outputs"]["build"]["hop"] == "build"
        assert steps[3]["outputs"]["merge"]["hop"] == "merge"


def test_merge_denial_keeps_build_and_is_terminal(tmp_path, run):
    gateway = Gateway()
    with WorkflowRunner(tmp_path / "workflow.db", gateway) as runner:
        assert runner.execute(run)["pending_hop"] == "build"
        gateway.build_status = "allowed"
        second = runner.execute(run, resume=True)
        assert second["status"] == "interrupted"
        assert second["pending_hop"] == "merge"
        gateway.merge_status = "denied"
        denied = runner.execute(run, resume=True)
        assert denied["status"] == "denied"
        assert denied["reason"] == "denied-merge"
        gateway.merge_status = "allowed"
        assert runner.execute(run, resume=True)["status"] == "denied"


def test_build_denial_never_reaches_merge(tmp_path, run):
    gateway = Gateway()
    gateway.build_status = "denied"
    with WorkflowRunner(tmp_path / "workflow.db", gateway) as runner:
        runner.execute(run)
        denied = runner.execute(run, resume=True)
        assert denied["status"] == "denied"
        assert "merge" not in gateway.calls


def test_stale_resume_after_build_blocks_merge_effect(tmp_path, run):
    gateway = Gateway()
    with WorkflowRunner(tmp_path / "workflow.db", gateway) as runner:
        runner.execute(run)
        gateway.build_status = "allowed"
        runner.execute(run, resume=True)
        gateway.stale = True
        with pytest.raises(StaleAuthorityError):
            runner.execute(run, resume=True)
        assert gateway.calls.count("merge") == 1


def test_immutable_query_rejected(tmp_path, run):
    gateway = Gateway()
    with WorkflowRunner(tmp_path / "workflow.db", gateway) as runner:
        runner.execute(run)
        with pytest.raises(InvalidRunError):
            runner.execute(replace(run, query="other plan"), resume=True)


def test_definition_digest_pins_merge_node(tmp_path, monkeypatch):
    with WorkflowRunner(tmp_path / "first.db", Gateway()) as runner:
        original = runner.definition_digest

    def changed_merge(self, state):
        return {"status": "denied"}

    monkeypatch.setattr(WorkflowRunner, "_merge_node", changed_merge)
    with WorkflowRunner(tmp_path / "second.db", Gateway()) as runner:
        assert runner.definition_digest != original


def test_bad_build_receipt_is_rejected(tmp_path, run):
    gateway = Gateway()
    gateway.build_status = "allowed"

    def bad_allowed(*, run, hop, digest, timeout_s):
        return GovernedToolOutcome.allowed({"notes": "private task body"})

    gateway.call_hop = bad_allowed  # type: ignore[method-assign]
    with WorkflowRunner(tmp_path / "workflow.db", gateway) as runner:
        with pytest.raises(PilotError, match="effect receipt"):
            runner.execute(run)


def test_merge_crash_window_recovers_plan_handoff(tmp_path, run):
    """Merge effect recorded, process dies before plan_handoff: recover finishes the step."""
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    with WorkflowRunner(path, gateway) as runner:
        runner.execute(run)
        gateway.build_status = "allowed"
        pending = runner.execute(run, resume=True)
        assert pending["pending_hop"] == "merge"
        plan_id = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
            "plan_id"
        ]
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert steps[3]["status"] == "doing"
        assert not (steps[3].get("outputs") or {}).get("merge")

    def historical(*, run, hop, digest):
        assert hop == "merge"
        return GovernedToolOutcome.allowed(
            {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "claimed_by": run.claimed_by,
                "hop": hop,
                "digest": digest,
                "note_appended": True,
            }
        )

    gateway.reconcile_hop = historical  # type: ignore[method-assign]
    with WorkflowRunner(path, gateway) as runner:
        recovered = runner.execute(run)
        assert recovered["status"] == "completed"
        assert recovered["output"]["merge"]["hop"] == "merge"
        plan_id = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
            "plan_id"
        ]
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert steps[3]["status"] == "done"
        assert steps[3]["outputs"]["merge"]["hop"] == "merge"


def test_checkpoint_ahead_does_not_complete_without_plan_handoff(tmp_path, run):
    """Graph scratch may say completed; plan merge still doing is authority."""
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    with WorkflowRunner(path, gateway) as runner:
        runner.execute(run)
        gateway.build_status = "allowed"
        pending = runner.execute(run, resume=True)
        assert pending["pending_hop"] == "merge"
        cfg = {"configurable": {"thread_id": run.thread_id}}
        runner._graph.update_state(
            cfg,
            {
                "status": "completed",
                "merge_output": {
                    "task_id": run.task_id,
                    "run_id": run.run_id,
                    "claimed_by": run.claimed_by,
                    "hop": "merge",
                    "digest": "deadbeef",
                    "note_appended": True,
                },
            },
            as_node="merge",
        )
        plan_id = runner._graph.get_state(cfg).values["plan_id"]
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert steps[3]["status"] == "doing"
        still = runner.execute(run)
        assert still["status"] != "completed"
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert steps[3]["status"] == "doing"


def test_crashed_checkpointer_rebuilds_from_plan(tmp_path, run):
    """Lost LangGraph sqlite still resumes from plan_get via the run goal."""
    lost = tmp_path / "lost.db"
    rebuilt = tmp_path / "rebuilt.db"
    gateway = Gateway()
    with WorkflowRunner(lost, gateway) as runner:
        runner.execute(run)
        gateway.build_status = "allowed"
        pending = runner.execute(run, resume=True)
        assert pending["pending_hop"] == "merge"
        plan_id = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
            "plan_id"
        ]
    with WorkflowRunner(rebuilt, gateway) as runner:
        resumed = runner.execute(run)
        assert resumed["status"] == "interrupted"
        assert resumed["pending_hop"] == "merge"
        assert (
            runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values[
                "plan_id"
            ]
            == plan_id
        )
        gateway.merge_status = "allowed"
        done = runner.execute(run, resume=True)
        assert done["status"] == "completed"
        assert done["output"]["build"]["hop"] == "build"
        assert done["output"]["merge"]["hop"] == "merge"
        steps = {row["step_idx"]: row for row in plan_get(plan_id)["steps"]}
        assert [steps[i]["status"] for i in range(4)] == ["done", "done", "done", "done"]


# --- task #1497: close the bound plan when origin authority expired -----------


def _task_row(task_id: int, status: str, claimed_by: str | None, lock: str | None) -> None:
    from unified_agent import storage

    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "claimed_by, lock_expires_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "claimed_by=excluded.claimed_by, lock_expires_at=excluded.lock_expires_at",
            (task_id, "origin", status, "med", now, now, claimed_by, lock),
        )
        conn.commit()
    finally:
        conn.close()


def _plan_id(runner, run):
    return runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values["plan_id"]


def test_expired_origin_closes_bound_plan_and_ends_run_denied(tmp_path, run):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        assert runner.execute(run)["pending_hop"] == "build"
        plan_id = _plan_id(runner, run)
    _task_row(run.task_id, "done", run.claimed_by, "2026-09-06T23:00:00Z")

    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    swept = close_expired_workflow_plans(path)
    assert swept["checked"] == 1 and swept["left_alone"] == 0
    [closed] = swept["closed"]
    assert closed["plan_id"] == plan_id and closed["plan_closed"] is True
    assert "origin authority expired: origin task is done" in closed["reason"]
    assert run.run_id in closed["reason"]

    loaded = plan_get(plan_id)
    assert loaded["plan"]["status"] == "abandoned"
    steps = {row["step_idx"]: row for row in loaded["steps"]}
    assert steps[1]["status"] == "doing"  # history kept, not rewritten
    assert loaded["checkpoints"][-1]["context"]["outputs"]["run_id"] == run.run_id

    with WorkflowRunner(path, Gateway()) as runner:
        state = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values
        assert state["status"] == "denied"
        result = runner.execute(run, resume=True)
        assert result["status"] == "denied"
        assert result["output"] is None
        assert "origin authority expired" in result["reason"]

    again = close_expired_workflow_plans(path)
    assert again == {"checked": 0, "closed": [], "left_alone": 0, "unverifiable": []}


@pytest.mark.parametrize(
    ("status", "owner", "lock", "cause"),
    [
        ("doing", "Desktop_Agent4", "2000-01-01T00:00:00+00:00", "origin claim lease expired"),
        ("doing", "server_agent2", "2999-01-01T00:00:00+00:00", "origin task owner changed"),
        ("open", None, None, "origin task is open"),
    ],
)
def test_each_lapse_cause_is_recorded(tmp_path, run, status, owner, lock, cause):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
    _task_row(run.task_id, status, owner, lock)
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    [closed] = close_expired_workflow_plans(path)["closed"]
    assert cause in closed["reason"]


def test_missing_origin_task_counts_as_expired(tmp_path, run):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    [closed] = close_expired_workflow_plans(path)["closed"]
    assert "origin task no longer exists" in closed["reason"]


def test_live_authority_is_never_touched_even_after_renewal(tmp_path, run):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
        plan_id = _plan_id(runner, run)
    # Same claimant, renewed live lease: revalidate would call it stale, but the
    # sweep must not close a run whose owner still holds the task.
    _task_row(run.task_id, "doing", run.claimed_by, "2999-01-01T00:00:00+00:00")
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    swept = close_expired_workflow_plans(path)
    assert swept["closed"] == [] and swept["left_alone"] == 1
    assert plan_get(plan_id)["plan"]["status"] == "active"


def test_completed_run_is_left_alone(tmp_path, run):
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    gateway.build_status = gateway.merge_status = "allowed"
    with WorkflowRunner(path, gateway) as runner:
        assert runner.execute(run)["status"] == "completed"
        assert runner.close_if_origin_expired(run.run_id) is None


def test_unloadable_run_is_unverifiable_not_closed(tmp_path, run):
    from unified_agent.langgraph_workflow import close_expired_workflow_plans
    from unified_agent.tools.plan import plan_create

    orphan = plan_create(goal="governed workflow 01JQ9Z6Y5K7M2N4P6R8T0V2X5A", steps=["plan"])
    with WorkflowRunner(tmp_path / "workflow.db", Gateway()):
        pass
    swept = close_expired_workflow_plans(tmp_path / "workflow.db")
    assert swept["closed"] == []
    assert swept["unverifiable"][0]["plan_id"] == orphan["plan_id"]
    assert plan_get(orphan["plan_id"])["plan"]["status"] == "active"
    no_db = close_expired_workflow_plans(tmp_path / "absent.db")
    assert no_db["unverifiable"][0]["error"] == "no checkpoint database"


# --- Devin review on PR #816 ---------------------------------------------------


def test_pre_deploy_checkpoint_with_old_definition_digest_still_closes(tmp_path, run):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
        plan_id = _plan_id(runner, run)
        runner._graph.update_state(
            {"configurable": {"thread_id": run.thread_id}},
            {"definition_digest": "digest-from-an-older-deploy"},
            as_node="dispatch",
        )
        with pytest.raises(PilotError):
            runner.load_run(run.run_id)
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    [closed] = close_expired_workflow_plans(path)["closed"]
    assert closed["plan_id"] == plan_id
    assert plan_get(plan_id)["plan"]["status"] == "abandoned"


def _at_merge_gate(tmp_path, run):
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    gateway.build_status = "allowed"
    with WorkflowRunner(path, gateway) as runner:
        runner.execute(run)
        pending = runner.execute(run, resume=True)
        assert pending["pending_hop"] == "merge"
        return path, _plan_id(runner, run)


def test_run_waiting_at_the_merge_gate_is_closed_when_origin_expired(tmp_path, run):
    path, plan_id = _at_merge_gate(tmp_path, run)
    _task_row(run.task_id, "done", run.claimed_by, "2026-09-06T23:00:00Z")
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    [closed] = close_expired_workflow_plans(path)["closed"]
    assert closed["plan_id"] == plan_id
    assert plan_get(plan_id)["plan"]["status"] == "abandoned"


@pytest.mark.parametrize("effect_state", ["prepared", "executing", "committed", "garbage"])
def test_started_merge_effect_is_never_closed(tmp_path, run, effect_state):
    import json

    from unified_agent import storage
    from unified_agent.langgraph_dispatch import _EFFECT_TOOL
    from unified_agent.langgraph_workflow import close_expired_workflow_plans
    from unified_agent.langgraph_workflow_dispatch import WorkflowDispatchAdapter

    path, plan_id = _at_merge_gate(tmp_path, run)
    body = "{not json" if effect_state == "garbage" else json.dumps({"state": effect_state})
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?,?,?,?)",
            (WorkflowDispatchAdapter._key(run, "merge"), _EFFECT_TOOL, body, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    swept = close_expired_workflow_plans(path)
    assert swept["closed"] == []
    assert "merge outcome is uncertain" in swept["unverifiable"][0]["error"]
    assert plan_get(plan_id)["plan"]["status"] == "active"


def test_duplicate_goal_plan_not_bound_to_the_checkpoint_is_unverifiable(tmp_path, run):
    from unified_agent.langgraph_workflow import close_expired_workflow_plans
    from unified_agent.tools.plan import plan_create

    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
        bound = _plan_id(runner, run)
    twin = plan_create(goal=f"governed workflow {run.run_id}", steps=["plan"])["plan_id"]
    swept = close_expired_workflow_plans(path)
    assert [c["plan_id"] for c in swept["closed"]] == [bound]
    assert [u["plan_id"] for u in swept["unverifiable"]] == [twin]
    assert "not the plan bound" in swept["unverifiable"][0]["error"]
    assert swept["left_alone"] == 0


def test_completed_checkpoint_with_active_plan_is_reported_not_left_silent(tmp_path, run):
    path = tmp_path / "workflow.db"
    gateway = Gateway()
    gateway.build_status = "allowed"
    with WorkflowRunner(path, gateway) as runner:
        runner.execute(run)
        runner.execute(run, resume=True)
        runner._graph.update_state(
            {"configurable": {"thread_id": run.thread_id}},
            {"status": "completed", "merge_output": {"hop": "merge"}},
            as_node="merge",
        )
        plan_id = _plan_id(runner, run)
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    swept = close_expired_workflow_plans(path)
    assert swept["closed"] == [] and swept["left_alone"] == 0
    assert "reconcile the merge handoff" in swept["unverifiable"][0]["error"]
    assert plan_get(plan_id)["plan"]["status"] == "active"


def test_denied_run_with_active_plan_gets_its_plan_closed(tmp_path, run):
    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        runner.execute(run)
        runner._graph.update_state(
            {"configurable": {"thread_id": run.thread_id}},
            {"status": "denied", "denial_reason": "denied-build"},
            as_node="dispatch",
        )
        plan_id = _plan_id(runner, run)
    _task_row(run.task_id, "doing", run.claimed_by, "2999-01-01T00:00:00+00:00")
    from unified_agent.langgraph_workflow import close_expired_workflow_plans

    [closed] = close_expired_workflow_plans(path)["closed"]
    assert closed["reason"].startswith("workflow denied: denied-build")
    assert plan_get(plan_id)["plan"]["status"] == "abandoned"


def test_review_scan_surfaces_the_workflow_sweep(monkeypatch, capsys):
    from unified_agent import queue_hygiene, review_cli

    monkeypatch.setattr(
        queue_hygiene,
        "run_hygiene_pass",
        lambda **_: {
            "released": [],
            "demoted": [],
            "archived": [],
            "workflow_plans": {"error": "RuntimeError: boom"},
        },
    )
    monkeypatch.setattr(review_cli.reviewer, "reconcile_tasks", lambda **_: {"results": []})
    monkeypatch.setattr(
        review_cli.reviewer, "scan_tasks", lambda **_: {"results": []}, raising=False
    )
    try:
        review_cli.main(["scan", "--dry-run", "--no-notify"])
    except SystemExit:
        pass
    assert "workflow plan sweep failed: RuntimeError: boom" in capsys.readouterr().err


def test_workflow_plan_metadata_cannot_be_edited_and_the_sweep_still_closes_it(tmp_path, run):
    """Review #1516: the binding is the exact goal text; plan_edit must not change it."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.langgraph_workflow import close_expired_workflow_plans
    from unified_agent.tools.plan import plan_manage

    path = tmp_path / "workflow.db"
    with WorkflowRunner(path, Gateway()) as runner:
        assert runner.execute(run)["pending_hop"] == "build"
        plan_id = _plan_id(runner, run)
    for action, params in (
        ("edit", {"description": "why"}),
        ("edit", {"title": "renamed"}),
        # Review round 2: the runner reads its stages by fixed index, so removing the
        # open merge step or reordering would strand the run.
        ("step_remove", {"step_idx": 3}),
        ("step_reorder", {"order": [0, 1, 3, 2]}),
        ("step_add", {"description": "extra"}),
        ("step_edit", {"step_idx": 3, "description": "merge differently"}),
    ):
        with pytest.raises(ToolError, match="workflow-bound"):
            plan_manage(action, {"plan_id": plan_id, **params})
    assert len(plan_get(plan_id)["steps"]) == 4
    assert plan_get(plan_id)["plan"]["goal"] == f"governed workflow {run.run_id}"
    _task_row(run.task_id, "done", run.claimed_by, "2026-09-06T23:00:00Z")
    [closed] = close_expired_workflow_plans(path)["closed"]
    assert closed["plan_id"] == plan_id and closed["plan_closed"] is True
