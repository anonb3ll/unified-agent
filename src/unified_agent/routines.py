"""Durable, opt-in routine scheduler.

One database transaction owns each tick, its task rows, and its fire receipts.
The unique fire key makes a repeated tick or API retry harmless.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from threading import RLock

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage, table_schemas
from .change_feed import task_changed
from .sync import schedule_sync_to_vault
from .tools.tasks import DOMAINS, PROJECTS, _resolve_lane_for_add

_MAX_DUE_PER_TICK = 100
_TERMINAL = ("done", "cancelled")
ACTIVITY_GATES = ("always", "require_activity")
ACTIVITY_GATE_SCOPES = ("org", "project")
# Routine bookkeeping in event_log: the create event of a fire and the release
# event of a queued run. Never activity, or a gate would see its own last fire.
_ROUTINE_EVENT_KEYS = ("routine:%", "routine-run:%")
# Why a fire produced no task; every skipped or suppressed receipt carries one.
SKIP_OVERLAP = "overlap"  # skipped: concurrency=skip and an earlier task is active
SKIP_NO_SIGNAL = "no_signal"  # suppressed: the gate's scope saw nothing at all
SKIP_SELF_ECHO = "self_echo"  # suppressed: the scope saw only routine bookkeeping
SKIP_CATCH_UP_NONE = "catch_up_none"  # skipped: catch_up=none passed over this slot
SKIP_REASONS = (SKIP_OVERLAP, SKIP_NO_SIGNAL, SKIP_SELF_ECHO, SKIP_CATCH_UP_NONE)
_MAX_RECENT_RUNS = 100
_RECENT_RUN_COLUMNS = (
    "id,fire_key,scheduled_for,status,reason,subject_key,task_id,created_at,"
    "window_start,gate_matched"
)
_manual_fire_lock = RLock()


# Server-owned context, never a tool argument: set only by the Control Center's
# "Run now" route for the verified human operator (task #2480).
_operator_run_now: ContextVar[bool] = ContextVar("ua_routine_operator_run_now", default=False)


# Server-owned receipt namespace (task #2480, review round 3). An ordinary
# ``routine_fire`` call (MCP or the bearer webhook route) stores ``event:<key>``;
# the Control Center's Run now stores ``cc-run:<key>``. Only the Run now route
# enters ``run_now_namespace()``, so no caller-supplied key reaches that namespace
# and a look-alike bearer key cannot capture a browser fire.
_fire_key_prefix: ContextVar[str] = ContextVar("ua_routine_fire_key_prefix", default="event:")


@contextmanager
def run_now_namespace():
    """Store this call's fire receipt under the Run now namespace (``cc-run:``)."""
    token = _fire_key_prefix.set("cc-run:")
    try:
        yield
    finally:
        _fire_key_prefix.reset(token)


@contextmanager
def operator_run_now():
    """Let ``routine_fire`` fire a cron routine once, outside its schedule (task #2480).

    Entered only by ``POST /api/action/routine/run`` around one guarded
    ``routine_fire`` call, after the route has verified the human operator. The
    fire is an ordinary ``event:<key>`` receipt: the same idempotency, pause and
    concurrency rules apply, and the cron cursor is not moved, so the next
    scheduled slot still fires.
    """
    token = _operator_run_now.set(True)
    try:
        yield
    finally:
        _operator_run_now.reset(token)


# --- Control Center "Run now" receipts (task #2480) ----------------------------------
#
# ``routine_fire`` replays from the live ``routine_runs`` row, which a queue release
# later rewrites (queued -> created), and scopes its key to one routine. The browser
# contract is stricter: the first answer is immutable, a key is bound to the request
# that first used it, and two callers never share a run by sharing a header key.
# Receipts live in ``idempotency_keys`` through the tenant-scoped storage helpers.

_RUN_NOW_TOOL = "cc_routine_run"
#: How long a settled Run now receipt is kept; older receipts are pruned on use.
#: Only the frozen answer expires: the derived fire key, and the routine's own
#: permanent ``routine_runs`` receipt for it, stay bound to the first fire.
#: Must stay longer than ``RUN_NOW_PENDING_STALE`` so pruning never removes a
#: reservation still inside its takeover window.
RUN_NOW_RECEIPT_TTL = timedelta(days=7)
#: A reservation older than this is taken to belong to a request that died.
RUN_NOW_PENDING_STALE = timedelta(minutes=10)
_RUN_NOW_PRUNE_BATCH = 500


