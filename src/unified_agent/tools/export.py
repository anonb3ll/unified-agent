"""Structured export tools: tasks, memory notes, and vault journal entries.

Exports land in the device's inbox exports directory (inside the UA_VAULT_WRITE_DIRS
allowlist, enforced by safety.resolve_in_vault) or are returned inline.
The inline path is capped at WRITE_MAX_BYTES (2 MB); a direct vault write
gets the larger EXPORT_VAULT_MAX_BYTES ceiling. No PII redaction in v1 —
the operator reviews exports before sharing them.
"""

import csv
import datetime
import errno
import hashlib
import inspect
import io
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, profiles, safety, storage
from ..safety import resolve_in_vault
from .memory import (
    GRAPH_BACKFILL_META_KEY,
    GRAPH_IMPORT_META_PREFIX,
    _graph_import_incarnation,
    _graph_import_owners,
    _is_index_id,
)
from .tasks import PROJECTS, STATUSES

TASK_COLUMNS = (
    "id",
    "title",
    "notes",
    "status",
    "priority",
    "parent_id",
    "created_at",
    "updated_at",
    "completed_at",
    "source_host",
    "project",
)

TASK_FORMATS = ("csv", "json", "markdown")
MEMORY_FORMATS = ("jsonl", "json")

_MANIFEST_TASK_COLUMNS = (
    "id",
    "title",
    "notes",
    "status",
    "priority",
    "parent_id",
    "created_at",
    "updated_at",
    "completed_at",
    "archived_at",
    "source_host",
    "claimed_by",
    "claimed_at",
    "lock_expires_at",
    "untrusted",
    "worktree_path",
    "branch",
    "project",
)
_MANIFEST_MEMORY_COLUMNS = (
    "id",
    "title",
    "content",
    "tags",
    "author",
    "updated_by",
    "created_at",
    "updated_at",
    "salience",
    "confidence",
    "superseded_by",
    "source_host",
    "expires_at",
    "feedback_helpful",
    "feedback_not_helpful",
    "feedback_stale",
    "feedback_wrong",
    # Evidence-backed memory (task #1459). A manifest from a hub without these
    # columns reads them as NULL, which matches an unlabeled local row.
    "epistemic",
    "evidence",
    "valid_from",
    "contradicts",
    # Bi-temporal validity (task #2095); NULL from an older hub = still valid.
    "valid_to",
)

# Columns, composite primary key, and note references. Tenant IDs are deliberately
# omitted from the wire format: imports stamp the authenticated target scope.
_GRAPH_TABLES = {
    "memory_entities": (("note_id", "kind", "value"), ("note_id", "kind", "value"), ("note_id",)),
    "memory_links": (
        ("src_id", "dst_id", "rel", "actor", "created_at"),
        ("src_id", "dst_id", "rel"),
        ("src_id", "dst_id"),
    ),
}
_GRAPH_IMPORT_RECEIPT_PREFIX = "memory_graph_import_receipt:"
_GRAPH_IMPORT_OWNERSHIP_TABLE = "memory_graph_import_ownership"
_IMPORT_ROW_OWNERSHIP_TABLE = "migration_import_row_ownership"
# memory_embeddings and memory_chunks are rebuildable vector caches, not import
# references. hard_delete_memory removes both, including their vec0 mirrors,
# when a rolled-back note goes away.
_MEMORY_IMPORT_REFERENCES = (("memory_recall_votes", "note_id"), ("delegations", "memory_id"))
# Durable task relationships are not included in migration manifests. The schema
# inventory regression requires an explicit decision for every new task-ID column;
# task_embeddings is the only excluded relationship (rebuildable vector cache).
_TASK_IMPORT_REFERENCES = (
    ("delegations", "task_id"),
    ("dispatch_launch_keys", "task_id"),
    ("dispatch_runs", "task_id"),
    ("task_dependencies", "task_id"),
    ("task_dependencies", "depends_on_task_id"),
    ("executor_attempts", "task_id"),
    ("agent_sessions", "task_id"),
    ("autonomous_cycles", "task_id"),
    ("workflow_instances", "task_id"),
    ("workflow_task_steps", "task_id"),
    ("workflow_closures", "task_id"),
    ("routine_runs", "task_id"),
    ("task_holds", "root_task_id"),
    ("task_hold_members", "task_id"),
    ("node_dispatch", "task_id"),
    ("task_goal_links", "task_id"),
    ("task_comments", "task_id"),
    ("task_comment_threads", "task_id"),
    ("sprint_tasks", "task_id"),
    ("run_settings", "task_id"),
    ("run_operating_snapshots", "task_id"),
    ("run_usage_heartbeats", "task_id"),
    ("session_credentials", "task_id"),
    ("task_liveness", "task_id"),
    ("task_origins", "task_id"),
    ("completion_deliveries", "task_id"),
    ("message_index", "task_id"),
    ("work_proposal_items", "task_id"),
    ("session_handoff_notes", "task_id"),
    ("task_workflow", "task_id"),
    ("task_checks", "task_id"),
    ("design_panels", "task_id"),
    ("friction_reports", "task_id"),
    # The operator's release or keep of a review hold (task #2514): later activity.
    ("review_hold_decisions", "task_id"),
)

# Journal lines exactly as vault_journal writes them:
# '- **HH:MM** [Agent_Label] text #tag1 #tag2'
JOURNAL_LINE_RE = re.compile(r"^- \*\*(\d{2}:\d{2})\*\* \[([^\]]+)\] (.*)$")
DAILY_NOTE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def _parse_since(since: str | None) -> datetime.date | None:
    if since is None:
        return None
    try:
        return datetime.date.fromisoformat(since)
    except ValueError:
        raise ToolError(f"since must be an ISO date (YYYY-MM-DD), got {since!r}") from None


#: Same-second export names ``_deliver`` tries before giving up.
_EXPORT_NAME_ATTEMPTS = 1000


def _deliver(name: str, ext: str, content: str, rows: int, write_to_vault: bool) -> dict:
    """Return the export inline, or write it under the vault export dir.

    The inline path is capped at WRITE_MAX_BYTES (it rides an MCP response).
    A direct vault write gets the larger EXPORT_VAULT_MAX_BYTES ceiling so a
    full memory/task backup — the weekly-export cron's job — isn't rejected
    for size alone.
    """
    data = content.encode()
    cap = config.EXPORT_VAULT_MAX_BYTES if write_to_vault else config.WRITE_MAX_BYTES
    if len(data) > cap:
        raise ToolError(
            f"export is {len(data)} bytes, over the {cap} byte cap; "
            "narrow it with filters (status/tag/since)"
        )
    if not write_to_vault:
        return {"content": content, "rows": rows}
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    # Structured exports need real extensions (.csv opens in LibreOffice);
    # opt into them explicitly on top of the default .md/.txt vault write set.
    export_suffixes = (".csv", ".json", ".jsonl")
    stem = f"{name}-{ts}"

    def candidate(n: int) -> Path:
        # Same-second exports must not clobber each other: bump a counter suffix.
        suffix = "" if n == 1 else f"-{n}"
        return resolve_in_vault(
            f"00 - Inbox/exports/{stem}{suffix}.{ext}",
            for_write=True,
            extra_write_suffixes=export_suffixes,
        )

    directory = candidate(1).parent
    directory.mkdir(parents=True, exist_ok=True)
    root = config.vault_root().expanduser().resolve()
    p, leftover = _publish_export(directory, stem, ext, data, candidate, root)
    out = {"path": str(p.relative_to(root)), "rows": rows, "bytes": len(data)}
    if leftover:
        out["leftover_temp"] = leftover
    return out


#: ``os.link`` errnos that mean "this filesystem has no hard links", which
#: send ``_publish_export`` to the in-place exclusive write. Built from the
#: platform's own constants (Darwin's ENOTSUP and EOPNOTSUPP differ; Linux
#: aliases them). EACCES is deliberately absent: from link(2) it means write or
#: search permission on the directory was denied, a real refusal that is
#: reported, not a missing feature to work around.
_LINK_UNSUPPORTED = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}
)


def _os_error(exc: OSError) -> str:
    """``OSError`` as ``ENAME: message``, so a refusal names its errno on every platform."""
    name = errno.errorcode.get(exc.errno, f"errno {exc.errno}") if exc.errno else "OSError"
    return f"{name}: {exc}"


