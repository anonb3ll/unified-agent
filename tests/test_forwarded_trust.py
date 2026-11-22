"""Task #1892: ``X-Forwarded-*`` trust is scoped to the listener it arrives on.

Before this change the HTTP hub ran uvicorn with ``proxy_headers=True`` (the
default) and ``forwarded_allow_ips=127.0.0.1``, so any process on the hub host
could send ``X-Forwarded-For`` and make the hub resolve a claimed tailnet
peer — satisfying device provenance (#1250) and the session-credential device
check (#1744) with a spoofed identity. Forwarded headers are now honoured only
where a trusted proxy is actually expected: on the owner-only Unix listener
(``UA_HTTP_PROXY_SOCKET``) or, under the explicit ``UA_HTTP_PROXY_HEADERS=1``
escape hatch, on TCP peers matching ``UA_HTTP_FORWARDED_ALLOW_IPS``.
"""

from __future__ import annotations

import json
import os
import socket
import stat

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from unified_agent import config, server, tailnet


def _recorded_app():
    seen: dict = {}

    async def app(scope, receive, send):
        seen.update(scope)

    return app, seen


def _http_scope(
    *,
    client=("127.0.0.1", 51000),
    server=("127.0.0.1", 8765),
    headers=(),
    path="/mcp",
    method="GET",
    query_string=b"",
):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": list(headers),
        "client": client,
        "server": server,
        "scheme": "http",
        "query_string": query_string,
    }


def _socket_scope(sock, *, headers=(), client=None, path="/mcp", method="GET", query_string=b""):
    """The scope uvicorn builds for a connection on the trusted Unix listener."""
    return _http_scope(
        client=client,
        server=(str(sock), None),
        headers=headers,
        path=path,
        method=method,
        query_string=query_string,
    )


def _host_header(scope) -> str | None:
    raw = next((v for n, v in scope["headers"] if n == b"host"), None)
    return raw.decode("latin-1") if raw is not None else None


async def _round_trip(app, scope, body=b""):
    """Drive one HTTP request through a full ASGI stack; return (status, body)."""
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


