"""A mission decision is enforced at the real local task-merge boundary."""

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import autonomy_modes, config, goals, merge, storage

HUMAN = "Desktop_Person"
AGENT = "Desktop_Agent2"


@contextmanager
def as_actor(label):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def boundary(isolated_env, monkeypatch):
    storage.init_db()
    repo = config.workspace_root()
    repo.mkdir(parents=True, exist_ok=True)
    quota_root = repo / "empty-codex-sessions"
    quota_root.mkdir()
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(quota_root))
    for args in (
        ["init", "-b", "main"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        ["checkout", "-b", "codex/task-17"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id,title,status,priority,project,claimed_by,worktree_path,branch,created_at,updated_at) "
            "VALUES (17,'Merge me','done','med','unified-agent','Desktop_Agent5',?,?,?,?)",
            (str(repo), "codex/task-17", storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    mission = goals.goal_add("Mission", author=HUMAN)["goal"]["id"]
    goals.link_task(17, mission, author=HUMAN)
    monkeypatch.setattr(
        merge,
        "find_task_worktree",
        lambda _id: ({"path": str(repo), "branch": "codex/task-17"}, str(repo)),
    )
    validate = Mock(return_value={"success": True})
    monkeypatch.setattr(merge, "run_validation_gates", validate)
    real_git = merge._run_git_in_repo
    mutations = []
    hooks = {}

    def git(args, path):
        if args[0] in {"checkout", "pull", "merge", "branch", "worktree", "update-ref"}:
            mutations.append(args)
            if args[0] in hooks:
                hooks[args[0]]()
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_git(args, path)

    monkeypatch.setattr(merge, "_run_git_in_repo", git)
    monkeypatch.setattr(merge, "_worktree_remove_in_repo", Mock())
    monkeypatch.setattr(merge, "sync_state_to_vault", Mock())
    monkeypatch.setattr(merge, "_run_post_merge_verify", lambda _id: None)
    monkeypatch.setattr(merge, "_close_delegation_ledger", Mock(return_value={}))
    return {
        "repo": repo,
        "mission": mission,
        "validate": validate,
        "mutations": mutations,
        "hooks": hooks,
        "monkeypatch": monkeypatch,
    }


def mode(boundary, name):
    with as_actor(HUMAN):
        autonomy_modes.set_mode(boundary["mission"], name, HUMAN)


def approve_production(*, force=False):
    from unified_agent import safety
    from unified_agent.tools import approvals

    with as_actor(AGENT), pytest.raises(approvals.ApprovalPendingError) as pending:
        safety.check_prod_action_gate(
            "git_merge_task",
            {"task_id": 17, "force": force, "dry_run": False, "allow_in_flight": False},
        )
    with as_actor(HUMAN):
        approvals.approval_resolve(pending.value.approval_id, "approve", resolved_by=HUMAN)


def test_action_review_pauses_before_validation_and_git_mutation(boundary):
    mode(boundary, "action-review")
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["success"] is False
    assert result["autonomy"]["decision"] == "pause_for_human"
    assert result["autonomy"]["actor"] == AGENT
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


def test_broken_goal_link_denies_even_with_force(boundary):
    conn = storage.connect()
    conn.execute("UPDATE task_goal_links SET goal_id=999 WHERE task_id=17")
    conn.commit()
    conn.close()
    with as_actor(AGENT), pytest.raises(ToolError, match="mission|goal"):
        merge.orchestrate_merge(17, force=True)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


def _stamp_mission_org(boundary, org):
    conn = storage.connect()
    conn.execute("UPDATE goals SET org_id=? WHERE id=?", (org, boundary["mission"]))
    conn.commit()
    conn.close()


def test_mission_resolves_only_within_the_task_tenant(boundary, monkeypatch):
    """#2014: tenant B's task linked (pre-fence) to tenant A's goal has no readable
    mission, and that denies rather than falling back to the unlinked legacy path."""
    _stamp_mission_org(boundary, "org_a")
    conn = storage.connect()
    conn.execute("UPDATE tasks SET org_id='org_b' WHERE id=17")
    conn.commit()
    conn.close()
    # Merges run from untenanted cron: the task row's tenant decides, not the caller's.
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    with pytest.raises(ToolError, match="unknown mission goal"):
        merge._task_merge_mission(17)
    with as_actor(AGENT), pytest.raises(ToolError, match="unknown mission goal"):
        merge.orchestrate_merge(17, force=True)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []
    _stamp_mission_org(boundary, "org_b")
    assert merge._task_merge_mission(17) == boundary["mission"]


def test_mission_moving_tenant_after_checkout_stops_the_merge(boundary):
    """#2014: the execution-admission fence re-reads the goal under the task's tenant."""
    mode(boundary, "observe")
    boundary["hooks"]["pull"] = lambda: _stamp_mission_org(boundary, "org_a")
    approve_production()
    with as_actor(AGENT), pytest.raises(ToolError, match="execution admission"):
        merge.orchestrate_merge(17)
    assert [args[0] for args in boundary["mutations"]] == ["checkout", "pull"]


def acknowledge(boundary):
    with as_actor(AGENT):
        paused = merge.orchestrate_merge(17)
    with as_actor(HUMAN):
        autonomy_modes.review_action(paused["autonomy"]["pending_id"], HUMAN)
    return paused["autonomy"]["pending_id"]


def test_human_review_allows_only_exact_task_and_committed_source(boundary):
    mode(boundary, "action-review")
    pending = acknowledge(boundary)
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert result["success"] is True
    [merge_args] = [args for args in boundary["mutations"] if args[0] == "merge"]
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=boundary["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert merge_args[1] == expected_head
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT started FROM autonomy_pending_actions WHERE id=?", (pending,)
            ).fetchone()[0]
            == 1
        )
        conn.execute(
            "INSERT INTO tasks (id,title,status,priority,project,created_at,updated_at) VALUES (18,'Another task','done','med','unified-agent',?,?)",
            (storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    goals.link_task(18, boundary["mission"], author=HUMAN)
    boundary["mutations"].clear()
    with as_actor(AGENT):
        other = merge.orchestrate_merge(18, force=True)
    assert other["autonomy"]["decision"] == "pause_for_human"
    assert other["autonomy"]["pending_id"] != pending
    assert boundary["mutations"] == []


@pytest.mark.parametrize(
    "change",
    [
        "grant",
        "lifecycle",
        "preset",
        "revocation",
        "scope",
        "link",
        "actor",
        "head",
        "policy",
        "profile",
        "access",
        "takeover",
        "status",
        "cancel",
    ],
)
def test_validation_time_authority_or_source_change_stops_before_checkout(
    boundary, monkeypatch, tmp_path, change
):
    from unified_agent import agent_lifecycle

    mode(boundary, "checkpoint")
    with as_actor(HUMAN):
        autonomy_modes.grant_standing(boundary["mission"], "merge", HUMAN)

    def mutate(*_args, **_kwargs):
        if change == "grant":
            with as_actor(HUMAN):
                autonomy_modes.revoke_grant(boundary["mission"], "merge", HUMAN)
        elif change == "lifecycle":
            with as_actor(HUMAN):
                agent_lifecycle.set_agent_state(AGENT, "pause", "test stop")
        elif change == "preset":
            policy = tmp_path / "policy.yaml"
            policy.write_text("trust_presets:\n  agents:\n    Desktop_Agent2: inspection\n")
            monkeypatch.setenv("UA_POLICY_FILE", str(policy))
        elif change == "revocation":
            monkeypatch.setattr(config, "revoked_agents", lambda: frozenset({AGENT}))
        elif change == "scope":
            monkeypatch.setattr(config, "agent_scopes", lambda: {AGENT: frozenset({"deploy"})})
        elif change == "link":
            conn = storage.connect()
            conn.execute("DELETE FROM task_goal_links WHERE task_id=17")
            conn.commit()
            conn.close()
        elif change == "policy":
            policy = tmp_path / "deny-policy.yaml"
            policy.write_text(
                "enabled: true\nrules:\n  - name: stop-merge\n    type: tool\n    tool: git_merge_task\n    action: deny\n"
            )
            monkeypatch.setenv("UA_POLICY_FILE", str(policy))
        elif change == "profile":
            monkeypatch.setattr(merge.profiles, "tool_is_allowed", lambda _tool: False)
        elif change == "access":
            monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
        elif change in {"takeover", "status", "cancel"}:
            conn = storage.connect()
            if change == "takeover":
                conn.execute(
                    "UPDATE tasks SET current_run_id='sp-successor', current_run_agent='Desktop_agent3' WHERE id=17"
                )
            elif change == "status":
                conn.execute("UPDATE tasks SET status='doing' WHERE id=17")
            else:
                conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=17")
            conn.commit()
            conn.close()
        elif change == "actor":
            monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        else:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "changed during validation",
                ],
                cwd=boundary["repo"],
                check=True,
                capture_output=True,
            )

    boundary["validate"].side_effect = mutate
    with as_actor(AGENT), pytest.raises(ToolError):
        merge.orchestrate_merge(17, force=True)
    assert boundary["mutations"] == []
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM autonomy_pending_actions WHERE started=1"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_new_head_cannot_reuse_human_review(boundary):
    mode(boundary, "action-review")
    previous = acknowledge(boundary)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "new source",
        ],
        cwd=boundary["repo"],
        check=True,
        capture_output=True,
    )
    with as_actor(AGENT):
        new = merge.orchestrate_merge(17)
    assert new["autonomy"]["decision"] == "pause_for_human"
    assert new["autonomy"]["pending_id"] != previous
    assert boundary["mutations"] == []


