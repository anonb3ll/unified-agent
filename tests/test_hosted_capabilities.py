"""Hosted/container hub capability boundary (task #1442).

An empty ``UA_WORKSPACE=/data/workspace`` (or the test opt-in
``UA_HOSTED_WORKSPACE_GUARD=1``) must not look like a successful empty
listing. A missing vault and a missing notification bridge must raise
instead of returning an empty success. A populated workspace keeps the
single-tenant default.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools import files, notify
from unified_agent.tools.doctor import system_doctor
from unified_agent.tools.shell import run_cmd, run_cmd_check
from unified_agent.tools.vault import vault_list, vault_search, vault_wiki_custodian


def _guard(monkeypatch) -> None:
    monkeypatch.setenv("UA_HOSTED_WORKSPACE_GUARD", "1")


class TestEmptyWorkspaceFailsClosed:
    def test_fs_list_on_empty_workspace_is_success_without_guard(self, isolated_env):
        """Current default: an ordinary empty temp workspace still lists."""
        out = files.fs_list(".")
        assert out["entries"] == []
        assert out["truncated"] is False

    def test_fs_list_refuses_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        with pytest.raises(ToolError, match="workspace"):
            files.fs_list(".")

    def test_fs_search_refuses_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        with pytest.raises(ToolError, match="workspace"):
            files.fs_search("anything")

    def test_populated_workspace_still_lists(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        (isolated_env / "keep.txt").write_text("x")
        out = files.fs_list(".")
        assert [e["name"] for e in out["entries"]] == ["keep.txt"]

    def test_run_cmd_ls_refuses_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        with pytest.raises(ToolError, match="workspace"):
            run_cmd("ls")

    def test_run_cmd_echo_still_runs_on_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        out = run_cmd("echo hi")
        assert out["exit_code"] == 0
        assert out["stdout"].strip() == "hi"

    def test_git_init_bootstraps_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        out = run_cmd("git init")
        assert out["exit_code"] == 0
        assert (isolated_env / ".git").exists()

    def test_run_cmd_check_blocks_census_on_empty_hosted_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        out = run_cmd_check("ls")
        assert out["ok"] is False
        assert out["verdict"] == "block"
        assert "hosted-workspace" in {f["code"] for f in out["findings"]}

    def test_fs_list_allows_populated_project_checkout(self, isolated_env, monkeypatch, tmp_path):
        _guard(monkeypatch)
        dev = tmp_path / "dev"
        project = dev / "unified_agent"
        project.mkdir(parents=True)
        (project / "a.txt").write_text("x")
        monkeypatch.setenv("UA_DEV_ROOT", str(dev))
        out = files.fs_list(str(project))
        assert [e["name"] for e in out["entries"] if e["name"] == "a.txt"] == ["a.txt"]

    def test_ls_of_only_an_outside_path_runs(self, isolated_env, monkeypatch, tmp_path):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "there.txt").write_text("x")
        out = run_cmd_check(f"ls {outside}")
        assert "hosted-workspace" not in {f["code"] for f in out["findings"]}
        assert out["ok"] is True

    def test_grep_and_find_outside_are_not_a_workspace_census(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for command in (
            f"grep -r needle {outside}",
            f"grep -e needle {outside}",
            f"rg needle {outside}",
            f"rg --files {outside}",
            f"find {outside} -name keep.txt",
        ):
            out = run_cmd_check(command)
            assert "hosted-workspace" not in {f["code"] for f in out["findings"]}, command

    def test_ls_ignore_and_find_exec_outside_are_not_a_workspace_census(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for command in (
            f"ls -I foo {outside}",
            f"ls --ignore=foo {outside}",
            f"find {outside} -exec echo {{}} \\;",
            f"find -L {outside} -name keep.txt",
            f"ls --color {outside}",
        ):
            out = run_cmd_check(command)
            assert "hosted-workspace" not in {f["code"] for f in out["findings"]}, command

    def test_ls_ignore_without_a_path_still_censuses_the_empty_workspace(
        self, isolated_env, monkeypatch
    ):
        _guard(monkeypatch)
        with pytest.raises(ToolError, match="workspace"):
            run_cmd("ls -I foo")

    def test_optional_ls_color_does_not_hide_a_workspace_target(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        out = run_cmd_check(f"ls --color {isolated_env} {outside}")
        assert "hosted-workspace" in {f["code"] for f in out["findings"]}

    def test_find_double_dash_still_sees_the_workspace(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        out = run_cmd_check(f"find -- {isolated_env}")
        assert "hosted-workspace" in {f["code"] for f in out["findings"]}

    def test_grep_without_a_path_still_censuses_the_empty_workspace(
        self, isolated_env, monkeypatch
    ):
        _guard(monkeypatch)
        with pytest.raises(ToolError, match="workspace"):
            run_cmd("grep -r needle")

    def test_git_status_with_dash_c_outside_is_not_a_workspace_census(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for command in (
            f"git -C {outside} status",
            f"git --git-dir {outside}/.git status",
            f"git --work-tree {outside} status",
            f"git --git-dir={outside}/.git --work-tree={outside} status",
        ):
            out = run_cmd_check(command)
            assert "hosted-workspace" not in {f["code"] for f in out["findings"]}, command

    def test_git_status_in_the_empty_workspace_is_still_refused(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ToolError, match="workspace"):
            run_cmd("git status")
        with pytest.raises(ToolError, match="workspace"):
            run_cmd(f"git -C {outside} --work-tree {isolated_env} status")

    def test_ls_that_also_names_the_empty_workspace_is_refused(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ToolError, match="workspace"):
            run_cmd(f"ls {isolated_env} {outside}")

    def test_ls_in_populated_task_worktree_runs(self, isolated_env, monkeypatch, tmp_path):
        _guard(monkeypatch)
        worktree = tmp_path / "task-42"
        worktree.mkdir()
        (worktree / "keep.txt").write_text("x")
        monkeypatch.setattr(
            "unified_agent.safety.get_active_worktree_path",
            lambda *_args, **_kwargs: worktree,
        )
        out = run_cmd("ls")
        assert out["exit_code"] == 0
        assert "keep.txt" in out["stdout"]


class TestVaultAndNotifyFailClosed:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_custodian_refuses_empty_hosted_vault_without_writes(
        self, isolated_env, monkeypatch, tmp_path, dry_run
    ):
        _guard(monkeypatch)
        root = tmp_path / "empty-custodian-vault"
        root.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(root))

        with pytest.raises(ToolError, match="vault"):
            vault_wiki_custodian(dry_run=dry_run)

        assert list(root.iterdir()) == []

    def test_vault_discovery_does_not_grant_a_denied_developer_workspace(
        self, isolated_env, monkeypatch
    ):
        _guard(monkeypatch)
        root = isolated_env / "vault"
        wiki = root / "Wiki"
        wiki.mkdir(parents=True)
        (wiki / "note.md").write_text("# synthetic note\n")
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        identity = config.agent_identity_var.set("Desktop_Agent1")
        binding = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            assert [e["name"] for e in vault_list("Wiki")["entries"]] == ["note.md"]
            assert vault_search("synthetic", "Wiki")["matches"][0]["path"] == "Wiki/note.md"
            with pytest.raises(ToolError, match="filesystem access is denied"):
                files.fs_list(".")
            with pytest.raises(ToolError, match="filesystem access is denied"):
                run_cmd("echo hi")
        finally:
            config.workspace_root_var.reset(binding)
            config.agent_identity_var.reset(identity)

    def test_missing_vault_is_not_an_empty_list(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_VAULT_PATH", str(isolated_env / "no-vault"))
        with pytest.raises(ToolError, match="vault"):
            vault_list(".")

    def test_empty_vault_dir_refuses_under_guard(self, isolated_env, monkeypatch, tmp_path):
        _guard(monkeypatch)
        root = tmp_path / "empty-vault"
        root.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        with pytest.raises(ToolError, match="vault") as raised:
            vault_list(".")
        message = str(raised.value)
        assert "UA_VAULT_PATH" not in message
        assert "disabled" not in message.lower()

    def test_populated_vault_still_lists(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        root = isolated_env / "vault"
        inbox = root / "00 - Inbox"
        inbox.mkdir(parents=True)
        (inbox / "note.md").write_text("# hi\n")
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        names = [e["name"] for e in vault_list(".")["entries"]]
        assert "00 - Inbox" in names

    def test_populated_workspace_lists_an_empty_vault(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        (isolated_env / "keep.txt").write_text("x")
        root = isolated_env / "empty-vault"
        root.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        assert vault_list(".")["entries"] == []

    def test_disabled_vault_is_not_an_empty_listing(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        with pytest.raises(ToolError, match="disabled"):
            vault_list(".")

    def test_missing_notify_bridge_raises(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_BRIDGE_DIR", str(isolated_env / "no-bridge"))
        with pytest.raises(ToolError, match="send.ts not found"):
            notify.notify("hello")


class TestDoctorReportsDegradedFamilies:
    def test_doctor_names_missing_families(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        monkeypatch.setenv("UA_VAULT_PATH", str(isolated_env / "no-vault"))
        monkeypatch.setenv("UA_BRIDGE_DIR", str(isolated_env / "no-bridge"))
        checks = {c["capability"]: c for c in system_doctor()["checks"]}
        row = checks["hosted_local_dependencies"]
        assert row["status"] == "DEGRADED"
        for family in ("workspace", "vault", "notify"):
            assert family in row["detail"]

    def test_doctor_live_when_dependencies_present(self, isolated_env, monkeypatch):
        (isolated_env / "keep.txt").write_text("x")
        vault = isolated_env / "vault"
        (vault / "00 - Inbox").mkdir(parents=True)
        (vault / "00 - Inbox" / "note.md").write_text("# hi\n")
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))
        bridge = isolated_env / "bridge" / "src"
        bridge.mkdir(parents=True)
        (bridge / "send.ts").write_text("// send\n")
        monkeypatch.setenv("UA_BRIDGE_DIR", str(isolated_env / "bridge"))
        checks = {c["capability"]: c for c in system_doctor()["checks"]}
        assert checks["hosted_local_dependencies"]["status"] == "LIVE"

    def test_doctor_is_dormant_on_a_normal_empty_tree(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_VAULT_PATH", str(isolated_env / "no-vault"))
        monkeypatch.setenv("UA_BRIDGE_DIR", str(isolated_env / "no-bridge"))
        checks = {c["capability"]: c for c in system_doctor()["checks"]}
        row = checks["hosted_local_dependencies"]
        assert row["status"] == "DORMANT"
        assert "workspace" in row["detail"]

    def test_doctor_deny_identity_still_names_families(self, isolated_env, monkeypatch):
        _guard(monkeypatch)
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            checks = {c["capability"]: c for c in system_doctor()["checks"]}
        finally:
            config.workspace_root_var.reset(token)
        row = checks["hosted_local_dependencies"]
        assert row["status"] == "DEGRADED"
        assert "workspace" in row["detail"]

    def test_doctor_ignores_a_populated_agent_workspace_binding(
        self, isolated_env, monkeypatch, tmp_path
    ):
        _guard(monkeypatch)
        bound = tmp_path / "team-a"
        bound.mkdir()
        (bound / "a.txt").write_text("x")
        token = config.workspace_root_var.set(bound)
        try:
            checks = {c["capability"]: c for c in system_doctor()["checks"]}
        finally:
            config.workspace_root_var.reset(token)
        assert checks["hosted_local_dependencies"]["status"] == "DEGRADED"

    def test_doctor_disabled_vault_is_not_live(self, isolated_env, monkeypatch):
        (isolated_env / "keep.txt").write_text("x")
        monkeypatch.setenv("UA_VAULT_PATH", "")
        bridge = isolated_env / "bridge" / "src"
        bridge.mkdir(parents=True)
        (bridge / "send.ts").write_text("// send\n")
        monkeypatch.setenv("UA_BRIDGE_DIR", str(isolated_env / "bridge"))
        checks = {c["capability"]: c for c in system_doctor()["checks"]}
        row = checks["hosted_local_dependencies"]
        assert row["status"] != "LIVE"
        assert "vault (disabled)" in row["detail"]
        assert "vault," not in row["detail"]
