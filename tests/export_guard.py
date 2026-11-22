"""The one skip rule for tests that read files the public export drops (task #2615).

``scripts/public_release/exclude.txt`` drops ``deploy/mac-hub/``, the
``deploy/gcp/`` fragments and the release tooling itself, so an exported tree
carries no Mac wrappers at all. The private repo still ships them, so a missing
file there has to keep failing loudly instead of skipping green: the skip needs
BOTH an export and the missing file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def is_public_export(repo: Path) -> bool:
    """True when ``repo`` is an exported tree: it carries no release tooling."""
    return not (repo / "scripts" / "public_release").is_dir()


def exported_skip(repo: Path, *paths: Path) -> bool:
    """True when a body reading ``paths`` must skip: an export lacks one of them."""
    return is_public_export(repo) and any(not path.exists() for path in paths)


def needs_exported_paths(*paths: Path, repo: Path) -> pytest.MarkDecorator:
    """``skipif`` marker for a test body that reads files the export does not carry."""
    return pytest.mark.skipif(
        exported_skip(repo, *paths),
        reason="not carried by the public export: "
        + ", ".join(_display(path, repo) for path in paths),
    )


def require_exported_paths(repo: Path, *paths: Path) -> None:
    """Call-site form for a fixture or a body that cannot carry a marker."""
    if exported_skip(repo, *paths):
        pytest.skip(
            "not carried by the public export: "
            + ", ".join(_display(path, repo) for path in paths if not path.exists())
        )


def _display(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.name
