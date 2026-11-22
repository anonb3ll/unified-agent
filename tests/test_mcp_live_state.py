"""MCP 2.x progress notifications and live-state resource subscriptions (task #823)."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import anyio
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated

from unified_agent import (
    config,
    events,
    mcp_live_state,
    mcp_progress,
    object_model,
    profiles,
    storage,
)
from unified_agent.mcp_live_state import BOARD_URI, RUN_URI_TEMPLATE, TASK_URI_TEMPLATE, task_uri
from unified_agent.server import mcp
from unified_agent.tools.tasks import task_add, task_delete, task_update


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeProgressContext:
    def __init__(self):
        self.reports: list[tuple[float, float | None, str | None]] = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, total, message))


class TestProgressAdapter:
    @pytest.mark.anyio
    async def test_worker_thread_reports_absolute_milestones(self):
        ctx = _FakeProgressContext()

        def worker():
            mcp_progress.report_progress(0, 3, "starting")
            mcp_progress.report_progress(3, 3, "success")

        with mcp_progress.progress_scope(ctx):
            await anyio.to_thread.run_sync(worker)

        assert ctx.reports == [(0, 3, "starting"), (3, 3, "success")]

    def test_unbound_report_is_noop(self):
        mcp_progress.report_progress(1, 2, "ignored")


class TestLiveStateDiff:
    def test_first_snapshot_emits_nothing(self):
        current = {BOARD_URI: "a", task_uri(1): "b"}
        assert mcp_live_state.diff_uris(None, current) == []

    def test_task_update_emits_board_and_task(self):
        prev = {BOARD_URI: "a", task_uri(1): "a"}
        current = {BOARD_URI: "b", task_uri(1): "b"}
        assert mcp_live_state.diff_uris(prev, current) == [BOARD_URI, task_uri(1)]

    def test_task_delete_emits_board_and_task(self):
        prev = {BOARD_URI: "a", task_uri(1): "a"}
        current = {BOARD_URI: "b"}
        assert mcp_live_state.diff_uris(prev, current) == [BOARD_URI, task_uri(1)]

    def test_run_update_emits_run_uri(self):
        uri = mcp_live_state.run_uri("abc")
        prev = {uri: "1"}
        current = {uri: "2"}
        assert mcp_live_state.diff_uris(prev, current) == [uri]

    def test_registry_resource_update_emits_its_uri(self):
        uri = "unified-agent://plans/plan-1"
        assert mcp_live_state.diff_uris({uri: "before"}, {uri: "after"}) == [uri]

    def test_org_charter_snapshot_detects_changed_office(self, monkeypatch):
        from unified_agent import org_registry

        original = org_registry.load_registry()
        before = mcp_live_state._org_unit_snapshot()
        changed = [dict(office) for office in original]
        changed[0]["decision_rights"] = ["updated decision right"]
        monkeypatch.setattr(org_registry, "load_registry", lambda: changed)
        after = mcp_live_state._org_unit_snapshot()
        assert mcp_live_state.diff_uris(before, after) == ["unified-agent://org-units/founder-ceo"]

        shared_baseline = {**before, "unified-agent://board": "task-rev"}
        mcp_live_state._replace_org_revisions(shared_baseline, after)
        assert (
            mcp_live_state.diff_uris(
                shared_baseline, {**after, "unified-agent://board": "task-rev"}
            )
            == []
        )

    def test_snapshot_strategy_covers_every_served_type(self):
        conn = storage.connect()
        try:
            for object_type in object_model.served_types():
                if object_type.kind == "org_unit":
                    assert object_type.table is None
                    assert mcp_live_state._org_unit_snapshot()
                    continue
                assert object_type.table is not None
                assert mcp_live_state._table_snapshot(conn, object_type.table) is not None
            assert mcp_live_state._table_snapshot(conn, "event_log") == []
        finally:
            conn.close()
        assert len(mcp_live_state.snapshot()[BOARD_URI]) == 64

    def test_timestamp_free_session_and_token_changes_invalidate_uris(self):
        session_id = "01J8ZQ4T7X9K2M5N7P8R0S3T6W"
        before = mcp_live_state.snapshot()
        _NewObjectFixtures.insert_session(session_id)
        conn = storage.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO agent_tokens (agent_label, token_hash, status, issued_at)"
                " VALUES ('Desktop_LiveReader', ?, 'active', '2026-09-17T00:00:00Z')",
                ("a" * 64,),
            )
            token_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        session_uri = f"unified-agent://sessions/{session_id}"
        token_uri = f"unified-agent://agent-tokens/{token_id}"
        after_insert = mcp_live_state.snapshot()
        assert mcp_live_state.diff_uris(before, after_insert) == [token_uri, session_uri]
        assert len(after_insert[session_uri]) == 64
        assert len(after_insert[token_uri]) == 64
        assert "Desktop_LiveReader" not in after_insert[session_uri]
        assert "a" * 64 not in after_insert[token_uri]

        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET last_heartbeat_at='2026-09-18T00:00:00Z' WHERE id=?",
                (session_id,),
            )
            conn.execute("UPDATE agent_tokens SET status='revoked' WHERE id=?", (token_id,))
            conn.commit()
        finally:
            conn.close()
        after_update = mcp_live_state.snapshot()
        assert mcp_live_state.diff_uris(after_insert, after_update) == [token_uri, session_uri]

    def test_memory_feedback_invalidates_note_even_without_updated_at(self):
        note_id = _NewObjectFixtures.insert_memory_note("feedback-snapshot-1284")
        before = mcp_live_state.snapshot()
        from unified_agent.tools.memory import memory_feedback

        memory_feedback(note_id, "helpful")
        after = mcp_live_state.snapshot()
        assert mcp_live_state.diff_uris(before, after) == [f"unified-agent://memory/{note_id}"]

    def test_memory_restore_invalidates_note_without_updated_at(self):
        note_id = _NewObjectFixtures.insert_memory_note("restore-snapshot-1284")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE memory SET superseded_by='replacement-note' WHERE id=?", (note_id,)
            )
            conn.commit()
        finally:
            conn.close()
        before = mcp_live_state.snapshot()
        from unified_agent.tools.memory import memory_restore

        assert memory_restore(note_id)["restored"] is True
        after = mcp_live_state.snapshot()
        assert f"unified-agent://memory/{note_id}" in mcp_live_state.diff_uris(before, after)

    def test_session_invalidation_includes_joined_task_fields(self):
        task_id = _NewObjectFixtures.insert_task()
        session_id = "01J8ZQ4T7X9K2M5N7P8R0S3T6X"
        _NewObjectFixtures.insert_session(session_id)
        conn = storage.connect()
        try:
            conn.execute("UPDATE agent_sessions SET task_id=? WHERE id=?", (task_id, session_id))
            conn.commit()
        finally:
            conn.close()
        before = mcp_live_state.snapshot()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET title='joined task changed', status='doing' WHERE id=?",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()
        changed = mcp_live_state.diff_uris(before, mcp_live_state.snapshot())
        assert f"unified-agent://sessions/{session_id}" in changed
        assert f"unified-agent://tasks/{task_id}" in changed

    def test_agent_status_transition_invalidates_agent_uri(self, monkeypatch):
        _NewObjectFixtures.insert_agent("Desktop_StatusTransition")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agents SET last_seen='2026-09-17T00:00:00+00:00' "
                "WHERE label='Desktop_StatusTransition'"
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(config, "agent_status_thresholds", lambda: (10.0, 20.0, 30.0))
        last_seen = datetime(2026, 9, 17, tzinfo=UTC)
        before = mcp_live_state._agent_liveness_snapshot(last_seen + timedelta(seconds=10))
        after = mcp_live_state._agent_liveness_snapshot(last_seen + timedelta(seconds=11))
        assert mcp_live_state.diff_uris(before, after) == [
            "unified-agent://agents/Desktop_StatusTransition"
        ]

    def test_malformed_agent_label_does_not_break_liveness_snapshot(self):
        _NewObjectFixtures.insert_agent("malformed/agent")
        result = mcp_live_state._agent_liveness_snapshot()
        assert "unified-agent://agents/malformed/agent" not in result

    def test_session_clock_revision_changes_at_minute_boundary(self):
        session_id = _NewObjectFixtures.insert_session()
        before = mcp_live_state._session_clock_snapshot(datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC))
        same_minute = mcp_live_state._session_clock_snapshot(
            datetime(2026, 9, 22, 12, 0, 1, tzinfo=UTC)
        )
        next_minute = mcp_live_state._session_clock_snapshot(
            datetime(2026, 9, 22, 12, 1, 0, tzinfo=UTC)
        )
        uri = f"unified-agent://sessions/{session_id}"
        assert mcp_live_state.diff_uris(before, same_minute) == []
        assert mcp_live_state.diff_uris(before, next_minute) == [uri]

    def test_terminal_session_clock_does_not_keep_advancing(self):
        session_id = _NewObjectFixtures.insert_session()
        conn = storage.connect()
        try:
            conn.execute(
                # A terminal session has both an end time and a terminal status;
                # an ended row still marked 'running' would be reaped on read.
                "UPDATE agent_sessions SET status='completed', ended_at='2026-09-22T12:00:00Z'"
                " WHERE id=?",
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()
        before = mcp_live_state._session_clock_snapshot(datetime(2026, 9, 22, 12, 0, 1, tzinfo=UTC))
        after = mcp_live_state._session_clock_snapshot(datetime(2026, 9, 22, 12, 0, 2, tzinfo=UTC))
        assert before == after
        assert f"unified-agent://sessions/{session_id}" not in before
        from unified_agent.registry import get_session

        session = get_session(session_id)
        assert session is not None
        assert (
            session["heartbeat_age_s"]
            == (
                datetime(2026, 9, 22, 12, tzinfo=UTC) - datetime(2026, 9, 17, tzinfo=UTC)
            ).total_seconds()
        )

    def test_unrelated_commit_reuses_evidence_snapshot(self, monkeypatch):
        from unified_agent.events import record_event

        record_event("test.evidence", payload={"n": 1})
        before, cursor = mcp_live_state.snapshot_with_cursor()
        assert cursor is not None
        _NewObjectFixtures.insert_task()
        queries: list[str] = []
        original_connect = storage.connect

        def traced_connect():
            conn = original_connect()
            conn.set_trace_callback(queries.append)
            return conn

        monkeypatch.setattr(storage, "connect", traced_connect)
        after, _ = mcp_live_state.snapshot_with_cursor(before, cursor)
        assert any("FROM event_log" in sql for sql in queries)
        assert not any('SELECT * FROM "event_log"' in sql for sql in queries)
        assert {
            uri: revision
            for uri, revision in before.items()
            if uri.startswith("unified-agent://evidence/")
        } == {
            uri: revision
            for uri, revision in after.items()
            if uri.startswith("unified-agent://evidence/")
        }

    def test_cursor_captures_append_and_prune(self):
        from unified_agent.events import record_event

        before, cursor = mcp_live_state.snapshot_with_cursor()
        event_id = record_event("test.new", payload={"n": 1})
        appended, cursor = mcp_live_state.snapshot_with_cursor(before, cursor)
        event_uri = f"unified-agent://evidence/{event_id}"
        assert event_uri in mcp_live_state.diff_uris(before, appended)

        conn = storage.connect()
        try:
            with events.ledger_maintenance(conn):
                conn.execute("DELETE FROM event_log WHERE ulid=?", (event_id,))
            conn.commit()
        finally:
            conn.close()
        pruned, _ = mcp_live_state.snapshot_with_cursor(appended, cursor)
        assert event_uri in mcp_live_state.diff_uris(appended, pruned)

    def test_event_snapshot_detects_pruning_and_hash_rewrites(self, monkeypatch):
        from unified_agent.events import record_event

        # created_at is hashed: record a genuinely old event rather than
        # tampering with its timestamp, which pruning correctly refuses.
        old_stamp = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        with monkeypatch.context() as clock:
            clock.setattr(storage, "now", lambda: old_stamp)
            old_id = record_event("test.old", payload={"n": 1})
        kept_id = record_event("test.kept", payload={"n": 2})
        assert events.verify_event_chain()["status"] == "valid"
        before = mcp_live_state.snapshot()

        from unified_agent.events import prune_events

        assert prune_events(older_than_days=1) == 1
        assert events.verify_event_chain()["status"] == "valid"
        after = mcp_live_state.snapshot()
        changed = mcp_live_state.diff_uris(before, after)
        assert f"unified-agent://evidence/{old_id}" in changed
        assert f"unified-agent://evidence/{kept_id}" in changed

    def test_incremental_snapshot_matches_full_snapshot(self):
        """Carrying the ledger forward yields exactly what a fresh snapshot sees (#2028)."""
        from unified_agent.events import record_event

        record_event("test.evidence", payload={"n": 1})
        before, cursor = mcp_live_state.snapshot_with_cursor()
        _NewObjectFixtures.insert_task()
        same_ledger, cursor = mcp_live_state.snapshot_with_cursor(before, cursor)
        assert same_ledger == mcp_live_state.snapshot()
        assert cursor.appended_evidence == ()

        event_id = record_event("test.appended", payload={"n": 2})
        appended, next_cursor = mcp_live_state.snapshot_with_cursor(same_ledger, cursor)
        assert appended == mcp_live_state.snapshot()
        assert next_cursor.appended_evidence == (f"unified-agent://evidence/{event_id}",)

    def test_snapshot_changes_match_full_diff(self):
        from unified_agent.events import record_event

        before, cursor = mcp_live_state.snapshot_with_cursor()
        task_id = _NewObjectFixtures.insert_task()
        event_id = record_event("test.appended", payload={"n": 1})
        after, next_cursor = mcp_live_state.snapshot_with_cursor(before, cursor)
        changed = mcp_live_state._snapshot_changes(before, after, cursor, next_cursor)
        assert changed == mcp_live_state.diff_uris(before, after)
        assert task_uri(task_id) in changed
        assert f"unified-agent://evidence/{event_id}" in changed

    def test_poll_does_not_walk_the_ledger_on_an_unrelated_commit(self, monkeypatch):
        """A changed poll renders and diffs only what moved, not every event (#2028)."""
        from unified_agent.events import record_event

        for n in range(5):
            record_event("test.evidence", payload={"n": n})
        _, state = mcp_live_state._poll_once(mcp_live_state._PollState())
        try:
            evidence_type = object_model.by_kind("evidence_event")
            rendered: list[str] = []
            real_render = type(evidence_type).render

            def counting_render(self, object_id):
                if self.kind == "evidence_event":
                    rendered.append(str(object_id))
                return real_render(self, object_id)

            monkeypatch.setattr(type(evidence_type), "render", counting_render)

            real_diff = mcp_live_state.diff_uris

            def no_ledger_diff(previous, current):
                if any(uri.startswith("unified-agent://evidence/") for uri in current):
                    raise AssertionError("poll diffed the whole ledger")
                return real_diff(previous, current)

            monkeypatch.setattr(mcp_live_state, "diff_uris", no_ledger_diff)
            task_id = _NewObjectFixtures.insert_task()
            uris, state = mcp_live_state._poll_once(state)
            assert task_uri(task_id) in uris
            assert rendered == []

            event_id = record_event("test.appended", payload={"n": 99})
            uris, state = mcp_live_state._poll_once(state)
            assert f"unified-agent://evidence/{event_id}" in uris
            assert rendered == [event_id]
        finally:
            if state.conn is not None:
                state.conn.close()


class TestMcpResources:
    @pytest.mark.anyio
    async def test_list_and_read_board_and_task(self):
        created = task_add("mcp live board")
        tid = created["id"]
        async with Client(mcp) as client:
            listed = await client.list_resources()
            uris = {str(r.uri) for r in listed.resources}
            assert BOARD_URI in uris
            templates = await client.list_resource_templates()
            t_uris = {t.uri_template for t in templates.resource_templates}
            assert TASK_URI_TEMPLATE in t_uris
            assert RUN_URI_TEMPLATE in t_uris

            board = await client.read_resource(BOARD_URI)
            payload = json.loads(board.contents[0].text)
            assert payload["uri"] == BOARD_URI
            assert any(row["id"] == tid for row in payload["tasks"])

            row = await client.read_resource(task_uri(tid))
            body = json.loads(row.contents[0].text)
            assert body["task"]["id"] == tid
            assert body["task"]["title"] == "mcp live board"

    @pytest.mark.anyio
    async def test_missing_task_and_run_are_not_found(self):
        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource("unified-agent://tasks/999999")
            with pytest.raises(MCPError, match="unknown dispatch run"):
                await client.read_resource("unified-agent://runs/missing")
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource("unified-agent://tasks/not-an-id")

    @pytest.mark.anyio
    async def test_deleted_task_is_not_found(self):
        created = task_add("delete me")
        tid = created["id"]
        task_delete(tid)
        async with Client(mcp) as client:
            with pytest.raises(MCPError, match="unknown task"):
                await client.read_resource(task_uri(tid))

    @pytest.mark.anyio
    async def test_research_profile_hides_run_resources(self, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent8")
        session = config.mcp_session_id_var.set("live-state-research")
        try:
            async with Client(mcp) as client:
                templates = await client.list_resource_templates()
                t_uris = {t.uri_template for t in templates.resource_templates}
                assert TASK_URI_TEMPLATE in t_uris
                assert RUN_URI_TEMPLATE not in t_uris
                with pytest.raises(MCPError, match="not exposed"):
                    await client.read_resource("unified-agent://runs/anything")
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.pop("live-state-research", None)


async def _baseline_built(timeout: float = 5.0) -> None:
    """Wait for the poller's first baseline instead of sleeping past it."""
    with anyio.fail_after(timeout):
        while (ready := mcp_live_state._baseline_ready) is None or not ready.is_set():
            await anyio.sleep(0.002)


async def _poller_started(timeout: float = 5.0) -> threading.Event:
    """Wait until the poller has published its baseline-ready event, unset or not."""
    with anyio.fail_after(timeout):
        while (ready := mcp_live_state._baseline_ready) is None:
            await anyio.sleep(0.002)
    return ready


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _NewObjectFixtures:
    """Row insert helpers for the platform object readers (task #1072 rest)."""

    @staticmethod
    def insert_plan(plan_id: str = "plan-live-001") -> str:
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO plans (id, goal, scope, status, author, escalated, created_at, updated_at)"
                " VALUES (?, 'live goal', 'default', 'active', 'Desktop_Agent1', 0, ?, ?)",
                (plan_id, "2026-09-17T00:00:00Z", "2026-09-17T00:00:00Z"),
            )
            conn.commit()
            return plan_id
        finally:
            conn.close()

    @staticmethod
    def insert_task(title: str = "live task") -> int:
        conn = storage.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
                " VALUES (?, 'open', 'med', ?, ?)",
                (title, "2026-09-17T00:00:00Z", "2026-09-17T00:00:00Z"),
            )
            conn.commit()
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)
        finally:
            conn.close()

    @staticmethod
    def insert_delegation() -> int:
        task_id = _NewObjectFixtures.insert_task()
        conn = storage.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, callback,"
                " status, created_at, updated_at) VALUES ('Desktop_Agent1', 'Desktop_agent3',"
                " ?, 'live spec', 'none', 'pending', ?, ?)",
                (task_id, "2026-09-17T00:00:00Z", "2026-09-17T00:00:00Z"),
            )
            conn.commit()
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)
        finally:
            conn.close()

    @staticmethod
    def insert_agent(label: str = "Desktop_LiveReader") -> str:
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO agents (label, kind, host, known, first_seen, last_seen,"
                " created_at, updated_at) VALUES (?, 'cli', 'desktop', 1, ?, ?, ?, ?)",
                (
                    label,
                    "2026-09-17T00:00:00Z",
                    "2026-09-17T00:00:00Z",
                    "2026-09-17T00:00:00Z",
                    "2026-09-17T00:00:00Z",
                ),
            )
            conn.commit()
            return label
        finally:
            conn.close()

    @staticmethod
    def insert_session(
        session_id: str = "01J8ZQ4T7X9K2M5N7P8R0S3T6V",
        *,
        heartbeat: str = "2026-09-17T00:00:00Z",
    ) -> str:
        """Insert a 'running' session.

        Reads through registry.get_session reap a running session whose
        heartbeat is older than UA_SESSION_ABANDON_MINUTES (#855), so a test
        that reads one back as running must pass a fresh ``heartbeat``.
        """
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO agent_sessions (id, agent, kind, status, started_at,"
                " last_heartbeat_at, task_id, meta) VALUES (?, ?, ?, 'running', ?, ?, NULL, '{}')",
                (
                    session_id,
                    "Desktop_LiveReader",
                    "cli",
                    "2026-09-17T00:00:00Z",
                    heartbeat,
                ),
            )
            conn.commit()
            return session_id
        finally:
            conn.close()

    @staticmethod
    def insert_memory_note(note_id: str = "live-note-001") -> str:
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO memory (id, title, content, tags, author, created_at, updated_at)"
                " VALUES (?, 'live', 'live content', '[]', 'Desktop_Agent1', ?, ?)",
                (note_id, "2026-09-17T00:00:00Z", "2026-09-17T00:00:00Z"),
            )
            conn.commit()
            return note_id
        finally:
            conn.close()