def _publish_export(directory: Path, stem: str, ext: str, data: bytes, candidate, root: Path):
    """Write ``data`` under a free export name, whole or not at all (#2091, #2094).

    The bytes go to a private hidden temp file in the export directory first,
    are fsynced, and only then are published with ``os.link`` into the first
    free name from ``candidate(1)``, ``candidate(2)``, ... A hard link never
    replaces an existing file (it fails with ``FileExistsError`` and the next
    name is tried), so no pathname is ever claimed empty and later replaced or
    unlinked: a note Obsidian Sync or an operator puts at an export name while
    the export runs, locked or not, is never overwritten or deleted. Two
    exporters racing on one name cannot both publish it. On any failure only
    the private temp is removed; a published name is never touched again. The
    directory is fsynced (best effort) after the link and the temp's removal.

    A filesystem without hard links (``os.link`` fails with one of
    ``_LINK_UNSUPPORTED``) falls back to the pre-#2094 write: an exclusive
    create (``open(..., "xb")``) of the free name, written through its own
    descriptor (see ``_write_exclusive_in_place`` for its limits).

    Every exit reports what is left behind: an ``OSError`` becomes a
    ``ToolError`` naming its errno, and a temp that could not be removed is
    named in the error, or returned as ``leftover`` (vault-relative) after a
    successful publish.
    """
    label = f"{stem}.{ext}"
    tmp = directory / f".{stem}.{secrets.token_hex(8)}.tmp"
    tmp_made = False
    try:
        try:
            with open(tmp, "xb") as f:
                tmp_made = True
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            published = _link_into_free_name(tmp, candidate, label)
        except OSError as exc:
            raise ToolError(
                f"export of {label} failed ({_os_error(exc)}). Nothing was published "
                "under an export name. Check free space and permissions on the vault, "
                "then retry"
            ) from exc
        if published is None:
            published = _write_exclusive_in_place(data, candidate, root, label)
    except BaseException as exc:
        left = _remove_private_temp(tmp, root) if tmp_made else None
        if left is None:
            raise
        note = f" The temp file {left} could not be removed; delete it by hand."
        if isinstance(exc, ToolError):
            raise ToolError(f"{exc}{note}") from (exc.__cause__ or exc)
        raise
    leftover = _remove_private_temp(tmp, root)
    _fsync_directory(directory)
    return published, leftover


def _link_into_free_name(tmp: Path, candidate, label: str) -> Path | None:
    """Hard-link ``tmp`` into the first free candidate name; ``None`` if links are unsupported."""
    for n in range(1, _EXPORT_NAME_ATTEMPTS + 1):
        p = candidate(n)
        try:
            os.link(tmp, p)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in _LINK_UNSUPPORTED:
                return None
            raise
        return p
    raise ToolError(
        f"could not claim a free export name for {label} after "
        f"{_EXPORT_NAME_ATTEMPTS} tries; retry in a second"
    )


def _write_exclusive_in_place(data: bytes, candidate, root: Path, label: str) -> Path:
    """The no-hard-link fallback: exclusive-create a free name and write through it.

    The exclusive create keeps the name claim race-free against other
    exporters, but it owns the inode only at the instant it runs. Two limits
    follow, and both are reported rather than hidden:

    - A failure part-way can leave an incomplete file at the name. It is not
      deleted, because by then the name cannot be proven ours.
    - A note renamed onto the name mid-write leaves this descriptor on the
      displaced inode. After the write and fsync the descriptor's inode is
      compared with the name's; on a mismatch (or a vanished name) the export
      is reported as not delivered and the note now there is left alone.
    """
    fallback = "this vault's filesystem has no hard links, so the export is written in place"
    for n in range(1, _EXPORT_NAME_ATTEMPTS + 1):
        p = candidate(n)
        rel = p.relative_to(root)
        try:
            f = open(p, "xb")
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError(
                f"export could not create {rel} ({_os_error(exc)}); {fallback}. Nothing "
                "should be left at that name. Check permissions and free space on the "
                "vault, then retry"
            ) from exc
        try:
            with f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                mine = os.fstat(f.fileno())
            try:
                there = os.stat(p, follow_symlinks=False)
            except FileNotFoundError:
                there = None
        except OSError as exc:
            raise ToolError(
                f"export write to {rel} failed ({_os_error(exc)}); {fallback}, and {rel} "
                "may be incomplete. It was not deleted, since another writer may have "
                "put a note there: check it, delete it if it is a partial export, then "
                "retry"
            ) from exc
        if there is None or (there.st_dev, there.st_ino) != (mine.st_dev, mine.st_ino):
            what = "vanished" if there is None else "now holds a file this export did not write"
            raise ToolError(
                f"export name {rel} was taken over while the export was written ({fallback}): "
                f"the name {what}, so the export was not delivered. The file there was not "
                "touched. Retry the export"
            )
        _fsync_directory(p.parent)
        return p
    raise ToolError(
        f"could not claim a free export name for {label} after "
        f"{_EXPORT_NAME_ATTEMPTS} tries; retry in a second"
    )


def _remove_private_temp(tmp: Path, root: Path) -> str | None:
    """Best-effort removal of the export's own temp; the vault-relative path if it survives.

    Never raises, so a cleanup failure cannot hide the error that caused it.
    """
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        return None
    except OSError:
        return str(tmp.relative_to(root))
    return None


