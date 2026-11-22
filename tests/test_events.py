"""Swarm Event Bus (task #157): the append-only event_log and the local pub/sub socket.

The point of this layer is that a consumer can be *told* something happened
instead of polling for it — and can reconnect without silently missing what it
slept through. Both halves are asserted here against a real Unix socket and a
real SQLite event_log; a mocked socket proves nothing about framing, partial
reads, or a subscriber that vanishes mid-stream.

The split that matters: the durable append is NOT best-effort (task #156 will
fail closed on "no episode record"), while the socket fan-out IS. Tests below
pin both directions.
"""

import os
import socket
import sqlite3
import stat

import pytest

from unified_agent import events, storage
from unified_agent.tools.events import event_log_query

# Generous enough not to flake under full-suite load, short enough that a
# genuinely-not-delivered event fails the test rather than hanging it.
DELIVERY_TIMEOUT = 5.0
# Used when asserting that NOTHING arrives. A real push lands in single-digit
# milliseconds, so this is ~2 orders of magnitude of headroom.
SILENCE_TIMEOUT = 1.0


@pytest.fixture
def bus(tmp_path):
    """A started EventBusServer on a tmp socket, stopped on teardown."""
    sock_path = tmp_path / "events.sock"
    os.environ["UA_EVENT_SOCKET"] = str(sock_path)
    server = events.EventBusServer(socket_path=sock_path)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        os.environ.pop("UA_EVENT_SOCKET", None)


# --- ULID ordering -----------------------------------------------------------


def test_ulid_is_lexicographically_monotonic_within_one_millisecond():
    """Same-millisecond ULIDs must still sort in generation order.

    A random-suffix ULID collides on ordering inside a millisecond, which is
    exactly the resolution a burst of events arrives at. If this is not
    monotonic, replay-from-cursor silently drops or repeats events.
    """
    ulids = [events.new_ulid(ts_ms=1_800_000_000_000) for _ in range(200)]
    assert len(set(ulids)) == len(ulids), "duplicate ULIDs generated in one millisecond"
    assert ulids == sorted(ulids), "ULIDs generated in one millisecond do not sort in order"
    assert all(len(u) == events.ULID_LEN for u in ulids)


def test_ulid_orders_across_milliseconds():
    early = events.new_ulid(ts_ms=1_800_000_000_000)
    later = events.new_ulid(ts_ms=1_800_000_001_000)
    assert early < later


# --- the durable log ---------------------------------------------------------


def test_record_event_appends_a_readable_row():
    ulid = events.record_event(
        channel="agent.task.updated",
        payload={"task_id": 42, "status": "doing"},
        originator="Desktop_Agent1",
    )
    assert len(ulid) == events.ULID_LEN

    rows = events.query_events(channels=["agent.task.updated"])
    assert len(rows) == 1
    row = rows[0]
    assert row["ulid"] == ulid
    assert row["channel"] == "agent.task.updated"
    assert row["originator"] == "Desktop_Agent1"
    assert row["payload"] == {"task_id": 42, "status": "doing"}
    assert row["created_at"]


def test_events_in_the_same_millisecond_read_back_in_insertion_order(monkeypatch):
    monkeypatch.setattr(events, "_now_ms", lambda: 1_800_000_000_000)
    for i in range(10):
        events.record_event(channel="agent.task.updated", payload={"i": i})

    rows = events.query_events(channels=["agent.task.updated"])
    assert [r["payload"]["i"] for r in rows] == list(range(10))


def test_causal_columns_exist_and_round_trip():
    """#156's columns ship in THIS migration so the policy engine adds semantics,
    not a second table rebuild on the live shared memory.db."""
    parent = events.record_event(channel="agent.health.alert", payload={})
    child = events.record_event(
        channel="agent.health.alert",
        payload={},
        episode_id="ep_20260727_abc",
        causal_parent_id=parent,
    )
    rows = {r["ulid"]: r for r in events.query_events(channels=["agent.health.alert"])}
    assert rows[child]["episode_id"] == "ep_20260727_abc"
    assert rows[child]["causal_parent_id"] == parent
    assert rows[parent]["episode_id"] is None
    assert rows[parent]["causal_parent_id"] is None


