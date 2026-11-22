"""Read-only provenance for the cockpit sequence and its vault projection."""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import planning_loop


@pytest.fixture
def sources(isolated_env, tmp_path, monkeypatch):
    directory = isolated_env / "docs" / "queue"
    directory.mkdir(parents=True)
    manifest = {"schema_version": 1, "items": [], "optional_tail": []}
    (directory / "NEXT-WORK.json").write_text(json.dumps(manifest))
    (directory / "NEXT-WORK.md").write_text("# Next work\n\nUse verified replacements.\n")
    vault = tmp_path / "vault" / "Wiki"
    vault.mkdir(parents=True)
    mirror = vault / "Unified-Agent-Implementation-Sequence.md"
    mirror.write_text(
        "---\ntitle: Mirrored sequence\n---\n# Next work\n\nUse verified replacements.\n"
    )
    monkeypatch.setattr(planning_loop.storage, "require_live_task_store", lambda: None)
    monkeypatch.setattr(planning_loop, "_read_next_work_live_snapshot", lambda: ([], {}, {}, set()))
    return isolated_env, mirror, manifest


def test_matching_mirror_has_body_hashes_and_authoritative_provenance(sources):
    root, _, _ = sources
    view = planning_loop.run_next_work_view(workspace_root=root)
    mirror = view["projection_mirror"]
    assert mirror["status"] == "matching"
    assert mirror["canonical_hash"] == mirror["mirror_hash"]
    assert len(mirror["canonical_hash"]) == 64
    assert mirror["authority"] == "hub-local"
    assert "Vault mirror: matching" in planning_loop.render_next_work_markdown(view)


def test_real_body_drift_warns_and_retains_both_hashes(sources):
    root, path, _ = sources
    path.write_text("# Next work\n\nA bare cancellation is enough.\n")
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "drift"
    assert view["projection_mirror"]["canonical_hash"] != view["projection_mirror"]["mirror_hash"]
    assert any("vault mirror" in warning.lower() for warning in view["drift_warnings"])
    assert "Vault mirror: drift" in planning_loop.render_next_work_markdown(view)


def test_frontmatter_and_newline_variations_do_not_create_drift(sources):
    root, path, _ = sources
    path.write_bytes(
        b"---\r\ntitle: Mirror\r\n---\r\n# Next work\r\n\r\nUse verified replacements.\r\n"
    )
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "matching"


@pytest.mark.parametrize("missing", ["mirror", "canonical"])
def test_missing_projection_source_is_unavailable_with_warning(sources, missing):
    root, path, _ = sources
    (path if missing == "mirror" else root / "docs" / "queue" / "NEXT-WORK.md").unlink()
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "unavailable"
    assert any("vault mirror" in warning.lower() for warning in view["drift_warnings"])


def test_denied_mirror_does_not_read_its_content(sources, monkeypatch):
    root, path, _ = sources
    original_read = Path.read_text

    def guarded_read(self, *args, **kwargs):
        if self == path:
            pytest.fail("denied mirror must not be read")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(
        planning_loop,
        "resolve_in_vault",
        lambda *args, **kwargs: (_ for _ in ()).throw(ToolError("denied")),
    )
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "unavailable"
    assert view["projection_mirror"]["mirror_hash"] is None


def test_explicit_workspace_deny_cannot_be_bypassed_by_hub_projection(sources, monkeypatch):
    _, _, manifest = sources
    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: pytest.fail("DENY must prevent all file reads")
    )
    token = planning_loop.config.workspace_root_var.set(planning_loop.config.WORKSPACE_DENIED)
    try:
        view = planning_loop.next_work_projection_payload(manifest)
    finally:
        planning_loop.config.workspace_root_var.reset(token)
    assert view["projection_mirror"]["status"] == "unavailable"
    assert view["projection_mirror"]["canonical_hash"] is None
    assert view["projection_mirror"]["mirror_hash"] is None


