"""Hub side of `unified-agent node` (task #1327, I1).

A node is a device that connects OUT to the hub over HTTP (tailnet), so the
device never listens on a port. It registers host facts, heartbeats, long-polls
its own dispatch queue, runs claimed work through an existing executor backend,
and posts one receipt per item. This module owns the durable half of that
contract; `node_agent` is the device half, and `webui/routes.py` is the thin
HTTP adapter between them.

Identity: a node name is bound to the agent identity that first registered it
(the device token's approved label). Every later call for that node must come
from the same identity, so one signed-in device cannot drain another's queue.

Accuracy contract for the Devices pane: a heartbeat writes ``host_reports`` with
``executor.reported: true`` and ``worktrees.reported: true`` because the device
itself measured them — never inferred from an MCP label prefix.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from . import (
    acp_matrix,
    agent_lifecycle,
    config,
    edition,
    events,
    node_claim_ops,
    node_followups,
    session_credentials,
    session_settings,
    storage,
    table_schemas,
    task_holds,
    trust_presets,
)
from .node_registry import (  # noqa: F401 - the node registry; re-exported here
    CODE_SHA_FEATURE,
    CWD_REALPATH_FEATURE,
    LEASE_GRACE_S,
    LIVE_SESSIONS_FEATURE,
    NODE_LIVE_WINDOW_S,
    RETIRED_CODE,
    WAKE_FEATURE,
    NodeError,
    _check_binding,
    _load,
    _owned_node,
    _row,
    bounded_code_sha,
    bounded_config_state,
    bounded_native_sessions,
    bounded_vault_sync_state,
    heartbeat_is_live,
    list_nodes,
    node_live_beat,
    retired_message,
    root_paths,
    validate_node_name,
)
from .telemetry.redaction import redact_sensitive_content

_log = logging.getLogger(__name__)

CHANNEL_NODE = "agent.node"
NODE_DISPATCH_TOOL = "node_dispatch"
# Approval tool name for agent→client permission prompts relayed from a
# node's ACP backend (task #1328, I2). Always gated: a permission prompt IS
# the approval request — nothing about it is optional.
ACP_PERMISSION_TOOL = "acp_permission"

ITEM_STATUSES = frozenset({"queued", "claimed", "finished", "failed", "killed", "cancelled"})
TERMINAL_ITEM_STATUSES = frozenset({"finished", "failed", "killed", "cancelled"})
RECEIPT_STATUSES = frozenset({"finished", "failed", "killed"})
# Native backends that continue a session through their CLI's own flag (#1741):
# ``claude --resume <id>`` and ``codex exec resume <id>``.
NATIVE_RESUME_BACKENDS = frozenset({"claude", "codex"})
RESUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

# Backends a node may be asked to run. The device enforces its own allowlist too
# (UA_NODE_BACKENDS); this is the hub's vocabulary, matching executor.backends
# plus every `acp:<harness>` name the launch matrix knows (task #1328, I2).
NODE_BACKENDS = frozenset(
    {"codex", "agy", "pi", "claude", "cursor", "hermes", "copilot", "devin", "agy-interactive"}
    | {f"acp:{name}" for name in acp_matrix.ACP_HARNESSES}
    # The built-in, no-model device acceptance job (task #2112).
    | {"acceptance"}
    # A supervised argv command (task #2413); runs only on a node that allowlists it.
    | {"command"}
)

DEFAULT_TIMEOUT_S = 3600
MAX_TIMEOUT_S = 6 * 3600
MAX_PROMPT_CHARS = 200_000
MAX_FACTS_BYTES = 64_000
MAX_RECEIPT_TEXT = 8_000
MAX_PROGRESS_CHARS = 2_000
# /api/node/events bounds (task #1328): a node streams one normalized ACP event
# at a time but may batch; both caps keep a runaway or hostile stream bounded.
MAX_NODE_EVENTS_PER_CALL = 200
MAX_NODE_EVENT_BYTES = 32_768
# /api/node/permission bounds: the option list a harness offered, verbatim.
MAX_PERMISSION_OPTIONS = 32
MAX_PERMISSION_BYTES = 32_768
# agent_sessions statuses meaning "still live" — mirrors registry's private
# tuple (crew.py keeps its own copy the same way).
_OPEN_SESSION_STATUSES = frozenset({"starting", "running", "waiting"})

_ITEM_RE = re.compile(r"^nd-[0-9a-f]{16}$")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _now_dt(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bounded_facts(facts: Any) -> dict[str, Any]:
    if facts is None:
        return {}
    if not isinstance(facts, dict):
        raise NodeError("facts must be a JSON object")
    encoded = json.dumps(facts, default=str)
    if len(encoded.encode("utf-8")) > MAX_FACTS_BYTES:
        raise NodeError(f"facts exceed {MAX_FACTS_BYTES} bytes")
    return json.loads(encoded)


_HEARTBEAT_KEYS = frozenset(
    {"surfaces", "harness_config", "vault_sync", "native_sessions", "code_sha"}
)


def _merge_surface_facts(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Fit an advisory surface refresh without rejecting a lease-renewing heartbeat."""
    base = {
        key: value
        for key, value in existing.items()
        if key != "surfaces"
        and not (
            key in ("harness_config", "vault_sync", "native_sessions", "code_sha") and key in update
        )
    }
    if "native_sessions" in update:
        # Discovery runs on its own interval (#1741); a heartbeat without the
        # key keeps the last list. Re-checked against the node's own roots.
        base["native_sessions"] = bounded_native_sessions(
            update.get("native_sessions"), root_paths(existing)
        )
    if "harness_config" in update:
        state = bounded_config_state(update.get("harness_config"))
        if state is not None:
            base["harness_config"] = state
    if "vault_sync" in update:
        sync_state = bounded_vault_sync_state(update.get("vault_sync"))
        if sync_state is not None:
            base["vault_sync"] = sync_state
    if "code_sha" in update:
        # A reported null or malformed value clears the stored SHA: the node
        # no longer runs from a readable checkout (#1946).
        code_sha = bounded_code_sha(update.get("code_sha"))
        if code_sha is not None:
            base["code_sha"] = code_sha
    reported = update.get("surfaces")
    surfaces: dict[str, list[str]] = {}
    if isinstance(reported, dict):
        for field in ("hermes_profiles", "kanban_assignees"):
            names = reported.get(field)
            surfaces[field] = (
                [name for name in names if isinstance(name, str)][:50]
                if isinstance(names, list)
                else []
            )
    merged = {**base, "surfaces": surfaces}
    while len(json.dumps(merged, default=str).encode("utf-8")) > MAX_FACTS_BYTES and merged.get(
        "native_sessions"
    ):
        merged["native_sessions"] = merged["native_sessions"][:-1]
    while len(json.dumps(merged, default=str).encode("utf-8")) > MAX_FACTS_BYTES:
        nonempty = [names for names in surfaces.values() if names]
        if not nonempty:
            return base
        max(nonempty, key=len).pop()
    return merged


def _record(kind: str, payload: dict[str, Any], originator: str) -> str | None:
    """Best-effort ledger entry. The queue row is the source of truth."""
    try:
        return events.record_event(CHANNEL_NODE, {"kind": kind, **payload}, originator=originator)
    except Exception:  # noqa: BLE001 - a ledger hiccup must not fail a node call
        return None


# --- registration and heartbeat -------------------------------------------------


def _executor_report(node_row: dict[str, Any], facts: dict[str, Any], as_of: str, claim: Any):
    backends = facts.get("backends") or []
    return {
        "reported": True,
        "as_of": as_of,
        # The node proves liveness by the authenticated heartbeat itself.
        "unit_status": "LIVE",
        "detail": "ua-node heartbeat; backends: " + (", ".join(backends) or "none"),
        "agent": node_row["agent"],
        "claim": claim,
    }


def _worktrees_report(facts: dict[str, Any], as_of: str) -> dict[str, Any]:
    roots = facts.get("worktree_roots")
    if not isinstance(roots, list):
        return {"reported": False, "as_of": None}
    total = sum(int(r.get("worktrees") or 0) for r in roots if isinstance(r, dict))
    return {
        "reported": True,
        "as_of": as_of,
        "total": total,
        # The Devices pane reads `count`; keep both until the census shape is unified.
        "count": total,
        "roots": [r.get("path") for r in roots if isinstance(r, dict)],
    }


def _write_host_report(
    conn: sqlite3.Connection,
    node_row: dict[str, Any],
    facts: dict[str, Any],
    *,
    as_of: str,
    claim: Any,
    refresh_worktrees: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO host_reports
          (host, last_seen, last_seen_via, version, executor_json, worktrees_json, updated_at)
        VALUES (?, ?, 'ua_node', ?, ?, ?, ?)
        ON CONFLICT(host) DO UPDATE SET
          last_seen = excluded.last_seen,
          last_seen_via = excluded.last_seen_via,
          version = COALESCE(excluded.version, host_reports.version),
          executor_json = excluded.executor_json,
          worktrees_json = COALESCE(excluded.worktrees_json, host_reports.worktrees_json),
          updated_at = excluded.updated_at
        """,
        (
            node_row["node"],
            as_of,
            node_row.get("version"),
            json.dumps(_executor_report(node_row, facts, as_of, claim)),
            json.dumps(_worktrees_report(facts, as_of)) if refresh_worktrees else None,
            as_of,
        ),
    )


def register(
    node: Any,
    *,
    identity: str,
    facts: Any = None,
    version: str | None = None,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Register (or re-register) a node under ``identity``. Idempotent."""
    name = validate_node_name(node)
    if config._node_hosted_or_public() and not device_node:
        raise NodeError(
            "hosted registration requires a device-bound sign-in; "
            "rerun unified-agent login on this device",
            status=403,
        )
    clean = _bounded_facts(facts)
    as_of = _iso(_now_dt(now))
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (name,)).fetchone())
        if existing is not None:
            # A bound sign-in may migrate its own legacy unbound registration.
            # It can never replace a different device binding or agent owner.
            binding_row = existing
            if config._node_hosted_or_public() and not existing.get("device_node"):
                binding_row = {**existing, "device_node": device_node}
            _check_binding(binding_row, identity, device_node)
        conn.execute(
            """
            INSERT INTO nodes
              (node, agent, device_node, facts_json, version, registered_at,
               last_heartbeat, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node) DO UPDATE SET
              device_node = COALESCE(excluded.device_node, nodes.device_node),
              facts_json = excluded.facts_json,
              version = excluded.version,
              last_heartbeat = excluded.last_heartbeat,
              updated_at = excluded.updated_at
            """,
            (name, identity, device_node, json.dumps(clean), version, as_of, as_of, as_of),
        )
        node_row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (name,)).fetchone())
        _write_host_report(conn, node_row, clean, as_of=as_of, claim=None)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _record(
        "register",
        {"node": name, "agent": identity, "backends": clean.get("backends") or []},
        identity,
    )
    return {
        "node": name,
        "agent": identity,
        "registered": True,
        "first_registration": existing is None,
        "as_of": as_of,
        # Protocol features this hub build serves to nodes (task #1744). Code
        # support only: whether a feature is switched on is checked per call.
        "hub_features": [
            session_credentials.FEATURE,
            CODE_SHA_FEATURE,
            WAKE_FEATURE,
            LIVE_SESSIONS_FEATURE,
            node_claim_ops.FEATURE,
        ],
    }


def _cancel_requested(conn: sqlite3.Connection, item: dict[str, Any]) -> bool:
    if item.get("cancel_requested"):
        return True
    run_id = item.get("run_id")
    if not run_id:
        return False
    # A governed `dispatch_cancel` on the linked run reaches the node too.
    try:
        run = conn.execute(
            "SELECT termination_outcome, state FROM dispatch_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return run is not None and (
        run["termination_outcome"] is not None or run["state"] == "cancelled"
    )


def heartbeat(
    node: Any,
    *,
    identity: str,
    running: Any = None,
    facts: Any = None,
    progress: Any = None,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh liveness, renew the running item's lease, and relay cancellation.

    ``running`` is the item id the node is executing (or None). ``progress`` is
    an optional short note recorded on the event ledger, which is how a node
    streams progress without a second route.
    """
    name = validate_node_name(node)
    ref = _now_dt(now)
    as_of = _iso(ref)
    cancel: list[str] = []
    revoked: list[str] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        node_row = _owned_node(conn, name, identity, device_node)
        # Heartbeats refresh advisory keys only (surfaces, and since #1736 the
        # harness-config state); a full fact set replaces the row.
        surface_only = (
            isinstance(facts, dict) and "surfaces" in facts and set(facts) <= _HEARTBEAT_KEYS
        )
        if facts is not None:
            update = _bounded_facts(facts)
            clean = (
                _merge_surface_facts(_load(node_row.get("facts_json")) or {}, update)
                if surface_only
                else update
            )
            conn.execute("UPDATE nodes SET facts_json=? WHERE node=?", (json.dumps(clean), name))
        else:
            clean = _load(node_row.get("facts_json")) or {}
        conn.execute(
            "UPDATE nodes SET last_heartbeat=?, updated_at=? WHERE node=?", (as_of, as_of, name)
        )
        claim = None
        if running:
            item = _row(
                conn.execute(
                    "SELECT * FROM node_dispatch WHERE id=? AND node=?", (str(running), name)
                ).fetchone()
            )
            if item is not None and item["status"] == "claimed":
                lease = ref + timedelta(seconds=int(item["timeout_s"]) + LEASE_GRACE_S)
                conn.execute(
                    "UPDATE node_dispatch SET lease_expires_at=?, updated_at=? WHERE id=?",
                    (_iso(lease), as_of, item["id"]),
                )
                # The heartbeat proves the node is alive; the linked run's
                # lease moves in step so reclaim never calls live work stale.
                _renew_linked_run(
                    conn,
                    item.get("run_id"),
                    lease=lease,
                    target_state=None,
                    expected_states=("claimed", "launched", "running"),
                )
                try:
                    _require_native_item(conn, item)
                except NodeError:
                    # Preserve cancellation delivery even for malformed policy.
                    conn.execute(
                        "UPDATE node_dispatch SET cancel_requested=1 WHERE id=?",
                        (item["id"],),
                    )
                    item["cancel_requested"] = 1
                if _cancel_requested(conn, item):
                    cancel.append(item["id"])
                    # A cancel ends the session's hub access at once (task #1744).
                    revoked = session_credentials.revoke_for_item(
                        conn, item["id"], "cancelled", now=ref
                    )
                else:
                    # The node is alive and running this item: slide the
                    # session credential's idle window, never its cap.
                    session_credentials.slide_idle(conn, item["id"], name, now=ref)
                claim = {
                    "item_id": item["id"],
                    "task_id": item["task_id"],
                    "run_id": item["run_id"],
                    "backend": item["backend"],
                    "claimed_at": item["claimed_at"],
                }
                # Long ACP turns renew the queue lease above but not the
                # mirrored agent_sessions row — keep it alive so the reaper
                # does not mark active work abandoned after 30 minutes.
                _heartbeat_acp_sessions_for_item(conn, item["id"], name)
        _write_host_report(
            conn,
            node_row,
            clean,
            as_of=as_of,
            claim=claim,
            refresh_worktrees=facts is not None and not surface_only,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    session_credentials.emit_revoked(revoked, "cancelled", actor=identity)
    for item_id in cancel:
        _record("cancel_relayed", {"node": name, "item_id": item_id}, identity)
    if progress and running:
        _record(
            "progress",
            {"node": name, "item_id": str(running), "note": str(progress)[:MAX_PROGRESS_CHARS]},
            identity,
        )
    return {"node": name, "as_of": as_of, "cancel": cancel}


# --- queue ----------------------------------------------------------------------


def _validate_item(
    *,
    prompt: Any,
    cwd: Any,
    backend: Any,
    model: Any = None,
    timeout_s: Any = None,
    run_id: Any = None,
    task_id: Any = None,
    hermes_profile: Any = None,
    resume_session_id: Any = None,
    permission_mode: Any = None,
    cwd_realpath: Any = None,
) -> dict[str, Any]:
    """Shape checks for one queue item. Pure: touches no storage.

    ``cwd_realpath`` (task #1770) is the checkout's real path as the node
    measured it when the work was planned. The node refuses to launch when
    ``cwd`` no longer resolves to it.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise NodeError("prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise NodeError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 4096:
        raise NodeError("cwd is required (under 4096 characters)")
    if cwd_realpath is not None and (
        not isinstance(cwd_realpath, str)
        or not cwd_realpath.startswith("/")
        or len(cwd_realpath) > 4096
    ):
        raise NodeError("cwd_realpath must be an absolute path (under 4096 characters)")
    backend_name = str(backend or "").strip().lower()
    if backend_name not in NODE_BACKENDS:
        raise NodeError(f"unknown backend {backend!r}; expected one of {sorted(NODE_BACKENDS)}")
    if backend_name == "acp:hermes":
        try:
            acp_matrix.validate_hermes_profile(hermes_profile)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
    elif hermes_profile is not None:
        raise NodeError("hermes_profile is only supported by acp:hermes")
    acp = acp_matrix.acp_backend_name(backend_name) is not None
    if permission_mode is not None and (not acp or permission_mode not in session_settings.MODES):
        raise NodeError(
            "permission_mode requires ACP and must be supervised, auto-accept-edits, or full-access"
        )
    effective_mode = (permission_mode or "supervised") if acp else None
    if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int)):
        raise NodeError("task_id must be an integer")
    for field, value in (("model", model), ("run_id", run_id)):
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise NodeError(f"{field} must be a string under 200 characters")
    if isinstance(timeout_s, bool):
        raise NodeError("timeout_s must be an integer")
    try:
        timeout = int(timeout_s or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError) as exc:
        raise NodeError("timeout_s must be an integer") from exc
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise NodeError(f"timeout_s must be between 1 and {MAX_TIMEOUT_S}")
    if resume_session_id is not None:
        if not isinstance(resume_session_id, str) or not RESUME_ID_RE.fullmatch(resume_session_id):
            raise NodeError("resume_session_id is malformed")
        harness = acp_matrix.acp_backend_name(backend_name)
        if harness is None and backend_name not in NATIVE_RESUME_BACKENDS:
            raise NodeError(
                f"backend {backend_name!r} cannot resume a session; "
                f"resume needs acp:<harness> or one of {sorted(NATIVE_RESUME_BACKENDS)}",
                status=409,
            )
    return {
        "prompt": prompt,
        "cwd": cwd.strip(),
        "cwd_realpath": cwd_realpath,
        "backend": backend_name,
        "hermes_profile": hermes_profile,
        "model": model,
        "permission_mode": effective_mode,
        "timeout_s": timeout,
        "run_id": run_id,
        "task_id": task_id,
        "resume_session_id": resume_session_id,
    }


def _require_settings_support(node_row: dict[str, Any], item: dict[str, Any]) -> None:
    if acp_matrix.acp_backend_name(item.get("backend")) is None:
        return
    features = (_load(node_row.get("facts_json")) or {}).get("node_features") or []
    if session_settings.FEATURE not in features:
        raise NodeError(
            "this ua-node predates session settings; upgrade it before launching ACP", status=409
        )


def _require_realpath_support(node_row: dict[str, Any], item: dict[str, Any]) -> None:
    if item.get("cwd_realpath") is None:
        return
    features = (_load(node_row.get("facts_json")) or {}).get("node_features")
    if not (isinstance(features, list) and CWD_REALPATH_FEATURE in features):
        raise NodeError(
            f"node {node_row['node']!r} cannot enforce the checkout realpath; upgrade ua-node",
            status=409,
        )


def _require_resume_support(node_row: dict[str, Any], item: dict[str, Any]) -> None:
    """A resume goes only to a node that honours it (#1741).

    A ua-node that predates resume ignores ``resume_session_id`` and would
    open a fresh session in its place, so the hub refuses instead.
    """
    if item.get("resume_session_id") is None:
        return
    features = (_load(node_row.get("facts_json")) or {}).get("node_features")
    if not (isinstance(features, list) and acp_matrix.RESUME_FEATURE in features):
        raise NodeError(
            f"node {node_row['node']!r} runs a ua-node without session resume; "
            "upgrade it, or Start again for a fresh session",
            status=409,
        )


def _accepting_node(conn: sqlite3.Connection, name: str, backend: str) -> dict[str, Any]:
    node_row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (name,)).fetchone())
    if node_row is None:
        raise NodeError(f"node {name!r} is not registered", status=404)
    if node_row.get("retired_at"):
        raise NodeError(retired_message(name), status=409, code=RETIRED_CODE)
    advertised = (_load(node_row.get("facts_json")) or {}).get("backends")
    if isinstance(advertised, list) and backend not in advertised:
        raise NodeError(
            f"node {name!r} does not advertise backend {backend!r} "
            f"(advertised: {', '.join(advertised) or 'none'})",
            status=409,
        )
    return node_row


