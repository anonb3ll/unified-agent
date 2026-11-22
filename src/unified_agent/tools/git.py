"""Safe git diff extraction and Conventional Commit execution inside the workspace."""

import os
import re
import subprocess
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..safety import resolve_in_workspace


class GitOperationError(ToolError):
    """Git availability/execution failure, distinct from authority refusal."""


CONVENTIONAL_COMMIT_REGEX = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([a-zA-Z0-9_\-\s]+\))?!?:\s[^\n]{1,100}$"
)

# Must not start with "-" (would be parsed as a git option) and stays within
# git's own ref-name character rules.
BRANCH_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _run_git(args: list[str], cwd: str, use_worktree: bool = True) -> subprocess.CompletedProcess:
    from ..execution_locality import refuse_tool

    refuse_tool("git repository operation")
    workdir = resolve_in_workspace(cwd, use_worktree=use_worktree)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return res
    except FileNotFoundError:
        raise GitOperationError("git executable not found on system path") from None
    except subprocess.TimeoutExpired:
        raise GitOperationError("git command timed out after 30 seconds") from None


def _ensure_repo_in_workspace(repo_path: str, use_worktree: bool = True) -> None:
    """resolve_in_workspace confines `repo_path` itself, but git walks up
    from there to find the repository — refuse repos whose root lies outside
    the workspace, or every tool here would operate on an outer repo."""
    res = _run_git(["rev-parse", "--show-toplevel"], repo_path, use_worktree=use_worktree)
    if res.returncode != 0:
        from ..hosted_capabilities import refuse_empty_hosted_workspace

        refuse_empty_hosted_workspace("git")
        raise GitOperationError(f"Directory {repo_path!r} is not inside a git repository.")
    top = Path(res.stdout.strip()).resolve()
    root = config.workspace_root().resolve()
    if not top.is_relative_to(root):
        raise ToolError(
            f"repository root {str(top)!r} is outside the workspace root; "
            "git tools only operate on repositories inside the workspace."
        )


def git_diff_staged(repo_path: str = ".") -> dict:
    """Retrieve staged (cached) file changes and raw diff text from a git repository.

    Returns {staged_files: [{file, status}], diff_text, is_clean}.
    """
    _ensure_repo_in_workspace(repo_path)

    # 1. Get status of staged changes
    res_status = _run_git(["status", "--porcelain"], repo_path)
    if res_status.returncode != 0:
        raise ToolError(f"git status failed: {res_status.stderr.strip()}")

    staged_files = []
    for line in res_status.stdout.splitlines():
        if len(line) >= 3:
            index_status = line[0]  # Staged status
            filepath = line[3:]
            if index_status in ("M", "A", "D", "R", "C"):
                staged_files.append({"file": filepath, "status": index_status})

    # 2. Get staged diff
    res_diff = _run_git(["diff", "--cached"], repo_path)
    if res_diff.returncode != 0:
        raise ToolError(f"git diff failed: {res_diff.stderr.strip()}")

    diff_text = res_diff.stdout
    is_clean = not staged_files and not diff_text.strip()

    return {
        "staged_files": staged_files,
        "diff_text": diff_text,
        "is_clean": is_clean,
    }


def git_commit_staged(message: str, repo_path: str = ".") -> dict:
    """Commit staged changes using a message conforming to the Conventional Commits specification.

    Conforming formats:
    - feat(ui): add new button
    - fix: resolve syntax error
    - refactor!: rewrite memory module (breaking change indicated by !)

    A body is allowed after the header, separated by a blank line.

    Returns {commit_hash, summary, stdout}.
    """
    message = message.strip()
    lines = message.splitlines()
    header = lines[0] if lines else ""
    if not CONVENTIONAL_COMMIT_REGEX.match(header):
        raise ToolError(
            f"Commit header {header!r} does not match Conventional Commits specification. "
            "Expected format: '<type>(<scope>)?: <description>' (max 100 characters). "
            "Types allowed: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert."
        )
    if len(lines) > 1 and lines[1].strip():
        raise ToolError(
            "Conventional Commits requires a blank line between the header and the body."
        )

    _ensure_repo_in_workspace(repo_path)

    # Approval gate (task 62 · C): opt-in global queue, layered AFTER the
    # commit-format and workspace-repo checks above — it can only tighten,
    # never bypass them. With UA_APPROVAL_TOOLS unset (or not listing
    # "git_commit_staged") this is a no-op.
    from .approvals import check_approval_gate

    check_approval_gate(
        "git_commit_staged",
        {"message": message, "repo_path": repo_path},
        requested_by=config.verified_agent_identity(),
    )

    # Make the commit
    res_commit = _run_git(["commit", "-m", message], repo_path)
    if res_commit.returncode != 0:
        raise ToolError(f"git commit failed: {res_commit.stderr.strip()}")

    # Get the latest commit hash and summary
    res_log = _run_git(["log", "-n", "1", "--pretty=format:%h %s"], repo_path)
    commit_hash = ""
    summary = ""
    if res_log.returncode == 0 and res_log.stdout.strip():
        parts = res_log.stdout.strip().split(" ", 1)
        commit_hash = parts[0]
        summary = parts[1] if len(parts) > 1 else ""

    return {
        "commit_hash": commit_hash,
        "summary": summary,
        "stdout": res_commit.stdout.strip(),
    }


