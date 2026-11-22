"""Hook inbox endpoints: bearer-only session registration and unread digest (task #1861)."""

import secrets
import traceback

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import registry, server, storage, webui
from unified_agent.messaging import index as message_index
from unified_agent.messaging import send as messaging_send

ME = "Desktop_Agent2"
PEER = "Desktop_Agent1"


@pytest.fixture
def env(monkeypatch):
    tokens = {ME: secrets.token_urlsafe(24), PEER: secrets.token_urlsafe(24)}
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in tokens.items()))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    with TestClient(server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))) as http:
        yield http, tokens


def _auth(tokens, who=ME):
    return {"Authorization": f"Bearer {tokens[who]}"}


def _register(http, tokens, who=ME, external="ext-1", **body):
    return http.post(
        "/api/inbox/session",
        headers=_auth(tokens, who),
        json={"external_session_id": external, **body},
    )


def _send(to=ME, body="hello", to_session=None, frm=PEER, **kwargs):
    return messaging_send.send_message(
        act="inform",
        to_agent=None if to_session else to,
        to_session=to_session,
        body=body,
        from_agent=frm,
        **kwargs,
    )


def _summary(http, tokens, who=ME, **params):
    return http.get("/api/inbox/summary", headers=_auth(tokens, who), params=params)


def _read_state():
    with storage.connect() as conn:
        index = conn.execute(
            "SELECT message_id, read_at, acked_at FROM message_index ORDER BY message_id"
        ).fetchall()
        reads = conn.execute("SELECT count(*) FROM message_reads").fetchone()[0]
    return [tuple(r) for r in index], reads


# --- authentication ------------------------------------------------------------------


def test_unauthenticated_requests_are_refused(env):
    http, _ = env
    assert http.post("/api/inbox/session", json={"external_session_id": "x"}).status_code == 401
    assert http.get("/api/inbox/summary").status_code == 401


def test_a_cookie_session_is_refused_even_with_csrf(env):
    http, tokens = env
    login = http.post("/api/session", json={"token": tokens[ME]})
    assert login.status_code == 200
    csrf = {"x-ua-csrf": login.json()["csrf"]}
    assert http.get("/api/inbox/summary", headers=csrf).status_code == 401
    res = http.post("/api/inbox/session", headers=csrf, json={"external_session_id": "x"})
    assert res.status_code == 401
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM agent_sessions").fetchone()[0] == 0


def test_a_bad_origin_is_refused(env):
    http, tokens = env
    headers = {**_auth(tokens), "Origin": "https://untrusted.example"}
    assert http.get("/api/inbox/summary", headers=headers).status_code == 403


# --- session registration ------------------------------------------------------------


def test_register_is_idempotent_and_uses_the_bearer_identity(env):
    http, tokens = env
    first = _register(http, tokens, host="box", cwd="/work/repo")
    assert first.status_code == 200
    body = first.json()
    assert body["agent"] == ME and body["session_id"]
    again = _register(http, tokens)
    assert again.json() == body
    # The same external id under another agent is a different session.
    other = _register(http, tokens, who=PEER).json()
    assert other["agent"] == PEER and other["session_id"] != body["session_id"]
    with storage.connect() as conn:
        rows = conn.execute("SELECT agent, host FROM agent_sessions ORDER BY agent").fetchall()
    assert [(r["agent"], r["host"]) for r in rows][1] == (ME, "box")
    assert len(rows) == 2


def test_register_ignores_an_agent_in_the_body_and_validates_input(env):
    http, tokens = env
    res = _register(http, tokens, agent=PEER)
    assert res.json()["agent"] == ME
    headers = _auth(tokens)
    assert http.post("/api/inbox/session", headers=headers, json={}).status_code == 400
    assert http.post("/api/inbox/session", headers=headers, json=[]).status_code == 400
    assert (
        http.post(
            "/api/inbox/session", headers=headers, json={"external_session_id": 5}
        ).status_code
        == 400
    )
    assert _register(http, tokens, external="x" * 600).status_code == 400


def test_register_heartbeats_the_session(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE agent_sessions SET last_heartbeat_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (session_id,),
        )
        conn.commit()
    _register(http, tokens)
    assert (registry.get_session(session_id) or {})["last_seen_at"] > "2021"


# --- summary -------------------------------------------------------------------------


