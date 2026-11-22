"""One job per Control Center launch, enforced by the database (task #2501).

A keyed launch binds its operation key to the run ``claim_and_dispatch`` mints,
in the claim's own transaction (``dispatch_launch_keys``). Hub workers are
separate processes, so the proof here is separate processes: two of them claim
two different ready tasks under one key at the same moment, and exactly one job
starts. Only the process launch boundary is a test double.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from unified_agent import storage
from unified_agent.dispatch import coordinator, supervise
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import delegate, tasks

SRC = Path(__file__).resolve().parent.parent / "src"
KEY = "cc-launch-op:test"

#: One hub worker: wait for the shared start time, claim, report.
WORKER = """
import json, sys, time
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RecordingLaunchBoundary

task_id, key, start_at = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
while time.time() < start_at:
    time.sleep(0.002)
launch = RecordingLaunchBoundary()
result = coordinator.claim_and_dispatch(
    task_id=task_id, actor="Desktop_Agent2", launch=launch, launch_key=key
)
print(json.dumps({"result": result, "spawns": len(launch.calls)}))
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    return tmp_path


def _ready_task() -> int:
    spec = (
        "### Objective\nlaunch key fixture\n### Scope\n- src/unified_agent/dispatch/\n"
        "### Definition of Done\n- one job per launch\n- Gates: pytest\n"
        "### TDD Constraints\n- Stub: process launch boundary only\n"
        "### Boundaries\n- Must not edit Red-tier config\n"
    )
    result = delegate.delegate(
        task_spec=spec, from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(result["task_id"], append_note="[plan: approved @ 2026-08-23T00:00:00Z]")
    return int(result["task_id"])


def _workers(task_ids: list[int]) -> list[dict]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]),
    }
    start_at = f"{time.time() + 2.0:.3f}"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(task_id), KEY, start_at],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for task_id in task_ids
    ]
    out = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr[-2000:]
        out.append(json.loads(stdout.strip().splitlines()[-1]))
    return out


def test_two_workers_launching_one_key_start_exactly_one_job(db):
    first, second = _ready_task(), _ready_task()
    reports = _workers([first, second])
    assert sum(r["spawns"] for r in reports) == 1, reports
    [won] = [r["result"] for r in reports if r["result"]["launched"]]
    [lost] = [r["result"] for r in reports if not r["result"]["launched"]]
    assert lost["reason"] == "launch_key_bound" and lost["run_id"] == won["run_id"]
    with closing(storage.connect()) as conn:
        runs = conn.execute("SELECT run_id, state FROM dispatch_runs").fetchall()
        bound = coordinator.launch_key_run(conn, KEY)
    assert [(r["run_id"], r["state"]) for r in runs] == [(won["run_id"], "launched")]
    assert bound["run_id"] == won["run_id"] and bound["launched"] is True
    # The loser's task is untouched: its delegation is pending again, not in flight.
    loser = second if bound["task_id"] == first else first
    with closing(storage.connect()) as conn:
        status = conn.execute("SELECT status FROM delegations WHERE task_id=?", (loser,)).fetchone()
    assert status["status"] == "pending"


