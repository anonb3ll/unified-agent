"""GateGuard-style deterministic command deny (task #834).

Seam: ``safety.vet_command`` is the single chokepoint for ``run_cmd``,
``run_cmd_check``, and workflow steps that call ``run_cmd``. Approvals must not
override this layer.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.safety import vet_command
from unified_agent.tools import workflow as wf
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
)
from unified_agent.tools.shell import run_cmd, run_cmd_check
from unified_agent.tools.workflow import workflow_run


def _codes(out: dict) -> set[str]:
    return {f["code"] for f in out["findings"]}


def _assert_denied(command: str) -> None:
    with pytest.raises(ToolError, match="destructive command denied"):
        vet_command(command)


def _assert_allowed(command: str) -> None:
    argv = vet_command(command)
    assert argv[0]


class TestDestructiveRm:
    def test_default_allowlist_still_rejects_rm_as_unknown_program(self):
        with pytest.raises(ToolError, match="not in the allowlist"):
            vet_command("rm -rf tmp")

    def test_allowlisted_rm_rf_is_still_denied(self, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "rm:find:git:echo")
        _assert_denied("rm -rf tmp")

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf tmp",
            "rm -fr tmp",
            "rm -Rf tmp",
            "rm -r -f tmp",
            "rm -f -r tmp",
            "rm --recursive --force tmp",
            "rm --force --recursive tmp",
            "  rm   -rf   ./build  ",
        ],
    )
    def test_recursive_force_variants(self, monkeypatch, command):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "rm:find:git:echo")
        _assert_denied(command)

    def test_path_prefixed_rm_on_allowlist_still_denied(self, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "/bin/rm:rm")
        _assert_denied("/bin/rm -rf tmp")

    def test_non_recursive_rm_is_not_this_gate(self, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "rm:echo")
        assert vet_command("rm file.txt") == ["rm", "file.txt"]
        assert vet_command("rm -f file.txt") == ["rm", "-f", "file.txt"]


class TestDestructiveGit:
    @pytest.mark.parametrize(
        "command",
        [
            "git checkout .",
            "git checkout ./",
            "git checkout -- src/file.py",
            "git checkout --force main",
            "git checkout -f main",
            "git checkout -qf main",
            "git reset --hard",
            "git reset --hard HEAD~1",
            "git reset HEAD --hard",
            "git restore .",
            "git restore --worktree .",
            "git restore -- src/file.py",
            "git restore --force src/file.py",
            "git switch -f main",
            "git switch --force main",
            "git switch --discard-changes main",
            "git clean -fd",
            "git clean --force",
            "git rm -r src/old",
            "git -C sub reset --hard",
            "git --git-dir=.git checkout --force main",
            "  git   checkout   .  ",
        ],
    )
    def test_force_and_path_data_loss_denied(self, command):
        _assert_denied(command)

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git log --oneline",
            "git checkout -b feature",
            "git checkout main",
            "git switch main",
            "git reset --soft HEAD",
            "git reset --mixed HEAD",
            "git restore --staged src/file.py",
            "git add src/file.py",
            "git commit -m ok",
            "git push origin main",
            "git fetch origin",
            "git commit --amend --no-edit",
            "git push --force origin feature",
        ],
    )
    def test_non_destructive_git_allowed_by_this_gate(self, command):
        _assert_allowed(command)


class TestDestructiveFind:
    @pytest.mark.parametrize(
        "command",
        [
            "find . -name junk -delete",
            "find . -delete",
            "find . -exec rm {} ;",
            "find . -exec rm {} +",
            "find . -exec rm -rf {} ;",
            "find . -execdir rm {} ;",
            "find . -ok rm {} ;",
            "find . -okdir unlink {} ;",
            "find . -exec rmdir {} ;",
            "find . -exec git reset --hard {} ;",
            "find ./src -exec /bin/rm {} ;",
        ],
    )
    def test_destructive_find_denied(self, command):
        _assert_denied(command)

    def test_read_only_find_allowed(self):
        _assert_allowed("find . -name '*.py'")

    def test_non_destructive_find_exec_is_not_hard_denied(self):
        argv = vet_command("find . -exec grep foo {} ;")
        assert argv[0] == "find"
        out = run_cmd_check("find . -exec grep foo {} ;")
        assert out["verdict"] == "warn"
        assert "allowlist-bypass" in _codes(out)
        assert "destructive" not in _codes(out)


class TestRunCmdAndCheck:
    def test_run_cmd_check_blocks_with_destructive_code(self, isolated_env):
        out = run_cmd_check("git checkout .")
        assert out["ok"] is False
        assert out["verdict"] == "block"
        assert "destructive" in _codes(out)

    def test_run_cmd_raises_before_execution(self, isolated_env):
        with pytest.raises(ToolError, match="destructive command denied"):
            run_cmd("git checkout .")

    def test_find_delete_is_block_not_warn(self, isolated_env):
        out = run_cmd_check("find . -name junk -delete")
        assert out["ok"] is False
        assert out["verdict"] == "block"
        assert "destructive" in _codes(out)

    def test_legitimate_commands_still_run(self, isolated_env):
        out = run_cmd("echo hi")
        assert out["exit_code"] == 0
        assert out["stdout"].strip() == "hi"
        assert run_cmd_check("ls -la")["verdict"] == "ok"
        assert run_cmd_check("git status")["verdict"] == "ok"


class TestApprovalCannotBypass:
    def test_destructive_find_never_reaches_the_queue(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        with pytest.raises(ToolError, match="destructive command denied"):
            run_cmd("find . -name junk -delete")
        assert approval_pending()["pending"] == []

    def test_destructive_git_never_queues_even_when_run_cmd_is_gated(
        self, isolated_env, monkeypatch
    ):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ToolError, match="destructive command denied"):
            run_cmd("git reset --hard")
        assert approval_pending()["pending"] == []

    def test_approval_of_echo_does_not_authorize_destructive_git(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            run_cmd("echo gated-ok")
        rid = approval_pending()["pending"][0]["id"]
        approval_resolve(rid, "approve", resolved_by="Operator")
        assert run_cmd("echo gated-ok")["exit_code"] == 0
        with pytest.raises(ToolError, match="destructive command denied"):
            run_cmd("git checkout .")


class TestWorkflowUsesSameChokepoint:
    def test_workflow_run_cmd_is_denied(self, tmp_path, monkeypatch, isolated_env):
        d = tmp_path / "workflows"
        d.mkdir()
        monkeypatch.setattr(wf, "_workflows_dir", lambda: d)
        (d / "lossy.yaml").write_text(
            "name: lossy\nsteps:\n  - {name: s1, tool: run_cmd, args: {command: 'git checkout .'}}\n",
            encoding="utf-8",
        )
        r = workflow_run("lossy")
        # A gate refusal before execution is deterministic, so the workflow
        # stops as "denied" rather than a retryable "failed" (73064b41).
        assert r["status"] == "denied"
        assert r["steps"][0]["status"] == "denied"
        assert "destructive command denied" in r["steps"][0]["error"]
