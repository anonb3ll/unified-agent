"""Preset boundaries intersect every existing filesystem resolver."""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety, storage
from unified_agent.tools.audit import security_audit
from unified_agent.tools.files import fs_list, fs_search, fs_write
from unified_agent.tools.vault import vault_briefing_digest, vault_list, vault_read, vault_search
from unified_agent.tools.vault_graph import vault_query


@pytest.fixture
def select_preset(tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    identity = config.agent_identity_var.set("Desktop_Agent2")

    def select(name):
        policy.write_text(f"version: 1\ntrust_presets:\n  default: {name}\n")
        return policy

    yield select
    config.agent_identity_var.reset(identity)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "dev" / "unified_agent"
    root.mkdir(parents=True)
    (root / "code.py").write_text("OUTSIDE_MARKER = 1\n")
    return root


@pytest.fixture
def active_worktree(project):
    root = project / ".worktrees" / "active"
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks(title, status, claimed_by, worktree_path, created_at, "
            "updated_at, lock_expires_at, current_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "active test claim",
                "doing",
                "Desktop_Agent2",
                str(root),
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(hours=1)).isoformat(),
                "test-run",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return root


@pytest.mark.parametrize("preset", ["inspection", "configured-workspace", "active-worktree"])
def test_project_read_cannot_widen_preset(select_preset, project, active_worktree, preset):
    select_preset(preset)
    with pytest.raises(ToolError):
        safety.resolve_for_project_read(str(project / "code.py"))


@pytest.mark.parametrize("family", ["audit", "vault"])
def test_broad_audit_and_vault_roots_cannot_widen_workspace_preset(
    select_preset, isolated_env, tmp_path, monkeypatch, family
):
    select_preset("configured-workspace")
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "note.md"
    note.write_text("OUTSIDE_MARKER\n")
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(root))
    resolver = safety.resolve_for_audit if family == "audit" else safety.resolve_in_vault
    with pytest.raises(ToolError):
        resolver(str(note))


def test_active_worktree_does_not_admit_cockpit_union(select_preset, isolated_env, active_worktree):
    select_preset("active-worktree")
    with pytest.raises(ToolError):
        safety.resolve_in_workspace(str(isolated_env / "note.md"))
    assert safety.resolve_in_workspace("code.py") == active_worktree / "code.py"


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_path_escape_into_existing_workspace_union_stays_denied(
    select_preset, isolated_env, active_worktree, kind
):
    select_preset("active-worktree")
    outside = isolated_env / "outside.md"
    outside.write_text("OUTSIDE_MARKER\n")
    if kind == "symlink":
        (active_worktree / "escape.md").symlink_to(outside)
        path = "escape.md"
    else:
        import os

        path = os.path.relpath(outside, active_worktree)
    with pytest.raises(ToolError):
        safety.resolve_in_workspace(path)


def test_recursive_search_skips_symlink_into_other_readable_project(
    select_preset, isolated_env, project
):
    select_preset("configured-workspace")
    (isolated_env / "escaped.py").symlink_to(project / "code.py")
    (isolated_env / "local.py").write_text("LOCAL_MARKER = 1\n")
    matches = fs_search("MARKER")["matches"]
    assert [match["line"] for match in matches] == ["LOCAL_MARKER = 1"]


def test_inspection_reads_but_rejects_actual_fs_write(select_preset, isolated_env):
    select_preset("inspection")
    target = isolated_env / "code.py"
    target.write_text("unchanged\n")
    assert safety.resolve_for_project_read("code.py") == target
    with pytest.raises(ToolError):
        fs_write("code.py", "changed\n")
    assert target.read_text() == "unchanged\n"


def test_configured_workspace_allows_write_but_not_active_policy_file(
    select_preset, isolated_env, monkeypatch
):
    policy = select_preset("configured-workspace")
    local_policy = isolated_env / "policy.yaml"
    local_policy.write_text(policy.read_text())
    monkeypatch.setenv("UA_POLICY_FILE", str(local_policy))
    assert fs_write("code.py", "written\n")["bytes_written"] == 8
    with pytest.raises(ToolError):
        fs_write("policy.yaml", "trust_presets: {}\n")
    assert "configured-workspace" in local_policy.read_text()


def test_vault_write_intent_is_enforced_when_vault_is_inside_workspace(
    select_preset, isolated_env, monkeypatch
):
    select_preset("inspection")
    vault = isolated_env / "vault"
    (vault / "00 - Inbox").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    target = "00 - Inbox/note.md"
    assert safety.resolve_in_vault(target) == vault / target
    with pytest.raises(ToolError):
        safety.resolve_in_vault(target, for_write=True)


@pytest.mark.parametrize("family", ["workspace", "project", "audit", "vault"])
def test_preset_never_restores_explicit_workspace_deny(
    select_preset, isolated_env, monkeypatch, family
):
    select_preset("configured-workspace")
    vault = isolated_env / "vault"
    vault.mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    resolvers = {
        "workspace": safety.resolve_in_workspace,
        "project": safety.resolve_for_project_read,
        "audit": safety.resolve_for_audit,
        "vault": safety.resolve_in_vault,
    }
    try:
        with pytest.raises(ToolError):
            resolvers[family](".")
    finally:
        config.workspace_root_var.reset(token)