def test_summary_lists_unread_newest_first_and_changes_no_read_state(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    ids = [_send(body=f"mail {n}\nsecond line")["message_id"] for n in range(3)]
    own = _send(to_session=session_id, body="for the session", expects_reply=True)
    _send(to=PEER, frm=ME, body="someone else's mail")
    before = _read_state()

    res = _summary(http, tokens, session_id=session_id)
    assert res.status_code == 200
    body = res.json()
    assert body["agent"] == ME and body["session_id"] == session_id
    assert body["unread"] == 4
    listed = [m["message_id"] for m in body["messages"]]
    assert listed == [own["message_id"], *reversed(ids)]
    seqs = [m["seq"] for m in body["messages"]]
    assert seqs == sorted(seqs, reverse=True)
    assert body["newest_seq"] == seqs[0]
    assert "next_seq" not in body  # a snapshot, not a pager
    first = body["messages"][0]
    assert first["from_agent"] == PEER and first["act"] == "inform"
    assert first["expects_reply"] is True and first["preview"] == "for the session"
    assert set(first) == {
        "message_id",
        "seq",
        "from_agent",
        "act",
        "conversation_id",
        "task_id",
        "expects_reply",
        "created_at",
        "preview",
    }
    assert body["messages"][1]["preview"] == "mail 2"

    assert _read_state() == before
    assert _summary(http, tokens, session_id=session_id).json()["unread"] == 4


def test_summary_without_a_session_sees_label_mail_only(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    _send(body="label mail")
    _send(to_session=session_id, body="session mail")
    body = _summary(http, tokens).json()
    assert body["unread"] == 1 and body["session_id"] is None
    assert [m["preview"] for m in body["messages"]] == ["label mail"]


def test_newest_seq_and_unread_cover_more_than_the_page(env):
    http, tokens = env
    ids = [_send(body=f"m{n}")["message_id"] for n in range(30)]
    body = _summary(http, tokens, limit=2).json()
    assert body["unread"] == 30 and len(body["messages"]) == 2
    assert [m["message_id"] for m in body["messages"]] == [ids[29], ids[28]]
    assert body["newest_seq"] == body["messages"][0]["seq"]
    assert len(_summary(http, tokens, limit=500).json()["messages"]) == 20
    assert len(_summary(http, tokens).json()["messages"]) == 5


def test_after_seq_limits_the_snapshot_to_newer_mail(env):
    http, tokens = env
    for n in range(4):
        _send(body=f"m{n}")
    full = _summary(http, tokens, limit=20).json()
    cursor = full["messages"][2]["seq"]  # the second oldest message
    body = _summary(http, tokens, after_seq=cursor).json()
    assert [m["seq"] for m in body["messages"]] == [m["seq"] for m in full["messages"][:2]]
    assert body["unread"] == 4 and body["newest_seq"] == full["newest_seq"]
    empty = _summary(http, tokens, after_seq=full["newest_seq"]).json()
    assert empty["messages"] == [] and empty["unread"] == 4


def test_summary_excludes_mail_the_session_or_label_read(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    read_by_session = _send(body="a")["message_id"]
    read_by_label = _send(body="b")["message_id"]
    _send(body="c")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        message_index.mark_read(conn, ME, [read_by_session], session_id)
        message_index.mark_read(conn, ME, [read_by_label], None)
        conn.commit()
    finally:
        conn.close()
    body = _summary(http, tokens, session_id=session_id).json()
    assert [m["preview"] for m in body["messages"]] == ["c"]
    assert body["unread"] == 1


def test_preview_is_the_first_line_truncated_to_160_characters(env):
    http, tokens = env
    _send(body="x" * 400 + "\nnext")
    _send(body="\n\n  padded first line  \nsecond")
    _send(body="")
    previews = [m["preview"] for m in _summary(http, tokens).json()["messages"]]
    assert previews == ["", "padded first line", "x" * 160]


def test_another_agents_session_is_refused_and_unknown_looks_the_same(env):
    http, tokens = env
    theirs = _register(http, tokens, who=PEER).json()["session_id"]
    stolen = _summary(http, tokens, session_id=theirs)
    unknown = _summary(http, tokens, session_id="01NOSUCHSESSION")
    assert stolen.status_code == unknown.status_code == 403
    assert stolen.json()["error"].replace(theirs, "S") == unknown.json()["error"].replace(
        "01NOSUCHSESSION", "S"
    )


def test_summary_touches_the_session_heartbeat(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE agent_sessions SET last_heartbeat_at='2020-01-01T00:00:00+00:00', "
            "status='abandoned' WHERE id=?",
            (session_id,),
        )
        conn.commit()
    assert _summary(http, tokens, session_id=session_id).status_code == 200
    row = registry.get_session(session_id)
    assert row["last_seen_at"] > "2021" and row["status"] == "running"


def test_summary_rejects_bad_parameters(env):
    http, tokens = env
    for params in ({"limit": "x"}, {"limit": 0}, {"after_seq": -1}, {"after_seq": "z"}):
        assert _summary(http, tokens, **params).status_code == 400, params


def test_summary_opens_one_connection_of_its_own(env, monkeypatch):
    """The central auth gate reads the token table itself; the route adds exactly one."""
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    _send(body="hi")
    from_route = []
    real = storage.connect

    def counting(*args, **kwargs):
        stack = traceback.format_stack(limit=12)
        if any("_inbox_summary_sync" in frame for frame in stack):
            from_route.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(storage, "connect", counting)
    assert _summary(http, tokens, session_id=session_id).status_code == 200
    assert len(from_route) == 1


def test_preview_skips_a_long_blank_preamble(env):
    http, tokens = env
    _send(body=(" \n" * 700) + "the real first line")
    body = _summary(http, tokens).json()
    assert body["messages"][0]["preview"] == "the real first line"


def test_session_owed_reports_the_callers_own_session_only(env):
    http, tokens = env
    session_id = _register(http, tokens).json()["session_id"]
    url = "/api/inbox/session/owed"
    res = http.post(url, headers=_auth(tokens), json={"session_id": session_id})
    assert res.status_code == 200
    assert res.json()["count"] == 0 and res.json()["report_only"] is True
    other = http.post(url, headers=_auth(tokens, PEER), json={"session_id": session_id})
    assert other.status_code == 404
    assert http.post(url, headers=_auth(tokens), json={}).status_code == 400
    assert http.post(url, json={"session_id": session_id}).status_code == 401