def _fsync_directory(directory: Path) -> None:
    """Best effort: make the directory's new or removed entries durable.

    Some platforms and filesystems refuse to open or fsync a directory; the
    export's bytes are already fsynced, so a refusal here is not an error.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _md_cell(value) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def export_tasks(
    format: str = "csv",
    status: str = "all",
    write_to_vault: bool = True,
    project: str | None = None,
) -> dict:
    """Export the task list as csv, json, or markdown.

    `status` filters to one of open|doing|blocked|done, or "all". `project`
    restricts to one project stream (configured project stream ids); omitted exports across every project. With write_to_vault
    the file lands in "00 - Inbox/exports/" and the result is {path, rows,
    bytes}; otherwise {content, rows}. Inline cap 2 MB; vault-write cap 32 MB.
    """
    if format not in TASK_FORMATS:
        raise ToolError(f"format must be one of {TASK_FORMATS}, got {format!r}")
    if status != "all" and status not in STATUSES:
        raise ToolError(f"status must be one of {STATUSES + ('all',)}, got {status!r}")
    if project is not None and project not in PROJECTS:
        raise ToolError(f"project must be one of {PROJECTS}, got {project!r}")
    # NULL is the private-hub scope, never a wildcard across tenants.
    where, params = ["org_id IS ?"], [edition.tenant_org_id()]
    if status != "all":
        where.append("status=?")
        params.append(status)
    if project is not None:
        where.append("project=?")
        params.append(project)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = storage.connect()
    try:
        rows = conn.execute(f"SELECT * FROM tasks {clause} ORDER BY id", params).fetchall()
    finally:
        conn.close()
    records = [{c: r[c] for c in TASK_COLUMNS} for r in rows]

    if format == "csv":
        buf = io.StringIO()
        # csv defaults (QUOTE_MINIMAL + CRLF line endings) are RFC-4180 output:
        # commas/quotes/newlines in fields are quoted, so LibreOffice opens it.
        writer = csv.writer(buf)
        writer.writerow(TASK_COLUMNS)
        for rec in records:
            writer.writerow(["" if rec[c] is None else rec[c] for c in TASK_COLUMNS])
        return _deliver("tasks", "csv", buf.getvalue(), len(records), write_to_vault)
    if format == "json":
        return _deliver(
            "tasks", "json", json.dumps(records, indent=2) + "\n", len(records), write_to_vault
        )
    lines = [
        "| " + " | ".join(TASK_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in TASK_COLUMNS) + " |",
    ]
    for rec in records:
        lines.append("| " + " | ".join(_md_cell(rec[c]) for c in TASK_COLUMNS) + " |")
    return _deliver("tasks", "md", "\n".join(lines) + "\n", len(records), write_to_vault)


def export_memory(
    format: str = "jsonl",
    tag: str | None = None,
    since: str | None = None,
    write_to_vault: bool = True,
) -> dict:
    """Export memory notes as jsonl or json.

    Auto-maintained index notes are excluded; superseded notes are included
    and flagged (`superseded` / `superseded_by`). `tag` filters to notes
    carrying it; `since` (ISO date) filters to notes updated on/after it.
    With write_to_vault the file lands in "00 - Inbox/exports/" and the
    result is {path, rows, bytes}; otherwise {content, rows}. Inline cap 2 MB;
    vault-write cap 32 MB. Each note includes its entities and outgoing links
    whose endpoints are both in the filtered export, without graph read caps.
    """
    if format not in MEMORY_FORMATS:
        raise ToolError(f"format must be one of {MEMORY_FORMATS}, got {format!r}")
    since_date = _parse_since(since)
    conn = storage.connect()
    try:
        conn.execute("BEGIN")  # Notes and graph must describe the same read snapshot.
        # Tenant scope (task #1459): only the caller's tenant's notes, so no
        # other tenant's content, evidence refs or note links are exported.
        # NULL is the private-hub scope, never a wildcard.
        rows = conn.execute(
            "SELECT * FROM memory WHERE org_id IS ? ORDER BY updated_at, id",
            (edition.tenant_org_id(),),
        ).fetchall()
        graph = _graph_rows(conn, {r["id"] for r in rows if not _is_index_id(r["id"])})
    finally:
        conn.close()

    records = []
    for r in rows:
        if _is_index_id(r["id"]):
            continue
        tags = json.loads(r["tags"])
        if tag is not None and tag not in tags:
            continue
        if since_date is not None:
            try:
                updated = datetime.datetime.fromisoformat(r["updated_at"]).date()
            except ValueError:
                updated = None  # unparseable timestamp: keep the note rather than drop it
            if updated is not None and updated < since_date:
                continue
        keys = r.keys()
        superseded_by = r["superseded_by"] if "superseded_by" in keys else None
        records.append(
            {
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "tags": tags,
                "author": r["author"] if "author" in keys else None,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "salience": r["salience"] if "salience" in keys else 0.5,
                "confidence": r["confidence"] if "confidence" in keys else None,
                "superseded": bool(superseded_by),
                "superseded_by": superseded_by,
                "source_host": r["source_host"] if "source_host" in keys else None,
                # Evidence-backed fields (task #1459); null/empty for legacy notes.
                "epistemic": r["epistemic"] if "epistemic" in keys else None,
                "evidence": _json_list(r["evidence"]) if "evidence" in keys else [],
                "valid_from": r["valid_from"] if "valid_from" in keys else None,
                "contradicts": _json_list(r["contradicts"]) if "contradicts" in keys else [],
                "valid_to": r["valid_to"] if "valid_to" in keys else None,
            }
        )

    exported = {rec["id"] for rec in records}
    entities: dict[str, list] = {}
    links: dict[str, list] = {}
    for row in graph["memory_entities"]:
        entities.setdefault(row["note_id"], []).append(row)
    for row in graph["memory_links"]:
        if row["src_id"] in exported and row["dst_id"] in exported:
            links.setdefault(row["src_id"], []).append(row)
    for rec in records:
        rec["entities"] = entities.get(rec["id"], [])
        rec["links"] = links.get(rec["id"], [])

    if format == "jsonl":
        content = "".join(json.dumps(rec) + "\n" for rec in records)
    else:
        content = json.dumps(records, indent=2) + "\n"
    return _deliver("memory", format, content, len(records), write_to_vault)


def _json_list(raw) -> list:
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _manifest_rows(
    conn, table: str, columns: tuple[str, ...], order_by: str = "id", *, tenant: bool = False
) -> list[dict]:
    available = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    selected = [c for c in columns if c in available]
    where, params = "", ()
    if tenant and "org_id" in available:
        # Same tenant rule as export_memory: NULL is the private-hub scope.
        where, params = " WHERE org_id IS ?", (edition.tenant_org_id(),)
    rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM {table}{where} ORDER BY {order_by}", params
    ).fetchall()
    return [{c: row[c] for c in selected} for row in rows]


def _manifest_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _graph_rows(conn, note_ids: set[str]) -> dict[str, list[dict]]:
    """Read complete graph rows, fencing both the row and every note endpoint.

    A pre-graph database exports empty collections. Joins also withhold corrupt
    cross-tenant rows written around the DB triggers, as graph retrieval does.
    """
    graph = {}
    org_id = edition.tenant_org_id()
    for table, (columns, keys, refs) in _GRAPH_TABLES.items():
        if not conn.execute(f"PRAGMA table_info({table})").fetchall():
            graph[table] = []
            continue
        joins = " ".join(
            f"JOIN memory n{i} ON n{i}.id=g.{ref} AND n{i}.org_id IS ?"
            for i, ref in enumerate(refs)
        )
        rows = conn.execute(
            f"SELECT {', '.join('g.' + c for c in columns)} FROM {table} g {joins} "
            f"WHERE g.org_id IS ? ORDER BY {', '.join('g.' + c for c in keys)}",
            (org_id,) * (len(refs) + 1),
        ).fetchall()
        graph[table] = [dict(row) for row in rows if all(row[ref] in note_ids for ref in refs)]
    return graph


def export_migration_manifest(write_to_vault: bool = False) -> dict:
    """Create a deterministic, read-only snapshot for cross-hub migration review.

    The manifest preserves stable task/memory IDs, relationships, claims,
    archival/review markers, source hosts, and append-only provenance indexes.
    It never imports, rewrites, or reconciles rows. ``write_to_vault`` only
    records the generated JSON under the normal Inbox export allowlist.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN")
        tasks = _manifest_rows(conn, "tasks", _MANIFEST_TASK_COLUMNS, tenant=True)
        memory = _manifest_rows(conn, "memory", _MANIFEST_MEMORY_COLUMNS, tenant=True)
        memory = [row for row in memory if not _is_index_id(row["id"])]
        graph = _graph_rows(conn, {row["id"] for row in memory})
        agent_events = _manifest_rows(
            conn, "agent_events", ("id", "agent", "ts", "kind", "ref_id", "summary")
        )
        event_log = _manifest_rows(
            conn,
            "event_log",
            (
                "ulid",
                "channel",
                "payload",
                "originator",
                "episode_id",
                "causal_parent_id",
                "created_at",
            ),
            order_by="ulid",
            tenant=True,
        )
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        # agent_events has no tenant column (task #1459). Every manifest,
        # including private NULL scope, keeps only rows whose owning record
        # belongs to that scope. Unowned and foreign refs are withheld/countable.
        total = len(agent_events)
        agent_events = _tenant_agent_events(conn, agent_events)
        withheld = total - len(agent_events)
    finally:
        conn.close()

    payload = {
        "manifest_version": 1,
        "source_host": config.current_source_host(),
        "schema_version": schema_version["value"] if schema_version else None,
        "tasks": tasks,
        "memory": memory,
        **graph,
        "agent_events": agent_events,
        "event_log": event_log,
    }
    payload["agent_events_withheld"] = withheld
    manifest = {
        **payload,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "fingerprint": _manifest_hash(payload),
    }
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if write_to_vault:
        delivered = _deliver("migration-manifest", "json", content, 1, True)
        delivered["fingerprint"] = manifest["fingerprint"]
        return {"manifest": manifest, **delivered}
    return {"manifest": manifest, "content": content, "rows": len(tasks) + len(memory)}


# What each agent_events kind's ref_id points at, from its producers
# (storage.record_agent_event callers). A kind is attributable only when the
# owning table carries org_id; a table without the column (a hub before its
# migration) withholds its kind. Unlisted kinds (lifecycle: agent label,
# langgraph_effect: operation key, workflow: workflow name, journal: vault
# path) cannot be attributed and are withheld.
_AGENT_EVENT_OWNERS: dict[str, tuple[str, str]] = {
    "task": ("tasks", "id"),  # tools/tasks.py, routines.py, tools/doctor.py
    "run_recorder": ("tasks", "id"),  # run_records.py: ref_id=str(task_id)
    "memory": ("memory", "id"),  # tools/memory.py
    "delegation": ("delegations", "id"),  # tools/delegate.py
    "executor_shadow": ("delegations", "id"),  # executor/daemon.py
    "executor_circuit_breaker": ("delegations", "id"),  # executor/daemon.py
    "approval": ("approvals", "id"),  # tools/approvals.py
    "plan": ("plans", "id"),  # tools/plan.py; plans.org_id since task #2014
    "fleet": ("event_log", "ulid"),  # fleet_actions.py: ref_id=event ulid
}


_SQLITE_PARAM_CHUNK = 500  # well under SQLite's default 999 host-parameter limit


def _native_key(ref: str, key_type: str):
    """Convert a TEXT ref_id to the owner key's native type, or None to withhold."""
    if "INT" in key_type.upper():
        try:
            value = int(ref)
            # Python ints are unbounded; SQLite INTEGER parameters are signed 64-bit.
            return value if -(1 << 63) <= value < (1 << 63) else None
        except (TypeError, ValueError):
            return None
    return ref


