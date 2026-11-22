"""Control Center v2 knowledge routes (tasks #2466, #2469).

Each route adapts one registered tool through its guarded wrapper. These tests
drive the real scoped app (``routes.web_routes()`` behind the hub auth middleware)
with the fixture identities of ``test_route_scopes``: the developer agent
``Desktop_Agent2``, the chief of staff ``Laptop_Agent5``, the public ``server_agent8``
and the verified human operator.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, TOKENS, _bearer, _client
from test_route_scopes import _isolated as _isolated

from unified_agent import config, embeddings, profiles, server
from unified_agent.tools import approvals, memory
from unified_agent.webui import cc_knowledge, route_scopes, routes

DEV = "Desktop_Agent2"

#: Every write route with a body the tool accepts, and the tool its approval gate names.
WRITES = {
    "/api/action/memory/save": ({"content": "fixture note", "title": "t"}, "memory_save"),
    "/api/action/memory/delete": ({"key": "{key}"}, "memory_delete"),
    "/api/action/memory/restore": ({"key": "{key}"}, "memory_restore"),
    "/api/action/memory/promote": ({"key": "{key}", "dry_run": True}, "memory_promote"),
    "/api/action/memory/feedback": ({"key": "{key}", "signal": "helpful"}, "memory_feedback"),
    "/api/action/vault/write": ({"path": "Wiki/a.md", "content": "x"}, "vault_write"),
    "/api/action/vault/journal": ({"entry": "did a thing"}, "vault_journal"),
}
READS = {"/api/memory/note": {"key": "{key}"}, "/api/vault/list": {"dir": "Wiki"}}


@pytest.fixture(autouse=True)
def _knowledge(_isolated, tmp_path, monkeypatch):
    def offline(_text: str):
        raise RuntimeError("lexical-only test")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)
    vault = tmp_path / "vault"
    (vault / "Wiki").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    return vault


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _note(author: str = DEV, **extra) -> str:
    from unified_agent import config

    token = config.agent_identity_var.set(author)
    try:
        return memory.memory_save(content="seed note", title="seed", author=author, **extra)["id"]
    finally:
        config.agent_identity_var.reset(token)


def _fill(body: dict, key: str) -> dict:
    return {k: (key if v == "{key}" else v) for k, v in body.items()}


# --- happy paths ----------------------------------------------------------------------


def test_memory_note_returns_the_full_note(client):
    key = _note(confidence=0.9)
    res = client.get("/api/memory/note", params={"key": key}, headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    note = res.json()
    assert note["id"] == key
    assert note["content"] == "seed note"
    assert note["author"] == DEV
    assert {"tags", "updated_at", "links"} <= set(note)
    versions = client.get(
        "/api/memory/note", params={"key": key, "include_versions": "true"}, headers=_bearer(DEV)
    )
    assert "versions" in versions.json()


def test_memory_note_unknown_key_is_404(client):
    res = client.get("/api/memory/note", params={"key": "nope"}, headers=_bearer(DEV))
    assert res.status_code == 404
    assert "no memory note" in res.json()["error"]


@pytest.mark.parametrize("identity", [DEV, OPERATOR])
def test_memory_save_stamps_the_verified_author(client, identity):
    res = client.post(
        "/api/action/memory/save",
        headers=_bearer(identity),
        json={"content": "browser note", "title": "b", "tags": ["project:x", "fact"]},
    )
    assert res.status_code == 200, res.text
    assert memory.memory_get(res.json()["id"])["author"] == identity


def test_replacing_another_authors_note_needs_overwrite(client):
    key = _note(author="Laptop_Agent5")
    body = {"key": key, "content": "replaced"}
    refused = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert refused.status_code == 400, refused.text
    assert memory.memory_get(key)["content"] == "seed note"
    replaced = client.post(
        "/api/action/memory/save", headers=_bearer(DEV), json={**body, "overwrite": True}
    )
    assert replaced.status_code == 200, replaced.text
    assert memory.memory_get(key)["content"].startswith("replaced")


def test_memory_feedback_restore_promote_and_delete(client):
    key = _note(confidence=0.9)
    headers = _bearer(DEV)
    feedback = client.post(
        "/api/action/memory/feedback", headers=headers, json={"key": key, "signal": "helpful"}
    )
    assert feedback.status_code == 200 and feedback.json()["signal"] == "helpful"
    promote = client.post(
        "/api/action/memory/promote", headers=headers, json={"key": key, "dry_run": True}
    )
    assert promote.status_code == 200, promote.text
    restore = client.post("/api/action/memory/restore", headers=headers, json={"key": key})
    assert restore.status_code == 200 and restore.json()["id"] == key
    deleted = client.post("/api/action/memory/delete", headers=headers, json={"key": key})
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    again = client.post("/api/action/memory/restore", headers=headers, json={"key": "gone"})
    assert again.status_code == 404


def test_vault_write_journal_and_list(client, _knowledge):
    headers = _bearer(DEV)
    write = client.post(
        "/api/action/vault/write", headers=headers, json={"path": "Wiki/a.md", "content": "x"}
    )
    assert write.status_code == 200, write.text
    assert (_knowledge / "Wiki/a.md").read_text() == "x"
    journal = client.post("/api/action/vault/journal", headers=headers, json={"entry": "did x"})
    assert journal.status_code == 200, journal.text
    assert f"[{DEV}] did x" in journal.json()["entry"]
    listing = client.get("/api/vault/list", params={"dir": "Wiki"}, headers=headers)
    assert listing.status_code == 200
    assert [e["name"] for e in listing.json()["entries"]] == ["a.md"]


def test_vault_links_is_a_shared_authenticated_read(client, _knowledge):
    (_knowledge / "Wiki/a.md").write_text("see [[b]]")
    (_knowledge / "Wiki/b.md").write_text("back to [[a]]")
    params = {"path": "Wiki/a.md"}
    assert client.get("/api/vault/links", params=params).status_code == 401
    res = client.get("/api/vault/links", params=params, headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    assert {"outgoing", "backlinks"} <= set(res.json())
    assert client.get("/api/vault/links", headers=_bearer(DEV)).status_code == 400
    scope = route_scopes.scope_for("GET", "/api/vault/links")
    assert scope.access is route_scopes.Access.ANY_AUTHENTICATED
    assert scope.read_tools == ("vault_links",)


@pytest.mark.parametrize(
    "path,needle",
    [("Projects/a.md", "restricted"), ("../outside.md", "outside"), ("Wiki/locked.md", "locked")],
)
def test_vault_write_keeps_the_tools_own_guards(client, _knowledge, path, needle):
    (_knowledge / "Wiki/locked.md").write_text("---\nlocked: true\n---\nkeep\n")
    res = client.post(
        "/api/action/vault/write", headers=_bearer(DEV), json={"path": path, "content": "y"}
    )
    assert res.status_code == 400, res.text
    assert needle in res.json()["error"].lower()
    assert (_knowledge / "Wiki/locked.md").read_text().endswith("keep\n")


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*READS, *WRITES])
def test_unauthenticated_and_public_profile_are_refused(client, path):
    method = "get" if path in READS else "post"
    body = READS.get(path) or WRITES[path][0]
    kwargs = {"params": body} if method == "get" else {"json": body}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    denied = getattr(client, method)(path, headers=_bearer("server_agent8"), **kwargs)
    assert denied.status_code == 403
    assert "unavailable to this profile" in denied.json()["error"]


@pytest.mark.parametrize("path", list(WRITES))
def test_cookie_session_write_needs_the_csrf_token(path):
    session = _client([r for r in routes.web_routes() if r.path == "/api/session"])
    login = session.post("/api/session", json={"token": TOKENS["Laptop_Agent5"]})
    assert login.status_code == 200
    browser = TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=routes.web_routes())),
        base_url="http://127.0.0.1",
        cookies=dict(session.cookies),
    )
    res = browser.post(path, json=_fill(WRITES[path][0], "k"))
    assert res.status_code == 403
    assert "CSRF" in res.json()["error"]


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/memory/save", {"content": "x", "author": "Laptop_Agent5"}),
        ("/api/action/memory/promote", {"key": "k", "agent": "Laptop_Agent5"}),
        ("/api/action/vault/journal", {"entry": "x", "agent": "Laptop_Agent5"}),
    ],
)
def test_identity_named_in_the_body_must_be_the_verified_one(client, path, body):
    res = client.post(path, headers=_bearer(DEV), json=body)
    assert res.status_code == 400
    assert "authenticated identity" in res.json()["error"]
    field = "author" if "author" in body else "agent"
    same = client.post(path, headers=_bearer(DEV), json={**body, field: DEV})
    assert same.status_code != 400 or "authenticated identity" not in same.text


@pytest.mark.parametrize("path", list(WRITES))
def test_approval_gate_answers_202(client, path, monkeypatch):
    body, tool = WRITES[path]
    key = _note(confidence=0.9)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", tool)
    res = client.post(path, headers=_bearer(DEV), json=_fill(body, key))
    assert res.status_code == 202, res.text
    payload = res.json()
    assert payload["status"] == "approval_pending"
    assert payload["tool"] == tool
    assert payload["code"] == "approval_required"
    assert payload["approval_id"] in {row["id"] for row in approvals.approval_pending()["pending"]}


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/memory/save", {"content": "x", "colour": "red"}),
        ("/api/action/memory/save", {"content": 5}),
        ("/api/action/memory/save", {"content": "x", "tags": ["t"] * 51}),
        ("/api/action/memory/save", {"title": "no content"}),
        ("/api/action/memory/feedback", {"key": "k", "signal": "meh"}),
        ("/api/action/memory/delete", {"key": "k" * 201}),
        ("/api/action/vault/write", {"path": "Wiki/a.md", "mode": "prepend"}),
        ("/api/action/vault/journal", {"entry": "x", "tags": "not-a-list"}),
    ],
)
def test_bad_write_input_is_400(client, path, body):
    res = client.post(path, headers=_bearer(DEV), json=body)
    assert res.status_code == 400, res.text


@pytest.mark.parametrize("path", ["/api/action/memory/save", "/api/action/vault/write"])
def test_malformed_json_is_400(client, path):
    res = client.post(path, headers=_bearer(DEV), content=b"{not json")
    assert res.status_code == 400
    assert client.post(path, headers=_bearer(DEV), json=[1]).status_code == 400


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/memory/note", {}),
        ("/api/memory/note", {"key": "k", "colour": "red"}),
        ("/api/vault/list", {"max_entries": "lots"}),
        ("/api/vault/list", {"max_entries": "501"}),
    ],
)
def test_bad_read_input_is_400(client, path, params):
    assert client.get(path, headers=_bearer(DEV), params=params).status_code == 400


def test_every_route_is_declared_as_a_csrf_guarded_capability():
    for route in cc_knowledge.ROUTES:
        if route.path == "/api/vault/links":
            continue  # the shared read pattern, as /api/vault/page
        method = sorted(route.methods - {"HEAD"})[0]
        scope = route_scopes.scope_for(method, route.path)
        assert scope is not None and scope.access is route_scopes.Access.CAPABILITY
        assert scope.operator and scope.csrf == (method == "POST")
        assert scope.tools, route.path


async def test_oversized_body_is_refused_while_streaming(monkeypatch):
    """Codex #1507: the cap was checked only after the whole body was buffered."""
    from starlette.requests import Request

    monkeypatch.setattr(cc_knowledge, "MAX_BODY_BYTES", 1_000)
    pulled: list[int] = []

    async def receive():
        pulled.append(1)
        return {"type": "http.request", "body": b" " * 500, "more_body": len(pulled) < 50}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    refused = await cc_knowledge.read_body(Request(scope, receive))
    assert refused.status_code == 413
    assert len(pulled) < 50  # stopped as soon as the stream passed the cap


