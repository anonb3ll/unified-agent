"""Tests for the LangGraph MCP tool wrappers."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools.approvals import approval_resolve
from unified_agent.tools.langgraph import (
    langgraph_checkpoints,
    langgraph_progress_note,
    langgraph_run,
    langgraph_workflow_run,
)
from unified_agent.tools.tasks import task_add, task_claim, task_update


def _approve(approval_id: int) -> None:
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _setup_db(tmp_path, monkeypatch):
    data_home = tmp_path / "ua-home"
    monkeypatch.setenv("UA_HOME", str(data_home))
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    config.ensure_dirs()
    storage.init_db()
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-langgraph-session")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_langgraph_run_requires_claimed_doing_task():
    task = task_add("Research task", notes="test research notes")

    # Task is 'open' and unclaimed -> rejected
    with pytest.raises(ToolError, match="must be 'doing'"):
        langgraph_run(task["id"], query="test query")

    # Task in 'doing' but unclaimed -> rejected
    task_update(task["id"], status="doing", worktree=False)
    with pytest.raises(ToolError, match="is unclaimed"):
        langgraph_run(task["id"], query="test query")

    # Non-existent task -> rejected
    with pytest.raises(ToolError, match="not found"):
        langgraph_run(99999, query="test query")

    # Empty query -> rejected
    with pytest.raises(ToolError, match="query must not be empty"):
        langgraph_run(task["id"], query="   ")


def test_langgraph_checkpoints_empty_and_populated(tmp_path):
    # Empty / non-existent DB
    res = langgraph_checkpoints()
    assert res["exists"] is False
    assert res["checkpoints"] == []

    # Valid task execution creating checkpoints
    task = task_add("Valid research", notes="notes")
    claimed = task_claim(task["id"], agent="Desktop_Agent2", timeout_seconds=3600)

    run_res = langgraph_run(claimed["id"], query="autonomous AI agents")
    assert run_res["run_id"]
    assert run_res["thread_id"] == f"ua-lg:{run_res['run_id']}"
    assert run_res["status"] in ("completed", "interrupted")
    assert run_res["events_count"] > 0

    # Query checkpoints
    ckpts = langgraph_checkpoints(run_id=run_res["run_id"])
    assert ckpts["exists"] is True
    assert ckpts["count"] > 0


def test_progress_note_real_approval_resume_and_history(monkeypatch):
    from unified_agent import server
    from unified_agent.langgraph_dispatch import UnifiedAgentDispatchAdapter
    from unified_agent.tools.tasks import task_list

    def no_auto_record(*_args, **_kwargs):
        pytest.fail("note-only graph must never auto-record task completion")

    monkeypatch.setattr(UnifiedAgentDispatchAdapter, "record_task_outcome", no_auto_record)
    task = task_claim(
        task_add("Progress note", notes="private original notes")["id"], "Desktop_Agent2"
    )
    call = server.REGISTERED_TOOLS["langgraph_progress_note"]
    first = call(task_id=task["id"], output_digest="a" * 64)
    assert first["status"] == "interrupted"
    run_id = first["run_id"]
    resumed = call(task_id=task["id"], output_digest="a" * 64, run_id=run_id)
    assert resumed["status"] == "interrupted"
    assert resumed["interrupted_by"]["approval_id"] == first["interrupted_by"]["approval_id"]
    _approve(first["interrupted_by"]["approval_id"])
    result = call(task_id=task["id"], output_digest="a" * 64, run_id=run_id)
    assert result["status"] == "completed"
    after = next(t for t in task_list(status="doing")["tasks"] if t["id"] == task["id"])
    assert after["notes"].count(run_id) == 1
    assert after["notes"].startswith(task["notes"])
    for field in ("status", "claimed_by", "lock_expires_at", "worktree_path", "branch"):
        assert after[field] == task[field]
    assert "private original notes" not in str(result)
    assert "notes" not in result["output"]
    # New wrapper/runner instance, original task revision is now stale. Only
    # the historical receipt can be returned; no additional mutation occurs.
    duplicate = call(task_id=task["id"], output_digest="a" * 64, run_id=run_id)
    assert duplicate["status"] == "completed"
    assert duplicate["output"] == result["output"]
    latest = next(t for t in task_list(status="doing")["tasks"] if t["id"] == task["id"])
    assert latest["updated_at"] == after["updated_at"]


def test_progress_note_resume_cannot_change_target_or_digest():
    task = task_claim(task_add("Pinned note")["id"], "Desktop_Agent2")
    result = langgraph_progress_note(task["id"], "b" * 64)
    for target, digest in ((task["id"] + 1, "b" * 64), (task["id"], "c" * 64)):
        with pytest.raises(ToolError, match="original task or output digest"):
            langgraph_progress_note(target, digest, run_id=result["run_id"])
    task_update(task["id"], append_note="concurrent change", worktree=False)
    with pytest.raises(ToolError, match="revision changed"):
        langgraph_progress_note(task["id"], "b" * 64, run_id=result["run_id"])


def test_progress_note_budget_configuration_is_revalidated(monkeypatch):
    task = task_claim(task_add("Budget pin")["id"], "Desktop_Agent2")
    result = langgraph_progress_note(task["id"], "c" * 64)
    monkeypatch.setenv("UA_AUTONOMY_MAX_TOKENS", "1")
    with pytest.raises(ToolError, match="budget revision changed"):
        langgraph_progress_note(task["id"], "c" * 64, run_id=result["run_id"])


def test_progress_note_rejects_unbounded_inputs_and_missing_authority(monkeypatch):
    for task_id, digest in ((True, "a" * 64), (0, "a" * 64), (1, "free text")):
        with pytest.raises(ToolError):
            langgraph_progress_note(task_id, digest)
    with pytest.raises(ToolError, match="ULID"):
        langgraph_progress_note(1, "a" * 64, run_id="../anything")
    with pytest.raises(TypeError):
        langgraph_progress_note(1, "a" * 64, status="done")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    with pytest.raises(ToolError, match="authenticated HTTP"):
        langgraph_progress_note(1, "a" * 64)


def test_progress_note_resume_requires_original_identity():
    task = task_claim(task_add("Private receipt")["id"], "Desktop_Agent2")
    result = langgraph_progress_note(task["id"], "d" * 64)
    identity = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="identity"):
            langgraph_progress_note(task["id"], "d" * 64, run_id=result["run_id"])
    finally:
        config.agent_identity_var.reset(identity)


def test_workflow_run_two_gates_and_attribution(monkeypatch):
    from unified_agent import server
    from unified_agent.langgraph_dispatch import UnifiedAgentDispatchAdapter
    from unified_agent.tools.tasks import task_list

    def no_auto_record(*_args, **_kwargs):
        pytest.fail("workflow graph must never auto-record task completion")

    def no_git_merge(*_args, **_kwargs):
        pytest.fail("workflow merge hop must not call git_merge_task")

    monkeypatch.setattr(UnifiedAgentDispatchAdapter, "record_task_outcome", no_auto_record)
    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", no_git_merge)
    task = task_claim(
        task_add("Workflow chain", notes="private original notes")["id"], "Desktop_Agent2"
    )
    call = server.REGISTERED_TOOLS["langgraph_workflow_run"]
    query = "plan then verify then merge"
    first = call(task_id=task["id"], query=query)
    assert first["status"] == "interrupted"
    assert first["pending_hop"] == "build"
    assert first["interrupted_by"]["claimed_by"] == "Desktop_Agent2"
    run_id = first["run_id"]
    _approve(first["interrupted_by"]["approval_id"])
    second = call(task_id=task["id"], query=query, run_id=run_id)
    assert second["status"] == "interrupted"
    assert second["pending_hop"] == "merge"
    assert second["interrupted_by"]["hop"] == "merge"
    _approve(second["interrupted_by"]["approval_id"])
    result = call(task_id=task["id"], query=query, run_id=run_id)
    assert result["status"] == "completed"
    assert result["output"]["claimed_by"] == "Desktop_Agent2"
    assert result["output"]["build"]["hop"] == "build"
    assert result["output"]["merge"]["hop"] == "merge"
    after = next(t for t in task_list(status="doing")["tasks"] if t["id"] == task["id"])
    assert after["notes"].count("LangGraph workflow build") == 1
    assert after["notes"].count("LangGraph workflow merge") == 1
    assert after["notes"].count("Desktop_Agent2") >= 2
    assert after["notes"].startswith(task["notes"])
    for field in ("status", "claimed_by", "lock_expires_at", "worktree_path", "branch"):
        assert after[field] == task[field]
    assert "private original notes" not in str(result)
    duplicate = call(task_id=task["id"], query=query, run_id=run_id)
    assert duplicate["status"] == "completed"
    assert duplicate["output"] == result["output"]
    latest = next(t for t in task_list(status="doing")["tasks"] if t["id"] == task["id"])
    assert latest["updated_at"] == after["updated_at"]


def test_workflow_run_rejects_unbounded_inputs_and_resume_mismatch():
    task = task_claim(task_add("Pinned workflow")["id"], "Desktop_Agent2")
    result = langgraph_workflow_run(task["id"], "bounded plan")
    with pytest.raises(ToolError, match="conflicts"):
        langgraph_workflow_run(task["id"] + 1, "bounded plan", run_id=result["run_id"])
    with pytest.raises(ToolError, match="conflicts"):
        langgraph_workflow_run(task["id"], "other plan", run_id=result["run_id"])
    with pytest.raises(ToolError, match="ULID"):
        langgraph_workflow_run(task["id"], "bounded plan", run_id="../anything")
    with pytest.raises(ToolError, match="empty"):
        langgraph_workflow_run(task["id"], "   ")


# --- research run resume from a validated checkpoint (task #2522) -----------------------


@pytest.fixture
def fake_search(monkeypatch):
    """Replace the registry's web_search; record calls; optionally block inside one."""
    import threading

    from unified_agent import langgraph_dispatch

    state = {"calls": 0, "entered": threading.Event(), "release": None}

    def web_search(query: str, max_results: int = 5):
        state["calls"] += 1
        state["entered"].set()
        if state["release"] is not None:
            assert state["release"].wait(10)
        return {"items": [{"title": "Result", "url": "https://example.test/r"}]}

    real = langgraph_dispatch._default_tool_registry
    monkeypatch.setattr(
        langgraph_dispatch, "_default_tool_registry", lambda: {**real(), "web_search": web_search}
    )
    return state