def test_query_events_since_returns_only_what_follows_the_cursor():
    first = events.record_event(channel="agent.task.updated", payload={"n": 1})
    events.record_event(channel="agent.task.updated", payload={"n": 2})
    events.record_event(channel="agent.task.updated", payload={"n": 3})

    rows = events.query_events(channels=["agent.task.updated"], since=first)
    assert [r["payload"]["n"] for r in rows] == [2, 3]


def test_event_log_query_reports_source_liveness_for_empty_windows():
    cursor = events.record_event(channel="agent.task.updated", payload={})

    idle = event_log_query(channels=["agent.task.updated"], since=cursor)
    no_writer = event_log_query(channels=["agent.health.alert"])

    assert idle["events"] == []
    assert idle["source_event_count"] == 1
    assert idle["last_event_at"]
    assert no_writer["events"] == []
    assert no_writer["source_event_count"] == 0
    assert no_writer["last_event_at"] is None


def test_channel_wildcard_matches_a_prefix_and_nothing_else():
    events.record_event(channel="agent.task.updated", payload={})
    events.record_event(channel="agent.approval.requested", payload={})
    events.record_event(channel="browser.drift.alert", payload={})

    matched = events.query_events(channels=["agent.*"])
    assert {r["channel"] for r in matched} == {
        "agent.task.updated",
        "agent.approval.requested",
    }


def test_item_id_filter_reaches_an_item_behind_newer_fleet_events():
    """A transcript reload reads its own item, not a global window (task #1992)."""
    events.record_event(
        channel="agent.node", payload={"item_id": "nd-mine", "acp_kind": "turn_checkpoint"}
    )
    for n in range(5):
        events.record_event(channel="agent.node", payload={"item_id": f"nd-other-{n}"})
    events.record_event(channel="agent.node", payload={"item_id": "nd-mine", "n": "late"})
    events.record_event(channel="agent.task.updated", payload={"item_id": "nd-mine"})

    rows = events.query_events(channels=["agent.node"], item_id="nd-mine", limit=2)
    assert [r["payload"].get("acp_kind") or r["payload"].get("n") for r in rows] == [
        "turn_checkpoint",
        "late",
    ]
    page = event_log_query(channels=["agent.node"], item_id="nd-mine", after_seq=0, limit=1)
    assert page["count"] == 1 and page["events"][0]["payload"]["acp_kind"] == "turn_checkpoint"
    rest = event_log_query(
        channels=["agent.node"], item_id="nd-mine", after_seq=page["seq_cursor"], limit=10
    )
    assert [r["payload"].get("n") for r in rest["events"]] == ["late"]
    assert event_log_query(item_id="nd-none")["events"] == []


def test_item_reads_count_channel_liveness_once_per_scan(monkeypatch):
    """Paging or refreshing one item must not recount the whole channel on
    every page (task #2010 review); the first page still reports liveness."""
    from unified_agent.tools import events as events_tool

    for n in range(3):
        events.record_event(channel="agent.node", payload={"item_id": "nd-mine", "n": n})
    counts = []
    real = events.event_log_liveness
    monkeypatch.setattr(
        events_tool.events,
        "event_log_liveness",
        lambda **kw: counts.append(kw) or real(**kw),
    )

    first = event_log_query(channels=["agent.node"], item_id="nd-mine", after_seq=0, limit=1)
    assert first["source_event_count"] >= 3 and len(counts) == 1
    later = event_log_query(
        channels=["agent.node"], item_id="nd-mine", after_seq=first["seq_cursor"], limit=1
    )
    assert later["count"] == 1 and "source_event_count" not in later
    by_ulid = event_log_query(channels=["agent.node"], item_id="nd-mine", since=first["cursor"])
    assert by_ulid["count"] == 2 and "source_event_count" not in by_ulid
    assert len(counts) == 1
    # Fleet-wide reads keep their liveness on every page.
    event_log_query(channels=["agent.node"], after_seq=first["seq_cursor"])
    assert len(counts) == 2


