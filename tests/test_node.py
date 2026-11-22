"""ua-node (task #1327, I1): device ↔ hub contract against an in-process hub.

The "fake hub" is the real route table behind the real auth middleware, driven
through Starlette's TestClient — so these tests exercise the same identity
binding, JSON validation and SQLite queue a device reaches over the tailnet.
Nothing sleeps: long-polls use ``wait_s=0`` and backoff waits are recorded.
"""

from __future__ import annotations

import json
import os
import random
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    agent_lifecycle,
    config,
    host_registry,
    node_agent,
    node_queue,
    server,
    storage,
    webui,
)
from unified_agent.tools.approvals import approval_resolve

CLAUDE_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
CODEX_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
NODE = "desk-01"


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"Desktop_Agent1:{CLAUDE_TOKEN},Desktop_Agent2:{CODEX_TOKEN}"
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    # Static bearers stand in for device-flow tokens here; the device-credential
    # requirement itself is covered in TestDevicePlaneAuth.
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client


def _transport(client, token=CLAUDE_TOKEN):
    def call(path, body, timeout):
        resp = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        return resp.status_code, resp.json()

    return call


class FakeBackend:
    name = "codex"

    def __init__(self, result=None, during=None):
        self.result = result or {"status": "success", "text": "done", "exit_code": 0}
        self.during = during
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.during is not None:
            return self.during(kwargs)
        return self.result


def _facts(root: Path):
    return {
        "os": {"system": "Linux"},
        "harnesses": {"codex": {"path": "/usr/bin/codex", "version": "codex 1.0"}},
        "backends": ["codex"],
        "node_features": node_agent.node_features(),
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 2}],
    }


def _agent(hub, tmp_path, *, backend=None, transport=None, sleeps=None, **kw):
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    fake = backend or FakeBackend()
    agent = node_agent.NodeAgent(
        node=NODE,
        transport=transport or _transport(hub),
        roots=[root],
        backends=("codex",),
        backend_factory=lambda name, agent_label: fake,
        facts_fn=lambda: _facts(root),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
        **kw,
    )
    return agent, fake, root


def _receipt_events() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
        ).fetchall()
    finally:
        conn.close()
    return [p for p in (json.loads(r["payload"]) for r in rows) if p["kind"] == "receipt"]


