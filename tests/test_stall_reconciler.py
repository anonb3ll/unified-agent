"""Stall reconciler gaps left by #1916 (task #1950).

1. Output-silence stall detection reaches the Pi and agy-interactive backends.
2. The detached supervisor's Popen-to-PID-write window can no longer lose a
   clean exit: the child does no work until its pid is durable and published,
   and the parent's give-up and the supervisor's publish are one atomic race.
3. A duplicate (or stale) node receipt never transitions a run that is no
   longer bound to its item, nor one the reconciler already handed back.
"""

from __future__ import annotations

import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from unified_agent import config, node_queue, storage
from unified_agent.dispatch import coordinator, launch, scheduler, supervise, supervisor_spawn
from unified_agent.dispatch.launch import (
    NodeRoutedLaunchBoundary,
    RecordingLaunchBoundary,
    RoutedLaunchBoundary,
    StubProcessSpawner,
)
from unified_agent.executor import agy_interactive as ai
from unified_agent.tools import delegate, pi_runner, tasks

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups and pipes are POSIX")


# --- 1. stall detection: Pi -------------------------------------------------

_FAKE_PI_SILENT = """
import json, sys, time
sys.stdin.readline()
print(json.dumps({"type": "message_update",
                  "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}}), flush=True)
time.sleep(60)
"""

_FAKE_PI_CHATTY = """
import json, sys, time
sys.stdin.readline()
for _ in range(8):
    print(json.dumps({"type": "message_update",
                      "assistantMessageEvent": {"type": "text_delta", "delta": "."}}), flush=True)
    time.sleep(0.1)
print(json.dumps({"type": "agent_end"}), flush=True)
sys.stdin.read()
"""


