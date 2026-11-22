"""Lost claim responses are reconciled before any fresh claim (task #2136).

The hub commits a claim, the response is lost, and the next worker iteration
must not claim a second item while the first stays claimed and unobserved.
These tests drive the real routes, auth middleware and SQLite queue in
process, with credentials generated at runtime, and count backend launches.
"""

from __future__ import annotations

import errno
import json
import os
import random
import secrets
import subprocess
import sys
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_hosted_device_key as hosted_tests
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    device_key,
    device_login,
    node_agent,
    node_claim_journal,
    node_claim_ops,
    node_queue,
    oauth,
    oauth_approver,
    server,
    storage,
    webui,
)

OWNER = "node_owner_" + secrets.token_hex(4)
OTHER = "other_owner_" + secrets.token_hex(4)
OWNER_TOKEN = secrets.token_urlsafe(24)
OTHER_TOKEN = secrets.token_urlsafe(24)
NODE = "recovery-node"


class Crash(BaseException):
    """Stands in for the worker process dying at a chosen point."""


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{OWNER}:{OWNER_TOKEN},{OTHER}:{OTHER_TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    storage.init_db()
    with TestClient(_app()) as client:
        yield client


def _app():
    return server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))


def _transport(client, token=OWNER_TOKEN, log=None):
    def call(path, body, timeout):
        if log is not None:
            log.append((path, json.loads(json.dumps(body))))
        resp = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        return resp.status_code, resp.json()

    return call


def _drop_first_claim(real, *, mode="claim"):
    """Deliver the first matching claim to the hub, then lose its response."""
    state = {"dropped": 0}

    def call(path, body, timeout):
        status, payload = real(path, body, timeout)
        op = body.get("claim_op") or {}
        if path == "/api/node/claim" and op.get("mode") == mode and not state["dropped"]:
            state["dropped"] += 1
            raise node_agent.NodeTransportError("connection reset after the hub committed")
        return status, payload

    return call, state


class CountingBackend:
    name = "codex"

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "text": "done", "exit_code": 0}


def _facts(root: Path):
    return {
        "os": {"system": "Linux"},
        "harnesses": {"codex": {"path": "/usr/bin/codex", "version": "codex 1.0"}},
        "backends": ["codex"],
        "node_features": node_agent.node_features(),
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 1}],
    }


def _agent(transport, tmp_path, *, backend=None, sleeps=None, node=NODE, **kw):
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    fake = backend or CountingBackend()
    agent = node_agent.NodeAgent(
        node=node,
        transport=transport,
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


def _enqueue(root, prompt, node=NODE, **kw):
    return node_queue.enqueue(
        node,
        prompt=prompt,
        cwd=str(root / "repo"),
        backend="codex",
        created_by="operator-test",
        **kw,
    )["item_id"]


def _journal(agent):
    path = agent._claim_journal.path
    return json.loads(path.read_text()) if path.exists() else None


def _op_rows():
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM node_claim_ops").fetchall()]
    finally:
        conn.close()


def _claim_events():
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
        ).fetchall()
    finally:
        conn.close()
    return [p for p in (json.loads(r["payload"]) for r in rows) if p["kind"] == "claim"]


def _register_direct(node=NODE, identity=OWNER, root="/tmp"):
    node_queue.register(node, identity=identity, facts=_facts(Path(root)))


def _op(mode="claim", *, op_id=None, issued_at=None):
    return {
        "id": op_id or node_claim_journal.new_op_id(),
        "mode": mode,
        "issued_at": int(issued_at if issued_at is not None else datetime.now(UTC).timestamp()),
    }


# --- the lost-response regression ---------------------------------------------


