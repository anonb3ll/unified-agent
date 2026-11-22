"""Execution backends for the Active Executor daemon (task #196).

The daemon owns the *lifecycle* — delegation claim, task claim, worktree
isolation, lock renewal, failure/backoff policy, and the finish-to-review
transition. A backend owns only the *execution*: turn a prompt into a run
inside an already-prepared working directory and report what happened.

That split is why adding Codex is additive rather than invasive. Before this
module the daemon called ``pi_run_task`` inline, so "which agent executes" and
"how the task lifecycle works" were the same decision. They are now separate,
and every backend answers the same contract:

    {"status": "success"|"error"|"timeout"|"killed",
     "text": str, "tools_used": list[str], "files_modified": list[str],
     "events_count": int, "exit_code": int, "error": str | None}

That shape is Pi's existing return value, kept verbatim so the daemon's
failure policy, attempt ceiling, and registry bookkeeping did not have to
change to accommodate a second backend. "killed" was added for task #383: a
backend that honours ``kill_check`` reports it separately from "error" so the
daemon can treat an operator-requested stop as terminal rather than retrying
it through the normal failure/backoff policy.
"""

from __future__ import annotations

import codecs
import io
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .. import config, harness_billing, run_records, trust_presets
from .child_env import build_executor_child_env

logger = logging.getLogger("unified_agent.executor")

# agy print-mode drain (task #1158): when the root agent backgrounds work and
# goes idle, the CLI waits a short grace (~5s) then kills those tasks —
# "root agent idle; waiting up to 5s for 1 background task(s)" — and may still
# emit a SUCCESS result. stream-json keeps the pipe parseable but does not
# defeat this; our spawn/monitor path has no idle killer of its own. Detect
# the CLI's own teardown signatures so a silent mid-flight kill cannot look
# like a successful attempt.
_AGY_BACKGROUND_TEARDOWN_RE = re.compile(
    r"(?:root agent idle;\s*waiting up to\s+\S+\s+for\s+\d+\s+background task"
    r"|terminating\s+\d+\s+background task\(s\)(?:\s+on exit)?"
    r"|Print mode:\s*drain grace period expired"
    r"|All your background tasks have been stopped)",
    re.IGNORECASE,
)
AGY_BACKGROUND_TEARDOWN_REASON = "agy_background_teardown"
# Auto-resume ceiling after a dirty-worktree teardown (task #1172). Two resumes
# means initial invoke + up to two ``--continue`` re-invokes.
AGY_MAX_TEARDOWN_RESUMES = 2
AGY_TEARDOWN_RESUME_EXHAUSTED_REASON = "agy_teardown_resume_exhausted"
AGY_RESUME_PROMPT = (
    "Continue the previous turn and finish any incomplete work. "
    "Prefer foreground commands; do not background long-running tasks."
)


def detect_agy_background_teardown(*chunks: str | None) -> str | None:
    """Return a named error when agy print-mode drained background work.

    The trigger is inside the agy CLI (not ``run_popen_with_kill_check``): print
    mode ends the turn after a short idle wait while child commands are still
    running. Prompt-level "foreground only" instructions do not prevent it.

    This is the *fast path* only (task #1247): it matches four literal
    phrases the vendor CLI happened to print as of the #1158 investigation.
    It is not the safety net — see
    ``detect_agy_unconfirmed_background_launch`` for the vendor-text-
    independent detector that backs it up, added after task #1143 found a
    live agy release (1.2.7) that drops backgrounded work with zero
    teardown-related text on stdout or stderr, defeating this regex entirely.
    """
    blob = "\n".join(c for c in chunks if c)
    if not blob or not _AGY_BACKGROUND_TEARDOWN_RE.search(blob):
        return None
    return (
        f"{AGY_BACKGROUND_TEARDOWN_REASON}: agy print-mode drained background "
        "task(s) after idle grace — session torn down mid-flight"
    )


def _command_line_backgrounds(command_line: str) -> bool:
    """Return whether a shell command contains an async-list ``&`` operator.

    This is intentionally a small lexical scan, not a shell interpreter. It
    handles quoting, escaping, comments, boolean ``&&``, and common fd
    redirections. A syntactically valid background launch is enough to make
    the result ambiguous: agy exposes no correlated child id and no terminal
    success event that this layer can verify, so a later generic ``wait`` or
    ``command_status`` call must not restore success.
    """
    in_single_quote = False
    in_double_quote = False
    escaped = False
    in_comment = False

    for index, char in enumerate(command_line):
        if in_comment:
            if char == "\n":
                in_comment = False
            continue

        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if in_single_quote or in_double_quote:
            continue

        if char == "#" and (
            index == 0 or command_line[index - 1].isspace() or command_line[index - 1] in ";|&()<>"
        ):
            in_comment = True
            continue
        if char != "&":
            continue

        previous = command_line[index - 1] if index else ""
        following = command_line[index + 1] if index + 1 < len(command_line) else ""
        if previous == "&" or following == "&":  # boolean AND
            continue
        if previous in "<>" or following == ">":  # fd / &> redirection
            continue
        return True

    return False


def detect_agy_unconfirmed_background_launch(stdout: str) -> str | None:
    """Return a named error when a run backgrounded shell work that agy's own
    protocol never confirmed landed (task #1247's structural safety net).

    Unlike ``detect_agy_background_teardown``, this reads the *tool-call
    requests* the session itself made — JSON shaped by our own
    ``parse_agy_stream_json`` contract, not vendor log prose — so it cannot
    be defeated by the vendor CLI changing what it prints, the way #1158's
    four-phrase regex was defeated by agy 1.2.7 (task #1143: confirmed live,
    5/5 reproductions, zero teardown-related text on stdout or stderr for a
    backgrounded write that never landed on disk).

    A `run_command` tool call containing an unquoted shell "&" hands work off
    to keep running after the tool call itself reports DONE. agy's stream-json
    protocol has no correlated child id or terminal success event that this
    layer can verify. Therefore the launch remains unresolved even if the
    session later calls a generic wait/status tool: ambiguity defaults to
    not-success, independent of whatever agy's own status field or log text
    says this release.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "step_update":
            continue
        step = event.get("step_update") or {}
        if step.get("step_type") != "tool" or step.get("state") != "DONE":
            continue

        tool_name = step.get("tool_name")
        if tool_name != "run_command":
            continue

        params = (step.get("tool_info") or {}).get("parameters") or {}
        command_line = params.get("CommandLine") or params.get("command") or ""
        if isinstance(command_line, str) and _command_line_backgrounds(command_line):
            return (
                f"{AGY_BACKGROUND_TEARDOWN_REASON}: run backgrounded a shell command "
                "and the executor has no correlated child completion proving it "
                "landed — treating the result as unresolved rather than trusting "
                "agy's own reported status"
            )

    return None


def worktree_has_uncommitted_changes(cwd: str) -> bool:
    """True when ``git status --porcelain`` is non-empty in ``cwd``.

    Used by AgyBackend auto-resume (task #1172): a dirty tree after teardown
    means work is incomplete and ``--continue`` may finish it. A clean tree
    must never trigger resume — that is a completed delivery, not a fumble.
    Non-git directories and git failures return False (fail closed: no resume).
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


_RESUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def _checked_resume_id(value: str) -> str:
    """A harness session id bound for argv: never empty, never a flag (#1741)."""
    text = str(value)
    if not _RESUME_ID_RE.fullmatch(text):
        raise ValueError("resume session id is malformed")
    return text


class KillSwitchTriggered(Exception):
    """Raised by `run_popen_with_kill_check` when `kill_check()` returned True.

    Carries whatever the child had written before it was terminated, mirroring
    `subprocess.TimeoutExpired`'s `.stdout`/`.stderr` so callers can reuse the
    same partial-output handling they already have for timeouts.
    """

    def __init__(self, stdout: str, stderr: str):
        super().__init__("kill switch triggered")
        self.stdout = stdout
        self.stderr = stderr


class RunStalled(subprocess.TimeoutExpired):
    """Raised by `run_popen_with_kill_check` when a live child went silent.

    Task #1916: no new stdout/stderr bytes for `stall_timeout_s`. It subclasses
    `TimeoutExpired` so every backend's existing timeout handler reports it
    (status ``timeout``), and the daemon retries it with backoff.
    """

    def __init__(self, cmd, stall_timeout_s: float, output: str, stderr: str):
        super().__init__(cmd, stall_timeout_s, output=output, stderr=stderr)
        self.stall_timeout_s = stall_timeout_s

    def __str__(self) -> str:
        return f"Execution stalled: no output for {self.stall_timeout_s:g} seconds"


def timeout_error(exc: subprocess.TimeoutExpired, timeout_s: int) -> str:
    """Operator-facing error for a timed-out or stalled child."""
    if isinstance(exc, RunStalled):
        return str(exc)
    return f"Execution timed out after {timeout_s} seconds"


def _drain(stream, into: list[str], output_received: Callable[[], None]) -> None:
    """Read available pipe chunks, preserving subprocess's text decoding.

    Runs on its own thread so a chatty child (large JSONL output) cannot
    deadlock the poll loop by filling the OS pipe buffer while the main
    thread is busy waiting on `Popen.wait(timeout=...)` instead of reading.
    Receipt activity is independent of line endings or completed characters;
    a flushed partial JSONL record still means the child is making progress.
    """
    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder(stream.encoding)(errors=stream.errors), translate=True
    )
    try:
        while chunk := stream.buffer.read1(65_536):
            output_received()
            text = decoder.decode(chunk)
            if text:
                into.append(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            into.append(tail)
    finally:
        stream.close()


def run_popen_with_kill_check(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_s: int,
    stdin: int = subprocess.DEVNULL,
    kill_check: Callable[[], bool] | None = None,
    poll_interval_s: float = 1.0,
    stall_timeout_s: float | None = None,
) -> subprocess.CompletedProcess:
    """`subprocess.run`'s contract, but interruptible (task #383).

    Every backend used to call `subprocess.run(..., timeout=timeout_s)`,
    which blocks until the child exits or the full timeout elapses — an
    operator kill request arriving mid-run had no way to reach an
    already-dispatched subprocess and had to wait out the remainder of
    `timeout_s` (bounded by the budget system, but not instant). This
    launches the same child via `Popen` and polls `kill_check()` (when
    given) every `poll_interval_s`, calling `.terminate()` the moment it
    returns True instead of waiting for the timeout.

    Raises `subprocess.TimeoutExpired` on ordinary timeout — same exception,
    same `.stdout`/`.stderr` partial-output shape as `subprocess.run`, so
    existing `except subprocess.TimeoutExpired` call sites need no changes.
    Raises `KillSwitchTriggered` when `kill_check()` fired instead.

    Stall detection (task #1916): received output is the run's progress signal.
    When no new stdout/stderr bytes arrive for `stall_timeout_s` (default
    `config.executor_stall_timeout_s()`, ``0`` disables) the child is killed
    and `RunStalled`, a `TimeoutExpired`, is raised.
    """
    if stall_timeout_s is None:
        stall_timeout_s = config.executor_stall_timeout_s()
    from ..hosted_execution import refuse_hosted_execution

    refuse_hosted_execution("executor")
    proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=stdin,
        cwd=cwd,
        env=env,
        start_new_session=os.name == "posix",
    )

    def stop_tree(sig: int) -> None:
        # Each harness owns a process group, including its tool/build children.
        # Killing only the CLI left those children running into the retry.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()

    start = time.monotonic()
    last_progress_at = start
    progress_lock = threading.Lock()

    def output_received() -> None:
        nonlocal last_progress_at
        with progress_lock:
            last_progress_at = time.monotonic()

    out_lines: list[str] = []
    err_lines: list[str] = []
    out_thread = threading.Thread(
        target=_drain, args=(proc.stdout, out_lines, output_received), daemon=True
    )
    err_thread = threading.Thread(
        target=_drain, args=(proc.stderr, err_lines, output_received), daemon=True
    )
    out_thread.start()
    err_thread.start()

    killed = False
    returncode: int | None = None
    try:
        while True:
            try:
                returncode = proc.wait(timeout=poll_interval_s)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                with progress_lock:
                    stalled_for = now - last_progress_at
                if stall_timeout_s and stalled_for >= stall_timeout_s:
                    stop_tree(signal.SIGKILL)
                    proc.wait()
                    out_thread.join(timeout=2)
                    err_thread.join(timeout=2)
                    raise RunStalled(
                        argv,
                        stall_timeout_s,
                        output="".join(out_lines),
                        stderr="".join(err_lines),
                    ) from None
                if now - start >= timeout_s:
                    stop_tree(signal.SIGKILL)
                    proc.wait()
                    out_thread.join(timeout=2)
                    err_thread.join(timeout=2)
                    raise subprocess.TimeoutExpired(
                        argv,
                        timeout_s,
                        output="".join(out_lines),
                        stderr="".join(err_lines),
                    ) from None
                if kill_check is not None and kill_check():
                    killed = True
                    stop_tree(signal.SIGTERM)
                    try:
                        returncode = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        stop_tree(signal.SIGKILL)
                        returncode = proc.wait()
                    stop_tree(signal.SIGKILL)  # reap tools even if the CLI honored SIGTERM
                    break
    finally:
        out_thread.join(timeout=2)
        err_thread.join(timeout=2)

    if killed:
        raise KillSwitchTriggered("".join(out_lines), "".join(err_lines))
    return subprocess.CompletedProcess(argv, returncode, "".join(out_lines), "".join(err_lines))


