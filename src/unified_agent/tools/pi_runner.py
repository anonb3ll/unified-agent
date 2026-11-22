"""Pi coding agent (pi.dev) RPC runner tool.

Runs Pi in headless RPC mode (--mode rpc) over stdin/stdout JSONL records,
aggregating tool calls and model output into structured results.

The subprocess does not inherit the hub environment. Pi is a coding agent
with its own shell, so a copied ``os.environ`` would hand it ``UA_AGENT_TOKENS``
and integration secrets. The child env is an allowlist, the same idea as
``run_cmd``. The steward profile cannot call this tool.

PR #867 also forced a one-shot hub approval before spawn. That gate is not
here: it raised on executor hosts with ``NO_LOCAL_MEMORY_DB`` (the approvals
table lives on the hub) and spent executor retry attempts while a pass was
pending. Its hub-side redesign is task #1524.
"""

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic.json_schema import SkipJsonSchema

from .. import config
from ..mcp_progress import report_progress
from ..safety import resolve_in_workspace

# Metered provider keys are deliberately absent. Pi authenticates against
# the operator's ChatGPT subscription (provider `openai-codex`, OAuth tokens in
# ~/.pi/agent/auth.json). OPENAI_API_KEY and PI_API_KEY are the only provider
# keys copied, and only when already present in the hub environment — key files
# (~/.hermes/.env, sleep-pass.env, gemini.env) are not scraped.
_FORWARDED_API_KEYS = (
    "OPENAI_API_KEY",
    "PI_API_KEY",
    # Executors may route through OpenRouter.
    "OPENROUTER_API_KEY",
)

# Metered keys that must never reach Pi, whatever the allowlists say:
#   - ANTHROPIC_API_KEY: excluded since the tool was written.
#   - GEMINI_API_KEY: excluded 2026-07-29. It was forwarded *and* scraped from
#     key files, so every pi_run_task call billed a real AI Studio key —
#     verified by session logs showing provider `google`, gemini-3.1-pro-preview.
#     ``config.load_gemini_env()`` writes it into the hub's own environment at
#     startup, so it is always present in the parent.
# The allowlist already leaves them out; _build_env strips them again last so a
# future allowlist edit cannot silently re-open the metered path.
_BLOCKED_API_KEYS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)

# The only provider Pi may run on (task #2025): the operator's ChatGPT
# subscription. Pi otherwise takes its provider from ~/.pi/agent/settings.json,
# so a stale settings file could select `google` and bill an API key. Every
# call passes `--provider` and `--model` (Pi's `--provider` requires
# `--model`), so the settings default is never consulted, and a model written
# as `<other provider>/<id>` is refused.
PI_PROVIDER = "openai-codex"
DEFAULT_PI_MODEL = "gpt-5.5"


def _pinned_pi_model(model: str | None) -> str:
    """The model to pass with ``--provider openai-codex``; refuses other providers."""
    value = (model or "").strip() or DEFAULT_PI_MODEL
    if "/" in value:
        provider, _, model_id = value.partition("/")
        if provider != PI_PROVIDER or not model_id.strip():
            raise ToolError(
                f"pi_run_task runs only on provider {PI_PROVIDER!r} (the subscription); "
                f"got a model for provider {provider!r}"
            )
        value = model_id.strip()
    return value


# Inherited only when set. PATH and LANG always have a value (see _build_env).
# UA_AGENT_LABEL is stamped from the verified identity, not from the raw
# caller argument. Nothing else from os.environ is copied.
_OPTIONAL_ENV_KEYS = (
    "HOME",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
)

# An exact npm version is full semver: MAJOR.MINOR.PATCH with an optional
# prerelease and build suffix. Anything shorter floats: npm reads ``pkg@2026``
# or ``pkg@1.2`` as a range (or a dist-tag) and installs whatever it currently
# resolves to, and ``latest``/``next2`` are dist-tags. npm refuses a dist-tag
# that is valid semver, so a full ``x.y.z`` cannot be retargeted.
_SEMVER_NUM = r"(?:0|[1-9][0-9]*)"
_SEMVER_IDENT = r"[0-9A-Za-z-]+"
_EXACT_SEMVER = re.compile(
    rf"{_SEMVER_NUM}\.{_SEMVER_NUM}\.{_SEMVER_NUM}"
    rf"(?:-{_SEMVER_IDENT}(?:\.{_SEMVER_IDENT})*)?"
    rf"(?:\+{_SEMVER_IDENT}(?:\.{_SEMVER_IDENT})*)?"
)

