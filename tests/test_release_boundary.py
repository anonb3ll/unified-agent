"""Tests for src/unified_agent/release.py — Versioned immutable release boundaries & rollback proof."""

from pathlib import Path

import pytest

from unified_agent import release


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src_repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello v1')\n")
    (repo / "config.json").write_text('{"env": "prod"}\n')
    (repo / ".venv").mkdir()
    (repo / ".venv" / "junk.txt").write_text("junk")
    return repo


@pytest.fixture
def releases_dir(tmp_path: Path) -> Path:
    rel_dir = tmp_path / "releases"
    rel_dir.mkdir()
    return rel_dir


def test_release_helpers_default_to_data_home(source_repo: Path, tmp_path: Path, monkeypatch):
    """Omitted releases dir is data_home()/releases, the CLI default when --releases-dir is absent.

    config.DATA_DIR does not exist; resolving None used to raise AttributeError.
    """
    monkeypatch.setattr(release.config, "data_home", lambda: tmp_path)

    empty = release.list_releases(releases_base_dir=None, component="unified-agent")
    assert empty == []

    created = release.create_release(
        source_dir=source_repo,
        releases_base_dir=None,
        component="unified-agent",
        version="v1.0.0",
        authorize=False,
    )
    assert created["ok"] is True
    release_path = Path(created["release_path"])
    assert release_path.is_dir()
    assert release_path.parent.parent == (tmp_path / "releases").resolve()

    listed = release.list_releases(releases_base_dir=None, component="unified-agent")
    assert [row["release_id"] for row in listed] == [release_path.name]

    rolled = release.rollback_release(
        releases_base_dir=None,
        component="unified-agent",
        authorize=False,
    )
    assert rolled["ok"] is True
    assert rolled["target_release"] == release_path.name
    assert rolled["switched"] is False


class TestCreateRelease:
    def test_create_release_structure_and_manifest(self, source_repo: Path, releases_dir: Path):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            creator="Desktop_agent3",
            authorize=False,
        )

        assert res["ok"] is True
        assert res["component"] == "unified-agent"
        assert res["version"] == "v1.0.0"
        assert res["updated_current"] is False

        target_path = Path(res["release_path"])
        assert target_path.is_dir()

        manifest_path = target_path / "MANIFEST.json"
        assert manifest_path.is_file()

        # Verify excluded files (.venv) were not copied
        assert not (target_path / ".venv").exists()

        # Check immutability (read-only file permission)
        app_file = target_path / "src" / "app.py"
        assert app_file.is_file()
        assert (app_file.stat().st_mode & 0o222) == 0

    def test_create_release_authorized_updates_current_symlink(
        self, source_repo: Path, releases_dir: Path
    ):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            creator="Desktop_agent3",
            authorize=True,
        )

        assert res["ok"] is True
        assert res["updated_current"] is True

        current_symlink = releases_dir / "unified-agent" / "current"
        assert current_symlink.is_symlink()
        assert current_symlink.resolve().name == res["release_id"]


