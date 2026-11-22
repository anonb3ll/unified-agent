"""Safe merge/write helpers for ``~/.unified_agent/service.env``.

Task #1406: an agent rewrite blanked ``UA_REPLICA_GCS_URI`` and
``UA_BACKUP_GCS_URI``, which took Litestream offline. Callers that rewrite
the file must merge rather than template-clobber, and must refuse to blank
the protected GCS target keys when a non-empty value already exists.
"""

from __future__ import annotations

import os
import secrets
import shlex
from pathlib import Path

# Keys that continuous replication / offsite backup depend on. A rewrite may
# update them to a new non-empty value, but must not replace a live value with
# empty/quoted-empty (the #1406 failure mode).
PROTECTED_GCS_KEYS: frozenset[str] = frozenset(
    {
        "UA_REPLICA_GCS_URI",
        "UA_BACKUP_GCS_URI",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines the way a sourcing shell would."""
    env: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].lstrip()
        key, sep, value = raw.partition("=")
        if not sep:
            continue
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = []
        env[key.strip()] = parts[0] if len(parts) == 1 else value.strip().strip("\"'")
    return env


def parse_env_file_raw(path: Path) -> dict[str, str]:
    """Return each key's assignment text exactly as written (last one wins).

    The counterpart of :func:`parse_env_file` for rewrites that must keep a
    value's shell meaning: ``UA_TRANSPORT=$MODE`` parses to the literal
    ``$MODE``, and re-rendering that value quotes it into a different setting.
    Keys and ``export`` prefixes are read the same way as there.
    """
    raw_values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].lstrip()
        key, sep, value = raw.partition("=")
        if not sep:
            continue
        raw_values[key.strip()] = value
    return raw_values


UNRESOLVED_WORKSPACE_PIN = (
    "cannot resolve UA_WORKSPACE from service.env: export its referenced "
    "variable or pass --workspace to replace this pin; initialization does "
    "not execute shell expressions"
)


def resolve_workspace_pin(raw: str) -> Path:
    """The directory an installed ``UA_WORKSPACE`` pin names, without a shell.

    The one rule ``unified-agent init`` and ``system_doctor``'s
    ``workspace_pin`` row share (#1825): a plain ``$VAR``/``${VAR}`` reference
    is expanded from this process's environment, and anything still
    unresolved (or a command substitution) raises ``ValueError`` rather than
    being mistaken for a literal directory name. The message never carries the
    expression or any variable's value.
    """
    value = raw.strip()
    if "`" not in value:
        value = os.path.expandvars(value)
    if "$" in value or "`" in value:
        raise ValueError(UNRESOLVED_WORKSPACE_PIN)
    return Path(value).expanduser()


def merge_env_updates(
    existing: dict[str, str],
    updates: dict[str, str],
    *,
    protected: frozenset[str] = PROTECTED_GCS_KEYS,
) -> dict[str, str]:
    """Merge ``updates`` into ``existing``, refusing to blank protected keys.

    Protected keys keep their prior non-empty value when the update would set
    them to ``""``. Unprotected keys and new keys follow ``updates`` as-is.
    """
    merged = dict(existing)
    for key, value in updates.items():
        if key in protected and value == "" and (existing.get(key) or "") != "":
            continue
        merged[key] = value
    return merged


def render_env_file(
    values: dict[str, str],
    *,
    header: str | None = None,
    verbatim: dict[str, str] | None = None,
) -> str:
    """Render a deterministic ``KEY=value`` body (sorted keys).

    ``verbatim`` maps keys to assignment text written as-is instead of the
    rendered value: an untouched shell expression carried over from the file
    being rewritten (see :func:`parse_env_file_raw`).
    """
    lines: list[str] = []
    if header:
        lines.append(header.rstrip() + "\n")
    for key in sorted(values):
        if verbatim and key in verbatim:
            lines.append(f"{key}={verbatim[key]}\n")
            continue
        val = values[key]
        # Quote when the value needs it for safe shell sourcing.
        rendered = shlex.quote(val) if val and any(c in val for c in " \t\"'`$\\") else val
        lines.append(f"{key}={rendered}\n")
    return "".join(lines)


def write_env_file(
    path: Path,
    values: dict[str, str],
    *,
    header: str | None = None,
    verbatim: dict[str, str] | None = None,
) -> None:
    """Atomically write ``values`` as a mode-0600 env file."""
    body = render_env_file(values, header=header, verbatim=verbatim)
    _write_env_text(path, body)


def write_env_text(path: Path, body: str) -> None:
    """Publish ``body`` verbatim, atomically and owner-only (see below)."""
    _write_env_text(path, body)


def _write_env_text(path: Path, body: str) -> None:
    """Publish rendered text atomically with owner-only access.

    A symlinked env file is updated through the link: the temp file is built
    beside the resolved target and replaces *it*, so the link survives and
    every reader of the original target sees the new content.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call: concurrent writers never share or clobber a temp file.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    # Born 0600 (O_EXCL, explicit mode): the bearer tokens in this file are
    # never readable by other users, not even before a later chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(body)
        tmp.replace(path)
    except BaseException:
        if fd != -1:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def merge_write_env_file(
    path: Path,
    updates: dict[str, str],
    *,
    header: str | None = None,
    protected: frozenset[str] = PROTECTED_GCS_KEYS,
    preserve_lines: bool = False,
) -> dict[str, str]:
    """Read-merge-write ``path`` with protected-key blanking refused."""
    existing = parse_env_file(path) if path.is_file() else {}
    merged = merge_env_updates(existing, updates, protected=protected)
    if not preserve_lines or not path.is_file():
        write_env_file(path, merged, header=header)
        return merged
    # A targeted identity/token edit must not normalize unrelated shell
    # expressions into quoted literals or discard the operator's comments.
    replacements = {key: merged[key] for key in updates if merged.get(key) == updates[key]}
    remaining = dict(replacements)
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        raw = line.lstrip()
        exported = raw.startswith("export ")
        if exported:
            raw = raw[7:].lstrip()
        key, separator, _ = raw.partition("=")
        key = key.strip()
        if separator and key in replacements:
            prefix = "export " if exported else ""
            lines.append(prefix + render_env_file({key: replacements[key]}))
            remaining.pop(key, None)
        else:
            lines.append(line)
    body = "".join(lines)
    if remaining:
        if body and not body.endswith("\n"):
            body += "\n"
        body += render_env_file(remaining)
    _write_env_text(path, body)
    return merged