class _StalledSpawn:
    """A launch boundary whose first spawn stalls until released (a slow spawner)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered, self.release = threading.Event(), threading.Event()

    def launch(self, *, run_id, task_id, agent_label, delegation_id):
        self.calls.append(run_id)
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(30)
        return {"ok": True, "stub": True, "run_id": run_id}


def test_a_stalled_spawn_holds_its_key_after_its_lease_is_reclaimed(db):
    """Review #1521 pass 4 item 1: a slow spawn never lets a second launcher spawn.

    Launcher A stalls inside its spawn while its lease runs out; launcher B
    reclaims the run and tries the same key. B spawns nothing; when A's spawn
    returns, its outcome is the key's launch.
    """
    task = _ready_task()
    boundary = _StalledSpawn()
    results: dict[str, dict] = {}
    launcher_a = threading.Thread(
        target=lambda: results.setdefault(
            "a",
            coordinator.claim_and_dispatch(
                task_id=task,
                actor="Desktop_Agent2",
                launch=boundary,
                launch_key=KEY,
                lease_ttl_s=2.0,
            ),
        )
    )
    launcher_a.start()
    assert boundary.entered.wait(30)
    time.sleep(2.5)  # A's lease runs out while it is still spawning
    b = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert b["launched"] is False and b["reason"] == "launch_unsettled", b
    assert len(boundary.calls) == 1
    boundary.release.set()
    launcher_a.join(30)
    # Reclaim kept A's run claimed while its spawn was unsettled (#2512): A launches it.
    assert results["a"]["launched"] is True
    with closing(storage.connect()) as conn:
        bound = coordinator.launch_key_run(conn, KEY)
    assert (bound["run_id"], bound["launched"]) == (results["a"]["run_id"], True)
    again = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert again["reason"] == "already_claimed" and len(boundary.calls) == 1
    # A launcher that no longer holds the claim cannot mark a spawn started.
    assert coordinator._fence_spawn(KEY, results["a"]["run_id"], "Desktop_Agent2") is False


class _CrashingSpawn:
    """A launch boundary that dies inside the spawn: no outcome is ever recorded."""

    def launch(self, *, run_id, task_id, agent_label, delegation_id):
        raise RuntimeError("launcher died mid-spawn")


def _key_row(**fields) -> None:
    sets = ", ".join(f"{name}=?" for name in fields)
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            f"UPDATE dispatch_launch_keys SET {sets} WHERE launch_key=?", (*fields.values(), KEY)
        )


def test_a_crashed_launcher_is_settled_only_once_it_and_its_job_are_gone(db):
    """Review #1521 pass 5: the fenced recovery of a spawn whose launcher died."""
    task = _ready_task()
    with pytest.raises(RuntimeError):
        coordinator.claim_and_dispatch(
            task_id=task,
            actor="Desktop_Agent2",
            launch=_CrashingSpawn(),
            launch_key=KEY,
            lease_ttl_s=2.0,
        )
    with closing(storage.connect()) as conn:
        bound = coordinator.launch_key_run(conn, KEY)
        launcher = conn.execute(
            "SELECT spawn_launcher FROM dispatch_launch_keys WHERE launch_key=?", (KEY,)
        ).fetchone()[0]
    run_id = bound["run_id"]
    assert bound["spawn_unsettled"] is True
    assert supervise.parse_pid_token(launcher)[0] == os.getpid()
    time.sleep(2.5)  # the crashed claim's lease runs out; a retry reclaims the run
    boundary = RecordingLaunchBoundary()
    held = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert held["reason"] == "launch_unsettled" and boundary.calls == []

    def settle() -> dict:
        return coordinator.settle_unsettled_spawn(run_id, actor="Desktop_Operator")

    # The job may be running: the run records a live process.
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
            (supervise.pid_token(os.getpid()), run_id),
        )
    assert settle()["reason"] == "job_may_be_running"
    with closing(storage.connect()) as conn, conn:
        conn.execute("UPDATE dispatch_runs SET evidence_ref=NULL WHERE run_id=?", (run_id,))
    # The launcher may still be spawning: another process, alive.
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _key_row(spawn_launcher=supervise.pid_token(alive.pid))
        assert settle()["reason"] == "launcher_may_be_spawning"
    finally:
        alive.kill()
        alive.wait()
    # This process launched it and is no longer inside that spawn: both are gone.
    _key_row(spawn_launcher=launcher)
    assert settle()["settled"] is True
    assert settle()["reason"] == "not_unsettled"
    ran = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert ran["launched"] is True and len(boundary.calls) == 1


