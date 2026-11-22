import os
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.safety import (
    check_fetch_host,
    project_read_roots,
    resolve_for_audit,
    resolve_for_project_read,
    resolve_in_workspace,
    vet_command,
)
from unified_agent.tools.files import fs_list, fs_read, fs_search, fs_write
from unified_agent.tools.shell import run_cmd


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",
        "100.100.100.100",
        "224.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
        "::ffff:224.0.0.1",
    ],
)
def test_fetch_refuses_non_public_ip_without_override(monkeypatch, address):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: False)
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host(address)
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host(address, strict=True)


def test_fetch_override_applies_only_to_non_strict_requests(monkeypatch):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
    assert check_fetch_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host("127.0.0.1", strict=True)
    assert check_fetch_host("93.184.216.34") == "93.184.216.34"
    assert check_fetch_host("93.184.216.34", strict=True) == "93.184.216.34"


# IPv6 forms that carry an IPv4 destination but that ``ipaddress`` does not
# unwrap via ``ipv4_mapped``. Python reports every one of these as
# ``is_global``, so judging the IPv6 wrapper alone lets them through.
EMBEDDED_IPV4_RESTRICTED = [
    # IPv4-translated, ::ffff:0:0:0/96 (RFC 2765)
    "::ffff:0:127.0.0.1",
    "::ffff:0:100.64.0.1",
    "::ffff:0:224.0.0.1",
    "::ffff:0:169.254.169.254",
    # IPv4-compatible, ::/96 (deprecated, RFC 4291)
    "::127.0.0.1",
    "::100.64.0.1",
    "::169.254.169.254",
    # Well-known NAT64 prefix, 64:ff9b::/96 (RFC 6052)
    "64:ff9b::127.0.0.1",
    "64:ff9b::100.64.0.1",
    "64:ff9b::224.0.0.1",
    "64:ff9b::169.254.169.254",
    "64:ff9b::10.0.0.1",
]

# Already refused on main because Python classifies the whole prefix as
# non-global. Pinned so a future predicate change cannot reopen them.
TUNNEL_PREFIX_RESTRICTED = [
    "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
    "2002:a9fe:a9fe::",  # 6to4 wrapping 169.254.169.254
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo
    "64:ff9b:1::a00:1",  # NAT64 local-use prefix (RFC 8215)
]


@pytest.mark.parametrize("address", EMBEDDED_IPV4_RESTRICTED + TUNNEL_PREFIX_RESTRICTED)
def test_fetch_refuses_ipv4_embedded_in_ipv6(monkeypatch, address):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: False)
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host(address)
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host(address, strict=True)


@pytest.mark.parametrize(
    "address",
    ["::ffff:93.184.216.34", "::ffff:0:93.184.216.34", "::8.8.8.8", "64:ff9b::93.184.216.34"],
)
def test_fetch_allows_public_ipv4_embedded_in_ipv6(monkeypatch, address):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: False)
    assert check_fetch_host(address) == address
    assert check_fetch_host(address, strict=True) == address


def test_embedded_ipv4_override_still_non_strict_only(monkeypatch):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
    assert check_fetch_host("64:ff9b::127.0.0.1") == "64:ff9b::127.0.0.1"
    with pytest.raises(ToolError, match="non-public address"):
        check_fetch_host("64:ff9b::127.0.0.1", strict=True)


@pytest.mark.parametrize(
    "restricted",
    ["100.64.0.1", "224.0.0.1", "::ffff:100.64.0.1", "::ffff:0:100.64.0.1", "64:ff9b::224.0.0.1"],
)
def test_fetch_refuses_restricted_dns_answer(monkeypatch, restricted):
    monkeypatch.setattr(config, "allow_local_fetch", lambda: False)
    monkeypatch.setattr(
        "unified_agent.safety.socket.getaddrinfo",
        lambda *_args: [
            (None, None, None, None, ("93.184.216.34", 0)),
            (None, None, None, None, (restricted, 0)),
        ],
    )
    with pytest.raises(ToolError, match="resolves to a non-public address"):
        check_fetch_host("example.test")
    with pytest.raises(ToolError, match="resolves to a non-public address"):
        check_fetch_host("example.test", strict=True)


