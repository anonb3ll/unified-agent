"""Safety chokepoints: workspace path sandboxing and shell command vetting."""

import codecs
import ipaddress
import re
import shlex
import socket
import subprocess
from pathlib import Path

import yaml
from httpx import HTTPTransport
from mcp.server.mcpserver.exceptions import ToolError

from . import config, profiles, trust_presets

PROD_ACTIONS = {
    "git_merge_task": "merge",
    "task_delete": "delete",
    # Reserved for the deploy tool; keeping the action vocabulary centralized
    # prevents a future deploy entrypoint from bypassing the same gate.
    "deploy": "deploy",
    # Fleet control (task #799): restart_service on a registered local host.
    "fleet_host_action": "restart",
    # Device sign-in revoke (task #1321, J2): drops a device's OAuth sessions.
    "oauth_device_manage": "revoke",
    # ua-node work (task #1327, I1): queuing an item runs an agent CLI on a device.
    "node_dispatch": "dispatch",
    # ACP adapter (task #1328, I2): a harness's mid-turn permission prompt is
    # the same class of action — a running agent asking to do more on a device.
    "acp_permission": "dispatch",
}


def check_prod_action_authority(tool: str) -> tuple[str, str] | None:
    """The identity half of the prod-action gate: anonymity, revocation, scope.

    Returns ``(action, identity)`` when the caller may run ``tool`` subject to
    approval, or None when ``tool`` is not a prod action (or an anonymous call
    is let through by the open posture). Raises ``ToolError`` otherwise. Split
    out so a path that needs no second approval (an operator-authenticated
    node follow-up, task #1872) runs exactly the same identity checks.
    """
    action = PROD_ACTIONS.get(tool)
    identity = config.verified_agent_identity()
    if action is None:
        return None
    if identity is None:
        # HTTP denies anonymous prod actions. Stdio still skips unless
        # UA_REQUIRE_CALLER_IDENTITY=1. UA_LEGACY_OPEN_POSTURE=1 skips on
        # every transport. The skip path emits the migration warning.
        if config.anonymous_prod_action_open():
            return None
        raise ToolError(
            f"anonymous callers cannot run prod action {action!r}. "
            "Authenticate, or set UA_LEGACY_OPEN_POSTURE=1 to restore the "
            "open posture."
        )

    if identity in config.revoked_agents():
        raise ToolError(f"agent {identity!r} is revoked from prod action {action!r}")

    scopes = config.agent_scopes()
    if scopes and action not in scopes.get(identity, frozenset()):
        raise ToolError(f"agent {identity!r} is not scoped for prod action {action!r}")
    return action, identity


def check_prod_action_gate(tool: str, payload: dict | None = None) -> None:
    """Enforce identity scope, revocation, and human approval for prod actions."""
    authority = check_prod_action_authority(tool)
    if authority is None:
        return
    action, identity = authority

    # A dry run validates without mutating history; it must remain usable for review.
    if tool == "git_merge_task" and (payload or {}).get("dry_run"):
        return

    from .tools.approvals import check_approval_gate

    approval_payload = dict(payload or {})
    approval_payload.update({"action": action, "agent": identity})
    check_approval_gate(tool, approval_payload, requested_by=identity, force=True)


# Directories that hold hook scripts executing unattended on the next
# commit/checkout/etc: the three VCS metadata dirs, plus .husky (the common
# convention for git's configurable core.hooksPath, e.g. in JS repos) --
# core.hooksPath can redirect hooks to an arbitrary directory name, which
# this denylist cannot fully close, but .husky covers the common case.
# Compared case-insensitively so a case-preserving-but-insensitive filesystem
# (macOS APFS, Windows NTFS) can't evade the check by case alone.
_HOOK_DIRS = frozenset({".git", ".hg", ".svn", ".husky"})

# --- Widened READ reach across project checkouts (task #497, option C of #495) ---
#
# The cockpit workspace holds no product code, so `fs_read`/`fs_search` could not
# open the repos agents are asked to reason about; they shelled out or worked
# blind. The directory denylist applies ONLY to the widened project-repo roots.
# The secret-name regex applies there AND to workspace reads
# (`fs_read` / `fs_search` / `resolve_for_project_read`). Workspace writes still
# go through `resolve_in_workspace` with no secret-name check, so ordinary
# source files can be written.
#
# Runtime and dependency trees. Reading them is never the point of a code
# question, they dwarf real source in any search, and `.venv` in particular is
# the live-serving runtime CLAUDE.md forbids touching.
_PROJECT_READ_DENIED_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
    }
)

