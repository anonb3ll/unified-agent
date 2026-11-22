import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools import graph
from unified_agent.tools.delegate import delegate, delegate_claim


def _task(conn, task_id: int, title: str) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
        "VALUES (?, ?, 'open', 'med', ?, ?)",
        (task_id, title, storage.now(), storage.now()),
    )


def test_dependency_edges_have_stable_ids_and_reject_cycles():
    conn = storage.connect()
    try:
        _task(conn, 1, "one")
        _task(conn, 2, "two")
        _task(conn, 3, "three")
        conn.commit()
    finally:
        conn.close()

    first = graph.dependency_add(2, 1, agent="Desktop_Agent1")
    second = graph.dependency_add(3, 2, agent="Desktop_Agent1")
    assert first["dependency"]["id"] != second["dependency"]["id"]
    with pytest.raises(ToolError, match="cycle"):
        graph.dependency_add(1, 3, agent="Desktop_Agent1")


def test_coordination_manage_dispatches_dependency_list():
    result = graph.coordination_manage("dependency_list", {"limit": 10})
    assert result == {"dependencies": [], "truncated": False}


def test_coordination_manage_rejects_bad_action_and_params():
    with pytest.raises(ToolError, match="action must be one of"):
        graph.coordination_manage("unknown")
    with pytest.raises(ToolError, match="invalid params"):
        graph.coordination_manage("handoff_list", {"unexpected": True})


def test_coordination_manage_dispatches_project_repo_bind_get_list_unbind():
    bound = graph.coordination_manage(
        "project_repo_bind",
        {"project": "browser", "repo": "someowner/browser", "bound_by": "test-agent"},
    )
    assert bound["binding"]["repo"] == "someowner/browser"

    fetched = graph.coordination_manage("project_repo_get", {"project": "browser"})
    assert fetched["binding"]["repo"] == "someowner/browser"

    listed = graph.coordination_manage("project_repo_list", {})
    projects = {row["project"] for row in listed["projects"]}
    assert projects == {"unified-agent", "browser", "sample-store", "client-portal"}

    unbound = graph.coordination_manage(
        "project_repo_unbind", {"project": "browser", "unbound_by": "test-agent"}
    )
    assert unbound["unbound"] is True
    assert graph.coordination_manage("project_repo_get", {"project": "browser"})["binding"] is None


def test_dependency_remove_requires_creator():
    conn = storage.connect()
    try:
        _task(conn, 10, "ten")
        _task(conn, 11, "eleven")
        conn.commit()
    finally:
        conn.close()
    edge_id = graph.dependency_add(10, 11, agent="Desktop_Agent1")["dependency"]["id"]
    with pytest.raises(ToolError, match="creator"):
        graph.dependency_remove(edge_id, agent="Desktop_agent3")
    assert graph.dependency_remove(edge_id, agent="Desktop_Agent1")["removed"] is True


def test_handoff_list_redacts_task_spec_and_callback_payload(monkeypatch):
    monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "0")
    graph.handoff("private task spec", "Desktop_agent3", agent="Desktop_Agent1")
    rows = graph.handoff_list()["handoffs"]
    assert rows
    assert "task_spec" not in rows[0]
    assert "callback_result" not in rows[0]


def test_handoff_rejects_identity_mismatch(monkeypatch):
    from unified_agent import config

    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="identity mismatch"):
            graph.handoff("spec", "Desktop_agent3", agent="Desktop_Agent5")
    finally:
        config.agent_identity_var.reset(token)


def test_racing_delegation_claims_have_one_winner():
    result = delegate(
        "race claim",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
        callback="none",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: delegate_claim(result["delegation_id"], agent="Desktop_agent3"),
                range(2),
            )
        )
    assert [item["claimed"] for item in results].count(True) == 1


