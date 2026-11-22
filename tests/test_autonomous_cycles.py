import sqlite3
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools import cycles
from unified_agent.tools.approvals import ApprovalPendingError, approval_pending, approval_resolve


class _CyclesClock:
    """``cycles.time`` with only ``monotonic`` controlled.

    Replacing ``time.monotonic`` itself would move every other clock reader in
    the process too: the agent-roster cache, the tenant beacon and sqlite all
    read it, so a finite clock is drained before the cycle reads its first
    value (CI shard 2). Everything but ``monotonic`` passes through to ``time``.
    """

    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __getattr__(self, name: str):
        return getattr(time, name)

    def monotonic(self) -> float:
        return next(self._values)


@pytest.fixture(autouse=True)
def _explicit_cloud_configuration(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")


def test_autonomous_cycle_manage_dispatches_list(isolated_env):
    assert cycles.autonomous_cycle_manage("list", {"limit": 10}) == {"cycles": []}


def test_autonomous_cycle_manage_rejects_bad_action_and_params(isolated_env):
    with pytest.raises(ToolError, match="action must be one of"):
        cycles.autonomous_cycle_manage("unknown")
    with pytest.raises(ToolError, match="invalid params"):
        cycles.autonomous_cycle_manage("get", {"unexpected": True})


def _executing_recovery_cycle():
    started = cycles.autonomous_cycle_start(
        "Desktop_Agent1",
        capabilities=["cloud_worker_recover", "gcloud_ops"],
    )
    cycle_id = started["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "recovery-delegate", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(cycle_id, "executing", "recovery-execute", "Desktop_Agent1")
    return cycle_id


def test_recover_worker_is_a_state_bound_cycle_consumer(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    cycle_id = _executing_recovery_cycle()
    seen = []

    def fake_recover(**kwargs):
        kwargs["before_mutation"]()
        seen.append(kwargs["cycle_id"])
        return {"outcome": "recovered", "target": "example-worker"}

    monkeypatch.setattr(cloud_recovery, "_cloud_worker_recover", fake_recover)
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert seen == [cycle_id]
    assert result["transition"]["transition"]["to_state"] == "validating"


def test_recover_worker_uses_real_approval_boundary(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import gcloud

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")
    monkeypatch.setattr(
        gcloud,
        "gcloud_inventory",
        lambda **kwargs: {
            "status": "ok",
            "sections": {"instances": "ok"},
            "instances": [
                {"name": "example-worker", "status": "TERMINATED", "zone": "europe-west1-b"}
            ],
            "errors": [],
        },
    )
    cycle_id = _executing_recovery_cycle()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "awaiting_approval"
    assert approval_pending()["pending"][-1]["tool"] == "gcloud_ops"
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "executing"


def test_recover_worker_real_core_rechecks_killed_cycle_before_mutation(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import gcloud

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")
    cycle_id = _executing_recovery_cycle()
    calls = []

    def inventory_then_kill(**kwargs):
        cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "operator stop")
        return {
            "status": "ok",
            "sections": {"instances": "ok"},
            "instances": [
                {"name": "example-worker", "status": "TERMINATED", "zone": "europe-west1-b"}
            ],
            "errors": [],
        }

    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory_then_kill)
    monkeypatch.setattr(gcloud, "gcloud_ops", lambda **kwargs: calls.append(kwargs))
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "blocked"
    assert calls == []
    assert result["elapsed_s"] >= 0
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "killed"


def test_recover_worker_rejects_unverified_owner_and_nonexecuting_cycle(monkeypatch):
    from unified_agent import config

    cycle_id = cycles.autonomous_cycle_start(
        "Desktop_Agent1", capabilities=["cloud_worker_recover", "gcloud_ops"]
    )["cycle"]["id"]
    with pytest.raises(ToolError, match="verified identity"):
        cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1"}
        )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="executing cycle"):
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1"}
            )
    finally:
        config.agent_identity_var.reset(token)

    owned_by_other = cycles.autonomous_cycle_start(
        "Desktop_agent3", capabilities=["cloud_worker_recover", "gcloud_ops"]
    )["cycle"]["id"]
    cycles.autonomous_cycle_transition(
        owned_by_other, "delegated", "other-delegate", "Desktop_agent3"
    )
    cycles.autonomous_cycle_transition(
        owned_by_other, "executing", "other-execute", "Desktop_agent3"
    )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="owner"):
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": owned_by_other, "agent": "Desktop_Agent1"}
            )
    finally:
        config.agent_identity_var.reset(token)


def test_recover_worker_requires_profile_gcloud_access(monkeypatch):
    from unified_agent import config, profiles

    cycle_id = _executing_recovery_cycle()
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda name: name != "gcloud_ops")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="requires gcloud_ops"):
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1"}
            )
    finally:
        config.agent_identity_var.reset(token)