def test_dry_run_validates_without_creating_or_consuming_action(boundary, monkeypatch):
    mode(boundary, "action-review")
    evaluate = Mock(side_effect=AssertionError("dry run must not persist evaluation"))
    started = Mock(side_effect=AssertionError("dry run must not start an action"))
    monkeypatch.setattr(autonomy_modes, "evaluate", evaluate)
    monkeypatch.setattr(autonomy_modes, "mark_started", started)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, dry_run=True)
    assert result["success"] and result["dry_run"]
    assert result["autonomy"]["decision"] == "pause_for_human"
    boundary["validate"].assert_called_once()
    assert boundary["mutations"] == []
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM autonomy_pending_actions").fetchone()[0] == 0
    finally:
        conn.close()


def test_unlinked_task_keeps_legacy_behavior_without_verified_caller(boundary, monkeypatch):
    conn = storage.connect()
    conn.execute("DELETE FROM task_goal_links WHERE task_id=17")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    monkeypatch.setattr(
        autonomy_modes, "evaluate", Mock(side_effect=AssertionError("legacy task has no mission"))
    )
    assert merge.orchestrate_merge(17)["success"]
    assert boundary["mutations"][0] == ["checkout", "main"]


@pytest.mark.parametrize("failure", ["anonymous", "wrong-project", "unavailable-links"])
def test_linked_authority_requires_verified_caller_and_readable_same_project_goal(
    boundary, monkeypatch, failure
):
    if failure == "anonymous":
        monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    else:
        conn = storage.connect()
        if failure == "wrong-project":
            conn.execute("UPDATE goals SET project='other' WHERE id=?", (boundary["mission"],))
        else:
            conn.execute("DROP TABLE task_goal_links")
        conn.commit()
        conn.close()
    with as_actor(AGENT), pytest.raises(ToolError):
        merge.orchestrate_merge(17, force=True)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


