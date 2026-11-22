import asyncio
import uuid
from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from test_pilot_auth import Provider

from unified_agent import events
from unified_agent.pilot_app import WORKFLOW, _check_boundary, create_pilot_app
from unified_agent.pilot_auth import PilotAuth
from unified_agent.tools.approvals import approval_resolve


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "pilot"))
    monkeypatch.setenv("UA_PILOT_PRIVATE_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    monkeypatch.setenv("UA_EVENT_BUS", "0")
    monkeypatch.setenv("UA_VAULT_PATH", "")
    provider = Provider()
    auth = PilotAuth(
        tmp_path / "pilot" / "auth.sqlite", "org-a", {"user-a": "pilot-reader"}, provider
    )
    app = create_pilot_app(auth, origin="https://pilot.example")
    return auth, app, provider


async def login(client):
    start = await client.get("/pilot/login")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return await client.get("/pilot/callback", params={"code": "valid", "state": state})


@pytest.mark.asyncio
async def test_http_login_csrf_provider_revocation_logout_and_no_private_routes(
    tmp_path, monkeypatch
):
    auth, app, provider = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        start = await client.get("/pilot/login")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://pilot.example"
        ) as stranger:
            assert (
                await stranger.get("/pilot/callback", params={"code": "valid", "state": state})
            ).status_code == 401
        callback = await client.get("/pilot/callback", params={"code": "valid", "state": state})
        assert callback.status_code == 303
        cookies = callback.headers.get_list("set-cookie")
        assert any(
            "pilot_session=" in c and "HttpOnly" in c and "Secure" in c and "SameSite=lax" in c
            for c in cookies
        )
        assert (await client.get("/pilot/discovery")).json()["profile"] == "pilot-reader"
        for path in ["/api/tasks", "/api/approvals", "/api/platform/objects/task/1", "/mcp"]:
            assert (await client.get(path)).status_code == 404
        csrf = client.cookies["pilot_csrf"]
        assert (await client.post("/pilot/logout")).status_code == 403
        assert (
            await client.post(
                "/pilot/logout", headers={"x-ua-csrf": csrf, "Origin": "https://evil.example"}
            )
        ).status_code == 403
        provider.enabled = False
        assert (await client.get("/pilot/discovery")).status_code == 401
        provider.enabled = True
        logout = await client.post("/pilot/logout", headers={"x-ua-csrf": csrf})
        assert logout.status_code == 303 and logout.headers["location"].startswith(
            "https://auth.example/logout"
        )
        assert (await client.get("/pilot/discovery")).status_code == 401


@pytest.mark.asyncio
async def test_session_cookies_are_lax_and_mutations_still_need_csrf(tmp_path, monkeypatch):
    # Task #2579: sign-in reaches /pilot/callback on a redirect chain that starts
    # at the identity provider. Browsers withhold SameSite=Strict cookies on that
    # chain, so the first page after sign-in answered 401. Lax fixes the
    # navigation; the header CSRF check and the Origin check still guard writes.
    _auth, app, _provider = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        callback = await login(client)
        assert callback.status_code == 303
        cookies = callback.headers.get_list("set-cookie")
        for name in ("pilot_session=", "pilot_csrf="):
            (line,) = [c for c in cookies if c.startswith(name)]
            assert "SameSite=lax" in line and "Secure" in line and "Path=/pilot" in line
            assert "strict" not in line.lower()
        assert (await client.get("/pilot/discovery")).status_code == 200
        body = {"request_id": str(uuid.uuid4())}
        assert (await client.post("/pilot/workflow", json=body)).status_code == 403
        assert (
            await client.post("/pilot/workflow", json=body, headers={"x-ua-csrf": "wrong"})
        ).status_code == 403
        csrf = client.cookies["pilot_csrf"]
        foreign = {"x-ua-csrf": csrf, "Origin": "https://evil.example"}
        assert (await client.post("/pilot/workflow", json=body, headers=foreign)).status_code == 403
        allowed = await client.post("/pilot/workflow", json=body, headers={"x-ua-csrf": csrf})
        assert allowed.status_code == 409