def test_a_launcher_paused_after_its_fence_is_never_settled_from_under_it(db, monkeypatch):
    """Review #1521 pass 6: the fenced spawn is in flight from the moment its mark exists.

    Launcher A pauses between its fence and the launch boundary until its lease has
    expired. The operator's settlement is refused (this process may still spawn
    it), a retry spawns nothing, and when A resumes exactly one job starts.
    """
    task = _ready_task()
    paused, resume = threading.Event(), threading.Event()
    real_fence = coordinator._fence_spawn

    def fence_then_pause(launch_key, run_id, actor):
        fenced = real_fence(launch_key, run_id, actor)
        paused.set()
        assert resume.wait(30)
        return fenced

    monkeypatch.setattr(coordinator, "_fence_spawn", fence_then_pause)
    boundary = RecordingLaunchBoundary()
    results: dict[str, dict] = {}
    launcher_a = threading.Thread(
        target=lambda: results.setdefault(
            "a",
            coordinator.claim_and_dispatch(
                task_id=task,
                actor="Desktop_Agent2",
                launch=boundary,
                launch_key=KEY,
                lease_ttl_s=2.0,
            ),
        )
    )
    launcher_a.start()
    assert paused.wait(30)
    time.sleep(2.5)  # A's lease runs out while it is paused after its fence
    with closing(storage.connect()) as conn:
        run_id = coordinator.launch_key_run(conn, KEY)["run_id"]
    settle = coordinator.settle_unsettled_spawn(run_id, actor="Desktop_Operator")
    assert settle == {"settled": False, "run_id": run_id, "reason": "launcher_may_be_spawning"}
    monkeypatch.setattr(coordinator, "_fence_spawn", real_fence)
    retry = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert retry["reason"] == "launch_unsettled" and boundary.calls == []
    resume.set()
    launcher_a.join(30)
    assert [call["run_id"] for call in boundary.calls] == [run_id]
    with closing(storage.connect()) as conn:
        assert coordinator.launch_key_run(conn, KEY)["launched"] is True
    assert run_id not in coordinator._SPAWNS_IN_FLIGHT


def test_a_failed_fence_recheck_spawns_nothing_and_leaves_nothing_registered(db, monkeypatch):
    """Review #1521 final check: an error re-reading the fence is not a spawn in flight."""
    task = _ready_task()

    def recheck_fails(launch_key, run_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(coordinator, "_fence_holds", recheck_fails)
    boundary = RecordingLaunchBoundary()
    with pytest.raises(sqlite3.OperationalError):
        coordinator.claim_and_dispatch(
            task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
        )
    assert boundary.calls == []
    assert coordinator._SPAWNS_IN_FLIGHT == set()


def test_a_failed_spawn_frees_the_key_for_its_own_task_only(db):
    first, second = _ready_task(), _ready_task()
    failing = RecordingLaunchBoundary(fail_with="binary_not_found:agy")
    failed = coordinator.claim_and_dispatch(
        task_id=first, actor="Desktop_Agent2", launch=failing, launch_key=KEY
    )
    assert failed["launched"] is False and failed["run_id"]
    with closing(storage.connect()) as conn:
        assert coordinator.launch_key_run(conn, KEY)["launched"] is False
    # Another task may not take a key bound to this launch, even a failed one.
    other = RecordingLaunchBoundary()
    refused = coordinator.claim_and_dispatch(
        task_id=second, actor="Desktop_Agent2", launch=other, launch_key=KEY
    )
    assert refused["reason"] == "launch_key_bound" and other.calls == []
    # The same task retries under the same key: the key moves to the new run.
    retry = RecordingLaunchBoundary()
    ran = coordinator.claim_and_dispatch(
        task_id=first, actor="Desktop_Agent2", launch=retry, launch_key=KEY
    )
    assert ran["launched"] is True and len(retry.calls) == 1
    with closing(storage.connect()) as conn:
        bound = coordinator.launch_key_run(conn, KEY)
    assert (bound["run_id"], bound["launched"]) == (ran["run_id"], True)
    again = coordinator.claim_and_dispatch(
        task_id=first, actor="Desktop_Agent2", launch=retry, launch_key=KEY
    )
    assert again["reason"] == "already_claimed" and len(retry.calls) == 1


# --- unkeyed dispatch shares the spawn fence (task #2512) ---------------------


def _expire_lease(run_id: str) -> None:
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?",
            ("2000-01-01T00:00:00+00:00", run_id),
        )


def _run_state(run_id: str) -> str:
    with closing(storage.connect()) as conn:
        return conn.execute("SELECT state FROM dispatch_runs WHERE run_id=?", (run_id,)).fetchone()[
            0
        ]


