import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.approvals import ApprovalPendingError, approval_pending
from unified_agent.tools.git import git_commit_staged, git_diff_staged

pytestmark = pytest.mark.smoke


@pytest.fixture
def mock_git_repo(isolated_env, monkeypatch):
    """Create a git repository inside the isolated workspace and configure basic user details."""
    repo_dir = isolated_env / "test_git_repo"
    repo_dir.mkdir()

    # Initialize repository
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True
    )

    # We must mock resolve_in_workspace so that it returns this temporary directory
    # instead of the real workspace.
    import unified_agent.tools.git as git_tool

    monkeypatch.setattr(git_tool, "resolve_in_workspace", lambda path, use_worktree=True: repo_dir)

    return repo_dir


def test_git_diff_and_commit_flow(mock_git_repo):
    # 1. Initially repository should be clean
    res = git_diff_staged()
    assert res["is_clean"] is True
    assert res["staged_files"] == []
    assert res["diff_text"] == ""

    # 2. Create and stage a file
    test_file = mock_git_repo / "dummy.txt"
    test_file.write_text("line 1\nline 2\n")

    # Stage it
    subprocess.run(["git", "add", "dummy.txt"], cwd=mock_git_repo, capture_output=True)

    # Check staged diff
    res_diff = git_diff_staged()
    assert res_diff["is_clean"] is False
    assert len(res_diff["staged_files"]) == 1
    assert res_diff["staged_files"][0]["file"] == "dummy.txt"
    assert res_diff["staged_files"][0]["status"] == "A"
    assert "dummy.txt" in res_diff["diff_text"]
    assert "+line 1" in res_diff["diff_text"]

    # 3. Test git commit validation
    # Non-conforming message should fail
    with pytest.raises(ToolError, match="does not match Conventional Commits"):
        git_commit_staged("fixed dummy.txt")

    # Too long message should fail
    with pytest.raises(ToolError, match="does not match Conventional Commits"):
        git_commit_staged("feat: " + ("a" * 150))

    # Conforming message should succeed
    res_commit = git_commit_staged("feat: add dummy file for testing")
    assert res_commit["commit_hash"] != ""
    assert res_commit["summary"] == "feat: add dummy file for testing"

    # After commit, diff should be clean again
    res_after = git_diff_staged()
    assert res_after["is_clean"] is True


def test_commit_message_with_body(mock_git_repo):
    test_file = mock_git_repo / "body.txt"
    test_file.write_text("content\n")
    subprocess.run(["git", "add", "body.txt"], cwd=mock_git_repo, capture_output=True)

    # Body without the required blank line after the header must fail
    with pytest.raises(ToolError, match="blank line"):
        git_commit_staged("feat: add body file\nexplaining the change")

    # Header + blank line + body conforms to Conventional Commits
    res = git_commit_staged("feat: add body file\n\nLonger explanation of why this change exists.")
    assert res["summary"] == "feat: add body file"


def test_repo_outside_workspace_rejected(isolated_env, tmp_path, monkeypatch):
    # A repo whose root is OUTSIDE the workspace: git tools must refuse even
    # though the path handed to them resolves inside the workspace, because
    # git walks up from that path to find the repository root.
    outer = tmp_path / "outer_repo"
    inner = outer / "nested_dir"
    inner.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=outer, capture_output=True)

    import unified_agent.tools.git as git_tool

    monkeypatch.setattr(git_tool, "resolve_in_workspace", lambda path, use_worktree=True: inner)

    with pytest.raises(ToolError, match="outside the workspace"):
        git_diff_staged()
    with pytest.raises(ToolError, match="outside the workspace"):
        git_commit_staged("feat: should never land")


def test_commit_gated_when_listed_commits_nothing(mock_git_repo, monkeypatch):
    """check_approval_gate wiring on git_commit_staged (task #115, design-doc T5)."""
    test_file = mock_git_repo / "dummy.txt"
    test_file.write_text("line 1\n")
    subprocess.run(["git", "add", "dummy.txt"], cwd=mock_git_repo, capture_output=True)

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "git_commit_staged")
    with pytest.raises(ApprovalPendingError, match="queued"):
        git_commit_staged("feat: add dummy file")

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=mock_git_repo, capture_output=True, text=True
    )
    assert log.stdout.strip() == ""
    pending = approval_pending()["pending"]
    assert len(pending) == 1
    assert pending[0]["tool"] == "git_commit_staged"