@pytest.mark.asyncio
async def test_real_pending_denied_approved_gate_and_sanitized_evidence(tmp_path, monkeypatch):
    auth, app, provider = setup(tmp_path, monkeypatch)
    from unified_agent import storage

    # The pilot itself must never reach the private event bus. A hub message may
    # fan out its own event on the messaging channels (task #2435: an approval
    # decision now sends one receipt message to the requesting agent), so those
    # are allowed here; anything else still fails.
    def _pilot_publish(event, *_args, **_kwargs):
        if str(event.get("channel", "")).startswith("agent.message"):
            return False
        pytest.fail("pilot must not publish to private IPC")

    monkeypatch.setattr(events, "publish", _pilot_publish)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        await login(client)
        headers = {"x-ua-csrf": client.cookies["pilot_csrf"]}
        body = {"request_id": str(uuid.uuid4())}
        pending = await client.post("/pilot/workflow", json=body, headers=headers)
        assert pending.status_code == 409
        approval_id = pending.json()["approval_id"]
        with storage.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            assert row["tool"] == WORKFLOW
            conn.execute(
                "UPDATE approvals SET status='denied', reason='operator denial' WHERE id=?",
                (approval_id,),
            )
            conn.commit()
        assert (await client.post("/pilot/workflow", json=body, headers=headers)).status_code == 403
        body = {"request_id": str(uuid.uuid4())}
        pending = await client.post("/pilot/workflow", json=body, headers=headers)
        approval_id = pending.json()["approval_id"]
        # The separate human operator must use UA_EVENT_BUS=0 too.
        assert (
            approval_resolve(approval_id, "approve", resolved_by="Operator")["status"] == "approved"
        )
        result = await client.post("/pilot/workflow", json=body, headers=headers)
        assert result.status_code == 200
        assert set(result.json()) == {"event_id", "approval_id", "organization_id"}
        assert (await client.post("/pilot/workflow", json=body, headers=headers)).status_code == 409
        with storage.connect() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM event_log WHERE channel='pilot.verification'"
                ).fetchone()[0]
                == 1
            )
        assert (
            await client.post("/pilot/workflow", json={**body, "approved": True}, headers=headers)
        ).status_code == 400
        for malformed in [None, [], {"request_id": 1}, {"request_id": "x" * 2000}]:
            assert (
                await client.post("/pilot/workflow", json=malformed, headers=headers)
            ).status_code == 400
        assert (await client.post("/pilot/lifecycle", content=b"x" * 65537)).status_code == 400
        assert (await client.post("/pilot/lifecycle", content=b"{}")).status_code == 400
        assert (
            await client.post(
                "/pilot/lifecycle", content=b"{}", headers={"workos-signature": "verified-fixture"}
            )
        ).status_code == 200
        assert (await client.get("/pilot/discovery")).status_code == 401


@pytest.mark.asyncio
async def test_pilot_approval_consumption_and_revocation_race(tmp_path, monkeypatch):
    auth, app, provider = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        await login(client)
        headers = {"x-ua-csrf": client.cookies["pilot_csrf"]}
        body = {"request_id": str(uuid.uuid4())}
        pending = await client.post("/pilot/workflow", json=body, headers=headers)
        approval_id = pending.json()["approval_id"]
        approval_resolve(approval_id, "approve", resolved_by="Operator")

        consumed, revoked = await asyncio.gather(
            client.post("/pilot/workflow", json=body, headers=headers),
            asyncio.to_thread(approval_resolve, approval_id, "revoke", "Operator"),
            return_exceptions=True,
        )

        if consumed.status_code == 200:
            assert isinstance(revoked, Exception)
        else:
            assert consumed.status_code == 409
            assert revoked["status"] == "revoked"


def test_process_boundary_and_rollback(tmp_path, monkeypatch):
    auth, app, provider = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_HOME", str(tmp_path / "private"))
    with pytest.raises(ValueError, match="separate process"):
        create_pilot_app(auth, origin="https://pilot.example")
    # New adapter cannot mutate the existing private authentication function.
    from unified_agent.webui.auth import cookie_paths_cover

    assert cookie_paths_cover("/api/session") and not cookie_paths_cover("/pilot/discovery")


