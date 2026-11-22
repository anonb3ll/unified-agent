from unified_agent import storage
from unified_agent.tools import graph
from unified_agent.tools.tasks import task_add, task_claim, task_list, task_update


def _ids(**kwargs) -> set[int]:
    return {task["id"] for task in task_list(status="all", **kwargs)["tasks"]}


def test_frontier_excludes_open_prerequisites_and_includes_done_or_cancelled_ones():
    prerequisite = task_add("prerequisite")
    blocked = task_add("blocked by prerequisite")
    ready = task_add("ready task")
    graph.dependency_add(blocked["id"], prerequisite["id"], agent="Desktop_Agent1")

    assert ready["id"] in _ids(frontier=True)
    assert blocked["id"] not in _ids(frontier=True)

    task_update(prerequisite["id"], status="done", actor="test-agent")
    assert blocked["id"] in _ids(frontier=True)

    cancelled_prerequisite = task_add("cancelled prerequisite")
    unblocked_by_cancel = task_add("unblocked by cancellation")
    graph.dependency_add(
        unblocked_by_cancel["id"], cancelled_prerequisite["id"], agent="Desktop_Agent1"
    )
    task_update(cancelled_prerequisite["id"], status="cancelled")

    assert unblocked_by_cancel["id"] in _ids(frontier=True)


def test_frontier_composes_with_system_stream_filter_for_similar_titles():
    private = task_add("Fix MCP worker retry", notes="stream: PRIVATE")
    public = task_add("Fix MCP worker retry", notes="stream: PUBLIC")
    system = task_add("Fix MCP worker retry", notes="stream: SYSTEM")

    system_ids = _ids(frontier=True, notes_contain="stream: SYSTEM")

    assert system_ids == {system["id"]}
    assert private["id"] not in system_ids
    assert public["id"] not in system_ids


def test_stale_claims_returns_only_expired_doing_tasks():
    stale = task_add("stale lease")
    current = task_add("current lease")
    open_task = task_add("never claimed")
    task_claim(stale["id"], agent="Desktop_Agent1")
    task_claim(current["id"], agent="Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (stale["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    assert _ids(stale_claims=True) == {stale["id"]}
    assert current["id"] not in _ids(stale_claims=True)
    assert open_task["id"] not in _ids(stale_claims=True)


def test_dependency_list_filters_to_one_task():
    first = task_add("first prerequisite")
    second = task_add("second prerequisite")
    target = task_add("target")
    other_target = task_add("other target")
    graph.dependency_add(target["id"], first["id"], agent="Desktop_Agent1")
    graph.dependency_add(target["id"], second["id"], agent="Desktop_Agent1")
    graph.dependency_add(other_target["id"], first["id"], agent="Desktop_Agent1")

    dependencies = graph.dependency_list(task_id=target["id"])["dependencies"]

    assert {edge["depends_on_task_id"] for edge in dependencies} == {first["id"], second["id"]}
    assert all(edge["task_id"] == target["id"] for edge in dependencies)
