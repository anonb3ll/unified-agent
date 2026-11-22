"""Read-only JSON payloads for the control center.

Aggregate panes still use `server.REGISTERED_TOOLS`, the same
`_guarded_and_instrumented` callables the MCP loop registers. That is deliberate: it
guarantees the UI traverses the identical policy gate and perf instrumentation as an
MCP client, and it avoids the drift failure mode the TUI has today, where
`_local_tool_registry()` is fifteen hand-maintained name→function pairs that nothing
keeps in sync with the real registration list.

The task, approval, and session rows also carry links to the platform object
registry. Detail reads therefore use `/api/platform/objects/{kind}/{id}` rather
than inventing pane-specific object identities.

Known and accepted cost: UI page loads therefore write `agent_perf_samples` rows and
show up in `perf_summary`. Policy consistency is worth more than a clean perf table;
the 10s pane poll keeps the volume in the same order as one active agent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import os
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio

from .. import (
    config,
    edition,
    host_registry,
    monitor,
    needs_operator,
    object_model,
    platform_http,
    registry,
    storage,
    zulip_worker,
)
from .. import events as event_log
from ..capability_verification import build_verification
from ..dispatch import routing
from ..executor import evidence
from ..messaging import wake as message_wake
from ..telemetry import projection as telemetry_projection
from ..telemetry.redaction import redact_sensitive_content, redacted_preview
from ..tools import zulip as zulip_tools
from ..tools.worktree_policy import cached_survey_worktrees, survey_worktrees
from . import project_registry
from .contracts import DispatchRequest, DispatchStackRequest, dispatch_spec
from .spa import REPO_ROOT

# Attention kinds with a design (Codex, "Unified Agent Operations Streamlining
# Context", 2026-08-09, approved) but no existing structured data source yet.
# Listed in attention_payload()'s `omitted` so a clean scan reads as "nothing
# checked" rather than silently passing as "nothing found".
_ATTENTION_OMITTED = (
    {
        "kind": "roadmap_conflict",
        "reason": "no existing structured data source for roadmap/plan conflicts; needs design",
    },
    {
        "kind": "migration_gate",
        "reason": (
            "single-hub migration gate state (live soak window, external "
            "executor-auth verification) has no dedicated status field yet"
        ),
    },
)

# Cap idle/stale worktree rows in the Attention pane so they cannot bury
# operator decision items (defect list P2). Overflow is informational only —
# never auto-deleted; point operators at cleanup_stale_worktrees(dry_run).
_STALE_WORKTREE_ATTENTION_CAP = 5

# system_doctor shells out to systemctl several times, so it is far too slow for a
# 10s pane poll. Cached briefly; the UI shows a "checked Ns ago" stamp rather than
# pretending it is live.
_DOCTOR_CACHE_S = 30.0
# (reading stamp, result, start generation of the call that produced it).
_doctor_cache: tuple[float, dict, int] | None = None
# Single-flight across event loops and threads (task #1897). All state below is
# process-wide and guarded by one threading lock, held only for assignments,
# never across an await. Each doctor call gets a start generation when it
# starts, under that lock:
#
# - A poll shares whatever call is in flight.
# - A forced request needs a call that starts after it arrived, so it never
#   uses a completed reading either. It joins the queued forced call if there
#   is one (it has not started yet); otherwise, with a call in flight, it
#   queues one; otherwise it starts one. Forced requests that arrive before the
#   queued call starts share it, so such a burst costs at most two calls; a
#   request arriving after it started queues the next one, so a continuing
#   stream of forced requests costs one call per start, not two in all.
# - When the call in flight finishes it promotes the queued call in the same
#   lock section, so no request can slip into a gap between the two.
# - A call runs as its own task, so a request that is cancelled (a closed tab)
#   never fails the requests sharing that call. Waiters await a
#   concurrent.futures.Future (safe from any loop) and hold no worker thread.
# - A request's wait is bounded by _DOCTOR_WAIT_S (then it gets the last
#   reading with an error, or TimeoutError when there is none), and a queued
#   call stops waiting for a stalled predecessor after half of it and starts
#   anyway. This bounds waiting, not the probes: a call that hangs keeps its
#   worker thread until the tool's own timeouts end it.
_DOCTOR_WAIT_S = 300.0  # at most the doctor's own RUN_WAIT_TIMEOUT_S
_doctor_state_lock = threading.Lock()
_doctor_generation = 0


class _DoctorCall:
    """One system_doctor call made by the web layer, shared by its waiters."""

    def __init__(self, *, forced: bool) -> None:
        self.forced = forced
        # Start generation, assigned (under the lock) when the call starts.
        self.generation: int | None = None
        # Resolves to (reading stamp, result), or the call's exception.
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        # Set when a queued call is promoted to run.
        self.promoted: concurrent.futures.Future = concurrent.futures.Future()


_doctor_inflight: _DoctorCall | None = None
_doctor_pending_forced: _DoctorCall | None = None
# Strong references to running call tasks (the event loop keeps only weak ones).
_doctor_tasks: set[asyncio.Task] = set()
# gcloud_inventory shells out to the gcloud CLI (30s timeout per section), so it
# is far too slow for a pane poll. Cached longer than the doctor check, and the
# payload always carries `checked_at` / `cached_age_s` so the UI can stamp how
# old the reading is rather than implying it is live.
_CLOUD_CACHE_S = 120.0
_cloud_cache: tuple[float, tuple[str, str], dict] | None = None
_CLAIMED_BY_RE = re.compile(r"claimed-by:\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)
ZULIP_INBOUND_LOOKBACK_HOURS = 72
ZULIP_INBOUND_UI_LIMIT = 30
# Attention scan + 24h stats need a wider window than the recent-message table.
ZULIP_INBOUND_SCAN_LIMIT = 500
_ZULIP_PROTOCOL_CHANNELS = frozenset({"requests", "coord", "review"})
_TASK_TOPIC_RE = re.compile(r"^task-(\d+)\b", re.IGNORECASE)

# Operator panes (Action Deck picker, Queue & Health) must see live work.
# ``task_list(status="all")`` defaults to limit 100 and sorts priority→age, so
# done history fills the window and open tasks never reach desktop_open
# (task #685). Query each active status separately, and follow ``next_cursor``
# until the page is no longer ``truncated`` (task #1430) so a queue larger than
# one page is read whole rather than silently cut at the tool's 500-row cap.
_ACTIVE_QUEUE_STATUSES = ("open", "doing", "waiting_review", "blocked")
_ACTIVE_QUEUE_LIMIT = 500
# Pages followed per read before the pane reports itself incomplete instead of
# looping; 200 pages of 500 is far beyond any real queue.
_TASK_LIST_MAX_PAGES = 200
_COMPLETED_WINDOW_LIMIT = 100_000

# Kanban board (task #728): exactly the six task statuses. Backlog IS open —
# there is no seventh "backlog" column. Done is a rolling 30-day window;
# cancelled is present in the column list but hidden by default.
BOARD_STATUSES = ("open", "doing", "waiting_review", "blocked", "done", "cancelled")
BOARD_COLUMN_LABELS = {
    "open": "Backlog",
    "doing": "Doing",
    "waiting_review": "Waiting review",
    "blocked": "Blocked",
    "done": "Done",
    "cancelled": "Cancelled",
}
BOARD_DONE_WINDOW_DAYS = 30
BOARD_AGING_OPEN_DAYS = 14
BOARD_AGING_DOING_HOURS = 48
_BOARD_UNBLOCK_RE = re.compile(r"unblock-condition:\s*([^\n]+)", re.IGNORECASE)
_BOARD_BLOCKED_RE = re.compile(r"(?:blocked|blocker):\s*([^\n]+)", re.IGNORECASE)


def _board_now() -> datetime:
    """Clock seam for board aging thresholds (tests stub this)."""
    return datetime.now(UTC)


def _task_owner(task: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return structured ownership, or an evidenced notes fallback."""
    if task.get("claimed_by"):
        return task["claimed_by"], "structured_claimed_by"
    matches = _CLAIMED_BY_RE.findall(task.get("notes") or "")
    if matches:
        return matches[-1], "notes_claimed_by"
    return None, None


def _platform_ref(kind: str, object_id: Any) -> dict[str, str] | None:
    """Return the registry identity for a related object without exposing data."""
    if object_id is None:
        return None
    try:
        object_type = object_model.by_kind(kind)
        return {"kind": kind, "id": str(object_id), "uri": object_type.render(object_id)}
    except (KeyError, TypeError, ValueError):
        return None


def _task_object_links(tasks: list[dict[str, Any]]) -> None:
    """Attach stable platform links while keeping the queue payload compact."""
    task_ids = {str(task["id"]) for task in tasks if task.get("id") is not None}
    if not task_ids:
        return

    for task in tasks:
        task_id = task.get("id")
        owner, _ = _task_owner(task)
        task["platform"] = _platform_ref("task", task_id)
        task["links"] = {
            "agent": _platform_ref("agent", owner),
            "run": None,
            "approvals": [],
        }

    latest_runs: dict[str, str] = {}
    approvals_by_task: dict[str, list[dict[str, str]]] = {}
    try:
        conn = storage.connect()
        try:
            for row in conn.execute(
                "SELECT task_id, run_id FROM dispatch_runs "
                "WHERE task_id IN ({}) ORDER BY created_at DESC".format(
                    ",".join("?" for _ in task_ids)
                ),
                tuple(task_ids),
            ):
                latest_runs.setdefault(str(row["task_id"]), str(row["run_id"]))
            for row in conn.execute(
                "SELECT id, payload FROM approvals WHERE org_id IS ? ORDER BY id DESC",
                (edition.tenant_org_id(),),
            ):
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    continue
                task_id = payload.get("task_id") if isinstance(payload, dict) else None
                if str(task_id) in task_ids:
                    ref = _platform_ref("approval", row["id"])
                    if ref is not None:
                        approvals_by_task.setdefault(str(task_id), []).append(ref)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - links are additive to a healthy queue read
        return

    for task in tasks:
        task_id = task.get("id")
        key = str(task_id) if task_id is not None else ""
        task["links"]["run"] = _platform_ref("run", latest_runs.get(key))
        task["links"]["approvals"] = approvals_by_task.get(key, [])


def _approval_platform_row(row: dict[str, Any]) -> dict[str, Any]:
    public = _public_approval_row(row)
    public["platform"] = _platform_ref("approval", row.get("id"))
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    task_id = payload.get("task_id") if isinstance(payload, dict) else row.get("task_id")
    public["links"] = {"task": _platform_ref("task", task_id)}
    return public


