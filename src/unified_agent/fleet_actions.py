"""Approval-gated fleet host control actions (task #799).

DoD minimum: restart an allowlisted service unit on a *registered* host that is
also the hub's local host. Remote hosts and destructive ops are denied by
default until a remote execution plane exists. Every mutating call goes through
``check_prod_action_gate`` (#773) so it is scoped, attributed, and one-shot
approval-gated.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, host_registry, safety, storage
from .monitor import SERVICE_CHECK_TIMEOUT_S, _service_probe, launchd_label_for_unit

# Live action. Stubs stay listed so the UI/API contract is stable for #798 bind.
ACTION_RESTART_SERVICE = "restart_service"
ACTION_REAP_EXECUTOR = "reap_executor"
ACTION_RELEASE_WORKTREE = "release_worktree"
ACTION_RELOAD_REGISTRATION = "reload_registration"

LIVE_ACTIONS = frozenset({ACTION_RESTART_SERVICE})
STUB_ACTIONS = frozenset(
    {
        ACTION_REAP_EXECUTOR,
        ACTION_RELEASE_WORKTREE,
        ACTION_RELOAD_REGISTRATION,
    }
)
ALL_ACTIONS = LIVE_ACTIONS | STUB_ACTIONS

# Restart-safe units only — timers and ad-hoc names stay out.
ALLOWED_RESTART_UNITS = frozenset(
    {
        "unified-agent.service",
        "unified-agent",
        "unified-agent-executor.service",
        "unified-agent-executor",
        "unified-agent-zulip-worker.service",
        "unified-agent-zulip-worker",
        "com.unified-agent.hub",
        "com.unified-agent.executor-agy",
        "com.unified-agent.zulip-worker",
    }
)

_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOOL_NAME = "fleet_host_action"


def normalize_unit(unit: str) -> str:
    raw = (unit or "").strip()
    if not raw or not _UNIT_RE.match(raw):
        raise ToolError("unit must be a non-empty systemd/launchd unit name")
    if raw.endswith(".service") or raw.startswith("com."):
        return raw
    if raw in ALLOWED_RESTART_UNITS or f"{raw}.service" in ALLOWED_RESTART_UNITS:
        return raw if raw in ALLOWED_RESTART_UNITS else f"{raw}.service"
    return raw


def _registered_hosts() -> set[str]:
    try:
        payload = host_registry.fleet_payload()
    except Exception:
        return {config.current_source_host()}
    hosts = {str(row.get("host")) for row in payload.get("hosts") or [] if row.get("host")}
    local = payload.get("local_host") or config.current_source_host()
    hosts.add(str(local))
    return hosts


def _assert_registered_host(host: str) -> str:
    cleaned = (host or "").strip()
    if not cleaned or not _HOST_RE.match(cleaned):
        raise ToolError("host must be a non-empty hostname")
    if cleaned not in _registered_hosts():
        raise ToolError(
            f"host {cleaned!r} is not a registered fleet host; "
            "refuse control actions against unknown hosts"
        )
    return cleaned


def _restart_command(unit: str) -> list[str]:
    if sys.platform == "darwin":
        label = launchd_label_for_unit(unit) or unit
        uid = os.getuid()
        return ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
    normalized = unit if unit.endswith(".service") else f"{unit}.service"
    return ["systemctl", "--user", "restart", normalized]


def _probe_unit(unit: str) -> dict[str, str] | None:
    return _service_probe(unit, SERVICE_CHECK_TIMEOUT_S)


def _execute_restart(unit: str) -> dict[str, Any]:
    cmd = _restart_command(unit)
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(f"service restart failed to start: {exc}") from exc
    after = _probe_unit(unit)
    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[:500],
        "stderr": (proc.stderr or "")[:500],
        "state_after": after,
    }
    if proc.returncode != 0:
        raise ToolError(
            f"service restart exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or 'no output')[:200]}"
        )
    return result


def _audit(
    *,
    actor: str,
    action: str,
    host: str,
    unit: str | None,
    status: str,
    detail: dict[str, Any] | None = None,
) -> str:
    payload = {
        "actor": actor,
        "action": action,
        "host": host,
        "unit": unit,
        "status": status,
        "detail": detail or {},
        "timestamp": storage.now(),
    }
    ulid = events.record_event(
        "agent.fleet.control",
        payload=payload,
        originator=actor,
    )
    try:
        storage.record_agent_event(
            "fleet",
            ref_id=ulid,
            agent=actor,
            summary=f"{action} {unit or ''} on {host}: {status}".strip(),
        )
    except Exception:
        pass
    return ulid


def fleet_host_action(
    action: str,
    host: str,
    unit: str | None = None,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Run one fleet control action under the prod-action + approval gate."""
    action_key = (action or "").strip()
    if action_key not in ALL_ACTIONS:
        raise ToolError(
            f"action {action!r} is not allowed; choose from: {', '.join(sorted(ALL_ACTIONS))}"
        )
    if action_key in STUB_ACTIONS:
        raise ToolError(
            f"action {action_key!r} is denied-by-default in this increment "
            "(task #799 DoD is restart_service only); file a follow-up to enable it"
        )

    identity = actor or config.verified_agent_identity()
    if not identity:
        raise ToolError("authenticated identity required for fleet control actions")

    target_host = _assert_registered_host(host)
    local_host = config.current_source_host()
    if target_host != local_host:
        raise ToolError(
            f"remote host control is not available yet (requested {target_host!r}, "
            f"local hub is {local_host!r}); restart_service only runs on the hub host"
        )

    unit_name = normalize_unit(unit or "unified-agent-executor.service")
    candidates = {unit_name, f"{unit_name}.service", unit_name.removesuffix(".service")}
    if not (candidates & ALLOWED_RESTART_UNITS):
        raise ToolError(
            f"unit {unit_name!r} is not on the restart allowlist; "
            f"allowed: {', '.join(sorted(u for u in ALLOWED_RESTART_UNITS if u.endswith('.service') or u.startswith('com.')))}"
        )

    gate_payload = {
        "action": action_key,
        "host": target_host,
        "unit": unit_name,
    }
    token = config.agent_identity_var.set(identity)
    try:
        # Prod-action policy (#773): scope + revocation + forced approval gate.
        safety.check_prod_action_gate(TOOL_NAME, gate_payload)
        before = _probe_unit(unit_name)
        executed = _execute_restart(unit_name)
        audit_id = _audit(
            actor=identity,
            action=action_key,
            host=target_host,
            unit=unit_name,
            status="ok",
            detail={"before": before, "result": executed},
        )
    except ToolError:
        raise
    except Exception as exc:
        _audit(
            actor=identity,
            action=action_key,
            host=target_host,
            unit=unit_name,
            status="error",
            detail={"error": str(exc)[:200]},
        )
        raise ToolError(f"fleet action failed: {exc}") from exc
    finally:
        config.agent_identity_var.reset(token)

    return {
        "status": "ok",
        "action": action_key,
        "host": target_host,
        "unit": unit_name,
        "local_host": local_host,
        "actor": identity,
        "state_before": before,
        "state_after": executed.get("state_after"),
        "audit_event_id": audit_id,
        "timestamp": storage.now(),
    }