@pytest.mark.parametrize("path", [".git/hooks/pre-commit", "service.env", "sub/secrets/notes.md"])
def test_existing_read_guards_precede_preset_allow(select_preset, path):
    select_preset("configured-workspace")
    with pytest.raises(ToolError, match="hook-execution|secret-bearing"):
        safety.resolve_for_project_read(path)


def test_no_preset_preserves_broad_project_audit_and_vault_reads(tmp_path, monkeypatch, project):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n")
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    monkeypatch.setenv("UA_AUDIT_ROOTS", str(project))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert safety.resolve_for_project_read(str(project / "code.py")) == project / "code.py"
    assert safety.resolve_for_audit(str(project / "code.py")) == project / "code.py"
    assert safety.resolve_in_vault(".") == vault


@pytest.mark.parametrize("kind", ["symlink", "glob-traversal"])
def test_file_listing_cannot_reveal_entries_outside_preset(
    select_preset, isolated_env, project, kind
):
    select_preset("configured-workspace")
    if kind == "symlink":
        (isolated_env / "escaped.py").symlink_to(project / "code.py")
        assert fs_list(glob=None)["entries"] == []
    else:
        with pytest.raises(ToolError, match="glob"):
            fs_list(glob="../dev/unified_agent/*")


def test_audit_refuses_recursive_symlink_escape(select_preset, isolated_env, project):
    select_preset("configured-workspace")
    (isolated_env / "escaped.py").symlink_to(project / "code.py")
    with pytest.raises(ToolError):
        security_audit(".")


def test_inspection_refuses_audit_remediation(select_preset, isolated_env):
    select_preset("inspection")
    with pytest.raises(ToolError):
        security_audit(".", remediate=True)
    assert not (isolated_env / ".gitignore").exists()


@pytest.mark.parametrize("family", ["workspace", "audit"])
def test_active_worktree_lookup_failure_cannot_fall_back_to_workspace(
    select_preset, isolated_env, monkeypatch, family
):
    select_preset("active-worktree")

    def unavailable():
        raise OSError("test database unavailable")

    monkeypatch.setattr(storage, "connect", unavailable)
    resolver = safety.resolve_in_workspace if family == "workspace" else safety.resolve_for_audit
    with pytest.raises(ToolError):
        resolver(".")


@pytest.mark.parametrize("family", ["workspace", "audit"])
def test_missing_active_claim_cannot_fall_back_to_workspace(select_preset, family):
    select_preset("active-worktree")
    resolver = safety.resolve_in_workspace if family == "workspace" else safety.resolve_for_audit
    with pytest.raises(ToolError):
        resolver(".")


@pytest.mark.parametrize("operation", ["list", "search"])
def test_vault_enumeration_skips_symlink_outside_preset(
    select_preset, isolated_env, tmp_path, monkeypatch, operation
):
    select_preset("configured-workspace")
    vault = isolated_env / "vault"
    vault.mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_MARKER\n")
    (vault / "escaped.md").symlink_to(outside)
    if operation == "list":
        assert vault_list()["entries"] == []
    else:
        assert vault_search("OUTSIDE_MARKER")["matches"] == []


def test_vault_glob_cannot_list_outside_preset(select_preset, isolated_env, tmp_path, monkeypatch):
    select_preset("configured-workspace")
    vault = isolated_env / "vault"
    vault.mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    (tmp_path / "outside.md").write_text("OUTSIDE_MARKER\n")
    assert vault_list(glob="../../outside.md")["entries"] == []


def test_vault_transclusion_cannot_read_outside_preset(
    select_preset, isolated_env, tmp_path, monkeypatch
):
    select_preset("configured-workspace")
    monkeypatch.setenv("UA_VAULT_PATH", str(tmp_path))
    (tmp_path / "outside.md").write_text("OUTSIDE_MARKER\n")
    (isolated_env / "local.md").write_text("![[../outside]]\n")
    result = vault_read("workspace/local.md", expand_embeds=True)
    assert "OUTSIDE_MARKER" not in result["content"]
    assert "![[../outside]]" in result["content"]


@pytest.mark.parametrize("kind", ["journal", "inbox"])
def test_briefing_digest_checks_indirect_file_targets(
    select_preset, isolated_env, tmp_path, monkeypatch, kind
):
    select_preset("configured-workspace")
    vault = isolated_env / "vault"
    (vault / "Logs").mkdir(parents=True)
    (vault / "00 - Inbox").mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    outside = tmp_path / "outside.md"
    outside.write_text("- **12:00** [Desktop_Agent2] OUTSIDE_MARKER\n")
    if kind == "journal":
        (vault / "Logs" / "2026-09-29.md").symlink_to(outside)
    else:
        (vault / "00 - Inbox" / "escaped.md").symlink_to(outside)
    result = vault_briefing_digest(date="2026-09-29")
    assert result["entries"] == []
    assert result["inbox"] == []


def test_vault_query_corpus_checks_each_symlink_target(
    select_preset, isolated_env, tmp_path, monkeypatch
):
    select_preset("configured-workspace")
    vault = isolated_env / "vault"
    vault.mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    outside = tmp_path / "outside.md"
    outside.write_text("# OUTSIDE_MARKER\n")
    (vault / "escaped.md").symlink_to(outside)
    result = vault_query("OUTSIDE_MARKER")
    assert result["results"] == []
    assert result["graph"]["notes"] == 0
