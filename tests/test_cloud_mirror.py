"""Cloud mirror shipper (task #1285): projection, cursor, dedupe-safety, sink failure.

Entirely offline. `HttpSink` tests inject an `httpx.Client(transport=httpx.MockTransport(...))`
so no real network call is ever made -- see `test_http_sink_failure_never_raises_and_holds_cursor`.
"""

from __future__ import annotations

import json
import stat

import httpx
import pytest

from unified_agent import cloud_mirror, events, storage


@pytest.fixture(autouse=True)
def _init_db():
    storage.init_db()


def _record_event(channel: str, payload: dict, originator: str = "Desktop_Agent1") -> str:
    return events.record_event(channel, payload=payload, originator=originator, notify=False)


# --------------------------------------------------------------------------
# Projection: allowlist enforcement
# --------------------------------------------------------------------------


def test_projection_drops_non_allowlisted_channel():
    raw = {
        "ulid": "01AAAA",
        "channel": "agent.journal.appended",
        "payload": {"kind": "journal", "ref_id": "1", "summary": "some private journal text"},
        "originator": "Desktop_Agent1",
        "created_at": "2026-09-23T00:00:00Z",
    }
    assert cloud_mirror.project_event(raw) is None


def test_projection_drops_message_channel():
    raw = {
        "ulid": "01AAAB",
        "channel": "agent.message.sent",
        "payload": {"body": "hello there, this is a private message"},
        "originator": "Desktop_Agent1",
        "created_at": "2026-09-23T00:00:00Z",
    }
    assert cloud_mirror.project_event(raw) is None


def test_projection_never_leaks_summary_text():
    """The whole point: sensitive text embedded in a task title must never appear anywhere."""
    marker = "canary token that must never leave the box 93f2"
    raw = {
        "ulid": "01AAAC",
        "channel": cloud_mirror.CHANNEL_TASK_UPDATED,
        "payload": {
            "kind": "task",
            "ref_id": "42",
            "summary": f"created (high): fix the {marker} leak",
        },
        "originator": "Desktop_Agent1",
        "created_at": "2026-09-23T00:00:00Z",
    }
    record = cloud_mirror.project_event(raw)
    assert record is not None
    dumped = json.dumps(record)
    assert marker not in dumped
    assert "summary" not in record
    assert record["task_id"] == "42"
    assert record["status"] is None


def test_projection_only_emits_allowed_keys():
    raw = {
        "ulid": "01AAAD",
        "channel": cloud_mirror.CHANNEL_TASK_UPDATED,
        "payload": {
            "kind": "task",
            "ref_id": "7",
            "summary": "status open->doing: secret title text",
        },
        "originator": "Desktop_Agent1",
        "episode_id": "ep-1",
        "causal_parent_id": None,
        "created_at": "2026-09-23T00:00:01Z",
        "dedupe_key": None,
    }
    record = cloud_mirror.project_event(raw)
    assert set(record) == {
        "schema_version",
        "event_id",
        "channel",
        "kind",
        "agent",
        "task_id",
        "status",
        "timestamp",
    }
    assert record["event_id"] == "01AAAD"
    assert record["schema_version"] == cloud_mirror.SCHEMA_VERSION


def test_projection_cycle_channel_extracts_structured_status():
    raw = {
        "ulid": "01AAAE",
        "channel": cloud_mirror.CHANNEL_CYCLE_UPDATED,
        "payload": {
            "cycle": {"id": 9, "delegation_id": "del-9", "state": "running"},
            "transition": {
                "to_state": "verifying",
                "reason": "operator note with a secret token abc123",
            },
            "action": "transition",
        },
        "originator": "server_agent1",
        "created_at": "2026-09-23T00:00:02Z",
    }
    record = cloud_mirror.project_event(raw)
    assert record["kind"] == "cycle"
    assert record["task_id"] == "del-9"
    assert record["status"] == "verifying"
    assert "secret token abc123" not in json.dumps(record)


def test_projection_approval_elicitation_extracts_action_as_status():
    raw = {
        "ulid": "01AAAF",
        "channel": cloud_mirror.CHANNEL_APPROVAL_ELICITATION,
        "payload": {
            "action": "requested",
            "approval_id": 5,
            "tool": "fs_write",
            "path": "/private/etc/some/secret/path.txt",
        },
        "originator": "Desktop_Agent2",
        "created_at": "2026-09-23T00:00:03Z",
    }
    record = cloud_mirror.project_event(raw)
    assert record["kind"] == "approval"
    assert record["status"] == "requested"
    assert record["task_id"] == "5"
    assert "/private/etc/some/secret/path.txt" not in json.dumps(record)


