"""Gateway call policy: visibility stays on the profile, permission is per call.

Task #1357. Defaults do not hide or deny a tool. A configured rate limit is
per tool and per caller, deny beats allow, and a deny is an evidence event.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest

from unified_agent import (
    config,
    config_revisions,
    events,
    gateway,
    policy,
    profiles,
    safety,
    storage,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    gateway.reset_limits()
    return policy_path


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@contextlib.contextmanager
def _verified(agent: str):
    token = config.agent_identity_var.set(agent)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _scope(agent: str, **kwargs) -> gateway.CallScope:
    return gateway.CallScope(agent=agent, profile=kwargs.pop("profile", "developer"), **kwargs)


def test_unconfigured_policy_allows_the_call_and_does_not_hide_the_tool(_isolated):
    assert profiles.tool_is_allowed_for(profiles.DEVELOPER, "task_list") is True
    assert profiles.tool_is_allowed_for(profiles.DEVELOPER, "config_revision_manage") is False
    policy.evaluate_policy("task_list", payload={}, originator="Desktop_Agent4")
    assert profiles.tool_is_allowed_for(profiles.DEVELOPER, "task_list") is True
    assert gateway.bucket_count() == 0


def test_rate_rule_binding_edit_resets_only_edited_bucket(_isolated):
    def policy_text(project: str, *, stable: bool = True) -> str:
        other = (
            "  - name: stable_cap\n    type: rate_limit\n    tool: web_fetch\n"
            "    max_calls: 1\n    window_seconds: 60\n"
            if stable
            else ""
        )
        return (
            "rules:\n  - name: scoped_cap\n    type: rate_limit\n    tool: web_search\n"
            f"    bind:\n      project: {project}\n"
            "    max_calls: 1\n    window_seconds: 60\n" + other
        )

    scope = _scope(
        "Desktop_Agent4",
        claims=frozenset(
            {
                gateway.ClaimBind(project="Alpha", task="1"),
                gateway.ClaimBind(project="Beta", task="2"),
            }
        ),
    )
    _write(_isolated, policy_text("Alpha"))
    policy.evaluate_policy("web_search", now=1, call_scope=scope)
    policy.evaluate_policy("web_fetch", now=1, call_scope=scope)

    _write(_isolated, policy_text("Beta"))
    policy.evaluate_policy("web_search", now=2, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_fetch", now=2, call_scope=scope)
    assert gateway.bucket_count() == 2

    _write(_isolated, policy_text("Beta", stable=False))
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=3, call_scope=scope)
    assert gateway.bucket_count() == 1


def test_explicit_policy_cannot_erase_or_share_live_rate_budget(_isolated):
    _write(
        _isolated,
        "rules:\n  - name: search_cap\n    type: rate_limit\n    tool: web_search\n"
        "    max_calls: 1\n    window_seconds: 60\n",
    )
    explicit = policy.load_policy()
    scope = _scope("Desktop_Agent2")

    policy.evaluate_policy("web_search", now=1, call_scope=scope)
    policy.evaluate_policy("web_search", now=1, call_scope=scope, policy_config=explicit)
    assert gateway.bucket_count() == 2
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=2, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=2, call_scope=scope, policy_config=explicit)
    assert gateway.bucket_count() == 2


def test_rate_limit_is_per_caller_and_per_tool_and_expires(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    cursor = _scope("Desktop_Agent4")
    codex = _scope("Desktop_Agent2")
    policy.evaluate_policy("web_search", originator="Desktop_Agent4", now=1_000, call_scope=cursor)
    with pytest.raises(policy.PolicyViolation, match="rate limited") as exc:
        policy.evaluate_policy(
            "web_search", originator="Desktop_Agent4", now=1_010, call_scope=cursor
        )
    assert exc.value.reason_code == "rate_limited"
    policy.evaluate_policy("web_search", originator="Desktop_Agent2", now=1_010, call_scope=codex)
    policy.evaluate_policy("web_fetch", originator="Desktop_Agent4", now=1_010, call_scope=cursor)
    policy.evaluate_policy("web_search", originator="Desktop_Agent4", now=1_060, call_scope=cursor)


def test_deny_beats_allow_and_a_deny_does_not_spend_the_rate_budget(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: allow_search
    type: tool
    tool: web_search
    action: allow
  - name: deny_search
    type: tool
    tool: web_search
    action: deny
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with pytest.raises(policy.PolicyViolation, match="denied by policy rule") as exc:
        policy.evaluate_policy("web_search", now=100, call_scope=scope)
    assert exc.value.reason_code == "policy_deny"
    _write(
        _isolated,
        """