def test_recover_worker_dry_run_does_not_charge_usage(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    cycle_id = _executing_recovery_cycle()
    monkeypatch.setattr(
        cloud_recovery,
        "_cloud_worker_recover",
        lambda **kwargs: {"outcome": "would_recover", "target": "example-worker"},
    )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": True}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "would_recover"
    cycle = cycles.autonomous_cycle_get(cycle_id)["cycle"]
    assert cycle["used_retries"] == 0
    assert "elapsed_s" not in result


def test_recover_worker_failed_core_charges_retry(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    cycle_id = _executing_recovery_cycle()

    def broken_core(**kwargs):
        raise RuntimeError("provider detail must not be returned")

    monkeypatch.setattr(cloud_recovery, "_cloud_worker_recover", broken_core)
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "failed"
    assert "provider detail" not in result["detail"]
    assert result["usage"]["cycle"]["used_retries"] == 1


def test_recover_worker_budget_can_expire_after_health(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    started = cycles.autonomous_cycle_start(
        "Desktop_Agent1",
        capabilities=["cloud_worker_recover", "gcloud_ops"],
        budgets={"time_s": 60},
    )
    cycle_id = started["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "time-delegate", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(cycle_id, "executing", "time-execute", "Desktop_Agent1")

    def slow_core(**kwargs):
        try:
            kwargs["before_mutation"]()
        except ToolError as exc:
            return {"outcome": "blocked", "detail": str(exc), "target": "example-worker"}
        return {"outcome": "recovered", "target": "example-worker"}

    monkeypatch.setattr(cloud_recovery, "_cloud_worker_recover", slow_core)
    monkeypatch.setattr(cycles, "time", _CyclesClock(0.0, 31.0, 31.0))
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "blocked"
    assert "expired" in result["detail"]


def test_recover_worker_requires_owner_executing_state_and_explicit_capabilities():
    from unified_agent import config

    token = config.agent_identity_var.set("Desktop_Agent1")
    with pytest.raises(ToolError, match="explicit cloud_worker_recover"):
        try:
            cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
            cycles.autonomous_cycle_transition(
                cycle_id, "delegated", "cap-delegate", "Desktop_Agent1"
            )
            cycles.autonomous_cycle_transition(
                cycle_id, "executing", "cap-execute", "Desktop_Agent1"
            )
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1"}
            )
        finally:
            config.agent_identity_var.reset(token)


def test_recover_worker_rejects_owner_mismatch_and_exhausted_budget():
    from unified_agent import config

    cycle_id = _executing_recovery_cycle()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="identity mismatch"):
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_agent3"}
            )
    finally:
        config.agent_identity_var.reset(token)

    started = cycles.autonomous_cycle_start(
        "Desktop_Agent1", capabilities=["cloud_worker_recover", "gcloud_ops"], budgets={"tokens": 1}
    )
    exhausted = started["cycle"]["id"]
    cycles.autonomous_cycle_transition(exhausted, "delegated", "budget-delegate", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(exhausted, "executing", "budget-execute", "Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute("UPDATE autonomous_cycles SET used_tokens=max_tokens WHERE id=?", (exhausted,))
        conn.commit()
    finally:
        conn.close()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="token budget"):
            cycles.autonomous_cycle_manage(
                "recover_worker", {"cycle_id": exhausted, "agent": "Desktop_Agent1"}
            )
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("outcome", ["awaiting_approval", "denied", "failed"])
def test_recover_worker_non_success_does_not_promote_cycle(monkeypatch, outcome):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    cycle_id = _executing_recovery_cycle()
    monkeypatch.setattr(
        cloud_recovery,
        "_cloud_worker_recover",
        lambda **kwargs: {"outcome": outcome, "target": "example-worker"},
    )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == outcome
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "executing"


def _task(task_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, 'cycle task', 'open', 'med', ?, ?)",
            (task_id, storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def test_cycle_transitions_are_bounded_and_idempotent():
    _task(1)
    started = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=1, max_rework=1)
    cycle_id = started["cycle"]["id"]
    result = cycles.autonomous_cycle_transition(
        cycle_id, "delegated", "dispatch-1", "Desktop_Agent1", expected_version=0
    )
    replay = cycles.autonomous_cycle_transition(
        cycle_id, "delegated", "dispatch-1", "Desktop_Agent1", expected_version=0
    )
    assert result["cycle"]["version"] == 1
    assert replay["idempotent"] is True
    with pytest.raises(ToolError, match="invalid transition"):
        cycles.autonomous_cycle_transition(cycle_id, "completed", "bad", "Desktop_Agent1")


def test_cycle_requires_evidence_and_stops_after_rework_budget():
    started = cycles.autonomous_cycle_start("Desktop_Agent1", max_rework=0)
    cycle_id = started["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"step-{index}", "Desktop_Agent1")
    stopped = cycles.autonomous_cycle_transition(
        cycle_id, "rework", "rework-1", "Desktop_Agent1", reason="tests failed"
    )
    assert stopped["cycle"]["state"] == "blocked"
    assert stopped["cycle"]["stop_reason"] == "rework budget exhausted"

    with pytest.raises(ToolError, match="pr_url"):
        cycles.autonomous_cycle_transition(cycle_id, "pr", "pr-1", "Desktop_Agent1")


def _approve_and_retry_pr(cycle_id: str, key: str, pr_url: str) -> dict:
    """pr/completed transitions are always approval-gated (force=True in
    cycles.py) regardless of UA_APPROVAL_TOOLS — mirrors
    test_pr_transition_requires_approval_and_retry_consumes_it's pattern."""
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(cycle_id, "pr", key, "Desktop_Agent1", pr_url=pr_url)
    pending = approval_pending()["pending"]
    approval_resolve(pending[-1]["id"], "approve", resolved_by="Operator")
    return cycles.autonomous_cycle_transition(cycle_id, "pr", key, "Desktop_Agent1", pr_url=pr_url)


def test_pr_transition_rejects_prose_pr_url():
    """Evidence contract (task #373, 3.2): pr_url must be a verifiable
    pointer, not free text — extends #372's evidence_ref pattern from tasks
    to cycles. Rejected before the approval gate even fires."""
    cycle_id = _cycle_at_reviewing()
    with pytest.raises(ToolError, match="verifiable pointer"):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "pr-1", "Desktop_Agent1", pr_url="see my notes for details"
        )
    assert approval_pending()["pending"] == []


def test_pr_transition_accepts_url():
    cycle_id = _cycle_at_reviewing()
    result = _approve_and_retry_pr(cycle_id, "pr-1", "https://github.com/example/repo/pull/9")
    assert result["cycle"]["state"] == "pr"


def test_pr_transition_accepts_commit_sha():
    cycle_id = _cycle_at_reviewing()
    result = _approve_and_retry_pr(cycle_id, "pr-1", "4cf6d9d8")
    assert result["cycle"]["state"] == "pr"


def test_pr_transition_rejects_short_hex_that_is_not_url_or_sha():
    cycle_id = _cycle_at_reviewing()
    with pytest.raises(ToolError, match="verifiable pointer"):
        cycles.autonomous_cycle_transition(cycle_id, "pr", "pr-1", "Desktop_Agent1", pr_url="abc12")


def test_cycle_rejects_stale_writer_and_identity_mismatch():
    from unified_agent import config

    started = cycles.autonomous_cycle_start("Desktop_Agent1")
    cycle_id = started["cycle"]["id"]
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="identity mismatch"):
            cycles.autonomous_cycle_transition(cycle_id, "delegated", "x", "Desktop_agent3")
    finally:
        config.agent_identity_var.reset(token)
    with pytest.raises(ToolError, match="stale cycle version"):
        cycles.autonomous_cycle_transition(
            cycle_id, "delegated", "x", "Desktop_Agent1", expected_version=7
        )


@pytest.mark.parametrize(
    ("usage", "dimension"),
    [
        ({"elapsed_s": 2}, "time_s"),
        ({"tokens": 2}, "tokens"),
        ({"retries": 2}, "retries"),
        ({"cost_cents": 2}, "cost_cents"),
    ],
)
def test_budget_exhaustion_is_visible_as_blocked(usage, dimension):
    started = cycles.autonomous_cycle_start(
        "Desktop_Agent1",
        budgets={"time_s": 1, "tokens": 1, "retries": 1, "cost_cents": 1},
    )
    result = cycles.autonomous_cycle_record_usage(started["cycle"]["id"], "Desktop_Agent1", **usage)
    assert result["blocked"] is True
    assert result["cycle"]["state"] == "blocked"
    assert dimension in result["reason"]


def test_capability_policy_denial_blocks_cycle():
    started = cycles.autonomous_cycle_start("Desktop_Agent1", capabilities=["task_list"])
    result = cycles.autonomous_cycle_record_usage(
        started["cycle"]["id"], "Desktop_Agent1", capability="run_cmd"
    )
    assert result["blocked"] is True
    assert "run_cmd" in result["reason"]


def test_capability_denial_blocks_transition_without_consuming_approval():
    started = cycles.autonomous_cycle_start("Desktop_Agent1", capabilities=["task_list"])
    cycle_id = started["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "cap-delegate", "Desktop_Agent1")
    result = cycles.autonomous_cycle_transition(
        cycle_id,
        "executing",
        "cap-execute",
        "Desktop_Agent1",
        capability="run_cmd",
    )
    assert result["cycle"]["state"] == "blocked"
    assert "run_cmd" in result["cycle"]["stop_reason"]
    assert not [
        row
        for row in approval_pending()["pending"]
        if row["tool"] == "autonomous_cycle:external_mutation"
    ]


def test_known_agent_allowlist_is_applied_to_cycle(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "no-hermes-config"))
    started = cycles.autonomous_cycle_start("Desktop_Agent5", capabilities=["run_cmd"])
    assert started["cycle"]["state"] == "blocked"
    assert "run_cmd" in started["cycle"]["stop_reason"]


def _cycle_at_reviewing() -> str:
    started = cycles.autonomous_cycle_start("Desktop_Agent1")
    cycle_id = started["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"gate-step-{index}", "Desktop_Agent1")
    return cycle_id


def test_pr_transition_requires_approval_and_retry_consumes_it():
    cycle_id = _cycle_at_reviewing()
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "gate-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/1"
        )
    pending = approval_pending()["pending"]
    assert pending[-1]["tool"] == "autonomous_cycle:external_mutation"
    approval_resolve(pending[-1]["id"], "approve", resolved_by="Operator")
    result = cycles.autonomous_cycle_transition(
        cycle_id, "pr", "gate-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/1"
    )
    assert result["cycle"]["state"] == "pr"


def test_merge_and_escalation_gates_leave_cycle_unchanged_until_approved():
    cycle_id = _cycle_at_reviewing()
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "blocked", "gate-blocked", "Desktop_Agent1", reason="needs human"
        )
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "reviewing"
    pending = approval_pending()["pending"][-1]
    assert pending["tool"] == "autonomous_cycle:escalation"
    approval_resolve(pending["id"], "approve", resolved_by="Operator")
    blocked = cycles.autonomous_cycle_transition(
        cycle_id, "blocked", "gate-blocked", "Desktop_Agent1", reason="needs human"
    )
    assert blocked["cycle"]["state"] == "blocked"

    completed_cycle = _cycle_at_reviewing()
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            completed_cycle,
            "pr",
            "gate-pr-merge",
            "Desktop_Agent1",
            pr_url="https://github.com/example/pr/2",
        )
    pr_pending = approval_pending()["pending"][-1]
    approval_resolve(pr_pending["id"], "approve", resolved_by="Operator")
    cycles.autonomous_cycle_transition(
        completed_cycle,
        "pr",
        "gate-pr-merge",
        "Desktop_Agent1",
        pr_url="https://github.com/example/pr/2",
    )
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            completed_cycle, "completed", "gate-complete", "Desktop_Agent1"
        )
    merge_pending = approval_pending()["pending"][-1]
    assert merge_pending["tool"] == "autonomous_cycle:merge"


