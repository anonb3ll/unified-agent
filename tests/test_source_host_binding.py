"""Hub-written rows record the verified caller's host (task #1633).

``config.source_host_var`` had no request setter, so every task created on the
hub stamped the hub's own hostname. The auth middleware now binds it from the
verified identity through the operator's UA_AGENT_HOSTS binding only, read
from an in-memory snapshot built outside requests, else the hub. Request headers, the self-reported heartbeat host and self-chosen ua-node
names are never used. The binding ends with the request, so a task the request
spawned cannot keep stamping the caller's host.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, node_queue, registry, session_credentials, source_host, storage
from unified_agent.server import HubAuthASGIMiddleware

TOKENS = "Desktop_Agent1:tok-desk,gcp_Agent2:tok-gcp"
DESK = {"Authorization": "Bearer tok-desk"}
GCP = {"Authorization": "Bearer tok-gcp"}


@pytest.fixture(autouse=True)
def _hub(monkeypatch):
    monkeypatch.setenv("UA_SOURCE_HOST", "mac-hub")
    monkeypatch.setenv("UA_AGENT_TOKENS", TOKENS)
    monkeypatch.delenv("UA_AGENT_HOSTS", raising=False)
    source_host.reset_snapshot()
    storage.init_db()
    registry.reset_throttle()


def _set_hosts(monkeypatch, value: str) -> None:
    """Set UA_AGENT_HOSTS and load its snapshot, as hub startup does."""
    monkeypatch.setenv("UA_AGENT_HOSTS", value)
    source_host.prime()


def _task_host(task_id: int) -> str | None:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT source_host FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return row["source_host"]


async def _create_task(request):
    from unified_agent.tools.tasks import task_add

    task = task_add("filed over the hub")
    return JSONResponse(
        {
            "id": task["id"],
            "write_host": config.write_source_host(),
            "machine_host": config.current_source_host(),
        }
    )


async def _probe(request):
    return JSONResponse({"write_host": config.write_source_host()})


@pytest.fixture
def client():
    app = Starlette(
        routes=[
            Route("/task", _create_task, methods=["POST"]),
            Route("/api/probe", _probe, methods=["GET"]),
        ]
    )
    return TestClient(HubAuthASGIMiddleware(app))


class TestResolver:
    def test_binding_resolves(self, monkeypatch):
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        assert source_host.resolve_identity_host("Desktop_Agent1") == "linux-desktop-desktop"

    def test_unbound_identity_is_none(self):
        assert source_host.resolve_identity_host("gcp_Agent2") is None
        assert source_host.resolve_identity_host("") is None
        assert source_host.resolve_identity_host(None) is None

    def test_hub_binding_is_the_hub(self, monkeypatch):
        _set_hosts(monkeypatch, "Desktop_Agent1:hub")
        assert source_host.resolve_identity_host("Desktop_Agent1") is None

    def test_bad_binding_values_bind_nothing(self, monkeypatch):
        _set_hosts(monkeypatch, "Desktop_Agent1:" + "x" * 300)
        assert source_host.resolve_identity_host("Desktop_Agent1") is None

    def test_malformed_bindings_are_the_hub_fallback(self, monkeypatch):
        def boom():
            raise RuntimeError("malformed UA_AGENT_HOSTS")

        monkeypatch.setattr(config, "agent_hosts", boom)
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        assert source_host.resolve_identity_host("Desktop_Agent1") is None

    def test_self_reported_heartbeat_host_is_never_used(self):
        # Round 1 P2 #1: agent_heartbeat(host=...) is a tool argument.
        registry.touch_agent("gcp_Agent2", via="heartbeat_tool", host="arbitrary-tool-host")
        assert source_host.resolve_identity_host("gcp_Agent2") is None

    def test_self_named_live_node_is_never_used(self, monkeypatch):
        # Round 2 P2 #1: a node registers under a name it picks itself.
        node_queue.register("arbitrary-tool-host", identity="Desktop_Agent1")
        assert source_host.resolve_identity_host("Desktop_Agent1") is None
        _set_hosts(monkeypatch, "Desktop_Agent1:operator-desktop")
        assert source_host.resolve_identity_host("Desktop_Agent1") == "operator-desktop"

    def test_explicit_hub_binding_ignores_stored_hosts(self, monkeypatch):
        # Round 1 P2 #4: label:hub is an answer, and nothing stored overrides it.
        registry.touch_agent("Desktop_Agent1", via="heartbeat_tool", host="arbitrary-tool-host")
        node_queue.register("arbitrary-node", identity="Desktop_Agent1")
        _set_hosts(monkeypatch, "Desktop_Agent1:hub")
        assert source_host.resolve_identity_host("Desktop_Agent1") is None


async def test_resolution_never_touches_the_store_or_a_worker_thread(monkeypatch):
    """Round 2 P2 #2: 50 concurrent requests against a stalled store start no workers."""
    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    store_calls = 0

    def stalled(*args, **kwargs):
        nonlocal store_calls
        store_calls += 1
        time.sleep(1.0)
        raise RuntimeError("stalled store")

    monkeypatch.setattr(storage, "connect", stalled)
    monkeypatch.setattr(storage, "connect_read_only", stalled)
    threads_before = threading.active_count()
    peak = threads_before

    async def one() -> str:
        nonlocal peak
        bound = source_host.bind("Desktop_Agent1")
        try:
            await asyncio.sleep(0)
            peak = max(peak, threading.active_count())
            return config.write_source_host()
        finally:
            source_host.reset(bound)

    started = time.monotonic()
    hosts = await asyncio.gather(*(one() for _ in range(50)))
    elapsed = time.monotonic() - started
    assert hosts == ["linux-desktop-desktop"] * 50
    assert store_calls == 0
    assert peak == threads_before
    assert elapsed < 0.5, elapsed


