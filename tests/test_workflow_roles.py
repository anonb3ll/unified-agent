"""Durable role workflows use the governed tools and the existing task queue."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, edition, storage, tool_surface
from unified_agent.tools import tasks, workflow

pytestmark = pytest.mark.smoke


@pytest.fixture
def role_workflow(tmp_path, monkeypatch):
    directory = tmp_path / "workflows"
    directory.mkdir()
    spec = {
        "name": "roles",
        "inputs": ["message"],
        "roles": {
            "builder": {"preferred_seats": ["Desktop_Agent2"], "skills": ["build"]},
            "reviewer": {"preferred_seats": ["Desktop_Agent1"]},
        },
        "steps": [
            {
                "name": "build",
                "actor_role": "builder",
                "objective": "Build the item",
                "tool": "run_cmd_check",
                "args": {"command": "{{ inputs.message }}"},
                "allowed_exits": ["handoff", "blocked", "waiting", "failed"],
                "next_hop": {"require": ["reviewer"]},
            },
            {
                "name": "review",
                "actor_role": "reviewer",
                "objective": "Review the item",
                "tool": "run_cmd_check",
                "args": {"command": "{{ steps.build.command }}"},
                "allowed_exits": ["done", "blocked", "failed"],
            },
        ],
    }
    (directory / "roles.yaml").write_text(json.dumps(spec))
    monkeypatch.setattr(workflow, "_workflows_dir", lambda: directory)
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2:Desktop_Agent1")
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *a, **kw: None)
    calls = []
    registry = tool_surface.registry()
    monkeypatch.setitem(registry, "task_add", tasks.task_add)
    monkeypatch.setitem(registry, "task_update", tasks.task_update)

    def execute(command):
        calls.append(command)
        return {"command": command, "ok": True}

    monkeypatch.setitem(registry, "run_cmd_check", execute)
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        yield directory, spec, calls
    finally:
        config.agent_identity_var.reset(token)


def start():
    return workflow.workflow_run("roles", inputs={"message": "echo build"})


def claim(frontier):
    return tasks.task_claim(
        frontier["task_id"],
        config.agent_identity_var.get(),
        expected_lane=config.agent_identity_var.get(),
    )


def close(instance, task, exit="handoff", reason="handed_off_to", target="reviewer"):
    return workflow.workflow_run(
        "roles",
        instance_id=instance,
        closure={
            "exit": exit,
            "reason": reason,
            "target": target,
            "task_id": task["id"],
            "expected_updated_at": task["updated_at"],
        },
    )


def test_two_roles_resume_and_data_passing(role_workflow):
    first = start()
    assert first["status"] == "waiting"
    assert first["frontier"]["actor_role"] == "builder"
    assert role_workflow[2] == []
    build = claim(first["frontier"])
    next_state = close(first["instance_id"], build)
    assert next_state["frontier"]["actor_role"] == "reviewer"
    assert tasks.task_list(status="done")["tasks"][0]["id"] == build["id"]
    # A new connection / invocation reads the persisted instance, not process state.
    resumed = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert resumed["frontier"] == next_state["frontier"]
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        review = claim(resumed["frontier"])
        done = close(first["instance_id"], review, "done", "no_follow_on", None)
    finally:
        config.agent_identity_var.reset(token)
    assert done["status"] == "done"
    assert done["frontier"] is None
    assert role_workflow[2] == ["echo build", "echo build"]
    assert len(done["trail"]) == 2


@pytest.mark.parametrize(
    "closure",
    [
        {},
        {"exit": "handoff"},
        {"exit": "handoff", "reason": "no_follow_on"},
        {"exit": "done", "reason": "no_follow_on"},
        {"exit": "handoff", "reason": "handed_off_to", "target": "builder"},
        {"exit": "blocked", "reason": "blocked_on"},
    ],
)
def test_invalid_closure_before_effect(role_workflow, closure):
    first = start()
    task = claim(first["frontier"])
    closure.update(task_id=task["id"], expected_updated_at=task["updated_at"])
    with pytest.raises(ToolError):
        workflow.workflow_run("roles", instance_id=first["instance_id"], closure=closure)
    assert role_workflow[2] == []
    assert tasks.task_list(status="doing")["tasks"][0]["id"] == task["id"]


def test_role_cannot_be_impersonated(role_workflow):
    first = start()
    task = claim(first["frontier"])
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="role|owner"):
            close(first["instance_id"], task)
    finally:
        config.agent_identity_var.reset(token)
    assert role_workflow[2] == []


def test_terminal_task_update_requires_closure(role_workflow):
    first = start()
    task = claim(first["frontier"])
    with pytest.raises(ToolError, match="closure"):
        tasks.task_update(task["id"], status="done", worktree=False)
    assert tasks.task_list(status="doing")["tasks"][0]["id"] == task["id"]


def test_handoff_failure_rolls_back_and_retry_does_not_repeat_tool(role_workflow, monkeypatch):
    from unified_agent import workflow_roles

    first = start()
    task = claim(first["frontier"])
    real = workflow_roles._insert_successor
    monkeypatch.setattr(
        workflow_roles,
        "_insert_successor",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mid-handoff")),
    )
    with pytest.raises(RuntimeError, match="mid-handoff"):
        close(first["instance_id"], task)
    assert tasks.task_list(status="doing")["tasks"][0]["id"] == task["id"]
    assert len(tasks.task_list(status="all")["tasks"]) == 1
    monkeypatch.setattr(workflow_roles, "_insert_successor", real)
    assert close(first["instance_id"], task)["frontier"]["actor_role"] == "reviewer"
    assert role_workflow[2] == ["echo build"]


def test_stale_closure_and_definition_drift(role_workflow):
    first = start()
    task = claim(first["frontier"])
    # Pin the definition for an instance even if the installed YAML changes.
    (role_workflow[0] / "roles.yaml").write_text("not: a workflow")
    changed = tasks.task_update(task["id"], append_note="new revision", worktree=False)
    with pytest.raises(ToolError, match="revision"):
        close(first["instance_id"], task)
    assert close(first["instance_id"], changed)["frontier"]["actor_role"] == "reviewer"


def test_tenant_fence(role_workflow, monkeypatch):
    first = start()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "other-org")
    with pytest.raises(ToolError, match="no workflow instance"):
        workflow.workflow_run("roles", instance_id=first["instance_id"])


@pytest.mark.parametrize(
    "patch",
    [
        {"actor_role": "ghost"},
        {"allowed_exits": ["invented"]},
        {"next_hop": {"require": ["ghost"]}},
        {"objective": ""},
    ],
)
def test_format_refuses_unknown_roles_and_exits(role_workflow, patch):
    spec = role_workflow[1]
    spec["steps"][0].update(patch)
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    with pytest.raises(ToolError):
        start()
    assert tasks.task_list(status="all")["tasks"] == []


def test_interrupted_effect_is_not_repeated(role_workflow):
    first = start()
    task = claim(first["frontier"])
    with storage.connect() as conn:
        conn.execute(
            "UPDATE workflow_instances SET execution='executing' WHERE id=?",
            (first["instance_id"],),
        )
    with pytest.raises(ToolError, match="indeterminate"):
        close(first["instance_id"], task)
    assert role_workflow[2] == []


@pytest.mark.parametrize(
    "exit,reason,target", [("blocked", "blocked_on", "task:12"), ("failed", "denied", None)]
)
def test_interrupted_effect_can_receive_no_effect_disposition(role_workflow, exit, reason, target):
    first = start()
    task = claim(first["frontier"])
    with storage.connect() as conn:
        conn.execute(
            "UPDATE workflow_instances SET execution='executing' WHERE id=?",
            (first["instance_id"],),
        )
    state = close(first["instance_id"], task, exit, reason, target)
    assert state["status"] == exit
    assert state["frontier"]["execution"] == "indeterminate"
    assert role_workflow[2] == []


def test_capability_exception_before_reservation_is_retriable(role_workflow, monkeypatch):
    from unified_agent.tools import capability

    spec = role_workflow[1]
    spec["inputs"].append("task_id")
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    monkeypatch.setattr(capability, "check_agent_capability", lambda *a: {"capable": True})
    first = workflow.workflow_run("roles", inputs={"message": "echo build", "task_id": 42})
    task = claim(first["frontier"])

    def refuse(*args):
        raise ToolError("capability unavailable")

    monkeypatch.setattr(capability, "check_agent_capability", refuse)
    result = close(first["instance_id"], task)
    assert result["step_result"]["status"] == "denied"
    assert result["frontier"]["execution"] == "idle"
    assert role_workflow[2] == []
    monkeypatch.setattr(capability, "check_agent_capability", lambda *a: {"capable": True})
    assert close(first["instance_id"], task)["frontier"]["actor_role"] == "reviewer"


def test_unexpected_execution_exception_writes_indeterminate_receipt(role_workflow, monkeypatch):
    def fail(*args):
        raise RuntimeError("unexpected execution exception")

    monkeypatch.setattr(workflow, "_execute_role_step", fail)
    first = start()
    state = close(first["instance_id"], claim(first["frontier"]))
    assert state["step_result"]["status"] == "indeterminate"
    assert state["frontier"]["execution"] == "indeterminate"
    assert role_workflow[2] == []


def test_operator_can_cancel_unclaimed_frontier_with_trail(role_workflow):
    first = start()
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        tasks.task_update(first["frontier"]["task_id"], status="cancelled", worktree=False)
    finally:
        config.agent_identity_var.reset(token)
    state = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert state["status"] == "failed"
    assert state["trail"][0]["actor"] == "Desktop_Person"
    assert state["trail"][0]["reason"] == "cancelled"
    assert role_workflow[2] == []


def test_cancellation_during_effect_cannot_be_overwritten(role_workflow, monkeypatch):
    first = start()
    task = claim(first["frontier"])

    def cancel_during_effect(command):
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            tasks.task_update(task["id"], status="cancelled", worktree=False)
        finally:
            config.agent_identity_var.reset(token)
        return {"command": command}

    monkeypatch.setitem(tool_surface.registry(), "run_cmd_check", cancel_during_effect)
    state = close(first["instance_id"], task)
    assert state["status"] == "failed"
    assert state["frontier"]["execution"] == "indeterminate"
    assert state["trail"][0]["reason"] == "cancelled"
    assert state["step_result"]["status"] == "indeterminate"
    assert state["outputs"] == {}
    assert len(tasks.task_list(status="all")["tasks"]) == 1


@pytest.mark.parametrize("acknowledge", [True, False])
def test_owner_can_finish_requested_cancellation(role_workflow, acknowledge):
    first = start()
    task = claim(first["frontier"])
    requested = tasks.task_update(task["id"], request_cancel=True, worktree=False)
    if acknowledge:
        tasks.task_update(
            task["id"], status="cancelled", expected_run_id=task["current_run_id"], worktree=False
        )
        state = workflow.workflow_run("roles", instance_id=first["instance_id"])
    else:
        state = close(first["instance_id"], requested, "failed", "cancelled", None)
    assert state["status"] == "failed"
    assert state["trail"][0]["reason"] == "cancelled"
    assert role_workflow[2] == []


def test_retired_seat_can_close_with_cancellation(role_workflow, monkeypatch):
    from unified_agent import workflow_roles

    first = start()
    task = claim(first["frontier"])
    monkeypatch.setattr(workflow_roles, "_candidates", lambda role: [])
    state = close(first["instance_id"], task, "failed", "cancelled", None)
    assert state["status"] == "failed"
    assert role_workflow[2] == []


def test_other_agent_cannot_use_fenced_cancel_ack(role_workflow):
    first = start()
    task = claim(first["frontier"])
    tasks.task_update(task["id"], request_cancel=True, worktree=False)
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="closure|owner|cancel"):
            tasks.task_update(
                task["id"],
                status="cancelled",
                expected_run_id=task["current_run_id"],
                worktree=False,
            )
    finally:
        config.agent_identity_var.reset(token)


def test_wrong_role_claimant_cannot_acknowledge_workflow_cancellation(role_workflow):
    first = start()
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        # A lane fence may select another seat's inventory; it is not role authority.
        task = tasks.task_claim(
            first["frontier"]["task_id"], "Desktop_agent3", expected_lane="Desktop_Agent2"
        )
        tasks.task_update(task["id"], request_cancel=True, worktree=False)
        with pytest.raises(ToolError, match="closure|owner|role"):
            tasks.task_update(
                task["id"],
                status="cancelled",
                expected_run_id=task["current_run_id"],
                worktree=False,
            )
    finally:
        config.agent_identity_var.reset(token)
    state = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert state["status"] == "waiting" and state["trail"] == []
    assert role_workflow[2] == []


def test_operator_bound_role_preserves_explicit_denied_disposition(role_workflow, monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent2")
    first = start()
    task = claim(first["frontier"])
    state = close(first["instance_id"], task, "failed", "denied", None)
    assert state["status"] == "failed"
    assert state["frontier"]["task_status"] == "cancelled"
    assert state["trail"][0]["reason"] == "denied"
    assert state["trail"][0]["actor"] == "Desktop_Agent2"
    assert role_workflow[2] == []


def test_operator_explicit_denial_still_requires_current_revision(role_workflow, monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent2")
    first = start()
    task = claim(first["frontier"])
    changed = tasks.task_update(task["id"], append_note="new revision", worktree=False)
    with pytest.raises(ToolError, match="revision"):
        close(first["instance_id"], task, "failed", "denied", None)
    state = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert state["status"] == "waiting" and state["trail"] == []
    assert (
        close(first["instance_id"], changed, "failed", "denied", None)["trail"][0]["reason"]
        == "denied"
    )
    assert role_workflow[2] == []


def test_cancel_hold_restore_retains_failed_workflow_frontier(role_workflow, monkeypatch):
    from unified_agent import task_holds

    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1")
    first = start()
    task = claim(first["frontier"])
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        task_holds.task_hold(task["id"], mode="cancel", reason="wrong direction")
    finally:
        config.agent_identity_var.reset(token)
    tasks.task_update(
        task["id"],
        status="cancelled",
        release_claim=True,
        expected_run_id=task["current_run_id"],
        worktree=False,
    )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        restored = task_holds.task_hold(task["id"], mode="restore")
    finally:
        config.agent_identity_var.reset(token)
    assert restored["restored"][0]["control"] == "retained"
    assert restored["restored"][0]["detail"] == "workflow_failed"
    state = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert state["status"] == "failed"
    assert state["frontier"]["task_status"] == "cancelled"
    assert len(state["trail"]) == 1 and role_workflow[2] == []


@pytest.mark.parametrize("through_parent", [False, True])
def test_failed_denied_frontier_is_terminal_and_deletable_with_trail(
    role_workflow, monkeypatch, through_parent
):
    from unified_agent import safety

    monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a: None)
    first = start()
    task = claim(first["frontier"])
    target = task["id"]
    if through_parent:
        target = tasks.task_add("Workflow parent")["id"]
        task = tasks.task_update(task["id"], parent_id=target, worktree=False)
    failed = close(first["instance_id"], task, "failed", "denied", None)
    assert failed["status"] == "failed"
    assert failed["frontier"]["task_status"] == "cancelled"
    with pytest.raises(ToolError):
        claim(failed["frontier"])
    assert tasks.task_delete(target)["deleted"] is True
    historical = workflow.workflow_run("roles", instance_id=first["instance_id"])
    assert historical["status"] == "failed" and historical["frontier"] is None
    assert historical["trail"] == failed["trail"]
    assert role_workflow[2] == []


def test_successor_task_contains_instance_reference(role_workflow):
    first = start()
    second = close(first["instance_id"], claim(first["frontier"]))
    row = next(
        t
        for t in tasks.task_list(status="all")["tasks"]
        if t["id"] == second["frontier"]["task_id"]
    )
    assert first["instance_id"] in row["notes"]
    assert "Workflow: roles" in row["notes"]


@pytest.mark.parametrize("through_parent", [False, True])
def test_frontier_task_deletion_is_refused(role_workflow, monkeypatch, through_parent):
    from unified_agent import safety

    monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a: None)
    first = start()
    target = first["frontier"]["task_id"]
    if through_parent:
        target = tasks.task_add("Workflow parent")["id"]
        tasks.task_update(first["frontier"]["task_id"], parent_id=target, worktree=False)
    with pytest.raises(ToolError, match="frontier|workflow"):
        tasks.task_delete(target)
    assert (
        workflow.workflow_run("roles", instance_id=first["instance_id"])["frontier"]["task_id"]
        == first["frontier"]["task_id"]
    )


def test_approval_pending_can_resume_through_same_gate(role_workflow, monkeypatch):
    from unified_agent.tools.approvals import ApprovalPendingError

    first = start()
    task = claim(first["frontier"])
    registry = tool_surface.registry()
    original = registry["run_cmd_check"]
    monkeypatch.setitem(
        registry,
        "run_cmd_check",
        lambda **kw: (_ for _ in ()).throw(ApprovalPendingError("human needed")),
    )
    pending = close(first["instance_id"], task)
    assert pending["step_result"]["status"] == "pending-approval"
    assert len(tasks.task_list(status="all")["tasks"]) == 1
    monkeypatch.setitem(registry, "run_cmd_check", original)
    assert close(first["instance_id"], task)["frontier"]["actor_role"] == "reviewer"


def test_successor_inherits_scope_and_quarantine(role_workflow):
    first = start()
    task = claim(first["frontier"])
    updated = tasks.task_update(task["id"], domain="professional", worktree=False)
    second = close(first["instance_id"], updated)
    successor = next(
        t
        for t in tasks.task_list(status="all")["tasks"]
        if t["id"] == second["frontier"]["task_id"]
    )
    assert successor["domain"] == "professional"
    assert successor["project"] == task["project"]
    assert successor["org_id"] == task["org_id"]
    assert successor["claimed_by"] is None
    assert successor["current_run_id"] is None


def test_blocked_closure_does_not_run_tool(role_workflow):
    first = start()
    task = claim(first["frontier"])
    state = close(first["instance_id"], task, "blocked", "blocked_on", "task:12")
    assert state["status"] == "blocked"
    assert state["trail"][0]["reason"] == "blocked_on"
    assert role_workflow[2] == []


def test_task_creation_and_instance_roll_back_together(role_workflow, monkeypatch):
    from unified_agent import workflow_roles

    real = workflow_roles.on_task_created

    def fail_after_instance(*args):
        real(*args)
        raise RuntimeError("after instance insert")

    monkeypatch.setattr(workflow_roles, "on_task_created", fail_after_instance)
    with pytest.raises(RuntimeError, match="after instance insert"):
        start()
    assert tasks.task_list(status="all")["tasks"] == []


def test_office_and_team_resolve_seats_without_changing_identity(role_workflow, monkeypatch):
    from unified_agent import org_registry, team_spec

    spec = role_workflow[1]
    spec["roles"]["builder"] = {"office": "engineering"}
    spec["roles"]["reviewer"] = {"team": "delivery", "skills": ["review"]}
    monkeypatch.setattr(org_registry, "find_office", lambda _: {"members": ["Desktop_Agent2"]})
    monkeypatch.setattr(
        team_spec,
        "resolve",
        lambda **kw: {
            "skills": [],
            "seats": [
                {"label": "Desktop_Agent2", "skills": ["build"]},
                {"label": "Desktop_Agent1", "skills": ["review"]},
            ],
        },
    )
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    assert start()["frontier"]["agent"] == "Desktop_Agent2"
    assert config.verified_agent_identity() == "Desktop_Agent2"


def test_empty_eligibility_intersection_never_falls_back(role_workflow, monkeypatch):
    from unified_agent import org_registry, team_spec

    spec = role_workflow[1]
    spec["roles"]["builder"] = {
        "preferred_seats": ["Desktop_Agent2"],
        "team": "delivery",
        "office": "engineering",
    }
    monkeypatch.setattr(
        team_spec,
        "resolve",
        lambda **kw: {"skills": [], "seats": [{"label": "Desktop_Agent1", "skills": []}]},
    )
    monkeypatch.setattr(org_registry, "find_office", lambda _: {"members": ["Desktop_Agent2"]})
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    with pytest.raises(ToolError, match="no eligible seats"):
        start()
    assert tasks.task_list(status="all")["tasks"] == []


def test_admission_revoked_during_tool_cannot_handoff(role_workflow, monkeypatch):
    first = start()
    task = claim(first["frontier"])

    def revoke(command):
        tasks.task_update(task["id"], release_claim=True, worktree=False)
        return {"command": command}

    monkeypatch.setitem(tool_surface.registry(), "run_cmd_check", revoke)
    with pytest.raises(ToolError):
        close(first["instance_id"], task)
    assert len(tasks.task_list(status="all")["tasks"]) == 1
    assert (
        workflow.workflow_run("roles", instance_id=first["instance_id"])["frontier"]["execution"]
        == "ready"
    )


def test_sensitive_inputs_refused_before_task_created(role_workflow):
    with pytest.raises(ToolError, match="sensitive"):
        workflow.workflow_run("roles", inputs={"message": "Bearer " + "A" * 48})
    assert tasks.task_list(status="all")["tasks"] == []


def test_direct_completion_cannot_strand_frontier(role_workflow):
    from unified_agent import run_records

    first = start()
    task = claim(first["frontier"])
    expected = run_records.capture_direct_completion(task["id"])
    assert expected is not None
    assert not run_records.complete_direct(expected, actor="reviewer")
    assert tasks.task_list(status="doing")["tasks"][0]["id"] == task["id"]
    assert workflow.workflow_run("roles", instance_id=first["instance_id"])["trail"] == []


def test_kernel_step_read_is_the_one_run_records_uses(role_workflow):
    """#2450: run_records asks the kernel's workflow_steps, not workflow_roles."""
    from unified_agent import run_records, workflow_roles, workflow_steps

    assert workflow_roles.is_workflow_task is workflow_steps.is_workflow_task
    task = claim(start()["frontier"])
    conn = storage.connect()
    try:
        assert workflow_steps.is_workflow_task(conn, task["id"])
    finally:
        conn.close()
    expected = run_records.capture_direct_completion(task["id"])
    assert not run_records.complete_direct(expected, actor="reviewer")