def _approve_latest(tool: str) -> None:
    pending = [row for row in approval_pending()["pending"] if row["tool"] == tool]
    assert pending
    approval_resolve(pending[-1]["id"], "approve", resolved_by="Operator")


def test_acceptance_cycle_reaches_completed_only_after_approved_pr_and_merge():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"accept-{index}", "Desktop_Agent1")

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id,
            "pr",
            "accept-pr",
            "Desktop_Agent1",
            pr_url="https://github.com/example/unified-agent/pull/262",
        )
    _approve_latest("autonomous_cycle:external_mutation")
    cycles.autonomous_cycle_transition(
        cycle_id,
        "pr",
        "accept-pr",
        "Desktop_Agent1",
        pr_url="https://github.com/example/unified-agent/pull/262",
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(cycle_id, "completed", "accept-merge", "Desktop_Agent1")
    _approve_latest("autonomous_cycle:merge")
    completed = cycles.autonomous_cycle_transition(
        cycle_id, "completed", "accept-merge", "Desktop_Agent1"
    )
    assert completed["cycle"]["state"] == "completed"
    assert [item["to_state"] for item in cycles.autonomous_cycle_get(cycle_id)["transitions"]] == [
        "delegated",
        "executing",
        "validating",
        "reviewing",
        "pr",
        "completed",
    ]


def _green_worktree(monkeypatch):
    """Same seam-mocking as test_green_path_eligibility.py's fixture of the
    same name: worktree found, gates green, no red files."""
    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }

    def fake_gates(task_id, wt_path, force=False):
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr("unified_agent.cycle_eligibility._head_sha", lambda wt_path: wt["head"])
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._pr_evidence_binds_worktree",
        lambda pr_url, wt: None,
    )