class TestLostClaimResponse:
    def test_next_iteration_reconciles_a_not_b(self, hub, tmp_path):
        lossy, dropped = _drop_first_claim(_transport(hub))
        sleeps: list[float] = []
        agent, backend, root = _agent(lossy, tmp_path, sleeps=sleeps)
        assert agent.run_once() == "registered"
        first, second = _enqueue(root, "first"), _enqueue(root, "second")

        assert agent.run_once() == "backoff"
        assert dropped["dropped"] == 1
        # The hub committed A; the worker only knows its pending operation.
        assert node_queue.get_item(first)["status"] == "claimed"
        assert _journal(agent)["state"] == "pending"
        assert backend.calls == []

        assert agent.run_once() == "ran"
        assert [c["prompt"] for c in backend.calls] == ["first"]
        a = node_queue.get_item(first)
        assert a["status"] == "finished" and a["attempts"] == 1
        assert node_queue.get_item(second)["status"] == "queued"
        assert _journal(agent) is None
        assert [e["item_id"] for e in _claim_events()] == [first]

        # Only now does a fresh logical operation take the next item.
        assert agent.run_once() == "ran"
        assert [c["prompt"] for c in backend.calls] == ["first", "second"]

    def test_lost_empty_poll_closes_without_a_second_claim(self, hub, tmp_path):
        lossy, _ = _drop_first_claim(_transport(hub))
        agent, backend, root = _agent(lossy, tmp_path)
        agent.run_once()
        assert agent.run_once() == "backoff"  # empty poll whose answer was lost
        item = _enqueue(root, "arrives after the lost poll")
        # The lost operation is closed as no_claim, never bound to the new item.
        assert agent.run_once() == "reconciled"
        assert node_queue.get_item(item)["status"] == "queued"
        assert agent.run_once() == "ran"
        assert node_queue.get_item(item)["attempts"] == 1
        assert len(backend.calls) == 1

    def test_lost_reconcile_response_is_reconciled_again(self, hub, tmp_path):
        real = _transport(hub)
        lossy, _ = _drop_first_claim(real)
        twice, _ = _drop_first_claim(lossy, mode="reconcile")
        agent, backend, root = _agent(twice, tmp_path)
        agent.run_once()
        item = _enqueue(root, "only")
        assert agent.run_once() == "backoff"
        assert agent.run_once() == "backoff"
        assert agent.run_once() == "ran"
        assert len(backend.calls) == 1
        assert node_queue.get_item(item)["attempts"] == 1

    def test_pre_commit_failure_rolls_back_the_claim_and_its_operation(
        self, hub, tmp_path, monkeypatch
    ):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        item = _enqueue(root, "rolled back")
        real_bind = node_claim_ops.bind

        def bind_then_fail(*args, **kwargs):
            real_bind(*args, **kwargs)
            raise RuntimeError("synthetic failure before commit")

        monkeypatch.setattr(node_claim_ops, "bind", bind_then_fail)
        with pytest.raises(RuntimeError):
            agent.run_once()
        assert node_queue.get_item(item)["status"] == "queued"
        assert node_queue.get_item(item)["attempts"] == 0
        assert _op_rows() == []
        assert _journal(agent)["state"] == "pending"
        monkeypatch.setattr(node_claim_ops, "bind", real_bind)
        assert agent.run_once() == "reconciled"
        assert _op_rows()[0]["state"] == "no_claim"
        assert agent.run_once() == "ran"
        assert len(backend.calls) == 1


# --- worker restarts ------------------------------------------------------------


def _receipts_down(real):
    """A hub that answers everything except receipt posts, until ``up`` is set."""
    state = {"up": False}

    def call(path, body, timeout):
        if path == "/api/node/receipt" and not state["up"]:
            raise node_agent.NodeTransportError("hub unreachable for the receipt")
        return real(path, body, timeout)

    return call, state


def _disk_full(monkeypatch):
    """Receipt spool writes fail; the claim journal's own writes still succeed."""
    real = node_claim_journal.write_private_durable

    def full(path, payload):
        if path.name.startswith("nd-"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, payload)

    monkeypatch.setattr(node_claim_journal, "write_private_durable", full)


class TestReceiptDurability:
    def test_unspooled_receipt_keeps_start_intent_until_the_hub_acknowledges(
        self, hub, tmp_path, monkeypatch
    ):
        log: list = []
        flaky, state = _receipts_down(_transport(hub, log=log))
        agent, backend, root = _agent(flaky, tmp_path)
        agent.run_once()
        first, second = _enqueue(root, "first"), _enqueue(root, "second")
        _disk_full(monkeypatch)
        assert agent.run_once() == "backoff"  # ran, spool failed, receipt send failed
        assert _journal(agent)["state"] == "start_intent"
        assert agent._local_outcomes == {}
        claims_before = len([p for p, _ in log if p == "/api/node/claim"])
        assert [agent.run_once() for _ in range(2)] == ["backoff", "backoff"]
        assert len([p for p, _ in log if p == "/api/node/claim"]) == claims_before
        assert node_queue.get_item(second)["status"] == "queued"
        # Once the hub is reachable the receipt lands and resolves the journal.
        state["up"] = True
        assert agent.run_once() == "reconciled"
        assert _journal(agent) is None
        assert node_queue.get_item(first)["status"] == "finished"
        assert agent.run_once() == "ran"
        assert [c["prompt"] for c in backend.calls] == ["first", "second"]

    def test_restart_after_unspooled_receipt_never_claims_new_work(
        self, hub, tmp_path, monkeypatch
    ):
        flaky, _state = _receipts_down(_transport(hub))
        agent, backend, root = _agent(flaky, tmp_path)
        agent.run_once()
        first, second = _enqueue(root, "first"), _enqueue(root, "second")
        _disk_full(monkeypatch)
        assert agent.run_once() == "backoff"
        del agent  # the process stops with the receipt only in memory
        log: list = []
        restarted, backend2, _ = _agent(_transport(hub, log=log), tmp_path)
        restarted.run_once()
        # The journal still says start_intent and no receipt exists anywhere:
        # the outcome is unknown, so the node holds rather than claim B.
        assert [restarted.run_once() for _ in range(2)] == ["held", "held"]
        assert _journal(restarted)["state"] == "start_intent"
        assert not [p for p, _ in log if p == "/api/node/claim"]
        assert node_queue.get_item(first)["status"] == "claimed"
        assert node_queue.get_item(second)["status"] == "queued"
        assert len(backend.calls) == 1 and backend2.calls == []

    def test_spooled_receipt_is_fsynced_with_its_directory(self, tmp_path, monkeypatch):
        agent, _, _ = _agent(lambda *a: (200, {}), tmp_path)
        synced: list[os.stat_result] = []
        real_fsync = os.fsync

        def record(fd):
            synced.append(os.fstat(fd))
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", record)
        item_id = "nd-" + "1" * 16
        assert agent._spool(item_id, {"status": "finished", "attempt": 1}) is True
        file_stat = agent._spool_path(item_id).stat()
        dir_stat = agent.spool_dir.stat()
        assert any((s.st_dev, s.st_ino) == (file_stat.st_dev, file_stat.st_ino) for s in synced)
        assert any((s.st_dev, s.st_ino) == (dir_stat.st_dev, dir_stat.st_ino) for s in synced)
        assert file_stat.st_mode & 0o777 == 0o600
        assert agent._local_outcomes == {item_id: 1}

    def test_failed_spool_records_no_outcome(self, tmp_path, monkeypatch):
        agent, _, _ = _agent(lambda *a: (200, {}), tmp_path)
        _disk_full(monkeypatch)
        assert agent._spool("nd-" + "2" * 16, {"status": "finished", "attempt": 1}) is False
        assert agent._local_outcomes == {}