def test_merge_closeout_cannot_complete_workflow_step(role_workflow):
    from unified_agent import goals, merge

    first = start()
    task = claim(first["frontier"])
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        mission = goals.goal_add("Workflow mission", author="Desktop_Person")["goal"]["id"]
        goals.link_task(task["id"], mission, author="Desktop_Person")
    finally:
        config.agent_identity_var.reset(token)
    context = {
        "current_run_id": task["current_run_id"],
        "task_status": task["status"],
        "task_claimant": task["claimed_by"],
    }
    with storage.connect() as conn, pytest.raises(ToolError, match="workflow_run closure"):
        merge._merge_task_fence(conn, task["id"], mission, context)
    assert workflow.workflow_run("roles", instance_id=first["instance_id"])["trail"] == []


def test_failed_disposition_cannot_be_overwritten(role_workflow):
    first = start()
    task = claim(first["frontier"])
    failed = close(first["instance_id"], task, "failed", "denied", None)
    assert failed["status"] == "failed"
    assert failed["frontier"]["task_status"] == "cancelled"
    with pytest.raises(ToolError):
        claim(failed["frontier"])
    with pytest.raises(ToolError, match="complete|failed|terminal"):
        close(first["instance_id"], task)
    assert role_workflow[2] == []


def test_scalar_output_survives_role_handoff(role_workflow, monkeypatch):
    spec = role_workflow[1]
    spec["steps"][1]["args"] = {"command": "{{ steps.build.value }}"}
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    calls = []

    def scalar(command):
        calls.append(command)
        return "built"

    monkeypatch.setitem(tool_surface.registry(), "run_cmd_check", scalar)
    first = start()
    next_state = close(first["instance_id"], claim(first["frontier"]))
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        done = close(
            first["instance_id"], claim(next_state["frontier"]), "done", "no_follow_on", None
        )
    finally:
        config.agent_identity_var.reset(token)
    assert done["status"] == "done"
    assert calls == ["echo build", "built"]