def test_graph_keeps_duplicate_dispatches_and_stale_runs_consistent(monkeypatch):
    from unified_agent import server
    from unified_agent.webui import api

    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "dependency_list",
        lambda **kwargs: {"dependencies": [{"id": 4, "task_id": 2, "depends_on_task_id": 1}]},
    )
    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "handoff_list",
        lambda **kwargs: {
            "handoffs": [
                {
                    "id": 8,
                    "task_id": 2,
                    "status": "failed",
                    "from_agent": "Desktop_Agent1",
                    "to_agent": "Desktop_agent3",
                }
            ]
        },
    )
    graph_payload = asyncio.run(
        api.execution_graph_payload(
            tasks_res={
                "tasks": [
                    {"id": 1, "title": "prerequisite", "status": "done"},
                    {"id": 2, "title": "dispatch", "status": "doing"},
                ]
            },
            delegations_res={
                "delegations": [
                    {
                        "id": 10,
                        "task_id": 2,
                        "status": "pending",
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                    },
                    {
                        "id": 11,
                        "task_id": 2,
                        "status": "failed",
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                    },
                ]
            },
            sessions_res={
                "sessions": [
                    {
                        "id": "stale-1",
                        "task_id": 2,
                        "delegation_id": 10,
                        "status": "abandoned",
                        "kind": "pi_run",
                    }
                ]
            },
        )
    )
    assert graph_payload["counts"] == {
        "tasks": 2,
        "delegations": 2,
        "dependencies": 1,
        "handoffs": 1,
        "runs": 1,
    }
    assert {node["id"] for node in graph_payload["nodes"]} >= {
        "delegation:10",
        "delegation:11",
        "session:stale-1",
    }
    assert any(link["kind"] == "depends_on" for link in graph_payload["links"])
    assert graph_payload["runs"][0]["state"] == "abandoned"
    assert graph_payload["runs"][0]["validation"] is None
    assert graph_payload["runs"][0]["review"] is None
    assert graph_payload["runs"][0]["pr"] is None


def test_graph_does_not_depend_on_event_log_coverage(monkeypatch):
    from unified_agent import server
    from unified_agent.webui import api

    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "dependency_list",
        lambda **kwargs: {"dependencies": []},
    )
    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "handoff_list",
        lambda **kwargs: {"handoffs": []},
    )
    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "event_log_query",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("partial event log")),
    )
    payload = asyncio.run(
        api.execution_graph_payload(
            tasks_res={"tasks": []},
            delegations_res={"delegations": []},
            sessions_res={"sessions": []},
        )
    )
    assert payload["counts"] == {
        "tasks": 0,
        "delegations": 0,
        "dependencies": 0,
        "handoffs": 0,
        "runs": 0,
    }


def test_dependency_list_proves_completeness_at_the_page_boundary():
    conn = storage.connect()
    try:
        for task_id in (1, 2, 3):
            _task(conn, task_id, f"dependency fixture {task_id}")
        conn.commit()
    finally:
        conn.close()
    first = graph.dependency_add(2, 1, agent="Desktop_Agent2")["dependency"]
    second = graph.dependency_add(3, 2, agent="Desktop_Agent2")["dependency"]
    assert graph.dependency_list(limit=1) == {"dependencies": [first], "truncated": True}
    assert graph.dependency_list(limit=2) == {"dependencies": [first, second], "truncated": False}
    assert graph.dependency_list(limit=1, task_id=3) == {
        "dependencies": [second],
        "truncated": False,
    }


# --- ready dependents (task #2454) -------------------------------------------------


def _ready_messages(task_id=None):
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT i.message_id, i.sender, i.recipient, i.act, i.task_id, i.expects_reply, "
            "json_extract(e.payload, '$.body') AS body FROM message_index i "
            "JOIN event_log e ON e.ulid = i.event_ulid WHERE i.sender='hub-graph' "
            "ORDER BY i.event_seq"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if task_id is None or r["task_id"] == task_id]


def _ready_events():
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.task.ready' ORDER BY ROWID"
        ).fetchall()
    finally:
        conn.close()
    import json

    return [json.loads(r["payload"]) for r in rows]


def _set(task_id, **fields):
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def _new(title, **fields):
    from unified_agent.tools import tasks

    task_id = tasks.task_add(title)["id"]
    if fields:
        _set(task_id, **fields)
    return task_id


def _close(task_id, status="done"):
    from unified_agent.tools import tasks

    return tasks.task_update(task_id, status=status, actor="Desktop_Agent1")


