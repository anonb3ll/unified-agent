"""Cloud mirror PoC (task #1285): metadata-only event shipper.

The hub's ``event_log`` (see ``events.py``) is the append-only, ULID-ordered
record of everything that happens locally — task lifecycle, approvals,
autonomous-cycle transitions, health signals, builds. This module ships a
*strict, allowlisted projection* of a subset of that log to an off-box sink
(local file for tests, or an HTTP receiver — see ``cloud_mirror_receiver.py``)
so a remote, read-only timeline can exist without the hub's local authority
changing at all.

Three hard rules, enforced structurally rather than by convention:

1. **Channel allowlist.** Only ``ALLOWLISTED_CHANNELS`` is ever read past the
   query stage. Everything else — most importantly ``agent.message.*``
   (message bodies) and ``agent.journal.appended`` (journal text) — never
   even reaches the projection step.
2. **Field allowlist, no free text, ever.** ``_PROJECTORS`` below maps each
   allowlisted channel to a function that reads specific, structured payload
   keys only. Critically, several of the allowlisted channels carry a
   ``summary`` field (``storage.record_agent_event``'s activity-index mirror,
   e.g. ``f"created ({priority}): {title}"`` in ``tools/tasks.py``) that
   embeds free-form task/approval text. That field is never read here, by
   any projector, under any channel — not filtered after the fact, just
   never touched. The output record type (``MirrorRecord`` /
   ``_ALLOWED_RECORD_KEYS``) has no field wide enough to hold it, so an
   accidental future addition of a new key would need an explicit, visible
   schema change, not a one-line payload passthrough.
3. **Sink failure is never the hub's problem.** ``run_once`` never raises
   into a caller. A sink error is logged and the cursor stays exactly where
   it was, so the same batch is retried whole on the next run — no partial
   ships, no silent gaps.

Off by default: ``run_once`` is a no-op unless ``UA_CLOUD_MIRROR_ENABLED=1``.
See ``docs/cloud-mirror.md`` for the acceptance mapping and deployment notes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import config, events, storage

logger = logging.getLogger("unified_agent.cloud_mirror")

# approvals.status's own CHECK constraint (storage.py CREATE TABLE
# approvals). No importable constant exists for this enum at the time of
# writing -- kept in sync with that CHECK constraint by hand.
_APPROVAL_STATUS_ENUM = frozenset(
    {"pending", "approved", "denied", "consumed", "revoked", "expired"}
)
_ELICITATION_ACTIONS = frozenset(
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


def _task_status_enum() -> frozenset[str]:
    """tasks.status's own CHECK constraint, via tools.tasks.STATUSES (exact match, no drift).

    Imported lazily rather than at module level so a module that may never
    enrich a single task-kind record doesn't pay tools.tasks's heavier
    import cost every time.
    """
    from .tools.tasks import STATUSES

    return frozenset(STATUSES)


SCHEMA_VERSION = 1

# The only channels this module will ever read past the query stage. Adding a
# channel here is a deliberate, reviewable act — see the module docstring.
# Sourced from events.py's own channel constants plus two literal channel
# strings used at their call sites (agent.cycle.updated in tools/cycles.py,
# agent.approval.elicitation in elicitation.py) that have no importable
# constant of their own yet.
CHANNEL_TASK_UPDATED = events.CHANNEL_TASK_UPDATED  # "agent.task.updated"
CHANNEL_APPROVAL_REQUESTED = events.CHANNEL_APPROVAL_REQUESTED  # "agent.approval.requested"
CHANNEL_APPROVAL_ELICITATION = "agent.approval.elicitation"
CHANNEL_CYCLE_UPDATED = "agent.cycle.updated"
CHANNEL_HEALTH_ALERT = events.CHANNEL_HEALTH_ALERT  # "agent.health.alert"
CHANNEL_PROJECT_BUILT = events.CHANNEL_PROJECT_BUILT  # "agent.project.built"

ALLOWLISTED_CHANNELS: tuple[str, ...] = (
    CHANNEL_TASK_UPDATED,
    CHANNEL_APPROVAL_REQUESTED,
    CHANNEL_APPROVAL_ELICITATION,
    CHANNEL_CYCLE_UPDATED,
    CHANNEL_HEALTH_ALERT,
    CHANNEL_PROJECT_BUILT,
)

# Deliberately EXCLUDED (documented, not just omitted):
#   agent.message.*        -- message bodies (messaging/schema.py CHANNEL_MESSAGE_PREFIX)
#   agent.journal.appended -- journal text (storage.record_agent_event kind="journal")
#   agent.supervisor.wake  -- carries free-form completion/block summaries
#   agent.fleet.control    -- "detail" is an arbitrary free-form dict
#   agent.cycle.updated's own "reason"/"detail" sub-fields -- see _cycle_fields

# The complete set of top-level keys a projected record may ever carry. A
# projector returning anything outside this set is a bug, and tests assert
# against it directly rather than trusting the projector functions alone.
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


def _mirrored_kind_fields(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Fields for the ``storage.record_agent_event`` mirror shape.

    ``mirror_agent_event`` (events.py) always emits
    ``{"kind": ..., "ref_id": ..., "summary": ...}`` for task/approval/health
    activity. ``ref_id`` is a bare id string (task id, approval id, or a ulid
    for fleet-style events) and is safe to carry as ``task_id``. ``summary``
    is free text embedding the task/approval title and is never read.

    ``status`` is left ``None`` here — never parsed out of ``summary`` prose
    — for every kind. For task and approval-requested records specifically,
    ``run_once``'s ``_enrich_structured_statuses`` backfills it afterwards
    with a batched, read-only lookup of the CURRENT ``tasks.status`` /
    ``approvals.status`` column, i.e. status *as of ship time*, not as of
    this particular historical event. Approval elicitation records carry a
    fixed, code-defined action status and are excluded from that lookup.
    ``kind == "health"`` has no structured status source at all and stays
    ``None``.
    """
    return {"kind": kind, "task_id": payload.get("ref_id"), "status": None}


