"""Periodic audit-ledger chain-head re-anchor (task #768).

Writes today's SHA-256 chain head to a dated vault note. Does not mutate
``event_log`` (no prev_hash rewrite, no backfill). The note is an external
witness so a wholesale SQLite rewrite is detectable against yesterday's file.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from mcp.server.mcpserver.exceptions import ToolError

from .. import edition, events, storage
from ..note_lock import note_bytes_are_locked, note_write_lock
from ..safety import LEDGER_ANCHOR_DIR, VaultLockedError, resolve_in_vault
from .safe_read import NotRegularFileError, read_regular_file, write_regular_file

ANCHOR_DIR = LEDGER_ANCHOR_DIR

_HEAD_HASH_LINE_RE = re.compile(r"^- head_hash:", re.MULTILINE)


def _witness_prefix(stamp: str, *, locked: bool = True) -> str:
    """The frontmatter and heading every witness this module creates for ``stamp`` opens with.

    ``locked=False`` is the legacy opening the first re-anchor (#768) wrote
    before the template gained ``locked: true``; such witnesses may still sit in
    the vault and stay this writer's (see ``_has_witness_shape``).
    """
    lock_line = "locked: true\n" if locked else ""
    return (
        "---\n"
        "type: audit-ledger-head\n"
        f"{lock_line}"
        f"date: {stamp}\n"
        "---\n\n"
        f"# Audit ledger chain head {stamp}\n\n"
    )


def _has_witness_shape(raw: bytes, stamp: str) -> bool:
    """True if ``raw`` has the exact shape of the witness this module writes for ``stamp``.

    The whole opening this module writes (type, ``locked: true``, the day's
    ``date``, the heading) followed by a ``- head_hash:`` line, compared after
    dropping a UTF-8 BOM and folding CRLF/CR to LF, so a sync or editor that
    rewrites line endings does not turn the day's witness into a refusal. The
    legacy opening without ``locked: true`` (``_witness_prefix(locked=False)``)
    is accepted too, so a witness filed before the lock line was added keeps its
    append-only contract (#2094).

    This is not proof of authorship, and it does not need to be: the anchor
    directory is reserved (``safety.resolve_in_vault`` refuses writes there
    without ``for_ledger_anchor``, which only this module passes, so no hub tool
    can plant a note there, ``vault_write(force=True)`` included). The only
    other way in is direct filesystem access, which could forge any shape and
    rewrite any witness anyway. The check exists so an operator's own locked
    note at the day's path, or a stray type marker on one, is refused rather
    than appended to.
    """
    text = raw.decode("utf-8", errors="replace").removeprefix("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for prefix in (_witness_prefix(stamp), _witness_prefix(stamp, locked=False)):
        if text.startswith(prefix) and _HEAD_HASH_LINE_RE.search(text, len(prefix)):
            return True
    return False


def _day(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError(f"date must be YYYY-MM-DD, got {value!r}") from exc


def _snapshot_fields(report: dict, *, anchored_at: str) -> str:
    body = (
        f"- status: {report['status']}\n"
        f"- verified: {str(report['verified']).lower()}\n"
        f"- total_verified: {report['total_verified']}\n"
        f"- head_hash: {report.get('head_hash') or ''}\n"
        f"- head_ulid: {report.get('head_ulid') or ''}\n"
        f"- anchored_at: {anchored_at}\n"
    )
    if report.get("tampered_at"):
        body += f"- tampered_at: {json.dumps(report['tampered_at'], sort_keys=True)}\n"
    return body


def _snapshot_note(report: dict, *, stamp: str, anchored_at: str) -> str:
    return _witness_prefix(stamp) + _snapshot_fields(report, anchored_at=anchored_at)


def _snapshot_block(report: dict, *, stamp: str, anchored_at: str, rerun: bool) -> str:
    heading = f"# Audit ledger chain head {stamp}"
    if rerun:
        heading += " (re-anchor)"
    return "\n---\n\n" + heading + "\n\n" + _snapshot_fields(report, anchored_at=anchored_at)


def _latest_head_hash(text: str) -> str | None:
    latest = None
    for line in text.splitlines():
        if line.startswith("- head_hash:"):
            latest = line.split(":", 1)[1].strip()
    return latest


def reanchor_chain_head(*, day: str | None = None) -> dict:
    """Verify the chain and snapshot its head into ``Logs/audit-ledger-heads/YYYY-MM-DD.md``.

    The dated note is an immutable external witness. A same-head re-run is a
    no-op. A later head is appended as a new block; the original snapshot is
    never truncated. The exists check, the read of the prior snapshot and the
    write all run under the per-note lock the hub's other note writers take
    (#2086), so a snapshot filed while this one waited is appended to, never
    replaced. The chain is verified before the lock is taken.

    ``locked: true`` (#2091): every witness this writer creates carries it, as
    the marker that no other writer may touch the note. For a note with the
    exact witness shape this writer creates for the day (see
    ``_has_witness_shape``; a BOM or CRLF line endings are tolerated, and so is
    the legacy unlocked opening) the lock and this writer's contract agree,
    append-only, so a re-run still appends or reports unchanged. Any other note
    at the day's path, locked or not (#2094), is refused rather than appended
    to or diverted to a sibling: appending would vouch for content this writer
    never wrote, and the witness path is fixed by date and read back by
    ``anchor.VaultAnchorBackend``, so a sibling would be a witness nothing
    checks. A locked one raises ``VaultLockedError``, an unlocked one a plain
    ``ToolError``. The refusal fails the run, and the daily re-anchor job with
    it, so it is seen.
    """
    edition.require_feature("audit_reanchor")
    storage.init_db()
    report = events.verify_event_chain()
    stamp = _day(day)
    anchored_at = datetime.now(UTC).isoformat(timespec="seconds")

    # The anchor dir is reserved: only this writer may pass for_ledger_anchor.
    path = resolve_in_vault(f"{ANCHOR_DIR}/{stamp}.md", for_write=True, for_ledger_anchor=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    current_hash = report.get("head_hash") or ""
    not_regular = ToolError(
        f"ledger anchor {ANCHOR_DIR}/{stamp}.md is not a regular file (or cannot be "
        "read), so it was not touched: a FIFO or device there would block the "
        "re-anchor. Move it aside, then rerun the re-anchor for this day"
    )
    with note_write_lock(path):
        # Read and write only a regular file, without blocking (#2212).
        if path.exists():
            existing = read_regular_file(path)
            if existing is None:
                raise not_regular
            if not _has_witness_shape(existing, stamp):
                if note_bytes_are_locked(existing):
                    raise VaultLockedError(
                        f"ledger anchor {ANCHOR_DIR}/{stamp}.md is locked (frontmatter "
                        "`locked: true`) and is not a witness this re-anchor wrote; it was "
                        "not touched. Move it aside, then rerun the re-anchor for this day"
                    )
                raise ToolError(
                    f"ledger anchor {ANCHOR_DIR}/{stamp}.md exists but is not a witness "
                    "this re-anchor wrote, so it was not touched: appending would vouch "
                    "for content the re-anchor never wrote. Move it aside, then rerun "
                    "the re-anchor for this day"
                )
            try:
                existing_text = existing.decode("utf-8")
            except UnicodeDecodeError as exc:
                # The opening matched (it is checked with errors="replace"), but
                # bytes further down are not UTF-8: refuse rather than read a head
                # hash out of a mangled witness or append to it (#2094).
                raise ToolError(
                    f"ledger anchor {ANCHOR_DIR}/{stamp}.md is not valid UTF-8 (byte "
                    f"{exc.start}), so its recorded head hashes cannot be read; it was "
                    "not touched. Move it aside (or restore it from vault history), then "
                    "rerun the re-anchor for this day"
                ) from exc
            latest = _latest_head_hash(existing_text)
            if latest == current_hash:
                return {
                    **report,
                    "anchor_path": f"{ANCHOR_DIR}/{stamp}.md",
                    "anchor_bytes": len(existing),
                    "anchored_at": anchored_at,
                    "anchor_action": "unchanged",
                }
            # Prior snapshot is an external witness — append, never truncate.
            block = _snapshot_block(report, stamp=stamp, anchored_at=anchored_at, rerun=True)
            encoded = existing + block.encode()
            try:
                write_regular_file(path, encoded)
            except NotRegularFileError as exc:
                raise not_regular from exc
            action = "appended"
        else:
            encoded = _snapshot_note(report, stamp=stamp, anchored_at=anchored_at).encode()
            try:
                write_regular_file(path, encoded)
            except NotRegularFileError as exc:
                raise not_regular from exc
            action = "created"
    return {
        **report,
        "anchor_path": f"{ANCHOR_DIR}/{stamp}.md",
        "anchor_bytes": len(encoded),
        "anchored_at": anchored_at,
        "anchor_action": action,
    }


def main() -> int:
    result = reanchor_chain_head()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
