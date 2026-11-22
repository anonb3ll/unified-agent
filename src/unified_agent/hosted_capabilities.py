"""Capability boundary for a hosted or containerised hub (task #1442).

The appliance image sets ``UA_WORKSPACE=/data/workspace`` and does not ship a
vault or the iMessage bridge. Tools that list that empty tree must not return
an empty success: an empty result reads as an all-clear. A normal host, whose
workspace is populated, is unchanged. ``UA_HOSTED_WORKSPACE_GUARD=1`` turns the
same check on for any other empty workspace. Tests set it because their temp
directory is not ``/data/workspace``; an operator can set it too. It does not
enable a deployment mode.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import config

# Image default. See Dockerfile ``UA_WORKSPACE`` / ``VOLUME /data``.
CONTAINER_WORKSPACE = Path("/data/workspace")

# Listings whose successful empty output is a census of the workspace.
_WORKSPACE_CENSUS_COMMANDS = frozenset({"ls", "find", "rg", "grep"})
# git init / git clone create the checkout. Every other git subcommand on an
# empty hosted workspace is a census (or would fail looking like one).
_GIT_BOOTSTRAP = frozenset({"init", "clone"})
_GIT_VALUE_FLAGS = frozenset({"-c", "-C", "--git-dir", "--work-tree"})

_WORKSPACE_DETAIL = (
    "workspace {root} is empty, so an empty result would read as success. "
    "A hosted hub (UA_WORKSPACE=/data/workspace) has no local checkout until "
    "one is mounted. Mount a populated tree or point UA_WORKSPACE at one."
)
_VAULT_DETAIL = (
    "vault root {root} is missing or empty, so an empty listing would read as "
    "success. Point the vault setting at a populated vault, or set it to an "
    "empty string to turn vault tools off explicitly."
)


def _dir_unpopulated(path: Path) -> bool:
    """True when ``path`` is missing or a directory with no entries."""
    try:
        if not path.is_dir():
            return True
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return True
    return False


def _guard_active_for(root: Path) -> bool:
    """True only for the container workspace, or the explicit test opt-in.

    An ordinary empty temp directory (the test fixture, a dev scratch dir)
    stays on the default path. Populated trees never trip the guard.
    """
    if not _dir_unpopulated(root):
        return False
    if os.environ.get("UA_HOSTED_WORKSPACE_GUARD") == "1":
        return True
    try:
        return root.resolve() == CONTAINER_WORKSPACE
    except OSError:
        return False


def hosted_workspace_guard_active() -> bool:
    """Agent-facing guard. Uses the caller's workspace binding."""
    return _guard_active_for(config.workspace_root())


def refuse_empty_hosted_workspace(tool: str) -> None:
    """Raise when a workspace census would otherwise return an empty success."""
    if not hosted_workspace_guard_active():
        return
    root = config.workspace_root()
    raise ToolError(f"{tool} refused: " + _WORKSPACE_DETAIL.format(root=root))


def refuse_resolved_empty_workspace(tool: str, resolved: Path) -> None:
    """Raise only when ``resolved`` sits inside the empty hosted workspace.

    An absolute read of a populated project checkout is outside that tree
    and stays allowed.
    """
    if not hosted_workspace_guard_active():
        return
    root = config.workspace_root().resolve()
    try:
        resolved = resolved.resolve()
    except OSError:
        return
    if resolved == root or resolved.is_relative_to(root):
        raise ToolError(f"{tool} refused: " + _WORKSPACE_DETAIL.format(root=root))


def _git_subcommand(argv: list[str]) -> str:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tok
    return ""


def hub_workspace_root() -> Path:
    """Configured ``UA_WORKSPACE``, ignoring the request-scoped agent binding.

    ``process_workspace_root`` still returns a Path override, so a doctor row
    must not use it: two agents would otherwise see different host health.
    """
    return Path(os.environ.get("UA_WORKSPACE") or str(config.default_workspace())).expanduser()


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or resolved.is_relative_to(root)