def test_capability_refusal_before_effect_remains_retriable(role_workflow, monkeypatch):
    from unified_agent.tools import capability

    spec = role_workflow[1]
    spec["inputs"].append("task_id")
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    monkeypatch.setattr(capability, "check_agent_capability", lambda *a: {"capable": True})
    first = workflow.workflow_run("roles", inputs={"message": "echo build", "task_id": 42})
    task = claim(first["frontier"])
    monkeypatch.setattr(capability, "check_agent_capability", lambda *a: {"capable": False})
    result = close(first["instance_id"], task)
    assert result["step_result"]["status"] == "denied"
    assert result["frontier"]["execution"] == "idle"
    assert role_workflow[2] == []
    monkeypatch.setattr(capability, "check_agent_capability", lambda *a: {"capable": True})
    assert close(first["instance_id"], task)["frontier"]["actor_role"] == "reviewer"


def test_team_skill_does_not_qualify_every_seat(role_workflow, monkeypatch):
    from unified_agent import team_spec

    spec = role_workflow[1]
    spec["roles"]["builder"] = {"team": "delivery", "skills": ["build"]}
    monkeypatch.setattr(
        team_spec,
        "resolve",
        lambda **kw: {
            "skills": ["build"],
            "seats": [{"label": "Desktop_Agent1", "skills": []}],
        },
    )
    (role_workflow[0] / "roles.yaml").write_text(json.dumps(spec))
    with pytest.raises(ToolError, match="eligible"):
        start()