async def _read_approval_platform_rows(
    buckets: list[list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """Use the platform reader for approval detail, preserving queue metadata.

    One batch read for every row of every bucket (task #1786). The per-row form
    called ``platform_http.read_object`` once per row, and each call opened its
    own storage connection: a 56-row queue cost 59 connects per poll, which a
    fast-polling browser tab turned into ~300% hub CPU. ``read_objects`` runs
    the same access, profile and reader gates once and reads every row on one
    connection.

    Per-row semantics are unchanged. A row the reader would not return (a
    malformed, missing or stale id) keeps its queue form, and a gate failure
    keeps every row in its queue form, exactly as when each per-row read failed.
    """
    ids = [str(row["id"]) for bucket in buckets for row in bucket if row.get("id") is not None]
    found: dict[str, Any] = {}
    if ids:
        try:
            found = await anyio.to_thread.run_sync(
                lambda: platform_http.read_objects("approval", ids)
            )
        except Exception:  # noqa: BLE001 - aggregate queue remains available on a stale row
            found = {}

    def merge(row: dict[str, Any]) -> dict[str, Any]:
        approval_id = row.get("id")
        platform_payload = found.get(str(approval_id)) if approval_id is not None else None
        approval = platform_payload.get("approval") if isinstance(platform_payload, dict) else None
        if not isinstance(approval, dict):
            return _approval_platform_row(row)
        return _approval_platform_row({**row, **approval})

    return [[merge(row) for row in bucket] for bucket in buckets]


def _session_platform_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["platform"] = _platform_ref("session", row.get("id"))
    out["links"] = {
        "agent": _platform_ref("agent", row.get("agent")),
        "task": _platform_ref("task", row.get("task_id")),
        "delegation": _platform_ref("delegation", row.get("delegation_id")),
    }
    return out


def registered_tool_names() -> frozenset[str]:
    """Every tool name the hub registers (``server.REGISTERED_TOOLS``)."""
    from .. import server

    return frozenset(server.REGISTERED_TOOLS)


async def call_tool(name: str, /, **kwargs: Any) -> Any:
    """Invoke a registered MCP tool off the event loop.

    Every tool function is synchronous and hits SQLite. Calling one inline would
    block the event loop for every concurrent SSE stream, so they run in a worker
    thread.
    """
    from .. import server

    fn = server.REGISTERED_TOOLS.get(name)
    if fn is None:
        raise KeyError(f"tool not registered: {name}")

    def _run() -> Any:
        return fn(**kwargs)

    return await anyio.to_thread.run_sync(_run)


async def append_evidence_event(
    *,
    channel: str,
    payload: dict[str, Any],
    originator: str,
    dedupe_key: str | None = None,
) -> tuple[str, bool]:
    """Append an evidence event through the platform object's governed writer.

    The HTTP adapter enforces the same object profile gate used by MCP resource
    readers, the global agent-access switch, the policy engine, and the approval
    gate before delegating to the registry's writer. The originator is always
    the identity bound by hub auth middleware.
    """
    identity = config.verified_agent_identity()
    if not identity or identity != originator:
        raise PermissionError("evidence append requires the verified request identity")

    def _append() -> tuple[str, bool]:
        return _registered_platform_write(
            "evidence_event",
            "append",
            actor=identity,
            operation="evidence_event.append",
            production=True,
            channel=channel,
            payload=payload,
            originator=identity,
            dedupe_key=dedupe_key,
        )

    return await anyio.to_thread.run_sync(_append)


async def resolve_platform_approval(
    *, approval_id: int, decision: str, actor: str, reason: str = ""
) -> dict[str, Any]:
    """Resolve an approval through its registered platform writer.

    The HTTP operator recovery path is the one intentional access-mode
    exception: a verified human may resolve approvals while agent access is
    disabled. Profile, policy, and approval gates still run before the writer.
    """
    return await anyio.to_thread.run_sync(
        lambda: _registered_platform_write(
            "approval",
            "resolve",
            actor=actor,
            operation="approval_resolve",
            production=True,
            human_recovery=True,
            id=approval_id,
            decision=decision,
            resolved_by=actor,
            reason=reason,
        )
    )


def _registered_platform_write(
    kind: str,
    action: str,
    *,
    actor: str,
    operation: str,
    production: bool,
    human_recovery: bool = False,
    **kwargs: Any,
) -> Any:
    """Apply the HTTP-side object/profile/access/policy gates to one writer."""
    from .. import agent_access, gateway, profiles, safety
    from ..tools import approvals

    object_type = object_model.by_kind(kind)
    profile = profiles.current()
    if not profiles.tool_is_allowed_for(profile, object_type.profile_tool):
        from mcp.server.mcpserver.exceptions import ResourceError

        from ..platform_readers import PROFILE_DENIED_MESSAGE

        raise ResourceError(PROFILE_DENIED_MESSAGE.format(profile=profile))
    agent_access.enforce(
        operation,
        read_only=False,
        production=production,
        human_recovery=human_recovery,
    )
    gate_payload = dict(kwargs)
    # One invocation, one pass: when a policy `require_approval` rule already
    # consumed an approval for this operation, the generic UA_APPROVAL_TOOLS
    # gate must not queue a second request for the same write (the policy
    # gate's payload carries action/agent, so the two never match by key).
    # A failure before the writer starts restores the consumed pass.
    with gateway.invocation(operation), approvals.approval_invocation() as receipts:
        safety.check_policy_gate(operation, gate_payload, originator=actor)
        policy_consumed = any(
            isinstance(receipt, dict) and receipt.get("approval_id") is not None
            for receipt in receipts.values()
        )
        if not policy_consumed:
            approvals.check_approval_gate(operation, gate_payload, requested_by=actor)
        approvals.mark_governed_execution()
        return object_type.writer(action)(**kwargs)


async def _safe(name: str, /, default: Any = None, **kwargs: Any) -> Any:
    """Call a tool, degrading to `default` on failure while keeping the failure visible.

    A dashboard that 500s because one of eight panes had a bad day is useless precisely
    when you opened it to find out what was wrong. But a pane that silently renders its
    empty default is worse than useless: "no anomalies" and "the anomaly check crashed"
    look identical, and the operator reads a false all-clear.

    So the default supplies the *shape* the renderer needs, and an `error` key is always
    merged in alongside it.
    """
    try:
        return await call_tool(name, **kwargs)
    except Exception as exc:  # noqa: BLE001 - reported in-band, never swallowed
        message = f"{name} failed: {str(exc)[:200]}"
        if isinstance(default, dict):
            return {**default, "error": message}
        return {"error": message}


async def _task_list_all(**kwargs: Any) -> dict:
    """``task_list`` read to the end by following ``next_cursor`` (task #1430).

    Same shape as one ``_safe("task_list", ...)`` call: ``{tasks, error?}``.
    A read that stops early, because a page failed or the page guard tripped,
    says so in ``error`` and ``truncated`` rather than passing for complete.
    """
    kwargs.setdefault("limit", _ACTIVE_QUEUE_LIMIT)
    tasks: list[dict] = []
    cursor = None
    for _ in range(_TASK_LIST_MAX_PAGES):
        page = await _safe(
            "task_list", default={"tasks": []}, **kwargs, **({"cursor": cursor} if cursor else {})
        )
        if not isinstance(page, dict):
            return {"tasks": tasks, "truncated": True, "error": "task_list returned no page"}
        tasks.extend(page.get("tasks") or [])
        if page.get("error"):
            return {"tasks": tasks, "truncated": True, "error": page["error"]}
        if not page.get("truncated"):
            return {"tasks": tasks, "truncated": False}
        cursor = page.get("next_cursor")
        if not cursor:
            # More rows exist but there is no way to reach them: report the
            # prefix as partial rather than as the whole queue (Devin, #814).
            return {
                "tasks": tasks,
                "truncated": True,
                "error": "task_list reported more rows but returned no next_cursor",
            }
    return {
        "tasks": tasks,
        "truncated": True,
        "error": f"task_list read stopped after {_TASK_LIST_MAX_PAGES} pages",
    }


async def agents_payload() -> dict:
    from ..tools.registry import select_fleet_agents

    # Same roster-or-node qualification as agent_list. Raw list_agents keeps
    # attribution strings for history; this page's counts must not.
    agents = await anyio.to_thread.run_sync(select_fleet_agents)
    sessions = await sessions_payload(limit=200)
    doing_tasks_res = await _task_list_all(status="doing", fields="compact")
    doing_tasks = doing_tasks_res.get("tasks", []) if isinstance(doing_tasks_res, dict) else []

    active_tasks_by_agent: dict[str, int] = {}
    for t in doing_tasks:
        owner = t.get("claimed_by")
        if owner:
            active_tasks_by_agent[owner] = active_tasks_by_agent.get(owner, 0) + 1

    open_sessions_by_agent: dict[str, int] = {}
    for s in sessions.get("sessions", []):
        if s.get("status") in ("starting", "running", "waiting"):
            ag = s.get("agent")
            if ag:
                open_sessions_by_agent[ag] = open_sessions_by_agent.get(ag, 0) + 1

    configured_tokens = set(config.agent_tokens().keys())
    configured_labels = set(config.agent_labels())

    counts: dict[str, int] = {}
    for agent in agents:
        lbl = agent.get("label", "")
        agent["open_sessions"] = open_sessions_by_agent.get(lbl, 0)
        agent["active_tasks"] = active_tasks_by_agent.get(lbl, 0)
        agent["is_token_user"] = lbl in configured_tokens
        agent["is_delegate"] = lbl in configured_labels
        counts[agent["status"]] = counts.get(agent["status"], 0) + 1

    out = {"agents": agents, "counts": counts}
    if isinstance(doing_tasks_res, dict) and doing_tasks_res.get("error"):
        # Active-task counts come from a partial doing read (#814).
        out["error"] = f"doing tasks: {doing_tasks_res['error']}"
        out["truncated"] = True
    return out


async def org_payload() -> dict:
    """Project the checked-in org onto live heartbeat and active task state."""
    from .. import org_registry

    offices = await anyio.to_thread.run_sync(org_registry.load_registry)
    agents = await anyio.to_thread.run_sync(registry.list_agents)
    tasks, queue_error = await _active_queue_tasks()
    exact_counts = None
    if len(tasks) >= _ACTIVE_QUEUE_LIMIT:
        # task_list caps each status at 500. Once the combined active queue
        # reaches that size, derive office totals from the full local ledger.
        try:
            exact_counts = await anyio.to_thread.run_sync(_org_exact_open_counts, offices)
        except Exception as exc:  # noqa: BLE001 - show an incomplete count explicitly
            queue_error = f"{queue_error + '; ' if queue_error else ''}org count unavailable: {exc}"
    from .. import personas

    live_agents = {agent["label"]: agent for agent in agents}
    rows = []
    for office in offices:
        epic_ids = set(office["owned_epics"])
        members = set(office["members"])
        open_tasks = sum(
            1
            for task in tasks
            if epic_ids.intersection([task.get("id"), *(task.get("lineage") or [])])
            or task.get("claimed_by") in members
        )
        if exact_counts is not None:
            open_tasks = exact_counts[office["id"]]
        member_rows = []
        for label in office["members"]:
            live = live_agents.get(label, {})
            persona = live.get("persona") or personas.get_persona(label)
            member_rows.append(
                {
                    "label": label,
                    "status": live.get("status", "offline"),
                    "last_seen": live.get("last_seen"),
                    "profile": office["default_profiles"].get(label),
                    "display_name": persona.get("display_name", label),
                    "avatar": persona.get("avatar", "🤖"),
                    "color": persona.get("color", "#64748b"),
                    "colour": persona.get("colour", persona.get("color", "#64748b")),
                    "bio": persona.get("bio", ""),
                    "model_routing_preference": persona.get("model_routing_preference"),
                    "persona": persona,
                }
            )
        rows.append(
            {
                **office,
                "members": member_rows,
                "open_tasks": open_tasks,
            }
        )
    return {"offices": rows, "as_of": storage.now(), "error": queue_error}


def personas_payload() -> dict[str, Any]:
    """All known agent personas for Control Center presentation."""
    from .. import personas

    return {"personas": personas.all_personas()}


def _org_exact_open_counts(offices: list[dict]) -> dict[str, int]:
    """Count every active task when task_list's bounded window is insufficient."""
    conn = storage.connect()
    try:
        # Same tenant fence as task_list (task #1814).
        rows = conn.execute(
            "SELECT id, parent_id, claimed_by, status, archived_at FROM tasks WHERE org_id IS ?",
            (edition.tenant_org_id(),),
        ).fetchall()
    finally:
        conn.close()
    parents = {row["id"]: row["parent_id"] for row in rows}
    counts = {office["id"]: 0 for office in offices}
    active = {"open", "doing", "waiting_review", "blocked"}
    for row in rows:
        if row["status"] not in active or row["archived_at"] is not None:
            continue
        ancestry = {row["id"]}
        ancestor = row["parent_id"]
        while ancestor is not None and ancestor not in ancestry:
            ancestry.add(ancestor)
            ancestor = parents.get(ancestor)
        for office in offices:
            if (
                ancestry.intersection(office["owned_epics"])
                or row["claimed_by"] in office["members"]
            ):
                counts[office["id"]] += 1
    return counts


def dispatch_routes_payload() -> dict[str, Any]:
    """Expose native route aliases and preflight state without launch authority."""
    scheduler_state = routing.scheduler_runtime_state()
    return {
        "routes": routing.route_inventory(scheduler_state=scheduler_state),
        "scheduler": scheduler_state,
        "resolution_states": {
            "unknown": "unknown_label",
            "ambiguous": "ambiguous_label",
            "not_dispatchable": "not_dispatchable",
            "malformed": "malformed_route",
        },
        # Env values with canonical fleet seeds filling unset keys so the
        # Dispatch Studio selector lands pre-populated (task #1153).
        "model_defaults": config.model_defaults_for_selector(),
        "model_options": config.model_selector_options(),
    }


def _never_connected_agent(label: str) -> dict:
    """The registry row shape for a known label with no sighting (task #2523)."""
    from .. import personas

    return personas.enrich_agent(
        {
            "label": label,
            "kind": "unknown",
            "host": None,
            "known": True,
            "first_seen": None,
            "last_seen": None,
            "last_seen_via": None,
            "pid": None,
            "version": None,
            "status": "never_connected",
            "meta": {},
        }
    )


async def agent_detail(label: str) -> dict | None:
    """One agent's registry row, sessions and activity, plus its seat (task #2523).

    ``connection`` is ``connected`` once the hub has recorded a sighting of the
    label (``registry.touch_agent``), else ``never_connected``. A label with no
    sighting is still answered when the hub's tenant knows it (a seat, the org
    chart, the configured roster or a token row: ``agent_enrollment.seat_for``);
    any other label is None (404).
    """
    from .. import agent_enrollment

    agent = await anyio.to_thread.run_sync(registry.get_agent, label)
    seat = await anyio.to_thread.run_sync(agent_enrollment.seat_for, label)
    if agent is None and seat is None:
        return None
    connection = "connected" if agent is not None else "never_connected"
    if agent is None:
        agent = _never_connected_agent(label)
    sessions = await anyio.to_thread.run_sync(lambda: registry.list_sessions(agent=label, limit=25))
    activity = await _safe("agent_activity", default={"events": []}, agent=label, limit=50)
    return {
        "agent": agent,
        "sessions": sessions,
        "activity": activity.get("events", []),
        "seat": seat,
        "connection": connection,
    }


async def sessions_payload(
    agent: str | None = None, status: str | None = None, limit: int = 100
) -> dict:
    sessions = await anyio.to_thread.run_sync(
        lambda: registry.list_sessions(agent=agent, status=status, limit=limit)
    )
    sessions = [_session_platform_row(session) for session in sessions]
    # Idle-wake request and operator grant (task #2050); None for a session with neither.
    states = await anyio.to_thread.run_sync(
        lambda: message_wake.wake_states([str(s["id"]) for s in sessions])
    )
    for session in sessions:
        session["wake"] = states.get(str(session["id"]))
    open_count = sum(1 for s in sessions if s["status"] in ("starting", "running", "waiting"))
    return {"sessions": sessions, "open": open_count, "total": len(sessions)}


async def session_detail_payload(session_id: str) -> dict | None:
    """One session as ``GET /api/sessions`` lists it: platform links and wake (task #2501).

    ``registry.get_session`` reads history older than the list page; this adds the
    same ``platform``/``links`` and ``wake`` (idle-wake request and operator grant,
    task #2050) the list handler adds, so the detail page needs no list read.
    """
    session = await anyio.to_thread.run_sync(registry.get_session, session_id)
    if session is None:
        return None
    session = _session_platform_row(session)
    states = await anyio.to_thread.run_sync(lambda: message_wake.wake_states([str(session["id"])]))
    session["wake"] = states.get(str(session["id"]))
    return session


async def runs_payload(
    *,
    kind: str | None = None,
    agent: str | None = None,
    task_id: int | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict:
    """Uniform run rows across dispatch_runs, self-pickup events and sessions (#1652)."""
    from .. import runs_projection

    return await anyio.to_thread.run_sync(
        lambda: runs_projection.list_runs(
            kind=kind, agent=agent, task_id=task_id, cursor=cursor, limit=limit
        )
    )


async def run_detail_payload(run_id: str) -> dict | None:
    """One uniform run row, or None when no store holds it for this tenant."""
    from .. import runs_projection

    return await anyio.to_thread.run_sync(runs_projection.get_run, run_id)


async def crew_live_payload(session_id: str, *, message_limit: int = 50) -> dict:
    """FirstMate-style live crew view for one agent session (task #1025)."""
    from .. import crew

    return await anyio.to_thread.run_sync(
        lambda: crew.crew_live_view(session_id, message_limit=message_limit)
    )


async def crew_intervene(
    *,
    session_id: str,
    body: str,
    from_agent: str,
    message_id: str | None = None,
) -> dict:
    """Typed intervention into a live crew session (task #1025)."""
    from .. import crew

    return await anyio.to_thread.run_sync(
        lambda: crew.intervene_on_session(
            session_id=session_id,
            body=body,
            from_agent=from_agent,
            message_id=message_id,
        )
    )


async def _active_queue_tasks() -> tuple[list[dict], str | None]:
    """Load live tasks without letting done history fill the default window."""
    seen: dict[Any, dict] = {}
    errors: list[str] = []
    for status in _ACTIVE_QUEUE_STATUSES:
        result = await _task_list_all(status=status, fields="compact")
        if not isinstance(result, dict):
            continue
        if result.get("error"):
            errors.append(str(result["error"]))
        for task in result.get("tasks") or []:
            task_id = task.get("id")
            key = task_id if task_id is not None else id(task)
            if key in seen:
                continue
            for flag in ("waiting_for_review", "laptop_lane", "notes_archived"):
                if flag in task and type(task[flag]) is int:
                    task[flag] = bool(task[flag])
            seen[key] = task
    tasks = list(seen.values())
    _task_object_links(tasks)
    return tasks, ("; ".join(errors) if errors else None)


async def tasks_payload(project: str | None = None, completed_after: str | None = None) -> dict:
    all_tasks, error = await _active_queue_tasks()
    # Counts reflect the active queue (open/doing/waiting_review/blocked).
    # Done history is excluded so it cannot crowd out live work (task #685).
    projects: dict[str, int] = {}
    for task in all_tasks:
        proj = task.get("project", "unified-agent")
        projects[proj] = projects.get(proj, 0) + 1

    tasks = all_tasks if project is None else [t for t in all_tasks if t.get("project") == project]
    buckets: dict[str, int] = {}
    for task in tasks:
        buckets[task.get("status", "unknown")] = buckets.get(task.get("status", "unknown"), 0) + 1
    active = [t for t in tasks if t.get("status") in ("doing", "blocked", "waiting_review")]
    out = {"buckets": buckets, "active": active, "total": len(tasks), "projects": projects}
    if completed_after is not None:
        # Done tasks completed at or after the bound (task #2514), filtered in SQL by
        # task_list itself so a large done history cannot crowd the window out.
        done = await _task_list_all(
            status="done", completed_after=completed_after, project=project, fields="compact"
        )
        completed = list(done.get("tasks") or []) if isinstance(done, dict) else []
        _task_object_links(completed)
        out["completed"] = completed
        out["completed_after"] = completed_after
        out["completed_truncated"] = bool(isinstance(done, dict) and done.get("truncated"))
        if isinstance(done, dict) and done.get("error"):
            out["completed_error"] = done["error"]
    if error:
        # A page failed after earlier pages loaded: the counts are a prefix,
        # so say so instead of presenting them as the whole queue (#814).
        out["error"] = error
        out["truncated"] = True
    return out


async def task_search_payload(query: str, project: str | None = None, limit: int = 20) -> dict:
    """Ranked task search for the Control Center query box (task #1419).

    A thin pass-through to `task_list(query=...)` -- the ranked mode itself
    lives entirely in `task_search.py`/`tasks.py`, not here. This exists only
    so the query box has a `_safe`-wrapped, degrade-gracefully endpoint the
    same way every other webui pane does, rather than the SPA calling the MCP
    tool registry directly.
    """
    result = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        query=query,
        project=project,
        limit=limit,
    )
    tasks = result.get("tasks") or []
    _task_object_links(tasks)
    out: dict[str, Any] = {
        "tasks": tasks,
        "query": query,
        "truncated": bool(result.get("truncated", False)),
        "limit": result.get("limit", limit),
    }
    if result.get("error"):
        out["error"] = result["error"]
    return out


def _board_blocked_reason(task: dict[str, Any]) -> str | None:
    notes = task.get("notes") or ""
    match = _BOARD_UNBLOCK_RE.search(notes) or _BOARD_BLOCKED_RE.search(notes)
    if match:
        return match.group(1).strip()
    if task.get("status") == "blocked":
        return f"task #{task.get('id')} is blocked"
    return None


def _board_card_alerts(task: dict[str, Any], now: datetime) -> list[str]:
    alerts: list[str] = []
    status = task.get("status")
    created = _parse_dt(task.get("created_at"))
    if status == "open" and created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now - created > timedelta(days=BOARD_AGING_OPEN_DAYS):
            alerts.append("aging_open")
    if status == "doing":
        anchor = (
            _parse_dt(task.get("claimed_at"))
            or _parse_dt(task.get("updated_at"))
            or _parse_dt(task.get("created_at"))
        )
        if anchor is not None:
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
            if now - anchor > timedelta(hours=BOARD_AGING_DOING_HOURS):
                alerts.append("aging_doing")
        lock_expires = _parse_dt(task.get("lock_expires_at"))
        if lock_expires is not None:
            if lock_expires.tzinfo is None:
                lock_expires = lock_expires.replace(tzinfo=UTC)
            if lock_expires < now:
                alerts.append("lock_expired")
    return alerts


def _board_task_card(
    task: dict[str, Any],
    *,
    now: datetime,
    dep_counts: dict[Any, int],
    subtasks: list[dict] | None = None,
) -> dict[str, Any]:
    owner, _owner_source = _task_owner(task)
    age = _age_s(task.get("created_at"), now)
    return {
        "kind": "task",
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "priority": task.get("priority", "med"),
        "claimed_by": owner,
        "lane": task.get("lane") or "default",
        "project": task.get("project") or "unified-agent",
        "domain": task.get("domain"),
        "parent_id": task.get("parent_id"),
        "age_s": age,
        "worktree_path": task.get("worktree_path"),
        "branch": task.get("branch"),
        "dep_count": int(dep_counts.get(task.get("id"), 0)),
        "blocked_reason": _board_blocked_reason(task),
        "lock_expires_at": task.get("lock_expires_at"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "evidence_ref": task.get("evidence_ref"),
        "alerts": _board_card_alerts(task, now),
        "subtasks": subtasks or [],
        "swimlane": task.get("lane") or task.get("project") or "default",
        "rollup": task.get("rollup"),
        "project_view": task.get("project_view"),
    }


def _board_delegation_card(delegation: dict[str, Any], now: datetime) -> dict[str, Any]:
    deadline = _parse_dt(delegation.get("deadline"))
    remaining: int | None = None
    if deadline is not None:
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        remaining = int((deadline - now).total_seconds())
    status = delegation.get("status") or "pending"
    column = "doing" if status in {"pending", "in_flight", "running"} else "open"
    return {
        "kind": "delegation",
        "id": f"delegation:{delegation.get('id')}",
        "delegation_id": delegation.get("id"),
        "title": (delegation.get("task_spec") or "Delegation")[:120],
        "status": column,
        "priority": "med",
        "claimed_by": delegation.get("to_agent"),
        "from_agent": delegation.get("from_agent"),
        "to_agent": delegation.get("to_agent"),
        "task_id": delegation.get("task_id"),
        "lane": "default",
        "project": "unified-agent",
        "parent_id": None,
        "age_s": _age_s(delegation.get("created_at"), now),
        "worktree_path": None,
        "branch": None,
        "dep_count": 0,
        "blocked_reason": None,
        "deadline": delegation.get("deadline"),
        "deadline_remaining_s": remaining,
        "alerts": ["overdue"] if remaining is not None and remaining < 0 else [],
        "subtasks": [],
        "swimlane": delegation.get("to_agent") or "default",
    }


def _board_matches_filters(
    card: dict[str, Any],
    *,
    project: str | None,
    lane: str | None,
    agent: str | None,
    priority: str | None,
    min_age_days: float | None,
    claimed_by: str | None = None,
) -> bool:
    if project is not None and card.get("project") != project:
        return False
    if lane is not None and card.get("lane") != lane:
        return False
    if priority is not None and card.get("priority") != priority:
        return False
    if agent is not None:
        owners = {
            card.get("claimed_by"),
            card.get("to_agent"),
            card.get("from_agent"),
        }
        if agent not in owners:
            return False
    # The claim lock alone (task #2523): a delegation card has no claim, and a
    # task an agent only sent or received as a delegation is not its claim.
    if claimed_by is not None and card.get("claimed_by") != claimed_by:
        return False
    if min_age_days is not None:
        age_s = card.get("age_s")
        if age_s is None or age_s < min_age_days * 86400:
            return False
    return True


async def board_payload(
    *,
    project: str | None = None,
    lane: str | None = None,
    agent: str | None = None,
    priority: str | None = None,
    min_age_days: float | None = None,
    swimlane_by: str = "lane",
    include_cancelled: bool = False,
    claimed_by: str | None = None,
) -> dict:
    """Read-only kanban board snapshot for Command Center (task #728).

    Columns are the six task statuses exactly (open is Backlog). Done covers a
    rolling 30-day completion window. Cancelled is always listed but empty unless
    ``include_cancelled``. Mutations stay on existing ``/api/action/task``.
    ``agent`` matches a claim or either end of a delegation; ``claimed_by``
    (task #2523) matches the task's claim lock exactly and drops delegation cards.
    """
    now = _board_now()
    errors: list[str] = []
    swimlane_by = swimlane_by if swimlane_by in {"lane", "project"} else "lane"

    tasks_by_id: dict[Any, dict] = {}

    async def _load(status: str, fields: str = "compact", **kwargs: Any) -> None:
        result = await _task_list_all(status=status, fields=fields, **kwargs)
        if not isinstance(result, dict):
            return
        if result.get("error"):
            errors.append(str(result["error"]))
        for task in result.get("tasks") or []:
            tid = task.get("id")
            if tid is None:
                continue
            tasks_by_id[tid] = task

    for status in ("open", "doing", "waiting_review", "blocked"):
        await _load(status)
    completed_after = (now - timedelta(days=BOARD_DONE_WINDOW_DAYS)).isoformat(timespec="seconds")
    await _load("done", completed_after=completed_after)
    if include_cancelled:
        await _load("cancelled")
    # Blocked tasks are few; re-load full so _board_blocked_reason can read
    # unblock-condition text that the compact projection drops.
    await _load("blocked", fields="full")

    # ponytail: single 2000-edge window; dependency_list caps at 2000. Switch
    # to per-task dependency_list(task_id=...) calls if edges ever exceed it.
    deps_res = await _safe("dependency_list", default={"dependencies": []}, limit=2000)
    if isinstance(deps_res, dict) and deps_res.get("error"):
        errors.append(str(deps_res["error"]))
    dep_counts: dict[Any, int] = {}
    for edge in (deps_res.get("dependencies") if isinstance(deps_res, dict) else None) or []:
        tid = edge.get("task_id")
        if tid is None:
            continue
        dep_counts[tid] = dep_counts.get(tid, 0) + 1

    # Nest subtasks under parents that are also on the board AND share the
    # parent's status; subtasks in a different status stay top-level so every
    # card renders in its own authoritative column. Orphan subtasks stay
    # top-level so nothing disappears when a parent is filtered out.
    children: dict[Any, list[dict]] = {}
    children_any: dict[Any, list[dict]] = {}
    roots: list[dict] = []
    for task in tasks_by_id.values():
        parent_id = task.get("parent_id")
        parent = tasks_by_id.get(parent_id) if parent_id is not None else None
        if parent_id is not None and parent is not None:
            children_any.setdefault(parent_id, []).append(task)
        if parent is not None and (task.get("status") or "open") == (
            parent.get("status") or "open"
        ):
            children.setdefault(parent_id, []).append(task)
        else:
            roots.append(task)

    columns: dict[str, list[dict]] = {status: [] for status in BOARD_STATUSES}

    def _sorted_children(bucket: dict[Any, list[dict]], parent_id: Any) -> list[dict]:
        return sorted(
            bucket.get(parent_id, []),
            key=lambda t: (t.get("priority") != "high", t.get("id") or 0),
        )

    def project_steps(task: dict) -> list[dict]:
        return [
            _board_task_card(
                child,
                now=now,
                dep_counts=dep_counts,
                subtasks=project_steps(child),
            )
            for child in _sorted_children(children_any, task.get("id"))
        ]

    def nested_card(task: dict) -> dict:
        nested = [nested_card(child) for child in _sorted_children(children, task.get("id"))]
        card = _board_task_card(task, now=now, dep_counts=dep_counts, subtasks=nested)
        if task.get("rollup") is not None:
            card["steps"] = project_steps(task)
        return card

    def set_swimlane(card: dict, swimlane: str) -> None:
        card["swimlane"] = swimlane
        for subtask in card["subtasks"]:
            set_swimlane(subtask, swimlane)

    def filter_tree(card: dict) -> dict | None:
        kept_subtasks = [
            kept for subtask in card["subtasks"] if (kept := filter_tree(subtask)) is not None
        ]
        if not kept_subtasks and not _board_matches_filters(
            card,
            project=project,
            lane=lane,
            agent=agent,
            priority=priority,
            min_age_days=min_age_days,
            claimed_by=claimed_by,
        ):
            return None
        return {**card, "subtasks": kept_subtasks}

    for task in roots:
        card = nested_card(task)
        swimlane = (card.get("lane") if swimlane_by == "lane" else card.get("project")) or "default"
        set_swimlane(card, swimlane)
        card = filter_tree(card)
        if card is None:
            continue
        status = card.get("status") or "open"
        if status not in columns:
            continue
        if status == "cancelled" and not include_cancelled:
            continue
        columns[status].append(card)

    delegations_res = await _safe(
        "delegate_list", default={"delegations": []}, status="pending", limit=200
    )
    if isinstance(delegations_res, dict) and delegations_res.get("error"):
        errors.append(str(delegations_res["error"]))
    for delegation in (
        delegations_res.get("delegations") if isinstance(delegations_res, dict) else None
    ) or []:
        card = _board_delegation_card(delegation, now)
        # Delegations inherit the linked task's project/lane so project and
        # lane filters surface them with the work they belong to.
        linked = tasks_by_id.get(delegation.get("task_id"))
        if linked is not None:
            card["project"] = linked.get("project") or card["project"]
            card["lane"] = linked.get("lane") or card["lane"]
        if not _board_matches_filters(
            card,
            project=project,
            lane=lane,
            agent=agent,
            priority=priority,
            min_age_days=min_age_days,
            claimed_by=claimed_by,
        ):
            continue
        columns[card["status"]].append(card)

    # WIP indicators — source of truth is #339 / config.lane_wip_limits().
    lane_limits = config.lane_wip_limits()
    doing_tasks = [t for t in tasks_by_id.values() if t.get("status") == "doing"]
    lane_counts: dict[str, int] = {}
    agent_counts: dict[str, int] = {}
    for task in doing_tasks:
        lock_expires = _parse_dt(task.get("lock_expires_at"))
        if lock_expires is not None:
            if lock_expires.tzinfo is None:
                lock_expires = lock_expires.replace(tzinfo=UTC)
            if lock_expires < now:
                continue  # expired locks excluded from WIP accounting (#339)
        task_lane = task.get("lane") or "default"
        lane_counts[task_lane] = lane_counts.get(task_lane, 0) + 1
        owner, _ = _task_owner(task)
        if owner:
            agent_counts[owner] = agent_counts.get(owner, 0) + 1

    wip_lanes: dict[str, dict[str, Any]] = {}
    for task_lane, count in sorted(lane_counts.items()):
        limit = lane_limits.get(task_lane)
        cap = int(limit["wip_max"]) if limit else None
        mode = str(limit["mode"]) if limit else None
        over = bool(cap is not None and count > cap)
        wip_lanes[task_lane] = {
            "count": count,
            "cap": cap,
            "mode": mode,
            "over": over,
        }
    # Surface configured caps even at zero so the UI can show headroom.
    for task_lane, limit in lane_limits.items():
        wip_lanes.setdefault(
            task_lane,
            {
                "count": 0,
                "cap": int(limit["wip_max"]),
                "mode": str(limit["mode"]),
                "over": False,
            },
        )

    agent_cap = config.agent_wip_max()
    wip_agents = {
        label: {
            "count": count,
            "cap": config.agent_wip_max_for(label),
            "over": count > config.agent_wip_max_for(label),
        }
        for label, count in sorted(agent_counts.items())
    }

    alerts: list[dict[str, Any]] = []
    for task_lane, info in wip_lanes.items():
        if info.get("over"):
            alerts.append(
                {
                    "kind": "wip_lane",
                    "lane": task_lane,
                    "count": info["count"],
                    "cap": info["cap"],
                    "mode": info.get("mode"),
                }
            )
    for label, info in wip_agents.items():
        if info.get("over"):
            alerts.append(
                {
                    "kind": "wip_agent",
                    "agent": label,
                    "count": info["count"],
                    "cap": info["cap"],
                }
            )

    column_rows = []
    for status in BOARD_STATUSES:
        cards = columns[status]
        column_rows.append(
            {
                "status": status,
                "label": BOARD_COLUMN_LABELS[status],
                "cards": cards,
                "count": len(cards),
                "hidden_by_default": status == "cancelled",
            }
        )

    out: dict[str, Any] = {
        "as_of": now.isoformat(timespec="seconds"),
        "columns": column_rows,
        "swimlane_by": swimlane_by,
        "wip": {"lanes": wip_lanes, "agents": wip_agents, "agent_cap": agent_cap},
        "alerts": alerts,
        "filters": {
            "project": project,
            "lane": lane,
            "agent": agent,
            "claimed_by": claimed_by,
            "priority": priority,
            "min_age_days": min_age_days,
            "include_cancelled": include_cancelled,
        },
        "thresholds": {
            "aging_open_days": BOARD_AGING_OPEN_DAYS,
            "aging_doing_hours": BOARD_AGING_DOING_HOURS,
            "done_window_days": BOARD_DONE_WINDOW_DAYS,
        },
    }
    if errors:
        out["errors"] = errors
        out["error"] = "; ".join(errors)
    return out


# Flow metrics (task #729 / Lane A Increment A2): rolling completion window and
# WIP age bucket edges, seconds. Buckets are closed-open except the last, which
# is unbounded — matching the contract pinned on UA task #729.
FLOW_METRICS_WINDOW_DAYS = 30
_WIP_AGE_BUCKET_EDGES_S = (
    (0, "0-1d", 86400),
    (86400, "1-3d", 3 * 86400),
    (3 * 86400, "3-7d", 7 * 86400),
    (7 * 86400, "7-14d", 14 * 86400),
    (14 * 86400, "14-30d", 30 * 86400),
    (30 * 86400, "30d+", None),
)


def _percentile(samples: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over a sample list (sorted internally).

    Returns None for an empty sample list — "no samples" and "0 seconds" are
    different facts, and the pinned contract represents the first as null.
    """
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return float(ordered[rank - 1])


async def flow_metrics_payload() -> dict:
    """Flow metrics for the Board's metrics strip (task #729).

    Shape is PINNED (frontend workstream builds against it — do not change
    without noting on UA task #729):

        {"cycle_time": {"p50_s, p90_s}, "lead_time": {...},
         "wip_age": {"buckets": [{label, count}], "oldest_s"},
         "window_days": 30, "as_of": iso}

    cycle = claimed_at->completed_at; lead = created_at->completed_at.
    Samples come from done tasks in the rolling 30-day completion window.
    null p50/p90 when a metric has no samples. WIP age buckets cover the
    active queue (open/doing/waiting_review/blocked) by created_at age.
    """
    now = _board_now()
    window_start = (now - timedelta(days=FLOW_METRICS_WINDOW_DAYS)).isoformat(timespec="seconds")

    cycle_samples: list[float] = []
    lead_samples: list[float] = []
    wip_ages: list[int] = []
    total_done_samples = 0
    attributed_done_samples = 0

    def _age_or_none(raw: Any, ref: datetime) -> float | None:
        when = _parse_dt(raw)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (ref - when).total_seconds())

    read_errors: list[str] = []

    async def _collect(status: str, **kwargs: Any) -> None:
        nonlocal total_done_samples, attributed_done_samples
        result = await _task_list_all(status=status, fields="compact", **kwargs)
        if isinstance(result, dict) and result.get("error"):
            read_errors.append(f"{status}: {result['error']}")
        for task in (result.get("tasks") if isinstance(result, dict) else None) or []:
            completed = task.get("completed_at")
            if completed:
                total_done_samples += 1
                if task.get("claimed_by"):
                    attributed_done_samples += 1
                done_at = _parse_dt(completed)
                if done_at is not None and done_at.tzinfo is None:
                    done_at = done_at.replace(tzinfo=UTC)
                # Sample ages are measured claim->done and create->done, not
                # claim->now: a completed task's age relative to *now* would
                # grow every poll even though the work itself is finished.
                if done_at is not None:
                    if task.get("claimed_at") is not None:
                        claimed = _parse_dt(task["claimed_at"])
                        if claimed is not None:
                            if claimed.tzinfo is None:
                                claimed = claimed.replace(tzinfo=UTC)
                            cycle_samples.append(max(0.0, (done_at - claimed).total_seconds()))
                    if task.get("created_at") is not None:
                        created = _parse_dt(task["created_at"])
                        if created is not None:
                            if created.tzinfo is None:
                                created = created.replace(tzinfo=UTC)
                            lead_samples.append(max(0.0, (done_at - created).total_seconds()))
            else:
                age = _age_or_none(task.get("created_at"), now)
                if age is not None:
                    wip_ages.append(int(age))

    for status in ("open", "doing", "waiting_review", "blocked"):
        await _collect(status)
    await _collect("done", completed_after=window_start, limit=_COMPLETED_WINDOW_LIMIT)

    bucket_counts: list[int] = [0] * len(_WIP_AGE_BUCKET_EDGES_S)
    for age in wip_ages:
        for idx, (_floor, _label, ceiling) in enumerate(_WIP_AGE_BUCKET_EDGES_S):
            if ceiling is None or age < ceiling:
                bucket_counts[idx] += 1
                break

    return {
        "cycle_time": {
            "p50_s": _percentile(cycle_samples, 50),
            "p90_s": _percentile(cycle_samples, 90),
        },
        "lead_time": {
            "p50_s": _percentile(lead_samples, 50),
            "p90_s": _percentile(lead_samples, 90),
        },
        "wip_age": {
            "buckets": [
                {"label": label, "count": count}
                for (_floor, label, _ceiling), count in zip(
                    _WIP_AGE_BUCKET_EDGES_S, bucket_counts, strict=True
                )
            ],
            "oldest_s": float(max(wip_ages)) if wip_ages else None,
        },
        "attribution_coverage": {
            "total_done": total_done_samples,
            "attributed_done": attributed_done_samples,
            "coverage_ratio": (
                round(attributed_done_samples / total_done_samples, 4)
                if total_done_samples > 0
                else None
            ),
        },
        "window_days": FLOW_METRICS_WINDOW_DAYS,
        "as_of": now.isoformat(timespec="seconds"),
        # Metrics over a partial read are not the queue's metrics (#814).
        **({"error": "; ".join(read_errors), "truncated": True} if read_errors else {}),
    }


def _queue_urgency_key(task: dict) -> tuple[int, int, datetime, int]:
    """Sort actionable backlog by urgency, priority, then oldest creation."""
    urgency = task.get("urgency")
    urgency_rank = {"now": 0, "next": 1, "later": 2}.get(urgency, 1)
    priority_rank = {"high": 0, "med": 1, "low": 2}.get(task.get("priority"), 1)
    created = _parse_dt(task.get("created_at")) or datetime.max.replace(tzinfo=UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return urgency_rank, priority_rank, created, task.get("id") or 0


async def queue_health_payload() -> dict:
    """Queue health breakdown for Control Center v2."""
    now = datetime.now(UTC)
    tasks, error = await _active_queue_tasks()

    desktop_open: list[dict] = []
    dispatch_ready: list[dict] = []
    interactive_only: list[dict] = []
    operator_only: list[dict] = []
    doing: list[dict] = []
    waiting_review: list[dict] = []
    stale_claims: list[dict] = []
    laptop_lane: list[dict] = []
    blocked_archived: list[dict] = []
    inconsistent: list[dict] = []
    other: list[dict] = []

    for task in tasks:
        status = task.get("status", "open")
        title = task.get("title", "")
        # Compact task rows intentionally omit notes.  These flags are computed
        # in the task-list SQL projection, preserving queue categorization
        # without loading every task's potentially large notes body.
        notes_str = task.get("notes") or ""
        is_laptop = (
            task.get("lane") == "laptop"
            or bool(task.get("laptop_lane", False))
            or (task.get("source_host") or "").lower().startswith("laptop")
        )
        is_archived = (
            "[archived" in title.lower()
            or bool(task.get("notes_archived", False))
            or "[archived" in notes_str.lower()
        )
        is_waiting_review = (
            bool(task.get("waiting_for_review", False))
            or "[waiting-for: review]" in notes_str.lower()
        )

        # status and completed_at record one fact twice; either half without the
        # other is a contradiction no notes marker can explain away.
        timestamp_conflict = (status == "done") != bool(task.get("completed_at"))

        if (status == "done" and is_waiting_review) or timestamp_conflict:
            # Historical contradiction: the Q3 transition guard prevents new
            # rows, but old rows remain visible until explicitly reconciled.
            inconsistent.append(task)
        elif status == "blocked" or is_archived:
            blocked_archived.append(task)
        elif is_laptop and status != "done":
            laptop_lane.append(task)
        elif status == "doing":
            doing.append(task)
            lock_expires = _parse_dt(task.get("lock_expires_at"))
            if lock_expires is not None:
                if lock_expires.tzinfo is None:
                    lock_expires = lock_expires.replace(tzinfo=UTC)
                if lock_expires < now:
                    stale_claims.append(task)
        elif status == "open":
            if is_waiting_review:
                waiting_review.append(task)
            elif task.get("lane") == "interactive-only":
                interactive_only.append(task)
            elif config.canonical_task_lane(task.get("lane")) == "operator-only":
                operator_only.append(task)
            elif task.get("lane") == "dispatch-ready":
                dispatch_ready.append(task)
            else:
                desktop_open.append(task)
        elif status == "waiting_review":
            # The real status (task-lifecycle-contract.md §6 Q5) and the legacy
            # `[waiting-for: review]` notes marker on an 'open' row both land here.
            waiting_review.append(task)
        elif status in ("done", "cancelled"):
            # Both terminal; intentionally excluded from this active-queue view.
            pass
        else:
            # Defensive fallback (task-lifecycle-contract.md §1.3/§1.4 step 3):
            # a status this view doesn't know about yet degrades to
            # visible-but-uncategorized instead of silently vanishing.
            other.append(task)

    for bucket in (desktop_open, dispatch_ready, interactive_only, operator_only):
        bucket.sort(key=_queue_urgency_key)

    from ..queue_hygiene import queue_pressure

    try:
        backpressure = await anyio.to_thread.run_sync(queue_pressure)
    except Exception as exc:  # noqa: BLE001 - reported in-band, never swallowed
        backpressure = {
            "level": "unknown",
            "signals": [],
            "error": f"queue_pressure failed: {exc}"[:200],
        }

    from ..dispatch.launch import live_run_count

    dispatch_wip: dict[str, Any] = {"cap": config.dispatch_max_live_runs(), "in_flight": None}
    try:
        dispatch_wip["in_flight"] = await anyio.to_thread.run_sync(live_run_count)
    except Exception as exc:  # noqa: BLE001 - show unavailable instead of false zero
        dispatch_wip["error"] = f"dispatch run count unavailable: {exc}"[:200]

    out = {
        "backpressure": backpressure,
        "dispatch_wip": dispatch_wip,
        "desktop_open": desktop_open,
        "dispatch_ready": dispatch_ready,
        "interactive_only": interactive_only,
        "operator_only": operator_only,
        "doing": doing,
        "waiting_review": waiting_review,
        "stale_claims": stale_claims,
        "laptop_lane": laptop_lane,
        "blocked_archived": blocked_archived,
        "inconsistent": inconsistent,
        "other": other,
        "counts": {
            "desktop_open": len(desktop_open),
            "dispatch_ready": len(dispatch_ready),
            "interactive_only": len(interactive_only),
            "operator_only": len(operator_only),
            "doing": len(doing),
            "waiting_review": len(waiting_review),
            "stale_claims": len(stale_claims),
            "laptop_lane": len(laptop_lane),
            "blocked_archived": len(blocked_archived),
            "inconsistent": len(inconsistent),
            "other": len(other),
            "total_active": (
                len(desktop_open)
                + len(dispatch_ready)
                + len(interactive_only)
                + len(operator_only)
                + len(doing)
                + len(waiting_review)
            ),
        },
    }
    # Temporary response aliases keep pre-upgrade SDKs usable. Totals count
    # only the canonical bucket; callers must not concatenate both aliases.
    out[config.LEGACY_OPERATOR_QUEUE_KEY] = operator_only
    out["counts"][config.LEGACY_OPERATOR_QUEUE_KEY] = len(operator_only)
    if error:
        out["error"] = error
    return out


async def review_approval_payload() -> dict:
    """Read-only review and approval wait state."""
    errors: list[str] = []
    review_waits = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="full",
        notes_contain="[waiting-for: review]",
    )
    if isinstance(review_waits, dict) and review_waits.get("error"):
        errors.append(review_waits["error"])

    plan_approvals = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="compact",
        notes_contain="[waiting-for: plan-approval]",
    )
    if isinstance(plan_approvals, dict) and plan_approvals.get("error"):
        errors.append(plan_approvals["error"])

    human_merges = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="compact",
        notes_contain="[waiting-for: human-merge]",
    )
    if isinstance(human_merges, dict) and human_merges.get("error"):
        errors.append(human_merges["error"])

    approvals = await approvals_payload()
    if isinstance(approvals, dict) and approvals.get("error"):
        errors.append(approvals["error"])

    reviews: list[dict] = []
    tasks_raw = []
    if isinstance(review_waits, dict) and "tasks" in review_waits:
        tasks_raw.extend(
            [{"kind": "review", **t} for t in review_waits["tasks"] if t.get("status") != "done"]
        )
    if isinstance(plan_approvals, dict) and "tasks" in plan_approvals:
        tasks_raw.extend(
            [
                {"kind": "plan_approval", **t}
                for t in plan_approvals["tasks"]
                if t.get("status") != "done"
            ]
        )
    if isinstance(human_merges, dict) and "tasks" in human_merges:
        tasks_raw.extend(
            [
                {"kind": "human_merge", **t}
                for t in human_merges["tasks"]
                if t.get("status") != "done"
            ]
        )

    seen_ids = set()
    for t in tasks_raw:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            reviews.append(t)

    pending_approvals = approvals.get("pending", []) if isinstance(approvals, dict) else []
    kind_counts: dict[str, int] = {}
    for t in reviews:
        kind = t.get("kind", "review")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    out = {
        "reviews": reviews,
        "approvals": pending_approvals,
        "counts": {
            "reviews": len(reviews),
            "approvals": len(pending_approvals),
            "total_waits": len(reviews) + len(pending_approvals),
            "by_kind": kind_counts,
        },
    }
    if errors:
        out["errors"] = errors
        out["error"] = "; ".join(errors)
    return out


async def executor_delegation_payload() -> dict:
    """Read-only executor and delegation state."""
    errors: list[str] = []
    delegations_result = await _safe("delegate_list", default={"delegations": []}, status="all")
    if isinstance(delegations_result, dict) and delegations_result.get("error"):
        errors.append(delegations_result["error"])
    delegations = (
        delegations_result.get("delegations", []) if isinstance(delegations_result, dict) else []
    )

    worktrees, worktree_errors = await _surveyed_worktrees()

    counts: dict[str, int] = {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "cancelled": 0,
        "error": 0,
    }
    recent_failures: list[dict[str, Any]] = []
    for d in delegations:
        raw_status = d.get("status", "unknown")
        status = _normalize_delegation_status(raw_status)
        d["outcome"] = evidence.delegation_outcome(raw_status)
        if status in counts:
            counts[status] += 1
        else:
            counts[status] = counts.get(status, 0) + 1
        if status == "error":
            detail = str(d.get("callback_result") or d.get("reason") or "failed")
            recent_failures.append(
                {
                    "id": d.get("id"),
                    "task_id": d.get("task_id"),
                    "from_agent": d.get("from_agent"),
                    "to_agent": d.get("to_agent"),
                    "status": status,
                    "updated_at": d.get("updated_at"),
                    "detail": detail[:160],
                }
            )

    health = await health_payload()
    executor_health = next(
        (
            check
            for check in health.get("checks", [])
            if check.get("capability") == "active_executor_service"
        ),
        None,
    )

    sessions = await sessions_payload(limit=50)
    recent_sessions = (sessions.get("sessions") or [])[:25]

    out = {
        "delegations": delegations,
        "stale_worktrees": worktrees,
        "recent_failures": recent_failures,
        "recent_sessions": recent_sessions,
        "counts": counts,
        "total_delegations": len(delegations),
        "outcomes": evidence.outcomes_snapshot(),
        "health": {
            "summary": health.get("summary", {}),
            "check": executor_health,
            "cached_age_s": health.get("cached_age_s"),
        },
    }
    if errors:
        out["errors"] = errors
        out["error"] = "; ".join(errors)
    if worktree_errors:
        out["worktree_errors"] = worktree_errors
        out["worktree_error"] = "; ".join(worktree_errors)
    return out


#: Most expired passes ``/api/approvals`` returns (task #1786). They authorize
#: nothing and render in a secondary bucket, so the newest are kept and the
#: total is reported alongside; the full set stays in ``approval_pending``.
APPROVALS_EXPIRED_PASSES_CAP = 50


async def approvals_payload() -> dict:
    result = await _safe(
        "approval_pending",
        default={"pending": [], "approved_unconsumed": []},
        newest_expired=True,
    )
    if not isinstance(result, dict):
        return {"pending": [], "approved_unconsumed": []}

    expired_all = list(result.get("expired_passes", []))
    # newest_expired=True: approval_pending returns the newest window of its
    # own bound (still oldest-first) and the uncapped count, so this slice and
    # the total cover every expired pass, not just the oldest `limit` of them.
    expired = expired_all[-APPROVALS_EXPIRED_PASSES_CAP:] if APPROVALS_EXPIRED_PASSES_CAP else []
    expired_total = result.get("expired_passes_total")
    if not isinstance(expired_total, int):
        expired_total = len(expired_all)
    pending, live, expired = await _read_approval_platform_rows(
        [
            list(result.get("pending", [])),
            list(result.get("approved_unconsumed", [])),
            expired,
        ]
    )
    out = {
        "pending": pending,
        "approved_unconsumed": live,
        # Task #596: inert grants, carried through so they stay visible
        # somewhere rather than disappearing when they left
        # `approved_unconsumed`. Secondary by design — the operator's decision
        # list is `pending`, and an expired pass authorizes nothing.
        "expired_passes": expired,
        "expired_passes_total": expired_total,
        "expired_passes_truncated": len(expired) < expired_total,
        "gated_tools": result.get("gated_tools", []),
        "pass_ttl_hours": result.get("pass_ttl_hours"),
    }
    if result.get("error"):
        out["error"] = result["error"]
    return out


#: The only gate implemented today (`tools.approvals.check_approval_gate`) is
#: the hub-side global allowlist (`UA_APPROVAL_TOOLS`) — see that module's
#: docstring. Kept as a named constant (not inlined into the payload) so a
#: second gate kind, if one is ever added, has one place to register a label.
_APPROVAL_GATE = {
    "kind": "global_allowlist",
    "label": "Global allowlist (UA_APPROVAL_TOOLS)",
}


async def approval_detail_payload(approval_id: int, caller: str | None = None) -> dict[str, Any]:
    """Full single-row detail for the Control Center approvals drawer (task #1263).

    ``approvals_payload`` (the list view) deliberately strips ``payload`` and
    ``reason`` to keep the queue compact and to avoid ever putting a gated
    tool's raw arguments in front of an operator who did not ask to see them.
    This is the opt-in read for the row an operator explicitly opened: it
    still never returns the payload verbatim — values pass through
    ``sanitize_metadata`` (the same redaction telemetry spans use), so a
    secret-shaped argument (token, password, API key) is elided before it
    ever reaches the browser, pretty-printed or not.

    Adds three fields the list view has no room for: ``gate`` (which
    mechanism parked this request — today always the global allowlist; see
    ``tools.approvals.check_approval_gate``), ``device`` (the requester's last
    known host from the agent registry, best-effort) with a ``device_verified``
    flag, and ``pr_url`` / ``commit_sha`` when the gated payload happens to
    carry one (e.g. a delegation or work-product-release payload that names a
    PR or commit).

    ``device_verified`` is a deliberately conservative reading of
    ``registry.last_seen_via`` (see ``touch_agent``'s docstring): only
    ``"hub_request"`` — an authenticated request the hub itself observed —
    counts as verified. Every other channel (``heartbeat_tool``,
    ``agent_event``, ``executor_poll``, ``backfill``) is a self-reported
    claim an agent could set to any string, so it renders as unverified. This
    mirrors the #1217 honesty rule applied to devices instead of identities:
    a bare hostname must never be presented as if it were confirmed.

    Task #2514 adds three answers the Inbox needs before it offers Approve:

    * ``payload`` is a complete redacted preview (``redacted_preview``), not the
      telemetry projection that kept ten items per list and ten keys per mapping.
      ``payload_complete`` and ``payload_omissions`` say what, if anything, it
      still leaves out, so an approver never approves a call whose eleventh
      target was silently hidden; an incomplete preview must be shown as such.
    * ``pass``: ``{state, expires_at, call_outcome}`` (``approvals.pass_view``):
      whether an approved pass has expired, and whether a consumed pass's call
      completed or failed.
    * ``approver``: ``{may_approve: yes|no|unknown, reason}`` for ``caller``
      (``approvals.may_approve``, the ``approval_resolve`` rule with the
      alias-aware ``_same_person``).

    Returns ``{"error": ...}`` for an unknown id rather than raising — the
    route maps that to 404.
    """
    from ..tools.approvals import get_approval, may_approve, pass_view

    row = await anyio.to_thread.run_sync(get_approval, approval_id)
    if row is None:
        return {"error": f"no approval request with id {approval_id}"}

    raw_payload = row.get("payload")
    raw_payload = raw_payload if isinstance(raw_payload, dict) else {}

    requested_by = row.get("requested_by")
    device: str | None = None
    device_verified = False
    if requested_by:
        try:
            agent = await anyio.to_thread.run_sync(registry.get_agent, requested_by)
            if agent:
                device = agent.get("host")
                device_verified = device is not None and agent.get("last_seen_via") == "hub_request"
        except Exception:  # noqa: BLE001 - device is best-effort, never blocks the read
            device = None
            device_verified = False

    task_id = raw_payload.get("task_id")
    if task_id is None:
        task_id = raw_payload.get("parent_task_id")
    pr_url = raw_payload.get("pr_url") or raw_payload.get("pull_request_url")
    commit_sha = raw_payload.get("commit_sha") or raw_payload.get("sha")
    display_payload, completeness = redacted_preview(raw_payload)
    if not isinstance(display_payload, dict):
        display_payload = {}
    review_display_complete = None
    if row.get("tool") == "github_app.process_pr_event" and "independent_review" in raw_payload:
        from .. import independent_review

        review_display_complete = False
        try:
            review = independent_review.validate(
                raw_payload["independent_review"],
                repo=raw_payload.get("repo"),
                number=raw_payload.get("number"),
                head_sha=raw_payload.get("head_sha"),
            )
        except independent_review.ReviewEvidenceError:
            pass
        else:
            # The generic telemetry projection truncates strings/arrays/depth.
            # This strict bounded schema needs its full attestation and ALL
            # actors visible before resolution. Preserve existing redaction.
            def redact_review(value):
                if isinstance(value, str):
                    return redact_sensitive_content(value, max_len=2048)
                if isinstance(value, list):
                    return [redact_review(item) for item in value]
                if isinstance(value, dict):
                    return {key: redact_review(item) for key, item in value.items()}
                return value

            displayed = redact_review(review)
            display_payload["independent_review"] = displayed
            review_display_complete = displayed == review

    return {
        "id": row.get("id"),
        "tool": row.get("tool"),
        "status": row.get("status"),
        "outcome": evidence.approval_outcome(row.get("status")),
        "requested_by": requested_by,
        "run_id": row.get("run_id"),
        "device": device,
        "device_verified": device_verified,
        "resolved_by": row.get("resolved_by"),
        "reason": row.get("reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "resolved_at": row.get("resolved_at"),
        "gate": _APPROVAL_GATE,
        "payload": display_payload,
        "payload_complete": completeness["complete"],
        "payload_omissions": completeness["omissions"],
        "payload_omissions_total": completeness["omissions_total"],
        "payload_redacted_values": completeness["redacted"],
        "review_display_complete": review_display_complete,
        "pass": await anyio.to_thread.run_sync(pass_view, row),
        "approver": dict(zip(("may_approve", "reason"), may_approve(row, caller), strict=True)),
        "pr_url": pr_url if isinstance(pr_url, str) else None,
        "commit_sha": commit_sha if isinstance(commit_sha, str) else None,
        "platform": _platform_ref("approval", row.get("id")),
        "links": {"task": _platform_ref("task", task_id)},
    }


async def memory_payload(
    *,
    query: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    as_of: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    project: str | None = None,
) -> dict:
    """Read-only memory browse/search for the Control Center Memory pane.

    Mirrors ``/api/approvals`` and ``/api/executor-delegation``: thin wrapper over
    registered MCP tools (``memory_search`` / ``memory_list``), no writes.
    Empty ``query`` browses newest-first; a non-empty query runs hybrid search.
    ``as_of`` (task #2095) passes through to ``memory_search`` unchanged; browse
    ignores it.

    ``tag`` and ``tags`` (every one required), ``project`` (the tag
    ``project:<name>``) and ``author`` go to the tool, which applies them
    before its cap (task #2515), so a filtered page is never a filtered
    slice of an unfiltered one.
    """
    cleaned_query = (query or "").strip()
    cleaned_tag = (tag or "").strip() or None
    cleaned_as_of = (as_of or "").strip() or None
    cleaned_author = (author or "").strip() or None
    cleaned_project = (project or "").strip() or None
    capped = max(1, min(int(limit), 200))
    wanted: list[str] = []
    for item in [cleaned_tag, *(tags or [])]:
        cleaned = (item or "").strip()
        if cleaned and cleaned not in wanted:
            wanted.append(cleaned)
    if cleaned_project and f"project:{cleaned_project}" not in wanted:
        wanted.append(f"project:{cleaned_project}")

    if cleaned_query:
        search_args: dict[str, Any] = {
            "query": cleaned_query,
            "tags": wanted or None,
            "limit": capped,
        }
        if cleaned_as_of:
            search_args["as_of"] = cleaned_as_of
        if cleaned_author:
            search_args["author"] = cleaned_author
        result = await _safe("memory_search", default={"results": []}, **search_args)
        notes = result.get("results", []) if isinstance(result, dict) else []
        mode = "search"
    else:
        list_args: dict[str, Any] = {"tag": wanted[0] if wanted else None, "limit": capped}
        if len(wanted) > 1:
            list_args["tags"] = wanted[1:]
        if cleaned_author:
            list_args["author"] = cleaned_author
        result = await _safe("memory_list", default={"items": []}, **list_args)
        notes = result.get("items", []) if isinstance(result, dict) else []
        mode = "browse"

    out: dict[str, Any] = {
        "mode": mode,
        "query": cleaned_query or None,
        "tag": cleaned_tag,
        "tags": wanted,
        "author": cleaned_author,
        "limit": capped,
        "results": notes,
        "items": notes,
        "total": len(notes),
    }
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result["error"]
        out["errors"] = [result["error"]]
    return out


# Named Control Center error when UA_VAULT_PATH is empty (task #1123).
VAULT_NOT_CONFIGURED = "vault not configured"


def _vault_not_configured(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Vault pane's named error state."""
    return {"error": VAULT_NOT_CONFIGURED, "code": "vault_not_configured", **extra}


async def vault_search_payload(
    *,
    pattern: str,
    path: str = ".",
    max_results: int = 100,
) -> dict[str, Any]:
    """Read-only vault search for the Control Center Vault pane (task #1123)."""
    if config.vault_root() is None:
        return _vault_not_configured(matches=[], truncated=False)
    return await call_tool(
        "vault_search",
        pattern=pattern,
        path=path,
        max_results=max_results,
    )


async def vault_page_payload(*, path: str) -> dict[str, Any]:
    """Note content + outgoing links + backlinks via vault_read/vault_links."""
    if config.vault_root() is None:
        return _vault_not_configured(
            path=path,
            content="",
            total_lines=0,
            truncated=False,
            outgoing=[],
            backlinks=[],
        )
    note = await call_tool("vault_read", path=path)
    links = await call_tool("vault_links", path=path)
    return {
        **note,
        "outgoing": links.get("outgoing", []) if isinstance(links, dict) else [],
        "backlinks": links.get("backlinks", []) if isinstance(links, dict) else [],
    }


async def vault_ingest_status_payload(*, path: str) -> dict[str, Any]:
    """Read-only ingest classification — never marks (task #1123)."""
    if config.vault_root() is None:
        return _vault_not_configured(
            path=path,
            content_hash="",
            status=None,
            last_ingested_at=None,
            agent=None,
            pages=[],
        )
    # Explicit mark=False — the web surface must not record an ingest.
    return await call_tool("vault_ingest_status", path=path, mark=False)


async def vault_briefing_payload(
    *,
    date: str | None = None,
    include_inbox: bool = True,
) -> dict[str, Any]:
    """Daily briefing digest + inbox ingest strip (task #1123 / #968)."""
    if config.vault_root() is None:
        return _vault_not_configured(
            date=date,
            entries=[],
            desktop_count=0,
            laptop_count=0,
            deduped_count=0,
            inbox=[],
            inbox_pending_count=0,
        )
    return await call_tool(
        "vault_briefing_digest",
        date=date,
        include_inbox=include_inbox,
    )


# Named Control Center error when Horizon-2 inventory cannot be read (task #1147).
HORIZON2_UNAVAILABLE = "horizon2 unavailable"

# Named Control Center error when the quota projection cannot be read (task #1136).
QUOTA_PROJECTION_UNAVAILABLE = "quota projection unavailable"


def _horizon2_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Connectors pane's named error state."""
    return {
        "error": HORIZON2_UNAVAILABLE,
        "code": "horizon2_unavailable",
        "servers": [],
        "packages": [],
        "server_count": 0,
        "package_count": 0,
        "doctor": {
            "label": "Horizon-2 connectors",
            "checks": [],
        },
        **extra,
    }


def _quota_projection_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Quota pane's named error state."""
    return {
        "error": QUOTA_PROJECTION_UNAVAILABLE,
        "code": "quota_projection_unavailable",
        "quotas": [],
        "checked_at": None,
        "failure_class_counts": {},
        **extra,
    }


def _failure_class_counts(quotas: list[dict[str, Any]]) -> dict[str, int]:
    """Count non-null failure_class values present on projected lane rows."""
    counts: dict[str, int] = {}
    for row in quotas:
        fc = row.get("failure_class")
        if isinstance(fc, str) and fc:
            counts[fc] = counts.get(fc, 0) + 1
    return counts


async def quota_payload() -> dict[str, Any]:
    """Read-only fleet lane availability for the Quota pane (task #1136).

    Passes through ``model_quota_status()`` — never invents availability, never
    probes providers, never mutates credential_pool or dispatch state. Failures
    surface as a named ``quota projection unavailable`` payload.
    """
    try:
        raw = await call_tool("model_quota_status")
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _quota_projection_unavailable()

    if not isinstance(raw, dict):
        return _quota_projection_unavailable()

    quotas_raw = raw.get("quotas")
    if not isinstance(quotas_raw, list):
        return _quota_projection_unavailable()

    quotas: list[dict[str, Any]] = [row for row in quotas_raw if isinstance(row, dict)]
    result = {
        "quotas": quotas,
        "checked_at": raw.get("checked_at"),
        "failure_class_counts": _failure_class_counts(quotas),
    }
    registry_error = raw.get("credential_pool_error")
    if isinstance(registry_error, str) and registry_error:
        result["credential_pool_error"] = registry_error
    return result


# Named Control Center error when the credential pool cannot be read (task #1121).
CREDENTIAL_POOL_UNAVAILABLE = "credential pool unavailable"

# Metadata fields the credential pool view is allowed to render. Anything else on
# a row (there should never be anything else — credential_pool_register rejects
# secret-shaped text — but this is a second, independent guard) is dropped before
# the payload leaves this function, so a schema change on the tool side can never
# widen what the Control Center displays.
_CREDENTIAL_POOL_METADATA_FIELDS = (
    "id",
    "provider",
    "label",
    "note",
    "status",
    "active",
    "created_at",
    "updated_at",
)


def _credential_pool_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Credential Pool pane's named error state."""
    return {
        "error": CREDENTIAL_POOL_UNAVAILABLE,
        "code": "credential_pool_unavailable",
        "credentials": [],
        "count": 0,
        **extra,
    }


def _credential_pool_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project one credential_pool_list row down to display metadata only."""
    return {field: row.get(field) for field in _CREDENTIAL_POOL_METADATA_FIELDS}


async def credential_pool_payload() -> dict[str, Any]:
    """Read-only credential metadata inventory for the Credential Pool pane (task #1121/#1184).

    Passes through ``credential_pool_list(include_revoked=True)`` — never
    registers, revokes, or otherwise mutates a credential, and never surfaces
    a secret value: the tool itself stores metadata only (provider, label,
    note, status), and this function re-projects each row to that same named
    field set as a second, independent guard against ever forwarding an
    unexpected field. ``include_revoked=True`` is required here: task #1184's
    objective is an operator view of "registered/revoked" status, and
    ``credential_pool_list``'s own default (``include_revoked=False``) exists
    for the MCP tool's other callers (e.g. registration duplicate checks),
    not for this read surface — the pane's whole job is to show operators
    both states, most notably that a credential *was* revoked. Failures
    surface as a named ``credential pool unavailable`` payload.
    """
    try:
        raw = await call_tool("credential_pool_list", include_revoked=True)
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _credential_pool_unavailable()

    if not isinstance(raw, dict):
        return _credential_pool_unavailable()

    credentials_raw = raw.get("credentials")
    if not isinstance(credentials_raw, list):
        return _credential_pool_unavailable()

    credentials = [_credential_pool_row(row) for row in credentials_raw if isinstance(row, dict)]
    return {"credentials": credentials, "count": len(credentials)}


def _connector_horizon2_registration_supported(connector: str) -> bool:
    from .. import connectors

    manifest = connectors.get_manifest(connector)
    return manifest is None or manifest.horizon2_registration_supported


def _connector_lifecycle_status(
    *,
    active: bool,
    connector: str,
    created_at: str | None,
    live_connections: frozenset[str] | None,
) -> str:
    """Map a governed server row to registered/authorized/revoked."""
    from ..horizon2 import review_is_current

    if not active:
        return "revoked"
    if not _connector_horizon2_registration_supported(connector):
        return "dormant"
    if live_connections is not None and connector not in live_connections:
        return "registered"
    if not review_is_current(created_at):
        return "registered"
    return "authorized"


def _package_lifecycle_status(server_statuses: list[str]) -> str:
    """Prefer authorized > registered > dormant > revoked; empty means dormant."""
    if not server_statuses:
        return "dormant"
    for preferred in ("authorized", "registered", "dormant", "revoked"):
        if preferred in server_statuses:
            return preferred
    return "dormant"


def _connector_health(status: str) -> str:
    """Doctor-style LIVE/DORMANT/MISCONFIGURED label for a lifecycle status."""
    if status == "authorized":
        return "LIVE"
    if status == "registered":
        return "MISCONFIGURED"
    return "DORMANT"


async def connectors_payload() -> dict[str, Any]:
    """Read-only Horizon-2 connector inventory for the Connectors pane (task #1147).

    Combines ``horizon2_server_list(include_revoked=True)`` with
    ``horizon2_package_list`` and derives per-item lifecycle labels so dormant
    packaging is never mistaken for an authorized integration. Never calls
    register/revoke/authorize.
    """
    from ..horizon2 import live_mcp_connections

    try:
        servers_raw = await call_tool("horizon2_server_list", include_revoked=True)
        packages_raw = await call_tool("horizon2_package_list")
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _horizon2_unavailable()

    if not isinstance(servers_raw, dict) or not isinstance(packages_raw, dict):
        return _horizon2_unavailable()

    live = live_mcp_connections()
    servers: list[dict[str, Any]] = []
    by_connector: dict[str, list[str]] = {}
    servers_by_connector: dict[str, list[dict[str, Any]]] = {}
    for row in servers_raw.get("servers") or []:
        if not isinstance(row, dict):
            continue
        connector = str(row.get("connector") or "")
        created_at = row.get("created_at") if isinstance(row.get("created_at"), str) else None
        status = _connector_lifecycle_status(
            active=bool(row.get("active")),
            connector=connector,
            created_at=created_at,
            live_connections=live,
        )
        policy = str(row.get("policy") or "one-shot approval")
        last_verified = (
            created_at if _connector_horizon2_registration_supported(connector) else None
        )
        enriched = {
            **row,
            "status": status,
            "policy": policy,
            "last_verified": last_verified,
            "last-verified": last_verified,
        }
        servers.append(enriched)
        if connector:
            by_connector.setdefault(connector, []).append(status)
            servers_by_connector.setdefault(connector, []).append(enriched)

    packages: list[dict[str, Any]] = []
    doctor_checks: list[dict[str, Any]] = []
    for row in packages_raw.get("packages") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("package") or "")
        status = _package_lifecycle_status(by_connector.get(name, []))
        health = _connector_health(status)
        health_detail = f"Horizon-2 package {name}: {status}"
        health_gate = "horizon2_package_list / governed_servers"
        health_checked_at = None
        health_cached_age_s = None
        if name == "int-gcp-agent20":
            # Lifecycle/approval registration does not prove cloud health.
            # Reuse cloud_payload's shared 120s inventory cache; opening or
            # refreshing this pane must not spawn another gcloud process.
            from ..tools.gcloud import classify_gcp_Agent20_health

            try:
                if not config.gcp_project() or not config.gcp_worker():
                    gcp_health = classify_gcp_Agent20_health(None)
                else:
                    cloud = await cloud_payload()
                    gcp_health = classify_gcp_Agent20_health(cloud)
                    health_checked_at = cloud.get("checked_at")
                    health_cached_age_s = cloud.get("cached_age_s")
            except Exception as exc:  # noqa: BLE001 - pane reports health, never 500s
                gcp_health = {
                    "status": "DEGRADED",
                    "detail": f"cached cloud inventory failed: {str(exc)[:200]}",
                }
            health = gcp_health["status"]
            health_detail = f"GCP runtime health {health}: {gcp_health['detail']}"
            health_gate = "read-only cloud_payload inventory (shared cache)"
        pkg_servers = servers_by_connector.get(name, [])
        pkg_last_verified = max(
            (s.get("last_verified") for s in pkg_servers if s.get("last_verified")),
            default=None,
        )
        pkg_policy = (
            pkg_servers[0].get("policy")
            if pkg_servers and pkg_servers[0].get("policy")
            else str(row.get("policy") or "one-shot approval")
        )
        packages.append(
            {
                **row,
                "status": status,
                "health": health,
                "health_detail": health_detail,
                "health_checked_at": health_checked_at,
                "health_cached_age_s": health_cached_age_s,
                "policy": pkg_policy,
                "last_verified": pkg_last_verified,
                "last-verified": pkg_last_verified,
            }
        )
        doctor_checks.append(
            {
                "package": name,
                "capability": f"connector:{name}",
                "status": status,
                "health": health,
                "health_checked_at": health_checked_at,
                "health_cached_age_s": health_cached_age_s,
                "detail": health_detail,
                "gate": health_gate,
            }
        )

    return {
        "servers": servers,
        "packages": packages,
        "server_count": len(servers),
        "package_count": len(packages),
        "doctor": {
            "label": "Horizon-2 connectors",
            "checks": doctor_checks,
        },
    }


async def anomalies_payload() -> dict:
    return await _safe("check_anomalies", default={"anomalies": []})


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _surveyed_worktrees() -> tuple[list[dict[str, Any]], list[str]]:
    """Stale worktrees across the workspace AND the wider dev checkout.

    `cleanup_stale_worktrees` only walks `config.workspace_root()/.worktrees`,
    but COLLABORATION.md Rule 2 puts task worktrees in `~/dev/<repo>/.worktrees`
    — outside the workspace for every product repo. Panels fed from the
    narrower tool never saw those (found by hand 2026-08-19, same gap
    `_stale_worktrees_check` in tools/doctor.py already covers for
    `system_doctor`). This reuses that same read-only survey so the webui
    and `system_doctor` agree instead of one under-reporting.

    Filters on `stale` (task status/idle-time policy), not `disposable`
    (removal safety) — a fresh `doing` task's clean, base-equal checkout reads
    as disposable but is not stale, and a genuinely stale worktree holding
    uncommitted or unmerged work reads as not disposable but very much is
    stale. Conflating the two hid exactly the worktrees this panel exists to
    surface (task #445 / PR #193 review).
    """
    try:
        # Shared with system_doctor for UA_WORKTREE_SURVEY_CACHE_S (#1897): a
        # survey runs git several times per worktree, and this pane polls.
        found, _age_s = await anyio.to_thread.run_sync(
            lambda: cached_survey_worktrees(survey_worktrees)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a panel error, not a crash
        return [], [str(exc)]
    return [w for w in found if w["stale"]], []


def _age_s(raw: Any, now: datetime) -> int | None:
    when = _parse_dt(raw)
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, int((now - when).total_seconds()))


def _source_subject(source: dict[str, Any]) -> str:
    if "evidence_gap" in source:
        return f"evidence_gap:{source['evidence_gap']}"
    if "task_id" in source:
        return f"task:{source['task_id']}"
    if "approval_id" in source:
        return f"approval:{source['approval_id']}"
    if "worktree" in source:
        return f"worktree:{source['worktree']}"
    if "anomaly" in source:
        return f"anomaly:{source['anomaly']}"
    if "agent_label" in source:
        return f"agent:{source['agent_label']}"
    if "zulip_message_id" in source:
        return f"zulip:{source['zulip_message_id']}"
    return "unknown"


def _public_approval_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive approval payloads while keeping the operator-facing metadata."""
    public = {k: v for k, v in row.items() if k not in {"payload", "reason"}}
    public.pop("payload", None)
    public.pop("reason", None)
    public["outcome"] = evidence.approval_outcome(row.get("status"))
    return public


def _normalize_delegation_status(raw_status: str | None) -> str:
    status = (raw_status or "unknown").lower()
    if status == "in_flight":
        return "running"
    if status == "failed":
        return "error"
    return status


def _attention_item(
    *,
    kind: str,
    reason: str,
    source: dict[str, Any],
    severity: str,
    age_s: int | None,
    title: str | None = None,
    source_host: str | None = None,
    hub_origin: str | None = None,
) -> dict[str, Any]:
    host = source_host or config.current_source_host()
    origin = hub_origin or "local"
    item = {
        "kind": kind,
        "severity": severity,
        "source": source,
        "source_subject": _source_subject(source),
        "source_host": host,
        "hub_origin": origin,
        "age_s": age_s,
        "reason": reason,
    }
    if title:
        item["title"] = title
    return item


async def _identity_drift_items(now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Agent labels active recently that nobody added to UA_AGENT_LABELS.

    Same source as the `agent_label_drift` doctor check (`registry.unregistered_labels`),
    but queried directly instead of through the slow, systemctl-shelling `system_doctor`
    tool so this stays cheap enough for a 10s pane poll. Deliberately not filtered against
    a known-intentional allowlist (e.g. a human web-UI login token) — that judgment belongs
    to the operator reading the reason, not hardcoded here where it would go stale silently.
    """
    errors: list[str] = []
    try:
        labels = await anyio.to_thread.run_sync(registry.unregistered_labels)
    except Exception as exc:  # noqa: BLE001 - reported in-band, never swallowed
        errors.append(f"unregistered_labels failed: {str(exc)[:200]}")
        return [], errors

    items: list[dict[str, Any]] = []
    expected = config.expected_unregistered_labels()
    for label in labels:
        if label in expected:
            continue
        agent = await anyio.to_thread.run_sync(registry.get_agent, label)
        items.append(
            _attention_item(
                kind="identity_drift",
                severity="low",
                source={"agent_label": label},
                age_s=_age_s(agent.get("last_seen") if agent else None, now),
                title=label,
                reason="agent label active in the last 7d but not in UA_AGENT_LABELS",
            )
        )
    return items, errors


async def _stale_agent_items(
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Configured agents that have gone quiet — covers laptop connectivity too.

    Scoped to `known` (registered) labels that have been observed at least once
    (`last_seen` not null): a configured-but-never-launched label is a different fact
    (surfaced separately in /api/agents) from a teammate who stopped responding, and
    conflating them would make every fresh deployment's unused default labels page.
    """
    try:
        agents = await anyio.to_thread.run_sync(registry.list_agents)
    except Exception as exc:  # noqa: BLE001 - reported in-band, never swallowed
        return [], [f"list_agents failed: {str(exc)[:200]}"], []

    items: list[dict[str, Any]] = []
    # Informational rows are returned separately by attention_payload. They are
    # deliberately not counted as active notifications.
    informational = []
    expected_offline = config.expected_offline_agent_labels()
    for agent in agents:
        if not agent.get("known") or agent.get("last_seen") is None:
            continue
        status = agent.get("status")
        if status not in ("stale", "offline"):
            continue
        label = agent["label"]
        if label in expected_offline or (
            (label == "pi_worker" or label in config.operator_aliases())
            and not agent.get("active_tasks")
            and not agent.get("open_sessions")
        ):
            informational.append(
                _attention_item(
                    kind="stale_agent",
                    severity="info",
                    source={"agent_label": label},
                    age_s=_age_s(agent.get("last_seen"), now),
                    title=label,
                    reason=(
                        "offline state expected by configured topology"
                        if label in expected_offline
                        else "stale agent has no active task or session"
                    ),
                )
            )
            continue
        items.append(
            _attention_item(
                kind="stale_agent",
                severity="medium" if status == "offline" else "low",
                source={"agent_label": label},
                age_s=_age_s(agent.get("last_seen"), now),
                title=label,
                reason=f"configured agent has gone {status}",
            )
        )
    return items, [], informational


_CYCLE_ATTENTION_SEVERITY = {"failed": "high", "blocked": "medium", "killed": "medium"}


async def _autonomous_cycle_items(
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Bounded-autonomy cycles that stopped and need a human look (task #373
    Tier 2, 3.5). Was entirely absent from this view before — not even
    flagged in _ATTENTION_OMITTED, unlike roadmap_conflict/migration_gate,
    which at least documented the gap. `failed`/`blocked`/`killed` are the
    three "stopped, not by success" states (docs/s5-bounded-autonomy-
    acceptance-gates.md MUST 10); `blocked` already covers over-budget,
    since autonomous_cycle_record_usage sets state='blocked' on overage —
    there is no separate "over-budget" state to query.

    One list call, not three state-filtered ones: cycle volume is low
    enough that filtering client-side is simpler than three round-trips,
    and matches how `doing`/`review_waits` above filter task_list results
    in Python rather than adding a bespoke tool-side filter.
    """
    result = await _safe("autonomous_cycle_list", default={"cycles": []}, limit=200)
    if isinstance(result, dict) and result.get("error"):
        return [], [result["error"]], []

    items: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    for cycle in result.get("cycles", []) if isinstance(result, dict) else []:
        state = cycle.get("state")
        if state not in _CYCLE_ATTENTION_SEVERITY:
            continue
        cycle_id = cycle.get("id")
        task_id = cycle.get("task_id")
        # task_id only included when set: _source_subject's first branch
        # renders "task:{task_id}" whenever the key is present, even if the
        # value is None ("task:None") — a cycle with no bound task should
        # fall through to the agnostic "unknown" subject instead.
        source: dict[str, Any] = {"cycle_id": cycle_id}
        if task_id is not None:
            source["task_id"] = task_id
        item = _attention_item(
            kind="autonomous_cycle_stopped",
            severity=_CYCLE_ATTENTION_SEVERITY[state],
            source=source,
            age_s=_age_s(cycle.get("updated_at"), now),
            title=f"cycle {cycle_id}" + (f" (task #{task_id})" if task_id else ""),
            reason=cycle.get("stop_reason") or f"cycle is {state}",
        )
        if "acceptance smoke cleanup" in (cycle.get("stop_reason") or "").lower():
            item["severity"] = "info"
            item["reason"] = "intentional acceptance-smoke cleanup; history only"
            informational.append(item)
        else:
            items.append(item)
    return items, [], informational


# Evidence gaps older than this drop out of the attention pane; the ledger
# keeps them for good. Matches the identity-drift window.
_EVIDENCE_GAP_ATTENTION_WINDOW = timedelta(days=7)
_EVIDENCE_GAP_ATTENTION_LIMIT = 50


async def _evidence_gap_items(now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Recent ``audit.evidence.gap`` ledger events (task #1672, H12).

    A gap means an action committed (or a high-tier approval was refused)
    without the signed evidence it should carry. Without this source the only
    trace was a ledger row and a log warning. A refused high-tier approval is
    ``high``: the operator cannot approve it until signing works. A resolution
    that proceeded without its stamp is ``medium``.
    """
    from ..tools.approvals import CHANNEL_EVIDENCE_GAP

    try:
        rows = await anyio.to_thread.run_sync(
            lambda: event_log.query_events(
                channels=[CHANNEL_EVIDENCE_GAP],
                limit=_EVIDENCE_GAP_ATTENTION_LIMIT,
                newest_first=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - reported in-band, never swallowed
        return [], [f"evidence gap query failed: {str(exc)[:200]}"]

    items: list[dict[str, Any]] = []
    for row in rows:
        created = _parse_dt(row.get("created_at"))
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if now - created > _EVIDENCE_GAP_ATTENTION_WINDOW:
                continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = payload.get("kind") or "event"
        ref_id = payload.get("ref_id")
        outcome = payload.get("outcome") or "proceeded"
        tier = payload.get("tier")
        error_type = payload.get("error_type") or "unknown"
        tool = payload.get("tool")
        title = f"{kind} {ref_id}" + (f" ({tool})" if tool else "")
        refused = outcome == "refused"
        if refused:
            reason = f"{tier or 'high'}-tier {kind} refused: producer stamp missing ({error_type})"
        elif kind == "relay_delivery":
            # public_relay._record_delivery_gap: the channel accepted the message
            # but relay.public.sent was not written, so the attempt stays open.
            reason = (
                "relay message delivered but its relay.public.sent record was not "
                f"written; the attempt stays unresolved ({error_type})"
            )
        else:
            reason = (
                f"{kind} proceeded without signed evidence at "
                f"{payload.get('stage') or 'unknown stage'} ({error_type})"
            )
        items.append(
            _attention_item(
                kind="evidence_gap",
                severity="high" if refused else "medium",
                source={"evidence_gap": row.get("ulid")},
                age_s=_age_s(row.get("created_at"), now),
                title=title,
                reason=reason,
            )
        )
    return items, []


def _zulip_site() -> str:
    return zulip_tools.zulip_site_status()["site"] or ""


def _zulip_stream_url(channel: str, topic: str | None = None) -> str:
    site = _zulip_site()
    if not site:
        return ""
    canonical = zulip_tools._STREAM_CANONICAL.get(channel, channel)
    base = f"{site}/#narrow/stream/{quote(canonical)}"
    if topic:
        return f"{base}/topic/{quote(topic.replace(' ', '-'))}"
    return base


def _strip_zulip_html(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content or "")
    return re.sub(r"\s+", " ", text).strip()[:200]


def _task_id_from_topic(topic: str | None) -> int | None:
    if not topic:
        return None
    match = _TASK_TOPIC_RE.match(topic.strip())
    return int(match.group(1)) if match else None


def _zulip_sender_is_bot(payload: dict[str, Any]) -> bool:
    email = str(payload.get("sender_email") or "")
    if "-bot@" in email:
        return True
    name = str(payload.get("sender_full_name") or "")
    return name in zulip_worker.BOT_IDENTITIES


def _normalize_zulip_inbound_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    channel = zulip_tools._logical_channel(str(payload.get("channel") or ""))
    topic = str(payload.get("topic") or "")
    task_id = _task_id_from_topic(topic)
    actionable = payload.get("actionable_for")
    if not isinstance(actionable, list):
        actionable = []
    protocol_channel = channel in _ZULIP_PROTOCOL_CHANNELS
    protocol_ok = (not protocol_channel) or task_id is not None
    return {
        "ulid": event.get("ulid"),
        "created_at": event.get("created_at"),
        "message_id": payload.get("message_id"),
        "channel": channel,
        "topic": topic,
        "task_id": task_id,
        "sender": payload.get("sender_full_name") or payload.get("sender_email") or "",
        "content_preview": _strip_zulip_html(str(payload.get("content") or "")),
        "actionable_for": actionable,
        "target_reason": payload.get("target_reason"),
        "is_bot": _zulip_sender_is_bot(payload),
        "protocol_ok": protocol_ok,
        "zulip_url": _zulip_stream_url(channel, topic or None),
    }


def _recent_zulip_inbound_rows(*, limit: int = ZULIP_INBOUND_SCAN_LIMIT) -> list[dict[str, Any]]:
    rows = event_log.query_events(
        channels=["zulip.inbound"],
        limit=limit,
        newest_first=True,
    )
    return [_normalize_zulip_inbound_event(event) for event in rows]


def _zulip_coordination_stats(
    rows: list[dict[str, Any]], *, now: datetime, hours: float = 24.0
) -> dict[str, int]:
    cutoff = now - timedelta(hours=hours)
    inbound_24h = 0
    actionable_24h = 0
    protocol_total = 0
    protocol_compliant = 0
    for row in rows:
        created = _parse_dt(row.get("created_at"))
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            continue
        inbound_24h += 1
        if row.get("actionable_for"):
            actionable_24h += 1
        if row.get("channel") in _ZULIP_PROTOCOL_CHANNELS:
            protocol_total += 1
            if row.get("protocol_ok"):
                protocol_compliant += 1
    return {
        "inbound_24h": inbound_24h,
        "actionable_24h": actionable_24h,
        "protocol_total_24h": protocol_total,
        "protocol_compliant_24h": protocol_compliant,
    }


async def _zulip_attention_items(
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Zulip worker health and human coordination messages needing follow-up."""
    site_status = zulip_tools.zulip_site_status()
    if site_status["configuration_error"]:
        # Retired (the default) with no site set is not a failed query: there
        # is simply no Zulip source to scan. A site that is set but invalid,
        # or an un-retired Zulip without one, is still reported.
        if zulip_tools.zulip_retired() and not os.environ.get("UA_ZULIP_SITE", "").strip():
            return [], [], []
        return [], [], [site_status["configuration_error"]]
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    try:
        health = await zulip_worker_health_payload()
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"zulip_worker_health failed: {str(exc)[:200]}"]

    status = health.get("status")
    if status == "error":
        items.append(
            _attention_item(
                kind="zulip_worker_degraded",
                severity="high",
                source={"zulip_worker": status},
                age_s=_age_s(health.get("last_successful_poll"), now),
                title="Zulip worker",
                reason=health.get("last_error") or "one or more bot/channel polls are failing",
            )
        )
    elif status == "stale":
        items.append(
            _attention_item(
                kind="zulip_worker_stale",
                severity="medium",
                source={"zulip_worker": status},
                age_s=_age_s(health.get("last_successful_poll"), now),
                title="Zulip worker",
                reason="Zulip polling has gone quiet beyond the stale threshold",
            )
        )

    try:
        rows = await anyio.to_thread.run_sync(_recent_zulip_inbound_rows)
    except Exception as exc:  # noqa: BLE001
        return items, informational, [f"zulip inbound query failed: {str(exc)[:200]}"]

    lookback = now - timedelta(hours=ZULIP_INBOUND_LOOKBACK_HOURS)
    seen_message_ids: set[int] = set()
    for row in rows:
        message_id = row.get("message_id")
        if not isinstance(message_id, int) or message_id in seen_message_ids:
            continue
        created = _parse_dt(row.get("created_at"))
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < lookback:
            continue
        seen_message_ids.add(message_id)

        source = {
            "zulip_message_id": message_id,
            "channel": row.get("channel"),
            "topic": row.get("topic"),
            "zulip_url": row.get("zulip_url"),
        }
        task_id = row.get("task_id")
        if task_id is not None:
            source["task_id"] = task_id

        actionable = row.get("actionable_for") or []
        if actionable:
            items.append(
                _attention_item(
                    kind="zulip_actionable",
                    severity="medium",
                    source=source,
                    age_s=_age_s(row.get("created_at"), now),
                    title=f"#{row.get('channel')}/{row.get('topic') or '—'}",
                    reason=(
                        f"Zulip message targets {', '.join(actionable)}"
                        + ("" if task_id else "; topic is not task-<id>")
                    ),
                )
            )
            continue

        if row.get("is_bot"):
            continue

        channel = str(row.get("channel") or "")
        if channel == "requests":
            items.append(
                _attention_item(
                    kind="zulip_request",
                    severity="medium",
                    source=source,
                    age_s=_age_s(row.get("created_at"), now),
                    title=f"#{channel}/{row.get('topic') or '—'}",
                    reason=(
                        "human Zulip request without [target:] routing — "
                        "create a task and use a task-<id> topic"
                    ),
                )
            )
        elif channel in {"coord", "review"} and not row.get("protocol_ok"):
            informational.append(
                _attention_item(
                    kind="zulip_protocol_drift",
                    severity="info",
                    source=source,
                    age_s=_age_s(row.get("created_at"), now),
                    title=f"#{channel}/{row.get('topic') or '—'}",
                    reason="coordination message missing task-<id> topic convention",
                )
            )

    return items, informational, errors


async def _executor_health_items(now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Flag degraded or failing executor service state in attention items."""
    try:
        health = await health_payload()
    except Exception as exc:  # noqa: BLE001
        return [], [f"health_payload failed: {str(exc)[:200]}"]

    items: list[dict[str, Any]] = []
    if isinstance(health, dict) and health.get("error"):
        return [], [health["error"]]
    checks = health.get("checks", []) if isinstance(health, dict) else []
    for check in checks:
        if check.get("capability") == "active_executor_service":
            status = check.get("status")
            # system_doctor's contract uses uppercase capability-health states
            # (tools/doctor.py:_unit_check, _active_executor_check) — this used to
            # compare against non-contract lowercase values that never matched,
            # silently dropping executor_degraded whenever the executor was down
            # (task #445 / PR #197 review). Only MISCONFIGURED is attention-worthy
            # here: the unit is active but cannot authenticate to the hub, an
            # actual break. DORMANT covers "not installed on this host" too — the
            # normal state on any machine that isn't running the executor — so
            # flagging it produced a false positive on every such host. UNKNOWN is
            # the deliberate self-call skip inside the hub process, not a failure.
            if status == "MISCONFIGURED":
                items.append(
                    _attention_item(
                        kind="executor_degraded",
                        severity="high",
                        source={"executor": "service"},
                        age_s=0,
                        title="active_executor_service",
                        reason=check.get("message")
                        or check.get("detail")
                        or f"executor service status is {status}",
                    )
                )
    return items, []


async def attention_payload() -> dict:
    """Everything waiting on the operator, derived from existing read-only tools."""
    now = datetime.now(UTC)
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []

    drift_items, drift_errors = await _identity_drift_items(now)
    items.extend(drift_items)
    errors.extend(drift_errors)

    stale_agent_items, stale_agent_errors, stale_agent_info = await _stale_agent_items(now)
    items.extend(stale_agent_items)
    informational.extend(stale_agent_info)
    errors.extend(stale_agent_errors)

    cycle_items, cycle_errors, cycle_info = await _autonomous_cycle_items(now)
    items.extend(cycle_items)
    informational.extend(cycle_info)
    errors.extend(cycle_errors)

    exec_items, exec_errors = await _executor_health_items(now)
    items.extend(exec_items)
    errors.extend(exec_errors)

    gap_items, gap_errors = await _evidence_gap_items(now)
    items.extend(gap_items)
    errors.extend(gap_errors)

    zulip_items, zulip_info, zulip_errors = await _zulip_attention_items(now)
    items.extend(zulip_items)
    informational.extend(zulip_info)
    errors.extend(zulip_errors)

    review_waits = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="full",
        notes_contain="[waiting-for: review]",
    )
    if isinstance(review_waits, dict) and review_waits.get("error"):
        errors.append(review_waits["error"])
    # Also surface markerless waiting_review rows (typed status without the
    # legacy note marker). Merge by id so a task is not double-counted.
    typed_waiting = await _safe(
        "task_list",
        default={"tasks": []},
        status="waiting_review",
        fields="full",
    )
    if isinstance(typed_waiting, dict) and typed_waiting.get("error"):
        errors.append(typed_waiting["error"])
    review_by_id: dict[Any, dict[str, Any]] = {}
    for task in review_waits.get("tasks", []) if isinstance(review_waits, dict) else []:
        if task.get("id") is not None:
            review_by_id[task["id"]] = task
    for task in typed_waiting.get("tasks", []) if isinstance(typed_waiting, dict) else []:
        if task.get("id") is not None:
            review_by_id.setdefault(task["id"], task)
    for task in review_by_id.values():
        status = task.get("status")
        # Historical [review: approved]/merged-upstream] only suppresses when
        # the task is already terminal — a reopened cycle can keep old markers.
        if status == "done":
            continue
        if status is not None and status not in {"open", "doing", "waiting_review", "blocked"}:
            continue
        decision_kind = "awaiting_operator" if status == "waiting_review" else "review_wait"
        items.append(
            _attention_item(
                kind=decision_kind,
                severity="medium",
                source={"task_id": task.get("id")},
                source_host=task.get("source_host"),
                hub_origin=task.get("hub_origin"),
                age_s=_age_s(task.get("updated_at") or task.get("created_at"), now),
                title=task.get("title"),
                reason=(
                    "awaiting the operator's review decision"
                    if decision_kind == "awaiting_operator"
                    else "task is waiting for human review"
                ),
            )
        )

    plan_approvals = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="compact",
        notes_contain="[waiting-for: plan-approval]",
    )
    if isinstance(plan_approvals, dict) and plan_approvals.get("error"):
        errors.append(plan_approvals["error"])
    for task in plan_approvals.get("tasks", []) if isinstance(plan_approvals, dict) else []:
        if task.get("status") == "done":
            continue
        items.append(
            _attention_item(
                kind="plan_approval_wait",
                severity="high",
                source={"task_id": task.get("id")},
                source_host=task.get("source_host"),
                hub_origin=task.get("hub_origin"),
                age_s=_age_s(task.get("updated_at") or task.get("created_at"), now),
                title=task.get("title"),
                reason="plan is waiting for approval",
            )
        )

    human_merges = await _safe(
        "task_list",
        default={"tasks": []},
        status="all",
        fields="compact",
        notes_contain="[waiting-for: human-merge]",
    )
    if isinstance(human_merges, dict) and human_merges.get("error"):
        errors.append(human_merges["error"])
    for task in human_merges.get("tasks", []) if isinstance(human_merges, dict) else []:
        if task.get("status") == "done":
            continue
        items.append(
            _attention_item(
                kind="human_merge_wait",
                severity="high",
                source={"task_id": task.get("id")},
                source_host=task.get("source_host"),
                hub_origin=task.get("hub_origin"),
                age_s=_age_s(task.get("updated_at") or task.get("created_at"), now),
                title=task.get("title"),
                reason="task is waiting for human merge",
            )
        )

    blocked_res = await _safe(
        "task_list",
        default={"tasks": []},
        status="blocked",
        fields="full",
    )
    if isinstance(blocked_res, dict) and blocked_res.get("error"):
        errors.append(blocked_res["error"])
    for task in blocked_res.get("tasks", []) if isinstance(blocked_res, dict) else []:
        notes = task.get("notes") or ""
        # Prefer an explicit owner directive over incidental prose keywords.
        owner_m = re.search(
            r"(?:decision-owner|blocked-owner|awaiting):\s*([^\n]+)",
            notes,
            re.IGNORECASE,
        )
        if owner_m:
            owner = owner_m.group(1).strip().lower()
            if owner in {"human", "operator", config.operator_label().lower()} | {
                alias.lower() for alias in config.operator_aliases()
            }:
                decision_type = "the operator's decision"
            elif any(word in owner for word in ("hardware", "webgpu", "apple", "device", "env")):
                decision_type = "environment/hardware decision"
            else:
                decision_type = "developer fix"
        else:
            decision_type = "developer fix"
        blocker = re.search(r"unblock-condition:\s*([^\n]+)", notes, re.IGNORECASE)
        blocker_text = blocker.group(1).strip() if blocker else f"task #{task.get('id')} is blocked"
        items.append(
            _attention_item(
                kind="blocked_task",
                severity="medium",
                source={"task_id": task.get("id")},
                source_host=task.get("source_host"),
                hub_origin=task.get("hub_origin"),
                age_s=_age_s(task.get("updated_at") or task.get("created_at"), now),
                title=task.get("title"),
                reason=f"{blocker_text}; requires {decision_type}",
            )
        )
        items[-1]["decision_type"] = decision_type

    approvals = await approvals_payload()
    if isinstance(approvals, dict) and approvals.get("error"):
        errors.append(approvals["error"])
    for approval in approvals.get("pending", []) if isinstance(approvals, dict) else []:
        tool = approval.get("tool") or approval.get("tool_name") or "approval"
        items.append(
            _attention_item(
                kind="approval",
                severity="high",
                source={"approval_id": approval.get("id")},
                source_host=approval.get("source_host"),
                hub_origin=approval.get("hub_origin"),
                age_s=_age_s(approval.get("created_at") or approval.get("requested_at"), now),
                title=str(tool),
                reason=f"{tool} is waiting for approval",
            )
        )

    anomalies = await anomalies_payload()
    if isinstance(anomalies, dict) and anomalies.get("error"):
        errors.append(anomalies["error"])
    for anomaly in anomalies.get("anomalies", []) if isinstance(anomalies, dict) else []:
        if (
            anomaly.get("status") == "cleared"
            or anomaly.get("cleared") is True
            or anomaly.get("active") is False
        ):
            continue
        rule = anomaly.get("rule") or "anomaly"
        subject = anomaly.get("subject") or "unknown"
        severity = anomaly.get("severity") or "critical"
        item = _attention_item(
            kind="anomaly",
            severity=severity,
            source={"anomaly": f"{rule}:{subject}"},
            source_host=anomaly.get("source_host"),
            hub_origin=anomaly.get("hub_origin"),
            age_s=_age_s(anomaly.get("created_at") or anomaly.get("updated_at"), now),
            title=str(rule),
            reason=anomaly.get("detail") or f"{rule} needs attention",
        )
        # info-severity anomalies (e.g. idle_lane) stay out of stuck/attention
        if severity == "info":
            informational.append(item)
        else:
            items.append(item)

    candidates, worktree_errors = await _surveyed_worktrees()
    errors.extend(worktree_errors)
    stale_worktree_items: list[dict[str, Any]] = []
    for worktree in candidates:
        path = worktree.get("path")
        reason = worktree.get("reason") or "stale"
        stale_worktree_items.append(
            _attention_item(
                kind="stale_worktree",
                severity="low",
                source={"worktree": path},
                source_host=worktree.get("source_host"),
                hub_origin=worktree.get("hub_origin"),
                age_s=_age_s(worktree.get("last_modified"), now),
                title=path,
                reason=f"worktree {reason}",
            )
        )
    if len(stale_worktree_items) > _STALE_WORKTREE_ATTENTION_CAP:
        overflow = len(stale_worktree_items) - _STALE_WORKTREE_ATTENTION_CAP
        items.extend(stale_worktree_items[:_STALE_WORKTREE_ATTENTION_CAP])
        informational.append(
            _attention_item(
                kind="stale_worktree_overflow",
                severity="low",
                source={"worktree": "overflow"},
                age_s=0,
                title=f"{overflow} more stale worktree(s)",
                reason=(
                    f"capped at {_STALE_WORKTREE_ATTENTION_CAP} in attention; "
                    "run cleanup_stale_worktrees(dry_run=True) — not auto-deleted"
                ),
            )
        )
    else:
        items.extend(stale_worktree_items)

    doing = await _safe("task_list", default={"tasks": []}, status="doing", fields="compact")
    if isinstance(doing, dict) and doing.get("error"):
        errors.append(doing["error"])
    for task in doing.get("tasks", []) if isinstance(doing, dict) else []:
        lock_expires = _parse_dt(task.get("lock_expires_at"))
        if lock_expires is None:
            continue
        if lock_expires.tzinfo is None:
            lock_expires = lock_expires.replace(tzinfo=UTC)
        if lock_expires >= now:
            continue
        owner = task.get("claimed_by") or "unknown agent"
        items.append(
            _attention_item(
                kind="stale_claim",
                severity="medium",
                source={"task_id": task.get("id")},
                source_host=task.get("source_host"),
                hub_origin=task.get("hub_origin"),
                age_s=max(0, int((now - lock_expires).total_seconds())),
                title=task.get("title"),
                reason=f"{owner} claim expired",
            )
        )

    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items.sort(key=lambda item: (severity_rank.get(item["severity"], 1), -(item["age_s"] or 0)))

    # Suppress items a wrist/operator has acknowledged (task #779). A non-expired
    # ``mobile.attention.ack`` event drops the matching item from every attention
    # consumer (web, digest, mobile) until its TTL lapses, then it resurfaces.
    from ..projection import attention_ack

    items, _suppressed = await anyio.to_thread.run_sync(attention_ack.filter_acknowledged, items)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {
        "items": items,
        "informational": informational,
        "counts": counts,
        "total": len(items),
        "errors": errors,
        "omitted": list(_ATTENTION_OMITTED),
        "needs_operator": await anyio.to_thread.run_sync(needs_operator.payload),
    }


async def digest_payload() -> dict:
    """Decision-grade daily exception-only digest.

    Answers:
    1. What is waiting on the operator (approvals, reviews, plan approvals, human merges)
    2. What is stuck (blocked tasks, stopped autonomous cycles, stale claims, anomalies, degraded executor)
    3. What is running (active sessions, active doing tasks, active cycles)
    4. Who owns each lane (lane ownership mapping across agents)
    5. What changed today (tasks completed today, recent finished sessions)
    6. Distinct and evidenced states (status, evidence, scanned_sources, errors, omitted)
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    attention = await attention_payload()
    agents_res = await agents_payload()
    sessions_res = await sessions_payload(limit=100)
    cycles_res = await cycles_payload(limit=50)
    health_res = await health_payload()

    items = attention.get("items", [])
    errors = list(attention.get("errors", []))
    if isinstance(agents_res, dict) and agents_res.get("error"):
        errors.append(agents_res["error"])
    if isinstance(sessions_res, dict) and sessions_res.get("error"):
        errors.append(sessions_res["error"])
    if isinstance(cycles_res, dict) and cycles_res.get("error"):
        errors.append(cycles_res["error"])
    if isinstance(health_res, dict) and health_res.get("error"):
        errors.append(health_res["error"])

    # Group attention items by kind
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        sections.setdefault(item["kind"], []).append(item)

    operator_kinds = {
        "approval",
        "review_wait",
        "awaiting_operator",
        config.LEGACY_OPERATOR_ATTENTION_KIND,
        "plan_approval_wait",
        "human_merge_wait",
        "inconsistent_completion",
        # A missing signed record (task #1672): the operator has to fix signing
        # or reconcile the record; nothing retries it on its own.
        "evidence_gap",
    }
    stuck_kinds = {
        "blocked_task",
        "autonomous_cycle_stopped",
        "stale_claim",
        "anomaly",
        "executor_degraded",
        "identity_drift",
        "stale_agent",
        "stale_worktree",
    }

    waiting_on_operator = [it for it in items if it.get("kind") in operator_kinds]
    stuck = [it for it in items if it.get("kind") in stuck_kinds]

    # Running work
    doing_tasks_res = await _safe("task_list", default={"tasks": []}, status="doing", fields="full")
    if isinstance(doing_tasks_res, dict) and doing_tasks_res.get("error"):
        errors.append(doing_tasks_res["error"])
    doing_tasks = doing_tasks_res.get("tasks", []) if isinstance(doing_tasks_res, dict) else []
    running_tasks = []
    for task in doing_tasks:
        owner, owner_source = _task_owner(task)
        running_tasks.append(
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "claimed_by": owner,
                "claimed_by_source": owner_source,
                # Structured column only, for lane-ownership matching below — a
                # `claimed-by:` note is a descriptive hint that can outlive claim
                # expiry/release and must not be treated as a current claim.
                "structured_claimed_by": task.get("claimed_by"),
                "project": task.get("project", "unified-agent"),
                "worktree": task.get("worktree_path"),
                "branch": task.get("branch"),
            }
        )

    open_sessions = [
        {
            "id": s.get("id"),
            "agent": s.get("agent"),
            "status": s.get("status"),
            "kind": s.get("kind"),
            "task_id": s.get("task_id"),
        }
        for s in (sessions_res.get("sessions", []) if isinstance(sessions_res, dict) else [])
        if s.get("status") in ("starting", "running", "waiting")
    ]

    active_cycles = [
        {
            "id": c.get("id"),
            "agent": c.get("agent"),
            "state": c.get("state"),
            "task_id": c.get("task_id"),
            "task_type": c.get("task_type"),
        }
        for c in (cycles_res.get("cycles", []) if isinstance(cycles_res, dict) else [])
        if c.get("state") in ("running", "paused")
    ]

    running = {
        "tasks": running_tasks,
        "sessions": open_sessions,
        "cycles": active_cycles,
        "total": len(running_tasks) + len(open_sessions) + len(active_cycles),
    }

    # Lane ownership: configured/observed agents, plus anyone holding a
    # structured claim (open roster has no compiled placeholders).
    agents_list = agents_res.get("agents", []) if isinstance(agents_res, dict) else []
    lane_ownership: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for ag in agents_list:
        lbl = ag.get("label", "")
        ag_tasks = [t for t in running_tasks if t.get("structured_claimed_by") == lbl]
        ag_sessions = [s for s in open_sessions if s.get("agent") == lbl]
        lane_ownership.append(
            {
                "agent": lbl,
                "status": ag.get("status", "idle"),
                "host": ag.get("host"),
                "active_task": ag_tasks[0] if ag_tasks else None,
                "active_tasks_count": len(ag_tasks),
                "open_sessions_count": len(ag_sessions),
            }
        )
        if lbl:
            seen_labels.add(lbl)
    for task in running_tasks:
        lbl = task.get("structured_claimed_by") or ""
        if not lbl or lbl in seen_labels:
            continue
        seen_labels.add(lbl)
        matching = [t for t in running_tasks if t.get("structured_claimed_by") == lbl]
        lane_ownership.append(
            {
                "agent": lbl,
                "status": "working",
                "host": None,
                "active_task": matching[0],
                "active_tasks_count": len(matching),
                "open_sessions_count": 0,
            }
        )

    # Changed today. Filtered in SQL by completed_at rather than paged through
    # task_list's default 100-row, priority-ordered result — that ordering has
    # no relationship to completion time, so once more than 100 tasks are done
    # overall, tasks completed today can fall outside the page and undercount.
    all_tasks_res = await _safe(
        "task_list",
        default={"tasks": []},
        status="done",
        fields="full",
        completed_after=today_start.isoformat(),
        limit=500,
    )
    if isinstance(all_tasks_res, dict) and all_tasks_res.get("error"):
        errors.append(all_tasks_res["error"])
    all_done = all_tasks_res.get("tasks", []) if isinstance(all_tasks_res, dict) else []
    completed_today: list[dict[str, Any]] = []
    for t in all_done:
        completed_at = _parse_dt(t.get("completed_at"))
        if completed_at is not None:
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=UTC)
            if completed_at >= today_start:
                owner, owner_source = _task_owner(t)
                completed_today.append(
                    {
                        "id": t.get("id"),
                        "title": t.get("title"),
                        "completed_at": t.get("completed_at"),
                        "claimed_by": owner,
                        "claimed_by_source": owner_source,
                        "project": t.get("project", "unified-agent"),
                    }
                )

    changed_today = {
        "tasks_completed": completed_today,
        "total_completed": len(completed_today),
    }

    # Distinct & evidenced states
    if errors and not items and not running["total"] and not changed_today["total_completed"]:
        status = "unavailable"
        evidence = f"digest queries failed: {'; '.join(errors)}"
    elif errors:
        status = "error"
        evidence = f"partial data: {len(items)} exceptions, {running['total']} running, {len(errors)} query error(s)"
    elif items:
        status = "action_needed"
        evidence = (
            f"{len(items)} exceptions ({len(waiting_on_operator)} waiting on the operator, {len(stuck)} stuck); "
            f"{len(running_tasks)} task(s) running; {len(completed_today)} completed today"
        )
    elif health_res.get("summary", {}).get("misconfigured", 0) > 0:
        status = "degraded"
        evidence = f"0 exceptions, but system health is degraded; {len(running_tasks)} task(s) running; {len(completed_today)} completed today"
    elif health_res.get("summary", {}).get("degraded", 0) > 0:
        status = "degraded"
        evidence = f"0 exceptions, but system health is degraded; {len(running_tasks)} task(s) running; {len(completed_today)} completed today"
    else:
        status = "clean"
        evidence = f"clean state: 0 exceptions; {len(running_tasks)} task(s) running; {len(completed_today)} completed today"

    counts = attention.get("counts", {})

    # Event-driven supervisor wakes (task #1028): completions/blocks already
    # recorded on agent.supervisor.wake — no task-board poll for this slice.
    supervisor_wakes: dict[str, Any] = {"wakes": [], "count": 0}
    try:
        from ..supervision import recent_supervisor_wakes

        supervisor_wakes = recent_supervisor_wakes(limit=50)
    except Exception as exc:
        errors.append(f"supervisor_wakes: {exc}")

    return {
        "generated_at": now.isoformat(),
        "status": status,
        "evidence": evidence,
        "waiting_on_operator": waiting_on_operator,
        config.LEGACY_OPERATOR_DIGEST_KEY: waiting_on_operator,
        "stuck": stuck,
        "running": running,
        "lane_ownership": lane_ownership,
        "changed_today": changed_today,
        "supervisor_wakes": supervisor_wakes,
        "sections": [
            {"kind": kind, "count": len(kind_items), "items": kind_items}
            for kind, kind_items in sections.items()
        ],
        "counts": counts,
        "total": attention.get("total", 0),
        "scanned_sources": [
            "tasks",
            "approvals",
            "anomalies",
            "autonomous_cycles",
            "agents",
            "worktrees",
            "executor_health",
            "supervisor_wakes",
            "evidence_gaps",
        ],
        "errors": errors,
        "omitted": attention.get("omitted", []),
    }


def _backup_readiness(buckets: list, sections: dict) -> dict:
    """Derive a restore-readiness signal from the buckets section.

    Deliberately conservative: when the buckets section could not be read this
    reports `unknown`, never `none`. "We could not look" and "there are no
    backups" demand opposite operator responses, so they must not render the
    same.
    """
    if sections.get("buckets") != "ok":
        return {
            "state": "unknown",
            "detail": "buckets section unavailable; backup readiness not determined",
            "buckets": [],
        }
    backup_buckets = [
        b for b in buckets if isinstance(b, dict) and "backup" in str(b.get("name", "")).lower()
    ]
    if not backup_buckets:
        return {
            "state": "none",
            "detail": "no backup bucket found in this project",
            "buckets": [],
        }
    return {
        "state": "present",
        "detail": f"{len(backup_buckets)} backup bucket(s) present",
        "buckets": [
            {
                "name": b.get("name"),
                "location": b.get("location"),
                "versioning_enabled": b.get("versioning_enabled", False),
            }
            for b in backup_buckets
        ],
    }


async def cloud_payload(force: bool = False) -> dict:
    """Read-only GCP health, instance, and budget state for the Control Center.

    Read-first by construction: this calls `gcloud_inventory` only. It never
    starts, stops, or otherwise mutates an instance, and performs no
    remediation -- recovery stays behind the approval-gated `gcloud_ops` path.

    Every field distinguishes "read successfully" from "could not read". A
    caller must be able to tell an idle project from an unreadable one; the
    two look identical if you only inspect the lists.
    """
    global _cloud_cache

    from mcp.server.mcpserver.exceptions import ToolError

    from ..tools.gcloud import _configured_cloud_target

    now = time.time()
    try:
        cloud_target = _configured_cloud_target()
        configuration_error = None
    except ToolError as exc:
        cloud_target = None
        configuration_error = str(exc)
    if cloud_target is None or (_cloud_cache and _cloud_cache[1] != cloud_target):
        _cloud_cache = None
    # Configuration is checked before the cache: disabling an integration
    # must not surface a formerly healthy reading or invoke ambient gcloud.
    if (
        configuration_error is None
        and not force
        and _cloud_cache
        and (now - _cloud_cache[0]) < _CLOUD_CACHE_S
    ):
        cached = dict(_cloud_cache[2])
        cached["cached_age_s"] = round(now - _cloud_cache[0], 1)
        return cached

    unconfigured = {
        "status": "unknown",
        "project_id": None,
        "instances": [],
        "buckets": [],
        "budgets": [],
        "sections": {},
        "errors": [],
    }
    if configuration_error is not None:
        inv = {**unconfigured, "error": configuration_error}
    else:
        inv = await _safe("gcloud_inventory", project=cloud_target[0], default=unconfigured)

    sections = inv.get("sections") or {}
    errors = list(inv.get("errors") or [])
    # _safe reports a tool-level failure in-band via an `error` key. Fold it
    # into the same list the per-section failures use, so one renderer covers
    # both and neither can be missed.
    if inv.get("error"):
        errors.append({"section": "gcloud_inventory", "error": inv["error"]})

    instances_readable = sections.get("instances") == "ok"
    instances = inv.get("instances") or []

    # A credential that could not read every section it was asked for is a
    # credential whose silence proves nothing. The hub's service account
    # (ua-backup-uploader) cannot even describe the project, so treating its
    # empty instance list as "there is no worker" would report a RUNNING VM as
    # deleted. Absence is only claimed on a fully-readable inventory.
    partial_coverage = any(state != "ok" for state in sections.values())
    under_privileged = partial_coverage or bool(inv.get("error"))

    worker_instances = [i for i in instances if cloud_target and i.get("name") == cloud_target[1]]
    if not instances_readable or len(worker_instances) > 1:
        instance_state = "unknown"
    elif not worker_instances:
        instance_state = "unknown" if under_privileged else "absent"
    else:
        # Compute Engine also reports transitional states (PROVISIONING,
        # STAGING, STOPPING, SUSPENDING, REPAIRING). Those are neither up nor
        # down; readable transitions stay distinct from an unknown reading.
        worker_status = str(worker_instances[0].get("status", "")).upper()
        if worker_status == "RUNNING":
            instance_state = "running"
        elif worker_status in {"TERMINATED", "STOPPED", "SUSPENDED"}:
            instance_state = "stopped"
        elif worker_status in {"PROVISIONING", "STAGING", "STOPPING", "SUSPENDING", "REPAIRING"}:
            instance_state = "transitional"
        else:
            instance_state = "unknown"

    budgets_readable = sections.get("budgets") == "ok"

    status = inv.get("status") or "unknown"
    if inv.get("error"):
        status = "unknown"

    payload = {
        "status": status,
        "project_id": inv.get("project_id"),
        "sections": sections,
        # True when at least one requested section could not be read. An
        # operator seeing this knows the whole reading is partial, and that the
        # usual cause is missing IAM on the hub's service account rather than a
        # change in the infrastructure. `compute.viewer` is what the instances
        # section needs; see docs/cloud-agent5.md.
        "partial_coverage": partial_coverage,
        "instance_state": instance_state,
        "instances": instances,
        "budgets": {
            "state": "ok" if budgets_readable else "unknown",
            "items": inv.get("budgets") or [],
        },
        "backups": _backup_readiness(inv.get("buckets") or [], sections),
        "errors": errors,
        # Read-only by construction; surfaced so the UI can say so rather than
        # leaving an operator to wonder whether opening the pane acted on anything.
        "remediation": "manual",
        "checked_at": datetime.now(UTC).isoformat(),
        "cached_age_s": 0.0,
    }

    if configuration_error is None:
        _cloud_cache = (now, cloud_target, payload)
    return dict(payload)


def _tool_cache_age(result: object) -> float:
    """Seconds the doctor tool had already held ``result`` in its own cache."""
    age = result.get("cache_age_s") if isinstance(result, dict) else None
    return float(age) if isinstance(age, int | float) and age > 0 else 0.0


def _doctor_reading(entry: tuple, now: float) -> dict:
    out = dict(entry[1])
    out["cached_age_s"] = round(now - entry[0], 1)
    return out


def _start_doctor_call_locked(call: _DoctorCall) -> None:
    """Give ``call`` the next start generation and the in-flight slot (lock held)."""
    global _doctor_generation, _doctor_inflight
    _doctor_generation += 1
    call.generation = _doctor_generation
    _doctor_inflight = call


def _finish_doctor_call(
    call: _DoctorCall, outcome: tuple[float, dict] | None, error: BaseException | None
) -> None:
    """Publish ``call``'s outcome and, atomically, promote the queued call."""
    global _doctor_cache, _doctor_inflight, _doctor_pending_forced
    promoted: _DoctorCall | None = None
    with _doctor_state_lock:
        if outcome is not None and (_doctor_cache is None or call.generation >= _doctor_cache[2]):
            # A call that started earlier but finished later never replaces a
            # newer reading (a forced one, typically).
            _doctor_cache = (outcome[0], outcome[1], call.generation)
        if _doctor_inflight is call:
            _doctor_inflight = None
            promoted = _doctor_pending_forced
            if promoted is not None:
                _doctor_pending_forced = None
                _start_doctor_call_locked(promoted)
    if promoted is not None and not promoted.promoted.done():
        promoted.promoted.set_result(None)
    if error is not None:
        call.future.set_exception(error)
    else:
        call.future.set_result(outcome)


async def _run_doctor_call(call: _DoctorCall) -> None:
    try:
        # force reaches the tool too, or it would hand back its own shared
        # report (task #1850).
        result = await _safe(
            "system_doctor",
            default={"checks": [], "summary": {}},
            **({"refresh": True} if call.forced else {}),
        )
    except BaseException as exc:  # noqa: BLE001 - handed to every waiter
        _finish_doctor_call(call, None, exc)
        return
    # Stamp when the reading was taken, not when this call started: a slow run
    # stamped at its start came back already expired, so a polled pane ran the
    # doctor back to back (task #1850). A report the tool served from its own
    # cache is older by its cache_age_s.
    _finish_doctor_call(call, (time.monotonic() - _tool_cache_age(result), result), None)


async def _run_queued_doctor_call(call: _DoctorCall) -> None:
    """Wait for promotion (bounded), then run the queued forced call."""
    global _doctor_pending_forced
    try:
        await asyncio.wait_for(asyncio.wrap_future(call.promoted), timeout=_DOCTOR_WAIT_S / 2)
    except TimeoutError:
        # The call ahead is stalled. Start anyway, rather than leave Refresh
        # hanging behind it; its late result cannot replace this newer one.
        with _doctor_state_lock:
            if call.generation is None:
                if _doctor_pending_forced is call:
                    _doctor_pending_forced = None
                _start_doctor_call_locked(call)
    except BaseException as exc:
        # The loop that queued this call is going away (shutdown cancels the
        # wait) before the call could run. Leave the queue and resolve the call,
        # or the call ahead would later promote it into the in-flight slot where
        # nothing runs it, and its waiters would wait out the full bound (#1926).
        # If it was promoted already, finishing it frees the slot as well.
        with _doctor_state_lock:
            if _doctor_pending_forced is call:
                _doctor_pending_forced = None
        _finish_doctor_call(call, None, exc)
        raise
    await _run_doctor_call(call)


def _spawn_doctor_task(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _doctor_tasks.add(task)
    task.add_done_callback(_doctor_tasks.discard)


async def health_payload(force: bool = False) -> dict:
    global _doctor_pending_forced
    with _doctor_state_lock:
        entry = _doctor_cache
        now = time.monotonic()
        if not force and entry is not None and now - entry[0] < _DOCTOR_CACHE_S:
            return _doctor_reading(entry, now)
        spawn = None
        if not force and _doctor_inflight is not None:
            call = _doctor_inflight
        elif force and _doctor_pending_forced is not None:
            # Queued and not started: it starts after this request arrived.
            call = _doctor_pending_forced
        elif _doctor_inflight is None:
            call = _DoctorCall(forced=force)
            _start_doctor_call_locked(call)
            spawn = _run_doctor_call(call)
        else:
            # A forced request never shares a call that began before it: its
            # probes (the worktree survey above all) may predate the change the
            # operator just made. Queue one forced call behind it.
            call = _doctor_pending_forced = _DoctorCall(forced=True)
            spawn = _run_queued_doctor_call(call)
    if spawn is not None:
        _spawn_doctor_task(spawn)
    waited = asyncio.wrap_future(call.future)
    # A waiter that gave up (timeout, closed request) leaves this future behind;
    # retrieve its outcome so a later failure is not logged as never retrieved.
    waited.add_done_callback(lambda f: f.cancelled() or f.exception())
    try:
        # Shielded: a closed request must not cancel the shared call.
        outcome = await asyncio.wait_for(asyncio.shield(waited), timeout=_DOCTOR_WAIT_S)
    except TimeoutError:
        last = _doctor_cache
        message = f"system_doctor did not answer within {_DOCTOR_WAIT_S:.0f}s"
        if last is None:
            raise TimeoutError(message) from None
        stale = _doctor_reading(last, time.monotonic())
        stale["error"] = f"{message}; showing the last reading"
        return stale
    return _doctor_reading(outcome, time.monotonic())


async def perf_payload(since_hours: float = 24.0) -> dict:
    return await _safe("perf_summary", default={"by_tool": {}}, since_hours=since_hours)


async def delivery_slo_payload(window_days: float = 7.0) -> dict:
    """Epic C (#1188) exit-test metrics for the Control Center telemetry pane
    (task #1259): dispatch success rate, p90 cycle time, unattributed
    completions, CI green rate.

    Not routed through `server.REGISTERED_TOOLS`/`call_tool` — `monitor.delivery_slo`
    is a plain read over the existing `dispatch_runs`/`tasks` stores, not a
    registered MCP tool (Epic B is shrinking the default tool count), so this
    follows the same direct-call-off-thread pattern as `devices_payload` /
    `telemetry_payload` rather than adding one. Never mutates anything;
    degrades a metric to `status: "unavailable"` with a reason instead of
    ever raising or fabricating a number — so this call itself should not
    fail, but a bug's failure mode still must not 500 the pane.
    """
    try:
        return await anyio.to_thread.run_sync(lambda: monitor.delivery_slo(window_days))
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {
            "error": f"delivery_slo failed: {str(exc)[:200]}",
            "code": "delivery_slo_unavailable",
            "window_days": window_days,
        }


async def costs_payload(since: str | None = None, until: str | None = None) -> dict:
    """Per-agent cost rollup and budget status for the Control Center (task #1348).

    Read-only over the self-pickup run record and the broker spend ledger; see
    `budgets.costs_payload`. Invalid bounds and failures come back as a named
    error state rather than a 500.
    """
    from .. import budgets

    try:
        return await anyio.to_thread.run_sync(lambda: budgets.costs_payload(since, until))
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {"error": f"costs unavailable: {str(exc)[:200]}", "code": "costs_unavailable"}


async def tool_allowance_payload(agent: str | None = None) -> dict:
    """Per-tool call allowance for the Control Center (task #2144).

    Read-only over the gateway's live rate-limit counter and the decision
    ledger; see `tool_allowance.tool_allowance`. The request's verified
    identity is bound by the hub middleware, so the per-caller fence inside
    the reader applies unchanged. Fence refusals surface as a named error
    state rather than a 500.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .. import tool_allowance as tool_allowance_mod

    try:
        return await anyio.to_thread.run_sync(lambda: tool_allowance_mod.tool_allowance(agent))
    except ToolError as exc:
        return {"error": str(exc)[:300], "code": "forbidden", "tools": []}
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {
            "error": f"tool allowance unavailable: {str(exc)[:200]}",
            "code": "tool_allowance_unavailable",
            "tools": [],
        }


async def config_revisions_payload(
    kind: str = "policy",
    from_revision: int | None = None,
    to_revision: int | None = None,
) -> dict:
    """Revision list for the Control Center. Read-only. ``from`` adds a diff."""
    from .. import config_revisions

    def _read() -> dict:
        listed = config_revisions.list_revisions(kind)
        if from_revision is not None:
            listed["diff"] = config_revisions.diff_revisions(kind, from_revision, to_revision)
        return listed

    try:
        return await anyio.to_thread.run_sync(_read)
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {
            "error": str(exc)[:300],
            "code": "config_revisions_unavailable",
            "kind": kind,
            "revisions": [],
            "count": 0,
            "drift": False,
            "latest_diff": "",
        }


async def config_revision_rollback_payload(kind: str, revision: int, actor: str) -> dict:
    """Approval-gated restore. A pending gate is a 202 payload, not a failure."""
    from .. import config as ua_config
    from .. import config_revisions
    from ..tools.approvals import ApprovalPendingError

    def _run() -> dict:
        from .. import agent_access, gateway, safety

        token = ua_config.agent_identity_var.set(actor)
        try:
            # Same outer gates as the MCP wrapper. The inner rollback still
            # uses config_revision_rollback for the one-shot pass.
            agent_access.enforce("config_revision_manage", read_only=False)
            with gateway.invocation("config_revision_manage"):
                safety.check_policy_gate(
                    "config_revision_manage",
                    {"kind": kind, "revision": revision},
                    originator=actor,
                )
                return config_revisions.rollback(kind, revision, actor=actor)
        finally:
            ua_config.agent_identity_var.reset(token)

    try:
        return await anyio.to_thread.run_sync(_run)
    except ApprovalPendingError as exc:
        return {
            "pending": True,
            "approval_id": exc.approval_id,
            "code": exc.code,
            "error": str(exc),
            "status": 202,
        }
    except config_revisions.ConfigFanoutError as exc:
        return exc.result


# ---------------------------------------------------------------------------
# Operating pane (task #1757): profiles, office bindings, sprints, run settings
# ---------------------------------------------------------------------------

# Profiles and office bindings are policy. They change only through the
# reviewed policy revision path, which has no HTTP commit route; the pane
# names that path rather than offering a second writer.
OPERATING_POLICY_EDIT = {
    "tool": "config_revision_manage",
    "action": "commit",
    "kind": "policy",
    "block": "operating_profiles",
    "history_route": "/api/config-revisions?kind=policy",
}
OPERATING_SPRINT_OPS = {
    "add": "operating_sprint_add",
    "update": "operating_sprint_update",
    "link": "operating_sprint_link",
    "unlink": "operating_sprint_unlink",
}
OPERATING_RUN_OPS = {"set": "operating_run_set", "clear": "operating_run_clear"}
_OPERATING_SPRINT_FIELDS = {
    "add": {"name", "goal_id", "org_unit", "profile", "overrides", "starts_at", "ends_at"},
    "update": {
        "sprint_id",
        "name",
        "status",
        "profile",
        "overrides",
        "org_unit",
        "starts_at",
        "ends_at",
    },
    "link": {"sprint_id", "task_id"},
    "unlink": {"sprint_id", "task_id"},
}
_OPERATING_RUN_FIELDS = {
    "set": {"task_id", "profile", "overrides"},
    "clear": {"task_id"},
}


class OperatingRequestError(ValueError):
    """A malformed Operating pane request (400, never a 500)."""


async def operating_payload() -> dict:
    """Profiles, office bindings and the office list for the Operating pane."""
    from .. import operating_profiles, org_registry

    def _read() -> dict:
        view = operating_profiles.profiles_view()
        offices = [
            {
                "id": office["id"],
                "name": office["name"],
                "lead": office["lead"],
                "members": list(office["members"]),
                "profile": view["units"].get(office["id"]),
            }
            for office in org_registry.load_registry()
        ]
        return {**view, "offices": offices, "policy_edit": dict(OPERATING_POLICY_EDIT)}

    try:
        return await anyio.to_thread.run_sync(_read)
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {
            "error": str(exc)[:300],
            "code": "operating_unavailable",
            "configured": False,
            "default": None,
            "units": {},
            "profiles": {},
            "fields": [],
            "enforced_fields": [],
            "offices": [],
            "policy_edit": dict(OPERATING_POLICY_EDIT),
        }


async def operating_resolve_payload(task_id: int | None, sprint_id: int | None) -> dict:
    """Effective settings with the layer that set each field. Raises ToolError."""
    from .. import operating_profiles, storage

    def _read() -> dict:
        # One connection and one read transaction, so the resolved values and
        # the stored run layer describe the same database state. Schema setup
        # (executescript) runs before the snapshot starts.
        conn = storage.connect()
        try:
            operating_profiles.ensure_schema(conn)
            conn.execute("BEGIN")
            try:
                resolved = operating_profiles.resolve(
                    task_id=task_id, sprint_id=sprint_id, conn=conn, fenced=True
                )
                row = (
                    conn.execute(
                        "SELECT profile, overrides, set_by, set_at FROM run_settings "
                        "WHERE task_id=?",
                        (int(task_id),),
                    ).fetchone()
                    if task_id is not None
                    else None
                )
            finally:
                conn.rollback()
        finally:
            conn.close()
        if task_id is None:
            return resolved
        # The stored run layer, separate from the resolved values, so an edit
        # can start from what is set rather than replacing it blind.
        stored = None
        if row is not None:
            try:
                overrides = json.loads(row["overrides"] or "{}")
            except ValueError:
                overrides = {}
            stored = {
                "profile": row["profile"],
                "overrides": overrides if isinstance(overrides, dict) else {},
                "set_by": row["set_by"],
                "set_at": row["set_at"],
            }
        return {**resolved, "run_settings": stored}

    return await anyio.to_thread.run_sync(_read)


async def operating_sprints_payload(status: str | None) -> dict:
    """Sprints with their linked task ids. Raises ToolError on a bad status."""
    from .. import operating_profiles

    def _read() -> dict:
        listed = operating_profiles.sprint_list(status)
        sprints = [operating_profiles.sprint_get(int(row["id"])) for row in listed["sprints"]]
        return {"sprints": sprints, "statuses": list(operating_profiles.SPRINT_STATUSES)}

    return await anyio.to_thread.run_sync(_read)


def _operating_params(op: str, body: dict, allowed: dict[str, set[str]]) -> dict[str, Any]:
    if op not in allowed:
        raise OperatingRequestError(f"op must be one of {sorted(allowed)}")
    if "actor" in body or "author" in body:
        raise OperatingRequestError("actor is taken from the authenticated identity")
    params = {k: v for k, v in body.items() if k != "op"}
    unknown = sorted(set(params) - allowed[op])
    if unknown:
        raise OperatingRequestError(f"unknown field(s) for {op}: {', '.join(unknown)}")
    return params


async def operating_action_payload(kind: str, body: dict, actor: str) -> dict:
    """Sprint or run-settings write through goal_manage's gates.

    The caller's identity is bound for the write, so a human writes directly
    and an agent's write parks as an ``operating_settings`` approval. A
    pending gate is a 202 payload, not a failure.
    """
    from .. import config as ua_config
    from .. import operating_profiles
    from ..tools.approvals import ApprovalPendingError

    ops = OPERATING_SPRINT_OPS if kind == "sprint" else OPERATING_RUN_OPS
    fields = _OPERATING_SPRINT_FIELDS if kind == "sprint" else _OPERATING_RUN_FIELDS
    op = str(body.get("op") or "")
    params = _operating_params(op, body, fields)
    action = ops[op]

    def _run() -> dict:
        from .. import agent_access, gateway, safety, trust_presets
        from ..tools import approvals

        gate_payload = {"action": action, "params": params}
        token = ua_config.agent_identity_var.set(actor)
        try:
            # The gates the MCP wrapper runs for goal_manage, for every caller,
            # human included. Only profile tool visibility is decided by the
            # route (see routes._operating_authority).
            agent_access.enforce("goal_manage", read_only=False)
            trust_presets.check_call("goal_manage", read_only=False, payload=gate_payload)
            operating_profiles.preflight(action, params)
            with gateway.invocation("goal_manage"), approvals.approval_invocation() as receipts:
                safety.check_policy_gate("goal_manage", gate_payload, originator=actor)
                # One invocation, one pass (as _registered_platform_write): a
                # policy require_approval that already consumed a pass is not
                # asked for a second one by the UA_APPROVAL_TOOLS gate.
                policy_consumed = any(
                    isinstance(receipt, dict) and receipt.get("approval_id") is not None
                    for receipt in receipts.values()
                )
                if not policy_consumed:
                    approvals.check_approval_gate("goal_manage", gate_payload, requested_by=actor)
                # Execution is deliberately not marked here. manage() runs its
                # own authorization (an agent's operating_settings approval)
                # and validation before it commits, and refuses without
                # writing. Any refusal therefore leaves this scope unstarted,
                # which restores every pass consumed above; only a completed
                # write keeps them spent.
                return operating_profiles.manage(action, {**params, "actor": actor})
        finally:
            ua_config.agent_identity_var.reset(token)

    try:
        return await anyio.to_thread.run_sync(_run)
    except ApprovalPendingError as exc:
        return {
            "pending": True,
            "approval_id": exc.approval_id,
            "code": exc.code,
            "error": str(exc),
            "status": 202,
        }


async def teams_payload() -> dict:
    """Committed team specs (task #1736). Read-only."""
    from .. import team_spec

    return await anyio.to_thread.run_sync(team_spec.team_list)


def _team_up_gates(actor: str, params: dict, *, read_only: bool) -> None:
    """The MCP guard chain for coordination_manage(team_up), for HTTP (#1736).

    Agent access, the profile ceiling on the consolidated action, trust-preset
    admission, then the policy gate on both the manager and the action, the
    same order ``server.py`` and ``coordination_manage`` apply. Raises
    ``PermissionError`` for a denial; HTTP must not be a side door.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .. import agent_access, gateway, profiles, safety, trust_presets
    from ..tools.approvals import ApprovalPendingError

    payload = {"action": "team_up", "params": params}
    try:
        agent_access.enforce("coordination_manage", read_only=read_only)
        profile = profiles.default_for_identity(actor)
        if not profiles.consolidated_action_is_allowed_for(
            profile, "coordination_manage", "team_up"
        ):
            raise PermissionError(
                f"team_up is not exposed by the {profile} profile (coordination_manage)"
            )
        trust_presets.check_call("coordination_manage", read_only=read_only, payload=payload)
        with gateway.invocation("coordination_manage"):
            safety.check_policy_gate("coordination_manage", payload, originator=actor)
            safety.check_policy_gate("team_up", params, originator=actor)
    except ApprovalPendingError:
        raise
    except (ToolError, agent_access.AgentAccessDenied) as exc:
        raise PermissionError(str(exc)) from exc


async def team_plan_payload(team: str, actor: str) -> dict:
    """Dry-run boot plan for one committed team (task #1736). Read-only."""
    from .. import config as ua_config
    from .. import team_spec

    def _run() -> dict:
        token = ua_config.agent_identity_var.set(actor)
        try:
            _team_up_gates(actor, {"team": team, "plan": True}, read_only=True)
            return team_spec.plan_spec(team_spec.resolve(team), actor=actor)
        finally:
            ua_config.agent_identity_var.reset(token)

    return await anyio.to_thread.run_sync(_run)


async def team_up_payload(team: str, plan_sha256: str | None, actor: str) -> dict:
    """Approval-gated team boot. A pending gate is a 202 payload, not a failure."""
    from .. import config as ua_config
    from .. import team_spec
    from ..tools.approvals import ApprovalPendingError

    def _run() -> dict:
        token = ua_config.agent_identity_var.set(actor)
        try:
            params = {"team": team, "plan": False, "expected_plan_sha256": plan_sha256}
            _team_up_gates(actor, params, read_only=False)
            return team_spec.boot(
                team_spec.resolve(team), actor=actor, expected_plan_sha256=plan_sha256
            )
        finally:
            ua_config.agent_identity_var.reset(token)

    try:
        return await anyio.to_thread.run_sync(_run)
    except ApprovalPendingError as exc:
        return {
            "pending": True,
            "approval_id": exc.approval_id,
            "code": exc.code,
            "tool": team_spec.TEAM_UP_TOOL,
            "error": str(exc),
        }


async def scorecards_payload(window_days: float = 14.0) -> dict:
    """Per-agent scorecards for the Control Center (task #1354). Read-only."""
    from .. import scorecards

    try:
        return await anyio.to_thread.run_sync(lambda: scorecards.scorecards_payload(window_days))
    except Exception as exc:  # noqa: BLE001 - named error state, never 500 the pane
        return {
            "error": f"scorecards unavailable: {str(exc)[:200]}",
            "code": "scorecards_unavailable",
        }


async def events_payload(
    channels: list[str] | None = None,
    since: str | None = None,
    limit: int = 100,
    after_seq: int | None = None,
    operator_recovery: str | None = None,
    item_id: str | None = None,
    channel_prefix: str | None = None,
) -> dict:
    """One ``event_log_query`` page through the registered tool.

    ``item_id`` narrows the page to one node dispatch item (task #1992).
    ``channel_prefix`` is the single-prefix form of a ``channels`` entry.

    A refusal or failure raises, so the route answers with its status instead
    of an error inside a 200 (task #1811). ``operator_recovery`` is the verified
    operator's identity when the route found the human-recovery exemption; it
    lets this one read pass the agent-access switch and nothing else.
    """
    from .. import agent_access

    # Both cursors are rejected by the route (and by the tool); pass through
    # whichever one the caller sent rather than silently choosing.
    kwargs: dict[str, Any] = {"channels": channels, "since": since, "limit": limit}
    if after_seq is not None:
        kwargs["after_seq"] = after_seq
    if item_id:
        kwargs["item_id"] = item_id
    if channel_prefix:
        kwargs["channel_prefix"] = channel_prefix
    if operator_recovery is None:
        return await call_tool("event_log_query", **kwargs)
    with agent_access.operator_recovery_read("event_log_query", operator_recovery):
        return await call_tool("event_log_query", **kwargs)


def _unconfigured_zulip_health(error: str) -> dict:
    """Empty configured work cannot inherit cached worker health or trigger IO."""
    return {
        "status": "unconfigured",
        "poll_interval_s": zulip_worker.DEFAULT_POLL_INTERVAL_S,
        "retry_base_s": zulip_worker.DEFAULT_RETRY_BASE_S,
        "retry_max_s": zulip_worker.DEFAULT_RETRY_MAX_S,
        "last_successful_poll": None,
        "last_error": error,
        "summary": {key: 0 for key in ("healthy", "error", "stale", "not_started")},
        "channels": [],
        "queues": [],
        "queue_summary": {key: 0 for key in ("healthy", "error", "reset", "not_started")},
        "queue_error": None,
    }


async def zulip_worker_health_payload() -> dict:
    """Read-only Zulip worker health for the existing overview surface."""
    site_status = zulip_tools.zulip_site_status()
    if site_status["configuration_error"]:
        return _unconfigured_zulip_health(site_status["configuration_error"])
    return await anyio.to_thread.run_sync(
        lambda: zulip_worker.ZulipPollingWorker().health_payload()
    )


async def zulip_coordination_payload() -> dict:
    """Zulip coordination surface: worker health, recent inbound, protocol stats."""
    site_status = zulip_tools.zulip_site_status()
    site = site_status["site"] or ""
    if site_status["configuration_error"]:
        return {
            "site": "",
            "worker": _unconfigured_zulip_health(site_status["configuration_error"]),
            "recent_inbound": [],
            "stats": {},
            "error": site_status["configuration_error"],
        }
    now = datetime.now(UTC)
    worker = await zulip_worker_health_payload()
    try:
        recent = await anyio.to_thread.run_sync(_recent_zulip_inbound_rows)
        stats = _zulip_coordination_stats(recent, now=now)
    except Exception as exc:  # noqa: BLE001
        return {
            "site": site,
            "worker": worker,
            "recent_inbound": [],
            "stats": {},
            "error": str(exc)[:200],
        }
    return {
        "site": site,
        "worker": worker,
        "recent_inbound": recent[:ZULIP_INBOUND_UI_LIMIT],
        "stats": stats,
    }


async def telemetry_payload(
    trace_id: str | None = None,
    task_id: int | None = None,
    limit: int = 500,
) -> dict:
    """Read-only telemetry waterfall and exception digest for the Command Center."""
    return await anyio.to_thread.run_sync(
        lambda: telemetry_projection.telemetry_payload(
            trace_id=trace_id,
            task_id=task_id,
            limit=limit,
        )
    )


async def anonymous_request_count(hours: float = 24.0) -> int:
    """How many unauthenticated hub requests were seen recently.

    This is the gauge that turns "auth is on" from a belief into a measurement, and
    it is the gate for flipping UA_ENFORCE_AUTH=1. Before this existed the only
    signal was a stderr line nobody reads.
    """
    from datetime import UTC, datetime

    since_dt = datetime.now(UTC) - timedelta(hours=hours)
    result = await _safe(
        "event_log_query", default={"events": []}, channels=["hub.auth.anonymous"], limit=500
    )
    events = result.get("events", []) if isinstance(result, dict) else []
    count = 0
    for event in events:
        created = event.get("created_at")
        if not created:
            continue
        try:
            when = datetime.fromisoformat(created)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when >= since_dt:
            count += 1
    return count


async def devices_payload() -> dict:
    """Fleet host rows for the Hosts page (/agents/hosts). Read-only; freshness is per-row.

    Runs ``system_doctor`` (cached via ``health_payload``) so the hub's own
    snapshot is persisted, then aggregates stored snapshots with MCP last-seen.
    """
    await health_payload()
    try:
        payload = await anyio.to_thread.run_sync(host_registry.fleet_payload)
    except Exception as exc:  # noqa: BLE001 - reported in-band
        payload = {"hosts": [], "error": f"devices query failed: {str(exc)[:200]}"}
    payload["oauth"] = await oauth_sessions_payload()
    # Tailnet peers with no host row (task #1389): display-only "unreported"
    # rows so an unmanaged device (e.g. the Mac laptop) is a known blank.
    from .. import tailnet

    try:
        census = await anyio.to_thread.run_sync(tailnet.peers)
    except Exception:  # noqa: BLE001 - advisory
        census = None
    if census is None:
        payload["unreported_peers"] = []
        payload["tailnet_census"] = "unavailable"
    else:
        names = {h.get("host") for h in payload.get("hosts", []) if h.get("host")}
        # UA_NODE_NAME may be unrelated to the Tailscale name. A registered
        # node's authenticated device binding supplies the peer identity.
        for host in payload.get("hosts", []):
            node = host.get("node")
            if isinstance(node, dict) and isinstance(node.get("device_node"), str):
                names.add(node["device_node"])
        payload["unreported_peers"] = tailnet.unreported_peers(names, census)
        payload["tailnet_census"] = "ok"
    return payload


async def nodes_payload() -> dict:
    """Registered ua-node devices for the Sessions launcher (task #1329, I3).

    Read-only projection of ``node_queue.list_nodes`` plus recent dispatch
    items. Never invents a node that has not registered.
    """
    from .. import node_acceptance, node_queue, node_registry, node_resume

    try:
        raw = await anyio.to_thread.run_sync(node_queue.list_nodes)
        # Device acceptance per node (task #2112); never raises.
        acceptance = await anyio.to_thread.run_sync(node_acceptance.states)
        recent = await anyio.to_thread.run_sync(lambda: node_queue.list_recent_items(limit=25))
        profile_statuses = await anyio.to_thread.run_sync(node_queue.hermes_profile_statuses)
        resume_modes = await anyio.to_thread.run_sync(node_resume.observed_resume_modes)
        session_settings = await anyio.to_thread.run_sync(node_queue.observed_session_settings)
        retired = await anyio.to_thread.run_sync(node_registry.read_retired_nodes)
    except Exception as exc:  # noqa: BLE001 - reported in-band
        return {
            "nodes": [],
            "recent_items": [],
            "retired_nodes": [],
            "error": f"nodes query failed: {str(exc)[:200]}",
        }
    nodes = []
    for name, info in sorted(raw.items(), key=lambda kv: kv[0]):
        harnesses = info.get("harnesses") if isinstance(info.get("harnesses"), dict) else {}
        # Launch options are the advertised backends only — installed harness
        # metadata stays diagnostic; _accepting_node rejects anything else.
        backends = [b for b in (info.get("backends") or []) if isinstance(b, str)]
        roots = [r for r in (info.get("worktree_roots") or []) if isinstance(r, str)]
        nodes.append(
            {
                "node": name,
                "agent": info.get("agent"),
                "device_node": info.get("device_node"),
                "version": info.get("version"),
                "registered_at": info.get("registered_at"),
                "last_heartbeat": info.get("last_heartbeat"),
                "os": info.get("os"),
                "backends": backends,
                "harnesses": harnesses,
                "worktree_roots": roots,
                # Latest device acceptance run, or None when never run (task #2112).
                "acceptance": acceptance.get(name),
                "hermes_profiles": [
                    {**profile, "last_item": profile_statuses.get((name, profile["name"]))}
                    for profile in node_queue.hermes_profiles(name, info.get("surfaces") or {})
                ],
                "session_settings": {
                    backend + (":" + profile if profile else ""): settings
                    for (node, backend, profile), settings in session_settings.items()
                    if node == name
                },
                "controls": {
                    backend: node_queue.acp_matrix.control_capabilities(
                        backend,
                        advertised=backend in backends,
                        version=(harnesses.get(backend) or {}).get("version")
                        if isinstance(harnesses.get(backend), dict)
                        else None,
                        features=info.get("node_features"),
                        resume_mode=resume_modes.get((name, backend)),
                        settings=session_settings.get((name, backend, None)),
                    )
                    for backend in sorted(set(backends) | {"acp:hermes", "acp:cursor", "devin"})
                },
            }
        )
    return {
        "nodes": nodes,
        "recent_items": recent,
        "acceptance_required": config.node_require_acceptance(),
        # Retired rows (task #2181) are left out of ``nodes``; listed here so the
        # Hosts page can offer Unretire (task #2208). Who, when and why only.
        "retired_nodes": [
            {
                "node": name,
                "agent": info.get("agent"),
                "last_heartbeat": info.get("last_heartbeat"),
                "retired_at": info.get("retired_at"),
                "retired_by": info.get("retired_by"),
                "retired_reason": info.get("retired_reason"),
            }
            for name, info in sorted(retired.items())
        ],
    }


async def oauth_sessions_payload() -> dict:
    """Signed-in OAuth sessions grouped by device (task #1321). Never token values."""
    from ..tools import oauth_devices

    try:
        return await anyio.to_thread.run_sync(oauth_devices.list_device_sessions)
    except Exception as exc:  # noqa: BLE001 - reported in-band
        return {"devices": [], "session_count": 0, "error": str(exc)[:200]}


async def oauth_revoke_action(*, device: str | None, session_id: str | None, actor: str) -> dict:
    """Approval-gated revoke of one device's (or one session's) sign-in (task #1321)."""
    from ..tools import oauth_devices

    def _run() -> dict:
        return oauth_devices.revoke_device_sessions(
            device=device, session_id=session_id, actor=actor
        )

    return await anyio.to_thread.run_sync(_run)


async def overview_payload() -> dict:
    """Everything the dashboard paints in one round trip."""
    agents = await agents_payload()
    sessions = await sessions_payload(limit=50)
    tasks = await tasks_payload()
    queue_health = await queue_health_payload()
    review_approval = await review_approval_payload()
    executor_delegation = await executor_delegation_payload()
    approvals = await approvals_payload()
    anomalies = await anomalies_payload()
    health = await health_payload()
    devices = await devices_payload()
    zulip_health = await zulip_worker_health_payload()
    zulip_coordination = await zulip_coordination_payload()
    metrics = await _safe("metrics_snapshot", default={})
    # The Action Deck's workflow picker used to carry a hard-coded list of names that
    # matched no definition in the repo, so every run 400'd. It reads this instead.
    workflows = await _safe("workflow_list", default={"workflows": []})
    anonymous = await anonymous_request_count()
    graph = await agent_graph_payload(
        agents_res=agents, tasks_res=tasks, delegations_res=executor_delegation
    )
    execution_graph = await execution_graph_payload(
        tasks_res=tasks, delegations_res=executor_delegation, sessions_res=sessions
    )
    # tasks=None on purpose: the graph needs the full active queue (open
    # parents included), not tasks_payload()'s doing/blocked/waiting bucket.
    task_graph = await task_graph_payload(
        delegations_res=executor_delegation,
        approvals_res=approvals,
    )

    return {
        "agents": agents,
        "sessions": sessions,
        "tasks": tasks,
        "queue_health": queue_health,
        "projects": project_registry.registry(),
        "review_approval": review_approval,
        "executor_delegation": executor_delegation,
        "approvals": approvals,
        "anomalies": anomalies,
        "zulip_worker": zulip_health,
        "zulip_coordination": zulip_coordination,
        "agent_graph": graph,
        "execution_graph": execution_graph,
        "task_graph": task_graph,
        "workflows": workflows,
        "health": {
            "summary": health.get("summary", {}),
            "checks": health.get("checks", []),
            "cached_age_s": health.get("cached_age_s"),
        },
        "devices": devices,
        "metrics": metrics,
        "auth": {
            "enforce": config.enforce_auth(),
            "tokens_configured": bool(config.agent_tokens()),
            "anonymous_requests_24h": anonymous,
        },
        "edition": config.edition(),
        "is_enterprise": config.is_enterprise_edition(),
    }


def _workspace_binding(label: str, mapping: dict | None = None) -> dict[str, str]:
    """Metadata-only scoped context for an agent node. Never includes memory."""
    if mapping is None:
        mapping = config.agent_workspaces()
    if label not in mapping:
        return {"workspace_binding": "unbound"}
    value = mapping[label]
    if value is config.WORKSPACE_DENIED:
        return {"workspace_binding": "denied"}
    return {"workspace_binding": "bound", "workspace_root": str(value)}


async def agent_graph_payload(
    agents_res: dict | None = None,
    tasks_res: dict | None = None,
    delegations_res: dict | None = None,
    messages_res: dict | None = None,
    workspaces: dict | None = None,
) -> dict:
    """Build nodes and links for the live D3 agent topology graph."""
    if agents_res is None:
        agents_res = await agents_payload()
    if tasks_res is None:
        tasks_res = await tasks_payload()
    if delegations_res is None:
        delegations_res = await executor_delegation_payload()
    if messages_res is None:
        messages_res = await _safe("message_list", default={"messages": []}, limit=50)

    nodes: list[dict] = []
    links: list[dict] = []
    seen_nodes: set[str] = set()

    # --- Pass 1: collect all nodes ---
    for agent in agents_res.get("agents", []):
        label = agent.get("label")
        node_id = f"agent:{label}"
        seen_nodes.add(node_id)
        node = {
            "id": node_id,
            "label": label,
            "type": "agent",
            "status": agent.get("status", "idle"),
            "sessions": agent.get("open_sessions", 0),
            "tasks": agent.get("active_tasks", 0),
        }
        node.update(_workspace_binding(str(label or ""), workspaces))
        nodes.append(node)

    tasks_list = tasks_res.get("active", [])
    if isinstance(tasks_res, dict) and "tasks" in tasks_res:
        tasks_list = tasks_res.get("tasks", [])

    for task in tasks_list:
        tid = task.get("id")
        node_id = f"task:{tid}"
        seen_nodes.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "label": f"#{tid} {task.get('title')}",
                "type": "task",
                "status": task.get("status", "open"),
                "priority": task.get("priority", "med"),
                "claimed_by": task.get("claimed_by"),
            }
        )

    # --- Pass 2: generate links (all nodes already in seen_nodes) ---
    for task in tasks_list:
        tid = task.get("id")
        node_id = f"task:{tid}"
        owner = task.get("claimed_by")
        if owner and f"agent:{owner}" in seen_nodes:
            links.append({"source": f"agent:{owner}", "target": node_id, "kind": "claim"})

        parent_id = task.get("parent_id")
        if parent_id and f"task:{parent_id}" in seen_nodes:
            links.append({"source": f"task:{parent_id}", "target": node_id, "kind": "subtask"})

    for d in delegations_res.get("delegations", []):
        from_ag = d.get("from_agent")
        to_ag = d.get("to_agent")
        if from_ag and to_ag:
            from_id = f"agent:{from_ag}"
            to_id = f"agent:{to_ag}"
            if from_id in seen_nodes and to_id in seen_nodes:
                links.append(
                    {
                        "source": from_id,
                        "target": to_id,
                        "kind": "delegates",
                        "status": d.get("status"),
                    }
                )

    for message in messages_res.get("messages", []) if isinstance(messages_res, dict) else []:
        from_ag = message.get("from_agent")
        to_ag = message.get("to_agent")
        if not from_ag or not to_ag:
            continue
        from_id = f"agent:{from_ag}"
        to_id = f"agent:{to_ag}"
        if from_id not in seen_nodes or to_id not in seen_nodes:
            continue
        link: dict[str, Any] = {
            "source": from_id,
            "target": to_id,
            "kind": "message",
            "act": message.get("act"),
            "conversation_id": message.get("conversation_id"),
            "message_id": message.get("message_id"),
        }
        if message.get("task_id") is not None:
            link["task_id"] = message.get("task_id")
        links.append(link)

    return {"nodes": nodes, "links": links}


async def execution_graph_payload(
    tasks_res: dict | None = None,
    delegations_res: dict | None = None,
    sessions_res: dict | None = None,
) -> dict:
    """Project task -> delegation -> run relationships without storing new graph state.

    Existing rows are the source of truth. Artifacts, validation, review, and PR values
    are explicit nulls until those systems emit durable identifiers.
    """
    if tasks_res is None:
        tasks_res = await _safe("task_list", default={"tasks": []}, status="all", fields="compact")
    if delegations_res is None:
        delegations_res = await executor_delegation_payload()
    if sessions_res is None:
        sessions_res = await sessions_payload(limit=100)
    dependencies_res = await _safe("dependency_list", default={"dependencies": []})
    handoffs_res = await _safe("handoff_list", default={"handoffs": []})

    task_rows = tasks_res.get("tasks", []) if isinstance(tasks_res, dict) else []
    delegation_rows = (
        delegations_res.get("delegations", []) if isinstance(delegations_res, dict) else []
    )
    session_rows = sessions_res.get("sessions", []) if isinstance(sessions_res, dict) else []
    dependency_rows = (
        dependencies_res.get("dependencies", []) if isinstance(dependencies_res, dict) else []
    )
    handoff_rows = handoffs_res.get("handoffs", []) if isinstance(handoffs_res, dict) else []
    nodes: list[dict] = []
    links: list[dict] = []
    task_ids = {task.get("id") for task in task_rows}
    delegation_ids = {item.get("id") for item in delegation_rows}

    for task in task_rows:
        task_id = task.get("id")
        nodes.append(
            {
                "id": f"task:{task_id}",
                "type": "task",
                "task_id": task_id,
                "label": task.get("title"),
                "state": task.get("status"),
                "priority": task.get("priority"),
                "owner": task.get("claimed_by"),
            }
        )

    for delegation in delegation_rows:
        delegation_id = delegation.get("id")
        task_id = delegation.get("task_id")
        nodes.append(
            {
                "id": f"delegation:{delegation_id}",
                "type": "delegation",
                "delegation_id": delegation_id,
                "task_id": task_id,
                "label": f"delegation #{delegation_id}",
                "state": delegation.get("status"),
                "from_agent": delegation.get("from_agent"),
                "to_agent": delegation.get("to_agent"),
            }
        )
        if task_id in task_ids:
            links.append(
                {
                    "source": f"task:{task_id}",
                    "target": f"delegation:{delegation_id}",
                    "kind": "dispatch",
                }
            )

    for dependency in dependency_rows:
        task_id = dependency.get("task_id")
        prerequisite_id = dependency.get("depends_on_task_id")
        if task_id in task_ids and prerequisite_id in task_ids:
            links.append(
                {
                    "source": f"task:{task_id}",
                    "target": f"task:{prerequisite_id}",
                    "kind": "depends_on",
                    "dependency_id": dependency.get("id"),
                }
            )

    runs: list[dict] = []
    for session in session_rows:
        session_id = session.get("id")
        delegation_id = session.get("delegation_id")
        task_id = session.get("task_id")
        nodes.append(
            {
                "id": f"session:{session_id}",
                "type": "run",
                "session_id": session_id,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "label": session.get("kind") or "agent run",
                "state": session.get("status"),
                "agent": session.get("agent"),
            }
        )
        if delegation_id in delegation_ids:
            links.append(
                {
                    "source": f"delegation:{delegation_id}",
                    "target": f"session:{session_id}",
                    "kind": "executes",
                }
            )
        elif task_id in task_ids:
            links.append(
                {"source": f"task:{task_id}", "target": f"session:{session_id}", "kind": "executes"}
            )
        runs.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "agent": session.get("agent"),
                "kind": session.get("kind"),
                "state": session.get("status"),
                "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"),
                "duration_s": session.get("duration_s"),
                "worktree": session.get("worktree_path"),
                "branch": session.get("branch"),
                "validation": None,
                "review": None,
                "pr": None,
                "summary": session.get("summary"),
                "exit_status": session.get("exit_status"),
            }
        )

    return {
        "nodes": nodes,
        "links": links,
        "runs": runs,
        "counts": {
            "tasks": len(task_rows),
            "delegations": len(delegation_rows),
            "dependencies": len(dependency_rows),
            "handoffs": len(handoff_rows),
            "runs": len(runs),
        },
        "dependencies": dependency_rows,
        "handoffs": handoff_rows,
    }


async def task_graph_payload(
    tasks_res: dict | None = None,
    delegations_res: dict | None = None,
    dependencies_res: dict | None = None,
    plans_res: dict | None = None,
    approvals_res: dict | None = None,
) -> dict:
    """One navigable graph of tasks, agents, and plans (task #806 / G1, #808 / G3).

    Read-only projection over existing rows — no new tables, no new route. Rides
    /api/overview so the Graph pane paints it in the same round trip as the
    agent topology. Node kinds carry the identity the task drawer needs:
    ``task:{id}``, ``agent:{label}``, ``plan:{id}`` (plan steps ride the plan
    node as a ``steps`` list rather than separate nodes — a 12-step plan would
    otherwise flood the canvas with near-invisible step boxes).

    G3 (#808) overlays audit-level tracking: each task/plan node carries
    attribution (claimed_by, device, agent) and proof state (approval_status,
    evidence_ref presence, tamper-evident badge) with unattributed rows
    flagged distinctly.

    Links: parent → child (parent_id), prerequisite → dependent
    (dependency_list), claim (agent → task), delegates (agent → agent),
    authored (agent → plan). Agent nodes are derived from the edges themselves
    (claims, delegation endpoints, plan authors) rather than the live agent
    registry: this pane shows who work is bound to, not heartbeat state, which
    the existing Agent Topology canvas already renders. Edges referencing
    nodes outside the active-queue window (done/cancelled tasks) are dropped
    by the seen-node guard, same as agent_graph_payload.
    """
    if tasks_res is None:
        # The full active queue, not tasks_payload()'s `active` bucket: that
        # excludes `open` rows, which would drop every Backlog parent from the
        # parent/subtask edges — the exact relationships this pane exists to
        # show (task #806).
        all_tasks, tasks_error = await _active_queue_tasks()
        tasks_res = {"tasks": all_tasks}
        if tasks_error:
            tasks_res["error"] = tasks_error
    if delegations_res is None:
        delegations_res = await executor_delegation_payload()
    if dependencies_res is None:
        dependencies_res = await _safe("dependency_list", default={"dependencies": []})
    if plans_res is None:
        plans_res = await _safe("plan_list", default={"plans": []}, status="all", limit=200)

    errors: list[str] = []
    for res in (tasks_res, delegations_res, dependencies_res, plans_res):
        if isinstance(res, dict) and res.get("error"):
            errors.append(str(res["error"]))

    task_rows = tasks_res.get("tasks", []) if isinstance(tasks_res, dict) else []
    delegation_rows = (
        delegations_res.get("delegations", []) if isinstance(delegations_res, dict) else []
    )
    dependency_rows = (
        dependencies_res.get("dependencies", []) if isinstance(dependencies_res, dict) else []
    )
    plan_rows = plans_res.get("plans", []) if isinstance(plans_res, dict) else []

    # G3 (#808): Query evidence report for audit attribution & proof state
    evidence_by_task: dict[int, dict] = {}
    try:
        from ..audit.evidence_report import build_evidence_report

        report = await anyio.to_thread.run_sync(lambda: build_evidence_report(limit=200))
        for rec in report.get("records") or []:
            t_key = rec.get("task_id")
            if t_key is not None:
                evidence_by_task[t_key] = rec
    except Exception:
        pass

    nodes: list[dict] = []
    seen_nodes: set[str] = set()

    for task in task_rows:
        tid = task.get("id")
        node_id = f"task:{tid}"
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)

        # Audit overlay (G3 / task #808)
        rec = evidence_by_task.get(tid) if tid is not None else None
        if rec:
            human = rec.get("human") or config.operator_name()
            device = rec.get("device") or task.get("source_host") or "unattributed"
            agent = rec.get("agent") or task.get("claimed_by") or "unattributed"
            claimed_by = task.get("claimed_by") or (agent if agent != "unattributed" else None)
            proof = rec.get("proof") or {}
            evidence_ref = proof.get("evidence_ref") or task.get("evidence_ref")
            tamper_evident = bool(proof.get("tamper_evident"))
            approvals_list = rec.get("action", {}).get("approvals") or []
        else:
            human = config.operator_name()
            device = task.get("source_host") or "unattributed"
            agent = task.get("claimed_by") or _task_owner(task)[0] or "unattributed"
            claimed_by = task.get("claimed_by")
            evidence_ref = task.get("evidence_ref")
            tamper_evident = False
            approvals_list = []
            if approvals_res and isinstance(approvals_res, dict):
                for a in approvals_res.get("pending") or []:
                    if str(a.get("task_id") or a.get("id")) == str(tid):
                        approvals_list.append(a)

        if any(a.get("status") == "pending" for a in approvals_list):
            approval_status = "pending"
        elif any(a.get("status") in ("approved", "consumed") for a in approvals_list):
            approval_status = "approved"
        elif any(a.get("status") == "denied" for a in approvals_list):
            approval_status = "denied"
        else:
            approval_status = "none"

        unattributed = (
            not human
            or human in ("unattributed", "—")
            or not device
            or device in ("unattributed", "—")
            or not agent
            or agent in ("unattributed", "—")
        )

        proof_label = evidence_ref or ("tamper-evident" if tamper_evident else "unverified")
        chain = f"{human} → {device} → {agent} → #{tid} {task.get('title')} → {proof_label}"

        audit = {
            "human": human,
            "device": device,
            "agent": agent,
            "claimed_by": claimed_by,
            "approval_status": approval_status,
            "evidence_ref": evidence_ref,
            "has_evidence": bool(evidence_ref),
            "tamper_evident": tamper_evident,
            "unattributed": bool(unattributed),
            "chain": chain,
        }

        nodes.append(
            {
                "id": node_id,
                "kind": "task",
                "task_id": tid,
                "label": f"#{tid} {task.get('title')}",
                "status": task.get("status", "open"),
                "priority": task.get("priority", "med"),
                "lane": task.get("lane") or "default",
                "project": task.get("project") or "unified-agent",
                "claimed_by": claimed_by,
                "device": device,
                "agent": agent,
                "approval_status": approval_status,
                "evidence_ref": evidence_ref,
                "tamper_evident": tamper_evident,
                "unattributed": bool(unattributed),
                "audit": audit,
                "parent_id": task.get("parent_id"),
            }
        )

    for plan in plan_rows:
        plan_id = plan.get("id")
        node_id = f"plan:{plan_id}"
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        author = plan.get("author")
        needs_human = bool(plan.get("needs_human"))
        p_agent = author or "unattributed"
        p_unattributed = not author or author in ("unattributed", "—")
        p_audit = {
            "human": config.operator_name(),
            "device": "hub",
            "agent": p_agent,
            "claimed_by": author,
            "approval_status": "pending" if needs_human else "none",
            "evidence_ref": None,
            "has_evidence": False,
            "tamper_evident": False,
            "unattributed": p_unattributed,
            "chain": f"Operator → hub → {p_agent} → plan:{plan_id} {plan.get('goal') or plan_id} → unverified",
        }
        nodes.append(
            {
                "id": node_id,
                "kind": "plan",
                "plan_id": plan_id,
                "label": str(plan.get("goal") or plan_id),
                "status": plan.get("status", "active"),
                "needs_human": needs_human,
                "claimed_by": author,
                "device": "hub",
                "agent": p_agent,
                "approval_status": "pending" if needs_human else "none",
                "evidence_ref": None,
                "tamper_evident": False,
                "unattributed": p_unattributed,
                "audit": p_audit,
            }
        )

    # Agent nodes are derived from the edges that reference them (claims,
    # delegation endpoints, plan authors), so the graph shows exactly the
    # agents bound to visible work — heartbeat-absent labels stay out.
    agents: dict[str, dict] = {}
    for task in task_rows:
        owner = task.get("claimed_by")
        if owner:
            agents.setdefault(owner, {"claims": 0, "delegations": 0})["claims"] += 1
    for d in delegation_rows:
        for endpoint in (d.get("from_agent"), d.get("to_agent")):
            if endpoint:
                agents.setdefault(endpoint, {"claims": 0, "delegations": 0})["delegations"] += 1
    for plan in plan_rows:
        author = plan.get("author")
        if author:
            agents.setdefault(author, {"claims": 0, "delegations": 0})
    for label in sorted(agents):
        node_id = f"agent:{label}"
        seen_nodes.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "kind": "agent",
                "label": label,
                "claims": agents[label]["claims"],
                "delegations": agents[label]["delegations"],
            }
        )

    # --- edges (only between nodes that exist above) ---
    links: list[dict] = []
    seen_links: set[tuple] = set()

    def _link(source: str, target: str, kind: str, **extra: Any) -> None:
        if source not in seen_nodes or target not in seen_nodes:
            return
        key = (source, target, kind)
        if key in seen_links:
            return
        seen_links.add(key)
        links.append({"source": source, "target": target, "kind": kind, **extra})

    for task in task_rows:
        tid = task.get("id")
        node_id = f"task:{tid}"
        owner = task.get("claimed_by")
        if owner:
            _link(f"agent:{owner}", node_id, "claim")
        parent_id = task.get("parent_id")
        if parent_id is not None:
            _link(f"task:{parent_id}", node_id, "subtask")

    for dependency in dependency_rows:
        _link(
            f"task:{dependency.get('depends_on_task_id')}",
            f"task:{dependency.get('task_id')}",
            "depends_on",
            dependency_id=dependency.get("id"),
        )

    for d in delegation_rows:
        from_ag = d.get("from_agent")
        to_ag = d.get("to_agent")
        if not from_ag or not to_ag:
            continue
        _link(
            f"agent:{from_ag}",
            f"agent:{to_ag}",
            "delegates",
            status=d.get("status"),
            delegation_id=d.get("id"),
        )

    for plan in plan_rows:
        author = plan.get("author")
        if author:
            _link(f"agent:{author}", f"plan:{plan.get('id')}", "authored")

    payload = {
        "nodes": nodes,
        "links": links,
        "counts": {
            "tasks": len(task_rows),
            "plans": len(plan_rows),
            "agents": len(agents),
            "dependencies": len(dependency_rows),
            "delegations": len(delegation_rows),
            "links": len(links),
        },
        "errors": errors,
    }
    # Scalar `error` alongside `errors`, same contract as board_payload /
    # executor_delegation_payload: the webui renderers branch on `data.error`
    # for their distinct failure state (review round 1, finding 1).
    if errors:
        payload["error"] = "; ".join(errors)
    return payload


async def lineage_payload(raw_task_id: str) -> dict:
    """Per-task lineage subtree behind GET /api/execution-graph?task=N (task #730).

    Shape is PINNED (the frontend drawer builds against it — do not change
    without noting on UA task #730):

        {"task": {"id", "title"|null},
         "lineage": {"delegations": [...], "dispatch_runs": [...],
                     "autonomous_cycles": [...],
                     "refs": {"pr_url", "worktree_path", "branch"}},
         "empty": bool}

    Delegates to the same `graph.lineage` projection the coordination surface
    exposes (coordination_manage action "lineage"), so the UI and MCP agents
    see one lineage definition. Called directly rather than through
    server.REGISTERED_TOOLS: `lineage` is deliberately not a standalone
    registered tool (the coordination manager is its MCP surface), and this is
    a read-only projection like registry.list_agents in agents_payload. A
    blank or non-integer task id is an empty result, not a 400/500: legacy
    rows with no lane, run, or PR render as empty nodes and a missing task id
    must still render (never 500). No task param at all keeps the full-graph
    payload.
    """
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        return {"empty": True}

    from ..tools import graph as graph_tool

    return await anyio.to_thread.run_sync(graph_tool.lineage, task_id)


async def resolve_approval(
    approval_id: int | str, decision: str, resolved_by: str, reason: str = ""
) -> dict:
    writer = object_model.by_kind("approval").writer("resolve")
    return await anyio.to_thread.run_sync(
        writer,
        int(approval_id),
        decision,
        resolved_by,
        reason,
    )


async def update_task(task_id: int, **kwargs) -> dict:
    return await call_tool("task_update", id=task_id, **kwargs)


async def run_workflow(
    name: str,
    inputs: dict | None = None,
    author: str | None = None,
    instance_id: str | None = None,
    role_bindings: dict | None = None,
    closure: dict | None = None,
) -> dict:
    arguments = {"name": name, "inputs": inputs or {}, "author": author}
    for key, value in (
        ("instance_id", instance_id),
        ("role_bindings", role_bindings),
        ("closure", closure),
    ):
        if value is not None:
            arguments[key] = value
    return await call_tool("workflow_run", **arguments)


# Mirrors config.identity_alias_allowlist(): the operator's web-UI login
# (config.operator_label()) is deliberately unregistered as an agent label, so
# a Dispatch submission from that identity is attributed to the operator's
# bare name (config.operator_name()) for this one call.
# config.verify_write_identity() accepts exactly this (verified, claimed)
# pair; every other identity passes through unaliased and is still subject to
# the normal mismatch check there.
def dispatch_from_agent(identity: str) -> str:
    return config.operator_dispatch_alias(identity)


def start_ui_dispatch(
    *,
    task_id: int,
    actor: str,
    expected_project: str | None = None,
    expected_lane: str | None = None,
    launch_key: str | None = None,
) -> dict:
    """Approve-path claim that starts the target agent's headless CLI.

    Control Center dispatch is an operator click. That click is the approval.
    Scheduler ticks still use the stub spawner and do not start a process.
    A devices-only hub (task #2109) routes the click to the agent's ua-node.
    ``launch_key`` (task #2501) binds a keyed launch's operation key to the run,
    so the database refuses a second job for the same launch on any worker.
    """
    from ..dispatch.coordinator import claim_and_dispatch
    from ..dispatch.launch import ui_launch_boundary

    launch = ui_launch_boundary()
    return claim_and_dispatch(
        task_id=task_id,
        actor=actor,
        launch=launch,
        expected_project=expected_project,
        expected_lane=expected_lane,
        **({"launch_key": launch_key} if launch_key is not None else {}),
    )


async def dispatch_stack_payload(request: DispatchStackRequest) -> dict:
    """Order a selected stack and expose exactly one eligible next task."""
    from ..dispatch import coordinator

    def unavailable(source: str) -> dict:
        return {
            "error": "dispatch stack preview read unavailable",
            "code": "dispatch_stack_read_unavailable",
            "source": source,
            "task_ids": list(request.task_ids),
            "ordered_task_ids": [],
            "next_task_id": None,
            "items": [],
        }

    source = "task_list"
    try:
        # Selected tasks and completed prerequisites can both fall beyond
        # the first bounded page. Read all governed pages before readiness.
        tasks_result = await _task_list_all(status="all", fields="full", limit=500)
        if (
            not isinstance(tasks_result, dict)
            or tasks_result.get("error")
            or tasks_result.get("truncated") is not False
            or not isinstance(tasks_result.get("tasks"), list)
        ):
            return unavailable(source)
        source = "dependency_list"
        edges = []
        for task_id in request.task_ids:
            # A global bounded page can hide this task's later prerequisite
            # edges. Each selected task needs an explicitly complete read.
            dependencies_result = await _safe(
                source, default={"dependencies": []}, limit=2000, task_id=task_id
            )
            if (
                not isinstance(dependencies_result, dict)
                or dependencies_result.get("error")
                or dependencies_result.get("truncated") is not False
                or not isinstance(dependencies_result.get("dependencies"), list)
            ):
                return unavailable(source)
            edges.extend(dependencies_result["dependencies"])
    except Exception:  # noqa: BLE001 - a failed projection must never authorize launch
        return unavailable(source)
    tasks = {task.get("id"): task for task in (tasks_result.get("tasks") or [])}
    selected = set(request.task_ids)
    prerequisites = {task_id: [] for task_id in request.task_ids}
    for edge in edges:
        task_id = edge.get("task_id")
        if task_id in selected:
            prerequisites[task_id].append(edge.get("depends_on_task_id"))

    ordered: list[int] = []
    remaining = set(request.task_ids)
    while remaining:
        ready = [
            task_id
            for task_id in request.task_ids
            if task_id in remaining and all(dep not in remaining for dep in prerequisites[task_id])
        ]
        if not ready:
            ordered.extend(task_id for task_id in request.task_ids if task_id in remaining)
            break
        ordered.extend(ready)
        remaining.difference_update(ready)

    items: list[dict[str, Any]] = []
    for task_id in ordered:
        task = tasks.get(task_id)
        reasons: list[str] = []
        if task is None:
            reasons.append("unknown_task")
        else:
            if request.project is not None and task.get("project") != request.project:
                reasons.append("project_mismatch")
            if request.expected_lane is not None and config.canonical_task_lane(
                task.get("lane") or "default"
            ) not in {
                "default",
                config.canonical_task_lane(request.expected_lane),
            }:
                reasons.append("lane_mismatch")
            unsatisfied = [
                dep
                for dep in prerequisites[task_id]
                if dep not in tasks or tasks[dep].get("status") not in {"done", "cancelled"}
            ]
            if unsatisfied:
                reasons.append("dependencies_incomplete")
        if not reasons:
            # coordinator.evaluate_ready is a bounded SQLite read; keep this
            # projection synchronous so the stack remains one deterministic
            # read/ordering pass.
            verdict = coordinator.evaluate_ready(
                task_id=task_id,
                expected_project=request.project,
                expected_lane=request.expected_lane,
            )
            if not verdict.get("ready"):
                reasons.append(str(verdict.get("reason") or "not_ready"))
        items.append(
            {
                "task_id": task_id,
                "title": (task or {}).get("title"),
                "status": (task or {}).get("status"),
                "project": (task or {}).get("project"),
                "lane": (task or {}).get("lane") or "default",
                "depends_on": prerequisites[task_id],
                "eligible": not reasons,
                "blocked_reasons": reasons,
            }
        )
    next_task_id = next((item["task_id"] for item in items if item["eligible"]), None)
    return {
        "task_ids": list(request.task_ids),
        "ordered_task_ids": ordered,
        "next_task_id": next_task_id,
        "items": items,
    }


def require_recorded_task(task_id: int) -> None:
    """A keyed launch's recorded task must still exist in this tenant (task #2501).

    A repeat continues the task its attempt recorded; when that task has gone
    (deleted, or not this tenant's) the repeat is a typed conflict: start a new
    launch with a new key. Never a 400, which would read as a bad request.
    """
    from ..tool_errors import ConflictError

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (task_id, edition.tenant_org_id())
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ConflictError(
            f"this launch's task #{task_id} no longer exists; start a new launch with a new key"
        )


async def dispatch_prompt(
    request: DispatchRequest, from_agent: str, *, attempt: Any | None = None
) -> dict:
    """Create a delegation, record operator approval, and start the agent CLI.

    ``attempt`` (task #2501) is the keyed Control Center launch this runs under
    (``cc_launch.Attempt``). Each step records its result on it and skips what a
    previous request of the same attempt already did, so a repeat after an
    approval parked a later step continues the same delegation: the delegation is
    created once (``delegate`` also gets the attempt's derived idempotency key),
    the approval note keeps its first timestamp (so the parked approval's payload
    matches), and the start is ``claim_and_dispatch``, which never launches a
    claimed task twice.
    """
    delegate_from_agent = dispatch_from_agent(from_agent)
    steps: dict[str, Any] = attempt.steps if attempt is not None else {}
    if steps.get("task_id") is None:
        keyed = {"idempotency_key": attempt.op_key} if attempt is not None else {}
        result = await call_tool(
            "delegate",
            task_spec=dispatch_spec(request),
            from_agent=delegate_from_agent,
            to_agent=request.agent,
            callback="none",
            priority="med",
            parent_task_id=request.task_id,
            project=request.project,
            **keyed,
        )
        task_id = int(result["task_id"])
        delegation_id = result.get("delegation_id")
        if attempt is not None:
            await attempt.asave(task_id=task_id, delegation_id=delegation_id)
    else:
        task_id = int(steps["task_id"])
        delegation_id = steps.get("delegation_id")
        await anyio.to_thread.run_sync(lambda: require_recorded_task(task_id))
    approved_at = steps.get("approved_at") or datetime.now(UTC).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    if not steps.get("noted"):
        if attempt is not None and not steps.get("approved_at"):
            await attempt.asave(approved_at=approved_at)
        await call_tool(
            "task_update",
            id=task_id,
            append_note=f"[plan: approved @ {approved_at}]",
            worktree=False,
        )
        if attempt is not None:
            await attempt.asave(noted=True)

    def _start() -> dict:
        if attempt is None:
            return start_ui_dispatch(task_id=task_id, actor=delegate_from_agent)
        return start_ui_dispatch(
            task_id=task_id, actor=delegate_from_agent, launch_key=attempt.op_key
        )

    started = await anyio.to_thread.run_sync(_start)
    if attempt is not None and started.get("run_id"):
        await attempt.asave(run_id=started.get("run_id"))
    spawned = bool(started.get("spawned"))
    launched = bool(started.get("launched"))
    error = None
    if not spawned:
        error = str(started.get("error") or started.get("reason") or "agent_did_not_start")
    return {
        "status": "running" if launched and spawned else "pending",
        "task_id": task_id,
        "parent_task_id": request.task_id,
        "delegation_id": delegation_id,
        "session_id": started.get("run_id"),
        "run_id": started.get("run_id"),
        "pid": started.get("pid"),
        "spawned": spawned,
        "worktree": None,
        "pr": None,
        "project": request.project,
        "reason": started.get("reason"),
        "error": error,
    }


async def projects_payload() -> dict:
    """Device-local project registry (task #356) — no filesystem browsing, fixed set."""
    return {"projects": project_registry.registry()}


def _public_cycle(cycle: dict) -> dict:
    """Expose cycle lifecycle data without execution prompts or raw logs."""
    out = dict(cycle)
    raw_capabilities = out.get("allowed_capabilities")
    if isinstance(raw_capabilities, str):
        try:
            import json

            out["allowed_capabilities"] = json.loads(raw_capabilities)
        except (TypeError, ValueError):
            out["allowed_capabilities"] = []
    return out


async def cycles_payload(state: str | None = None, limit: int = 100) -> dict:
    result = await _safe("autonomous_cycle_list", default={"cycles": []}, state=state, limit=limit)
    cycles = result.get("cycles", []) if isinstance(result, dict) else []
    out = {"cycles": [_public_cycle(cycle) for cycle in cycles]}
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result["error"]
    return out


async def plans_payload(
    status: str = "all",
    scope: str | None = None,
    limit: int = 50,
) -> dict:
    """List plans for the Plans pane via ``plan_manage('list')``.

    Authenticated HTTP developer sessions do not expose legacy ``plan_list``;
    the consolidated manager is the profile-safe read path (task #1117).
    """
    params: dict[str, Any] = {"status": status, "limit": limit}
    if scope is not None:
        params["scope"] = scope
    return await call_tool("plan_manage", action="list", params=params)


async def plan_detail(plan_id: str) -> dict:
    """Full plan + steps + checkpoints via ``plan_manage('get')`` (task #1117)."""
    return await call_tool("plan_manage", action="get", params={"plan_id": plan_id})


async def review_plan(*, intent: str, task_id: int | None = None) -> dict:
    """Resolve the human plan-confirmation gate via ``plan_manage('review')``."""
    params: dict[str, Any] = {"intent": intent}
    if task_id is not None:
        params["task_id"] = task_id
    return await call_tool("plan_manage", action="review", params=params)


NO_PROPOSALS_SOURCE = "no proposals source"
NO_PROPOSALS_SOURCE_CODE = "no_proposals_source"
# Closed action map for POST /api/action/proposal (tasks #1148/#1157): the
# route can only ever file a task or mark ingest — no shell, vault-write,
# merge, or deploy authority is reachable through it.
PROPOSAL_ACTIONS = frozenset({"file_task", "dismiss"})


def _no_proposals_source(**extra: Any) -> dict[str, Any]:
    return {
        "proposals": [],
        "error": NO_PROPOSALS_SOURCE,
        "code": NO_PROPOSALS_SOURCE_CODE,
        **extra,
    }


async def proposals_payload() -> dict[str, Any]:
    """Pending session-learning / skill-cluster proposals (task #1148).

    Read-only over the vault Inbox. Named ``no proposals source`` when the
    vault is disabled or the Inbox is missing. Parsing errors stay per-item.
    """
    if config.vault_root() is None:
        return _no_proposals_source()
    return await anyio.to_thread.run_sync(_list_pending_proposals_sync)


def _list_pending_proposals_sync() -> dict[str, Any]:
    from ..tools.learning import list_pending_proposals

    return list_pending_proposals()


async def proposal_action(
    *,
    action: str,
    proposal_id: str,
    agent: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """CSRF-gated file_task | dismiss for the Proposals pane (tasks #1148/#1157).

    Never auto-invoked from sleep-pass / cron. dismiss only marks ingest —
    it does not rewrite proposal content. ``reason`` is an optional reviewer
    note stored with the actor attribution.
    """
    from ..tools.learning import dismiss_proposal, file_proposal_as_task

    if action == "file_task":
        return await anyio.to_thread.run_sync(
            lambda: file_proposal_as_task(proposal_id, agent=agent, reason=reason)
        )
    if action == "dismiss":
        return await anyio.to_thread.run_sync(
            lambda: dismiss_proposal(proposal_id, agent=agent, reason=reason)
        )
    raise ValueError("action must be file_task or dismiss")


async def org_proposals_payload(
    *,
    status: str | None = None,
    scope: str | None = None,
    author: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List org proposals for Control Center (task #1431)."""
    from .. import self_organization

    return await anyio.to_thread.run_sync(
        lambda: self_organization.list_org_proposals(
            status=status, scope=scope, author=author, limit=limit
        )
    )


async def org_proposal_detail(proposal_id: str) -> dict[str, Any]:
    """Detail for one org proposal (task #1431)."""
    from .. import self_organization

    record = await anyio.to_thread.run_sync(self_organization.get_org_proposal, proposal_id)
    if record is None:
        return {"error": f"no org proposal with id {proposal_id}"}
    return {"proposal": record}


async def cycle_detail(cycle_id: str) -> dict:
    result = await call_tool("autonomous_cycle_get", cycle_id=cycle_id)
    out = {
        "cycle": _public_cycle(result["cycle"]),
        "transitions": result.get("transitions", []),
    }
    return out


async def cycle_start(*, agent: str, **kwargs: Any) -> dict:
    result = await call_tool("autonomous_cycle_start", agent=agent, **kwargs)
    return {"cycle": _public_cycle(result["cycle"]), "created": result.get("created", True)}


async def cycle_transition(*, agent: str, **kwargs: Any) -> dict:
    result = await call_tool("autonomous_cycle_transition", agent=agent, **kwargs)
    return {
        "cycle": _public_cycle(result["cycle"]),
        "transition": result.get("transition"),
        "idempotent": result.get("idempotent", False),
    }


async def cycle_record_usage(*, agent: str, **kwargs: Any) -> dict:
    result = await call_tool("autonomous_cycle_record_usage", agent=agent, **kwargs)
    return {
        "cycle": _public_cycle(result["cycle"]),
        "blocked": result.get("blocked", False),
        "reason": result.get("reason"),
    }


async def cycle_kill(*, agent: str, cycle_id: str, reason: str) -> dict:
    result = await call_tool("autonomous_cycle_kill", agent=agent, cycle_id=cycle_id, reason=reason)
    return {
        "cycle": _public_cycle(result["cycle"]),
        "killed_from": result.get("killed_from"),
    }


async def add_dependency(task_id: int, depends_on_task_id: int, agent: str) -> dict:
    return await call_tool(
        "dependency_add",
        task_id=task_id,
        depends_on_task_id=depends_on_task_id,
        agent=agent,
    )


async def remove_dependency(dependency_id: int, agent: str) -> dict:
    return await call_tool("dependency_remove", dependency_id=dependency_id, agent=agent)


async def create_handoff(
    task_spec: str,
    to_agent: str,
    from_agent: str,
    deadline: str | None = None,
    priority: str = "med",
    parent_task_id: int | None = None,
) -> dict:
    return await call_tool(
        "handoff",
        task_spec=task_spec,
        to_agent=to_agent,
        agent=from_agent,
        deadline=deadline,
        priority=priority,
        parent_task_id=parent_task_id,
    )


async def fleet_control_action(
    *,
    action: str,
    host: str,
    unit: str | None = None,
    actor: str,
) -> dict:
    """Run an approval-gated fleet control action for the Hosts page (task #799)."""
    from .. import fleet_actions

    def _run() -> dict:
        return fleet_actions.fleet_host_action(action, host, unit, actor=actor)

    return await anyio.to_thread.run_sync(_run)


async def mobile_projection_payload(
    *,
    freshness: str = "live",
    error_code: str | None = None,
) -> dict[str, Any]:
    """Return the bounded, validated mobile.v1 projection payload."""
    from ..projection import mobile_v1

    attention = await attention_payload()
    health = await health_payload()

    proj = mobile_v1.build_mobile_projection(
        attention_data=attention,
        health_data=health,
        source_host=config.current_source_host(),
        hub_origin="local",
        freshness=freshness,
        error_code=error_code,
    )
    return mobile_v1.to_wire(proj)


async def mobile_action_apply(body: object, identity: str | None) -> dict[str, Any]:
    """Apply one ``mobile-action.v1`` request (task #779 — Watch slice 2).

    The Watch never reaches this: the authenticated iPhone relay does, after a
    fresh hub auth. ``identity`` is the resolved iPhone identity from the route
    guard. Returns a ``mobile-action-result.v1`` mapping; the route maps
    ``rejected`` reasons to their HTTP status.
    """
    import functools

    from .. import policy
    from ..projection import attention_ack, mobile_action_v1
    from ..projection.mobile_v1 import mobile_item_ref

    try:
        request = mobile_action_v1.parse_mobile_action(body)
    except mobile_action_v1.MobileActionError:
        return mobile_action_v1.build_action_result(
            _fallback_action_id(body), "rejected", reason="invalid_envelope"
        )

    # The validator already closes the allowlist, but keep the explicit gate so
    # a widened validator can't silently auto-apply a new verb here.
    if request.action != "acknowledge":
        return mobile_action_v1.build_action_result(
            request.action_id, "rejected", reason="unknown_action"
        )

    # Resolve the ref against FRESH attention — the projection the wrist held is
    # provenance only. Gone/never-present -> noop (it resolved on the wrist).
    attention = await attention_payload()
    items = attention.get("items", []) if isinstance(attention, dict) else []
    live_refs = {
        mobile_item_ref(
            str(item.get("kind", "")),
            item.get("source_subject") or item.get("item_ref"),
        )
        for item in items
    }
    already_acked = set(await anyio.to_thread.run_sync(attention_ack.active_acknowledgements))

    if request.item_ref not in live_refs and request.item_ref not in already_acked:
        return mobile_action_v1.build_action_result(request.action_id, "noop")

    try:
        policy.evaluate_policy(
            "mobile_action.acknowledge",
            payload={"item_ref": request.item_ref, "acked_by": identity or "mobile"},
            originator=identity,
        )
    except policy.PolicyViolation:
        return mobile_action_v1.build_action_result(
            request.action_id, "rejected", reason="policy_denied"
        )

    ulid, acknowledged_until, created = await anyio.to_thread.run_sync(
        functools.partial(
            attention_ack.record_acknowledgement,
            request.item_ref,
            action=request.action,
            acked_by=identity or "mobile",
            action_id=request.action_id,
        )
    )
    return mobile_action_v1.build_action_result(
        request.action_id,
        "applied" if created else "noop",
        audit_ref=ulid if created else None,
        acknowledged_until=acknowledged_until,
    )


def _fallback_action_id(body: object) -> str:
    """Best-effort action_id echo for an invalid envelope; synthesise if absent."""
    from ..projection import mobile_action_v1

    if isinstance(body, dict):
        candidate = body.get("action_id")
        if isinstance(candidate, str) and mobile_action_v1._ACTION_ID.fullmatch(candidate):
            return candidate
    return mobile_action_v1.new_action_id()


async def langgraph_checkpoints_payload(
    run_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only LangGraph checkpoint inspection (Increment A4 / task #753)."""
    default: dict[str, Any] = {
        "checkpoint_db": "",
        "exists": False,
        "count": 0,
        "checkpoints": [],
    }
    kwargs: dict[str, Any] = {"limit": limit}
    if run_id:
        kwargs["run_id"] = run_id
    res = await _safe("langgraph_checkpoints", default=default, **kwargs)
    if isinstance(res, dict) and "count" not in res:
        res["count"] = len(res.get("checkpoints", []))
    return res


async def evidence_payload(
    *,
    task_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    pr: str | None = None,
    limit: int = 50,
    item: str | None = None,
    before_task_id: int | None = None,
) -> dict[str, Any]:
    """User-attributable agent evidence report payload (task #781).

    ``item`` scopes it to one node dispatch item (task #1989). ``before_task_id``
    is the keyset cursor a previous page returned as ``next_cursor`` (task #2516).
    """
    from ..audit.evidence_report import build_evidence_report

    return await anyio.to_thread.run_sync(
        lambda: build_evidence_report(
            task_id=task_id,
            since=since,
            until=until,
            agent=agent,
            project=project,
            pr=pr,
            limit=limit,
            item=item,
            before_task_id=before_task_id,
        )
    )


_CAPABILITY_ITEM_KEYS = (
    "id",
    "family",
    "capability",
    "customer_purpose",
    "origin",
    "implementation_state",
    "consumer",
    "entry_point",
    "connection_state",
    "evidence",
    "boundaries",
    "incomplete",
    "wedge",
    # Computed by the server via capability_verification — never pass through from raw.
    "verification",
)
_CAPABILITY_FAMILY_KEYS = ("id", "label", "pilot", "definition")
_CAPABILITY_BOUNDARY_KEYS = (
    "security",
    "identity",
    "approval",
    "privacy",
    "release",
    "tenant_workspace",
)
_CAPABILITY_INCOMPLETE_KEYS = (
    "owner",
    "task_ref",
    "decision",
    "smallest_next_action",
    "non_action",
)
_CAPABILITY_PILOT_FAMILY_IDS = (
    "governance-evidence",
    "dispatch-delegation",
    "langgraph-planning",
    "provider-quota",
    "first-mate",
)


def _sanitize_capability_origin(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"kind": "", "refs": []}
    refs = raw.get("refs")
    return {
        "kind": raw.get("kind") if isinstance(raw.get("kind"), str) else "",
        "refs": list(refs) if isinstance(refs, list) else [],
    }


def _sanitize_capability_entry_point(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"kind": "", "ref": "", "note": ""}
    out: dict[str, Any] = {
        "kind": raw.get("kind") if isinstance(raw.get("kind"), str) else "",
        "ref": raw.get("ref") if isinstance(raw.get("ref"), str) else "",
    }
    note = raw.get("note")
    if isinstance(note, str) and note:
        out["note"] = note
    return out


def _sanitize_capability_evidence(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {
            "type": entry.get("type") if isinstance(entry.get("type"), str) else "",
            "ref": entry.get("ref") if isinstance(entry.get("ref"), str) else "",
        }
        date = entry.get("date")
        if isinstance(date, str) and date:
            row["date"] = date
        rows.append(row)
    return rows


def _sanitize_capability_boundaries(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {key: "" for key in _CAPABILITY_BOUNDARY_KEYS}
    return {
        key: raw[key] if isinstance(raw.get(key), str) else "" for key in _CAPABILITY_BOUNDARY_KEYS
    }


def _sanitize_capability_incomplete(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in _CAPABILITY_INCOMPLETE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out or None


def _sanitize_capability_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item: dict[str, Any] = {}
    for key in _CAPABILITY_ITEM_KEYS:
        if key not in raw:
            continue
        if key == "verification":
            # Server-computed only — never copy from raw catalog JSON.
            continue
        if key == "origin":
            item[key] = _sanitize_capability_origin(raw.get(key))
        elif key == "entry_point":
            item[key] = _sanitize_capability_entry_point(raw.get(key))
        elif key == "evidence":
            item[key] = _sanitize_capability_evidence(raw.get(key))
        elif key == "boundaries":
            item[key] = _sanitize_capability_boundaries(raw.get(key))
        elif key == "incomplete":
            incomplete = _sanitize_capability_incomplete(raw.get(key))
            if incomplete is not None:
                item[key] = incomplete
        elif key in ("implementation_state", "connection_state"):
            # Pass through verbatim — never fabricate, upgrade, or infer.
            value = raw.get(key)
            if isinstance(value, str):
                item[key] = value
        else:
            value = raw.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                item[key] = value
            elif isinstance(value, list):
                item[key] = list(value)
    return item


def _sanitize_capability_family(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        key: raw[key]
        for key in _CAPABILITY_FAMILY_KEYS
        if key in raw and (isinstance(raw[key], (str, bool, int, float)) or raw[key] is None)
    }


def _build_capability_catalog_payload(
    catalog_path: Path, *, now: date | None = None
) -> dict[str, Any]:
    """Synchronous builder for the sanitized capability-catalog projection."""
    manifest = object_model.manifest()
    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except FileNotFoundError:
        return {
            "available": False,
            "error": f"capability catalog not found: {catalog_path}",
            "catalog": None,
            "manifest": manifest,
            "status": 404,
        }
    except OSError as exc:
        return {
            "available": False,
            "error": f"capability catalog unreadable: {exc}",
            "catalog": None,
            "manifest": manifest,
            "status": 404,
        }
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "error": f"capability catalog is not valid JSON: {exc}",
            "catalog": None,
            "manifest": manifest,
            "status": 404,
        }

    if not isinstance(data, dict):
        return {
            "available": False,
            "error": "capability catalog root must be a JSON object",
            "catalog": None,
            "manifest": manifest,
            "status": 404,
        }

    families_raw = data.get("families")
    items_raw = data.get("items")
    families = [
        fam
        for fam in (
            _sanitize_capability_family(row)
            for row in (families_raw if isinstance(families_raw, list) else [])
        )
        if fam is not None
    ]
    items = [
        item
        for item in (
            _sanitize_capability_item(row)
            for row in (items_raw if isinstance(items_raw, list) else [])
        )
        if item is not None
    ]

    freshness = data.get("freshness") if isinstance(data.get("freshness"), dict) else {}
    counts_by_state: dict[str, int] = {}
    counts_by_implementation: dict[str, int] = {}
    for item in items:
        conn = item.get("connection_state")
        if isinstance(conn, str) and conn:
            counts_by_state[conn] = counts_by_state.get(conn, 0) + 1
        impl = item.get("implementation_state")
        if isinstance(impl, str) and impl:
            counts_by_implementation[impl] = counts_by_implementation.get(impl, 0) + 1

    pilot_families = [
        fam_id
        for fam_id in _CAPABILITY_PILOT_FAMILY_IDS
        if any(f.get("id") == fam_id and f.get("pilot") is True for f in families)
    ]

    queue_coverage_list = (
        data.get("queue_coverage") if isinstance(data.get("queue_coverage"), list) else []
    )
    as_of = freshness.get("as_of") if isinstance(freshness.get("as_of"), str) else ""
    if not as_of:
        as_of = datetime.now(UTC).date().isoformat()
    freshness_meta = {"as_of": as_of}
    evaluation_date = now or datetime.now(UTC).date()

    counts_by_classification: dict[str, int] = {}
    counts_by_evidence_maturity: dict[str, int] = {}
    missing_consumer = 0
    missing_evidence = 0
    review_only_recommendations = 0
    for item in items:
        verification = build_verification(
            item, freshness_meta, queue_coverage_list, now=evaluation_date
        )
        item["verification"] = verification
        classification = verification.get("classification")
        if isinstance(classification, str) and classification:
            counts_by_classification[classification] = (
                counts_by_classification.get(classification, 0) + 1
            )
        maturity = verification.get("evidence_maturity")
        if isinstance(maturity, str) and maturity:
            counts_by_evidence_maturity[maturity] = counts_by_evidence_maturity.get(maturity, 0) + 1
        missing = verification.get("missing")
        if isinstance(missing, list):
            if "consumer" in missing:
                missing_consumer += 1
            if "evidence" in missing:
                missing_evidence += 1
        if verification.get("recommendation") is not None:
            review_only_recommendations += 1

    catalog = {
        "schema_version": data.get("schema_version")
        if isinstance(data.get("schema_version"), str)
        else "",
        "families": families,
        "items": items,
        "queue_coverage": queue_coverage_list,
        "traceable_paths": data.get("traceable_paths")
        if isinstance(data.get("traceable_paths"), list)
        else [],
    }

    return {
        "available": True,
        "catalog": catalog,
        "manifest": manifest,
        "meta": {
            "as_of": freshness.get("as_of") if isinstance(freshness.get("as_of"), str) else "",
            "source_baseline_sha": freshness.get("source_baseline_sha")
            if isinstance(freshness.get("source_baseline_sha"), str)
            else "",
            "origin_main_sha": freshness.get("origin_main_sha")
            if isinstance(freshness.get("origin_main_sha"), str)
            else "",
            "live_hub_checked_at": freshness.get("live_hub_checked_at")
            if isinstance(freshness.get("live_hub_checked_at"), str)
            else "",
            "total_items": len(items),
            "pilot_families": pilot_families,
            "counts_by_state": counts_by_state,
            "counts_by_implementation": counts_by_implementation,
            "source": str(catalog_path),
            "verification_meta": {
                "counts_by_classification": counts_by_classification,
                "counts_by_evidence_maturity": counts_by_evidence_maturity,
                "missing_consumer": missing_consumer,
                "missing_evidence": missing_evidence,
                "review_only_recommendations": review_only_recommendations,
                "as_of": as_of,
                "evaluated_on": evaluation_date.isoformat(),
            },
        },
        "status": 200,
    }


async def capability_catalog_payload(
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Read-only sanitized projection of docs/capability-catalog/catalog.json (task #1066).

    Always includes ``manifest`` from ``object_model.manifest()`` so agents
    polling this endpoint get machine-readable platform object discovery
    alongside the capability inventory (task #1127).
    """
    path = catalog_path or (REPO_ROOT / "docs" / "capability-catalog" / "catalog.json")
    return await anyio.to_thread.run_sync(_build_capability_catalog_payload, path)


# Named Control Center error when the Slack gateway status projection cannot
# be built (task #1185).
SLACK_GATEWAY_UNAVAILABLE = "slack gateway status unavailable"

# Evidence-event channels the Slack ingress adapter writes (slack_gateway.py /
# slack_outbound.py). Read-only: this pane never dispatches or replies.
_SLACK_EVENT_CHANNELS = (
    "slack.inbound",
    "slack.dispatch.failed",
    "slack.outbound",
    "slack.outbound.failed",
)


def _slack_gateway_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Slack Gateway status pane."""
    return {
        "error": SLACK_GATEWAY_UNAVAILABLE,
        "code": "slack_gateway_unavailable",
        "status": "UNKNOWN",
        "reasons": [],
        "recent_events": [],
        **extra,
    }


def _slack_gateway_status(config: Any) -> tuple[str, list[str]]:
    """Derive a Doctor-style LIVE/DORMANT/MISCONFIGURED label.

    Never inspects a secret value — only presence/absence of the signing
    secret and bot token, and the non-secret allowlists (channels, users).
    """
    if not config.enabled:
        return "DORMANT", ["UA_SLACK_ENABLED is not set"]
    reasons: list[str] = []
    if not config.signing_secret:
        reasons.append("SLACK_SIGNING_SECRET is not configured")
    if not config.users:
        reasons.append("UA_SLACK_USERS allowlist is empty")
    if not config.channel_ids:
        reasons.append("UA_SLACK_CHANNEL_IDS / UA_SLACK_CHANNEL_MAP is empty")
    if not config.bot_token:
        reasons.append("SLACK_BOT_TOKEN is not configured (outbound replies disabled)")
    if reasons:
        return "MISCONFIGURED", reasons
    return "LIVE", []


async def slack_gateway_payload() -> dict[str, Any]:
    """Read-only Slack gateway health / recent ingress activity (task #1185).

    Status-only surface for the Slack HTTP ingress adapter
    (``webui/routes.py::slack_events`` -> ``slack_gateway.handle_http_request``):
    never sends a Slack message and never expands the write surface. Derives
    LIVE / DORMANT / MISCONFIGURED from ``SlackConfig.from_env()`` — presence
    checks only, never a secret value — and lists the most recent
    ``slack.inbound`` / ``slack.dispatch.failed`` / ``slack.outbound*``
    evidence-log entries via the existing read-only ``event_log_query`` tool.
    Failures surface as a named ``slack gateway status unavailable`` payload.
    """
    try:
        from .. import slack_gateway as slack_gateway_module

        config = slack_gateway_module.SlackConfig.from_env()
        status, reasons = _slack_gateway_status(config)

        raw = await call_tool("event_log_query", channels=list(_SLACK_EVENT_CHANNELS), limit=20)
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _slack_gateway_unavailable()

    if not isinstance(raw, dict):
        return _slack_gateway_unavailable(status=status, reasons=reasons)

    events_raw = raw.get("events")
    events_list = (
        [row for row in events_raw if isinstance(row, dict)] if isinstance(events_raw, list) else []
    )
    recent_events = [
        {
            "ulid": row.get("ulid"),
            "channel": row.get("channel"),
            "created_at": row.get("created_at"),
        }
        for row in events_list
    ]
    error_count = sum(
        1 for row in recent_events if str(row.get("channel") or "").endswith(".failed")
    )
    last_event_at = recent_events[0]["created_at"] if recent_events else None

    return {
        "status": status,
        "reasons": reasons,
        "enabled": config.enabled,
        "channel_count": len(config.channel_ids),
        "user_allowlist_count": len(config.users),
        "signing_secret_present": bool(config.signing_secret),
        "bot_token_present": bool(config.bot_token),
        "coordinator_agent": config.coordinator_agent,
        "recent_events": recent_events,
        "recent_error_count": error_count,
        "last_event_at": last_event_at,
        "config_pointer": (
            "UA_SLACK_ENABLED, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, "
            "UA_SLACK_USERS, UA_SLACK_CHANNEL_IDS / UA_SLACK_CHANNEL_MAP"
        ),
    }


# Named Control Center error when the notify delivery status projection
# cannot be built (task #1183).
NOTIFY_DELIVERY_UNAVAILABLE = "notify delivery status unavailable"


def _notify_delivery_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Notify Delivery Status strip."""
    return {
        "error": NOTIFY_DELIVERY_UNAVAILABLE,
        "code": "notify_delivery_unavailable",
        "deliveries": [],
        "count": 0,
        "last_delivery": None,
        **extra,
    }


async def notify_delivery_payload() -> dict[str, Any]:
    """Read-only outbound notify/iMessage delivery outcomes (task #1183).

    Passes through ``notify_delivery_status()`` — a projection of the durable
    delivery log ``notify()`` / ``batch_notify()`` already write on every
    attempt. Never sends a notification: the send path stays exactly where it
    was, MCP/approval-gated, with no new unsolicited send path from `/ui`.
    Failures surface as a named ``notify delivery status unavailable``
    payload.
    """
    try:
        raw = await call_tool("notify_delivery_status", limit=20)
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _notify_delivery_unavailable()

    if not isinstance(raw, dict):
        return _notify_delivery_unavailable()

    deliveries_raw = raw.get("deliveries")
    if not isinstance(deliveries_raw, list):
        return _notify_delivery_unavailable()

    deliveries = [row for row in deliveries_raw if isinstance(row, dict)]
    return {
        "deliveries": deliveries,
        "count": raw.get("count", len(deliveries)),
        "last_delivery": raw.get("last_delivery"),
        "success_count": raw.get("success_count"),
        "failure_count": raw.get("failure_count"),
    }


# Named Control Center error when the dispatch delivery receipts projection
# cannot be built (task #1182).
DISPATCH_RECEIPTS_UNAVAILABLE = "dispatch delivery receipts unavailable"


def _dispatch_receipts_unavailable(**extra: Any) -> dict[str, Any]:
    """Structured empty-shape payload for the Delivery Receipts pane."""
    return {
        "error": DISPATCH_RECEIPTS_UNAVAILABLE,
        "code": "dispatch_receipts_unavailable",
        "receipts": [],
        "count": 0,
        **extra,
    }


async def dispatch_receipts_payload(
    *, task_id: int | None = None, state: str | None = None
) -> dict[str, Any]:
    """Read-only recent dispatch delivery-receipt summaries (task #1182).

    Passes through ``dispatch_delivery_receipts_list()`` — a projection of the
    existing ``dispatch_runs`` ledger, never a second one. Never mutates a run
    and never weakens an evidence gate. For the full accountability receipt
    (transitions, policy/approval outcome, executor auth proof) on one run,
    call the existing ``dispatch_delivery_receipt(run_id=...)`` tool. Failures
    surface as a named ``dispatch delivery receipts unavailable`` payload.
    """
    try:
        raw = await call_tool(
            "dispatch_delivery_receipts_list", task_id=task_id, state=state, limit=20
        )
    except Exception:  # noqa: BLE001 - named error state, never 500 the pane
        return _dispatch_receipts_unavailable()

    if not isinstance(raw, dict):
        return _dispatch_receipts_unavailable()

    receipts_raw = raw.get("receipts")
    if not isinstance(receipts_raw, list):
        return _dispatch_receipts_unavailable()

    receipts = [row for row in receipts_raw if isinstance(row, dict)]
    return {
        "receipts": receipts,
        "count": raw.get("count", len(receipts)),
        "terminal_count": raw.get("terminal_count"),
    }


async def skills_teams_payload(
    actor: str, action: str = "catalog", params: dict | None = None
) -> dict:
    """Catalog HTTP adapter over the registered tool, with verified identity."""
    from .. import config, config_revisions, skill_catalog
    from ..tools.approvals import ApprovalPendingError

    def _run() -> dict:
        token = config.agent_identity_var.set(actor)
        try:
            return skill_catalog.call(action, params or {})
        finally:
            config.agent_identity_var.reset(token)

    try:
        return await anyio.to_thread.run_sync(_run)
    except ApprovalPendingError as exc:
        return {
            "pending": True,
            "approval_id": exc.approval_id,
            "code": exc.code,
            "error": str(exc),
            "status": 202,
        }
    except config_revisions.ConfigFanoutError as exc:
        return exc.result
