"""The audit gates run the invoking checkout's own src, even from a worktree (#2204).

The shared venv installs `unified_agent` in editable mode from one checkout, so
its `unified-agent-audit` imports that checkout's code everywhere. These tests
build a throwaway repository with a linked worktree whose `src/unified_agent`
is a sentinel package, and check what the audit entry points import there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SENTINEL_CLI = """\
import json, os, subprocess, sys

import unified_agent

LABEL = {label!r}


def main(argv=None):
    child = subprocess.run(
        [sys.executable, "-c", "import unified_agent; print(unified_agent.__file__)"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    record = {{"label": LABEL, "file": unified_agent.__file__, "child": child, "argv": argv}}
    log = os.environ.get("AUDIT_SENTINEL_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(record) + "\\n")
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def _write_sentinel(checkout: Path, label: str) -> None:
    pkg = checkout / "src" / "unified_agent"
    (pkg / "audit").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "audit" / "__init__.py").write_text("")
    (pkg / "audit" / "cli.py").write_text(SENTINEL_CLI.format(label=label))


def _git_env(home: Path) -> dict[str, str]:
    """Git with no user or system config: hooks land only in the temp repository."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "PYTHONPATH"}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    return env


@pytest.fixture
def repo_with_worktree(tmp_path):
    """A main checkout and a linked worktree whose sentinel packages differ."""
    env = _git_env(tmp_path)
    main = tmp_path / "main"
    (main / "scripts").mkdir(parents=True)
    for name in ("run_audit.py", "install-hooks.sh", "check_staged_conflict_markers.py"):
        shutil.copy2(SCRIPTS / name, main / "scripts" / name)
    _write_sentinel(main, "main")

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git(main, "init", "-q", "--initial-branch=main", "--template=")
    git(main, "add", ".")
    git(main, "commit", "-qm", "base")
    worktree = tmp_path / "main" / ".worktrees" / "task"
    git(main, "worktree", "add", "-q", "-b", "task", str(worktree))
    _write_sentinel(worktree, "worktree")
    return main, worktree, env, git


def _records(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_installed_package_is_not_the_worktree(repo_with_worktree):
    """Precondition: without the entry point, the worktree's src is not imported."""
    _, worktree, env, _ = repo_with_worktree
    plain = subprocess.run(
        [sys.executable, "-c", "import unified_agent; print(unified_agent.__file__)"],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
    )
    assert str(worktree) not in plain.stdout


def test_run_audit_imports_the_worktrees_own_src(repo_with_worktree):
    _, worktree, env, _ = repo_with_worktree
    result = subprocess.run(
        [sys.executable, str(worktree / "scripts" / "run_audit.py"), ".", "--fail-on", "high"],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    [record] = _records(result.stdout)
    src = worktree / "src"
    assert record["label"] == "worktree"
    assert Path(record["file"]).resolve().is_relative_to(src.resolve())
    # A subprocess the audit starts imports the same checkout's code.
    assert Path(record["child"]).resolve().is_relative_to(src.resolve())
    assert record["argv"] == [".", "--fail-on", "high"]


def test_run_audit_refuses_when_the_checkout_has_no_package(tmp_path):
    checkout = tmp_path / "bare"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_audit.py", checkout / "scripts" / "run_audit.py")
    result = subprocess.run(
        [sys.executable, str(checkout / "scripts" / "run_audit.py"), "."],
        cwd=checkout,
        env=_git_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing to audit with another checkout's code" in result.stderr


@pytest.mark.parametrize("legacy_branch", [False, True], ids=["entry-point", "legacy-branch"])
def test_installed_pre_commit_hook_audits_the_committing_worktree(
    repo_with_worktree, tmp_path, legacy_branch
):
    main, worktree, env, git = repo_with_worktree
    # The main checkout's venv supplies the dependencies, as on a real machine
    # where linked worktrees have no `.venv` of their own.
    venv_python = main / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    venv_python.chmod(0o755)
    subprocess.run(
        ["bash", str(main / "scripts" / "install-hooks.sh")],
        cwd=main,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (main / ".git" / "hooks" / "pre-commit").is_file()

    log = tmp_path / "audit.jsonl"
    env = {**env, "AUDIT_SENTINEL_LOG": str(log)}
    (worktree / "change.txt").write_text("worktree change\n")
    if legacy_branch:
        # A branch cut before scripts/run_audit.py existed takes the hook's
        # PYTHONPATH fallback, which must import the same checkout's src.
        (worktree / "scripts" / "run_audit.py").unlink()
    git(worktree, "add", "-A")
    subprocess.run(
        ["git", "commit", "-qm", "worktree change"],
        cwd=worktree,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    [record] = _records(log.read_text())
    assert record["label"] == "worktree"
    assert Path(record["file"]).resolve().is_relative_to((worktree / "src").resolve())
    assert record["argv"] == [".", "--fail-on", "high"]