def test_source_advance_after_admission_merges_reviewed_sha_and_skips_cleanup(boundary):
    mode(boundary, "action-review")
    acknowledge(boundary)
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=boundary["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def advance():
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "unreviewed successor",
            ],
            cwd=boundary["repo"],
            check=True,
            capture_output=True,
        )

    boundary["hooks"]["merge"] = advance
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["success"]
    assert result["cleanup_skipped"] == "source changed after admission"
    [args] = [args for args in boundary["mutations"] if args[0] == "merge"]
    assert args[1] == reviewed_head
    assert not any(args[0] == "branch" for args in boundary["mutations"])
    merge._worktree_remove_in_repo.assert_not_called()
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM tasks WHERE id=17").fetchone()[0] == "waiting_review"
        )
    finally:
        conn.close()
    assert result["merged_source_head"] == reviewed_head
    assert result["task_completed"] is False
    merge._close_delegation_ledger.assert_not_called()


def test_wrapper_production_approval_is_consumed_once(boundary):
    from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve, get_approval
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "observe")
    with as_actor(AGENT), pytest.raises(ApprovalPendingError) as pending:
        git_merge_task(17)
    approval_id = pending.value.approval_id
    with as_actor(HUMAN):
        approval_resolve(approval_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT):
        assert git_merge_task(17)["success"]
    assert get_approval(approval_id)["status"] == "consumed"
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM approvals WHERE tool='git_merge_task'").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_cancelled_mission_run_cannot_merge_even_with_force(boundary):
    conn = storage.connect()
    conn.execute("UPDATE tasks SET current_run_id='sp-cancelled', cancel_requested=1 WHERE id=17")
    conn.commit()
    conn.close()
    with as_actor(AGENT), pytest.raises(ToolError, match="cancellation"):
        merge.orchestrate_merge(17, force=True)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


@pytest.mark.parametrize("field", ["run", "status"])
def test_task_state_change_before_mission_admission_cannot_adopt_old_status_guard(
    boundary, monkeypatch, field
):
    mode(boundary, "observe")
    original_find = merge.find_task_worktree

    def find(task_id):
        conn = storage.connect()
        if field == "run":
            conn.execute("UPDATE tasks SET current_run_id='sp-successor' WHERE id=17")
        else:
            conn.execute("UPDATE tasks SET status='doing' WHERE id=17")
        conn.commit()
        conn.close()
        return original_find(task_id)

    monkeypatch.setattr(merge, "find_task_worktree", find)
    evaluated = Mock(wraps=autonomy_modes.evaluate)
    monkeypatch.setattr(autonomy_modes, "evaluate", evaluated)
    with as_actor(AGENT), pytest.raises(ToolError, match="before.*admission"):
        merge.orchestrate_merge(17)
    evaluated.assert_not_called()
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


@pytest.mark.parametrize("kind", ["tool", "default", "role", "threshold"])
def test_direct_merge_enforces_stable_central_policy_denial(boundary, monkeypatch, kind):
    mode(boundary, "observe")
    rule = {
        "tool": "rules:\n  - name: denied\n    type: tool\n    tool: git_merge_task\n    action: deny\n",
        "default": "default_action: deny\nrules: []\n",
        "role": "rules:\n  - name: wrong-role\n    type: role\n    tool: git_merge_task\n    roles: [human]\n    action: allow\n",
        "threshold": "rules:\n  - name: too-large\n    type: threshold\n    tool: git_merge_task\n    field: task_id\n    max: 0\n    action: deny\n",
    }[kind]
    path = boundary["repo"] / "policy.yaml"
    path.write_text(rule)
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    with as_actor(AGENT), pytest.raises(ToolError):
        merge.orchestrate_merge(17)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


