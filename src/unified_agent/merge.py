"""Git Merge & Worktree Orchestration CLI.

Validates, merges, and cleans up completed task branches/worktrees.
"""

import argparse
import contextvars
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_access,
    autonomy_modes,
    config,
    gateway,
    policy,
    post_init,
    profiles,
    storage,
    trust_presets,
)
from .audit.engine import format_text as _format_text
from .audit.engine import run_audit
from .note_lock import unlocked_note_slot
from .safety import resolve_in_vault, resolve_task_worktree
from .sync import sync_state_to_vault
from .tools.git import (
    git_worktree_list,
    parse_worktree_porcelain,
    register_merge_orchestrator,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge and clean up completed task branches and worktrees."
    )
    parser.add_argument("task_id", type=int, help="ID of the task to merge.")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force merge even if task is not marked done, or if there are uncommitted changes.",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Dry run: validate everything but do not merge or delete.",
    )
    return parser.parse_args()


def write_failed_report(
    task_id: int, reason: str, test_output: str, audit_output: str
) -> Path | None:
    """Write a detailed merge failure markdown report into the Obsidian vault inbox.

    Returns the report path, or None if it could not be written. A rerun
    replaces the task's previous report, unless that note is ``locked: true``
    (an operator keeping it): then the report goes to the next free sibling
    (``Merge-Failed-Task-<id>-2.md``) and that path is returned, so the merge
    error names the report that holds this failure (#2091).
    """
    # Route through the vault write chokepoint: confines the report to the
    # UA_VAULT_WRITE_DIRS allowlist (00 - Inbox) rather than trusting convention.
    try:
        report_file = resolve_in_vault(f"00 - Inbox/Merge-Failed-Task-{task_id}.md", for_write=True)
    except ToolError:
        return None
    report_file.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = f"""---
date: {datetime.now().strftime("%Y-%m-%d")}
title: "Merge Failure Report: Task {task_id}"
tags: [merge-failure, task-{task_id}, automated-report]
---

# Merge Failure Report: Task {task_id}

* **Generated**: `{ts}`
* **Status**: `FAILED`
* **Reason**: {reason}

---

## Validation Logs

### Security Audit Output
```text
{audit_output.strip() or "No audit output recorded."}
```

### Pytest Execution Output
```text
{test_output.strip() or "No test output recorded."}
```
"""
    try:
        # The per-note lock the hub's other note writers take (#2086). The
        # report is built above, so nothing slow runs while it is held.
        with unlocked_note_slot(report_file) as slot:
            slot.write_text(content, encoding="utf-8")
        return slot
    except Exception as e:
        print(f"Error writing failure report to vault: {e}", file=sys.stderr)
        return None


#: pytest's exit code for "no tests were collected" (pytest ExitCode.NO_TESTS_COLLECTED).
#: Distinct from 1, "tests were collected and some failed" — task #466.
PYTEST_NO_TESTS_COLLECTED = 5


class TestTimeoutError(RuntimeError):
    """Base for "the test runner's own wall-clock ceiling fired" — distinct
    from a genuine test failure.

    Task #1254: a bare `RuntimeError` alone reads to `_validate_and_hold` (and
    every other caller of `run_validation_gates`) exactly like a real test
    failure — both landed a "failed"/"rejected" verdict, which is how seven
    review runs on 09-20 through 09-22 posted false "pytest timed out" FAILED
    verdicts even though two of those PRs later merged green. A dedicated
    subclass lets callers tell "the suite ran and something broke" apart from
    "the suite never finished" without parsing the message string, while
    still behaving as a plain `RuntimeError` for any caller that only catches
    that.

    Shared by every native test runner's timeout (`PytestTimeoutError`,
    `BunTestTimeoutError`, …) so `reviewer._test_timeout_cause` can recognize
    any of them with one `isinstance` check instead of enumerating types.
    """


class PytestTimeoutError(TestTimeoutError):
    """The worktree's pytest run exceeded ``config.review_pytest_timeout_s()``."""


class BunTestTimeoutError(TestTimeoutError):
    """The worktree's `bun test` run exceeded ``config.review_bun_test_timeout_s()``.

    Task #1295 follow-up to #1254: `_run_bun_test` used to raise a bare
    `RuntimeError` on timeout, which the review pipeline's timeout-vs-failure
    branch (keyed on `PytestTimeoutError`/`TestTimeoutError`) couldn't tell
    apart from a real bun-test failure — the same false-FAILED-verdict bug
    #1254 fixed for pytest, just not yet for bun.
    """


def _safe_test_paths(workdir: Path, test_paths: list[str]) -> list[str]:
    """Keep only relative test files inside the worktree; never an option."""
    root = workdir.resolve()
    safe: list[str] = []
    for raw in test_paths:
        rel = str(raw).strip()
        if not rel or rel.startswith("-") or Path(rel).is_absolute():
            continue
        target = (root / rel).resolve()
        if target.is_file() and target.is_relative_to(root):
            safe.append(rel)
    return safe


