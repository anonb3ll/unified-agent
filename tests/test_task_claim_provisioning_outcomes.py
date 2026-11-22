"""A committed claim remains readable without bypassing native authority."""

import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools import tasks


@pytest.fixture
def repo(isolated_env, workspace_is_product_repo):
    subprocess.run(
        ["git", "init", "-b", "main", str(isolated_env)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(isolated_env),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    return isolated_env


@pytest.mark.parametrize("notes", ["", "[code-task]"])
def test_denied_hub_workspace_returns_native_claim_without_provisioning(repo, monkeypatch, notes):
    item = tasks.task_add("Native claim", notes=notes)
    attempted = []
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *a, **kw: attempted.append(1))
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        result = tasks.task_claim(item["id"], "Desktop_Agent2")
    finally:
        config.workspace_root_var.reset(token)
    assert result["status"] == "doing"
    assert result["current_run_id"]
    assert result["native_execution_allowed"] is True
    assert result["worktree_path"] is None
    assert result["worktree_source_sha"] is None
    assert result["provisioning"] == {"status": "native-required", "reason": "workspace_denied"}
    assert attempted == []
    assert not (repo / ".worktrees").exists()


def test_denied_renewal_preserves_run_and_reports_extended_lease(repo):
    item = tasks.task_add("Renew without hub files")
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        first = tasks.task_claim(item["id"], "Desktop_Agent2", timeout_seconds=60)
        second = tasks.task_claim(
            item["id"],
            "Desktop_Agent2",
            timeout_seconds=3600,
            expected_run_id=first["current_run_id"],
        )
    finally:
        config.workspace_root_var.reset(token)
    assert second["current_run_id"] == first["current_run_id"]
    assert second["lock_expires_at"] > first["lock_expires_at"]
    assert second["provisioning"]["reason"] == "workspace_denied"
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE channel=? AND json_extract(payload,'$.kind')=? AND json_extract(payload,'$.task_id')=?",
                (tasks.run_records.CHANNEL, tasks.run_records.KIND_CLAIMED, item["id"]),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_optional_provision_failure_reports_committed_claim(repo, monkeypatch):
    item = tasks.task_add("Provision unavailable")

    def fail(*args, **kwargs):
        raise OSError("simulated unavailable checkout")

    monkeypatch.setattr(tasks, "_ensure_task_worktree", fail)
    result = tasks.task_claim(item["id"], "Desktop_Agent2")
    assert result["status"] == "doing" and result["current_run_id"]
    assert result["provisioning"] == {"status": "unavailable", "reason": "worktree_unavailable"}
    assert result["worktree_path"] is None


@pytest.mark.parametrize("change", ["takeover", "cancel", "trust"])
def test_postcommit_provision_failure_never_returns_stale_or_denied_claim(
    repo, monkeypatch, change
):
    item = tasks.task_add("Postcommit barrier")
    denied = []
    original_native = tasks.run_records.native_execution_status

    def native(*args, **kwargs):
        return (
            {
                "native_execution_allowed": False,
                "native_execution_denial": "native authority revoked",
            }
            if denied
            else original_native(*args, **kwargs)
        )

    monkeypatch.setattr(tasks.run_records, "native_execution_status", native)

    def barrier(*args, **kwargs):
        if change == "trust":
            denied.append(True)
        else:
            conn = storage.connect()
            try:
                if change == "takeover":
                    conn.execute(
                        "UPDATE tasks SET current_run_id='sp-successor',current_run_agent='Desktop_agent3',claimed_by='Desktop_agent3' WHERE id=?",
                        (item["id"],),
                    )
                else:
                    conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (item["id"],))
                conn.commit()
            finally:
                conn.close()
        raise OSError("provisioning failed after authority changed")

    monkeypatch.setattr(tasks, "_ensure_task_worktree", barrier)
    with pytest.raises(
        ToolError, match="native authority revoked" if change == "trust" else "run_not_current"
    ):
        tasks.task_claim(item["id"], "Desktop_Agent2")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT current_run_id,claimed_by,cancel_requested FROM tasks WHERE id=?", (item["id"],)
        ).fetchone()
        if change == "takeover":
            assert tuple(row) == ("sp-successor", "Desktop_agent3", 0)
        elif change == "cancel":
            assert row["cancel_requested"] == 1
    finally:
        conn.close()


def test_denied_renewal_does_not_probe_existing_native_checkout(repo, monkeypatch):
    item = tasks.task_add("Existing checkout")
    first = tasks.task_claim(item["id"], "Desktop_Agent2")
    assert first["worktree_path"]

    def refuse_probe(*args, **kwargs):
        pytest.fail("DENY must not read native checkout provenance")

    monkeypatch.setattr(tasks, "_task_worktree_source_sha", refuse_probe)
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        renewed = tasks.task_claim(
            item["id"], "Desktop_Agent2", expected_run_id=first["current_run_id"]
        )
    finally:
        config.workspace_root_var.reset(token)
    assert renewed["current_run_id"] == first["current_run_id"]
    assert renewed["worktree_path"] == first["worktree_path"]
    assert renewed["worktree_source_sha"] is None
    assert renewed["provisioning"]["reason"] == "workspace_denied"


