"""Client ID Metadata Documents on the hub OAuth server (task #1323, Epic J4).

MCP auth spec 2025-11-25 prefers CIMD over dynamic registration: the client_id
is an HTTPS URL whose JSON document is the client's metadata. The network is a
mock transport here (the fixture server); the real transport is the SSRF shield.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse, urlunsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, oauth, oauth_cimd
from unified_agent.server import HubAuthASGIMiddleware

CLIENT_ID = "https://client.example.com/oauth/client.json"
REDIRECT = "http://127.0.0.1:7777/cb"


def _doc(**overrides) -> dict:
    doc = {
        "client_id": CLIENT_ID,
        "client_name": "Example MCP",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "none",
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not None}


class _Fixture:
    """A tiny document server behind httpx.MockTransport; counts fetches."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response] = {}
        self.fetches: list[str] = []

    def serve(self, url: str, body, status: int = 200, headers=None) -> None:
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routes[url] = httpx.Response(status, content=content, headers=headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetches.append(url)
        return self.routes.get(url, httpx.Response(404))


@pytest.fixture
def fixture_server(monkeypatch):
    fx = _Fixture()
    monkeypatch.setattr(oauth_cimd, "_transport", lambda: httpx.MockTransport(fx.handler))
    oauth_cimd.reset_cache()
    yield fx
    oauth_cimd.reset_cache()


@pytest.fixture(autouse=True)
def _reset():
    oauth.reset_provider()
    yield
    oauth.reset_provider()


def _app(monkeypatch, *, cimd: bool = True, allow: str = "") -> TestClient:
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "http://127.0.0.1:8765")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "hub-pass")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent8")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_CIMD", "1" if cimd else "0")
    monkeypatch.setenv("UA_MCP_OAUTH_CIMD_ALLOW", allow)
    oauth.reset_provider()

    async def homepage(request):
        return JSONResponse({"identity": config.verified_agent_identity()})

    # Not "/": exact "/" is public and redirects in the real hub (task
    # #1400), which would defeat the 401/identity assertions below.
    app = Starlette(routes=[Route("/whoami", homepage, methods=["GET"]), *oauth.http_routes()])
    return TestClient(HubAuthASGIMiddleware(app), follow_redirects=False)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _authorize(client: TestClient, challenge: str, client_id: str = CLIENT_ID):
    return client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT,
            "state": "st",
            "scope": "mcp",
        },
    )


class TestCimdFlow:
    def test_url_client_id_completes_authorization_code_flow(self, monkeypatch, fixture_server):
        fixture_server.serve(CLIENT_ID, _doc())
        client = _app(monkeypatch)
        verifier, challenge = _pkce()
        resp = _authorize(client, challenge)
        assert resp.status_code == 302, resp.text
        rid = parse_qs(urlparse(resp.headers["location"]).query)["rid"][0]

        page = client.get("/oauth/consent", params={"rid": rid}).text
        assert "Example MCP (client.example.com)" in page, "the host is always shown"

        approved = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                "passphrase": "hub-pass",
                "agent": "Desktop_Agent8",
            },
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        who = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token.json()['access_token']}"}
        )
        assert who.json()["identity"] == "Desktop_Agent8"

        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.json()["refresh_token"],
                "client_id": CLIENT_ID,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert fixture_server.fetches == [CLIENT_ID], "document cached across requests"

    def test_unregistered_redirect_uri_is_refused(self, monkeypatch, fixture_server):
        fixture_server.serve(CLIENT_ID, _doc(redirect_uris=["http://127.0.0.1:1/other"]))
        client = _app(monkeypatch)
        resp = _authorize(client, _pkce()[1])
        assert resp.status_code == 400

    def test_metadata_advertises_cimd_only_when_enabled(self, monkeypatch):
        client = _app(monkeypatch)
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["client_id_metadata_document_supported"] is True
        assert meta["registration_endpoint"], "DCR stays available"
        client = _app(monkeypatch, cimd=False)
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert "client_id_metadata_document_supported" not in meta

    def test_disabled_means_url_client_ids_are_unknown(self, monkeypatch, fixture_server):
        fixture_server.serve(CLIENT_ID, _doc())
        client = _app(monkeypatch, cimd=False)
        assert _authorize(client, _pkce()[1]).status_code == 400
        assert fixture_server.fetches == [], "nothing is fetched while CIMD is off"


class TestCimdValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "http://client.example.com/c.json",
            "https://client.example.com",
            "https://client.example.com/",
            # userinfo in the authority (built, so no literal credential URL sits in the diff)
            urlunsplit(("https", "someone" + chr(58) + "x@client.example.com", "/c.json", "", "")),
            "https://client.example.com/c.json?x=1",
            "https://client.example.com/c.json#frag",
            "https://client.example.com/a/../c.json",
        ],
    )
    def test_malformed_client_id_urls(self, bad):
        with pytest.raises(oauth_cimd.CIMDError):
            oauth_cimd.validate_client_id_url(bad)

    def test_allowlist(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH_CIMD_ALLOW", ".example.com,exact.test")
        assert oauth_cimd.validate_client_id_url(CLIENT_ID) == "client.example.com"
        assert oauth_cimd.validate_client_id_url("https://exact.test/c.json") == "exact.test"
        with pytest.raises(oauth_cimd.CIMDError, match="ALLOW"):
            oauth_cimd.validate_client_id_url("https://evil.test/c.json")
        with pytest.raises(oauth_cimd.CIMDError, match="ALLOW"):
            oauth_cimd.validate_client_id_url("https://notexample.com/c.json")

    @pytest.mark.parametrize(
        "doc, match",
        [
            (_doc(client_id="https://other.example.com/c.json"), "does not match"),
            (_doc(client_secret="s"), "secret"),
            (_doc(token_endpoint_auth_method="client_secret_post"), "public client"),
            (_doc(redirect_uris=[]), "redirect_uris"),
            (_doc(grant_types=["client_credentials"]), "grant type"),
        ],
    )
    def test_document_rules(self, fixture_server, doc, match):
        fixture_server.serve(CLIENT_ID, doc)
        with pytest.raises(oauth_cimd.CIMDError, match=match):
            oauth_cimd.resolve(CLIENT_ID)

    def test_redirects_are_refused(self, fixture_server):
        fixture_server.serve(
            CLIENT_ID, b"", status=302, headers={"location": "https://127.0.0.1/c.json"}
        )
        with pytest.raises(oauth_cimd.CIMDError, match="redirect"):
            oauth_cimd.resolve(CLIENT_ID)
        assert fixture_server.fetches == [CLIENT_ID], "the redirect is never followed"

    def test_oversized_document_is_refused(self, fixture_server):
        fixture_server.serve(CLIENT_ID, b"{" + b" " * (oauth_cimd.MAX_DOCUMENT_BYTES + 10) + b"}")
        with pytest.raises(oauth_cimd.CIMDError, match="too large"):
            oauth_cimd.resolve(CLIENT_ID)

    def test_failures_are_negatively_cached(self, fixture_server):
        for _ in range(3):
            with pytest.raises(oauth_cimd.CIMDError):
                oauth_cimd.resolve(CLIENT_ID)
        assert len(fixture_server.fetches) == 1

    def test_cache_expires(self, fixture_server, monkeypatch):
        fixture_server.serve(CLIENT_ID, _doc())
        now = [1000.0]
        monkeypatch.setattr(oauth_cimd, "_clock", lambda: now[0])
        oauth_cimd.resolve(CLIENT_ID)
        oauth_cimd.resolve(CLIENT_ID)
        now[0] += oauth_cimd.CACHE_TTL_S + 1
        oauth_cimd.resolve(CLIENT_ID)
        assert len(fixture_server.fetches) == 2

    def test_remote_name_cannot_pose_as_a_fleet_label(self, fixture_server):
        fixture_server.serve(CLIENT_ID, _doc(client_name="Desktop_Agent1"))
        client = oauth_cimd.resolve(CLIENT_ID)
        assert client.client_name == "Desktop_Agent1 (client.example.com)"
        assert oauth._resolve_suggested_agent(client) != "Desktop_Agent1"

    def test_real_transport_refuses_private_addresses_even_with_local_override(self, monkeypatch):
        """No mock: the transport refuses a private resolution, and ignores the
        UA_ALLOW_LOCAL_FETCH override that agent web fetches honour."""
        monkeypatch.setenv("UA_ALLOW_LOCAL_FETCH", "1")
        monkeypatch.setattr(
            "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))]
        )
        oauth_cimd.reset_cache()
        with pytest.raises(oauth_cimd.CIMDError, match="non-public"):
            oauth_cimd.resolve("https://internal.example.com/c.json")


class TestReviewRound1:
    """PR #764 review: strict URLs, dedup, fetch cap, TTL timing, allowlist, order."""

    @pytest.mark.parametrize(
        "bad",
        [
            "https://[oops/path",
            "https://93.184.216.34/c.json",
            "https://[2001:db8::1]/c.json",
            "https://client.example.com:8443/c.json",
            "https://client.example.com/a%2F..%2Fb",
            "https://client.example.com/%2e%2e/c.json",
            "https://client%2eexample.com/c.json",
            "https://client.example.com/c json",
            "https://CLIENT.example.com/c.json",
            "https://localhost/c.json",
            "https://client.example.com\\@evil.test/c.json",
        ],
    )
    def test_strict_url_rejections_never_raise_other_errors(self, bad):
        with pytest.raises(oauth_cimd.CIMDError):
            oauth_cimd.validate_client_id_url(bad)

    def test_non_string_grant_types_are_rejected_cleanly(self, fixture_server):
        fixture_server.serve(CLIENT_ID, _doc(grant_types=[{}]))
        with pytest.raises(oauth_cimd.CIMDError, match="non-string"):
            oauth_cimd.resolve(CLIENT_ID)

    def test_concurrent_lookups_share_one_fetch(self, fixture_server, monkeypatch):
        import threading

        fixture_server.serve(CLIENT_ID, _doc())
        gate = threading.Event()
        real = fixture_server.handler

        def slow(request):
            gate.wait(2)
            return real(request)

        monkeypatch.setattr(oauth_cimd, "_transport", lambda: httpx.MockTransport(slow))
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(oauth_cimd.resolve(CLIENT_ID)))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(5)
        assert len(results) == 8
        assert fixture_server.fetches == [CLIENT_ID]

    def test_inflight_cap_refuses_without_negative_caching(self, fixture_server, monkeypatch):
        fixture_server.serve(CLIENT_ID, _doc())
        sem = oauth_cimd._inflight
        taken = 0
        while sem.acquire(blocking=False):
            taken += 1
        try:
            with pytest.raises(oauth_cimd.CIMDError, match="in flight"):
                oauth_cimd.resolve(CLIENT_ID)
        finally:
            for _ in range(taken):
                sem.release()
        assert oauth_cimd.resolve(CLIENT_ID).client_id == CLIENT_ID, "busy is not cached"

    def test_ttl_starts_when_the_fetch_completes(self, fixture_server, monkeypatch):
        fixture_server.serve(CLIENT_ID, _doc())
        now = [1000.0]
        real = fixture_server.handler

        def slow(request):
            now[0] += 50  # the fetch itself takes 50 s of clock time
            return real(request)

        monkeypatch.setattr(oauth_cimd, "_transport", lambda: httpx.MockTransport(slow))
        monkeypatch.setattr(oauth_cimd, "_clock", lambda: now[0])
        oauth_cimd.resolve(CLIENT_ID)
        assert oauth_cimd._cache[CLIENT_ID].expires_at == 1050.0 + oauth_cimd.CACHE_TTL_S

    def test_tightened_allowlist_applies_to_cached_clients(self, fixture_server, monkeypatch):
        fixture_server.serve(CLIENT_ID, _doc())
        oauth_cimd.resolve(CLIENT_ID)
        monkeypatch.setenv("UA_MCP_OAUTH_CIMD_ALLOW", "only.test")
        with pytest.raises(oauth_cimd.CIMDError, match="ALLOW"):
            oauth_cimd.resolve(CLIENT_ID)

    def test_stored_registration_wins_over_cimd_for_url_ids(self, monkeypatch, fixture_server):
        import asyncio

        from unified_agent import oauth_store

        _app(monkeypatch, cimd=False)
        oauth_store.save_client(
            CLIENT_ID,
            {
                "client_id": CLIENT_ID,
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
            },
            None,
        )
        provider = oauth.get_provider()
        provider.clients.pop(CLIENT_ID, None)
        got = asyncio.run(provider.get_client(CLIENT_ID))
        assert got is not None and got.client_id == CLIENT_ID
        assert fixture_server.fetches == []