def _paused_research_run(title: str = "Research resume") -> dict:
    task = task_claim(task_add(title)["id"], "Desktop_Agent2")
    first = langgraph_run(task["id"], query="durable graph checkpoints")
    assert first["status"] == "interrupted"
    assert first["outcome"] == "approval_interrupted"
    return first


def test_research_run_resumes_from_its_checkpoint_and_produces_output(fake_search):
    from unified_agent import server

    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    call = server.REGISTERED_TOOLS["langgraph_run"]
    resumed = call(run_id=first["run_id"], resume=True)
    assert resumed["outcome"] == "run_completed" and resumed["status"] == "completed"
    assert resumed["output"]["evidence_digest"]
    assert resumed["resumed"] is True and fake_search["calls"] == 1
    for kept in ("run_id", "thread_id", "task_id", "trace_id", "budget_revision"):
        assert resumed[kept] == first[kept]
    # A repeat reads the stored outcome: no second execution.
    again = call(run_id=first["run_id"], resume=True)
    assert again["outcome"] == "run_completed" and again["duplicate"] is True
    assert again["output"] == resumed["output"] and fake_search["calls"] == 1


def test_research_resume_by_a_non_owner_is_run_denied(fake_search):
    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    identity = config.agent_identity_var.set("Desktop_Agent1")
    try:
        denied = langgraph_run(run_id=first["run_id"], resume=True)
    finally:
        config.agent_identity_var.reset(identity)
    assert denied["outcome"] == "run_denied" and denied["code"] == "not_run_owner"
    assert denied["output"] is None and fake_search["calls"] == 0