def test_hub_projection_never_uses_client_supplied_path_for_filesystem(sources, monkeypatch):
    root, _, manifest = sources
    monkeypatch.setenv("UA_WORKSPACE", str(root))
    manifest = {**manifest, "manifest_path": "/etc/passwd", "mirror_path": "/etc/passwd"}
    read_paths = []
    original_read = Path.read_text

    def recorded_read(self, *args, **kwargs):
        read_paths.append(self)
        assert str(self) != "/etc/passwd"
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recorded_read)
    view = planning_loop.next_work_projection_payload(manifest)
    assert view["projection_mirror"]["status"] == "matching"
    assert read_paths
    assert view["projection_source"]["status"] == "drift"


def test_cli_manifest_override_exposes_canonical_manifest_discrepancy(sources, tmp_path):
    root, _, manifest = sources
    override = tmp_path / "override.json"
    override.write_text(json.dumps({**manifest, "plan_id": "different"}))
    view = planning_loop.run_next_work_view(manifest_path=override, workspace_root=root)
    assert view["projection_source"]["status"] == "drift"
    assert any("manifest source" in warning.lower() for warning in view["drift_warnings"])


def _remote(monkeypatch, view):
    def retired():
        raise planning_loop.storage.LiveStoreError("remote client")

    monkeypatch.setattr(planning_loop.storage, "require_live_task_store", retired)
    monkeypatch.setattr(planning_loop, "_read_remote_next_work_view", lambda manifest: view)


def test_old_remote_hub_missing_provenance_is_unverifiable_even_with_local_match(
    sources, monkeypatch
):
    root, _, manifest = sources
    _remote(monkeypatch, planning_loop.build_next_work_view(manifest, []))
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "unavailable"
    assert view["projection_mirror"]["authority"] == "remote-hub-unverified"
    assert any("provenance" in warning.lower() for warning in view["drift_warnings"])


def test_remote_hub_drift_cannot_be_overwritten_by_local_matching_mirror(sources, monkeypatch):
    root, path, manifest = sources
    monkeypatch.setenv("UA_WORKSPACE", str(root))
    path.write_text("# Old hub mirror\n")
    hub_view = planning_loop.next_work_projection_payload(manifest)
    path.write_text((root / "docs" / "queue" / "NEXT-WORK.md").read_text())
    _remote(monkeypatch, hub_view)
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "drift"
    assert view["projection_mirror"]["authority"] == "hub-local"


def test_remote_cockpit_source_discrepancy_is_visible(sources, monkeypatch):
    root, _, manifest = sources
    monkeypatch.setenv("UA_WORKSPACE", str(root))
    hub_view = planning_loop.next_work_projection_payload(manifest)
    (root / "docs" / "queue" / "NEXT-WORK.md").write_text("# Different client cockpit\n")
    _remote(monkeypatch, hub_view)
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["client_cockpit_source"]["status"] == "drift"
    assert any("client cockpit" in warning.lower() for warning in view["drift_warnings"])


def test_mirror_check_never_writes_sources(sources, monkeypatch):
    root, _, _ = sources
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *args, **kwargs: pytest.fail("read-only projection must not write"),
    )
    planning_loop.run_next_work_view(workspace_root=root)


def test_frontmatter_separator_blank_lines_are_not_body_drift(sources):
    root, path, _ = sources
    path.write_text("---\ntitle: Mirror\n---\n\n# Next work\n\nUse verified replacements.\n\n")
    view = planning_loop.run_next_work_view(workspace_root=root)
    assert view["projection_mirror"]["status"] == "matching"


def test_semantically_identical_manifest_formats_match_authoritative_source(sources, tmp_path):
    root, _, manifest = sources
    path = tmp_path / "formatted.json"
    path.write_text(json.dumps(manifest, indent=4, sort_keys=True))
    view = planning_loop.run_next_work_view(manifest_path=path, workspace_root=root)
    assert view["projection_source"]["status"] == "matching"
    assert view["projection_consistency"] == "matching"


