"""Zulip display projections must remain readable without a configured site."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from unified_agent.tools import zulip
from unified_agent.webui import api


def no_io(*args, **kwargs):
    pytest.fail("unconfigured Zulip display attempted IO")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "site",
    [
        None,
        "",
        " ",
        "http://zulip.example.test",
        "https://" + "fixture-user:fixture-password" + chr(64) + "zulip.example.test",
        "https://zulip.example.test?private=value",
    ],
)
async def test_unconfigured_zulip_display_has_no_io(monkeypatch, site):
    if site is None:
        monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    else:
        monkeypatch.setenv("UA_ZULIP_SITE", site)
    monkeypatch.setattr(api.zulip_worker, "ZulipPollingWorker", no_io)
    monkeypatch.setattr(api, "_recent_zulip_inbound_rows", no_io)
    monkeypatch.setattr(zulip, "_config_path", no_io)
    monkeypatch.setattr(zulip.httpx, "Client", no_io)

    assert api._zulip_site() == ""
    assert api._zulip_stream_url("coord", "task-1") == ""
    worker = await api.zulip_worker_health_payload()
    assert worker["status"] == "unconfigured"
    assert worker["channels"] == worker["queues"] == []
    payload = await api.zulip_coordination_payload()
    assert set(payload) == {"site", "worker", "recent_inbound", "stats", "error"}
    assert payload["site"] == ""
    assert payload["worker"] == worker
    assert payload["recent_inbound"] == [] and payload["stats"] == {}
    assert "UA_ZULIP_SITE" in payload["error"]
    assert "private" not in payload["error"]
    items, informational, errors = await api._zulip_attention_items(datetime.now(UTC))
    assert items == informational == []
    # Retired with no site set is no source to scan; a site that is set but
    # invalid stays an attention error (it is a misconfiguration to fix).
    assert errors == ([payload["error"]] if (site or "").strip() else [])
    assert zulip.zulip_retired()  # display never re-opens outbound sending


def test_unconfigured_inbound_normalization_does_not_invent_a_link(monkeypatch):
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    row = api._normalize_zulip_inbound_event({"payload": {"channel": "coord", "topic": "task-1"}})
    assert row["zulip_url"] == ""
    assert row["task_id"] == 1


@pytest.mark.anyio
async def test_configured_zulip_display_preserves_health_history_and_links(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test/")
    expected_health = {"status": "retired", "channels": [{"channel": "coord"}]}
    calls = []

    def worker():
        calls.append("worker")
        return SimpleNamespace(health_payload=lambda: expected_health)

    monkeypatch.setattr(api.zulip_worker, "ZulipPollingWorker", worker)
    monkeypatch.setattr(api, "_recent_zulip_inbound_rows", lambda: [])
    payload = await api.zulip_coordination_payload()
    assert payload == {
        "site": "https://zulip.example.test",
        "worker": expected_health,
        "recent_inbound": [],
        "stats": api._zulip_coordination_stats([], now=datetime.now(UTC)),
    }
    assert calls == ["worker"]
    assert (
        api._zulip_stream_url("coord", "task 1")
        == "https://zulip.example.test/#narrow/stream/coord/topic/task-1"
    )


@pytest.mark.anyio
async def test_configured_zulip_history_error_keeps_safe_site(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")

    async def health():
        return {"status": "retired", "channels": []}

    def history():
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(api, "zulip_worker_health_payload", health)
    monkeypatch.setattr(api, "_recent_zulip_inbound_rows", history)
    payload = await api.zulip_coordination_payload()
    assert payload["site"] == "https://zulip.example.test"
    assert payload["error"] == "history unavailable"
    assert payload["recent_inbound"] == []


@pytest.mark.anyio
async def test_configured_retired_worker_preserves_channel_shape(monkeypatch):
    from unified_agent import storage

    monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")
    monkeypatch.setenv("UA_ZULIP_RETIRED", "1")
    storage.init_db()
    payload = await api.zulip_worker_health_payload()
    assert payload["status"] == "retired"
    assert len(payload["channels"]) == 25
    assert len(payload["queues"]) == 5
    assert payload["last_successful_poll"] is None
