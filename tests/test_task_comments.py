"""Append-only task comments, distinct from the notes spec (task #1423)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import mcp_live_state, profiles, server, storage
from unified_agent.task_comments import append_comment, get_comment, list_comments, task_comment
from unified_agent.tools.tasks import task_add
from unified_agent.webui.routes import web_routes

_MODULE = Path(__file__).resolve().parents[1] / "src/unified_agent/task_comments.py"


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()


def test_append_does_not_change_notes_and_replays(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Spec", notes="DoD: keep notes as the spec")
    first = append_comment(
        task["id"],
        "starting the slice",
        mentions=["Desktop_Agent2"],
        author="Desktop_Agent4",
        idempotency_key="c1",
    )
    replay = append_comment(
        task["id"],
        "a different body must not win",
        author="Desktop_Agent4",
        idempotency_key="c1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["comment"]["id"] == first["comment"]["id"]
    assert replay["comment"]["body"] == "starting the slice"
    listed = list_comments(task["id"])
    assert listed["revision"] == 1
    assert listed["notes_are_spec"] is True
    assert listed["comments"][0]["participants"] == ["Desktop_Agent4", "Desktop_Agent2"]
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (task["id"],)).fetchone()[
            "notes"
        ]
    finally:
        conn.close()
    assert notes == "DoD: keep notes as the spec"


def test_thread_refs_and_reply(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Thread")
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plans (id, goal, created_at, updated_at) VALUES (?,?,?,?)",
            ("plan-1", "ship", now, now),
        )
        conn.execute(
            "INSERT INTO memory (id, content, created_at, updated_at) VALUES (?,?,?,?)",
            ("dec-1", "use ancestry", now, now),
        )
        conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("Desktop_Agent4", "Desktop_Agent2", task["id"], "handoff", now, now),
        )
        handoff_id = conn.execute("SELECT id FROM delegations").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    parent = append_comment(task["id"], "proposal", author="Desktop_Agent4")
    reply = append_comment(
        task["id"],
        "agreed",
        parent_comment_id=parent["comment"]["id"],
        refs={"plan_id": "plan-1", "decision_id": "dec-1", "handoff_id": handoff_id},
        author="Desktop_Agent2",
    )
    assert reply["comment"]["refs"]["handoff_id"] == handoff_id
    assert reply["comment"]["parent_comment_id"] == parent["comment"]["id"]
    assert list_comments(task["id"])["revision"] == 2
    with pytest.raises(ToolError, match="unknown plan"):
        append_comment(task["id"], "nope", refs={"plan_id": "missing"}, author="Desktop_Agent4")
    stored = append_comment(
        task["id"],
        "keyed",
        refs={"plan_id": "plan-1"},
        idempotency_key="k1",
        author="Desktop_Agent4",
    )
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM plans WHERE id=?", ("plan-1",))
        conn.commit()
    finally:
        conn.close()
    replay = append_comment(
        task["id"],
        "keyed",
        refs={"plan_id": "plan-1"},
        idempotency_key="k1",
        author="Desktop_Agent4",
    )
    assert replay["idempotent_replay"] is True
    assert replay["comment"]["id"] == stored["comment"]["id"]


def test_update_and_delete_are_refused_and_unrouted(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Immutable")
    posted = append_comment(task["id"], "posted", author="Desktop_Agent4")
    with pytest.raises(ToolError, match="append-only"):
        task_comment("delete", {"task_id": task["id"]})
    source = _MODULE.read_text()
    schema = (_MODULE.parent / "table_schemas.py").read_text()
    assert "UPDATE task_comments" not in source
    assert "UPDATE task_comments" not in schema
    # The only delete is the task-row cascade, defined with the table DDL in
    # table_schemas.py. Comments have no delete API.
    assert "DELETE FROM task_comments" not in source
    assert schema.count("DELETE FROM task_comments") == 1
    assert "task_comments_on_task_delete" in schema
    comment_routes = [route for route in web_routes() if route.path.endswith("/comments")]
    assert comment_routes
    for route in comment_routes:
        methods = set(route.methods or ()) - {"HEAD", "OPTIONS"}
        assert methods <= {"GET", "POST"}
        assert "PUT" not in methods
        assert "DELETE" not in methods
        assert "PATCH" not in methods
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
        conn.commit()
    finally:
        conn.close()
    assert get_comment(posted["comment"]["id"]) is None


def test_idempotency_conflict_replays_the_winner(monkeypatch, tmp_path):
    """A unique-index miss after the pre-check returns the stored comment."""
    _db(monkeypatch, tmp_path)
    task = task_add(title="Race")
    first = append_comment(
        task["id"],
        "original",
        author="Desktop_Agent4",
        idempotency_key="same-key",
    )
    lookups = {"n": 0}
    real_connect = storage.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        original = conn.execute

        def execute(sql, *a, **kw):
            if isinstance(sql, str) and "idempotency_key=?" in sql and "SELECT" in sql.upper():
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
    replay = append_comment(
        task["id"],
        "must not replace the original",
        author="Desktop_Agent4",
        idempotency_key="same-key",
    )
    assert replay["idempotent_replay"] is True
    assert replay["comment"]["id"] == first["comment"]["id"]
    assert replay["comment"]["body"] == "original"
    assert list_comments(task["id"])["revision"] == 1
    assert lookups["n"] >= 2


def test_comment_poll_uses_revision_not_body(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Poll")
    posted = append_comment(task["id"], "short", author="Desktop_Agent4")
    comment_id = posted["comment"]["id"]
    uri = f"unified-agent://comments/{comment_id}"
    before = mcp_live_state.snapshot()[uri]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_comments SET body=? WHERE id=?",
            ("a much longer historical body that must not move the revision", comment_id),
        )
        conn.commit()
    finally:
        conn.close()
    assert mcp_live_state.snapshot()[uri] == before
    second = append_comment(task["id"], "another", author="Desktop_Agent4")
    after = mcp_live_state.snapshot()
    assert after[f"unified-agent://comments/{second['comment']['id']}"]
    assert after[uri] == before


def test_comment_reads_require_the_task_list_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    task = task_add(title="Thread")
    append_comment(task["id"], "hello", author="Desktop_Agent4")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        allowed = client.get(f"/api/tasks/{task['id']}/comments")
        assert allowed.status_code == 200
        assert allowed.json()["comments"]
        with patch.object(profiles, "tool_is_allowed_for", return_value=False):
            denied = client.get(f"/api/tasks/{task['id']}/comments")
        assert denied.status_code == 403
        assert "not exposed" in denied.json()["error"]


# --- a credential-shaped author is refused (task #1893) ----------------------------


def _inert_credential() -> str:
    import secrets

    from unified_agent import session_credentials

    return session_credentials.PREFIX + secrets.token_urlsafe(32)


def _comment_rows() -> list:
    conn = storage.connect()
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM task_comments")] + [
            tuple(r) for r in conn.execute("SELECT * FROM task_comment_threads")
        ]
    finally:
        conn.close()


def test_a_credential_shaped_author_is_refused_and_writes_nothing(monkeypatch, tmp_path):
    from unified_agent import config

    _db(monkeypatch, tmp_path)
    monkeypatch.delenv("UA_TRANSPORT", raising=False)
    task = task_add(title="Thread")
    value = _inert_credential()
    before = _comment_rows()
    for identity in (None, "Local_Verified"):
        token = config.agent_identity_var.set(identity)
        try:
            with pytest.raises(ToolError) as exc:
                append_comment(task_id=task["id"], body="hello", author=value)
        finally:
            config.agent_identity_var.reset(token)
        message = str(exc.value)
        assert "author" in message and value[4:14] not in message
        assert _comment_rows() == before


def test_comment_authors_are_otherwise_unchanged(monkeypatch, tmp_path):
    from unified_agent import config

    _db(monkeypatch, tmp_path)
    monkeypatch.delenv("UA_TRANSPORT", raising=False)
    task = task_add(title="Thread")
    plain = append_comment(task_id=task["id"], body="hello", author="Local_Writer")
    assert plain["comment"]["author"] == "Local_Writer"
    token = config.agent_identity_var.set("Local_Verified")
    try:
        stamped = append_comment(task_id=task["id"], body="again", author="Local_Writer")
    finally:
        config.agent_identity_var.reset(token)
    assert stamped["comment"]["author"] == "Local_Verified"


# --- a mention notifies, and says nothing the thread said (task #2392) ---


def _mailbox(label: str) -> list[dict]:
    from unified_agent.messaging import index as message_index

    conn = storage.connect()
    try:
        return [dict(row) for row in message_index.query(conn, recipient=label)]
    finally:
        conn.close()


def test_a_mention_sends_one_pointer_notice_and_never_the_body(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Mentioned work")
    out = append_comment(
        task_id=task["id"],
        body="secret-thread-text: the deploy token is being rotated",
        mentions=["Desktop_Agent2"],
        author="Desktop_Agent4",
    )
    assert out["notified_mentions"] == ["Desktop_Agent2"]
    [notice] = _mailbox("Desktop_Agent2")
    assert notice["task_id"] == task["id"]
    assert f"Task #{task['id']}" in notice["body"]
    assert "Desktop_Agent4" in notice["body"]
    assert "secret-thread-text" not in notice["body"]


def test_a_mention_of_the_author_is_not_a_notice(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Self mention")
    out = append_comment(
        task_id=task["id"],
        body="note to self",
        mentions=["Desktop_Agent4"],
        author="Desktop_Agent4",
    )
    assert "notified_mentions" not in out
    assert _mailbox("Desktop_Agent4") == []


def test_an_idempotent_replay_does_not_notify_twice(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    task = task_add(title="Replayed mention")
    first = append_comment(
        task_id=task["id"],
        body="look",
        mentions=["Desktop_Agent2"],
        author="Desktop_Agent4",
        idempotency_key="m1",
    )
    replay = append_comment(
        task_id=task["id"],
        body="look again",
        mentions=["Desktop_Agent2"],
        author="Desktop_Agent4",
        idempotency_key="m1",
    )
    assert first["comment"]["id"] == replay["comment"]["id"] and replay["idempotent_replay"] is True
    assert len(_mailbox("Desktop_Agent2")) == 1


def test_a_mention_list_over_the_fan_out_cap_is_refused(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("UA_MESSAGE_MAX_RECIPIENTS", "1")
    task = task_add(title="Too many mentions")
    with pytest.raises(ToolError, match="at most 1"):
        append_comment(
            task_id=task["id"],
            body="everyone",
            mentions=["Desktop_Agent2", "Desktop_Agent5"],
            author="Desktop_Agent4",
        )
