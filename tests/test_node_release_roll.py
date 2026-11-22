"""scripts/node-release-roll.sh: gate before swap, roll back on a failed restart (task #2298).

Every test runs against a throwaway git repo and a fake HOME in tmp_path. git is
real (worktrees are the subject); `uv`, `systemctl`, `launchctl`, `ps` and `uname`
are fakes on PATH, so nothing touches a real service or a real home.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "node-release-roll.sh"

_FAKE_UV = """#!/usr/bin/env bash
echo "uv $*" >> "$FAKE_LOG"
[ "${FAKE_UV:-ok}" = fail ] && exit 1
mkdir -p .venv/bin
cat > .venv/bin/python <<'PY'
#!/usr/bin/env bash
echo "python $*" >> "$FAKE_LOG"
wt="$(cd "$(dirname "$0")/../.." && pwd -P)"
case "$*" in
  *__file__*)
    if [ "${FAKE_GATE:-ok}" = elsewhere ]; then echo /elsewhere/unified_agent/__init__.py
    else echo "$wt/src/unified_agent/__init__.py"; fi ;;
  *metadata*)
    [ "${FAKE_GATE:-ok}" = noversion ] && { echo PackageNotFoundError >&2; exit 1; }
    echo 0.0.0 ;;
  *) exit 2 ;;
esac
PY
chmod +x .venv/bin/python
"""

# Healthy unless the release link points at a target containing $FAKE_BAD.
_HEALTH = """
target="$(readlink "$HOME/.unified_agent/release/current" 2>/dev/null || true)"
healthy() { [ -z "${FAKE_BAD:-}" ] || [[ "$target" != *"$FAKE_BAD"* ]]; }
"""

# An installed executor is a directory $FAKE_EXECUTORS/<unit or label> holding its
# pid and cmd, plus children (a count), running, restart_fails, and after (the
# command line a restart produces; without it a restart does not change the pid).
_EXECUTOR_RESTART = r"""
exec_restart() {
  if [ -n "${FAKE_EXEC_HANG:-}" ]; then touch "$FAKE_EXEC_HANG"; sleep 2; fi
  [ ! -e "$dir/restart_fails" ] || exit 1
  [ -e "$dir/after" ] || return 0
  echo "$(($(cat "$dir/pid") + 1))" > "$dir/pid"
  after="$(cat "$dir/after")"
  echo "${after//TARGET/$target}" > "$dir/cmd"
}
"""

_FAKE_SYSTEMCTL = (
    """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
"""
    + _HEALTH
    + _EXECUTOR_RESTART
    + r"""
unit="${*: -1}"
if [[ $unit == unified-agent-executor* ]]; then
  dir="$FAKE_EXECUTORS/$unit"
  case "$*" in
    *"show -p LoadState"*) if [ -d "$dir" ]; then echo loaded; else echo not-found; fi ;;
    *"show -p MainPID"*) cat "$dir/pid" ;;
    *is-active*) [ -e "$dir/running" ] || exit 3 ;;
    *restart*) exec_restart ;;
  esac
  exit 0
fi
"""
    + """
case "$*" in
  *"show -p ExecStart"*)
    echo "{ path=${FAKE_EXEC:-$HOME/.unified_agent/release/current}/.venv/bin/unified-agent ;"\
" argv[]=x node ; }" ;;
  *"show -p MainPID"*) echo 4242 ;;
  *is-active*) healthy || exit 3 ;;
  *restart*)
    # FAKE_HANG: the first restart blocks, so a test can signal the roll mid-restart.
    if [ -n "${FAKE_HANG:-}" ] && [ ! -e "$FAKE_HANG" ]; then touch "$FAKE_HANG"; sleep 2; fi
    exit "${FAKE_RESTART_RC:-0}" ;;
esac
"""
)

_FAKE_LAUNCHCTL = (
    """#!/usr/bin/env bash
