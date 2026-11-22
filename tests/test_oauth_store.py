"""Durable hub OAuth store (task #1321, Epic J2).

A hub restart must log no device out: registered clients and refresh tokens
persist in the hub DB, refresh tokens and client secrets only as SHA-256
digests. Per-device revoke drops a device's sessions (approval-gated).
"""

from __future__ import annotations

import hashlib
import re

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, oauth, oauth_store, safety, storage
from unified_agent.server import HubAuthASGIMiddleware
from unified_agent.tools import oauth_devices
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve
from unified_agent.tools.doctor import _oauth_store_check

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT = "unified-agent-cli"
NODE = {"node": "mac.tail1234.ts.net", "node_id": "nMAC1", "user": "operator@example.com"}


@pytest.fixture(autouse=True)
def _reset_oauth(monkeypatch):
    oauth.reset_provider()
    oauth_store.reset_write_failures()
    monkeypatch.setattr(oauth, "_resolve_peer_device", lambda ip: dict(NODE))
    yield
    oauth.reset_provider()
    oauth_store.reset_write_failures()


def _app(monkeypatch) -> TestClient:
    """Build the hub app. Calling it again simulates a hub restart."""
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "http://127.0.0.1:8765")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "hub-pass")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent8")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    oauth.reset_provider()

    async def homepage(request):
        return JSONResponse({"identity": config.verified_agent_identity()})

    # Not "/": exact "/" is public and redirects in the real hub (task
    # #1400), which would defeat the 401/identity assertions below.
    app = Starlette(routes=[Route("/whoami", homepage, methods=["GET"]), *oauth.http_routes()])
    return TestClient(HubAuthASGIMiddleware(app), follow_redirects=False)