_DEFAULT_AGENT_LABEL = "pi_worker"


def _is_exact_package_spec(spec: str) -> bool:
    """True when ``spec`` pins a full semver version (``name@1.2.3``).

    Scoped names start with ``@``, so the version separator is the last ``@``.
    ``@earendil-works/pi-coding-agent``, ``pkg@latest``, ``pkg@next2``,
    ``pkg@2026`` and ``pkg@1.2`` are not exact.
    """
    name, sep, version = spec.strip().rpartition("@")
    if not sep or not name or not version:
        return False
    return _EXACT_SEMVER.fullmatch(version) is not None


def _require_local_pi_binary(explicit: str) -> str:
    if not os.path.isabs(explicit):
        raise ToolError(
            f"UA_PI_EXECUTABLE must be an absolute path of a local pi binary; refusing {explicit!r}"
        )
    if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
        raise ToolError(f"UA_PI_EXECUTABLE {explicit!r} is not an executable file")
    return explicit


def _package_launcher(package: str) -> list[str]:
    bunx_bin = shutil.which("bunx")
    if bunx_bin:
        return [bunx_bin, package]
    npx_bin = shutil.which("npx")
    if npx_bin:
        # `-y` only installs the already-pinned spec; it is not the floating
        # `@earendil-works/pi-coding-agent` fallback.
        return [npx_bin, "-y", package]
    raise ToolError(
        f"UA_PI_PACKAGE={package!r} is an exact version spec but neither bunx nor npx is on PATH"
    )


def resolve_pi_executable() -> list[str]:
    """Find the command prefix to launch Pi.

    Prefers ``UA_PI_EXECUTABLE`` (absolute local binary) or a ``pi`` already
    on PATH / ``~/.local/bin``. The unpinned ``bunx`` / ``npx -y
    @earendil-works/pi-coding-agent`` fallback is refused. ``UA_PI_PACKAGE``
    is used only when it is an exact version spec (``package@x.y.z``, full
    semver) and no local ``pi`` was found.
    """
    explicit = os.environ.get("UA_PI_EXECUTABLE", "").strip()
    if explicit:
        return [_require_local_pi_binary(explicit)]

    pi_bin = shutil.which("pi") or shutil.which(
        "pi", path=os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
    )
    if pi_bin:
        return [pi_bin]

    local_pi = os.path.expanduser("~/.local/bin/pi")
    if os.path.isfile(local_pi) and os.access(local_pi, os.X_OK):
        return [local_pi]

    package = os.environ.get("UA_PI_PACKAGE", "").strip()
    if package:
        if not _is_exact_package_spec(package):
            raise ToolError(
                "UA_PI_PACKAGE must be an exact version spec (package@x.y.z, full semver), "
                "not a floating tag or range. Refusing the unpinned npx -y / bunx fallback."
            )
        return _package_launcher(package)

    raise ToolError(
        "pi executable not found. Refusing the unpinned `npx -y` / `bunx` "
        "@earendil-works/pi-coding-agent fallback. Install pi on PATH, set "
        "UA_PI_EXECUTABLE to an absolute local binary, or set UA_PI_PACKAGE "
        "to an exact version spec (package@x.y.z)."
    )


_GROUP_KILL_CONFIRM_S = 2.0


