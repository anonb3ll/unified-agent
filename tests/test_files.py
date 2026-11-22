"""Behavioral tests for unified_agent.tools.files.

Covers fs_read, fs_write, fs_list, fs_search. All paths are resolved through
the workspace root (UA_WORKSPACE), which conftest.py points at a tmp_path per
test. Limits exercised at documented values — do not relax to make tests
easier:

- fs_read: 2000 lines default, max 5000; 1 MB file cap (READ_MAX_BYTES).
- fs_write: 2 MB per call (WRITE_MAX_BYTES); append mode adds, does not
  overwrite; creates parent dirs.
- fs_list: max_entries 500 default, max 2000; sorts entries by name.
- fs_search: skips binaries (\\x00 in first 8 KB), skips files >1 MB
  (SEARCH_FILE_MAX_BYTES), respects max_results.
"""

from __future__ import annotations

import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools import files
from unified_agent.tools.approvals import ApprovalPendingError, approval_pending

# --- fs_read --------------------------------------------------------------


class TestFsRead:
    def test_reads_existing_file(self, isolated_env):
        (isolated_env / "hello.txt").write_text("alpha\nbeta\ngamma\n")
        out = files.fs_read("hello.txt")
        assert out["path"] == "hello.txt"
        assert out["content"] == "alpha\nbeta\ngamma"
        assert out["total_lines"] == 3
        assert out["truncated"] is False

    def test_offset_and_limit_paging(self, isolated_env):
        # 10 lines, read offset=3 limit=4 → lines 4..7
        (isolated_env / "ten.txt").write_text("\n".join(f"line{i}" for i in range(10)))
        out = files.fs_read("ten.txt", offset=3, limit=4)
        assert out["content"] == "line3\nline4\nline5\nline6"
        assert out["truncated"] is True  # 7..9 still unread

    def test_limit_clamped_to_5000(self, isolated_env):
        (isolated_env / "x.txt").write_text("a\nb\nc\n")
        out = files.fs_read("x.txt", limit=99_999)
        # limit was clamped to 5000; since the file has 3 lines, no truncation
        assert out["total_lines"] == 3
        assert out["truncated"] is False

    def test_limit_clamped_to_at_least_1(self, isolated_env):
        (isolated_env / "x.txt").write_text("a\nb\nc\n")
        out = files.fs_read("x.txt", offset=0, limit=0)
        # limit=0 → clamped to 1 → only "a" returned
        assert out["content"] == "a"
        assert out["total_lines"] == 3
        assert out["truncated"] is True

    def test_missing_file_refused(self, isolated_env):
        with pytest.raises(ToolError, match="no such file"):
            files.fs_read("does_not_exist.txt")

    def test_directory_refused(self, isolated_env):
        (isolated_env / "subdir").mkdir()
        with pytest.raises(ToolError, match="is a directory"):
            files.fs_read("subdir")

    def test_oversize_file_refused(self, isolated_env):
        # Synthesize a file just over the cap, without writing >1 MB of data.
        big = isolated_env / "big.bin"
        # Truncate to just over the cap (READ_MAX_BYTES + 1)
        with open(big, "wb") as f:
            f.seek(config.READ_MAX_BYTES)
            f.write(b"\x00")
        with pytest.raises(ToolError, match="larger than"):
            files.fs_read("big.bin")


# --- fs_write -------------------------------------------------------------