def _tenant_agent_events(conn, rows: list[dict]) -> list[dict]:
    """Keep rows whose ref resolves to this tenant via its owning table.

    Refs are grouped per owning table, converted to the key's native type and
    checked in batched ``key IN (...)`` lookups, so each uses the primary-key
    index rather than a per-ref scan. A ref that does not convert is withheld.
    """
    org_id = edition.tenant_org_id()
    by_table: dict[tuple[str, str], dict] = {}
    for row in rows:
        owner = _AGENT_EVENT_OWNERS.get(row.get("kind"))
        if owner is not None and row.get("ref_id") is not None:
            by_table.setdefault(owner, {})[str(row["ref_id"])] = None
    owned: set[tuple[str, str, str]] = set()
    for (table, key), refs in by_table.items():
        info = {r["name"]: r["type"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "org_id" not in info or key not in info:
            continue
        native = {ref: _native_key(ref, info[key] or "") for ref in refs}
        values = sorted({v for v in native.values() if v is not None}, key=str)
        found: set[str] = set()
        for start in range(0, len(values), _SQLITE_PARAM_CHUNK):
            chunk = values[start : start + _SQLITE_PARAM_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for hit in conn.execute(
                f"SELECT {key} FROM {table} WHERE org_id IS ? AND {key} IN ({marks})",
                (org_id, *chunk),
            ):
                found.add(str(hit[0]))
        owned |= {
            (table, key, ref) for ref, v in native.items() if v is not None and str(v) in found
        }
    return [
        row
        for row in rows
        if row.get("kind") in _AGENT_EVENT_OWNERS
        and (*_AGENT_EVENT_OWNERS[row["kind"]], str(row.get("ref_id"))) in owned
    ]


def _index_by_id(rows: list[dict], keys: tuple[str, ...] = ("id",)) -> dict[str, dict]:
    return {
        str(row["id"]) if keys == ("id",) else json.dumps([row[k] for k in keys]): row
        for row in rows
    }


def _rows_differ(left: dict, right: dict) -> bool:
    """A key missing on one side reads as NULL, so a manifest written before a
    column existed (e.g. the #1459 memory fields) does not flag every row."""
    return any(left.get(key) != right.get(key) for key in set(left) | set(right))


def _content_key(row: dict, keys: tuple[str, ...]) -> str:
    return _manifest_hash({key: row.get(key) for key in keys})


def compare_migration_manifests(left: dict, right: dict) -> dict:
    """Diff two manifests without changing either source or the local database."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ToolError("left and right must be manifest objects")
    if left.get("manifest_version") != 1 or right.get("manifest_version") != 1:
        raise ToolError("unsupported migration manifest version")
    _validate_graph_rows(left)
    _validate_graph_rows(right)

    conflicts: list[dict] = []
    summary: dict[str, dict[str, int]] = {}
    for kind in ("tasks", "memory", *_GRAPH_TABLES):
        keys = _GRAPH_TABLES[kind][1] if kind in _GRAPH_TABLES else ("id",)
        left_rows = _index_by_id(left.get(kind, []), keys)
        right_rows = _index_by_id(right.get(kind, []), keys)
        left_ids, right_ids = set(left_rows), set(right_rows)
        only_left = sorted(left_ids - right_ids)
        only_right = sorted(right_ids - left_ids)
        changed = sorted(
            item_id
            for item_id in left_ids & right_ids
            if _rows_differ(left_rows[item_id], right_rows[item_id])
        )
        summary[kind] = {
            "left_only": len(only_left),
            "right_only": len(only_right),
            "changed": len(changed),
            "same": len(left_ids & right_ids) - len(changed),
        }
        for item_id in changed:
            conflict = {"kind": kind, "id": item_id, "type": "conflicting_edit"}
            if kind == "tasks":
                left_claim = left_rows[item_id].get("claimed_by")
                right_claim = right_rows[item_id].get("claimed_by")
                if left_claim != right_claim and (left_claim or right_claim):
                    conflict["type"] = "active_claim_conflict"
                    conflict["left_claimed_by"] = left_claim
                    conflict["right_claimed_by"] = right_claim
            conflicts.append(conflict)
        conflicts.extend(
            {"kind": kind, "id": item_id, "type": "left_only"} for item_id in only_left
        )
        conflicts.extend(
            {"kind": kind, "id": item_id, "type": "right_only"} for item_id in only_right
        )

        if kind in _GRAPH_TABLES:
            continue
        by_content: dict[str, list[str]] = {}
        for item_id, row in {**left_rows, **right_rows}.items():
            content_keys = ("title", "notes") if kind == "tasks" else ("title", "content")
            by_content.setdefault(_content_key(row, content_keys), []).append(item_id)
        for ids in by_content.values():
            if len(set(ids)) > 1:
                conflicts.append(
                    {"kind": kind, "ids": sorted(set(ids)), "type": "duplicate_candidate"}
                )

    task_ids = {str(row["id"]) for row in left.get("tasks", [])}
    for row in left.get("tasks", []):
        parent_id = row.get("parent_id")
        if parent_id is not None and str(parent_id) not in task_ids:
            conflicts.append({"kind": "tasks", "id": str(row["id"]), "type": "missing_parent"})
    return {
        "left_source_host": left.get("source_host"),
        "right_source_host": right.get("source_host"),
        "left_fingerprint": left.get("fingerprint"),
        "right_fingerprint": right.get("fingerprint"),
        "same": left.get("fingerprint") == right.get("fingerprint"),
        "summary": summary,
        "conflicts": conflicts,
        "requires_human_disposition": bool(conflicts),
    }


# Tenant fence for migration import/rollback (task #1795). ``IS`` rather than
# ``=`` so the untenanted private hub (``tenant_org_id() is None``) matches
# its NULL rows; the same rule as tools/memory.py's _TENANT_SQL.
_TENANT_SQL = "org_id IS ?"
_INSERT_REJECTED = {"reason": "insert_rejected"}


def _local_row(conn, table: str, columns: tuple[str, ...], row_id, org_id) -> dict | None:
    """The caller's tenant's row ``row_id``, or ``None``.

    Another tenant's row with the same id reads exactly like an absent one,
    so the import's unchanged/conflicting classification is never an
    existence or content oracle for it.
    """
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE id=? AND {_TENANT_SQL}",
        (row_id, org_id),
    ).fetchone()
    return {c: row[c] for c in columns} if row is not None else None


def _try_insert(conn, table: str, columns: tuple[str, ...], row: dict, org_id) -> bool:
    """Insert one manifest row stamped with the caller's ``org_id``.

    Runs under a savepoint so a rejected row leaves the rest of the import
    intact. Any integrity failure returns ``False`` with no detail: ids are
    global primary keys, so a collision with another tenant's row cannot be
    told apart from any other constraint failure, and must not be.
    """
    conn.execute("SAVEPOINT migration_row")
    try:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}, org_id) "
            f"VALUES ({', '.join('?' for _ in columns)}, ?)",
            (*(row.get(c) for c in columns), org_id),
        )
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT migration_row")
        conn.execute("RELEASE SAVEPOINT migration_row")
        return False
    conn.execute("RELEASE SAVEPOINT migration_row")
    return True


def _validate_graph_rows(payload: dict, *, rollback: bool = False) -> None:
    """Reject malformed/ambiguous graph input before any database mutation."""
    for table, (columns, keys, _refs) in _GRAPH_TABLES.items():
        rows = payload.get(table, [])
        if not isinstance(rows, list):
            raise ToolError(f"{table} must be a list of graph row objects")
        seen = set()
        for row in rows:
            required = keys if rollback else tuple(c for c in columns if c != "actor")
            if not isinstance(row, dict) or any(not isinstance(row.get(c), str) for c in required):
                raise ToolError(f"{table} rows require string fields: {required}")
            if not rollback and row.get("actor") is not None and not isinstance(row["actor"], str):
                raise ToolError(f"{table} actor must be a string or null")
            key = tuple(row[c] for c in keys)
            if key in seen:
                raise ToolError(f"{table} contains duplicate graph keys")
            seen.add(key)


def _ensure_graph_import_ownership(conn) -> None:
    """Track a graph row's current inserting import, across every SQL writer.

    An import ID is a fresh row incarnation, not a timestamp or SQLite rowid.
    Native deletes, replacements and edits revoke that ownership inside the
    writer's transaction. INSERT invalidation also covers INSERT OR REPLACE
    when SQLite's recursive delete triggers are disabled. The schema is lazy:
    databases that never import graphs need no ownership bookkeeping. A missing
    trigger leaves every existing proof for that table unprovable, so discard
    them transactionally before repairing the protection gap.
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_GRAPH_IMPORT_OWNERSHIP_TABLE} ("
        "graph_table TEXT NOT NULL, graph_key TEXT NOT NULL, owner TEXT NOT NULL, "
        "PRIMARY KEY (graph_table, graph_key))"
    )
    for table, (_columns, keys, _refs) in _GRAPH_TABLES.items():
        triggers = [f"{table}_import_owner_{event}" for event in ("insert", "update", "delete")]
        installed = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", triggers
            )
        }
        if installed != set(triggers):
            conn.execute(
                f"DELETE FROM {_GRAPH_IMPORT_OWNERSHIP_TABLE} WHERE graph_table=?", (table,)
            )
        old = "json_array(" + ", ".join(f"OLD.{key}" for key in keys) + ")"
        new = "json_array(" + ", ".join(f"NEW.{key}" for key in keys) + ")"
        for event, matching in (
            ("INSERT", f"graph_key={new}"),
            ("DELETE", f"graph_key={old}"),
            ("UPDATE", f"graph_key IN ({old}, {new})"),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_import_owner_{event.lower()} "
                f"BEFORE {event} ON {table} BEGIN "
                f"DELETE FROM {_GRAPH_IMPORT_OWNERSHIP_TABLE} "
                f"WHERE graph_table='{table}' AND {matching}; END"
            )


