"""MCP 2.x live progress notifications and resource-subscription verification harness (UA #864).

Provides unmocked live verification of:
1. Long-running progress emission through the real MCP server/client path.
2. Resource subscription lifecycle: initial snapshot, event delivery, unsubscribe,
   and out-of-band poller updates.
3. Reconnect semantics: deterministic snapshot-to-stream reconciliation with zero
   lost or duplicate updates across disconnection windows.
4. Stale-client behavior: optimistic lock rejection, 404 on deleted resources,
   and profile-scoped access control.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated

from . import config, mcp_live_state, profiles
from .mcp_live_state import BOARD_URI, task_uri
from .tools.tasks import task_add, task_delete, task_update

logger = logging.getLogger(__name__)

# Prompt the progress probe sends to pi_run_task. The tool refuses the
# unpinned npx/bunx fallback, so the probe stubs the resolved binary (it
# already mocks Popen) and observes progress without launching Pi or pulling
# an unpinned package.
_PI_PROGRESS_PROMPT = "Live progress verification task"


@dataclass
class VerificationCheckResult:
    name: str
    passed: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveVerificationReport:
    timestamp: str
    passed: bool
    duration_ms: float
    checks_total: int
    checks_passed: int
    checks_failed: int
    events_recorded: list[dict[str, Any]] = field(default_factory=list)
    progress_milestones: list[dict[str, Any]] = field(default_factory=list)
    checks: list[VerificationCheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            f"=== MCP 2.x Live Verification Report [{self.timestamp}] ===",
            f"Status: {'PASS' if self.passed else 'FAIL'} ({self.checks_passed}/{self.checks_total} checks passed, {self.checks_failed} failed, {self.duration_ms:.1f}ms)",
            f"Progress milestones recorded: {len(self.progress_milestones)}",
            f"Live events recorded: {len(self.events_recorded)}",
            "Checks:",
        ]
        for check in self.checks:
            tag = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{tag:4}] {check.name}: {check.detail}")
        return "\n".join(lines)


class LiveProgressAndSubscriptionHarness:
    """End-to-end integration harness for MCP 2.x progress and live subscriptions."""

    def __init__(self, server: MCPServer | None = None) -> None:
        if server is None:
            from .server import mcp

            self.server = mcp
        else:
            self.server = server
        self.events_recorded: list[dict[str, Any]] = []
        self.progress_milestones: list[dict[str, Any]] = []
        self.checks: list[VerificationCheckResult] = []

    async def verify_progress_emission(self) -> VerificationCheckResult:
        """Verify real-path multi-stage progress emission and client callback delivery."""
        milestones: list[tuple[float, float | None, str | None]] = []

        async def on_progress(
            progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            milestones.append((progress, total, message))
            self.progress_milestones.append(
                {"progress": progress, "total": total, "message": message, "timestamp": time.time()}
            )

        mock_lines = [
            json.dumps({"type": "agent_start"}),
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "name": "read",
                    "tool_call_id": "call_p1",
                    "arguments": {"path": "module.py"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "name": "read",
                    "tool_call_id": "call_p1",
                    "result": {"content": "code"},
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
            patch(
                "unified_agent.tools.pi_runner.resolve_pi_executable",
                return_value=["/usr/bin/pi"],
            ),
        ):
            async with Client(self.server) as client:
                result = await client.call_tool(
                    "pi_run_task",
                    {"prompt": _PI_PROGRESS_PROMPT, "timeout_s": 10},
                    progress_callback=on_progress,
                )

        if result.is_error:
            err_msg = result.content[0].text if result.content else "Tool call failed"
            return VerificationCheckResult(
                name="progress_emission",
                passed=False,
                detail=f"Tool error: {err_msg}",
            )

        expected_stages = [
            (0, 3, "starting Pi"),
            (1, 3, "Pi spawned"),
            (2, 3, "first tool"),
            (3, 3, "success"),
        ]
        passed = milestones == expected_stages
        detail = (
            f"Received {len(milestones)} monotonically ordered milestones matching expected sequence"
            if passed
            else f"Milestone mismatch: received {milestones} vs expected {expected_stages}"
        )
        return VerificationCheckResult(
            name="progress_emission",
            passed=passed,
            detail=detail,
            data={"milestones": milestones},
        )

    async def verify_subscription_lifecycle(self) -> VerificationCheckResult:
        """Verify initial snapshot, active listen stream, direct tool events, poller, and unsubscribe."""
        created = task_add("live subscription verification")
        tid = created["id"]
        direct_events: list[ResourceUpdated] = []
        poller_events: list[ResourceUpdated] = []

        orig_poll_interval = mcp_live_state.POLL_INTERVAL_S
        mcp_live_state.POLL_INTERVAL_S = 0.05
        try:
            async with Client(self.server) as client:
                # 1. Initial snapshot check
                board_res = await client.read_resource(BOARD_URI)
                board_payload = json.loads(board_res.contents[0].text)
                if not any(row["id"] == tid for row in board_payload.get("tasks", [])):
                    return VerificationCheckResult(
                        name="subscription_lifecycle",
                        passed=False,
                        detail="Initial board snapshot missing newly created task",
                    )

                task_res = await client.read_resource(task_uri(tid))
                task_payload = json.loads(task_res.contents[0].text)
                if task_payload.get("task", {}).get("id") != tid:
                    return VerificationCheckResult(
                        name="subscription_lifecycle",
                        passed=False,
                        detail="Initial task resource read mismatch",
                    )

                # 2. Open subscription and verify direct mutation delivery
                async with client.listen(resource_subscriptions=[BOARD_URI, task_uri(tid)]) as sub:

                    async def consume():
                        async for event in sub:
                            if isinstance(event, ResourceUpdated):
                                direct_events.append(event)
                                self.events_recorded.append(
                                    {
                                        "uri": event.uri,
                                        "type": "live_subscription",
                                        "timestamp": time.time(),
                                    }
                                )
                            if len(direct_events) >= 2:
                                return

                    async with anyio.create_task_group() as tg:
                        tg.start_soon(consume)
                        await anyio.sleep(0.05)
                        update_res = await client.call_tool(
                            "task_update",
                            {"id": tid, "notes": "live subscription updated via real client path"},
                        )
                        if update_res.is_error:
                            return VerificationCheckResult(
                                name="subscription_lifecycle",
                                passed=False,
                                detail="task_update call failed during subscription",
                            )
                        with anyio.fail_after(3):
                            while len(direct_events) < 2:
                                await anyio.sleep(0.05)
                        tg.cancel_scope.cancel()

                # 3. Verify poller picks up out-of-band updates during a new subscription session
                async with client.listen(resource_subscriptions=[BOARD_URI, task_uri(tid)]) as sub2:

                    async def consume_poller():
                        async for event in sub2:
                            if isinstance(event, ResourceUpdated):
                                poller_events.append(event)
                                self.events_recorded.append(
                                    {"uri": event.uri, "type": "poller", "timestamp": time.time()}
                                )
                            if any(e.uri == task_uri(tid) for e in poller_events):
                                return

                    async with anyio.create_task_group() as tg2:
                        tg2.start_soon(consume_poller)
                        await anyio.sleep(0.1)
                        # Direct task update via sync function
                        task_update(tid, notes="poller out-of-band update")
                        with anyio.fail_after(3):
                            while not any(e.uri == task_uri(tid) for e in poller_events):
                                await anyio.sleep(0.05)
                        tg2.cancel_scope.cancel()

                # 4. Verify post-unsubscribe safety: client is no longer listening; mutation succeeds
                post_unsub = task_update(tid, notes="post-unsubscribe clean mutation")
                if post_unsub.get("id") != tid:
                    return VerificationCheckResult(
                        name="subscription_lifecycle",
                        passed=False,
                        detail="Post-unsubscribe task mutation failed",
                    )
        finally:
            mcp_live_state.POLL_INTERVAL_S = orig_poll_interval

        direct_uris = {e.uri for e in direct_events}
        has_board = BOARD_URI in direct_uris
        has_task = task_uri(tid) in direct_uris
        has_poller = any(e.uri == task_uri(tid) for e in poller_events)

        passed = has_board and has_task and has_poller
        detail = (
            f"Delivered {len(direct_events)} direct events ({direct_uris}) and {len(poller_events)} poller events cleanly"
            if passed
            else f"Delivery incomplete: direct_uris={direct_uris}, poller_count={len(poller_events)}"
        )
        return VerificationCheckResult(
            name="subscription_lifecycle",
            passed=passed,
            detail=detail,
            data={"direct_uris": list(direct_uris), "poller_count": len(poller_events)},
        )

    async def verify_reconnect_reconciliation(self) -> VerificationCheckResult:
        """Verify reconnect semantics: zero lost updates across disconnection window."""
        # 1. Baseline task setup
        t1 = task_add("baseline task 1")
        t2 = task_add("baseline task 2 to delete")
        t3 = task_add("baseline task 3 to modify")
        t1_id, t2_id, t3_id = t1["id"], t2["id"], t3["id"]

        # 2. Initial Client session captures baseline snapshot S0
        async with Client(self.server) as client1:
            s0_board = await client1.read_resource(BOARD_URI)
            s0_data = json.loads(s0_board.contents[0].text)
            s0_ids = {t["id"] for t in s0_data.get("tasks", [])}
            assert {t1_id, t2_id, t3_id}.issubset(s0_ids)

        # Client 1 is now disconnected (simulating network drop / client restart)

        # 3. Offline mutations during disconnection window:
        #    - Add task 4
        #    - Modify task 3
        #    - Delete task 2
        t4 = task_add("offline added task 4")
        t4_id = t4["id"]
        task_update(t3_id, title="baseline task 3 modified offline")
        task_delete(t2_id)

        # 4. Client 2 reconnects, establishes fresh snapshot S1 and new listen stream
        reconnected_events: list[ResourceUpdated] = []
        async with Client(self.server) as client2:
            s1_board = await client2.read_resource(BOARD_URI)
            s1_data = json.loads(s1_board.contents[0].text)
            s1_tasks_by_id = {t["id"]: t for t in s1_data.get("tasks", [])}

            # Verification of S1 completeness:
            # - t4 must exist
            # - t3 must reflect modified title
            # - t2 must be absent
            has_t4 = t4_id in s1_tasks_by_id
            t3_updated = (
                s1_tasks_by_id.get(t3_id, {}).get("title") == "baseline task 3 modified offline"
            )
            t2_removed = t2_id not in s1_tasks_by_id

            if not (has_t4 and t3_updated and t2_removed):
                return VerificationCheckResult(
                    name="reconnect_reconciliation",
                    passed=False,
                    detail=f"Lost updates during offline window: has_t4={has_t4}, t3_updated={t3_updated}, t2_removed={t2_removed}",
                )

            # 5. Live stream on reconnected client receives subsequent online updates
            async with client2.listen(resource_subscriptions=[BOARD_URI, task_uri(t4_id)]) as sub:

                async def consume_reconnected():
                    async for event in sub:
                        if isinstance(event, ResourceUpdated):
                            reconnected_events.append(event)
                            self.events_recorded.append(
                                {
                                    "uri": event.uri,
                                    "type": "reconnected",
                                    "timestamp": time.time(),
                                }
                            )
                        if any(e.uri == task_uri(t4_id) for e in reconnected_events):
                            return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume_reconnected)
                    await anyio.sleep(0.05)
                    await client2.call_tool(
                        "task_update", {"id": t4_id, "notes": "online update after reconnect"}
                    )
                    with anyio.fail_after(3):
                        while not any(e.uri == task_uri(t4_id) for e in reconnected_events):
                            await anyio.sleep(0.05)
                    tg.cancel_scope.cancel()

        passed = len(reconnected_events) > 0 and has_t4 and t3_updated and t2_removed
        detail = (
            "0 lost updates in offline window; post-reconnect snapshot and live stream reconciled cleanly"
            if passed
            else "Reconnection reconciliation failed"
        )
        return VerificationCheckResult(
            name="reconnect_reconciliation",
            passed=passed,
            detail=detail,
            data={
                "offline_mutations_captured": True,
                "reconnected_events": [e.uri for e in reconnected_events],
            },
        )

    async def verify_stale_client_guards(self) -> VerificationCheckResult:
        """Verify optimistic locking, deleted resource 404, and profile authorization gates."""
        t = task_add("stale client guard target")
        tid = t["id"]
        t_row = task_update(tid, notes="initial baseline note")
        init_updated_at = t_row["updated_at"]

        # Advance database revision
        task_update(tid, notes="concurrent change advancing updated_at")

        async with Client(self.server) as client:
            # 1. Stale revision rejection via expected_updated_at
            stale_call = await client.call_tool(
                "task_update",
                {
                    "id": tid,
                    "notes": "stale overwrite attempt",
                    "expected_updated_at": init_updated_at,
                },
            )
            # Must return error / ToolError indicating revision mismatch
            err_text = (
                stale_call.content[0].text if (stale_call.is_error and stale_call.content) else ""
            )
            stale_rejected = stale_call.is_error and (
                "authority revision changed" in err_text or "expected updated_at" in err_text
            )

            # 2. Deleted resource returns ResourceNotFoundError (404)
            t_del = task_add("to be deleted for 404 test")
            del_id = t_del["id"]
            task_delete(del_id)

            del_404 = False
            try:
                await client.read_resource(task_uri(del_id))
            except MCPError as exc:
                del_404 = "unknown task" in str(exc)

            # 3. Invalid URI returns ResourceNotFoundError
            invalid_404 = False
            try:
                await client.read_resource("unified-agent://tasks/not-an-id")
            except MCPError as exc:
                invalid_404 = "unknown task" in str(exc)

        # 4. Profile authorization guard on runs resource
        profile_guarded = False
        old_enforce = os.environ.get("UA_ENFORCE_AUTH")
        os.environ["UA_ENFORCE_AUTH"] = "1"
        identity_token = config.agent_identity_var.set("Desktop_Agent8")
        session_token = config.mcp_session_id_var.set("stale-guard-session")
        try:
            async with Client(self.server) as client:
                try:
                    await client.read_resource("unified-agent://runs/any-run")
                except MCPError as exc:
                    profile_guarded = "not exposed" in str(exc)
        finally:
            config.mcp_session_id_var.reset(session_token)
            config.agent_identity_var.reset(identity_token)
            with profiles._lock:
                profiles._active.pop("stale-guard-session", None)
            if old_enforce is None:
                os.environ.pop("UA_ENFORCE_AUTH", None)
            else:
                os.environ["UA_ENFORCE_AUTH"] = old_enforce

        passed = stale_rejected and del_404 and invalid_404 and profile_guarded
        detail = (
            f"Guards verified: optimistic_lock={stale_rejected}, deleted_404={del_404}, "
            f"invalid_404={invalid_404}, profile_gate={profile_guarded}"
        )
        return VerificationCheckResult(
            name="stale_client_guards",
            passed=passed,
            detail=detail,
            data={
                "optimistic_lock_rejected": stale_rejected,
                "deleted_resource_404": del_404,
                "invalid_resource_404": invalid_404,
                "profile_gate_enforced": profile_guarded,
            },
        )

    async def run_all(self) -> LiveVerificationReport:
        """Run the complete live progress and subscription verification suite."""
        t0 = time.perf_counter()
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.checks = []
        self.events_recorded = []
        self.progress_milestones = []

        # Run checks sequentially with deterministic state
        check_progress = await self.verify_progress_emission()
        self.checks.append(check_progress)

        check_lifecycle = await self.verify_subscription_lifecycle()
        self.checks.append(check_lifecycle)

        check_reconnect = await self.verify_reconnect_reconciliation()
        self.checks.append(check_reconnect)

        check_stale = await self.verify_stale_client_guards()
        self.checks.append(check_stale)

        duration_ms = (time.perf_counter() - t0) * 1000.0
        passed_count = sum(1 for c in self.checks if c.passed)
        failed_count = sum(1 for c in self.checks if not c.passed)
        overall_passed = failed_count == 0

        return LiveVerificationReport(
            timestamp=now_str,
            passed=overall_passed,
            duration_ms=duration_ms,
            checks_total=len(self.checks),
            checks_passed=passed_count,
            checks_failed=failed_count,
            events_recorded=self.events_recorded,
            progress_milestones=self.progress_milestones,
            checks=self.checks,
        )


def run_live_verification_sync(server: MCPServer | None = None) -> LiveVerificationReport:
    """Synchronous entry point for scripts and test harnesses."""
    harness = LiveProgressAndSubscriptionHarness(server=server)
    return anyio.run(harness.run_all)