class TestMiddlewareBinding:
    def test_desktop_identity_records_desktop_host(self, client, monkeypatch):
        """DoD: a task created by an identity bound to the desktop records that host."""
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        resp = client.post("/task", headers=DESK)
        assert resp.status_code == 200
        body = resp.json()
        assert body["write_host"] == "linux-desktop-desktop"
        assert _task_host(body["id"]) == "linux-desktop-desktop"

    def test_machine_host_stays_the_hub(self, client, monkeypatch):
        # fleet_actions, Devices and the doctor ask "which machine is this
        # process?" through current_source_host(); a caller must not move it.
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        assert client.post("/task", headers=DESK).json()["machine_host"] == "mac-hub"

    def test_unbound_identity_falls_back_to_hub(self, client):
        body = client.post("/task", headers=GCP).json()
        assert body["write_host"] == "mac-hub"
        assert _task_host(body["id"]) == "mac-hub"

    def test_unauthenticated_request_falls_back_to_hub(self, client):
        body = client.post("/task").json()
        assert _task_host(body["id"]) == "mac-hub"

    def test_spoofed_host_headers_are_ignored(self, client, monkeypatch):
        spoof = {
            "Host": "attacker-box",
            "X-Forwarded-Host": "attacker-box",
            "X-UA-Source-Host": "attacker-box",
            "X-Source-Host": "attacker-box",
        }
        # Unbound caller: still the hub, not the header.
        body = client.post("/task", headers={**GCP, **spoof}).json()
        assert _task_host(body["id"]) == "mac-hub"
        # Bound caller: its binding, not the header.
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        body = client.post("/task", headers={**DESK, **spoof}).json()
        assert _task_host(body["id"]) == "linux-desktop-desktop"

    def test_api_route_is_bound_too(self, client, monkeypatch):
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        assert client.get("/api/probe", headers=DESK).json() == {
            "write_host": "linux-desktop-desktop"
        }

    def test_binding_does_not_leak_across_requests(self, client, monkeypatch):
        _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
        assert (
            client.get("/api/probe", headers=DESK).json()["write_host"] == "linux-desktop-desktop"
        )
        assert client.get("/api/probe", headers=GCP).json()["write_host"] == "mac-hub"
        assert client.get("/api/probe").json()["write_host"] == "mac-hub"
        assert config.source_host_var.get() is None


async def test_binding_does_not_leak_across_concurrent_requests(monkeypatch):
    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    both_inside = asyncio.Barrier(2)
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        await both_inside.wait()
        seen[scope["ua_identity"]] = config.write_source_host()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = HubAuthASGIMiddleware(app)

    async def call(token: str) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/probe",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1234),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await middleware(scope, receive, send)

    await asyncio.wait_for(asyncio.gather(call("tok-desk"), call("tok-gcp")), timeout=10)
    assert seen == {"Desktop_Agent1": "linux-desktop-desktop", "gcp_Agent2": "mac-hub"}
    assert config.source_host_var.get() is None


async def test_mcp_task_add_over_the_hub_records_the_callers_host(monkeypatch):
    """End to end: a task_add tool call on /mcp stamps the bound desktop host."""
    import httpx

    from unified_agent.server import mcp

    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    accept = {"Accept": "application/json, text/event-stream", **DESK}
    app = mcp.streamable_http_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as client:
            init = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "source-host", "version": "1.0"},
                    },
                },
                headers={**accept, "X-Forwarded-Host": "attacker-box"},
            )
            session_id = init.headers.get("mcp-session-id")
            assert session_id
            call = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "task_add", "arguments": {"title": "from the desktop"}},
                },
                headers={**accept, "mcp-session-id": session_id},
            )
            assert call.status_code == 200, call.text

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT source_host FROM tasks WHERE title = ?", ("from the desktop",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, call.text
    assert row["source_host"] == "linux-desktop-desktop"
    assert config.source_host_var.get() is None