# Secret-bearing filenames. Workspace reads and project-repo reads both refuse
# these. The cockpit holds live credentials (`projects/imessage-bridge/.env`,
# `service.env`) and the widened project roots can too; neither is a place an
# agent should `fs_read`. Writes are not checked here. Matched against the
# file name only, case-insensitively. `token` stays anchored to name-component
# boundaries so `tokenizer.py` still reads.
_PROJECT_READ_SECRET_RX = re.compile(
    r"""(
          \.env(\.|$)            # .env, .env.local, service.env, sleep-pass.env
        | ^\.netrc$
        | ^\.npmrc$
        | ^\.pypirc$
        | ^\.htpasswd$
        | ^id_(rsa|dsa|ecdsa|ed25519)(\.|$)
        # Anchored to name-component boundaries, not bare substrings: a bare
        # `token` match rejects `tokenizer.py` and `tokenization_utils.py`,
        # which is both wrong and baffling to hit in an ML repo.
        | (^|[._-])credentials?([._-]|$)
        | (^|[._-])secrets?([._-]|$)
        | (^|[._-])tokens?([._-]|$)
        | \.pem$
        | \.key$
        | \.p12$
        | \.pfx$
        | \.keystore$
        | service[-_]account(?![A-Za-z0-9])  # service_account.json, not service_accounting.py
        | ^auth\.json$
        | (^|[._-])pgpass$       # .pgpass
        | (^|[._-])kubeconfig([._-]|$)
        | \.(db|sqlite|sqlite3)$
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _reject_managed_secret_root(resolved: Path, path: str) -> None:
    """Keep all file tools away from the provider-only secret store."""
    from . import agent_access

    if not agent_access.managed_secret_mode():
        return
    from .managed_secrets import _root

    secret_root = _root()
    if resolved == secret_root or resolved.is_relative_to(secret_root):
        raise ToolError(f"path {path!r} reaches managed secret storage; reads are write-only")


def project_read_roots() -> list[Path]:
    """Every root a READ-only tool may resolve into: workspace + project checkouts.

    Deliberately not used by any mutating tool. `config.project_repo_roots()`
    already knew where the product repos were (it routes worktree creation);
    this is the first time path resolution is told the same thing.
    """
    roots = [config.workspace_root().resolve()]
    for repo in config.project_repo_roots():
        resolved = repo.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def guard_hook_dir(resolved: Path, root: Path, path: str) -> None:
    """Refuse a path that reaches into a hook-execution directory under root.

    Unlike guard_project_read's other rules, this one isn't widened-root-only:
    resolve_in_workspace already enforces it for the workspace itself. Exposed
    separately so `fs_search` (tools/files.py) can apply it to every candidate,
    workspace-rooted or not, without paying for a full guard_project_read call
    (deps/secrets checks) that only makes sense for a project checkout.
    """
    rel = resolved.relative_to(root) if resolved != root else Path()
    if any(part.lower() in _HOOK_DIRS for part in rel.parts):
        raise ToolError(
            f"path {path!r} targets a hook-execution directory; "
            "workspace tools do not touch .git/.hg/.svn/.husky internals"
        )


def _secret_name_parts(path: Path) -> list[str]:
    """Every name component, skipping roots and traversal markers."""
    return [part for part in path.parts if part not in {"", ".", "..", "/", "\\"}]


def _secret_path_inside_readable_root(resolved: Path) -> Path:
    """Path components inside a readable root, not the absolute prefix.

    A parent such as a pytest temp directory can contain ``secret`` without
    the file itself being secret-bearing. The check is the path inside the
    workspace or project checkout.
    """
    try:
        roots = project_read_roots()
    except Exception:
        return Path(resolved.name)
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return Path() if resolved == root else resolved.relative_to(root)
    return Path(resolved.name)


def guard_secret_filename(resolved: Path, path: str) -> None:
    """Refuse credential, key, and database filenames on a read.

    Checks every component of a relative requested path and every component of
    the resolved path inside its readable root, so ``service.env/contents.txt``
    is refused and a symlink cannot hide ``service.env`` behind ``notes.txt``.
    Not used by ``resolve_in_workspace``: ``fs_write`` and other mutating tools
    keep writing ordinary source files. ``tokenizer.py`` stays readable:
    ``token`` is anchored at name-component boundaries.
    """
    names: list[str] = []
    requested = Path(path)
    sources = [_secret_path_inside_readable_root(resolved)]
    if not requested.is_absolute():
        sources.append(requested)
    for candidate in sources:
        for part in _secret_name_parts(candidate):
            if part not in names:
                names.append(part)
    for name in names:
        if _PROJECT_READ_SECRET_RX.search(name):
            raise ToolError(
                f"path {path!r} looks secret-bearing ({name!r}); "
                "reads exclude credential, key, and database files"
            )

    # Recursive fs_search uses this guard per real candidate, including
    # workspace files, so a symlink cannot bypass the selected preset root.
    trust_presets.check_path(resolved)


def guard_project_read(resolved: Path, project_root: Path, path: str) -> None:
    """Apply the widened-root read rules. Raises ToolError, or returns None.

    Not private: `fs_search` (tools/files.py) also calls this per matched
    file, so recursing into a project checkout can't bypass the same rules
    a direct `fs_read` on that file would apply.
    """
    guard_hook_dir(resolved, project_root, path)
    rel = resolved.relative_to(project_root) if resolved != project_root else Path()
    parts_lower = [part.lower() for part in rel.parts]

    for part in parts_lower:
        if part in _PROJECT_READ_DENIED_DIRS:
            raise ToolError(
                f"path {path!r} reaches into {part!r} inside {project_root}; "
                "project reads cover source, not runtime or dependency trees"
            )
    guard_secret_filename(resolved, path)


def resolve_for_project_read(path: str) -> Path:
    """Resolve a path for a READ-ONLY tool: workspace, or any project checkout.

    Read reach and write reach are deliberately asymmetric (task #495 option C).
    An agent can *see* every ~/dev project checkout and can still only *write*
    inside its workspace -- `resolve_in_workspace` is untouched and remains the
    boundary for `fs_write`, `run_cmd`, and every other mutating tool.

    A relative path still resolves against the workspace (or the active task
    worktree), exactly as before, so no existing call changes meaning. Reaching
    a project checkout is therefore an explicit act: it takes an absolute path.
    """
    base_root = config.workspace_root().resolve()
    wt_root = get_active_worktree_path()
    root = wt_root if wt_root else base_root

    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    _reject_managed_secret_root(resolved, path)

    if resolved == root or resolved.is_relative_to(base_root):
        # Delegate rather than reimplement the sandbox. Secret filenames are
        # an extra read-only guard: resolve_in_workspace itself stays free of
        # it so fs_write of ordinary source files is unchanged.
        checked = resolve_in_workspace(path)
        guard_secret_filename(checked, path)
        return checked

    # A relative path that escapes the workspace must remain an escape. Without
    # this check, ../<project>/... could resolve into a known checkout and gain
    # the widened read reach even though project access is documented as an
    # explicit absolute-path operation.
    if not candidate.is_absolute():
        raise ToolError(
            f"path {path!r} escapes the workspace root {base_root}. "
            "Use an absolute path to read a known project checkout."
        )

    for project_root in project_read_roots()[1:]:
        if resolved == project_root or resolved.is_relative_to(project_root):
            guard_project_read(resolved, project_root, path)
            return resolved

    readable = ", ".join(str(r) for r in project_read_roots())
    raise ToolError(
        f"path {path!r} is outside every readable root. Readable: {readable}. "
        "Writes remain confined to the workspace root."
    )


def _repository_root(path: Path, search_root: Path) -> Path | None:
    """Return the nearest Git worktree containing *path*, without opening metadata.

    ``search_root`` bounds the upward walk so it never climbs past the caller's
    sandbox boundary. Callers must pass whichever root actually contains
    *path* (``containing_root`` in ``resolve_in_workspace``) rather than
    always ``base_root`` -- a project-sourced task worktree (task #994) can
    live entirely outside ``base_root``, and a walk bounded by ``base_root``
    would never satisfy the loop condition for a path in such a worktree,
    silently returning ``None`` and skipping the hooksPath check below.
    """
    current = path if path.is_dir() else path.parent
    while current == search_root or current.is_relative_to(search_root):
        if (current / ".git").exists():
            return current
        if current == search_root:
            break
        current = current.parent
    return None


def _configured_hooks_path(repo_root: Path) -> Path | None:
    """Resolve Git's effective ``core.hooksPath`` for a worktree.

    ``git config`` is used rather than parsing ``.git/config`` so worktree config,
    includes, and global configuration have the same semantics Git itself applies.
    Exit status 1 means the key is unset; other failures are closed rather than
    silently reopening a code-execution path.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--path", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ToolError(f"could not verify Git's configured hooks path in {repo_root}: {e}") from e
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git exited with status {result.returncode}"
        raise ToolError(f"could not verify Git's configured hooks path in {repo_root}: {detail}")
    configured = result.stdout.strip()
    if not configured:
        return None
    hooks = Path(configured)
    return (hooks if hooks.is_absolute() else repo_root / hooks).resolve()


def get_active_worktree_path(agent_identity: str | None = None) -> Path | None:
    """Return the active worktree Path for the given (or verified current) agent identity

    if it holds a task in 'doing' status with an assigned worktree.
    """
    if agent_identity is None:
        agent_identity = config.verified_agent_identity()
    if not agent_identity:
        return None
    try:
        from .storage import connect

        conn = connect()
        try:
            row = conn.execute(
                "SELECT worktree_path FROM tasks WHERE status='doing' AND claimed_by=? AND worktree_path IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (agent_identity,),
            ).fetchone()
            if row and row["worktree_path"]:
                wt = (config.workspace_root() / row["worktree_path"]).resolve()
                if wt.is_dir():
                    return wt
        finally:
            conn.close()
    except Exception:
        pass
    return None


def resolve_in_workspace(
    path: str,
    use_worktree: bool = True,
    protect_configured_hooks: bool = False,
    *,
    for_write: bool = False,
) -> Path:
    """Resolve a workspace-relative (or absolute) path, refusing anything outside the root.

    When `use_worktree` is True (default), resolves against the calling agent's
    active task worktree directory if one exists.

    ``for_write`` applies the selected trust preset's write restrictions.
    Existing mutating callers using ``protect_configured_hooks`` also imply
    write intent for the preset, preserving their hooks protection.

    When ``protect_configured_hooks`` is true, also consults Git configuration
    to protect a custom ``core.hooksPath``. Mutating file tools enable this;
    read-only paths avoid paying for a Git subprocess.

    ``.resolve()`` collapses ``..`` and follows symlinks, so links pointing outside
    the workspace are also rejected.

    Also refuses any path that reaches into a hook-execution directory (``.git``,
    ``.hg``, ``.svn``, ``.husky``) — those hold hook scripts that execute
    unattended on the next commit/checkout, which would let an ungated write
    turn into code execution that bypasses the approval gate entirely.
    Ordinary dotfiles (``.gitignore``) are unaffected; only these specific
    directory names are blocked. Secret filenames (``.env``, ``service.env``,
    database files) are refused on read by ``guard_secret_filename``, not here,
    so a write of an ordinary source file is unchanged. For paths inside a Git worktree, the
    effective ``core.hooksPath`` is also read through Git (including global,
    include, and worktree configuration) and denied when it targets an arbitrary
    directory name. The repository-root search for this check is bounded by
    whichever of ``base_root``/``wt_root`` actually contains the resolved path
    (``containing_root``), not ``base_root`` unconditionally -- otherwise a
    project-sourced task worktree living outside ``base_root`` (task #994)
    would never find its own repository root, and the hooksPath check would
    silently no-op for every write inside it (task #996).

    The sandbox is ``base_root`` (the cockpit) union the active task worktree,
    not ``base_root`` alone. A ``[code-task]`` (or other project-sourced) task
    worktree lives under its own product repo, outside ``base_root`` entirely
    (task #994) — so containment has to be checked against whichever of the
    two roots actually contains the resolved path, not against ``base_root``
    unconditionally (task #995). This never widens reach beyond that union:
    a path outside both roots is still refused.
    """
    base_root = config.workspace_root().resolve()
    wt_root = get_active_worktree_path() if use_worktree else None
    root = wt_root if wt_root else base_root

    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    _reject_managed_secret_root(resolved, path)

    containing_root = None
    for candidate_root in (base_root, wt_root):
        if candidate_root is not None and (
            resolved == candidate_root or resolved.is_relative_to(candidate_root)
        ):
            containing_root = candidate_root
            break
    if containing_root is None:
        raise ToolError(
            f"path {path!r} escapes the workspace root {base_root}. "
            "Use a path relative to the workspace."
        )
    guard_hook_dir(resolved, containing_root, path)
    repo_root = _repository_root(resolved, containing_root) if protect_configured_hooks else None
    if repo_root is not None:
        hooks_root = _configured_hooks_path(repo_root)
        if hooks_root is not None and (
            resolved == hooks_root or resolved.is_relative_to(hooks_root)
        ):
            raise ToolError(
                f"path {path!r} targets the configured Git hooks directory {hooks_root}; "
                "workspace tools do not write hook programs"
            )
    trust_presets.check_path(resolved, for_write=for_write or protect_configured_hooks)
    return resolved


def resolve_task_worktree(path: str) -> Path:
    """Resolve a task worktree for the review/merge GATES. Not agent-facing.

    Task #496. Since #371 a ``[code-task]`` worktree is deliberately sourced
    from -- and registered against -- its project's own ~/dev checkout, so it
    can live under ``<project>/.worktrees/``. The gates (clean-tree check,
    security audit, pytest) have to run *inside* that directory, but every gate
    runner resolved its cwd through ``resolve_in_workspace``, which refuses any
    path outside the cockpit by construction. The result was worse than a wrong
    label: a complete, green, pushed task got a false ``[review: rejected]``,
    which claims the gates found something wrong rather than that they never
    ran.

    This is deliberately NOT a relaxation of ``resolve_in_workspace``. That
    function remains the sandbox boundary for every agent-facing tool and every
    untrusted delegation, and is unchanged. This one is reachable only from the
    pipeline's own gate runners.

    Accepts exactly two shapes:
      * anything inside the workspace root (delegated, so behaviour is identical)
      * anything strictly under a project checkout's ``.worktrees/`` directory

    The second is narrow on purpose. A project checkout's own root is refused:
    that is the live-serving tree the hub runs from, and CLAUDE.md forbids the
    pipeline operating there. Only the disposable per-task worktrees qualify.
    """
    base_root = config.workspace_root().resolve()
    candidate = Path(path).expanduser()
    resolved = (candidate if candidate.is_absolute() else base_root / candidate).resolve()

    if resolved == base_root or resolved.is_relative_to(base_root):
        return resolve_in_workspace(path, use_worktree=False)

    for project_root in config.project_repo_roots():
        worktrees_home = (project_root.resolve() / ".worktrees").resolve()
        if resolved != worktrees_home and resolved.is_relative_to(worktrees_home):
            return resolved

    raise ToolError(
        f"path {path!r} is not a task worktree: it is neither inside the workspace "
        f"root {base_root} nor under a project checkout's .worktrees/ directory."
    )


# Legacy line-based lock check: a leading ``---`` block scanned for ``locked: true``.
# It is one input to :func:`vault_lock_state`, never the whole decision.
VAULT_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
VAULT_LOCKED_TRUE_RE = re.compile(r"^locked:\s*['\"]?true['\"]?\s*$", re.IGNORECASE | re.MULTILINE)
# A line that names the `locked` key at all, however it is quoted or valued.
_LOCKED_KEY_LINE_RE = re.compile(r"^[ \t]*['\"]?locked['\"]?[ \t]*:", re.IGNORECASE | re.MULTILINE)
# How much of a note's head the lock check reads (frontmatter must fit in it).
VAULT_LOCK_HEAD_BYTES = 65536
# YAML 1.1 falsy scalars. Any other value of a `locked` key (true/yes/on in any
# case, empty, null, a list...) counts as locked: the guard fails closed.
_LOCK_FALSE_VALUES = frozenset({"false", "no", "off", "0"})

# Cheap guards so hostile frontmatter cannot make yaml.compose recurse or crawl.
_YAML_MAX_NESTING = 64
_YAML_MAX_INDENT = 128
# Column-0 `locked:` line with its value, for scanning past a `...` end marker.
_LOCKED_VALUE_LINE_RE = re.compile(
    r"^['\"]?locked['\"]?[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE
)


class VaultLockedError(ToolError):
    """A vault write was refused because the note is locked (or its lock is unverifiable)."""


def _yaml_too_deep(text: str) -> bool:
    depth = deepest = 0
    for ch in text:
        if ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth = max(0, depth - 1)
    if deepest > _YAML_MAX_NESTING:
        return True
    return any(len(ln) - len(ln.lstrip(" ")) > _YAML_MAX_INDENT for ln in text.splitlines())


def _line_scan_locked(text: str) -> bool:
    """True if a column-0 `locked:` line has any value other than false/no/off/0."""
    for m in _LOCKED_VALUE_LINE_RE.finditer(text):
        value = re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\"").strip().lower()
        if value not in _LOCK_FALSE_VALUES:
            return True
    return False


LOCK_LOCKED = "locked"
LOCK_UNLOCKED = "unlocked"
LOCK_UNVERIFIABLE = "unverifiable"
# Reason for an unverifiable verdict that a larger read of the head could resolve.
LOCK_REASON_BOUNDED = "cloud vault access requires complete, bounded frontmatter"


def vault_lock_state(head: bytes, limit: int = VAULT_LOCK_HEAD_BYTES) -> tuple[str, str]:
    """Classify a note from the first ``limit`` bytes of its file.

    The single lock-detection routine for every vault path, local and cloud.
    Returns ``(state, reason)``: ``locked``, ``unlocked``, or ``unverifiable``
    (frontmatter that cannot be parsed or bounded; ``reason`` says why).

    A note is locked if EITHER the legacy line check or a YAML parse says so:
    the regex alone misses comments, quoted keys, CRLF and YAML boolean aliases,
    while YAML alone can read a ``locked: true`` line as part of a multi-line
    scalar or stop at a ``...`` end marker the line check reads past. A UTF-8
    BOM and CRLF line endings are tolerated.
    """
    body = head.removeprefix(codecs.BOM_UTF8)
    lines = body.splitlines()
    if not lines or lines[0].rstrip() != b"---":
        return LOCK_UNLOCKED, ""
    truncated = len(head) >= limit
    end = next(
        (
            i
            for i in range(1, len(lines))
            if lines[i].rstrip() in {b"---", b"..."}
            and (i < len(lines) - 1 or not truncated or head.endswith((b"\n", b"\r")))
        ),
        None,
    )
    normalized = b"\n".join(lines).decode("utf-8", errors="replace")
    hub_block = VAULT_FRONTMATTER_RE.match(normalized)
    if end is None or (hub_block is None and truncated):
        # No closing delimiter inside the head, or only a `...` one while the
        # legacy `---` block runs past the bound: the real block may end beyond
        # what was read, so do not report a definite verdict.
        return LOCK_UNVERIFIABLE, LOCK_REASON_BOUNDED
    if hub_block is not None:
        if VAULT_LOCKED_TRUE_RE.search(hub_block.group(1)):
            return LOCK_LOCKED, ""
        if lines[end].rstrip() == b"..." and _line_scan_locked(hub_block.group(1)):
            # A `...` ends the YAML document early but the legacy block (and
            # Obsidian's `---` delimiter) continues: honour a lock found there.
            return LOCK_LOCKED, ""
    try:
        text = b"\n".join(lines[1:end]).decode("utf-8")
        if VAULT_LOCKED_TRUE_RE.search(text):
            return LOCK_LOCKED, ""
        if _yaml_too_deep(text):
            return LOCK_UNVERIFIABLE, "cloud vault access cannot verify malformed frontmatter"
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        return LOCK_UNVERIFIABLE, "cloud vault access cannot verify malformed frontmatter"
    if node is None:
        return LOCK_UNLOCKED, ""
    if not isinstance(node, yaml.MappingNode):
        return LOCK_UNVERIFIABLE, "cloud vault access requires mapping frontmatter"
    for key, value in node.value:
        # Merge keys could inherit a lock. Refuse instead of guessing, and
        # inspect every duplicate locked key rather than accepting last-wins.
        if not isinstance(key, yaml.ScalarNode) or key.value == "<<":
            return LOCK_UNVERIFIABLE, "cloud vault access cannot verify indirect frontmatter keys"
        if key.value.strip().lower() == "locked" and (
            not isinstance(value, yaml.ScalarNode)
            or value.value.strip().lower() not in _LOCK_FALSE_VALUES
        ):
            return LOCK_LOCKED, ""
    return LOCK_UNLOCKED, ""


def _frontmatter_region(head: bytes, limit: int) -> str:
    """Best-effort frontmatter text of a note whose block could not be parsed.

    Bounded by the closing delimiter when there is one. Without one, the whole
    head if it was truncated (the block may continue), else only up to the first
    blank line, so an ordinary body line is not mistaken for frontmatter.
    """
    body = head.removeprefix(codecs.BOM_UTF8)
    lines = body.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ""
    dash = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    other = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "..."), None)
    stop = dash if dash is not None else other
    if stop is not None:
        return "\n".join(lines[1:stop])
    if len(head) >= limit:
        return "\n".join(lines[1:])
    blank = next((i for i in range(1, len(lines)) if not lines[i].strip()), len(lines))
    return "\n".join(lines[1:blank])


