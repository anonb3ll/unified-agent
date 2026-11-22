"""Control Center launches are safe to retry (tasks #2466, #2501).

The real scoped app runs behind the hub auth middleware with the fixture
identities of ``test_route_scopes``: Desktop_Agent2 is a developer (it holds the
dispatch capability), Laptop_Agent5 is chief-of-staff, server_agent8 is public and
Desktop_Operator is the verified human operator. Only the agent start
(``api.start_ui_dispatch``) is stubbed; delegations, notes, approvals, the node
queue and the attempt records are real.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated
from test_webui_cc_work import _approve

from unified_agent import edition, node_queue, profiles, registry, storage
from unified_agent.dispatch import coordinator
from unified_agent.tools import tasks
from unified_agent.webui import api, cc_launch, routes

DEV = "Desktop_Agent2"
COS = "Laptop_Agent5"
NODE = "desk-cc"
DISPATCH = {"agent": COS, "project": "unified-agent", "prompt": "do it"}


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _run(task_id: int, state: str, run_id: str, key: str | None = None) -> str:
    """A ``dispatch_runs`` row, as the coordinator leaves one, bound to ``key`` if given."""
    at = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, COS, state, DEV, at, at),
        )
        if key is not None:
            coordinator._bind_launch_key(conn, key, task_id, run_id, at)
    return run_id


def _launched(task_id: int, run_id: str, key: str | None = None) -> dict:
    """The coordinator's success answer, with the run it leaves behind.

    A stale claim of the task is reclaimed first, as ``claim_and_dispatch`` does,
    and a launch key bound elsewhere refuses the claim, as it does too.
    """
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE dispatch_runs SET state='expired' WHERE task_id=? AND state='claimed'",
            (task_id,),
        )
        refusal = coordinator._launch_key_refusal(conn, key, task_id) if key else None
    if refusal is not None:
        return {**refusal, "spawned": False, "pid": None}
    _run(task_id, "launched", run_id, key)
    return {"launched": True, "reason": "ok", "spawned": True, "pid": 1, "run_id": run_id}


def _unsettled_spawn(task_id: int, run_id: str, key: str) -> None:
    """A keyed run whose launcher died mid-spawn: started, no outcome, lease reclaimed."""
    _run(task_id, "retryable", run_id, key)
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    at = datetime.now(UTC).isoformat()
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE dispatch_launch_keys SET spawn_started_at=? WHERE launch_key=?", (at, key)
        )
        try:  # the launcher's process token (a process that has exited)
            conn.execute(
                "UPDATE dispatch_launch_keys SET spawn_launcher=? WHERE launch_key=?",
                (f"pid:{gone.pid}", key),
            )
        except sqlite3.OperationalError:
            pass


def _spawn_failed(task_id: int, run_id: str, key: str | None = None) -> dict:
    """The coordinator's spawn-failure answer (``claim_and_dispatch``), run left retryable."""
    _run(task_id, "retryable", run_id, key)
    return {
        "launched": False,
        "reason": "launch_failed",
        "error": "binary_not_found:agy",
        "run_id": run_id,
        "spawned": False,
        "pid": None,
    }


@pytest.fixture
def starts(monkeypatch) -> list[int]:
    """Every agent start, by task id; each launches a fresh run."""
    calls: list[int] = []

    def start(*, task_id, actor, launch_key=None, **_kwargs):
        calls.append(task_id)
        return _launched(task_id, f"run-{len(calls)}", launch_key)

    monkeypatch.setattr(api, "start_ui_dispatch", start)
    return calls


def _post(client, path, body, who=DEV, key="launch-1"):
    headers = {**_bearer(who), **({"Idempotency-Key": key} if key else {})}
    return client.post(path, headers=headers, json=body)


def _lookup(client, key="launch-1", who=DEV) -> dict:
    res = client.get(f"/api/launch-attempts/{key}", headers=_bearer(who))
    assert res.status_code == 200, res.text
    return res.json()


def _count(table: str) -> int:
    with closing(storage.connect()) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _register_node(tmp_path) -> None:
    node_queue.register(
        NODE,
        identity=DEV,
        facts={
            "backends": ["codex"],
            "harnesses": {},
            "worktree_roots": [{"path": str(tmp_path), "worktrees": 0}],
        },
        version="test",
    )


# --- dispatch -----------------------------------------------------------------------


