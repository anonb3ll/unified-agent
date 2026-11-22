"""Supervision for spawned agent sessions (task #602).

Three problems, all reachable the moment ``UA_DISPATCH_REAL_SPAWN=1``:

1. **Reclaim kills live work.** ``coordinator.reclaim_expired_leases`` checks
   whether the recorded pid is alive only when the row is ``claimed`` — the
   narrow Popen-to-launched window. Every real spawn lands in ``launched``,
   where a live pid got no protection at all: at lease expiry (300s default)
   the run was marked ``retryable``/``timeout`` and its delegation released
   back to ``pending``, so the next tick could launch the SAME task again
   while the first agent was still working. Concurrent duplicate agents on one
   task is the outcome the whole durable-dispatch design exists to prevent.

2. **Nothing bounds a runaway.** No timeout, no kill.

3. **PID reuse.** ``os.kill(pid, 0)`` answers "is *a* process alive", not "is
   *our* process alive". After a reboot or enough pid churn, an unrelated
   process answers yes and a dead run looks live forever.

The identity fix has to be portable: the canonical hub is macOS, so
``/proc/<pid>/stat`` is unavailable, and psutil is not a dependency. ``ps -o
lstart=`` exists on both macOS and Linux, so the process start time is read
through it and hashed to a short token that fits in ``evidence_ref`` without
whitespace. A pid whose start token no longer matches is a different process
wearing a recycled number.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config

logger = logging.getLogger(__name__)

#: How long to let a SIGTERM be handled before escalating to SIGKILL.
TERM_GRACE_S = 10.0

#: Task #1221 finding 4: SIGKILL cannot be caught, blocked, or ignored, but a
#: process (or a member of its group) stuck in uninterruptible I/O wait (D
#: state — blocked on NFS, a hung disk, a wedged FUSE mount) can survive
#: delivery for a real interval. This bounds how long ``terminate`` polls for
#: the target to actually disappear before it will report ``killed``; past
#: it, delivery is not treated as confirmation on its own.
KILL_CONFIRM_S = 2.0

#: Retries for recording a real exit code when SQLite is briefly contended.
_REPORT_EXIT_RETRIES = 5
_REPORT_EXIT_BACKOFF_S = 0.2

# Task #1221: `terminate`/`terminate_ours` outcomes split into two buckets —
# whether cessation of the target process is actually *confirmed* or merely
# *unknown*. "we sent a signal and updated a row" (recorded) is not the same
# claim as "the process is actually gone" (confirmed); collapsing them let a
# refused or identity-mismatched termination report success. A caller that
# needs to know whether it is now safe to relaunch the same work (e.g.
# ``dispatch.coordinator.resume_run``) must gate on ``CONFIRMED``, never on
# whether termination was merely attempted.
CONFIRMED_TERMINATION_OUTCOMES = frozenset({"already_gone", "terminated", "killed"})
# "kill_unconfirmed" (finding 4): SIGKILL was delivered but the target — or a
# member of its process group — was still observed alive through the
# KILL_CONFIRM_S polling window. Listed explicitly (rather than relying on
# classify_termination_outcome's default-to-unknown fallthrough) so this
# contract is visible at the definition site, not just in behavior.
UNKNOWN_TERMINATION_OUTCOMES = frozenset(
    {
        "refused",
        "identity_mismatch",
        "kill_unconfirmed",
        # "node_cancel_requested" (task #1327 review): the cancel was relayed to
        # the ua-node's queue — the node forwards it on its next heartbeat and
        # cessation is only proven by the receipt it posts back.
        "node_cancel_requested",
    }
)

#: The three cessation claims a governed caller may make about a termination
#: attempt. "requested" = recorded intent, termination not yet attempted (or
#: its outcome not yet durably known — e.g. a crash between the two writes).
#: "confirmed" = terminate_ours reported the target is actually gone.
#: "unknown" = termination was refused, hit identity_mismatch, or otherwise
#: could not be verified — the target may still be running.
CESSATION_REQUESTED = "requested"
CESSATION_CONFIRMED = "confirmed"
CESSATION_UNKNOWN = "unknown"


def classify_termination_outcome(outcome: str | None) -> str:
    """Map a raw ``terminate``/``terminate_ours`` outcome to a cessation claim.

    Never returns ``CESSATION_REQUESTED`` — that state exists only for a run
    row that recorded cancellation intent before a termination attempt could
    run or complete (see ``dispatch.coordinator.cancel_run``). An outcome this
    function has never seen (or ``None``, e.g. an unexpected exception was
    caught by the caller) is treated as unknown — fail closed.
    """
    if outcome in CONFIRMED_TERMINATION_OUTCOMES:
        return CESSATION_CONFIRMED
    return CESSATION_UNKNOWN


def process_start_token(pid: int) -> str | None:
    """Short, portable fingerprint of when `pid` started, or None if unknown.

    ``ps -o lstart=`` is used rather than ``/proc`` because the canonical hub
    runs macOS. Returns None when ps is missing, times out, or the process is
    already gone — callers must treat None as "cannot distinguish", never as a
    match.
    """
    if pid <= 0:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw:
        return None
    normalized = " ".join(raw.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


def pid_token(pid: int) -> str:
    """The ``evidence_ref`` value recording a live child.

    ``pid:<n>:<start>`` when the start time is readable, else the legacy
    ``pid:<n>``. Both parse; the short form just carries a weaker guarantee.
    """
    start = process_start_token(pid)
    return f"pid:{pid}:{start}" if start else f"pid:{pid}"


def parse_pid_token(evidence_ref: str | None) -> tuple[int | None, str | None]:
    """Split an evidence_ref into (pid, start_token).

    Accepts the legacy ``pid:<n>`` written before this task, returning a None
    start token for it.
    """
    text = (evidence_ref or "").strip()
    if not text.startswith("pid:"):
        return None, None
    parts = text.split(":")
    try:
        pid = int(parts[1])
    except (IndexError, ValueError):
        return None, None
    start = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    return pid, start


def pid_alive(pid: int | None) -> bool:
    """Is any process with this pid alive? (Says nothing about identity.)"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by someone else. Still alive.
        return True
    except OSError:
        return False
    return True


