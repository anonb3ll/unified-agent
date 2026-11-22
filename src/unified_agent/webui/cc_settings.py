"""Control Center v2 settings routes: session credentials and the evidence export (#2516).

* ``GET /api/session-credentials`` lists session-scoped MCP credentials for the
  verified human operator: metadata only (``session_credentials.list_credentials``),
  never a value or its digest, tenant-scoped, ``Cache-Control: no-store``. Revoking
  stays ``POST /api/action/session-credential-revoke``.
* ``GET /api/evidence/export`` is the complete evidence export: every matching
  record, not one capped page. CSV streams one keyset page at a time
  (``evidence_report.iter_evidence_report_csv``); PDF, and CSV on a hub that signs
  its exports, is built whole first, because the signature covers the full body.

Request and response shapes are in ``docs/control-center-api.md``.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import edition, session_credentials
from ..audit import evidence_signing
from ..node_registry import NodeError
from . import routes

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


async def session_credential_list(request: Request) -> Response:
    """GET /api/session-credentials — the operator's credential list, metadata only."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    if not request.scope.get("ua_identity"):
        return routes._json({"error": "authenticated identity required"}, status=401)
    if not routes._operator_recovery(request):
        return routes._json(
            {"error": "listing session credentials is an operator action"}, status=403
        )
    params = request.query_params
    unknown = sorted(set(params) - {"state", "node", "limit"})
    if unknown:
        return routes._json({"error": "unknown parameter(s): " + ", ".join(unknown)}, status=400)
    limit_raw = params.get("limit", "200")
    if not limit_raw.isdigit():
        return routes._json({"error": "limit must be a positive integer"}, status=400)
    try:
        listed = await anyio.to_thread.run_sync(
            lambda: session_credentials.list_credentials(
                state=params.get("state", "active"),
                node=params.get("node") or None,
                limit=int(limit_raw),
            )
        )
    except (ValueError, NodeError) as exc:
        return routes._json({"error": str(exc)[:300]}, status=400)
    return routes._json(listed, headers=NO_STORE)


#: Query parameters of the export: the ``/api/evidence`` filters plus ``format``.
_EXPORT_PARAMS = frozenset(
    {"format", "task_id", "task", "since", "until", "agent", "project", "pr", "item"}
)
_CSV = "text/csv; charset=utf-8"


def _export_filters(request: Request) -> dict[str, Any] | Response:
    params = request.query_params
    unknown = sorted(set(params) - _EXPORT_PARAMS)
    if unknown:
        return routes._json({"error": "unknown parameter(s): " + ", ".join(unknown)}, status=400)
    task_raw = params.get("task_id") or params.get("task")
    if task_raw is not None and not task_raw.isdigit():
        return routes._json({"error": f"invalid task_id: {task_raw!r}"}, status=400)
    item = (params.get("item") or "").strip() or None
    if item is not None:
        from ..audit.evidence_report import parse_item_ref

        try:
            parse_item_ref(item)
        except ToolError as exc:
            return routes._json_body_error(exc, status=400, limit=None)
    return {
        "task_id": int(task_raw) if task_raw else None,
        "since": params.get("since"),
        "until": params.get("until"),
        "agent": params.get("agent"),
        "project": params.get("project"),
        "pr": params.get("pr"),
        "item": item,
    }


def _signed(body: str | bytes, report: dict[str, Any], media_type: str, filename: str):
    chain = report.get("chain_integrity") or {}
    try:
        sidecar = evidence_signing.sign_export(
            evidence_signing.encode_for_signing(body),
            chain_head=str(chain.get("head_hash") or ""),
            generated_at=str(report.get("generated_at") or ""),
        )
    except RuntimeError as exc:
        return routes._json({"error": str(exc)}, status=503)
    headers = {
        "content-disposition": f'attachment; filename="{filename}"',
        "x-evidence-complete": "true",
    }
    headers.update(evidence_signing.response_headers(sidecar))
    return Response(
        evidence_signing.encode_for_signing(body), media_type=media_type, headers=headers
    )


async def evidence_export(request: Request) -> Response:
    """GET /api/evidence/export?format=csv|pdf — every matching record, uncapped."""
    from ..audit.evidence_report import (
        build_evidence_report,
        format_evidence_report,
        iter_evidence_report_csv,
    )

    guard = routes._guard(request)
    if guard is not None:
        return guard
    fmt = (request.query_params.get("format") or "").lower()
    if fmt not in ("csv", "pdf"):
        return routes._json({"error": "format must be csv or pdf"}, status=400)
    filters = _export_filters(request)
    if isinstance(filters, Response):
        return filters
    try:
        edition.require_feature("evidence_reporting")
    except edition.FeatureNotAvailableError as exc:
        return routes._json({"error": str(exc)}, status=403)
    signing = evidence_signing.configured()
    if fmt == "csv" and not signing:
        return StreamingResponse(
            iter_evidence_report_csv(**filters),
            media_type=_CSV,
            headers={
                "content-disposition": 'attachment; filename="evidence-report.csv"',
                "x-evidence-complete": "true",
            },
        )
    if fmt == "csv":
        return await _signed_csv(filters)
    report = await anyio.to_thread.run_sync(lambda: build_evidence_report(complete=True, **filters))
    if signing:
        try:
            report = evidence_signing.with_report_metadata(report)
        except RuntimeError as exc:
            return routes._json({"error": str(exc)}, status=503)
    try:
        body = format_evidence_report(report, fmt=fmt)
    except ValueError as exc:
        return routes._json({"error": str(exc)}, status=422)
    except RuntimeError as exc:
        return routes._json({"error": str(exc)}, status=503)
    media_type = _CSV if fmt == "csv" else "application/pdf"
    filename = f"evidence-report.{fmt}"
    if signing:
        return _signed(body, report, media_type, filename)
    return Response(
        body,
        media_type=media_type,
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "x-evidence-complete": "true",
        },
    )


def _spool_signed_csv(filters: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Page the signed CSV into a private temporary file, signing as it is written.

    Returns the file's path and its signature sidecar. The file is removed when
    anything fails; otherwise the response removes it once it has been sent.
    """
    from ..audit.evidence_report import iter_signed_evidence_report_csv

    fd, path = tempfile.mkstemp(prefix="ua-evidence-", suffix=".csv")
    try:
        chunks, sidecar = iter_signed_evidence_report_csv(**filters)
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
        return path, sidecar()
    except BaseException:
        os.unlink(path)
        raise


async def _signed_csv(filters: dict[str, Any]) -> Response:
    """The complete signed CSV, bounded in memory (review #1538).

    The signature covers the whole body, so it cannot go out in headers before
    the body is written: the export is paged into a temporary file with an
    incremental signer, then that file is streamed and deleted.
    """
    try:
        path, sidecar = await anyio.to_thread.run_sync(lambda: _spool_signed_csv(filters))
    except RuntimeError as exc:
        return routes._json({"error": str(exc)}, status=503)
    headers = {
        "content-disposition": 'attachment; filename="evidence-report.csv"',
        "x-evidence-complete": "true",
        **evidence_signing.response_headers(sidecar),
    }
    return FileResponse(
        path, media_type=_CSV, headers=headers, background=BackgroundTask(os.unlink, path)
    )


ROUTES: list[Route] = [
    Route("/api/session-credentials", session_credential_list, methods=["GET"]),
    Route("/api/evidence/export", evidence_export, methods=["GET"]),
]
