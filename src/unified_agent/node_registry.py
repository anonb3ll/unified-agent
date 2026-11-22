"""Registered ua-node devices: names, ownership and the node list.

``node_queue`` (the hub side of ``unified-agent node``) registers nodes and
runs their work queue. Host and harness views, the agent registry and
harness-config bundles only need to read who the nodes are and check that a
caller owns one, so those reads live here, below the queue.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from . import config, storage

# A node is "live" for scheduler routing while its heartbeat is fresher than
# this window — 3x the node's default 30s heartbeat interval.
NODE_LIVE_WINDOW_S = 90
# How far in the future a heartbeat may be stamped and still count as live
# (tasks #1987, #1997): enough for ordinary clock skew between a node and the
# hub, not enough for a rolled-back hub clock or a hand-edited row to keep a
# stopped node live indefinitely.
NODE_CLOCK_SKEW_S = 60
# Lease slack past an item's own timeout: a node that stops heartbeating for
# this long after its run should have ended is presumed gone. Lives here, below
# the queue, so the session-credential lifetime (task #1744) reads the same value.
LEASE_GRACE_S = 300
# Bound checkout items are refused by older ua-nodes that ignore cwd_realpath.
CWD_REALPATH_FEATURE = "cwd_realpath"

_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NodeError(ValueError):
    """A node request the hub refuses; ``status`` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        # A stable, machine-readable reason for refusals a device must tell
        # apart from a credential failure (``RETIRED_CODE``, task #2181).
        self.code = code


def parse_heartbeat(raw: Any) -> datetime | None:
    """A stored heartbeat as an aware UTC datetime; None when absent or unparseable."""
    if isinstance(raw, datetime):
        beat = raw
    elif isinstance(raw, str) and raw:
        try:
            beat = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return beat if beat.tzinfo else beat.replace(tzinfo=UTC)


def heartbeat_is_live(beat: Any, now: datetime) -> bool:
    """The one node liveness rule (tasks #1987, #1997).

    Live means no older than ``NODE_LIVE_WINDOW_S`` and no further in the
    future than ``NODE_CLOCK_SKEW_S``. Without the lower bound, a
    future-dated heartbeat counted as live for as long as the clock lagged
    it, so a stopped node stayed routable. ``beat`` is a stored timestamp
    string or a datetime; a missing or unparseable beat is not live.
    """
    parsed = parse_heartbeat(beat)
    if parsed is None:
        return False
    reference = now if now.tzinfo else now.replace(tzinfo=UTC)
    age = (reference - parsed).total_seconds()
    return -NODE_CLOCK_SKEW_S <= age <= NODE_LIVE_WINDOW_S


def effective_heartbeat(beat: Any, floor: Any) -> Any:
    """``beat``, or None when it is not after the node's ``heartbeat_floor``.

    Retire and un-retire copy the stored ``last_heartbeat`` into the floor
    (task #2196). That beat was sent before the device was refused and backed
    off, so it must not make the node live again; only a newer registration
    or heartbeat counts. ``last_heartbeat`` itself is never cleared: the
    doctor still reports when the node was last seen.
    """
    floor_at = parse_heartbeat(floor)
    if floor_at is None:
        return beat
    parsed = parse_heartbeat(beat)
    if parsed is None or parsed <= floor_at:
        return None
    return beat


def node_live_beat(row: Mapping[str, Any]) -> Any:
    """The heartbeat that counts for a node's liveness. Every liveness reader uses this.

    ``row`` is a raw ``nodes`` row (``SELECT *``, or any SELECT that includes
    ``heartbeat_floor``) or a ``read_nodes``/``list_nodes`` entry, which
    carries ``live_heartbeat`` already. Either way a beat at or before the
    floor (task #2196) is None. ``last_heartbeat`` stays the last-seen
    history for display; it must not be judged live on its own, or a reader
    disagrees with routing for the live window after ``node_unretire``.

    A row that has a beat but neither key raises ValueError (task #2192): it
    comes from a SELECT that left out ``heartbeat_floor``, and reading its
    beat unfenced would call a returned node live. A mapping with no beat at
    all (``{}`` for a node that is not listed) is None.
    """
    if "live_heartbeat" in row:
        return row["live_heartbeat"]
    if "heartbeat_floor" not in row:
        if row.get("last_heartbeat") is None:
            return None
        raise ValueError(
            "node row has last_heartbeat but no heartbeat_floor; select heartbeat_floor "
            "or pass a read_nodes entry"
        )
    return effective_heartbeat(row.get("last_heartbeat"), row.get("heartbeat_floor"))