def pid_is_ours(evidence_ref: str | None) -> bool:
    """Is the process recorded in `evidence_ref` still alive AND still ours?

    With a start token, a recycled pid is correctly rejected. Without one
    (legacy ``pid:<n>`` rows), this degrades to liveness alone — which is what
    the code did before this task, so old rows are no worse off than they were.
    """
    pid, expected_start = parse_pid_token(evidence_ref)
    if not pid_alive(pid):
        return False
    if expected_start is None:
        return True
    current = process_start_token(pid)
    if current is None:
        # Cannot re-read the start time (ps gone, permissions, race). Fall back
        # to liveness rather than declaring a possibly-live agent dead — the
        # wall-clock bound is what stops it running forever.
        return True
    return current == expected_start


def _process_group_id(pid: int) -> int | None:
    """Return the process group for `pid`, or None when it cannot be read."""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _pgid_alive(pgid: int) -> bool:
    """Is any process in `pgid` still alive?"""
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # On Darwin, killpg(pgid, 0) raises EPERM (PermissionError) when
        # no live targets remain in the group. On Linux it raises ESRCH (ProcessLookupError).
        if sys.platform == "darwin":
            return False
        return True
    except OSError:
        return False
    return True


def _termination_target_alive(pid: int, pgid: int | None) -> bool:
    """Whether the detached CLI session is still running.

    After SIGTERM the leader pid may exit while tool subprocesses in its process
    group keep running. Completion must wait for the whole group, not just the
    leader.

    Group-checking only makes sense when ``pgid`` is a *distinct* group from
    our own — matching the condition ``_signal_process_tree`` uses to decide
    whether it signalled the group or just the leader. A child spawned without
    its own session (no ``start_new_session=True`` — true for every plain test
    fixture process, false only for the production detached-CLI path) shares
    our process group, so ``_pgid_alive`` would read the whole group,
    including this caller, as permanently "alive" and confirmation would
    never complete (finding 4's post-SIGKILL check depends on this being
    accurate, not just a best-effort hint).
    """
    if pgid is not None and pgid > 0 and pgid != os.getpgrp():
        return _pgid_alive(pgid)
    return pid_alive(pid)


