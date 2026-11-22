"""Read-only discovery of native Claude Code and Codex sessions on a node (task #1741).

A ua-node lists harness sessions that were started outside the hub so the
operator can adopt one from the Sessions pane. Discovery reports metadata
only: harness, session id, cwd (as recorded and fully resolved), last activity
and a title when one is cheap to find. Transcript contents never leave the device: a bounded prefix of each
session file is parsed for the fields below and everything else is dropped
on the spot.

Rules, all enforced here on the device (the hub re-checks the cwd against the
node's reported roots):

* A session is listed only when its cwd, fully resolved (symlinks followed,
  ``..`` collapsed), is a worktree root or inside one. A cwd that is a
  symlink out of a root, or spells ``root/../elsewhere``, is not listed.
* Session files are read only from the harness's own store
  (``$CLAUDE_CONFIG_DIR/projects`` or ``~/.claude/projects``;
  ``$CODEX_HOME/sessions`` or ``~/.codex/sessions``; ``UA_NODE_CLAUDE_PROJECTS``
  and ``UA_NODE_CODEX_HOME`` override both). A file in the store that
  is a symlink resolving outside it is skipped.
* Every read is capped (lines, bytes, files, results), and a malformed file
  is skipped rather than failing discovery.

Layouts (learned from file names and record keys, not from any content):

* Claude Code: ``projects/<encoded cwd>/<session uuid>.jsonl``; records carry
  ``sessionId`` and ``cwd``; a ``custom-title`` record carries the
  ``customTitle`` the user set, the only title reported. ``summary`` records
  are derived from the conversation and are never read for a title.
* Codex: ``sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``; the first
  record is ``session_meta`` whose ``payload`` has ``id`` and ``cwd``. Thread
  names in ``session_index.jsonl`` are not reported: nothing in the CLI shows
  whether a name was set by the user or generated from the conversation.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HARNESSES = ("claude", "codex")
#: Newest sessions reported per scan; keeps the heartbeat fact small.
MAX_RESULTS = 50
#: Session files examined per harness per scan, newest first.
MAX_FILES = 200
#: Claude records read from the head of one session file.
MAX_HEAD_LINES = 60
#: Bytes read from the head of one session file (Codex session_meta included).
MAX_HEAD_BYTES = 128 * 1024
MAX_TITLE_CHARS = 120
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
#: A session written to more recently than this may still be open in a
#: terminal; adopting it would put two writers on one session file. The hub
#: checks it against the last scan, and the node again just before it spawns.
DEFAULT_ADOPT_QUIET_S = 120.0


def adopt_quiet_s() -> float:
    """``UA_NODE_ADOPT_QUIET_S`` (default 120; 0 turns the check off)."""
    raw = os.environ.get("UA_NODE_ADOPT_QUIET_S", "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_ADOPT_QUIET_S
    except ValueError:
        return DEFAULT_ADOPT_QUIET_S


def session_file(
    harness: str,
    session_id: str,
    *,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
) -> Path | None:
    """The store file of one native session, or None. Only inside the store."""
    if not SESSION_ID_RE.fullmatch(str(session_id)):
        return None
    if harness == "claude":
        store = claude_dir if claude_dir is not None else claude_store()
        candidates = store.glob(f"*/{session_id}.jsonl")
    elif harness == "codex":
        store = (codex_dir if codex_dir is not None else codex_home()) / "sessions"
        candidates = store.glob(f"*/*/*/rollout-*-{session_id}.jsonl")
    else:
        return None
    store_real = _real(store)
    if store_real is None:
        return None
    try:
        for path in candidates:
            if _inside(path, store_real):
                return path
    except OSError:
        return None
    return None


def claude_store() -> Path:
    """``UA_NODE_CLAUDE_PROJECTS``, else ``$CLAUDE_CONFIG_DIR/projects``, else ``~/.claude/projects``."""
    override = os.environ.get("UA_NODE_CLAUDE_PROJECTS")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / "projects"


def codex_home() -> Path:
    """``UA_NODE_CODEX_HOME``, else ``$CODEX_HOME``, else ``~/.codex``."""
    raw = os.environ.get("UA_NODE_CODEX_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(raw).expanduser()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _real(path: Path) -> Path | None:
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None


def resolve_in_roots(cwd: Any, roots: Iterable[Path]) -> str | None:
    """The fully resolved ``cwd`` when it is a root or inside one, else None.

    ``realpath`` follows every symlink and collapses ``..``, and the test is
    by path components, so ``/a/dev-other`` is not inside ``/a/dev``.
    """
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        return None
    target = _real(Path(cwd))
    if target is None:
        return None
    for root in roots:
        root_real = _real(Path(root).expanduser())
        if root_real is None:
            continue
        if target == root_real or root_real in target.parents:
            return str(target)
    return None


def _inside(path: Path, store_real: Path) -> bool:
    real = _real(path)
    return real is not None and store_real in real.parents


def _newest_files(paths: Iterable[Path]) -> list[tuple[float, Path]]:
    found: list[tuple[float, Path]] = []
    for path in paths:
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda entry: entry[0], reverse=True)
    return found[:MAX_FILES]


def _head_records(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    """Up to ``max_lines`` JSON records from the file head, byte-capped."""
    out: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_HEAD_BYTES)
    except OSError:
        return out
    for line in raw.split(b"\n")[:max_lines]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # The last line of a capped read is usually cut mid-record.
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_TITLE_CHARS] or None


def _claude_meta(path: Path) -> dict[str, Any] | None:
    session_id = cwd = title = None
    for record in _head_records(path, max_lines=MAX_HEAD_LINES):
        if session_id is None and isinstance(record.get("sessionId"), str):
            session_id = record["sessionId"]
        if cwd is None and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        # Only a title the user set: a ``summary`` record is
        # derived from the conversation, so it never leaves the device.
        if record.get("type") == "custom-title":
            title = _title(record.get("customTitle")) or title
    # The file is named for its session; a record naming another id is not this one.
    if session_id is None or session_id != path.stem:
        return None
    return {"external_session_id": session_id, "cwd": cwd, "title": title}


def _codex_meta(path: Path) -> dict[str, Any] | None:
    records = _head_records(path, max_lines=1)
    if not records or records[0].get("type") != "session_meta":
        return None
    payload = records[0].get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id")
    if not isinstance(session_id, str) or not path.stem.endswith(session_id):
        return None
    return {"external_session_id": session_id, "cwd": payload.get("cwd"), "title": None}


def _scan(
    harness: str,
    files: list[tuple[float, Path]],
    store_real: Path,
    roots: list[Path],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reader = _claude_meta if harness == "claude" else _codex_meta
    # When this view was taken: the hub says how stale it is (#1741 review).
    scanned_at = _iso(time.time())
    for mtime, path in files:
        if len(out) >= limit:
            # Files are newest first, so nothing later can make the cut.
            break
        if not _inside(path, store_real):
            continue
        meta = reader(path)
        if meta is None or not SESSION_ID_RE.fullmatch(meta["external_session_id"]):
            continue
        cwd = resolve_in_roots(meta["cwd"], roots)
        if cwd is None:
            continue
        title = meta["title"]
        out.append(
            {
                "harness": harness,
                "external_session_id": meta["external_session_id"],
                # The cwd as the session recorded it, and fully resolved: the
                # hub cannot resolve node paths, so it checks the resolved one.
                "cwd": meta["cwd"],
                "cwd_realpath": cwd,
                "last_activity": _iso(mtime),
                "scanned_at": scanned_at,
                "title": title,
            }
        )
    return out


def discover(
    roots: Iterable[Path],
    *,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    limit: int = MAX_RESULTS,
) -> list[dict[str, Any]]:
    """Native sessions whose cwd is inside ``roots``, newest first. Never raises."""
    root_list = [Path(r).expanduser() for r in roots]
    if not root_list:
        return []
    found: list[dict[str, Any]] = []
    store = claude_dir if claude_dir is not None else claude_store()
    store_real = _real(store)
    if store_real is not None and store_real.is_dir():
        try:
            files = _newest_files(
                p
                for project in store.iterdir()
                if project.is_dir()
                for p in project.glob("*.jsonl")
            )
        except OSError:
            files = []
        found.extend(_scan("claude", files, store_real, root_list, limit=limit))
    home = codex_dir if codex_dir is not None else codex_home()
    sessions = home / "sessions"
    sessions_real = _real(sessions)
    if sessions_real is not None and sessions_real.is_dir():
        try:
            files = _newest_files(sessions.glob("*/*/*/rollout-*.jsonl"))
        except OSError:
            files = []
        found.extend(_scan("codex", files, sessions_real, root_list, limit=limit))
    found.sort(key=lambda row: row["last_activity"], reverse=True)
    return found[: max(0, int(limit))]
