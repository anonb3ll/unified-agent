"""Obsidian vault tools: notes, wikilinks, and the agent journal.

Read anywhere in the vault; writes are confined to UA_VAULT_WRITE_DIRS
(default "00 - Inbox", Logs, and Wiki) so agents capture, journal, and maintain
the wiki layer while the operator's curated notes stay agent-read-only. All paths
route through safety.resolve_in_vault().
"""

import codecs
import contextlib
import datetime
import hashlib
import io
import json
import locale
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, profiles, storage, trust_presets, untrusted

# The note lock (#2086) and the `locked: true` verdict (#2091) live in core so
# the generated-note writers can share them; their names stay importable here.
from ..note_lock import (
    _FRONTMATTER_MAX_BYTES,
    _LOCK_READ_CEILING,
    _frontmatter_cache,
    _lock_verdict,
    note_bytes_are_locked,
    note_is_locked,
    note_write_lock,
    note_write_locks,
)
from ..note_lock import vault_rel as _vault_rel
from ..safety import (
    VAULT_FRONTMATTER_RE,
    VAULT_LOCK_HEAD_BYTES,
    VaultLockedError,
    resolve_in_vault,
)
from ..tool_errors import OutcomeUnknownError

# [[target]], [[target|alias]], [[target#heading]], and [[target\|alias]] —
# the trailing backslash is Obsidian's required escape for an aliased link
# inside a markdown table cell, where a bare | would end the cell. Without the
# optional \\? the capture keeps that backslash and the target never resolves,
# so every table-aliased link reads as a dangling one.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#\n]+?)\\?(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# ![[target]] transclusion — same shape as WIKILINK_RE with the embed marker.
# Group 1 is the note path, with any #section / |alias dropped.
TRANSCLUSION_RE = re.compile(r"!\[\[([^\[\]|#\n]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# Leading YAML frontmatter block: ---\n...\n---. Parsed cheaply with regexes
# (no YAML dependency) since the only thing vault tools care about is a
# handful of well-known scalar keys (here: `locked`). Defined in safety so the
# cloud resolver applies the same lock semantics as every other vault path.
_FRONTMATTER_RE = VAULT_FRONTMATTER_RE

# Cross-machine briefing digest (Roadmap Phase 1.2): identity used to dedup a
# journal entry that both machines logged (e.g. a synced PR-merge note). PR
# number outranks a bare task-id since a PR mention is the more specific claim.
_PR_URL_RX = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
_TASK_REF_RX = re.compile(r"#(\d+)\b")

# Steward-inbox lifecycle markers. A marker is evidence that a capture was
# handled, not evidence that its derived Wiki page is authoritative.
_PROCESSED_MARKER_RE = re.compile(r"(?m)^\s*processed\s*→\s*(?P<target>.+?)\s*$")
_SUPERSEDED_MARKER_RE = re.compile(
    r"(?m)^\s*superseded(?:\s*[-_]\s*by)?\s*:\s*(?P<target>.+?)\s*$", re.I
)
_STATUS_RE = re.compile(r"^status:\s*['\"]?(?P<value>[^'\"\s#]+)", re.I | re.M)
_SHIPPED_RE = re.compile(
    r"^(?:implementation(?:_status)?|shipped):\s*['\"]?(?:shipped|true)['\"]?\s*$",
    re.I | re.M,
)


# Note revisions (task #2111): ``sha256:<hex>`` of the note's raw bytes on disk.
# ``vault_read`` returns one and ``vault_write(expected_revision=...)`` refuses a
# write when the note no longer matches it. ``REVISION_ABSENT`` names a note
# that does not exist, so a create-only write can say "must not exist yet".
REVISION_ABSENT = "absent"
_REVISION_PREFIX = "sha256:"
_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")


class VaultRevisionConflict(ToolError):
    """A vault write was refused because the note changed since the caller's read."""


def _revision_of_bytes(data: bytes) -> str:
    return _REVISION_PREFIX + hashlib.sha256(data).hexdigest()


def _note_revision(p: Path) -> str:
    """Current revision of the note at ``p``, streamed; ``REVISION_ABSENT`` if missing."""
    if not p.is_file():
        return REVISION_ABSENT
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return _REVISION_PREFIX + digest.hexdigest()


def _validated_revision(expected_revision: str) -> str:
    if expected_revision == REVISION_ABSENT or (
        isinstance(expected_revision, str) and _REVISION_RE.fullmatch(expected_revision)
    ):
        return expected_revision
    raise ToolError(
        f"expected_revision must be the `revision` vault_read returned "
        f"('sha256:' + 64 lowercase hex) or {REVISION_ABSENT!r} for a note that must "
        f"not exist yet; got {expected_revision!r}"
    )


def _check_revision(path: str, p: Path, expected_revision: str) -> None:
    """Refuse the write unless the note is still at ``expected_revision``. Under the note lock."""
    current = _note_revision(p)
    if current == expected_revision:
        return
    if expected_revision == REVISION_ABSENT:
        raise VaultRevisionConflict(
            f"note {path!r} already exists (revision {current}); expected_revision="
            f"{REVISION_ABSENT!r} only creates a new note. Nothing was written: "
            "vault_read it, merge your change into its content, and write again with "
            "that revision"
        )
    if current == REVISION_ABSENT:
        raise VaultRevisionConflict(
            f"note {path!r} does not exist (expected revision {expected_revision}); it was "
            "moved or deleted since your read. Nothing was written: check where it went, "
            f"or pass expected_revision={REVISION_ABSENT!r} to create it again"
        )
    raise VaultRevisionConflict(
        f"note {path!r} changed since your read: expected revision {expected_revision}, "
        f"current revision {current}. Nothing was written: re-read the note with "
        "vault_read, merge your change into the current content, and write again with "
        "the new revision"
    )


def _decode_note(data: bytes) -> str:
    """Decode note bytes exactly as ``Path.read_text(errors="replace")`` would."""
    with io.TextIOWrapper(io.BytesIO(data), errors="replace") as text:
        return text.read()


def _frontmatter_block(text: str) -> str | None:
    """The raw YAML frontmatter body of `text`, or None if it has none."""
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _locked_for_path(p: Path) -> bool:
    """Lock verdict for a note; the helper lives in core ``note_lock`` (#2091)."""
    return note_is_locked(p)


def _is_locked(text: str) -> bool:
    """True if `text` starts with YAML frontmatter that locks the note."""
    data = text.encode("utf-8", errors="replace")
    limit = _LOCK_READ_CEILING
    verdict = _lock_verdict(data[:limit], limit, final=True)
    return bool(verdict)


def _is_locked_path(p: Path) -> bool:
    """True if the note at `p` is locked, without reading it whole."""
    return _locked_for_path(p)


def _preset_readable(path: Path) -> bool:
    """Check each enumerated or embedded target before following it for a read."""
    try:
        resolve_in_vault(str(path))
    except ToolError:
        return False
    return True


def _iter_notes(root: Path):
    """Yield vault note files under root, skipping hidden dirs (.git/.obsidian)."""
    for f in sorted(root.rglob("*")):
        if not _preset_readable(f):
            continue
        if not f.is_file() or f.suffix not in (".md", ".txt"):
            continue
        if any(part.startswith(".") for part in f.relative_to(root).parts):
            continue
        yield f


def _find_note_by_target(target: str, parent_dir: Path, root: Path) -> Path | None:
    target_clean = target.strip()
    # Try direct paths with various suffixes relative to parent and root
    for suffix in (".md", ".txt", ""):
        cand = target_clean + suffix
        # Relative to parent directory
        p = (parent_dir / cand).resolve()
        if p.is_relative_to(root) and _preset_readable(p) and p.is_file():
            return p
        # Relative to vault root
        p = (root / cand).resolve()
        if p.is_relative_to(root) and _preset_readable(p) and p.is_file():
            return p

    # If it's a simple name without slashes, search the entire vault
    if "/" not in target_clean and "\\" not in target_clean:
        for f in _iter_notes(root):
            if f.stem == target_clean:
                return f

    return None


def _expand_transclusions(
    content: str, parent_file: Path, root: Path, current_depth: int = 0, max_depth: int = 2
) -> str:
    if current_depth >= max_depth:
        return content

    def replace_match(match: re.Match) -> str:
        target = match.group(1).strip()
        target_path = _find_note_by_target(target, parent_file.parent, root)
        if not target_path or not target_path.is_file():
            return match.group(0)

        if target_path.stat().st_size > config.READ_MAX_BYTES:
            return f"\n\n* [Transclusion: {target} (file too large)] *\n\n"

        try:
            target_content = target_path.read_text(errors="replace")
            if untrusted.is_vault_inbox_path(target_path.relative_to(root).as_posix()):
                target_content = untrusted.UNTRUSTED_NOTE + "\n\n" + target_content
            # Recursively expand embeds
            target_content = _expand_transclusions(
                target_content, target_path, root, current_depth + 1, max_depth
            )
            return f"\n\n--- [Transclusion: {target}] ---\n{target_content}\n--- [End Transclusion: {target}] ---\n\n"
        except Exception as e:
            return f"\n\n* [Transclusion Error: {target} ({str(e)})] *\n\n"

    return TRANSCLUSION_RE.sub(replace_match, content)


def _apply_template(template_content: str, target_path: Path, content: str = "") -> str:
    now = datetime.datetime.now()
    yesterday = now - datetime.timedelta(days=1)
    tomorrow = now + datetime.timedelta(days=1)

    replacements = {
        "{{date}}": now.strftime("%Y-%m-%d"),
        "{{time}}": now.strftime("%H:%M"),
        "{{datetime}}": now.isoformat(timespec="seconds"),
        "{{yesterday}}": yesterday.strftime("%Y-%m-%d"),
        "{{tomorrow}}": tomorrow.strftime("%Y-%m-%d"),
        "{{title}}": target_path.stem,
        "{{content}}": content,
    }

    result = template_content
    for placeholder, replacement in replacements.items():
        result = result.replace(placeholder, replacement)

    if "{{content}}" not in template_content and content:
        result = result.rstrip() + "\n\n" + content

    return result


def vault_read(path: str, offset: int = 0, limit: int = 2000, expand_embeds: bool = False) -> dict:
    """Read a note from the Obsidian vault (max 2000 lines per call; page with offset).

    If expand_embeds is True, parses ![[note]] transclusions and expands them inline.
    Returns {path, content, total_lines, truncated, revision}. ``revision`` is
    ``sha256:<hex>`` of the note's raw bytes on disk (never of the expanded
    embeds or the page window); pass it to ``vault_write(expected_revision=...)``
    so the write is refused if someone else changed the note in between.
    ``locked`` (task #2515) is the verdict ``vault_write``'s guard reaches on
    those same bytes: true when the frontmatter has ``locked: true``, and
    true when the frontmatter is too long to decide (the guard fails closed).
    """
    p = resolve_in_vault(path)
    if not p.is_file():
        raise ToolError(f"no such note: {path}")
    if p.stat().st_size > config.READ_MAX_BYTES:
        raise ToolError(f"file is larger than {config.READ_MAX_BYTES} bytes")

    # One read serves both the content and the revision, so they always agree.
    raw = p.read_bytes()
    revision = _revision_of_bytes(raw)
    content = _decode_note(raw)
    if expand_embeds:
        root = config.vault_root().expanduser().resolve()
        content = _expand_transclusions(content, p, root)

    lines = content.splitlines()
    window = lines[offset : offset + max(1, min(limit, 5000))]
    rel = _vault_rel(p)
    result = {
        "path": rel,
        "content": "\n".join(window),
        "total_lines": len(lines),
        "truncated": offset + len(window) < len(lines),
        "revision": revision,
        "locked": note_bytes_are_locked(raw),
    }
    if untrusted.is_vault_inbox_path(rel) or (
        expand_embeds and untrusted.UNTRUSTED_NOTE in content
    ):
        # An Inbox capture is external input until a human files it (task #1911).
        untrusted.mark_untrusted(result, "vault_inbox")
    return result


_VAULT_WRITE_MODES = ("overwrite", "append")

# Streaming readback chunk for the post-write line count (1 MiB) and the
# boundary set it must honor. The counter deliberately mirrors
# str.splitlines() — which splits on LF, CR, CRLF, VT, FF, record separators,
# NEL, and the Unicode line/paragraph separators — so total_lines_after from
# vault_write agrees with vault_read's splitlines()-based total_lines, even
# though file-object iteration only recognizes universal newlines (Devin
# review of PR #621).
_READ_CHUNK_BYTES = 1 << 20
_LINE_SEPARATORS_RE = re.compile(r"\r\n|[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def _count_note_lines(path: Path, chunk_size: int = _READ_CHUNK_BYTES) -> int:
    """Count lines streaming fixed-size chunks, str.splitlines() semantics.

    Never materializes more than one chunk of the note: a single multi-GB
    line (external editors can create one; the write cap does not bound the
    existing file) must not exhaust the server after the write already
    succeeded. Byte chunks go through an incremental decoder so a split
    multi-byte separator is reassembled, and at most one held-back CR crosses
    a chunk edge so a CRLF pair straddling chunks counts once.
    """
    decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))("replace")
    boundaries = 0
    fragment = 0  # chars after the last processed boundary (a final unterminated line)
    carry = ""  # at most one held-back trailing CR (may pair with the next LF)
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if carry:
                text = carry + text
                carry = ""
            if not text:
                continue
            if text[-1] == "\r":
                carry = "\r"
                text = text[:-1]
                if not text:
                    continue
            matches = list(_LINE_SEPARATORS_RE.finditer(text))
            boundaries += len(matches)
            if matches:
                fragment = len(text) - matches[-1].end()
            else:
                fragment += len(text)
    if carry:  # trailing CR at EOF is a boundary by itself, nothing follows
        boundaries += 1
        fragment = 0
    return boundaries + (1 if fragment else 0)


