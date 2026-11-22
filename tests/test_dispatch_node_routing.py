"""Scheduler -> ua-node routed launches (task #1327, I1).

With ``UA_DISPATCH_NODE_ROUTING=1`` the scheduler's launch boundary sends
approved dispatch work for an agent label to that label's registered ua-node,
fail-closed when the node is offline or missing the backend. The dispatch run
lifecycle (lease, cancel, outcome, SLO) is exercised against real persistence;
the local spawn path is a recording stub.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import node_queue, storage
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import (
    NodeRoutedLaunchBoundary,
    RecordingLaunchBoundary,
    RoutedLaunchBoundary,
    StubProcessSpawner,
    scheduler_launch_boundary,
)
from unified_agent.tools import delegate, tasks

NODE = "n1"
AGENT = "Desktop_Agent2"
ROOT = "/home/a/dev"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
    monkeypatch.delenv("UA_DISPATCH_NODE_ROUTING", raising=False)
    storage.init_db()
    return tmp_path


def _spec(title: str = "Node routing fixture") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/node_queue.py\n"
        "### Definition of Done\n"
        "- node-routed scheduler launches keep run lifecycle correct\n"
    )


def _delegated(*, to_agent: str = AGENT) -> dict:
    result = delegate.delegate(
        task_spec=_spec(),
        from_agent="Desktop_Agent5",
        to_agent=to_agent,
        callback="none",
    )
    return {"delegation_id": result["delegation_id"], "task_id": result["task_id"]}


def _approve(task_id: int, *, ts: str = "2026-08-23T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")


def _node(node: str = NODE, *, agent: str = AGENT, backends=("codex",), roots=(ROOT,)):
    node_queue.register(
        node,
        identity=agent,
        facts={
            "os": {"system": "Linux"},
            "backends": list(backends),
            "worktree_roots": [{"path": r, "exists": True, "worktrees": 1} for r in roots],
        },
    )


def _items() -> list[dict]:
    conn = storage.connect()
    try:
        return [{k: r[k] for k in r.keys()} for r in conn.execute("SELECT * FROM node_dispatch")]
    finally:
        conn.close()


def _expire_run_lease(run_id: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET lease_expires_at=?, updated_at=? WHERE run_id=?",
            (past, storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _expire_item_lease(item_id: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch SET lease_expires_at=?, updated_at=? WHERE id=?",
            (past, storage.now(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _task_notes(task_id: int) -> str:
    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == task_id)
    return task.get("notes") or ""


def _live_run(task_id: int, run_id: str, state: str = "running") -> None:
    """Insert a dispatch_runs row directly, reproducing the exact table state
    ``claim_and_dispatch`` leaves behind without driving the coordinator."""
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'test', ?, ?)",
            (run_id, task_id, AGENT, state, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


class TestBoundarySelection:
    def test_flag_off_returns_plain_routed_boundary(self, db):
        boundary = scheduler_launch_boundary()
        assert type(boundary) is RoutedLaunchBoundary
        assert isinstance(boundary.spawner, StubProcessSpawner)

    def test_flag_on_wraps_in_node_boundary(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        boundary = scheduler_launch_boundary()
        assert isinstance(boundary, NodeRoutedLaunchBoundary)
        assert isinstance(boundary.fallback, RoutedLaunchBoundary)
        assert isinstance(boundary.fallback.spawner, StubProcessSpawner)

    def test_no_node_for_label_falls_back(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        packed = _delegated()
        _approve(packed["task_id"])
        fallback = RecordingLaunchBoundary()

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=fallback)
        )

        assert len(out["launched"]) == 1
        assert len(fallback.calls) == 1
        assert _items() == []

    def test_offline_node_fails_closed(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE node=?",
                ("2000-01-01T00:00:00+00:00", NODE),
            )
            conn.commit()
        finally:
            conn.close()
        packed = _delegated()
        _approve(packed["task_id"])
        fallback = RecordingLaunchBoundary()

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=fallback)
        )

        assert out["launched"] == []
        assert fallback.calls == []
        assert _items() == []
        failed = [s for s in out["skipped"] if s.get("reason") == "launch_failed"]
        assert len(failed) == 1
        assert failed[0]["error"].startswith("node_offline:")
        run = coordinator.get_run(failed[0]["run_id"])
        assert run["state"] == "retryable"

    def test_backend_not_advertised_fails_closed(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node(backends=("agy",))
        packed = _delegated()
        _approve(packed["task_id"])
        fallback = RecordingLaunchBoundary()

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=fallback)
        )

        assert out["launched"] == []
        assert fallback.calls == []
        assert _items() == []
        failed = [s for s in out["skipped"] if s.get("reason") == "launch_failed"]
        assert failed[0]["error"] == "node_backend_unavailable:codex"


class TestRoutedLaunch:
    def test_node_dispatch_delivers_assigned_skill_in_prompt(self, db, monkeypatch):
        from unified_agent import skills_registry

        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        skill_dir = Path(os.environ["UA_WORKSPACE"]) / "dispatch-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: dispatch-skill\ndescription: A focused dispatch test skill.\n---\n\n"
            "NODE_DISPATCH_SKILL_MARKER\n"
        )
        skills_registry.publish("dispatch-skill", str(skill_dir), author="test")
        skills_registry.assign("dispatch-skill", "developer", target_type="profile", author="test")
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True,
            launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary()),
        )

        assert len(out["launched"]) == 1
        item = _items()[0]
        assert "NODE_DISPATCH_SKILL_MARKER" in item["prompt"]

    def test_node_prompt_ends_with_the_operating_profile_block(self, db, monkeypatch):
        from unified_agent import operating_profiles, skills_registry

        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        skill_dir = Path(os.environ["UA_WORKSPACE"]) / "order-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: order-skill\ndescription: A prompt-order test skill.\n---\n\n"
            "NODE_ORDER_SKILL_MARKER\n"
        )
        skills_registry.publish("order-skill", str(skill_dir), author="test")
        skills_registry.assign("order-skill", "developer", target_type="profile", author="test")
        monkeypatch.setattr(
            operating_profiles,
            "append_operating_brief",
            lambda prompt, task_id, **_kw: f"{prompt.rstrip()}\n\nOperating profile: ORDER_BRIEF",
        )
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True,
            launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary()),
        )

        assert len(out["launched"]) == 1
        prompt = _items()[0]["prompt"]
        assert "NODE_ORDER_SKILL_MARKER" in prompt
        assert prompt.endswith("Operating profile: ORDER_BRIEF")

    def test_tick_enqueues_one_item_and_launches_run(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])
        fallback = RecordingLaunchBoundary()

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=fallback)
        )

        assert len(out["launched"]) == 1
        run_id = out["launched"][0]["run_id"]
        assert fallback.calls == [], "no local spawn for a node-routed label"

        items = _items()
        assert len(items) == 1
        item = items[0]
        assert item["node"] == NODE
        assert item["run_id"] == run_id
        assert item["task_id"] == packed["task_id"]
        assert item["backend"] == "codex"
        assert item["cwd"] == ROOT
        assert item["created_by"] == "dispatch-scheduler"
        assert item["status"] == "queued"

        run = coordinator.get_run(run_id)
        assert run["state"] == "launched"
        assert run["evidence_ref"] == f"node:{NODE}:{item['id']}"

    def test_worktree_inside_reported_root_is_used(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET worktree_path=? WHERE id=?",
                (f"{ROOT}/repo/.worktrees/task-1", packed["task_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )

        assert _items()[0]["cwd"] == f"{ROOT}/repo/.worktrees/task-1"

    def test_claim_heartbeat_receipt_drives_run_lifecycle(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
        run_id = out["launched"][0]["run_id"]
        item = _items()[0]

        claimed = node_queue.claim_next(NODE, identity=AGENT)
        assert claimed is not None
        assert claimed["item_id"] == item["id"]
        run = coordinator.get_run(run_id)
        assert run["state"] == "running"
        item_lease = datetime.fromisoformat(claimed["lease_expires_at"])
        run_lease = datetime.fromisoformat(run["lease_expires_at"])
        assert run_lease == item_lease

        node_queue.heartbeat(NODE, identity=AGENT, running=item["id"])
        renewed = coordinator.get_run(run_id)
        assert renewed["state"] == "running"
        assert datetime.fromisoformat(renewed["lease_expires_at"]) >= datetime.now(UTC)

        result = node_queue.post_receipt(
            NODE,
            item["id"],
            identity=AGENT,
            receipt={"status": "finished", "text": "done"},
        )
        assert result["run_outcome"]["ok"] is True
        run = coordinator.get_run(run_id)
        assert run["state"] == "finished"
        assert run["evidence_ref"] == f"node:{NODE}:{item['id']}"

        conn = storage.connect()
        try:
            delegation = conn.execute(
                "SELECT status FROM delegations WHERE id=?",
                (packed["delegation_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert delegation["status"] == "completed"
        task = next(
            t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"]
        )
        assert task["status"] == "done"

    def test_cancel_before_claim_never_reaches_node(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
        run_id = out["launched"][0]["run_id"]

        cancelled = coordinator.cancel_run(run_id, actor="Operator", reason="test cancel")
        assert cancelled["ok"] is True

        assert node_queue.claim_next(NODE, identity=AGENT) is None
        assert node_queue.get_item(_items()[0]["id"])["status"] == "cancelled"


class TestLeaseExpiry:
    def test_expired_unclaimed_item_is_retryable(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
        run_id = out["launched"][0]["run_id"]
        item_id = _items()[0]["id"]

        _expire_run_lease(run_id)
        reclaim = coordinator.reclaim_expired_leases()

        run = coordinator.get_run(run_id)
        assert run["state"] == "retryable"
        assert run["failure_reason"] == "node_unclaimed"
        assert node_queue.get_item(item_id)["status"] == "cancelled"
        assert any(r["run_id"] == run_id for r in reclaim["reclaimed"])

        conn = storage.connect()
        try:
            delegation = conn.execute(
                "SELECT status FROM delegations WHERE id=?",
                (packed["delegation_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert delegation["status"] == "pending"

    def test_expired_claimed_item_fails_outcome_unknown(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
        run_id = out["launched"][0]["run_id"]
        item = _items()[0]
        assert node_queue.claim_next(NODE, identity=AGENT) is not None

        _expire_run_lease(run_id)
        _expire_item_lease(item["id"])
        reclaim = coordinator.reclaim_expired_leases()

        run = coordinator.get_run(run_id)
        assert run["state"] == "failed"
        assert run["failure_reason"] == "lease_expired_outcome_unknown"

        stored = node_queue.get_item(item["id"])
        assert stored["status"] == "failed"
        assert stored["receipt"]["status"] == "failed"
        assert stored["receipt"]["reason"] == "lease_expired_outcome_unknown"
        assert stored["receipt"]["hub_generated"] is True

        conn = storage.connect()
        try:
            delegation = conn.execute(
                "SELECT status FROM delegations WHERE id=?",
                (packed["delegation_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert delegation["status"] == "failed"

        assert reclaim["node_expired"] == [
            {
                "run_id": run_id,
                "task_id": packed["task_id"],
                "delegation_id": packed["delegation_id"],
                "node": NODE,
                "item_id": item["id"],
                "failure_reason": "lease_expired_outcome_unknown",
            }
        ]
        # A claimed node run must reconcile, never silently relaunch.
        assert not any(r["run_id"] == run_id for r in reclaim["reclaimed"])

    def test_reclaim_drops_the_items_followups_with_events(self, db, monkeypatch):
        """settle_expired_run ends the item inside reclaim's transaction; the
        post-commit sweep then drops its undelivered follow-ups (#1740)."""
        import json as _json

        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])
        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
        run_id = out["launched"][0]["run_id"]
        item = _items()[0]
        assert node_queue.claim_next(NODE, identity=AGENT) is not None
        ts = storage.now()
        conn = storage.connect()
        try:
            for seq, (fid, status) in enumerate(
                [("nf-00000000000000f1", "queued"), ("nf-00000000000000f2", "claimed")], 1
            ):
                conn.execute(
                    "INSERT INTO node_followups (id, item_id, seq, mode, body, "
                    "idempotency_key, status, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'queue', 'next', ?, ?, ?, ?, ?)",
                    (fid, item["id"], seq, f"key-{fid}", status, AGENT, ts, ts),
                )
            conn.commit()
        finally:
            conn.close()

        _expire_run_lease(run_id)
        _expire_item_lease(item["id"])
        coordinator.reclaim_expired_leases()

        conn = storage.connect()
        try:
            rows = {
                r["id"]: (r["status"], r["reason"])
                for r in conn.execute(
                    "SELECT id, status, reason FROM node_followups WHERE item_id=?",
                    (item["id"],),
                )
            }
            events = [
                _json.loads(r["payload"])
                for r in conn.execute(
                    "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
                )
            ]
        finally:
            conn.close()
        assert rows == {
            "nf-00000000000000f1": ("dropped", "session_ended_before_delivery"),
            # The node acknowledged it and may have sent it: unconfirmed, not "never".
            "nf-00000000000000f2": ("dropped", "delivery_unconfirmed"),
        }
        dropped = sorted(e["followup_id"] for e in events if e.get("kind") == "followup_dropped")
        assert dropped == ["nf-00000000000000f1", "nf-00000000000000f2"]

    def test_tick_after_claimed_expiry_notes_failure_not_retry(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])
        fallback = RecordingLaunchBoundary()
        boundary = NodeRoutedLaunchBoundary(fallback=fallback)

        out = scheduler.scheduler_tick(force=True, launch=boundary)
        run_id = out["launched"][0]["run_id"]
        item = _items()[0]
        assert node_queue.claim_next(NODE, identity=AGENT) is not None
        _expire_run_lease(run_id)
        _expire_item_lease(item["id"])

        tick = scheduler.scheduler_tick(force=True, launch=boundary)

        assert tick["launched"] == []
        assert fallback.calls == []
        assert any(e["run_id"] == run_id for e in tick["node_expired"])
        notes = _task_notes(packed["task_id"])
        assert (
            "[dispatch: failed @ " in notes and f"run={run_id}" in notes and f"node={NODE}" in notes
        )
        assert not any(
            "retryable @ " in line and f"run={run_id}" in line for line in notes.splitlines()
        )


