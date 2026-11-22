"""OAuth 2.1 MCP authorization server tests across agents (task #810, #817)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, oauth
from unified_agent.server import HubAuthASGIMiddleware


@pytest.fixture(autouse=True)
def _reset_oauth():
    oauth.reset_provider()
    yield
    oauth.reset_provider()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _enable(
    monkeypatch, *, passphrase: str = "hub-pass", default_agent: str = "Desktop_Agent8"
) -> None:
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "http://127.0.0.1:8765")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", passphrase)
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", default_agent)
    oauth.reset_provider()


def _app(monkeypatch, *, enforce: bool = True, default_agent: str = "Desktop_Agent8") -> TestClient:
    _enable(monkeypatch, default_agent=default_agent)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1" if enforce else "0")

    async def homepage(request):
        return JSONResponse(
            {
                "identity": config.verified_agent_identity(),
                "via": request.scope.get("ua_identity_via"),
            }
        )

    # Not "/": exact "/" is public and redirects in the real hub (task
    # #1400), which would defeat the 401/identity assertions below.
    app = Starlette(routes=[Route("/whoami", homepage, methods=["GET"]), *oauth.http_routes()])
    return TestClient(HubAuthASGIMiddleware(app), follow_redirects=False)


class TestMcpOauthConfig:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_ISSUER", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        assert config.mcp_oauth_enabled() is False
        assert oauth.get_provider() is None
        assert oauth.http_routes() == []

    def test_partial_config_stays_off(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        assert config.mcp_oauth_enabled() is False

    def test_http_non_loopback_issuer_refused(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "http://example.test")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "x")
        assert config.mcp_oauth_enabled() is False


class TestMcpOauthFlow:
    def test_metadata_and_protected_resource(self, monkeypatch):
        client = _app(monkeypatch)
        meta = client.get("/.well-known/oauth-authorization-server")
        assert meta.status_code == 200
        body = meta.json()
        assert str(body["issuer"]).rstrip("/") == "http://127.0.0.1:8765"
        assert str(body["registration_endpoint"]).endswith("/register")
        assert "S256" in body["code_challenge_methods_supported"]
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert str(prm.json()["resource"]).rstrip("/") == "http://127.0.0.1:8765/mcp"

    def test_dcr_consent_token_authenticates_as_desktop_Agent8(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "grok-bot",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]

        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": "http://127.0.0.1/cb",
                "state": "st",
                "scope": "mcp",
            },
        )
        assert authorize.status_code == 302
        consent_url = authorize.headers["location"]
        assert "/oauth/consent?rid=" in consent_url
        rid = parse_qs(urlparse(consent_url).query)["rid"][0]

        denied = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "wrong"},
        )
        assert denied.status_code == 403

        approved = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "hub-pass"},
        )
        assert approved.status_code == 302
        redirected = urlparse(approved.headers["location"])
        assert redirected.scheme == "http"
        code = parse_qs(redirected.query)["code"][0]
        assert parse_qs(redirected.query)["state"] == ["st"]

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200
        access = token.json()["access_token"]
        assert token.json()["refresh_token"]

        who = client.get("/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.status_code == 200
        assert who.json() == {"identity": "Desktop_Agent8", "via": "oauth"}

    def test_consent_page_renders_client_name_and_agent_selector(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "Claude Desktop Client",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        consent_page = client.get(f"/oauth/consent?rid={rid}")
        assert consent_page.status_code == 200
        assert "Authorize Claude Desktop Client" in consent_page.text
        assert 'name="agent"' in consent_page.text
        assert "Desktop_Agent1" in consent_page.text
        assert "Desktop_agent3" in consent_page.text

    def test_custom_agent_identity_at_consent(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "Generic IDE Tool",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        approved = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_agent3",
            },
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200
        access = token.json()["access_token"]
        who = client.get("/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.status_code == 200
        assert who.json() == {"identity": "Desktop_agent3", "via": "oauth"}

    def test_client_name_matching_fleet_identity_is_auto_suggested(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "Desktop_Agent5",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        consent_page = client.get(f"/oauth/consent?rid={rid}")
        assert 'value="Desktop_Agent5"' in consent_page.text

        approved = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "hub-pass"},
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200
        access = token.json()["access_token"]
        who = client.get("/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.status_code == 200
        assert who.json() == {"identity": "Desktop_Agent5", "via": "oauth"}

    def test_invalid_agent_label_at_consent_refused(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        bad_agent = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_NonexistentFleetAgent",
            },
        )
        assert bad_agent.status_code == 400
        assert "Invalid agent label" in bad_agent.text

    def test_refresh_token_preserves_agent_identity(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        approved = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Laptop_Agent1",
            },
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token_resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token_resp.status_code == 200
        access1 = token_resp.json()["access_token"]
        refresh1 = token_resp.json()["refresh_token"]

        who1 = client.get("/whoami", headers={"Authorization": f"Bearer {access1}"})
        assert who1.status_code == 200
        assert who1.json() == {"identity": "Laptop_Agent1", "via": "oauth"}

        # Refresh token exchange
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh1,
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 200
        access2 = refreshed.json()["access_token"]
        who2 = client.get("/whoami", headers={"Authorization": f"Bearer {access2}"})
        assert who2.status_code == 200
        assert who2.json() == {"identity": "Laptop_Agent1", "via": "oauth"}

    def test_client_credentials_is_pinned_noninteractive_and_nonrefreshable(self, monkeypatch):
        client = _app(monkeypatch, default_agent="server_agent5")
        monkeypatch.setenv("UA_MCP_OAUTH_CLIENTS", "server_agent5:bot-client:bot-secret")
        token = client.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "bot-client",
                "client_secret": "bot-secret",
                "scope": "mcp",
            },
        )
        assert token.status_code == 200
        body = token.json()
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "mcp"
        assert "refresh_token" not in body

        who = client.get("/whoami", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert who.status_code == 200
        assert who.json() == {"identity": "server_agent5", "via": "oauth"}

        wrong = client.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "bot-client",
                "client_secret": "wrong",
            },
        )
        assert wrong.status_code == 401

        refresh = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "bot-client",
                "refresh_token": "missing",
            },
        )
        assert refresh.status_code == 401

    def test_public_host_consent_cannot_change_pinned_identity(self, monkeypatch):
        client = _app(monkeypatch, default_agent="server_agent5")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "hub-bot",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            headers={"Host": "public.example.test"},
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": "http://127.0.0.1/cb",
            },
            headers={"Host": "public.example.test"},
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        changed = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_agent3",
            },
            headers={"Host": "public.example.test"},
        )
        assert changed.status_code == 403

    async def test_revocation_is_audited_and_removes_rotated_credentials(self, monkeypatch):
        _enable(monkeypatch)
        provider = oauth.get_provider()
        client = oauth.OAuthClientInformationFull(client_id="audit-client")
        issued = provider._issue(
            client, ["mcp"], "http://127.0.0.1:8765/mcp", subject="Desktop_Agent1"
        )
        events_seen = []
        monkeypatch.setattr(
            oauth.events, "record_event", lambda *args, **kwargs: events_seen.append((args, kwargs))
        )
        access = await provider.load_access_token(issued.access_token)
        assert access is not None
        await provider.revoke_token(access)
        assert await provider.load_access_token(issued.access_token) is None
        assert issued.refresh_token not in provider.refresh
        assert events_seen[0][0] == ("oauth.token_revoked",)

        issued_again = provider._issue(
            client, ["mcp"], "http://127.0.0.1:8765/mcp", subject="Desktop_Agent1"
        )
        refresh = provider.refresh[issued_again.refresh_token]
        await provider.revoke_token(refresh)
        assert await provider.load_access_token(issued_again.access_token) is None

    def test_401_advertises_resource_metadata(self, monkeypatch):
        client = _app(monkeypatch)
        resp = client.get("/whoami")
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert "resource_metadata=" in challenge
        assert "oauth-protected-resource/mcp" in challenge

    def test_static_bearer_still_works(self, monkeypatch):
        client = _app(monkeypatch)
        resp = client.get("/whoami", headers={"Authorization": "Bearer static-token"})
        assert resp.status_code == 200
        assert resp.json()["identity"] == "Desktop_Agent1"
        assert resp.json()["via"] == "bearer"

    def test_reuse_of_authorization_code_fails(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        approved = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "hub-pass"},
        )
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        }
        assert client.post("/token", data=payload).status_code == 200
        retry = client.post("/token", data=payload)
        assert retry.status_code == 400


class TestMcpOauthStrengthen870:
    """Task #870: public-Host pin, client_credentials, refresh rotation, whoami, revoke."""

    def test_public_host_consent_pins_identity_and_refuses_other_label(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "grok.example.ts.net")
        client = _app(monkeypatch, default_agent="server_agent8")
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "client_name": "grok-bot",
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        headers = {"Host": "grok.example.ts.net"}
        page = client.get(f"/oauth/consent?rid={rid}", headers=headers)
        assert page.status_code == 200
        assert "pinned" in page.text.lower()
        assert 'name="agent"' in page.text
        assert "Desktop_Agent1" not in page.text or "datalist" not in page.text

        refused = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_Agent1",
            },
            headers=headers,
        )
        assert refused.status_code == 403
        assert "pinned" in refused.text.lower()

        # Refusal must not consume the pending consent (asserted refusal, not silent fallback).
        approved = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "server_agent8",
            },
            headers=headers,
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200
        access = token.json()["access_token"]
        who = client.get("/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.json() == {"identity": "server_agent8", "via": "oauth"}

    def test_client_credentials_maps_to_pinned_identity_and_profile_allow_list(self, monkeypatch):
        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
            "bot-cc|bot-secret|server_agent8",
        )
        client = _app(monkeypatch, default_agent="server_agent8")
        denied = client.post(
            "/oauth/client-credentials",
            data={
                "grant_type": "client_credentials",
                "client_id": "bot-cc",
                "client_secret": "wrong",
            },
        )
        assert denied.status_code == 401

        ok = client.post(
            "/oauth/client-credentials",
            data={
                "grant_type": "client_credentials",
                "client_id": "bot-cc",
                "client_secret": "bot-secret",
            },
        )
        assert ok.status_code == 200
        body = ok.json()
        access = body["access_token"]
        assert body["refresh_token"]
        who = client.get("/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.json() == {"identity": "server_agent8", "via": "oauth"}

        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent import profiles
        from unified_agent.server import _guarded_and_instrumented

        identity = config.agent_identity_var.set("server_agent8")
        session = config.mcp_session_id_var.set("mcp-session-cc-bot")
        try:
            assert profiles.current() == "public"
            assert profiles.tool_is_allowed("task_list")
            assert not profiles.tool_is_allowed("memory_search")

            @_guarded_and_instrumented
            def memory_search():
                return "should-not-run"

            with pytest.raises(ToolError, match="not exposed by the active public profile"):
                memory_search()
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)

    def test_refresh_rotation_preserves_identity_and_invalidates_old(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        approved = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_Agent4",
            },
        )
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token_resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        access1 = token_resp.json()["access_token"]
        refresh1 = token_resp.json()["refresh_token"]

        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh1,
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 200
        access2 = refreshed.json()["access_token"]
        refresh2 = refreshed.json()["refresh_token"]
        assert refresh2 != refresh1
        assert access2 != access1

        who2 = client.get("/whoami", headers={"Authorization": f"Bearer {access2}"})
        assert who2.json() == {"identity": "Desktop_Agent4", "via": "oauth"}

        # Old refresh is invalid after rotation.
        replay = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh1,
                "client_id": client_id,
            },
        )
        assert replay.status_code == 400

        # Old access invalidated on refresh rotation.
        who_old = client.get("/whoami", headers={"Authorization": f"Bearer {access1}"})
        assert who_old.status_code == 401

    def test_revoke_refresh_invalidates_linked_access(self, monkeypatch):
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "code_challenge": challenge,
                "redirect_uri": "http://127.0.0.1/cb",
            },
        )
        rid = parse_qs(urlparse(authorize.headers["location"]).query)["rid"][0]
        approved = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "hub-pass"},
        )
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token_resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        access = token_resp.json()["access_token"]
        refresh = token_resp.json()["refresh_token"]
        assert (
            client.get("/whoami", headers={"Authorization": f"Bearer {access}"}).status_code == 200
        )

        revoked = client.post(
            "/revoke",
            data={
                "token": refresh,
                "token_type_hint": "refresh_token",
                "client_id": client_id,
                "client_secret": "",
            },
        )
        assert revoked.status_code == 200
        assert (
            client.get("/whoami", headers={"Authorization": f"Bearer {access}"}).status_code == 401
        )
        replay = client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
        )
        assert replay.status_code == 400

    def test_oauth_whoami_reports_agent_profile_scopes_age(self, monkeypatch):
        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
            "whoami-cc|whoami-secret|cloud_agent13",
        )
        client = _app(monkeypatch)
        ok = client.post(
            "/oauth/client-credentials",
            data={
                "grant_type": "client_credentials",
                "client_id": "whoami-cc",
                "client_secret": "whoami-secret",
            },
        )
        access = ok.json()["access_token"]
        who = client.get("/oauth/whoami", headers={"Authorization": f"Bearer {access}"})
        assert who.status_code == 200
        body = who.json()
        assert body["agent"] == "cloud_agent13"
        assert body["profile"] == "chief-of-staff"
        assert "mcp" in body["scopes"]
        assert isinstance(body["token_age_s"], int)
        assert body["token_age_s"] >= 0
        assert body["via"] == "oauth"
        assert client.get("/oauth/whoami").status_code == 401
        assert (
            client.get("/oauth/whoami", headers={"Authorization": "Bearer not-a-token"}).status_code
            == 401
        )

    def test_doctor_whoami_cli(self, monkeypatch):
        from unified_agent import bootstrap

        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
            "doc-cc|doc-secret|Desktop_Agent1",
        )
        _enable(monkeypatch, default_agent="Desktop_Agent1")
        provider = oauth.get_provider()
        assert provider is not None
        token = provider.exchange_client_credentials("doc-cc", "doc-secret")
        monkeypatch.setenv("UA_WHOAMI_BEARER", token.access_token)
        assert bootstrap.doctor_main(["--whoami"]) == 0
        assert bootstrap.doctor_main(["--whoami", "--json"]) == 0
        monkeypatch.setenv("UA_WHOAMI_BEARER", "bogus")
        assert bootstrap.doctor_main(["--whoami"]) == 1