def parse_worktree_porcelain(stdout: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output into worktree records.

    Split out from ``git_worktree_list`` so callers that reach a repo outside
    the workspace sandbox — see ``merge._worktrees_of_project_repo`` — parse
    identically instead of keeping a second copy of this format in sync.
    """
    worktrees = []
    current = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        parts = line.split(" ", 1)
        key = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        if key == "worktree":
            current["path"] = val
        elif key == "HEAD":
            current["head"] = val
        elif key == "branch":
            current["branch"] = val.replace("refs/heads/", "")
        elif key == "bare":
            current["bare"] = True

    if current:
        worktrees.append(current)

    return worktrees


def git_worktree_list(repo_path: str = ".") -> dict:
    """List all active git worktrees registered for the repository.

    Returns {worktrees: [{path, head, branch, bare}]}.
    """
    _ensure_repo_in_workspace(repo_path)
    res = _run_git(["worktree", "list", "--porcelain"], repo_path)
    if res.returncode != 0:
        raise ToolError(f"git worktree list failed: {res.stderr.strip()}")

    return {"worktrees": parse_worktree_porcelain(res.stdout)}


def git_worktree_add(path: str, branch: str, repo_path: str = ".") -> dict:
    """Create a new concurrent git worktree checkout at a specified path.

    If the branch does not exist locally, git will automatically create it pointing to HEAD.

    Paths here resolve against the workspace ROOT, never the caller's active
    task worktree. `resolve_in_workspace` defaults to the latter, so an agent
    holding task A that claimed task B had `.worktrees/task-B` resolved
    against A's checkout and created `.worktrees/task-A/.worktrees/task-B`
    (task #187; observed live as `.worktrees/task-180/.worktrees/task-159`).
    Where worktrees live is a property of the workspace — it must not depend
    on which agent is calling or what it happens to be working on.
    """
    if not BRANCH_NAME_REGEX.match(branch):
        raise ToolError(
            f"invalid branch name {branch!r}: must start with a letter or digit and "
            "contain only letters, digits, '.', '_', '/', '-'"
        )
    _ensure_repo_in_workspace(repo_path, use_worktree=False)
    resolved_path = resolve_in_workspace(path, use_worktree=False)

    # Check if branch exists locally
    res_branch = _run_git(
        ["show-ref", "--verify", f"refs/heads/{branch}"], repo_path, use_worktree=False
    )
    exists = res_branch.returncode == 0

    args = ["worktree", "add", str(resolved_path)]
    if not exists:
        args += ["-b", branch]
    else:
        args += [branch]

    res = _run_git(args, repo_path, use_worktree=False)
    if res.returncode != 0:
        raise GitOperationError(f"git worktree add failed: {res.stderr.strip()}")

    return {
        "path": str(resolved_path),
        "branch": branch,
        "stdout": res.stdout.strip(),
    }


def git_worktree_remove(path: str, force: bool = False, repo_path: str = ".") -> dict:
    """Remove and clean up a previously created git worktree.

    Pass force=True to remove worktrees with modified staged or untracked changes.

    Root-anchored for the same reason as `git_worktree_add`: a caller inside
    its own task worktree must still address `.worktrees/task-N` by the path
    everything else records (task #187).
    """
    _ensure_repo_in_workspace(repo_path, use_worktree=False)
    resolved_path = resolve_in_workspace(path, use_worktree=False)
    args = ["worktree", "remove", str(resolved_path)]
    if force:
        args.insert(2, "--force")
    res = _run_git(args, repo_path, use_worktree=False)
    if res.returncode != 0:
        raise ToolError(f"git worktree remove failed: {res.stderr.strip()}")

    return {
        "path": str(resolved_path),
        "deleted": True,
        "stdout": res.stdout.strip(),
    }


# The merge orchestrator lives in ``merge`` (github), which this kernel module
# may not import. ``merge`` registers it here when imported, and
# ``post_init.register_default_hooks`` imports ``merge`` in every hub process
# (#2450). Absent, git_merge_task refuses before any gate runs: nothing merges.
_merge_orchestrator = None


def register_merge_orchestrator(handler) -> None:
    """Install ``handler(task_id=, force=, dry_run=, allow_in_flight=)`` for git_merge_task."""
    global _merge_orchestrator
    if not callable(handler):
        raise TypeError("merge orchestrator must be callable")
    _merge_orchestrator = handler


def merge_orchestrator():
    """The registered merge orchestrator, or None in a process that never loaded ``merge``."""
    return _merge_orchestrator


def git_merge_task(
    task_id: int,
    force: bool = False,
    dry_run: bool = False,
    allow_in_flight: bool = False,
) -> dict:
    """Orchestrate validation, merging, and worktree/branch cleanup for a completed task branch.

    dry_run=True runs every validation step but performs no merge or cleanup.

    allow_in_flight=True lets an automated caller merge a task that is still
    ``doing``/``blocked`` rather than ``done`` — the reviewer's ``+yolo``
    delivery path, which reviews before the task is closed. It relaxes the
    status precondition ONLY: the clean-worktree check, security audit and
    test suite still gate the merge. Use it instead of ``force``, which
    skips those gates.
    """
    from .. import gateway
    from ..safety import check_prod_action_gate
    from . import approvals

    orchestrate_merge = merge_orchestrator()
    if orchestrate_merge is None:
        raise ToolError(
            "git_merge_task: the merge orchestrator (unified_agent.merge) is not loaded "
            "in this process; nothing was merged"
        )

    with (
        gateway.invocation("git_merge_task"),
        approvals.approval_invocation(join_existing=True, restore_unstarted=True),
    ):
        check_prod_action_gate(
            "git_merge_task",
            {
                "task_id": task_id,
                "force": force,
                "dry_run": dry_run,
                "allow_in_flight": allow_in_flight,
            },
        )
        try:
            return orchestrate_merge(
                task_id=task_id,
                force=force,
                dry_run=dry_run,
                allow_in_flight=allow_in_flight,
            )
        except Exception as e:
            raise ToolError(str(e)) from e