def test_dispatch_same_key_starts_once_and_replays_the_first_answer(client, starts):
    first = _post(client, "/api/action/dispatch", DISPATCH)
    assert first.status_code == 202, first.text
    again = _post(client, "/api/action/dispatch", DISPATCH)
    assert (again.status_code, again.json()) == (202, first.json())
    assert len(starts) == 1 and _count("delegations") == 1
    attempt = _lookup(client)
    assert attempt["state"] == "started" and attempt["kind"] == "dispatch"
    assert attempt["run_id"] == first.json()["run_id"] == "run-1"
    assert attempt["task_id"] == first.json()["task_id"]
    assert attempt["delegation_id"] == first.json()["delegation_id"]
    # The same key with another body is a conflict and starts nothing.
    other = _post(client, "/api/action/dispatch", {**DISPATCH, "prompt": "other"})
    assert other.status_code == 409 and other.json()["code"] == "conflict"
    # Another identity's key is its own: unknown to the first, a separate launch.
    assert _lookup(client, who=OPERATOR) == {"key": "launch-1", "state": "unknown"}
    assert _post(client, "/api/action/dispatch", {**DISPATCH, "agent": DEV}, who=COS).json()[
        "run_id"
    ] == ("run-2")
    # The lookup takes the dispatch capability; a public identity is refused.
    denied = client.get("/api/launch-attempts/launch-1", headers=_bearer("server_agent8"))
    assert denied.status_code == 403
    assert _post(client, "/api/action/dispatch", DISPATCH, who="server_agent8").status_code == 403


def test_dispatch_without_a_key_behaves_as_before(client, starts):
    for _ in range(2):
        res = _post(client, "/api/action/dispatch", DISPATCH, key=None)
        assert res.status_code == 202 and res.json()["status"] == "running"
    assert len(starts) == 2 and _count("delegations") == 2
    with closing(storage.connect()) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys WHERE tool=?", (cc_launch.TOOL,)
        ).fetchone()
    assert rows[0] == 0


@pytest.mark.parametrize("key", ["", " x", "a" * 129, "bad/key", "-lead"])
def test_a_malformed_key_is_refused_before_anything_runs(client, starts, key):
    res = client.post(
        "/api/action/dispatch", headers={**_bearer(DEV), "Idempotency-Key": key}, json=DISPATCH
    )
    assert res.status_code == 400 and "Idempotency-Key" in res.json()["error"]
    assert starts == [] and _count("delegations") == 0