def vault_write(
    path: str,
    content: str = "",
    mode: str = "overwrite",
    append: bool = False,
    force: bool = False,
    template: str | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Create, overwrite, or append to a .md/.txt note in an allowlisted vault subdir.

    ``mode`` is ``"overwrite"`` (default — replaces the whole file) or
    ``"append"`` (adds to the end). ``append=True`` is the legacy spelling of
    ``mode="append"`` and still works. Any other ``mode`` is an error rather
    than a silent fall-through to overwrite (task #854): before this an unknown
    keyword such as ``mode="append"`` was dropped by argument validation and the
    call clobbered the file.

    If template is specified, reads that template note from the vault, replaces
    placeholders ({{date}}, {{time}}, {{title}}, {{content}}), and writes/appends.
    Writes are confined to UA_VAULT_WRITE_DIRS (default "00 - Inbox", Logs,
    Wiki); max 2 MB per call; creates parent directories. If the target note
    already exists and its YAML frontmatter has `locked: true`, both overwrite
    and append are refused; pass force=True to override, but only with
    human/steward approval — a templated write is subject to the same lock.

    ``expected_revision`` (task #2111) makes the write conditional: pass the
    ``revision`` vault_read returned and the write (overwrite, append or
    template) is refused, with nothing written, if the note has changed since;
    re-read, merge and retry with the new revision. ``"absent"`` means the note
    must not exist yet (create-only). Omitted, the write is unconditional,
    unless the hub sets UA_VAULT_REQUIRE_REVISION=1, which refuses a blind
    overwrite of an existing note (creates and appends stay allowed). The lock
    guard is checked first.
    Returns {path, bytes_written, mode, append, total_lines_after, revision} —
    ``revision`` is the note's new revision; the
    effective-mode echo (#1165, extended per the PR #617 review) makes a
    silently-stripped ``mode`` visible at write time and reports the true
    effective mode for the legacy ``append=True`` spelling too; a readback
    failure after a durable write yields ``total_lines_after=None`` (and an
    append's ``revision=None``) rather than a retryable error.
    """
    return _vault_write(
        path,
        content,
        mode,
        append,
        force,
        template,
        expected_revision=expected_revision,
        require_revision=config.vault_require_revision(),
    )


def vault_write_unless(path: str, content: str, unless: Callable[[Path], bool]) -> dict | None:
    """``vault_write`` (overwrite, no force) unless ``unless(p)`` says the note needs no write.

    ``unless`` gets the resolved note path and runs under the note lock, before
    the ``locked: true`` refusal and the write, so a caller's "already filed?"
    check and the write are one critical section: a second writer queued on the
    lock sees the first one's note (#2091), and a locked note that already
    holds the content is a skip, not ``VaultLockedError``. Returns None when
    ``unless`` skipped the write, else ``vault_write``'s result. Not an MCP
    tool; ``tools.learning`` files its proposals through it.
    """
    return _vault_write(path, content, "overwrite", False, False, None, unless=unless)


# Hub-internal read-modify-write writers retry this many times after a revision
# conflict before giving up (task #2111): re-read, rebuild, write again.
MERGE_WRITE_RETRIES = 3


def note_snapshot(path: str) -> tuple[str | None, str]:
    """``(text, revision)`` of a vault note from one read; ``(None, "absent")`` if missing.

    The text uses UTF-8 (errors replaced) and universal newlines, like
    Path.read_text, whole and unpaged. The revision hashes the original bytes.
    """
    p = resolve_in_vault(path)
    if not p.is_file():
        return None, REVISION_ABSENT
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, _revision_of_bytes(raw)


def vault_write_merged(
    path: str,
    build: Callable[[str | None], str | None],
    *,
    force: bool = False,
    retries: int = MERGE_WRITE_RETRIES,
) -> dict | None:
    """Read-modify-write a note without ever overwriting it blind (task #2111).

    Reads the note, calls ``build(current_text_or_None)`` for the new content,
    and writes it with ``expected_revision`` set to the revision just read, so
    an edit that lands between the read and the write conflicts. On a
    revision conflict it re-reads and rebuilds, up to ``retries`` more times,
    then raises ``VaultRevisionConflict``. ``build`` returning None means no
    write is needed; the result is then None. This is how the hub's own
    generated-note writers stay valid under ``UA_VAULT_REQUIRE_REVISION=1``.
    ``build`` must merge the current content to preserve other writers. Whole-
    note replacements that ignore it must pass ``retries=0`` so a conflict
    cannot authorize an unchanged stale payload against a newer revision.
    Not an MCP tool.
    """
    last: VaultRevisionConflict | None = None
    for _attempt in range(retries + 1):
        current, revision = note_snapshot(path)
        content = build(current)
        if content is None:
            return None
        try:
            return vault_write(path, content, force=force, expected_revision=revision)
        except VaultRevisionConflict as exc:
            last = exc
    raise VaultRevisionConflict(
        f"note {path!r} changed between read and write on each of {retries + 1} attempts; "
        f"nothing was overwritten. Run the write again once the note is quiet, or merge by "
        f"hand. Last conflict: {last}"
    )


def _vault_write(
    path: str,
    content: str,
    mode: str,
    append: bool,
    force: bool,
    template: str | None,
    *,
    unless: Callable[[Path], bool] | None = None,
    expected_revision: str | None = None,
    require_revision: bool = False,
) -> dict | None:
    if mode not in _VAULT_WRITE_MODES:
        raise ToolError(
            f"mode must be one of {_VAULT_WRITE_MODES}, got {mode!r} "
            "(vault_write has no partial-edit mode: read the note, then write it back whole)"
        )
    if expected_revision is not None:
        expected_revision = _validated_revision(expected_revision)
    append = append or mode == "append"
    # Effective mode covers the legacy `append=True` spelling too — echoing the
    # raw `mode` param made that path report "overwrite" while appending
    # (Devin review, PR #617).
    effective_mode = "append" if append else "overwrite"

    p = resolve_in_vault(path, for_write=True)
    if force and profiles.cloud_hosted_caller():
        # Keyed on the cloud identity as well as the cloud profile (task #1799):
        # a cloud-hosted chief of staff still cannot override a lock.
        raise ToolError("cloud vault stewardship cannot use force")

    if template:
        tpl_path = resolve_in_vault(template)
        if not tpl_path.is_file():
            raise ToolError(f"template note not found: {template}")
        try:
            template_content = tpl_path.read_text(errors="replace")
            content = _apply_template(template_content, p, content)
        except Exception as e:
            raise ToolError(f"failed to read or apply template: {e}") from e

    data = content.encode()
    if len(data) > config.WRITE_MAX_BYTES:
        raise ToolError(f"content exceeds {config.WRITE_MAX_BYTES} bytes; write in chunks")
    # Check and write under the per-note lock so obsidian_append and
    # vault_journal cannot land between them (#1890).
    with note_write_lock(p):
        # ``unless`` first: a locked note that already holds what the caller
        # would write needs no write, so it is "already filed", not a refusal.
        if unless is not None and unless(p):
            return None
        if not force and p.is_file() and _is_locked_path(p):
            raise VaultLockedError(
                f"note {path!r} is locked (frontmatter `locked: true`); pass force=True "
                "only with human/steward approval to override"
            )
        # Revision checks (#2111) sit after the lock guard, so a locked note
        # still reports as locked, and inside the note lock, so a second writer
        # holding the same revision queues here and sees the first one's bytes.
        if expected_revision is not None:
            _check_revision(path, p, expected_revision)
        elif require_revision and not append and p.is_file():
            raise ToolError(
                f"note {path!r} exists and this hub requires a revision to overwrite "
                "(UA_VAULT_REQUIRE_REVISION=1). Nothing was written: vault_read it, merge "
                "your change into its content, and write again with "
                "expected_revision=<the revision vault_read returned>; appends need no revision"
            )

        if profiles.cloud_hosted_caller():
            # Template IO can take time; a lock or redirected path introduced
            # during it must be checked against the same boundary before writing.
            # The note lock is keyed by the first resolution; refuse a redirect.
            if resolve_in_vault(path, for_write=True) != p:
                raise ToolError(f"note {path!r} was redirected during the write; retry")

        _write_note_bytes(p, data, append)
        # The new revision is taken under the note lock so it names these bytes,
        # not a later writer's. An overwrite wrote exactly ``data``; an append
        # rehashes the note, and a failed readback leaves the write durable.
        if not append:
            revision: str | None = _revision_of_bytes(data)
        else:
            try:
                revision = _note_revision(p)
            except OSError:
                revision = None
    # General vault files do not take an ``agent`` parameter like journals do.
    # Preserve authenticated attribution in the append-only event ledger rather
    # than trusting note content or a caller-supplied label.
    events.record_event(
        "vault.write",
        payload={
            "path": _vault_rel(p),
            "bytes_written": len(data),
            "mode": effective_mode,
            "append": append,
        },
        originator=config.verified_agent_identity(),
    )
    # Readback echo (#1165): a client holding a stale tool-schema snapshot can
    # silently strip `mode` before the call arrives (the hub-side extra="forbid"
    # guard cannot catch a kwarg that never reaches the server), turning an
    # intended append into a clobbering overwrite. Echo the EFFECTIVE mode and
    # post-write shape so any intent-vs-result mismatch is visible at write time.
    # The existing note is NOT bounded by WRITE_MAX_BYTES (repeated appends
    # and external edits can grow it arbitrarily), so count lines by streaming
    # fixed-size chunks instead of decoding the note into memory — and never
    # materialize a single line either: readline()-style iteration buffers a
    # multi-GB line wholesale (Devin review, PR #621). A readback failure
    # must not turn the already-durable write into a retryable error — a
    # retried append would duplicate.
    try:
        total_lines_after = _count_note_lines(p)
    except OSError:
        total_lines_after = None
    return {
        "path": _vault_rel(p),
        "bytes_written": len(data),
        "mode": effective_mode,
        "append": append,
        "total_lines_after": total_lines_after,
        "revision": revision,
    }


def _write_note_bytes(p: Path, data: bytes, append: bool) -> None:
    """Write ``data`` to the note, telling a refusal from an unknown outcome (task #2521).

    An OSError creating the directory or opening the note is a refusal: the
    note is as it was. Once the open succeeds the note may already be changed
    (an overwrite truncates at open, an append may land in part), so an
    OSError from the write, flush or close raises ``OutcomeUnknownError``.
    """
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "ab" if append else "wb")
    except OSError as exc:
        raise ToolError(
            f"cannot write note {_vault_rel(p)!r} ({type(exc).__name__}); nothing was "
            "written. Check note and directory permissions and available disk space"
        ) from None
    try:
        with f:
            f.write(data)
    except OSError as exc:
        raise OutcomeUnknownError(
            f"write to {_vault_rel(p)!r} failed after it began ({type(exc).__name__}); the "
            f"note may be {'partly appended' if append else 'truncated or partly written'}. "
            "Read it before writing again"
        ) from None


def vault_move(source: str, destination: str, force: bool = False) -> dict:
    """Move or rename a single note within the vault, creating destination parent dirs as needed.

    Restricted to the chief-of-staff profile — the archive/organize authority
    the operator granted Desktop_Agent5 on 2026-08-04 to move captures the way
    vault_write already lets it create them. Developer and steward callers get
    a permission error rather than a partial move. Both source and destination
    resolve through the same `resolve_in_vault(for_write=True)` chokepoint
    vault_write uses, so they inherit its traversal, hidden-path, write-dir,
    and file-type checks — chief-of-staff's widened `01 - Projects` .. `04 -
    Archive` allowlist is what lets this reach PARA folders vault_write cannot.

    A locked source (`locked: true` frontmatter) is refused unless force=True;
    an existing destination is refused unless force=True (no silent
    overwrite). Both checks and the move run under the note locks of source
    and destination (``note_write_locks``, sorted order, #2058). The move is a single `shutil.move` — atomic on the vault's own
    filesystem, since source and destination both resolve under the same
    vault root. Returns {source, destination, bytes_moved}.
    """
    if profiles.current() != profiles.CHIEF_OF_STAFF:
        raise ToolError(
            "vault_move requires the chief-of-staff profile; activate it with "
            "profile_activate(profile='chief-of-staff') before moving vault notes"
        )
    if force and profiles.cloud_hosted_caller():
        raise ToolError("cloud vault stewardship cannot use force")

    src = resolve_in_vault(source, for_write=True)
    dst = resolve_in_vault(destination, for_write=True)
    if src == dst:
        raise ToolError("source and destination are the same path")

    # Check and move under both notes' locks (#2058), so no hub writer can lock
    # or create either note between the checks and the move. The checks run
    # after the wait: a note seen before it may have changed while we queued.
    with note_write_locks(src, dst):
        if not src.is_file():
            raise ToolError(f"no such note: {source}")
        if dst.is_dir():
            raise ToolError(f"destination {destination!r} is a directory; give a full note path")
        if not force and _is_locked_path(src):
            raise VaultLockedError(
                f"note {source!r} is locked (frontmatter `locked: true`); pass force=True "
                "only with human/steward approval to move it"
            )
        if not force and dst.exists():
            raise ToolError(
                f"destination {destination!r} already exists; pass force=True to overwrite"
            )

        dst.parent.mkdir(parents=True, exist_ok=True)
        size = src.stat().st_size
        shutil.move(str(src), str(dst))
        _frontmatter_cache.pop(str(src), None)

    events.record_event(
        "vault.move",
        payload={"source": _vault_rel(src), "destination": _vault_rel(dst), "bytes_moved": size},
        originator=config.verified_agent_identity(),
    )
    return {"source": _vault_rel(src), "destination": _vault_rel(dst), "bytes_moved": size}


def vault_archive_plan(
    inbox_path: str | None = None,
    archive_root: str | None = None,
    as_of: str | None = None,
) -> dict:
    """Build a read-only plan for filing processed inbox captures.

    A processed marker permits filing only when the content-hash ingest ledger
    is unchanged. A ``status: final`` frontmatter value is the sole signal
    that a capture may support an authoritative Wiki promotion; all other
    processed captures are explicitly ``archive-only``. A
    ``superseded: ...`` marker is independently sufficient to file a capture,
    but never permits Wiki promotion.

    The planner never writes, moves, or edits notes. Its destination preserves
    the inbox-relative path below ``archive_root/YYYY-MM-DD`` so a later
    chief-of-staff ``vault_move`` call can execute the reviewed plan.
    """
    if inbox_path is None:
        inbox_path = "00 - Inbox"
    if archive_root is None:
        archive_root = "04 - Archive/inbox-archive"

    root = resolve_in_vault(inbox_path)
    if not root.is_dir():
        raise ToolError(f"inbox path is not a directory: {inbox_path}")

    if as_of is None:
        as_of = datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(as_of)
    except ValueError as e:
        raise ToolError("as_of must be YYYY-MM-DD") from e

    archive_prefix = archive_root.rstrip("/") + "/" + as_of
    candidates: list[dict] = []
    counts = {"archive_ready": 0, "archive_only": 0, "authoritative": 0, "pending": 0}

    for note in _iter_notes(root):
        rel = _vault_rel(note)
        content = note.read_text(errors="replace")
        processed = _PROCESSED_MARKER_RE.search(content)
        superseded = _SUPERSEDED_MARKER_RE.search(content)
        frontmatter = _frontmatter_block(content) or ""
        status_match = _STATUS_RE.search(frontmatter)
        status = status_match.group("value").lower() if status_match else None
        shipped = bool(_SHIPPED_RE.search(frontmatter))
        ingest = _classify_ingest(note)

        if superseded:
            classification = "archive-only"
            action = "move"
            reason = "explicit superseded marker"
            promotion = "none"
        elif processed and ingest["status"] == "unchanged":
            authoritative = status == "final" or shipped
            classification = "authoritative" if authoritative else "archive-only"
            action = "move"
            reason = "processed marker and unchanged ingest ledger"
            promotion = "authoritative" if authoritative else "none"
        else:
            classification = "pending"
            action = "hold"
            reason = "missing processed marker or ingest ledger is new/changed"
            promotion = "none"

        if action == "move":
            counts["archive_ready"] += 1
            counts[classification.replace("-", "_")] += 1
        else:
            counts["pending"] += 1

        relative = note.relative_to(root)
        candidates.append(
            {
                "source": rel,
                "destination": f"{archive_prefix}/{relative}",
                "action": action,
                "classification": classification,
                "promotion": promotion,
                "reason": reason,
                "status": status,
                "shipped": shipped,
                "ingest_status": ingest["status"],
                "processed_target": processed.group("target").strip() if processed else None,
                "superseded_target": superseded.group("target").strip() if superseded else None,
            }
        )

    return {
        "inbox": _vault_rel(root),
        "archive_root": archive_prefix,
        "read_only": True,
        "counts": counts,
        "candidates": candidates,
    }


def vault_list(path: str = ".", glob: str | None = None, max_entries: int = 500) -> dict:
    """List a vault directory (optionally filtered by a glob like '*.md').

    Hidden entries (.git, .obsidian) are omitted. Returns
    {entries: [{name, type, size}], truncated}.
    """
    from ..hosted_capabilities import refuse_unpopulated_vault

    refuse_unpopulated_vault("vault_list")
    if path == "." and profiles.current() == profiles.CLOUD_VAULT_STEWARD:
        path = "Wiki"
    if glob and profiles.cloud_hosted_caller():
        if Path(glob).is_absolute() or ".." in Path(glob).parts:
            raise ToolError("cloud vault listing requires a relative glob without parent traversal")
    p = resolve_in_vault(path)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    cap = max(1, min(max_entries, 2000))
    items = sorted(
        (e for e in (p.glob(glob) if glob else p.iterdir()) if not e.name.startswith(".")),
        key=lambda x: x.name,
    )
    entries = []
    for entry in items:
        real = entry.resolve()
        if not _preset_readable(real):
            continue
        entries.append(
            {
                "name": entry.name,
                "type": "dir" if real.is_dir() else "file",
                "size": real.stat().st_size if real.is_file() else None,
            }
        )
        if len(entries) > cap:
            break
    return {"entries": entries[:cap], "truncated": len(entries) > cap}


def vault_search(pattern: str, path: str = ".", max_results: int = 100) -> dict:
    """Regex-search notes (.md/.txt) under a vault directory.

    Returns {matches: [{path, line_no, line}], truncated}.
    """
    from ..hosted_capabilities import refuse_unpopulated_vault

    refuse_unpopulated_vault("vault_search")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}") from e
    if path == "." and profiles.current() == profiles.CLOUD_VAULT_STEWARD:
        path = "Wiki"
    root = resolve_in_vault(path)
    if not root.is_dir():
        raise ToolError(f"not a directory: {path}")
    cap = max(1, min(max_results, 1000))
    matches, truncated = [], False
    for f in _iter_notes(root):
        if f.stat().st_size > config.SEARCH_FILE_MAX_BYTES:
            continue
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if rx.search(line):
                if len(matches) >= cap:
                    truncated = True
                    break
                matches.append({"path": _vault_rel(f), "line_no": i, "line": line[:500]})
        if truncated:
            break
    return {"matches": matches, "truncated": truncated}


def vault_links(path: str) -> dict:
    """Map a note's graph neighborhood: outgoing [[wikilinks]] and incoming backlinks.

    A backlink is any note whose [[target]] matches this note's name (or its
    vault-relative path). Returns {path, outgoing: [str], backlinks: [str]}.
    """
    p = resolve_in_vault(path)
    if not p.is_file():
        raise ToolError(f"no such note: {path}")
    root = config.vault_root().expanduser().resolve()
    outgoing = sorted(
        {m.group(1).strip() for m in WIKILINK_RE.finditer(p.read_text(errors="replace"))}
    )

    stem = p.stem
    rel_no_ext = str(p.relative_to(root).with_suffix(""))
    backlinks = []
    for f in _iter_notes(root):
        if f == p:
            continue
        for m in WIKILINK_RE.finditer(f.read_text(errors="replace")):
            target = m.group(1).strip()
            if target == stem or target == rel_no_ext:
                backlinks.append(_vault_rel(f))
                break
    return {"path": _vault_rel(p), "outgoing": outgoing, "backlinks": backlinks}


# Task #379: journal entries had grown to 900+ chars each, and a busy daily
# (Logs/2026-07-29.md, 60 KB) already overflows vault_read for every agent
# that boots and asks "what happened today". A convention alone won't hold —
# agents write what they write — so this is enforced here.
#
# A journal entry is a bookkeeping index ("did X, see task #N / commit abc"),
# not the record itself: the detail belongs in the task, commit, or memory
# note it points at. ~2 lines is enough for that pointer, which is what makes
# this cap a framing decision rather than an arbitrary number.
#
# Rejecting instead of truncating is deliberate: Logs/ is the historical
# record (memory b0edae991a72), and a truncation that silently drops an
# observation with no task/commit/memory anchor repeats the mistake #225's
# own boundaries exist to forbid. A rejection forces the caller to either
# shorten the entry or give the detail a real home first — nothing is ever
# silently lost.
_JOURNAL_ENTRY_MAX_CHARS = 240


def _validated_journal_entry(entry: str) -> str:
    """Collapse whitespace and enforce the task #379 cap shared by every journal note."""
    entry = " ".join(entry.split())
    if not entry:
        raise ToolError("journal entry is empty")
    if len(entry) > _JOURNAL_ENTRY_MAX_CHARS:
        raise ToolError(
            f"journal entry is {len(entry)} chars, over the {_JOURNAL_ENTRY_MAX_CHARS}-char "
            "cap (task #379). A journal entry is a bookkeeping index, not the record "
            "itself — shorten it to something like '<what happened>, see task #N / "
            "commit <sha> / memory <id>' and put the detail there. If this observation "
            "has no task, commit, or memory note to point at yet, create one first "
            "(task_add / memory_save) so the detail has a home, then journal a pointer "
            "to it."
        )
    return entry


def _journal_line(entry: str, agent: str, tags: list[str] | None) -> str:
    now = datetime.datetime.now().astimezone()
    tag_suffix = "".join(f" #{t.strip().lstrip('#')}" for t in (tags or []) if t.strip())
    return f"- **{now.strftime('%H:%M')}** [{agent.strip()}] {entry}{tag_suffix}\n"


@contextlib.contextmanager
def _open_cloud_journal_note(p: Path):
    """Open beneath the authorized vault without following raced symlinks."""
    root = config.vault_root().expanduser().resolve()
    relative = p.relative_to(root)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            relative.name,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o666,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        note = open(fd, "a+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with note:
        yield note


def _journal_descriptor_locked(note) -> bool:
    """Check the frontmatter of the same inode that will receive the append."""
    sizes = (_FRONTMATTER_MAX_BYTES, VAULT_LOCK_HEAD_BYTES, _LOCK_READ_CEILING)
    for n, limit in enumerate(sizes):
        verdict = _lock_verdict(os.pread(note.fileno(), limit, 0), limit, final=n == len(sizes) - 1)
        if verdict is not None:
            return verdict
    return True


def _append_journal_line(
    rel: str,
    header: str,
    line: str,
    *,
    for_journal: bool = True,
    dedupe_needle: str | None = None,
) -> tuple[Path, bool, bool]:
    """Append under an OS lock; returns (path, created_note, appended).

    Append mode never truncates history. Hold an OS lock through header
    initialization, append and flush so other hub processes cannot append
    while the creator's header is still buffered. Closing also releases the
    lock on error, and process exit releases it after an interrupted call.
    """
    p = resolve_in_vault(rel, for_write=True, for_journal=for_journal)
    # Set once the first byte is handed to the note (task #2521). An OSError
    # before that is a refusal: nothing was written. One after it may have left
    # part of the line (a full disk, a failed flush or close), so it is an
    # unknown outcome, not a refusal a caller may blindly repeat.
    writing = {"began": False}
    try:
        # The note lock vault_write and obsidian_append also take (#1890).
        with note_write_lock(p):
            cloud = profiles.cloud_hosted_caller()
            if cloud:
                # Another caller may have created/locked the note while we waited.
                resolve_in_vault(rel, for_write=True, for_journal=for_journal)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
            opener = _open_cloud_journal_note(p) if cloud else open(p, "a+")
            with opener as note:
                return _write_journal_descriptor(p, note, header, line, dedupe_needle, writing)
    except OSError as exc:
        if writing["began"]:
            raise OutcomeUnknownError(
                f"journal append to {_vault_rel(p)!r} failed after the write began "
                f"({type(exc).__name__}); the note may hold part of the entry. Read it "
                "before journaling again"
            ) from None
        raise ToolError(
            f"cannot append journal note {_vault_rel(p)!r} ({type(exc).__name__}); "
            "check note and directory permissions and available disk space"
        ) from None


def _write_journal_descriptor(p, note, header, line, dedupe_needle, writing=None):
    if _journal_descriptor_locked(note):
        # Same guard as vault_write: a locked day note takes no appends.
        raise VaultLockedError(
            f"journal note {_vault_rel(p)!r} is locked (frontmatter `locked: true`); "
            "unlock it or use vault_write(force=True) with human/steward approval"
        )
    created = os.fstat(note.fileno()).st_size == 0
    if dedupe_needle is not None:
        note.seek(0)
        if any(dedupe_needle in existing for existing in note):
            return p, False, False
    if writing is not None:
        writing["began"] = True
    if created:
        note.write(header)
    note.write(line)
    note.flush()
    return p, created, True


def vault_journal(
    entry: str,
    agent: str,
    date: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Append a timestamped, agent-attributed entry to today's journal note.

    The bookkeeping chokepoint: every agent logs decisions/actions here with its
    label (e.g. Desktop_Agent1). Creates the daily note with frontmatter on first
    write. Entries are capped at `_JOURNAL_ENTRY_MAX_CHARS` (task #379) — a
    journal entry is a pointer to where the detail lives (a task, commit, or
    memory note), not the detail itself. A locked day note (`locked: true`
    frontmatter) takes no appends and raises a lock error; use
    `vault_write(force=True)` only with human/steward approval to override.
    Returns {path, entry, created_note}.
    """
    return _daily_journal_append(entry, agent, date, tags)


def _daily_journal_append(
    entry: str,
    agent: str,
    date: str | None,
    tags: list[str] | None,
    *,
    dedupe_needle: str | None = None,
) -> dict:
    entry = _validated_journal_entry(entry)
    agent = config.verify_write_identity(agent)
    if not agent or not agent.strip():
        raise ToolError("agent label is required (e.g. Desktop_Agent1)")
    if profiles.cloud_hosted_caller() and (
        len(tags or []) > 16
        or any(not re.fullmatch(r"[\w/-]{1,64}", t.lstrip("#")) for t in (tags or []))
    ):
        raise ToolError("cloud journal tags require at most 16 single-line tags of 1–64 characters")

    if date is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ToolError("date must be in YYYY-MM-DD format")
        try:
            parsed = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise ToolError("date must be a valid calendar date in YYYY-MM-DD format") from None
        if profiles.cloud_hosted_caller() and (
            date != parsed.isoformat() or abs((parsed - datetime.date.today()).days) > 1
        ):
            # Append-only must not mean "append to any day": the cloud scope
            # may log today, or yesterday/tomorrow across a timezone boundary.
            raise ToolError("cloud journal date must be within one day of today (or omitted)")
        day = date
    else:
        day = datetime.datetime.now().strftime("%Y-%m-%d")

    line = _journal_line(entry, agent, tags)
    p, created, appended = _append_journal_line(
        f"Logs/{day}.md",
        f"---\ndate: {day}\ntype: journal\n---\n\n# {day} — Agent journal\n",
        line,
        dedupe_needle=dedupe_needle,
    )
    if not appended:
        return {"path": _vault_rel(p), "entry": line.rstrip("\n"), "created_note": False}
    # Activity index: best-effort, must never break the journal write.
    try:
        storage.record_agent_event(
            "journal", ref_id=_vault_rel(p), agent=agent.strip(), summary=entry
        )
    except Exception:
        pass
    return {"path": _vault_rel(p), "entry": line.rstrip("\n"), "created_note": created}


def journal_pointer_append(entry: str, agent: str, day: str, needle: str) -> dict:
    """Append a daily pointer once, checking and appending under the same file lock."""
    if not needle or needle not in entry:
        raise ToolError("journal pointer needle must be present in the entry")
    return _daily_journal_append(entry, agent, day, ["pr-watch"], dedupe_needle=needle)


# Task #1920: a high-volume job (pr-watch wrote ~95 of 109 lines in one day)
# drowns the shared daily journal. Such a job keeps its per-event lines in its
# own per-day note under Logs/<stream>/ and leaves the daily journal a pointer.
_JOURNAL_STREAM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def journal_stream_path(stream: str, day: str) -> str:
    """Vault-relative path of a job's per-day stream note: ``Logs/<stream>/<day>.md``."""
    if not _JOURNAL_STREAM_RE.fullmatch(stream):
        raise ToolError(f"journal stream name {stream!r} must be lowercase letters, digits or '-'")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ToolError("date must be in YYYY-MM-DD format")
    return f"Logs/{stream}/{day}.md"


def journal_stream_append(
    stream: str,
    entry: str,
    agent: str,
    day: str,
    tags: list[str] | None = None,
) -> dict:
    """Append a journal-format line to a job's per-day stream note (internal, not a tool).

    Same line format, entry cap, lock guard and write allowlist as
    :func:`vault_journal`; only the target note differs. Creates the note with
    frontmatter on first write. Returns {path, entry, created_note}.
    """
    rel = journal_stream_path(stream, day)
    entry = _validated_journal_entry(entry)
    agent = config.verify_write_identity(agent)
    if not agent or not agent.strip():
        raise ToolError("agent label is required (e.g. Desktop_Agent1)")
    line = _journal_line(entry, agent, tags)
    header = f"---\ndate: {day}\ntype: journal\nstream: {stream}\n---\n\n# {day} — {stream}\n"
    p, created, _ = _append_journal_line(rel, header, line, for_journal=False)
    try:
        storage.record_agent_event(
            "journal", agent=agent.strip(), summary=entry, ref_id=_vault_rel(p)
        )
    except Exception:  # noqa: BLE001 - activity must never break the journal write
        pass
    return {"path": _vault_rel(p), "entry": line.rstrip("\n"), "created_note": created}


def journal_mentions(day: str, needle: str) -> bool:
    """True if the daily journal for ``day`` already contains ``needle`` (missing note: False)."""
    p = resolve_in_vault(f"Logs/{day}.md")
    try:
        return needle in p.read_text(errors="replace")
    except FileNotFoundError:
        return False


def _briefing_dedup_key(text: str) -> tuple[str, str] | None:
    """PR number or bare #task-id referenced in an entry, or None if neither is present.

    Entries with no such reference are never deduplicated (Roadmap Phase 1.2
    spec: "dedup by (date, task-id | PR-url)") — free-form notes stay as-is
    even if two machines happened to phrase something similarly.
    """
    pr = _PR_URL_RX.search(text)
    if pr:
        return ("pr", pr.group(1))
    task = _TASK_REF_RX.search(text)
    if task:
        return ("task", task.group(1))
    return None


def _inbox_triage(root: Path) -> list[dict]:
    """Ledger state of every top-level `00 - Inbox` capture, sorted by path.

    Only `.md`/`.txt` files directly in the inbox: attachments are not captures,
    and subdirectories (`00 - Inbox/Laptop/`) belong to another machine's flow.
    A missing inbox is an empty list, not an error — the digest still has a
    journal to report.
    """
    inbox = root / "00 - Inbox"
    if not _preset_readable(inbox) or not inbox.is_dir():
        return []
    return sorted(
        (
            _classify_ingest(f)
            for f in inbox.iterdir()
            if _preset_readable(f) and f.is_file() and f.suffix.lower() in (".md", ".txt")
        ),
        key=lambda e: e["path"],
    )


_JOURNAL_ENTRY_RX = re.compile(r"^- \*\*(\d{2}:\d{2})\*\* \[([^\]]+)\] (.+)$", re.MULTILINE)
_TAG_RX = re.compile(r"#(\w[\w-]*)")


def parse_journal_entries(text: str) -> list[dict]:
    """Parse a `Logs/YYYY-MM-DD.md` journal note into structured entries.

    Shared parser (System Leverage Plan "one log parser, two consumers"
    convergence note): the sleep pass's own daily digest (``sleep_pass`` re-exports it), and Roadmap
    Phase 1.2's cross-machine briefing (`Logs/Laptop/*.md` merge) reuses this
    directly rather than re-implementing the format.

    Entry line shape: ``- **HH:MM** [agent] free text ... #tag1 #tag2``.
    Lines that don't match (blank lines, the frontmatter block, headings) are
    silently skipped — a malformed line must not break the whole parse.
    """
    entries = []
    for m in _JOURNAL_ENTRY_RX.finditer(text):
        time_str, agent, rest = m.groups()
        # Exclude purely-numeric matches (e.g. "#132" in "Task #132 resolved") —
        # a real tag always mixes in a letter (#security, #task-132); a bare
        # number is a task-id reference in prose, not an intentional tag.
        tags = [t for t in _TAG_RX.findall(rest) if not t.isdigit()]
        entries.append({"time": time_str, "agent": agent, "text": rest.strip(), "tags": tags})
    return entries


def vault_briefing_digest(date: str | None = None, include_inbox: bool = True) -> dict:
    """Merge today's desktop + laptop journals into one deduped cross-machine digest.

    Roadmap Phase 1.2 — the "one log parser, two consumers" convergence:
    consumer 1 is the nightly sleep pass's own daily digest
    (`parse_journal_entries`), this is consumer 2. Reads
    `Logs/{date}.md` (desktop), `Logs/Laptop/{date}.md` (historical laptop
    mirror), and the explicit `Logs/pr-watch/{date}.md` stream (64 KiB bound),
    tags each entry with its source `machine`, merges them
    chronologically, and drops a laptop-side entry that references the same PR
    or #task-id as one already seen (see `_briefing_dedup_key`) — entries with
    neither reference are always kept.

    The `Logs/Laptop/` namespace has no live writer as of 2026-09-05 — every
    machine journals to vault root `Logs/` against the one shared hub, so the
    laptop entries only ever appear on a historical `date=` read. The old
    laptop-journal staleness fields (`laptop_stale_hours` / `stale` /
    `escalate`) were removed with the namespace: they reported an obsolete
    path as perpetually escalated (task #761). Use `agent_list` / session
    heartbeats to judge whether an agent is live.

    With `include_inbox` (default on), also reports the ingest-ledger state of
    every `00 - Inbox` capture — `inbox: [{path, status, last_ingested_at,
    agent, pages}]` with the same `new`/`unchanged`/`changed`/`empty`
    classification `vault_ingest_status` uses, plus `inbox_pending_count`
    (captures that are `new` or `changed`). A briefing that triages the inbox
    off a bare directory listing cannot tell a capture already folded into the
    wiki from one still waiting, and re-proposes finished work; this is the
    state that distinguishes them. Reporting only — it never marks an ingest.
    Pass `include_inbox=False` for the journal merge alone.

    Returns {date, entries: [{time, agent, text, tags, machine}],
    desktop_count, laptop_count, pr_watch_count, deduped_count} plus {inbox,
    inbox_pending_count} unless opted out, plus {delivery_slo,
    delivery_slo_line} — Epic C (#1188) exit-test metrics over the trailing
    7 days as of *now* (task #1259), not scoped to `date`: re-reading an old
    day's briefing shows the current SLO, not a historical one, since
    dispatch_runs/tasks carry no separate "as of" snapshot to replay against.
    Best-effort: a computation failure never breaks the journal digest —
    `delivery_slo` is None and the line says so instead of raising.
    """
    from .. import monitor
    from ..delivery_slo import format_delivery_slo_line

    if date is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ToolError("date must be in YYYY-MM-DD format")
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise ToolError("date must be a valid calendar date in YYYY-MM-DD format") from None
        day_str = date
    else:
        day_str = datetime.datetime.now().strftime("%Y-%m-%d")

    root = resolve_in_vault(".")
    trust_presets.check_path(root)

    def _read_entries(rel_path: str, machine: str, max_bytes: int | None = None) -> list[dict]:
        p = root / rel_path
        if not _preset_readable(p) or not p.is_file():
            return []
        if max_bytes is None:
            text = p.read_text(errors="replace")
        else:
            with p.open("rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ToolError("pr-watch journal exceeds the 64 KiB briefing limit")
            text = data.decode("utf-8", errors="replace")
        entries = parse_journal_entries(text)
        for e in entries:
            e["machine"] = machine
        return entries

    desktop_entries = _read_entries(f"Logs/{day_str}.md", "desktop")
    stream_entries = _read_entries(journal_stream_path("pr-watch", day_str), "pr-watch", 64 * 1024)
    laptop_entries = _read_entries(f"Logs/Laptop/{day_str}.md", "laptop")

    merged: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    deduped_count = 0
    for e in desktop_entries + laptop_entries + stream_entries:
        key = _briefing_dedup_key(e["text"])
        if key is not None:
            if key in seen_keys:
                deduped_count += 1
                continue
            seen_keys.add(key)
        merged.append(e)
    merged.sort(key=lambda e: e["time"])

    result = {
        "date": day_str,
        "entries": merged,
        "desktop_count": len(desktop_entries),
        "laptop_count": len(laptop_entries),
        "pr_watch_count": len(stream_entries),
        "deduped_count": deduped_count,
    }

    if include_inbox:
        inbox = _inbox_triage(root)
        result["inbox"] = inbox
        result["inbox_pending_count"] = sum(1 for e in inbox if e["status"] in ("new", "changed"))

    try:
        slo = monitor.delivery_slo()
        result["delivery_slo"] = slo
        result["delivery_slo_line"] = format_delivery_slo_line(slo)
    except Exception as exc:  # noqa: BLE001 - best-effort, never break the briefing
        result["delivery_slo"] = None
        result["delivery_slo_line"] = f"Delivery SLO: unavailable ({str(exc)[:120]})"

    return result


def _classify_ingest(p: Path) -> dict:
    """Ledger state of one capture: {path, content_hash, status, last_ingested_at,
    agent, pages}.

    The single hashing path behind both `vault_ingest_status` (one capture, the
    steward gate) and `vault_briefing_digest` (every capture, the triage list) —
    two implementations would drift, and a briefing that disagrees with the gate
    about what is ingested is worse than one that says nothing.

    Read-only: classifying never records an ingest.
    """
    data = p.read_bytes()
    content_hash = hashlib.sha256(data).hexdigest()
    rel = _vault_rel(p)
    row = storage.ingest_ledger_get(rel)

    if not data.decode(errors="replace").strip():
        status = "empty"
    elif row is None:
        status = "new"
    elif row["content_hash"] == content_hash:
        status = "unchanged"
    else:
        status = "changed"

    return {
        "path": rel,
        "content_hash": content_hash,
        "status": status,
        "last_ingested_at": row["ingested_at"] if row else None,
        "agent": row["agent"] if row else None,
        "pages": json.loads(row["pages"]) if row else [],
    }


def vault_ingest_status(
    path: str,
    mark: bool = False,
    agent: str | None = None,
    pages: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    """The steward-inbox pre-ingest gate: check or record a capture's ingest state.

    Hashes the note's current bytes (sha256) and compares against the
    vault_ingest_ledger to classify it: "new" (never ingested), "unchanged"
    (matches the last ingested hash — skip it), "changed" (differs — re-ingest
    it), or "empty" (whitespace-only content — reject it, never mark). After
    folding a "new"/"changed" capture into the wiki, call again with
    mark=True (and an agent identity, plus the wiki `pages` it fed) to record
    the ingest. `reason` is an optional human note stored with the actor
    attribution (task #1157). Returns {path, content_hash, status,
    last_ingested_at, agent, pages}.
    """
    p = resolve_in_vault(path)
    if not p.is_file():
        raise ToolError(f"no such note: {path}")
    classified = _classify_ingest(p)
    content_hash = classified["content_hash"]
    rel = classified["path"]

    if mark:
        if profiles.current() == profiles.RESEARCH:
            raise ToolError(
                "research profile cannot mark vault ingest status "
                "(mark=True); read-only status checks remain allowed"
            )
        if classified["status"] == "empty":
            raise ToolError(f"note {path!r} is empty; refusing to mark it ingested")
        agent = config.verify_write_identity(agent)
        if not agent or not agent.strip():
            raise ToolError("agent label is required (e.g. Desktop_Agent1) to mark an ingest")
        agent = agent.strip()
        pages_list = pages or []
        storage.ingest_ledger_mark(rel, content_hash, agent, json.dumps(pages_list), reason=reason)
        updated = storage.ingest_ledger_get(rel)
        return {
            "path": rel,
            "content_hash": content_hash,
            "status": "marked",
            "last_ingested_at": updated["ingested_at"],
            "agent": agent,
            "pages": pages_list,
            "reason": reason,
        }

    return classified


def vault_wiki_custodian(dry_run: bool = False) -> dict:
    """Audit the Obsidian Wiki, check broken links/orphans, and rebuild Wiki/Health.md.

    Writes the generated hygiene report to Wiki/Health.md; the curated Wiki/Index.md
    map is Steward-maintained and left untouched.
    """
    from ..wiki import run_custodian

    try:
        return run_custodian(dry_run=dry_run)
    except Exception as e:
        raise ToolError(str(e)) from e