def _fake_pi(tmp_path, monkeypatch, source: str, stall_s: float) -> None:
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    script = tmp_path / "fake_pi.py"
    script.write_text(source)
    monkeypatch.setattr(pi_runner, "resolve_pi_executable", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(pi_runner.config, "executor_stall_timeout_s", lambda: stall_s)


@posix_only
def test_pi_silent_child_is_stopped_as_stalled(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, _FAKE_PI_SILENT, stall_s=0.5)

    started = time.monotonic()
    result = pi_runner.pi_run_task(prompt="go quiet", cwd="", timeout_s=60, poll_interval_s=0.05)

    assert time.monotonic() - started < 15
    assert result["status"] == "timeout"
    assert result["error"] == "Execution stalled: no output for 0.5 seconds"
    assert result["text"] == "hi"


@posix_only
def test_pi_child_that_keeps_emitting_events_is_not_stalled(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, _FAKE_PI_CHATTY, stall_s=0.5)

    result = pi_runner.pi_run_task(prompt="talk", cwd="", timeout_s=60, poll_interval_s=0.05)

    assert result["status"] == "success", result
    assert result["text"] == "." * 8


@posix_only
def test_pi_stall_detection_disabled_by_zero(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, _FAKE_PI_SILENT, stall_s=0)

    result = pi_runner.pi_run_task(prompt="go quiet", cwd="", timeout_s=2, poll_interval_s=0.05)

    assert result["status"] == "timeout"
    assert result["error"] == "Execution timed out after 2 seconds"


# --- 1. stall detection: agy-interactive -------------------------------------

_FAKE_AGY_SILENT = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    import sys, time
    sys.stdout.write(">\n"); sys.stdout.flush()
    sys.stdin.readline()
    sys.stdout.write("working on it\n"); sys.stdout.flush()
    time.sleep(60)
    """
).lstrip()


def test_agy_interactive_pexpect_silent_session_is_stopped_as_stalled(tmp_path, monkeypatch):
    pytest.importorskip("pexpect")
    fake = tmp_path / "fake-agy-silent"
    fake.write_text(_FAKE_AGY_SILENT, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))

    started = time.monotonic()
    res = ai.run_interactive_session(
        agy_bin=str(fake),
        prompt="do the work",
        cwd=str(tmp_path),
        env={**os.environ, "TERM": "xterm"},
        timeout_s=60,
        settings_path=tmp_path / "settings.json",
        stall_timeout_s=1.0,
    )

    assert time.monotonic() - started < 20
    assert res["status"] == "timeout", res
    assert res["error"] == "Execution stalled: no output for 1 seconds"
    assert "working on it" in res["text"]


def test_agy_interactive_stall_default_comes_from_executor_config(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_pexpect(**kwargs):
        seen.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(ai, "_driver_available", lambda: "pexpect")
    monkeypatch.setattr(ai, "_run_with_pexpect", fake_pexpect)
    monkeypatch.setattr(ai.config, "executor_stall_timeout_s", lambda: 42.0)
    ai.run_interactive_session(
        agy_bin="agy",
        prompt="p",
        cwd=str(tmp_path),
        env={},
        timeout_s=60,
        settings_path=tmp_path / "settings.json",
    )
    assert seen["stall_timeout_s"] == 42.0


def test_agy_interactive_tmux_static_pane_is_stopped_as_stalled(tmp_path, monkeypatch):
    panes = iter([">\n"])

    def capture(_socket, _session, _env):
        # After submission the TUI echoes the turn on the prompt line, then
        # goes silent: the pane stops changing.
        return next(panes, "> do the work\nworking on it")

    killed: list[str] = []
    monkeypatch.setattr(ai, "_tmux_new_session", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: 4321)
    monkeypatch.setattr(ai, "_kill_process_tree", lambda pgid: True)
    monkeypatch.setattr(ai, "_tmux_capture", capture)
    monkeypatch.setattr(ai, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: killed.append(socket))
    monkeypatch.setattr(ai.subprocess, "run", Mock())

    started = time.monotonic()
    res = ai._run_with_tmux(
        agy_bin="agy",
        prompt="do the work",
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=60,
        kill_check=None,
        allow_classes=ai.AGY_INTERACTIVE_ALLOW_CLASSES,
        progress_cb=None,
        model=None,
        stall_timeout_s=0.6,
    )

    assert time.monotonic() - started < 15
    assert res["status"] == "timeout", res
    assert res["error"] == "Execution stalled: no output for 0.6 seconds"
    assert killed


# --- 2. supervisor Popen-to-PID window ---------------------------------------


@pytest.fixture
def launched_run(isolated_env, tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    storage.init_db()
    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RoutedLaunchBoundary(spawner=StubProcessSpawner())
    )
    assert out["launched"]
    authority = {
        "data_home": str(config.data_home().resolve()),
        "policy_file": str(policy.resolve()),
        "policy_file_required": False,
        "agent_labels": ":".join(sorted(config.agent_labels())),
        "launch_caller": None,
    }
    return out["run_id"], authority


def _run_supervisor(tmp_path, monkeypatch, run_id, authority, argv) -> Path:
    payload = {
        "argv": argv,
        "cwd": str(tmp_path),
        "log_path": str(tmp_path / "child.log"),
        "run_id": run_id,
        "authority": authority,
    }
    handshake = tmp_path / "handshake"
    handshake.mkdir()
    pid_path = handshake / "pid"
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(pid_path))
    return pid_path


def _is_start_gate(pid: int) -> bool:
    """Whether the process at ``pid`` is still the dispatch start gate."""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        argv = proc_cmdline.read_bytes().split(b"\0")
        return b"-c" in argv and supervisor_spawn._GATE_SOURCE.encode() in argv
    args = subprocess.run(
        ["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout
    return "dispatch start gate" in args


@posix_only
def test_child_does_no_work_before_its_pid_is_recorded(launched_run, tmp_path, monkeypatch):
    """A child that would finish inside the Popen-to-PID window cannot: the
    work only starts once the pid is durable, so its clean exit is reported."""
    run_id, authority = launched_run
    marker = tmp_path / "work-done"
    _run_supervisor(
        tmp_path,
        monkeypatch,
        run_id,
        authority,
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
    )
    real_persist = launch._persist_spawn_pid
    seen_in_window: list[bool] = []

    def persist_after_window(rid, pid):
        # The window between Popen and the PID write. Popen returns only after
        # the child has exec'd, so the process at ``pid`` is now whatever was
        # launched: it must be the start gate, not the work, however slowly
        # either starts. The gate runs the work only once it is released.
        seen_in_window.append((_is_start_gate(pid), marker.exists()))
        return real_persist(rid, pid)

    monkeypatch.setattr(launch, "_persist_spawn_pid", persist_after_window)

    supervisor_spawn.main()

    assert seen_in_window == [(True, False)]
    assert marker.read_text() == "x"
    run = coordinator.get_run(run_id)
    assert run["state"] == "finished", run
    assert run["evidence_ref"].startswith("pid:")


@posix_only
def test_parent_giving_up_in_the_window_means_the_work_never_runs(
    launched_run, tmp_path, monkeypatch
):
    run_id, authority = launched_run
    marker = tmp_path / "work-done"
    pid_path = _run_supervisor(
        tmp_path,
        monkeypatch,
        run_id,
        authority,
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
    )
    real_persist = launch._persist_spawn_pid

    def parent_deadline_fires_in_window(rid, pid):
        assert supervise.abandon_handshake(pid_path) is True
        return real_persist(rid, pid)

    monkeypatch.setattr(launch, "_persist_spawn_pid", parent_deadline_fires_in_window)
    report = Mock()
    monkeypatch.setattr(supervise, "_report_exit_resilient", report)

    # main() reaps the never-released gate before it returns, so the CLI has
    # either run by now or never will.
    supervisor_spawn.main()

    assert not marker.exists()
    assert pid_path.read_text() == "abandoned"
    report.assert_not_called()


def test_parent_abandons_when_supervisor_never_publishes(tmp_path, monkeypatch):
    captured: dict = {}

    def popen(*_args, **kwargs):
        captured["pid_path"] = Path(kwargs["env"]["UA_SUP_PID_PATH"])
        return Mock()

    monkeypatch.setattr(supervise.subprocess, "Popen", popen)
    monkeypatch.setattr(supervise, "SUPERVISOR_HANDSHAKE_S", 0.2)
    with pytest.raises(RuntimeError, match="did not report child pid"):
        supervise.spawn_via_detached_supervisor(
            ["child"], cwd=".", env={}, log_path=tmp_path / "c.log", run_id="abandon"
        )
    # A late supervisor cannot publish after the parent gave up, so it never
    # releases the child's work.
    assert supervise.publish_handshake_pid(captured["pid_path"], 4242) is False
    assert not captured["pid_path"].parent.exists()


def test_supervisor_publishing_at_the_deadline_wins_over_abandonment(tmp_path, monkeypatch):
    supervisor = Mock()
    monkeypatch.setattr(supervise.subprocess, "Popen", lambda *_a, **_k: supervisor)
    monkeypatch.setattr(supervise, "SUPERVISOR_HANDSHAKE_S", 0.1)
    real_abandon = supervise.abandon_handshake

    def publish_then_abandon(pid_path):
        assert supervise.publish_handshake_pid(pid_path, 4242) is True
        return real_abandon(pid_path)

    monkeypatch.setattr(supervise, "abandon_handshake", publish_then_abandon)
    pid = supervise.spawn_via_detached_supervisor(
        ["child"], cwd=".", env={}, log_path=tmp_path / "c.log", run_id="race"
    )
    assert pid == 4242
    supervisor.kill.assert_not_called()


# --- 3. node queue duplicate receipts ----------------------------------------

NODE = "n1"
AGENT = "Desktop_Agent2"
ROOT = "/home/a/dev"


@pytest.fixture
def node_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
    monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
    storage.init_db()
    node_queue.register(
        NODE,
        identity=AGENT,
        facts={
            "os": {"system": "Linux"},
            "backends": ["codex"],
            "worktree_roots": [{"path": ROOT, "exists": True, "worktrees": 1}],
        },
    )
    return tmp_path


def _node_run() -> tuple[str, dict, str]:
    packed = delegate.delegate(
        task_spec=(
            "### Objective\nNode receipt fixture\n"
            "### Scope\n- src/unified_agent/node_queue.py\n"
            "### Definition of Done\n- receipts are idempotent\n"
        ),
        from_agent="Desktop_Agent5",
        to_agent=AGENT,
        callback="none",
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-23T00:00:00Z]")
    out = scheduler.scheduler_tick(
        force=True, launch=NodeRoutedLaunchBoundary(fallback=RecordingLaunchBoundary())
    )
    assert len(out["launched"]) == 1
    run_id = out["launched"][0]["run_id"]
    claimed = node_queue.claim_next(NODE, identity=AGENT)
    assert claimed is not None
    return run_id, packed, claimed["item_id"]


def _delegation_status(delegation_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT status FROM delegations WHERE id=?", (delegation_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


def _item_row(item_id: str) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM node_dispatch WHERE id=?", (item_id,)).fetchone()
        return {k: row[k] for k in row.keys()}
    finally:
        conn.close()


def test_duplicate_receipt_for_old_item_does_not_close_resumed_run(node_db):
    run_id, packed, first_item = _node_run()
    receipt = {"status": "failed", "reason": "tests red", "attempt": 1}
    first = node_queue.post_receipt(NODE, first_item, identity=AGENT, receipt=receipt)
    assert first["duplicate"] is False
    assert coordinator.get_run(run_id)["state"] == "failed"

    resumed = coordinator.resume_run(run_id, actor="Operator")
    assert resumed["ok"] is True, resumed
    run = coordinator.get_run(run_id)
    assert run["state"] == "launched"
    assert run["evidence_ref"] != f"node:{NODE}:{first_item}"
    old_item = _item_row(first_item)

    dup = node_queue.post_receipt(NODE, first_item, identity=AGENT, receipt=receipt)

    assert dup["duplicate"] is True
    assert dup["run_outcome"]["ok"] is False
    assert dup["run_outcome"]["reason"] == "run_rebound"
    after = coordinator.get_run(run_id)
    assert after["state"] == "launched"
    assert after["evidence_ref"] == run["evidence_ref"]
    assert _delegation_status(packed["delegation_id"]) == "in_flight"
    assert _item_row(first_item) == old_item


def test_duplicate_receipt_does_not_confirm_cancellation_of_a_rebound_run(node_db):
    run_id, _packed, first_item = _node_run()
    receipt = {"status": "failed", "attempt": 1}
    node_queue.post_receipt(NODE, first_item, identity=AGENT, receipt=receipt)
    assert coordinator.resume_run(run_id, actor="Operator")["ok"] is True
    assert node_queue.claim_next(NODE, identity=AGENT) is not None
    cancel = coordinator.cancel_run(run_id, actor="Operator", reason="stop")
    assert cancel["cessation"] != "confirmed"
    before = coordinator.get_run(run_id)

    dup = node_queue.post_receipt(NODE, first_item, identity=AGENT, receipt=receipt)

    assert dup["run_outcome"]["reason"] == "run_rebound"
    after = coordinator.get_run(run_id)
    assert after["termination_outcome"] == before["termination_outcome"]
    assert after["termination_outcome"] != "confirmed"


def test_duplicate_receipt_does_not_retransition_a_reclaimed_run(node_db, monkeypatch):
    run_id, packed, item = _node_run()
    # The first receipt is stored but its run close fails, so the run stays
    # live until the lease reconciler hands it back as retryable.
    real_close = node_queue._close_linked_run
    monkeypatch.setattr(
        node_queue, "_close_linked_run", Mock(return_value={"ok": False, "reason": "boom"})
    )
    receipt = {"status": "failed", "attempt": 1}
    node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)
    monkeypatch.setattr(node_queue, "_close_linked_run", real_close)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()
    coordinator.reclaim_expired_leases()
    assert coordinator.get_run(run_id)["state"] == "retryable"
    assert _delegation_status(packed["delegation_id"]) == "pending"

    dup = node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)

    assert dup["duplicate"] is True
    assert dup["run_outcome"]["ok"] is False
    assert coordinator.get_run(run_id)["state"] == "retryable"
    assert _delegation_status(packed["delegation_id"]) == "pending"


def test_duplicate_receipt_still_retries_a_failed_close_of_its_own_run(node_db, monkeypatch):
    run_id, packed, item = _node_run()
    real_close = node_queue._close_linked_run
    monkeypatch.setattr(
        node_queue, "_close_linked_run", Mock(return_value={"ok": False, "reason": "boom"})
    )
    receipt = {"status": "finished", "attempt": 1}
    node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)
    monkeypatch.setattr(node_queue, "_close_linked_run", real_close)
    assert coordinator.get_run(run_id)["state"] == "running"

    dup = node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)
    again = node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)

    assert dup["run_outcome"]["ok"] is True
    assert again["run_outcome"]["reason"] == "already_terminal"
    assert coordinator.get_run(run_id)["state"] == "finished"
    assert _delegation_status(packed["delegation_id"]) == "completed"


# --- review round 1 (PR #1230) -----------------------------------------------


def _pid_gone(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.01)
    return False


def _fake_pi_leader_exits(pidfile: Path, descendant: str) -> str:
    return f"""
import json, os, subprocess, sys
sys.stdin.readline()
print(json.dumps({{"type": "message_update",
                  "assistantMessageEvent": {{"type": "text_delta", "delta": "hi"}}}}), flush=True)
child = subprocess.Popen([sys.executable, "-c", {descendant!r}])
open({str(pidfile)!r}, "w").write(str(child.pid))
os._exit(0)
"""


@posix_only
def test_pi_stall_kills_descendant_holding_stdout_after_leader_exits(tmp_path, monkeypatch):
    pidfile = tmp_path / "descendant.pid"
    source = _fake_pi_leader_exits(pidfile, "import time; time.sleep(30)")
    _fake_pi(tmp_path, monkeypatch, source, stall_s=0.5)

    started = time.monotonic()
    result = pi_runner.pi_run_task(
        prompt="leave a child", cwd="", timeout_s=20, poll_interval_s=0.05
    )

    assert time.monotonic() - started < 8
    assert result["status"] == "timeout", result
    assert result["error"] == "Execution stalled: no output for 0.5 seconds"
    assert _pid_gone(int(pidfile.read_text()))


_FINITE_CHATTY = (
    "import time\n"
    "for _ in range(60):\n"
    "    print('{}', flush=True)\n"
    "    time.sleep(0.1)\n"
    "open(MARKER, 'w').write('done')\n"
)


@posix_only
@pytest.mark.parametrize("stall_s", [0, 1.0])
def test_pi_descendant_still_writing_after_pi_exits_is_left_to_finish(
    tmp_path, monkeypatch, stall_s
):
    """Codex round 2: a background job that keeps producing output after Pi
    exits is progress. It runs to completion within the stall and wall-clock
    budget; it is never cut short and then reported as success."""
    pidfile = tmp_path / "descendant.pid"
    marker = tmp_path / "marker"
    descendant = _FINITE_CHATTY.replace("MARKER", repr(str(marker)))
    _fake_pi(tmp_path, monkeypatch, _fake_pi_leader_exits(pidfile, descendant), stall_s=stall_s)

    started = time.monotonic()
    result = pi_runner.pi_run_task(
        prompt="background job", cwd="", timeout_s=30, poll_interval_s=0.05
    )

    # Outlives the 5 s post-exit kill the first fix round added.
    assert time.monotonic() - started >= 5.5
    assert result["status"] == "success", result
    assert result["exit_code"] == 0
    assert marker.read_text() == "done"


@posix_only
def test_pi_descendant_that_goes_silent_after_pi_exits_is_a_stall_not_a_success(
    tmp_path, monkeypatch
):
    pidfile = tmp_path / "descendant.pid"
    marker = tmp_path / "marker"
    descendant = (
        "import time\n"
        "for _ in range(5):\n"
        "    print('{}', flush=True)\n"
        "    time.sleep(0.1)\n"
        "time.sleep(30)\n"
        f"open({str(marker)!r}, 'w').write('done')\n"
    )
    _fake_pi(tmp_path, monkeypatch, _fake_pi_leader_exits(pidfile, descendant), stall_s=0.5)

    result = pi_runner.pi_run_task(
        prompt="background job", cwd="", timeout_s=30, poll_interval_s=0.05
    )

    assert result["status"] == "timeout", result
    assert result["error"] == "Execution stalled: no output for 0.5 seconds"
    assert _pid_gone(int(pidfile.read_text()))
    assert not marker.exists()


_FAKE_AGY_WITH_TOOL = r"""#!/usr/bin/env python3
import subprocess, sys, time
sys.stdout.write(">\n"); sys.stdout.flush()
sys.stdin.readline()
tool = (
    "import signal, time\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "while True:\n"
    "    open(%r, 'a').write('x')\n"
    "    time.sleep(0.05)\n"
)
child = subprocess.Popen(
    [sys.executable, "-c", tool % HEARTBEAT],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
open(PIDFILE, "w").write(str(child.pid))
sys.stdout.write("working\n"); sys.stdout.flush()
time.sleep(60)
"""


@posix_only
def test_agy_interactive_stall_kills_tool_descendants(tmp_path, monkeypatch):
    pytest.importorskip("pexpect")
    heartbeat = tmp_path / "heartbeat"
    pidfile = tmp_path / "tool.pid"
    fake = tmp_path / "fake-agy-tool"
    fake.write_text(
        _FAKE_AGY_WITH_TOOL.replace("HEARTBEAT", repr(str(heartbeat))).replace(
            "PIDFILE", repr(str(pidfile))
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(ai, "_TREE_KILL_GRACE_S", 0.5)

    res = ai.run_interactive_session(
        agy_bin=str(fake),
        prompt="do the work",
        cwd=str(tmp_path),
        env={**os.environ, "TERM": "xterm"},
        timeout_s=60,
        settings_path=tmp_path / "settings.json",
        stall_timeout_s=1.0,
    )

    assert res["status"] == "timeout", res
    tool_pid = int(pidfile.read_text())
    try:
        # The tool is the heartbeat's only writer; once it is gone it has stopped.
        assert _pid_gone(tool_pid, 2.0)
    finally:
        if not _pid_gone(tool_pid, 0.1):
            os.kill(tool_pid, 9)


def test_agy_interactive_tmux_stall_kills_the_pane_process_group(tmp_path, monkeypatch):
    panes = iter([">\n"])
    order: list[tuple[str, object]] = []
    monkeypatch.setattr(ai, "_tmux_new_session", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_tmux_capture", lambda *a: next(panes, "> do it\nworking"))
    monkeypatch.setattr(ai, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: 4321)
    monkeypatch.setattr(ai, "_kill_process_tree", lambda pgid: order.append(("tree", pgid)))
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: order.append(("server", 0)))
    monkeypatch.setattr(ai.subprocess, "run", Mock())

    res = ai._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=60,
        kill_check=None,
        allow_classes=ai.AGY_INTERACTIVE_ALLOW_CLASSES,
        progress_cb=None,
        model=None,
        stall_timeout_s=0.5,
    )

    assert res["status"] == "timeout"
    assert order[0] == ("tree", 4321)


def test_duplicate_receipt_check_and_close_are_atomic_against_resume(node_db, monkeypatch):
    """Codex round 1: a duplicate paused after its binding check, while another
    duplicate closes the run and the operator resumes it onto a new item, must
    not then fail the resumed run."""
    run_id, packed, item = _node_run()
    old_ref = f"node:{NODE}:{item}"
    real_close = node_queue._close_linked_run
    monkeypatch.setattr(
        node_queue, "_close_linked_run", Mock(return_value={"ok": False, "reason": "boom"})
    )
    receipt = {"status": "failed", "attempt": 1}
    node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)
    monkeypatch.setattr(node_queue, "_close_linked_run", real_close)
    assert coordinator.get_run(run_id)["state"] == "running"

    real_bound = node_queue._run_bound_to_item
    interleaved: list[str] = []

    def bound_then_interleave(*args, **kwargs):
        verdict = real_bound(*args, **kwargs)
        if not interleaved:
            interleaved.append("x")
            # Duplicate B lands in the gap and closes the old attempt...
            assert scheduler.record_run_outcome(run_id, "failed", evidence_ref=old_ref)["ok"]
            # ...and the operator resumes the run onto a new node item.
            assert coordinator.resume_run(run_id, actor="Operator")["ok"] is True
        return verdict

    monkeypatch.setattr(node_queue, "_run_bound_to_item", bound_then_interleave)

    dup = node_queue.post_receipt(NODE, item, identity=AGENT, receipt=receipt)

    assert interleaved
    assert dup["run_outcome"]["ok"] is False
    after = coordinator.get_run(run_id)
    resumed_ref = after["evidence_ref"]
    assert after["state"] == "launched", after
    assert resumed_ref != old_ref and resumed_ref.startswith(f"node:{NODE}:")
    assert _delegation_status(packed["delegation_id"]) == "in_flight"


def test_cancellation_confirmation_is_fenced_to_the_current_item(node_db, monkeypatch):
    run_id, packed, item = _node_run()
    cancel = coordinator.cancel_run(run_id, actor="Operator", reason="stop")
    assert cancel["cessation"] != "confirmed"

    real_bound = node_queue._run_bound_to_item
    interleaved: list[str] = []

    def bound_then_interleave(*args, **kwargs):
        verdict = real_bound(*args, **kwargs)
        if not interleaved:
            interleaved.append("x")
            run = coordinator.get_run(run_id)
            coordinator.finalize_cancellation(
                run,
                actor="Operator",
                reason="confirmed by operator",
                termination="operator",
                cessation="confirmed",
            )
            assert coordinator.resume_run(run_id, actor="Operator")["ok"] is True
        return verdict

    monkeypatch.setattr(node_queue, "_run_bound_to_item", bound_then_interleave)

    node_queue.post_receipt(NODE, item, identity=AGENT, receipt={"status": "failed", "attempt": 1})

    assert interleaved
    after = coordinator.get_run(run_id)
    assert after["state"] == "launched", after
    assert after["termination_outcome"] is None
    assert _delegation_status(packed["delegation_id"]) == "in_flight"


def _patch_killpg(monkeypatch, outcomes):
    """``os.killpg`` stand-in: pops one outcome per call (an exception class or None)."""
    calls: list[tuple[int, int]] = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome is not None:
            raise outcome()

    monkeypatch.setattr(ai.os, "killpg", killpg)
    monkeypatch.setattr(ai.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(ai, "_TREE_KILL_GRACE_S", 0.1)
    monkeypatch.setattr(ai, "_TREE_KILL_CONFIRM_S", 0.1)
    return calls


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_eperm_never_confirms_the_group_is_gone(monkeypatch, platform):
    """Codex round 2: EPERM means a member exists but cannot be signalled
    (killpg(2)); only ESRCH proves the group is gone."""
    import signal

    monkeypatch.setattr(sys, "platform", platform)
    calls = _patch_killpg(monkeypatch, [PermissionError])

    assert ai._pgid_alive(12345) is True
    assert ai._kill_process_tree(12345) is False
    sent = {sig for _pgid, sig in calls}
    assert signal.SIGTERM in sent and signal.SIGKILL in sent


def test_esrch_after_eperm_confirms_the_group_is_gone(monkeypatch):
    import signal

    monkeypatch.setattr(sys, "platform", "darwin")
    # SIGTERM delivered, first probe EPERM (still present), then ESRCH.
    calls = _patch_killpg(monkeypatch, [None, PermissionError, ProcessLookupError])

    assert ai._kill_process_tree(12345) is True
    assert all(sig != signal.SIGKILL for _pgid, sig in calls)


@pytest.mark.parametrize("pgid", [0, 1, -5])
def test_kill_process_tree_refuses_init_and_nonpositive_groups(monkeypatch, pgid):
    calls = _patch_killpg(monkeypatch, [None])
    assert ai._kill_process_tree(pgid) is False
    assert calls == []


def test_unconfirmed_child_cleanup_is_reported_with_an_actionable_note(monkeypatch):
    _patch_killpg(monkeypatch, [PermissionError])
    child = Mock(pid=12345)
    child.isalive.return_value = True

    note = ai._terminate_child(child)

    assert note is not None
    assert "12345" in note and "ps -g 12345" in note
    child.close.assert_called()


def test_tmux_stall_with_unconfirmed_cleanup_says_so(tmp_path, monkeypatch):
    panes = iter([">\n"])
    monkeypatch.setattr(ai, "_tmux_new_session", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_tmux_capture", lambda *a: next(panes, "> do it\nworking"))
    monkeypatch.setattr(ai, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: 4321)
    monkeypatch.setattr(ai, "_kill_process_tree", lambda pgid: False)
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: None)
    monkeypatch.setattr(ai.subprocess, "run", Mock())

    res = ai._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=60,
        kill_check=None,
        allow_classes=ai.AGY_INTERACTIVE_ALLOW_CLASSES,
        progress_cb=None,
        model=None,
        stall_timeout_s=0.5,
    )

    assert res["status"] == "timeout"
    assert res["cleanup_confirmed"] is False
    assert res["error"].startswith("Execution stalled: no output for 0.5 seconds")
    assert "4321" in res["error"] and "not confirmed gone" in res["error"]


# --- review round 3 (PR #1230) -----------------------------------------------


@posix_only
def test_pi_descendant_still_writing_after_agent_end_is_not_stalled(tmp_path, monkeypatch):
    """Codex round 3: output read while draining after ``agent_end`` is
    progress too; the stall clock must not freeze at the protocol's end."""
    marker = tmp_path / "marker"
    writer = (
        # 1.2 s of output after agent_end, 0.05 s apart, against a 0.5 s stall.
        "import time\n"
        "for _ in range(24):\n"
        "    print('{}', flush=True)\n"
        "    time.sleep(0.05)\n"
        f"open({str(marker)!r}, 'w').write('done')\n"
    )
    source = f"""
import json, subprocess, sys
sys.stdin.readline()
subprocess.Popen([sys.executable, "-c", {writer!r}])
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.stdin.read()
"""
    _fake_pi(tmp_path, monkeypatch, source, stall_s=0.5)

    result = pi_runner.pi_run_task(prompt="build", cwd="", timeout_s=30, poll_interval_s=0.05)

    assert result["status"] == "success", result
    assert marker.read_text() == "done"


@posix_only
def test_pi_descendant_silent_after_agent_end_is_still_a_stall(tmp_path, monkeypatch):
    pidfile = tmp_path / "descendant.pid"
    source = f"""
import json, subprocess, sys
sys.stdin.readline()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
open({str(pidfile)!r}, "w").write(str(child.pid))
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.stdin.read()
"""
    _fake_pi(tmp_path, monkeypatch, source, stall_s=0.5)

    result = pi_runner.pi_run_task(prompt="build", cwd="", timeout_s=30, poll_interval_s=0.05)

    assert result["status"] == "timeout", result
    assert result["error"] == "Execution stalled: no output for 0.5 seconds"
    assert _pid_gone(int(pidfile.read_text()))


def test_tmux_stop_with_failed_pane_lookup_and_no_recorded_group_is_unconfirmed(monkeypatch):
    killed_server: list[str] = []
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: None)
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: killed_server.append(socket))

    note = ai._tmux_stop("sock", "sess", {})

    assert note is not None and "unknown" in note
    assert killed_server == ["sock"]