def test_green_path_eligible_cycle_reaches_pr_and_merge_with_no_human(monkeypatch):
    """the operator's 2026-08-25 direction: everything up to the merge is automated.

    SUPERSEDES the earlier assertion that 'pr' stays gated for a green cycle.
    That test encoded task #384's boundary, where 'merge' was the only action a
    green verdict could skip; the human stop then landed on 'pr' — BEFORE the
    pull request existed, so a human was interrupted to authorize creating the
    very artifact they were meant to review.

    What is unchanged: both bypasses are earned per-transition by a
    pure-function eligibility check, both record why in the transition row, and
    anything not provably green still goes to the human gate (the three tests
    below). MUST 8/9 govern auto-merge-to-remote and are untouched."""
    _task(501)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=501)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"gp-{index}", "Desktop_Agent1")

    _green_worktree(monkeypatch)

    pending_before_pr = len(approval_pending()["pending"])
    cycles.autonomous_cycle_transition(
        cycle_id, "pr", "gp-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/501"
    )
    # The PR opened with no human in the loop, and no request was parked.
    assert len(approval_pending()["pending"]) == pending_before_pr

    pr_transition = cycles.autonomous_cycle_get(cycle_id)["transitions"][-1]
    assert pr_transition["to_state"] == "pr"
    assert pr_transition["reason"].startswith("[auto-pr]")
    assert "no red-tier files touched" in pr_transition["reason"]
    assert "validation gates passed" in pr_transition["reason"]

    pending_before = len(approval_pending()["pending"])
    result = cycles.autonomous_cycle_transition(cycle_id, "completed", "gp-merge", "Desktop_Agent1")
    assert result["cycle"]["state"] == "completed"
    # No new approval request was created for the merge — it was skipped, not
    # silently auto-approved after the fact.
    assert len(approval_pending()["pending"]) == pending_before

    transitions = cycles.autonomous_cycle_get(cycle_id)["transitions"]
    merge_transition = transitions[-1]
    assert merge_transition["to_state"] == "completed"
    assert merge_transition["reason"].startswith("[green-path auto-merge]")
    assert "no red-tier files touched" in merge_transition["reason"]