def test_structured_mission_pause_restores_production_approval_for_retry(boundary):
    from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve, get_approval
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "action-review")
    with as_actor(AGENT), pytest.raises(ApprovalPendingError) as pending:
        git_merge_task(17)
    approval_id = pending.value.approval_id
    with as_actor(HUMAN):
        approval_resolve(approval_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT):
        paused = git_merge_task(17)
    assert paused["autonomy"]["decision"] == "pause_for_human"
    assert get_approval(approval_id)["status"] == "approved"
    with as_actor(HUMAN):
        autonomy_modes.review_action(paused["autonomy"]["pending_id"], HUMAN)
    with as_actor(AGENT):
        assert git_merge_task(17)["success"]
    assert get_approval(approval_id)["status"] == "consumed"


def test_takeover_immediately_before_start_is_denied_on_owning_transaction(boundary, monkeypatch):
    mode(boundary, "observe")
    original_start = autonomy_modes.mark_started

    def takeover(*args, **kwargs):
        conn = storage.connect()
        conn.execute("UPDATE tasks SET current_run_id='sp-last-moment' WHERE id=17")
        conn.commit()
        conn.close()
        return original_start(*args, **kwargs)

    monkeypatch.setattr(autonomy_modes, "mark_started", takeover)
    approve_production()
    with as_actor(AGENT), pytest.raises(ToolError):
        merge.orchestrate_merge(17)
    assert boundary["mutations"] == []
    conn = storage.connect()
    try:
        row = conn.execute("SELECT started, decision FROM autonomy_pending_actions").fetchone()
        assert row["started"] == 0
        assert row["decision"] == "deny"
    finally:
        conn.close()


@pytest.mark.parametrize("positional", [False, True])
def test_wrapped_merge_preserves_exact_policy_payload_and_charges_rate_once(
    boundary, monkeypatch, positional
):
    from unified_agent import gateway, server
    from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve, get_approval
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "action-review")
    path = boundary["repo"] / "policy.yaml"
    path.write_text(
        "rules:\n  - name: one-merge\n    type: rate_limit\n    tool: git_merge_task\n    max_calls: 1\n    window_seconds: 600\n  - name: signed-merge\n    type: tool\n    tool: git_merge_task\n    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    gateway.reset_limits()
    registered = server._guarded_and_instrumented(git_merge_task)

    def wrapped():
        return registered(17) if positional else registered(task_id=17)

    # The registered wrapper's kwargs omit all defaults. Preserve that exact
    # approved payload rather than requesting another full-default body pass.
    with as_actor(AGENT), pytest.raises(ApprovalPendingError) as policy_pending:
        wrapped()
    policy_id = policy_pending.value.approval_id
    assert get_approval(policy_id)["payload"] == ({} if positional else {"task_id": 17})
    with as_actor(HUMAN):
        approval_resolve(policy_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT), pytest.raises(ApprovalPendingError) as production_pending:
        wrapped()
    production_id = production_pending.value.approval_id
    assert production_id != policy_id
    assert get_approval(policy_id)["status"] == "approved"
    with as_actor(HUMAN):
        approval_resolve(production_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT):
        paused = wrapped()
        assert paused["autonomy"]["decision"] == "pause_for_human"
        # A second pre-execution pause must neither charge the only slot nor
        # spend either exact pass.
        assert wrapped()["autonomy"]["pending_id"] == paused["autonomy"]["pending_id"]
    assert get_approval(policy_id)["status"] == "approved"
    assert get_approval(production_id)["status"] == "approved"
    with as_actor(HUMAN):
        autonomy_modes.review_action(paused["autonomy"]["pending_id"], HUMAN)
    with as_actor(AGENT):
        assert wrapped()["success"]
    assert get_approval(policy_id)["status"] == "consumed"
    assert get_approval(production_id)["status"] == "consumed"
    # Rechecks inside this one execution did not exhaust its single slot;
    # a separate invocation does exceed it.
    with as_actor(AGENT), pytest.raises(ToolError, match="rate|Rate"):
        wrapped()
    gateway.reset_limits()


@pytest.mark.parametrize("failure", ["before-validation", "after-checkout"])
def test_production_pass_restores_only_before_real_execution(boundary, failure):
    from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve, get_approval
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "observe")
    with as_actor(AGENT), pytest.raises(ApprovalPendingError) as pending:
        git_merge_task(17)
    with as_actor(HUMAN):
        approval_resolve(pending.value.approval_id, "approve", resolved_by=HUMAN)
    if failure == "before-validation":
        boundary["validate"].side_effect = ToolError("validation admission stopped")
    else:
        boundary["hooks"]["checkout"] = lambda: (_ for _ in ()).throw(ToolError("checkout failed"))
    with as_actor(AGENT), pytest.raises(ToolError):
        git_merge_task(17)
    expected = "approved" if failure == "before-validation" else "consumed"
    assert get_approval(pending.value.approval_id)["status"] == expected


