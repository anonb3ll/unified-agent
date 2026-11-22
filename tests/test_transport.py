"""UA_TRANSPORT / HTTP hub configuration parsing and MCPServer wiring."""

import pytest

from unified_agent import config
from unified_agent.server import _http_settings


class TestTransportConfig:
    def test_default_is_stdio(self, monkeypatch):
        monkeypatch.delenv("UA_TRANSPORT", raising=False)
        assert config.transport() == "stdio"

    def test_http_accepted(self, monkeypatch):
        monkeypatch.setenv("UA_TRANSPORT", "http")
        assert config.transport() == "http"

    def test_invalid_transport_rejected(self, monkeypatch):
        monkeypatch.setenv("UA_TRANSPORT", "sse")
        with pytest.raises(ValueError, match="UA_TRANSPORT"):
            config.transport()

    def test_host_port_defaults(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_HOST", raising=False)
        monkeypatch.delenv("UA_HTTP_PORT", raising=False)
        assert config.http_host() == "127.0.0.1"
        assert config.http_port() == 8765

    def test_port_override_and_validation(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_PORT", "9000")
        assert config.http_port() == 9000
        monkeypatch.setenv("UA_HTTP_PORT", "not-a-port")
        with pytest.raises(ValueError, match="UA_HTTP_PORT"):
            config.http_port()

    def test_allowed_hosts_comma_separated(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*, desktop-host:*")
        assert config.http_allowed_hosts() == ["127.0.0.1:*", "desktop-host:*"]
        monkeypatch.delenv("UA_HTTP_ALLOWED_HOSTS", raising=False)
        assert config.http_allowed_hosts() == []

    def test_primary_client_requires_explicit_hub_url(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
        with pytest.raises(RuntimeError, match="UA_HTTP_HUB_URL.*Desktop_agent3"):
            config.http_hub_url("Desktop_agent3")

    def test_server_client_keeps_local_bind_default(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
        assert config.http_hub_url("server_agent3") == "http://127.0.0.1:8765/mcp"

    def test_explicit_satellite_url_is_preserved(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
        assert (
            config.http_hub_url("Desktop_agent3", "http://127.0.0.1:9876/mcp")
            == "http://127.0.0.1:9876/mcp"
        )


class TestHttpSettings:
    def test_no_allowlist_means_no_security_settings(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_ALLOWED_HOSTS", raising=False)
        settings = _http_settings()
        assert settings == {"host": "127.0.0.1", "port": 8765}

    def test_allowlist_enables_rebinding_protection(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_ALLOWED_ORIGINS", raising=False)
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "desktop-host:*")
        sec = _http_settings()["transport_security"]
        assert sec.enable_dns_rebinding_protection is True
        assert sec.allowed_hosts == ["desktop-host:*"]
        # "null" is not a default origin (sandboxed iframes). The hub's own
        # origin is, and so is every host we already accept a Host header from.
        assert "null" not in sec.allowed_origins
        assert "http://127.0.0.1:8765" in sec.allowed_origins
        assert "http://desktop-host:*" in sec.allowed_origins

    def test_origins_are_explicitly_overridable(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "desktop-host:*")
        monkeypatch.setenv("UA_HTTP_ALLOWED_ORIGINS", "https://only.this")
        sec = _http_settings()["transport_security"]
        assert sec.allowed_origins == ["https://only.this"]


class TestMcpSessionIdleTimeout:
    """Task #1298: UA_MCP_SESSION_IDLE_TIMEOUT_S keeps 24/7 MCP clients from being
    reaped by the SDK's stateful-session idle timeout (default 1800s / 30min,
    ``DEFAULT_SESSION_IDLE_TIMEOUT`` in the installed SDK's
    ``mcp/server/streamable_http_manager.py``)."""

    def test_unset_means_no_override(self, monkeypatch):
        monkeypatch.delenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", raising=False)
        assert config.mcp_session_idle_timeout_configured() is False
        # Absent, not None — the SDK's own default must apply unmodified.
        assert "session_idle_timeout" not in _http_settings()

    def test_empty_string_means_no_override(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "")
        assert config.mcp_session_idle_timeout_configured() is False
        assert "session_idle_timeout" not in _http_settings()

    def test_explicit_value_reaches_http_settings(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "3600")
        assert config.mcp_session_idle_timeout_s() == 3600
        assert _http_settings()["session_idle_timeout"] == 3600

    def test_zero_disables_timeout_via_none(self, monkeypatch):
        # StreamableHTTPSessionManager.__init__ (installed SDK,
        # mcp/server/streamable_http_manager.py:98-99) rejects 0 outright: it
        # requires session_idle_timeout to be either None or a finite value > 0.
        # None is the SDK's own "no timeout" value (same file, docstring at
        # streamable_http_manager.py:76-77: "None disables the timeout so
        # sessions live until the client deletes them or the manager shuts
        # down"). This knob accepts "0" as the conventional disable spelling
        # and translates it to the SDK's real disable value, None, rather than
        # forwarding the literal 0 and letting the SDK reject it.
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "0")
        assert config.mcp_session_idle_timeout_configured() is True
        assert config.mcp_session_idle_timeout_s() is None
        settings = _http_settings()
        assert "session_idle_timeout" in settings
        assert settings["session_idle_timeout"] is None

    def test_negative_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "-5")
        with pytest.raises(ValueError, match="UA_MCP_SESSION_IDLE_TIMEOUT_S"):
            config.mcp_session_idle_timeout_s()

    def test_non_numeric_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "forever")
        with pytest.raises(ValueError, match="UA_MCP_SESSION_IDLE_TIMEOUT_S"):
            config.mcp_session_idle_timeout_s()

    def test_wrapped_http_app_applies_configured_timeout(self, monkeypatch):
        """A direct mcp.streamable_http_app() call — not routed through
        mcp.run(**_http_settings())'s explicit kwarg forwarding — must still
        pick up the configured idle timeout, the same way wrapped_http_app()
        already threads host/transport_security through via kwargs.setdefault."""
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "120")
        from unified_agent import server

        real_original = server.original_http_app
        captured: dict = {}

        def fake_original(*args, **kwargs):
            captured.update(kwargs)
            return real_original(*args, **kwargs)

        monkeypatch.setattr(server, "original_http_app", fake_original)
        server.wrapped_http_app()
        assert captured.get("session_idle_timeout") == 120

    def test_wrapped_http_app_does_not_clobber_explicit_kwarg(self, monkeypatch):
        """kwargs.setdefault must not override a session_idle_timeout the
        caller already passed explicitly (e.g. the SDK's own
        run_streamable_http_async, which resolves it from _http_settings()
        and forwards it by keyword)."""
        monkeypatch.setenv("UA_MCP_SESSION_IDLE_TIMEOUT_S", "120")
        from unified_agent import server

        real_original = server.original_http_app
        captured: dict = {}

        def fake_original(*args, **kwargs):
            captured.update(kwargs)
            return real_original(*args, **kwargs)

        monkeypatch.setattr(server, "original_http_app", fake_original)
        server.wrapped_http_app(session_idle_timeout=999)
        assert captured.get("session_idle_timeout") == 999


class TestHttpGracefulShutdown:
    """Task #1812: the HTTP hub must not wait forever for open MCP streams."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("UA_HTTP_GRACEFUL_SHUTDOWN_S", raising=False)
        assert config.http_graceful_shutdown_s() == config.HTTP_GRACEFUL_SHUTDOWN_DEFAULT_S

    def test_override(self, monkeypatch):
        monkeypatch.setenv("UA_HTTP_GRACEFUL_SHUTDOWN_S", "3")
        assert config.http_graceful_shutdown_s() == 3
        monkeypatch.setenv("UA_HTTP_GRACEFUL_SHUTDOWN_S", "25")
        assert config.http_graceful_shutdown_s() == config.HTTP_GRACEFUL_SHUTDOWN_MAX_S

    @pytest.mark.parametrize("raw", ["0", "-5", "soon", "26", "90"])
    def test_invalid_rejected_loudly(self, monkeypatch, raw):
        monkeypatch.setenv("UA_HTTP_GRACEFUL_SHUTDOWN_S", raw)
        with pytest.raises(ValueError, match="UA_HTTP_GRACEFUL_SHUTDOWN_S"):
            config.http_graceful_shutdown_s()

    def test_serve_passes_bounded_shutdown_and_settings(self, monkeypatch):
        import uvicorn

        from unified_agent import server

        monkeypatch.setenv("UA_HTTP_HOST", "127.0.0.1")
        monkeypatch.setenv("UA_HTTP_PORT", "0")
        monkeypatch.setenv("UA_HTTP_GRACEFUL_SHUTDOWN_S", "4")
        monkeypatch.delenv("UA_HTTP_PROXY_SOCKET", raising=False)
        sentinel_app = object()
        monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: sentinel_app)
        captured: dict = {}

        async def fake_serve(self, sockets=None):
            captured["config"] = self.config
            captured["sockets"] = sockets

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        try:
            server._serve_http_hub()
        finally:
            for sock in captured.get("sockets") or []:
                sock.close()
        cfg = captured["config"]
        assert cfg.app is sentinel_app
        assert (cfg.host, cfg.port) == ("127.0.0.1", 0)
        assert cfg.timeout_graceful_shutdown == 4

    def test_sigterm_with_open_stream_exits_within_timeout(self, tmp_path):
        """A hub holding an endless stream exits 0 on SIGTERM and runs its cleanup."""
        import os
        import signal
        import socket
        import subprocess
        import sys
        import time

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = tmp_path / "serve.py"
        script.write_text(
            "import asyncio\n"
            "from unified_agent import server\n"
            "async def endless(scope, receive, send):\n"
            "    if scope['type'] == 'lifespan':\n"
            "        while True:\n"
            "            msg = await receive()\n"
            "            if msg['type'] == 'lifespan.startup':\n"
            "                await send({'type': 'lifespan.startup.complete'})\n"
            "            else:\n"
            "                await send({'type': 'lifespan.shutdown.complete'})\n"
            "                return\n"
            "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
            "    while True:\n"
            "        await send({'type': 'http.response.body', 'body': b'.', 'more_body': True})\n"
            "        await asyncio.sleep(0.2)\n"
            "server.mcp.streamable_http_app = lambda: endless\n"
            "try:\n"
            "    server._serve_http_hub()\n"
            "finally:\n"
            "    print('CLEANUP-RAN', flush=True)\n"
        )
        env = {
            **os.environ,
            "UA_HTTP_HOST": "127.0.0.1",
            "UA_HTTP_PORT": str(port),
            "UA_HTTP_GRACEFUL_SHUTDOWN_S": "1",
        }
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            stream = None
            while stream is None:
                try:
                    stream = socket.create_connection(("127.0.0.1", port), timeout=1)
                except OSError:
                    if time.monotonic() > deadline or proc.poll() is not None:
                        raise
                    time.sleep(0.02)
            stream.sendall(b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert stream.recv(64).startswith(b"HTTP/1.1 200")
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=15)
            elapsed = time.monotonic() - started
            stream.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert proc.returncode == 0, out
        assert "CLEANUP-RAN" in out
        assert elapsed < 8, f"shutdown took {elapsed:.1f}s"