def heartbeat_status(beat: Any, now: datetime | None = None) -> str:
    """Display status for a node-derived row: online | idle | stale | offline | unknown.

    "online" exactly when ``heartbeat_is_live`` holds, so Devices and agent
    lists never show a node online that routing calls offline (task #1997).
    ``registry.derive_status`` alone does not do that: it reads any future
    stamp as online and its online cutoff can be wider than the node window.
    A beat past the skew allowance reads offline; an older one takes the
    registry's idle/stale/offline grading, never online.
    """
    from . import registry

    parsed = parse_heartbeat(beat)
    if parsed is None:
        return "unknown"
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if heartbeat_is_live(parsed, reference):
        return "online"
    if parsed > reference:
        return "offline"
    status = registry.derive_status(parsed.isoformat(), now=reference)
    return "idle" if status == "online" else status


def node_status(row: Mapping[str, Any], now: datetime | None = None) -> str:
    """Display status for a node row or listing entry, for Devices and agent lists alike.

    ``heartbeat_status`` of ``node_live_beat``, except that a stored beat at
    or before ``heartbeat_floor`` reads "offline": after ``node_unretire`` the
    node is fenced until it beats again, which routing calls offline too.
    Devices used to read "unknown" there while the agent list read "offline"
    (task #2192).
    """
    live = node_live_beat(row)
    if live is None and row.get("last_heartbeat"):
        return "offline"
    return heartbeat_status(live, now)


def validate_node_name(node: Any) -> str:
    name = str(node or "").strip()
    if not _NODE_RE.fullmatch(name):
        raise NodeError("node must be 1-128 chars of [A-Za-z0-9._-], starting alphanumeric")
    return name


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return {k: row[k] for k in row.keys()} if row is not None else None


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _check_binding(
    row: dict[str, Any],
    identity: str,
    device_node: str | None,
    *,
    revoked: frozenset[str] | None = None,
) -> None:
    """A node answers only to its agent, and — once bound — only to its device.

    The device binding is the tailnet node the device token was issued to, or,
    when that hub has no tailnet peer, the device-key thumbprint from
    ``unified-agent login``. It stops a static bearer for the same agent label
    from draining a device-bound node's queue. A node whose agent label is
    revoked (``UA_REVOKED_AGENTS``) answers to nobody (task #2112): its
    heartbeat, claim and receipt are all
    refused, the same way a revoked label loses its session credentials.
    ``revoked`` overrides the configured set, for the acceptance probe only.
    """
    revoked_labels = config.revoked_agents() if revoked is None else revoked
    if row["agent"] in revoked_labels:
        raise NodeError(
            f"node {row['node']!r} belongs to revoked agent {row['agent']!r}", status=403
        )
    if row["agent"] != identity:
        raise NodeError(f"node {row['node']!r} is bound to a different agent", status=403)
    if config._node_hosted_or_public() and (not row.get("device_node") or not device_node):
        raise NodeError(
            "hosted nodes require a device-bound sign-in; rerun unified-agent login "
            "and re-register this device before claiming work",
            status=403,
        )
    if row.get("device_node") and row["device_node"] != device_node:
        raise NodeError(f"node {row['node']!r} is bound to a different device", status=403)
    if row.get("retired_at"):
        # Task #2181: checked only once the caller proved it owns the row, so a
        # foreign identity gets the ordinary binding refusal and learns nothing
        # about retirement. The owner is refused until an operator un-retires
        # it, so a renamed device's old name cannot come back into routing.
        raise NodeError(retired_message(row["node"]), status=403, code=RETIRED_CODE)


