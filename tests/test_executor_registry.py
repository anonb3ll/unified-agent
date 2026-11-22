"""The executor's contribution to the registry: liveness while idle, and Pi sessions.

Two gaps these close. The executor polls every 5s but only *touches the hub* when it
finds work, so a healthy idle executor read as offline. And a Pi run left nothing but a
JSONL transcript on disk that nothing indexed, so "what is it doing right now?" had no
answer at all.
"""

import pytest

from unified_agent import registry, storage
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import pi_runner as pi_runner_tool
from unified_agent.tools import tasks as tasks_tool


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    # Point the hub at a dead port so the daemon uses in-process dispatch, mirroring
    # tests/test_executor.py:31. This is load-bearing for isolation, not cosmetic: the
    # real hub runs on 127.0.0.1:8765 on dev machines with no UA_AGENT_TOKENS set, so
    # a test that omits this reaches the live hub and its real database.
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    # The standalone case the in-process fallback exists for, opted into explicitly
    # exactly as tests/test_executor.py does. Production defaults to requiring the hub;
    # this must never be set outside a test.
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    # A daemon refuses to construct without a resolvable token (the #82 fix: a missing
    # token must look like a misconfiguration, not silently fall back in-process).
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-test")
    # TestPiSessions stubs `pi_run_task`. Pi is an explicit alias, not the
    # Desktop_agent3 default (native agy CLI).
    monkeypatch.setenv("UA_EXECUTOR_BACKEND", "pi")
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    storage.init_db()
    registry.reset_throttle()
    yield
    registry.reset_throttle()


def _agent_row(label):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM agents WHERE label=?", (label,)).fetchone()
    finally:
        conn.close()


class TestIdleHeartbeat:
    def test_poll_with_no_work_still_reports_liveness(self):
        """The gap this closes: an idle executor is healthy, not absent."""
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        assert daemon.poll_once() is None  # nothing pending

        row = _agent_row("Desktop_agent3")
        assert row is not None, "an idle poll left no trace of a running executor"
        assert row["last_seen_via"] == "executor_poll"
        assert row["kind"] == "executor"
        assert row["pid"]

    def test_repeated_polls_are_throttled(self, monkeypatch):
        """A 5s poll must not become a write per tick on a shared SQLite file."""
        monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "3600")
        registry.reset_throttle()
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        for _ in range(5):
            daemon.poll_once()

        conn = storage.connect()
        try:
            updates = conn.execute(
                "SELECT created_at, updated_at FROM agents WHERE label='Desktop_agent3'"
            ).fetchone()
        finally:
            conn.close()
        assert updates["created_at"] == updates["updated_at"]

    def test_status_derives_to_online_after_a_poll(self):
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()
        agent = registry.get_agent("Desktop_agent3")
        assert agent["status"] == "online"


class TestPiSessions:
    def _delegate(self):
        return delegate_tool.delegate(
            task_spec="Smoke test task",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )

    def test_a_successful_run_opens_and_closes_a_session(self, monkeypatch):
        monkeypatch.setattr(
            pi_runner_tool,
            "pi_run_task",
            lambda **kw: {"status": "success", "text": "done", "exit_code": 0, "error": None},
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        res = self._delegate()
        daemon.poll_once()

        sessions = registry.list_sessions(reap=False)
        assert len(sessions) == 1, "the Pi run left no session record"
        session = sessions[0]
        assert session["agent"] == "Desktop_agent3"
        assert session["kind"] == "pi_run"
        assert session["status"] == "completed"
        assert session["exit_status"] == "success"
        assert session["task_id"] == res["task_id"]
        assert session["ended_at"] is not None
        # The link that makes the on-disk transcript findable from the DB.
        task = tasks_tool.task_list(status="doing")["tasks"][0]
        expected_session = (
            f"task-{res['task_id']}-{task['current_run_id']}-delegation-{res['delegation_id']}-g1"
        )
        assert session["external_session_id"] == expected_session
        assert session["transcript_path"].endswith(f".worktrees/sessions/{expected_session}.jsonl")

    def test_a_failed_run_records_the_failure(self, monkeypatch):
        monkeypatch.setattr(
            pi_runner_tool,
            "pi_run_task",
            lambda **kw: {"status": "error", "text": "nope", "exit_code": 1, "error": "boom"},
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        self._delegate()
        daemon.poll_once()

        session = registry.list_sessions(reap=False)[0]
        assert session["status"] == "failed"
        assert session["exit_status"] == "error"

    def test_a_crashing_run_does_not_leave_the_session_running(self, monkeypatch):
        """A row asserting 'running' forever is worse than no row at all.

        A backend exception is normalized into a failed execution result
        (task #445 / PR #194 review) rather than propagated: it must still
        count toward the circuit breaker and requeue/fail the delegation like
        any other failed run, so it no longer escapes poll_once as a raised
        exception.
        """

        def explode(**kw):
            raise RuntimeError("pi died")

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", explode)
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        self._delegate()
        daemon.poll_once()

        session = registry.list_sessions(reap=False)[0]
        assert session["status"] == "failed"
        assert daemon.consecutive_failures == 1
        # Normalized into the same failed-run shape a completed-but-failed
        # backend call produces, not the raise-and-reraise "exception" exit
        # status that path used before.
        assert session["exit_status"] == "error"

    def test_the_task_runs_even_when_session_recording_fails(self, monkeypatch):
        """Bookkeeping must never gate real work."""
        monkeypatch.setattr(registry, "start_session", lambda *a, **k: None)
        ran = {}
        monkeypatch.setattr(
            pi_runner_tool,
            "pi_run_task",
            lambda **kw: (
                ran.setdefault("yes", True)
                and {"status": "success", "text": "ok", "exit_code": 0, "error": None}
            ),
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        self._delegate()
        daemon.poll_once()
        assert ran.get("yes") is True

    def test_shadow_mode_records_no_session(self, monkeypatch):
        """Shadow mode's whole contract is zero side effects."""
        monkeypatch.setattr(
            pi_runner_tool,
            "pi_run_task",
            lambda **kw: {"status": "success", "text": "ok", "exit_code": 0, "error": None},
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=True)
        self._delegate()
        daemon.poll_once()
        assert registry.list_sessions(reap=False) == []