def _ensure_import_row_ownership(conn) -> None:
    """Revoke rollback authority on every note/task row mutation, including ABA.

    Ownership is installed lazily in the importing transaction. Missing triggers
    make old ownership unprovable (for example after a table rebuild), so it is
    discarded before the triggers are restored. INSERT also covers replacements
    when SQLite's recursive delete triggers are disabled.
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_IMPORT_ROW_OWNERSHIP_TABLE} ("
        "row_table TEXT NOT NULL, row_key TEXT NOT NULL, owner TEXT NOT NULL, "
        "PRIMARY KEY (row_table, row_key))"
    )
    for table in ("memory", "tasks"):
        triggers = [f"migration_{table}_owner_{event}" for event in ("insert", "update", "delete")]
        installed = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", triggers
            )
        }
        if installed != set(triggers):
            conn.execute(f"DELETE FROM {_IMPORT_ROW_OWNERSHIP_TABLE} WHERE row_table=?", (table,))
        for event, matching in (
            ("INSERT", "row_key=json_array(NEW.id)"),
            ("DELETE", "row_key=json_array(OLD.id)"),
            ("UPDATE", "row_key IN (json_array(OLD.id), json_array(NEW.id))"),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS migration_{table}_owner_{event.lower()} "
                f"BEFORE {event} ON {table} BEGIN "
                f"DELETE FROM {_IMPORT_ROW_OWNERSHIP_TABLE} "
                f"WHERE row_table='{table}' AND {matching}; END"
            )


def _owned_import_rows(conn, table: str, ids: list, org_id, owner: str | None) -> list:
    if owner is None:
        return []  # Legacy row-ID tokens cannot prove a current row incarnation.
    found = []
    for row_id in dict.fromkeys(ids):
        row = conn.execute(
            f"SELECT r.id FROM {table} r WHERE r.id=? AND r.org_id IS ? "
            f"AND EXISTS (SELECT 1 FROM {_IMPORT_ROW_OWNERSHIP_TABLE} o "
            "WHERE o.row_table=? AND o.row_key=json_array(r.id) AND o.owner=?)",
            (row_id, org_id, table, owner),
        ).fetchone()
        if row is not None:
            found.append(row["id"])
    return found


def _safe_task_rollback_rows(conn, ids: list) -> list:
    """A parent may cascade only into current, explicitly authorized children.

    Traverse the affected subtrees once, then propagate unsafe descendants up
    through that same set. UNION terminates even for a corrupt parent cycle;
    unrelated tasks are never scanned. The existing parent_id index bounds the
    descendant lookups rather than one full-tree walk per candidate parent.
    """
    if not ids:
        return []
    conn.execute("CREATE TEMP TABLE _ua_rollback_tasks(id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO _ua_rollback_tasks(id) VALUES (?)", ((i,) for i in ids))
    # These operational references are not part of migration manifests. Their
    # presence proves later activity, even when the task row itself is unchanged.
    # Batch matches across the affected set: tables without a task-ID index are
    # scanned once, never once per descendant. Indexed references use the set's IDs.
    external = " UNION ".join(
        f"SELECT {column} FROM {table} WHERE {column} IN (SELECT id FROM affected)"
        for table, column in _TASK_IMPORT_REFERENCES
    )
    safe = {
        row[0]
        for row in conn.execute(
            "WITH RECURSIVE affected(id,parent_id) AS ("
            "SELECT t.id,t.parent_id FROM tasks t JOIN _ua_rollback_tasks c ON c.id=t.id "
            "UNION SELECT t.id,t.parent_id FROM tasks t JOIN affected a ON t.parent_id=a.id"
            f"), referenced(id) AS ({external}), blocked(id,parent_id) AS ("
            "SELECT a.id,a.parent_id FROM affected a LEFT JOIN _ua_rollback_tasks c ON c.id=a.id "
            "WHERE c.id IS NULL OR a.id IN (SELECT id FROM referenced) UNION "
            "SELECT a.id,a.parent_id FROM affected a JOIN blocked b ON a.id=b.parent_id"
            ") SELECT c.id FROM _ua_rollback_tasks c "
            "WHERE NOT EXISTS (SELECT 1 FROM blocked b WHERE b.id=c.id)"
        )
    }
    return [row_id for row_id in ids if row_id in safe]


def _safe_memory_rollback_rows(conn, ids: list, token: dict, org_id, owner: str | None) -> list:
    """Do not cascade through graph data outside this receipt's explicit rows."""
    if not ids:
        return []
    conn.execute("CREATE TEMP TABLE _ua_rollback_memory(id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO _ua_rollback_memory VALUES (?)", ((note,) for note in ids))
    referenced = {
        row[0]
        for row in conn.execute(
            " UNION ".join(
                f"SELECT {column} FROM {table} "
                f"WHERE {column} IN (SELECT id FROM _ua_rollback_memory)"
                for table, column in _MEMORY_IMPORT_REFERENCES
            )
        )
    }
    conn.execute(
        "CREATE TEMP TABLE _ua_rollback_graph (graph_table TEXT, graph_key TEXT, "
        "PRIMARY KEY(graph_table,graph_key))"
    )
    for table, (_columns, keys, _refs) in _GRAPH_TABLES.items():
        conn.executemany(
            f"INSERT INTO _ua_rollback_graph VALUES (?,json_array({','.join('?' for _ in keys)}))",
            ((table, *(row[k] for k in keys)) for row in token.get(table, [])),
        )
    safe = []
    for note_id in ids:
        if note_id in referenced:
            continue
        external = False
        for table, (_columns, keys, refs) in _GRAPH_TABLES.items():
            key = "json_array(" + ",".join(f"g.{k}" for k in keys) + ")"
            attached = " OR ".join(f"g.{ref}=?" for ref in refs)
            if conn.execute(
                f"SELECT 1 FROM {table} g WHERE ({attached}) AND NOT (g.org_id IS ? "
                f"AND EXISTS (SELECT 1 FROM {_GRAPH_IMPORT_OWNERSHIP_TABLE} o "
                f"WHERE o.graph_table=? AND o.graph_key={key} AND o.owner=?) "
                "AND EXISTS (SELECT 1 FROM _ua_rollback_graph c "
                f"WHERE c.graph_table=? AND c.graph_key={key})) LIMIT 1",
                (*((note_id,) * len(refs)), org_id, table, owner, table),
            ).fetchone():
                external = True
                break
        if not external:
            safe.append(note_id)
    return safe


