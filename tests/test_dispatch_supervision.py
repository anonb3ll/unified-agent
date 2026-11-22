"""Supervision of spawned agent sessions (task #602).

The bug this closes was reachable the moment UA_DISPATCH_REAL_SPAWN=1:
``reclaim_expired_leases`` checked pid liveness only for state 'claimed' — the
narrow Popen-to-launched window. Every real spawn lands in 'launched', where a
live pid got no protection, so at lease expiry (300s) the run was marked
retryable/timeout and its delegation released back to pending. The next tick
could launch the same task alongside the agent already doing it.

Real child processes are used deliberately: the whole subject is
process-boundary behaviour, which a mock cannot exercise.
"""

from __future__ import annotations

import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_agent import storage
from unified_agent.dispatch import coordinator, supervise
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.tools import delegate, tasks


@pytest.fixture(autouse=True)
def exit_watchers(monkeypatch):
    """Finish this test's dispatch reporting before monkeypatch restores UA_HOME.

    A terminal run row does not mean its exit watcher has finished the remaining
    bookkeeping. Track only threads created by the dispatch helper, without
    changing the global threading module or joining unrelated observers.
    """
    thread_factory = supervise.threading.Thread
    watchers = []

    def tracked_thread(*args, **kwargs):
        thread = thread_factory(*args, **kwargs)
        if kwargs.get("target") is supervise._wait_and_report_exit and thread.name.startswith(
            "dispatch-supervisor-"
        ):
            watchers.append(thread)
        return thread

    monkeypatch.setattr(supervise, "threading", SimpleNamespace(Thread=tracked_thread))
    yield watchers
    deadline = time.monotonic() + 20
    for watcher in watchers:
        if watcher.ident is not None:
            watcher.join(timeout=max(0, deadline - time.monotonic()))
    unfinished = [watcher.name for watcher in watchers if watcher.is_alive()]
    assert not unfinished, f"dispatch exit watchers did not finish: {unfinished}"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_DISPATCH_RUN_MAX_S", raising=False)
    storage.init_db()
    return tmp_path


@pytest.fixture
def child():
    """A real, harmless, long-lived child that is always cleaned up."""
    procs: list[subprocess.Popen] = []

    def _spawn(seconds: float = 30.0) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        procs.append(proc)
        return proc

    yield _spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _spec() -> str:
    return (
        "### Objective\nSupervision\n### Scope\n- src/unified_agent/dispatch/\n"
        "### Definition of Done\n- live runs are not reclaimed\n"
        "### TDD Constraints\n- Must be real: process boundary\n"
        "### Boundaries\n- Must not enable production dispatch\n"
    )


def _launched_run_with_pid(pid_token: str | None) -> dict:
    packed = delegate.delegate(
        task_spec=_spec(),
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent1",
        callback="memory",
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-25T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    if pid_token is not None:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
                (pid_token, out["run_id"]),
            )
            conn.commit()
        finally:
            conn.close()
    return {"run_id": out["run_id"], "task_id": packed["task_id"]}


def _expire_lease(run_id: str, *, seconds_ago: float = 60.0) -> None:
    """Backdate the lease instead of sleeping 300s."""
    past = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (past, run_id))
        conn.commit()
    finally:
        conn.close()


def _backdate_created(run_id: str, *, seconds_ago: float) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET created_at=? WHERE run_id=?", (past, run_id))
        conn.commit()
    finally:
        conn.close()


# ── the bug ─────────────────────────────────────────────────────────────────


def test_a_live_launched_run_is_renewed_not_reclaimed(db, child):
    """THE regression. Before #602 this run was marked retryable/'timeout' and
    its delegation released, so a later tick could double-launch it while the
    agent was still working."""
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _expire_lease(run["run_id"])

    result = coordinator.reclaim_expired_leases()

    assert result["reclaimed_count"] == 0, "a working agent must not be reclaimed"
    assert result["renewed_count"] == 1
    assert coordinator.get_run(run["run_id"])["state"] == "launched"


def test_the_delegation_is_not_released_while_the_agent_works(db, child):
    """The reclaim path releases the delegation back to 'pending', which is
    what actually enables the duplicate launch. Assert the delegation itself."""
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _expire_lease(run["run_id"])
    coordinator.reclaim_expired_leases()

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status FROM delegations WHERE task_id=?", (run["task_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] != "pending", "released delegation = duplicate launch next tick"


