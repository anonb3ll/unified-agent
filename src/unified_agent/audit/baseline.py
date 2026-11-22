"""Operator baseline of accepted secret findings, by value hash (task #2323).

An operator who has looked at a SECRETS-KEY finding in an agent's own home
(a fixture JWK an agent read into its session log, say) records it with
``unified-agent-audit baseline accept <root> --reason "..." --yes``. Each
value that makes up the finding is stored as sha256(value) with its detector
(``rule``), the file it was found in (``root`` + relative ``path``), and who
accepted it, when and why. The value itself is never stored or printed.

A run that is given the baseline grades a finding INFO, "accepted (reviewed
<date> by <who>)", only when every value on its line is in the baseline under
the same detector and for the same file, and the file is outside every git
work tree (the engine enforces that; repo source is never accepted). A new
value, the same value under another detector, and the same value in another
file keep their full severity. Only SECRETS-KEY findings are eligible.

What the baseline is: trusted local operator configuration. It is not proof
that a human reviewed anything, and any process running as the same user can
rewrite it. The file (``$UA_HOME/audit-baseline.json``) is JSON Lines: a
header line, then one entry per line, each carrying the digest of its own
canonical form and of the entry before it. Entries are only ever appended
(O_APPEND, under an exclusive lock). The chain detects an edited, reordered
or inserted entry; it cannot detect entries removed from the end, since there
is no external anchor. The file is opened without following a symlink and
checked on the open descriptor: a regular file owned by the effective user,
mode without group or other bits, under the size cap. A file that fails any
check, does not parse, or whose chain does not verify is ignored with a
warning and nothing is downgraded (fail closed); ``accept`` refuses to
append to it.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import config

BASELINE_NAME = "audit-baseline.json"
VERSION = 1
HEADER = {"format": "unified-agent-audit-baseline", "version": VERSION}
RULE_PREFIX = "SECRETS-KEY:"
JWK_RULE = f"{RULE_PREFIX}JWK"
MAX_BYTES = 16 << 20
MAX_REASON = 500
SHOWN_HASH = 12  # hex characters `baseline list` shows
_FIELDS = frozenset({"rule", "sha256", "root", "path", "who", "at", "reason", "prev", "digest"})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile("[\\x00-\\x1f\\x7f  ]+")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW
_APPEND_FLAGS = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW


class BaselineError(ValueError):
    """The baseline file cannot be appended to (damaged, or bad input)."""


def default_path() -> Path:
    return config.data_home() / BASELINE_NAME


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def current_identity() -> str:
    """The verified hub identity when one is bound, else the login name."""
    who = config.verified_agent_identity() or os.environ.get("USER")
    if not who:
        with contextlib.suppress(Exception):
            who = getpass.getuser()
    return _clean(who or "unknown", 120)


def _clean(text: str, limit: int) -> str:
    return _CONTROL.sub(" ", text).strip()[:limit]


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(entry: dict) -> str:
    return value_hash(_canonical({k: v for k, v in entry.items() if k != "digest"}))


def _file_key(root: str, path: str) -> str:
    return os.path.normpath(os.path.join(root, path))


@dataclass
class Baseline:
    path: Path
    entries: list[dict] = field(default_factory=list)
    warning: str | None = None
    _index: dict[str, dict[tuple[str, str], dict]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for entry in self.entries:
            values = self._index.setdefault(_file_key(entry["root"], entry["path"]), {})
            values[(entry["rule"], entry["sha256"])] = entry

    def accepted(self, file: Path | str, values) -> dict | None:
        """The newest entry covering a finding in ``file`` made of ``values``
        ((rule, sha256) pairs), or None unless every value is accepted there."""
        values = set(values)
        known = self._index.get(os.path.normpath(str(file)))
        if not values or not known:
            return None
        if any(
            not rule.startswith(RULE_PREFIX) or (rule, sha) not in known for rule, sha in values
        ):
            return None
        return max((known[v] for v in values), key=lambda entry: entry["at"])

    def holds_file(self, file: Path | str) -> bool:
        """Whether the operator accepted any value in ``file``. A gating finding
        there is then a value that rotated or was added since (task #2419)."""
        return bool(self._index.get(os.path.normpath(str(file))))


def accepted_note(entry: dict) -> str:
    return f"accepted (reviewed {entry['at'][:10]} by {entry['who']})"


def _ignored(path: Path, why: str) -> Baseline:
    return Baseline(path, warning=f"audit baseline {path} ignored: {why}; nothing downgraded")


def _check_entry(entry, prev: str) -> str | None:
    if not isinstance(entry, dict) or set(entry) != _FIELDS:
        return "an entry has missing or unknown fields"
    if not all(isinstance(entry[k], str) for k in _FIELDS):
        return "an entry has a non-string field"
    if not entry["rule"].startswith(RULE_PREFIX) or not _HEX64.fullmatch(entry["sha256"]):
        return "an entry has an invalid rule or hash"
    if not os.path.isabs(entry["root"]) or os.path.isabs(entry["path"]) or not entry["path"]:
        return "an entry has an invalid path"
    if entry["prev"] != prev or entry["digest"] != _digest(entry):
        return "the entry chain does not verify (edited, reordered or inserted)"
    return None


def _descriptor_problem(fd: int) -> str | None:
    """Checks on the open descriptor itself, so the file judged is the file
    read: a path swapped after an earlier check cannot slip through."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_uid != os.geteuid():
        return "not owned by the current user"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return f"mode {stat.S_IMODE(info.st_mode):04o}, expected 0600"
    if info.st_size > MAX_BYTES:
        return "too large"
    return None


def _read_fd(fd: int) -> bytes:
    chunks, total = [], 0
    offset = 0
    while total <= MAX_BYTES:
        chunk = os.pread(fd, 1 << 20, offset)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _parse(data: bytes) -> tuple[list[dict], str | None]:
    """Entries of a baseline's bytes, or a reason it is not one."""
    if len(data) > MAX_BYTES:
        return [], "too large"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], "not UTF-8"
    if not text:
        return [], None
    if not text.endswith("\n"):
        return [], "the last line is incomplete"
    lines = text.split("\n")[:-1]
    try:
        records = [json.loads(line) for line in lines]
    except ValueError:
        return [], "a line is not JSON"
    if not records or records[0] != HEADER:
        return [], "unknown format"
    prev = ""
    for entry in records[1:]:
        problem = _check_entry(entry, prev)
        if problem:
            return [], problem
        prev = entry["digest"]
    return records[1:], None


