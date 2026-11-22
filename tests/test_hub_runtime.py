"""Hub start record and checkout HEAD resolution (task #1771)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from unified_agent import hub_runtime

SHA_A = "a" * 40
SHA_B = "b" * 40


def _fake_repo(root, head: str, *, loose: dict[str, str] | None = None, packed: str = ""):
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n")
    for ref, sha in (loose or {}).items():
        path = git / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha + "\n")
    if packed:
        (git / "packed-refs").write_text(packed)
    return root


class TestCheckoutHead:
    def test_loose_branch_ref(self, tmp_path):
        repo = _fake_repo(tmp_path, "ref: refs/heads/main", loose={"refs/heads/main": SHA_A})
        assert hub_runtime.checkout_head(repo) == SHA_A

    def test_packed_branch_ref(self, tmp_path):
        packed = f"# pack-refs with: peeled fully-peeled sorted\n{SHA_B} refs/heads/main\n"
        repo = _fake_repo(tmp_path, "ref: refs/heads/main", packed=packed)
        assert hub_runtime.checkout_head(repo) == SHA_B

    def test_loose_ref_wins_over_packed(self, tmp_path):
        packed = f"{SHA_B} refs/heads/main\n"
        repo = _fake_repo(
            tmp_path, "ref: refs/heads/main", loose={"refs/heads/main": SHA_A}, packed=packed
        )
        assert hub_runtime.checkout_head(repo) == SHA_A

    def test_detached_head(self, tmp_path):
        assert hub_runtime.checkout_head(_fake_repo(tmp_path, SHA_A)) == SHA_A

    def test_linked_worktree_uses_common_dir_refs(self, tmp_path):
        main = _fake_repo(
            tmp_path / "main", "ref: refs/heads/main", loose={"refs/heads/main": SHA_A}
        )
        wt_git = main / ".git" / "worktrees" / "wt"
        wt_git.mkdir(parents=True)
        (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
        (wt_git / "commondir").write_text("../..\n")
        (main / ".git" / "refs" / "heads" / "feature").write_text(SHA_B + "\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {wt_git}\n")
        assert hub_runtime.checkout_head(wt) == SHA_B

    def test_not_a_checkout_is_none(self, tmp_path):
        assert hub_runtime.checkout_head(tmp_path) is None

    def test_unresolvable_ref_is_none(self, tmp_path):
        assert hub_runtime.checkout_head(_fake_repo(tmp_path, "ref: refs/heads/gone")) is None

    def test_matches_git_rev_parse(self, tmp_path):
        if not shutil.which("git"):
            pytest.skip("git not installed")
        repo = tmp_path / "real"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        for argv in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "commit", "-q", "--allow-empty", "-m", "one"],
            ["git", "pack-refs", "--all"],
            ["git", "commit", "-q", "--allow-empty", "-m", "two"],
        ):
            subprocess.run(argv, cwd=repo, check=True, env=env, capture_output=True)
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        assert hub_runtime.checkout_head(repo) == expected


class TestStartRecord:
    def test_record_then_read_round_trip(self, tmp_path):
        repo = _fake_repo(tmp_path / "repo", SHA_A)
        record = hub_runtime.record_hub_start(root=repo)
        assert record is not None
        assert record["sha"] == SHA_A
        assert record["checkout"] == str(repo.resolve())
        assert record["pid"] == os.getpid()
        assert hub_runtime.read_hub_start() == record
        on_disk = json.loads(hub_runtime.start_record_path().read_text())
        assert on_disk == record

    def test_record_outside_a_checkout_keeps_null_sha(self, tmp_path):
        record = hub_runtime.record_hub_start(root=tmp_path)
        assert record is not None and record["sha"] is None

    def test_unreadable_record_is_none(self):
        path = hub_runtime.start_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert hub_runtime.read_hub_start() is None