def test_green_path_red_tier_diff_still_requires_approval(monkeypatch):
    """A cycle that would otherwise be green-path eligible, but whose diff
    touches a red-tier file, must still hit the human approval gate — MUST 9:
    the default stays human-merge for anything not provably green."""
    _task(502)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=502)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"rt-{index}", "Desktop_Agent1")

    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/unified_agent/safety.py", "status": "M"}],
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "rt-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/502"
        )
    _approve_latest("autonomous_cycle:external_mutation")
    cycles.autonomous_cycle_transition(
        cycle_id, "pr", "rt-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/502"
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(cycle_id, "completed", "rt-merge", "Desktop_Agent1")
    merge_pending = approval_pending()["pending"][-1]
    assert merge_pending["tool"] == "autonomous_cycle:merge"


def test_green_path_eligibility_failure_fails_closed_to_approval(monkeypatch):
    """If the eligibility check itself blows up, that must never be read as
    'eligible' — it must fall through to the normal human-approval path,
    same as any other ineligible verdict."""
    _task(503)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=503)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"fc-{index}", "Desktop_Agent1")

    def exploding_worktree(task_id):
        raise RuntimeError("git worktree lookup exploded")

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", exploding_worktree)

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "fc-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/503"
        )
    _approve_latest("autonomous_cycle:external_mutation")
    cycles.autonomous_cycle_transition(
        cycle_id, "pr", "fc-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/503"
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(cycle_id, "completed", "fc-merge", "Desktop_Agent1")


def test_auto_pr_still_gated_when_validation_gates_fail(monkeypatch):
    """A worktree that exists but whose gates fail is the case the two tests
    above do not cover: they fail at red-tier and at an exploding lookup. Here
    the lookup succeeds and pytest/audit is what says no. Still human-gated."""
    _task(504)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=504)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"vg-{index}", "Desktop_Agent1")

    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }

    def failing_gates(task_id, wt_path, force=False):
        raise RuntimeError("pytest: 3 failed")

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", failing_gates)
    monkeypatch.setattr("unified_agent.cycle_eligibility._head_sha", lambda wt_path: wt["head"])
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "vg-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/504"
        )
    assert approval_pending()["pending"][-1]["tool"] == "autonomous_cycle:external_mutation"


def test_auto_pr_eligibility_rejects_a_cycle_carrying_a_stop_reason(monkeypatch):
    """Unit-level: a cycle that recorded friction is not 'green', even if
    everything else checks out. Mirrors the merge criteria's same rule."""
    from unified_agent.reviewer import autonomous_cycle_pr_path_eligibility

    _green_worktree(monkeypatch)

    verdict = autonomous_cycle_pr_path_eligibility(
        505, {"state": "reviewing", "stop_reason": "budget exhausted"}
    )
    assert verdict["eligible"] is False
    assert "stop_reason" in verdict["reasons"][-1]


def test_auto_pr_eligibility_rejects_wrong_state(monkeypatch):
    """'reviewing' is the only state TRANSITIONS allows into 'pr'. Anything
    else is a caller bug, and must not be auto-approved on its way through."""
    from unified_agent.reviewer import autonomous_cycle_pr_path_eligibility

    _green_worktree(monkeypatch)

    for state in ("executing", "validating", "pr", "completed", "blocked"):
        verdict = autonomous_cycle_pr_path_eligibility(506, {"state": state, "stop_reason": None})
        assert verdict["eligible"] is False, f"{state!r} must not be auto-PR eligible"
        assert "expected 'reviewing'" in verdict["reasons"][0]