def test_closing_last_prerequisite_tells_next_owner_once():
    a = _new("prerequisite")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a, agent="Desktop_Agent1")

    _close(a)
    (sent,) = _ready_messages(b)
    assert sent["recipient"] == "Desktop_Agent2" and sent["act"] == "inform"
    assert sent["message_id"].startswith("task-ready-") and sent["expects_reply"] == 1
    assert f"Task #{b} is ready" in sent["body"] and f"done: #{a}" in sent["body"]
    # A retried close and a second closer change nothing.
    _close(a)
    from unified_agent import ready_dependents

    assert ready_dependents.drain_pending()["sent"] == []
    assert len(_ready_messages(b)) == 1
    assert len(_ready_events()) == 1


def test_dependent_waits_for_every_prerequisite():
    a1, a2 = _new("first"), _new("second")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a1, agent="Desktop_Agent1")
    graph.dependency_add(b, a2, agent="Desktop_Agent1")
    _close(a1)
    assert _ready_messages(b) == []
    _close(a2)
    assert len(_ready_messages(b)) == 1


def test_reopen_and_reclose_sends_once_per_readiness_generation():
    a = _new("prerequisite")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a, agent="Desktop_Agent1")
    _close(a)
    _close(a, "open")
    _close(a)
    assert len(_ready_messages(b)) == 1  # B's own readiness never changed

    # B itself closed and reopened: a new generation, so the next close sends once.
    _close(b)
    _close(b, "open")
    _set(b, next_owner="Desktop_Agent2")  # closing B ended its ownership
    _close(a, "open")
    _close(a)
    _close(a, "open")
    _close(a)
    assert len(_ready_messages(b)) == 2

    # A new prerequisite edge is a new generation as well.
    c = _new("late prerequisite")
    graph.dependency_add(b, c, agent="Desktop_Agent1")
    _close(c)
    assert len(_ready_messages(b)) == 3


def test_concurrent_prerequisite_closes_notify_once():
    a1, a2 = _new("first"), _new("second")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a1, agent="Desktop_Agent1")
    graph.dependency_add(b, a2, agent="Desktop_Agent1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_close, [a1, a2]))
    assert len(_ready_messages(b)) == 1
    assert len(_ready_events()) == 1


@pytest.mark.parametrize(
    ("fields", "owner", "source"),
    [
        (
            {"next_owner": "Desktop_Agent2", "claimed_by": "Desktop_Agent5"},
            "Desktop_Agent2",
            "next_owner",
        ),
        ({"claimed_by": "Desktop_Agent5", "status": "doing"}, "Desktop_Agent5", "claimant"),
        ({"lane": "Desktop_Agent5"}, "Desktop_Agent5", "lane"),
        ({}, None, None),
    ],
)
def test_owner_resolution_order(fields, owner, source):
    from unified_agent import queue_closure

    a = _new("prerequisite")
    b = _new("dependent", **fields)
    graph.dependency_add(b, a, agent="Desktop_Agent1")
    _close(a)
    (event,) = _ready_events()
    assert (event["owner"], event["owner_source"]) == (owner, source)
    assert [m["recipient"] for m in _ready_messages(b)] == ([owner] if owner else [])
    assert queue_closure.queue_health()["ready_unowned"] == (0 if owner else 1)


def test_cancelled_prerequisite_is_named_and_blocked_dependent_stays_blocked():
    a1, a2 = _new("shipped"), _new("dropped")
    b = _new("dependent", next_owner="Desktop_Agent2", status="blocked")
    graph.dependency_add(b, a1, agent="Desktop_Agent1")
    graph.dependency_add(b, a2, agent="Desktop_Agent1")
    _close(a1)
    _close(a2, "cancelled")
    (sent,) = _ready_messages(b)
    assert f"done: #{a1}" in sent["body"] and f"cancelled: #{a2}" in sent["body"]
    assert "did not deliver its work" in sent["body"] and "still blocked" in sent["body"]
    conn = storage.connect()
    try:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (b,)).fetchone()[0] == "blocked"
    finally:
        conn.close()