echo "launchctl $*" >> "$FAKE_LOG"
"""
    + _HEALTH
    + _EXECUTOR_RESTART
    + r"""
label="${*: -1}"
label="${label##*/}"
if [[ $label == com.unified-agent.executor-* ]]; then
  dir="$FAKE_EXECUTORS/$label"
  [ -d "$dir" ] || exit 113
  case "$1" in
    print)
      if [ -e "$dir/running" ]; then echo "state = running"; echo "pid = $(cat "$dir/pid")"
      else echo "state = waiting"; fi ;;
    kickstart) exec_restart ;;
  esac
  exit 0
fi
"""
    + """
case "$1" in
  print)
    echo "program = ${FAKE_EXEC:-$HOME/.unified_agent/release/current}/.venv/bin/unified-agent"
    if healthy; then echo "state = running"; echo "pid = 4242"; else echo "state = waiting"; fi ;;
  kickstart) exit "${FAKE_RESTART_RC:-0}" ;;
esac
"""
)

_FAKE_UNAME = """#!/usr/bin/env bash
echo "${FAKE_UNAME:-Linux}"
"""

# Only the BSD-portable spelling (one keyword per -o) matches; anything else is real ps.
_FAKE_PS = r"""#!/usr/bin/env bash
case "$*" in
  "-axo ppid= -o pid=")
    [ "${FAKE_PS:-ok}" = ok ] || exit 1
    echo "1 2"
    for d in "$FAKE_EXECUTORS"/*/; do
      [ -e "$d/children" ] || continue
      for ((i = 1; i <= $(cat "$d/children"); i++)); do echo "$(cat "$d/pid") $((9000 + i))"; done
    done ;;
  "-o command= -p "*)
    for d in "$FAKE_EXECUTORS"/*/; do
      if [ "$(cat "$d/pid" 2>/dev/null)" = "$4" ]; then cat "$d/cmd"; exit 0; fi
    done
    exit 1 ;;
  *) PATH=/usr/bin:/bin exec ps "$@" ;;