@pytest.mark.parametrize("field", ["projection_mirror", "projection_source"])
def test_authenticated_remote_reader_rejects_inconsistent_digest_metadata(
    sources, monkeypatch, field
):
    root, _, manifest = sources
    monkeypatch.setenv("UA_WORKSPACE", str(root))
    payload = planning_loop.next_work_projection_payload(manifest)
    key = "mirror_hash" if field == "projection_mirror" else "canonical_manifest_hash"
    payload[field][key] = "0" * 64
    from unified_agent import hub_client

    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://hub.example/mcp")
    monkeypatch.setattr(hub_client, "HubClient", lambda **kwargs: object())
    monkeypatch.setattr(hub_client, "_post_json", lambda *args: payload)
    with pytest.raises(ToolError, match="provenance"):
        planning_loop._read_remote_next_work_view(manifest)


@pytest.mark.parametrize("project_copy_exists", [True, False])
def test_hub_provenance_uses_process_cockpit_despite_request_workspace_binding(
    sources, tmp_path, project_copy_exists
):
    root, mirror, manifest = sources
    canonical_markdown = "# Actual hub source\n"
    canonical_manifest = {**manifest, "plan_id": "actual-hub-source"}
    (root / "docs" / "queue" / "NEXT-WORK.md").write_text(canonical_markdown)
    (root / "docs" / "queue" / "NEXT-WORK.json").write_text(json.dumps(canonical_manifest))
    project = tmp_path / "agent-project"
    project.mkdir()
    if project_copy_exists:
        directory = project / "docs" / "queue"
        directory.mkdir(parents=True)
        # Copies agree with the incoming manifest and vault, but are not hub authority.
        (directory / "NEXT-WORK.md").write_text(mirror.read_text().split("---\n")[-1])
        (directory / "NEXT-WORK.json").write_text(json.dumps(manifest))
    token = planning_loop.config.workspace_root_var.set(project)
    try:
        view = planning_loop.next_work_projection_payload(manifest)
    finally:
        planning_loop.config.workspace_root_var.reset(token)
    assert view["projection_mirror"]["canonical_hash"] == planning_loop._next_work_body_hash(
        canonical_markdown
    )
    assert view["projection_mirror"]["status"] == "drift"
    assert view["projection_source"]["canonical_manifest_hash"] == (
        planning_loop._next_work_manifest_object_hash(canonical_manifest)
    )
    assert view["projection_source"]["status"] == "drift"


@pytest.mark.parametrize("filename", ["NEXT-WORK.md", "NEXT-WORK.json"])
def test_hub_canonical_source_symlink_cannot_escape_cockpit(sources, tmp_path, filename):
    root, _, manifest = sources
    path = root / "docs" / "queue" / filename
    outside = tmp_path / filename
    outside.write_text(path.read_text())
    path.unlink()
    path.symlink_to(outside)
    view = planning_loop.next_work_projection_payload(manifest)
    field = "projection_mirror" if filename.endswith(".md") else "projection_source"
    assert view[field]["status"] == "unavailable"


@pytest.mark.parametrize("protected_directory", [".git", "private.env"])
def test_bound_request_cannot_read_protected_canonical_symlink_target(
    sources, tmp_path, protected_directory
):
    root, _, manifest = sources
    protected = root / protected_directory / "canonical.md"
    protected.parent.mkdir()
    protected.write_text("# Protected content\n")
    path = root / "docs" / "queue" / "NEXT-WORK.md"
    path.unlink()
    path.symlink_to(protected)
    project = tmp_path / "agent-project"
    project.mkdir()
    token = planning_loop.config.workspace_root_var.set(project)
    try:
        view = planning_loop.next_work_projection_payload(manifest)
    finally:
        planning_loop.config.workspace_root_var.reset(token)
    assert view["projection_mirror"]["canonical_hash"] is None
    assert view["projection_mirror"]["status"] == "unavailable"


def test_cli_default_source_ignores_request_binding_but_keeps_process_cockpit(sources, tmp_path):
    root, _, _ = sources
    project = tmp_path / "agent-project"
    project.mkdir()
    token = planning_loop.config.workspace_root_var.set(project)
    try:
        view = planning_loop.run_next_work_view()
    finally:
        planning_loop.config.workspace_root_var.reset(token)
    assert view["manifest_path"] == str(root / "docs" / "queue" / "NEXT-WORK.json")
    assert view["projection_source"]["status"] == "matching"
    assert view["projection_mirror"]["status"] == "matching"
