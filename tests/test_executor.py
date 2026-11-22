"""Unit tests for the Active Executor Daemon (Phase 4.2)."""

import logging
import os
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from product_repo_env import point_default_project_at

from unified_agent import config, lease_reaper, storage
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import cycles as cycles_tool
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import pi_runner as pi_runner_tool
from unified_agent.tools import tasks as tasks_tool

TOMBSTONE_DB_TEXT = "AUTHORITATIVE memory.db is on Mac hub only. Hub: http://100.64.0.10:8765/mcp\n"


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Use an isolated temporary SQLite database for tests."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    # These tests are the standalone case the in-process fallback exists for:
    # there is no hub to talk to, so they opt in explicitly, exactly as a
    # non-production run must. Production defaults to requiring the hub.
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    # Point at a closed port. Otherwise the client reaches whatever hub happens
    # to be listening on the developer's machine and gets a 401 — which is how
    # these tests used to "pass": the auth rejection was swallowed into the
    # in-process fallback. Test outcomes must not depend on a live local hub.
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    # These tests stub `pi_run_task`. Pi is an explicit alias now; Desktop_agent3's
    # default backend is the native `agy` CLI. Opt into the alias here.
    monkeypatch.setenv("UA_EXECUTOR_BACKEND", "pi")
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

    storage._connection = None
    storage.init_db()
    yield
    if storage._connection:
        storage._connection.close()
        storage._connection = None


def test_atomic_claim_race_condition():
    """Verify atomic task claim race condition: only one winner claims a task."""
    # Create task
    t = tasks_tool.task_add(title="Race task", notes="claimed-by: Desktop_Agent5")
    task_id = t["id"]

    # First agent claims task
    res1 = tasks_tool.task_claim(task_id, agent="Desktop_agent3")
    assert res1["claimed_by"] == "Desktop_agent3"
    assert res1["status"] == "doing"

    # Second agent is told to wait; only one winner holds the task (#2388)
    res2 = tasks_tool.task_claim(task_id, agent="Laptop_Agent1")
    assert res2["claimed"] is False
    assert res2["safe_wait"]["owner"] == "Desktop_agent3"


def _delegated_to_a_held_task() -> tuple[dict, int]:
    created = delegate_tool.delegate(
        task_spec="Contested", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    held = tasks_tool.task_claim(
        created["task_id"], "Laptop_Agent1", expected_lane="Desktop_agent3"
    )
    assert held["claimed_by"] == "Laptop_Agent1"
    return created, created["delegation_id"]


def _delegation_status(delegation_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT status FROM delegations WHERE id=?", (delegation_id,)
        ).fetchone()[0]
    finally:
        conn.close()


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_poll_treats_safe_wait_as_not_claimed_and_releases_the_delegation(mock_pi_run):
    """A returned safe_wait must take the same exit a ConflictError took (#2388)."""
    _, delegation_id = _delegated_to_a_held_task()
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

    assert daemon.poll_once() is None

    mock_pi_run.assert_not_called()
    assert _delegation_status(delegation_id) == "pending"


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_an_executor_that_misses_safe_wait_still_cannot_run_the_task(mock_pi_run):
    """Version skew: the claim-result reader an older daemon reaches runs no work."""
    created, delegation_id = _delegated_to_a_held_task()
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    taken = delegate_tool.delegate_claim(delegation_id, "Desktop_agent3")
    waited = tasks_tool.task_claim(created["task_id"], "Desktop_agent3")
    assert waited["claimed"] is False

    res = daemon._execute_claimed_task(taken["delegation"], waited)

    assert res["status"] == "missing_run_token"
    mock_pi_run.assert_not_called()
    # The held task has a run, so the old retire path is refused: stuck in_flight.
    assert _delegation_status(delegation_id) == "in_flight"


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_poll_never_picks_up_interrupted_work_from_its_queued_delegation(mock_pi_run):
    """#2482: blocked, unowned work is not claimed through a delegation queued for it."""
    created = delegate_tool.delegate(
        task_spec="Interrupted", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    task_id, delegation_id = created["task_id"], created["delegation_id"]
    assert delegate_tool.delegate_claim(delegation_id, "Desktop_agent3")["claimed"]
    tasks_tool.task_claim(task_id, "Desktop_agent3")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task_id,)
        )
        conn.execute("DELETE FROM agents WHERE label='Desktop_agent3'")
        [reaped] = lease_reaper.reap_stale_claim_leases(conn)
        assert reaped.to_status == "blocked"
        conn.execute(
            "UPDATE delegations SET status='pending', attempts=0, last_attempt_at=NULL WHERE id=?",
            (delegation_id,),
        )
        conn.commit()
    finally:
        conn.close()
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

    assert daemon.find_pending_work() == []
    assert daemon.poll_once() is None
    # An executor that lists the delegation anyway is refused at the claim itself.
    with pytest.raises(ToolError, match="held as interrupted work"):
        tasks_tool.task_claim(task_id, "Desktop_agent3")

    mock_pi_run.assert_not_called()
    assert _delegation_status(delegation_id) == "pending"
    row = tasks_tool.task_list(status="blocked")["tasks"][0]
    assert (row["id"], row["claimed_by"]) == (task_id, None)


def test_lock_renewal_fallback_does_not_read_safe_wait_as_renewed(monkeypatch):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

    def retired_store():
        raise storage.LiveStoreError("local task store retired")

    class FakeHub:
        authoritative_run_checks = False

        def call_tool(self, name, arguments):
            if name == "task_claim":
                return {"id": 42, "claimed": False, "safe_wait": {"owner": "Laptop_Agent1"}}
            return {"runs": []}

    daemon.hub_client = FakeHub()
    monkeypatch.setattr(storage, "connect", retired_store)
    assert daemon.renew_lock(42) is False


def test_a_hostile_title_cannot_influence_the_worktree_path(isolated_env):
    """The worktree path is `.worktrees/task-<id>` — no title component at all.

    The daemon used to build `.worktrees/task-<id>-<slug>` from the title and
    needed an explicit traversal guard. Now that it uses the worktree
    task_claim created, the path is derived from an integer id and a hostile
    title cannot reach it (task #178).
    """
    t = tasks_tool.task_add(title="../../../etc/passwd")

    claimed = tasks_tool.task_claim(t["id"], agent="Desktop_agent3")

    assert ".." not in (claimed["worktree_path"] or "")
    assert claimed["worktree_path"] in (None, f".worktrees/task-{t['id']}")


