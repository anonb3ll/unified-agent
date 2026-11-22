"""Control Center reads say when they are partial (task #1430, Devin review on #814).

A read that stops early, because a page failed or a truncated page carried no
cursor, must reach the payload as `error` + `truncated` instead of presenting
the collected prefix as the whole queue.
"""

import anyio

from unified_agent.webui import api


def _pages(*pages):
    calls = iter(pages)

    async def fake_safe(name, /, default=None, **kwargs):
        assert name == "task_list"
        return next(calls)

    return fake_safe


def test_truncated_page_without_cursor_is_reported_partial(monkeypatch):
    monkeypatch.setattr(
        api, "_safe", _pages({"tasks": [{"id": 1}], "truncated": True, "next_cursor": None})
    )
    out = anyio.run(lambda: api._task_list_all(status="open"))
    assert out["tasks"] == [{"id": 1}]
    assert out["truncated"] is True
    assert "no next_cursor" in out["error"]


def test_cursor_is_followed_to_a_complete_read(monkeypatch):
    monkeypatch.setattr(
        api,
        "_safe",
        _pages(
            {"tasks": [{"id": 1}], "truncated": True, "next_cursor": "c1"},
            {"tasks": [{"id": 2}], "truncated": False, "next_cursor": None},
        ),
    )
    out = anyio.run(lambda: api._task_list_all(status="open"))
    assert [t["id"] for t in out["tasks"]] == [1, 2]
    assert out == {"tasks": [{"id": 1}, {"id": 2}], "truncated": False}


def test_tasks_payload_surfaces_a_partial_read(monkeypatch):
    async def partial():
        return [{"id": 1, "status": "open", "project": "unified-agent"}], "open: page 2 failed"

    monkeypatch.setattr(api, "_active_queue_tasks", partial)
    out = anyio.run(api.tasks_payload)
    assert out["total"] == 1
    assert out["truncated"] is True
    assert out["error"] == "open: page 2 failed"


def test_tasks_payload_complete_read_has_no_error(monkeypatch):
    async def whole():
        return [{"id": 1, "status": "open", "project": "unified-agent"}], None

    monkeypatch.setattr(api, "_active_queue_tasks", whole)
    out = anyio.run(api.tasks_payload)
    assert "error" not in out and "truncated" not in out


def test_flow_metrics_surface_a_partial_read(monkeypatch):
    async def fake_all(status, **kwargs):
        if status == "doing":
            return {"tasks": [], "truncated": True, "error": "page 2 failed"}
        return {"tasks": [], "truncated": False}

    monkeypatch.setattr(api, "_task_list_all", fake_all)
    out = anyio.run(api.flow_metrics_payload)
    assert out["truncated"] is True
    assert out["error"] == "doing: page 2 failed"


def test_agents_payload_surfaces_a_partial_doing_read(monkeypatch):
    async def fake_all(status, **kwargs):
        assert status == "doing"
        return {"tasks": [], "truncated": True, "error": "page 2 failed"}

    async def no_sessions(limit=200):
        return {"sessions": []}

    monkeypatch.setattr(api, "_task_list_all", fake_all)
    monkeypatch.setattr(api, "sessions_payload", no_sessions)
    monkeypatch.setattr("unified_agent.tools.registry.select_fleet_agents", lambda: [])
    out = anyio.run(api.agents_payload)
    assert out["truncated"] is True
    assert out["error"] == "doing tasks: page 2 failed"