rules:
  - name: allow_search
    type: tool
    tool: web_search
    action: allow
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    policy.evaluate_policy("web_search", now=100, call_scope=scope)
    with pytest.raises(policy.PolicyViolation) as limited:
        policy.evaluate_policy("web_search", now=101, call_scope=scope)
    assert limited.value.reason_code == "rate_limited"


def test_agent_and_project_bindings_do_not_apply_to_other_callers(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: cursor_search
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
    bind:
      agent: Desktop_Agent4
      project: unified-agent
""",
    )
    outsider = _scope("Desktop_Agent2", projects=frozenset({"unified-agent"}))
    for tick in range(3):
        policy.evaluate_policy("web_search", now=tick, call_scope=outsider)
    other_project = _scope("Desktop_Agent4", projects=frozenset({"browser"}))
    for tick in range(3):
        policy.evaluate_policy("web_search", now=tick, call_scope=other_project)
    bound = _scope("Desktop_Agent4", projects=frozenset({"unified-agent"}))
    policy.evaluate_policy("web_search", now=10, call_scope=bound)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=11, call_scope=bound)


def test_claim_binding_uses_the_callers_doing_task(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: project_shell
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
    bind:
      project: unified-agent
      company: acme
""",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, project, org_id, created_at, updated_at) "
            "VALUES ('bound', 'doing', 'Desktop_Agent4', 'unified-agent', 'acme', 't', 't')"
        )
        conn.commit()
        task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    finally:
        conn.close()
    with _verified("Desktop_Agent4"):
        policy.evaluate_policy("run_cmd", now=50)
        with pytest.raises(policy.PolicyViolation) as exc:
            policy.evaluate_policy("run_cmd", now=51)
    assert exc.value.reason_code == "rate_limited"
    with _verified("Desktop_Agent2"):
        policy.evaluate_policy("run_cmd", now=51)
    _write(
        _isolated,
        f"""
rules:
  - name: one_task
    type: rate_limit
    tool: fs_read
    max_calls: 1
    window_seconds: 60
    bind:
      task: "{task_id}"
""",
    )
    gateway.reset_limits()
    with _verified("Desktop_Agent4"):
        policy.evaluate_policy("fs_read", now=1)
        with pytest.raises(policy.PolicyViolation, match="rate limited"):
            policy.evaluate_policy("fs_read", now=2)


def test_deny_writes_evidence_without_counting_as_a_tool_call(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_*
    max_calls: 1
    window_seconds: 60
""",
    )
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        safety.check_policy_gate("web_search", payload={}, episode_id="ep-gw")
        with pytest.raises(policy.PolicyViolation) as exc:
            safety.check_policy_gate("web_search", payload={}, episode_id="ep-gw")
    finally:
        config.agent_identity_var.reset(token)
    assert exc.value.reason_code == "rate_limited"
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel = ?",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    finally:
        conn.close()
    assert rows
    payload = json.loads(rows[-1]["payload"])
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == "rate_limited"
    assert payload["rule"] == "search_cap"
    assert payload["tool_name"] == "web_search"
    assert "tool" not in payload
    chain = events.verify_event_chain()
    assert chain["status"] == "valid"
    assert chain["verified"] is True


def test_rate_limit_bounds_and_bind_shape_fail_closed(_isolated):
    cases = [
        "max_calls: 0\n    window_seconds: 60",
        "max_calls: 1001\n    window_seconds: 60",
        "max_calls: 1\n    window_seconds: 0",
        "max_calls: 1\n    window_seconds: 3601",
        "max_calls: 1\n    window_seconds: 60\n    action: allow",
        "max_calls: 1\n    window_seconds: 60\n    bind:\n      trust: low",
    ]
    for body in cases:
        _write(
            _isolated,
            f"""
rules:
  - name: bad
    type: rate_limit
    tool: web_search
    {body}
""",
        )
        with pytest.raises(policy.PolicyViolation):
            policy.load_policy()


def test_full_bucket_table_refuses_instead_of_dropping_a_live_budget(monkeypatch):
    monkeypatch.setattr(gateway, "MAX_BUCKETS", 1)
    gateway.reset_limits()
    a = (("rule", "web_search", "A"), 1, 60.0)
    b = (("rule", "web_search", "B"), 1, 60.0)
    assert gateway._LIMITS.admit([a], 1.0) == (None, False)
    assert gateway._LIMITS.admit([b], 2.0) == (0, True)
    assert gateway._LIMITS.admit([a], 3.0) == (0, False)
    assert gateway._LIMITS.admit([b], 61.0) == (None, False)
    assert gateway.bucket_count() == 1


def test_capacity_refusal_is_a_recorded_deny(_isolated, monkeypatch):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 5
    window_seconds: 60
""",
    )
    monkeypatch.setattr(gateway, "MAX_BUCKETS", 1)
    policy.evaluate_policy("web_search", now=1, call_scope=_scope("Desktop_Agent4"))
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("web_search", now=2, call_scope=_scope("Desktop_Agent2"))
    assert exc.value.reason_code == "rate_limit_capacity"