def _kill_process_group(proc: Any, *, include_leader: bool = True) -> bool:
    """SIGKILL Pi and every descendant still in its process group.

    Pi runs in its own session (``start_new_session=True``), so its pid is
    also its process-group id. Killing only Pi left a descendant that had
    inherited stdout holding the pipe open, and the stdout read blocked past
    the deadline (Devin, PR #936). The group id stays allocated while any
    member lives, so this is safe after Pi itself has been reaped.

    ``include_leader=False`` is for a Pi that already exited on its own: the
    group is still signalled, but there is no fallback to ``proc.kill()``.
    """
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid <= 1:
        return False
    group = hasattr(os, "killpg") and isinstance(pid, int) and pid > 1
    if group:
        if pid == os.getpgrp():
            return False
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            pass
    if include_leader:
        try:
            proc.kill()
        except OSError:
            pass
    if not group:
        # Without a process group only the direct child can be checked.
        if os.name == "posix":
            return False
        try:
            proc.wait(timeout=_GROUP_KILL_CONFIRM_S)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
    deadline = time.monotonic() + _GROUP_KILL_CONFIRM_S
    while True:
        proc.poll()  # Reap our leader before asking whether the group remains.
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # EPERM cannot prove that descendants have stopped.
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.025)


def _stdout_lines(stream: Any, received: Callable[[], None], stopped: Callable[[], bool]):
    """Read pipe chunks as progress, assembling JSONL only for the RPC parser."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        for line in stream:
            received()
            yield line
        return
    fragments: list[bytes] = []
    # poll, not select: select() raises once a busy process's fd passes 1023.
    poller = None
    if os.name == "posix":
        poller = select.poll()
        poller.register(buffer, select.POLLIN)
    while not stopped():
        # A survivor can retain the pipe after a failed group kill. Do not let
        # its silence prevent returning the unconfirmed-cleanup result.
        if poller is not None and not poller.poll(50):
            continue
        chunk = buffer.read1(65536)
        if not chunk:
            break
        received()
        pieces = chunk.split(b"\n")
        for piece in pieces[:-1]:
            fragments.append(piece)
            yield b"".join(fragments).decode(stream.encoding or "utf-8", errors="replace") + "\n"
            fragments.clear()
        if pieces[-1]:
            fragments.append(pieces[-1])
    if fragments and not stopped():
        yield b"".join(fragments).decode(stream.encoding or "utf-8", errors="replace")


def parse_rpc_line(line: str) -> dict[str, Any] | None:
    """Parse a single JSONL RPC record from stdout/stdin."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _build_env(agent_label: str) -> dict[str, str]:
    """Build the subprocess environment from an allowlist.

    Does not copy ``os.environ`` and does not read key files. Hub secrets
    (``UA_AGENT_TOKENS``, ``SLACK_*``, ``SPECTRUM_*``, ``WORKOS_*``,
    ``GEMINI_API_KEY``, ``ANTHROPIC_API_KEY``) are therefore absent even when
    the hub process holds them. ``UA_AGENT_LABEL`` is the checked label the
    caller passes in — ``pi_run_task`` runs that label through
    ``config.verify_write_identity`` first.
    """
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
    # Pi is commonly installed into ~/.local/bin; keep it reachable for the
    # child's own shell (a directory, not a secret).
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in path.split(":"):
        path = f"{local_bin}:{path}"
    env: dict[str, str] = {
        "PATH": path,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "UA_AGENT_LABEL": agent_label,
    }
    for key in _OPTIONAL_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    for key in _FORWARDED_API_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    for blocked in _BLOCKED_API_KEYS:
        env.pop(blocked, None)
    return env


def _checked_agent_label(agent_label: str | None) -> str:
    """Label stamped on the child env and the vault journal.

    The default ``pi_worker`` is not an identity. An authenticated caller who
    leaves it unset is stamped with their verified identity. An explicit label
    goes through ``verify_write_identity``, which rejects a spoofed author
    when auth is enforced and otherwise returns the verified identity.
    """
    claimed = (agent_label or "").strip()
    if not claimed or claimed == _DEFAULT_AGENT_LABEL:
        checked = config.verify_write_identity(None)
        return checked or _DEFAULT_AGENT_LABEL
    checked = config.verify_write_identity(claimed)
    return checked or claimed