def vault_head_is_locked(head: bytes, limit: int = VAULT_LOCK_HEAD_BYTES) -> bool:
    """Local-caller lock verdict: locked, or unparseable frontmatter naming `locked`.

    Frontmatter that cannot be verified fails closed only when its frontmatter
    region mentions a `locked` key; an unrelated parse problem, or a body line,
    does not block an ordinary write. (If the closing delimiter is missing and
    the head was cut off, the whole head counts as the region: fail-safe.)
    """
    state, _ = vault_lock_state(head, limit)
    if state == LOCK_LOCKED:
        return True
    if state == LOCK_UNVERIFIABLE:
        return bool(_LOCKED_KEY_LINE_RE.search(_frontmatter_region(head, limit)))
    return False


def _check_cloud_vault_lock(path: Path) -> None:
    """Fail closed on locked or unreadable notes, without loading their body.

    The cloud scope cannot override locks and refuses any frontmatter it cannot
    verify (see :func:`vault_lock_state`). No cache can hide an Obsidian lock edit.
    """
    if path.is_dir():
        return
    try:
        with open(path, "rb") as note:
            head = note.read(VAULT_LOCK_HEAD_BYTES)
    except FileNotFoundError:
        return  # A new Wiki note or daily journal can be created.
    except (OSError, UnicodeError) as exc:
        raise ToolError("cloud vault access cannot verify the note's lock") from exc
    state, reason = vault_lock_state(head)
    if state == LOCK_LOCKED:
        raise VaultLockedError("cloud vault access excludes locked notes; force cannot override")
    if state == LOCK_UNVERIFIABLE:
        raise ToolError(reason)