@pytest.mark.parametrize("first_key", [KEY, None], ids=["keyed_first", "unkeyed_first"])
def test_a_stalled_spawn_holds_its_task_against_an_unkeyed_dispatch(db, first_key):
    """#2512: a scheduler dispatch never spawns a second job beside a stalled spawn.

    Launcher A stalls inside its spawn past its lease; an unkeyed dispatch of the
    same task (the scheduler path) reclaims nothing and spawns nothing. When A's
    spawn returns, its run is the task's one launch.
    """
    task = _ready_task()
    boundary = _StalledSpawn()
    results: dict[str, dict] = {}
    launcher_a = threading.Thread(
        target=lambda: results.setdefault(
            "a",
            coordinator.claim_and_dispatch(
                task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=first_key
            ),
        )
    )
    launcher_a.start()
    assert boundary.entered.wait(30)
    run_id = boundary.calls[0]
    _expire_lease(run_id)  # A's lease runs out while it is still spawning
    try:
        b = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    finally:
        calls_meanwhile, state_meanwhile = len(boundary.calls), _run_state(run_id)
        boundary.release.set()
        launcher_a.join(30)
    assert (b["launched"], b["reason"], b["run_id"]) == (False, "launch_unsettled", run_id), b
    assert (calls_meanwhile, state_meanwhile) == (1, "claimed")
    assert results["a"]["launched"] is True and results["a"]["run_id"] == run_id
    assert len(boundary.calls) == 1
    with closing(storage.connect()) as conn:
        runs = conn.execute("SELECT run_id, state FROM dispatch_runs").fetchall()
    assert [(r["run_id"], r["state"]) for r in runs] == [(run_id, "launched")]


def test_reclaim_leaves_an_unsettled_spawn_claimed_and_its_delegation_in_flight(db):
    """#2512: lease reclaim never hands an unsettled spawn back as retryable."""
    task = _ready_task()
    with pytest.raises(RuntimeError):
        coordinator.claim_and_dispatch(
            task_id=task, actor="Desktop_Agent2", launch=_CrashingSpawn()
        )
    with closing(storage.connect()) as conn:
        run_id = conn.execute("SELECT run_id FROM dispatch_runs").fetchone()[0]
    _expire_lease(run_id)
    swept = coordinator.reclaim_expired_leases(actor="queue-hygiene")
    assert swept["reclaimed_count"] == 0
    assert [item["run_id"] for item in swept["spawn_unsettled"]] == [run_id]
    assert _run_state(run_id) == "claimed"
    with closing(storage.connect()) as conn:
        status = conn.execute("SELECT status FROM delegations WHERE task_id=?", (task,)).fetchone()
    assert status["status"] == "in_flight"
    # A run handed back by an older reclaim still holds its task: neither a new
    # claim nor a resume of it spawns while its outcome is unrecorded.
    with closing(storage.connect()) as conn, conn:
        conn.execute("UPDATE dispatch_runs SET state='retryable' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE delegations SET status='pending' WHERE task_id=?", (task,))
    boundary = RecordingLaunchBoundary()
    held = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert (held["reason"], held["run_id"]) == ("launch_unsettled", run_id)
    resumed = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=boundary)
    assert (resumed["ok"], resumed["reason"]) == (False, "launch_unsettled")
    assert boundary.calls == []


def test_an_unkeyed_unsettled_spawn_is_settled_only_with_proof(db):
    """#2512: the operator's settlement covers an unkeyed run, under the same proof."""
    task = _ready_task()
    with pytest.raises(RuntimeError):
        coordinator.claim_and_dispatch(
            task_id=task, actor="Desktop_Agent2", launch=_CrashingSpawn()
        )
    with closing(storage.connect()) as conn:
        run_id = conn.execute("SELECT run_id FROM dispatch_runs").fetchone()[0]

    def settle() -> dict:
        return coordinator.settle_unsettled_spawn(run_id, actor="Desktop_Operator")

    def mark_launcher(token: str) -> None:
        with closing(storage.connect()) as conn, conn:
            conn.execute(
                "UPDATE dispatch_launch_keys SET spawn_launcher=? WHERE run_id=?", (token, run_id)
            )

    assert settle()["reason"] == "lease_still_live"
    _expire_lease(run_id)
    with closing(storage.connect()) as conn, conn:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
            (supervise.pid_token(os.getpid()), run_id),
        )
    assert settle()["reason"] == "job_may_be_running"
    with closing(storage.connect()) as conn, conn:
        conn.execute("UPDATE dispatch_runs SET evidence_ref=NULL WHERE run_id=?", (run_id,))
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        mark_launcher(supervise.pid_token(alive.pid))
        assert settle()["reason"] == "launcher_may_be_spawning"
    finally:
        alive.kill()
        alive.wait()
    boundary = RecordingLaunchBoundary()
    held = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert held["reason"] == "launch_unsettled" and boundary.calls == []
    mark_launcher(supervise.pid_token(os.getpid()))
    assert settle()["settled"] is True
    ran = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert ran["launched"] is True and len(boundary.calls) == 1 and ran["run_id"] != run_id


