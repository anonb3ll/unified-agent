"""Profile inspection stays read-only and respects configured MCP approval gates."""

import pytest

from unified_agent import config, operating_profiles, server
from unified_agent.tools import approvals, tasks


@pytest.mark.parametrize(
    "action",
    ["operating_profiles", "operating_resolve", "operating_sprint_list", "operating_sprint_get"],
)
@pytest.mark.parametrize("positional", [False, True])
@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_profile_reads_honor_configured_approval_without_becoming_writes(
    isolated_env, tmp_path, monkeypatch, action, positional, configured
):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage" if configured else "")
    path = tmp_path / "inspection-policy.yaml"
    path.write_text("version: 1\ntrust_presets:\n  default: inspection\n")
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _: True)
    effects = []

    def goal_manage(action, params=None):
        effects.append(action)
        return {"action": action}

    wrapped = server._guarded_and_instrumented(goal_manage)

    def call():
        return wrapped(action) if positional else wrapped(action=action)

    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        if configured:
            with pytest.raises(approvals.ApprovalPendingError) as parked:
                call()
            assert effects == []
            [request] = approvals.approval_pending()["pending"]
            assert request["id"] == parked.value.approval_id
            assert request["tool"] == "goal_manage"
            assert request["payload"]["action"] == action
            human = config.agent_identity_var.set("Desktop_Person")
            try:
                approvals.approval_resolve(request["id"], "approve")
            finally:
                config.agent_identity_var.reset(human)
        # The inspection trust preset admits reads but denies writes, so
        # success also proves these actions retain their read classification.
        assert call() == {"action": action}
        assert effects == [action]
        if configured:
            assert approvals.get_approval(request["id"])["status"] == "consumed"
            assert approvals.approval_pending()["pending"] == []
            with pytest.raises(approvals.ApprovalPendingError) as again:
                call()
            assert again.value.approval_id != request["id"]
            assert effects == [action]
        else:
            assert approvals.approval_pending()["pending"] == []
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize(
    "action",
    [
        "operating_sprint_add",
        "operating_sprint_update",
        "operating_sprint_link",
        "operating_sprint_unlink",
        "operating_run_set",
        "operating_run_clear",
    ],
)
def test_profile_mutations_keep_write_approval(isolated_env, monkeypatch, action):
    # Only actionable, visible targets reach the write gate; missing targets
    # are rejected before approval rather than parking impossible requests.
    tid = tasks.task_add("visible operating target")["id"]
    human = config.agent_identity_var.set("Desktop_Person")
    try:
        sid = operating_profiles.sprint_add("visible sprint")["id"]
    finally:
        config.agent_identity_var.reset(human)
    params = {"task_id": tid, "sprint_id": sid}
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _: True)
    effects = []

    def goal_manage(action, params=None):
        effects.append(action)

    wrapped = server._guarded_and_instrumented(goal_manage)
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(approvals.ApprovalPendingError):
            wrapped(action=action, params=params)
    finally:
        config.agent_identity_var.reset(token)
    assert effects == []
    pending = approvals.approval_pending()["pending"]
    assert len(pending) == 1 and pending[0]["tool"] == "goal_manage"