_GREP_VALUE_FLAGS = frozenset(
    {
        "-e",
        "-f",
        "-m",
        "-A",
        "-B",
        "-C",
        "--regexp",
        "--file",
        "--include",
        "--exclude",
        "--exclude-dir",
        "--max-count",
    }
)
_RG_VALUE_FLAGS = frozenset(
    {
        "-e",
        "-f",
        "-g",
        "-t",
        "-T",
        "-m",
        "-A",
        "-B",
        "-C",
        "--regexp",
        "--file",
        "--glob",
        "--type",
        "--type-not",
        "--max-count",
    }
)
_LS_VALUE_FLAGS = frozenset(
    {
        "-I",
        "--ignore",
        "--hide",
        "-w",
        "--width",
        "-T",
        "--tabsize",
        "--time-style",
        "--quoting-style",
        "--block-size",
        "--format",
        "--sort",
        "--indicator-style",
    }
)
_FIND_GLOBALS = frozenset({"-H", "-L", "-P"})
_FIND_VALUE_FLAGS = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
        "-type",
        "-maxdepth",
        "-mindepth",
        "-printf",
        "-newer",
        "-size",
    }
)


_PATTERN_SUPPLIED = frozenset({"-e", "--regexp", "-f", "--file"})


def _flag_name(tok: str) -> str:
    return tok.split("=", 1)[0]


def _positionals(argv: list[str], value_flags: frozenset[str]) -> tuple[list[str], bool]:
    """Non-option arguments, and whether a flag already supplied the pattern.

    ``-e`` / ``--regexp`` and ``-f`` / ``--file`` carry the pattern, so every
    later positional is a path. A pattern is never itself a path.
    """
    positional: list[str] = []
    saw_pattern_flag = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        name = _flag_name(tok)
        if name in _PATTERN_SUPPLIED:
            saw_pattern_flag = True
        if name in value_flags and "=" not in tok and i + 1 < len(argv):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positional.append(tok)
        i += 1
    return positional, saw_pattern_flag


def _as_path(raw: str, workdir: Path) -> Path:
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else workdir / candidate


def _find_search_roots(argv: list[str]) -> list[str]:
    """Starting-points only. Predicates and ``-exec`` payloads are not roots."""
    roots: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _FIND_GLOBALS:
            i += 1
            continue
        if tok == "-D" and i + 1 < len(argv):
            i += 2
            continue
        if tok.startswith("-O"):
            i += 1
            continue
        if tok == "--":
            roots.extend(argv[i + 1 :])
            break
        if tok.startswith("-") or tok in ("(", "!", ")"):
            break
        roots.append(tok)
        i += 1
    return roots


def _listing_targets(argv: list[str], workdir: Path) -> list[Path]:
    """Paths a census command would actually search.

    ``grep`` and ``rg`` take a pattern before their paths. That pattern is
    not a path, including when it is a relative word such as ``needle``.
    ``-e`` / ``-f`` already supplied it, and ``rg --files`` takes none, so
    every positional is a path in those forms. ``ls -I`` consumes an ignore
    pattern. ``find`` paths stop at the first expression, so ``-name`` and
    ``-exec`` payloads are not roots. No path operand means the command
    searches ``workdir``.
    """
    prog = argv[0]
    if prog in ("grep", "rg"):
        flags = _GREP_VALUE_FLAGS if prog == "grep" else _RG_VALUE_FLAGS
        positional, saw_pattern = _positionals(argv, flags)
        pattern_free = prog == "rg" and any(_flag_name(tok) == "--files" for tok in argv[1:])
        if saw_pattern or pattern_free:
            path_args = positional
        else:
            path_args = positional[1:]
    elif prog == "find":
        path_args = _find_search_roots(argv)
    elif prog == "ls":
        path_args, _saw = _positionals(argv, _LS_VALUE_FLAGS)
    else:
        path_args = [arg for arg in argv[1:] if not arg.startswith("-")]
    if not path_args:
        return [workdir]
    return [_as_path(arg, workdir) for arg in path_args]