def test_unbound_agent_cannot_read_workflow_snapshot(role_workflow):
    first = start()
    token = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(ToolError, match="participant|role|access"):
            workflow.workflow_run("roles", instance_id=first["instance_id"])
    finally:
        config.agent_identity_var.reset(token)
    assert (
        workflow.workflow_run("roles", instance_id=first["instance_id"])["instance_id"]
        == first["instance_id"]
    )


def test_secret_result_is_not_returned_or_advanced(role_workflow, monkeypatch):
    first = start()
    task = claim(first["frontier"])
    synthetic_secret = "Bearer " + "Z" * 48
    monkeypatch.setitem(
        tool_surface.registry(), "run_cmd_check", lambda **kw: {"command": synthetic_secret}
    )
    result = close(first["instance_id"], task)
    assert result["step_result"]["status"] == "indeterminate"
    assert synthetic_secret not in json.dumps(result)
    assert result["outputs"] == {}
    assert result["frontier"]["execution"] == "indeterminate"
    with storage.connect() as conn:
        receipt = conn.execute(
            "SELECT receipt FROM workflow_instances WHERE id=?", (first["instance_id"],)
        ).fetchone()[0]
    assert synthetic_secret not in receipt


def test_workflow_schema_available_after_db_init(isolated_env):
    with storage.connect() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"workflow_instances", "workflow_task_steps", "workflow_closures"} <= names


def test_unpersistable_effect_becomes_indeterminate(role_workflow, monkeypatch):
    first = start()
    task = claim(first["frontier"])
    monkeypatch.setitem(tool_surface.registry(), "run_cmd_check", lambda **kw: {"value": object()})
    result = close(first["instance_id"], task)
    assert result["step_result"]["status"] == "indeterminate"
    assert result["frontier"]["execution"] == "indeterminate"
    with pytest.raises(ToolError, match="indeterminate"):
        close(first["instance_id"], task)


def test_completed_step_cannot_reenter_queue(role_workflow):
    first = start()
    task = claim(first["frontier"])
    close(first["instance_id"], task)
    with pytest.raises(ToolError, match="cannot be reopened"):
        tasks.task_update(task["id"], status="open", worktree=False)
