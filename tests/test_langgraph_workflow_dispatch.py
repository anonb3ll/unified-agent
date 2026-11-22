"""Local database acceptance checks for the two-hop workflow receipts."""

from dataclasses import replace

import pytest

pytest.importorskip("langgraph")

from test_langgraph_dispatch import _claimed_task, _run

from unified_agent import config, storage
from unified_agent import langgraph_dispatch as dispatch
from unified_agent.langgraph_pilot import InvalidRunError, StaleAuthorityError
from unified_agent.langgraph_workflow_dispatch import WorkflowDispatchAdapter
from unified_agent.tools.approvals import approval_resolve
from unified_agent.tools.tasks import task_update

PLAN = "a" * 64
VERIFY = "b" * 64


@pytest.fixture
def authenticated_hub(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-langgraph-workflow-session")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def setup(writer=task_update):
    storage.init_db()
    task = _claimed_task()
    run = replace(_run(task), required_capabilities=("task_update",), query="plan then merge")
    adapter = WorkflowDispatchAdapter(
        budget_revision=lambda _: "budget-v1", tool_registry={"task_update": writer}
    )
    return task, run, adapter


def approve(adapter, run, hop, digest):
    pending = adapter.call_hop(run=run, hop=hop, digest=digest)
    assert pending.status == "pending"
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)
    return pending.approval_id


def read_task(run):
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (run.task_id,)).fetchone())
    finally:
        conn.close()


def test_build_then_merge_receipts_are_attributed_and_gated(authenticated_hub):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return task_update(**kwargs)

    task, origin, adapter = setup(writer)
    approve(adapter, origin, "build", PLAN)
    build = adapter.call_hop(run=origin, hop="build", digest=PLAN)
    assert build.status == "allowed"
    assert build.result["hop"] == "build"
    assert build.result["claimed_by"] == origin.claimed_by
    after_build = adapter.refresh_after_hop(origin, "build", PLAN)
    assert after_build.task_updated_at != origin.task_updated_at
    approve(adapter, after_build, "merge", VERIFY)
    merge = adapter.call_hop(run=after_build, hop="merge", digest=VERIFY)
    assert merge.status == "allowed"
    assert merge.result["hop"] == "merge"
    current = read_task(origin)
    assert current["status"] == "doing"
    assert current["claimed_by"] == task["claimed_by"]
    notes = current["notes"]
    assert notes.count("LangGraph workflow build") == 1
    assert notes.count("LangGraph workflow merge") == 1
    assert origin.claimed_by in notes
    assert "status" not in calls[0] and calls[0]["worktree"] is False
    assert adapter.call_hop(run=origin, hop="build", digest=PLAN) == build
    assert adapter.call_hop(run=after_build, hop="merge", digest=VERIFY) == merge
    assert len(calls) == 2


def test_policy_and_adapter_approvals_both_park_then_hop_writes_once(
    authenticated_hub, monkeypatch, tmp_path
):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return task_update(**kwargs)

    _, run, adapter = setup(writer)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "rules:\n  - name: hop_gate\n    type: tool\n    tool: task_update\n"
        "    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))

    policy_pending = adapter.call_hop(run=run, hop="build", digest=PLAN)
    assert policy_pending.status == "pending"
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(policy_pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)

    adapter_pending = adapter.call_hop(run=run, hop="build", digest=PLAN)
    assert adapter_pending.status == "pending"
    assert adapter_pending.approval_id != policy_pending.approval_id
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(adapter_pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)

    assert adapter.call_hop(run=run, hop="build", digest=PLAN).status == "allowed"
    assert len(calls) == 1
    assert read_task(run)["notes"].count("LangGraph workflow build") == 1


def test_foreign_edit_after_build_is_stale_at_merge(authenticated_hub):
    _, origin, adapter = setup()
    approve(adapter, origin, "build", PLAN)
    adapter.call_hop(run=origin, hop="build", digest=PLAN)
    after_build = adapter.refresh_after_hop(origin, "build", PLAN)
    task_update(origin.task_id, append_note="unrelated edit", worktree=False)
    with pytest.raises(StaleAuthorityError, match="changed after"):
        adapter.refresh_after_hop(origin, "build", PLAN)
    with pytest.raises(StaleAuthorityError, match="revision"):
        adapter.call_hop(run=after_build, hop="merge", digest=VERIFY)
    assert "LangGraph workflow merge" not in read_task(origin)["notes"]


def test_merge_denial_is_terminal(authenticated_hub):
    _, origin, adapter = setup()
    approve(adapter, origin, "build", PLAN)
    adapter.call_hop(run=origin, hop="build", digest=PLAN)
    after_build = adapter.refresh_after_hop(origin, "build", PLAN)
    pending = adapter.call_hop(run=after_build, hop="merge", digest=VERIFY)
    approval_resolve(int(pending.approval_id), "deny", resolved_by="Operator", reason="Hold merge")
    denied = adapter.call_hop(run=after_build, hop="merge", digest=VERIFY)
    assert denied.status == "denied"
    assert adapter.reconcile_hop(run=after_build, hop="merge", digest=VERIFY) == denied
    assert "LangGraph workflow merge" not in read_task(origin)["notes"]
    assert "LangGraph workflow build" in read_task(origin)["notes"]


def test_forged_run_cannot_reuse_build_key(authenticated_hub):
    _, origin, adapter = setup()
    approve(adapter, origin, "build", PLAN)
    adapter.call_hop(run=origin, hop="build", digest=PLAN)
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
        adapter.call_hop(run=replace(origin, trace_id="forged"), hop="build", digest=PLAN)


@pytest.mark.parametrize("change", ["identity", "capability", "revision", "digest"])
def test_invalid_authority_cannot_write_build(authenticated_hub, change):
    _, run, adapter = setup()
    token = None
    if change == "identity":
        token = config.agent_identity_var.set("Desktop_Agent1")
    elif change == "capability":
        run = replace(run, required_capabilities=("web_search",))
    elif change == "revision":
        task_update(run.task_id, append_note="changed", worktree=False)
    try:
        with pytest.raises((StaleAuthorityError, InvalidRunError)):
            adapter.call_hop(
                run=run,
                hop="build",
                digest="arbitrary note" if change == "digest" else PLAN,
            )
    finally:
        if token is not None:
            config.agent_identity_var.reset(token)
    assert "LangGraph workflow build" not in read_task(run)["notes"]


def test_revalidate_failure_after_approval_releases_prepared_effect(authenticated_hub, monkeypatch):
    """Pre-write failures after consuming approval must not leave a prepared effect."""
    import sqlite3

    _, origin, adapter = setup()
    pending = adapter.call_hop(run=origin, hop="build", digest=PLAN)
    approval_id = int(pending.approval_id)
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    revalidate_calls = {"count": 0}
    real_revalidate = adapter.revalidate

    def flaky_revalidate(run_arg):
        revalidate_calls["count"] += 1
        if revalidate_calls["count"] > 1:
            raise sqlite3.OperationalError("database is locked")
        return real_revalidate(run_arg)

    monkeypatch.setattr(adapter, "revalidate", flaky_revalidate)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        adapter.call_hop(run=origin, hop="build", digest=PLAN)

    key = adapter._key(origin, "build")
    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    assert approval["status"] == "approved"
    assert effect is None