class RunNowInFlight(ToolError):
    """The same Run now request is still being processed."""

    code = "in_flight"


def _run_now_row_key(identity: str, key: str) -> str:
    return f"cc.routine.run:{identity}:{key}"


def run_now_digest(body: dict) -> str:
    """The request digest a Run now key is bound to."""
    import hashlib as _hashlib
    import json as _json

    return _hashlib.sha256(_json.dumps(body, sort_keys=True).encode()).hexdigest()


def run_now_fire_key(identity: str, key: str) -> str:
    """The ``routine_fire`` idempotency key for one caller's Run now key.

    Bounded and derived from tenant, identity and client key, so two callers who
    send the same header key get their own fires, and the same caller's repeat
    (including the one after an approval) derives the same key.
    """
    import hashlib as _hashlib

    material = "\x1f".join((edition.tenant_org_id() or "", identity, key))
    return "cc:" + _hashlib.sha256(material.encode()).hexdigest()[:48]


def run_now_begin(identity: str, key: str, digest: str) -> tuple[str | None, dict | None]:
    """Reserve ``key`` for this request or return its stored first answer.

    Returns ``(owner, None)`` when the caller now owns the reservation and must fire
    and then call ``run_now_finish`` with ``owner``; ``(None, response)`` when the key
    already settled for the same request. Raises ``ConflictError`` when the key was
    used for a different request, ``RunNowInFlight`` while another request with the
    same key is still running (a reservation older than ``RUN_NOW_PENDING_STALE`` is
    taken over), and ``storage.AmbiguousIdempotencyReceipt`` for a receipt written
    before tenant scoping. Prunes receipts older than ``RUN_NOW_RECEIPT_TTL``.
    The receipt mechanics are ``storage.request_receipt_begin``.
    """
    from .tool_errors import ConflictError

    try:
        return storage.request_receipt_begin(
            _RUN_NOW_TOOL,
            _run_now_row_key(identity, key),
            digest,
            ttl=RUN_NOW_RECEIPT_TTL,
            stale=RUN_NOW_PENDING_STALE,
            prune_batch=_RUN_NOW_PRUNE_BATCH,
        )
    except storage.ReceiptConflict:
        raise ConflictError(
            "Idempotency-Key was already used for a different Run now request; "
            "use a new key for a new run"
        ) from None
    except storage.ReceiptInFlight:
        raise RunNowInFlight(
            "this Run now request is still in flight; repeat it shortly to read its answer"
        ) from None


def run_now_finish(
    identity: str, key: str, digest: str, owner: str, response: dict | None
) -> tuple[bool, dict | None]:
    """Settle the reservation ``owner`` holds: ``(still_owner, canonical answer)``.

    With a ``response`` the reservation becomes the immutable first answer; with
    None (nothing ran: refused, parked, failed) it is dropped so the request can be
    repeated. Only the owner settles or drops it. A caller that lost ownership (its
    reservation was taken over) gets ``(False, stored answer or None)`` and must
    return that stored answer, or ``in_flight`` when none is stored yet, never its
    own local answer.
    """
    return storage.request_receipt_finish(
        _RUN_NOW_TOOL, _run_now_row_key(identity, key), digest, owner, response
    )