class TestRestart:
    def test_restart_with_pending_journal_reconciles_after_hub_restart(self, hub, tmp_path):
        lossy, _ = _drop_first_claim(_transport(hub))
        agent, _, root = _agent(lossy, tmp_path)
        agent.run_once()
        first, second = _enqueue(root, "first"), _enqueue(root, "second")
        assert agent.run_once() == "backoff"
        del agent
        with TestClient(_app()) as restarted_hub:
            fresh, backend, _ = _agent(_transport(restarted_hub), tmp_path)
            assert fresh.run_once() == "registered"
            assert fresh.run_once() == "ran"
        assert [c["prompt"] for c in backend.calls] == ["first"]
        assert node_queue.get_item(first)["attempts"] == 1
        assert node_queue.get_item(second)["status"] == "queued"

    def test_crash_after_ready_before_start_intent_runs_once(self, hub, tmp_path, monkeypatch):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        item = _enqueue(root, "ready only")

        def crash(self, entry):
            raise Crash

        with monkeypatch.context() as patch:
            patch.setattr(node_claim_journal.ClaimJournal, "mark_start_intent", crash)
            with pytest.raises(Crash):
                agent.run_once()
        assert _journal(agent)["state"] == "ready"
        assert backend.calls == []
        restarted, backend2, _ = _agent(_transport(hub), tmp_path)
        restarted.run_once()
        assert restarted.run_once() == "ran"
        assert len(backend.calls) + len(backend2.calls) == 1
        assert node_queue.get_item(item)["status"] == "finished"

    @pytest.mark.parametrize("where", ["before_backend", "after_backend"])
    def test_ambiguous_start_intent_holds_and_never_relaunches(
        self, hub, tmp_path, monkeypatch, where
    ):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        first, second = _enqueue(root, "maybe started"), _enqueue(root, "must wait")
        real_execute = node_agent.NodeAgent.execute

        def crash(self, item):
            if where == "after_backend":
                real_execute(self, item)
            raise Crash

        with monkeypatch.context() as patch:
            patch.setattr(node_agent.NodeAgent, "execute", crash)
            with pytest.raises(Crash):
                agent.run_once()
        launched = len(backend.calls)
        assert _journal(agent)["state"] == "start_intent"
        sleeps: list[float] = []
        log: list = []
        restarted, backend2, _ = _agent(_transport(hub, log=log), tmp_path, sleeps=sleeps)
        restarted.run_once()
        for _ in range(3):
            assert restarted.run_once() == "held"
        assert backend2.calls == [] and len(backend.calls) == launched
        assert sleeps[-1] == node_agent.CLAIM_HOLD_S
        assert not [p for p, _ in log if p == "/api/node/claim"]
        assert node_queue.get_item(first)["attempts"] == 1
        assert node_queue.get_item(second)["status"] == "queued"
        # The operator reconciles the item, then releases the hold explicitly.
        restarted._claim_journal.path.unlink()
        assert restarted.run_once() == "ran"
        assert [c["prompt"] for c in backend2.calls] == ["must wait"]

    def test_spooled_receipt_resolves_start_intent(self, hub, tmp_path, monkeypatch):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        first, second = _enqueue(root, "finished"), _enqueue(root, "next")

        def crash(self, entry):
            raise Crash

        with monkeypatch.context() as patch:
            patch.setattr(node_claim_journal.ClaimJournal, "clear", crash)
            with pytest.raises(Crash):
                agent.run_once()
        assert _journal(agent)["state"] == "start_intent"
        restarted, backend2, _ = _agent(_transport(hub), tmp_path)
        restarted.run_once()
        # The spooled receipt is resent and establishes the outcome.
        assert restarted.run_once() == "reconciled"
        assert node_queue.get_item(first)["status"] == "finished"
        assert node_queue.get_item(second)["status"] == "queued"
        assert restarted.run_once() == "ran"
        assert [c["prompt"] for c in backend.calls + backend2.calls] == ["finished", "next"]


