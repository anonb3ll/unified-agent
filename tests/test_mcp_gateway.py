"""The hub fronts downstream MCP servers through one gateway tool (task #1336).

No test here touches the network: DNS is a table, the downstream session is
either a stub or a JSON-mode MCP server behind an httpx2 mock transport (which
drives the real SDK), and the pinning tests swap in a dialler that records. The
stdio tests run a small real child process, because killing it is the point.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import socket
import stat
import sys
import threading
import time
import tomllib
from types import SimpleNamespace
from urllib.parse import quote

import httpcore2
import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, managed_secrets, storage
from unified_agent.cli import harness_entry
from unified_agent.cli import main as cli_main
from unified_agent.tools import approvals, mcp_gateway

# Built at import so no credential-shaped literal sits in the source.
TOKEN = "-".join(("synthetic", "gateway", "bearer", hashlib.sha256(b"1336").hexdigest()[:8]))
SECRET = "mcp:gbrain"
URL = "https://brain.example.ts.net:8443/mcp"
READ_ONLY = {
    "search",
    "query",
    "get_page",
    "get_links",
    "get_backlinks",
    "traverse_graph",
    "list_pages",
    "get_timeline",
}


def _configure(monkeypatch, servers=None, *, enabled=True, secret=True):
    monkeypatch.setenv("UA_MCP_GATEWAY", "1" if enabled else "0")
    if servers is None:
        servers = {"gbrain": {"url": URL, "secret": SECRET}}
    monkeypatch.setenv("UA_MCP_GATEWAY_SERVERS", json.dumps(servers))
    if secret:
        monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
        managed_secrets.set_secret(SECRET, TOKEN, requested_by="tester", consumer="mcp_gateway")
        # The hub serves with managed-secret mode as the operator configures it;
        # the store stays readable for the internal consumer either way.
        monkeypatch.delenv("UA_MANAGED_SECRETS")


class _FakeSession:
    def __init__(self, *, delay: float = 0.0, error: bool = False, text: str = "hit"):
        self.calls: list[tuple[str, dict | None]] = []
        self.delay = delay
        self.error = error
        self.text = text

    async def list_tools(self):
        names = [*sorted(READ_ONLY), "put_page", "delete_page"]
        tools = [
            SimpleNamespace(name=n, description=f"{n} tool", input_schema={"type": "object"})
            for n in names
        ]
        return SimpleNamespace(tools=tools, next_cursor=None)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        block = SimpleNamespace(type="text", text=self.text)
        return SimpleNamespace(content=[block], structured_content=None, is_error=self.error)


@pytest.fixture(autouse=True)
def _fresh_gateway_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.delenv("UA_OPERATOR_LABEL", raising=False)
    mcp_gateway.reset_discovery_cache()
    mcp_gateway._SLOTS.clear()
    yield
    mcp_gateway.reset_discovery_cache()
    mcp_gateway._SLOTS.clear()


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """A resolver table; an entry may be a callable for answers that change."""
    table: dict = {"brain.example.ts.net": ["100.64.0.15"]}
    calls: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        answer = table.get(host.lower().rstrip("."))
        answer = answer() if callable(answer) else answer
        if not answer:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in answer
        ]

    monkeypatch.setattr(mcp_gateway.socket, "getaddrinfo", getaddrinfo)
    return SimpleNamespace(table=table, calls=calls)


@pytest.fixture
def downstream(monkeypatch):
    """Replace the transport with a stub session; record what it was opened with."""
    state = SimpleNamespace(session=_FakeSession(), opened=[])

    @contextlib.asynccontextmanager
    async def fake_open(server, token, ip, call_state):
        state.opened.append((server.url, token))
        yield state.session

    monkeypatch.setattr(mcp_gateway, "_open_session", fake_open)
    return state


def _mcp_server(*, reply=None, session_id=None):
    """A JSON-mode streamable-HTTP MCP server. ``reply`` may override any method."""

    def handler(request):
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx2.Response(202)
        method = body["method"]
        if reply and method in reply:
            return reply[method](request, body)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "search", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "hit"}], "isError": False}
        headers = {"mcp-session-id": session_id} if session_id else {}
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        return httpx2.Response(200, json=payload, headers=headers)

    return httpx2.MockTransport(handler)


@pytest.fixture
def serve(monkeypatch):
    """Route the real session path to a mock MCP server: serve(**_mcp_server kwargs)."""

    def install(**kwargs):
        transport = _mcp_server(**kwargs)
        monkeypatch.setattr(mcp_gateway, "_make_transport", lambda server, ip: transport)

    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 5.0)
    return install


def _events() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (mcp_gateway.EVENT_CHANNEL,)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload"]) for r in rows]


def test_dormant_when_flag_off(monkeypatch, downstream):
    _configure(monkeypatch, enabled=False, secret=False)
    with pytest.raises(ToolError, match="dormant"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {"query": "x"})
    assert downstream.opened == []
    assert _events() == []


def test_call_passes_through_with_bearer_and_untrusted_wrapper(monkeypatch, downstream):
    _configure(monkeypatch)
    result = mcp_gateway.mcp_gateway("call", "gbrain", "search", {"query": "graph"})
    assert downstream.opened == [(URL, TOKEN)]
    assert downstream.session.calls == [("search", {"query": "graph"})]
    assert result["server"] == "gbrain"
    assert result["tool"] == "search"
    assert result["untrusted"] is True
    assert result["trust"]["level"] == "untrusted"
    assert result["content"] == [{"type": "text", "text": "hit"}]


def test_tool_outside_allow_list_is_refused_before_any_network_call(monkeypatch, downstream):
    _configure(
        monkeypatch,
        {"gbrain": {"url": URL, "secret": SECRET, "allow": ["search", "get_page"]}},
    )
    for tool in ("put_page", "query"):  # write tool; read tool narrowed out by config
        with pytest.raises(ToolError, match="not allowed"):
            mcp_gateway.mcp_gateway("call", "gbrain", tool, {})
    assert downstream.opened == []
    assert [e["outcome"] for e in _events()] == ["refused_tool", "refused_tool"]


def test_list_tools_returns_only_the_allowed_intersection(monkeypatch, downstream):
    _configure(monkeypatch)
    result = mcp_gateway.mcp_gateway("list_tools", "gbrain")
    assert {t["tool"] for t in result["content"]} == READ_ONLY
    assert {t["name"] for t in result["content"]} == {f"gbrain.{name}" for name in READ_ONLY}
    assert {t["access"] for t in result["content"]} == {"read"}
    assert result["untrusted"] is True
    _configure(
        monkeypatch,
        {"gbrain": {"url": URL, "secret": SECRET, "allow": ["search"]}},
        secret=False,
    )
    narrowed = mcp_gateway.mcp_gateway("list_tools", "gbrain")
    assert [t["tool"] for t in narrowed["content"]] == ["search"]


def test_unknown_server_is_refused(monkeypatch, downstream):
    _configure(monkeypatch)
    with pytest.raises(ToolError, match="unknown"):
        mcp_gateway.mcp_gateway("call", "elsewhere", "search", {})
    assert downstream.opened == []
    assert _events()[-1]["outcome"] == "refused_server"


@pytest.mark.parametrize(
    "servers",
    [
        {"gbrain": {"url": "http://brain.example.com/mcp", "secret": SECRET}},
        {"gbrain": {"url": "https://" + "u:p" + "\x40brain.example.ts.net/mcp", "secret": SECRET}},
        {"gbrain": {"url": URL, "secret": SECRET, "allow": ["delete_page"]}},
        {"gbrain": {"url": URL}},
        {"other": {"url": URL, "secret": SECRET}},
        {"gbrain": {"url": URL, "secret": SECRET, "headers": {}}},
    ],
    ids=["plain-http-public", "userinfo", "widened-allow", "no-secret", "no-cap", "extra-key"],
)
def test_invalid_config_is_refused(monkeypatch, downstream, servers):
    _configure(monkeypatch, servers, secret=False)
    with pytest.raises(ToolError, match="misconfigured"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert downstream.opened == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000/mcp",
        "http://[::1]:9000/mcp",
        "http://100.64.0.15:9000/mcp",
        "http://brain.tail0000.ts.net/mcp",
        "https://brain.example.com/mcp",
    ],
)
def test_https_loopback_and_tailnet_urls_are_accepted(url):
    servers = mcp_gateway.parse_servers(json.dumps({"gbrain": {"url": url, "secret": SECRET}}))
    assert servers["gbrain"].url == url


def test_redirect_is_refused_by_the_http_client():
    async def scenario():
        redirect = httpx2.MockTransport(
            lambda request: httpx2.Response(307, headers={"Location": "https://evil.example/"})
        )
        state = mcp_gateway._CallState()
        async with mcp_gateway._build_http_client(TOKEN, state, redirect) as client:
            assert client.follow_redirects is False
            assert client.trust_env is False
            with pytest.raises(mcp_gateway.GatewayRedirect):
                await client.post(URL, json={})
        return state.violation

    assert asyncio.run(scenario()) == "redirect_refused"


def test_redirect_during_a_call_is_reported_as_refused(monkeypatch):
    """The real session path over a transport that only ever redirects."""
    _configure(monkeypatch)
    seen: list[str] = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx2.Response(307, headers={"Location": "https://evil.example/mcp"})

    transport = httpx2.MockTransport(redirect)
    monkeypatch.setattr(mcp_gateway, "_make_transport", lambda server, ip: transport)
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 5.0)
    with pytest.raises(ToolError, match="redirect"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "redirect_refused"
    assert seen and all(url == URL for url in seen)  # never the Location target


def test_timeout_is_handled(monkeypatch, downstream):
    _configure(monkeypatch)
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 0.05)
    downstream.session.delay = 5
    with pytest.raises(ToolError, match="timed out"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "timeout"


def test_oversized_response_is_capped(monkeypatch, downstream):
    _configure(monkeypatch)
    monkeypatch.setattr(mcp_gateway, "MAX_RESPONSE_BYTES", 1000)
    downstream.session.text = "x" * 5000
    result = mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert result["truncated"] is True
    assert len(json.dumps(result["content"])) <= 1200
    assert _events()[-1]["outcome"] == "ok_truncated"


def test_downstream_error_and_missing_secret(monkeypatch, downstream):
    _configure(monkeypatch)
    downstream.session.error = True
    with pytest.raises(ToolError, match="downstream"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "downstream_error"
    _configure(monkeypatch, {"gbrain": {"url": URL, "secret": "mcp:absent"}}, secret=False)
    with pytest.raises(ToolError, match="credential"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "secret_unavailable"


def test_event_carries_args_hash_not_raw_args(monkeypatch, downstream):
    _configure(monkeypatch)
    args = {"query": "distinctive-needle-31337"}
    mcp_gateway.mcp_gateway("call", "gbrain", "search", args)
    (event,) = _events()
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert event["args_sha256"] == digest
    assert event["server"] == "gbrain"
    assert event["tool"] == "search"
    assert event["outcome"] == "ok"
    assert isinstance(event["latency_ms"], int)
    assert "distinctive-needle-31337" not in json.dumps(event)


def test_secret_never_leaks_into_results_events_or_logs(monkeypatch, downstream, caplog):
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    outputs = [
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {"query": "q"}),
        mcp_gateway.mcp_gateway("list_tools", "gbrain"),
    ]
    downstream.session.error = True
    with pytest.raises(ToolError) as failure:
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    swept = json.dumps(outputs) + json.dumps(_events()) + str(failure.value) + caplog.text
    assert downstream.opened and all(token == TOKEN for _, token in downstream.opened)
    assert TOKEN not in swept


# --- P1: DNS scope and pinning -------------------------------------------------

PUBLIC_URL = "https://brain.example.com/mcp"


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "10.0.0.1",
        "127.0.0.1",
        "100.64.0.15",
        "0.0.0.0",
        "224.0.0.1",
        "::ffff:169.254.169.254",
        "::a9fe:a9fe",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        "fe80::1",
        "fd00:ec2::254",
        "::",
        "ff02::1",
    ],
)
def test_public_scope_rejects_non_public_addresses(address):
    import ipaddress

    assert not mcp_gateway.address_allowed(ipaddress.ip_address(address), "public")


def test_scope_sets_accept_only_their_ranges():
    import ipaddress

    def ok(address, scope):
        return mcp_gateway.address_allowed(ipaddress.ip_address(address), scope)

    assert ok("93.184.216.34", "public") and ok("2606:4700::1", "public")
    assert ok("127.0.0.2", "loopback") and ok("::1", "loopback")
    assert ok("::ffff:127.0.0.1", "loopback")
    assert not ok("100.64.0.15", "loopback")
    assert ok("100.64.0.15", "tailnet") and ok("fd7a:115c:a1e0::1", "tailnet")
    assert not ok("169.254.169.254", "tailnet") and not ok("93.184.216.34", "tailnet")


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/mcp",
        "http://0177.0.0.1/mcp",
        "http://0x7f.1/mcp",
        "https://127.1/mcp",
        "https://[::ffff:169.254.169.254]/mcp",
        "https://169.254.169.254/mcp",
        "http://[fe80::1]/mcp",
    ],
)
def test_numeric_and_non_public_literal_hosts_are_refused_in_config(url):
    with pytest.raises(mcp_gateway.GatewayConfigError):
        mcp_gateway.parse_servers(json.dumps({"gbrain": {"url": url, "secret": SECRET}}))


def test_ipv4_mapped_and_trailing_dot_hosts_classify_by_their_address():
    assert mcp_gateway.classify_host("::ffff:127.0.0.1") == "loopback"
    assert mcp_gateway.classify_host("localhost.") == "loopback"
    assert mcp_gateway.classify_host("brain.example.ts.net.") == "tailnet"
    assert mcp_gateway.classify_host("brain.ts.net.example.com") == "public"


@pytest.mark.parametrize(
    ("url", "host", "answer"),
    [
        (PUBLIC_URL, "brain.example.com", ["169.254.169.254"]),
        (PUBLIC_URL, "brain.example.com", ["93.184.216.34", "10.0.0.5"]),
        (PUBLIC_URL, "brain.example.com", ["::ffff:169.254.169.254"]),
        (URL, "brain.example.ts.net", ["93.184.216.34"]),
        ("http://localhost.:9000/mcp", "localhost", ["169.254.169.254"]),
    ],
    ids=["metadata", "any-bad-address", "ipv4-mapped", "tailnet-name-public", "localhost-dot"],
)
def test_resolved_address_outside_scope_is_refused_before_connecting(
    monkeypatch, dns, downstream, url, host, answer
):
    _configure(monkeypatch, {"gbrain": {"url": url, "secret": SECRET}})
    dns.table[host] = answer
    with pytest.raises(ToolError, match="outside its"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert downstream.opened == []
    assert _events()[-1]["outcome"] == "address_refused"


class _RecordingBackend:
    """Stands in for the real dialler: records the TCP target and the TLS name."""

    def __init__(self):
        self.dialled: list[tuple[str, int]] = []
        self.sni: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialled.append((host, port))
        backend = self

        class Stream:
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                backend.sni.append(server_hostname)
                raise httpcore2.ConnectError("test stops at the TLS handshake")

            async def aclose(self):
                return None

            def get_extra_info(self, info):
                return None

        return Stream()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def test_rebinding_cannot_move_the_pinned_connection(monkeypatch, dns):
    """First answer public, every later answer private: the dial uses the first."""
    _configure(monkeypatch, {"gbrain": {"url": PUBLIC_URL, "secret": SECRET}})
    answers = iter([["93.184.216.34"]])
    dns.table["brain.example.com"] = lambda: next(answers, ["169.254.169.254"])
    backend = _RecordingBackend()
    monkeypatch.setattr(mcp_gateway, "_inner_backend", lambda: backend)
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 5.0)
    with pytest.raises(ToolError):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert dns.calls == ["brain.example.com"]  # resolved once, never again
    assert backend.dialled and set(backend.dialled) == {("93.184.216.34", 443)}
    assert backend.sni and set(backend.sni) == {"brain.example.com"}  # SNI keeps the name
    assert _events()[-1]["outcome"] == "transport_error"


def test_pinned_backend_refuses_any_other_host_or_port():
    backend = mcp_gateway._PinnedBackend("brain.example.com", 443, "93.184.216.34", object())
    for host, port in (("evil.example", 443), ("brain.example.com", 80)):
        with pytest.raises(httpcore2.ConnectError):
            asyncio.run(backend.connect_tcp(host, port))


# --- P1: response size bound before the SDK parses -------------------------------


def _big_json(request, body, status=200):
    payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"pad": "x" * (2 * 1024 * 1024)}}
    return httpx2.Response(status, json=payload)


@pytest.mark.parametrize("method", ["initialize", "tools/call", "error-status"])
def test_oversized_body_is_refused_before_parsing(monkeypatch, serve, method):
    _configure(monkeypatch)
    if method == "error-status":
        serve(reply={"tools/call": lambda request, body: _big_json(request, body, status=500)})
    else:
        serve(reply={method: _big_json})
    with pytest.raises(ToolError, match="too large"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "response_too_large"


def test_oversized_list_tools_and_streamed_body_stop_at_the_cap(monkeypatch, serve):
    _configure(monkeypatch)
    sent: list[int] = []

    async def chunks():
        for _ in range(64):  # 64 x 64 KiB = 4 MiB, no Content-Length
            sent.append(64 * 1024)
            yield b"x" * (64 * 1024)

    def streamed(request, body):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    serve(reply={"tools/list": streamed})
    started = time.monotonic()
    with pytest.raises(ToolError, match="too large"):
        mcp_gateway.mcp_gateway("list_tools", "gbrain")
    assert time.monotonic() - started < 4
    assert sum(sent) <= mcp_gateway.MAX_WIRE_BYTES + 64 * 1024
    assert _events()[-1]["outcome"] == "response_too_large"


def test_compressed_response_is_refused(monkeypatch, serve):
    _configure(monkeypatch)
    serve(
        reply={
            "initialize": lambda request, body: httpx2.Response(
                200, headers={"content-encoding": "gzip"}, content=b"\x1f\x8b"
            )
        }
    )
    with pytest.raises(ToolError, match="compressed"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert _events()[-1]["outcome"] == "encoding_refused"


# --- P1: a reflecting server cannot hand the bearer back -------------------------


def test_reflected_bearer_is_redacted_from_content_and_sdk_logs(monkeypatch, serve, caplog):
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)

    def echo(request, body):
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        text = " | ".join(
            (
                auth,
                base64.b64encode(token.encode()).decode(),
                base64.b64encode(auth.encode()).decode(),
                quote(token, safe=""),
            )
        )
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def echo_tools(request, body):
        auth = request.headers.get("authorization", "")
        tools = [
            {
                "name": "search",
                "description": auth,
                "inputSchema": {"type": "object", "default": auth},
            }
        ]
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": tools}}
        )

    # The server also hands the bearer back as its session id, which the SDK logs.
    serve(reply={"tools/call": echo, "tools/list": echo_tools}, session_id=TOKEN)
    called = mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    listed = mcp_gateway.mcp_gateway("list_tools", "gbrain")
    assert "[redacted]" in called["content"][0]["text"]
    assert listed["content"][0]["description"] == "Bearer [redacted]"
    assert "Received session ID: [redacted]" in caplog.text  # the SDK path really ran
    swept = json.dumps([called, listed, _events()]) + caplog.text
    for form in mcp_gateway.secret_forms(TOKEN):
        assert form not in swept, form


# --- P2: one event for every post-flag outcome -----------------------------------


@pytest.mark.parametrize(
    ("args", "outcome"),
    [
        (("delete", "gbrain", "search", {}), "refused_action"),
        (("call", "gbrain", None, {}), "refused_action"),
        (("call", "gbrain", "search", ["not", "a", "dict"]), "refused_arguments"),
        (("call", "gbrain", "search", {"q": "x" * 70000}), "refused_arguments"),
    ],
)
def test_every_validation_refusal_records_exactly_one_event(monkeypatch, downstream, args, outcome):
    _configure(monkeypatch, secret=False)
    with pytest.raises(ToolError):
        mcp_gateway.mcp_gateway(*args)
    assert [e["outcome"] for e in _events()] == [outcome]
    assert downstream.opened == []


def test_unexpected_failure_still_records_one_event(monkeypatch, downstream):
    _configure(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mcp_gateway, "_wrap", boom)
    with pytest.raises(ToolError, match="internally"):
        mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    assert [e["outcome"] for e in _events()] == ["internal_error"]


def test_doctor_row_states(monkeypatch):
    from unified_agent.tools import doctor

    monkeypatch.delenv("UA_MCP_GATEWAY", raising=False)
    assert mcp_gateway.doctor_check()["status"] == "DORMANT"
    _configure(monkeypatch, {"gbrain": {"url": "ftp://x", "secret": SECRET}}, secret=False)
    assert mcp_gateway.doctor_check()["status"] == "MISCONFIGURED"
    _configure(monkeypatch, secret=False)
    row = mcp_gateway.doctor_check()
    assert row["status"] == "MISCONFIGURED" and "secret" in row["detail"]
    _configure(monkeypatch)
    row = mcp_gateway.doctor_check()
    assert row["capability"] == "mcp_gateway"
    assert row["status"] == "LIVE"
    assert TOKEN not in json.dumps(row)
    names = {c["capability"] for c in doctor._run_system_doctor()["checks"]}
    assert "mcp_gateway" in names


def test_registration_profiles_and_classification():
    from unified_agent import profiles, server, tool_classification, toolsets
    from unified_agent.tools import profiles as profile_tools

    assert "mcp_gateway" in server.REGISTERED_TOOLS
    assert toolsets.ASSIGNMENTS["mcp_gateway"] == "connectors"
    assert (
        tool_classification.classify_tool("mcp_gateway")
        == tool_classification.ToolExposureClass.WITHHELD
    )
    assert not profiles.tool_is_allowed_for(profiles.DEVELOPER, "mcp_gateway")
    assert profiles.tool_is_allowed_for(profiles.STEWARD, "mcp_gateway")
    assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, "mcp_gateway")
    for profile in (profiles.RESEARCH, profiles.OPERATOR, profiles.PUBLIC):
        assert not profiles.tool_is_allowed_for(profile, "mcp_gateway"), profile
    assert "mcp_gateway" in profile_tools.MANAGED_SECRET_CONSUMERS


# --- slice 2: the registry ---------------------------------------------------------

STDIO_NAME = os.path.basename(sys.executable)


@contextlib.contextmanager
def _as(identity):
    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def stdio_catalog(monkeypatch):
    """Let codebase-memory start this test's Python, as the catalog would its binary."""
    entry = dataclasses.replace(
        mcp_gateway.SERVER_CATALOG["codebase-memory"], commands=frozenset({STDIO_NAME})
    )
    monkeypatch.setitem(mcp_gateway.SERVER_CATALOG, "codebase-memory", entry)


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ({"command": "codebase-memory-mcp"}, "absolute"),
        ({"command": "/opt/../bin/codebase-memory-mcp"}, "absolute"),
        ({"command": "/bin/sh"}, "not on this server's list"),
        ({"command": "/x/codebase-memory-mcp", "args": "--flag"}, "args"),
        ({"command": "/x/codebase-memory-mcp", "cwd": "relative"}, "cwd"),
        ({"command": "/x/codebase-memory-mcp", "env": {"API_TOKEN": "v"}}, "API_TOKEN"),
        ({"command": "/x/codebase-memory-mcp", "env": {"LD_PRELOAD": "/x.so"}}, "LD_PRELOAD"),
        ({"command": "/x/codebase-memory-mcp", "env": {"PATH": "/x"}}, "PATH"),
        ({"command": "/x/codebase-memory-mcp", "secret": SECRET}, "go together"),
        ({"command": "/x/codebase-memory-mcp", "secret_env": "TOKEN"}, "go together"),
        ({"command": "/x/codebase-memory-mcp", "url": URL}, "unknown keys"),
    ],
)
def test_stdio_config_refusals_name_the_problem(spec, needle):
    raw = json.dumps({"codebase-memory": {"transport": "stdio", **spec}})
    with pytest.raises(mcp_gateway.GatewayConfigError, match=needle):
        mcp_gateway.parse_servers(raw)