class TestCancellationRace:
    def test_evidence_write_losing_cancel_race_cleans_up(self, db, monkeypatch):
        """A cancellation committed between enqueue and evidence write must not
        leave an unfinalized run or a live queue item behind."""
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        real_enqueue = node_queue.enqueue

        def racing_enqueue(node, **fields):
            out = real_enqueue(node, **fields)
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE dispatch_runs SET termination_outcome='requested' WHERE run_id=?",
                    (fields["run_id"],),
                )
                conn.commit()
            finally:
                conn.close()
            return out

        monkeypatch.setattr(node_queue, "enqueue", racing_enqueue)
        boundary = NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())

        result = coordinator.claim_and_dispatch(
            task_id=packed["task_id"],
            delegation_id=packed["delegation_id"],
            actor="dispatch-scheduler",
            launch=boundary,
        )

        # The run is NOT finalized by the boundary, so claim_and_dispatch must
        # hit the cancellation lock on its launched-transition rather than the
        # already-finalized 'cancelled_during_launch' shortcut.
        assert result["reason"] == "cancellation_in_progress"
        run = coordinator.get_run(result["run_id"])
        assert run["termination_outcome"] == "requested"
        item = node_queue.get_item(_items()[0]["id"])
        assert item["cancel_requested"] == 1
        assert node_queue.claim_next(NODE, identity=AGENT) is None
        assert node_queue.get_item(item["id"])["status"] == "cancelled"