def _pi_timeout_cap_s() -> int:
    """Hard ceiling for a Pi run.

    ``run_cmd`` caps interactive shells at 120s. Delegated Pi runs already
    have their own ceiling (``UA_EXECUTOR_TIMEOUT_S``, default 300). The
    watchdog uses the greater of the two so an executor budget above 120s
    is honored and a caller still cannot ask for an unbounded wait.
    """
    return max(config.CMD_TIMEOUT_CAP_S, config.executor_timeout_s())


def _capped_timeout(timeout_s: int) -> int:
    try:
        requested = int(timeout_s)
    except (TypeError, ValueError) as exc:
        raise ToolError("timeout_s must be a number of seconds") from exc
    return max(1, min(requested, _pi_timeout_cap_s()))


def pi_run_task(
    prompt: str,
    cwd: str | None = None,
    model: str | None = None,
    session_dir: str | None = None,
    session_id: str | None = None,
    agent_label: str = "pi_worker",
    log_journal: bool = False,
    timeout_s: int = 120,
    kill_check: SkipJsonSchema[Callable[[], bool]] | None = None,
    poll_interval_s: float = 1.0,
) -> dict[str, Any]:
    """Execute a coding subtask using Pi in headless RPC mode (--mode rpc).

    Args:
        prompt: Task instructions for the Pi coding agent.
        cwd: Directory context for execution (workspace relative or absolute).
        model: Optional model id on the ``openai-codex`` subscription provider
            (default ``gpt-5.5``). Pi is always started with
            ``--provider openai-codex``; a model naming another provider
            (``google/...``) is refused.
        session_dir: Directory for storing Pi JSONL session logs. Must resolve
            inside the workspace.
        session_id: Resume a previous session by ID for multi-turn continuity.
        agent_label: Agent label for attribution. Checked against the verified
            identity; the default ``pi_worker`` stamps the verified identity
            when one exists.
        log_journal: If True, appends an entry to today's vault journal.
        timeout_s: Command timeout in seconds. Capped at the greater of
            ``CMD_TIMEOUT_CAP_S`` (the ``run_cmd`` ceiling) and
            ``executor_timeout_s`` (default 300, ``UA_EXECUTOR_TIMEOUT_S``).
            The cap covers the stdout read, not only the final wait.
        kill_check: Polled every `poll_interval_s` while Pi's subprocess is
            running (task #383/#1525); the moment it returns True the
            subprocess is killed and this returns `status="killed"` instead
            of waiting out `timeout_s`. Pi's own stdin/stdout RPC protocol
            means this can't reuse `run_popen_with_kill_check` (that helper
            assumes a plain argv the caller never talks to over stdin) — a
            small watcher thread does the equivalent here: it never touches
            Pi's pipes itself, only `Popen.poll()`/`.kill()`, so it cannot
            race the main thread's own reads.

    Stall detection (task #1950, the #1916 contract every other backend has):
    each chunk Pi writes to stdout is the run's progress signal. When
    none arrives for ``config.executor_stall_timeout_s()`` seconds (``0``
    disables) Pi's process group is killed and the run returns
    ``status="timeout"`` with an ``Execution stalled`` error, which the
    executor daemon retries only after confirmed cleanup; possible survivors block the task.

    Returns:
        Structured dict with keys: status, text, tools_used, files_modified,
        events_count, exit_code, error.

    The steward profile is refused. The child env is an allowlist (see
    ``_build_env``); hub secrets never reach Pi.
    """
    from .. import execution_locality
    from ..hosted_execution import refuse_hosted_execution

    # Task #2109: Pi is an agent; a devices-only hub never runs one itself.
    refuse_hosted_execution("pi_run_task")
    execution_locality.refuse_tool("pi_run_task")
    if not prompt or not prompt.strip():
        raise ToolError("prompt must not be empty")

    from .. import profiles

    if profiles.current() == profiles.STEWARD:
        raise ToolError(
            "pi_run_task is refused for the steward profile; "
            "developer and chief-of-staff may call it"
        )

    target_cwd = resolve_in_workspace(cwd or "")
    session_dir_resolved: str | None = None
    if session_dir:
        session_dir_resolved = str(resolve_in_workspace(session_dir))

    label = _checked_agent_label(agent_label)
    pinned_model = _pinned_pi_model(model)
    timeout = _capped_timeout(timeout_s)
    pi_cmd = resolve_pi_executable()

    report_progress(0, 3, "starting Pi")
    env = _build_env(label)
    # Headless RPC cannot answer Pi's own tool prompts; they would block the
    # stdout loop until the watchdog killed the child.
    cmd = list(pi_cmd) + ["--mode", "rpc", "--no-approve"]
    cmd.extend(["--provider", PI_PROVIDER, "--model", pinned_model])
    if session_dir_resolved:
        cmd.extend(["--session-dir", session_dir_resolved])
    if session_id:
        cmd.extend(["--session-id", session_id.strip()])

    req_id = "req_1"
    prompt_payload = json.dumps({"id": req_id, "type": "prompt", "message": prompt.strip()}) + "\n"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=target_cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Own process group, so a timeout or cancel reaches descendants
            # that inherited stdout (see _kill_process_group). POSIX only.
            start_new_session=os.name == "posix",
        )
    except OSError as e:
        raise ToolError(f"Failed to spawn Pi subprocess: {e}") from e

    cleanup_confirmed: bool | None = None
    cleanup_lock = threading.Lock()

    def _stop(*, include_leader: bool = True) -> None:
        nonlocal cleanup_confirmed
        with cleanup_lock:
            cleanup_confirmed = _kill_process_group(proc, include_leader=include_leader)

    def _with_cleanup(result: dict[str, Any]) -> dict[str, Any]:
        with cleanup_lock:
            confirmed = cleanup_confirmed
        if confirmed is not None:
            result["cleanup_confirmed"] = confirmed
            if not confirmed:
                note = (
                    f"process group {getattr(proc, 'pid', None)} was not confirmed gone; "
                    "inspect and stop its members before retrying this task"
                )
                result["error"] = f"{result['error']}; {note}" if result.get("error") else note
        return result

    killed = threading.Event()
    # Set once this call stops reading Pi's pipes. The watcher runs until
    # then, not only while Pi is alive: a descendant can hold stdout open
    # after Pi exits, and a cancel must still reach it.
    finished = threading.Event()
    if kill_check is not None:

        def _watch_for_kill() -> None:
            while not finished.wait(poll_interval_s):
                try:
                    if not kill_check():
                        continue
                except Exception:  # noqa: BLE001 - a broken kill_check must never crash Pi
                    return
                if proc.poll() is None:
                    killed.set()
                    _stop()
                else:
                    # Pi already exited; its result stands. Reap leftovers.
                    _stop(include_leader=False)
                return

        threading.Thread(target=_watch_for_kill, daemon=True).start()

    report_progress(1, 3, "Pi spawned")
    assistant_chunks: list[str] = []
    tools_used: list[dict[str, Any]] = []
    files_modified: list[str] = []
    events_count = 0
    error_msg: str | None = None
    saw_tool = False

    # Track in-flight tool calls so we can attach results to the right entry.
    _inflight: dict[str | None, dict[str, Any]] = {}
    # `timeout_s` used to apply only to the post-loop communicate, so a Pi
    # that never closed stdout blocked this read forever. The timer kills the
    # child at the deadline, which unblocks readline; the loop also checks the
    # deadline between lines.
    timed_out = False
    deadline = time.monotonic() + timeout

    def _expire() -> None:
        nonlocal timed_out
        timed_out = True
        _stop()

    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()

    stall_timeout_s = config.executor_stall_timeout_s()
    stalled = threading.Event()
    progress_lock = threading.Lock()
    last_progress_at = time.monotonic()

    def _output_received() -> None:
        nonlocal last_progress_at
        with progress_lock:
            last_progress_at = time.monotonic()

    if stall_timeout_s:

        def _watch_for_stall() -> None:
            # Runs until this call stops reading Pi's pipes, not only while Pi
            # lives: Pi exiting is not pipe EOF when a descendant inherited
            # stdout (PR #1230 review). A descendant still writing is progress
            # and is left to finish within the stall and wall-clock budget; one
            # that goes silent is stalled, and the kill reaches the remaining
            # group members either way. A run ended by this kill reports
            # status "timeout", never success.
            interval = min(poll_interval_s, max(0.05, stall_timeout_s / 4))
            while not finished.wait(interval):
                leader_alive = proc.poll() is None
                with progress_lock:
                    silent_for = time.monotonic() - last_progress_at
                if silent_for >= stall_timeout_s:
                    stalled.set()
                    _stop(include_leader=leader_alive)
                    return

        threading.Thread(target=_watch_for_stall, daemon=True).start()

    def _expired() -> bool:
        return timed_out or stalled.is_set() or time.monotonic() >= deadline

    timeout_result: dict[str, Any] | None = None
    try:
        # Send prompt command to stdin. A child that stops reading can block
        # here until the watchdog kills it; that surfaces as BrokenPipeError,
        # not TimeoutExpired. After the deadline it is a timeout.
        if proc.stdin:
            try:
                proc.stdin.write(prompt_payload)
                proc.stdin.flush()
            except BrokenPipeError:
                if killed.is_set() or _expired():
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
                raise

        # Read JSONL events from stdout. Killing a stuck child can raise
        # while this iterator is blocked; after the deadline that is a timeout.
        stdout_lines = None
        if proc.stdout:
            try:
                stdout_lines = _stdout_lines(
                    proc.stdout, _output_received, lambda: killed.is_set() or _expired()
                )
            except TypeError:
                stdout_lines = iter(())
            try:
                for raw_line in stdout_lines:
                    if _expired():
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    record = parse_rpc_line(raw_line)
                    if not record:
                        continue
                    events_count += 1
                    rec_type = record.get("type")

                    if rec_type == "message_update":
                        update = record.get("update", {})
                        ame = record.get("assistantMessageEvent", {})
                        if update.get("kind") == "text_delta" and update.get("text"):
                            assistant_chunks.append(update.get("text"))
                        elif ame.get("type") == "text_delta" and ame.get("delta"):
                            assistant_chunks.append(ame.get("delta"))
                        elif (
                            ame.get("type") == "text_end"
                            and ame.get("content")
                            and not assistant_chunks
                        ):
                            assistant_chunks.append(ame.get("content"))
                    elif rec_type == "message_end":
                        msg = record.get("message", {})
                        if msg.get("role") == "assistant" and not assistant_chunks:
                            for item in msg.get("content", []):
                                if (
                                    isinstance(item, dict)
                                    and item.get("type") == "text"
                                    and item.get("text")
                                ):
                                    assistant_chunks.append(item.get("text"))
                    elif rec_type == "tool_execution_start":
                        if not saw_tool:
                            saw_tool = True
                            report_progress(2, 3, "first tool")
                        entry = {
                            "name": record.get("name", "unknown"),
                            "tool_call_id": record.get("tool_call_id"),
                            "arguments": record.get("arguments"),
                            "result": None,
                        }
                        tools_used.append(entry)
                        _inflight[entry["tool_call_id"]] = entry
                    elif rec_type == "tool_execution_end":
                        tcid = record.get("tool_call_id")
                        entry = _inflight.pop(tcid, None)
                        if entry is not None:
                            entry["result"] = record.get("result")
                        # Track file modifications from write/edit tools.
                        name = record.get("name", "")
                        result = record.get("result")
                        if name in ("write", "edit", "create_file", "replace") and isinstance(
                            result, dict
                        ):
                            fpath = result.get("path") or result.get("file")
                            if fpath and fpath not in files_modified:
                                files_modified.append(fpath)
                    elif rec_type == "agent_end":
                        break
                    elif rec_type == "error":
                        error_msg = record.get("message", "Unknown RPC error")
                        break
            except OSError:
                if killed.is_set() or _expired():
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
                raise

        if _expired():
            raise subprocess.TimeoutExpired(cmd, timeout)

        # Closing stdin is Pi's cue to exit. Close it here and hand
        # communicate() no stdin: Python 3.11's communicate() flushes a closed
        # stdin and raises ValueError, so a completed run returned nothing
        # (PR #936). Then keep reading stdout through the progress clock until
        # EOF: output after agent_end, e.g. a descendant still writing, is
        # progress too and must not look like a stall (PR #1230 round 3).
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.stdin = None
        stdout_drained = threading.Event()
        stdout_errors: list[OSError] = []
        if stdout_lines is not None:
            # communicate must drain stderr at the same time: waiting for
            # stdout EOF first deadlocks a child whose stderr pipe fills
            # during shutdown. Give the remaining stdout iterator one reader,
            # which keeps observing progress even after the leader exits.
            stdout_pipe = proc.stdout
            proc.stdout = None

            def _drain_stdout() -> None:
                try:
                    for _raw_line in stdout_lines:
                        pass
                except OSError as exc:
                    stdout_errors.append(exc)
                finally:
                    try:
                        if stdout_pipe is not None and hasattr(stdout_pipe, "close"):
                            stdout_pipe.close()
                    except OSError as exc:
                        stdout_errors.append(exc)
                    finally:
                        stdout_drained.set()

            threading.Thread(target=_drain_stdout, daemon=True).start()
        else:
            stdout_drained.set()

        remaining = max(0.01, deadline - time.monotonic())
        _rest, stderr_data = proc.communicate(timeout=remaining)
        if not stdout_drained.wait(max(0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired(cmd, timeout)
        if stdout_errors:
            if killed.is_set() or _expired():
                raise subprocess.TimeoutExpired(cmd, timeout) from None
            raise stdout_errors[0]
        exit_code = proc.returncode
        if timed_out or stalled.is_set():
            raise subprocess.TimeoutExpired(cmd, timeout)
    except subprocess.TimeoutExpired:
        _stop()
        if cleanup_confirmed:
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # A pipe broken by the kill_check watcher lands here too (task #1525).
        timeout_status = (
            "killed" if killed.is_set() and proc.returncode == -signal.SIGKILL else "timeout"
        )
        report_progress(3, 3, timeout_status)
        if timeout_status == "killed":
            timeout_error = "kill switch triggered"
        elif stalled.is_set():
            timeout_error = f"Execution stalled: no output for {stall_timeout_s:g} seconds"
        else:
            timeout_error = f"Execution timed out after {timeout} seconds"
        timeout_result = {
            "status": timeout_status,
            "text": "".join(assistant_chunks),
            "tools_used": tools_used,
            "files_modified": files_modified,
            "events_count": events_count,
            "exit_code": -1,
            "error": timeout_error,
        }
        return timeout_result
    finally:
        finished.set()
        timer.cancel()
        if proc.poll() is None:
            _stop()
            if cleanup_confirmed:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if timeout_result is not None:
            _with_cleanup(timeout_result)

    if killed.is_set() and exit_code == -signal.SIGKILL:
        # kill_check fired (task #1525): report the same "killed" status
        # every other backend uses (executor/backends.py's KillSwitchTriggered
        # path), not a generic error — a cancelled run and a failed one are
        # different outcomes downstream (exec_status == "killed" handling in
        # executor/daemon.py).
        status = "killed"
        error_msg = error_msg or "kill switch triggered"
    else:
        if exit_code != 0 and not error_msg:
            stderr_text = (stderr_data or "").strip()
            error_msg = stderr_text if stderr_text else f"Pi exited with code {exit_code}"
        status = "error" if (exit_code != 0 or error_msg) else "success"

    if log_journal:
        try:
            from .vault import vault_journal

            summary = f"Ran Pi subtask: {prompt[:60]}... status={status} tools={len(tools_used)}"
            vault_journal(entry=summary, agent=label, tags=["pi-worker", "task-execution"])
        except Exception:
            pass

    report_progress(3, 3, status)
    return _with_cleanup(
        {
            "status": status,
            "text": "".join(assistant_chunks),
            "tools_used": tools_used,
            "files_modified": files_modified,
            "events_count": events_count,
            "exit_code": exit_code,
            "error": error_msg,
        }
    )
