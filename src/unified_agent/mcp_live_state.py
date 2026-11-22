"""Read-only MCP resources and live-state subscriptions (task #823).

Board / task / run rows are exposed as MCP resources. Connected clients
subscribe with ``Client.listen(resource_subscriptions=[...])``. Same-request
``task_update`` publishes immediately; a lifespan SQLite poller covers
updates from another process (executor, scheduler).

This module is the **MCP adapter** over :mod:`unified_agent.object_model`
(task #1070). The URIs, names, titles, descriptions, profile gates, and
telemetry keys are the registry's declarations — this file derives them instead
of restating them, so the platform object model stays the single source of truth
and MCP remains one adapter over it rather than the only way to see the domain.
Adding a served object type is one declaration plus a reader; nothing here needs
a matching edit.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import pickle
import re
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import anyio
import anyio.from_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError
from mcp.shared.subscriptions import ResourceUpdated

from . import agent_access, change_feed, edition, object_model, profiles, storage

logger = logging.getLogger(__name__)

BOARD_URI = object_model.BOARD_URI
TASK_URI_TEMPLATE = object_model.TASK_URI_TEMPLATE
RUN_URI_TEMPLATE = object_model.RUN_URI_TEMPLATE
_EVIDENCE_PREFIX = "unified-agent://evidence/"

#: Poll interval for cross-process SQLite changes. Tests may monkeypatch.
POLL_INTERVAL_S = 1.0

#: Unset while the poller builds its first baseline. Resource reads wait on it
#: (worker threads, never the loop), so a read cannot predate a baseline that
#: is still being built and miss a commit that baseline absorbed (task #1958;
#: formerly the loop was simply blocked for the duration). ``None`` when no
#: poller is running.
#:
#: The guarantee has two documented limits (task #1962). A read that waits
#: BASELINE_WAIT_S without the event being set fails with an explicit
#: "not ready" ResourceError rather than reading unguarded; the client retries.
#: After three consecutive failed baselines the poller sets the event without a
#: baseline, so reads stop waiting on a store that keeps failing. Those reads
#: are best-effort: one that succeeds may predate the baseline the poller
#: eventually builds, and a commit landing in between is absorbed into that
#: baseline without a notification.
_baseline_ready: threading.Event | None = None
#: Longest a resource read waits for the baseline before failing as not ready.
BASELINE_WAIT_S = 30.0

_PROFILE_DENIED = "resource is not exposed by the active {profile} profile"

#: Registry handles for the two id-addressed legacy resources.
_TASK = object_model.by_kind("task")
_RUN = object_model.by_kind("run")


def task_uri(task_id: int) -> str:
    """Concrete task URI.

    Intentionally unvalidated, matching the pre-registry behaviour: the poller
    feeds this from raw rows, and a malformed id must surface as the reader's
    ``ResourceNotFoundError``, not as a failure to build the URI. Adapters that
    want validation use :meth:`object_model.ObjectType.render`.
    """
    return _TASK.uri_template.replace("{task_id}", str(int(task_id)))


def run_uri(run_id: str) -> str:
    """Concrete run URI. Unvalidated, for the same reason as :func:`task_uri`."""
    return _RUN.uri_template.replace("{run_id}", f"{run_id}")


def _deny(tool_name: str) -> None:
    if not profiles.tool_is_allowed(tool_name):
        raise ResourceError(_PROFILE_DENIED.format(profile=profiles.current()))


def _row_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def read_board() -> dict[str, Any]:
    """Compact board snapshot; same visibility as ``task_list``."""
    _deny("task_list")
    from .tools.tasks import task_list

    payload = task_list(status="all", fields="compact", limit=100)
    # A board read is a snapshot, not a sweep: say when it is partial so a
    # client never mistakes the first 100 rows for the whole queue (#1430).
    return {
        "uri": BOARD_URI,
        "tasks": payload["tasks"],
        "truncated": payload["truncated"],
        "next_cursor": payload["next_cursor"],
    }


def read_task(task_id: str) -> dict[str, Any]:
    """One task row, or not-found. Aligns with ``task_list`` profile permission."""
    _deny("task_list")
    try:
        tid = int(task_id)
    except (TypeError, ValueError) as exc:
        raise ResourceNotFoundError(f"unknown task: {task_id!r}") from exc
    if tid <= 0:
        raise ResourceNotFoundError(f"unknown task: {task_id!r}")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown task: {tid}")
    from .tools.tasks import _row

    return {"uri": task_uri(tid), "task": _row(row)}


def read_run(run_id: str) -> dict[str, Any]:
    """Dispatch run status; same profile permission as ``dispatch_report_self``."""
    _deny("dispatch_report_self")
    rid = (run_id or "").strip()
    if not rid:
        raise ResourceNotFoundError("unknown run")
    from . import run_records

    if rid.startswith(run_records.RUN_ID_PREFIX):
        # Self-pickup runs (task #1436) live on the event log, not dispatch_runs.
        sp = run_records.get_run(rid)
        if sp is None:
            raise ResourceNotFoundError(f"unknown self-pickup run: {rid}")
        return {
            "uri": run_uri(rid),
            "run": {
                **sp,
                "agent_label": sp["agent"],
                "created_at": sp["claimed_at"],
                "updated_at": sp["finished_at"] or sp["claimed_at"],
                "path": "self_pickup",
            },
        }
    from .dispatch import coordinator

    run = coordinator.get_run(rid)
    if run is None:
        raise ResourceNotFoundError(f"unknown dispatch run: {rid}")
    return {"uri": run_uri(rid), "run": run}


def _row_revision(row: Any) -> str:
    """Revision of one row: a digest of its column values.

    Pickled rather than ``repr``-ed: ``repr`` escapes every byte of a BLOB
    (memory embeddings), which made row hashing most of a poll (task #2028).
    The digest only has to be stable within one poller process.
    """
    return hashlib.sha256(pickle.dumps(tuple(row), protocol=5)).hexdigest()


def _table_snapshot(conn, table: str) -> list[tuple[str, str]] | None:
    """Return key/revision pairs, hashing rows so timestamp precision is irrelevant."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return None
    columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    primary_keys = sorted((int(row["pk"]), str(row["name"])) for row in columns if int(row["pk"]))
    if len(primary_keys) != 1:
        return None
    key_col = primary_keys[0][1]
    # Comments are append-only. A poll must not rehash every historical body;
    # id plus the immutable thread_revision is the subscription key.
    if table == "task_comments":
        rows = conn.execute("SELECT id, thread_revision FROM task_comments ORDER BY id")
        return [
            (
                str(row["id"]),
                hashlib.sha256(f"{row['id']}:{row['thread_revision']}".encode()).hexdigest(),
            )
            for row in rows
        ]
    if table == "agent_sessions":
        # Only this hub's tenant's sessions publish (task #1995), so another
        # tenant's session ids and activity never reach a subscriber; the
        # resource read itself is fenced in registry.get_session.
        rows = conn.execute(
            f'SELECT * FROM "{table}" WHERE org_id IS ? ORDER BY "{key_col}"',
            (edition.tenant_org_id(),),
        )
    else:
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{key_col}"')
    return [(str(row[key_col]), _row_revision(row)) for row in rows]


def _collection_revision(rows: list[tuple[str, str]]) -> str:
    """Hash ordered key/revision pairs without retaining a growing repr string."""
    digest = hashlib.sha256()
    for key, revision in rows:
        for value in (key, revision):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class _EventCursor:
    count: int
    last_rowid: int
    tail_hash: str | None
    # Claim ULIDs let incremental finish events update run URIs without
    # searching the historical JSON payloads again.
    run_claims: dict[str, str] | None = None
    # Claim/finish revisions of self-pickup runs before the heartbeat overlay,
    # so an unchanged run keeps its revision across incremental polls.
    run_revisions: dict[str, str] | None = None
    # Every non-evidence URI in the snapshot this cursor came with. The next
    # incremental snapshot drops exactly these from a copy of that snapshot
    # and keeps its evidence entries as they are (task #2028).
    non_evidence: frozenset[str] | None = None
    # Evidence URIs added since the prior snapshot, or None when the evidence
    # map was rebuilt in full. Lets a diff skip the unchanged ledger.
    appended_evidence: tuple[str, ...] | None = None


def _same_event_position(left: _EventCursor, right: _EventCursor) -> bool:
    return (
        left.count == right.count
        and left.last_rowid == right.last_rowid
        and left.tail_hash == right.tail_hash
    )


def _event_log_snapshot(
    conn, previous: dict[str, str] | None, cursor: _EventCursor | None
) -> tuple[list[tuple[str, str]], _EventCursor, bool]:
    """Reuse ledger revisions on unrelated writes; hash only newly appended rows.

    Returns ``(rows, cursor, incremental)``. When ``incremental`` is true the
    evidence entries of ``previous`` still hold and ``rows`` holds only the
    appended events (none when the ledger did not move); the caller keeps the
    ledger without visiting it, which at 600k rows is most of a poll (task
    #2028). Otherwise ``rows`` is the whole ledger.

    Pruning or a tail rewrite falls back to a full snapshot because pruning
    rewrites the hash chain. A same-size rewrite of a non-tail event is outside
    live invalidation: full evidence verification must detect that tampering.
    The cursor is local to one poller, never persisted.
    """
    count = int(conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0])
    tail = conn.execute(
        "SELECT ROWID, entry_hash FROM event_log ORDER BY ROWID DESC LIMIT 1"
    ).fetchone()
    current = _EventCursor(
        count, int(tail["ROWID"]) if tail else 0, tail["entry_hash"] if tail else None
    )
    if previous is not None and cursor is not None and _same_event_position(cursor, current):
        return [], current, True
    if (
        previous is not None
        and cursor is not None
        and current.count > cursor.count
        and current.last_rowid > cursor.last_rowid
    ):
        appended = conn.execute(
            "SELECT * FROM event_log WHERE ROWID > ? ORDER BY ROWID", (cursor.last_rowid,)
        ).fetchall()
        if (
            len(appended) == current.count - cursor.count
            and appended[0]["prev_hash"] == cursor.tail_hash
        ):
            return [(str(row["ulid"]), _row_revision(row)) for row in appended], current, True
    snap = _table_snapshot(conn, "event_log")
    if snap is None:
        # Schema drift (composite/absent single-column PK): silently mapping
        # None to [] would fabricate removal notifications for every evidence
        # URI and then go permanently silent — surface it loudly instead.
        logger.error(
            "event_log snapshot unavailable (unexpected schema); "
            "evidence invalidation is disabled for this poller"
        )
        snap = []
    return snap, current, False