def test_a_dead_launched_run_is_still_reclaimed(db, child):
    """The fix must not make reclaim useless: a process that died without
    reporting still has to be collected."""
    proc = child(30.0)
    token = supervise.pid_token(proc.pid)
    proc.kill()
    proc.wait(timeout=10)

    run = _launched_run_with_pid(token)
    _expire_lease(run["run_id"])
    result = coordinator.reclaim_expired_leases()

    assert result["reclaimed_count"] == 1
    assert result["renewed_count"] == 0
    assert coordinator.get_run(run["run_id"])["state"] == "retryable"


def test_a_run_with_no_pid_recorded_is_reclaimed_as_before(db):
    run = _launched_run_with_pid(None)
    _expire_lease(run["run_id"])
    assert coordinator.reclaim_expired_leases()["reclaimed_count"] == 1


# ── pid reuse ───────────────────────────────────────────────────────────────


def test_a_recycled_pid_is_not_mistaken_for_our_process(db, child):
    """os.kill(pid, 0) answers 'is A process alive', not 'is OUR process
    alive'. A start-time token distinguishes them."""
    proc = child(30.0)
    real_token = supervise.pid_token(proc.pid)
    assert supervise.pid_is_ours(real_token) is True

    pid, start = supervise.parse_pid_token(real_token)
    assert pid == proc.pid
    if start is None:
        pytest.skip("ps -o lstart= unavailable on this host; identity degrades to liveness")

    forged = f"pid:{proc.pid}:{'0' * len(start)}"
    assert supervise.pid_is_ours(forged) is False, "different start time = different process"


def test_a_legacy_pid_token_still_works(db, child):
    """Rows written before #602 carry bare 'pid:N'. They must keep the
    behaviour they had — liveness only — not start failing closed."""
    proc = child(30.0)
    assert supervise.pid_is_ours(f"pid:{proc.pid}") is True
    proc.kill()
    proc.wait(timeout=10)
    assert supervise.pid_is_ours(f"pid:{proc.pid}") is False


def test_malformed_evidence_is_not_read_as_a_live_process(db):
    for junk in (None, "", "not-a-pid", "pid:", "pid:abc", "https://example/pr/1"):
        assert supervise.pid_is_ours(junk) is False, junk


# ── wall-clock ceiling ──────────────────────────────────────────────────────


def test_a_live_run_past_the_ceiling_is_flagged_overran(db, child, monkeypatch):
    """A hung agent must not hold its slot forever just because it is alive."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    result = coordinator.reclaim_expired_leases()
    assert result["overran_count"] == 1
    assert result["renewed_count"] == 0
    assert result["overran"][0]["pid"] == proc.pid
    assert coordinator.get_run(run["run_id"])["state"] == "retryable"


def test_the_ceiling_can_be_disabled_explicitly(db, child, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "0")
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=100_000)
    _expire_lease(run["run_id"])
    assert coordinator.reclaim_expired_leases()["renewed_count"] == 1


def test_unparseable_age_does_not_kill_on_a_guess():
    assert supervise.runtime_exceeded("not-a-timestamp", max_s=1) is False
    assert supervise.runtime_exceeded(None, max_s=1) is False


# ── termination ─────────────────────────────────────────────────────────────


def test_terminate_stops_a_real_process_and_reports_how(db, child):
    proc = child(60.0)
    outcome = supervise.terminate(proc.pid, grace_s=5.0)
    assert outcome in {"terminated", "killed"}
    proc.wait(timeout=10)
    assert proc.poll() is not None
    assert supervise.pid_alive(proc.pid) is False


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored(db):
    """SIGTERM first is not optional — a headless CLI may need to flush a log.
    But an agent that traps it must not become unkillable."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\ntime.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"  # the handler is installed
        assert supervise.terminate(proc.pid, grace_s=1.0) == "killed"
        proc.wait(timeout=10)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_terminating_an_already_dead_process_is_not_an_error(db, child):
    proc = child(30.0)
    proc.kill()
    proc.wait(timeout=10)
    assert supervise.terminate(proc.pid) == "already_gone"


