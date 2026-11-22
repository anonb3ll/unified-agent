"""PTY-driven interactive agy executor backend (task #1177).

Print-mode ``agy -p`` drains background work after ~5s idle (#1158). Interactive
agy survived a full dispatch lifecycle in the #1146 experiment. This backend
drives that path over a PTY: feed the prompt (with a trailing newline — turn
submission requires Enter), stream the transcript, auto-approve only the
allowlisted command classes validated in that experiment, and exit via ``/exit``
when the ``>`` prompt returns.

Fail closed: unknown or destructive commands are never auto-approved.

Stall detection (task #1950): after the turn is submitted, any new transcript
output is the run's progress signal. A session that writes nothing for
``config.executor_stall_timeout_s()`` seconds (``0`` disables) is stopped and
returned as ``status="timeout"`` with an ``Execution stalled`` error, the same
contract ``run_popen_with_kill_check`` gives the print-mode backends (#1916).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from .. import config

logger = logging.getLogger("unified_agent.executor.agy_interactive")

# Command classes validated in the #1146 interactive experiment. Class-level
# ``command(<name>)`` entries in settings.json skip the approve menu for those
# programs; the menu driver is the fail-closed backstop when a prompt still
# appears (or for a more-specific argv that was not pre-seeded).
AGY_INTERACTIVE_ALLOW_CLASSES: tuple[str, ...] = ("git", "pytest", "ruff", "gh")

# Never auto-approve these even if the first token somehow matched a class.
_DESTRUCTIVE_COMMAND_RE = re.compile(
    r"^(?:"
    r"rm|rmdir|sudo|doas|chmod|chown|chgrp|mkfs|dd|shutdown|reboot|halt|"
    r"poweroff|userdel|deluser|passwd|killall|pkill|wipefs|shred|"
    r"curl|wget|nc|ncat|netcat|python[23]?|perl|ruby|node|bash|sh|zsh|fish"
    r")(?:\s|$)",
    re.IGNORECASE,
)

_APPROVAL_HINT_RE = re.compile(
    r"(?:allow(?:\s+this)?\s+command|do you want to (?:allow|run|proceed)|"
    r"permission(?:\s+required)?|approve\s+(?:this\s+)?(?:command|tool)|"
    r"run\s+this\s+command)",
    re.IGNORECASE,
)
# MULTILINE must be a compile flag — inline (?m) mid-alternation is a
# PatternError on Python 3.11+ ("global flags not at the start").
_COMMAND_EXTRACT_RE = re.compile(
    r"(?:"
    r"command\(([^)]+)\)|"
    r"`([^`\n]+)`|"
    r"^\s{0,4}((?:git|pytest|ruff|gh|uv|python3?|rm|sudo)\b[^\n]*)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_PROMPT_RE = re.compile(r"^>\s*$", re.MULTILINE)
_MENU_CHOICE_RE = re.compile(
    r"^\s*(?:❯\s*)?1\.\s*Yes\b",
    re.IGNORECASE | re.MULTILINE,
)

DEFAULT_AGY_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
AGY_INTERACTIVE_APPROVAL_DENIED_REASON = "agy_interactive_approval_denied"
AGY_INTERACTIVE_DRIVER_REASON = "agy_interactive_driver"


ApprovalDecision = Literal["approve", "deny", "ignore"]


def settings_allow_entries(
    allow_classes: Sequence[str] = AGY_INTERACTIVE_ALLOW_CLASSES,
) -> list[str]:
    """Persisted ``permissions.allow`` entries for the allowlisted classes."""
    return [f"command({cls})" for cls in allow_classes]


def command_class(command: str) -> str | None:
    """Return the first-token program class, or None if empty."""
    text = (command or "").strip()
    if not text:
        return None
    # Strip shell wrappers like `env FOO=1 git status`.
    tokens = text.split()
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1
    if i >= len(tokens):
        return None
    prog = Path(tokens[i]).name
    if prog in {"env", "nice", "nohup", "command"} and i + 1 < len(tokens):
        prog = Path(tokens[i + 1]).name
    return prog.lower() or None


def is_allowlisted_command(
    command: str,
    allow_classes: Sequence[str] = AGY_INTERACTIVE_ALLOW_CLASSES,
) -> bool:
    """True only for an allowlisted class that is not on the destructive denylist."""
    text = (command or "").strip()
    if not text:
        return False
    if _DESTRUCTIVE_COMMAND_RE.match(text):
        return False
    cls = command_class(text)
    if cls is None:
        return False
    allowed = {c.lower() for c in allow_classes}
    return cls in allowed


def extract_approval_command(screen_text: str) -> str | None:
    """Best-effort command extraction from an interactive approval screen."""
    if not screen_text:
        return None
    # Prefer the last match near the end of the buffer (most recent prompt).
    matches = list(_COMMAND_EXTRACT_RE.finditer(screen_text))
    if not matches:
        return None
    groups = matches[-1].groups()
    for g in groups:
        if g and g.strip():
            return g.strip()
    return None


def looks_like_approval_menu(screen_text: str) -> bool:
    """Heuristic: approval copy and/or a numbered Yes option."""
    if not screen_text:
        return False
    tail = screen_text[-4000:]
    return bool(_APPROVAL_HINT_RE.search(tail) or _MENU_CHOICE_RE.search(tail))


def decide_approval(
    screen_text: str,
    *,
    allow_classes: Sequence[str] = AGY_INTERACTIVE_ALLOW_CLASSES,
) -> ApprovalDecision:
    """Approve only allowlisted classes; deny unknown/destructive; else ignore."""
    if not looks_like_approval_menu(screen_text):
        return "ignore"
    command = extract_approval_command(screen_text)
    if command is None:
        # Menu visible but no parseable command — fail closed.
        return "deny"
    if is_allowlisted_command(command, allow_classes=allow_classes):
        return "approve"
    return "deny"


def approval_keystroke(decision: ApprovalDecision) -> str | None:
    """Keystroke to send for a decided menu action (Enter-terminated)."""
    if decision == "approve":
        # First menu option is "Yes" in the #1146 interactive surface.
        return "1\n"
    if decision == "deny":
        return "3\n"
    return None


def detect_interactive_prompt(buffer: str) -> bool:
    """True when the TUI has returned to the ``>`` input prompt."""
    return bool(buffer and _PROMPT_RE.search(buffer))


def seed_agy_settings_allows(
    settings_path: Path | str,
    *,
    allow_classes: Sequence[str] = AGY_INTERACTIVE_ALLOW_CLASSES,
) -> list[str]:
    """Merge class-level ``command(...)`` allows into settings.json.

    Creates the file (and parents) when missing. Never removes existing entries.
    Returns the entries that were newly added.
    """
    path = Path(settings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("agy-interactive: could not parse %s (%s); rewriting allows", path, e)

    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow

    existing = {str(x) for x in allow}
    added: list[str] = []
    for entry in settings_allow_entries(allow_classes):
        if entry not in existing:
            allow.append(entry)
            added.append(entry)
            existing.add(entry)

    if added:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    return added


def resolve_agy_settings_path() -> Path:
    raw = (os.environ.get("UA_AGY_SETTINGS_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_AGY_SETTINGS_PATH


def _ensure_trailing_newline(prompt: str) -> str:
    return prompt if prompt.endswith("\n") else prompt + "\n"


def _driver_available() -> str:
    """Return ``pexpect``, ``tmux``, or raise."""
    try:
        import pexpect  # noqa: F401

        return "pexpect"
    except ImportError:
        pass
    if shutil.which("tmux"):
        return "tmux"
    raise RuntimeError(f"{AGY_INTERACTIVE_DRIVER_REASON}: need pexpect (preferred) or tmux on PATH")


def run_interactive_session(
    *,
    agy_bin: str,
    prompt: str,
    cwd: str,
    env: dict[str, str],
    timeout_s: int,
    kill_check: Callable[[], bool] | None = None,
    allow_classes: Sequence[str] = AGY_INTERACTIVE_ALLOW_CLASSES,
    settings_path: Path | None = None,
    progress_cb: Callable[[str], None] | None = None,
    model: str | None = None,
    stall_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Drive one interactive agy session to completion or a named error.

    ``stall_timeout_s`` defaults to ``config.executor_stall_timeout_s()``.
    """
    from ..hosted_execution import refuse_hosted_execution

    refuse_hosted_execution("agy-interactive")
    if stall_timeout_s is None:
        stall_timeout_s = config.executor_stall_timeout_s()
    seed_path = settings_path if settings_path is not None else resolve_agy_settings_path()
    added = seed_agy_settings_allows(seed_path, allow_classes=allow_classes)
    if added:
        logger.info("agy-interactive: seeded settings allows %s into %s", added, seed_path)

    # Ensure the child flushes promptly on the PTY (Python stdio otherwise
    # block-buffers when not attached to a human TTY).
    child_env = dict(env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    child_env.setdefault("TERM", "xterm")

    driver = _driver_available()
    if driver == "pexpect":
        return _run_with_pexpect(
            agy_bin=agy_bin,
            prompt=prompt,
            cwd=cwd,
            env=child_env,
            timeout_s=timeout_s,
            kill_check=kill_check,
            allow_classes=allow_classes,
            progress_cb=progress_cb,
            model=model,
            stall_timeout_s=stall_timeout_s,
        )
    return _run_with_tmux(
        agy_bin=agy_bin,
        prompt=prompt,
        cwd=cwd,
        env=child_env,
        timeout_s=timeout_s,
        kill_check=kill_check,
        allow_classes=allow_classes,
        progress_cb=progress_cb,
        model=model,
        stall_timeout_s=stall_timeout_s,
    )


def _stall_error(stall_timeout_s: float) -> str:
    return f"Execution stalled: no output for {stall_timeout_s:g} seconds"


class _Progress:
    """Monotonic time of the last transcript output (task #1950)."""

    def __init__(self, stall_timeout_s: float) -> None:
        self.stall_timeout_s = stall_timeout_s
        self.last = time.monotonic()

    def received(self) -> None:
        self.last = time.monotonic()

    def stalled(self) -> bool:
        return bool(self.stall_timeout_s) and (time.monotonic() - self.last >= self.stall_timeout_s)


class _ReadTap:
    """``pexpect`` ``logfile_read`` sink: called once per chunk read from the child.

    ``child.before`` is no progress signal: on ``TIMEOUT`` pexpect keeps its
    buffer and hands the same text back again, so a silent child would look
    busy forever.
    """

    def __init__(self, progress: _Progress) -> None:
        self._progress = progress

    def write(self, data: str) -> None:
        if data:
            self._progress.received()

    def flush(self) -> None:
        return None


#: SIGTERM-to-SIGKILL grace when stopping a session's process group (#1950).
_TREE_KILL_GRACE_S = 2.0
#: How long to wait for the group to disappear after SIGKILL.
_TREE_KILL_CONFIRM_S = 2.0


def _pgid_alive(pgid: int) -> bool:
    """False only when the kernel says the group has no members (ESRCH).

    EPERM means at least one member exists but cannot be signalled, e.g. a
    setuid or credential-changing tool (Darwin and Linux killpg(2) alike), so
    it is never taken as proof the group is gone (PR #1230 review round 2).
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _kill_process_tree(pgid: int | None, *, reap: Callable[[], object] | None = None) -> bool:
    """Stop every process in ``pgid``: SIGTERM, then SIGKILL after a grace period.

    A session's tool descendants (git, pytest, a build) share its process
    group. Signalling only the CLI left them running, still writing the
    worktree, while the stall was reported and retried (PR #1230 review).
    ``reap`` lets the caller reap its own child so a zombie leader does not
    read as a live group member. Returns True only once ESRCH confirms the
    group is gone; False when it is still present (or unsignallable) after
    SIGKILL and the confirmation window, or when ``pgid`` is refused (init,
    non-positive, or this process's own group).
    """
    if not pgid or pgid <= 1 or pgid == os.getpgrp():
        return False
    for sig, wait_s in (
        (signal.SIGTERM, _TREE_KILL_GRACE_S),
        (signal.SIGKILL, _TREE_KILL_CONFIRM_S),
    ):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        deadline = time.monotonic() + wait_s
        while True:
            if reap is not None:
                try:
                    reap()
                except Exception:  # noqa: BLE001 - reaping is best effort
                    pass
            if not _pgid_alive(pgid):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False


def _unconfirmed_note(pgid: int | None) -> str:
    if pgid is None:
        return (
            "the session's process group is unknown, so its exit was not confirmed; "
            "check for leftover processes started in the task's working directory "
            "(e.g. `ps -ef`, `lsof +D <cwd>`) and stop them before retrying this task"
        )
    return (
        f"process group {pgid} was not confirmed gone after SIGTERM and SIGKILL "
        "(a member may be unsignallable, e.g. setuid, or stuck in uninterruptible I/O); "
        f"inspect it with `ps -g {pgid}` and stop it before retrying this task"
    )


def _terminate_child(child: Any) -> str | None:
    """Stop a pexpect child and its whole process group, then close it.

    ``pexpect.spawn`` runs the child in its own session (``forkpty``), so its
    pid is its process-group id. Returns None once the group is confirmed
    gone, else an actionable note for the run's error.
    """
    pid = getattr(child, "pid", None)
    pgid = pid if isinstance(pid, int) and pid > 1 else None
    stopped = _kill_process_tree(pgid, reap=getattr(child, "isalive", None))
    if not stopped:
        try:
            child.terminate(force=True)
        except Exception:  # noqa: BLE001 - best-effort fallback
            pass
    try:
        child.close(force=True)
    except Exception:  # noqa: BLE001 - already gone
        pass
    return None if stopped else _unconfirmed_note(pgid)


def _stopped_result(
    transcript: list[str], *, error: str, status: str, cleanup_note: str | None
) -> dict[str, Any]:
    """A kill-switch/stall/timeout result that says whether cleanup was confirmed."""
    result = _error_result(
        transcript,
        error=error if cleanup_note is None else f"{error}; {cleanup_note}",
        exit_code=-1,
        status=status,
    )
    result["cleanup_confirmed"] = cleanup_note is None
    return result


def _argv(agy_bin: str, model: str | None) -> list[str]:
    argv = [agy_bin]
    if model:
        argv.extend(["--model", model])
    return argv


def _run_with_pexpect(
    *,
    agy_bin: str,
    prompt: str,
    cwd: str,
    env: dict[str, str],
    timeout_s: int,
    kill_check: Callable[[], bool] | None,
    allow_classes: Sequence[str],
    progress_cb: Callable[[str], None] | None,
    model: str | None,
    stall_timeout_s: float = 0,
) -> dict[str, Any]:
    import pexpect

    argv = _argv(agy_bin, model)
    transcript: list[str] = []
    deadline = time.monotonic() + max(1, timeout_s)
    child: Any = None
    progress = _Progress(stall_timeout_s)

    def _append(chunk: str) -> None:
        if not chunk:
            return
        transcript.append(chunk)
        if progress_cb is not None:
            progress_cb(chunk)

    def _run() -> dict[str, Any]:
        nonlocal child
        try:
            child = pexpect.spawn(
                argv[0],
                argv[1:],
                cwd=cwd,
                env=env,
                encoding="utf-8",
                codec_errors="replace",
                timeout=min(30, max(5, timeout_s)),
                dimensions=(40, 160),
                # pexpect's default select() raises once this fd passes 1023.
                use_poll=True,
            )
            child.logfile_read = _ReadTap(progress)
            # Wait for the initial prompt, then submit the task turn.
            idx = child.expect(
                [_PROMPT_RE, pexpect.EOF, pexpect.TIMEOUT], timeout=min(60, timeout_s)
            )
            _append(child.before or "")
            _append(child.after if isinstance(child.after, str) else "")
            if idx != 0:
                return _error_result(
                    transcript,
                    error="agy-interactive: timed out waiting for initial '>' prompt",
                    exit_code=-1,
                    status="timeout" if idx == 2 else "error",
                )

            child.send(_ensure_trailing_newline(prompt))
            submitted = True
            saw_post_submit_output = False
            last_approval_fp: str | None = None
            progress.received()

            while time.monotonic() < deadline:
                if kill_check is not None and kill_check():
                    return _stopped_result(
                        transcript,
                        error="Execution stopped by kill switch",
                        status="killed",
                        cleanup_note=None,
                    )
                if progress.stalled():
                    return _stopped_result(
                        transcript,
                        error=_stall_error(stall_timeout_s),
                        status="timeout",
                        cleanup_note=None,
                    )

                remaining = max(0.1, deadline - time.monotonic())
                try:
                    idx = child.expect(
                        [_PROMPT_RE, pexpect.EOF, pexpect.TIMEOUT],
                        timeout=min(2.0, remaining),
                    )
                except pexpect.ExceptionPexpect as e:
                    return _error_result(
                        transcript,
                        error=f"agy-interactive: pexpect error: {e}",
                        exit_code=-1,
                    )

                before = child.before or ""
                after = child.after if isinstance(child.after, str) else ""
                _append(before)
                if after:
                    _append(after)
                blob = "".join(transcript)

                if detect_agy_teardown(blob):
                    return _error_result(
                        transcript,
                        error=detect_agy_teardown(blob),
                        exit_code=getattr(child, "exitstatus", None) or 0,
                    )

                # Completion / EOF take priority over approval. A buffer that still
                # contains an earlier allowlisted menu must not block /exit once
                # the live turn has returned to '>'.
                if idx == 0 and submitted:
                    # Returned to '>' after the turn — ask for a clean exit.
                    if before.strip() or saw_post_submit_output:
                        child.send("/exit\n")
                        try:
                            child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=min(15, remaining))
                            _append(child.before or "")
                        except pexpect.ExceptionPexpect:
                            pass
                        child.close(force=True)
                        exit_code = child.exitstatus if child.exitstatus is not None else 0
                        text = "".join(transcript)
                        if detect_agy_teardown(text):
                            return _error_result(
                                transcript,
                                error=detect_agy_teardown(text),
                                exit_code=exit_code or 0,
                            )
                        return {
                            "status": "success",
                            "text": text,
                            "tools_used": [],
                            "files_modified": [],
                            "events_count": text.count("\n") + 1,
                            "exit_code": exit_code if exit_code is not None else 0,
                            "error": None,
                            "driver": "pexpect",
                        }
                    saw_post_submit_output = True
                    continue

                if idx == 1:  # EOF without clean /exit
                    exit_code = child.exitstatus if child.exitstatus is not None else -1
                    text = "".join(transcript)
                    teardown = detect_agy_teardown(text)
                    if teardown:
                        return _error_result(transcript, error=teardown, exit_code=exit_code or 0)
                    status = "success" if exit_code == 0 else "error"
                    return {
                        "status": status,
                        "text": text,
                        "tools_used": [],
                        "files_modified": [],
                        "events_count": text.count("\n") + 1,
                        "exit_code": exit_code if exit_code is not None else -1,
                        "error": None
                        if status == "success"
                        else f"agy-interactive exited with code {exit_code}",
                        "driver": "pexpect",
                    }

                # Approval menus are handled on TIMEOUT (or unmatched output): the
                # child is waiting for a keystroke and has not returned to '>'.
                recent = (before + after)[-6000:]
                decision = decide_approval(recent, allow_classes=allow_classes)
                if decision in {"approve", "deny"}:
                    fp = f"{decision}:{extract_approval_command(recent) or recent[-200:]}"
                    if fp != last_approval_fp:
                        last_approval_fp = fp
                        keys = approval_keystroke(decision)
                        if keys:
                            child.send(keys)
                        if decision == "deny":
                            return _error_result(
                                transcript,
                                error=(
                                    f"{AGY_INTERACTIVE_APPROVAL_DENIED_REASON}: refused to "
                                    f"auto-approve non-allowlisted command "
                                    f"{extract_approval_command(recent)!r}"
                                ),
                                exit_code=1,
                            )
                    continue

                if before.strip():
                    saw_post_submit_output = True

            return _stopped_result(
                transcript,
                error=f"Execution timed out after {timeout_s} seconds",
                status="timeout",
                cleanup_note=None,
            )
        except FileNotFoundError as e:
            return _error_result(
                transcript,
                error=f"agy binary not found ({agy_bin!r}): {e}",
                exit_code=-1,
            )

    result: dict[str, Any] | None = None
    try:
        result = _run()
        return result
    finally:
        # Leader exit, EOF, prompt errors and approval denial all leave possible
        # descendants. Every return must preserve the group-cleanup outcome.
        if child is not None:
            try:
                note = _terminate_child(child)
            except Exception:  # noqa: BLE001 - unknown cleanup must fence retry
                note = _unconfirmed_note(getattr(child, "pid", None))
            if result is not None:
                result["cleanup_confirmed"] = note is None
                if note:
                    error = result.get("error")
                    result["error"] = f"{error}; {note}" if error else note


def _tmux_exec_script(cmd: str, env: dict[str, str], *, bearer_file: str | None = None) -> str:
    """``env -i`` script for the tmux pane.

    Allowlisted values stay on the ``env -i`` argv. The bound bearer does not:
    the pane shell reads it from ``bearer_file`` on fd 3, so the token is never
    an argument. ``bearer_file`` is omitted when the child has no bearer.
    """
    public = (
        (key, value)
        for key, value in env.items()
        if key.isidentifier() and key != "UA_AGENT_BEARER_TOKEN"
    )
    assignments = " ".join(shlex_quote(f"{key}={value}") for key, value in public)
    if bearer_file is None or "UA_AGENT_BEARER_TOKEN" not in env:
        # Existing tmux servers retain their own ambient environment. Replace it.
        return f"#!/bin/sh\nexec /usr/bin/env -i {assignments} {cmd}\n"
    inner = (
        'IFS= read -r UA_AGENT_BEARER_TOKEN <&3 || [ -n "$UA_AGENT_BEARER_TOKEN" ] || exit 1\n'
        "export UA_AGENT_BEARER_TOKEN\n"
        'exec "$@"\n'
    )
    return (
        "#!/bin/sh\n"
        f"exec /usr/bin/env -i {assignments} /bin/sh -c {shlex_quote(inner)} _ {cmd} "
        f"3< {shlex_quote(bearer_file)}\n"
    )


def _run_with_tmux(
    *,
    agy_bin: str,
    prompt: str,
    cwd: str,
    env: dict[str, str],
    timeout_s: int,
    kill_check: Callable[[], bool] | None,
    allow_classes: Sequence[str],
    progress_cb: Callable[[str], None] | None,
    model: str | None,
    stall_timeout_s: float = 0,
) -> dict[str, Any]:
    """Fallback when pexpect is unavailable: tmux new-session + send-keys."""
    session = f"ua-agy-i-{os.getpid()}-{int(time.time() * 1000) % 1_000_000}"
    argv = _argv(agy_bin, model)
    cmd = " ".join(shlex_quote(a) for a in argv)
    transcript: list[str] = []
    work_dir: Path | None = None
    socket = ""
    # Every tmux client gets the allowlisted env (task #1948): if the session
    # has ended, any later client (has-session, capture-pane, kill-server)
    # would otherwise start a fresh server at this socket from the daemon's
    # own environment.
    # The server copies this environment. The bound bearer stays in the 0600
    # file the pane reads on fd 3, never in the tmux server.
    tmux_env = _tmux_server_env(env)
    progress = _Progress(stall_timeout_s)

    def _append(chunk: str) -> None:
        if chunk:
            progress.received()
            transcript.append(chunk)
            if progress_cb is not None:
                progress_cb(chunk)

    try:
        # A private tmux server (task #1948): its socket lives in a 0700
        # mkdtemp dir and it is started from the allowlisted child env, so its
        # global environment never holds the daemon's or the user's default
        # server's secrets. The wrapper still execs agy under ``env -i``.
        work_dir = Path(tempfile.mkdtemp(prefix="ua-agy-i-", dir=_tmux_socket_base()))
        socket = str(work_dir / "tmux.sock")
        if len(os.fsencode(socket)) > _MAX_SOCKET_PATH:
            return _error_result(
                transcript,
                error=(
                    f"agy-interactive tmux socket path is {len(os.fsencode(socket))} bytes, "
                    f"over the {_MAX_SOCKET_PATH}-byte Unix socket limit: {socket}"
                ),
                exit_code=-1,
            )
        env_file = work_dir / "env.sh"
        bearer_file = None
        token = env.get("UA_AGENT_BEARER_TOKEN")
        if token is not None:
            # Create at 0600 so the token is never briefly group-readable.
            bearer_path = work_dir / "bearer"
            fd = os.open(bearer_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                # A trailing newline so POSIX ``read`` finds a delimiter.
                # Without it, ``read`` returns non-zero at EOF and the pane
                # exits before ``exec``.
                os.write(fd, (token + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            bearer_file = str(bearer_path)
        env_file.write_text(_tmux_exec_script(cmd, env, bearer_file=bearer_file), encoding="utf-8")
        env_file.chmod(0o700)

        _tmux_new_session(socket, session, cwd, str(env_file), tmux_env)
        # Record the pane's process group while the session certainly exists,
        # so cleanup can still reach and confirm it if a later lookup fails.
        pane_pgid = _tmux_pane_pid(socket, session, tmux_env)

        deadline = time.monotonic() + max(1, timeout_s)
        submitted = False
        last_approval_fp: str | None = None
        last_pane_len = 0
        while time.monotonic() < deadline:
            if kill_check is not None and kill_check():
                return _stopped_result(
                    transcript,
                    error="Execution stopped by kill switch",
                    status="killed",
                    cleanup_note=_tmux_stop(socket, session, tmux_env, known_pgid=pane_pgid),
                )
            if submitted and progress.stalled():
                return _stopped_result(
                    transcript,
                    error=_stall_error(stall_timeout_s),
                    status="timeout",
                    cleanup_note=_tmux_stop(socket, session, tmux_env, known_pgid=pane_pgid),
                )

            pane = _tmux_capture(socket, session, tmux_env)
            if pane and (not transcript or pane != "".join(transcript)):
                # Append only the delta when possible.
                prev = "".join(transcript)
                if pane.startswith(prev):
                    _append(pane[len(prev) :])
                else:
                    transcript.clear()
                    _append(pane)

            blob = "".join(transcript)
            if detect_agy_teardown(blob):
                _tmux_kill_server(socket, tmux_env)
                return _error_result(transcript, error=detect_agy_teardown(blob), exit_code=0)

            if not submitted and detect_interactive_prompt(blob):
                # send-keys with Enter for submission.
                subprocess.run(
                    _tmux_argv(socket, "send-keys", "-t", session, "-l", prompt.rstrip("\n")),
                    check=False,
                    env=tmux_env,
                    capture_output=True,
                    timeout=10,
                )
                subprocess.run(
                    _tmux_argv(socket, "send-keys", "-t", session, "Enter"),
                    check=False,
                    env=tmux_env,
                    capture_output=True,
                    timeout=10,
                )
                submitted = True
                progress.received()
                time.sleep(0.2)
                continue

            # Only re-evaluate when the pane grew so a prior allowlisted
            # approval does not re-fire forever on a static capture.
            if len(blob) > last_pane_len:
                recent = blob[last_pane_len:][-6000:]
                last_pane_len = len(blob)
                decision = decide_approval(recent, allow_classes=allow_classes)
                if decision in {"approve", "deny"}:
                    fp = f"{decision}:{extract_approval_command(recent) or recent[-200:]}"
                    if fp != last_approval_fp:
                        last_approval_fp = fp
                        keys = approval_keystroke(decision) or ""
                        for ch in keys.rstrip("\n"):
                            subprocess.run(
                                _tmux_argv(socket, "send-keys", "-t", session, "-l", ch),
                                check=False,
                                env=tmux_env,
                                capture_output=True,
                                timeout=5,
                            )
                        subprocess.run(
                            _tmux_argv(socket, "send-keys", "-t", session, "Enter"),
                            check=False,
                            env=tmux_env,
                            capture_output=True,
                            timeout=5,
                        )
                        if decision == "deny":
                            _tmux_kill_server(socket, tmux_env)
                            return _error_result(
                                transcript,
                                error=(
                                    f"{AGY_INTERACTIVE_APPROVAL_DENIED_REASON}: refused to "
                                    f"auto-approve non-allowlisted command "
                                    f"{extract_approval_command(recent)!r}"
                                ),
                                exit_code=1,
                            )
                    time.sleep(0.2)
                    continue

            if submitted and detect_interactive_prompt(blob):
                subprocess.run(
                    _tmux_argv(socket, "send-keys", "-t", session, "-l", "/exit"),
                    check=False,
                    env=tmux_env,
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    _tmux_argv(socket, "send-keys", "-t", session, "Enter"),
                    check=False,
                    env=tmux_env,
                    capture_output=True,
                    timeout=5,
                )
                # Wait briefly for the session to end.
                for _ in range(20):
                    if not _tmux_alive(socket, session, tmux_env):
                        break
                    time.sleep(0.25)
                _tmux_kill_server(socket, tmux_env)
                text = "".join(transcript)
                if detect_agy_teardown(text):
                    return _error_result(transcript, error=detect_agy_teardown(text), exit_code=0)
                return {
                    "status": "success",
                    "text": text,
                    "tools_used": [],
                    "files_modified": [],
                    "events_count": text.count("\n") + 1,
                    "exit_code": 0,
                    "error": None,
                    "driver": "tmux",
                }

            if not _tmux_alive(socket, session, tmux_env):
                text = "".join(transcript)
                return {
                    "status": "success" if submitted else "error",
                    "text": text,
                    "tools_used": [],
                    "files_modified": [],
                    "events_count": text.count("\n") + 1,
                    "exit_code": 0 if submitted else -1,
                    "error": None if submitted else "agy-interactive tmux session ended early",
                    "driver": "tmux",
                }

            time.sleep(0.25)

        return _stopped_result(
            transcript,
            error=f"Execution timed out after {timeout_s} seconds",
            status="timeout",
            cleanup_note=_tmux_stop(socket, session, tmux_env, known_pgid=pane_pgid),
        )
    except FileNotFoundError as e:
        return _error_result(
            transcript,
            error=f"agy binary not found ({agy_bin!r}): {e}",
            exit_code=-1,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        return _error_result(
            transcript,
            error=f"agy-interactive tmux session failed to start (exit {e.returncode}): {detail}",
            exit_code=-1,
        )
    finally:
        if socket:
            _tmux_kill_server(socket, tmux_env)
        if work_dir is not None:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


# sun_path is 108 bytes on Linux and 104 on macOS, NUL included.
_MAX_SOCKET_PATH = 100


def _tmux_socket_base() -> str | None:
    """Short parent for the private socket dir; ``None`` means the default.

    ``tempfile`` honours ``TMPDIR``, which on macOS (``/var/folders/...``) or
    a nested workspace can push the socket past the Unix path limit. ``/tmp``
    exists on both supported platforms; the 0700 ``mkdtemp`` dir under it
    keeps the socket private.
    """
    base = "/tmp"
    if os.path.isdir(base) and os.access(base, os.W_OK | os.X_OK):
        return base
    return None


def _tmux_server_env(env: dict[str, str]) -> dict[str, str]:
    """Allowlisted env for a tmux client, without the bound bearer."""
    server = dict(env)
    server.pop("UA_AGENT_BEARER_TOKEN", None)
    return server


def _tmux_argv(socket: str, *args: str) -> list[str]:
    """argv for a tmux client bound to the private server at ``socket``."""
    return ["tmux", "-S", socket, *args]


def _tmux_new_session(
    socket: str, session: str, cwd: str, command: str, env: dict[str, str]
) -> None:
    """Start the private server and its one session from ``env`` alone.

    ``env=`` makes the client, and so the server it forks (whose global
    environment is copied from the client), carry only the allowlisted child
    env. ``-f /dev/null`` keeps the user's tmux.conf (``update-environment``,
    ``set-environment``, ``run-shell``) out of it.
    """
    subprocess.run(
        _tmux_argv(
            socket, "-f", os.devnull, "new-session", "-d", "-s", session, "-c", cwd, command
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=_tmux_server_env(env),
    )


def _tmux_alive(socket: str, session: str, env: dict[str, str]) -> bool:
    res = subprocess.run(
        _tmux_argv(socket, "has-session", "-t", session),
        capture_output=True,
        timeout=5,
        env=dict(env),
    )
    return res.returncode == 0


def _tmux_capture(socket: str, session: str, env: dict[str, str]) -> str:
    res = subprocess.run(
        _tmux_argv(socket, "capture-pane", "-pt", session, "-S", "-"),
        capture_output=True,
        text=True,
        timeout=5,
        env=dict(env),
    )
    return res.stdout if res.returncode == 0 else ""


def _tmux_pane_pid(socket: str, session: str, env: dict[str, str]) -> int | None:
    """The pane's process id; tmux starts each pane in its own session."""
    try:
        res = subprocess.run(
            _tmux_argv(socket, "display-message", "-p", "-t", session, "#{pane_pid}"),
            capture_output=True,
            text=True,
            timeout=5,
            env=dict(env),
        )
        if res.returncode != 0:
            return None
        pid = int(str(res.stdout).strip())
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _tmux_stop(
    socket: str, session: str, env: dict[str, str], *, known_pgid: int | None = None
) -> str | None:
    """Kill the pane's process group, tool descendants included, then the server.

    Killing the server only HUPs the pane; a descendant that ignores SIGHUP
    would keep running (PR #1230 review). The pane pid is looked up now and
    falls back to ``known_pgid``, recorded when the session started. Returns
    None only when ESRCH confirms that group is gone. A failed lookup with no
    recorded group proves nothing (inherited-group descendants can outlive
    the pane), so the server is still killed but the cleanup is reported
    unconfirmed with an actionable note (PR #1230 review round 3).
    """
    pane_pid = _tmux_pane_pid(socket, session, env) or known_pgid
    stopped = False
    if pane_pid is not None:
        # The pane process leads its own session, so its pid is its group id.
        stopped = _kill_process_tree(pane_pid)
    _tmux_kill_server(socket, env)
    return None if stopped else _unconfirmed_note(pane_pid)


def _tmux_kill_server(socket: str, env: dict[str, str]) -> None:
    subprocess.run(
        _tmux_argv(socket, "kill-server"),
        capture_output=True,
        timeout=5,
        check=False,
        env=dict(env),
    )


def detect_agy_teardown(text: str) -> str | None:
    """Reuse the print-mode teardown detector so a bad fixture cannot lie."""
    # Local import avoids a circular import at module load: backends imports us.
    from .backends import detect_agy_background_teardown

    return detect_agy_background_teardown(text)


def _error_result(
    transcript: list[str],
    *,
    error: str | None,
    exit_code: int,
    status: str = "error",
) -> dict[str, Any]:
    text = "".join(transcript)
    return {
        "status": status,
        "text": text,
        "tools_used": [],
        "files_modified": [],
        "events_count": text.count("\n") + 1 if text else 0,
        "exit_code": exit_code,
        "error": error,
    }