def test_research_resume_of_a_missing_or_corrupt_checkpoint_is_run_failed(fake_search):
    import sqlite3

    from unified_agent.langgraph_pilot import default_checkpoint_path

    missing = langgraph_run(run_id="01JQ9Z6Y5K7M2N4P6R8T0V2X4Z", resume=True)
    assert missing["outcome"] == "run_failed" and missing["code"] == "checkpoint_missing"
    first = _paused_research_run()
    with sqlite3.connect(default_checkpoint_path()) as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint=? WHERE thread_id=?",
            (b"\xc1junk", first["thread_id"]),
        )
    corrupt = langgraph_run(run_id=first["run_id"], resume=True)
    assert corrupt["outcome"] == "run_failed" and corrupt["code"] == "checkpoint_corrupt"
    assert corrupt["output"] is None and fake_search["calls"] == 0
    with pytest.raises(ToolError, match="resume=True"):
        langgraph_run(run_id=first["run_id"])
    with pytest.raises(ToolError, match="run_id"):
        langgraph_run(resume=True)


def test_research_resume_refuses_a_checkpoint_from_another_graph_revision(fake_search, monkeypatch):
    from unified_agent.tools import langgraph as langgraph_tools

    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    monkeypatch.setattr(langgraph_tools, "_DEFAULT_PLANNER_DIGEST", "f" * 64)
    refused = langgraph_run(run_id=first["run_id"], resume=True)
    assert refused["outcome"] == "run_failed" and refused["code"] == "checkpoint_incompatible"
    assert fake_search["calls"] == 0