def test_terminate_returns_refused_on_permission_error(monkeypatch):
    """PR #292. PermissionError subclasses OSError — must not read as
    already_gone, or coordinator overrun blocking fails open."""

    def deny_signal(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", deny_signal)
    monkeypatch.setattr(os, "killpg", deny_signal)
    assert supervise.terminate(12345) == "refused"


def test_sigkill_delivery_alone_is_not_confirmation(db, monkeypatch):
    """Devin review finding 4: SIGKILL cannot be caught, but a process stuck in
    uninterruptible I/O wait can still be observed alive right after delivery
    — a real process (harder to construct portably in a test) is simulated by
    forcing the post-kill liveness check to report "still here" for the whole
    confirmation window. terminate() must report 'kill_unconfirmed' (an
    UNKNOWN outcome) rather than blindly report 'killed' (a CONFIRMED one).
    """
    monkeypatch.setattr(supervise, "KILL_CONFIRM_S", 0.2)
    monkeypatch.setattr(supervise, "_termination_target_alive", lambda pid, pgid: True)
    monkeypatch.setattr(os, "kill", lambda *a, **kw: None)
    monkeypatch.setattr(supervise, "pid_alive", lambda pid: True)

    outcome = supervise.terminate(999999, grace_s=0.05)

    assert outcome == "kill_unconfirmed"
    assert outcome in supervise.UNKNOWN_TERMINATION_OUTCOMES
    assert supervise.classify_termination_outcome(outcome) == supervise.CESSATION_UNKNOWN


def test_sigkill_confirmed_once_the_target_actually_exits(db, child):
    """The normal case: a real process dies promptly under SIGKILL and
    terminate() confirms it within the (short, test-scoped) poll window."""
    proc = child(60.0)
    outcome = supervise.terminate(proc.pid, grace_s=0.2)

    assert outcome == "killed"
    assert outcome in supervise.CONFIRMED_TERMINATION_OUTCOMES
    proc.wait(timeout=10)


def test_reclaim_treats_kill_unconfirmed_like_a_refused_termination(db, monkeypatch):
    """Devin finding 4 + finding 1: an overran run whose SIGKILL exit was never
    observed must be blocked for operator reconciliation, exactly like a
    refused or identity-mismatched termination — never quietly retried."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")

    run = _launched_run_with_pid("pid:999999:fake-start")
    run_id = run["run_id"]
    _backdate_created(run_id, seconds_ago=120)
    _expire_lease(run_id, seconds_ago=1)

    monkeypatch.setattr(supervise, "pid_is_ours", lambda _evidence: True)
    monkeypatch.setattr(coordinator, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(supervise, "terminate_ours", lambda _evidence: "kill_unconfirmed")

    result = coordinator.reclaim_expired_leases()

    assert result["overran_count"] == 1
    assert result["overran"][0]["terminated"] == "kill_unconfirmed"
    run_row = coordinator.get_run(run_id)
    assert run_row["state"] == "blocked"
    assert run_row["failure_reason"] == "runtime_ceiling_kill_unconfirmed"


# ── regressions from review of PR #292 ───────────────────────────


def test_overran_processes_are_killed_by_reclaim_itself_not_the_caller(db, child, monkeypatch):
    """Termination originally lived in DispatchScheduler.tick(),
    but claim_and_dispatch() also calls reclaim_expired_leases() and DISCARDS
    the result — so a run found overran through that path had its row marked
    terminal while the process kept running. Kill where it is detected."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    proc = child(60.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    result = coordinator.reclaim_expired_leases()

    assert result["overran_count"] == 1
    assert result["overran"][0]["terminated"] in {"terminated", "killed"}
    proc.wait(timeout=15)
    assert proc.poll() is not None, "reclaim must stop the process, not just the row"
    assert supervise.pid_alive(proc.pid) is False


def test_nan_ceiling_does_not_silently_disable_the_bound(monkeypatch):
    """float('nan') parses, and every comparison against NaN
    is False — so a NaN ceiling never fires while the config claims a bound."""
    from unified_agent import config

    for raw in ("nan", "NaN"):
        monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", raw)
        assert config.dispatch_run_max_s() == 3600.0

    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "nan")
    assert supervise.runtime_exceeded((datetime.now(UTC) - timedelta(days=365)).isoformat()) is True


