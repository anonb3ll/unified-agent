"""AcpClient.terminate() stops the agent's whole process group (task #1984).

An ACP agent runs tools as subprocesses. Signalling only the agent pid left
those children running, still able to write into the worktree after the
session ended, including during the error-path end-of-turn checkpoint
(#1743). The agent now leads its own session and process group, and
terminate() sends SIGTERM to the group, then SIGKILL to whatever is left
after the grace window.
"""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unified_agent import acp_client
from unified_agent.acp_client import AcpClient

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")

# The agent forks one tool-like child that records its pid and then sleeps.
# FAKE_CHILD_IGNORES_TERM makes the child ignore SIGTERM, so only SIGKILL
# stops it. FAKE_AGENT_EXITS makes the agent exit right after the fork,
# leaving the child orphaned in the agent's group.
FORKING_AGENT = r"""
import os, signal, sys, time

pid_file = os.environ["FAKE_CHILD_PID_FILE"]
child = os.fork()
if child == 0:
    if os.environ.get("FAKE_CHILD_IGNORES_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    tmp = pid_file + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(str(os.getpid()))
    os.replace(tmp, pid_file)
    while True:
        time.sleep(0.1)
if os.environ.get("FAKE_AGENT_EXITS"):
    while not os.path.exists(pid_file):
        time.sleep(0.01)
    os._exit(int(os.environ.get("FAKE_AGENT_EXIT_CODE", "0")))
for _line in sys.stdin:
    pass
"""


_HAVE_PROC = Path("/proc/self/stat").exists()
# Bound at import: tests that patch subprocess.run to simulate a missing ps
# (acp_client.subprocess is this same module) must not break the helper.
_REAL_SUBPROCESS_RUN = subprocess.run


