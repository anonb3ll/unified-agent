"""Shared fixtures and hermetic test environment for E2E Test Suite (Requirements R1-R5)."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from unified_agent import (
    autonomy_modes,
    config,
    embeddings,
    project_bindings,
    skills_registry,
    storage,
)
from unified_agent.tools import approvals


@pytest.fixture(autouse=True)
def isolated_e2e_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Hermetic test environment for all Batch 2 E2E tests."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "ua_home"
    home.mkdir(parents=True, exist_ok=True)
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    dev_root = tmp_path / "dev"
    dev_root.mkdir(parents=True, exist_ok=True)
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.setenv("UA_DEV_ROOT", str(dev_root))
    monkeypatch.setenv("UA_SKILL_REGISTRY_ROOTS", str(skills_root))
    monkeypatch.setenv("UA_AGENT_IDENTITY", "Desktop_Agent7")
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    monkeypatch.setenv("UA_STATE_SYNC_DEBOUNCE", "0")
    monkeypatch.setenv("UA_ANOMALY_SERVICES", "")
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))

    # Disable host credential files and external models
    monkeypatch.setattr(config, "load_gemini_env", lambda: None)
    monkeypatch.setattr(embeddings, "generate_embedding", lambda *args, **kwargs: [0.0] * 768)

    # Clean isolated DB path
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()

    conn = storage.connect()
    try:
        project_bindings.ensure_schema(conn)
        skills_registry.ensure_schema(conn)
        autonomy_modes.ensure_schema(conn)
    finally:
        conn.close()

    yield


@pytest.fixture
def mock_git_repo(tmp_path: Path) -> Path:
    """Create a standalone temporary Git repo with 'main' branch."""
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

    (repo_dir / "README.md").write_text("# Unified Agent Test\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    return repo_dir


@contextmanager
def identity_context(label: str):
    """Temporarily switch the active verified agent identity."""
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def resolve_human_approval(
    approval_id: int, decision: str = "approve", approver: str = "Desktop_Person"
) -> dict:
    """Helper to record human approval on an open request."""
    with identity_context(approver):
        return approvals.approval_resolve(approval_id, decision)
