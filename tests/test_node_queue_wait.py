"""Direct enqueue honours node freshness: offline targets wait visibly, then expire (task #2110).

Automatic selection (``select_node_for_agent``) already refuses a node whose
heartbeat is not live. A direct enqueue used to accept any registered node
silently, so work for a device that was gone for days launched whenever it
came back. Now the item is accepted only as waiting, with a reason and a
deadline, and it expires rather than launching late.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import node_queue, storage
from unified_agent.dispatch import coordinator

NODE = "dev1"
AGENT = "Desktop_Agent2"


@pytest.fixture
def hub(tmp_path):
    storage.init_db()
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True)
    node_queue.register(
        NODE,
        identity=AGENT,
        facts={
            "os": {"system": "Linux"},
            "backends": ["codex"],
            "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 1}],
        },
    )
    return root


def _set_heartbeat(value: str | None) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (value, NODE))
        conn.commit()
    finally:
        conn.close()


def _enqueue(root) -> dict:
    return node_queue.enqueue(
        NODE, created_by="Desktop_Agent1", prompt="hi", cwd=str(root / "repo"), backend="codex"
    )


def _item(item_id: str) -> dict:
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM node_dispatch WHERE id=?", (item_id,)).fetchone())
    finally:
        conn.close()


def _long_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).isoformat()


class TestEnqueue:
    def test_live_node_is_not_waiting(self, hub):
        out = _enqueue(hub)
        assert "waiting" not in out
        assert out["status"] == "queued"

    def test_offline_node_is_accepted_as_waiting_with_a_deadline(self, hub):
        _set_heartbeat(_long_ago())
        before = datetime.now(UTC)
        out = _enqueue(hub)
        waiting = out["waiting"]
        assert waiting["reason"] == "node_offline"
        assert waiting["active"] is True
        deadline = datetime.fromisoformat(waiting["expires_at"])
        expected = before + timedelta(seconds=node_queue.NODE_QUEUE_WAIT_DEFAULT_S)
        assert abs((deadline - expected).total_seconds()) < 30

    def test_never_heartbeat(self, hub):
        _set_heartbeat(None)
        assert _enqueue(hub)["waiting"]["reason"] == "node_never_heartbeat"

    def test_wait_window_is_configurable(self, hub, monkeypatch):
        monkeypatch.setenv("UA_NODE_QUEUE_WAIT_S", "120")
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        created = datetime.fromisoformat(out["created_at"])
        deadline = datetime.fromisoformat(out["waiting"]["expires_at"])
        assert abs((deadline - created).total_seconds() - 120) < 2

    @pytest.mark.parametrize("raw", ["soon", "10", str(8 * 24 * 3600)])
    def test_bad_wait_window_refuses_only_when_it_applies(self, hub, monkeypatch, raw):
        """Review (#1235): a malformed window must not break a live node's work,
        and for an offline node it is a NodeError dispatch callers handle."""
        monkeypatch.setenv("UA_NODE_QUEUE_WAIT_S", raw)
        assert "waiting" not in _enqueue(hub)
        _set_heartbeat(_long_ago())
        with pytest.raises(node_queue.NodeError, match="UA_NODE_QUEUE_WAIT_S"):
            _enqueue(hub)


class TestExpiry:
    def test_node_back_before_deadline_claims_the_work(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        claimed = node_queue.claim_next(NODE, identity=AGENT)
        assert claimed is not None and claimed["item_id"] == out["item_id"]

    def test_node_back_after_deadline_never_runs_it(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        later = datetime.now(UTC) + timedelta(seconds=node_queue.NODE_QUEUE_WAIT_DEFAULT_S + 60)
        assert node_queue.claim_next(NODE, identity=AGENT, now=later) is None
        row = _item(out["item_id"])
        assert row["status"] == "failed"
        assert node_queue.WAIT_EXPIRED_REASON in row["receipt_json"]

    def test_hub_sweep_expires_work_for_a_node_that_never_returns(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        assert node_queue.expire_waiting(actor="coordinator") == []
        later = datetime.now(UTC) + timedelta(days=2)
        assert node_queue.expire_waiting(actor="coordinator", now=later) == [out["item_id"]]
        assert _item(out["item_id"])["status"] == "failed"

    def test_coordinator_lease_sweep_runs_the_wait_sweep(self, hub, monkeypatch):
        calls = []
        monkeypatch.setattr(
            node_queue, "expire_waiting", lambda *, actor: calls.append(actor) or ["nd-x"]
        )
        out = coordinator.reclaim_expired_leases()
        assert calls
        assert out["node_wait_expired"] == ["nd-x"]

    def test_cancelled_waiting_item_ends_cancelled_not_failed(self, hub):
        """Review (#1235): an operator's cancel is the outcome, not an expiry."""
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET cancel_requested=1 WHERE id=?", (out["item_id"],)
            )
            conn.commit()
        finally:
            conn.close()
        later = datetime.now(UTC) + timedelta(days=2)
        node_queue.expire_waiting(actor="coordinator", now=later)
        row = _item(out["item_id"])
        assert row["status"] == "cancelled"
        assert row["receipt_json"] is None

    def test_returning_node_records_a_cancel_as_cancelled(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET cancel_requested=1 WHERE id=?", (out["item_id"],)
            )
            conn.commit()
        finally:
            conn.close()
        later = datetime.now(UTC) + timedelta(days=2)
        assert node_queue.claim_next(NODE, identity=AGENT, now=later) is None
        assert _item(out["item_id"])["status"] == "cancelled"

    def test_expiry_is_attributed_to_the_sweep_not_the_caller(self, hub, monkeypatch):
        """Review (#1235): an unrelated enqueuer must not own another item's expiry."""
        recorded = []
        monkeypatch.setattr(
            node_queue, "_record", lambda kind, payload, who: recorded.append((kind, who))
        )
        _set_heartbeat(_long_ago())
        _enqueue(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch_wait SET expires_at=?",
                ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()
        _set_heartbeat(datetime.now(UTC).isoformat())
        _enqueue(hub)  # unrelated work by another caller runs the expiry
        assert ("wait_expired", node_queue.WAIT_SWEEP_ACTOR) in recorded
        assert ("wait_expired", "Desktop_Agent1") not in recorded

    def test_live_items_are_never_expired_by_the_sweep(self, hub):
        out = _enqueue(hub)
        later = datetime.now(UTC) + timedelta(days=30)
        assert node_queue.expire_waiting(actor="coordinator", now=later) == []
        assert _item(out["item_id"])["status"] == "queued"


class TestVisibility:
    def test_listing_shows_the_wait(self, hub):
        _set_heartbeat(_long_ago())
        waiting = _enqueue(hub)
        _set_heartbeat(datetime.now(UTC).isoformat())
        live = _enqueue(hub)
        items = {i["id"]: i for i in node_queue.list_recent_items()}
        assert items[waiting["item_id"]]["waiting"]["reason"] == "node_offline"
        assert items[waiting["item_id"]]["waiting"]["active"] is True
        assert items[live["item_id"]]["waiting"] is None

    def test_claimed_item_is_no_longer_actively_waiting(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        node_queue.claim_next(NODE, identity=AGENT)
        items = {i["id"]: i for i in node_queue.list_recent_items()}
        assert items[out["item_id"]]["waiting"]["active"] is False


@pytest.mark.parametrize("age_s", [0, 30, 89, 91, 600, 86400])
def test_direct_and_automatic_paths_agree_on_liveness(hub, age_s):
    beat = datetime.now(UTC) - timedelta(seconds=age_s)
    _set_heartbeat(beat.isoformat())
    selected = node_queue.select_node_for_agent(AGENT, "codex")
    direct = _enqueue(hub)
    assert selected["ok"] is ("waiting" not in direct)


def test_direct_and_automatic_paths_agree_after_unretire(hub):
    """A beat from before a retire/un-retire counts for neither path (#2196 review).

    The row is put in the state ``node_unretire`` leaves it in: the stored
    beat, still inside the live window, copied into ``heartbeat_floor``.
    """
    beat = (datetime.now(UTC) - timedelta(seconds=30)).isoformat(timespec="seconds")
    _set_heartbeat(beat)
    conn = storage.connect()
    try:
        conn.execute("UPDATE nodes SET heartbeat_floor=last_heartbeat WHERE node=?", (NODE,))
        conn.commit()
    finally:
        conn.close()

    selected = node_queue.select_node_for_agent(AGENT, "codex")
    direct = _enqueue(hub)
    assert selected["ok"] is False
    assert direct["waiting"]["reason"] == "node_offline"

    node_queue.heartbeat(NODE, identity=AGENT)
    assert node_queue.select_node_for_agent(AGENT, "codex")["ok"] is True
    assert "waiting" not in _enqueue(hub)


# --- Adversarial review of #1235 (Codex): ordering, durable settlement, hub
# sweep, preflight. Each test below failed before its fix.


def _scheduled_waiting_run(hub, monkeypatch) -> tuple[str, dict]:
    """A scheduler launch whose node went stale between selection and enqueue.

    Selection saw a live node; ``enqueue`` judged it not live, so the run-linked
    item is waiting while its run holds only the ordinary dispatch lease.
    """
    from unified_agent.dispatch import scheduler
    from unified_agent.dispatch.launch import NodeRoutedLaunchBoundary, RecordingLaunchBoundary
    from unified_agent.tools import delegate, tasks

    monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
    packed = delegate.delegate(
        task_spec=(
            "### Objective\nWait race fixture\n"
            "### Scope\n- src/unified_agent/node_queue.py\n"
            "### Definition of Done\n- a waiting run survives reclamation\n"
        ),
        from_agent="Desktop_Agent5",
        to_agent=AGENT,
        callback="none",
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-23T00:00:00Z]")
    with monkeypatch.context() as m:
        m.setattr(node_queue, "node_wait_reason", lambda _row, _ref: "node_offline")
        out = scheduler.scheduler_tick(
            force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
        )
    assert len(out["launched"]) == 1, out
    run_id = out["launched"][0]["run_id"]
    conn = storage.connect()
    try:
        item = dict(
            conn.execute("SELECT * FROM node_dispatch WHERE run_id=?", (run_id,)).fetchone()
        )
    finally:
        conn.close()
    assert item["status"] == "queued"
    assert coordinator.get_run(run_id)["state"] == "launched"
    return run_id, item


def _set(sql: str, params: tuple) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _past(seconds: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _wait_row(item_id: str) -> dict:
    conn = storage.connect()
    try:
        return dict(
            conn.execute("SELECT * FROM node_dispatch_wait WHERE item_id=?", (item_id,)).fetchone()
        )
    finally:
        conn.close()


class TestReclamationOrdering:
    """Finding 1: run-lease reclamation must not override the wait deadline."""

    def test_waiting_run_survives_its_run_lease_until_the_wait_deadline(self, hub, monkeypatch):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        deadline = _wait_row(item["id"])["expires_at"]

        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        coordinator.reclaim_expired_leases()

        run = coordinator.get_run(run_id)
        assert run["state"] == "launched", run
        assert _item(item["id"])["status"] == "queued"
        # The run's lease now follows the wait deadline, not the 300 s dispatch lease.
        lease = datetime.fromisoformat(run["lease_expires_at"])
        assert abs((lease - datetime.fromisoformat(deadline)).total_seconds()) < 2

        # The node comes back within the wait: it can still claim the work.
        claimed = node_queue.claim_next(NODE, identity=AGENT)
        assert claimed is not None and claimed["item_id"] == item["id"]

    def test_both_deadlines_passed_ends_failed_wait_expired(self, hub, monkeypatch):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        _set("UPDATE node_dispatch_wait SET expires_at=? WHERE item_id=?", (_past(), item["id"]))

        out = coordinator.reclaim_expired_leases()

        row = _item(item["id"])
        assert row["status"] == "failed"
        assert node_queue.WAIT_EXPIRED_REASON in row["receipt_json"]
        run = coordinator.get_run(run_id)
        assert run["state"] == "failed", run
        assert run["failure_reason"] == node_queue.WAIT_EXPIRED_REASON
        assert out["node_wait_expired"] == [item["id"]]
        assert not any(r["run_id"] == run_id for r in out["reclaimed"])


class TestDurableSettlement:
    """Finding 2: a failed linked-run close is retried by a later sweep."""

    @pytest.mark.parametrize("fail_times", [1, 2])
    def test_close_failure_is_retried_by_a_later_sweep(self, hub, monkeypatch, fail_times):
        """One failure: reclamation's wait sweep retries and settles it. Two:
        the retry inside reclamation fails too, the lease loop must still leave
        the run alone (never retryable), and the next sweep settles it."""
        import sqlite3

        from unified_agent.dispatch import scheduler

        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        _set("UPDATE node_dispatch_wait SET expires_at=? WHERE item_id=?", (_past(), item["id"]))

        real = scheduler.record_run_outcome
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)

        monkeypatch.setattr(scheduler, "record_run_outcome", flaky)

        assert node_queue.expire_waiting(actor="coordinator") == [item["id"]]
        assert _item(item["id"])["status"] == "failed"
        assert coordinator.get_run(run_id)["state"] == "launched"
        assert _wait_row(item["id"])["settled_at"] is None

        # Ordinary reclamation of the expired run lease must not hand the run
        # back as retryable while its settlement is pending; it retries it.
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        out = coordinator.reclaim_expired_leases()
        assert not any(r["run_id"] == run_id for r in out["reclaimed"])
        if fail_times == 2:
            assert coordinator.get_run(run_id)["state"] == "launched"
            assert _wait_row(item["id"])["settled_at"] is None
            node_queue.expire_waiting(actor="coordinator")

        run = coordinator.get_run(run_id)
        assert run["state"] == "failed", run
        assert run["failure_reason"] == node_queue.WAIT_EXPIRED_REASON
        assert _wait_row(item["id"])["settled_at"] is not None

    def test_expiry_without_a_run_is_settled_at_once(self, hub):
        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        later = datetime.now(UTC) + timedelta(days=2)
        node_queue.expire_waiting(actor="coordinator", now=later)
        wait = _wait_row(out["item_id"])
        assert wait["expired_at"] is not None
        assert wait["settled_at"] is not None


class TestHubSweep:
    """Finding 3: the hub runs the wait sweep itself, independent of dispatch."""

    def test_sweep_interval_default_and_disable(self, monkeypatch):
        monkeypatch.delenv("UA_NODE_WAIT_SWEEP_S", raising=False)
        assert node_queue.node_wait_sweep_s() == 60
        monkeypatch.setenv("UA_NODE_WAIT_SWEEP_S", "0")
        assert node_queue.node_wait_sweep_s() == 0
        for bad in ("soon", "-1"):
            monkeypatch.setenv("UA_NODE_WAIT_SWEEP_S", bad)
            with pytest.raises(ValueError, match="UA_NODE_WAIT_SWEEP_S"):
                node_queue.node_wait_sweep_s()

    def test_sweep_once_expires_overdue_work_and_never_raises(self, hub, monkeypatch):
        from unified_agent import server

        _set_heartbeat(_long_ago())
        out = _enqueue(hub)
        _set(
            "UPDATE node_dispatch_wait SET expires_at=? WHERE item_id=?", (_past(), out["item_id"])
        )
        assert server._node_wait_sweep_once() == 1
        assert _item(out["item_id"])["status"] == "failed"

        def boom(**_kw):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(node_queue, "expire_waiting", boom)
        assert server._node_wait_sweep_once() == 0

    def test_sweep_loop_repeats_until_cancelled(self, monkeypatch):
        import anyio

        from unified_agent import server

        runs = []
        monkeypatch.setattr(server, "_node_wait_sweep_once", lambda: runs.append(1) or 0)

        async def main():
            with anyio.move_on_after(0.5):
                await server._node_wait_sweep_loop(0.01)

        anyio.run(main)
        assert len(runs) >= 2

    @pytest.mark.parametrize(("raw", "started"), [("60", True), ("0", False)])
    def test_hub_lifespan_starts_the_sweep_unless_disabled(self, monkeypatch, raw, started):
        import anyio

        from unified_agent import mcp_live_state, server

        async def idle(*_a):
            return None

        seen = []

        async def loop(interval):
            seen.append(interval)

        monkeypatch.setattr(mcp_live_state, "poll_loop", idle)
        monkeypatch.setattr(server, "_node_wait_sweep_loop", loop)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_NODE_WAIT_SWEEP_S", raw)

        async def main():
            async with server._hub_lifespan(server.mcp):
                # Tasks the lifespan started run on the next scheduling rounds.
                for _ in range(20):
                    await anyio.sleep(0)

        anyio.run(main)
        assert seen == ([60] if started else [])

    @pytest.mark.parametrize("env", ["UA_NODE_QUEUE_WAIT_S", "UA_NODE_WAIT_SWEEP_S"])
    def test_hub_refuses_to_start_on_a_malformed_setting(self, monkeypatch, capsys, env):
        from unified_agent import server

        monkeypatch.setenv(env, "soon")
        with pytest.raises(SystemExit) as exc:
            server._require_node_queue_settings()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to start" in err and env in err


class TestPreflight:
    """Finding 4: a malformed wait window never consumes an approval."""

    @pytest.mark.parametrize("offline", [True, False])
    def test_malformed_window_is_refused_before_the_approval_gate(self, hub, monkeypatch, offline):
        from unified_agent import safety

        def gate(*_a, **_k):
            raise AssertionError("the approval gate must not be reached")

        monkeypatch.setattr(safety, "check_prod_action_gate", gate)
        monkeypatch.setenv("UA_NODE_QUEUE_WAIT_S", "soon")
        if offline:
            _set_heartbeat(_long_ago())
        with pytest.raises(node_queue.NodeError, match="UA_NODE_QUEUE_WAIT_S") as exc:
            node_queue.request_dispatch(
                NODE, actor="Desktop_Agent1", prompt="hi", cwd=str(hub / "repo"), backend="codex"
            )
        assert exc.value.status == 400


class TestWaitingCancellationSettlement:
    @pytest.mark.parametrize("deadline_passed", [False, True])
    def test_http_cancel_then_claim_closes_wait_and_never_retries(
        self, hub, monkeypatch, deadline_passed
    ):
        import secrets

        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from unified_agent import server, webui
        from unified_agent.dispatch import scheduler
        from unified_agent.dispatch.launch import RecordingLaunchBoundary

        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        if deadline_passed:
            _set(
                "UPDATE node_dispatch_wait SET expires_at=? WHERE item_id=?", (_past(), item["id"])
            )
        token = secrets.token_urlsafe(24)
        monkeypatch.setenv("UA_AGENT_TOKENS", f"{AGENT}:{token}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            cancelled = client.post(
                "/api/action/node-session",
                headers=headers,
                json={"action": "cancel", "item_id": item["id"]},
            )
            assert cancelled.status_code == 200, cancelled.json()
            response = client.post(
                "/api/node/claim", headers=headers, json={"node": NODE, "wait_s": 0}
            )
            assert response.status_code == 200, response.json()
        assert _item(item["id"])["status"] == "cancelled"
        run = coordinator.get_run(run_id)
        assert run["state"] == "failed", run
        assert run["failure_reason"] == "cancel_requested"
        assert _wait_row(item["id"])["expired_at"] is not None
        assert _wait_row(item["id"])["settled_at"] is not None
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        reclaimed = coordinator.reclaim_expired_leases()
        assert not any(row["run_id"] == run_id for row in reclaimed["reclaimed"])
        conn = storage.connect()
        try:
            delegation = conn.execute(
                "SELECT status FROM delegations WHERE id=?", (run["delegation_id"],)
            ).fetchone()
        finally:
            conn.close()
        assert delegation["status"] == "failed"
        tick = scheduler.scheduler_tick(force=True, launch=RecordingLaunchBoundary())
        assert tick["launched"] == []

    @pytest.mark.parametrize("fail_times", [1, 2])
    def test_cancelled_wait_close_failure_is_durable_and_retried(
        self, hub, monkeypatch, fail_times
    ):
        import sqlite3

        from unified_agent.dispatch import scheduler

        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        node_queue.cancel(item["id"], actor="Operator")
        real = scheduler.record_run_outcome
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) <= fail_times:
                raise sqlite3.OperationalError("injected database is locked")
            return real(*args, **kwargs)

        monkeypatch.setattr(scheduler, "record_run_outcome", flaky)
        assert node_queue.claim_next(NODE, identity=AGENT) is None
        assert _item(item["id"])["status"] == "cancelled"
        assert _wait_row(item["id"])["expired_at"] is not None
        assert _wait_row(item["id"])["settled_at"] is None
        assert coordinator.get_run(run_id)["state"] == "launched"
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        reclaimed = coordinator.reclaim_expired_leases()
        assert not any(row["run_id"] == run_id for row in reclaimed["reclaimed"])
        if fail_times == 2:
            assert coordinator.get_run(run_id)["state"] == "launched"
            assert _wait_row(item["id"])["settled_at"] is None
            node_queue.expire_waiting(actor="retry")
        run = coordinator.get_run(run_id)
        assert run["state"] == "failed" and run["failure_reason"] == "cancel_requested"
        assert _wait_row(item["id"])["settled_at"] is not None

    def test_crash_after_cancel_commit_retains_settlement_intent(self, hub, monkeypatch):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        node_queue.cancel(item["id"], actor="Operator")
        with monkeypatch.context() as m:
            m.setattr(node_queue, "_settle_expired_waits", lambda _: None)
            assert node_queue.claim_next(NODE, identity=AGENT) is None
        assert _item(item["id"])["status"] == "cancelled"
        assert coordinator.get_run(run_id)["state"] == "launched"
        assert _wait_row(item["id"])["expired_at"] is not None
        assert _wait_row(item["id"])["settled_at"] is None
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))
        coordinator.reclaim_expired_leases()
        assert coordinator.get_run(run_id)["state"] == "failed"
        assert coordinator.get_run(run_id)["failure_reason"] == "cancel_requested"
        assert _wait_row(item["id"])["settled_at"] is not None

    def test_sweep_recovers_legacy_cancelled_wait_without_marker(self, hub, monkeypatch):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        # This is the durable state the old claim bulk update left behind.
        _set(
            "UPDATE node_dispatch SET status='cancelled', cancel_requested=1 WHERE id=?",
            (item["id"],),
        )
        assert _wait_row(item["id"])["expired_at"] is None
        node_queue.expire_waiting(actor="recovery")
        run = coordinator.get_run(run_id)
        assert run["state"] == "failed" and run["failure_reason"] == "cancel_requested"
        assert _wait_row(item["id"])["settled_at"] is not None