#: Vault directory holding the audit-ledger chain-head witnesses
#: (``audit/reanchor.py``). Reserved for that writer (task #1908).
LEDGER_ANCHOR_DIR = "Logs/audit-ledger-heads"


def _in_ledger_anchor_dir(rel: Path) -> bool:
    """True when ``rel`` is the anchor dir or below it, compared case-folded.

    Case-folded because the hub's APFS volume is case-insensitive by default:
    ``Logs/Audit-Ledger-Heads/x.md`` lands in the same directory there.
    """
    reserved = tuple(part.casefold() for part in Path(LEDGER_ANCHOR_DIR).parts)
    return tuple(part.casefold() for part in rel.parts[: len(reserved)]) == reserved


def resolve_in_vault(
    path: str,
    for_write: bool = False,
    extra_write_suffixes: tuple[str, ...] = (),
    *,
    for_journal: bool = False,
    for_ledger_anchor: bool = False,
) -> Path:
    """Resolve a vault-relative (or absolute) path inside the Obsidian vault.

    Baseline reads may target anything in the vault except dot-directories/files
    (.git, .obsidian, .trash). Writes are additionally confined to the
    UA_VAULT_WRITE_DIRS allowlist and to Markdown/plain-text files, so agents
    can capture and journal but never rewrite curated notes.

    `extra_write_suffixes` opts a caller into additional write extensions on
    top of .md/.txt (e.g. the structured-export tools need .csv/.json/.jsonl).
    The write-dir allowlist is unaffected — this only widens the file-type
    check, never where a write may land.

    Writes under ``Logs/audit-ledger-heads/`` (:data:`LEDGER_ANCHOR_DIR`) are
    refused unless ``for_ledger_anchor`` is set, which only
    ``audit.reanchor.reanchor_chain_head`` passes (task #1908). The notes there
    are the external witness of the event_log chain head; an agent tool,
    including ``vault_write(force=True)`` and ``vault_move``, must not be able
    to forge or rewrite one. Reads are unaffected.
    """
    root = config.vault_root()
    if root is None:
        raise ToolError("vault tools are disabled; set UA_VAULT_PATH to an explicit vault root")
    root = root.resolve()
    if not root.is_dir():
        raise ToolError(
            f"vault root {root} does not exist; create it or point UA_VAULT_PATH "
            "at your Obsidian vault in the MCP server config"
        )
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise ToolError(f"path {path!r} escapes the vault root {root}. Use a vault-relative path.")
    rel = resolved.relative_to(root)
    if any(part.startswith(".") for part in rel.parts):
        raise ToolError(
            f"path {path!r} targets a hidden file or directory; "
            "vault tools do not touch .git/.obsidian internals"
        )
    # Two cloud scopes (task #1799). ``cloud_vault`` is the Wiki-confined
    # cloud-vault-steward profile and governs reads and writes. ``cloud_caller``
    # adds any cloud-hosted identity whatever its profile (cloud_agent13 is
    # chief-of-staff; cloud_agent8 is research; cloud_agent17 is a developer): its
    # writes keep the canonical-journal,
    # fail-closed lock and write-dir guards even though its reads follow the
    # wider profile.
    cloud_vault = profiles.current() == profiles.CLOUD_VAULT_STEWARD
    cloud_caller = profiles.cloud_hosted_caller()
    journal_exception = cloud_vault and for_journal and for_write
    if cloud_caller and for_journal and for_write:
        # Only the native journal calls this internal flag. Canonicalize
        # first: a symlink cannot redirect an append to Wiki or another day.
        if rel.as_posix() != path or not re.fullmatch(r"Logs/\d{4}-\d{2}-\d{2}\.md", path):
            raise ToolError("cloud journal writes require a canonical daily Logs note")
    if cloud_vault:
        if not journal_exception and not rel.is_relative_to("Wiki"):
            raise ToolError("cloud vault access is restricted to unlocked Wiki notes")
        if not resolved.is_dir() and resolved.suffix not in {".md", ".txt"}:
            raise ToolError("cloud vault access is limited to Markdown/plain-text notes")
    if for_write and not for_ledger_anchor and _in_ledger_anchor_dir(rel):
        raise ToolError(
            f"{LEDGER_ANCHOR_DIR}/ is reserved for the audit-ledger re-anchor job; "
            "agent tools cannot write there, force=True included"
        )
    if for_write:
        # Chief-of-staff's PARA scope is available only after authenticated,
        # session-scoped profile activation.  Environment configuration remains
        # the baseline boundary for every other request.
        # The cloud journal exception is still bounded by the installation's
        # write dirs, exactly like every other profile's vault_journal.
        allowed = (
            profiles.within_configured_write_dirs(("Logs",))
            if journal_exception
            else profiles.vault_write_dirs()
        )
        if not any(rel.is_relative_to(d) for d in allowed):
            raise ToolError(
                f"vault writes are restricted to {', '.join(allowed) or 'no directories'}; "
                f"{str(rel)!r} is outside. Extend UA_VAULT_WRITE_DIRS (colon-separated) "
                "to widen it."
            )
        if resolved.suffix not in ((".md", ".txt") + extra_write_suffixes):
            allowed_sfx = ", ".join((".md", ".txt") + extra_write_suffixes)
            raise ToolError(
                f"vault writes here are limited to {allowed_sfx}; got {resolved.suffix!r}"
            )
    trust_presets.check_path(resolved, for_write=for_write)
    if cloud_vault or (cloud_caller and for_write):
        _check_cloud_vault_lock(resolved)
    return resolved