def _crashed_unkeyed_spawn() -> tuple[int, str]:
    """An unkeyed dispatch whose spawn raised: its run is claimed, its spawn unsettled."""
    task = _ready_task()
    with pytest.raises(RuntimeError):
        coordinator.claim_and_dispatch(
            task_id=task, actor="Desktop_Agent2", launch=_CrashingSpawn()
        )
    with closing(storage.connect()) as conn:
        run_id = conn.execute("SELECT run_id FROM dispatch_runs").fetchone()[0]
    return task, run_id


def _alerts_and_notes(task: int, run_id: str) -> tuple[list[dict], list[str]]:
    with closing(storage.connect()) as conn:
        alerts = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM event_log WHERE channel='agent.health.alert'"
            ).fetchall()
        ]
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (task,)).fetchone()[0]
    lines = [line for line in notes.splitlines() if f"spawn-unsettled run={run_id}" in line]
    return alerts, lines


def test_the_tick_and_the_hygiene_pass_report_an_unsettled_spawn(db):
    """#2512 review: neither periodic surface drops a held run."""
    from unified_agent import queue_hygiene
    from unified_agent.dispatch import scheduler

    _task, run_id = _crashed_unkeyed_spawn()
    _expire_lease(run_id)
    boundary = RecordingLaunchBoundary()
    tick = scheduler.scheduler_tick(force=True, launch=boundary)
    assert [item["run_id"] for item in tick["spawn_unsettled"]] == [run_id]
    assert tick["spawn_unsettled_count"] == 1 and boundary.calls == []
    assert tick["spawn_unsettled"][0]["settle"].endswith(f"/{run_id}/settle-spawn")
    swept = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert [item["run_id"] for item in swept["dispatch_spawn_unsettled"]] == [run_id]
    assert swept["dispatch_spawn_unsettled"][0]["settle"].endswith(f"/{run_id}/settle-spawn")


def test_an_unsettled_spawn_is_announced_once_with_its_settle_route(db):
    """#2512 review: one health alert and one task note per run, not one per sweep."""
    from unified_agent import queue_hygiene

    task, run_id = _crashed_unkeyed_spawn()
    assert (
        queue_hygiene.run_hygiene_pass(send_notifications=False, dry_run=True)[
            "dispatch_spawn_unsettled"
        ][0]["warned"]
        is False
    )  # a live lease, and a dry run, announce nothing
    assert _alerts_and_notes(task, run_id) == ([], [])
    _expire_lease(run_id)
    first = coordinator.reclaim_expired_leases(actor="queue-hygiene")
    assert first["spawn_unsettled"][0]["warned"] is True
    for _ in range(2):
        coordinator.reclaim_expired_leases(actor="queue-hygiene")
        queue_hygiene.run_hygiene_pass(send_notifications=False)
    alerts, lines = _alerts_and_notes(task, run_id)
    assert [(a["kind"], a["run_id"], a["task_id"]) for a in alerts] == [
        ("dispatch_spawn_unsettled", run_id, task)
    ]
    route = f"POST /api/dispatch-runs/{run_id}/settle-spawn"
    assert alerts[0]["settle"] == route and route in alerts[0]["summary"]
    assert len(lines) == 1 and route in lines[0]


def test_the_doctor_degrades_on_a_spawn_unsettled_past_the_grace(db):
    """#2512 review: a launcher crash is visible in system_doctor, by run id."""
    from unified_agent.tools import doctor

    _task, run_id = _crashed_unkeyed_spawn()
    assert doctor._dispatch_unsettled_spawns_check()["status"] == "LIVE"  # lease still live
    _expire_lease(run_id)  # expired long ago: past the grace
    check = doctor._dispatch_unsettled_spawns_check()
    assert check["status"] == "DEGRADED" and run_id in check["detail"]