def test_executing_parent_receipts_do_not_consume_child_pause_approval(boundary):
    from unified_agent.tools import approvals
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "action-review")
    with as_actor(AGENT), pytest.raises(approvals.ApprovalPendingError) as pending:
        git_merge_task(17)
    with as_actor(HUMAN):
        approvals.approval_resolve(pending.value.approval_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT), approvals.approval_invocation():
        approvals.mark_governed_execution()
        paused = git_merge_task(17)
        assert paused["autonomy"]["decision"] == "pause_for_human"
        assert approvals.governed_execution_started()
    assert approvals.get_approval(pending.value.approval_id)["status"] == "approved"


def test_body_cannot_supply_an_opaque_authorizing_context(boundary):
    with as_actor(AGENT), pytest.raises(TypeError):
        merge.orchestrate_merge(
            17, action_context={"mission_id": boundary["mission"], "authorized": True}
        )
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


@pytest.mark.parametrize("positional", [False, True])
def test_wrapped_policy_threshold_uses_actual_bound_arguments(boundary, monkeypatch, positional):
    from unified_agent import server
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "observe")
    path = boundary["repo"] / "policy.yaml"
    path.write_text(
        "rules:\n  - name: deny-task\n    type: threshold\n    tool: git_merge_task\n    field: task_id\n    max: 0\n    action: deny\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    wrapped = server._guarded_and_instrumented(git_merge_task)
    with as_actor(AGENT), pytest.raises(ToolError, match="deny-task"):
        if positional:
            wrapped(17)
        else:
            wrapped(task_id=17)
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


def test_direct_orchestrator_requires_production_approval_before_start(boundary):
    from unified_agent.tools import approvals

    mode(boundary, "observe")
    with as_actor(AGENT), pytest.raises(approvals.ApprovalPendingError) as pending:
        merge.orchestrate_merge(17)
    assert boundary["mutations"] == []
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM autonomy_pending_actions WHERE started=1"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()
    with as_actor(HUMAN):
        approvals.approval_resolve(pending.value.approval_id, "approve", resolved_by=HUMAN)
    with as_actor(AGENT):
        assert merge.orchestrate_merge(17)["success"]
    assert approvals.get_approval(pending.value.approval_id)["status"] == "consumed"


@pytest.mark.parametrize("positional", [False, True])
def test_policy_threshold_sees_defaults_but_approval_keeps_exact_wrapper_payload(
    boundary, monkeypatch, positional
):
    from unified_agent import server
    from unified_agent.tools import approvals
    from unified_agent.tools.git import git_merge_task

    path = boundary["repo"] / "policy.yaml"
    path.write_text(
        "rules:\n  - name: force-ceiling\n    type: threshold\n    tool: git_merge_task\n    field: force\n    max: 0\n    action: deny\n  - name: signed\n    type: tool\n    tool: git_merge_task\n    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    wrapped = server._guarded_and_instrumented(git_merge_task)
    with as_actor(AGENT), pytest.raises(approvals.ApprovalPendingError) as pending:
        if positional:
            wrapped(17)
        else:
            wrapped(task_id=17)
    assert approvals.get_approval(pending.value.approval_id)["payload"] == (
        {} if positional else {"task_id": 17}
    )
    boundary["validate"].assert_not_called()
    assert boundary["mutations"] == []


@pytest.mark.parametrize("change", ["cancel_requested=1", "current_run_id='sp-successor'"])
def test_checkout_holds_task_run_write_fence(boundary, change):
    mode(boundary, "observe")
    blocked = []

    def concurrent_writer():
        conn = storage.connect()
        try:
            conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute(f"UPDATE tasks SET {change} WHERE id=17")
                conn.commit()
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                blocked.append(True)
        finally:
            conn.close()
        assert blocked == [True], "run/cancellation writer committed before the first Git mutation"
        # Exercise the real first Git mutation in this disposable repository.
        subprocess.run(
            ["git", "checkout", "main"], cwd=boundary["repo"], check=True, capture_output=True
        )

    boundary["hooks"]["checkout"] = concurrent_writer
    approve_production()
    with as_actor(AGENT):
        merge.orchestrate_merge(17)
    assert blocked == [True]


def _running_task(boundary):
    from unified_agent import run_records

    conn = storage.connect()
    conn.execute("UPDATE tasks SET status='doing', claimed_by=? WHERE id=17", (AGENT,))
    conn.execute(
        "INSERT INTO delegations(from_agent,to_agent,task_id,task_spec,status,callback,created_at,updated_at) "
        "VALUES (?, ?, 17, 'fixture', 'in_flight', 'none', ?, ?)",
        (HUMAN, AGENT, storage.now(), storage.now()),
    )
    conn.commit()
    conn.close()
    return run_records.record_claim(17, agent=AGENT, claimed_at=storage.now(), prior_status="done")


def test_takeover_after_merge_retains_successor_state_and_separate_exact_merge_evidence(boundary):
    old_run = _running_task(boundary)
    mode(boundary, "observe")

    def takeover():
        conn = storage.connect()
        conn.execute(
            "UPDATE tasks SET current_run_id='sp-successor', current_run_agent='Desktop_Agent1', "
            "claimed_by='Desktop_Agent1', status='doing', notes='successor owns this' WHERE id=17"
        )
        conn.execute("UPDATE delegations SET claim_generation=claim_generation+1 WHERE task_id=17")
        conn.commit()
        conn.close()

    # The merge itself runs under the task write fence, so the successor
    # commits after the merge and before closeout acquires its own fence.
    real_closeout = merge._mission_merge_closeout

    def closeout_after_takeover(*args, **kwargs):
        takeover()
        return real_closeout(*args, **kwargs)

    boundary["monkeypatch"].setattr(merge, "_mission_merge_closeout", closeout_after_takeover)
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    conn = storage.connect()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=17").fetchone()
        assert (task["status"], task["current_run_id"], task["claimed_by"], task["notes"]) == (
            "doing",
            "sp-successor",
            "Desktop_Agent1",
            "successor owns this",
        )
        assert (
            conn.execute("SELECT status FROM delegations WHERE task_id=17").fetchone()[0]
            == "in_flight"
        )
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM event_log WHERE channel='agent.merge.local'"
            ).fetchone()[0]
        )
        assert payload["admitted_run_id"] == old_run
        assert payload["merged_source_head"] == result["merged_source_head"]
        assert payload["task_completed"] is False
    finally:
        conn.close()
    assert result["task_completed"] is False
    merge._worktree_remove_in_repo.assert_not_called()
    merge._close_delegation_ledger.assert_not_called()


