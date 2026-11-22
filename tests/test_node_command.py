"""Supervised long commands on a ua-node (task #2413).

The acceptance run is real: a NodeAgent claims a command item from the
in-process hub and runs an actual subprocess through CommandBackend, while the
session that started it is ended and a second session adopts it.
"""

# The `hub` fixture is imported from test_node, so every test parameter redefines it.
# ruff: noqa: F811

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_node import NODE, _facts, hub  # noqa: F401 - hub is a fixture

from unified_agent import config, node_command, node_queue, registry, storage
from unified_agent.messaging import delivery
from unified_agent.node_registry import NodeError
from unified_agent.tools import messages as message_tools
from unified_agent.tools import registry as registry_tools

OWNER = "Desktop_Agent1"
PY = sys.executable


def _command_node(hub, tmp_path, monkeypatch, allow=(PY,)):
    import random

    from test_node import _transport

    from unified_agent import node_agent

    monkeypatch.setenv("UA_NODE_COMMANDS", ",".join(allow))
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    agent = node_agent.NodeAgent(
        node=NODE,
        transport=_transport(hub),
        roots=[root],
        backends=("command",),
        backend_factory=lambda name, agent_label: pytest.fail("a command needs no factory"),
        facts_fn=lambda: {**_facts(root), "backends": ["command"]},
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    assert agent.run_once() == "registered"
    return agent, root


def _start(root, argv, *, owner_session=None, **kw):
    return node_queue.enqueue(
        NODE,
        prompt=node_command.describe(argv),
        cwd=str(root / "repo"),
        backend="command",
        created_by=OWNER,
        command={"argv": argv, "owner_agent": OWNER, "owner_session": owner_session},
        **kw,
    )["item_id"]


def _session(external):
    return registry_tools.session_start(
        kind="claude_code", agent=OWNER, external_session_id=external
    )["session_id"]


def _owner_messages():
    rows = message_tools.message_list(to_agent=OWNER, from_agent=delivery.NODE_NOTICE_SENDER)
    return rows["messages"]


def _node_events(kind):
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
        ).fetchall()
    finally:
        conn.close()
    return [p for p in (json.loads(r["payload"]) for r in rows) if p["kind"] == kind]


def test_a_command_is_an_argv_list_and_only_a_command_item_carries_one(hub, tmp_path, monkeypatch):
    _, root = _command_node(hub, tmp_path, monkeypatch)
    with pytest.raises(NodeError, match="never a shell string"):
        _start(root, "pytest -q")
    with pytest.raises(NodeError, match="go together"):
        node_queue.enqueue(
            NODE, prompt="x", cwd=str(root / "repo"), backend="command", created_by=OWNER
        )
    with pytest.raises(NodeError, match="go together"):
        node_queue.enqueue(
            NODE,
            prompt="x",
            cwd=str(root / "repo"),
            backend="codex",
            created_by=OWNER,
            command={"argv": [PY], "owner_agent": OWNER},
        )


def test_the_command_outlives_its_session_and_the_owner_hears_once(hub, tmp_path, monkeypatch):
    """Acceptance: end the launching session mid-job; the job completes, a later
    session adopts it, and the owner label gets exactly one message."""
    agent, root = _command_node(hub, tmp_path, monkeypatch)
    first, second = _session("launcher"), _session("later")
    argv = [PY, "-c", "import time; time.sleep(1.5); print('built' + ' ok')"]
    item_id = _start(root, argv, owner_session=first)

    def mid_job():
        time.sleep(0.4)
        registry.end_session(first, status="abandoned")  # the launching session dies
        node_command.adopt(item_id, caller=OWNER, session_id=second)

    side = threading.Thread(target=mid_job)
    side.start()
    assert agent.run_once() == "ran"
    side.join()

    state = node_command.status(item_id, caller=OWNER)
    assert state["status"] == "finished" and state["exit_code"] == 0
    assert state["owner_session"] == second and state["attempt"] == 1
    assert "built ok" in (tmp_path / "sessions" / item_id / "command.log").read_text()
    [message] = _owner_messages()
    assert message["to_session"] is None  # label mail: no dead session can strand it
    assert item_id in message["body"] and "exit code 0" in message["body"]
    assert "built ok" not in message["body"]  # a pointer, never the output
    assert message["message_id"] == node_command.notice_id(item_id, 1, "finished")
    # A retried delivery is a duplicate, never a second message.
    node_command.deliver_notices()
    assert len(_owner_messages()) == 1
    assert node_command.unsettled() == {"overdue": [], "unsent": []}