esac
"""


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    (repo / "f.txt").write_text(msg)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


# A private agy server is a real socket at <base>/ua-agy-i-*/tmux.sock; the files
# beside it say what this client reports: `sessions` (names) or `error` (message).
_FAKE_TMUX = r"""#!/usr/bin/env bash
echo "tmux $*" >> "$FAKE_LOG"
dir="$(dirname "$2")"
if [ -e "$dir/error" ]; then cat "$dir/error" >&2; exit 1; fi
cat "$dir/sessions"
"""


class Host:
    """A fake node host: repo with two release worktrees, the link on the older one."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        # Outside tmp_path: a Unix socket path must stay short.
        self.tmux_base = Path(tempfile.mkdtemp(prefix="uart", dir="/tmp"))
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text(".venv/\n")
        self.old_sha = _commit(self.repo, "one")
        self.new_sha = _commit(self.repo, "two")
        self.stale = self._worktree("release-20260901-aaaaaaaa", self.old_sha)
        self.old = self._worktree("release-20261001-bbbbbbbb", self.old_sha)
        self.release = self.home / ".unified_agent" / "release"
        self.release.mkdir(parents=True)
        self.link = self.release / "current"
        self.link.symlink_to(self.old)
        self.new = self.repo / ".worktrees" / f"release-{date.today():%Y%m%d}-{self.new_sha[:8]}"
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("uv", _FAKE_UV),
            ("systemctl", _FAKE_SYSTEMCTL),
            ("launchctl", _FAKE_LAUNCHCTL),
            ("uname", _FAKE_UNAME),
            ("ps", _FAKE_PS),
            ("tmux", _FAKE_TMUX),
        ):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def _worktree(self, name: str, sha: str) -> Path:
        path = self.repo / ".worktrees" / name
        _git(self.repo, "worktree", "add", "-q", "--detach", str(path), sha)
        return path.resolve()

    def env(self, **env: str) -> dict[str, str]:
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        return {
            "PATH": os.pathsep.join([str(self.bin), git_dir, "/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "LANG": "C",
            "FAKE_LOG": str(self.log),
            "FAKE_EXECUTORS": str(self.root / "executors"),
            "UA_ROLL_TMUX_BASE": str(self.tmux_base),
            "UA_ROLL_WAIT_SECONDS": "2",
            "UA_ROLL_SETTLE_SECONDS": "0",
            **env,
        }

    def argv(self, *args: str) -> list[str]:
        return ["bash", str(SCRIPT), "--repo", str(self.repo), *args]

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.argv(*args), capture_output=True, text=True, env=self.env(**env), timeout=60
        )

    def calls(self) -> str:
        return self.log.read_text()

    def add_executor(
        self,
        name: str,
        *,
        cmd: str | None = None,
        after: str | None = "default",
        children: int = 0,
        running: bool = True,
        pid: int = 7000,
    ) -> Path:
        """Install a fake executor, by default idle and running the old release."""
        path = self.root / "executors" / name
        path.mkdir(parents=True)
        (path / "pid").write_text(f"{pid}\n")
        (path / "cmd").write_text((cmd or self.exec_cmd(self.old)) + "\n")
        (path / "children").write_text(f"{children}\n")
        if running:
            (path / "running").touch()
        if after == "default":
            after = self.exec_cmd("TARGET")
        if after is not None:
            (path / "after").write_text(after + "\n")
        return path

    def add_tmux_server(self, name: str, *, sessions: str = "", error: str = "") -> None:
        """A private agy tmux server, as agy_interactive leaves it during a run."""
        path = self.tmux_base / f"ua-agy-i-{name}"
        path.mkdir()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path / "tmux.sock"))
        (path / "sessions").write_text(sessions + "\n")
        if error:
            (path / "error").write_text(error + "\n")

    def exec_cmd(self, tree: Path | str) -> str:
        """The command line a Linux executor shows: real interpreter, link-named script."""
        return f"{tree}/.venv/bin/python {self.link}/.venv/bin/unified-agent-executor"

    def target(self) -> Path:
        return _link_target(self.link)


@pytest.fixture
def host(tmp_path):
    built = Host(tmp_path)
    yield built
    shutil.rmtree(built.tmux_base, ignore_errors=True)


def _link_target(link: Path) -> Path:
    """A symlink's target resolved against the link's own directory."""
    return (link.parent / os.readlink(link)).resolve()


def _out(res: subprocess.CompletedProcess) -> str:
    return res.stdout + res.stderr


def test_script_syntax_and_shellcheck():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_roll_swaps_link_records_previous_and_restarts(host):
    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert host.target() == host.new.resolve()
    assert _link_target(host.release / "previous") == host.old
    assert "systemctl --user restart ua-node" in host.calls()
    assert "uv sync --all-extras" in host.calls()
    # #2297: --version exits 1 on a hub-client host; it must never be the gate.
    assert "--version" not in host.calls()
    assert not list(host.release.glob("*.tmp.*"))


@pytest.mark.parametrize(("knob", "value"), [("FAKE_GATE", "elsewhere"), ("FAKE_UV", "fail")])
def test_failed_gate_never_swaps_or_restarts(host, knob, value):
    res = host.run("--sha", host.new_sha, **{knob: value})

    assert res.returncode != 0
    assert host.target() == host.old
    assert "restart" not in host.calls()
    assert not (host.release / "previous").exists()


def test_failed_restart_rolls_the_link_back(host):
    res = host.run("--sha", host.new_sha, FAKE_BAD=host.new_sha[:8])

    assert res.returncode != 0
    assert host.target() == host.old
    assert host.calls().count("systemctl --user restart ua-node") == 2
    assert "rolled back" in _out(res)
    assert not (host.release / "previous").exists()


