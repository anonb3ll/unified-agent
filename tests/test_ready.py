"""GET /readyz: the live store must be writable, and WAL must checkpoint.

/healthz stays a content-free liveness probe. Readiness uses the real
database (config.db_path / storage.connect), returns 503 when that store
cannot be written, and treats WorkOS reachability as advisory.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, ready, server, storage
from unified_agent.webui import web_routes


@pytest.fixture(autouse=True)
def fresh_workos_probe(monkeypatch):
    monkeypatch.setattr(ready, "_WORKOS_PROBE", None)


@pytest.fixture
def client():
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as c:
        yield c


def _probe_row() -> sqlite3.Row | None:
    conn = storage.connect()
    try:
        return conn.execute("SELECT value FROM meta WHERE key = ?", (ready._PROBE_KEY,)).fetchone()
    finally:
        conn.close()


def test_dns_stall_is_bounded_and_keeps_one_worker(monkeypatch):
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_test")
    monkeypatch.setattr(ready, "_WORKOS_TIMEOUT_S", 0.04)
    monkeypatch.setattr(ready, "probe_sqlite", lambda: ("ok", "ok"))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def stalled_dns(*_args, **_kwargs):
        calls.append(True)
        started.set()
        release.wait(3)
        raise ready.socket.gaierror("synthetic unavailable resolver")

    monkeypatch.setattr(ready.socket, "getaddrinfo", stalled_dns)
    try:
        began = time.monotonic()
        assert ready.readiness() == {
            "ok": True,
            "sqlite": "ok",
            "wal": "ok",
            "workos": "unreachable",
        }
        assert time.monotonic() - began < 0.5
        assert started.wait(0.5)
        for _ in range(6):
            assert ready.probe_workos() == "unreachable"
        assert calls == [True]
    finally:
        release.set()
        assert ready._WORKOS_PROBE.done.wait(1)


def test_completed_advisory_result_is_cached(monkeypatch):
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_test")
    calls = []

    def refuse(*_args, **_kwargs):
        calls.append(True)
        raise OSError("synthetic unreachable")

    monkeypatch.setattr(ready.socket, "create_connection", refuse)
    assert ready.probe_workos() == "unreachable"
    assert ready.probe_workos() == "unreachable"
    assert calls == [True]


def test_readyz_200_when_the_store_is_writable(client):
    storage.init_db()
    res = client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["sqlite"] == "ok"
    assert body["wal"] == "ok"
    assert body["workos"] == "skipped"
    assert _probe_row() is None


def test_readyz_503_when_the_database_is_readonly(client, monkeypatch):
    """A store this process cannot chmod back stays read-only and is not ready.

    ``storage.connect`` tightens an owned database to 0600 after the WAL
    pragma, so mode 0444 on our own file is repaired and would still be
    ready. Pretend the file is owned by someone else: the pragma can read
    it, the chmod is skipped, and the write is refused.
    """
    storage.init_db()
    path = config.db_path()
    path.chmod(0o444)
    monkeypatch.setattr(storage, "_process_owns", lambda _path: False)
    res = client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["ok"] is False
    assert body["sqlite"] == "readonly"
    assert body["wal"] == "unchecked"
    assert set(body) == {"ok", "sqlite", "wal", "workos"}


def test_readyz_503_when_the_database_is_missing(client):
    path = config.db_path()
    for suffix in ("", "-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    res = client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["ok"] is False
    assert body["sqlite"] == "unavailable"
    assert body["wal"] == "unchecked"
    assert not path.exists()


def test_workos_unreachable_does_not_fail_readiness(client, monkeypatch):
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01READY")

    def _refuse(*_args, **_kwargs):
        raise OSError("timed out")

    monkeypatch.setattr(ready.socket, "create_connection", _refuse)
    storage.init_db()
    res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["workos"] == "unreachable"


def test_workos_reachable_is_advisory(monkeypatch):
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_test")
    calls: list[tuple] = []

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def close(self):
            return None

    def _connect(address, timeout):
        calls.append((address, timeout))
        return _Sock()

    monkeypatch.setattr(ready.socket, "create_connection", _connect)
    storage.init_db()
    body = ready.readiness()
    assert body["ok"] is True
    assert body["workos"] == "reachable"
    assert calls == [(("api.workos.com", 443), 1.0)]


def test_workos_is_not_contacted_when_unconfigured(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("socket.create_connection should not run")

    monkeypatch.setattr(ready.socket, "create_connection", _boom)
    storage.init_db()
    assert ready.readiness()["workos"] == "skipped"
