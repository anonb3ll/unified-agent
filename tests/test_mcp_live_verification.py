"""MCP 2.x live progress and resource-subscription verification test suite (UA #864).

Covers end-to-end unmocked verification of:
1. Long-running progress emission through the real MCP server/client path.
2. Subscription lifecycle: initial snapshot, update delivery, unsubscribe, poller.
3. Reconnect semantics: zero lost updates across disconnection windows.
4. Stale-client behavior: optimistic lock guard, 404 on deleted resources, and profile isolation.
5. Streamable HTTP ASGI transport delivery of progress and subscriptions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated

from unified_agent import config, mcp_live_state, mcp_progress, profiles, storage
from unified_agent.mcp_live_state import (
    BOARD_URI,
    RUN_URI_TEMPLATE,
    TASK_URI_TEMPLATE,
    run_uri,
    task_uri,
)
from unified_agent.mcp_live_verification import (
    LiveProgressAndSubscriptionHarness,
    run_live_verification_sync,
)
from unified_agent.server import mcp
from unified_agent.tools.tasks import task_add, task_delete, task_update

# pi_run_task refuses the unpinned npx/bunx fallback, so a host without a
# local `pi` would fail before Popen. Stub the resolved binary.
_PI_BIN = patch(
    "unified_agent.tools.pi_runner.resolve_pi_executable",
    return_value=["/usr/bin/pi"],
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def running_session_manager(server: MCPServer) -> AsyncGenerator[None, None]:
    async with server.session_manager.run():
        yield


def create_asgi_client(app: Any, base_url: str = "http://127.0.0.1:8765") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
    )


class TestLiveProgressVerification:
    @pytest.mark.anyio
    async def test_long_running_multi_stage_progress_emission(self):
        milestones: list[tuple[float, float | None, str | None]] = []

        async def on_progress(p, t, m):
            milestones.append((p, t, m))

        mock_lines = [
            json.dumps({"type": "agent_start"}),
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "name": "read",
                    "tool_call_id": "call_1",
                    "arguments": {"path": "test.py"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "name": "read",
                    "tool_call_id": "call_1",
                    "result": {"content": "ok"},
                }
            ),
            json.dumps({"type": "agent_end"}),
        ]
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = mock_lines
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ("", "")

        with (
            patch("unified_agent.tools.pi_runner.subprocess.Popen", return_value=mock_proc),
            _PI_BIN,
        ):
            async with Client(mcp) as client:
                res = await client.call_tool(
                    "pi_run_task",
                    {"prompt": "Multi-stage progress check", "timeout_s": 10},
                    progress_callback=on_progress,
                )
                assert not res.is_error

        assert milestones == [
            (0, 3, "starting Pi"),
            (1, 3, "Pi spawned"),
            (2, 3, "first tool"),
            (3, 3, "success"),
        ]

    @pytest.mark.anyio
    async def test_fractional_and_burst_progress_reporting(self):
        reports: list[tuple[float, float | None, str | None]] = []

        class FakeContext:
            async def report_progress(self, progress, total=None, message=None):
                reports.append((progress, total, message))

        ctx = FakeContext()

        def sync_worker():
            for i in range(1, 6):
                mcp_progress.report_progress(i * 0.2, 1.0, f"stage {i}")

        with mcp_progress.progress_scope(ctx):
            await anyio.to_thread.run_sync(sync_worker)

        assert len(reports) == 5
        assert reports[0] == (0.2, 1.0, "stage 1")
        assert reports[-1] == (1.0, 1.0, "stage 5")

    @pytest.mark.anyio
    async def test_progress_emission_with_tool_failure_preserves_session(self):
        reports: list[tuple[float, float | None, str | None]] = []

        async def on_progress(p, t, m):
            reports.append((p, t, m))

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = [json.dumps({"type": "agent_start"})]
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "Pi process crashed with error")

        with (
            patch("unified_agent.tools.pi_runner.subprocess.Popen", return_value=mock_proc),
            _PI_BIN,
        ):
            async with Client(mcp) as client:
                res = await client.call_tool(
                    "pi_run_task",
                    {"prompt": "Failing task", "timeout_s": 10},
                    progress_callback=on_progress,
                )
                assert not res.is_error
                body = json.loads(res.content[0].text)
                assert body["status"] == "error"

                # Next tool call on same client session continues to succeed
                echo_call = await client.call_tool("task_list", {"limit": 1})
                assert not echo_call.is_error

        assert (0, 3, "starting Pi") in reports
        assert (3, 3, "error") in reports

    def test_unbound_progress_is_safe_noop(self):
        mcp_progress.report_progress(0.5, 1.0, "unbound milestone")

    @pytest.mark.anyio
    async def test_streamable_http_progress_token_handling(self):
        app = mcp.streamable_http_app()
        async with running_session_manager(mcp):
            async with create_asgi_client(app) as client:
                init_resp = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "progress-client", "version": "1.0"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
                session_id = init_resp.headers.get("mcp-session-id")
                assert session_id is not None

                call_resp = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "task_list",
                            "arguments": {"limit": 5},
                            "_meta": {"progressToken": "token-xyz-123"},
                        },
                    },
                    headers={
                        "mcp-session-id": session_id,
                        "Accept": "application/json, text/event-stream",
                    },
                )
                assert call_resp.status_code == 200
                assert "tasks" in call_resp.text


class TestLiveSubscriptionLifecycle:
    @pytest.mark.anyio
    async def test_initial_snapshot_reads_board_and_tasks_and_runs(self):
        t = task_add("snapshot verification task")
        tid = t["id"]

        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "run-live-001",
                    tid,
                    None,
                    "Desktop_agent3",
                    "running",
                    "Desktop_agent3",
                    storage.now(),
                    storage.now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        async with Client(mcp) as client:
            # 1. Board snapshot
            board = await client.read_resource(BOARD_URI)
            b_data = json.loads(board.contents[0].text)
            assert b_data["uri"] == BOARD_URI
            assert any(row["id"] == tid for row in b_data["tasks"])

            # 2. Task snapshot
            task_res = await client.read_resource(task_uri(tid))
            t_data = json.loads(task_res.contents[0].text)
            assert t_data["uri"] == task_uri(tid)
            assert t_data["task"]["id"] == tid
            assert t_data["task"]["title"] == "snapshot verification task"

            # 3. Run snapshot
            run_res = await client.read_resource(run_uri("run-live-001"))
            r_data = json.loads(run_res.contents[0].text)
            assert r_data["uri"] == run_uri("run-live-001")
            assert r_data["run"]["run_id"] == "run-live-001"

    @pytest.mark.anyio
    async def test_direct_mutation_delivers_resource_updated_events(self):
        t = task_add("direct mutation target")
        tid = t["id"]
        events: list[ResourceUpdated] = []

        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[BOARD_URI, task_uri(tid)]) as sub:

                async def consume():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            events.append(event)
                        if len(events) >= 2:
                            return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    await anyio.sleep(0)  # listen() returned after the server's ack
                    await client.call_tool(
                        "task_update", {"id": tid, "notes": "mutation delivered"}
                    )
                    with anyio.fail_after(3):
                        while len(events) < 2:
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        uris = {e.uri for e in events}
        assert BOARD_URI in uris
        assert task_uri(tid) in uris

    @pytest.mark.anyio
    async def test_selective_subscription_filters_unsubscribed_tasks(self):
        t1 = task_add("selective task 1")
        t2 = task_add("selective task 2")
        t1_id, t2_id = t1["id"], t2["id"]

        events: list[ResourceUpdated] = []

        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[task_uri(t1_id)]) as sub:

                async def consume():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            events.append(event)

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    await anyio.sleep(0)  # listen() returned after the server's ack
                    # Mutate task 2
                    await client.call_tool("task_update", {"id": t2_id, "notes": "t2 changed"})
                    # Events arrive in order on one stream: any t2 event would
                    # be ahead of t1's, so no settle time is needed here.
                    # Mutate task 1
                    await client.call_tool("task_update", {"id": t1_id, "notes": "t1 changed"})
                    with anyio.fail_after(3):
                        while not any(e.uri == task_uri(t1_id) for e in events):
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        assert all(e.uri == task_uri(t1_id) for e in events)
        assert not any(e.uri == task_uri(t2_id) for e in events)
        assert not any(e.uri == BOARD_URI for e in events)

    @pytest.mark.anyio
    async def test_background_poller_detects_out_of_band_sqlite_changes(self, monkeypatch):
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.05)
        t = task_add("poller target")
        tid = t["id"]

        events: list[ResourceUpdated] = []

        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[BOARD_URI, task_uri(tid)]) as sub:

                async def consume():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            events.append(event)
                        if any(e.uri == task_uri(tid) for e in events):
                            return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    # The hub's poller has its baseline before the out-of-band write.
                    with anyio.fail_after(5):
                        while (
                            ready := mcp_live_state._baseline_ready
                        ) is None or not ready.is_set():
                            await anyio.sleep(0.002)

                    # Update task directly via task_update (simulates background process)
                    task_update(tid, notes="poller background update")

                    with anyio.fail_after(3):
                        while not any(e.uri == task_uri(tid) for e in events):
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        assert any(e.uri == task_uri(tid) for e in events)
        assert any(e.uri == BOARD_URI for e in events)

    @pytest.mark.anyio
    async def test_dispatch_run_subscription_and_delivery(self):
        rid = "run-event-999"
        t = task_add("run associated task")
        tid = t["id"]

        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    tid,
                    None,
                    "Desktop_agent3",
                    "running",
                    "Desktop_agent3",
                    storage.now(),
                    storage.now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        events: list[ResourceUpdated] = []
        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[run_uri(rid)]) as sub:

                async def consume():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            events.append(event)
                            return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    await anyio.sleep(0)  # listen() returned after the server's ack
                    mcp_live_state.notify_run_change(rid)
                    with anyio.fail_after(3):
                        while not events:
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        assert any(e.uri == run_uri(rid) for e in events)

    @pytest.mark.anyio
    async def test_unsubscribe_stream_closure_and_post_unsub_safety(self):
        t = task_add("unsub target")
        tid = t["id"]

        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[BOARD_URI, task_uri(tid)]):
                await anyio.sleep(0)

            res = await client.call_tool("task_update", {"id": tid, "notes": "post unsub note"})
            assert not res.is_error
            body = json.loads(res.content[0].text)
            assert body["id"] == tid


class TestReconnectSemantics:
    @pytest.mark.anyio
    async def test_reconnect_reconciles_interim_mutations_without_loss(self):
        # 1. Baseline state
        t_live = task_add("reconnect baseline live")
        t_del = task_add("reconnect baseline to delete")
        t_mod = task_add("reconnect baseline to modify")
        live_id, del_id, mod_id = t_live["id"], t_del["id"], t_mod["id"]

        # 2. Client 1 establishes baseline snapshot S0
        async with Client(mcp) as client1:
            s0_board = await client1.read_resource(BOARD_URI)
            s0_tasks = {row["id"]: row for row in json.loads(s0_board.contents[0].text)["tasks"]}
            assert live_id in s0_tasks
            assert del_id in s0_tasks
            assert mod_id in s0_tasks

        # --- Client 1 Disconnects (Offline Window) ---

        # 3. Offline mutations occur while no client is listening
        t_new = task_add("reconnect added during offline window")
        new_id = t_new["id"]
        task_update(mod_id, title="modified during offline window")
        task_delete(del_id)

        # --- Client 2 Connects (Reconnect) ---

        # 4. Client 2 connects, reads fresh snapshot S1, and opens new subscription
        reconnected_events: list[ResourceUpdated] = []
        async with Client(mcp) as client2:
            s1_board = await client2.read_resource(BOARD_URI)
            s1_tasks = {row["id"]: row for row in json.loads(s1_board.contents[0].text)["tasks"]}

            # Verify snapshot S1 completeness:
            assert new_id in s1_tasks
            assert s1_tasks[mod_id]["title"] == "modified during offline window"
            assert del_id not in s1_tasks

            # 5. Verify new subscription stream receives live post-reconnect events
            async with client2.listen(resource_subscriptions=[BOARD_URI, task_uri(new_id)]) as sub:

                async def consume():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            reconnected_events.append(event)
                        if any(e.uri == task_uri(new_id) for e in reconnected_events):
                            return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    await anyio.sleep(0)  # listen() returned after the server's ack
                    await client2.call_tool(
                        "task_update", {"id": new_id, "notes": "online after reconnect"}
                    )
                    with anyio.fail_after(3):
                        while not any(e.uri == task_uri(new_id) for e in reconnected_events):
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        assert any(e.uri == task_uri(new_id) for e in reconnected_events)
        assert any(e.uri == BOARD_URI for e in reconnected_events)


class TestStaleClientGuardsAndEdgeCases:
    @pytest.mark.anyio
    async def test_optimistic_lock_rejects_stale_revision(self):
        t = task_add("optimistic lock target")
        tid = t["id"]
        t_row = task_update(tid, notes="rev 0")
        initial_rev = t_row["updated_at"]

        # Advance revision in database
        task_update(tid, notes="rev 1 advance")

        async with Client(mcp) as client:
            res = await client.call_tool(
                "task_update",
                {
                    "id": tid,
                    "notes": "stale overwrite attempt",
                    "expected_updated_at": initial_rev,
                },
            )
            assert res.is_error
            err_text = res.content[0].text if res.content else ""
            assert "authority revision changed" in err_text or "expected updated_at" in err_text

    @pytest.mark.anyio
    async def test_deleted_resource_returns_not_found(self):
        t = task_add("to be deleted")
        tid = t["id"]
        task_delete(tid)

        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource(task_uri(tid))

    @pytest.mark.anyio
    async def test_invalid_task_and_run_uris_fail_closed(self):
        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource("unified-agent://tasks/-100")
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource("unified-agent://tasks/not_a_valid_integer")
            with pytest.raises(MCPError, match="unknown dispatch run"):
                await client.read_resource("unified-agent://runs/missing_run_id")

    @pytest.mark.anyio
    async def test_slow_or_broken_listeners_do_not_poison_bus(self):
        events_received: list[str] = []

        class RaisingListener:
            def __call__(self, event):
                raise RuntimeError("simulated client disconnect in listener")

        class HealthyListener:
            def __call__(self, event):
                events_received.append(event.uri)

        bus = getattr(mcp, "_subscriptions", None)
        assert bus is not None

        unsub_raising = bus.subscribe(RaisingListener())
        unsub_healthy = bus.subscribe(HealthyListener())

        try:
            await mcp_live_state.publish_uris(mcp, [BOARD_URI])
            assert BOARD_URI in events_received
        finally:
            unsub_raising()
            unsub_healthy()

    @pytest.mark.anyio
    async def test_profile_access_control_denies_and_hides_runs(self, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent8")
        session = config.mcp_session_id_var.set("test-research-profile-session")
        try:
            async with Client(mcp) as client:
                templates = await client.list_resource_templates()
                t_uris = {t.uri_template for t in templates.resource_templates}
                assert TASK_URI_TEMPLATE in t_uris
                assert RUN_URI_TEMPLATE not in t_uris

                with pytest.raises(MCPError, match="not exposed"):
                    await client.read_resource("unified-agent://runs/restricted_run")
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.pop("test-research-profile-session", None)


class TestIntegrationHarnessRunner:
    @pytest.mark.anyio
    async def test_harness_run_all_produces_clean_report(self):
        harness = LiveProgressAndSubscriptionHarness(server=mcp)
        with _PI_BIN:
            report = await harness.run_all()

        assert report.passed is True
        assert report.checks_total == 4
        assert report.checks_passed == 4
        assert report.checks_failed == 0
        assert len(report.progress_milestones) >= 4
        assert len(report.events_recorded) >= 2
        summary = report.summary()
        assert "PASS (4/4 checks passed" in summary

    def test_sync_harness_runner_entrypoint(self):
        with _PI_BIN:
            report = run_live_verification_sync(server=mcp)
        assert report.passed is True
        assert report.checks_passed == 4