def test_infinity_ceiling_is_an_explicit_no_bound(monkeypatch):
    """Infinity is an honest 'no ceiling' — distinct from NaN and from 0."""
    from unified_agent import config

    for raw in ("inf", "infinity", "-inf"):
        monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", raw)
        ceiling = config.dispatch_run_max_s()
        assert not math.isfinite(ceiling) or ceiling <= 0

    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "inf")
    assert (
        supervise.runtime_exceeded((datetime.now(UTC) - timedelta(days=365)).isoformat()) is False
    )


def test_refused_overran_termination_blocks_the_run(db, child, monkeypatch):
    """PR #292. A refused kill leaves the process live — block before
    the ready-set can relaunch. Delegation must stay in_flight until then."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    monkeypatch.setattr(supervise, "terminate_ours", lambda evidence_ref, **kw: "refused")
    proc = child(60.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    coordinator.reclaim_expired_leases()

    assert coordinator.get_run(run["run_id"])["state"] == "blocked"
    assert coordinator.get_run(run["run_id"])["failure_reason"] == (
        "runtime_ceiling_termination_refused"
    )
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status FROM delegations WHERE task_id=?", (run["task_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] != "pending", "delegation must not be released while kill refused"


def test_overran_delegation_stays_in_flight_until_process_is_gone(db, child, monkeypatch):
    """PR #292. Terminate before release: delegation stays in_flight
    while the overran child is still alive mid-reclaim."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    proc = child(60.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    import threading

    killing = threading.Event()
    observed = threading.Event()

    # Termination holds until the delegation has been read mid-reclaim.
    def slow_kill(evidence_ref, **kw):
        killing.set()
        observed.wait(timeout=10)
        return supervise.terminate(proc.pid, grace_s=0.5)

    monkeypatch.setattr(supervise, "terminate_ours", slow_kill)

    during: list[str] = []

    def observe() -> None:
        assert killing.wait(timeout=10), "termination never started"
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM delegations WHERE task_id=?", (run["task_id"],)
            ).fetchone()
            during.append(row["status"])
        finally:
            conn.close()
            observed.set()

    observer = threading.Thread(target=observe)
    observer.start()
    result = coordinator.reclaim_expired_leases()
    observer.join(timeout=5)

    assert result["overran_count"] == 1
    assert during == ["in_flight"], "delegation must not flip to pending before kill"
    proc.wait(timeout=15)
    assert coordinator.get_run(run["run_id"])["state"] == "retryable"


def test_identity_mismatch_blocks_overran_run(db, child, monkeypatch):
    """A recycled pid must not be signalled; block instead of retryable release."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    proc = child(60.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    pid, start = supervise.parse_pid_token(supervise.pid_token(proc.pid))
    if start is None:
        pytest.skip("ps -o lstart= unavailable; cannot test identity mismatch")
    forged = f"pid:{proc.pid}:{'0' * len(start)}"
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
            (forged, run["run_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    result = coordinator.reclaim_expired_leases()

    assert result["overran_count"] == 1
    assert result["overran"][0]["terminated"] == "identity_mismatch"
    assert coordinator.get_run(run["run_id"])["state"] == "blocked"
    assert proc.poll() is None, "must not signal a process whose identity does not match"
    proc.kill()
    proc.wait(timeout=10)


def test_overran_reclaim_skips_already_terminal_run(db, child, monkeypatch):
    """PR #295. If the exit watcher reports while terminate blocks,
    the guarded UPDATE matches zero rows and reclaim must not be claimed."""
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _backdate_created(run["run_id"], seconds_ago=120)
    _expire_lease(run["run_id"])

    def finish_then_terminate(evidence_ref, **kw):
        from unified_agent.dispatch.report import report_outcome

        report_outcome("finished", run_id=run["run_id"], evidence_ref="https://example/pr/1")
        return supervise.terminate(proc.pid, grace_s=0.5)

    monkeypatch.setattr(supervise, "terminate_ours", finish_then_terminate)

    result = coordinator.reclaim_expired_leases()

    assert result["overran_count"] == 1
    assert result["reclaimed_count"] == 0
    assert coordinator.get_run(run["run_id"])["state"] == "finished"
    assert coordinator.get_run(run["run_id"])["evidence_ref"] == "https://example/pr/1"
    proc.kill()
    proc.wait(timeout=10)


def test_persist_spawn_pid_does_not_overwrite_terminal_evidence(db):
    """PR #295. A fast-exiting child can self-report before spawn()
    returns; do not replace terminal evidence with the pid token."""
    from unified_agent.dispatch.launch import _persist_spawn_pid
    from unified_agent.dispatch.report import report_outcome

    run = _launched_run_with_pid(None)
    report_outcome("finished", run_id=run["run_id"], evidence_ref="https://example/pr/1")

    _persist_spawn_pid(run["run_id"], 424242)

    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished"
    assert row["evidence_ref"] == "https://example/pr/1"


def test_spawn_exit_watcher_records_finished_on_zero_exit(db):
    """Codex PR #292. CliProcessSpawner must supervise exits via report_exit."""
    import subprocess
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    run = _launched_run_with_pid(None)
    supervise.spawn_exit_watcher(proc, run["run_id"])
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if coordinator.get_run(run["run_id"])["state"] == "finished":
            break
        time.sleep(0.01)
    assert coordinator.get_run(run["run_id"])["state"] == "finished"