def test_registry_gives_each_server_its_transport_secret_and_allow_list():
    servers = mcp_gateway.parse_servers(
        json.dumps(
            {
                "gbrain": {"url": URL, "secret": SECRET, "allow": ["search", "put_page"]},
                "codebase-memory": {
                    "transport": "stdio",
                    "command": "/opt/bin/codebase-memory-mcp",
                    "args": ["--tool-profile=analysis"],
                },
            }
        )
    )
    gbrain, code = (servers[n] for n in ("gbrain", "codebase-memory"))
    assert (gbrain.transport, gbrain.scope) == ("http", "tailnet")
    assert gbrain.access("search") == "read" and gbrain.access("put_page") == "write"
    assert gbrain.access("query") is None  # read, but narrowed out
    assert (code.transport, code.secret, code.writes) == ("stdio", None, frozenset())
    assert code.reads == mcp_gateway.SERVER_CATALOG["codebase-memory"].reads


def test_a_stdio_entry_keeps_its_working_directory_and_secret():
    servers = mcp_gateway.parse_servers(
        json.dumps(
            {
                "codebase-memory": {
                    "transport": "stdio",
                    "command": "/opt/bin/codebase-memory-mcp",
                    "cwd": "/opt/graph",
                    "secret": "mcp:graph",
                    "secret_env": "GRAPH_AUTH",
                }
            }
        )
    )
    code = servers["codebase-memory"]
    assert (code.secret, code.secret_env, code.cwd) == ("mcp:graph", "GRAPH_AUTH", "/opt/graph")