class TestConfig:
    def test_proxy_headers_default_off(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_PROXY_HEADERS", raising=False)
        assert config.http_proxy_headers() is False

    @pytest.mark.parametrize("raw", ["1", "on", "true", "yes", "ON"])
    def test_proxy_headers_opted_in(self, monkeypatch, raw):
        monkeypatch.setenv("UA_HTTP_PROXY_HEADERS", raw)
        assert config.http_proxy_headers() is True

    @pytest.mark.parametrize("raw", ["0", "off", "nonsense", "2"])
    def test_proxy_headers_unknown_values_fail_closed(self, monkeypatch, raw):
        monkeypatch.setenv("UA_HTTP_PROXY_HEADERS", raw)
        assert config.http_proxy_headers() is False

    def test_forwarded_allow_ips_defaults_to_uvicorns(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_FORWARDED_ALLOW_IPS", raising=False)
        assert config.http_forwarded_allow_ips() == "127.0.0.1"

    def test_forwarded_allow_ips_override(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_FORWARDED_ALLOW_IPS", "127.0.0.1, 10.0.0.0/8")
        assert config.http_forwarded_allow_ips() == "127.0.0.1, 10.0.0.0/8"

    def test_trusted_proxy_socket_default_unset(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_PROXY_SOCKET", raising=False)
        assert config.http_trusted_proxy_socket() is None

    def test_trusted_proxy_socket_returns_absolute_path(self, monkeypatch, tmp_path):
        sock = tmp_path / "run" / "hub.sock"
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", str(sock))
        assert config.http_trusted_proxy_socket() == sock

    def test_trusted_proxy_socket_expands_user(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", "~/run/hub.sock")
        got = config.http_trusted_proxy_socket()
        assert got is not None and got.is_absolute()

    def test_trusted_proxy_socket_rejects_relative(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", "run/hub.sock")
        with pytest.raises(ValueError, match="UA_HTTP_PROXY_SOCKET"):
            config.http_trusted_proxy_socket()

    def test_trusted_proxy_socket_rejects_sun_path_overflow(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", "/" + "s" * 200)
        with pytest.raises(ValueError, match="UA_HTTP_PROXY_SOCKET"):
            config.http_trusted_proxy_socket()


class TestTrustedListenerProxyHeaders:
    async def test_loopback_tcp_xff_is_ignored_by_default(self):
        """The exploit itself: a loopback peer cannot claim a tailnet IP."""
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips=None
        )
        await mw(_http_scope(headers=[(b"x-forwarded-for", b"100.64.0.14")]), None, None)
        assert seen["client"] == ("127.0.0.1", 51000)

    async def test_loopback_tcp_x_forwarded_proto_is_ignored_by_default(self):
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips=None
        )
        await mw(_http_scope(headers=[(b"x-forwarded-proto", b"https")]), None, None)
        assert seen["scheme"] == "http"

    async def test_tcp_xff_honoured_for_allowed_peer(self):
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips="127.0.0.1"
        )
        await mw(_http_scope(headers=[(b"x-forwarded-for", b"100.64.0.14")]), None, None)
        assert seen["client"] == ("100.64.0.14", 0)

    async def test_tcp_xff_ignored_for_peer_outside_allow_ips(self):
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips="127.0.0.1"
        )
        await mw(
            _http_scope(
                client=("10.1.2.3", 51000),
                headers=[(b"x-forwarded-for", b"100.64.0.14")],
            ),
            None,
            None,
        )
        assert seen["client"] == ("10.1.2.3", 51000)

    async def test_tcp_xff_honoured_for_peer_in_allowed_network(self):
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips="10.0.0.0/8"
        )
        await mw(
            _http_scope(
                client=("10.1.2.3", 51000),
                headers=[(b"x-forwarded-for", b"100.64.0.14")],
            ),
            None,
            None,
        )
        assert seen["client"] == ("100.64.0.14", 0)

    async def test_unix_listener_trusts_forwarded_headers(self, tmp_path):
        """Tailscale Serve -> unix socket: the forwarded tailnet peer wins."""
        sock = str(tmp_path / "hub.sock")
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=sock, tcp_forwarded_allow_ips=None
        )
        await mw(
            _http_scope(
                client=None,
                server=(sock, None),
                headers=[
                    (b"x-forwarded-for", b"100.64.0.14"),
                    (b"x-forwarded-proto", b"https"),
                ],
            ),
            None,
            None,
        )
        assert seen["client"] == ("100.64.0.14", 0)
        assert seen["scheme"] == "https"

    async def test_other_listeners_stay_untrusted_when_socket_configured(self, tmp_path):
        """Configuring the socket must not widen trust onto the TCP listener."""
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app,
            trusted_socket_path=str(tmp_path / "hub.sock"),
            tcp_forwarded_allow_ips=None,
        )
        await mw(_http_scope(headers=[(b"x-forwarded-for", b"100.64.0.14")]), None, None)
        assert seen["client"] == ("127.0.0.1", 51000)

    async def test_spoofed_forwarded_ip_never_reaches_whois(self, monkeypatch):
        """End of the exploit chain: the ignored XFF leaves a loopback client,
        which the peer resolver refuses without ever spawning whois."""
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=None, tcp_forwarded_allow_ips=None
        )
        await mw(_http_scope(headers=[(b"x-forwarded-for", b"100.64.0.14")]), None, None)
        calls = []
        monkeypatch.setattr(tailnet, "_run_whois", lambda ip: calls.append(ip))
        client = seen["client"]
        assert tailnet.resolve_peer_device(client[0]) is None
        assert calls == []


