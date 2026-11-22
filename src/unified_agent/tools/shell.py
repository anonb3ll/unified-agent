"""Allowlisted shell execution inside the workspace sandbox, plus dry-run command vetting."""

import os
import re
import shlex
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..safety import (
    DESTRUCTIVE_COMMAND_PREFIX,
    get_active_worktree_path,
    guard_secret_filename,
    resolve_in_workspace,
    vet_command,
)

# Tokens that would be shell syntax in a terminal but reach the program as literal argv
# entries here (exact matches only, to avoid flagging patterns like grep '<div>').
_SHELL_SYNTAX_TOKENS = {
    "|",
    "||",
    "&&",
    ";",
    "&",
    ">",
    ">>",
    "<",
    "<<",
    "2>",
    "2>>",
    "2>&1",
    "1>&2",
}
_EXPANSION_RE = re.compile(r"\$[\w{(]|`")

_READ_ONLY_PROGRAMS = {
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "wc",
    "diff",
    "which",
    "echo",
    "date",
}
_FIND_SIDE_EFFECT_ACTIONS = {
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprint0",
    "-fprintf",
    "-fls",
}
_GIT_READ_ONLY = {
    "status",
    "log",
    "diff",
    "show",
    "blame",
    "grep",
    "ls-files",
    "ls-tree",
    "rev-parse",
    "describe",
    "shortlog",
    "reflog",
    "cat-file",
}
_GIT_MUTATING = {
    "add",
    "commit",
    "push",
    "fetch",
    "pull",
    "merge",
    "rebase",
    "reset",
    "restore",
    "checkout",
    "switch",
    "clean",
    "rm",
    "mv",
    "cherry-pick",
    "revert",
    "am",
    "apply",
    "clone",
    "init",
    "gc",
    "prune",
    "submodule",
}
_GIT_NETWORK = {"push", "fetch", "pull", "clone", "ls-remote"}
# Remote-mutating git subcommands force the approval gate (coordinator/operator
# amendment to #682 / PR #328). fetch/pull/clone/ls-remote stay warn-only so
# routine pulls (e.g. deploy-mac-hub) do not park approval rows.
_GIT_REMOTE_MUTATING = frozenset({"push"})
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
# Long-standing built-in and shipped git commands. git never lets an alias
# shadow one of these, so any other subcommand may be alias.<name>=!cmd from a
# config the agent wrote (a clone --config, or a bare repo laid out by fs_write).
_GIT_KNOWN_SUBCOMMANDS = frozenset(
    """
    add am annotate apply archive bisect blame branch bugreport bundle cat-file
    check-attr check-ignore check-mailmap check-ref-format checkout checkout-index
    cherry cherry-pick clean clone column commit commit-graph commit-tree config
    count-objects credential describe diagnose diff diff-files diff-index diff-tree
    difftool fast-export fast-import fetch fetch-pack filter-branch fmt-merge-msg
    for-each-ref for-each-repo format-patch fsck gc get-tar-commit-id grep
    hash-object help hook index-pack init interpret-trailers log ls-files ls-remote
    ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-tree
    mergetool mktag mktree multi-pack-index mv name-rev notes pack-objects
    pack-refs patch-id prune pull push range-diff read-tree rebase reflog remote
    repack replace request-pull rerere reset restore rev-list rev-parse revert rm
    shortlog show show-branch show-index show-ref sparse-checkout stash status
    stripspace submodule switch symbolic-ref tag unpack-file unpack-objects
    update-index update-ref var verify-commit verify-pack verify-tag version
    whatchanged worktree write-tree
    """.split()
)
# Global options that point git at a repository of the caller's choosing.
_GIT_REPO_SELECT_FLAGS = ("--git-dir", "--work-tree")
# Advisory codes that must hit the approval queue even when UA_APPROVAL_TOOLS
# does not list run_cmd (task #682). shell-syntax / no-expansion stay warn-only.
# "network" alone does not force — only when paired with _GIT_REMOTE_MUTATING.
_APPROVAL_REQUIRED_CODES = frozenset(
    {
        "path-escape",
        "allowlist-bypass",
        "arbitrary-code",
    }
)


def _git_subcommand(rest: list[str]) -> str:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _GIT_VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tok
    return ""


def _sed_in_place(rest: list[str]) -> bool:
    return any(
        a.startswith("--in-place")
        or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
        for a in rest
        if a.startswith("-")
    )


