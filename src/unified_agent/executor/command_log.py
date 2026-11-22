"""Standalone capped log relay for CommandBackend.

Run by absolute filename in an isolated interpreter: no package imports, hub
credentials or agent-owned reader threads are needed. The relay and command
have independent sessions; losing the node agent does not close the pipe's
reader. EOF (including inherited descendant stdout) owns the relay's lifetime.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import BinaryIO


def _publish(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def _write_all(log: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = log.write(remaining)
        if not written:
            raise OSError("command log write made no progress")
        remaining = remaining[written:]


def relay(log_path: str, state_path: str, cap: int) -> int:
    state = {"ready": True, "truncated": False, "done": False, "error": None}
    path = Path(state_path)
    marker = (
        f"\n[unified-agent] output truncated at {cap} bytes; further output was discarded\n"
    ).encode()
    try:
        with open(log_path, "ab", buffering=0) as log:
            _publish(path, state)
            while chunk := os.read(0, 65536):
                # Keep draining after a disk failure too: do not SIGPIPE or
                # deadlock a running command. The receipt reports the failure.
                if state["error"]:
                    continue
                changed = False
                try:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
                    try:
                        size = os.fstat(log.fileno()).st_size
                        keep = chunk[: max(0, cap - size)]
                        _write_all(log, keep)
                        if len(keep) < len(chunk):
                            # A previous relay may already have appended the
                            # marker. Repeated appends must not add more markers.
                            if size <= cap:
                                _write_all(log, marker)
                            if not state["truncated"]:
                                state["truncated"] = True
                                changed = True
                    finally:
                        if os.name == "posix":
                            fcntl.flock(log.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    state["error"] = str(exc)
                    changed = True
                if changed:
                    try:
                        _publish(path, state)
                    except OSError as exc:
                        # A full/removed state directory must not take the
                        # pipe's reader down with it. Drain to EOF, then fail
                        # the relay exit status if the state cannot be stored.
                        state["error"] = state["error"] or str(exc)
    except OSError as exc:
        state["error"] = str(exc)
    state["done"] = True
    try:
        _publish(path, state)
    except OSError as exc:
        state["error"] = state["error"] or str(exc)
    return 1 if state["error"] else 0


if __name__ == "__main__":
    raise SystemExit(relay(sys.argv[1], sys.argv[2], int(sys.argv[3])))