def _owned_node(
    conn: sqlite3.Connection, node: str, identity: str, device_node: str | None = None
) -> dict[str, Any]:
    """The ``nodes`` row ``identity`` owns, or a refusal.

    Past the binding checks, a node whose owner agent is retired answers to
    nobody (task #2315): heartbeat, claim and receipt are refused with
    ``agent_retired`` and a rate-limited ``lifecycle.refused`` event, the
    same way a retirement revoked that agent's device sign-ins. Only the
    owner learns this; a foreign identity gets the binding refusal first.
    """
    from . import agent_lifecycle

    row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (node,)).fetchone())
    if row is None:
        raise NodeError(f"node {node!r} is not registered", status=404)
    _check_binding(row, identity, device_node)
    refusal = agent_lifecycle.retired_refusal(
        row["agent"], site="node_registry.owned_node", conn=conn
    )
    if refusal is not None:
        raise NodeError(refusal, status=403, code=agent_lifecycle.REFUSAL_CODES["retired"])
    return row


def list_nodes() -> dict[str, dict[str, Any]]:
    """Active (not retired) registered nodes keyed by name, with parsed facts.

    Never raises.
    """
    try:
        return read_nodes()
    except sqlite3.Error:
        return {}


def read_nodes(*, include_retired: bool = False) -> dict[str, dict[str, Any]]:
    """``list_nodes`` that raises ``sqlite3.Error`` when the store cannot be read.

    For callers that must tell "no nodes" from "could not look", such as the
    doctor's fleet rows, which report a locked store as UNKNOWN (task #1987).
    Retired rows (task #2181) are left out unless ``include_retired``; then
    each row also carries ``retired_at``, ``retired_by`` and ``retired_reason``.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT node, agent, device_node, facts_json, version, registered_at, "
            "last_heartbeat, heartbeat_floor, retired_at, retired_by, retired_reason "
            "FROM nodes" + ("" if include_retired else " WHERE retired_at IS NULL")
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        facts = _load(row["facts_json"]) or {}
        roots_raw = facts.get("worktree_roots") or []
        roots: list[str] = []
        if isinstance(roots_raw, list):
            for entry in roots_raw:
                if isinstance(entry, str) and entry.strip():
                    roots.append(entry.strip())
                elif isinstance(entry, dict):
                    if entry.get("exists") is False:
                        continue
                    path = entry.get("path")
                    if isinstance(path, str) and path.strip():
                        roots.append(path.strip())
        out[row["node"]] = {
            "agent": row["agent"],
            "device_node": row["device_node"],
            "version": row["version"],
            "registered_at": row["registered_at"],
            "last_heartbeat": row["last_heartbeat"],
            # The beat that counts for liveness: None while ``last_heartbeat``
            # predates the last retire/un-retire (task #2196).
            "live_heartbeat": effective_heartbeat(row["last_heartbeat"], row["heartbeat_floor"]),
            "os": facts.get("os"),
            "backends": facts.get("backends") or [],
            "harnesses": facts.get("harnesses") or {},
            # Paths only — counts stay on Devices; the Sessions launcher needs cwd options.
            "worktree_roots": roots,
            "surfaces": facts.get("surfaces") if isinstance(facts.get("surfaces"), dict) else {},
            # Node-measured checkouts and harness-config state (#1736).
            "repos": _repos(facts.get("repos")),
            "harness_config": bounded_config_state(facts.get("harness_config")),
            # The node's own vault_sync doctor row (#1813), so a failing
            # client-side sync is visible from the hub.
            "vault_sync": bounded_vault_sync_state(facts.get("vault_sync")),
            # Commit the node's running code was imported from (#1946), so
            # system_doctor can flag a node running an old checkout.
            "code_sha": bounded_code_sha(facts.get("code_sha")),
            # Protocol features this ua-node build implements (#1740), e.g.
            # acp_followups; short strings only.
            "node_features": [
                f
                for f in (facts.get("node_features") or [])[:16]
                if isinstance(f, str) and 0 < len(f) <= 40
            ]
            if isinstance(facts.get("node_features"), list)
            else [],
            # Native Claude Code / Codex sessions the node discovered under its
            # roots (#1741), re-checked here against the roots it reported.
            "native_sessions": bounded_native_sessions(
                facts.get("native_sessions"), root_paths(facts)
            ),
        }
        if include_retired:
            out[row["node"]].update(
                retired_at=row["retired_at"],
                retired_by=row["retired_by"],
                retired_reason=row["retired_reason"],
            )
    return out


def read_retired_nodes() -> dict[str, dict[str, Any]]:
    """Retired node rows only (task #2181), keyed by name. Raises ``sqlite3.Error``."""
    return {
        name: info
        for name, info in read_nodes(include_retired=True).items()
        if info.get("retired_at")
    }


NATIVE_HARNESSES = ("claude", "codex")
MAX_NATIVE_SESSIONS = 50
NATIVE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def root_paths(facts: Any) -> list[str]:
    """The node-resolved realpath of every worktree root it reported (#1741 review).

    The hub cannot resolve a node's paths, so only the node's own ``realpath``
    of a root counts: a root reported without one (a ua-node that predates it)
    admits no discovered session at all.
    """
    out: list[str] = []
    raw = facts.get("worktree_roots") if isinstance(facts, dict) else None
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or entry.get("exists") is False:
            continue
        value = entry.get("realpath")
        if isinstance(value, str) and value.startswith("/") and value not in out:
            out.append(value)
    return out


def path_in_roots(path: Any, roots: list[str]) -> bool:
    """Absolute, ``..``-free, and a root or inside one (path components, not a prefix)."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096:
        return False
    candidate = PurePosixPath(path)
    if ".." in candidate.parts or "\x00" in path:
        return False
    for root in roots:
        root_path = PurePosixPath(root)
        if ".." in root_path.parts:
            continue
        if candidate == root_path or root_path in candidate.parents:
            return True
    return False


def bounded_native_sessions(raw: Any, roots: list[str]) -> list[dict[str, Any]]:
    """A node's discovered native sessions (#1741): metadata only, inside its roots.

    The node already limits discovery to its roots; the hub re-checks each
    session's node-resolved ``cwd_realpath`` against the node-resolved root
    realpaths, so a row outside them, or one without a resolved form, never
    reaches the Sessions pane. ``cwd`` in the result is that resolved path. Unknown fields are dropped: no transcript text can ride
    along.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in raw[: MAX_NATIVE_SESSIONS * 2]:
        if len(out) >= MAX_NATIVE_SESSIONS:
            break
        if not isinstance(row, dict) or row.get("harness") not in NATIVE_HARNESSES:
            continue
        sid = row.get("external_session_id")
        if not isinstance(sid, str) or not NATIVE_SESSION_ID_RE.fullmatch(sid):
            continue
        # Compared on the node-resolved forms only: a cwd whose recorded path
        # sits under a root but resolves elsewhere (a symlink) never matches.
        real = row.get("cwd_realpath")
        if (row["harness"], sid) in seen or not path_in_roots(real, roots):
            continue
        seen.add((row["harness"], sid))
        last = row.get("last_activity")
        scanned = row.get("scanned_at")
        title = row.get("title")
        out.append(
            {
                "harness": row["harness"],
                "external_session_id": sid,
                # Both are the node-resolved path, so a stored list re-bounds
                # to itself when read back.
                "cwd": real,
                "cwd_realpath": real,
                "last_activity": last if isinstance(last, str) and len(last) <= 40 else None,
                "scanned_at": scanned if isinstance(scanned, str) and len(scanned) <= 40 else None,
                "title": " ".join(title.split())[:120] if isinstance(title, str) else None,
            }
        )
    return out


# Hub feature advertised at registration: this hub stores a heartbeat
# ``code_sha`` fact (#1946). A node sends the fact on heartbeats only to a hub
# that advertises it, because an older hub treats a heartbeat carrying an
# unknown key as a full fact set and would replace the registration facts.
CODE_SHA_FEATURE = "node_code_sha"
# The hub serves /api/node/wake (task #1873); a node starts its wake loop only then.
WAKE_FEATURE = "node_wake_v3"
# The hub reads ``live_sessions`` on /api/node/wake (task #2428).
LIVE_SESSIONS_FEATURE = "node_wake_live_sessions"
_CODE_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def bounded_code_sha(raw: Any) -> str | None:
    """A node's running checkout HEAD (#1946): a full lowercase hex commit id, else None."""
    return raw if isinstance(raw, str) and _CODE_SHA.fullmatch(raw) else None


VAULT_SYNC_STATUSES = frozenset({"LIVE", "DORMANT", "MISCONFIGURED", "UNKNOWN"})
MAX_VAULT_SYNC_DETAIL = 400


def bounded_vault_sync_state(raw: Any) -> dict[str, Any] | None:
    """A node's self-reported vault_sync doctor row (#1813): status, detail, as_of."""
    if not isinstance(raw, dict) or raw.get("status") not in VAULT_SYNC_STATUSES:
        return None
    detail = raw.get("detail")
    as_of = raw.get("as_of")
    return {
        "status": raw["status"],
        "detail": detail[:MAX_VAULT_SYNC_DETAIL] if isinstance(detail, str) else "",
        "as_of": as_of if isinstance(as_of, str) and len(as_of) <= 40 else None,
    }


def bounded_config_state(raw: Any) -> dict[str, Any] | None:
    """A node's harness-config state (#1736): version, in-sync surfaces, skills.

    ``skills`` counts only with ``skill_surfaces``: the in-sync, skill-bearing
    surfaces that deliver them, each of which must also be in ``in_sync``. A
    report of published skills with no matching applied surface is dropped, so
    a team plan never treats a bare skill list as delivered. The node still
    self-reports; the hub cannot read its disk.
    """
    if not isinstance(raw, dict):
        return None
    version = raw.get("bundle_version")
    if not isinstance(version, str) or not version or len(version) > 64:
        return None

    def names(key: str, limit: int) -> list[str]:
        values = raw.get(key)
        if not isinstance(values, list):
            return []
        return [v for v in values if isinstance(v, str) and 0 < len(v) <= 64][:limit]

    in_sync = names("in_sync", 32)
    skill_surfaces = [name for name in names("skill_surfaces", 32) if name in in_sync]
    return {
        "bundle_version": version,
        "in_sync": in_sync,
        "skill_surfaces": skill_surfaces,
        "skills": names("skills", 64) if skill_surfaces else [],
    }


_REPO_FIELDS = ("root", "root_realpath", "name", "path", "realpath")


def _repos(raw: Any) -> list[dict[str, str]]:
    """Checkouts a node reported under its roots; malformed rows are dropped."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for row in raw[:64]:
        if isinstance(row, dict) and all(
            isinstance(row.get(field), str) and row[field] for field in _REPO_FIELDS
        ):
            out.append({field: row[field] for field in _REPO_FIELDS})
    return out


# --- operator retire (task #2181) ----------------------------------------------
#
# A device that re-registers under a new hostname leaves its old ``nodes`` row
# behind forever: offline, still named by the doctor's fleet rows and still a
# candidate in the routing scan. ``node_retire`` is the operator path that
# takes such a row out of service without deleting it.
#
# * Authority: a lifecycle operator (``UA_LIFECYCLE_OPERATORS``; unset, the
#   operator label and its aliases), the same set ``task_claim_release`` and
#   ``agent_terminate`` use. A verified bearer identity is required; the node's
#   own agent is not enough.
# * Effect: ``retired_at``/``retired_by``/``retired_reason`` are stamped and one
#   ``agent.node`` ledger event (``kind="retire"``) is written in the same
#   transaction. The row, its facts and its history stay.
# * A retired row is left out of ``list_nodes``/``read_nodes`` (the doctor's
#   fleet rows, the Devices/fleet listing, team and resume lookups) and out of
#   routing (``node_queue.select_node_for_agent`` and a direct enqueue).
# * Re-registration and heartbeats from the owning device are refused (HTTP
#   403, ``code="node_retired"``) while retired: the safe choice, so a retired
#   name cannot quietly come back into routing. An operator brings it back with
#   ``node_unretire``. Ownership is checked first, so another identity gets
#   the ordinary binding refusal and learns nothing about retirement.
# * A node with queued or claimed work is refused (409): cancel that first, so
#   no receipt is stranded behind the refusal above.

NODE_LIFECYCLE_CHANNEL = "agent.node"
#: ``code`` on a retired-node refusal; the device side keys on it (``node_agent``).
RETIRED_CODE = "node_retired"
MAX_RETIRE_REASON = 500


def retired_message(node: str) -> str:
    return (
        f"node {node!r} was retired by an operator; it registers and takes work again "
        "only after an operator runs coordination_manage action node_unretire, or "
        "re-register this device under a new node name"
    )


def _retire_operator(verb: str) -> str:
    from mcp.server.mcpserver.exceptions import ToolError

    from . import agent_lifecycle
    from .tool_errors import PermissionDeniedError

    who = config.verified_agent_identity()
    if not who:
        raise ToolError(f"{verb} a node requires an authenticated identity")
    if who not in agent_lifecycle.operators():
        raise PermissionDeniedError(
            f"{who!r} may not {verb.lower()} a node: that is an operator action "
            "(UA_LIFECYCLE_OPERATORS)"
        )
    return who


def _retire_reason(reason: Any) -> str:
    from mcp.server.mcpserver.exceptions import ToolError

    text = " ".join(str(reason or "").split())
    if not text:
        raise ToolError("a reason is required")
    if len(text) > MAX_RETIRE_REASON:
        raise ToolError(f"reason must be at most {MAX_RETIRE_REASON} characters")
    return text


def _set_retired(node: Any, reason: Any, *, retire: bool) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    from . import events
    from .tool_errors import ConflictError

    verb = "Retire" if retire else "Un-retire"
    who = _retire_operator(verb)
    try:
        name = validate_node_name(node)
    except NodeError as exc:
        raise ToolError(str(exc)) from None
    text = _retire_reason(reason)
    conn = storage.connect()
    publish: dict[str, Any] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (name,)).fetchone())
        if row is None:
            raise ToolError(f"node {name!r} is not registered")
        already = bool(row.get("retired_at")) == retire
        if already:
            conn.rollback()
            return {
                "node": name,
                "retired": retire,
                "changed": False,
                "retired_at": row.get("retired_at"),
                "retired_by": row.get("retired_by"),
                "retired_reason": row.get("retired_reason"),
            }
        if retire:
            open_items = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM node_dispatch WHERE node=? AND status IN ('queued','claimed') "
                    "ORDER BY created_at LIMIT 20",
                    (name,),
                ).fetchall()
            ]
            if open_items:
                raise ConflictError(
                    f"node {name!r} has queued or claimed work ({', '.join(open_items)}); "
                    "cancel it before retiring the node",
                    node=name,
                )
        ts = storage.now()
        if retire:
            conn.execute(
                "UPDATE nodes SET retired_at=?, retired_by=?, retired_reason=?, "
                "heartbeat_floor=last_heartbeat, updated_at=? WHERE node=?",
                (ts, who, text, ts, name),
            )
        else:
            # Task #2196: the stored beat predates the refusal and the device's
            # long backoff. It becomes the floor rather than being cleared, so
            # routing waits for a newer one and the doctor keeps the history.
            # The floor is that beat, not the clock: both are second-precision,
            # so a clock floor would also ignore a registration in the same second.
            conn.execute(
                "UPDATE nodes SET retired_at=NULL, retired_by=NULL, retired_reason=NULL, "
                "heartbeat_floor=last_heartbeat, updated_at=? WHERE node=?",
                (ts, name),
            )
        payload = {
            "kind": "retire" if retire else "unretire",
            "node": name,
            "agent": row["agent"],
            "actor": who,
            "reason": text,
            "last_heartbeat": row.get("last_heartbeat"),
            "at": ts,
        }
        ulid, _won, publish = events.record_event_once_on_connection(
            conn, NODE_LIFECYCLE_CHANNEL, payload=payload, originator=who
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    if publish is not None:
        events.publish(publish)
    return {
        "node": name,
        "retired": retire,
        "changed": True,
        "retired_at": ts if retire else None,
        "retired_by": who if retire else None,
        "retired_reason": text if retire else None,
        "event_ulid": ulid,
    }


def node_retire(node: str, reason: str) -> dict[str, Any]:
    """Retire a registered ua-node row; lifecycle operators only (task #2181).

    The row is kept and marked retired, with one ``agent.node`` ledger event
    (``kind="retire"``). A retired node is left out of fleet rows, the node
    listing and routing, and its re-registration and heartbeats are refused
    until ``node_unretire``. Refused (409) while the node has queued or claimed
    work. Retiring an already retired node changes nothing (``changed=False``).
    """
    return _set_retired(node, reason, retire=True)


def node_unretire(node: str, reason: str) -> dict[str, Any]:
    """Return a retired ua-node row to service; lifecycle operators only (task #2181).

    Clears the retire marker and records ``kind="unretire"`` on ``agent.node``.
    The device must then register again: the heartbeat stored from before
    retirement is kept but no longer counts as live (task #2196), so routing
    waits for that registration or a fresh heartbeat. An active node changes
    nothing.
    """
    return _set_retired(node, reason, retire=False)
