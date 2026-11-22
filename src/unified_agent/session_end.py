"""Session wrap-up & RESUME.md session end card generator (Task #159).

Generates a structured 3-bullet `RESUME.md` file at the project root for frictionless
context re-entry across all agents (Desktop_Agent1, Desktop_agent3, Laptop_Agent1, etc.).
"""

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import config, storage


def _get_git_status(repo_path: Path) -> dict:
    """Retrieve git status summary (branch, modified/uncommitted files, untracked files)."""
    info = {
        "branch": "main",
        "modified": [],
        "untracked": [],
        "clean": True,
        "is_repo": False,
    }
    try:
        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch_res.returncode == 0:
            info["is_repo"] = True
            b = branch_res.stdout.strip()
            if b:
                info["branch"] = b

        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status_res.returncode == 0:
            lines = [line_str for line_str in status_res.stdout.splitlines() if line_str.strip()]
            for line in lines:
                code = line[:2]
                filepath = line[3:].strip()
                if code == "??":
                    info["untracked"].append(filepath)
                else:
                    info["modified"].append(filepath)
            info["clean"] = len(lines) == 0
    except Exception:
        pass
    return info


def _get_tasks_summary() -> tuple[list[dict], list[dict]]:
    """Retrieve completed tasks from recent session and active doing/open tasks."""
    done_tasks = []
    active_tasks = []
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT id, title, status, claimed_by, updated_at, completed_at, notes "
                "FROM tasks ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
            for r in rows:
                task_dict = dict(r)
                if task_dict["status"] == "done":
                    # Include completed tasks (up to 10)
                    if len(done_tasks) < 10:
                        done_tasks.append(task_dict)
                elif task_dict["status"] in ("doing", "open"):
                    if len(active_tasks) < 10:
                        active_tasks.append(task_dict)
        finally:
            conn.close()
    except Exception:
        pass
    return done_tasks, active_tasks


def _get_test_linter_status(repo_path: Path, run_checks: bool = False) -> str:
    """Check test & linter status or report state."""
    if not run_checks:
        return "Tests/linter: status ready for verification (`uv run pytest` / `ruff check`)."
    from . import execution_locality

    if execution_locality.devices_only():
        # Task #2109: the checks run project code; a devices-only hub skips them.
        return "Tests/linter: not run on a devices-only hub (run them on a device)."

    broken = []
    try:
        pytest_res = subprocess.run(
            ["uv", "run", "pytest", "-q"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pytest_res.returncode != 0:
            broken.append("pytest failures detected")
    except Exception as e:
        broken.append(f"pytest check error: {e}")

    try:
        ruff_res = subprocess.run(
            ["uv", "run", "ruff", "check"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if ruff_res.returncode != 0:
            broken.append("ruff linter issues detected")
    except Exception as e:
        broken.append(f"ruff check error: {e}")

    if broken:
        return f"Broken: {', '.join(broken)}"
    return "All tests & linters passing (100% green)."


def resolve_repo_root(repo_path: Path | None = None) -> Path:
    """Resolve project root path via explicit argument, UA_WORKSPACE env, or git top-level."""
    if repo_path is not None:
        return Path(repo_path).expanduser().resolve()

    if "UA_WORKSPACE" in os.environ:
        return config.workspace_root().resolve()

    cwd = Path.cwd().resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except Exception:
        pass

    return config.workspace_root().resolve()


def generate_resume_content(repo_path: Path | None = None, run_checks: bool = False) -> str:
    """Generate structured 3-bullet RESUME.md markdown content."""
    repo_path = resolve_repo_root(repo_path)

    git_info = _get_git_status(repo_path)
    done_tasks, active_tasks = _get_tasks_summary()

    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # 1. Done Bullet
    if done_tasks:
        done_items = []
        for t in done_tasks[:5]:
            agent_str = f" [{t['claimed_by']}]" if t.get("claimed_by") else ""
            done_items.append(f"#{t['id']} ({t['title']}){agent_str}")
        done_text = f"Completed in session: {', '.join(done_items)}"
    else:
        done_text = "No tasks marked done in recent session window."

    # 2. State / Broken Bullet
    state_parts = []
    if git_info["is_repo"]:
        state_parts.append(f"Branch: `{git_info['branch']}`")
        if git_info["clean"]:
            state_parts.append("Git state: clean")
        else:
            mod_cnt = len(git_info["modified"])
            untrk_cnt = len(git_info["untracked"])
            state_parts.append(f"Git uncommitted: {mod_cnt} modified, {untrk_cnt} untracked")

    doing_tasks = [t for t in active_tasks if t["status"] == "doing"]
    if doing_tasks:
        dt = doing_tasks[0]
        agent_str = f" [{dt['claimed_by']}]" if dt.get("claimed_by") else ""
        state_parts.append(f"Active task: #{dt['id']} ({dt['title']}){agent_str}")
    elif active_tasks:
        at = active_tasks[0]
        state_parts.append(f"Next task: #{at['id']} ({at['title']})")

    check_status = _get_test_linter_status(repo_path, run_checks=run_checks)
    state_parts.append(check_status)

    state_text = " | ".join(state_parts)

    # 3. Next Action Bullet
    if doing_tasks:
        dt = doing_tasks[0]
        next_action_text = (
            f"Resume work on task #{dt['id']} (`{dt['title']}`). "
            f"Run `uv run pytest` and verify gates."
        )
    elif active_tasks:
        at = active_tasks[0]
        next_action_text = (
            f"Claim and start next task #{at['id']} (`{at['title']}`). Run `uv run pytest`."
        )
    else:
        next_action_text = "Run `uv run pytest` and check task queue (`task_list()`) for new work."

    content = (
        f"# RESUME — Session Wrap-Up ({now_str})\n\n"
        f"• Done: {done_text}\n"
        f"• State / Broken: {state_text}\n"
        f"• Next Action: {next_action_text}\n"
    )
    return content


def generate_resume_file(repo_path: Path | None = None, run_checks: bool = False) -> Path:
    """Generate and write RESUME.md to resolved repo_path / 'RESUME.md'."""
    repo_path = resolve_repo_root(repo_path)
    target = repo_path / "RESUME.md"
    content = generate_resume_content(repo_path=repo_path, run_checks=run_checks)
    target.write_text(content, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for unified-agent-session-end / ua session end."""
    parser = argparse.ArgumentParser(
        description="Session wrap-up tool: auto-generates RESUME.md at project root."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Target project root directory (defaults to workspace_root or git top-level)",
    )
    parser.add_argument(
        "--check-tests",
        action="store_true",
        help="Execute pytest and ruff checks to include in State / Broken bullet",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print RESUME.md output to stdout without writing file",
    )

    args = parser.parse_args(argv)
    target_path = resolve_repo_root(args.path)

    content = generate_resume_content(repo_path=target_path, run_checks=args.check_tests)

    if args.dry_run:
        sys.stdout.write(content)
        return 0

    outfile = generate_resume_file(repo_path=target_path, run_checks=args.check_tests)
    print(f"Wrote RESUME.md to {outfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
