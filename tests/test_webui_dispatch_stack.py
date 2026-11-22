import asyncio
from datetime import UTC, datetime

import pytest
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated

from unified_agent import profiles, server, storage
from unified_agent.dispatch import coordinator
from unified_agent.tools import delegate, graph, tasks
from unified_agent.webui import api, routes
from unified_agent.webui.contracts import DispatchStackRequest


def test_dispatch_stack_orders_tasks_and_exposes_the_next_ready_increment(monkeypatch):
    async def fake_safe(name, default=None, **kwargs):
        if name == "task_list":
            return {
                "tasks": [
                    {"id": 1, "title": "first", "status": "open", "project": "unified-agent"},
                    {"id": 2, "title": "second", "status": "open", "project": "unified-agent"},
                ]
            }
        return {
            "dependencies": (
                [{"task_id": 2, "depends_on_task_id": 1}] if kwargs["task_id"] == 2 else []
            ),
            "truncated": False,
        }

    monkeypatch.setattr(api, "_safe", fake_safe)
    monkeypatch.setattr(
        coordinator,
        "evaluate_ready",
        lambda *, task_id, **_kwargs: {"ready": task_id == 1, "reason": "ok"},
    )

    result = asyncio.run(
        api.dispatch_stack_payload(
            DispatchStackRequest((2, 1), project="unified-agent", expected_lane=None)
        )
    )

    assert result["ordered_task_ids"] == [1, 2]
    assert result["next_task_id"] == 1
    assert result["items"][1]["blocked_reasons"] == ["dependencies_incomplete"]


@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
def test_dispatch_stack_post_never_launches_past_an_unfinished_dependency(identity, monkeypatch):
    first = tasks.task_add("unfinished prerequisite")
    second_id = _ready_delegation()
    second = {"id": second_id}
    graph.dependency_add(second_id, first["id"], agent="Desktop_Agent2")
    assert coordinator.evaluate_ready(task_id=second_id)["ready"]
    launches = []

    def start_ui_dispatch(**kwargs):
        launches.append(kwargs["task_id"])
        return {"status": "fixture-launch"}

    monkeypatch.setattr(api, "start_ui_dispatch", start_ui_dispatch)
    client = _client(routes.web_routes())
    selected = [second["id"], first["id"]]
    get = client.get(
        "/api/dispatch-stack",
        headers=_bearer(identity),
        params={"task_ids": ",".join(map(str, selected))},
    )
    assert get.status_code == 200, get.text
    assert get.json()["next_task_id"] is None
    assert "dependencies_incomplete" in get.json()["items"][1]["blocked_reasons"]
    post = client.post(
        "/api/action/dispatch-stack", headers=_bearer(identity), json={"task_ids": selected}
    )
    assert post.status_code in {409, 503}, post.text
    assert launches == []


@pytest.mark.parametrize("failed_tool", ["task_list", "dependency_list"])
def test_dispatch_stack_read_failure_never_reports_a_ready_task(failed_tool, monkeypatch):
    async def failed_safe(name, default=None, **kwargs):
        if name == failed_tool:
            return {**default, "error": "fixture read denied"}
        if name == "task_list":
            return {"tasks": [{"id": 1, "title": "fixture", "status": "open"}]}
        return {"dependencies": []}

    monkeypatch.setattr(api, "_safe", failed_safe)
    evaluations = []

    def ready(**kwargs):
        evaluations.append(kwargs)
        return {"ready": True}

    monkeypatch.setattr(coordinator, "evaluate_ready", ready)
    result = asyncio.run(api.dispatch_stack_payload(DispatchStackRequest((1,), None, None)))
    assert result.get("error")
    assert result["next_task_id"] is None
    assert evaluations == []


def _ready_delegation():
    from test_dispatch_coordinator import _spec

    result = delegate.delegate(
        task_spec=_spec("private HTTP dispatch fixture"),
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent1",
        callback="none",
    )
    task_id = result["task_id"]
    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")
    assert coordinator.evaluate_ready(task_id=task_id)["ready"]
    return task_id


@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
def test_dispatch_stack_completed_prerequisite_launches_without_preview_grant(
    identity, monkeypatch
):
    first = tasks.task_add("completed prerequisite")
    tasks.task_update(first["id"], status="done", actor="Desktop_Agent2")
    task_id = _ready_delegation()
    graph.dependency_add(task_id, first["id"], agent="Desktop_Agent2")
    launches = []

    def start_ui_dispatch(**kwargs):
        assert not profiles.http_read_tool_is_allowed("dependency_list", {})
        assert not profiles.tool_is_allowed("dependency_list")
        launches.append(kwargs["task_id"])
        return {"status": "fixture-only"}

    monkeypatch.setattr(api, "start_ui_dispatch", start_ui_dispatch)
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack", headers=_bearer(identity), json={"task_ids": [task_id]}
    )
    assert response.status_code == 202, response.text
    assert launches == [task_id]
    assert response.json()["items"][0]["depends_on"] == [first["id"]]