@contextmanager
def manual_fire_guard():
    """Serialize one-shot approval checks with routine pauses and manual fires."""
    with _manual_fire_lock:
        yield


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables (the DDL lives in ``table_schemas``)."""
    table_schemas.routines(conn)


def _utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ToolError("timestamp must include a UTC offset")
    return result.astimezone(UTC)


def _field(spec: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        base, sep, step_text = part.partition("/")
        step = int(step_text) if sep and step_text.isdigit() else 1
        if step < 1 or (sep and not step_text.isdigit()):
            raise ToolError("invalid cron step")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            first, last = int(a), int(b)
        else:
            first = int(base)
            last = high if sep else first
        if not (low <= first <= last <= high):
            raise ToolError("cron field out of range")
        values.update(range(first, last + 1, step))
    if not values:
        raise ToolError("empty cron field")
    return values


def _cron_fields(schedule: str) -> tuple[set[int], ...]:
    parts = schedule.split()
    if len(parts) != 5:
        raise ToolError("cron schedule must have five UTC fields")
    try:
        return tuple(
            _field(part, low, high)
            for part, (low, high) in zip(
                parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True
            )
        )
    except ValueError as exc:
        raise ToolError(f"invalid cron field: {exc}") from exc


def cron_next(schedule: str, after: datetime) -> datetime:
    """Next matching UTC minute strictly after `after` (five-field cron)."""
    minute, hour, day, month, weekday = _cron_fields(schedule)
    candidate = _utc(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # The Gregorian calendar repeats every 400 years. Jump by month, day, and
    # hour so rare day-of-week/leap-day combinations remain valid and bounded.
    deadline = candidate + timedelta(days=146_097)
    while candidate <= deadline:
        if candidate.month not in month:
            candidate = (candidate.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(
                day=1
            )
            continue
        cron_weekday = (candidate.weekday() + 1) % 7
        if candidate.day not in day or not (
            cron_weekday in weekday or (cron_weekday == 0 and 7 in weekday)
        ):
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in hour:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        if candidate.minute in minute:
            return candidate
        candidate += timedelta(minutes=1)
    raise ToolError("cron schedule has no occurrence within 400 years")


def _cron_latest(schedule: str, cutoff: datetime, after: datetime) -> datetime | None:
    """Latest due occurrence after cursor, without replaying intermediate fires."""
    minute, hour, day, month, weekday = _cron_fields(schedule)
    candidate = cutoff.replace(second=0, microsecond=0)
    while candidate > after:
        if candidate.month not in month:
            candidate = candidate.replace(day=1, hour=0, minute=0) - timedelta(minutes=1)
            continue
        cron_weekday = (candidate.weekday() + 1) % 7
        if candidate.day not in day or not (
            cron_weekday in weekday or (cron_weekday == 0 and 7 in weekday)
        ):
            candidate = candidate.replace(hour=0, minute=0) - timedelta(minutes=1)
            continue
        if candidate.hour not in hour:
            candidate = candidate.replace(minute=0) - timedelta(minutes=1)
            continue
        if candidate.minute in minute:
            return candidate
        candidate -= timedelta(minutes=1)
    return None


def routine_add(
    title: str,
    *,
    trigger: str = "cron",
    schedule: str | None = None,
    notes: str | None = None,
    lane: str = "default",
    project: str = "unified-agent",
    domain: str = "hobby",
    concurrency: str = "skip",
    catch_up: str = "last",
    start_at: str | None = None,
    activity_gate: str = "always",
    activity_gate_scope: str = "org",
) -> dict:
    """Register an inert routine. Cron ticks require UA_ROUTINES_ENABLED=1.

    `activity_gate="require_activity"` suppresses a fire when nothing happened
    in `activity_gate_scope` (`org`, or `project` = this routine's project)
    since its previous fire; the default `always` fires on every schedule.
    """
    if not title.strip() or "\n" in title or len(title) > 200:
        raise ToolError("title must be one nonempty line of at most 200 characters")
    if trigger not in ("cron", "api", "webhook"):
        raise ToolError("trigger must be cron, api, or webhook")
    if (trigger == "cron") != (schedule is not None):
        raise ToolError("cron requires schedule; api/webhook must omit it")
    if activity_gate not in ACTIVITY_GATES:
        raise ToolError("activity_gate must be always or require_activity")
    if activity_gate_scope not in ACTIVITY_GATE_SCOPES:
        raise ToolError("activity_gate_scope must be org or project")
    if activity_gate_scope == "project" and not (project or "").strip():
        raise ToolError("activity_gate_scope=project needs the routine to have a project")
    if project not in PROJECTS or domain not in DOMAINS:
        raise ToolError("unknown project or domain")
    lane = _resolve_lane_for_add(lane, None)
    if concurrency not in ("skip", "queue") or catch_up not in ("none", "last", "all"):
        raise ToolError("invalid concurrency or catch_up policy")
    try:
        start = _utc(start_at) if start_at else datetime.now(UTC)
    except ValueError as exc:
        raise ToolError(f"invalid start_at: {exc}") from exc
    if schedule is not None:
        cron_next(schedule, start)
    ts = datetime.now(UTC).isoformat()
    with closing(storage.connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO routines (title,notes,trigger,schedule,lane,project,domain,"
            "concurrency,catch_up,cursor_at,created_at,updated_at,org_id,untrusted,"
            "activity_gate,activity_gate_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                title.strip(),
                notes,
                trigger,
                schedule,
                lane,
                project,
                domain,
                concurrency,
                catch_up,
                start.isoformat(),
                ts,
                ts,
                edition.tenant_org_id(),
                int(config.is_agent_untrusted(config.verified_agent_identity())),
                activity_gate,
                activity_gate_scope,
            ),
        )
        result = {"id": cur.lastrowid}
    return result


def routine_list(*, include_paused: bool = True, recent_runs: int = 0) -> dict:
    """List routine definitions in the caller's tenant.

    `recent_runs` above 0 (at most 100) adds each routine's newest receipts,
    newest first, including skips with their typed `reason` and `subject_key`.
    """
    limit = min(recent_runs, _MAX_RECENT_RUNS)
    with closing(storage.connect()) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM routines WHERE org_id IS ? AND (? OR paused=0) ORDER BY id",
            (edition.tenant_org_id(), int(include_paused)),
        ).fetchall()
        routines = [dict(row) for row in rows]
        if limit > 0:
            for routine in routines:
                routine["recent_runs"] = [
                    dict(run)
                    for run in conn.execute(
                        f"SELECT {_RECENT_RUN_COLUMNS} FROM routine_runs WHERE routine_id=? "
                        "ORDER BY id DESC LIMIT ?",
                        (routine["id"], limit),
                    )
                ]
        return {"routines": routines}


def routine_pause(routine_id: int, *, paused: bool = True) -> dict:
    with manual_fire_guard(), closing(storage.connect()) as conn, conn:
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "UPDATE routines SET paused=?,updated_at=?,"
            "cursor_at=CASE WHEN paused=1 AND ?=0 THEN ? ELSE cursor_at END "
            "WHERE id=? AND org_id IS ? RETURNING id,paused",
            (int(paused), now, int(paused), now, routine_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError("unknown routine")
        return dict(row)


def _active_task(conn: sqlite3.Connection, routine_id: int) -> int | None:
    """The routine's oldest unfinished task, or None when it has none."""
    row = conn.execute(
        "SELECT t.id FROM routine_runs rr JOIN tasks t ON t.id=rr.task_id "
        "WHERE rr.routine_id=? AND t.status NOT IN ('done','cancelled') ORDER BY rr.id LIMIT 1",
        (routine_id,),
    ).fetchone()
    return None if row is None else row["id"]


def _activity_window_start(conn: sqlite3.Connection, routine: sqlite3.Row) -> str:
    """When the gate window opens: the previous fire or suppression, else creation.

    A skipped fire reported nothing, so it does not move the window. Truncated
    to whole seconds because task and event times are second-precision, so
    activity in the same second as the previous fire still counts.
    """
    row = conn.execute(
        "SELECT created_at FROM routine_runs WHERE routine_id=? "
        "AND status IN ('created','queued','suppressed') ORDER BY id DESC LIMIT 1",
        (routine["id"],),
    ).fetchone()
    start = row["created_at"] if row is not None else routine["created_at"]
    return _utc(start).isoformat(timespec="seconds")


def _scope_activity(conn: sqlite3.Connection, routine: sqlite3.Row, since: str) -> tuple[int, int]:
    """Count activity in the routine's gate scope at or after ``since``.

    Returns ``(matched, echoes)``. Activity is task rows created or updated,
    plus event_log rows. Everything is fenced to the routine's tenant (the
    tenant that stamped it). Tasks any routine spawned, every event about them,
    and routine bookkeeping events never match; they are counted as echoes, so
    a quiet gate can tell "only routines ran" from "nothing happened". event_log
    rows carry no project, so ``project`` scope counts only events whose payload
    names a task in that project (``task_id``, or ``ref_id`` on a ``kind: task``
    event).
    """
    org_id = routine["org_id"]
    project_scope = int(routine["activity_gate_scope"] == "project")
    # Tasks any routine spawned are bookkeeping, not activity: otherwise two
    # gated routines in one scope keep each other firing forever.
    own = "SELECT task_id FROM routine_runs WHERE task_id IS NOT NULL"
    tasks, task_echoes = conn.execute(
        f"SELECT count(*), COALESCE(SUM(id IN ({own})),0) FROM tasks "
        "WHERE org_id IS ? AND (created_at>=? OR updated_at>=?) AND (?=0 OR project=?)",
        (org_id, since, since, project_scope, routine["project"]),
    ).fetchone()
    task_events, event_echoes = conn.execute(
        "WITH ev AS (SELECT dedupe_key,CAST(CASE WHEN json_valid(payload) THEN "
        "COALESCE(json_extract(payload,'$.task_id'),CASE WHEN "
        "json_extract(payload,'$.kind')='task' THEN json_extract(payload,'$.ref_id') END) "
        "END AS INTEGER) AS task_id "
        "FROM event_log WHERE org_id IS ? AND created_at>=?) "
        "SELECT count(*), COALESCE(SUM((dedupe_key IS NOT NULL AND "
        "(dedupe_key LIKE ? OR dedupe_key LIKE ?)) OR "
        f"(task_id IS NOT NULL AND task_id IN ({own}))),0) FROM ev "
        "WHERE (?=0 OR task_id IN (SELECT id FROM tasks WHERE org_id IS ? AND project=?))",
        (
            org_id,
            since,
            *_ROUTINE_EVENT_KEYS,
            project_scope,
            org_id,
            routine["project"],
        ),
    ).fetchone()
    echoes = task_echoes + event_echoes
    return tasks + task_events - echoes, echoes


def _gate_subject(routine: sqlite3.Row) -> str:
    if routine["activity_gate_scope"] == "project":
        return f"project:{routine['project']}"
    return f"org:{routine['org_id'] or 'default'}"


def _create_fire(
    conn: sqlite3.Connection,
    routine: sqlite3.Row,
    fire_key: str,
    at: datetime,
    publish_after_commit: list[dict],
) -> tuple[str, int | None, str | None, bool]:
    """Write one fire's receipt; returns (status, task_id, reason, created_new)."""
    prior = conn.execute(
        "SELECT status,task_id,reason FROM routine_runs WHERE routine_id=? AND fire_key=?",
        (routine["id"], fire_key),
    ).fetchone()
    if prior is not None:
        return prior["status"], prior["task_id"], prior["reason"], False
    active_task = _active_task(conn, routine["id"])
    active = active_task is not None
    skip = active and routine["concurrency"] == "skip"
    reason = window_start = gate_matched = None
    echoes = 0
    subject_key = f"routine:{routine['id']}"
    if not skip and routine["activity_gate"] == "require_activity":
        window_start = _activity_window_start(conn, routine)
        gate_matched, echoes = _scope_activity(conn, routine, window_start)
    if skip:
        status, task_id, reason = "skipped", None, SKIP_OVERLAP
        subject_key = f"task:{active_task}"
    elif gate_matched == 0:
        # A quiet scope: a receipt and no task. The caller advances the cursor
        # exactly as for a fire, so the next tick does not retry this minute.
        status, task_id = "suppressed", None
        reason = SKIP_SELF_ECHO if echoes else SKIP_NO_SIGNAL
        subject_key = _gate_subject(routine)
    else:
        status = "queued" if active else "created"
        ts = datetime.now(UTC).isoformat()
        notes = (routine["notes"] or "").strip()
        notes = (
            notes + "\n" if notes else ""
        ) + f"[routine: {routine['id']}] [scheduled: {at.isoformat()}]"
        cur = conn.execute(
            "INSERT INTO tasks (title,notes,status,priority,created_at,updated_at,"
            "source_host,project,domain,lane,org_id,untrusted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                routine["title"],
                notes,
                "blocked" if status == "queued" else "open",
                "med",
                ts,
                ts,
                config.write_source_host(),
                routine["project"],
                routine["domain"],
                routine["lane"],
                routine["org_id"],
                int(
                    bool(routine["untrusted"])
                    or config.is_agent_untrusted(config.verified_agent_identity())
                ),
            ),
        )
        task_id = cur.lastrowid
        _, inserted, publish_payload = events.record_event_once_on_connection(
            conn,
            events.CHANNEL_TASK_UPDATED,
            payload={
                "kind": "task",
                "ref_id": str(task_id),
                "task_id": task_id,
                "from_status": None,
                "status": "blocked" if status == "queued" else "open",
                "updated_at": ts,
                "summary": f"routine {routine['id']} created task: {routine['title']}",
            },
            originator=config.verified_agent_identity() or "unified-agent-routines",
            dedupe_key=f"routine:{routine['id']}:{fire_key}:create",
        )
        if not inserted:
            raise RuntimeError("routine task event dedupe collision")
        if publish_payload is not None:
            publish_after_commit.append(publish_payload)
    conn.execute(
        "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,task_id,created_at,"
        "reason,window_start,gate_matched,subject_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            routine["id"],
            fire_key,
            at.isoformat(),
            status,
            task_id,
            datetime.now(UTC).isoformat(),
            reason,
            window_start,
            gate_matched,
            subject_key,
        ),
    )
    return status, task_id, reason, True


