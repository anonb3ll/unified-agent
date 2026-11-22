"""Task #2225: the audit judges what git will commit, not only the working copy.

The pre-commit hook runs a whole-tree audit. It read only working-tree
files, so a secret staged in the index and then removed from the working
copy (or the file deleted) passed the gate and was committed. Separately,
an untracked transcript or credential store was relaxed on one `git
ls-files` answer; staging it while the audit ran left the secret relaxed.

Every key below is a fake assembled at runtime, so the repo's own gates
stay clean.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import pytest
from test_audit_transcripts import _fake_jwk, _session_json, _vendor_key

from unified_agent.audit import cli, engine, run_audit

STAGED_NOTE = "staged in the git index"
TRANSCRIPT = ".hermes/sessions/session_20261025_100000_10ba09d7.json"


def _git(root, *args, env=None, input_text=None):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    ).stdout


def _repo(root):
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    return root


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _base_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _jwk_line() -> str:
    return json.dumps(_fake_jwk()) + "\n"


def _secrets(report):
    return [fd for fd in report["findings"] if fd["rule"] == "SECRETS-KEY"]


def _index_findings(report):
    return [fd for fd in report["findings"] if fd["rule"] == "AUDIT-INDEX-INCOMPLETE"]


def _partial_findings(report):
    return [fd for fd in report["findings"] if fd["rule"] == "AUDIT-INDEX-PARTIAL"]


def _record_blob_fetches(monkeypatch):
    """Object ids sent to `git cat-file --batch` / `cat-file blob`."""
    real_run = engine.subprocess.run
    fetched = []

    def recording(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "cat-file" in cmd:
            if "--batch" in cmd:
                data = kwargs.get("input") or b""
                fetched.append(data.decode() if isinstance(data, bytes) else data)
            elif "blob" in cmd:
                fetched.append(cmd[-1])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", recording)
    return fetched


def _high_secret(report, rel):
    return [fd for fd in _secrets(report) if fd["path"] == rel and fd["severity"] == "high"]


class TestStagedBlobIsAudited:
    @pytest.mark.parametrize(
        "rel",
        [".codex/sessions/session.jsonl", TRANSCRIPT, "src/settings.json"],
    )
    def test_staged_secret_survives_a_cleaned_working_copy(self, tmp_path, rel):
        """Reviewer repro: stage a JWK, overwrite only the working copy."""
        root = _repo(tmp_path / "repo")
        text = _session_json("hermes") if rel == TRANSCRIPT else _jwk_line()
        _write(root, rel, text)
        _git(root, "add", rel)
        _write(root, rel, "{}\n")
        assert _git(root, "show", f":{rel}") == text

        report = run_audit(root, include_repo_rules=False)

        found = _high_secret(report, rel)
        assert found, "the staged secret is what the commit will carry"
        assert all(STAGED_NOTE in fd["message"] for fd in found)
        assert report["status"] == "fail"

    def test_staged_secret_in_a_file_deleted_from_the_working_copy(self, tmp_path):
        root = _repo(tmp_path / "repo")
        rel = "config/keys.py"
        path = _write(root, rel, f'KEY = "{_vendor_key()}"\n')
        _git(root, "add", rel)
        path.unlink()

        assert _high_secret(run_audit(root, include_repo_rules=False), rel)

    def test_staged_secret_found_when_auditing_one_file(self, tmp_path):
        root = _repo(tmp_path / "repo")
        rel = "notes.json"
        path = _write(root, rel, _jwk_line())
        _git(root, "add", rel)
        _write(root, rel, "{}\n")

        assert _high_secret(run_audit(path, include_repo_rules=False), rel)

    def test_staged_secret_in_a_subdirectory_audit(self, tmp_path):
        root = _repo(tmp_path / "repo")
        _write(root, "sub/a.json", _jwk_line())
        _write(root, "other/b.json", _jwk_line())
        _git(root, "add", ".")
        _write(root, "sub/a.json", "{}\n")
        _write(root, "other/b.json", "{}\n")

        report = run_audit(root / "sub", include_repo_rules=False)

        assert _high_secret(report, "a.json")
        assert {fd["path"] for fd in _secrets(report)} == {"a.json"}, "stays inside the root"

    def test_the_index_named_by_git_index_file_is_audited(self, tmp_path, monkeypatch):
        """`git commit -a` and `git commit <path>` run the hook against a
        temporary index exported as GIT_INDEX_FILE; that index is the commit."""
        root = _repo(tmp_path / "repo")
        rel = "app.json"
        _write(root, rel, "{}\n")
        _git(root, "add", rel)
        alt = tmp_path / "alt-index"
        _write(root, rel, _jwk_line())
        _git(root, "add", rel, env={**_base_env(), "GIT_INDEX_FILE": str(alt)})
        _write(root, rel, "{}\n")

        assert not _secrets(run_audit(root, include_repo_rules=False))
        monkeypatch.setenv("GIT_INDEX_FILE", str(alt))
        assert _high_secret(run_audit(root, include_repo_rules=False), rel)

    def test_same_content_in_index_and_working_copy_is_reported_once(self, tmp_path):
        root = _repo(tmp_path / "repo")
        rel = "conf.json"
        path = _write(root, rel, _jwk_line())
        _git(root, "add", rel)
        # Same bytes, newer mtime: git sees a stat change and lists the path.
        later = path.stat().st_mtime + 10
        os.utime(path, (later, later))
        assert _git(root, "diff-files", "--name-only").split() == [rel]

        found = _secrets(run_audit(root, include_repo_rules=False))
        assert len(found) == 1
        assert STAGED_NOTE not in found[0]["message"]

    def test_a_different_staged_line_is_reported_beside_the_working_copy(self, tmp_path):
        root = _repo(tmp_path / "repo")
        rel = "conf.json"
        _write(root, rel, _jwk_line())
        _git(root, "add", rel)
        _write(root, rel, "{}\n" + _jwk_line())

        found = _high_secret(run_audit(root, include_repo_rules=False), rel)
        assert sorted((fd["line"], STAGED_NOTE in fd["message"]) for fd in found) == [
            (1, True),
            (2, False),
        ]

    def test_excluded_and_skipped_paths_stay_out_of_scope(self, tmp_path):
        root = _repo(tmp_path / "repo")
        for rel in ("vendor/x.json", "node_modules/pkg/x.json"):
            _write(root, rel, _jwk_line())
            _git(root, "add", "-f", rel)
            _write(root, rel, "{}\n")

        report = run_audit(root, excludes=["vendor/"], include_repo_rules=False)

        assert not _secrets(report)
        assert report["summary"]["excluded"] >= 1

    def test_suppression_marker_applies_to_the_staged_blob(self, tmp_path):
        root = _repo(tmp_path / "repo")
        rel = "fixture.py"
        _write(root, rel, f'KEY = "{_vendor_key()}"  # ua:allow\n')
        _git(root, "add", rel)
        _write(root, rel, "\n")

        assert not _secrets(run_audit(root, include_repo_rules=False))

    def test_oversized_and_binary_blobs_are_not_read(self, tmp_path, monkeypatch):
        """Bounded like the working-copy read: a blob over the size cap is
        never fetched, and a binary blob is not scanned. Since review r5 a new
        oversized blob whose working copy is not the same bytes is unread new
        content, so the gate fails (it was a silent, documented exemption)."""
        root = _repo(tmp_path / "repo")
        _write(root, "big.json", _jwk_line() + "x" * 4096)
        (root / "bin.dat").write_bytes(b"\x00" + _jwk_line().encode())
        _git(root, "add", ".")
        _write(root, "big.json", "{}\n")
        (root / "bin.dat").write_bytes(b"\x00")
        monkeypatch.setattr(engine.config, "SEARCH_FILE_MAX_BYTES", 1024)
        fetched = _record_blob_fetches(monkeypatch)

        report = run_audit(root, include_repo_rules=False)

        assert not _secrets(report)
        assert [fd["severity"] for fd in _index_findings(report)] == ["high"]
        big_oid = _git(root, "rev-parse", ":big.json").strip()
        assert fetched and big_oid not in "".join(fetched)

    def test_fetched_blobs_are_scanned_and_dropped_one_at_a_time(self, tmp_path):
        """PR #1315 review r2: every fetched blob used to be kept until the
        last was read. 64 distinct 512 KiB blobs (32 MiB) with no working
        copies must not raise peak memory anywhere near their total."""
        import tracemalloc

        root = _repo(tmp_path / "repo")
        rows = []
        for i in range(64):
            data = f"{i:04}".encode() + b"\0" + b"x" * (512 * 1024 - 5)
            oid = (
                subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=root,
                    input=data,
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
            rows.append(f"100644 {oid}\t{i:03}.bin\n")
        _git(root, "update-index", "--index-info", input_text="".join(rows))

        tracemalloc.start()
        try:
            report = run_audit(root, include_repo_rules=False)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert report["status"] == "pass"
        assert peak < 16 * 1024 * 1024, f"peak {peak / 2**20:.1f} MiB for 32 MiB of blobs"

    def test_a_clean_tree_fetches_no_blobs(self, tmp_path, monkeypatch):
        """Every staged blob whose bytes the working-copy scan already read
        (same object id) is proven covered; nothing is fetched."""
        root = _repo(tmp_path / "repo")
        for i in range(20):
            _write(root, f"f{i}.json", json.dumps({"n": i}))
        _git(root, "add", ".")
        fetched = _record_blob_fetches(monkeypatch)

        report = run_audit(root, include_repo_rules=False)

        assert report["status"] == "pass" and not report["findings"]
        assert fetched == []

    @pytest.mark.parametrize("target", ["tree", "file"])
    def test_an_unreadable_index_fails_the_gate(self, tmp_path, target):
        root = _repo(tmp_path / "repo")
        path = _write(root, "deep/er/a.json", _jwk_line())
        (root / ".git" / "index").write_bytes(b"not an index")

        report = run_audit(root if target == "tree" else path, include_repo_rules=False)

        assert [fd["severity"] for fd in _secrets(report)] == ["high"]
        assert [fd["severity"] for fd in _index_findings(report)] == ["high"]


class TestIndexFlagsFiltersAndLinks:
    """PR #1315 review: git's own idea of a clean working copy is no proof
    that the working-copy scan read the staged bytes."""

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_index_flags_that_hide_a_change(self, tmp_path, flag):
        root = _repo(tmp_path / "repo")
        _write(root, "a.json", _jwk_line())
        _git(root, "add", "a.json")
        _write(root, "a.json", "{}\n")
        _git(root, "update-index", flag, "a.json")
        assert not _git(root, "diff-files", "--raw")

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    def test_a_clean_filter_that_stages_the_secret(self, tmp_path):
        root = _repo(tmp_path / "repo")
        filt = tmp_path / "filter.py"
        filt.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.write({_jwk_line()!r})\n")
        _git(root, "config", "filter.probe.clean", f"{shlex.quote(sys.executable)} {filt}")
        _write(root, ".gitattributes", "a.json filter=probe\n")
        _write(root, "a.json", "{}\n")
        _git(root, "add", "a.json", ".gitattributes")
        assert _git(root, "show", ":a.json") == _jwk_line()
        assert not _git(root, "diff-files", "--raw")

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    def test_an_unchanged_symlink_whose_link_text_is_the_secret(self, tmp_path):
        root = _repo(tmp_path / "repo")
        (root / "link").symlink_to(_jwk_line().strip())
        _git(root, "add", "link")
        assert not _git(root, "diff-files", "--raw")

        assert _high_secret(run_audit(root, include_repo_rules=False), "link")

    @pytest.mark.parametrize("hidden", ["cleaned", "deleted", "skip-worktree"])
    def test_a_secret_already_in_head_is_audited_again(self, tmp_path, capsys, hidden):
        """PR #1315 review r2: HEAD membership is no audit receipt. A secret
        admitted earlier (here a transcript relaxed while untracked, then
        committed) stays in every later commit while its working copy is
        hidden, so every index blob the scan did not read is fetched."""
        root = _repo(tmp_path / "repo")
        path = _write(root, TRANSCRIPT, _session_json("hermes"))
        assert {fd["severity"] for fd in _secrets(run_audit(root))} == {"low"}
        _git(root, "add", TRANSCRIPT)
        _git(root, "commit", "-qm", "seed", "--no-verify")
        if hidden == "cleaned":
            path.write_text("{}\n")
        else:
            path.unlink()
        if hidden == "skip-worktree":
            _git(root, "update-index", "--skip-worktree", TRANSCRIPT)
        _write(root, "new.txt", "an unrelated change\n")
        _git(root, "add", "new.txt")
        assert _git(root, "rev-parse", f"HEAD:{TRANSCRIPT}") == _git(
            root, "rev-parse", f":{TRANSCRIPT}"
        )

        report = run_audit(root, include_repo_rules=False)

        assert _high_secret(report, TRANSCRIPT)
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_a_replacement_ref_cannot_stand_in_for_the_staged_blob(self, tmp_path):
        """PR #1315 review r2: `git replace` made object reads return benign
        bytes for the secret's id. Audit reads ignore replacements, and the
        fetched bytes must hash to the id that was asked for."""
        root = _repo(tmp_path / "repo")
        _write(root, "a.json", _jwk_line())
        _git(root, "add", "a.json")
        _write(root, "a.json", "{}\n")
        private = _git(root, "rev-parse", ":a.json").strip()
        benign = _git(root, "hash-object", "-w", "--stdin", input_text="{}\n").strip()
        _git(root, "replace", private, benign)
        assert _git(root, "cat-file", "blob", private) == "{}\n"

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    def test_a_work_tree_bound_by_git_dir_and_git_work_tree(self, tmp_path, monkeypatch):
        """PR #1315 review r2: a hook (or a user) can bind a plain directory
        to a repository with GIT_DIR and GIT_WORK_TREE; there is no `.git`."""
        root = tmp_path / "plain"
        root.mkdir()
        db = tmp_path / "bare.git"
        _git(tmp_path, "init", "-q", "--bare", str(db))
        monkeypatch.setenv("GIT_DIR", str(db))
        monkeypatch.setenv("GIT_WORK_TREE", str(root))
        _write(root, "a.json", _jwk_line())
        _git(root, "add", "a.json")
        _write(root, "a.json", "{}\n")
        assert not (root / ".git").exists()

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    @pytest.mark.parametrize(
        "case",
        ["work-tree", "work-tree-shadowed", "git-dir", "both", "subdirectory"],
    )
    def test_relative_git_dir_and_work_tree_mean_the_callers_cwd(
        self, tmp_path, monkeypatch, capsys, case
    ):
        """PR #1315 review r3: git reads a relative GIT_DIR or GIT_WORK_TREE
        against the caller's directory. The audit runs git elsewhere, so it
        must resolve them first, or it reads no index (or the wrong one)."""
        root = tmp_path / "plain"
        root.mkdir()
        db = tmp_path / "db.git"
        _git(tmp_path, "init", "-q", "--bare", str(db))
        rel = "a.json"
        cwd, git_dir, work_tree, audited = tmp_path, str(db), "plain", root
        if case == "work-tree-shadowed":
            (root / "plain").mkdir()  # what a mis-resolved `plain` would find
        elif case == "git-dir":
            git_dir, work_tree = "db.git", str(root)
        elif case == "both":
            git_dir = "db.git"
        elif case == "subdirectory":
            cwd, git_dir, work_tree, rel = root, "../db.git", ".", "sub/a.json"
            audited = root / "sub"
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("GIT_DIR", git_dir)
        monkeypatch.setenv("GIT_WORK_TREE", work_tree)
        _write(root, rel, _jwk_line())
        _git(cwd, "add", str(root / rel))
        _write(root, rel, "{}\n")
        assert _git(cwd, "show", f":{rel}") == _jwk_line()

        report = run_audit(audited, include_repo_rules=False)

        assert _high_secret(report, "a.json")
        assert not _index_findings(report)
        assert cli.main([str(audited), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_a_relative_git_index_file_means_the_top_level(self, tmp_path, monkeypatch):
        """Git resolves a relative GIT_INDEX_FILE against the work tree's top
        level, whatever the caller's directory; so does the audit."""
        root = _repo(tmp_path / "repo")
        _write(root, "a.json", "{}\n")
        _git(root, "add", "a.json")
        (root / "sub").mkdir()
        monkeypatch.chdir(root / "sub")
        monkeypatch.setenv("GIT_INDEX_FILE", "alt-index")
        _write(root, "a.json", _jwk_line())
        _git(root / "sub", "add", str(root / "a.json"))
        assert (root / "alt-index").is_file()
        _write(root, "a.json", "{}\n")

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    def test_a_relative_git_common_dir_means_the_top_level(self, tmp_path, monkeypatch):
        """A linked work tree bound by GIT_DIR plus a relative GIT_COMMON_DIR,
        from the top level. Git checks that value from its starting directory
        and uses it from the top level, so it is valid wherever both name the
        repository (see the subdirectory case in TestCallerGitBinding)."""
        main = _repo(tmp_path / "main")
        _write(main, "seed.txt", "seed\n")
        _git(main, "add", "seed.txt")
        _git(main, "commit", "-qm", "seed", "--no-verify")
        linked = tmp_path / "linked"
        _git(main, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
        gitdir = (main / ".git" / "worktrees" / "linked").resolve()
        monkeypatch.chdir(linked)
        monkeypatch.setenv("GIT_DIR", str(gitdir))
        monkeypatch.setenv("GIT_WORK_TREE", str(linked))
        monkeypatch.setenv("GIT_COMMON_DIR", "../main/.git")
        _write(linked, "a.json", _jwk_line())
        _git(linked, "add", "a.json")
        _write(linked, "a.json", "{}\n")

        assert _high_secret(run_audit(linked, include_repo_rules=False), "a.json")

    @pytest.mark.parametrize("marker", ["dir", "file"])
    def test_a_nested_repo_with_only_staged_deleted_files(self, tmp_path, marker):
        """PR #1315 review r2: a nested repo was found only through a working
        file inside it; `.git` alone now marks it."""
        outer = _repo(tmp_path / "outer")
        inner = outer / "inner"
        if marker == "dir":
            _repo(inner)
        else:
            inner.mkdir()
            _git(inner, "init", "-q", f"--separate-git-dir={tmp_path / 'inner.git'}")
            assert (inner / ".git").is_file()
        _write(inner, "a.json", _jwk_line())
        _git(inner, "add", "a.json")
        (inner / "a.json").unlink()

        assert _high_secret(run_audit(outer, include_repo_rules=False), "inner/a.json")

    def test_a_nested_repo_ignores_the_outer_hooks_index(self, tmp_path, monkeypatch):
        """The hook's GIT_INDEX_FILE names the outer repo's index; a nested
        repo is read with its own."""
        outer = _repo(tmp_path / "outer")
        inner = _repo(outer / "inner")
        _write(inner, "a.json", _jwk_line())
        _git(inner, "add", "a.json")
        _write(inner, "a.json", "{}\n")
        _write(outer, "x.txt", "x\n")
        _git(outer, "add", "x.txt")
        monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git" / "index"))

        assert _high_secret(run_audit(outer, include_repo_rules=False), "inner/a.json")


@pytest.mark.skipif(os.uname().sysname == "Darwin", reason="APFS needs UTF-8 names")
def test_a_name_that_is_not_utf8_is_audited(tmp_path):
    """Review probe: a non-UTF-8 name crashed the check-ignore call before
    the index pass; it is now checked, and its staged blob found."""
    root = _repo(tmp_path / "repo")
    rel = os.fsdecode(b"bad-\xff.json")
    _write(root, rel, _jwk_line())
    subprocess.run([b"git", b"add", b"--", os.fsencode(rel)], cwd=root, check=True)
    _write(root, rel, "{}\n")
    _write(root, os.fsdecode(b"ignored-\xfe.log"), "x\n")
    _write(root, ".gitignore", "*.log\n")

    report = run_audit(root, include_repo_rules=False)

    assert _high_secret(report, rel)


def test_outside_git_nothing_changes(tmp_path, monkeypatch):
    """No work tree: no git call, no index finding, the same report."""
    root = tmp_path / "plain"
    _write(root, "a.json", _jwk_line())
    _write(root, "sub/repo/b.json", "{}\n")
    _repo(root / "sub" / "repo")
    real_run = engine.subprocess.run
    calls = []

    def recording(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", recording)
    plain = tmp_path / "plain-only"
    _write(plain, "a.json", _jwk_line())

    assert [fd["severity"] for fd in _secrets(run_audit(plain))] == ["high"]
    assert not calls
    report = run_audit(root, include_repo_rules=False)
    assert [fd["path"] for fd in _secrets(report)] == ["a.json"]
    assert not _index_findings(report)


def test_no_audit_git_call_starts_a_configured_fsmonitor(tmp_path):
    root = _repo(tmp_path / "repo")
    marker = tmp_path / "monitor-ran"
    helper = tmp_path / "monitor.sh"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    helper.chmod(0o755)
    _write(root, "a.json", _jwk_line())
    _git(root, "add", "a.json")
    _write(root, "a.json", "{}\n")
    _git(root, "config", "core.fsmonitor", str(helper))

    assert _high_secret(run_audit(root), "a.json")
    assert not marker.exists()


class TestCallerGitBinding:
    """PR #1315 review r4: the binding is what git resolves from the caller's
    own directory, and every git helper in the audit path uses it."""

    @pytest.mark.parametrize("bound", ["relative", "absolute"])
    def test_git_dir_alone_keeps_the_callers_implicit_work_tree(
        self, tmp_path, monkeypatch, capsys, bound
    ):
        """GIT_DIR without GIT_WORK_TREE makes the caller's directory the top
        level, which is also where a relative GIT_INDEX_FILE is resolved.
        Auditing a subdirectory used to read `sub/.git/selected-index`: a
        missing file, so an empty index, so a pass."""
        root = _repo(tmp_path / "repo")
        _write(root, "sub/a.json", _jwk_line())
        _git(root, "add", "sub/a.json")
        _write(root, "sub/a.json", "{}\n")
        (root / ".git" / "selected-index").write_bytes((root / ".git" / "index").read_bytes())
        monkeypatch.chdir(root)
        monkeypatch.setenv("GIT_DIR", ".git" if bound == "relative" else str(root / ".git"))
        monkeypatch.setenv("GIT_INDEX_FILE", ".git/selected-index")
        assert _git(root, "show", ":sub/a.json") == _jwk_line()

        report = run_audit(root / "sub", include_repo_rules=False)

        assert _high_secret(report, "a.json")
        assert cli.main([str(root / "sub"), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_core_worktree_binds_a_git_dir_without_git_work_tree(self, tmp_path, monkeypatch):
        """With GIT_DIR set and no GIT_WORK_TREE, git takes `core.worktree`
        from the repository's config before the caller's directory."""
        root = tmp_path / "plain"
        root.mkdir()
        db = tmp_path / "db.git"
        _git(tmp_path, "init", "-q", "--bare", str(db))
        _git(tmp_path, "--git-dir", str(db), "config", "core.bare", "false")
        _git(tmp_path, "--git-dir", str(db), "config", "core.worktree", str(root))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GIT_DIR", "db.git")
        _write(root, "a.json", _jwk_line())
        _git(tmp_path, "add", str(root / "a.json"))
        _write(root, "a.json", "{}\n")
        assert _git(tmp_path, "show", ":a.json") == _jwk_line()

        assert _high_secret(run_audit(root, include_repo_rules=False), "a.json")

    @pytest.mark.parametrize("value", ["missing-index", "unreadable-index"])
    def test_a_named_index_that_cannot_be_read_fails_closed(
        self, tmp_path, monkeypatch, capsys, value
    ):
        """Git reads a missing GIT_INDEX_FILE as an empty index; the audit
        must not: that would vouch for a commit it never saw."""
        root = _repo(tmp_path / "repo")
        _write(root, "a.json", "{}\n")
        _git(root, "add", "a.json")
        index = root / ".git" / value
        if value == "unreadable-index":
            index.write_bytes((root / ".git" / "index").read_bytes())
            index.chmod(0)
            if os.access(index, os.R_OK):
                pytest.skip("running with privileges that read a mode-000 file")
        monkeypatch.chdir(root)
        monkeypatch.setenv("GIT_INDEX_FILE", str(index))
        try:
            report = run_audit(root, include_repo_rules=False)
            rc = cli.main([str(root), "--fail-on", "high"])
        finally:
            if index.exists():
                index.chmod(0o600)
        capsys.readouterr()

        assert [fd["severity"] for fd in _index_findings(report)] == ["high"]
        assert rc == 1

    def test_a_relative_common_dir_valid_from_a_subdirectory(self, tmp_path, monkeypatch):
        """Review r4 probe: git checks a relative GIT_COMMON_DIR from its
        starting directory and uses it from the top level, so a value that
        names a repository from both works from a subdirectory too."""
        root = _repo(tmp_path / "repo")
        _write(root, "sub/a.json", _jwk_line())
        _git(root, "add", "sub/a.json")
        _write(root, "sub/a.json", "{}\n")
        (root / ".common").symlink_to(".git", target_is_directory=True)
        (root / "sub" / ".common").symlink_to("../.git", target_is_directory=True)
        monkeypatch.chdir(root / "sub")
        monkeypatch.setenv("GIT_COMMON_DIR", ".common")
        assert _git(root / "sub", "show", ":sub/a.json") == _jwk_line()

        for audited, rel in ((root, "sub/a.json"), (root / "sub", "a.json")):
            report = run_audit(audited, include_repo_rules=False)
            assert _high_secret(report, rel) and not _index_findings(report)

    def _shadowed(self, tmp_path, monkeypatch):
        """A real repository bound by a relative GIT_DIR from the parent, plus
        a second valid repository at the path that relative value names when
        read from the audited directory instead (review r4)."""
        root = tmp_path / "plain"
        root.mkdir()
        db = tmp_path / "db.git"
        _git(tmp_path, "init", "-q", "--bare", str(db))
        shadow = root / "db.git"
        _git(root, "init", "-q", "--bare", str(shadow))
        real = {"GIT_DIR": str(db), "GIT_WORK_TREE": str(root)}
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GIT_DIR", "db.git")
        monkeypatch.setenv("GIT_WORK_TREE", str(root))
        return root, db, shadow, real

    @pytest.mark.parametrize("shadow_state", ["untracked", "local-remote"])
    def test_mcp_tracking_and_remotes_read_the_callers_repository(
        self, tmp_path, monkeypatch, capsys, shadow_state
    ):
        """Review r4 repro: `_git_file_tracked` and `_git_has_network_remote`
        ran with the unresolved variables, read the shadow repository, and
        dropped (untracked there) or downgraded (only a local remote there)
        a tracked MCP bearer token."""
        root, db, shadow, real = self._shadowed(tmp_path, monkeypatch)
        token = "".join(chr(65 + (i * 11) % 26) for i in range(40))
        config = {"mcpServers": {"fixture": {"url": "http://localhost/mcp"}}}
        config["mcpServers"]["fixture"]["headers"] = {"Authorization": "Bearer " + token}
        _write(root, ".mcp.json", json.dumps(config))
        env = {**_base_env(), **real}
        _git(root, "add", ".mcp.json", env=env)
        _git(root, "remote", "add", "origin", "https://example.invalid/r.git", env=env)
        if shadow_state == "local-remote":
            shadow_env = {**env, "GIT_DIR": str(shadow)}
            _git(root, "add", ".mcp.json", env=shadow_env)
            _git(root, "remote", "add", "origin", str(tmp_path / "local.git"), env=shadow_env)

        report = run_audit(root)

        mcp = [fd for fd in report["findings"] if fd["rule"] == "AGENT-MCP-SECRETS"]
        assert mcp and {fd["severity"] for fd in mcp} == {"high"}
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_check_ignore_reads_the_callers_repository(self, tmp_path, monkeypatch):
        """A file the shadow repository ignores (its info/exclude) but the
        real one tracks keeps its INJECTION-BASE64 finding."""
        root, db, shadow, real = self._shadowed(tmp_path, monkeypatch)
        (shadow / "info").mkdir(exist_ok=True)
        (shadow / "info" / "exclude").write_text("docs/\n")
        _write(root, "docs/doc1.txt", "Important payload: " + "z" * 70 + "\n")
        _git(root, "add", "docs/doc1.txt", env={**_base_env(), **real})

        report = run_audit(root, include_repo_rules=False)

        assert "INJECTION-BASE64" in {fd["rule"] for fd in report["findings"]}

    def test_every_git_call_carries_the_resolved_binding(self, tmp_path, monkeypatch):
        """The binding rev-parse may carry the caller's relative GIT_DIR.

        That command is exactly the engine's read prefix plus its binding
        arguments. It runs isolated, and, when the process has any GIT_CONFIG*
        key, once more without isolation so those keys are still present.
        Every other git process gets an absolute GIT_DIR and GIT_WORK_TREE.
        """
        root, db, shadow, real = self._shadowed(tmp_path, monkeypatch)
        _write(root, ".mcp.json", json.dumps({"mcpServers": {}}))
        _write(root, "a.json", _jwk_line())
        _write(root, "docs/doc1.txt", "text\n")
        _git(root, "add", ".", env={**_base_env(), **real})
        real_run = engine.subprocess.run
        calls = []

        def recording(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                calls.append((cmd, kwargs.get("env"), kwargs.get("cwd")))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(engine.subprocess, "run", recording)
        run_audit(root)

        binding = [*engine._GIT_READ, *engine._BINDING_ARGS]
        caller_config = [key for key in os.environ if key.startswith("GIT_CONFIG")]
        stripped = [
            key for key in caller_config if key not in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}
        ]
        probes = []
        rest = []
        for call in calls:
            if call[0] == binding and not rest:
                probes.append(call)
            else:
                rest.append(call)
        assert len(probes) == (2 if caller_config else 1)
        for cmd, env, cwd in probes:
            assert cmd == binding and cwd == tmp_path
            assert env.get("GIT_DIR") == "db.git"
            assert env.get("GIT_WORK_TREE") == str(root)
        isolated = probes[0][1]
        assert isolated.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert all(key not in isolated for key in stripped)
        if len(probes) == 2:
            compared = probes[1][1]
            if os.environ.get("GIT_CONFIG_NOSYSTEM") == "1":
                assert compared.get("GIT_CONFIG_NOSYSTEM") == "1"
            else:
                assert compared.get("GIT_CONFIG_NOSYSTEM") != "1"
            if "GIT_CONFIG_GLOBAL" not in os.environ:
                assert "GIT_CONFIG_GLOBAL" not in compared
            assert all(key in compared for key in caller_config)
        assert len(rest) > 5
        for cmd, env, _ in rest:
            assert env is not None, f"inherited the caller's relative values: {cmd[:4]}"
            assert env.get("GIT_DIR") == str(db), cmd[:4]
            assert env.get("GIT_WORK_TREE") == str(root), cmd[:4]


class TestIncompleteIndexCoverageFailsTheGate:
    """PR #1315 review: a git failure, a timeout or the blob cap used to turn
    an unscanned staged secret into a passing gate."""

    @pytest.mark.parametrize(
        "operation",
        [("ls-files", "-s"), ("rev-parse",), ("diff-index",), ("--batch-check",), ("--batch",)],
        ids=["snapshot", "head", "diff-index", "sizes", "blobs"],
    )
    @pytest.mark.parametrize("failure", ["exit", "timeout"])
    def test_git_failure_fails_the_gate(self, tmp_path, monkeypatch, capsys, operation, failure):
        root = _repo(tmp_path / "repo")
        _write(root, "seed.txt", "seed\n")
        _git(root, "add", "seed.txt")
        _git(root, "commit", "-qm", "seed", "--no-verify")
        _write(root, "a.json", _jwk_line())
        _git(root, "add", "a.json")
        _write(root, "a.json", "{}\n")
        real_run = engine.subprocess.run

        def fault(cmd, *args, **kwargs):
            if isinstance(cmd, list) and all(token in cmd for token in operation):
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 10)
                return subprocess.CompletedProcess(cmd, 128, b"", b"synthetic failure")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(engine.subprocess, "run", fault)

        report = run_audit(root, include_repo_rules=False)
        assert report["status"] == "fail"
        assert _high_secret(report, "a.json") or _index_findings(report)
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    @pytest.mark.parametrize("head", ["unborn", "born"])
    def test_more_staged_blobs_than_the_cap_fails_the_gate(self, tmp_path, capsys, head):
        """At the real cap: 5,001 new index entries with no working copies,
        the secret in the last one. What the commit adds (Tier A) must be
        covered in full, so this fails closed."""
        root = _repo(tmp_path / "repo")
        if head == "born":
            _write(root, "seed.txt", "seed\n")
            _git(root, "add", "seed.txt")
            _git(root, "commit", "-qm", "seed", "--no-verify")
        harmless = _git(root, "hash-object", "-w", "--stdin", input_text='{"n": 0}\n').strip()
        secret = _git(root, "hash-object", "-w", "--stdin", input_text=_jwk_line()).strip()
        count = engine.MAX_FILES + 1
        info = "".join(
            f"100644 {secret if i == count - 1 else harmless}\t{i:04}.json\n" for i in range(count)
        )
        _git(root, "update-index", "--index-info", input_text=info)

        report = run_audit(root, include_repo_rules=False)

        assert report["status"] == "fail"
        assert _index_findings(report)
        assert report["truncated"] and report["truncated_by"] == ["files"]
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_a_large_committed_tree_passes_with_a_partial_notice(self, tmp_path, capsys):
        """12,000 committed files, all present: the walk reads 5,000, and the
        other 7,000 are committed content equal to HEAD (Tier B). Only 5,000
        fit the blob budget; the rest are reported, not a gate failure."""
        root = _repo(tmp_path / "repo")
        for i in range(12000):
            path = root / f"d{i // 500:03}" / f"f{i:05}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"n": i}) + "\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "seed", "--no-verify")

        report = run_audit(root, include_repo_rules=False)

        assert not _index_findings(report)
        partial = _partial_findings(report)
        assert [fd["severity"] for fd in partial] == ["medium"]
        assert "2000 committed blobs" in partial[0]["message"]
        assert report["truncated_by"] == ["files"]
        assert report["summary"]["high"] == 0
        assert cli.main([str(root), "--fail-on", "high"]) == 0
        capsys.readouterr()

    @pytest.mark.parametrize("operation", ["--batch-check", "--batch"])
    @pytest.mark.parametrize("failure", ["exit", "timeout"])
    def test_a_git_failure_reading_committed_blobs_is_a_notice(
        self, tmp_path, monkeypatch, capsys, operation, failure
    ):
        """Tier B only (nothing staged; a committed file's working copy is
        gone): a git failure is a medium notice, not a gate failure."""
        root = _repo(tmp_path / "repo")
        _write(root, "old.json", '{"n": 1}\n')
        _git(root, "add", "old.json")
        _git(root, "commit", "-qm", "seed", "--no-verify")
        (root / "old.json").unlink()
        real_run = engine.subprocess.run

        def fault(cmd, *args, **kwargs):
            if isinstance(cmd, list) and operation in cmd:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 10)
                return subprocess.CompletedProcess(cmd, 128, b"", b"synthetic failure")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(engine.subprocess, "run", fault)

        report = run_audit(root, include_repo_rules=False)
        assert not _index_findings(report)
        partial = _partial_findings(report)
        assert [fd["severity"] for fd in partial] == ["medium"]
        assert "1 committed blobs" in partial[0]["message"]
        assert report["truncated"] and report["truncated_by"] == ["files"]
        assert cli.main([str(root), "--fail-on", "high"]) == 0
        capsys.readouterr()

    def test_a_missing_committed_object_is_reported_as_unread(self, tmp_path, capsys):
        """PR #1315 review r3, with real git: a Tier B object gone from the
        object store stops that tree's Tier B pass. The report must say the
        scan is incomplete and how much was left unread, not claim coverage."""
        root = _repo(tmp_path / "repo")
        _write(root, "secret.json", _jwk_line())
        _write(root, "benign.json", "benign unique historical bytes\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "seed", "--no-verify")
        _write(root, "secret.json", "{}\n")
        oid = _git(root, "rev-parse", ":benign.json").strip()
        (root / "benign.json").unlink()
        (root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()

        report = run_audit(root, include_repo_rules=False)

        partial = _partial_findings(report)
        assert [fd["severity"] for fd in partial] == ["medium"]
        assert "2 committed blobs" in partial[0]["message"]
        assert report["truncated"] and report["truncated_by"] == ["files"]
        assert not _index_findings(report)
        assert cli.main([str(root), "--fail-on", "high"]) == 0
        capsys.readouterr()

    def test_an_oversized_hidden_committed_blob_is_reported_as_unread(self, tmp_path, monkeypatch):
        """An oversized committed blob whose working copy was cleaned is
        unread content, not the symmetric size exemption (which applies
        when the working copy is itself over the cap)."""
        root = _repo(tmp_path / "repo")
        _write(root, "big.json", _jwk_line() + "x" * 4096)
        _write(root, "big-present.json", "y" * 4096)
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "seed", "--no-verify")
        _write(root, "big.json", "{}\n")
        monkeypatch.setattr(engine.config, "SEARCH_FILE_MAX_BYTES", 1024)

        report = run_audit(root, include_repo_rules=False)

        partial = _partial_findings(report)
        assert len(partial) == 1 and "1 committed blobs" in partial[0]["message"]
        assert report["truncated_by"] == ["files"]

    def test_the_partial_notice_claims_no_audit_history(self, tmp_path):
        """PR #1315 review r3: no receipt proves committed content was ever
        audited, and CI has the same caps; the notice says only what is known."""
        root = _repo(tmp_path / "repo")
        _write(root, "old.json", '{"n": 1}\n')
        _git(root, "add", "old.json")
        _git(root, "commit", "-qm", "seed", "--no-verify")
        (root / "old.json").unlink()
        original = engine._blob_sizes

        def missing(snapshot, oids):
            raise engine._GitFailure("synthetic failure")

        engine._blob_sizes = missing
        try:
            report = run_audit(root, include_repo_rules=False)
        finally:
            engine._blob_sizes = original

        (fd,) = _partial_findings(report)
        text = (fd["message"] + " " + fd["fix"]).lower()
        assert "audited when committed" not in text
        assert "covers it" not in text and "scan all committed" not in text
        assert "not re-scanned this run" in text
        assert "same" in text and "caps" in text

    def test_index_changed_during_the_audit_fails_closed(self, tmp_path, monkeypatch):
        """PR #1315 review: staging right after an index read must not leave
        a transcript relaxed. The index is read before and after the scan;
        any difference keeps every relaxation off and fails the gate."""
        root = _repo(tmp_path / "repo")
        _write(root, TRANSCRIPT, _session_json("hermes"))
        real_run = engine.subprocess.run
        fired = []

        def stage_after_first_read(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if isinstance(cmd, list) and "ls-files" in cmd and "-s" in cmd and not fired:
                fired.append(True)
                real_run(["git", "add", TRANSCRIPT], cwd=root, check=True, capture_output=True)
                _write(root, TRANSCRIPT, "{}\n")
            return result

        monkeypatch.setattr(engine.subprocess, "run", stage_after_first_read)

        report = run_audit(root, include_repo_rules=False)

        assert fired
        assert report["status"] == "fail"
        assert all(fd["severity"] == "high" for fd in _secrets(report))
        assert _index_findings(report) or _secrets(report)

    def test_credential_store_staged_during_the_audit_fails_closed(self, tmp_path, monkeypatch):
        root = _repo(tmp_path / "repo")
        rel = ".tool/auth.json"
        _write(root, rel, json.dumps({"token": _vendor_key()})).chmod(0o600)
        real_run = engine.subprocess.run
        fired = []

        def stage_after_first_read(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if isinstance(cmd, list) and "ls-files" in cmd and "-s" in cmd and not fired:
                fired.append(True)
                real_run(["git", "add", rel], cwd=root, check=True, capture_output=True)
            return result

        monkeypatch.setattr(engine.subprocess, "run", stage_after_first_read)

        report = run_audit(root, include_repo_rules=False)

        assert fired and report["status"] == "fail"
        assert [fd["path"] for fd in _secrets(report)] == [rel]


class TestRelaxationIsRecheckedAfterTheScan:
    def _stage_after_check(self, monkeypatch, root, rel, then=None):
        original = engine._git_file_tracked
        calls = []

        def stage_after_read(directory, name):
            tracked = original(directory, name)
            calls.append(tracked)
            _git(root, "add", rel)
            if then:
                then()
            return tracked

        monkeypatch.setattr(engine, "_git_file_tracked", stage_after_read)
        return calls

    def test_untracked_transcript_in_a_repo_is_still_relaxed(self, tmp_path):
        root = _repo(tmp_path / "repo")
        _write(root, TRANSCRIPT, _session_json("hermes"))

        assert {fd["severity"] for fd in _secrets(run_audit(root))} == {"low"}

    def test_transcript_staged_after_the_tracking_check_keeps_full_severity(
        self, tmp_path, monkeypatch
    ):
        """Reviewer repro: `git add` lands right after the untracked answer."""
        root = _repo(tmp_path / "repo")
        _write(root, TRANSCRIPT, _session_json("hermes"))
        calls = self._stage_after_check(monkeypatch, root, TRANSCRIPT)

        report = run_audit(root, include_repo_rules=False)

        assert calls == [False], "the relaxing answer was given"
        assert _git(root, "ls-files", TRANSCRIPT).strip() == TRANSCRIPT
        found = _secrets(report)
        assert found and {fd["severity"] for fd in found} == {"high"}
        assert all("transcript" not in fd["message"] for fd in found)

    def test_transcript_staged_then_cleaned_mid_audit_reports_the_staged_blob(
        self, tmp_path, monkeypatch
    ):
        root = _repo(tmp_path / "repo")
        _write(root, TRANSCRIPT, _session_json("hermes"))
        self._stage_after_check(
            monkeypatch, root, TRANSCRIPT, then=lambda: _write(root, TRANSCRIPT, "{}\n")
        )

        found = _secrets(run_audit(root, include_repo_rules=False))

        assert found and {fd["severity"] for fd in found} == {"high"}

    def test_an_index_unreadable_at_the_recheck_keeps_full_severity(self, tmp_path, monkeypatch):
        root = _repo(tmp_path / "repo")
        _write(root, TRANSCRIPT, _session_json("hermes"))
        original = engine._git_file_tracked

        def corrupt_after_read(directory, name):
            tracked = original(directory, name)
            (root / ".git" / "index").write_bytes(b"not an index")
            return tracked

        monkeypatch.setattr(engine, "_git_file_tracked", corrupt_after_read)

        found = _secrets(run_audit(root, include_repo_rules=False))
        assert found and {fd["severity"] for fd in found} == {"high"}

    def test_credential_store_staged_after_the_tracking_check_reports_the_key(
        self, tmp_path, monkeypatch
    ):
        root = _repo(tmp_path / "repo")
        rel = ".tool/auth.json"
        path = _write(root, rel, json.dumps({"token": _vendor_key()}))
        path.chmod(0o600)
        assert not _secrets(run_audit(root, include_repo_rules=False)), "untracked: a store"
        self._stage_after_check(monkeypatch, root, rel)

        found = _secrets(run_audit(root, include_repo_rules=False))

        assert [fd["path"] for fd in found] == [rel]
        assert found[0]["severity"] == "high"


def _inject_config(monkeypatch, mode, pairs):
    """Transient caller config, as `git -c` or GIT_CONFIG_COUNT pass it."""
    if mode == "parameters":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", " ".join(f"'{k}={v}'" for k, v in pairs))
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(pairs)))
        for i, (key, value) in enumerate(pairs):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)


def _mcp_repo(root, rel=".mcp.json"):
    token = "".join(chr(65 + (i * 11) % 26) for i in range(40))
    config = {"mcpServers": {"fixture": {"url": "http://localhost/mcp"}}}
    config["mcpServers"]["fixture"]["headers"] = {"Authorization": "Bearer " + token}
    text = json.dumps(config)
    _write(root, rel, text)
    _git(root, "add", rel)
    _git(root, "remote", "add", "origin", "https://example.invalid/review.git")
    return text


def _mcp_high(report):
    return [
        fd
        for fd in report["findings"]
        if fd["rule"] == "AGENT-MCP-SECRETS" and fd["severity"] == "high"
    ]


class TestReviewRound5:
    """PR #1315 review r5: caller-controlled git state and content sources."""

    def test_a_persisted_core_worktree_is_the_work_tree(self, tmp_path, monkeypatch, capsys):
        """`core.worktree` in the repository's own config places the work tree
        below the `.git` marker's directory. The marker's directory gave the
        wrong pathspec, so no staged entry was read."""
        root = _repo(tmp_path / "repo")
        work = root / "work"
        work.mkdir()
        _git(root, "config", "core.worktree", str(work))
        monkeypatch.chdir(work)
        _write(work, "a.json", _jwk_line())
        _git(work, "add", "a.json")
        _write(work, "a.json", "{}\n")
        assert _git(work, "rev-parse", "--show-toplevel").strip() == str(work)
        assert _git(work, "show", ":a.json") == _jwk_line()

        assert _high_secret(run_audit(work, include_repo_rules=False), "a.json")
        assert cli.main([str(work), "--fail-on", "high"]) == 1
        capsys.readouterr()

    @pytest.mark.parametrize("where", ["root", "gitdir"])
    def test_a_newline_in_a_bound_path_fails_closed(self, tmp_path, monkeypatch, capsys, where):
        """A newline in the work tree or git dir path shifted the fields of the
        binding answer, and the audit fell back to a clean `.git` index in the
        same directory. A path git cannot hand over unambiguously is refused."""
        root = _repo(tmp_path / ("repo\npart" if where == "root" else "repo"))
        _write(root, "a.json", "{}\n")
        _git(root, "add", "a.json")
        db = tmp_path / ("db\npart.git" if where == "gitdir" else "db.git")
        _git(tmp_path, "init", "-q", "--bare", str(db))
        monkeypatch.setenv("GIT_DIR", str(db))
        monkeypatch.setenv("GIT_WORK_TREE", str(root))
        _write(root, "a.json", _jwk_line())
        _git(root, "add", "a.json")
        _write(root, "a.json", "{}\n")
        monkeypatch.chdir(root)
        assert _git(root, "show", ":a.json") == _jwk_line()

        report = run_audit(root, include_repo_rules=False)

        assert report["summary"]["high"] > 0
        assert _index_findings(report) or _high_secret(report, "a.json")
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    @pytest.mark.parametrize("binding", ["dir", "dir-worktree", "bare-dir"])
    def test_an_explicit_binding_to_another_repository_fails_closed(
        self, tmp_path, monkeypatch, capsys, binding
    ):
        """Review r5 scope assertions: the caller's GIT_DIR (and GIT_WORK_TREE)
        bind repository A while the audit is asked about a clean repository B.
        B's own index says nothing about A's pending commit, so the run must
        not pass as if it did."""
        repo_a = _repo(tmp_path / "a")
        _write(repo_a, "a.json", _jwk_line())
        _git(repo_a, "add", "a.json")
        _write(repo_a, "a.json", "{}\n")
        clean = _repo(tmp_path / "clean")
        _write(clean, "a.json", "{}\n")
        _git(clean, "add", "a.json")
        monkeypatch.chdir(repo_a)
        monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
        if binding == "dir-worktree":
            monkeypatch.setenv("GIT_WORK_TREE", str(repo_a))
        if binding == "bare-dir":
            _git(repo_a, "config", "core.bare", "true")

        report = run_audit(clean, include_repo_rules=False)

        assert [fd["severity"] for fd in _index_findings(report)] == ["high"]
        assert cli.main([str(clean), "--fail-on", "high"]) == 1
        capsys.readouterr()

    @pytest.mark.parametrize("mode", ["parameters", "count"])
    @pytest.mark.parametrize("binding", ["none", "index", "dir-worktree"])
    def test_transient_url_rewrites_cannot_downgrade_an_mcp_secret(
        self, tmp_path, monkeypatch, capsys, mode, binding
    ):
        """A caller-supplied `url.<x>.insteadOf` made the HTTPS remote look
        local, so a tracked MCP bearer token fell from high to info."""
        root = _repo(tmp_path / "repo")
        text = _mcp_repo(root)
        monkeypatch.chdir(root)
        assert _mcp_high(run_audit(root))
        if binding == "index":
            monkeypatch.setenv("GIT_INDEX_FILE", str(root / ".git" / "index"))
        if binding == "dir-worktree":
            monkeypatch.setenv("GIT_DIR", str(root / ".git"))
            monkeypatch.setenv("GIT_WORK_TREE", str(root))
        rewrite = ("url.file:///tmp/review-only/.insteadOf", "https://example.invalid/")
        _inject_config(monkeypatch, mode, [rewrite])
        assert "file:///tmp/review-only/" in _git(root, "remote", "-v")
        assert _git(root, "show", ":.mcp.json") == text

        assert _mcp_high(run_audit(root))
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    @pytest.mark.parametrize("source", ["repository", "global", "count", "parameters"])
    @pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
    def test_a_network_enabling_rewrite_keeps_full_severity(
        self, tmp_path, monkeypatch, capsys, key, source
    ):
        """A local raw remote that config rewrites to HTTPS is pushed over the
        network. Review r6: isolation hid a global or transient rewrite, so
        the tracked MCP token fell to info. The local-only downgrade now needs
        both the isolated and the caller's own view of the remotes to be local
        (the repository's own rewrite was always seen)."""
        root = _repo(tmp_path / "repo")
        _mcp_repo(root)
        _git(root, "remote", "set-url", "origin", "file://" + str(tmp_path / "local.git"))
        rewrite = (f"url.https://example.invalid/.{key}", f"file://{tmp_path}/")
        if source == "repository":
            _git(root, "config", *rewrite)
        elif source == "global":
            config = tmp_path / "global.gitconfig"
            _git(root, "config", "--file", str(config), *rewrite)
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        else:
            _inject_config(monkeypatch, source, [rewrite])
        url = _git(root, "remote", "get-url", "--push", "origin").strip()
        assert url.startswith("https://example.invalid/")
        monkeypatch.chdir(root)

        assert _mcp_high(run_audit(root))
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_a_local_only_remote_still_downgrades(self, tmp_path, monkeypatch):
        """Control: the documented downgrade for a repository whose raw remote
        URLs are all local paths is unchanged."""
        root = _repo(tmp_path / "repo")
        _mcp_repo(root)
        _git(root, "remote", "set-url", "origin", str(tmp_path / "local.git"))
        monkeypatch.chdir(root)

        mcp = [fd for fd in run_audit(root)["findings"] if fd["rule"] == "AGENT-MCP-SECRETS"]
        assert mcp and {fd["severity"] for fd in mcp} == {"info"}

    def test_every_git_call_is_isolated_from_caller_config(self, tmp_path, monkeypatch):
        """One isolated environment for every git process the audit starts:
        no system or global config, and no transient `-c` parameters."""
        root = _repo(tmp_path / "repo")
        _mcp_repo(root)
        _write(root, "docs/doc1.txt", "text\n")
        _git(root, "add", ".")
        monkeypatch.chdir(root)
        _inject_config(monkeypatch, "count", [("core.excludesFile", "/nonexistent")])
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.abbrev=12'")
        real_run = engine.subprocess.run
        envs = []

        def recording(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                envs.append((cmd, kwargs.get("env")))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(engine.subprocess, "run", recording)
        run_audit(root)

        # Two deliberate exceptions, each only able to make a result stricter:
        # the binding query repeated with transient config (fails closed if the
        # answer changes), and the caller's view of the remotes, which must
        # also be local-only before an MCP finding is downgraded (review r6).
        compare = [
            (cmd, env)
            for cmd, env in envs
            if env is not None and env.get("GIT_CONFIG_NOSYSTEM") != "1"
        ]
        kinds = sorted("remote" if "remote" in cmd else "binding" for cmd, _ in compare)
        assert kinds == ["binding", "remote"], [cmd[:8] for cmd, _ in compare]
        assert all("--path-format=absolute" in cmd for cmd, _ in compare if "remote" not in cmd)
        envs = [item for item in envs if item not in compare]
        assert len(envs) > 5
        for cmd, env in envs:
            assert env is not None, cmd[:5]
            assert env.get("GIT_CONFIG_NOSYSTEM") == "1", cmd[:5]
            assert env.get("GIT_CONFIG_GLOBAL") == os.devnull, cmd[:5]
            leaked = [
                k
                for k in env
                if k.startswith("GIT_CONFIG_")
                and k not in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL")
            ]
            assert not leaked and "GIT_CONFIG" not in env, (cmd[:5], leaked)

    @pytest.mark.parametrize("hidden", ["cleaned", "deleted"])
    def test_a_new_oversized_blob_that_was_not_read_fails_closed(
        self, tmp_path, monkeypatch, capsys, hidden
    ):
        """A new staged blob over the size cap, with a working copy that is
        not the same bytes, was skipped silently. It is unread new content."""
        root = _repo(tmp_path / "repo")
        monkeypatch.setattr(engine.config, "SEARCH_FILE_MAX_BYTES", 1024)
        path = _write(root, "a.json", _jwk_line() + " " * 2048)
        _git(root, "add", "a.json")
        if hidden == "cleaned":
            path.write_text("{}\n")
        else:
            path.unlink()

        report = run_audit(root, include_repo_rules=False)

        assert [fd["severity"] for fd in _index_findings(report)] == ["high"]
        assert report["truncated"] and report["truncated_by"] == ["files"]

    def test_a_new_oversized_blob_matching_its_working_copy_is_exempt(self, tmp_path, monkeypatch):
        """Control: the same bytes in the working copy fall under the size
        exemption every file over the cap has."""
        root = _repo(tmp_path / "repo")
        monkeypatch.setattr(engine.config, "SEARCH_FILE_MAX_BYTES", 1024)
        _write(root, "a.json", _jwk_line() + " " * 2048)
        _git(root, "add", "a.json")

        report = run_audit(root, include_repo_rules=False)

        assert report["status"] == "pass" and not report["truncated"]

    @pytest.mark.parametrize("hidden", ["cleaned", "deleted"])
    def test_the_mcp_rule_reads_the_staged_blob(self, tmp_path, monkeypatch, capsys, hidden):
        """The MCP semantic credential rule read only the working copy, so a
        cleaned or deleted `.mcp.json` hid a staged bearer token."""
        root = _repo(tmp_path / "repo")
        text = _mcp_repo(root)
        monkeypatch.chdir(root)
        assert _mcp_high(run_audit(root))
        if hidden == "cleaned":
            _write(root, ".mcp.json", '{"mcpServers": {}}')
        else:
            (root / ".mcp.json").unlink()
        assert _git(root, "show", ":.mcp.json") == text

        report = run_audit(root)

        assert _mcp_high(report)
        assert all(STAGED_NOTE in fd["message"] for fd in _mcp_high(report))
        assert cli.main([str(root), "--fail-on", "high"]) == 1
        capsys.readouterr()

    def test_the_agent_settings_rule_reads_the_staged_blob(self, tmp_path, monkeypatch):
        """The other content-reading repo rule, on the same footing."""
        root = _repo(tmp_path / "repo")
        _write(root, ".claude/settings.json", json.dumps({"defaultMode": "bypassPermissions"}))
        _git(root, "add", ".claude/settings.json")
        _write(root, ".claude/settings.json", "{}")
        monkeypatch.chdir(root)

        found = [
            fd
            for fd in run_audit(root)["findings"]
            if fd["rule"] == "AGENT-PERMISSIVE-CONFIG" and fd["severity"] == "high"
        ]
        assert found and all(STAGED_NOTE in fd["message"] for fd in found)


class TestNestedMcpConfig:
    @pytest.mark.parametrize("outer_repo", [False, True])
    @pytest.mark.parametrize(
        ("remote", "severity"),
        [("network", "high"), ("local", "info"), ("none", "high"), ("error", "high")],
    )
    def test_nested_config_uses_its_own_remote(
        self, tmp_path, monkeypatch, outer_repo, remote, severity
    ):
        root = tmp_path / "root"
        root.mkdir()
        if outer_repo:
            _repo(root)
            _git(root, "remote", "add", "origin", str(tmp_path / "outer.git"))
        nested = _repo(root / "nested")
        _mcp_repo(nested)
        if remote in {"local", "error"}:
            _git(nested, "remote", "set-url", "origin", str(tmp_path / "local.git"))
        elif remote == "none":
            _git(nested, "remote", "remove", "origin")
        if remote == "error":
            real_run = engine.subprocess.run

            def failed_remote(cmd, *args, **kwargs):
                if "remote" in cmd and kwargs.get("cwd") == nested:
                    return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="unavailable")
                return real_run(cmd, *args, **kwargs)

            monkeypatch.setattr(engine.subprocess, "run", failed_remote)
        report = run_audit(root)
        found = [fd for fd in report["findings"] if fd["rule"] == "AGENT-MCP-SECRETS"]
        assert [(fd["path"], fd["severity"]) for fd in found] == [("nested/.mcp.json", severity)]
        assert report["status"] == ("fail" if severity == "high" else "pass")

    @pytest.mark.parametrize("hidden", ["cleaned", "deleted", "committed-deleted"])
    def test_hidden_nested_config_is_audited(self, tmp_path, hidden):
        root = _repo(tmp_path / "root")
        nested = _repo(root / "nested")
        _mcp_repo(nested)
        if hidden == "committed-deleted":
            _git(nested, "commit", "-qm", "fixture")
        if hidden == "cleaned":
            _write(nested, ".mcp.json", "{}")
        else:
            (nested / ".mcp.json").unlink()
        found = _mcp_high(run_audit(root))
        assert [fd["path"] for fd in found] == ["nested/.mcp.json"]
        assert STAGED_NOTE in found[0]["message"]

    @pytest.mark.parametrize("mode", ["count", "parameters"])
    @pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
    @pytest.mark.parametrize("network_enabling", [True, False])
    def test_nested_remote_keeps_transient_config(
        self, tmp_path, monkeypatch, mode, key, network_enabling
    ):
        root = _repo(tmp_path / "root")
        _write(root, "readme.txt", "fixture")
        _git(root, "add", ".")
        _git(root, "remote", "add", "origin", str(tmp_path / "outer.git"))
        nested = _repo(root / "nested")
        _mcp_repo(nested)
        local = f"file://{tmp_path}/"
        network = "https://example.invalid/"
        if network_enabling:
            _git(nested, "remote", "set-url", "origin", local + "nested.git")
        target, source = (network, local) if network_enabling else (local, network)
        _inject_config(monkeypatch, mode, [(f"url.{target}.{key}", source)])
        # A hook's explicit outer binding must not leak into the nested check.
        monkeypatch.setenv("GIT_DIR", str(root / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(root))
        monkeypatch.setenv("GIT_INDEX_FILE", str(root / ".git/index"))
        monkeypatch.chdir(root)
        found = _mcp_high(run_audit(root))
        assert [fd["path"] for fd in found] == ["nested/.mcp.json"]

    @pytest.mark.parametrize("skip", ["excluded", "repo-rules-disabled", "untracked"])
    def test_nested_config_respects_scope(self, tmp_path, skip):
        root = _repo(tmp_path / "root")
        nested = _repo(root / "nested")
        _mcp_repo(nested)
        if skip == "untracked":
            _git(nested, "rm", "--cached", ".mcp.json")
        report = run_audit(
            root,
            excludes=["nested/.mcp.json"] if skip == "excluded" else None,
            include_repo_rules=skip != "repo-rules-disabled",
        )
        assert not _mcp_high(report)

    @pytest.mark.parametrize("hidden", [False, True])
    def test_a_deeper_repo_is_judged_by_its_own_index(self, tmp_path, hidden):
        """#2263: root > a > a/config, each committing an MCP config with a
        bearer. `a/config/mcp_config.json` sits at a's `config/mcp_config.json`
        path, so a's check read it and asked a's index, which calls it
        untracked: the committed credential in the deepest repo went unreported."""
        root = _repo(tmp_path / "root")
        _mcp_repo(root)
        outer = _repo(root / "a")
        _mcp_repo(outer)
        inner = _repo(outer / "config")
        _mcp_repo(inner, "mcp_config.json")
        if hidden:
            (inner / "mcp_config.json").unlink()
        found = sorted(fd["path"] for fd in _mcp_high(run_audit(root)))
        assert found == [".mcp.json", "a/.mcp.json", "a/config/mcp_config.json"]