def test_one_hub_resolves_each_bot_credential_to_its_own_identity(monkeypatch):
    """One account, three client-credentials triples, three identities."""
    bots = {
        "cloud_agent13": "chief-of-staff",
        "cloud_agent14": "cto",
        "cloud_agent15": "developer",
    }
    monkeypatch.setenv("UA_AGENT_LABELS", ":".join([*bots, "Desktop_Agent1"]))
    monkeypatch.setenv(
        "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
        ",".join(f"{label}-id|{label}-secret|{label}" for label in bots),
    )
    client = _app(monkeypatch, default_agent="cloud_agent13")
    for label, profile in bots.items():
        issued = client.post(
            "/oauth/client-credentials",
            data={
                "grant_type": "client_credentials",
                "client_id": f"{label}-id",
                "client_secret": f"{label}-secret",
            },
        )
        assert issued.status_code == 200, issued.text
        who = client.get(
            "/oauth/whoami",
            headers={"Authorization": "Bearer " + issued.json()["access_token"]},
        )
        assert who.status_code == 200
        assert (who.json()["agent"], who.json()["profile"]) == (label, profile)


def test_revoked_bot_token_fails_closed(monkeypatch):
    """Revoking a bot refresh token rejects the linked access token."""
    monkeypatch.setenv("UA_AGENT_LABELS", "cloud_agent15:Desktop_Agent1")
    monkeypatch.setenv(
        "UA_MCP_OAUTH_CLIENT_CREDENTIALS",
        "swe-id|swe-secret|cloud_agent15",
    )
    client = _app(monkeypatch, default_agent="cloud_agent15")
    issued = client.post(
        "/oauth/client-credentials",
        data={
            "grant_type": "client_credentials",
            "client_id": "swe-id",
            "client_secret": "swe-secret",
        },
    )
    assert issued.status_code == 200, issued.text
    access = issued.json()["access_token"]
    refresh = issued.json()["refresh_token"]
    assert (
        client.get("/oauth/whoami", headers={"Authorization": "Bearer " + access}).status_code
        == 200
    )
    revoked = client.post(
        "/revoke",
        data={
            "token": refresh,
            "token_type_hint": "refresh_token",
            "client_id": "swe-id",
            "client_secret": "swe-secret",
        },
    )
    assert revoked.status_code == 200
    assert (
        client.get("/oauth/whoami", headers={"Authorization": "Bearer " + access}).status_code
        == 401
    )