def resolve_for_audit(path: str, *, for_write: bool = False) -> Path:
    """Resolve a path for read-only auditing: workspace root or any UA_AUDIT_ROOTS root.

    Relative paths resolve against the process workspace root (not the
    per-agent ContextVar binding). Explicit ``UA_AGENT_WORKSPACES`` DENY
    identities may still run ``security_audit`` — it is a read-only advisory
    check, same rationale as ``process_workspace_root()`` for hub census
    (steward-inbox / UA #891). When configured, the Obsidian vault root is
    always an auditable root so ``00 - Inbox`` captures are reachable without
    ``vault=True``. Write/shell tools stay confined via ``workspace_root()``.

    A cloud-hosted identity (``config.CLOUD_HOSTED_IDENTITIES``, task #1799) is
    refused outright: the DENY exemption above is for identities on the
    operator's own machines, and every root here is the hub host's disk. This
    covers ``security_audit``, ``detect_dev_servers``, ``skill_review`` and the
    skill registry's publish/drift reads, which all resolve through here. A
    vault-scoped audit (``vault=True``) goes through ``resolve_in_vault``.
    """
    identity = config.verified_agent_identity()
    if config.is_cloud_hosted_identity(identity):
        raise ToolError(
            f"Hub-mediated filesystem access is denied for agent {identity!r} "
            "(cloud-hosted identity); audit and skill inspection read the hub "
            "host's disk. Use vault=True for vault notes, or this identity's own tools."
        )
    ws = config.process_workspace_root().resolve()
    roots = [ws] + [r.resolve() for r in config.audit_roots()]
    vault = config.vault_root()
    if vault is not None:
        v = vault.expanduser().resolve()
        if v not in roots:
            roots.append(v)
    candidate = Path(path).expanduser()
    candidates = [
        (candidate if candidate.is_absolute() else ws / candidate).resolve(),
    ]
    if vault is not None and not candidate.is_absolute():
        vault_candidate = (vault.expanduser().resolve() / candidate).resolve()
        if vault_candidate not in candidates:
            candidates.append(vault_candidate)
    matches: list[Path] = []
    for resolved in candidates:
        for root in roots:
            if resolved == root or resolved.is_relative_to(root):
                matches.append(resolved)
                break
    if not matches:
        raise ToolError(
            f"path {path!r} is outside the auditable roots "
            f"({', '.join(str(r) for r in roots)}). Add a parent directory to the "
            "UA_AUDIT_ROOTS env var (colon-separated) in the MCP server config to audit it."
        )
    existing = [m for m in matches if m.exists()]
    resolved = existing[0] if existing else matches[0]
    trust_presets.check_path(resolved, for_write=for_write)
    return resolved