def _device_sign_in(client: TestClient, agent: str = "Desktop_Agent2") -> dict:
    grant = client.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT}).json()
    page = client.get("/oauth/device", params={"user_code": grant["user_code"]}).text
    csrf = re.search(r'name="csrf" value="([^"]*)"', page).group(1)
    approved = client.post(
        "/oauth/device",
        data={
            "user_code": grant["user_code"],
            "passphrase": "hub-pass",
            "agent": agent,
            "action": "approve",
            "csrf": csrf,
        },
    )
    assert approved.status_code == 200, approved.text
    token = client.post(
        "/token",
        data={
            "grant_type": DEVICE_GRANT,
            "device_code": grant["device_code"],
            "client_id": CLI_CLIENT,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()


def _refresh(client: TestClient, refresh_token: str, client_id: str = CLI_CLIENT, **extra):
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    data.update(extra)
    return client.post("/token", data=data)


def _rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM oauth_refresh_tokens").fetchall()]
    finally:
        conn.close()


class TestRestartSurvival:
    def test_device_refresh_survives_restart_with_device_binding(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)

        client = _app(monkeypatch)  # hub restart: every in-memory map is gone
        old_auth = {"Authorization": f"Bearer {issued['access_token']}"}
        still = client.get("/whoami", headers=old_auth)
        assert still.status_code == 200, "a live access token survives the restart"
        assert still.json()["identity"] == "Desktop_Agent2"

        refreshed = _refresh(client, issued["refresh_token"])
        assert client.get("/whoami", headers=old_auth).status_code == 401, "rotation retires it"
        assert refreshed.status_code == 200, refreshed.text
        body = refreshed.json()
        me = client.get("/whoami", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.json()["identity"] == "Desktop_Agent2"
        who = oauth.get_provider().whoami(body["access_token"])
        assert who["device_node"] == {"node": NODE["node"], "node_id": NODE["node_id"]}
        assert who["auth_method"] == "device_code"

    def test_rotated_refresh_is_single_use_across_restart(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        client = _app(monkeypatch)
        assert _refresh(client, issued["refresh_token"]).status_code == 200
        client = _app(monkeypatch)
        replay = _refresh(client, issued["refresh_token"])
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

    def test_refresh_tokens_are_hashed_at_rest(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        rows = _rows()
        assert len(rows) == 1
        digest = hashlib.sha256(issued["refresh_token"].encode()).hexdigest()
        assert rows[0]["token_sha256"] == digest
        flat = repr(rows)
        assert issued["refresh_token"] not in flat
        assert issued["access_token"] not in flat
        assert rows[0]["device_node_id"] == NODE["node_id"]
        # Operator views carry only a digest prefix, never a token.
        listed = oauth_devices.list_device_sessions()
        assert issued["refresh_token"] not in repr(listed)
        assert listed["devices"][0]["sessions"][0]["session_id"] == digest[:16]

    def test_expired_rows_pruned_on_start(self, monkeypatch):
        client = _app(monkeypatch)
        _device_sign_in(client)
        conn = storage.connect()
        with conn:
            conn.execute("UPDATE oauth_refresh_tokens SET expires_at = 1")
        conn.close()
        _app(monkeypatch)
        assert _rows() == []


class TestDynamicClientPersistence:
    def _register(self, client: TestClient) -> dict:
        resp = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1:9999/callback"],
                "client_name": "Desktop_Agent2",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_confidential_client_secret_stored_as_digest_and_verified_after_restart(
        self, monkeypatch
    ):
        client = _app(monkeypatch)
        reg = self._register(client)
        conn = storage.connect()
        row = dict(conn.execute("SELECT * FROM oauth_clients").fetchone())
        conn.close()
        assert reg["client_secret"] not in repr(row)
        assert row["secret_sha256"] == hashlib.sha256(reg["client_secret"].encode()).hexdigest()

        client = _app(monkeypatch)  # restart
        provider = oauth.get_provider()
        restored = provider.clients[reg["client_id"]]
        assert restored.client_secret is None
        issued = provider._issue(restored, ["mcp"], None, subject="Desktop_Agent2")

        wrong = _refresh(
            client, issued.refresh_token, reg["client_id"], client_secret="not-the-secret"
        )
        assert wrong.status_code == 401
        ok = _refresh(
            client, issued.refresh_token, reg["client_id"], client_secret=reg["client_secret"]
        )
        assert ok.status_code == 200, ok.text

    def test_revoke_endpoint_authenticates_restored_client(self, monkeypatch):
        client = _app(monkeypatch)
        reg = self._register(client)
        client = _app(monkeypatch)
        provider = oauth.get_provider()
        issued = provider._issue(provider.clients[reg["client_id"]], ["mcp"], None)
        resp = client.post(
            "/revoke",
            data={
                "token": issued.refresh_token,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert _rows() == []

    def test_reserved_and_seeded_ids_are_not_overridden_by_store(self, monkeypatch):
        oauth_store.save_client(
            CLI_CLIENT,
            {"client_id": CLI_CLIENT, "redirect_uris": [], "token_endpoint_auth_method": "x"},
            "evil",
        )
        _app(monkeypatch)
        provider = oauth.get_provider()
        assert provider.clients[CLI_CLIENT].token_endpoint_auth_method == "none"
        assert CLI_CLIENT not in provider.client_secret_digests


class TestDeviceRevoke:
    def test_revoke_by_device_kills_refresh_and_live_access(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        auth = {"Authorization": f"Bearer {issued['access_token']}"}
        assert client.get("/whoami", headers=auth).status_code == 200

        revoked = oauth.get_provider().revoke_sessions(
            device=NODE["node_id"], actor="Desktop_Person"
        )
        assert len(revoked) == 1 and revoked[0]["agent"] == "Desktop_Agent2"
        assert "token_sha256" not in revoked[0]
        assert client.get("/whoami", headers=auth).status_code == 401
        assert _refresh(client, issued["refresh_token"]).status_code == 400

        client = _app(monkeypatch)  # stays revoked across restart
        assert _refresh(client, issued["refresh_token"]).status_code == 400

    def test_retire_revokes_a_live_device_sign_in_and_unretire_does_not_restore_it(
        self, monkeypatch
    ):
        """Task #2315: the retire transaction deletes the sign-in and the
        provider forgets its in-memory tokens; unretire re-issues nothing."""
        from unified_agent import agent_lifecycle

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        auth = {"Authorization": f"Bearer {issued['access_token']}"}
        assert client.get("/whoami", headers=auth).json()["identity"] == "Desktop_Agent2"
        operator = config.agent_identity_var.set(config.operator_label())
        try:
            res = agent_lifecycle.agent_retire("Desktop_Agent2", "decommissioned")
            assert len(res["revoked"]["device_signins"]) == 1
            assert _rows() == []
            agent_lifecycle.agent_unretire("Desktop_Agent2", "rehired")
        finally:
            config.agent_identity_var.reset(operator)
        # Revoked, not merely refused: a bad token (401), not a retired one (403).
        assert client.get("/whoami", headers=auth).status_code == 401
        assert _refresh(client, issued["refresh_token"]).status_code == 400
        client = _app(monkeypatch)  # stays revoked across a restart
        assert client.get("/whoami", headers=auth).status_code == 401

    def test_an_approved_device_code_yields_nothing_after_a_retire(self, monkeypatch):
        """Review of #2315: approve, retire before the poll: no token now, none after
        an unretire; and a retired label cannot be approved at all."""
        from unified_agent import agent_lifecycle

        client = _app(monkeypatch)

        def approve(agent: str):
            grant = client.post(
                "/oauth/device_authorization", data={"client_id": CLI_CLIENT}
            ).json()
            page = client.get("/oauth/device", params={"user_code": grant["user_code"]}).text
            csrf = re.search(r'name="csrf" value="([^"]*)"', page).group(1)
            res = client.post(
                "/oauth/device",
                data={
                    "user_code": grant["user_code"],
                    "passphrase": "hub-pass",
                    "agent": agent,
                    "action": "approve",
                    "csrf": csrf,
                },
            )
            return grant, res

        def poll(grant):
            return client.post(
                "/token",
                data={
                    "grant_type": DEVICE_GRANT,
                    "device_code": grant["device_code"],
                    "client_id": CLI_CLIENT,
                },
            )

        grant, approved = approve("Desktop_Agent2")
        assert approved.status_code == 200, approved.text
        operator = config.agent_identity_var.set(config.operator_label())
        try:
            agent_lifecycle.agent_retire("Desktop_Agent2", "decommissioned")
            assert poll(grant).json()["error"] in {"access_denied", "invalid_grant"}
            _, refused = approve("Desktop_Agent2")  # a retired label is not approvable
            assert refused.status_code == 400 and "is retired" in refused.text
            provider = oauth.get_provider()
            # The retire dropped the approved grant. A grant approved in a race
            # (status set after the drop) is refused at the poll itself.
            assert all(g.subject != "Desktop_Agent2" for g in provider.device_grants.values())
            pending = client.post(
                "/oauth/device_authorization", data={"client_id": CLI_CLIENT}
            ).json()
            held = provider.device_grants[pending["device_code"]]
            held.status, held.subject = "approved", "Desktop_Agent2"
            raced_poll = poll(pending)
            assert raced_poll.status_code == 400
            assert raced_poll.json()["error"] == "access_denied"
            assert "is retired" in raced_poll.json()["error_description"]
            agent_lifecycle.agent_unretire("Desktop_Agent2", "rehired")
        finally:
            config.agent_identity_var.reset(operator)
        late = poll(grant)
        assert late.status_code == 400 and "access_token" not in late.json()
        assert _rows() == []

    def test_session_id_rejects_like_wildcards(self, monkeypatch):
        _app(monkeypatch)
        with pytest.raises(ValueError):
            oauth_store.revoke_sessions(session_id="%" * 16)
        with pytest.raises(ValueError):
            oauth_store.revoke_sessions(device="a", session_id="b" * 16)

    def test_tool_revoke_is_approval_gated_then_runs(self, monkeypatch):
        assert safety.PROD_ACTIONS["oauth_device_manage"] == "revoke"
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        # Desktop_Person and Operator are one person, so an agent requests the revoke.
        identity = config.agent_identity_var.set("Desktop_Agent5")
        try:
            with pytest.raises(ApprovalPendingError) as pending:
                oauth_devices.oauth_device_manage(action="revoke", device=NODE["node"])
            assert len(_rows()) == 1, "nothing is revoked before approval"
            human = config.agent_identity_var.set("Operator")
            try:
                approval_resolve(pending.value.approval_id, "approve")
            finally:
                config.agent_identity_var.reset(human)
            result = oauth_devices.oauth_device_manage(action="revoke", device=NODE["node"])
        finally:
            config.agent_identity_var.reset(identity)
        assert result["revoked"] == 1
        assert result["actor"] == "Desktop_Agent5"
        assert _rows() == []
        assert _refresh(client, issued["refresh_token"]).status_code == 400

        conn = storage.connect()
        row = conn.execute(
            "SELECT payload, originator FROM event_log WHERE channel='hub.oauth.sessions_revoked'"
        ).fetchone()
        conn.close()
        assert row is not None and row["originator"] == "Desktop_Agent5"
        assert issued["refresh_token"] not in row["payload"]

    def test_tool_revoke_requires_identity_and_one_target(self, monkeypatch):
        from mcp.server.mcpserver.exceptions import ToolError

        _app(monkeypatch)
        with pytest.raises(ToolError, match="identity"):
            oauth_devices.oauth_device_manage(action="revoke", device="x")
        with pytest.raises(ToolError, match="exactly one"):
            oauth_devices.oauth_device_manage(action="revoke")
        with pytest.raises(ToolError, match="action"):
            oauth_devices.oauth_device_manage(action="delete")

    def test_tool_list_groups_by_device(self, monkeypatch):
        client = _app(monkeypatch)
        _device_sign_in(client, agent="Desktop_Agent2")
        provider = oauth.get_provider()
        provider._issue(provider.clients[CLI_CLIENT], ["mcp"], None, subject="Desktop_Agent8")
        listed = oauth_devices.oauth_device_manage()
        assert listed["session_count"] == 2
        bound, unbound = listed["devices"]
        assert bound["bound"] and bound["device_node_id"] == NODE["node_id"]
        assert not unbound["bound"]


class TestDoctorRow:
    def test_dormant_when_oauth_off(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "0")
        assert _oauth_store_check()["status"] == "DORMANT"

    def test_live_with_counts(self, monkeypatch):
        client = _app(monkeypatch)
        _device_sign_in(client)
        check = _oauth_store_check()
        assert check["status"] == "LIVE"
        assert "1 live session(s) across 1 bound device(s)" in check["detail"]


class TestReviewHardening:
    """PR #761 review: rotation/revoke race, name-only sessions, fail-closed writes."""

    def test_rotation_refuses_token_whose_durable_row_is_gone(self, monkeypatch):
        """A revoke that deletes the row while this process still holds the
        token in memory must win: no replacement token is issued."""
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        assert issued["refresh_token"] in oauth.get_provider().refresh
        oauth_store.revoke_sessions(device=NODE["node_id"])  # another path deletes the row
        resp = _refresh(client, issued["refresh_token"])
        assert resp.status_code == 400
        assert _rows() == []

    def test_memory_only_token_still_rotates_when_its_write_failed(self, monkeypatch):
        client = _app(monkeypatch)
        real_save = oauth_store.save_refresh

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(oauth_store, "save_refresh", boom)
        issued = _device_sign_in(client)
        monkeypatch.setattr(oauth_store, "save_refresh", real_save)
        provider = oauth.get_provider()
        assert issued["refresh_token"] in provider.memory_only_refresh
        assert _rows() == []
        assert oauth_store.write_failure_status()["count"] >= 1
        assert _oauth_store_check()["status"] == "DEGRADED"
        # No durable row, but its write failed: rotation honours memory once.
        rotated = _refresh(client, issued["refresh_token"])
        assert rotated.status_code == 200, rotated.text
        assert issued["refresh_token"] not in provider.memory_only_refresh
        assert len(_rows()) == 1, "the replacement token is durable again"
        oauth_store.reset_write_failures()

    def test_revoke_rotation_race_is_serialized(self, monkeypatch):
        """Revoke runs while a rotation holds the lock; it must see and delete
        the replacement token the rotation issues."""
        import threading

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        provider = oauth.get_provider()
        real_issue = provider._issue
        entered, release = threading.Event(), threading.Event()

        def slow_issue(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real_issue(*args, **kwargs)

        monkeypatch.setattr(provider, "_issue", slow_issue)
        result: dict = {}
        rotate = threading.Thread(
            target=lambda: result.setdefault("resp", _refresh(client, issued["refresh_token"]))
        )
        rotate.start()
        assert entered.wait(5)
        revoker = threading.Thread(
            target=lambda: result.setdefault(
                "revoked", provider.revoke_sessions(device=NODE["node"], actor="Desktop_Person")
            )
        )
        revoker.start()
        release.set()
        rotate.join(10)
        revoker.join(10)
        assert result["resp"].status_code == 200
        new_refresh = result["resp"].json()["refresh_token"]
        assert _rows() == [], "the replacement token must not survive the revoke"
        assert new_refresh not in provider.refresh

    def test_revoke_by_id_reaches_name_only_sessions(self, monkeypatch):
        client = _app(monkeypatch)
        _device_sign_in(client)  # bound by name + id
        provider = oauth.get_provider()
        provider._issue(
            provider.clients[CLI_CLIENT],
            ["mcp"],
            None,
            subject="Desktop_Agent8",
            extra_claims={"device_node": {"node": NODE["node"], "node_id": ""}},
        )
        listed = oauth_devices.list_device_sessions()
        bound = [g for g in listed["devices"] if g["bound"]]
        assert len(bound) == 1 and len(bound[0]["sessions"]) == 2, "one row per device"
        revoked = provider.revoke_sessions(device=NODE["node_id"], actor="Desktop_Person")
        assert len(revoked) == 2
        assert _rows() == []

    def test_restored_client_with_wrong_secret_never_looks_public(self, monkeypatch):
        import asyncio

        client = _app(monkeypatch)
        reg = TestDynamicClientPersistence()._register(client)
        _app(monkeypatch)
        provider = oauth.get_provider()
        token = oauth._presented_client_secret.set("wrong")
        try:
            got = asyncio.run(provider.get_client(reg["client_id"]))
        finally:
            oauth._presented_client_secret.reset(token)
        assert got.client_secret and got.client_secret != reg["client_secret"]

    def test_registration_refused_when_it_cannot_persist(self, monkeypatch):
        client = _app(monkeypatch)
        monkeypatch.setattr(oauth_store, "save_client", lambda *a, **k: False)
        resp = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1:9999/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"

    def test_active_client_is_not_pruned_by_registration_age(self, monkeypatch):
        client = _app(monkeypatch)
        reg = TestDynamicClientPersistence()._register(client)
        provider = oauth.get_provider()
        issued = provider._issue(provider.clients[reg["client_id"]], ["mcp"], None)
        conn = storage.connect()
        with conn:
            conn.execute("UPDATE oauth_clients SET registered_at = 1")
        conn.close()
        oauth_store.delete_refresh(issued.refresh_token)
        pruned = oauth_store.prune_expired()
        assert pruned["clients"] == 0, "last use, not registration age, drives pruning"


class TestReviewRound2:
    """PR #761 review round 2: access survival, lazy client reload, ownership, body bound."""

    def test_access_tokens_hashed_at_rest_and_revoke_cascades_after_restart(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        conn = storage.connect()
        rows = [dict(r) for r in conn.execute("SELECT * FROM oauth_access_tokens").fetchall()]
        conn.close()
        assert len(rows) == 1
        assert issued["access_token"] not in repr(rows)
        assert (
            rows[0]["refresh_sha256"]
            == hashlib.sha256(issued["refresh_token"].encode()).hexdigest()
        )

        client = _app(monkeypatch)  # restart, then revoke the device
        auth = {"Authorization": f"Bearer {issued['access_token']}"}
        assert client.get("/whoami", headers=auth).status_code == 200
        oauth.get_provider().revoke_sessions(device=NODE["node"], actor="Desktop_Person")
        assert client.get("/whoami", headers=auth).status_code == 401
        client = _app(monkeypatch)
        assert client.get("/whoami", headers=auth).status_code == 401, "stays revoked durably"

    def test_whoami_works_for_restored_access_token(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        _app(monkeypatch)
        who = oauth.get_provider().whoami(issued["access_token"])
        assert who is not None and who["agent"] == "Desktop_Agent2"
        assert who["device_node"]["node_id"] == NODE["node_id"]

    def test_cross_client_refresh_does_not_destroy_owner_session(self, monkeypatch):
        import asyncio

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        reg = TestDynamicClientPersistence()._register(client)
        provider = oauth.get_provider()
        other = provider.clients[reg["client_id"]]
        stolen = oauth.RefreshToken(
            token=issued["refresh_token"], client_id=CLI_CLIENT, scopes=["mcp"]
        )
        with pytest.raises(oauth.TokenError):
            asyncio.run(provider.exchange_refresh_token(other, stolen, []))
        assert len(_rows()) == 1, "the owner's durable session is untouched"
        # The same after a restart, when only the durable row exists.
        _app(monkeypatch)
        provider = oauth.get_provider()
        other = asyncio.run(provider.get_client(reg["client_id"]))
        with pytest.raises(oauth.TokenError):
            asyncio.run(provider.exchange_refresh_token(other, stolen, []))
        assert len(_rows()) == 1
        assert _refresh(client, issued["refresh_token"]).status_code in (200,)

    def test_client_missed_at_startup_is_reloaded_lazily(self, monkeypatch):
        client = _app(monkeypatch)
        reg = TestDynamicClientPersistence()._register(client)
        real_load = oauth_store.load_clients

        def locked():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(oauth_store, "load_clients", locked)
        client = _app(monkeypatch)  # restart while the store is briefly unavailable
        monkeypatch.setattr(oauth_store, "load_clients", real_load)
        provider = oauth.get_provider()
        assert reg["client_id"] not in provider.clients
        issued = provider._issue(
            asyncio_get(provider, reg["client_id"]), ["mcp"], None, subject="Desktop_Agent2"
        )
        ok = _refresh(
            client, issued.refresh_token, reg["client_id"], client_secret=reg["client_secret"]
        )
        assert ok.status_code == 200, ok.text

    def test_declared_oversized_body_is_not_buffered(self):
        import asyncio

        calls = {"wrapper_reads": 0}
        big = oauth._SECRET_BIND_MAX_BODY + 1

        async def receive():
            calls["wrapper_reads"] += 1
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def inner(scope, recv, send):
            calls["reads_before_inner"] = calls["wrapper_reads"]
            calls["same_receive"] = recv is receive
            calls["secret"] = oauth._presented_client_secret.get()

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-length", str(big).encode())],
        }
        asyncio.run(oauth._SecretBinding(inner)(scope, receive, None))
        assert calls == {
            "wrapper_reads": 0,
            "reads_before_inner": 0,
            "same_receive": True,
            "secret": None,
        }


def asyncio_get(provider, client_id):
    import asyncio

    return asyncio.run(provider.get_client(client_id))


class TestReviewRound3:
    """PR #761 review round 3: orphan access, pruned clients, shared names, cache race."""

    def test_access_not_persisted_when_its_refresh_write_failed(self, monkeypatch):
        client = _app(monkeypatch)
        real_save = oauth_store.save_refresh
        monkeypatch.setattr(
            oauth_store, "save_refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )
        issued = _device_sign_in(client)
        monkeypatch.setattr(oauth_store, "save_refresh", real_save)
        conn = storage.connect()
        n = conn.execute("SELECT COUNT(*) FROM oauth_access_tokens").fetchone()[0]
        conn.close()
        assert n == 0, "no durable access row without a durable session"
        client = _app(monkeypatch)
        auth = {"Authorization": f"Bearer {issued['access_token']}"}
        assert client.get("/whoami", headers=auth).status_code == 401

    def test_client_pruned_while_loaded_is_resaved_on_issue(self, monkeypatch):
        client = _app(monkeypatch)
        reg = TestDynamicClientPersistence()._register(client)
        conn = storage.connect()
        with conn:
            conn.execute("DELETE FROM oauth_clients")  # pruned behind the live process
        conn.close()
        provider = oauth.get_provider()
        issued = provider._issue(provider.clients[reg["client_id"]], ["mcp"], None)
        client = _app(monkeypatch)  # restart
        ok = _refresh(
            client, issued.refresh_token, reg["client_id"], client_secret=reg["client_secret"]
        )
        assert ok.status_code == 200, ok.text

    def _issue_on(self, provider, node, subject):
        return provider._issue(
            provider.clients[CLI_CLIENT],
            ["mcp"],
            None,
            subject=subject,
            extra_claims={"device_node": node},
        )

    def test_shared_name_does_not_reach_another_node_id(self, monkeypatch):
        _app(monkeypatch)
        provider = oauth.get_provider()
        a = self._issue_on(provider, {"node": "mac", "node_id": "n1"}, "Desktop_Agent2")
        b = self._issue_on(provider, {"node": "mac", "node_id": "n2"}, "Desktop_Agent8")
        revoked = provider.revoke_sessions(device="n1", actor="Desktop_Person")
        assert [r["device_node_id"] for r in revoked] == ["n1"]
        assert provider._lookup_access(a.access_token) is None
        assert provider._lookup_access(b.access_token) is not None, "n2 stays signed in"
        with pytest.raises(ValueError, match="node ids"):
            self._issue_on(provider, {"node": "mac", "node_id": "n3"}, "Desktop_Agent8")
            provider.revoke_sessions(device="mac", actor="Desktop_Person")
        listed = oauth_devices.list_device_sessions()["devices"]
        assert sorted(g["device_node_id"] for g in listed) == ["n2", "n3"]

    def test_persisted_access_load_waits_for_revoke(self, monkeypatch):
        import threading

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        _app(monkeypatch)  # restart: the access token is only in the store
        provider = oauth.get_provider()
        done = threading.Event()
        result = {}

        def lookup():
            result["access"] = provider._lookup_access(issued["access_token"])
            done.set()

        with provider._rotation_lock:
            t = threading.Thread(target=lookup)
            t.start()
            assert not done.wait(0.3), "lookup must block while revoke holds the lock"
            provider._revoke_sessions_locked(NODE["node_id"], None, "Desktop_Person")
        t.join(5)
        assert result["access"] is None, "a revoked token is never cached"


class TestSigninFamily:
    """A device sign-in keeps one ``signin_id`` across refresh rotations (task #1744).

    Session-scoped MCP credentials are bound to that id, so rotation must keep
    it and must never leave a moment with no durable row for it.
    """

    def _family(self, signin_id: str) -> list[dict]:
        return [r for r in _rows() if '"signin_id": "' + signin_id + '"' in r["claims"]]

    def test_device_sign_in_carries_a_signin_id_across_rotation(self, monkeypatch):
        import json

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        (row,) = _rows()
        signin_id = json.loads(row["claims"]).get("signin_id")
        assert isinstance(signin_id, str) and re.fullmatch(r"[0-9a-f]{32}", signin_id)
        access = oauth.get_provider()._lookup_access(issued["access_token"])
        assert access.claims["signin_id"] == signin_id
        assert oauth_store.live_signin_exists("Desktop_Agent2", signin_id)
        client = _app(monkeypatch)  # restart: rotate from the durable row only
        refreshed = _refresh(client, issued["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        (new_row,) = _rows()
        assert new_row["token_sha256"] != row["token_sha256"]
        assert json.loads(new_row["claims"])["signin_id"] == signin_id
        assert oauth_store.live_signin_exists("Desktop_Agent2", signin_id)
        assert not oauth_store.live_signin_exists("Desktop_Agent8", signin_id)

    def test_rotation_is_one_transaction(self, monkeypatch):
        """The delete of the old row and the insert of the new one are one write:
        the two-step consume-then-save path is never taken for a durable row."""
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        client = _app(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("rotation must not split delete and insert")

        monkeypatch.setattr(oauth_store, "consume_refresh", forbidden)
        monkeypatch.setattr(oauth_store, "save_refresh", forbidden)
        refreshed = _refresh(client, issued["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        assert len(_rows()) == 1
        replay = _refresh(client, issued["refresh_token"])
        assert replay.status_code == 400

    def test_a_revoked_sign_in_is_not_live(self, monkeypatch):
        import json

        client = _app(monkeypatch)
        _device_sign_in(client)
        (row,) = _rows()
        signin_id = json.loads(row["claims"])["signin_id"]
        oauth.get_provider().revoke_sessions(session_id=row["token_sha256"][:16], actor="op")
        assert not oauth_store.live_signin_exists("Desktop_Agent2", signin_id)

    def test_an_expired_row_rotates_into_nothing(self, monkeypatch):
        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        conn = storage.connect()
        try:
            conn.execute("UPDATE oauth_refresh_tokens SET expires_at=1")
            conn.commit()
        finally:
            conn.close()
        client = _app(monkeypatch)
        resp = _refresh(client, issued["refresh_token"])
        assert resp.status_code == 400
        assert _rows() == []

    # --- hardening (task #1893) ------------------------------------------------------

    def test_a_refreshed_access_token_carries_the_signin_id(self, monkeypatch):
        """Task #1893 item 9: the access token a refresh issues keeps the family id."""
        import json

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        (row,) = _rows()
        signin_id = json.loads(row["claims"])["signin_id"]
        client = _app(monkeypatch)  # restart: rotate from the durable row only
        refreshed = _refresh(client, issued["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        access = oauth.get_provider()._lookup_access(refreshed.json()["access_token"])
        assert access is not None and access.claims["signin_id"] == signin_id
        _app(monkeypatch)  # and from the durable access row after another restart
        reloaded = oauth.get_provider()._lookup_access(refreshed.json()["access_token"])
        assert reloaded is not None and reloaded.claims["signin_id"] == signin_id

    def test_rotation_at_the_expiry_second_leaves_no_orphan(self, monkeypatch):
        """Task #1893 item 6: the store rotated a row live at its last second; the
        provider's own clock has moved on. The rotation stands, no orphan row."""
        import asyncio
        import time as time_mod

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        _app(monkeypatch)  # restart: rotate from the durable row only
        provider = oauth.get_provider()
        edge = int(time_mod.time()) - 5
        conn = storage.connect()
        try:
            conn.execute("UPDATE oauth_refresh_tokens SET expires_at=?", (edge,))
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(oauth_store, "_now", lambda: edge)
        token = oauth.RefreshToken(
            token=issued["refresh_token"], client_id=CLI_CLIENT, scopes=["mcp"], expires_at=edge
        )
        cli = provider.clients[CLI_CLIENT]
        issued_again = asyncio.run(provider.exchange_refresh_token(cli, token, []))
        (row,) = _rows()
        assert row["token_sha256"] == oauth_store.digest(issued_again.refresh_token)

    def test_a_raise_after_rotation_deletes_the_successor(self, monkeypatch):
        """Task #1893 item 6: any raise after a committed rotation drops its successor."""
        import asyncio

        client = _app(monkeypatch)
        issued = _device_sign_in(client)
        _app(monkeypatch)
        provider = oauth.get_provider()

        def boom(*args, **kwargs):
            raise RuntimeError("issue failed")

        monkeypatch.setattr(provider, "_issue", boom)
        token = oauth.RefreshToken(
            token=issued["refresh_token"], client_id=CLI_CLIENT, scopes=["mcp"]
        )
        with pytest.raises(RuntimeError):
            asyncio.run(provider.exchange_refresh_token(provider.clients[CLI_CLIENT], token, []))
        assert _rows() == [], "no orphan successor the client never received"

    def test_a_store_down_duplicate_goes_with_a_single_session_revoke(self, monkeypatch):
        """Task #1893 item 7: a failed atomic rotation followed by a successful
        fallback save leaves two rows of one family; revoking either one by
        session id removes the whole family."""
        import json
        import sqlite3

        client = _app(monkeypatch)
        issued = _device_sign_in(client)  # held in this process's memory
        (old,) = _rows()
        signin_id = json.loads(old["claims"])["signin_id"]

        real_rotate = oauth_store.rotate_refresh
        store = {"down": True}

        def flaky(*args, **kwargs):
            if store["down"]:
                raise sqlite3.OperationalError("database is locked")
            return real_rotate(*args, **kwargs)

        monkeypatch.setattr(oauth_store, "rotate_refresh", flaky)
        refreshed = _refresh(client, issued["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        store["down"] = False
        client = _app(monkeypatch)  # restart: only the durable rows remain
        rows = _rows()
        assert len(rows) == 2, "the old row survived and the fallback saved a successor"
        assert {json.loads(r["claims"])["signin_id"] for r in rows} == {signin_id}
        provider = oauth.get_provider()
        new_digest = oauth_store.digest(refreshed.json()["refresh_token"])
        revoked = provider.revoke_sessions(session_id=new_digest[:16], actor="op")
        assert len(revoked) == 2
        assert all("signin_id" not in r and "token_sha256" not in r for r in revoked)
        assert not oauth_store.live_signin_exists("Desktop_Agent2", signin_id)
        assert _rows() == []
        assert _refresh(client, issued["refresh_token"]).status_code == 400

    def test_a_malformed_claims_row_fails_closed_for_that_row_only(self, monkeypatch):
        """Task #1893 item 8: one bad row must not refuse every sign-in of its label."""
        import json
        import secrets

        client = _app(monkeypatch)
        _device_sign_in(client)
        (row,) = _rows()
        signin_id = json.loads(row["claims"])["signin_id"]
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, subject, claims,"
                " issued_at) VALUES (?, ?, ?, ?, 0)",
                (
                    oauth_store.digest(secrets.token_urlsafe(16)),
                    CLI_CLIENT,
                    "Desktop_Agent2",
                    '{"signin_id": ',
                ),
            )
            conn.commit()
        finally:
            conn.close()
        assert oauth_store.live_signin_exists("Desktop_Agent2", signin_id)
        assert not oauth_store.live_signin_exists("Desktop_Agent2", secrets.token_hex(16))
        # A single-session revoke that reaches the bad row still works.
        bad = [r for r in _rows() if r["token_sha256"] != row["token_sha256"]][0]
        revoked = oauth_store.revoke_sessions(session_id=bad["token_sha256"][:16])
        assert [r["token_sha256"] for r in revoked] == [bad["token_sha256"]]
        assert oauth_store.live_signin_exists("Desktop_Agent2", signin_id)