def test_a_chatty_command_log_is_capped_and_marked_truncated(tmp_path, monkeypatch):
    """Task #2552: the child wrote straight to a file fd, so nothing bounded it."""
    from unified_agent.executor import backends

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "4096")
    log = tmp_path / "command.log"
    chatter = [PY, "-c", "import sys\nfor _ in range(2000): sys.stdout.write('x' * 100 + '\\n')"]
    result = backends.CommandBackend(chatter, str(log)).run(cwd=str(tmp_path), timeout_s=60)
    assert result["status"] == "success" and result["truncated"] is True
    body = log.read_bytes()
    assert len(body) < 4096 + 200  # the cap plus the one marker line
    assert b"output truncated at 4096 bytes" in body
    # A quiet command is untouched and not marked.
    quiet = tmp_path / "quiet.log"
    small = backends.CommandBackend([PY, "-c", "print('ok')"], str(quiet)).run(
        cwd=str(tmp_path), timeout_s=60
    )
    assert small["truncated"] is False and quiet.read_text() == "ok\n"


def test_the_cap_counts_output_already_in_the_log(tmp_path, monkeypatch):
    from unified_agent.executor import backends

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "300")
    log = tmp_path / "command.log"
    log.write_bytes(b"y" * 280)
    result = backends.CommandBackend([PY, "-c", "print('z' * 100)"], str(log)).run(
        cwd=str(tmp_path), timeout_s=60
    )
    assert result["truncated"] is True
    assert log.stat().st_size < 300 + 200


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_command_backend_log_is_visible_before_command_exits(tmp_path):
    from unified_agent.executor.backends import CommandBackend

    log = tmp_path / "live.log"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    code = (
        "from pathlib import Path; import time; "
        "print('live-output', flush=True); "
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    result = {}
    runner = threading.Thread(
        target=lambda: result.update(
            CommandBackend([PY, "-c", code], str(log), poll_interval_s=0.05).run(
                cwd=str(tmp_path), timeout_s=10
            )
        )
    )
    runner.start()
    try:
        assert _wait_for(ready.exists), "command did not start"
        assert runner.is_alive()
        assert _wait_for(lambda: b"live-output" in log.read_bytes(), timeout=1), (
            "output was buffered until command exit"
        )
    finally:
        release.touch()
        runner.join(timeout=5)
    assert not runner.is_alive()
    assert result["status"] == "success"


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL/process-group lifetime on POSIX")
def test_command_backend_and_capped_logging_outlive_runner_sigkill(tmp_path, monkeypatch):
    from unified_agent.executor import backends

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "4096")
    log = tmp_path / "detached.log"
    ready = tmp_path / "child-pid"
    release = tmp_path / "release"
    done = tmp_path / "done"
    command = (
        "from pathlib import Path; import os, time; "
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "print('survived-runner', flush=True)\n"
        "for _ in range(2000): print('x' * 100, flush=True)\n"
        f"Path({str(done)!r}).touch()\n"
    )
    code = (
        "from unified_agent.executor.backends import CommandBackend; "
        f"CommandBackend({[PY, '-c', command]!r}, {str(log)!r}).run("
        f"cwd={str(tmp_path)!r}, timeout_s=20)"
    )
    # Exercise the worktree under test, not a different installed checkout.
    monkeypatch.setenv("PYTHONPATH", str(Path(backends.__file__).parents[2]))
    runner = subprocess.Popen(
        [PY, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        # The runner and the command each start a fresh interpreter that
        # imports the package; on a loaded CI runner that alone can pass 5 s
        # (#2599). The waits return as soon as the file appears.
        assert _wait_for(ready.exists, timeout=30), "command did not start"
        runner.kill()
        runner.wait(timeout=5)
        release.touch()
        assert _wait_for(done.exists, timeout=30), (
            "command died with its runner (broken stdout pipe)"
        )
        assert _wait_for(lambda: b"output truncated" in log.read_bytes(), timeout=30)
        assert log.read_bytes().startswith(b"survived-runner\n")
        assert log.stat().st_size < 4096 + 200
    finally:
        release.touch()
        if runner.poll() is None:
            runner.kill()
        runner.wait(timeout=5)
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_command_backend_preserves_background_output_after_primary_exit(tmp_path, monkeypatch):
    from unified_agent.executor.backends import CommandBackend

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "4096")
    log = tmp_path / "descendant.log"
    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "from pathlib import Path; import time\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "try:\n"
        " time.sleep(5.1)  # write after the former five-second capture deadline\n"
        " print('descendant-output', flush=True)\n"
        " for _ in range(2000): print('x' * 100, flush=True)\n"
        "finally:\n"
        f" Path({str(done)!r}).touch()\n"
    )
    command = (
        "import subprocess\n"
        f"subprocess.Popen({[PY, '-c', descendant]!r})\n"
        "print('parent-done', flush=True)\n"
    )
    try:
        started = time.monotonic()
        result = CommandBackend([PY, "-c", command], str(log)).run(cwd=str(tmp_path), timeout_s=10)
        assert time.monotonic() - started < 4, "primary return waited for inherited stdout"
        assert result["status"] == "success"
        assert result["truncated"] is False  # receipt precedes descendant's chatter
        assert b"parent-done" in log.read_bytes()
        release.touch()
        assert _wait_for(done.exists, timeout=10), "background writer blocked after run() returned"
        assert _wait_for(lambda: b"output truncated" in log.read_bytes())
        assert b"descendant-output" in log.read_bytes()
        assert log.stat().st_size < 4096 + 200
    finally:
        release.touch()
        _wait_for(done.exists)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cancellation")
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_command_backend_stops_the_command_group_not_the_relay(tmp_path, stop):
    from unified_agent.executor.backends import CommandBackend

    parent_pid = tmp_path / "parent-pid"
    child_pid = tmp_path / "child-pid"
    descendant = (
        "from pathlib import Path; import os, time; "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    command = (
        "from pathlib import Path; import os, subprocess, time; "
        f"Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        f"subprocess.Popen({[PY, '-c', descendant]!r}); time.sleep(60)"
    )
    try:
        result = CommandBackend(
            [PY, "-c", command], str(tmp_path / "stopped.log"), poll_interval_s=0.05
        ).run(
            cwd=str(tmp_path),
            timeout_s=1,
            kill_check=(lambda: child_pid.exists()) if stop == "cancel" else None,
        )
        assert result["status"] == ("killed" if stop == "cancel" else "error")
        assert parent_pid.exists() and child_pid.exists(), "process tree did not start"

        def descendant_stopped():
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", child_pid.read_text()],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return not status or status.startswith("Z")

        assert _wait_for(descendant_stopped), "command's descendant escaped cancellation"
    finally:
        if parent_pid.exists():
            try:
                os.killpg(int(parent_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_command_backend_repeated_appends_do_not_add_truncation_markers(tmp_path, monkeypatch):
    from unified_agent.executor.backends import CommandBackend

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "300")
    log = tmp_path / "reused.log"
    command = [PY, "-c", "print('x' * 1000)"]
    first = CommandBackend(command, str(log)).run(cwd=str(tmp_path), timeout_s=10)
    size = log.stat().st_size
    second = CommandBackend(command, str(log)).run(cwd=str(tmp_path), timeout_s=10)
    assert first["truncated"] and second["truncated"]
    assert log.stat().st_size == size
    assert log.read_bytes().count(b"output truncated") == 1


def test_command_log_relay_keeps_draining_if_its_state_directory_disappears(tmp_path):
    from unified_agent.executor import command_log

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = state_dir / "relay.json"
    relay = subprocess.Popen(
        [PY, "-I", command_log.__file__, str(tmp_path / "log"), str(state), "300"],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(state.exists), "relay did not become ready"
        state.unlink()
        state_dir.rmdir()
        assert relay.stdin is not None
        # Larger than the kernel pipe: this exposes an early reader exit, not
        # just output accepted into a pipe before a broken relay has closed it.
        relay.stdin.write(b"x" * 200_000)
        relay.stdin.close()
        assert relay.wait(timeout=5) == 1  # state durability failed, not the writer
    finally:
        if relay.stdin is not None and not relay.stdin.closed:
            try:
                relay.stdin.close()
            except BrokenPipeError:
                pass
        if relay.poll() is None:
            relay.kill()
        relay.wait(timeout=5)


def test_overlapping_command_relays_share_the_cap_and_one_marker(tmp_path, monkeypatch):
    from unified_agent.executor.backends import CommandBackend

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "4096")
    log = tmp_path / "shared.log"
    ready = [tmp_path / f"ready-{i}" for i in range(2)]

    def run(i):
        code = (
            "from pathlib import Path; import time\n"
            f"Path({str(ready[i])!r}).touch()\n"
            f"while not Path({str(ready[1 - i])!r}).exists(): time.sleep(0.01)\n"
            "print('x' * 200_000, flush=True)\n"
        )
        return CommandBackend([PY, "-c", code], str(log)).run(cwd=str(tmp_path), timeout_s=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert all(result["status"] == "success" and result["truncated"] for result in results)
    assert len({result["log_capture"]["state_path"] for result in results}) == 2
    assert log.stat().st_size < 4096 + 200
    assert log.read_bytes().count(b"output truncated") == 1


@pytest.mark.parametrize("state_file", ["missing", "invalid", "stale-ready"])
def test_dead_log_relay_and_bad_state_cannot_report_clean_capture(
    tmp_path, monkeypatch, state_file
):
    from unified_agent.executor import backends

    spawned = []
    real_popen = subprocess.Popen

    def spawn(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        if len(argv) > 2 and str(argv[2]).endswith("command_log.py"):
            spawned.append((proc, Path(argv[4])))
        return proc

    monkeypatch.setattr(backends.subprocess, "Popen", spawn)
    ready, release = tmp_path / "ready", tmp_path / "release"
    code = (
        "from pathlib import Path; import time\n"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    result = {}
    runner = threading.Thread(
        target=lambda: result.update(
            backends.CommandBackend([PY, "-c", code], str(tmp_path / "log")).run(
                cwd=str(tmp_path), timeout_s=10
            )
        )
    )
    runner.start()
    try:
        assert _wait_for(ready.exists)
        relay, journal = spawned[0]
        relay.kill()
        relay.wait(timeout=5)
        if state_file == "missing":
            journal.unlink()
        elif state_file == "invalid":
            journal.write_text("[]")
        # stale-ready keeps the valid startup state of the now-dead relay.
        release.touch()
        runner.join(timeout=5)
        assert not runner.is_alive()
        assert result["exit_code"] == 0 and result["status"] == "error"
        assert result["log_capture"]["state"] == "unknown"
        assert result["log_capture"]["error"]
        if state_file != "stale-ready":
            assert result["log_capture"]["truncated"] is None
    finally:
        release.touch()
        runner.join(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="FIFO blocks the relay before readiness")
def test_a_relay_finishing_during_the_state_read_is_not_reported_dead(tmp_path, monkeypatch):
    """The exit status is sampled before the state read (task #2552 review).

    A relay that publishes ``done`` and exits between the read and the exit
    check is a clean finish, not a dead relay: the stale read says pending.
    """
    from unified_agent.executor import backends

    spawned = []
    real_popen = subprocess.Popen

    def spawn(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        if len(argv) > 2 and str(argv[2]).endswith("command_log.py"):
            spawned.append(proc)
        return proc

    primary_done, release = tmp_path / "primary-done", tmp_path / "release"

    class RacingJson:
        """Lets the relay finish while the state it just read is still in hand."""

        fired = False

        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, text, *args, **kwargs):
            state = json.loads(text, *args, **kwargs)
            if primary_done.exists() and not state.get("done") and not self.fired:
                self.fired = True
                release.touch()
                spawned[0].wait(timeout=5)
            return state

    racing = RacingJson()
    monkeypatch.setattr(backends.subprocess, "Popen", spawn)
    monkeypatch.setattr(backends, "json", racing)
    descendant = (
        "from pathlib import Path; import time\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    command = (
        "import subprocess; from pathlib import Path\n"
        f"subprocess.Popen({[PY, '-c', descendant]!r})\n"
        f"Path({str(primary_done)!r}).touch()\n"
    )
    try:
        result = backends.CommandBackend([PY, "-c", command], str(tmp_path / "log")).run(
            cwd=str(tmp_path), timeout_s=10
        )
        assert racing.fired, "the relay never finished during a state read"
        assert result["status"] == "success", result
        assert result["log_capture"]["state"] == "pending"
        assert result["log_capture"]["error"] is None
    finally:
        release.touch()


def test_command_readiness_timeout_kills_and_reaps_an_unused_relay(tmp_path, monkeypatch):
    from unified_agent.executor import backends

    log = tmp_path / "blocked.log"
    os.mkfifo(log)
    spawned = []
    real_popen = subprocess.Popen

    def spawn(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(backends.subprocess, "Popen", spawn)
    try:
        with pytest.raises(RuntimeError, match="relay failed to start"):
            backends.CommandBackend([PY, "-c", "pass"], str(log)).run(
                cwd=str(tmp_path), timeout_s=10
            )
        assert len(spawned) == 1, "an unready sink must not start the command"
        assert spawned[0].poll() is not None, "unused relay survived readiness refusal"
    finally:
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def test_command_launch_failure_closes_and_reaps_unused_relay(tmp_path, monkeypatch):
    from unified_agent.executor import backends

    spawned = []
    real_popen = subprocess.Popen

    def spawn(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(backends.subprocess, "Popen", spawn)
    with pytest.raises(FileNotFoundError):
        backends.CommandBackend([str(tmp_path / "missing-command")], str(tmp_path / "log")).run(
            cwd=str(tmp_path), timeout_s=10
        )
    assert len(spawned) == 1
    assert _wait_for(lambda: spawned[0].poll() is not None)


def test_command_backend_refuses_to_start_without_a_log_sink(tmp_path):
    from unified_agent.executor.backends import CommandBackend

    executed = tmp_path / "executed"
    command = [PY, "-c", f"from pathlib import Path; Path({str(executed)!r}).touch()"]
    with pytest.raises(OSError, match="[Dd]irectory"):
        CommandBackend(command, str(tmp_path)).run(cwd=str(tmp_path), timeout_s=10)
    assert not executed.exists()


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="requires Linux /dev/full")
def test_command_backend_reports_log_write_failure_instead_of_success(tmp_path):
    from unified_agent.executor.backends import CommandBackend

    log = tmp_path / "full.log"
    log.symlink_to("/dev/full")
    result = CommandBackend([PY, "-c", "print('output')"], str(log)).run(
        cwd=str(tmp_path), timeout_s=10
    )
    assert result["status"] == "error"
    assert result["exit_code"] == 0  # command passed; log durability did not
    assert result["error"]


@pytest.mark.parametrize("raw", ["", "bad", "0", "-1"])
def test_command_log_cap_invalid_configuration_uses_default(monkeypatch, raw):
    from unified_agent.executor.backends import DEFAULT_COMMAND_LOG_MAX_BYTES, command_log_max_bytes

    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", raw)
    assert command_log_max_bytes() == DEFAULT_COMMAND_LOG_MAX_BYTES


def test_pending_log_capture_is_a_stored_snapshot_not_a_final_log_claim(hub, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "1024")
    agent, root = _command_node(hub, tmp_path, monkeypatch)
    release = tmp_path / "release-capture"
    done = tmp_path / "capture-writer-done"
    descendant = (
        "from pathlib import Path; import time\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "try:\n"
        " print('x' * 200_000, flush=True)\n"
        "finally:\n"
        f" Path({str(done)!r}).touch()\n"
    )
    command = f"import subprocess; subprocess.Popen({[PY, '-c', descendant]!r}); print('main done')"
    item_id = _start(root, [PY, "-c", command])
    try:
        assert agent.run_once() == "ran"
        state = node_command.status(item_id, caller=OWNER)
        snapshot = state["log_capture"]
        assert state["status"] == "finished"
        assert snapshot["state"] == "pending" and snapshot["truncated"] is False
        assert snapshot["error"] is None
        journal = Path(snapshot["state_path"])
        assert journal.exists()
        conn = storage.connect()
        try:
            receipt = json.loads(
                conn.execute(
                    "SELECT receipt_json FROM node_dispatch WHERE id=?", (item_id,)
                ).fetchone()[0]
            )
        finally:
            conn.close()
        assert receipt["log_capture"] == snapshot
        [message] = _owner_messages()
        assert "capture pending" in message["body"]
        release.touch()
        assert _wait_for(done.exists)
        assert _wait_for(lambda: json.loads(journal.read_text())["done"])
        assert json.loads(journal.read_text())["truncated"] is True
        assert node_command.status(item_id, caller=OWNER)["log_capture"] == snapshot
    finally:
        release.touch()
        _wait_for(done.exists)


def test_the_owner_is_told_when_the_command_log_was_truncated(hub, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_NODE_COMMAND_LOG_MAX_BYTES", "2048")
    agent, root = _command_node(hub, tmp_path, monkeypatch)
    argv = [PY, "-c", "import sys\nfor _ in range(500): sys.stdout.write('x' * 100 + '\\n')"]
    item_id = _start(root, argv)
    assert agent.run_once() == "ran"
    state = node_command.status(item_id, caller=OWNER)
    assert state["status"] == "finished" and state["exit_code"] == 0
    log = tmp_path / "sessions" / item_id / "command.log"
    assert log.stat().st_size < 2048 + 200
    [message] = _owner_messages()
    assert "truncated at the node's log cap" in message["body"]


def test_a_writer_without_the_fence_is_refused_and_audited(hub, tmp_path, monkeypatch):
    _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(tmp_path / "dev", [PY, "-c", "pass"])
    claim = node_queue.claim_next(NODE, identity=OWNER)
    assert claim["item_id"] == item_id and claim["command"]["argv"][0] == PY
    token = claim["command"]["fence_token"]
    receipt = {"status": "finished", "exit_code": 0, "attempt": claim["attempt"]}
    for bad in (None, "0" * 32):
        with pytest.raises(NodeError, match="stale fence") as exc:
            node_queue.post_receipt(
                NODE, item_id, identity=OWNER, receipt={**receipt, "fence_token": bad}
            )
        assert exc.value.status == 409 and exc.value.code == "stale_fence"
    with pytest.raises(NodeError, match="stale receipt"):
        node_queue.post_receipt(
            NODE, item_id, identity=OWNER, receipt={**receipt, "attempt": 0, "fence_token": token}
        )
    assert len(_node_events("stale_fence")) == 2
    assert node_command.status(item_id, caller=OWNER)["status"] == "claimed"
    # The holder of the fence completes it, once; a resent receipt adds nothing.
    done = node_queue.post_receipt(
        NODE, item_id, identity=OWNER, receipt={**receipt, "fence_token": token}
    )
    again = node_queue.post_receipt(
        NODE, item_id, identity=OWNER, receipt={**receipt, "fence_token": token}
    )
    assert done["status"] == "finished" and again["duplicate"] is True
    assert len(_owner_messages()) == 1
    assert "fence_token" not in node_command.status(item_id, caller=OWNER)


def test_a_node_runs_only_what_it_allowlists(hub, tmp_path, monkeypatch):
    agent, root = _command_node(hub, tmp_path, monkeypatch, allow=("/usr/bin/true",))
    item_id = _start(root, [PY, "-c", "print('should not run')"])
    assert agent.run_once() == "ran"
    state = node_command.status(item_id, caller=OWNER)
    assert state["status"] == "failed"
    assert not (tmp_path / "sessions" / item_id / "command.log").exists()
    [message] = _owner_messages()
    assert "command_not_allowlisted" in message["body"]
    # With nothing allowlisted, a node never even advertises the backend.
    monkeypatch.setenv("UA_NODE_BACKENDS", "codex,command")
    monkeypatch.setenv("UA_NODE_COMMANDS", "")
    from unified_agent import node_agent

    assert "command" not in node_agent.allowed_backends()


def test_adoption_is_the_owners_alone_and_never_after_the_end(hub, tmp_path, monkeypatch):
    _, root = _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(root, [PY, "-c", "pass"])
    other = registry_tools.session_start(
        kind="claude_code", agent="Desktop_Agent2", external_session_id="x"
    )["session_id"]
    with pytest.raises(node_command.CommandError, match="visible to you"):
        node_command.adopt(item_id, caller="Desktop_Agent2", session_id=other)
    with pytest.raises(node_command.CommandError, match="is not a session of"):
        node_command.adopt(item_id, caller=OWNER, session_id=other)
    mine = _session("mine")
    assert node_command.adopt(item_id, caller=OWNER, session_id=mine)["owner_session"] == mine
    claim = node_queue.claim_next(NODE, identity=OWNER)
    node_queue.post_receipt(
        NODE,
        item_id,
        identity=OWNER,
        receipt={
            "status": "failed",
            "exit_code": 2,
            "attempt": claim["attempt"],
            "fence_token": claim["command"]["fence_token"],
        },
    )
    with pytest.raises(node_command.CommandError, match="already failed"):
        node_command.adopt(item_id, caller=OWNER, session_id=mine)


@pytest.mark.parametrize("claimed", [False, True])
def test_configured_lifecycle_operator_can_adopt_without_gaining_status_access(
    hub, tmp_path, monkeypatch, claimed
):
    _, root = _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(root, [PY, "-c", "pass"])
    operator = "Desktop_Agent2"
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", operator)
    assert not config.is_human_identity(operator)
    claim = node_queue.claim_next(NODE, identity=OWNER) if claimed else None
    mine = _session("owner-for-operator")
    foreign = registry_tools.session_start(
        kind="claude_code", agent=operator, external_session_id="operator-session"
    )["session_id"]
    # Recovery authority does not make ordinary reads available to the operator.
    with pytest.raises(node_command.CommandError, match="visible to you"):
        node_command.status(item_id, caller=operator)
    with pytest.raises(node_command.CommandError, match="visible to you"):
        node_command.adopt("missing-item", caller=operator, session_id=None)
    with pytest.raises(node_command.CommandError, match="is not a session of"):
        node_command.adopt(item_id, caller=operator, session_id=foreign)
    adopted = node_command.adopt(item_id, caller=operator, session_id=mine)
    assert adopted["owner_agent"] == OWNER and adopted["owner_session"] == mine
    assert node_command.adopt(item_id, caller=operator, session_id=None)["owner_session"] is None
    with pytest.raises(node_command.CommandError, match="visible to you"):
        node_command.adopt(item_id, caller="Desktop_Agent4", session_id=None)
    claim = claim or node_queue.claim_next(NODE, identity=OWNER)
    node_queue.post_receipt(
        NODE,
        item_id,
        identity=OWNER,
        receipt={
            "status": "failed",
            "attempt": claim["attempt"],
            "fence_token": claim["command"]["fence_token"],
        },
    )
    with pytest.raises(node_command.CommandError, match="already failed"):
        node_command.adopt(item_id, caller=operator, session_id=None)


def test_the_mcp_actions_bind_the_owner_to_the_verified_caller(hub, tmp_path, monkeypatch):
    from unified_agent.tools import graph as graph_tool

    _, root = _command_node(hub, tmp_path, monkeypatch)
    monkeypatch.setattr(node_queue, "_require_native_item", lambda *a, **k: None)
    monkeypatch.setattr("unified_agent.safety.check_prod_action_gate", lambda *a, **k: None)
    token = config.agent_identity_var.set(OWNER)
    try:
        started = graph_tool.coordination_manage(
            "node_command_start",
            {"node": NODE, "argv": [PY, "-c", "pass"], "cwd": str(root / "repo")},
        )
        assert started["owner_agent"] == OWNER
        got = graph_tool.coordination_manage("node_command_status", {"item_id": started["item_id"]})
        assert got["argv"] == [PY, "-c", "pass"] and got["status"] == "queued"
        with pytest.raises(ToolError, match="never a shell string"):
            graph_tool.coordination_manage(
                "node_command_start", {"node": NODE, "argv": "ls -la", "cwd": str(root)}
            )
    finally:
        config.agent_identity_var.reset(token)
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="visible to you"):
            graph_tool.coordination_manage("node_command_status", {"item_id": started["item_id"]})
    finally:
        config.agent_identity_var.reset(token)


def test_the_doctor_reports_a_command_past_its_lease_or_an_unsent_message(
    hub, tmp_path, monkeypatch
):
    from unified_agent.tools import doctor

    _, root = _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(root, [PY, "-c", "pass"])
    node_queue.claim_next(NODE, identity=OWNER)
    assert doctor._command_items_unsettled_check()["status"] == "LIVE"
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (item_id,),
        )
        conn.execute(
            "INSERT INTO node_command_notice (item_id, attempt, terminal, owner_agent, body, "
            "created_at) VALUES ('nd-x', 1, 'finished', ?, 'b', '2000-01-01T00:00:00+00:00')",
            (OWNER,),
        )
        conn.commit()
    finally:
        conn.close()
    check = doctor._command_items_unsettled_check()
    assert check["status"] == "DEGRADED"
    assert item_id in check["detail"] and "nd-x" in check["detail"]


def test_a_message_lost_after_commit_is_redelivered_exactly_once(hub, tmp_path, monkeypatch):
    """The completion CAS and the intent commit together; a crash before delivery
    leaves the intent, and hygiene sends it once."""
    from unified_agent import queue_hygiene
    from unified_agent.messaging import send as send_mod

    _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(tmp_path / "dev", [PY, "-c", "pass"])
    claim = node_queue.claim_next(NODE, identity=OWNER)
    real = send_mod.send_service_notice

    def crash(**_kwargs):
        raise RuntimeError("hub restarted between commit and delivery")

    monkeypatch.setattr(send_mod, "send_service_notice", crash)
    done = node_queue.post_receipt(
        NODE,
        item_id,
        identity=OWNER,
        receipt={
            "status": "finished",
            "exit_code": 0,
            "attempt": claim["attempt"],
            "fence_token": claim["command"]["fence_token"],
        },
    )
    assert done["status"] == "finished" and _owner_messages() == []
    conn = storage.connect()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM node_command_notice WHERE item_id=? AND sent_at IS NULL",
            (item_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert pending == 1  # committed with the completion, not lost with the send
    monkeypatch.setattr(send_mod, "send_service_notice", real)
    first = queue_hygiene.run_hygiene_pass()["command_notices"]
    second = queue_hygiene.run_hygiene_pass()["command_notices"]
    assert first["sent"] == [node_command.notice_id(item_id, 1, "finished")]
    assert second["sent"] == [] and len(_owner_messages()) == 1


def test_an_approval_binds_the_exact_argv_and_the_node_runs_it_unchanged(
    hub, tmp_path, monkeypatch
):
    from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve

    agent, root = _command_node(hub, tmp_path, monkeypatch)
    approved = [PY, "-c", "print('a b')", "--flag=x y"]
    other = [PY, "-c", "print('a b')", "--flag=x", "y"]  # same words, other split
    command = {"argv": approved, "owner_agent": OWNER}
    fields = {
        "backend": "command",
        "prompt": node_command.describe(approved),
        "cwd": str(root / "repo"),
    }
    with pytest.raises(ApprovalPendingError) as pending:
        node_queue.request_dispatch(NODE, actor=OWNER, command=dict(command), **fields)
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(pending.value.approval_id, "approve", resolved_by="Desktop_Person")
    finally:
        config.agent_identity_var.reset(token)
    # The pass for one argv cannot be spent by another, even one that reads alike.
    with pytest.raises(ApprovalPendingError):
        node_queue.request_dispatch(
            NODE,
            actor=OWNER,
            command={"argv": other, "owner_agent": OWNER},
            **{**fields, "prompt": node_command.describe(other)},
        )
    item = node_queue.request_dispatch(NODE, actor=OWNER, command=dict(command), **fields)
    seen = {}
    from unified_agent.executor import backends

    real_init = backends.CommandBackend.__init__

    def record(self, argv, log_path, **kw):
        seen["argv"] = argv
        real_init(self, argv, log_path, **kw)

    monkeypatch.setattr(backends.CommandBackend, "__init__", record)
    assert agent.run_once() == "ran"
    assert seen["argv"] == approved  # never re-split, re-quoted or merged
    assert node_command.status(item["item_id"], caller=OWNER)["argv"] == approved


def test_an_unverified_caller_reads_no_command(hub, tmp_path, monkeypatch):
    _command_node(hub, tmp_path, monkeypatch)
    item_id = _start(tmp_path / "dev", [PY, "-c", "pass", "--token=secret-value"])
    with pytest.raises(node_command.CommandError, match="visible to you"):
        node_command.status(item_id, caller=None)
    assert node_command.status(item_id, caller=OWNER)["argv"][-1] == "--token=secret-value"