def _self_pickup_run_revisions(
    conn,
    previous: dict[str, str] | None = None,
    prior_cursor: _EventCursor | None = None,
    current_cursor: _EventCursor | None = None,
) -> tuple[dict[str, str], _EventCursor | None]:
    """Revisions for self-pickup run URIs (task #1436): claim, finish, heartbeat.

    Self-pickup runs share the ``run`` kind but live on the event log, not
    ``dispatch_runs``, so the table snapshot above never sees them. A run's
    revision changes when its finish lands, and when a heartbeat updates its
    usage (PR #940): a heartbeat appends no event, and the process that wrote
    it may not share this poller's subscription bus.
    """
    from . import run_records

    base, cursor = _self_pickup_base_revisions(conn, previous, prior_cursor, current_cursor)
    out = dict(base)
    for run_id, heartbeat_rev in run_records.heartbeat_revisions(conn).items():
        try:
            uri = run_uri(run_id)
        except ValueError:
            continue
        if uri in out:
            out[uri] = hashlib.sha256(f"{out[uri]}:{heartbeat_rev}".encode()).hexdigest()
    return out, cursor


def _self_pickup_base_revisions(
    conn,
    previous: dict[str, str] | None,
    prior_cursor: _EventCursor | None,
    current_cursor: _EventCursor | None,
) -> tuple[dict[str, str], _EventCursor | None]:
    """Claim ULID plus finish ULID per self-pickup run URI, without heartbeats."""
    from . import run_records

    if (
        previous is not None
        and prior_cursor is not None
        and current_cursor is not None
        and prior_cursor.run_claims is not None
        and prior_cursor.run_revisions is not None
    ):
        cached = dict(prior_cursor.run_revisions)
        # Copy: the prior cursor belongs to state not yet replaced (#1958).
        claims = dict(prior_cursor.run_claims)
        if _same_event_position(prior_cursor, current_cursor):
            return cached, prior_cursor
        if (
            current_cursor.count > prior_cursor.count
            and current_cursor.last_rowid > prior_cursor.last_rowid
        ):
            appended = conn.execute(
                "SELECT ROWID, ulid, prev_hash, channel, payload FROM event_log "
                "WHERE ROWID > ? ORDER BY ROWID",
                (prior_cursor.last_rowid,),
            ).fetchall()
            if (
                len(appended) == current_cursor.count - prior_cursor.count
                and appended[0]["prev_hash"] == prior_cursor.tail_hash
            ):
                for row in appended:
                    if row["channel"] != run_records.CHANNEL:
                        continue
                    payload = json.loads(row["payload"])
                    if payload.get("kind") not in (
                        run_records.KIND_CLAIMED,
                        run_records.KIND_FINISHED,
                    ):
                        continue
                    run_id = payload.get("run_id")
                    if not run_id:
                        continue
                    uri = run_uri(run_id)
                    if payload.get("kind") == run_records.KIND_CLAIMED:
                        claims[run_id] = row["ulid"]
                    claim_ulid = claims.get(run_id)
                    if claim_ulid is None:
                        continue
                    finish_ulid = (
                        row["ulid"] if payload.get("kind") == run_records.KIND_FINISHED else ""
                    )
                    cached[uri] = hashlib.sha256(f"{claim_ulid}:{finish_ulid}".encode()).hexdigest()
                return cached, _EventCursor(
                    current_cursor.count,
                    current_cursor.last_rowid,
                    current_cursor.tail_hash,
                    claims,
                    cached,
                )

    rows = conn.execute(
        "SELECT json_extract(c.payload, '$.run_id') AS run_id, c.ulid AS claim_ulid, "
        "f.ulid AS finish_ulid FROM event_log c "
        "LEFT JOIN event_log f ON f.dedupe_key = "
        "'selfpickup:finished:' || json_extract(c.payload, '$.run_id') "
        "WHERE c.channel=? AND json_extract(c.payload, '$.kind')=?",
        (run_records.CHANNEL, run_records.KIND_CLAIMED),
    ).fetchall()
    out: dict[str, str] = {}
    claims: dict[str, str] = {}
    for row in rows:
        if not row["run_id"]:
            continue
        try:
            uri = run_uri(row["run_id"])
        except ValueError:
            continue
        out[uri] = hashlib.sha256(
            f"{row['claim_ulid']}:{row['finish_ulid'] or ''}".encode()
        ).hexdigest()
        claims[row["run_id"]] = row["claim_ulid"]
    next_cursor = None
    if current_cursor is not None:
        next_cursor = _EventCursor(
            current_cursor.count,
            current_cursor.last_rowid,
            current_cursor.tail_hash,
            claims,
            out,
        )
    return out, next_cursor


