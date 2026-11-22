"""Synchronization logic to dump database state to the Obsidian vault.

State snapshots are written through a debounced background scheduler so a burst
of quick sequential mutations collapses into a single write (see
``schedule_sync_to_vault``). ``sync_state_to_vault`` remains the synchronous
write primitive and the flush target; the atomic temp-file+rename guarantee and
the ``UA_STATE_SYNC`` off-switch are preserved on both paths.
"""

import atexit
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import config, storage
from .safety import resolve_in_vault

logger = logging.getLogger(__name__)

# --- Debounced scheduler state -------------------------------------------------
#
# A single threading.Timer is (re)armed on every schedule call; it is cancelled
# and replaced when another mutation lands within the debounce window, so N rapid
# mutations coalesce into one snapshot ~debounce seconds after the last one.
# All state below is guarded by ``_debounce_lock``. ``_scheduled_gen`` is a
# generation token: a timer that has already fired but is still blocked on the
# lock (raced by a newer schedule/flush) sees a stale gen and bails, so it never
# clobbers a freshly-armed timer or double-writes.
_debounce_lock = threading.Lock()
_debounce_timer: threading.Timer | None = None
_pending_snapshot = False
_scheduled_gen = 0
_atexit_registered = False
# Serializes the actual snapshot writes and makes flush_pending_sync a true
# barrier. Both the timer callback and flush acquire it BEFORE releasing
# _debounce_lock (always debounce -> write order, never the reverse): from the
# instant a writer consumes _pending_snapshot and becomes invisible to the
# debounce state, it is already visible through this lock. Without that
# handover there is a gap between the two locks where a concurrent flush sees
# nothing pending, finds the write lock free, and returns while the write is
# still in flight — the residual race behind the test_sync flake that
# survived the first _write_lock fix.
_write_lock = threading.Lock()


def _escape_backticks(text: str) -> str:
    """Escape backticks to prevent markdown code fence escapes/injections."""
    if not text:
        return ""
    return text.replace("`", "\\`")


# Obsidian's renderer OOMs (V8 heap exhaustion -> renderer crash) on md files
# past roughly 2MB. The .md state mirrors are human-readable fallbacks; the
# .txt JSON mirrors are the machine-readable source of truth and stay full.
MD_MIRROR_CAP_BYTES = 1_500_000


def _cap_md(text: str, full_mirror: str | None = None) -> str:
    """Cap an .md snapshot below Obsidian's OOM threshold, keeping its leading entries.

    The memory snapshot lists the most recently updated note first; the tasks
    snapshot lists its directory table before the per-task detail. A capped
    file keeps its frontmatter intact and says it is incomplete right after
    it and again at the end, naming ``full_mirror`` (the uncapped .txt JSON
    mirror), so a reader does not mistake it for the whole state (task #1920).
    """
    if len(text.encode()) <= MD_MIRROR_CAP_BYTES:
        return text
    where = f"`{full_mirror}`" if full_mirror else "the .txt JSON mirror"
    notice = (
        f"**[Truncated by unified-agent: {MD_MIRROR_CAP_BYTES // 1000}KB cap. This file is "
        f"incomplete; the full snapshot is {where} (JSON).]**"
    )
    lines = text.split("\n")
    head_end = 0
    if lines[0] == "---" and "---" in lines[1:]:
        head_end = lines.index("---", 1) + 1
    head = [*lines[:head_end], "", notice]
    tail = ["", notice]
    budget = MD_MIRROR_CAP_BYTES - sum(len(line.encode()) + 1 for line in head + tail)
    kept, size = [], 0
    for line in lines[head_end:]:
        size += len(line.encode()) + 1
        if size > budget:
            break
        kept.append(line)
    return "\n".join(head + kept + tail)


# A temp file older than this cannot belong to a live atomic write: that write
# is one fdopen/write/replace, measured in milliseconds. Anything more recent
# may be another process's in-flight write, and removing that would break a
# working writer rather than clean up after a dead one.
_STALE_TEMP_AGE_S = 3600.0