def _project_task_updated(payload: dict[str, Any]) -> dict[str, Any]:
    return _mirrored_kind_fields(payload, "task")


def _project_approval_requested(payload: dict[str, Any]) -> dict[str, Any]:
    return _mirrored_kind_fields(payload, "approval")


def _project_approval_elicitation(payload: dict[str, Any]) -> dict[str, Any]:
    """elicitation.py's ``_log`` payload: ``{"action", "approval_id", "tool", **extra}``.

    ``action`` is one of a fixed, code-defined enum ("requested", "approved",
    "denied", "timeout", ...) and ``approval_id``/``tool`` are identifiers —
    all safe. ``**extra`` (arbitrary tool-call kwargs, potentially including
    secrets or file paths) is never read.
    """
    action = payload.get("action")
    status = action if isinstance(action, str) and action in _ELICITATION_ACTIONS else None
    approval_id = payload.get("approval_id")
    return {
        "kind": "approval",
        "task_id": str(approval_id) if approval_id is not None else None,
        "status": status,
    }


def _project_cycle_updated(payload: dict[str, Any]) -> dict[str, Any]:
    """tools/cycles.py's payload: ``{"cycle": {...}, "transition": {...}?, "action": ...}``.

    Only ``cycle.id``/``cycle.delegation_id`` and ``cycle.state`` /
    ``transition.to_state`` are read — both are enum-like state tokens the
    cycle state machine itself defines, never operator-supplied prose. Any
    ``reason`` field on a transition is free text and is never read.
    """
    cycle = payload.get("cycle")
    cycle = cycle if isinstance(cycle, dict) else {}
    transition = payload.get("transition")
    transition = transition if isinstance(transition, dict) else {}
    task_id = cycle.get("delegation_id") or cycle.get("id")
    status = transition.get("to_state") or cycle.get("state")
    status = status if isinstance(status, str) else None
    return {
        "kind": "cycle",
        "task_id": str(task_id) if task_id is not None else None,
        "status": status,
    }


def _project_health_alert(payload: dict[str, Any]) -> dict[str, Any]:
    return _mirrored_kind_fields(payload, "health")


def _project_project_built(payload: dict[str, Any]) -> dict[str, Any]:
    """tools/tasks.py's ``project_build``: ``{"parent_task_id", "project_id", "identity"}``.

    All three are identifiers; ``status`` is a fixed constant ("built") since
    reaching this channel at all means the build event fired.
    """
    task_id = payload.get("parent_task_id") or payload.get("project_id")
    return {
        "kind": "build",
        "task_id": str(task_id) if task_id is not None else None,
        "status": "built",
    }


_PROJECTORS = {
    CHANNEL_TASK_UPDATED: _project_task_updated,
    CHANNEL_APPROVAL_REQUESTED: _project_approval_requested,
    CHANNEL_APPROVAL_ELICITATION: _project_approval_elicitation,
    CHANNEL_CYCLE_UPDATED: _project_cycle_updated,
    CHANNEL_HEALTH_ALERT: _project_health_alert,
    CHANNEL_PROJECT_BUILT: _project_project_built,
}