def test_nested_gate_in_one_invocation_is_charged_once(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: ops_cap
    type: rate_limit
    tool: gcloud_ops
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with gateway.invocation("gcloud_ops"):
        policy.evaluate_policy("gcloud_ops", now=1, call_scope=scope)
        with gateway.invocation("gcloud_ops"):
            policy.evaluate_policy("gcloud_ops", now=1, call_scope=scope)
    with (
        gateway.invocation("gcloud_ops"),
        pytest.raises(policy.PolicyViolation, match="rate limited"),
    ):
        policy.evaluate_policy("gcloud_ops", now=2, call_scope=scope)


def test_parked_later_step_keeps_the_workflow_rate_slot(_isolated):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: workflow_cap
    type: rate_limit
    tool: workflow_run
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with pytest.raises(approvals.ApprovalPendingError):
        with gateway.invocation("workflow_run"):
            policy.evaluate_policy("workflow_run", now=1, call_scope=scope)
            with gateway.invocation("task_add"):
                policy.evaluate_policy("task_add", now=1, call_scope=scope)
            with gateway.invocation("run_cmd"):
                raise approvals.ApprovalPendingError("second step parked")
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("workflow_run", now=2, call_scope=scope)


def test_caught_pending_before_progress_refunds_the_workflow_slot(_isolated):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: workflow_cap
    type: rate_limit
    tool: workflow_run
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with gateway.invocation("workflow_run"):
        policy.evaluate_policy("workflow_run", now=1, call_scope=scope)
        try:
            with gateway.invocation("task_add"):
                raise approvals.ApprovalPendingError("first step parked")
        except approvals.ApprovalPendingError:
            gateway.release_unprogressed_invocation()
    assert gateway.bucket_count() == 0
    with gateway.invocation("workflow_run"):
        policy.evaluate_policy("workflow_run", now=2, call_scope=scope)
        with gateway.invocation("task_add"):
            policy.evaluate_policy("task_add", now=2, call_scope=scope)
        try:
            with gateway.invocation("run_cmd"):
                raise approvals.ApprovalPendingError("later step parked")
        except approvals.ApprovalPendingError:
            gateway.release_unprogressed_invocation()
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("workflow_run", now=3, call_scope=scope)


def test_approval_before_any_step_refunds_the_workflow_slot(_isolated):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: workflow_cap
    type: rate_limit
    tool: workflow_run
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with pytest.raises(approvals.ApprovalPendingError), gateway.invocation("workflow_run"):
        policy.evaluate_policy("workflow_run", now=1, call_scope=scope)
        raise approvals.ApprovalPendingError("parked before steps")
    assert gateway.bucket_count() == 0
    with gateway.invocation("workflow_run"):
        policy.evaluate_policy("workflow_run", now=2, call_scope=scope)


def test_each_workflow_step_is_charged_as_its_own_invocation(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with gateway.invocation("workflow_run"):
        with gateway.invocation("web_search"):
            policy.evaluate_policy("web_search", now=1, call_scope=scope)
        with (
            gateway.invocation("web_search"),
            pytest.raises(policy.PolicyViolation, match="rate limited"),
        ):
            policy.evaluate_policy("web_search", now=2, call_scope=scope)


def test_uncommitted_invocation_can_refund_and_committed_keeps_the_slot(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with gateway.invocation("web_search") as call:
        policy.evaluate_policy("web_search", now=1, call_scope=scope)
        call.refund()
    with gateway.invocation("web_search") as call:
        policy.evaluate_policy("web_search", now=2, call_scope=scope)
        call.commit()
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=3, call_scope=scope)


def test_blank_project_claim_matches_a_restrictive_project_limit(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: project_search
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
    bind:
      project: unified-agent
""",
    )
    blank = _scope(
        "Desktop_Agent4",
        claims=frozenset({gateway.ClaimBind(project="", task="4")}),
    )
    policy.evaluate_policy("web_search", now=1, call_scope=blank)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=2, call_scope=blank)
    elsewhere = _scope(
        "Desktop_Agent2",
        claims=frozenset({gateway.ClaimBind(project="browser", company="", task="9")}),
    )
    for tick in range(3):
        policy.evaluate_policy("web_search", now=10 + tick, call_scope=elsewhere)


def test_known_other_project_misses_a_two_field_restrictive_bind(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: acme_search
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
    bind:
      project: unified-agent
      company: acme
""",
    )
    outsider = _scope(
        "Desktop_Agent4",
        claims=frozenset({gateway.ClaimBind(project="browser", company="", task="9")}),
    )
    for tick in range(3):
        policy.evaluate_policy("web_search", now=tick, call_scope=outsider)
    blank = _scope(
        "Desktop_Agent2",
        claims=frozenset({gateway.ClaimBind(project="", company="", task="4")}),
    )
    policy.evaluate_policy("web_search", now=10, call_scope=blank)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=11, call_scope=blank)


def test_restrictive_project_rule_applies_to_an_unclaimed_caller(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: no_prod_deploy
    type: tool
    tool: gcloud_ops
    action: deny
    bind:
      project: unified-agent
""",
    )
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("gcloud_ops", call_scope=_scope("Desktop_Agent4"))
    assert exc.value.reason_code == "policy_deny"
    elsewhere = _scope("Desktop_Agent4", projects=frozenset({"browser"}))
    policy.evaluate_policy("gcloud_ops", call_scope=elsewhere)


def test_bound_allow_does_not_open_default_deny_for_an_unclaimed_caller(_isolated):
    _write(
        _isolated,
        """
default_action: deny
rules:
  - name: project_reads
    type: tool
    tool: fs_read
    action: allow
    bind:
      project: unified-agent
""",
    )
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("fs_read", call_scope=_scope("Desktop_Agent4"))
    assert exc.value.reason_code == "default_deny"
    inside = _scope("Desktop_Agent4", projects=frozenset({"unified-agent"}))
    policy.evaluate_policy("fs_read", call_scope=inside)


def test_approval_parked_inside_the_tool_body_refunds_the_slot(_isolated):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: shell_cap
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    for tick in range(3):
        with pytest.raises(approvals.ApprovalPendingError), gateway.invocation():
            policy.evaluate_policy("run_cmd", now=tick, call_scope=scope)
            raise approvals.ApprovalPendingError("parked in tool body")
    assert gateway.bucket_count() == 0
    with gateway.invocation():
        policy.evaluate_policy("run_cmd", now=10, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"), gateway.invocation():
        policy.evaluate_policy("run_cmd", now=11, call_scope=scope)


def test_denied_approval_refunds_the_rate_slot(_isolated):
    from mcp.server.mcpserver.exceptions import ToolError

    _write(
        _isolated,
        """
rules:
  - name: shell_cap
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    denied = ToolError("approval request 9 for run_cmd was DENIED: no. Do not retry")
    for tick in range(3):
        with pytest.raises(ToolError), gateway.invocation():
            policy.evaluate_policy("run_cmd", now=tick, call_scope=scope)
            raise denied
    assert gateway.bucket_count() == 0
    with gateway.invocation():
        policy.evaluate_policy("run_cmd", now=10, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"), gateway.invocation():
        policy.evaluate_policy("run_cmd", now=11, call_scope=scope)


def test_blocked_call_keeps_a_sibling_budget(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 2
    window_seconds: 60
  - name: shell_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    policy.evaluate_policy("web_search", now=1, call_scope=scope)
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 2
    window_seconds: 30
  - name: shell_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=2, call_scope=scope)
    _write(
        _isolated,
        """
rules:
      - name: search_cap
        type: rate_limit
        tool: web_search
        max_calls: 2
        window_seconds: 60
""",
    )
    policy.evaluate_policy("web_search", now=3, call_scope=scope)
    # The rule was edited twice, so its previous bucket generation is gone;
    # the call at t=3 starts the restored rule's budget.
    policy.evaluate_policy("web_search", now=4, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=5, call_scope=scope)


def test_project_bound_role_allow_list_stays_inside_that_project(_isolated):
    _write(
        _isolated,
        """
default_action: allow
rules:
  - name: project_stewards
    type: role
    tool: approval_resolve
    action: allow
    roles: [steward]
    bind:
      project: unified-agent
""",
    )
    outsider = _scope(
        "Desktop_Agent4",
        projects=frozenset({"browser"}),
        claims=frozenset({gateway.ClaimBind(project="browser", task="9")}),
    )
    policy.evaluate_policy("approval_resolve", call_scope=outsider)
    insider = _scope(
        "Desktop_Agent4",
        projects=frozenset({"unified-agent"}),
        claims=frozenset({gateway.ClaimBind(project="unified-agent", task="3")}),
    )
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("approval_resolve", call_scope=insider)
    assert exc.value.reason_code == "role_denied"


def test_bound_role_allow_does_not_admit_an_unclaimed_caller(_isolated):
    _write(
        _isolated,
        """
default_action: deny
rules:
  - name: project_developers
    type: role
    tool: fs_read
    action: allow
    roles: [developer]
    bind:
      project: unified-agent
""",
    )
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("fs_read", call_scope=_scope("Desktop_Agent4"))
    assert exc.value.reason_code == "default_deny"
    inside = _scope("Desktop_Agent4", projects=frozenset({"unified-agent"}))
    policy.evaluate_policy("fs_read", call_scope=inside)


def test_adapter_originator_cannot_satisfy_a_role(_isolated):
    _write(
        _isolated,
        """
default_action: deny
rules:
  - name: cursor_only
    type: role
    tool: fs_read
    action: allow
    roles: [Desktop_Agent4]
""",
    )
    with _verified("Desktop_Agent2"):
        with pytest.raises(policy.PolicyViolation) as exc:
            policy.evaluate_policy(
                "fs_read",
                originator="Desktop_Agent4",
                call_scope=_scope("Desktop_Agent2"),
            )
        assert exc.value.reason_code == "role_denied"
    with _verified("Desktop_Agent4"):
        policy.evaluate_policy(
            "fs_read",
            originator="Desktop_Agent2",
            call_scope=_scope("Desktop_Agent4"),
        )


def test_other_errors_keep_the_slot(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: shell_cap
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with pytest.raises(RuntimeError), gateway.invocation():
        policy.evaluate_policy("run_cmd", now=1, call_scope=scope)
        raise RuntimeError("tool ran and failed")
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("run_cmd", now=2, call_scope=scope)


def test_reclaiming_a_needed_expired_bucket_still_respects_the_cap(monkeypatch):
    monkeypatch.setattr(gateway, "MAX_BUCKETS", 1)
    gateway.reset_limits()
    a = (("a", "web_search", "X"), 5, 60.0)
    b = (("b", "web_search", "X"), 5, 60.0)
    assert gateway._LIMITS.admit([a], 1.0) == (None, False)
    assert gateway._LIMITS.admit([a, b], 100.0)[1] is True
    assert gateway.bucket_count() <= 1


def test_sessionless_unverified_callers_are_keyed_by_mcp_session(_isolated, monkeypatch):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    monkeypatch.setattr(config, "current_mcp_session_id", lambda: "s-one")
    policy.evaluate_policy("web_search", now=1)
    monkeypatch.setattr(config, "current_mcp_session_id", lambda: "s-two")
    policy.evaluate_policy("web_search", now=2)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=3)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", originator="rotated-label", now=4)


def test_rate_denial_does_not_consume_an_approval_pass(_isolated, monkeypatch):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: gate_search
    type: tool
    tool: web_search
    action: require_approval
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )
    passes: list[str] = []
    monkeypatch.setattr(
        approvals, "check_approval_gate", lambda tool, *_a, **_k: passes.append(tool)
    )
    scope = _scope("Desktop_Agent4")
    policy.evaluate_policy("web_search", now=1, call_scope=scope)
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("web_search", now=2, call_scope=scope)
    assert exc.value.reason_code == "rate_limited"
    assert passes == ["web_search"]


def test_parked_approval_refunds_the_rate_slot(_isolated, monkeypatch):
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: gate_search
    type: tool
    tool: web_search
    action: require_approval
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
""",
    )

    def _park(*_a, **_k):
        raise approvals.ApprovalPendingError("parked")

    monkeypatch.setattr(approvals, "check_approval_gate", _park)
    scope = _scope("Desktop_Agent4")
    for tick in range(3):
        with pytest.raises(approvals.ApprovalPendingError):
            policy.evaluate_policy("web_search", now=tick, call_scope=scope)
    assert gateway.bucket_count() == 0


def test_duplicate_rate_limit_names_are_rejected(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: cap
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
  - name: cap
    type: rate_limit
    tool: web_*
    max_calls: 5
    window_seconds: 60
""",
    )
    with pytest.raises(policy.PolicyViolation, match="more than one rate_limit"):
        policy.load_policy()


def test_expired_claim_is_not_in_binding_scope(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: project_shell
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
    bind:
      project: unified-agent
""",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, project, lock_expires_at, "
            "created_at, updated_at) VALUES ('stale', 'doing', 'Desktop_Agent4', "
            "'unified-agent', '2000-01-01T00:00:00+00:00', 't', 't')"
        )
        conn.execute(
            "INSERT INTO tasks (title, status, claimed_by, project, lock_expires_at, "
            "created_at, updated_at) VALUES ('live', 'doing', 'Desktop_Agent4', "
            "'browser', '2999-01-01T00:00:00+00:00', 't', 't')"
        )
        conn.commit()
    finally:
        conn.close()
    with _verified("Desktop_Agent4"):
        for tick in range(3):
            policy.evaluate_policy("run_cmd", now=tick)
    assert gateway.bucket_count() == 0


def test_adapter_originator_cannot_select_an_agent_binding(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: cursor_search
    type: rate_limit
    tool: web_search
    max_calls: 1
    window_seconds: 60
    bind:
      agent: Desktop_Agent4
""",
    )
    for tick in range(3):
        policy.evaluate_policy("web_search", originator="Desktop_Agent4", now=tick)
    with _verified("Desktop_Agent2"):
        for tick in range(3):
            policy.evaluate_policy("web_search", originator="Desktop_Agent4", now=10 + tick)
    assert gateway.bucket_count() == 0
    with _verified("Desktop_Agent4"):
        policy.evaluate_policy("web_search", originator="someone-else", now=20)
        with pytest.raises(policy.PolicyViolation, match="rate limited"):
            policy.evaluate_policy("web_search", originator="someone-else", now=21)


def test_policy_commit_revisions_a_rate_limit_rule(_isolated):
    document = {
        "version": 1,
        "enabled": True,
        "default_action": "allow",
        "rules": [
            {
                "name": "search_cap",
                "type": "rate_limit",
                "tool": "web_search",
                "max_calls": 2,
                "window_seconds": 60,
                "bind": {"agent": "Desktop_Agent4"},
            }
        ],
    }
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        committed = config_revisions.commit("policy", document)
    finally:
        config.agent_identity_var.reset(token)
    assert committed["unchanged"] is False
    listed = config_revisions.list_revisions("policy")
    assert listed["revisions"][0]["revision"] == committed["revision"]
    shown = config_revisions.show_revision("policy", committed["revision"])
    assert shown["document"]["rules"][0]["type"] == "rate_limit"
    loaded = policy.load_policy()
    assert loaded.rules[0].type == "rate_limit"
    assert loaded.rules[0].bind == {"agent": "Desktop_Agent4"}
    chain = events.verify_event_chain()
    assert chain["verified"] is True


def test_default_deny_is_not_an_allow_from_a_rate_rule(_isolated):
    _write(
        _isolated,
        """
default_action: deny
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 2
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    with pytest.raises(policy.PolicyViolation) as exc:
        policy.evaluate_policy("web_search", now=1, call_scope=scope)
    assert exc.value.reason_code == "default_deny"
    assert gateway.bucket_count() == 0


def test_edited_rate_rule_starts_with_its_new_budget(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 2
    window_seconds: 60
""",
    )
    scope = _scope("Desktop_Agent4")
    policy.evaluate_policy("web_search", now=1, call_scope=scope)
    policy.evaluate_policy("web_search", now=2, call_scope=scope)
    _write(
        _isolated,
        """
rules:
  - name: search_cap
    type: rate_limit
    tool: web_search
    max_calls: 3
    window_seconds: 60
""",
    )
    for tick in (3, 4, 5):
        policy.evaluate_policy("web_search", now=tick, call_scope=scope)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("web_search", now=6, call_scope=scope)


def test_multi_key_bind_requires_one_claim_not_a_union(_isolated):
    _write(
        _isolated,
        """
rules:
  - name: mixed_shell
    type: rate_limit
    tool: run_cmd
    max_calls: 1
    window_seconds: 60
    bind:
      project: Alpha
      company: South
""",
    )
    mixed = _scope(
        "Desktop_Agent4",
        companies=frozenset({"North", "South"}),
        projects=frozenset({"Alpha", "Beta"}),
        claims=frozenset(
            {
                gateway.ClaimBind(company="North", project="Alpha", task="1"),
                gateway.ClaimBind(company="South", project="Beta", task="2"),
            }
        ),
    )
    for tick in range(3):
        policy.evaluate_policy("run_cmd", now=tick, call_scope=mixed)
    assert gateway.bucket_count() == 0
    both = _scope(
        "Desktop_Agent4",
        companies=frozenset({"South"}),
        projects=frozenset({"Alpha"}),
        claims=frozenset({gateway.ClaimBind(company="South", project="Alpha", task="3")}),
    )
    policy.evaluate_policy("run_cmd", now=10, call_scope=both)
    with pytest.raises(policy.PolicyViolation, match="rate limited"):
        policy.evaluate_policy("run_cmd", now=11, call_scope=both)


def test_goal_manage_wrapper_does_not_spend_approval_when_rate_limited(_isolated, monkeypatch):
    from unified_agent import server
    from unified_agent.tools import approvals

    _write(
        _isolated,
        """
rules:
  - name: goal_cap
    type: rate_limit
    tool: goal_manage
    max_calls: 1
    window_seconds: 60
""",
    )

    def dummy(action, params=None):
        return {"ok": True}

    dummy.__name__ = "goal_manage"
    wrapped = server._guarded_and_instrumented(dummy)
    consumed = []

    def _watch(*_args, **_kwargs):
        consumed.append(1)
        return {"approval_id": 99}

    monkeypatch.setattr(approvals, "check_approval_gate", _watch)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: True)
    from unified_agent import agent_access

    monkeypatch.setattr(agent_access, "enforce", lambda *_a, **_k: None)
    with _verified("Desktop_Agent4"):
        assert wrapped("create", {"title": "a"}) == {"ok": True}
        assert consumed == [1]
        with pytest.raises(policy.PolicyViolation) as exc:
            wrapped("create", {"title": "b"})
    assert exc.value.reason_code == "rate_limited"
    assert consumed == [1]


