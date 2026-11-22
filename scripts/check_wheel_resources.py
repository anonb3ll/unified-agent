#!/usr/bin/env python3
"""Check a built wheel's UI/catalog/auth from an unrelated scratch directory.

Run with the project's dependency environment; dependencies are reused, but
the imported product must come exclusively from the extracted wheel. This is
an artifact check, not a cold dependency-resolution or browser-rendering test.
No real hub, vault, provider, service or database is contacted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PROBE = r"""
import json, os, secrets
from html.parser import HTMLParser
from pathlib import Path
import unified_agent
from unified_agent import server, storage, webui
from unified_agent.webui import spa
from starlette.applications import Starlette
from starlette.testclient import TestClient

def require(condition, message):
    # Explicit checks: `python -O` would strip bare asserts.
    if not condition:
        raise SystemExit("probe check failed: " + str(message)[:500])

installed = Path(os.environ["UA_ARTIFACT_IMPORT_ROOT"]).resolve()
require(Path(unified_agent.__file__).resolve().is_relative_to(installed), "product not imported from the wheel")
require(spa.REPO_ROOT.is_relative_to(installed), "resources not package-local")
require(spa.UI_ROOT.is_relative_to(installed), "UI not package-local")
token = secrets.token_urlsafe(24)
os.environ["UA_AGENT_TOKENS"] = "Local_Adopter:" + token
storage.init_db()

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if value and value.startswith("/ui/"):
            self.paths.append(value)

app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
with TestClient(app) as client:
    response = client.get("/ui")
    require(response.status_code == 200, response.text)
    parser = Assets(); parser.feed(response.text)
    require(parser.paths, "index does not reference any built assets")
    for asset in parser.paths:
        result = client.get(asset)
        require(result.status_code == 200, asset)
        require(result.content, asset)
    require(client.get("/api/capability-catalog").status_code in {401, 403}, "unauthenticated catalog read allowed")
    require(client.get("/api/capability-catalog", headers={"Authorization": "Bearer invalid"}).status_code in {401, 403}, "wrong bearer allowed")
    response = client.get("/api/capability-catalog", headers={"Authorization": "Bearer " + token})
    require(response.status_code == 200, response.text)
    payload = response.json()
    require(payload["available"] is True, "catalog unavailable")
    require(payload["manifest"], "catalog manifest missing")
    count = payload["meta"]["total_items"]

print(json.dumps({"ui_status": 200, "index_assets_checked": len(parser.paths),
                  "unauthenticated_api_refused": True, "wrong_token_refused": True,
                  "catalog_available": True, "catalog_items": count,
                  "package_local_resources": True}))
"""


_ENV_ALLOW = frozenset(
    {"PATH", "HOME", "LANG", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PYTHONIOENCODING"}
)


class WheelCheckError(ValueError):
    """The built wheel is missing a required resource or ships a build-only file."""


def check(wheel: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="ua-wheel-check-") as scratch:
        root = Path(scratch)
        installed = root / "site-packages"
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                    raise ValueError("wheel contains an unsafe member path")
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("wheel contains a symlink")
            archive.extractall(installed)
            names = archive.namelist()
        prefix = "unified_agent/_resources/"
        for required in (
            prefix + "packages/web/dist/index.html",
            prefix + "docs/capability-catalog/catalog.json",
        ):
            if required not in names:
                raise WheelCheckError(f"wheel is missing {required}")
        if "unified_agent/webui/_build_hook.py" in names:
            raise WheelCheckError("wheel ships the hatch build hook")
        # Allowlist, not denylist: the probe gets only what an interpreter needs,
        # so no credential in the caller's environment can reach it.
        env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW or k.startswith("LC_")}
        env.update(
            PYTHONPATH=str(installed),
            PYTHONDONTWRITEBYTECODE="1",
            UA_ARTIFACT_IMPORT_ROOT=str(installed),
            UA_SETTINGS_FILE="",
            UA_STATE_SYNC="0",
            UA_HOME=str(root / "state"),
            UA_WORKSPACE=str(root / "workspace"),
            UA_DEV_ROOT=str(root / "dev"),
            UA_VAULT_PATH="",
            UA_TRANSPORT="http",
            UA_HTTP_ALLOWED_HOSTS="testserver,localhost:*,127.0.0.1:*",
            UA_HTTP_HOST="127.0.0.1",
            UA_HTTP_PORT="8765",
            UA_ENFORCE_AUTH="1",
            UA_DEPLOYMENT_MODE="authenticated_private",
            UA_OPERATOR_LABEL="Human_Owner",
            UA_OPERATOR_ALIASES="",
            UA_OPERATOR_NAME="Synthetic Owner",
        )
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        payload = json.loads(result.stdout)
        payload.update(
            wheel=wheel.name,
            sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            wheel_members=len(names),
        )
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.wheel.resolve(strict=True))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"wheel artifact check failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