def project_event(raw_event: dict[str, Any]) -> dict[str, Any] | None:
    """Project one ``events.query_events`` row into a metadata-only record.

    Returns ``None`` for a channel outside ``ALLOWLISTED_CHANNELS`` — belt
    and suspenders with the caller filtering channels at query time; this
    function refuses to trust that filter alone.
    """
    channel = raw_event.get("channel")
    projector = _PROJECTORS.get(channel)
    if projector is None:
        return None
    payload = raw_event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    fields = projector(payload)
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_id": raw_event.get("ulid"),
        "channel": channel,
        "agent": raw_event.get("originator"),
        "timestamp": raw_event.get("created_at"),
        **fields,
    }
    extra_keys = set(record) - _ALLOWED_RECORD_KEYS
    if extra_keys:  # pragma: no cover - defense in depth, not reachable via _PROJECTORS above
        raise RuntimeError(f"projector for {channel!r} produced disallowed keys: {extra_keys}")
    return record


def _lookup_statuses(conn: Any, table: str, ids: list[int]) -> dict[str, str]:
    """One batched ``SELECT id, status FROM <table> WHERE id IN (...)``.

    ``table`` is always one of the two fixed literals ``run_once`` passes
    below, never external input, so the f-string is safe. Ids/values only are
    parameterized.
    """
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, status FROM {table} WHERE id IN ({placeholders})", ids
    ).fetchall()  # noqa: S608
    return {str(row["id"]): row["status"] for row in rows}


def _enrich_structured_statuses(records: list[dict[str, Any]]) -> None:
    """Batched, read-only status backfill for task and approval-requested records.

    Mutates ``records`` in place. Runs at most two queries total for the
    whole batch — one ``tasks`` lookup, one ``approvals`` lookup — never one
    query per event. This is a snapshot read of CURRENT status, taken at ship
    time; it is not the status the task/approval held at the moment the
    mirrored event itself fired (see ``_mirrored_kind_fields``'s docstring).

    A status value outside the table's own known CHECK-constraint enum is
    dropped (left ``None``) rather than passed through, so a future schema
    change that adds a new status value fails closed here until this
    module's enum constants are updated to match. Any failure reading
    storage (locked DB, missing file, schema drift) is logged and every
    affected record is simply left with ``status: None`` — this function
    must never raise into ``run_once``.
    """
    task_ids: set[int] = set()
    approval_ids: set[int] = set()
    for record in records:
        if record.get("task_id") is None:
            continue
        try:
            numeric_id = int(record["task_id"])
        except (TypeError, ValueError):
            continue
        if record.get("kind") == "task":
            task_ids.add(numeric_id)
        elif record.get("kind") == "approval":
            approval_ids.add(numeric_id)

    if not task_ids and not approval_ids:
        return

    try:
        conn = storage.connect()
        try:
            task_statuses = _lookup_statuses(conn, "tasks", sorted(task_ids))
            approval_statuses = _lookup_statuses(conn, "approvals", sorted(approval_ids))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - status enrichment must never break shipping
        logger.warning(
            "cloud_mirror: structured status lookup failed, leaving status null: %s", exc
        )
        return

    task_enum = _task_status_enum()
    for record in records:
        if record.get("kind") == "task":
            status = task_statuses.get(record.get("task_id"))
            record["status"] = status if status in task_enum else None
        elif (
            record.get("kind") == "approval"
            and record.get("channel") != CHANNEL_APPROVAL_ELICITATION
        ):
            status = approval_statuses.get(record.get("task_id"))
            record["status"] = status if status in _APPROVAL_STATUS_ENUM else None


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


class MirrorSink(Protocol):
    """Where a batch of projected records goes. Raise on failure; never swallow."""

    def send(self, records: list[dict[str, Any]]) -> None: ...


def _ensure_private_directories(path: Path) -> None:
    """Create missing directories in path with mode 0700; leave existing ones alone."""
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


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to cloud mirror JSONL sink")
        view = view[written:]