def _take_option_value(argv: list[str], i: int) -> tuple[str | None, int]:
    """Value of ``argv[i]`` (``--opt=value`` or the next argument) and the next index."""
    tok = argv[i]
    if "=" in tok:
        return tok.split("=", 1)[1], i + 1
    if i + 1 < len(argv):
        return argv[i + 1], i + 2
    return None, i + 1


def _git_effective_dir(argv: list[str], workdir: Path) -> Path:
    """Tree Git will inspect, after global ``-C``, ``--git-dir``, and ``--work-tree``.

    ``-C`` is applied as it is seen (a relative ``-C`` is relative to the
    previous one). ``--git-dir`` and ``--work-tree`` are resolved against the
    directory after the ``-C`` options already seen, not skipped. The work
    tree wins when both are set; otherwise the git dir; otherwise the
    directory after ``-C``.
    """
    current = workdir
    git_dir: Path | None = None
    work_tree: Path | None = None
    i = 1
    while i < len(argv):
        tok = argv[i]
        name = _flag_name(tok)
        if tok == "-C" or (tok.startswith("-C") and not tok.startswith("--")):
            if tok == "-C":
                value, i = _take_option_value(argv, i)
            else:
                value, i = tok[2:], i + 1
            if value is not None:
                current = _as_path(value, current)
            continue
        if name in ("--git-dir", "--work-tree"):
            value, i = _take_option_value(argv, i)
            if value is not None:
                resolved = _as_path(value, current)
                if name == "--git-dir":
                    git_dir = resolved
                else:
                    work_tree = resolved
            continue
        if name == "-c" or (tok.startswith("-c") and not tok.startswith("--")):
            if tok == "-c":
                _value, i = _take_option_value(argv, i)
            else:
                i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if work_tree is not None:
        return work_tree
    if git_dir is not None:
        return git_dir
    return current


def empty_hosted_census_message(argv: list[str], workdir: Path | None = None) -> str | None:
    """Error text when ``argv`` would census an empty hosted workspace.

    ``git init`` and ``git clone`` are not a census: they create the tree.
    A listing is refused only when one of its targets is that empty tree.
    A grep/rg pattern is not a target. Git is classified after ``-C``,
    ``--git-dir``, and ``--work-tree``, not from the empty cwd alone.
    A command whose cwd is a populated task worktree, or whose every path
    operand is outside the workspace, is not a census of the empty tree.
    One outside operand does not exempt a command that also names the
    empty workspace. Returns None when the command should run.
    """
    if not argv or not hosted_workspace_guard_active():
        return None
    root = config.workspace_root().resolve()
    cwd = workdir.resolve() if workdir is not None else root
    prog = argv[0]
    if prog == "git":
        sub = _git_subcommand(argv)
        if sub in _GIT_BOOTSTRAP or not sub:
            return None
        if not _inside(_git_effective_dir(argv, cwd), root):
            return None
        return _WORKSPACE_DETAIL.format(root=root)
    if prog not in _WORKSPACE_CENSUS_COMMANDS:
        return None
    if any(_inside(target, root) for target in _listing_targets(argv, cwd)):
        return _WORKSPACE_DETAIL.format(root=root)
    return None


def refuse_empty_hosted_command(tool: str, argv: list[str], workdir: Path | None = None) -> None:
    """Raise for a census command on an empty hosted workspace."""
    message = empty_hosted_census_message(argv, workdir)
    if message:
        raise ToolError(f"{tool} refused: {message}")


def vault_disabled() -> bool:
    """True when ``UA_VAULT_PATH=''`` turns vault tools off on purpose."""
    return config.vault_root() is None