# cloud_agent17 is a developer (2026-11-11). cloud_agent13 is chief-of-staff.
# cloud_agent8 is research. cloud_agent1 is a cloud-hosted developer.
# None of them gets the Control Center.
@pytest.mark.parametrize(
    ("agent", "profile"),
    [
        ("cloud_agent17", "developer"),
        ("cloud_agent13", "chief-of-staff"),
        ("cloud_agent8", "research"),
        ("cloud_agent14", "cto"),
        ("cloud_agent15", "developer"),
        ("cloud_agent16", "developer"),
        ("cloud_agent1", "developer"),
    ],
)
@pytest.mark.parametrize("managed", [False, True])
def test_cloud_oauth_identity_verification_remains_available(monkeypatch, managed, agent, profile):
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1" if managed else "0")
    monkeypatch.setenv("UA_AGENT_LABELS", f"{agent}:Desktop_Agent1")
    monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
    secret = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "cloud-test|" + secret + "|" + agent)
    client = _app(monkeypatch, default_agent=agent)
    issued = client.post(
        "/oauth/client-credentials",
        data={
            "grant_type": "client_credentials",
            "client_id": "cloud-test",
            "client_secret": secret,
        },
    )
    assert issued.status_code == 200
    token = issued.json()["access_token"]
    who = client.get("/oauth/whoami", headers={"Authorization": "Bearer " + token})
    assert who.status_code == 200
    assert who.json()["agent"] == agent
    assert who.json()["profile"] == profile
    assert (
        client.post(
            "/api/action/handoff",
            headers={"Authorization": "Bearer " + token},
            json={"to_agent": "Desktop_Agent1", "task_spec": "must not delegate"},
        ).status_code
        == 403
    )