def test_concurrent_requests_with_one_key_launch_once(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def slow_start(*, task_id, actor, launch_key=None, **_kwargs):
        calls.append(task_id)
        entered.set()
        assert release.wait(30)
        return _launched(task_id, "run-slow", launch_key)

    monkeypatch.setattr(api, "start_ui_dispatch", slow_start)
    results: dict[str, object] = {}
    first = threading.Thread(
        target=lambda: results.setdefault("a", _post(client, "/api/action/dispatch", DISPATCH))
    )
    first.start()
    assert entered.wait(30)
    second = _post(client, "/api/action/dispatch", DISPATCH)
    assert second.status_code == 409 and second.json()["code"] == "launch_in_progress"
    assert _lookup(client)["state"] == "in_progress"
    release.set()
    first.join(30)
    assert results["a"].status_code == 202
    assert calls == [results["a"].json()["task_id"]] and _count("delegations") == 1
    replay = _post(client, "/api/action/dispatch", DISPATCH)
    assert replay.json() == results["a"].json()


@pytest.mark.parametrize("heartbeat", [True, False], ids=["heartbeat", "starved-heartbeat"])
def test_a_slow_launch_keeps_its_lease_and_its_own_answer(client, monkeypatch, heartbeat):
    """Review #1521 item 1: the lease lapsing mid-launch must not hand the attempt over.

    The heartbeat keeps the lease current in the database (another hub worker sees
    it held); a starved heartbeat still never hands over a request its own worker
    is running.
    """
    monkeypatch.setattr(cc_launch, "LEASE_S", 0.3)
    if not heartbeat:
        monkeypatch.setattr(cc_launch, "_heartbeat", lambda attempt, stop: None)
    entered, release = threading.Event(), threading.Event()

    def slow_start(*, task_id, actor, launch_key=None, **_kwargs):
        entered.set()
        assert release.wait(30)
        return _launched(task_id, "run-slow", launch_key)

    monkeypatch.setattr(api, "start_ui_dispatch", slow_start)
    results: dict[str, object] = {}
    first = threading.Thread(
        target=lambda: results.setdefault("a", _post(client, "/api/action/dispatch", DISPATCH))
    )
    first.start()
    assert entered.wait(30)

    def lease_until() -> datetime:
        with closing(storage.connect()) as conn:
            return datetime.fromisoformat(cc_launch._read(conn, DEV, "launch-1")["lease_until"])

    before = lease_until()
    time.sleep(1.0)  # more than three leases: only a heartbeat keeps the hold
    # The heartbeat moved the lease in the database (load may delay a beat, so the
    # proof is the renewal, not the lease being current at one sampled instant).
    renewed = lease_until() - before >= timedelta(seconds=0.5)
    assert renewed is heartbeat
    second = _post(client, "/api/action/dispatch", DISPATCH)
    assert second.status_code == 409 and second.json()["code"] == "launch_in_progress"
    release.set()
    first.join(30)
    assert results["a"].status_code == 202, results["a"].text
    assert results["a"].json()["run_id"] == "run-slow"
    assert _lookup(client)["state"] == "started"


def test_a_repeat_after_the_approval_continues_the_same_delegation(client, starts, monkeypatch):
    """The approval gate parks the note after the delegation exists; the repeat resumes it."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_update")
    parked = _post(client, "/api/action/dispatch", DISPATCH)
    assert parked.status_code == 202 and parked.json()["code"] == "approval_required"
    attempt = _lookup(client)
    assert attempt["state"] == "parked" and attempt["approval_id"] == parked.json()["approval_id"]
    assert attempt["delegation_id"] and _count("delegations") == 1
    # The gate still runs with a key: a repeat before the decision parks again.
    still = _post(client, "/api/action/dispatch", DISPATCH)
    assert still.status_code == 202 and still.json()["approval_id"] == attempt["approval_id"]
    assert starts == []
    _approve(attempt["approval_id"])
    ran = _post(client, "/api/action/dispatch", DISPATCH)
    assert ran.status_code == 202 and ran.json()["status"] == "running", ran.text
    assert ran.json()["delegation_id"] == attempt["delegation_id"]
    assert _count("delegations") == 1 and starts == [attempt["task_id"]]
    assert _lookup(client)["state"] == "started"


# --- dispatch stack -----------------------------------------------------------------


def test_a_stack_repeat_starts_the_task_the_attempt_chose(client, monkeypatch):
    first, second = (tasks.task_add(title)["id"] for title in ("first", "second"))
    previews = iter([first, second, second])

    async def preview(_request):
        return {"task_ids": [first, second], "next_task_id": next(previews)}

    launched: list[int] = []

    def start(*, task_id, launch_key=None, **_kwargs):
        launched.append(task_id)
        if len(launched) == 1:  # the coordinator's real spawn failure: a run id, not launched
            return _spawn_failed(task_id, "run-f", launch_key)
        return _launched(task_id, "run-s", launch_key)

    monkeypatch.setattr(api, "dispatch_stack_payload", preview)
    monkeypatch.setattr(api, "start_ui_dispatch", start)
    body = {"task_ids": [first, second], "project": "unified-agent"}
    failed = _post(client, "/api/action/dispatch-stack", body)
    assert failed.status_code == 202 and _lookup(client)["state"] == "failed"
    assert _lookup(client)["reason"] == "binary_not_found:agy"
    ran = _post(client, "/api/action/dispatch-stack", body)
    assert ran.json()["dispatch"]["run_id"] == "run-s"
    assert launched == [first, first]  # not the second, which the preview now names
    # Pass 3 item 4: the answer names the task it started, not the moved preview.
    assert ran.json()["next_task_id"] == first
    assert _post(client, "/api/action/dispatch-stack", body).json() == ran.json()
    assert launched == [first, first]


class _NoLiveOwners(set):
    """The hub worker of a second request: it never sees the first one running."""

    def add(self, _token) -> None:
        return None


def test_a_request_that_lost_its_hold_before_selecting_launches_nothing(
    client, starts, monkeypatch
):
    """Pass 3 item 1: a stack task is chosen durably, or nothing launches.

    The first request stalls between its preview and recording the task it chose;
    its lease lapses (no heartbeat, another worker), and a second request takes the
    attempt over and launches the task its own preview names. The first then
    finds it lost the hold: it launches nothing and answers 409.
    """
    monkeypatch.setattr(cc_launch, "LEASE_S", 0.3)
    monkeypatch.setattr(cc_launch, "_heartbeat", lambda attempt, stop: None)
    monkeypatch.setattr(cc_launch, "_LIVE_OWNERS", _NoLiveOwners())
    first, second = (tasks.task_add(title)["id"] for title in ("first", "second"))
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    async def preview(_request):
        calls.append(len(calls))
        if len(calls) == 1:
            entered.set()
            await anyio.to_thread.run_sync(lambda: release.wait(30))
            return {"task_ids": [first, second], "next_task_id": first}
        return {"task_ids": [first, second], "next_task_id": second}

    monkeypatch.setattr(api, "dispatch_stack_payload", preview)
    body = {"task_ids": [first, second], "project": "unified-agent"}
    results: dict[str, object] = {}
    worker_1 = threading.Thread(
        target=lambda: results.setdefault("a", _post(client, "/api/action/dispatch-stack", body))
    )
    worker_1.start()
    assert entered.wait(30)
    time.sleep(0.5)  # the first request's lease lapses
    taken = _post(client, "/api/action/dispatch-stack", body)
    assert taken.status_code == 202, taken.text
    assert taken.json()["dispatch"]["run_id"] == "run-1"
    release.set()
    worker_1.join(30)
    stale = results["a"]
    assert stale.status_code == 409 and stale.json()["code"] == "launch_in_progress", stale.text
    assert starts == [second]  # one job, the one the holder chose
    attempt = _lookup(client)
    assert (attempt["state"], attempt["task_id"], attempt["run_id"]) == ("started", second, "run-1")


def test_a_lost_task_receipt_is_recorded_by_the_next_write(client, starts, monkeypatch):
    """Pass 3 item 2: a later save or a heartbeat never drops an unwritten receipt."""
    real_write = cc_launch._write
    failed: list[str] = []

    def flaky_write(conn, identity, key, record):
        steps = record.get("steps") or {}
        if record.get("state") == "in_progress" and steps.get("task_id") and not failed:
            failed.append(key)
            raise sqlite3.OperationalError("database is locked")
        real_write(conn, identity, key, record)

    monkeypatch.setattr(cc_launch, "_write", flaky_write)
    res = _post(client, "/api/action/dispatch", DISPATCH)
    assert len(failed) == 1 and res.status_code == 202, res.text
    attempt = _lookup(client)
    assert attempt["state"] == "started", attempt
    assert (attempt["task_id"], attempt["run_id"]) == (res.json()["task_id"], "run-1")
    # The heartbeat alone records a pending receipt too.
    failed.clear()
    held = cc_launch.begin(DEV, "beat", "dispatch", "d")
    assert held.save(task_id=41) is False and failed
    assert held.renew() is True
    assert cc_launch.lookup(DEV, "beat")["task_id"] == 41
    assert held.save(noted=True) and cc_launch.lookup(DEV, "beat")["task_id"] == 41


# --- ua-node start and restart ------------------------------------------------------


def test_device_start_parks_then_queues_one_item_and_restart_is_keyed(client, tmp_path):
    _register_node(tmp_path)
    body = {
        "action": "start",
        "node": NODE,
        "prompt": "p",
        "cwd": str(tmp_path),
        "backend": "codex",
    }
    path = "/api/action/node-session"
    parked = _post(client, path, body, key="dev-1")
    assert parked.status_code == 202 and parked.json()["code"] == "approval_required"
    assert _lookup(client, "dev-1")["state"] == "parked"
    _approve(parked.json()["approval_id"])
    queued = _post(client, path, body, key="dev-1")
    assert queued.status_code == 201, queued.text
    assert _post(client, path, body, key="dev-1").json() == queued.json()
    assert _count("node_dispatch") == 1
    item_id = queued.json()["item_id"]
    attempt = _lookup(client, "dev-1")
    assert attempt["state"] == "started" and attempt["item_id"] == item_id
    assert attempt["session_id"] is None
    # The session the node reports is routed by its hub registry row id.
    row_id = registry.start_session(
        DEV, "acp", external_session_id="ext-9", meta={"item_id": item_id}
    )
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE node_dispatch SET session_id='ext-9', status='finished' WHERE id=?",
            (item_id,),
        )
    assert _lookup(client, "dev-1")["session_id"] == row_id
    listed = client.get("/api/nodes", headers=_bearer(OPERATOR)).json()["recent_items"]
    assert [i["agent_session_id"] for i in listed if i["id"] == item_id] == [row_id]
    restart = {"action": "restart", "item_id": item_id}
    parked = _post(client, path, restart, key="dev-2")
    _approve(parked.json()["approval_id"])
    again = _post(client, path, restart, key="dev-2")
    assert again.status_code == 201 and again.json()["item_id"] != item_id
    assert _post(client, path, restart, key="dev-2").json() == again.json()
    assert _count("node_dispatch") == 2


# --- crash safety -------------------------------------------------------------------


def _abandon(identity: str, key: str, kind: str, request: dict) -> cc_launch.Attempt:
    """An attempt whose request died: held, then its lease ran out (a hub restart)."""
    held = cc_launch.begin(identity, key, kind, cc_launch.request_digest(kind, request))
    assert isinstance(held, cc_launch.Attempt)
    _expire(identity, key)
    return held


def _expire(identity: str, key: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with closing(storage.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        record = cc_launch._read(conn, identity, key)
        record["lease_until"] = past
        cc_launch._write(conn, identity, key, record)
        conn.commit()


def test_a_launch_interrupted_by_a_restart_is_reconciled_not_repeated(client, tmp_path, starts):
    _register_node(tmp_path)
    fields = {"prompt": "p", "cwd": str(tmp_path), "backend": "codex"}
    # The item was queued before the restart, but the attempt never heard back.
    held = _abandon(DEV, "crash-1", "device", {"node": NODE, **fields})
    item = node_queue.enqueue(NODE, created_by=DEV, launch_key=held.op_key, **fields)
    attempt = _lookup(client, "crash-1")
    assert attempt["state"] == "started" and attempt["item_id"] == item["item_id"]
    # Review #1521 item 2: the reconciled start replays a real answer to a repeat.
    body = {"action": "start", "node": NODE, **fields}
    replay = _post(client, "/api/action/node-session", body, key="crash-1")
    assert replay.status_code == 201, replay.text
    assert replay.json()["item_id"] == item["item_id"] and _count("node_dispatch") == 1
    # Nothing was created before the restart: failed, and the same key continues it.
    _abandon(DEV, "crash-2", "dispatch", cc_launch_request())
    attempt = _lookup(client, "crash-2")
    assert attempt["state"] == "failed" and attempt["reason"] == cc_launch.INTERRUPTED_REASON
    ran = _post(client, "/api/action/dispatch", DISPATCH, key="crash-2")
    assert ran.status_code == 202 and _count("delegations") == 1 and len(starts) == 1
    # The task a repeat would continue has gone: a typed 409, not a 400.
    gone = _abandon(DEV, "crash-4", "dispatch", cc_launch_request())
    gone.save(task_id=99999)
    _expire(DEV, "crash-4")
    missing = _post(client, "/api/action/dispatch", DISPATCH, key="crash-4")
    assert missing.status_code == 409 and missing.json()["code"] == "conflict"
    # A live hold is never taken over: the lease protects a slow first request.
    cc_launch.begin(DEV, "crash-3", "dispatch", cc_launch.request_digest("dispatch", {}))
    with pytest.raises(cc_launch.LaunchBusy):
        cc_launch.begin(DEV, "crash-3", "dispatch", cc_launch.request_digest("dispatch", {}))


def cc_launch_request() -> dict:
    import dataclasses

    from unified_agent.webui.contracts import parse_dispatch_request

    return dataclasses.asdict(parse_dispatch_request(DISPATCH))


#: The state table of ``cc_launch`` (docstring, and "Launches" in the API doc), one row
#: per (record state, lease, evidence): what GET answers and what a repeat POST does.
#: ``get`` False: the POST comes first, so the POST itself must reconcile (item 5).
STATE_TABLE = [
    # row, kind, record state, lease, evidence, get, GET state, POST outcome
    ("live-lease", "dispatch", "in_progress", "live", None, True, "in_progress", "busy"),
    ("lapsed-nothing", "dispatch", "in_progress", "lapsed", None, True, "failed", "continues"),
    (
        "lapsed-claimed-run",
        "dispatch",
        "in_progress",
        "lapsed",
        "claimed",
        True,
        "failed",
        "continues",
    ),
    (
        "lapsed-spawn-failed",
        "dispatch",
        "in_progress",
        "lapsed",
        "retryable",
        True,
        "failed",
        "continues",
    ),
    (
        "lapsed-launched-run",
        "dispatch",
        "in_progress",
        "lapsed",
        "launched",
        True,
        "started",
        "replays",
    ),
    (
        "lapsed-launched-run-post-first",
        "dispatch",
        "in_progress",
        "lapsed",
        "launched",
        False,
        "started",
        "replays",
    ),
    # Pass 3 item 3: only the run bound to this attempt's key proves its launch.
    (
        "lapsed-unbound-run",
        "dispatch",
        "in_progress",
        "lapsed",
        "unbound",
        True,
        "failed",
        "continues",
    ),
    ("lapsed-queue-item", "device", "in_progress", "lapsed", "item", True, "started", "replays"),
    ("lapsed-no-item", "device", "in_progress", "lapsed", None, True, "failed", "continues"),
    ("started-task-gone", "dispatch", "started", None, "task-gone", True, "started", "conflict"),
    ("parked", "dispatch", "parked", None, None, True, "parked", "continues"),
    ("failed", "dispatch", "failed", None, None, True, "failed", "continues"),
    # Task #2509: a failed record whose own run launched lost a receipt, not a launch.
    ("failed-launched-run", "dispatch", "failed", None, "launched", True, "started", "replays"),
    (
        "failed-launched-run-post-first",
        "dispatch",
        "failed",
        None,
        "launched",
        False,
        "started",
        "replays",
    ),
    ("failed-spawn-failed", "dispatch", "failed", None, "retryable", True, "failed", "continues"),
    (
        "failed-run-of-another-attempt",
        "dispatch",
        "failed",
        None,
        "launched-elsewhere",
        True,
        "failed",
        "continues",
    ),
    ("failed-queue-item", "device", "failed", None, "item", True, "started", "replays"),
    ("failed-unbound-run", "dispatch", "failed", None, "unbound", True, "failed", "continues"),
    # Review #1521 pass 5: a spawn that started without a recorded outcome may still
    # be running: never failed, never repeated, until the operator settles it.
    (
        "lapsed-spawn-unsettled",
        "dispatch",
        "in_progress",
        "lapsed",
        "unsettled",
        True,
        "unsettled",
        "unsettled",
    ),
    (
        "failed-spawn-unsettled",
        "dispatch",
        "failed",
        None,
        "unsettled",
        True,
        "unsettled",
        "unsettled",
    ),
]


def _edit(identity: str, key: str, **fields) -> None:
    with closing(storage.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        record = cc_launch._read(conn, identity, key)
        record.update(fields)
        cc_launch._write(conn, identity, key, record)
        conn.commit()


@pytest.mark.parametrize(
    ("kind", "state", "lease", "evidence", "get", "get_state", "post"),
    [row[1:] for row in STATE_TABLE],
    ids=[row[0] for row in STATE_TABLE],
)
def test_reconciliation_follows_the_state_table(
    client, starts, tmp_path, kind, state, lease, evidence, get, get_state, post
):
    """Review #1521 items 2-6: what proves a launch, for GET and a repeat POST alike."""
    fields = {"prompt": "p", "cwd": str(tmp_path), "backend": "codex"}
    if kind == "device":
        _register_node(tmp_path)
        path, body = "/api/action/node-session", {"action": "start", "node": NODE, **fields}
        request = {"node": NODE, **fields}
    else:
        path, body, request = "/api/action/dispatch", DISPATCH, cc_launch_request()
    held = cc_launch.begin(DEV, "row", kind, cc_launch.request_digest(kind, request))
    task_id = None
    if kind == "dispatch":
        task_id = tasks.task_add("delegated")["id"]
        held.save(task_id=task_id)
    if evidence in ("claimed", "retryable", "launched"):
        _run(task_id, evidence, "run-x", held.op_key)
    elif evidence == "launched-elsewhere":  # another attempt's run of the same task
        _run(task_id, "finished", "run-x", cc_launch.operation_key(DEV, "other"))
    elif evidence == "unbound":  # an unkeyed launch of the same task, newer than the attempt
        _run(task_id, "finished", "run-x")
    elif evidence == "unsettled":
        _unsettled_spawn(task_id, "run-x", held.op_key)
    elif evidence == "item":
        node_queue.enqueue(NODE, created_by=DEV, launch_key=held.op_key, **fields)
    if state == "started":
        _run(task_id, "launched", "run-x", held.op_key)
        _edit(
            DEV,
            "row",
            state="started",
            owner=None,
            ids={"task_id": task_id, "run_id": "run-x"},
            response={"status_code": 202, "body": {"status": "running", "run_id": "run-x"}},
        )
    elif state in ("parked", "failed"):
        _edit(DEV, "row", state=state, owner=None, reason="x")
    if lease == "lapsed":
        _expire(DEV, "row")
    if evidence == "task-gone":
        with closing(storage.connect()) as conn, conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    if get:
        assert _lookup(client, "row")["state"] == get_state
    res = _post(client, path, body, key="row")
    if post == "unsettled":
        assert res.status_code == 409 and res.json()["code"] == "launch_unsettled", res.text
        assert res.json()["run_id"] == "run-x" and starts == []
    elif post == "busy":
        assert res.status_code == 409 and res.json()["code"] == "launch_in_progress"
    elif post == "conflict":
        assert res.status_code == 409 and res.json()["code"] == "conflict", res.text
    elif post == "replays":
        assert starts == [] and res.status_code in (201, 202), res.text
        if kind == "device":
            assert res.json()["item_id"] and _count("node_dispatch") == 1
        else:
            assert res.json()["run_id"] == "run-x" and res.json()["status"] == "running"
    elif kind == "device":  # continues: on to the approval gate, which still runs
        assert res.status_code == 202 and res.json()["code"] == "approval_required"
        assert _count("node_dispatch") == 0
    else:  # continues: the same task is started, no second delegation
        assert res.status_code == 202, res.text
        assert starts == [task_id] and _count("delegations") == 0
    if not get:
        assert _lookup(client, "row")["state"] == get_state


@pytest.mark.parametrize("final_receipt_fails", [False, True], ids=["run-id", "run-id-and-final"])
def test_a_lost_run_receipt_never_downgrades_a_launch(
    client, starts, monkeypatch, final_receipt_fails
):
    """Task #2509: the run committed, then writing its id failed.

    The answer, the lookup and a repeat with the same key all say ``started`` with
    the launch's ids, and the repeat starts nothing more.
    """
    real_write = cc_launch._write
    failures: list[str] = []

    def flaky_write(conn, identity, key, record):
        run_receipt = record.get("state") == "in_progress" and record["steps"].get("run_id")
        final = record.get("state") == "started" and final_receipt_fails and not failures[1:]
        if (run_receipt and not failures) or (final and failures):
            failures.append(record.get("state"))
            raise sqlite3.OperationalError("database is locked")
        real_write(conn, identity, key, record)

    monkeypatch.setattr(cc_launch, "_write", flaky_write)
    res = _post(client, "/api/action/dispatch", DISPATCH)
    assert failures == (["in_progress", "started"] if final_receipt_fails else ["in_progress"])
    assert res.status_code == 202, res.text
    answer = res.json()
    assert answer["status"] == "running" and answer["run_id"] == "run-1"
    if final_receipt_fails:  # nothing final was written: the lease lapses, evidence settles it
        _expire(DEV, "launch-1")
    attempt = _lookup(client)
    assert attempt["state"] == "started", attempt
    assert (attempt["task_id"], attempt["delegation_id"], attempt["run_id"]) == (
        answer["task_id"],
        answer["delegation_id"],
        "run-1",
    )
    again = _post(client, "/api/action/dispatch", DISPATCH)
    assert again.status_code == 202, again.text
    assert again.json()["run_id"] == "run-1" and again.json()["task_id"] == answer["task_id"]
    assert starts == [answer["task_id"]] and _count("delegations") == 1
    assert _count("dispatch_runs") == 1


# --- the store ----------------------------------------------------------------------


def test_records_are_tenant_scoped_and_an_unscoped_legacy_record_is_refused(monkeypatch):
    held = cc_launch.begin(DEV, "k1", "device", "d")
    cc_launch.finish(held, routes._json({"item_id": "nd-1"}, status=201))
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    # The record was written before tenant scoping: neither replayed nor repeated.
    with pytest.raises(storage.AmbiguousIdempotencyReceipt):
        cc_launch.begin(DEV, "k1", "device", "d")
    tenant_a = cc_launch.begin(DEV, "k2", "device", "d")
    tenant_a_key = tenant_a.op_key
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    assert cc_launch.lookup(DEV, "k2") == {"key": "k2", "state": "unknown"}
    assert cc_launch.operation_key(DEV, "k2", tenant_a.record["generation"]) != tenant_a_key


def test_finish_by_a_lost_holder_changes_nothing_and_old_terminal_records_are_pruned(
    monkeypatch,
):
    stale = cc_launch.begin(DEV, "p1", "device", "d")
    stale.token = "someone-else"
    cc_launch.finish(stale, routes._json({"item_id": "nd-1"}, status=201))
    assert cc_launch.lookup(DEV, "p1")["state"] == "in_progress"
    assert stale.save(item_id="nd-1") is False  # a lost hold records nothing
    for key in ("p2", "p3", "p4"):
        cc_launch.finish(cc_launch.begin(DEV, key, "device", "d"), routes._json({}, status=400))
    monkeypatch.setattr(cc_launch, "MAX_RECORDS", 2)
    cc_launch.begin(DEV, "p5", "device", "d")  # a new record prunes the oldest terminal ones
    states = {k: cc_launch.lookup(DEV, k)["state"] for k in ("p1", "p2", "p3", "p4", "p5")}
    assert states == {
        "p1": "in_progress",
        "p2": "unknown",
        "p3": "unknown",
        "p4": "failed",
        "p5": "in_progress",
    }
    monkeypatch.setattr(cc_launch, "RETENTION_S", -1)
    cc_launch.begin(DEV, "p6", "device", "d")
    assert cc_launch.lookup(DEV, "p4")["state"] == "unknown"
    assert cc_launch.lookup(DEV, "p1")["state"] == "in_progress"  # never pruned in flight


def test_a_pruned_key_starts_a_new_launch_and_adopts_nothing(client, starts, monkeypatch):
    """Review #1521 pass 4 item 2: nothing bound to a pruned attempt is adopted.

    The same client key, reused after its record was pruned, is a new launch: it
    starts the task it asks for and answers with it, never the pruned launch's run.
    """
    first, second = (tasks.task_add(title)["id"] for title in ("first", "second"))

    async def preview(request):
        return {"task_ids": list(request.task_ids), "next_task_id": request.task_ids[0]}

    monkeypatch.setattr(api, "dispatch_stack_payload", preview)
    path = "/api/action/dispatch-stack"
    old = _post(client, path, {"task_ids": [first], "project": "unified-agent"})
    assert old.status_code == 202 and old.json()["dispatch"]["run_id"] == "run-1"
    dispatched = _post(client, "/api/action/dispatch", DISPATCH, key="d-1")
    assert dispatched.status_code == 202 and _count("delegations") == 1
    monkeypatch.setattr(cc_launch, "RETENTION_S", -1)
    cc_launch.begin(DEV, "prune-now", "device", "d")  # a new record prunes the old ones
    assert _lookup(client)["state"] == "unknown"
    new = _post(client, path, {"task_ids": [second], "project": "unified-agent"})
    assert new.status_code == 202, new.text
    assert new.json()["next_task_id"] == second and new.json()["dispatch"]["run_id"] == "run-3"
    assert starts == [first, dispatched.json()["task_id"], second]
    attempt = _lookup(client)
    assert (attempt["state"], attempt["task_id"], attempt["run_id"]) == ("started", second, "run-3")
    # A dispatch is a new delegation too, not the pruned launch's.
    again = _post(client, "/api/action/dispatch", {**DISPATCH, "prompt": "other"}, key="d-1")
    assert again.status_code == 202, again.text
    assert again.json()["task_id"] != dispatched.json()["task_id"] and _count("delegations") == 2


def test_an_unsettled_launch_is_never_pruned_and_the_operator_settles_it(
    client, starts, monkeypatch
):
    """Review #1521 pass 5: an unsettled spawn holds its attempt until it is settled.

    A lapsed attempt whose spawn started without a recorded outcome reads
    ``unsettled`` with its run, survives capacity pruning (so its key never gets a
    new generation), and refuses a repeat. The operator's settle route records the
    spawn as failed once its launcher is gone; then the same key continues.
    """
    request = cc_launch_request()
    held = cc_launch.begin(DEV, "u-1", "dispatch", cc_launch.request_digest("dispatch", request))
    task_id = tasks.task_add("delegated")["id"]
    held.save(task_id=task_id)
    _unsettled_spawn(task_id, "run-x", held.op_key)
    _expire(DEV, "u-1")
    attempt = _lookup(client, "u-1")
    assert (attempt["state"], attempt["run_id"]) == ("unsettled", "run-x"), attempt
    assert "repeat it" not in attempt["reason"]
    monkeypatch.setattr(cc_launch, "MAX_RECORDS", 0)
    monkeypatch.setattr(cc_launch, "RETENTION_S", -1)
    cc_launch.begin(DEV, "prune-now", "device", "d")  # prunes every terminal record
    assert _lookup(client, "u-1")["state"] == "unsettled"
    again = _post(client, "/api/action/dispatch", DISPATCH, key="u-1")
    assert again.status_code == 409 and again.json()["code"] == "launch_unsettled", again.text
    assert starts == []
    path = "/api/dispatch-runs/run-x/settle-spawn"
    assert client.post(path, headers=_bearer(DEV)).status_code == 403
    settled = client.post(path, headers=_bearer(OPERATOR))
    assert settled.status_code == 200 and settled.json()["settled"] is True, settled.text
    assert client.post(path, headers=_bearer(OPERATOR)).json()["reason"] == "not_unsettled"
    attempt = _lookup(client, "u-1")
    assert (attempt["state"], attempt["reason"]) == ("failed", cc_launch.SETTLED_REASON)
    ran = _post(client, "/api/action/dispatch", DISPATCH, key="u-1")
    assert ran.status_code == 202 and starts == [task_id], ran.text


# --- session detail -----------------------------------------------------------------


def test_session_detail_carries_the_wake_state_the_list_adds(client):
    session_id = registry.start_session(DEV, "claude_code", external_session_id="s-1")
    grant = client.post(
        f"/api/sessions/{session_id}/wake-grant", headers=_bearer(OPERATOR), json={"grant": True}
    )
    assert grant.status_code == 200, grant.text
    detail = client.get(f"/api/sessions/{session_id}", headers=_bearer(OPERATOR)).json()
    listed = client.get("/api/sessions", headers=_bearer(OPERATOR)).json()["sessions"]
    [row] = [s for s in listed if s["id"] == session_id]
    assert detail["wake"] is not None and detail["wake"] == row["wake"]
    assert detail["platform"] == row["platform"]