def test_research_resume_carries_the_budget_and_never_refreshes_it(fake_search, monkeypatch):
    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    monkeypatch.setenv("UA_AUTONOMY_MAX_TOKENS", "1")  # the hub budget changed mid-pause
    refused = langgraph_run(run_id=first["run_id"], resume=True)
    assert refused["outcome"] == "run_denied" and "budget" in refused["reason"]
    assert refused["budget_revision"] == first["budget_revision"]
    assert fake_search["calls"] == 0


def test_two_concurrent_research_resumes_execute_once(fake_search):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    # A live lease held elsewhere (another hub process) refuses the resume outright.
    _hold_lease_elsewhere(first["run_id"])
    held = langgraph_run(run_id=first["run_id"], resume=True)
    _expire_lease(first["run_id"])
    assert held["outcome"] == "run_conflict" and held["code"] == "resume_in_flight"
    assert fake_search["calls"] == 0

    fake_search["release"] = threading.Event()

    def resume():
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("test-langgraph-session")
        try:
            return langgraph_run(run_id=first["run_id"], resume=True)
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(resume)
        assert fake_search["entered"].wait(10)
        second = pool.submit(resume).result(timeout=10)
        fake_search["release"].set()
        done = running.result(timeout=10)
    assert second["outcome"] == "run_conflict" and second["code"] == "resume_in_flight"
    assert done["outcome"] == "run_completed" and fake_search["calls"] == 1


def _hold_lease_elsewhere(run_id: str) -> None:
    import sqlite3

    from unified_agent.langgraph_pilot import default_checkpoint_path

    with sqlite3.connect(default_checkpoint_path()) as conn:
        conn.execute(
            "INSERT INTO langgraph_resume_leases (thread_id, org_id, run_id, holder, token, "
            "acquired_at, expires_at) VALUES (?, NULL, ?, 'other', 'elsewhere', "
            "'2026-01-01T00:00:00.000000+00:00', '2999-01-01T00:00:00.000000+00:00')",
            (f"ua-lg:{run_id}", run_id),
        )


def _expire_lease(run_id: str) -> None:
    import sqlite3

    from unified_agent.langgraph_pilot import default_checkpoint_path

    with sqlite3.connect(default_checkpoint_path()) as conn:
        changed = conn.execute(
            "UPDATE langgraph_resume_leases SET expires_at='2000-01-01T00:00:00.000000+00:00' "
            "WHERE thread_id=?",
            (f"ua-lg:{run_id}",),
        ).rowcount
    assert changed == 1


def _lease_token(run_id: str) -> str | None:
    import sqlite3

    from unified_agent.langgraph_pilot import default_checkpoint_path

    with sqlite3.connect(default_checkpoint_path()) as conn:
        row = conn.execute(
            "SELECT token FROM langgraph_resume_leases WHERE thread_id=?", (f"ua-lg:{run_id}",)
        ).fetchone()
    return row[0] if row else None