def test_unreadable_diff_is_not_read_as_clean_and_still_requires_approval(monkeypatch):
    """An unreadable diff must not read as clean.

    `_red_tier_for_task` returned [] both for 'diff is clean' and for 'diff
    could not be read at all' (no worktree, no usable main base, git diff
    failing). An eligibility check reading only that list would treat an
    unreadable diff as 'no red-tier files touched' and skip the human gate —
    so a branch touching safety.py could bypass approval purely because the
    diff command failed. Fail closed instead."""
    _task(507)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=507)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"ud-{index}", "Desktop_Agent1")

    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }

    def unreadable_diff(wt_path, branch):
        raise RuntimeError("fatal: ambiguous argument 'origin/main'")

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(
        "unified_agent.merge.run_validation_gates",
        lambda task_id, wt_path, force=False: {"success": True},
    )
    monkeypatch.setattr("unified_agent.cycle_eligibility._changed_files_vs_main", unreadable_diff)

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "pr", "ud-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/507"
        )
    assert approval_pending()["pending"][-1]["tool"] == "autonomous_cycle:external_mutation"


def test_an_empty_diff_is_not_provably_clean_either(monkeypatch):
    """The same ambiguity one level down: `_changed_files_vs_main` returning []
    means either 'branch has no changes' or 'the diff produced nothing usable'.
    Neither is grounds for certifying a branch green."""
    from unified_agent.reviewer import _red_tier_for_task_checked

    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": "a" * 40}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.cycle_eligibility._changed_files_vs_main", lambda p, b: [])

    hits, unclassified = _red_tier_for_task_checked(508)
    assert hits == []
    assert unclassified is not None, "an empty diff must not certify as classified-clean"


def test_renaming_a_red_tier_file_away_is_classified_red_tier(monkeypatch, tmp_path):
    """`git diff --name-status` puts both rename paths on one line; the old one counts."""
    import subprocess

    from unified_agent.cycle_eligibility import _changed_files_vs_main, _is_red_tier

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@e", "-c", "user.name=t", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    pkg = tmp_path / "src" / "unified_agent"
    pkg.mkdir(parents=True)
    (pkg / "safety.py").write_text("".join(f"line{i} = {i}\n" for i in range(50)))
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "task/x")
    git("mv", "src/unified_agent/safety.py", "src/unified_agent/notes.txt")
    git("commit", "-qm", "rename")
    monkeypatch.setattr("unified_agent.safety.resolve_task_worktree", lambda path: path)

    changed = _changed_files_vs_main(str(tmp_path), "task/x")
    assert _is_red_tier(changed) == ["src/unified_agent/safety.py"]


def test_eligibility_gates_do_not_run_inside_the_write_transaction(monkeypatch):
    """Eligibility gates must not run inside the write transaction.

    The eligibility check runs the security audit and pytest, which take
    minutes. Running them inside the BEGIN IMMEDIATE transaction held the
    SQLite write lock for that whole time, and every connection has a 5s busy
    timeout — so unrelated task/memory/approval writes would fail with
    'database is locked'. Assert the lock is actually free while the gates
    run, by doing a real unrelated write from a second connection."""
    _task(509)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=509)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"lk-{index}", "Desktop_Agent1")

    observed = {}

    def gates_that_probe_the_lock(task_id, wt_path, force=False):
        from unified_agent import storage

        probe = storage.connect()
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("UPDATE tasks SET updated_at=? WHERE id=?", (storage.now(), 509))
            probe.commit()
            observed["write_succeeded"] = True
        except sqlite3.OperationalError as exc:  # pragma: no cover - failure path is the bug
            observed["write_succeeded"] = False
            observed["error"] = str(exc)
        finally:
            probe.close()
        return {"success": True}

    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": "a" * 40}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", gates_that_probe_the_lock)
    monkeypatch.setattr("unified_agent.cycle_eligibility._head_sha", lambda wt_path: wt["head"])
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._pr_evidence_binds_worktree",
        lambda pr_url, wt: None,
    )

    cycles.autonomous_cycle_transition(
        cycle_id, "pr", "lk-pr", "Desktop_Agent1", pr_url="https://github.com/example/pr/509"
    )
    assert observed.get("write_succeeded") is True, (
        f"write lock was held during validation gates: {observed.get('error')}"
    )


def test_auto_pr_eligibility_propagates_unexpected_validation_errors(monkeypatch):
    """Only operational gate failures may be
    converted into an ineligible verdict. Implementation bugs must propagate."""
    from unified_agent.reviewer import autonomous_cycle_pr_path_eligibility

    _green_worktree(monkeypatch)

    def buggy_gates(task_id, wt_path, force=False):
        raise TypeError("programmer error")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", buggy_gates)

    with pytest.raises(TypeError, match="programmer error"):
        autonomous_cycle_pr_path_eligibility(510, {"state": "reviewing", "stop_reason": None})