def _require_acceptance(conn: sqlite3.Connection, node_row: dict[str, Any], backend: str) -> None:
    """UA_NODE_REQUIRE_ACCEPTANCE=1: real work waits for device acceptance (task #2112)."""
    from . import node_acceptance

    node_acceptance.require_accepted(conn, node_row, backend)


def hermes_profiles(node: str, surfaces: dict[str, Any]) -> list[dict[str, Any]]:
    """Name-only discovery plus existing canonical surface mappings. No identity guess."""
    from . import host_registry

    rows = host_registry.map_surfaces(
        node,
        surfaces if isinstance(surfaces, dict) else {},
        mapping=config.surface_map(),
        labels=config.agent_labels(),
    )
    out = []
    for row in rows:
        if row["kind"] != "hermes":
            continue
        reason = None
        try:
            acp_matrix.validate_hermes_profile(row["name"])
        except ValueError:
            reason = "unsafe_profile_name"
        if not reason and (
            not row["agent"]
            or not (
                row["agent"] in config.agent_labels() or config.is_fleet_roster_label(row["agent"])
            )
        ):
            reason = "unmapped_profile"
        out.append({**row, "launchable": reason is None, "reason": reason})
    return out


def _bind_hermes_profile(node_row: dict[str, Any], item: dict[str, Any]) -> str | None:
    if item.get("backend") != "acp:hermes":
        return None
    profile = item.get("hermes_profile")
    try:
        acp_matrix.validate_hermes_profile(profile)
    except ValueError as exc:
        raise NodeError(str(exc)) from exc
    facts = _load(node_row.get("facts_json")) or {}
    row = next(
        (
            r
            for r in hermes_profiles(node_row["node"], facts.get("surfaces") or {})
            if r["name"] == profile
        ),
        None,
    )
    if not row or not row["launchable"]:
        raise NodeError("Hermes profile is not discovered and canonically mapped", status=409)
    if item.get("profile_agent") is not None and item["profile_agent"] != row["agent"]:
        raise NodeError("Hermes profile mapping changed since dispatch", status=409)
    return str(row["agent"])


def _running_identity_refusal(
    conn: sqlite3.Connection, *labels: Any, site: str
) -> NodeError | None:
    """The 403 for the first of ``labels`` that is paused, terminated or retired, or None.

    Tasks #2305, #2315. The refusal carries the lifecycle code (``agent_paused``
    and so on) and is recorded as a ``lifecycle.refused`` event. Resuming the
    agent lifts it.
    """
    for label in dict.fromkeys(lab for lab in labels if isinstance(lab, str) and lab):
        refusal = agent_lifecycle.lifecycle_refusal(label, site=site, conn=conn)
        if refusal is not None:
            return NodeError(refusal["message"], status=403, code=refusal["code"])
    return None


def _require_running_identity(conn: sqlite3.Connection, *labels: Any, site: str) -> None:
    """Raise :func:`_running_identity_refusal`'s 403 when there is one."""
    refused = _running_identity_refusal(conn, *labels, site=site)
    if refused is not None:
        raise refused