def test_terminate_kills_child_process_group(db):
    """Codex PR #292. Detached CLIs use start_new_session — descendants must die too."""
    import textwrap

    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import subprocess, sys, time
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                )
                print(child.pid, flush=True)
                time.sleep(60)
                """
            ),
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = int(leader.stdout.readline().strip())
        assert supervise.pid_alive(child_pid)
        assert supervise.terminate(leader.pid, grace_s=1.0) in {"terminated", "killed"}
        leader.wait(timeout=10)
        _wait_pid_gone(child_pid)
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=10)


def test_spawn_exit_watcher_survives_early_parent_exit(db, tmp_path):
    """Codex PR #292. Detached supervisor reports finished when CLI exits 0."""
    import os
    import sys
    import time

    from unified_agent import config

    run = _launched_run_with_pid(None)
    log_dir = config.data_home() / "dispatch-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run['run_id']}-test.log"
    # The CLI exits only once the test releases it, so the run is still
    # launched when checked however slowly the test itself runs.
    release = tmp_path / "release-cli"
    cli = (
        "import os, time\n"
        "deadline = time.monotonic() + 30\n"
        f"while not os.path.exists({str(release)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
    )
    pid = supervise.spawn_via_detached_supervisor(
        [sys.executable, "-c", cli],
        cwd=".",
        env=os.environ.copy(),
        log_path=log_path,
        run_id=run["run_id"],
    )
    try:
        assert pid > 0
        assert coordinator.get_run(run["run_id"])["state"] == "launched"
    finally:
        release.touch()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        state = coordinator.get_run(run["run_id"])["state"]
        if state == "finished":
            break
        time.sleep(0.01)
    assert coordinator.get_run(run["run_id"])["state"] == "finished"


