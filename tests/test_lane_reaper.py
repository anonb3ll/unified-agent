"""Tests for orphaned Codex-lane process reaper (UA #1316)."""

from unified_agent.lane_reaper import (
    Proc,
    _pid_still_matches,
    classify_orphans,
    is_keeper,
    is_lane_pytest,
    is_sandbox,
    is_target,
    reap,
)

LANE = "/home/you/.codex/worktrees/abc/repo"


def test_lane_sandbox_under_init_is_orphan():
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(
            pid=50,
            ppid=1,
            cmdline="/usr/bin/codex-linux-sandbox -- pytest",
            cwd=LANE,
        ),
    ]
    orphans = classify_orphans(procs)
    assert [p.pid for p in orphans] == [50]


def test_non_lane_sandbox_is_not_target():
    """Devin #758: ad-hoc sandbox outside lane roots must not be reaped."""
    p = Proc(
        pid=50,
        ppid=1,
        cmdline="/usr/bin/codex-linux-sandbox -- pytest",
        cwd="/home/alice/projects/demo",
    )
    assert is_sandbox(p) is False
    assert classify_orphans([Proc(pid=1, ppid=0, cmdline="/sbin/init"), p]) == []


def test_pytest_in_codex_worktree_under_dead_parent_is_orphan():
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(
            pid=80,
            ppid=1,
            cmdline="python -m pytest tests/test_foo.py",
            cwd=LANE,
        ),
    ]
    orphans = classify_orphans(procs)
    assert [p.pid for p in orphans] == [80]


def test_pytest_under_live_cursor_is_not_orphan():
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(pid=10, ppid=1, cmdline="/usr/share/cursor/cursor --no-sandbox"),
        Proc(pid=11, ppid=10, cmdline="zsh"),
        Proc(
            pid=12,
            ppid=11,
            cmdline="python -m pytest tests/test_reviewer_pipeline.py",
            cwd="/home/you/dev/unified_agent/.worktrees/task-1338",
        ),
    ]
    assert classify_orphans(procs) == []


def test_sandbox_shell_pytest_tree_reaps_targets():
    """Devin #758: shell between sandbox and pytest still clears both targets."""
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(pid=100, ppid=1, cmdline="opt/codex/bin/codex-linux-sandbox", cwd=LANE),
        Proc(pid=101, ppid=100, cmdline="zsh"),
        Proc(pid=102, ppid=101, cmdline="pytest -q", cwd=LANE),
    ]
    orphans = classify_orphans(procs)
    assert [p.pid for p in orphans] == [102, 100]


def test_generator_mentioning_pytest_path_is_not_target():
    """Devin #758: argv must be a real pytest invocation, not a path mention."""
    p = Proc(
        pid=9,
        ppid=1,
        cmdline="python tools/generate.py tests/test_pytest_plugin.py",
        cwd="/home/alice/repo/.worktrees/task-8",
    )
    assert is_lane_pytest(p) is False
    assert is_target(p) is False


def test_plain_pytest_outside_lane_paths_is_ignored():
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(pid=9, ppid=1, cmdline="pytest -q", cwd="/tmp/scratch"),
    ]
    assert classify_orphans(procs) == []


def test_is_keeper_excludes_sandbox_binary():
    assert is_keeper(Proc(pid=1, ppid=0, cmdline="/usr/bin/codex-linux-sandbox", cwd=LANE)) is False
    assert is_keeper(Proc(pid=2, ppid=1, cmdline="/usr/bin/codex exec --foo")) is True
    assert (
        is_keeper(Proc(pid=3, ppid=1, cmdline="/home/x/.codex/worktrees/a/bin/python -m pytest"))
        is False
    )


def test_reap_skips_when_starttime_changes(monkeypatch):
    """Devin #758: refuse SIGTERM if the PID was reused (starttime mismatch)."""
    orphan = Proc(
        pid=420,
        ppid=1,
        cmdline="pytest -q",
        cwd=LANE,
        starttime=1000,
    )
    monkeypatch.setattr("unified_agent.lane_reaper.read_proc_starttime", lambda pid: 9999)
    monkeypatch.setattr(
        "unified_agent.lane_reaper.read_proc_cmdline",
        lambda pid: "pytest -q",
    )
    monkeypatch.setattr("unified_agent.lane_reaper.read_proc_cwd", lambda pid: LANE)
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("unified_agent.lane_reaper.os.kill", fake_kill)
    report = reap([orphan], kill=True)
    assert killed == []
    assert report[0]["action"] == "skipped"
    assert report[0]["error"] == "pid-reused-or-gone"


def test_pid_still_matches_requires_live_target(monkeypatch):
    orphan = Proc(pid=7, ppid=1, cmdline="pytest -q", cwd=LANE, starttime=1)
    monkeypatch.setattr("unified_agent.lane_reaper.read_proc_starttime", lambda pid: 1)
    monkeypatch.setattr(
        "unified_agent.lane_reaper.read_proc_cmdline",
        lambda pid: "/bin/sleep 30",
    )
    monkeypatch.setattr("unified_agent.lane_reaper.read_proc_cwd", lambda pid: "/tmp")
    assert _pid_still_matches(orphan) is False


def test_bare_init_shell_pytest_is_left_alone():
    """Devin #758: live detached shell under init must not kill its pytest."""
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(pid=200, ppid=1, cmdline="zsh"),
        Proc(pid=201, ppid=200, cmdline="python -m pytest -q", cwd=LANE),
    ]
    assert classify_orphans(procs) == []


def test_sandbox_under_init_still_reaps_shell_pytest():
    """Sandbox→shell→pytest under init remains the primary reap path."""
    procs = [
        Proc(pid=1, ppid=0, cmdline="/sbin/init"),
        Proc(pid=100, ppid=1, cmdline="codex-linux-sandbox", cwd=LANE),
        Proc(pid=200, ppid=100, cmdline="zsh"),
        Proc(pid=201, ppid=200, cmdline="python -m pytest -q", cwd=LANE),
    ]
    assert [p.pid for p in classify_orphans(procs)] == [201, 100]