def test_record_event_reports_a_durable_failure_instead_of_returning_a_ulid(tmp_path, monkeypatch):
    """A ledger that swallows its own write failure is worse than no ledger.

    record_agent_event's never-raises contract is right for bookkeeping and
    wrong here: #156 must be able to fail closed on "no episode record", which
    it cannot do if a lost append is indistinguishable from a successful one.
    """
    broken_home = tmp_path / "broken_home"
    broken_home.mkdir()
    (broken_home / "memory.db").mkdir()  # a directory where the DB should be
    monkeypatch.setenv("UA_HOME", str(broken_home))

    with pytest.raises(sqlite3.Error):
        events.record_event(channel="agent.task.updated", payload={"lost": True})


# --- the socket: push ---------------------------------------------------------


def test_a_subscriber_is_pushed_a_matching_event(bus):
    with events.subscribe(["agent.task.*"], socket_path=bus.socket_path) as sub:
        ulid = events.record_event(
            channel="agent.task.updated", payload={"task_id": 7}, originator="Desktop_Agent1"
        )
        got = sub.next_event(timeout=DELIVERY_TIMEOUT)

    assert got is not None, "subscriber was never pushed the event"
    assert got["ulid"] == ulid
    assert got["channel"] == "agent.task.updated"
    assert got["payload"] == {"task_id": 7}


def test_live_delivery_queries_the_log_not_at_all(bus, monkeypatch):
    """Push, not poll — asserted rather than assumed.

    The whole point of the bus is that a consumer stops querying. Break
    query_events entirely: a live subscriber must still be delivered its event,
    because nothing on that path reads the log. (The replay path deliberately
    does query it, which is what the reconnect test covers.)
    """
    with events.subscribe(["agent.task.*"], socket_path=bus.socket_path) as sub:
        ulid = events.record_event(channel="agent.task.updated", payload={"n": 1})

        def no_queries_allowed(*a, **kw):
            raise AssertionError("live delivery queried the event log")

        monkeypatch.setattr(events, "query_events", no_queries_allowed)
        got = sub.next_event(timeout=DELIVERY_TIMEOUT)

    assert got is not None and got["ulid"] == ulid


def test_a_subscriber_hears_silence_on_a_non_matching_channel(bus):
    with events.subscribe(["browser.*"], socket_path=bus.socket_path) as sub:
        events.record_event(channel="agent.task.updated", payload={"task_id": 7})
        assert sub.next_event(timeout=SILENCE_TIMEOUT) is None


def test_multiple_subscribers_all_receive_the_same_event(bus):
    with (
        events.subscribe(["agent.*"], socket_path=bus.socket_path) as a,
        events.subscribe(["agent.task.updated"], socket_path=bus.socket_path) as b,
    ):
        ulid = events.record_event(channel="agent.task.updated", payload={})
        assert a.next_event(timeout=DELIVERY_TIMEOUT)["ulid"] == ulid
        assert b.next_event(timeout=DELIVERY_TIMEOUT)["ulid"] == ulid


# --- the socket: replay -------------------------------------------------------


def test_a_reconnecting_subscriber_misses_nothing(bus):
    """Replay-from-cursor is what makes the bus trustworthy. Without it every
    consumer has to keep polling defensively, and the bus buys nothing."""
    cursor = events.record_event(channel="agent.task.updated", payload={"n": 1})
    events.record_event(channel="agent.task.updated", payload={"n": 2})
    events.record_event(channel="agent.task.updated", payload={"n": 3})

    with events.subscribe(["agent.task.*"], since=cursor, socket_path=bus.socket_path) as sub:
        replayed = [
            sub.next_event(timeout=DELIVERY_TIMEOUT),
            sub.next_event(timeout=DELIVERY_TIMEOUT),
        ]
        assert [e["payload"]["n"] for e in replayed] == [2, 3]

        # ...and live streaming resumes after the replay, without a gap.
        events.record_event(channel="agent.task.updated", payload={"n": 4})
        live = sub.next_event(timeout=DELIVERY_TIMEOUT)
        assert live["payload"]["n"] == 4