def _require_native_item(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """Use stored hub authority, never a node's optional local policy copy."""
    profile_agent = None
    if item.get("backend") == "acp:hermes":
        node_row = _accepting_node(conn, item["node"], item["backend"])
        profile_agent = _bind_hermes_profile(node_row, item)
        if profile_agent in config.revoked_agents():
            raise NodeError("Hermes profile agent is revoked", status=403)
    originator = item.get("created_by")
    target = item.get("agent")
    if item.get("run_id"):
        run = conn.execute(
            "SELECT r.agent_label, d.from_agent FROM dispatch_runs r "
            "LEFT JOIN delegations d ON d.id=r.delegation_id WHERE r.run_id=?",
            (item["run_id"],),
        ).fetchone()
        if run is not None:
            target = run["agent_label"]
            originator = run["from_agent"]
    # A paused, terminated or retired identity runs nothing, on any backend
    # (tasks #2305, #2315): the device owner, the dispatched target and the
    # profile the session runs as are each checked. Admission, claim, heartbeat
    # and bound-attempt checks all pass here. A heartbeat that fails this turns
    # the running item's cancel on, the same flag the pause stop handler sets.
    _require_running_identity(
        conn, item.get("agent"), target, profile_agent, site="node_queue.native_item"
    )
    try:
        # The device owner, dispatched target and delegator are independent
        # ceilings: an unbound node cannot launder a bound target's launch.
        trust_presets.require_native_allowed(item.get("agent"), originator=originator)
        if profile_agent and profile_agent != item.get("agent"):
            trust_presets.require_native_allowed(profile_agent, originator=originator)
        if target != item.get("agent"):
            trust_presets.require_native_allowed(target, originator=originator)
        creator = item.get("created_by")
        if creator != originator and creator != "dispatch-scheduler":
            # A manual item may reference an existing run. That link cannot
            # discard its independently authenticated creator's restriction.
            trust_presets.require_native_allowed(item.get("agent"), originator=creator)
            if profile_agent and profile_agent != item.get("agent"):
                trust_presets.require_native_allowed(profile_agent, originator=creator)
    except Exception as exc:
        raise NodeError(f"native item refused by hub policy: {exc}", status=403) from exc


def _permission_block(conn: sqlite3.Connection, item: dict[str, Any] | None) -> str | None:
    """Why a mid-turn permission prompt for ``item`` must be denied, or None.

    The turn must still be claimed and not cancel-requested, directly or
    through its linked run (task #2315 review): a cancel is relayed on the
    next heartbeat, so the session may still be asking in between.
    """
    if item is None:
        return "item is no longer queued for this node"
    if item["status"] != "claimed":
        return f"item is {item['status']}, not claimed"
    if _cancel_requested(conn, item):
        return "item cancellation was requested"
    return None


def _require_not_held(
    conn: sqlite3.Connection, task_id: Any, pending: list[dict[str, Any] | None] | None = None
) -> None:
    """A held task (task #1747) takes no node dispatch, the same check task_claim makes."""
    if task_id is None:
        return
    held = task_holds.claim_block(conn, int(task_id), pending=pending)
    if held:
        raise NodeError(held, status=409)


def _hold_reason(conn: sqlite3.Connection, row: dict[str, Any]) -> str | None:
    """Why this item's task is held right now, or None (task #2493).

    ``_claim`` skips a held item so it stays queued: the hold clears and the work
    resumes, so failing the item would destroy it. ``enqueue`` and
    ``request_dispatch`` refuse instead, through ``_require_not_held``, because
    there the caller can be told before the work exists.
    """
    task_id = row.get("task_id")
    if task_id is None:
        return None
    return task_holds.claim_block(conn, int(task_id))


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Side tables: planned cwd realpath (#1770), waits (#2110), claim operations (#2136),
    supervised commands (#2413)."""
    from . import node_command

    table_schemas.node_dispatch_cwd(conn)
    table_schemas.node_dispatch_wait(conn)
    table_schemas.node_claim_ops(conn)
    node_command.ensure_schema(conn)


# How long an item queued for a node that is not live may wait for it (#2110).
NODE_QUEUE_WAIT_DEFAULT_S = 3600
NODE_QUEUE_WAIT_MIN_S = 60
NODE_QUEUE_WAIT_MAX_S = 7 * 24 * 3600
WAIT_EXPIRED_REASON = "waiting_expired_node_offline"
# Wait expiry is the hub's decision, not the act of whoever happened to call
# enqueue or claim when it ran: events and run closures carry this actor.
WAIT_SWEEP_ACTOR = "node-queue-wait-sweep"
# How often the hub's own lifespan task runs ``expire_waiting`` (#2110 review).
NODE_WAIT_SWEEP_DEFAULT_S = 60
# Unsettled expiries retried per sweep; the rest wait for the next one.
_WAIT_RECONCILE_LIMIT = 100


def node_wait_sweep_s() -> int:
    """``UA_NODE_WAIT_SWEEP_S``: seconds between hub wait sweeps; 0 disables.

    Malformed or negative raises, like ``node_queue_wait_s``: the hub refuses
    to start rather than silently never expiring waiting work.
    """
    import os

    raw = (os.environ.get("UA_NODE_WAIT_SWEEP_S") or "").strip()
    if not raw:
        return NODE_WAIT_SWEEP_DEFAULT_S
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"UA_NODE_WAIT_SWEEP_S must be an integer, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"UA_NODE_WAIT_SWEEP_S must be 0 (off) or more seconds, got {value}")
    return value


def node_queue_wait_s() -> int:
    """``UA_NODE_QUEUE_WAIT_S``: the wait deadline for an offline target node.

    Bounded to [60 s, 7 days]. A malformed value raises: a silent default would
    let queued work launch at a time nobody chose.
    """
    import os

    raw = (os.environ.get("UA_NODE_QUEUE_WAIT_S") or "").strip()
    if not raw:
        return NODE_QUEUE_WAIT_DEFAULT_S
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"UA_NODE_QUEUE_WAIT_S must be an integer, got {raw!r}") from None
    if not NODE_QUEUE_WAIT_MIN_S <= value <= NODE_QUEUE_WAIT_MAX_S:
        raise ValueError(
            f"UA_NODE_QUEUE_WAIT_S must be {NODE_QUEUE_WAIT_MIN_S}-{NODE_QUEUE_WAIT_MAX_S} "
            f"seconds, got {value}"
        )
    return value


def node_wait_reason(node_row: dict[str, Any], ref: datetime) -> str | None:
    """Why ``node_row`` cannot take work now, or None when it is live.

    The same liveness rule automatic selection uses (``heartbeat_is_live``), so
    a direct enqueue and ``select_node_for_agent`` never disagree (#2110),
    including over a beat from before the last retire/un-retire (#2196).
    """
    if not node_row.get("last_heartbeat"):
        return "node_never_heartbeat"
    if not heartbeat_is_live(node_live_beat(node_row), ref):
        return "node_offline"
    return None


def _wait_view(wait: dict[str, Any] | None, *, status: str, ref: datetime) -> dict[str, Any] | None:
    """The ``waiting`` block shown for an item, or None when it is not waiting."""
    if not wait:
        return None
    expires = _parse_ts(wait.get("expires_at"))
    return {
        "reason": wait["reason"],
        "last_heartbeat": wait.get("last_heartbeat"),
        "expires_at": wait["expires_at"],
        "since": wait.get("created_at"),
        "active": status == "queued" and (expires is None or expires >= ref),
    }


def _expire_waiting(
    conn: sqlite3.Connection, as_of: str, *, node: str | None = None
) -> list[dict[str, Any]]:
    """End expired or cancelled waits, atomically persisting run-close intent.

    The work never started, so this is a definite outcome, not an unknown one:
    the linked run, if any, is closed failed by the caller after commit. The
    wait row records ``expired_at`` in this same transaction and keeps
    ``settled_at`` empty until that close succeeds, so a failed close (or a
    crash after commit) stays visible to ``_reconcile_unsettled_waits``.
    """
    ensure_schema(conn)
    # Cancellation ends a wait immediately, before claim's generic bulk
    # cancellation makes the item terminal. Also recover terminal cancelled
    # rows left by older hubs without a settlement marker. Linked-run fences
    # remain authoritative when cancellation came through dispatch_cancel.
    cancelled_sql = (
        "(d.status='cancelled' OR d.cancel_requested=1 OR EXISTS ("
        "SELECT 1 FROM dispatch_runs r WHERE r.run_id=d.run_id AND "
        "(r.termination_outcome IS NOT NULL OR r.state IN "
        "('cancelled', 'finished', 'failed', 'blocked', 'denied', 'expired'))))"
    )
    sql = (
        "SELECT d.id, d.run_id, d.node, " + cancelled_sql + " AS waiting_cancelled "
        "FROM node_dispatch d JOIN node_dispatch_wait w ON w.item_id=d.id "
        "WHERE w.expired_at IS NULL AND ("
        "(d.status='queued' AND (w.expires_at < ? OR " + cancelled_sql + ")) "
        "OR d.status='cancelled')"
    )
    params: tuple[Any, ...] = (as_of,)
    if node is not None:
        sql += " AND d.node=?"
        params = (as_of, node)
    expired = [_row(r) for r in conn.execute(sql, params).fetchall()]
    receipt = json.dumps({"status": "failed", "reason": WAIT_EXPIRED_REASON, "hub_generated": True})
    for row in expired:
        # With no linked run there is nothing left to settle.
        conn.execute(
            "UPDATE node_dispatch_wait SET expired_at=?, settled_at=? "
            "WHERE item_id=? AND expired_at IS NULL",
            (as_of, None if row.get("run_id") else as_of, row["id"]),
        )
        if row.get("waiting_cancelled"):
            # An operator cancelled it while it waited: that is the outcome,
            # not an offline-node failure.
            row["outcome"] = "cancelled"
            conn.execute(
                "UPDATE node_dispatch SET status='cancelled', completed_at=?, updated_at=? "
                "WHERE id=? AND status='queued'",
                (as_of, as_of, row["id"]),
            )
            continue
        row["outcome"] = "failed"
        conn.execute(
            "UPDATE node_dispatch SET status='failed', completed_at=?, updated_at=?, "
            "receipt_json=? WHERE id=? AND status='queued'",
            (as_of, as_of, receipt, row["id"]),
        )
    return expired


# ``_close_linked_run`` results after which there is nothing left for an
# expired wait to settle: closed now, already terminal, or refused by the
# item-binding guards because the run is no longer this item's to close
# (rebound to a resumed item, handed back, or gone). A guard race, a state
# race or a database error is retried by the next sweep.
_WAIT_SETTLED_REASONS = frozenset(
    {"already_terminal", "run_rebound", "run_not_live", "unknown_run"}
)


def _settle_wait_run(item_id: str, run_id: str, node: str, *, cancelled: bool) -> bool:
    """Close an expired wait's linked run; mark the wait settled only if it took.

    Idempotent: ``_close_linked_run`` leaves a terminal run alone and refuses a
    run bound to another item, so a retry can never close the wrong run.
    """
    res = _close_linked_run(
        run_id,
        "cancelled" if cancelled else "failed",
        reason="cancel_requested" if cancelled else WAIT_EXPIRED_REASON,
        actor=WAIT_SWEEP_ACTOR,
        evidence_ref=f"node:{node}:{item_id}",
    )
    if not (res.get("ok") or res.get("reason") in _WAIT_SETTLED_REASONS):
        return False
    return _mark_wait_settled(item_id)


def _mark_wait_settled(item_id: str) -> bool:
    """Record that an expired wait needs nothing more. False on a DB error."""
    try:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch_wait SET settled_at=? "
                "WHERE item_id=? AND expired_at IS NOT NULL AND settled_at IS NULL",
                (storage.now(), item_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False  # the next sweep retries; the close is idempotent
    return True


def _settle_expired_waits(expired: list[dict[str, Any]]) -> None:
    """After commit: events, close each linked run, drop orphaned follow-ups.

    Attributed to ``WAIT_SWEEP_ACTOR``, never to the unrelated caller whose
    enqueue or claim happened to run the expiry. A close that fails here is
    left unsettled for ``_reconcile_unsettled_waits``.
    """
    if not expired:
        return
    for row in expired:
        cancelled = row.get("outcome") == "cancelled"
        _record(
            "wait_cancelled" if cancelled else "wait_expired",
            {
                "node": row["node"],
                "item_id": row["id"],
                "reason": "cancel_requested" if cancelled else WAIT_EXPIRED_REASON,
            },
            WAIT_SWEEP_ACTOR,
        )
        if row.get("run_id"):
            _settle_wait_run(row["id"], row["run_id"], row["node"], cancelled=cancelled)
    node_followups.sweep_ended(actor=WAIT_SWEEP_ACTOR)


def _reconcile_unsettled_waits() -> list[str]:
    """Retry the run close of every expired wait not yet settled (bounded).

    Covers a close that returned an error and a process that died between the
    expiry commit and the close. Returns the item ids settled now.
    """
    try:
        conn = storage.connect()
        try:
            ensure_schema(conn)
            rows = [
                _row(r)
                for r in conn.execute(
                    "SELECT d.id, d.run_id, d.node, d.status FROM node_dispatch_wait w "
                    "JOIN node_dispatch d ON d.id = w.item_id "
                    "WHERE w.expired_at IS NOT NULL AND w.settled_at IS NULL "
                    "ORDER BY w.expired_at ASC LIMIT ?",
                    (_WAIT_RECONCILE_LIMIT,),
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    settled: list[str] = []
    for row in rows:
        if not row.get("run_id"):
            done = _mark_wait_settled(row["id"])
        else:
            done = _settle_wait_run(
                row["id"], row["run_id"], row["node"], cancelled=row["status"] == "cancelled"
            )
        if done:
            settled.append(row["id"])
    return settled


def _wait_verdict(conn: sqlite3.Connection, item: dict[str, Any], as_of: str) -> str | None:
    """How run-lease reclamation must treat ``item``'s wait, if it has one.

    ``"wait_unsettled"``: the wait expired but its run close is still pending,
    so only the wait sweep may settle the run. ``"waiting"``: the item is
    still queued under a wait, so the wait deadline, not the run lease,
    decides its outcome. None: no wait applies.
    """
    ensure_schema(conn)
    wait = _row(
        conn.execute(
            "SELECT expires_at, expired_at, settled_at FROM node_dispatch_wait WHERE item_id=?",
            (item["id"],),
        ).fetchone()
    )
    if wait is None:
        return None
    if not wait.get("settled_at") and (wait.get("expired_at") or item["status"] == "cancelled"):
        # Cancellation can precede its durable sweep marker. Even if that
        # sweep failed, ordinary lease expiry must never retry cancelled work.
        return "wait_unsettled"
    if item["status"] == "queued" and not wait.get("expired_at"):
        return "waiting"
    return None


def expire_waiting(*, actor: str, now: datetime | None = None) -> list[str]:
    """Hub-side sweep: settle cancelled waits and work whose deadline passed.

    A node that stays offline never calls ``claim_next``, so its expired waits
    are settled here: the coordinator's lease sweep calls this before its own
    reclamation, and the hub's lifespan runs it every ``UA_NODE_WAIT_SWEEP_S``.
    First it retries the run close of earlier expiries that did not settle,
    then it expires new ones. Returns the newly expired item ids.
    """
    _reconcile_unsettled_waits()
    as_of = _iso(_now_dt(now))
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        expired = _expire_waiting(conn, as_of)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _settle_expired_waits(expired)
    return [row["id"] for row in expired]


def enqueue(
    node: Any,
    *,
    created_by: str,
    expected_profile_agent: str | None = None,
    resume_binding: dict[str, Any] | None = None,
    settings_source: str | None = None,
    launch_key: str | None = None,
    command: dict[str, Any] | None = None,
    _transaction: sqlite3.Connection | None = None,
    _after_commit: list | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Queue one item for ``node``. Ungated — HTTP callers go through
    ``request_dispatch``, which puts the approval gate in front of this.

    ``fields``: prompt, cwd, backend, and optional model, timeout_s, run_id, task_id,
    cwd_realpath.

    ``launch_key`` (task #2501): a Control Center launch's derived key. An item
    already queued under it is returned (``replayed: true``) instead of a second
    one, checked and inserted in one ``BEGIN IMMEDIATE`` transaction and backed by
    a unique index.
    """
    name = validate_node_name(node)
    item = _validate_item(**fields)
    _require_command_shape(item, command)
    ts = storage.now()
    ref = _parse_ts(ts) or _now_dt(None)
    item_id = f"nd-{secrets.token_hex(8)}"
    hold_events: list[dict[str, Any] | None] = []
    wait: dict[str, Any] | None = None
    expired_waits: list[dict[str, Any]] = []
    own = _transaction is None
    if not own and _after_commit is None:
        raise ValueError("a shared enqueue transaction requires after-commit callbacks")
    conn = _transaction if _transaction is not None else storage.connect()
    try:
        if own:
            conn.execute("BEGIN IMMEDIATE")
        if launch_key is not None:
            existing = _launched_item(conn, launch_key)
            if existing is not None:
                if own:
                    conn.commit()
                return existing
        expired_waits = _expire_waiting(conn, ts)
        node_row = _accepting_node(conn, name, item["backend"])
        # Task #2110: the same liveness rule automatic selection applies. A node
        # that is not live gets the item only as visibly waiting, with a
        # deadline after which it expires rather than launching late.
        reason = node_wait_reason(node_row, ref)
        if reason is not None:
            # Read only when it applies: a malformed window must not break
            # work for a live node. NodeError, so dispatch callers handle it.
            try:
                wait_s = node_queue_wait_s()
            except ValueError as exc:
                raise NodeError(str(exc), status=500) from exc
            wait = {
                "reason": reason,
                "last_heartbeat": node_row.get("last_heartbeat"),
                "expires_at": _iso(ref + timedelta(seconds=wait_s)),
                "created_at": ts,
            }
        _require_acceptance(conn, node_row, item["backend"])
        item["profile_agent"] = expected_profile_agent
        item["profile_agent"] = _bind_hermes_profile(node_row, item)
        _require_native_item(
            conn, {**item, "node": name, "agent": node_row["agent"], "created_by": created_by}
        )
        _require_resume_support(node_row, item)
        _require_settings_support(node_row, item)
        _require_realpath_support(node_row, item)
        _require_not_held(conn, item["task_id"], hold_events)
        if item["resume_session_id"] is not None:
            # Check and insert in this one BEGIN IMMEDIATE transaction, so two
            # approved resumes of one session cannot both queue (#1741 review).
            open_resume = conn.execute(
                "SELECT d.id FROM node_dispatch_resume r JOIN node_dispatch d "
                "ON d.id = r.item_id WHERE r.resume_session_id=? "
                "AND d.status IN ('queued', 'claimed') LIMIT 1",
                (item["resume_session_id"],),
            ).fetchone()
            if open_resume is not None:
                raise NodeError(
                    f"a resume of this session is already queued or running ({open_resume['id']})",
                    status=409,
                )
        conn.execute(
            """
            INSERT INTO node_dispatch
              (id, node, agent, backend, prompt, cwd, model, timeout_s, run_id, task_id,
               hermes_profile, profile_agent, permission_mode, settings_source, status, created_by, created_at, updated_at,
               launch_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                item_id,
                name,
                node_row["agent"],
                item["backend"],
                item["prompt"],
                item["cwd"],
                item["model"],
                item["timeout_s"],
                item["run_id"],
                item["task_id"],
                item["hermes_profile"],
                item["profile_agent"],
                item["permission_mode"],
                settings_source
                or (
                    "resumed_session"
                    if item.get("resume_session_id")
                    else "explicit_session"
                    if fields.get("permission_mode") not in (None, "supervised")
                    else "supervised_default"
                ),
                created_by,
                ts,
                ts,
                launch_key,
            ),
        )
        if item["cwd_realpath"] is not None:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO node_dispatch_cwd (item_id, cwd_realpath, created_at) "
                "VALUES (?, ?, ?)",
                (item_id, item["cwd_realpath"], ts),
            )
        if command is not None:
            from . import node_command

            node_command.insert(
                conn,
                item_id,
                argv=command["argv"],
                owner_agent=command["owner_agent"],
                owner_session=command.get("owner_session"),
                as_of=ts,
            )
        if wait is not None:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO node_dispatch_wait (item_id, reason, last_heartbeat, expires_at, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (item_id, wait["reason"], wait["last_heartbeat"], wait["expires_at"], ts),
            )
        if item["resume_session_id"] is not None:
            binding = resume_binding or {}
            conn.execute(
                "INSERT INTO node_dispatch_resume (item_id, resume_session_id, source, "
                "source_item_id, agent_session_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    item["resume_session_id"],
                    str(binding.get("source") or "resume"),
                    binding.get("source_item_id"),
                    binding.get("agent_session_id"),
                    created_by,
                    ts,
                ),
            )
        if own:
            conn.commit()
    except BaseException:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()
    enqueue_event = {
        "node": name,
        "item_id": item_id,
        "backend": item["backend"],
        "task_id": item["task_id"],
    }
    if item["resume_session_id"] is not None:
        enqueue_event["resume_session_id"] = item["resume_session_id"]
    waiting = _wait_view(wait, status="queued", ref=ref)
    if waiting is not None:
        enqueue_event["waiting"] = {"reason": wait["reason"], "expires_at": wait["expires_at"]}

    def publish():
        task_holds.publish(hold_events)
        _settle_expired_waits(expired_waits)
        _record("enqueue", enqueue_event, created_by)

    if own:
        publish()
    else:
        _after_commit.append(publish)
    out = {"item_id": item_id, "node": name, "status": "queued", "created_at": ts}
    if waiting is not None:
        out["waiting"] = waiting
    return out


def request_dispatch(
    node: Any,
    *,
    actor: str,
    expected_profile_agent: str | None = None,
    resume_binding: dict[str, Any] | None = None,
    resume_model_origin: str | None = None,
    launch_key: str | None = None,
    command: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Approval-gated enqueue for the HTTP surface.

    ``launch_key`` (task #2501): when an item was already queued under it, that
    item is returned before the approval gate, so a repeated launch neither queues
    a second item nor consumes an approval.

    Queuing work on a device runs an agent CLI there, so it is a prod action:
    scoped, attributed, and one-shot approved on the exact payload. A pending
    approval raises ``ApprovalPendingError``; re-POST the same body once approved.
    Everything checkable is checked BEFORE the gate, so an approval is never
    consumed by a request that could not have queued.
    """
    from . import config, safety

    name = validate_node_name(node)
    item = _validate_item(**fields)
    _require_command_shape(item, command)
    conn = storage.connect()
    try:
        if launch_key is not None:
            existing = _launched_item(conn, launch_key)
            if existing is not None:
                return existing
        node_row = _accepting_node(conn, name, item["backend"])
        # Before the approval gate, so an unaccepted node never consumes one.
        _require_acceptance(conn, node_row, item["backend"])
        item["profile_agent"] = expected_profile_agent
        item["profile_agent"] = _bind_hermes_profile(node_row, item)
        _require_native_item(
            conn, {**item, "node": name, "agent": node_row["agent"], "created_by": actor}
        )
        _require_resume_support(node_row, item)
        _require_settings_support(node_row, item)
        _require_realpath_support(node_row, item)
        # Before the approval gate, so a held task never consumes an approval.
        _require_not_held(conn, item["task_id"])
    finally:
        conn.close()
    # Task #2110 review: the wait window is checked here, before the gate, for
    # a live target too: the node can go stale between this preflight and
    # enqueue, and enqueue would then refuse after the approval was consumed.
    # The hub refuses to start with a malformed value, so this only bites a
    # process that skipped that check; enqueue keeps its own check for races.
    try:
        node_queue_wait_s()
    except ValueError as exc:
        raise NodeError(
            f"{exc}; fix UA_NODE_QUEUE_WAIT_S in the hub environment and restart the hub",
            status=400,
        ) from exc
    settings_source = (
        "resumed_session"
        if item["resume_session_id"]
        else "explicit_session"
        if fields.get("permission_mode") not in (None, "supervised")
        else "supervised_default"
    )
    if item["resume_session_id"] and resume_model_origin in (
        "operating_profile",
        "explicit_session",
        "native_default",
    ):
        settings_source += ";model=" + resume_model_origin
    if (
        item["model"] is None
        and item["resume_session_id"] is None
        and acp_matrix.acp_backend_name(item["backend"]) is not None
    ):
        from . import operating_profiles

        snapshot = operating_profiles.settings_for_launch(
            item["task_id"], item["profile_agent"] or node_row["agent"]
        )
        if snapshot is None and item["task_id"] is None:
            # Taskless interactive sessions still inherit organization/agent
            # model defaults through the same resolver. Permission stays explicit.
            try:
                resolved = operating_profiles.resolve(
                    agent=item["profile_agent"] or node_row["agent"]
                )
                snapshot = {"settings": resolved["settings"]}
            except Exception:  # noqa: BLE001 - absent defaults preserve native model
                snapshot = None
        chosen_model, reason = operating_profiles.profile_model(
            snapshot,
            agent_label=item["profile_agent"] or node_row["agent"],
            backend=acp_matrix.acp_backend_name(item["backend"]),
        )
        if chosen_model is not None:
            item["model"] = chosen_model
            settings_source += ";model=operating_profile"
    fields = {**fields, "model": item["model"], "permission_mode": item["permission_mode"]}
    gate_payload = {
        "node": name,
        "backend": fields.get("backend"),
        "cwd": fields.get("cwd"),
        "prompt": fields.get("prompt"),
        "model": item["model"],
        "permission_mode": item["permission_mode"],
        "timeout_s": fields.get("timeout_s"),
        "task_id": fields.get("task_id"),
        "run_id": fields.get("run_id"),
    }
    if item["hermes_profile"] is not None:
        gate_payload.update(
            hermes_profile=item["hermes_profile"], profile_agent=item["profile_agent"]
        )
    if item["resume_session_id"] is not None:
        # An approval names the exact session it reopens (#1741).
        gate_payload["resume_session_id"] = item["resume_session_id"]
    if item["cwd_realpath"] is not None:
        # ... and the exact checkout the cwd resolved to when planned (#1770).
        gate_payload["cwd_realpath"] = item["cwd_realpath"]
    if command is not None:
        # ... and the exact argv a supervised command runs (#2413).
        gate_payload["command"] = list(command["argv"])
    token = config.agent_identity_var.set(actor)
    try:
        safety.check_prod_action_gate(NODE_DISPATCH_TOOL, gate_payload)
    finally:
        config.agent_identity_var.reset(token)
    return enqueue(
        name,
        created_by=actor,
        expected_profile_agent=item["profile_agent"],
        resume_binding=resume_binding,
        settings_source=settings_source,
        launch_key=launch_key,
        command=command,
        **fields,
    )


def _require_command_shape(item: dict[str, Any], command: dict[str, Any] | None) -> None:
    """A ``command`` item carries exactly one validated argv and owner, and only it does."""
    from . import node_command

    if (item["backend"] == node_command.BACKEND) != (command is not None):
        raise NodeError("backend 'command' and a command argv go together (task #2413)")
    if command is None:
        return
    try:
        command["argv"] = node_command.validate_argv(command.get("argv"))
    except node_command.CommandError as exc:
        raise NodeError(str(exc)) from exc
    if not isinstance(command.get("owner_agent"), str) or not command["owner_agent"]:
        raise NodeError("a command needs an owner_agent")


def _launched_item(conn: sqlite3.Connection, launch_key: str) -> dict[str, Any] | None:
    """The item a launch key already queued, as ``enqueue`` reports it (task #2501)."""
    row = conn.execute(
        "SELECT id, node, status, created_at FROM node_dispatch WHERE launch_key=?",
        (launch_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "item_id": row["id"],
        "node": row["node"],
        "status": row["status"],
        "created_at": row["created_at"],
        "replayed": True,
    }


def item_for_launch_key(launch_key: str) -> dict[str, Any] | None:
    """Public read of ``_launched_item`` for launch-attempt reconciliation."""
    conn = storage.connect()
    try:
        return _launched_item(conn, launch_key)
    finally:
        conn.close()


def _fail_expired(conn: sqlite3.Connection, name: str, as_of: str) -> list[dict[str, Any]]:
    """Fail expired claims without starting a second potentially overlapping run.

    A lost heartbeat does not prove that the backend stopped. Its outcome must
    be reconciled before an operator dispatches the work again.
    """
    expired = [
        _row(r)
        for r in conn.execute(
            "SELECT id, run_id FROM node_dispatch WHERE node=? AND status='claimed' "
            "AND lease_expires_at < ?",
            (name, as_of),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE node_dispatch SET status='failed', completed_at=?, updated_at=?, "
        "receipt_json=? WHERE node=? AND status='claimed' AND lease_expires_at < ? ",
        (
            as_of,
            as_of,
            json.dumps(
                {
                    "status": "failed",
                    "reason": "lease_expired_outcome_unknown",
                    "hub_generated": True,
                }
            ),
            name,
            as_of,
        ),
    )
    _stash_revoked(
        session_credentials.revoke_for_items(conn, [row["id"] for row in expired], "lease_expired"),
        "lease_expired",
        conn,
    )
    return expired


# Credentials revoked inside a transaction this module does not commit (the
# coordinator's ``settle_expired_run``, or ``_fail_expired`` inside claim_next).
# Their ``.revoked`` events go out from ``flush_revoked_events`` once the
# transaction has committed. An id whose revocation is not committed yet stays
# pending (a flush can run while that transaction is still open) until it
# commits or ``_PENDING_TTL_S`` passes, when its transaction is taken as rolled back.
#
# Each entry carries the identity of the revocation it stashed: the reason and
# the ``revoked_at`` its transaction wrote. A committed row with another reason
# or ``revoked_at`` was revoked by someone else, who records that event: the
# stashed transaction rolled back and the entry is dropped (tasks #1893, #1898).
# A credential is revoked at most once (every revoke is guarded by ``revoked_at
# IS NULL``), so a flush emits at most one event per credential, even when two
# stashed sweeps share a reason and a second.
_PENDING_REVOKED: list[tuple[str, str, str | None, float]] = []
_PENDING_LOCK = threading.Lock()
_PENDING_TTL_S = 600.0


def _stash_revoked(
    credential_ids: list[str], reason: str, conn: sqlite3.Connection | None = None
) -> None:
    """Stash revocations made in ``conn``'s open transaction for a later flush.

    ``conn`` is the revoking connection. Its uncommitted ``revoked_at`` is read
    so the flush can tell this revocation from another with the same reason;
    without it the entry matches on the reason alone.
    """
    if not credential_ids:
        return
    revoked_at: dict[str, str | None] = {}
    if conn is not None:
        try:
            revoked_at = {
                row["credential_id"]: row["revoked_at"]
                for row in conn.execute(
                    "SELECT credential_id, revoked_at FROM session_credentials "
                    f"WHERE credential_id IN ({','.join('?' * len(credential_ids))})",
                    list(credential_ids),
                ).fetchall()
            }
        except sqlite3.Error:
            revoked_at = {}
    stamp = time.monotonic()
    with _PENDING_LOCK:
        _PENDING_REVOKED.extend((cid, reason, revoked_at.get(cid), stamp) for cid in credential_ids)


def flush_revoked_events(*, actor: str | None) -> None:
    """Record ``.revoked`` once per stashed revocation that committed."""
    with _PENDING_LOCK:
        pending = list(_PENDING_REVOKED)
        _PENDING_REVOKED.clear()
    if not pending:
        return
    try:
        conn = storage.connect()
        try:
            committed = {
                row["credential_id"]: (row["revoke_reason"], row["revoked_at"])
                for row in conn.execute(
                    "SELECT credential_id, revoke_reason, revoked_at FROM session_credentials "
                    "WHERE revoked_at IS NOT NULL "
                    f"AND credential_id IN ({','.join('?' * len(pending))})",
                    [entry[0] for entry in pending],
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error:
        committed = {}
    now = time.monotonic()
    keep: list[tuple[str, str, str | None, float]] = []
    emitted: set[str] = set()
    for cid, reason, revoked_at, stamp in pending:
        if cid in committed:
            # Every entry for a committed credential settles here. Only an entry
            # whose revocation is the stored one emits, and only once; any other
            # entry's transaction rolled back, and the revoke that did commit
            # records its own event.
            stored_reason, stored_at = committed[cid]
            if (
                cid not in emitted
                and stored_reason == reason
                and (revoked_at is None or revoked_at == stored_at)
            ):
                emitted.add(cid)
                session_credentials.emit_revoked([cid], reason, actor=actor)
        elif now - stamp < _PENDING_TTL_S:
            keep.append((cid, reason, revoked_at, stamp))
    if keep:
        with _PENDING_LOCK:
            _PENDING_REVOKED.extend(keep)


def claim_next(
    node: Any,
    *,
    identity: str,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the oldest queued item for ``node``; None when empty.

    The legacy, unjournaled claim: a lost response cannot be reconciled. A
    worker that persists a claim-operation id uses ``claim_operation``.
    """
    item, _disposition = _claim(node, identity=identity, device_node=device_node, now=now)
    return item


def claim_operation(
    node: Any,
    claim_op: Any,
    *,
    identity: str,
    device_node: str | None = None,
    final: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim, or reconcile, one durable logical claim operation (task #2136).

    Returns ``{"item": <payload or None>, "claim_op": <disposition>}``. The
    operation row is read and settled inside the same transaction that claims
    the item, so an original handler and any reconciliation for the same id
    agree on one disposition. ``final`` is the last poll of a long poll: an
    empty queue then closes the operation as ``no_claim`` instead of leaving it
    ``pending`` for the next poll. ``item`` is set only when the bound attempt
    is still this caller's to execute. See ``node_claim_ops`` for the contract.
    """
    op = node_claim_ops.parse(claim_op)
    item, disposition = _claim(
        node, identity=identity, device_node=device_node, now=now, op=op, final=final
    )
    return {"item": item, "claim_op": disposition}


def _claim_payload(
    conn: sqlite3.Connection, row: dict[str, Any], lease: datetime
) -> dict[str, Any]:
    """The execution payload for a claim, read inside the claiming transaction."""
    return {
        "item_id": row["id"],
        "node": row["node"],
        "agent": row["agent"],
        "backend": row["backend"],
        "hermes_profile": row["hermes_profile"],
        "profile_agent": row["profile_agent"],
        "prompt": row["prompt"],
        "cwd": row["cwd"],
        "model": row["model"],
        "permission_mode": row["permission_mode"]
        or ("supervised" if str(row["backend"]).startswith("acp:") else None),
        "settings_source": row["settings_source"] or "legacy_supervised_default",
        "timeout_s": row["timeout_s"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "attempt": int(row["attempts"]) + 1,
        "lease_expires_at": _iso(lease),
        # Receipt-linked ACP session id (task #1328): joins a completed
        # dispatch to its agent_sessions row. Not a resume handle.
        "session_id": row["session_id"],
        # The harness session this item reopens (task #1741), or None for a
        # fresh one. Sent only to a node that advertised session_resume.
        **_resume_claim(row["id"], conn),
        # The planned checkout realpath (task #1770): the node refuses to
        # launch when the cwd no longer resolves to it. None when unbound.
        "cwd_realpath": _planned_realpath(row["id"], conn),
        # Whether a finished ACP session should idle for operator follow-ups:
        # only for work a human started outside the scheduler (task #1741).
        "hold_for_followups": _holds_for_followups(row),
    }


def _bound_refusal(
    conn: sqlite3.Connection,
    op_row: dict[str, Any],
    *,
    device_node: str | None,
    ref: datetime,
) -> str | None:
    """Why a bound attempt may no longer be executed, or None when it still may.

    A recorded binding is history, not a fresh execution grant: the item must
    still be claimed by exactly that attempt, under a live lease, by the same
    device, with no cancellation, receipt or policy change since. Nothing here
    renews a lease or reissues an attempt.
    """
    if (op_row.get("device_node") or "") != (device_node or ""):
        return "device_changed"
    item = _row(
        conn.execute(
            "SELECT * FROM node_dispatch WHERE id=? AND node=?",
            (op_row["item_id"], op_row["node"]),
        ).fetchone()
    )
    if item is None:
        return "item_missing"
    if item["receipt_json"] is not None:
        return "receipt_stored"
    if item["status"] != "claimed":
        return f"item_{item['status']}"
    if int(item["attempts"] or 0) != int(op_row["attempt"]):
        return "attempt_superseded"
    lease = _parse_ts(item.get("lease_expires_at"))
    if lease is None or lease <= ref:
        return "lease_expired"
    if _cancel_requested(conn, item):
        return "cancel_requested"
    if item.get("run_id"):
        run = conn.execute(
            "SELECT state FROM dispatch_runs WHERE run_id=?", (item["run_id"],)
        ).fetchone()
        if run is not None and run["state"] not in ("launched", "running"):
            return "run_not_live"
    try:
        _require_native_item(conn, item)
    except NodeError:
        return "policy_denied"
    return None


def _settled(
    conn: sqlite3.Connection,
    op_row: dict[str, Any],
    *,
    device_node: str | None,
    ref: datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The item (only if still executable) and disposition of a settled operation."""
    if op_row["state"] != "bound":
        return None, node_claim_ops.disposition(op_row, executable=False)
    refusal = _bound_refusal(conn, op_row, device_node=device_node, ref=ref)
    payload = node_claim_ops.snapshot(op_row)
    if payload is None and refusal is None:
        refusal = "snapshot_unreadable"
    executable = refusal is None
    return (
        payload if executable else None,
        node_claim_ops.disposition(op_row, executable=executable, refusal=refusal),
    )


def _claim(
    node: Any,
    *,
    identity: str,
    device_node: str | None = None,
    now: datetime | None = None,
    op: node_claim_ops.ClaimOp | None = None,
    final: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    name = validate_node_name(node)
    ref = _now_dt(now)
    as_of = _iso(ref)
    now_s = int(ref.timestamp())
    expired: list[dict[str, Any]] = []
    expired_waits: list[dict[str, Any]] = []
    denied: dict[str, Any] | None = None
    refused: NodeError | None = None
    row = None
    payload: dict[str, Any] | None = None
    disposition: dict[str, Any] | None = None
    claimed = False
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        node_row = _owned_node(conn, name, identity, device_node)
        expired = _fail_expired(conn, name, as_of)
        # Persist waiting cancellation/expiry and its linked-run close intent
        # before the generic bulk update removes queued rows from the sweep.
        expired_waits = _expire_waiting(conn, as_of, node=name)
        # Items cancelled before they were ever claimed never reach the node —
        # directly, or through a governed dispatch_cancel on the linked run.
        conn.execute(
            "UPDATE node_dispatch SET status='cancelled', completed_at=?, updated_at=? "
            "WHERE node=? AND status='queued' AND (cancel_requested=1 OR run_id IN ("
            "SELECT run_id FROM dispatch_runs WHERE termination_outcome IS NOT NULL "
            "OR state IN ('cancelled', 'finished', 'failed', 'blocked', 'denied', 'expired')))",
            (as_of, as_of, name),
        )
        # A paused, terminated or retired device owner claims nothing (tasks
        # #2305, #2315), whatever is queued. The check runs after the expiry and
        # cancellation settlement above, which commits despite the refusal: a
        # pause's stop handler marks the owner's queued items cancel-requested,
        # and only this sweep moves them to ``cancelled``. A paused owner's node
        # waits for the resume: the refusal code says it is not a credential
        # failure.
        refused = _running_identity_refusal(conn, node_row["agent"], site="node_queue.claim")
        if refused is not None:
            raise refused
        op_row: dict[str, Any] | None = None
        select = True
        if op is not None:
            # The operation's one linearization point (task #2136): read and
            # settle it in this transaction, before any queue mutation.
            node_claim_ops.cleanup(conn, now_s)
            op_row = node_claim_ops.load(conn, name, op.id)
            if op_row is not None:
                node_claim_ops.check_same_request(op_row, op, identity)
            if op.mode == "reconcile":
                # Reconciliation never selects work. An operation the hub has
                # not settled is closed here, so its original cannot commit.
                if op_row is None:
                    op_row = node_claim_ops.insert(
                        conn,
                        name,
                        op,
                        identity=identity,
                        device_node=device_node,
                        state="no_claim",
                        reason="reconciled_unknown",
                        now=now_s,
                        as_of=as_of,
                    )
                elif op_row["state"] == "pending":
                    op_row = node_claim_ops.close(
                        conn, op_row, reason="reconciled_pending", now=now_s, as_of=as_of
                    )
                select = False
            elif op_row is None:
                node_claim_ops.require_fresh(op, now_s)
                op_row = node_claim_ops.insert(
                    conn,
                    name,
                    op,
                    identity=identity,
                    device_node=device_node,
                    state="pending",
                    reason=None,
                    now=now_s,
                    as_of=as_of,
                )
            elif op_row["state"] != "pending":
                select = False
            elif (op_row.get("device_node") or "") != (device_node or ""):
                raise NodeError("claim_op.id conflicts with an earlier claim operation", status=409)
            elif abs(now_s - op.issued_at) > node_claim_ops.MAX_AGE_S:
                op_row = node_claim_ops.close(
                    conn, op_row, reason="operation_expired", now=now_s, as_of=as_of
                )
                select = False
            if not select:
                payload, disposition = _settled(conn, op_row, device_node=device_node, ref=ref)
        if select:
            # A run-linked item is invisible until its run is actually launched:
            # the launch boundary commits the item before the coordinator marks
            # the run launched, so a node must not see it earlier. A run that dies
            # while still 'claimed' is settled by reclaim (unclaimed -> retryable).
            # A run_id with no dispatch_runs row does not gate: the coordinator
            # inserts the row before invoking the launch boundary, so a missing
            # row means the link is advisory (manual enqueue) and there is no
            # lifecycle to corrupt.
            from . import node_acceptance

            acceptance_sql, acceptance_params = node_acceptance.claim_filter(conn, node_row, ref)
            # A paused dispatch target is not a dead item: failing it would
            # make the later resume unable to run the work. Skip it this claim.
            skipped: list[str] = []
            while True:
                exclude_sql = ""
                exclude_params: tuple[str, ...] = ()
                if skipped:
                    exclude_sql = f"AND id NOT IN ({','.join('?' * len(skipped))}) "
                    exclude_params = tuple(skipped)
                row = _row(
                    conn.execute(
                        "SELECT * FROM node_dispatch WHERE node=? AND status='queued' "
                        + acceptance_sql
                        + exclude_sql
                        + "AND (run_id IS NULL OR NOT EXISTS ("
                        "SELECT 1 FROM dispatch_runs "
                        "WHERE dispatch_runs.run_id = node_dispatch.run_id "
                        "AND (state NOT IN ('launched', 'running') "
                        "OR termination_outcome IS NOT NULL))) "
                        "ORDER BY created_at ASC, rowid ASC LIMIT 1",
                        (name, *acceptance_params, *exclude_params),
                    ).fetchone()
                )
                if row is None:
                    break
                try:
                    held = _hold_reason(conn, row)
                    if held:
                        # A held task keeps its item queued (task #2493): the hold
                        # clears and the work resumes, so failing the item would
                        # destroy it. Same reasoning as a paused target below.
                        skipped.append(row["id"])
                        row = None
                        continue
                    _require_native_item(conn, row)
                    _require_settings_support(node_row, row)
                    row["cwd_realpath"] = _planned_realpath(row["id"], conn)
                    _require_realpath_support(node_row, row)
                except NodeError as exc:
                    if exc.code == agent_lifecycle.REFUSAL_CODES["paused"]:
                        skipped.append(row["id"])
                        row = None
                        continue
                    denied = {**row, "denial_reason": str(exc)}
                    conn.execute(
                        "UPDATE node_dispatch SET status='failed', completed_at=?, updated_at=? "
                        "WHERE id=? AND status='queued'",
                        (as_of, as_of, row["id"]),
                    )
                    row = None
                    break
                break
            if row is not None:
                lease = ref + timedelta(seconds=int(row["timeout_s"]) + LEASE_GRACE_S)
                conn.execute(
                    "UPDATE node_dispatch SET status='claimed', attempts=attempts+1, "
                    "claimed_at=?, lease_expires_at=?, updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (as_of, _iso(lease), as_of, row["id"]),
                )
                # The node has the work now: the linked run moves launched ->
                # running and its lease follows the item's, in the same txn.
                _renew_linked_run(
                    conn,
                    row.get("run_id"),
                    lease=lease,
                    target_state="running",
                    expected_states=("launched", "running"),
                )
                payload = _claim_payload(conn, row, lease)
                if row["backend"] == "command":
                    # A fresh fence for this attempt (task #2413): the receipt
                    # must echo it, so an earlier claim's writer is refused.
                    from . import node_command

                    payload["command"] = node_command.mint_fence(
                        conn, row["id"], payload["attempt"], as_of
                    )
                claimed = True
                if op_row is not None:
                    op_row = node_claim_ops.bind(conn, op_row, payload, now=now_s, as_of=as_of)
            elif op_row is not None and final:
                op_row = node_claim_ops.close(
                    conn, op_row, reason="no_item", now=now_s, as_of=as_of
                )
            if op_row is not None:
                disposition = node_claim_ops.disposition(op_row, executable=claimed)
        conn.commit()
    except BaseException as exc:
        if refused is None or exc is not refused:
            conn.rollback()
            raise
        # The owner refusal keeps the settlement; it is raised below, after
        # the same post-commit sweep a normal claim runs.
        conn.commit()
    finally:
        conn.close()
    flush_revoked_events(actor=identity)
    _settle_expired_waits(expired_waits)
    for failed in expired:
        if failed.get("run_id"):
            _close_linked_run(
                failed["run_id"],
                "failed",
                reason="lease_expired_outcome_unknown",
                actor=identity,
                evidence_ref=f"node:{name}:{failed['id']}",
            )
    if denied is not None:
        _record(
            "policy_denied",
            {"node": name, "item_id": denied["id"], "reason": denied["denial_reason"]},
            identity,
        )
        if denied.get("run_id"):
            _close_linked_run(
                denied["run_id"],
                "failed",
                reason="trust_preset_native_denied",
                actor=identity,
                evidence_ref=f"node:{name}:{denied['id']}",
            )
    # Follow-ups of this node's items that just ended without a node receipt
    # (cancelled before start, lease expired, policy denied) are dropped, with
    # events, rather than left queued (task #1740).
    node_followups.sweep_ended(actor=identity, node=name)
    if refused is not None:
        raise refused
    if claimed and payload is not None:
        event: dict[str, Any] = {"node": name, "item_id": payload["item_id"]}
        if op is not None:
            event["claim_op"] = op.id
        _record("claim", event, identity)
    elif op is not None and disposition is not None and op.mode == "reconcile":
        _record(
            "claim_reconciled",
            {
                "node": name,
                "claim_op": op.id,
                "state": disposition["state"],
                "item_id": disposition["item_id"],
                "executable": disposition["executable"],
                "reason": disposition["reason"],
            },
            identity,
        )
    return payload, disposition


def _holds_for_followups(row: dict[str, Any]) -> bool:
    return not row.get("run_id") and config.actor_kind_for_identity(row.get("created_by")) == (
        "human"
    )


def _planned_realpath(item_id: str, conn: sqlite3.Connection | None = None) -> str | None:
    own = conn is None
    conn = conn or storage.connect()
    try:
        try:
            row = conn.execute(
                "SELECT cwd_realpath FROM node_dispatch_cwd WHERE item_id=?", (item_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
    finally:
        if own:
            conn.close()
    return row["cwd_realpath"] if row is not None else None


def _resume_claim(item_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """``resume_session_id`` and its ``resume_source`` (``resume`` | ``adopt``)."""
    own = conn is None
    conn = conn or storage.connect()
    try:
        row = conn.execute(
            "SELECT resume_session_id, source FROM node_dispatch_resume WHERE item_id=?",
            (item_id,),
        ).fetchone()
    finally:
        if own:
            conn.close()
    if row is None:
        return {"resume_session_id": None, "resume_source": None}
    return {"resume_session_id": row["resume_session_id"], "resume_source": row["source"]}


def cancel(item_id: str, *, actor: str) -> dict[str, Any]:
    """Request cancellation; relayed to the node on its next heartbeat."""
    revoked: list[str] = []
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE node_dispatch SET cancel_requested=1, updated_at=? WHERE id=? "
            "AND status IN ('queued', 'claimed')",
            (storage.now(), item_id),
        )
        changed = cur.rowcount
        if changed:
            # Revoked at the cancel request, not at the receipt (task #1744).
            revoked = session_credentials.revoke_for_item(conn, item_id, "cancelled")
        conn.commit()
    finally:
        conn.close()
    session_credentials.emit_revoked(revoked, "cancelled", actor=actor)
    if changed:
        _record("cancel_requested", {"item_id": item_id}, actor)
    return {"item_id": item_id, "cancel_requested": bool(changed)}


def cancel_for_run_evidence(evidence_ref: str) -> str:
    """Terminate path for a ``node:<node>:<item>`` evidence token (task #1327).

    A governed cancellation of a node-routed run cannot signal a local pid —
    the work lives on a device. Returns a ``terminate``-style outcome:
    ``"already_gone"`` when the item never ran or is already terminal
    (cessation confirmed), or ``"node_cancel_requested"`` when the item was
    claimed and the cancel is queued for the node's next heartbeat — cessation
    then stays unconfirmed until the node's receipt proves the backend exited.
    """
    parts = str(evidence_ref or "").split(":")
    if len(parts) != 3 or parts[0] != "node":
        return "already_gone"
    _node_name, item_id = parts[1], parts[2]
    item = get_item(item_id)
    if item is None or item["status"] in TERMINAL_ITEM_STATUSES:
        return "already_gone"
    ts = storage.now()
    conn = storage.connect()
    try:
        if item["status"] == "queued":
            cur = conn.execute(
                "UPDATE node_dispatch SET status='cancelled', completed_at=?, updated_at=? "
                "WHERE id=? AND status='queued'",
                (ts, ts, item_id),
            )
            conn.commit()
            if cur.rowcount:
                # It never ran: its follow-ups are dropped now (task #1740).
                node_followups.sweep_ended(actor="dispatch-cancel", item_id=item_id)
                return "already_gone"
            # Lost the race: the item was claimed between read and write —
            # fall through and request the cancel on the claimed item.
        cur = conn.execute(
            "UPDATE node_dispatch SET cancel_requested=1, updated_at=? WHERE id=? "
            "AND status='claimed'",
            (ts, item_id),
        )
        revoked = (
            session_credentials.revoke_for_item(conn, item_id, "cancelled") if cur.rowcount else []
        )
        conn.commit()
        session_credentials.emit_revoked(revoked, "cancelled", actor="dispatch-cancel")
        if not cur.rowcount:
            # The item left 'claimed' between read and write. A node receipt
            # landing in between proves the backend exited.
            fresh = get_item(item_id)
            if fresh is None or fresh["status"] in TERMINAL_ITEM_STATUSES:
                return "already_gone"
    finally:
        conn.close()
    _record(
        "cancel_requested", {"item_id": item_id, "run_id": item.get("run_id")}, "dispatch-cancel"
    )
    return "node_cancel_requested"


def sweep_ended_followups(*, actor: str) -> list[str]:
    """Drop, with events, follow-ups of every item that has ended (task #1740).

    For callers that end node items inside their own transaction (the
    coordinator's ``settle_expired_run``): call after that transaction commits.
    Also records the ``.revoked`` events of session credentials that
    transaction revoked (task #1744).
    """
    flush_revoked_events(actor=actor)
    return node_followups.sweep_ended(actor=actor)


def _outcome_state(status: str) -> str:
    return "finished" if status == "finished" else "failed"


# The same binding as ``_run_bound_to_item``, as a condition on a
# ``dispatch_runs`` row. Passed into the run's compare-and-swap so the check
# and the write are one statement (task #1950, PR #1230 review): a snapshot
# check alone let a resume land between it and the close.
_BOUND_TO_ITEM_SQL = (
    "evidence_ref = ? OR (evidence_ref IS NULL AND state IN ('launched','running') "
    "AND ? = (SELECT nd.id FROM node_dispatch nd WHERE nd.run_id = dispatch_runs.run_id "
    "ORDER BY nd.created_at DESC, nd.rowid DESC LIMIT 1))"
)
_LIVE_RUN_SQL = "state IN ('claimed','launched','running')"
_PENDING_CANCEL_SQL = "state = 'cancelled' AND termination_outcome IN ('requested','unknown')"


def _bound_guard(evidence_ref: str, condition: str) -> tuple[str, tuple[Any, ...]]:
    item_id = evidence_ref.rsplit(":", 1)[-1]
    return f"({_BOUND_TO_ITEM_SQL}) AND {condition}", (evidence_ref, item_id)


def _run_bound_to_item(run: dict[str, Any], evidence_ref: str) -> bool:
    """Is ``evidence_ref`` (``node:<node>:<item>``) the run's current item? (#1950)"""
    bound = run.get("evidence_ref")
    if bound is not None:
        return bound == evidence_ref
    if run.get("state") not in ("launched", "running"):
        return False
    conn = storage.connect()
    try:
        newest = conn.execute(
            "SELECT id FROM node_dispatch WHERE run_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT 1",
            (run["run_id"],),
        ).fetchone()
    finally:
        conn.close()
    return newest is not None and evidence_ref.rsplit(":", 1)[-1] == newest["id"]


def _close_linked_run(
    run_id: str,
    status: str,
    *,
    reason: str | None,
    actor: str,
    evidence_ref: str,
    node_confirmed: bool = False,
) -> dict[str, Any]:
    """Close a linked dispatch run, so dispatch_delivery_receipt sees the outcome.

    Safe to call again: a run already in a terminal state is left alone, which
    is what lets a duplicate receipt retry a close that failed the first time.

    Only the run's current item may close it (task #1950). ``resume_run``
    reuses the run id with a new item, so a late or duplicate receipt for the
    old item would otherwise fail the resumed run, or confirm cessation of a
    cancellation aimed at the new item. A run whose ``evidence_ref`` names
    another item is ``run_rebound`` and left alone. A run with no evidence is
    bound only to its newest item and only once launched: a resumed run sits
    in ``claimed`` with its evidence cleared until its new item is enqueued,
    and an old item must not close it then either. A run that is no longer
    live (``retryable``: the reconciler already handed it back) is
    ``run_not_live`` and is not transitioned again. The snapshot checks here
    pick the reason; the same conditions are enforced again inside the write
    (``guard_sql``), so a run rebound or released after the check is refused
    as ``guard_refused`` and left untouched.

    ``node_confirmed`` marks a real node-posted receipt: it proves the backend
    on the device exited, so a cancellation still pending confirmation
    (``termination_outcome`` of ``requested``/``unknown``) is finalized as
    confirmed. Hub-generated lease-expiry calls leave it False — a lost
    heartbeat never proves cessation.
    """
    try:
        from .dispatch import coordinator, scheduler

        run = coordinator.get_run(run_id)
        if run is not None and not _run_bound_to_item(run, evidence_ref):
            return {"ok": False, "reason": "run_rebound", "state": run["state"]}
        if (
            node_confirmed
            and run is not None
            and run["state"] == "cancelled"
            and run.get("termination_outcome") in ("requested", "unknown")
        ):
            guard_sql, guard_params = _bound_guard(evidence_ref, _PENDING_CANCEL_SQL)
            return coordinator.finalize_cancellation(
                run,
                actor=actor,
                reason=f"node_receipt:{status}",
                termination="node_receipt",
                cessation="confirmed",
                guard_sql=guard_sql,
                guard_params=guard_params,
            )
        if run is not None and run["state"] in scheduler.TERMINAL_OUTCOMES:
            return {"ok": True, "reason": "already_terminal", "state": run["state"]}
        if run is not None and run["state"] not in ("claimed", "launched", "running"):
            return {"ok": False, "reason": "run_not_live", "state": run["state"]}
        state = _outcome_state(status)
        guard_sql, guard_params = _bound_guard(evidence_ref, _LIVE_RUN_SQL)
        return scheduler.record_run_outcome(
            run_id,
            state,
            actor=actor,
            failure_reason=None if state == "finished" else (reason or status),
            evidence_ref=evidence_ref,
            guard_sql=guard_sql,
            guard_params=guard_params,
        )
    except Exception as exc:  # noqa: BLE001 - the queue row itself is durable
        return {"ok": False, "reason": f"record_run_outcome failed: {exc}"[:200]}


def _bounded_str(value: Any, limit: int) -> str | None:
    return None if value is None else str(value)[:limit]


def _bounded_log_capture(value: Any) -> dict[str, Any] | None:
    """An attempt's node-local capture snapshot, never a live/final-log promise."""
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    return {
        "state": state if state in ("pending", "complete", "error", "unknown") else "unknown",
        "truncated": value.get("truncated") if isinstance(value.get("truncated"), bool) else None,
        "error": _bounded_str(value.get("error"), 300),
        "state_path": _bounded_str(value.get("state_path"), 500),
    }


def _bounded_receipt(receipt: dict[str, Any], *, name: str, as_of: str) -> dict[str, Any]:
    """Only named, typed, size-capped fields reach storage."""
    exit_code = receipt.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    files = receipt.get("files_modified")
    files = files if isinstance(files, list) else []
    return {
        "status": str(receipt.get("status")),
        "reason": _bounded_str(receipt.get("reason") or None, 300),
        "text": str(receipt.get("text") or "")[:MAX_RECEIPT_TEXT],
        "exit_code": exit_code,
        "backend_status": _bounded_str(receipt.get("backend_status"), 40),
        "files_modified": [str(f)[:500] for f in files[:200]],
        "started_at": _bounded_str(receipt.get("started_at"), 40),
        "finished_at": _bounded_str(receipt.get("finished_at"), 40),
        "attempt": receipt.get("attempt") if isinstance(receipt.get("attempt"), int) else None,
        # ACP session id negotiated by the backend (task #1328); NULL for native.
        "session_id": _bounded_str(receipt.get("session_id"), 200),
        # A supervised command's fence and log pointer (task #2413).
        "fence_token": _bounded_str(receipt.get("fence_token"), 80),
        "log_path": _bounded_str(receipt.get("log_path"), 500),
        # Snapshot at primary-command completion, not a future guarantee about
        # surviving descendants' output (task #2552).
        "truncated": bool(receipt.get("truncated")),
        "log_capture": _bounded_log_capture(receipt.get("log_capture")),
        "recorded_at": as_of,
        "node": name,
    }


def post_receipt(
    node: Any,
    item_id: Any,
    *,
    identity: str,
    receipt: Any,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the node's receipt for one item. Idempotent by item id.

    The first receipt wins and is final. A retried POST (the node lost the
    response and resent it) returns the stored receipt with ``duplicate: true``
    and records nothing new; it only retries closing a linked run that is not
    yet terminal. ``receipt["attempt"]`` fences stale claims: a receipt from an
    attempt that is no longer the latest claim is refused.
    """
    name = validate_node_name(node)
    item_key = str(item_id or "")
    if not _ITEM_RE.fullmatch(item_key):
        raise NodeError("item_id is malformed")
    if not isinstance(receipt, dict):
        raise NodeError("receipt must be a JSON object")
    status = str(receipt.get("status") or "")
    if status not in RECEIPT_STATUSES:
        raise NodeError(f"receipt status must be one of {sorted(RECEIPT_STATUSES)}")
    as_of = _iso(_now_dt(now))
    stored = _bounded_receipt(receipt, name=name, as_of=as_of)
    evidence_ref = f"node:{name}:{item_key}"
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        item = _row(
            conn.execute(
                "SELECT * FROM node_dispatch WHERE id=? AND node=?", (item_key, name)
            ).fetchone()
        )
        if item is None:
            raise NodeError(f"item {item_key!r} is not queued for node {name!r}", status=404)
        if item["receipt_json"] is not None:
            if (_load(item["receipt_json"]) or {}).get("hub_generated"):
                raise NodeError(
                    "lease expired; execution outcome requires reconciliation", status=409
                )
            conn.commit()
            first = _load(item["receipt_json"]) or {}
            run_outcome = None
            if item.get("run_id"):
                run_outcome = _close_linked_run(
                    item["run_id"],
                    str(first.get("status") or item["status"]),
                    reason=first.get("reason"),
                    actor=identity,
                    evidence_ref=evidence_ref,
                    node_confirmed=True,
                )
            return {
                "item_id": item_key,
                "duplicate": True,
                "status": item["status"],
                "run_outcome": run_outcome,
                "receipt": first,
            }
        attempts = int(item["attempts"] or 0)
        if stored["attempt"] is not None and stored["attempt"] != attempts:
            raise NodeError(
                f"stale receipt: attempt {stored['attempt']} is not the latest claim ({attempts})",
                status=409,
            )
        if item["status"] != "claimed":
            raise NodeError(f"item {item_key!r} is {item['status']}, not claimed", status=409)
        command_row = None
        if item["backend"] == "command":
            from . import node_command

            try:
                command_row = node_command.check_fence(
                    conn, item_key, attempt=attempts, token=stored["fence_token"]
                )
            except node_command.CommandError as exc:
                raise NodeError(str(exc), status=exc.status, code=exc.code) from exc
        # One compare-and-set on the claim this receipt answers (task #2413).
        changed = conn.execute(
            "UPDATE node_dispatch SET status=?, receipt_json=?, completed_at=?, "
            "session_id=COALESCE(?, session_id), "
            "lease_expires_at=NULL, updated_at=? WHERE id=? AND receipt_json IS NULL "
            "AND status='claimed' AND attempts=?",
            (status, json.dumps(stored), as_of, stored["session_id"], as_of, item_key, attempts),
        ).rowcount
        if changed != 1:
            raise NodeError(f"item {item_key!r} changed under this receipt", status=409)
        if command_row is not None:
            node_command.record_completion(conn, item, command_row, stored, as_of)
        # The session is over: its hub credential ends in this same transaction.
        revoked = session_credentials.revoke_for_item(conn, item_key, "session_end")
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, NodeError) and exc.code == "stale_fence":
            _record(
                "stale_fence",
                {"node": name, "item_id": item_key, "attempt": stored["attempt"]},
                identity,
            )
        raise
    finally:
        conn.close()

    session_credentials.emit_revoked(revoked, "session_end", actor=identity)
    if item["backend"] == "command":
        # The owner hears once, by label, even if its session is gone (#2413).
        from . import node_command

        node_command.deliver_notices(item_id=item_key)
    event_id = _record(
        "receipt",
        {"node": name, "item_id": item_key, "status": status, "task_id": item["task_id"]},
        identity,
    )
    # The session is over: an operator follow-up it never confirmed delivering
    # is closed as dropped rather than left looking queued (task #1740).
    if acp_matrix.acp_backend_name(item["backend"]) is None:
        # A native resume (task #1741) streams no session events: the receipt
        # is what ends its adopted session row, with the receipt's status, before
        # the generic sweep below could close it as item_<status>. The binding stays.
        from . import node_resume

        node_resume.close_native_for_item(item_key, name, status)
    node_followups.sweep_ended(actor=identity, item_id=item_key)
    if item["backend"] == "acceptance":
        # Device acceptance (task #2112): record the run's outcome as it lands.
        from . import node_acceptance

        node_acceptance.on_receipt(item)
    run_outcome = None
    if item.get("run_id"):
        run_outcome = _close_linked_run(
            item["run_id"],
            status,
            reason=stored["reason"],
            actor=identity,
            evidence_ref=evidence_ref,
            node_confirmed=True,
        )
    return {
        "item_id": item_key,
        "duplicate": False,
        "status": status,
        "evidence_ref": evidence_ref,
        "event_id": event_id,
        "run_outcome": run_outcome,
        "receipt": stored,
    }


def get_item(item_id: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = _row(conn.execute("SELECT * FROM node_dispatch WHERE id=?", (item_id,)).fetchone())
        if row is not None:
            row["cwd_realpath"] = _planned_realpath(item_id, conn)
    finally:
        conn.close()
    if row is not None:
        row["receipt"] = _load(row.pop("receipt_json"))
    return row


def observed_session_settings() -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """Last negotiated advertisement per node/backend/profile, advisory only.

    A refused first launch still records discovery. Dispatch must renegotiate
    each time; a cached choice can never grant a mode the current agent omits.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT e.payload, e.created_at, d.backend, d.hermes_profile, d.model, "
            "d.permission_mode, d.settings_source, ROW_NUMBER() OVER (PARTITION BY d.node, d.backend, d.hermes_profile ORDER BY e.ulid DESC) AS settings_rank FROM event_log e "
            "JOIN node_dispatch d ON d.id=json_extract(e.payload, '$.item_id') "
            "AND d.node=json_extract(e.payload, '$.node') "
            "WHERE e.channel=? AND json_extract(e.payload, '$.kind')='acp_event' "
            "AND json_extract(e.payload, '$.acp_kind') "
            "IN ('settings_discovered', 'session_open', 'session_settings_update') "
            "AND json_type(e.payload, '$.data.settings')='object' "
            ") WHERE settings_rank=1",
            (CHANNEL_NODE,),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for row in rows:
        payload = _load(row["payload"]) or {}
        data = payload.get("data") or {}
        settings = data.get("settings") if isinstance(data, dict) else None
        if not isinstance(settings, dict):
            continue
        key = (payload.get("node"), row["backend"], row["hermes_profile"])
        out.setdefault(
            key,
            {
                **_project_session_settings(settings, dict(row)),
                "observed_at": _bounded_str(payload.get("at") or row["created_at"], 100),
                "source": "node_session_advertisement",
            },
        )
    return out


def hermes_profile_statuses() -> dict[tuple[str, str], dict[str, Any]]:
    """Latest queue status per discovered profile, independent of recent-item pagination."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, node, hermes_profile, profile_agent, status, cancel_requested, "
            "session_id, updated_at FROM (SELECT *, ROW_NUMBER() OVER ("
            "PARTITION BY node, hermes_profile ORDER BY created_at DESC, rowid DESC) AS rank "
            "FROM node_dispatch WHERE hermes_profile IS NOT NULL) WHERE rank=1"
        ).fetchall()
    finally:
        conn.close()
    return {
        (r["node"], r["hermes_profile"]): {
            "item_id": r["id"],
            "status": r["status"],
            "profile_agent": r["profile_agent"],
            "session_id": r["session_id"],
            "cancel_requested": bool(r["cancel_requested"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


def list_recent_items(*, limit: int = 25) -> list[dict[str, Any]]:
    """Recent node_dispatch rows for the Sessions pane (newest first). Never raises.

    Prompts are returned as a short preview only — restart reuses the stored
    prompt server-side via ``restart`` so readers of ``/api/nodes`` never need
    the full text.
    """
    cap = max(1, min(int(limit or 25), 100))
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT id, node, agent, backend, hermes_profile, profile_agent, prompt, cwd, model, permission_mode, settings_source, timeout_s, "
                "run_id, task_id, status, cancel_requested, session_id, created_by, "
                "created_at, updated_at, completed_at, claimed_at, receipt_json "
                "FROM node_dispatch ORDER BY created_at DESC LIMIT ?",
                (cap,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _row(row)
        receipt = _load(item.pop("receipt_json", None)) or {}
        prompt = str(item.pop("prompt", "") or "")
        preview = prompt if len(prompt) <= 80 else f"{prompt[:77]}…"
        outcome_unknown = bool(
            isinstance(receipt, dict)
            and receipt.get("hub_generated")
            and receipt.get("reason") == "lease_expired_outcome_unknown"
        )
        item["evidence_ref"] = f"node:{item['node']}:{item['id']}"
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        item["prompt_preview"] = preview
        item["outcome_unknown"] = outcome_unknown
        item["restartable"] = item["status"] in TERMINAL_ITEM_STATUSES and not outcome_unknown
        out.append(item)
    try:
        from . import node_resume

        node_resume.annotate_items(out)
    except sqlite3.Error:
        pass
    _annotate_waits(out)
    annotate_registry_sessions(out)
    return out


def annotate_registry_sessions(items: list[dict[str, Any]]) -> None:
    """Add ``agent_session_id``: the hub registry row the item's session is (task #2501).

    ``session_id`` is the harness's own (external) session id; the Control Center
    routes to the hub's ``agent_sessions`` row id. The row whose ``meta.item_id``
    is this item wins; otherwise the newest row for that external id (a resumed
    session keeps one durable row, rebound to its newest item). Tenant-fenced;
    None until the node reports a session. Never raises.
    """
    for item in items:
        item["agent_session_id"] = None
    sids = sorted({str(i["session_id"]) for i in items if i.get("session_id")})
    if not sids:
        return
    from . import edition

    try:
        conn = storage.connect()
        try:
            marks = ",".join("?" for _ in sids)
            rows = conn.execute(
                "SELECT id, external_session_id, json_extract(meta, '$.item_id') AS item_id "
                f"FROM agent_sessions WHERE external_session_id IN ({marks}) AND org_id IS ? "
                "ORDER BY started_at ASC, rowid ASC",
                [*sids, edition.tenant_org_id()],
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    by_item: dict[tuple[str, str], str] = {}
    newest: dict[str, str] = {}
    for row in rows:
        newest[row["external_session_id"]] = row["id"]
        if row["item_id"]:
            by_item[(row["external_session_id"], str(row["item_id"]))] = row["id"]
    for item in items:
        sid = item.get("session_id")
        if sid:
            item["agent_session_id"] = by_item.get((str(sid), str(item["id"]))) or newest.get(
                str(sid)
            )


def _annotate_waits(items: list[dict[str, Any]]) -> None:
    """Attach the ``waiting`` block (#2110) to each listed item. Never raises."""
    for item in items:
        item["waiting"] = None
    ids = [item["id"] for item in items]
    if not ids:
        return
    try:
        conn = storage.connect()
        try:
            ensure_schema(conn)
            marks = ",".join("?" for _ in ids)
            waits = {
                r["item_id"]: _row(r)
                for r in conn.execute(
                    f"SELECT * FROM node_dispatch_wait WHERE item_id IN ({marks})", ids
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return
    ref = _now_dt(None)
    for item in items:
        item["waiting"] = _wait_view(waits.get(item["id"]), status=item["status"], ref=ref)


def restart(item_id: str, *, actor: str, launch_key: str | None = None) -> dict[str, Any]:
    """Re-queue a terminal item using its stored fields (approval-gated).

    ``launch_key`` (task #2501): as for ``request_dispatch``. A repeat after the
    restart queued returns that item even though the source is no longer the
    newest, so it is checked before the terminal-status rule.

    Refuses unconfirmed lease-expiry failures — the original node process may
    still be running, so a second dispatch would overlap.
    """
    if launch_key is not None:
        existing = item_for_launch_key(launch_key)
        if existing is not None:
            return existing
    item = get_item(item_id)
    if item is None:
        raise NodeError(f"item {item_id!r} not found", status=404)
    if item["status"] not in TERMINAL_ITEM_STATUSES:
        raise NodeError(
            f"item {item_id!r} is {item['status']!r}; restart requires a terminal status",
            status=409,
        )
    receipt = item.get("receipt") or {}
    if (
        isinstance(receipt, dict)
        and receipt.get("hub_generated")
        and receipt.get("reason") == "lease_expired_outcome_unknown"
    ):
        raise NodeError(
            f"item {item_id!r} ended with an unconfirmed lease expiry; "
            "reconcile the node outcome before restarting",
            status=409,
        )
    return request_dispatch(
        item["node"],
        actor=actor,
        prompt=item["prompt"],
        cwd=item["cwd"],
        backend=item["backend"],
        model=None
        if set((item.get("settings_source") or "").split(";"))
        & {"model=operating_profile", "model=native_default"}
        else item.get("model"),
        permission_mode=None,
        timeout_s=item.get("timeout_s"),
        task_id=item.get("task_id"),
        run_id=item.get("run_id"),
        hermes_profile=item.get("hermes_profile"),
        expected_profile_agent=item.get("profile_agent"),
        # Re-bind the planned checkout realpath (#1770/#2011): dropping it would
        # let a checkout retargeted inside the roots launch unchecked.
        cwd_realpath=item.get("cwd_realpath"),
        launch_key=launch_key,
    )


# --- scheduler node routing (task #1327, I2) -----------------------------------
#
# When UA_DISPATCH_NODE_ROUTING=1 the dispatch scheduler's launch boundary asks
# here which registered node should run a governed run for an agent label. The
# contract is fail-closed: a label that HAS a registered node never falls back
# to a hub-local spawn — it errors until the node comes back or advertises the
# backend.


def select_node_for_agent(
    agent_label: str, backend: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Pick the node a dispatch run for ``agent_label`` should execute on.

    ``{"routed": False}`` means no node is registered for the label — the
    caller falls back to its ordinary (local) boundary. Otherwise either
    ``{"routed": True, "ok": True, "node", "facts"}`` for the live node with
    the freshest heartbeat that advertises ``backend`` (ties broken by node
    name), or ``{"routed": True, "ok": False, "error"}`` when the label's
    nodes are all stale or none advertise the backend.
    """
    ref = _now_dt(now)
    conn = storage.connect()
    try:
        rows = [_row(r) for r in conn.execute("SELECT * FROM nodes WHERE agent=?", (agent_label,))]
    finally:
        conn.close()
    if not rows:
        return {"routed": False}
    # Task #2181: a retired row is never a candidate. A label whose only rows
    # are retired still counts as node-routed, so it fails closed rather than
    # falling back to a hub-local spawn.
    active = [row for row in rows if not row.get("retired_at")]
    if not active:
        names = ",".join(sorted(str(row["node"]) for row in rows))
        return {"routed": True, "ok": False, "error": f"node_retired:{names}"}
    rows = active
    live: list[tuple[dict[str, Any], dict[str, Any], datetime]] = []
    for row in rows:
        # A beat from before the last retire/un-retire does not count (#2196).
        heartbeat_at = _parse_ts(node_live_beat(row))
        # A future-dated beat past the skew allowance is not live (#1997).
        if heartbeat_at is None or not heartbeat_is_live(heartbeat_at, ref):
            continue
        facts = _load(row.get("facts_json")) or {}
        live.append((row, facts, heartbeat_at))
    if not live:
        names = ",".join(sorted(str(row["node"]) for row in rows))
        return {"routed": True, "ok": False, "error": f"node_offline:{names}"}
    advertising = [
        (row, facts, hb)
        for row, facts, hb in live
        if isinstance(facts.get("backends"), list) and backend in facts["backends"]
    ]
    if not advertising:
        return {"routed": True, "ok": False, "error": f"node_backend_unavailable:{backend}"}
    if config.node_require_acceptance():
        # Task #2112: only a node that passed device acceptance takes real work.
        from . import node_acceptance

        accepted = {row["node"] for row in node_acceptance.filter_accepted([e[0] for e in live])}
        names = ",".join(sorted(str(e[0]["node"]) for e in advertising))
        advertising = [entry for entry in advertising if entry[0]["node"] in accepted]
        if not advertising:
            return {"routed": True, "ok": False, "error": f"node_not_accepted:{names}"}
    advertising.sort(key=lambda entry: (-entry[2].timestamp(), str(entry[0]["node"])))
    row, facts, _hb = advertising[0]
    return {"routed": True, "ok": True, "node": row["node"], "facts": facts}


def node_cwd_for_task(facts: dict[str, Any], worktree_path: str | None) -> str | None:
    """Choose the item cwd inside the node's reported worktree roots.

    A task worktree is used verbatim only when it is equal to or inside one of
    the node's reported roots (path-component comparison — a sibling that
    merely shares a string prefix is NOT inside); otherwise the first reported
    root is the fallback. A root the node explicitly reported as missing
    (``exists: false``) counts for neither matching nor fallback. None means
    the node reported no usable roots and the caller must fail the launch.
    """
    roots = [
        str(r["path"])
        for r in facts.get("worktree_roots") or []
        if isinstance(r, dict) and r.get("path") and r.get("exists") is not False
    ]
    if not roots:
        return None
    if worktree_path:
        wt = PurePosixPath(str(worktree_path))
        for root in roots:
            root_path = PurePosixPath(root)
            if wt == root_path or root_path in wt.parents:
                return str(worktree_path)
    return roots[0]


def settle_expired_run(conn: sqlite3.Connection, run_id: str, as_of: str) -> str | None:
    """Settle the queue item linked to a lease-expired node run.

    Runs on the caller's connection/transaction. Returns "unclaimed" when the
    item was still queued (now cancelled — the run never reached a node, so
    the caller can safely retry it) or "claimed" when the node had claimed it
    (now failed with the same hub_generated receipt ``_fail_expired`` writes —
    a lost heartbeat does not prove the backend stopped, so the outcome must
    be reconciled, never silently relaunched). None when the item is missing
    or already terminal, leaving the caller's ordinary logic in charge.

    A waiting item (task #2110) answers to its wait deadline, not to the run
    lease: "waiting" while it is still queued under a wait on a launched run
    (the run lease is renewed to the wait deadline, so a node that returns in
    time can still claim it), and "wait_unsettled" while an expired wait's
    run close is pending. The caller leaves the run alone in both cases;
    ``expire_waiting`` fails it ``waiting_expired_node_offline`` at the deadline.
    """
    item = _row(
        conn.execute(
            "SELECT * FROM node_dispatch WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    )
    if item is None:
        return None
    wait_verdict = _wait_verdict(conn, item, as_of)
    if wait_verdict == "wait_unsettled":
        return wait_verdict
    if wait_verdict == "waiting":
        run = conn.execute("SELECT state FROM dispatch_runs WHERE run_id=?", (run_id,)).fetchone()
        # A run still 'claimed' never crossed the launch boundary, so its
        # item is invisible to the node and can never be claimed: the
        # ordinary unclaimed path below hands it back.
        if run is not None and run["state"] in ("launched", "running"):
            deadline = _parse_ts(
                conn.execute(
                    "SELECT expires_at FROM node_dispatch_wait WHERE item_id=?", (item["id"],)
                ).fetchone()["expires_at"]
            )
            as_of_dt = _parse_ts(as_of)
            if deadline is not None and as_of_dt is not None and deadline > as_of_dt:
                _renew_linked_run(
                    conn,
                    run_id,
                    lease=deadline,
                    target_state=None,
                    expected_states=("launched", "running"),
                )
            return wait_verdict
    if item["status"] in TERMINAL_ITEM_STATUSES:
        return None
    if item["status"] == "queued":
        conn.execute(
            "UPDATE node_dispatch SET status='cancelled', completed_at=?, updated_at=? "
            "WHERE id=? AND status='queued'",
            (as_of, as_of, item["id"]),
        )
        return "unclaimed"
    if item["status"] == "claimed":
        _stash_revoked(
            session_credentials.revoke_for_item(conn, item["id"], "lease_expired"),
            "lease_expired",
            conn,
        )
        conn.execute(
            "UPDATE node_dispatch SET status='failed', completed_at=?, updated_at=?, "
            "receipt_json=? WHERE id=? AND status='claimed'",
            (
                as_of,
                as_of,
                json.dumps(
                    {
                        "status": "failed",
                        "reason": "lease_expired_outcome_unknown",
                        "hub_generated": True,
                    }
                ),
                item["id"],
            ),
        )
        return "claimed"
    return None


def _renew_linked_run(
    conn: sqlite3.Connection,
    run_id: Any,
    *,
    lease: datetime,
    target_state: str | None,
    expected_states: tuple[str, ...],
) -> None:
    """Best-effort: keep a node item's linked dispatch run lease in step.

    A failed transition (cancelled run, already-terminal row) must not fail
    the queue operation that carries it.
    """
    if not run_id:
        return
    try:
        from .dispatch import coordinator

        coordinator.transition_run(
            conn,
            str(run_id),
            target_state=target_state,
            expected_states=expected_states,
            lease_expires_at=_iso(lease),
            require_cancellation_unlocked=True,
        )
    except Exception:  # noqa: BLE001 - lease renewal is best-effort
        return


# --- ACP adapter acceptors (task #1328, Epic I2) --------------------------------


def _heartbeat_acp_sessions_for_item(conn: sqlite3.Connection, item_id: str, node: str) -> None:
    """Best-effort: renew open ACP and adopted-native mirror rows owned by this item.

    Not tenant-fenced (task #1995), like every ``agent_sessions`` read in this
    module: they are node control-plane paths keyed by node item or ACP session
    id, and the node queue carries no tenant. Caller-facing session reads go
    through ``registry``, which is fenced.
    """
    try:
        rows = conn.execute(
            "SELECT id, meta FROM agent_sessions "
            "WHERE (kind='acp' OR kind LIKE '%\\_native' ESCAPE '\\') "
            "AND status IN ('starting','running','waiting')"
        ).fetchall()
    except sqlite3.Error:
        return
    from . import registry

    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("item_id") == item_id and meta.get("node") == node:
            try:
                registry.heartbeat_session(row["id"])
            except Exception:  # noqa: BLE001 - heartbeat must not sink the node lease
                pass


def _acp_session_row(external_sid: str, *, any_kind: bool = False) -> dict[str, Any] | None:
    """The agent_sessions row keyed on this ACP session id, if any.

    ``any_kind`` also finds an adopted native row (task #1741): a session
    adopted from Claude Code and resumed over ACP keeps one durable row.
    """
    kind_clause = "" if any_kind else " AND kind='acp'"
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT id, status, kind, agent, meta FROM agent_sessions "
                f"WHERE external_session_id=?{kind_clause} "
                "ORDER BY started_at DESC LIMIT 1",
                (external_sid,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return _row(row)


def _project_session_settings(raw: Any, item: dict[str, Any]) -> dict[str, Any]:
    """Native observations cannot rewrite the dispatch's approved request intent."""
    harness = acp_matrix.acp_backend_name(item.get("backend"))
    observed = _redact_tree(raw) if isinstance(raw, dict) else {}
    return session_settings.reported(
        {
            **observed,
            "requested_model": _redact_tree(item.get("model")),
            "requested_mode": item.get("permission_mode") or ("supervised" if harness else None),
            "source": _redact_tree(
                item.get("settings_source")
                or ("supervised_default" if harness else "native_unreported")
            ),
        },
        harness or "",
    )


def _resume_meta(data: dict[str, Any] | None, item: dict[str, Any], node: str) -> dict[str, Any]:
    """The persisted resume binding (task #1741): what reopening this session needs."""
    data = data if isinstance(data, dict) else {}
    capability = data.get("resume_capability")
    return {
        "node": node,
        "backend": item.get("backend"),
        "cwd": item.get("cwd"),
        "resume_capability": capability if capability in ("resume", "load", "none") else None,
        "session_settings": _project_session_settings(data.get("settings"), item)
        if acp_matrix.acp_backend_name(item.get("backend"))
        or isinstance(data.get("settings"), dict)
        else None,
    }


def _open_acp_session(
    item: dict[str, Any],
    node: str,
    item_key: str,
    external_sid: str | None,
    data: dict[str, Any] | None = None,
) -> None:
    """Mirror an ACP session into agent_sessions. Best-effort, idempotent on the
    external id: a replayed session_open heartbeats the existing row instead of
    double-opening, and an already-closed row is left alone, unless this item
    was queued to resume that very session (task #1741): then the durable row
    reopens and is rebound to this item."""
    if not external_sid:
        return
    from . import node_resume, registry

    if node_resume.reopen_for_item(
        item, node, item_key, external_sid, _resume_meta(data, item, node)
    ):
        return
    existing = _acp_session_row(external_sid)
    if existing is not None:
        if existing["status"] in _OPEN_SESSION_STATUSES:
            registry.heartbeat_session(existing["id"])
        return
    # agent_sessions.task_id is a real FK (PRAGMA foreign_keys=ON): a stale or
    # synthetic task id on the item must not silently sink the mirror row.
    task_id = item.get("task_id")
    if task_id is not None:
        conn = storage.connect()
        try:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()
        if exists is None:
            task_id = None
    registry.start_session(
        agent=str(item.get("profile_agent") or item["agent"]),
        kind="acp",
        model=(_resume_meta(data, item, node).get("session_settings") or {}).get(
            "current_model_id"
        ),
        task_id=task_id,
        external_session_id=external_sid,
        meta={
            "node": node,
            "item_id": item_key,
            "acp_session_id": external_sid,
            "task_id": item.get("task_id"),
            "device_agent": item["agent"],
            "hermes_profile": item.get("hermes_profile"),
            "profile_agent": item.get("profile_agent"),
            **_resume_meta(data, item, node),
        },
    )


def _update_session_settings(
    sid: str | None, item_key: str, node: str, data: dict[str, Any], item: dict[str, Any]
) -> None:
    if not sid:
        return
    conn = storage.connect()
    try:
        row = _row(
            conn.execute(
                "SELECT id, meta FROM agent_sessions WHERE external_session_id=? "
                "AND (kind='acp' OR kind LIKE '%\\_native' ESCAPE '\\') "
                "AND json_extract(meta, '$.item_id')=? AND json_extract(meta, '$.node')=? "
                "ORDER BY started_at DESC LIMIT 1",
                (sid, item_key, node),
            ).fetchone()
        )
    finally:
        conn.close()
    if row is None:
        return
    meta = _load(row.get("meta")) or {}
    if meta.get("item_id") != item_key or meta.get("node") != node:
        return
    settings = data.get("settings")
    if not isinstance(settings, dict):
        return
    previous = meta.get("session_settings")
    already_refused = isinstance(previous, dict) and previous.get("refused") is True
    meta["session_settings"] = _project_session_settings(settings, item)
    if already_refused or data.get("refused") is True:
        meta["session_settings"]["refused"] = True
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agent_sessions SET model=?, meta=? WHERE id=? "
            "AND json_extract(meta, '$.item_id')=? AND json_extract(meta, '$.node')=?",
            (
                meta["session_settings"]["current_model_id"],
                json.dumps(meta),
                row["id"],
                item_key,
                node,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _record_item_session(item_key: str, node: str, sid: str | None) -> None:
    """Note the live ACP session id on its claimed item (task #1741 review).

    Until now ``session_id`` was written only by the receipt, so a claimed
    item never showed which session it was running. First id wins, and only
    while the item is claimed by this node. Best-effort.
    """
    if not sid:
        return
    try:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET session_id=? WHERE id=? AND node=? "
                "AND status='claimed' AND session_id IS NULL",
                (sid, item_key, node),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _close_acp_session(
    external_sid: str | None,
    stop_reason: str | None,
    *,
    item_key: str,
    node: str,
) -> None:
    """End the mirrored session when the turn ends. Best-effort.

    Bound to ``item_key``/``node`` so a fabricated ``turn_end`` cannot close
    another dispatch's agent_sessions row.
    """
    if not external_sid:
        return
    from . import registry

    row = _acp_session_row(external_sid, any_kind=True)
    if row is None or row["status"] not in _OPEN_SESSION_STATUSES:
        return
    try:
        conn = storage.connect()
        try:
            full = conn.execute(
                "SELECT meta FROM agent_sessions WHERE id=?", (row["id"],)
            ).fetchone()
        finally:
            conn.close()
        meta = json.loads((full["meta"] if full else None) or "{}")
    except (sqlite3.Error, TypeError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    if meta.get("item_id") != item_key or meta.get("node") != node:
        return
    status = "failed" if stop_reason in ("timeout", "error", "resume_unsupported") else "completed"
    # Not tenant-fenced (task #1995): the row was found by its ACP session id
    # and bound to this node item above. Node receipts carry no tenant, so a
    # fence here could only strand a mirror row open until the reaper.
    registry.end_session(row["id"], status=status, exit_status=stop_reason, tenant_scoped=False)


def _redact_tree(value: Any, depth: int = 0) -> Any:
    """Scrub credential shapes from every string in an event field (task #1744).

    Dictionary keys and values use the same shared redaction patterns. Colliding
    scrubbed keys gain deterministic ``#2``, ``#3``, ... suffixes, reserving all
    scrubbed names and preserving unchanged keys and every evidence value.
    No truncation is added beyond the existing bounds.
    """
    if isinstance(value, str):
        return redact_sensitive_content(value, max_len=len(value) + 64) if value else value
    if depth >= 12:
        # Too deep to walk: keep it, flattened and scrubbed, rather than unscrubbed.
        flat = json.dumps(value, default=str)
        return redact_sensitive_content(flat, max_len=len(flat) + 64)
    if isinstance(value, list):
        return [_redact_tree(item, depth + 1) for item in value]
    if isinstance(value, dict):
        keys = {key: _redact_tree(key) if isinstance(key, str) else key for key in value}
        reserved = set(keys.values())
        unchanged = {key for key, scrubbed in keys.items() if key == scrubbed}
        out: dict[Any, Any] = {}
        next_suffix: dict[str, int] = {}
        for key, item in value.items():
            scrubbed = keys[key]
            if scrubbed in out or (key != scrubbed and scrubbed in unchanged):
                base = scrubbed
                suffix = next_suffix.get(base, 2)
                scrubbed = f"{base}#{suffix}"
                while scrubbed in reserved or scrubbed in out:
                    suffix += 1
                    scrubbed = f"{base}#{suffix}"
                next_suffix[base] = suffix + 1
            out[scrubbed] = _redact_tree(item, depth + 1)
        return out
    return value


def _redact_text(value: Any) -> str | None:
    """A node-supplied scalar as text, scrubbed of credential shapes before any cut."""
    return None if value is None else _redact_tree(str(value))


def record_node_event(
    node: Any,
    item_id: Any,
    events_list: Any,
    *,
    identity: str,
    device_node: str | None = None,
) -> dict[str, Any]:
    """Append a batch of normalized ACP events to the evidence ledger.

    Bounded twice over — at most ``MAX_NODE_EVENTS_PER_CALL`` per call, each
    under ``MAX_NODE_EVENT_BYTES`` — and durable through ``events.record_event``.
    Unknown event kinds/variants are stored verbatim; the hub does not need to
    understand a variant to keep it in the audit trail. ``session_open`` /
    ``turn_end`` additionally mirror the ACP session's lifecycle onto
    agent_sessions, keyed by the backend-negotiated session id.

    Events are accepted only while the item is ``claimed`` by this node — a
    finished or foreign item cannot append fabricated ACP evidence.
    """
    name = validate_node_name(node)
    item_key = str(item_id or "")
    if not _ITEM_RE.fullmatch(item_key):
        raise NodeError("item_id is malformed")
    if not isinstance(events_list, list):
        raise NodeError("events must be a JSON array")
    if len(events_list) > MAX_NODE_EVENTS_PER_CALL:
        raise NodeError(f"events exceeds {MAX_NODE_EVENTS_PER_CALL} per call")
    conn = storage.connect()
    try:
        _owned_node(conn, name, identity, device_node)
        item = _row(
            conn.execute(
                "SELECT * FROM node_dispatch WHERE id=? AND node=?", (item_key, name)
            ).fetchone()
        )
        if item is None:
            raise NodeError(f"item {item_key!r} is not queued for node {name!r}", status=404)
        if item["status"] != "claimed":
            raise NodeError(
                f"item {item_key!r} is {item['status']!r}, not claimed; "
                "ACP events are only accepted for an in-flight claim",
                status=409,
            )
    finally:
        conn.close()

    recorded = 0
    dropped = 0
    for event in events_list:
        if not isinstance(event, dict):
            dropped += 1
            continue
        try:
            encoded = json.dumps(event, default=str)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if len(encoded.encode("utf-8")) > MAX_NODE_EVENT_BYTES:
            dropped += 1
            continue
        kind = _bounded_str(event.get("kind"), 80) or "unknown"
        acp_sid = _bounded_str(event.get("session_id"), 200)
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}
        # Normalized turn id (task #1740): set on turn_start/turn_end/
        # turn_aborted and on every update inside that turn. Absent from
        # events of a node that predates turn lifecycle events.
        turn_id = _bounded_str(event.get("turn_id") or data.get("turn_id"), 40)
        # The ledger copy is scrubbed of credential shapes (task #1744); the
        # control logic below keeps reading the event as received.
        _record(
            "acp_event",
            {
                "node": name,
                "item_id": item_key,
                "acp_kind": _redact_tree(kind),
                "turn_id": _redact_tree(turn_id),
                "session_id": _redact_tree(acp_sid),
                "at": _redact_tree(event.get("at")),
                "data": _redact_tree(event.get("data")),
                # Redact before the cut: a value straddling it would otherwise be
                # left too short to match the credential shape (task #1893).
                "text": _bounded_str(_redact_text(event.get("text")), 4_000),
                "tool_call": _redact_tree(event.get("tool_call")),
                "plan": _redact_tree(event.get("plan")),
                # Hub-owned queue provenance, independent of native update data.
                "hermes_profile": item.get("hermes_profile"),
                "profile_agent": item.get("profile_agent"),
                "device_agent": item["agent"],
            },
            identity,
        )
        recorded += 1
        # The backend emits session_open before the wire has an id and
        # session_ready once session/new returned one — either may carry the
        # ACP id (top-level session_id, or data.acp_session_id as a fallback).
        sid = acp_sid or _bounded_str(data.get("acp_session_id"), 200)
        if kind in ("session_open", "session_ready"):
            _record_item_session(item_key, name, sid)
            _open_acp_session(item, name, item_key, sid, data)
            # Joins the item's session credential to this ACP session and its
            # agent_sessions row (task #1744). The hub picks the credential
            # from its own rows, never from the event.
            session_credentials.bind_acp_session(item_key, name, sid)
        elif kind == "session_settings_update":
            _update_session_settings(sid, item_key, name, data, item)
        elif kind == "session_end" or (kind == "turn_end" and turn_id is None):
            # A session may now run several turns (follow-ups, task #1740), so
            # the mirror closes on session_end. A turn_end without a turn id
            # comes from a node that predates that and still ends the session.
            # An idle-reaped session (task #1741) records why it stopped; its
            # resume binding stays on the row.
            stop = data.get("end_reason") or data.get("stop_reason") or data.get("stopReason")
            _close_acp_session(sid, _bounded_str(stop, 40), item_key=item_key, node=name)
        elif kind in ("followup_delivered", "followup_dropped"):
            node_followups.apply_node_event(item_key, kind, data)
        if kind in ("turn_end", "turn_aborted") and turn_id and item.get("task_id") is not None:
            _record_turn_summary(
                item, name, item_key, _redact_tree(turn_id), sid, data, identity, kind=kind
            )
    if item["backend"] == "acceptance":
        # Device acceptance (task #2112): the hub cancels its cancel item once running.
        from . import node_acceptance

        kinds = [str(e.get("kind")) for e in events_list if isinstance(e, dict)]
        node_acceptance.on_node_events(item, kinds)
    return {"recorded": recorded, "dropped": dropped}


# Served by the partial expression index idx_event_log_node_item_turn
# (storage._NODE_TURN_INDEX_SQL, SQLite >= 3.42, task #2017); keep the json_extract text of the
# item_id/turn_id terms identical to the index or SQLite will not use it.
_TURN_EVENTS_SQL = (
    "SELECT json_extract(payload, '$.acp_kind') AS k, "
    "json_extract(payload, '$.data.status') AS status, "
    "COALESCE(json_extract(payload, '$.data.toolCallId'), "
    "json_extract(payload, '$.tool_call.toolCallId')) AS call_id "
    "FROM event_log "
    "WHERE channel=? AND json_extract(payload, '$.kind')='acp_event' "
    "AND json_extract(payload, '$.item_id')=? "
    "AND json_extract(payload, '$.turn_id')=? ORDER BY ROWID"
)


def _record_turn_summary(
    item: dict[str, Any],
    name: str,
    item_key: str,
    turn_id: str,
    session_id: str | None,
    data: dict[str, Any],
    identity: str,
    *,
    kind: str = "turn_end",
) -> None:
    """One summary per finished turn, on the task's evidence chain (task #1989).

    A turn ends in ``turn_end`` (``outcome: completed``) or ``turn_aborted``
    (``outcome: aborted`` with the ``abort_reason`` the backend gave: cancel,
    steer, agent_cancel, timeout, error), so a failed turn is summarized too.

    Streamed ACP events carry item_id but not task_id: tagging every one
    would flood the task's evidence report, which reads all matching events.
    This single event carries task_id and the dispatch's evidence_ref
    (``node:<node>:<item>``), with counts of what the turn recorded. It is
    deduplicated per turn, so a node replaying its spool adds nothing.
    Best-effort, like ``_record``: the ACP events themselves are durable, and
    a failure must not fail the node call. It is logged, not swallowed: a task
    report missing a finished turn's summary has a visible cause.
    """
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(_TURN_EVENTS_SQL, (CHANNEL_NODE, item_key, turn_id)).fetchall()
        finally:
            conn.close()
        counts = _turn_tool_counts(rows)
        stop = data.get("stop_reason") or data.get("stopReason")
        outcome: dict[str, Any] = {"outcome": "completed"}
        if kind == "turn_aborted":
            outcome = {
                "outcome": "aborted",
                "abort_reason": _bounded_str(_redact_text(data.get("reason")), 40),
            }
        events.record_event(
            CHANNEL_NODE,
            {
                "kind": "turn_summary",
                **outcome,
                "node": name,
                "item_id": item_key,
                "task_id": int(item["task_id"]),
                "evidence_ref": f"node:{name}:{item_key}",
                "turn_id": turn_id,
                "session_id": _redact_tree(session_id),
                "stop_reason": _bounded_str(_redact_text(stop), 40),
                **counts,
            },
            originator=identity,
            dedupe_key=f"turn_summary:{name}:{item_key}:{turn_id}",
        )
    except Exception:  # noqa: BLE001 - a ledger hiccup must not fail a node call
        _log.warning(
            "turn summary not recorded for item %s turn %s", item_key, turn_id, exc_info=True
        )


def _turn_tool_counts(rows: list[Any]) -> dict[str, int]:
    """Event and tool-call counts for one turn's ACP events, in arrival order.

    ``tool_calls_failed`` counts calls, not updates: each ``toolCallId`` once,
    when its latest reported status (the initial ``tool_call`` included) is
    ``failed``. A status event without a call id cannot be matched to a call
    and counts on its own.
    """
    tool_calls = updates = loose_failed = 0
    latest: dict[str, Any] = {}
    for row in rows:
        kind, status, call_id = row["k"], row["status"], row["call_id"]
        if kind == "tool_call":
            tool_calls += 1
        elif kind == "tool_call_update":
            updates += 1
        else:
            continue
        if call_id is None:
            loose_failed += status == "failed"
        elif status is not None:
            latest[str(call_id)] = status
    return {
        "events": len(rows),
        "tool_calls": tool_calls,
        "tool_call_updates": updates,
        "tool_calls_failed": loose_failed + sum(s == "failed" for s in latest.values()),
    }


def _select_permission_option(options: list[dict[str, Any]]) -> str | None:
    """Pick the option id the approved request should map to: the first
    ``allow_once`` wins (the least privilege the harness offered), else the
    first ``allow*`` option, else None → the node cancels."""
    first_allow: str | None = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind") or "")
        option_id = opt.get("optionId")
        if option_id is None:
            continue
        if kind == "allow_once":
            return str(option_id)
        if first_allow is None and kind.startswith("allow"):
            first_allow = str(option_id)
    return first_allow


def request_node_permission(
    node: Any,
    item_id: Any,
    payload: Any,
    *,
    identity: str,
    device_node: str | None = None,
) -> dict[str, Any]:
    """Hub-mediated answer for one ACP ``session/request_permission`` prompt.

    The request goes through the global approval queue exactly like a gated
    tool call: first poll creates/reuses a pending row, a human resolves it,
    a later poll returns the decision. HTTP callers always get a 200-shaped
    status dict — ``pending`` / ``approved``(+``option_id``) / ``denied`` —
    and the node maps anything that is not an approved selection to the ACP
    ``cancelled`` outcome. Fail-closed: nothing here ever auto-approves.
    """
    name = validate_node_name(node)
    item_key = str(item_id or "")
    if not _ITEM_RE.fullmatch(item_key):
        raise NodeError("item_id is malformed")
    if not isinstance(payload, dict):
        raise NodeError("permission request must be a JSON object")
    # Every node-supplied string that reaches the approvals table or the ledger
    # is scrubbed of credential shapes, before it is cut to its bound (task
    # #1744, #1893). The scrub is deterministic, so a re-polled identical prompt
    # still matches its pending row.
    session_id = _bounded_str(_redact_text(payload.get("session_id")), 200)
    if not session_id:
        raise NodeError("session_id is required")
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict) or not tool_call:
        raise NodeError("tool_call must be a JSON object")
    tool_call = _redact_tree(tool_call)
    options_raw = payload.get("options")
    if not isinstance(options_raw, list) or len(options_raw) > MAX_PERMISSION_OPTIONS:
        raise NodeError(f"options must be a JSON array of at most {MAX_PERMISSION_OPTIONS}")
    options: list[dict[str, Any]] = []
    for opt in options_raw:
        if not isinstance(opt, dict):
            raise NodeError("each option must be a JSON object")
        options.append(
            {
                "optionId": _bounded_str(_redact_text(opt.get("optionId")), 120),
                "name": _bounded_str(_redact_text(opt.get("name")), 200),
                "kind": _bounded_str(_redact_text(opt.get("kind")), 60),
            }
        )
    canonical = {
        "node": name,
        "item_id": item_key,
        "session_id": session_id,
        "tool_call": tool_call,
        "options": options,
    }
    try:
        if len(json.dumps(canonical, default=str)) > MAX_PERMISSION_BYTES:
            raise NodeError("permission request exceeds size limit")
    except (TypeError, ValueError) as exc:
        raise NodeError("permission request is not JSON-encodable") from exc
    conn = storage.connect()
    try:
        _owned_node(conn, name, identity, device_node)
        item = _row(
            conn.execute(
                "SELECT * FROM node_dispatch WHERE id=? AND node=?",
                (item_key, name),
            ).fetchone()
        )
        if item is None:
            raise NodeError(f"item {item_key!r} is not queued for node {name!r}", status=404)
        # Every backend (task #2315 review): the device owner, the dispatched
        # target and the profile identity must each be running, the same
        # checks a claim and a heartbeat make. The caller is checked first so
        # its refusal is recorded at this site.
        _require_running_identity(conn, identity, site="node_queue.permission")
        _require_native_item(conn, item)
        if item.get("hermes_profile") is not None:
            canonical.update(
                hermes_profile=item["hermes_profile"], profile_agent=item["profile_agent"]
            )
            if len(json.dumps(canonical, default=str)) > MAX_PERMISSION_BYTES:
                raise NodeError("permission request exceeds size limit")
        blocked = _permission_block(conn, item)
    finally:
        conn.close()
    if blocked is not None:
        # The turn is over or being stopped: any pending prompt is stale.
        # Deny-shaped 200 keeps the node on the cancelled path.
        return {"status": "denied", "reason": blocked}

    # Scope + revocation (same checks as check_prod_action_gate) without nesting
    # a second approval — mid-turn permission already parks in the queue below.
    if identity in config.revoked_agents():
        raise NodeError(f"agent {identity!r} is revoked from prod action 'dispatch'")
    refusal = agent_lifecycle.lifecycle_refusal(identity, site="node_queue.permission")
    if refusal is not None:
        raise NodeError(refusal["message"], status=403, code=refusal["code"])
    scopes = config.agent_scopes()
    if scopes and "dispatch" not in scopes.get(identity, frozenset()):
        raise NodeError(f"agent {identity!r} is not scoped for prod action 'dispatch'")

    from .tools import approvals as approvals_tool

    gate = approvals_tool.check_approval_gate(
        ACP_PERMISSION_TOOL, canonical, requested_by=identity, force=True, request_only=True
    )
    status = gate.get("status")
    approval_id = gate.get("approval_id")
    if status == "approved":
        # request_only reports the pass without spending it — consume it here
        # atomically so one grant answers exactly one permission prompt. The
        # item and every identity are re-read in the consuming transaction
        # (task #2315 review): a grant decided before a pause or a cancel is
        # not spent on a turn that may no longer run.
        stale: str | None = None
        consumed_rows = 0
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _row(
                conn.execute(
                    "SELECT * FROM node_dispatch WHERE id=? AND node=?", (item_key, name)
                ).fetchone()
            )
            stale = _permission_block(conn, current)
            if stale is None:
                _require_running_identity(conn, identity, site="node_queue.permission")
                _require_native_item(conn, current)
                consumed_rows = conn.execute(
                    "UPDATE approvals SET status='consumed', updated_at=? "
                    "WHERE id=? AND status='approved'",
                    (storage.now(), approval_id),
                ).rowcount
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        if stale is not None:
            return {"status": "denied", "approval_id": approval_id, "reason": stale}
        if consumed_rows != 1:
            return {"status": "pending", "approval_id": approval_id}
        _record(
            "acp_permission",
            {
                "node": name,
                "item_id": item_key,
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": "approved",
            },
            identity,
        )
        return {
            "status": "approved",
            "approval_id": approval_id,
            "option_id": _select_permission_option(options),
        }
    if status == "denied":
        _record(
            "acp_permission",
            {
                "node": name,
                "item_id": item_key,
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": "denied",
            },
            identity,
        )
        return {"status": "denied", "approval_id": approval_id, "reason": gate.get("reason")}
    return {"status": "pending", "approval_id": approval_id}


__all__ = [
    "ACP_PERMISSION_TOOL",
    "CHANNEL_NODE",
    "MAX_NODE_EVENT_BYTES",
    "MAX_NODE_EVENTS_PER_CALL",
    "NODE_BACKENDS",
    "NODE_DISPATCH_TOOL",
    "NODE_LIVE_WINDOW_S",
    "NodeError",
    "annotate_registry_sessions",
    "cancel",
    "claim_next",
    "claim_operation",
    "enqueue",
    "get_item",
    "heartbeat",
    "item_for_launch_key",
    "list_nodes",
    "list_recent_items",
    "node_cwd_for_task",
    "post_receipt",
    "record_node_event",
    "register",
    "request_dispatch",
    "restart",
    "select_node_for_agent",
    "settle_expired_run",
    "sweep_ended_followups",
    "expire_waiting",
    "node_queue_wait_s",
    "node_wait_sweep_s",
    "node_wait_reason",
    "request_node_permission",
    "validate_node_name",
]


# --- agent lifecycle stop (task #1723) ----------------------------------------


def lifecycle_stop_items(agent: str, actor: str, reason: str) -> list[dict[str, Any]]:
    """Cancel ``agent``'s queued and claimed node items for a lifecycle pause or terminate.

    Registered with ``agent_lifecycle`` as its ``node`` stop handler, so the
    lifecycle module does not import this runtime module. ``reason`` is part of
    the handler signature; :func:`cancel` takes none. One row per item; a
    failed cancel is reported as ``control='failed'``, never aborts the rest.
    """
    del reason
    conn = storage.connect()
    try:
        try:
            org = edition.tenant_org_id()
            # Unbound legacy work is private-only; otherwise require scoped task/run ownership.
            rows = conn.execute(
                "SELECT id, status FROM node_dispatch WHERE agent=? AND status IN ('queued','claimed') "
                "AND (task_id IS NULL OR task_id IN (SELECT id FROM tasks WHERE org_id IS ?)) "
                "AND (run_id IS NULL OR run_id IN (SELECT run_id FROM dispatch_runs "
                "WHERE task_id IN (SELECT id FROM tasks WHERE org_id IS ?))) "
                "AND (? IS NULL OR task_id IS NOT NULL OR run_id IS NOT NULL)",
                (agent, org, org, org),
            ).fetchall()
        except Exception:  # noqa: BLE001 - table absent on a fresh store
            rows = []
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            cancel(row["id"], actor=actor)
            detail = (
                "will not start"
                if row["status"] == "queued"
                else "relayed on the node's next heartbeat"
            )
            out.append({"path": "node", "ref": row["id"], "control": "requested", "detail": detail})
        except Exception as exc:  # noqa: BLE001 - report, never abort the fan-out
            out.append(
                {"path": "node", "ref": row["id"], "control": "failed", "detail": str(exc)[:200]}
            )
    return out


agent_lifecycle.register_stop_handler("node", lifecycle_stop_items)
