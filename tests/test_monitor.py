"""agent_events activity index + monitor query side (task 64 phases A-C).

Covers: events written by task/memory/journal mutations, event-write failure
never breaking the primary operation, agent_activity filters, metrics_snapshot
shapes, all four anomaly rules triggering and clearing around their
thresholds, the phase B perf instrumentation wrapper + perf_summary, and the
phase C anomaly push-notification dedupe/cooldown ledger.
"""

import json
import os
import plistlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from export_guard import require_exported_paths
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.monitor import (
    agent_activity,
    check_anomalies,
    check_anomalies_and_notify,
    delivery_slo,
    instrumented,
    metrics_snapshot,
    observability_manage,
    perf_summary,
)
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add, task_update
from unified_agent.tools.vault import vault_journal, vault_read


@pytest.fixture(autouse=True)
def vault_dir(isolated_env):
    """vault_journal needs an existing vault root (conftest only sets the env var)."""
    from unified_agent import config

    config.vault_root().mkdir(parents=True, exist_ok=True)


def _events(**kwargs):
    return agent_activity(**kwargs)["events"]


def _as_agent(agent: str):
    """Bind the authenticated caller for writes that should be attributed."""
    return config.agent_identity_var.set(agent)


def _iso_days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _backdate_event(event_id: int, ts: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_events SET ts=? WHERE id=?", (ts, event_id))
        conn.commit()
    finally:
        conn.close()


def _gone_silent(agent: str, days_ago: float = 3.0, entries: int = 2) -> None:
    """Make `agent` look like a daemon that established a journaling cadence
    and then stopped. `entries` must clear SILENT_AGENT_MIN_EVENTS — a single
    journal reads as a one-off tool run and is exempt by design.

    Also backdates agents.last_seen so hub-freshness matches the stale journals
    (vault_journal → record_agent_event would otherwise leave last_seen at now).
    """
    for i in range(entries):
        vault_journal(f"check-in {i}", agent=agent)
    for e in _events(kind="journal"):
        if e["agent"] == agent:
            _backdate_event(e["id"], _iso_days_ago(days_ago))
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agents SET last_seen=? WHERE label=?",
            (_iso_days_ago(days_ago), agent),
        )
        conn.commit()
    finally:
        conn.close()


def _backdate_task(task_id: int, updated_at: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (updated_at, task_id))
        conn.commit()
    finally:
        conn.close()