def _safe_structured_rollback_rows(conn, memory_ids: list, task_ids: list) -> tuple[list, list]:
    """Preserve references in known schema paths, including before graph backfill.

    An owned source note can disappear with its referenced row. If that source
    must survive, propagate preservation along its references (including cycles).
    This reads structured fields once, not once per candidate; arbitrary content
    and application-defined JSON payloads are deliberately outside this contract.
    """
    if not memory_ids and not task_ids:
        return memory_ids, task_ids
    notes, tasks = set(memory_ids), set(task_ids)
    edges: dict[str, set[tuple[str, str | int]]] = {}
    for row in conn.execute(
        "SELECT id,superseded_by,contradicts,evidence FROM memory "
        "WHERE superseded_by IS NOT NULL OR contradicts IS NOT NULL OR evidence IS NOT NULL"
    ):
        refs = [("memory", str(ref)) for ref in _json_list(row["contradicts"])]
        if row["superseded_by"]:
            refs.append(("memory", row["superseded_by"]))
        for raw in _json_list(row["evidence"]):
            kind, separator, ref = str(raw).partition(":")
            if separator and kind == "memory":
                refs.append((kind, ref))
            elif separator and kind == "task":
                try:
                    refs.append((kind, int(ref)))
                except ValueError:
                    pass
        relevant = {
            (kind, ref)
            for kind, ref in refs
            if (kind == "memory" and ref in notes) or (kind == "task" and ref in tasks)
        }
        if relevant:
            edges[row["id"]] = relevant
    blocked_notes, blocked_tasks = set(), set()
    # A comment's formal decision_id path names a note. Comments are later
    # operational data, so their task is itself excluded by the task planner.
    if notes:
        for row in conn.execute("SELECT refs FROM task_comments WHERE refs!='{}'"):
            try:
                refs = json.loads(row["refs"])
            except (TypeError, ValueError):
                continue
            if isinstance(refs, dict) and refs.get("decision_id") is not None:
                note = str(refs["decision_id"]).strip()
                if note in notes:
                    blocked_notes.add(note)
    pending = [source for source in edges if source not in notes] + list(blocked_notes)
    visited = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for kind, ref in edges.get(source, ()):
            if kind == "task":
                blocked_tasks.add(ref)
            elif ref not in blocked_notes:
                blocked_notes.add(ref)
                pending.append(ref)
    return (
        [note for note in memory_ids if note not in blocked_notes],
        [task for task in task_ids if task not in blocked_tasks],
    )


def _import_graph(
    conn, manifest: dict, resolved_memory: set, org_id, inserted_ids: dict, owner: str
) -> dict:
    """Insert graph rows only after every referenced note is safely resolved."""
    result = {}
    manifest_ids = {row["id"] for row in manifest.get("memory", [])}
    for table, (columns, keys, refs) in _GRAPH_TABLES.items():
        outcome = {"inserted": [], "unchanged": [], "held": []}
        for row in manifest.get(table, []):
            key = {c: row[c] for c in keys}
            # Conflicting/rejected manifest notes cannot acquire new graph rows.
            # A graph-only manifest may reference an existing note in this org.
            ready = all(
                row[ref] in resolved_memory
                or row[ref] not in manifest_ids
                and _local_row(conn, "memory", ("id",), row[ref], org_id) is not None
                for ref in refs
            )
            if not ready:
                outcome["held"].append({**key, "reason": "unresolved_memory"})
                continue
            where = " AND ".join(f"{c}=?" for c in keys)
            local = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE {where} AND {_TENANT_SQL}",
                (*(row[c] for c in keys), org_id),
            ).fetchone()
            if local is not None:
                if dict(local) == {c: row.get(c) for c in columns}:
                    outcome["unchanged"].append(key)
                else:
                    outcome["held"].append({**key, "reason": "conflicting_edit"})
            elif _try_insert(conn, table, columns, row, org_id):
                conn.execute(
                    f"INSERT INTO {_GRAPH_IMPORT_OWNERSHIP_TABLE} "
                    "(graph_table, graph_key, owner) VALUES "
                    f"(?, json_array({', '.join('?' for _ in keys)}), ?)",
                    (table, *(row[c] for c in keys), owner),
                )
                outcome["inserted"].append(key)
                inserted_ids[table].append(key)
            else:
                outcome["held"].append({**key, **_INSERT_REJECTED})
        result[table] = outcome
    return result


def _preserve_imported_graph(conn, manifest: dict, result: dict, org_id, owner: str) -> list:
    """Protect restored graphs from legacy backfill without advancing its cursor.

    Each import owns a marker reference, so overlapping imports can roll back
    independently. Rowid and creation time bind it to this note incarnation.
    """
    if not any(table in manifest for table in _GRAPH_TABLES):
        return []
    cursor_row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (GRAPH_BACKFILL_META_KEY,)
    ).fetchone()
    try:
        cursor = int(cursor_row["value"]) if cursor_row else 0
    except (TypeError, ValueError):
        cursor = 0
    protected = set(result["memory"]["inserted"])
    for table, (_columns, _keys, refs) in _GRAPH_TABLES.items():
        for key in result[table]["inserted"]:
            protected.update(key[ref] for ref in refs)
    changes = []
    for note_id in sorted(protected):
        note = conn.execute(
            f"SELECT rowid, created_at FROM memory WHERE id=? AND {_TENANT_SQL}", (note_id, org_id)
        ).fetchone()
        if note is None or note["rowid"] <= cursor:
            continue
        value = _graph_import_incarnation(note)
        key = GRAPH_IMPORT_META_PREFIX + note_id
        old = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        owners = _graph_import_owners(old["value"] if old else None, value)
        owners.add(owner)
        marker = json.dumps({"incarnation": value, "owners": sorted(owners)})
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, marker))
        changes.append({"note_id": note_id, "value": value, "owner": owner})
    return changes