class TestRegistrationAndHeartbeat:
    def test_register_reports_measured_host_row(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        assert agent.run_once() == "registered"
        assert agent.agent == "Desktop_Agent1"

        rows = {row["host"]: row for row in host_registry.fleet_payload()["hosts"]}
        row = rows[NODE]
        assert row["host_source"] == "node"
        assert row["executor"]["reported"] is True
        assert row["executor"]["agent"] == "Desktop_Agent1"
        assert row["worktrees"]["reported"] is True
        assert row["worktrees"]["total"] == 2
        assert row["worktrees"]["roots"] == [str(root)]
        assert row["node"]["backends"] == ["codex"]
        # The bound agent groups under the node, not under a "desktop" prefix guess.
        assert "Desktop_Agent1" in {a["label"] for a in row["agents"]}
        assert "desktop" not in rows

    def test_heartbeat_refreshes_host_registry_and_claim(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        claimed = agent.claim()
        assert claimed["item_id"] == item["item_id"]
        agent.running_item = claimed["item_id"]
        assert agent.heartbeat()["cancel"] == []

        row = {r["host"]: r for r in host_registry.fleet_payload()["hosts"]}[NODE]
        assert row["last_seen_via"] == "ua_node"
        assert row["executor"]["claim"]["item_id"] == item["item_id"]

    def test_heartbeat_refreshes_surface_names_without_reprobing_harnesses(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, root = _agent(hub, tmp_path)
        measurements = 0

        def measured_facts():
            nonlocal measurements
            measurements += 1
            return _facts(root)

        agent._facts_fn = measured_facts
        agent.register()
        assert measurements == 1
        monkeypatch.setattr(
            node_agent,
            "collect_surfaces",
            lambda: {"hermes_profiles": ["first"], "kanban_assignees": []},
        )
        agent.heartbeat()
        assert node_queue.list_nodes()[NODE]["surfaces"]["hermes_profiles"] == ["first"]

        monkeypatch.setattr(
            node_agent,
            "collect_surfaces",
            lambda: {"hermes_profiles": ["second"], "kanban_assignees": []},
        )
        agent.heartbeat()
        node = node_queue.list_nodes()[NODE]
        assert node["surfaces"]["hermes_profiles"] == ["second"]
        assert node["backends"] == ["codex"]
        assert node["worktree_roots"] == [str(root)]
        assert measurements == 1

    def test_surface_heartbeat_preserves_worktree_measurement_time(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        before = next(h for h in host_registry.fleet_payload()["hosts"] if h["host"] == NODE)
        later = datetime.now(UTC) + timedelta(minutes=1)
        node_queue.heartbeat(
            NODE,
            identity="Desktop_Agent1",
            facts={"surfaces": {"hermes_profiles": ["updated"]}},
            now=later,
        )
        after = next(h for h in host_registry.fleet_payload()["hosts"] if h["host"] == NODE)
        assert after["last_seen"] != before["last_seen"]
        assert after["worktrees"] == before["worktrees"]
        assert after["node"]["surfaces"]["hermes_profiles"] == ["updated"]

    def test_surface_growth_near_facts_limit_keeps_running_lease(self, hub, tmp_path, monkeypatch):
        agent, _, root = _agent(hub, tmp_path)
        agent._facts_fn = lambda: {**_facts(root), "padding": "x" * 63_000}
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        agent.running_item = agent.claim()["item_id"]
        before = node_queue.get_item(item["item_id"])["lease_expires_at"]
        names = [f"profile{i:02d}" + "x" * 50 for i in range(50)]
        monkeypatch.setattr(
            node_agent,
            "collect_surfaces",
            lambda: {"hermes_profiles": names, "kanban_assignees": []},
        )
        later = datetime.now(UTC) + timedelta(minutes=1)
        monkeypatch.setattr(node_queue, "_now_dt", lambda _now: later)
        assert agent.heartbeat()["cancel"] == []
        assert node_queue.get_item(item["item_id"])["lease_expires_at"] > before
        stored = node_queue.list_nodes()[NODE]
        assert 0 < len(stored["surfaces"]["hermes_profiles"]) < len(names)
        conn = storage.connect()
        try:
            raw = conn.execute("SELECT facts_json FROM nodes WHERE node=?", (NODE,)).fetchone()[0]
        finally:
            conn.close()
        assert len(raw.encode("utf-8")) <= node_queue.MAX_FACTS_BYTES

    def test_node_is_bound_to_first_identity(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        intruder, _, _ = _agent(hub, tmp_path, transport=_transport(hub, CODEX_TOKEN))
        with pytest.raises(node_agent.NodeAuthError):
            intruder.register()
        with pytest.raises(node_agent.NodeAuthError):
            intruder.claim()

    def test_unauthenticated_calls_are_refused(self, hub):
        resp = hub.post("/api/node/register", json={"node": NODE})
        assert resp.status_code == 401

    def test_unknown_fields_and_bad_names_rejected(self, hub):
        headers = {"Authorization": f"Bearer {CLAUDE_TOKEN}"}
        assert (
            hub.post("/api/node/register", json={"node": NODE, "x": 1}, headers=headers).status_code
            == 400
        )
        assert (
            hub.post("/api/node/register", json={"node": "../etc"}, headers=headers).status_code
            == 400
        )


class TestLongPollAndExecution:
    def test_idle_when_queue_empty(self, hub, tmp_path):
        agent, fake, _ = _agent(hub, tmp_path)
        agent.run_once()
        assert agent.run_once() == "idle"
        assert fake.calls == []

    def test_long_poll_returns_work_and_receipt_closes_it(self, hub, tmp_path):
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="fix it", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        assert agent.run_once() == "ran"

        call = fake.calls[0]
        assert call["prompt"] == "fix it"
        assert call["cwd"] == str((root / "repo").resolve())
        assert call["agent_label"] == "Desktop_Agent1"
        assert call["session_id"] == item["item_id"]
        assert call["kill_check"]() is False

        stored = node_queue.get_item(item["item_id"])
        assert stored["status"] == "finished"
        assert stored["receipt"]["text"] == "done"
        assert len(_receipt_events()) == 1

    def test_long_poll_waits_then_returns_none(self, hub, tmp_path, monkeypatch):
        from unified_agent.webui import routes

        monkeypatch.setattr(routes, "NODE_CLAIM_POLL_S", 0.01)
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        agent.claim_wait_s = 0.03
        assert agent.claim() is None

    def test_checkout_retargeted_outside_its_root_after_registration_is_refused(
        self, hub, tmp_path
    ):
        """The node re-resolves the cwd at launch, not from what it measured at register.

        A team plan binds a checkout the node reported earlier. If that path is
        swapped for a symlink leaving the root afterwards, the launch is refused.
        """
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        checkout = root / "repo"
        assert checkout.is_dir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        checkout.rmdir()
        checkout.symlink_to(elsewhere)
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(checkout), backend="codex", created_by="Operator"
        )
        agent.run_once()
        assert fake.calls == []
        stored = node_queue.get_item(item["item_id"])
        assert stored["status"] == "failed"
        assert stored["receipt"]["reason"] == "cwd_outside_worktree_roots"

    def test_checkout_retargeted_inside_the_roots_after_approval_is_refused(self, hub, tmp_path):
        """Task #1770: the item binds the planned real path, not only the path string.

        A symlinked checkout retargeted to another directory that is still
        inside the worktree roots passes the root check, so the node compares
        the resolved cwd with the realpath recorded at enqueue and refuses.
        """
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        planned = root / "planned"
        planned.mkdir()
        other = root / "other"
        other.mkdir()
        link = root / "checkout"
        link.symlink_to(planned)
        item = node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(link),
            cwd_realpath=str(planned.resolve()),
            backend="codex",
            created_by="Operator",
        )
        assert node_queue.get_item(item["item_id"])["cwd_realpath"] == str(planned.resolve())
        link.unlink()
        link.symlink_to(other)
        agent.run_once()
        assert fake.calls == []
        stored = node_queue.get_item(item["item_id"])
        assert stored["status"] == "failed"
        assert stored["receipt"]["reason"] == "cwd_realpath_changed"

    def test_checkout_matching_its_planned_realpath_runs(self, hub, tmp_path):
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        link = root / "checkout"
        link.symlink_to(root / "repo")
        node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(link),
            cwd_realpath=str((root / "repo").resolve()),
            backend="codex",
            created_by="Operator",
        )
        assert agent.run_once() == "ran"
        assert fake.calls[0]["cwd"] == str((root / "repo").resolve())

    def test_cwd_realpath_must_be_absolute(self, hub, tmp_path):
        with pytest.raises(node_queue.NodeError, match="cwd_realpath"):
            node_queue.enqueue(
                NODE,
                prompt="p",
                cwd=str(tmp_path),
                cwd_realpath="relative/path",
                backend="codex",
                created_by="Operator",
            )

    @pytest.mark.parametrize("boundary", ["enqueue", "request"])
    def test_old_node_cannot_receive_bound_checkout(self, hub, tmp_path, monkeypatch, boundary):
        # Model an old active node: it honours settings but ignores cwd_realpath.
        monkeypatch.setattr(node_agent, "NODE_FEATURES", ("session_settings",))
        agent, fake, root = _agent(hub, tmp_path)
        agent.register()
        submit = node_queue.enqueue if boundary == "enqueue" else node_queue.request_dispatch
        actor = {"created_by": "Operator"} if boundary == "enqueue" else {"actor": "Operator"}
        with pytest.raises(node_queue.NodeError, match="realpath") as refusal:
            submit(
                NODE,
                prompt="p",
                cwd=str(root / "repo"),
                cwd_realpath=str((root / "repo").resolve()),
                backend="codex",
                **actor,
            )
        assert refusal.value.status == 409
        assert fake.calls == []

    def test_node_downgraded_after_enqueue_cannot_claim_bound_checkout(
        self, hub, tmp_path, monkeypatch
    ):
        agent, fake, root = _agent(hub, tmp_path)
        agent.register()
        checkout = root / "checkout"
        checkout.symlink_to(root / "repo")
        other = root / "other"
        other.mkdir()
        item = node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(checkout),
            cwd_realpath=str((root / "repo").resolve()),
            backend="codex",
            created_by="Operator",
        )
        monkeypatch.setattr(node_agent, "NODE_FEATURES", ("session_settings",))
        agent.register()  # old binary reconnects to the same node
        checkout.unlink()
        checkout.symlink_to(other)
        execute = agent.execute
        # Negative control: the old binary ignores the newly added bound field.
        agent.execute = lambda item: execute({k: v for k, v in item.items() if k != "cwd_realpath"})
        assert agent.run_once() == "idle"
        assert node_queue.get_item(item["item_id"])["status"] == "failed"
        assert fake.calls == []

    def test_old_node_still_accepts_unbound_items(self, hub, tmp_path, monkeypatch):
        monkeypatch.setattr(node_agent, "NODE_FEATURES", ("session_settings",))
        agent, fake, root = _agent(hub, tmp_path)
        agent.register()
        node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        assert agent.run_once() == "ran"
        assert len(fake.calls) == 1

    def test_realpath_side_table_upgrades_legacy_queue_idempotently(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        old = node_queue.enqueue(
            NODE, prompt="old", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        with storage.connect() as conn:
            conn.execute("DROP TABLE node_dispatch_cwd")
        assert node_queue.get_item(old["item_id"])["cwd_realpath"] is None
        storage.init_db()  # same schema version, self-contained table recreated
        new = node_queue.enqueue(
            NODE,
            prompt="bound",
            cwd=str(root / "repo"),
            backend="codex",
            cwd_realpath=str(root / "repo"),
            created_by="Operator",
        )
        storage.init_db()
        with storage.connect() as conn:
            node_queue.ensure_schema(conn)
            node_queue.ensure_schema(conn)
        assert node_queue.get_item(new["item_id"])["cwd_realpath"] == str(root / "repo")
        assert agent.claim()["item_id"] == old["item_id"]

    def test_bound_resolved_path_substituted_before_backend_is_refused(self, hub, tmp_path):
        agent, fake, root = _agent(hub, tmp_path)
        agent.register()
        checkout = root / "repo"
        other = root / "other"
        other.mkdir()
        node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(checkout),
            cwd_realpath=str(checkout),
            backend="codex",
            created_by="Operator",
        )

        def substitute(*args, **kwargs):
            checkout.rmdir()
            checkout.symlink_to(other)
            return fake

        agent._backend_factory = substitute
        agent.run_once()
        assert fake.calls == []

    def test_restart_keeps_the_checkout_realpath_binding(self, hub, tmp_path, monkeypatch):
        """A restarted bound item re-binds the planned realpath (#2011).

        Without it the new approval and item carry only the path string, so a
        checkout retargeted inside the roots would launch unchecked.
        """
        from unified_agent import safety

        agent, fake, root = _agent(hub, tmp_path)
        agent.register()
        planned = str((root / "repo").resolve())
        first = node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(root / "repo"),
            cwd_realpath=planned,
            backend="codex",
            created_by="Operator",
        )
        with storage.connect() as conn:
            conn.execute(
                "UPDATE node_dispatch SET status='finished' WHERE id=?", (first["item_id"],)
            )
        payloads: list[dict] = []
        monkeypatch.setattr(
            safety, "check_prod_action_gate", lambda tool, payload: payloads.append(payload)
        )
        restarted = node_queue.restart(first["item_id"], actor="Operator")
        assert payloads[-1]["cwd_realpath"] == planned
        assert node_queue.get_item(restarted["item_id"])["cwd_realpath"] == planned

    def test_cwd_outside_roots_is_refused_without_running(self, hub, tmp_path):
        agent, fake, _ = _agent(hub, tmp_path)
        agent.run_once()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(outside), backend="codex", created_by="Operator"
        )
        agent.run_once()
        assert fake.calls == []
        stored = node_queue.get_item(item["item_id"])
        assert stored["status"] == "failed"
        assert stored["receipt"]["reason"] == "cwd_outside_worktree_roots"

    def test_backend_not_allowed_on_node(self, hub, tmp_path):
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        agent.backends = ("agy",)
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        agent.run_once()
        assert fake.calls == []
        assert node_queue.get_item(item["item_id"])["receipt"]["reason"].startswith(
            "backend_not_allowed_on_node"
        )

    def test_enqueue_refuses_backend_the_node_does_not_advertise(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.enqueue(NODE, prompt="p", cwd=str(root), backend="agy", created_by="A")
        assert err.value.status == 409

    def test_hub_cancellation_reaches_kill_check(self, hub, tmp_path):
        holder = {}

        def during(kwargs):
            node_queue.cancel(kwargs["session_id"], actor="Operator")
            holder["agent"].heartbeat()
            assert kwargs["kill_check"]() is True
            return {"status": "killed", "text": ""}

        agent, _, root = _agent(hub, tmp_path, backend=FakeBackend(during=during))
        holder["agent"] = agent
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        agent.run_once()
        receipt = node_queue.get_item(item["item_id"])["receipt"]
        assert receipt["status"] == "killed"
        assert receipt["reason"] == "cancelled_by_hub"

    def test_observed_cancellation_wins_over_backend_success(self, hub, tmp_path):
        holder = {}

        def during(kwargs):
            node_queue.cancel(kwargs["session_id"], actor="Operator")
            holder["agent"].heartbeat()
            assert kwargs["kill_check"]() is True
            return {"status": "success", "text": "finished during cancellation"}

        agent, _, root = _agent(hub, tmp_path, backend=FakeBackend(during=during))
        holder["agent"] = agent
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        agent.run_once()
        receipt = node_queue.get_item(item["item_id"])["receipt"]
        assert receipt["status"] == "killed"
        assert receipt["reason"] == "cancelled_by_hub"

    def test_expired_lease_is_failed_without_reexecution(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", timeout_s=10
        )
        assert agent.claim()["item_id"] == item["item_id"]
        later = datetime.now(UTC) + timedelta(seconds=10 + node_queue.LEASE_GRACE_S + 5)
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1", now=later) is None
        stored = node_queue.get_item(item["item_id"])
        assert stored["status"] == "failed"
        assert stored["attempts"] == 1
        assert stored["receipt"]["reason"] == "lease_expired_outcome_unknown"

    def test_receipt_closes_linked_dispatch_run(self, hub, tmp_path, monkeypatch):
        from unified_agent.dispatch import scheduler

        seen = []
        monkeypatch.setattr(
            scheduler,
            "record_run_outcome",
            lambda run_id, state, **kw: seen.append((run_id, state, kw)) or {"ok": True},
        )
        agent, _, root = _agent(hub, tmp_path)
        agent.run_once()
        # The item's linked run must be launched for the node to see the item
        # (a node-routed item is invisible until its run leaves 'claimed').
        run_id, task_id = _linked_run("launched")
        item = node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(root / "repo"),
            backend="codex",
            created_by="A",
            run_id=run_id,
            task_id=task_id,
        )
        agent.run_once()
        evidence_ref = f"node:{NODE}:{item['item_id']}"
        guard_sql, guard_params = node_queue._bound_guard(evidence_ref, node_queue._LIVE_RUN_SQL)
        assert seen == [
            (
                run_id,
                "finished",
                {
                    "actor": "Desktop_Agent1",
                    "failure_reason": None,
                    "evidence_ref": evidence_ref,
                    # Task #1950: the close is fenced to this item, atomically.
                    "guard_sql": guard_sql,
                    "guard_params": guard_params,
                },
            )
        ]


