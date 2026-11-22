"""Remote-executor worktree localization (task #1094 follow-up).

A desktop executor claims tasks through the Mac hub, so task_claim records a
HUB-side absolute worktree path (/Users/you/dev/<repo>/.worktrees/...).
Handing that path to a backend as cwd on the desktop used to spawn the child
in a directory that only exists on the hub. These tests pin the re-rooting
map and the create/fallback behavior.
"""

import subprocess
from pathlib import Path

import pytest

from unified_agent.executor.daemon import ExecutorDaemon


def _daemon() -> ExecutorDaemon:
    """Bare instance: localize_worktree only needs self._git (staticmethod)."""
    return object.__new__(ExecutorDaemon)


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    # Commit identity must not depend on the runner's global git config: CI has
    # no user.name/user.email, so `git commit` exits 128 there while passing on
    # a developer machine. Pin it in the throwaway repo instead.
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "executor-test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Executor Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "init"], check=True
    )
    return root


class TestRemoteWorktreeLocalCandidate:
    def test_maps_hub_home_onto_local_dev(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        cand = ExecutorDaemon._remote_worktree_local_candidate(
            "/Users/you/dev/browser/.worktrees/task-1109"
        )
        assert cand == tmp_path / "dev" / "browser" / ".worktrees" / "task-1109"

    def test_returns_none_without_dev_segment(self):
        assert ExecutorDaemon._remote_worktree_local_candidate("/srv/worktrees/task-1") is None


class TestLocalizeWorktree:
    def test_existing_local_path_returned_unchanged(self, tmp_path):
        existing = tmp_path / "already-here"
        existing.mkdir()
        assert _daemon().localize_worktree(str(existing), 1, "task/1-x") == str(existing)

    def test_creates_local_worktree_from_hub_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        d = _daemon()
        hub_path = "/Users/you/dev/browser/.worktrees/task-1109"
        local = d.localize_worktree(hub_path, 1109, "task/1109-slug")
        assert local is not None
        assert Path(local) == repo / ".worktrees" / "task-1109"
        assert Path(local).is_dir()
        branch = subprocess.run(
            ["git", "-C", local, "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch == "task/1109-slug"
        # Second call is idempotent — the worktree already exists.
        assert d.localize_worktree(hub_path, 1109, "task/1109-slug") == local

    def test_reuses_local_branch_when_it_already_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(["git", "-C", str(repo), "branch", "task/7-slug"], check=True)
        local = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-7", 7, "task/7-slug"
        )
        assert local is not None
        assert Path(local).is_dir()

    def test_falls_back_to_none_without_local_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # no ~/dev/browser on this host at all
        result = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-3", 3, "task/3-slug"
        )
        assert result is None

    @pytest.mark.parametrize("enclosing", ["home", "dev root"])
    def test_walk_stops_below_dev_root_when_checkout_absent(self, tmp_path, monkeypatch, enclosing):
        # Task #2288: with ~/dev/<repo> absent, the .git walk-up used to climb to
        # ~/dev, ~ and / and run `git worktree add` in whichever ancestor was a
        # repository (a dotfile repo in ~, say).
        home = tmp_path / "home"
        outer = _init_repo(home if enclosing == "home" else home / "dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        result = _daemon().localize_worktree(
            "/Users/you/dev/example-project/.worktrees/task-3", 3, "task/3-slug"
        )
        assert result is None
        listed = _git(outer, "worktree", "list", "--porcelain")
        assert listed.count("worktree ") == 1
        assert not (home / "dev" / "example-project").exists()

    def test_rejects_parent_segments_escaping_dev_root(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        outer = _init_repo(home)
        (home / "dev").mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert (
            ExecutorDaemon._remote_worktree_local_candidate("/Users/you/dev/../.worktrees/task-5")
            is None
        )
        result = _daemon().localize_worktree(
            "/Users/you/dev/../.worktrees/task-5", 5, "task/5-slug"
        )
        assert result is None
        assert _git(outer, "worktree", "list", "--porcelain").count("worktree ") == 1

    def test_symlink_below_dev_root_cannot_alias_enclosing_repo(self, tmp_path):
        # Review of #2288: a lexical bound is not enough when ~/dev/<repo> is a
        # symlink to an enclosing repository such as ~.
        home = tmp_path / "home"
        _init_repo(home)
        dev_root = home / "dev"
        dev_root.mkdir()
        (dev_root / "example-project").symlink_to(home, target_is_directory=True)
        candidate = dev_root / "example-project" / ".worktrees" / "task-7"
        assert ExecutorDaemon._local_repo_below_dev_root(candidate, dev_root) is None

    def test_git_symlink_escaping_dev_root_is_refused(self, tmp_path):
        home = tmp_path / "home"
        _init_repo(home)
        checkout = home / "dev" / "example-project"
        checkout.mkdir(parents=True)
        (checkout / ".git").symlink_to(home / ".git", target_is_directory=True)
        candidate = checkout / ".worktrees" / "task-8"
        assert ExecutorDaemon._local_repo_below_dev_root(candidate, home / "dev") is None

    def test_symlinked_dev_root_still_finds_checkout(self, tmp_path):
        real_dev = tmp_path / "volume" / "dev"
        repo = _init_repo(real_dev / "example-project")
        home = tmp_path / "home"
        home.mkdir()
        (home / "dev").symlink_to(real_dev, target_is_directory=True)
        candidate = home / "dev" / "example-project" / ".worktrees" / "task-9"
        found = ExecutorDaemon._local_repo_below_dev_root(candidate, home / "dev")
        assert found == home / "dev" / "example-project"
        assert found.resolve() == repo.resolve()

    def test_falls_back_to_none_when_git_add_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        d = _daemon()

        def boom(*_a, **_k):
            raise RuntimeError("git exploded")

        d._git = boom  # type: ignore[method-assign]
        result = d.localize_worktree("/Users/you/dev/browser/.worktrees/task-4", 4, "task/4-slug")
        assert result is None
        assert repo.exists()

    # F1: wrong-repo re-root must fail closed when origin remote mismatches hub-side repo
    def test_fails_closed_when_local_repo_origin_remote_mismatches_hub_repo(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/anonb3ll/other-repo.git",
            ],
            check=True,
        )
        hub_path = "/Users/you/dev/browser/.worktrees/task-1109"
        result = _daemon().localize_worktree(hub_path, 1109, "task/1109-slug")
        assert result is None
        assert not (repo / ".worktrees" / "task-1109").exists()

    def test_succeeds_when_local_repo_origin_remote_matches_hub_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "git@github.com:anonb3ll/browser.git",
            ],
            check=True,
        )
        hub_path = "/Users/you/dev/browser/.worktrees/task-1109"
        result = _daemon().localize_worktree(hub_path, 1109, "task/1109-slug")
        assert result is not None
        assert Path(result) == repo / ".worktrees" / "task-1109"
        assert Path(result).is_dir()

    # F2: stale local branch must be compared to hub branch head and recreated or failed closed
    def test_stale_local_branch_recreated_to_match_hub_head(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        # task/7-slug branch is created at initial commit
        subprocess.run(["git", "-C", str(repo), "branch", "task/7-slug"], check=True)
        # Add a new commit so HEAD advances past the branch
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "second"], check=True
        )
        commit2_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Localize with hub_branch_head pointing to commit2_sha
        local = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-7",
            7,
            "task/7-slug",
            hub_branch_head=commit2_sha,
        )
        assert local is not None
        assert Path(local).is_dir()
        worktree_head = subprocess.run(
            ["git", "-C", local, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert worktree_head == commit2_sha

    def test_stale_local_branch_fails_closed_when_recreation_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(["git", "-C", str(repo), "branch", "task/7-slug"], check=True)
        # Non-existent commit SHA cannot be recreated
        result = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-7",
            7,
            "task/7-slug",
            hub_branch_head="0123456789abcdef0123456789abcdef01234567",
        )
        assert result is None
        assert not (repo / ".worktrees" / "task-7").exists()

    def test_stale_local_branch_fails_closed_on_mismatch_when_recreate_false(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(["git", "-C", str(repo), "branch", "task/7-slug"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "second"], check=True
        )
        commit2_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        result = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-7",
            7,
            "task/7-slug",
            hub_branch_head=commit2_sha,
            recreate_stale=False,
        )
        assert result is None
        assert not (repo / ".worktrees" / "task-7").exists()

    def test_stale_local_branch_recreated_from_origin_remote_tracking_branch(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        subprocess.run(["git", "-C", str(repo), "branch", "task/7-slug"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "second"], check=True
        )
        commit2_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Set up origin/task/7-slug tracking ref to point to commit2_sha
        subprocess.run(["git", "-C", str(repo), "branch", "origin/task/7-slug", "HEAD"], check=True)

        local = _daemon().localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-7",
            7,
            "task/7-slug",
        )
        assert local is not None
        assert Path(local).is_dir()
        worktree_head = subprocess.run(
            ["git", "-C", local, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert worktree_head == commit2_sha


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class TestLocalizeWorktreeAttestedExisting:
    """Task #1528: an EXISTING path is admitted only when it matches the attested run.

    With ``hub_branch_head`` supplied (authoritative execution), an existing
    hub-local directory or re-rooted mirror must be a git worktree toplevel on
    the attested branch at exactly the attested SHA; anything else fails closed
    instead of being executed against.
    """

    def _hub_local(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        base = _git(repo, "rev-parse", "HEAD")
        wt = repo / ".worktrees" / "task-1"
        _git(repo, "worktree", "add", "-q", "-b", "task/1-x", str(wt))
        return repo, wt, base

    def test_hub_local_wrong_sha_fails_closed(self, tmp_path):
        _repo, wt, base = self._hub_local(tmp_path)
        _commit(wt, "advance")
        result = _daemon().localize_worktree(
            str(wt), 1, "task/1-x", hub_branch_head=base, recreate_stale=False
        )
        assert result is None

    def test_hub_local_wrong_branch_fails_closed(self, tmp_path):
        _repo, wt, base = self._hub_local(tmp_path)
        _git(wt, "checkout", "-q", "-b", "task/other")
        result = _daemon().localize_worktree(
            str(wt), 1, "task/1-x", hub_branch_head=base, recreate_stale=False
        )
        assert result is None

    def test_hub_local_detached_head_fails_closed(self, tmp_path):
        _repo, wt, base = self._hub_local(tmp_path)
        _git(wt, "checkout", "-q", "--detach")
        result = _daemon().localize_worktree(
            str(wt), 1, "task/1-x", hub_branch_head=base, recreate_stale=False
        )
        assert result is None

    def test_hub_local_plain_subdirectory_fails_closed(self, tmp_path):
        repo, _wt, base = self._hub_local(tmp_path)
        sub = repo / "subdir"
        sub.mkdir()
        result = _daemon().localize_worktree(
            str(sub), 1, "task/1-x", hub_branch_head=base, recreate_stale=False
        )
        assert result is None

    def test_hub_local_non_git_directory_fails_closed(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = _daemon().localize_worktree(
            str(plain),
            1,
            "task/1-x",
            hub_branch_head="0123456789abcdef0123456789abcdef01234567",
            recreate_stale=False,
        )
        assert result is None

    def test_hub_local_matching_worktree_admitted(self, tmp_path):
        _repo, wt, base = self._hub_local(tmp_path)
        result = _daemon().localize_worktree(
            str(wt), 1, "task/1-x", hub_branch_head=base, recreate_stale=False
        )
        assert result == str(wt)

    def test_relative_path_is_validated_against_workspace_root(self, tmp_path, monkeypatch):
        from unified_agent import config

        repo, wt, base = self._hub_local(tmp_path)
        monkeypatch.setattr(config, "workspace_root", lambda: repo)
        rel = ".worktrees/task-1"
        assert (
            _daemon().localize_worktree(
                rel, 1, "task/1-x", hub_branch_head=base, recreate_stale=False
            )
            == rel
        )
        _commit(wt, "advance")
        assert (
            _daemon().localize_worktree(
                rel, 1, "task/1-x", hub_branch_head=base, recreate_stale=False
            )
            is None
        )
        # A relative path that does not exist cannot be attested either.
        assert (
            _daemon().localize_worktree(
                ".worktrees/missing", 1, "task/1-x", hub_branch_head=base, recreate_stale=False
            )
            is None
        )

    def test_legacy_call_without_hub_head_keeps_existing_dir_unchanged(self, tmp_path):
        _repo, wt, _base = self._hub_local(tmp_path)
        _git(wt, "checkout", "-q", "-b", "task/other")
        _commit(wt, "advance")
        assert _daemon().localize_worktree(str(wt), 1, "task/1-x") == str(wt)

    def _mirror(self, tmp_path, monkeypatch, origin=None):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        if origin:
            _git(repo, "remote", "add", "origin", origin)
        base = _git(repo, "rev-parse", "HEAD")
        hub_path = "/Users/you/dev/browser/.worktrees/task-7"
        first = _daemon().localize_worktree(
            hub_path,
            7,
            "task/7-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert first is not None
        assert Path(first) == repo / ".worktrees" / "task-7"
        return repo, hub_path, base, Path(first)

    def test_rerooted_matching_mirror_admitted(self, tmp_path, monkeypatch):
        _repo, hub_path, base, mirror = self._mirror(
            tmp_path, monkeypatch, origin="git@github.com:anonb3ll/browser.git"
        )
        result = _daemon().localize_worktree(
            hub_path,
            7,
            "task/7-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert result == str(mirror)

    def test_rerooted_stale_mirror_head_fails_closed(self, tmp_path, monkeypatch):
        _repo, hub_path, base, mirror = self._mirror(tmp_path, monkeypatch)
        _commit(mirror, "stale generation work")
        result = _daemon().localize_worktree(
            hub_path,
            7,
            "task/7-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert result is None

    def test_rerooted_mirror_on_wrong_branch_fails_closed(self, tmp_path, monkeypatch):
        _repo, hub_path, base, mirror = self._mirror(tmp_path, monkeypatch)
        _git(mirror, "checkout", "-q", "-b", "task/other")
        result = _daemon().localize_worktree(
            hub_path,
            7,
            "task/7-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert result is None

    def test_rerooted_mirror_origin_mismatch_fails_closed(self, tmp_path, monkeypatch):
        repo, hub_path, base, _mirror = self._mirror(
            tmp_path, monkeypatch, origin="git@github.com:anonb3ll/browser.git"
        )
        _git(repo, "remote", "set-url", "origin", "https://github.com/anonb3ll/other-repo.git")
        result = _daemon().localize_worktree(
            hub_path,
            7,
            "task/7-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert result is None

    def test_rerooted_legacy_call_keeps_existing_mirror_unchanged(self, tmp_path, monkeypatch):
        _repo, hub_path, _base, mirror = self._mirror(tmp_path, monkeypatch)
        _commit(mirror, "stale generation work")
        assert _daemon().localize_worktree(hub_path, 7, "task/7-slug") == str(mirror)

    def test_created_worktree_head_must_equal_attested_sha(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = _init_repo(tmp_path / "dev" / "browser")
        base = _git(repo, "rev-parse", "HEAD")
        d = _daemon()
        real_git = d._git

        def lying_git(worktree_abs, args, *, check=True):
            # Simulate a worktree that materialises on a commit other than the
            # attested one: the post-create HEAD probe must catch it.
            if args == ["rev-parse", "HEAD"] and Path(worktree_abs).name == "task-9":
                return subprocess.CompletedProcess(args, 0, stdout="f" * 40 + "\n", stderr="")
            return real_git(worktree_abs, args, check=check)

        d._git = lying_git  # type: ignore[method-assign]
        result = d.localize_worktree(
            "/Users/you/dev/browser/.worktrees/task-9",
            9,
            "task/9-slug",
            hub_branch_head=base,
            hub_repo="browser",
            recreate_stale=False,
        )
        assert result is None


@pytest.mark.parametrize("available", [True, False])
def test_missing_attested_commit_fetches_origin_once_before_creating_checkout(
    tmp_path, monkeypatch, available
):
    upstream = _init_repo(tmp_path / "upstream")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    local = tmp_path / "dev" / "browser"
    local.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "commit", "-q", "--allow-empty", "-m", "new source"],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if not available:
        sha = "1" * 40
    daemon = _daemon()
    monkeypatch.setattr(daemon, "_origin_matches_hub_repo", lambda *_args: True)
    calls = []
    original = daemon._git

    def traced(repo, args, **kwargs):
        calls.append(args)
        return original(repo, args, **kwargs)

    monkeypatch.setattr(daemon, "_git", traced)
    result = daemon.localize_worktree(
        "/Users/you/dev/browser/.worktrees/task-4",
        4,
        "task/4-source",
        hub_branch_head=sha,
        recreate_stale=False,
    )
    assert calls.count(["fetch", "origin"]) == 1
    if available:
        assert result is not None
        assert (
            subprocess.check_output(["git", "-C", result, "rev-parse", "HEAD"], text=True).strip()
            == sha
        )
    else:
        assert result is None
        assert not (local / ".worktrees/task-4").exists()