def test_pinned_supervisor_is_refused(host):
    """A swap is pointless while the unit names a versioned path."""
    res = host.run("--sha", host.new_sha, FAKE_EXEC=str(host.old))

    assert res.returncode != 0
    assert "release/current" in _out(res)
    assert host.target() == host.old
    assert not host.new.exists()


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_dry_run_prints_steps_and_writes_nothing(host):
    before = _snapshot(host.root)
    log_before = host.calls()

    res = host.run("--sha", host.new_sha, "--dry-run")

    assert res.returncode == 0, _out(res)
    for step in ("worktree add", "uv sync --all-extras", "ln -s", "restart ua-node"):
        assert step in res.stdout, res.stdout
    assert _snapshot(host.root) == before
    assert "restart" not in host.calls()
    assert "uv sync" not in host.calls().replace(log_before, "")


def test_check_only_reports_and_exits_zero(host):
    res = host.run("--check-only")

    assert res.returncode == 0, _out(res)
    assert str(host.old) in res.stdout
    assert "supervisor names release/current: yes" in res.stdout
    assert f"stale    {host.stale}" in res.stdout
    assert f"current  {host.old}" in res.stdout


def test_check_only_reports_a_missing_target_and_pinned_unit(host):
    shutil.rmtree(host.stale)
    host.link.unlink()
    host.link.symlink_to(host.stale)

    res = host.run("--check-only", FAKE_EXEC=str(host.old))

    assert res.returncode == 0, _out(res)
    assert "MISSING" in res.stdout
    assert "supervisor names release/current: no" in res.stdout


def test_retire_stale_never_lists_the_current_target(host):
    listed = host.run("--retire-stale")

    assert listed.returncode == 0, _out(listed)
    assert str(host.stale) in listed.stdout
    assert str(host.old) not in listed.stdout
    assert host.stale.exists()

    removed = host.run("--retire-stale", "--yes")

    assert removed.returncode == 0, _out(removed)
    assert not host.stale.exists()
    assert host.old.exists()


def test_retire_stale_refuses_while_the_unit_is_pinned(host):
    res = host.run("--retire-stale", "--yes", FAKE_EXEC=str(host.stale))

    assert res.returncode != 0
    assert host.stale.exists()


def test_macos_restarts_with_launchctl_kickstart(host):
    res = host.run("--sha", host.new_sha, FAKE_UNAME="Darwin")

    assert res.returncode == 0, _out(res)
    assert "launchctl kickstart -k gui/" in host.calls()
    assert "com.unified-agent.node" in host.calls()
    assert "systemctl" not in host.calls()
    assert host.target() == host.new.resolve()


def test_macos_failed_kickstart_names_the_normal_shell_constraint(host):
    res = host.run("--sha", host.new_sha, FAKE_UNAME="Darwin", FAKE_RESTART_RC="1")

    assert res.returncode != 0
    assert "normal login shell" in _out(res)
    assert host.target() == host.old


# Review fixes at 5e590cc3 (one regression test each).

_SLOW_GIT = """#!/usr/bin/env bash
# Real git, plus a late extra line on `worktree list`: a reader that stops early
# leaves this write to hit a closed pipe (SIGPIPE, 141 under pipefail).
"$REAL_GIT" "$@"
rc=$?
case "$*" in *"worktree list"*) sleep 0.3; echo ;; esac
exit "$rc"
"""


def test_large_worktree_inventory_never_dies_on_sigpipe(host):
    for i in range(105):
        _git(
            host.repo,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(host.repo / ".worktrees" / f"task-{i}"),
        )
    slow = host.root / "slowbin"
    slow.mkdir()
    (slow / "git").write_text(_SLOW_GIT)
    (slow / "git").chmod(0o755)
    path = os.pathsep.join([str(slow), host.env()["PATH"]])
    knobs = {"PATH": path, "REAL_GIT": shutil.which("git") or "/usr/bin/git"}

    for args in (["--check-only"], ["--retire-stale"], ["--sha", host.new_sha]):
        res = host.run(*args, **knobs)
        assert res.returncode == 0, (args, res.returncode, _out(res))
    assert host.target() == host.new.resolve()