# python --version/-V/-h/--help print text and do not run a program. Any other
# python/python3 argv (script, -m, -c, REPL, or an extra argument) executes code.
_PYTHON_HARMLESS_ARGS = frozenset({"-h", "--help", "-V", "--version"})
# git -c keys that are safe to set for one call (task #1904). Any other key is
# treated as able to run a program: the exec-capable set is open-ended
# (include.path, core.hooksPath, diff.external, <driver>.textconv, gpg.program,
# credential.helper, ...), so an allowlist is the only sound check.
# Compared case-insensitively (the PoC is `git -c ALIAS.z=!id`).
_GIT_SAFE_CONFIG_KEYS = frozenset(
    {
        "user.name",
        "user.email",
        "core.quotepath",
        "core.abbrev",
        "core.autocrlf",
        "init.defaultbranch",
        "commit.gpgsign",
        "tag.gpgsign",
        "gc.auto",
        "pull.rebase",
        "pull.ff",
        "merge.ff",
        "push.default",
        "fetch.prune",
        "diff.noprefix",
        "diff.mnemonicprefix",
        "diff.renames",
        "log.decorate",
        "log.date",
        "status.short",
    }
)
_GIT_SAFE_CONFIG_PREFIXES = ("advice.", "color.")
# `git config` forms that only read. Anything else may write a key that a later
# git call executes (alias.x=!cmd, core.pager, core.hooksPath, include.path).
_GIT_CONFIG_READ_FLAGS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "-l",
        "--list",
    }
)
_GIT_CONFIG_READ_SUBCOMMANDS = frozenset({"get", "list"})
_GIT_CONFIG_WRITE_FLAGS = frozenset(
    {
        "--add",
        "--replace-all",
        "--unset",
        "--unset-all",
        "--rename-section",
        "--remove-section",
        "-e",
        "--edit",
    }
)
# git config options whose next token is a value, not a key.
_GIT_CONFIG_VALUE_FLAGS = frozenset(
    {"-f", "--file", "--blob", "-t", "--type", "--default", "--comment"}
)
# Programs that print file contents; their path operands get fs_read's secret-name guard.
_CONTENT_READERS = frozenset(
    {"cat", "head", "tail", "grep", "diff", "uniq", "sort", "sed", "awk", "wc"}
)
_AWK_PROGRAM_OPTIONS = frozenset({"-f", "--file", "-e", "--source", "-E", "--exec"})


def _program_name(prog: str) -> str:
    return Path(prog).name


def _python_executes_code(rest: list[str]) -> bool:
    """True unless every argument is a harmless version/help flag."""
    if not rest:
        return True
    return any(arg not in _PYTHON_HARMLESS_ARGS for arg in rest)


def _awk_can_run_code(rest: list[str]) -> bool:
    """True when awk is given program text, -f/-e, or an explicit system() call.

    A program argument can call system() even when that literal is not in argv
    (`awk -f script`, `awk '{...}'`). Version/help flags with no program do not.
    """
    if any("system(" in tok for tok in rest):
        return True
    i = 0
    n = len(rest)
    while i < n:
        tok = rest[i]
        if tok == "--":
            return i + 1 < n
        if tok in _AWK_PROGRAM_OPTIONS or tok.startswith(("--file=", "--source=")):
            return True
        if tok.startswith("-f") and len(tok) > 2:
            return True
        if tok.startswith("-e") and not tok.startswith("--") and len(tok) > 2:
            return True
        takes_value = tok in {"-F", "-v", "--field-separator", "--assign"}
        attached_short = tok.startswith(("-F", "-v")) and len(tok) > 2
        attached_long = tok.startswith(("--field-separator=", "--assign="))
        if takes_value or attached_short or attached_long:
            if attached_short or attached_long or "=" in tok:
                i += 1
            else:
                i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return True
    return False


def _git_config_key(value: str) -> str:
    return value.split("=", 1)[0].strip().casefold()


def _git_c_executes(value: str) -> bool:
    key = _git_config_key(value)
    return key not in _GIT_SAFE_CONFIG_KEYS and not key.startswith(_GIT_SAFE_CONFIG_PREFIXES)


def _git_exec_config_hits(rest: list[str]) -> list[str]:
    """argv tokens that make git run a program (alias, pager, --config-env, --exec-path)."""
    hits: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        tok = rest[i]
        if tok == "-c":
            if i + 1 < n and _git_c_executes(rest[i + 1]):
                hits.append(rest[i + 1])
            i += 2
            continue
        if tok.startswith("-c") and not tok.startswith("--") and len(tok) > 2:
            value = tok[2:]
            if _git_c_executes(value):
                hits.append(value)
            i += 1
            continue
        if tok == "--config-env" or tok.startswith("--config-env="):
            hits.append(tok)
            i += 2 if tok == "--config-env" else 1
            continue
        if tok == "--exec-path" or tok.startswith("--exec-path="):
            hits.append(tok)
            i += 1
            continue
        name, eq, value = tok.partition("=")
        if len(name) >= 3 and "--config".startswith(name):
            # clone/init --config (any unambiguous prefix) sets a key like -c does,
            # but persists it in the new repository's config.
            if not eq:
                value = rest[i + 1] if i + 1 < n else ""
            if _git_c_executes(value):
                hits.append(value)
            i += 1 if eq else 2
            continue
        i += 1
    n_global = len(rest) - len(_git_subcommand_args(rest)) - (1 if _git_subcommand(rest) else 0)
    hits.extend(
        tok
        for tok in rest[:n_global]
        if tok in _GIT_REPO_SELECT_FLAGS or tok.startswith(("--git-dir=", "--work-tree="))
    )
    return hits


