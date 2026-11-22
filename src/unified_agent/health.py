"""Liveness and readiness probes as always-loaded HTTP routes.

`/healthz` and `/readyz` are not Control Center routes. A hub started with
``UA_COMPONENTS=kernel`` does not load ``control-center``, and its routes are
therefore not registered; deployment health checks (``deploy/gcp/control-plane/
verify.sh``, the Mac hub scripts, the fleet node roll) still require both
endpoints to answer on any HTTP hub (task #2603 review finding).

They live here, in the always-loaded adapter, and ``webui/routes.py`` keeps its
own copies for the full-surface route list: Control Center may not import the
composition root (docs/architecture/components.md), so the two cannot share one
function. Keep them in step; the readiness logic itself stays in
``ready.readiness``. The handlers stay auth-exempt and content-free: ``healthz``
reveals only that a process is listening, ``readyz`` reports store writability
and WAL sanity without an org id, token or path.
"""

from __future__ import annotations

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


async def healthz(request: Request) -> Response:
    """Liveness probe. Deliberately auth-exempt and deliberately content-free.

    It reveals only that a process is listening — no version, no agent names, nothing
    that would help someone who should not be here.

    An unknown ``UA_TOOLSETS`` is the exception: the probe returns ``{"ok": false}``
    with no toolset name, so a hub that reached this route still fails the check.
    """
    from .toolsets import ToolsetConfigError, require_known_toolsets

    try:
        require_known_toolsets()
    except ToolsetConfigError:
        return _json({"ok": False}, status=503)
    return _json({"ok": True})


async def readyz(request: Request) -> Response:
    """Readiness probe. Auth-exempt. 503 when the live store cannot be written.

    Reports store writability and WAL sanity, plus an advisory WorkOS TCP
    result that never fails the probe. No org id, token, or path.
    """
    from . import ready

    body = await anyio.to_thread.run_sync(ready.readiness)
    return _json(body, status=200 if body["ok"] else 503)


def health_routes() -> list[Route]:
    """The two probe routes, in the order the adapter registers them."""
    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
    ]


__all__ = ["health_routes", "healthz", "readyz"]