class TestWaitingSweepFailure:
    def test_cancelled_wait_survives_sweep_failure_without_becoming_retryable(
        self, hub, monkeypatch
    ):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        # The governed termination helper can commit cancellation before the
        # independent wait sweep has recorded its settlement marker.
        evidence = f"node:{NODE}:{item['id']}"
        assert node_queue.cancel_for_run_evidence(evidence) == "already_gone"
        assert _item(item["id"])["status"] == "cancelled"
        assert _wait_row(item["id"])["expired_at"] is None
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))

        def broken_sweep(**_kwargs):
            raise RuntimeError("injected wait maintenance failure")

        with monkeypatch.context() as m:
            m.setattr(node_queue, "expire_waiting", broken_sweep)
            outcome = coordinator.reclaim_expired_leases()
        run = coordinator.get_run(run_id)
        assert run["state"] == "launched", run
        assert not any(row["run_id"] == run_id for row in outcome["reclaimed"])
        # When maintenance recovers, normal durable cancellation settlement
        # closes the run. It must never pass through retryable or pending.
        coordinator.reclaim_expired_leases()
        run = coordinator.get_run(run_id)
        assert run["state"] == "failed" and run["failure_reason"] == "cancel_requested"
        assert _wait_row(item["id"])["settled_at"] is not None
        conn = storage.connect()
        try:
            delegation = conn.execute(
                "SELECT status FROM delegations WHERE id=?", (run["delegation_id"],)
            ).fetchone()
        finally:
            conn.close()
        assert delegation["status"] == "failed"

    def test_sweep_failure_does_not_stop_unrelated_unclaimed_run_reclamation(
        self, hub, monkeypatch
    ):
        run_id, item = _scheduled_waiting_run(hub, monkeypatch)
        _set("DELETE FROM node_dispatch_wait WHERE item_id=?", (item["id"],))
        _set("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (_past(), run_id))

        def broken_sweep(**_kwargs):
            raise RuntimeError("injected wait maintenance failure")

        monkeypatch.setattr(node_queue, "expire_waiting", broken_sweep)
        coordinator.reclaim_expired_leases()
        run = coordinator.get_run(run_id)
        assert run["state"] == "retryable" and run["failure_reason"] == "node_unclaimed"