def _existing_file_sink_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    content = path.read_bytes()
    complete_end = content.rfind(b"\n") + 1
    tail = content[complete_end:]
    if tail:
        try:
            json.loads(tail)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # An interrupted final append is not a delivered event. Remove
            # only that incomplete tail so the next retry starts a new line.
            with path.open("r+b") as handle:
                handle.truncate(complete_end)
                handle.flush()
                os.fsync(handle.fileno())
            content = content[:complete_end]
        else:
            # A complete JSON value without its delimiter is also recoverable,
            # but it must be separated before appending the next record.
            fd = os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                _write_all(fd, b"\n")
            finally:
                os.close(fd)
            content += b"\n"

    event_ids: set[str] = set()
    for line in content.splitlines():
        try:
            event_id = json.loads(line).get("event_id")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if isinstance(event_id, str):
            event_ids.add(event_id)
    return event_ids


@dataclass
class FileSink:
    """Append batches as JSONL. For tests and local/offline use."""

    path: Path

    def send(self, records: list[dict[str, Any]]) -> None:
        _ensure_private_directories(self.path.parent)
        existing_ids = _existing_file_sink_ids(self.path)
        pending = []
        for record in records:
            event_id = record.get("event_id")
            if isinstance(event_id, str) and event_id in existing_ids:
                continue
            pending.append(record)
            if isinstance(event_id, str):
                existing_ids.add(event_id)
        payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in pending)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        original_size = os.fstat(fd).st_size
        try:
            _write_all(fd, payload.encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            # Preserve the cursor retry contract even when an append is
            # interrupted after writing a prefix of this batch.
            try:
                os.ftruncate(fd, original_size)
                os.fsync(fd)
            except OSError:
                logger.exception("cloud_mirror: could not roll back partial JSONL append")
            raise
        finally:
            os.close(fd)


class HttpSink:
    """POST a batch as JSON to a receiver (``cloud_mirror_receiver.py``'s ``/ingest``).

    The bearer token is read from the environment variable NAMED by
    ``token_env_var`` at send time only — never stored, never logged, and
    never accepted as a literal value. An injectable ``client`` (an
    ``httpx.Client``, typically built with a ``mock_transport`` in tests) lets
    tests exercise a genuinely unreachable target with zero real network
    activity.
    """

    def __init__(
        self,
        url: str,
        *,
        token_env_var: str = "UA_CLOUD_MIRROR_INGEST_TOKEN",
        timeout_s: float = 5.0,
        client: Any | None = None,
    ) -> None:
        parsed_url = urlsplit(url)
        loopback_http = parsed_url.scheme == "http" and parsed_url.hostname == "127.0.0.1"
        if (
            (parsed_url.scheme != "https" and not loopback_http)
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.fragment
            or parsed_url.query
            or "?" in url
        ):
            raise ValueError(
                "cloud mirror HTTP sink requires HTTPS or literal http://127.0.0.1, "
                "without credentials, query, or fragment"
            )
        self.url = url
        self._loopback_http = loopback_http
        self.token_env_var = token_env_var
        self.timeout_s = timeout_s
        self._client = client

    def _make_client(self):
        import httpx

        return httpx.Client(timeout=self.timeout_s, trust_env=not self._loopback_http)

    def send(self, records: list[dict[str, Any]]) -> None:
        import httpx

        token = os.environ.get(self.token_env_var, "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        owns_client = self._client is None
        client = self._client or self._make_client()
        try:
            response = client.post(self.url, json={"records": records}, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"cloud mirror HTTP sink failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()


# --------------------------------------------------------------------------
# Cursor persistence
# --------------------------------------------------------------------------


def default_cursor_path() -> Path:
    return config.data_home() / "cloud-mirror-cursor.json"


def read_cursor(path: Path | None = None) -> str | None:
    cursor_path = path or default_cursor_path()
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if cursor_path.is_symlink():
            raise ValueError(f"unreadable cursor file: {cursor_path}") from None
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable cursor file: {cursor_path}") from exc
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError(f"invalid cursor file: {cursor_path}")
    return cursor


def write_cursor(cursor: str, *, path: Path | None = None) -> None:
    cursor_path = path or default_cursor_path()
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{cursor_path.name}.", suffix=".tmp", dir=cursor_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"cursor": cursor}, handle)
        os.replace(tmp_name, cursor_path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Env gate + entry point
# --------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get("UA_CLOUD_MIRROR_ENABLED", "0") == "1"


def _default_sink() -> MirrorSink | None:
    """Build a sink from env vars, or ``None`` if none is configured.

    ``UA_CLOUD_MIRROR_SINK`` selects ``file`` (default target
    ``UA_CLOUD_MIRROR_FILE_PATH``, else ``<data_home>/cloud-mirror.jsonl``) or
    ``http`` (target ``UA_CLOUD_MIRROR_URL``, required).
    """
    kind = os.environ.get("UA_CLOUD_MIRROR_SINK", "file").strip().lower()
    if kind == "http":
        url = os.environ.get("UA_CLOUD_MIRROR_URL", "").strip()
        if not url:
            logger.warning("cloud_mirror: UA_CLOUD_MIRROR_SINK=http but UA_CLOUD_MIRROR_URL unset")
            return None
        try:
            return HttpSink(
                url,
                token_env_var=os.environ.get(
                    "UA_CLOUD_MIRROR_TOKEN_VAR", "UA_CLOUD_MIRROR_INGEST_TOKEN"
                ),
            )
        except ValueError as exc:
            logger.warning("cloud_mirror: invalid HTTP sink URL: %s", exc)
            return None
    if kind != "file":
        logger.warning("cloud_mirror: unsupported UA_CLOUD_MIRROR_SINK value %r", kind)
        return None
    data_home = config.data_home().resolve()
    configured_path = os.environ.get("UA_CLOUD_MIRROR_FILE_PATH")
    path = (
        Path(configured_path).expanduser() if configured_path else data_home / "cloud-mirror.jsonl"
    )
    if not path.is_absolute():
        path = data_home / path
    path = path.resolve()
    if path == data_home or data_home not in path.parents:
        logger.warning("cloud_mirror: file sink path must be below UA_HOME")
        return None
    return FileSink(path)


def run_once(
    *,
    sink: MirrorSink | None = None,
    cursor_path: Path | None = None,
    batch_size: int = 200,
    force: bool = False,
) -> dict[str, Any]:
    """Ship one batch of allowlisted, projected events past the persisted cursor.

    Never raises. ``force=True`` bypasses the ``UA_CLOUD_MIRROR_ENABLED`` gate
    (used by tests and by an operator's manual dry run); production callers
    (the timer unit) should leave it ``False``.
    """
    if not force and not enabled():
        return {"enabled": False, "shipped": 0}

    resolved_sink = sink or _default_sink()
    if resolved_sink is None:
        return {"enabled": True, "shipped": 0, "error": "no sink configured"}

    try:
        cursor = read_cursor(cursor_path)
    except ValueError as exc:
        logger.warning("cloud_mirror: %s; refusing to replay event history", exc)
        return {"enabled": True, "shipped": 0, "cursor": None, "error": "unreadable cursor"}
    try:
        raw_events = events.query_events(
            channels=list(ALLOWLISTED_CHANNELS),
            since=cursor,
            limit=batch_size,
            newest_first=False,
        )
    except Exception as exc:  # noqa: BLE001 - never break the hub on a read failure either
        logger.warning("cloud_mirror: event_log read failed: %s", exc)
        return {"enabled": True, "shipped": 0, "cursor": cursor, "error": str(exc)[:300]}

    if not raw_events:
        return {"enabled": True, "shipped": 0, "cursor": cursor}

    records = [r for r in (project_event(e) for e in raw_events) if r is not None]
    new_cursor = raw_events[-1]["ulid"]

    if not records:
        # Nothing allowlisted in this window, but the window itself was read
        # successfully -- advance past it so a run doesn't spin on the same
        # excluded events forever.
        try:
            write_cursor(new_cursor, path=cursor_path)
        except OSError as exc:
            logger.warning("cloud_mirror: cursor write failed at %s: %s", cursor, exc)
            return {"enabled": True, "shipped": 0, "cursor": cursor, "error": str(exc)[:300]}
        return {"enabled": True, "shipped": 0, "cursor": new_cursor}

    _enrich_structured_statuses(records)

    try:
        resolved_sink.send(records)
    except Exception as exc:  # noqa: BLE001 - sink failure must never raise into the hub
        logger.warning("cloud_mirror: sink failed, cursor held at %s: %s", cursor, exc)
        return {"enabled": True, "shipped": 0, "cursor": cursor, "error": str(exc)[:300]}

    try:
        write_cursor(new_cursor, path=cursor_path)
    except OSError as exc:
        logger.warning("cloud_mirror: cursor write failed after delivery at %s: %s", cursor, exc)
        return {"enabled": True, "shipped": 0, "cursor": cursor, "error": str(exc)[:300]}
    return {"enabled": True, "shipped": len(records), "cursor": new_cursor}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Ship one batch of allowlisted events to the cloud mirror sink."
    )
    parser.add_argument("--force", action="store_true", help="Ignore UA_CLOUD_MIRROR_ENABLED.")
    args = parser.parse_args(argv)
    result = run_once(force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