def test_dependent_in_another_tenant_is_never_notified(monkeypatch):
    from unified_agent import edition

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    a = _new("prerequisite")
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    b = _new("other tenant's dependent", next_owner="Desktop_Agent2")
    conn = storage.connect()
    try:  # a cross-tenant edge no tool would write
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id, created_by, created_at) "
            "VALUES (?, ?, 'test', ?)",
            (b, a, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    _close(a)
    assert _ready_events() == [] and _ready_messages() == []


def test_flag_off_records_and_sends_nothing(monkeypatch):
    from unified_agent import ready_dependents

    monkeypatch.setenv("UA_READY_DEPENDENT_NOTIFY", "0")
    a = _new("prerequisite")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a, agent="Desktop_Agent1")
    _close(a)
    assert _ready_events() == [] and _ready_messages() == []
    assert ready_dependents.drain_pending()["enabled"] is False


def test_crash_before_send_is_recovered_once_by_the_drain(monkeypatch):
    from unified_agent import ready_dependents
    from unified_agent.messaging import send as messaging_send

    a = _new("prerequisite")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a, agent="Desktop_Agent1")
    real = messaging_send.send_ready_notice

    def dies(**kwargs):
        raise RuntimeError("process died")

    monkeypatch.setattr(messaging_send, "send_ready_notice", dies)
    _close(a)  # the close commits; its readiness event is the durable record
    assert _ready_messages() == [] and len(_ready_events()) == 1
    monkeypatch.setattr(messaging_send, "send_ready_notice", real)
    assert len(ready_dependents.drain_pending()["sent"]) == 1
    assert ready_dependents.drain_pending()["sent"] == []
    assert len(_ready_messages(b)) == 1


def test_pending_notice_for_a_dependent_no_longer_ready_is_skipped(monkeypatch):
    from unified_agent import ready_dependents

    a = _new("prerequisite")
    b = _new("dependent", next_owner="Desktop_Agent2")
    graph.dependency_add(b, a, agent="Desktop_Agent1")
    with monkeypatch.context() as patched:  # no post-commit send this time
        patched.setattr(ready_dependents, "drain_pending", lambda **kw: {})
        _close(a)
        _close(a, "open")
    result = ready_dependents.drain_pending()
    assert result["sent"] == [] and len(result["skipped"]) == 1
    assert ready_dependents.drain_pending()["skipped"] == []  # skipped once, for good
    assert _ready_messages() == []


def test_every_closer_reaches_the_chokepoint():
    """task_update, complete_direct (close-on-merge, merge orchestrator, reviewer
    auto-close) and record_transition_in_tx (the merge closeout) all notify."""
    from unified_agent import run_records

    owner = {"next_owner": "Desktop_Agent2"}
    a1, a2, a3 = _new("update"), _new("direct"), _new("merge closeout")
    deps = [_new(f"dependent {i}", **owner) for i in range(3)]
    for dep, pre in zip(deps, (a1, a2, a3), strict=True):
        graph.dependency_add(dep, pre, agent="Desktop_Agent1")

    _close(a1)
    expected = run_records.capture_direct_completion(a2)
    assert run_records.complete_direct(expected, actor="merge-orchestrator", note="merged")
    raw = storage.connect()
    tx = run_records.attach(raw)
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (storage.next_task_revision(raw, a3), a3),
        )
        run_records.record_transition_in_tx(
            tx, a3, old_status="open", new_status="done", actor="merge", claimed_by=None
        )
        raw.commit()
    finally:
        raw.close()
    run_records.flush(tx)
    assert [len(_ready_messages(d)) for d in deps] == [1, 1, 1]


def test_hub_graph_sender_and_ids_are_reserved():
    from unified_agent.messaging import send as messaging_send

    with pytest.raises(ToolError, match="reserved for the hub"):
        messaging_send.send_message(
            act="inform", from_agent="hub-graph", to_agent="Desktop_Agent2", body="hi"
        )
    with pytest.raises(ToolError, match="reserved for hub service notices"):
        messaging_send.send_message(
            act="inform",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_Agent2",
            body="hi",
            message_id="task-ready-X",
        )