class TestVerifyRelease:
    def test_verify_clean_release(self, source_repo: Path, releases_dir: Path):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
        )
        release_dir = Path(res["release_path"])

        verif = release.verify_release(release_dir)
        assert verif["ok"] is True
        assert verif["errors"] == []

    def test_verify_detects_tampered_file(self, source_repo: Path, releases_dir: Path):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
        )
        release_dir = Path(res["release_path"])

        target_file = release_dir / "config.json"
        target_file.chmod(0o644)
        target_file.write_text('{"env": "tampered"}\n')

        verif = release.verify_release(release_dir)
        assert verif["ok"] is False
        assert any("Checksum mismatch" in err for err in verif["errors"])

    def test_verify_detects_unexpected_file(self, source_repo: Path, releases_dir: Path):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
        )
        release_dir = Path(res["release_path"])

        # Inject an unexpected file into the release tree
        release_dir.chmod(0o755)
        extra_file = release_dir / "sneaky_backdoor.py"
        extra_file.write_text("import os\n")

        verif = release.verify_release(release_dir)
        assert verif["ok"] is False
        assert any("Unexpected file present" in err for err in verif["errors"])

    def test_verify_detects_unsafe_manifest_path(self, source_repo: Path, releases_dir: Path):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
        )
        release_dir = Path(res["release_path"])

        # Tamper MANIFEST.json with path traversal entry
        manifest_path = release_dir / "MANIFEST.json"
        manifest_path.chmod(0o644)
        import json

        m_data = json.loads(manifest_path.read_text())
        m_data["files"]["../outside.txt"] = "dummyhash"
        manifest_path.write_text(json.dumps(m_data))

        verif = release.verify_release(release_dir)
        assert verif["ok"] is False
        assert any("Unsafe path in manifest" in err for err in verif["errors"])

    def test_create_release_rejects_unsafe_component_or_version(
        self, source_repo: Path, releases_dir: Path
    ):
        with pytest.raises(ValueError, match="Invalid component"):
            release.create_release(
                source_dir=source_repo,
                releases_base_dir=releases_dir,
                component="../bad_component",
                version="v1.0.0",
            )

        with pytest.raises(ValueError, match="Invalid version"):
            release.create_release(
                source_dir=source_repo,
                releases_base_dir=releases_dir,
                component="unified-agent",
                version="1.0/bad",
            )

    def test_verify_release_handles_non_dict_files_field(
        self, source_repo: Path, releases_dir: Path
    ):
        res = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
        )
        release_dir = Path(res["release_path"])

        manifest_path = release_dir / "MANIFEST.json"
        manifest_path.chmod(0o644)
        import json

        m_data = json.loads(manifest_path.read_text())
        m_data["files"] = ["not_a_dict"]
        manifest_path.write_text(json.dumps(m_data))

        verif = release.verify_release(release_dir)
        assert verif["ok"] is False
        assert "files' field must be a mapping" in verif["error"]


class TestRollbackRelease:
    def test_rollback_rehearsal_unauthorized_does_not_mutate(
        self, source_repo: Path, releases_dir: Path
    ):
        # Create release 1 and set current
        rel1 = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            authorize=True,
        )

        # Update source and create release 2
        (source_repo / "src" / "app.py").write_text("print('hello v2')\n")
        rel2 = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.1.0",
            authorize=True,
        )

        current_symlink = releases_dir / "unified-agent" / "current"
        assert current_symlink.resolve().name == rel2["release_id"]

        # Attempt rollback without authorization (rehearsal mode)
        rollback_res = release.rollback_release(
            releases_base_dir=releases_dir,
            component="unified-agent",
            target_release_id=rel1["release_id"],
            authorize=False,
        )

        assert rollback_res["ok"] is True
        assert rollback_res["switched"] is False
        assert "warning" in rollback_res
        assert current_symlink.resolve().name == rel2["release_id"]

    def test_rollback_authorized_switches_symlink(self, source_repo: Path, releases_dir: Path):
        rel1 = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            authorize=True,
        )

        (source_repo / "src" / "app.py").write_text("print('hello v2')\n")
        rel2 = release.create_release(
            source_dir=source_repo,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.1.0",
            authorize=True,
        )

        current_symlink = releases_dir / "unified-agent" / "current"
        assert current_symlink.resolve().name == rel2["release_id"]

        # Authorized rollback to rel1
        rollback_res = release.rollback_release(
            releases_base_dir=releases_dir,
            component="unified-agent",
            target_release_id=rel1["release_id"],
            authorize=True,
        )

        assert rollback_res["ok"] is True
        assert rollback_res["switched"] is True
        assert rollback_res["previous_release"] == rel2["release_id"]
        assert rollback_res["target_release"] == rel1["release_id"]
        assert current_symlink.resolve().name == rel1["release_id"]
