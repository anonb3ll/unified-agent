"""Worktree lifecycle policy tool.

Scans for stale or abandoned worktrees (> max_idle_days idle or associated with
completed/blocked tasks) and safely purges them to keep git workspace lightweight.

Only real git worktrees are ever considered. `.worktrees/` also holds non-repo
directories — the Pi executor writes its RPC session transcripts to
`.worktrees/sessions/` — and this tool must not treat those as prunable. A
cleanup routine that cannot identify what it is deleting has no business
deleting it.
"""

import contextvars
import errno
import functools
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import config, storage
from .git import git_worktree_remove

# How deep to follow `.worktrees/` chains. Legitimate layouts are flat; the
# nesting this exists to find is one level deep. The bound is a loop guard.
_MAX_NEST_DEPTH = 5


def _is_git_worktree(path: Path) -> bool:
    """True if `path` is a git worktree root (or a full clone).

    A linked worktree carries a `.git` FILE pointing at the parent repo's
    gitdir; a normal clone carries a `.git` directory. Anything else under
    `.worktrees/` is just a directory that happens to live there.
    """
    return (path / ".git").exists()


def _iter_worktree_dirs(base: Path, depth: int = 0) -> Iterator[Path]:
    """Yield candidate worktree directories under `base`, descending into nesting.

    Scanning only `base.iterdir()` missed `.worktrees/task-180/.worktrees/task-159`
    (task #187) — a worktree created inside another one, which is precisely the
    shape our own creation path used to produce.

    This walks `<dir>/.worktrees/` rather than reading `git worktree list`
    because it also finds directories git never registered (a half-created
    worktree, a leftover from a deleted repo), and because it works in a
    workspace that is not itself a git repo. The trade-off: nesting somewhere
    other than a `.worktrees/` subdirectory is not found.
    """
    if depth > _MAX_NEST_DEPTH:
        return
    try:
        items = sorted(base.iterdir())
    except OSError:
        return
    for item in items:
        if item.is_symlink() or not item.is_dir():
            continue
        yield item
        nested = item / ".worktrees"
        if nested.is_dir() and not nested.is_symlink():
            yield from _iter_worktree_dirs(nested, depth + 1)


def _task_id_from_folder(name: str) -> int | None:
    if not name.startswith("task-"):
        return None
    try:
        return int(name.split("-")[1])
    except (IndexError, ValueError):
        return None


# Deploy checkouts (`.worktrees/release-<task>-<sha>`, and a rollback checkout
# kept beside one) are what the hub, node and executor services run from. They
# are clean and contained in main by construction, so they always read as
# disposable; they are retired by the deploy steward, never by idle cleanup.
# The match is a bare prefix on purpose and over-matches: `release-notes` or
# `rollback-hotfix` is kept too. Deploy names have no fixed grammar
# (`release-<sha8>`, `release-<task>-<sha8>`, `release-<date>-<n>`, ...), and
# this guard is the backstop for code a process scan cannot see, so keeping an
# ordinary worktree is the right side to err on (task #2178 review).
_DEPLOY_CHECKOUT_PREFIXES = ("release-", "rollback-")


def _is_deploy_checkout(name: str) -> bool:
    return name.startswith(_DEPLOY_CHECKOUT_PREFIXES)


# The node release links `scripts/node-release-roll.sh` maintains (task #2298).
# A supervisor runs whatever `current` points at, and `previous` is the
# rollback, so neither target is ever a cleanup candidate, whatever its name.
# Deleting a live target put a node into a 203/EXEC restart loop (task #2292).
_RELEASE_LINK_NAMES = ("current", "previous")


def _release_link_targets() -> set[Path]:
    """Resolved targets of the node release links under ~ and UA_HOME."""
    bases = {Path.home() / ".unified_agent", config.data_home()}
    targets: set[Path] = set()
    for base in bases:
        for name in _RELEASE_LINK_NAMES:
            link = base / "release" / name
            try:
                if link.is_symlink():
                    targets.add(link.resolve())
            except (OSError, RuntimeError):
                continue
    return targets


def _is_release_link_target(item: Path) -> bool:
    targets = _release_link_targets()
    if not targets:
        return False
    try:
        return item.resolve() in targets
    except (OSError, RuntimeError):
        # Unresolvable: keep it rather than guess.
        return True


_UNSET: Any = object()


# Environment variables whose values name code a process imports or runs:
# `python -m pkg` with PYTHONPATH pointing into a checkout leaves no other trace.
_CODE_PATH_ENV = ("PYTHONPATH", "VIRTUAL_ENV")

_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# A `PATH=` assignment anywhere in a word (`X=1;PATH=bin;tool`), not MANPATH=.
_PATH_ASSIGNMENT_RE = re.compile(r"(?<![A-Za-z0-9_])PATH=")


def _env_code_paths(assignments: list[str], cwd: str | None = None) -> set[str]:
    """Paths named by PYTHONPATH / VIRTUAL_ENV in `NAME=value` strings.

    Python reads a relative entry against its own cwd, so a relative entry
    (and the empty one, which is the cwd) is joined to the process's `cwd`,
    and skipped when that is unknown: the hub's own cwd says nothing about
    the process. Each path is kept raw, normalized and resolved
    (`_path_forms`; task #2209).
    """
    paths: set[str] = set()
    for item in assignments:
        name, sep, value = item.partition("=")
        if sep and name in _CODE_PATH_ENV:
            for part in value.split(os.pathsep):
                if part.startswith("/"):
                    paths.update(_path_forms(part))
                elif cwd:
                    paths.update(_path_forms(os.path.join(cwd, part) if part else cwd))
    return paths


def _env_value(assignments: list[str], name: str) -> str | None:
    """The last `name=value` among `assignments`, or None."""
    value = None
    for item in assignments:
        key, sep, val = item.partition("=")
        if sep and key == name:
            value = val
    return value


