"""Quota / usage pane Control Center routes (task #1136).

Ground truth is the real ``model_quota_status`` projection against the fixture
DB / Codex sessions tree — availability states are asserted through the HTTP
surface, not stubs. Strictly read-only: no route may mutate credential_pool or
dispatch state.
"""

from __future__ import annotations

import inspect
import json
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import credential_pool, server, storage, webui
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.dispatch.report import report_outcome
from unified_agent.tools import delegate, tasks
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-quota-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _write_codex_meter(sessions_root, *, used_percent: float, window_minutes: int = 10080):
    day = sessions_root / "2026" / "09" / "18"
    day.mkdir(parents=True)
    path = day / "rollout-meter.jsonl"
    event = {
        "timestamp": "2026-09-18T08:16:37.000Z",
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "prolite",
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": window_minutes,
                    "resets_in_seconds": None,
                },
            }
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


def _spec() -> str:
    return (
        "### Objective\nQuota pane\n"
        "### Scope\n- src/unified_agent/\n"
        "### Definition of Done\n- availability projected\n"
        "### TDD Constraints\n- Must be real: observations\n"
        "### Boundaries\n- Must not probe providers\n"
    )


def _record_failed_run(agent: str, failure_reason: str, monkeypatch) -> str:
    packed = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent=agent, callback="memory"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-25T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", out["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", agent)
    assert report_outcome("failed", failure_reason=failure_reason)["ok"] is True
    return out["run_id"]


def _by_lane(body: dict) -> dict:
    return {row["lane"]: row for row in body["quotas"]}


class TestQuotaInventoryRoute:
    def test_missing_registry_is_partial_inventory_with_fleet_evidence(self, client):
        storage.join_init_background_threads()
        conn = storage.connect()
        try:
            timestamp = storage.now()
            conn.execute(
                "INSERT INTO executor_attempts "
                "(agent, backend, started_at, finished_at, outcome, detail, failure_class) "
                "VALUES (?, ?, ?, ?, 'failure', ?, ?)",
                (
                    "Desktop_Agent2",
                    "codex",
                    timestamp,
                    timestamp,
                    "synthetic committed quota exhaustion",
                    "provider_quota_exhausted",
                ),
            )
            conn.execute("DROP TABLE credential_pool")
            conn.commit()
        finally:
            conn.close()

        response = client.get("/api/quota", headers=_auth())
        assert response.status_code == 200
        body = response.json()
        assert body["credential_pool_error"] == "credential metadata unavailable"
        assert body.get("error") is None
        assert _by_lane(body)["codex"]["availability"] == "unavailable"
        assert body["failure_class_counts"]["provider_quota_exhausted"] == 1

    def test_lists_lanes_from_real_model_quota_status(self, client):
        projected = credential_pool.model_quota_status()
        res = client.get("/api/quota", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        assert "quotas" in body
        assert body["checked_at"]
        assert {q["lane"] for q in body["quotas"]} >= {"codex", "claude", "agy", "cursor"}
        assert len(body["quotas"]) == len(projected["quotas"])
        for live, via_http in zip(projected["quotas"], body["quotas"], strict=True):
            assert via_http["lane"] == live["lane"]
            assert via_http["availability"] == live["availability"]
            assert via_http["source"] == live["source"]

    def test_unknown_with_no_source_distinct_from_available_and_exhausted(
        self, client, monkeypatch, tmp_path
    ):
        """Honesty is the feature: no observations stay unknown, never available."""
        sessions = tmp_path / "codex-sessions"
        _write_codex_meter(sessions, used_percent=5.0)
        monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))
        _record_failed_run(
            "Desktop_Agent1",
            "HTTP 429: The usage limit has been reached — quota exhausted",
            monkeypatch,
        )

        res = client.get("/api/quota", headers=_auth())
        assert res.status_code == 200, res.text
        lanes = _by_lane(res.json())

        codex = lanes["codex"]
        assert codex["availability"] == "available"
        assert codex["used_percent"] == 5.0
        assert codex["window_minutes"] == 10080
        assert codex["observed_at"] == "2026-09-18T08:16:37.000Z"
        assert codex["source"] == "codex_rollout_rate_limits"
        assert codex["source_path"]
        assert codex["availability"] != "unknown"
        assert codex["availability"] != "unavailable"

        claude = lanes["claude"]
        assert claude["availability"] == "unavailable"
        assert claude["failure_class"] == "provider_quota_exhausted"
        assert claude["source"] == "dispatch_runs.failure_class"
        assert claude["observed_at"]
        assert claude["availability"] != "unknown"
        assert claude["availability"] != "available"

        agy = lanes["agy"]
        assert agy["availability"] == "unknown"
        assert agy["source"] == "no source"
        assert agy["availability"] != "available"
        assert agy["availability"] != "unavailable"
        assert agy["availability"] != claude["availability"]
        assert agy["availability"] != codex["availability"]

    def test_exhausted_lane_renders_failure_class_evidence(self, client, monkeypatch):
        _record_failed_run(
            "Desktop_Agent1",
            "HTTP 429: The usage limit has been reached — quota exhausted",
            monkeypatch,
        )
        res = client.get("/api/quota", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        claude = _by_lane(body)["claude"]
        assert claude["availability"] == "unavailable"
        assert claude["failure_class"] == "provider_quota_exhausted"
        assert (
            "usage limit" in (claude.get("evidence") or "").lower()
            or "quota" in (claude.get("evidence") or "").lower()
        )
        counts = body.get("failure_class_counts") or {}
        assert counts.get("provider_quota_exhausted", 0) >= 1

    def test_metered_codex_includes_evidence_fields(self, client, monkeypatch, tmp_path):
        sessions = tmp_path / "codex-sessions"
        source = _write_codex_meter(sessions, used_percent=12.5, window_minutes=300)
        monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))

        res = client.get("/api/quota", headers=_auth())
        assert res.status_code == 200, res.text
        codex = _by_lane(res.json())["codex"]
        assert codex["availability"] == "available"
        assert codex["used_percent"] == 12.5
        assert codex["window_minutes"] == 300
        assert codex["observed_at"] == "2026-09-18T08:16:37.000Z"
        assert codex["source_path"] == str(source)
        assert codex["source"] == "codex_rollout_rate_limits"

    def test_requires_auth(self, client):
        assert client.get("/api/quota").status_code == 401


class TestQuotaUnavailable:
    def test_named_error_when_projection_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(ws))
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
        storage.init_db()

        def boom(**_kwargs):
            raise OSError("quota projection storage unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/quota", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "quota projection unavailable"
            assert body["code"] == "quota_projection_unavailable"
            assert body["quotas"] == []
            # Named error — not an empty-success pane.
            assert body.get("error")


class TestQuotaReadOnlyInvariant:
    def test_quota_routes_are_get_only(self):
        quota_paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if "/quota" in route.path or "/credential_pool" in route.path
        ]
        assert quota_paths, "expected quota read routes to be registered"
        for path, methods in quota_paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        assert not any("/api/action/quota" in route.path for route in web_routes())
        assert not any("/api/action/credential" in route.path for route in web_routes())
        assert not any("/api/action/credential_pool" in route.path for route in web_routes())

    def test_api_helpers_never_reach_credential_or_dispatch_mutations(self):
        blob = inspect.getsource(webui_api.quota_payload)
        for forbidden in (
            "credential_pool_register",
            "credential_pool_revoke",
            "claim_and_dispatch",
            "report_outcome",
            "dispatch_cancel",
            "dispatch_resume",
        ):
            assert forbidden not in blob, f"quota_payload must not call {forbidden}"