def _git_subcommand_args(rest: list[str]) -> list[str]:
    """argv after the git subcommand (global options such as -C/-c skipped)."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _GIT_VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return rest[i + 1 :]
    return []


def _git_config_writes(args: list[str]) -> bool:
    """True unless a `git config` argv only reads (get/list, or a bare key)."""
    positionals: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in _GIT_CONFIG_READ_FLAGS:
            return False
        if tok in _GIT_CONFIG_WRITE_FLAGS:
            return True
        if tok in _GIT_CONFIG_VALUE_FLAGS:
            i += 2
            continue
        if not tok.startswith("-"):
            positionals.append(tok)
        i += 1
    if positionals and positionals[0] in _GIT_CONFIG_READ_SUBCOMMANDS:
        return False
    # A bare `git config <key>` reads; a key and a value (or `set`/`unset`/
    # `edit`/`rename-section`, ...) writes.
    return len(positionals) != 1 or positionals[0] in {"set", "unset", "edit"}


def _has_flag(args: list[str], *names: str, short: str | None = None) -> str | None:
    """First token naming one of ``names`` (bare or ``=value``) or ``short``/``short<v>``.

    GNU getopt_long and git's parse-options accept any unambiguous prefix of a long
    option (``--compress-prog`` for ``--compress-program``), so a token whose name is
    a prefix of a listed long option counts, including one-letter names (``--u``).
    """
    longs = [n for n in names if n.startswith("--")]
    for tok in args:
        if tok in names:
            return tok
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if len(name) >= 3 and any(n.startswith(name) for n in longs):
                return tok
            continue
        if short and tok.startswith(short):
            return tok
    return None


# git subcommands whose diff options include --output=<file> (a file write).
_GIT_OUTPUT_SUBCOMMANDS = frozenset(
    {"log", "show", "diff", "diff-tree", "diff-index", "diff-files", "format-patch", "whatchanged"}
)
# Short options whose attached value is a path (`git -C/etc`, `sort -o/tmp/x`).
# Checked per program: elsewhere an attached value is usually a pattern or script
# (`sed -e/x/d`, `grep -e/foo/`), not a path.
_ATTACHED_PATH_SHORT_OPTIONS = {"git": ("-C",), "sort": ("-o", "-T")}
# git options whose value is free text, never a path (`git commit -m .git/hooks/x`).
_GIT_TEXT_VALUE_OPTIONS = frozenset({"-m", "--message"})


def _attached_path_value(prog: str, tok: str) -> str | None:
    for opt in _ATTACHED_PATH_SHORT_OPTIONS.get(_program_name(prog), ()):
        if tok.startswith(opt) and len(tok) > len(opt):
            return tok[len(opt) :]
    return None


def _git_exec_subcommand_reason(rest: list[str]) -> str | None:
    """Why a git subcommand/flag runs a caller-supplied program, if it does."""
    sub = _git_subcommand(rest)
    args = _git_subcommand_args(rest)
    if sub and sub not in _GIT_KNOWN_SUBCOMMANDS:
        return (
            f"git {sub} is not a built-in git command; it may run alias.{sub}=!cmd from a "
            f"repository config or an external git-{sub} program"
        )
    if sub == "config" and _git_config_writes(args):
        return (
            "git config writes repository or global configuration; a key such as "
            "alias.<name>=!cmd, core.pager, core.hooksPath or include.path makes a later "
            "git call run an arbitrary program"
        )
    if sub in {"filter-branch", "difftool", "mergetool"}:
        return f"git {sub} runs external commands or configured tools"
    if sub == "grep":
        hit = _has_flag(args, "--open-files-in-pager", short="-O")
        if hit:
            return f"git grep {hit} runs the named pager program"
    if sub == "rebase":
        hit = _has_flag(args, "--exec", short="-x")
        if hit:
            return f"git rebase {hit} runs a command after each commit"
    if sub == "bisect" and args[:1] == ["run"]:
        return "git bisect run executes a command"
    if sub == "submodule" and "foreach" in args:
        return "git submodule foreach executes a command in every submodule"
    if sub in {"ls-remote", "fetch", "pull", "clone", "push", "archive"}:
        hit = _has_flag(args, "--upload-pack", "--receive-pack", "--exec", "--template")
        if hit is None and sub in {"ls-remote", "clone"}:
            # -u is --upload-pack here; on fetch/pull it is the harmless --update-head-ok.
            hit = _has_flag(args, short="-u")
        if hit:
            return f"git {sub} {hit} runs a supplied program or installs template hooks"
    return None


def _arg_path_values(prog: str, rest: list[str]) -> list[str]:
    """Candidate path strings in an argv: bare args, ``--opt=value`` and known ``-Xvalue``."""
    is_git = _program_name(prog) == "git"
    values: list[str] = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if is_git and tok in _GIT_TEXT_VALUE_OPTIONS:
            skip_next = True
            continue
        if tok.startswith("--"):
            if "=" in tok and not (is_git and tok.split("=", 1)[0] in _GIT_TEXT_VALUE_OPTIONS):
                values.append(tok.split("=", 1)[1])
            continue
        if tok.startswith("-") and len(tok) > 2:
            attached = _attached_path_value(prog, tok)
            if attached:
                values.append(attached)
            continue
        if not tok.startswith("-"):
            values.append(tok)
    return [v for v in values if v]


def _sort_output_paths(rest: list[str]) -> list[str]:
    """Write destinations of a sort argv: ``-o F``, ``-oF``, ``-uoF``, ``--output[=]F``.

    sort only reads its operands, so they are not destinations. ``--output`` is
    matched by unambiguous prefix (``--out``), as GNU getopt_long accepts it.
    """
    out: list[str] = []
    value_options = {
        "--buffer-size",
        "--compress-program",
        "--field-separator",
        "--files0-from",
        "--key",
        "--output",
        "--parallel",
        "--random-source",
        "--sort",
        "--temporary-directory",
    }
    i = 0
    while i < len(rest):
        tok = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else None
        if tok == "--":
            break
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            matches = [option for option in value_options if option.startswith(name)]
            if len(name) >= 3 and len(matches) == 1:
                option = matches[0]
                if option == "--output" and (eq or nxt is not None):
                    out.append(value if eq else nxt)
                if not eq and nxt is not None:
                    i += 1
        elif tok.startswith("-") and len(tok) > 1:
            cluster = tok[1:]
            for j, option in enumerate(cluster):
                if option not in "ktoST":
                    continue
                value = cluster[j + 1 :]
                if option == "o" and (value or nxt is not None):
                    out.append(value or nxt)
                if not value and nxt is not None:
                    i += 1
                break  # Everything after an argument-taking option is its value.
        i += 1
    return [v for v in out if v]


def _uniq_output_paths(rest: list[str]) -> list[str]:
    """uniq's write destination: its second operand (``uniq [OPTION]... [IN [OUT]]``)."""
    operands: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            operands.extend(rest[i + 1 :])
            break
        if tok in {"-f", "-s", "-w"}:  # value in the next token
            i += 2
            continue
        if not tok.startswith("-") or tok == "-":
            operands.append(tok)
        i += 1
    return operands[1:2]