def test_detached_supervisor_cleans_pid_file_when_start_fails(db, monkeypatch, tmp_path):
    """A failed supervisor start must not leave its handshake directory behind."""
    import tempfile

    tracked: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def track_handshake_dir(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        tracked.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", track_handshake_dir)

    def fail_popen(*args, **kwargs):
        raise OSError(2, "simulated supervisor start failure")

    monkeypatch.setattr(supervise.subprocess, "Popen", fail_popen)
    with pytest.raises(OSError, match="simulated supervisor start failure"):
        supervise.spawn_via_detached_supervisor(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            env=os.environ.copy(),
            log_path=tmp_path / "run.log",
            run_id="run-pid-cleanup",
        )
    assert tracked
    assert not tracked[0].exists()


def test_terminate_waits_for_process_group_after_leader_exits(db):
    """Codex PR #292. SIGTERM may kill the leader while tool children linger."""
    import textwrap

    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import signal, subprocess, sys, time

                def _exit_on_term(signum, frame):
                    sys.exit(0)

                signal.signal(signal.SIGTERM, _exit_on_term)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "print('ready', flush=True); time.sleep(60)",
                    ],
                )
                print(child.pid, flush=True)
                time.sleep(60)
                """
            ),
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # The child shares the leader's stdout: its pid, and the child's "ready"
        # once it ignores SIGTERM, arrive in either order.
        lines = {leader.stdout.readline().strip() for _ in range(2)}
        assert "ready" in lines
        (child_pid,) = (int(line) for line in lines if line != "ready")
        assert supervise.pid_alive(child_pid)
        assert supervise.terminate(leader.pid, grace_s=1.0) in {"terminated", "killed"}
        leader.wait(timeout=10)
        _wait_pid_gone(child_pid)
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=10)


def test_reclaim_renews_with_configured_lease_ttl(db, child, monkeypatch):
    """Codex PR #292. Renewal must honour UA_DISPATCH_SCHEDULER_LEASE_TTL_S."""
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_LEASE_TTL_S", "30")
    proc = child(30.0)
    run = _launched_run_with_pid(supervise.pid_token(proc.pid))
    _expire_lease(run["run_id"])

    before = datetime.now(UTC)
    result = coordinator.reclaim_expired_leases()
    after = datetime.now(UTC)

    assert result["renewed_count"] == 1
    row = coordinator.get_run(run["run_id"])
    renewed = datetime.fromisoformat(row["lease_expires_at"])
    if renewed.tzinfo is None:
        renewed = renewed.replace(tzinfo=UTC)
    lower = before + timedelta(seconds=25)
    upper = after + timedelta(seconds=35)
    assert lower <= renewed <= upper


def test_wait_and_report_exit_retries_transient_sqlite_lock(db, monkeypatch):
    """Codex PR #296. A brief lock must not strand a successful agent at launched."""
    from unified_agent.dispatch import report as report_mod

    run = _launched_run_with_pid(None)
    calls = 0
    real_report = report_mod.report_exit

    def flaky_report(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise sqlite3.OperationalError("database is locked")
        return real_report(*args, **kwargs)

    monkeypatch.setattr(report_mod, "report_exit", flaky_report)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait(timeout=10)
    supervise._wait_and_report_exit(proc, run["run_id"])
    assert calls == 2
    assert coordinator.get_run(run["run_id"])["state"] == "finished"


def test_wait_and_report_exit_repairs_task_bookkeeping_after_sqlite_lock(db, monkeypatch):
    """A post-commit task lock must not strand the task in doing."""
    from unified_agent.dispatch import scheduler

    run = _launched_run_with_pid(None)
    calls = 0
    attempts = []
    real_task_update = scheduler.tasks_tool.task_update

    def flaky_task_update(*args, **kwargs):
        nonlocal calls
        calls += 1
        attempts.append(kwargs)
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_task_update(*args, **kwargs)

    monkeypatch.setattr(scheduler.tasks_tool, "task_update", flaky_task_update)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait(timeout=10)
    supervise._report_exit_resilient(run["run_id"], 0)

    # The failed attempt and retry each carry the complete atomic task write:
    # close, dispatch outcome and follow-up. No separate note writes may race.
    assert calls == 2
    for attempt in attempts:
        assert attempt["status"] == "done"
        assert attempt["expected_updated_at"] is not None
        assert f"{scheduler.DISPATCH_NOTE_PREFIX} finished" in attempt["append_note"]
        assert f"[follow-up: pending run={run['run_id']}" in attempt["append_note"]
    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == run["task_id"])
    assert task["status"] == "done"
    assert task["completed_at"] is not None
    notes = task["notes"] or ""
    assert f"run={run['run_id']}" in notes
    assert f"{scheduler.DISPATCH_NOTE_PREFIX} finished" in notes
    assert notes.count(f"{scheduler.DISPATCH_NOTE_PREFIX} finished") == 1
    assert notes.count(f"[follow-up: pending run={run['run_id']}") == 1


def test_record_run_outcome_reopened_task_not_reclosed_by_duplicate_report(db, monkeypatch):
    """Codex PR #304. A delayed same-run report must not close a reopened task."""
    from unified_agent.dispatch import scheduler

    run = _launched_run_with_pid(None)
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert scheduler.record_run_outcome(run["run_id"], "finished")["ok"] is True

    tasks.task_update(run["task_id"], status="doing")
    duplicate = scheduler.record_run_outcome(run["run_id"], "finished")
    assert duplicate["ok"] is False
    assert duplicate["reason"] == "already_terminal"

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == run["task_id"])
    assert task["status"] == "doing"
    assert task["completed_at"] is None