def _pytest_gate_env() -> dict[str, str]:
    """Environment for the validation pytest run: an allowlist, never the host's.

    Task #1844: the gate must judge the candidate's code, not the hub's
    policy. Only PATH/HOME/LANG are carried over, so ``UA_APPROVAL_*`` and
    every other ``UA_*`` knob of the hub process stay out. ``HOME`` alone
    would still let ``config`` seed the hub's ``settings.toml`` into the run
    at import, so the settings file is disabled the same way the test
    suite's own conftest disables it.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "UA_SETTINGS_FILE": "",
    }


def _run_pytest(cwd: str, test_paths: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run the worktree's test suite with cwd confined to the workspace root.

    ``test_paths`` (task #1787) narrows the run to those files; None runs the
    whole suite. A list that filters down to nothing is refused rather than
    widened into a full-suite run.
    """
    from . import execution_locality

    # Task #2109: the test gate runs project code; never on a devices-only hub.
    execution_locality.refuse("merge test gate")
    workdir = resolve_task_worktree(cwd)
    args = [sys.executable, "-m", "pytest"]
    if test_paths is not None:
        safe = _safe_test_paths(Path(workdir), test_paths)
        if not safe:
            raise RuntimeError("targeted pytest run has no valid test files")
        args += ["-q", *safe]
    env = _pytest_gate_env()
    timeout_s = config.review_pytest_timeout_s()
    try:
        return subprocess.run(
            args,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        raise RuntimeError("python executable not found to run pytest") from None
    except subprocess.TimeoutExpired:
        raise PytestTimeoutError(f"pytest timed out after {timeout_s} seconds") from None


def _run_bun_test(cwd: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a Bun project's declared test suite without invoking a shell."""
    from . import execution_locality

    execution_locality.refuse("merge test gate")
    workdir = resolve_task_worktree(cwd)
    bun = shutil.which("bun")
    if not bun:
        return subprocess.CompletedProcess(["bun", *args], 127, "", "bun executable not found")
    timeout_s = config.review_bun_test_timeout_s()
    try:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        return subprocess.run(
            [bun, *args],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise BunTestTimeoutError(f"bun test timed out after {timeout_s} seconds") from None


def _run_tests(cwd: str, test_paths: list[str] | None = None) -> subprocess.CompletedProcess:
    """Select the repository-native test runner from checked-in project metadata.

    ``test_paths`` narrows a pytest run (task #1787); Bun suites ignore it.
    """
    from . import execution_locality

    execution_locality.refuse("merge test gate")
    workdir = resolve_task_worktree(cwd)
    package_json = workdir / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        if isinstance(package, dict) and isinstance(package.get("scripts"), dict):
            script = package["scripts"].get("test")
            if script:
                try:
                    args = shlex.split(script)
                except ValueError:
                    args = []
                if args[:2] == ["bun", "test"]:
                    return _run_bun_test(cwd, args[1:])
                if not shutil.which("bun"):
                    return subprocess.CompletedProcess(
                        ["bun", "test"], 127, "", "Bun is required by package.json test script"
                    )
    return _run_pytest(cwd, test_paths)


def _run_audit(cwd: str) -> dict:
    """Run the read-only security audit in-process, cwd confined to the workspace root.

    Calls the importable engine directly instead of spawning a subprocess, so we
    keep the structured findings and skip interpreter startup.
    """
    return run_audit(resolve_task_worktree(cwd))


def _worktrees_of_project_repo(project_repo: Path) -> list[dict]:
    """List `project_repo`'s worktrees, bypassing the workspace sandbox.

    Task #465: mirrors the narrow, internal-only escape hatch established by
    ``tools.tasks._git_worktree_add_from_known_project_repo``. ``project_repo``
    is never agent- or caller-supplied — it comes only from the fixed,
    existence-checked set in ``config.project_repo_roots()`` — so the
    agent-facing ``git_*`` tools' reach is unchanged.
    """
    res = subprocess.run(
        ["git", "-C", str(project_repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        return []
    return parse_worktree_porcelain(res.stdout)


def _known_repo_or_raise(repo: str) -> str:
    """Confirm `repo` is the workspace root or a known project repo; return it unchanged.

    `orchestrate_merge`'s `main_repo` is always one of those two — sourced
    from `find_task_worktree`, which in turn only ever returns the workspace
    root or a `config.project_repo_roots()` entry — never caller input. This
    enforces that invariant at the point of use rather than trusting it
    implicitly, before any unsandboxed git call runs against it.
    """
    resolved = Path(repo).resolve()
    known = {
        config.workspace_root().resolve(),
        *(p.resolve() for p in config.project_repo_roots()),
    }
    if resolved not in known:
        raise ToolError(f"refusing to run git against unrecognized repo path {repo!r}")
    return repo


def _run_git_in_repo(args: list[str], repo: str) -> subprocess.CompletedProcess:
    """Run git directly against `repo`, bypassing the workspace sandbox.

    `tools.git._run_git` resolves its cwd through `resolve_in_workspace()`,
    which rejects any path outside `UA_WORKSPACE` by design. A project-scoped
    task's main checkout is a project repo under ~/dev, outside the workspace
    by construction (`find_task_worktree`) — so passing it to the sandboxed
    runner meant the very first `git checkout main` raised before any
    project-scoped task could be merged (task #467 / PR #203 review). Callers
    must resolve `repo` through `_known_repo_or_raise` first: this function
    trusts its argument completely and must never be reachable with
    caller-supplied input.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        raise ToolError("git executable not found on system path") from None
    except subprocess.TimeoutExpired:
        raise ToolError("git command timed out after 30 seconds") from None


def _worktree_remove_in_repo(wt_path: str, repo: str, force: bool = False) -> None:
    """Remove a worktree at `wt_path` registered against `repo`. Raises on failure."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(wt_path)
    res = _run_git_in_repo(args, repo)
    if res.returncode != 0:
        raise ToolError(f"git worktree remove failed: {res.stderr.strip()}")


# Name tokens that put a number other than a task id right before it: a PR
# number (`pr-994`, `gate-960-<sha>`, `reland-858`) or a deploy/batch label.
# A worktree named that way is never task N's checkout (task #1844).
_NON_TASK_ID_PREFIXES = frozenset({"pr", "pull", "gate", "reland", "deploy", "batch", "release"})


def names_task_id(text: str, task_id: int, *, slug: bool = True) -> bool:
    """Whether a worktree branch or path names task ``task_id``.

    The id must be its own token at the end of the name: ``task-<N>``,
    ``task/<N>-<slug>``, ``<agent>/<N>-<slug>``, ``<agent>-<N>-<slug>``,
    ``task_<N>-agy``, ``task<N>-<slug>``. ``slug=False`` refuses a trailing
    slug (the reconcile pass's stricter form). An id preceded by a PR-style
    word (``pr-<N>``, ``gate-<N>-<sha>``, ``reland-<N>``) never matches,
    because that number is not a task id (task #1844).
    """
    if not text:
        return False
    tail = rf"(?P<task>task[-_]?)?{task_id}(?:-agy)?"
    if slug:
        tail += r"(?:[-_][a-zA-Z0-9-]+)?"
    tail_rx = re.compile(tail + r"/?$")
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch in "-_/"]
    for start in starts:
        m = tail_rx.match(text, start)
        if m is None:
            continue
        if m.group("task"):
            return True
        prefix_words = re.split(r"[-_/.]", text[:start].rstrip("-_/"))
        if prefix_words[-1].lower() not in _NON_TASK_ID_PREFIXES:
            return True
    return False


def _legacy_workspace_checkout(raw_path: object) -> bool:
    """Whether stored checkout provenance names a pre-#2041 workspace path."""
    if not isinstance(raw_path, str) or not raw_path:
        return False
    recorded = Path(raw_path)
    return (
        not recorded.is_absolute()
        and len(recorded.parts) == 2
        and recorded.parts[0] == ".worktrees"
        and re.fullmatch(r"task-\d+(?:[-_][A-Za-z0-9-]+)?", recorded.parts[1]) is not None
    )


def _task_repo_routing(row) -> tuple[Path | None, bool]:
    """Which product repo a task's worktree belongs to, and whether only it counts.

    Task #1844. Returns ``(repo, exclusive)``:

    - A named-project task, or a ``[code-task]`` row of the default project,
      returns its checkout (``tools.tasks._worktree_source_repo``) with
      ``exclusive=True``. Its code can only live there, so a same-named
      ``task-<id>`` worktree in the workspace (cockpit) repo must never be
      validated in its place.
    - A full task row without a valid legacy workspace-relative checkout is
      product-sourced, including unmarked default-project tasks claimed since
      #2041. It is exclusive even when that product checkout is missing here.
      A recorded ``.worktrees/...`` relative path keeps the historical cockpit
      fallback. Private legacy callers lacking checkout provenance retain the
      pre-#2041 marker rule; review callers must pass their complete task row.
    - A product-sourced task whose checkout is absent on this host returns
      ``(None, True)``: there is no repo to search, and the workspace still
      holds none of its code (PR #1000 review), so nothing is found.
    - Anything else (no row, an unknown project, a project whose checkout is
      the workspace itself) returns ``(None, False)`` and keeps the
      workspace-first lookup.
    """
    if row is None:
        return None, False
    keys = row.keys()
    project = row["project"] if "project" in keys else None
    notes = row["notes"] if "notes" in keys else None
    if not project:
        return None, False
    from .tools.tasks import _CODE_TASK_MARKER_RX, DEFAULT_PROJECT, _worktree_source_repo

    try:
        workspace = config.workspace_root().resolve()
    except Exception:
        workspace = None
    known = project in config._KNOWN_PROJECTS
    expected = (
        (config.dev_root() / config.PROJECT_REPO_DIRNAMES.get(project, project)).resolve()
        if known
        else None
    )
    if expected is not None and expected == workspace:
        return None, False
    # Product-sourced is decided by the task, not by whether its checkout
    # exists here; _worktree_source_repo returns None for an absent checkout.
    has_provenance = "worktree_path" in keys
    raw_path = row["worktree_path"] if has_provenance else None
    legacy_relative = _legacy_workspace_checkout(raw_path)
    exclusive = known and (
        project != DEFAULT_PROJECT
        or bool(notes and _CODE_TASK_MARKER_RX.search(notes))
        or (has_provenance and not legacy_relative)
    )
    source = _worktree_source_repo(project)
    if source is None or not Path(source).is_dir():
        return None, exclusive
    source = Path(source).resolve()
    if source == workspace:
        return None, False
    return source, exclusive


def find_task_worktree(task_id: int) -> tuple[dict | None, str | None]:
    """Locate the git worktree whose branch/path matches a task id.

    Returns ``(worktree, main_repo_path)`` where ``worktree`` is the matching
    entry (or ``None``) and ``main_repo_path`` is the first worktree listed by
    the repo that was searched (its main checkout), if any.

    Task #465: a project-scoped task's worktree *directory* lives under the
    workspace root like every other, but it is sourced from that project's own
    ~/dev checkout, so its git metadata is registered against the PROJECT repo
    — `git worktree list` run in the workspace never lists it. Searching only
    the workspace repo therefore reported "no git worktree found" for every
    project-scoped task (#365, #369, #372, #423-#426, #440). Both repos are
    searched now: the workspace first, so existing behavior is unchanged
    wherever it already worked.

    Task #1844: workspace-first was wrong for a task whose code lives in a
    product repo. The cockpit workspace can hold a same-named
    ``.worktrees/task-<id>`` checkout (minted at claim time) with no product
    code in it, and the review gates then ran pytest there and reported a
    false failure. The task row's project now routes the lookup first; see
    ``_task_repo_routing``. A product-sourced task is looked up only in its
    own repo; on a miss the caller's no-worktree fallbacks apply.

    `UA_WORKSPACE` is deliberately allowed to be a non-git cockpit directory
    (CLAUDE.md topology) — the shared cross-repo workspace, not itself a
    product repo. `git_worktree_list` raises `ToolError` there, and left
    unguarded that propagated straight out of this function, so the
    project-repo fallback below was never reached and every project-scoped
    task read as "no git worktree found" on such a workspace (PR #202
    review). A non-repository workspace is treated as an empty worktree
    list instead: nothing to fail, just nothing found there yet.
    """
    # Check task database record first
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT worktree_path, branch, project, notes FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()

    expected_path = None
    branch = None
    if row and row["worktree_path"]:
        try:
            expected_path = str(resolve_task_worktree(row["worktree_path"]))
            branch = row["branch"]
        except Exception:
            expected_path = None

    def match(candidates: list[dict]) -> dict | None:
        for wt in candidates:
            if expected_path and (
                wt.get("path") == expected_path or (branch and wt.get("branch") == branch)
            ):
                return wt
        # Fallback to task-id matching on the worktree's branch or path
        for wt in candidates:
            if names_task_id(wt.get("branch") or "", task_id) or names_task_id(
                wt.get("path") or "", task_id
            ):
                return wt
        return None

    routed_repo, exclusive = _task_repo_routing(row)
    if routed_repo is None and exclusive:
        # Product-sourced, but its checkout is absent on this host. Claim time
        # then cut the worktree from the workspace (tools.tasks fallback), so
        # trust exactly the checkout recorded on the row and nothing matched by
        # name: a same-numbered cockpit worktree is not this task's (PR #1000).
        if not expected_path or not _legacy_workspace_checkout(row["worktree_path"]):
            return None, None
        try:
            ws = git_worktree_list(repo_path=str(config.workspace_root())).get("worktrees", [])
        except ToolError:
            ws = []
        for wt in ws:
            if wt.get("path") == expected_path:
                return wt, ws[0]["path"]
        return None, None
    if routed_repo is not None:
        routed_worktrees = _worktrees_of_project_repo(routed_repo)
        routed_main = routed_worktrees[0]["path"] if routed_worktrees else str(routed_repo)
        found = match(routed_worktrees)
        if found is not None or exclusive:
            return found, routed_main

    try:
        worktrees = git_worktree_list(repo_path=str(config.workspace_root())).get("worktrees", [])
    except ToolError:
        worktrees = []
    main_repo = worktrees[0]["path"] if worktrees else None

    found = match(worktrees)
    if found is not None:
        return found, main_repo

    # Only on a miss: the worktree may be registered against a project repo
    # rather than the workspace (see the docstring). Deferred so the common
    # case costs no extra subprocesses.
    for project_repo in config.project_repo_roots():
        if routed_repo is not None and project_repo.resolve() == routed_repo:
            continue  # already searched above
        project_worktrees = _worktrees_of_project_repo(project_repo)
        found = match(project_worktrees)
        if found is not None:
            project_main = project_worktrees[0]["path"] if project_worktrees else str(project_repo)
            return found, project_main

    return None, main_repo


# Task #1787: the reviewer's +yolo path merges right after it validated the
# same head, with exact-head CI standing in for pytest or with targeted tests.
# `git_merge_task` re-runs the gates; without this scope that second run was a
# whole-suite pytest on the hub. The scope is process-internal (never a tool
# argument), bound to one task and one head sha, so no agent can use it to
# skip tests on a merge it asks for.
_REVIEW_GATE_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ua_review_gate_scope", default=None
)


@contextmanager
def reviewed_gate_scope(
    task_id: int, head_sha: str, *, skip_tests: bool, test_paths: list[str] | None
):
    """Let the merge gates reuse the reviewer's test scope for this task and head."""
    token = _REVIEW_GATE_SCOPE.set(
        {
            "task_id": task_id,
            "head": head_sha,
            "skip_tests": skip_tests,
            "test_paths": list(test_paths) if test_paths is not None else None,
        }
    )
    try:
        yield
    finally:
        _REVIEW_GATE_SCOPE.reset(token)


def _reviewed_gate_kwargs(task_id: int, wt_path: str) -> dict:
    """Gate kwargs from an active reviewer scope; {} (whole suite) unless it matches."""
    scope = _REVIEW_GATE_SCOPE.get()
    if not scope or scope["task_id"] != task_id or not scope["head"]:
        return {}
    from .cycle_eligibility import _head_sha

    head = _head_sha(wt_path)
    if not head or head != scope["head"]:
        return {}
    kwargs: dict = {"skip_tests": bool(scope["skip_tests"])}
    if scope["test_paths"] is not None:
        kwargs["test_paths"] = list(scope["test_paths"])
    return kwargs


def run_validation_gates(
    task_id: int,
    wt_path: str,
    force: bool = False,
    *,
    skip_tests: bool = False,
    test_paths: list[str] | None = None,
) -> dict:
    """Run the merge validation gates inside a task worktree without merging.

    Gates: clean-tree check, in-process security audit, pytest. This function
    deliberately has **no task-status precondition** — it is shared by the real
    merge path (``orchestrate_merge``, which enforces status ``done`` first)
    and the validate+hold review path (``reviewer.py``, which validates
    in-flight ``doing``/``blocked`` tasks). It never mutates the repository.

    ``skip_tests`` (task #1338): when True, skip the local pytest/bun-test run
    after a successful clean-tree + audit — the review pipeline uses this when
    exact-head CI already proved the required test shards SUCCESS.
    The real merge path never passes ``skip_tests=True``.

    ``test_paths`` (task #1787): run only these pytest files instead of the
    whole suite. The review pipeline passes them when no exact-head CI can
    stand in, so the hub never runs the full suite. None keeps the full run.

    Raises ``ValueError``/``RuntimeError`` on gate failure (unless ``force``).
    """
    # 1. Check for clean worktree state.
    #
    # Resolve-then-trust, the same pattern `_known_repo_or_raise` +
    # `_run_git_in_repo` uses above. `tools.git._run_git` would re-resolve the
    # cwd through `resolve_in_workspace`, which rejects a project-sourced
    # worktree by construction (task #496) — the clean-tree gate raised before
    # any gate could report on the code.
    wt_dir = str(resolve_task_worktree(wt_path))
    status_res = _run_git_in_repo(["status", "--porcelain"], wt_dir)
    if status_res.returncode != 0:
        raise RuntimeError(f"git status failed: {status_res.stderr.strip()}")

    if status_res.stdout.strip() and not force:
        raise ValueError(
            f"Worktree contains uncommitted changes:\n{status_res.stdout}\nCommit/stash changes or use force=True."
        )

    # 2. Run the security audit (in-process) and unit tests inside the worktree
    audit_report = _run_audit(wt_path)
    audit_failed = audit_report["status"] == "fail"

    test_stdout = ""
    test_stderr = ""
    no_tests_collected = False
    if skip_tests:
        test_failed = False
    else:
        test_res = _run_tests(wt_path, test_paths)
        test_stdout = test_res.stdout
        test_stderr = test_res.stderr
        no_tests_collected = test_res.returncode == PYTEST_NO_TESTS_COLLECTED
        test_failed = test_res.returncode != 0

    if (audit_failed or test_failed) and not force:
        reason = []
        if audit_failed:
            reason.append("Security audit failed")
        if no_tests_collected:
            reason.append("No tests were collected")
        elif test_failed:
            reason.append("Unit tests failed")
        reason_str = " & ".join(reason)

        report_path = write_failed_report(
            task_id,
            reason_str,
            test_stdout + "\n" + test_stderr,
            _format_text(audit_report),
        )
        report_name = report_path.name if report_path else None
        raise RuntimeError(f"Validation checks failed: {reason_str}. Failure report: {report_name}")

    return {
        "success": True,
        "audit_status": audit_report["status"],
        "tests_passed": not test_failed,
        "tests_skipped": skip_tests,
        "tests_targeted": test_paths is not None,
    }


def _run_post_merge_verify(task_id: int) -> dict | None:
    """Run the post-merge-verify workflow, recording a non-fatal warning note on
    the task if it trips (#905).

    Returns the workflow result, or None when the hook is disabled via
    ``UA_POST_MERGE_VERIFY`` set to ``0``/``false``/``no``/``off``. Never raises:
    a completed merge must not be treated as failed just because verification did.
    """
    if config.env_flag_off("UA_POST_MERGE_VERIFY"):
        return None

    try:
        from .tools.workflow import workflow_run

        workflow_res = workflow_run(name="post-merge-verify")
    except Exception as e:
        workflow_res = {"workflow": "post-merge-verify", "status": "failed", "error": str(e)}

    is_failed = False
    fail_detail = ""
    if not isinstance(workflow_res, dict):
        is_failed = True
        fail_detail = f"unexpected result: {workflow_res}"
    elif workflow_res.get("status") != "done":
        is_failed = True
        fail_detail = workflow_res.get("error") or f"status={workflow_res.get('status')}"
    else:
        for step in workflow_res.get("steps", []):
            res = step.get("result")
            if isinstance(res, dict):
                if res.get("exit_code") not in (None, 0):
                    is_failed = True
                    fail_detail = f"step {step.get('name')} exit_code={res.get('exit_code')}"
                    break
                if res.get("status") in (
                    "fail",
                    "failed",
                    "degraded",
                    "incompatible",
                    "MISCONFIGURED",
                ):
                    is_failed = True
                    fail_detail = f"step {step.get('name')} status={res.get('status')}"
                    break

    if is_failed:
        warn_note = (
            f"[warning: post-merge verification failed @ "
            f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}: {fail_detail}]"
        )
        try:
            from . import run_records

            if run_records.scoped_direct_completion(task_id) is not None:
                if run_records.append_scoped_direct_note(task_id, warn_note):
                    sync_state_to_vault()
                return workflow_res
            conn = storage.connect()
            try:
                storage.begin_task_write(conn)  # fence the revision read with its write
                row = conn.execute(
                    "SELECT notes, updated_at FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                cur_notes = (row["notes"] or "") if row else ""
                updated_notes = f"{cur_notes}\n{warn_note}".strip()
                conn.execute(
                    "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
                    (
                        updated_notes,
                        storage.next_revision(row["updated_at"] if row else None),
                        task_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            sync_state_to_vault()
        except Exception as e:
            print(f"Warning appending post-merge verification note: {e}", file=sys.stderr)

    return workflow_res


def _close_delegation_ledger(task_id: int) -> dict:
    """Record the ``merged`` outcome for delegations resolved by this merge (#908).

    Marks pending/in-flight delegation rows for the task as ``completed`` with
    ``callback_result='merged'`` and stamps a ``[review: merged @ ...]`` marker
    on the task notes so the ledger stops reading as write-only. Never raises.
    """
    from . import run_records

    expected = run_records.scoped_direct_completion(task_id)
    try:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if expected is not None and (
                task is None or run_records._direct_completion_snapshot_on(conn, task) != expected
            ):
                return {"delegations_closed": 0}
            open_rows = conn.execute(
                "SELECT id FROM delegations WHERE task_id = ? AND status IN ('pending', 'in_flight')",
                (task_id,),
            ).fetchall()
            any_rows = conn.execute(
                "SELECT 1 FROM delegations WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            if not any_rows:
                return {"delegations_closed": 0}

            ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT notes, updated_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            cur_notes = (row["notes"] or "") if row else ""
            if "[review: merged" not in cur_notes:
                updated_notes = f"{cur_notes}\n[review: merged @ {ts}]".strip()
                conn.execute(
                    "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
                    (
                        updated_notes,
                        storage.next_revision(row["updated_at"] if row else None),
                        task_id,
                    ),
                )
            if open_rows:
                conn.execute(
                    "UPDATE delegations SET status='completed', callback_result='merged', "
                    "completed_at=?, updated_at=? "
                    "WHERE task_id=? AND status IN ('pending', 'in_flight')",
                    (storage.now(), storage.now(), task_id),
                )
            if expected is not None:
                advanced = run_records._direct_completion_snapshot_on(
                    conn, conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                )
            conn.commit()
            if expected is not None and run_records.scoped_direct_completion(task_id) == expected:
                run_records._direct_completion_scope.set(advanced)
            return {"delegations_closed": len(open_rows)}
        finally:
            conn.close()
    except Exception as e:
        print(f"Warning: delegation ledger closeout failed: {e}", file=sys.stderr)
        return {"error": str(e)}


#: Statuses an in-flight auto-merge may run against (task #1026 review fix).
#: A ``done`` task needs no relaxation; anything else here — ``open``,
#: ``cancelled``, ``waiting_review`` — is either not started or deliberately
#: parked, and must not be merged by an automated caller.
IN_FLIGHT_MERGEABLE_STATUSES = ("doing", "blocked")


def _task_merge_mission(task_id: int) -> int | None:
    """Resolve only the stored single-valued goal edge; unreadable authority denies."""
    try:
        conn = storage.connect()
        try:
            task = conn.execute("SELECT project FROM tasks WHERE id=?", (task_id,)).fetchone()
            # The goal must share the task row's tenant (#2014), not the
            # caller's: merges run from untenanted cron. A link to another
            # tenant's goal reads as an unknown goal and denies below; it
            # never reads as no link, which would take the ungoverned path.
            link = conn.execute(
                "SELECT l.goal_id, g.id, g.project FROM task_goal_links l "
                "JOIN tasks t ON t.id=l.task_id "
                "LEFT JOIN goals g ON g.id=l.goal_id AND g.org_id IS t.org_id "
                "WHERE l.task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise ToolError("merge task authority no longer exists")
            if link is None:
                return None
            if link["id"] is None:
                raise ToolError("merge task links to an unknown mission goal")
            project = (task["project"] or "unified-agent").strip() or "unified-agent"
            if project != link["project"]:
                raise ToolError("merge task and mission goal belong to different projects")
            return int(link["goal_id"])
        finally:
            conn.close()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("cannot verify merge task mission authority") from exc


def _merge_action_context(task_id: int, worktree: dict, repo: str | None) -> dict:
    """Bind review to a trusted repository, attached worktree and exact branch commit."""
    repo = str(Path(_known_repo_or_raise(repo or str(config.workspace_root()))).resolve())
    wt_path = str(resolve_task_worktree(worktree["path"]))
    branch = worktree.get("branch")
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ToolError("merge requires a named source branch")

    def read(args, cwd):
        result = _run_git_in_repo(args, cwd)
        if result.returncode != 0:
            raise ToolError("cannot verify merge source repository and branch")
        return result.stdout.strip()

    # A registered path alone is insufficient: prove it still belongs to this
    # repository and is still attached to the branch the task will merge.
    common = read(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    wt_common = read(["rev-parse", "--path-format=absolute", "--git-common-dir"], wt_path)
    if Path(common).resolve() != Path(wt_common).resolve():
        raise ToolError("merge worktree no longer belongs to the trusted repository")
    if read(["symbolic-ref", "--quiet", "--short", "HEAD"], wt_path) != branch:
        raise ToolError("merge worktree is no longer attached to the task source branch")
    head = read(["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], repo)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ToolError("merge source has no verifiable exact commit")
    try:
        conn = storage.connect()
        try:
            task = conn.execute(
                "SELECT current_run_id, status, cancel_requested, claimed_by FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise ToolError("cannot verify current merge task run authority") from exc
    if task is None:
        raise ToolError("merge task authority no longer exists")
    if task["cancel_requested"]:
        raise ToolError("merge task run has a cancellation request")
    run_id = task["current_run_id"]
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ToolError("merge task current run authority is malformed")
    return {
        "task_id": task_id,
        "repo": repo,
        "branch": branch,
        "head": head,
        "policy_fingerprint": trust_presets._fingerprint(policy.load_policy()),
        "current_run_id": run_id,
        "task_status": task["status"],
        "task_claimant": task["claimed_by"],
    }


def _merge_caller_authority(
    task_id: int, force: bool, allow_in_flight: bool, *, dry_run: bool = False
) -> str:
    """Recheck current denial-only admission without consuming wrapper approval again."""
    from .safety import check_prod_action_gate

    actor = config.verified_agent_identity()
    if not actor:
        raise ToolError("mission-linked merges require a verified caller identity")
    if not profiles.tool_is_allowed("git_merge_task"):
        raise ToolError("current profile denies git_merge_task")
    agent_access.enforce("git_merge_task", read_only=dry_run, production=not dry_run)
    payload = {
        "task_id": task_id,
        "force": force,
        "allow_in_flight": allow_in_flight,
        "dry_run": True,
    }
    check_prod_action_gate("git_merge_task", payload)
    # Current central rules re-evaluate, but invocation scopes share the exact
    # wrapper approval payload and rate reservation for this one action.
    from .safety import check_policy_gate

    check_policy_gate(
        "git_merge_task",
        {
            "task_id": task_id,
            "force": force,
            "allow_in_flight": allow_in_flight,
            "dry_run": dry_run,
        },
    )
    trust_presets.check_call("git_merge_task", read_only=False, payload=payload)
    return actor


def _paused_merge(task_id: int, decision: dict) -> dict:
    if decision["decision"] == "deny":
        raise ToolError(f"mission autonomy denied merge: {decision['reason']}")
    return {"success": False, "task_id": task_id, "autonomy": decision}


def _merge_start_admission(conn, task_id: int, mission_id: int, actor: str, context: dict) -> None:
    """Add exact task authority denial inside mark_started's owning transaction."""
    from .tools import approvals

    _merge_task_fence(conn, task_id, mission_id, context)
    if config.verified_agent_identity() != actor:
        raise ToolError("merge caller changed at execution admission")
    if trust_presets._fingerprint(policy.load_policy()) != context["policy_fingerprint"]:
        raise ToolError("merge policy changed at execution admission")
    approvals.revalidate_invocation_approvals(conn)


@contextmanager
def _lock_merge_source(context: dict):
    """Use Git's native ref lock to serialize tip validation and closeout.

    Git writers acquire this same lock (including for packed refs). Keep it
    through the SQLite closeout commit; delete the branch afterward by SHA CAS.
    Never replace/remove another process's existing lock.
    """
    repo, branch = context["repo"], context["branch"]
    checked = _run_git_in_repo(["check-ref-format", f"refs/heads/{branch}"], repo)
    if checked.returncode != 0:
        raise ToolError("merge source branch is not a valid Git ref")
    located = _run_git_in_repo(["rev-parse", "--git-path", f"refs/heads/{branch}.lock"], repo)
    if located.returncode != 0:
        raise ToolError("merge source ref lock could not be located")
    lock = Path(located.stdout.removesuffix("\n"))
    if not lock.is_absolute():
        lock = Path(repo) / lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    identity = os.fstat(fd)
    try:
        yield
    finally:
        os.close(fd)
        try:
            held = lock.stat(follow_symlinks=False)
            if (held.st_dev, held.st_ino) == (identity.st_dev, identity.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def _merge_task_fence(conn, task_id, mission_id, context):
    from . import workflow_roles

    if workflow_roles.is_workflow_task(conn, task_id):
        raise ToolError("workflow task requires an explicit workflow_run closure")
    row = conn.execute(
        "SELECT t.*, l.goal_id, g.id AS goal_exists, g.project AS goal_project "
        "FROM tasks t LEFT JOIN task_goal_links l ON l.task_id=t.id "
        "LEFT JOIN goals g ON g.id=l.goal_id AND g.org_id IS t.org_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    if row is None or row["goal_exists"] is None or row["goal_id"] != mission_id:
        raise ToolError("merge mission linkage changed at execution admission")
    project = (row["project"] or "unified-agent").strip() or "unified-agent"
    if project != row["goal_project"]:
        raise ToolError("merge mission project changed at execution admission")
    if row["cancel_requested"]:
        raise ToolError("merge cancellation requested at execution admission")
    if (row["current_run_id"], row["status"], row["claimed_by"]) != (
        context["current_run_id"],
        context["task_status"],
        context["task_claimant"],
    ):
        raise ToolError("merge task run/status/claimant changed at execution admission")
    return row


def _merge_delegations(conn, task_id):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, to_agent, claim_generation, status FROM delegations "
            "WHERE task_id=? AND status IN ('pending', 'in_flight') ORDER BY id",
            (task_id,),
        ).fetchall()
    ]


def _mission_merge_closeout(
    task_id, mission_id, actor, context, wt_path, commit_hash, admitted_delegations
):
    """Close only the admitted task/run/source/delegation generation.

    The SQLite writer fence and native source-ref lock overlap through commit.
    Exact merge evidence is separate from mutable successor task state. A
    preexisting done task with retained work reopens only under its own CAS.
    """
    from . import events, run_records

    conn = storage.connect()
    tx = run_records.attach(conn)
    completed = closeout_committed = False
    skipped = branch_retained = None
    delegation_closeout = {"delegations_closed": 0}
    publish = reopened = None
    review_scope = run_records.scoped_direct_completion(task_id)
    completed_snapshot = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _merge_task_fence(conn, task_id, mission_id, context)
        except ToolError:
            row = None
            skipped = "task authority changed after admission"
        if row is not None:
            try:
                with _lock_merge_source(context):
                    current_wt, current_repo = find_task_worktree(task_id)
                    if (
                        current_wt is None
                        or current_wt["path"] != wt_path
                        or _merge_action_context(task_id, current_wt, current_repo) != context
                    ):
                        skipped = "source changed after admission"
                    elif _merge_delegations(conn, task_id) != admitted_delegations:
                        skipped = "delegation ownership changed after admission"
                    else:
                        open_run = run_records._open_run_on(conn, task_id)
                        if (open_run["run_id"] if open_run else None) != context["current_run_id"]:
                            skipped = "run ledger ownership changed after admission"
                        else:
                            conn.execute("SAVEPOINT merge_closeout")
                            try:
                                notes = f"{row['notes'] or ''}\n[merged source: {context['head']}; commit: {commit_hash}]".strip()
                                updated = conn.execute(
                                    "UPDATE tasks SET status='done', notes=?, updated_at=?, completed_at=? "
                                    "WHERE id=? AND current_run_id IS ? AND status=? "
                                    "AND claimed_by IS ? AND cancel_requested=0",
                                    (
                                        notes,
                                        storage.next_revision(row["updated_at"]),
                                        storage.now(),
                                        task_id,
                                        context["current_run_id"],
                                        context["task_status"],
                                        context["task_claimant"],
                                    ),
                                )
                                if updated.rowcount != 1:
                                    raise ToolError("task closeout was superseded")
                                run_records.record_transition_in_tx(
                                    tx,
                                    task_id,
                                    old_status=context["task_status"],
                                    new_status="done",
                                    actor=actor,
                                    claimed_by=context["task_claimant"],
                                )
                                for delegation in admitted_delegations:
                                    changed = conn.execute(
                                        "UPDATE delegations SET status='completed', callback_result='merged', completed_at=?, updated_at=? "
                                        "WHERE id=? AND task_id=? AND claim_generation=? AND status=? AND to_agent=?",
                                        (
                                            storage.now(),
                                            storage.now(),
                                            delegation["id"],
                                            task_id,
                                            delegation["claim_generation"],
                                            delegation["status"],
                                            delegation["to_agent"],
                                        ),
                                    )
                                    if changed.rowcount != 1:
                                        raise ToolError("delegation closeout was superseded")
                                # Uncommitted work is successor work; it keeps the task open.
                                # Git removal itself waits until closeout evidence is durable.
                                dirty = _run_git_in_repo(["status", "--porcelain"], wt_path)
                                if dirty.returncode != 0 or dirty.stdout.strip():
                                    raise ToolError("source worktree has uncommitted work")
                                _merge_task_fence(
                                    conn,
                                    task_id,
                                    mission_id,
                                    {
                                        **context,
                                        "task_status": "done",
                                        "current_run_id": None
                                        if open_run
                                        else context["current_run_id"],
                                    },
                                )
                                completed = True
                                delegation_closeout = {
                                    "delegations_closed": len(admitted_delegations)
                                }
                                conn.execute("RELEASE SAVEPOINT merge_closeout")
                            except Exception as exc:
                                conn.execute("ROLLBACK TO SAVEPOINT merge_closeout")
                                conn.execute("RELEASE SAVEPOINT merge_closeout")
                                tx.info_pending_publish.clear()
                                tx.info_changed_runs.clear()
                                tx.info_traces.clear()
                                tx.info_completion_returns.clear()
                                tx.info_ready_notices.clear()
                                skipped = f"source cleanup or completion bookkeeping failed: {exc}"
                    if not completed:
                        _retain_merge_work(conn, task_id, context, row, commit_hash, skipped)
                    publish = _record_local_merge(
                        conn, task_id, actor, context, commit_hash, completed, skipped
                    )
                    if completed and review_scope is not None:
                        completed_snapshot = run_records._direct_completion_snapshot_on(
                            conn,
                            conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(),
                        )
                    conn.commit()  # Native ref lock still prevents a newer tip.
                    closeout_committed = completed
                    if completed:
                        # Reacquire the task writer fence after durable closeout.
                        # A successor admitted in that gap owns this checkout.
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            _merge_cleanup_fence(
                                conn, task_id, mission_id, context, open_run, admitted_delegations
                            )
                        except ToolError as exc:
                            completed = False
                            skipped = str(exc)
                            conn.rollback()
                        else:
                            try:
                                _worktree_remove_in_repo(wt_path, context["repo"], force=False)
                                # A retained checkout must stay visible for review.
                                if admitted_delegations:
                                    notes = (
                                        conn.execute(
                                            "SELECT notes FROM tasks WHERE id=?", (task_id,)
                                        ).fetchone()["notes"]
                                        or ""
                                    )
                                    if "[review: merged" not in notes:
                                        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                                        # A transaction after the durable closeout:
                                        # the note needs its own revision (#2087).
                                        conn.execute(
                                            "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
                                            (
                                                f"{notes}\n[review: merged @ {ts}]",
                                                storage.next_task_revision(conn, task_id),
                                                task_id,
                                            ),
                                        )
                            except ToolError as exc:
                                completed = False
                                skipped = f"source worktree retained after closeout: {exc}"
                                reopened = _reopen_after_failed_cleanup(
                                    conn, task_id, actor, context, open_run, commit_hash, skipped
                                )
                            else:
                                if review_scope is not None:
                                    completed_snapshot = run_records._direct_completion_snapshot_on(
                                        conn,
                                        conn.execute(
                                            "SELECT * FROM tasks WHERE id=?", (task_id,)
                                        ).fetchone(),
                                    )
                                conn.commit()
            except (OSError, ToolError) as exc:
                if not conn.in_transaction:
                    raise
                skipped = f"source identity could not be fenced: {exc}"
                _retain_merge_work(conn, task_id, context, row, commit_hash, skipped)
                publish = _record_local_merge(
                    conn, task_id, actor, context, commit_hash, False, skipped
                )
                conn.commit()
        else:
            publish = _record_local_merge(
                conn, task_id, actor, context, commit_hash, False, skipped
            )
            conn.commit()
    finally:
        conn.close()
    if closeout_committed:
        run_records.flush(tx)
    if completed:
        # Deleting the source ref also requires current task authority, even
        # after releasing the native ref lock. The SHA CAS fences Git writers.
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _merge_cleanup_fence(
                    conn, task_id, mission_id, context, open_run, admitted_delegations
                )
            except ToolError as exc:
                branch_retained = str(exc)
            else:
                deleted = _run_git_in_repo(
                    ["update-ref", "-d", f"refs/heads/{context['branch']}", context["head"]],
                    context["repo"],
                )
                if deleted.returncode != 0:
                    branch_retained = "expected-SHA deletion refused"
                    print(
                        "Warning: source branch retained; expected-SHA deletion refused",
                        file=sys.stderr,
                    )
        finally:
            conn.rollback()
            conn.close()
    for payload in (publish, reopened):
        if payload is not None:
            try:
                events.publish(payload)
            except Exception:
                pass
    if completed and completed_snapshot is not None:
        if run_records.scoped_direct_completion(task_id) == review_scope:
            # Only advance to our own fenced writes captured before commit.
            # A later successor is never recaptured as the reviewer's identity.
            run_records._direct_completion_scope.set(completed_snapshot)
    return completed, skipped, delegation_closeout, branch_retained


def _merge_cleanup_fence(conn, task_id, mission_id, context, open_run, admitted_delegations):
    """Require the exact durable closeout while holding BEGIN IMMEDIATE."""
    _merge_task_fence(
        conn,
        task_id,
        mission_id,
        {
            **context,
            "task_status": "done",
            "current_run_id": None if open_run else context["current_run_id"],
        },
    )
    if _merge_delegations(conn, task_id):
        raise ToolError("delegation authority changed before source cleanup")
    for delegation in admitted_delegations:
        row = conn.execute(
            "SELECT claim_generation, status, to_agent, callback_result FROM delegations "
            "WHERE id=? AND task_id=?",
            (delegation["id"], task_id),
        ).fetchone()
        if row is None or tuple(row) != (
            delegation["claim_generation"],
            "completed",
            delegation["to_agent"],
            "merged",
        ):
            raise ToolError("delegation authority changed before source cleanup")


def _reopen_after_failed_cleanup(conn, task_id, actor, context, open_run, commit_hash, reason):
    """Reopen a just-closed task whose source checkout could not be removed.

    Only the closeout this call committed is reopened: the CAS matches the
    exact post-closeout status, claimant and run pointer. A second merge
    evidence event records that the task was not left complete.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT notes, updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    notes = f"{row['notes'] or ''}\n[outstanding: {reason}]".strip() if row else ""
    updated = conn.execute(
        "UPDATE tasks SET status='waiting_review', notes=?, updated_at=?, completed_at=NULL "
        "WHERE id=? AND status='done' AND claimed_by IS ? AND current_run_id IS ?",
        (
            notes,
            storage.next_revision(row["updated_at"] if row else None),
            task_id,
            context["task_claimant"],
            None if open_run else context["current_run_id"],
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return None
    publish = _record_local_merge(conn, task_id, actor, context, commit_hash, False, reason)
    conn.commit()
    print(
        f"Warning: task {task_id} reopened; {reason} (merge commit {commit_hash})", file=sys.stderr
    )
    return publish


def _retain_merge_work(conn, task_id, context, row, commit_hash, reason):
    notes = f"{row['notes'] or ''}\n[merged source: {context['head']}; commit: {commit_hash}; outstanding: {reason}]".strip()
    conn.execute(
        "UPDATE tasks SET status=CASE WHEN status='done' THEN 'waiting_review' ELSE status END, "
        "notes=?, updated_at=?, completed_at=CASE WHEN status='done' THEN NULL ELSE completed_at END "
        "WHERE id=? AND current_run_id IS ? AND status=? AND claimed_by IS ? AND cancel_requested=0",
        (
            notes,
            storage.next_task_revision(conn, task_id),
            task_id,
            context["current_run_id"],
            context["task_status"],
            context["task_claimant"],
        ),
    )


def _record_local_merge(conn, task_id, actor, context, commit_hash, completed, skipped):
    from . import events

    return events.record_event_once_on_connection(
        conn,
        "agent.merge.local",
        payload={
            "task_id": task_id,
            "merged_source_head": context["head"],
            "merge_commit": commit_hash,
            "admitted_run_id": context["current_run_id"],
            "admitted_task_status": context["task_status"],
            "task_completed": completed,
            "closeout_skipped": skipped,
        },
        originator=actor,
    )[2]


def orchestrate_merge(
    task_id: int, force: bool = False, dry_run: bool = False, allow_in_flight: bool = False
) -> dict:
    """Enforce central policy and mission admission before local task merge IO."""
    from .safety import check_policy_gate
    from .tools import approvals

    with (
        gateway.invocation("git_merge_task"),
        approvals.approval_invocation(join_existing=True, restore_unstarted=True),
    ):
        try:
            check_policy_gate(
                "git_merge_task",
                {
                    "task_id": task_id,
                    "force": force,
                    "dry_run": dry_run,
                    "allow_in_flight": allow_in_flight,
                },
            )
            return _orchestrate_merge(task_id, force, dry_run, allow_in_flight)
        finally:
            if not approvals.governed_execution_started():
                gateway.release_unprogressed_invocation()


def _legacy_cleanup_skip_reason(
    task_id: int, wt_path: str, wt_branch: str, pinned_head: str
) -> str | None:
    """Why legacy cleanup must retain the source, or None if it is still the merged commit."""
    from .cycle_eligibility import _head_sha

    try:
        current, _repo = find_task_worktree(task_id)
    except Exception as exc:
        return f"source identity could not be fenced: {exc}"
    if (
        current is None
        or current["path"] != wt_path
        or current.get("branch") != wt_branch
        or (current.get("head") or _head_sha(wt_path)) != pinned_head
    ):
        return "source changed after admission"
    return None


def _branch_delete_skip_reason(branch: str, pinned_head: str, repo: str, force: bool) -> str | None:
    """Apply the checks `git branch -d/-D` make and `update-ref -d` does not.

    Neither form deletes a branch checked out in a worktree. Only force waives
    the requirement that the commit is still merged.
    """
    worktrees = _run_git_in_repo(["worktree", "list", "--porcelain"], repo)
    if worktrees.returncode != 0:
        return "source branch checkouts could not be verified"
    if f"branch refs/heads/{branch}" in worktrees.stdout.splitlines():
        return "source branch is checked out in another worktree"
    if not force:
        contained = _run_git_in_repo(
            ["merge-base", "--is-ancestor", pinned_head, "refs/heads/main"], repo
        )
        if contained.returncode != 0:
            return "source commit is no longer contained in main"
    return None


def _orchestrate_merge(
    task_id: int,
    force: bool = False,
    dry_run: bool = False,
    allow_in_flight: bool = False,
) -> dict:
    """Orchestrate validation, merging, and worktree/branch cleanup for a task branch.

    ``allow_in_flight`` relaxes **only** the ``status == 'done'`` precondition,
    and only for the statuses in ``IN_FLIGHT_MERGEABLE_STATUSES``. Every other
    safeguard is untouched: ``run_validation_gates`` still runs with
    ``force=False``, so the clean-worktree check, the security audit and the
    test suite all still have to pass, and worktree removal still runs without
    ``--force``.

    It exists because the reviewer's ``+yolo`` delivery path (task #1026)
    merges work that is green but still ``doing``/``blocked`` — review runs
    *before* the task is marked done, so every yolo merge hit the status guard
    and failed. Widening ``force`` to cover that case would have bought the
    status relaxation at the price of the audit and test gates, which is
    exactly the trade this flag exists to avoid.
    """
    # 1. Query the task from memory.db
    conn = storage.connect()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    if not task:
        raise ValueError(f"Task {task_id} does not exist in memory.db.")

    from . import run_records

    completion = run_records.capture_direct_completion(task_id, task)
    inherited = run_records.scoped_direct_completion(task_id)
    if completion is None or (inherited is not None and completion != inherited):
        raise run_records.DirectCompletionChanged(
            "task completion refused: orchestration identity changed"
        )

    status = task["status"]
    title = task["title"]

    if status != "done" and not force:
        if not (allow_in_flight and status in IN_FLIGHT_MERGEABLE_STATUSES):
            hint = "Use force=True to override."
            if allow_in_flight:
                hint = (
                    "allow_in_flight=True only covers "
                    f"{', '.join(IN_FLIGHT_MERGEABLE_STATUSES)}. {hint}"
                )
            raise ValueError(f"Task {task_id} status is '{status}', not 'done'. {hint}")

    # 2. Find worktree matching task_id
    task_wt, main_repo = find_task_worktree(task_id)
    if not task_wt:
        raise ValueError(f"Could not locate git worktree for Task {task_id}.")

    wt_path = task_wt["path"]
    wt_branch = task_wt["branch"]

    # Mission authority comes from the stored goal edge, never task notes,
    # the claimant, or caller-supplied labels. No link retains the legacy path.
    mission_id = _task_merge_mission(task_id)
    action_context = None
    actor = None
    decision = None
    if mission_id is not None:
        action_context = _merge_action_context(task_id, task_wt, main_repo)
        if (
            action_context["current_run_id"] != task["current_run_id"]
            or action_context["task_status"] != status
        ):
            raise ToolError(
                "merge task run/status changed before mission admission; retry for fresh review"
            )
        actor = _merge_caller_authority(task_id, force, allow_in_flight, dry_run=dry_run)
        decide = autonomy_modes.preview_action if dry_run else autonomy_modes.evaluate
        decision = decide(mission_id, "merge", actor, action_context=action_context)
        if not dry_run and decision["decision"] != "proceed":
            return _paused_merge(task_id, decision)

    # Legacy (unlinked) tasks pin the source commit before the gates, as the
    # mission path does through action_context["head"]: the merge lands this
    # exact commit, never a branch name a commit made during validation moved.
    from .cycle_eligibility import _head_sha

    pinned_head = None
    if action_context is None and not dry_run:
        pinned_head = task_wt.get("head") or _head_sha(wt_path)
        if not pinned_head:
            raise ToolError(f"cannot pin the head commit of task {task_id}'s worktree; retry")
        scope = _REVIEW_GATE_SCOPE.get()
        if scope and scope["task_id"] == task_id and scope["head"] != pinned_head:
            # The reviewer classified red-tier files at its own head; any other
            # head is unreviewed, so the merge must not land it.
            raise ToolError(
                f"task {task_id} head {pinned_head[:12]} is not the reviewed head "
                f"{str(scope['head'])[:12]}; re-run review on the new head"
            )

    # 3-4. Clean-tree check + security audit + tests (shared with reviewer.py)
    run_validation_gates(task_id, wt_path, force=force, **_reviewed_gate_kwargs(task_id, wt_path))

    if dry_run:
        result = {"success": True, "dry_run": True}
        if decision is not None:
            result["autonomy"] = decision
        return result

    # Validation may execute arbitrary project tests and take substantial time.
    # Re-read authority and source immediately before the FIRST git mutation,
    # including checkout/pull, and let mark_started atomically recheck current
    # lifecycle, budget, mode, grants and exact-action human acknowledgment.
    if _task_merge_mission(task_id) != mission_id:
        raise ToolError("merge mission linkage changed during validation; retry for fresh review")
    main_repo = _known_repo_or_raise(main_repo or str(config.workspace_root()))
    admitted_delegations = []

    def checkout_main(conn=None):
        from .tools import approvals

        approvals.mark_governed_execution()
        gateway.mark_execution_started()
        co_res = _run_git_in_repo(["checkout", "main"], main_repo)
        if co_res.returncode != 0:
            raise RuntimeError(f"Error checking out main: {co_res.stderr.strip()}")

    def start_admission(conn):
        _merge_start_admission(conn, task_id, mission_id, actor, current_context)
        current_delegations = _merge_delegations(conn, task_id)
        if not admitted_delegations:
            admitted_delegations.extend(current_delegations)
        elif admitted_delegations != current_delegations:
            raise ToolError("delegation ownership changed at execution admission")

    if mission_id is not None:
        current_actor = _merge_caller_authority(task_id, force, allow_in_flight)
        if current_actor != actor:
            raise ToolError("merge caller changed during validation; fresh review required")
        current_wt, current_repo = find_task_worktree(task_id)
        if current_wt is None:
            raise ToolError("merge source worktree disappeared during validation")
        current_context = _merge_action_context(task_id, current_wt, current_repo)
        if current_context != action_context or current_wt["path"] != wt_path:
            raise ToolError(
                "merge source, policy or task run changed during validation; fresh validation and review required"
            )
        from .safety import check_prod_action_gate

        check_prod_action_gate(
            "git_merge_task",
            {
                "task_id": task_id,
                "force": force,
                "dry_run": False,
                "allow_in_flight": allow_in_flight,
            },
        )
        autonomy_modes.mark_started(
            decision["pending_id"],
            actor=actor,
            action_context=current_context,
            _admission_check=start_admission,
            _start_execution=checkout_main,
        )

    else:
        from .safety import check_prod_action_gate

        check_prod_action_gate(
            "git_merge_task",
            {
                "task_id": task_id,
                "force": force,
                "dry_run": False,
                "allow_in_flight": allow_in_flight,
            },
        )

    # Mission-bound checkout already ran under mark_started's owning fence.
    if action_context is None:
        current_wt, _repo = find_task_worktree(task_id)
        if (
            current_wt is None
            or current_wt["path"] != wt_path
            or (current_wt.get("head") or _head_sha(wt_path)) != pinned_head
        ):
            raise ToolError(
                f"task {task_id} source moved during validation; nothing merged. "
                "Re-run review/merge on the new head."
            )
        if run_records.capture_direct_completion(task_id) != completion:
            raise run_records.DirectCompletionChanged(
                "task completion refused: orchestration revision/run was superseded"
            )
        checkout_main()

    pull_res = _run_git_in_repo(["pull"], main_repo)
    if pull_res.returncode != 0:
        # A main with no upstream (local-only delivery) has nothing to pull;
        # a tracked main that failed to pull is stale or conflicted.
        upstream = _run_git_in_repo(["rev-parse", "--abbrev-ref", "main@{upstream}"], main_repo)
        if upstream.returncode == 0:
            raise RuntimeError(f"Error pulling main: {pull_res.stderr.strip()}")

    commit_msg = f"feat(merge): merge task {task_id}: {title}"
    # Merge the reviewed object, not a branch name that can advance after the
    # final authority check.
    merge_source = action_context["head"] if action_context is not None else pinned_head
    if action_context is None:
        merge_res = _run_git_in_repo(["merge", merge_source, "-m", commit_msg], main_repo)
    else:
        # The merge itself also runs under a task write fence: a cancellation,
        # takeover, delegation change or approval revocation that committed
        # after checkout stops the merge rather than only its closeout.
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            start_admission(conn)
            merge_res = _run_git_in_repo(["merge", merge_source, "-m", commit_msg], main_repo)
            conn.commit()
        finally:
            conn.close()
    if merge_res.returncode != 0:
        raise RuntimeError(f"Error merging branch: {merge_res.stderr.strip()}")

    log_res = _run_git_in_repo(["log", "-n", "1", "--pretty=format:%h"], main_repo)
    commit_hash = log_res.stdout.strip() if log_res.returncode == 0 else "unknown"

    task_completed = True
    branch_retained = None
    if action_context is not None:
        task_completed, cleanup_skipped, delegation_closeout, branch_retained = (
            _mission_merge_closeout(
                task_id,
                mission_id,
                actor,
                action_context,
                wt_path,
                commit_hash,
                admitted_delegations,
            )
        )
    else:
        # Refuse superseded completion before removing the successor's checkout.
        note = (
            f"[merged commit: {commit_hash} @ {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}]"
        )
        if not run_records.complete_direct(completion, actor="merge-orchestrator", note=note):
            raise run_records.DirectCompletionChanged(
                "task completion refused: orchestration revision/run was superseded"
            )

        # 6. Cleanup Worktree and Branch. A source that advanced after admission
        # was not merged/reviewed by this action and must remain for its owner.
        cleanup_skipped = _legacy_cleanup_skip_reason(task_id, wt_path, wt_branch, pinned_head)
        if cleanup_skipped is not None:
            print(f"Warning: source retained; {cleanup_skipped}", file=sys.stderr)
        else:
            try:
                _worktree_remove_in_repo(wt_path, main_repo, force=force)
            except Exception as e:
                # update-ref, unlike `git branch -d`, would delete a branch that
                # is still checked out, so the retained worktree keeps its ref.
                cleanup_skipped = f"source worktree retained: {e}"
                print(f"Warning removing worktree: {e}", file=sys.stderr)
            else:
                cleanup_skipped = _branch_delete_skip_reason(
                    wt_branch, pinned_head, main_repo, force
                )
                if cleanup_skipped is not None:
                    print(f"Warning: source branch retained; {cleanup_skipped}", file=sys.stderr)
                else:
                    # The SHA CAS covers a commit landing after the checks above.
                    deleted = _run_git_in_repo(
                        ["update-ref", "-d", f"refs/heads/{wt_branch}", pinned_head], main_repo
                    )
                    if deleted.returncode != 0:
                        branch_retained = "expected-SHA deletion refused"
                        print(
                            "Warning: source branch retained; expected-SHA deletion refused",
                            file=sys.stderr,
                        )

    sync_state_to_vault()

    # 8. Post-merge verification workflow (#905). Non-fatal; opt out with
    #    UA_POST_MERGE_VERIFY=0 on hosts that rely on GitHub CI exclusively.
    workflow_res = _run_post_merge_verify(task_id)

    # 9. Delegation ledger closeout (#908)
    if action_context is None:
        delegation_closeout = _close_delegation_ledger(task_id)

    return {
        "success": True,
        "commit_hash": commit_hash,
        "task_id": task_id,
        "branch": wt_branch,
        **(
            {"merged_source_head": action_context["head"], "task_completed": task_completed}
            if action_context is not None
            else {}
        ),
        "post_merge_verification": workflow_res,
        "delegation_closeout": delegation_closeout,
        **({"cleanup_skipped": cleanup_skipped} if cleanup_skipped is not None else {}),
        **({"branch_retained": branch_retained} if branch_retained is not None else {}),
    }


def main():
    args = parse_args()
    print(f"🚀 Starting merge orchestration for Task {args.task_id}...")
    try:
        post_init.register_default_hooks()
        storage.init_db()
        res = orchestrate_merge(args.task_id, force=args.force, dry_run=args.dry_run)
        if res.get("dry_run"):
            print("✔ Dry run validation complete. Exiting without merging.")
        elif not res.get("success"):
            decision = res.get("autonomy", {})
            print(
                f"Merge {decision.get('decision', 'paused')}: {decision.get('reason', 'review required')}"
            )
            print(f"Pending autonomy review ID: {decision.get('pending_id', 'unknown')}")
            sys.exit(2)
        else:
            print(
                f"🎉 Merge orchestration completed successfully! Commit hash: {res['commit_hash']}"
            )
            if res.get("task_completed") is False:
                print(
                    f"Task remains outstanding: {res.get('cleanup_skipped', 'closeout deferred')}"
                )
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


def _orchestrate(**kwargs):
    # Looks the name up at call time, so a patch of orchestrate_merge holds.
    return orchestrate_merge(**kwargs)


# The kernel's git_merge_task calls this (#2450).
register_merge_orchestrator(_orchestrate)


if __name__ == "__main__":
    main()
