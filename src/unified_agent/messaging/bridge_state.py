"""Cursor and idempotency file shared by the messaging bridges (task #1863).

One small JSON file (``config.bridge_state_path()``) records what each bridge
has already carried, so a re-run never sends twice. The hub side is idempotent
on its own as well (bridge message ids are derived, and ``send_message`` dedupes
on ``message_id``); this file saves the work and keeps the cursors.

Writes are atomic (temp file plus ``os.replace``), and a run holds an exclusive
lock on a sibling ``.lock`` file so two runs cannot interleave.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import config

STATE_VERSION = 1


class BridgeBusy(RuntimeError):
    """Another bridge run holds the lock."""


def derive_id(prefix: str, *parts: str) -> str:
    """A stable message id: ``<prefix>-`` plus 24 hex chars of a SHA-256 over ``parts``."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class BridgeCapacityError(RuntimeError):
    """The hub message cap is too small to carry the bridge header and some text."""


# The smallest hub message cap the bridges run with. Their headers are a few
# hundred bytes; below this a relayed message would be mostly header.
MIN_MESSAGE_BYTES = 1024
# The least text a relayed message may carry after its header.
MIN_TEXT_BYTES = 64


def check_capacity() -> None:
    """Refuse to run when ``UA_MESSAGE_MAX_BYTES`` cannot carry a relayed message."""
    total = config.message_max_body_bytes()
    if total < MIN_MESSAGE_BYTES:
        raise BridgeCapacityError(
            f"UA_MESSAGE_MAX_BYTES is {total}; the messaging bridges need at least "
            f"{MIN_MESSAGE_BYTES} bytes to carry their untrusted header and the text"
        )


def cap_text(text: str, max_bytes: int, note: str) -> tuple[str, bool]:
    """Truncate ``text`` so the result, note included, is at most ``max_bytes`` bytes.

    Cuts on a UTF-8 character boundary. Returns the text and whether it was cut; a
    cut text ends with ``note`` and the number of bytes left out.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    # The omitted count is at most len(encoded), so its digits bound the suffix.
    reserve = len(f"\n[truncated: {len(encoded)} bytes omitted; {note}]".encode())
    if max_bytes - reserve < 0:
        raise BridgeCapacityError(f"{max_bytes} bytes cannot hold the truncation note")
    kept = encoded[: max_bytes - reserve].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(kept.encode("utf-8"))
    return f"{kept}\n[truncated: {omitted} bytes omitted; {note}]", True


def fit_body(header: str, text: str, note: str) -> tuple[str, bool]:
    """``header + text`` cut so the whole body fits one hub message.

    The text budget is ``config.bridge_max_text_bytes()`` or whatever
    ``UA_MESSAGE_MAX_BYTES`` leaves after the header, whichever is smaller.
    """
    total = config.message_max_body_bytes()
    room = total - len(header.encode("utf-8"))
    budget = min(config.bridge_max_text_bytes(), room)
    if budget < MIN_TEXT_BYTES:
        raise BridgeCapacityError(
            f"UA_MESSAGE_MAX_BYTES is {total}; after the {total - room}-byte bridge "
            f"header fewer than {MIN_TEXT_BYTES} bytes are left for the text"
        )
    capped, cut = cap_text(text, budget, note)
    return header + capped, cut


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        # entry key -> hub message id, for thread entries sent to the hub
        "brain_sent": {},
        # hub message id -> thread id, for hub replies appended to a thread
        "brain_relayed": {},
        # thread id -> last hub seq scanned in its brain:<id> conversation
        "brain_hub_cursor": {},
        # hub label -> last seq scanned in its mailbox
        "a2a_cursor": {},
        # hub message id -> {"state": ..., "reply_id": ..., "seq": ...}
        "a2a_forwarded": {},
        # hub message id -> a gateway answer the hub has not accepted yet
        "a2a_pending": {},
        # hub message id -> transient forward failures so far
        "a2a_transient": {},
    }


def load(path: Path | None = None) -> dict[str, Any]:
    target = path or config.bridge_state_path()
    state = empty_state()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"bridge state {target} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"bridge state {target} is not a JSON object")
    for key, default in state.items():
        value = raw.get(key, default)
        if isinstance(default, dict) and not isinstance(value, dict):
            raise RuntimeError(f"bridge state {target}: {key} must be an object")
        state[key] = value
    return state


def save(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or config.bridge_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bridge-state-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: Path | None = None) -> Iterator[None]:
    """Hold the bridge run lock, or raise ``BridgeBusy`` at once."""
    target = path or config.bridge_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with open(lock_path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeBusy(f"another bridge run holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