def test_replay_respects_the_channel_filter(bus):
    cursor = events.record_event(channel="agent.task.updated", payload={"n": 0})
    events.record_event(channel="browser.drift.alert", payload={"n": 1})
    events.record_event(channel="agent.task.updated", payload={"n": 2})

    with events.subscribe(["agent.task.*"], since=cursor, socket_path=bus.socket_path) as sub:
        first = sub.next_event(timeout=DELIVERY_TIMEOUT)
        assert first["payload"]["n"] == 2
        assert sub.next_event(timeout=SILENCE_TIMEOUT) is None


# --- the socket cannot take the caller down -----------------------------------


def test_record_event_survives_no_bus_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_EVENT_SOCKET", str(tmp_path / "nobody-is-listening.sock"))
    ulid = events.record_event(channel="agent.task.updated", payload={"n": 1})
    assert ulid
    assert [r["ulid"] for r in events.query_events(channels=["agent.task.updated"])] == [ulid]


def test_record_event_survives_a_stale_socket_file(tmp_path, monkeypatch):
    """A socket file left behind by a killed process refuses connections with
    ECONNREFUSED rather than ENOENT — a different errno, same requirement."""
    stale = tmp_path / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))
    s.close()  # the file survives; nothing is accepting on it
    assert stale.exists()

    monkeypatch.setenv("UA_EVENT_SOCKET", str(stale))
    ulid = events.record_event(channel="agent.task.updated", payload={"n": 1})
    assert ulid
    assert [r["ulid"] for r in events.query_events(channels=["agent.task.updated"])] == [ulid]


def test_record_event_survives_a_plain_file_at_the_socket_path(tmp_path, monkeypatch):
    not_a_socket = tmp_path / "not-a-socket"
    not_a_socket.write_text("this is a regular file")
    monkeypatch.setenv("UA_EVENT_SOCKET", str(not_a_socket))
    assert events.record_event(channel="agent.task.updated", payload={})


def test_a_subscriber_that_vanishes_does_not_take_the_server_with_it(bus):
    victim = events.subscribe(["agent.*"], socket_path=bus.socket_path)
    victim.abort()  # close the fd without an orderly unsubscribe

    events.record_event(channel="agent.task.updated", payload={"n": 1})

    with events.subscribe(["agent.*"], socket_path=bus.socket_path) as survivor:
        ulid = events.record_event(channel="agent.task.updated", payload={"n": 2})
        assert survivor.next_event(timeout=DELIVERY_TIMEOUT)["ulid"] == ulid


# --- the socket is owner-only --------------------------------------------------


def test_socket_is_owner_only(bus):
    """Same-user IPC. This is NOT an authentication boundary — it is a
    least-privilege default so another local account cannot read task and
    approval payloads off the bus."""
    mode = stat.S_IMODE(os.stat(bus.socket_path).st_mode)
    assert mode == 0o600, f"events.sock is {oct(mode)}, expected 0o600"


def test_a_stale_socket_file_is_replaced_rather_than_blocking_startup(tmp_path):
    stale = tmp_path / "events.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))
    s.close()
    assert stale.exists()

    server = events.EventBusServer(socket_path=stale)
    server.start()  # must not raise EADDRINUSE on a dead predecessor's socket
    try:
        with events.subscribe(["agent.*"], socket_path=stale) as sub:
            ulid = events.record_event(channel="agent.task.updated", payload={})
            assert sub.next_event(timeout=DELIVERY_TIMEOUT)["ulid"] == ulid
    finally:
        server.stop()


def test_stop_removes_the_socket_file(tmp_path):
    server = events.EventBusServer(socket_path=tmp_path / "events.sock")
    server.start()
    assert server.socket_path.exists()
    server.stop()
    assert not server.socket_path.exists()


# --- the existing chokepoint feeds the bus -------------------------------------


def test_record_agent_event_reaches_a_subscriber(bus):
    """~18 call sites already funnel through record_agent_event (task claim and
    update, approval request, delegation, memory save, journal). Hooking the bus
    in there is what makes them all publishers without editing any of them."""
    from unified_agent.tools import tasks as tasks_tool

    task = tasks_tool.task_add(title="event bus wiring", notes="n")

    with events.subscribe(["agent.task.*"], socket_path=bus.socket_path) as sub:
        tasks_tool.task_update(id=task["id"], status="doing")
        got = sub.next_event(timeout=DELIVERY_TIMEOUT)

    assert got is not None, "a real task_update produced no event on the bus"
    assert got["channel"] == "agent.task.updated"
    assert str(task["id"]) in str(got["payload"])