# --- concurrent server handlers -------------------------------------------------


class TestConcurrentHandlers:
    def test_reconcile_fences_an_original_that_has_not_reached_its_transaction(
        self, hub, monkeypatch
    ):
        _register_direct()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )["item_id"]
        op = _op()
        paused, release = threading.Event(), threading.Event()
        real_connect = storage.connect

        def connect(*args, **kwargs):
            if threading.current_thread().name == "late-original" and not paused.is_set():
                paused.set()
                assert release.wait(timeout=10)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(storage, "connect", connect)
        result: dict = {}

        def original():
            result.update(node_queue.claim_operation(NODE, op, identity=OWNER))

        thread = threading.Thread(target=original, name="late-original")
        thread.start()
        assert paused.wait(timeout=10)
        reconciled = node_queue.claim_operation(NODE, {**op, "mode": "reconcile"}, identity=OWNER)
        assert reconciled["claim_op"]["state"] == "no_claim"
        assert reconciled["item"] is None
        release.set()
        thread.join(timeout=10)
        assert result["item"] is None
        assert result["claim_op"]["state"] == "no_claim"
        assert node_queue.get_item(item)["status"] == "queued"
        assert node_queue.get_item(item)["attempts"] == 0

    def test_reconcile_waits_for_an_original_inside_its_transaction(self, hub, monkeypatch):
        _register_direct()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )["item_id"]
        op = _op()
        inside, release = threading.Event(), threading.Event()
        real_require = node_queue._require_native_item

        def pause(conn, row):
            if threading.current_thread().name == "slow-original":
                inside.set()
                assert release.wait(timeout=10)
            return real_require(conn, row)

        monkeypatch.setattr(node_queue, "_require_native_item", pause)
        results: dict = {}

        def run(name, mode):
            results[name] = node_queue.claim_operation(NODE, {**op, "mode": mode}, identity=OWNER)

        original = threading.Thread(target=run, args=("original", "claim"), name="slow-original")
        original.start()
        assert inside.wait(timeout=10)
        recon = threading.Thread(target=run, args=("reconcile", "reconcile"), name="recon")
        recon.start()
        recon.join(timeout=0.5)
        assert recon.is_alive(), "reconcile must wait for the open claim transaction"
        release.set()
        original.join(timeout=10)
        recon.join(timeout=10)
        assert results["original"]["item"]["item_id"] == item
        assert results["reconcile"]["item"]["item_id"] == item
        assert results["reconcile"]["claim_op"]["executable"] is True
        assert node_queue.get_item(item)["attempts"] == 1

    def test_concurrent_same_operation_claims_bind_once(self, hub):
        _register_direct()
        items = [
            node_queue.enqueue(
                NODE, prompt=str(i), cwd="/tmp", backend="codex", created_by="operator-test"
            )["item_id"]
            for i in range(3)
        ]
        op = _op()
        barrier = threading.Barrier(5)

        def claim(_index):
            barrier.wait(timeout=10)
            return node_queue.claim_operation(NODE, op, identity=OWNER)

        with ThreadPoolExecutor(5) as pool:
            results = list(pool.map(claim, range(5)))
        assert {r["claim_op"]["item_id"] for r in results} == {items[0]}
        assert [node_queue.get_item(i)["status"] for i in items] == ["claimed", "queued", "queued"]
        assert node_queue.get_item(items[0])["attempts"] == 1
        assert len([r for r in _op_rows() if r["state"] == "bound"]) == 1

    def test_long_poll_original_stops_once_reconciled(self, hub, monkeypatch):
        _register_direct()
        op = _op()
        first = node_queue.claim_operation(NODE, op, identity=OWNER, final=False)
        assert first["claim_op"]["state"] == "pending"
        node_queue.claim_operation(NODE, {**op, "mode": "reconcile"}, identity=OWNER)
        item = node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )["item_id"]
        later = node_queue.claim_operation(NODE, op, identity=OWNER, final=False)
        assert later["claim_op"]["state"] == "no_claim" and later["item"] is None
        assert node_queue.get_item(item)["status"] == "queued"


# --- retention, expiry, capacity -------------------------------------------------