@pytest.mark.parametrize("replanned", [False, True], ids=["repair", "preserve-newer-edit"])
def test_record_run_outcome_replays_bookkeeping_on_concurrent_terminal_update(
    db, monkeypatch, replanned
):
    """PR #304. rowcount==0 must still repair stranded task bookkeeping."""
    from unified_agent.dispatch import scheduler

    run = _launched_run_with_pid(None)
    real_get_run = coordinator.get_run
    # Task revisions and record_run_outcome's finish identity retain
    # microseconds. Model the race within one second, without clock rounding
    # making a pre-finish task edit appear newer than the terminal transition.
    before_finish = "2026-10-07T01:00:00.100000+00:00"
    finished = "2026-10-07T01:00:00.200000+00:00"
    after_finish = "2026-10-07T01:00:00.300000+00:00"
    setup = storage.connect()
    try:
        setup.execute("UPDATE tasks SET updated_at=? WHERE id=?", (before_finish, run["task_id"]))
        setup.commit()
    finally:
        setup.close()

    def stale_launched_then_terminalize(run_id: str):
        row = real_get_run(run_id)
        if row and row["state"] == "launched":
            side = storage.connect()
            try:
                side.execute(
                    "UPDATE dispatch_runs SET state='finished', updated_at=? WHERE run_id=?",
                    (finished, run_id),
                )
                if replanned:
                    side.execute(
                        "UPDATE tasks SET notes=notes || '\nHuman replanned this task', "
                        "updated_at=? WHERE id=?",
                        (after_finish, run["task_id"]),
                    )
                side.commit()
            finally:
                side.close()
            return row
        return row

    monkeypatch.setattr(coordinator, "get_run", stale_launched_then_terminalize)

    out = scheduler.record_run_outcome(run["run_id"], "finished")
    assert out["ok"] is False
    assert out["reason"] == "already_terminal"
    assert coordinator.get_run(run["run_id"])["state"] == "finished"

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == run["task_id"])
    notes = task["notes"] or ""
    if replanned:
        assert task["status"] == "open"
        assert task["completed_at"] is None
        assert task["updated_at"] == after_finish
        assert notes.endswith("Human replanned this task")
        assert f"{scheduler.DISPATCH_NOTE_PREFIX} finished" not in notes
        assert "[follow-up: pending" not in notes
    else:
        assert task["status"] == "done"
        assert task["completed_at"] is not None
        assert notes.count(f"{scheduler.DISPATCH_NOTE_PREFIX} finished") == 1
        assert notes.count(f"[follow-up: pending run={run['run_id']}") == 1


def test_cli_process_spawner_exit_zero_is_recorded_finished(db, monkeypatch):
    """Task #604 acceptance: production CliProcessSpawner records exit 0 as finished."""
    from unified_agent.dispatch import routing
    from unified_agent.dispatch.launch import CliProcessSpawner

    run = _launched_run_with_pid(None)
    monkeypatch.setattr(
        routing,
        "headless_argv",
        lambda backend, binary, prompt=None, cwd=None, **_kw: [
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
    )
    out = CliProcessSpawner().spawn(
        {
            "binary": sys.executable,
            "cli_backend": "claude",
            "cwd": ".",
            "task_spec": "irrelevant",
            "run_id": run["run_id"],
            "agent_label": "Desktop_Agent1",
            "task_id": run["task_id"],
            "env": {},
        }
    )
    assert out["ok"] is True and out["spawned"] is True
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        state = coordinator.get_run(run["run_id"])["state"]
        if state in {"finished", "failed"}:
            break
        time.sleep(0.02)
    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished"
    assert not row["failure_reason"]


def _wait_pid_gone(pid: int, timeout: float = 5.0) -> None:
    """Wait for ``pid`` to stop running rather than sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while supervise.pid_alive(pid):
        assert time.monotonic() < deadline, f"pid {pid} is still alive"
        time.sleep(0.01)