DESTRUCTIVE_COMMAND_PREFIX = "destructive command denied:"

_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)
_GIT_VALUE_PREFIXES = (
    "--git-dir=",
    "--work-tree=",
    "--namespace=",
    "--super-prefix=",
)
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_EXEC_TERMINATORS = frozenset({";", "+"})
_DESTRUCTIVE_FIND_EXEC_CMDS = frozenset({"rm", "rmdir", "unlink"})


def _command_basename(token: str) -> str:
    """Normalize argv[0] so ``/bin/rm`` and ``git.exe`` match the same rules as ``rm``/``git``."""
    name = Path(token.replace("\\", "/")).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


def _short_flag_letters(token: str) -> str:
    if len(token) >= 2 and token.startswith("-") and not token.startswith("--"):
        return token[1:]
    return ""


def _destructive_denied(detail: str) -> str:
    return (
        f"{DESTRUCTIVE_COMMAND_PREFIX} {detail}. "
        "run_cmd will not execute this; the approval queue cannot override this gate."
    )


def _rm_destructive_reason(argv: list[str]) -> str | None:
    """Deny recursive+force ``rm`` (combined or split flags), even if ``rm`` is allowlisted."""
    if _command_basename(argv[0]) != "rm":
        return None
    has_r = has_f = False
    for token in argv[1:]:
        if token == "--recursive":
            has_r = True
            continue
        if token == "--force":
            has_f = True
            continue
        letters = _short_flag_letters(token)
        if "r" in letters or "R" in letters:
            has_r = True
        if "f" in letters:
            has_f = True
    if has_r and has_f:
        return _destructive_denied("recursive force-delete (rm -rf)")
    return None


