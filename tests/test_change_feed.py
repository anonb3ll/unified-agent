"""change_feed carries task and run change notices from domain code to live views.

Domain modules (tools.tasks, routines, run_records, dispatch.report) publish
here instead of importing the MCP live-state adapter, which subscribes.
"""

from unified_agent import change_feed, mcp_live_state


def test_listener_receives_task_and_run_notices():
    seen = []

    def listener(kind, ids):
        seen.append((kind, ids))

    change_feed.subscribe(listener)
    change_feed.subscribe(listener)  # idempotent
    try:
        change_feed.task_changed(7)
        change_feed.task_changed()
        change_feed.runs_changed(["r1", None, "r1", "r2"])
        change_feed.runs_changed([None])
    finally:
        change_feed.unsubscribe(listener)
    assert seen == [("task", (7,)), ("task", ()), ("run", ("r1", "r2"))]


def test_failing_listener_does_not_break_the_write_path():
    calls = []

    def broken(kind, ids):
        raise RuntimeError("boom")

    def healthy(kind, ids):
        calls.append(kind)

    change_feed.subscribe(broken)
    change_feed.subscribe(healthy)
    try:
        change_feed.task_changed(1)
    finally:
        change_feed.unsubscribe(broken)
        change_feed.unsubscribe(healthy)
    assert calls == ["task"]


def test_mcp_live_state_publishes_notices_as_resource_uris(monkeypatch):
    published = []
    monkeypatch.setattr(mcp_live_state, "notify_uris", published.append)

    change_feed.task_changed(42)
    change_feed.runs_changed(["run-a"])

    assert published == [
        [mcp_live_state.BOARD_URI, mcp_live_state.task_uri(42)],
        [mcp_live_state.run_uri("run-a")],
    ]