class TestRetentionAndExpiry:
    def test_stale_original_is_refused_outside_its_admission_window(self, hub):
        _register_direct()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )["item_id"]
        old = int(datetime.now(UTC).timestamp()) - node_claim_ops.MAX_AGE_S - 5
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.claim_operation(NODE, _op(issued_at=old), identity=OWNER)
        assert err.value.status == 409
        assert node_queue.get_item(item)["status"] == "queued"
        assert _op_rows() == []

    def test_expired_tombstone_cannot_be_outlived_by_its_original(self, hub):
        _register_direct()
        start = datetime.now(UTC)
        op = _op(issued_at=start.timestamp())
        node_queue.claim_operation(NODE, {**op, "mode": "reconcile"}, identity=OWNER, now=start)
        assert _op_rows()[0]["state"] == "no_claim"
        item = node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )["item_id"]
        later = start + timedelta(seconds=node_claim_ops.TOMBSTONE_RETENTION_S + 5)
        # Any claim transaction runs the bounded cleanup.
        node_queue.claim_operation(
            NODE, _op(issued_at=later.timestamp()), identity=OWNER, now=later
        )
        assert all(r["op_id"] != op["id"] for r in _op_rows())
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.claim_operation(NODE, op, identity=OWNER, now=later)
        assert err.value.status == 409
        # The fresh op above took the item; the deleted fence never reopened.
        assert node_queue.get_item(item)["attempts"] == 1

    def test_bound_record_is_kept_while_its_claim_is_live(self, hub):
        _register_direct()
        start = datetime.now(UTC)
        node_queue.enqueue(
            NODE,
            prompt="p",
            cwd="/tmp",
            backend="codex",
            created_by="operator-test",
            timeout_s=node_queue.MAX_TIMEOUT_S,
        )
        op = _op(issued_at=start.timestamp())
        bound = node_queue.claim_operation(NODE, op, identity=OWNER, now=start)
        item = bound["item"]["item_id"]
        later = start + timedelta(seconds=node_claim_ops.BOUND_RETENTION_S + 5)
        conn = storage.connect()
        try:  # a heartbeat-renewed lease keeps the claim live this long
            conn.execute(
                "UPDATE node_dispatch SET lease_expires_at=? WHERE id=?",
                ((later + timedelta(hours=1)).isoformat(), item),
            )
            conn.commit()
        finally:
            conn.close()
        node_queue.claim_operation(
            NODE, _op(issued_at=later.timestamp()), identity=OWNER, now=later
        )
        assert any(r["op_id"] == op["id"] for r in _op_rows())
        node_queue.post_receipt(
            NODE, item, identity=OWNER, receipt={"status": "finished", "attempt": 1}
        )
        node_queue.claim_operation(
            NODE, _op(issued_at=later.timestamp()), identity=OWNER, now=later
        )
        assert all(r["op_id"] != op["id"] for r in _op_rows())

    def test_capacity_refuses_new_operations_without_evicting(self, hub, monkeypatch):
        _register_direct()
        monkeypatch.setattr(node_claim_ops, "MAX_OPS_PER_NODE", 2)
        kept = [_op(), _op()]
        for op in kept:
            node_queue.claim_operation(NODE, op, identity=OWNER)
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.claim_operation(NODE, _op(), identity=OWNER)
        assert err.value.status == 503
        with pytest.raises(node_queue.NodeError):
            node_queue.claim_operation(NODE, _op("reconcile"), identity=OWNER)
        assert {r["op_id"] for r in _op_rows()} == {op["id"] for op in kept}

    def test_schema_is_idempotent_and_keeps_existing_rows(self, hub):
        _register_direct()
        node_queue.claim_operation(NODE, _op(), identity=OWNER)
        storage.init_db()
        conn = storage.connect()
        try:
            node_queue.ensure_schema(conn)
            node_queue.ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
        assert len(_op_rows()) == 1


# --- two worker processes, one journal ----------------------------------------------