class TestRevokeFailsClosed:
    def test_revoke_with_failing_store_denies_durable_reload(self, monkeypatch):
        import asyncio

        from unified_agent import oauth_store

        _app(monkeypatch)
        provider = oauth.get_provider()
        issued = provider._issue(
            provider.clients["unified-agent-cli"], ["mcp"], None, subject="Desktop_Agent8"
        )
        access = provider._lookup_access(issued.access_token)

        def boom(*a, **k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(oauth_store, "delete_access", boom)
        monkeypatch.setattr(oauth_store, "delete_refresh", boom)
        asyncio.run(provider.revoke_token(access))
        assert provider._lookup_access(issued.access_token) is None
        assert provider._load_persisted_refresh(issued.refresh_token) is None
        assert oauth_store.write_failure_status()["last_kind"] == "revoke"
        oauth_store.reset_write_failures()


def test_shared_ssrf_shield_strict_mode(monkeypatch):
    """PR #764 review round 2: strict mode lives in safety.check_fetch_host."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import safety

    monkeypatch.setenv("UA_ALLOW_LOCAL_FETCH", "1")
    # The override still admits a private address in default mode...
    assert safety.check_fetch_host("10.0.0.5") == "10.0.0.5"
    # ...but strict mode ignores it, and also refuses tailnet CGNAT space.
    for addr in ("10.0.0.5", "100.64.0.10", "127.0.0.1", "192.0.2.1"):
        with pytest.raises(ToolError):
            safety.check_fetch_host(addr, strict=True)
    assert safety.check_fetch_host("93.184.216.34", strict=True) == "93.184.216.34"
    assert isinstance(oauth_cimd._transport(), safety.SSRFShieldTransport)
    assert oauth_cimd._transport().strict is True