class TestPlatformObjectLiveReads:
    """Live MCP reads of every platform object URI (task #1072)."""

    @pytest.mark.anyio
    async def test_live_reads_return_the_real_rows(self):
        _NewObjectFixtures.insert_plan()
        delegation_id = _NewObjectFixtures.insert_delegation()
        _NewObjectFixtures.insert_agent()
        session_id = _NewObjectFixtures.insert_session(heartbeat=_now_iso())
        _NewObjectFixtures.insert_memory_note()

        async with Client(mcp) as client:
            plan = json.loads(
                (await client.read_resource("unified-agent://plans/plan-live-001")).contents[0].text
            )
            assert plan["plan"]["goal"] == "live goal"
            assert plan["plan"]["status"] == "active"

            delegation = json.loads(
                (await client.read_resource(f"unified-agent://delegations/{delegation_id}"))
                .contents[0]
                .text
            )
            assert delegation["delegation"]["from_agent"] == "Desktop_Agent1"
            assert delegation["delegation"]["status"] == "pending"

            agent = json.loads(
                (await client.read_resource("unified-agent://agents/Desktop_LiveReader"))
                .contents[0]
                .text
            )
            assert agent["agent"]["label"] == "Desktop_LiveReader"
            assert agent["agent"]["status"] in {"online", "idle", "stale", "offline", "unknown"}

            session = json.loads(
                (await client.read_resource(f"unified-agent://sessions/{session_id}"))
                .contents[0]
                .text
            )
            assert session["session"]["agent"] == "Desktop_LiveReader"
            assert session["session"]["status"] == "running"

            memory = json.loads(
                (await client.read_resource("unified-agent://memory/live-note-001"))
                .contents[0]
                .text
            )
            assert memory["memory_note"]["content"] == "live content"

    @pytest.mark.anyio
    async def test_new_templates_are_registered_and_listed(self):
        async with Client(mcp) as client:
            templates = await client.list_resource_templates()
            t_uris = {t.uri_template for t in templates.resource_templates}
            for uri in (
                "unified-agent://plans/{plan_id}",
                "unified-agent://delegations/{delegation_id}",
                "unified-agent://agents/{agent_label}",
                "unified-agent://sessions/{session_id}",
                "unified-agent://memory/{memory_id}",
            ):
                assert uri in t_uris, uri

    @pytest.mark.anyio
    async def test_missing_objects_are_not_found(self):
        async with Client(mcp) as client:
            for uri, label in (
                ("unified-agent://plans/nonexistent", "unknown plan"),
                ("unified-agent://delegations/999999", "unknown delegation"),
                ("unified-agent://agents/NeverSeen", "unknown agent"),
                ("unified-agent://sessions/01J8ZQ4T7X9K2M5N7P8R0S3T6W", "unknown session"),
                ("unified-agent://memory/nonexistent-note", "unknown memory note"),
            ):
                with pytest.raises(MCPError, match=label):
                    await client.read_resource(uri)

    @pytest.mark.anyio
    async def test_research_profile_hides_gated_new_resources(self, monkeypatch):
        """A profile without plan/delegation/agent tools sees neither list nor read.

        Deliberately tests the gate path at the live MCP boundary, not just the
        reader unit level: research may read memory and sessions but not plans,
        delegations, or agents, and the resource list must reflect that.
        """
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent8")
        session = config.mcp_session_id_var.set("live-state-platform-gates")
        try:
            async with Client(mcp) as client:
                templates = await client.list_resource_templates()
                t_uris = {t.uri_template for t in templates.resource_templates}
                # memory + session are on the research allow-list.
                assert "unified-agent://memory/{memory_id}" in t_uris
                assert "unified-agent://sessions/{session_id}" in t_uris
                # plan / delegation / agent are not.
                assert "unified-agent://plans/{plan_id}" not in t_uris
                assert "unified-agent://delegations/{delegation_id}" not in t_uris
                assert "unified-agent://agents/{agent_label}" not in t_uris
                with pytest.raises(MCPError, match="not exposed"):
                    await client.read_resource("unified-agent://plans/anything")
                with pytest.raises(MCPError, match="not exposed"):
                    await client.read_resource("unified-agent://agents/Desktop_LiveReader")
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.pop("live-state-platform-gates", None)