@pytest.mark.parametrize("failed_tool", ["task_list", "dependency_list"])
@pytest.mark.parametrize("failure", ["exception", "in-band"])
def test_dispatch_stack_http_read_failure_never_launches(failed_tool, failure, monkeypatch):
    task_id = _ready_delegation()
    launches = []

    def failed_read(**kwargs):
        if failure == "exception":
            raise RuntimeError("fixture read unavailable")
        return {"error": "fixture read unavailable"}

    failed_read.__name__ = failed_tool
    monkeypatch.setitem(
        server.REGISTERED_TOOLS, failed_tool, server._guarded_and_instrumented(failed_read)
    )
    monkeypatch.setattr(api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs))
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack",
        headers=_bearer("Desktop_Agent2"),
        json={"task_ids": [task_id]},
    )
    assert response.status_code == 503, response.text
    assert response.json()["source"] == failed_tool
    assert response.json()["next_task_id"] is None
    assert launches == []


@pytest.mark.parametrize("failed_tool", ["task_list", "dependency_list"])
def test_dispatch_stack_unexpected_preview_exception_fails_closed(failed_tool, monkeypatch):
    async def failed_safe(name, default=None, **kwargs):
        if name == failed_tool:
            raise RuntimeError("fixture adapter exception")
        return {"tasks": [{"id": 1}], "dependencies": []}

    monkeypatch.setattr(api, "_safe", failed_safe)
    result = asyncio.run(api.dispatch_stack_payload(DispatchStackRequest((1,), None, None)))
    assert result["source"] == failed_tool
    assert result["next_task_id"] is None
    assert result["code"] == "dispatch_stack_read_unavailable"


def test_dispatch_preview_keeps_dependency_policy_gate(monkeypatch, tmp_path):
    task_id = _ready_delegation()
    policy = tmp_path / "dependency-policy.yaml"
    policy.write_text(
        "rules:\n  - name: deny_preview\n    type: tool\n    tool: dependency_list\n    action: deny\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    launches = []
    monkeypatch.setattr(api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs))
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack",
        headers=_bearer("Desktop_Agent2"),
        json={"task_ids": [task_id]},
    )
    assert response.status_code == 503, response.text
    assert response.json()["source"] == "dependency_list"
    assert launches == []


def _seed_dependency_edges(task_id, count):
    """Persist unrelated edges without stubbing the graph or readiness reads."""
    conn = storage.connect()
    try:
        first_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tasks").fetchone()[0]
        now = storage.now()
        prerequisite_ids = range(first_id, first_id + count)
        conn.executemany(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, ?, 'done', 'low', ?, ?)",
            [(task, f"noise prerequisite {task}", now, now) for task in prerequisite_ids],
        )
        conn.executemany(
            "INSERT INTO task_dependencies "
            "(task_id, depends_on_task_id, created_by, created_at) VALUES (?, ?, ?, ?)",
            [(task_id, dep, "Desktop_Agent2", now) for dep in prerequisite_ids],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
@pytest.mark.parametrize("completed", [False, True])
def test_dispatch_stack_reads_late_prerequisite_after_global_edge_limit(
    identity, completed, monkeypatch
):
    prerequisite = tasks.task_add("late selected prerequisite")
    if completed:
        tasks.task_update(prerequisite["id"], status="done", actor="Desktop_Agent2")
    selected_id = _ready_delegation()
    unrelated = tasks.task_add("unrelated dependency owner")
    _seed_dependency_edges(unrelated["id"], 2001)
    graph.dependency_add(selected_id, prerequisite["id"], agent="Desktop_Agent2")
    assert all(
        edge["task_id"] != selected_id for edge in graph.dependency_list(limit=2000)["dependencies"]
    )
    launches = []
    monkeypatch.setattr(
        api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs["task_id"]) or {}
    )
    client = _client(routes.web_routes())
    preview = client.get(
        "/api/dispatch-stack", headers=_bearer(identity), params={"task_ids": str(selected_id)}
    )
    response = client.post(
        "/api/action/dispatch-stack", headers=_bearer(identity), json={"task_ids": [selected_id]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["depends_on"] == [prerequisite["id"]]
    if completed:
        assert preview.json()["next_task_id"] == selected_id
        assert response.status_code == 202, response.text
        assert launches == [selected_id]
    else:
        assert preview.json()["next_task_id"] is None
        assert "dependencies_incomplete" in preview.json()["items"][0]["blocked_reasons"]
        assert response.status_code == 409, response.text
        assert launches == []


def test_dispatch_stack_scoped_dependency_overflow_never_launches(monkeypatch):
    selected_id = _ready_delegation()
    _seed_dependency_edges(selected_id, 2001)
    page = graph.dependency_list(limit=10_000, task_id=selected_id)
    assert len(page["dependencies"]) == 2000
    assert page["truncated"] is True
    launches = []
    monkeypatch.setattr(api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs))
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack",
        headers=_bearer("Desktop_Agent2"),
        json={"task_ids": [selected_id]},
    )
    assert response.status_code == 503, response.text
    assert response.json()["source"] == "dependency_list"
    assert response.json()["next_task_id"] is None
    assert launches == []