def test_auto_pr_eligibility_ineligible_when_head_moves_during_validation(monkeypatch):
    """task #598, pr-path twin of the green-path regression test: the auto-PR
    bypass must hold the same way when HEAD moves mid-validation, not just
    the auto-merge bypass."""
    from unified_agent.reviewer import autonomous_cycle_pr_path_eligibility

    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }
    moved_sha = "f" * 40

    def fake_gates(task_id, wt_path, force=False):
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    heads = iter([wt["head"], moved_sha])
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr("unified_agent.cycle_eligibility._head_sha", lambda wt_path: next(heads))
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._pr_evidence_binds_worktree",
        lambda pr_url, wt: None,
    )

    verdict = autonomous_cycle_pr_path_eligibility(513, {"state": "reviewing", "stop_reason": None})
    assert verdict["eligible"] is False
    assert "HEAD moved during validation" in verdict["reasons"][-1]


def test_auto_pr_eligibility_propagates_unexpected_classification_errors(monkeypatch):
    """Red-tier classification must not swallow
    implementation bugs — only operational inspection failures fail closed."""
    from unified_agent.reviewer import autonomous_cycle_pr_path_eligibility

    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": "a" * 40}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._is_red_tier",
        lambda changed: (_ for _ in ()).throw(TypeError("programmer error")),
    )

    with pytest.raises(TypeError, match="programmer error"):
        autonomous_cycle_pr_path_eligibility(512, {"state": "reviewing", "stop_reason": None})


def test_mismatched_pr_evidence_still_requires_approval(monkeypatch):
    """Codex P1 on PR #289: auto-PR bypass must bind pr_url to worktree HEAD."""
    _task(511)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=511)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"eb-{index}", "Desktop_Agent1")

    wt = {
        "path": "/tmp/wt-x",
        "branch": "fix/task-x",
        "head": "abc123def4567890abc123def4567890abc123de",
    }
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(
        "unified_agent.merge.run_validation_gates",
        lambda task_id, wt_path, force=False: {"success": True},
    )
    monkeypatch.setattr(
        "unified_agent.cycle_eligibility._changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )

    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id,
            "pr",
            "eb-pr",
            "Desktop_Agent1",
            pr_url="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )
    assert approval_pending()["pending"][-1]["tool"] == "autonomous_cycle:external_mutation"


def test_acceptance_cycle_failure_requires_approved_escalation():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "fail-delegate", "Desktop_Agent1")
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "failed", "fail-stop", "Desktop_Agent1", reason="executor crashed"
        )
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "delegated"
    _approve_latest("autonomous_cycle:escalation")
    failed = cycles.autonomous_cycle_transition(
        cycle_id, "failed", "fail-stop", "Desktop_Agent1", reason="executor crashed"
    )
    assert failed["cycle"]["state"] == "failed"
    assert failed["cycle"]["stop_reason"] == "executor crashed"


def test_acceptance_rework_and_operator_resume_are_bounded():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", max_rework=1)["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "rw-delegate", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(cycle_id, "executing", "rw-execute", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(
        cycle_id, "rework", "rw-rework", "Desktop_Agent1", reason="validation failed"
    )
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["rework_count"] == 1
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "rw-retry", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(cycle_id, "executing", "rw-execute-2", "Desktop_Agent1")
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "blocked", "rw-block", "Desktop_Agent1", reason="operator review"
        )
    _approve_latest("autonomous_cycle:escalation")
    cycles.autonomous_cycle_transition(
        cycle_id, "blocked", "rw-block", "Desktop_Agent1", reason="operator review"
    )
    resumed = cycles.autonomous_cycle_transition(cycle_id, "plan", "rw-resume", "Desktop_Agent1")
    assert resumed["cycle"]["state"] == "plan"


# --- kill switch (task #373 Tier 2, 3.3) ---------------------------------


def test_kill_requires_reason():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    with pytest.raises(ToolError, match="reason is required"):
        cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "")


def test_kill_works_from_any_non_terminal_state_without_approval():
    """The kill switch must never wait on an approval round-trip — that
    defeats the point of a kill switch. 'killed' is deliberately absent
    from APPROVAL_ACTIONS."""
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "k-1", "Desktop_Agent1")
    cycles.autonomous_cycle_transition(cycle_id, "executing", "k-2", "Desktop_Agent1")
    result = cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "runaway loop suspected")
    assert result["cycle"]["state"] == "killed"
    assert result["killed_from"] == "executing"
    assert "runaway loop suspected" in result["cycle"]["stop_reason"]
    assert approval_pending()["pending"] == []


def test_killed_is_distinct_from_blocked_failed_completed():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "test isolation")
    cycle = cycles.autonomous_cycle_get(cycle_id)["cycle"]
    assert cycle["state"] == "killed"
    assert cycle["state"] not in {"blocked", "failed", "completed"}


def test_kill_is_recorded_in_transition_history():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "audit trail check")
    transitions = cycles.autonomous_cycle_get(cycle_id)["transitions"]
    assert transitions[-1]["to_state"] == "killed"
    assert transitions[-1]["actor"] == "Desktop_Agent1"
    assert "audit trail check" in transitions[-1]["reason"]


def test_cannot_kill_an_already_terminal_cycle():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "first kill")
    with pytest.raises(ToolError, match="already terminal"):
        cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "second kill")


