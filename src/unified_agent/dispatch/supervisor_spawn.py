"""Detached supervisor entry point for real CLI spawns (task #602).

``CliProcessSpawner`` cannot block on ``proc.wait()`` — timer ticks and Control
Center dispatch must return immediately. A short-lived parent that exits before
the agent finishes leaves no in-process waiter, and a polling reaper cannot
observe exit status without being the child's parent.

This module is invoked as a detached ``python -m`` subprocess. It spawns the
CLI, reports the child's pid to the parent through a handshake file, then polls
the exact run's current authority while waiting and reports its real exit code.

Start gate (task #1950): the CLI is started behind a tiny gate that blocks
before ``exec`` until this supervisor writes a release byte to a pipe. The byte
is written only after the pid is recorded in ``dispatch_runs`` and published to
the parent, so no work can run, or exit cleanly unobserved, in the window
between Popen and the pid write. ``exec`` keeps the pid and its start time, so
the recorded ``pid:<n>:<start>`` token still names the CLI. If the supervisor
dies, or the parent gives up first, the gate reads EOF and exits without
running the CLI. See ``supervise.publish_handshake_pid`` for the parent race.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

#: Exit status of a gate that was never released (the CLI never ran).
GATE_NOT_RELEASED_EXIT = 97

# Runs under ``python -I -S``: no cwd, user site or PYTHON* environment on the
# path, so the dispatched cwd cannot shadow a module the gate imports.
_GATE_SOURCE = (
    "import os, sys\n"
    "fd = int(sys.argv[1])\n"
    "go = os.read(fd, 1)\n"
    "os.close(fd)\n"
    "if go != b'1':\n"
    f"    os._exit({GATE_NOT_RELEASED_EXIT})\n"
    "try:\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except OSError as exc:\n"
    "    sys.stderr.write(f'dispatch start gate: exec failed: {exc}\\n')\n"
    "    os._exit(127)\n"
)


def gated_argv(argv: list[str], gate_fd: int) -> list[str]:
    """``argv`` wrapped in the start gate that waits on ``gate_fd``."""
    return [sys.executable, "-I", "-S", "-c", _GATE_SOURCE, str(gate_fd), *argv]


def _release_gate(write_fd: int) -> bool:
    try:
        return os.write(write_fd, b"1") == 1
    except OSError:
        # The gate already exited; the wait below reports its status.
        return False


def main() -> None:
    payload = json.load(sys.stdin)
    pid_path = os.environ["UA_SUP_PID_PATH"]
    authority = payload.get("authority")
    if not isinstance(authority, dict) or set(authority) != {
        "data_home",
        "policy_file",
        "policy_file_required",
        "agent_labels",
        "launch_caller",
    }:
        raise ValueError("detached supervisor requires fixed parent authority")
    for key in ("data_home", "policy_file"):
        if not isinstance(authority[key], str) or not Path(authority[key]).is_absolute():
            raise ValueError("detached supervisor authority paths must be absolute")
    if not isinstance(authority["policy_file_required"], bool) or not isinstance(
        authority["agent_labels"], str
    ):
        raise ValueError("detached supervisor authority metadata is invalid")
    caller = authority["launch_caller"]
    if caller is not None and (
        not isinstance(caller, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", caller) is None
    ):
        raise ValueError("detached supervisor launch caller is invalid")
    # The child keeps exactly the caller's already-scrubbed environment. The
    # privileged supervisor alone receives parent-owned configuration/storage.
    child_env = dict(os.environ)
    child_env.pop("UA_SUP_PID_PATH", None)
    os.environ["UA_HOME"] = authority["data_home"]
    os.environ["UA_POLICY_FILE"] = authority["policy_file"]
    os.environ["UA_AGENT_LABELS"] = authority["agent_labels"]
    from . import claude_sandbox

    # Task #2191: snapshot the identity of the settings file and sidecar this
    # launch wrote, before anything else can run. dispatch_resume reuses the
    # run_id, so by the time this child exits a resumed launch may have
    # rewritten both names (as new inodes) for its own live child; only the
    # entries that still match this snapshot are removed.
    owned = claude_sandbox.snapshot_run_artifacts(str(payload.get("run_id") or ""))
    try:
        _supervise(payload, pid_path, authority, child_env, caller)
    finally:
        # The run is over (or never started), so its per-run settings file and
        # identity sidecar have no further reader. The launch log is evidence
        # and stays.
        claude_sandbox.discard_run_artifacts(owned)


def _supervise(
    payload: dict,
    pid_path: str,
    authority: dict,
    child_env: dict[str, str],
    caller: str | None,
) -> None:
    from .supervise import _require_native_run, _wait_and_report_exit

    _require_native_run(
        payload["run_id"], policy_required=authority["policy_file_required"], launch_caller=caller
    )
    gate_read, gate_write = os.pipe()
    try:
        try:
            with open(payload["log_path"], "w", encoding="utf-8") as log_f:
                proc = subprocess.Popen(  # noqa: S603 - argv list from routed launch
                    gated_argv(payload["argv"], gate_read),
                    cwd=payload["cwd"],
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(gate_read,),
                )
        finally:
            # Only the gate holds the read end; only this process the write end.
            os.close(gate_read)
        # The supervisor can outlive the launch parent. Persist the same guarded
        # PID evidence before publishing the handshake, so its later cancellation
        # does not mistake an already-spawned child for an absent process.
        from . import coordinator, launch, supervise

        own_token = supervise.pid_token(proc.pid)
        try:
            recorded = launch._persist_spawn_pid(payload["run_id"], proc.pid)
            if not recorded:
                current = coordinator.get_run(payload["run_id"])
                outcome = supervise.terminate_ours(own_token)
                if current is not None and current.get("termination_outcome") is not None:
                    launch._persist_spawn_pid_after_cancel(payload["run_id"], proc.pid)
                    current["evidence_ref"] = own_token
                    coordinator.finalize_cancellation(
                        current,
                        actor="dispatch-supervisor",
                        reason="cancelled_during_launch",
                        termination=outcome,
                        cessation=supervise.classify_termination_outcome(outcome),
                        allow_downgrade_confirmed_cessation=True,
                    )
        except Exception:
            supervise.terminate_ours(own_token)
            raise
        published = supervise.publish_handshake_pid(Path(pid_path), proc.pid)
        if recorded and published:
            # The pid is durable and the parent holds it: only now may the
            # CLI run. Unrecorded (cancelled or no longer live) or abandoned
            # runs never release the gate, so their work never starts.
            _release_gate(gate_write)
    finally:
        os.close(gate_write)

    if not published:
        # The parent gave up and reports this launch as failed; the gate has
        # read EOF and exits without running the CLI. Reap it and stop.
        proc.wait()
        return

    _wait_and_report_exit(
        proc,
        payload["run_id"],
        policy_required=authority["policy_file_required"],
        launch_caller=caller,
    )


if __name__ == "__main__":
    main()
