"""Configured approvals cover manager reads as well as writes (task #1934)."""

from contextlib import contextmanager

import conftest
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import (
    config,
    goals,
    profiles,
    self_organization,
    server,
    skills_registry,
    storage,
    task_comments,
)
from unified_agent.tools import approvals

MANAGERS = ("goal_manage", "task_comment", "skill_registry", "org_proposal")


@pytest.fixture(scope="session", autouse=True)
def host_repo_unchanged():
    # These synthetic adapters never invoke Git or create a worktree.
    yield


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, tmp_path_factory, monkeypatch, _store_templates):
    monkeypatch.setattr(config, "load_gemini_env", lambda: None)
    monkeypatch.setattr(profiles, "current", lambda: profiles.CHIEF_OF_STAFF)
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing-policy.yaml"))
    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
    yield from conftest.isolated_env.__wrapped__(
        tmp_path=tmp_path,
        tmp_path_factory=tmp_path_factory,
        monkeypatch=monkeypatch,
        bootstrap_test_store=True,
        _store_templates=_store_templates,
    )


@contextmanager
def identity(label):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def approval_rows():
    conn = storage.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM approvals ORDER BY id")]
    finally:
        conn.close()


def manager_call(monkeypatch, name, read_only, positional):
    # Keep the real registered wrapper and native action dispatcher. Replace
    # only its final backend with a synthetic effect, never a live authority.
    specs = {
        "goal_manage": (goals, "goal_list", "goal_add", "add", {"title": "inert"}),
        "task_comment": (
            task_comments,
            "list_comments",
            "append_comment",
            "append",
            {"task_id": 1, "body": "inert"},
        ),
        "skill_registry": (
            skills_registry,
            "list_skills",
            "assign",
            "assign",
            {"name": "inert", "target": "inert"},
        ),
        "org_proposal": (
            self_organization,
            "list_org_proposals",
            "review_org_proposal",
            "review",
            {"proposal_id": "inert"},
        ),
    }
    module, reader, writer, write_action, write_params = specs[name]
    effects = []

    def backend(*args, **kwargs):
        effects.append((args, kwargs))
        return {"executed": True}

    monkeypatch.setattr(module, reader if read_only else writer, backend)
    action = " LIST " if read_only else write_action
    params = {"task_id": 1} if read_only and name == "task_comment" else None
    if not read_only:
        params = write_params
    arguments = {"action": action}
    if params is not None:
        arguments["params"] = params
    wrapped = server.REGISTERED_TOOLS[name]

    def call():
        return wrapped(*arguments.values()) if positional else wrapped(**arguments)

    return call, effects


@pytest.mark.parametrize("name", MANAGERS)
@pytest.mark.parametrize("read_only", [True, False], ids=["read", "write"])
@pytest.mark.parametrize("positional", [True, False], ids=["positional", "kwargs"])
@pytest.mark.parametrize("policy_gate", [True, False], ids=["policy-and-config", "config-only"])
def test_configured_manager_calls_spend_one_pass(
    monkeypatch, tmp_path, name, read_only, positional, policy_gate
):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", name)
    if policy_gate:
        policy = tmp_path / "manager-policy.yaml"
        policy.write_text(
            f"rules:\n  - name: manager_gate\n    type: tool\n    tool: {name}\n"
            "    action: require_approval\n"
        )
        monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    call, effects = manager_call(monkeypatch, name, read_only, positional)
    with identity("Desktop_Agent2"), pytest.raises(approvals.ApprovalPendingError) as parked:
        call()
    assert effects == []
    [row] = approval_rows()
    assert row["id"] == parked.value.approval_id
    with identity("Desktop_Person"):
        approvals.approval_resolve(row["id"], "approve")
    with identity("Desktop_Agent2"):
        assert call() == {"executed": True}
    assert len(effects) == 1
    [row] = approval_rows()
    assert row["status"] == "consumed"
    with identity("Desktop_Agent2"), pytest.raises(approvals.ApprovalPendingError):
        call()
    assert len(effects) == 1


@pytest.mark.parametrize("name", MANAGERS)
def test_unconfigured_manager_read_runs_without_approval(monkeypatch, name):
    call, effects = manager_call(monkeypatch, name, True, False)
    with identity("Desktop_Agent2"):
        assert call() == {"executed": True}
    assert len(effects) == 1
    assert approval_rows() == []


@pytest.mark.parametrize("name", MANAGERS)
def test_denied_manager_read_never_runs_backend(monkeypatch, name):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", name)
    call, effects = manager_call(monkeypatch, name, True, False)
    with identity("Desktop_Agent2"), pytest.raises(approvals.ApprovalPendingError) as parked:
        call()
    with identity("Desktop_Person"):
        approvals.approval_resolve(parked.value.approval_id, "deny")
    with identity("Desktop_Agent2"), pytest.raises(ToolError):
        call()
    assert effects == []


@pytest.mark.parametrize(
    "name,action,module,backend_name,params",
    [
        ("goal_manage", "get", goals, "get_goal", {"goal_id": 1}),
        ("task_comment", "get", task_comments, "get_comment", {"comment_id": 1}),
        ("skill_registry", "get", skills_registry, "get", {"name": "inert"}),
        (
            "skill_registry",
            "resolve",
            skills_registry,
            "resolve",
            {"agent_label": "inert"},
        ),
        (
            "skill_registry",
            "drift",
            skills_registry,
            "drift",
            {"surface_dir": "inert"},
        ),
        ("org_proposal", "get", self_organization, "get_org_proposal", {"proposal_id": "inert"}),
    ],
)
@pytest.mark.parametrize("positional", [False, True], ids=["kwargs", "positional"])
@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_non_list_manager_actions_keep_the_read_route(
    monkeypatch, name, action, module, backend_name, params, positional, configured
):
    if configured:
        monkeypatch.setenv("UA_APPROVAL_TOOLS", name)
    effects = []
    read_routes = []
    real_trust_check = server.trust_presets.check_call

    def backend(*args, **kwargs):
        effects.append((args, kwargs))
        return {"executed": True}

    def trust_check(tool, *, read_only, payload, **kwargs):
        # Policy checks trust again with its loaded _config. Record the
        # wrapper's classification so a write-route regression cannot hide
        # behind the policy layer independently recognizing this read.
        if "_config" not in kwargs:
            read_routes.append((tool, read_only))
        return real_trust_check(tool, read_only=read_only, payload=payload, **kwargs)

    monkeypatch.setattr(module, backend_name, backend)
    monkeypatch.setattr(server.trust_presets, "check_call", trust_check)
    wrapped = server.REGISTERED_TOOLS[name]

    def call():
        return wrapped(action, params) if positional else wrapped(action=action, params=params)

    if configured:
        with identity("Desktop_Agent2"), pytest.raises(approvals.ApprovalPendingError) as parked:
            call()
        assert effects == []
        with identity("Desktop_Person"):
            approvals.approval_resolve(parked.value.approval_id, "approve")
    with identity("Desktop_Agent2"):
        expected = {"executed": True}
        assert call() == ({"proposal": expected} if name == "org_proposal" else expected)
    assert effects and len(effects) == 1
    assert read_routes == [(name, True)] * (2 if configured else 1)
    rows = approval_rows()
    assert len(rows) == (1 if configured else 0)
    if configured:
        assert rows[0]["status"] == "consumed"
