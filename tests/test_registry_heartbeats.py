"""Heartbeat writers, session lifecycle, and the never-raises contract.

`touch_agent` runs on hot paths — every authenticated hub request, every
`record_agent_event`. The most important assertions here are the negative ones: that
it throttles, and that it cannot break the operation it is attached to.
"""

import pytest

from unified_agent import config, registry, storage
from unified_agent.tools.registry import agent_context


@pytest.fixture(autouse=True)
def _fresh_db():
    storage.init_db()
    registry.reset_throttle()
    yield
    registry.reset_throttle()


def _agent_row(label):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM agents WHERE label = ?", (label,)).fetchone()
    finally:
        conn.close()


class TestTouchAgent:
    def test_writes_a_row(self):
        assert registry.touch_agent("Desktop_Agent1", via="hub_request") is True
        row = _agent_row("Desktop_Agent1")
        assert row["last_seen_via"] == "hub_request"
        assert row["first_seen"] == row["last_seen"]

    def test_throttled_within_the_window(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "3600")
        registry.reset_throttle()
        assert registry.touch_agent("A", via="one") is True
        assert registry.touch_agent("A", via="two") is False
        assert _agent_row("A")["last_seen_via"] == "one", "throttled call still wrote"

    def test_force_bypasses_the_throttle(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "3600")
        registry.reset_throttle()
        registry.touch_agent("A", via="one")
        assert registry.touch_agent("A", via="two", force=True) is True
        assert _agent_row("A")["last_seen_via"] == "two"

    def test_throttle_is_per_label(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "3600")
        registry.reset_throttle()
        assert registry.touch_agent("A", via="x") is True
        assert registry.touch_agent("B", via="x") is True, "one agent throttled another"

    def test_zero_interval_disables_throttling(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        assert registry.touch_agent("A", via="x") is True
        assert registry.touch_agent("A", via="y") is True

    def test_first_seen_is_preserved_across_touches(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("A", via="x")
        first = _agent_row("A")["first_seen"]
        registry.touch_agent("A", via="y")
        assert _agent_row("A")["first_seen"] == first

    def test_known_flag_tracks_config(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.touch_agent("Desktop_Agent1", via="x", force=True)
        registry.touch_agent("randolph", via="x", force=True)
        assert _agent_row("Desktop_Agent1")["known"] == 1
        assert _agent_row("randolph")["known"] == 0

    def test_empty_and_none_labels_are_ignored(self):
        assert registry.touch_agent(None, via="x") is False
        assert registry.touch_agent("   ", via="x") is False
        conn = storage.connect()
        try:
            assert conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"] == 0
        finally:
            conn.close()

    def test_never_raises_when_the_db_is_broken(self, monkeypatch):
        """The whole contract. A failed heartbeat must not break a real tool call."""

        def boom():
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(storage, "connect", boom)
        registry.reset_throttle()
        assert registry.touch_agent("A", via="x") is False

    def test_optional_fields_are_not_clobbered_by_a_later_touch(self, monkeypatch):
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
        registry.reset_throttle()
        registry.touch_agent("A", via="x", kind="executor", pid=4242)
        registry.touch_agent("A", via="y")  # a caller that knows neither
        row = _agent_row("A")
        assert row["kind"] == "executor"
        assert row["pid"] == 4242

    def test_agent_context_exposes_identity_peers_and_rules(self, monkeypatch):
        registry.touch_agent("Laptop_Agent1", via="hub_request", host="laptop", force=True)
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            context = agent_context()
        finally:
            config.agent_identity_var.reset(token)

        assert context["identity"] == "Desktop_Agent2"
        assert any(peer["label"] == "Laptop_Agent1" for peer in context["peers"])
        assert "task_claim_is_atomic_and_claimed_by_is_authoritative" in context["rules"]
        assert all("token" not in peer for peer in context["peers"])


class TestRecordAgentEventHeartbeat:
    def test_record_agent_event_touches_the_agent(self):
        """~18 existing call sites become heartbeat writers with no edits to them."""
        storage.record_agent_event("task", ref_id="1", agent="Desktop_Agent5")
        assert _agent_row("Desktop_Agent5") is not None
        assert _agent_row("Desktop_Agent5")["last_seen_via"] == "agent_event"

    def test_unattributed_event_creates_no_agent(self):
        storage.record_agent_event("task", ref_id="1", agent=None)
        conn = storage.connect()
        try:
            assert conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"] == 0
        finally:
            conn.close()

    def test_event_still_recorded_when_the_heartbeat_fails(self, monkeypatch):
        """The primary write must survive a broken registry."""
        monkeypatch.setattr(registry, "touch_agent", lambda *a, **k: 1 / 0)
        storage.record_agent_event("task", ref_id="9", agent="A")
        conn = storage.connect()
        try:
            assert conn.execute("SELECT COUNT(*) c FROM agent_events").fetchone()["c"] == 1
        finally:
            conn.close()


class TestSessions:
    def test_lifecycle(self):
        sid = registry.start_session("A", "pi_run", external_session_id="task-7-session")
        assert sid
        open_now = registry.list_sessions(status="open")
        assert [s["id"] for s in open_now] == [sid]
        assert open_now[0]["external_session_id"] == "task-7-session"

        assert registry.end_session(sid, status="completed", exit_status="success") is True
        done = registry.list_sessions()
        assert done[0]["status"] == "completed"
        assert done[0]["ended_at"] is not None
        assert registry.list_sessions(status="open") == []

    def test_start_session_touches_its_agent(self):
        registry.start_session("A", "pi_run")
        assert _agent_row("A") is not None

    def test_end_session_on_unknown_id_is_false_not_an_exception(self):
        assert registry.end_session("nope") is False
        assert registry.end_session(None) is False

    def test_duration_and_heartbeat_age_are_reported(self):
        sid = registry.start_session("A", "pi_run")
        row = registry.list_sessions()[0]
        assert row["duration_s"] is not None and row["duration_s"] >= 0
        assert row["heartbeat_age_s"] is not None
        registry.end_session(sid)

    def test_reaper_marks_stale_running_sessions_abandoned(self, monkeypatch):
        """A killed executor must not leave a row asserting it is still running."""
        sid = registry.start_session("A", "pi_run")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET last_heartbeat_at='2020-01-01T00:00:00+00:00'"
                " WHERE id=?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()

        assert registry.reap_abandoned_sessions() == 1
        row = registry.list_sessions(reap=False)[0]
        assert row["status"] == "abandoned"
        assert row["exit_status"] == "no_heartbeat"

    def test_reaper_leaves_fresh_sessions_alone(self):
        registry.start_session("A", "pi_run")
        assert registry.reap_abandoned_sessions() == 0
        assert registry.list_sessions(reap=False)[0]["status"] == "running"

    def test_heartbeat_session_rescues_a_long_run(self, monkeypatch):
        sid = registry.start_session("A", "pi_run")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET last_heartbeat_at='2020-01-01T00:00:00+00:00'"
                " WHERE id=?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()
        assert registry.heartbeat_session(sid) is True
        assert registry.reap_abandoned_sessions() == 0

    def test_list_sessions_reaps_by_default(self):
        """A read must never report a dead run as live."""
        sid = registry.start_session("A", "pi_run")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET last_heartbeat_at='2020-01-01T00:00:00+00:00'"
                " WHERE id=?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()
        assert registry.list_sessions()[0]["status"] == "abandoned"


class TestListAgents:
    def test_configured_but_unseen_labels_are_surfaced(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Ghost:Desktop_Agent1")
        registry.touch_agent("Desktop_Agent1", via="x", force=True)
        labels = {a["label"]: a for a in registry.list_agents()}
        assert labels["Ghost"]["status"] == "unknown"
        assert labels["Desktop_Agent1"]["status"] == "online"

    def test_include_configured_false_hides_unseen(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Ghost:Desktop_Agent1")
        registry.touch_agent("Desktop_Agent1", via="x", force=True)
        labels = {a["label"] for a in registry.list_agents(include_configured=False)}
        assert labels == {"Desktop_Agent1"}

    def test_open_session_and_task_counts(self):
        registry.start_session("A", "pi_run")
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO tasks (title, status, priority, claimed_by, created_at, updated_at)"
                " VALUES ('t','doing','med','A','2026-01-01T00:00:00+00:00',"
                "'2026-01-01T00:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        agent = next(a for a in registry.list_agents() if a["label"] == "A")
        assert agent["open_sessions"] == 1
        assert agent["active_tasks"] == 1

    def test_unregistered_labels_reports_drift(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.touch_agent("Desktop_Agent1", via="x", force=True)
        registry.touch_agent("mystery-cron", via="x", force=True)
        assert registry.unregistered_labels() == ["mystery-cron"]
