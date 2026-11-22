"""The export skip fires only in an export, and only for a file that is missing (task #2615).

An exported tree carries no ``deploy/mac-hub/`` wrappers, so the test bodies
that exercise them skip there. The same bodies in the private repo must still
run (and fail on a renamed file) — a green skip must never hide a regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from export_guard import (
    exported_skip,
    is_public_export,
    needs_exported_paths,
    require_exported_paths,
)

WRAPPER = "deploy/mac-hub/run-litestream.sh"


def _tree(root: Path, *, export: bool) -> Path:
    """A repo root that looks private (release tooling) or exported (none)."""
    repo = root / ("export" if export else "private")
    (repo / "scripts").mkdir(parents=True)
    if not export:
        (repo / "scripts" / "public_release").mkdir()
    return repo


def _wrapper(repo: Path) -> Path:
    path = repo / WRAPPER
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def test_only_an_export_lacks_the_release_tooling(tmp_path):
    assert is_public_export(_tree(tmp_path / "a", export=True))
    assert not is_public_export(_tree(tmp_path / "b", export=False))


def test_skip_needs_an_export_and_a_missing_file(tmp_path):
    private = _tree(tmp_path / "a", export=False)
    exported = _tree(tmp_path / "b", export=True)
    present = _wrapper(private)

    # Private repo, file present: the body runs.
    assert not exported_skip(private, present)
    # Private repo, file gone: still runs, so the body fails loudly on the read.
    assert not exported_skip(private, private / "deploy/mac-hub/renamed.sh")
    # Export, file absent: skip.
    assert exported_skip(exported, exported / WRAPPER)
    # Export, file present (a future export that keeps it): run.
    assert not exported_skip(exported, _wrapper(exported))


def test_marker_carries_the_condition_and_names_the_path(tmp_path):
    exported = _tree(tmp_path, export=True)
    marker = needs_exported_paths(exported / WRAPPER, repo=exported)
    assert marker.mark.args[0] is True
    assert WRAPPER in marker.mark.kwargs["reason"]


def test_call_site_form_skips_only_when_the_export_drops_the_file(tmp_path):
    exported = _tree(tmp_path, export=True)
    with pytest.raises(pytest.skip.Exception, match=WRAPPER):
        require_exported_paths(exported, exported / WRAPPER)
