"""Project stream -> source-repository binding (task #1425, Parity 20).

``webui/project_registry.py`` resolves a project stream (``unified-agent``,
other configured ids) to a DEVICE-LOCAL ``~/dev`` checkout path,
existence-checked only, and deliberately never lists directory contents
(task #356). It has no notion of which git repository that project *is*.
This module adds that: a durable, DB-backed binding from a project stream id
to a normalized GitHub ``owner/name`` repository, consumed for display (the
Control Center project view, ``webui/project_registry.py``) and to warn — or,
opt-in, refuse — task-driven worktree creation (``tools/tasks.py``) when a
project has no bound repo or its checkout's origin doesn't match one.

Binding shape: ``project -> {repo, default_branch, bound_by, bound_at}``.
A DB row always wins over ``config.PROJECT_REPO_SEED``'s config-seeded
default (see ``effective_binding``); the seed exists so a freshly migrated
hub is not silently unbound for the one project whose repo is already known.

Read-only in reach: ``resolve()`` and ``check_worktree_binding()`` never
touch the filesystem beyond ``Path.is_dir()`` (an existence check) and a
direct, non-following read of that one repository's own ``.git/config``
file (see ``_read_git_origin``) — it opens no other file and lists no
directory. Binding a repo here grants no new filesystem reach; it only lets
already-existing, existence-checked resolution report an honest
match/mismatch instead of staying silent.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import urllib.parse
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, storage, table_schemas
from .tools.tasks import PROJECTS

logger = logging.getLogger(__name__)

# GitHub only, for now (task #1425 scope). Nothing in config currently names
# an alternate allowed host, so this is the one allow-listed set; a future
# project on a different forge should add a config-level override here
# rather than loosening this default.
ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})

_SCP_STYLE_RE = re.compile(r"^git@(?P<host>[^:/]+):(?P<path>.+)$")
_OWNER_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables (the DDL lives in ``table_schemas``)."""
    table_schemas.project_bindings(conn)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def normalize_repo(raw: str, *, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> str:
    """Normalize a repo reference to ``owner/name`` on an allowed host.

    Accepts ``owner/name``, ``github.com/owner/name``,
    ``https://github.com/owner/name(.git)?``, ``ssh://git@github.com/owner/name(.git)?``,
    and ``git@github.com:owner/name.git``. Rejects: empty/whitespace input, any
    URL carrying credentials or userinfo (``user:pass@`` or a non-``git``
    username before the host), any host other than ``allowed_hosts``, and
    anything that doesn't resolve to exactly two non-empty path segments
    (``owner//name``, ``owner/``, and ``/name`` are all rejected, not
    silently collapsed).

    GitHub owner/repo names are case-insensitive, so the result is always
    lowercased (``Owner/Name`` and ``owner/name`` normalize identically) --
    this is the one representation stored and compared against everywhere in
    this module; nothing here preserves original display casing.
    """
    text = (raw or "").strip()
    if not text:
        raise ToolError("repo must not be empty")
    if any(ch.isspace() for ch in text):
        raise ToolError(f"invalid repo {raw!r}: must not contain whitespace")

    host: str | None
    path: str
    if text.startswith("git@") or text.startswith("ssh://"):
        m = _SCP_STYLE_RE.match(text)
        if text.startswith("ssh://"):
            parsed = urllib.parse.urlsplit(text)
            # ``git@`` is the conventional SSH user, not a credential.
            if parsed.password or (parsed.username and parsed.username != "git"):
                raise ToolError(f"repo URL must not carry credentials: {raw!r}")
            host = parsed.hostname
            path = parsed.path.lstrip("/")
        elif m:
            host, path = m.group("host"), m.group("path")
        else:
            raise ToolError(f"invalid scp-style repo url: {raw!r}")
    elif "://" in text:
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme not in ("https", "http"):
            raise ToolError(f"unsupported repo URL scheme: {parsed.scheme!r}")
        if parsed.username or parsed.password:
            raise ToolError(f"repo URL must not carry credentials or userinfo: {raw!r}")
        host = parsed.hostname
        path = parsed.path.lstrip("/")
    elif "/" in text:
        # "owner/name" or "host/owner/name"
        first, rest = text.split("/", 1)
        if "@" in first:
            raise ToolError(f"repo must not carry credentials or userinfo: {raw!r}")
        if first.lower().rstrip(".") in allowed_hosts:
            host = first
            path = rest
        else:
            host = "github.com"
            path = text
    else:
        raise ToolError(f"unrecognized repo format: {raw!r}")

    host = (host or "").lower().rstrip(".")
    if host not in allowed_hosts:
        raise ToolError(f"repo host {host!r} is not allowed (allowed: {sorted(allowed_hosts)})")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    # No filtering of empty segments here: "owner//name" must be rejected as
    # malformed, not silently collapsed into "owner/name".
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        raise ToolError(f"repo must resolve to exactly owner/name, got {path!r} from {raw!r}")
    owner, name = segments
    if not _OWNER_NAME_SEGMENT_RE.match(owner) or not _OWNER_NAME_SEGMENT_RE.match(name):
        raise ToolError(f"invalid owner/name in repo {raw!r}")
    return f"{owner.lower()}/{name.lower()}"


def _require_known_project(project: str) -> None:
    if project not in PROJECTS:
        raise ToolError(f"unknown project stream: {project!r} (known: {sorted(PROJECTS)})")


def bind(
    project: str,
    repo: str,
    default_branch: str | None = None,
    bound_by: str | None = None,
) -> dict[str, Any]:
    """Bind `project` to `repo` (upsert). Persisted binding wins over any
    config-seeded default from now on."""
    _require_known_project(project)
    normalized = normalize_repo(repo)
    # A verified identity always wins over a caller-claimed ``bound_by``.
    who = (config.verified_agent_identity() or bound_by or "").strip()
    if not who:
        raise ToolError("bound_by is required when no verified identity is present")
    branch = (default_branch or "").strip() or None
    now = storage.now()
    conn = storage.connect()
    try:
        # Task #1425 review: the write and its audit event must land or fail
        # together -- record_event_once_on_connection appends the event on
        # THIS connection's open transaction (see config_revisions.py's
        # `_record`/`_append_live`, the strongest existing precedent for a
        # domain write with a durable audit trail) instead of the row being
        # committed first and the event appended afterward on a separate
        # connection, which could report a raised error while the binding
        # change had already landed unaudited.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO project_repo_bindings (project, repo, default_branch, bound_by, bound_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
            "repo=excluded.repo, default_branch=excluded.default_branch, "
            "bound_by=excluded.bound_by, bound_at=excluded.bound_at",
            (project, normalized, branch, who, now),
        )
        row = conn.execute(
            "SELECT * FROM project_repo_bindings WHERE project=?", (project,)
        ).fetchone()
        events.record_event_once_on_connection(
            conn,
            "project.repo.bound",
            payload={"project": project, "repo": normalized, "bound_by": who},
            originator=who,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    assert row is not None
    binding = _row_dict(row)
    binding["source"] = "db"
    return {"binding": binding}


def unbind(project: str, unbound_by: str | None = None) -> dict[str, Any]:
    """Remove `project`'s DB binding, if any. Does not touch a config seed --
    resolution simply falls back to it (or to no binding) afterward."""
    _require_known_project(project)
    who = (config.verified_agent_identity() or unbound_by or "").strip()
    if not who:
        raise ToolError("unbound_by is required when no verified identity is present")
    conn = storage.connect()
    try:
        # Same atomic write+event pattern as `bind` -- see the comment there.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM project_repo_bindings WHERE project=?", (project,)
        ).fetchone()
        conn.execute("DELETE FROM project_repo_bindings WHERE project=?", (project,))
        previous = _row_dict(row) if row is not None else None
        events.record_event_once_on_connection(
            conn,
            "project.repo.unbound",
            payload={"project": project, "previous": previous},
            originator=who,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"unbound": previous is not None, "previous": previous}