def test_tmux_stop_falls_back_to_the_recorded_pane_group(monkeypatch):
    trees: list[int] = []
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: None)
    monkeypatch.setattr(ai, "_kill_process_tree", lambda pgid: trees.append(pgid) or True)
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: None)

    assert ai._tmux_stop("sock", "sess", {}, known_pgid=4321) is None
    assert trees == [4321]


def test_tmux_run_records_the_pane_group_at_start(tmp_path, monkeypatch):
    """The pane pid is captured when the session starts, so a lookup that
    fails at stop time still kills and confirms the owned group."""
    panes = iter([">\n"])
    lookups = iter([4321])
    trees: list[int] = []
    monkeypatch.setattr(ai, "_tmux_new_session", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: next(lookups, None))
    monkeypatch.setattr(ai, "_tmux_capture", lambda *a: next(panes, "> do it\nworking"))
    monkeypatch.setattr(ai, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(ai, "_kill_process_tree", lambda pgid: trees.append(pgid) or True)
    monkeypatch.setattr(ai, "_tmux_kill_server", lambda socket, env: None)
    monkeypatch.setattr(ai.subprocess, "run", Mock())

    res = ai._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=60,
        kill_check=None,
        allow_classes=ai.AGY_INTERACTIVE_ALLOW_CLASSES,
        progress_cb=None,
        model=None,
        stall_timeout_s=0.5,
    )

    assert res["status"] == "timeout"
    assert res["cleanup_confirmed"] is True
    assert trees == [4321]