def _hold_lock(host: Host) -> None:
    lock = host.release / ".roll.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))  # a live process holds it


def test_retire_and_roll_share_one_lock(host):
    _hold_lock(host)

    retire = host.run("--retire-stale", "--yes")
    roll = host.run("--sha", host.new_sha)

    assert retire.returncode != 0 and "lock" in _out(retire)
    assert host.stale.exists()
    assert roll.returncode != 0 and "lock" in _out(roll)
    assert not host.new.exists()  # refused before preparing a worktree
    assert "restart" not in host.calls()


def test_relative_previous_link_is_resolved_against_its_directory(host):
    (host.release / "previous").symlink_to(os.path.relpath(host.stale, host.release))

    res = host.run("--retire-stale", "--yes")

    assert res.returncode == 0, _out(res)
    assert host.stale.exists()


def test_supervisor_must_name_this_hosts_exact_link(host):
    """Another home's release/current is not this host's link."""
    other = host.root / "other" / ".unified_agent" / "release" / "current"
    other.parent.mkdir(parents=True)
    other.symlink_to(host.stale)

    roll = host.run("--sha", host.new_sha, FAKE_EXEC=str(other))
    retire = host.run("--retire-stale", "--yes", FAKE_EXEC=str(other))
    check = host.run("--check-only", FAKE_EXEC=str(other))

    assert roll.returncode != 0
    assert host.target() == host.old
    assert retire.returncode != 0
    assert host.stale.exists()
    assert "supervisor names release/current: no" in check.stdout
    assert f"pinned   {host.stale}" in check.stdout  # the supervisor's tree, resolved


def test_sigterm_after_the_swap_rolls_back(host):
    marker = host.root / "hung"
    proc = subprocess.Popen(
        host.argv("--sha", host.new_sha),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=host.env(FAKE_HANG=str(marker)),
    )
    for _ in range(300):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    proc.terminate()
    out, _ = proc.communicate(timeout=60)

    assert proc.returncode != 0, out
    assert host.target() == host.old
    assert not (host.release / ".roll.txn").exists()
    assert not (host.release / ".roll.lock").exists()


def test_killed_roll_is_recovered_on_the_next_run(host):
    marker = host.root / "hung"
    proc = subprocess.Popen(
        host.argv("--sha", host.new_sha),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=host.env(FAKE_HANG=str(marker)),
    )
    for _ in range(300):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    proc.kill()  # no trap runs: the next run must recover
    proc.wait(timeout=10)
    assert host.target() == host.new.resolve()

    check = host.run("--check-only")
    res = host.run("--sha", host.new_sha)

    assert check.returncode == 0 and "interrupted roll" in check.stdout
    assert res.returncode == 0, _out(res)
    assert "interrupted" in _out(res)
    assert host.target() == host.new.resolve()
    assert _link_target(host.release / "previous") == host.old
    assert not (host.release / ".roll.txn").exists()


def test_already_current_target_is_health_checked(host):
    _git(host.repo, "worktree", "add", "-q", "--detach", str(host.new), host.new_sha)
    host.link.unlink()
    host.link.symlink_to(host.new)

    res = host.run("--sha", host.new_sha, FAKE_BAD=host.new_sha[:8])

    assert res.returncode != 0, _out(res)
    assert "syncing and restarting" in _out(res)
    assert "uv sync --all-extras" in host.calls()
    assert "gate: ok" in _out(res)
    assert "systemctl --user restart ua-node" in host.calls()


def test_sha_must_be_a_full_commit_id(host):
    res = host.run("--sha", "HEAD~1")

    assert res.returncode != 0
    assert "full 40- or 64-character" in _out(res)
    assert "restart" not in host.calls()


