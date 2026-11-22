"""Local request receipts for controlled execution (task #1938).

A receipt is written, and flushed to disk, before a keyed request is sent. It
holds the idempotency key, the hub and identity it was sent as, the exact
validated arguments and the last known outcome, so a retry after a crash,
timeout or dropped response reuses the same key and the hub can replay instead
of repeating the effect.

Receipts never hold a credential: the client accepts no token as an argument,
and identity fields are refused from input. The directory is private (0700)
and each file 0600. It lives in ``UA_CLI_STATE_DIR`` or
``$XDG_STATE_HOME/unified-agent/cli/receipts`` (default
``~/.local/state/unified-agent/cli/receipts``).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .result import CliError, strict_json

SCHEMA_VERSION = "ua.receipt/1"
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Outcomes after which an identical new request is a new request, not a retry.
SETTLED = frozenset({"committed"})
# Settled receipts older than this are removed; unsettled ones are kept until
# their outcome is known, however old.
RETENTION_DAYS = 30
_PRUNE_LIMIT = 500
_LOCK_WAIT_S = 30.0
_held = threading.local()
_MAX_BYTES = 2 * 1024 * 1024
_DIGEST_RE = re.compile(r"[0-9a-f]{64}|[A-Za-z0-9._:-]{1,128}")


def directory() -> Path:
    explicit = os.environ.get("UA_CLI_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser() / "receipts"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "unified-agent" / "cli" / "receipts"


def index_directory() -> Path:
    """One small file per request fingerprint naming its newest key."""
    return directory() / "index"


def new_key() -> str:
    return f"ua-{uuid.uuid4().hex}"


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise CliError(
            "validation",
            "An idempotency key is 1-128 letters, digits, '.', '_', ':' or '-'.",
            2,
        )
    return key


def request_digest(hub: str, identity: str, tool: str, arguments: dict) -> str:
    canonical = json.dumps(
        {"hub": hub, "identity": identity, "tool": tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def path_for(key: str) -> Path:
    return directory() / f"{validate_key(key)}.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _atomic_write(folder: Path, target: Path, encoded: bytes) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    directory_fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _index_path(digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise CliError("invalid_receipt", "The local receipt has no valid request digest.", 2)
    return index_directory() / f"{digest}.json"


@contextlib.contextmanager
def fingerprint_lock(digest: str, *, blocking: bool = True):
    """Exclusive per-request-fingerprint lock across processes and threads.

    ``flock`` on ``index/<digest>.lock``. Retry lookup, key minting and index
    publication happen under it, so two overlapping identical commands agree on
    one key; prune takes it too. Re-entrant within one thread. Lock files are
    never removed: unlinking a lock file another process holds would split it.
    Yields False only when ``blocking`` is False and the lock is busy.
    """
    path = _index_path(digest).with_suffix(".lock")
    held = getattr(_held, "digests", None)
    if held is None:
        held = _held.digests = set()
    if digest in held:
        yield True
        return
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking:
                    yield False
                    return
                if time.monotonic() >= deadline:
                    raise CliError(
                        "receipt_locked",
                        "Another ua command holds this request's receipt lock; nothing was "
                        "sent. Retry when it finishes.",
                        2,
                    ) from None
                time.sleep(0.05)
        held.add(digest)
        try:
            yield True
        finally:
            held.discard(digest)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_index(index: Path) -> str | None:
    """The key an index entry names; None when absent; raises when unreadable."""
    try:
        raw = index.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _unreadable() from None
    try:
        entry = strict_json(raw[:4096])
        key = entry["idempotency_key"] if isinstance(entry, dict) else None
        return validate_key(key)
    except (ValueError, UnicodeError, RecursionError, KeyError, TypeError, CliError):
        raise _unreadable() from None


def _may_repoint(index: Path, key: str) -> bool:
    """Whether the index may name ``key``: never over another unsettled receipt."""
    try:
        named = _read_index(index)
    except CliError:
        return False  # unreadable: leave it, so retries fail closed
    if named is None or named == key:
        return named is None
    try:
        return load(named).get("state") in SETTLED
    except CliError as error:
        return error.code == "not_found"


BEFORE_SEND = "before_send"
AFTER_SEND = "after_send"


def save(receipt: dict, *, phase: str = BEFORE_SEND) -> Path:
    """Atomically replace the receipt and publish its fingerprint index, both flushed.

    Runs under the fingerprint lock. The index is written before this returns,
    so it exists before the request is sent, and it is never repointed away
    from another receipt that is still unsettled.

    ``phase`` says whether the request has been sent yet. Before sending, a
    failed save means nothing was sent (``receipt_unavailable``). After
    sending, the request reached the hub (or may have), so a failed save is
    ``receipt_not_updated``: the caller still holds the outcome and the key.
    """
    if phase not in {BEFORE_SEND, AFTER_SEND}:
        raise ValueError(f"unknown receipt phase {phase!r}")
    try:
        receipt = {**receipt, "updated_at": _now()}
        target = path_for(receipt["idempotency_key"])
        index = _index_path(receipt["request_digest"])
        with fingerprint_lock(receipt["request_digest"]):
            encoded = json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False).encode()
            _atomic_write(directory(), target, encoded)
            if _may_repoint(index, receipt["idempotency_key"]):
                entry = {
                    "schema_version": SCHEMA_VERSION,
                    "idempotency_key": receipt["idempotency_key"],
                }
                _atomic_write(index.parent, index, json.dumps(entry, sort_keys=True).encode())
    except OSError:
        if phase == AFTER_SEND:
            raise not_updated(receipt["idempotency_key"]) from None
        # Never send a keyed request whose key could be lost on a crash.
        raise CliError(
            "receipt_unavailable",
            "Could not persist the local request receipt; nothing was sent. "
            "Check UA_CLI_STATE_DIR permissions.",
            2,
        ) from None
    except CliError as error:
        if phase == AFTER_SEND and error.code == "receipt_locked":
            raise not_updated(receipt["idempotency_key"]) from None
        raise
    return target


def not_updated(key: str) -> CliError:
    """A receipt save that failed after the request was sent."""
    return CliError(
        "receipt_not_updated",
        f"The request reached the hub, but its local receipt could not be updated. Keep "
        f"the idempotency key {key}: ua reconcile {key} or a rerun with "
        f"--idempotency-key {key} recovers the outcome without a duplicate. Check "
        "UA_CLI_STATE_DIR permissions.",
        2,
        reference={"idempotency_key": key},
    )


def reserve(digest: str, make, *, force_new: bool = False) -> tuple[dict, bool]:
    """Find the unsettled key for this request or mint and publish one, atomically.

    Returns ``(receipt, reused)``. ``make(key)`` builds a new receipt. The
    lookup and the publication of a new key share one fingerprint lock, so an
    overlapping identical command reuses this key instead of minting another.
    """
    with fingerprint_lock(digest):
        found = None if force_new else find_retry(digest)
        if found is not None:
            return found, True
        receipt = make(new_key())
        save(receipt)
        return receipt, False


def load(key: str) -> dict:
    target = path_for(key)
    try:
        with target.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("oversized")
        receipt = strict_json(raw)
    except FileNotFoundError:
        raise CliError(
            "not_found", "No local receipt for this key; use a lookup command instead.", 2
        ) from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CliError("invalid_receipt", "The local receipt is unreadable.", 2) from None
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("idempotency_key") != key
        or not isinstance(receipt.get("arguments"), dict)
    ):
        raise CliError("invalid_receipt", "The local receipt is unreadable.", 2)
    return receipt


def prepare(*, hub: str, identity: str, command: str, tool: str, arguments: dict, key: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "idempotency_key": key,
        "command": command,
        "tool": tool,
        "hub": hub,
        "identity": identity,
        "arguments": arguments,
        "request_digest": request_digest(hub, identity, tool, arguments),
        "state": "prepared",
        "created_at": _now(),
    }


def _unreadable() -> CliError:
    return CliError(
        "receipt_unreadable",
        "A local receipt for an identical request is unreadable, so its retry key is "
        "unknown and nothing was sent. Pass --idempotency-key with the original key, or "
        "--new-key to start a separate request.",
        2,
    )


def find_retry(digest: str) -> dict | None:
    """The unsettled receipt for an identical request, found by its fingerprint.

    An identical command after an unknown, conflicting or parked outcome is a
    retry of that request, so it reuses its key rather than minting a new one.
    The lookup is one index read. An index entry that cannot be read, or that
    names a receipt that cannot be read, fails closed: a fresh key here could
    create a second delegation.
    """
    key = _read_index(_index_path(digest))
    if key is None:
        return None
    try:
        receipt = load(key)
    except CliError:
        raise _unreadable() from None
    if receipt.get("request_digest") != digest:
        raise _unreadable()
    return None if receipt.get("state") in SETTLED else receipt


def prune(now: float | None = None) -> int:
    """Remove settled receipts older than RETENTION_DAYS; bounded per call.

    Each removal holds that request's fingerprint lock and re-reads the
    receipt and index under it: the index is unlinked only while it still
    names the committed receipt being removed, so a concurrent save that just
    published a new in-flight key keeps its index. A busy lock is skipped.
    The index goes first, so a crash in between leaves a settled receipt and
    never an index naming a missing unsettled one. Unreadable files are left.
    """
    folder = directory()
    if not folder.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_DAYS * 86400
    removed = 0
    for checked, entry in enumerate(folder.glob("*.json")):
        if checked >= _PRUNE_LIMIT:
            break
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            digest = load(entry.stem).get("request_digest")
            with fingerprint_lock(digest, blocking=False) as locked:
                if not locked:
                    continue
                receipt = load(entry.stem)
                if receipt.get("state") not in SETTLED or entry.stat().st_mtime >= cutoff:
                    continue
                index = _index_path(digest)
                if _read_index(index) == entry.stem:
                    index.unlink()
                entry.unlink()
                removed += 1
        except (OSError, CliError):
            continue
    return removed


def public(receipt: dict, target: Path | None = None) -> dict[str, Any]:
    """The reference printed to the caller: no arguments, no request body."""
    view = {
        "idempotency_key": receipt["idempotency_key"],
        "state": receipt.get("state"),
        "receipt": str(target or path_for(receipt["idempotency_key"])),
    }
    for field in ("delegation_id", "task_id", "approval_id", "uri"):
        if receipt.get(field) is not None:
            view[field] = receipt[field]
    return view