def effective_binding(project: str) -> dict[str, Any] | None:
    """The binding that governs `project`: its DB row, else its config seed,
    else None. DB always wins over the seed."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM project_repo_bindings WHERE project=?", (project,)
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        binding = _row_dict(row)
        binding["source"] = "db"
        return binding
    seed = config.PROJECT_REPO_SEED.get(project)
    if seed:
        return {
            "project": project,
            "repo": seed["repo"],
            "default_branch": seed.get("default_branch"),
            "bound_by": None,
            "bound_at": None,
            "source": "config_default",
        }
    return None


def get(project: str) -> dict[str, Any]:
    _require_known_project(project)
    return {"project": project, "binding": effective_binding(project)}


def list_bindings() -> dict[str, Any]:
    return {"projects": [{"project": p, "binding": effective_binding(p)} for p in PROJECTS]}


_GIT_CONFIG_SECTION_RE = re.compile(r'^\s*\[([^"\]\s]+)(?:\s+"([^"]*)")?\]\s*$')
_GIT_CONFIG_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*=\s*(.*?)\s*$")
_MAX_GIT_CONFIG_BYTES = 1024 * 1024


def _parse_git_config_origin_url(text: str) -> str | None:
    """Minimal, non-recursive read of an origin URL from raw
    git-config text.

    Deliberately does NOT implement `include.path`/`includeIf` (so a
    malicious checkout's own config can never pull in another file) or
    `url.<base>.insteadOf` rewriting (so a rewrite rule elsewhere in the same
    file can never change what this reports) -- those directives, if
    present, are just inert key/value pairs to this parser. See
    `_read_git_origin` and docs/project-repositories.md.
    """
    current_section: str | None = None
    current_subsection: str | None = None
    origin_urls: list[str] = []
    for raw_line in text.splitlines():
        # Git config comments start with '#' or ';' outside a quoted value;
        # this narrow reader only ever looks at `[remote "origin"]`/`url =`
        # lines, neither of which legitimately contains one, so a simple
        # strip is enough for this internal use.
        line = raw_line.split("#", 1)[0].split(";", 1)[0]
        section_match = _GIT_CONFIG_SECTION_RE.match(line)
        if section_match:
            current_section = section_match.group(1).strip().lower()
            current_subsection = section_match.group(2)
            continue
        if not (
            (current_section == "remote" and current_subsection == "origin")
            or (current_section == "remote.origin" and current_subsection is None)
        ):
            continue
        kv_match = _GIT_CONFIG_KEY_VALUE_RE.match(line)
        if not kv_match:
            continue
        key, value = kv_match.group(1).lower(), kv_match.group(2).strip()
        if key != "url":
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        origin_urls.append(value)
    # Repeated identical values are harmless; conflicting values are
    # ambiguous because Git could use another URL than the one validated.
    unique_origin_urls = set(origin_urls)
    if len(unique_origin_urls) == 1 and origin_urls[0]:
        return next(iter(unique_origin_urls))
    return None


def _read_git_origin(path: Path) -> str | None:
    """Read a checkout's `origin` remote URL directly from its own
    `<path>/.git/config` file -- never by invoking `git remote get-url
    origin`, which applies `url.<x>.insteadOf` rewrites and follows
    `include.path`/`includeIf` directives from that (possibly untrusted)
    checkout's own config, and reads the full merged config stack (global,
    system, worktree-specific) rather than exactly one file.

    `<path>/.git` must be a real repository directory. A worktree or
    submodule's `.git` is instead a *file* containing a `gitdir: <path>`
    pointer to another location; this deliberately does not follow it --
    an untrusted checkout could point that anywhere on disk -- and returns
    None instead, same as "no readable origin".

    The only filesystem reach this module has beyond `is_dir()`: opens
    exactly this one file without following symlinks, requires a regular file,
    and reads at most `_MAX_GIT_CONFIG_BYTES`, with no directive-following of
    any kind. See docs/project-repositories.md.
    """
    # Open each component relative to its already-open parent and refuse
    # symlinks. This avoids both following attacker-controlled .git/config
    # links and check-then-open races from Path.is_dir/is_file/read_text.
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    nofollow = os.O_NOFOLLOW
    directory_fd = file_fd = None
    try:
        directory_fd = os.open(
            path / ".git", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        )
        file_fd = os.open(
            "config", os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_GIT_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_GIT_CONFIG_BYTES:
            return None
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    return _parse_git_config_origin_url(text)


def resolve(project: str, dev_root: str | Path | None = None) -> dict[str, Any]:
    """Resolve `project`'s bound repo alongside its device-local checkout.

    Returns ``{project, repo, default_branch, repo_source, local_path,
    local_state, remote_matches}``. ``local_state`` is one of
    ``"checked_out"``, ``"not_checked_out_here"``, or ``"unknown"`` (an
    unrecognized project stream). ``remote_matches`` is ``None`` when it
    cannot be determined (not checked out here, no bound repo, or no
    readable ``origin`` remote), else a bool.
    """
    binding = effective_binding(project) if project in PROJECTS else None
    repo = binding["repo"] if binding else None
    default_branch = binding.get("default_branch") if binding else None
    repo_source = binding["source"] if binding else None

    if project not in PROJECTS:
        return {
            "project": project,
            "repo": repo,
            "default_branch": default_branch,
            "repo_source": repo_source,
            "local_path": None,
            "local_state": "unknown",
            "remote_matches": None,
        }

    root = Path(dev_root) if dev_root is not None else config.dev_root()
    dirname = config.PROJECT_REPO_DIRNAMES.get(project, project)
    path = root / dirname
    checked_out = (
        not path.is_symlink() and path.is_dir() and path.resolve().is_relative_to(root.resolve())
    )
    local_state = "checked_out" if checked_out else "not_checked_out_here"

    remote_matches: bool | None = None
    if checked_out and repo:
        origin = _read_git_origin(path)
        if origin is not None:
            try:
                remote_matches = normalize_repo(origin) == repo
            except ToolError:
                remote_matches = False

    return {
        "project": project,
        "repo": repo,
        "default_branch": default_branch,
        "repo_source": repo_source,
        "local_path": str(path) if checked_out else None,
        "local_state": local_state,
        "remote_matches": remote_matches,
    }


def check_worktree_binding(project: str, project_repo_path: Path) -> str | None:
    """Called right before a task-driven worktree is cut from a real,
    known-checked-out ``~/dev`` project repo (``tools.tasks._ensure_task_worktree``).

    Returns a warning message when the project has no bound repo, or its
    checkout's ``origin`` doesn't match the bound repo (or can't be read).
    Returns ``None`` when everything checks out.

    Non-strict by default (``config.project_repo_strict_worktree_check()``
    false): a hard refusal here would break existing worktree creation for
    every project that isn't yet bound, which as of task #1425 is every
    project except the config-seeded ``unified-agent`` default. Under the
    opt-in strict flag this raises ``ToolError`` instead of returning a
    message. The caller lets it propagate (#912): a binding refusal is an
    error the claimant must see, not a silent "no worktree created".
    """
    binding = effective_binding(project)
    if binding is None:
        message = f"project {project!r} has no bound source repository"
    else:
        origin = _read_git_origin(project_repo_path)
        if origin is None:
            message = (
                f"could not read the origin remote at {project_repo_path} to verify "
                f"project {project!r}'s bound repo {binding['repo']!r}"
            )
        else:
            try:
                normalized_origin = normalize_repo(origin)
            except ToolError:
                normalized_origin = None
            if normalized_origin != binding["repo"]:
                observed = (
                    f"normalized origin {normalized_origin!r}"
                    if normalized_origin is not None
                    else "an invalid origin URL"
                )
                message = (
                    f"checkout at {project_repo_path} ({observed}) does not match "
                    f"project {project!r}'s bound repo {binding['repo']!r}"
                )
            else:
                message = None

    if message is None:
        return None
    if config.project_repo_strict_worktree_check():
        raise ToolError(f"refusing worktree creation: {message}")
    logger.warning("project repo binding warning for %r: %s", project, message)
    try:
        events.record_event(
            "task.worktree.repo_binding_warning",
            payload={"project": project, "message": message},
            originator=config.verified_agent_identity() or "system",
            notify=False,
        )
    except Exception:
        logger.debug("failed to record repo_binding_warning event", exc_info=True)
    return message