class TestSharedJournal:
    def test_second_worker_claims_nothing_while_the_first_holds_the_lock(self, hub, tmp_path):
        inside, release = threading.Event(), threading.Event()
        real = _transport(hub)

        def slow(path, body, timeout):
            if path == "/api/node/claim" and threading.current_thread().name == "worker-1":
                inside.set()
                assert release.wait(timeout=10)
            return real(path, body, timeout)

        one, backend1, root = _agent(slow, tmp_path)
        log: list = []
        two, backend2, _ = _agent(_transport(hub, log=log), tmp_path)
        one.run_once()
        two.run_once()
        first, second = _enqueue(root, "first"), _enqueue(root, "second")
        outcome: dict = {}
        thread = threading.Thread(
            target=lambda: outcome.update(one=one.run_once()), name="worker-1"
        )
        thread.start()
        assert inside.wait(timeout=10)
        assert two.run_once() == "held"
        assert not [p for p, _ in log if p == "/api/node/claim"]
        release.set()
        thread.join(timeout=10)
        assert outcome["one"] == "ran"
        assert two.run_once() == "ran"
        assert [c["prompt"] for c in backend1.calls] == ["first"]
        assert [c["prompt"] for c in backend2.calls] == ["second"]
        assert node_queue.get_item(first)["attempts"] == 1
        assert node_queue.get_item(second)["attempts"] == 1

    def test_lock_held_by_another_process_holds_this_worker(self, hub, tmp_path):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        item = _enqueue(root, "p")
        lock = agent._claim_journal.lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys\n"
                "f = open(sys.argv[1], 'a')\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "sys.stdin.read()\n",
                str(lock),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            assert agent.run_once() == "held"
            assert node_queue.get_item(item)["status"] == "queued"
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
        assert agent.run_once() == "ran"
        assert len(backend.calls) == 1

    def test_start_intent_from_another_worker_is_never_launched_twice(self, hub, tmp_path):
        both = threading.Barrier(2)
        launches: list[str] = []

        class SlowBackend(CountingBackend):
            def run(self, **kwargs):
                launches.append(kwargs["prompt"])
                return super().run(**kwargs)

        real = _transport(hub)
        one, _, root = _agent(real, tmp_path, backend=SlowBackend())
        two, _, _ = _agent(real, tmp_path, backend=SlowBackend())
        one.run_once()
        two.run_once()
        _enqueue(root, "once")

        def run(agent):
            both.wait(timeout=10)
            return agent.run_once()

        with ThreadPoolExecutor(2) as pool:
            outcomes = sorted(pool.map(run, [one, two]))
        assert launches == ["once"]
        assert outcomes in (["held", "ran"], ["idle", "ran"])


# --- stale authority -----------------------------------------------------------------


class TestStaleAuthority:
    def _lose_then(self, hub, tmp_path):
        lossy, _ = _drop_first_claim(_transport(hub))
        agent, backend, root = _agent(lossy, tmp_path)
        agent.run_once()
        item = _enqueue(root, "stale")
        assert agent.run_once() == "backoff"
        return agent, backend, item

    def test_cancelled_item_is_reconciled_but_not_run(self, hub, tmp_path):
        agent, backend, item = self._lose_then(hub, tmp_path)
        node_queue.cancel(item, actor="operator-test")
        assert agent.run_once() == "reconciled"
        assert backend.calls == []
        assert _journal(agent) is None
        rows = _op_rows()
        assert rows[0]["state"] == "bound" and rows[0]["item_id"] == item

    def test_expired_lease_is_reconciled_but_not_run(self, hub, tmp_path):
        agent, backend, item = self._lose_then(hub, tmp_path)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET lease_expires_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(), item),
            )
            conn.commit()
        finally:
            conn.close()
        assert agent.run_once() == "reconciled"
        assert backend.calls == []
        assert node_queue.get_item(item)["status"] == "failed"

    def test_superseded_attempt_is_not_run(self, hub, tmp_path):
        agent, backend, item = self._lose_then(hub, tmp_path)
        conn = storage.connect()
        try:
            conn.execute("UPDATE node_dispatch SET attempts=attempts+1 WHERE id=?", (item,))
            conn.commit()
        finally:
            conn.close()
        assert agent.run_once() == "reconciled"
        assert backend.calls == []

    def test_revoked_agent_keeps_the_operation_pending(self, hub, tmp_path, monkeypatch):
        agent, backend, item = self._lose_then(hub, tmp_path)
        monkeypatch.setenv("UA_REVOKED_AGENTS", OWNER)
        assert agent.run_once() == "auth_error"
        assert _journal(agent)["state"] == "pending"
        assert backend.calls == []
        assert node_queue.get_item(item)["status"] == "claimed"

    def test_other_identity_cannot_reconcile_or_learn_the_operation(self, hub, tmp_path):
        agent, _, _item = self._lose_then(hub, tmp_path)
        op = _journal(agent)
        resp = hub.post(
            "/api/node/claim",
            json={
                "node": NODE,
                "wait_s": 0,
                "claim_op": {"id": op["op_id"], "mode": "reconcile", "issued_at": op["issued_at"]},
            },
            headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
        )
        assert resp.status_code == 403
        assert "item" not in resp.json()

    def test_changed_device_binding_is_not_executable(self, hub):
        _register_direct()
        node_queue.enqueue(
            NODE, prompt="p", cwd="/tmp", backend="codex", created_by="operator-test"
        )
        op = _op()
        bound = node_queue.claim_operation(NODE, op, identity=OWNER)
        assert bound["claim_op"]["executable"] is True
        other = node_queue.claim_operation(
            NODE, {**op, "mode": "reconcile"}, identity=OWNER, device_node="device-key:other"
        )
        assert other["item"] is None
        assert other["claim_op"]["reason"] == "device_changed"

    def test_conflicting_fingerprint_is_refused(self, hub):
        _register_direct()
        op = _op()
        node_queue.claim_operation(NODE, op, identity=OWNER)
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.claim_operation(
                NODE, {**op, "issued_at": op["issued_at"] - 1}, identity=OWNER
            )
        assert err.value.status == 409