def import_migration_manifest(manifest: dict, apply: bool = False) -> dict:
    """Reconcile a migration manifest (from export_migration_manifest) into this database.

    Idempotent and never destructive: a manifest row absent locally is a
    candidate insert; a manifest row that already exists locally with
    identical column values is a no-op; a manifest row that exists locally
    with *different* values is held for human review and never overwritten
    — the local row is left byte-for-byte unchanged (task #285's no-silent-
    reassign/overwrite gate applies to migration import exactly as it does
    to live task mutation). Task rows are inserted parent-before-child so
    the `parent_id REFERENCES tasks(id)` foreign key never trips; a row
    whose parent cannot be resolved (not local, not insertable this pass)
    is held rather than inserted with a dangling reference.

    Optional ``memory_entities`` and ``memory_links`` collections preserve the
    persisted graph verbatim. Their composite keys are reconciled after notes;
    missing, foreign or conflicting endpoints are held as unresolved_memory.
    Manifests predating graph exports may omit either collection.

    ``apply=False`` (default) is a dry-run preview: it attempts the same
    inserts inside one transaction and rolls it back, so it reports exactly
    what ``apply=True`` would do and persists nothing. ``apply=True``
    commits the safe inserts and returns a ``rollback_token`` containing their
    ids and a tenant-bound receipt. ``rollback_migration_import`` can undo rows
    still owned by this import without cascading into subsequent work.

    Tenant fence (task #1795): every lookup is scoped to the caller's
    ``edition.tenant_org_id()`` and every inserted row is stamped with it.
    A row that exists only in another tenant is treated as absent; its
    insert then fails on the global primary key and the row is held as
    ``insert_rejected``, the same reason any other integrity failure gets,
    so the result never reveals that tenant's row or its contents.
    """
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ToolError("manifest must be a manifest_version=1 migration manifest object")
    _validate_graph_rows(manifest)

    org_id = edition.tenant_org_id()
    conn = storage.connect()
    try:
        # One explicit transaction so the per-row savepoints nest inside it
        # (a bare SAVEPOINT would open, and its RELEASE commit, its own).
        # IMMEDIATE takes the write reservation before the first lookup: a
        # deferred BEGIN would read an older WAL snapshot, and a commit by
        # another connection before the first insert would then fail that
        # insert with SQLITE_BUSY_SNAPSHOT. Now a competing writer waits.
        conn.execute("BEGIN IMMEDIATE")
        if any(manifest.get(table) for table in _GRAPH_TABLES):
            _ensure_graph_import_ownership(conn)
        if manifest.get("memory") or manifest.get("tasks"):
            _ensure_import_row_ownership(conn)
        import_id = secrets.token_hex(16)
        result: dict[str, dict] = {}
        inserted_ids: dict[str, list] = {
            "tasks": [],
            "memory": [],
            **{t: [] for t in _GRAPH_TABLES},
        }

        # Memory rows have no relational ordering constraint: insert/skip/hold directly.
        memory_result = {"inserted": [], "unchanged": [], "held": []}
        for row in manifest.get("memory", []):
            local = _local_row(conn, "memory", _MANIFEST_MEMORY_COLUMNS, row["id"], org_id)
            if local is None:
                if _try_insert(conn, "memory", _MANIFEST_MEMORY_COLUMNS, row, org_id):
                    memory_result["inserted"].append(row["id"])
                    inserted_ids["memory"].append(row["id"])
                else:
                    memory_result["held"].append({"id": row["id"], **_INSERT_REJECTED})
            elif local == {c: row.get(c) for c in _MANIFEST_MEMORY_COLUMNS}:
                memory_result["unchanged"].append(row["id"])
            else:
                memory_result["held"].append({"id": row["id"], "reason": "conflicting_edit"})
        result["memory"] = memory_result
        resolved_memory = set(memory_result["inserted"]) | set(memory_result["unchanged"])
        result.update(
            _import_graph(conn, manifest, resolved_memory, org_id, inserted_ids, import_id)
        )

        # Task rows: resolve in dependency order (parent before child) so the
        # FK never fires. Iterate to a fixed point; whatever can never be
        # placed (missing parent) is held rather than force-inserted.
        pending = {row["id"]: row for row in manifest.get("tasks", [])}
        task_result = {"inserted": [], "unchanged": [], "held": []}
        # Keys present in resolved_ids mean "this id is confirmed to exist in
        # the target DB" (already present, or inserted earlier this call) —
        # deliberately NOT pre-seeded for pending/absent rows, so a parent
        # that hasn't been placed yet is never mistaken for one that has
        # (dict `in` checks key presence, not truthiness of the stored value).
        resolved_ids: set = set()
        for tid, row in list(pending.items()):
            local = _local_row(conn, "tasks", _MANIFEST_TASK_COLUMNS, tid, org_id)
            if local is not None:
                resolved_ids.add(tid)
                if local == {c: row.get(c) for c in _MANIFEST_TASK_COLUMNS}:
                    task_result["unchanged"].append(tid)
                else:
                    task_result["held"].append({"id": tid, "reason": "conflicting_edit"})
                del pending[tid]

        progressed = True
        while pending and progressed:
            progressed = False
            for tid, row in list(pending.items()):
                parent_id = row.get("parent_id")
                parent_ready = (
                    parent_id is None
                    or parent_id in resolved_ids
                    or parent_id not in pending
                    # Fenced: another tenant's task is never adopted as a parent.
                    and _local_row(conn, "tasks", ("id",), parent_id, org_id) is not None
                )
                if not parent_ready:
                    continue
                del pending[tid]
                progressed = True
                if not _try_insert(conn, "tasks", _MANIFEST_TASK_COLUMNS, row, org_id):
                    # Not resolved: a child of this row is then held as missing_parent.
                    task_result["held"].append({"id": tid, **_INSERT_REJECTED})
                    continue
                inserted_ids["tasks"].append(tid)
                task_result["inserted"].append(tid)
                resolved_ids.add(tid)

        for tid in pending:
            task_result["held"].append({"id": tid, "reason": "missing_parent"})
        result["tasks"] = task_result
        row_mutated = bool(inserted_ids["memory"] or inserted_ids["tasks"])
        for table in ("memory", "tasks"):
            if not inserted_ids[table]:
                continue
            conn.executemany(
                f"INSERT INTO {_IMPORT_ROW_OWNERSHIP_TABLE} "
                "(row_table,row_key,owner) "
                f"SELECT ?,json_array(id),? FROM {table} WHERE id=? AND org_id IS ?",
                ((table, import_id, row_id, org_id) for row_id in inserted_ids[table]),
            )
        markers = _preserve_imported_graph(conn, manifest, result, org_id, import_id)
        graph_mutated = bool(markers) or any(inserted_ids[table] for table in _GRAPH_TABLES)
        if graph_mutated or row_mutated:
            # A one-shot receipt prevents a replayed rollback from deleting rows
            # a later import inserted under the same composite keys.
            # Immutable membership survives native revisions and a caller
            # trimming token IDs. Even graph-only imports record an empty list
            # so legacy, unprovable membership can fail closed separately.
            receipt = json.dumps({"version": 1, "org_id": org_id, "memory": inserted_ids["memory"]})
            conn.execute(
                "INSERT INTO meta(key,value) VALUES (?,?)",
                (_GRAPH_IMPORT_RECEIPT_PREFIX + import_id, receipt),
            )

        if apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    result["applied"] = apply
    if apply:
        # Preserve legacy token/rollback response shapes when no graph was added.
        result["rollback_token"] = {
            table: ids for table, ids in inserted_ids.items() if table in ("tasks", "memory") or ids
        }
        if markers:
            result["rollback_token"]["memory_graph_backfill"] = markers
        if graph_mutated:
            result["rollback_token"]["memory_graph_import_id"] = import_id
        if row_mutated:
            result["rollback_token"]["migration_import_id"] = import_id
    return result


def rollback_migration_import(rollback_token: dict) -> dict:
    """Undo exactly one prior ``import_migration_manifest(apply=True)`` call.

    Deletes only recorded row ids still owned by this import. Any native
    update, deletion or replacement revokes that row's authority, including
    same-byte updates. A note with graph data outside this receipt is kept;
    task parents are kept if a descendant is unauthorized or has acquired
    operational references. This prevents foreign-key cascades into later work.

    Tenant fence (task #1795): only the caller's tenant's rows are deleted.
    An id belonging to another tenant is a no-op and the response is the
    same as for an id that never existed.

    Composite graph keys in a token are deleted before notes, including graph
    rows added to pre-existing notes. Legacy tokens may omit graph collections.
    Imports carry a tenant-bound, one-use receipt: replaying their token reports
    zero even if a subsequent import reused the same row ids or graph keys.
    Explicit graph deletes also require current ownership by that import. A
    later native edit or replacement revokes it; older tokens without durable
    row ownership cannot authorize deletion. Legacy row-ID-only tokens remain
    accepted but fail closed, returning zero for those unprovable rows.

    ``rolled_back`` counts the token's ids that were rows of the caller's
    tenant and were deleted by this call. Another tenant's row is never
    deleted, so a foreign id and a missing id both count 0 and the counts
    reveal nothing about other tenants. A replayed token reports 0.
    """
    if not isinstance(rollback_token, dict):
        raise ToolError("rollback_token must be the object returned by import_migration_manifest")
    _validate_graph_rows(rollback_token, rollback=True)
    markers = rollback_token.get("memory_graph_backfill", [])
    if not isinstance(markers, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("note_id"), str)
        or not isinstance(row.get("value"), str)
        or not isinstance(row.get("owner"), str)
        for row in markers
    ):
        raise ToolError("memory_graph_backfill must contain note_id, value and owner marker fields")
    import_id = rollback_token.get("memory_graph_import_id")
    if import_id is not None and (
        not isinstance(import_id, str) or re.fullmatch(r"[0-9a-f]{32}", import_id) is None
    ):
        raise ToolError("memory_graph_import_id must be the receipt returned by import")
    if import_id is None and (markers or any(rollback_token.get(t) for t in _GRAPH_TABLES)):
        raise ToolError("graph rollback_token requires memory_graph_import_id from import")
    if any(marker["owner"] != import_id for marker in markers):
        # Validate before connecting or consuming the one-use receipt. A copied
        # marker must not let one import release another import's backfill guard.
        raise ToolError("memory_graph_backfill owner must match memory_graph_import_id")
    row_import_id = rollback_token.get("migration_import_id")
    if row_import_id is not None and (
        not isinstance(row_import_id, str) or re.fullmatch(r"[0-9a-f]{32}", row_import_id) is None
    ):
        raise ToolError("migration_import_id must be the receipt returned by import")
    if row_import_id is not None and import_id is not None and row_import_id != import_id:
        raise ToolError("migration_import_id and memory_graph_import_id must match")
    task_ids = rollback_token.get("tasks", [])
    memory_ids = rollback_token.get("memory", [])
    if not isinstance(task_ids, list) or any(type(row_id) is not int for row_id in task_ids):
        raise ToolError("rollback tasks must be a list of integer row IDs")
    if not isinstance(memory_ids, list) or any(
        not isinstance(row_id, str) for row_id in memory_ids
    ):
        raise ToolError("rollback memory must be a list of string row IDs")
    org_id = edition.tenant_org_id()
    conn = storage.connect()
    try:
        # Read-then-write: take the write reservation first (see the import).
        conn.execute("BEGIN IMMEDIATE")
        if any(rollback_token.get(table) for table in _GRAPH_TABLES):
            _ensure_graph_import_ownership(conn)
        if row_import_id is not None:
            _ensure_import_row_ownership(conn)
            _ensure_graph_import_ownership(conn)
        receipt = row_import_id or import_id
        imported_notes = None  # Legacy scalar receipts cannot prove original membership.
        if receipt is not None:
            saved = conn.execute(
                "SELECT value FROM meta WHERE key=?", (_GRAPH_IMPORT_RECEIPT_PREFIX + receipt,)
            ).fetchone()
            try:
                payload = json.loads(saved["value"]) if saved else None
            except (TypeError, ValueError):
                payload = None
            valid = saved is not None and saved["value"] == json.dumps(org_id)
            if isinstance(payload, dict):
                notes = payload.get("memory")
                valid = (
                    payload.get("version") == 1
                    and "org_id" in payload
                    and payload.get("org_id") == org_id
                    and isinstance(notes, list)
                    and all(isinstance(note, str) for note in notes)
                )
                if valid:
                    imported_notes = set(notes)
            if not valid:
                conn.rollback()
                return {
                    "rolled_back": {
                        "tasks": 0,
                        "memory": 0,
                        **{table: 0 for table in _GRAPH_TABLES if table in rollback_token},
                    }
                }
            conn.execute(
                "DELETE FROM meta WHERE key=? AND value=?",
                (_GRAPH_IMPORT_RECEIPT_PREFIX + receipt, saved["value"]),
            )
        # Plan every cascade while the IMMEDIATE reservation excludes writers,
        # before explicit graph deletes revoke their own ownership records.
        memory_rows = _owned_import_rows(conn, "memory", memory_ids, org_id, row_import_id)
        memory_rows = _safe_memory_rollback_rows(
            conn, memory_rows, rollback_token, org_id, row_import_id
        )
        memory_rows, task_rows = _safe_structured_rollback_rows(
            conn, memory_rows, _owned_import_rows(conn, "tasks", task_ids, org_id, row_import_id)
        )
        task_rows = _safe_task_rollback_rows(conn, task_rows)
        deleted_notes = set(memory_rows)
        # Remove explicit graph inserts first, including rows added to notes
        # that already existed before this import. Note deletion then cascades.
        deleted = {}
        for table, (_columns, keys, _refs) in _GRAPH_TABLES.items():
            if table not in rollback_token:
                continue
            count = 0
            where = " AND ".join(f"{c}=?" for c in keys)
            for row in rollback_token.get(table, []):
                count += conn.execute(
                    f"DELETE FROM {table} WHERE {where} AND {_TENANT_SQL} "
                    f"AND EXISTS (SELECT 1 FROM {_GRAPH_IMPORT_OWNERSHIP_TABLE} "
                    f"WHERE graph_table=? AND graph_key=json_array({', '.join('?' for _ in keys)}) "
                    "AND owner=?)",
                    (*(row[c] for c in keys), org_id, table, *(row[c] for c in keys), import_id),
                ).rowcount
            deleted[table] = count
        for marker in markers:
            # A retained imported note may carry later native graph data. Keep
            # its one-startup backfill protection rather than erase that data
            # indirectly when this receipt is consumed.
            if marker["note_id"] not in deleted_notes and (
                imported_notes is None or marker["note_id"] in imported_notes
            ):
                continue
            # Explicit graph deletes have completed. Any remaining entity or
            # incident link may belong to native code, another import, an old
            # unprovable generation or even an inconsistent legacy tenant row.
            # Reconciliation must not overwrite it after the last guard leaves.
            # Deliberately do not tenant-filter this conservative presence check.
            if any(
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE "
                    + " OR ".join(f"{ref}=?" for ref in refs)
                    + " LIMIT 1",
                    (marker["note_id"],) * len(refs),
                ).fetchone()
                for table, (_columns, _keys, refs) in _GRAPH_TABLES.items()
            ):
                continue
            note = conn.execute(
                f"SELECT rowid, created_at FROM memory WHERE id=? AND {_TENANT_SQL}",
                (marker["note_id"], org_id),
            ).fetchone()
            key = GRAPH_IMPORT_META_PREFIX + marker["note_id"]
            current = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if note is not None and _graph_import_incarnation(note) == marker["value"]:
                owners = _graph_import_owners(
                    current["value"] if current else None, marker["value"]
                )
                if marker["owner"] not in owners:
                    continue
                owners.remove(marker["owner"])
                if not owners:
                    conn.execute("DELETE FROM meta WHERE key=?", (key,))
                else:
                    value = json.dumps({"incarnation": marker["value"], "owners": sorted(owners)})
                    conn.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
        deleted.update(
            {
                "tasks": _delete_owned(conn, "tasks", task_rows, org_id),
                "memory": _delete_owned(conn, "memory", memory_rows, org_id),
            }
        )
        conn.commit()
    finally:
        conn.close()
    return {"rolled_back": deleted}