def test_catalog_classifies_each_tool_once_and_leaves_destructive_tools_out():
    for name, entry in mcp_gateway.SERVER_CATALOG.items():
        assert not entry.reads & entry.writes, name
    catalog = mcp_gateway.SERVER_CATALOG
    assert not {"delete_page", "forget"} & (catalog["gbrain"].reads | catalog["gbrain"].writes)
    code = catalog["codebase-memory"]
    assert "delete_project" not in code.reads | code.writes


def test_a_bad_entry_refuses_only_itself(monkeypatch, downstream):
    _configure(
        monkeypatch,
        {
            "gbrain": {"url": URL, "secret": SECRET},
            "codebase-memory": {"transport": "stdio", "command": "/bin/sh"},
            "unknown": {"url": URL, "secret": SECRET},
        },
    )
    assert mcp_gateway.mcp_gateway("call", "gbrain", "search", {})["server"] == "gbrain"
    with pytest.raises(ToolError, match="misconfigured.*not on this server"):
        mcp_gateway.mcp_gateway("call", "codebase-memory", "list_projects", {})
    with pytest.raises(ToolError, match="misconfigured.*no gateway catalog entry"):
        mcp_gateway.mcp_gateway("call", "unknown", "search", {})
    listed = mcp_gateway.mcp_gateway("list_tools")
    assert listed["servers"]["gbrain"]["state"] == "ok"
    assert listed["servers"]["codebase-memory"]["state"] == "misconfigured"
    assert _events()[-1]["outcome"] == "ok_partial"


