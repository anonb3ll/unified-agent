"""Exercise the same resource lookup from source and wheel-style layouts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent.webui import spa


def _layout(tmp_path: Path, source: bool, install_root: Path | None = None) -> tuple[Path, Path]:
    default_install = tmp_path / "venv/lib/python/site-packages"
    root = tmp_path / "checkout" if source else (install_root or default_install)
    package = root / "src/unified_agent" if source else root / "unified_agent"
    (package / "webui").mkdir(parents=True)
    if source:
        (root / "pyproject.toml").write_text('[project]\nname = "unified-agent"\n')
    (package / "__init__.py").touch()
    (package / "webui/__init__.py").touch()
    shutil.copyfile(spa.__file__, package / "webui/spa.py")
    resources = root if source else package / "_resources"
    dist = resources / "packages/web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/ui/assets/app.js"></script>')
    (dist / "assets/app.js").write_text("window.synthetic = true;")
    catalog = resources / "docs/capability-catalog/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"items": []}')
    return package.parent, resources


def _probe(import_root: Path, tmp_path: Path, override: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("UA_")}
    env.update(PYTHONPATH=str(import_root), PYTHONDONTWRITEBYTECODE="1", UA_SETTINGS_FILE="")
    if override:
        env["UA_WEB_DIST_DIR"] = str(override)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from unified_agent.webui import spa; "
            "print(json.dumps({'root': str(spa.REPO_ROOT), 'ui': str(spa.UI_ROOT), "
            "'ready': spa.modern_dist_ready(), "
            "'status': spa.serve_modern().status_code, "
            "'catalog': (spa.REPO_ROOT/'docs/capability-catalog/catalog.json').is_file()}))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(probe.stdout)


@pytest.mark.parametrize("source", [False, True])
def test_resources_resolve_without_a_neighbor_checkout(tmp_path, source):
    import_root, resources = _layout(tmp_path, source)
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(resources),
        "ui": str(resources),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_installed_override_preserves_custom_ui_without_repointing_catalog(tmp_path):
    import_root, resources = _layout(tmp_path, False)
    override = tmp_path / "custom-ui"
    override.mkdir()
    (override / "index.html").write_text("custom UI")
    result = _probe(import_root, tmp_path, override)
    assert result["ready"] and result["status"] == 200
    assert result["root"] == str(resources)
    assert result["catalog"]


def test_installed_missing_resources_do_not_adopt_unrelated_parent_assets(tmp_path):
    import_root, resources = _layout(tmp_path, False)
    shutil.rmtree(resources)
    unrelated = import_root.parent / "packages/web/dist"
    unrelated.mkdir(parents=True)
    (unrelated / "index.html").write_text("unrelated app")
    result = _probe(import_root, tmp_path)
    assert result["root"] == str(resources)
    assert not result["ready"]
    assert result["status"] == 503
    assert not result["catalog"]


def test_install_into_a_directory_named_src_uses_packaged_resources(tmp_path):
    """`pip install --target <x>/src` is not a checkout: the marker wins."""
    import_root, resources = _layout(tmp_path, False, install_root=tmp_path / "proj/src")
    # A stray parent tree that would match the old name-only heuristic.
    stray = tmp_path / "proj/packages/web/dist"
    stray.mkdir(parents=True)
    (stray / "index.html").write_text("stray app")
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(resources),
        "ui": str(resources),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_src_named_install_without_resources_or_pyproject_is_not_a_checkout(tmp_path):
    import_root, resources = _layout(tmp_path, False, install_root=tmp_path / "proj/src")
    shutil.rmtree(resources)
    stray = tmp_path / "proj/packages/web/dist"
    stray.mkdir(parents=True)
    (stray / "index.html").write_text("stray app")
    result = _probe(import_root, tmp_path)
    assert result["root"] == str(resources)
    assert not result["ready"]
    assert result["status"] == 503


def _embed_sdist_resources(checkout: Path) -> Path:
    """Add the bundle a release sdist embeds under the package (stale build)."""
    embedded = checkout / "src/unified_agent/_resources"
    dist = embedded / "packages/web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("embedded sdist build")
    (dist / "assets/app.js").write_text("window.embedded = true;")
    catalog = embedded / "docs/capability-catalog/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"items": []}')
    return embedded


def test_editable_extracted_sdist_serves_rebuilt_ui_and_embedded_catalog(tmp_path):
    """A real extracted sdist has no top-level catalog: UI and catalog split."""
    import_root, checkout = _layout(tmp_path, True)
    embedded = _embed_sdist_resources(checkout)
    shutil.rmtree(checkout / "docs")  # an sdist carries the catalog only embedded
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(embedded),
        "ui": str(checkout),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_checkout_with_leftover_bundle_prefers_its_own_ui_and_catalog(tmp_path):
    import_root, checkout = _layout(tmp_path, True)
    _embed_sdist_resources(checkout)
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(checkout),
        "ui": str(checkout),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_editable_extracted_sdist_without_rebuild_uses_embedded_bundle(tmp_path):
    """No checkout build: the embedded bundle still serves the UI."""
    import_root, checkout = _layout(tmp_path, True)
    embedded = _embed_sdist_resources(checkout)
    shutil.rmtree(checkout / "packages/web/dist")
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(checkout),
        "ui": str(embedded),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_checkout_without_build_or_bundle_keeps_checkout_root(tmp_path):
    """A plain unbuilt checkout keeps its root: build hint for /ui, catalog found."""
    import_root, checkout = _layout(tmp_path, True)
    shutil.rmtree(checkout / "packages/web/dist")
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(checkout),
        "ui": str(checkout),
        "ready": False,
        "status": 503,
        "catalog": True,
    }


def _foreign_project(project: Path, name: str = "someone-elses-site") -> None:
    """Another project with its own pyproject.toml, built UI and catalog."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    dist = project / "packages/web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("foreign site")
    (dist / "assets/app.js").write_text("window.foreign = true;")
    catalog = project / "docs/capability-catalog/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"items": ["foreign"]}')


def _target_install(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    project = tmp_path / "site"
    _foreign_project(project, name)
    import_root, resources = _layout(tmp_path, False, install_root=project / "src")
    (project / "src/unified_agent-0.3.2.dist-info").mkdir()
    return project, import_root, resources


@pytest.mark.parametrize("name", ["someone-elses-site", "unified-agent"])
def test_target_install_into_a_built_project_src_serves_its_packaged_ui(tmp_path, name):
    """`pip install --target site/src` beside a built project: packaged wins.

    The dist-info marks an installed wheel even if the host project happens to
    share the distribution name.
    """
    _project, import_root, resources = _target_install(tmp_path, name)
    result = _probe(import_root, tmp_path)
    assert result == {
        "root": str(resources),
        "ui": str(resources),
        "ready": True,
        "status": 200,
        "catalog": True,
    }


def test_foreign_project_src_without_dist_info_is_still_not_a_checkout(tmp_path):
    """A different project name alone is enough to refuse its assets."""
    project = tmp_path / "site"
    _foreign_project(project)
    import_root, resources = _layout(tmp_path, False, install_root=project / "src")
    shutil.rmtree(resources)
    result = _probe(import_root, tmp_path)
    assert result["root"] == str(resources) and result["ui"] == str(resources)
    assert not result["ready"]
    assert result["status"] == 503
    assert not result["catalog"]