def _delete_owned(conn, table: str, ids: list, org_id) -> int:
    """Delete the caller's tenant's rows among ``ids``; return how many.

    Counted before deleting, so a task row removed by ``ON DELETE CASCADE``
    from a parent earlier in the same token still counts once.
    """
    owned = [
        row_id
        for row_id in dict.fromkeys(ids)
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND {_TENANT_SQL}", (row_id, org_id)
        ).fetchone()
    ]
    for row_id in owned:
        if table == "memory":
            storage.hard_delete_memory(conn, row_id, org_id=org_id)
        else:
            conn.execute(f"DELETE FROM {table} WHERE id=? AND {_TENANT_SQL}", (row_id, org_id))
    return len(owned)


def _split_tags(text: str) -> tuple[str, list[str]]:
    """Strip trailing '#tag' tokens off a journal entry, returning (text, tags)."""
    words = text.split(" ")
    tags: list[str] = []
    while words and words[-1].startswith("#") and len(words[-1]) > 1:
        tags.append(words.pop()[1:])
    tags.reverse()
    return " ".join(words).rstrip(), tags


def _journal_entries_in(
    logs_dir: Path, machine: str, agent: str | None, since_date: datetime.date | None
) -> list[dict]:
    """Parse every daily note directly inside `logs_dir`, tagged with `machine`."""
    entries: list[dict] = []
    if not logs_dir.is_dir():
        return entries
    for f in sorted(logs_dir.iterdir()):
        m = DAILY_NOTE_RE.match(f.name)
        if m is None or not f.is_file():
            continue
        try:
            day = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if since_date is not None and day < since_date:
            continue
        for line in f.read_text(errors="replace").splitlines():
            lm = JOURNAL_LINE_RE.match(line)
            if lm is None:
                continue
            time_, label, text = lm.groups()
            if agent is not None and label != agent:
                continue
            text, tags = _split_tags(text)
            entries.append(
                {
                    "date": m.group(1),
                    "time": time_,
                    "agent": label,
                    "machine": machine,
                    "text": text,
                    "tags": tags,
                }
            )
    return entries


def export_journal(
    agent: str | None = None, since: str | None = None, write_to_vault: bool = True
) -> dict:
    """Export vault journal entries as jsonl, from every device namespace.

    Reads the desktop root (`Logs/YYYY-MM-DD.md`) *and* every subdirectory
    under `Logs/` (e.g. any historical `Logs/Laptop/YYYY-MM-DD.md`), so a
    root-only scan doesn't silently drop entries from another namespace.
    Each row carries `machine` ("desktop"/other), matching
    `vault_briefing_digest`.

    Parses '- **HH:MM** [Agent_Label] text #tags' lines; `agent` filters by
    exact label, `since` (ISO date) by daily-note date. With write_to_vault
    the file lands in this device's inbox exports directory and the result is
    {path, rows, bytes}; otherwise {content, rows}. Inline cap 2 MB; vault 32 MB.
    """
    since_date = _parse_since(since)
    logs_root = resolve_in_vault("Logs")
    entries = _journal_entries_in(logs_root, "desktop", agent, since_date)
    if logs_root.is_dir():
        for sub in sorted(p for p in logs_root.iterdir() if p.is_dir()):
            entries.extend(_journal_entries_in(sub, sub.name.lower(), agent, since_date))
    entries.sort(key=lambda e: (e["date"], e["time"], e["machine"]))
    content = "".join(json.dumps(e) + "\n" for e in entries)
    return _deliver("journal", "jsonl", content, len(entries), write_to_vault)


_EXPORT_ACTIONS = {
    "tasks": export_tasks,
    "memory": export_memory,
    "journal": export_journal,
    "manifest": export_migration_manifest,
    "compare": compare_migration_manifests,
    "import": import_migration_manifest,
    "rollback": rollback_migration_import,
}


def export_data(action: str, params: dict | None = None) -> dict:
    """Dispatch export and migration operations through one compatibility API.

    ``action`` is one of tasks, memory, journal, manifest, compare, import, or
    rollback. Authorization is checked against the underlying legacy operation,
    so consolidating the entry point cannot grant import or rollback access that
    the active profile does not already have.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = _EXPORT_ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_EXPORT_ACTIONS)}")
    if not profiles.consolidated_action_is_allowed("export_data", fn.__name__):
        raise ToolError(f"export action {action!r} is not exposed by the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for export action {action!r}: {exc}") from exc
    # Preserve the underlying policy chokepoint for migration/import actions.
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