class TestReceiptIdempotency:
    def test_posting_twice_records_one_receipt(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(NODE, prompt="p", cwd=str(root), backend="codex", created_by="A")
        agent.claim()
        first = agent.post_receipt(item["item_id"], {"status": "finished", "text": "one"})
        second = agent.post_receipt(item["item_id"], {"status": "failed", "text": "two"})
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["receipt"]["text"] == "one"
        assert node_queue.get_item(item["item_id"])["status"] == "finished"
        assert len(_receipt_events()) == 1

    def test_lost_response_is_resent_and_deduped(self, hub, tmp_path):
        real = _transport(hub)
        dropped = {"n": 0}

        def lossy(path, body, timeout):
            status, payload = real(path, body, timeout)
            if path == "/api/node/receipt" and dropped["n"] == 0:
                dropped["n"] += 1
                raise node_agent.NodeTransportError("connection reset after send")
            return status, payload

        sleeps: list[float] = []
        agent, _, root = _agent(hub, tmp_path, transport=lossy, sleeps=sleeps)
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="A"
        )
        assert agent.run_once() == "backoff"
        assert agent._pending_receipts, "receipt must be kept for resend"
        assert agent.run_once() == "idle"  # flushes the resend, then polls
        assert agent._pending_receipts == []
        assert node_queue.get_item(item["item_id"])["status"] == "finished"
        assert len(_receipt_events()) == 1

    def test_receipt_for_unclaimed_item_is_refused(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(NODE, prompt="p", cwd=str(root), backend="codex", created_by="A")
        with pytest.raises(node_agent.NodeRequestError) as err:
            agent.post_receipt(item["item_id"], {"status": "finished"})
        assert err.value.status == 409

    def test_late_receipt_does_not_hide_unknown_outcome(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", timeout_s=10
        )
        agent.claim()
        later = datetime.now(UTC) + timedelta(seconds=10 + node_queue.LEASE_GRACE_S + 5)
        conn = storage.connect()
        try:
            node_queue._fail_expired(conn, NODE, later.isoformat(timespec="seconds"))
            conn.commit()
        finally:
            conn.close()
        assert node_queue.get_item(item["item_id"])["status"] == "failed"
        with pytest.raises(node_agent.NodeRequestError) as err:
            agent.post_receipt(item["item_id"], {"status": "finished"})
        assert err.value.status == 409
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1") is None

    def test_enqueue_validates_types(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        with pytest.raises(node_queue.NodeError):
            node_queue.enqueue(
                NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", task_id="7"
            )
        with pytest.raises(node_queue.NodeError):
            node_queue.enqueue(
                NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", timeout_s="x"
            )


class TestReconnectAndBackoff:
    def test_backoff_grows_with_jitter_and_resets(self):
        backoff = node_agent.Backoff(base_s=1, cap_s=8, rng=random.Random(1))
        delays = [backoff.next_delay() for _ in range(6)]
        steps = [1, 2, 4, 8, 8, 8]
        for delay, step in zip(delays, steps, strict=True):
            assert step / 2 <= delay <= step
        backoff.reset()
        assert backoff.next_delay() <= 1

    def test_node_reconnects_after_transport_failures(self, hub, tmp_path):
        real = _transport(hub)
        failures = {"left": 3}

        def flaky(path, body, timeout):
            if failures["left"]:
                failures["left"] -= 1
                raise node_agent.NodeTransportError("connection refused")
            return real(path, body, timeout)

        sleeps: list[float] = []
        agent, _, _ = _agent(hub, tmp_path, transport=flaky, sleeps=sleeps)
        outcomes = [agent.run_once() for _ in range(4)]
        assert outcomes == ["backoff", "backoff", "backoff", "registered"]
        assert len(sleeps) == 3
        assert sleeps[0] <= 1 < sleeps[2] and sleeps[1] <= sleeps[2] * 2
        assert agent.backoff.failures == 0

    def test_hub_500_backs_off(self, tmp_path):
        sleeps: list[float] = []
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=lambda path, body, timeout: (503, {"error": "busy"}),
            roots=[tmp_path],
            facts_fn=dict,
            sleep=sleeps.append,
        )
        assert agent.run_once() == "backoff"
        assert len(sleeps) == 1

    def test_auth_failure_waits_long_and_reregisters(self, tmp_path):
        sleeps: list[float] = []
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=lambda path, body, timeout: (401, {"error": "invalid token"}),
            roots=[tmp_path],
            facts_fn=dict,
            sleep=sleeps.append,
        )
        agent.registered = True
        assert agent.run_once() == "auth_error"
        assert sleeps == [node_agent.AUTH_BACKOFF_S]
        assert agent.registered is False

    def test_hub_forgetting_the_node_triggers_reregistration(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        conn = storage.connect()
        try:
            conn.execute("DELETE FROM nodes")
            conn.commit()
        finally:
            conn.close()
        assert agent.run_once() == "backoff"
        assert agent.run_once() == "registered"


class TestAgentLifecycleGate:
    """Task #2315: a paused or terminated owner admits and claims nothing until resumed."""

    def _set(self, action: str, reason: str = "hold") -> dict:
        verified = config.agent_identity_var.set(config.operator_label())
        try:
            return agent_lifecycle.set_agent_state("Desktop_Agent1", action, reason)
        finally:
            config.agent_identity_var.reset(verified)

    def _enqueue(self, root: Path) -> dict:
        return node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Desktop_Agent1"
        )

    def _refusals(self) -> set[tuple[str, str]]:
        agent_lifecycle.join_refusal_writers()
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT payload FROM event_log WHERE channel=? "
                "AND json_extract(payload, '$.kind')=?",
                (agent_lifecycle.CHANNEL, agent_lifecycle.KIND_REFUSED),
            ).fetchall()
        finally:
            conn.close()
        return {(p["site"], p["state"]) for p in (json.loads(r["payload"]) for r in rows)}

    @pytest.mark.parametrize("action,state", [("pause", "paused"), ("terminate", "terminated")])
    def test_stopped_owner_is_refused_until_resumed(self, hub, tmp_path, action, state):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        queued = self._enqueue(root)
        self._set(action)
        with pytest.raises(node_queue.NodeError) as admitted:
            self._enqueue(root)
        with pytest.raises(node_queue.NodeError) as claimed:
            node_queue.claim_next(NODE, identity="Desktop_Agent1")
        for err in (admitted.value, claimed.value):
            assert (err.status, err.code) == (403, f"agent_{state}")
            assert f"is {state}" in str(err)
        # Review of #2315: the refused claim still settles the stop's cancel,
        # so the stopped owner's queued item reaches a terminal state.
        conn = storage.connect()
        try:
            status = conn.execute(
                "SELECT status FROM node_dispatch WHERE id=?", (queued["item_id"],)
            ).fetchone()["status"]
        finally:
            conn.close()
        assert status == "cancelled"
        assert {("node_queue.native_item", state), ("node_queue.claim", state)} <= self._refusals()
        self._set("resume", reason="")
        # The stop cancelled the item queued before it; resume revives nothing.
        fresh = self._enqueue(root)
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1")["item_id"] == fresh["item_id"]

    def test_heartbeat_relays_the_pause_cancel_once(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = self._enqueue(root)
        node_queue.claim_next(NODE, identity="Desktop_Agent1")
        self._set("pause")
        for _ in range(2):
            beat = node_queue.heartbeat(NODE, identity="Desktop_Agent1", running=item["item_id"])
            assert beat["cancel"] == [item["item_id"]]
        conn = storage.connect()
        try:
            kinds = [
                json.loads(r["payload"])["kind"]
                for r in conn.execute(
                    "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
                )
            ]
        finally:
            conn.close()
        # The stop handler's cancel is the only one; heartbeats only relay it.
        assert kinds.count("cancel_requested") == 1

    def test_node_waits_out_a_pause_without_dropping_its_registration(self, hub, tmp_path):
        sleeps: list[float] = []
        agent, fake, root = _agent(hub, tmp_path, sleeps=sleeps)
        assert agent.run_once() == "registered"
        self._set("pause")
        assert agent.run_once() == "agent_stopped"
        assert sleeps == [node_agent.CLAIM_HOLD_S] and agent.registered is True
        self._set("resume", reason="")
        self._enqueue(root)
        outcomes = [agent.run_once() for _ in range(2)]
        assert "ran" in outcomes and len(fake.calls) == 1

    def _as_operator(self, action, label: str, reason: str = "hold") -> None:
        verified = config.agent_identity_var.set(config.operator_label())
        try:
            agent_lifecycle.set_agent_state(label, action, reason)
        finally:
            config.agent_identity_var.reset(verified)

    def _launched_run(self, label: str) -> tuple[str, int]:
        from unified_agent.tools import tasks

        task_id = tasks.task_add(title="linked")["id"]
        run_id = f"run-{task_id}-tgt"
        ts = storage.now()
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
                "created_at, updated_at) VALUES (?, ?, ?, 'launched', ?, ?, ?)",
                (run_id, task_id, label, "test", ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id, task_id

    def test_paused_dispatch_target_stays_queued_until_resumed(self, hub, tmp_path):
        """A paused run target is not a failed item: claim skips it, resume can take it."""
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        run_id, task_id = self._launched_run("Desktop_Agent2")
        held = node_queue.enqueue(
            NODE,
            prompt="for codex",
            cwd=str(root / "repo"),
            backend="codex",
            created_by="Desktop_Agent1",
            run_id=run_id,
            task_id=task_id,
        )
        self._as_operator("pause", "Desktop_Agent2")
        # The pause cancels the live run. The defect is the claim that still
        # sees a live run whose target is paused and marks the item failed.
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET state='launched', termination_outcome=NULL "
                "WHERE run_id=?",
                (run_id,),
            )
            conn.execute(
                "UPDATE node_dispatch SET status='queued', cancel_requested=0, "
                "completed_at=NULL WHERE id=?",
                (held["item_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        other = self._enqueue(root)
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed is not None and claimed["item_id"] == other["item_id"]
        assert node_queue.get_item(held["item_id"])["status"] == "queued"
        self._as_operator("resume", "Desktop_Agent2", reason="")
        again = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert again is not None and again["item_id"] == held["item_id"]

    def test_permission_refuses_a_paused_non_hermes_target(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        run_id, task_id = self._launched_run("Desktop_Agent2")
        item = node_queue.enqueue(
            NODE,
            prompt="for codex",
            cwd=str(root / "repo"),
            backend="codex",
            created_by="Desktop_Agent1",
            run_id=run_id,
            task_id=task_id,
        )
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed is not None and claimed["item_id"] == item["item_id"]
        self._as_operator("pause", "Desktop_Agent2")
        prompt = {
            "session_id": "sess-paused-target",
            "tool_call": {"toolCallId": "tc-1", "title": "Edit file"},
            "options": [{"optionId": "allow-1", "kind": "allow_once", "name": "Allow"}],
        }
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.request_node_permission(
                NODE, item["item_id"], prompt, identity="Desktop_Agent1"
            )
        assert (err.value.status, err.value.code) == (403, "agent_paused")
        assert node_queue.get_item(item["item_id"])["status"] == "claimed"
        self._as_operator("resume", "Desktop_Agent2", reason="")
        # The pause's dispatch stop cancelled the target's run, so the turn is
        # being stopped: resume revives nothing and the prompt stays denied.
        decided = node_queue.request_node_permission(
            NODE, item["item_id"], prompt, identity="Desktop_Agent1"
        )
        assert decided["status"] == "denied"
        assert decided["reason"] == "item cancellation was requested"


class TestShutdown:
    def test_sigterm_kills_running_item_and_posts_receipt(self, hub, tmp_path):
        holder = {}

        def during(kwargs):
            holder["agent"].stop()  # what the SIGTERM handler does
            assert kwargs["kill_check"]() is True
            return {"status": "killed"}

        agent, _, root = _agent(hub, tmp_path, backend=FakeBackend(during=during), heartbeat_s=3600)
        holder["agent"] = agent
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="A"
        )
        agent.run_forever()  # returns once stopped
        receipt = node_queue.get_item(item["item_id"])["receipt"]
        assert receipt["status"] == "killed"
        assert receipt["reason"] == "node_shutdown"


class TestDispatchRoute:
    def test_dispatch_is_approval_gated(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        headers = {"Authorization": f"Bearer {CODEX_TOKEN}"}
        body = {"node": NODE, "prompt": "p", "cwd": str(root), "backend": "codex"}
        pending = hub.post("/api/node/dispatch", json=body, headers=headers)
        assert pending.status_code == 202
        approval_id = pending.json()["approval_id"]

        token = config.agent_identity_var.set("Desktop_Person")
        try:
            approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(token)

        queued = hub.post("/api/node/dispatch", json=body, headers=headers)
        assert queued.status_code == 201
        item = node_queue.get_item(queued.json()["item_id"])
        assert item["created_by"] == "Desktop_Agent2"
        assert item["agent"] == "Desktop_Agent1"  # runs as the node's bound agent


class TestFactsAndTransport:
    def test_collect_facts_reports_only_binaries_on_path(self, tmp_path, monkeypatch):
        for name in ("UA_CODEX_BIN", "UA_AGY_BIN"):
            monkeypatch.delenv(name, raising=False)
        (tmp_path / "repo" / ".worktrees" / "task-1").mkdir(parents=True)
        paths = {"codex": "/opt/codex", "claude": "/opt/claude"}
        facts = node_agent.collect_facts(
            roots=[tmp_path],
            backends=("codex", "agy", "pi"),
            which=paths.get,
            run=lambda argv, **kw: SimpleNamespace(stdout=f"{argv[0]} 9.9\n", stderr=""),
        )
        assert set(facts["harnesses"]) == {"codex", "claude"}
        assert facts["harnesses"]["codex"]["version"] == "/opt/codex 9.9"
        assert facts["backends"] == ["codex"]
        assert facts["worktree_roots"] == [
            {
                "path": str(tmp_path),
                "realpath": os.path.realpath(tmp_path),
                "exists": True,
                "worktrees": 1,
            }
        ]

    def test_transport_refuses_plain_http_off_tailnet(self):
        with pytest.raises(node_agent.NodeAuthError):
            node_agent.HubTransport("http://hub.example.com:8765")
        assert node_agent.HubTransport("http://100.64.0.10:8765/mcp").hub == (
            "http://100.64.0.10:8765"
        )

    def test_transport_without_sign_in_is_an_auth_error(self):
        transport = node_agent.HubTransport("http://127.0.0.1:1", token_fn=lambda: None)
        with pytest.raises(node_agent.NodeAuthError):
            transport("/api/node/register", {}, 1)

    def test_default_node_name_is_sanitized(self, monkeypatch):
        monkeypatch.setenv("UA_NODE_NAME", "my host/1")
        assert node_agent.default_node_name() == "my-host-1"


def _linked_run(state: str = "running", termination_outcome: str | None = None) -> tuple[str, int]:
    from unified_agent.tools import tasks

    task_id = tasks.task_add(title="linked")["id"]
    run_id = f"run-{task_id}-feedbeef"
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "termination_outcome, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, "Desktop_Agent1", state, "test", termination_outcome, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id, task_id


class TestDevicePlaneAuth:
    def test_static_bearer_refused_without_opt_in(self, hub, monkeypatch):
        monkeypatch.delenv("UA_NODE_ALLOW_BEARER")
        headers = {"Authorization": f"Bearer {CLAUDE_TOKEN}"}
        resp = hub.post("/api/node/register", json={"node": NODE}, headers=headers)
        assert resp.status_code == 403
        assert "device sign-in" in resp.json()["error"]

    def test_device_bound_node_refuses_other_devices(self, hub, tmp_path, monkeypatch):
        from unified_agent.webui import routes

        binding = {"device": "laptop-a"}
        monkeypatch.setattr(routes, "_device_binding_name", lambda: binding["device"])
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        assert node_queue.list_nodes()[NODE]["device_node"] == "laptop-a"
        binding["device"] = None  # e.g. a static bearer for the same agent label
        with pytest.raises(node_agent.NodeAuthError):
            agent.claim()
        binding["device"] = "laptop-b"
        with pytest.raises(node_agent.NodeAuthError):
            agent.register()


class TestReviewHardening:
    def test_spooled_receipt_survives_restart(self, hub, tmp_path):
        real = _transport(hub)

        def down_for_receipts(path, body, timeout):
            if path == "/api/node/receipt":
                raise node_agent.NodeTransportError("hub unreachable")
            return real(path, body, timeout)

        agent, _, root = _agent(hub, tmp_path, transport=down_for_receipts)
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="A"
        )
        assert agent.run_once() == "backoff"
        assert (tmp_path / "spool" / f"{item['item_id']}.json").exists()

        # The process dies here; a fresh one reloads the spool and delivers it.
        restarted, fake, _ = _agent(hub, tmp_path)
        assert [p[0] for p in restarted._pending_receipts] == [item["item_id"]]
        restarted.run_once()
        assert node_queue.get_item(item["item_id"])["status"] == "finished"
        assert not (tmp_path / "spool" / f"{item['item_id']}.json").exists()
        assert fake.calls == [], "the work must not run a second time"
        assert len(_receipt_events()) == 1

    def test_expired_attempt_cannot_be_reclaimed(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", timeout_s=10
        )
        assert agent.claim()["attempt"] == 1
        later = datetime.now(UTC) + timedelta(seconds=10 + node_queue.LEASE_GRACE_S + 5)
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1", now=later) is None
        assert node_queue.get_item(item["item_id"])["attempts"] == 1
        with pytest.raises(node_agent.NodeRequestError) as err:
            agent.post_receipt(item["item_id"], {"status": "finished", "attempt": 1})
        assert err.value.status == 409

    def test_duplicate_receipt_retries_a_failed_run_close(self, hub, tmp_path, monkeypatch):
        from unified_agent.dispatch import scheduler

        calls = []

        def flaky_outcome(run_id, state, **kw):
            calls.append(state)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return {"ok": True}

        monkeypatch.setattr(scheduler, "record_run_outcome", flaky_outcome)
        run_id, _ = _linked_run()
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", run_id=run_id
        )
        agent.claim()
        first = agent.post_receipt(item["item_id"], {"status": "finished"})
        assert first["run_outcome"]["ok"] is False
        second = agent.post_receipt(item["item_id"], {"status": "finished"})
        assert second["duplicate"] is True
        assert second["run_outcome"] == {"ok": True}
        assert calls == ["finished", "finished"]

    def test_lease_expiry_closes_linked_run(self, hub, tmp_path, monkeypatch):
        from unified_agent.dispatch import scheduler

        seen = []
        monkeypatch.setattr(
            scheduler,
            "record_run_outcome",
            lambda run_id, state, **kw: (
                seen.append((run_id, state, kw["failure_reason"])) or {"ok": True}
            ),
        )
        run_id, _ = _linked_run()
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root), backend="codex", created_by="A", run_id=run_id
        )
        agent.claim()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", item["item_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        assert agent.claim() is None
        assert node_queue.get_item(item["item_id"])["status"] == "failed"
        assert seen == [(run_id, "failed", "lease_expired_outcome_unknown")]

    def test_cancelled_linked_run_never_reaches_the_node(self, hub, tmp_path):
        run_id, _ = _linked_run(state="cancelled", termination_outcome="confirmed")
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="A", run_id=run_id
        )
        assert agent.run_once() == "idle"
        assert fake.calls == []
        assert node_queue.get_item(item["item_id"])["status"] == "cancelled"

    def test_invalid_dispatch_is_refused_before_the_approval_gate(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        headers = {"Authorization": f"Bearer {CODEX_TOKEN}"}
        bad = {"node": NODE, "prompt": "p", "cwd": str(root), "backend": "agy"}
        assert hub.post("/api/node/dispatch", json=bad, headers=headers).status_code == 409
        conn = storage.connect()
        try:
            parked = conn.execute(
                "SELECT COUNT(*) c FROM approvals WHERE tool=?", (node_queue.NODE_DISPATCH_TOOL,)
            ).fetchone()["c"]
        finally:
            conn.close()
        assert parked == 0

    def test_receipt_fields_are_bounded(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        item = node_queue.enqueue(NODE, prompt="p", cwd=str(root), backend="codex", created_by="A")
        agent.claim()
        stored = agent.post_receipt(
            item["item_id"],
            {
                "status": "finished",
                "exit_code": {"nested": "x" * 10_000},
                "backend_status": "y" * 10_000,
                "files_modified": ["z" * 10_000] * 500,
                "started_at": "t" * 10_000,
                "unexpected": "dropped",
            },
        )["receipt"]
        assert stored["exit_code"] is None
        assert len(stored["backend_status"]) == 40
        assert len(stored["files_modified"]) == 200
        assert all(len(f) == 500 for f in stored["files_modified"])
        assert len(stored["started_at"]) == 40
        assert "unexpected" not in stored


class TestSelectNodeForAgent:
    """Hub-side node selection for scheduler-routed dispatch (task #1327, I2)."""

    def _register(
        self,
        node: str,
        *,
        agent: str = "Desktop_Agent2",
        backends=("codex",),
        heartbeat: str | None = None,
    ):
        node_queue.register(
            node,
            identity=agent,
            facts={
                "backends": list(backends),
                "worktree_roots": [{"path": "/home/a/dev", "exists": True, "worktrees": 1}],
            },
        )
        if heartbeat is not None:
            conn = storage.connect()
            try:
                conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (heartbeat, node))
                conn.commit()
            finally:
                conn.close()

    def test_no_registered_node_is_not_routed(self, hub):
        assert node_queue.select_node_for_agent("Desktop_Agent2", "codex") == {"routed": False}

    def test_node_for_a_different_agent_is_not_routed(self, hub):
        self._register("n1", agent="Desktop_Agent1")
        assert node_queue.select_node_for_agent("Desktop_Agent2", "codex") == {"routed": False}

    def test_stale_heartbeat_is_offline(self, hub):
        self._register("n2", heartbeat="2000-01-01T00:00:00+00:00")
        self._register("n1", heartbeat="2000-01-01T00:00:00+00:00")
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["routed"] is True
        assert sel["ok"] is False
        assert sel["error"] == "node_offline:n1,n2"

    def test_live_node_without_backend_is_unavailable(self, hub):
        self._register("n1", backends=("agy",))
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is False
        assert sel["error"] == "node_backend_unavailable:codex"

    def test_non_list_backends_do_not_advertise(self, hub):
        node_queue.register("n1", identity="Desktop_Agent2", facts={"backends": "codex"})
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is False
        assert sel["error"] == "node_backend_unavailable:codex"

    def test_freshest_live_node_wins(self, hub):
        self._register("n1", heartbeat=(datetime.now(UTC) - timedelta(seconds=60)).isoformat())
        self._register("n2")
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is True
        assert sel["node"] == "n2"
        assert sel["facts"]["backends"] == ["codex"]

    def test_future_dated_heartbeat_is_offline(self, hub):
        """A heartbeat stamped past the clock-skew allowance is not live (#1997).

        Before the fix, any future-dated heartbeat counted as live, so a
        stopped node with a rolled-back hub clock or a hand-edited row stayed
        routable and won the freshest-heartbeat sort.
        """
        future = datetime.now(UTC) + timedelta(seconds=node_queue.NODE_LIVE_WINDOW_S * 10)
        self._register("n1", heartbeat=future.isoformat())
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel == {"routed": True, "ok": False, "error": "node_offline:n1"}

    def test_future_dated_heartbeat_does_not_outrank_a_live_node(self, hub):
        future = datetime.now(UTC) + timedelta(hours=1)
        self._register("n1", heartbeat=future.isoformat())
        self._register("n2")
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is True
        assert sel["node"] == "n2"

    def test_heartbeat_within_clock_skew_is_live(self, hub):
        skewed = datetime.now(UTC) + timedelta(seconds=30)
        self._register("n1", heartbeat=skewed.isoformat())
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is True
        assert sel["node"] == "n1"

    def test_heartbeat_tie_breaks_on_node_name(self, hub):
        ts = datetime.now(UTC).isoformat()
        self._register("n2", heartbeat=ts)
        self._register("n1", heartbeat=ts)
        sel = node_queue.select_node_for_agent("Desktop_Agent2", "codex")
        assert sel["ok"] is True
        assert sel["node"] == "n1"


def test_shared_heartbeat_liveness_rule_bounds_both_ends():
    """One rule for routing, host views, team plans and doctor (#1997)."""
    from unified_agent import node_registry

    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    skew = node_registry.NODE_CLOCK_SKEW_S
    window = node_registry.NODE_LIVE_WINDOW_S
    live = node_registry.heartbeat_is_live
    assert live(now, now)
    assert live(now - timedelta(seconds=window), now)
    assert not live(now - timedelta(seconds=window + 1), now)
    assert live(now + timedelta(seconds=skew), now)
    assert not live(now + timedelta(seconds=skew + 1), now)
    assert live((now + timedelta(seconds=skew)).isoformat().replace("+00:00", "Z"), now)
    assert live("2026-10-12T11:59:30", now)  # naive reads as UTC
    for missing in (None, "", "not a time"):
        assert not live(missing, now)


def test_node_heartbeat_status_never_disagrees_with_liveness():
    """Devices status for a node-derived row agrees with routing (#1997).

    ``registry.derive_status`` reads any future stamp as online and its
    online cutoff (120 s) is wider than the node window (90 s), so a node
    routing calls offline still displayed online."""
    from unified_agent import node_registry

    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    skew = node_registry.NODE_CLOCK_SKEW_S
    window = node_registry.NODE_LIVE_WINDOW_S
    status = node_registry.heartbeat_status
    for offset in range(-3 * skew, 2 * window, 5):
        beat = now - timedelta(seconds=offset)
        assert (status(beat, now) == "online") == node_registry.heartbeat_is_live(beat, now)
    assert status(now + timedelta(hours=1), now) == "offline"
    assert status(now + timedelta(seconds=skew), now) == "online"
    assert status(now - timedelta(seconds=window + 1), now) == "idle"
    assert status(now - timedelta(hours=2), now) == "stale"
    assert status(now - timedelta(days=2), now) == "offline"
    for missing in (None, "", "not a time"):
        assert status(missing, now) == "unknown"


class TestNodeCwdForTask:
    FACTS = {
        "worktree_roots": [
            {"path": "/home/a/dev", "exists": True, "worktrees": 3},
            {"path": "/home/a/other", "exists": True, "worktrees": 0},
        ]
    }

    def test_worktree_inside_root_is_returned(self):
        cwd = node_queue.node_cwd_for_task(self.FACTS, "/home/a/dev/repo/.worktrees/t-1")
        assert cwd == "/home/a/dev/repo/.worktrees/t-1"

    def test_worktree_outside_roots_falls_back_to_first_root(self):
        assert node_queue.node_cwd_for_task(self.FACTS, "/tmp/x") == "/home/a/dev"
        assert node_queue.node_cwd_for_task(self.FACTS, None) == "/home/a/dev"

    def test_sibling_string_prefix_is_not_inside(self):
        assert node_queue.node_cwd_for_task(self.FACTS, "/home/a/dev2/x") == "/home/a/dev"

    def test_no_roots_returns_none(self):
        assert node_queue.node_cwd_for_task({}, "/home/a/dev/x") is None
        assert node_queue.node_cwd_for_task({"worktree_roots": [{"exists": True}]}, None) is None


class TestLinkedRunLease:
    """claim_next/heartbeat renew the linked dispatch run's lease (I2)."""

    def _enqueued_linked_item(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.register()
        run_id, _ = _linked_run(state="launched")
        item = node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=str(root / "repo"),
            backend="codex",
            created_by="dispatch-scheduler",
            run_id=run_id,
        )
        return run_id, item, root

    def _run_lease(self, run_id: str) -> str:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT state, lease_expires_at FROM dispatch_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        return row["lease_expires_at"]

    def test_claim_moves_linked_run_to_running(self, hub, tmp_path):
        run_id, item, _ = self._enqueued_linked_item(hub, tmp_path)
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed is not None
        conn = storage.connect()
        try:
            run = conn.execute(
                "SELECT state, lease_expires_at FROM dispatch_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert run["state"] == "running"
        assert datetime.fromisoformat(run["lease_expires_at"]) == datetime.fromisoformat(
            claimed["lease_expires_at"]
        )

    def test_heartbeat_renews_linked_run_lease(self, hub, tmp_path):
        run_id, item, _ = self._enqueued_linked_item(hub, tmp_path)
        node_queue.claim_next(NODE, identity="Desktop_Agent1")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?",
                ("2000-01-01T00:00:00+00:00", run_id),
            )
            conn.commit()
        finally:
            conn.close()
        node_queue.heartbeat(NODE, identity="Desktop_Agent1", running=item["item_id"])
        assert datetime.fromisoformat(self._run_lease(run_id)) > datetime.now(UTC)


class TestSecondReview:
    def test_node_binding_beats_a_remembered_host(self, hub, tmp_path, monkeypatch):
        from unified_agent import registry

        real = registry.list_agents

        def with_old_host(**kw):
            rows = real(**kw)
            for row in rows:
                if row.get("label") == "Desktop_Agent1":
                    row["host"] = "old-desktop"
            return rows

        monkeypatch.setattr(registry, "list_agents", with_old_host)
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        rows = {row["host"]: row for row in host_registry.fleet_payload()["hosts"]}
        assert "Desktop_Agent1" in {a["label"] for a in rows[NODE]["agents"]}
        assert "old-desktop" not in rows

    def test_unwritable_session_dir_fails_the_item_not_the_node(self, hub, tmp_path):
        agent, fake, root = _agent(hub, tmp_path)
        agent.session_root = tmp_path / "blocked"
        agent.session_root.write_text("not a directory")
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="A"
        )
        assert agent.run_once() == "ran"
        assert fake.calls == []
        receipt = node_queue.get_item(item["item_id"])["receipt"]
        assert receipt["status"] == "failed"
        assert receipt["reason"].startswith("session_dir_unavailable")

    def test_offline_node_does_not_absorb_activity_elsewhere(self, hub, tmp_path, monkeypatch):
        from unified_agent import registry

        real = registry.list_agents

        def on_laptop(**kw):
            rows = real(**kw)
            for row in rows:
                if row.get("label") == "Desktop_Agent1":
                    row["host"] = "laptop-02"
            return rows

        monkeypatch.setattr(registry, "list_agents", on_laptop)
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE node=?",
                ("2000-01-01T00:00:00+00:00", NODE),
            )
            conn.execute(
                "UPDATE host_reports SET last_seen=? WHERE host=?",
                ("2000-01-01T00:00:00+00:00", NODE),
            )
            conn.commit()
        finally:
            conn.close()
        rows = {row["host"]: row for row in host_registry.fleet_payload()["hosts"]}
        # A remembered hostname is not a host row unless it is a doctor snapshot
        # or a ua-node. The offline node still must not absorb the agent.
        assert "laptop-02" not in rows
        assert rows[NODE]["agents"] == []
        assert rows[NODE]["staleness"] not in ("online", "idle")

    def test_offline_node_keeps_its_agent_without_looking_alive(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.register()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE node=?",
                ("2000-01-01T00:00:00+00:00", NODE),
            )
            conn.execute(
                "UPDATE host_reports SET last_seen=? WHERE host=?",
                ("2000-01-01T00:00:00+00:00", NODE),
            )
            conn.commit()
        finally:
            conn.close()
        rows = {row["host"]: row for row in host_registry.fleet_payload()["hosts"]}
        assert "Desktop_Agent1" in {a["label"] for a in rows[NODE]["agents"]}
        assert "desktop" not in rows
        # The agent's own recent requests do not revive the node row.
        assert rows[NODE]["last_seen"] == "2000-01-01T00:00:00+00:00"
        assert rows[NODE]["staleness"] not in ("online", "idle")