class TestWorktreeBindsToTheWorkspaceRepo:
    """The worktree must be created in the workspace, not the daemon's cwd.

    create_task_worktree used to run `os.system("git worktree add -B ...")`, which
    inherits the *process* cwd. UA_WORKSPACE was irrelevant: git bound to whichever
    repository the daemon was started from, which sidestepped
    _ensure_repo_in_workspace entirely — the guard that exists to refuse repos
    outside the workspace root. Requirement 2 of the daemon (worktree sandbox
    confinement) failed open the same way requirement 1 did in PR #82.
    """

    def _git(self, *args, cwd):
        return subprocess.run(list(("git",) + args), cwd=cwd, capture_output=True, text=True)

    def _workspace_repo(self, workspace):
        """Make the workspace root itself a git repo with one commit."""
        self._git("init", "-b", "main", cwd=workspace)
        self._git("config", "user.name", "Test User", cwd=workspace)
        self._git("config", "user.email", "test@example.com", cwd=workspace)
        (workspace / "initial.txt").write_text("hello")
        self._git("add", "initial.txt", cwd=workspace)
        self._git("commit", "-m", "feat: initial commit", cwd=workspace)
        point_default_project_at(workspace)

    def _product_repo(self):
        repo = config.dev_root() / "unified_agent"
        repo.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", "main", cwd=repo)
        self._git("config", "user.name", "Test User", cwd=repo)
        self._git("config", "user.email", "test@example.com", cwd=repo)
        (repo / "product.txt").write_text("product source\n")
        self._git("add", "product.txt", cwd=repo)
        self._git("commit", "-m", "feat: initial commit", cwd=repo)
        point_default_project_at(repo)
        return repo

    class FakeBackend:
        name = "codex"
        session_kind = "codex_run"

        def __init__(self, *, status="success", write=True):
            self.status = status
            self.write = write
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            if self.write:
                cwd = Path(kwargs["cwd"])
                root = cwd if cwd.is_absolute() else Path(os.environ["UA_WORKSPACE"]) / cwd
                (root / "child.txt").write_text("child change\n")
            return {
                "status": self.status,
                "text": "ok" if self.status == "success" else "boom",
                "tools_used": [],
                "files_modified": ["child.txt"] if self.write else [],
                "events_count": 1,
                "exit_code": 0 if self.status == "success" else 1,
                "error": None if self.status == "success" else "boom",
            }

    def _delegate_code_task(self):
        return delegate_tool.delegate(
            task_spec="[code-task]\n\nChange a file.",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_Agent2",
            callback="none",
        )

    def _task_row(self, task_id):
        conn = storage.connect()
        try:
            return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()

    def test_daemon_commits_dirty_successful_child_run_and_records_sha(self, isolated_env):
        self._workspace_repo(isolated_env)
        daemon = ExecutorDaemon(
            agent_label="Desktop_Agent2",
            shadow_mode=False,
            backend=self.FakeBackend(write=True),
        )
        task_id = self._delegate_code_task()["task_id"]

        res = daemon.poll_once()

        row = self._task_row(task_id)
        worktree = isolated_env / row["worktree_path"]
        assert str(worktree) in self._git("worktree", "list", cwd=isolated_env).stdout
        assert self._git("rev-list", "--count", "main..HEAD", cwd=worktree).stdout.strip() == "1"
        sha = self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        assert self._git("show", "HEAD:child.txt", cwd=worktree).stdout == "child change\n"
        assert res["commit"] == {"status": "committed", "reason": None, "sha": sha}
        assert row["evidence_ref"] == sha
        body = self._git("log", "-1", "--format=%B", cwd=worktree).stdout
        assert "Child agent: Desktop_Agent2" in body
        assert "child sandbox blocked git metadata writes" in body

    def test_failed_child_run_never_gets_daemon_commit(self, isolated_env):
        self._workspace_repo(isolated_env)
        daemon = ExecutorDaemon(
            agent_label="Desktop_Agent2",
            shadow_mode=False,
            backend=self.FakeBackend(status="error", write=True),
        )
        task_id = self._delegate_code_task()["task_id"]

        res = daemon.poll_once()

        row = self._task_row(task_id)
        worktree = isolated_env / row["worktree_path"]
        assert res["status"] == "error"
        assert self._git("rev-list", "--count", "main..HEAD", cwd=worktree).stdout.strip() == "0"
        assert row["evidence_ref"] is None

    def test_clean_successful_child_run_is_zero_output_terminal_failure(self, isolated_env):
        """Backend success + clean worktree must fail loud (task #1158).

        Previously this stamped [waiting-for: review] with evidence_ref=None and
        looked like progress — that is how #1115 burned 5/5 attempts with no
        commits. zero_output_attempt is terminal: no retry budget burn.
        """
        self._workspace_repo(isolated_env)
        daemon = ExecutorDaemon(
            agent_label="Desktop_Agent2",
            shadow_mode=False,
            backend=self.FakeBackend(write=False),
        )
        task_id = self._delegate_code_task()["task_id"]

        res = daemon.poll_once()

        row = self._task_row(task_id)
        worktree = isolated_env / row["worktree_path"]
        assert res["status"] == "zero_output_attempt"
        assert res["failed_terminal"] is True
        assert res["commit"] == {"status": "refused", "reason": "worktree clean", "sha": None}
        assert self._git("rev-list", "--count", "main..HEAD", cwd=worktree).stdout.strip() == "0"
        assert row["evidence_ref"] is None
        assert "zero_output_attempt" in (row["notes"] or "")
        failed = delegate_tool.delegate_list(status="failed")["delegations"]
        assert len(failed) == 1
        assert "zero_output_attempt" in (failed[0]["callback_result"] or "")
        # A second poll must not retry — budget not consumed by silent no-ops.
        assert daemon.poll_once() is None

    def test_worktree_lands_in_the_workspace_not_the_process_cwd(self, isolated_env):
        self._workspace_repo(isolated_env)
        t = tasks_tool.task_add(title="full execution lifecycle test")

        claimed = tasks_tool.task_claim(t["id"], agent="Desktop_agent3")

        wt_rel = claimed["worktree_path"]
        assert wt_rel == str(isolated_env.resolve() / f".worktrees/task-{t['id']}")
        assert (isolated_env / wt_rel).is_dir()
        # The registration belongs to the workspace repo, and to no other.
        listed = self._git("worktree", "list", cwd=isolated_env).stdout
        assert str(isolated_env / wt_rel) in listed
        assert claimed["branch"] in self._git("branch", "--list", cwd=isolated_env).stdout

    def test_code_task_product_worktree_recorded_and_backend_cwd(self, isolated_env):
        product_repo = self._product_repo()
        backend = self.FakeBackend(write=False)
        assert product_repo != isolated_env
        daemon = ExecutorDaemon(
            agent_label="Desktop_Agent2",
            shadow_mode=False,
            backend=backend,
        )
        task_id = self._delegate_code_task()["task_id"]

        res = daemon.poll_once()

        row = self._task_row(task_id)
        expected = row["worktree_path"]
        assert Path(expected).parent == product_repo / ".worktrees"
        assert Path(expected).name.startswith(f"task-{task_id}-run-")
        assert "-delegation-" in Path(expected).name
        assert res["worktree"] == expected
        assert backend.calls[0]["cwd"] == expected
        assert Path(expected).is_dir()
        assert (Path(expected) / "product.txt").read_text() == "product source\n"
        assert not (isolated_env / ".worktrees").exists()
        assert expected in self._git("worktree", "list", cwd=product_repo).stdout

    def test_no_worktree_reported_when_workspace_is_not_a_repo(self, isolated_env):
        """A non-git workspace is supported — but it must not report a worktree."""
        t = tasks_tool.task_add(title="no repo here")
        claimed = tasks_tool.task_claim(t["id"], agent="Desktop_agent3")

        # The old code sent the exit status to /dev/null and returned the path
        # anyway, so a task with no sandbox was indistinguishable from one with.
        assert claimed["worktree_path"] is None
        assert claimed["provisioning"] == {"status": "skipped", "reason": "no_product_repo"}
        assert not (isolated_env / ".worktrees").exists()

    def test_one_canonical_active_checkout_with_legacy_checkout_preserved(
        self, isolated_env, monkeypatch
    ):
        """The backend, sandbox and merger bind to the same canonical attempt tree.

        Preserve the initial claim checkout while isolating the delegated attempt
        by run and generation. The old tree must never become a second active
        execution context as it did in task #178.
        """
        self._workspace_repo(isolated_env)
        seen = {}

        def fake_pi_run_task(**kwargs):
            seen.update(kwargs)
            return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run_task)
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        delegate_tool.delegate(
            task_spec="Smoke", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )

        res = daemon.poll_once()

        worktrees = [
            ln
            for ln in self._git("worktree", "list", cwd=isolated_env).stdout.splitlines()
            if ".worktrees/" in ln
        ]
        assert len(worktrees) == 2, f"expected preserved claim + attempt trees, got {worktrees}"
        assert sum("-generation-" in entry for entry in worktrees) == 1
        task_branches = [
            b
            for b in self._git("branch", "--list", cwd=isolated_env).stdout.splitlines()
            if "task" in b
        ]
        assert len(task_branches) == 2, (
            f"expected preserved claim + attempt branches, got {task_branches}"
        )
        assert sum("-generation-" in entry for entry in task_branches) == 1

        # The DB, the sandbox resolver and Pi must all name the SAME directory.
        row = tasks_tool.task_list(status="doing")["tasks"][0]
        assert res["worktree"] == row["worktree_path"]
        assert seen["cwd"] == row["worktree_path"]
        assert (isolated_env / row["worktree_path"]).is_dir()

    def test_the_agents_sandbox_resolves_to_the_worktree_pi_ran_in(self, isolated_env, monkeypatch):
        """get_active_worktree_path drives every fs_*/run_cmd call the agent
        makes; it pointed at the empty worktree while the work was elsewhere."""
        from unified_agent.safety import get_active_worktree_path

        self._workspace_repo(isolated_env)
        monkeypatch.setattr(
            pi_runner_tool,
            "pi_run_task",
            lambda **kw: {"status": "success", "text": "ok", "exit_code": 0, "error": None},
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        delegate_tool.delegate(
            task_spec="Smoke", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )
        res = daemon.poll_once()

        sandbox = get_active_worktree_path("Desktop_agent3")
        assert sandbox is not None
        assert sandbox == (isolated_env / res["worktree"]).resolve()

    def test_execute_runs_in_workspace_root_and_records_the_degradation(
        self, isolated_env, monkeypatch
    ):
        """No worktree => Pi gets the workspace root, not a path nobody created."""
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        seen = {}

        def fake_pi_run_task(**kwargs):
            seen.update(kwargs)
            return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run_task)

        del_res = delegate_tool.delegate(
            task_spec="Task with no worktree",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        res = daemon.poll_once()

        assert res is not None and res["task_id"] == del_res["task_id"]
        assert res["worktree"] is None
        assert seen["cwd"] == "."


def test_ensure_task_worktree_leaves_columns_null_on_failure(isolated_env):
    """task_claim still succeeds without git, but must not claim a worktree it has not got."""
    t = tasks_tool.task_add(title="No git here")

    claimed = tasks_tool.task_claim(t["id"], agent="Desktop_agent3")

    assert claimed["status"] == "doing"
    assert claimed["worktree_path"] is None
    assert claimed["branch"] is None


def test_shadow_mode_passivity(tmp_path, monkeypatch):
    """Verify shadow mode logs actions and executes zero DB mutations or subprocesses."""
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=True)

    # Delegate a task to Desktop_agent3
    del_res = delegate_tool.delegate(
        task_spec="Perform shadow test",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    task_id = del_res["task_id"]

    # Poll once in shadow mode
    res = daemon.poll_once()

    assert res is not None
    assert res["status"] == "shadow_skipped"
    assert res["task_id"] == task_id
    assert res["action"] == "skipped_all_side_effects"

    # Verify task remains open and unclaimed in DB
    task_row = tasks_tool.task_list(status="all")["tasks"][0]
    assert task_row["status"] == "open"
    assert task_row["claimed_by"] != "Desktop_agent3"


def test_crash_recovery_reclaims_expired_locks():
    """Verify crash recovery reclaims expired locks owned by daemon on startup."""
    # Add task with expired lock
    t = tasks_tool.task_add(title="Crashed task")
    task_id = t["id"]

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing', claimed_by='Desktop_agent3', lock_expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    daemon = ExecutorDaemon(agent_label="Desktop_agent3")
    res = daemon.reclaim_expired_locks()

    assert res["reclaimed_count"] == 1
    assert res["reclaimed_tasks"][0]["id"] == task_id

    # Verify task status is reset to open in DB
    task_row = tasks_tool.task_list(status="all")["tasks"][0]
    assert task_row["status"] == "open"
    assert task_row["claimed_by"] is None


def _periodic_reclaim_daemon(monkeypatch, steps: list[float]):
    """Daemon whose poll loop advances a fake clock by ``steps`` then stops."""
    now = {"t": 1_000.0}
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", poll_interval=0, clock=lambda: now["t"])
    calls: list[float] = []

    def fake_reclaim():
        calls.append(now["t"])
        return {"reclaimed_count": 0, "reclaimed_tasks": [], "released_delegations": 0}

    remaining = list(steps)

    def fake_poll():
        now["t"] += remaining.pop(0)
        if not remaining:
            daemon.running = False

    monkeypatch.setattr(daemon, "reclaim_expired_locks", fake_reclaim)
    monkeypatch.setattr(daemon, "poll_once", fake_poll)
    monkeypatch.setattr("unified_agent.executor.daemon.time.sleep", lambda _s: None)
    return daemon, calls


def test_run_forever_reclaims_expired_locks_periodically(monkeypatch):
    """Task #1916: lock reclaim ran only at startup; a lease that lapsed while
    the daemon kept running was never recovered until the next restart."""
    monkeypatch.setenv("UA_EXECUTOR_RECLAIM_INTERVAL_S", "300")
    daemon, calls = _periodic_reclaim_daemon(monkeypatch, [100, 199, 1, 200, 50])
    daemon.run_forever()
    # Startup at t=1000, then once the 300s interval elapsed (t=1300); not at
    # 1100/1299 (inside the interval) nor 1500 (only 200s after 1300).
    assert calls == [1_000.0, 1_300.0]


def test_periodic_reclaim_interval_zero_keeps_startup_only(monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_RECLAIM_INTERVAL_S", "0")
    daemon, calls = _periodic_reclaim_daemon(monkeypatch, [1_000, 1_000, 1_000])
    daemon.run_forever()
    assert calls == [1_000.0]


def test_reclaim_interval_config_default(monkeypatch):
    monkeypatch.delenv("UA_EXECUTOR_RECLAIM_INTERVAL_S", raising=False)
    assert config.executor_reclaim_interval_s() == 300.0
    monkeypatch.setenv("UA_EXECUTOR_RECLAIM_INTERVAL_S", "bad")
    assert config.executor_reclaim_interval_s() == 300.0


def test_periodic_reclaim_failure_does_not_stop_the_loop(monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_RECLAIM_INTERVAL_S", "10")
    daemon, calls = _periodic_reclaim_daemon(monkeypatch, [20, 20, 20])
    recorder = daemon.reclaim_expired_locks

    def flaky():
        recorder()
        if len(calls) == 2:
            raise RuntimeError("hub hiccup")
        return {}

    monkeypatch.setattr(daemon, "reclaim_expired_locks", flaky)
    daemon.run_forever()
    # Startup, then every iteration (20s > 10s); the raise on the second call
    # is logged and the loop keeps polling and reclaiming.
    assert calls == [1_000.0, 1_020.0, 1_040.0]


@pytest.mark.parametrize(
    "db_payload",
    [
        None,
        TOMBSTONE_DB_TEXT.encode("utf-8"),
        b"\x00\xffhalf-written sqlite this is not",
    ],
)
def test_run_forever_does_not_crash_loop_on_unusable_local_db(db_payload, monkeypatch, caplog):
    db = Path(config.db_path())
    if db_payload is None:
        db.unlink(missing_ok=True)
    else:
        db.write_bytes(db_payload)

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", poll_interval=0.01)
    polls = 0

    def stop_after_startup():
        nonlocal polls
        polls += 1
        daemon.running = False

    monkeypatch.setattr(daemon, "poll_once", stop_after_startup)

    with caplog.at_level(logging.WARNING, logger="unified_agent.executor"):
        daemon.run_forever()

    assert polls == 1
    assert "skipping local expired-lock reclaim" in caplog.text


def test_lock_renewal():
    """Verify daemon lock renewal updates lock_expires_at."""
    t = tasks_tool.task_add(title="Long task")
    task_id = t["id"]

    tasks_tool.task_claim(task_id, agent="Desktop_agent3", timeout_seconds=100)

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", lock_timeout_s=3600)
    success = daemon.renew_lock(task_id)

    assert success is True
    updated = tasks_tool.task_list(status="all")["tasks"][0]
    assert updated["lock_expires_at"] > storage.now()


def test_lock_renewal_refuses_a_cancelled_run(monkeypatch):
    """Task #1525 (PR #866 review: "cancelled executor lease keeps
    renewing"): `renew_lock`'s direct-SQL write must never extend a lease
    once `request_cancel` has flagged the task's current run."""
    t = tasks_tool.task_add(title="Cancellable long task")
    task_id = t["id"]
    tasks_tool.task_claim(task_id, agent="Desktop_agent3", timeout_seconds=100)
    monkeypatch.setattr("unified_agent.config.verified_agent_identity", lambda: "Desktop_agent3")
    tasks_tool.task_update(task_id, request_cancel=True, worktree=False)
    before = tasks_tool.task_list(status="all")["tasks"][0]["lock_expires_at"]

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", lock_timeout_s=3600)
    success = daemon.renew_lock(task_id)

    assert success is False
    after = tasks_tool.task_list(status="all")["tasks"][0]["lock_expires_at"]
    assert after == before


def test_lock_renewal_rechecks_cancellation_in_conditional_write(monkeypatch):
    from unified_agent.executor import daemon as daemon_mod

    task_id = tasks_tool.task_add(title="Renewal race")["id"]
    claimed = tasks_tool.task_claim(task_id, agent="Desktop_agent3", timeout_seconds=100)
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", lock_timeout_s=3600)

    def cancel_after_read(_task_id, _hub, _run_id):
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
        return False

    monkeypatch.setattr(daemon_mod, "_cancel_requested_for_task", cancel_after_read)
    assert daemon.renew_lock(task_id, claimed["current_run_id"]) is False


def test_cancel_requested_for_task_reflects_request_cancel(monkeypatch):
    """`_cancel_requested_for_task` is what `_execute_claimed_task`'s
    `kill_check` closure polls during backend execution (task #383/#1525) to
    stop a cancelled run — the same signal a killed cycle or a paused agent
    already use, since `renew_lock` only ever runs once before dispatch."""
    from unified_agent.executor import daemon as daemon_mod

    t = tasks_tool.task_add(title="Cancel me mid-run")
    task_id = t["id"]
    tasks_tool.task_claim(task_id, agent="Desktop_agent3", timeout_seconds=3600)
    monkeypatch.setattr("unified_agent.config.verified_agent_identity", lambda: "Desktop_agent3")
    tasks_tool.task_update(task_id, request_cancel=True, worktree=False)

    assert daemon_mod._cancel_requested_for_task(task_id) is True

    other = tasks_tool.task_add(title="Not cancelled")
    assert daemon_mod._cancel_requested_for_task(other["id"]) is False


def test_cancel_requested_for_task_reads_hub_when_local_store_is_retired(monkeypatch):
    from unified_agent.executor import daemon as daemon_mod

    class FakeHub:
        def call_tool(self, name, arguments):
            assert name == "observability_manage"
            assert arguments == {"action": "run_history", "params": {"task_id": 42, "limit": 1}}
            return {"runs": [{"state": "open", "cancel_requested": True}]}

    def retired_store():
        raise storage.LiveStoreError("local task store retired")

    monkeypatch.setattr(storage, "connect", retired_store)
    assert daemon_mod._cancel_requested_for_task(42, FakeHub()) is True


def test_cancel_requested_for_task_stops_a_superseded_run(monkeypatch):
    from unified_agent.executor import daemon as daemon_mod

    class FakeHub:
        def call_tool(self, name, arguments):
            return {
                "runs": [{"run_id": "sp-successor", "state": "open", "cancel_requested": False}]
            }

    def retired_store():
        raise storage.LiveStoreError("local task store retired")

    monkeypatch.setattr(storage, "connect", retired_store)
    assert daemon_mod._cancel_requested_for_task(42, FakeHub(), "sp-original") is True


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_executor_does_not_spawn_after_cancelled_prestart_renewal(mock_pi_run, monkeypatch):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    created = delegate_tool.delegate(
        task_spec="Cancelled before start", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    taken = delegate_tool.delegate_claim(created["delegation_id"], "Desktop_agent3")
    claimed = tasks_tool.task_claim(created["task_id"], "Desktop_agent3")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (created["task_id"],))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(daemon, "_cycle_gate_for_task", lambda _task_id: None)
    monkeypatch.setattr(daemon, "task_worktree", lambda _claim: None)
    result = daemon._execute_claimed_task(
        taken["delegation"],
        claimed,
    )
    assert result["status"] == "claim_not_current"
    mock_pi_run.assert_not_called()
    assert (
        delegate_tool.delegate_get(created["delegation_id"])["delegation"]["status"] == "cancelled"
    )
    assert daemon.poll_once() is None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_executor_does_not_spawn_without_a_run_token(mock_pi_run):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    created = delegate_tool.delegate(
        task_spec="Missing token test", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    taken = delegate_tool.delegate_claim(created["delegation_id"], "Desktop_agent3")
    result = daemon._execute_claimed_task(
        taken["delegation"],
        {"current_run_id": None, "notes": "", "project": "unified-agent"},
    )
    assert result["status"] == "missing_run_token"
    mock_pi_run.assert_not_called()
    assert (
        delegate_tool.delegate_get(created["delegation_id"])["delegation"]["status"] == "cancelled"
    )


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_executor_does_not_spawn_without_claim_generation(mock_pi_run):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    created = delegate_tool.delegate(
        task_spec="Missing generation test", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    taken = delegate_tool.delegate_claim(created["delegation_id"], "Desktop_agent3")
    delegation = {**taken["delegation"], "claim_generation": None}

    result = daemon._execute_claimed_task(delegation, {"current_run_id": "sp-claimed"})

    assert result["status"] == "missing_claim_generation"
    mock_pi_run.assert_not_called()
    assert (
        delegate_tool.delegate_get(created["delegation_id"])["delegation"]["status"] == "in_flight"
    )


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_finish_to_review_lifecycle(mock_pi_run, tmp_path, monkeypatch):
    """Verify finish-to-review lifecycle annotates [waiting-for: review] and sets status to doing."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    mock_pi_run.return_value = {
        "status": "success",
        "text": "Completed execution successfully",
        "tools_used": [],
        "files_modified": [],
        "events_count": 5,
        "exit_code": 0,
        "error": None,
    }

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

    # Create a delegation
    del_res = delegate_tool.delegate(
        task_spec="Full execution lifecycle test",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    task_id = del_res["task_id"]
    assert task_id > 0

    # Run poll_once
    poll_res = daemon.poll_once()

    assert poll_res is not None
    assert poll_res["status"] == "success"
    assert poll_res["waiting_for_review"] is True

    # Verify task notes contain [waiting-for: review] and status is 'doing'
    task_info = tasks_tool.task_list(status="doing")["tasks"][0]
    assert "[waiting-for: review]" in task_info["notes"]
    assert task_info["status"] == "doing"
    # Must NOT be marked done
    assert task_info["completed_at"] is None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_stale_executor_cannot_annotate_successor_run(mock_pi_run, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    delegated = delegate_tool.delegate(
        task_spec="Stale finish fencing test",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    task_id = delegated["task_id"]

    def takeover(**_kwargs):
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET current_run_id='sp-successor', claimed_by='Desktop_Agent2' "
                "WHERE id=?",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "status": "success",
            "text": "done",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }

    mock_pi_run.side_effect = takeover
    result = daemon.poll_once()
    assert result["status"] == "run_not_current"
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT current_run_id, notes FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["current_run_id"] == "sp-successor"
    assert "[waiting-for: review]" not in (row["notes"] or "")
    assert (
        delegate_tool.delegate_get(delegated["delegation_id"])["delegation"]["status"]
        == "cancelled"
    )
    assert daemon.poll_once() is None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_hub_outage_after_backend_still_closes_attempt(mock_pi_run, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    delegated = delegate_tool.delegate(
        task_spec="Post-run hub outage", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    task_id = delegated["task_id"]
    mock_pi_run.return_value = {
        "status": "success",
        "text": "done",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": 0,
        "error": None,
    }
    from unified_agent.executor import daemon as daemon_mod

    def failed_postrun_check(*_args):
        return mock_pi_run.called

    def hub_outage(*_args, **_kwargs):
        raise RuntimeError("hub unavailable")

    monkeypatch.setattr(daemon_mod, "_cancel_requested_for_task", failed_postrun_check)
    monkeypatch.setattr(daemon, "_retire_stopped_delegation", hub_outage)
    result = daemon.poll_once()
    assert result["status"] == "run_not_current"
    assert result["delegation_retirement_error"] == "hub unavailable"
    conn = storage.connect()
    try:
        attempt = conn.execute(
            "SELECT * FROM executor_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
        session = conn.execute(
            "SELECT * FROM agent_sessions WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert attempt["finished_at"] is not None
    assert "delegation retirement pending" in attempt["detail"]
    assert session["ended_at"] is not None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_finish_to_review_does_not_claim_waiting_review_when_the_task_transition_fails(
    mock_pi_run, tmp_path, monkeypatch
):
    """A run that succeeds but whose task_update(status="doing", notes=...)
    call fails must not have its executor_attempts row finalized as
    waiting_review — the task never actually got [waiting-for: review], so
    the review scanner can't find it, and claiming otherwise made the
    trust-ladder evidence lie about what state the task was in (task #445 /
    PR #201 review)."""
    from unified_agent.executor import evidence

    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    mock_pi_run.return_value = {
        "status": "success",
        "text": "Completed execution successfully",
        "tools_used": [],
        "files_modified": [],
        "events_count": 5,
        "exit_code": 0,
        "error": None,
    }

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    del_res = delegate_tool.delegate(
        task_spec="Lifecycle transition failure test",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    task_id = del_res["task_id"]

    real_call_tool = daemon.hub_client.call_tool

    def flaky_call_tool(tool_name, arguments):
        if tool_name == "task_update" and "evidence_ref" in arguments:
            raise RuntimeError("hub rejected the update (401)")
        return real_call_tool(tool_name, arguments)

    monkeypatch.setattr(daemon.hub_client, "call_tool", flaky_call_tool)

    with pytest.raises(RuntimeError, match="hub rejected the update"):
        daemon.poll_once()

    # The task itself never got the marker — undiscoverable by the review scanner.
    task_info = tasks_tool.task_list(status="doing")["tasks"][0]
    assert task_info["id"] == task_id
    assert "[waiting-for: review]" not in (task_info["notes"] or "")

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM executor_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["outcome"] != "waiting_review"
    assert row["finished_at"] is not None
    assert evidence.outcomes_snapshot()["attempts"].get("waiting_review", 0) == 0


class TestBoundedAutonomyCycleGate:
    """Task #373 Tier 2, 3.3 — the daemon-side half of the S5 budget/kill-
    switch gate. autonomous_cycle_record_usage's comparison logic was already
    correct; nothing used to check it before dispatching more work, and
    nothing measured usage automatically. These tests exercise the wiring
    added in _execute_claimed_task, not the cycle state machine itself
    (see tests/test_autonomous_cycles.py for that)."""

    def _delegate(self) -> int:
        del_res = delegate_tool.delegate(
            task_spec="Bounded autonomy gate test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        return del_res["task_id"]

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_killed_cycle_blocks_execution_without_running_the_backend(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]
        cycles_tool.autonomous_cycle_kill(cycle_id, "Desktop_agent3", "test: stop before it starts")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        poll_res = daemon.poll_once()

        assert poll_res["status"] == "blocked_by_cycle"
        assert poll_res["cycle_state"] == "killed"
        mock_pi_run.assert_not_called()

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_killed_cycle_marks_delegation_failed_not_pending(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        """Regression test for a busy-loop scripts/kill_switch_rehearsal.py
        caught: refusing a killed/blocked cycle must not put the delegation
        back to 'pending' — that just gets it reclaimed and re-refused on
        every single poll, forever, with no operator ever in the loop."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]
        cycles_tool.autonomous_cycle_kill(cycle_id, "Desktop_agent3", "test: busy-loop regression")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM delegations WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "failed"

        # The bug this guards: a second poll must NOT pick the same
        # delegation back up and refuse it again.
        second_poll = daemon.poll_once()
        assert second_poll is None
        mock_pi_run.assert_not_called()

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_blocked_cycle_blocks_execution_without_running_the_backend(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start(
            "Desktop_agent3", task_id=task_id, budgets={"time_s": 60}
        )["cycle"]["id"]
        # Drive it over budget the same way real usage would.
        cycles_tool.autonomous_cycle_record_usage(cycle_id, "Desktop_agent3", elapsed_s=120)
        assert cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]["state"] == "blocked"

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        poll_res = daemon.poll_once()

        assert poll_res["status"] == "blocked_by_cycle"
        assert poll_res["cycle_state"] == "blocked"
        mock_pi_run.assert_not_called()

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_no_cycle_keeps_the_default_300s_timeout(self, mock_pi_run, tmp_path, monkeypatch):
        """Most delegated tasks have no bound cycle — cycles are opt-in.
        Unchanged behavior is the default, not a special case."""
        monkeypatch.delenv("UA_EXECUTOR_TIMEOUT_S", raising=False)
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }
        self._delegate()
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()

        # Assert against the real config accessor — default stays 300 when unset.
        assert config.executor_timeout_s() == 300
        assert mock_pi_run.call_args.kwargs["timeout_s"] == config.executor_timeout_s()

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_no_cycle_uses_configured_executor_timeout(self, mock_pi_run, tmp_path, monkeypatch):
        """Task #1149: unbounded runs honor UA_EXECUTOR_TIMEOUT_S (opt-in)."""
        monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "1200")
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }
        self._delegate()
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()

        assert config.executor_timeout_s() == 1200
        assert mock_pi_run.call_args.kwargs["timeout_s"] == config.executor_timeout_s()

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_cycle_caps_timeout_to_remaining_budget(self, mock_pi_run, tmp_path, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_TIMEOUT_S", raising=False)
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }
        task_id = self._delegate()
        cycles_tool.autonomous_cycle_start(
            "Desktop_agent3", task_id=task_id, budgets={"time_s": 45}
        )

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()

        # min(configured=300, remaining=45) == 45 — the subprocess-level
        # timeout is the real, automatic enforcement of the cycle's time budget.
        assert mock_pi_run.call_args.kwargs["timeout_s"] == 45

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_cycle_caps_at_min_of_remaining_and_configured(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        """Cycle-bound runs keep min(remaining_budget, configured) — not a
        hardcoded 300 ceiling that ignores UA_EXECUTOR_TIMEOUT_S."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }

        # Remaining budget is the tighter bound when configured is larger.
        monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "1200")
        task_id = self._delegate()
        cycles_tool.autonomous_cycle_start(
            "Desktop_agent3", task_id=task_id, budgets={"time_s": 45}
        )
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()
        assert mock_pi_run.call_args.kwargs["timeout_s"] == 45

        # Configured ceiling is the tighter bound when remaining is larger.
        mock_pi_run.reset_mock()
        monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "90")
        task_id2 = self._delegate()
        cycles_tool.autonomous_cycle_start(
            "Desktop_agent3", task_id=task_id2, budgets={"time_s": 200}
        )
        daemon.poll_once()
        assert config.executor_timeout_s() == 90
        assert mock_pi_run.call_args.kwargs["timeout_s"] == 90

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_successful_run_records_daemon_measured_usage(self, mock_pi_run, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]

        ticks = iter([100.0, 100.0, 103.0, 103.0, 103.0, 103.0])
        daemon = ExecutorDaemon(
            agent_label="Desktop_agent3", shadow_mode=False, clock=lambda: next(ticks, 103.0)
        )
        daemon.poll_once()

        used = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]["used_time_s"]
        assert used > 0

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_failed_run_records_a_retry_against_the_cycle(self, mock_pi_run, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "error",
            "text": "",
            "tools_used": [],
            "files_modified": [],
            "events_count": 0,
            "exit_code": 1,
            "error": "boom",
        }
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        daemon.poll_once()

        assert cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]["used_retries"] == 1


class TestCycleGateHubFallback:
    """Task #992 — residual DoD of #987.

    PR #509 wrapped exactly one call site (`reclaim_expired_locks`'s
    `storage.connect()`) so a desktop executor's tombstoned local memory.db
    (see docs/mac-hub-topology.md) doesn't crash-loop the daemon. It
    never touched the OTHER local-storage read on the same hot path: the
    cycle gate in `_execute_claimed_task` (`cycles_tool.
    active_cycle_for_task`), which raised the same `LiveStoreError`
    uncaught — a live desktop was observed claiming a task over the hub and
    then losing it to this exact unhandled exception on the very next line.

    These tests simulate the tombstone by making the *direct* local lookup
    (`cycles_tool.active_cycle_for_task`, as called from inside
    `ExecutorDaemon._cycle_gate_for_task`) raise `storage.LiveStoreError`,
    while everything else in the test (delegation, claim, mark-failed) keeps
    using the test fixture's real local SQLite — exactly mirroring
    production, where only ONE call ever hits local storage.connect()
    directly and everything else already round-trips the hub via
    `hub_client.call_tool`. The critical property under test: a killed/
    blocked cycle must still refuse execution when this happens — the naive
    fix (swallow-and-continue, matching `reclaim_expired_locks`'s pattern)
    would silently turn the kill switch into a no-op instead, which is
    exactly what scripts/kill_switch_rehearsal.py exists to catch.
    """

    def _delegate(self) -> int:
        del_res = delegate_tool.delegate(
            task_spec="Cycle gate hub-fallback test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        return del_res["task_id"]

    @staticmethod
    def _raise_tombstone(*_args, **_kwargs):
        raise storage.LiveStoreError(
            "memory.db at /home/you/.unified_agent/memory.db is not a "
            "SQLite database (header=b'AUTHORITATIVE me'); check UA_HOME."
        )

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_killed_cycle_still_blocks_execution_when_local_lookup_hits_tombstone(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        task_id = self._delegate()
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]
        cycles_tool.autonomous_cycle_kill(cycle_id, "Desktop_agent3", "test: tombstone fallback")
        # Ground truth, captured via `autonomous_cycle_get` — a code path
        # that does NOT go through `active_cycle_for_task`, so it is
        # unaffected by the tombstone simulation below. This is what a real,
        # separate, healthy Mac hub would answer if asked.
        expected_cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        monkeypatch.setattr(
            "unified_agent.executor.daemon.cycles_tool.active_cycle_for_task",
            self._raise_tombstone,
        )
        hub_calls = []
        real_call_tool = daemon.hub_client.call_tool

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            if tool_name == "autonomous_cycle_manage" and arguments.get("action") == "get_for_task":
                # Simulate a genuinely separate, healthy hub — NOT the
                # test's in-process fallback, which would reuse (and thus
                # be defeated by) the same monkeypatched local function.
                return {"cycle": expected_cycle}
            return real_call_tool(tool_name, arguments)

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        poll_res = daemon.poll_once()

        assert poll_res["status"] == "blocked_by_cycle"
        assert poll_res["cycle_state"] == "killed"
        mock_pi_run.assert_not_called()
        assert (
            "autonomous_cycle_manage",
            {"action": "get_for_task", "params": {"task_id": task_id}},
        ) in hub_calls

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM delegations WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "failed"

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_normal_task_still_executes_through_the_hub_fallback_path(
        self, mock_pi_run, tmp_path, monkeypatch
    ):
        """No cycle bound to the task (the common case) — a local-lookup
        failure that falls back to the hub must still resolve to "no cycle"
        and let the task run normally, not refuse it."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        mock_pi_run.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }
        self._delegate()

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        monkeypatch.setattr(
            "unified_agent.executor.daemon.cycles_tool.active_cycle_for_task",
            self._raise_tombstone,
        )
        real_call_tool = daemon.hub_client.call_tool

        def call_tool(tool_name, arguments):
            if tool_name == "autonomous_cycle_manage" and arguments.get("action") == "get_for_task":
                # A real hub, asked about a task with no bound cycle at all
                # (the common case), answers "no cycle" — not a raised error.
                return {"cycle": None}
            return real_call_tool(tool_name, arguments)

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        poll_res = daemon.poll_once()

        mock_pi_run.assert_called_once()
        assert poll_res["status"] != "blocked_by_cycle"
        assert poll_res["status"] != "blocked_by_cycle_gate_unverifiable"

    def test_hub_call_uses_the_get_for_task_action_with_the_right_params(self, monkeypatch):
        """Isolated unit test of _cycle_gate_for_task itself: on a local
        failure it must call the hub with exactly the new consolidated
        action, and return whatever cycle dict the hub answers with."""
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        monkeypatch.setattr(
            "unified_agent.executor.daemon.cycles_tool.active_cycle_for_task",
            self._raise_tombstone,
        )
        calls = []

        def fake_call_tool(tool_name, arguments):
            calls.append((tool_name, arguments))
            return {"cycle": {"id": "cyc-1", "state": "blocked", "stop_reason": "over budget"}}

        monkeypatch.setattr(daemon.hub_client, "call_tool", fake_call_tool)

        cycle = daemon._cycle_gate_for_task(42)

        assert calls == [
            ("autonomous_cycle_manage", {"action": "get_for_task", "params": {"task_id": 42}})
        ]
        assert cycle == {"id": "cyc-1", "state": "blocked", "stop_reason": "over budget"}

    @patch("unified_agent.tools.pi_runner.pi_run_task")
    def test_hub_unreachable_too_fails_closed_not_open(self, mock_pi_run, tmp_path, monkeypatch):
        """If local storage is unusable AND the hub round-trip also fails,
        the gate cannot be verified. Fail-closed (refuse execution) is the
        only safe choice for a kill-switch/budget gate — proceeding would be
        a silent bypass, and crashing would revert to the #987 crash-loop."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
        task_id = self._delegate()

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        monkeypatch.setattr(
            "unified_agent.executor.daemon.cycles_tool.active_cycle_for_task",
            self._raise_tombstone,
        )
        real_call_tool = daemon.hub_client.call_tool

        def failing_call_tool(tool_name, arguments):
            if tool_name == "autonomous_cycle_manage" and arguments.get("action") == "get_for_task":
                raise RuntimeError("hub unreachable: simulated network failure")
            return real_call_tool(tool_name, arguments)

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        poll_res = daemon.poll_once()

        assert poll_res["status"] == "blocked_by_cycle_gate_unverifiable"
        mock_pi_run.assert_not_called()

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM delegations WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "failed"


class TestHubAuthBoundary:
    """The executor must not execute outside the hub when the hub refuses it.

    Phase 4.2's first stated requirement was a hub client auth boundary. It was
    nullified in practice: HubClient fabricated a `token-<label>` credential the
    hub could not know, every call 401'd, urllib.error.HTTPError was caught as a
    transport error, and the tool ran in-process at DEBUG log level — outside the
    hub's auth check, the approval gate, and per-agent workspace scoping. The
    live daemon did that every 5 seconds for days while reporting healthy.
    """

    def _client(self, monkeypatch, allow_fallback):
        from unified_agent.executor.daemon import HubClient

        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        return HubClient(agent_label="Desktop_agent3", allow_fallback=allow_fallback)

    def test_auth_rejection_never_falls_back(self, monkeypatch):
        import urllib.error

        from unified_agent import hub_client

        client = self._client(monkeypatch, allow_fallback=True)  # even when allowed
        called = []
        monkeypatch.setattr(client, "_direct_tool_fallback", lambda *a, **k: called.append(a) or {})

        def raise_401(*args, **kwargs):
            raise urllib.error.HTTPError(client.hub_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(hub_client.urllib.request, "urlopen", raise_401)

        with pytest.raises(RuntimeError, match="auth rejected"):
            client.call_tool("task_list", {})
        assert called == [], "a 401 must not run the tool in-process"

    def test_transport_failure_still_falls_back_when_allowed(self, monkeypatch):
        import urllib.error

        from unified_agent import hub_client

        client = self._client(monkeypatch, allow_fallback=True)
        monkeypatch.setattr(client, "_direct_tool_fallback", lambda *a, **k: {"ok": True})

        def raise_conn(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(hub_client.urllib.request, "urlopen", raise_conn)

        assert client.call_tool("task_list", {}) == {"ok": True}

    def test_no_fabricated_token(self, monkeypatch):
        """A missing credential must look like a misconfiguration, not succeed."""
        from unified_agent.executor.daemon import HubClient

        monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("UA_AGENT_TOKENS", "Someone_Else:tok-other")

        with pytest.raises(RuntimeError, match="no bearer token resolved"):
            HubClient(agent_label="Desktop_agent3", allow_fallback=False)

        client = HubClient(agent_label="Desktop_agent3", allow_fallback=True)
        assert client.bearer_token is None, "must not invent a token-<label> credential"

    def test_daemon_requires_the_hub_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_ALLOW_DIRECT", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        assert daemon.hub_client.allow_fallback is False

    def test_primary_client_wrong_hub_never_falls_back(self, monkeypatch):
        import urllib.error

        from unified_agent import hub_client
        from unified_agent.executor.daemon import HubClient

        monkeypatch.delenv("UA_EXECUTOR_ALLOW_DIRECT", raising=False)
        monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        client = HubClient(agent_label="Desktop_agent3", allow_fallback=True)
        called = []
        monkeypatch.setattr(client, "_direct_tool_fallback", lambda *a, **k: called.append(a) or {})

        monkeypatch.setattr(
            hub_client.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("connection refused")),
        )

        with pytest.raises(RuntimeError, match="Failed to communicate with HTTP hub"):
            client.call_tool("task_list", {})
        assert called == []


class TestHubClientTransport:
    """The client must speak MCP streamable-HTTP, not hand-rolled JSON-RPC.

    As shipped it sent no Accept header, never performed the `initialize`
    handshake, never sent Mcp-Session-Id, and json.loads()'d a body the hub
    returns SSE-framed. Against the live hub that is 406, then 400 Missing
    session ID. It had therefore NEVER completed a single hub call — every
    piece of executor work in history ran through the in-process fallback,
    outside the hub's auth check, approval gate and workspace scoping. The
    401s from the missing token (#82/#173) hid it.
    """

    def _client(self, monkeypatch, hub):
        from unified_agent import hub_client
        from unified_agent.executor.daemon import HubClient

        monkeypatch.setattr(hub_client.urllib.request, "urlopen", hub)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        return HubClient(agent_label="Desktop_agent3", allow_fallback=False)

    def test_sends_the_accept_header_streamable_http_requires(self, monkeypatch):
        hub = _FakeHub()
        client = self._client(monkeypatch, hub)
        client.ping()
        for _method, headers in hub.requests:
            assert "text/event-stream" in headers["Accept"]

    def test_handshakes_before_calling_a_tool(self, monkeypatch):
        hub = _FakeHub()
        client = self._client(monkeypatch, hub)
        client.call_tool("delegate_list", {"status": "pending"})
        methods = [m for m, _ in hub.requests]
        assert methods[0] == "initialize"
        assert "notifications/initialized" in methods
        assert methods[-1] == "tools/call"

    def test_sends_the_session_id_on_subsequent_requests(self, monkeypatch):
        hub = _FakeHub()
        client = self._client(monkeypatch, hub)
        client.call_tool("delegate_list", {})
        _method, headers = hub.requests[-1]
        # urllib title-cases header names, so match case-insensitively.
        low = {k.lower(): v for k, v in headers.items()}
        assert low["mcp-session-id"] == hub.session_id

    def test_reuses_one_session_across_calls(self, monkeypatch):
        hub = _FakeHub()
        client = self._client(monkeypatch, hub)
        client.call_tool("delegate_list", {})
        client.call_tool("delegate_list", {})
        assert [m for m, _ in hub.requests].count("initialize") == 1

    def test_decodes_the_sse_framed_response(self, monkeypatch):
        hub = _FakeHub()
        client = self._client(monkeypatch, hub)
        assert client.call_tool("delegate_list", {}) == {"delegations": []}

    def test_unwraps_the_content_envelope_to_the_tools_own_dict(self, monkeypatch):
        """Returning the envelope would fail SILENTLY: find_pending_work does
        .get("delegations", []) on it and sees [] — 'no work' — forever."""
        hub = _FakeHub(tool_payload={"delegations": [{"id": 7, "task_id": 42}]})
        client = self._client(monkeypatch, hub)
        res = client.call_tool("delegate_list", {})
        assert res["delegations"][0]["task_id"] == 42
        assert "content" not in res

    def test_raises_on_a_tool_error_rather_than_returning_the_envelope(self, monkeypatch):
        hub = _FakeHub(is_error=True)
        client = self._client(monkeypatch, hub)
        with pytest.raises(RuntimeError, match="tool error"):
            client.call_tool("delegate_list", {})

    def test_re_handshakes_once_when_the_session_expires(self, monkeypatch):
        hub = _FakeHub(expire_after=1)
        client = self._client(monkeypatch, hub)
        client.call_tool("delegate_list", {})
        client.call_tool("delegate_list", {})  # 404 -> re-initialize -> succeed
        assert [m for m, _ in hub.requests].count("initialize") == 2

    def test_auth_rejection_still_never_falls_back(self, monkeypatch):
        """The #82 boundary must survive the transport rewrite."""
        from unified_agent.executor.daemon import HubClient

        hub = _FakeHub(auth_status=401)
        monkeypatch.setattr(hub_client_urllib(), "urlopen", hub)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-wrong")
        client = HubClient(agent_label="Desktop_agent3", allow_fallback=True)
        called = []
        monkeypatch.setattr(client, "_direct_tool_fallback", lambda *a: called.append(a))

        with pytest.raises(RuntimeError, match="auth rejected"):
            client.call_tool("delegate_list", {})
        assert called == []


def hub_client_urllib():
    from unified_agent import hub_client

    return hub_client.urllib.request


class _FakeResp:
    def __init__(self, status, headers, text):
        self.status, self.headers, self._text = status, headers, text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHub:
    """Emulates the hub at the urlopen boundary, enforcing the real contract."""

    def __init__(self, *, auth_status=None, tool_payload=None, is_error=False, expire_after=None):
        self.auth_status = auth_status
        self.tool_payload = tool_payload if tool_payload is not None else {"delegations": []}
        self.is_error = is_error
        self.expire_after = expire_after
        self.session_id = "sess-1"
        self.requests = []
        self._tool_calls = 0

    def __call__(self, req, timeout=None):
        import json as _json

        body = _json.loads(req.data.decode())
        method = body.get("method")
        headers = dict(req.headers)
        self.requests.append((method, headers))
        low = {k.lower(): v for k, v in headers.items()}

        if self.auth_status:
            raise urllib.error.HTTPError(req.full_url, self.auth_status, "Unauthorized", {}, None)
        if "text/event-stream" not in (low.get("accept") or ""):
            raise urllib.error.HTTPError(req.full_url, 406, "Not Acceptable", {}, None)

        if method == "initialize":
            self.session_id = (
                f"sess-{self.requests.count(('initialize', headers)) or 1}-{len(self.requests)}"
            )
            return _FakeResp(
                200, {"mcp-session-id": self.session_id}, '{"jsonrpc":"2.0","id":1,"result":{}}'
            )
        if method == "notifications/initialized":
            return _FakeResp(202, {}, "")
        if not low.get("mcp-session-id"):
            raise urllib.error.HTTPError(req.full_url, 400, "Missing session ID", {}, None)

        if method == "tools/call":
            self._tool_calls += 1
            if self.expire_after is not None and self._tool_calls > self.expire_after:
                self.expire_after = None
                raise urllib.error.HTTPError(req.full_url, 404, "Session expired", {}, None)
            inner = _json.dumps(self.tool_payload)
            result = {"content": [{"type": "text", "text": inner}], "isError": self.is_error}
            msg = _json.dumps({"jsonrpc": "2.0", "id": 2, "result": result})
        else:
            msg = '{"jsonrpc":"2.0","id":3,"result":{"tools":[]}}'
        return _FakeResp(
            200, {"content-type": "text/event-stream"}, f"event: message\r\ndata: {msg}\r\n\r\n"
        )


class TestDelegationRunsExactlyOnce:
    """A pending delegation must be executed once, not once per poll tick.

    Nothing marked a delegation as taken, so find_pending_work re-listed it
    every 5s and the daemon re-claimed it — 138 times on task #149. task_claim
    is not a gate: it deliberately succeeds for the agent already holding the
    lock, because that is the renewal path. The fix is a state transition on the
    delegation itself. See #169.
    """

    def _daemon_with_fake_pi(self, monkeypatch):
        runs = []

        def fake_pi_run_task(**kwargs):
            runs.append(kwargs)
            return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run_task)
        return ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False), runs

    def _delegate(self):
        return delegate_tool.delegate(
            task_spec="Do the thing",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )

    def test_poll_uses_consolidated_delegation_manager(self, monkeypatch):
        """Authenticated developer profiles hide the legacy delegation aliases."""
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        calls = []

        def call_tool(tool_name, arguments):
            calls.append((tool_name, arguments))
            return {"delegations": []}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        assert daemon.find_pending_work() == []
        assert calls == [
            (
                "delegation_manage",
                {
                    "action": "list",
                    "params": {"status": "pending", "to_agent": "Desktop_agent3"},
                },
            )
        ]

    def test_repeated_poll_ticks_execute_once(self, monkeypatch):
        daemon, runs = self._daemon_with_fake_pi(monkeypatch)
        self._delegate()

        first = daemon.poll_once()
        # Ten further ticks, exactly as the 5s loop would drive them — no sleeps.
        later = [daemon.poll_once() for _ in range(10)]

        assert first is not None
        assert all(r is None for r in later), "a later tick picked the delegation up again"
        assert len(runs) == 1, f"Pi ran {len(runs)} times, expected 1"

    def test_a_failing_execution_is_not_retried_on_every_tick(self, monkeypatch):
        """THE production scenario, and the one the other tests here miss.

        On the happy path _execute_claimed_task marks the delegation
        'completed' at the end, so it leaves the poll query by itself — which is
        why a naive re-run test passes even against the broken code. The real
        loop needed a FAILURE: an exception left the row 'pending',
        run_forever swallowed it, and the next tick re-claimed the same
        delegation. Measured against the pre-fix daemon this is 1 attempt per
        poll (10 polls -> 10 attempts); task #149 logged ~138 of them over 22
        minutes before one finally succeeded.

        After the fix the row is in_flight, so a failure is attempted once and
        then left for the startup reclaim rather than hammered forever.
        """
        attempts = []

        def boom(self, delegation, claim_res):
            attempts.append(delegation["id"])
            raise RuntimeError("failure mid-execution")

        monkeypatch.setattr(ExecutorDaemon, "_execute_claimed_task", boom)
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        self._delegate()

        for _ in range(10):
            try:
                daemon.poll_once()
            except RuntimeError:
                pass  # run_forever swallows per-tick errors and keeps going

        assert len(attempts) == 1, f"retried {len(attempts)} times; the tick loop is back"
        assert delegate_tool.delegate_list(status="in_flight")["delegations"]

    def test_delegation_leaves_the_poll_query_immediately(self, monkeypatch):
        daemon, _runs = self._daemon_with_fake_pi(monkeypatch)
        self._delegate()
        daemon.poll_once()

        assert daemon.find_pending_work() == []

    def test_two_daemons_execute_it_once_between_them(self, monkeypatch):
        _d, runs = self._daemon_with_fake_pi(monkeypatch)
        a = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        b = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
        self._delegate()

        results = [a.poll_once(), b.poll_once()]

        assert sum(r is not None for r in results) == 1
        assert len(runs) == 1

    def test_a_second_delegation_is_still_picked_up(self, monkeypatch):
        """The fix must stop re-runs, not stop the daemon working."""
        daemon, runs = self._daemon_with_fake_pi(monkeypatch)
        self._delegate()
        daemon.poll_once()
        self._delegate()

        assert daemon.poll_once() is not None
        assert len(runs) == 2

    def test_completion_callback_still_fires_for_an_in_flight_row(self, monkeypatch):
        """fire_completion_callbacks filtered on status='pending'.

        Introducing in_flight without widening that filter would have silently
        skipped every callback and stranded the row — the delegation would never
        reach 'completed'.
        """
        daemon, _runs = self._daemon_with_fake_pi(monkeypatch)
        res = self._delegate()
        daemon.poll_once()

        tasks_tool.task_update(
            res["task_id"],
            status="done",
            notes="[review: no-merge @ test]",
        )

        completed = delegate_tool.delegate_list(status="completed")["delegations"]
        assert [d["id"] for d in completed] == [res["delegation_id"]]

    def test_task_claim_failure_releases_the_delegation(self, monkeypatch):
        """Failing between taking the delegation and starting the run must not
        strand it as in_flight forever."""
        daemon, runs = self._daemon_with_fake_pi(monkeypatch)
        self._delegate()
        original = daemon.hub_client.call_tool

        def fail_task_claim(tool_name, arguments):
            if tool_name == "task_claim":
                raise RuntimeError("hub hiccup")
            return original(tool_name, arguments)

        monkeypatch.setattr(daemon.hub_client, "call_tool", fail_task_claim)
        assert daemon.poll_once() is None
        assert runs == []

        monkeypatch.setattr(daemon.hub_client, "call_tool", original)
        assert daemon.poll_once() is not None, "delegation was stranded in_flight"
        assert len(runs) == 1

    def test_restart_recovers_an_orphaned_in_flight_delegation(self, monkeypatch):
        """A daemon that died mid-run leaves the row in_flight; the startup
        reclaim must release it once the task lock has expired."""
        daemon, runs = self._daemon_with_fake_pi(monkeypatch)
        res = self._delegate()
        delegate_tool.delegate_claim(res["delegation_id"], agent="Desktop_agent3")
        # Simulate the crash: task held with a long-expired lock, delegation in_flight.
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET status='doing', claimed_by='Desktop_agent3', "
                "lock_expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                (res["task_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        recovery = daemon.reclaim_expired_locks()

        assert recovery["released_delegations"] == 1
        assert daemon.poll_once() is not None, "orphaned delegation was never recovered"
        assert len(runs) == 1

    def test_a_live_run_is_not_yanked_out_from_under_itself(self, monkeypatch):
        """Recovery is tied to lock expiry, so an in-flight run with a valid
        lock must survive a reclaim pass."""
        daemon, _runs = self._daemon_with_fake_pi(monkeypatch)
        res = self._delegate()
        delegate_tool.delegate_claim(res["delegation_id"], agent="Desktop_agent3")
        tasks_tool.task_claim(res["task_id"], agent="Desktop_agent3", timeout_seconds=3600)

        assert daemon.reclaim_expired_locks()["released_delegations"] == 0
        assert delegate_tool.delegate_list(status="in_flight")["delegations"]


class TestDelegateClaimAtomicity:
    def test_only_one_claimer_wins(self):
        res = delegate_tool.delegate(
            task_spec="Race", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )
        first = delegate_tool.delegate_claim(res["delegation_id"], agent="Desktop_agent3")
        second = delegate_tool.delegate_claim(res["delegation_id"], agent="Desktop_agent3")

        assert first["claimed"] is True
        assert second["claimed"] is False
        assert second["delegation"]["status"] == "in_flight"

    def test_claim_is_scoped_to_the_addressed_agent(self):
        res = delegate_tool.delegate(
            task_spec="Not yours", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )
        assert (
            delegate_tool.delegate_claim(res["delegation_id"], agent="Laptop_agent3")["claimed"]
            is False
        )

    def test_release_returns_it_to_pending(self):
        res = delegate_tool.delegate(
            task_spec="Give back", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )
        claimed = delegate_tool.delegate_claim(res["delegation_id"], agent="Desktop_agent3")
        assert (
            delegate_tool.delegate_release(
                res["delegation_id"],
                agent="Desktop_agent3",
                expected_claim_generation=claimed["delegation"]["claim_generation"],
            )["released"]
            is True
        )
        assert delegate_tool.delegate_list(status="pending")["delegations"]


class TestSelfHealingGuardrails:
    """Tests for Phase 5.2: Self-healing guardrails (backoff, attempt ceilings, recovery)."""

    def test_robust_config_fallbacks(self, monkeypatch, caplog):
        """Invalid or missing config env vars fall back to default values."""
        from unified_agent import config

        monkeypatch.setenv("UA_EXECUTOR_MAX_ATTEMPTS", "invalid_number")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "-5.0")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_FACTOR", "0")

        assert config.executor_max_attempts() == 5
        assert config.executor_backoff_base_s() == 10.0
        assert config.executor_backoff_factor() == 2.0

        # Task #1149: invalid/zero/negative timeout must never become 0.
        with caplog.at_level(logging.WARNING, logger="unified_agent.config"):
            monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "not-a-number")
            assert config.executor_timeout_s() == 300
            monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "0")
            assert config.executor_timeout_s() == 300
            monkeypatch.setenv("UA_EXECUTOR_TIMEOUT_S", "-10")
            assert config.executor_timeout_s() == 300
        assert "UA_EXECUTOR_TIMEOUT_S" in caplog.text

    def test_healthy_delegation_unaffected_zero_added_latency(self, monkeypatch):
        """A healthy delegation executes immediately on first attempt without backoff latency."""
        runs = []

        def fake_pi_run_task(**kwargs):
            runs.append(kwargs)
            return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run_task)
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

        delegate_tool.delegate(
            task_spec="Healthy task", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )

        poll_res = daemon.poll_once()
        assert poll_res is not None
        assert poll_res["status"] == "success"
        assert len(runs) == 1

    def test_backoff_enforcement_with_injected_clock(self, monkeypatch):
        """Failing delegation is NOT retried until its exponential backoff window elapses."""
        from datetime import UTC, datetime

        curr_time = datetime.now(UTC).timestamp()

        def mock_clock():
            return curr_time

        attempts_count = 0

        def failing_pi_run(**kwargs):
            nonlocal attempts_count
            attempts_count += 1
            return {"status": "error", "text": "transient error", "exit_code": 1, "error": "failed"}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", failing_pi_run)
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "10.0")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_FACTOR", "2.0")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False, clock=mock_clock)

        delegate_tool.delegate(
            task_spec="Failing task for backoff",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )

        # 1st attempt at curr_time
        res1 = daemon.poll_once()
        assert res1 is not None
        assert attempts_count == 1

        # Immediate tick at curr_time -> must be skipped due to 10s backoff
        res_backoff1 = daemon.poll_once()
        assert res_backoff1 is None

        # Tick at curr_time + 5s -> still in backoff window
        curr_time += 5.0
        res_backoff2 = daemon.poll_once()
        assert res_backoff2 is None
        assert attempts_count == 1

        # Advance curr_time by 6s more (total 11s) -> backoff window elapsed (10s), 2nd attempt runs!
        curr_time += 6.0
        res2 = daemon.poll_once()
        assert res2 is not None
        assert attempts_count == 2

    def test_backoff_window_is_capped(self, monkeypatch):
        """Task #1916: retry backoff is min(base * factor^(n-1), cap)."""
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "10")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_FACTOR", "2")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_CAP_S", "35")
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=True)
        assert [daemon._backoff_window_s(n) for n in (1, 2, 3, 4, 9)] == [
            10.0,
            20.0,
            35.0,
            35.0,
            35.0,
        ]

    def test_backoff_cap_default(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_BACKOFF_CAP_S", raising=False)
        assert config.executor_backoff_cap_s() == 3600.0

    def test_attempt_ceiling_transitions_to_failed_terminal_state(self, monkeypatch):
        """Reaching max attempt ceiling transitions delegation to terminal 'failed' state."""
        from datetime import UTC, datetime

        curr_time = datetime.now(UTC).timestamp()

        def mock_clock():
            return curr_time

        def failing_pi_run(**kwargs):
            return {"status": "error", "text": "persistent error", "exit_code": 1, "error": "fatal"}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", failing_pi_run)
        monkeypatch.setenv("UA_EXECUTOR_MAX_ATTEMPTS", "2")
        monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "10.0")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False, clock=mock_clock)

        del_res = delegate_tool.delegate(
            task_spec="Ceiling test task", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )

        # 1st attempt (fails)
        daemon.poll_once()

        # Advance past backoff window for 2nd attempt
        curr_time += 15.0
        daemon.poll_once()

        # Check delegation in delegate_list
        failed_dels = delegate_tool.delegate_list(status="failed")["delegations"]
        assert len(failed_dels) == 1
        assert failed_dels[0]["id"] == del_res["delegation_id"]
        assert "attempt ceiling" in (failed_dels[0]["callback_result"] or "").lower()

        # Subsequent poll_once returns None and does not attempt again
        curr_time += 1000.0
        assert daemon.poll_once() is None

    def test_clean_worktree_recovery_reports_workspace_state(self, isolated_env, monkeypatch):
        """Failing run after creating worktree leaves worktree/branch reported and task uncompleted."""
        TestWorktreeBindsToTheWorkspaceRepo()._workspace_repo(isolated_env)

        def failing_pi_run(**kwargs):
            return {"status": "error", "text": "failed mid-run", "exit_code": 1, "error": "err"}

        monkeypatch.setattr(pi_runner_tool, "pi_run_task", failing_pi_run)
        monkeypatch.setenv("UA_EXECUTOR_MAX_ATTEMPTS", "1")

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)

        del_res = delegate_tool.delegate(
            task_spec="Worktree recovery task",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )

        daemon.poll_once()

        # Task must NOT be marked done
        task_info = tasks_tool.task_list(status="all")["tasks"][0]
        assert task_info["status"] != "done"
        assert task_info["completed_at"] is None

        # Failed delegation records worktree details in callback_result / notes
        failed_dels = delegate_tool.delegate_list(status="failed")["delegations"]
        assert len(failed_dels) == 1
        assert failed_dels[0]["id"] == del_res["delegation_id"]