_HUP_RESISTANT_TOOL = (
    "import os, signal, subprocess, sys, time\n"
    "tool = (\n"
    "    'import signal, time\\n'\n"
    "    'signal.signal(signal.SIGHUP, signal.SIG_IGN)\\n'\n"
    "    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
    "    'while True:\\n'\n"
    '    \'    open(%r, "a").write("x")\\n\'\n'
    "    '    time.sleep(0.05)\\n'\n"
    ") % sys.argv[1]\n"
    "child = subprocess.Popen([sys.executable, '-c', tool])\n"
    "open(sys.argv[2], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


@posix_only
@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
@pytest.mark.parametrize("recorded", [True, False])
def test_real_tmux_pane_lookup_failure_with_hup_resistant_tool(tmp_path, monkeypatch, recorded):
    heartbeat = tmp_path / "heartbeat"
    pidfile = tmp_path / "tool.pid"
    script = tmp_path / "pane.py"
    script.write_text(_HUP_RESISTANT_TOOL)
    monkeypatch.setattr(ai, "_TREE_KILL_GRACE_S", 0.3)
    work = Path(tempfile.mkdtemp(prefix="ua-1950-", dir="/tmp"))
    socket = str(work / "s")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    cmd = " ".join(
        ai.shlex_quote(a) for a in (sys.executable, str(script), str(heartbeat), str(pidfile))
    )
    ai._tmux_new_session(socket, "t", str(tmp_path), cmd, env)
    try:
        # The parent publishes the PID before the tool installs its signal
        # handlers. A heartbeat proves the resistant descendant is running.
        ready_deadline = time.monotonic() + 10
        while time.monotonic() < ready_deadline:
            if all(p.exists() and p.stat().st_size > 0 for p in (pidfile, heartbeat)):
                break
            time.sleep(0.02)
        assert pidfile.exists() and pidfile.stat().st_size > 0
        assert heartbeat.exists() and heartbeat.stat().st_size > 0
        tool_pid = int(pidfile.read_text())
        pane_pid = ai._tmux_pane_pid(socket, "t", env)
        assert pane_pid is not None
        monkeypatch.setattr(ai, "_tmux_pane_pid", lambda *a: None)

        note = ai._tmux_stop(socket, "t", env, known_pgid=pane_pid if recorded else None)

        if recorded:
            assert note is None
            # The tool is the heartbeat's only writer; once it is gone it has stopped.
            assert _pid_gone(tool_pid, 3.0)
        else:
            # Ownership cannot be established: never reported as confirmed.
            assert note is not None
    finally:
        if (
            pidfile.exists()
            and pidfile.read_text()
            and not _pid_gone(int(pidfile.read_text()), 0.1)
        ):
            os.kill(int(pidfile.read_text()), 9)
        ai._tmux_kill_server(socket, env)
        shutil.rmtree(work, ignore_errors=True)


@posix_only
@pytest.mark.parametrize("exit_code", [0, 7])
def test_pi_agent_end_drains_full_stderr_while_stdout_progresses(tmp_path, monkeypatch, exit_code):
    """Shutdown diagnostics must not block the post-agent_end progress drain."""
    marker = tmp_path / "writer-done"
    writer = (
        "import time\n"
        "for _ in range(20):\n"
        "    print('{}', flush=True)\n"
        "    time.sleep(0.05)\n"
        f"open({str(marker)!r}, 'w').write('done')\n"
    )
    diagnostics = "shutdown diagnostic\n" * 16384
    source = f"""
import json, subprocess, sys
sys.stdin.readline()
subprocess.Popen([sys.executable, "-c", {writer!r}])
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.stdin.read()
sys.stderr.write({diagnostics!r})
sys.stderr.flush()
sys.exit({exit_code})
"""
    _fake_pi(tmp_path, monkeypatch, source, stall_s=0.5)

    result = pi_runner.pi_run_task(prompt="shutdown", cwd="", timeout_s=10, poll_interval_s=0.05)

    assert result["status"] == ("success" if exit_code == 0 else "error"), result["status"]
    assert result["exit_code"] == exit_code
    assert result["error"] == (None if exit_code == 0 else diagnostics.strip())
    assert marker.read_text() == "done"


@posix_only
@pytest.mark.parametrize("agent_end_first", [False, True])
def test_pi_partial_stdout_bytes_keep_progress_alive(tmp_path, monkeypatch, agent_end_first):
    source = f"""
import json, os, subprocess, sys
sys.stdin.readline()
if {agent_end_first!r}:
    print(json.dumps({{"type": "agent_end"}}), flush=True)
    subprocess.Popen([sys.executable, "-c", "import os,time; [(os.write(1,b'.'),time.sleep(.05)) for _ in range(20)]"])
    os._exit(0)
else:
    import time
    payload = json.dumps({{"type":"message_update","update":{{"kind":"text_delta","text":"ok"}}}}).encode() + b'\\n'
    for offset in range(0, len(payload), 4):
        os.write(1, payload[offset:offset+4])
        time.sleep(.05)
    print(json.dumps({{"type":"agent_end"}}), flush=True)
    sys.stdin.read()
"""
    _fake_pi(tmp_path, monkeypatch, source, stall_s=0.3)
    result = pi_runner.pi_run_task(prompt="partial output", timeout_s=5, poll_interval_s=0.025)
    assert result["status"] == "success", result
    if not agent_end_first:
        assert result["text"] == "ok"


@posix_only
def test_pi_timeout_propagates_unconfirmed_group_cleanup(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, _FAKE_PI_SILENT, stall_s=0.15)
    original = pi_runner._kill_process_group

    def unconfirmed(proc, **kwargs):
        original(proc, **kwargs)  # Stop the fixture; simulate an inaccessible survivor.
        return False

    monkeypatch.setattr(pi_runner, "_kill_process_group", unconfirmed)
    result = pi_runner.pi_run_task(prompt="quiet", timeout_s=3, poll_interval_s=0.025)
    assert result.get("cleanup_confirmed") is False, result
    assert "process group" in result["error"]
    assert "before retrying" in result["error"]


@pytest.mark.parametrize("gone", [False, True])
def test_pi_cleanup_confirms_group_cessation_not_just_leader_exit(monkeypatch, gone):
    proc = Mock(pid=12345)
    proc.poll.return_value = 0

    def killpg(pgid, sig):
        assert pgid == 12345
        if gone:
            raise ProcessLookupError()
        raise PermissionError()

    monkeypatch.setattr(pi_runner.os, "killpg", killpg)
    monkeypatch.setattr(pi_runner, "_GROUP_KILL_CONFIRM_S", 0, raising=False)
    assert pi_runner._kill_process_group(proc) is gone
    if not gone:
        proc.kill.assert_called()


@pytest.mark.parametrize(
    "route", ["initial_timeout", "initial_eof", "pty_error", "deny", "teardown", "eof"]
)
def test_pexpect_every_return_preserves_unconfirmed_cleanup(tmp_path, monkeypatch, route):
    import pexpect

    child = Mock(pid=12345, before="", after="", exitstatus=0)
    child.isalive.return_value = False  # Leader exit does not prove descendant exit.
    steps = iter([2] if route == "initial_timeout" else [1] if route == "initial_eof" else [0, 1])

    def expect(*args, **kwargs):
        idx = next(steps)
        if child.send.called:
            if route == "pty_error":
                raise pexpect.ExceptionPexpect("fixture read failed")
            if route == "deny":
                child.before = "Allow this command?\n`rm -rf /tmp/fixture`\n1. Yes\n3. No\n"
                return 2
            if route == "teardown":
                child.before = "fixture teardown"
        return idx

    child.expect.side_effect = expect
    monkeypatch.setattr(pexpect, "spawn", lambda *a, **kw: child)
    cleanup = Mock(return_value="process group 12345 unconfirmed; stop it before retrying")
    monkeypatch.setattr(ai, "_terminate_child", cleanup)
    if route == "teardown":
        monkeypatch.setattr(
            ai, "detect_agy_teardown", lambda text: "fixture teardown" if text else None
        )
    result = ai._run_with_pexpect(
        agy_bin="fixture",
        prompt="go",
        cwd=str(tmp_path),
        env={},
        timeout_s=2,
        kill_check=None,
        allow_classes=(),
        progress_cb=None,
        model=None,
    )
    assert result.get("cleanup_confirmed") is False, result
    assert "process group 12345" in result["error"]
    cleanup.assert_called_once_with(child)


def test_agy_pexpect_uses_poll_so_a_busy_process_can_read_its_pty(tmp_path, monkeypatch):
    """pexpect's default select() raises once the pty's fd passes 1023 (task #2352)."""
    import pexpect

    spawn = Mock(side_effect=pexpect.ExceptionPexpect("fixture stops here"))
    monkeypatch.setattr(pexpect, "spawn", spawn)
    with pytest.raises(pexpect.ExceptionPexpect, match="fixture stops here"):
        ai._run_with_pexpect(
            agy_bin="fixture",
            prompt="go",
            cwd=str(tmp_path),
            env={},
            timeout_s=2,
            kill_check=None,
            allow_classes=(),
            progress_cb=None,
            model=None,
        )
    assert spawn.call_args.kwargs["use_poll"] is True


@posix_only
def test_pi_unconfirmed_survivor_cannot_hold_the_stdout_reader(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, _FAKE_PI_SILENT, stall_s=0.15)
    original = pi_runner._kill_process_group
    survivors = []

    def cannot_stop(proc, **kwargs):
        survivors.append(proc)
        return False

    monkeypatch.setattr(pi_runner, "_kill_process_group", cannot_stop)
    started = time.monotonic()
    try:
        result = pi_runner.pi_run_task(prompt="quiet", timeout_s=3, poll_interval_s=0.025)
        assert time.monotonic() - started < 2
        assert result["status"] == "timeout"
        assert result["cleanup_confirmed"] is False
        assert survivors[0].poll() is None  # The live fixture really retained stdout.
    finally:
        for proc in survivors:
            original(proc)


@pytest.mark.parametrize("pid", [-1, 0, 1])
def test_pi_cleanup_refuses_init_and_nonpositive_groups(monkeypatch, pid):
    proc = Mock(pid=pid)
    signal_group = Mock()
    monkeypatch.setattr(pi_runner.os, "killpg", signal_group)
    assert pi_runner._kill_process_group(proc) is False
    signal_group.assert_not_called()
    proc.kill.assert_not_called()


def test_pi_stdout_reads_a_pipe_numbered_past_the_select_limit():
    """A long-lived process can hold more fds than select() can name."""
    number = 2048
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard <= number:
        pytest.skip("the hard fd limit is below the test's pipe number")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, number + 1), hard))
    read_end, write_end = os.pipe()
    try:
        os.dup2(read_end, number)
        os.close(read_end)
        os.write(write_end, b'{"type":"agent_end"}\n')
        os.close(write_end)
        with open(number, encoding="utf-8") as stream:
            lines = list(pi_runner._stdout_lines(stream, lambda: None, lambda: False))
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert lines == ['{"type":"agent_end"}\n']