class TestFsWrite:
    def test_writes_new_file(self, isolated_env):
        out = files.fs_write("created.txt", "hello")
        assert out["path"] == "created.txt"
        assert out["bytes_written"] == 5
        assert (isolated_env / "created.txt").read_text() == "hello"

    def test_overwrite_by_default(self, isolated_env):
        (isolated_env / "f.txt").write_text("old content longer than new")
        files.fs_write("f.txt", "new")
        assert (isolated_env / "f.txt").read_text() == "new"

    def test_append_mode_preserves_existing(self, isolated_env):
        (isolated_env / "log.txt").write_text("line1\n")
        files.fs_write("log.txt", "line2\n", append=True)
        assert (isolated_env / "log.txt").read_text() == "line1\nline2\n"

    def test_creates_parent_directories(self, isolated_env):
        files.fs_write("a/b/c/deep.txt", "buried")
        assert (isolated_env / "a/b/c/deep.txt").read_text() == "buried"

    def test_content_2mb_cap(self, isolated_env):
        huge = "x" * (config.WRITE_MAX_BYTES + 1)
        with pytest.raises(ToolError, match="exceeds"):
            files.fs_write("huge.txt", huge)

    def test_refuses_imessage_bridge_tree(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_BRIDGE_DIR", raising=False)
        with pytest.raises(ToolError, match="iMessage bridge"):
            files.fs_write("projects/imessage-bridge/src/send.ts", "evil")
        with pytest.raises(ToolError, match="iMessage bridge"):
            files.fs_write("projects/imessage-bridge/.env", "SECRET=1", append=True)
        assert not (isolated_env / "projects/imessage-bridge/src/send.ts").exists()
        files.fs_write("projects/other/file.txt", "ok")
        assert (isolated_env / "projects/other/file.txt").read_text() == "ok"

        bridge = isolated_env / "custom-bridge"
        monkeypatch.setenv("UA_BRIDGE_DIR", str(bridge))
        with pytest.raises(ToolError, match="iMessage bridge"):
            files.fs_write("custom-bridge/src/send.ts", "evil")

    def test_refuses_git_hook_path(self, isolated_env):
        """Regression test for the fs_write gate-bypass fix (safety.py's hook-dir
        denylist) -- pinned at the actual tool boundary, not just the resolve_in_workspace()
        helper, so a future refactor that reorders path resolution can't silently reopen it."""
        with pytest.raises(ToolError, match="hook-execution directory"):
            files.fs_write(".git/hooks/post-commit", "#!/bin/sh\necho pwned\n")

    def test_refuses_configured_git_hook_path(self, isolated_env):
        subprocess.run(["git", "init", "-q", str(isolated_env)], check=True)
        subprocess.run(
            ["git", "-C", str(isolated_env), "config", "core.hooksPath", "automation"],
            check=True,
        )

        with pytest.raises(ToolError, match="configured Git hooks directory"):
            files.fs_write("automation/pre-commit", "#!/bin/sh\necho pwned\n")


# --- fs_list --------------------------------------------------------------


class TestFsList:
    def test_lists_directory(self, isolated_env):
        (isolated_env / "a.txt").write_text("x")
        (isolated_env / "b.txt").write_text("y")
        (isolated_env / "sub").mkdir()
        out = files.fs_list(".")
        names = [e["name"] for e in out["entries"]]
        assert "a.txt" in names and "b.txt" in names and "sub" in names
        # sub is a dir
        sub_entry = next(e for e in out["entries"] if e["name"] == "sub")
        assert sub_entry["type"] == "dir"
        # files have a size, dirs do not
        a_entry = next(e for e in out["entries"] if e["name"] == "a.txt")
        assert a_entry["type"] == "file"
        assert a_entry["size"] == 1

    def test_glob_filter(self, isolated_env):
        (isolated_env / "a.py").write_text("")
        (isolated_env / "b.txt").write_text("")
        out = files.fs_list(".", glob="*.py")
        names = [e["name"] for e in out["entries"]]
        assert names == ["a.py"]

    def test_max_entries_clamped_to_2000(self, isolated_env):
        # We don't actually create 2001 files (slow + noisy). Just verify the
        # cap is applied: max_entries=99999 → 2000, max_entries=0 → 1.
        (isolated_env / "a.txt").write_text("x")
        out = files.fs_list(".", max_entries=99_999)
        # 1 entry created; the cap is much higher so we still see 1
        assert len(out["entries"]) == 1
        # Cap on min side
        out2 = files.fs_list(".", max_entries=0)
        assert len(out2["entries"]) == 1  # clamped to 1

    def test_not_a_directory_refused(self, isolated_env):
        (isolated_env / "f.txt").write_text("x")
        with pytest.raises(ToolError, match="not a directory"):
            files.fs_list("f.txt")

    def test_sorted_by_name(self, isolated_env):
        for n in ["c", "a", "b"]:
            (isolated_env / n).write_text("x")
        out = files.fs_list(".")
        assert [e["name"] for e in out["entries"]] == ["a", "b", "c"]

    @pytest.mark.parametrize("glob", ["../*", "sub/../../*", "/etc/*"])
    def test_glob_traversal_refused(self, isolated_env, glob):
        (isolated_env / "sub").mkdir()
        with pytest.raises(ToolError, match="glob"):
            files.fs_list(".", glob=glob)

    def test_glob_entries_pass_read_guards(self, isolated_env, tmp_path):
        """A glob must not list what fs_read refuses: hook dirs, secrets, escapes."""
        (isolated_env / ".git").mkdir()
        (isolated_env / ".git" / "config").write_text("x")
        (isolated_env / "service.env").write_text("x")
        (isolated_env / "a.py").write_text("x")
        outside = tmp_path / "outside"
        outside.mkdir()
        (isolated_env / "escape").symlink_to(outside)
        assert [e["name"] for e in files.fs_list(".", glob="*")["entries"]] == ["a.py"]
        assert files.fs_list(".", glob=".git/*")["entries"] == []


# --- fs_search ------------------------------------------------------------


class TestFsSearch:
    def test_finds_match_with_line_numbers(self, isolated_env):
        (isolated_env / "a.py").write_text("def foo():\n    return 1\n")
        (isolated_env / "b.py").write_text("def bar():\n    return 2\n")
        out = files.fs_search(r"def \w+", path=".")
        paths = {m["path"] for m in out["matches"]}
        assert paths == {"a.py", "b.py"}
        # line_no is 1-indexed
        a_match = next(m for m in out["matches"] if m["path"] == "a.py")
        assert a_match["line_no"] == 1
        assert "def foo" in a_match["line"]

    def test_invalid_regex_refused(self, isolated_env):
        with pytest.raises(ToolError, match="invalid regex"):
            files.fs_search("(unclosed")

    def test_skips_binary_files(self, isolated_env):
        (isolated_env / "bin.dat").write_bytes(b"\x00\x01\x02\x03binary-ish")
        (isolated_env / "ok.txt").write_text("binary-ish also here\n")
        out = files.fs_search(r"binary-ish", path=".")
        paths = {m["path"] for m in out["matches"]}
        assert "ok.txt" in paths
        assert "bin.dat" not in paths

    def test_skips_oversize_files(self, isolated_env):
        # Create a file over SEARCH_FILE_MAX_BYTES; we don't need to write that
        # much — sparse file via seek is enough.
        big = isolated_env / "big.log"
        with open(big, "wb") as f:
            f.seek(config.SEARCH_FILE_MAX_BYTES)
            f.write(b"\nneedle\n")
        (isolated_env / "small.log").write_text("needle in a small file\n")
        out = files.fs_search(r"needle", path=".")
        paths = {m["path"] for m in out["matches"]}
        assert "small.log" in paths
        assert "big.log" not in paths

    def test_glob_filter(self, isolated_env):
        (isolated_env / "a.py").write_text("marker\n")
        (isolated_env / "b.txt").write_text("marker\n")
        out = files.fs_search(r"marker", path=".", glob="*.py")
        paths = {m["path"] for m in out["matches"]}
        assert paths == {"a.py"}

    def test_max_results_clamped_to_1000(self, isolated_env):
        # We don't make 1001 matches; just verify clamp behavior with
        # max_results=99_999 (clamped down) and max_results=0 (clamped up to 1).
        (isolated_env / "a.txt").write_text("x\n" * 10)
        out = files.fs_search(r"x", path=".", max_results=99_999)
        # cap is 1000; we have 10 matches → all returned, no truncation
        assert len(out["matches"]) == 10
        assert out["truncated"] is False

    def test_truncation_flag(self, isolated_env):
        # 5 matches, ask for 2 → truncated
        (isolated_env / "a.txt").write_text("\n".join(f"hit{i}" for i in range(5)))
        out = files.fs_search(r"hit\d+", path=".", max_results=2)
        assert len(out["matches"]) == 2
        assert out["truncated"] is True


class TestApprovalGate:
    """check_approval_gate wiring on fs_write (task #115, reconciling design-doc T5)."""

    def test_ungated_by_default(self, isolated_env, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        out = files.fs_write("note.txt", "hello")
        assert out == {"path": "note.txt", "bytes_written": 5}

    def test_gated_when_listed_writes_nothing(self, isolated_env, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
        with pytest.raises(ApprovalPendingError, match="queued"):
            files.fs_write("note.txt", "hello")
        assert not (isolated_env / "note.txt").exists()
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        assert pending[0]["tool"] == "fs_write"
        assert pending[0]["payload"]["path"] == "note.txt"
        # raw content never enters the queued payload — only a hash + size
        assert "content" not in pending[0]["payload"]
        assert pending[0]["payload"]["bytes"] == 5