def _alive(pid: int, *, use_proc: bool | None = None) -> bool:
    """True while ``pid`` is a running (not zombie) process. Portable.

    ``os.kill(pid, 0)`` rules out a pid that is gone; a zombie still answers
    it, so its state comes from ``/proc`` on Linux and ``ps`` elsewhere.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if _HAVE_PROC if use_proc is None else use_proc:
        try:
            raw = Path(f"/proc/{pid}/stat").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return False
        # Field 3, after the parenthesised command name, is the state.
        return raw.rsplit(b")", 1)[1].split()[0] not in (b"Z", b"X")
    state = _REAL_SUBPROCESS_RUN(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith(("Z", "X"))


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def forking_agent(tmp_path, monkeypatch):
    script = tmp_path / "forking_agent.py"
    script.write_text(FORKING_AGENT)
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PID_FILE", str(pid_file))
    monkeypatch.setattr(acp_client, "KILL_GRACE_S", 0.5)
    started: list[int] = []

    def spawn() -> tuple[AcpClient, int]:
        client = AcpClient([sys.executable, str(script)], session_dir=str(tmp_path))
        assert _wait_for(pid_file.exists), "agent never forked its child"
        child = int(pid_file.read_text())
        started.append(child)
        return client, child

    yield spawn
    for pid in started:  # never leak a sleeper past the test
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_agent_leads_its_own_process_group(forking_agent):
    client, child = forking_agent()
    try:
        assert os.getpgid(client.pid) == client.pid
        assert os.getpgid(child) == client.pid
        assert os.getpgid(client.pid) != os.getpgrp()
    finally:
        client.close()


def test_terminate_stops_the_agents_children(forking_agent):
    client, child = forking_agent()
    assert _alive(child)
    client.terminate()
    assert client.exited() is not None
    assert _wait_for(lambda: not _alive(child), timeout=5.0), "agent's child survived terminate"
    client.close()


def test_terminate_kills_a_child_that_ignores_sigterm(forking_agent, monkeypatch):
    monkeypatch.setenv("FAKE_CHILD_IGNORES_TERM", "1")
    client, child = forking_agent()
    started = time.monotonic()
    client.terminate()
    assert _wait_for(lambda: not _alive(child), timeout=5.0), "SIGTERM-immune child survived"
    # Bounded by the grace window, not left to run until the session is reaped.
    assert time.monotonic() - started < 0.5 + 3.0
    client.close()


def test_terminate_sweeps_children_after_the_agent_already_exited(forking_agent, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, child = forking_agent()
    assert _wait_for(lambda: client.exited() is not None), "agent did not exit"
    assert _alive(child)
    client.terminate()
    assert _wait_for(lambda: not _alive(child), timeout=5.0), "orphaned child survived terminate"
    client.close()


def test_terminate_is_idempotent(forking_agent):
    client, child = forking_agent()
    client.terminate()
    started = time.monotonic()
    client.terminate()  # second call returns at once, signalling nothing
    assert time.monotonic() - started < 0.1
    client.close()
    assert _wait_for(lambda: not _alive(child), timeout=5.0)


# ── review fixes (PR #1120) ─────────────────────────────────────────


def _recording_killpg(monkeypatch, *, darwin_eperm: bool):
    """Wrap os.killpg, recording signals; optionally emulate Darwin's EPERM.

    On Darwin, ``killpg(pgid, 0)`` raises EPERM rather than ESRCH once no
    live process is left in the group (see dispatch/supervise._pgid_alive).
    """
    import errno

    real = os.killpg
    sent: list[int] = []

    def killpg(pgid, sig):
        sent.append(sig)
        if sig == 0 and darwin_eperm:
            try:
                return real(pgid, 0)
            except ProcessLookupError:
                raise PermissionError(errno.EPERM, "Operation not permitted") from None
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    return sent


@pytest.mark.parametrize(("platform", "alive"), [("darwin", False), ("linux", True)])
def test_group_alive_reads_eperm_per_platform(monkeypatch, platform, alive):
    def eperm(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "killpg", eperm)
    assert AcpClient._group_alive(424242) is alive


def test_darwin_terminate_keeps_the_leader_until_group_sweep(forking_agent, monkeypatch):
    """The zombie anchor can hold the group through grace; never reap before SIGKILL."""
    import signal

    client, child = forking_agent()
    monkeypatch.setattr(acp_client, "KILL_GRACE_S", 0.5)
    monkeypatch.setattr(sys, "platform", "darwin")
    sent = _recording_killpg(monkeypatch, darwin_eperm=True)
    started = time.monotonic()
    client.terminate()
    elapsed = time.monotonic() - started
    assert _wait_for(lambda: not _alive(child), timeout=5.0)
    assert signal.SIGTERM in sent
    assert elapsed < 1.5, f"terminate exceeded grace: {elapsed:.2f}s"
    client.close()


def test_exited_does_not_reap_the_leader_before_terminate(forking_agent, monkeypatch):
    """The unreaped leader keeps its pid, and so the pgid, from being recycled."""
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, child = forking_agent()
    assert _wait_for(lambda: client.exited() is not None)
    assert client.exited() == 0
    assert client._proc.returncode is None  # still a zombie we own
    client.terminate()
    assert client.exited() == 0
    assert _wait_for(lambda: not _alive(child), timeout=5.0)
    client.close()


def test_terminate_never_signals_a_group_whose_leader_was_already_reaped(
    forking_agent, monkeypatch
):
    """Once the leader is reaped its pgid number may be recycled: no killpg."""
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, child = forking_agent()
    client._proc.wait(timeout=5.0)  # reaped behind the client's back
    sent = _recording_killpg(monkeypatch, darwin_eperm=False)
    client.terminate()
    assert sent == []
    os.kill(child, 9)
    client.close()


@pytest.mark.parametrize("missing", ["waitid", "WNOWAIT"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_missing_waitid_stops_descendants_before_reporting_exit(
    forking_agent, monkeypatch, missing, exit_code
):
    """Pre-3.13 macOS lacks waitid; stdout is still held by the child."""
    monkeypatch.delattr(os, missing, raising=False)
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    monkeypatch.setenv("FAKE_AGENT_EXIT_CODE", str(exit_code))
    monkeypatch.setenv("FAKE_CHILD_IGNORES_TERM", "1")
    client, child = forking_agent()
    try:
        assert _wait_for(lambda: client.exited() is not None)
        client.terminate()
        assert client.exited() == exit_code
        assert _wait_for(lambda: not _alive(child), timeout=1), "fallback left child alive"
    finally:
        client.close()


def test_missing_waitid_does_not_reap_or_stop_running_leader(forking_agent, monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    client, child = forking_agent()
    try:
        assert client.exited() is None
        assert client._proc.returncode is None
        assert _alive(client.pid) and _alive(child)
    finally:
        client.close()


def test_missing_waitid_observation_error_never_reaps(forking_agent, monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    client, child = forking_agent()

    def unavailable(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(acp_client.subprocess, "run", unavailable)
    try:
        with pytest.raises(acp_client.AcpError, match="cannot observe"):
            client.exited()
        assert client._proc.returncode is None
        assert _alive(client.pid) and _alive(child)
    finally:
        client.close()


def test_group_signals_precede_reaping_even_when_leader_exits(forking_agent, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    monkeypatch.setenv("FAKE_CHILD_IGNORES_TERM", "1")
    client, child = forking_agent()
    assert _wait_for(lambda: client.exited() is not None)
    real = os.killpg

    def anchored_signal(pgid, sig):
        assert client._proc.returncode is None, "group signal after PID anchor was reaped"
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", anchored_signal)
    try:
        client.terminate()
        assert _wait_for(lambda: not _alive(child), timeout=1)
    finally:
        client.close()


# ── review fixes (PR #1149) ─────────────────────────────────────────


def test_terminate_returns_once_the_group_is_gone_not_after_grace(forking_agent, monkeypatch):
    """The unreaped zombie leader must not hold terminate() for the grace window.

    SIGTERM stops both the agent and its child at once; only the leader's
    zombie (kept as the pgid anchor) is left, which is not a live member. The
    final group SIGKILL is still sent, without waiting out the grace window.
    """
    import signal

    client, child = forking_agent()
    monkeypatch.setattr(acp_client, "KILL_GRACE_S", 3.0)
    sent = _recording_killpg(monkeypatch, darwin_eperm=False)
    started = time.monotonic()
    client.terminate()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 1.5, f"terminate waited out the grace window: {elapsed:.2f}s"
        assert sent.index(signal.SIGTERM) < sent.index(signal.SIGKILL)
        assert client.exited() == -signal.SIGTERM
        assert _wait_for(lambda: not _alive(child), timeout=1.0)
    finally:
        client.close()


def test_terminate_of_a_childless_agent_is_prompt(tmp_path, monkeypatch):
    script = tmp_path / "idle_agent.py"
    script.write_text("import sys\nfor _line in sys.stdin:\n    pass\n")
    monkeypatch.setattr(acp_client, "KILL_GRACE_S", 3.0)
    client = AcpClient([sys.executable, str(script)], session_dir=str(tmp_path))
    try:
        started = time.monotonic()
        client.terminate()
        assert time.monotonic() - started < 1.5
        assert client.exited() is not None
    finally:
        client.close()


def test_group_with_only_a_zombie_leader_is_not_alive(forking_agent, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, child = forking_agent()
    try:
        assert _wait_for(lambda: client.exited() is not None)
        assert client._proc.returncode is None  # zombie still anchors the pgid
        assert AcpClient._group_alive(client.pid) is True  # the orphan child is live
        os.kill(child, 9)
        assert _wait_for(lambda: not AcpClient._group_alive(client.pid), timeout=2.0)
    finally:
        client.close()


# ── independent review fixes (PR #1149, second round) ─────────────


# The agent forks FAKE_CHILDREN SIGTERM-immune children, records their pids,
# then exits (or idles). FAKE_CHILD_COMM renames each child (Linux prctl
# PR_SET_NAME) to the given latin-1 bytes, e.g. a non-UTF-8 name.
MANY_CHILD_AGENT = r"""
import ctypes, os, signal, sys, time