def _scope(token: str) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1234),
    }


async def _run_detached_child(middleware_factory, token: str) -> tuple[str, str]:
    """A request spawns a task that writes only after the request has ended."""
    from unified_agent.tools.tasks import task_add

    released = asyncio.Event()
    children: list[asyncio.Task] = []
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        seen["in_request"] = config.write_source_host()

        async def child():
            await released.wait()
            return task_add("written after the request ended")["id"]

        children.append(asyncio.create_task(child()))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await middleware_factory(app)(_scope(token), receive, send)
    released.set()
    task_id = await asyncio.wait_for(children[0], timeout=10)
    return seen["in_request"], _task_host(task_id)


async def test_detached_task_after_bearer_request_stamps_the_hub(monkeypatch):
    """Review P2 #2: the copied ContextVar must not outlive the request."""
    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    in_request, stored = await _run_detached_child(HubAuthASGIMiddleware, "tok-desk")
    assert in_request == "linux-desktop-desktop"
    assert stored == "mac-hub"


async def test_detached_task_after_session_credential_request_stamps_the_hub(monkeypatch):
    """Review P2 #2, the uas_ path (_serve_session_credential)."""
    from types import SimpleNamespace

    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    verified = SimpleNamespace(
        subject="Desktop_Agent1",
        auth_metadata={"auth_method": "acp_session", "scopes": []},
        grant=SimpleNamespace(item_id="item-1633"),
        peer_device=None,
    )
    monkeypatch.setattr(session_credentials, "is_session_token", lambda value: True)
    monkeypatch.setattr(session_credentials, "verify", lambda *a, **k: verified)
    in_request, stored = await _run_detached_child(HubAuthASGIMiddleware, "uas_stub")
    assert in_request == "linux-desktop-desktop"
    assert stored == "mac-hub"


async def test_self_named_node_does_not_override_the_operator_binding(monkeypatch):
    """Round 2 P2 #1 end to end: a registered node name never reaches the stamp."""
    node_queue.register("arbitrary-tool-host", identity="Desktop_Agent1")
    _set_hosts(monkeypatch, "Desktop_Agent1:operator-desktop")
    in_request, _ = await _run_detached_child(HubAuthASGIMiddleware, "tok-desk")
    assert in_request == "operator-desktop"


async def test_pre_app_telemetry_fault_leaves_no_binding(monkeypatch):
    """Round 2 P3: a fault before the app runs must not leave a live binding set."""
    from unified_agent import mcp_runtime_telemetry

    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")

    def boom(**kwargs):
        raise RuntimeError("telemetry fault")

    monkeypatch.setattr(mcp_runtime_telemetry, "record_session_touch", boom)
    outer = config.source_host_var.set("outer-host")
    try:
        with pytest.raises(RuntimeError, match="telemetry fault"):
            await _run_detached_child(HubAuthASGIMiddleware, "tok-desk")
        assert config.source_host_var.get() == "outer-host"
        assert config.write_source_host() == "outer-host"
    finally:
        config.source_host_var.reset(outer)


async def test_heartbeat_tool_host_does_not_reach_the_stamp(monkeypatch):
    """Review P2 #1 end to end: a heartbeat host argument, then a task_add."""
    from unified_agent.tools.registry import agent_heartbeat

    token = config.agent_identity_var.set("gcp_Agent2")
    try:
        agent_heartbeat(host="arbitrary-tool-host")
    finally:
        config.agent_identity_var.reset(token)
    in_request, _ = await _run_detached_child(HubAuthASGIMiddleware, "tok-gcp")
    assert in_request == "mac-hub"


def _slow_fs(monkeypatch, delay: float = 0.2) -> tuple[list[str], threading.Event]:
    """Make every filesystem probe the binding parser uses slow, and record it.

    A probe holds for ``delay`` or until the returned event is set. A probe on
    the event loop stalls it for the full delay (the test cannot set the event
    meanwhile); one off the loop ends as soon as the test sets it."""
    import os
    from pathlib import Path

    calls: list[str] = []
    release = threading.Event()
    real_exists, real_is_file, real_read = os.path.exists, Path.is_file, Path.read_text

    def exists(path, *a, **k):
        calls.append(f"exists:{path}")
        release.wait(delay)
        return real_exists(path, *a, **k)

    def is_file(self, *a, **k):
        calls.append(f"is_file:{self}")
        release.wait(delay)
        return real_is_file(self, *a, **k)

    def read_text(self, *a, **k):
        calls.append(f"read_text:{self}")
        release.wait(delay)
        return real_read(self, *a, **k)

    monkeypatch.setattr(config.os.path, "exists", exists)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_text", read_text)
    return calls, release