def _record_passed_over_slot(conn: sqlite3.Connection, routine: sqlite3.Row, at: datetime) -> bool:
    """Receipt a slot catch_up=none will not fire; True when this tick wrote it."""
    cur = conn.execute(
        "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at,"
        "reason,subject_key) VALUES (?,?,?,'skipped',?,?,?) "
        "ON CONFLICT(routine_id,fire_key) DO NOTHING",
        (
            routine["id"],
            "cron:" + at.isoformat(),
            at.isoformat(),
            datetime.now(UTC).isoformat(),
            SKIP_CATCH_UP_NONE,
            f"routine:{routine['id']}",
        ),
    )
    return cur.rowcount == 1


def _manual_fire_state(conn: sqlite3.Connection, routine_id: int, idempotency_key: str):
    """Validate a manual fire and return its definition plus any saved receipt."""
    if os.environ.get("UA_ROUTINES_ENABLED") != "1":
        raise ToolError("routines are disabled; set UA_ROUTINES_ENABLED=1")
    if not idempotency_key or len(idempotency_key) > 200:
        raise ToolError("idempotency_key must be 1-200 characters")
    routine = conn.execute(
        "SELECT * FROM routines WHERE id=? AND org_id IS ?",
        (routine_id, edition.tenant_org_id()),
    ).fetchone()
    if routine is None:
        raise ToolError("unknown routine")
    previous = conn.execute(
        "SELECT status,task_id FROM routine_runs WHERE routine_id=? AND fire_key=?",
        (routine_id, _fire_key_prefix.get() + idempotency_key),
    ).fetchone()
    if previous is not None:
        return routine, {"status": previous["status"], "task_id": previous["task_id"]}
    if routine["paused"]:
        raise ToolError("routine is paused")
    if routine["trigger"] == "cron" and not _operator_run_now.get():
        raise ToolError("cron routines are fired by the scheduler")
    return routine, None