def test_record_agent_event_never_raises_when_the_ledger_is_broken(monkeypatch):
    """The restrictive-direction rule for this task: record_agent_event's
    never-raises contract may not be relaxed to make bus errors visible."""

    def boom(*a, **kw):
        raise RuntimeError("ledger is on fire")

    monkeypatch.setattr(events, "record_event", boom)

    storage.record_agent_event("task", ref_id="1", agent="Desktop_Agent1", summary="s")

    conn = storage.connect()
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()
    finally:
        conn.close()
    assert rows["n"] == 1, "the primary agent_events write was lost to a bus failure"


def test_bus_disabled_leaves_record_agent_event_exactly_as_it_was(monkeypatch):
    monkeypatch.setenv("UA_EVENT_BUS", "0")

    storage.record_agent_event("task", ref_id="1", agent="Desktop_Agent1", summary="s")

    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"] == 0
    finally:
        conn.close()


def test_bus_disabled_still_lets_an_explicit_record_event_through(monkeypatch):
    """UA_EVENT_BUS=0 turns off the implicit mirror and the socket, not the
    ledger API itself — otherwise disabling the bus would silently disarm #156."""
    monkeypatch.setenv("UA_EVENT_BUS", "0")
    ulid = events.record_event(channel="agent.health.alert", payload={"x": 1})
    assert ulid
    assert len(events.query_events(channels=["agent.health.alert"])) == 1


def test_connection_event_writer_does_not_commit_or_publish(monkeypatch):
    storage.init_db()
    published = []
    monkeypatch.setattr(events, "publish", published.append)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ulid, inserted, payload = events.record_event_once_on_connection(
            conn, "agent.test.atomic", payload={"ok": True}
        )
        assert inserted is True
        assert payload["ulid"] == ulid
        assert published == []
        conn.rollback()
    finally:
        conn.close()
    assert events.query_events(channels=["agent.test.atomic"]) == []


def test_legacy_approval_mirror_keeps_existing_payload_shape(monkeypatch):
    monkeypatch.setenv("UA_EVENT_BUS", "1")
    storage.record_agent_event(
        "approval", ref_id="42", agent="legacy-agent", summary="approved: run_cmd"
    )
    row = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])[0]
    assert row["payload"] == {"kind": "approval", "ref_id": "42", "summary": "approved: run_cmd"}
    assert row["originator"] == "legacy-agent"


def test_structured_approval_does_not_expand_cloud_projection(monkeypatch):
    from unified_agent import cloud_mirror, config

    monkeypatch.setenv("UA_EVENT_BUS", "1")
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        storage.record_agent_event(
            "approval",
            ref_id="42",
            agent="unverified-peer",
            summary="untrusted-free-text",
            approval_tool="run_cmd",
        )
    finally:
        config.agent_identity_var.reset(token)
    row = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])[0]
    assert row["payload"]["requested_by"] == "Desktop_Agent2"
    projected = cloud_mirror.project_event(row)
    assert projected["agent"] == "Desktop_Agent2"
    assert projected["task_id"] == "42"
    assert set(projected) == cloud_mirror._ALLOWED_RECORD_KEYS
    assert "summary" not in projected
    assert "tool" not in projected
    assert "requested_by" not in projected


def test_structured_approval_preserves_restricted_profile_reads(monkeypatch):
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError

    from unified_agent import config, platform_readers, server
    from unified_agent.tools.approvals import check_approval_gate

    monkeypatch.setenv("UA_EVENT_BUS", "1")
    result = check_approval_gate(
        "run_cmd", {"command": "synthetic-private-detail"}, force=True, request_only=True
    )
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("server_agent8")
    session = config.mcp_session_id_var.set("approval-event-public-session")
    try:
        with pytest.raises(ToolError, match="not exposed by the active public profile"):
            server.REGISTERED_TOOLS["event_log_query"](channels=[events.CHANNEL_APPROVAL_REQUESTED])
        with pytest.raises(ResourceError, match="not exposed by the active public profile"):
            platform_readers.read_approval(str(result["approval_id"]))
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