def _signal_process_tree(pid: int, sig: signal.Signals, pgid: int | None = None) -> None:
    """Deliver `sig` to the detached CLI session, not just the leader pid.

    ``CliProcessSpawner`` uses ``start_new_session=True``, so tool subprocesses
    live in the leader's process group. Signalling only the leader leaves
    descendants running after reclaim releases the delegation.
    """
    if pgid is None:
        pgid = _process_group_id(pid)
    if pgid is not None and pgid > 0 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            if sys.platform == "darwin":
                pass
            else:
                raise
        except OSError:
            pass
    os.kill(pid, sig)


def _try_reap(pid: int) -> None:
    """Best-effort non-blocking reap so a zombie we own does not read as alive.

    ``os.kill(pid, 0)`` / ``os.killpg`` report a zombie as alive until its real
    parent reaps it — POSIX keeps the pid slot until then. When the caller
    happens to be that parent (true for tests that ``subprocess.Popen`` a
    child directly and terminate it without an interleaved ``wait()``),
    without this the post-SIGKILL confirmation loop below would spin for its
    full window and report ``kill_unconfirmed`` for a process that is
    actually gone. When the caller is not the parent (the production path,
    where a detached supervisor owns the child), ``waitpid`` raises
    ``ChildProcessError`` and this is a no-op — the supervisor's own blocking
    ``wait()`` does the real reaping, promptly, since SIGKILL cannot be
    blocked or ignored.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def terminate(pid: int, *, grace_s: float = TERM_GRACE_S) -> str:
    """SIGTERM, then SIGKILL after `grace_s`. Returns what it took.

    Never SIGKILLs first: a headless agent CLI may need to flush a log or
    finish a write. Returns 'already_gone', 'terminated', 'killed',
    'kill_unconfirmed', or 'refused'.
    """
    if not pid_alive(pid):
        return "already_gone"
    pgid = _process_group_id(pid)
    try:
        _signal_process_tree(pid, signal.SIGTERM, pgid=pgid)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        # PermissionError subclasses OSError — handle before the broad OSError
        # catch so denied signals surface as "refused" for coordinator blocking.
        return "refused"
    except OSError:
        return "already_gone"

    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not _termination_target_alive(pid, pgid):
            return "terminated"
        time.sleep(0.1)

    try:
        _signal_process_tree(pid, signal.SIGKILL, pgid=pgid)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "refused"
    except OSError:
        return "terminated"

    # Task #1221 finding 4: SIGKILL delivery is not itself confirmation — a
    # process wedged in uninterruptible I/O wait can outlive it. Poll for the
    # target to actually disappear before calling this "killed" (a CONFIRMED
    # outcome); past the window, report 'kill_unconfirmed' (an UNKNOWN
    # outcome) rather than let delivery alone stand in for cessation.
    kill_deadline = time.monotonic() + KILL_CONFIRM_S
    while time.monotonic() < kill_deadline:
        _try_reap(pid)
        if not _termination_target_alive(pid, pgid):
            return "killed"
        time.sleep(0.05)
    _try_reap(pid)
    return "kill_unconfirmed" if _termination_target_alive(pid, pgid) else "killed"


def terminate_ours(evidence_ref: str | None, *, grace_s: float = TERM_GRACE_S) -> str:
    """Terminate the process recorded in `evidence_ref`, identity-checked.

      Returns the same outcomes as ``terminate`` (including ``kill_unconfirmed``),
      plus ``identity_mismatch`` when
      the pid is alive but no longer matches the recorded start token — signaling
    only the bare pid would risk hitting an unrelated recycled process.
    """
    if isinstance(evidence_ref, str) and evidence_ref.startswith("node:"):
        # A node-routed run has no local pid to signal; the queue item is the
        # thing to stop. Lazy import: node_queue pulls in dispatch modules.
        from .. import node_queue

        return node_queue.cancel_for_run_evidence(evidence_ref)
    pid, _start = parse_pid_token(evidence_ref)
    if pid is None:
        return "already_gone"
    if not pid_alive(pid):
        return "already_gone"
    if not pid_is_ours(evidence_ref):
        return "identity_mismatch"
    return terminate(pid, grace_s=grace_s)


def _operational_error_is_teardown(exc: sqlite3.OperationalError) -> bool:
    """True when the database is gone (test teardown), not merely contended."""
    msg = str(exc).lower()
    return "no such table" in msg or "unable to open" in msg


def _get_run_if_not_terminal(run_id: str) -> bool:
    """Return True when ``run_id`` should still receive an exit report."""
    from .coordinator import get_run
    from .scheduler import TERMINAL_OUTCOMES

    for attempt in range(_REPORT_EXIT_RETRIES):
        try:
            run = get_run(run_id)
            if run and run.get("state") in TERMINAL_OUTCOMES:
                return False
            return True
        except sqlite3.OperationalError as exc:
            if _operational_error_is_teardown(exc):
                logger.debug("dispatch exit watcher lost its database", exc_info=True)
                return False
            if attempt + 1 >= _REPORT_EXIT_RETRIES:
                logger.warning("dispatch exit watcher could not read run state", exc_info=True)
                return False
            time.sleep(_REPORT_EXIT_BACKOFF_S * (2**attempt))
    return True


def _report_exit_resilient(
    run_id: str,
    returncode: int | None,
    *,
    detail: str | None = None,
) -> None:
    """Record the real exit code, retrying brief SQLite contention.

    A supervisor that exits 0 after a successful agent must not leave the run
    stranded at ``launched`` because a short-lived tick held the database lock —
    the poll reaper would then observe only ``report_exit(None)`` and record
    ``failed``.
    """
    from .report import report_exit

    for attempt in range(_REPORT_EXIT_RETRIES):
        try:
            report_exit(returncode, run_id=run_id, detail=detail)
            return
        except sqlite3.OperationalError as exc:
            if _operational_error_is_teardown(exc):
                logger.debug("dispatch exit watcher lost its database", exc_info=True)
                return
            if attempt + 1 >= _REPORT_EXIT_RETRIES:
                logger.warning("dispatch exit report exhausted retries", exc_info=True)
                return
            time.sleep(_REPORT_EXIT_BACKOFF_S * (2**attempt))


def _require_native_run(
    run_id: str, *, policy_required: bool = False, launch_caller: str | None = None
) -> None:
    """Verify this exact run against parent-owned storage and current policy."""
    from .. import storage, trust_presets

    try:
        storage.require_live_task_store()
        if policy_required and not config.policy_file_path().is_file():
            raise ToolError("native supervisor policy authority is missing")
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT r.agent_label, r.state, d.from_agent FROM dispatch_runs r "
                "LEFT JOIN delegations d ON d.id=r.delegation_id WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["state"] not in {"claimed", "launched", "running"}:
            raise ToolError("native supervisor run authority is missing or terminal")
        trust_presets.require_native_allowed(row["agent_label"], originator=row["from_agent"])
        if launch_caller is not None:
            # Captured authenticated parent authority is independent of the
            # child's target identity; checking it grants no actor permissions.
            trust_presets.require_native_allowed(launch_caller, originator=row["from_agent"])
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("native supervisor could not verify current authority") from exc


def _wait_and_report_exit(
    proc: subprocess.Popen,
    run_id: str,
    *,
    policy_required: bool = False,
    launch_caller: str | None = None,
) -> None:
    """Poll live per-run authority without depending on scheduler ticks."""
    from .coordinator import cancel_run

    own_token = pid_token(proc.pid)
    stop_requested = False
    while True:
        # Once an authority file has existed, disappearing is a read failure,
        # not permission to silently use the unconfigured policy default.
        policy_required = policy_required or config.policy_file_path().is_file()
        if not stop_requested:
            try:
                _require_native_run(
                    run_id, policy_required=policy_required, launch_caller=launch_caller
                )
            except ToolError:
                try:
                    result = cancel_run(
                        run_id, actor="dispatch-supervisor", reason="trust_preset_native_denied"
                    )
                    if not result.get("ok"):
                        terminate_ours(own_token)
                except Exception:
                    # Lost storage cannot authorize continued native execution.
                    # The Popen child belongs to this supervisor; stop only its
                    # captured identity and do not manufacture a ledger outcome.
                    terminate_ours(own_token)
                stop_requested = True
        try:
            returncode = proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            returncode = None
            break

    if stop_requested or not _get_run_if_not_terminal(run_id):
        # A denied run is governed by the cancellation outcome. Even after
        # authority loss prevents its ledger write, never publish success.
        return
    _report_exit_resilient(run_id, returncode)


#: How long the launch parent waits for the supervisor's pid handshake.
SUPERVISOR_HANDSHAKE_S = 10.0

#: Content the parent writes when it gives up on the handshake.
HANDSHAKE_ABANDONED = "abandoned"

# Task #1950: the handshake is one atomic race with exactly one winner.
#
# The supervisor starts the CLI behind a start gate (``supervisor_spawn``): the
# child blocks before exec until the supervisor writes a release byte, and the
# supervisor writes it only after the pid is durable in ``dispatch_runs`` and
# published here. A child therefore cannot do work, or exit cleanly, in the
# window between Popen and the pid write.
#
# The supervisor publishes by hard-linking a complete temp file onto the
# handshake path (``link`` fails if the path exists); the parent gives up by
# creating that same path with ``O_EXCL``. Whichever lands first wins. If the
# parent wins, the supervisor never releases the gate, so no work ran and the
# parent's retryable outcome is true. If the supervisor wins, the parent reads
# a complete pid and returns it, and the supervisor reports the real exit.


def publish_handshake_pid(pid_path: Path, pid: int) -> bool:
    """Supervisor side: publish ``pid`` atomically. False when the parent gave up."""
    pid_path = Path(pid_path)
    tmp = pid_path.with_name(f".{pid_path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(str(pid), encoding="utf-8")
        os.link(tmp, pid_path)
        return True
    except OSError:
        # FileExistsError: the parent abandoned first. FileNotFoundError: the
        # parent already removed the handshake directory. Either way the parent
        # is not waiting for this child, so it must not run.
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def abandon_handshake(pid_path: Path) -> bool:
    """Parent side: give up on the handshake. False when the supervisor published first."""
    try:
        fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, HANDSHAKE_ABANDONED.encode())
    finally:
        os.close(fd)
    return True


def _read_handshake_pid(pid_path: Path) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def spawn_via_detached_supervisor(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Any,
    run_id: str,
) -> int:
    """Spawn `argv` under a detached supervisor that reports the real exit code.

    Returns the CLI pid immediately. The supervisor stays alive until the child
    exits and calls ``report_exit`` with the real exit code. The supervisor
    receives fixed parent authority separately from the child environment.
    """
    from ..hosted_execution import refuse_hosted_execution

    refuse_hosted_execution("dispatch")
    import json
    import shutil
    import tempfile

    from .. import execution_locality

    # Task #2109: a devices-only hub never hosts an agent process tree.
    execution_locality.refuse("dispatch supervisor spawn")

    # A private (0700) directory, so nobody else can pre-create or swap the
    # handshake path the two sides race on.
    handshake_dir = Path(tempfile.mkdtemp(prefix="ua-dispatch-pid-"))
    pid_path = handshake_dir / "pid"

    payload = json.dumps(
        {
            "argv": argv,
            "cwd": cwd,
            "log_path": str(log_path),
            "run_id": run_id,
            # Fixed parent process authority, independent of filtered child
            # environment or dispatch request extras. These are non-secret
            # paths/identity labels, never policy bodies or bearer values.
            "authority": {
                "data_home": str(config.data_home().absolute()),
                "policy_file": str(config.policy_file_path().absolute()),
                "policy_file_required": config.policy_file_path().is_file(),
                "agent_labels": ":".join(sorted(config.agent_labels())),
                "launch_caller": config.verified_agent_identity(),
            },
        }
    ).encode()
    supervisor_env = dict(env)
    supervisor_env["UA_SUP_PID_PATH"] = str(pid_path)
    try:
        supervisor = subprocess.Popen(  # noqa: S603 - fixed module argv
            [sys.executable, "-m", "unified_agent.dispatch.supervisor_spawn"],
            stdin=subprocess.PIPE,
            env=supervisor_env,
            # Import the installed/source package from this checkout, not a
            # workspace-local ``unified_agent`` package in the dispatched cwd.
            cwd=str(Path(__file__).resolve().parents[2]),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert supervisor.stdin is not None
        supervisor.stdin.write(payload)
        supervisor.stdin.close()

        deadline = time.monotonic() + SUPERVISOR_HANDSHAKE_S
        while time.monotonic() < deadline:
            pid = _read_handshake_pid(pid_path)
            if pid is not None:
                return pid
            time.sleep(0.01)
        if abandon_handshake(pid_path):
            # Won the race: the supervisor can no longer release its child.
            supervisor.kill()
            raise RuntimeError("detached supervisor did not report child pid")
        # The supervisor published at the deadline: its child is live and
        # supervised, so this launch succeeded.
        pid = _read_handshake_pid(pid_path)
        if pid is None:
            raise RuntimeError("detached supervisor published an unreadable child pid")
        return pid
    finally:
        shutil.rmtree(handshake_dir, ignore_errors=True)


def spawn_exit_watcher(proc: subprocess.Popen, run_id: str) -> None:
    """In-process exit waiter for tests and direct Popen supervision.

    Production spawns route through ``spawn_via_detached_supervisor`` →
    ``supervisor_spawn``, which blocks on ``wait()`` and reports the real exit
    code even when the tick process exits immediately. This helper only covers
    cases where the parent already holds the ``Popen`` handle (tests).
    """
    if proc.pid is None or proc.pid <= 0:
        return
    threading.Thread(
        target=_wait_and_report_exit,
        args=(proc, run_id),
        kwargs={"launch_caller": config.verified_agent_identity()},
        name=f"dispatch-supervisor-{run_id[:12]}",
        daemon=True,
    ).start()


def runtime_exceeded(started_at: Any, *, now: Any = None, max_s: float | None = None) -> bool:
    """Has a run been going longer than the wall-clock ceiling?

    Unparseable timestamps return False — a run whose age cannot be determined
    is left to the liveness check rather than killed on a guess.
    """
    from datetime import UTC, datetime

    ceiling = config.dispatch_run_max_s() if max_s is None else max_s
    if ceiling <= 0:
        return False
    if isinstance(started_at, str):
        try:
            parsed = datetime.fromisoformat(started_at)
        except ValueError:
            return False
    elif isinstance(started_at, datetime):
        parsed = started_at
    else:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - parsed).total_seconds() > ceiling
