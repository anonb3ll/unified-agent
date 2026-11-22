"""Full skill packages share the existing reviewed immutable version boundary."""

import json
import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_skill_catalog import actor
from test_skills_registry import GOOD_BODY, make_skill

from unified_agent import skill_catalog, storage
from unified_agent import skills_registry as reg
from unified_agent.tools import approvals, files


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("gcp_agent5")


def package(root):
    source = make_skill(
        root,
        "package-skill",
        body=GOOD_BODY + "\nRead [notes](references/notes.md).\nRun `scripts/check.py`.\n",
    )
    (source / "references").mkdir()
    (source / "references/notes.md").write_text("Reviewed supporting procedure.\n")
    (source / "scripts").mkdir()
    script = source / "scripts/check.py"
    script.write_text("print('checked')\n")
    script.chmod(0o755)
    (source / "assets").mkdir()
    (source / "assets/icon.bin").write_bytes(b"\x00\x01\xffASSET")
    return source


def publish_package(root):
    source = package(root)
    with actor():
        result = reg.publish("package-skill", "package-skill")
    assert result["ok"]
    return source, result["skill"]


def test_support_change_creates_immutable_version_and_bounded_manifest(isolated_env):
    source, first = publish_package(isolated_env)
    manifest = first["package_manifest"]
    assert manifest["schema_version"] == 1
    assert [f["path"] for f in manifest["files"]] == [
        "SKILL.md",
        "assets/icon.bin",
        "references/notes.md",
        "scripts/check.py",
    ]
    assert "package_payload" not in first
    old = reg.get("package-skill", 1)
    (source / "references/notes.md").write_text("Updated notes.\n")
    with actor():
        second = reg.publish("package-skill", "package-skill")["skill"]
    assert second["version"] == 2
    assert second["content_hash"] != first["content_hash"]
    assert reg.get("package-skill", 1) == old


def test_actual_support_payload_audit_blocks_code(isolated_env):
    source = package(isolated_env)
    (source / "scripts/check.py").write_text("import openai\neval(input())\n")
    with actor():
        result = reg.publish("package-skill", "package-skill")
    assert result["blocked"]
    assert result["review"]["status"] == "fail"
    assert reg.get("package-skill") is None