@pytest.mark.parametrize(
    "incomplete", [{"dependencies": []}, {"dependencies": [], "truncated": True}]
)
def test_dispatch_stack_missing_or_incomplete_dependency_proof_never_launches(
    incomplete, monkeypatch
):
    selected_id = _ready_delegation()

    def incomplete_read(**kwargs):
        return incomplete

    incomplete_read.__name__ = "dependency_list"
    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "dependency_list",
        server._guarded_and_instrumented(incomplete_read),
    )
    launches = []
    monkeypatch.setattr(api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs))
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack",
        headers=_bearer("Desktop_Agent2"),
        json={"task_ids": [selected_id]},
    )
    assert response.status_code == 503, response.text
    assert response.json()["source"] == "dependency_list"
    assert launches == []


@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
@pytest.mark.parametrize("completed", [False, True])
def test_dispatch_stack_reads_selected_and_prerequisite_beyond_first_task_page(
    identity, completed, monkeypatch
):
    prerequisite = tasks.task_add("late task-page prerequisite", priority="low")
    if completed:
        tasks.task_update(prerequisite["id"], status="done", actor="Desktop_Agent2")
    selected_id = _ready_delegation()
    graph.dependency_add(selected_id, prerequisite["id"], agent="Desktop_Agent2")
    conn = storage.connect()
    try:
        now = storage.now()
        conn.executemany(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at) "
            "VALUES (?, 'open', 'high', ?, ?)",
            [(f"higher priority fixture {i}", now, now) for i in range(500)],
        )
        conn.commit()
    finally:
        conn.close()
    page = tasks.task_list(status="all", fields="full", limit=500)
    assert page["truncated"] is True
    assert page["next_cursor"]
    assert {selected_id, prerequisite["id"]}.isdisjoint(task["id"] for task in page["tasks"])
    launches = []

    def start_ui_dispatch(**kwargs):
        assert not profiles.http_read_tool_is_allowed("dependency_list", {})
        launches.append(kwargs["task_id"])
        return {"status": "fixture-only"}

    monkeypatch.setattr(api, "start_ui_dispatch", start_ui_dispatch)
    client = _client(routes.web_routes())
    preview = client.get(
        "/api/dispatch-stack", headers=_bearer(identity), params={"task_ids": str(selected_id)}
    )
    response = client.post(
        "/api/action/dispatch-stack", headers=_bearer(identity), json={"task_ids": [selected_id]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["depends_on"] == [prerequisite["id"]]
    if completed:
        assert preview.json()["next_task_id"] == selected_id
        assert response.status_code == 202, response.text
        assert launches == [selected_id]
    else:
        assert preview.json()["next_task_id"] is None
        assert preview.json()["items"][0]["blocked_reasons"] == ["dependencies_incomplete"]
        assert response.status_code == 409, response.text
        assert launches == []


@pytest.mark.parametrize("failure", ["exception", "in-band", "missing-cursor"])
def test_dispatch_stack_incomplete_later_task_page_never_launches(failure, monkeypatch):
    selected_id = _ready_delegation()
    original = tasks.task_list
    calls = []

    def failed_page(**kwargs):
        calls.append(kwargs.get("cursor"))
        if kwargs.get("cursor"):
            if failure == "exception":
                raise RuntimeError("later task page unavailable")
            return {"tasks": [], "error": "later task page unavailable"}
        page = original(**kwargs)
        page["truncated"] = True
        page["next_cursor"] = None if failure == "missing-cursor" else "fixture-next-page"
        return page

    failed_page.__name__ = "task_list"
    monkeypatch.setitem(
        server.REGISTERED_TOOLS, "task_list", server._guarded_and_instrumented(failed_page)
    )
    launches = []
    monkeypatch.setattr(api, "start_ui_dispatch", lambda **kwargs: launches.append(kwargs))
    evaluations = []
    monkeypatch.setattr(coordinator, "evaluate_ready", lambda **kwargs: evaluations.append(kwargs))
    response = _client(routes.web_routes()).post(
        "/api/action/dispatch-stack",
        headers=_bearer("Desktop_Agent2"),
        json={"task_ids": [selected_id]},
    )
    assert response.status_code == 503, response.text
    assert response.json()["source"] == "task_list"
    assert response.json()["next_task_id"] is None
    assert evaluations == []
    assert launches == []
    assert calls == ([None] if failure == "missing-cursor" else [None, "fixture-next-page"])