def test_dirty_release_worktree_is_never_reused(host):
    _git(host.repo, "worktree", "add", "-q", "--detach", str(host.new), host.new_sha)
    (host.new / "f.txt").write_text("edited in place")

    res = host.run("--sha", host.new_sha)

    assert res.returncode != 0
    assert "uncommitted" in _out(res)
    assert host.target() == host.old
    assert "uv sync" not in host.calls()
    assert "restart" not in host.calls()


REPO_ROOT = SCRIPT.parents[1]


def test_systemd_template_satisfies_the_supervisor_check(host):
    """#2322: the shipped ua-node.service ExecStart is the path the roll requires."""
    unit = (REPO_ROOT / "deploy" / "systemd" / "ua-node.service").read_text(encoding="utf-8")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    exe = exec_line.removeprefix("ExecStart=").split()[0].replace("%h", str(host.home))
    assert exe == f"{host.home}/.unified_agent/release/current/.venv/bin/unified-agent"

    res = host.run("--check-only", FAKE_EXEC=exe.removesuffix("/.venv/bin/unified-agent"))
    assert "supervisor names release/current: yes" in res.stdout, _out(res)


@needs_exported_paths(
    REPO_ROOT / "deploy" / "mac-hub" / "com.unified-agent.node.plist",
    REPO_ROOT / "deploy" / "mac-hub" / "render_launchagent.py",
    repo=REPO_ROOT,
)
def test_rendered_plist_template_satisfies_the_supervisor_check(host):
    """#2322: an installed node plist rendered from the template passes on macOS,
    read from the plist file when launchd reports no program."""
    template = REPO_ROOT / "deploy" / "mac-hub" / "com.unified-agent.node.plist"
    agents = host.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    renderer = REPO_ROOT / "deploy" / "mac-hub" / "render_launchagent.py"
    dest = agents / "com.unified-agent.node.plist"
    subprocess.run(
        [sys.executable, str(renderer), "--home", str(host.home), str(template), str(dest)],
        check=True,
        capture_output=True,
    )
    (host.bin / "launchctl").write_text('#!/usr/bin/env bash\necho "launchctl $*" >> "$FAKE_LOG"\n')

    res = host.run("--check-only", FAKE_UNAME="Darwin")
    assert "supervisor names release/current: yes" in res.stdout, _out(res)


# Executors after the node roll (task #2354).

LINUX_EXECUTOR = "unified-agent-executor.service"
LINUX_CODEX_EXECUTOR = "unified-agent-executor-codex.service"
MAC_AGY = "com.unified-agent.executor-agy"
MAC_CODEX = "com.unified-agent.executor-codex"
EXECUTORS_NOT_ROLLED = 3


def test_idle_executor_is_restarted_after_the_node_and_verified(host):
    executor = host.add_executor(LINUX_EXECUTOR)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert host.target() == host.new.resolve()
    calls = host.calls()
    node_restart = calls.index("systemctl --user restart ua-node")
    assert calls.index(f"systemctl --user restart {LINUX_EXECUTOR}") > node_restart
    assert (executor / "pid").read_text().strip() == "7001"
    assert f"{LINUX_EXECUTOR}: on {host.new_sha[:8]}" in res.stdout


def test_no_executor_installed_is_not_an_error(host):
    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert f"{LINUX_EXECUTOR}: not installed" in res.stdout
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


def test_linux_codex_executor_is_rolled_when_it_is_the_one_installed(host):
    codex = host.add_executor(LINUX_CODEX_EXECUTOR)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert (codex / "pid").read_text().strip() == "7001"
    assert f"{LINUX_CODEX_EXECUTOR}: on {host.new_sha[:8]}" in res.stdout
    assert f"{LINUX_EXECUTOR}: not installed" in res.stdout