def test_support_secret_is_never_persisted(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text("TOKEN = " + repr("abcdefghijklmnop") + "\n")
    with actor(), pytest.raises(ToolError, match="credential material"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_missing_asset_refuses_publication(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").unlink()
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")


@pytest.mark.parametrize("target", ["../outside.md", "/tmp/outside.md", "file:///tmp/outside.md"])
def test_reference_escape_refuses_publication(isolated_env, target):
    make_skill(isolated_env, "package-skill", body=GOOD_BODY + f"\nRead [notes]({target}).\n")
    with actor(), pytest.raises(ToolError, match="supporting.*path"):
        reg.publish("package-skill", "package-skill")


def test_symlink_support_refuses_publication(isolated_env):
    source = package(isolated_env)
    (source / "references/link.md").symlink_to(source / "references/notes.md")
    with actor(), pytest.raises(ToolError, match="symlink"):
        reg.publish("package-skill", "package-skill")


def test_source_drift_during_review_refuses_publish(isolated_env, monkeypatch):
    source = package(isolated_env)
    review = reg.skill_review

    def changed(path):
        result = review(path)
        (source / "references/notes.md").write_text("Unreviewed replacement")
        return result

    monkeypatch.setattr(reg, "skill_review", changed)
    with actor(), pytest.raises(ToolError, match="changed while it was being reviewed"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_install_exact_immutable_package_and_per_surface_drift(isolated_env):
    source, first = publish_package(isolated_env)
    (source / "references/notes.md").write_text("Source edited after publication")
    with actor():
        result = skill_catalog.install("package-skill", 1, first["content_hash"], "surface")
    destination = isolated_env / "surface/package-skill"
    assert (destination / "references/notes.md").read_text() == "Reviewed supporting procedure.\n"
    assert (destination / "assets/icon.bin").read_bytes() == b"\x00\x01\xffASSET"
    assert (destination / "scripts/check.py").stat().st_mode & 0o111
    assert result["installed"]
    with actor():
        assert reg.drift("surface")["synced"] == [{"name": "package-skill", "version": 1}]
    (destination / "references/notes.md").write_text("Local modification")
    with actor():
        assert reg.drift("surface")["stale"][0]["name"] == "package-skill"
    assert not (isolated_env / "another-surface").exists()


def test_tampered_stored_support_refuses_install(isolated_env):
    _, row = publish_package(isolated_env)
    conn = storage.connect()
    try:
        conn.execute("DROP TRIGGER skill_versions_immutable_update")
        payload = json.loads(
            conn.execute(
                "SELECT package_payload FROM skill_versions WHERE name='package-skill'"
            ).fetchone()[0]
        )
        payload["references/notes.md"] = "dGFtcGVyZWQ="
        conn.execute(
            "UPDATE skill_versions SET package_payload=? WHERE name='package-skill'",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()
    with actor(), pytest.raises(ToolError, match="package.*hash|package.*attestation"):
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    assert not (isolated_env / "surface").exists()


def test_package_swap_failure_rolls_back_exact_existing_tree(isolated_env, monkeypatch):
    source, first = publish_package(isolated_env)
    with actor():
        skill_catalog.install("package-skill", 1, first["content_hash"], "surface")
    (source / "references/notes.md").write_text("New reviewed procedure")
    with actor():
        second = reg.publish("package-skill", "package-skill")["skill"]
    original = os.replace

    def fail_stage(src, dst):
        if Path(src).name.startswith(".ua-skill-stage-"):
            raise OSError("synthetic swap failure")
        return original(src, dst)

    from pathlib import Path

    monkeypatch.setattr(files.os, "replace", fail_stage)
    with actor(), pytest.raises(OSError, match="synthetic"):
        skill_catalog.install(
            "package-skill",
            2,
            second["content_hash"],
            "surface",
            expected_hash=first["content_hash"],
        )
    with actor():
        assert reg.drift("surface")["stale"][0]["on_disk_hash"] == first["content_hash"]
    assert (
        isolated_env / "surface/package-skill/references/notes.md"
    ).read_text() == "Reviewed supporting procedure.\n"
    assert not list((isolated_env / "surface").glob(".ua-skill-*"))


def test_package_approval_binds_all_files_and_destination_base(isolated_env):
    source, first = publish_package(isolated_env)
    seen = []
    original = approvals.check_approval_gate

    def gate(tool, payload, **kwargs):
        if tool == "fs_write":
            seen.append(payload)
        return original(tool, payload, **kwargs)

    from unittest.mock import patch

    with actor(), patch.object(approvals, "check_approval_gate", gate):
        skill_catalog.install("package-skill", 1, first["content_hash"], "surface")
    assert seen
    assert seen[-1]["content_sha256"] == first["content_hash"]
    assert seen[-1]["package_manifest"] == first["package_manifest"]
    assert seen[-1]["expected_content_sha256"] == ""


def test_package_to_single_file_removes_old_assets_under_policy(isolated_env, monkeypatch):
    import shutil

    from unified_agent import safety

    source, first = publish_package(isolated_env)
    with actor():
        skill_catalog.install("package-skill", 1, first["content_hash"], "surface")
    make_skill(isolated_env, "package-skill")
    for name in ("assets", "scripts", "references"):
        shutil.rmtree(source / name)
    with actor():
        second = reg.publish("package-skill", "package-skill")["skill"]
    policy = safety.check_policy_gate

    def deny_removal(tool, payload, **kwargs):
        if tool == "fs_write" and str(payload.get("path", "")).endswith("assets/icon.bin"):
            raise ToolError("old asset is protected by policy")
        return policy(tool, payload, **kwargs)

    monkeypatch.setattr(safety, "check_policy_gate", deny_removal)
    with actor(), pytest.raises(ToolError, match="protected by policy"):
        skill_catalog.install(
            "package-skill",
            2,
            second["content_hash"],
            "surface",
            expected_hash=first["content_hash"],
        )
    destination = isolated_env / "surface/package-skill"
    assert (destination / "assets/icon.bin").exists()
    monkeypatch.setattr(safety, "check_policy_gate", policy)
    with actor():
        assert skill_catalog.install(
            "package-skill",
            2,
            second["content_hash"],
            "surface",
            expected_hash=first["content_hash"],
        )["installed"]
        assert reg.drift("surface")["synced"] == [{"name": "package-skill", "version": 2}]
    assert sorted(p.name for p in destination.iterdir()) == ["SKILL.md"]


def test_legacy_version_stays_unattested_and_refuses_support(isolated_env):
    source = make_skill(isolated_env, "type-hints")
    body = (source / "SKILL.md").read_text()
    import hashlib

    digest = hashlib.sha256(body.encode()).hexdigest()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO skill_versions(name,version,content_hash,source,content,published_by,published_at) VALUES (?,?,?,?,?,?,?)",
            ("type-hints", 1, digest, str(source), body, "gcp_agent5", storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    (source / "helper.py").write_text("print('unreviewed')")
    with actor(), pytest.raises(ToolError, match="support"):
        skill_catalog.install("type-hints", 1, digest, "surface")
    assert reg.get("type-hints", 1)["package_manifest"] is None
    destination = isolated_env / "surface/type-hints"
    destination.mkdir(parents=True)
    (destination / "helper.py").write_text("Partial legacy installation")
    with actor():
        report = reg.drift("surface")
        assert report["missing"] == [{"name": "type-hints", "registry_version": 1}]
        assert report["stale"] == []
    with actor():
        assert reg.publish("type-hints", "type-hints")["skill"]["version"] == 2


def test_executable_skill_md_manifest_refuses_install(isolated_env):
    make_skill(isolated_env, "type-hints")
    with actor():
        row = reg.publish("type-hints", "type-hints")["skill"]
    manifest = row["package_manifest"]
    manifest["files"][0]["mode"] = 0o755
    conn = storage.connect()
    try:
        conn.execute("DROP TRIGGER skill_versions_immutable_update")
        conn.execute(
            "UPDATE skill_versions SET package_manifest=? WHERE name='type-hints'",
            (json.dumps(manifest),),
        )
        conn.commit()
    finally:
        conn.close()
    with actor(), pytest.raises(ToolError, match="attestation"):
        skill_catalog.install("type-hints", 1, row["content_hash"], "surface")


@pytest.mark.parametrize("mutation", ["extra", "missing", "mode"])
def test_support_surface_drift_detects_full_membership(isolated_env, mutation):
    _, row = publish_package(isolated_env)
    with actor():
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    destination = isolated_env / "surface/package-skill"
    if mutation == "extra":
        (destination / "extra.txt").write_text("Local extra")
    elif mutation == "missing":
        (destination / "references/notes.md").unlink()
    else:
        (destination / "scripts/check.py").chmod(0o644)
    with actor():
        assert reg.drift("surface")["stale"][0]["name"] == "package-skill"


def test_legacy_unchanged_republish_mints_attested_version_without_mutating_old(isolated_env):
    import hashlib

    source = make_skill(isolated_env, "type-hints")
    body = (source / "SKILL.md").read_text()
    digest = hashlib.sha256(body.encode()).hexdigest()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO skill_versions(name,version,content_hash,source,content,published_by,published_at) VALUES (?,?,?,?,?,?,?)",
            ("type-hints", 1, digest, str(source), body, "old-publisher", storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    before = reg.get("type-hints", 1)
    storage.init_db()
    with actor():
        new = reg.publish("type-hints", "type-hints")["skill"]
        again = reg.publish("type-hints", "type-hints")
    assert new["version"] == 2
    assert new["content_hash"] == digest
    assert new["package_manifest"] is not None
    assert again["idempotent"]
    assert reg.get("type-hints", 1) == before


def test_skill_version_provenance_is_immutable(isolated_env):
    import sqlite3

    _, row = publish_package(isolated_env)
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE skill_versions SET published_by='forged' WHERE id=?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM skill_versions WHERE id=?", (row["id"],))
    finally:
        conn.close()


@pytest.mark.parametrize(
    "mutation",
    ["oversized", "too_many", "fifo", "symlink_root", "secret_filename", "binary_script"],
)
def test_package_bounds_and_unsafe_member_types_fail_closed(isolated_env, monkeypatch, mutation):
    source = package(isolated_env)
    path = "package-skill"
    if mutation == "oversized":
        monkeypatch.setattr(reg, "MAX_PACKAGE_FILE_BYTES", 8)
    elif mutation == "too_many":
        monkeypatch.setattr(reg, "MAX_PACKAGE_FILES", 2)
    elif mutation == "fifo":
        os.mkfifo(source / "special")
    elif mutation == "symlink_root":
        (isolated_env / "source-link").symlink_to(source, target_is_directory=True)
        path = "source-link"
    elif mutation == "secret_filename":
        (source / ".env").write_text("opaque secret")
    else:
        (source / "scripts/check.py").write_bytes(b"\x00Executable binary")
    with actor(), pytest.raises(ToolError):
        reg.publish("package-skill", path)
    assert reg.get("package-skill") is None


def test_external_asset_link_is_not_treated_as_package_file(isolated_env):
    make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY + "\nRead [docs](https://example.org/scripts/check.py).\n",
    )
    with actor():
        assert reg.publish("type-hints", "type-hints")["ok"]


def test_staging_refusal_restores_approval_and_preserves_local_directory(isolated_env, monkeypatch):
    _, row = publish_package(isolated_env)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    with actor(), pytest.raises(approvals.ApprovalPendingError) as pending:
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    with actor("Desktop_Person"):
        approvals.approval_resolve(
            pending.value.approval_id, "approve", reason="Install immutable package"
        )
    from pathlib import Path

    original = Path.write_bytes

    def failed(path, data):
        if any(p.startswith(".ua-skill-stage-") for p in path.parts):
            raise OSError("synthetic staging failure")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", failed)
    with actor(), pytest.raises(OSError, match="synthetic staging"):
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    with actor():
        assert pending.value.approval_id in {
            p["id"] for p in approvals.approval_pending()["approved_unconsumed"]
        }
    assert not (isolated_env / "surface/package-skill").exists()
    assert not list((isolated_env / "surface").glob(".ua-skill-*"))


def test_pre_package_schema_upgrade_preserves_legacy_row_and_provenance(isolated_env):
    import hashlib

    source = make_skill(isolated_env, "type-hints")
    body = (source / "SKILL.md").read_text()
    digest = hashlib.sha256(body.encode()).hexdigest()
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE skill_versions")
        conn.execute(
            "CREATE TABLE skill_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL, source TEXT NOT NULL, content TEXT NOT NULL, published_by TEXT, published_at TEXT NOT NULL, UNIQUE(name,version))"
        )
        conn.execute(
            "INSERT INTO skill_versions(name,version,content_hash,source,content,published_by,published_at) VALUES (?,?,?,?,?,?,?)",
            (
                "type-hints",
                1,
                digest,
                str(source),
                body,
                "legacy-publisher",
                "2026-09-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    storage.init_db()
    storage.init_db()
    legacy = reg.get("type-hints", 1)
    assert legacy["package_manifest"] is None
    assert legacy["content_hash"] == digest
    assert legacy["published_by"] == "legacy-publisher"
    assert legacy["published_at"] == "2026-09-01T00:00:00+00:00"
    with actor():
        assert reg.publish("type-hints", "type-hints")["skill"]["version"] == 2


def _filesystem_keeps_both(directory, paths) -> bool:
    """Whether this filesystem stores both names as distinct entries.

    Linux filesystems do. macOS APFS (case- and normalization-insensitive)
    collapses ``A.md``/``a.md`` and composed/decomposed ``café.md`` into one
    file, so the second write just overwrites the first (task #1698).
    """
    probe = directory / ".collision-probe"
    probe.mkdir()
    for path in paths:
        (probe / path).write_text(path)
    kept = len(os.listdir(probe))
    for child in probe.iterdir():
        child.unlink()
    probe.rmdir()
    return kept == len(paths)


class _AliasEntry:
    """A directory entry for an existing file, listed under another name."""

    def __init__(self, entry, name):
        self._entry = entry
        self.name = name
        self.path = os.path.join(os.path.dirname(entry.path), name)

    def __getattr__(self, attr):
        return getattr(self._entry, attr)


def _list_alias_too(monkeypatch, directory, real, alias):
    """Make the package walk see ``alias`` beside ``real`` in ``directory``.

    Only the registry's own ``os`` reference is replaced, so nothing else in
    the test sees the synthetic entry.
    """
    import contextlib

    real_os = reg.os
    target = os.path.realpath(directory)

    @contextlib.contextmanager
    def scandir(path):
        with real_os.scandir(path) as children:
            entries = list(children)
        if os.path.realpath(path) == target:
            entries += [_AliasEntry(entry, alias) for entry in entries if entry.name == real]
        yield iter(entries)

    class _Os:
        def __getattr__(self, name):
            return getattr(real_os, name)

    shim = _Os()
    shim.scandir = scandir
    monkeypatch.setattr(reg, "os", shim)


@pytest.mark.parametrize("strategy", ["filesystem", "synthetic"])
@pytest.mark.parametrize("paths", [("A.md", "a.md"), ("caf\u00e9.md", "cafe\u0301.md")])
def test_package_refuses_casefold_and_normalization_collisions(
    isolated_env, monkeypatch, paths, strategy
):
    """Two members whose portable names collide are refused.

    ``filesystem`` writes both files for real where the filesystem keeps them
    apart (Linux); on one that collapses the pair (macOS APFS) it falls back to
    the synthetic listing, which feeds the same walk a second directory entry
    under the colliding name. Either way the registry's check is what refuses.
    """
    source = package(isolated_env)
    references = source / "references"
    if strategy == "filesystem" and _filesystem_keeps_both(references, paths):
        for path in paths:
            (references / path).write_text("Identical bytes")
        assert len({entry.name for entry in os.scandir(references)} & set(paths)) == 2
    else:
        (references / paths[0]).write_text("Identical bytes")
        _list_alias_too(monkeypatch, references, paths[0], paths[1])
    with actor(), pytest.raises(ToolError, match="collisions"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_staged_payload_corruption_refuses_activation_without_spending_approval(
    isolated_env, monkeypatch
):
    from pathlib import Path

    _, row = publish_package(isolated_env)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    with actor(), pytest.raises(approvals.ApprovalPendingError) as pending:
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    with actor("Desktop_Person"):
        approvals.approval_resolve(
            pending.value.approval_id, "approve", reason="Install reviewed package"
        )
    original = Path.write_bytes

    def corrupt(path, data):
        if (
            any(part.startswith(".ua-skill-stage-") for part in path.parts)
            and path.name == "icon.bin"
        ):
            data = b"corrupted stage"
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", corrupt)
    with actor(), pytest.raises(ToolError, match="staged skill package changed"):
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    with actor():
        assert pending.value.approval_id in {
            p["id"] for p in approvals.approval_pending()["approved_unconsumed"]
        }
    assert not (isolated_env / "surface/package-skill").exists()
    assert not list((isolated_env / "surface").glob(".ua-skill-*"))


@pytest.mark.parametrize("mutation", ["delete", "rename"])
def test_attested_missing_instruction_with_support_is_stale(isolated_env, mutation):
    _, row = publish_package(isolated_env)
    with actor():
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    md = isolated_env / "surface/package-skill/SKILL.md"
    if mutation == "delete":
        md.unlink()
    else:
        md.rename(md.with_name("renamed.md"))
    with actor():
        drift = reg.drift("surface")
    assert drift["missing"] == []
    assert drift["stale"][0]["name"] == "package-skill"


def test_empty_existing_directory_uses_consistent_base_hash(isolated_env):
    _, row = publish_package(isolated_env)
    destination = isolated_env / "surface/package-skill"
    destination.mkdir(parents=True)
    empty_hash = reg._package_hash(reg._package_manifest({}))
    with actor(), pytest.raises(ToolError, match="exact expected_hash"):
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    with actor():
        result = skill_catalog.install(
            "package-skill", 1, row["content_hash"], "surface", expected_hash=empty_hash
        )
        assert reg.drift("surface")["synced"] == [{"name": "package-skill", "version": 1}]
    assert result["installed"]


@pytest.mark.parametrize(
    "reference", ["examples/usage.md", "../outside.md", "file:///tmp/outside.md"]
)
def test_support_markdown_local_references_fail_closed(isolated_env, reference):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(f"Read [example]({reference}).\n")
    with actor(), pytest.raises(ToolError, match="supporting file"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_support_markdown_links_are_relative_to_document(isolated_env):
    source = package(isolated_env)
    (source / "references/examples").mkdir()
    (source / "references/examples/usage.md").write_text("Bounded usage example.\n")
    (source / "references/notes.md").write_text("Read [example](examples/usage.md#usage).\n")
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]


@pytest.mark.parametrize("boundary", ["policy", "canonical_guard"])
@pytest.mark.parametrize("directory", ["surface/package-skill/references", "surface"])
def test_new_package_directory_receives_write_authority_checks(
    isolated_env, monkeypatch, boundary, directory
):
    from unified_agent import safety

    _, row = publish_package(isolated_env)
    protected = isolated_env / directory
    if boundary == "policy":
        original = safety.check_policy_gate

        def deny_directory(tool, payload, **kwargs):
            if tool == "fs_write" and payload.get("path") == str(protected):
                raise ToolError("protected new directory")
            return original(tool, payload, **kwargs)

        monkeypatch.setattr(safety, "check_policy_gate", deny_directory)
    else:
        original = files.resolve_in_workspace

        def deny_directory(path, **kwargs):
            if path == str(protected):
                raise ToolError("protected new directory")
            return original(path, **kwargs)

        monkeypatch.setattr(files, "resolve_in_workspace", deny_directory)
    with actor(), pytest.raises(ToolError, match="protected new directory"):
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    assert not (isolated_env / "surface").exists()


def test_instructionless_attested_packages_retain_drift_work_limit(isolated_env, monkeypatch):
    _, row = publish_package(isolated_env)
    with actor():
        skill_catalog.install("package-skill", 1, row["content_hash"], "surface")
    (isolated_env / "surface/package-skill/SKILL.md").unlink()
    monkeypatch.setattr(reg, "MAX_DRIFT_SKILLS", 0)
    with actor(), pytest.raises(ToolError, match="skill directory limit"):
        reg.drift("surface")


def test_support_reference_cannot_bind_wrong_root_relative_member(isolated_env):
    source = package(isolated_env)
    (source / "examples").mkdir()
    (source / "examples/usage.md").write_text("Example in the wrong directory.\n")
    (source / "references/notes.md").write_text("Read [example](examples/usage.md).\n")
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")


def test_support_markdown_reference_work_limit_is_bounded(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text("[notes](notes.md)\n" * 4097)
    with actor(), pytest.raises(ToolError, match="reference work limit"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_markdown_link_is_counted_once_against_package_reference_cap(isolated_env):
    source = make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY + "\n[notes](references/notes.md)\n" * 2049,
    )
    (source / "references").mkdir()
    (source / "references/notes.md").write_text("Supporting notes.\n")
    with actor():
        assert reg.publish("type-hints", "type-hints")["ok"]


@pytest.mark.parametrize("bare_count, allowed", [(2047, True), (2048, False)])
def test_reference_cap_counts_actual_links_and_standalone_paths_across_docs(
    isolated_env, bare_count, allowed
):
    source = make_skill(
        isolated_env, "type-hints", body=GOOD_BODY + "\n[notes](references/notes.md).\n"
    )
    (source / "references/assets").mkdir(parents=True)
    (source / "references/assets/example.txt").write_text("Reviewed example.\n")
    (source / "references/notes.md").write_text("[example](assets/example.txt)\n" * 2048)
    (source / "references/other.md").write_text("Use `assets/example.txt`.\n" * bare_count)
    with actor():
        if allowed:
            assert reg.publish("type-hints", "type-hints")["ok"]
        else:
            with pytest.raises(ToolError, match="reference work limit"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


@pytest.mark.parametrize("label_style", ["backticked", "plain"])
@pytest.mark.parametrize(
    "destination", ["references/notes.md", "references/missing.md", "https://example.org"]
)
@pytest.mark.parametrize("script_exists", [True, False])
def test_markdown_link_labels_preserve_local_file_validation(
    isolated_env, label_style, destination, script_exists
):
    label = "Run `scripts/check.py`" if label_style == "backticked" else "Run scripts/check.py"
    source = make_skill(
        isolated_env, "type-hints", body=GOOD_BODY + f"\n[{label}]({destination})\n"
    )
    (source / "references").mkdir()
    (source / "references/notes.md").write_text("Supporting notes.\n")
    if script_exists:
        (source / "scripts").mkdir()
        (source / "scripts/check.py").write_text("print('checked')\n")
    with actor():
        if script_exists and destination != "references/missing.md":
            assert reg.publish("type-hints", "type-hints")["ok"]
        else:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


def test_support_document_label_paths_use_document_relative_scope(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text("[Run `scripts/check.py`](https://example.org)\n")
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")


@pytest.mark.parametrize("links, bare, allowed", [(2047, 1, True), (2048, 0, False)])
def test_reference_cap_counts_label_and_destination_as_distinct_occurrences(
    isolated_env, links, bare, allowed
):
    source = make_skill(
        isolated_env, "type-hints", body=GOOD_BODY + "\n[notes](references/notes.md).\n"
    )
    (source / "references/scripts").mkdir(parents=True)
    (source / "references/scripts/check.py").write_text("print('checked')\n")
    (source / "references/notes.md").write_text(
        "[Run scripts/check.py](scripts/check.py)\n" * links + "`scripts/check.py`\n" * bare
    )
    with actor():
        if allowed:
            assert reg.publish("type-hints", "type-hints")["ok"]
        else:
            with pytest.raises(ToolError, match="reference work limit"):
                reg.publish("type-hints", "type-hints")


@pytest.mark.parametrize("form", ["bare", "code", "markdown", "encoded", "escaped"])
@pytest.mark.parametrize("exists", [True, False])
def test_bracketed_support_filename_is_checked_without_partial_bare_reference(
    isolated_env, form, exists
):
    name = "assets/example[1].txt"
    if form == "bare":
        instruction = f"Read {name}."
    elif form == "code":
        instruction = f"Read `{name}`."
    elif form == "encoded":
        instruction = "Read [example](assets/example%5B1%5D.txt)."
    elif form == "escaped":
        instruction = r"Read [example](assets/example\[1\].txt)."
    else:
        instruction = f"Read [example]({name})."
    source = make_skill(isolated_env, "type-hints", body=GOOD_BODY + f"\n{instruction}\n")
    if exists:
        (source / "assets").mkdir()
        (source / name).write_text("Reviewed bracketed example.\n")
    with actor():
        if exists:
            row = reg.publish("type-hints", "type-hints")["skill"]
            assert reg.get("type-hints", row["version"])["content_hash"] == row["content_hash"]
            assert name in reg.version_files("type-hints", row["version"])
        else:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


@pytest.mark.parametrize("missing", [True, False])
def test_markdown_label_scan_keeps_adjacent_labels_and_bracketed_members(isolated_env, missing):
    source = make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY + "\n[Run scripts/check[1].py](references/notes.md)"
        "[Run scripts/missing.py](references/notes.md)\n",
    )
    (source / "references").mkdir()
    (source / "references/notes.md").write_text("Reviewed notes.\n")
    (source / "scripts").mkdir()
    (source / "scripts/check[1].py").write_text("print('checked')\n")
    if not missing:
        (source / "scripts/missing.py").write_text("print('also checked')\n")
    with actor():
        if missing:
            with pytest.raises(ToolError, match="scripts/missing.py"):
                reg.publish("type-hints", "type-hints")
        else:
            assert reg.publish("type-hints", "type-hints")["ok"]


@pytest.mark.parametrize("filename", ["scripts/check.py", "assets/example[1].txt"])
@pytest.mark.parametrize("exists", [True, False])
def test_standalone_bracket_mentions_keep_filename_brackets(isolated_env, filename, exists):
    source = make_skill(isolated_env, "type-hints", body=GOOD_BODY + f"\nRead [{filename}].\n")
    if exists:
        path = source / filename
        path.parent.mkdir(parents=True)
        path.write_text("Reviewed supporting file.\n")
    with actor():
        if exists:
            assert reg.publish("type-hints", "type-hints")["ok"]
        else:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")


@pytest.mark.parametrize("missing", [True, False])
def test_adjacent_standalone_brackets_do_not_hide_missing_reference(isolated_env, missing):
    source = make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY + "\nRead [scripts/check.py][references/missing.md].\n",
    )
    (source / "scripts").mkdir()
    (source / "scripts/check.py").write_text("print('checked')\n")
    if not missing:
        (source / "references").mkdir()
        (source / "references/missing.md").write_text("Reviewed notes.\n")
    with actor():
        if missing:
            with pytest.raises(
                ToolError, match=r"missing supporting file: 'references/missing.md'"
            ):
                reg.publish("type-hints", "type-hints")
        else:
            assert reg.publish("type-hints", "type-hints")["ok"]


@pytest.mark.parametrize("later", ["../outside.md", "file:///tmp/outside.md", "other.md"])
@pytest.mark.parametrize("document", ["instructions", "support"])
def test_unclosed_destination_cannot_hide_later_reference(isolated_env, later, document):
    body = f"[broken](unclosed\nRead [unsafe]({later}).\n"
    source = package(isolated_env)
    if document == "instructions":
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
    else:
        (source / "references/notes.md").write_text(body)
    with (
        actor(),
        pytest.raises(ToolError, match="supporting.*link|supporting.*path|missing supporting"),
    ):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_unclosed_destination_does_not_disable_reference_work_limit(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(
        "[broken](unclosed\n" + "[notes](notes.md)\n" * 4097
    )
    with actor(), pytest.raises(ToolError, match="supporting.*link|reference work limit"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize("later", ["../outside.md", "file:///tmp/outside.md", "other.md"])
def test_closed_malformed_destination_cannot_hide_later_reference(isolated_env, later):
    source = package(isolated_env)
    (source / "references/unclosed").write_text("Reviewed first target.\n")
    (source / "references/notes.md").write_text(f"[broken](unclosed Read [unsafe]({later})).\n")
    with actor(), pytest.raises(ToolError, match="supporting.*path|missing supporting"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize(
    "title",
    [
        '"Reviewed notes"',
        "'Reviewed notes'",
        "(Reviewed notes)",
        r'"Reviewed \[notes\] \(checked\)"',
    ],
)
def test_markdown_titles_retain_existing_destination_validation(isolated_env, title):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(f"[script](../scripts/check.py {title})\n")
    # Use an in-package destination relative to the document; traversal syntax
    # remains refused even when it would resolve inside the captured package.
    with actor(), pytest.raises(ToolError, match="supporting.*path"):
        reg.publish("package-skill", "package-skill")
    (source / "references/notes.md").write_text(f"[notes](notes.md {title})\n")
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]


def test_markdown_title_local_mentions_are_verified(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text('[notes](notes.md "Read scripts/missing.py")\n')
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize("count, allowed", [(4096, True), (4097, False)])
def test_adjacent_bracket_mentions_obey_exact_reference_cap(isolated_env, count, allowed):
    source = make_skill(
        isolated_env, "type-hints", body=GOOD_BODY + "\n" + "[assets/a.txt]" * count
    )
    (source / "assets").mkdir()
    (source / "assets/a.txt").write_text("Reviewed asset.\n")
    with actor():
        if allowed:
            assert reg.publish("type-hints", "type-hints")["ok"]
        else:
            with pytest.raises(ToolError, match="reference work limit"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


@pytest.mark.parametrize(
    "title",
    [
        '"first" Read [unsafe]({later}) "last"',
        "'first' Read [unsafe]({later}) 'last'",
        "(first) Read [unsafe]({later}) (last)",
    ],
)
@pytest.mark.parametrize("later", ["../outside.md", "file:///tmp/outside.md", "other.md"])
def test_closed_title_cannot_hide_later_destination(isolated_env, title, later):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(f"[notes](notes.md {title.format(later=later)})\n")
    with actor(), pytest.raises(ToolError, match="supporting.*path|missing supporting"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize(
    "title",
    [
        '"Reviewed ) notes"',
        '"Reviewed ( notes"',
        "'Reviewed ) notes'",
        r'"Reviewed \"notes\" )"',
        r"'Reviewed \'notes\' ('",
    ],
)
def test_quoted_titles_keep_literal_parentheses_and_escaped_quotes(isolated_env, title):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(f"[notes](notes.md {title})\n")
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]


@pytest.mark.parametrize(
    "wrapper", ["`{body}`", "``{body}``", "```markdown\n{body}\n```", "~~~markdown\n{body}\n~~~"]
)
@pytest.mark.parametrize("missing", [True, False])
def test_code_markdown_examples_keep_local_file_checks(isolated_env, wrapper, missing):
    package(isolated_env)
    path = "assets/missing.txt" if missing else "assets/icon.bin"
    make_skill(
        isolated_env,
        "package-skill",
        body=GOOD_BODY + "\n" + wrapper.format(body=f"[example](unfinished Read {path}"),
    )
    with actor():
        if missing:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("package-skill", "package-skill")
            assert reg.get("package-skill") is None
        else:
            assert reg.publish("package-skill", "package-skill")["ok"]


def test_literal_code_does_not_hide_following_prose_destination(isolated_env):
    package(isolated_env)
    make_skill(
        isolated_env,
        "package-skill",
        body=GOOD_BODY + "\n`[example](unfinished`\n[unsafe](../outside.md).\n",
    )
    with actor(), pytest.raises(ToolError, match="supporting.*path"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize("missing", [True, False])
def test_inline_code_label_does_not_hide_destination(isolated_env, missing):
    source = package(isolated_env)
    (source / "references/scripts").mkdir()
    (source / "references/scripts/check.py").write_text("print('checked')\n")
    (source / "references/notes.md").write_text("[Run `scripts/check.py`](other.md)\n")
    if not missing:
        (source / "references/other.md").write_text("Reviewed note.\n")
    with actor():
        if missing:
            with pytest.raises(ToolError, match=r"missing supporting file: 'references/other.md'"):
                reg.publish("package-skill", "package-skill")
        else:
            assert reg.publish("package-skill", "package-skill")["ok"]


def test_code_after_link_title_remains_literal(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text(
        '[notes](notes.md "Use `quoted code`")\n`[example](unfinished`\n'
    )
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]


@pytest.mark.parametrize("document", ["instructions", "support"])
def test_title_backtick_does_not_hide_later_missing_destination(isolated_env, document):
    source = package(isolated_env)
    body = '[ok](notes.md "Use `") [bad](missing.md) `\n'
    if document == "instructions":
        (source / "notes.md").write_text("Reviewed first destination.\n")
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
    else:
        (source / "references/notes.md").write_text(body)
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


def test_commonmark_fence_interrupts_paragraph_and_keeps_bare_file_checks(isolated_env):
    source = package(isolated_env)
    # CommonMark recognizes this physical fence before inline parsing. The
    # missing.md example is literal code, not a rendered link destination.
    body = "`prefix\n~~~\n`\n[bad](missing.md)\n"
    (source / "references/notes.md").write_text(body)
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]
        assert reg.get("package-skill") is not None
    # Local file mentions inside literal code remain governed and relative
    # to their supporting document even when Markdown syntax is suppressed.
    (source / "references/notes.md").write_text(body + "assets/missing.txt\n")
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill")["version"] == 1


@pytest.mark.parametrize(
    "markup",
    ["![example](references/notes.md)", "[notes][support]\n\n[support]: references/notes.md"],
)
@pytest.mark.parametrize("missing", [True, False])
def test_canonical_image_and_reference_links_verify_destinations(isolated_env, markup, missing):
    source = make_skill(isolated_env, "type-hints", body=GOOD_BODY + "\n" + markup)
    if not missing:
        (source / "references").mkdir()
        (source / "references/notes.md").write_text("Reviewed note.\n")
    with actor():
        if missing:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")
        else:
            assert reg.publish("type-hints", "type-hints")["ok"]
            assert reg.get("type-hints") is not None


@pytest.mark.parametrize("destination", ["missing.md", "../outside.md", "file:///tmp/outside.md"])
@pytest.mark.parametrize("depth", [20, 21, 30])
@pytest.mark.parametrize("document", ["instructions", "support"])
def test_deep_blockquotes_refuse_reference_truncation(isolated_env, destination, depth, document):
    source = package(isolated_env)
    body = "> " * depth + f"[bad]({destination})\n"
    if document == "instructions":
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
    else:
        (source / "references/notes.md").write_text(body)
    with actor(), pytest.raises(ToolError, match="Markdown nesting limit"):
        reg.publish("package-skill", "package-skill")
    assert reg.get("package-skill") is None


@pytest.mark.parametrize("destination", ["missing.md", "../outside.md", "file:///tmp/outside.md"])
@pytest.mark.parametrize("document", ["instructions", "support"])
def test_get_refuses_previously_attested_truncated_deep_document(
    isolated_env, monkeypatch, destination, document
):
    source = package(isolated_env)
    body = "> " * 20 + f"[bad]({destination})\n"
    if document == "instructions":
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
    else:
        (source / "references/notes.md").write_text(body)
    # Model a complete immutable version persisted by the earlier checker that
    # silently ignored the deep paragraph, without changing its attested bytes.
    with monkeypatch.context() as patch:
        patch.setattr(reg, "_check_package_references", lambda *args: None)
        with actor():
            assert reg.publish("package-skill", "package-skill")["ok"]
    with pytest.raises(ToolError, match="Markdown nesting limit"):
        reg.get("package-skill")


@pytest.mark.parametrize("context", ["brackets", "images"])
@pytest.mark.parametrize("depth, allowed", [(2, True), (20, False)])
def test_inline_nesting_limit_refuses_truncation_with_shallow_controls(
    isolated_env, context, depth, allowed
):
    source = package(isolated_env)
    if context == "brackets":
        body = "[" * depth + "[notes](notes.md)" + "]" * depth
    else:
        body = "[notes](notes.md)"
        for _ in range(depth):
            body = "![" + body + "](notes.md)"
    (source / "references/notes.md").write_text(body + "\n")
    with actor():
        if allowed:
            assert reg.publish("package-skill", "package-skill")["ok"]
        else:
            with pytest.raises(ToolError, match="Markdown nesting limit"):
                reg.publish("package-skill", "package-skill")
            assert reg.get("package-skill") is None


@pytest.mark.parametrize("exists", [True, False])
def test_literal_code_preserves_unmatched_filename_bracket(isolated_env, exists):
    source = make_skill(
        isolated_env, "type-hints", body=GOOD_BODY + "\nRead `assets/check].txt`.\n"
    )
    if exists:
        (source / "assets").mkdir()
        (source / "assets/check].txt").write_text("Reviewed literal filename.\n")
    with actor():
        if exists:
            assert reg.publish("type-hints", "type-hints")["ok"]
            assert reg.get("type-hints") is not None
        else:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")


@pytest.mark.parametrize("document", ["instructions", "support"])
@pytest.mark.parametrize("missing", [True, False])
def test_multiline_inline_code_keeps_following_prose_destination(isolated_env, document, missing):
    source = package(isolated_env)
    # Unlike a real CommonMark fence, four-space indentation cannot interrupt
    # this paragraph. The multiline inline span closes before the prose link.
    body = "`prefix\n    ~~~\n`\n[notes](other.md)\n"
    if document == "instructions":
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
        destination = source / "other.md"
    else:
        (source / "references/notes.md").write_text(body)
        destination = source / "references/other.md"
    if not missing:
        destination.write_text("Reviewed following destination.\n")
    with actor():
        if missing:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("package-skill", "package-skill")
            assert reg.get("package-skill") is None
        else:
            assert reg.publish("package-skill", "package-skill")["ok"]
            assert reg.get("package-skill") is not None


def test_shallow_blockquotes_keep_valid_local_destinations(isolated_env):
    source = package(isolated_env)
    (source / "references/notes.md").write_text("> " * 19 + "[notes](notes.md)\n")
    with actor():
        assert reg.publish("package-skill", "package-skill")["ok"]
        assert reg.get("package-skill") is not None


@pytest.mark.parametrize("field", ["label", "destination", "title"])
@pytest.mark.parametrize("document", ["instructions", "support"])
@pytest.mark.parametrize("exists", [True, False])
def test_unused_reference_definitions_validate_local_fields(isolated_env, field, document, exists):
    source = make_skill(isolated_env, "type-hints")
    local = "assets/check.txt"
    forms = {
        "label": f"[{local}]: https://example.org\n",
        "destination": f"[unused]: {local}\n",
        "title": f'[unused]: https://example.org "Read {local}"\n',
    }
    if document == "instructions":
        make_skill(isolated_env, "type-hints", body=GOOD_BODY + "\n" + forms[field])
        destination = source / local
    else:
        (source / "references").mkdir()
        (source / "references/notes.md").write_text(forms[field])
        destination = source / "references" / local
    if exists:
        destination.parent.mkdir(parents=True)
        destination.write_text("Reviewed definition support.\n")
    with actor():
        if exists:
            assert reg.publish("type-hints", "type-hints")["ok"]
            assert reg.get("type-hints") is not None
        else:
            with pytest.raises(ToolError, match="missing supporting"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


def test_shadowed_reference_definition_keeps_support_validation(isolated_env):
    make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY
        + "\n[notes]: https://example.org\n[notes]: assets/missing.txt\nRead [notes].\n",
    )
    with actor(), pytest.raises(ToolError, match="missing supporting"):
        reg.publish("type-hints", "type-hints")
    assert reg.get("type-hints") is None


@pytest.mark.parametrize("count, allowed", [(4096, True), (4097, False)])
def test_used_reference_definition_is_not_double_counted(isolated_env, count, allowed):
    source = make_skill(
        isolated_env,
        "type-hints",
        body=GOOD_BODY + "\n[notes]: assets/check.txt\n\n" + "[notes]\n" * count,
    )
    (source / "assets").mkdir()
    (source / "assets/check.txt").write_text("Reviewed reference destination.\n")
    with actor():
        if allowed:
            assert reg.publish("type-hints", "type-hints")["ok"]
            assert reg.get("type-hints") is not None
        else:
            with pytest.raises(ToolError, match="reference work limit"):
                reg.publish("type-hints", "type-hints")
            assert reg.get("type-hints") is None


@pytest.mark.parametrize("document", ["instructions", "support"])
def test_get_refuses_attested_unused_definition_missing_file(isolated_env, monkeypatch, document):
    source = package(isolated_env)
    body = '[unused]: https://example.org "Read assets/missing.txt"\n'
    if document == "instructions":
        make_skill(isolated_env, "package-skill", body=GOOD_BODY + "\n" + body)
    else:
        (source / "references/notes.md").write_text(body)
    with monkeypatch.context() as patch:
        patch.setattr(reg, "_check_package_references", lambda *args: None)
        with actor():
            assert reg.publish("package-skill", "package-skill")["ok"]
    with pytest.raises(ToolError, match="missing supporting"):
        reg.get("package-skill")