class TestServeHttpHubSockets:
    def _capture(self, monkeypatch):
        import uvicorn

        monkeypatch.setenv("UA_HTTP_HOST", "127.0.0.1")
        monkeypatch.setenv("UA_HTTP_PORT", "0")
        sentinel_app = object()
        monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: sentinel_app)
        captured: dict = {}

        async def fake_serve(self, sockets=None):
            captured["config"] = self.config
            captured["sockets"] = sockets

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        return captured, sentinel_app

    def _serve_and_collect(self, captured):
        """Run the hub entry point, snapshot socket facts, then close them."""
        infos = []
        try:
            server._serve_http_hub()
            infos = [(s.family, s.getsockname()) for s in captured["sockets"]]
        finally:
            for sock in captured.get("sockets") or []:
                sock.close()
        return infos

    def test_tcp_only_by_default_and_proxy_headers_off(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_PROXY_SOCKET", raising=False)
        captured, sentinel_app = self._capture(monkeypatch)
        infos = self._serve_and_collect(captured)
        cfg = captured["config"]
        assert cfg.app is sentinel_app
        # Never uvicorn's connecting-IP trust: per-listener trust lives in
        # TrustedListenerProxyHeaders inside the wrapped app.
        assert cfg.proxy_headers is False
        assert len(infos) == 1
        family, name = infos[0]
        assert family == socket.AF_INET
        assert name[0] == "127.0.0.1"

    def test_trusted_socket_adds_owner_only_unix_listener(self, monkeypatch, tmp_path):
        sock_path = tmp_path / "run" / "hub.sock"
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", str(sock_path))
        captured, _ = self._capture(monkeypatch)
        infos = self._serve_and_collect(captured)
        assert [family for family, _ in infos] == [socket.AF_INET, socket.AF_UNIX]
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        # uvicorn would have made it 0666; the parent dir is owner-only too.
        assert stat.S_IMODE(sock_path.parent.stat().st_mode) == 0o700

    def test_stale_socket_file_is_replaced(self, monkeypatch, tmp_path):
        sock_path = tmp_path / "hub.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(sock_path))
        stale.close()
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", str(sock_path))
        captured, _ = self._capture(monkeypatch)
        infos = self._serve_and_collect(captured)
        assert len(infos) == 2
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600

    def test_non_socket_file_at_socket_path_is_never_unlinked(self, monkeypatch, tmp_path):
        sock_path = tmp_path / "hub.sock"
        sock_path.write_text("not a socket")
        monkeypatch.setenv("UA_HTTP_PROXY_SOCKET", str(sock_path))
        captured, _ = self._capture(monkeypatch)
        try:
            with pytest.raises(RuntimeError, match="UA_HTTP_PROXY_SOCKET"):
                server._serve_http_hub()
        finally:
            for sock in captured.get("sockets") or []:
                sock.close()
        assert sock_path.read_text() == "not a socket"


class TestSocketForwardedHost:
    """Host restoration on the trusted socket only (task #1892, round 2).

    Tailscale Serve's ``unix:`` backend proxies as ``http://localhost``: the
    connection arrives with ``Host: localhost`` and the browser's host only in
    ``X-Forwarded-Host``, which tailscaled sets after stripping any
    client-supplied value. The trusted socket restores the real Host so the
    public-host slice, the connector-callback host check, allowed-hosts and
    the same-origin fallback all keep working. TCP never honours it.
    """

    def _mw(self, app, sock, *, tcp_allow=None):
        return server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=str(sock), tcp_forwarded_allow_ips=tcp_allow
        )

    async def test_socket_forwarded_host_replaces_localhost(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test"),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "hub.example.test"

    async def test_socket_forwarded_host_may_carry_a_port(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test:443"),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "hub.example.test:443"

    async def test_socket_without_forwarded_host_keeps_proxy_host(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(sock, headers=[(b"host", b"localhost")]),
            None,
            None,
        )
        assert _host_header(seen) == "localhost"

    @pytest.mark.parametrize(
        "bad",
        [
            b"hub.example.test, evil.example.test",
            b"hub.example.test,evil.example.test",
            b"evil.example.test/x",
            b"evil example.test",
            b"user@evil.example.test",
            b"",
            b"   ",
            b"hub.example.test:port",
            b"hub.example.test:99999",
            b"-bad-.example.test..",
        ],
    )
    async def test_socket_malformed_forwarded_host_keeps_localhost(self, tmp_path, bad):
        """One exact host or nothing: a malformed value can only come from the
        trusted proxy itself (clients cannot inject it), so the connection
        degrades to the proxy's own Host claim rather than routing by a
        smuggled name."""
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[(b"host", b"localhost"), (b"x-forwarded-host", bad)],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "localhost"

    async def test_socket_duplicate_forwarded_host_headers_keep_localhost(self, tmp_path):
        """tailscaled emits exactly one X-Forwarded-Host; two means something
        downstream tried to add one — refuse both."""
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test"),
                    (b"x-forwarded-host", b"evil.example.test"),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "localhost"

    async def test_tcp_forwarded_host_is_ignored_by_default(self):
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app,
            trusted_socket_path="/tmp/unused-hub.sock",
            tcp_forwarded_allow_ips=None,
        )
        await mw(
            _http_scope(
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test"),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "localhost"

    async def test_tcp_forwarded_host_is_ignored_with_legacy_flag(self):
        """The legacy TCP path keeps the connecting peer's real Host header —
        today's TCP proxy does — so X-Forwarded-Host must not rewrite it."""
        app, seen = _recorded_app()
        mw = server.TrustedListenerProxyHeaders(
            app,
            trusted_socket_path="/tmp/unused-hub.sock",
            tcp_forwarded_allow_ips="127.0.0.1",
        )
        await mw(
            _http_scope(
                headers=[
                    (b"host", b"real.example.test"),
                    (b"x-forwarded-host", b"hub.example.test"),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == "real.example.test"

    async def test_socket_allowed_host_reaches_the_app(self, tmp_path):
        """The header UA_HTTP_ALLOWED_HOSTS / transport security compares is
        the rewritten one, so /mcp no longer 421s on the socket mount."""
        sock = tmp_path / "hub.sock"
        monkey_host = "hub.example.ts.net"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", monkey_host.encode()),
                ],
            ),
            None,
            None,
        )
        assert _host_header(seen) == monkey_host


class TestSocketXffHopSelection:
    """Rightmost-untrusted XFF selection on the trusted socket (task #1892).

    The socket's only client is the local proxy, modelled as 127.0.0.1; the
    allowlist is the ordinary one, so an appending proxy cannot let a
    client-supplied leftmost hop win (uvicorn's ``always_trust`` leftmost
    selection was the round-1 hole).
    """

    def _mw(self, app, sock):
        return server.TrustedListenerProxyHeaders(
            app, trusted_socket_path=str(sock), tcp_forwarded_allow_ips=None
        )

    async def test_socket_without_xff_seeds_loopback_client(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(_socket_scope(sock), None, None)
        assert seen["client"] == ("127.0.0.1", 0)

    async def test_socket_single_hop_is_the_peer(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(sock, headers=[(b"x-forwarded-for", b"100.64.0.14")]),
            None,
            None,
        )
        assert seen["client"] == ("100.64.0.14", 0)

    async def test_socket_two_hop_xff_rightmost_untrusted_wins(self, tmp_path):
        """A client-supplied left hop cannot win: the proxy-appended rightmost
        hop is the one selected."""
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[(b"x-forwarded-for", b"203.0.113.66, 100.64.0.14")],
            ),
            None,
            None,
        )
        assert seen["client"] == ("100.64.0.14", 0)

    async def test_socket_trusted_right_hop_walks_left(self, tmp_path):
        sock = tmp_path / "hub.sock"
        app, seen = _recorded_app()
        await self._mw(app, sock)(
            _socket_scope(
                sock,
                headers=[(b"x-forwarded-for", b"100.64.0.14, 127.0.0.1")],
            ),
            None,
            None,
        )
        assert seen["client"] == ("100.64.0.14", 0)


class TestEndToEndForwardedHost:
    """A real request through TrustedListenerProxyHeaders -> HubAuthASGIMiddleware -> Starlette."""

    def _stack(self, sock):
        async def echo(request):
            return JSONResponse({"path": request.url.path, "host": request.headers.get("host")})

        app = Starlette(
            routes=[
                Route("/mcp", echo, methods=["GET", "POST"]),
                Route("/ui", echo),
                Route("/api/board", echo),
            ]
        )
        return server.TrustedListenerProxyHeaders(
            server.HubAuthASGIMiddleware(app),
            trusted_socket_path=str(sock),
            tcp_forwarded_allow_ips=None,
        )

    async def test_public_host_gate_sees_the_forwarded_host(self, tmp_path, monkeypatch):
        """The round-1 hole: without the rewrite a Funnel request arrived as
        Host localhost and the public slice did not apply, exposing /ui + /api."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            (b"x-forwarded-host", b"public.example.test"),
        ]
        for blocked in ("/ui", "/api/board"):
            status, body = await _round_trip(
                app, _socket_scope(sock, headers=headers, path=blocked)
            )
            assert status == 404, blocked
            assert b"not found" in body
        status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path="/mcp"))
        assert status == 200

    async def test_socket_without_forwarded_host_serves_localhost(self, tmp_path, monkeypatch):
        """No X-Forwarded-Host -> the proxy's own Host claim stands; localhost
        is not a public host, so the full (authenticated) app is served."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        status, body = await _round_trip(
            app, _socket_scope(sock, headers=[(b"host", b"localhost")], path="/ui")
        )
        assert status == 200
        assert b'"host":"localhost"' in body

    async def test_socket_malformed_forwarded_host_degrades_to_localhost(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            (b"x-forwarded-host", b"public.example.test, evil.example.test"),
        ]
        status, body = await _round_trip(app, _socket_scope(sock, headers=headers, path="/ui"))
        assert status == 200
        assert b'"host":"localhost"' in body

    async def test_tcp_ignores_forwarded_host_headers(self, tmp_path, monkeypatch):
        """The same headers over TCP are ignored entirely: the public host
        cannot be asserted on the untrusted listener."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            (b"x-forwarded-host", b"public.example.test"),
        ]
        status, body = await _round_trip(
            app,
            _http_scope(headers=headers, path="/ui"),
        )
        assert status == 200
        assert b'"host":"localhost"' in body


class TestFunnelRequestHeader:
    """tailscaled's Funnel marker forces the public slice on every listener (#1892 r3).

    Funnel terminates TLS on the configured SNI while the HTTP Host can claim
    anything — ``localhost`` included — and tailscaled copies that claim into
    ``X-Forwarded-Host`` on the socket, so neither header can tell public
    traffic apart from the proxy's own localhost presentation. tailscaled
    strips client copies of ``Tailscale-Funnel-Request`` and re-emits ``?1``
    only for Funnel connections: its presence is the authoritative public
    signal. It can only restrict, so it is honoured on every listener —
    which also closes the pre-existing TCP-proxy hole, where the client's
    Host always reached the hub intact.
    """

    FUNNEL = (b"tailscale-funnel-request", b"?1")

    def _stack(self, sock, *, tcp_allow=None):
        async def echo(request):
            return JSONResponse({"path": request.url.path, "host": request.headers.get("host")})

        app = Starlette(
            routes=[
                Route("/mcp", echo, methods=["GET", "POST"]),
                Route("/ui", echo),
                Route("/api/board", echo),
                Route("/oauth/consent", echo),
            ]
        )
        return server.TrustedListenerProxyHeaders(
            server.HubAuthASGIMiddleware(app),
            trusted_socket_path=str(sock),
            tcp_forwarded_allow_ips=tcp_allow,
        )

    def _slice_env(self, monkeypatch):
        from unified_agent import oauth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        # /oauth/consent sits in the public slice only while OAuth is on.
        monkeypatch.setattr(oauth, "get_provider", lambda: object())

    async def test_funnel_socket_localhost_gets_the_public_slice(self, tmp_path, monkeypatch):
        """The round-3 hole: Funnel + claimed-localhost. A public client can
        send Host: localhost because TLS picked the connection on SNI; on the
        socket that claim rides in X-Forwarded-Host too. Only the funnel
        marker still says the request is public."""
        self._slice_env(monkeypatch)
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            (b"x-forwarded-host", b"localhost"),
            self.FUNNEL,
        ]
        for blocked in ("/ui", "/api/board"):
            status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path=blocked))
            assert status == 404, blocked
        for allowed in ("/mcp", "/oauth/consent"):
            status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path=allowed))
            assert status == 200, allowed

    @pytest.mark.parametrize(
        "xfh",
        [
            None,
            b"public.example.test, evil.example.test",
            b"hub.example.test:port",
        ],
        ids=["missing", "comma-list", "bad-port"],
    )
    async def test_funnel_socket_bad_forwarded_host_still_sliced(self, tmp_path, monkeypatch, xfh):
        """A missing or malformed X-Forwarded-Host on a funnel request takes
        the same closed path — without the marker those degrade to
        ``Host: localhost`` and the full app (the pre-round-3 hole)."""
        self._slice_env(monkeypatch)
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [(b"host", b"localhost"), self.FUNNEL]
        if xfh is not None:
            headers.append((b"x-forwarded-host", xfh))
        status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path="/ui"))
        assert status == 404

    async def test_funnel_socket_duplicate_forwarded_host_still_sliced(self, tmp_path, monkeypatch):
        self._slice_env(monkeypatch)
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            self.FUNNEL,
            (b"x-forwarded-host", b"public.example.test"),
            (b"x-forwarded-host", b"public.example.test"),
        ]
        status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path="/ui"))
        assert status == 404

    async def test_funnel_marker_sliced_even_without_public_hosts(self, tmp_path, monkeypatch):
        """The marker is stronger than the config list: a Funnel request is
        public by definition, so the slice still applies when
        UA_HTTP_PUBLIC_HOSTS was never configured."""
        from unified_agent import oauth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.delenv("UA_HTTP_PUBLIC_HOSTS", raising=False)
        monkeypatch.setattr(oauth, "get_provider", lambda: object())
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [(b"host", b"localhost"), self.FUNNEL]
        status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path="/ui"))
        assert status == 404
        status, _ = await _round_trip(app, _socket_scope(sock, headers=headers, path="/mcp"))
        assert status == 200

    async def test_funnel_tcp_localhost_gets_the_public_slice(self, tmp_path, monkeypatch):
        """The pre-existing TCP hole, closed: ``proxy http://127.0.0.1:8765``
        keeps the client Host, so ``Host: localhost`` + the marker arrives
        over plain TCP — the slice applies there too."""
        self._slice_env(monkeypatch)
        app = self._stack(tmp_path / "hub.sock")
        headers = [(b"host", b"localhost"), self.FUNNEL]
        for blocked in ("/ui", "/api/board"):
            status, _ = await _round_trip(app, _http_scope(headers=headers, path=blocked))
            assert status == 404, blocked
        status, _ = await _round_trip(app, _http_scope(headers=headers, path="/mcp"))
        assert status == 200

    async def test_funnel_tcp_legacy_forwarded_flag_still_sliced(self, tmp_path, monkeypatch):
        """The marker is honoured on every listener, including the legacy
        ``UA_HTTP_PROXY_HEADERS`` TCP trust path."""
        self._slice_env(monkeypatch)
        app = self._stack(tmp_path / "hub.sock", tcp_allow="127.0.0.1")
        headers = [(b"host", b"localhost"), self.FUNNEL]
        status, _ = await _round_trip(app, _http_scope(headers=headers, path="/ui"))
        assert status == 404

    async def test_funnel_session_credential_refused_before_verify(self, tmp_path, monkeypatch):
        """A ``uas_`` session credential presented over Funnel is refused like
        a public Host — before it can reach verification."""
        from unified_agent import session_credentials

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        calls = []

        def no_verify(*args, **kwargs):
            calls.append(1)
            raise AssertionError("verify ran for a funnel request")

        monkeypatch.setattr(session_credentials, "verify", no_verify)
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        headers = [
            (b"host", b"localhost"),
            self.FUNNEL,
            (b"authorization", b"Bearer uas_stolen"),
        ]
        status, body = await _round_trip(app, _socket_scope(sock, headers=headers, path="/mcp"))
        assert status == 401
        assert b"session credential check failed" in body
        assert calls == []

    async def test_funnel_device_page_renders_pinned_agent(self, tmp_path, monkeypatch):
        """End of the consent half of the exploit chain: a funnel request
        claiming ``Host: localhost`` renders the pinned public-agent block,
        not the selectable one."""
        from unified_agent import oauth

        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "hub-pass")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Public_Agent")
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        oauth.reset_provider()
        sock = tmp_path / "hub.sock"
        app = Starlette(routes=[*oauth.http_routes()])
        stack = server.TrustedListenerProxyHeaders(
            server.HubAuthASGIMiddleware(app),
            trusted_socket_path=str(sock),
            tcp_forwarded_allow_ips=None,
        )
        headers = [
            (b"host", b"localhost"),
            self.FUNNEL,
            (b"content-type", b"application/x-www-form-urlencoded"),
        ]
        status, body = await _round_trip(
            stack,
            _socket_scope(sock, headers=headers, path="/oauth/device_authorization", method="POST"),
            body=b"client_id=unified-agent-cli",
        )
        assert status == 200, body
        user_code = json.loads(body)["user_code"]
        status, body = await _round_trip(
            stack,
            _socket_scope(
                sock,
                headers=headers,
                path="/oauth/device",
                query_string=f"user_code={user_code}".encode(),
            ),
        )
        assert status == 200
        assert b"Agent Identity (pinned)" in body
        assert b'list="agent-list"' not in body
        assert b"Public_Agent" in body


class TestConnectorCallbackForwardedHost:
    """The connector OAuth callback's host check works over the socket mount."""

    def _stack(self, sock):
        from unified_agent import webui

        app = Starlette(routes=webui.web_routes())
        return server.TrustedListenerProxyHeaders(
            server.HubAuthASGIMiddleware(app),
            trusted_socket_path=str(sock),
            tcp_forwarded_allow_ips=None,
        )

    async def test_forwarded_host_satisfies_callback_host_check(self, tmp_path, monkeypatch):
        """Host localhost + X-Forwarded-Host <issuer host> -> the callback's
        issuer comparison passes (reaching the credential checks, 400 here)
        instead of 403ing on Host localhost."""
        from unified_agent import oauth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
        monkeypatch.setattr(oauth, "get_provider", lambda: object())
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        status, body = await _round_trip(
            app,
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test"),
                ],
                path="/oauth/connectors/callback",
                query_string=b"error=access_denied",
            ),
        )
        assert status == 400
        assert b"declined" in body

    async def test_forwarded_host_default_port_satisfies_callback_host_check(
        self, tmp_path, monkeypatch
    ):
        """``Host: <issuer>:443`` over https is the same origin — an explicit
        default port must not 403 the callback (task #1892, round 3)."""
        from unified_agent import oauth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
        monkeypatch.setattr(oauth, "get_provider", lambda: object())
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        status, body = await _round_trip(
            app,
            _socket_scope(
                sock,
                headers=[
                    (b"host", b"localhost"),
                    (b"x-forwarded-host", b"hub.example.test:443"),
                    (b"x-forwarded-proto", b"https"),
                ],
                path="/oauth/connectors/callback",
                query_string=b"error=access_denied",
            ),
        )
        assert status == 400
        assert b"declined" in body

    async def test_localhost_host_still_fails_callback_host_check(self, tmp_path, monkeypatch):
        """Without X-Forwarded-Host the proxy's Host localhost does not match
        the issuer — the control case proving the rewrite is what fixed it."""
        from unified_agent import oauth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
        monkeypatch.setattr(oauth, "get_provider", lambda: object())
        sock = tmp_path / "hub.sock"
        app = self._stack(sock)
        status, body = await _round_trip(
            app,
            _socket_scope(
                sock,
                headers=[(b"host", b"localhost")],
                path="/oauth/connectors/callback",
                query_string=b"error=access_denied",
            ),
        )
        assert status == 403
        assert b"Connector callback host is invalid" in body