def _git_subcommand_and_rest(argv: list[str]) -> tuple[str, list[str]] | None:
    if _command_basename(argv[0]) != "git":
        return None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in _GIT_VALUE_FLAGS:
            i += 2
            continue
        if token.startswith(_GIT_VALUE_PREFIXES):
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token.lower(), argv[i + 1 :]
    return None


def _is_dot_pathspec(token: str) -> bool:
    return token in {".", "./", ".\\"}


def _has_force_or_path_discard(rest: list[str]) -> bool:
    for token in rest:
        if token in {"--", "--force"} or _is_dot_pathspec(token):
            return True
        if "f" in _short_flag_letters(token):
            return True
    return False


def _git_restore_writes_worktree(rest: list[str]) -> bool:
    staged = "--staged" in rest or any("S" in _short_flag_letters(t) for t in rest)
    worktree = "--worktree" in rest or any("W" in _short_flag_letters(t) for t in rest)
    return worktree or not staged


def _git_destructive_reason(argv: list[str]) -> str | None:
    """Deny force/path checkout, hard reset, and equivalent working-tree discards."""
    parsed = _git_subcommand_and_rest(argv)
    if parsed is None:
        return None
    command, rest = parsed
    if command == "reset" and "--hard" in rest:
        return _destructive_denied("git reset --hard discards uncommitted work")
    if command == "checkout" and _has_force_or_path_discard(rest):
        return _destructive_denied("git checkout discards uncommitted work (force or path)")
    if command == "restore" and _git_restore_writes_worktree(rest):
        return _destructive_denied("git restore overwrites the working tree")
    if command == "switch" and any(
        t in {"--discard-changes", "--force"}
        or "f" in _short_flag_letters(t)
        or "C" in _short_flag_letters(t)
        for t in rest
    ):
        return _destructive_denied("git switch -f/--discard-changes discards uncommitted work")
    if command == "clean" and any(t == "--force" or "f" in _short_flag_letters(t) for t in rest):
        return _destructive_denied("git clean -f deletes untracked files")
    if command == "rm" and any(
        "r" in _short_flag_letters(t) or "R" in _short_flag_letters(t) for t in rest
    ):
        return _destructive_denied("git rm -r recursively removes tracked files")
    return None


def _find_exec_payload_destructive(payload: list[str]) -> bool:
    if not payload:
        return False
    base = _command_basename(payload[0])
    if base in _DESTRUCTIVE_FIND_EXEC_CMDS:
        return True
    return _git_destructive_reason(payload) is not None


def _find_destructive_reason(argv: list[str]) -> str | None:
    """Deny ``find -delete`` and ``-exec``/``-ok`` payloads that delete or hard-reset."""
    if _command_basename(argv[0]) != "find":
        return None
    rest = argv[1:]
    if "-delete" in rest:
        return _destructive_denied("find -delete removes matched files")
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _FIND_EXEC_ACTIONS:
            payload: list[str] = []
            i += 1
            while i < len(rest) and rest[i] not in _FIND_EXEC_TERMINATORS:
                payload.append(rest[i])
                i += 1
            if _find_exec_payload_destructive(payload):
                return _destructive_denied(
                    f"find {token} runs a destructive command ({' '.join(payload[:3])})"
                )
            continue
        i += 1
    return None


def _destructive_command_reason(argv: list[str]) -> str | None:
    """Return a deny message for GateGuard-style destructive argv, or None."""
    return (
        _rm_destructive_reason(argv)
        or _git_destructive_reason(argv)
        or _find_destructive_reason(argv)
    )


