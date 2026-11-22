"""Approval rows record the caller's current run for display (task #1653).

``run_id`` is linkage for Inbox and Runs. It is not an authorization input:
``approval_resolve`` and the gate match a row without reading it.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, platform_readers, server, storage, webui
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
    get_approval,
)
from unified_agent.webui import api as webui_api

AGENT = "Desktop_Agent2"
RUN = "sp-CLAIMED00000000000000000001"
OTHER_RUN = "sp-OTHER0000000000000000000001"
DISPATCH_RUN = "run-7-abcd1234"


def _now() -> str:
    return storage.now()


def _insert_task(
    agent: str,
    run_id: str | None,
    *,
    org_id: str | None = None,
    status: str = "doing",
    claim: bool = True,
    claimed_by: str | None = None,
    lock_expires_at: str | None = None,
    archived_at: str | None = None,
    updated_at: str | None = None,
    current_run_agent: str | None = None,
) -> int:
    storage.init_db()
    holder = agent if claim else claimed_by
    run_agent = agent if run_id else None
    if current_run_agent is not None:
        run_agent = current_run_agent
    written_at = updated_at or _now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at, "
            "claimed_by, current_run_id, current_run_agent, org_id, "
            "lock_expires_at, archived_at) "
            "VALUES ('linkage', ?, 'med', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                status,
                _now(),
                written_at,
                holder,
                run_id,
                run_agent,
                org_id,
                lock_expires_at,
                archived_at,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_dispatch(task_id: int, agent: str, run_id: str, *, state: str = "running") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'dispatch-coordinator', ?, ?)",
            (run_id, task_id, agent, state, _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _request(command: str, *, requested_by: str = "claimed-peer") -> int:
    with pytest.raises(ApprovalPendingError) as caught:
        check_approval_gate("run_cmd", {"command": command}, requested_by=requested_by)
    assert caught.value.approval_id is not None
    return int(caught.value.approval_id)


class TestRunLinkage:
    def test_claimed_run_is_stored_and_a_request_outside_a_run_is_null(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        token = config.agent_identity_var.set(AGENT)
        try:
            inside = _request("echo inside")
            assert get_approval(inside)["run_id"] is None

            _insert_task(AGENT, RUN)
            linked = _request("echo linked")
        finally:
            config.agent_identity_var.reset(token)

        row = get_approval(linked)
        assert row["run_id"] == RUN
        assert row["requested_by"] == "claimed-peer"
        listed = approval_pending()["pending"]
        assert {item["id"]: item["run_id"] for item in listed}[linked] == RUN

    def test_requested_by_does_not_select_another_agents_run(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _insert_task(AGENT, RUN)
        # No verified identity. The supplied requester names the agent who holds
        # the run; that label is not authority for the linkage.
        approval_id = _request("echo forged", requested_by=AGENT)
        assert get_approval(approval_id)["run_id"] is None

    def test_two_claimed_runs_store_null(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _insert_task(AGENT, RUN)
        _insert_task(AGENT, OTHER_RUN)
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo ambiguous")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] is None

    def test_other_tenants_run_is_not_used(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _insert_task(AGENT, RUN, org_id="other-org")
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo foreign")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] is None

    def test_unbounded_cached_run_id_is_not_stored(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _insert_task(AGENT, "not a run")
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo messy")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] is None

    def test_unique_live_dispatch_run_is_used_when_no_claim_run(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        task_id = _insert_task(AGENT, None, status="open")
        _insert_dispatch(task_id, AGENT, DISPATCH_RUN)
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo dispatch")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] == DISPATCH_RUN

    def test_claimed_run_wins_over_a_live_dispatch_slot(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        task_id = _insert_task(AGENT, RUN, lock_expires_at="2999-01-01T00:00:00+00:00")
        _insert_dispatch(task_id, AGENT, DISPATCH_RUN)
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo both")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] == RUN

    def test_unclaimed_doing_run_links_until_archived_or_stale(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        task_id = _insert_task(
            AGENT,
            RUN,
            claim=False,
            current_run_agent=AGENT,
        )
        token = config.agent_identity_var.set(AGENT)
        try:
            live = _request("echo unclaimed-live")
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE tasks SET archived_at=? WHERE id=?",
                    (_now(), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            archived = _request("echo unclaimed-archived")
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE tasks SET archived_at=NULL, updated_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", task_id),
                )
                conn.commit()
            finally:
                conn.close()
            stale = _request("echo unclaimed-stale")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(live)["run_id"] == RUN
        assert get_approval(archived)["run_id"] is None
        assert get_approval(stale)["run_id"] is None

    def test_expired_claim_yields_to_the_live_dispatch_run(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        task_id = _insert_task(AGENT, RUN, lock_expires_at="2000-01-01T00:00:00+00:00")
        _insert_dispatch(task_id, AGENT, DISPATCH_RUN)
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo expired-claim")
        finally:
            config.agent_identity_var.reset(token)
        assert get_approval(approval_id)["run_id"] == DISPATCH_RUN

    def test_resolve_ignores_run_id(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _insert_task(AGENT, RUN)
        token = config.agent_identity_var.set(AGENT)
        try:
            approval_id = _request("echo resolve")
        finally:
            config.agent_identity_var.reset(token)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE approvals SET run_id=? WHERE id=?",
                ("sp-REWRITTEN000000000000000001", approval_id),
            )
            conn.execute(
                "UPDATE tasks SET current_run_id=? WHERE claimed_by=?",
                (OTHER_RUN, AGENT),
            )
            conn.commit()
        finally:
            conn.close()
        # The resolver is not the requester, and the rewritten run id is not
        # an argument resolve accepts or consults.
        assert "run_id" not in inspect.signature(approval_resolve).parameters
        resolved = approval_resolve(approval_id, "approve", resolved_by="Operator")
        assert resolved["status"] == "approved"
        assert resolved["run_id"] == "sp-REWRITTEN000000000000000001"
        # The pass names the requester that queued it (#2492), and this request
        # recorded the supplied label, so it is spent as that requester rather
        # than under the verified identity that differs from it.
        consumed = check_approval_gate(
            "run_cmd", {"command": "echo resolve"}, requested_by="claimed-peer"
        )
        assert consumed == {"gated": True, "approval_id": approval_id}
        assert get_approval(approval_id)["status"] == "consumed"


class TestRequestEvent:
    def test_payload_carries_run_id_and_the_chain_stays_valid(self, monkeypatch):
        monkeypatch.setenv("UA_EVENT_BUS", "1")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        prior = events.record_event(channel="agent.test.prior", payload={"n": 1})
        prior_row = events.query_events(channels=["agent.test.prior"])[0]
        token = config.agent_identity_var.set(AGENT)
        try:
            _insert_task(AGENT, RUN)
            approval_id = _request("echo event")
        finally:
            config.agent_identity_var.reset(token)

        emitted = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])
        assert len(emitted) == 1
        assert emitted[0]["payload"] == {
            "kind": "approval",
            "ref_id": str(approval_id),
            "summary": "queued: run_cmd",
            "tool": "run_cmd",
            "requested_by": AGENT,
            "run_id": RUN,
        }
        assert "echo event" not in json.dumps(emitted)
        report = events.verify_event_chain()
        assert report["status"] == "valid"
        assert report["verified"] is True
        again = events.query_events(channels=["agent.test.prior"])[0]
        assert again["ulid"] == prior
        assert again["entry_hash"] == prior_row["entry_hash"]
        assert again["payload"] == {"n": 1}


class TestReaders:
    def test_http_list_and_detail_expose_run_id(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        token = config.agent_identity_var.set(AGENT)
        try:
            _insert_task(AGENT, RUN)
            approval_id = _request("echo http")
            detail = anyio.run(webui_api.approval_detail_payload, approval_id)
            listing = anyio.run(webui_api.approvals_payload)
        finally:
            config.agent_identity_var.reset(token)

        assert detail["run_id"] == RUN
        pending = listing["pending"]
        assert len(pending) == 1
        assert pending[0]["run_id"] == RUN
        assert "payload" not in pending[0]

        opened = platform_readers.read_approval(str(approval_id))
        assert opened["approval"]["run_id"] == RUN

        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            listed = client.get("/api/approvals")
            one = client.get(f"/api/approvals/{approval_id}")
        assert listed.status_code == 200
        assert one.status_code == 200
        body = listed.json()
        assert body["pending"][0]["id"] == approval_id
        assert body["pending"][0]["run_id"] == RUN
        assert one.json()["run_id"] == RUN


class TestMigration:
    def test_column_add_is_idempotent_on_pending_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        assert storage.SCHEMA_VERSION == 43
        storage.init_db()
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(db)
        try:
            stored = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            assert stored == "43"
            # Pre-#1653 approvals, built explicitly. DROP COLUMN reparses the
            # stored CREATE text, and a comma in the comment before the
            # rightmost column raises "incomplete input" on SQLite builds
            # older than the 2025-12-02 fix.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE approvals")
            conn.execute(
                """
                CREATE TABLE approvals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tool TEXT NOT NULL,
                  payload TEXT NOT NULL DEFAULT '{}',
                  requested_by TEXT,
                  status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','denied','consumed','revoked','expired')),
                  reason TEXT,
                  resolved_by TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resolved_at TEXT,
                  org_id TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at) "
                "VALUES ('run_cmd', '{}', ?, 'pending', ?, ?)",
                (
                    (AGENT, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                    ("other-peer", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                ),
            )
            conn.execute("CREATE INDEX idx_approvals_status ON approvals(status)")
            conn.execute("CREATE INDEX idx_approvals_tool ON approvals(tool)")
            conn.execute("CREATE INDEX idx_approvals_org_id ON approvals(org_id)")
            conn.commit()
            assert "run_id" not in {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
        finally:
            conn.close()

        storage.init_db()
        storage.init_db()

        conn = sqlite3.connect(db)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
            assert "run_id" in cols
            rows = conn.execute(
                "SELECT tool, requested_by, status, run_id FROM approvals ORDER BY id"
            ).fetchall()
            assert rows == [
                ("run_cmd", AGENT, "pending", None),
                ("run_cmd", "other-peer", "pending", None),
            ]
            assert (
                conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
                == stored
            )
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='approvals'"
                )
            }
            assert "idx_approvals_run_id" in indexes
        finally:
            conn.close()
        assert storage.SCHEMA_VERSION == 43
