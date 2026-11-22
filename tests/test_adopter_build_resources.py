"""Release assembly refuses incomplete artifacts; ordinary builds warn and continue."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from unified_agent.webui.build_resources import (
    RELEASE_ENV,
    RESOURCE_PATHS,
    release_build_requested,
    resource_mappings,
)


def _resources(root: Path):
    dist = root / RESOURCE_PATHS[0]
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("synthetic UI")
    (dist / "assets/app.js").write_text("window.synthetic = true;")
    catalog = root / RESOURCE_PATHS[1]
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"items": []}')


def test_editable_and_unrelated_targets_need_no_release_assets(tmp_path):
    assert resource_mappings(tmp_path, "wheel", "editable") == {}
    assert resource_mappings(tmp_path, "other", "standard") == {}


@pytest.mark.parametrize(
    "target,prefix", [("wheel", "unified_agent"), ("sdist", "src/unified_agent")]
)
def test_release_includes_both_resource_families(tmp_path, target, prefix):
    _resources(tmp_path)
    assert resource_mappings(tmp_path, target, "standard") == {
        str(tmp_path / rel): f"{prefix}/_resources/{rel}" for rel in RESOURCE_PATHS
    }


def test_sdist_wheel_rebuild_uses_embedded_assets_without_node(tmp_path):
    packaged = tmp_path / "src/unified_agent/_resources"
    _resources(packaged)
    assert resource_mappings(tmp_path, "wheel", "standard") == {
        str(packaged / rel): f"unified_agent/_resources/{rel}" for rel in RESOURCE_PATHS
    }


def test_missing_ui_refuses_a_broken_release(tmp_path):
    with pytest.raises(ValueError, match="Control Center release assets"):
        resource_mappings(tmp_path, "wheel", "standard", release=True)


@pytest.mark.parametrize("value", [None, "broken json", "[]", '{"items": "wrong type"}'])
def test_missing_or_invalid_catalog_refuses_a_broken_release(tmp_path, value):
    _resources(tmp_path)
    catalog = tmp_path / RESOURCE_PATHS[1]
    if value is None:
        catalog.unlink()
    else:
        catalog.write_text(value)
    with pytest.raises(ValueError, match="capability catalog"):
        resource_mappings(tmp_path, "wheel", "standard", release=True)


def test_resource_symlink_is_not_packaged(tmp_path):
    _resources(tmp_path)
    other = tmp_path / "unrelated.txt"
    other.write_text("do not copy")
    (tmp_path / RESOURCE_PATHS[0] / "leak.txt").symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        resource_mappings(tmp_path, "wheel", "standard")


def test_missing_js_assets_refuses_a_broken_release(tmp_path):
    _resources(tmp_path)
    shutil.rmtree(tmp_path / RESOURCE_PATHS[0] / "assets")
    with pytest.raises(ValueError, match="Control Center release assets"):
        resource_mappings(tmp_path, "sdist", "standard", release=True)


def test_resource_symlink_ancestor_is_not_packaged(tmp_path):
    outside = tmp_path / "outside"
    _resources(outside)
    source = tmp_path / "source"
    source.mkdir()
    (source / "packages").symlink_to(outside / "packages", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        resource_mappings(source, "wheel", "standard")


@pytest.fixture
def no_release_env(monkeypatch):
    monkeypatch.delenv(RELEASE_ENV, raising=False)


@pytest.mark.parametrize("target", ["wheel", "sdist"])
def test_ordinary_build_of_unbuilt_tree_warns_and_packages_nothing(
    tmp_path, target, no_release_env
):
    """`uv tool install .`, install.sh and clean_install_smoke build this way."""
    warnings: list[str] = []
    assert resource_mappings(tmp_path, target, "standard", warn=warnings.append) == {}
    assert len(warnings) == 1
    assert "Control Center release assets are missing" in warnings[0]
    assert RELEASE_ENV in warnings[0] and "503" in warnings[0]


def test_ordinary_build_with_invalid_catalog_warns_and_packages_nothing(tmp_path, no_release_env):
    _resources(tmp_path)
    (tmp_path / RESOURCE_PATHS[1]).write_text("broken json")
    warnings: list[str] = []
    assert resource_mappings(tmp_path, "wheel", "standard", warn=warnings.append) == {}
    assert "capability catalog" in warnings[0]


def test_ordinary_build_of_a_built_tree_still_packages_resources(tmp_path, no_release_env):
    _resources(tmp_path)
    warnings: list[str] = []
    mapped = resource_mappings(tmp_path, "wheel", "standard", warn=warnings.append)
    assert mapped == {
        str(tmp_path / rel): f"unified_agent/_resources/{rel}" for rel in RESOURCE_PATHS
    }
    assert warnings == []


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_release_env_flag_refuses_the_same_unbuilt_tree(tmp_path, monkeypatch, value):
    monkeypatch.setenv(RELEASE_ENV, value)
    assert release_build_requested()
    with pytest.raises(ValueError, match="Control Center release assets"):
        resource_mappings(tmp_path, "wheel", "standard", warn=pytest.fail)


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_release_env_flag_off_values(monkeypatch, value):
    monkeypatch.setenv(RELEASE_ENV, value)
    assert not release_build_requested()


def test_ordinary_build_still_refuses_symlinked_resources(tmp_path, no_release_env):
    _resources(tmp_path)
    other = tmp_path / "unrelated.txt"
    other.write_text("do not copy")
    (tmp_path / RESOURCE_PATHS[0] / "leak.txt").symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        resource_mappings(tmp_path, "wheel", "standard", warn=lambda _m: None)


@pytest.mark.parametrize("release", [True, False])
def test_extracted_sdist_with_rebuilt_ui_keeps_its_embedded_catalog(tmp_path, release):
    """Rebuilt top-level UI + catalog only in the sdist bundle: map each one."""
    packaged = tmp_path / "src/unified_agent/_resources"
    _resources(packaged)
    _resources(tmp_path)
    (tmp_path / RESOURCE_PATHS[1]).unlink()  # an sdist has no top-level catalog
    mapped = resource_mappings(tmp_path, "wheel", "standard", release=release, warn=pytest.fail)
    assert mapped == {
        str(tmp_path / RESOURCE_PATHS[0]): f"unified_agent/_resources/{RESOURCE_PATHS[0]}",
        str(packaged / RESOURCE_PATHS[1]): f"unified_agent/_resources/{RESOURCE_PATHS[1]}",
    }


def test_catalog_symlink_is_refused_wherever_it_is_resolved(tmp_path):
    _resources(tmp_path)
    catalog = tmp_path / RESOURCE_PATHS[1]
    real = tmp_path / "real-catalog.json"
    catalog.rename(real)
    catalog.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        resource_mappings(tmp_path, "wheel", "standard", release=True)
