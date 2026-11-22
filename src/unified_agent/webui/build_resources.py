"""Release resource assembly; no runtime configuration or provider access.

Builders produce the Vite bundle before making a wheel/sdist. Consumers of
those artifacts need Python only. Editable installs intentionally keep using
the checkout, including its existing missing-build diagnostic.

Only an explicit release build (``UA_RELEASE_BUILD=1``) refuses to build
without complete Control Center assets. An ordinary build of an unbuilt tree
(``uv tool install .``, ``install.sh``, a ``git+`` install) still produces a
wheel, without ``_resources``, and warns; its ``/ui`` then answers the
existing 503 build hint.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

RESOURCE_PATHS = ("packages/web/dist", "docs/capability-catalog/catalog.json")
RELEASE_ENV = "UA_RELEASE_BUILD"


class IncompleteResources(ValueError):
    """The UI build or capability catalog is missing or invalid."""


def release_build_requested(environ: Mapping[str, str] | None = None) -> bool:
    """True when the builder explicitly asked for a release build."""
    env = os.environ if environ is None else environ
    return env.get(RELEASE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _stderr_warning(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def resource_mappings(
    root: Path,
    target: str,
    version: str,
    *,
    release: bool | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Map complete UI/catalog assets into both wheel and sdist package resources.

    An sdist carries the built bundle under src/unified_agent/_resources, so
    rebuilding its wheel does not require Node or a sibling source checkout.
    A release build (``release`` True, default from ``UA_RELEASE_BUILD``)
    refuses incomplete assets rather than shipping a wheel whose UI returns 503.
    Any other build warns and packages no resources. Symlinked resources are
    refused in every mode.
    """
    if release is None:
        release = release_build_requested()
    if target == "wheel" and version == "editable":
        return {}
    if target not in {"wheel", "sdist"}:
        return {}
    packaged = root / "src/unified_agent/_resources"
    # Each resource comes from the prepared source tree when it has it, else
    # from an sdist's embedded bundle. They are chosen independently: an
    # extracted sdist with a rebuilt UI keeps its catalog only in the bundle.
    ui_base = root if (root / RESOURCE_PATHS[0]).is_dir() else packaged
    catalog_base = root if (root / RESOURCE_PATHS[1]).is_file() else packaged
    bases = dict(zip(RESOURCE_PATHS, (ui_base, catalog_base), strict=True))
    for rel, base in bases.items():
        source = base / rel
        ancestors = [
            base.joinpath(*Path(rel).parts[:i]) for i in range(1, len(Path(rel).parts) + 1)
        ]
        paths = [*ancestors, *source.rglob("*")] if source.is_dir() else ancestors
        if any(path.is_symlink() for path in paths):
            raise ValueError(f"release resource contains a symlink: {rel}")
    try:
        _check_complete(ui_base, catalog_base)
    except IncompleteResources as exc:
        if release:
            raise
        (warn or _stderr_warning)(
            f"{exc} Building without packaged Control Center resources ({RELEASE_ENV} "
            "is not set): the installed hub's /ui will answer 503 with a build hint."
        )
        return {}
    prefix = "unified_agent/_resources" if target == "wheel" else "src/unified_agent/_resources"
    return {str(base / rel): f"{prefix}/{rel}" for rel, base in bases.items()}


def _check_complete(ui_base: Path, catalog_base: Path) -> None:
    dist = ui_base / RESOURCE_PATHS[0]
    index = dist / "index.html"
    if not index.is_file() or not any((dist / "assets").glob("*.js")):
        raise IncompleteResources(
            "Control Center release assets are missing: build protocol, client and web "
            "before uv build (see docs/adopter-package-build.md). "
            "Install a prepared wheel/sdist to avoid needing Node on the hub."
        )
    catalog = catalog_base / RESOURCE_PATHS[1]
    try:
        data = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncompleteResources(
            "capability catalog release resource is missing or invalid."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise IncompleteResources(
            "capability catalog release resource must contain an items array."
        )