class TestVaultSyncFact:
    """Task #1813: a node's failing vault sync is visible from the hub doctor."""

    def _fleet_check(self):
        from unified_agent.tools import doctor

        return doctor._fleet_vault_sync_check()

    def test_heartbeat_reports_probe_once_per_interval_and_doctor_flags_it(self, hub, tmp_path):
        calls = []

        def probe():
            calls.append(1)
            return {
                "status": "MISCONFIGURED",
                "detail": "set UA_VAULT_SYNC_REMOTE_HOST and UA_VAULT_SYNC_REMOTE_ROOT",
                "as_of": _now_iso(),
            }

        agent, _, _ = _agent(hub, tmp_path, vault_sync_fn=probe)
        agent.register()
        agent.heartbeat()
        agent.heartbeat()
        assert len(calls) == 1
        assert node_queue.list_nodes()[NODE]["vault_sync"]["status"] == "MISCONFIGURED"
        check = self._fleet_check()
        assert check["status"] == "MISCONFIGURED"
        assert NODE in check["detail"]
        assert "UA_VAULT_SYNC_REMOTE_HOST" in check["detail"]

        agent._vault_sync_probed_at -= node_agent.VAULT_SYNC_PROBE_S
        agent._vault_sync_fn = lambda: {
            "status": "LIVE",
            "detail": "last sync 60s ago",
            "as_of": _now_iso(),
        }
        agent.heartbeat()
        assert node_queue.list_nodes()[NODE]["vault_sync"]["status"] == "LIVE"
        assert self._fleet_check()["status"] == "LIVE"

    def test_dormant_and_offline_nodes_do_not_count(self, hub, tmp_path):
        agent, _, _ = _agent(
            hub,
            tmp_path,
            vault_sync_fn=lambda: {"status": "DORMANT", "detail": "not here", "as_of": _now_iso()},
        )
        agent.register()
        agent.heartbeat()
        assert self._fleet_check()["status"] == "DORMANT"

        node_queue.heartbeat(
            NODE,
            identity="Desktop_Agent1",
            facts={
                "surfaces": {},
                "vault_sync": {"status": "MISCONFIGURED", "detail": "stale report"},
            },
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        assert self._fleet_check()["status"] == "DORMANT"

    def test_malformed_fact_is_dropped(self, hub, tmp_path):
        agent, _, _ = _agent(
            hub, tmp_path, vault_sync_fn=lambda: {"status": "ON FIRE", "detail": "x"}
        )
        agent.register()
        agent.heartbeat()
        assert node_queue.list_nodes()[NODE]["vault_sync"] is None
        assert node_registry_bound("LIVE", "d" * 1000)["detail"] == "d" * 400

    def test_live_node_without_report_or_with_stale_report_is_unknown(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path, vault_sync_fn=lambda: None)
        agent.register()
        agent.heartbeat()
        check = self._fleet_check()
        assert check["status"] == "UNKNOWN" and "no vault sync report yet" in check["detail"]

        node_queue.heartbeat(
            NODE,
            identity="Desktop_Agent1",
            facts={
                "surfaces": {},
                "vault_sync": {
                    "status": "LIVE",
                    "detail": "ok",
                    "as_of": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                },
            },
        )
        check = self._fleet_check()
        assert check["status"] == "UNKNOWN" and "stale" in check["detail"]

    def test_probe_failure_reports_unknown_not_nothing(self, monkeypatch):
        """An omitted fact would leave the hub holding a stale LIVE report."""
        from unified_agent.tools import doctor

        def boom():
            raise RuntimeError("systemctl missing")

        monkeypatch.setattr(doctor, "vault_sync_status", boom)
        state = node_agent.collect_vault_sync_state()
        assert state["status"] == "UNKNOWN" and "systemctl missing" in state["detail"]
        monkeypatch.setattr(doctor, "vault_sync_status", lambda: {"status": "LIVE", "detail": "ok"})
        state = node_agent.collect_vault_sync_state()
        assert state["status"] == "LIVE" and state["as_of"]


def _now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def node_registry_bound(status, detail):
    from unified_agent import node_registry

    return node_registry.bounded_vault_sync_state({"status": status, "detail": detail})


HUB_SHA = "a" * 40
OLD_SHA = "b" * 40


class TestCodeShaFact:
    """Task #1946: a node running an old checkout is visible from the hub doctor."""

    @staticmethod
    def _resolve(root):
        # Bypass the per-process cache so each tree is read fresh.
        return node_agent.code_sha.__wrapped__(root)

    def test_sha_from_a_normal_checkout(self, tmp_path):
        git = tmp_path / "repo" / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text(HUB_SHA + "\n")
        assert self._resolve(tmp_path / "repo") == HUB_SHA

    def test_sha_from_a_packed_ref(self, tmp_path):
        git = tmp_path / "repo" / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "packed-refs").write_text(f"# pack-refs\n{OLD_SHA} refs/heads/main\n")
        assert self._resolve(tmp_path / "repo") == OLD_SHA

    def test_sha_from_a_worktree_dot_git_file(self, tmp_path):
        common = tmp_path / "main" / ".git"
        wt_git = common / "worktrees" / "task"
        wt_git.mkdir(parents=True)
        (wt_git / "commondir").write_text("../..\n")
        (wt_git / "HEAD").write_text("ref: refs/heads/task-branch\n")
        (common / "refs" / "heads").mkdir(parents=True)
        (common / "refs" / "heads" / "task-branch").write_text(OLD_SHA + "\n")
        checkout = tmp_path / "wt"
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {wt_git}\n")
        assert self._resolve(checkout) == OLD_SHA

    def test_non_git_install_is_none(self, tmp_path):
        assert self._resolve(tmp_path) is None
        (tmp_path / ".git").write_text("not a gitdir pointer\n")
        assert self._resolve(tmp_path) is None

    def test_default_root_is_resolved_once_per_process(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            node_agent.hub_runtime, "checkout_head", lambda root: calls.append(root) or HUB_SHA
        )
        node_agent.code_sha.cache_clear()
        try:
            assert node_agent.code_sha() == HUB_SHA
            assert node_agent.code_sha() == HUB_SHA
            assert len(calls) == 1
        finally:
            node_agent.code_sha.cache_clear()

    def _registered(self, hub, tmp_path, monkeypatch, sha):
        monkeypatch.setattr(node_agent, "code_sha", lambda root=None: sha)
        agent, _, _ = _agent(hub, tmp_path, vault_sync_fn=lambda: None)
        agent.register()
        return agent

    def test_register_and_heartbeat_carry_the_sha(self, hub, tmp_path, monkeypatch):
        agent = self._registered(hub, tmp_path, monkeypatch, OLD_SHA)
        assert node_agent.CODE_SHA_FEATURE in agent.hub_features
        assert node_queue.list_nodes()[NODE]["code_sha"] == OLD_SHA
        monkeypatch.setattr(node_agent, "code_sha", lambda root=None: HUB_SHA)
        agent.heartbeat()
        info = node_queue.list_nodes()[NODE]
        assert info["code_sha"] == HUB_SHA
        # A heartbeat refreshes advisory keys only; registration facts survive.
        assert info["backends"] == ["codex"]

    def test_heartbeat_withholds_the_sha_from_a_hub_without_the_feature(
        self, hub, tmp_path, monkeypatch
    ):
        agent = self._registered(hub, tmp_path, monkeypatch, HUB_SHA)
        agent.hub_features = frozenset()
        assert "code_sha" not in agent._heartbeat_facts()

    def test_heartbeat_bounds_and_clears_the_sha(self, hub, tmp_path, monkeypatch):
        self._registered(hub, tmp_path, monkeypatch, HUB_SHA)
        for bad in ("abc123", HUB_SHA.upper(), HUB_SHA + "0", 42):
            node_queue.heartbeat(
                NODE, identity="Desktop_Agent1", facts={"surfaces": {}, "code_sha": HUB_SHA}
            )
            node_queue.heartbeat(
                NODE, identity="Desktop_Agent1", facts={"surfaces": {}, "code_sha": bad}
            )
            assert node_queue.list_nodes()[NODE]["code_sha"] is None, bad
        node_queue.heartbeat(
            NODE, identity="Desktop_Agent1", facts={"surfaces": {}, "code_sha": "c" * 64}
        )
        assert node_queue.list_nodes()[NODE]["code_sha"] == "c" * 64
        # A heartbeat without the key (an older node) keeps the stored value.
        node_queue.heartbeat(NODE, identity="Desktop_Agent1", facts={"surfaces": {}})
        assert node_queue.list_nodes()[NODE]["code_sha"] == "c" * 64

    def _check(self, monkeypatch, hub_sha=HUB_SHA):
        from unified_agent.tools import doctor

        monkeypatch.setattr(doctor, "_hub_code_sha", lambda: hub_sha)
        row = doctor._fleet_node_versions_check()
        assert row["capability"] == "fleet_node_versions"
        assert row["status"] != "MISCONFIGURED"
        return row

    def test_doctor_live_when_node_matches_hub(self, hub, tmp_path, monkeypatch):
        self._registered(hub, tmp_path, monkeypatch, HUB_SHA)
        row = self._check(monkeypatch)
        assert row["status"] == "LIVE" and NODE in row["detail"]

    def test_doctor_degraded_names_the_node_and_short_sha(self, hub, tmp_path, monkeypatch):
        self._registered(hub, tmp_path, monkeypatch, OLD_SHA)
        row = self._check(monkeypatch)
        assert row["status"] == "DEGRADED"
        assert f"{NODE} at {OLD_SHA[:12]}" in row["detail"]
        assert HUB_SHA[:12] in row["detail"]

    def test_doctor_unknown_when_live_node_reports_no_sha(self, hub, tmp_path, monkeypatch):
        self._registered(hub, tmp_path, monkeypatch, None)
        row = self._check(monkeypatch)
        assert row["status"] == "UNKNOWN" and NODE in row["detail"]
        assert self._check(monkeypatch, hub_sha=None)["status"] == "UNKNOWN"

    def test_doctor_skips_offline_nodes(self, hub, tmp_path, monkeypatch):
        self._registered(hub, tmp_path, monkeypatch, OLD_SHA)
        node_queue.heartbeat(
            NODE,
            identity="Desktop_Agent1",
            facts={"surfaces": {}, "code_sha": OLD_SHA},
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        assert self._check(monkeypatch)["status"] == "DORMANT"


# --- Control Center launches (task #2501) ------------------------------------------


def test_launch_key_queues_one_item_and_restart_replays_it(hub, tmp_path):
    agent, _, root = _agent(hub, tmp_path)
    agent.register()
    fields = {"prompt": "p", "cwd": str(root / "repo"), "backend": "codex"}
    first = node_queue.enqueue(NODE, created_by="Operator", launch_key="lk-1", **fields)
    again = node_queue.request_dispatch(NODE, actor="Operator", launch_key="lk-1", **fields)
    assert again == {**first, "replayed": True}
    assert node_queue.item_for_launch_key("lk-2") is None
    with storage.connect() as conn:
        conn.execute("UPDATE node_dispatch SET status='finished' WHERE id=?", (first["item_id"],))
    restarted = node_queue.enqueue(NODE, created_by="Operator", launch_key="lk-2", **fields)
    # A repeated restart answers the item its key queued, before the approval gate.
    replay = node_queue.restart(first["item_id"], actor="Operator", launch_key="lk-2")
    assert replay == {**restarted, "replayed": True}
    with storage.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM node_dispatch").fetchone()[0]
        assert count == 2
        with pytest.raises(storage.sqlite3.IntegrityError):  # the unique index
            conn.execute("UPDATE node_dispatch SET launch_key='lk-1' WHERE launch_key='lk-2'")


def test_recent_items_carry_the_registry_session_id(hub, tmp_path):
    from unified_agent import registry

    agent, _, root = _agent(hub, tmp_path)
    agent.register()
    item = node_queue.enqueue(
        NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
    )
    [listed] = node_queue.list_recent_items()
    assert listed["agent_session_id"] is None  # no session reported yet
    row_id = registry.start_session(
        "Desktop_Agent1", "acp", external_session_id="ext-1", meta={"item_id": item["item_id"]}
    )
    registry.start_session("Desktop_Agent1", "acp", external_session_id="ext-1", meta={})
    with storage.connect() as conn:
        conn.execute("UPDATE node_dispatch SET session_id='ext-1' WHERE id=?", (item["item_id"],))
    [listed] = node_queue.list_recent_items()
    assert listed["session_id"] == "ext-1" and listed["agent_session_id"] == row_id
