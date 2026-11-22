from __future__ import annotations

import pytest

from unified_agent import config, events, storage, zulip_automation, zulip_worker


def _init_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()


def test_send_once_is_idempotent_and_rate_limited(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        zulip_automation.zulip,
        "zulip_send_message",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"sent": True, "message_id": 1},
    )

    first = zulip_automation._send_once(
        kind="event",
        dedupe_key="event:1",
        channel="ops",
        topic="automation",
        content="failure",
    )
    second = zulip_automation._send_once(
        kind="event",
        dedupe_key="event:1",
        channel="ops",
        topic="automation",
        content="failure",
    )

    assert first["sent"] is True
    assert second["duplicate"] is True
    assert len(calls) == 1


def test_bridge_initializes_without_replaying_old_events(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    events.record_event("agent.task.updated", payload={"summary": "blocked"}, notify=False)

    result = zulip_automation.bridge_once()

    assert result["status"] == "initialized"
    assert result["sent"] == 0
    assert storage.get_zulip_bridge_cursor(zulip_automation.BRIDGE_STATE_NAME)


@pytest.mark.parametrize("key", ["waiting_on_operator", config.LEGACY_OPERATOR_DIGEST_KEY])
def test_render_digest_contains_daily_sections(key):
    content = zulip_automation.render_digest(
        "morning_digest",
        {
            "generated_at": "2026-08-21T12:00:00+00:00",
            "status": "action_needed",
            "evidence": "1 exception",
            key: [{"kind": "review_wait", "title": "Review PR"}],
            "stuck": [],
            "running": {"tasks": []},
            "changed_today": {
                "tasks_completed": [{"id": 12, "title": "Shipped fix", "source": {"task_id": 12}}],
            },
        },
        {"queue_summary": {"healthy": 5, "error": 0, "reset": 0}},
    )

    assert "Morning work digest" in content
    assert "Waiting on the operator (1)" in content
    assert "Review PR" in content
    assert "Completed today (1)" in content
    assert "Shipped fix" in content
    assert "Zulip worker: healthy=5" in content


def test_bounded_retry_delay_caps_large_attempts():
    assert zulip_worker.bounded_retry_delay(1, base_s=5, max_s=300) == 5
    assert zulip_worker.bounded_retry_delay(1025, base_s=5, max_s=300) == 300


def test_plaintext_for_classification_strips_html_wrappers():
    decision = zulip_worker.classify_target(
        zulip_worker.plaintext_for_classification(
            "<p>[target: Desktop_Agent2] please inspect task-12</p>"
        )
    )
    assert decision["target_reason"] == "explicit-target"
    assert decision["actionable_for"] == ["Desktop_Agent2"]


def test_report_agent_default_has_zulip_credentials():
    """The retired Zulip reporter's default must map to a bot credential (task #1879)."""
    from unified_agent.tools import zulip

    assert zulip_automation.REPORT_AGENT_DEFAULT in zulip._BOT_CONFIG_FILES