def _session_task_revisions(conn) -> dict[str, str]:
    """Capture task fields joined into each session resource representation.

    Fenced to this hub's tenant, like the session rows themselves (task #1995).
    """
    rows = conn.execute(
        "SELECT s.id, s.task_id, t.title, t.status "
        "FROM agent_sessions s LEFT JOIN tasks t ON t.id=s.task_id AND t.org_id IS s.org_id "
        "WHERE s.org_id IS ?",
        (edition.tenant_org_id(),),
    )
    return {
        str(row["id"]): hashlib.sha256(
            repr((row["task_id"], row["title"], row["status"])).encode("utf-8")
        ).hexdigest()
        for row in rows
    }


def snapshot_with_cursor(
    previous: dict[str, str] | None = None, event_cursor: _EventCursor | None = None
) -> tuple[dict[str, str], _EventCursor]:
    """Map every served instance URI to its backing row revision.

    With ``previous`` and its cursor, the evidence ledger is carried over from
    ``previous`` and only appended events are rendered (task #2028). The
    returned cursor records the non-evidence URIs and the appended evidence
    URIs, so :func:`_snapshot_changes` can diff without walking the ledger.
    """
    conn = storage.connect()
    try:
        tables = {item.table for item in object_model.served_types() if item.table}
        table_rows = {
            table: _table_snapshot(conn, table) for table in tables if table != "event_log"
        }
        event_rows, next_cursor, incremental = _event_log_snapshot(conn, previous, event_cursor)
        table_rows["event_log"] = event_rows
        session_task_revisions = _session_task_revisions(conn)
        self_pickup_revisions, next_cursor = _self_pickup_run_revisions(
            conn, previous, event_cursor, next_cursor
        )
    finally:
        conn.close()

    evidence: dict[str, str]
    if not incremental or previous is None:
        evidence = {}
    elif event_cursor is not None and event_cursor.non_evidence is not None:
        # A C-level copy, then drop the few non-evidence keys: the ledger
        # itself is never iterated in Python.
        evidence = dict(previous)
        for uri in event_cursor.non_evidence:
            evidence.pop(uri, None)
    else:
        evidence = {uri: rev for uri, rev in previous.items() if uri.startswith(_EVIDENCE_PREFIX)}
    others: dict[str, str] = {}
    appended: list[str] = []
    for object_type in object_model.served_types():
        if object_type.table is None:
            continue
        rows = table_rows.get(object_type.table)
        if rows is None:
            continue
        if object_type.collection:
            others[object_type.uri_template] = _collection_revision(rows)
            continue
        target = evidence if object_type.table == "event_log" else others
        for object_id, revision in rows:
            try:
                uri = object_type.render(object_id)
            except ValueError:
                continue
            if object_type.kind == "session":
                revision = hashlib.sha256(
                    f"{revision}:{session_task_revisions.get(object_id, '')}".encode()
                ).hexdigest()
            target[uri] = revision
            if incremental and target is evidence:
                appended.append(uri)
    others.update(self_pickup_revisions)
    others.update(_org_unit_snapshot())
    revisions = evidence
    revisions.update(others)
    return revisions, replace(
        next_cursor,
        non_evidence=frozenset(others),
        appended_evidence=tuple(appended) if incremental and previous is not None else None,
    )