pid_file = os.environ["FAKE_CHILD_PID_FILE"]
pids = []
ready_r, ready_w = os.pipe()
for _ in range(int(os.environ.get("FAKE_CHILDREN", "1"))):
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        comm = os.environ.get("FAKE_CHILD_COMM")
        if comm:
            ctypes.CDLL(None).prctl(15, comm.encode("latin-1"), 0, 0, 0)
        os.write(ready_w, b".")  # handler installed and name set
        while True:
            time.sleep(0.1)
    pids.append(child)
ready = 0
while ready < len(pids):  # every child has installed its handler and name
    ready += len(os.read(ready_r, len(pids) - ready))
tmp = pid_file + ".tmp"
with open(tmp, "w") as fh:
    fh.write(" ".join(map(str, pids)))
os.replace(tmp, pid_file)
if os.environ.get("FAKE_AGENT_EXITS"):
    os._exit(0)
for _line in sys.stdin:
    pass
"""


@pytest.fixture
def many_child_agent(tmp_path, monkeypatch):
    script = tmp_path / "many_child_agent.py"
    script.write_text(MANY_CHILD_AGENT)
    pid_file = tmp_path / "children.pid"
    monkeypatch.setenv("FAKE_CHILD_PID_FILE", str(pid_file))
    monkeypatch.setattr(acp_client, "KILL_GRACE_S", 0.3)
    started: list[int] = []

    def spawn() -> tuple[AcpClient, list[int]]:
        client = AcpClient([sys.executable, str(script)], session_dir=str(tmp_path))
        assert _wait_for(pid_file.exists), "agent never forked its children"
        children = [int(p) for p in pid_file.read_text().split()]
        started.extend(children)
        return client, children

    yield spawn
    for pid in started:  # never leak a sleeper past the test
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc")
def test_non_utf8_process_name_cannot_abort_the_sweep(many_child_agent, monkeypatch):
    """A /proc name that is not UTF-8 must not stop terminate() before SIGKILL."""
    monkeypatch.setenv("FAKE_CHILD_COMM", "\xff")
    client, children = many_child_agent()
    try:
        client.terminate()
        assert all(not _alive(pid) for pid in children), "renamed child survived terminate"
    finally:
        client.close()


def test_liveness_scan_errors_presume_the_group_alive(monkeypatch):
    def broken(pgid):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(acp_client, "_proc_stat", lambda path: broken(0))
    monkeypatch.setattr(acp_client.subprocess, "run", lambda *a, **k: broken(0))
    assert AcpClient._group_alive(424242) is True


def test_failed_cleanup_is_retryable(many_child_agent, monkeypatch):
    """terminate() latches only once the group was swept, so a retry still kills."""
    client, children = many_child_agent()
    real = os.killpg
    failures = [RuntimeError("signal delivery failed")]

    def flaky(pgid, sig):
        if failures:
            raise failures.pop()
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", flaky)
    try:
        with pytest.raises(RuntimeError):
            client.terminate()
        assert all(_alive(pid) for pid in children)
        client.terminate()
        assert all(not _alive(pid) for pid in children), "retry did not sweep the group"
    finally:
        client.close()


def test_terminate_returns_only_after_sigkilled_descendants_are_gone(many_child_agent, monkeypatch):
    """With the leader already exited, SIGKILL cleanup waits for the group, not the leader."""
    monkeypatch.setenv("FAKE_CHILDREN", "16")
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, children = many_child_agent()
    try:
        assert _wait_for(lambda: client.exited() is not None)
        client.terminate()
        survivors = [pid for pid in children if _alive(pid)]
        assert survivors == [], f"{len(survivors)} SIGKILLed descendants still running"
    finally:
        client.close()


def test_group_that_outlives_sigkill_is_reported_not_hung(many_child_agent, monkeypatch, caplog):
    client, children = many_child_agent()
    monkeypatch.setattr(AcpClient, "_group_alive", staticmethod(lambda pgid: True))
    try:
        started = time.monotonic()
        with caplog.at_level("WARNING", logger="unified_agent.acp"):
            client.terminate()
        assert time.monotonic() - started < 0.3 * 2 + 2.0
        assert any("not confirmed stopped" in r.getMessage() for r in caplog.records)
    finally:
        monkeypatch.undo()
        client.close()


def test_final_sigkill_does_not_trust_a_negative_member_scan(many_child_agent, monkeypatch):
    """A /proc snapshot can miss a just-forked descendant (fork-and-exit race).

    The race is made deterministic by forcing the member scan to report no
    live member while a SIGTERM-immune child is in the group: the final
    group SIGKILL before reaping must still reach it.
    """
    import signal

    client, children = many_child_agent()
    monkeypatch.setattr(AcpClient, "_group_has_live_member", staticmethod(lambda pgid: False))
    sent = _recording_killpg(monkeypatch, darwin_eperm=False)
    try:
        client.terminate()
        assert signal.SIGKILL in sent
        assert client._proc.returncode is not None
        assert _wait_for(lambda: all(not _alive(pid) for pid in children), timeout=2.0), (
            "descendant missed by the scan survived terminate"
        )
    finally:
        client.close()


# ── independent review fixes (PR #1149, third round) ──────────────


def test_alive_helper_agrees_with_ps_and_sees_zombies(tmp_path):
    """The survivor checks must not pass vacuously where /proc is absent."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _alive(child.pid, use_proc=False) is True
        if _HAVE_PROC:
            assert _alive(child.pid, use_proc=True) is True
        child.kill()
        assert _wait_for(lambda: not _alive(child.pid, use_proc=False), timeout=5.0)
        assert child.returncode is None  # unreaped zombie: alive to kill(0), not running
        if _HAVE_PROC:
            assert _alive(child.pid, use_proc=True) is False
    finally:
        child.kill()
        child.wait()
    assert _alive(child.pid, use_proc=False) is False


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EIO])
def test_alive_helper_handles_proc_read_errors(monkeypatch, error_number):
    """Reaping during read means gone; unrelated observation failures must surface."""
    pid = os.getpid()  # kill(pid, 0) succeeds before the simulated proc read race.
    error = OSError(error_number, os.strerror(error_number))
    reads = []

    class FailingStat(io.BytesIO):
        def read(self, *args, **kwargs):
            reads.append(True)
            raise error

    def open_stat(path, mode, **kwargs):
        assert path == Path(f"/proc/{pid}/stat")
        assert mode == "rb"
        return FailingStat()

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_stat)
        if error_number in (errno.ENOENT, errno.ESRCH):
            assert _alive(pid, use_proc=True) is False
        else:
            with pytest.raises(type(error)) as caught:
                _alive(pid, use_proc=True)
            assert caught.value is error
    assert reads == [True]