# `codex exec -s/--sandbox` accepts exactly these, verified against
# codex-cli 0.145.0 `codex exec --help`.
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# Deliberately NOT danger-full-access. Codex 0.145.0 auto-cancels MCP tool
# calls under read-only/workspace-write, and the tempting "fix" is to hand it
# full access so its unified-agent calls go through. That trade is refused
# here: the daemon drives the whole task lifecycle over its own authenticated
# hub client, so a child that cannot call MCP tools loses nothing it needs.
# See _resolve_codex_sandbox for the gate on raising this.
DEFAULT_CODEX_SANDBOX = "workspace-write"

DEFAULT_CODEX_BIN = "codex"
DEFAULT_CODEX_MODEL = "gpt-6-luna"

CHILD_CACHE_ENV = {
    "UV_CACHE_DIR": "uv",
    "RUFF_CACHE_DIR": "ruff",
}

# Cap for a supervised command's output log (task #2552). Counted over the whole
# file, so a re-run appending to the same path cannot grow it past the cap.
COMMAND_LOG_MAX_ENV = "UA_NODE_COMMAND_LOG_MAX_BYTES"
DEFAULT_COMMAND_LOG_MAX_BYTES = 50 * 1024 * 1024


def command_log_max_bytes() -> int:
    """The log cap in bytes; an unset or malformed value means the default."""
    raw = os.environ.get(COMMAND_LOG_MAX_ENV, "").strip()
    if not raw:
        return DEFAULT_COMMAND_LOG_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the default", COMMAND_LOG_MAX_ENV, raw)
        return DEFAULT_COMMAND_LOG_MAX_BYTES
    return value if value > 0 else DEFAULT_COMMAND_LOG_MAX_BYTES


