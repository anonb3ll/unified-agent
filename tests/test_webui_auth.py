"""Authentication for the web control center.

Two properties matter most here and both are asserted directly:

* You can always reach the login route. Gating `/api/session` behind auth is a
  deadlock — you would need a session to create a session — and it is an easy
  regression to introduce when tightening the middleware.
* The cookie never works on `/mcp`. Tool calls stay bearer-only, which is what keeps
  a cross-site request from reaching one even if it rides the cookie.
"""

import hashlib
import re
import secrets
import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.webui import auth
from unified_agent.webui import spa as web_spa

# Generated per run, not a literal: a fixed credential-shaped string in source
# trips the secret scanner, and rightly so — the scanner cannot tell a fixture
# from the real thing, and that is the property that makes it useful.
TOKEN = f"tok-{secrets.token_urlsafe(8)}"


def _mcp_stub(request):
    return PlainTextResponse(f"mcp:{request.scope.get('ua_identity')}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    storage.init_db()
    routes = [*webui.web_routes(), Route("/mcp", _mcp_stub, methods=["GET"])]
    app = server.HubAuthASGIMiddleware(Starlette(routes=routes))
    with TestClient(app) as c:
        yield c


def _login(client):
    res = client.post("/api/session", json={"token": TOKEN})
    assert res.status_code == 200, res.text
    return res


class TestEnforceMode:
    def test_data_requires_a_credential(self, client):
        assert client.get("/api/overview").status_code == 401

    def test_bearer_token_is_accepted(self, client):
        res = client.get("/api/overview", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 200

    def test_wrong_bearer_is_rejected(self, client):
        res = client.get("/api/overview", headers={"Authorization": "Bearer nope"})
        assert res.status_code == 401

    def test_healthz_stays_open(self, client):
        """A liveness probe that needs a credential is not a liveness probe."""
        assert client.get("/healthz").status_code == 200

    def test_login_page_is_reachable_without_a_credential(self, client, tmp_path, monkeypatch):
        """Otherwise there is no way to obtain one — the deadlock this guards.

        A minimal fake dist stands in for the Vite build so the test does not
        depend on whether packages/web was built on this host — the shell and
        its content-addressed assets must stay public so sign-in can render.
        """
        dist = tmp_path / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("<html><body>shell</body></html>", encoding="utf-8")
        (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)
        assert client.get("/ui").status_code == 200
        assert client.get("/ui/assets/app.js").status_code == 200

    def test_login_route_is_reachable_without_a_credential(self, client):
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401


class TestCookieSession:
    def test_login_sets_a_hardened_cookie(self, client):
        res = _login(client)
        assert res.json()["agent"] == "Desktop_Agent1"
        assert res.json()["csrf"]
        cookie = res.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie

    def test_cookie_is_not_secure_over_plain_http(self, client):
        """tailscale serve terminates TLS and proxies plaintext to loopback, so an
        unconditional Secure flag would break the primary localhost path."""
        assert "Secure" not in _login(client).headers["set-cookie"]

    def test_cookie_secure_flag_follows_the_request_scheme(self, client):
        """Task #1892: `Secure` follows the scope's scheme, not a raw header.

        A forwarded proto is honoured only after the listener-scoped proxy
        middleware (TrustedListenerProxyHeaders) settles it into the scope — on
        the untrusted TCP listener a bare X-Forwarded-Proto is ignored.
        """
        res = client.post(
            "/api/session", json={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"}
        )
        assert "Secure" not in res.headers["set-cookie"]
        res = client.post("https://127.0.0.1/api/session", json={"token": TOKEN})
        assert "Secure" in res.headers["set-cookie"]

    def test_cookie_authenticates_api_requests(self, client):
        _login(client)
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/whoami").json()["via"] == "cookie"

    def test_signout_revokes_immediately(self, client):
        csrf = _login(client).json()["csrf"]
        assert client.delete("/api/session", headers={"x-ua-csrf": csrf}).status_code == 200
        assert client.get("/api/overview").status_code == 401

    def test_signout_needs_the_csrf_token_of_the_session_its_cookie_names(
        self, client, monkeypatch
    ):
        """Review round 9: a sign-out started as A must not end B's session.

        The browser's cookie can change to B's between the CSRF read and the
        DELETE, so the request arrives with B's cookie and A's token. It is
        refused with a code, B's session survives and its cookie is kept.
        """
        other = f"tok-{secrets.token_urlsafe(8)}"
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN},Desktop_Agent2:{other}")
        csrf_a = _login(client).json()["csrf"]
        res_b = client.post("/api/session", json={"token": other})
        assert res_b.status_code == 200, res_b.text
        assert client.get("/api/whoami").json()["agent"] == "Desktop_Agent2"

        for headers in ({"x-ua-csrf": csrf_a}, {}):
            res = client.delete("/api/session", headers=headers)
            assert res.status_code == 403, res.text
            assert res.json()["code"] == "csrf_session_mismatch"
            assert "set-cookie" not in res.headers
            # B is still signed in.
            assert client.get("/api/whoami").json()["agent"] == "Desktop_Agent2"
            assert client.get("/api/overview").status_code == 200

        res = client.delete("/api/session", headers={"x-ua-csrf": res_b.json()["csrf"]})
        assert res.status_code == 200
        assert client.get("/api/overview").status_code == 401

    def test_signout_refuses_when_the_session_lookup_fails(self, client, monkeypatch):
        """Review round 10: a failed lookup is not "no live session".

        A lock timeout (or any SQLite error) while reading the session must not
        skip the CSRF comparison: B's cookie with a wrong token is answered with
        a code, nothing is revoked and the cookie is kept.
        """
        other = f"tok-{secrets.token_urlsafe(8)}"
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN},Desktop_Agent2:{other}")
        csrf_a = _login(client).json()["csrf"]
        assert client.post("/api/session", json={"token": other}).status_code == 200

        real_connect = storage.connect

        class LockedSessionReads:
            """A connection whose ui_sessions reads time out, as under a held write lock."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if "ui_sessions" in sql and sql.lstrip().upper().startswith("SELECT"):
                    raise sqlite3.OperationalError("database is locked")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with monkeypatch.context() as patched:
            patched.setattr(
                storage, "connect", lambda *a, **k: LockedSessionReads(real_connect(*a, **k))
            )
            res = client.delete("/api/session", headers={"x-ua-csrf": csrf_a})

        assert res.status_code == 503, res.text
        assert res.json()["code"] == "session_lookup_failed"
        assert "set-cookie" not in res.headers
        # B is still signed in.
        assert client.get("/api/whoami").json()["agent"] == "Desktop_Agent2"
        assert client.get("/api/overview").status_code == 200

    def test_signout_without_a_live_session_only_clears_the_cookie(self, client):
        assert client.delete("/api/session").status_code == 200

    def test_expired_session_is_rejected(self, client):
        _login(client)
        conn = storage.connect()
        try:
            conn.execute("UPDATE ui_sessions SET expires_at = '2020-01-01T00:00:00+00:00'")
            conn.commit()
        finally:
            conn.close()
        assert client.get("/api/overview").status_code == 401

    def test_revoked_session_is_rejected(self, client):
        _login(client)
        conn = storage.connect()
        try:
            conn.execute("UPDATE ui_sessions SET revoked = 1")
            conn.commit()
        finally:
            conn.close()
        assert client.get("/api/overview").status_code == 401

    def test_forged_cookie_is_rejected(self, client):
        client.cookies.set(auth.COOKIE_NAME, "made-up-session-id")
        assert client.get("/api/overview").status_code == 401

    def test_cookie_does_not_authenticate_mcp(self, client):
        """The rule that makes cookie auth safe: tool calls stay bearer-only."""
        _login(client)
        assert client.get("/mcp").status_code == 401
        res = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 200
        assert res.text == "mcp:Desktop_Agent1"


class TestWarnMode:
    def test_ui_is_not_stricter_than_the_hub_it_fronts(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            assert c.get("/api/overview").status_code == 200

    def test_no_tokens_configured_means_no_login_possible(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            res = c.post("/api/session", json={"token": "anything"})
            assert res.status_code == 401
            assert "no tokens configured" in res.json()["error"]


class TestAnonymousCounter:
    def test_anonymous_request_is_recorded_for_the_rollout_gauge(self, monkeypatch):
        """Before this, 'is anything still calling without a token?' was unanswerable,
        so flipping UA_ENFORCE_AUTH=1 was a leap rather than a decision."""
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        storage.init_db()
        server._last_anon_log = 0.0
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            c.get("/api/overview")

        from unified_agent import events

        recorded = events.query_events(channels=["hub.auth.anonymous"], limit=10)
        assert len(recorded) == 1
        assert recorded[0]["payload"]["path"] == "/api/overview"

    def test_browser_favicon_requests_do_not_inflate_the_gauge(self, monkeypatch):
        """Found in production after the M1 deploy: opening /ui in a browser logged an
        anonymous hub request for /favicon.ico, which the page never asked for and no
        client controls. The gauge exists to answer "is something still calling without
        a token?" before enforcing auth — counting a tab icon reports work that isn't
        there."""
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()
        server._last_anon_log = 0.0
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            res = c.get("/favicon.ico")
        assert res.status_code == 404, "favicon should 404, not 401"

        from unified_agent import events

        assert events.query_events(channels=["hub.auth.anonymous"], limit=10) == []

    def test_a_real_unauthenticated_api_call_is_still_counted(self, monkeypatch):
        """The exclusion list must stay narrow: widening it to quiet the number would
        hollow out the instrument."""
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()
        server._last_anon_log = 0.0
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            c.get("/api/whoami")

        from unified_agent import events

        assert len(events.query_events(channels=["hub.auth.anonymous"], limit=10)) == 1

    def test_public_paths_do_not_inflate_the_gauge(self, monkeypatch):
        """The login page loading is not the signal the rollout is measuring."""
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        storage.init_db()
        server._last_anon_log = 0.0
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            c.get("/ui")
            c.get("/healthz")

        from unified_agent import events

        assert events.query_events(channels=["hub.auth.anonymous"], limit=10) == []

    def test_counter_is_rate_limited(self, monkeypatch):
        """An unauthenticated client in a retry loop must not flood event_log."""
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        storage.init_db()
        server._last_anon_log = 0.0
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            for _ in range(5):
                c.get("/api/overview")

        from unified_agent import events

        assert len(events.query_events(channels=["hub.auth.anonymous"], limit=10)) == 1


class TestOriginMatcher:
    def test_exact_match(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_ALLOWED_ORIGINS", "http://example.test")
        assert auth.origin_allowed("http://example.test")
        assert not auth.origin_allowed("http://other.test")

    def test_any_port_wildcard(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_ALLOWED_ORIGINS", "http://desktop:*")
        assert auth.origin_allowed("http://desktop:9999")
        assert auth.origin_allowed("http://desktop")
        assert not auth.origin_allowed("http://desktop.evil.test:80")

    def test_defaults_include_the_hub_origin_but_not_null(self, monkeypatch):
        """Sandboxed iframes send Origin: null. That is opt-in, not the default."""
        monkeypatch.delenv("UA_HTTP_ALLOWED_ORIGINS", raising=False)
        monkeypatch.setenv("UA_HTTP_HOST", "127.0.0.1")
        monkeypatch.setenv("UA_HTTP_PORT", "8765")
        origins = config.http_allowed_origins()
        assert "null" not in origins
        assert "http://127.0.0.1:8765" in origins

    def test_explicit_null_origin_is_still_honored(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_ALLOWED_ORIGINS", "null,http://127.0.0.1:8765")
        assert config.http_allowed_origins()[0] == "null"
        assert auth.origin_allowed("null")

    def test_allowed_hosts_feed_the_origin_list(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_ALLOWED_ORIGINS", raising=False)
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "desktop.tailnet.ts.net:*")
        assert auth.origin_allowed("https://desktop.tailnet.ts.net:8765")


def _host_request(host: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/overview",
        "raw_path": b"/api/overview",
        "query_string": b"",
        "headers": [(b"host", host.encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
    }
    return Request(scope)


class TestHostAllowlist:
    def test_session_create_rejects_an_unexpected_host(self, client):
        refused = client.post(
            "/api/session",
            json={"token": TOKEN},
            headers={"host": "evil.example"},
        )
        assert refused.status_code == 403
        assert refused.json()["error"] == "host not allowed"
        assert "set-cookie" not in refused.headers

    def test_guard_rejects_an_unexpected_host(self, monkeypatch):
        from unified_agent.webui.routes import _guard

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        blocked = _guard(_host_request("evil.example"))
        assert blocked is not None
        assert blocked.status_code == 403
        assert b"host not allowed" in blocked.body

    def test_guard_accepts_loopback_with_a_port(self, monkeypatch):
        from unified_agent.webui.routes import _guard

        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        assert _guard(_host_request("127.0.0.1:8765")) is None
        assert _guard(_host_request("localhost")) is None

    def test_warn_mode_still_rejects_a_bad_host(self, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        monkeypatch.delenv("UA_HTTP_ALLOWED_HOSTS", raising=False)
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            assert client.get("/api/overview").status_code == 200
            refused = client.get("/api/overview", headers={"host": "evil.example"})
            assert refused.status_code == 403
            assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 200

    def test_configured_host_wildcard_matches_any_port(self, monkeypatch):
        from unified_agent.webui.routes import _guard

        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "desktop.tailnet.ts.net:*")
        assert _guard(_host_request("desktop.tailnet.ts.net:443")) is None
        assert _guard(_host_request("desktop.tailnet.ts.net")) is None
        blocked = _guard(_host_request("desktop.tailnet.ts.net.evil"))
        assert blocked is not None and blocked.status_code == 403


class TestSessionIdIsHashed:
    def test_create_session_stores_a_digest_and_resolves_the_raw_cookie(self, isolated_env):
        raw, csrf = auth.create_session("Desktop_Agent1", user_agent="pytest")
        conn = storage.connect()
        try:
            row = conn.execute("SELECT id, agent, csrf FROM ui_sessions").fetchone()
        finally:
            conn.close()
        assert row["id"] != raw
        assert re.fullmatch(r"[0-9a-f]{64}", row["id"])
        assert row["id"] == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in row["id"]
        resolved = auth.resolve_session(raw)
        assert resolved == {"agent": "Desktop_Agent1", "csrf": csrf}
        assert auth.resolve_session(row["id"]) is None
        assert auth.revoke_session(raw) is True
        assert auth.resolve_session(raw) is None