def _killpg_failing(monkeypatch, failing: set[int]):
    """os.killpg that raises EPERM for the given signals (probe 0 still works)."""
    import errno

    real = os.killpg
    sent: list[int] = []

    def killpg(pgid, sig):
        sent.append(sig)
        if sig in failing:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    return sent


def test_signal_error_leaves_terminate_unconfirmed_and_retryable(many_child_agent, monkeypatch):
    import signal

    client, children = many_child_agent()
    sent = _killpg_failing(monkeypatch, {signal.SIGTERM, signal.SIGKILL})
    try:
        assert client.terminate() is False
        assert all(_alive(pid) for pid in children)
        assert client._proc.returncode is None  # the anchor is kept for a retry
        assert signal.SIGKILL in sent
        monkeypatch.undo()  # real killpg again
        monkeypatch.setattr(acp_client, "KILL_GRACE_S", 0.3)
        retried = _recording_killpg(monkeypatch, darwin_eperm=False)
        assert client.terminate() is True
        assert signal.SIGKILL in retried, "the retry sent no signals"
        assert all(not _alive(pid) for pid in children)
        assert client.terminate() is True  # latched once confirmed
    finally:
        client.close()


def test_group_still_alive_after_sigkill_grace_is_unconfirmed(many_child_agent, monkeypatch):
    client, children = many_child_agent()
    monkeypatch.setattr(AcpClient, "_group_alive", staticmethod(lambda pgid: True))
    try:
        assert client.terminate() is False
        assert client._terminated is False
    finally:
        monkeypatch.undo()
        monkeypatch.setattr(acp_client, "KILL_GRACE_S", 0.3)
        client.close()