def test_a_holder_that_lost_its_lease_cannot_record_a_completion(fake_search, monkeypatch):
    """Item 3 of the #1540 review: an expired holder is fenced at its next write."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from unified_agent import langgraph_resume
    from unified_agent.tools.tasks import task_list

    monkeypatch.setattr(langgraph_resume, "HEARTBEAT_S", 3600.0)  # the holder stalls
    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    fake_search["release"] = threading.Event()

    def resume():
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("test-langgraph-session")
        try:
            return langgraph_run(run_id=first["run_id"], resume=True)
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale = pool.submit(resume)
        assert fake_search["entered"].wait(10)
        stale_token = _lease_token(first["run_id"])
        _expire_lease(first["run_id"])
        takeover = pool.submit(resume)
        deadline = time.monotonic() + 10
        while _lease_token(first["run_id"]) == stale_token:
            assert time.monotonic() < deadline, "the second resume never took the lease"
            time.sleep(0.02)
        fake_search["release"].set()
        results = [stale.result(timeout=20), takeover.result(timeout=20)]
    stale_result, new_result = results
    assert stale_result["outcome"] == "run_conflict" and stale_result["code"] == "lease_lost"
    assert stale_result["output"] is None
    assert new_result["outcome"] == "run_completed" and new_result["duplicate"] is False
    assert fake_search["calls"] == 1
    task = next(t for t in task_list(status="done")["tasks"] if t["id"] == first["task_id"])
    assert task["notes"].count(f"[langgraph-run: {first['run_id']}]") == 1  # one completion


def test_resume_lease_heartbeat_renews_until_the_lease_is_lost(monkeypatch):
    import sqlite3
    import time

    from unified_agent import langgraph_resume
    from unified_agent.langgraph_dispatch import UnifiedAgentDispatchAdapter
    from unified_agent.langgraph_pilot import PilotRunner, default_checkpoint_path
    from unified_agent.tools.langgraph import DefaultResearchPlanner

    monkeypatch.setattr(langgraph_resume, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(langgraph_resume, "LEASE_TTL_S", 0.4)
    run_id = "01JQ9Z6Y5K7M2N4P6R8T0V2X4Z"
    adapter = UnifiedAgentDispatchAdapter(budget_revision=lambda _t: "b")
    with PilotRunner(
        default_checkpoint_path(), planner=DefaultResearchPlanner(), gateway=adapter
    ) as runner:
        with langgraph_resume.resume_lease(runner, run_id, holder="Desktop_Agent2"):
            time.sleep(0.8)  # twice the TTL: alive only because it was renewed
            with pytest.raises(langgraph_resume.ResumeInFlightError):
                with langgraph_resume.resume_lease(runner, run_id, holder="Desktop_Agent1"):
                    pass
            with sqlite3.connect(default_checkpoint_path()) as conn:
                conn.execute("UPDATE langgraph_resume_leases SET token='taken-over'")
            time.sleep(0.8)  # the heartbeat stops renewing a lease it lost
            with sqlite3.connect(default_checkpoint_path()) as conn:
                (expires,) = conn.execute(
                    "SELECT expires_at FROM langgraph_resume_leases"
                ).fetchone()
            assert expires < langgraph_resume._now()


def test_research_replay_checks_the_tasks_tenant_first(fake_search):
    """Item 2 of the #1540 review: no replay of a run whose task left the tenant."""
    first = _paused_research_run()
    _approve(int(first["interrupted_by"]["approval_id"]))
    done = langgraph_run(run_id=first["run_id"], resume=True)
    assert done["outcome"] == "run_completed"
    conn = storage.connect(scoped=False)
    try:
        conn.execute("UPDATE tasks SET org_id='org_elsewhere' WHERE id=?", (first["task_id"],))
        conn.commit()
    finally:
        conn.close()
    replay = langgraph_run(run_id=first["run_id"], resume=True)
    assert replay["outcome"] == "run_denied" and replay["code"] == "task_not_accessible"
    assert replay["output"] is None and replay["events"] == []
