"""Platform readers for the served platform objects (task #1072).

First slice: approval and evidence events — the two objects a buyer's auditor
or compliance officer has to be able to *open*. Before that slice they were
reachable only by asking an agent to call ``approval_pending`` or
``event_log_query``. Second slice (this card): plan, delegation, agent,
session, and memory_note — every remaining declared type now resolves a real
reader.

The tests deliberately check the undeceptive properties, not just the happy path:
an audit reader must not rewrite what it read, must not leak whether an id was
malformed or merely absent, and must be held to the same profile gate the
declaration advertises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError
from org_test_helpers import installation_org as installation_org

from unified_agent import mcp_live_state, object_model, platform_readers, profiles, storage

_ULID = "01J8ZQ4T7X9K2M5N7P8R0S3T6V"


def _insert_approval(*, status: str = "pending", tool: str = "run_cmd") -> int:
    conn = storage.connect()
    try:
        cursor = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                tool,
                '{"command": "echo hi"}',
                "Desktop_Agent1",
                status,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_event(*, payload: str = '{"k":"v"}', ulid: str = _ULID) -> str:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, created_at, prev_hash, entry_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ulid,
                "agent.task.updated",
                payload,
                "Desktop_Agent5",
                "2026-09-17T00:00:00Z",
                "prev-deadbeef",
                "entry-cafebabe",
            ),
        )
        conn.commit()
        return ulid
    finally:
        conn.close()


def _insert_plan(*, plan_id: str = "plan-abc123", status: str = "active") -> str:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plans (id, goal, scope, status, author, escalated,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                "ship the platform readers",
                "default",
                status,
                "Desktop_Agent1",
                1 if status == "active" else 0,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        return plan_id
    finally:
        conn.close()


def _insert_delegation(*, status: str = "pending") -> int:
    task = _insert_task(title="delegate me")
    conn = storage.connect()
    try:
        cursor = conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec,"
            " deadline, callback, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Desktop_Agent1",
                "Desktop_agent3",
                task,
                "one-line task_spec",
                None,
                "none",
                status,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_task(*, title: str = "task") -> int:
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


def _insert_agent(*, label: str = "Desktop_ReaderTest") -> str:
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


def _insert_session(*, session_id: str = _ULID, status: str = "running") -> str:
    # read_session reaps a 'running' session whose heartbeat is older than
    # UA_SESSION_ABANDON_MINUTES (#855), so the fixture heartbeats now.
    heartbeat = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at,"
            " last_heartbeat_at, task_id, meta) VALUES (?, ?, ?, ?, ?, ?, NULL, '{}')",
            (
                session_id,
                "Desktop_ReaderTest",
                "cli",
                status,
                "2026-09-17T00:00:00Z",
                heartbeat,
            ),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def _insert_memory_note(*, memory_id: str = "note-abc123") -> str:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, author, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                "reader test",
                "hello from the reader",
                '["project:unified-agent"]',
                "Desktop_Agent1",
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        return memory_id
    finally:
        conn.close()


class TestApprovalReader:
    def test_returns_the_stored_row(self):
        approval_id = _insert_approval()
        result = platform_readers.read_approval(str(approval_id))
        assert result["uri"] == f"unified-agent://approvals/{approval_id}"
        row = result["approval"]
        assert row["id"] == approval_id
        assert row["tool"] == "run_cmd"
        assert row["status"] == "pending"
        assert row["requested_by"] == "Desktop_Agent1"
        # Decoded by tools.approvals.get_approval, which is the domain's own
        # representation of an approval. Contrast TestEvidenceReader, where the
        # raw column must survive the read untouched.
        assert row["payload"] == {"command": "echo hi"}

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown approval"):
            platform_readers.read_approval("999999")

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "1.5", ""])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        """A reader must not reveal whether an id was malformed or absent."""
        with pytest.raises(ResourceNotFoundError, match="unknown approval"):
            platform_readers.read_approval(bad)

    def test_resolved_approval_is_readable(self):
        approval_id = _insert_approval(status="consumed")
        assert platform_readers.read_approval(str(approval_id))["approval"]["status"] == "consumed"


class TestEvidenceReader:
    def test_returns_the_chain_links(self):
        _insert_event()
        result = platform_readers.read_evidence_event(_ULID)
        assert result["uri"] == f"unified-agent://evidence/{_ULID}"
        record = result["evidence_event"]
        assert record["ulid"] == _ULID
        assert record["channel"] == "agent.task.updated"
        # A verifier needs both links to check an entry against its neighbours.
        assert record["prev_hash"] == "prev-deadbeef"
        assert record["entry_hash"] == "entry-cafebabe"

    def test_payload_is_never_rewritten(self):
        original = '{  "a" : 1 , "b" : [2,3] }'
        _insert_event(payload=original)
        record = platform_readers.read_evidence_event(_ULID)["evidence_event"]
        assert record["payload"] == original
        assert record["payload_parsed"] == {"a": 1, "b": [2, 3]}

    def test_non_json_payload_yields_none_and_keeps_the_raw_value(self):
        _insert_event(payload="not json at all")
        record = platform_readers.read_evidence_event(_ULID)["evidence_event"]
        assert record["payload"] == "not json at all"
        assert record["payload_parsed"] is None

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown evidence event"):
            platform_readers.read_evidence_event(_ULID)

    @pytest.mark.parametrize("bad", ["short", "has/slash", "has space", "", "x" * 65])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown evidence event"):
            platform_readers.read_evidence_event(bad)


class TestPlanReader:
    def test_returns_the_stored_row(self):
        _insert_plan()
        result = platform_readers.read_plan("plan-abc123")
        assert result["uri"] == "unified-agent://plans/plan-abc123"
        row = result["plan"]
        assert row["id"] == "plan-abc123"
        assert row["goal"] == "ship the platform readers"
        assert row["status"] == "active"
        assert row["author"] == "Desktop_Agent1"
        # Derived alias of escalated, matching plan_get's representation.
        assert row["needs_human"] is True

    def test_non_escalated_plan_reports_needs_human_false(self):
        _insert_plan(status="blocked")
        row = platform_readers.read_plan("plan-abc123")["plan"]
        assert row["needs_human"] is False
        assert row["status"] == "blocked"

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown plan"):
            platform_readers.read_plan("plan-missing")

    @pytest.mark.parametrize("bad", ["", " ", "plan with space", "has/slash", ".."])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown plan"):
            platform_readers.read_plan(bad)


class TestDelegationReader:
    def test_returns_the_stored_row(self):
        delegation_id = _insert_delegation()
        result = platform_readers.read_delegation(str(delegation_id))
        assert result["uri"] == f"unified-agent://delegations/{delegation_id}"
        row = result["delegation"]
        assert row["id"] == delegation_id
        assert row["from_agent"] == "Desktop_Agent1"
        assert row["to_agent"] == "Desktop_agent3"
        assert row["status"] == "pending"
        assert row["callback"] == "none"
        assert row["task_spec"] == "one-line task_spec"

    def test_completed_delegation_is_readable(self):
        delegation_id = _insert_delegation(status="completed")
        assert (
            platform_readers.read_delegation(str(delegation_id))["delegation"]["status"]
            == "completed"
        )

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown delegation"):
            platform_readers.read_delegation("999999")

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "1.5", ""])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown delegation"):
            platform_readers.read_delegation(bad)


class TestAgentReader:
    def test_returns_the_stored_row(self):
        _insert_agent()
        result = platform_readers.read_agent("Desktop_ReaderTest")
        assert result["uri"] == "unified-agent://agents/Desktop_ReaderTest"
        row = result["agent"]
        assert row["label"] == "Desktop_ReaderTest"
        assert row["kind"] == "cli"
        assert row["host"] == "desktop"
        assert row["known"] is True
        # Status is derived at read time, never stored.
        assert row["status"] in {"online", "idle", "stale", "offline", "unknown"}
        assert row["meta"] == {}

    def test_missing_label_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown agent"):
            platform_readers.read_agent("Desktop_NeverHeardOf")

    @pytest.mark.parametrize("bad", ["", "space in label", "has/slash", "too-long-" * 10])
    def test_malformed_label_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown agent"):
            platform_readers.read_agent(bad)


class TestSessionReader:
    def test_returns_the_stored_row(self):
        _insert_session()
        result = platform_readers.read_session(_ULID)
        assert result["uri"] == f"unified-agent://sessions/{_ULID}"
        row = result["session"]
        assert row["id"] == _ULID
        assert row["agent"] == "Desktop_ReaderTest"
        assert row["kind"] == "cli"
        assert row["status"] == "running"
        # Joined-task fields are null when no task is attached, never missing.
        assert row["task_id"] is None
        assert row["meta"] == {}

    def test_completed_session_is_readable(self):
        _insert_session(status="completed")
        assert platform_readers.read_session(_ULID)["session"]["status"] == "completed"

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown session"):
            platform_readers.read_session("01J8ZQ4T7X9K2M5N7P8R0S3T6W")

    @pytest.mark.parametrize("bad", ["", " ", "has/slash", "x" * 200])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown session"):
            platform_readers.read_session(bad)


class TestMemoryNoteReader:
    def test_returns_the_stored_row(self):
        _insert_memory_note()
        result = platform_readers.read_memory_note("note-abc123")
        assert result["uri"] == "unified-agent://memory/note-abc123"
        row = result["memory_note"]
        assert row["id"] == "note-abc123"
        assert row["content"] == "hello from the reader"
        assert row["author"] == "Desktop_Agent1"
        # The tags column is stored raw; the reader never rewrites it.
        assert row["tags"] == '["project:unified-agent"]'

    def test_missing_id_is_not_found(self):
        with pytest.raises(ResourceNotFoundError, match="unknown memory note"):
            platform_readers.read_memory_note("note-missing")

    @pytest.mark.parametrize("bad", ["", "has space", "has/slash", "x" * 65])
    def test_malformed_id_is_indistinguishable_from_missing(self, bad):
        with pytest.raises(ResourceNotFoundError, match="unknown memory note"):
            platform_readers.read_memory_note(bad)


class TestGate:
    def test_approval_reader_is_gated(self):
        with patch.object(profiles, "tool_is_allowed", return_value=False):
            with pytest.raises(ResourceError, match="not exposed by the active"):
                platform_readers.read_approval("1")

    def test_evidence_reader_is_gated(self):
        with patch.object(profiles, "tool_is_allowed", return_value=False):
            with pytest.raises(ResourceError, match="not exposed by the active"):
                platform_readers.read_evidence_event(_ULID)

    @pytest.mark.parametrize(
        ("reader", "argument"),
        [
            (platform_readers.read_plan, "plan-abc123"),
            (platform_readers.read_delegation, "1"),
            (platform_readers.read_agent, "Desktop_ReaderTest"),
            (platform_readers.read_session, _ULID),
            (platform_readers.read_memory_note, "note-abc123"),
        ],
    )
    def test_remaining_readers_are_gated(self, reader, argument):
        with patch.object(profiles, "tool_is_allowed", return_value=False):
            with pytest.raises(ResourceError, match="not exposed by the active"):
                reader(argument)

    @pytest.mark.parametrize(
        ("reader", "kind", "argument"),
        [
            (platform_readers.read_approval, "approval", "1"),
            (platform_readers.read_evidence_event, "evidence_event", _ULID),
            (platform_readers.read_plan, "plan", "plan-abc123"),
            (platform_readers.read_delegation, "delegation", "1"),
            (platform_readers.read_agent, "agent", "Desktop_ReaderTest"),
            (platform_readers.read_session, "session", _ULID),
            (platform_readers.read_memory_note, "memory_note", "note-abc123"),
        ],
    )
    def test_the_enforced_gate_is_the_declared_one(self, reader, kind, argument):
        """The reader must consult the tool the declaration advertises."""
        with patch.object(profiles, "tool_is_allowed", return_value=True) as allowed:
            with pytest.raises(ResourceNotFoundError):
                reader(argument)
        allowed.assert_called_once_with(object_model.by_kind(kind).profile_tool)

    def test_deny_message_matches_the_legacy_mcp_adapter(self):
        """One wording, two modules — pinned so they cannot drift apart."""
        assert platform_readers.PROFILE_DENIED_MESSAGE == mcp_live_state._PROFILE_DENIED


class TestRegistryWiring:
    @pytest.mark.parametrize(
        ("kind", "expected_reader"),
        [
            ("approval", "read_approval"),
            ("evidence_event", "read_evidence_event"),
            ("plan", "read_plan"),
            ("delegation", "read_delegation"),
            ("agent", "read_agent"),
            ("session", "read_session"),
            ("memory_note", "read_memory_note"),
            ("org_unit", "read_org_unit"),
        ],
    )
    def test_object_type_resolves_to_the_platform_reader(self, kind, expected_reader):
        object_type = object_model.by_kind(kind)
        assert object_type.served is True
        assert object_type.reader_path == f"unified_agent.platform_readers:{expected_reader}"
        assert object_type.reader() is getattr(platform_readers, expected_reader)

    def test_all_remaining_types_are_declared_as_served(self):
        served = [object_type.kind for object_type in object_model.served_types()]
        for kind in ("plan", "delegation", "agent", "session", "memory_note"):
            assert kind in served

    def test_registry_stays_sound(self):
        assert object_model.validate_registry() == []

    def test_org_unit_reader_exposes_configured_charter_under_task_gate(self, installation_org):
        result = platform_readers.read_org_unit("platform-engineering")
        assert result["uri"] == "unified-agent://org-units/platform-engineering"
        assert result["org_unit"]["lead"] == "Desktop_Agent2"
        assert "Desktop_Agent2" in result["org_unit"]["members"]
        assert result["org_unit"]["decision_rights"]
        with pytest.raises(ResourceNotFoundError, match="unknown org unit"):
            platform_readers.read_org_unit("missing-office")
        with patch.object(profiles, "tool_is_allowed", return_value=False):
            with pytest.raises(ResourceError, match="not exposed by the active"):
                platform_readers.read_org_unit("platform-engineering")

    def test_reader_output_is_json_serializable(self):
        """The payload is a wire artifact, not a Python object graph."""
        approval_id = _insert_approval()
        _insert_event()
        plan_id = _insert_plan()
        delegation_id = _insert_delegation()
        _insert_agent()
        _insert_session()
        memory_id = _insert_memory_note()
        assert json.loads(json.dumps(platform_readers.read_approval(str(approval_id))))
        assert json.loads(json.dumps(platform_readers.read_evidence_event(_ULID)))
        assert json.loads(json.dumps(platform_readers.read_plan(plan_id)))
        assert json.loads(json.dumps(platform_readers.read_delegation(str(delegation_id))))
        assert json.loads(json.dumps(platform_readers.read_agent("Desktop_ReaderTest")))
        assert json.loads(json.dumps(platform_readers.read_session(_ULID)))
        assert json.loads(json.dumps(platform_readers.read_memory_note(memory_id)))

    def test_platform_readers_do_not_import_the_mcp_adapter(self):
        """The point of the module: an adapter layer calls the platform, not the
        other way round. Checked on the source, since an import that happens to be
        satisfied by an already-loaded module would not show up at runtime.

        Note this does NOT assert independence from the MCP *SDK*: the readers
        raise ``ResourceError``/``ResourceNotFoundError`` so they honour the same
        contract the legacy resources already publish. A future HTTP adapter
        should map those to its own status codes rather than reusing them.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path(platform_readers.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not [name for name in imported if "mcp_live_state" in name], imported