# Programs that run the program named later on their command line, with the
# options that take a separate argument (task #2192). An option not listed is
# taken to stand alone; long options written `--name=value` are one token.
# `env -S <string>` is handled apart: its string is split like a `-c` string.
# The table is illustrative, not complete (`watch`, `stdbuf`, `time -v` are
# absent): a wrapper missing here hides only a bare-name program, and the
# argv[0] and `-c` bare-word lookups still over-keep around it.
_WRAPPER_ARG_OPTS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "sudo": frozenset(
        {
            *("-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir"),
            *("-h", "--host", "-p", "--prompt", "-r", "--role", "-t", "--type"),
            *("-T", "--command-timeout", "-U", "--other-user", "-R", "--chroot"),
        }
    ),
    "doas": frozenset({"-u", "-C"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "xargs": frozenset(
        {
            *("-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L"),
            *("-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars"),
            "--process-slot-var",
        }
    ),
    # Shell builtins that run a program, seen inside a `sh -c` string.
    "exec": frozenset({"-a"}),
    "command": frozenset(),
}
# Wrappers that take `NAME=value` assignments before the program, `--` or not.
_ASSIGNING_WRAPPERS = frozenset({"env", "sudo"})
# Positional arguments before the program (`timeout DURATION cmd`). Options
# may come after them too: GNU getopt permutes (`timeout 30 -k 5 cmd`).
_WRAPPER_POSITIONALS = {"timeout": 1}
_MAX_WRAPPERS = 8

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"})
_SHELL_ARG_OPTS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
# Reserved words and keywords that come before the program of a simple
# command (`while true; do tool; done`, `time tool`, `{ tool; }`).
_SHELL_KEYWORDS = frozenset(
    {
        *("while", "until", "for", "select", "do", "done", "if", "then", "elif"),
        *("else", "fi", "case", "esac", "time", "builtin", "coproc", "function"),
        *("{", "}", "!", "[", "[[", "]]"),
    }
)
# `find ... -exec <cmd> ;` and its siblings run <cmd>.
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# `sh -c` strings are parsed this deep (`sh -c "sh -c '...'"`). shlex reads
# strings up to `_MAX_SHLEX_CHARS`; a longer one, or one it cannot parse, is
# split on operators and whitespace instead, whole, up to the kernel's limit
# for one argument (`_MAX_SHELL_CHARS`). Every bare word of a `-c` string is
# also looked up on PATH, the first `_MAX_BARE_WORDS` distinct ones. A
# process's own PATH and the directories its `PATH=` overrides add are each
# searched up to `_MAX_PATH_DIRS` directories; any past that is kept as a path
# itself, with every symbolic link in it followed (`_path_dirs`,
# `_path_overflow`, `_extend_override`; tasks #2209, #2226).
_MAX_SHELL_DEPTH = 2
_MAX_SHLEX_CHARS = 8192
_MAX_SHELL_CHARS = 131072
_MAX_BARE_WORDS = 2048
_MAX_PATH_DIRS = 64


def _unwrap(argv: list[str]) -> tuple[int, list[str], list[str]]:
    """Where the program `argv` runs starts once leading wrappers are skipped.

    `env X=1 tool`, `sudo -- X=1 tool`, `nice -n 5 tool`, `timeout 30 -k 5
    tool`, `xargs -n 1 tool` and `nohup setsid tool` all run `tool`. Returns
    (index, strings, path_values): the index may equal len(argv) when nothing
    follows the wrappers, `strings` holds any `env -S` string, which is a
    command line of its own, and `path_values` every `PATH=` a wrapper
    assigns (`env PATH=<wt>/bin tool` runs <wt>/bin/tool; task #2199).
    Bounded to `_MAX_WRAPPERS` hops.
    """
    i = 0
    embedded: list[str] = []
    path_values: list[str] = []
    for _ in range(_MAX_WRAPPERS):
        if i >= len(argv):
            break
        name = os.path.basename(argv[i])
        arg_opts = _WRAPPER_ARG_OPTS.get(name)
        if arg_opts is None:
            break
        i += 1
        positionals = _WRAPPER_POSITIONALS.get(name, 0)
        options = True
        while i < len(argv):
            tok = argv[i]
            if options and tok == "--":
                options = False
                i += 1
            elif options and name == "env" and tok == "-":
                # `env - tool` is POSIX shorthand for `env -i tool`.
                i += 1
            elif options and tok.startswith("-") and len(tok) > 1:
                if name == "env" and tok in ("-S", "--split-string"):
                    embedded.extend(argv[i + 1 : i + 2])
                    i += 2
                elif name == "env" and tok.startswith("--split-string="):
                    embedded.append(tok.partition("=")[2])
                    i += 1
                elif name == "env" and tok.startswith("-S"):
                    embedded.append(tok[2:])
                    i += 1
                else:
                    i += 2 if tok in arg_opts else 1
            elif name in _ASSIGNING_WRAPPERS and _ENV_ASSIGNMENT_RE.match(tok):
                if tok.startswith("PATH="):
                    path_values.append(tok[5:])
                i += 1
            elif positionals:
                positionals -= 1
                i += 1
            else:
                break
    return min(i, len(argv)), embedded, path_values


def _shell_command(argv: list[str]) -> str | None:
    """The command string of `sh -c '<cmd>'` (or another of `_SHELLS`), else None."""
    if not argv or os.path.basename(argv[0]).lstrip("-") not in _SHELLS:
        return None
    has_c = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            i += 1
            break
        if len(tok) > 1 and tok[0] in "-+":
            if not tok.startswith("--") and "c" in tok[1:]:
                has_c = True
            i += 2 if tok in _SHELL_ARG_OPTS else 1
            continue
        break
    return argv[i] if has_c and i < len(argv) else None


def _find_exec_commands(argv: list[str]) -> list[list[str]]:
    """The commands `find ... -exec <cmd> ;` (or -execdir, -ok, -okdir) runs."""
    if not argv or os.path.basename(argv[0]) != "find":
        return []
    commands: list[list[str]] = []
    i = 1
    while i < len(argv):
        if argv[i] in _FIND_EXEC:
            j = i + 1
            while j < len(argv) and argv[j] not in (";", "+"):
                j += 1
            if j > i + 1:
                commands.append(argv[i + 1 : j])
            i = j
        i += 1
    return commands


def _shell_commands(command: str) -> list[list[str]]:
    """The words of each simple command in a `-c` string.

    Backticks and newlines separate commands, like `;`. shlex splits words
    and the `; && || | &` operators between commands. A string it cannot
    parse (an unbalanced quote), or one past `_MAX_SHLEX_CHARS`, is split on
    those operators and whitespace with quotes dropped instead: cruder, but
    it reads the whole string and can only add candidate paths, never hide
    one that parsing would have found.
    """
    text = command[:_MAX_SHELL_CHARS].replace("`", " ; ").replace("\n", " ; ")
    tokens: list[str] | None = None
    if len(text) <= _MAX_SHLEX_CHARS:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = None
    if tokens is None:
        return [
            words
            for segment in re.split(r"[();<>|&]+", text)
            if (words := [w.strip("'\"") for w in segment.split() if w.strip("'\"")])
        ]
    commands: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= _SHELL_OPERATOR_CHARS:
            if current:
                commands.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        commands.append(current)
    return commands


def _simple_command(words: list[str]) -> tuple[list[str], list[str]]:
    """`words` without the leading keywords and `NAME=value` assignments.

    Also returns the values of the leading `PATH=` assignments: `PATH=<wt>/bin
    tool` runs <wt>/bin/tool (task #2199).
    """
    i = 0
    path_values: list[str] = []
    while i < len(words) and (words[i] in _SHELL_KEYWORDS or _ENV_ASSIGNMENT_RE.match(words[i])):
        if words[i].startswith("PATH="):
            path_values.append(words[i][5:])
        i += 1
    return words[i:], path_values


def _path_dirs(path_env: str) -> tuple[list[str], list[str]]:
    """The absolute entries of `path_env`, without repeats, split at the cap.

    Returns (searched, overflow): the first `_MAX_PATH_DIRS` entries, which
    lookups search, and the rest, which they skip. A PATH of ~15k entries
    otherwise cost every bare word ~15k stats (task #2209). The scans keep the
    overflow as live paths (`_path_overflow`), so a capped search never hides
    a checkout: anything found in a directory lies under it, under its
    realpath, or behind a link in it (`_unsearched_dir_forms`).
    """
    dirs = list(dict.fromkeys(d for d in path_env.split(os.pathsep) if d.startswith("/")))
    return dirs[:_MAX_PATH_DIRS], dirs[_MAX_PATH_DIRS:]


# Resolving a process-supplied path walks it like the kernel does: one
# component at a time, following at most `_MAX_SYMLINKS` links (Linux
# MAXSYMLINKS). One scan walks at most `_RESOLVE_BUDGET` components in all,
# and a resolved path may hold at most `_MAX_RESOLVED_CHARS` characters. A
# path past any of these limits is not dropped: it makes liveness unknown,
# so cleanup removes nothing on its basis (review of task #2209).
_MAX_SYMLINKS = 40
_RESOLVE_BUDGET = 2_000_000
_MAX_RESOLVED_CHARS = 8192


class _Unresolvable(Exception):
    """A process-supplied path that could not be resolved within those limits.

    `where` names the process ("pid <n> (<comm>)") once a scan knows it. The
    path itself is left out of the message: a command line may carry data.
    """

    def __init__(self, reason: str, where: str = "") -> None:
        self.reason = reason
        self.where = where
        super().__init__(f"{where}: {reason}" if where else reason)

    def at(self, where: str) -> "_Unresolvable":
        return _Unresolvable(self.reason, where)


class _Resolver:
    """Iterative, bounded `realpath` for one scan.

    `os.path.realpath` follows any number of links (the kernel stops at 40),
    recursed once per link before Python 3.12, and its work was bounded only
    by trimming the input, which hid long relative paths the kernel resolves
    from the cwd. This walks a component at a time with `lstat`/`readlink`,
    with no recursion, memoizing each path and each node it reads.

    Only an error that proves the path names nothing ends the walk quietly,
    with the rest kept as written after the real prefix: ENOENT or ENOTDIR;
    a component over `_MAX_NAME_BYTES`, past every filesystem name limit
    the hub meets; or ENAMETOOLONG on a candidate shorter than the
    platform's PATH_MAX, where the kernel has rejected the component itself
    and would reject it for the process too. Name limits differ by
    filesystem and encoding (255 bytes on ext4 and ZFS, 255 UTF-16 units on
    HFS+, 255 characters on APFS, so up to 1,020 UTF-8 bytes), so a fixed
    byte limit below that is never assumed (review of task #2209, round 4).
    Any other ENAMETOOLONG (the scanner's absolute path past PATH_MAX, while
    the process opens it relative to a long cwd), ELOOP, EOVERFLOW and any
    other error raise `_Unresolvable`, as do a 41st link, a resolved path
    past `_MAX_RESOLVED_CHARS` and a spent budget.

    A directory the hub may not search (EACCES, EPERM) depends on whose path
    it is (`strict`). For a process with the hub's own credentials it is
    unknown: a running script stays open after its directory is made
    unsearchable. For another user's process it ends the walk, the boundary
    the scan already accepts for their unreadable /proc entries; treating it
    as unknown would leave liveness unknown whenever a root process names a
    path under /root (review of task #2209, round 3).
    """

    def __init__(self, budget: int = _RESOLVE_BUDGET) -> None:
        self.budget = budget
        # Whose path is being resolved: True for the hub's own credentials,
        # and outside a scan, where nothing says otherwise.
        self.strict = True
        self._paths: dict[str, tuple[str, bool] | _Unresolvable] = {}
        # candidate -> link target, "" for a non-link, or a walk-ending marker
        self._nodes: dict[str, str | _Unresolvable] = {}
        # directory -> the symbolic links directly in it, or a marker
        self._links: dict[str, tuple[str, ...] | _Unresolvable] = {}

    def links_in(self, directory: str) -> tuple[str, ...]:
        """The symbolic links directly in `directory`, listed once per scan.

        Each entry listed spends one unit of the scan's budget. A directory
        that does not exist holds none; one the hub may not list is handled
        as `resolve` handles a denied component, and any other error raises
        `_Unresolvable` (task #2226).
        """
        hit = self._links.get(directory)
        if hit is None:
            hit = self._list_links(directory)
            self._links[directory] = hit
        if hit is _DENIED:
            if self.strict:
                raise _Unresolvable("permission denied listing a PATH directory")
            return ()
        if isinstance(hit, _Unresolvable):
            raise hit
        return hit

    def _list_links(self, directory: str) -> tuple[str, ...] | _Unresolvable:
        links: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.budget -= 1
                    if self.budget < 0:
                        return _Unresolvable("the scan's path-resolution budget is spent")
                    if entry.is_symlink():
                        links.append(os.path.join(directory, entry.name))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return ()
            if exc.errno == errno.ENAMETOOLONG and len(os.fsencode(directory)) < _PATH_MAX:
                return ()
            if exc.errno in (errno.EACCES, errno.EPERM):
                return _DENIED
            return _Unresolvable(errno.errorcode.get(exc.errno or 0, "OSError"))
        return tuple(links)

    def resolve(self, path: str) -> str:
        hit = self._paths.get(path)
        if hit is None:
            try:
                hit = self._walk(path)
            except _Unresolvable as exc:
                hit = exc
            self._paths[path] = hit
        if isinstance(hit, _Unresolvable):
            raise hit
        result, denied = hit
        if denied and self.strict:
            raise _Unresolvable("permission denied on a path component")
        return result

    def _node(self, candidate: str) -> str | _Unresolvable:
        node = self._nodes.get(candidate)
        if node is None:
            try:
                st = os.lstat(candidate)
                node = os.readlink(candidate) if stat.S_ISLNK(st.st_mode) else ""
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    node = _MISSING
                elif exc.errno == errno.ENAMETOOLONG and len(os.fsencode(candidate)) < _PATH_MAX:
                    node = _MISSING
                elif exc.errno in (errno.EACCES, errno.EPERM):
                    node = _DENIED
                else:
                    node = _Unresolvable(errno.errorcode.get(exc.errno or 0, "OSError"))
            self._nodes[candidate] = node
        return node

    def _walk(self, path: str) -> tuple[str, bool]:
        """(resolved path, whether a denied directory ended the walk)."""
        pending = path.split("/")
        pending.reverse()
        parts: list[str] = []
        length = 0
        links = 0
        while pending:
            self.budget -= 1
            if self.budget < 0:
                raise _Unresolvable("the scan's path-resolution budget is spent")
            name = pending.pop()
            if not name or name == ".":
                continue
            if name == "..":
                if parts:
                    length -= len(parts.pop()) + 1
                continue
            candidate = "/" + "/".join(parts) + ("/" if parts else "") + name
            if len(os.fsencode(name)) > _MAX_NAME_BYTES:
                node: str | _Unresolvable = _MISSING
            else:
                node = self._node(candidate)
            if node is _MISSING or node is _DENIED:
                rest = "/".join(reversed(pending))
                return (f"{candidate}/{rest}" if rest else candidate), node is _DENIED
            if isinstance(node, _Unresolvable):
                raise node
            if node:
                links += 1
                if links > _MAX_SYMLINKS:
                    raise _Unresolvable("too many symbolic links")
                if node.startswith("/"):
                    parts.clear()
                    length = 0
                pending.extend(reversed(node.split("/")))
                continue
            parts.append(name)
            length += len(name) + 1
            if length > _MAX_RESOLVED_CHARS:
                raise _Unresolvable("resolved path too long")
        return "/" + "/".join(parts), False


def _platform_path_max() -> int:
    """PATH_MAX here (4096 on Linux, 1024 on macOS); 1024 when it cannot be read.

    A smaller value only makes fewer ENAMETOOLONG errors count as proof.
    """
    try:
        value = os.pathconf("/", "PC_PATH_MAX")
    except (OSError, ValueError):
        return 1024
    return value if 0 < value <= 65536 else 1024


# Walk-ending markers for `_Resolver._node`; the name length no filesystem the
# hub runs on accepts (APFS: 255 characters, at most 1,020 UTF-8 bytes).
_MISSING = _Unresolvable("names nothing")
_DENIED = _Unresolvable("permission denied")
_MAX_NAME_BYTES = 1024
_PATH_MAX = _platform_path_max()
_SCAN_RESOLVER: contextvars.ContextVar[_Resolver | None] = contextvars.ContextVar(
    "_SCAN_RESOLVER", default=None
)


def _with_scan_resolver(func: Callable[..., Any]) -> Callable[..., Any]:
    """Run `func` with one `_Resolver` (memo and budget) for the whole scan."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _SCAN_RESOLVER.set(_Resolver())
        try:
            return func(*args, **kwargs)
        finally:
            _SCAN_RESOLVER.reset(token)

    return wrapper


def _path_forms(path: str) -> tuple[str, ...]:
    """A process-supplied path as given, normalized, and as the filesystem resolves it.

    Nothing the scan reads from a process is collapsed lexically on its own:
    in `<link>/../bin` the kernel follows `link` before `..`, so normpath
    alone can name another directory than the one the process uses, and an
    active checkout would read as idle (review of task #2209). Keeping every
    form means that any one lying in a checkout keeps it. Resolution uses the
    scan's `_Resolver` (a fresh one outside a scan) and raises `_Unresolvable`
    rather than drop the resolved form.
    """
    forms = {path, os.path.normpath(path)}
    if path.startswith("/"):
        forms.add((_SCAN_RESOLVER.get() or _Resolver()).resolve(path))
    return tuple(forms)


def _unsearched_dir_forms(directory: str) -> list[str]:
    """What a PATH directory past the lookup cap keeps live: the directory
    (`_path_forms`), and each symbolic link in it, followed. A lookup there
    could find a link to a checkout's program, which lies under neither the
    directory nor its realpath (task #2226).
    """
    resolver = _SCAN_RESOLVER.get() or _Resolver()
    forms = list(_path_forms(directory))
    for link in resolver.links_in(directory):
        forms.extend(_path_forms(link))
    return forms


def _path_overflow(path_env: str | None, seen: set[str]) -> list[str]:
    """The directories of a process's PATH that `_which` does not search.

    Each distinct PATH is read once per scan (`seen`); a PATH within the cap
    adds nothing. The scan adds these to its live paths, raw and resolved,
    with the links in them followed (`_unsearched_dir_forms`; tasks #2209,
    #2226).
    """
    if not path_env:
        return []
    resolver = _SCAN_RESOLVER.get()
    # Read once per PATH and strictness: a denied directory raises only when strict.
    key = "\0" + path_env if resolver is not None and resolver.strict else path_env
    if key in seen:
        return []
    seen.add(key)
    if path_env.count(os.pathsep) < _MAX_PATH_DIRS:
        return []
    return [form for d in _path_dirs(path_env)[1] for form in _unsearched_dir_forms(d)]


def _which(name: str, path_env: str | None, cache: dict[tuple[str, str], str | None]) -> str | None:
    """`name` found on the absolute entries of `path_env`, memoized in `cache`.

    One scan makes thousands of lookups against a handful of PATH values, so
    the cache is keyed on the raw `(name, path_env)`, and each distinct
    `path_env` is filtered to its absolute entries once, under the key
    `("", path_env)` (no lookup uses an empty name; task #2199). Only the
    first `_MAX_PATH_DIRS` of them are searched (`_path_dirs`, task #2209).
    The hit is returned as `shutil.which` built it, `<dir>/<name>`, never
    normalized: `_lookups` derives the forms kept as live paths.
    """
    if not name or "/" in name or not path_env:
        return None
    key = (name, path_env)
    if key in cache:
        return cache[key]
    dirs_key = ("", path_env)
    if dirs_key not in cache:
        cache[dirs_key] = os.pathsep.join(_path_dirs(path_env)[0]) or None
    dirs = cache[dirs_key]
    found = shutil.which(name, path=dirs) if dirs else None
    cache[key] = found or None
    return cache[key]


def _extend_override(
    override: tuple[str, ...],
    values: list[str],
    cwd: str | None,
    paths: set[str],
) -> tuple[str, ...]:
    """`override` with the directories of each `PATH=` value in `values` added.

    An override is the list of directories that `PATH=` assignments (a
    wrapper's, a `-c` string's) put on top of the process's own PATH. An
    absolute entry is kept as it is; a relative one is joined to the
    process's `cwd`, and the empty entry (which a shell reads as the cwd) is
    the cwd; both are skipped when the cwd is unknown, since the hub's own
    cwd says nothing about the process (task #2209). Nothing is collapsed
    lexically: the lookup lets the filesystem resolve `<link>/../bin` as the
    kernel does (`_path_forms`). Repeats are dropped.

    Lookups search at most `_MAX_PATH_DIRS` directories, so a command
    holding thousands of distinct `PATH=` words costs a bounded number of
    stats per word. A directory past the cap is added to `paths` instead,
    raw and resolved, with each symbolic link in it followed: anything a
    lookup could find in it lies under one of those, so keeping them keeps
    the same checkouts and a capped override can never hide one (tasks
    #2209, #2226).
    """
    if not values:
        return override
    dirs = list(override)
    seen = set(override)
    for value in values:
        for entry in value.split(os.pathsep):
            if entry.startswith("/"):
                directory = entry
            elif cwd:
                directory = os.path.join(cwd, entry) if entry else cwd
            else:
                continue
            if directory in seen:
                continue
            seen.add(directory)
            if len(dirs) < _MAX_PATH_DIRS:
                dirs.append(directory)
            else:
                paths.update(_unsearched_dir_forms(directory))
    return tuple(dirs)


def _lookups(
    name: str,
    path_env: str | None,
    override: tuple[str, ...],
    cache: dict[tuple[str, str], str | None],
) -> Iterator[str]:
    """`name` on `path_env` (first match), then in each override directory.

    Every override directory is searched, not only the first that holds
    `name`, so `PATH=/opt/bin` and `PATH=<wt>/bin` in one command both count:
    over-keeping is the safe side. Each hit yields its `_path_forms`.
    """
    for env in (path_env, *override):
        found = _which(name, env, cache)
        if found:
            yield from _path_forms(found)


def _command_string_paths(
    command: str,
    cwd: str | None,
    path_env: str | None,
    which_cache: dict[tuple[str, str], str | None],
    depth: int,
    path_override: tuple[str, ...] = (),
) -> set[str]:
    """Paths a `-c` (or `env -S`) string names: `_argv_paths` per simple command.

    Shell syntax this does not model (a function body, `parallel tool`) can
    still hide the program, so every bare word is also looked up on PATH: a
    pure over-keep backstop, bounded to `_MAX_BARE_WORDS` distinct words.
    A simple command's leading `PATH=` assignments resolve its program, and
    every `PATH=` word in the string (`export PATH=<wt>/bin; tool` too) joins
    the override the bare words are looked up on, besides `path_env` and the
    `path_override` an enclosing wrapper set (task #2199). The override is
    bounded by `_extend_override`, so the cost grows with the words, not
    with words times `PATH=` values (task #2209).
    """
    paths: set[str] = set()
    bare: set[str] = set()
    string_paths: list[str] = []
    for words in _shell_commands(command):
        program, assigned = _simple_command(words)
        override = _extend_override(path_override, assigned, cwd, paths)
        paths |= _argv_paths(program, cwd, path_env, which_cache, depth, override)
        string_paths.extend(word[5:] for word in words if word.startswith("PATH="))
        for word in words:
            if len(bare) >= _MAX_BARE_WORDS:
                break
            if (
                word
                and "/" not in word
                and not word.startswith("-")
                and not _ENV_ASSIGNMENT_RE.match(word)
                and word not in _SHELL_KEYWORDS
            ):
                bare.add(word)
    override = _extend_override(path_override, string_paths, cwd, paths)
    for word in bare:
        paths.update(_lookups(word, path_env, override, which_cache))
    return paths


def _argv_paths(
    argv: list[str],
    cwd: str | None,
    path_env: str | None,
    which_cache: dict[tuple[str, str], str | None] | None = None,
    _depth: int = 0,
    _path_override: tuple[str, ...] = (),
) -> set[str]:
    """Paths a command line names, with relative ones made absolute.

    Absolute tokens are taken as they are. A relative token holding a `/`
    (`../checkout/run.py`, `.venv/bin/tool`) is joined to the process's cwd.
    Either is kept raw, normalized and resolved (`_path_forms`), so a
    `<link>/../run.py` counts where the kernel finds it.
    The program it runs, found past leading wrappers (`env X=1 tool`, `sudo
    tool`, `xargs tool`; `_unwrap`), is resolved against the process's own
    absolute PATH entries when it is a bare name, or against the hub's PATH
    when the process's PATH could not be read (a process with other
    credentials, or `ps` without its environment); so is argv[0] itself, in
    case a checkout ships a program named like a wrapper. A `sh -c '<cmd>'`
    or `env -S` string is split, and each command in it, past shell keywords,
    treated the same way, `_MAX_SHELL_DEPTH` levels deep, as is the command
    `find -exec` runs (task #2192). Flags, URLs and `NAME=value` tokens are
    not joined. Over-matching only keeps a checkout, never removes one.
    `which_cache` memoizes PATH lookups for one scan.

    A `PATH=` a wrapper assigns (`env PATH=<wt>/bin tool`, `sudo PATH=...`)
    or that an enclosing `-c` command set (`_path_override`) is searched as
    well as `path_env`, never instead of it, and carries into the `-c`
    strings and `find -exec` commands of the program it runs (task #2199).
    Its relative entries are resolved against `cwd`, and it is bounded as
    `_extend_override` describes (task #2209).
    """
    if path_env is None:
        path_env = os.environ.get("PATH")
    if which_cache is None:
        which_cache = {}
    paths: set[str] = set()
    for arg in argv:
        if arg.startswith("/"):
            paths.update(_path_forms(arg))
        elif (
            cwd
            and "/" in arg
            and not arg.startswith("-")
            and "://" not in arg
            and not _ENV_ASSIGNMENT_RE.match(arg)
        ):
            paths.update(_path_forms(os.path.join(cwd, arg)))
    start, strings, assigned = _unwrap(argv)
    override = _extend_override(_path_override, assigned, cwd, paths)
    for index in {0, start}:
        if index < len(argv):
            paths.update(_lookups(argv[index], path_env, override, which_cache))
    if _depth >= _MAX_SHELL_DEPTH:
        return paths
    program = argv[start:]
    command = _shell_command(program)
    if command is not None:
        strings.append(command)
    for string in strings:
        paths |= _command_string_paths(string, cwd, path_env, which_cache, _depth + 1, override)
    for sub in _find_exec_commands(program):
        paths |= _argv_paths(sub, cwd, path_env, which_cache, _depth + 1, override)
    return paths


def _readlink(path: Path) -> str:
    return os.readlink(path)


# /proc/<pid>/status lines that decide whether this process may read another
# process's cwd, exe and environ (ptrace read access).
_CRED_FIELDS = ("Uid", "Gid", "Groups", "CapPrm")


def _proc_creds(status: Path) -> dict[str, str] | None:
    try:
        text = status.read_text()
    except OSError:
        return None
    creds = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name in _CRED_FIELDS:
            creds[name] = " ".join(value.split())
    return creds


# Why the most recent `_live_process_paths()` returned None, for the cleanup
# note and the doctor row; None after a scan that succeeded. Diagnostic only.
_last_unknown_cause: str | None = None

_GENERIC_UNKNOWN_CAUSE = (
    "no /proc and no working lsof, or a process with the hub's own credentials refused a read"
)
_DENIED_SHOWN = 3


def _proc_comm(entry: Path) -> str:
    """A process's short name from /proc/<pid>/comm, bounded and printable."""
    try:
        raw = (entry / "comm").read_bytes()[:64]
    except OSError:
        return "?"
    name = "".join(c if c.isalnum() or c in "._:-" else "?" for c in os.fsdecode(raw).strip())
    return name[:32] or "?"


def _denied_cause(denied: list[str]) -> str:
    shown = ", ".join(denied[:_DENIED_SHOWN])
    more = f" and {len(denied) - _DENIED_SHOWN} more" if len(denied) > _DENIED_SHOWN else ""
    return f"a process with the hub's own credentials refused a read: {shown}{more}"


def _proc_process_paths(proc: Path) -> frozenset[str] | None:
    """`_scan_proc(proc)` without the reason."""
    return _scan_proc(proc)[0]


@_with_scan_resolver
def _scan_proc(proc: Path) -> tuple[frozenset[str] | None, str | None]:
    """Each process's cwd, executable, command-line paths and code-path env, from /proc.

    Returns None (unknown) when /proc cannot be listed, or when a process with
    this process's own credentials refuses a read (task #2178). Such a refusal
    means something (an LSM policy, `hidepid`) hides processes the scan should
    see, so any of them could be running from a checkout. A process owned by
    another user, or one of ours holding extra credentials (a setgid helper,
    a service manager with capabilities, a PAM helper), is always refused; it
    is skipped as before, and its world-readable command line is still used.
    A process that exited mid-scan (ENOENT, ESRCH) is skipped.

    Same credentials means equal Uid, Gid, Groups and CapPrm lines. The
    kernel's read check ignores supplementary groups, so comparing them is
    stricter than it has to be, on purpose (task #2192). Setting groups takes
    CAP_SETGID, and a process that drops that privilege without an exec is
    non-dumpable and refuses every read: systemd's `(sd-pam)` helper does it
    on every user session. Counting such a process, or a same-uid one with
    another Gid (`newgrp`, a setgid helper) or extra capabilities (`systemd
    --user`), as same-credential would leave liveness unknown for as long as
    it runs, which on a desktop is always. So they are skipped: their cwd,
    executable and environment go unseen, while their command line is still
    read, with a bare program resolved against the hub's PATH. Removal still
    needs the worktree to be clean and merged. A refusing same-credential
    process (a daemon that cleared its dumpable flag, say) is named in the
    returned reason, "pid <n> (<comm>)" for the first few, so an operator
    sees which one blocks cleanup. Returns (paths, reason); reason is None
    when paths is not None. A path a process names that cannot be resolved
    within the kernel's limits (`_Resolver`) also makes liveness unknown,
    naming that process the same way (review of task #2209).
    """
    try:
        # Numeric pid order, so the named refusers are the same on every scan
        # and every filesystem (directory order is not specified).
        entries = sorted((e for e in proc.iterdir() if e.name.isdigit()), key=lambda e: int(e.name))
    except OSError:
        return None, f"{proc} could not be listed"
    own = _proc_creds(proc / "self" / "status")
    paths: set[str] = set()
    denied_same: list[str] = []
    unresolved: list[str] = []
    which_cache: dict[tuple[str, str], str | None] = {}
    seen_paths: set[str] = set()
    for entry in entries:
        try:
            if _scan_proc_entry(entry, own, paths, which_cache, seen_paths):
                denied_same.append(f"pid {entry.name} ({_proc_comm(entry)})")
        except _Unresolvable as exc:
            unresolved.append(f"pid {entry.name} ({_proc_comm(entry)}): {exc.reason}")
    if denied_same or unresolved:
        causes = [_denied_cause(denied_same)] if denied_same else []
        if unresolved:
            causes.append(_unresolved_cause(unresolved))
        return None, "; ".join(causes)
    return frozenset(paths), None


def _unresolved_cause(unresolved: list[str]) -> str:
    shown = ", ".join(unresolved[:_DENIED_SHOWN])
    more = f" and {len(unresolved) - _DENIED_SHOWN} more" if len(unresolved) > _DENIED_SHOWN else ""
    return f"a path a process names could not be resolved: {shown}{more}"


def _scan_proc_entry(
    entry: Path,
    own: Any,
    paths: set[str],
    which_cache: dict[tuple[str, str], str | None],
    seen_paths: set[str],
) -> bool:
    """Add one /proc entry's paths to `paths`; True when a same-credential read was refused."""
    theirs = _proc_creds(entry / "status")
    same_creds = theirs is None or own is None or theirs == own
    resolver = _SCAN_RESOLVER.get()
    if resolver is not None:
        # A denied path component is unknown only for the hub's own credentials.
        resolver.strict = same_creds
    denied = False
    cwd = None
    try:
        cwd = _readlink(entry / "cwd")
        paths.add(cwd)
    except PermissionError:
        denied = True
    except OSError:
        pass
    try:
        paths.add(_readlink(entry / "exe"))
    except PermissionError:
        denied = True
    except OSError:
        pass
    env: list[str] = []
    try:
        raw = (entry / "environ").read_bytes()
    except PermissionError:
        denied = True
    except OSError:
        pass
    else:
        env = [os.fsdecode(v) for v in raw.split(b"\0") if v]
        paths.update(_env_code_paths(env, cwd))
    if denied and theirs is not None and same_creds:
        return True
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return False
    argv = [os.fsdecode(arg) for arg in raw.split(b"\0") if arg]
    path_env = _env_value(env, "PATH")
    # PATH directories past the lookup cap are kept as paths (task #2209).
    paths.update(_path_overflow(path_env or os.environ.get("PATH"), seen_paths))
    paths.update(_argv_paths(argv, cwd, path_env, which_cache))
    return False


@_with_scan_resolver
def _ps_process_paths(cwds: dict[str, str] | None = None) -> set[str]:
    """Command-line paths and code-path env of running processes, from `ps`.

    `lsof -d cwd,txt` cannot see a script argument or PYTHONPATH (Python reads
    the script and closes it), so this covers them where /proc is absent. `-E`
    appends the environment for the caller's own processes, which supplies
    PATH for a bare argv[0]; `cwds` (pid -> cwd, from lsof) anchors relative
    arguments. Best effort: an empty set when `ps` cannot run, since lsof
    remains the authority. A path a process names that cannot be resolved
    raises `_Unresolvable` naming its pid, which `_live_process_paths` turns
    into unknown liveness (review of task #2209).

    Each row carries the process's effective uid next to its command line,
    read in the same `ps` call, so the two describe one process: a second
    read could find the pid reused by another user's process (review round
    5). Ownership is never inferred from the `-E` environment, since a
    process of ours can have an empty one or one whose last value holds a
    space (round 4). A row whose uid is not a number counts as ours (strict).
    """
    try:
        res = subprocess.run(
            ["ps", "-axwwE", "-o", "pid=,uid=,args="],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    cwds = cwds or {}
    own_uid = str(os.geteuid())
    paths: set[str] = set()
    which_cache: dict[tuple[str, str], str | None] = {}
    tail_cache: dict[tuple[str, str | None, str | None, bool], frozenset[str]] = {}
    seen_paths: set[str] = set()
    for line in res.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        pid, uid, rest = fields
        tokens = rest.split()
        if not pid.isdigit() or not tokens:
            continue
        resolver = _SCAN_RESOLVER.get()
        if resolver is not None:
            # A denied path component is unknown unless this row's uid is
            # another user's; an unreadable uid counts as ours.
            resolver.strict = not uid.isdigit() or uid == own_uid
        try:
            _ps_line_paths(tokens, cwds.get(pid), paths, which_cache, tail_cache, seen_paths)
        except _Unresolvable as exc:
            raise exc.at(f"pid {pid} ({os.path.basename(tokens[0])[:40]})") from None
    return paths


def _ps_line_paths(
    tokens: list[str],
    cwd: str | None,
    paths: set[str],
    which_cache: dict[tuple[str, str], str | None],
    tail_cache: dict[tuple[str, str | None, str | None, bool], frozenset[str]],
    seen_paths: set[str],
) -> None:
    """Add the paths of one `ps` line (pid and uid already split off) to `paths`."""
    # The `-E` environment ends the line as `NAME=value` words, and a value
    # such as LS_COLORS splits on its `;` into hundreds of pseudo-commands.
    # Each of those words is read on its own, once per scan, rather than
    # as part of every line (task #2199). Read alone, `X=1;tool` (a `-c`
    # string's last word) still yields `tool`. A word's cwd matters only
    # when it holds a `/` or assigns PATH, whose relative entries (`PATH=bin`)
    # are joined to it (task #2226).
    tail_from = len(tokens)
    while tail_from > 1 and _ENV_ASSIGNMENT_RE.match(tokens[tail_from - 1]):
        tail_from -= 1
    paths.update(_env_code_paths(tokens, cwd))
    path_env = _env_value(tokens, "PATH")
    paths.update(_argv_paths(tokens, cwd, path_env, which_cache))
    # `ps` joins argv with spaces and no quoting, so a `sh -c '<cmd>'`
    # string arrives in pieces: read the line as a command string too,
    # so a bare program inside it is still looked up (task #2192).
    lookup_path = path_env or os.environ.get("PATH")
    # PATH directories past the lookup cap are kept as paths (task #2209).
    paths.update(_path_overflow(lookup_path, seen_paths))
    paths.update(
        _command_string_paths(" ".join(tokens[:tail_from]), cwd, lookup_path, which_cache, 0)
    )
    # The result also depends on the row's strictness: a word read for
    # another user's process ends quietly at a denied component, which for
    # ours is unknown, so the two are cached apart (review round 5).
    resolver = _SCAN_RESOLVER.get()
    strict = resolver is None or resolver.strict
    for word in tokens[tail_from:]:
        anchored = "/" in word or _PATH_ASSIGNMENT_RE.search(word) is not None
        key = (word, cwd if anchored else None, lookup_path, strict)
        found = tail_cache.get(key)
        if found is None:
            found = tail_cache[key] = frozenset(
                _command_string_paths(word, key[1], lookup_path, which_cache, 0)
            )
        paths.update(found)


def _lsof_process_paths() -> frozenset[str] | None:
    """Each process's cwd and mapped program files, from lsof. None if it cannot run."""
    try:
        res = subprocess.run(
            ["lsof", "-n", "-P", "-w", "-d", "cwd,txt", "-Fpfn"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    paths: set[str] = set()
    cwds: dict[str, str] = {}
    pid = fd = ""
    for line in res.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, fd = value, ""
        elif tag == "f":
            fd = value
        elif tag == "n" and value.startswith("/"):
            paths.add(value)
            if fd == "cwd" and pid:
                cwds[pid] = value
    if res.returncode != 0 and not paths:
        return None
    return frozenset(paths | _ps_process_paths(cwds))


def _live_process_paths() -> frozenset[str] | None:
    """Paths running processes work in or execute from; None when unknown.

    Linux reads /proc (cwd, executable, command-line paths, which is where a
    venv entry point such as `<checkout>/.venv/bin/unified-agent` shows, and
    PYTHONPATH / VIRTUAL_ENV / PATH for the caller's own processes).
    Elsewhere `lsof` lists each process's cwd and mapped program files, and
    `ps` adds command-line paths and the same environment. None means
    liveness cannot be judged on this host, and nothing is removed; the
    reason is left in `_last_unknown_cause`.

    The fail-safe for a refused read exists only on the /proc path. A partial
    lsof (a process whose cwd it could not inspect) still reads as known: lsof
    does not say which processes it skipped, and its exit status and warnings
    also cover other users' processes, which it can never inspect. Treating
    them as unknown has not been checked against the hub's own lsof, and if it
    fires routinely there, cleanup would remove nothing, ever (task #2192).
    """
    global _last_unknown_cause
    proc = Path("/proc")
    try:
        has_proc = (proc / "self" / "cwd").exists()
    except OSError:
        has_proc = False
    try:
        if has_proc:
            paths, cause = _scan_proc(proc)
        else:
            paths = _lsof_process_paths()
            cause = None if paths is not None else "no /proc, and lsof could not run"
    except _Unresolvable as exc:
        # A path a process names could not be resolved within the kernel's
        # limits: unknown, so cleanup removes nothing (review of task #2209).
        paths, cause = None, f"a path a process names could not be resolved: {exc}"
    _last_unknown_cause = cause
    return paths


def _liveness_unknown_note(cause: str | None = None) -> str:
    cause = cause or _last_unknown_cause or _GENERIC_UNKNOWN_CAUSE
    return (
        f"running processes could not be listed on this host ({cause}), so cleanup "
        "removes nothing until liveness can be judged"
    )


def _in_use(item: Path, live_paths: frozenset[str] | None) -> bool | None:
    """True if a running process works in or runs code from `item`; None if unknown.

    Unknown must be treated as in use: the caller removes with --force, so a
    check that could not run must keep the checkout rather than call it idle.
    """
    if live_paths is None:
        return None
    roots = {str(item.absolute())}
    try:
        roots.add(str(item.resolve()))
    except OSError:
        pass
    return any(p == root or p.startswith(root + os.sep) for root in roots for p in live_paths)


def _git_dir(item: Path) -> Path | None:
    """The gitdir of a worktree root: `.git` itself, or where a `.git` file points."""
    dotgit = item / ".git"
    if dotgit.is_dir():
        return dotgit
    try:
        text = dotgit.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            target = Path(line.removeprefix("gitdir:").strip())
            return target if target.is_absolute() else item / target
    return None


# Per-worktree files in the gitdir that git rewrites when the worktree is used.
_ACTIVITY_FILES = ("index", "HEAD", "logs/HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD")


def _last_activity(item: Path) -> datetime:
    """Newest mtime of the worktree root and its gitdir's index, HEAD and friends.

    The root's own mtime moves only when an entry directly inside it is added,
    removed or renamed; staging, committing, checking out, fetching and
    merging move the index, HEAD, its reflog or the FETCH/ORIG/MERGE heads
    instead (task #2177). Read these before running any git command on the
    worktree: `git status` may refresh the index. A root that cannot be
    stat'ed (removed mid-scan) reads as active now, so it is never idle.
    """
    try:
        newest = item.stat().st_mtime
    except OSError:
        return datetime.now(UTC)
    gitdir = _git_dir(item)
    if gitdir is not None:
        for name in _ACTIVITY_FILES:
            meta = gitdir / name
            try:
                newest = max(newest, meta.stat().st_mtime)
            except OSError:
                pass
    return datetime.fromtimestamp(newest, tz=UTC)


def _worktree_staleness(
    item: Path,
    task_map: dict[int, Any],
    cutoff_dt: datetime,
    live_paths: frozenset[str] | None = _UNSET,
) -> tuple[int | None, bool, str | None]:
    """Is `item` stale by task-status/idle-time policy? Independent of disposability.

    Staleness ("this checkout's work is done, nothing should still be using it")
    and disposability ("removing this destroys nothing") answer different
    questions — a task that just entered `doing` has a fresh, non-stale
    worktree that still equals the base branch and so reads as disposable; a
    genuinely stale worktree can hold uncommitted or unmerged work and so reads
    as not disposable. Conflating the two hides the second case and
    misreports the first (see task #445 / PR #193 review).

    Idleness is the newest of the root, index, HEAD and HEAD-reflog mtimes
    (`_last_activity`, task #2177). A deploy checkout (`release-*`,
    `rollback-*`) is never stale (task #2174), and neither is the target of a
    node release link (`release/current`, `release/previous`, task #2298),
    whatever its name. Neither is an idle worktree a
    running process works in or runs code from: live use contradicts idleness,
    which file times cannot see. A worktree whose task is done or blocked, or
    whose task is gone, is still reported stale while in use, so a parked
    shell does not hide it (task #2178); callers mark it in use and never
    remove it. `live_paths` is `_live_process_paths()`, passed in so a scan of
    many worktrees lists processes once; when it is None (unknown) an idle
    worktree reads "liveness_unknown" rather than stale.

    Returns (task_id, stale, reason). `reason` is one of "task_done",
    "task_blocked", "idle_timeout", "orphaned_task" when stale, and
    "deploy_checkout", "in_use", "liveness_unknown", "active_claim" or None
    when not.
    """
    task_id = _task_id_from_folder(item.name)
    if _is_deploy_checkout(item.name) or _is_release_link_target(item):
        return task_id, False, "deploy_checkout"
    if live_paths is _UNSET:
        live_paths = _live_process_paths()
    in_use = _in_use(item, live_paths)
    is_idle = _last_activity(item) < cutoff_dt
    if is_idle and in_use is not False:
        # Only idleness would make this stale, and a live (or possibly live)
        # process contradicts it. Task-status verdicts below still apply.
        is_idle = False
        live_reason: str | None = "in_use" if in_use else "liveness_unknown"
    else:
        live_reason = None

    if task_id is not None and task_id in task_map:
        task = dict(task_map[task_id])
        try:
            lease = datetime.fromisoformat(str(task.get("lock_expires_at") or ""))
            if lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
        except ValueError:
            lease = None
        if task.get("claimed_by") and lease is not None and lease > datetime.now(UTC):
            return task_id, False, "active_claim"
        t_status = task["status"]
        if t_status in ("done", "blocked"):
            return task_id, True, f"task_{t_status}"
        if is_idle:
            return task_id, True, "idle_timeout"
        return task_id, False, live_reason
    if task_id is not None and task_id not in task_map:
        return task_id, True, "orphaned_task"
    if is_idle:
        return task_id, True, "idle_timeout"
    return task_id, False, live_reason


def _load_task_status_map() -> dict[int, Any]:
    conn = storage.connect()
    try:
        cur = conn.execute("SELECT id, status, updated_at, claimed_by, lock_expires_at FROM tasks")
        return {row["id"]: dict(row) for row in cur.fetchall()}
    finally:
        conn.close()


def _git(path: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """Run git inside `path`. Returns None when git could not be run at all."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        # A survey must not write the worktree's index: `git status` refreshes
        # it opportunistically, which would also make the worktree look active
        # to `_last_activity` (task #2177).
        "GIT_OPTIONAL_LOCKS": "0",
    }
    # A ceiling the caller set still holds: without it, a directory that is
    # not a repository reads as whatever repository encloses it (task #2280).
    ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
    if ceiling:
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    try:
        return subprocess.run(
            ["git", *args], cwd=path, env=env, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _base_contains_head(path: Path, base: str) -> tuple[bool, str] | None:
    """Would merging HEAD into `base` change `base`? None when unanswerable.

    Returns (True, "") when the merge result is byte-identical to `base`'s
    current tree — HEAD contributes nothing, so nothing is lost by removing it.
    That covers a squash-merged branch and a branch the base has advanced past,
    which the ancestry and two-dot tests respectively get wrong.

    Returns None rather than a verdict when `merge-tree --write-tree` is
    unavailable (git < 2.38) or the merge cannot be evaluated, so the caller
    falls back instead of treating "don't know" as "safe to delete".
    """
    merged = _git(path, ["merge-tree", "--write-tree", base, "HEAD"])
    if merged is None or merged.returncode not in (0, 1):
        # 128/129 here is an old git rejecting --write-tree, or a bad ref.
        return None
    if merged.returncode == 1:
        # Real divergence: the branch and the base both changed the same lines.
        return False, "conflicts_with_base"

    lines = merged.stdout.strip().splitlines()
    if not lines or not lines[0].strip():
        return None
    base_tree = _git(path, ["rev-parse", f"{base}^{{tree}}"])
    if base_tree is None or base_tree.returncode != 0:
        return None
    if lines[0].strip() == base_tree.stdout.strip():
        return True, ""
    return False, "unmerged_content"


def _worktree_is_disposable(path: Path) -> tuple[bool, str]:
    """(True, "") if removing `path` destroys nothing; (False, reason) otherwise.

    Two questions, in order: is there uncommitted work, and would the committed
    work still be there if this directory went away?

    The second is answered by *containment* — would merging HEAD into the base
    branch change the base branch? — rather than by ancestry or by a two-dot
    diff. Both of the simpler tests have already produced false "unmerged"
    verdicts here, in opposite directions:

    - Ancestry (`git merge-base --is-ancestor`) reads a squash-merged branch as
      unmerged forever, because squashing rewrites the commits (memory
      `85b18db80389`).
    - `git diff base..HEAD` — the fix for the above — is symmetric: it compares
      two tips and reports a difference whether the branch is ahead OR behind.
      Once the base branch advances past a merged worktree, which it always
      does, that worktree reads `unmerged_content` permanently and is retained
      forever. Verified live 2026-08-18 on `codex/fix-webui-spec-findings` and
      `codex/reconcile-agy-mobile`: both merged (PRs #189, #190), both reported
      unmerged, main strictly contained both.

    Containment answers the question actually being asked, and gets both cases
    right: a squash-merged branch contributes nothing to the merge result, and
    so does one the base has simply moved past.

    Anything this cannot establish — git missing, no base branch, a git too old
    for `merge-tree --write-tree`, a directory that only looks like a worktree —
    falls back to the two-dot check and otherwise returns not-disposable.
    Removal here runs with `--force`, which discards commits and uncommitted
    files without a prompt, so uncertainty must resolve toward keeping the
    directory.
    """
    status = _git(path, ["--no-optional-locks", "status", "--porcelain"])
    if status is None or status.returncode != 0:
        return False, "git_status_unavailable"
    if status.stdout.strip():
        return False, "uncommitted_changes"

    base = None
    for candidate in ("main", "master"):
        res = _git(path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"])
        if res is not None and res.returncode == 0:
            base = candidate
            break
    if base is None:
        return False, "no_base_branch"

    contained = _base_contains_head(path, base)
    if contained is not None:
        return contained

    # Fallback: git predates `merge-tree --write-tree` (2.38), or the merge
    # could not be evaluated. This is the symmetric check described above, kept
    # only because its failure mode is to retain a directory rather than remove
    # one — wrong in the safe direction.
    diff = _git(path, ["diff", "--quiet", f"{base}..HEAD"])
    if diff is None or diff.returncode not in (0, 1):
        return False, "git_diff_unavailable"
    if diff.returncode == 1:
        return False, "unmerged_content"

    return True, ""


def cleanup_stale_worktrees(
    max_idle_days: float = 7.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan and prune stale or abandoned task worktrees.

    Evaluates worktrees in `.worktrees/` against task status (done/blocked) and
    filesystem modification time (idle > max_idle_days).

    Args:
        max_idle_days: Threshold in days for an idle worktree (default 7.0).
        dry_run: If True, returns candidates without removing them (default False).

    Returns:
        {
          "cleaned_count": int,
          "stale_worktrees": list[dict],   # candidates, nested ones first
          "skipped": list[dict],           # dirs that are not git worktrees
          "retained": list[dict],          # stale, but hold work — not removed
          "failed": list[dict],            # removals git refused, left on disk
          "dry_run": bool,
          "liveness_unknown": bool,        # either process scan (initial or the
                                           # pre-removal recheck) could not list
          "note": str,                     # present when liveness_unknown, naming
                                           # the first unknown scan's cause
        }

    A candidate appearing in `retained` is the normal outcome for a worktree
    with uncommitted or unmerged work: it is stale by policy but removing it
    would destroy something, so it is surfaced instead. Nothing here deletes
    unmerged work.

    A stale candidate a running process uses carries `in_use: True` and is
    retained as `in_use`, never removed (task #2178). When running processes
    cannot be listed on this host (no /proc and no working lsof, or an
    unreadable process with the hub's own credentials), every candidate is
    retained as `liveness_unknown` and the result says so rather than
    silently removing nothing.
    """
    # Known product repositories are the same trusted roots task_claim uses.
    # Arbitrary audit roots are never removal roots. workspace_root still
    # enforces the caller's existing DENY posture before any survey or removal.
    ws_root = config.workspace_root().resolve()
    roots = list(dict.fromkeys(p.resolve() for p in [ws_root, *config.project_repo_roots()]))

    now = datetime.now(UTC)
    cutoff_dt = now - timedelta(days=max_idle_days)

    # Query active task statuses
    task_map = _load_task_status_map()
    live_paths = _live_process_paths()
    # Each scan overwrites `_last_unknown_cause`, so the cause behind the
    # first unknown scan (initial or recheck) is captured as it happens.
    unknown_cause: str | None = (
        (_last_unknown_cause or _GENERIC_UNKNOWN_CAUSE) if live_paths is None else None
    )
    liveness_unknown = live_paths is None

    stale_candidates = []
    skipped: list[dict[str, str]] = []
    seen = set()
    for repo_root in roots:
        for item in _iter_worktree_dirs(repo_root / ".worktrees"):
            if item in seen:
                continue
            seen.add(item)
            path = str(item.relative_to(ws_root)) if repo_root == ws_root else str(item)
            if not _is_git_worktree(item):
                skipped.append({"path": path, "reason": "not_a_git_worktree"})
                continue

            task_id, is_stale, reason = _worktree_staleness(item, task_map, cutoff_dt, live_paths)

            if is_stale:
                candidate = {
                    "path": path,
                    "task_id": task_id,
                    "reason": reason,
                    "last_modified": _last_activity(item).isoformat(),
                    "in_use": _in_use(item, live_paths),
                }
                if repo_root != ws_root:
                    candidate["repo_path"] = str(repo_root)
                stale_candidates.append(candidate)

    # Children before parents: removing a parent takes a worktree nested inside
    # it down with it, so a nested candidate must be dealt with first.
    stale_candidates.sort(key=lambda c: c["path"].count("/"), reverse=True)

    cleaned_count = 0
    failed: list[dict[str, str]] = []
    retained: list[dict[str, Any]] = []
    if not dry_run and stale_candidates:
        for cand in stale_candidates:
            rel_path = cand["path"]
            worktree_path = Path(rel_path)
            worktree_path = (
                worktree_path if worktree_path.is_absolute() else ws_root / worktree_path
            )
            # The process snapshot above predates this candidate's git checks;
            # a process that started using the checkout since then must keep it.
            # In use in either snapshot, or unknown in either, keeps it.
            now_in_use = _in_use(worktree_path, _live_process_paths())
            if now_in_use is None and not liveness_unknown:
                liveness_unknown = True
                unknown_cause = _last_unknown_cause or _GENERIC_UNKNOWN_CAUSE
            if cand["in_use"] is None or now_in_use is None:
                retained.append({**cand, "retained_because": "liveness_unknown"})
                continue
            if cand["in_use"] or now_in_use:
                retained.append({**cand, "retained_because": "in_use"})
                continue
            disposable, why = _worktree_is_disposable(worktree_path)
            if not disposable:
                # Reported, not removed. Removal below is forced, and this is
                # the only thing standing between "stale" and "your unpushed
                # work is gone" — orphaned_task worktrees in particular are
                # produced by ordinary task deletion, so real work reaches here.
                retained.append({**cand, "retained_because": why})
                continue
            try:
                if "repo_path" in cand:
                    from .tasks import _git_worktree_remove_from_known_project_repo

                    _git_worktree_remove_from_known_project_repo(
                        worktree_path, Path(cand["repo_path"]), force=True
                    )
                else:
                    git_worktree_remove(path=rel_path, force=True)
                cleaned_count += 1
            except Exception as e:
                # Report and move on. There used to be a shutil.rmtree fallback
                # here, which recursively deleted whatever git had just refused
                # to remove — the failure case is exactly when least is known
                # about the directory, so it is the worst moment to force it.
                failed.append({"path": rel_path, "error": str(e)})

    result: dict[str, Any] = {
        "cleaned_count": cleaned_count if not dry_run else 0,
        "stale_worktrees": stale_candidates,
        "skipped": skipped,
        "retained": retained,
        "failed": failed,
        "dry_run": dry_run,
        "liveness_unknown": liveness_unknown,
    }
    if liveness_unknown:
        result["note"] = _liveness_unknown_note(unknown_cause)
    return result


def survey_worktrees(
    extra_roots: list[Path] | None = None, max_idle_days: float = 7.0
) -> list[dict[str, Any]]:
    """Read-only census of worktrees under the workspace and each extra root.

    Includes configured project repositories by default, because task_claim
    creates task worktrees there. Explicit extra_roots can restrict the extra
    survey to the caller's selected set.

    The survey reads `config.process_workspace_root()` rather than
    `workspace_root()`, so a Control Center login whose identity is
    `UA_AGENT_WORKSPACES` DENY (the configured operator label) still sees hub worktrees
    instead of aborting the whole Executor & Delegations pane.

    Each entry carries two independent verdicts: `disposable` (removing this
    destroys nothing — clean and merged) and `stale` (task status/idle-time
    policy, the same test `cleanup_stale_worktrees` applies). A worktree can be
    stale without being disposable (idle, but holding uncommitted or unmerged
    work — the case that most needs a human) or disposable without being stale
    (a task that just started, whose clean checkout still equals the base
    branch). Callers that want "should this be flagged for attention" want
    `stale`; `disposable` is only about removal safety. `in_use` is True when
    a running process works in or runs code from the worktree, False when
    none does, and None when running processes cannot be listed on this host
    (then idle worktrees read `stale_reason: "liveness_unknown"` and every
    entry carries `liveness_note`, naming the cause). A stale worktree in use
    is reported, and is never removed.

    This SURVEYS and never removes. cleanup_stale_worktrees removes only from
    the admitted workspace or known project repos, using the owning repo's
    worktree helper and unchanged disposability checks. Arbitrary audit roots
    extend this read-only census and never extend removal reach.
    """
    hub_root = config.process_workspace_root()
    roots: list[Path] = [hub_root]
    roots.extend(
        extra_roots
        if extra_roots is not None
        else [*config.audit_roots(), *config.project_repo_roots()]
    )

    task_map = _load_task_status_map()
    cutoff_dt = datetime.now(UTC) - timedelta(days=max_idle_days)
    live_paths = _live_process_paths()
    liveness_note = _liveness_unknown_note() if live_paths is None else None

    seen: set[Path] = set()
    found: list[dict[str, Any]] = []
    for root in roots:
        try:
            resolved_root = Path(root).resolve()
        except OSError:
            continue
        base = resolved_root / ".worktrees"
        if not base.is_dir():
            continue
        for item in _iter_worktree_dirs(base):
            try:
                resolved = item.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not _is_git_worktree(item):
                continue
            # Staleness first: it reads index and HEAD mtimes, which the git
            # commands behind disposability must not get a chance to touch.
            task_id, stale, stale_reason = _worktree_staleness(
                item, task_map, cutoff_dt, live_paths
            )
            disposable, reason = _worktree_is_disposable(item)
            found.append(
                {
                    "path": str(item),
                    "root": str(resolved_root),
                    "in_workspace": resolved_root == hub_root.resolve(),
                    "disposable": disposable,
                    "reason": reason,
                    "task_id": task_id,
                    "stale": stale,
                    "stale_reason": stale_reason,
                    "in_use": _in_use(item, live_paths),
                    **({"liveness_note": liveness_note} if liveness_note else {}),
                }
            )
    return found


# One shared survey (task #1897). Each surveyed worktree costs four or five git
# processes, one of them a `merge-tree --write-tree`, and on the hub the doctor
# check, the doctor's host snapshot, and the attention and executor panes each
# ran a full survey on their own poll. Holds (monotonic finish, roots, result).
_survey_cache: tuple[float, tuple[str, ...], list[dict[str, Any]]] | None = None
# Held across a survey on purpose: a caller that arrives meanwhile waits and
# reads the result instead of starting a second survey (single flight).
_survey_lock = threading.Lock()


def _survey_roots() -> tuple[str, ...]:
    return tuple(
        str(p)
        for p in [
            config.process_workspace_root(),
            *config.audit_roots(),
            *config.project_repo_roots(),
        ]
    )


def clear_survey_cache() -> None:
    """Forget the shared survey so the next call re-runs git (tests)."""
    global _survey_cache
    with _survey_lock:
        _survey_cache = None


def cached_survey_worktrees(
    survey: Callable[[], list[dict[str, Any]]] | None = None,
    *,
    force: bool = False,
) -> tuple[list[dict[str, Any]], float]:
    """`survey_worktrees()` shared for ``UA_WORKTREE_SURVEY_CACHE_S`` seconds.

    Returns (worktrees, age_s): the default-root survey and how many seconds
    ago it ran (0.0 for one run by this call). ``survey`` replaces the survey
    function (the web panes pass their module binding so tests can stub it).
    ``force`` surveys now and replaces the shared result, for a
    ``system_doctor(refresh=True)`` run. A survey that raises is not cached.
    The roots are part of the key, so a changed ``UA_AUDIT_ROOTS`` is never
    answered from the old survey.
    """
    global _survey_cache
    ttl = config.worktree_survey_cache_s()
    run = survey or survey_worktrees
    if ttl <= 0:
        return run(), 0.0
    roots = _survey_roots()
    with _survey_lock:
        entry = _survey_cache
        now = time.monotonic()
        if not force and entry is not None and entry[1] == roots and now - entry[0] < ttl:
            return [dict(item) for item in entry[2]], round(now - entry[0], 1)
        found = run()
        _survey_cache = (time.monotonic(), roots, [dict(item) for item in found])
    return found, 0.0