class TestSandbox:
    def test_relative_path_inside(self, isolated_env):
        p = resolve_in_workspace("sub/file.txt")
        assert p == isolated_env / "sub" / "file.txt"

    def test_dotdot_escape_rejected(self, isolated_env):
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace("../outside.txt")

    def test_deep_dotdot_escape_rejected(self, isolated_env):
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace("a/b/../../../../etc/passwd")

    def test_absolute_path_outside_rejected(self, isolated_env):
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace("/etc/passwd")

    def test_absolute_path_inside_allowed(self, isolated_env):
        p = resolve_in_workspace(str(isolated_env / "ok.txt"))
        assert p == isolated_env / "ok.txt"

    def test_symlink_escape_rejected(self, isolated_env, tmp_path):
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        link = isolated_env / "link.txt"
        os.symlink(outside, link)
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace("link.txt")

    def test_workspace_root_itself_allowed(self, isolated_env):
        assert resolve_in_workspace(".") == isolated_env

    @pytest.mark.parametrize("hook_dir", [".git", ".hg", ".svn", ".husky"])
    def test_hook_dir_rejected(self, isolated_env, hook_dir):
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace(f"{hook_dir}/hooks/post-commit")

    def test_hook_dir_rejected_nested(self, isolated_env):
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace("sub/.git/config")

    @pytest.mark.parametrize("hook_dir", [".git", ".hg", ".svn", ".husky"])
    def test_bare_hook_dir_itself_rejected(self, isolated_env, hook_dir):
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace(hook_dir)

    def test_hook_dir_rejected_absolute_path(self, isolated_env):
        abs_path = str(isolated_env / ".git" / "config")
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace(abs_path)

    @pytest.mark.parametrize("cased", [".GIT", ".Git", ".HUSKY", ".Husky"])
    def test_hook_dir_rejected_case_insensitive(self, isolated_env, cased):
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace(f"{cased}/hooks/post-commit")

    def test_ordinary_dotfile_still_allowed(self, isolated_env):
        p = resolve_in_workspace(".gitignore")
        assert p == isolated_env / ".gitignore"

    @pytest.mark.parametrize("custom_hooks_dir", ["myhooks", ".hooks", "tools/githooks"])
    def test_arbitrary_core_hookspath_dir_is_denied(self, isolated_env, custom_hooks_dir):
        subprocess.run(["git", "init", "-q", str(isolated_env)], check=True)
        subprocess.run(
            ["git", "-C", str(isolated_env), "config", "core.hooksPath", custom_hooks_dir],
            check=True,
        )
        with pytest.raises(ToolError, match="configured Git hooks directory"):
            resolve_in_workspace(f"{custom_hooks_dir}/pre-commit", protect_configured_hooks=True)

    def test_core_hookspath_does_not_block_neighboring_paths(self, isolated_env):
        subprocess.run(["git", "init", "-q", str(isolated_env)], check=True)
        subprocess.run(
            ["git", "-C", str(isolated_env), "config", "core.hooksPath", "tools/githooks"],
            check=True,
        )
        assert (
            resolve_in_workspace("tools/build.py", protect_configured_hooks=True)
            == isolated_env / "tools" / "build.py"
        )


