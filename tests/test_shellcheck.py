"""Tests for run_cmd_check dry-run vetting and run_cmd advisory warnings."""

import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools import shell as shell_mod
from unified_agent.tools.approvals import ApprovalPendingError, approval_pending
from unified_agent.tools.shell import run_cmd, run_cmd_check

_FORCED = {"path-escape", "allowlist-bypass", "arbitrary-code"}


def codes(out):
    return {f["code"] for f in out["findings"]}


class TestRunCmdCheck:
    def test_clean_command_ok(self, isolated_env):
        out = run_cmd_check("ls -la")
        assert out["ok"] is True
        assert out["verdict"] == "ok"
        assert out["mutates"] == "no"
        assert out["findings"] == []
        assert out["program_found"] is True

    def test_denied_program_blocks(self, isolated_env):
        out = run_cmd_check("rm -rf /")
        assert out["ok"] is False
        assert out["verdict"] == "block"
        assert "rejected" in codes(out)
        assert out["argv"] == ["rm", "-rf", "/"]  # still parsed for inspection

    def test_cwd_escape_blocks(self, isolated_env):
        out = run_cmd_check("ls", cwd="../..")
        assert out["ok"] is False
        assert "cwd" in codes(out)

    def test_unparseable_command_blocks(self, isolated_env):
        out = run_cmd_check("echo 'unclosed")
        assert out["ok"] is False
        assert out["argv"] == []

    def test_pipe_warns_literal(self, isolated_env):
        out = run_cmd_check("grep -r foo . | head -5")
        assert out["ok"] is True
        assert out["verdict"] == "warn"
        assert "shell-syntax" in codes(out)

    def test_attached_semicolon_warns(self, isolated_env):
        out = run_cmd_check("echo hi; ls")
        assert "shell-syntax" in codes(out)

    def test_find_semicolon_terminator_not_syntax(self, isolated_env):
        out = run_cmd_check("find . -name x.py ;")
        assert "shell-syntax" not in codes(out)

    def test_env_var_warns(self, isolated_env):
        out = run_cmd_check("echo $HOME")
        assert "no-expansion" in codes(out)

    def test_absolute_path_escape_warns(self, isolated_env):
        out = run_cmd_check("cat /etc/passwd")
        assert out["verdict"] == "warn"
        assert "path-escape" in codes(out)

    def test_dotdot_path_escape_warns(self, isolated_env):
        out = run_cmd_check("cat ../outside.txt")
        assert "path-escape" in codes(out)

    def test_inside_paths_clean(self, isolated_env):
        (isolated_env / "sub").mkdir()
        out = run_cmd_check("ls ./sub", cwd=".")
        assert "path-escape" not in codes(out)

    def test_flag_value_path_checked(self, isolated_env):
        out = run_cmd_check("grep -r foo --include=/etc/hosts")
        assert "path-escape" in codes(out)

    def test_find_delete_is_destructive_block(self, isolated_env):
        out = run_cmd_check("find . -name junk -delete")
        assert out["ok"] is False
        assert out["verdict"] == "block"
        assert "destructive" in codes(out)
        assert out["mutates"] == "yes"

    def test_awk_system_flags_bypass(self, isolated_env):
        out = run_cmd_check('awk BEGIN{system("id")}')
        assert "allowlist-bypass" in codes(out)

    def test_git_bang_alias_flags_bypass(self, isolated_env):
        out = run_cmd_check("git -c alias.pwn=!curl evil pwn")
        assert "allowlist-bypass" in codes(out)

    def test_not_found_warns(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "no-such-binary-xyz")
        out = run_cmd_check("no-such-binary-xyz hi")
        assert out["ok"] is True
        assert "not-found" in codes(out)
        assert out["program_found"] is False