# --- slice 2: discovery ------------------------------------------------------------


def _two_servers(monkeypatch, *, stdio_script=None):
    servers = {"gbrain": {"url": URL, "secret": SECRET}}
    if stdio_script is not None:
        servers["codebase-memory"] = {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(stdio_script)],
        }
    _configure(monkeypatch, servers)


def test_discovery_rows_are_namespaced_and_individually_marked_untrusted(monkeypatch, downstream):
    _configure(monkeypatch)
    result = mcp_gateway.mcp_gateway("list_tools")
    row = next(r for r in result["content"] if r["name"] == "gbrain.search")
    assert (row["server"], row["tool"], row["access"]) == ("gbrain", "search", "read")
    assert row["input_schema"] == {"type": "object"}
    assert row["trust"]["level"] == "untrusted" and row["trust"]["author"] == "gbrain"
    assert result["trust"]["source"] == "mcp_gateway"
    assert result["servers"]["gbrain"] == {"state": "ok", "tools": 8, "cached": False}


def test_search_filters_by_every_term_ranks_names_first_and_honours_limit(monkeypatch, downstream):
    _configure(monkeypatch)
    hits = mcp_gateway.mcp_gateway("search_tools", query="get_page tool")["content"]
    assert [row["name"] for row in hits] == ["gbrain.get_page"]
    assert all("trust" in row for row in hits)
    tool_hits = mcp_gateway.mcp_gateway("search_tools", query="TOOL", limit=2)["content"]
    assert len(tool_hits) == 2
    assert mcp_gateway.mcp_gateway("search_tools", query="no-such-term")["content"] == []
    ranked = mcp_gateway._search(
        [
            {"name": "a.other", "description": "mentions search"},
            {"name": "b.search", "description": ""},
        ],
        "search",
        5,
    )
    assert [row["name"] for row in ranked] == ["b.search", "a.other"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": None},
        {"query": "   "},
        {"query": "x" * 201},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 101},
    ],
)
def test_search_validates_query_and_limit(monkeypatch, downstream, kwargs):
    _configure(monkeypatch)
    with pytest.raises(ToolError, match="query|limit"):
        mcp_gateway.mcp_gateway("search_tools", **kwargs)
    assert downstream.opened == []
    assert _events()[-1]["outcome"] == "refused_arguments"


