"""Shared fixtures and configuration for E2E Test Suite (Requirements R1-R4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def e2e_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the fixture agent identity after the parent fixture isolates state."""
    monkeypatch.setenv("UA_AGENT_IDENTITY", "Desktop_Agent7")


@pytest.fixture
def mock_git_repo(tmp_path: Path) -> Path:
    """Create a standalone temporary Git repo with 'main' and 'devin/task-1327' branches."""
    repo_dir = tmp_path / "unified_agent_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "e2e@agent.internal"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "E2E Test Runner"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # Initial file on main
    (repo_dir / "README.md").write_text("# Unified Agent Test\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    return repo_dir