def load(path: Path | None = None) -> Baseline:
    """Read and verify the baseline. Never raises: a damaged file comes back
    empty with a warning. A missing file is an empty baseline."""
    path = Path(path) if path is not None else default_path()
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return Baseline(path)
    except OSError as exc:  # ELOOP: a symlink; EACCES; ...
        return _ignored(path, f"cannot open without following links ({type(exc).__name__})")
    try:
        problem = _descriptor_problem(fd)
        if problem:
            return _ignored(path, problem)
        entries, problem = _parse(_read_fd(fd))
    except OSError as exc:
        return _ignored(path, f"unreadable ({type(exc).__name__})")
    finally:
        os.close(fd)
    if problem:
        return _ignored(path, problem)
    return Baseline(path, entries)


def _lock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX hosts only
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def append(items, *, root: Path, reason: str, who: str | None = None, path: Path | None = None):
    """Append one entry per new (rule, sha256, file) in ``items`` (dicts with
    ``path`` relative to ``root`` and ``values``, (rule, sha256) pairs).

    The file is opened O_APPEND without following links, locked, and checked
    on the descriptor before anything is read or written. Returns (added,
    total). Refuses (BaselineError) to touch a damaged or foreign file.
    """
    reason = _clean(reason or "", MAX_REASON)
    if not reason:
        raise BaselineError("a non-empty --reason is required")
    path = Path(path) if path is not None else default_path()
    root_text = str(Path(root).resolve())
    who = _clean(who, 120) if who else current_identity()
    at = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, _APPEND_FLAGS, 0o600)
    except OSError as exc:
        raise BaselineError(f"cannot open {path} without following links") from exc
    try:
        _lock(fd)
        problem = _descriptor_problem(fd)
        if problem:
            raise BaselineError(f"audit baseline {path}: {problem}")
        data = _read_fd(fd)
        entries, problem = _parse(data)
        if problem:
            raise BaselineError(f"audit baseline {path}: {problem}")
        seen = {(_file_key(e["root"], e["path"]), e["rule"], e["sha256"]) for e in entries}
        prev = entries[-1]["digest"] if entries else ""
        lines = [] if data else [_canonical(HEADER)]
        added = 0
        for item in items:
            for rule, sha in sorted(item["values"]):
                key = (_file_key(root_text, item["path"]), rule, sha)
                if key in seen or not rule.startswith(RULE_PREFIX):
                    continue
                seen.add(key)
                entry = {
                    "rule": rule,
                    "sha256": sha,
                    "root": root_text,
                    "path": item["path"],
                    "who": who,
                    "at": at,
                    "reason": reason,
                    "prev": prev,
                }
                entry["digest"] = prev = _digest(entry)
                lines.append(_canonical(entry))
                added += 1
        if added:
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            while payload:
                payload = payload[os.write(fd, payload) :]
            os.fsync(fd)
        return added, len(entries) + added
    finally:
        os.close(fd)


def listing(baseline: Baseline) -> list[dict]:
    """Entries for display: hashes truncated, nothing else to hide."""
    return [
        {
            "at": e["at"],
            "who": e["who"],
            "rule": e["rule"],
            "sha256": e["sha256"][:SHOWN_HASH],
            "root": e["root"],
            "path": e["path"],
            "reason": e["reason"],
        }
        for e in baseline.entries
    ]