def routine_fire_preflight(routine_id: int, *, idempotency_key: str) -> dict | None:
    """Read-only HTTP preflight; the transactional fire repeats these checks."""
    with closing(storage.connect()) as conn:
        _, previous = _manual_fire_state(conn, routine_id, idempotency_key)
        return previous


def routine_fire(routine_id: int, *, idempotency_key: str) -> dict:
    """Fire an API/webhook routine using the caller's stable event ID."""
    publish_after_commit: list[dict] = []
    with closing(storage.connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        routine, previous = _manual_fire_state(conn, routine_id, idempotency_key)
        if previous is not None:
            return previous
        status, task_id, _, created_new = _create_fire(
            conn,
            routine,
            _fire_key_prefix.get() + idempotency_key,
            datetime.now(UTC),
            publish_after_commit,
        )
    if task_id and created_new:
        schedule_sync_to_vault()
        task_changed(task_id)
        _record_task_activity(task_id, f"routine {routine_id} created task")
    _publish_events(publish_after_commit)
    return {"status": status, "task_id": task_id}


def _release_queued(conn: sqlite3.Connection, publish_after_commit: list[dict]) -> list[int]:
    """Expose at most one queued task after its earlier work becomes terminal."""
    released: list[int] = []
    rows = conn.execute(
        "SELECT rr.id,rr.routine_id,rr.task_id FROM routine_runs rr "
        "JOIN tasks t ON t.id=rr.task_id "
        "WHERE rr.status='queued' AND t.status='blocked' ORDER BY rr.id"
    ).fetchall()
    for row in rows:
        earlier = conn.execute(
            "SELECT 1 FROM routine_runs old JOIN tasks t ON t.id=old.task_id "
            "WHERE old.routine_id=? AND old.id<? "
            "AND t.status NOT IN ('done','cancelled') LIMIT 1",
            (row["routine_id"], row["id"]),
        ).fetchone()
        if earlier is not None:
            continue
        conn.execute(
            "UPDATE tasks SET status='open',updated_at=? WHERE id=?",
            (storage.next_task_revision(conn, row["task_id"]), row["task_id"]),
        )
        conn.execute("UPDATE routine_runs SET status='created' WHERE id=?", (row["id"],))
        _, inserted, publish_payload = events.record_event_once_on_connection(
            conn,
            events.CHANNEL_TASK_UPDATED,
            payload={
                "kind": "task",
                "ref_id": str(row["task_id"]),
                "task_id": row["task_id"],
                "from_status": "blocked",
                "status": "open",
                "updated_at": storage.now(),
                "summary": "queued routine task released",
            },
            originator="unified-agent-routines",
            dedupe_key=f"routine-run:{row['id']}:release",
        )
        if not inserted:
            raise RuntimeError("routine release event dedupe collision")
        if publish_payload is not None:
            publish_after_commit.append(publish_payload)
        released.append(row["task_id"])
    return released


def _record_task_activity(task_id: int, summary: str) -> None:
    try:
        storage.record_agent_event(
            "task",
            ref_id=str(task_id),
            agent=config.verified_agent_identity() or "unified-agent-routines",
            summary=summary,
            mirror=False,
        )
    except Exception:
        pass


def _publish_events(payloads: list[dict]) -> None:
    for payload in payloads:
        try:
            events.publish(payload)
        except Exception:
            pass


def _count_fire(result: dict, status: str, reason: str | None) -> None:
    result[status] += 1
    if reason is not None:
        result["skip_reasons"][reason] = result["skip_reasons"].get(reason, 0) + 1


def routine_tick(*, now: datetime | None = None, force: bool = False) -> dict:
    """Run one hub-side scheduler tick; safe for repeated or racing callers."""
    if not force and os.environ.get("UA_ROUTINES_ENABLED") != "1":
        return {"status": "disabled", "created": 0, "queued": 0, "skipped": 0}
    cutoff = _utc(now or datetime.now(UTC)).replace(second=0, microsecond=0)
    result: dict = {
        "status": "ok",
        "created": 0,
        "queued": 0,
        "skipped": 0,
        "suppressed": 0,
        "released": 0,
        "skip_reasons": {},
    }
    changed: list[int] = []
    publish_after_commit: list[dict] = []
    with closing(storage.connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        changed.extend(_release_queued(conn, publish_after_commit))
        result["released"] = len(changed)
        routines = conn.execute(
            "SELECT * FROM routines WHERE trigger='cron' AND paused=0 ORDER BY id"
        ).fetchall()
        for routine in routines:
            cursor = _utc(routine["cursor_at"])
            if routine["catch_up"] != "all":
                latest = _cron_latest(routine["schedule"], cutoff, cursor)
                if latest is None:
                    continue
                selected = [latest] if routine["catch_up"] == "last" or latest == cutoff else []
                for at in selected:
                    status, task_id, reason, created_new = _create_fire(
                        conn, routine, "cron:" + at.isoformat(), at, publish_after_commit
                    )
                    if created_new:
                        _count_fire(result, status, reason)
                        if task_id:
                            changed.append(task_id)
                if not selected and _record_passed_over_slot(conn, routine, latest):
                    _count_fire(result, "skipped", SKIP_CATCH_UP_NONE)
                conn.execute(
                    "UPDATE routines SET cursor_at=?,updated_at=? WHERE id=?",
                    (cutoff.isoformat(), datetime.now(UTC).isoformat(), routine["id"]),
                )
                continue
            due: list[datetime] = []
            candidate = cron_next(routine["schedule"], cursor)
            while candidate <= cutoff and len(due) < _MAX_DUE_PER_TICK:
                due.append(candidate)
                cursor = candidate
                candidate = cron_next(routine["schedule"], cursor)
            if not due:
                continue
            for at in due:
                status, task_id, reason, created_new = _create_fire(
                    conn, routine, "cron:" + at.isoformat(), at, publish_after_commit
                )
                if created_new:
                    _count_fire(result, status, reason)
                    if task_id:
                        changed.append(task_id)
            conn.execute(
                "UPDATE routines SET cursor_at=?,updated_at=? WHERE id=?",
                (cursor.isoformat(), datetime.now(UTC).isoformat(), routine["id"]),
            )
            if len(due) == _MAX_DUE_PER_TICK and candidate <= cutoff:
                result["backlog"] = True
    if changed:
        schedule_sync_to_vault()
        for task_id in changed:
            task_changed(task_id)
            _record_task_activity(task_id, "routine task created or released")
    _publish_events(publish_after_commit)
    return result