def test_shadow_mode_records_agent_event_telemetry():
    """Stage 1 Trust Ladder: Shadow mode records telemetry event to agent_events."""
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=True)
    del_res = delegate_tool.delegate(
        task_spec="Shadow telemetry test task",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )

    res = daemon.poll_once()
    assert res is not None
    assert res["status"] == "shadow_skipped"

    conn = storage.connect()
    try:
        events = conn.execute(
            "SELECT * FROM agent_events WHERE kind='executor_shadow'",
        ).fetchall()
        assert len(events) == 1
        assert events[0]["agent"] == "Desktop_agent3"
        assert events[0]["ref_id"] == str(del_res["delegation_id"])
        assert "would claim task" in events[0]["summary"]
    finally:
        conn.close()


def test_untrusted_task_quarantined_under_stage_2(monkeypatch):
    """Stage 2 Trust Ladder: Untrusted tasks are refused by default and marked quarantined."""
    seen_runs = []

    def fake_pi_run(**kwargs):
        seen_runs.append(kwargs)
        return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_UNTRUSTED", "0")

    del_res = delegate_tool.delegate(
        task_spec="Untrusted external work",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET untrusted=1 WHERE id=?", (del_res["task_id"],))
        conn.commit()
    finally:
        conn.close()

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    res = daemon.poll_once()

    assert res is not None
    assert res["status"] == "quarantined_untrusted"
    assert res["failed_terminal"] is True
    assert len(seen_runs) == 0  # Subprocess was never invoked!

    quarantined_tasks = tasks_tool.task_list(status="blocked")["tasks"]
    assert len(quarantined_tasks) == 1
    assert quarantined_tasks[0]["id"] == del_res["task_id"]
    assert "[failed: untrusted task quarantined (Trust Ladder Stage 2)]" in (
        quarantined_tasks[0]["notes"] or ""
    )

    # Delegation is terminal failed
    failed_dels = delegate_tool.delegate_list(status="failed")["delegations"]
    assert len(failed_dels) == 1
    assert "quarantined" in (failed_dels[0]["callback_result"] or "").lower()


def test_repeated_untrusted_quarantine_does_not_consume_execution_failure_budget(monkeypatch):
    """Quarantine is a policy refusal, not a backend failure for breaker accounting."""
    seen_runs = []

    def fake_pi_run(**kwargs):
        seen_runs.append(kwargs)
        return {"status": "success", "text": "must not run", "exit_code": 0, "error": None}

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_UNTRUSTED", "0")
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", "2")

    task_ids = []
    for label in ("one", "two", "three"):
        del_res = delegate_tool.delegate(
            task_spec=f"Untrusted external work {label}",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        task_ids.append(del_res["task_id"])
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET untrusted=1 WHERE id=?", (del_res["task_id"],))
            conn.commit()
        finally:
            conn.close()

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    results = [daemon.poll_once() for _ in task_ids]

    assert [result["status"] for result in results] == [
        "quarantined_untrusted",
        "quarantined_untrusted",
        "quarantined_untrusted",
    ]
    assert daemon.consecutive_failures == 0
    assert daemon.circuit_breaker_tripped_until == 0.0
    assert seen_runs == []
    assert {task["id"] for task in tasks_tool.task_list(status="blocked")["tasks"]} == set(task_ids)

    conn = storage.connect()
    try:
        events = conn.execute(
            "SELECT * FROM agent_events WHERE kind='executor_circuit_breaker'"
        ).fetchall()
    finally:
        conn.close()
    assert events == []


def test_untrusted_task_allowed_when_explicitly_configured(monkeypatch):
    """Stage 5 Trust Ladder: Untrusted tasks execute when UA_EXECUTOR_ALLOW_UNTRUSTED=1."""
    seen_runs = []

    def fake_pi_run(**kwargs):
        seen_runs.append(kwargs)
        return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", fake_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_UNTRUSTED", "1")

    del_res = delegate_tool.delegate(
        task_spec="Untrusted allowed work",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
    )
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET untrusted=1 WHERE id=?", (del_res["task_id"],))
        conn.commit()
    finally:
        conn.close()

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    res = daemon.poll_once()

    assert res is not None
    assert res["status"] == "success"
    assert len(seen_runs) == 1


def test_circuit_breaker_trips_after_consecutive_failures(monkeypatch):
    """Stage 4 Trust Ladder: Circuit breaker trips after threshold consecutive failures and enforces cooldown."""
    from datetime import UTC, datetime

    curr_time = datetime.now(UTC).timestamp()

    def mock_clock():
        return curr_time

    def failing_pi_run(**kwargs):
        return {
            "status": "error",
            "text": "transient API failure",
            "exit_code": 1,
            "error": "fatal",
        }

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", failing_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_COOLDOWN_S", "60.0")
    monkeypatch.setenv("UA_EXECUTOR_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "1.0")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False, clock=mock_clock)

    # 1st failing delegation
    delegate_tool.delegate(
        task_spec="Task 1", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    res1 = daemon.poll_once()
    assert res1 is not None
    assert daemon.consecutive_failures == 1
    assert daemon.circuit_breaker_tripped_until == 0.0

    # Advance time slightly past task backoff
    curr_time += 5.0

    # 2nd failing execution -> hits threshold (2) and trips circuit breaker!
    # The trip consumes the failure budget, so the counter resets to 0.
    res2 = daemon.poll_once()
    assert res2 is not None
    assert daemon.consecutive_failures == 0
    assert daemon.circuit_breaker_tripped_until == curr_time + 60.0

    # Verify circuit breaker event was recorded
    conn = storage.connect()
    try:
        events = conn.execute(
            "SELECT * FROM agent_events WHERE kind='executor_circuit_breaker'",
        ).fetchall()
        assert len(events) == 1
        assert "Circuit breaker tripped" in events[0]["summary"]
    finally:
        conn.close()

    # Next poll while inside cooldown is blocked by circuit breaker
    res_blocked = daemon.poll_once()
    assert res_blocked is not None
    assert res_blocked["status"] == "circuit_breaker_active"
    assert res_blocked["cooldown_remaining_s"] == 60.0

    # Advance time past the 60s cooldown
    curr_time += 65.0
    res_after = daemon.poll_once()
    # Now it executes again
    assert res_after is not None
    assert res_after["status"] != "circuit_breaker_active"


def test_circuit_breaker_reset_on_trip_requires_fresh_threshold_to_retrip(monkeypatch):
    """Stage 4 Trust Ladder: a trip consumes the failure budget. After cooldown, a single
    further failure must not immediately re-trip the breaker — it takes another full
    `threshold` consecutive failures to trip again."""
    from datetime import UTC, datetime

    curr_time = datetime.now(UTC).timestamp()

    def mock_clock():
        return curr_time

    def failing_pi_run(**kwargs):
        return {
            "status": "error",
            "text": "transient API failure",
            "exit_code": 1,
            "error": "fatal",
        }

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", failing_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_COOLDOWN_S", "60.0")
    monkeypatch.setenv("UA_EXECUTOR_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "1.0")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False, clock=mock_clock)

    delegate_tool.delegate(
        task_spec="Task 1", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )

    # 1st failing execution
    daemon.poll_once()
    assert daemon.consecutive_failures == 1
    assert daemon.circuit_breaker_tripped_until == 0.0

    curr_time += 5.0

    # 2nd failing execution -> hits threshold (2) and trips the breaker.
    # The counter resets to 0 once the trip is recorded.
    daemon.poll_once()
    assert daemon.consecutive_failures == 0
    first_trip_until = daemon.circuit_breaker_tripped_until
    assert first_trip_until == curr_time + 60.0

    # Advance past the cooldown window
    curr_time += 65.0

    # A single further failure after cooldown must NOT re-trip the breaker.
    daemon.poll_once()
    assert daemon.consecutive_failures == 1
    assert daemon.circuit_breaker_tripped_until == first_trip_until

    curr_time += 5.0

    # It takes another full `threshold` (2) consecutive failures to trip again.
    daemon.poll_once()
    assert daemon.consecutive_failures == 0
    assert daemon.circuit_breaker_tripped_until == curr_time + 60.0
    assert daemon.circuit_breaker_tripped_until != first_trip_until

    # Verify both trips were recorded as separate events
    conn = storage.connect()
    try:
        events = conn.execute(
            "SELECT * FROM agent_events WHERE kind='executor_circuit_breaker' ORDER BY id",
        ).fetchall()
        assert len(events) == 2
        assert "Circuit breaker tripped" in events[0]["summary"]
        assert "Circuit breaker tripped" in events[1]["summary"]
    finally:
        conn.close()


def test_circuit_breaker_resets_on_success(monkeypatch):
    """Stage 4 Trust Ladder: Consecutive failures counter resets to 0 upon successful execution."""
    from datetime import UTC, datetime

    curr_time = datetime.now(UTC).timestamp()

    def mock_clock():
        return curr_time

    status_to_return = "error"

    def mock_pi_run(**kwargs):
        if status_to_return == "error":
            return {"status": "error", "text": "err", "exit_code": 1, "error": "err"}
        return {"status": "success", "text": "ok", "exit_code": 0, "error": None}

    monkeypatch.setattr(pi_runner_tool, "pi_run_task", mock_pi_run)
    monkeypatch.setenv("UA_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("UA_EXECUTOR_BACKOFF_BASE_S", "1.0")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False, clock=mock_clock)

    delegate_tool.delegate(
        task_spec="Reset test task", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )

    # 1st attempt fails
    daemon.poll_once()
    assert daemon.consecutive_failures == 1

    # Advance time past backoff window
    curr_time += 5.0

    # 2nd attempt succeeds
    status_to_return = "success"
    daemon.poll_once()
    assert daemon.consecutive_failures == 0


class TestFailurePairGateEligibility:
    """Task #1142 — mechanical from→to failure-pair gate at claim eligibility."""

    @staticmethod
    def _seed_failed_pair(n: int, *, reason: str = "boom"):
        from unified_agent.tools.delegate import delegate, delegate_fail

        for i in range(n):
            r = delegate(
                task_spec=f"Pair failure {i}",
                from_agent="Desktop_Agent5",
                to_agent="server_agent2",
                callback="none",
            )
            delegate_fail(r["delegation_id"], agent="server_agent2", reason=reason)

    def test_find_pending_work_refuses_poisoned_pair_with_failure_pair_gate(self, monkeypatch):
        self._seed_failed_pair(3, reason="provider quota exhausted")
        # Fresh pending work on the same pair must not be claimable.
        pending = delegate_tool.delegate(
            task_spec="Should be gated",
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            callback="none",
        )
        daemon = ExecutorDaemon(agent_label="server_agent2", shadow_mode=False)
        eligible = daemon.find_pending_work()
        assert eligible == []
        assert any(
            r.get("reason") == "failure_pair_gate"
            and r.get("delegation_id") == pending["delegation_id"]
            for r in daemon._last_failure_pair_refusals
        )

    def test_zero_output_failures_feed_failure_pair_gate(self, monkeypatch):
        """#1158 zero_output_attempt reasons must gate the pair like other failures."""
        self._seed_failed_pair(
            3,
            reason=(
                "zero_output_attempt: backend reported success but worktree "
                "has no new commit (clean tree)"
            ),
        )
        pending = delegate_tool.delegate(
            task_spec="Should be gated by zero-output pair",
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            callback="none",
        )
        daemon = ExecutorDaemon(agent_label="server_agent2", shadow_mode=False)
        assert daemon.find_pending_work() == []
        assert any(
            r.get("reason") == "failure_pair_gate"
            and r.get("delegation_id") == pending["delegation_id"]
            for r in daemon._last_failure_pair_refusals
        )
        from unified_agent.tools.delegate import pair_failure_status

        status = pair_failure_status("Desktop_Agent5", "server_agent2")
        assert status["blocked"] is True
        assert status["last_failure_class"] == "zero_output_attempt"

    def test_review_annotation_reset_allows_eligibility_again(self, monkeypatch):
        from unified_agent.tools.delegate import delegate_fail

        seeded = []
        for i in range(3):
            r = delegate_tool.delegate(
                task_spec=f"Pair failure {i}",
                from_agent="Desktop_Agent5",
                to_agent="server_agent2",
                callback="none",
            )
            delegate_fail(r["delegation_id"], agent="server_agent2", reason="boom")
            seeded.append(r)
        tasks_tool.task_update(
            seeded[-1]["task_id"],
            notes="[delegated-to: server_agent2]\n[review: rework]",
            actor="test-agent",
        )
        pending = delegate_tool.delegate(
            task_spec="After review reset",
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            callback="none",
        )
        daemon = ExecutorDaemon(agent_label="server_agent2", shadow_mode=False)
        eligible = daemon.find_pending_work()
        assert [d["id"] for d in eligible] == [pending["delegation_id"]]

    def test_failure_pair_gate_unverifiable_fails_closed(self, monkeypatch):
        """Hub unreachable during the pair check refuses claim (fail-closed)."""
        pending = delegate_tool.delegate(
            task_spec="Gate check must fail closed",
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            callback="none",
        )
        daemon = ExecutorDaemon(agent_label="server_agent2", shadow_mode=False)

        def boom(*_a, **_k):
            raise storage.LiveStoreError("tombstone")

        monkeypatch.setattr(delegate_tool, "pair_failure_status", boom)

        real_call_tool = daemon.hub_client.call_tool

        def failing_hub(tool_name, arguments):
            if tool_name == "delegation_manage" and arguments.get("action") == "failure_pair_gate":
                raise RuntimeError("hub unreachable: simulated")
            return real_call_tool(tool_name, arguments)

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_hub)

        eligible = daemon.find_pending_work()
        assert eligible == []
        assert any(
            r.get("reason") == "failure_pair_gate_unverifiable"
            and r.get("delegation_id") == pending["delegation_id"]
            for r in daemon._last_failure_pair_refusals
        )


class TestDelegationLifecycleHubFallback:
    """Task #1000 — the residual DoD of #987/#992.

    PR #509 wrapped `reclaim_expired_locks`'s `storage.connect()`. PR #516
    wrapped the bounded-autonomy cycle-gate read. Neither touched the other
    five direct `storage.connect()` call sites in this file that mutate/read
    `tasks`/`delegations` rows: `renew_lock`, `_mark_delegation_failed`,
    `_read_delegation_attempts`, `_retry_delegation_to_pending`, and
    `_mark_delegation_completed` (the last of these is the most severe: a
    task the executor fully executed could crash here and never report
    completion). Live proof of the `_mark_delegation_failed` bug: on this
    desktop host, `_mark_delegation_failed`'s `conn = storage.connect()`
    used to sit OUTSIDE its try/except, so a tombstoned local memory.db
    raised straight out of the function, uncaught, crash-looping
    `poll_once` every ~5s trying to terminal-fail a stuck delegation.

    These tests call each fixed method directly (mirroring
    `TestCycleGateHubFallback.test_hub_call_uses_the_get_for_task_action_
    with_the_right_params`), simulating the tombstone by making
    `storage.connect` itself raise `storage.LiveStoreError`, and replacing
    `daemon.hub_client.call_tool` with a fake that answers as a genuinely
    separate, healthy hub would — not the in-process fallback, which would
    reuse (and thus be defeated by) the same monkeypatched `storage.connect`.
    """

    @staticmethod
    def _raise_tombstone(*_args, **_kwargs):
        raise storage.LiveStoreError(
            "memory.db at /home/you/.unified_agent/memory.db is not a "
            "SQLite database (header=b'AUTHORITATIVE me'); check UA_HOME."
        )

    # -- renew_lock ---------------------------------------------------

    def test_renew_lock_lands_via_hub_when_local_storage_is_a_tombstone(self, monkeypatch):
        t = tasks_tool.task_add(title="Long task")
        task_id = t["id"]
        tasks_tool.task_claim(task_id, agent="Desktop_agent3", timeout_seconds=100)

        daemon = ExecutorDaemon(agent_label="Desktop_agent3", lock_timeout_s=3600)
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        hub_calls = []

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            return {"id": task_id, "status": "doing", "claimed_by": "Desktop_agent3"}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        result = daemon.renew_lock(task_id)

        assert result is True
        assert hub_calls == [
            (
                "observability_manage",
                {"action": "run_history", "params": {"task_id": task_id, "limit": 1}},
            ),
            (
                "task_claim",
                {
                    "id": task_id,
                    "agent": "Desktop_agent3",
                    "timeout_seconds": 3600,
                    "expected_run_id": None,
                },
            ),
        ]

    def test_renew_lock_hub_unreachable_too_fails_loudly(self, monkeypatch):
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", lock_timeout_s=3600)
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        def failing_call_tool(tool_name, arguments):
            raise RuntimeError("hub unreachable: simulated network failure")

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        with pytest.raises(RuntimeError, match="could not renew lock"):
            daemon.renew_lock(42)

    # -- _mark_delegation_failed ---------------------------------------

    def test_mark_delegation_failed_lands_via_hub_when_local_storage_is_a_tombstone(
        self, monkeypatch
    ):
        del_res = delegate_tool.delegate(
            task_spec="Mark-failed hub fallback test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        delegation_id = del_res["delegation_id"]
        task_id = del_res["task_id"]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        hub_calls = []

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            return {"failed": True}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        daemon._mark_delegation_failed(delegation_id, task_id, "boom")

        assert hub_calls == [
            (
                "delegation_manage",
                {
                    "action": "fail",
                    "params": {
                        "id": delegation_id,
                        "agent": "Desktop_agent3",
                        "reason": "boom",
                        "expected_claim_generation": None,
                        "task_id": task_id,
                    },
                },
            )
        ]

    def test_mark_delegation_failed_hub_unreachable_too_fails_loudly(self, monkeypatch):
        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        def failing_call_tool(tool_name, arguments):
            raise RuntimeError("hub unreachable: simulated network failure")

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        with pytest.raises(RuntimeError, match="could not mark delegation #7 failed"):
            daemon._mark_delegation_failed(7, 99, "boom")

    # -- _read_delegation_attempts ---------------------------------------

    def test_read_delegation_attempts_lands_via_hub_when_local_storage_is_a_tombstone(
        self, monkeypatch
    ):
        del_res = delegate_tool.delegate(
            task_spec="Attempts read hub fallback test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        delegation_id = del_res["delegation_id"]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        hub_calls = []

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            return {"delegation": {"id": delegation_id, "attempts": 3}}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        attempts = daemon._read_delegation_attempts(delegation_id)

        assert attempts == 3
        assert hub_calls == [
            ("delegation_manage", {"action": "get", "params": {"id": delegation_id}})
        ]

    def test_read_delegation_attempts_hub_unreachable_too_fails_loudly(self, monkeypatch):
        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        def failing_call_tool(tool_name, arguments):
            raise RuntimeError("hub unreachable: simulated network failure")

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        with pytest.raises(RuntimeError, match="could not read attempt count"):
            daemon._read_delegation_attempts(7)

    # -- _retry_delegation_to_pending ---------------------------------------

    def test_retry_delegation_lands_via_hub_when_local_storage_is_a_tombstone(self, monkeypatch):
        del_res = delegate_tool.delegate(
            task_spec="Retry hub fallback test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        delegation_id = del_res["delegation_id"]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        hub_calls = []

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            return {"retried": True}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        daemon._retry_delegation_to_pending(delegation_id, "Attempt 1/3 failed; backoff active")

        assert hub_calls == [
            (
                "delegation_manage",
                {
                    "action": "retry",
                    "params": {
                        "id": delegation_id,
                        "agent": "Desktop_agent3",
                        "reason": "Attempt 1/3 failed; backoff active",
                        "expected_claim_generation": None,
                    },
                },
            )
        ]

    def test_retry_delegation_hub_unreachable_too_fails_loudly(self, monkeypatch):
        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        def failing_call_tool(tool_name, arguments):
            raise RuntimeError("hub unreachable: simulated network failure")

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        with pytest.raises(RuntimeError, match="could not retry delegation #7"):
            daemon._retry_delegation_to_pending(7, "backoff active")

    # -- _mark_delegation_completed ---------------------------------------

    def test_mark_delegation_completed_lands_via_hub_when_local_storage_is_a_tombstone(
        self, monkeypatch
    ):
        del_res = delegate_tool.delegate(
            task_spec="Mark-completed hub fallback test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )
        delegation_id = del_res["delegation_id"]

        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        hub_calls = []

        def call_tool(tool_name, arguments):
            hub_calls.append((tool_name, arguments))
            return {"completed": True}

        monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)

        daemon._mark_delegation_completed(delegation_id, "executor finish-to-review (success)")

        assert hub_calls == [
            (
                "delegation_manage",
                {
                    "action": "complete",
                    "params": {
                        "id": delegation_id,
                        "agent": "Desktop_agent3",
                        "result": "executor finish-to-review (success)",
                        "expected_claim_generation": None,
                    },
                },
            )
        ]

    def test_mark_delegation_completed_hub_unreachable_too_fails_loudly(self, monkeypatch):
        """The MOST SEVERE of the five sites: a fully-executed task's
        completion must never be silently swallowed (the old code just
        logged a warning and moved on) when both local storage and the hub
        are unusable."""
        daemon = ExecutorDaemon(agent_label="Desktop_agent3")
        monkeypatch.setattr(storage, "connect", self._raise_tombstone)

        def failing_call_tool(tool_name, arguments):
            raise RuntimeError("hub unreachable: simulated network failure")

        monkeypatch.setattr(daemon.hub_client, "call_tool", failing_call_tool)

        with pytest.raises(RuntimeError, match="could not mark delegation #7 completed"):
            daemon._mark_delegation_completed(7, "executor finish-to-review (success)")


class TestDelegationManageNewActions:
    """Task #1000's new `delegation_manage` actions (fail/complete/retry/get) —
    the hub-callable equivalents the executor daemon's hub fallback above
    round-trips to. Exercised directly here (not just via the daemon) since
    they are new first-class dispatch-table entries, same as `create`/
    `claim`/`release`/`cancel` already have direct tests
    (TestDelegateClaimAtomicity)."""

    def _delegate(self) -> dict:
        return delegate_tool.delegate(
            task_spec="New delegation_manage actions test",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
        )

    def test_fail_action_sets_terminal_failed_status(self):
        del_res = self._delegate()
        res = delegate_tool.delegation_manage(
            "fail",
            {"id": del_res["delegation_id"], "agent": "Desktop_agent3", "reason": "boom"},
        )
        assert res["failed"] is True
        row = delegate_tool.delegate_list(status="failed")["delegations"][0]
        assert row["id"] == del_res["delegation_id"]
        assert row["callback_result"] == "boom"

    def test_complete_action_sets_completed_status_and_timestamp(self):
        del_res = self._delegate()
        res = delegate_tool.delegation_manage(
            "complete",
            {"id": del_res["delegation_id"], "agent": "Desktop_agent3", "result": "done"},
        )
        assert res["completed"] is True
        row = delegate_tool.delegate_list(status="completed")["delegations"][0]
        assert row["id"] == del_res["delegation_id"]
        assert row["completed_at"] is not None

    def test_retry_action_returns_to_pending_without_touching_attempts(self):
        del_res = self._delegate()
        claimed = delegate_tool.delegate_claim(del_res["delegation_id"], agent="Desktop_agent3")
        res = delegate_tool.delegation_manage(
            "retry",
            {
                "id": del_res["delegation_id"],
                "agent": "Desktop_agent3",
                "reason": "Attempt 1/3 failed; backoff active",
                "expected_claim_generation": claimed["delegation"]["claim_generation"],
            },
        )
        assert res["retried"] is True
        row = delegate_tool.delegate_list(status="pending")["delegations"][0]
        assert row["id"] == del_res["delegation_id"]
        # Unlike delegate_release, retry must not decrement attempts back
        # toward 0 — the attempt already happened.
        assert row["attempts"] == 1

    def test_get_action_returns_the_full_delegation_row(self):
        del_res = self._delegate()
        res = delegate_tool.delegation_manage("get", {"id": del_res["delegation_id"]})
        assert res["delegation"]["id"] == del_res["delegation_id"]
        assert res["delegation"]["attempts"] == 0

    def test_get_action_raises_for_an_unknown_id(self):
        with pytest.raises(ToolError, match="no delegation with id"):
            delegate_tool.delegation_manage("get", {"id": 9999999})


@pytest.mark.parametrize(
    "expiry,reclaimed",
    [
        ("2026-09-30T04:00:01+00:00", 0),
        ("2026-09-30T04:00:00+00:00", 1),
        ("2026-09-30T03:59:59+00:00", 1),
    ],
)
def test_executor_startup_reclaims_at_exact_expiry(monkeypatch, expiry, reclaimed):
    now = "2026-09-30T04:00:00+00:00"
    monkeypatch.setattr(storage, "now", lambda: now)
    task_id = tasks_tool.task_add("Expiry startup")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing',claimed_by='Desktop_agent3',lock_expires_at=? WHERE id=?",
            (expiry, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    result = ExecutorDaemon(agent_label="Desktop_agent3").reclaim_expired_locks()
    assert result["reclaimed_count"] == reclaimed
    task = tasks_tool.task_list(status="all")["tasks"][0]
    assert task["status"] == ("open" if reclaimed else "doing")


def test_failed_periodic_reclaim_retries_next_tick(monkeypatch):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", clock=lambda: 100.0)
    responses = iter([{"skipped": True, "reason": "RuntimeError"}, {"reclaimed_count": 0}])
    monkeypatch.setattr(daemon, "reclaim_expired_locks", lambda: next(responses))
    assert daemon.maybe_reclaim_expired_locks()["skipped"]
    assert daemon._last_reclaim_at is None
    assert daemon.maybe_reclaim_expired_locks() == {"reclaimed_count": 0}
    assert daemon._last_reclaim_at == 100.0
    assert daemon.maybe_reclaim_expired_locks() is None