def _sweep_stale_temps(path: Path) -> None:
    """Remove orphaned temp files left by interrupted writes of `path`.

    `_write_atomic` cleans up its own temp file on exception, but nothing
    survives a SIGKILL between mkstemp and os.replace — so a process killed
    mid-snapshot leaves a partial file behind, and nothing ever collects it.
    Four such artifacts (~595 KB, the oldest nine days old) were found in the
    live vault on 2026-07-27 (task #172).

    Deliberately narrow, per that task's own boundary: only siblings carrying
    this exact derived filename's temp prefix, and only ones old enough to be
    certainly dead. Best-effort — housekeeping must never fail the snapshot it
    precedes.
    """
    try:
        cutoff = time.time() - _STALE_TEMP_AGE_S
        for leftover in path.parent.glob(f"{path.name}.tmp*"):
            try:
                if leftover.is_file() and leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
                    logger.info("removed stale sync temp file %s", leftover.name)
            except OSError:
                continue
    except Exception:  # pragma: no cover - never let housekeeping break a write
        logger.debug("stale-temp sweep failed for %s", path, exc_info=True)


def _write_atomic(path: Path, content: str) -> None:
    """Atomic write via temporary file and rename to prevent concurrent torn reads."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temps(path)
    fd, temp_path_str = tempfile.mkstemp(dir=str(parent), prefix=path.name + ".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path_str, str(path))
    except Exception:
        if os.path.exists(temp_path_str):
            try:
                os.remove(temp_path_str)
            except Exception:
                pass
        raise


def _ensure_atexit_registered() -> None:
    """Flush any pending snapshot on interpreter shutdown (idempotent)."""
    global _atexit_registered
    if _atexit_registered:
        return
    with _debounce_lock:
        if _atexit_registered:
            return
        atexit.register(flush_pending_sync)
        _atexit_registered = True


def _run_debounced_snapshot(gen: int) -> None:
    """Timer callback: write the snapshot unless a newer schedule/flush superseded us."""
    global _debounce_timer, _pending_snapshot
    with _debounce_lock:
        if gen != _scheduled_gen:
            # A newer schedule (or a flush) took over; this timer is stale.
            return
        _debounce_timer = None
        if not _pending_snapshot:
            return
        _pending_snapshot = False
        # Take the write lock while still holding the debounce lock: consuming
        # the flag and becoming write-lock-visible must be atomic, or a flush
        # slipping between the two lock acquisitions returns while our write
        # is still in flight (see _write_lock comment).
        _write_lock.acquire()
    try:
        # The (potentially slow) write runs outside the debounce lock.
        sync_state_to_vault()
    finally:
        _write_lock.release()


def schedule_sync_to_vault() -> None:
    """Coalesce a vault snapshot onto a debounced background timer.

    Non-blocking: returns immediately after (re)arming the timer. A burst of
    rapid mutations collapses into a single write ``UA_STATE_SYNC_DEBOUNCE``
    seconds after the last call. This is the default runtime path for mutation
    hooks. When state sync is disabled the call is a no-op; when the debounce
    window is non-positive it falls back to a synchronous write for determinism.
    """
    if not config.state_sync_enabled():
        return
    debounce = config.state_sync_debounce_seconds()
    if debounce <= 0:
        sync_state_to_vault()
        return

    global _debounce_timer, _pending_snapshot, _scheduled_gen
    with _debounce_lock:
        _pending_snapshot = True
        if _debounce_timer is not None:
            _debounce_timer.cancel()
        _scheduled_gen += 1
        gen = _scheduled_gen
        _debounce_timer = threading.Timer(debounce, _run_debounced_snapshot, args=(gen,))
        _debounce_timer.daemon = True
        _debounce_timer.start()
    _ensure_atexit_registered()


def flush_pending_sync() -> None:
    """Synchronously write any pending debounced snapshot, cancelling the timer.

    Safe to call when nothing is pending (no-op). Used by tests and atexit so a
    scheduled-but-unfired snapshot is never lost.
    """
    global _debounce_timer, _pending_snapshot, _scheduled_gen
    with _debounce_lock:
        if _debounce_timer is not None:
            _debounce_timer.cancel()
            _debounce_timer = None
        # Advance the generation so any timer that already fired and is waiting
        # on the lock recognises itself as stale and bails.
        _scheduled_gen += 1
        should_write = _pending_snapshot
        _pending_snapshot = False
        # Same atomic handover as the timer callback (see _write_lock comment):
        # taking the write lock before releasing the debounce lock — even when
        # there is nothing to write — makes flush a barrier against both
        # in-flight timer writes and concurrent flushes. On return, no
        # snapshot consumed before this call can land afterwards.
        _write_lock.acquire()
    try:
        if should_write:
            sync_state_to_vault()
    finally:
        _write_lock.release()


def sync_state_now() -> None:
    """Immediate snapshot, serialized against debounced/flushed writes.

    Entry point for out-of-band writers (e.g. the init_db startup sync):
    holding the write lock keeps this write from interleaving with a
    concurrent timer or flush write to the same files.
    """
    with _write_lock:
        sync_state_to_vault()


def sync_on_startup() -> None:
    """Background post-init hook: one state snapshot after ``init_db``.

    Registered by ``post_init``; it lived in storage until task #1720.
    Best-effort, like every other startup hook: a failed snapshot never
    surfaces from a daemon thread.
    """
    try:
        sync_state_now()
    except Exception:
        pass


def sync_state_to_vault() -> None:
    """Export the memory and tasks tables to JSON and Markdown files inside the vault.

    Synchronous write primitive. Runs on startup and as the flush/debounce target
    of :func:`schedule_sync_to_vault`; also callable directly when an immediate,
    deterministic snapshot is required.
    """
    if not config.state_sync_enabled():
        logger.debug("State sync disabled by environment. Skipping state sync.")
        return
    try:
        # Check if vault path is configured
        root = config.vault_root()
        if not root or not root.exists():
            logger.debug("Vault path not set or directory does not exist. Skipping state sync.")
            return

        conn = storage.connect()
        try:
            # 1. Fetch memory notes (excluding indexes)
            mem_rows = conn.execute(
                "SELECT id, title, content, tags, author, updated_by, created_at, updated_at "
                "FROM memory WHERE id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%' "
                "ORDER BY updated_at DESC"
            ).fetchall()
            memories = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "tags": json.loads(r["tags"]) if r["tags"].startswith("[") else r["tags"],
                    "author": r["author"],
                    "updated_by": r["updated_by"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in mem_rows
            ]

            # 2. Fetch tasks
            task_rows = conn.execute(
                "SELECT id, title, notes, status, priority, parent_id, created_at, updated_at, completed_at "
                "FROM tasks ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, created_at"
            ).fetchall()
            tasks = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "notes": r["notes"],
                    "status": r["status"],
                    "priority": r["priority"],
                    "parent_id": r["parent_id"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "completed_at": r["completed_at"],
                }
                for r in task_rows
            ]
        finally:
            conn.close()

        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        sync_dir = config.state_sync_dir()

        # 3. Write JSON snapshots to .txt files (since resolve_in_vault limits writes to .txt/.md)
        memory_txt_path = resolve_in_vault(
            f"{sync_dir}/{config.state_mirror_name('state_memory', '.txt')}", for_write=True
        )
        _write_atomic(memory_txt_path, json.dumps(memories, indent=2))

        tasks_txt_path = resolve_in_vault(
            f"{sync_dir}/{config.state_mirror_name('state_tasks', '.txt')}", for_write=True
        )
        _write_atomic(tasks_txt_path, json.dumps(tasks, indent=2))

        # 4. Generate & write Markdown memory snapshot
        mem_md = [
            "---",
            "type: snapshot",
            f"generated: {timestamp}",
            "tags: [snapshot, memory, fallback]",
            "---",
            "",
            "# Offline Memory Snapshot",
            f"*Generated: {timestamp} (UTC)*",
            "",
            "This file is a read-only offline backup of the shared agent memory.",
            "It is automatically updated on every write operation.",
            "",
        ]
        for m in memories:
            tags_str = ", ".join(m["tags"]) if isinstance(m["tags"], list) else str(m["tags"])
            mem_md.extend(
                [
                    f"## {m['title'] or 'Untitled'} (ID: `memory:{m['id']}`)",
                    f"- **Tags**: `{tags_str}`",
                    f"- **Author**: `{m['author']}` | **Updated By**: `{m['updated_by']}`",
                    f"- **Created**: `{m['created_at']}` | **Updated**: `{m['updated_at']}`",
                    "",
                    _escape_backticks(m["content"]),
                    "",
                    "---",
                    "",
                ]
            )

        memory_md_path = resolve_in_vault(
            f"{sync_dir}/{config.state_mirror_name('state_memory', '.md')}", for_write=True
        )
        # ponytail: Obsidian's renderer OOMs on md files past ~2MB (a 3MB file ->
        # 3.8GB heap -> renderer crash on vault load). The .md snapshot is a
        # human-readable fallback only — state_verify and offline recovery read
        # the .txt JSON mirror, which stays full. Cap the .md at 1.5MB.
        _write_atomic(
            memory_md_path,
            _cap_md("\n".join(mem_md), config.state_mirror_name("state_memory", ".txt")),
        )

        # 5. Generate & write Markdown tasks snapshot
        task_md = [
            "---",
            "type: snapshot",
            f"generated: {timestamp}",
            "tags: [snapshot, tasks, fallback]",
            "---",
            "",
            "# Offline Tasks Snapshot",
            f"*Generated: {timestamp} (UTC)*",
            "",
            "This file is a read-only offline backup of the shared task list.",
            "",
            "## Task Directory",
            "",
            "| ID | Title | Status | Priority | Assignee | Updated At |",
            "|---|---|---|---|---|---|",
        ]

        # Populate directory table
        for t in tasks:
            # Extract claimed-by
            assignee = "Unclaimed"
            if t["notes"]:
                m = re.search(r"claimed-by:\s*([^\n\r@]+)", t["notes"], re.IGNORECASE)
                if m:
                    assignee = m.group(1).strip()

            task_md.append(
                f"| `{t['id']}` | {t['title']} | `{t['status']}` | `{t['priority']}` | `{assignee}` | `{t['updated_at']}` |"
            )

        task_md.extend(["", "## Detailed Task List", ""])

        for t in tasks:
            task_md.extend(
                [
                    f"### Task #{t['id']}: {t['title']}",
                    f"- **Status**: `{t['status']}`",
                    f"- **Priority**: `{t['priority']}`",
                    f"- **Updated**: `{t['updated_at']}`"
                    + (f" | **Completed**: `{t['completed_at']}`" if t["completed_at"] else ""),
                ]
            )
            if t["notes"]:
                task_md.extend(["", "```text", _escape_backticks(t["notes"].strip()), "```"])
            task_md.extend(["", "---", ""])

        tasks_md_path = resolve_in_vault(
            f"{sync_dir}/{config.state_mirror_name('state_tasks', '.md')}", for_write=True
        )
        _write_atomic(
            tasks_md_path,
            _cap_md("\n".join(task_md), config.state_mirror_name("state_tasks", ".txt")),
        )
        logger.debug("State synchronization to vault completed successfully.")

    except ToolError as e:
        logger.warning(f"Unable to sync state to vault: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error during state sync: {e}")