# --- a resume fences its own spawn (task #2527) -------------------------------


def _retryable_run(*, key: str | None = None, launched: bool = False) -> tuple[int, str]:
    """A run of a fresh task handed back as ``retryable``, its claim's spawn settled.

    ``launched``: the claim reached launch first (a ``dispatch-launched`` event),
    as a run that timed out after launch did; otherwise its claim's spawn failed.
    """
    task = _ready_task()
    boundary = RecordingLaunchBoundary(fail_with=None if launched else "binary_not_found:agy")
    out = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=key
    )
    run_id = out["run_id"]
    assert out["launched"] is launched, out
    if launched:
        with closing(storage.connect()) as conn, conn:
            conn.execute(
                "UPDATE dispatch_runs SET state='retryable', lease_owner=NULL, "
                "lease_expires_at=NULL WHERE run_id=?",
                (run_id,),
            )
            conn.execute("UPDATE delegations SET status='pending' WHERE task_id=?", (task,))
    assert _run_state(run_id) == "retryable"
    return task, run_id


def _spawn_marks(run_id: str) -> dict[str, str | None]:
    """The run's spawn marks: spawn key -> recorded outcome."""
    with closing(storage.connect()) as conn:
        rows = conn.execute(
            "SELECT launch_key, spawn_outcome FROM dispatch_launch_keys WHERE run_id=?", (run_id,)
        ).fetchall()
    return {row["launch_key"]: row["spawn_outcome"] for row in rows}


@pytest.mark.parametrize(
    ("key", "launched"),
    [(None, False), (None, True), (KEY, True)],
    ids=["unkeyed", "unkeyed_launched_before", "keyed_launched_before"],
)
def test_a_stalled_resume_holds_its_task_against_a_second_dispatch(db, key, launched):
    """#2527: a resume fences its own spawn, as a claim does.

    The resumed spawn stalls past its lease. Reclaim keeps the run claimed and a
    scheduler dispatch of the task spawns nothing beside it. A run that reached
    launch before its resume is held too: that launch proves its claim's spawn,
    not the resume's.
    """
    task, run_id = _retryable_run(key=key, launched=launched)
    boundary = _StalledSpawn()
    results: dict[str, dict] = {}
    resumer = threading.Thread(
        target=lambda: results.setdefault(
            "a", coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=boundary)
        )
    )
    resumer.start()
    assert boundary.entered.wait(30)
    _expire_lease(run_id)  # the resume's lease runs out while it is still spawning
    try:
        swept = coordinator.reclaim_expired_leases(actor="queue-hygiene")
        held = [item["run_id"] for item in coordinator.unsettled_spawns()]
        second = coordinator.claim_and_dispatch(
            task_id=task, actor="Desktop_Agent2", launch=boundary
        )
    finally:
        calls_meanwhile, state_meanwhile = len(boundary.calls), _run_state(run_id)
        boundary.release.set()
        resumer.join(30)
    assert [item["run_id"] for item in swept["spawn_unsettled"]] == [run_id]
    assert held == [run_id]
    assert (second["launched"], second["reason"], second["run_id"]) == (
        False,
        "launch_unsettled",
        run_id,
    ), second
    assert (calls_meanwhile, state_meanwhile) == (1, "claimed")
    assert (results["a"]["ok"], results["a"]["state"]) == (True, "launched"), results["a"]
    assert _run_state(run_id) == "launched" and len(boundary.calls) == 1


def test_a_settled_resumed_spawn_lets_later_work_proceed(db):
    """#2527: a resume's recorded outcome releases its task; nothing stays held."""
    task, run_id = _retryable_run()
    boundary = RecordingLaunchBoundary()
    resumed = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=boundary)
    assert (resumed["ok"], resumed["state"]) == (True, "launched"), resumed
    assert _spawn_marks(run_id) == {
        f"run:{run_id}": "failed",
        coordinator._resume_spawn_key(run_id): "spawned",
    }
    assert coordinator.unsettled_spawns() == [] and coordinator._SPAWNS_IN_FLIGHT == set()
    # The job fails and the run is handed back: a scheduler dispatch proceeds.
    with closing(storage.connect()) as conn, conn:
        conn.execute("UPDATE dispatch_runs SET state='retryable' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE delegations SET status='pending' WHERE task_id=?", (task,))
    ran = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert ran["launched"] is True and ran["run_id"] != run_id and len(boundary.calls) == 2