def _targets_hook_dir(value: str, workdir: Path) -> bool:
    """True when a path argument lands inside .git/.hg (config, hooks) or a .husky tree."""
    path = Path(value).expanduser()
    resolved = (path if path.is_absolute() else workdir / path).resolve()
    parts = [p.lower() for p in resolved.parts]
    if ".husky" in parts:
        return True
    return any(part in {".git", ".hg"} for part in parts[:-1])


_SED_SCRIPT_READ_CAP = 1024 * 1024


def _sed_sources(rest: list[str]) -> tuple[list[str], list[str]]:
    """Inline script text and ``-f``/``--file`` filenames from a sed argv.

    ``-f`` / ``--file`` always name a script file. The filename is not sed
    program text, even when it contains spaces or starts with ``e`` or ``w``.
    """
    scripts: list[str] = []
    files: list[str] = []
    saw_script_source = False
    i = 0
    n = len(rest)

    def take_file(text: str) -> None:
        if text:
            files.append(text)

    while i < n:
        arg = rest[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("--expression="):
            saw_script_source = True
            scripts.append(arg.split("=", 1)[1])
            i += 1
            continue
        if arg in ("-e", "--expression"):
            saw_script_source = True
            if i + 1 < n:
                scripts.append(rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        if arg.startswith("--file="):
            saw_script_source = True
            take_file(arg.split("=", 1)[1])
            i += 1
            continue
        if arg in ("-f", "--file"):
            saw_script_source = True
            if i + 1 < n:
                take_file(rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        if arg.startswith("--"):
            i += 1
            continue
        if arg.startswith("-") and arg != "-":
            j = 1
            while j < len(arg):
                opt = arg[j]
                if opt == "e":
                    saw_script_source = True
                    if j + 1 < len(arg):
                        scripts.append(arg[j + 1 :])
                    elif i + 1 < n:
                        scripts.append(rest[i + 1])
                        i += 1
                    break
                if opt == "f":
                    saw_script_source = True
                    if j + 1 < len(arg):
                        take_file(arg[j + 1 :])
                    elif i + 1 < n:
                        take_file(rest[i + 1])
                        i += 1
                    break
                if opt == "i":
                    break
                j += 1
            i += 1
            continue
        if not saw_script_source:
            scripts.append(arg)
        break
    return scripts, files


def _sed_script_root_ok(resolved: Path, workdir: Path) -> bool:
    """True when the script is inside the workspace, the command cwd, or the active worktree."""
    roots = [config.workspace_root().resolve(), workdir.resolve()]
    worktree = get_active_worktree_path()
    if worktree is not None:
        roots.append(worktree.resolve())
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _read_sed_script(resolved: Path) -> bytes | None:
    """Read at most one byte past the cap from a regular file. None if it is not a regular file."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(resolved, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks: list[bytes] = []
        remaining = _SED_SCRIPT_READ_CAP + 1
        while remaining > 0:
            part = os.read(fd, remaining)
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sed_file_bypass(filename: str, workdir: Path) -> str | None:
    """Why a sed -f file must be approved, or None when it is absent or safe.

    Missing files are not flagged: sed will not run them. A regular file inside
    the workspace or the active task worktree is scanned for e/w/W, with the
    read capped so a growing file cannot be slurped. Stdin, non-regular files,
    unreadable files, and paths outside that boundary fail closed without blocking.
    """
    if filename == "-":
        return (
            "sed -f - reads the script from stdin, so e/w commands cannot be ruled out "
            "before execution."
        )
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = workdir / path
    try:
        resolved = path.resolve()
    except OSError:
        return f"sed -f {filename!r} could not be resolved, so e/w commands cannot be ruled out."
    if not _sed_script_root_ok(resolved, workdir):
        return (
            f"sed -f {filename!r} resolves outside the workspace, so its e/w commands "
            "cannot be inspected."
        )
    try:
        blob = _read_sed_script(resolved)
    except FileNotFoundError:
        return None
    except OSError:
        return f"sed -f {filename!r} could not be read, so e/w commands cannot be ruled out."
    if blob is None:
        return f"sed -f {filename!r} is not a regular file, so e/w commands cannot be ruled out."
    if len(blob) > _SED_SCRIPT_READ_CAP:
        return f"sed -f {filename!r} is too large to inspect, so e/w commands cannot be ruled out."
    text = blob.decode("utf-8", errors="replace")
    if _sed_script_has_exec_or_write(text):
        return (
            f"sed -f {filename!r} contains the e, w, or W command, which executes a program "
            "or writes a file and sidesteps the run_cmd allowlist."
        )
    return None


def _skip_delimited(script: str, i: int, delim: str, *, regex: bool) -> int:
    """Advance past a sed pattern or replacement, returning the index after delim."""
    n = len(script)
    bracket = False
    while i < n:
        char = script[i]
        if char == "\\" and not bracket:
            i += 2
            continue
        if regex and char == "[" and not bracket:
            bracket = True
            i += 1
            if i < n and script[i] == "^":
                i += 1
            if i < n and script[i] == "]":
                i += 1
            continue
        if regex and char == "]" and bracket:
            bracket = False
            i += 1
            continue
        if char == delim and not bracket:
            return i + 1
        if char == "\n" and not bracket:
            return i
        i += 1
    return i


def _skip_one_sed_address(script: str, i: int) -> int | None:
    n = len(script)
    if i >= n:
        return None
    char = script[i]
    if char.isdigit():
        j = i + 1
        while j < n and script[j].isdigit():
            j += 1
        if j < n and script[j] == "~":
            j += 1
            while j < n and script[j].isdigit():
                j += 1
        return j
    if char == "$":
        return i + 1
    if char == "/":
        return _skip_delimited(script, i + 1, "/", regex=True)
    if char == "\\" and i + 1 < n:
        return _skip_delimited(script, i + 2, script[i + 1], regex=True)
    return None


def _skip_sed_addresses(script: str, i: int) -> int:
    nxt = _skip_one_sed_address(script, i)
    if nxt is None:
        return i
    i = nxt
    k = i
    n = len(script)
    while k < n and script[k] in " \t":
        k += 1
    if k < n and script[k] == ",":
        k += 1
        while k < n and script[k] in " \t":
            k += 1
        second = _skip_one_sed_address(script, k)
        if second is not None:
            return second
    return i


def _skip_sed_subst(script: str, i: int) -> tuple[int, bool]:
    """Parse an s/// command starting at its delimiter. The bool is the e/w/W flag."""
    n = len(script)
    if i >= n or script[i] in "\\\n":
        return i, False
    delim = script[i]
    i = _skip_delimited(script, i + 1, delim, regex=True)
    i = _skip_delimited(script, i, delim, regex=False)
    while i < n:
        char = script[i]
        if char.isdigit() or char in "gpiImM":
            i += 1
            continue
        if char in "ewW":
            return i, True
        break
    return i, False


def _skip_sed_y(script: str, i: int) -> int:
    n = len(script)
    if i >= n or script[i] in "\\\n":
        return i
    delim = script[i]
    i = _skip_delimited(script, i + 1, delim, regex=False)
    return _skip_delimited(script, i, delim, regex=False)


def _sed_script_has_exec_or_write(script: str) -> bool:
    """True if GNU sed script text contains the e command or the w/W write commands.

    Covers the bare commands and the s///e, s///w, and s///W flags. Letters inside
    a substitute pattern, replacement, y/// transliteration, comment, or label are
    not commands. Unparseable scripts fail closed (treated as executable).
    """
    i = 0
    n = len(script)
    steps = 0
    while i < n:
        steps += 1
        if steps > n + 2:
            return True
        begin = i
        char = script[i]
        if char in " \t;\n":
            i += 1
        elif char == "#":
            nl = script.find("\n", i)
            i = n if nl < 0 else nl + 1
        else:
            i = _skip_sed_addresses(script, i)
            while i < n and script[i] in " \t":
                i += 1
            if i < n and script[i] == "!":
                i += 1
                while i < n and script[i] in " \t":
                    i += 1
            if i >= n:
                break
            if script[i] in ";\n":
                i += 1
            else:
                cmd = script[i]
                i += 1
                if cmd in "ewW":
                    return True
                if cmd == "s":
                    i, dangerous = _skip_sed_subst(script, i)
                    if dangerous:
                        return True
                elif cmd == "y":
                    i = _skip_sed_y(script, i)
                elif cmd in "btT:":
                    while i < n and script[i] in " \t":
                        i += 1
                    while i < n and script[i] not in "\n;":
                        i += 1
                elif cmd in "aci":
                    if i < n and script[i] == "\\":
                        i += 1
                    nl = script.find("\n", i)
                    i = n if nl < 0 else nl + 1
                elif cmd in "rR":
                    nl = script.find("\n", i)
                    i = n if nl < 0 else nl + 1
        if i <= begin:
            i = begin + 1
    return False


def _mutation_risk(argv: list[str]) -> str:
    """Classify filesystem mutation risk as no|maybe|yes (heuristic, argv-based)."""
    prog, rest = argv[0], argv[1:]
    if prog in _READ_ONLY_PROGRAMS:
        return "no"
    if prog == "sort":
        return "yes" if _sort_output_paths(rest) else "no"
    if prog == "uniq":
        return "yes" if _uniq_output_paths(rest) else "no"
    if prog == "find":
        return "yes" if any(t in _FIND_SIDE_EFFECT_ACTIONS for t in rest) else "no"
    if prog == "sed":
        return "yes" if _sed_in_place(rest) else "no"
    if prog == "ruff":
        if "--fix" in rest or (
            rest and rest[0] == "format" and not {"--check", "--diff"} & set(rest)
        ):
            return "yes"
        return "no"
    if prog == "git":
        sub = _git_subcommand(rest)
        if sub in _GIT_OUTPUT_SUBCOMMANDS and _has_flag(_git_subcommand_args(rest), "--output"):
            return "yes"
        if sub in _GIT_READ_ONLY:
            return "no"
        if sub in _GIT_MUTATING:
            return "yes"
        return "maybe"
    return "maybe"  # python3/uv/pytest/awk and custom allowlist entries can do anything


def _advisories(argv: list[str], workdir: Path) -> list[dict]:
    """Warn/info findings for a vetted argv: literal shell syntax, path escapes, bypasses."""
    findings: list[dict] = []
    prog, rest = argv[0], argv[1:]

    def add(severity: str, code: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "message": message})

    syntax = sorted(
        {t for t in rest if t in _SHELL_SYNTAX_TOKENS or (len(t) > 1 and t.endswith(";"))}
        - ({";"} if prog == "find" else set())
    )
    if syntax:
        add(
            "warn",
            "shell-syntax",
            f"run_cmd uses no shell: {', '.join(syntax)} reach {prog} as literal arguments, so "
            "pipes/redirection/chaining will not happen. Make separate run_cmd calls instead "
            "(or fs_write to save output).",
        )
    expansions = [t for t in argv if _EXPANSION_RE.search(t)]
    if expansions:
        add(
            "warn",
            "no-expansion",
            f"no shell expansion: {expansions[0]!r} is passed literally "
            "($VAR, $(...) and backticks are never expanded).",
        )

    root = config.workspace_root().resolve()
    name = _program_name(prog)
    if name == "sort":
        output_paths = _sort_output_paths(rest)
    elif name == "uniq":
        output_paths = _uniq_output_paths(rest)
    else:
        output_paths = []
    path_args = [(raw, False) for raw in rest] + [(raw, True) for raw in output_paths]
    for raw, explicit_path in path_args:
        tok = raw
        if not explicit_path and tok.startswith("-"):
            if "=" in tok:
                tok = tok.split("=", 1)[1]
            elif not tok.startswith("--") and _attached_path_value(prog, tok):
                tok = _attached_path_value(prog, tok)  # git -C/etc, sort -o/tmp/x
            else:
                continue
        if not tok:
            continue
        if not explicit_path and not (
            tok.startswith(("/", "~", "./", "../")) or tok in (".", "..") or "/../" in tok
        ):
            continue
        resolved = Path(tok).expanduser()
        resolved = (resolved if resolved.is_absolute() else workdir / resolved).resolve()
        if not (resolved == root or resolved.is_relative_to(root)):
            add(
                "warn",
                "path-escape",
                f"argument {raw!r} resolves to {resolved}, outside the workspace root {root}. "
                f"run_cmd only confines cwd, not arguments — {prog} could read or write there. "
                "Keep paths inside the workspace.",
            )

    # fs_read refuses credential/key/database files; a content reader must not
    # print them either. Only existing paths count, so a pattern such as
    # `grep token f.txt` is not mistaken for one.
    if name in _CONTENT_READERS:
        for value in _arg_path_values(prog, rest):
            candidate = Path(value).expanduser()
            candidate = candidate if candidate.is_absolute() else workdir / candidate
            if not os.path.lexists(candidate):
                continue
            try:
                guard_secret_filename(candidate.resolve(), value)
            except ToolError as e:
                add("block", "secret-path", f"{name}: {e}")
                break

    if prog == "find":
        hits = [t for t in rest if t in _FIND_SIDE_EFFECT_ACTIONS]
        if hits:
            add(
                "warn",
                "allowlist-bypass",
                f"find {hits[0]} deletes or writes files or runs an arbitrary program, "
                "sidestepping the run_cmd allowlist.",
            )
    if name == "sort":
        hit = _has_flag(rest, "--compress-program")
        if hit:
            add(
                "warn",
                "allowlist-bypass",
                f"sort {hit} pipes data through the named program, sidestepping the run_cmd "
                "allowlist.",
            )
    # Pure readers (cat, ls, grep, ...) never write. sort writes only to -o/--output;
    # its operands are inputs. Anything else might write any path argument,
    # including read-only git subcommands given --output.
    if name in _READ_ONLY_PROGRAMS:
        hook_candidates: list[str] = []
    elif name in ("sort", "uniq"):
        hook_candidates = output_paths
    else:
        hook_candidates = _arg_path_values(prog, rest)
    hook_hits = [v for v in hook_candidates if _targets_hook_dir(v, workdir)]
    if hook_hits:
        add(
            "warn",
            "allowlist-bypass",
            f"argument {hook_hits[0]!r} points into .git, .hg or .husky; a hook or config "
            "written there runs a program on the next git operation, sidestepping the run_cmd "
            "allowlist.",
        )
    if name == "awk" and _awk_can_run_code(rest):
        add(
            "warn",
            "allowlist-bypass",
            "awk program text or -f/-e script can call system(), sidestepping the run_cmd allowlist.",
        )
    if name == "sed":
        inline_scripts, script_files = _sed_sources(rest)
        if any(_sed_script_has_exec_or_write(script) for script in inline_scripts):
            add(
                "warn",
                "allowlist-bypass",
                "sed script contains the e, w, or W command, which executes a program or writes "
                "a file and sidesteps the run_cmd allowlist.",
            )
        for filename in script_files:
            reason = _sed_file_bypass(filename, workdir)
            if reason:
                add("warn", "allowlist-bypass", reason)
    if name == "git":
        for hit in _git_exec_config_hits(rest):
            if hit == "--config-env" or hit.startswith("--config-env="):
                message = (
                    "git --config-env reads configuration from the environment, which can set a "
                    "shell alias or core.pager and sidestep the run_cmd allowlist."
                )
            elif hit == "--exec-path" or hit.startswith("--exec-path="):
                message = (
                    "git --exec-path selects which git binaries run, sidestepping the "
                    "run_cmd allowlist."
                )
            elif hit.startswith(_GIT_REPO_SELECT_FLAGS):
                message = (
                    f"git {hit} selects a repository whose config (aliases, pager, hooks) "
                    "may have been written by the agent, sidestepping the run_cmd allowlist."
                )
            else:
                message = (
                    f"git -c {hit!r} sets {_git_config_key(hit)}, which can run an arbitrary "
                    "program and sidestep the run_cmd allowlist."
                )
            add("warn", "allowlist-bypass", message)
        reason = _git_exec_subcommand_reason(rest)
        if reason:
            add("warn", "allowlist-bypass", f"{reason}, sidestepping the run_cmd allowlist.")
        sub = _git_subcommand(rest)
        if sub in _GIT_NETWORK:
            add(
                "warn",
                "network",
                f"git {sub} contacts remote repositories.",
            )
    if name in ("python3", "python") and _python_executes_code(rest):
        add(
            "warn",
            "arbitrary-code",
            f"{name} executes arbitrary code (a script, -m, -c, or the REPL); only its cwd is "
            "confined to the workspace.",
        )
    if name in ("uv", "pytest"):
        add(
            "warn",
            "arbitrary-code",
            f"{name} executes arbitrary project code; only its cwd is confined to the workspace.",
        )
    return findings


def _devices_only_refusal() -> str | None:
    """Why a devices-only hub (task #2109) refuses run_cmd, or None.

    The whole tool is refused, not a list of programs: an allowlisted wrapper
    (``env``, ``timeout``), an interpreter, or a repository-configured git
    helper (``core.fsmonitor``) can each start an agent in a child process that
    the hub's audit hook cannot see. A control plane has no need to run
    commands; that work belongs on a device.
    """
    from .. import execution_locality

    if not execution_locality.devices_only():
        return None
    return execution_locality.refusal_message("run_cmd")


def run_cmd_check(command: str, cwd: str = ".") -> dict:
    """Vet a run_cmd invocation without executing anything (dry run).

    Reports whether run_cmd would accept the command and flags likely mistakes:
    shell syntax that becomes literal arguments, unexpanded $VAR/$(...), path
    arguments escaping the workspace, allowlist bypasses (non-destructive find
    -exec, awk program text or system(), sed e/w/W, git alias/pager/exec-path/
    --config-env), arbitrary code (python3/python scripts, -m, -c, uv, pytest),
    and a no|maybe|yes file-mutation estimate. Finding severities: "block"
    (run_cmd would reject it — allowlist, cwd, or the deterministic
    destructive-command gate), "warn" (probably not what you intended), "info"
    (heads-up). Returns {ok, verdict, argv, program, program_found, mutates,
    findings}.
    """
    from ..hosted_execution import execution_refusal_message, is_hosted

    findings: list[dict] = []
    argv: list[str] = []
    if is_hosted():
        findings.append(
            {
                "severity": "block",
                "code": "hosted_execution_refused",
                "message": execution_refusal_message("run_cmd"),
            }
        )
    try:
        argv = vet_command(command)
    except ToolError as e:
        message = str(e)
        code = "destructive" if message.startswith(DESTRUCTIVE_COMMAND_PREFIX) else "rejected"
        findings.append({"severity": "block", "code": code, "message": message})
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
    workdir = config.workspace_root().resolve()
    try:
        workdir = resolve_in_workspace(cwd)
    except ToolError as e:
        findings.append({"severity": "block", "code": "cwd", "message": str(e)})

    program = argv[0] if argv else ""
    program_found = bool(program) and shutil.which(program) is not None
    if program and not program_found:
        findings.append(
            {
                "severity": "warn",
                "code": "not-found",
                "message": f"{program!r} not found on PATH; run_cmd would return exit code 127.",
            }
        )
    if argv:
        findings.extend(_advisories(argv, workdir))
        from ..hosted_capabilities import empty_hosted_census_message

        hosted = empty_hosted_census_message(argv, workdir)
        if hosted:
            findings.append(
                {
                    "severity": "block",
                    "code": "hosted-workspace",
                    "message": hosted,
                }
            )
    devices = _devices_only_refusal()
    if devices:
        findings.append({"severity": "block", "code": "server-execution", "message": devices})

    blocked = any(f["severity"] == "block" for f in findings)
    warned = any(f["severity"] == "warn" for f in findings)
    return {
        "ok": not blocked,
        "verdict": "block" if blocked else ("warn" if warned else "ok"),
        "argv": argv,
        "program": program,
        "program_found": program_found,
        "mutates": _mutation_risk(argv) if argv else "no",
        "findings": findings,
    }


def _execute_in_sandbox(
    argv: list[str], workdir: Path, env: dict, timeout: float
) -> subprocess.CompletedProcess:
    """Run command inside a containerized sandbox (Docker or Podman)."""
    engine = config.sandbox_engine()
    if engine == "auto":
        if shutil.which("podman"):
            engine = "podman"
        elif shutil.which("docker"):
            engine = "docker"
        else:
            raise FileNotFoundError("No container engine (docker or podman) found in PATH")

    ws_root = config.workspace_root().resolve()
    container_name = f"ua-sandbox-{uuid.uuid4().hex}"

    # Map the host workdir inside the container by mounting the workspace root
    # at the exact same path. This makes absolute paths resolve transparently.
    container_cmd = [
        engine,
        "run",
        "--rm",
        "--name",
        container_name,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "100",
        "-v",
        f"{ws_root}:{ws_root}",
        "-w",
        str(workdir),
    ]

    if not config.sandbox_network():
        container_cmd.extend(["--network", "none"])

    # Map current user/group on Unix to prevent permissions issues on files written by sandbox
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        container_cmd.extend(["--user", f"{os.getuid()}:{os.getgid()}"])

    # Copy environment and customize for container sandbox:
    # Drop host PATH so it does not shadow container PATH, and override HOME
    sandbox_env = env.copy()
    sandbox_env.pop("PATH", None)
    sandbox_env["HOME"] = str(ws_root)

    # Pass specified environment variables
    for k, v in sandbox_env.items():
        container_cmd.extend(["-e", f"{k}={v}"])

    container_cmd.append(config.sandbox_image())
    container_cmd.extend(argv)

    try:
        return subprocess.run(
            container_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        subprocess.run(
            [engine, "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        raise e


def run_cmd(command: str, cwd: str = ".", timeout_s: int = 30) -> dict:
    """Run an allowlisted command inside the workspace (no shell; metacharacters are literal).

    Timeout is capped at 120 s and combined output at 50 KB. `warnings` carries
    advisory vetting notes (literal shell syntax, workspace-escaping path args, …);
    use run_cmd_check to inspect a command before running it.
    Returns {exit_code, stdout, stderr, truncated, timed_out, warnings}.

    Approval (task #62 · C, tightened by #682): layered AFTER allowlist/cwd
    and the deterministic destructive-command deny (task #834) — can only
    tighten, never bypass. Clean commands still respect UA_APPROVAL_TOOLS
    (unset = no-op). Dangerous advisory classes (path-escape, allowlist-bypass,
    arbitrary-code) force the gate regardless. Git network advisories stay
    warn-only except ``git push`` (remote mutation).
    """
    from .. import agent_access
    from ..hosted_execution import refuse_hosted_execution

    refuse_hosted_execution("run_cmd")
    agent_access.enforce("run_cmd", read_only=False)
    devices = _devices_only_refusal()
    if devices:
        raise ToolError(devices)
    argv = vet_command(command)
    workdir = resolve_in_workspace(cwd)
    from ..hosted_capabilities import refuse_empty_hosted_command

    refuse_empty_hosted_command("run_cmd", argv, workdir)
    findings = _advisories(argv, workdir)
    for f in findings:
        if f["severity"] == "block":
            raise ToolError(f["message"])
    warnings = [f["message"] for f in findings if f["severity"] == "warn"]
    from .approvals import check_approval_gate

    payload: dict = {"command": command, "cwd": cwd}
    dangerous_codes = sorted({f["code"] for f in findings if f["code"] in _APPROVAL_REQUIRED_CODES})
    if argv[0] == "git" and _git_subcommand(argv[1:]) in _GIT_REMOTE_MUTATING:
        # Keep the network code on the payload so reviewers see the reason.
        dangerous_codes = sorted(set(dangerous_codes) | {"network"})
    force = bool(dangerous_codes)
    if force:
        payload["advisory_codes"] = dangerous_codes
    check_approval_gate(
        "run_cmd", payload, requested_by=config.verified_agent_identity(), force=force
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    if _program_name(argv[0]) == "git":
        # Never discover a bare repository the agent laid out with fs_write; its
        # config could define aliases or a pager. Explicit --git-dir is flagged above.
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="safe.bareRepository",
            GIT_CONFIG_VALUE_0="explicit",
        )
    timeout = max(1, min(timeout_s, config.CMD_TIMEOUT_CAP_S))
    try:
        if config.sandbox_enabled():
            proc = _execute_in_sandbox(argv, workdir, env, timeout)
        else:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        stdout, stderr, exit_code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        stdout = (
            (e.stdout or b"").decode(errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr = (
            (e.stderr or b"").decode(errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        exit_code, timed_out = -1, True
    except FileNotFoundError as e:
        if config.sandbox_enabled():
            return {
                "exit_code": 127,
                "stdout": "",
                "stderr": f"container engine not found or failed to start: {e}",
                "truncated": False,
                "timed_out": False,
                "warnings": warnings,
            }
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"command not found: {argv[0]}",
            "truncated": False,
            "timed_out": False,
            "warnings": warnings,
        }

    cap = config.CMD_OUTPUT_CAP
    truncated = len(stdout) + len(stderr) > cap
    if truncated:
        stdout = stdout[: cap // 2]
        stderr = stderr[: cap // 2]
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "timed_out": timed_out,
        "warnings": warnings,
    }
