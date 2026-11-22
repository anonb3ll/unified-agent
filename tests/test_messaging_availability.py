"""Availability preflight at send (task #2392).

Ground truth:
- a live heartbeat, an open session or an online node reads available and adds
  no warning;
- a known but quiet label reads degraded: the message is still written, and the
  sender is told it will wait;
- no liveness record at all reads unknown, and is still written;
- an unreadable resolver fails open: unknown, never a refusal;
- only a label that is not an addressable identity is unavailable, and that
  refuses the send;
- an office fan-out reports every member and never drops a quiet one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import node_queue, registry, storage
from unified_agent.messaging import availability
from unified_agent.messaging import index as message_index
from unified_agent.messaging.send import send_message

SENDER = "Desktop_Agent4"
PEER = "Desktop_Agent2"
NOW = datetime.now(UTC)


def _clock() -> datetime:
    """The clock at call time, not import time (task #2433).

    ``NOW`` above is import time. A shard can import this module minutes before
    it runs a test, and a heartbeat stamped at ``NOW`` then reads "agent stale"
    against the real clock, so anything asserted to be live must take its stamp
    when the test runs.
    """
    return datetime.now(UTC)


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent4:Desktop_Agent2:Desktop_Agent4b")
    (tmp_path / "ws").mkdir()
    storage.init_db()


def _chart(tmp_path, monkeypatch, members: list[str]) -> None:
    chart = {
        "schema_version": 1,
        "offices": [
            {
                "id": "platform",
                "name": "Platform Engineering",
                "lead": members[0],
                "members": members,
                "owned_epics": [],
                "default_lanes": [],
                "decision_rights": [],
            }
        ],
    }
    path = tmp_path / "org.yaml"
    path.write_text(yaml.safe_dump(chart), encoding="utf-8")
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))


def _seen(label: str, at: datetime | None) -> None:
    stamp = _clock().isoformat()
    seen = None if at is None else at.isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agents (label, kind, known, first_seen, last_seen, last_seen_via, "
            "created_at, updated_at) VALUES (?, 'agent', 1, ?, ?, 'heartbeat', ?, ?)",
            (label, stamp, seen, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _preflight(label: str, now: datetime | None = None) -> availability.Preflight:
    conn = storage.connect()
    try:
        return availability.preflight(conn, label, now=now)
    finally:
        conn.close()


def _mailbox(label: str) -> list[str]:
    conn = storage.connect()
    try:
        return [row["message_id"] for row in message_index.query(conn, recipient=label)]
    finally:
        conn.close()


def test_a_live_heartbeat_is_available_and_adds_no_warning(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _seen(PEER, _clock())
    result = send_message(act="inform", to_agent=PEER, body="hello", from_agent=SENDER)
    assert result["availability"] == {
        "agent": PEER,
        "availability": availability.AVAILABLE,
        "reason": "live now",
    }
    assert "delivery_warning" not in result


def test_a_quiet_known_label_is_degraded_warned_and_still_written(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _seen(PEER, NOW - timedelta(days=2))
    result = send_message(act="inform", to_agent=PEER, body="hello", from_agent=SENDER)
    assert result["availability"]["availability"] == availability.DEGRADED
    assert "will wait" in result["delivery_warning"]
    assert result["accepted"] is True
    assert result["message_id"] in _mailbox(PEER)


def test_no_record_at_all_is_unknown_and_still_written(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    result = send_message(act="inform", to_agent=PEER, body="hello", from_agent=SENDER)
    assert result["availability"]["availability"] == availability.UNKNOWN
    assert "could not confirm" in result["delivery_warning"]
    assert result["accepted"] is True


def test_an_open_session_is_available_even_without_a_fresh_heartbeat(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _seen(PEER, NOW - timedelta(days=2))
    registry.register_session(PEER, "codex", external_session_id="ext-x", host="n")
    assert _preflight(PEER).verdict == availability.AVAILABLE


def test_a_live_node_is_available_even_when_the_agent_is_quiet(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _seen(PEER, NOW - timedelta(days=2))
    node_queue.register("probe.local", identity=PEER, now=NOW)
    # Judge at the instant the node registered: the module-level NOW is import
    # time, and a long shard run makes the real clock minutes older, which would
    # read the node as stale and turn this into a degraded case.
    assert _preflight(PEER, now=NOW).verdict == availability.AVAILABLE


def test_an_unreadable_resolver_fails_open(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)

    def _boom():
        raise OSError("store gone")

    monkeypatch.setattr(availability.storage, "connect", _boom)
    assert availability.preflight_label(PEER).verdict == availability.UNKNOWN


def test_a_malformed_label_is_unavailable_and_refused(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    assert _preflight("not a label").verdict == availability.UNAVAILABLE
    with pytest.raises(ToolError):
        send_message(act="inform", to_agent="not a label", body="hello", from_agent=SENDER)


def test_an_unavailable_verdict_carries_the_refusal_text():
    ready = availability.Preflight("x", availability.UNAVAILABLE, "not an identity")
    assert availability.reject_reason(ready)
    assert (
        availability.reject_reason(availability.Preflight("x", availability.DEGRADED, "quiet"))
        == ""
    )


def test_an_office_send_reports_every_member_and_drops_nobody(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    members = [SENDER, PEER, "Desktop_Agent4b"]
    _chart(tmp_path, monkeypatch, members)
    _seen(PEER, _clock())
    _seen("Desktop_Agent4b", NOW - timedelta(days=2))
    result = send_message(
        act="inform", to_agent=None, to_office="platform", body="standby", from_agent=SENDER
    )
    assert result["count"] == 2
    verdicts = {member: entry["availability"] for member, entry in result["availability"].items()}
    assert verdicts == {PEER: availability.AVAILABLE, "Desktop_Agent4b": availability.DEGRADED}
    assert any("will wait" in warning for warning in result["warnings"])
