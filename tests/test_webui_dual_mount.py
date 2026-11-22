"""Single-mount /ui: the Vite SPA is the only shell (#889, retired #993).

The legacy vanilla mount (/ui/legacy, /ui/excalidraw) was removed in #993 —
these tests pin the surviving contract: dist present → SPA + immutable
content-addressed assets; dist absent → hard 503 with a build hint.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.webui import spa as web_spa

TOKEN = f"tok-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _write_dist(dist, index_html: str = "<html><body>modern</body></html>"):
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(index_html, encoding="utf-8")
    return dist


def test_modern_spa_served_when_dist_present(client, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id='root'>modern-shell</div>"
        "<script type='module' src='/ui/assets/app.js'></script></body></html>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('modern')", encoding="utf-8")
    monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)

    index = client.get("/ui")
    assert index.status_code == 200
    assert "modern-shell" in index.text
    assert index.headers.get("cache-control") == "no-cache"

    asset = client.get("/ui/assets/app.js")
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert "immutable" in asset.headers.get("cache-control", "")

    spa = client.get("/ui/tasks")
    assert spa.status_code == 200
    assert "modern-shell" in spa.text

    missing = client.get("/ui/assets/missing.js")
    assert missing.status_code == 404


def test_modern_spa_remains_available_in_core_edition(client, tmp_path, monkeypatch):
    """Edition gating belongs to enterprise APIs, not the modern shell mount."""
    dist = _write_dist(
        tmp_path / "dist",
        "<html><body><div id='root'>modern-core-shell</div></body></html>",
    )
    monkeypatch.setenv("UA_EDITION", "core")
    monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)

    response = client.get("/ui")

    assert response.status_code == 200
    assert "modern-core-shell" in response.text


def test_modern_vendor_assets_are_served_immutably(client, tmp_path, monkeypatch):
    """`dist/vendor/` holds the prebuilt dependency files the Vite build keeps
    out of the bundle graph (packages/web/vite.config.ts). The directory name
    carries a content hash, so it must get the same immutable cache as
    `assets/` — not the no-cache default."""
    dist = tmp_path / "dist"
    vendor = dist / "vendor" / "mermaid-parser-core-5c83305b"
    vendor.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>modern</body></html>", encoding="utf-8")
    (vendor / "chunk-FOHPRMQF.mjs").write_text("export const x = 1", encoding="utf-8")
    monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)

    res = client.get("/ui/vendor/mermaid-parser-core-5c83305b/chunk-FOHPRMQF.mjs")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert "immutable" in res.headers.get("cache-control", "")


def test_retired_legacy_and_excalidraw_mounts_404(client, tmp_path, monkeypatch):
    """#993: /ui/legacy* and /ui/excalidraw/* are gone — including the
    extensionless `/ui/legacy`, which must 404 rather than fall through to the
    SPA client-route fallback and render the modern shell."""
    dist = _write_dist(tmp_path / "dist")
    monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)

    for path in (
        "/ui/legacy",
        "/ui/legacy/app.js",
        "/ui/legacy/manifest.json",
        "/ui/legacy/excalidraw/bundle.js",
        "/ui/excalidraw/bundle.js",
    ):
        assert client.get(path).status_code == 404, path


def test_missing_dist_is_503_with_build_hint(client, tmp_path, monkeypatch):
    """No legacy fallback (#993): a hub without the Vite build gets a hard 503
    naming the fix, on the index and on asset paths alike."""
    monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", tmp_path / "no-dist-here")

    index = client.get("/ui")
    assert index.status_code == 503
    assert "packages/web" in index.text

    assert client.get("/ui/assets/app.js").status_code == 503
    # The retired prefixes stay 404 even when the build is missing.
    assert client.get("/ui/legacy").status_code == 404
