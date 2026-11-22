"""Real Git proof: stale host commits cannot touch a successor run's index/ref."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_agent import config, storage
from unified_agent.executor.daemon import ExecutorDaemon, ExecutorOwnershipLost, HubClient
from unified_agent.tools import delegate as delegation_tools
from unified_agent.tools import tasks


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def isolated_runs(isolated_env, workspace_is_product_repo):
    root = Path(isolated_env)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "base.txt").write_text("base")
    git(root, "add", "base.txt")
    git(root, "commit", "-m", "base")
    old = root / ".worktrees" / "task-17-old"
    successor = root / ".worktrees" / "task-17-successor"
    git(root, "worktree", "add", "-b", "task/17-old", str(old))
    git(root, "worktree", "add", "-b", "task/17-successor", str(successor))
    (old / "old.txt").write_text("old worker result")
    (successor / "successor.txt").write_text("successor dirty data")
    git(successor, "add", "successor.txt")
    current = {"run_id": "sp-old", "generation": 1}
    calls = []

    def hub(tool, arguments):
        if tool == "delegation_manage":
            assert arguments == {"action": "get", "params": {"id": 99}}
            return {
                "delegation": {
                    "id": 99,
                    "task_id": 17,
                    "to_agent": "Desktop_Agent2",
                    "status": "in_flight",
                    "claim_generation": current["generation"],
                }
            }
        assert tool == "observability_manage"
        assert arguments == {"action": "run_history", "params": {"task_id": 17, "limit": 1}}
        calls.append(current["run_id"])
        return {
            "runs": [
                {
                    "run_id": current["run_id"],
                    "state": "open",
                    "cancel_requested": False,
                    "native_execution_allowed": True,
                }
            ]
        }

    daemon = object.__new__(ExecutorDaemon)
    daemon.agent_label = "Desktop_Agent2"
    daemon.hub_client = SimpleNamespace(authoritative_run_checks=True, _call_over_hub=hub)
    return daemon, old, successor, current, calls


def snapshot(repo):
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "index": git(repo, "write-tree"),
        "status": git(repo, "status", "--porcelain"),
        "file": (repo / "successor.txt").read_bytes(),
    }


@pytest.mark.parametrize("replacement", ["run", "generation"])
@pytest.mark.parametrize("barrier", ["before-check", "before-add", "before-commit"])
def test_takeover_at_final_git_boundary_keeps_successor_ref_index_and_files(
    isolated_runs, monkeypatch, barrier, replacement
):
    daemon, old, successor, current, calls = isolated_runs
    before = snapshot(successor)

    def takeover():
        if replacement == "run":
            current["run_id"] = "sp-successor"
        else:
            current["generation"] = 2

    if barrier == "before-check":
        takeover()
    original_git = daemon._git

    def at_barrier(repo, args, **kwargs):
        if (barrier == "before-add" and args[0] == "add") or (
            barrier == "before-commit" and "commit" in args
        ):
            takeover()
        return original_git(repo, args, **kwargs)

    monkeypatch.setattr(daemon, "_git", at_barrier)
    with pytest.raises(ExecutorOwnershipLost) as lost:
        daemon._commit_child_changes(
            task_id=17,
            worktree=str(old),
            branch="task/17-old",
            expected_run_id="sp-old",
            delegation_id=99,
            expected_claim_generation=1,
        )
    assert snapshot(successor) == before
    if barrier == "before-check":
        assert git(old, "status", "--porcelain")
        assert lost.value.commit is None
    else:
        assert not git(old, "status", "--porcelain")
        assert lost.value.commit["sha"] == git(old, "rev-parse", "HEAD")
    assert calls[-1] == ("sp-successor" if replacement == "run" else "sp-old")


def test_current_run_commit_is_retained_for_review(isolated_runs):
    daemon, old, successor, current, calls = isolated_runs
    before = snapshot(successor)
    result = daemon._commit_child_changes(
        task_id=17,
        worktree=str(old),
        branch="task/17-old",
        expected_run_id="sp-old",
        delegation_id=99,
        expected_claim_generation=1,
    )
    assert result["sha"] == git(old, "rev-parse", "HEAD")
    assert result["status"] == "committed"
    assert snapshot(successor) == before
    assert calls == ["sp-old", "sp-old"]


@pytest.mark.parametrize("replacement", ["run", "generation"])
@pytest.mark.parametrize("barrier", ["before-add", "before-finish-update"])
def test_real_daemon_takeover_after_last_check_preserves_canonical_successor(
    isolated_runs, monkeypatch, replacement, barrier
):
    _, root_old, _, _, _ = isolated_runs
    root = root_old.parent.parent
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(root / "empty-sessions"))
    (root / "empty-sessions").mkdir()
    created = delegation_tools.delegate(
        "Implement isolated output", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            cwd = (root / kwargs["cwd"]).resolve()
            (cwd / "old-result.txt").write_text("old run output")
            return {"status": "success", "text": "completed old output"}

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend())
    monkeypatch.setattr(daemon, "_cycle_gate_for_task", lambda _id: None)
    monkeypatch.setattr(daemon, "_lifecycle_stop_check", lambda: lambda: False)
    original_git = daemon._git
    observed = {}

    def take_over(repo):
        if not observed:
            with storage.connect() as conn:
                if replacement == "run":
                    conn.execute(
                        "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                        (claim["id"],),
                    )
                    conn.commit()
            token = config.agent_identity_var.set("Desktop_Agent2")
            try:
                if replacement == "generation":
                    assert delegation_tools.delegate_retry(
                        delegation["id"],
                        "Desktop_Agent2",
                        expected_claim_generation=delegation["claim_generation"],
                        expected_run_id=claim["current_run_id"],
                        task_id=claim["id"],
                    )["retried"]
                    next_generation = delegation_tools.delegate_claim(
                        delegation["id"], "Desktop_Agent2"
                    )["delegation"]["claim_generation"]
                else:
                    next_generation = delegation["claim_generation"]
                next_claim = tasks.task_claim(claim["id"], "Desktop_Agent2")
                next_claim = tasks.task_update(
                    claim["id"],
                    worktree=True,
                    expected_run_id=next_claim["current_run_id"],
                    expected_delegation_id=delegation["id"],
                    expected_claim_generation=next_generation,
                )
            finally:
                config.agent_identity_var.reset(token)
            successor = (root / next_claim["worktree_path"]).resolve()
            assert successor != Path(repo).resolve()
            (successor / "successor.txt").write_text("new worker staged output")
            git(successor, "add", "successor.txt")
            observed.update(path=successor, before=snapshot(successor), claim=next_claim)

    def takeover_before_add(repo, args, **kwargs):
        if barrier == "before-add" and args[0] == "add":
            take_over(repo)
        return original_git(repo, args, **kwargs)

    original_call = daemon.hub_client.call_tool

    def takeover_before_finish(tool, arguments):
        if (
            barrier == "before-finish-update"
            and tool == "task_update"
            and arguments.get("evidence_ref")
        ):
            with storage.connect() as conn:
                current = conn.execute(
                    "SELECT worktree_path FROM tasks WHERE id=?", (claim["id"],)
                ).fetchone()
            take_over(root / current["worktree_path"])
        return original_call(tool, arguments)

    monkeypatch.setattr(daemon.hub_client, "call_tool", takeover_before_finish)
    monkeypatch.setattr(daemon, "_git", takeover_before_add)
    result = daemon._execute_claimed_task(delegation, claim)
    assert result["status"] == "run_not_current"
    assert result["commit"]["sha"]
    assert snapshot(observed["path"]) == observed["before"]
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
        assert row["worktree_path"] == observed["claim"]["worktree_path"]
        assert row["branch"] == observed["claim"]["branch"]
        assert "[waiting-for: review]" not in row["notes"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM executor_attempts WHERE outcome='waiting_review'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("action", ["fail", "retry", "complete"])
def test_terminal_transition_uses_hub_cas_even_with_usable_local_store(monkeypatch, action):
    daemon = object.__new__(ExecutorDaemon)
    daemon.agent_label = "Desktop_Agent2"
    calls = []
    key = {"fail": "failed", "retry": "retried", "complete": "completed"}[action]

    def authority(tool, arguments):
        calls.append((tool, arguments))
        return {key: False, "reason": "run_not_current"}

    daemon.hub_client = SimpleNamespace(authoritative_run_checks=True, call_tool=authority)
    monkeypatch.setattr(
        storage,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("stale local authority used")),
    )
    with pytest.raises(ExecutorOwnershipLost):
        daemon._delegation_transition(action, 99, 3, task_id=17, expected_run_id="sp-old")
    assert calls == [
        (
            "delegation_manage",
            {
                "action": action,
                "params": {
                    "id": 99,
                    "agent": "Desktop_Agent2",
                    "expected_claim_generation": 3,
                    "task_id": 17,
                    "expected_run_id": "sp-old",
                },
            },
        )
    ]


def test_remote_renewal_uses_exact_hub_run_instead_of_usable_local_db(monkeypatch):
    daemon = object.__new__(ExecutorDaemon)
    daemon.agent_label = "Desktop_Agent2"
    daemon.lock_timeout_s = 60
    calls = []

    def authority(tool, arguments):
        calls.append((tool, arguments))
        if tool == "observability_manage":
            return {
                "runs": [
                    {
                        "run_id": "sp-old",
                        "state": "open",
                        "cancel_requested": False,
                        "native_execution_allowed": True,
                    }
                ]
            }
        assert tool == "task_claim"
        return {
            "id": 17,
            "current_run_id": "sp-successor",
            "claimed_by": "Desktop_Agent2",
            "status": "doing",
            "native_execution_allowed": True,
        }

    daemon.hub_client = SimpleNamespace(authoritative_run_checks=True, _call_over_hub=authority)
    monkeypatch.setattr(
        storage, "connect", lambda: (_ for _ in ()).throw(AssertionError("local lease write used"))
    )
    assert daemon.renew_lock(17, "sp-old") is False
    assert calls[-1] == (
        "task_claim",
        {"id": 17, "agent": "Desktop_Agent2", "timeout_seconds": 60, "expected_run_id": "sp-old"},
    )


@pytest.mark.parametrize("stage", ["before-provision", "before-backend"])
@pytest.mark.parametrize("failure", ["unavailable", "malformed-generation"])
def test_canonical_attempt_admission_failure_never_starts_backend(
    isolated_runs, monkeypatch, stage, failure
):
    _, old, _, _, _ = isolated_runs
    root = old.parent.parent
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(root / "empty-sessions"))
    created = delegation_tools.delegate(
        "Never execute without canonical authority",
        "Desktop_Agent1",
        "Desktop_Agent2",
        callback="none",
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)

    class Backend:
        name = "synthetic"
        calls = 0

        def run(self, **kwargs):
            self.calls += 1
            raise AssertionError("unverified attempt reached backend")

    backend = Backend()
    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=backend)
    daemon.hub_client.authoritative_run_checks = True
    broken = {"active": stage == "before-provision"}

    def authority(tool, arguments):
        if broken["active"] and tool == "delegation_manage" and arguments["action"] == "get":
            if failure == "unavailable":
                raise RuntimeError("synthetic authority unavailable")
            result = daemon.hub_client._direct_tool_fallback(tool, arguments)
            result["delegation"]["claim_generation"] = None
            return result
        return daemon.hub_client._direct_tool_fallback(tool, arguments)

    monkeypatch.setattr(daemon.hub_client, "_call_over_hub", authority)
    monkeypatch.setattr(daemon.hub_client, "call_tool", authority)
    monkeypatch.setattr(daemon, "_cycle_gate_for_task", lambda _id: None)
    monkeypatch.setattr(daemon, "_lifecycle_stop_check", lambda: lambda: False)
    if stage == "before-backend":
        from unified_agent.executor import daemon as daemon_module

        original_start = daemon_module.registry.start_session

        def lose_authority_after_session(*args, **kwargs):
            session = original_start(*args, **kwargs)
            broken["active"] = True
            return session

        monkeypatch.setattr(daemon_module.registry, "start_session", lose_authority_after_session)
    result = daemon._execute_claimed_task(delegation, claim)
    assert result["status"] == (
        "claim_not_current" if stage == "before-provision" else "run_not_current"
    )
    assert backend.calls == 0
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM executor_attempts WHERE outcome='waiting_review'"
            ).fetchone()[0]
            == 0
        )
        if stage == "before-backend":
            assert conn.execute("SELECT outcome FROM executor_attempts").fetchone()[0] == "killed"


def wire_canonical_rpc_peer(client, monkeypatch, *, transform=None):
    """Use real MCP handshake/decoding and canonical APIs with a verified peer."""
    calls = []

    def post(body, timeout=10):
        headers = {"mcp-session-id": "synthetic-session", "content-type": "application/json"}
        if body["method"] != "tools/call":
            return 200, headers, json.dumps({"result": {}})
        tool = body["params"]["name"]
        arguments = body["params"]["arguments"]
        calls.append((tool, arguments))
        result = client._direct_tool_fallback(tool, arguments)
        if transform is not None:
            result = transform(tool, arguments, result)
        return 200, headers, json.dumps({"result": {"structuredContent": result}})

    monkeypatch.setattr(client, "_post", post)
    return calls


@pytest.mark.parametrize("lane", ["dispatch-ready", "Desktop_Agent2"])
def test_remote_renewal_preserves_actual_canonical_claim_lane(isolated_runs, monkeypatch, lane):
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        task = tasks.task_add(title="Renew an authorized lane", lane=lane)
        claim = tasks.task_claim(
            task["id"], "Desktop_Agent2", timeout_seconds=60, expected_lane=lane
        )
    finally:
        config.agent_identity_var.reset(token)
    daemon = object.__new__(ExecutorDaemon)
    daemon.agent_label = "Desktop_Agent2"
    daemon.lock_timeout_s = 60
    daemon.hub_client = HubClient(
        agent_label="Desktop_Agent2",
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    calls = wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    assert daemon.hub_client.authoritative_run_checks is True
    assert daemon.renew_lock(task["id"], claim["current_run_id"], expected_lane=claim["lane"])
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        assert row["current_run_id"] == claim["current_run_id"]
        assert row["claimed_by"] == "Desktop_Agent2"
        assert row["lane"] == lane
        assert row["lock_expires_at"] >= claim["lock_expires_at"]
    assert calls[-1][0] == "task_claim"
    assert calls[-1][1]["expected_lane"] == lane
    assert config.agent_identity_var.get() is None


@pytest.mark.parametrize("authoritative", [True, False])
@pytest.mark.parametrize(
    "failure", ["provisioning", "source-sha", "localization", "rpc", "missing"]
)
def test_remote_checkout_failure_never_runs_backend_in_shared_workspace(
    isolated_runs, monkeypatch, failure, authoritative
):
    _, old, _, _, _ = isolated_runs
    root = old.parent.parent
    created = delegation_tools.delegate(
        "Code output requires isolation", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)

    class Backend:
        name = "synthetic"
        calls = 0

        def run(self, **kwargs):
            self.calls += 1
            (root / "shared-output.txt").write_text("unsafe workspace output")
            return {"status": "success", "text": "unsafe"}

    backend = Backend()
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        shadow_mode=False,
        backend=backend,
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    monkeypatch.setattr(daemon, "_cycle_gate_for_task", lambda _id: None)
    monkeypatch.setattr(daemon, "_lifecycle_stop_check", lambda: lambda: False)

    def transform(tool, arguments, result):
        if failure == "source-sha" and tool == "task_update" and arguments.get("worktree"):
            result.pop("worktree_source_sha", None)
        if failure == "missing" and tool == "task_update" and arguments.get("worktree"):
            result["worktree_path"] = None
        return result

    wire_canonical_rpc_peer(daemon.hub_client, monkeypatch, transform=transform)
    daemon.hub_client.authoritative_run_checks = authoritative
    original_call = daemon.hub_client.call_tool

    def failed_rpc(tool, arguments):
        if failure == "rpc" and tool == "task_update" and arguments.get("worktree"):
            raise RuntimeError("synthetic provisioning RPC failure")
        return original_call(tool, arguments)

    monkeypatch.setattr(daemon.hub_client, "call_tool", failed_rpc)
    if failure == "provisioning":

        def unavailable_checkout(*_args, **_kwargs):
            raise OSError("synthetic git worktree creation failure")

        monkeypatch.setattr(
            tasks, "_git_worktree_add_from_known_project_repo", unavailable_checkout
        )
    elif failure == "localization":
        monkeypatch.setattr(daemon, "localize_worktree", lambda *_args, **_kwargs: None)
    root_head = git(root, "rev-parse", "HEAD")
    root_index = git(root, "write-tree")
    result = daemon._execute_claimed_task(delegation, claim)
    assert result["status"] == "provisioning_failed"
    with storage.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
        assert task["status"] == "blocked"
        assert task["current_run_id"] is None
        assert (
            conn.execute(
                "SELECT status FROM delegations WHERE id=?", (delegation["id"],)
            ).fetchone()[0]
            == "failed"
        )
    assert backend.calls == 0
    assert not (root / "shared-output.txt").exists()
    assert git(root, "rev-parse", "HEAD") == root_head
    assert git(root, "write-tree") == root_index
    assert (root / claim["worktree_path"]).exists()


@pytest.mark.parametrize(
    "exception", [RuntimeError("hub unavailable"), tasks.ToolError("inventory denied")]
)
def test_recovery_inventory_outage_uses_no_readable_local_authority(
    isolated_runs, monkeypatch, exception
):
    task = tasks.task_add(title="Preserve readable local state")
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        tasks.task_claim(task["id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)
    with storage.connect() as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    calls = []

    def unavailable_rpc(tool, arguments):
        calls.append((tool, arguments))
        raise exception

    monkeypatch.setattr(daemon.hub_client, "call_tool", unavailable_rpc)
    result = daemon.reclaim_expired_locks()
    assert result["skipped"] is True
    assert result["reclaimed_count"] == result["released_delegations"] == 0
    assert [tool for tool, _args in calls] == ["task_list"]
    with storage.connect() as conn:
        assert (
            dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()) == before
        )


def test_scout_report_path_is_unique_to_run_and_generation(isolated_runs, monkeypatch):
    from unified_agent.scout import default_report_relpath, report_path_from_text

    _, old, _, _, _ = isolated_runs
    root = old.parent.parent
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    created = delegation_tools.delegate(
        "[task-shape: scout]\nInvestigate isolated reports",
        "Desktop_Agent1",
        "Desktop_Agent2",
        callback="none",
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)
    observed = {}

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            report = report_path_from_text(kwargs["prompt"])
            assert claim["current_run_id"] in report
            assert (
                f"delegation-{delegation['id']}-generation-{delegation['claim_generation']}"
                in report
            )
            old_artifact = root / "reports" / report
            old_artifact.parent.mkdir(parents=True, exist_ok=True)
            old_artifact.write_text("old attempt report")
            observed.update(old=old_artifact, path=report)
            token = config.agent_identity_var.set("Desktop_Agent2")
            try:
                assert delegation_tools.delegate_retry(
                    delegation["id"],
                    "Desktop_Agent2",
                    expected_claim_generation=delegation["claim_generation"],
                    expected_run_id=claim["current_run_id"],
                    task_id=claim["id"],
                )["retried"]
                new_generation = delegation_tools.delegate_claim(
                    delegation["id"], "Desktop_Agent2"
                )["delegation"]["claim_generation"]
                tasks.task_update(
                    claim["id"],
                    worktree=True,
                    expected_run_id=claim["current_run_id"],
                    expected_delegation_id=delegation["id"],
                    expected_claim_generation=new_generation,
                )
            finally:
                config.agent_identity_var.reset(token)
            successor_path = default_report_relpath(
                f"task-{claim['id']}-{claim['current_run_id']}-delegation-{delegation['id']}-generation-{new_generation}"
            )
            successor_artifact = root / "reports" / successor_path
            assert successor_artifact != old_artifact
            successor_artifact.write_text("successor report")
            observed["successor"] = successor_artifact
            return {
                "status": "success",
                "text": f"investigation complete\n[scout: report={report}]",
            }

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=Backend())
    monkeypatch.setattr(daemon, "_cycle_gate_for_task", lambda _id: None)
    monkeypatch.setattr(daemon, "_lifecycle_stop_check", lambda: lambda: False)
    result = daemon._execute_claimed_task(delegation, claim)
    assert observed["old"].read_text() == "old attempt report"
    assert result["status"] == "run_not_current"
    assert observed["successor"].read_text() == "successor report"


def test_remote_cancelled_prestart_retires_delegation_and_releases_exact_claim(
    isolated_runs, monkeypatch
):
    created = delegation_tools.delegate(
        "Cancelled admission", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
        tasks.task_update(
            claim["id"],
            request_cancel=True,
            expected_run_id=claim["current_run_id"],
            worktree=False,
        )
    finally:
        config.agent_identity_var.reset(token)

    class Backend:
        name = "synthetic"

        def run(self, **kwargs):
            raise AssertionError("cancelled backend started")

    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        shadow_mode=False,
        backend=Backend(),
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    result = daemon._execute_claimed_task(delegation, claim)
    assert result["status"] == "blocked_by_trust_preset"
    assert result["cleanup_error"] is None
    with storage.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
        assert task["status"] == "blocked"
        assert task["current_run_id"] is None
        assert task["claimed_by"] is None
    assert delegation_tools.delegate_get(delegation["id"])["delegation"]["status"] == "cancelled"


def test_recovery_uses_complete_task_inventory_when_agent_has_200_inflight(
    isolated_runs, monkeypatch
):
    created = delegation_tools.delegate(
        "Expired recoverable attempt", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)
    unrelated = tasks.task_add("Unrelated inventory")
    with storage.connect() as conn:
        source = dict(
            conn.execute("SELECT * FROM delegations WHERE id=?", (delegation["id"],)).fetchone()
        )
        source.pop("id")
        source["task_id"] = unrelated["id"]
        columns = ",".join(source)
        placeholders = ",".join("?" for _ in source)
        conn.executemany(
            f"INSERT INTO delegations ({columns}) VALUES ({placeholders})",
            [tuple(source.values())] * 200,
        )
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (claim["id"],),
        )
        conn.commit()
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    calls = wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    result = daemon.reclaim_expired_locks()
    assert result["reclaimed_count"] == result["released_delegations"] == 1
    assert delegation_tools.delegate_get(delegation["id"])["delegation"]["status"] == "pending"
    with storage.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
        assert task["status"] == "open"
        assert task["claimed_by"] is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM delegations WHERE task_id=? AND status='in_flight'",
                (unrelated["id"],),
            ).fetchone()[0]
            == 200
        )
    assert any(
        tool == "delegation_manage"
        and args["action"] == "list"
        and args["params"].get("task_id") == claim["id"]
        for tool, args in calls
    )


@pytest.mark.parametrize(
    "stage",
    [
        "before-provision",
        "during-provision",
        "provisioning-fail-race",
        "after-localization",
        "before-backend",
        "post-backend",
        "post-backend-retired",
        "before-commit",
        "during-commit",
        "before-handoff",
    ],
)
def test_cancelled_current_attempt_closes_run_and_releases_claim(isolated_runs, monkeypatch, stage):
    _, old, _, _, _ = isolated_runs
    root = old.parent.parent
    created = delegation_tools.delegate(
        "Cancellation cleanup", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)

    def cancel():
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            tasks.task_update(
                claim["id"],
                request_cancel=True,
                expected_run_id=claim["current_run_id"],
                worktree=False,
            )
        finally:
            config.agent_identity_var.reset(token)

    class Backend:
        name = "synthetic"
        calls = 0

        def run(self, **kwargs):
            self.calls += 1
            (root / kwargs["cwd"] / "cancelled-output.txt").write_text("retain cancelled output")
            if stage in ("post-backend", "post-backend-retired"):
                cancel()
                if stage == "post-backend-retired":
                    token = config.agent_identity_var.set("Desktop_Agent2")
                    try:
                        delegation_tools.delegate_cancel(
                            delegation["id"],
                            "Desktop_Agent2",
                            expected_run_id=claim["current_run_id"],
                            expected_claim_generation=delegation["claim_generation"],
                        )
                    finally:
                        config.agent_identity_var.reset(token)
            return {"status": "success", "text": "output before cancellation"}

    backend = Backend()
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        shadow_mode=False,
        backend=backend,
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    monkeypatch.setattr(daemon, "_lifecycle_stop_check", lambda: lambda: False)

    def cycle_gate(_id):
        if stage == "before-provision":
            cancel()
        return None

    monkeypatch.setattr(daemon, "_cycle_gate_for_task", cycle_gate)
    original_add = tasks._git_worktree_add_from_known_project_repo

    def worktree_add(*args, **kwargs):
        if stage == "during-provision":
            cancel()
        return original_add(*args, **kwargs)

    monkeypatch.setattr(tasks, "_git_worktree_add_from_known_project_repo", worktree_add)
    original_localize = daemon.localize_worktree

    def localize(*args, **kwargs):
        path = original_localize(*args, **kwargs)
        if stage == "after-localization":
            cancel()
        return path

    monkeypatch.setattr(daemon, "localize_worktree", localize)
    from unified_agent.executor import daemon as daemon_module

    original_start = daemon_module.registry.start_session

    def start_session(*args, **kwargs):
        session = original_start(*args, **kwargs)
        if stage == "before-backend":
            cancel()
        return session

    monkeypatch.setattr(daemon_module.registry, "start_session", start_session)
    original_commit = daemon._commit_child_changes

    def commit(**kwargs):
        if stage == "before-commit":
            cancel()
        return original_commit(**kwargs)

    monkeypatch.setattr(daemon, "_commit_child_changes", commit)
    original_git = daemon._git

    def git_call(repo, args, **kwargs):
        if stage == "during-commit" and args[0] == "add":
            cancel()
        return original_git(repo, args, **kwargs)

    monkeypatch.setattr(daemon, "_git", git_call)
    original_call = daemon.hub_client.call_tool

    def call(tool, arguments):
        if stage == "provisioning-fail-race":
            if tool == "task_update" and arguments.get("worktree"):
                raise RuntimeError("synthetic provisioning failure")
            if tool == "delegation_manage" and arguments.get("action") == "fail":
                cancel()
        if stage == "before-handoff" and tool == "task_update" and arguments.get("evidence_ref"):
            cancel()
        return original_call(tool, arguments)

    monkeypatch.setattr(daemon.hub_client, "call_tool", call)
    result = daemon._execute_claimed_task(delegation, claim)
    assert result["status"] in ("claim_not_current", "run_not_current")
    assert backend.calls == (
        0
        if stage
        in (
            "before-provision",
            "during-provision",
            "provisioning-fail-race",
            "after-localization",
            "before-backend",
        )
        else 1
    )
    with storage.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
        assert task["status"] == "cancelled"
        assert task["current_run_id"] is None
        assert task["claimed_by"] is None
        assert not task["cancel_requested"]
        assert "[waiting-for: review]" not in task["notes"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM executor_attempts WHERE outcome='waiting_review'"
            ).fetchone()[0]
            == 0
        )
        if backend.calls:
            assert (
                root / task["worktree_path"] / "cancelled-output.txt"
            ).read_text() == "retain cancelled output"
    assert delegation_tools.delegate_get(delegation["id"])["delegation"]["status"] == "cancelled"


@pytest.mark.parametrize("replacement", ["run", "generation"])
def test_cancel_cleanup_cas_preserves_successor(isolated_runs, monkeypatch, replacement):
    created = delegation_tools.delegate(
        "Cleanup race", "Desktop_Agent1", "Desktop_Agent2", callback="none"
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        delegation = delegation_tools.delegate_claim(created["delegation_id"], "Desktop_Agent2")[
            "delegation"
        ]
        claim = tasks.task_claim(created["task_id"], "Desktop_Agent2")
        tasks.task_update(
            claim["id"],
            request_cancel=True,
            expected_run_id=claim["current_run_id"],
            worktree=False,
        )
    finally:
        config.agent_identity_var.reset(token)
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2",
        hub_url="http://hub.invalid/mcp",
        bearer_token="synthetic-only",
    )
    wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    original_call = daemon.hub_client.call_tool
    successor = {}

    def call(tool, arguments):
        if tool == "task_update" and arguments.get("status") == "cancelled":
            token = config.agent_identity_var.set("Desktop_Agent2")
            try:
                if replacement == "run":
                    tasks.task_update(
                        claim["id"],
                        status="blocked",
                        release_claim=True,
                        expected_run_id=claim["current_run_id"],
                        worktree=False,
                    )
                    tasks.task_claim(claim["id"], "Desktop_Agent2")
                else:
                    # Simulate an independent recovery which has advanced the
                    # stored attempt while this cleanup RPC is in transit.
                    with storage.connect() as conn:
                        conn.execute(
                            "UPDATE delegations SET status='pending' WHERE id=?",
                            (delegation["id"],),
                        )
                        conn.commit()
                    delegation_tools.delegate_claim(delegation["id"], "Desktop_Agent2")
                with storage.connect() as conn:
                    successor["task"] = dict(
                        conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone()
                    )
                    successor["delegation"] = dict(
                        conn.execute(
                            "SELECT * FROM delegations WHERE id=?", (delegation["id"],)
                        ).fetchone()
                    )
            finally:
                config.agent_identity_var.reset(token)
        return original_call(tool, arguments)

    monkeypatch.setattr(daemon.hub_client, "call_tool", call)
    assert daemon._retire_stopped_delegation(
        delegation, run_id=claim["current_run_id"], reason="cancelled worker"
    )
    assert successor, "cancelled current run must attempt fenced task cleanup"
    with storage.connect() as conn:
        assert (
            dict(conn.execute("SELECT * FROM tasks WHERE id=?", (claim["id"],)).fetchone())
            == successor["task"]
        )
        assert (
            dict(
                conn.execute("SELECT * FROM delegations WHERE id=?", (delegation["id"],)).fetchone()
            )
            == successor["delegation"]
        )