def vault_unpopulated() -> bool:
    """True when vault tools are pointed at a missing or empty directory.

    A disabled vault (``UA_VAULT_PATH=''``) is not an empty directory. It is
    reported separately by :func:`vault_disabled`.
    """
    root = config.vault_root()
    if root is None:
        return False
    return _dir_unpopulated(root)


def refuse_unpopulated_vault(tool: str) -> None:
    """Raise for a missing or empty vault while the hosted guard is active.

    A missing vault already fails in ``resolve_in_vault``. This closes the
    remaining hole: a directory that exists and contains nothing, which
    ``vault_list`` would otherwise return as an empty success. A normal host
    with a populated vault is unchanged, and the guard stays off there.
    """
    # Vault capabilities belong to the host, not to the caller's fs/run/git
    # workspace. Keep the hosted empty-vault guard while allowing a DENY
    # identity to reach the separately enforced vault path/profile boundary.
    if not _guard_active_for(hub_workspace_root()):
        return
    root = config.vault_root()
    if root is None or not _dir_unpopulated(root):
        return
    raise ToolError(f"{tool} refused: " + _VAULT_DETAIL.format(root=root))


def notify_bridge_absent() -> bool:
    """True when the iMessage ``send.ts`` bridge is not on disk.

    Uses the process workspace when ``UA_BRIDGE_DIR`` is unset, so a
    ``UA_AGENT_WORKSPACES`` DENY identity does not turn this probe into
    UNKNOWN.
    """
    from .tools.notify import _BRIDGE_DIR_ENV, _DEFAULT_BRIDGE_DIR

    raw = os.environ.get(_BRIDGE_DIR_ENV)
    if raw:
        script = Path(raw).expanduser().resolve() / "src" / "send.ts"
    else:
        script = hub_workspace_root() / _DEFAULT_BRIDGE_DIR / "src" / "send.ts"
    return not script.is_file()


def degraded_families(root: Path | None = None) -> list[str]:
    """Families whose local preconditions are absent. Order is stable.

    ``root`` defaults to the configured ``UA_WORKSPACE``, not a request-scoped
    agent binding, so a DENY identity and a bound agent see the same host.
    """
    if root is None:
        root = config.process_workspace_root()
    families: list[str] = []
    if _dir_unpopulated(root):
        families.append("workspace")
    if vault_disabled():
        families.append("vault (disabled)")
    elif vault_unpopulated():
        families.append("vault")
    if notify_bridge_absent():
        families.append("notify")
    return families


def doctor_check() -> dict:
    """One doctor row for the hosted local-dependency boundary.

    This reports the consequence. It does not enable a deployment mode
    (that enum belongs to task #1422). DEGRADED only when the hosted guard
    is active (empty ``/data/workspace`` or the test opt-in). A fresh
    install missing those local trees is DORMANT, so ``unified-agent doctor``
    does not fail a normal cold start.
    """
    root = hub_workspace_root()
    families = degraded_families(root)
    gate = "UA_WORKSPACE, UA_VAULT_PATH, UA_BRIDGE_DIR"
    if not families:
        return {
            "capability": "hosted_local_dependencies",
            "status": "LIVE",
            "detail": (
                "workspace, vault, and notification bridge are present; "
                "hosted empty-success guard is inactive"
            ),
            "gate": gate,
        }
    named = ", ".join(families)
    if _guard_active_for(root):
        return {
            "capability": "hosted_local_dependencies",
            "status": "DEGRADED",
            "detail": (
                f"hosted workspace is empty; absent families: {named}. "
                "Workspace census tools refuse an empty listing; notify "
                "raises when send.ts is missing. No deployment mode was enabled."
            ),
            "gate": gate,
        }
    return {
        "capability": "hosted_local_dependencies",
        "status": "DORMANT",
        "detail": (
            f"local families not all present ({named}); not an empty "
            "hosted workspace, so the empty-success guard is inactive"
        ),
        "gate": gate,
    }