def _snapshot_changes(
    previous: dict[str, str],
    current: dict[str, str],
    prior_cursor: _EventCursor | None,
    next_cursor: _EventCursor | None,
) -> list[str]:
    """``diff_uris`` for two consecutive poller snapshots, without walking the ledger.

    When ``current`` carried the evidence of ``previous`` forward, only the
    non-evidence URIs of either side and the appended events can differ. Any
    other pair falls back to the full diff.
    """
    if (
        prior_cursor is None
        or next_cursor is None
        or prior_cursor.non_evidence is None
        or next_cursor.non_evidence is None
        or next_cursor.appended_evidence is None
    ):
        return diff_uris(previous, current)
    candidates = prior_cursor.non_evidence | next_cursor.non_evidence
    candidates = candidates.union(next_cursor.appended_evidence)
    return sorted(uri for uri in candidates if previous.get(uri) != current.get(uri))


def _org_unit_snapshot() -> dict[str, str]:
    """Revision map for the checked-in organization charter, outside SQLite."""
    from . import org_registry

    object_type = object_model.by_kind("org_unit")
    return {
        object_type.render(office["id"]): hashlib.sha256(
            json.dumps(office, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for office in org_registry.load_registry()
    }


def _replace_org_revisions(previous: dict[str, str], current_org: dict[str, str]) -> None:
    """Keep the shared baseline current after a charter-only notification."""
    for uri in list(previous):
        if uri.startswith("unified-agent://org-units/"):
            del previous[uri]
    previous.update(current_org)


def snapshot() -> dict[str, str]:
    """One complete snapshot for callers outside the long-running poller."""
    return snapshot_with_cursor()[0]


def _agent_liveness_snapshot(now: datetime | None = None) -> dict[str, str]:
    """Capture time-derived agent statuses without rescanning all resource rows."""
    from .registry import derive_status

    reference = now or datetime.now(UTC)
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT label, last_seen FROM agents")
        revisions = {}
        agent_type = object_model.by_kind("agent")
        for row in rows:
            try:
                uri = agent_type.render(row["label"])
            except ValueError:
                # A legacy or externally written invalid label must not kill
                # the shared poller for otherwise valid resources.
                continue
            revisions[uri] = derive_status(row["last_seen"], now=reference)
        return revisions
    finally:
        conn.close()


def _session_clock_snapshot(now: datetime | None = None) -> dict[str, str]:
    """Bucket live clock fields so idle sessions notify at most once per minute."""
    from .registry import _parse_ts

    reference = now or datetime.now(UTC)
    conn = storage.connect()
    try:
        # Fenced to this hub's tenant, like the session snapshot (task #1995).
        rows = conn.execute(
            "SELECT id, started_at, last_heartbeat_at FROM agent_sessions "
            "WHERE ended_at IS NULL AND org_id IS ?",
            (edition.tenant_org_id(),),
        )
        revisions = {}
        session_type = object_model.by_kind("session")
        for row in rows:
            try:
                uri = session_type.render(row["id"])
            except ValueError:
                continue
            started = _parse_ts(row["started_at"])
            heartbeat = _parse_ts(row["last_heartbeat_at"])
            duration = int((reference - started).total_seconds() // 60) if started else None
            heartbeat_age = (
                int((reference - heartbeat).total_seconds() // 60) if heartbeat else None
            )
            revisions[uri] = hashlib.sha256(
                repr((duration, heartbeat_age)).encode("utf-8")
            ).hexdigest()
        return revisions
    finally:
        conn.close()


def _database_version(conn) -> int:
    """SQLite changes this connection's version only after another commits."""
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


def diff_uris(
    previous: dict[str, str] | None,
    current: dict[str, str],
) -> list[str]:
    """URIs that changed between snapshots; the first snapshot is the baseline."""
    if previous is None:
        return []
    return sorted(
        uri for uri in previous.keys() | current.keys() if previous.get(uri) != current.get(uri)
    )


def update_subscription_telemetry(server: MCPServer) -> None:
    """Update telemetry subscription counts from active bus listeners.

    Counter keys come from each object type's declared ``telemetry_key``, so the
    long-standing board/tasks/runs series keep reporting under the names
    dashboards already know while a newly served object type is measured without
    a new branch here. A listener on an unrecognized URI still counts toward the
    total.
    """
    try:
        from . import mcp_runtime_telemetry

        bus = getattr(server, "_subscriptions", None)
        listeners = getattr(bus, "_listeners", {}) if bus else {}
        total = 0
        counts: dict[str, int] = {}
        for uri, subs in listeners.items():
            count = len(subs) if hasattr(subs, "__len__") else 1
            total += count
            object_type = object_model.match_uri(str(uri))
            if object_type is None:
                continue
            key = object_type.telemetry_key or object_type.kind
            counts[key] = counts.get(key, 0) + count
        mcp_runtime_telemetry.record_subscription_counts(
            total=total,
            # "total" is an explicit parameter of record_subscription_counts;
            # a registry kind literally named "total" would collide into a
            # TypeError (silently swallowed) — rename it defensively.
            **{("kind_total" if key == "total" else key): count for key, count in counts.items()},
        )
    except Exception:
        pass


async def publish_uris(server: MCPServer, uris: list[str]) -> None:
    update_subscription_telemetry(server)
    bus = getattr(server, "_subscriptions", None)
    if bus is None:
        return
    seen: set[str] = set()
    for uri in uris:
        if uri in seen:
            continue
        seen.add(uri)
        await bus.publish(ResourceUpdated(uri=uri))


def notify_task_change(task_id: int | None = None) -> None:
    """Best-effort publish after a same-process task mutation."""
    uris = [BOARD_URI]
    if task_id is not None:
        uris.append(task_uri(task_id))
    notify_uris(uris)


def notify_run_change(run_id: str | None) -> None:
    if not run_id:
        return
    notify_uris([run_uri(run_id)])


def _on_change(kind: str, ids: tuple) -> None:
    """Publish domain change notices (see ``change_feed``) to subscribed clients."""
    if kind == "task":
        notify_task_change(ids[0] if ids else None)
    elif kind == "run":
        notify_uris([run_uri(r) for r in ids])


change_feed.subscribe(_on_change)


def notify_uris(uris: list[str]) -> None:
    """Publish from a sync tool thread; no-op if no MCP event loop is bound."""
    if not uris:
        return
    try:
        from .server import mcp
    except Exception:
        return

    async def _publish() -> None:
        await publish_uris(mcp, uris)

    try:
        anyio.from_thread.run(_publish)
    except RuntimeError:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(_publish())
                return
        except RuntimeError:
            pass
        return
    except Exception:
        logger.debug("live-state notify failed", exc_info=True)


@dataclass
class _PollState:
    """Baseline the poller diffs against; touched by one poll at a time."""

    conn: Any = None
    version: int | None = None
    previous: dict[str, str] | None = None
    event_cursor: _EventCursor | None = None
    liveness: dict[str, str] | None = None
    session_clock: dict[str, str] | None = None
    org: dict[str, str] | None = None


def _poll_once(state: _PollState) -> tuple[list[str], _PollState]:
    """One poll: the URIs that changed and the state to adopt once they publish.

    ``state`` itself is left as the last published baseline, so a failed or
    partial publish is diffed and published again by the next poll.

    Runs in a worker thread (task #1958). The snapshot reads and hashes every
    served row (about 1 s per changed poll on the Mac hub at 600k event rows,
    4.6 s for a full baseline). Run on the event loop, it stalled every HTTP
    request behind it. The connection only carries ``PRAGMA data_version``;
    polls never overlap, so it may move between worker threads.
    """
    if state.conn is None:
        state.conn = storage.connect(check_same_thread=False)
    version = _database_version(state.conn)
    if state.previous is None:
        previous, event_cursor = snapshot_with_cursor()
        return [], replace(
            state,
            version=version,
            previous=previous,
            event_cursor=event_cursor,
            liveness=_agent_liveness_snapshot(),
            session_clock=_session_clock_snapshot(),
            org=_org_unit_snapshot(),
        )
    current = state.previous
    next_cursor = state.event_cursor
    uris: list[str] = []
    if version != state.version:
        current, next_cursor = snapshot_with_cursor(state.previous, state.event_cursor)
        uris.extend(_snapshot_changes(state.previous, current, state.event_cursor, next_cursor))
    liveness = _agent_liveness_snapshot()
    uris.extend(diff_uris(state.liveness, liveness))
    session_clock = _session_clock_snapshot()
    uris.extend(diff_uris(state.session_clock, session_clock))
    org = _org_unit_snapshot()
    uris.extend(diff_uris(state.org, org))
    if org != state.org:
        # Copy before editing: ``current`` may still be the published baseline.
        current = dict(current)
        _replace_org_revisions(current, org)
    return uris, replace(
        state,
        version=version,
        previous=current,
        event_cursor=next_cursor,
        liveness=liveness,
        session_clock=session_clock,
        org=org,
    )


def _has_listeners(server: MCPServer) -> bool:
    """Whether a published event could reach anyone.

    A bus that does not expose ``_listeners`` (a custom or remote bus) counts
    as listened to, so the poller fails open to polling.
    """
    bus = getattr(server, "_subscriptions", None)
    if bus is None:
        return False
    listeners = getattr(bus, "_listeners", None)
    return listeners is None or bool(listeners)


async def poll_loop(server: MCPServer) -> None:
    """Publish committed changes, reconnecting after a failed SQLite poll.

    Baseline initialization runs immediately after opening the connection
    (task #1315 / Devin F3 on PR #695). Sleep happens *after* a successful
    baseline or poll (and after failed attempts), so there is no blind
    startup window where subscribers can read pre-commit content without
    later invalidation.

    While nobody listens, polls are skipped and the last baseline is kept
    (task #1958). The first poll after a listener arrives diffs against that
    older baseline, so every commit made while idle is still published: the
    cost is a few redundant notifications, never a missed one.
    """
    global _baseline_ready
    ready = threading.Event()
    _baseline_ready = ready
    state = _PollState()
    consecutive_failures = 0
    try:
        while True:
            if state.previous is None or _has_listeners(server):
                try:
                    uris, candidate = await anyio.to_thread.run_sync(_poll_once, state)
                    if uris:
                        await publish_uris(server, uris)
                    state = candidate
                    ready.set()
                    consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        # A store that keeps failing must not make every
                        # resource read wait BASELINE_WAIT_S and then fail as
                        # not ready. Reads after this point are best-effort:
                        # they are not ordered after any baseline (see
                        # _baseline_ready).
                        ready.set()
                    if consecutive_failures == 3 or consecutive_failures % 60 == 0:
                        logger.warning(
                            "live-state poll failed %d consecutive times; reconnecting",
                            consecutive_failures,
                            exc_info=True,
                        )
                    else:
                        logger.debug("live-state poll failed; reconnecting", exc_info=True)
                    if state.conn is not None:
                        try:
                            state.conn.close()
                        except Exception:
                            pass
                        state.conn = None
                    # PRAGMA data_version is connection-local. Force a full diff
                    # against the last successfully published snapshot on reconnect.
                    state.version = None
            await anyio.sleep(POLL_INTERVAL_S)
    finally:
        ready.set()
        if _baseline_ready is ready:
            _baseline_ready = None
        if state.conn is not None:
            try:
                state.conn.close()
            except Exception:
                pass


def _handler_for(object_type: object_model.ObjectType):
    """Build the resource handler the MCP framework expects for one object type.

    ``ResourceTemplate.create_resource`` calls ``fn(**params)`` with the values it
    extracted from the URI, while ``func_metadata`` derives the *published*
    parameter schema from ``inspect.signature(fn)``. A bare ``**params`` signature
    therefore works but would advertise a resource template with no parameters,
    so the declared parameter name is installed explicitly via ``__signature__``
    rather than generated with ``exec`` — which the security audit forbids, and
    rightly so for a server hosting untrusted content.
    """
    reader = object_type.reader()

    def handler(**params):
        """Dispatch one read; the template parameter arrives as a keyword."""
        agent_access.enforce(f"resource:{object_type.kind}", read_only=True)
        ready = _baseline_ready
        if ready is not None and not ready.wait(BASELINE_WAIT_S):
            # Reading now could predate the baseline still being built, and a
            # commit after this read would then be absorbed into it unannounced
            # (task #1962). Fail explicitly; the client retries.
            raise ResourceError(
                f"live state is not ready: the change baseline is still being built "
                f"after {BASELINE_WAIT_S:g}s; retry the read"
            )
        if object_type.id_param is None:
            return reader()
        return reader(**params)

    # A static resource must advertise *no* parameters and a template must
    # advertise exactly its declared one, so the signature is set explicitly in
    # both cases rather than left as the bare **params.
    #
    # ``__annotations__`` must carry the same parameter: ``func_metadata`` reads
    # the signature, but pydantic's ``validate_call`` (which the framework wraps
    # every handler in) resolves each parameter's type hint out of the function's
    # annotations. Signature-only overrides raise ``KeyError: 'task_id'`` there.
    parameters = (
        []
        if object_type.id_param is None
        else [
            inspect.Parameter(
                object_type.id_param,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
            )
        ]
    )
    handler.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    handler.__annotations__ = {} if object_type.id_param is None else {object_type.id_param: str}
    handler.__name__ = object_type.name
    handler.__doc__ = object_type.description
    return handler


def register_resources(server: MCPServer) -> None:
    """Register every served object type as an MCP resource.

    A loop over :mod:`unified_agent.object_model`: the URIs, names, titles,
    descriptions, and mime types are the declaration's. Adding a served resource
    is one declaration plus a reader — there is no second place to edit.
    """
    for object_type in object_model.served_types():
        server.resource(
            object_type.uri_template,
            name=object_type.name,
            title=object_type.title,
            description=object_type.description,
            mime_type=object_model.DEFAULT_MIME_TYPE,
        )(_handler_for(object_type))


def resource_visible(uri_or_template: str) -> bool:
    """Whether this URI/template should appear in list results for the session.

    Derived from the declared ``profile_tool``, so an object can never be listed
    to a session whose profile denies the equivalent tool. An unrecognized URI
    keeps the historical default: visible.
    """
    object_type = object_model.match_uri(uri_or_template)
    if object_type is None:
        return True
    return profiles.tool_is_allowed(object_type.profile_tool)
