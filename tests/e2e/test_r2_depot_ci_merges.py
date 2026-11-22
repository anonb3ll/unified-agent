"""Release packaging, cutover, and rollback integration checks.

Depot CI and GitHub merge gates are verified by their own checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from unified_agent import release

# ==============================================================================
# TIER 1: FEATURE COVERAGE (R2)
# ==============================================================================


class TestR2FeatureCoverage:
    """Tier 1: Primary behavioral coverage for Requirement R2."""

    def test_r2_immutable_release_manifest_creation(self, tmp_path: Path):
        """Verify release packaging produces immutable files and accurate MANIFEST.json.

        Authoritative Source: src/unified_agent/release.py, spec_miner_survey_3 § 1.3.
        """
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("print('v0.3.3')\n")
        releases_dir = tmp_path / "releases"

        res = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v0.3.3",
            creator="Desktop_Agent7",
            authorize=False,
        )
        assert res["ok"] is True
        target = Path(res["release_path"])
        manifest_file = target / "MANIFEST.json"
        assert manifest_file.exists()

        # Check read-only permissions
        app_file = target / "app.py"
        assert (app_file.stat().st_mode & 0o222) == 0

        # Check manifest data
        manifest_data = json.loads(manifest_file.read_text())
        assert manifest_data["version"] == "v0.3.3"
        assert manifest_data["creator"] == "Desktop_Agent7"
        assert "app.py" in manifest_data["files"]

    def test_r2_atomic_release_cutover_and_rollback(self, tmp_path: Path):
        """Verify release cutover with --authorize and atomic rollback to prior release.

        Authoritative Source: release.py cutover & rollback proof.
        """
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("print('v1')\n")
        releases_dir = tmp_path / "releases"

        # 1. Create first release with authorize=True
        r1 = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            creator="agent",
            authorize=True,
        )
        current_symlink = releases_dir / "unified-agent" / "current"
        assert current_symlink.is_symlink()
        assert current_symlink.resolve() == Path(r1["release_path"]).resolve()

        # 2. Create second release with authorize=True
        (source / "app.py").write_text("print('v2')\n")
        r2 = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v2.0.0",
            creator="agent",
            authorize=True,
        )
        assert current_symlink.resolve() == Path(r2["release_path"]).resolve()

        # 3. Rollback to r1 with authorize=True
        r1_id = Path(r1["release_path"]).name
        rollback_res = release.rollback_release(
            releases_base_dir=releases_dir,
            component="unified-agent",
            target_release_id=r1_id,
            authorize=True,
        )
        assert rollback_res["ok"] is True
        assert current_symlink.resolve() == Path(r1["release_path"]).resolve()


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (R2)
# ==============================================================================


class TestR2BoundaryAndCornerCases:
    """Tier 2: Boundary conditions and adversarial corner cases for Requirement R2."""

    def test_r2_release_tampered_manifest_refuses_rollback(self, tmp_path: Path):
        """Adversarial Case: Tampered file in release directory fails manifest checksum verification."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("original content")
        releases_dir = tmp_path / "releases"

        r1 = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="unified-agent",
            version="v1.0.0",
            creator="agent",
            authorize=True,
        )
        r1_path = Path(r1["release_path"])
        app_file = r1_path / "app.py"

        # Make file writable and tamper with content
        os.chmod(app_file, 0o644)
        app_file.write_text("tampered content")

        # Rollback should detect checksum mismatch and abort
        r1_id = r1_path.name
        rollback_res = release.rollback_release(
            releases_base_dir=releases_dir,
            component="unified-agent",
            target_release_id=r1_id,
            authorize=True,
        )
        assert rollback_res["ok"] is False
        err = rollback_res.get("error", "").lower()
        assert (
            "no valid release" in err
            or "not a valid release" in err
            or "verification failed" in err
        )

    def test_r2_release_without_authorize_does_not_switch_symlink(self, tmp_path: Path):
        """Boundary: create_release with authorize=False leaves current symlink unchanged."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("v1")
        releases_dir = tmp_path / "releases"

        # Baseline release with authorize
        r1 = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="ua",
            version="v1",
            creator="agent",
            authorize=True,
        )
        current = releases_dir / "ua" / "current"
        assert current.resolve() == Path(r1["release_path"]).resolve()

        # New release with authorize=False
        (source / "file.txt").write_text("v2")
        r2 = release.create_release(
            source_dir=source,
            releases_base_dir=releases_dir,
            component="ua",
            version="v2",
            creator="agent",
            authorize=False,
        )
        assert r2["updated_current"] is False
        # Symlink still points to r1
        assert current.resolve() == Path(r1["release_path"]).resolve()