def test_a_failed_resumed_spawn_is_settled_and_retryable(db):
    """#2527: a resume whose spawn fails records it, and the run resumes again."""
    _task, run_id = _retryable_run()
    failing = RecordingLaunchBoundary(fail_with="binary_not_found:agy")
    out = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=failing)
    assert (out["ok"], out["reason"], out["state"]) == (False, "launch_failed", "retryable")
    assert _run_state(run_id) == "retryable"
    assert _spawn_marks(run_id)[coordinator._resume_spawn_key(run_id)] == "failed"
    assert coordinator.unsettled_spawns() == [] and coordinator._SPAWNS_IN_FLIGHT == set()
    boundary = RecordingLaunchBoundary()
    again = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=boundary)
    assert (again["ok"], again["state"]) == (True, "launched") and len(boundary.calls) == 1
    assert _spawn_marks(run_id)[coordinator._resume_spawn_key(run_id)] == "spawned"


class _LeaseLostSpawn:
    """A boundary that spawns, while its launcher loses the claim before recording launch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch(self, *, run_id, task_id, agent_label, delegation_id):
        self.calls.append(run_id)
        with closing(storage.connect()) as conn, conn:
            conn.execute(
                "UPDATE dispatch_runs SET lease_owner='Desktop_Other' WHERE run_id=?", (run_id,)
            )
        return {"ok": True, "stub": True, "run_id": run_id}


def test_a_resumed_keyed_run_keeps_its_launch_evidence(db):
    """#2527: a resume never rewrites the Control Center key's spawn record.

    The keyed claim spawned a job but lost its lease before recording the launch,
    so ``spawn_outcome='spawned'`` is the key's only launch evidence. A resume of
    the run whose own spawn fails leaves that evidence, and the key, as they were.
    """
    task = _ready_task()
    spawner = _LeaseLostSpawn()
    out = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=spawner, launch_key=KEY
    )
    run_id = out["run_id"]
    assert (out["launched"], out["reason"]) == (False, "lease_lost")
    _expire_lease(run_id)
    coordinator.reclaim_expired_leases(actor="queue-hygiene")
    assert _run_state(run_id) == "retryable"
    failing = RecordingLaunchBoundary(fail_with="binary_not_found:agy")
    resumed = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=failing)
    assert resumed["reason"] == "launch_failed" and len(failing.calls) == 1
    assert _spawn_marks(run_id) == {KEY: "spawned", coordinator._resume_spawn_key(run_id): "failed"}
    with closing(storage.connect()) as conn:
        bound = coordinator.launch_key_run(conn, KEY)
    assert (bound["run_id"], bound["launched"], bound["spawn_outcome"]) == (run_id, True, "spawned")
    # The key's job was spawned: a repeat launch under it starts nothing.
    boundary = RecordingLaunchBoundary()
    again = coordinator.claim_and_dispatch(
        task_id=task, actor="Desktop_Agent2", launch=boundary, launch_key=KEY
    )
    assert again["reason"] == "already_claimed" and boundary.calls == []


def test_a_crashed_resumed_spawn_is_held_until_the_operator_settles_it(db):
    """#2527: the settle-spawn recovery covers a resume's spawn, under the same proof."""
    task, run_id = _retryable_run()
    with pytest.raises(RuntimeError):
        coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=_CrashingSpawn())

    def settle() -> dict:
        return coordinator.settle_unsettled_spawn(run_id, actor="Desktop_Operator")

    assert settle()["reason"] == "lease_still_live"
    _expire_lease(run_id)
    boundary = RecordingLaunchBoundary()
    held = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert (held["reason"], held["run_id"]) == ("launch_unsettled", run_id) and boundary.calls == []
    assert _run_state(run_id) == "claimed"
    # This process launched it and is no longer inside that spawn: both are gone.
    assert settle()["settled"] is True
    assert _spawn_marks(run_id)[coordinator._resume_spawn_key(run_id)] == "failed"
    ran = coordinator.claim_and_dispatch(task_id=task, actor="Desktop_Agent2", launch=boundary)
    assert ran["launched"] is True and ran["run_id"] != run_id and len(boundary.calls) == 1