def test_projection_project_built():
    raw = {
        "ulid": "01AAAG",
        "channel": cloud_mirror.CHANNEL_PROJECT_BUILT,
        "payload": {"parent_task_id": 100, "project_id": 100, "identity": "Desktop_Agent1"},
        "originator": "Desktop_Agent1",
        "created_at": "2026-09-23T00:00:04Z",
    }
    record = cloud_mirror.project_event(raw)
    assert record["kind"] == "build"
    assert record["task_id"] == "100"
    assert record["status"] == "built"


def test_projection_approval_elicitation_drops_unrecognized_action():
    raw = {
        "ulid": "01AAAF",
        "channel": cloud_mirror.CHANNEL_APPROVAL_ELICITATION,
        "payload": {"action": "operator supplied note", "approval_id": 5},
        "originator": "Desktop_Agent2",
        "created_at": "2026-09-23T00:00:03Z",
    }
    record = cloud_mirror.project_event(raw)
    assert record["kind"] == "approval"
    assert record["status"] is None
    assert record["task_id"] == "5"


# --------------------------------------------------------------------------
# Cursor persistence + no-advance-on-failure
# --------------------------------------------------------------------------


class _RecordingSink:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.batches: list[list[dict]] = []

    def send(self, records):
        if self.fail:
            raise RuntimeError("simulated sink outage")
        self.batches.append(records)


def test_run_once_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_CLOUD_MIRROR_ENABLED", raising=False)
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "x"}
    )
    sink = _RecordingSink()
    result = cloud_mirror.run_once(sink=sink, cursor_path=tmp_path / "cursor.json")
    assert result["enabled"] is False
    assert result["shipped"] == 0
    assert sink.batches == []


def test_run_once_ships_allowlisted_events_and_advances_cursor(tmp_path):
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED,
        {"kind": "task", "ref_id": "1", "summary": "hidden title"},
    )
    _record_event("agent.message.sent", {"body": "should never ship"})
    _record_event(cloud_mirror.CHANNEL_PROJECT_BUILT, {"parent_task_id": 1, "project_id": 1})

    cursor_path = tmp_path / "cursor.json"
    sink = _RecordingSink()
    result = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)

    assert result["shipped"] == 2  # message.sent excluded
    assert cloud_mirror.read_cursor(cursor_path) == result["cursor"]
    shipped_channels = {r["channel"] for batch in sink.batches for r in batch}
    assert shipped_channels == {
        cloud_mirror.CHANNEL_TASK_UPDATED,
        cloud_mirror.CHANNEL_PROJECT_BUILT,
    }
    assert all("hidden title" not in json.dumps(r) for batch in sink.batches for r in batch)


@pytest.mark.parametrize(
    "contents", ["{", "[]", '{"cursor": null}', '{"cursor": 42}', '{"cursor": ""}']
)
def test_run_once_fails_closed_on_corrupt_cursor_without_replay(monkeypatch, tmp_path, contents):
    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(contents, encoding="utf-8")
    queried = False

    def unexpected_query(**kwargs):
        nonlocal queried
        queried = True
        raise AssertionError("must not replay history with an unreadable cursor")

    monkeypatch.setattr(cloud_mirror.events, "query_events", unexpected_query)
    sink = _RecordingSink()
    result = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)

    assert result == {
        "enabled": True,
        "shipped": 0,
        "cursor": None,
        "error": "unreadable cursor",
    }
    assert queried is False
    assert sink.batches == []


def test_run_once_does_not_advance_cursor_on_sink_failure(tmp_path):
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "t"}
    )
    cursor_path = tmp_path / "cursor.json"

    failing_sink = _RecordingSink(fail=True)
    result = cloud_mirror.run_once(sink=failing_sink, cursor_path=cursor_path, force=True)
    assert result["shipped"] == 0
    assert "error" in result
    assert cloud_mirror.read_cursor(cursor_path) is None  # unchanged: still nothing persisted

    # Retry with a working sink must see the SAME event again (cursor held).
    working_sink = _RecordingSink()
    result2 = cloud_mirror.run_once(sink=working_sink, cursor_path=cursor_path, force=True)
    assert result2["shipped"] == 1
    assert cloud_mirror.read_cursor(cursor_path) == result2["cursor"]