def test_detached_tmux_run_of_this_executor_is_busy(host):
    """A tmux-backed agy run has no direct child: the server daemonized away."""
    executor = host.add_executor(LINUX_EXECUTOR)
    host.add_tmux_server("run", sessions="ua-agy-i-7000-123456")

    res = host.run("--sha", host.new_sha)

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert LINUX_EXECUTOR in res.stderr and "BUSY" in res.stderr
    assert "ua-agy-i-7000-123456" in res.stderr
    assert (executor / "pid").read_text().strip() == "7000"
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()
    assert host.target() == host.new.resolve()


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"sessions": "ua-agy-i-6999-123456"}, id="another-executors-run"),
        pytest.param({"error": "no server running on x"}, id="dead-server-left-its-socket"),
    ],
)
def test_tmux_server_that_is_not_this_executors_run_is_idle(host, server):
    executor = host.add_executor(LINUX_EXECUTOR)
    host.add_tmux_server("other", **server)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert (executor / "pid").read_text().strip() == "7001"


@pytest.mark.parametrize(
    ("server", "knobs"),
    [
        pytest.param(
            {"error": "error connecting to x (Permission denied)"}, {}, id="server-unreachable"
        ),
        pytest.param({"sessions": "unexpected-name"}, {}, id="session-cannot-be-attributed"),
        pytest.param(None, {"UA_ROLL_TMUX_BASE": "/nonexistent"}, id="socket-base-unusable"),
    ],
)
def test_undeterminable_tmux_state_counts_as_busy(host, server, knobs):
    executor = host.add_executor(LINUX_EXECUTOR)
    if server:
        host.add_tmux_server("unknown", **server)

    res = host.run("--sha", host.new_sha, **knobs)

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert LINUX_EXECUTOR in res.stderr and "BUSY" in res.stderr
    assert (executor / "pid").read_text().strip() == "7000"
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


def test_stopped_executor_is_left_stopped(host):
    host.add_executor(LINUX_EXECUTOR, running=False)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert "left stopped" in res.stdout
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


def test_macos_verifies_a_real_directory_and_a_resolved_link(host):
    link_only = f"{host.link}/.venv/bin/unified-agent-executor"
    host.add_executor(MAC_AGY, cmd=link_only, after="TARGET/.venv/bin/unified-agent-executor")
    host.add_executor(MAC_CODEX, cmd=link_only, after=link_only, pid=8000)

    res = host.run("--sha", host.new_sha, FAKE_UNAME="Darwin")

    assert res.returncode == 0, _out(res)
    for label in (MAC_AGY, MAC_CODEX):
        assert f"kickstart -k gui/{os.getuid()}/{label}" in host.calls()
    assert (
        f"{MAC_AGY}: on {host.new_sha[:8]} (command line names {host.new.resolve()})" in res.stdout
    )
    assert f"{MAC_CODEX}: on {host.new_sha[:8]} (command line shows release/current" in res.stdout
    assert "systemctl" not in host.calls()


def test_busy_executor_fails_loudly_and_keeps_the_node_roll(host):
    busy = host.add_executor(MAC_AGY, children=2)
    idle = host.add_executor(MAC_CODEX, pid=8000)

    res = host.run("--sha", host.new_sha, FAKE_UNAME="Darwin")

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert MAC_AGY in res.stderr and "BUSY" in res.stderr
    assert f"kickstart -k gui/{os.getuid()}/{MAC_AGY}" not in host.calls()
    assert (busy / "pid").read_text().strip() == "7000"
    assert (idle / "pid").read_text().strip() == "8001"
    # The node roll stands: link, previous and transaction are all finished.
    assert host.target() == host.new.resolve()
    assert _link_target(host.release / "previous") == host.old
    assert not (host.release / ".roll.txn").exists()
    assert not (host.release / ".roll.lock").exists()
    assert host.new_sha in res.stderr  # names the command that finishes the job