def test_discovery_is_cached_expires_and_never_survives_a_config_change(monkeypatch, downstream):
    _configure(monkeypatch)
    assert mcp_gateway.mcp_gateway("list_tools")["servers"]["gbrain"]["cached"] is False
    second = mcp_gateway.mcp_gateway("search_tools", query="search")
    assert second["servers"]["gbrain"]["cached"] is True
    assert len(downstream.opened) == 1
    mcp_gateway._DISCOVERY["gbrain"].expires = 0.0
    assert mcp_gateway.mcp_gateway("list_tools")["servers"]["gbrain"]["cached"] is False
    assert len(downstream.opened) == 2
    narrowed = {"gbrain": {"url": URL, "secret": SECRET, "allow": ["search"]}}
    _configure(monkeypatch, narrowed, secret=False)
    assert [r["tool"] for r in mcp_gateway.mcp_gateway("list_tools")["content"]] == ["search"]
    assert len(downstream.opened) == 3


def test_discovery_ttl_is_bounded_and_zero_disables_the_cache(monkeypatch, downstream):
    for raw, expected in (("99999", 3600.0), ("-5", 0.0), ("junk", 300.0), ("nan", 300.0)):
        monkeypatch.setenv("UA_MCP_GATEWAY_DISCOVERY_TTL_S", raw)
        assert config.mcp_gateway_discovery_ttl_s() == expected
    monkeypatch.setenv("UA_MCP_GATEWAY_DISCOVERY_TTL_S", "0")
    _configure(monkeypatch)
    mcp_gateway.mcp_gateway("list_tools")
    mcp_gateway.mcp_gateway("list_tools")
    assert len(downstream.opened) == 2


def test_a_failed_discovery_is_retried_only_after_the_short_window(monkeypatch, serve):
    _configure(monkeypatch)
    hits: list[int] = []

    def broken(request, body):
        hits.append(1)
        return httpx2.Response(500, json={})

    serve(reply={"initialize": broken})
    first = mcp_gateway.mcp_gateway("list_tools")
    assert first["servers"]["gbrain"]["state"] == "transport_error"
    assert first["content"] == []
    second = mcp_gateway.mcp_gateway("list_tools")
    assert second["servers"]["gbrain"] == {**first["servers"]["gbrain"], "cached": True}
    assert len(hits) == 1
    entry = mcp_gateway._DISCOVERY["gbrain"]
    assert entry.expires - time.monotonic() <= mcp_gateway._FAILURE_TTL_S
    with pytest.raises(ToolError, match="failed"):  # naming the server reports the failure
        mcp_gateway.mcp_gateway("list_tools", "gbrain")
    assert _events()[-1]["outcome"] == "transport_error"


# --- slice 2: policy, approval and evidence ----------------------------------------


def _policy(tmp_path, tool, action="deny"):
    (tmp_path / "policy.yaml").write_text(
        f"version: 1\nrules:\n  - name: gateway_rule\n    type: tool\n"
        f'    tool: "{tool}"\n    action: {action}\n'
    )


@pytest.mark.parametrize(
    "spelling",
    [("gbrain", "search"), (None, "gbrain.search"), ("gbrain", "gbrain.search")],
    ids=["server-and-tool", "namespaced", "both"],
)
def test_policy_sees_the_canonical_name_however_the_caller_spells_it(
    monkeypatch, tmp_path, downstream, spelling
):
    _configure(monkeypatch)
    _policy(tmp_path, "mcp_gateway.gbrain.*")
    with pytest.raises(ToolError, match="mcp_gateway.gbrain.search"):
        mcp_gateway.mcp_gateway("call", *spelling, {"query": "x"})
    assert downstream.opened == []
    event = _events()[-1]
    assert (event["outcome"], event["server"], event["tool"]) == (
        "policy_denied",
        "gbrain",
        "search",
    )


def test_policy_rule_on_one_tool_leaves_the_others_alone(monkeypatch, tmp_path, downstream):
    _configure(monkeypatch)
    _policy(tmp_path, "mcp_gateway.gbrain.query")
    assert mcp_gateway.mcp_gateway("call", tool="gbrain.search", arguments={})["tool"] == "search"


WRITES = {"gbrain": {"url": URL, "secret": SECRET, "allow": ["search", "put_page"]}}


def _park(arguments):
    with _as("Desktop_Worker"), pytest.raises(approvals.ApprovalPendingError) as parked:
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", arguments)
    return parked.value.approval_id