def test_oversized_declared_body_is_413(client, monkeypatch):
    monkeypatch.setattr(cc_knowledge, "MAX_BODY_BYTES", 1_000)
    for path in ("/api/action/memory/save", "/api/action/plan/create"):
        res = client.post(path, headers=_bearer(DEV), content=b"{" + b" " * 2_000 + b"}")
        assert res.status_code == 413, (path, res.text)


def test_journal_entry_limit_matches_the_tool(client, monkeypatch):
    """Codex #1507: the adapter allowed 20,000 chars; the tool caps 240 after collapsing
    whitespace, so an over-long entry could park an approval that can never run."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "vault_journal")
    over = client.post("/api/action/vault/journal", headers=_bearer(DEV), json={"entry": "x" * 241})
    assert over.status_code == 400, over.text
    assert "240" in over.json()["error"]
    assert approvals.approval_pending()["pending"] == []
    monkeypatch.delenv("UA_APPROVAL_TOOLS")
    spaced = "word " * 40 + " " * 300  # 199 chars once whitespace collapses
    ok = client.post("/api/action/vault/journal", headers=_bearer(DEV), json={"entry": spaced})
    assert ok.status_code == 200, ok.text


def test_operator_can_write_journal_and_promote_from_the_browser(client, _knowledge):
    """Codex #1507: operator=True admitted the operator, but the operator profile's
    vault write dirs are empty, so every vault write was refused."""
    headers = _bearer(OPERATOR)
    write = client.post(
        "/api/action/vault/write", headers=headers, json={"path": "Wiki/op.md", "content": "x"}
    )
    assert write.status_code == 200, write.text
    assert (_knowledge / "Wiki/op.md").read_text() == "x"
    journal = client.post("/api/action/vault/journal", headers=headers, json={"entry": "op did x"})
    assert journal.status_code == 200, journal.text
    key = _note(author=OPERATOR, confidence=0.9)
    promote = client.post("/api/action/memory/promote", headers=headers, json={"key": key})
    assert promote.status_code == 200, promote.text
    # Only the configured write dirs open up, and the locked-note guard stays.
    outside = client.post(
        "/api/action/vault/write", headers=headers, json={"path": "Projects/a.md", "content": "x"}
    )
    assert outside.status_code == 400
    (_knowledge / "Wiki/locked.md").write_text("---\nlocked: true\n---\nkeep\n")
    locked = client.post(
        "/api/action/vault/write", headers=headers, json={"path": "Wiki/locked.md", "content": "y"}
    )
    assert locked.status_code == 400 and "locked" in locked.json()["error"].lower()


def test_an_agent_on_the_operator_profile_gains_no_vault_write(client, monkeypatch):
    from unified_agent import config

    monkeypatch.setitem(profiles.DEFAULT_PROFILES_BY_IDENTITY, "Desktop_Agent8", profiles.OPERATOR)
    for path, body in (
        ("/api/action/vault/write", {"path": "Wiki/a.md", "content": "x"}),
        ("/api/action/vault/journal", {"entry": "x"}),
        ("/api/action/memory/promote", {"key": "k"}),
    ):
        assert client.post(path, headers=_bearer("Desktop_Agent8"), json=body).status_code == 403
    # Even inside a route authority, the grant is the human operator's alone.
    token = config.agent_identity_var.set("Desktop_Agent8")
    try:
        with (
            profiles.http_route_authority(
                "Desktop_Agent8", ("vault_write",), (), human_workflow=True
            ),
            profiles.http_operator_vault_write_authority(),
        ):
            assert profiles.vault_write_dirs() == ()
    finally:
        config.agent_identity_var.reset(token)
    # The operator outside the grant keeps the read-only profile.
    token = config.agent_identity_var.set(OPERATOR)
    try:
        assert profiles.vault_write_dirs() == ()
    finally:
        config.agent_identity_var.reset(token)


# --- Knowledge hub gaps (task #2515) --------------------------------------------------


def test_opening_a_note_is_not_a_recall(client):
    key = _note()
    for _ in range(3):
        res = client.get("/api/memory/note", params={"key": key}, headers=_bearer(DEV))
        assert res.status_code == 200 and res.json()["access_count"] == 0
    assert memory.memory_get(key, count_access=False)["access_count"] == 0


def test_a_save_from_a_stale_copy_is_409_with_the_current_revision(client):
    key = _note()
    read = client.get("/api/memory/note", params={"key": key}, headers=_bearer(DEV)).json()
    peer = client.post(
        "/api/action/memory/save",
        headers=_bearer(OPERATOR),
        json={"key": key, "content": "peer append", "append": True},
    )
    assert peer.status_code == 200, peer.text
    body = {"key": key, "content": "replace", "expected_revision": read["revision"]}
    stale = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert stale.status_code == 409, stale.text
    current = memory.memory_get(key, count_access=False)
    assert stale.json()["code"] == "conflict"
    assert stale.json()["current_revision"] == current["revision"] == peer.json()["revision"]
    assert "peer append" in current["content"]
    body["expected_revision"] = current["revision"]
    fresh = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert fresh.status_code == 200, fresh.text


def test_restore_route_undeletes_in_place(client):
    key = _note()
    headers = _bearer(DEV)
    assert client.post("/api/action/memory/delete", headers=headers, json={"key": key}).json() == {
        "deleted": True
    }
    other = client.post("/api/action/memory/restore", headers=_bearer(OPERATOR), json={"key": key})
    assert other.status_code == 200 and other.json()["was_deleted"] is True
    note = client.get("/api/memory/note", params={"key": key}, headers=headers).json()
    assert note["content"] == "seed note" and "deleted" not in note


def test_memory_list_and_search_filter_author_project_and_tags_in_the_tool(client):
    for author in (DEV, "Laptop_Agent5", DEV):
        _note(author=author, tags=["project:x", "fact"])
    _note(author=DEV, tags=["project:y", "fact"])
    params = {"author": DEV, "project": "x", "tag": "fact", "limit": "1"}
    for extra in ({}, {"q": "seed"}):
        res = client.get("/api/memory", params={**params, **extra}, headers=_bearer(DEV))
        assert res.status_code == 200, res.text
        payload = res.json()
        assert payload["tags"] == ["fact", "project:x"] and payload["author"] == DEV
        assert len(payload["results"]) == 1
        assert set(payload["results"][0]["tags"]) == {"fact", "project:x"}


def test_vault_page_carries_the_lock_verdict(client, _knowledge):
    (_knowledge / "Wiki/locked.md").write_text("---\nlocked: true\n---\nkeep\n")
    (_knowledge / "Wiki/open.md").write_text("open\n")
    for name, locked in (("locked", True), ("open", False)):
        res = client.get(
            "/api/vault/page", params={"path": f"Wiki/{name}.md"}, headers=_bearer(DEV)
        )
        assert res.status_code == 200, res.text
        assert res.json()["locked"] is locked


#: Route -> (body, a different body for the same key).
_IDEMPOTENT = {
    "/api/action/vault/write": (
        {"path": "Wiki/log.md", "content": "line\n", "mode": "append"},
        {"path": "Wiki/log.md", "content": "other\n", "mode": "append"},
    ),
    "/api/action/vault/journal": ({"entry": "retried entry"}, {"entry": "another entry"}),
}


def _vault_text(vault, path: str, body: dict) -> str:
    if path.endswith("journal"):
        return "".join(p.read_text() for p in (vault / "Logs").glob("*.md"))
    return (vault / body["path"]).read_text()


@pytest.mark.parametrize("path", list(_IDEMPOTENT))
def test_a_retried_vault_append_writes_once(client, _knowledge, path):
    body, different = _IDEMPOTENT[path]
    headers = {**_bearer(DEV), "Idempotency-Key": "k-1"}
    first = client.post(path, headers=headers, json=body)
    assert first.status_code == 200, first.text
    again = client.post(path, headers=headers, json=body)
    assert again.status_code == 200 and again.json() == first.json()
    needle = body.get("content", "").strip() or body.get("entry")
    assert _vault_text(_knowledge, path, body).count(needle) == 1
    changed = client.post(path, headers=headers, json=different)
    assert changed.status_code == 409 and changed.json()["code"] == "conflict"
    # The key belongs to this caller: another identity with it writes its own.
    mine = client.post(path, headers={**_bearer(OPERATOR), "Idempotency-Key": "k-1"}, json=body)
    assert mine.status_code == 200, mine.text
    assert _vault_text(_knowledge, path, body).count(needle) == 2
    bad = client.post(path, headers={**_bearer(DEV), "Idempotency-Key": " k "}, json=body)
    assert bad.status_code == 400


def test_a_keyed_vault_write_is_classified_by_phase_not_status(client, _knowledge, monkeypatch):
    """Review of #1532 (as #1538): a refusal before the execution boundary (a parked
    approval) drops the receipt, so the approved repeat writes once; the tool's own
    refusal (the locked-note guard) is past the boundary and keeps the key."""
    (_knowledge / "Wiki/locked.md").write_text("---\nlocked: true\n---\nkeep\n")
    body = {"path": "Wiki/locked.md", "content": "x\n", "mode": "append"}

    def post(key, payload=body):
        headers = {**_bearer(DEV), "Idempotency-Key": key}
        return client.post("/api/action/vault/write", headers=headers, json=payload)

    assert post("k-2").status_code == 400
    again = post("k-2")
    assert again.status_code == 409 and again.json()["code"] == "outcome_unknown", again.text
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "vault_write")
    gated = {"path": "Wiki/gated.md", "content": "g\n", "mode": "append"}
    parked = post("k-3", gated)
    assert parked.status_code == 202, parked.text
    token = config.agent_identity_var.set(OPERATOR)
    try:
        approvals.approval_resolve(parked.json()["approval_id"], "approve", resolved_by=OPERATOR)
    finally:
        config.agent_identity_var.reset(token)
    ran = post("k-3", gated)
    assert ran.status_code == 200, ran.text
    assert post("k-3", gated).json() == ran.json()
    assert (_knowledge / "Wiki/gated.md").read_text() == "g\n"


@pytest.mark.parametrize("path", list(_IDEMPOTENT))
def test_a_vault_append_whose_ledger_write_failed_is_never_run_again(
    client, _knowledge, path, monkeypatch
):
    """Review of #1532: the ledger write failed after the append, call() answered 400,
    the receipt was dropped, and the same key appended a second time."""
    from unified_agent import gateway

    body, _ = _IDEMPOTENT[path]
    tool = f"vault_{path.rsplit('/', 1)[-1]}"
    real_record = gateway.record_call_result

    def ledger_down(tool_name, call, error, *args, **kwargs):
        if tool_name == tool and error is None:
            raise gateway.LedgerWriteError("decision event could not be written")
        return real_record(tool_name, call, error, *args, **kwargs)

    headers = {**_bearer(DEV), "Idempotency-Key": "k-ledger"}
    monkeypatch.setattr(gateway, "record_call_result", ledger_down)
    assert client.post(path, headers=headers, json=body).status_code == 400
    monkeypatch.setattr(gateway, "record_call_result", real_record)
    again = client.post(path, headers=headers, json=body)
    assert again.status_code == 409 and again.json()["code"] == "outcome_unknown", again.text
    needle = body.get("content", "").strip() or body.get("entry")
    assert _vault_text(_knowledge, path, body).count(needle) == 1


def test_a_stale_save_on_a_deleted_note_is_409_with_the_current_revision(client):
    """Review of #1532: read, delete, then save from the read answered 400."""
    key = _note()
    read = client.get("/api/memory/note", params={"key": key}, headers=_bearer(DEV)).json()
    assert client.post("/api/action/memory/delete", headers=_bearer(DEV), json={"key": key})
    body = {"key": key, "content": "edit", "expected_revision": read["revision"]}
    stale = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert stale.status_code == 409, stale.text
    current = memory.memory_get(key, count_access=False)
    assert current["deleted"] is True
    assert stale.json()["current_revision"] == current["revision"]