async def _timer_lateness(work) -> float:
    """How late a 10 ms timer fires while ``work()`` runs on the loop."""
    started = time.monotonic()
    timer = asyncio.create_task(asyncio.sleep(0.01))
    await asyncio.sleep(0)
    work()
    await timer
    return time.monotonic() - started


async def test_bind_reads_no_file_and_does_not_stall_the_loop(monkeypatch, tmp_path):
    """Round 3 P2: a file-backed binding is read at startup, never during bind."""
    hosts_file = tmp_path / "agent_hosts"
    hosts_file.write_text("Desktop_Agent1:file-desktop\n")
    _set_hosts(monkeypatch, str(hosts_file))
    calls, _release = _slow_fs(monkeypatch)

    seen: list[str] = []

    def fifty_binds():
        for _ in range(50):
            bound = source_host.bind("Desktop_Agent1")
            seen.append(config.write_source_host())
            source_host.reset(bound)

    elapsed = await _timer_lateness(fifty_binds)
    assert seen == ["file-desktop"] * 50
    assert calls == []
    assert elapsed < 0.1, elapsed


async def test_missing_snapshot_binds_the_hub_and_refreshes_off_the_loop(monkeypatch, tmp_path):
    """No snapshot for the current value: the hub now, the binding once loaded off-loop."""
    hosts_file = tmp_path / "agent_hosts"
    hosts_file.write_text("Desktop_Agent1:file-desktop\n")
    monkeypatch.setenv("UA_AGENT_HOSTS", str(hosts_file))  # no prime()
    calls, release = _slow_fs(monkeypatch)
    seen: list[str] = []

    def fifty_binds():
        for _ in range(50):
            bound = source_host.bind("Desktop_Agent1")
            seen.append(config.write_source_host())
            source_host.reset(bound)

    elapsed = await _timer_lateness(fifty_binds)
    assert seen == ["mac-hub"] * 50
    assert elapsed < 0.1, elapsed
    release.set()  # the off-loop refresh need not sit out its slow probes
    # One refresh in flight at most, and it lands without a request reading.
    assert source_host.wait_for_refresh(timeout=5)
    assert len([c for c in calls if c.startswith("read_text:")]) == 1
    assert source_host.resolve_identity_host("Desktop_Agent1") == "file-desktop"


def test_stale_snapshot_keeps_serving_while_refreshing(monkeypatch):
    _set_hosts(monkeypatch, "Desktop_Agent1:linux-desktop-desktop")
    monkeypatch.setattr(source_host, "REFRESH_S", 0.0)
    built: list[str] = []
    real = config.agent_hosts
    release = threading.Event()

    def slow_agent_hosts(raw=None):
        # Slow unless released: a rebuild on the request path would show up
        # in the timing below; the background one ends once released.
        release.wait(0.2)
        built.append(raw or "")
        return real(raw)

    monkeypatch.setattr(config, "agent_hosts", slow_agent_hosts)
    started = time.monotonic()
    for _ in range(50):
        assert source_host.resolve_identity_host("Desktop_Agent1") == "linux-desktop-desktop"
    assert time.monotonic() - started < 0.1
    release.set()
    assert source_host.wait_for_refresh(timeout=5)
    assert len(built) == 1


def test_edited_binding_file_is_picked_up_by_the_background_rebuild(monkeypatch, tmp_path):
    """PR #1303 review: an edited binding file must not keep stamping the old
    host. The value (the path) is unchanged, so the snapshot is rebuilt from
    the file's new contents once it is REFRESH_S old, off the request path."""
    hosts_file = tmp_path / "agent_hosts"
    hosts_file.write_text("Desktop_Agent1:desk-a\n")
    _set_hosts(monkeypatch, str(hosts_file))
    assert source_host.resolve_identity_host("Desktop_Agent1") == "desk-a"

    hosts_file.write_text("Desktop_Agent1:desk-b\n")
    # Within REFRESH_S the snapshot answers and nothing reads the file.
    assert source_host.resolve_identity_host("Desktop_Agent1") == "desk-a"
    assert source_host.wait_for_refresh(timeout=5)
    assert source_host.resolve_identity_host("Desktop_Agent1") == "desk-a"

    monkeypatch.setattr(source_host, "REFRESH_S", 0.0)
    source_host.resolve_identity_host("Desktop_Agent1")  # starts the rebuild
    assert source_host.wait_for_refresh(timeout=5)
    assert source_host.resolve_identity_host("Desktop_Agent1") == "desk-b"