def test_generic_wrapper_keeps_policy_pass_spent_after_bare_entry(_isolated, monkeypatch):
    from unified_agent import agent_access, server
    from unified_agent.tools import approvals

    _write(
        _isolated,
        "rules:\n  - name: governed_shell\n    type: tool\n    tool: run_cmd\n"
        "    action: require_approval\n",
    )
    monkeypatch.setattr(agent_access, "enforce", lambda *_a, **_k: None)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: True)
    executed = []

    def dummy(*, cmd):
        approvals.check_approval_gate("inner_shell_gate", {"cmd": cmd}, force=True)
        executed.append(cmd)
        return {"ok": True}

    dummy.__name__ = "run_cmd"
    wrapped = server._guarded_and_instrumented(dummy)
    with _verified("Desktop_Agent2"):
        with pytest.raises(approvals.ApprovalPendingError) as request:
            approvals.check_approval_gate("run_cmd", {"cmd": "echo test"}, force=True)
        approval_id = request.value.approval_id
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE approvals SET status='approved', resolved_at=? WHERE id=?",
                (storage.now(), approval_id),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(approvals.ApprovalPendingError):
            wrapped(cmd="echo test")

    assert not executed
    # Bare entry is the execution boundary; an unknown inner gate cannot prove
    # that no write occurred, even when this particular stub did not mutate.
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_registered_calls_record_one_reason_coded_decision_each(_isolated, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import agent_access, server

    monkeypatch.setattr(agent_access, "enforce", lambda *_a, **_k: None)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: True)
    # One row per outcome; a read-only allow would otherwise be held for the
    # hourly rollup (task #1922, tests/test_gateway_coalesce.py).
    monkeypatch.setenv("UA_GATEWAY_COALESCE_READS", "0")

    def allowed():
        return {"ok": True}

    allowed.__name__ = "task_list"
    with _verified("Desktop_Agent4"):
        assert server._guarded_and_instrumented(allowed)() == {"ok": True}

    def broken():
        raise ValueError("private argument must not be logged")

    broken.__name__ = "task_get"
    with _verified("Desktop_Agent4"):
        with pytest.raises(ValueError, match="private argument"):
            server._guarded_and_instrumented(broken)()

    def parked_workflow():
        return {"status": "pending-approval", "approval_id": 17}

    parked_workflow.__name__ = "workflow_run"
    with _verified("Desktop_Agent4"):
        assert server._guarded_and_instrumented(parked_workflow)()["approval_id"] == 17

    _write(
        _isolated,
        "rules:\n  - name: deny_task\n    type: tool\n    tool: task_list\n    action: deny\n",
    )
    with _verified("Desktop_Agent4"):
        with pytest.raises(policy.PolicyViolation):
            server._guarded_and_instrumented(allowed)()

    monkeypatch.setattr(profiles, "tool_is_allowed", lambda _name: False)
    with _verified("Desktop_Agent4"):
        with pytest.raises(ToolError, match="not exposed"):
            server._guarded_and_instrumented(broken)()

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY rowid",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    finally:
        conn.close()
    decisions = [json.loads(row["payload"]) for row in rows]
    assert [(d["decision"], d["reason_code"]) for d in decisions] == [
        ("allow", "allowed"),
        ("error", "tool_error"),
        ("pending", "approval_pending"),
        ("deny", "policy_deny"),
        ("deny", "profile_denied"),
    ]
    assert all("private argument" not in json.dumps(d) for d in decisions)
    assert events.verify_event_chain()["status"] == "valid"