def test_run_once_second_call_ships_nothing_new(tmp_path):
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "t"}
    )
    cursor_path = tmp_path / "cursor.json"
    sink = _RecordingSink()
    first = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert first["shipped"] == 1
    second = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert second["shipped"] == 0
    assert second["cursor"] == first["cursor"]


# --------------------------------------------------------------------------
# Structured status enrichment (batched, read-only, "as of ship time")
# --------------------------------------------------------------------------


def test_run_once_enriches_task_status_from_storage_at_ship_time(tmp_path):
    from unified_agent.tools.tasks import task_add, task_update

    task = task_add("mirror status test task")
    task_update(task["id"], status="doing", actor="Desktop_Agent1")

    cursor_path = tmp_path / "cursor.json"
    sink = _RecordingSink()
    result = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert result["shipped"] >= 1

    task_records = [
        r
        for batch in sink.batches
        for r in batch
        if r["kind"] == "task" and r["task_id"] == str(task["id"])
    ]
    assert task_records, "expected at least one projected agent.task.updated record"
    # Every task-kind record for this id reflects the CURRENT status ("doing"),
    # not whatever historical transition produced that particular event --
    # this is "status as of ship time", per docs/cloud-mirror.md.
    assert all(r["status"] == "doing" for r in task_records)


def test_run_once_enriches_approval_status_from_storage_at_ship_time(tmp_path):
    from unified_agent.tools.approvals import approval_resolve, check_approval_gate

    gate = check_approval_gate(
        "fake_gated_tool",
        {"path": "/tmp/x"},
        requested_by="Desktop_Agent1",
        force=True,
        request_only=True,
    )
    approval_id = gate["approval_id"]
    approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")

    cursor_path = tmp_path / "cursor.json"
    sink = _RecordingSink()
    result = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert result["shipped"] >= 1

    approval_records = [
        r
        for batch in sink.batches
        for r in batch
        if r["kind"] == "approval" and r["task_id"] == str(approval_id)
    ]
    assert approval_records, "expected at least one projected agent.approval.requested record"
    assert all(r["status"] == "approved" for r in approval_records)


def test_status_enrichment_rejects_values_outside_known_enum(monkeypatch, tmp_path):
    """A status value that isn't in the known enum is dropped, not passed through."""
    from unified_agent.tools.tasks import task_add

    task = task_add("enum guard test task")

    class _FakeCursor:
        def fetchall(self):
            return [{"id": task["id"], "status": "not-a-real-status"}]

    class _FakeConn:
        def execute(self, sql, params):
            return _FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(cloud_mirror.storage, "connect", lambda: _FakeConn())

    records = [
        {
            "schema_version": 1,
            "event_id": "e1",
            "channel": cloud_mirror.CHANNEL_TASK_UPDATED,
            "kind": "task",
            "agent": "Desktop_Agent1",
            "task_id": str(task["id"]),
            "status": None,
            "timestamp": "2026-09-23T00:00:00Z",
        }
    ]
    cloud_mirror._enrich_structured_statuses(records)
    assert records[0]["status"] is None


def test_status_enrichment_preserves_elicitation_actions_without_approval_rows(monkeypatch):
    class _FakeCursor:
        def fetchall(self):
            return [{"id": 7, "status": "approved"}]

    class _FakeConn:
        def execute(self, sql, params):
            assert sql.startswith("SELECT id, status FROM approvals")
            assert params == [7, 99]
            return _FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(cloud_mirror.storage, "connect", lambda: _FakeConn())
    records = [
        {
            "kind": "approval",
            "channel": cloud_mirror.CHANNEL_APPROVAL_ELICITATION,
            "task_id": "99",
            "status": "requested",
        },
        {
            "kind": "approval",
            "channel": cloud_mirror.CHANNEL_APPROVAL_ELICITATION,
            "task_id": "7",
            "status": "approved",
        },
        {
            "kind": "approval",
            "channel": cloud_mirror.CHANNEL_APPROVAL_REQUESTED,
            "task_id": "7",
            "status": None,
        },
    ]

    cloud_mirror._enrich_structured_statuses(records)

    assert [record["status"] for record in records] == ["requested", "approved", "approved"]


