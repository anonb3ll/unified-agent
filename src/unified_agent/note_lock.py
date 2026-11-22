"""Per-note advisory write lock and ``locked: true`` guard shared by every hub note writer.

Core layer: it depends only on ``config``, ``safety`` and path normalization,
so the vault tools (services), the generated-note writers and the audit
reanchor (core) all take the same lock from here (#2086) and read the same
frontmatter lock verdict (#2091). ``tools.vault`` re-exports ``note_write_lock``,
``note_write_locks`` and the lock-verdict helpers under their old names.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import stat
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import config
from .safety import (
    LOCK_REASON_BOUNDED,
    LOCK_UNVERIFIABLE,
    VAULT_LOCK_HEAD_BYTES,
    VaultLockedError,
    resolve_in_vault,
    vault_head_is_locked,
    vault_lock_state,
)


@contextlib.contextmanager
def note_write_lock(p: Path):
    """Hold the per-note advisory lock the hub's note writers take (#1890).

    ``vault_write``, ``vault_journal``, ``obsidian_append``, ``vault_move``
    (source and destination, via ``note_write_locks``), the wiki custodian's
    ``Wiki/Health.md`` rewrite and the generated-note writers (consolidate,
    sleep_pass, evolve, harness_config, merge's failure report and the audit
    reanchor, #2086) take this lock, so none of them can land between another's
    lock check and write on one note. Each takes it only around its own check
    and write, never across an LLM call, subprocess or network wait.

    It is an ``fcntl.flock`` on a sidecar file under ``$UA_HOME/locks/vault``
    named by the sha256 of the note's vault-relative path, not on the note
    itself: a writer that replaces the note atomically (sync, git checkout, a
    temp-file rename) swaps its inode and would escape a lock on it. A note that
    does not exist yet is locked the same way. The key is NFC-normalized and
    case-folded so two spellings of one note on a case-insensitive volume share
    a lock. The wait is unbounded: there is no timeout, a waiter blocks until the
    holder releases. Advisory only: Obsidian, its sync and other non-hub writers
    do not take it. Lock files are never removed; deleting one while another
    writer holds it would split the lock.
    """
    with note_write_locks(p):
        yield


@contextlib.contextmanager
def note_write_locks(*paths: Path):
    """Hold the note lock of every path in ``paths``, deadlock-free (#2058).

    Locks are taken in sorted lock-file order, so two callers locking the same
    notes in opposite argument order (``vault_move`` A->B against B->A) queue on
    the first shared lock instead of each holding one and waiting on the other.
    Paths that share a key (the same note, or spellings that differ only in case
    or Unicode form) take it once: a second ``flock`` on a fresh descriptor would
    block on the caller's own lock.
    """
    names = sorted({_note_lock_name(p) for p in paths})
    with contextlib.ExitStack() as held:
        for name in names:
            held.enter_context(_hold_note_lock_file(name))
        yield


def vault_rel(p: Path) -> str:
    """``p`` relative to the configured vault root, as a string."""
    return str(p.relative_to(config.vault_root().expanduser().resolve()))


def _note_lock_name(p: Path) -> str:
    rel = unicodedata.normalize("NFC", vault_rel(p)).casefold()
    return f"{hashlib.sha256(rel.encode()).hexdigest()}.lock"


@contextlib.contextmanager
def _hold_note_lock_file(name: str):
    lock_dir = config.data_home() / "locks" / "vault"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_dir / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# --- frontmatter ``locked: true`` verdict (moved from tools.vault, #2091) ----

# Frontmatter lives at the head of a note, so reading the whole file to inspect
# it is wasted IO — a journal or index note runs to hundreds of KiB and the block
# is a few hundred bytes. This bound is what gets read instead. A block larger
# than this reads as unverifiable, so the guard falls back to a larger read
# rather than guessing (see note_is_locked).
_FRONTMATTER_MAX_BYTES = 8192

# Lock verdict, keyed by path and stamped with the identity of the bytes it
# came from: (mtime_ns, size, ctime_ns, inode). A write changes at least the
# first two and an in-place replacement the last two, so a stale entry cannot be
# served — the cache never needs explicit invalidation, and a note edited in
# Obsidian mid-session is picked up on its next read.
_frontmatter_cache: dict[str, tuple[int, int, int, int, bool]] = {}
_FRONTMATTER_CACHE_MAX = 512

# Largest head the lock check will read to find a closing delimiter. A note
# whose frontmatter is still unterminated past this is treated as locked.
_LOCK_READ_CEILING = 1024 * 1024


def _lock_verdict(head: bytes, limit: int, *, final: bool) -> bool | None:
    """Verdict for a head read, or None when a larger read could still decide it."""
    state, reason = vault_lock_state(head, limit)
    if len(head) >= limit and state == LOCK_UNVERIFIABLE and reason == LOCK_REASON_BOUNDED:
        if not final:
            return None
        # Still cut off at the ceiling: a guard we cannot see fails closed.
        return True
    return vault_head_is_locked(head, limit)


_LOCK_READ_SIZES = (_FRONTMATTER_MAX_BYTES, VAULT_LOCK_HEAD_BYTES, _LOCK_READ_CEILING)


def _lock_verdict_from(read: Callable[[int], bytes]) -> bool:
    """Widen the head read until a verdict holds; fail closed at the ceiling."""
    for n, limit in enumerate(_LOCK_READ_SIZES):
        verdict = _lock_verdict(read(limit), limit, final=n == len(_LOCK_READ_SIZES) - 1)
        if verdict is not None:
            return verdict
    return True  # unreachable: the final read always decides


def note_bytes_are_locked(data: bytes) -> bool:
    """`note_is_locked` for a note's bytes already in hand: the same widening
    reads and fail-closed ceiling, with no second open of the file. A caller
    that has read a note safely decides its lock from those bytes (#2212)."""
    return _lock_verdict_from(lambda limit: data[:limit])


def note_is_locked(p: Path) -> bool:
    """True if the note at ``p`` has frontmatter ``locked: true``; False if absent.

    The same verdict ``vault_write`` refuses on: it reads only the note's head,
    widening the read rather than reporting "unlocked" on a block cut off by the
    bound, and caches the result per file identity. A note whose lock cannot be
    read raises ``ToolError``: a guard that cannot be read must not read as
    unlocked to a writer. A FIFO, socket or device at ``p`` is such a note: it
    is opened without blocking and refused once ``fstat`` shows it is not a
    regular file, since a blocking open of a FIFO never returns (#2212).
    """
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            raise _NotRegularNote(p)
        key = str(p)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino)
        cached = _frontmatter_cache.get(key)
        if cached is not None and cached[:4] == stamp:
            return cached[4]

        # Read the cheap head first. A block cut off by the bound may simply
        # close further down, so widen the read rather than report "unlocked"
        # on a note whose guard we merely failed to see.
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
        with open(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise _NotRegularNote(p)

            def read(limit: int) -> bytes:
                f.seek(0)
                return f.read(limit)

            locked = _lock_verdict_from(read)

        if len(_frontmatter_cache) >= _FRONTMATTER_CACHE_MAX:
            _frontmatter_cache.clear()
        _frontmatter_cache[key] = (*stamp, locked)
        return locked
    except (FileNotFoundError, NotADirectoryError):
        return False
    except _NotRegularNote as exc:
        raise ToolError(
            f"cannot read note {p.name!r} to check its lock: it is not a regular file "
            "(a FIFO, socket, device or directory); move it aside and retry"
        ) from exc
    except OSError as exc:
        raise ToolError(
            f"cannot read note {p.name!r} to check its lock: {exc}; fix the file's "
            "permissions (the hub needs read access) and retry"
        ) from exc


class _NotRegularNote(OSError):
    """`note_is_locked` found something other than a regular file at the path."""

    def __init__(self, p: Path) -> None:
        super().__init__(f"{p.name!r} is not a regular file")


#: Siblings ``unlocked_note_slot`` tries past the target before refusing. The
#: first sibling is ``<stem>-2``, so the last name tried is ``<stem>-21``.
MAX_LOCKED_SIBLINGS = 20


def _sibling(target: Path, n: int) -> Path:
    """``target`` with ``-n`` before its suffix, re-resolved through the vault chokepoint."""
    name = f"{target.stem}-{n}{target.suffix}"
    return resolve_in_vault(vault_rel(target.parent / name), for_write=True)


def _candidates(target: Path, max_siblings: int) -> Iterator[Path]:
    """``target``, then ``<stem>-2`` ... ``<stem>-{max_siblings + 1}``, resolved lazily."""
    yield target
    for n in range(2, max_siblings + 2):
        yield _sibling(target, n)


def _family_lock_name(target: Path) -> str:
    """Lock-file name for the sibling family of ``target`` (``<stem>.md``, ``<stem>-N.md``).

    Prefixed so it can never equal a per-note lock name, which is a bare sha256.
    """
    return f"family-{_note_lock_name(target)}"


@contextlib.contextmanager
def note_slot(
    target: Path,
    *,
    holds: Callable[[Path], bool] | None = None,
    max_siblings: int | None = None,
) -> Iterator[tuple[Path, bool]]:
    """Hold the note lock on the note to write, or on one that already holds the content.

    Yields ``(path, already)``. The whole scan and placement run under one
    family lock keyed by ``target`` (``_family_lock_name``), so two generated
    writers on the same family cannot interleave: the second waits until the
    first has written and released, then sees its note (#2091, review round 2).
    Lock order is always family first, then one per-note lock at a time, the
    same per-note lock ``vault_write`` and the other note writers take on their
    own; nothing takes the family lock while holding a per-note lock, so there
    is no lock-order inversion. ``vault_write`` does not take the family lock:
    an ordinary write can still change a candidate between the scan and the
    placement, which the re-checks below cover.

    With ``holds`` (a "does this note already hold what I would write?" check,
    e.g. a fingerprint compare), every existing candidate, ``target`` and its
    siblings ``<stem>-2`` through ``<stem>-{max_siblings + 1}``, locked or not,
    is checked first, each under its own note lock; the first match is yielded
    with ``already=True`` and its lock held, so a proposal filed to a sibling
    while the original was locked is found again rather than filed twice.
    Otherwise this behaves as ``unlocked_note_slot`` and yields ``(slot,
    False)``. During placement ``holds`` is checked again under each
    candidate's lock before a locked candidate is skipped and on the chosen
    slot, whether or not it exists, so ``holds`` must answer False for an
    absent note. Errors from ``holds`` propagate.
    """
    limit = MAX_LOCKED_SIBLINGS if max_siblings is None else max_siblings
    with _hold_note_lock_file(_family_lock_name(target)):
        if holds is not None:
            for candidate in _candidates(target, limit):
                # An absent name holds nothing; skipping it unlocked spares a
                # lock file per unused sibling. The family lock keeps generated
                # writers out until placement, which re-checks every candidate.
                if not candidate.exists():
                    continue
                with note_write_lock(candidate):
                    if holds(candidate):
                        yield candidate, True
                        return
        for candidate in _candidates(target, limit):
            with note_write_lock(candidate):
                already = holds is not None and holds(candidate)
                if already or not note_is_locked(candidate):
                    yield candidate, already
                    return
    raise VaultLockedError(
        f"note {vault_rel(target)!r} is locked (frontmatter `locked: true`)"
        + (f" and so are its {limit} siblings, -2 to -{limit + 1}" if limit else "")
        + "; the hub's generated writers never overwrite a locked note. Unlock it "
        "or move it aside, then rerun"
    )


@contextlib.contextmanager
def unlocked_note_slot(target: Path, *, max_siblings: int | None = None) -> Iterator[Path]:
    """Hold the note lock on the first of ``target``, ``<stem>-2``, ... that is not locked.

    For the hub's generated-note writers (#2091): ``target`` is a note the
    writer would otherwise overwrite. Under each candidate's note lock its
    ``locked: true`` frontmatter is read; a locked candidate is released and
    the next sibling tried, so a human-locked note is never rewritten and the
    check and the caller's write are one critical section. The yielded path is
    held until the ``with`` block exits; the caller writes it and reports it,
    so a sibling is never a silent substitute. An unlocked existing candidate
    is yielded as-is: the writer keeps whatever overwrite semantics it had for
    an unlocked note. Siblings resolve through ``safety.resolve_in_vault``, so
    they stay inside the write allowlist.

    ``max_siblings`` (default ``MAX_LOCKED_SIBLINGS``, 20) counts siblings, not
    the suffix: they run ``<stem>-2`` through ``<stem>-{max_siblings + 1}``.
    With ``max_siblings=0``, or once every sibling is locked too, it raises
    ``VaultLockedError`` naming the locked target. A candidate whose lock cannot
    be read, or a sibling the vault resolver refuses, raises ``ToolError``.
    """
    with note_slot(target, max_siblings=max_siblings) as (slot, _already):
        yield slot