def test_confirmed_terminate_reports_true(forking_agent):
    client, child = forking_agent()
    try:
        assert client.terminate() is True
        assert client.exited() is not None
        assert not _alive(child)
    finally:
        client.close()


def test_missing_proc_falls_back_to_ps_for_group_members(many_child_agent, monkeypatch, tmp_path):
    """The production path without /proc (macOS) is exercised, not only Linux."""
    monkeypatch.setattr(acp_client, "_PROC", tmp_path / "no-proc")
    runs: list[list[str]] = []
    real_run = acp_client.subprocess.run

    def recording_run(cmd, *args, **kwargs):
        runs.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(acp_client.subprocess, "run", recording_run)
    client, children = many_child_agent()
    try:
        assert AcpClient._group_alive(client.pid) is True
        assert client.terminate() is True
        assert all(not _alive(pid) for pid in children)
        assert any(cmd[:2] == ["/bin/ps", "-A"] for cmd in runs)
    finally:
        client.close()


def test_missing_waitid_reports_an_unconfirmed_stop_as_an_error(forking_agent, monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setenv("FAKE_AGENT_EXITS", "1")
    client, child = forking_agent()
    real = AcpClient.terminate
    monkeypatch.setattr(AcpClient, "terminate", lambda self: False)
    try:
        assert _wait_for(lambda: _alive(child) and not _alive(client.pid))
        with pytest.raises(acp_client.AcpError, match="not confirmed"):
            client.exited()
    finally:
        monkeypatch.setattr(AcpClient, "terminate", real)
        client.close()


def _fake_proc(tmp_path, monkeypatch, unreadable: type[OSError]) -> int:
    """A /proc with a zombie leader and one member whose stat read fails."""
    proc, pgid = tmp_path / "proc", 4242
    for pid, state in (("self", "R"), (str(pgid), "Z"), ("5151", "S")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "stat").write_bytes(f"{pid} (agent) {state} 1 {pgid} {pgid}".encode())
    member = proc / "5151" / "stat"
    real_read = Path.read_bytes

    def read_bytes(self):
        if self == member:
            raise unreadable(member)
        return real_read(self)

    monkeypatch.setattr(acp_client, "_PROC", proc)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    return pgid


def test_an_unreadable_member_keeps_the_group_alive(tmp_path, monkeypatch):
    """#1149: EACCES on a member's stat is not disappearance; it may be a survivor."""
    pgid = _fake_proc(tmp_path, monkeypatch, PermissionError)
    monkeypatch.setattr(os, "getpgid", lambda pid: pgid)
    assert AcpClient._group_alive(pgid) is True


def test_an_unreadable_process_in_another_group_does_not_pin_the_group(tmp_path, monkeypatch):
    """hidepid: an unrelated unreadable process must not keep a zombie-led group alive."""
    pgid = _fake_proc(tmp_path, monkeypatch, PermissionError)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9999)
    assert AcpClient._group_alive(pgid) is False


def test_an_unreadable_process_that_vanished_is_gone(tmp_path, monkeypatch):
    pgid = _fake_proc(tmp_path, monkeypatch, PermissionError)

    def getpgid(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "getpgid", getpgid)
    assert AcpClient._group_alive(pgid) is False


@pytest.mark.parametrize("gone", [FileNotFoundError, ProcessLookupError])
def test_a_member_that_disappeared_mid_scan_is_gone(tmp_path, monkeypatch, gone):
    pgid = _fake_proc(tmp_path, monkeypatch, gone)
    assert AcpClient._group_alive(pgid) is False