def test_provision_authority_error_is_not_downgraded_to_native_success(repo, monkeypatch):
    item = tasks.task_add("Authority error")

    def fail(*args, **kwargs):
        raise ToolError("project binding denied")

    monkeypatch.setattr(tasks, "_ensure_task_worktree", fail)
    with pytest.raises(ToolError, match="project binding denied"):
        tasks.task_claim(item["id"], "Desktop_Agent2")


def test_existing_checkout_provenance_io_failure_is_explicit(repo, monkeypatch):
    item = tasks.task_add("Provenance unavailable")

    def fail(*args, **kwargs):
        raise OSError("checkout provenance inaccessible")

    monkeypatch.setattr(tasks, "_task_worktree_source_sha", fail)
    result = tasks.task_claim(item["id"], "Desktop_Agent2")
    assert result["worktree_path"]
    assert result["worktree_source_sha"] is None
    assert result["provisioning"] == {"status": "ready", "reason": "provenance_unavailable"}


def test_denied_takeover_preserves_old_checkout_and_never_adopts_it(repo):
    item = tasks.task_add("Preserve abandoned checkout")
    first = tasks.task_claim(item["id"], "Desktop_agent3")
    old = repo / first["worktree_path"]
    (old / "old-only.txt").write_text("Prior output")
    conn = storage.connect()
    conn.execute(
        "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (item["id"],)
    )
    conn.commit()
    conn.close()
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        successor = tasks.task_claim(item["id"], "Desktop_Agent2")
    finally:
        config.workspace_root_var.reset(token)
    assert successor["current_run_id"] != first["current_run_id"]
    assert successor["worktree_path"] is None and successor["branch"] is None
    assert (old / "old-only.txt").read_text() == "Prior output"
    assert successor["provisioning"]["reason"] == "workspace_denied"


def test_denied_workspace_fast_path_still_refuses_postcommit_cancellation(repo, monkeypatch):
    item = tasks.task_add("Cancel at final admission")
    original = tasks.run_records.native_execution_status
    calls = []

    def cancel_at_read(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            conn = storage.connect()
            conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (item["id"],))
            conn.commit()
            conn.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(tasks.run_records, "native_execution_status", cancel_at_read)
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        with pytest.raises(ToolError, match="run_not_current"):
            tasks.task_claim(item["id"], "Desktop_Agent2")
    finally:
        config.workspace_root_var.reset(token)


def test_real_git_provisioner_policy_error_remains_an_error(repo, monkeypatch):
    item = tasks.task_add("Real git authority")
    monkeypatch.setattr(tasks, "_is_project_worktree_path", lambda dest, repo: False)
    with pytest.raises(ToolError, match="is not under"):
        tasks.task_claim(item["id"], "Desktop_Agent2")


def test_real_project_binding_refusal_remains_an_error(repo, monkeypatch):
    from unified_agent import project_bindings

    item = tasks.task_add("Binding required", notes="[code-task]")
    monkeypatch.setattr(tasks.config, "project_repo_path", lambda project: repo)

    def deny(*args, **kwargs):
        raise ToolError("project binding mismatch")

    monkeypatch.setattr(project_bindings, "check_worktree_binding", deny)
    with pytest.raises(ToolError, match="project binding mismatch"):
        tasks.task_claim(item["id"], "Desktop_Agent2")


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_real_checkout_provenance_failure_is_reported(repo, monkeypatch, failure):
    item = tasks.task_add("Actual provenance failure", notes="[code-task]")
    monkeypatch.setattr(tasks.config, "project_repo_path", lambda project: repo)
    first = tasks.task_claim(item["id"], "Desktop_Agent2")
    assert first["worktree_source_sha"]
    original = subprocess.run

    def fail_probe(args, *a, **kw):
        if args[:2] == ["git", "-C"] and args[3:5] == ["symbolic-ref", "--quiet"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            return subprocess.CompletedProcess(args, 1, "", "probe unavailable")
        return original(args, *a, **kw)

    monkeypatch.setattr(tasks.subprocess, "run", fail_probe)
    result = tasks.task_claim(item["id"], "Desktop_Agent2", expected_run_id=first["current_run_id"])
    assert result["worktree_path"] == first["worktree_path"]
    assert result["worktree_source_sha"] is None
    assert result["provisioning"] == {"status": "ready", "reason": "provenance_unavailable"}
