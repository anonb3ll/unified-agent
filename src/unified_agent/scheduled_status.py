"""Small run-correlated status records for timer-driven commands."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def _state_dir() -> Path:
    return Path.home() / ".unified_agent"


def _status_path(name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise ValueError("invalid scheduled status name")
    return _state_dir() / f"{name}-status"


def _read_fields(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _atomic_write(path: Path, contents: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        temp_path = Path(stream.name)
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def current_run_status(name: str) -> dict[str, str]:
    """Return the status belonging to the latest published scheduled start."""
    status_path = _status_path(name)
    pointer = _read_fields(status_path)
    run_id = pointer.get("run_id", "")
    prefix = f"{name}-run."
    if pointer.get("kind") != "scheduled" or not run_id.startswith(prefix):
        return {}
    suffix = run_id.removeprefix(prefix)
    if not re.fullmatch(r"[0-9a-f]{32}", suffix):
        return {}
    fields = _read_fields(_state_dir() / run_id / "status")
    if fields.get("kind") != "scheduled" or fields.get("run_id") != run_id:
        return {}
    return fields


def begin_scheduled_run(name: str, run_id: str | None = None) -> str:
    """Publish a fresh started record, or reuse the wrapper's current record."""
    state_dir = _state_dir()
    status_path = _status_path(name)
    state_dir.mkdir(parents=True, exist_ok=True)
    if run_id:
        current = current_run_status(name)
        if current.get("run_id") == run_id and current.get("state") == "started":
            return run_id

    lock_path = state_dir / f"{name}-status.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for old_run in state_dir.glob(f"{name}-run.*"):
            if old_run.is_dir():
                shutil.rmtree(old_run, ignore_errors=True)
        run_id = f"{name}-run.{uuid.uuid4().hex}"
        run_dir = state_dir / run_id
        run_dir.mkdir()
        _atomic_write(
            run_dir / "status",
            f"kind=scheduled\nrun_id={run_id}\nstate=started\n",
        )
        _atomic_write(status_path, f"kind=scheduled\nrun_id={run_id}\n")
        return run_id


def finish_scheduled_run(name: str, run_id: str | None, state: str, exit_code: int) -> bool:
    """Record completion only for this run; never change the latest-run pointer."""
    if not run_id or _read_fields(_status_path(name)).get("run_id") != run_id:
        return False
    run_dir = _state_dir() / run_id
    record = run_dir / "status"
    fields = _read_fields(record)
    if fields.get("run_id") != run_id:
        return False
    try:
        _atomic_write(
            record,
            f"kind=scheduled\nrun_id={run_id}\nstate={state}\nexit_code={exit_code}\n",
        )
    except OSError:
        return False
    return True