def test_source_ref_cannot_advance_between_cleanup_check_and_task_closeout(boundary, monkeypatch):
    mode(boundary, "observe")
    repo = boundary["repo"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    successor = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit-tree",
            tree,
            "-p",
            head,
        ],
        input="successor\n",
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    attempts = []

    def advance_while_cleanup(*args, **kwargs):
        attempts.append(
            subprocess.run(
                ["git", "update-ref", "refs/heads/codex/task-17", successor, head],
                cwd=repo,
                capture_output=True,
                text=True,
            )
        )

    monkeypatch.setattr(merge, "_worktree_remove_in_repo", advance_while_cleanup)
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert len(attempts) == 1 and attempts[0].returncode != 0
    assert "lock" in attempts[0].stderr
    assert result["task_completed"] is True
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        == head
    )


def test_cli_pause_surfaces_exact_review_instead_of_commit_hash_error(
    boundary, monkeypatch, capsys
):
    mode(boundary, "action-review")
    monkeypatch.setattr(
        merge, "parse_args", lambda: SimpleNamespace(task_id=17, force=True, dry_run=False)
    )
    with as_actor(AGENT), pytest.raises(SystemExit) as stopped:
        merge.main()
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert "pause_for_human" in output.out
    assert "pending" in output.out.lower()
    assert "action-review" in output.out
    assert "'commit_hash'" not in output.err
    assert boundary["mutations"] == []


@pytest.mark.parametrize("change", ["revoked", "expired", "payload", "grant", "reconsumed"])
def test_consumed_production_receipt_is_revalidated_after_validation(boundary, change):
    from unified_agent import safety
    from unified_agent.tools import approvals
    from unified_agent.tools.git import git_merge_task

    mode(boundary, "observe")
    approve_production()
    conn = storage.connect()
    approval_id = conn.execute("SELECT id FROM approvals WHERE tool='git_merge_task'").fetchone()[0]
    conn.close()

    def mutate_grant(*_args, **_kwargs):
        if change == "reconsumed":
            approvals.restore_consumed_approval(approval_id)
            with approvals.approval_invocation():
                safety.check_prod_action_gate(
                    "git_merge_task",
                    {"task_id": 17, "force": False, "dry_run": False, "allow_in_flight": False},
                )
            return
        conn = storage.connect()
        if change == "revoked":
            conn.execute("UPDATE approvals SET status='revoked' WHERE id=?", (approval_id,))
        elif change == "expired":
            conn.execute(
                "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (approval_id,),
            )
        elif change == "payload":
            conn.execute("UPDATE approvals SET payload='{}' WHERE id=?", (approval_id,))
        else:
            conn.execute("UPDATE approvals SET resolved_by=? WHERE id=?", (AGENT, approval_id))
        conn.commit()
        conn.close()

    boundary["validate"].side_effect = mutate_grant
    with as_actor(AGENT), pytest.raises(ToolError, match="approval"):
        git_merge_task(17)
    assert boundary["mutations"] == []
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM autonomy_pending_actions WHERE started=1"
            ).fetchone()[0]
            == 0
        )
        if change == "revoked":
            assert (
                conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[
                    0
                ]
                == "revoked"
            )
        elif change == "reconsumed":
            assert (
                conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[
                    0
                ]
                == "consumed"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["cancel_requested=1", "current_run_id='sp-successor'"])
def test_merge_itself_holds_task_run_write_fence(boundary, change):
    mode(boundary, "observe")
    blocked = []

    def concurrent_writer():
        conn = storage.connect()
        try:
            conn.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute(f"UPDATE tasks SET {change} WHERE id=17")
            blocked.append(True)
        finally:
            conn.close()

    boundary["hooks"]["merge"] = concurrent_writer
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert blocked == [True]
    assert result["task_completed"] is True