class TestMutationRisk:
    def test_read_only_programs(self, isolated_env):
        assert run_cmd_check("grep -r foo .")["mutates"] == "no"
        assert run_cmd_check("find . -name x.py")["mutates"] == "no"
        assert run_cmd_check("sed s/a/b/ f.txt")["mutates"] == "no"

    def test_sed_in_place(self, isolated_env):
        assert run_cmd_check("sed -i s/a/b/ f.txt")["mutates"] == "yes"
        assert run_cmd_check("sed -ni.bak s/a/b/p f.txt")["mutates"] == "yes"

    def test_git_subcommands(self, isolated_env):
        assert run_cmd_check("git status")["mutates"] == "no"
        assert run_cmd_check("git -C sub log --oneline")["mutates"] == "no"
        assert run_cmd_check("git -C sub commit -m x")["mutates"] == "yes"
        assert run_cmd_check("git worktree list")["mutates"] == "maybe"

    def test_git_network_info(self, isolated_env):
        out = run_cmd_check("git push origin main")
        assert "network" in codes(out)
        finding = next(f for f in out["findings"] if f["code"] == "network")
        assert finding["severity"] == "warn"
        assert out["verdict"] == "warn"
        assert out["mutates"] == "yes"

    def test_git_fetch_network_warn_only(self, isolated_env):
        out = run_cmd_check("git fetch origin")
        assert "network" in codes(out)
        finding = next(f for f in out["findings"] if f["code"] == "network")
        assert finding["severity"] == "warn"
        assert out["verdict"] == "warn"

    def test_ruff(self, isolated_env):
        assert run_cmd_check("ruff check src")["mutates"] == "no"
        assert run_cmd_check("ruff check --fix src")["mutates"] == "yes"
        assert run_cmd_check("ruff format --check src")["mutates"] == "no"
        assert run_cmd_check("ruff format src")["mutates"] == "yes"

    def test_arbitrary_code_is_maybe(self, isolated_env):
        out = run_cmd_check("python3 -c print(1)")
        assert out["mutates"] == "maybe"
        assert out["verdict"] == "warn"
        assert "arbitrary-code" in codes(out)
        finding = next(f for f in out["findings"] if f["code"] == "arbitrary-code")
        assert finding["severity"] == "warn"