def _resolve(approval_id, decision="approve"):
    with _as(config.DEFAULT_OPERATOR_LABEL):
        approvals.approval_resolve(int(approval_id), decision)


def test_a_write_parks_then_runs_once_on_approval_of_the_exact_arguments(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    args = {"slug": "x", "content": "distinctive-needle-31337"}
    approval_id = _park(args)
    assert downstream.opened == []
    assert _events()[-1]["outcome"] == "approval_pending"
    assert _events()[-1]["approval_id"] == approval_id
    assert _park(args) == approval_id  # a retry does not queue a second request

    _resolve(approval_id)
    other = {"slug": "x", "content": "something else"}
    assert _park(other) != approval_id  # the pass does not cover other arguments
    assert downstream.opened == []
    with _as("Desktop_Worker"):
        result = mcp_gateway.mcp_gateway("call", "gbrain", "put_page", args)
    assert result["content"] == [{"type": "text", "text": "hit"}]
    assert downstream.session.calls == [("put_page", args)]

    event = _events()[-1]
    assert event["outcome"] == "ok"
    assert (event["access"], event["caller"]) == ("write", "Desktop_Worker")
    assert event["approval_id"] == approval_id
    assert "distinctive-needle-31337" not in json.dumps(_events())

    assert _park(args) != approval_id  # one-shot: the pass is spent


def test_one_agents_approved_write_cannot_be_spent_by_another(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    args = {"slug": "x", "content": "y"}
    approval_id = _park(args)
    _resolve(approval_id)
    with _as("Desktop_Other"), pytest.raises(approvals.ApprovalPendingError) as parked:
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", args)
    assert parked.value.approval_id != approval_id
    assert downstream.opened == []
    with _as("Desktop_Worker"):
        assert mcp_gateway.mcp_gateway("call", "gbrain", "put_page", args)["tool"] == "put_page"


def test_a_denied_write_stays_refused_and_never_reaches_the_network(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    approval_id = _park({"slug": "x"})
    _resolve(approval_id, "deny")
    with _as("Desktop_Worker"), pytest.raises(ToolError, match="DENIED"):
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", {"slug": "x"})
    assert downstream.opened == []
    assert _events()[-1]["outcome"] == "approval_denied"


def test_a_write_needs_a_verified_unrevoked_caller(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    with pytest.raises(ToolError, match="verified caller"):
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", {})
    monkeypatch.setenv("UA_REVOKED_AGENTS", "Desktop_Worker")
    monkeypatch.setattr(config, "revoked_agents", lambda: frozenset({"Desktop_Worker"}))
    with _as("Desktop_Worker"), pytest.raises(ToolError, match="revoked"):
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", {})
    assert [e["outcome"] for e in _events()] == ["refused_identity", "refused_identity"]
    assert downstream.opened == []


def test_a_write_is_refused_unless_config_names_it(monkeypatch, downstream):
    _configure(monkeypatch)  # no allow: reads only
    with _as("Desktop_Worker"), pytest.raises(ToolError, match="not allowed"):
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", {})
    assert _events()[-1]["outcome"] == "refused_tool"


def test_a_tool_the_catalog_does_not_classify_fails_closed(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    for tool in ("delete_page", "brand_new_tool"):
        with _as("Desktop_Worker"), pytest.raises(ToolError, match="not allowed"):
            mcp_gateway.mcp_gateway("call", "gbrain", tool, {})
    assert downstream.opened == []


def test_discovery_reports_access_and_hides_tools_outside_the_allow_list(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    rows = mcp_gateway.mcp_gateway("list_tools", "gbrain")["content"]
    assert {r["name"]: r["access"] for r in rows} == {
        "gbrain.search": "read",
        "gbrain.put_page": "write",
    }


def test_registered_wrapper_parks_a_write_then_runs_it_once_approved(monkeypatch, downstream):
    from unified_agent import profiles, server

    _configure(monkeypatch, WRITES)
    monkeypatch.setattr(profiles, "current", lambda: profiles.STEWARD)
    call = {"action": "call", "server": "gbrain", "tool": "put_page", "arguments": {"slug": "x"}}
    tool = server.REGISTERED_TOOLS["mcp_gateway"]
    with _as("Desktop_Worker"), pytest.raises(approvals.ApprovalPendingError) as parked:
        tool(**call)
    assert downstream.opened == []
    _resolve(parked.value.approval_id)
    with _as("Desktop_Worker"):
        assert tool(**call)["tool"] == "put_page"
    assert downstream.session.calls == [("put_page", {"slug": "x"})]
    with _as("Desktop_Worker"), pytest.raises(approvals.ApprovalPendingError):
        tool(**call)


def test_evidence_names_action_and_caller_on_discovery_and_reads(monkeypatch, downstream):
    _configure(monkeypatch)
    with _as("Desktop_Worker"):
        mcp_gateway.mcp_gateway("search_tools", query="search")
        mcp_gateway.mcp_gateway("call", tool="gbrain.search", arguments={"query": "x"})
    discovery, read = _events()
    assert (discovery["action"], discovery["server"], discovery["tool"]) == (
        "search_tools",
        None,
        None,
    )
    assert (read["action"], read["server"], read["tool"], read["access"], read["caller"]) == (
        "call",
        "gbrain",
        "search",
        "read",
        "Desktop_Worker",
    )


def test_argument_digest_cannot_confirm_a_guessed_secret(monkeypatch, downstream):
    _configure(monkeypatch)
    mcp_gateway.mcp_gateway("call", "gbrain", "search", {"password": "guess-a"})
    mcp_gateway.mcp_gateway("call", "gbrain", "search", {"password": "guess-b"})
    first, second = _events()
    assert first["args_sha256"] == second["args_sha256"]


# --- slice 2: a stdio downstream, for real -----------------------------------------

SERVER_SCRIPT = """
import json, os, signal, sys, time

pid_file = {pid_file!r}
mode = {mode!r}
if pid_file:
    open(pid_file, "w").write(str(os.getpid()))
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "exit":
    sys.exit(3)
if mode == "flood":
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
print("noise for the hub log", file=sys.stderr, flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        if mode in ("hang", "ignore-term"):
            time.sleep(60)
        result = {{
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {{"tools": {{}}}},
            "serverInfo": {{"name": "fake", "version": "1"}},
        }}
    elif method == "tools/list":
        tools = [
            {{"name": "search_graph", "inputSchema": {{"type": "object"}}}},
            {{"name": "delete_project", "inputSchema": {{"type": "object"}}}},
        ]
        result = {{"tools": tools}}
    else:
        report = {{
            "argv": sys.argv,
            "env": dict(os.environ),
            "arguments": message["params"].get("arguments"),
        }}
        result = {{"content": [{{"type": "text", "text": json.dumps(report)}}], "isError": False}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}), flush=True)
"""


@pytest.fixture
def child(monkeypatch, tmp_path, stdio_catalog):
    """Configure codebase-memory as a real child in the given mode."""

    def install(mode="normal", **extra):
        script = tmp_path / "server.py"
        pid_file = str(tmp_path / "pid") if mode in ("hang", "ignore-term") else ""
        script.write_text(SERVER_SCRIPT.format(pid_file=pid_file, mode=mode))
        spec = {"transport": "stdio", "command": sys.executable, "args": [str(script)], **extra}
        _configure(monkeypatch, {"codebase-memory": spec})
        return tmp_path / "pid"

    return install


def _pid_gone(pid_file):
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_stdio_lists_and_calls_with_a_minimal_child_environment(monkeypatch, child, capfd):
    monkeypatch.setenv("UA_AGENT_TOKENS", "hub-authority-value")
    monkeypatch.setenv("SOME_HUB_SETTING", "hub-only")
    child(secret=SECRET, secret_env="CHILD_AUTH", env={"CHILD_MODE": "on"})
    listed = mcp_gateway.mcp_gateway("list_tools", "codebase-memory")
    assert [r["name"] for r in listed["content"]] == ["codebase-memory.search_graph"]
    result = mcp_gateway.mcp_gateway(
        "call", tool="codebase-memory.search_graph", arguments={"q": "x"}
    )
    # The reflected bearer is scrubbed from what the caller gets.
    report = json.loads(result["content"][0]["text"])
    assert report["arguments"] == {"q": "x"}
    assert report["env"]["CHILD_AUTH"] == "[redacted]"
    assert report["env"]["CHILD_MODE"] == "on"
    assert "UA_AGENT_TOKENS" not in report["env"] and "SOME_HUB_SETTING" not in report["env"]
    assert TOKEN not in json.dumps(report["argv"])
    assert "noise for the hub log" not in capfd.readouterr().err
    assert _events()[-1]["outcome"] == "ok"


@pytest.mark.parametrize("mode", ["hang", "ignore-term"])
def test_a_stuck_child_times_out_and_is_killed(monkeypatch, child, mode):
    pid_file = child(mode)
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 0.5)
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        mcp_gateway.mcp_gateway("call", "codebase-memory", "search_graph", {})
    assert time.monotonic() - started < 3.0
    assert _events()[-1]["outcome"] == "timeout"
    assert _pid_gone(pid_file)


def test_a_child_flooding_stdout_is_cut_off_at_the_byte_bound(monkeypatch, child):
    child("flood")
    monkeypatch.setattr(mcp_gateway, "MAX_WIRE_BYTES", 256 * 1024)
    with pytest.raises(ToolError, match="too large"):
        mcp_gateway.mcp_gateway("call", "codebase-memory", "search_graph", {})
    assert _events()[-1]["outcome"] == "response_too_large"


def test_a_child_that_exits_fails_fast(monkeypatch, child):
    child("exit")
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 10.0)
    started = time.monotonic()
    with pytest.raises(ToolError, match="failed"):
        mcp_gateway.mcp_gateway("call", "codebase-memory", "search_graph", {})
    assert time.monotonic() - started < 5
    assert _events()[-1]["outcome"] == "transport_error"


def test_a_command_that_cannot_start_is_a_bounded_refusal(monkeypatch, tmp_path, stdio_catalog):
    missing = tmp_path / STDIO_NAME
    spec = {"transport": "stdio", "command": str(missing)}
    _configure(monkeypatch, {"codebase-memory": spec})
    with pytest.raises(ToolError, match="could not be started"):
        mcp_gateway.mcp_gateway("call", "codebase-memory", "search_graph", {})
    assert _events()[-1]["outcome"] == "spawn_failed"


def test_one_dead_server_does_not_sink_discovery_of_the_rest(
    monkeypatch, tmp_path, stdio_catalog, downstream
):
    spec = {"transport": "stdio", "command": str(tmp_path / STDIO_NAME)}
    _configure(monkeypatch, {"gbrain": {"url": URL, "secret": SECRET}, "codebase-memory": spec})
    result = mcp_gateway.mcp_gateway("search_tools", query="get_page")
    assert [row["name"] for row in result["content"]] == ["gbrain.get_page"]
    assert result["servers"]["codebase-memory"]["state"] == "spawn_failed"
    assert _events()[-1]["outcome"] == "ok_partial"


# --- slice 2: a failure stays bounded ----------------------------------------------


def test_a_stuck_resolver_cannot_outlast_the_hard_deadline(monkeypatch, dns, downstream):
    _configure(monkeypatch)
    release = threading.Event()
    dns.table["brain.example.ts.net"] = lambda: release.wait(10) and ["100.64.0.15"]
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_gateway, "_TEARDOWN_GRACE_S", 0.1)
    started = time.monotonic()
    try:
        with pytest.raises(ToolError, match="timed out"):
            mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
        assert time.monotonic() - started < 2
        assert mcp_gateway.mcp_gateway("list_tools")["servers"]["gbrain"]["state"] == "timeout"
    finally:
        release.set()


def test_a_timed_out_call_keeps_its_slot_until_the_worker_really_ends(monkeypatch, dns, downstream):
    _configure(monkeypatch)
    release = threading.Event()
    dns.table["brain.example.ts.net"] = lambda: release.wait(10) and ["100.64.0.15"]
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_gateway, "_TEARDOWN_GRACE_S", 0.1)
    monkeypatch.setattr(mcp_gateway, "MAX_CONCURRENT_PER_SERVER", 1)
    try:
        with pytest.raises(ToolError, match="timed out"):
            mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
        with pytest.raises(ToolError, match="concurrent call limit"):
            mcp_gateway.mcp_gateway("call", "gbrain", "search", {})
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        slot = mcp_gateway._try_slot("gbrain")
        if slot is not None:
            slot.release()
            break
        time.sleep(0.01)
    else:
        pytest.fail("the worker ended but its slot never came back")


def test_one_stuck_server_leaves_the_others_their_tool_lists(monkeypatch):
    monkeypatch.setattr(mcp_gateway, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_gateway, "_TEARDOWN_GRACE_S", 0.1)
    release = threading.Event()
    registry = mcp_gateway.parse_servers(
        json.dumps(
            {
                "gbrain": {"url": URL, "secret": SECRET},
                "codebase-memory": {
                    "transport": "stdio",
                    "command": "/opt/bin/codebase-memory-mcp",
                },
            }
        )
    )
    monkeypatch.setattr(mcp_gateway, "_load_token", lambda server: "")

    @contextlib.asynccontextmanager
    async def connect(server, token, state):
        if server.name == "gbrain":
            await asyncio.to_thread(release.wait, 10)
        yield object()

    async def list_allowed(session, server):
        return [{"name": f"{server.name}.t", "server": server.name, "description": ""}]

    monkeypatch.setattr(mcp_gateway, "_connect", connect)
    monkeypatch.setattr(mcp_gateway, "_list_allowed", list_allowed)
    monkeypatch.setattr(mcp_gateway, "MAX_CONCURRENT_PER_SERVER", 1)
    try:
        rows, states, _ = mcp_gateway._discover(registry.values())
        healthy = mcp_gateway._try_slot("codebase-memory")
        assert healthy is not None  # the healthy server's slot is not pinned
        healthy.release()
        assert mcp_gateway._try_slot("gbrain") is None  # the stuck one's is
    finally:
        release.set()
    assert [row["name"] for row in rows] == ["codebase-memory.t"]
    assert states["gbrain"]["state"] == "timeout"
    assert states["codebase-memory"]["state"] == "ok"


def test_a_slot_is_free_by_the_time_the_caller_has_its_result():
    order = []

    async def work():
        order.append("work")
        return "done"

    assert mcp_gateway._run(work, on_exit=lambda: order.append("released")) == "done"
    assert order == ["work", "released"]


def test_calls_past_the_concurrency_limit_are_refused_not_queued(monkeypatch, downstream):
    _configure(monkeypatch, WRITES)
    monkeypatch.setattr(mcp_gateway, "MAX_CONCURRENT_PER_SERVER", 1)
    held = mcp_gateway._try_slot("gbrain")
    assert held is not None
    with _as("Desktop_Worker"), pytest.raises(ToolError, match="concurrent call limit"):
        mcp_gateway.mcp_gateway("call", "gbrain", "put_page", {"slug": "x"})
    assert _events()[-1]["outcome"] == "busy"
    assert mcp_gateway.mcp_gateway("list_tools")["servers"]["gbrain"]["state"] == "busy"
    held.release()
    assert _park({"slug": "x"})  # nothing was queued while busy, and the slot came back


# --- slice 2: doctor ---------------------------------------------------------------


def test_doctor_reports_each_server_separately_without_network(
    monkeypatch, tmp_path, dns, stdio_catalog
):
    executable = tmp_path / STDIO_NAME
    executable.write_text("#!/bin/sh\n")
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    good_stdio = {"transport": "stdio", "command": str(executable)}
    _configure(
        monkeypatch,
        {
            "gbrain": {"url": URL, "secret": SECRET, "allow": ["search", "put_page"]},
            "codebase-memory": good_stdio,
            "unknown": {"url": URL, "secret": SECRET},
        },
    )
    row = mcp_gateway.doctor_check()
    assert row["status"] == "DEGRADED"
    servers = row["servers"]
    assert servers["gbrain"]["status"] == "LIVE"
    assert (servers["gbrain"]["reads"], servers["gbrain"]["writes"]) == (1, 1)
    assert servers["gbrain"]["scope"] == "tailnet"
    assert servers["codebase-memory"]["status"] == "LIVE"
    assert servers["unknown"]["status"] == "MISCONFIGURED"
    assert "gbrain: LIVE" in row["detail"] and "unknown: MISCONFIGURED" in row["detail"]
    assert dns.calls == []  # config and metadata only

    executable.chmod(0o600)
    state = mcp_gateway.doctor_check()["servers"]["codebase-memory"]
    assert (state["status"], state["detail"]) == (
        "MISCONFIGURED",
        "command is not an executable file",
    )


def test_doctor_marks_a_server_whose_last_discovery_failed(monkeypatch, serve):
    _configure(monkeypatch)
    serve(reply={"initialize": lambda request, body: httpx2.Response(500, json={})})
    mcp_gateway.mcp_gateway("list_tools")
    row = mcp_gateway.doctor_check()
    assert row["status"] == "DEGRADED"
    assert row["servers"]["gbrain"]["detail"] == "last discovery failed: transport_error"


# --- slice 2: the one-entry harness config -----------------------------------------

HUB = "http://hub.example.ts.net:8765/mcp"


def test_each_harness_gets_its_own_entry_shape():
    entries = {e["harness"]: e for e in harness_entry.render_entries(HUB)["entries"]}
    assert set(entries) == set(harness_entry.HARNESSES)
    claude = json.loads(entries["claude"]["config"])["mcpServers"]["unified-agent"]
    assert (claude["type"], claude["url"]) == ("http", HUB)
    assert claude["headers"]["Authorization"] == "Bearer <this-agent-token>"
    codex = tomllib.loads(entries["codex"]["config"])["mcp_servers"]["unified-agent"]
    assert codex == {"url": HUB, "bearer_token_env_var": "UA_AGENT_BEARER_TOKEN"}
    antigravity = json.loads(entries["antigravity"]["config"])["mcpServers"]["unified-agent"]
    assert antigravity == {"serverUrl": HUB}
    cursor = json.loads(entries["cursor"]["config"])["mcpServers"]["unified-agent"]
    assert cursor["headers"]["Authorization"].endswith("${env:UA_CURSOR_HUB_TOKEN}")
    assert json.loads(entries["opencode"]["config"])["mcp"]["unified-agent"]["type"] == "remote"
    assert HUB in entries["hermes"]["config"]


def test_mcp_entry_prints_without_credentials_network_or_writes(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UA_HTTP_HUB_URL", HUB)
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", TOKEN)

    def no_client(*args, **kwargs):
        raise AssertionError("mcp-entry must not build a hub client")

    monkeypatch.setattr("unified_agent.cli.client.ReadClient", no_client)
    assert cli_main.run(["mcp-entry", "--harness", "codex"]) == 0
    printed = capsys.readouterr().out
    assert "[mcp_servers.unified-agent]" in printed and "no harness configuration" in printed
    assert cli_main.run(["mcp-entry", "--format", "json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert len(envelope["data"]["entries"]) == len(harness_entry.HARNESSES)
    assert TOKEN not in printed + json.dumps(envelope)
    assert list(home.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["mcp-entry"],
        ["mcp-entry", "--hub", "http://hub.example/not-mcp"],
        ["mcp-entry", "--hub", "http://" + "u:p" + "\x40hub.example/mcp"],
        ["mcp-entry", "--harness", "unknown", "--hub", HUB],
    ],
    ids=["no-hub", "wrong-path", "userinfo", "unknown-harness"],
)
def test_mcp_entry_refuses_bad_input(monkeypatch, capsys, argv):
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    assert cli_main.run(argv) == 2
    assert "ua:" in capsys.readouterr().err


def test_a_caller_denied_a_workspace_reaches_only_the_gateway_secret(monkeypatch):
    """DENY limits a caller's filesystem tools, not the hub's own work (task #2357)."""
    _configure(monkeypatch)
    server = next(
        iter(mcp_gateway.load_registry(config.mcp_gateway_servers_raw()).servers.values())
    )
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        assert mcp_gateway._secret_status(server) is None
        assert mcp_gateway._load_token(server) == TOKEN
        # The binding is back, and the store's other consumers keep their refusal.
        assert config.workspace_root_var.get() is config.WORKSPACE_DENIED
        with pytest.raises(ToolError, match="filesystem access is denied"):
            managed_secrets.metadata(SECRET)
    finally:
        config.workspace_root_var.reset(token)
