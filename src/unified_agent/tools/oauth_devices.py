"""Signed-in device sessions: list and revoke hub OAuth refresh tokens (task #1321, J2).

A device signed in with ``unified-agent login`` (RFC 8628) or an MCP client that
completed the authorization-code flow holds a refresh token in the hub's durable
OAuth store. This tool is the operator surface over that store:

* ``action="list"`` — read-only; every live session with its agent, client,
  auth method, and tailnet device binding. Never returns a token value.
* ``action="revoke"`` — deletes the refresh tokens for one ``device`` (tailnet
  node name or id) or one ``session_id``, and drops the matching live access
  tokens. A prod action (``safety.PROD_ACTIONS``): scoped, attributed, and
  one-shot approval-gated, like ``fleet_host_action``.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, identity_core, oauth_store, safety

TOOL_NAME = "oauth_device_manage"
_ACTIONS = ("list", "revoke")


def _sessions_by_device(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group sessions by device, the same way revoke scopes a device.

    A node id is the device identity. A name-only session (no id) joins the
    group of the single node id carrying that name; when no id or several ids
    carry it, it stays in its own name-keyed group, so one Revoke never reaches
    a second node that happens to share the name.
    """
    ids_by_name: dict[str, set[str]] = {}
    for row in sessions:
        if row.get("device_node_id") and row.get("device_node"):
            ids_by_name.setdefault(row["device_node"], set()).add(row["device_node_id"])
    groups: dict[str, dict[str, Any]] = {}
    for row in sessions:
        node_id = row.get("device_node_id") or None
        name = row.get("device_node") or None
        if not node_id and name and len(ids_by_name.get(name, ())) == 1:
            node_id = next(iter(ids_by_name[name]))
        key = f"id:{node_id}" if node_id else (f"name:{name}" if name else "")
        group = groups.setdefault(
            key,
            {
                "device_node": name,
                "device_node_id": node_id,
                "bound": bool(key),
                "sessions": [],
            },
        )
        if name and not group["device_node"]:
            group["device_node"] = name
        group["sessions"].append(row)
    return sorted(groups.values(), key=lambda g: (not g["bound"], str(g["device_node"] or "")))


def list_device_sessions() -> dict[str, Any]:
    """Live OAuth sessions grouped by device (shared by the tool and the Web UI)."""
    sessions = oauth_store.list_sessions()
    return {
        "devices": _sessions_by_device(sessions),
        "session_count": len(sessions),
        "oauth_enabled": config.mcp_oauth_enabled(),
    }


def revoke_device_sessions(
    *, device: str | None, session_id: str | None, actor: str | None
) -> dict[str, Any]:
    """Approval-gated revoke (shared by the tool and the Web UI)."""
    device = (device or "").strip() or None
    session_id = (session_id or "").strip().lower() or None
    if bool(device) == bool(session_id):
        raise ToolError("revoke needs exactly one of device or session_id")
    identity = actor or config.verified_agent_identity()
    if not identity:
        raise ToolError("authenticated identity required to revoke device sessions")
    gate_payload = {"device": device, "session_id": session_id}
    token = config.agent_identity_var.set(identity)
    try:
        safety.check_prod_action_gate(TOOL_NAME, gate_payload)
        provider = identity_core.oauth_provider()
        try:
            # The device's node ids and names, read before its rows go: the
            # session-credential cascade below matches on them (task #1744).
            device_scope = oauth_store.device_scope(device) if device else None
            if provider is not None:
                revoked = provider.revoke_sessions(
                    device=device, session_id=session_id, actor=identity
                )
            else:
                # OAuth off in this process: the durable rows are still revocable.
                revoked = [
                    {k: v for k, v in row.items() if k != "token_sha256"}
                    for row in oauth_store.revoke_sessions(device=device, session_id=session_id)
                ]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        # Session-scoped MCP credentials go with the sign-in (task #1744), for a
        # device revoke and a single-session revoke alike. The OAuth revoke
        # above is already committed and stands either way. A failure here is
        # reported, never swallowed: those credentials still stop working on
        # their next request, because every request re-checks that the device
        # sign-in family they were minted under is live, but their rows are not
        # marked revoked and no .revoked event is written until they are revoked
        # by node (POST /api/action/session-credential-revoke).
        try:
            from .. import session_credentials

            devices: set[str] = set()
            if device_scope is not None:
                devices |= device_scope[0] | device_scope[1]
            unbound: set[str] = set()
            for row in revoked:
                bound = {row.get("device_node"), row.get("device_node_id")} - {None, ""}
                if bound:
                    devices |= {str(v) for v in bound}
                elif row.get("agent"):
                    unbound.add(str(row["agent"]))
            cascade = (
                session_credentials.revoke_device_cascade(
                    devices=sorted(devices), unbound_agents=sorted(unbound), actor=identity
                )
                if devices or unbound
                else []
            )
            credentials: dict[str, Any] = {
                "session_credentials": "revoked",
                "session_credentials_revoked": len(cascade),
            }
        except Exception as exc:  # noqa: BLE001 - reported to the caller below
            logging.getLogger(__name__).warning(
                "session credential cascade failed after a device sign-in revoke (%s)",
                type(exc).__name__,
            )
            credentials = {
                "session_credentials": "failed",
                "warning": "the sign-in is revoked, but marking its session credentials "
                "revoked failed; they are refused on every request because their sign-in "
                "is gone, but their rows are not revoked. Revoke them node-wide with "
                'POST /api/action/session-credential-revoke {"node": "<node>"}; '
                "retrying this revoke finds no sign-in left to cascade from.",
            }
    finally:
        config.agent_identity_var.reset(token)
    return {
        "status": "ok" if credentials["session_credentials"] != "failed" else "partial",
        "device": device,
        "session_id": session_id,
        "revoked": len(revoked),
        "sessions": revoked,
        "actor": identity,
        **credentials,
    }


def oauth_device_manage(
    action: str = "list",
    device: str | None = None,
    session_id: str | None = None,
) -> dict:
    """List signed-in device sessions or revoke one device's hub OAuth sign-in.

    action: "list" (read-only) or "revoke". For revoke pass exactly one of
    `device` (tailnet node name or node id) or `session_id` (from list).
    Revoke is approval-gated; the first call parks an approval request.
    """
    key = (action or "").strip().lower()
    if key not in _ACTIONS:
        raise ToolError(f"action must be one of: {', '.join(_ACTIONS)}")
    if key == "list":
        return list_device_sessions()
    return revoke_device_sessions(device=device, session_id=session_id, actor=None)