@pytest.mark.parametrize(
    "expiry,active",
    [
        ("2026-09-30T04:00:01+00:00", True),
        ("2026-09-30T04:00:00+00:00", False),
        ("2026-09-30T03:59:59+00:00", False),
        (None, True),
    ],
)
def test_policy_claim_binding_uses_strict_active_expiry(monkeypatch, expiry, active):
    now = "2026-09-30T04:00:00+00:00"
    monkeypatch.setattr(storage, "now", lambda: now)
    conn = storage.connect()
    try:
        task_id = conn.execute(
            "INSERT INTO tasks(title,status,claimed_by,project,lock_expires_at,created_at,updated_at) "
            "VALUES('bound','doing','Desktop_Agent2','unified-agent',?,?,?)",
            (expiry, now, now),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    with _verified("Desktop_Agent2"):
        scope = gateway.resolve_scope(None, need_claims=True)
    bind = {"task": str(task_id), "project": "unified-agent"}
    assert gateway.binding_matches(bind, scope) is active
    # Expiry never lets restrictive rules fail open just because a claim vanished.
    assert gateway.binding_matches(bind, scope, unclaimed_matches=True)


def test_policy_unavailable_denial_carries_the_active_episode(_isolated, monkeypatch):
    """Task #2309: the claim-lookup failure path stamps the run's episode."""

    def broken(_agent):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gateway, "resolve_claims", broken)
    token = config.episode_id_var.set("acp:nd-00000000000000aa")
    try:
        with _verified("developer-agent"), pytest.raises(policy.PolicyViolation) as exc:
            gateway.resolve_scope(None, need_claims=True)
    finally:
        config.episode_id_var.reset(token)
    assert exc.value.reason_code == "policy_unavailable"
    conn = storage.connect()
    try:
        (row,) = conn.execute(
            "SELECT payload, episode_id FROM event_log WHERE channel = ?",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    finally:
        conn.close()
    assert row["episode_id"] == "acp:nd-00000000000000aa"
    assert json.loads(row["payload"])["reason_code"] == "policy_unavailable"
