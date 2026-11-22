"""Approval-gated self-healing for the private GCP worker (task #1043).

Scope is deliberately one recovery class: restarting the single existing
server-configured instance when it is verifiably stopped. There is no scaling, no
instance creation, no deletion, and no stop -- nothing here can destroy state.

Three properties carry the safety argument, and each is pinned by a test:

1. **Fails closed on an unclear signal.** Recovery runs only when health was
   actually read and says `stopped`. An unreadable inventory section is
   `unknown`, never "stopped, go fix it". This is the failure mode observed
   live: a hub credential without `compute.instances.list` reported a RUNNING
   worker as an empty project.

2. **Cannot bypass or rotate around the approval queue.** The mutation goes
   through `gcloud_ops`, whose approval gate is the authority. This module
   additionally refuses to act when `gcloud_ops` is not in the hub's gated-tool
   allowlist, so a config change that silently disables the queue disables
   recovery too rather than letting it run ungoverned. A denial is terminal
   for the cooldown window -- it is never retried into an approval.

3. **Idempotent under repeat calls.** Attempts are recorded on the audit event
   log with a dedupe key, and a cooldown window blocks a second attempt after
   a recent one regardless of what triggered it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events

# One recovery class, one instance. Widening this is a deliberate change, not
# a parameter an agent can pass in.
RECOVERY_ACTION = "instance_start"

# Recovery attempts land here; the channel doubles as the cooldown source and
# the audit trail, so there is no second store to drift out of sync.
RECOVERY_CHANNEL = "agent.cloud.recovery"

# A start takes well under a minute, but a flapping instance must not be
# restarted in a loop. One attempt per window, then a human looks.
COOLDOWN_MINUTES = 30

# States that justify a restart. Anything else -- including "unknown" -- does not.
_RECOVERABLE_STATES = frozenset({"stopped"})

# How many recent recovery events to scan for a cooldown hit. Read newest-first,
# so this only needs to cover the attempts that could fall inside the window.
_ATTEMPT_SCAN_LIMIT = 50


def _now() -> datetime:
    """The module's clock, as a seam.

    The cooldown window and the dedupe bucket must advance together; reading
    the wall clock in two places would let them disagree.
    """
    return datetime.now(UTC)


def _recent_attempt(target: str, now: datetime) -> dict[str, Any] | None:
    """Most recent recovery attempt for `target` inside the cooldown window."""
    cutoff = now - timedelta(minutes=COOLDOWN_MINUTES)
    # newest_first is load-bearing. query_events defaults to ascending ULID, so
    # a bare limit returns the OLDEST rows on the channel -- once this channel
    # held more than `limit` events, every one of them would be far outside the
    # cooldown window and the cooldown would silently stop firing.
    rows = events.query_events(
        channels=[RECOVERY_CHANNEL], limit=_ATTEMPT_SCAN_LIMIT, newest_first=True
    )
    for row in rows:
        payload = row.get("payload") or {}
        if payload.get("target") != target:
            continue
        raw = row.get("created_at")
        if not raw:
            continue
        try:
            created = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created >= cutoff:
            return {
                "at": created.isoformat(),
                "outcome": payload.get("outcome"),
                "ulid": row.get("ulid"),
            }
    return None


def _outcome(
    outcome: str,
    detail: str,
    *,
    target: str,
    health: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "outcome": outcome,
        "detail": detail,
        "target": target,
        "action": RECOVERY_ACTION,
        "checked_at": _now().isoformat(),
    }
    if health is not None:
        result["health"] = health
    result.update(extra)
    return result


def evaluate_recovery(inventory: dict[str, Any], target: str) -> dict[str, Any]:
    """Decide whether `inventory` justifies restarting `target`.

    Pure and side-effect free, so the decision can be tested apart from the
    approval and event-log machinery.

    Returns {eligible: bool, state: str, reason: str}.
    """
    sections = inventory.get("sections") or {}
    if sections.get("instances") != "ok":
        return {
            "eligible": False,
            "state": "unknown",
            "reason": (
                "instances section was not readable; refusing to act on an unverified health signal"
            ),
        }

    instances = inventory.get("instances") or []
    matches = [i for i in instances if i.get("name") == target]
    if len(matches) > 1:
        return {
            "eligible": False,
            "state": "unknown",
            "reason": f"{target} is ambiguous in readable inventory; refusing recovery",
        }
    match = matches[0] if matches else None

    if match is None:
        # The section read cleanly and the instance is not in it. That is a
        # missing VM, which recovery here cannot and must not create.
        return {
            "eligible": False,
            "state": "absent",
            "reason": f"{target} is not present in this project; creating an instance is out of scope",
        }

    status = str(match.get("status") or "").upper()
    if status == "RUNNING":
        return {"eligible": False, "state": "running", "reason": f"{target} is already running"}
    if status in {"TERMINATED", "STOPPED", "SUSPENDED"}:
        return {
            "eligible": True,
            "state": "stopped",
            "reason": f"{target} reports {status}; a start is the allowlisted recovery",
        }
    # PROVISIONING / STAGING / STOPPING / REPAIRING: in transition. Acting now
    # would race the transition.
    return {
        "eligible": False,
        "state": "transitional",
        "reason": f"{target} reports {status}; waiting for it to settle rather than acting mid-transition",
    }


def _cloud_worker_recover(
    target: str | None = None,
    project: str | None = None,
    zone: str | None = None,
    dry_run: bool = True,
    *,
    before_mutation: Callable[[], None] | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Internal recovery core; the public wrapper carries the MCP contract."""

    from .gcloud import _configured_cloud_target, gcloud_inventory, gcloud_ops

    if project is not None or zone is not None:
        raise ToolError("project and zone are server-configured; caller overrides are refused")
    try:
        server_project, server_worker = _configured_cloud_target()
    except ToolError as exc:
        return _outcome("unknown", str(exc), target="")
    if target is not None and target != server_worker:
        raise ToolError(f"only {server_worker!r} is an allowed recovery target")
    target = server_worker
    project = server_project

    now = _now()

    inventory = gcloud_inventory(project=project, sections=["instances"])
    health = {
        "status": inventory.get("status"),
        "sections": inventory.get("sections") or {},
        "errors": inventory.get("errors") or [],
    }

    decision = evaluate_recovery(inventory, target)
    state = decision["state"]

    if state == "unknown":
        return _outcome("unknown", decision["reason"], target=target, health=health)
    if state == "running":
        return _outcome("healthy", decision["reason"], target=target, health=health)
    if not decision["eligible"]:
        return _outcome(
            "not_eligible", decision["reason"], target=target, health=health, state=state
        )

    # An instance name is only unique within a zone. Never select the first
    # of duplicate names or let an omitted zone fall back inside gcloud_ops.
    matches = [i for i in inventory.get("instances") or [] if i.get("name") == target]
    if len(matches) != 1:
        return _outcome(
            "unknown", "worker zone is ambiguous; refusing recovery", target=target, health=health
        )
    raw_zone = matches[0].get("zone")
    if not isinstance(raw_zone, str) or not re.fullmatch(r"[a-z][a-z0-9-]*[0-9]-[a-z]", raw_zone):
        return _outcome(
            "unknown",
            "worker zone is missing or invalid; refusing recovery",
            target=target,
            health=health,
        )
    zone = raw_zone

    # Eligible from here on.
    recent = _recent_attempt(target, now)
    if recent is not None:
        return _outcome(
            "cooldown",
            (
                f"a recovery attempt for {target} was recorded at {recent['at']} "
                f"(outcome: {recent['outcome']}); within the {COOLDOWN_MINUTES}m cooldown"
            ),
            target=target,
            health=health,
            previous_attempt=recent,
        )

    # Defense in depth. gcloud_ops owns the approval decision, but if the hub's
    # gated-tool allowlist no longer covers it, the mutation would run
    # ungoverned -- so refuse rather than quietly self-heal without oversight.
    if "gcloud_ops" not in config.approval_gated_tools():
        return _outcome(
            "ungoverned",
            (
                "gcloud_ops is not in the hub's approval-gated tool allowlist; "
                "refusing to perform unapproved recovery"
            ),
            target=target,
            health=health,
        )

    if dry_run:
        return _outcome(
            "would_recover",
            f"would request {RECOVERY_ACTION} for {target} (approval still required)",
            target=target,
            health=health,
        )

    # One durable attempt record per target per cooldown window. Written
    # *before* the mutation: a crash mid-start must leave evidence that an
    # attempt happened, so the next call cools down instead of retrying blind.
    # Bucket the dedupe key by the cooldown window, not an arbitrary minute, so
    # the race guard and the policy window describe the same interval.
    bucket = int(now.timestamp()) // (COOLDOWN_MINUTES * 60)
    dedupe = f"{RECOVERY_CHANNEL}:{target}:{bucket}"
    attempt_id, is_first = events.record_event_once(
        RECOVERY_CHANNEL,
        payload={
            "target": target,
            "action": RECOVERY_ACTION,
            "outcome": "attempted",
            **({"cycle_id": cycle_id} if cycle_id else {}),
        },
        originator="cloud_worker_recover",
        dedupe_key=dedupe,
    )
    if not is_first:
        return _outcome(
            "cooldown",
            f"an identical recovery attempt for {target} is already recorded",
            target=target,
            health=health,
            attempt_id=attempt_id,
        )

    if before_mutation is not None:
        try:
            before_mutation()
        except Exception as exc:  # noqa: BLE001 - classify stale cycle state
            return _outcome(
                "blocked",
                f"recovery cycle was no longer eligible before mutation: {exc}",
                target=target,
                health=health,
                attempt_id=attempt_id,
                cycle_id=cycle_id,
            )

    try:
        res = gcloud_ops(action=RECOVERY_ACTION, target=target, project=project, zone=zone)
    except Exception as exc:  # noqa: BLE001 - classified and reported below
        name = type(exc).__name__
        text = str(exc)
        # An approval gate raises to signal "a human must decide", which is the
        # system working, not a failure. A denial is terminal: returning it as
        # an error would invite a caller to retry until someone says yes.
        if "ApprovalPending" in name:
            outcome, detail = (
                "awaiting_approval",
                f"recovery requested; awaiting human approval: {text[:200]}",
            )
        elif "ApprovalDenied" in name or "denied" in text.lower():
            outcome, detail = "denied", f"recovery denied by a human; not retried: {text[:200]}"
        else:
            outcome, detail = "failed", f"{name}: {text[:200]}"
        events.record_event(
            RECOVERY_CHANNEL,
            payload={
                "target": target,
                "action": RECOVERY_ACTION,
                "outcome": outcome,
                **({"cycle_id": cycle_id} if cycle_id else {}),
            },
            originator="cloud_worker_recover",
        )
        return _outcome(outcome, detail, target=target, health=health, attempt_id=attempt_id)

    events.record_event(
        RECOVERY_CHANNEL,
        payload={
            "target": target,
            "action": RECOVERY_ACTION,
            "outcome": "recovered",
            **({"cycle_id": cycle_id} if cycle_id else {}),
        },
        originator="cloud_worker_recover",
    )
    return _outcome(
        "recovered",
        f"{RECOVERY_ACTION} completed for {target}",
        target=target,
        health=health,
        attempt_id=attempt_id,
        result=res,
    )


def cloud_worker_recover(
    target: str | None = None,
    project: str | None = None,
    zone: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Detect a stopped private worker and, with approval, start it.

    ``dry_run`` defaults to true and reports what would happen without writing
    an attempt or mutating infrastructure. A real run requires the existing
    ``gcloud_ops`` approval gate, cooldown, and named-worker safeguards. The
    result outcome is one of ``unknown``, ``healthy``, ``not_eligible``,
    ``cooldown``, ``ungoverned``, ``would_recover``, ``awaiting_approval``,
    ``denied``, ``failed``, or ``recovered``. ``project`` and ``zone`` are
    server-configured and caller overrides are refused. ``target`` may only
    name the configured ``UA_GCP_WORKER``; omitting it selects that worker.
    """
    return _cloud_worker_recover(target, project, zone, dry_run)