def _age_receipts(older_than) -> None:
    from datetime import UTC, datetime

    from unified_agent import storage

    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - older_than * 2).isoformat()
        conn.execute("UPDATE idempotency_keys SET created_at=? WHERE tool LIKE 'cc_vault%'", (old,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("path", list(_IDEMPOTENT))
def test_an_unsettled_append_is_never_run_again(client, _knowledge, path, monkeypatch):
    """Review of #1532: the append landed, its receipt never settled, and after the
    takeover window a retry appended again. Now it answers outcome_unknown."""
    from unified_agent import storage

    body, _ = _IDEMPOTENT[path]
    headers = {**_bearer(DEV), "Idempotency-Key": "k-u"}
    settle = storage.request_receipt_finish

    def crash(*_args, **_kwargs):
        raise RuntimeError("hub died before the receipt settled")

    monkeypatch.setattr(storage, "request_receipt_finish", crash)
    with pytest.raises(RuntimeError):
        client.post(path, headers=headers, json=body)
    monkeypatch.setattr(storage, "request_receipt_finish", settle)
    _age_receipts(cc_knowledge.RECEIPT_PENDING_STALE)
    retry = client.post(path, headers=headers, json=body)
    assert retry.status_code == 409 and retry.json()["code"] == "outcome_unknown", retry.text
    needle = body.get("content", "").strip() or body.get("entry")
    assert _vault_text(_knowledge, path, body).count(needle) == 1


def test_an_append_cut_off_after_the_write_is_outcome_unknown(client, _knowledge, monkeypatch):
    """Review of #1532: an attempt that ends without an answer may have written, so the
    key must not run it again, even at once."""
    real_call = cc_knowledge.call

    async def cut_off(tool, /, **kwargs):
        await real_call(tool, **kwargs)
        raise RuntimeError("client went away")

    monkeypatch.setattr(cc_knowledge, "call", cut_off)
    headers = {**_bearer(DEV), "Idempotency-Key": "k-c"}
    body = {"entry": "cut off entry"}
    with pytest.raises(RuntimeError):
        client.post("/api/action/vault/journal", headers=headers, json=body)
    monkeypatch.setattr(cc_knowledge, "call", real_call)
    retry = client.post("/api/action/vault/journal", headers=headers, json=body)
    assert retry.status_code == 409 and retry.json()["code"] == "outcome_unknown", retry.text
    assert _vault_text(_knowledge, "/api/action/vault/journal", body).count("cut off entry") == 1


class _DiskFills:
    """A note handle whose first write lands in part and then fails, as on a full disk."""

    def __init__(self, handle):
        self._handle = handle

    def write(self, data):
        self._handle.write(data[: max(1, len(data) // 2)])
        self._handle.flush()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __iter__(self):
        return iter(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()


@pytest.mark.parametrize("path", list(_IDEMPOTENT))
def test_an_io_error_after_the_write_began_is_outcome_unknown_not_a_refusal(
    client, _knowledge, path, monkeypatch
):
    """Task #2521: an OSError partway through the append answered an ordinary 400
    refusal ("cannot append", no code), which reads as "nothing was written". It is
    409 outcome_unknown on the first answer and on every repeat of the key, and the
    identical repeat never appends a second time. An OSError before the first byte
    (the note cannot be opened) stays a plain refusal that names no outcome code."""
    from unified_agent.tools import vault as vault_tool

    body, _ = _IDEMPOTENT[path]
    needle = body.get("content", "").strip() or body.get("entry")
    headers = {**_bearer(DEV), "Idempotency-Key": "k-io"}
    monkeypatch.setattr(
        vault_tool, "open", lambda *a, **k: _DiskFills(open(*a, **k)), raising=False
    )
    first = client.post(path, headers=headers, json=body)
    assert first.status_code == 409 and first.json()["code"] == "outcome_unknown", first.text
    monkeypatch.delattr(vault_tool, "open")
    again = client.post(path, headers=headers, json=body)
    assert again.status_code == 409 and again.json()["code"] == "outcome_unknown", again.text
    assert _vault_text(_knowledge, path, body).count(needle) == 0  # only the torn part

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(vault_tool, "open", refuse, raising=False)
    refused = client.post(path, headers={**_bearer(DEV), "Idempotency-Key": "k-pre"}, json=body)
    assert refused.status_code == 400 and "code" not in refused.json(), refused.text
