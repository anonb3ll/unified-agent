"""Delivery SLO Control Center route (task #1259, Epic C #1188).

`GET /api/delivery_slo` is the HTTP surface for `monitor.delivery_slo()` —
read-only, never mutates dispatch_runs/tasks. Ground truth is the real
computation against the fixture DB (dispatch_runs + tasks rows inserted
directly, same as `test_monitor.py`'s `TestDeliverySlo`), not a stub.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-delivery-slo-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_CI_GREEN_RATE_PROVIDER", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _iso(days_ago: float) -> str:
    """Timestamps must be relative to now: the 7-day SLO window is anchored
    to the current time, so hardcoded dates silently age out of the window."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _insert_task(status: str, claimed_by: str | None, completed: bool) -> int:
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks "
            "(title, status, priority, created_at, updated_at, completed_at, "
            "claimed_by, claimed_at) VALUES (?, ?, 'med', ?, "
            "?, ?, ?, ?)",
            (
                "raw task",
                status,
                _iso(2.0),
                _iso(2.0),
                _iso(1.0) if completed else None,
                claimed_by,
                _iso(2.0) if claimed_by else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_run(task_id: int, state: str) -> None:
    import uuid

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, 'Desktop_Agent1', ?, 'Desktop_Agent1', "
            "?, ?)",
            (
                f"run-{uuid.uuid4().hex[:12]}",
                task_id,
                state,
                _iso(1.0),
                _iso(1.0),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestDeliverySloRoute:
    def test_empty_db_shape_is_uniformly_unavailable(self, client):
        res = client.get("/api/delivery_slo", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["window_days"] == 7.0
        for key in (
            "dispatch_success_rate",
            "p90_cycle_time_hours",
            "unattributed_completions",
            "ci_green_rate",
        ):
            assert body[key]["status"] == "unavailable"
            assert body[key]["value"] is None

    def test_reports_real_dispatch_and_task_rows(self, client):
        tid = _insert_task("done", "Desktop_Agent1", completed=True)
        for _ in range(9):
            _insert_run(tid, "finished")
        _insert_run(tid, "failed")

        res = client.get("/api/delivery_slo", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        rate = body["dispatch_success_rate"]
        assert rate["numerator"] == 9
        assert rate["denominator"] == 10
        assert rate["value"] == 0.9
        assert rate["status"] == "ok"

        unattributed = body["unattributed_completions"]
        assert unattributed["denominator"] == 1
        assert unattributed["numerator"] == 0
        assert unattributed["status"] == "ok"

    def test_unattributed_completion_breaches(self, client):
        _insert_task("done", None, completed=True)
        res = client.get("/api/delivery_slo", headers=_auth())
        body = res.json()
        assert body["unattributed_completions"]["status"] == "breach"
        assert body["unattributed_completions"]["numerator"] == 1

    def test_window_days_query_param(self, client):
        # Task completed "now" (real time) is inside a 1-day window and
        # outside a window of -1 (falls back to the 7-day default, still
        # covers "now").
        tid = _insert_task("done", "Desktop_Agent1", completed=False)
        conn = storage.connect()
        try:
            from datetime import UTC, datetime

            now_iso = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE tasks SET completed_at=?, claimed_at=? WHERE id=?",
                (now_iso, now_iso, tid),
            )
            conn.commit()
        finally:
            conn.close()

        res = client.get("/api/delivery_slo?window_days=1", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["window_days"] == 1.0
        assert body["unattributed_completions"]["denominator"] == 1

    @pytest.mark.parametrize("window", ["nan", "inf", "-inf", "invalid"])
    def test_invalid_window_uses_default_without_http_500(self, client, window):
        res = client.get(f"/api/delivery_slo?window_days={window}", headers=_auth())
        assert res.status_code == 200, res.text
        assert res.json()["window_days"] == 7.0

    def test_requires_auth(self, client):
        assert client.get("/api/delivery_slo").status_code == 401

    def test_named_error_never_500s(self, client, monkeypatch):
        def boom(window_days):
            raise OSError("delivery_slo storage unreachable")

        monkeypatch.setattr(webui_api.monitor, "delivery_slo", boom)
        res = client.get("/api/delivery_slo", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["code"] == "delivery_slo_unavailable"
        assert "delivery_slo failed" in body["error"]


class TestDeliverySloReadOnlyInvariant:
    def test_route_is_get_only(self):
        matches = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if route.path == "/api/delivery_slo"
        ]
        assert matches, "expected /api/delivery_slo to be registered"
        for _path, methods in matches:
            assert methods == frozenset({"GET"})

    def test_api_helper_never_mutates_dispatch_or_tasks(self):
        import inspect

        blob = inspect.getsource(webui_api.delivery_slo_payload)
        for forbidden in (
            "claim_and_dispatch",
            "report_outcome",
            "dispatch_cancel",
            "dispatch_resume",
            "task_update",
            "task_add",
        ):
            assert forbidden not in blob, f"delivery_slo_payload must not call {forbidden}"
