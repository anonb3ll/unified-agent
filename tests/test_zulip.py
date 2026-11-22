from __future__ import annotations

from pathlib import Path

import pytest

from unified_agent.tools import zulip

SITE = "https://zulip.example.test"


@pytest.fixture(autouse=True)
def _explicit_site(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_SITE", SITE)


def _write_zuliprc(path: Path, *, mode: int = 0o600, site: str = SITE) -> None:
    path.write_text(
        f"[api]\nemail=desktop-agent2-bot@zulip.example.test\nkey=not-a-real-secret\nsite={site}\n",
        encoding="utf-8",
    )
    path.chmod(mode)


class _Response:
    def __init__(self, body: dict[str, object] | None = None):
        self._body = body or {"result": "success", "id": 42}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._body


class _Client:
    def __init__(self, response: _Response | None = None):
        self.calls: list[dict[str, object]] = []
        self.response = response or _Response()

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, *, auth: tuple[str, str], data: dict[str, str]) -> _Response:
        self.calls.append({"url": url, "auth": auth, "data": data})
        return self.response

    def get(
        self,
        url: str,
        *,
        auth: tuple[str, str],
        params: dict[str, object],
    ) -> _Response:
        self.calls.append({"url": url, "auth": auth, "params": params})
        return self.response


def test_send_zulip_message_posts_bounded_channel_payload(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    client = _Client()
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_send_message(
        "Desktop_Agent2", "coord", "task-512", "Relay setup completed."
    )

    assert result == {
        "sent": True,
        "message_id": 42,
        "agent": "Desktop_Agent2",
        "channel": "coord",
        "topic": "task-512",
    }
    assert client.calls == [
        {
            "url": f"{SITE}/api/v1/messages",
            "auth": (
                "desktop-agent2-bot@zulip.example.test",
                "not-a-real-secret",
            ),
            "data": {
                "type": "stream",
                "to": "coord",
                "topic": "task-512",
                "content": "Relay setup completed.",
            },
        }
    ]


def test_send_zulip_message_maps_title_case_stream_names(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    client = _Client()
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_send_message("Desktop_Agent2", "requests", "task-512", "ping")

    assert result["sent"] is True
    assert client.calls[0]["data"]["to"] == "Requests"


def test_zulip_credential_status_reports_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    status = zulip.zulip_credential_status()
    assert status["ready"] is False
    assert status["missing"]


def test_send_zulip_message_rejects_unknown_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")

    with pytest.raises(zulip.ZulipRelayError, match="channel"):
        zulip.zulip_send_message("Desktop_Agent2", "private", "setup", "hello")


def test_send_zulip_message_rejects_when_retired(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "1")

    with pytest.raises(zulip.ZulipRelayError, match="retired"):
        zulip.zulip_send_message("Desktop_Agent2", "coord", "setup", "hello")


def test_send_zulip_message_allowed_when_explicitly_enabled(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    client = _Client()
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_send_message("Desktop_Agent2", "coord", "setup", "hello")

    assert result["sent"] is True


def test_send_zulip_message_refused_when_retired_unset(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
    client = _Client()
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    with pytest.raises(zulip.ZulipRelayError, match="retired"):
        zulip.zulip_send_message("Desktop_Agent2", "coord", "setup", "hello")
    assert client.calls == []
    assert zulip.zulip_retired() is True


@pytest.mark.parametrize("raw", ["", "maybe", "2"])
def test_send_zulip_message_refused_for_unrecognized_retirement_value(monkeypatch, tmp_path, raw):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", raw)
    client = _Client()
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    with pytest.raises(zulip.ZulipRelayError, match="retired"):
        zulip.zulip_send_message("Desktop_Agent2", "coord", "setup", "hello")
    assert client.calls == []


def test_load_zuliprc_rejects_group_or_other_permissions(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path, mode=0o640)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)

    with pytest.raises(zulip.ZulipRelayError, match="owner-only"):
        zulip.load_zuliprc("Desktop_Agent2")


def test_load_zuliprc_rejects_unapproved_site(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path, site="https://unexpected.example")
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)

    with pytest.raises(zulip.ZulipRelayError, match="site"):
        zulip.load_zuliprc("Desktop_Agent2")


def test_send_zulip_message_rejects_oversized_content(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")

    with pytest.raises(zulip.ZulipRelayError, match="maximum"):
        zulip.zulip_send_message(
            "Desktop_Agent2", "coord", "setup", "x" * (zulip.MAX_CONTENT_CHARS + 1)
        )


def test_fetch_zulip_messages_returns_bounded_inbound_records(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    client = _Client(
        _Response(
            {
                "result": "success",
                "messages": [
                    {
                        "id": 100,
                        "sender_email": "desktop-agent2-bot@zulip.example.test",
                        "sender_full_name": "Desktop_Agent2",
                        "type": "stream",
                        "display_recipient": "coord",
                        "subject": "relay-smoke",
                        "content": "self echo",
                        "timestamp": 1_700_000_000,
                    },
                    {
                        "id": 101,
                        "sender_email": "desktop-agent1-bot@zulip.example.test",
                        "sender_full_name": "Desktop_Agent1",
                        "type": "stream",
                        "display_recipient": "coord",
                        "subject": "relay-smoke",
                        "content": "inbound request",
                        "timestamp": 1_700_000_001,
                    },
                ],
            }
        )
    )
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_fetch_messages("Desktop_Agent2", "coord", topic="relay-smoke", limit=10)

    assert result == {
        "received": True,
        "agent": "Desktop_Agent2",
        "channel": "coord",
        "topic": "relay-smoke",
        "anchor": "newest",
        "next_anchor": 101,
        "messages": [
            {
                "id": 101,
                "sender_email": "desktop-agent1-bot@zulip.example.test",
                "sender_full_name": "Desktop_Agent1",
                "type": "stream",
                "channel": "coord",
                "topic": "relay-smoke",
                "content": "inbound request",
                "timestamp": 1_700_000_001,
            }
        ],
    }
    assert client.calls == [
        {
            "url": f"{SITE}/api/v1/messages",
            "auth": (
                "desktop-agent2-bot@zulip.example.test",
                "not-a-real-secret",
            ),
            "params": {
                "anchor": "newest",
                "num_before": 10,
                "num_after": 0,
                "narrow": (
                    '[{"operator": "channel", "operand": "coord"}, '
                    '{"operator": "topic", "operand": "relay-smoke"}]'
                ),
                "apply_markdown": "false",
            },
        }
    ]


def test_register_event_queue_uses_filtered_message_channels(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    client = _Client(_Response({"result": "success", "queue_id": "q-1", "last_event_id": 7}))
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_register_event_queue("Desktop_Agent2", ("coord", "review"))

    assert result["registered"] is True
    assert result["last_event_id"] == 7
    assert client.calls[0]["data"] == {
        "event_types": '["message"]',
        "fetch_event_types": '["message"]',
        "narrow": '[["channel", "coord"], ["channel", "Review"]]',
    }


def test_fetch_events_preserves_bad_queue_error_code(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    client = _Client(_Response({"result": "error", "code": "BAD_EVENT_QUEUE_ID"}))
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_fetch_events("Desktop_Agent2", "q-1", 7)

    assert result == {
        "received": False,
        "agent": "Desktop_Agent2",
        "queue_id": "q-1",
        "code": "BAD_EVENT_QUEUE_ID",
        "error": "Zulip event queue fetch was rejected",
    }


def test_fetch_zulip_messages_uses_cursor_for_newer_messages(monkeypatch, tmp_path):
    path = tmp_path / "zuliprc-desktop-agent2"
    _write_zuliprc(path)
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
    client = _Client(_Response({"result": "success", "messages": []}))
    monkeypatch.setattr(zulip.httpx, "Client", lambda timeout: client)

    result = zulip.zulip_fetch_messages("Desktop_Agent2", "coord", anchor="101", limit=5)

    assert result["next_anchor"] == 101
    assert client.calls[0]["params"] == {
        "anchor": 101,
        "num_before": 0,
        "num_after": 5,
        "narrow": '[{"operator": "channel", "operand": "coord"}]',
        "apply_markdown": "false",
    }


def test_fetch_zulip_messages_rejects_invalid_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)

    with pytest.raises(zulip.ZulipRelayError, match="limit"):
        zulip.zulip_fetch_messages("Desktop_Agent2", "coord", limit=0)

    with pytest.raises(zulip.ZulipRelayError, match="topic"):
        zulip.zulip_fetch_messages(
            "Desktop_Agent2", "coord", topic="x" * (zulip.MAX_TOPIC_CHARS + 1)
        )