class TestActiveWorktreeContainment:
    """Task #995: since #994, a project-sourced task worktree lives outside

    base_root entirely (under its own product repo, not the cockpit). The
    sandbox for resolve_in_workspace is base_root UNION the active worktree,
    not base_root alone — these pin that union without widening it further.
    """

    @pytest.fixture
    def active_worktree(self, tmp_path, isolated_env, monkeypatch):
        import unified_agent.safety as safety_mod

        wt = tmp_path / "dev" / "unified_agent" / ".worktrees" / "task-995"
        wt.mkdir(parents=True)
        monkeypatch.setattr(safety_mod, "get_active_worktree_path", lambda *a, **k: wt)
        return wt

    def test_relative_path_resolves_inside_a_project_sourced_worktree(self, active_worktree):
        p = resolve_in_workspace("src/mod.py")
        assert p == active_worktree / "src" / "mod.py"

    def test_absolute_path_inside_the_worktree_allowed(self, active_worktree):
        target = active_worktree / "sub" / "file.txt"
        assert resolve_in_workspace(str(target)) == target

    def test_worktree_root_itself_allowed(self, active_worktree):
        assert resolve_in_workspace(".") == active_worktree

    def test_cockpit_paths_remain_reachable_while_worktree_is_active(
        self, active_worktree, isolated_env
    ):
        # base_root U wt_root, not wt_root replacing base_root.
        target = isolated_env / "cockpit-file.txt"
        assert resolve_in_workspace(str(target)) == target

    def test_absolute_escape_outside_both_roots_still_rejected(self, active_worktree, tmp_path):
        outside = tmp_path / "elsewhere" / "secret.txt"
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace(str(outside))

    def test_relative_dotdot_escape_still_rejected(self, active_worktree):
        # Four levels up from task-995 lands exactly on tmp_path, which
        # contains neither root — same "outside both" case, via a relative path.
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace("../../../../etc/passwd")

    @pytest.mark.parametrize("hook_dir", [".git", ".hg", ".svn", ".husky"])
    def test_hook_dir_inside_the_worktree_rejected(self, active_worktree, hook_dir):
        # Regression guard: before the fix, the hook-dir check computed `rel`
        # via resolved.relative_to(base_root), which raises an uncaught
        # ValueError (not ToolError) once resolved is only under wt_root.
        with pytest.raises(ToolError, match="hook-execution directory"):
            resolve_in_workspace(f"{hook_dir}/hooks/post-commit")

    def test_ordinary_file_named_like_a_dotfile_still_allowed(self, active_worktree):
        p = resolve_in_workspace(".gitignore")
        assert p == active_worktree / ".gitignore"


class TestConfiguredHooksInProjectSourcedWorktree:
    """Task #996: protect_configured_hooks must extend to a project-sourced

    worktree, not just base_root. _repository_root previously always walked
    up bounded by base_root, so a resolved path inside a worktree living
    outside base_root (task #994) never satisfied the loop's containment
    check and the function returned None immediately -- silently skipping
    the custom core.hooksPath check for every write into such a worktree.
    """

    @pytest.fixture
    def project_worktree(self, tmp_path, isolated_env, monkeypatch):
        import unified_agent.safety as safety_mod

        wt = tmp_path / "dev" / "someproject" / ".worktrees" / "task-996"
        wt.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(wt)], check=True)
        monkeypatch.setattr(safety_mod, "get_active_worktree_path", lambda *a, **k: wt)
        return wt

    def test_arbitrary_hookspath_inside_project_worktree_is_denied(self, project_worktree):
        subprocess.run(
            ["git", "-C", str(project_worktree), "config", "core.hooksPath", "tools/githooks"],
            check=True,
        )
        with pytest.raises(ToolError, match="configured Git hooks directory"):
            resolve_in_workspace("tools/githooks/pre-commit", protect_configured_hooks=True)

    def test_fs_write_into_project_worktree_hookspath_refused(self, project_worktree):
        subprocess.run(
            ["git", "-C", str(project_worktree), "config", "core.hooksPath", "tools/githooks"],
            check=True,
        )
        with pytest.raises(ToolError, match="configured Git hooks directory"):
            fs_write("tools/githooks/pre-commit", "#!/bin/sh\necho pwned\n")
        assert not (project_worktree / "tools" / "githooks" / "pre-commit").exists()

    def test_neighboring_path_in_project_worktree_still_writable(self, project_worktree):
        subprocess.run(
            ["git", "-C", str(project_worktree), "config", "core.hooksPath", "tools/githooks"],
            check=True,
        )
        fs_write("tools/build.py", "print('ok')\n")
        assert (project_worktree / "tools" / "build.py").read_text() == "print('ok')\n"

    def test_base_root_containment_unchanged_no_widening(self, project_worktree, tmp_path):
        # #995's containment union (base_root U wt_root) must not be widened by
        # this fix: a path outside both roots is still refused, even though a
        # project worktree is active.
        outside = tmp_path / "elsewhere" / "secret.txt"
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace(str(outside), protect_configured_hooks=True)

    def test_base_root_hookspath_check_unaffected_by_active_project_worktree(
        self, project_worktree, isolated_env
    ):
        # A write to the cockpit (base_root) while a project worktree is
        # active must still be checked against the *cockpit's* own
        # core.hooksPath (if any is configured there), not the worktree's.
        subprocess.run(["git", "init", "-q", str(isolated_env)], check=True)
        subprocess.run(
            ["git", "-C", str(isolated_env), "config", "core.hooksPath", "cockpit-hooks"],
            check=True,
        )
        # Absolute, since a bare relative path resolves against the active
        # worktree root (root = wt_root when one is active) -- an absolute
        # path is how a caller reaches the cockpit itself in that state.
        target = isolated_env / "cockpit-hooks" / "pre-commit"
        with pytest.raises(ToolError, match="configured Git hooks directory"):
            resolve_in_workspace(str(target), protect_configured_hooks=True)


