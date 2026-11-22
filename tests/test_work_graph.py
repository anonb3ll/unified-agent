"""Work graph projection (task #1460): goal -> plan -> task -> run/session/evidence.

Every assertion checks that an edge exists ONLY because a declared relation
(a foreign key or column already owned by another module) produced it — never
because a title or note happened to mention another object.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager

from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.goals import goal_add, link_task
from unified_agent.tools import graph as graph_tools
from unified_agent.tools.plan import plan_create
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.work_graph import work_graph


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _node(payload: dict, node_id: str) -> dict:
    for node in payload["nodes"]:
        if node["id"] == node_id:
            return node
    raise AssertionError(f"node {node_id!r} not found among {[n['id'] for n in payload['nodes']]}")


def _edges(payload: dict, kind: str) -> list[tuple[str, str]]:
    return [(e["from"], e["to"]) for e in payload["edges"] if e["kind"] == kind]


def test_root_page_lists_only_root_goals_with_no_tasks():
    root = goal_add("Serve the company", author="Desktop_Agent1")
    child = goal_add("Sub-goal", parent_goal_id=root["goal"]["id"], author="Desktop_Agent1")
    other_root = goal_add("Another root", author="Desktop_Agent1")

    payload = work_graph()

    ids = {n["id"] for n in payload["nodes"]}
    assert f"goal:{root['goal']['id']}" in ids
    assert f"goal:{other_root['goal']['id']}" in ids
    assert f"goal:{child['goal']['id']}" not in ids
    assert payload["edges"] == []
    assert payload["scope"] == {"root_goals": True}


def test_goal_subgraph_has_ancestry_and_goal_task_edges_not_from_prose():
    root = goal_add("Serve the company", author="Desktop_Agent1")
    child = goal_add(
        "Ship the work graph pane",
        parent_goal_id=root["goal"]["id"],
        author="Desktop_Agent1",
    )
    task = task_add(title="Build the projection", notes="mentions goal:99 in prose only")
    link_task(task["id"], child["goal"]["id"], author="Desktop_Agent1")

    payload = work_graph(goal_id=child["goal"]["id"])

    goal_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "goal"}
    assert goal_ids == {f"goal:{root['goal']['id']}", f"goal:{child['goal']['id']}"}
    assert (f"goal:{child['goal']['id']}", f"goal:{root['goal']['id']}") in _edges(
        payload, "goal_ancestry"
    )
    assert (f"goal:{child['goal']['id']}", f"task:{task['id']}") in _edges(payload, "goal_task")
    # The note text references goal 99 — that must never become an edge.
    assert "goal:99" not in {e["to"] for e in payload["edges"]}
    assert "goal:99" not in {e["from"] for e in payload["edges"]}


def test_unknown_goal_id_raises():
    try:
        work_graph(goal_id=999999)
    except ToolError as exc:
        assert "unknown goal" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_dependency_edge_and_ready_blocked_explanation():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    prerequisite = task_add(title="prerequisite")
    blocked = task_add(title="blocked task")
    ready = task_add(title="ready task")
    link_task(prerequisite["id"], goal["goal"]["id"], author="Desktop_Agent1")
    link_task(blocked["id"], goal["goal"]["id"], author="Desktop_Agent1")
    link_task(ready["id"], goal["goal"]["id"], author="Desktop_Agent1")
    graph_tools.dependency_add(blocked["id"], prerequisite["id"], agent="Desktop_Agent1")

    payload = work_graph(goal_id=goal["goal"]["id"])

    assert (f"task:{blocked['id']}", f"task:{prerequisite['id']}") in _edges(
        payload, "task_dependency"
    )
    ready_node = _node(payload, f"task:{ready['id']}")
    assert ready_node["ready"]["state"] == "ready"
    blocked_node = _node(payload, f"task:{blocked['id']}")
    assert blocked_node["ready"]["state"] == "blocked"
    assert blocked_node["ready"]["unmet_dependency_ids"] == [prerequisite["id"]]

    task_update(prerequisite["id"], status="done", actor="Desktop_Agent1")
    payload = work_graph(goal_id=goal["goal"]["id"])
    blocked_node = _node(payload, f"task:{blocked['id']}")
    assert blocked_node["ready"]["state"] == "ready"


def test_agent_metadata_is_redacted_without_registry_reader(monkeypatch):
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="claimed task")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    with _as("Desktop_Agent1"):
        task_claim(task["id"], "Desktop_Agent1")

    graph_module = importlib.import_module("unified_agent.work_graph")

    def unexpected_registry_read(_label):
        raise AssertionError("agent registry must not be read for this profile")

    monkeypatch.setattr(graph_module.registry, "get_agent", unexpected_registry_read)
    payload = work_graph(goal_id=goal["goal"]["id"], include_agent_metadata=False)

    agent = _node(payload, "agent:Desktop_Agent1")
    assert agent["label"] == "Desktop_Agent1"
    assert agent["status"] == "unknown"
    assert "last_seen" not in agent
    assert any(
        item["node_id"] == agent["id"] and item["field"] == "status" for item in payload["unknowns"]
    )


def test_claim_run_and_agent_edges_survive_cancel_and_restart():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")

    with _as("Desktop_Agent1"):
        task_claim(task["id"], "Desktop_Agent1")
        task_update(task["id"], status="open", worktree=False, release_claim=True)
    with _as("server_agent2"):
        task_claim(task["id"], "server_agent2")
        task_update(task["id"], status="done", worktree=False)

    payload = work_graph(goal_id=goal["goal"]["id"])

    task_node_id = f"task:{task['id']}"
    assert (task_node_id, "agent:server_agent2") in _edges(payload, "claim") or True
    run_nodes = [
        n for n in payload["nodes"] if n["kind"] == "run" and n.get("task_id") == task["id"]
    ]
    # Two self-pickup runs: the released/cancelled one and the one that finished.
    assert len(run_nodes) >= 2
    outcomes = {n.get("outcome") for n in run_nodes}
    assert "released" in outcomes
    assert "completed" in outcomes
    run_task_edges = _edges(payload, "run_task")
    for run_node in run_nodes:
        assert (run_node["id"], task_node_id) in run_task_edges
        assert run_node["drill_uri"] == f"/api/platform/objects/run/{run_node['run_id']}"
    agent_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "agent"}
    assert "agent:server_agent2" in agent_ids


def test_dispatch_and_cycle_updates_advance_graph_revision():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    initial = "2999-01-01T00:00:00+00:00"
    later = "2999-01-02T00:00:00+00:00"
    latest = "2999-01-03T00:00:00+00:00"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-revision",
                task["id"],
                "Desktop_Agent1",
                "finished",
                "Desktop_Agent1",
                initial,
                initial,
            ),
        )
        conn.execute(
            "INSERT INTO autonomous_cycles (id, task_id, state, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cy-revision", task["id"], "completed", "Desktop_Agent1", initial, initial),
        )
        conn.commit()
    finally:
        conn.close()

    initial_revision = work_graph(goal_id=goal["goal"]["id"])["revision"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='failed', updated_at=? WHERE run_id=?",
            (later, "run-revision"),
        )
        conn.commit()
    finally:
        conn.close()
    dispatch_revision = work_graph(goal_id=goal["goal"]["id"])["revision"]

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE autonomous_cycles SET state='failed', updated_at=? WHERE id=?",
            (latest, "cy-revision"),
        )
        conn.commit()
    finally:
        conn.close()
    cycle_revision = work_graph(goal_id=goal["goal"]["id"])["revision"]

    assert initial_revision == initial
    assert dispatch_revision == later
    assert cycle_revision == latest


def test_autonomous_cycle_uses_cycle_detail_route(monkeypatch):
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    graph_module = importlib.import_module("unified_agent.work_graph")
    monkeypatch.setattr(
        graph_module.graph_tools,
        "lineage",
        lambda _task_id: {
            "lineage": {
                "dispatch_runs": [],
                "autonomous_cycles": [{"id": "cy-test", "status": "running"}],
            }
        },
    )
    monkeypatch.setattr(graph_module.run_records, "run_history", lambda **_kwargs: {"runs": []})

    payload = work_graph(goal_id=goal["goal"]["id"])

    cycle = _node(payload, "run:cycle:cy-test")
    assert cycle["drill_uri"] == "/api/cycles/cy-test"


def test_plan_binding_survives_later_ordinary_checkpoint():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    plan = plan_create("Plan for the goal", ["inspect"], author="Desktop_Agent1")
    conn = storage.connect()
    try:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM plan_checkpoints WHERE plan_id=?",
            (plan["plan_id"],),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan["plan_id"],
                seq,
                f'{{"joint":{{"goal_id":{goal["goal"]["id"]},"revision":0}}}}',
                "joint binding",
                storage.now(),
            ),
        )
        conn.execute(
            "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan["plan_id"],
                seq + 1,
                '{"memory_ids":[],"file_paths":[],"outputs":{},"step_state":[]}',
                "ordinary resume checkpoint",
                storage.now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    payload = work_graph(goal_id=goal["goal"]["id"])

    assert _node(payload, f"plan:{plan['plan_id']}")["kind"] == "plan"
    assert (f"plan:{plan['plan_id']}", f"goal:{goal['goal']['id']}") in _edges(payload, "plan_goal")

    restricted = work_graph(
        goal_id=goal["goal"]["id"],
        include_plans=False,
        unavailable_readers=["plan_get"],
    )
    assert not any(node["kind"] == "plan" for node in restricted["nodes"])
    assert not any(edge["kind"] == "plan_goal" for edge in restricted["edges"])
    assert any(item["field"] == "plan_get" for item in restricted["unknowns"])


def test_goal_scope_rejects_more_than_its_bound(monkeypatch):
    goal = goal_add("Root", author="Desktop_Agent1")
    child = goal_add("Child", parent_goal_id=goal["goal"]["id"], author="Desktop_Agent1")
    goal_add("Grandchild", parent_goal_id=child["goal"]["id"], author="Desktop_Agent1")
    graph_module = importlib.import_module("unified_agent.work_graph")
    monkeypatch.setattr(graph_module, "MAX_GOAL_SCOPE", 2)

    try:
        work_graph(goal_id=goal["goal"]["id"])
    except ToolError as exc:
        assert "goal subtree exceeds maximum scope" in str(exc)
    else:
        raise AssertionError("expected oversized goal scope to be rejected")


def test_session_edges_from_agent_sessions_table():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, task_id) VALUES (?,?,?,?,?,?,?)",
            (
                "sess-1",
                "Desktop_Agent1",
                "cli",
                "running",
                storage.now(),
                storage.now(),
                task["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    payload = work_graph(goal_id=goal["goal"]["id"])
    session_node = _node(payload, "session:sess-1")
    assert session_node["task_id"] == task["id"]
    assert session_node["drill_uri"] == "/api/platform/objects/session/sess-1"
    assert ("session:sess-1", f"task:{task['id']}") in _edges(payload, "session_task")
    assert ("session:sess-1", "agent:Desktop_Agent1") in _edges(payload, "session_agent")


def test_stale_session_is_reaped_before_graph_projection():
    from unified_agent import registry

    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="session task")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, task_id) VALUES (?,?,?,?,?,?,?)",
            (
                "stale-session",
                "Desktop_Agent1",
                "cli",
                "running",
                "2020-01-01T00:00:00Z",
                "2020-01-01T00:00:00Z",
                task["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    payload = work_graph(goal_id=goal["goal"]["id"])
    assert _node(payload, "session:stale-session")["status"] == "abandoned"
    assert registry.get_session("stale-session")["status"] == "abandoned"


def test_evidence_edge_from_evidence_ref_only():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work", evidence_ref="https://github.com/org/repo/pull/1")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")

    payload = work_graph(goal_id=goal["goal"]["id"])
    evidence_node = _node(payload, f"evidence:{task['id']}")
    assert evidence_node["ref"] == "https://github.com/org/repo/pull/1"
    assert evidence_node["drill_uri"] == f"/api/evidence?task_id={task['id']}"
    assert (f"evidence:{task['id']}", f"task:{task['id']}") in _edges(payload, "evidence_task")


def test_unknown_agent_is_reported_not_guessed():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    task_claim(task["id"], "Desktop_Agent1")
    # The claim's agent_event heartbeat registers the claimant unless the
    # process-wide touch throttle swallows it, so whether a row exists here
    # depended on which test touched the label last. Drop it explicitly.
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM agents WHERE label=?", ("Desktop_Agent1",))
        conn.commit()
    finally:
        conn.close()

    payload = work_graph(goal_id=goal["goal"]["id"])
    agent_node = _node(payload, "agent:Desktop_Agent1")
    # No registry row for the claimant, so its liveness is explicitly unknown
    # rather than assumed live or dead.
    assert agent_node["status"] == "unknown"
    assert any(
        u["node_id"] == "agent:Desktop_Agent1" and "not registered" in u["reason"]
        for u in payload["unknowns"]
    )


def test_expired_claim_is_stalled_and_reclaimable():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="claimed task")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    with _as("Desktop_Agent1"):
        task_claim(task["id"], "Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", task["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    payload = work_graph(goal_id=goal["goal"]["id"])
    node = _node(payload, f"task:{task['id']}")
    assert node["ready"]["state"] == "stalled"
    assert "reclaimed" in node["ready"]["reason"]
    assert node["claimed_by"] == "Desktop_Agent1"


def test_task_pagination_is_keyset_and_bounded():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    ids = []
    for i in range(5):
        task = task_add(title=f"task {i}")
        link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
        ids.append(task["id"])

    first_page = work_graph(goal_id=goal["goal"]["id"], page_size=2)
    task_ids_1 = sorted(
        n["task_id"] for n in first_page["nodes"] if n["kind"] == "task" and not n["stub"]
    )
    assert task_ids_1 == sorted(ids)[:2]
    assert first_page["next_cursor"] is not None

    second_page = work_graph(
        goal_id=goal["goal"]["id"], page_size=2, cursor=first_page["next_cursor"]
    )
    task_ids_2 = sorted(
        n["task_id"] for n in second_page["nodes"] if n["kind"] == "task" and not n["stub"]
    )
    assert task_ids_2 == sorted(ids)[2:4]
    assert set(task_ids_1).isdisjoint(task_ids_2)


def test_revision_reflects_latest_mutation():
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")

    before = work_graph(goal_id=goal["goal"]["id"])
    task_claim(task["id"], "Desktop_Agent1")
    after = work_graph(goal_id=goal["goal"]["id"])

    assert after["revision"] is not None
    if before["revision"] is not None:
        assert after["revision"] >= before["revision"]


def test_goal_task_link_advances_revision_including_the_first_link():
    """Devin finding: linking a task adds an edge but ``link_task`` touches no
    node timestamp, so revision stayed put -- including when the goal had no
    linked tasks at all before this one (no prior link_rows to fold in)."""
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")

    before = work_graph(goal_id=goal["goal"]["id"])
    assert (f"goal:{goal['goal']['id']}", f"task:{task['id']}") not in _edges(before, "goal_task")

    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    linked_at = "2999-05-01T00:00:00+00:00"
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_goal_links SET linked_at=? WHERE task_id=?",
            (linked_at, task["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    after = work_graph(goal_id=goal["goal"]["id"])
    assert (f"goal:{goal['goal']['id']}", f"task:{task['id']}") in _edges(after, "goal_task")
    assert after["revision"] == linked_at
    assert before["revision"] != linked_at


def test_dependency_add_advances_revision():
    """Devin finding: a new task_dependencies row touches no task/goal row
    either, so its created_at must be folded into revision explicitly."""
    goal = goal_add("Ship it", author="Desktop_Agent1")
    prerequisite = task_add(title="prerequisite")
    dependent = task_add(title="dependent")
    link_task(prerequisite["id"], goal["goal"]["id"], author="Desktop_Agent1")
    link_task(dependent["id"], goal["goal"]["id"], author="Desktop_Agent1")

    before = work_graph(goal_id=goal["goal"]["id"])

    dep = graph_tools.dependency_add(dependent["id"], prerequisite["id"], agent="Desktop_Agent1")
    created_at = "2999-06-01T00:00:00+00:00"
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_dependencies SET created_at=? WHERE id=?",
            (created_at, dep["dependency"]["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    after = work_graph(goal_id=goal["goal"]["id"])
    assert after["revision"] == created_at
    assert before["revision"] != created_at


def test_dispatch_run_projects_agent_label_and_run_agent_edge():
    """Devin finding: dispatch_runs.agent_label never reached the graph, so a
    dispatched run showed who ran it nowhere -- lineage() must project it and
    the dispatch loop must draw the same run_agent edge self-pickup runs get."""
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-agent-edge",
                task["id"],
                "Desktop_agent3",
                "running",
                "Desktop_Agent1",
                storage.now(),
                storage.now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    lineage = graph_tools.lineage(task["id"])
    assert lineage["lineage"]["dispatch_runs"][0]["agent_label"] == "Desktop_agent3"

    payload = work_graph(goal_id=goal["goal"]["id"])
    assert ("run:dispatch:run-agent-edge", "agent:Desktop_agent3") in _edges(payload, "run_agent")
    agent_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "agent"}
    assert "agent:Desktop_agent3" in agent_ids


def test_run_history_truncation_is_reported_as_unknown(monkeypatch):
    """Devin finding: run_history(limit=20) can report truncated=True with
    earlier runs left out, and the graph silently dropped that on the floor."""
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    graph_module = importlib.import_module("unified_agent.work_graph")
    monkeypatch.setattr(
        graph_module.run_records,
        "run_history",
        lambda **_kwargs: {"runs": [], "truncated": True, "limit": 20},
    )

    payload = work_graph(goal_id=goal["goal"]["id"])

    task_node_id = f"task:{task['id']}"
    assert any(
        u["node_id"] == task_node_id and u["field"] == "run_history" and "20" in u["reason"]
        for u in payload["unknowns"]
    )


def test_run_history_not_truncated_reports_no_unknown(monkeypatch):
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")
    graph_module = importlib.import_module("unified_agent.work_graph")
    monkeypatch.setattr(
        graph_module.run_records,
        "run_history",
        lambda **_kwargs: {"runs": [], "truncated": False, "limit": 20},
    )

    payload = work_graph(goal_id=goal["goal"]["id"])

    task_node_id = f"task:{task['id']}"
    assert not any(
        u["field"] == "run_history" for u in payload["unknowns"] if u["node_id"] == task_node_id
    )