class TestProxySocketParentDir:
    """The socket's parent directory is part of the trust boundary (task #1892).

    An existing parent must be owned by the hub user and not group- or
    world-writable: a lax dir lets another local user swap or reach the socket
    before its 0600 mode lands.
    """

    @pytest.fixture(autouse=True)
    def _release_proxy_socket_locks(self):
        yield
        while server._PROXY_SOCKET_LOCKS:
            _, fd = server._PROXY_SOCKET_LOCKS.popitem()
            os.close(fd)

    def test_group_writable_parent_is_refused(self, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o770)
        parent.chmod(0o770)
        with pytest.raises(RuntimeError, match="UA_HTTP_PROXY_SOCKET"):
            server._bind_trusted_proxy_socket(parent / "hub.sock")
        assert not (parent / "hub.sock").exists()

    def test_world_writable_parent_is_refused(self, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        with pytest.raises(RuntimeError, match="UA_HTTP_PROXY_SOCKET"):
            server._bind_trusted_proxy_socket(parent / "hub.sock")
        assert not (parent / "hub.sock").exists()

    def test_parent_owned_by_another_user_is_refused(self, monkeypatch, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        real_geteuid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_geteuid + 1)
        with pytest.raises(RuntimeError, match="UA_HTTP_PROXY_SOCKET"):
            server._bind_trusted_proxy_socket(parent / "hub.sock")
        assert not (parent / "hub.sock").exists()

    def test_private_owned_parent_is_accepted(self, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock = server._bind_trusted_proxy_socket(parent / "hub.sock")
        try:
            assert stat.S_IMODE((parent / "hub.sock").stat().st_mode) == 0o600
        finally:
            sock.close()

    def test_working_fchmod_avoids_chmod_on_the_path(self, monkeypatch, tmp_path):
        """macOS/BSD: fchmod lands on the socket's vnode, so no path chmod is
        needed before listen."""
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        real_chmod = os.chmod

        def bsd_fchmod(fd, mode):
            real_chmod(sock_path, mode)

        def path_chmod(p, mode, **kwargs):
            if os.fspath(p) == str(sock_path):
                raise AssertionError("path chmod used despite a working fchmod")
            return real_chmod(p, mode, **kwargs)

        monkeypatch.setattr(os, "fchmod", bsd_fchmod)
        monkeypatch.setattr(os, "chmod", path_chmod)
        sock = server._bind_trusted_proxy_socket(sock_path)
        try:
            assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        finally:
            sock.close()

    def test_live_listener_is_never_taken_over(self, tmp_path):
        """Task #2190: a second hub with the same socket path must not unlink
        and steal the socket a live hub is serving on."""
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            live.bind(str(sock_path))
            live.listen(1)
            inode = sock_path.stat().st_ino
            with pytest.raises(RuntimeError, match="live listener"):
                server._bind_trusted_proxy_socket(sock_path)
            assert sock_path.stat().st_ino == inode
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(sock_path))
            finally:
                client.close()
        finally:
            live.close()

    def test_stale_socket_from_a_dead_hub_is_replaced(self, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(sock_path))
        dead.listen(1)
        dead.close()
        assert sock_path.is_socket()
        sock = server._bind_trusted_proxy_socket(sock_path)
        try:
            assert sock_path.is_socket()
            assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        finally:
            sock.close()

    def test_inconclusive_probe_refuses(self, monkeypatch, tmp_path):
        """A probe that neither connects nor is refused (timeout, EACCES)
        cannot prove the socket is stale, so the bind refuses."""
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(sock_path))
        dead.close()
        real_socket = socket.socket

        class TimingOut:
            def __init__(self, *args, **kwargs):
                self._sock = real_socket(*args, **kwargs)

            def settimeout(self, value):
                self._sock.settimeout(value)

            def connect(self, address):
                raise TimeoutError("timed out")

            def close(self):
                self._sock.close()

        monkeypatch.setattr(socket, "socket", TimingOut)
        with pytest.raises(RuntimeError, match="probe connect failed"):
            server._bind_trusted_proxy_socket(sock_path)
        assert sock_path.is_socket()

    def test_returned_socket_is_already_listening(self, tmp_path):
        """Review finding on #1283: a socket bound but not yet listening
        answers ECONNREFUSED like a stale one, so the bind must listen before
        it returns (uvicorn listens only after the whole ASGI lifespan)."""
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        sock = server._bind_trusted_proxy_socket(sock_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(sock_path))
        finally:
            client.close()
            sock.close()

    def test_a_locked_path_is_refused_even_when_the_probe_says_stale(self, tmp_path):
        """The advisory lock decides ownership: a hub that holds it may be
        between bind and listen, or (macOS) have a full accept queue, and
        either answers the probe with ECONNREFUSED."""
        import fcntl

        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(sock_path))  # bound, never listening: probe is refused
        holder = os.open(f"{sock_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = sock_path.stat().st_ino
            with pytest.raises(RuntimeError, match="locked by another process"):
                server._bind_trusted_proxy_socket(sock_path)
            assert sock_path.stat().st_ino == inode
        finally:
            os.close(holder)
            other.close()

    def test_an_unheld_lock_file_left_by_a_dead_hub_is_reused(self, tmp_path):
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        (parent / "hub.sock.lock").touch(mode=0o600)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(sock_path))
        dead.close()
        sock = server._bind_trusted_proxy_socket(sock_path)
        try:
            assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        finally:
            sock.close()

    def test_noop_fchmod_falls_back_to_path_chmod(self, monkeypatch, tmp_path):
        """Linux: fchmod on a unix-socket fd silently does nothing to the fs
        node, so the mode must be verified and the path chmod used — safe
        because the parent is verified 0700 hub-owned first."""
        parent = tmp_path / "run"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        sock_path = parent / "hub.sock"
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
        sock = server._bind_trusted_proxy_socket(sock_path)
        try:
            assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        finally:
            sock.close()
