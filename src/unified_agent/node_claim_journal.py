"""The ua-node worker's durable claim journal (task #2136). Stdlib only.

One small JSON file per (hub, node) records the logical claim operation the
worker has outstanding, so a claim whose response was lost is reconciled with
the hub instead of followed by a fresh claim that could take a second item.

States, in order:

* ``pending``       an operation id was published before the claim was sent;
  the hub may or may not have bound it to an item;
* ``ready``         the hub bound the operation to one item attempt, and this
  worker has not started it;
* ``start_intent``  written immediately BEFORE the backend is invoked.

The file is removed (with a directory fsync) once the operation is closed
without work, or once the executed item's receipt is durably spooled.

Rules the worker follows, all fail-closed:

* Every read, reconciliation and ``ready -> start_intent`` transition happens
  under an exclusive ``flock`` on a sibling lock file, held from the journal
  read until the journal is cleared. A second worker process for the same hub
  and node finds the lock busy and claims nothing. The lock is re-read after
  acquisition; a state read before it is never trusted.
* ``start_intent`` with no locally established outcome (a spooled receipt for
  that attempt) is ambiguous: the backend may or may not have started. The
  worker HOLDS: it neither relaunches the item nor claims new work until the
  operator resolves it and removes the journal file.
* A journal that cannot be published, or one that is corrupt or names another
  hub or node, stops claiming instead of being silently replaced.

A process lock does not prove that an earlier backend process died, and a
lost or replaced journal file cannot be told apart from a first start; both
limits are documented in docs/cli-reference.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # POSIX only; without it the journal refuses to lock (fail closed).
    import fcntl
except ImportError:  # pragma: no cover - ua-node runs on Linux and macOS
    fcntl = None  # type: ignore[assignment]

#: Same string as the hub's ``node_claim_ops.FEATURE``; kept here so the node
#: does not import the hub-side module.
FEATURE = "node_claim_op_v1"
VERSION = 1
STATES = frozenset({"pending", "ready", "start_intent"})
_OP_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ITEM_RE = re.compile(r"^nd-[0-9a-f]{16}$")
MAX_JOURNAL_BYTES = 16_384


class JournalError(RuntimeError):
    """The journal cannot be trusted or written: stop claiming, never guess."""


class JournalBusy(RuntimeError):
    """Another worker process holds this journal's lock."""


def journal_key(hub: str, node: str) -> str:
    return hashlib.sha256(f"{hub}\n{node}".encode()).hexdigest()[:32]