class CommandBackend:
    """A supervised argv command on a ua-node (task #2413). Not a harness.

    Runs ``argv`` (never a shell string) in its own process group, with stdout
    and stderr appended to ``log_path``, so a long test or build keeps its
    output on disk and outlives the agent session that started it. No stall
    kill: a quiet build is normal. ``kill_check`` and ``timeout_s`` still stop
    the whole group. Returns the executor result shape plus ``exit_code``.

    The log is capped (``UA_NODE_COMMAND_LOG_MAX_BYTES``, default 50 MiB,
    counted over the whole file including earlier appends): past the cap the
    child's output is drained and discarded, one truncation marker is written,
    and the result carries ``truncated`` (task #2552). A detached relay owns
    the pipe and log until every inherited writer closes stdout. It survives
    runner death and normal return with background descendants; ``truncated``
    is the snapshot at primary-command completion, not a future guarantee.
    """

    def __init__(self, argv: list[str], log_path: str, *, poll_interval_s: float = 1.0):
        self.argv = list(argv)
        self.log_path = log_path
        self.poll_interval_s = poll_interval_s

    def run(
        self,
        *,
        cwd: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        from ..hosted_execution import refuse_hosted_execution

        refuse_hosted_execution("executor")
        cap = command_log_max_bytes()
        truncated = False
        relay_error = None
        log_capture = None
        log_path = str(Path(self.log_path).absolute())
        fd, state_path = tempfile.mkstemp(
            prefix=".command-log-", suffix=".json", dir=Path(log_path).parent
        )
        os.close(fd)
        relay = subprocess.Popen(  # noqa: S603 - fixed isolated helper, no shell
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("command_log.py")),
                log_path,
                state_path,
                str(cap),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
            env={},  # the stdlib-only copier needs no hub credentials
        )

        def relay_state() -> dict[str, Any]:
            try:
                state = json.loads(Path(state_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            if not isinstance(state, dict) or not all(
                isinstance(state.get(key), bool) for key in ("ready", "done", "truncated")
            ):
                return {}
            return state

        command_started = False
        try:
            # Do not start a command until its independent log sink is ready.
            deadline = time.monotonic() + 5
            while not (state := relay_state()).get("ready"):
                if relay.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("command log relay failed to start")
                time.sleep(0.01)
            if state.get("error"):
                raise OSError(f"command log relay: {state['error']}")
            assert relay.stdin is not None
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                    self.argv,
                    stdout=relay.stdin,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=cwd,
                    start_new_session=os.name == "posix",
                )
                command_started = True
            finally:
                relay.stdin.close()  # only the command/descendants retain writers

            def stop_tree(sig: int) -> None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()

            def finish() -> None:
                nonlocal truncated, relay_error, log_capture
                # Ordinary commands drain to EOF before their receipt. A
                # descendant may keep stdout open indefinitely: let the relay
                # keep logging independently instead of closing its log.
                try:
                    relay.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                # Sample the exit before reading state: the relay publishes
                # ``done`` before it exits, so an exited relay's state is final.
                # Polling after the read would call a relay that finished in
                # between dead (task #2552 review).
                relay_exit = relay.poll()
                state = relay_state()
                truncated = bool(state.get("truncated"))
                relay_error = state.get("error")
                unknown = not state or (not state.get("done") and relay_exit is not None)
                if unknown or (relay_exit not in (None, 0) and not relay_error):
                    relay_error = "command log relay failed; output may be incomplete"
                capture_state = "pending"
                if unknown:
                    capture_state = "unknown"
                elif relay_error:
                    capture_state = "error"
                elif state["done"]:
                    capture_state = "complete"
                log_capture = {
                    "state": capture_state,
                    "truncated": state.get("truncated"),
                    "error": relay_error,
                    "state_path": state_path,
                }

            start = time.monotonic()
            while True:
                try:
                    code = proc.wait(timeout=self.poll_interval_s)
                    break
                except subprocess.TimeoutExpired:
                    if kill_check is not None and kill_check():
                        stop_tree(signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            stop_tree(signal.SIGKILL)
                            proc.wait()
                        finish()
                        return {
                            "status": "killed",
                            "exit_code": proc.returncode,
                            "log_path": self.log_path,
                            "truncated": truncated,
                            "log_capture": log_capture,
                        }
                    if time.monotonic() - start >= timeout_s:
                        stop_tree(signal.SIGKILL)
                        proc.wait()
                        finish()
                        return {
                            "status": "error",
                            "error": f"timeout after {timeout_s}s",
                            "exit_code": proc.returncode,
                            "log_path": self.log_path,
                            "truncated": truncated,
                            "log_capture": log_capture,
                        }
            finish()
            return {
                "status": "success" if code == 0 and not relay_error else "error",
                "error": relay_error or (None if code == 0 else f"exit code {code}"),
                "exit_code": code,
                "log_path": self.log_path,
                "truncated": truncated,
                "log_capture": log_capture,
                "text": f"exit code {code}; log {self.log_path}",
            }
        finally:
            if relay.stdin is not None and not relay.stdin.closed:
                relay.stdin.close()
            if not command_started:
                # A refused/failed launch has no legitimate output writers.
                # Closing stdin cannot unblock an open() stuck before ready.
                try:
                    relay.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait()
            # Reap a descendant-held relay when EOF eventually arrives. This
            # thread owns only waitpid, never the command's output lifetime.
            elif relay.poll() is None:
                threading.Thread(target=relay.wait, name="command-log-reaper", daemon=True).start()


class ExecutionBackend(Protocol):
    """One way of executing a delegated task."""

    name: str

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Execute `prompt` in `cwd`, returning the contract documented above.

        `kill_check` (task #383), when given, is polled during execution; a
        backend that honours it should stop the child and return
        `status="killed"` the moment it returns True rather than waiting out
        `timeout_s`. Optional and backward compatible — a backend that
        ignores it simply stays bounded by the timeout only, exactly as
        before this parameter existed.

        `model` (task #1153), when given, overrides the backend's configured
        default for this run only. Backends that cannot pass a model flag
        still accept the parameter for contract uniformity and record it
        upstream via the daemon.
        """
        ...


def _add_child_cache_env(env: dict[str, str]) -> None:
    cache_root = (
        Path(env.get("TMPDIR") or env.get("TEMP") or env.get("TMP") or tempfile.gettempdir())
        / "unified-agent-executor-cache"
    )
    for key, name in CHILD_CACHE_ENV.items():
        path = cache_root / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)


def _child_env(agent_label: str, backend: str | None = None) -> dict[str, str]:
    """Allowlisted child environment for one native backend (task #1910).

    Built by ``executor.child_env.build_executor_child_env``: shared process
    plumbing, the backend's own config locations, operator passthrough, and a
    bearer bound to ``agent_label``. Nothing else from the daemon's environment
    reaches the child. Tool caches are then pointed at writable directories.
    """
    env = build_executor_child_env(backend, agent_label)
    _add_child_cache_env(env)
    return env


class PiBackend:
    """Pi Headless RPC — the original executor path, behaviour unchanged.

    This was a pass-through on purpose for a long time: Desktop_agent3 has been
    running this code for weeks, and the refactor was not meant to become a
    rewrite of the path that already works. `kill_check` (task #383) is now
    wired through to `pi_run_task` (task #1525, PR #866 review: "mid-run
    cancellation misses Pi executions") — a watcher thread polls it every
    ``poll_interval_s`` while Pi's RPC subprocess is running and kills it the
    moment it fires, the same contract `run_popen_with_kill_check` gives
    every other backend, adapted for Pi's own stdin/stdout RPC protocol
    rather than a plain `Popen`/`communicate` call.
    """

    name = "pi"
    # registry.start_session kind, preserved so existing session rows and any
    # dashboards keying on "pi_run" keep working.
    session_kind = "pi_run"

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        from ..tools import pi_runner as pi_runner_tool

        return pi_runner_tool.pi_run_task(
            prompt=prompt,
            cwd=cwd,
            model=model,
            session_dir=session_dir,
            session_id=session_id,
            agent_label=agent_label,
            timeout_s=timeout_s,
            kill_check=kill_check,
        )


def _resolve_codex_sandbox() -> str:
    """The sandbox posture for `codex exec`, with full access double-gated.

    Raises ValueError rather than silently downgrading, because a typo'd
    posture that quietly becomes the default is how a sandbox stops meaning
    anything. The daemon surfaces this at startup, not mid-task.
    """
    raw = (os.environ.get("UA_CODEX_SANDBOX") or DEFAULT_CODEX_SANDBOX).strip()
    if raw not in CODEX_SANDBOX_MODES:
        raise ValueError(
            f"UA_CODEX_SANDBOX={raw!r} is not a valid codex sandbox mode; "
            f"expected one of {', '.join(CODEX_SANDBOX_MODES)}"
        )
    if raw == "danger-full-access" and os.environ.get("UA_CODEX_ALLOW_FULL_ACCESS") != "1":
        raise ValueError(
            "refusing to run codex with --sandbox danger-full-access: that removes "
            "the sandbox for every model-generated shell command. It is available "
            "only with the explicit second opt-in UA_CODEX_ALLOW_FULL_ACCESS=1, "
            "which is a security decision for the operator, not a default. The "
            "daemon does not need it: it drives the task lifecycle over its own "
            "hub client, so Codex never has to call MCP tools itself."
        )
    return raw


def _codex_worktree_git_add_dir(cwd_abs: str) -> str | None:
    cwd = Path(cwd_abs).resolve()
    git_common_dir = _git_common_dir(cwd) or _git_common_dir_from_file(cwd)
    if git_common_dir is None:
        logger.warning(
            "codex backend could not resolve git common dir for cwd=%s; running without --add-dir",
            cwd,
        )
        return None

    try:
        git_common_dir.relative_to(cwd)
    except ValueError:
        return str(git_common_dir)
    return None


def _git_common_dir(cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None

    raw = proc.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _git_common_dir_from_file(cwd: Path) -> Path | None:
    git_file = cwd / ".git"
    try:
        raw = git_file.read_text().strip()
    except OSError:
        return None
    if not raw.startswith("gitdir:"):
        return None

    git_dir = Path(raw.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = git_file.parent / git_dir
    git_dir = git_dir.resolve()

    parts = git_dir.parts
    if "worktrees" not in parts:
        return None
    return Path(*parts[: parts.index("worktrees")]).resolve()


class CodexBackend:
    """OpenAI Codex CLI via `codex exec --json`, run as Desktop_Agent2.

    Uses subscription (ChatGPT) auth out of the Codex CLI's own credential
    store — no API key is introduced by this backend.
    """

    name = "codex"
    session_kind = "codex_run"

    def __init__(
        self,
        codex_bin: str | None = None,
        model: str | None = None,
        sandbox: str | None = None,
    ):
        self.codex_bin = codex_bin or os.environ.get("UA_CODEX_BIN") or DEFAULT_CODEX_BIN
        self.model = model or os.environ.get("UA_CODEX_MODEL") or DEFAULT_CODEX_MODEL
        # Resolved eagerly so a bad posture fails at daemon startup rather than
        # on the first delegated task, hours later and mid-claim.
        self.sandbox = sandbox or _resolve_codex_sandbox()
        if self.sandbox == "danger-full-access":
            logger.warning(
                "codex backend running with --sandbox danger-full-access "
                "(UA_CODEX_ALLOW_FULL_ACCESS=1): model-generated shell commands "
                "are NOT sandboxed"
            )

    def build_command(
        self,
        prompt: str,
        cwd_abs: str,
        *,
        model: str | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        """The argv for one non-interactive Codex run.

        ``resume_session_id`` continues an existing Codex session through the
        CLI's own subcommand, ``codex exec [OPTIONS] resume <SESSION_ID>
        <PROMPT>`` (verified against codex-cli 0.159.0 ``--help``; the parent
        ``exec`` options parse before the subcommand). Task #1741.

        The prompt goes in argv rather than stdin, and stdin is closed by the
        caller. Codex reads instructions from stdin when it is a pipe — an
        inherited or piped stdin makes `codex exec` block on a read that never
        completes, which in a daemon looks exactly like a hang.
        """
        use_model = model if model is not None else self.model
        argv = [
            self.codex_bin,
            "exec",
            "--json",
            "--model",
            use_model,
            "--sandbox",
            self.sandbox,
            "--cd",
            cwd_abs,
        ]
        add_dir = _codex_worktree_git_add_dir(cwd_abs)
        if add_dir is not None:
            argv.extend(["--add-dir", add_dir])
        if resume_session_id is not None:
            argv.extend(["resume", _checked_resume_id(resume_session_id)])
        from ..dispatch.routing import codex_prompt

        # ``--`` ends options for ``codex exec`` and ``codex exec resume`` alike,
        # so a prompt starting with ``-`` is never parsed as a flag.
        argv.extend(["--", codex_prompt(prompt)])
        return argv

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]:
        # The daemon hands over a workspace-relative path ('.worktrees/task-N'
        # or '.'); codex --cd needs a real directory.
        trust_presets.require_native_allowed(agent_label)
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        use_model = model if model is not None else self.model

        argv = self.build_command(
            prompt, cwd_abs, model=use_model, resume_session_id=resume_session_id
        )
        logger.info(
            "codex backend: %s (model=%s sandbox=%s cwd=%s)",
            session_id,
            use_model,
            self.sandbox,
            cwd_abs,
        )

        env = self._child_env(agent_label)
        try:
            proc = run_popen_with_kill_check(
                argv,
                cwd=cwd_abs,
                env=env,
                timeout_s=timeout_s,
                # See build_command: never inherit a stdin codex might read.
                stdin=subprocess.DEVNULL,
                kill_check=kill_check,
            )
        except subprocess.TimeoutExpired as e:
            partial = (
                e.stdout
                if isinstance(e.stdout, str)
                else (e.stdout or b"").decode("utf-8", "replace")
            )
            parsed = parse_codex_jsonl(partial)
            return _with_billing(
                {
                    "status": "timeout",
                    "text": parsed["text"],
                    "tools_used": parsed["tools_used"],
                    "files_modified": parsed["files_modified"],
                    "events_count": parsed["events_count"],
                    "exit_code": -1,
                    "error": timeout_error(e, timeout_s),
                    "usage": parsed["usage"],
                },
                "codex",
                self.codex_bin,
                env,
                cwd=cwd_abs,
                thread_id=None if resume_session_id else parsed.get("thread_id"),
            )
        except KillSwitchTriggered as e:
            parsed = parse_codex_jsonl(e.stdout)
            return _with_billing(
                {
                    "status": "killed",
                    "text": parsed["text"],
                    "tools_used": parsed["tools_used"],
                    "files_modified": parsed["files_modified"],
                    "events_count": parsed["events_count"],
                    "exit_code": -1,
                    "error": "Execution stopped by kill switch",
                    "usage": parsed["usage"],
                },
                "codex",
                self.codex_bin,
                env,
                cwd=cwd_abs,
                thread_id=None if resume_session_id else parsed.get("thread_id"),
            )
        except FileNotFoundError as e:
            return {
                "status": "error",
                "text": "",
                "tools_used": [],
                "files_modified": [],
                "events_count": 0,
                "exit_code": -1,
                "error": f"codex binary not found ({self.codex_bin!r}): {e}",
            }

        parsed = parse_codex_jsonl(proc.stdout or "")
        error = parsed["error"]
        if proc.returncode != 0 and not error:
            error = (proc.stderr or "").strip() or f"codex exited with code {proc.returncode}"

        return _with_billing(
            {
                "status": "error" if (proc.returncode != 0 or error) else "success",
                "text": parsed["text"],
                "tools_used": parsed["tools_used"],
                "files_modified": parsed["files_modified"],
                "events_count": parsed["events_count"],
                "exit_code": proc.returncode,
                "error": error,
                "usage": parsed["usage"],
            },
            "codex",
            self.codex_bin,
            env,
            cwd=cwd_abs,
            thread_id=None if resume_session_id else parsed.get("thread_id"),
        )

    @staticmethod
    def _child_env(agent_label: str) -> dict[str, str]:
        """Environment for the Codex child.

        `UA_AGENT_LABEL` is set so anything the child runs that consults it
        attributes work to the executing agent rather than inheriting the
        daemon's. The ambient `UA_AGENT_BEARER_TOKEN` is always dropped first
        — it belongs to whichever agent spawned this daemon — and the child is
        then bound to the requested agent's registry bearer; when no registry
        token resolves, an executor unit may fall back to its own
        `UA_EXECUTOR_TOKEN` only when `UA_EXECUTOR_AGENT` matches the requested
        label exactly, and otherwise the child runs with no bearer. Neither
        `UA_AGENT_TOKENS` nor `UA_EXECUTOR_TOKEN` is passed through raw. A
        child that still reaches the hub through its own
        `~/.codex/config.toml` token remains Codex's configuration. Everything
        else comes from the allowlist in `executor/child_env.py` (task #1910).
        """
        return _child_env(agent_label, "codex")


# --- harness usage (task #1917) ----------------------------------------------
#
# Each parser below also returns ``usage``: what the harness itself reported
# spending, normalized to one shape, or None when it reported nothing.
#
#   provider, model                str | None
#   input_tokens                   every prompt token processed, cached or not
#   output_tokens                  completion tokens
#   cache_read_tokens              the cached part of input_tokens, when known
#   cache_write_tokens             prompt tokens written to a cache, when known
#   cost_usd                       the harness's own USD figure, else None
#
# A missing or malformed number is None, never 0: an unknown cost must stay
# unknown so budgets keep holding the run's reserve for it.

HARNESS_USAGE_FIELDS = (
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)
_USAGE_COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# The subset of harness usage that ``run_usage`` accepts (run_records.USAGE_FIELDS).
_RUN_USAGE_FIELDS = ("provider", "model", "input_tokens", "output_tokens", "cost_usd")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _sum_counts(*values: int | None) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _harness_usage(
    provider: str | None,
    model: str | None = None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any] | None:
    """One normalized usage dict, or None when nothing was measured."""
    measured = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
    if all(v is None for v in measured):
        return None
    return {
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
    }


def add_harness_usage(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """Sum two invocations' usage (an auto-resumed run is several invocations)."""
    if not a or not b:
        return a or b
    cost = (
        None
        if a.get("cost_usd") is None or b.get("cost_usd") is None
        else (a["cost_usd"] + b["cost_usd"])
    )
    return _harness_usage(
        a.get("provider") or b.get("provider"),
        b.get("model") or a.get("model"),
        **{k: _sum_counts(a.get(k), b.get(k)) for k in _USAGE_COUNTS},
        cost_usd=cost,
    )


def to_run_usage(
    harness: Any, *, model: str | None = None, billing: str | None = None
) -> dict[str, Any] | None:
    """Harness usage as the ``run_usage`` a heartbeat or finish accepts.

    Keeps only the fields ``run_records.validate_usage`` allows (cache counts
    stay in the backend result), fills ``model`` from the daemon's resolved
    run model when the harness did not name one, adds ``billing`` when the
    backend observed it (task #1837), and returns None when the harness
    measured neither tokens nor cost.
    """
    if not isinstance(harness, dict):
        return None
    out = {k: harness.get(k) for k in _RUN_USAGE_FIELDS}
    out["input_tokens"] = _count(out["input_tokens"])
    out["output_tokens"] = _count(out["output_tokens"])
    out["cost_usd"] = _usd(out["cost_usd"])
    if all(out[k] is None for k in ("input_tokens", "output_tokens", "cost_usd")):
        return None
    if not isinstance(out["model"], str) or not out["model"]:
        out["model"] = model
    if not isinstance(out["provider"], str) or not out["provider"]:
        out["provider"] = None
    if billing in run_records.BILLING_VALUES:
        out["billing"] = billing
    return {k: v for k, v in out.items() if v is not None}


def _with_billing(
    result: dict[str, Any],
    harness: str,
    binary: str,
    env: dict[str, str],
    *,
    cwd: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Add the child's observed ``billing`` to a result that measured usage.

    Task #1837: ``harness_billing.probe`` asks the CLI, under the child's own
    environment and working directory (project settings can pick another
    credential), whether it is on a subscription login or an API key. The
    run's own evidence must agree before a subscription is granted:

    * Claude Code: the run's ``init`` event names ``apiKeySource``; only
      ``none`` (the OAuth login) supports a subscription.
    * Codex: the run's rollout under the child's ``CODEX_HOME`` (found by
      ``thread_id``) must record ``model_provider = "openai"``. A resumed run
      passes no ``thread_id``: Codex keeps the original ``session_meta`` on
      resume, so it is no evidence for the resumed turns.

    The probe runs only when there is usage to classify; None stays absent.
    """
    usage = result.get("usage")
    if not usage:
        return result
    session_provider = None
    if harness == "codex":
        home = env.get("CODEX_HOME") or (
            str(Path(env["HOME"]) / ".codex") if env.get("HOME") else None
        )
        session_provider = (
            harness_billing.codex_session_provider(thread_id, codex_home=Path(home))
            if home
            else None
        )
    billing = harness_billing.probe(
        harness, binary=binary, env=env, cwd=cwd, session_provider=session_provider
    )
    if (
        harness == "claude"
        and billing == "subscription"
        and not (isinstance(usage, dict) and usage.get("api_key_source") == "none")
    ):
        billing = None
    if billing is not None:
        result["billing"] = billing
    return result


# Only the end of a failed run's output is read for a limit message: the
# harness prints it last, and earlier prose may merely mention one.
_CAPACITY_TEXT_TAIL = 400


def capacity_from_failure(text: Any) -> dict[str, Any] | None:
    """A ``run_usage.capacity`` signal when a harness failure is a usage limit.

    Task #1841: the run's finish records ``capacity_ceiling`` instead of a
    lease variant once this reaches the hub. Only a plan usage ceiling counts;
    a transient rate limit ("rate limit reached") does not.
    """
    from ..quota_observe import is_plan_usage_limit

    if not isinstance(text, str) or not text.strip():
        return None
    if not is_plan_usage_limit(text):
        return None
    return {"limit_reached": True, "source": "executor", "detail": text.strip()[:200]}


def capacity_from_run_result(res: Any) -> dict[str, Any] | None:
    """The capacity signal of an unsuccessful backend result, if any.

    The error and the output are read independently: a generic error such as
    ``claude produced no result event`` can sit beside output whose last lines
    carry the limit message.
    """
    if not isinstance(res, dict) or res.get("status") == "success":
        return None
    hit = capacity_from_failure(res.get("error"))
    if hit is None:
        text = res.get("text")
        if isinstance(text, str):
            hit = capacity_from_failure(text.strip()[-_CAPACITY_TEXT_TAIL:])
    return hit


def _codex_turn_usage(raw: Any) -> dict[str, Any] | None:
    """``turn.completed.usage``: OpenAI convention, cached input inside input."""
    if not isinstance(raw, dict):
        return None
    return _harness_usage(
        "openai",
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_read_tokens=_count(raw.get("cached_input_tokens")),
    )


def _claude_usage(raw: Any, model: str | None, cost: Any = None) -> dict[str, Any] | None:
    """Anthropic usage: ``input_tokens`` excludes cache reads and writes."""
    if not isinstance(raw, dict):
        raw = {}
    uncached = _count(raw.get("input_tokens"))
    read = _count(raw.get("cache_read_input_tokens"))
    write = _count(raw.get("cache_creation_input_tokens"))
    return _harness_usage(
        "anthropic",
        model,
        input_tokens=_sum_counts(uncached, read, write),
        output_tokens=_count(raw.get("output_tokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
        cost_usd=_usd(cost),
    )


def parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    """Fold a `codex exec --json` event stream into the backend contract.

    Event vocabulary verified empirically against codex-cli 0.145.0:
      thread.started / turn.started / item.started
      item.completed with item.type in {agent_message, command_execution, file_change}
      turn.completed / turn.failed

    ``turn.completed`` carries ``usage`` (``input_tokens``, ``output_tokens``,
    and ``cached_input_tokens`` where the CLI reports it); turns are summed
    into ``usage`` (task #1917). Codex reports no USD cost.
    """
    text_chunks: list[str] = []
    tools_used: list[str] = []
    files_modified: list[str] = []
    events_count = 0
    error: str | None = None
    saw_turn = False
    usage: dict[str, Any] | None = None
    thread_id: str | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            # codex prints human-readable preamble lines on stdout too.
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        events_count += 1
        etype = event.get("type")

        if etype == "thread.started":
            if isinstance(event.get("thread_id"), str) and thread_id is None:
                thread_id = event["thread_id"]
            continue

        if etype == "turn.completed":
            saw_turn = True
            usage = add_harness_usage(usage, _codex_turn_usage(event.get("usage")))
            continue

        if etype in ("turn.failed", "error"):
            saw_turn = True
            err = event.get("error") or event.get("message") or {}
            if isinstance(err, dict):
                err = err.get("message") or json.dumps(err)
            error = str(err) if err else "codex reported a failed turn"
            continue

        if etype != "item.completed":
            continue

        item = event.get("item") or {}
        itype = item.get("type")

        if itype == "agent_message":
            if item.get("text"):
                text_chunks.append(item["text"])
        elif itype == "command_execution":
            cmd = (item.get("command") or "").strip()
            if cmd:
                tools_used.append(f"shell:{cmd}")
        elif itype == "file_change":
            for change in item.get("changes") or []:
                path = change.get("path") if isinstance(change, dict) else None
                if path and path not in files_modified:
                    files_modified.append(path)
        elif itype and ("tool" in itype or itype.endswith("_call")):
            # Forward-compat: MCP/tool item types this version did not emit
            # still land in tools_used rather than vanishing.
            tools_used.append(itype)

    if not saw_turn and not error:
        error = "codex produced no turn.completed event"

    return {
        "text": "\n".join(text_chunks),
        "tools_used": tools_used,
        "files_modified": files_modified,
        "events_count": events_count,
        "error": error,
        "usage": usage,
        "thread_id": thread_id,
    }


DEFAULT_AGY_BIN = "agy"


def _resolve_agy_skip_permissions() -> bool:
    raw = (os.environ.get("UA_AGY_SKIP_PERMISSIONS") or "0").strip()
    return raw == "1"


class AgyBackend:
    """Antigravity CLI (`agy`) via `agy -p --output-format stream-json`, run as Desktop_agent3.

    Uses subscription (Antigravity) auth out of the agy CLI's own credential
    store — no API key is introduced by this backend. Replaces the prior
    default of routing Desktop_agent3 dispatch through PiBackend, which in fact
    ran a ChatGPT/Codex-subscription-backed Pi CLI wearing the agy label
    rather than Antigravity itself (task #318).

    Argv shape is deliberate (task #1158): ``-p <prompt>`` must precede
    ``--output-format stream-json``. Putting ``-p`` after ``--output-format``,
    or using ``agy --continue -p"…"`` without stream-json, exits 2 with a help
    dump. stream-json is always passed so the executor can parse events; it
    does **not** defeat agy's print-mode background drain (see
    ``detect_agy_background_teardown``).

    Permission posture: agy's default `permission_mode` is "request-review",
    which blocks on a human approving risky tool calls (e.g. file writes) —
    fatal for a headless daemon with nobody watching. `--dangerously-skip-
    permissions` auto-approves every tool call instead, at the cost of
    removing that guardrail entirely. Off by default, gated behind the
    explicit opt-in UA_AGY_SKIP_PERMISSIONS=1, mirroring CodexBackend's
    danger-full-access gate: a security decision for the operator, not a
    default. Without it, a delegated task that needs to write a file will
    most likely time out waiting for an approval nobody can give — the
    daemon's existing timeout/backoff policy handles that like any other
    timeout, but it is worth knowing the mode exists before enabling it live.

    Skip-permissions also needs a verified sandbox posture (task #1948,
    ``dispatch.harness_sandbox``): the flag rides with ``--sandbox`` after a
    host sandbox probe passes, and ``run()`` refuses the launch otherwise.
    """

    name = "agy"
    session_kind = "agy_run"

    def __init__(
        self,
        agy_bin: str | None = None,
        model: str | None = None,
        skip_permissions: bool | None = None,
    ):
        self.agy_bin = agy_bin or os.environ.get("UA_AGY_BIN") or DEFAULT_AGY_BIN
        # Unlike Codex, no forced default model: agy already spans several
        # model families (Gemini, Claude, gpt-oss) under one subscription, so
        # picking one as "the" default would be an uninformed guess. Only
        # override when the operator says so.
        self.model = model or os.environ.get("UA_AGY_MODEL") or None
        self.skip_permissions = (
            skip_permissions if skip_permissions is not None else _resolve_agy_skip_permissions()
        )
        if self.skip_permissions:
            logger.warning(
                "agy backend running with --dangerously-skip-permissions "
                "(UA_AGY_SKIP_PERMISSIONS=1): tool-call approval is NOT requested"
            )

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None = None,
        sandbox_posture: Any = None,
    ) -> list[str]:
        """The argv for one non-interactive agy run.

        Unlike Codex, agy has no `--cd` flag — the working directory comes
        from the subprocess's `cwd` kwarg, set in `run()`. The prompt goes in
        argv (via `-p`) rather than stdin, and stdin is closed by the caller:
        an inherited/piped stdin produced agy's own `"error": "timeout
        waiting for response"` in manual testing even after the underlying
        turn had already completed successfully — the same stdin-read hazard
        CodexBackend already guards against.
        """
        use_model = model if model is not None else self.model
        argv = [self.agy_bin, "-p", prompt, "--output-format", "stream-json"]
        if use_model:
            argv.extend(["--model", use_model])
        argv.extend(self._permission_argv(sandbox_posture))
        return argv

    def _permission_argv(self, sandbox_posture: Any) -> list[str]:
        """``--sandbox --dangerously-skip-permissions`` or nothing (task #1948).

        Skip-permissions needs a verified sandbox posture; without one this
        raises ``HarnessSandboxUnavailable`` rather than run agy unsandboxed.
        """
        if not self.skip_permissions:
            return []
        from ..dispatch import harness_sandbox

        native = harness_sandbox.require(
            "agy", sandbox_posture, flags="--dangerously-skip-permissions"
        )
        return [*native, "--dangerously-skip-permissions"]

    def build_continue_command(
        self, *, model: str | None = None, sandbox_posture: Any = None
    ) -> list[str]:
        """Argv for a teardown auto-resume (task #1172).

        ``--continue`` resumes the most recent conversation in this cwd;
        ``-p`` keeps the turn non-interactive; ``stream-json`` stays required
        (``agy --continue -p"…"`` without it exits 2 with a help dump).
        """
        use_model = model if model is not None else self.model
        argv = [
            self.agy_bin,
            "--continue",
            "-p",
            AGY_RESUME_PROMPT,
            "--output-format",
            "stream-json",
        ]
        if use_model:
            argv.extend(["--model", use_model])
        argv.extend(self._permission_argv(sandbox_posture))
        return argv

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        use_model = model if model is not None else self.model
        child_env = self._child_env(agent_label)

        resumes_used = 0
        usage_so_far: dict[str, Any] | None = None
        sandbox_posture = (
            _resolve_harness_sandbox("agy", cwd_abs, child_env) if self.skip_permissions else None
        )
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        try:
            argv = self.build_command(prompt, model=use_model, sandbox_posture=sandbox_posture)
        except HarnessSandboxUnavailable as exc:
            return _sandbox_refused_result(str(exc))
        logger.info(
            "agy backend: %s (model=%s skip_permissions=%s cwd=%s)",
            session_id,
            use_model,
            self.skip_permissions,
            cwd_abs,
        )

        while True:
            result = self._invoke_once(
                argv=argv,
                cwd_abs=cwd_abs,
                env=child_env,
                timeout_s=timeout_s,
                kill_check=kill_check,
            )
            result["resume_count"] = resumes_used
            # An auto-resumed run spends across every invocation (task #1917).
            usage_so_far = add_harness_usage(usage_so_far, result.get("usage"))
            result["usage"] = usage_so_far

            # Timeout / kill / missing-binary: do not auto-resume.
            if result["status"] in {"timeout", "killed"} or result.get("_no_resume"):
                result.pop("_no_resume", None)
                return result

            teardown = result.get("error") and AGY_BACKGROUND_TEARDOWN_REASON in (
                result.get("error") or ""
            )
            if not teardown:
                return result

            # #1158 semantics preserved: teardown is an error. Auto-resume only
            # when the worktree is dirty (incomplete work survived the kill).
            if not worktree_has_uncommitted_changes(cwd_abs):
                return result

            if resumes_used >= AGY_MAX_TEARDOWN_RESUMES:
                result["error"] = (
                    f"{AGY_TEARDOWN_RESUME_EXHAUSTED_REASON}: agy print-mode still "
                    f"drained background task(s) after {resumes_used} resume(s) "
                    f"with a dirty worktree — giving up (resume_count={resumes_used})"
                )
                result["status"] = "error"
                return result

            resumes_used += 1
            argv = self.build_continue_command(model=use_model, sandbox_posture=sandbox_posture)
            logger.info(
                "agy backend auto-resume %s/%s after teardown (dirty worktree): %s",
                resumes_used,
                AGY_MAX_TEARDOWN_RESUMES,
                session_id,
            )

    def _invoke_once(
        self,
        *,
        argv: list[str],
        cwd_abs: str,
        env: dict[str, str],
        timeout_s: int,
        kill_check: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        try:
            proc = run_popen_with_kill_check(
                argv,
                cwd=cwd_abs,
                env=env,
                timeout_s=timeout_s,
                # See build_command: never inherit a stdin agy might read.
                stdin=subprocess.DEVNULL,
                kill_check=kill_check,
            )
        except subprocess.TimeoutExpired as e:
            partial = (
                e.stdout
                if isinstance(e.stdout, str)
                else (e.stdout or b"").decode("utf-8", "replace")
            )
            parsed = parse_agy_stream_json(partial)
            error = timeout_error(e, timeout_s)
            return {
                "status": "timeout",
                "text": parsed["text"],
                "tools_used": parsed["tools_used"],
                "files_modified": parsed["files_modified"],
                "events_count": parsed["events_count"],
                "exit_code": -1,
                "error": error,
                "usage": parsed["usage"],
            }
        except KillSwitchTriggered as e:
            parsed = parse_agy_stream_json(e.stdout)
            error = "Execution stopped by kill switch"
            return {
                "status": "killed",
                "text": parsed["text"],
                "tools_used": parsed["tools_used"],
                "files_modified": parsed["files_modified"],
                "events_count": parsed["events_count"],
                "exit_code": -1,
                "error": error,
                "usage": parsed["usage"],
            }
        except FileNotFoundError as e:
            return {
                "status": "error",
                "text": "",
                "tools_used": [],
                "files_modified": [],
                "events_count": 0,
                "exit_code": -1,
                "error": f"agy binary not found ({self.agy_bin!r}): {e}",
                "_no_resume": True,
            }

        parsed = parse_agy_stream_json(proc.stdout or "")
        error = parsed["error"]
        # Fast path first (literal vendor phrases, #1158), then the
        # vendor-text-independent structural safety net (#1247): a
        # backgrounded command the session never confirmed. Either one
        # overrides a clean-looking SUCCESS from the CLI.
        teardown = detect_agy_background_teardown(
            proc.stdout, proc.stderr
        ) or detect_agy_unconfirmed_background_launch(proc.stdout or "")
        if teardown:
            # Prefer the teardown reason even when the CLI stamped SUCCESS —
            # that is the live failure mode (#1112/#1116/#1158/#1143).
            error = teardown
        if proc.returncode != 0 and not error:
            error = (proc.stderr or "").strip() or f"agy exited with code {proc.returncode}"

        return {
            "status": "error" if (proc.returncode != 0 or error) else "success",
            "text": parsed["text"],
            "tools_used": parsed["tools_used"],
            "files_modified": parsed["files_modified"],
            "events_count": parsed["events_count"],
            "exit_code": proc.returncode,
            "error": error,
            "usage": parsed["usage"],
        }

    @staticmethod
    def _child_env(agent_label: str) -> dict[str, str]:
        """Environment for the agy child. See CodexBackend._child_env — same
        registry-first binding with the same-label `UA_EXECUTOR_TOKEN`
        fallback, and the ambient bearer never inherited. If the child reaches
        the hub through its own agy config, that remains its configuration.
        """
        return _child_env(agent_label, "agy")


class AgyInteractiveBackend:
    """PTY-driven interactive agy (task #1177) — teardown fix, layer 3.

    Opt-in via ``UA_EXECUTOR_BACKEND=agy-interactive``. Print-mode
    :class:`AgyBackend` remains the default for ``Desktop_agent3``; #1172
    auto-resume and #1158 teardown detection stay on that path unchanged.

    Drives ``agy`` over a PTY (pexpect preferred; tmux fallback), submits the
    task prompt with an explicit trailing newline, streams the transcript into
    the progress ``text``, auto-approves only the #1146 allowlisted command
    classes (git / pytest / ruff / gh), and ends with ``/exit`` when the ``>``
    prompt returns. Unknown or destructive commands fail closed.
    """

    name = "agy-interactive"
    session_kind = "agy_interactive_run"

    def __init__(
        self,
        agy_bin: str | None = None,
        model: str | None = None,
        *,
        settings_path: str | Path | None = None,
        allow_classes: tuple[str, ...] | None = None,
    ):
        from . import agy_interactive as ai

        self.agy_bin = agy_bin or os.environ.get("UA_AGY_BIN") or DEFAULT_AGY_BIN
        self.model = model or os.environ.get("UA_AGY_MODEL") or None
        self.settings_path = (
            Path(settings_path) if settings_path is not None else ai.resolve_agy_settings_path()
        )
        self.allow_classes = allow_classes or ai.AGY_INTERACTIVE_ALLOW_CLASSES

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir  # interactive session state lives in the CLI, not here
        from . import agy_interactive as ai

        cwd_abs = str((config.workspace_root() / cwd).resolve())
        use_model = model if model is not None else self.model
        child_env = _child_env(agent_label, self.name)
        progress: list[str] = []

        def _on_chunk(chunk: str) -> None:
            progress.append(chunk)

        logger.info(
            "agy-interactive backend: %s (model=%s cwd=%s settings=%s)",
            session_id,
            use_model,
            cwd_abs,
            self.settings_path,
        )
        result = ai.run_interactive_session(
            agy_bin=self.agy_bin,
            prompt=prompt,
            cwd=cwd_abs,
            env=child_env,
            timeout_s=timeout_s,
            kill_check=kill_check,
            allow_classes=self.allow_classes,
            settings_path=self.settings_path,
            progress_cb=_on_chunk,
            model=use_model,
        )
        # Prefer the live-streamed transcript when the driver returned empty text.
        if not result.get("text") and progress:
            result["text"] = "".join(progress)
        return result


# Tool names whose parameters may carry a written-file path. Best-effort
# only: agy's stream-json has no dedicated file-modification event like
# Codex's `item.completed`/`file_change`, so files_modified is inferred from
# write-tool call parameters instead. The key list is unverified against a
# real write call (only `list_dir`'s `DirectoryPath` parameter has been
# observed live) — extend it if a real run shows a different parameter name.
_AGY_WRITE_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
}
_AGY_PATH_PARAM_KEYS = ("TargetFile", "FilePath", "file_path", "path", "Path")


def parse_agy_stream_json(stdout: str) -> dict[str, Any]:
    """Fold an `agy -p --output-format stream-json` event stream into the backend contract.

    Event vocabulary verified empirically against a live agy CLI session
    (2026-08-10): `init`, `step_update` (step_type in {user_input, tool,
    agent_response, ...}, state in {ACTIVE, DONE}), and a terminal `result`
    event carrying `{status: SUCCESS|ERROR, response, error, usage}`.
    """
    tools_used: list[str] = []
    files_modified: list[str] = []
    events_count = 0
    result_text = ""
    result_error: str | None = None
    saw_result = False
    usage: dict[str, Any] | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        events_count += 1
        etype = event.get("event")

        if etype == "result":
            saw_result = True
            result = event.get("result") or {}
            result_text = result.get("response") or ""
            raw_usage = result.get("usage")
            if isinstance(raw_usage, dict):
                # agy names no provider: it runs Gemini and Claude models alike.
                usage = _harness_usage(
                    None,
                    input_tokens=_count(raw_usage.get("input_tokens")),
                    output_tokens=_count(raw_usage.get("output_tokens")),
                )
            status = result.get("status")
            if status != "SUCCESS":
                result_error = result.get("error") or f"agy reported status {status!r}"
            continue

        if etype != "step_update":
            continue

        step = event.get("step_update") or {}
        if step.get("step_type") != "tool" or step.get("state") != "DONE":
            continue

        tool_name = step.get("tool_name")
        if tool_name:
            tools_used.append(tool_name)

        if tool_name in _AGY_WRITE_TOOLS:
            params = (step.get("tool_info") or {}).get("parameters") or {}
            for key in _AGY_PATH_PARAM_KEYS:
                path = params.get(key)
                if path and path not in files_modified:
                    files_modified.append(path)
                    break

    if not saw_result and not result_error:
        result_error = "agy produced no result event"

    return {
        "text": result_text,
        "tools_used": tools_used,
        "files_modified": files_modified,
        "events_count": events_count,
        "error": result_error,
        "usage": usage,
    }


DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_CURSOR_BIN = "agent-with-hub"
DEFAULT_HERMES_BIN = "hermes"
DEFAULT_COPILOT_BIN = "copilot-with-hub"
DEFAULT_DEVIN_BIN = "devin-with-hub"

_FAMILY_TO_BACKEND = {
    "Claude": "claude",
    "Codex": "codex",
    "agy": "agy",
    "Cursor": "cursor",
    "Hermes": "hermes",
    "Copilot": "copilot",
}

# Mac hub executors use lowercase ``server_<token>`` labels (see the roster in
# AGENTS.md). Derived from _FAMILY_TO_BACKEND rather than restated, so a new
# family cannot be added to one map and forgotten in the other.
_SERVER_TOKEN_TO_BACKEND = {
    family.lower(): backend for family, backend in _FAMILY_TO_BACKEND.items()
}


def backend_name_for_label(agent_label: str) -> str:
    """Native headless backend for a roster label. Never infers Pi."""
    if agent_label in {"pi", "pi_worker"}:
        return "pi"
    if agent_label == "Desktop_Agent7":
        return "devin"
    if "_" not in (agent_label or ""):
        raise ValueError(f"no native headless backend for {agent_label!r}")
    host, segment = agent_label.split("_", 1)
    canonical = config.SEGMENT_TO_FAMILY.get(segment)
    if canonical is None and host == "server":
        # server_* is the only host whose family token is lowercase; keep the
        # relaxed lookup scoped to it so "Desktop_agent1" stays unresolvable.
        canonical = config.SEGMENT_TO_FAMILY_LOWER.get(segment)
    family = canonical or segment
    name = _FAMILY_TO_BACKEND.get(family)
    if name is None and host == "server":
        name = _SERVER_TOKEN_TO_BACKEND.get(family)
    if name is None:
        raise ValueError(f"no native headless backend for {agent_label!r}")
    return name


def _skip_permissions(env_name: str) -> bool:
    return (os.environ.get(env_name) or "0").strip() == "1"


def _resolve_harness_sandbox(
    cli_backend: str, cwd: str, env: dict[str, str], binary: str | None = None
):
    """Sandbox posture for a blanket-permission harness launch (task #1948)."""
    from ..dispatch import harness_sandbox

    return harness_sandbox.resolve(cli_backend, cwd=cwd, env=env, binary=binary)


def _sandbox_refused_result(error: str) -> dict[str, Any]:
    """Run result for a launch refused before spawn for want of a sandbox."""
    logger.error("%s", error)
    return {
        "status": "error",
        "text": "",
        "tools_used": [],
        "files_modified": [],
        "events_count": 0,
        "exit_code": -1,
        "error": error,
    }


def _run_native_cli(
    *,
    argv: list[str],
    cwd_abs: str,
    env: dict[str, str],
    timeout_s: int,
    kill_check: Callable[[], bool] | None,
    parse_stdout: Callable[[str], dict[str, Any]],
    bin_name: str,
) -> dict[str, Any]:
    try:
        proc = run_popen_with_kill_check(
            argv,
            cwd=cwd_abs,
            env=env,
            timeout_s=timeout_s,
            stdin=subprocess.DEVNULL,
            kill_check=kill_check,
        )
    except subprocess.TimeoutExpired as e:
        partial = (
            e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        )
        parsed = parse_stdout(partial)
        return {
            "status": "timeout",
            "text": parsed["text"],
            "tools_used": parsed["tools_used"],
            "files_modified": parsed["files_modified"],
            "events_count": parsed["events_count"],
            "exit_code": -1,
            "error": timeout_error(e, timeout_s),
            "usage": parsed.get("usage"),
        }
    except KillSwitchTriggered as e:
        parsed = parse_stdout(e.stdout)
        return {
            "status": "killed",
            "text": parsed["text"],
            "tools_used": parsed["tools_used"],
            "files_modified": parsed["files_modified"],
            "events_count": parsed["events_count"],
            "exit_code": -1,
            "error": "Execution stopped by kill switch",
            "usage": parsed.get("usage"),
        }
    except FileNotFoundError as e:
        return {
            "status": "error",
            "text": "",
            "tools_used": [],
            "files_modified": [],
            "events_count": 0,
            "exit_code": -1,
            "error": f"{bin_name} binary not found ({argv[0]!r}): {e}",
        }

    parsed = parse_stdout(proc.stdout or "")
    error = parsed["error"]
    if proc.returncode != 0 and not error:
        error = (proc.stderr or "").strip() or f"{bin_name} exited with code {proc.returncode}"
    return {
        "status": "error" if (proc.returncode != 0 or error) else "success",
        "text": parsed["text"],
        "tools_used": parsed["tools_used"],
        "files_modified": parsed["files_modified"],
        "events_count": parsed["events_count"],
        "exit_code": proc.returncode,
        "error": error,
        "usage": parsed.get("usage"),
    }


class ClaudeBackend:
    """Claude Code via ``claude -p --output-format stream-json``."""

    name = "claude"
    session_kind = "claude_run"

    def __init__(self, claude_bin: str | None = None, skip_permissions: bool | None = None):
        self.claude_bin = claude_bin or os.environ.get("UA_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN
        self.skip_permissions = (
            skip_permissions
            if skip_permissions is not None
            else _skip_permissions("UA_CLAUDE_SKIP_PERMISSIONS")
        )
        if self.skip_permissions:
            logger.warning(
                "claude backend requested --dangerously-skip-permissions "
                "(UA_CLAUDE_SKIP_PERMISSIONS=1); launch requires a verified sandbox"
            )

    def build_command(
        self,
        prompt: str | None = None,
        *,
        model: str | None = None,
        resume_session_id: str | None = None,
        dispatch_settings_path: str | Path | None = None,
        sandbox_posture: Any = None,
    ) -> list[str]:
        """``claude -p ...``; with ``resume_session_id``, ``--resume <id>``.

        ``-r, --resume [value]`` resumes a conversation by session id (verified
        against Claude Code 2.1.287 ``--help``); without ``--fork-session`` the
        session keeps its id. Task #1741.

        Skip-permissions is attached only with a confirmed fail-closed settings
        file (task #1965). ``dispatch_settings_path`` is that file.
        """
        from ..dispatch.routing import headless_argv

        argv = headless_argv(
            "claude",
            self.claude_bin,
            prompt=prompt,
            skip_permissions=self.skip_permissions,
            model=model,
            dispatch_settings_path=dispatch_settings_path,
            sandbox_posture=sandbox_posture,
        )
        if resume_session_id is not None:
            resume = ["--resume", _checked_resume_id(resume_session_id)]
            if "--" in argv:
                idx = argv.index("--")
                argv[idx:idx] = resume
            else:
                argv.extend(resume)
        return argv

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        from ..dispatch import claude_sandbox, harness_sandbox
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        settings_path = None
        posture = None
        owned: dict[Path, claude_sandbox.ArtifactIdentity] = {}
        if self.skip_permissions:
            try:
                settings_file = claude_sandbox.write_claude_dispatch_settings(
                    session_id, cwd=cwd_abs
                )
                # Identity, not name: a later run reusing this id keeps its file.
                owned = claude_sandbox.snapshot_artifacts([settings_file])
                settings_path = str(settings_file)
            except OSError as exc:
                return _sandbox_refused_result(
                    harness_sandbox.refusal_message(
                        "claude",
                        "--dangerously-skip-permissions",
                        f"could not write the fail-closed settings file ({type(exc).__name__})",
                    )
                )
        try:
            if settings_path is not None:
                posture = harness_sandbox.resolve(
                    "claude", cwd=cwd_abs, settings_path=settings_path
                )
            try:
                argv = self.build_command(
                    prompt,
                    model=model,
                    resume_session_id=resume_session_id,
                    dispatch_settings_path=settings_path,
                    sandbox_posture=posture,
                )
            except HarnessSandboxUnavailable as exc:
                return _sandbox_refused_result(str(exc))
            logger.info("claude backend: %s (model=%s cwd=%s)", session_id, model, cwd_abs)
            env = _child_env(agent_label, self.name)
            result = _run_native_cli(
                argv=argv,
                cwd_abs=cwd_abs,
                env=env,
                timeout_s=timeout_s,
                kill_check=kill_check,
                parse_stdout=parse_claude_stream_json,
                bin_name="claude",
            )
            return _with_billing(result, "claude", self.claude_bin, env, cwd=cwd_abs)
        finally:
            # Task #2191: the run is synchronous, so once it returns (or the
            # host probe refused it) the settings file has no reader left.
            if owned:
                claude_sandbox.discard_run_artifacts(owned)


def parse_claude_stream_json(stdout: str) -> dict[str, Any]:
    """Fold ``claude -p --output-format stream-json`` into the backend contract.

    ``usage`` (task #1917) comes from the terminal ``result`` event:
    ``total_cost_usd`` and ``usage`` (``input_tokens``, ``output_tokens``,
    ``cache_creation_input_tokens``, ``cache_read_input_tokens``). A run cut
    short has no result event; its usage is then the sum of each assistant
    message's ``usage``, counted once per message id, with no cost. The model
    is the ``system``/``init`` event's, else the assistant message's.
    """
    text_chunks: list[str] = []
    tools_used: list[str] = []
    events_count = 0
    error: str | None = None
    saw_result = False
    result_text = ""
    model: str | None = None
    result_usage: dict[str, Any] | None = None
    message_usage: dict[str, Any] = {}
    api_key_source: str | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events_count += 1
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            source = event.get("apiKeySource")
            if isinstance(source, str) and api_key_source is None:
                api_key_source = source
        if etype == "system" and isinstance(event.get("model"), str) and not model:
            model = event["model"]
            continue
        if etype == "result":
            saw_result = True
            result_text = str(event.get("result") or event.get("text") or "")
            if event.get("is_error"):
                error = result_text or "claude reported an error"
            result_usage = _claude_usage(event.get("usage"), None, event.get("total_cost_usd"))
            continue
        if etype == "assistant":
            message = event.get("message") or {}
            if isinstance(message.get("usage"), dict):
                key = str(message.get("id") or f"_anon{events_count}")
                message_usage[key] = message["usage"]
            if isinstance(message.get("model"), str) and not model:
                model = message["model"]
            content = message.get("content") or []
            if isinstance(content, str) and content:
                text_chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        text_chunks.append(str(block["text"]))
                    elif block.get("type") == "tool_use" and block.get("name"):
                        tools_used.append(str(block["name"]))
            continue
        if etype == "tool_use" and event.get("name"):
            tools_used.append(str(event["name"]))

    text = result_text or "\n".join(text_chunks)
    if not saw_result and not error:
        error = "claude produced no result event"
    usage = result_usage
    if usage is None:
        for raw in message_usage.values():
            usage = add_harness_usage(usage, _claude_usage(raw, None))
    if usage is not None:
        usage["model"] = model
        # Which credential the run itself used (``none`` is the OAuth login);
        # billing evidence for _with_billing, dropped by to_run_usage.
        if api_key_source is not None:
            usage["api_key_source"] = api_key_source
    return {
        "text": text,
        "tools_used": tools_used,
        "files_modified": [],
        "events_count": events_count,
        "error": error,
        "usage": usage,
    }


class CursorBackend:
    """Cursor Agent CLI via ``agent -p`` (default binary: agent-with-hub)."""

    name = "cursor"
    session_kind = "cursor_run"

    def __init__(self, cursor_bin: str | None = None, skip_permissions: bool | None = None):
        self.cursor_bin = cursor_bin or os.environ.get("UA_CURSOR_BIN") or DEFAULT_CURSOR_BIN
        self.skip_permissions = (
            skip_permissions
            if skip_permissions is not None
            else _skip_permissions("UA_CURSOR_SKIP_PERMISSIONS")
        )
        if self.skip_permissions:
            logger.warning("cursor backend running with --force (UA_CURSOR_SKIP_PERMISSIONS=1)")

    def build_command(
        self,
        prompt: str | None = None,
        cwd: str | None = None,
        *,
        model: str | None = None,
        sandbox_posture: Any = None,
    ) -> list[str]:
        from ..dispatch.routing import headless_argv

        return headless_argv(
            "cursor",
            self.cursor_bin,
            prompt=prompt,
            cwd=cwd,
            skip_permissions=self.skip_permissions,
            model=model,
            sandbox_posture=sandbox_posture,
        )

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        child_env = _child_env(agent_label, self.name)
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        try:
            argv = self.build_command(
                prompt,
                cwd=cwd_abs,
                model=model,
                sandbox_posture=_resolve_harness_sandbox("cursor", cwd_abs, child_env),
            )
        except HarnessSandboxUnavailable as exc:
            return _sandbox_refused_result(str(exc))
        logger.info("cursor backend: %s (model=%s cwd=%s)", session_id, model, cwd_abs)
        return _run_native_cli(
            argv=argv,
            cwd_abs=cwd_abs,
            env=child_env,
            timeout_s=timeout_s,
            kill_check=kill_check,
            parse_stdout=parse_cursor_stream_json,
            bin_name="cursor",
        )


def parse_cursor_stream_json(stdout: str) -> dict[str, Any]:
    """Best-effort fold of ``agent -p --output-format stream-json``."""
    text_chunks: list[str] = []
    tools_used: list[str] = []
    events_count = 0
    error: str | None = None
    result_text = ""
    saw_json = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_json = True
        events_count += 1
        etype = event.get("type") or event.get("event")
        if etype in {"result", "error"} or event.get("subtype") == "result":
            result_text = str(
                event.get("result") or event.get("text") or event.get("message") or ""
            )
            if etype == "error" or event.get("is_error") or event.get("subtype") == "error":
                error = result_text or "cursor reported an error"
            continue
        name = event.get("name") or event.get("tool") or event.get("tool_name")
        if name and etype in {"tool_call", "tool_use", "tool"}:
            tools_used.append(str(name))
        text = event.get("text") or event.get("content")
        if isinstance(text, str) and text:
            text_chunks.append(text)

    text = result_text or "\n".join(text_chunks)
    if not saw_json:
        text = stdout.strip()
    if not text and not error:
        error = "cursor produced no printable result"
    return {
        "text": text,
        "tools_used": tools_used,
        "files_modified": [],
        "events_count": events_count,
        "error": error,
    }


class CopilotBackend:
    """GitHub Copilot CLI via ``copilot -p`` (default binary: copilot-with-hub)."""

    name = "copilot"
    session_kind = "copilot_run"

    def __init__(self, copilot_bin: str | None = None, skip_permissions: bool | None = None):
        self.copilot_bin = copilot_bin or os.environ.get("UA_COPILOT_BIN") or DEFAULT_COPILOT_BIN
        self.skip_permissions = (
            skip_permissions
            if skip_permissions is not None
            else _skip_permissions("UA_COPILOT_SKIP_PERMISSIONS")
        )
        if self.skip_permissions:
            logger.warning(
                "copilot backend running with --allow-all (UA_COPILOT_SKIP_PERMISSIONS=1)"
            )

    def build_command(
        self,
        prompt: str | None = None,
        cwd: str | None = None,
        *,
        sandbox_posture: Any = None,
    ) -> list[str]:
        from ..dispatch.routing import headless_argv

        return headless_argv(
            "copilot",
            self.copilot_bin,
            prompt=prompt,
            cwd=cwd,
            skip_permissions=self.skip_permissions,
            sandbox_posture=sandbox_posture,
        )

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir, model
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        child_env = _child_env(agent_label, self.name)
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        try:
            argv = self.build_command(
                prompt,
                cwd=cwd_abs,
                sandbox_posture=_resolve_harness_sandbox(
                    "copilot", cwd_abs, child_env, self.copilot_bin
                ),
            )
        except HarnessSandboxUnavailable as exc:
            return _sandbox_refused_result(str(exc))
        logger.info("copilot backend: %s (cwd=%s)", session_id, cwd_abs)
        return _run_native_cli(
            argv=argv,
            cwd_abs=cwd_abs,
            env=child_env,
            timeout_s=timeout_s,
            kill_check=kill_check,
            parse_stdout=parse_copilot_jsonl,
            bin_name="copilot",
        )


def parse_copilot_jsonl(stdout: str) -> dict[str, Any]:
    """Best-effort fold of ``copilot -p --output-format json`` (JSONL)."""
    text_chunks: list[str] = []
    tools_used: list[str] = []
    events_count = 0
    error: str | None = None
    result_text = ""
    saw_json = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_json = True
        events_count += 1
        etype = event.get("type") or event.get("event") or event.get("kind")
        data = event.get("data")
        data_dict = data if isinstance(data, dict) else {}

        if etype == "session.error":
            error = str(
                data_dict.get("message")
                or data_dict.get("content")
                or event.get("message")
                or event.get("text")
                or "copilot reported an error"
            )
            continue

        if etype in {"result", "error", "assistant"} or event.get("subtype") == "result":
            result_text = str(
                event.get("result")
                or event.get("text")
                or event.get("message")
                or event.get("content")
                or data_dict.get("content")
                or data_dict.get("message")
                or ""
            )
            if etype == "error" or event.get("is_error") or event.get("subtype") == "error":
                error = result_text or "copilot reported an error"
            elif isinstance(event.get("message"), dict):
                content = event["message"].get("content")
                if isinstance(content, str) and content:
                    result_text = content
            continue

        if etype == "assistant.message":
            content = data_dict.get("content")
            if isinstance(content, str) and content:
                text_chunks.append(content)
            continue

        name = (
            event.get("name")
            or event.get("tool")
            or event.get("tool_name")
            or data_dict.get("toolName")
        )
        if name and etype in {
            "tool_call",
            "tool_use",
            "tool",
            "tool_execution",
            "tool.execution_start",
        }:
            tools_used.append(str(name))
        text = event.get("text") or event.get("content") or data_dict.get("content")
        if isinstance(text, str) and text:
            text_chunks.append(text)

    text = result_text or "\n".join(text_chunks)
    if not saw_json:
        text = stdout.strip()
    if not text and not error:
        error = "copilot produced no printable result"
    return {
        "text": text,
        "tools_used": tools_used,
        "files_modified": [],
        "events_count": events_count,
        "error": error,
    }


class HermesBackend:
    """Hermes Agent via ``hermes --oneshot`` (true Hermes persona, not Pi)."""

    name = "hermes"
    session_kind = "hermes_run"

    def __init__(self, hermes_bin: str | None = None, skip_permissions: bool | None = None):
        self.hermes_bin = hermes_bin or os.environ.get("UA_HERMES_BIN") or DEFAULT_HERMES_BIN
        self.skip_permissions = (
            skip_permissions
            if skip_permissions is not None
            else _skip_permissions("UA_HERMES_SKIP_PERMISSIONS")
        )
        if self.skip_permissions:
            logger.warning(
                "hermes backend requested --yolo (UA_HERMES_SKIP_PERMISSIONS=1); "
                "launch requires a verified sandbox"
            )

    def build_command(self, prompt: str | None = None, cwd: str | None = None) -> list[str]:
        from ..dispatch.routing import headless_argv

        return headless_argv(
            "hermes",
            self.hermes_bin,
            prompt=prompt,
            cwd=cwd,
            skip_permissions=self.skip_permissions,
        )

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir, model
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        try:
            argv = self.build_command(prompt, cwd=cwd_abs)
        except HarnessSandboxUnavailable as exc:
            return _sandbox_refused_result(str(exc))
        logger.info("hermes backend: %s (cwd=%s)", session_id, cwd_abs)
        return _run_native_cli(
            argv=argv,
            cwd_abs=cwd_abs,
            env=_child_env(agent_label, self.name),
            timeout_s=timeout_s,
            kill_check=kill_check,
            parse_stdout=parse_hermes_oneshot,
            bin_name="hermes",
        )


def parse_hermes_oneshot(stdout: str) -> dict[str, Any]:
    """``hermes --oneshot`` prints only the response on stdout."""
    text = (stdout or "").strip()
    return {
        "text": text,
        "tools_used": [],
        "files_modified": [],
        "events_count": 1 if text else 0,
        "error": None if text else "hermes produced no oneshot output",
    }


def parse_devin_print(stdout: str) -> dict[str, Any]:
    """``devin -p`` prints the response directly on stdout."""
    text = (stdout or "").strip()
    return {
        "text": text,
        "tools_used": [],
        "files_modified": [],
        "events_count": 1 if text else 0,
        "error": None if text else "devin produced no print output",
    }


class DevinBackend:
    """Devin CLI via ``devin --permission-mode ... -p``."""

    name = "devin"
    session_kind = "devin_run"

    def __init__(
        self,
        devin_bin: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
    ):
        from ..dispatch.routing import resolve_devin_permission_mode

        self.devin_bin = devin_bin or os.environ.get("UA_DEVIN_BIN") or DEFAULT_DEVIN_BIN
        self.model = model or os.environ.get("UA_DEVIN_MODEL") or None
        self.permission_mode = resolve_devin_permission_mode(permission_mode)
        if self.permission_mode == "dangerous":
            logger.warning(
                "devin backend requested --permission-mode dangerous "
                "(UA_DEVIN_PERMISSION_MODE=dangerous); launch requires a verified sandbox"
            )

    def build_command(self, prompt: str | None = None, *, model: str | None = None) -> list[str]:
        from ..dispatch.routing import headless_argv

        use_model = model if model is not None else self.model
        return headless_argv(
            "devin",
            self.devin_bin,
            prompt=prompt,
            model=use_model,
            permission_mode=self.permission_mode,
        )

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        trust_presets.require_native_allowed(agent_label)
        del session_dir
        cwd_abs = str((config.workspace_root() / cwd).resolve())
        from ..dispatch.harness_sandbox import HarnessSandboxUnavailable

        try:
            argv = self.build_command(prompt, model=model)
        except HarnessSandboxUnavailable as exc:
            return _sandbox_refused_result(str(exc))
        logger.info("devin backend: %s (model=%s cwd=%s)", session_id, model, cwd_abs)
        return _run_native_cli(
            argv=argv,
            cwd_abs=cwd_abs,
            env=_child_env(agent_label, self.name),
            timeout_s=timeout_s,
            kill_check=kill_check,
            parse_stdout=parse_devin_print,
            bin_name="devin",
        )


_BACKENDS: dict[str, Any] = {
    "pi": PiBackend,
    "codex": CodexBackend,
    "agy": AgyBackend,
    "agy-interactive": AgyInteractiveBackend,
    "claude": ClaudeBackend,
    "cursor": CursorBackend,
    "hermes": HermesBackend,
    "copilot": CopilotBackend,
    "devin": DevinBackend,
}


def get_backend(name: str | None = None, *, agent_label: str | None = None) -> Any:
    """Instantiate the backend for an explicit name or the agent's native CLI.

    ``UA_EXECUTOR_BACKEND`` still wins when set (including the explicit ``pi``
    alias). When it is unset, the label selects the persona's own headless
    CLI — never Pi wearing someone else's name. An unrecognised name raises
    instead of silently running a different agent.
    """
    if name is not None:
        resolved = name.strip().lower()
    else:
        resolved = config.executor_backend().strip().lower()
        if not resolved:
            if not agent_label:
                raise ValueError(
                    "executor backend not specified: set UA_EXECUTOR_BACKEND or pass "
                    "agent_label so the native headless CLI for that persona is used"
                )
            resolved = backend_name_for_label(agent_label)
    backend_cls = _BACKENDS.get(resolved)
    if backend_cls is None:
        raise ValueError(
            f"unknown executor backend {resolved!r}; "
            f"expected one of {', '.join(sorted(_BACKENDS))} (UA_EXECUTOR_BACKEND)"
        )
    return backend_cls()