class TestAllowlist:
    def test_allowed_command(self):
        assert vet_command("ls -la") == ["ls", "-la"]

    def test_denied_command(self):
        with pytest.raises(ToolError, match="not in the allowlist"):
            vet_command("rm -rf /")

    def test_denied_by_path_prefix(self):
        with pytest.raises(ToolError, match="not in the allowlist"):
            vet_command("/bin/rm file")

    def test_denied_with_matching_name_but_path(self):
        with pytest.raises(ToolError, match="not in the allowlist"):
            vet_command("/bin/ls file")
        with pytest.raises(ToolError, match="not in the allowlist"):
            vet_command("./ls file")

    def test_empty_command(self):
        with pytest.raises(ToolError, match="empty"):
            vet_command("   ")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "figlet:cowsay")
        assert config.shell_allowlist() == {"figlet", "cowsay"}
        with pytest.raises(ToolError):
            vet_command("ls")


class TestFsTools:
    def test_write_read_roundtrip(self, isolated_env):
        fs_write("notes/a.txt", "hello\nworld")
        out = fs_read("notes/a.txt")
        assert out["content"] == "hello\nworld"
        assert out["total_lines"] == 2
        assert out["truncated"] is False

    def test_read_paging(self, isolated_env):
        fs_write("big.txt", "\n".join(str(i) for i in range(100)))
        out = fs_read("big.txt", offset=10, limit=5)
        assert out["content"].splitlines() == ["10", "11", "12", "13", "14"]
        assert out["truncated"] is True

    def test_write_escape_rejected(self, isolated_env):
        with pytest.raises(ToolError):
            fs_write("../evil.txt", "x")

    def test_list(self, isolated_env):
        fs_write("d/one.py", "x")
        fs_write("d/two.txt", "y")
        out = fs_list("d", glob="*.py")
        assert [e["name"] for e in out["entries"]] == ["one.py"]


class TestRunCmd:
    def test_echo(self, isolated_env):
        out = run_cmd("echo hi")
        assert out["exit_code"] == 0
        assert out["stdout"].strip() == "hi"

    def test_metacharacters_inert(self, isolated_env):
        # No absolute path: a "/" arg would force the approval gate (task #682).
        out = run_cmd("echo hi; rm -rf x")
        assert out["exit_code"] == 0
        assert "rm" in out["stdout"]

    def test_cwd_sandboxed(self, isolated_env):
        with pytest.raises(ToolError):
            run_cmd("ls", cwd="../..")

    def test_denied(self, isolated_env):
        with pytest.raises(ToolError):
            run_cmd("curl http://example.com")