@pytest.mark.parametrize(
    "change",
    ["cancel_requested=1", "current_run_id='sp-successor'", "claimed_by='Desktop_Agent1'"],
)
def test_authority_change_after_checkout_stops_the_merge(boundary, change):
    mode(boundary, "observe")

    def after_checkout():
        conn = storage.connect()
        conn.execute(f"UPDATE tasks SET {change} WHERE id=17")
        conn.commit()
        conn.close()

    boundary["hooks"]["pull"] = after_checkout
    approve_production()
    with as_actor(AGENT), pytest.raises(ToolError, match="execution admission"):
        merge.orchestrate_merge(17)
    assert [args[0] for args in boundary["mutations"]] == ["checkout", "pull"]
    merge._worktree_remove_in_repo.assert_not_called()


def test_mission_closeout_marks_merged_delegations_reviewed(boundary):
    from unified_agent.tools import delegate

    _running_task(boundary)
    mode(boundary, "observe")
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["task_completed"] is True
    conn = storage.connect()
    try:
        task = conn.execute("SELECT status, notes FROM tasks WHERE id=17").fetchone()
        delegation = conn.execute("SELECT status FROM delegations WHERE task_id=17").fetchone()
    finally:
        conn.close()
    assert task["status"] == "done"
    assert "[review: merged @" in task["notes"]
    assert delegation["status"] == "completed"
    awaiting = delegate.outcomes_awaiting_review()
    assert [item for item in awaiting["outcomes"] if item["task_id"] == 17] == []


def test_merged_review_note_after_durable_closeout_advances_the_revision(boundary):
    """Task #2087: the "[review: merged @ ...]" line is written in a second
    transaction after the closeout committed. It wrote notes without a revision
    bump, so a caller holding the revision read at the durable closeout passed
    expected_updated_at over notes it never saw."""
    from unified_agent.tool_errors import ConflictError
    from unified_agent.tools import tasks

    _running_task(boundary)
    seen = {}

    def remove(*_a, **_k):
        # Runs between the durable closeout commit and the notes write: the
        # revision any reader could hold at that point.
        conn = storage.connect()
        try:
            seen.update(conn.execute("SELECT updated_at, notes FROM tasks WHERE id=17").fetchone())
        finally:
            conn.close()

    boundary["monkeypatch"].setattr(merge, "_worktree_remove_in_repo", remove)
    mode(boundary, "observe")
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["task_completed"] is True
    assert "[review: merged @" not in (seen["notes"] or "")
    conn = storage.connect()
    try:
        task = conn.execute("SELECT updated_at, notes FROM tasks WHERE id=17").fetchone()
    finally:
        conn.close()
    assert "[review: merged @" in task["notes"]
    assert task["updated_at"] != seen["updated_at"]
    with as_actor(AGENT), pytest.raises(ConflictError):
        tasks.task_update(17, append_note="stale writer", expected_updated_at=seen["updated_at"])


def test_worktree_removal_failure_after_durable_closeout_reopens_task(boundary):
    mode(boundary, "observe")
    merge._worktree_remove_in_repo.side_effect = ToolError("worktree is locked")
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert result["task_completed"] is False
    assert "source worktree retained after closeout" in result["cleanup_skipped"]
    assert ["update-ref", "-d"] not in [args[:2] for args in boundary["mutations"]]
    conn = storage.connect()
    try:
        task = conn.execute("SELECT status, completed_at, notes FROM tasks WHERE id=17").fetchone()
        events = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM event_log WHERE channel='agent.merge.local' ORDER BY ROWID"
            )
        ]
    finally:
        conn.close()
    assert (task["status"], task["completed_at"]) == ("waiting_review", None)
    assert "[outstanding: source worktree retained after closeout" in task["notes"]
    assert [event["task_completed"] for event in events] == [True, False]


def test_closeout_evidence_failure_never_removes_the_source_worktree(boundary, monkeypatch):
    mode(boundary, "observe")

    def ledger_down(*_args, **_kwargs):
        raise RuntimeError("event ledger rejected write")

    monkeypatch.setattr(merge, "_record_local_merge", ledger_down)
    approve_production()
    with as_actor(AGENT), pytest.raises(RuntimeError, match="ledger rejected"):
        merge.orchestrate_merge(17)
    merge._worktree_remove_in_repo.assert_not_called()
    conn = storage.connect()
    try:
        assert conn.execute("SELECT status FROM tasks WHERE id=17").fetchone()[0] == "done"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel='agent.merge.local'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_takeover_after_durable_closeout_retains_worktree_and_branch(boundary, monkeypatch):
    mode(boundary, "observe")
    real_connect = storage.connect
    taken_over = []

    class CloseoutBarrier:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            if taken_over:
                return
            event = self.conn.execute(
                "SELECT 1 FROM event_log WHERE channel='agent.merge.local' LIMIT 1"
            ).fetchone()
            if event:
                successor = real_connect()
                successor.execute(
                    "UPDATE tasks SET status='doing', claimed_by='Desktop_Agent1', "
                    "current_run_id='sp-successor' WHERE id=17"
                )
                successor.commit()
                successor.close()
                taken_over.append(True)

    monkeypatch.setattr(storage, "connect", lambda: CloseoutBarrier(real_connect()))
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert taken_over == [True]
    assert result["task_completed"] is False
    merge._worktree_remove_in_repo.assert_not_called()
    assert not any(args[:2] == ["update-ref", "-d"] for args in boundary["mutations"])
    conn = real_connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by, current_run_id FROM tasks WHERE id=17"
        ).fetchone()
        assert tuple(row) == ("doing", "Desktop_Agent1", "sp-successor")
    finally:
        conn.close()


