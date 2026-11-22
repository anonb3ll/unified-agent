"""Cloud mirror PoC (task #1285): read-only receiver for the metadata-only mirror.

A minimal Starlette ASGI app (Starlette is already a transitive dependency via
``mcp[cli]``; see other hand-rolled ASGI surfaces in this repo — ``server.py``,
``pilot_app.py``, ``oauth.py``, ``webui/routes.py`` — for the same pattern).
Meant to run as a small, separately-deployed service (a dedicated GCE VM with
persistent standard disk and IAP SSH forwarding is the current PoC target —
see ``docs/cloud-mirror.md``); it is **not** mounted into the private hub's
own MCP server.

Design invariants:

- **Every route requires a valid bearer token**, including the read-only
  ones. There is no unauthenticated read path — this is a private mirror of
  operational metadata, not a public status page.
- **The hub is authoritative; this receiver has no write-back path.** It only
  ever accepts ``POST /ingest`` from the shipper and answers reads. Nothing
  here can mutate hub state, and nothing here is on the hub's critical path —
  the hub works identically whether this service is up, degraded, or gone.
- **Dedupe by ``event_id`` via ``INSERT OR IGNORE``**, so a shipper retry
  (e.g. after a sink timeout whose response was lost, even though the POST
  itself landed) never double-counts.
- **Unknown fields are rejected, not silently dropped**, so a receiver
  running behind a newer/older shipper fails loudly on a schema mismatch
  rather than quietly losing data.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SCHEMA_VERSION = 1
MAX_FAILED_AUTH_PER_WINDOW = 10
AUTH_FAILURE_WINDOW_SECONDS = 60.0
MAX_TRACKED_AUTH_CLIENTS = 1024

_CHANNEL_KINDS = {
    "agent.task.updated": "task",
    "agent.approval.requested": "approval",
    "agent.approval.elicitation": "approval",
    "agent.cycle.updated": "cycle",
    "agent.health.alert": "health",
    "agent.project.built": "build",
}
_TASK_STATUSES = frozenset({"open", "doing", "waiting_review", "blocked", "done", "cancelled"})
_APPROVAL_STATUSES = frozenset({"pending", "approved", "denied", "consumed", "revoked", "expired"})
_ELICITATION_STATUSES = frozenset(
    {
        "requested",
        "challenge_issued",
        "challenge_rejected",
        "challenge_consumed",
        "approved",
        "denied",
        "cancel",
        "decline",
    }
)
_CYCLE_STATUSES = frozenset(
    {
        "plan",
        "delegated",
        "executing",
        "validating",
        "reviewing",
        "rework",
        "pr",
        "blocked",
        "completed",
        "failed",
        "killed",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_AGENT = re.compile(r"^[A-Za-z0-9_.: -]{1,128}$")
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

_REQUIRED_RECORD_KEYS = frozenset({"schema_version", "event_id", "channel", "timestamp"})
_ALLOWED_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "channel",
        "kind",
        "agent",
        "task_id",
        "status",
        "timestamp",
    }
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mirror_events (
    event_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    kind TEXT,
    agent TEXT,
    task_id TEXT,
    status TEXT,
    timestamp TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_mirror_events_timestamp ON mirror_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_mirror_events_agent ON mirror_events(agent);
CREATE INDEX IF NOT EXISTS idx_mirror_events_channel ON mirror_events(channel);
"""

# /reviews/pending is a STATE query, not an event query: it reports the
# LATEST status per (kind, task_id), never every historical "requested" event
# regardless of what happened after it. See reviews_pending()'s SQL below.
# task.status enum: storage.py's `tasks` CHECK constraint (waiting_review is
# the review-requested state). approval status enum: the `approvals` table's
# CHECK constraint ("pending" = outstanding) plus every elicitation action
# that leaves the authoritative approval row pending.
_PENDING_TASK_STATUSES = frozenset({"waiting_review"})
_PENDING_APPROVAL_STATUSES = frozenset(
    {
        "pending",
        "requested",
        "challenge_issued",
        "challenge_rejected",
        "challenge_consumed",
        "cancel",
        "decline",
    }
)


