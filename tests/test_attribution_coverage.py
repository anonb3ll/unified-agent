"""Tests for Task #745: Attribution coverage across completion paths."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, reviewer, storage
from unified_agent.dispatch import worker
from unified_agent.tools import tasks as tasks_tool
from unified_agent.webui import api as webui_api


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch, isolated_env, workspace_is_product_repo):
    # Claims provision real worktrees. An empty workspace lets git discover an
    # enclosing host repository, so seed a separate repository for each test.
    subprocess.run(
        ["git", "init", "-b", "main", str(isolated_env)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(isolated_env),
            "-c",
            "user.name=Attribution Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "fixture seed",
        ],
        check=True,
        capture_output=True,
    )
    workspace_token = config.workspace_root_var.set(isolated_env)
    try:
        db_file = tmp_path / "memory.db"
        monkeypatch.setattr(config, "db_path", lambda: db_file)
        storage.init_db()
        yield
    finally:
        config.workspace_root_var.reset(workspace_token)


def test_direct_task_update_stamps_caller_when_unclaimed():
    t = tasks_tool.task_add("Unclaimed fast task")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        updated = tasks_tool.task_update(t["id"], status="done")
    finally:
        config.agent_identity_var.reset(token)

    assert updated["status"] == "done"
    assert updated["claimed_by"] == "Desktop_Agent1"


def test_direct_task_update_preserves_existing_claimant(isolated_env):
    t = tasks_tool.task_add("Claimed task")
    # First claim by Desktop_agent3
    claimed = tasks_tool.task_claim(t["id"], agent="Desktop_agent3")
    worktree = (isolated_env / Path(claimed["worktree_path"])).resolve()
    assert worktree.is_relative_to(isolated_env.resolve())
    assert (worktree / ".git").is_file()

    # Complete under a different identity
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        updated = tasks_tool.task_update(t["id"], status="done")
    finally:
        config.agent_identity_var.reset(token)

    assert updated["status"] == "done"
    # Claimant must NOT be overwritten
    assert updated["claimed_by"] == "Desktop_agent3"


def test_grok_completion_stamps_claimed_by_end_to_end():
    t = tasks_tool.task_add("Grok attributed completion")
    tasks_tool.task_claim(t["id"], agent="Desktop_Agent8")
    token = config.agent_identity_var.set("Desktop_Agent8")
    try:
        updated = tasks_tool.task_update(t["id"], status="done")
    finally:
        config.agent_identity_var.reset(token)
    assert updated["status"] == "done"
    assert updated["claimed_by"] == "Desktop_Agent8"
    assert updated["worktree_path"] is None


def test_reviewer_mark_done_stamps_service_identity():
    t1 = tasks_tool.task_add("Reviewer done unclaimed")
    reviewer._mark_done_with_note(t1["id"], "[review: merged-upstream]")
    conn = storage.connect()
    try:
        row1 = conn.execute(
            "SELECT claimed_by, status FROM tasks WHERE id=?", (t1["id"],)
        ).fetchone()
        assert row1["status"] == "done"
        assert row1["claimed_by"] == "review-cron"
    finally:
        conn.close()

    t2 = tasks_tool.task_add("Reviewer done already claimed")
    tasks_tool.task_claim(t2["id"], agent="Desktop_Agent2")
    reviewer._mark_done_with_note(t2["id"], "[review: merged-upstream]")
    conn = storage.connect()
    try:
        row2 = conn.execute(
            "SELECT claimed_by, status FROM tasks WHERE id=?", (t2["id"],)
        ).fetchone()
        assert row2["status"] == "done"
        assert row2["claimed_by"] == "Desktop_Agent2"
    finally:
        conn.close()


def test_dispatch_worker_stamps_service_identity(monkeypatch):
    t = tasks_tool.task_add("Smoke worker task")
    run_id = "smoke-test-run-1"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'Desktop_Agent1', 'launched', 'dispatch-scheduler', ?, ?)",
            (run_id, t["id"], storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()

    res = worker.complete_smoke_run(
        run_id, evidence_ref="fake-evidence.json", append_task_note=False
    )
    assert res["ok"] is True

    done_task = tasks_tool.task_list(status="done")["tasks"][0]
    assert done_task["id"] == t["id"]
    assert done_task["claimed_by"] == "dispatch-smoke"


@pytest.mark.asyncio
async def test_flow_metrics_includes_attribution_coverage(monkeypatch):
    fixed_now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(webui_api, "_board_now", lambda: fixed_now)

    # 1 historical unclaimed done task (preserve the honest NULL)
    t1 = tasks_tool.task_add("Task 1")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
            (fixed_now.isoformat(), fixed_now.isoformat(), t1["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    # 1 claimed done task
    t2 = tasks_tool.task_add("Task 2")
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        tasks_tool.task_update(t2["id"], status="done")
    finally:
        config.agent_identity_var.reset(token)

    payload = await webui_api.flow_metrics_payload()
    cov = payload.get("attribution_coverage")
    assert cov is not None
    assert cov["total_done"] == 2
    assert cov["attributed_done"] == 1
    assert cov["coverage_ratio"] == 0.5


@pytest.mark.asyncio
async def test_flow_metrics_exceeds_active_queue_limit(monkeypatch):
    fixed_now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(webui_api, "_board_now", lambda: fixed_now)

    conn = storage.connect()
    try:
        now_str = fixed_now.isoformat(timespec="seconds")
        rows = [
            (
                f"Bulk Task {i}",
                "done",
                "med",
                "Desktop_agent3" if i % 2 == 0 else None,
                now_str,
                now_str,
                now_str,
            )
            for i in range(520)
        ]
        conn.executemany(
            "INSERT INTO tasks (title, status, priority, claimed_by, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    payload = await webui_api.flow_metrics_payload()
    cov = payload.get("attribution_coverage")
    assert cov is not None
    assert cov["total_done"] == 520
    assert cov["attributed_done"] == 260
    assert cov["coverage_ratio"] == 0.5


def test_task_list_full_fields_capped_below_compact():
    conn = storage.connect()
    try:
        now_str = storage.now()
        rows = [(f"Task {i}", "done", "med", None, now_str, now_str, now_str) for i in range(520)]
        conn.executemany(
            "INSERT INTO tasks (title, status, priority, claimed_by, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    full_res = tasks_tool.task_list(status="done", fields="full", limit=10_000)
    assert len(full_res["tasks"]) == 500

    compact_res = tasks_tool.task_list(status="done", fields="compact", limit=10_000)
    assert len(compact_res["tasks"]) == 520


def test_actor_fallback_stamps_when_unclaimed_and_no_auth():
    t = tasks_tool.task_add("Unclaimed task with actor fallback")
    assert config.verified_agent_identity() is None
    updated = tasks_tool.task_update(t["id"], status="done", actor="dispatch-scheduler")
    assert updated["status"] == "done"
    assert updated["claimed_by"] == "dispatch-scheduler"


def test_verified_identity_cannot_be_spoofed_via_actor():
    t = tasks_tool.task_add("Unclaimed task with spoof attempt")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        updated = tasks_tool.task_update(t["id"], status="done", actor="Desktop_agent3")
    finally:
        config.agent_identity_var.reset(token)

    assert updated["status"] == "done"
    assert updated["claimed_by"] == "Desktop_Agent1"


def test_actor_fallback_preserves_existing_claimant():
    t = tasks_tool.task_add("Pre-claimed task")
    tasks_tool.task_claim(t["id"], agent="Desktop_Agent2")
    assert config.verified_agent_identity() is None
    updated = tasks_tool.task_update(t["id"], status="done", actor="dispatch-scheduler")
    assert updated["status"] == "done"
    assert updated["claimed_by"] == "Desktop_Agent2"


def test_completion_without_attribution_is_fail_closed():
    t1 = tasks_tool.task_add("Task empty actor")
    with pytest.raises(ToolError, match="attribut"):
        tasks_tool.task_update(t1["id"], status="done", actor="   ")
    assert tasks_tool.task_list(status="open")["tasks"][0]["id"] == t1["id"]

    t2 = tasks_tool.task_add("Task none actor")
    with pytest.raises(ToolError, match="attribut"):
        tasks_tool.task_update(t2["id"], status="done", actor=None)
    assert {t["id"] for t in tasks_tool.task_list(status="open")["tasks"]} >= {t1["id"], t2["id"]}


def test_reopening_actor_completed_task_clears_claim_lock():
    t = tasks_tool.task_add("Reopen actor-completed task")
    completed = tasks_tool.task_update(t["id"], status="done", actor="dispatch-scheduler")
    assert completed["claimed_by"] == "dispatch-scheduler"
    assert completed["claimed_at"] is None
    assert completed["lock_expires_at"] is None

    reopened = tasks_tool.task_update(t["id"], status="open")
    assert reopened["claimed_by"] is None
    assert reopened["claimed_at"] is None
    assert reopened["lock_expires_at"] is None
    assert tasks_tool.task_claim(t["id"], agent="Desktop_Agent1")["claimed_by"] == "Desktop_Agent1"


def test_scheduler_record_run_outcome_stamps_attribution():
    from unified_agent.dispatch import scheduler

    t = tasks_tool.task_add("Scheduler outcome task")
    run_id = "sched-test-run-1"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'Desktop_Agent5', 'running', 'dispatch-scheduler', ?, ?)",
            (run_id, t["id"], storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()

    res = scheduler.record_run_outcome(
        run_id,
        "finished",
        actor="dispatch-scheduler",
        evidence_ref="fake-evidence.json",
        append_task_note=False,
    )
    assert res["ok"] is True
    done_task = tasks_tool.task_list(status="done")["tasks"][0]
    assert done_task["id"] == t["id"]
    assert done_task["claimed_by"] == "dispatch-scheduler"


def test_langgraph_record_task_outcome_stamps_attribution(monkeypatch):
    from unified_agent.langgraph_dispatch import (
        DispatchOutcome,
        UnifiedAgentDispatchAdapter,
    )
    from unified_agent.langgraph_pilot import PilotResult, PilotRun

    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    t = tasks_tool.task_add("Langgraph pilot task")
    tasks_tool.task_claim(t["id"], agent="Desktop_Agent2", timeout_seconds=3600)
    conn = storage.connect()
    try:
        trow = conn.execute(
            "SELECT updated_at, lock_expires_at FROM tasks WHERE id=?", (t["id"],)
        ).fetchone()
        updated_at = trow["updated_at"]
        lock_expires_at = trow["lock_expires_at"]
    finally:
        conn.close()

    run = PilotRun(
        run_id="01JQ9Z6Y5K7M2N4P6R8T0V2X4Z",
        task_id=t["id"],
        task_updated_at=updated_at,
        claimed_by="Desktop_Agent2",
        claim_expires_at=lock_expires_at,
        expected_profile="developer",
        required_capabilities=("task_update",),
        budget_revision="rev-1",
        trace_id="tr-1",
        query="test query",
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-lg-session")
    try:
        adapter = UnifiedAgentDispatchAdapter(
            budget_revision=lambda tid: "rev-1",
            tool_registry={"task_update": tasks_tool.task_update},
        )
        pilot_result = PilotResult(
            status="completed",
            output={"summary": "done"},
            duplicate=False,
            recovered=False,
        )
        outcome = DispatchOutcome.completed(pilot_result)
        res = adapter.record_task_outcome(run, outcome)
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(token)

    assert res.get("status") == "done"
    assert res.get("claimed_by") == "Desktop_Agent2"


def test_orchestrate_merge_stamps_attribution(tmp_path):
    from unittest.mock import MagicMock, patch

    from unified_agent import merge

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) "
            "VALUES (999, 'Test Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-999"),
        "branch": "agy/999",
        "head": "abcdef123",
    }

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch(
            "unified_agent.merge._run_audit",
            return_value={
                "root": "/tmp/worktree",
                "files_scanned": 1,
                "status": "pass",
                "summary": {"high": 0, "medium": 0, "low": 0, "info": 0, "excluded": 0},
                "findings": [],
                "truncated": False,
            },
        ),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="All tests passed", stderr=""),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo",
            return_value=MagicMock(returncode=0, stdout="abc1234", stderr=""),
        ),
    ):
        res = merge.orchestrate_merge(999, force=True)

    assert res["success"] is True
    conn = storage.connect()
    try:
        row = conn.execute("SELECT claimed_by, status FROM tasks WHERE id=999").fetchone()
        assert row["status"] == "done"
        assert row["claimed_by"] == "merge-orchestrator"
    finally:
        conn.close()


def test_webui_action_task_stamps_identity(monkeypatch):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server, webui

    t = tasks_tool.task_add("WebUI update task")
    tok = f"tok-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{tok}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        res = client.post(
            "/api/action/task",
            json={"id": t["id"], "status": "done"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "done"
        assert data["claimed_by"] == "Desktop_Person"


def test_dispatch_run_carries_host_into_evidence_and_event(tmp_path, monkeypatch):
    import json

    from unified_agent.dispatch import scheduler

    monkeypatch.setenv("UA_SOURCE_HOST", "TestWorkstation")
    t = tasks_tool.task_add("Dispatch host test task")
    run_id = "dispatch-host-run-1"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'Desktop_Agent2', 'running', 'dispatch-scheduler', ?, ?)",
            (run_id, t["id"], storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()

    res = scheduler.record_run_outcome(
        run_id,
        "finished",
        actor="dispatch-scheduler",
        evidence_ref="fake-evidence.json",
        append_task_note=True,
    )
    assert res["ok"] is True

    # 1. Task notes carry host
    conn = storage.connect()
    try:
        t_row = conn.execute("SELECT notes FROM tasks WHERE id=?", (t["id"],)).fetchone()
        assert "host=TestWorkstation" in (t_row["notes"] or "")

        # 2. Transition event payload carries host
        e_row = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.dispatch.transition' ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
        assert e_row is not None
        payload = json.loads(e_row["payload"])
        assert payload.get("host") == "TestWorkstation"
    finally:
        conn.close()


def test_reviewer_and_merge_handles_empty_string_claimant(tmp_path):
    from unittest.mock import MagicMock, patch

    from unified_agent import merge

    # Reviewer replaces empty string claimant
    t1 = tasks_tool.task_add("Reviewer empty claimant")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by='   ' WHERE id=?", (t1["id"],))
        conn.commit()
    finally:
        conn.close()

    reviewer._mark_done_with_note(t1["id"], "[review: merged-upstream]")
    conn = storage.connect()
    try:
        r1 = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (t1["id"],)).fetchone()
        assert r1["claimed_by"] == "review-cron"
    finally:
        conn.close()

    # Merge replaces empty string claimant
    t2 = tasks_tool.task_add("Merge empty claimant")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing', claimed_by='   ' WHERE id=?",
            (t2["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / f"task-{t2['id']}"),
        "branch": f"task/{t2['id']}-branch",
        "head": "abcdef456",
    }
    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch(
            "unified_agent.merge._run_audit",
            return_value={
                "root": "/tmp/worktree",
                "files_scanned": 1,
                "status": "pass",
                "summary": {"high": 0, "medium": 0, "low": 0, "info": 0, "excluded": 0},
                "findings": [],
                "truncated": False,
            },
        ),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="All tests passed", stderr=""),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo",
            return_value=MagicMock(returncode=0, stdout="abc1234", stderr=""),
        ),
    ):
        res = merge.orchestrate_merge(t2["id"], force=True)
    assert res["success"] is True

    conn = storage.connect()
    try:
        r2 = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (t2["id"],)).fetchone()
        assert r2["claimed_by"] == "merge-orchestrator"
    finally:
        conn.close()