def test_rerun_after_busy_restarts_only_what_is_left(host):
    busy = host.add_executor(LINUX_EXECUTOR, children=1)
    first = host.run("--sha", host.new_sha)
    assert first.returncode == EXECUTORS_NOT_ROLLED, _out(first)

    (busy / "children").write_text("0\n")
    host.log.write_text("")
    second = host.run("--sha", host.new_sha)

    assert second.returncode == 0, _out(second)
    assert "systemctl --user restart ua-node" not in host.calls()
    assert host.calls().count(f"restart {LINUX_EXECUTOR}") == 1

    host.log.write_text("")
    third = host.run("--sha", host.new_sha)

    assert third.returncode == 0, _out(third)
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()  # already on it, by name
    assert "already on" in third.stdout


def test_busy_executor_already_on_the_release_needs_nothing(host):
    _git(host.repo, "worktree", "add", "-q", "--detach", str(host.new), host.new_sha)
    host.link.unlink()
    host.link.symlink_to(host.new)
    host.add_executor(LINUX_EXECUTOR, cmd=host.exec_cmd(host.new.resolve()), children=1)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == 0, _out(res)
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"after": None}, id="restart-left-the-old-pid"),
        pytest.param({"after": "OLD"}, id="new-pid-still-on-the-old-release"),
        pytest.param({"after": "/elsewhere/.venv/bin/python x"}, id="new-pid-other-tree"),
    ],
)
def test_executor_not_on_the_release_after_restart_is_reported(host, setup):
    after = setup["after"]
    host.add_executor(LINUX_EXECUTOR, after=host.exec_cmd(host.old) if after == "OLD" else after)

    res = host.run("--sha", host.new_sha)

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert LINUX_EXECUTOR in res.stderr
    assert host.target() == host.new.resolve()


def test_failed_executor_restart_does_not_undo_the_node_roll(host):
    executor = host.add_executor(LINUX_EXECUTOR)
    (executor / "restart_fails").touch()

    res = host.run("--sha", host.new_sha)

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert LINUX_EXECUTOR in res.stderr
    assert host.target() == host.new.resolve()
    assert host.calls().count("systemctl --user restart ua-node") == 1


def test_unknown_child_count_is_not_idle(host):
    executor = host.add_executor(LINUX_EXECUTOR)

    res = host.run("--sha", host.new_sha, FAKE_PS="fail")

    assert res.returncode == EXECUTORS_NOT_ROLLED, _out(res)
    assert LINUX_EXECUTOR in res.stderr and "BUSY" in res.stderr
    assert (executor / "pid").read_text().strip() == "7000"
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


@pytest.mark.parametrize("children", [0, 1], ids=["idle", "busy"])
def test_dry_run_reports_executor_steps_and_writes_nothing(host, children):
    host.add_executor(LINUX_EXECUTOR, children=children)
    before = _snapshot(host.root)

    res = host.run("--sha", host.new_sha, "--dry-run")

    assert res.returncode == 0, _out(res)
    assert ("BUSY" in _out(res)) == bool(children)
    planned = f"[dry-run] systemctl --user restart {LINUX_EXECUTOR}" in res.stdout
    assert planned == (not children)
    assert LINUX_EXECUTOR in _out(res)
    assert _snapshot(host.root) == before
    assert f"restart {LINUX_EXECUTOR}" not in host.calls()


def test_sigterm_during_executor_restart_keeps_the_node_roll(host):
    marker = host.root / "hung"
    host.add_executor(LINUX_EXECUTOR)
    proc = subprocess.Popen(
        host.argv("--sha", host.new_sha),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=host.env(FAKE_EXEC_HANG=str(marker)),
    )
    for _ in range(300):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    proc.terminate()
    out, _ = proc.communicate(timeout=60)

    assert proc.returncode == EXECUTORS_NOT_ROLLED, out
    assert "nothing switched" not in out
    assert host.target() == host.new.resolve()
    assert not (host.release / ".roll.lock").exists()
    assert not (host.release / ".roll.txn").exists()