def _launched_run(db_flags) -> tuple[str, dict, dict]:
    """Tick one delegated+approved task onto the registered node."""
    packed = _delegated()
    _approve(packed["task_id"])
    out = scheduler.scheduler_tick(
        force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
    )
    assert len(out["launched"]) == 1
    return out["launched"][0]["run_id"], packed, _items()[0]


class TestCancellationCessation:
    """F1: cancel_run must stop the queue item, and only the node's receipt
    may confirm cessation of node-routed work."""

    def test_cancel_before_claim_confirms_and_cancels_item(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, _packed, item = _launched_run(db)

        result = coordinator.cancel_run(run_id, actor="Operator", reason="stop it")

        assert result["ok"] is True
        assert result["cessation"] == "confirmed"
        run = coordinator.get_run(run_id)
        assert run["state"] == "cancelled"
        assert run["termination_outcome"] == "confirmed"
        assert node_queue.get_item(item["id"])["status"] == "cancelled"
        assert node_queue.claim_next(NODE, identity=AGENT) is None

    def test_cancel_after_claim_holds_slot_until_receipt(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, packed, item = _launched_run(db)
        assert node_queue.claim_next(NODE, identity=AGENT) is not None

        result = coordinator.cancel_run(run_id, actor="Operator", reason="stop it")

        assert result["ok"] is True
        assert result["termination"] == "node_cancel_requested"
        assert result["cessation"] == "unknown"
        run = coordinator.get_run(run_id)
        assert run["state"] == "cancelled"
        assert run["termination_outcome"] == "unknown"
        assert node_queue.get_item(item["id"])["cancel_requested"] == 1

        # Unconfirmed cessation still holds the task's live slot.
        again = coordinator.claim_and_dispatch(
            task_id=packed["task_id"],
            delegation_id=packed["delegation_id"],
            actor="dispatch-scheduler",
            launch=RecordingLaunchBoundary(),
        )
        assert again["launched"] is False
        assert again["reason"] == "cessation_unconfirmed"
        assert again["blocking_run_id"] == run_id

        # The node's next heartbeat relays the cancel to the item.
        hb = node_queue.heartbeat(NODE, identity=AGENT, running=item["id"])
        assert hb["cancel"] == [item["id"]]

        # The receipt the node posts proves the backend exited -> confirmed.
        receipt = node_queue.post_receipt(
            NODE, item["id"], identity=AGENT, receipt={"status": "killed"}
        )
        assert receipt["run_outcome"]["ok"] is True
        run = coordinator.get_run(run_id)
        assert run["state"] == "cancelled"
        assert run["termination_outcome"] == "confirmed"

    def test_lease_expiry_does_not_confirm_cessation(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, _packed, item = _launched_run(db)
        assert node_queue.claim_next(NODE, identity=AGENT) is not None
        coordinator.cancel_run(run_id, actor="Operator", reason="stop it")
        assert coordinator.get_run(run_id)["termination_outcome"] == "unknown"

        _expire_item_lease(item["id"])
        assert node_queue.claim_next(NODE, identity=AGENT) is None

        # The hub-generated lease-expiry close must not promote unknown to
        # confirmed — the node never reported its backend stopped.
        run = coordinator.get_run(run_id)
        assert run["termination_outcome"] == "unknown"
        assert node_queue.get_item(item["id"])["status"] == "failed"


class TestOutcomeUnknownResume:
    """F2: lease-expired node work cannot be resumed — the first run's outcome
    was never reconciled, so a second run must not overlap it."""

    def test_resume_refused_after_reclaim_expiry(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, _packed, item = _launched_run(db)
        assert node_queue.claim_next(NODE, identity=AGENT) is not None
        _expire_run_lease(run_id)
        _expire_item_lease(item["id"])
        coordinator.reclaim_expired_leases()
        assert coordinator.get_run(run_id)["failure_reason"] == ("lease_expired_outcome_unknown")

        result = coordinator.resume_run(run_id, actor="Operator")

        assert result["ok"] is False
        assert result["reason"] == "outcome_unknown"
        assert _items() == [_items()[0]] or len(_items()) == 1

    def test_resume_refused_after_claim_next_expiry(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, _packed, item = _launched_run(db)
        assert node_queue.claim_next(NODE, identity=AGENT) is not None
        _expire_item_lease(item["id"])
        assert node_queue.claim_next(NODE, identity=AGENT) is None
        assert coordinator.get_run(run_id)["failure_reason"] == ("lease_expired_outcome_unknown")

        result = coordinator.resume_run(run_id, actor="Operator")

        assert result["ok"] is False
        assert result["reason"] == "outcome_unknown"


class TestClaimLaunchGate:
    """F3: a queue item linked to a run is invisible until the run is launched."""

    def test_linked_item_waits_for_launched_run(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        packed = _delegated()
        run_id = f"run-{packed['task_id']}-gate"
        ts = storage.now()
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
                "created_at, updated_at) VALUES (?, ?, ?, 'claimed', 'test', ?, ?)",
                (run_id, packed["task_id"], AGENT, ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
        node_queue.enqueue(
            NODE,
            prompt="p",
            cwd=ROOT,
            backend="codex",
            created_by="test",
            run_id=run_id,
            task_id=packed["task_id"],
        )

        # Still 'claimed' between enqueue and the coordinator's launch write.
        assert node_queue.claim_next(NODE, identity=AGENT) is None

        conn = storage.connect()
        try:
            coordinator.transition_run(
                conn, run_id, target_state="launched", expected_states=("claimed",)
            )
            conn.commit()
        finally:
            conn.close()

        claimed = node_queue.claim_next(NODE, identity=AGENT)
        assert claimed is not None
        assert claimed["run_id"] == run_id
        assert coordinator.get_run(run_id)["state"] == "running"

    def test_item_without_run_id_claims_immediately(self, db):
        _node()
        node_queue.enqueue(NODE, prompt="p", cwd=ROOT, backend="codex", created_by="test")
        assert node_queue.claim_next(NODE, identity=AGENT) is not None


class TestResumeRoutesToNode:
    """F5: resume_run's default boundary is the scheduler boundary — node
    routing applies to retries too."""

    def test_resumed_node_unclaimed_run_enqueues_on_node(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        _node()
        run_id, _packed, item = _launched_run(db)
        _expire_run_lease(run_id)
        coordinator.reclaim_expired_leases()
        assert coordinator.get_run(run_id)["failure_reason"] == "node_unclaimed"

        result = coordinator.resume_run(run_id, actor="Operator")

        assert result["ok"] is True
        items = _items()
        assert len(items) == 2
        fresh = [i for i in items if i["id"] != item["id"]][0]
        assert fresh["status"] == "queued"
        assert fresh["run_id"] == run_id
        assert fresh["node"] == NODE
        run = coordinator.get_run(run_id)
        assert run["state"] == "launched"
        assert run["evidence_ref"] == f"node:{NODE}:{fresh['id']}"


class TestWorkspaceDeny:
    """F6: node routing must not bypass a denied workspace binding."""

    def test_denied_binding_blocks_node_launch(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        # Conflicting duplicate bindings resolve WorkspaceDenied (fail closed).
        monkeypatch.setenv("UA_AGENT_WORKSPACES", f"{AGENT}:/one,{AGENT}:/two")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])

        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )

        assert out["launched"] == []
        assert _items() == []
        failed = [s for s in out["skipped"] if s.get("reason") == "launch_failed"]
        assert len(failed) == 1
        assert failed[0]["error"] == "workspace_denied"


class TestMissingWorktreeRoots:
    """F4: a root the node reported missing is never matched or chosen."""

    def test_missing_first_root_falls_back_to_existing(self):
        facts = {
            "worktree_roots": [
                {"path": "/missing/dev", "exists": False},
                {"path": "/home/a/dev", "exists": True},
            ]
        }
        assert node_queue.node_cwd_for_task(facts, "/other/path") == "/home/a/dev"

    def test_missing_root_not_matched_for_worktree(self):
        facts = {
            "worktree_roots": [
                {"path": "/missing/dev", "exists": False},
                {"path": "/home/a/dev"},
            ]
        }
        assert node_queue.node_cwd_for_task(facts, "/missing/dev/wt/task-1") == "/home/a/dev"

    def test_all_missing_returns_none(self):
        facts = {"worktree_roots": [{"path": "/missing/dev", "exists": False}]}
        assert node_queue.node_cwd_for_task(facts, None) is None

    def test_absent_exists_still_qualifies(self):
        facts = {"worktree_roots": [{"path": "/home/a/dev"}]}
        assert node_queue.node_cwd_for_task(facts, "/home/a/dev/wt") == "/home/a/dev/wt"


class TestLiveRunCeiling:
    """The node boundary must not count the run it is launching.

    ``claim_and_dispatch`` inserts the run as ``claimed`` BEFORE it invokes the
    launch boundary, so a ceiling check that counts every live row also counts
    the run asking for a slot: at ``UA_DISPATCH_MAX_LIVE_RUNS=1`` every node
    launch is refused (``max_live_runs_reached:1/1``). Mirror of
    ``test_a_launch_does_not_count_itself_against_the_ceiling`` in
    test_dispatch_routing.py, driven through ``NodeRoutedLaunchBoundary``.
    """

    def test_node_launch_does_not_count_itself_against_the_ceiling(self, db, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "1")
        _node()
        packed = _delegated()
        _approve(packed["task_id"])
        # The coordinator claims the run before the boundary sees it — the run
        # row is already live when the ceiling is counted.
        _live_run(packed["task_id"], "run-asking", "claimed")
        fallback = RecordingLaunchBoundary()
        boundary = NodeRoutedLaunchBoundary(fallback=fallback)

        out = boundary.launch(
            run_id="run-asking",
            task_id=packed["task_id"],
            agent_label=AGENT,
            delegation_id=packed["delegation_id"],
        )

        assert out["ok"] is True, out
        assert out["node_routed"] is True
        assert fallback.calls == [], "a node-routed label must not spawn locally"
        items = _items()
        assert [item["run_id"] for item in items] == ["run-asking"]

        # The ceiling still bites once the host is genuinely occupied — the
        # occupier must be another task's run (one live run per task is already
        # enforced by idx_dispatch_runs_live_task).
        _live_run(_delegated()["task_id"], "occupier", "running")
        refused = boundary.launch(
            run_id="run-asking-2",
            task_id=packed["task_id"],
            agent_label=AGENT,
            delegation_id=None,
        )
        assert refused["ok"] is False
        assert refused["spawned"] is False
        assert refused["node_routed"] is True
        assert refused["error"].startswith("max_live_runs_reached:")
        assert len(_items()) == 1, "a refused launch must not enqueue"