class TestProjectReadReach:
    @pytest.fixture
    def project_repo(self, tmp_path, isolated_env):
        repo = tmp_path / "dev" / "unified_agent"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "mod.py").write_text("VALUE = 1\n")
        assert repo.resolve() in project_read_roots()
        return repo

    def test_read_reaches_a_project_checkout(self, project_repo):
        got = fs_read(str(project_repo / "src" / "mod.py"))
        assert "VALUE = 1" in got["content"]

    def test_list_reaches_a_project_checkout(self, project_repo):
        names = {e["name"] for e in fs_list(str(project_repo / "src"))["entries"]}
        assert "mod.py" in names

    def test_search_reports_paths_relative_to_the_project_root(self, project_repo):
        hits = fs_search("VALUE", path=str(project_repo))["matches"]
        assert [h["path"] for h in hits] == ["src/mod.py"]

    def test_relative_escape_cannot_enter_a_project_checkout(self, isolated_env, project_repo):
        rel = os.path.relpath(project_repo / "src" / "mod.py", isolated_env)
        assert not os.path.isabs(rel)
        with pytest.raises(ToolError, match="absolute path"):
            resolve_for_project_read(rel)

    def test_search_skips_denied_project_files(self, project_repo):
        (project_repo / ".env").write_text("NEEDLE=secret\n")
        dep = project_repo / "node_modules" / "pkg.js"
        dep.parent.mkdir(parents=True)
        dep.write_text("NEEDLE=dependency\n")
        hits = fs_search("NEEDLE", path=str(project_repo))["matches"]
        assert hits == []

    def test_search_skips_symlink_that_escapes_readable_roots(self, project_repo, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("NEEDLE=outside\n")
        os.symlink(outside, project_repo / "src" / "linked.txt")
        hits = fs_search("NEEDLE", path=str(project_repo))["matches"]
        assert hits == []

    def test_search_skips_hook_directories_in_the_workspace_too(self, isolated_env):
        """The hook-dir guard is not a widened-root-only rule: resolve_in_workspace

        enforces it for the workspace itself, so a workspace-rooted fs_search must
        not become a way to read .git/.hg/.svn/.husky contents that a direct
        fs_read on the same path already refuses.
        """
        target = isolated_env / ".git" / "config"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[core]\nrepositoryformatversion = 0\n")
        hits = fs_search("repositoryformatversion", path=".")["matches"]
        assert hits == []

    def test_search_reports_a_cross_root_symlink_hit_without_crashing(
        self, project_repo, isolated_env
    ):
        """A symlink inside a project checkout may point into another readable

        root (here, the workspace). Reporting such a hit against the search
        root's own display root -- rather than whichever root actually
        contains the resolved file -- crashes relative_to() with an uncaught
        ValueError instead of returning a result.
        """
        (isolated_env / "leaked.txt").write_text("NEEDLE_CROSS\n")
        os.symlink(isolated_env / "leaked.txt", project_repo / "link_to_workspace.txt")
        hits = fs_search("NEEDLE_CROSS", path=str(project_repo))["matches"]
        assert [h["path"] for h in hits] == ["leaked.txt"]

    def test_search_resolves_the_active_worktree_at_most_once_per_call(
        self, project_repo, monkeypatch
    ):
        """fs_search must not re-resolve the active worktree per candidate file:

        that means one DB connection per call, not one per file scanned.
        """
        import unified_agent.safety as safety_mod

        calls = []
        original = safety_mod.get_active_worktree_path

        def counting(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(safety_mod, "get_active_worktree_path", counting)
        for i in range(5):
            (project_repo / "src" / f"extra{i}.py").write_text(f"NEEDLE {i}\n")

        hits = fs_search("NEEDLE", path=str(project_repo))["matches"]

        assert len(hits) == 5
        assert len(calls) <= 1

    def test_write_into_a_project_checkout_is_refused(self, project_repo):
        with pytest.raises(ToolError, match="escapes the workspace"):
            fs_write(str(project_repo / "src" / "mod.py"), "PWNED = 1\n")
        assert project_repo.joinpath("src/mod.py").read_text() == "VALUE = 1\n"

    def test_red_team_write_through_the_widened_read_path(self, project_repo):
        fs_read(str(project_repo / "src" / "mod.py"))
        for attempt in (
            str(project_repo / "src" / "mod.py"),
            str(project_repo / "newfile.txt"),
            str(project_repo / ".." / "unified_agent" / "src" / "mod.py"),
        ):
            with pytest.raises(ToolError, match="escapes the workspace"):
                fs_write(attempt, "PWNED\n")
        assert not project_repo.joinpath("newfile.txt").exists()

    def test_run_cmd_cwd_outside_the_workspace_is_refused(self, project_repo):
        with pytest.raises(ToolError, match="escapes the workspace"):
            run_cmd("git status", cwd=str(project_repo))

    def test_resolve_in_workspace_itself_is_unchanged(self, project_repo):
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace(str(project_repo / "src" / "mod.py"))

    def test_secret_bearing_files_are_refused(self, project_repo):
        (project_repo / ".env").write_text("API_KEY=live\n")
        (project_repo / "id_rsa").write_text("-----BEGIN-----\n")
        for name in (".env", "id_rsa"):
            with pytest.raises(ToolError, match="secret-bearing"):
                fs_read(str(project_repo / name))

    def test_workspace_secret_reads_are_refused(self, isolated_env):
        """Workspace reads use the same secret-name denylist as project reads.

        Ordinary source files, including names that merely contain ``token``,
        still read. ``fs_write`` of a non-secret source file is unchanged.
        """
        refused = {
            "service.env": "x\n",
            ".env": "x\n",
            ".env.local": "x\n",
            "auth.json": "{}\n",
            "foo.db": "x\n",
            "proj.sqlite3": "x\n",
            "notes.sqlite": "x\n",
            ".pgpass": "x\n",
            "kubeconfig": "x\n",
            "gcp-service-account.json": "{}\n",
            "service_account.json": "{}\n",
        }
        for name, body in refused.items():
            (isolated_env / name).write_text(body)
        (isolated_env / "tokenizer.py").write_text("OK = 1\n")
        (isolated_env / "plain.py").write_text("FINE = 1\n")
        (isolated_env / "author.json").write_text("{}\n")
        for name in refused:
            with pytest.raises(ToolError, match="secret-bearing"):
                fs_read(name)
        nested = isolated_env / "secrets" / "contents.txt"
        nested.parent.mkdir()
        nested.write_text("SECRET=1\n")
        with pytest.raises(ToolError, match="secret-bearing"):
            fs_read("service.env/contents.txt")
        hidden = isolated_env / "notes.txt"
        hidden.symlink_to(nested)
        with pytest.raises(ToolError, match="secret-bearing"):
            fs_read("notes.txt")
        accounting = isolated_env / "src" / "service_accounting.py"
        accounting.parent.mkdir()
        accounting.write_text("LEDGER = 1\n")
        tree = isolated_env / "service-accounting" / "report.py"
        tree.parent.mkdir()
        tree.write_text("LEDGER = 2\n")
        assert "LEDGER = 1" in fs_read("src/service_accounting.py")["content"]
        assert "LEDGER = 2" in fs_read("service-accounting/report.py")["content"]
        assert "OK = 1" in fs_read("tokenizer.py")["content"]
        assert "FINE = 1" in fs_read("plain.py")["content"]
        assert fs_read("author.json")["content"]
        written = fs_write("plain.py", "FINE = 2\n")
        assert written["bytes_written"] > 0
        assert "FINE = 2" in fs_read("plain.py")["content"]

        (isolated_env / "service.env").write_text("NEEDLE=secret\n")
        (isolated_env / "visible.py").write_text("NEEDLE=ok\n")
        hits = fs_search("NEEDLE", path=".")["matches"]
        assert [h["path"] for h in hits] == ["visible.py"]
        nested_hits = fs_search("SECRET", path=".")["matches"]
        assert nested_hits == []
        ledger_hits = fs_search("LEDGER", path=".")["matches"]
        assert {h["path"] for h in ledger_hits} == {
            "src/service_accounting.py",
            "service-accounting/report.py",
        }
        secret_file = isolated_env / ".env"
        secret_file.write_text("ALIAS_SECRET=1\n")
        alias = isolated_env / "alias.txt"
        alias.symlink_to(secret_file)
        disclosed = fs_search("ALIAS_SECRET", path=".")["matches"]
        assert disclosed == []

    def test_a_name_merely_containing_token_is_still_readable(self, project_repo):
        (project_repo / "src" / "tokenizer.py").write_text("OK = 1\n")
        assert "OK = 1" in fs_read(str(project_repo / "src" / "tokenizer.py"))["content"]

    def test_runtime_and_dependency_trees_are_refused(self, project_repo):
        for part in (".venv", "node_modules"):
            target = project_repo / part / "thing.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x = 1\n")
            with pytest.raises(ToolError, match="runtime or dependency trees"):
                fs_read(str(target))

    def test_hook_directories_are_refused(self, project_repo):
        target = project_repo / ".git" / "config"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[core]\n")
        with pytest.raises(ToolError, match="hook-execution directory"):
            fs_read(str(target))

    def test_a_path_outside_every_root_is_refused(self, tmp_path, project_repo):
        stranger = tmp_path / "elsewhere" / "secrets.txt"
        stranger.parent.mkdir(parents=True, exist_ok=True)
        stranger.write_text("nope\n")
        with pytest.raises(ToolError, match="outside every readable root"):
            fs_read(str(stranger))

    def test_relative_paths_still_mean_the_workspace(self, isolated_env, project_repo):
        (isolated_env / "src").mkdir(parents=True, exist_ok=True)
        (isolated_env / "src" / "mod.py").write_text("WORKSPACE = 1\n")
        assert "WORKSPACE = 1" in fs_read("src/mod.py")["content"]

    def test_audit_scope_is_not_widened(self, project_repo, monkeypatch):
        monkeypatch.delenv("UA_AUDIT_ROOTS", raising=False)
        with pytest.raises(ToolError, match="outside the auditable roots"):
            resolve_for_audit(str(project_repo / "src" / "mod.py"))


class TestResolveForAuditDenyBypass:
    """UA #891: DENY identities may still run read-only security_audit."""

    def test_deny_identity_can_resolve_workspace_audit_path(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_AUDIT_ROOTS", raising=False)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        (isolated_env / "note.md").write_text("hello\n")
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            with pytest.raises(ToolError, match="denied"):
                config.workspace_root()
            assert resolve_for_audit("note.md") == isolated_env / "note.md"
        finally:
            config.workspace_root_var.reset(token)

    def test_deny_identity_can_resolve_vault_inbox_relative(
        self, isolated_env, tmp_path, monkeypatch
    ):
        vault = tmp_path / "vault"
        inbox = vault / "00 - Inbox"
        inbox.mkdir(parents=True)
        note = inbox / "capture.md"
        note.write_text("capture\n")
        monkeypatch.delenv("UA_AUDIT_ROOTS", raising=False)
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            assert resolve_for_audit("00 - Inbox/capture.md") == note.resolve()
        finally:
            config.workspace_root_var.reset(token)

    def test_fs_write_still_denied_under_deny_binding(self, isolated_env):
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            with pytest.raises(ToolError, match="denied"):
                fs_write("still-denied.txt", "nope")
        finally:
            config.workspace_root_var.reset(token)

    def test_remediate_refused_under_deny_binding(self, isolated_env):
        from unified_agent.tools.audit import security_audit

        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            with pytest.raises(ToolError, match="remediate is not available"):
                security_audit(".", remediate=True)
        finally:
            config.workspace_root_var.reset(token)

    def test_remediate_refused_for_vault_path_via_audit_resolver(
        self, isolated_env, tmp_path, monkeypatch
    ):
        from unified_agent.tools.audit import security_audit

        vault = tmp_path / "vault"
        inbox = vault / "00 - Inbox"
        inbox.mkdir(parents=True)
        (inbox / "capture.md").write_text("capture\n")
        monkeypatch.delenv("UA_AUDIT_ROOTS", raising=False)
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))
        with pytest.raises(ToolError, match="vault paths"):
            security_audit("00 - Inbox", remediate=True)

    def test_remediate_refused_outside_the_workspace(self, isolated_env, tmp_path, monkeypatch):
        from unified_agent.tools.audit import security_audit

        outside = tmp_path / "audit_root"
        outside.mkdir()
        (outside / "README.md").write_text("hello\n")
        monkeypatch.setenv("UA_AUDIT_ROOTS", str(outside))
        monkeypatch.setenv("UA_VAULT_PATH", "")
        with pytest.raises(ToolError, match="active task worktree"):
            security_audit(str(outside), remediate=True)
        assert not (outside / ".gitignore").exists()

    def test_remediate_allowed_in_the_active_worktree(self, isolated_env, tmp_path, monkeypatch):
        from unified_agent.tools import audit as audit_mod
        from unified_agent.tools.audit import security_audit

        worktree = tmp_path / "task-wt"
        worktree.mkdir()
        (worktree / "README.md").write_text("hello\n")
        monkeypatch.setattr(audit_mod, "get_active_worktree_path", lambda: worktree)
        monkeypatch.setenv("UA_AUDIT_ROOTS", str(worktree))
        monkeypatch.setenv("UA_VAULT_PATH", "")
        report = security_audit(str(worktree), remediate=True)
        assert Path(report["root"]).resolve() == worktree.resolve()

    def test_remediate_still_allowed_inside_the_workspace(self, isolated_env, monkeypatch):
        from unified_agent.tools.audit import security_audit

        monkeypatch.delenv("UA_AUDIT_ROOTS", raising=False)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        (isolated_env / "README.md").write_text("hello\n")
        report = security_audit(".", remediate=True)
        assert report["status"] in {"pass", "warn", "fail"}
        assert Path(report["root"]).resolve() == isolated_env.resolve()