def test_cleanup_and_branch_delete_hold_task_write_fence(boundary):
    mode(boundary, "observe")
    blocked = []

    def concurrent_writer(*_args, **_kwargs):
        conn = storage.connect()
        try:
            conn.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute("UPDATE tasks SET claimed_by='Desktop_Agent1' WHERE id=17")
            blocked.append(True)
        finally:
            conn.close()

    merge._worktree_remove_in_repo.side_effect = concurrent_writer
    boundary["hooks"]["update-ref"] = concurrent_writer
    approve_production()
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17)
    assert result["task_completed"] is True
    assert blocked == [True, True]


def test_failed_cleanup_keeps_delegation_visible_for_review(boundary):
    from unified_agent.tools import delegate

    _running_task(boundary)
    mode(boundary, "observe")
    merge._worktree_remove_in_repo.side_effect = ToolError("worktree is locked")
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["task_completed"] is False
    awaiting = delegate.outcomes_awaiting_review()
    assert [item["task_id"] for item in awaiting["outcomes"]] == [17]
    conn = storage.connect()
    try:
        task = conn.execute("SELECT status, notes FROM tasks WHERE id=17").fetchone()
        assert task["status"] == "waiting_review"
        assert "[review: merged" not in task["notes"]
    finally:
        conn.close()


def test_branch_retention_after_successful_cleanup_preserves_closeout_result(boundary, monkeypatch):
    from unified_agent import run_records

    _running_task(boundary)
    mode(boundary, "observe")
    real_flush = run_records.flush

    def takeover_after_cleanup(tx):
        real_flush(tx)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET status='doing', claimed_by='Desktop_Agent1', "
                "current_run_id='sp-successor' WHERE id=17"
            )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(run_records, "flush", takeover_after_cleanup)
    approve_production(force=True)
    with as_actor(AGENT):
        result = merge.orchestrate_merge(17, force=True)
    assert result["task_completed"] is True
    assert "branch_retained" in result
    assert "cleanup_skipped" not in result
    merge._worktree_remove_in_repo.assert_called_once()
    assert not any(args[:2] == ["update-ref", "-d"] for args in boundary["mutations"])
    conn = storage.connect()
    try:
        task = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=17").fetchone()
        assert tuple(task) == ("doing", "Desktop_Agent1")
        event = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.merge.local'"
        ).fetchone()
        assert json.loads(event[0])["task_completed"] is True
    finally:
        conn.close()


def test_merge_cli_initializes_storage_before_orchestration(monkeypatch):
    calls = []
    monkeypatch.setattr(
        merge, "parse_args", lambda: SimpleNamespace(task_id=17, force=False, dry_run=True)
    )
    monkeypatch.setattr(storage, "init_db", lambda: calls.append("init"))

    def orchestrate(task_id, **kwargs):
        assert calls == ["init"]
        assert task_id == 17
        assert kwargs == {"force": False, "dry_run": True}
        calls.append("orchestrate")
        return {"dry_run": True}

    monkeypatch.setattr(merge, "orchestrate_merge", orchestrate)
    merge.main()
    assert calls == ["init", "orchestrate"]


def test_review_cli_migrates_legacy_store_before_mission_merge(boundary, monkeypatch, capsys):
    from unified_agent import review_cli

    conn = storage.connect()
    try:
        for name in (
            "autonomy_action_reviews",
            "autonomy_standing_grants",
            "autonomy_pending_actions",
            "autonomy_mode_revisions",
            "autonomy_mission_mode",
        ):
            conn.execute(f"DROP TABLE {name}")
        conn.commit()
    finally:
        conn.close()
    seen = []

    def review(task_id, dry_run=False):
        result = merge.orchestrate_merge(task_id, dry_run=dry_run)
        seen.append(result)
        return {"task_id": task_id, "verdict": "validated", "merge_result": result}

    monkeypatch.setattr(review_cli.reviewer, "review_task", review)
    monkeypatch.setattr(review_cli, "append_review_ledger", lambda _: {"appended": 0})
    with as_actor(AGENT):
        assert review_cli.main(["task", "17", "--no-notify", "--json"]) == 0
    assert len(seen) == 1
    assert seen[0]["autonomy"]["decision"] == "pause_for_human"
    assert seen[0]["autonomy"]["mission_id"] == boundary["mission"]
    assert seen[0]["autonomy"]["reason"].startswith("action-review mode:")
    assert boundary["mutations"] == []
    assert json.loads(capsys.readouterr().out)[0]["merge_result"] == seen[0]