# --- controls: ordinary paths unchanged ---------------------------------------------


class TestControls:
    def test_empty_polls_stay_idle_and_close_each_operation(self, hub, tmp_path):
        agent, backend, _ = _agent(_transport(hub), tmp_path)
        agent.run_once()
        assert [agent.run_once() for _ in range(3)] == ["idle"] * 3
        assert _journal(agent) is None
        rows = _op_rows()
        assert len(rows) == 3
        assert {(r["state"], r["reason"]) for r in rows} == {("no_claim", "no_item")}
        assert backend.calls == []

    def test_legacy_claim_without_operation_is_unchanged(self, hub, tmp_path):
        agent, _, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        item = _enqueue(root, "legacy")
        resp = hub.post(
            "/api/node/claim",
            json={"node": NODE, "wait_s": 0},
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        assert resp.status_code == 200
        assert set(resp.json()) == {"item"}
        assert resp.json()["item"]["item_id"] == item
        assert _op_rows() == []

    @pytest.mark.parametrize(
        "claim_op",
        [
            "nope",
            {"id": "x", "mode": "claim", "issued_at": 1_800_000_000},
            {"id": "0" * 32, "mode": "steal", "issued_at": 1_800_000_000},
            {"id": "0" * 32, "mode": "claim", "issued_at": True},
            {"id": "0" * 32, "mode": "claim", "issued_at": 1_800_000_000, "extra": 1},
        ],
    )
    def test_malformed_operation_is_refused_before_any_claim(self, hub, tmp_path, claim_op):
        agent, _, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        item = _enqueue(root, "p")
        resp = hub.post(
            "/api/node/claim",
            json={"node": NODE, "wait_s": 0, "claim_op": claim_op},
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        assert resp.status_code == 400
        assert node_queue.get_item(item)["status"] == "queued"
        assert _op_rows() == []

    def test_hub_without_the_feature_is_not_claimed_from(self, tmp_path):
        log: list = []

        def old_hub(path, body, timeout):
            log.append(path)
            if path == "/api/node/register":
                return 200, {"agent": OWNER, "hub_features": []}
            return 200, {"item": None}

        sleeps: list[float] = []
        agent, _, _ = _agent(old_hub, tmp_path, sleeps=sleeps)
        assert agent.run_once() == "registered"
        assert agent.run_once() == "held"
        assert "/api/node/claim" not in log
        assert agent.registered is False  # re-registers to pick up a hub upgrade
        assert sleeps == [node_agent.CLAIM_HOLD_S]

    @pytest.mark.parametrize("damage", ["corrupt", "other_node", "unknown_state"])
    def test_untrusted_journal_holds_and_is_left_for_the_operator(self, hub, tmp_path, damage):
        log: list = []
        agent, backend, root = _agent(_transport(hub, log=log), tmp_path)
        agent.run_once()
        _enqueue(root, "p")
        path = agent._claim_journal.path
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "version": 1,
            "hub": "",
            "node": NODE,
            "op_id": "a" * 32,
            "issued_at": 1_800_000_000,
            "state": "pending",
        }
        if damage == "corrupt":
            path.write_text("{not json")
        elif damage == "other_node":
            path.write_text(json.dumps({**entry, "node": "another-node"}))
        else:
            path.write_text(json.dumps({**entry, "state": "running"}))
        before = path.read_bytes()
        assert agent.run_once() == "held"
        assert path.read_bytes() == before
        assert not [p for p, _ in log if p == "/api/node/claim"]
        assert backend.calls == []

    def test_journal_publication_failure_sends_nothing(self, hub, tmp_path, monkeypatch):
        log: list = []
        agent, _, root = _agent(_transport(hub, log=log), tmp_path)
        agent.run_once()
        item = _enqueue(root, "p")

        def refuse(self, entry):
            raise node_claim_journal.JournalError("synthetic disk full")

        monkeypatch.setattr(node_claim_journal.ClaimJournal, "_publish", refuse)
        assert agent.run_once() == "held"
        assert not [p for p, _ in log if p == "/api/node/claim"]
        assert node_queue.get_item(item)["status"] == "queued"

    def test_disposition_free_answer_fails_closed(self, tmp_path):
        def odd_hub(path, body, timeout):
            if path == "/api/node/register":
                return 200, {"agent": OWNER, "hub_features": [node_claim_journal.FEATURE]}
            return 200, {"item": {"item_id": "nd-" + "0" * 16, "attempt": 1}}

        agent, backend, _ = _agent(odd_hub, tmp_path)
        agent.run_once()
        assert agent.run_once() == "held"
        assert backend.calls == []
        assert _journal(agent)["state"] == "pending"

    @pytest.mark.parametrize("item", [None, "not-an-item", ["nd-" + "0" * 16]])
    def test_executable_binding_without_a_usable_item_holds(self, tmp_path, item):
        claims: list[dict] = []

        def odd_hub(path, body, timeout):
            if path == "/api/node/register":
                return 200, {"agent": OWNER, "hub_features": [node_claim_journal.FEATURE]}
            if path != "/api/node/claim":
                return 200, {}
            claims.append(body["claim_op"])
            disposition = {
                "id": body["claim_op"]["id"],
                "state": "bound",
                "reason": None,
                "item_id": "nd-" + "0" * 16,
                "attempt": 1,
                "executable": True,
            }
            return 200, {"item": item, "claim_op": disposition}

        agent, backend, _ = _agent(odd_hub, tmp_path)
        agent.run_once()
        assert agent.run_once() == "held"
        entry = _journal(agent)
        assert entry["state"] == "pending"
        # The hold persists: every later iteration reconciles the same
        # operation and never starts a fresh claim or runs anything.
        assert [agent.run_once() for _ in range(2)] == ["held", "held"]
        assert _journal(agent) == entry
        assert backend.calls == []
        assert [c["mode"] for c in claims] == ["claim", "reconcile", "reconcile"]
        assert {c["id"] for c in claims} == {entry["op_id"]}

    def test_local_outcomes_are_pruned_once_receipts_flush(self, hub, tmp_path):
        agent, backend, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        for prompt in ("one", "two", "three"):
            _enqueue(root, prompt)
        assert [agent.run_once() for _ in range(3)] == ["ran"] * 3
        assert len(agent._local_outcomes) <= 1
        assert agent.run_once() == "idle"
        assert agent._local_outcomes == {}
        assert len(backend.calls) == 3

    def test_journal_file_is_private(self, hub, tmp_path, monkeypatch):
        agent, _, root = _agent(_transport(hub), tmp_path)
        agent.run_once()
        _enqueue(root, "p")
        modes: list[int] = []

        def observe(self, entry):
            modes.append(self.path.stat().st_mode & 0o777)
            raise Crash

        monkeypatch.setattr(node_claim_journal.ClaimJournal, "mark_start_intent", observe)
        with pytest.raises(Crash):
            agent.run_once()
        assert modes == [0o600]


# --- signed hosted-device proofs --------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(hosted_tests, "AGENT", "hosted_owner_" + secrets.token_hex(4))
    monkeypatch.setattr(hosted_tests, "STATIC", secrets.token_urlsafe(32))
    monkeypatch.setattr(
        oauth_approver,
        "verify",
        lambda *_args, **_kwargs: oauth_approver.Approval(oauth_approver.VIA_WORKOS, "operator"),
    )
    oauth.reset_provider()
    with hosted_tests._hub_app(monkeypatch, hosted=True) as client:
        yield client
    oauth.reset_provider()


def test_hosted_lost_claim_reconciles_with_fresh_proofs(hosted, tmp_path, monkeypatch):
    out = tmp_path / "sign-in.json"
    hosted_tests._login(hosted, out)
    proofs: list[str] = []
    sent: list[tuple[str, dict, str]] = []
    bridge = hosted_tests._bridge(hosted, proofs)
    dropped = {"n": 0}

    def lossy(req, timeout):
        body = json.loads(req.data)
        proof = dict(req.header_items()).get(device_key.PROOF_HEADER.capitalize(), "")
        sent.append((req.full_url, body, proof))
        response = bridge(req, timeout)
        if (body.get("claim_op") or {}).get("mode") == "claim" and not dropped["n"]:
            dropped["n"] += 1
            raise urllib.error.URLError("synthetic connection reset after the hub committed")
        return response

    monkeypatch.setattr(device_login, "open_no_redirect", lossy)
    transport = node_agent.HubTransport(hosted_tests.HUB, token_path=out)
    agent, backend, root = _agent(transport, tmp_path, node=hosted_tests.NODE)
    assert agent.run_once() == "registered"
    first = _enqueue(root, "first", node=hosted_tests.NODE)
    second = _enqueue(root, "second", node=hosted_tests.NODE)
    assert agent.run_once() == "backoff"
    assert agent.run_once() == "ran"
    assert [c["prompt"] for c in backend.calls] == ["first"]
    assert node_queue.get_item(first)["attempts"] == 1
    assert node_queue.get_item(second)["status"] == "queued"
    claims = [(body, proof) for url, body, proof in sent if url.endswith("/api/node/claim")]
    assert [b["claim_op"]["mode"] for b, _ in claims] == ["claim", "reconcile"]
    assert claims[0][0]["claim_op"]["id"] == claims[1][0]["claim_op"]["id"]
    assert all(p for _, p in claims) and claims[0][1] != claims[1][1]

    # Exact-proof replay of the reconciliation is still refused before the queue.
    body, proof = claims[1]
    saved = json.loads(out.read_text())
    replay = hosted.post(
        "/api/node/claim",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + saved["access_token"],
            "Content-Type": "application/json",
            device_key.PROOF_HEADER: proof,
        },
    )
    assert replay.status_code == 403
    assert "already used" in replay.json()["error"]
    assert node_queue.get_item(second)["status"] == "queued"
