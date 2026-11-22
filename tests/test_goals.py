"""Goals are a why-chain, not the dependency DAG (task #1349)."""

from __future__ import annotations

import secrets
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import profiles, server, storage
from unified_agent.goals import (
    EDGE_ANCESTRY,
    append_why_chain,
    goal_add,
    goal_id_for_task,
    goal_manage,
    link_task,
    why_chain,
)
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.planning_loop import decompose_into_goal_linked_tasks
from unified_agent.tools.tasks import task_add
from unified_agent.webui.routes import web_routes


def test_ancestry_is_not_a_dependency(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    root = goal_add("Serve the company", author="Desktop_Agent4")
    child = goal_add(
        "Share a why-chain",
        parent_goal_id=root["goal"]["id"],
        author="Desktop_Agent4",
    )
    task = task_add(title="Inject the chain", notes="spec stays in notes")
    linked = link_task(task["id"], child["goal"]["id"], author="Desktop_Agent4")
    chain = why_chain(task["id"])
    assert [item["id"] for item in chain["chain"]] == [root["goal"]["id"], child["goal"]["id"]]
    assert chain["edge"] == EDGE_ANCESTRY
    assert "dependency" in chain["meaning"]
    conn = storage.connect()
    try:
        deps = conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()["n"]
    finally:
        conn.close()
    assert deps == 0
    assert linked["dependency_rows_before"] == linked["dependency_rows_after"] == 0
    packet = build_session_packet(task_id=task["id"], agent="Desktop_Agent4")
    assert packet["why_chain"]["goal_id"] == child["goal"]["id"]
    prompt = append_why_chain("do the task", task["id"])
    assert "Why-chain" in prompt
    assert "not a dependency" in prompt
    assert append_why_chain("plain", task_add(title="unlinked")["id"]) == "plain"
    assert append_why_chain("plain", 623) == "plain"
    with pytest.raises(ToolError, match="unknown task"):
        why_chain(623)


def test_relink_to_a_different_goal_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task = task_add(title="one goal")
    first = goal_add("First", author="Desktop_Agent4")
    second = goal_add("Second", author="Desktop_Agent4")
    link_task(task["id"], first["goal"]["id"], author="Desktop_Agent4")
    try:
        link_task(task["id"], second["goal"]["id"], author="Desktop_Agent4")
    except Exception as exc:
        assert "single-valued" in str(exc)
    else:
        raise AssertionError("relink should fail")


def test_decompose_links_children_without_touching_dependencies(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    epic = task_add(
        title="Epic",
        notes="[project-spec]\n\nDoD:\n- Ship the why-chain\n",
    )
    goal = goal_add("Parity goals", author="Desktop_Agent4")
    result = decompose_into_goal_linked_tasks(
        goal["goal"]["id"], epic["id"], author="Desktop_Agent4"
    )
    assert len(result["created"]) == 1
    child_id = result["created"][0]["task_id"]
    assert why_chain(child_id)["goal_id"] == goal["goal"]["id"]
    assert why_chain(epic["id"])["goal_id"] == goal["goal"]["id"]
    again = decompose_into_goal_linked_tasks(
        goal["goal"]["id"], epic["id"], author="Desktop_Agent4"
    )
    assert again["created"] == []
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()["n"] == 0
        parent = conn.execute("SELECT parent_id FROM tasks WHERE id=?", (child_id,)).fetchone()
    finally:
        conn.close()
    assert parent["parent_id"] == epic["id"]


def test_goal_routes_have_no_delete(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    methods = {
        route.path: set(route.methods or ()) for route in web_routes() if "goal" in route.path
    }
    assert methods["/api/goals"] <= {"GET", "POST", "HEAD"}
    assert "DELETE" not in methods["/api/goals"]
    assert methods["/api/goals/{goal_id}"] <= {"GET", "HEAD"}
    assert goal_manage("list", {})["goals"] == []


def test_cross_project_parent_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    parent = goal_add("Parent", project="alpha", author="Desktop_Agent4")
    with pytest.raises(ToolError, match="same project"):
        goal_add(
            "Child",
            parent_goal_id=parent["goal"]["id"],
            project="beta",
            author="Desktop_Agent4",
        )


def test_deleted_task_drops_its_goal_link(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    goal = goal_add("Keep", author="Desktop_Agent4")
    task = task_add(title="ephemeral")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
        conn.commit()
        left = conn.execute("SELECT COUNT(*) AS n FROM task_goal_links").fetchone()["n"]
    finally:
        conn.close()
    assert left == 0
    with pytest.raises(ToolError, match="unknown task"):
        why_chain(task["id"])


def test_decompose_refuses_when_the_epic_serves_another_goal(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    epic = task_add(title="Epic", notes="[project-spec]\n\nDoD:\n- Ship the why-chain\n")
    serving = goal_add("Already", author="Desktop_Agent4")
    other = goal_add("Other", author="Desktop_Agent4")
    link_task(epic["id"], serving["goal"]["id"], author="Desktop_Agent4")
    before = storage.connect()
    try:
        count = before.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        before.close()
    with pytest.raises(ToolError, match="already serves"):
        decompose_into_goal_linked_tasks(other["goal"]["id"], epic["id"], author="Desktop_Agent4")
    after = storage.connect()
    try:
        assert after.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == count
    finally:
        after.close()


def test_truncation_keeps_the_linked_goal():
    from unified_agent.lifecycle.packet import _bound_packet

    packet = {
        "why_chain": {
            "goal_id": 2,
            "chain": [{"id": 1, "title": "root " * 4000}, {"id": 2, "title": "leaf"}],
        },
        "recent_events": [{"ulid": "x" * 100}],
    }
    bound = _bound_packet(packet)
    assert bound["why_chain"]["chain"][-1]["id"] == 2
    assert bound["why_chain"]["goal_id"] == 2


def test_goal_post_uses_the_authenticated_identity(monkeypatch, tmp_path):
    token = f"tok-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent4:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        missing = client.post("/api/goals", json={"action": "add", "title": "Nope"})
        assert missing.status_code == 401
        spoofed = client.post(
            "/api/goals",
            headers={"Authorization": f"Bearer {token}"},
            json={"action": "add", "title": "Owned", "author": "Desktop_Agent2"},
        )
        assert spoofed.status_code == 400
        created = client.post(
            "/api/goals",
            headers={"Authorization": f"Bearer {token}"},
            json={"action": "add", "title": "Owned"},
        )
    assert created.status_code == 201
    assert created.json()["goal"]["created_by"] == "Desktop_Agent4"


def test_cross_project_task_link_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task = task_add(title="local", project="browser")
    goal = goal_add("Elsewhere", project="unified-agent", author="Desktop_Agent4")
    with pytest.raises(ToolError, match="same project"):
        link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    assert goal_id_for_task(task["id"]) is None


def test_lost_link_race_reports_the_stored_goal(monkeypatch, tmp_path):
    """ON CONFLICT DO NOTHING must not report the caller's goal when another won."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task = task_add(title="contended")
    first = goal_add("First", author="Desktop_Agent4")
    second = goal_add("Second", author="Desktop_Agent4")
    link_task(task["id"], first["goal"]["id"], author="Desktop_Agent4")
    lookups = {"n": 0}
    real_connect = storage.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        original = conn.execute

        def execute(sql, *a, **kw):
            if (
                isinstance(sql, str)
                and "FROM task_goal_links WHERE task_id=?" in sql
                and sql.lstrip().upper().startswith("SELECT")
            ):
                lookups["n"] += 1
                if lookups["n"] == 1:

                    class _Miss:
                        def fetchone(self):
                            return None

                    return _Miss()
            return original(sql, *a, **kw)

        class _Conn:
            def execute(self, sql, *a, **kw):
                return execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(conn, name)

        return _Conn()

    monkeypatch.setattr(storage, "connect", connect)
    with pytest.raises(ToolError, match=f"serves goal {first['goal']['id']}"):
        link_task(task["id"], second["goal"]["id"], author="Desktop_Agent4")
    assert goal_id_for_task(task["id"]) == first["goal"]["id"]


def test_decompose_refuses_a_different_project(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    epic = task_add(
        title="Epic",
        notes="[project-spec]\n\nDoD:\n- Ship the why-chain\n",
        project="browser",
    )
    goal = goal_add("Other project", project="unified-agent", author="Desktop_Agent4")
    before = storage.connect()
    try:
        count = before.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        before.close()
    with pytest.raises(ToolError, match="same project"):
        decompose_into_goal_linked_tasks(goal["goal"]["id"], epic["id"], author="Desktop_Agent4")
    after = storage.connect()
    try:
        assert after.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == count
    finally:
        after.close()


def test_resume_packet_redacts_goal_text(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    goal = goal_add(
        "token=supersecretvalue",
        statement="api_key=abcdefghij",
        author="Desktop_Agent4",
    )
    task = task_add(title="linked")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    packet = build_session_packet(task_id=task["id"], agent="Desktop_Agent4")
    leaf = packet["why_chain"]["chain"][-1]
    assert "supersecretvalue" not in leaf["title"]
    assert "abcdefghij" not in leaf["statement"]
    assert "[REDACTED_SECRET]" in leaf["title"]
    assert "[REDACTED_SECRET]" in leaf["statement"]


def test_goal_reads_require_the_task_list_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    goal = goal_add("Visible", author="Desktop_Agent4")
    task = task_add(title="linked")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        allowed = client.get("/api/goals")
        assert allowed.status_code == 200
        assert allowed.json()["tree"]
        with patch.object(profiles, "tool_is_allowed_for", return_value=False):
            listing = client.get("/api/goals")
            detail = client.get(f"/api/goals/{goal['goal']['id']}")
            chain = client.get(f"/api/tasks/{task['id']}/why-chain")
        assert listing.status_code == 403
        assert "not exposed" in listing.json()["error"]
        assert detail.status_code == 403
        assert chain.status_code == 403


def test_decompose_without_author_files_nothing(monkeypatch, tmp_path):
    """Author is resolved before any child exists, so a retry cannot hide an orphan."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    from unified_agent import config

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    epic = task_add(title="Epic", notes="[project-spec]\n\nDoD:\n- Ship the why-chain\n")
    goal = goal_add("Parity goals", author="Desktop_Agent4")
    before = storage.connect()
    try:
        count = before.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        before.close()
    with pytest.raises(ToolError, match="author is required"):
        decompose_into_goal_linked_tasks(goal["goal"]["id"], epic["id"])
    after = storage.connect()
    try:
        assert after.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == count
    finally:
        after.close()


def test_decompose_links_an_existing_unlinked_coverage_child(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    epic = task_add(title="Epic", notes="[project-spec]\n\nDoD:\n- Ship the why-chain\n")
    child = task_add(
        title="Ship the why-chain",
        notes="[covers: dod-1]\n",
        parent_id=epic["id"],
    )
    goal = goal_add("Parity goals", author="Desktop_Agent4")
    result = decompose_into_goal_linked_tasks(
        goal["goal"]["id"], epic["id"], author="Desktop_Agent4"
    )
    assert result["created"] == []
    assert child["id"] in result["linked_task_ids"]
    assert goal_id_for_task(child["id"]) == goal["goal"]["id"]


def test_dispatch_prompt_redacts_goal_titles(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    secret = "sk-" + ("a" * 24)
    goal = goal_add(f"ship {secret}", author="Desktop_Agent4")
    task = task_add(title="linked")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    prompt = append_why_chain("do the task", task["id"])
    assert secret not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert "Why-chain" in prompt
    assert "not a dependency" in prompt


def test_read_only_mode_allows_goal_and_comment_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task = task_add(title="readable")
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "1")
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    from unified_agent.agent_access import AgentAccessDenied

    assert server.REGISTERED_TOOLS["goal_manage"]("list", {})["goals"] == []
    listed = server.REGISTERED_TOOLS["task_comment"]("list", {"task_id": task["id"]})
    assert listed["comments"] == []
    with pytest.raises(AgentAccessDenied, match="read-only"):
        server.REGISTERED_TOOLS["goal_manage"]("add", {"title": "nope", "author": "Desktop_Agent4"})
    with pytest.raises(AgentAccessDenied, match="read-only"):
        server.REGISTERED_TOOLS["task_comment"](
            "append",
            {"task_id": task["id"], "body": "nope", "author": "Desktop_Agent4"},
        )


def test_http_goal_add_honors_the_tool_approval_gate(monkeypatch, tmp_path):
    token = f"tok-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent4:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        pending = client.post(
            "/api/goals",
            headers={"Authorization": f"Bearer {token}"},
            json={"action": "add", "title": "Needs a human"},
        )
    assert pending.status_code == 202
    assert pending.json()["status"] == "pending"
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_mcp_goal_add_honors_the_approval_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    storage.init_db()
    from unified_agent.tools.approvals import ApprovalPendingError, get_approval

    # Configuration gates reads too: inspecting a queued write must not
    # silently grant an unrelated list call a pass.
    with pytest.raises(ApprovalPendingError) as read_pending:
        server.REGISTERED_TOOLS["goal_manage"]("list", {})
    with pytest.raises(ApprovalPendingError) as write_pending:
        server.REGISTERED_TOOLS["goal_manage"](
            "add", {"title": "Needs a human", "author": "Desktop_Agent4"}
        )
    assert read_pending.value.approval_id != write_pending.value.approval_id
    for parked, action in ((read_pending, "list"), (write_pending, "add")):
        row = get_approval(parked.value.approval_id)
        assert row["tool"] == "goal_manage"
        assert row["payload"]["action"] == action
        assert row["status"] == "pending"
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_http_goal_write_is_a_production_operation(monkeypatch, tmp_path):
    token = f"tok-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent4:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "1")
    monkeypatch.setenv("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", "1")
    monkeypatch.setenv("UA_RUNTIME_ENV", "development")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        blocked = client.post(
            "/api/goals",
            headers={"Authorization": f"Bearer {token}"},
            json={"action": "add", "title": "Blocked"},
        )
    assert blocked.status_code == 403


def test_goal_tree_keeps_a_cycle_visible(monkeypatch, tmp_path):
    from unified_agent.goals import goal_tree

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    first = goal_add("First", author="Desktop_Agent4")
    second = goal_add("Second", author="Desktop_Agent4")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE goals SET parent_goal_id=? WHERE id=?",
            (second["goal"]["id"], first["goal"]["id"]),
        )
        conn.execute(
            "UPDATE goals SET parent_goal_id=? WHERE id=?",
            (first["goal"]["id"], second["goal"]["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    ids = []

    def walk(nodes):
        for node in nodes:
            ids.append(node["id"])
            walk(node["children"])

    walk(goal_tree()["tree"])
    assert ids == [first["goal"]["id"], second["goal"]["id"]]


def test_moving_a_linked_task_to_another_project_is_refused(monkeypatch, tmp_path):
    from unified_agent.tools.tasks import task_update

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task = task_add(title="stay", project="unified-agent")
    goal = goal_add("Here", project="unified-agent", author="Desktop_Agent4")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent4")
    with pytest.raises(ToolError, match="different project"):
        task_update(task["id"], project="browser")
    assert why_chain(task["id"])["goal_id"] == goal["goal"]["id"]


def test_goal_edit_close_reopen_and_unlink(monkeypatch, tmp_path):
    """Task #2480: the goal actions the Control Center needs, through goal_manage."""
    from unified_agent import config, events
    from unified_agent.tool_errors import ConflictError

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent4")
    goal = goal_add("Ship CC2", statement="old", author="Desktop_Agent4")["goal"]
    gid = goal["id"]
    edited = goal_manage(
        "edit",
        {"goal_id": gid, "title": " Ship CC2 v2 ", "statement": "new", "author": "Desktop_Agent4"},
    )["goal"]
    assert (edited["title"], edited["statement"]) == ("Ship CC2 v2", "new")
    # Two writers read the same revision within one second: the second loses (409).
    seen = edited["revision"]
    goal_manage("edit", {"goal_id": gid, "statement": "first", "expected_revision": seen})
    with pytest.raises(ConflictError):
        goal_manage("edit", {"goal_id": gid, "statement": "second", "expected_revision": seen})
    with pytest.raises(ToolError, match="one nonempty line"):
        goal_manage("edit", {"goal_id": gid, "title": "a\nb"})
    with pytest.raises(ToolError, match="unknown goal"):
        goal_manage("edit", {"goal_id": 999, "title": "x"})

    closed = goal_manage("close", {"goal_id": gid, "status": "abandoned", "reason": "dropped"})
    assert closed["goal"]["status"] == "abandoned"
    with pytest.raises(ConflictError, match="already abandoned"):
        goal_manage("close", {"goal_id": gid})
    with pytest.raises(ToolError, match="achieved or abandoned"):
        goal_manage("close", {"goal_id": gid, "status": "open"})
    assert goal_manage("reopen", {"goal_id": gid})["goal"]["status"] == "open"
    with pytest.raises(ConflictError, match="already open"):
        goal_manage("reopen", {"goal_id": gid})

    task = task_add(title="serves it")
    link_task(task["id"], gid, author="Desktop_Agent4")
    other = goal_add("Other", author="Desktop_Agent4")["goal"]["id"]
    with pytest.raises(ToolError, match=f"serves goal {gid}"):
        goal_manage("unlink", {"task_id": task["id"], "goal_id": other})
    unlinked = goal_manage("unlink", {"task_id": task["id"], "goal_id": gid})
    assert unlinked["unlinked"] is True and goal_id_for_task(task["id"]) is None
    with pytest.raises(ToolError, match="serves no goal"):
        goal_manage("unlink", {"task_id": task["id"], "goal_id": gid})
    channels = [e["channel"] for e in events.query_events(limit=50)]
    for channel in ("goal.updated", "goal.closed", "goal.reopened", "goal.unlinked"):
        assert channel in channels
