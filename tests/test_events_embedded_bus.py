"""The event bus hosted inside the hub process.

#157 shipped `EventBusServer` and nothing ever ran it, so `publish()` has returned
False for every event this cluster has produced. The durable `event_log` masked it:
data kept accruing, so nothing looked broken. These tests cover both halves of the fix
— that embedding actually turns fan-out on, and that a bus which cannot bind never
takes the hub down with it.
"""

import socket

import pytest

from unified_agent import config, events, server, storage


@pytest.fixture(autouse=True)
def _socket_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_EVENT_SOCKET", str(tmp_path / "events.sock"))
    monkeypatch.delenv("UA_EVENT_BUS", raising=False)
    monkeypatch.delenv("UA_EVENT_BUS_EMBED", raising=False)
    storage.init_db()
    yield
    if server._event_bus_server is not None:
        try:
            server._event_bus_server.stop()
        except Exception:
            pass
        server._event_bus_server = None


class TestEmbeddedStart:
    def test_start_binds_the_socket_and_enables_publish(self):
        server._start_embedded_event_bus()
        assert server._event_bus_server is not None
        path = config.event_socket_path()
        assert path.exists() and path.is_socket()
        # The property that was false for this deployment's entire history.
        assert events.publish({"ulid": "01", "channel": "test", "payload": {}}) is True

    def test_stop_is_clean_and_releases_the_socket(self):
        server._start_embedded_event_bus()
        server._event_bus_server.stop()
        server._event_bus_server = None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            with pytest.raises(OSError):
                sock.connect(str(config.event_socket_path()))
        finally:
            sock.close()

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("UA_EVENT_BUS_EMBED", "0")
        server._start_embedded_event_bus()
        assert server._event_bus_server is None

    def test_disabled_when_the_bus_is_off_entirely(self, monkeypatch):
        monkeypatch.setenv("UA_EVENT_BUS", "0")
        server._start_embedded_event_bus()
        assert server._event_bus_server is None

    def test_bind_failure_never_propagates(self, tmp_path, monkeypatch, capsys):
        """A hub that refuses to start because a socket would not bind is a worse
        outcome than a hub with no live fan-out."""
        blocked = tmp_path / "not-a-socket"
        blocked.mkdir()
        monkeypatch.setenv("UA_EVENT_SOCKET", str(blocked / "nested" / "deep" / "x.sock"))
        monkeypatch.setattr(events.EventBusServer, "start", lambda self, timeout=10.0: 1 / 0)
        server._start_embedded_event_bus()  # must not raise
        assert server._event_bus_server is None
        assert "event bus unavailable" in capsys.readouterr().err


class TestPublishPosture:
    def test_publish_returns_false_with_no_bus_running(self):
        """The status quo this work changes — asserted so a regression is visible."""
        assert events.publish({"ulid": "01", "channel": "test", "payload": {}}) is False

    def test_record_event_still_persists_with_no_bus(self):
        """Fan-out is best-effort; the durable append is not."""
        ulid = events.record_event("agent.task.updated", payload={"n": 1})
        assert ulid
        assert events.query_events(limit=5)[-1]["ulid"] == ulid