def _ensure_private_directories(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            raise FileNotFoundError(f"no existing parent for {path}")
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(current)
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise


def _resolve_database_path(configured: str | None, data_home: Path) -> Path:
    """Keep the receiver database inside its private UA_HOME data root."""
    root = data_home.expanduser().resolve()
    candidate = Path(configured).expanduser() if configured else Path("cloud-mirror-receiver.db")
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("receiver database path must be below UA_HOME")
    return resolved


def _connect(db_path: Path) -> sqlite3.Connection:
    _ensure_private_directories(db_path.parent)
    fd = os.open(db_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _initialize_database(db_path: Path) -> None:
    """Create the receiver schema once before serving requests."""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": row["schema_version"],
        "event_id": row["event_id"],
        "channel": row["channel"],
        "kind": row["kind"],
        "agent": row["agent"],
        "task_id": row["task_id"],
        "status": row["status"],
        "timestamp": row["timestamp"],
        "received_at": row["received_at"],
    }


def create_app(
    *,
    db_path: Path,
    ingest_token_env_var: str = "UA_CLOUD_MIRROR_INGEST_TOKEN",
    read_token_env_var: str = "UA_CLOUD_MIRROR_RECEIVER_TOKEN",
    max_body_bytes: int = 1_000_000,
) -> Starlette:
    """Build the receiver ASGI app.

    ``token_env_var`` names (never carries) the environment variable holding
    the bearer token this receiver requires. Read at request time, not at
    app-build time, so rotating the token doesn't need a process restart.
    """
    _initialize_database(db_path)

    def response(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    failed_auth: dict[str, list[float]] = {}
    failed_auth_lock = threading.Lock()

    def authorized(request: Request, token_env_var: str) -> bool:
        expected = os.environ.get(token_env_var, "")
        if not expected:
            # No token configured: refuse everything rather than silently
            # running open. An operator must opt in explicitly.
            return False
        other_token_env_var = (
            read_token_env_var if token_env_var == ingest_token_env_var else ingest_token_env_var
        )
        other = os.environ.get(other_token_env_var, "")
        if not other or hmac.compare_digest(expected, other):
            # The write-only shipper credential and operator credential are
            # separate parts of the trust boundary; duplicate values fail closed.
            return False
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        got = header[7:]
        return hmac.compare_digest(got, expected)

    def require_auth(request: Request, *, ingest: bool = False) -> JSONResponse | None:
        client_key = request.client.host if request.client is not None else "unknown"
        now = time.monotonic()
        token_env_var = ingest_token_env_var if ingest else read_token_env_var
        is_authorized = authorized(request, token_env_var)
        with failed_auth_lock:
            attempts = [
                attempt
                for attempt in failed_auth.get(client_key, [])
                if now - attempt < AUTH_FAILURE_WINDOW_SECONDS
            ]
            if is_authorized:
                failed_auth.pop(client_key, None)
                return None
            if len(attempts) >= MAX_FAILED_AUTH_PER_WINDOW:
                failed_auth[client_key] = attempts
                return response({"error": "too many authentication failures"}, 429)
            attempts.append(now)
            failed_auth[client_key] = attempts
            if len(failed_auth) > MAX_TRACKED_AUTH_CLIENTS:
                failed_auth.pop(next(iter(failed_auth)))
            return response({"error": "unauthorized"}, 401)

    async def bounded_body(request: Request) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_body_bytes:
                raise ValueError("request body too large")
        return bytes(body)

    def _validate_record(record: Any) -> str | None:
        """Return an error string, or None if valid."""
        if not isinstance(record, dict):
            return "record must be an object"
        extra = set(record) - _ALLOWED_RECORD_KEYS
        if extra:
            return f"unknown fields: {sorted(extra)}"
        missing = _REQUIRED_RECORD_KEYS - set(record)
        if missing:
            return f"missing required fields: {sorted(missing)}"
        if record.get("schema_version") != SCHEMA_VERSION:
            return f"unsupported schema_version: {record.get('schema_version')!r}"
        if type(record.get("schema_version")) is not int:
            return "schema_version must be an integer"
        if not isinstance(record.get("event_id"), str) or not _ULID.fullmatch(record["event_id"]):
            return "event_id must be a canonical 26-character ULID"
        channel = record.get("channel")
        if not isinstance(channel, str) or channel not in _CHANNEL_KINDS:
            return "channel is not allowlisted"
        if not isinstance(record.get("timestamp"), str) or not record["timestamp"]:
            return "timestamp must be a non-empty string"
        if len(record["timestamp"]) > 64:
            return "timestamp must be a bounded timestamp token"
        try:
            parsed_timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            return "timestamp must be an ISO-8601 timestamp"
        if parsed_timestamp.utcoffset() is None:
            return "timestamp must include a timezone offset"
        record["timestamp"] = parsed_timestamp.astimezone(UTC).isoformat()
        for key in ("kind", "agent", "task_id", "status"):
            if key in record and record[key] is not None and not isinstance(record[key], str):
                return f"{key} must be a string or null"
        expected_kind = _CHANNEL_KINDS[channel]
        if record.get("kind") != expected_kind:
            return f"kind must be {expected_kind!r} for channel {channel!r}"
        for key in ("kind", "agent", "task_id", "status"):
            value = record.get(key)
            pattern = _SAFE_AGENT if key == "agent" else _SAFE_ID
            if value is not None and (len(value) > 128 or not pattern.fullmatch(value)):
                return f"{key} must be a bounded metadata token"
        status = record.get("status")
        allowed_statuses = {
            "task": _TASK_STATUSES,
            "approval": _APPROVAL_STATUSES | _ELICITATION_STATUSES,
            "cycle": _CYCLE_STATUSES,
            "health": frozenset(),
            "build": frozenset({"built"}),
        }[expected_kind]
        if channel == "agent.approval.requested":
            allowed_statuses = _APPROVAL_STATUSES
        elif channel == "agent.approval.elicitation":
            allowed_statuses = _APPROVAL_STATUSES | _ELICITATION_STATUSES
        if status is not None and status not in allowed_statuses:
            return f"status is not allowed for kind {expected_kind!r}"
        task_id = record.get("task_id")
        if expected_kind in {"task", "approval"} and task_id is not None and not task_id.isdigit():
            return f"task_id must be numeric for kind {expected_kind!r}"
        return None

    def _persist_records(records: list[dict[str, Any]]) -> dict[str, int]:
        conn = _connect(db_path)
        try:
            ingested = 0
            duplicates = 0
            for record in records:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO mirror_events "
                    "(event_id, channel, kind, agent, task_id, status, timestamp, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["event_id"],
                        record["channel"],
                        record.get("kind"),
                        record.get("agent"),
                        record.get("task_id"),
                        record.get("status"),
                        record["timestamp"],
                        record["schema_version"],
                    ),
                )
                if cur.rowcount:
                    ingested += 1
                else:
                    duplicates += 1
            conn.commit()
        finally:
            conn.close()
        return {"ingested": ingested, "duplicates": duplicates}

    async def ingest(request: Request) -> JSONResponse:
        denied = require_auth(request, ingest=True)
        if denied is not None:
            return denied
        try:
            raw = await bounded_body(request)
        except ValueError as exc:
            return response({"error": str(exc)}, 413)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return response({"error": "invalid JSON body"}, 400)
        if not isinstance(body, dict) or not isinstance(body.get("records"), list):
            return response({"error": 'body must be {"records": [...]}'}, 400)
        records = body["records"]
        if len(records) > 1000:
            return response({"error": "too many records in one batch (max 1000)"}, 413)

        for i, record in enumerate(records):
            err = _validate_record(record)
            if err is not None:
                return response({"error": f"record {i}: {err}"}, 422)

        result = await run_in_threadpool(_persist_records, records)
        return response(result)

    def timeline(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            limit = min(max(int(request.query_params.get("limit", "100")), 1), 1000)
        except ValueError:
            return response({"error": "limit must be an integer"}, 400)
        after = request.query_params.get("after")  # event_id cursor, exclusive
        channel = request.query_params.get("channel")

        where = []
        params: list[Any] = []
        if after:
            where.append("event_id > ?")
            params.append(after)
        if channel:
            where.append("channel = ?")
            params.append(channel)
        sql = "SELECT * FROM mirror_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_id ASC LIMIT ?"
        params.append(limit)

        conn = _connect(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        records = [_row_to_dict(r) for r in rows]
        next_cursor = records[-1]["event_id"] if len(records) == limit else None
        return response({"records": records, "next_cursor": next_cursor})

    def agents_health(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT agent, MAX(timestamp) AS last_seen, "
                "(SELECT channel FROM mirror_events m2 WHERE m2.agent = m1.agent "
                " ORDER BY m2.timestamp DESC, m2.event_id DESC LIMIT 1) AS last_channel "
                "FROM mirror_events m1 WHERE agent IS NOT NULL GROUP BY agent "
                "ORDER BY last_seen DESC"
            ).fetchall()
        finally:
            conn.close()
        agents = [
            {"agent": r["agent"], "last_seen": r["last_seen"], "last_channel": r["last_channel"]}
            for r in rows
        ]
        return response({"agents": agents})

    def reviews_pending(request: Request) -> JSONResponse:
        """State-level (not event-level) pending review/approval query.

        Groups by (kind, task_id) and keeps only the LATEST record per group
        -- event_id (a ULID) sorts lexicographically by generation time, so
        MAX(event_id) within a group is the most recent status for that
        task/approval. Without this, a task that transitioned
        waiting_review -> done, or an approval that went requested ->
        approved, would show up as pending forever just because it once
        passed through a pending-shaped status. A row with a null kind or
        task_id can't be grouped to a single entity and is excluded.
        """
        denied = require_auth(request)
        if denied is not None:
            return denied
        task_placeholders = ",".join("?" for _ in _PENDING_TASK_STATUSES)
        approval_placeholders = ",".join("?" for _ in _PENDING_APPROVAL_STATUSES)
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT m.* FROM mirror_events m "
                "JOIN ("
                "  SELECT kind, task_id, MAX(event_id) AS latest_event_id"
                "  FROM mirror_events"
                "  WHERE kind IS NOT NULL AND task_id IS NOT NULL"
                "  GROUP BY kind, task_id"
                ") latest "
                "  ON m.kind = latest.kind"
                "  AND m.task_id = latest.task_id"
                "  AND m.event_id = latest.latest_event_id "
                f"WHERE (m.kind = 'task' AND m.status IN ({task_placeholders})) "
                f"   OR (m.kind = 'approval' AND m.status IN ({approval_placeholders})) "
                "ORDER BY m.timestamp DESC LIMIT 500",
                (*_PENDING_TASK_STATUSES, *_PENDING_APPROVAL_STATUSES),
            ).fetchall()
        finally:
            conn.close()
        return response({"pending": [_row_to_dict(r) for r in rows]})

    def export_all(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM mirror_events ORDER BY event_id ASC").fetchall()
        finally:
            conn.close()
        return response(
            {"schema_version": SCHEMA_VERSION, "records": [_row_to_dict(r) for r in rows]}
        )

    def delete_all(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        conn = _connect(db_path)
        try:
            cur = conn.execute("DELETE FROM mirror_events")
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
        return response({"deleted": deleted})

    routes = [
        Route("/ingest", ingest, methods=["POST"]),
        Route("/timeline", timeline, methods=["GET"]),
        Route("/agents/health", agents_health, methods=["GET"]),
        Route("/reviews/pending", reviews_pending, methods=["GET"]),
        Route("/export", export_all, methods=["GET"]),
        Route("/data", delete_all, methods=["DELETE"]),
    ]
    return Starlette(routes=routes)


def main() -> int:  # pragma: no cover - standalone service entry point
    """Run the standalone receiver service with uvicorn.

    Container platforms use the default interface bind; a private GCE
    deployment sets ``UA_CLOUD_MIRROR_RECEIVER_HOST=127.0.0.1``.
    """
    import uvicorn

    from . import config

    db_path = _resolve_database_path(
        os.environ.get("UA_CLOUD_MIRROR_RECEIVER_DB"), config.data_home()
    )
    app = create_app(db_path=db_path)
    uvicorn.run(
        app,
        host=os.environ.get("UA_CLOUD_MIRROR_RECEIVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
