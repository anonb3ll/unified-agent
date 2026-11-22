"""Durable, approval-gated governance tools for Horizon 2 connectors."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, connectors, edition, events, post_init, service_probes, storage
from ..horizon2 import (
    CONNECTOR_PACKAGES,
    GovernedServer,
    ServerGovernance,
    package_connector,
)
from . import approvals


def _identity() -> str:
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError("Horizon 2 connector governance requires an authenticated caller")
    return identity


def _from_row(row: Any) -> GovernedServer:
    return GovernedServer(
        server_id=row["server_id"],
        connector=row["connector"],
        provenance=row["provenance"],
        scopes=frozenset(json.loads(row["scopes_json"])),
        reviewed_by=row["reviewed_by"],
        review_ref=row["review_ref"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"] if "created_at" in row.keys() else None,
    )


def _server_dict(server: GovernedServer) -> dict[str, Any]:
    return {
        "server_id": server.server_id,
        "connector": server.connector,
        "provenance": server.provenance,
        "scopes": sorted(server.scopes),
        "reviewed_by": server.reviewed_by,
        "review_ref": server.review_ref,
        "revoked_at": server.revoked_at,
        "created_at": server.created_at,
        "active": server.active,
    }


def _load(server_id: str) -> GovernedServer:
    post_init.register_default_hooks()
    storage.init_db_once()
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT server_id, connector, provenance, scopes_json, reviewed_by, review_ref, revoked_at, created_at "
            "FROM governed_servers WHERE server_id=? AND org_id IS ?",
            (server_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"unregistered Horizon 2 server: {server_id}")
    return _from_row(row)


def _exists(server_id: str) -> bool:
    post_init.register_default_hooks()
    storage.init_db_once()
    conn = storage.connect()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM governed_servers WHERE server_id=? AND org_id IS ?",
                (server_id, edition.tenant_org_id()),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def horizon2_server_list(include_revoked: bool = False) -> dict:
    """List reviewed Horizon 2 connector registrations and durable revocations."""
    conn = storage.connect()
    try:
        sql = (
            "SELECT server_id, connector, provenance, scopes_json, reviewed_by, review_ref, revoked_at, created_at "
            "FROM governed_servers WHERE org_id IS ?"
        )
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        rows = conn.execute(sql + " ORDER BY server_id", (edition.tenant_org_id(),)).fetchall()
    finally:
        conn.close()
    servers = [_server_dict(_from_row(row)) for row in rows]
    return {"servers": servers, "count": len(servers)}


def horizon2_server_register(
    server_id: str,
    connector: str,
    provenance: str,
    scopes: list[str],
    review_ref: str,
) -> dict:
    """Register one reviewed connector after consuming an exact one-shot approval."""
    identity = _identity()
    manifest = connectors.get_manifest(connector)
    if manifest is not None:
        # A manifest exists for this connector kind — refuse registration
        # outright if its declared entry points do not resolve to real code.
        # This never touches `scopes`: manifest capabilities and Horizon 2
        # scopes are deliberately different vocabularies (see connectors.py).
        try:
            connectors.validate_entry_points_resolve(manifest)
        except ValueError as exc:
            raise ToolError(
                f"connector manifest for {connector!r} is invalid, registration refused: {exc}"
            ) from exc
        if not manifest.horizon2_registration_supported:
            raise ToolError(f"connector {connector!r} does not support Horizon 2 registration")
    try:
        server = GovernedServer(
            server_id=server_id,
            connector=connector,
            provenance=provenance,
            scopes=frozenset(scopes),
            reviewed_by=identity,
            review_ref=review_ref,
        )
    except (TypeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    if _exists(server.server_id):
        raise ToolError(f"Horizon 2 server is already registered: {server.server_id}")
    payload = {"operation": "register", "server": _server_dict(server)}
    approval = approvals.check_approval_gate(
        "horizon2_server_register", payload, requested_by=identity, force=True
    )
    post_init.register_default_hooks()
    storage.init_db_once()
    timestamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO governed_servers "
            "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, revoked_at, created_at, updated_at, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                server.server_id,
                server.connector,
                server.provenance,
                json.dumps(sorted(server.scopes)),
                server.reviewed_by,
                server.review_ref,
                timestamp,
                timestamp,
                edition.tenant_org_id(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # Restoration uses another connection; release the failed insert's writer lock first.
        conn.rollback()
        approvals.restore_consumed_approval(approval["approval_id"])
        raise ToolError(f"Horizon 2 server is already registered: {server.server_id}") from exc
    finally:
        conn.close()
    event_id = events.record_event(
        "horizon2.connector.registered",
        payload={"server_id": server.server_id, "review_ref": server.review_ref},
        originator=identity,
    )
    return {
        "server": _server_dict(server),
        "approval_id": approval["approval_id"],
        "event_id": event_id,
    }


def horizon2_server_revoke(server_id: str) -> dict:
    """Persistently revoke a reviewed connector after consuming exact approval."""
    identity = _identity()
    server = _load(server_id)
    if not server.active:
        return {"server": _server_dict(server), "already_revoked": True}
    payload = {"operation": "revoke", "server_id": server_id, "review_ref": server.review_ref}
    approval = approvals.check_approval_gate(
        "horizon2_server_revoke", payload, requested_by=identity, force=True
    )
    revoked_at = storage.now()
    conn = storage.connect()
    try:
        update = conn.execute(
            "UPDATE governed_servers SET revoked_at=?, updated_at=? "
            "WHERE server_id=? AND revoked_at IS NULL AND org_id IS ?",
            (revoked_at, revoked_at, server_id, edition.tenant_org_id()),
        )
        conn.commit()
    finally:
        conn.close()
    if update.rowcount == 0:
        approvals.restore_consumed_approval(approval["approval_id"])
        return {"server": _server_dict(_load(server_id)), "already_revoked": True}
    revoked = _load(server_id)
    event_id = events.record_event(
        "horizon2.connector.revoked",
        payload={"server_id": server_id, "review_ref": revoked.review_ref},
        originator=identity,
    )
    return {
        "server": _server_dict(revoked),
        "approval_id": approval["approval_id"],
        "event_id": event_id,
    }


def horizon2_connector_authorize(
    server_id: str,
    scope: str,
    task_id: int,
    trace_id: str,
    connection_ref: str | None = None,
) -> dict:
    """Consume approval bound to connector, scope, task, and trace before use."""
    identity = _identity()
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
        raise ToolError("task_id must be a positive integer")
    if not isinstance(trace_id, str) or not trace_id.strip() or len(trace_id) > 128:
        raise ToolError("trace_id must be a non-empty string of at most 128 characters")
    loaded = _load(server_id)
    manifest = connectors.get_manifest(loaded.connector)
    if manifest is not None and not manifest.horizon2_registration_supported:
        raise ToolError(f"connector {loaded.connector!r} does not support Horizon 2 authorization")
    try:
        ServerGovernance([loaded]).authorize(server_id, scope, connection_ref=connection_ref)
    except PermissionError as exc:
        raise ToolError(str(exc)) from exc
    payload = {
        "server_id": server_id,
        "scope": scope,
        "task_id": task_id,
        "trace_id": trace_id,
    }
    approval = approvals.check_approval_gate(
        "horizon2_connector_authorize", payload, requested_by=identity, force=True
    )
    try:
        server = ServerGovernance([_load(server_id)]).authorize(
            server_id, scope, connection_ref=connection_ref
        )
    except (PermissionError, ToolError) as exc:
        approvals.restore_consumed_approval(approval["approval_id"])
        raise ToolError(str(exc)) from exc
    return {"server": _server_dict(server), "approval_id": approval["approval_id"]}


def horizon2_package_list() -> dict:
    """List packaged connectors and their default scopes. Does not register or dogfood.

    Every registered ``ConnectorManifest`` (task #1204) appears, including
    manifest-only connectors with empty ``scopes``. Such entries also carry
    ``manifest`` (name/version/capabilities/required_secrets/entry_points/
    default_enabled and any non-default registration policy) and ``secrets``
    (present/missing/satisfied — secret NAMES only, never values). Catalog
    packages without a manifest omit both keys; this is additive and never
    mutates a package's ``scopes`` entry.
    """
    packages = []
    manifests = connectors.all_manifests()
    secret_env, _ = service_probes.connector_secret_environment()
    for name in sorted(set(CONNECTOR_PACKAGES) | set(manifests)):
        scopes = CONNECTOR_PACKAGES.get(name, frozenset())
        entry: dict[str, Any] = {"package": name, "scopes": sorted(scopes)}
        manifest = manifests.get(name)
        if manifest is not None:
            entry["manifest"] = manifest.to_dict()
            entry["secrets"] = connectors.secret_status(manifest, env=secret_env)
        packages.append(entry)
    return {"packages": packages, "count": len(packages)}


def horizon2_package_register(
    package: str,
    provenance: str,
    review_ref: str,
    server_id: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    """Register one packaged connector via governed_servers. Approval-gated; not live dogfood."""
    identity = _identity()
    try:
        server = package_connector(
            package,
            provenance=provenance,
            review_ref=review_ref,
            reviewed_by=identity,
            server_id=server_id,
            scopes=scopes,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return horizon2_server_register(
        server.server_id,
        server.connector,
        server.provenance,
        sorted(server.scopes),
        server.review_ref,
    )