def test_killed_cycle_rejects_further_usage_recording():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1")["cycle"]["id"]
    cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "stop it")
    with pytest.raises(ToolError, match="terminal"):
        cycles.autonomous_cycle_record_usage(cycle_id, "Desktop_Agent1", elapsed_s=10)


def test_kill_can_stop_a_blocked_cycle_too():
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", max_rework=0)["cycle"]["id"]
    for index, state in enumerate(("delegated", "executing", "validating", "reviewing"), start=1):
        cycles.autonomous_cycle_transition(cycle_id, state, f"b-{index}", "Desktop_Agent1")
    stopped = cycles.autonomous_cycle_transition(
        cycle_id, "rework", "b-rework", "Desktop_Agent1", reason="tests failed"
    )
    assert stopped["cycle"]["state"] == "blocked"
    killed = cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "definitively done")
    assert killed["cycle"]["state"] == "killed"
    assert killed["killed_from"] == "blocked"


# --- active_cycle_for_task (task #373 Tier 2, 3.3 daemon wiring) ---------


def test_active_cycle_for_task_returns_none_with_no_cycle():
    assert cycles.active_cycle_for_task(9999) is None


def test_active_cycle_for_task_finds_non_terminal_cycle():
    _task(2)
    started = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=2)
    cycle_id = started["cycle"]["id"]
    found = cycles.active_cycle_for_task(2)
    assert found is not None
    assert found["id"] == cycle_id


def test_active_cycle_for_task_ignores_naturally_terminal_cycles():
    """completed/failed drop out — a naturally-finished cycle must not block
    a later, unrelated re-delegation of the same task forever."""
    _task(3)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=3)["cycle"]["id"]
    cycles.autonomous_cycle_transition(cycle_id, "delegated", "t3-1", "Desktop_Agent1")
    with pytest.raises(ApprovalPendingError):
        cycles.autonomous_cycle_transition(
            cycle_id, "failed", "t3-2", "Desktop_Agent1", reason="done with this task"
        )
    pending = approval_pending()["pending"]
    approval_resolve(pending[-1]["id"], "approve", resolved_by="Operator")
    cycles.autonomous_cycle_transition(
        cycle_id, "failed", "t3-2", "Desktop_Agent1", reason="done with this task"
    )
    assert cycles.active_cycle_for_task(3) is None


def test_active_cycle_for_task_still_finds_a_killed_cycle():
    """A killed cycle must NOT disappear from this lookup — the daemon's
    pre-dispatch gate (executor/daemon.py) relies on finding it to refuse
    further work. Excluding it here would make the kill switch a no-op."""
    _task(4)
    cycle_id = cycles.autonomous_cycle_start("Desktop_Agent1", task_id=4)["cycle"]["id"]
    cycles.autonomous_cycle_kill(cycle_id, "Desktop_Agent1", "stop this task's cycle")
    found = cycles.active_cycle_for_task(4)
    assert found is not None
    assert found["state"] == "killed"


def test_recovery_evidence_failure_prevents_promotion_but_charges_usage(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import cloud_recovery

    cycle_id = _executing_recovery_cycle()
    monkeypatch.setattr(
        cloud_recovery, "_cloud_worker_recover", lambda **kwargs: {"outcome": "healthy"}
    )
    record_event = cycles.events.record_event

    def fail_recovery_evidence(channel, **kwargs):
        if channel == "agent.cycle.recovery":
            raise RuntimeError("storage unavailable")
        return record_event(channel, **kwargs)

    monkeypatch.setattr(cycles.events, "record_event", fail_recovery_evidence)
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_manage(
            "recover_worker", {"cycle_id": cycle_id, "agent": "Desktop_Agent1", "dry_run": False}
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["evidence_error"] == "RuntimeError"
    assert result["usage"]["cycle"]["used_retries"] == 1
    assert cycles.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "executing"


def test_cycle_recovery_uses_explicit_server_worker(monkeypatch):
    from unified_agent import config
    from unified_agent.tools import gcloud

    monkeypatch.setenv("UA_GCP_PROJECT", "selected-project")
    monkeypatch.setenv("UA_GCP_WORKER", "selected-worker")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "gcloud_ops")
    seen = []

    def inventory(**kwargs):
        seen.append(kwargs)
        return {
            "sections": {"instances": "ok"},
            "instances": [
                {"name": "selected-worker", "status": "TERMINATED", "zone": "europe-west1-b"}
            ],
        }

    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory)
    monkeypatch.setattr(gcloud, "gcloud_ops", lambda **kwargs: pytest.fail("dry-run mutated"))
    cycle_id = _executing_recovery_cycle()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = cycles.autonomous_cycle_recover_worker(cycle_id, "Desktop_Agent1")
    finally:
        config.agent_identity_var.reset(token)
    assert result["outcome"] == "would_recover"
    assert result["target"] == "selected-worker"
    assert seen == [{"project": "selected-project", "sections": ["instances"]}]