class TestSubscriptionsAndProgress:
    @pytest.mark.anyio
    async def test_task_update_delivers_board_resource_updated(self):
        created = task_add("subscribe me")
        tid = created["id"]
        seen: list[ResourceUpdated] = []

        async with Client(mcp) as client:
            async with client.listen(resource_subscriptions=[BOARD_URI]) as sub:

                async def consume():
                    async for event in sub:
                        seen.append(event)
                        return

                async with anyio.create_task_group() as tg:
                    tg.start_soon(consume)
                    result = await client.call_tool(
                        "task_update", {"id": tid, "notes": "task-823 live"}
                    )
                    assert not result.is_error
                    with anyio.fail_after(3):
                        while not seen:
                            await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()

        assert any(isinstance(e, ResourceUpdated) and e.uri == BOARD_URI for e in seen)

    @pytest.mark.anyio
    async def test_poller_publishes_sqlite_change_without_mcp_tool_path(self, monkeypatch):
        published: list[ResourceUpdated] = []

        class _Bus:
            async def publish(self, event):
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.05)
        created = task_add("poller path")
        tid = created["id"]
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            await _baseline_built()
            task_update(tid, notes="changed out of band")
            with anyio.fail_after(2):
                while not any(
                    isinstance(e, ResourceUpdated) and e.uri == BOARD_URI for e in published
                ):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert any(isinstance(e, ResourceUpdated) and e.uri == BOARD_URI for e in published)
        assert any(isinstance(e, ResourceUpdated) and e.uri == task_uri(tid) for e in published)

    @pytest.mark.anyio
    async def test_poller_baselines_before_first_sleep(self, monkeypatch):
        """Startup must baseline immediately so early commits still invalidate.

        Devin F3 / PR #695 / task #1315: sleeping before the first baseline left
        a blind window where a subscriber could read pre-commit content and never
        receive an invalidation for a commit that landed before baseline.
        """
        published: list[ResourceUpdated] = []
        baselines: list[float] = []

        class _Bus:
            async def publish(self, event):
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        real_snapshot = mcp_live_state.snapshot_with_cursor
        start = time.monotonic()

        def counting_snapshot(*args, **kwargs):
            baselines.append(time.monotonic() - start)
            return real_snapshot(*args, **kwargs)

        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", counting_snapshot)
        # Long interval: under the old sleep-first loop, a mutation at ~20ms
        # would land inside the blind window and be absorbed into baseline.
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.25)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            with anyio.fail_after(1):
                while not baselines:
                    await anyio.sleep(0.01)
            # Baseline landed before a full poll interval elapsed.
            assert baselines[0] < 0.25
            tid = task_add("startup-window commit")["id"]
            task_update(tid, notes="landed after baseline, before next poll")
            with anyio.fail_after(2):
                while not any(
                    isinstance(e, ResourceUpdated) and e.uri == task_uri(tid) for e in published
                ):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert any(isinstance(e, ResourceUpdated) and e.uri == BOARD_URI for e in published)

    @pytest.mark.anyio
    async def test_poller_reconnects_and_publishes_after_failed_connection(self, monkeypatch):
        published: list[ResourceUpdated] = []

        class _Bus:
            async def publish(self, event):
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        task_id = task_add("poller reconnect path")["id"]
        actual_version = mcp_live_state._database_version
        connections = []

        def broken_once(conn):
            connections.append(conn)
            if len(connections) == 2:
                raise OSError("stale SQLite connection")
            return actual_version(conn)

        monkeypatch.setattr(mcp_live_state, "_database_version", broken_once)
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            with anyio.fail_after(2):
                while len(connections) < 2:
                    await anyio.sleep(0.01)
            task_update(task_id, notes="changed while connection was broken")
            with anyio.fail_after(2):
                while not any(event.uri == task_uri(task_id) for event in published):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert len(connections) >= 3
        assert connections[0] is not connections[2]
        assert any(event.uri == BOARD_URI for event in published)

    @pytest.mark.anyio
    async def test_poller_publishes_registry_resource_change(self, monkeypatch):
        published: list[ResourceUpdated] = []

        class _Bus:
            async def publish(self, event):
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        plan_id = _NewObjectFixtures.insert_plan("poll-plan-1284")
        plan_uri = f"unified-agent://plans/{plan_id}"
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.05)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            await _baseline_built()
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE plans SET updated_at='2026-09-18T00:00:00Z' WHERE id=?",
                    (plan_id,),
                )
                conn.commit()
            finally:
                conn.close()
            with anyio.fail_after(2):
                while not any(
                    isinstance(event, ResourceUpdated) and event.uri == plan_uri
                    for event in published
                ):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    @pytest.mark.anyio
    async def test_idle_poller_skips_table_snapshot(self, monkeypatch):
        calls = 0
        original_snapshot = mcp_live_state.snapshot_with_cursor

        def counted_snapshot(*args):
            nonlocal calls
            calls += 1
            return original_snapshot(*args)

        class _Server:
            _subscriptions = None

        passes = 0
        original_has_listeners = mcp_live_state._has_listeners

        def counted_has_listeners(server):
            nonlocal passes
            passes += 1
            return original_has_listeners(server)

        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", counted_snapshot)
        monkeypatch.setattr(mcp_live_state, "_has_listeners", counted_has_listeners)
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.002)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            # Each loop pass after the baseline asks about listeners.
            with anyio.fail_after(5):
                while passes < 5:
                    await anyio.sleep(0.002)
            tg.cancel_scope.cancel()
        assert calls == 1

    @pytest.mark.anyio
    async def test_poller_snapshot_runs_off_the_event_loop(self, monkeypatch):
        """Task #1958: a slow snapshot must not stall other requests on the loop."""
        original_snapshot = mcp_live_state.snapshot_with_cursor
        entered = threading.Event()
        release = threading.Event()
        released_in_time: list[bool] = []

        def blocking_snapshot(*args):
            # Holds until the test releases it. Run on the event loop, it would
            # block the test coroutine too, and the wait would time out.
            entered.set()
            released_in_time.append(release.wait(5))
            return original_snapshot(*args)

        class _Server:
            _subscriptions = None

        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", blocking_snapshot)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            with anyio.fail_after(10):
                while not entered.is_set():
                    await anyio.sleep(0.002)
            # The loop still runs this coroutine while the snapshot is held.
            await anyio.sleep(0)
            release.set()
            await _baseline_built()
            tg.cancel_scope.cancel()
        assert released_in_time == [True]

    @pytest.mark.anyio
    async def test_unlistened_poller_skips_polls_then_publishes_idle_commits(self, monkeypatch):
        """Task #1958: no listener, no snapshots; a later listener still sees idle commits.

        Task #2183: the waits are driven by the poller's own progress (each loop
        pass asks ``_has_listeners``), not by wall-clock sleeps, so a loaded
        runner only makes the test slower. The hang guard exists to fail a
        poller that stopped, not to bound how fast it must be.
        """
        hang_guard_s = 120
        calls = 0
        original_snapshot = mcp_live_state.snapshot_with_cursor
        original_has_listeners = mcp_live_state._has_listeners
        published: list[ResourceUpdated] = []
        published_task = anyio.Event()
        passes = 0
        pass_waiters: list[tuple[int, anyio.Event]] = []

        def counted_snapshot(*args):
            nonlocal calls
            calls += 1
            return original_snapshot(*args)

        def counted_has_listeners(server):
            nonlocal passes
            passes += 1
            for target, event in pass_waiters:
                if passes >= target:
                    event.set()
            return original_has_listeners(server)

        async def after_passes(count: int) -> None:
            """Return once the poller has run ``count`` more loop passes."""
            event = anyio.Event()
            pass_waiters.append((passes + count, event))
            with anyio.fail_after(hang_guard_s):
                await event.wait()

        class _Bus:
            def __init__(self):
                self._listeners: dict = {}

            async def publish(self, event):
                published.append(event)
                if event.uri == task_uri(tid):
                    published_task.set()

        class _Server:
            _subscriptions = _Bus()

        server = _Server()
        tid = task_add("idle poller")["id"]
        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", counted_snapshot)
        monkeypatch.setattr(mcp_live_state, "_has_listeners", counted_has_listeners)
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, server)
            # The baseline poll is done once the loop asks about listeners.
            await after_passes(1)
            assert calls == 1
            task_update(tid, notes="committed while nobody listened")
            await after_passes(5)
            assert calls == 1
            assert published == []
            server._subscriptions._listeners[object()] = lambda event: None
            with anyio.fail_after(hang_guard_s):
                await published_task.wait()
            tg.cancel_scope.cancel()

    @pytest.mark.anyio
    async def test_failed_publish_is_retried_on_the_next_poll(self, monkeypatch):
        """Task #1958 review: a failed publish must not advance the baseline."""
        published: list[ResourceUpdated] = []
        failures = 0

        class _Bus:
            async def publish(self, event):
                nonlocal failures
                if failures == 0:
                    failures += 1
                    raise RuntimeError("bus down once")
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        tid = task_add("publish retry")["id"]
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            await _baseline_built()
            task_update(tid, notes="published after one bus failure")
            with anyio.fail_after(2):
                while not any(e.uri == task_uri(tid) for e in published):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
        assert failures == 1

    @pytest.mark.anyio
    async def test_partial_baseline_failure_rebuilds_the_whole_baseline(self, monkeypatch):
        """Task #1958 review: a baseline is adopted whole or not at all.

        Reads wait for a successful baseline, so a retried baseline absorbing a
        commit hides nothing; a half-adopted one would diff liveness against
        nothing and drop its transitions.
        """
        snapshots = 0
        liveness_calls = 0
        original_snapshot = mcp_live_state.snapshot_with_cursor

        def counted_snapshot(*args):
            nonlocal snapshots
            snapshots += 1
            return original_snapshot(*args)

        def liveness(now=None):
            nonlocal liveness_calls
            liveness_calls += 1
            if liveness_calls == 1:
                raise OSError("liveness read failed during baseline")
            return {}

        class _Server:
            _subscriptions = None

        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", counted_snapshot)
        monkeypatch.setattr(mcp_live_state, "_agent_liveness_snapshot", liveness)
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            with anyio.fail_after(2):
                while liveness_calls < 2:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
        # The failed baseline was rebuilt from the table snapshot up.
        assert snapshots == 2

    @pytest.mark.anyio
    async def test_resource_reads_wait_for_the_startup_baseline(self, monkeypatch):
        """Task #1958 review: no read may predate the baseline it is diffed against."""
        original_snapshot = mcp_live_state.snapshot_with_cursor
        marks: list[str] = []
        reader_waiting = threading.Event()

        def held_snapshot(*args):
            # The baseline finishes only once a read is waiting on it.
            reader_waiting.wait(5)
            result = original_snapshot(*args)
            marks.append("baseline")
            return result

        class _Server:
            _subscriptions = None

        handler = mcp_live_state._handler_for(object_model.by_kind("board"))
        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", held_snapshot)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            ready = await _poller_started()
            real_wait = ready.wait

            def noted_wait(timeout=None):
                reader_waiting.set()
                return real_wait(timeout)

            ready.wait = noted_wait
            await anyio.to_thread.run_sync(handler)
            marks.append("read")
            tg.cancel_scope.cancel()
        assert marks[:2] == ["baseline", "read"]
        assert mcp_live_state._baseline_ready is None

    @pytest.mark.anyio
    async def test_failing_baseline_releases_resource_reads(self, monkeypatch):
        """Task #1958 steward review: a broken store must not hold every read 30 s."""

        def broken_snapshot(*args):
            raise OSError("store unreadable")

        class _Server:
            _subscriptions = None

        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", broken_snapshot)
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            ready = await _poller_started()
            assert not ready.is_set()
            with anyio.fail_after(2):
                while not ready.is_set():
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    @pytest.mark.anyio
    async def test_read_past_the_baseline_wait_fails_as_not_ready(self, monkeypatch):
        """Task #1962: a read must not silently predate a baseline still being built.

        ``ready.wait(BASELINE_WAIT_S)`` returning False used to fall through to
        the reader, so a commit after that read could be absorbed into the late
        baseline and never published. The read now fails as not ready, and a
        retry after the baseline lands succeeds.
        """
        original_snapshot = mcp_live_state.snapshot_with_cursor
        baseline_done = threading.Event()
        release = threading.Event()

        def delayed_snapshot(*args):
            # Held past BASELINE_WAIT_S until the test has seen the read fail.
            release.wait(5)
            result = original_snapshot(*args)
            baseline_done.set()
            return result

        class _Server:
            _subscriptions = None

        handler = mcp_live_state._handler_for(object_model.by_kind("board"))
        monkeypatch.setattr(mcp_live_state, "snapshot_with_cursor", delayed_snapshot)
        monkeypatch.setattr(mcp_live_state, "BASELINE_WAIT_S", 0.05)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            await _poller_started()
            with pytest.raises(ResourceError, match="not ready"):
                await anyio.to_thread.run_sync(handler)
            assert not baseline_done.is_set()
            release.set()
            with anyio.fail_after(5):
                while not mcp_live_state._baseline_ready.is_set():
                    await anyio.sleep(0.01)
            assert baseline_done.is_set()
            await anyio.to_thread.run_sync(handler)  # the retry reads normally
            tg.cancel_scope.cancel()

    @pytest.mark.anyio
    async def test_poller_publishes_agent_liveness_transition(self, monkeypatch):
        uri = "unified-agent://agents/Desktop_StatusTransition"
        states = iter(({uri: "online"}, {uri: "idle"}))
        published: list[ResourceUpdated] = []

        class _Bus:
            async def publish(self, event):
                published.append(event)

        class _Server:
            _subscriptions = _Bus()

        monkeypatch.setattr(mcp_live_state, "snapshot", lambda: {})
        monkeypatch.setattr(mcp_live_state, "_database_version", lambda conn: 1)
        monkeypatch.setattr(
            mcp_live_state,
            "_agent_liveness_snapshot",
            lambda now=None: next(states, {uri: "idle"}),
        )
        monkeypatch.setattr(mcp_live_state, "POLL_INTERVAL_S", 0.02)
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, _Server())
            with anyio.fail_after(1):
                while not any(event.uri == uri for event in published):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
        assert any(isinstance(event, ResourceUpdated) and event.uri == uri for event in published)

    @pytest.mark.anyio
    async def test_pi_run_task_progress_callback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        lines = [
            json.dumps({"type": "agent_start"}),
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "name": "read",
                    "tool_call_id": "call_1",
                    "arguments": {"path": "foo.py"},
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
        mock_proc.stdout = lines
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ("", "")
        reports: list[tuple[float, float | None, str | None]] = []

        async def on_progress(progress, total, message):
            reports.append((progress, total, message))

        with (
            patch("unified_agent.tools.pi_runner.subprocess.Popen", return_value=mock_proc),
            patch(
                "unified_agent.tools.pi_runner.resolve_pi_executable",
                return_value=["/usr/bin/pi"],
            ),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "pi_run_task",
                    {"prompt": "Say hello", "timeout_s": 10},
                    progress_callback=on_progress,
                )

        assert not result.is_error, result.content[0].text if result.content else result
        body = json.loads(result.content[0].text)
        assert body["status"] == "success"
        assert reports[0] == (0, 3, "starting Pi")
        assert (1, 3, "Pi spawned") in reports
        assert (2, 3, "first tool") in reports
        assert reports[-1] == (3, 3, "success")
        assert reports == sorted(reports, key=lambda item: item[0])


def test_run_resource_read_fail_closed_without_row():
    with pytest.raises(Exception, match="unknown dispatch run"):
        mcp_live_state.read_run("no-such-run")


def test_board_is_read_only_snapshot():
    created = task_add("board snapshot")
    payload = mcp_live_state.read_board()
    assert payload["uri"] == BOARD_URI
    assert any(row["id"] == created["id"] for row in payload["tasks"])
    assert payload["truncated"] is False
    assert payload["next_cursor"] is None
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] >= 1
    finally:
        conn.close()
