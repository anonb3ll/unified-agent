"""Where agent work may execute: on this host, or only on enrolled devices (task #2109).

The cloud control plane (epic #2107) coordinates and never executes. A hub
running with ``UA_EXECUTION_LOCALITY=devices`` must not start an agent harness,
headless CLI, executor, Pi session, dispatch supervisor, or server-side
workflow runtime in its own process tree. Agent work goes to an enrolled device
through the ua-node queue instead.

``local`` is the default and changes nothing. On a hosted platform
(``RAILWAY_ENVIRONMENT_ID`` set, or ``UA_HOSTED=1``) the default is ``devices``.
Hosted hubs must use ``devices``; ``local`` is allowed only on ordinary hosts.
An unrecognised value fails closed to ``devices``. ``validate()`` refuses it at
hub start, and the doctor row reports it.

Two layers enforce the boundary:

1. **Each launch surface checks the locality and refuses with an actionable
   message.** This covers Control Center and scheduler dispatch (routed to a
   node, with no local fallback), the dispatch spawner and supervisor,
   ``pi_run_task``, ``run_cmd`` (refused whole) and the workflows built on it,
   the project-code runners (``red_team_qa``, the merge test gate, bug-worker
   gates, session-end checks), the ``claude_cli`` leg of ``llm_client``, the
   agy path of ``gemini_ask``, the executor daemon, and ``unified-agent node``.
   LangGraph runs stay allowed: they execute in-process with a deterministic
   planner and start no model or agent process.
2. **``install_process_guard()``** registers a ``sys.addaudithook`` hook in the
   hub process. It refuses any ``subprocess.Popen``, ``os.system``, ``os.exec*``
   or ``os.posix_spawn`` *from the hub process* whose program is a known agent
   harness, agent module, or Git (whose configured helpers execute project code).
   Audit hooks cannot be removed.

The hook does not reach a child's own exec: a wrapper (``env``, ``timeout``),
a test runner, or a git helper configured by a repository runs outside the
hub's interpreter. Layer 1 therefore refuses every surface that runs caller-
or project-supplied code on a devices-only hub: ``run_cmd`` as a whole, Git
repository operations (including read commands with configured helpers), the
linter (whose Python module could be shadowed), the
merge and QA test gates, bug-worker gates, and the session-end checks. The
hosted image also ships no harness binaries. OS-level confinement of
descendants is not provided here.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import PurePath

LOCALITY_LOCAL = "local"
LOCALITY_DEVICES = "devices"
LOCALITIES = frozenset({LOCALITY_LOCAL, LOCALITY_DEVICES})
ENV = "UA_EXECUTION_LOCALITY"

# Basenames of agent harnesses and coding-agent CLIs. A hub in devices mode
# never executes these, whichever path or allowlist would otherwise reach them.
HARNESS_BINARIES = frozenset(
    {
        "agy",
        "aider",
        "amp",
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "crush",
        "cursor",
        "cursor-agent",
        "devin",
        "droid",
        "gemini",
        "goose",
        "grok",
        "hermes",
        "kiro",
        "opencode",
        "pi",
        "qwen",
        "unified-agent-executor",
    }
)

# Python module entry points that host an agent process tree.
_AGENT_MODULES = (
    "unified_agent.dispatch.supervisor_spawn",
    "unified_agent.executor",
    "unified_agent.node_agent",
)


_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
_PYTHON = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?t?(?:\.exe)?$")


class ServerExecutionRefused(RuntimeError):
    """A launch was attempted on a hub whose agent work belongs on devices."""


def _hosted(env: Mapping[str, str]) -> bool:
    return bool((env.get("RAILWAY_ENVIRONMENT_ID") or "").strip()) or (
        (env.get("UA_HOSTED") or "").strip() == "1"
    )


def raw_setting(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get(ENV) or "").strip().lower()


def locality(env: Mapping[str, str] | None = None) -> str:
    """``local`` or ``devices``. Unknown values fail closed to ``devices``."""
    env = os.environ if env is None else env
    raw = raw_setting(env)
    if _hosted(env):
        return LOCALITY_DEVICES
    if not raw:
        return LOCALITY_LOCAL
    return raw if raw in LOCALITIES else LOCALITY_DEVICES


def source(env: Mapping[str, str] | None = None) -> str:
    """Where the effective value came from, for doctor and errors."""
    env = os.environ if env is None else env
    raw = raw_setting(env)
    if _hosted(env) and raw == LOCALITY_LOCAL:
        return f"{ENV}=local is invalid on a hosted hub (failing closed)"
    if raw in LOCALITIES:
        return ENV
    if raw:
        return f"{ENV}={raw!r} (unrecognised; failing closed)"
    return "hosted platform default" if _hosted(env) else "default"


def devices_only(env: Mapping[str, str] | None = None) -> bool:
    return locality(env) == LOCALITY_DEVICES


def validate(env: Mapping[str, str] | None = None) -> None:
    """Refuse unknown settings and hosted local execution at hub start."""
    raw = raw_setting(env)
    if raw and raw not in LOCALITIES:
        raise ValueError(f"{ENV} must be one of {sorted(LOCALITIES)}, got {raw!r}")
    if _hosted(os.environ if env is None else env) and raw == LOCALITY_LOCAL:
        raise ValueError(f"{ENV}=local is refused on a hosted hub; use devices")


def refusal_message(surface: str) -> str:
    return (
        f"{surface} refused: this hub runs with {ENV}=devices "
        f"({source()}), so agent work executes only on enrolled devices, never "
        "on the hub. Dispatch the task to a ua-node device instead (Control "
        "Center dispatch or the node queue); register a device with "
        "`unified-agent node` on the machine that should run it."
    )


def refuse(surface: str) -> None:
    """Raise ServerExecutionRefused when this hub is devices-only."""
    if devices_only():
        raise ServerExecutionRefused(refusal_message(surface))


def refuse_tool(surface: str) -> None:
    """Same check, raised as the MCP ToolError a tool caller expects."""
    if devices_only():
        from mcp.server.mcpserver.exceptions import ToolError

        raise ToolError(refusal_message(surface))


def _command_words(argv: object) -> list[str]:
    if isinstance(argv, (str, bytes, os.PathLike)):
        try:
            return shlex.split(os.fsdecode(argv))
        except ValueError:
            return []
    else:
        try:
            parts = list(argv)  # type: ignore[arg-type]
        except TypeError:
            return []
    return [os.fsdecode(p) if isinstance(p, (bytes, os.PathLike)) else str(p) for p in parts]


def is_agent_launch(argv: object) -> bool:
    """True when ``argv`` (a Popen/exec args value) starts an agent process."""
    words = _command_words(argv)
    if not words:
        return False
    # A shell string ("claude -p ...") is one word with spaces.
    head = words[0].split()[0] if words[0].strip() else ""
    if PurePath(head).name in HARNESS_BINARIES:
        return True
    # `sh -c "claude ..."`, `bash -lc ...`: judge the command the shell runs.
    if PurePath(head).name in _SHELLS:
        for i, word in enumerate(words[1:-1], start=1):
            if word.startswith("-") and "c" in word.lstrip("-") and not word.startswith("--"):
                if is_agent_launch(words[i + 1]):
                    return True
    # A Python interpreter launching an agent module: `python -m <module>`.
    # Module names elsewhere in argv are data (a git --grep, a gh comment).
    if not _PYTHON.fullmatch(PurePath(head).name):
        return False
    # Parse interpreter options only. After -c, a script, or --, every later
    # -m is application data. -W and -X consume either an attached or separate
    # value, which may itself happen to look like an agent module.
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--" or word == "-" or not word.startswith("-"):
            return False
        if word in {"--help", "--help-env", "--help-xoptions", "--help-all", "--version"}:
            return False
        if word == "--check-hash-based-pycs":
            i += 2
            continue
        if word.startswith("--"):
            return False
        for pos, flag in enumerate(word[1:], start=1):
            if flag in {"c", "h", "V"}:
                return False
            if flag == "m":
                module = word[pos + 1 :] or (words[i + 1] if i + 1 < len(words) else "")
                return any(module == mod or module.startswith(mod + ".") for mod in _AGENT_MODULES)
            if flag in {"W", "X"}:
                if not word[pos + 1 :]:
                    i += 1
                break
            if flag not in "bBdEiIOPqRsSuvx":
                return False
        i += 1
    return False


def _git_launch(argv: object) -> bool:
    """Git can execute repository-controlled helpers even for read operations.

    Refuse it in the process backstop too: Git consumers beyond tools/git.py
    (reviewer, audit, planning, worktree helpers) share this risk. Device-mode
    repository operations belong on the device; ordinary local mode is intact.
    """
    words = _command_words(argv)
    if not words:
        return False
    if PurePath(words[0]).name in {"git", "git.exe"}:
        return True
    if PurePath(words[0]).name in _SHELLS:
        for i, word in enumerate(words[1:-1], start=1):
            if word.startswith("-") and "c" in word.lstrip("-") and not word.startswith("--"):
                return _git_launch(words[i + 1])
    return False


def _refused_launch(argv: object) -> bool:
    return is_agent_launch(argv) or _git_launch(argv)


_installed = False


def _audit_hook(event: str, args: tuple) -> None:
    if event == "subprocess.Popen":
        # (executable, args, cwd, env)
        executable, argv = args[0], args[1]
        if _refused_launch(argv) or (executable is not None and _refused_launch([executable])):
            raise ServerExecutionRefused(refusal_message("agent or repository process launch"))
    elif event == "os.system":
        # (command,) — a shell string.
        if _refused_launch(args[0]):
            raise ServerExecutionRefused(refusal_message("agent or repository process launch"))
    elif event in {"os.exec", "os.posix_spawn"}:
        # (path, argv, env)
        path, argv = args[0], args[1]
        if _refused_launch([path]) or _refused_launch(argv):
            raise ServerExecutionRefused(refusal_message("agent or repository process launch"))


def install_process_guard() -> bool:
    """Install the audit-hook backstop when devices-only. Returns True if active."""
    global _installed
    if not devices_only():
        return False
    if not _installed:
        sys.addaudithook(_audit_hook)
        _installed = True
    return True


def process_guard_installed() -> bool:
    return _installed