def test_org_a_cannot_read_org_b_session_or_ledger_rows(tmp_path, monkeypatch):
    """Two dedicated pilot stores must not share cookies or platform rows."""
    from unified_agent import storage

    monkeypatch.setenv("UA_PILOT_PRIVATE_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    monkeypatch.setenv("UA_EVENT_BUS", "0")
    monkeypatch.setenv("UA_VAULT_PATH", "")

    root_a, root_b = tmp_path / "org-a", tmp_path / "org-b"
    monkeypatch.setenv("UA_HOME", str(root_a))
    auth_a = PilotAuth(root_a / "auth.sqlite", "org-a", {"user-a": "pilot-reader"}, Provider())
    create_pilot_app(auth_a, origin="https://pilot-a.example")

    monkeypatch.setenv("UA_HOME", str(root_b))
    provider_b = Provider()
    provider_b.claims = replace(
        provider_b.claims, subject="user-b", organization_id="org-b", session_id="session-b"
    )
    auth_b = PilotAuth(root_b / "auth.sqlite", "org-b", {"user-b": "pilot-reader"}, provider_b)
    create_pilot_app(auth_b, origin="https://pilot-b.example")
    session_b = auth_b.login("valid", auth_b.begin_login())["id"]
    b_event_id = events.record_event(
        "pilot.verification",
        {"organization_id": "org-b"},
        originator="pilot-reader",
        notify=False,
    )
    with closing(storage.connect()) as conn:
        assert conn.execute("SELECT 1 FROM event_log WHERE ulid=?", (b_event_id,)).fetchone()
    monkeypatch.setenv("UA_HOME", str(root_a))
    assert auth_b.local_session(session_b) is not None
    assert auth_a.local_session(session_b) is None
    assert auth_a.resolve(session_b) is None
    with closing(storage.connect()) as conn:
        assert (
            conn.execute("SELECT 1 FROM event_log WHERE ulid=?", (b_event_id,)).fetchone() is None
        )
    with pytest.raises(ValueError, match="separate process"):
        _check_boundary(auth_b)


@pytest.mark.asyncio
async def test_logout_provider_failure_still_clears_local_cookies(tmp_path, monkeypatch):
    auth, app, provider = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        await login(client)
        session_id = client.cookies["pilot_session"]

        def unavailable(_):
            raise ValueError("provider-secret-error")

        provider.logout_url = unavailable
        result = await client.post(
            "/pilot/logout", headers={"x-ua-csrf": client.cookies["pilot_csrf"]}
        )
        assert result.status_code == 200
        assert result.json() == {"logged_out": True, "provider_logout": "unavailable"}
        assert "pilot_session" not in client.cookies and "pilot_csrf" not in client.cookies
        assert auth.resolve(session_id) is None


@pytest.mark.asyncio
async def test_logout_revokes_local_session_during_provider_outage(tmp_path, monkeypatch):
    auth, app, provider = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://pilot.example"
    ) as client:
        await login(client)
        session_id = client.cookies["pilot_session"]
        provider.enabled = False
        result = await client.post(
            "/pilot/logout", headers={"x-ua-csrf": client.cookies["pilot_csrf"]}
        )
        assert result.status_code == 303
        assert "pilot_session" not in client.cookies and "pilot_csrf" not in client.cookies
        provider.enabled = True
        assert auth.resolve(session_id) is None


def test_pilot_requires_event_bus_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "pilot"))
    monkeypatch.setenv("UA_PILOT_PRIVATE_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    monkeypatch.delenv("UA_EVENT_BUS", raising=False)
    auth = PilotAuth(
        tmp_path / "pilot" / "auth.sqlite", "org-a", {"user-a": "pilot-reader"}, Provider()
    )
    with pytest.raises(ValueError, match="UA_EVENT_BUS=0"):
        create_pilot_app(auth, origin="https://pilot.example")
