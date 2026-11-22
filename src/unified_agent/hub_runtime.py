"""What code the running hub started from, compared with its checkout (task #1771).

An unrecorded ``git pull`` once moved the live hub checkout while the hub kept
serving the old code for about 2.5 hours. Modules the hub imports lazily would
then have loaded from the newer tree, mixing two versions in one process. The
hub now records the checkout HEAD it started on, and ``system_doctor`` compares
that record with the checkout's current HEAD.

The HEAD is read from the ``.git`` files directly, without running ``git``:
the doctor is a read-only status call and should not spawn processes. A ref
storage this reader does not understand (reftable) reads as unknown, never as
a match.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import config

START_FILE = "hub-start.json"
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def package_checkout() -> Path:
    """Directory the running package was imported from: ``<checkout>/src/unified_agent``."""
    return Path(__file__).resolve().parents[2]


def start_record_path() -> Path:
    return config.data_home() / START_FILE


def _git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.removeprefix("gitdir:").strip())
            target = target if target.is_absolute() else root / target
            return target if target.is_dir() else None
    return None


def _common_dir(git_dir: Path) -> Path:
    try:
        rel = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    common = Path(rel)
    return common if common.is_absolute() else (git_dir / common).resolve()


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    common = _common_dir(git_dir)
    for base in (git_dir, common):
        try:
            value = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        return value if _SHA.match(value) else None
    try:
        packed = (common / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in packed:
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA.match(sha):
            return sha
    return None


def checkout_head(root: Path) -> str | None:
    """The commit ``root``'s HEAD points at, or None when it cannot be read."""
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        return _resolve_ref(git_dir, head.removeprefix("ref:").strip())
    return head if _SHA.match(head) else None


def record_hub_start(root: Path | None = None) -> dict | None:
    """Write the start record. Best effort: a failure never blocks hub startup."""
    checkout = (root or package_checkout()).resolve()
    record = {
        "sha": checkout_head(checkout),
        "checkout": str(checkout),
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    path = start_record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            json.dump(record, stream)
            temp = Path(stream.name)
        os.replace(temp, path)
    except OSError:
        return None
    return record


def read_hub_start() -> dict | None:
    try:
        record = json.loads(start_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True
