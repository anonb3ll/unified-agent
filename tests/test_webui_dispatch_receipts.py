"""Review/merge delivery-receipt list Control Center route (task #1182).

Lets an operator list recent dispatch delivery outcomes without MCP
spelunking. Ground truth is the real ``dispatch_runs`` ledger — rows are
asserted through the HTTP surface, not stubs. Strictly read-only: no route
here may mutate a run (cancel/resume/confirm), and this is a projection of
the existing ledger, not a second one.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import delegate, dispatch_report, tasks
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-dispreceipts-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _spec() -> str:
    return "### Objective\ndelivery receipt list rehearsal\n### Scope\n- dispatch\n"


def _seed_finished_run() -> tuple[int, str]:
    result = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(result["task_id"], append_note="[plan: approved @ 2026-09-17T00:00:00Z]")
    launch = RecordingLaunchBoundary()
    claimed = coordinator.claim_and_dispatch(task_id=result["task_id"], launch=launch)
    run_id = claimed["run_id"]
    scheduler.record_run_outcome(run_id, "finished", evidence_ref="receipt-proof-1182")
    return result["task_id"], run_id


class TestDispatchReceiptsRoute:
    def test_lists_recent_runs_from_real_ledger(self, client):
        task_id, run_id = _seed_finished_run()

        res = client.get("/api/dispatch_receipts", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        run_ids = {row["run_id"] for row in body["receipts"]}
        assert run_id in run_ids
        row = next(r for r in body["receipts"] if r["run_id"] == run_id)
        assert row["task_id"] == task_id
        assert row["state"] == "finished"
        assert row["terminal"] is True
        assert row["has_evidence"] is True
        assert row["evidence_ref"] == "receipt-proof-1182"
        for field in (
            "run_id",
            "task_id",
            "delegation_id",
            "agent_label",
            "state",
            "actor",
            "lease_expires_at",
            "failure_reason",
            "failure_class",
            "evidence_ref",
            "termination_outcome",
            "created_at",
            "updated_at",
            "terminal",
            "has_evidence",
        ):
            assert field in row

    def test_task_id_filter(self, client):
        task_id, run_id = _seed_finished_run()
        other_task, _ = _seed_finished_run()

        res = client.get("/api/dispatch_receipts", params={"task_id": task_id}, headers=_auth())
        assert res.status_code == 200, res.text
        run_ids = {row["run_id"] for row in res.json()["receipts"]}
        assert run_id in run_ids
        assert all(row["task_id"] == task_id for row in res.json()["receipts"])

    def test_bad_task_id_is_400(self, client):
        res = client.get(
            "/api/dispatch_receipts", params={"task_id": "not-an-int"}, headers=_auth()
        )
        assert res.status_code == 400

    def test_requires_auth(self, client):
        assert client.get("/api/dispatch_receipts").status_code == 401

    def test_full_receipt_available_for_drill_down(self, client):
        _task_id, run_id = _seed_finished_run()
        full = dispatch_report.dispatch_delivery_receipt(run_id=run_id)
        assert full["run_id"] == run_id
        assert full["evidence_ref"] == "receipt-proof-1182"


class TestDispatchReceiptsUnavailable:
    def test_named_error_when_projection_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(ws))
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()

        def boom(**_kwargs):
            raise OSError("dispatch ledger unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/dispatch_receipts", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "dispatch delivery receipts unavailable"
            assert body["code"] == "dispatch_receipts_unavailable"
            assert body["receipts"] == []


class TestDispatchReceiptsReadOnlyInvariant:
    def test_dispatch_receipts_route_is_get_only(self):
        paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if "/dispatch_receipts" in route.path
        ]
        assert paths, "expected the dispatch_receipts read route to be registered"
        for path, methods in paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        assert not any("/api/action/dispatch_receipts" in route.path for route in web_routes())

    def test_api_helper_never_mutates_a_run(self):
        src = inspect.getsource(webui_api.dispatch_receipts_payload)
        body = src.split('"""', 2)[-1]
        for forbidden in (
            "dispatch_cancel(",
            "dispatch_resume(",
            "dispatch_retry_cancellation(",
            "dispatch_confirm_cancellation(",
            "record_run_outcome(",
        ):
            assert forbidden not in body, f"dispatch_receipts_payload must not call {forbidden}"

    def test_tool_helper_never_mutates_a_run(self):
        from unified_agent.tools import dispatch_report as dispatch_report_module

        src = inspect.getsource(dispatch_report_module.dispatch_delivery_receipts_list)
        body = src.split('"""', 2)[-1]
        for forbidden in (
            "coordinator.cancel_run(",
            "coordinator.resume_run(",
            "coordinator.retry_cancellation(",
            "coordinator.confirm_cancellation(",
            "record_run_outcome(",
        ):
            assert forbidden not in body, (
                f"dispatch_delivery_receipts_list must not call {forbidden}"
            )