def _backdate_perf_samples(ts: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_perf_samples SET ts=?", (ts,))
        conn.commit()
    finally:
        conn.close()


def _backdate_notification(rule: str, subject: str, last_notified_at: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE anomaly_notifications SET last_notified_at=? WHERE rule=? AND subject=?",
            (last_notified_at, rule, subject),
        )
        conn.commit()
    finally:
        conn.close()


class TestEventHooks:
    def test_task_add_records_event(self):
        token = _as_agent("Desktop_Agent1")
        try:
            r = task_add("build the thing", notes="claim metadata is descriptive")
        finally:
            config.agent_identity_var.reset(token)
        evs = _events(kind="task")
        assert len(evs) == 1
        ev = evs[0]
        assert ev["kind"] == "task"
        assert ev["ref_id"] == str(r["id"])
        assert ev["agent"] == "Desktop_Agent1"
        assert "build the thing" in ev["summary"]

    def test_task_add_unclaimed_records_null_agent(self):
        task_add("orphan work")
        evs = _events(kind="task")
        assert evs[0]["agent"] is None

    def test_claimed_by_placeholder_markup_records_null_agent(self):
        """'claimed-by: [waiting-for: dev-claim]' is unclaimed markup, not a
        '[waiting-for:' agent label."""
        task_add("templated claim", notes="claimed-by: [waiting-for: dev-claim]")
        evs = _events(kind="task")
        assert evs[0]["agent"] is None

    def test_task_update_status_change_records_event(self):
        r = task_add("progress me")
        token = _as_agent("Laptop_agent3")
        try:
            task_update(r["id"], status="doing", notes="claim metadata is descriptive")
        finally:
            config.agent_identity_var.reset(token)
        evs = _events(kind="task")
        # newest first: the status transition, then the add
        assert len(evs) == 2
        assert evs[0]["ref_id"] == str(r["id"])
        assert evs[0]["agent"] == "Laptop_agent3"
        assert "open->doing" in evs[0]["summary"]

    def test_task_update_without_status_change_records_nothing(self):
        r = task_add("quiet edits")
        task_update(r["id"], title="renamed", priority="high")
        task_update(r["id"], status="open")  # same status: not a transition
        assert len(_events(kind="task")) == 1  # only the add

    def test_memory_save_records_events(self):
        r = memory_save("a fact", title="Fact", author="Desktop_Agent1")
        memory_save("a better fact", id=r["id"], author="Desktop_Agent1")
        evs = _events(kind="memory")
        assert len(evs) == 2
        assert evs[0]["summary"].startswith("update:")
        assert evs[1]["summary"].startswith("create:")
        assert all(e["agent"] == "Desktop_Agent1" and e["ref_id"] == r["id"] for e in evs)

    def test_vault_journal_records_event(self):
        vault_journal("merged PR 99", agent="Desktop_agent3", tags=["ship"])
        evs = _events(kind="journal")
        assert len(evs) == 1
        assert evs[0]["agent"] == "Desktop_agent3"
        assert "merged PR 99" in evs[0]["summary"]
        assert evs[0]["ref_id"].startswith("Logs/")

    def test_event_summary_truncated_to_200_chars(self):
        task_add("x" * 500)
        assert len(_events(kind="task")[0]["summary"]) <= 200


class TestEventWriteFailureTolerance:
    """A broken event write must never break the primary operation."""

    @pytest.fixture
    def broken_recorder(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("event index unavailable")

        monkeypatch.setattr(storage, "record_agent_event", boom)

    def test_task_add_and_update_survive(self, broken_recorder):
        r = task_add("still works")
        assert task_update(r["id"], status="doing")["status"] == "doing"

    def test_memory_save_survives(self, broken_recorder):
        r = memory_save("still saves", author="A")
        assert r["created"] is True
        assert memory_save("update", id=r["id"], author="A")["created"] is False

    def test_vault_journal_survives(self, broken_recorder):
        out = vault_journal("still journals", agent="Desktop_Agent1")
        assert "still journals" in out["entry"]


class TestAgentActivity:
    def _seed(self):
        token = _as_agent("A")
        try:
            task_add("t1", notes="claim metadata is descriptive")
        finally:
            config.agent_identity_var.reset(token)
        memory_save("note", author="B")
        vault_journal("j1", agent="A")

    def test_filters_by_agent_and_kind(self):
        self._seed()
        assert {e["kind"] for e in _events()} == {"task", "memory", "journal"}
        a_events = _events(agent="A")
        assert len(a_events) == 2
        assert {e["kind"] for e in a_events} == {"task", "journal"}
        assert [e["agent"] for e in _events(kind="memory")] == ["B"]

    def test_since_filter_and_validation(self):
        self._seed()
        old_id = _events(kind="task")[0]["id"]
        _backdate_event(old_id, _iso_days_ago(30))
        recent = _events(since=_iso_days_ago(1))
        assert old_id not in [e["id"] for e in recent]
        assert len(recent) == 2
        with pytest.raises(ToolError, match="ISO-8601"):
            agent_activity(since="yesterdayish")

    def test_limit_and_truncated(self):
        for i in range(5):
            task_add(f"t{i}")
        out = agent_activity(limit=3)
        assert len(out["events"]) == 3
        assert out["truncated"] is True
        assert agent_activity(limit=200)["truncated"] is False
        # newest first
        ids = [e["id"] for e in agent_activity()["events"]]
        assert ids == sorted(ids, reverse=True)


class TestMetricsSnapshot:
    def test_shapes_on_fixture_db(self):
        token = _as_agent("A")
        try:
            r1 = task_add("done thing", notes="claim metadata is descriptive")
            task_update(r1["id"], status="done", actor="test-agent")
        finally:
            config.agent_identity_var.reset(token)
        task_add("open thing")
        memory_save("note", author="A")
        vault_journal("did it", agent="B")

        snap = metrics_snapshot()
        assert set(snap) == {
            "generated_at",
            "events_last_7_days",
            "tasks_by_status",
            "done_task_cycle_days",
            "db_size_bytes",
        }
        assert snap["events_last_7_days"]["A"] == {"task": 2, "memory": 1}
        assert snap["events_last_7_days"]["B"] == {"journal": 1}
        assert snap["events_last_7_days"]["(unattributed)"] == {"task": 1}
        assert snap["tasks_by_status"] == {"done": 1, "open": 1}
        cycle = snap["done_task_cycle_days"]
        assert cycle["count"] == 1
        assert cycle["avg"] is not None and cycle["avg"] >= 0
        assert cycle["min"] <= cycle["avg"] <= cycle["max"]
        assert snap["db_size_bytes"] > 0
        datetime.fromisoformat(snap["generated_at"])

    def test_empty_db_is_sane(self):
        snap = metrics_snapshot()
        assert snap["events_last_7_days"] == {}
        assert snap["tasks_by_status"] == {}
        assert snap["done_task_cycle_days"] == {
            "count": 0,
            "avg": None,
            "min": None,
            "max": None,
        }

    def test_old_events_excluded_from_7_day_window(self):
        task_add("ancient", notes="claimed-by: A")
        _backdate_event(_events()[0]["id"], _iso_days_ago(8))
        assert metrics_snapshot()["events_last_7_days"] == {}

    def test_mixed_naive_and_aware_task_timestamps_are_supported(self):
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO tasks "
                "(title, status, priority, created_at, updated_at, completed_at) "
                "VALUES (?, 'done', 'low', ?, ?, ?)",
                (
                    "legacy done",
                    "2026-08-18 00:00:00",
                    "2026-08-19T00:00:00+00:00",
                    "2026-08-19T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        cycle = metrics_snapshot()["done_task_cycle_days"]
        assert cycle["count"] == 1
        assert cycle["avg"] == 1.0


class TestObservabilityManage:
    def test_dispatches_read_only_families(self):
        assert observability_manage("activity") == {"events": [], "truncated": False}
        assert observability_manage("metrics")["tasks_by_status"] == {}
        assert observability_manage("anomalies") == {"anomalies": []}
        assert observability_manage("performance")["by_tool"] == {}
        assert set(observability_manage("agents")) == {"agents", "counts", "unregistered"}
        assert set(observability_manage("context")) >= {"identity", "profile", "peers", "rules"}

    def test_rejects_bad_action_and_params(self):
        with pytest.raises(ToolError, match="action must be one of"):
            observability_manage("unknown")
        with pytest.raises(ToolError, match="params must be an object"):
            observability_manage("metrics", [])

    def test_read_only_action_works_for_authenticated_developer(self, monkeypatch):
        from unified_agent import profiles

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("observability-auth-test")
        try:
            assert observability_manage("activity", {"limit": 1}) == {
                "events": [],
                "truncated": False,
            }
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.clear()

    def test_notification_action_remains_profile_gated(self, monkeypatch):
        from unified_agent import profiles

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("observability-test")
        try:
            with pytest.raises(ToolError, match="not exposed"):
                observability_manage("anomalies_notify")
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.clear()


class TestAnomalies:
    def test_stale_doing_task_triggers_and_clears(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        r = task_add("long haul")
        task_update(r["id"], status="doing")
        # fresh: no anomaly
        assert check_anomalies()["anomalies"] == []
        # just under threshold: still clear
        _backdate_task(r["id"], _iso_days_ago(4.5))
        assert check_anomalies()["anomalies"] == []
        # past threshold: triggers
        _backdate_task(r["id"], _iso_days_ago(6))
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "stale_doing_task"]
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"
        assert "long haul" in hits[0]["detail"]
        # progressing the task clears it (update bumps updated_at)
        task_update(r["id"], status="blocked")
        assert check_anomalies()["anomalies"] == []

    def test_non_doing_tasks_never_stale(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        r = task_add("old but open")
        _backdate_task(r["id"], _iso_days_ago(30))
        assert check_anomalies()["anomalies"] == []

    def test_silent_agent_triggers_and_clears(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        # A journals recently: clear. B has a cadence but has gone quiet: triggers.
        _gone_silent("B")
        vault_journal("checking in", agent="A")
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:B"]
        assert anomalies[0]["rule"] == "silent_agent"
        # B journals again: clears
        vault_journal("hello", agent="B")
        assert check_anomalies()["anomalies"] == []

    def test_hub_alive_idle_executor_is_idle_lane_not_silent(self, monkeypatch):
        """An executor that heartbeats the hub but holds no claimed work is
        idle_lane (info), not silent_agent — Desktop_agent3 / server_agent2 noise."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("Desktop_agent3")
        from unified_agent import registry

        registry.touch_agent("Desktop_agent3", via="hub_request", force=True)
        anomalies = check_anomalies()["anomalies"]
        assert len(anomalies) == 1
        assert anomalies[0]["rule"] == "idle_lane"
        assert anomalies[0]["subject"] == "agent:Desktop_agent3"

    def test_hub_alive_with_claimed_work_stays_silent_agent(self, monkeypatch):
        """Hub heartbeat does not excuse an agent that claimed work and stopped
        journaling — that is still a silent failure."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("Desktop_agent3")
        from unified_agent import registry

        registry.touch_agent("Desktop_agent3", via="hub_request", force=True)
        r = task_add("live claim")
        task_update(r["id"], status="doing")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET claimed_by=? WHERE id=?",
                ("Desktop_agent3", r["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        anomalies = check_anomalies()["anomalies"]
        silent = [a for a in anomalies if a["rule"] == "silent_agent"]
        assert [a["subject"] for a in silent] == ["agent:Desktop_agent3"]
        assert not any(a["rule"] == "idle_lane" for a in anomalies)

    def test_idle_lane_excluded_from_notify_paging(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        from unified_agent import registry
        from unified_agent.monitor import check_anomalies_and_notify

        _gone_silent("Desktop_agent3")
        registry.touch_agent("Desktop_agent3", via="hub_request", force=True)
        calls: list[str] = []

        def fake_notify(message):
            calls.append(message)
            return {"delivered": True}

        monkeypatch.setattr("unified_agent.tools.notify.notify", fake_notify)
        result = check_anomalies_and_notify()
        assert any(a["rule"] == "idle_lane" for a in result["anomalies"])
        assert result["notified"] is False
        assert result["newly_notified"] == []
        assert calls == []

    def test_silent_agent_never_journaled_label_not_flagged(self, monkeypatch):
        """A label seen only via task events (human approvers, one-off
        attributions) is not held to the journaling cadence."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        task_add("busy but silent", notes="claimed-by: B")
        task_add("human touch", notes="claimed-by: Operator")
        assert check_anomalies()["anomalies"] == []

    def test_silent_agent_single_journal_label_not_flagged(self, monkeypatch):
        """A label that journaled exactly once is a tool invocation, not a
        daemon: `pi_run_task` attributes its one journal to `pi_worker` and
        nothing runs afterwards. One event is not a cadence, so there is
        nothing to be late for — and without this carve-out the finding
        re-fires on every cron tick forever."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        vault_journal("ran one Pi subtask", agent="pi_worker")
        _backdate_event(_events(kind="journal")[0]["id"], _iso_days_ago(3))
        assert check_anomalies()["anomalies"] == []

    def test_silent_agent_min_events_is_configurable(self, monkeypatch):
        """Raising the floor exempts agents with fewer journals than it."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_MIN_EVENTS", "3")
        _gone_silent("A", entries=2)
        assert check_anomalies()["anomalies"] == []
        # A third journal crosses the floor, and it is also stale: flagged.
        _gone_silent("A", entries=1)
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:A"]

    def test_silent_agent_old_journal_still_triggers(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:A"]

    def test_silent_agent_ignore_list_exempts_named_labels(self, monkeypatch):
        """A registered-but-idle agent Operator lists in the ignore env is not
        flagged, even with a stale cadence; other silent agents still are."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_IGNORE", "Laptop_Agent2, Laptop_Agent4")
        _gone_silent("Laptop_Agent2")
        _gone_silent("Laptop_Agent4")
        _gone_silent("Desktop_agent3")
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:Desktop_agent3"]

    def test_silent_agent_ignore_list_empty_by_default(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("Laptop_Agent2")
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:Laptop_Agent2"]

    def test_silent_agent_weekly_export_uses_its_own_cadence(self, monkeypatch):
        """The weekly export journals once a week. Under the global 2-day
        window it was flagged five days of every week; by default it is judged
        against 8 days instead, so a missed weekly run still pages."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.delenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", raising=False)
        _gone_silent("weekly-export-cron", days_ago=4)
        _gone_silent("Desktop_agent3", days_ago=4)
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:Desktop_agent3"]

    def test_silent_agent_weekly_export_missed_run_still_flags(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.delenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", raising=False)
        _gone_silent("weekly-export-cron", days_ago=9)
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:weekly-export-cron"]
        assert anomalies[0]["rule"] == "silent_agent"
        assert "threshold 8 days" in anomalies[0]["detail"]

    def test_silent_agent_days_by_label_shorter_than_global(self, monkeypatch):
        """A per-label threshold below the global one flags that label sooner."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "5")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", "fast=1")
        _gone_silent("fast", days_ago=2)
        _gone_silent("slow", days_ago=2)
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:fast"]
        assert "threshold 1 days" in anomalies[0]["detail"]

    def test_silent_agent_days_by_label_skips_malformed_entries(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", "A=junk, B=-3, =4, C, D=10")
        for agent in ("A", "B", "C", "D"):
            _gone_silent(agent, days_ago=3)
        anomalies = check_anomalies()["anomalies"]
        # A, B and C fall back to the global 2 days; D's valid 10-day entry holds.
        assert [a["subject"] for a in anomalies] == ["agent:A", "agent:B", "agent:C"]

    @pytest.mark.parametrize("bad", ["inf", "1e309", "1e20", "10000000", "nan"])
    def test_silent_agent_days_by_label_unusable_value_cannot_stop_the_check(
        self, monkeypatch, bad
    ):
        """A value that passes `> 0` but cannot make a cutoff date (or is not a
        number at all) is skipped: other labels keep their entries, the rule
        falls back to the global window and the other anomaly rules still run."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", f"unused={bad},kept=10")
        _gone_silent("kept", days_ago=3)
        _gone_silent("other", days_ago=3)
        r = task_add("stuck")
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(30))
        found = {(a["rule"], a["subject"]) for a in check_anomalies()["anomalies"]}
        assert ("silent_agent", "agent:other") in found
        assert ("silent_agent", "agent:kept") not in found
        assert ("stale_doing_task", f"task:{r['id']}") in found

    @pytest.mark.parametrize("bad", ["inf", "1e309", "10000000"])
    def test_silent_agent_global_days_unusable_value_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", bad)
        _gone_silent("A", days_ago=3)
        anomalies = check_anomalies()["anomalies"]
        assert [a["subject"] for a in anomalies] == ["agent:A"]
        assert "threshold 2 days" in anomalies[0]["detail"]

    def _journal_and_hub_ages(self, agent: str, journal_days: float, hub_days: float):
        """Journals and hub last_seen at independent ages (not _gone_silent's
        single age), so the hub-freshness window is tested on its own."""
        from unified_agent import registry

        _gone_silent(agent, days_ago=journal_days)
        # Force the agents row: the touch throttle is process-wide and may
        # have skipped it for a label an earlier test used.
        registry.touch_agent(agent, via="hub_request", force=True)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agents SET last_seen=? WHERE label=?",
                (_iso_days_ago(hub_days), agent),
            )
            conn.commit()
        finally:
            conn.close()

    def test_longer_label_window_judges_hub_freshness_by_label(self, monkeypatch):
        """Label 8d, global 2d: journal 9d old, hub seen 4d ago, no claim. The
        hub is fresh inside the label's 8 days, so this is idle_lane. A global
        2-day hub window would wrongly call it silent_agent."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", "slow_hub=8")
        self._journal_and_hub_ages("slow_hub", journal_days=9, hub_days=4)
        anomalies = check_anomalies()["anomalies"]
        assert [(a["rule"], a["subject"]) for a in anomalies] == [("idle_lane", "agent:slow_hub")]

    def test_shorter_label_window_judges_hub_freshness_by_label(self, monkeypatch):
        """Label 1d, global 5d: journal 2d old, hub seen 3d ago, no claim. The
        hub is stale for the label's 1 day, so this is silent_agent. A global
        5-day hub window would wrongly call it idle_lane."""
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "5")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", "fast_hub=1")
        self._journal_and_hub_ages("fast_hub", journal_days=2, hub_days=3)
        anomalies = check_anomalies()["anomalies"]
        assert [(a["rule"], a["subject"]) for a in anomalies] == [
            ("silent_agent", "agent:fast_hub")
        ]

    def test_unattributed_events_never_flag_silent_agent(self):
        task_add("nobody claimed this")  # agent NULL
        assert check_anomalies()["anomalies"] == []

    def test_threshold_env_fallback_on_garbage(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "not-a-number")
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "-1")
        r = task_add("edge")
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))  # > default 5
        rules = {a["rule"] for a in check_anomalies()["anomalies"]}
        assert "stale_doing_task" in rules


def _claim(task_id: int, agent: str, from_status: str = "open") -> None:
    """Force a task through `from_status` back to 'doing' under a new claimant,
    so the resulting event's summary is '<from_status>->doing' (a claim)."""
    token = _as_agent(agent)
    try:
        if from_status != "open":
            task_update(task_id, status=from_status)
        task_update(task_id, status="doing", notes="claim metadata is descriptive")
        task_update(task_id, status="open")  # release, ready for the next claim
    finally:
        config.agent_identity_var.reset(token)


class TestReviewWait:
    """review_wait: a task stuck '[waiting-for: review]' with no outcome."""

    @staticmethod
    def _hits():
        return [a for a in check_anomalies()["anomalies"] if a["rule"] == "review_wait"]

    def _waiting_task(self, notes="[waiting-for: review]", hours_old=12.0):
        r = task_add("needs eyes", notes=notes)
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(hours_old / 24.0))
        return r

    def test_triggers_past_threshold(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        r = self._waiting_task(hours_old=12.0)
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"
        assert "needs eyes" in hits[0]["detail"]
        assert "review pipeline" in hits[0]["detail"]

    def test_fresh_task_is_quiet(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        self._waiting_task(hours_old=2.0)
        assert self._hits() == []

    def test_review_outcome_clears(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        self._waiting_task(
            notes="[waiting-for: review]\n[review: approved] looks good",
            hours_old=12.0,
        )
        assert self._hits() == []

    def test_done_task_is_quiet(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        r = self._waiting_task(hours_old=12.0)
        task_update(
            r["id"],
            status="done",
            notes="[waiting-for: review]\n[review: no-merge] intentionally completed locally",
            actor="test-agent",
        )
        _backdate_task(r["id"], _iso_days_ago(0.5))
        assert self._hits() == []

    def test_env_override_and_garbage_fallback(self, monkeypatch):
        self._waiting_task(hours_old=12.0)
        # raised threshold: quiet
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "48")
        assert self._hits() == []
        # garbage / non-positive values fall back to the 6h default: triggers
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "not-a-number")
        assert len(self._hits()) == 1
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "-4")
        assert len(self._hits()) == 1

    def test_documentation_mention_in_backticks_is_quiet(self, monkeypatch):
        # Task #133's exact false-positive shape (originally found on #129):
        # notes that describe the annotation as prose, wrapped in backticks,
        # rather than marking real task state.
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        self._waiting_task(
            notes=(
                "REMAINING: harden the rule so it only matches the annotation as a "
                "real task-state marker, not the literal `[waiting-for: review]` "
                "token that appears here as documentation."
            ),
            hours_old=12.0,
        )
        assert self._hits() == []

    def test_real_marker_alongside_a_backticked_mention_still_triggers(self, monkeypatch):
        # A backtick-wrapped mention must not mask an actual, unquoted marker
        # sitting elsewhere in the same notes.
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        r = self._waiting_task(
            notes=(
                "[waiting-for: review]\n\n"
                "(see the `[waiting-for: review]` rule doc for what this means)"
            ),
            hours_old=12.0,
        )
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"

    def test_backticked_review_outcome_does_not_clear_a_real_marker(self, monkeypatch):
        # Symmetric case on the clearing side: a backtick-wrapped `[review:`
        # mention (documentation) must not suppress a genuine open marker.
        monkeypatch.setenv("UA_ANOMALY_REVIEW_WAIT_HOURS", "6")
        r = self._waiting_task(
            notes=(
                "[waiting-for: review]\n\n"
                "note: outcomes are recorded with a `[review: ...]` annotation."
            ),
            hours_old=12.0,
        )
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"


class TestUnverifiedDone:
    """unverified_done: a '[code-task]' task marked done with no evidence."""

    @staticmethod
    def _hits():
        return [a for a in check_anomalies()["anomalies"] if a["rule"] == "unverified_done"]

    def _done_code_task(self, notes="[code-task] fix the bug", evidence_ref=None):
        r = task_add("ship the fix", notes=notes)
        if evidence_ref is not None:
            # Bypass task_update's own write-time guard (tested separately in
            # test_task_completion_consistency.py) so this rule's backfill
            # posture — catching rows the guard didn't see — is exercised
            # directly, the same way test_reasserting_done_backfills a
            # pre-guard NULL by writing straight to the row.
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE tasks SET status='done', evidence_ref=? WHERE id=?",
                    (evidence_ref, r["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            conn = storage.connect()
            try:
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (r["id"],))
                conn.commit()
            finally:
                conn.close()
        return r

    def test_triggers_for_marked_task_with_no_evidence(self):
        r = self._done_code_task()
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"
        assert "code-task" in hits[0]["detail"]

    def test_unmarked_done_task_is_quiet(self):
        self._done_code_task(notes="just a normal task, no marker")
        assert self._hits() == []

    def test_evidence_ref_clears(self):
        self._done_code_task(evidence_ref="commit abc1234")
        assert self._hits() == []

    def test_review_completion_marker_clears(self):
        self._done_code_task(
            notes="[code-task] fix the bug\n[merged commit: abc1234 @ 2026-08-10T00:00:00Z]"
        )
        assert self._hits() == []

    def test_non_done_marked_task_is_quiet(self):
        # The rule only fires on status='done' — an open/doing [code-task]
        # task hasn't claimed completion yet, so there's nothing to verify.
        task_add("still working on it", notes="[code-task] fix the bug")
        assert self._hits() == []

    def test_documentation_mention_in_backticks_is_quiet(self):
        self._done_code_task(
            notes=(
                "REMAINING: harden the rule so it only matches the annotation as a "
                "real task-state marker, not the literal `[code-task]` token that "
                "appears here as documentation."
            )
        )
        assert self._hits() == []


class TestReassignmentThrash:
    def test_triggers_at_threshold_and_reports_subject(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("thrashy")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert len(hits) == 1
        assert hits[0]["subject"] == f"task:{r['id']}"
        assert "3 times" in hits[0]["detail"]
        assert "3 distinct agent" in hits[0]["detail"]

    def test_understood_done_task_can_be_suppressed(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("understood thrash")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        task_update(
            r["id"],
            status="done",
            notes="[anomaly: understood]\n\nBenign churn understood; keep hidden from the UI.",
            actor="test-agent",
        )
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert hits == []

    @pytest.mark.parametrize("terminal", ["done", "cancelled"])
    def test_terminal_task_with_recent_churn_is_quiet(self, monkeypatch, terminal):
        # Task #1368: once a task is done/cancelled its claim churn is history,
        # not an actionable thrash — it must not keep paging for the rest of
        # the window, with or without an [anomaly: understood] marker.
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("finished after churn")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        task_update(r["id"], status=terminal, notes="shipped", actor="test-agent")
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert hits == []

    @pytest.mark.parametrize("active", ["doing", "blocked", "open"])
    def test_non_terminal_task_with_recent_churn_still_fires(self, monkeypatch, active):
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("still churning")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        if active != "doing":
            task_update(r["id"], status=active, notes="parked", actor="test-agent")
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert [h["subject"] for h in hits] == [f"task:{r['id']}"]

    def test_below_threshold_no_trigger(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        r = task_add("stable")
        for agent in ("A", "B"):
            _claim(r["id"], agent)
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert hits == []

    def test_respects_window(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("old thrash")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        # Push every recorded event for this task outside the window.
        for ev in _events(kind="task"):
            if ev["ref_id"] == str(r["id"]):
                _backdate_event(ev["id"], _iso_days_ago(10))
        hits = [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]
        assert hits == []


class TestReassignmentThrashAcknowledgement:
    """Task #1412: a LIVE task whose churn has a known cause needs an
    acknowledgement, without handing anyone a switch that hides new churn."""

    def _thrashy(self, monkeypatch, backdate: bool = True):
        """Claim a task three times, then date that churn an hour ago.

        Backdating is what keeps the assertions order-independent: with claims and
        an acknowledgement both stamped 'now', same-second equality decides the
        outcome and the test flakes on a second boundary.
        """
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_MIN_CLAIMS", "3")
        monkeypatch.setenv("UA_ANOMALY_REASSIGN_WINDOW_DAYS", "7")
        r = task_add("thrashy live task")
        for agent in ("A", "B", "C"):
            _claim(r["id"], agent)
        if backdate:
            self._backdate_claims(r["id"])
        return r

    def _ack(self, task_id, days_ago: float, rule: str = "task_reassignment_thrash"):
        task_update(
            task_id,
            notes=f"[anomaly: understood {rule} @ {_iso_days_ago(days_ago)} by Desktop_Agent5]",
            actor="Desktop_Agent5",
        )

    def _backdate_claims(self, task_id):
        for ev in _events(kind="task"):
            if ev["ref_id"] == str(task_id):
                _backdate_event(ev["id"], _iso_days_ago(1 / 24))

    def _hits(self):
        return [
            a for a in check_anomalies()["anomalies"] if a["rule"] == "task_reassignment_thrash"
        ]

    def test_live_task_ack_after_churn_is_suppressed(self, monkeypatch):
        # Churn an hour ago, acknowledgement half an hour ago: the ack covers it.
        r = self._thrashy(monkeypatch)
        self._ack(r["id"], 1 / 48)
        assert self._hits() == []

    def test_new_claim_after_the_ack_re_pages(self, monkeypatch):
        # The ack must not be a permanent switch: churn dated after it is not
        # covered, which is what makes the marker time-boxed rather than silent.
        r = self._thrashy(monkeypatch)
        self._ack(r["id"], 1 / 48)
        assert self._hits() == [], "ack covering the backdated churn must suppress"
        _claim(r["id"], "D")
        assert [h["subject"] for h in self._hits()] == [f"task:{r['id']}"]

    def test_ack_does_not_cover_the_claims_that_predate_it(self, monkeypatch):
        # Ack written before the churn it claims to explain: everything still fires.
        r = self._thrashy(monkeypatch)
        self._ack(r["id"], 2)
        assert [h["subject"] for h in self._hits()] == [f"task:{r['id']}"]

    def test_ack_naming_another_rule_is_ignored(self, monkeypatch):
        r = self._thrashy(monkeypatch)
        self._ack(r["id"], 1 / 48, rule="unverified_done")
        assert len(self._hits()) == 1

    def test_ack_without_a_rule_or_timestamp_is_ignored(self, monkeypatch):
        # The legacy bare marker is ambiguous and undated, so it cannot be shown
        # to cover the churn at hand: a live task keeps paging.
        r = self._thrashy(monkeypatch)
        task_update(r["id"], notes="[anomaly: understood]", actor="Desktop_Agent5")
        assert len(self._hits()) == 1

    def test_unparseable_ack_timestamp_keeps_paging(self, monkeypatch):
        # A malformed marker must fail toward visibility, never toward silence.
        r = self._thrashy(monkeypatch)
        task_update(
            r["id"],
            notes="[anomaly: understood task_reassignment_thrash @ not-a-timestamp by X]",
            actor="Desktop_Agent5",
        )
        assert len(self._hits()) == 1

    def test_latest_ack_wins_when_several_are_present(self, monkeypatch):
        # The oldest marker (two hours before the churn) must not be the one used.
        r = self._thrashy(monkeypatch)
        for days_ago in (2 / 24, 1 / 24, 0):
            self._ack(r["id"], days_ago)
        assert self._hits() == []

    def test_claim_in_the_same_second_as_the_ack_still_pages(self, monkeypatch):
        # At second resolution a same-second claim cannot be distinguished from
        # churn the ack was written to cover, so it keeps reporting.
        #
        # This only exercises that guarantee if the claims and the ack actually
        # land in the same second. Both are stamped from wall-clock time
        # (storage.now() for the claim events, _iso_days_ago(0) for the ack
        # marker here), so without freezing the clock a slow run can cross a
        # real second boundary between the two and the ack ends up strictly
        # after the last claim — silently testing a different case and
        # flaking (task #1513). Freeze both to the same instant so the
        # same-second case is what actually runs, every time.
        frozen = datetime.now(UTC).replace(microsecond=0)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz is None else frozen.astimezone(tz)

        monkeypatch.setattr(storage, "datetime", _FrozenDateTime)
        monkeypatch.setattr(sys.modules[__name__], "datetime", _FrozenDateTime)
        r = self._thrashy(monkeypatch, backdate=False)
        self._ack(r["id"], 0)
        assert len(self._hits()) == 1

    def test_future_dated_ack_is_ignored(self, monkeypatch):
        # A typo'd timestamp must not cover every claim until the typo is noticed.
        r = self._thrashy(monkeypatch)
        self._ack(r["id"], -1)  # negative => one day in the future
        assert len(self._hits()) == 1

    def test_anonymous_marker_is_ignored(self, monkeypatch):
        # "attributed" is part of the contract: without `by <agent>` there is no
        # one answerable for the acknowledgement.
        r = self._thrashy(monkeypatch)
        task_update(
            r["id"],
            notes=f"[anomaly: understood task_reassignment_thrash @ {_iso_days_ago(0)}]",
            actor="Desktop_Agent5",
        )
        assert len(self._hits()) == 1

    def test_backticked_marker_is_a_mention_not_an_ack(self, monkeypatch):
        # A task describing the marker (docs, notes quoting it) must not
        # acknowledge itself — same posture as `[waiting-for: review]`.
        r = self._thrashy(monkeypatch)
        task_update(
            r["id"],
            notes="Use `[anomaly: understood task_reassignment_thrash "
            f"@ {_iso_days_ago(0)} by X]` to acknowledge known churn.",
            actor="Desktop_Agent5",
        )
        assert len(self._hits()) == 1

    def test_whitespace_only_attribution_is_ignored(self, monkeypatch):
        # `by` followed by spaces names nobody, so it is not an acknowledgement.
        r = self._thrashy(monkeypatch)
        task_update(
            r["id"],
            notes=f"[anomaly: understood task_reassignment_thrash @ {_iso_days_ago(0)} by    ]",
            actor="Desktop_Agent5",
        )
        assert len(self._hits()) == 1

    def test_ack_cannot_resurrect_or_alter_a_terminal_task(self, monkeypatch):
        # The terminal filter runs first (#1368), so an ack can neither be
        # applied to a finished task nor change its state.
        r = self._thrashy(monkeypatch)
        task_update(
            r["id"],
            status="done",
            notes=f"[anomaly: understood task_reassignment_thrash @ {_iso_days_ago(0)} by X]",
            actor="test-agent",
        )
        assert self._hits() == []


class TestAgentNeverCompletes:
    def test_triggers_at_threshold_and_clears_on_completion(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS", "3")
        # Moving a task into doing passes the per-agent WIP cap (task #1886);
        # this test needs three concurrent ones from the same agent.
        monkeypatch.setenv("UA_AGENT_WIP_MAX", "3")
        ids = []
        for i in range(3):
            r = task_add(f"claimed{i}")
            token = _as_agent("A")
            try:
                task_update(r["id"], status="doing", notes="claim metadata is descriptive")
            finally:
                config.agent_identity_var.reset(token)
            ids.append(r["id"])

        def hits():
            return [
                a for a in check_anomalies()["anomalies"] if a["rule"] == "agent_never_completes"
            ]

        h = hits()
        assert len(h) == 1
        assert h[0]["subject"] == "agent:A"
        assert "3 distinct" in h[0]["detail"]

        # Completing one of the claimed tasks clears the rule.
        task_update(ids[0], status="done", actor="test-agent")
        assert hits() == []

    def test_below_threshold_no_trigger(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS", "3")
        for i in range(2):
            r = task_add(f"claimed{i}")
            token = _as_agent("A")
            try:
                task_update(r["id"], status="doing", notes="claim metadata is descriptive")
            finally:
                config.agent_identity_var.reset(token)
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "agent_never_completes"]
        assert hits == []

    def test_unattributed_claims_never_flag_an_agent(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS", "3")
        for i in range(3):
            r = task_add(f"claimed{i}")
            task_update(r["id"], status="doing")  # no claimed-by note -> agent None
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "agent_never_completes"]
        assert hits == []


class TestInstrumentation:
    """Phase B: the server.py registration wrapper, exercised directly here
    (unit tests call bare tool functions, so this table stays empty otherwise)."""

    def _samples(self):
        conn = storage.connect()
        try:
            return conn.execute("SELECT * FROM agent_perf_samples ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_records_success_latency_and_memory(self):
        def add(x, y):
            return x + y

        assert instrumented(add)(2, 3) == 5
        rows = self._samples()
        assert len(rows) == 1
        row = rows[0]
        assert row["tool"] == "add"
        assert row["ok"] == 1
        assert row["error"] is None
        assert row["latency_ms"] >= 0
        # rss sampling is best-effort but succeeds on Linux/macOS CI runners
        assert row["rss_bytes"] is None or row["rss_bytes"] > 0

    def test_records_error_and_reraises_unchanged(self):
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            instrumented(boom)()
        row = self._samples()[0]
        assert row["tool"] == "boom"
        assert row["ok"] == 0
        assert "kaboom" in row["error"]

    def test_preserves_signature_for_schema_introspection(self):
        import inspect

        def typed(a: int, b: str = "x") -> dict:
            """docstring"""
            return {}

        wrapped = instrumented(typed)
        assert wrapped.__name__ == "typed"
        assert wrapped.__doc__ == "docstring"
        assert inspect.signature(wrapped) == inspect.signature(typed)

    def test_perf_sample_write_failure_never_breaks_wrapped_call(self, monkeypatch):
        def raise_on_record(*args, **kwargs):
            raise RuntimeError("perf sink unavailable")

        monkeypatch.setattr(storage, "record_perf_sample", raise_on_record)
        assert instrumented(lambda: 42)() == 42

    def test_perf_sample_write_failure_never_breaks_error_path(self, monkeypatch):
        def raise_on_record(*args, **kwargs):
            raise RuntimeError("perf sink unavailable")

        monkeypatch.setattr(storage, "record_perf_sample", raise_on_record)

        def boom():
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            instrumented(boom)()


class TestPerfSummary:
    def test_shapes_and_per_tool_aggregation(self):
        def ok_call():
            return "done"

        def err_call():
            raise RuntimeError("x")

        ok = instrumented(ok_call)
        err = instrumented(err_call)
        ok()
        ok()
        with pytest.raises(RuntimeError):
            err()

        summary = perf_summary()
        assert set(summary) == {"window_hours", "by_tool", "memory_rss_bytes"}
        assert summary["window_hours"] == 24.0

        ok_stats = summary["by_tool"]["ok_call"]
        assert ok_stats["calls"] == 2
        assert ok_stats["errors"] == 0
        assert ok_stats["error_rate"] == 0.0
        assert (
            ok_stats["min_latency_ms"] <= ok_stats["avg_latency_ms"] <= ok_stats["max_latency_ms"]
        )

        err_stats = summary["by_tool"]["err_call"]
        assert err_stats["calls"] == 1
        assert err_stats["errors"] == 1
        assert err_stats["error_rate"] == 1.0

        mem = summary["memory_rss_bytes"]
        assert mem["count"] == 3
        assert mem["min"] <= mem["avg"] <= mem["max"]
        assert mem["latest"] is not None

    def test_empty_db_is_sane(self):
        summary = perf_summary()
        assert summary["by_tool"] == {}
        assert summary["memory_rss_bytes"] == {
            "count": 0,
            "avg": None,
            "min": None,
            "max": None,
            "latest": None,
        }

    def test_since_hours_window_excludes_old_samples(self):
        def sampled():
            return 1

        instrumented(sampled)()
        _backdate_perf_samples(_iso_days_ago(2))  # older than the default 24h window
        assert perf_summary()["by_tool"] == {}
        assert perf_summary(since_hours=72)["by_tool"]["sampled"]["calls"] == 1

    def test_non_positive_since_hours_falls_back_to_default(self):
        def sampled():
            return 1

        instrumented(sampled)()
        summary = perf_summary(since_hours=-5)
        assert summary["window_hours"] == 24.0
        assert summary["by_tool"]["sampled"]["calls"] == 1


def _insert_perf_sample(ts: str, tool: str = "task_list") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_perf_samples (ts, tool, latency_ms, ok, error, rss_bytes) "
            "VALUES (?, ?, 1.0, 1, NULL, NULL)",
            (ts, tool),
        )
        conn.commit()
    finally:
        conn.close()


def _perf_row_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM agent_perf_samples").fetchone()[0]
    finally:
        conn.close()


class TestPrunePerfSamples:
    def test_removes_only_rows_older_than_window(self, isolated_env):
        storage.init_db()
        _insert_perf_sample(_iso_days_ago(40))
        _insert_perf_sample(_iso_days_ago(20))
        _insert_perf_sample(_iso_days_ago(3))
        instrumented(lambda: 1)()  # a fresh "now" sample

        removed = storage.prune_perf_samples(older_than_days=14)

        assert removed == 2
        assert _perf_row_count() == 2  # the 3-day-old row + the fresh one

    def test_zero_days_keeps_everything(self, isolated_env):
        storage.init_db()
        _insert_perf_sample(_iso_days_ago(400))
        assert storage.prune_perf_samples(older_than_days=0) == 0
        assert _perf_row_count() == 1

    def test_default_window_reads_env(self, isolated_env, monkeypatch):
        storage.init_db()
        _insert_perf_sample(_iso_days_ago(10))
        monkeypatch.setenv("UA_PERF_SAMPLE_RETENTION_DAYS", "7")
        assert storage.prune_perf_samples() == 1
        assert _perf_row_count() == 0

    def test_tool_index_dropped_by_migration(self, isolated_env):
        storage.init_db()
        conn = storage.connect()
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_perf_tool ON agent_perf_samples(tool)"
            )
            conn.commit()
        finally:
            conn.close()
        storage.init_db()  # runs _migrate again
        conn = storage.connect()
        try:
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "idx_agent_perf_tool" not in names
        assert "idx_agent_perf_ts" in names


class TestCheckAnomaliesAndNotify:
    @staticmethod
    def _mock_notify(monkeypatch, calls):
        def fake(msg, timeout_s=60):
            calls.append(msg)
            return {"delivered": True, "chars": len(msg), "error": None}

        monkeypatch.setattr("unified_agent.tools.notify.notify", fake)

    def test_notifies_new_anomaly_then_dedupes_within_cooldown(self, monkeypatch):
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")

        result = check_anomalies_and_notify()
        assert result["notified"] is True
        assert result["newly_notified"] == [{"rule": "silent_agent", "subject": "agent:A"}]
        assert result["notify_error"] is None
        assert len(calls) == 1
        assert "silent_agent" in calls[0]

        # Re-checking immediately: already notified within the cooldown window.
        result2 = check_anomalies_and_notify()
        assert result2["notified"] is False
        assert result2["newly_notified"] == []
        assert len(result2["anomalies"]) == 1  # detection still reports it
        assert len(calls) == 1

    def test_recurrence_after_clearing_notifies_again(self, monkeypatch):
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        r = task_add("flappy")
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))

        check_anomalies_and_notify()
        assert len(calls) == 1

        # Clears: progressing the task bumps updated_at.
        task_update(r["id"], status="blocked")
        cleared = check_anomalies_and_notify()
        assert cleared["newly_notified"] == []
        assert len(calls) == 1

        # Recurs: goes stale again — notifies immediately even though the
        # (rule, subject) pair was already notified once, because its ledger
        # row was pruned when the anomaly cleared above.
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))
        recurred = check_anomalies_and_notify()
        assert recurred["newly_notified"] == [
            {"rule": "stale_doing_task", "subject": f"task:{r['id']}"}
        ]
        assert len(calls) == 2

    def test_cooldown_expiry_allows_renotify(self, monkeypatch):
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        monkeypatch.setenv("UA_ANOMALY_NOTIFY_COOLDOWN_HOURS", "1")
        r = task_add("flappy")
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))

        check_anomalies_and_notify()
        assert len(calls) == 1

        _backdate_notification("stale_doing_task", f"task:{r['id']}", _iso_days_ago(1))
        result = check_anomalies_and_notify()
        assert result["newly_notified"] == [
            {"rule": "stale_doing_task", "subject": f"task:{r['id']}"}
        ]
        assert len(calls) == 2

    def test_renotify_backoff_doubles_up_to_cap_and_resets_on_clear(self, monkeypatch):
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_STALE_DOING_DAYS", "5")
        monkeypatch.setenv("UA_ANOMALY_NOTIFY_COOLDOWN_HOURS", "2")
        r = task_add("persistent")
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))
        subject = f"task:{r['id']}"

        def count() -> int:
            conn = storage.connect()
            try:
                return conn.execute(
                    "SELECT notify_count FROM anomaly_notifications WHERE subject=?", (subject,)
                ).fetchone()["notify_count"]
            finally:
                conn.close()

        check_anomalies_and_notify()  # page 1 at once
        assert (len(calls), count()) == (1, 1)

        # After page 1 the gap is the 2 h base: 1 h is quiet, 3 h pages again.
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(1 / 24))
        check_anomalies_and_notify()
        assert len(calls) == 1
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(3 / 24))
        check_anomalies_and_notify()
        assert (len(calls), count()) == (2, 2)

        # After page 2 the gap doubles to 4 h: 3 h is quiet, 5 h pages.
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(3 / 24))
        check_anomalies_and_notify()
        assert len(calls) == 2
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(5 / 24))
        check_anomalies_and_notify()
        assert (len(calls), count()) == (3, 3)

        # A large count is capped at 24 h: 23 h is quiet, 25 h pages.
        conn = storage.connect()
        try:
            conn.execute("UPDATE anomaly_notifications SET notify_count=40")
            conn.commit()
        finally:
            conn.close()
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(23 / 24))
        check_anomalies_and_notify()
        assert len(calls) == 3
        _backdate_notification("stale_doing_task", subject, _iso_days_ago(25 / 24))
        check_anomalies_and_notify()
        assert len(calls) == 4

        # Clearing drops the row; a recurrence pages at once with a fresh count.
        task_update(r["id"], status="blocked")
        check_anomalies_and_notify()
        task_update(r["id"], status="doing")
        _backdate_task(r["id"], _iso_days_ago(6))
        check_anomalies_and_notify()
        assert (len(calls), count()) == (5, 1)

    def test_notify_count_migration_is_idempotent_and_additive(self):
        conn = storage.connect()
        try:
            conn.execute("DROP TABLE anomaly_notifications")
            conn.execute(
                "CREATE TABLE anomaly_notifications (rule TEXT NOT NULL, subject TEXT NOT NULL, "
                "first_seen TEXT NOT NULL, last_notified_at TEXT NOT NULL, "
                "PRIMARY KEY (rule, subject))"
            )
            conn.execute("INSERT INTO anomaly_notifications VALUES ('r', 's', 't', 't')")
            conn.commit()
        finally:
            conn.close()
        storage.init_db()
        storage.init_db()  # second run must not raise
        conn = storage.connect()
        try:
            row = conn.execute("SELECT notify_count FROM anomaly_notifications").fetchone()
        finally:
            conn.close()
        assert row["notify_count"] == 1

    def test_notify_failure_is_swallowed_and_reported(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")

        def boom(msg, timeout_s=60):
            raise RuntimeError("bridge down")

        monkeypatch.setattr("unified_agent.tools.notify.notify", boom)

        result = check_anomalies_and_notify()
        assert result["notified"] is False
        assert "bridge down" in result["notify_error"]
        assert len(result["anomalies"]) == 1  # detection is unaffected by notify failure

    def test_no_anomalies_produces_no_notification(self):
        result = check_anomalies_and_notify()
        assert result == {
            "anomalies": [],
            "notified": False,
            "newly_notified": [],
            "notify_error": None,
        }

    def test_new_anomaly_is_journaled(self, monkeypatch):
        """Task #117: every detection must be inspectable in the vault afterward."""
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")

        check_anomalies_and_notify()

        today = datetime.now().strftime("%Y-%m-%d")
        note = vault_read(f"Logs/{today}.md")
        assert "Anomaly check: 1 new/re-triggered finding(s)" in note["content"]
        assert "[silent_agent] agent:A" in note["content"]
        assert "unified-agent-cron" in note["content"]

    def test_no_anomalies_writes_no_journal_entry(self, tmp_path, monkeypatch):
        # Nothing to detect → no notify, no journal write, no Logs note created.
        result = check_anomalies_and_notify()
        assert result["newly_notified"] == []
        today = datetime.now().strftime("%Y-%m-%d")
        with pytest.raises(ToolError, match="no such note"):
            vault_read(f"Logs/{today}.md")

    def test_journal_failure_does_not_break_notify(self, monkeypatch):
        """Best-effort: a vault_journal failure must not mask the alert itself."""
        calls = []
        self._mock_notify(monkeypatch, calls)
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")

        def boom(**kw):
            raise RuntimeError("vault unavailable")

        monkeypatch.setattr("unified_agent.tools.vault.vault_journal", boom)

        result = check_anomalies_and_notify()
        assert result["notified"] is True
        assert result["notify_error"] is None
        assert len(calls) == 1

    def test_anomaly_cron_extraction_logic(self, monkeypatch):
        """Task #907: verify extraction logic used in deploy/anomaly-check-cron.sh."""
        calls = []
        self._mock_notify(monkeypatch, calls)

        # Baseline: clean environment, 0 anomalies
        result = check_anomalies_and_notify()
        new = len(result.get("newly_notified", []))
        active = len(result.get("anomalies", []))
        assert new == 0
        assert active == 0

        # Trigger an anomaly
        monkeypatch.setenv("UA_ANOMALY_SILENT_AGENT_DAYS", "2")
        _gone_silent("A")
        result = check_anomalies_and_notify()
        new = len(result.get("newly_notified", []))
        active = len(result.get("anomalies", []))
        assert new == 1
        assert active == 1

        # Re-check under cooldown: active=1, newly_notified=0
        result2 = check_anomalies_and_notify()
        new2 = len(result2.get("newly_notified", []))
        active2 = len(result2.get("anomalies", []))
        assert new2 == 0
        assert active2 == 1

        # Resilient to missing newly_notified or anomalies keys
        partial = {}
        assert len(partial.get("newly_notified", [])) == 0
        assert len(partial.get("anomalies", [])) == 0

    def test_anomaly_cron_script_source_and_syntax(self):
        """Task #907: regression guard for anomaly-check-cron.sh source and bash syntax."""
        repo_root = Path(__file__).resolve().parents[1]
        cron_sh = repo_root / "deploy" / "anomaly-check-cron.sh"
        content = cron_sh.read_text()

        assert "new = len(result.get('newly_notified', []))" in content
        assert "active = len(result.get('anomalies', []))" in content
        assert "check_anomalies_and_notify" in content

        res = subprocess.run(["bash", "-n", str(cron_sh)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_anomaly_cron_script_execution_and_reporting(self, tmp_path):
        """Task #907: verify anomaly-check-cron.sh runs cleanly and reports stats."""
        repo_root = Path(__file__).resolve().parents[1]
        cron_sh = repo_root / "deploy" / "anomaly-check-cron.sh"

        env = dict(os.environ)
        env["UA_HOME"] = str(tmp_path)
        env["UA_REPO"] = str(repo_root)

        subprocess.run(
            ["uv", "run", "python3", "-c", "from unified_agent import storage; storage.init_db()"],
            env=env,
            check=True,
            capture_output=True,
        )

        res = subprocess.run(["bash", str(cron_sh)], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert "anomaly-check: 0 active anomaly(s), 0 newly notified" in res.stdout

    def test_anomaly_cron_script_skips_when_no_local_memory_db(self, tmp_path):
        """Task #907: verify anomaly-check-cron.sh exits cleanly when local DB is retired."""
        repo_root = Path(__file__).resolve().parents[1]
        cron_sh = repo_root / "deploy" / "anomaly-check-cron.sh"
        (tmp_path / "NO_LOCAL_MEMORY_DB").write_text("sentinel\n")

        env = dict(os.environ)
        env["UA_HOME"] = str(tmp_path)
        env["UA_REPO"] = str(repo_root)

        res = subprocess.run(["bash", str(cron_sh)], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert "NO_LOCAL_MEMORY_DB" in res.stdout
        assert "0 active anomaly(s)" in res.stdout

    def test_anomaly_watchdog_systemd_and_launchagent_config(self):
        """Task #907: verify systemd timer and LaunchAgent plist configuration."""
        repo_root = Path(__file__).resolve().parents[1]

        # Systemd timer
        timer_file = repo_root / "deploy" / "systemd" / "ua-anomaly-check.timer"
        timer_text = timer_file.read_text()
        assert "OnCalendar=*:0/30" in timer_text

        # Systemd service
        service_file = repo_root / "deploy" / "systemd" / "ua-anomaly-check.service"
        service_text = service_file.read_text()
        assert "EnvironmentFile=-%h/.unified_agent/service.env" in service_text
        assert "anomaly-check-cron.sh" in service_text

        # macOS LaunchAgent plist
        plist_file = repo_root / "deploy" / "mac-hub" / "com.unified-agent.anomaly-check.plist"
        require_exported_paths(repo_root, plist_file)
        data = plistlib.loads(plist_file.read_bytes())
        assert data.get("StartInterval") == 1800
        assert "run-anomaly-check.sh" in data["ProgramArguments"][0]

        # macOS LaunchAgent runner script
        mac_sh = repo_root / "deploy" / "mac-hub" / "run-anomaly-check.sh"
        assert mac_sh.exists()
        res = subprocess.run(["bash", "-n", str(mac_sh)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_weekly_export_systemd_and_launchagent_config(self):
        """Task #910: systemd timer/service + macOS LaunchAgent plist/runner all present and consistent."""
        repo_root = Path(__file__).resolve().parents[1]

        timer_text = (repo_root / "deploy" / "systemd" / "ua-weekly-export.timer").read_text()
        assert "OnCalendar=Sun *-*-* 02:00:00" in timer_text

        service_text = (repo_root / "deploy" / "systemd" / "ua-weekly-export.service").read_text()
        assert "EnvironmentFile=-%h/.unified_agent/service.env" in service_text
        assert "weekly-export-cron.sh" in service_text

        cron_sh = repo_root / "deploy" / "weekly-export-cron.sh"
        assert cron_sh.exists()
        res = subprocess.run(["bash", "-n", str(cron_sh)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

        plist_file = repo_root / "deploy" / "mac-hub" / "com.unified-agent.weekly-export.plist"
        require_exported_paths(repo_root, plist_file)
        data = plistlib.loads(plist_file.read_bytes())
        assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 2, "Minute": 0}
        assert "run-weekly-export.sh" in data["ProgramArguments"][0]

        mac_sh = repo_root / "deploy" / "mac-hub" / "run-weekly-export.sh"
        assert mac_sh.exists()
        res = subprocess.run(["bash", "-n", str(mac_sh)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


class TestPhaseDRules:
    """db_size_growth / approval_queue_age / bridge_disconnect (task 64 phase D)."""

    def _add_size_sample(self, ts: str, nbytes: int) -> None:
        conn = storage.connect()
        try:
            conn.execute("INSERT INTO db_size_samples (ts, bytes) VALUES (?, ?)", (ts, nbytes))
            conn.commit()
        finally:
            conn.close()

    def test_db_size_growth_triggers_over_threshold(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_DB_GROWTH_MB", "10")
        monkeypatch.setenv("UA_ANOMALY_DB_GROWTH_WINDOW_DAYS", "7")
        self._add_size_sample(_iso_days_ago(6), 1 * 1024 * 1024)
        self._add_size_sample(_iso_days_ago(0.1), 20 * 1024 * 1024)
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "db_size_growth"]
        assert len(hits) == 1
        assert hits[0]["subject"] == "db:memory.db"
        assert "19.0 MB" in hits[0]["detail"]

    def test_db_size_growth_quiet_under_threshold_or_one_sample(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_DB_GROWTH_MB", "10")
        # single sample in window: silent (no baseline)
        self._add_size_sample(_iso_days_ago(0.1), 500 * 1024 * 1024)
        assert [a for a in check_anomalies()["anomalies"] if a["rule"] == "db_size_growth"] == []
        # growth below threshold: silent; samples outside the window are ignored
        self._add_size_sample(_iso_days_ago(30), 1)
        self._add_size_sample(_iso_days_ago(6), 495 * 1024 * 1024)
        assert [a for a in check_anomalies()["anomalies"] if a["rule"] == "db_size_growth"] == []

    def test_record_db_size_sample_appends_and_prunes(self):
        from unified_agent.monitor import record_db_size_sample

        self._add_size_sample(_iso_days_ago(120), 123)  # beyond retention
        record_db_size_sample()
        conn = storage.connect()
        try:
            rows = conn.execute("SELECT ts, bytes FROM db_size_samples ORDER BY ts").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1  # old row pruned, fresh sample recorded
        assert rows[0]["bytes"] >= 0

    def test_approval_queue_age_aggregates_stale_pending(self, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_APPROVAL_AGE_HOURS", "12")
        conn = storage.connect()
        try:
            for age_days, status in ((1.0, "pending"), (2.0, "pending"), (3.0, "approved")):
                conn.execute(
                    "INSERT INTO approvals (tool, payload, status, created_at, updated_at) "
                    "VALUES ('run_cmd', '{}', ?, ?, ?)",
                    (status, _iso_days_ago(age_days), _iso_days_ago(age_days)),
                )
            # a fresh pending row must not count
            conn.execute(
                "INSERT INTO approvals (tool, payload, status, created_at, updated_at) "
                "VALUES ('run_cmd', '{}', 'pending', ?, ?)",
                (_iso_days_ago(0.01), _iso_days_ago(0.01)),
            )
            conn.commit()
        finally:
            conn.close()
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "approval_queue_age"]
        assert len(hits) == 1
        assert hits[0]["subject"] == "approvals:queue"
        assert "2 pending approval(s)" in hits[0]["detail"]

    def test_bridge_disconnect_silent_when_not_deployed(self):
        # isolated_env workspace has no bridge project and no UA_BRIDGE_DIR
        assert [a for a in check_anomalies()["anomalies"] if a["rule"] == "bridge_disconnect"] == []

    def test_bridge_disconnect_flags_missing_script_when_expected(self, tmp_path, monkeypatch):
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        monkeypatch.setenv("UA_BRIDGE_DIR", str(bridge))
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "bridge_disconnect"]
        assert len(hits) == 1
        assert "send script missing" in hits[0]["detail"]

    def test_bridge_disconnect_flags_missing_bun(self, tmp_path, monkeypatch):
        import unified_agent.monitor as monitor_mod

        bridge = tmp_path / "bridge"
        (bridge / "src").mkdir(parents=True)
        (bridge / "src" / "send.ts").write_text("// send")
        monkeypatch.setenv("UA_BRIDGE_DIR", str(bridge))
        monkeypatch.setattr(monitor_mod.shutil, "which", lambda name: None)
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "bridge_disconnect"]
        assert len(hits) == 1
        assert "bun executable not on PATH" in hits[0]["detail"]

    def test_notify_path_records_size_sample(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod, "_deliver", lambda msg: (True, None), raising=False)
        check_anomalies_and_notify()
        conn = storage.connect()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM db_size_samples").fetchone()["n"]
        finally:
            conn.close()
        assert n == 1


class TestServiceHealthRules:
    """service_down / service_crash_loop over systemd --user units.

    isolated_env (tests/conftest.py) sets UA_ANOMALY_SERVICES="" for every
    test by default, so these tests must opt back in explicitly via
    monkeypatch.setenv to exercise the rules.
    """

    @pytest.fixture(autouse=True)
    def _force_linux_systemd_probe(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "linux")

    @staticmethod
    def _fake_run(active_by_unit=None, restarts_by_unit=None, load_by_unit=None, raise_exc=None):
        """Build a subprocess.run stand-in matching the single systemctl
        invocation the rule makes: `show <unit>
        --property=LoadState,ActiveState,NRestarts`."""
        active_by_unit = active_by_unit or {}
        restarts_by_unit = restarts_by_unit or {}
        load_by_unit = load_by_unit or {}

        def fake_run(cmd, capture_output, text, timeout):
            if raise_exc is not None:
                raise raise_exc
            unit = cmd[3]
            proc = type("Proc", (), {})()
            proc.stdout = (
                f"LoadState={load_by_unit.get(unit, 'loaded')}\n"
                f"ActiveState={active_by_unit.get(unit, 'active')}\n"
                f"NRestarts={restarts_by_unit.get(unit, 0)}\n"
            )
            proc.returncode = 0
            return proc

        return fake_run

    def test_not_active_triggers_service_down(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(active_by_unit={"gbrain": "failed"}),
        )
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "service_down"]
        assert len(hits) == 1
        assert hits[0]["subject"] == "service:gbrain"
        assert "gbrain is failed" in hits[0]["detail"]

    def test_high_restart_count_triggers_crash_loop(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setenv("UA_ANOMALY_SERVICE_RESTARTS_MAX", "3")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(restarts_by_unit={"gbrain": 49}),
        )
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "service_crash_loop"]
        assert len(hits) == 1
        assert hits[0]["subject"] == "service:gbrain"
        assert "49 times" in hits[0]["detail"]

    def test_active_and_low_restarts_is_quiet(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(active_by_unit={"gbrain": "active"}, restarts_by_unit={"gbrain": 1}),
        )
        hits = [
            a
            for a in check_anomalies()["anomalies"]
            if a["rule"] in ("service_down", "service_crash_loop")
        ]
        assert hits == []

    def test_unloadable_unit_is_silent(self, monkeypatch):
        """A unit systemd has never heard of (not installed on this host —
        e.g. a CI runner) must never produce a finding, even if the stub
        ActiveState/NRestarts values would otherwise trigger both rules."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setenv("UA_ANOMALY_SERVICE_RESTARTS_MAX", "3")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(
                load_by_unit={"gbrain": "not-found"},
                active_by_unit={"gbrain": "inactive"},
                restarts_by_unit={"gbrain": 49},
            ),
        )
        hits = [
            a
            for a in check_anomalies()["anomalies"]
            if a["rule"] in ("service_down", "service_crash_loop")
        ]
        assert hits == []

    def test_missing_systemctl_is_silent_and_never_raises(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain,unified-agent")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(raise_exc=FileNotFoundError("systemctl not found")),
        )
        result = check_anomalies()
        hits = [
            a for a in result["anomalies"] if a["rule"] in ("service_down", "service_crash_loop")
        ]
        assert hits == []

    def test_timeout_is_silent_and_never_raises(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(raise_exc=subprocess.TimeoutExpired(cmd="systemctl", timeout=5)),
        )
        hits = [
            a
            for a in check_anomalies()["anomalies"]
            if a["rule"] in ("service_down", "service_crash_loop")
        ]
        assert hits == []

    def test_empty_services_env_disables_rule(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "")

        def fail_if_called(*args, **kwargs):
            raise AssertionError("systemctl should not be invoked when UA_ANOMALY_SERVICES=''")

        monkeypatch.setattr(monitor_mod.subprocess, "run", fail_if_called)
        hits = [
            a
            for a in check_anomalies()["anomalies"]
            if a["rule"] in ("service_down", "service_crash_loop")
        ]
        assert hits == []

    def test_default_services_are_unified_agent_and_gbrain(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        # isolated_env's autouse fixture forces UA_ANOMALY_SERVICES="" (see
        # class docstring); undo that here to observe the real default list.
        monkeypatch.delenv("UA_ANOMALY_SERVICES", raising=False)
        seen_units = []

        def record_units(cmd, capture_output, text, timeout):
            seen_units.append(cmd[3])
            proc = type("Proc", (), {})()
            proc.stdout = "LoadState=loaded\nActiveState=active\nNRestarts=0\n"
            proc.returncode = 0
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", record_units)
        check_anomalies()
        assert set(seen_units) == {"unified-agent", "gbrain"}

    def test_restart_threshold_env_override(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_ANOMALY_SERVICES", "gbrain")
        monkeypatch.setenv("UA_ANOMALY_SERVICE_RESTARTS_MAX", "100")
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_run(restarts_by_unit={"gbrain": 49}),
        )
        hits = [a for a in check_anomalies()["anomalies"] if a["rule"] == "service_crash_loop"]
        assert hits == []  # 49 <= override threshold 100


class TestHermesEnvRule:
    """hermes_env: filesystem probe of the hermes-agent venv (task 92)."""

    @staticmethod
    def _hits():
        return [a for a in check_anomalies()["anomalies"] if a["rule"] == "hermes_env"]

    @staticmethod
    def _make_install(tmp_path, *, venv=True, mcp=True):
        root = tmp_path / "hermes-agent"
        root.mkdir()
        if venv:
            site = root / "venv" / "lib" / "python3.11" / "site-packages"
            site.mkdir(parents=True)
            if mcp:
                (site / "mcp").mkdir()
        return root

    def test_absent_install_dir_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", str(tmp_path / "nope"))
        assert self._hits() == []

    def test_empty_env_disables_even_with_broken_install(self, tmp_path, monkeypatch):
        self._make_install(tmp_path, venv=False)
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", "")
        assert self._hits() == []

    def test_missing_venv_flags(self, tmp_path, monkeypatch):
        root = self._make_install(tmp_path, venv=False)
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", str(root))
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == "hermes:venv"
        assert "no venv/" in hits[0]["detail"]

    def test_venv_without_mcp_flags(self, tmp_path, monkeypatch):
        root = self._make_install(tmp_path, mcp=False)
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", str(root))
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == "hermes:venv"
        assert "[mcp] optional extra" in hits[0]["detail"]

    def test_healthy_install_is_clean_and_clears(self, tmp_path, monkeypatch):
        root = self._make_install(tmp_path, mcp=False)
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", str(root))
        assert len(self._hits()) == 1
        # Reinstalling the extra clears the finding on the next check.
        (root / "venv" / "lib" / "python3.11" / "site-packages" / "mcp").mkdir()
        assert self._hits() == []

    def test_windows_layout_site_packages_recognized(self, tmp_path, monkeypatch):
        root = tmp_path / "hermes-agent"
        (root / "venv" / "Lib" / "site-packages" / "mcp").mkdir(parents=True)
        monkeypatch.setenv("UA_ANOMALY_HERMES_DIR", str(root))
        assert self._hits() == []


class TestHostLowMemoryRule:
    """host_low_memory: /proc/meminfo probe (System Leverage Plan 2026-07-20,
    motivated by the same day's desktop freeze — free RAM had a 4-hour
    visible run-up in the logs before a snapd watchdog SIGABRT, unwatched)."""

    @staticmethod
    def _hits():
        return [a for a in check_anomalies()["anomalies"] if a["rule"] == "host_low_memory"]

    @staticmethod
    def _write_meminfo(tmp_path, *, total_kb: int, available_kb: int) -> str:
        path = tmp_path / "meminfo"
        path.write_text(
            f"MemTotal:       {total_kb} kB\n"
            f"MemFree:        {available_kb // 2} kB\n"
            f"MemAvailable:   {available_kb} kB\n"
        )
        return str(path)

    def test_disabled_by_default_in_tests(self):
        # conftest's isolated_env sets UA_ANOMALY_MEMINFO_PATH="" so the real
        # host's memory doesn't leak into unrelated tests' anomaly lists.
        assert self._hits() == []

    def test_missing_file_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", str(tmp_path / "nope"))
        assert self._hits() == []

    def test_malformed_file_is_silent(self, tmp_path, monkeypatch):
        path = tmp_path / "meminfo"
        path.write_text("garbage, no colons here\n")
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", str(path))
        assert self._hits() == []

    def test_low_available_memory_flags(self, tmp_path, monkeypatch):
        path = self._write_meminfo(tmp_path, total_kb=16_000_000, available_kb=1_000_000)  # 6.25%
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", path)
        hits = self._hits()
        assert len(hits) == 1
        assert hits[0]["subject"] == "host:memory"
        assert "6.2" in hits[0]["detail"] or "6.3" in hits[0]["detail"]
        assert "threshold 15%" in hits[0]["detail"]

    def test_healthy_memory_is_clean(self, tmp_path, monkeypatch):
        path = self._write_meminfo(tmp_path, total_kb=16_000_000, available_kb=8_000_000)  # 50%
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", path)
        assert self._hits() == []

    def test_threshold_override(self, tmp_path, monkeypatch):
        # 20% available clears the default 15% threshold but trips a raised one.
        path = self._write_meminfo(tmp_path, total_kb=16_000_000, available_kb=3_200_000)
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", path)
        assert self._hits() == []
        monkeypatch.setenv("UA_ANOMALY_LOW_MEM_PERCENT", "25")
        assert len(self._hits()) == 1

    def test_clears_once_memory_recovers(self, tmp_path, monkeypatch):
        path = self._write_meminfo(tmp_path, total_kb=16_000_000, available_kb=1_000_000)
        monkeypatch.setenv("UA_ANOMALY_MEMINFO_PATH", path)
        assert len(self._hits()) == 1
        self._write_meminfo(tmp_path, total_kb=16_000_000, available_kb=8_000_000)
        # _write_meminfo wrote a *new* temp path object above but reused the
        # same on-disk filename, so the env var still points at the live file.
        assert self._hits() == []


class TestLaunchdServiceProbe:
    """Darwin maps systemd-shaped unit names onto LaunchAgent labels."""

    def test_running_executor_launchagent_is_active(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.executor-agy"]
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                "gui/501/com.unified-agent.executor-agy = {\n"
                "	state = running\n"
                "	path = /tmp/com.unified-agent.executor-agy.plist\n"
                "}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("unified-agent-executor.service", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": None,
        }

    def test_running_launchagent_with_coalition_states_is_active(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.hub"]
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                "gui/501/com.unified-agent.hub = {\n"
                "\tstate = running\n"
                "\tresource coalition = {\n"
                "\t\tstate = active\n"
                "\t}\n"
                "\tjetsam coalition = {\n"
                "\t\tstate = active\n"
                "\t}\n"
                "}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("unified-agent.service", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": None,
        }

    def test_idle_periodic_launchagent_with_clean_exit_is_active(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.dispatch-tick"]
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                "gui/501/com.unified-agent.dispatch-tick = {\n"
                "\tstate = not running\n"
                "\truns = 262\n"
                "\tlast exit code = 0\n"
                "\trun interval = 120 seconds\n"
                "}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("ua-dispatch-tick.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": "0",
        }

    def test_idle_periodic_launchagent_with_failed_exit_is_inactive(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                "gui/501/com.unified-agent.dispatch-tick = {\n"
                "\tstate = not running\n"
                "\tlast exit code = 1\n"
                "\trun interval = 120 seconds\n"
                "}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("ua-dispatch-tick.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "1",
        }

    def test_idle_calendar_launchagent_with_clean_exit_is_active(self, monkeypatch):
        """A mapped calendar timer stays healthy between successful runs."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_calendar_run(cmd, capture_output, text, timeout):
            assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.sleep-pass"]
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                "gui/501/com.unified-agent.sleep-pass = {\n"
                "\tstate = not running\n"
                "\truns = 4\n"
                "\tlast exit code = 0\n"
                "\tevent triggers = {\n\t}\n}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_calendar_run)
        props = monitor_mod._service_probe("ua-sleep-pass.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": "0",
        }

    def test_missing_launchagent_is_not_found(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 113
            proc.stdout = ""
            proc.stderr = 'Could not find service "com.unified-agent.hub" in domain'
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        assert monitor_mod._service_probe("unified-agent.service", 1) == {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "NRestarts": "0",
        }

    def test_unparseable_launchctl_is_unavailable(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = "loaded = 1\n"
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        assert monitor_mod._service_probe("unified-agent.service", 1) is None

    def test_linux_only_timer_does_not_spawn_launchctl(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")

        def fake_run(*args, **kwargs):
            raise AssertionError("linux-only units must not call launchctl")

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("ua-backup.timer", 1)
        assert props["LoadState"] == "not-found"
        assert props["ActiveState"] == "inactive"


_CONSOLIDATE_LABEL = "com.unified-agent.consolidate-proposal"


class TestLaunchdFreshInstallNeverRun:
    """A freshly-installed calendar timer with zero runs must not read as
    MISCONFIGURED just because its first fire hasn't happened yet — only a
    schedule that is already overdue for a run is a real finding. Regression
    coverage for the 2026-09-04 ua-consolidate-proposal false positive: it was
    installed the same week as its Monday-00:00 trigger, after that week's
    slot had already passed, so `runs = 0` / `last exit code = (never
    exited)` looked identical to a broken job.

    The "since" anchor is `storage.launchd_first_seen`, not the plist's own
    mtime: a redeploy that rewrites these timer plists without a
    bootout+bootstrap cycle (deploy/mac-hub/setup-agents-and-executors.sh
    does exactly this) can reset the file's mtime long after the job's real
    bootstrap, which would make an already-long-broken timer read as freshly
    installed. Each test seeds (or deliberately omits) that anchor directly
    rather than manipulating file timestamps.
    """

    def _write_plist(self, tmp_path, schedule: dict):
        path = tmp_path / f"{_CONSOLIDATE_LABEL}.plist"
        plistlib.dump({"StartCalendarInterval": schedule}, path.open("wb"))
        return path

    def _fake_never_run_probe(self, plist_path):
        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                f"gui/501/{_CONSOLIDATE_LABEL} = {{\n"
                "\tstate = not running\n"
                f"\tpath = {plist_path}\n"
                "\truns = 0\n"
                "\tlast exit code = (never exited)\n"
                "\tevent triggers = {\n\t}\n}\n"
            )
            proc.stderr = ""
            return proc

        return fake_run

    def test_first_ever_observation_is_active_and_anchors_first_seen(self, tmp_path, monkeypatch):
        """The very first time this process ever sees a label loaded with
        zero runs, it cannot possibly be overdue relative to itself — it
        must report active and persist the anchor future observations use."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        # Even a schedule that would read as "overdue" by any wall-clock
        # measure doesn't matter yet — there's no prior anchor to compare to.
        plist_path = self._write_plist(tmp_path, {"Hour": 0, "Minute": 0})
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))

        assert monitor_mod.storage.get_launchd_first_seen(_CONSOLIDATE_LABEL) is None
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }
        assert monitor_mod.storage.get_launchd_first_seen(_CONSOLIDATE_LABEL) is not None

    def test_not_yet_due_since_anchor_is_active(self, tmp_path, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        now = datetime.now()
        # A month value guaranteed not to match this calendar month, so the
        # earliest matching occurrence is necessarily still in the future
        # relative to an anchor of "just now" — no run is overdue yet.
        future_month = (now.month % 12) + 1
        plist_path = self._write_plist(
            tmp_path, {"Month": future_month, "Day": 1, "Hour": 0, "Minute": 0}
        )
        monitor_mod.storage.record_launchd_first_seen(_CONSOLIDATE_LABEL, now.isoformat())
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_definitely_overdue_since_anchor_is_inactive(self, tmp_path, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        # A daily Hour/Minute schedule first observed well over a year ago
        # has necessarily missed a run if it still shows zero runs — overdue
        # even after allowing for the install-then-enable-later grace window.
        plist_path = self._write_plist(tmp_path, {"Hour": 0, "Minute": 0})
        anchor = (datetime.now() - timedelta(days=400)).isoformat()
        monitor_mod.storage.record_launchd_first_seen(_CONSOLIDATE_LABEL, anchor)
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_overdue_within_enable_later_grace_window_is_unknown(self, tmp_path, monkeypatch):
        """A one-day-old anchor with zero runs on a daily schedule reads as
        overdue by raw timing, but still within `_LAUNCHD_ENABLE_GRACE` — the
        residual single-observation lag a first-ever look could introduce.
        That ambiguity must read as UNKNOWN, never a confident MISCONFIGURED,
        until the grace window itself has passed."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        plist_path = self._write_plist(tmp_path, {"Hour": 0, "Minute": 0})
        anchor = (datetime.now() - timedelta(days=1)).isoformat()
        monitor_mod.storage.record_launchd_first_seen(_CONSOLIDATE_LABEL, anchor)
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "unknown",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_unreadable_plist_is_conservatively_inactive(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)
        monkeypatch.setattr(
            monitor_mod.subprocess,
            "run",
            self._fake_never_run_probe("/nonexistent/does-not-exist.plist"),
        )
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    @pytest.mark.parametrize(
        "bad_spec",
        [
            {"Hour": 99, "Minute": 0},
            {"Hour": 0, "Minute": "not-a-number"},
        ],
    )
    def test_malformed_calendar_fields_are_inactive_not_a_crash(
        self, tmp_path, monkeypatch, bad_spec
    ):
        """The loaded job's on-disk plist can be edited or redeployed without
        a bootout+bootstrap cycle, so its content isn't guaranteed to be
        well-formed relative to what's actually running. An out-of-range or
        wrong-type Hour/Minute must degrade to "never matches" (which, once
        there's an anchor old enough to be unambiguously overdue, resolves to
        a real finding), never raise out of system_doctor/check_anomalies."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        plist_path = self._write_plist(tmp_path, bad_spec)
        anchor = (datetime.now() - timedelta(days=400)).isoformat()
        monitor_mod.storage.record_launchd_first_seen(_CONSOLIDATE_LABEL, anchor)
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_nonzero_last_exit_code_stays_inactive_regardless_of_anchor(
        self, tmp_path, monkeypatch
    ):
        """A real failed run must never be excused by "not due yet" logic —
        that logic only ever applies to a job that has literally never run.
        No first_seen anchor is recorded at all here, proving it's the
        non-zero exit code gating this, not merely an anchor's absence."""
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)

        now = datetime.now()
        future_month = (now.month % 12) + 1
        plist_path = self._write_plist(
            tmp_path, {"Month": future_month, "Day": 1, "Hour": 0, "Minute": 0}
        )

        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                f"gui/501/{_CONSOLIDATE_LABEL} = {{\n"
                "\tstate = not running\n"
                f"\tpath = {plist_path}\n"
                "\truns = 1\n"
                "\tlast exit code = 1\n"
                "\tevent triggers = {\n\t}\n}\n"
            )
            proc.stderr = ""
            return proc

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        props = monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "1",
        }


class TestLaunchdZeroRunsAfterReboot:
    """launchd resets `runs` and `last exit code` when a job reloads at boot,
    so a daily calendar timer observed between boot and its first post-boot
    fire reads identically to a never-run job — but the first-seen anchor
    still holds a pre-boot timestamp and makes it read as overdue (the
    2026-09-19 steward-audit / cluster-briefing MISCONFIGURED false positive
    on the Mac hub, task #1178). The anchor is therefore clamped to the last
    boot time: a zero-runs window cannot be older than the boot that reset
    the counter.
    """

    def _write_plist(self, tmp_path, schedule: dict):
        path = tmp_path / f"{_CONSOLIDATE_LABEL}.plist"
        plistlib.dump({"StartCalendarInterval": schedule}, path.open("wb"))
        return path

    def _fake_never_run_probe(self, plist_path):
        def fake_run(cmd, capture_output, text, timeout):
            proc = type("Proc", (), {})()
            proc.returncode = 0
            proc.stdout = (
                f"gui/501/{_CONSOLIDATE_LABEL} = {{\n"
                "\tstate = not running\n"
                f"\tpath = {plist_path}\n"
                "\truns = 0\n"
                "\tlast exit code = (never exited)\n"
                "\tevent triggers = {\n\t}\n}\n"
            )
            proc.stderr = ""
            return proc

        return fake_run

    def _probe_with(self, tmp_path, monkeypatch, anchor_age_days, boot, schedule):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.sys, "platform", "darwin")
        monkeypatch.setattr(monitor_mod.os, "getuid", lambda: 501)
        plist_path = self._write_plist(tmp_path, schedule)
        anchor = (datetime.now() - timedelta(days=anchor_age_days)).isoformat()
        monitor_mod.storage.record_launchd_first_seen(_CONSOLIDATE_LABEL, anchor)
        monkeypatch.setattr(monitor_mod, "_launchd_boot_time", lambda: boot)
        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_never_run_probe(plist_path))
        return monitor_mod._service_probe("ua-consolidate-proposal.timer", 1)

    def test_pre_boot_anchor_clamps_to_boot_when_not_yet_due(self, tmp_path, monkeypatch):
        """Anchor from weeks ago + a boot one hour ago + a schedule whose next
        fire is still ahead must read active, not inactive. Before the clamp
        this reported MISCONFIGURED for the whole window between an early-morning
        reboot and a daily timer's first post-boot slot."""
        now = datetime.now()
        # Daily at one hour from now: always overdue relative to a 30-day-old
        # anchor (so this read "inactive" before the clamp) but never yet due
        # relative to a boot one hour ago.
        schedule = {"Hour": (now.hour + 1) % 24, "Minute": 0}
        props = self._probe_with(
            tmp_path,
            monkeypatch,
            anchor_age_days=30,
            boot=now - timedelta(hours=1),
            schedule=schedule,
        )
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_pre_boot_anchor_still_flags_overdue_once_boot_window_passes(
        self, tmp_path, monkeypatch
    ):
        """The clamp must not excuse a genuinely-broken job: with uptime past
        the enable grace window and a daily schedule that has fired its slot
        many times since boot, zero runs is still inactive."""
        props = self._probe_with(
            tmp_path,
            monkeypatch,
            anchor_age_days=400,
            boot=datetime.now() - timedelta(days=10),
            schedule={"Hour": 0, "Minute": 0},
        )
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_boot_newer_than_anchor_but_still_overdue_is_unknown_then_inactive(
        self, tmp_path, monkeypatch
    ):
        """A boot inside the grace window keeps the ambiguous UNKNOWN posture:
        overdue relative to the boot alone, not yet relative to boot+grace."""
        props = self._probe_with(
            tmp_path,
            monkeypatch,
            anchor_age_days=400,
            boot=datetime.now() - timedelta(days=1),
            schedule={"Hour": 0, "Minute": 0},
        )
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "unknown",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }

    def test_unprobeable_boot_time_keeps_anchor_semantics(self, tmp_path, monkeypatch):
        """If the boot time cannot be determined, the pre-existing anchor rules
        unchanged — a 400-day anchor on a daily schedule stays inactive."""
        props = self._probe_with(
            tmp_path,
            monkeypatch,
            anchor_age_days=400,
            boot=None,
            schedule={"Hour": 0, "Minute": 0},
        )
        assert props == {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "NRestarts": "0",
            "LastExitCode": "(never exited)",
        }


class TestLaunchdBootTime:
    """`_launchd_boot_time` parses `sysctl -n kern.boottime` ({ sec = N, ... })
    into a naive local datetime matching _next_calendar_occurrence's
    wall-clock convention, and degrades to None on any probe failure."""

    def _fake_sysctl(self, stdout, returncode=0):
        def fake_run(cmd, capture_output, text, timeout):
            assert cmd == ["sysctl", "-n", "kern.boottime"]
            proc = type("Proc", (), {})()
            proc.returncode = returncode
            proc.stdout = stdout
            proc.stderr = ""
            return proc

        return fake_run

    def test_parses_sec_field(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(
            monitor_mod.subprocess, "run", self._fake_sysctl("{ sec = 1758234420, usec = 0 }\n")
        )
        boot = monitor_mod._launchd_boot_time()
        assert boot == datetime.fromtimestamp(1758234420)
        assert boot.tzinfo is None

    def test_nonzero_returncode_is_none(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(monitor_mod.subprocess, "run", self._fake_sysctl("", returncode=1))
        assert monitor_mod._launchd_boot_time() is None

    def test_malformed_output_is_none(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setattr(
            monitor_mod.subprocess, "run", self._fake_sysctl("kern.boottime: unknown\n")
        )
        assert monitor_mod._launchd_boot_time() is None

    def test_oserror_is_none(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        def fake_run(cmd, capture_output, text, timeout):
            raise OSError("sysctl missing")

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        assert monitor_mod._launchd_boot_time() is None


class TestCalendarOccurrenceIsLocalTime:
    """StartCalendarInterval Hour/Minute are the host's LOCAL wall clock, not
    UTC. Comparing them against a UTC-normalized reference point silently
    misjudges "overdue" by the host's UTC offset — e.g. a UTC-7 Mac with a
    09:00 timer would see 09:00 UTC (02:00 local) as the trigger instead."""

    def test_next_occurrence_matches_naive_local_hour(self):
        import unified_agent.monitor as monitor_mod

        after = datetime(2026, 9, 4, 8, 0)  # naive wall-clock, 08:00
        occurrence = monitor_mod._next_calendar_occurrence({"Hour": 9, "Minute": 0}, after)
        assert occurrence == datetime(2026, 9, 4, 9, 0)
        assert occurrence.tzinfo is None

    def test_zero_runs_status_uses_naive_local_now_throughout(self, tmp_path, monkeypatch):
        """A regression guard for the UTC/local mismatch: since dispatching
        "now" through UTC would previously shift every Hour/Minute match by
        the host's offset, this pins it as naive (no tzinfo) — both on a
        first-ever observation (which records it as the anchor) and on a
        later one (which compares it against a stored anchor)."""
        import unified_agent.monitor as monitor_mod

        captured = []
        real_now = monitor_mod.datetime.now

        class _Recording(monitor_mod.datetime):
            @classmethod
            def now(cls, *args, **kwargs):
                result = real_now(*args, **kwargs)
                captured.append(result)
                return result

        label = "com.unified-agent.consolidate-proposal"
        path = tmp_path / f"{label}.plist"
        plistlib.dump({"StartCalendarInterval": {"Hour": 0, "Minute": 0}}, path.open("wb"))

        monkeypatch.setattr(monitor_mod, "datetime", _Recording)
        monitor_mod._launchd_zero_runs_status(label, str(path))  # first-ever: records the anchor
        monitor_mod._launchd_zero_runs_status(label, str(path))  # second: compares against it

        assert captured
        assert all(dt.tzinfo is None for dt in captured)


def _insert_dispatch_run(task_id: int, state: str, created_days_ago: float = 1.0) -> str:
    import uuid

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    ts = _iso_days_ago(created_days_ago)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, 'Desktop_Agent1', ?, 'Desktop_Agent1', ?, ?)",
            (run_id, task_id, state, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def _insert_raw_task(
    *,
    status: str = "done",
    claimed_by: str | None = None,
    claimed_days_ago: float | None = None,
    created_days_ago: float = 2.0,
    completed_days_ago: float | None = 0.0,
) -> int:
    """Insert a task row directly, bypassing task_update's attribution gate —
    used to simulate legacy/unattributed rows the delivery_slo metric exists
    to catch (see delivery_slo.py's module docstring)."""
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks "
            "(title, status, priority, created_at, updated_at, completed_at, "
            "claimed_by, claimed_at) VALUES (?, ?, 'med', ?, ?, ?, ?, ?)",
            (
                "raw task",
                status,
                _iso_days_ago(created_days_ago),
                _iso_days_ago(created_days_ago),
                _iso_days_ago(completed_days_ago) if completed_days_ago is not None else None,
                claimed_by,
                _iso_days_ago(claimed_days_ago) if claimed_days_ago is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class TestDeliverySlo:
    def test_empty_db_is_uniformly_unavailable(self):
        result = delivery_slo()
        assert result["window_days"] == 7.0
        for key in (
            "dispatch_success_rate",
            "p90_cycle_time_hours",
            "unattributed_completions",
            "ci_green_rate",
        ):
            assert result[key]["status"] == "unavailable"
            assert result[key]["value"] is None

    def test_dispatch_success_rate_over_real_rows(self):
        tid = _insert_raw_task()
        for _ in range(9):
            _insert_dispatch_run(tid, "finished", created_days_ago=1.0)
        _insert_dispatch_run(tid, "failed", created_days_ago=1.0)
        # In-flight and out-of-window runs must not affect the rate.
        _insert_dispatch_run(tid, "running", created_days_ago=0.1)
        _insert_dispatch_run(tid, "finished", created_days_ago=30.0)

        result = delivery_slo(window_days=7.0)
        rate = result["dispatch_success_rate"]
        assert rate["numerator"] == 9
        assert rate["denominator"] == 10
        assert rate["value"] == 0.9
        assert rate["status"] == "ok"

    def test_p90_and_unattributed_over_real_tasks(self):
        for _ in range(9):
            _insert_raw_task(
                claimed_by="Desktop_Agent1", claimed_days_ago=1.0, completed_days_ago=0.0
            )
        _insert_raw_task(claimed_by=None, claimed_days_ago=None, completed_days_ago=0.0)
        # Outside the 7-day window: must not count toward either metric.
        _insert_raw_task(
            claimed_by=None,
            claimed_days_ago=None,
            created_days_ago=31.0,
            completed_days_ago=30.0,
        )

        result = delivery_slo(window_days=7.0)
        unattributed = result["unattributed_completions"]
        assert unattributed["denominator"] == 10
        assert unattributed["numerator"] == 1
        assert unattributed["status"] == "breach"

        p90 = result["p90_cycle_time_hours"]
        assert p90["status"] == "ok"
        assert p90["denominator"] == 10

    def test_non_positive_window_days_falls_back_to_default(self):
        assert delivery_slo(window_days=0)["window_days"] == 7.0
        assert delivery_slo(window_days=-3)["window_days"] == 7.0

    def test_ci_green_rate_unconfigured_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_CI_GREEN_RATE_PROVIDER", raising=False)
        result = delivery_slo()
        ci = result["ci_green_rate"]
        assert ci["status"] == "unavailable"
        assert "UA_CI_GREEN_RATE_PROVIDER" in ci["detail"]


class TestCiGreenRateGhProvider:
    """The gh-backed CI provider: opt-in, cached, never raises."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        import unified_agent.monitor as monitor_mod

        monitor_mod._CI_GREEN_RATE_CACHE = {}
        yield
        monitor_mod._CI_GREEN_RATE_CACHE = {}

    def test_missing_repo_env_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.delenv("UA_CI_GREEN_RATE_REPO", raising=False)
        result = delivery_slo()
        ci = result["ci_green_rate"]
        assert ci["status"] == "unavailable"
        assert "UA_CI_GREEN_RATE_REPO" in ci["detail"]

    def test_gh_runs_are_parsed_and_cached(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.setenv("UA_CI_GREEN_RATE_REPO", "anonb3ll/unified-agent")

        calls = []

        completed_at = datetime.now(UTC).isoformat()

        def check(name, conclusion):
            return {
                "__typename": "CheckRun",
                "name": name,
                "status": "COMPLETED",
                "completedAt": completed_at,
                "conclusion": conclusion,
            }

        class _FakeProc:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                [
                    {
                        "number": 1,
                        "isDraft": False,
                        "statusCheckRollup": [
                            check("CI / test", "SUCCESS"),
                            check("CI / web", "FAILURE"),
                        ],
                    },
                    {
                        "number": 2,
                        "isDraft": False,
                        "statusCheckRollup": [
                            check("CI / test", "SUCCESS"),
                            check("CI / web", "SKIPPED"),
                        ],
                    },
                    {
                        "number": 3,
                        "isDraft": False,
                        "statusCheckRollup": [
                            {**check("CI / test", "SUCCESS"), "completedAt": "2026-01-01T00:00:00Z"}
                        ],
                    },
                    {
                        "number": 4,
                        "isDraft": False,
                        "statusCheckRollup": [
                            {
                                **check("CI / test", "FAILURE"),
                                "completedAt": "2026-01-01T00:00:00Z",
                            },
                            check("CI / web", "SUCCESS"),
                        ],
                    },
                ]
            )

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeProc()

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)

        result = delivery_slo(window_days=7.0)
        ci = result["ci_green_rate"]
        assert ci["status"] == "ok"
        assert ci["numerator"] == 1
        assert ci["denominator"] == 3
        assert len(calls) == 1
        argv = calls[0][0][0]
        assert argv[argv.index("--search") + 1].startswith("updated:>=")
        assert "isDraft" in argv[argv.index("--json") + 1].split(",")

        # Second call within the TTL must reuse the cache, not call gh again.
        delivery_slo(window_days=7.0)
        assert len(calls) == 1

    def test_drafts_are_excluded_from_ready_head_green_rate(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        completed_at = datetime.now(UTC).isoformat()
        prs = [
            {
                "number": number,
                "isDraft": draft,
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "CI / full-verification",
                        "status": "COMPLETED",
                        "completedAt": completed_at,
                        "conclusion": conclusion,
                    }
                ],
            }
            for number, (draft, conclusion) in enumerate(
                [(True, "FAILURE"), (True, "SUCCESS"), (False, "SUCCESS"), (False, "FAILURE")],
                start=1,
            )
        ]
        proc = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(prs))
        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: proc)

        runs, reason = monitor_mod._ci_green_rate_runs_uncached(7.0, "anonb3ll/unified-agent")
        assert reason is None
        assert runs == [{"conclusion": "success"}, {"conclusion": "failure"}]

    def test_coderabbit_check_run_is_not_a_ready_head_ci_sample(self, monkeypatch):
        """A CodeRabbit CheckRun is not CI, so it cannot pass or fail a ready head."""
        import unified_agent.monitor as monitor_mod

        completed_at = datetime.now(UTC).isoformat()
        prs = [
            {
                "number": 1,
                "isDraft": False,
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "CI / full-verification",
                        "status": "COMPLETED",
                        "completedAt": completed_at,
                        "conclusion": "SUCCESS",
                    },
                    {
                        "__typename": "CheckRun",
                        "name": "CodeRabbit",
                        "status": "COMPLETED",
                        "completedAt": completed_at,
                        "conclusion": "FAILURE",
                    },
                ],
            },
            {
                "number": 2,
                "isDraft": False,
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "CodeRabbit",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                    }
                ],
            },
        ]
        proc = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(prs))
        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: proc)

        runs, reason = monitor_mod._ci_green_rate_runs_uncached(7.0, "anonb3ll/unified-agent")
        assert reason is None
        assert runs == [{"conclusion": "success"}]

    def test_github_actions_check_names_count_as_ci(self, monkeypatch):
        """#1787: GitHub Actions reports name="full-verification", workflowName="CI"."""
        import unified_agent.monitor as monitor_mod

        completed_at = datetime.now(UTC).isoformat()

        def check(name, workflow, conclusion):
            return {
                "__typename": "CheckRun",
                "name": name,
                "workflowName": workflow,
                "status": "COMPLETED",
                "completedAt": completed_at,
                "conclusion": conclusion,
            }

        prs = [
            {
                "number": 1,
                "isDraft": False,
                "statusCheckRollup": [check("full-verification", "CI", "FAILURE")],
            },
            # Another workflow's job is not a CI sample.
            {
                "number": 2,
                "isDraft": False,
                "statusCheckRollup": [check("build", "Docker", "FAILURE")],
            },
            {
                "number": 3,
                "isDraft": False,
                "statusCheckRollup": [check("CI / full-verification", "", "SUCCESS")],
            },
        ]
        proc = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(prs))
        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: proc)

        runs, reason = monitor_mod._ci_green_rate_runs_uncached(7.0, "anonb3ll/unified-agent")
        assert reason is None
        assert runs == [{"conclusion": "failure"}, {"conclusion": "success"}]

    def test_later_skipped_duplicate_does_not_mask_a_failure(self, monkeypatch):
        """#1787: skipped re-runs from non-CI label events are ignored, not latest-wins."""
        import unified_agent.monitor as monitor_mod

        now = datetime.now(UTC)

        def check(conclusion, minutes):
            return {
                "__typename": "CheckRun",
                "name": "gates",
                "workflowName": "CI",
                "status": "COMPLETED",
                "completedAt": (now + timedelta(minutes=minutes)).isoformat(),
                "conclusion": conclusion,
            }

        prs = [
            {
                "number": 1,
                "isDraft": False,
                "statusCheckRollup": [check("FAILURE", 0), check("SKIPPED", 5)],
            }
        ]
        proc = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(prs))
        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: proc)
        runs, _ = monitor_mod._ci_green_rate_runs_uncached(7.0, "anonb3ll/unified-agent")
        assert runs == [{"conclusion": "failure"}]

    @pytest.mark.parametrize("metadata", [{}, {"isDraft": None}, {"isDraft": "false"}])
    def test_unknown_draft_state_makes_green_rate_unavailable(self, monkeypatch, metadata):
        import unified_agent.monitor as monitor_mod

        proc = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([metadata]))
        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: proc)

        runs, reason = monitor_mod._ci_green_rate_runs_uncached(7.0, "anonb3ll/unified-agent")
        assert runs is None
        assert "draft metadata" in reason

    def test_cold_gh_call_does_not_hold_cache_lock(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.setenv("UA_CI_GREEN_RATE_REPO", "anonb3ll/unified-agent")
        started = Event()
        release = Event()
        calls = 0

        class _FakeProc:
            returncode = 0
            stderr = ""
            stdout = "[]"

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                assert release.wait(3)
            return _FakeProc()

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(monitor_mod._ci_green_rate_runs, 7.0)
            assert started.wait(2)
            second = pool.submit(monitor_mod._ci_green_rate_runs, 30.0)
            try:
                assert second.result(timeout=1) == ([], None)
            finally:
                release.set()
            assert first.result(timeout=2) == ([], None)
        assert calls == 2
        # The earlier 7-day completion must not evict the 30-day result.
        assert monitor_mod._ci_green_rate_runs(30.0) == ([], None)
        assert monitor_mod._ci_green_rate_runs(7.0) == ([], None)
        assert calls == 2

    def test_gh_failure_degrades_to_unavailable_never_raises(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.setenv("UA_CI_GREEN_RATE_REPO", "anonb3ll/unified-agent")

        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            raise OSError("gh: command not found")

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)

        result = delivery_slo()
        ci = result["ci_green_rate"]
        assert ci["status"] == "unavailable"
        assert "gh CI provider errored" in ci["detail"]
        assert delivery_slo()["ci_green_rate"]["status"] == "unavailable"
        assert len(calls) == 1

    def test_gh_coverage_limit_is_unavailable(self, monkeypatch):
        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.setenv("UA_CI_GREEN_RATE_REPO", "anonb3ll/unified-agent")

        class _FakeProc:
            returncode = 0
            stderr = ""
            stdout = json.dumps([{"number": n, "statusCheckRollup": []} for n in range(1000)])

        monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **kw: _FakeProc())
        ci = delivery_slo()["ci_green_rate"]
        assert ci["status"] == "unavailable"
        assert "coverage limit" in ci["detail"]

    def test_gh_timeout_degrades_to_unavailable(self, monkeypatch):
        import subprocess

        import unified_agent.monitor as monitor_mod

        monkeypatch.setenv("UA_CI_GREEN_RATE_PROVIDER", "gh")
        monkeypatch.setenv("UA_CI_GREEN_RATE_REPO", "anonb3ll/unified-agent")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=20)

        monkeypatch.setattr(monitor_mod.subprocess, "run", fake_run)

        result = delivery_slo()
        assert result["ci_green_rate"]["status"] == "unavailable"