def new_op_id() -> str:
    return secrets.token_hex(16)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_durable(path: Path, payload: dict[str, Any]) -> None:
    """Atomic, durable 0600 JSON replace: file fsync, rename, directory fsync.

    Raises ``OSError``; nothing is published unless the whole write succeeded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class ClaimJournal:
    """The journal file and its lock for one (hub, node) pair."""

    def __init__(self, directory: Path, *, hub: str, node: str):
        self.hub = hub
        self.node = node
        self.directory = directory
        key = journal_key(hub, node)
        self.path = directory / f"{key}.json"
        self.lock_path = directory / f"{key}.lock"
        self._lock_fd: int | None = None

    # -- locking --

    @contextlib.contextmanager
    def locked(self) -> Iterator[ClaimJournal]:
        """Hold the exclusive journal lock; ``JournalBusy`` if another process has it."""
        if fcntl is None:
            raise JournalError("claim journal locking is unavailable on this platform")
        if self._lock_fd is not None:
            raise JournalError("claim journal lock is already held by this worker")
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise JournalError(f"cannot open claim journal lock {self.lock_path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise JournalBusy(f"another ua-node process holds {self.lock_path}") from exc
        except OSError as exc:
            os.close(fd)
            raise JournalError(f"cannot lock claim journal {self.lock_path}: {exc}") from exc
        self._lock_fd = fd
        try:
            yield self
        finally:
            self._lock_fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _require_lock(self) -> None:
        if self._lock_fd is None:
            raise JournalError("claim journal used without its lock")

    # -- reads --

    def load(self) -> dict[str, Any] | None:
        """The outstanding operation, None when there is none. Never repairs."""
        self._require_lock()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise JournalError(f"cannot read claim journal {self.path}: {exc}") from exc
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError(f"claim journal {self.path} is oversized")
        try:
            entry = json.loads(raw)
        except ValueError as exc:
            raise JournalError(f"claim journal {self.path} is corrupt") from exc
        self._validate(entry)
        return entry

    def _validate(self, entry: Any) -> None:
        where = f"claim journal {self.path}"
        if not isinstance(entry, dict) or entry.get("version") != VERSION:
            raise JournalError(f"{where} has an unknown format")
        if entry.get("hub") != self.hub or entry.get("node") != self.node:
            raise JournalError(f"{where} belongs to another hub or node")
        if entry.get("state") not in STATES:
            raise JournalError(f"{where} has an unknown state")
        if not isinstance(entry.get("op_id"), str) or not _OP_ID_RE.fullmatch(entry["op_id"]):
            raise JournalError(f"{where} has a malformed operation id")
        if type(entry.get("issued_at")) is not int:
            raise JournalError(f"{where} has a malformed issued_at")
        if entry["state"] != "pending":
            item_id, attempt = entry.get("item_id"), entry.get("attempt")
            if not isinstance(item_id, str) or not _ITEM_RE.fullmatch(item_id):
                raise JournalError(f"{where} has a malformed item id")
            if type(attempt) is not int or attempt < 1:
                raise JournalError(f"{where} has a malformed attempt")

    # -- writes --

    def _publish(self, entry: dict[str, Any]) -> None:
        """Atomic, durable 0600 replace: file fsync, rename, directory fsync."""
        self._require_lock()
        self._validate(entry)
        try:
            write_private_durable(self.path, entry)
        except OSError as exc:
            raise JournalError(f"cannot write claim journal {self.path}: {exc}") from exc

    def begin(self, *, issued_at: int) -> dict[str, Any]:
        """Publish a new ``pending`` operation; only when none is outstanding."""
        if self.load() is not None:
            raise JournalError(f"claim journal {self.path} already has an operation")
        entry = {
            "version": VERSION,
            "hub": self.hub,
            "node": self.node,
            "op_id": new_op_id(),
            "issued_at": int(issued_at),
            "state": "pending",
        }
        self._publish(entry)
        return entry

    def _transition(
        self, entry: dict[str, Any], *, expect: tuple[str, ...], state: str, **fields: Any
    ) -> dict[str, Any]:
        """Compare-and-set on the durable state under the lock."""
        current = self.load()
        if current is None or current["op_id"] != entry["op_id"] or current["state"] not in expect:
            raise JournalError(
                f"claim journal {self.path} changed underneath this worker; refusing to continue"
            )
        updated = {**current, **fields, "state": state}
        self._publish(updated)
        return updated

    def mark_ready(self, entry: dict[str, Any], *, item_id: str, attempt: int) -> dict[str, Any]:
        if entry["state"] == "ready":
            if (entry.get("item_id"), entry.get("attempt")) != (item_id, attempt):
                raise JournalError(f"claim journal {self.path} is bound to another item")
            return entry
        return self._transition(
            entry, expect=("pending",), state="ready", item_id=item_id, attempt=attempt
        )

    def mark_start_intent(self, entry: dict[str, Any]) -> dict[str, Any]:
        return self._transition(entry, expect=("ready",), state="start_intent")

    def clear(self, entry: dict[str, Any]) -> None:
        """Remove the journal durably; only the operation this worker holds."""
        current = self.load()
        if current is None:
            return
        if current["op_id"] != entry["op_id"]:
            raise JournalError(f"claim journal {self.path} holds another operation")
        try:
            self.path.unlink()
            _fsync_dir(self.directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalError(f"cannot clear claim journal {self.path}: {exc}") from exc