def vet_command(command: str) -> list[str]:
    """Split a command line and enforce the first-token allowlist.

    Returns argv for ``subprocess.run(shell=False)``, so shell metacharacters
    (``; | > $()``) are passed through as inert literal arguments.

    After the program allowlist, destructive argv patterns (recursive force
    ``rm``, force/path git checkout and reset-like discards, destructive
    ``find -exec``/``-delete``) are denied. ``UA_SHELL_ALLOWLIST`` replaces
    the default program list; this deny still applies to every program that
    list names, and a one-shot approval cannot authorize a command this layer
    rejects.
    """
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"could not parse command: {e}") from e
    if not argv:
        raise ToolError("empty command")
    allowlist = config.shell_allowlist()
    prog = argv[0]
    if prog not in allowlist:
        raise ToolError(
            f"command {prog!r} is not in the allowlist ({', '.join(sorted(allowlist))}). "
            "UA_SHELL_ALLOWLIST replaces the default list (colon-separated); "
            "include each default program name you still want."
        )
    reason = _destructive_command_reason(argv)
    if reason:
        raise ToolError(reason)
    return argv


# IPv6 forms that carry an IPv4 destination in their low 32 bits which
# ``ipaddress`` does not unwrap itself (``ipv4_mapped`` covers ``::ffff:0:0/96``
# only). Python reports each of these prefixes as ``is_global`` whatever IPv4
# address they wrap, so ``64:ff9b::127.0.0.1`` would otherwise pass as public:
#   ::ffff:0:0:0/96  IPv4-translated (RFC 2765)
#   ::/96            IPv4-compatible (deprecated, RFC 4291)
#   64:ff9b::/96     well-known NAT64 prefix (RFC 6052)
# 6to4 (2002::/16), Teredo (2001::/32) and the NAT64 local-use prefix
# (64:ff9b:1::/48) need no entry: Python already reports them non-global.
_EMBEDDED_IPV4_NETWORKS = (
    ipaddress.IPv6Network("::ffff:0:0:0/96"),
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("64:ff9b::/96"),
)


def _embedded_ipv4(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """IPv4 destination hidden inside an IPv6 address, if this address has one."""
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    mapped = addr.ipv4_mapped
    if mapped is not None:
        return mapped
    if any(addr in network for network in _EMBEDDED_IPV4_NETWORKS):
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return None


def _non_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Python treats CGNAT as neither private nor global, and some multicast
    # addresses as global. IPv6 wrappers around an IPv4 address (mapped,
    # translated, compatible, NAT64) can also report global while the embedded
    # IPv4 destination is restricted, so they are judged by that destination.
    embedded = _embedded_ipv4(addr)
    if embedded is not None:
        addr = embedded
    return not addr.is_global or addr.is_multicast


def check_fetch_host(hostname: str, *, strict: bool = False) -> str:
    """Reject non-public hosts unless UA_ALLOW_LOCAL_FETCH=1 (SSRF guard).

    ``strict=True`` is for fetches an unauthenticated caller can trigger (e.g.
    CIMD client metadata, task #1323): it ignores ``UA_ALLOW_LOCAL_FETCH`` and
    requires every resolved address to be globally routable.

    Returns a vetted IP address string.
    """
    allow_local = config.allow_local_fetch() and not strict
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False

    if is_ip:
        addr = ipaddress.ip_address(hostname)
        if not allow_local:
            if _non_public(addr):
                raise ToolError(
                    f"host {hostname!r} is a non-public address; "
                    "set UA_ALLOW_LOCAL_FETCH=1 to permit local fetches."
                )
        return hostname

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ToolError(f"could not resolve host {hostname!r}: {e}") from e

    vetted_ip = None
    for info in infos:
        ip_str = info[4][0]
        if not allow_local:
            addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
            if _non_public(addr):
                raise ToolError(
                    f"host {hostname!r} resolves to a non-public address ({addr}); "
                    "set UA_ALLOW_LOCAL_FETCH=1 to permit local fetches."
                )
        if vetted_ip is None:
            vetted_ip = ip_str

    if vetted_ip is None:
        raise ToolError(f"could not resolve host {hostname!r} to any IP address")
    return vetted_ip


class SSRFShieldTransport(HTTPTransport):
    """A custom transport that intercepts all outgoing requests (and redirects)

    to check their hosts for SSRF, and pins the connection to the vetted IP
    to prevent DNS rebinding attacks. ``strict=True`` applies
    ``check_fetch_host(strict=True)`` (public-only, no local override).
    """

    def __init__(self, *args, strict: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.strict = strict

    def handle_request(self, request):
        host = request.url.host
        vetted_ip = check_fetch_host(host, strict=self.strict)
        if vetted_ip != host:
            request.url = request.url.copy_with(host=vetted_ip)
            # Ensure Host header is the original domain
            request.headers["Host"] = host
            # Ensure SNI is set to the original domain for TLS/HTTPS
            if request.url.scheme == "https":
                request.extensions["sni_hostname"] = host
        return super().handle_request(request)


def check_policy_gate(
    tool: str,
    payload: dict | None = None,
    episode_id: str | None = None,
    originator: str | None = None,
) -> None:
    """Evaluate call permission for a tool call (task #156, gateway #1357).

    Profile visibility is a separate check in the server wrapper. This gate
    is whether this call is allowed: declarative rules plus per-caller rate
    limits. Raises PolicyViolation (subclass of ToolError) if evaluation
    fails closed or blocks the invocation.
    """
    from .policy import evaluate_policy

    if tool == "git_merge_task":
        from . import gateway

        evaluate_policy(
            tool=tool,
            payload=payload,
            episode_id=episode_id,
            originator=originator,
            _approval_payload=gateway._merge_policy_approval_payload(payload),
        )
        return
    evaluate_policy(tool=tool, payload=payload, episode_id=episode_id, originator=originator)