def test_status_enrichment_falls_back_to_null_when_storage_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("simulated: storage unavailable")

    monkeypatch.setattr(cloud_mirror.storage, "connect", _boom)

    records = [
        {
            "schema_version": 1,
            "event_id": "e1",
            "channel": cloud_mirror.CHANNEL_TASK_UPDATED,
            "kind": "task",
            "agent": "Desktop_Agent1",
            "task_id": "1",
            "status": None,
            "timestamp": "2026-09-23T00:00:00Z",
        }
    ]
    cloud_mirror._enrich_structured_statuses(records)  # must not raise
    assert records[0]["status"] is None


def test_status_enrichment_is_one_batched_query_per_table_not_per_event(monkeypatch, tmp_path):
    from unified_agent.tools.tasks import task_add, task_update

    tasks = [task_add(f"batch query test task {i}") for i in range(3)]
    for t in tasks:
        task_update(t["id"], status="doing", actor="Desktop_Agent1")

    call_count = {"n": 0}
    real_connect = cloud_mirror.storage.connect

    class _CountingConn:
        """sqlite3.Connection forbids setting `.execute` directly -- wrap instead.

        This patches storage.connect() globally for the run_once() call, which
        other code paths (events.query_events, etc.) also use -- so everything
        besides `execute` is proxied straight through to the real connection
        rather than reimplemented.
        """

        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("SELECT ID, STATUS FROM TASKS"):
                call_count["n"] += 1
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(cloud_mirror.storage, "connect", lambda: _CountingConn(real_connect()))

    cursor_path = tmp_path / "cursor.json"
    sink = _RecordingSink()
    cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert call_count["n"] == 1


# --------------------------------------------------------------------------
# FileSink (JSONL)
# --------------------------------------------------------------------------


def test_file_sink_appends_jsonl(tmp_path):
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "t"}
    )
    out_path = tmp_path / "mirror.jsonl"
    sink = cloud_mirror.FileSink(out_path)
    result = cloud_mirror.run_once(sink=sink, cursor_path=tmp_path / "cursor.json", force=True)
    assert result["shipped"] == 1
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["channel"] == cloud_mirror.CHANNEL_TASK_UPDATED


def test_file_sink_creates_private_storage_without_chmodding_existing_parents(tmp_path):
    tmp_path.chmod(0o755)
    existing_parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    out_path = tmp_path / "private" / "nested" / "mirror.jsonl"
    cloud_mirror.FileSink(out_path).send([{"event_id": "e1"}])

    assert stat.S_IMODE(tmp_path.stat().st_mode) == existing_parent_mode
    assert stat.S_IMODE(out_path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(out_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600


def test_file_sink_repairs_partial_tail_before_retry(tmp_path):
    out_path = tmp_path / "mirror.jsonl"
    out_path.write_bytes(b'{"event_id":"old"}\n{"event_id":"new"')

    cloud_mirror.FileSink(out_path).send([{"event_id": "new", "kind": "task"}])

    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert [record["event_id"] for record in records] == ["old", "new"]


def test_file_sink_rolls_back_partial_append_before_retry(monkeypatch, tmp_path):
    out_path = tmp_path / "mirror.jsonl"
    sink = cloud_mirror.FileSink(out_path)
    sink.send([{"event_id": "old"}])
    real_write = cloud_mirror.os.write
    calls = 0

    def partial_then_fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:5])
        raise OSError("simulated interrupted append")

    monkeypatch.setattr(cloud_mirror.os, "write", partial_then_fail)
    with pytest.raises(OSError, match="interrupted append"):
        sink.send([{"event_id": "new"}])
    monkeypatch.setattr(cloud_mirror.os, "write", real_write)

    sink.send([{"event_id": "new"}])
    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert [record["event_id"] for record in records] == ["old", "new"]


def test_file_sink_dedupes_retries_after_cursor_write_failure(monkeypatch, tmp_path):
    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "t"}
    )
    out_path = tmp_path / "mirror.jsonl"
    cursor_path = tmp_path / "cursor.json"
    sink = cloud_mirror.FileSink(out_path)
    monkeypatch.setattr(
        cloud_mirror,
        "write_cursor",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    first = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    second = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)

    assert "error" in first and "error" in second
    assert first["cursor"] is None and second["cursor"] is None
    assert len(out_path.read_text(encoding="utf-8").splitlines()) == 1


