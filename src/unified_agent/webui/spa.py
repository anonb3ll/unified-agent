"""Single-mount helpers for the modern Vite SPA.

``/ui`` serves packaged assets, or ``packages/web/dist`` in a source checkout
(Vite ``base: '/ui/'``), when the build exists. When ``dist/index.html`` is
absent every ``/ui`` response is a hard 503 with a build hint — the legacy
vanilla shell and its ``/ui/legacy`` mount were retired in task #993; there is
no fallback UI.
"""

from __future__ import annotations

import mimetypes
import os
import re
import tomllib
from pathlib import Path

from starlette.responses import PlainTextResponse, Response

# Static resources resolve against either a genuine source checkout (editable
# development) or the bundle INSIDE the installed package (``_resources``).
# The UI build and the capability catalog are resolved independently: an
# editable install of an extracted sdist can have a rebuilt checkout UI while
# its catalog exists only in the embedded bundle.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_DISTRIBUTION = "unified-agent"
_UI_INDEX = "packages/web/dist/index.html"
_CATALOG = "docs/capability-catalog/catalog.json"


def _is_source_checkout(package_root: Path) -> bool:
    """True only for this project's own ``src/`` layout, not a copied wheel.

    ``pip install --target <project>/src`` also yields ``src/unified_agent``
    beside a ``pyproject.toml``, so a checkout must also: declare this
    distribution as its project name, have no installed-wheel ``dist-info``
    beside the package, and not live under a ``site-packages`` directory.
    """
    parent = package_root.parent
    if parent.name != "src" or {"site-packages", "dist-packages"} & set(parent.parts):
        return False
    if any(parent.glob(f"{package_root.name}-*.dist-info")):
        return False
    pyproject = parent.parent / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return isinstance(name, str) and re.sub(r"[-_.]+", "-", name).lower() == _DISTRIBUTION


def _resource_root(package_root: Path, relative: str) -> Path:
    """The first root that actually has ``relative``.

    Order: a genuine source checkout, then the packaged ``_resources`` bundle.
    When neither has it, a genuine checkout keeps its root (``/ui`` gives the
    build hint) and anything else gets the missing packaged path, so ``/ui``
    answers 503 rather than adopting unrelated assets from a parent directory.
    """
    packaged = package_root / "_resources"
    candidates = [packaged]
    if _is_source_checkout(package_root):
        candidates.insert(0, package_root.parent.parent)
    for root in candidates:
        if (root / relative).is_file():
            return root
    return candidates[0]


UI_ROOT = _resource_root(_PACKAGE_ROOT, _UI_INDEX)
CATALOG_ROOT = _resource_root(_PACKAGE_ROOT, _CATALOG)
# api.py imports REPO_ROOT for the capability catalog; keep that name as the
# catalog root's compatibility alias.
REPO_ROOT = CATALOG_ROOT
MODERN_DIST_DIR = UI_ROOT / "packages" / "web" / "dist"

_ASSET_CACHE = "public, max-age=31536000, immutable"
_INDEX_CACHE = "no-cache"


def _dist_root(dist_dir: Path | None = None) -> Path:
    if dist_dir is not None:
        return dist_dir
    configured = os.environ.get("UA_WEB_DIST_DIR")
    return Path(configured).expanduser() if configured else MODERN_DIST_DIR


def modern_dist_ready(dist_dir: Path | None = None) -> bool:
    root = _dist_root(dist_dir)
    return (root / "index.html").is_file()


def _safe_under(base: Path, rel: str) -> Path | None:
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def serve_modern(rel: str = "", *, dist_dir: Path | None = None) -> Response:
    root = _dist_root(dist_dir).resolve()
    if not (root / "index.html").is_file():
        return PlainTextResponse(
            "modern web UI build missing — run `npm --prefix packages/web run build`",
            status_code=503,
        )

    rel = (rel or "").lstrip("/")
    if rel in ("", "."):
        index = root / "index.html"
        return Response(
            index.read_text(encoding="utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"cache-control": _INDEX_CACHE},
        )

    path = _safe_under(root, rel)
    if path is not None:
        media_type, _ = mimetypes.guess_type(str(path))
        media_type = media_type or "application/octet-stream"
        if path.suffix.lower() in {".js", ".mjs"}:
            media_type = "text/javascript; charset=utf-8"
        elif path.suffix.lower() == ".css":
            media_type = "text/css; charset=utf-8"
        elif path.suffix.lower() == ".html":
            media_type = "text/html; charset=utf-8"
        # Both directories are content-addressed by the Vite build: `assets/`
        # by per-file hash, `vendor/` by a hash in the directory name (see
        # packages/web/vite.config.ts). Everything else may change in place.
        posix = path.as_posix()
        immutable = "/assets/" in posix or "/vendor/" in posix
        cache = _ASSET_CACHE if immutable else _INDEX_CACHE
        return Response(
            path.read_bytes(),
            media_type=media_type,
            headers={"cache-control": cache},
        )

    # SPA fallback: extensionless client routes → index.html; real missing assets 404.
    if Path(rel).suffix:
        return PlainTextResponse("not found", status_code=404)
    index = root / "index.html"
    return Response(
        index.read_text(encoding="utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"cache-control": _INDEX_CACHE},
    )