class TestRunCmdCodeExecutionApproval:
    """Allowlisted interpreters and git/sed/awk exec forms force approval.

    UA_APPROVAL_TOOLS stays unset. The three audit PoCs must not run `id`.
    """

    def _forbid_exec(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("run_cmd executed the command")

        monkeypatch.setattr(shell_mod.subprocess, "run", _boom)

    def _assert_parked(self, command: str, code: str):
        with pytest.raises(ApprovalPendingError):
            run_cmd(command)
        pending = approval_pending()["pending"]
        assert pending, command
        codes_pending = pending[-1]["payload"].get("advisory_codes") or []
        assert codes_pending, command
        assert code in codes_pending
        assert code in codes(run_cmd_check(command))

    def test_python_script_poc_does_not_execute_id(self, isolated_env, monkeypatch):
        self._forbid_exec(monkeypatch)
        (isolated_env / "x.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "Path('ran-id').write_text(os.popen('id').read())\n",
            encoding="utf-8",
        )
        self._assert_parked("python3 x.py", "arbitrary-code")
        assert not (isolated_env / "ran-id").exists()

    def test_sed_e_poc_does_not_execute_id(self, isolated_env, monkeypatch):
        self._forbid_exec(monkeypatch)
        (isolated_env / "f.txt").write_text("line\n", encoding="utf-8")
        self._assert_parked("sed -n '1e id' f.txt", "allowlist-bypass")

    def test_git_alias_poc_does_not_execute_id(self, isolated_env, monkeypatch):
        self._forbid_exec(monkeypatch)
        self._assert_parked("git -c ALIAS.z=!id z", "allowlist-bypass")

    def test_legitimate_git_config_is_not_forced(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("git -c advice.detachedHead=false status")
        assert isinstance(res["exit_code"], int)
        assert approval_pending()["pending"] == []
        out = run_cmd_check("git -c advice.detachedHead=false status")
        assert codes(out).isdisjoint(_FORCED)

    def test_python_help_and_version_are_not_forced(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        for command in ("python3 --version", "python3 -V", "python3 -h", "python3 --help"):
            res = run_cmd(command)
            assert res["exit_code"] == 0, command
            assert codes(run_cmd_check(command)).isdisjoint(_FORCED)
        assert approval_pending()["pending"] == []

    def test_python_module_and_extra_args_are_arbitrary_code(self, isolated_env):
        assert "arbitrary-code" in codes(run_cmd_check("python3 -m json.tool"))
        assert "arbitrary-code" in codes(run_cmd_check("python3 -c print(1)"))
        assert "arbitrary-code" in codes(run_cmd_check("python3 --version x.py"))
        assert "arbitrary-code" in codes(run_cmd_check("python3"))

    def test_python_name_is_flagged_when_allowlisted(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "python:echo")
        self._forbid_exec(monkeypatch)
        self._assert_parked("python -m pip", "arbitrary-code")

    def test_uv_and_pytest_are_arbitrary_code(self, isolated_env, monkeypatch):
        self._forbid_exec(monkeypatch)
        self._assert_parked("uv run pytest", "arbitrary-code")
        self._assert_parked("pytest -q", "arbitrary-code")

    def test_awk_program_and_file_bypass_without_system_literal(self, isolated_env):
        assert "allowlist-bypass" in codes(run_cmd_check("awk -f prog.awk"))
        assert "allowlist-bypass" in codes(run_cmd_check("awk '{print 1}'"))
        assert "allowlist-bypass" in codes(run_cmd_check('awk BEGIN{system("id")}'))
        assert "allowlist-bypass" not in codes(run_cmd_check("awk --version"))

    @pytest.mark.parametrize(
        "command",
        [
            "sed -n '1e id' f.txt",
            "sed -e '1e id'",
            "sed -e 'w out.txt'",
            "sed -e 'W out.txt'",
            "sed -e 's/a/b/e'",
            "sed -e 's/a/b/w out.txt'",
        ],
    )
    def test_sed_exec_and_write_commands(self, isolated_env, command):
        assert "allowlist-bypass" in codes(run_cmd_check(command))

    @pytest.mark.parametrize(
        "command",
        [
            "sed s/a/b/ f.txt",
            "sed -e 's/e/w/'",
            "sed -e 'y/ew/we/'",
            "sed -e '/^e/p'",
            "sed -i s/a/b/ f.txt",
            "sed -f script.sed f.txt",
            "sed -f 'weekly rules' input.txt",
            "sed -f '1e id'",
            "sed --file=write.sed input.txt",
        ],
    )
    def test_sed_without_exec_or_write_is_not_forced(self, isolated_env, command):
        assert "allowlist-bypass" not in codes(run_cmd_check(command))

    def test_sed_f_inspects_workspace_script_contents(self, isolated_env, monkeypatch):
        (isolated_env / "weekly rules").write_text("p\n", encoding="utf-8")
        (isolated_env / "safe.sed").write_text("s/a/b/\n", encoding="utf-8")
        (isolated_env / "evil.sed").write_text("1e id\n", encoding="utf-8")
        (isolated_env / "write.sed").write_text("w out.txt\n", encoding="utf-8")
        assert "allowlist-bypass" not in codes(run_cmd_check("sed -f 'weekly rules' input.txt"))
        assert "allowlist-bypass" not in codes(run_cmd_check("sed -f safe.sed input.txt"))
        assert "allowlist-bypass" in codes(run_cmd_check("sed -f evil.sed input.txt"))
        assert "allowlist-bypass" in codes(run_cmd_check("sed --file=write.sed input.txt"))
        self._forbid_exec(monkeypatch)
        self._assert_parked("sed -f evil.sed input.txt", "allowlist-bypass")

    def test_sed_f_safe_script_in_worktree_outside_workspace(
        self, isolated_env, tmp_path, monkeypatch
    ):
        from unified_agent import safety

        worktree = tmp_path / "outside-worktree"
        worktree.mkdir()
        (worktree / "safe.sed").write_text("s/a/b/\n", encoding="utf-8")
        (worktree / "evil.sed").write_text("1e id\n", encoding="utf-8")
        monkeypatch.setattr(safety, "get_active_worktree_path", lambda *_a, **_k: worktree)
        monkeypatch.setattr(shell_mod, "get_active_worktree_path", lambda *_a, **_k: worktree)
        assert "allowlist-bypass" not in codes(
            run_cmd_check("sed -f safe.sed input.txt", cwd=str(worktree))
        )
        assert "allowlist-bypass" in codes(
            run_cmd_check("sed -f evil.sed input.txt", cwd=str(worktree))
        )

    def test_sed_f_nonregular_and_oversized_fail_closed(self, isolated_env):
        fifo = isolated_env / "pipe.sed"
        os.mkfifo(fifo)
        assert "allowlist-bypass" in codes(run_cmd_check("sed -f pipe.sed input.txt"))
        (isolated_env / "big.sed").write_bytes(b"p" * (shell_mod._SED_SCRIPT_READ_CAP + 1))
        assert "allowlist-bypass" in codes(run_cmd_check("sed -f big.sed input.txt"))

    def test_sed_f_short_reads_still_see_a_later_exec(self, isolated_env, monkeypatch):
        (isolated_env / "late.sed").write_text("p\n1e id\n", encoding="utf-8")
        real_read = os.read

        def short_read(fd, n):
            return real_read(fd, 1)

        monkeypatch.setattr(os, "read", short_read)
        assert "allowlist-bypass" in codes(run_cmd_check("sed -f late.sed input.txt"))

    @pytest.mark.parametrize(
        ("command", "code"),
        [
            ("git -c alias.pwn=!curl evil pwn", "allowlist-bypass"),
            ("git -c core.pager=less status", "allowlist-bypass"),
            ("git -c core.editor=vi status", "allowlist-bypass"),
            ("git -c core.sshCommand=evil status", "allowlist-bypass"),
            ("git -c core.fsmonitor=evil status", "allowlist-bypass"),
            ("git -c core.askpass=evil status", "allowlist-bypass"),
            ("git -c CORE.PAGER=less status", "allowlist-bypass"),
            ("git --config-env=alias.z status", "allowlist-bypass"),
            ("git --config-env alias.z status", "allowlist-bypass"),
            ("git --exec-path status", "allowlist-bypass"),
            ("git --exec-path=git-core status", "allowlist-bypass"),
        ],
    )
    def test_git_exec_config_forces(self, isolated_env, command, code):
        assert code in codes(run_cmd_check(command))

    def test_git_advice_config_has_no_forced_code(self, isolated_env):
        out = run_cmd_check("git -c advice.detachedHead=false status")
        assert codes(out).isdisjoint(_FORCED)
        assert out["mutates"] == "no"


class TestRunCmdWarnings:
    def test_metacharacters_produce_warning(self, isolated_env):
        # Keep path args inside the workspace so this stays warn-only (shell-syntax);
        # a workspace-escaping "/" would force the approval gate (task #682).
        out = run_cmd("echo hi; ls")
        assert out["exit_code"] == 0
        assert out["warnings"]  # attached ';' is advisory-flagged

    def test_clean_command_no_warnings(self, isolated_env):
        out = run_cmd("echo hi")
        assert out["exit_code"] == 0
        assert out["warnings"] == []


class TestExecBypassVectors:
    """Task #1904: every argv here runs a program or writes a hook, and must force.

    Each one returned verdict "ok" with no findings before the fix.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # git config writes persist an alias/pager/hook path for a later call.
            "git config alias.x !id",
            "git config core.pager touch",
            "git config --local core.hooksPath h",
            "git config --add include.path evil.cfg",
            "git config set core.editor vi",
            "git config --file cfg alias.x !id",
            # -c keys outside the safe allowlist.
            "git -c include.path=evil.cfg status",
            "git -c core.hooksPath=h commit -m x",
            "git -c diff.external=evil diff",
            "git -c diff.x.textconv=evil log -p",
            "git -c gpg.program=evil log --show-signature",
            "git -cinclude.path=evil.cfg status",
            # Subcommands and flags that execute a supplied command.
            "git grep --open-files-in-pager=touch x",
            "git grep -Otouch x",
            "git grep -O touch x",
            "git rebase -x id HEAD~1",
            "git rebase --exec=id HEAD~1",
            "git bisect run id",
            "git submodule foreach id",
            "git difftool -x id",
            "git mergetool",
            "git filter-branch --tree-filter id",
            "git ls-remote --upload-pack=touch .",
            "git fetch --upload-pack touch origin",
            "git clone -u touch src dst",
            "git push --receive-pack=touch origin",
            "git archive --remote=x --exec=touch HEAD",
            "git clone --template=tpl src dst",
            # sort runs its compressor; sort -o can overwrite an executable hook.
            "sort -S 64K --compress-program=python3 f",
            "sort --compress-program python3 f",
            # Writes into hook directories that fs_write refuses.
            "find . -fprint .git/hooks/pre-commit",
            "find . -fprintf .husky/pre-push %p",
            "sort -o .git/hooks/pre-commit f",
            "sort --output=.git/hooks/pre-commit f",
        ],
    )
    def test_exec_vectors_force(self, isolated_env, command):
        out = run_cmd_check(command)
        assert "allowlist-bypass" in codes(out), (command, out["findings"])

    def test_attached_short_flag_path_escape(self, isolated_env):
        assert "path-escape" in codes(run_cmd_check("git -C/etc log"))

    def test_git_config_alias_poc_does_not_execute(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("run_cmd executed the command")

        monkeypatch.setattr(shell_mod.subprocess, "run", _boom)
        with pytest.raises(ApprovalPendingError):
            run_cmd("git config alias.x !id")

    def test_sort_output_is_mutating(self, isolated_env):
        assert run_cmd_check("sort f")["mutates"] == "no"
        assert run_cmd_check("sort -o out.txt f")["mutates"] == "yes"
        assert run_cmd_check("sort --output=out.txt f")["mutates"] == "yes"

    @pytest.mark.parametrize(
        "command",
        [
            "git config user.name",
            "git config --get user.email",
            "git config --list",
            "git config -l --show-origin",
            "git config get user.name",
            "git config --get-regexp ^alias",
            "git -c user.name=X -c user.email=x@y commit -m x",
            "git -c color.ui=never log --oneline",
            "git -c core.quotepath=off status",
            "git grep -n foo",
            "git rebase origin/main",
            "git cherry-pick -x abc123",
            "git fetch origin",
            "git submodule status",
            "git bisect start",
            "sort -u f",
            "find . -name '*.py' -print",
        ],
    )
    def test_routine_commands_are_not_forced(self, isolated_env, command):
        assert codes(run_cmd_check(command)).isdisjoint(_FORCED), command

    @pytest.mark.parametrize(
        "command",
        [
            "git fetch -u origin",
            "git rebase -X theirs origin/main",
            "cat .git/hooks/pre-commit",
            "ls .git/hooks",
            "grep -r x .husky",
        ],
    )
    def test_near_misses_are_not_forced(self, isolated_env, command):
        assert codes(run_cmd_check(command)).isdisjoint(_FORCED), command


class TestExecBypassReviewFollowups:
    """PR #1046 review: abbreviations, --output, attached paths, read-only config."""

    @pytest.mark.parametrize(
        "command",
        [
            "sort -S 1K --compress-prog=./evil input.txt",
            "sort --compress=python3 f",
            "git grep --open-files=touch x",
            "git rebase --exe=id HEAD~1",
            "git ls-remote --upload=touch .",
            "git show --output=.git/hooks/pre-commit",
            "git log --outp=.git/hooks/pre-commit",
            "git diff --output .git/hooks/pre-commit",
            "sort -osub/../.git/hooks/pre-commit input.txt",
            "sort --out=.git/hooks/pre-commit f",
            "sort -o .git/hooks/pre-commit f",
            "sort --output .git/hooks/pre-commit f",
            "sort -uo.git/hooks/pre-commit f",
            "sort --o=.git/hooks/pre-commit f",
            "git ls-remote --u=touch .",
        ],
    )
    def test_review_vectors_force(self, isolated_env, command):
        (isolated_env / "sub").mkdir(exist_ok=True)
        assert "allowlist-bypass" in codes(run_cmd_check(command)), command

    @pytest.mark.parametrize(
        "command",
        [
            "git config --get-colorbool color.ui false",
            "git config --get-color color.diff.old red",
            "git commit -m .git/hooks/pre-commit",
            "git commit --message=.git/hooks/pre-commit",
            "sed -e/x/d f.txt",
            "grep -e/foo/ f.txt",
            # sort reads its operands; only -o/--output is a write destination.
            "sort .git/hooks/pre-commit",
            "sort -o out.txt .git/hooks/pre-commit",
            "sort -t o.git/hooks/pre-commit f",
            "sort -to.git/hooks/pre-commit f",
            "sort --field-separator o.git/hooks/pre-commit f",
        ],
    )
    def test_review_false_positives_not_forced(self, isolated_env, command):
        assert codes(run_cmd_check(command)).isdisjoint(_FORCED), command

    def test_git_output_is_mutating(self, isolated_env):
        assert run_cmd_check("git show --output=out.txt HEAD")["mutates"] == "yes"

    def test_clustered_sort_output_is_mutating(self, isolated_env):
        assert run_cmd_check("sort -uoout.txt f")["mutates"] == "yes"

    def test_sort_separator_is_not_output(self, isolated_env):
        assert run_cmd_check("sort -to f")["mutates"] == "no"

    def test_clustered_sort_output_escape(self, isolated_env):
        assert "path-escape" in codes(run_cmd_check("sort -uo/tmp/out f"))


@pytest.mark.parametrize("output_args", ["-o out", "-oout", "-uoout", "--output=out", "--o=out"])
def test_sort_bare_output_symlink_cannot_escape(isolated_env, tmp_path, output_args):
    workspace = shell_mod.config.workspace_root()
    (workspace / "out").symlink_to(tmp_path / "outside.txt")
    assert "path-escape" in codes(run_cmd_check(f"sort {output_args} input.txt"))


class TestGitConfigAndWriteBypasses:
    """Repo review 2026-11-07: clone --config, bare repos, aliases, uniq, secret reads."""

    @pytest.mark.parametrize(
        "command",
        [
            "git clone -q --config alias.pwn=!touch\\ x . dst",
            "git clone --config=alias.pwn=!id . dst",
            "git clone --conf core.pager=id . dst",
            "git --git-dir=evil pwn",
            "git --git-dir evil log",
            "git --work-tree=w status",
            # Not a built-in: runs alias.<name> from an agent-written config.
            "git -C dst pwn",
            "git pwn",
            # uniq writes its second operand.
            "uniq cfg.txt .git/config",
            "uniq hook.txt .git/hooks/pre-commit",
            "uniq -f 1 -c cfg.txt .git/config",
            # Any path inside .git is executable config, not only hooks/.
            "sort -o .git/config f",
        ],
    )
    def test_vectors_force(self, isolated_env, command):
        assert "allowlist-bypass" in codes(run_cmd_check(command)), command

    @pytest.mark.parametrize(
        "command",
        [
            "git rev-parse --git-dir",
            "git clone --config user.name=x src dst",
            "git stash list",
            "git worktree list",
            "git help status",
            "uniq in.txt out.txt",
            "uniq .git/config",
            "uniq -c -f 1 in.txt",
        ],
    )
    def test_routine_not_forced(self, isolated_env, command):
        assert codes(run_cmd_check(command)).isdisjoint(_FORCED), command

    def test_uniq_output_is_mutating_and_checked_for_escape(self, isolated_env):
        assert run_cmd_check("uniq in.txt")["mutates"] == "no"
        assert run_cmd_check("uniq in.txt out.txt")["mutates"] == "yes"
        assert "path-escape" in codes(run_cmd_check("uniq in.txt /tmp/out"))

    def test_agent_written_bare_repo_is_not_discovered(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        bare = isolated_env / "evil"
        for sub in ("objects", "refs"):
            (bare / sub).mkdir(parents=True)
        (bare / "HEAD").write_text("ref: refs/heads/main\n")
        (bare / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        res = run_cmd("git rev-parse --git-dir", cwd="evil")
        assert res["exit_code"] != 0
        assert "bare" in res["stderr"].lower()

    def test_secret_file_read_refused(self, isolated_env):
        name = "service" + ".env"
        (isolated_env / name).write_text("FAKE_TOKEN=abc\n")
        (isolated_env / "notes.txt").symlink_to(isolated_env / name)
        for command in (f"cat {name}", "head notes.txt", f"grep -n FAKE ./{name}"):
            out = run_cmd_check(command)
            assert out["verdict"] == "block" and "secret-path" in codes(out), command
            with pytest.raises(ToolError, match="secret-bearing"):
                run_cmd(command)
        # A pattern that merely looks like a secret name is not a path.
        (isolated_env / "f.txt").write_text("x\n")
        assert "secret-path" not in codes(run_cmd_check("grep token f.txt"))