def test_default_sink_rejects_unknown_kind_without_falling_back(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_CLOUD_MIRROR_SINK", "htttp")
    monkeypatch.setenv("UA_CLOUD_MIRROR_FILE_PATH", str(tmp_path / "fallback.jsonl"))
    assert cloud_mirror._default_sink() is None


def test_default_file_sink_must_remain_under_ua_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_CLOUD_MIRROR_FILE_PATH", str(tmp_path / "outside.jsonl"))
    assert cloud_mirror._default_sink() is None

    monkeypatch.setenv("UA_CLOUD_MIRROR_FILE_PATH", "mirrors/events.jsonl")
    sink = cloud_mirror._default_sink()
    assert isinstance(sink, cloud_mirror.FileSink)
    assert sink.path == home / "mirrors" / "events.jsonl"


@pytest.mark.parametrize(
    "url",
    [
        "http://mirror.example.invalid/ingest",
        "http://localhost/ingest",
        "http://127.0.0.2/ingest",
        "http://127.0.0.1/ingest" + "?token=" + "query-secret",
        "http://127.0.0.1/ingest#fragment",
        "http://" + "user" + ":" + "pass" + "@127.0.0.1/ingest",
        "https://" + "user" + ":" + "pass" + "@mirror.example.invalid/ingest",
        "https://mirror.example.invalid/ingest" + "?token=" + "query-secret",
    ],
)
def test_http_sink_rejects_insecure_or_credentialed_url(url):
    with pytest.raises(ValueError, match="HTTPS or literal http://127.0.0.1"):
        cloud_mirror.HttpSink(url)


# --------------------------------------------------------------------------
# HttpSink: fake transport, no real network, hub-unaffected on unreachable target
# --------------------------------------------------------------------------


def test_http_sink_success_uses_bearer_token_from_named_env_var(monkeypatch):
    monkeypatch.setenv("UA_CLOUD_MIRROR_INGEST_TOKEN", "test-token-123")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ingested": 1, "duplicates": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = cloud_mirror.HttpSink("https://mirror.example.invalid/ingest", client=client)
    sink.send(
        [{"schema_version": 1, "event_id": "e1", "channel": "agent.task.updated", "timestamp": "t"}]
    )
    assert seen["auth"] == "Bearer test-token-123"
    assert seen["body"]["records"][0]["event_id"] == "e1"


def test_http_sink_allows_literal_loopback_http_for_iap_tunnel(monkeypatch):
    monkeypatch.setenv("UA_CLOUD_MIRROR_INGEST_TOKEN", "test-token-123")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ingested": 1, "duplicates": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = cloud_mirror.HttpSink("http://127.0.0.1:8765/ingest", client=client)
    sink.send([{"schema_version": 1, "event_id": "e1", "channel": "x", "timestamp": "t"}])
    assert seen["url"] == "http://127.0.0.1:8765/ingest"
    assert seen["auth"] == "Bearer test-token-123"


@pytest.mark.parametrize(
    ("url", "trust_env"),
    [
        ("http://127.0.0.1:8765/ingest", False),
        ("https://mirror.example.invalid/ingest", True),
    ],
)
def test_http_sink_uses_proxy_environment_only_for_https(monkeypatch, url, trust_env):
    captured = {}
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: captured.update(kwargs) or "client",
    )

    sink = cloud_mirror.HttpSink(url)
    assert sink._make_client() == "client"
    assert captured["trust_env"] is trust_env


def test_http_sink_failure_never_raises_and_holds_cursor(monkeypatch, tmp_path):
    """Simulates an unreachable receiver with a fake transport -- no real network."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated: target unreachable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = cloud_mirror.HttpSink("https://mirror.example.invalid/ingest", client=client)

    _record_event(
        cloud_mirror.CHANNEL_TASK_UPDATED, {"kind": "task", "ref_id": "1", "summary": "t"}
    )
    cursor_path = tmp_path / "cursor.json"
    result = cloud_mirror.run_once(sink=sink, cursor_path=cursor_path, force=True)
    assert result["shipped"] == 0
    assert "error" in result
    assert cloud_mirror.read_cursor(cursor_path) is None


def test_http_sink_raises_runtime_error_on_http_status_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = cloud_mirror.HttpSink("https://mirror.example.invalid/ingest", client=client)
    with pytest.raises(RuntimeError):
        sink.send([{"schema_version": 1, "event_id": "e1", "channel": "x", "timestamp": "t"}])
