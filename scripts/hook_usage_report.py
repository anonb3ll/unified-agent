#!/usr/bin/env python3
"""Claude Code hook: report a run's measured usage when it ends (task #1837).

Interactive runs finished unknown-cost because nothing reported what the
session spent on them. This hook closes that gap without asking the agent to
remember anything:

* PostToolUse on ``task_claim`` / ``task_update``: a reply that names the
  caller's live run (``current_run_id`` with ``claimed_at``) is remembered per
  session in ``~/.cache/ua-usage-hook/<session>.json``. A remembered run ends
  when ``task_update`` moves its task out of ``doing``, or when any reply for
  that task names another run or none (``release_claim``, a takeover).
* No call is counted twice. The session keeps a cutoff that only moves
  forward: each report covers ``[max(claimed_at, cutoff), end)`` and then
  advances the cutoff to its end, so report windows never overlap, whatever
  the order of claims, renewals, takeovers or hub and local clocks. A newer
  claim ends older windows at its own ``claimed_at``. The hub keeps the
  larger of a run's cumulative reports, so each run keeps the windows it was
  open and every report sums all of them (task #2037): a run renewed after
  being superseded reports its whole total. No window is ever dropped.
* Only this session's own runs are remembered. The session learns its agent
  label from ``verified_caller``, which the hub adds to authenticated
  ``task_claim`` replies and unkeyed ``task_update`` replies, or, without it
  (an older hub, a keyed update), from a ``task_claim`` reply (a claim is
  always the caller's own). Replies naming
  another agent's run are ignored, and nothing is remembered until the label
  is known.
* SessionEnd: every run still remembered for the session is reported as
  usage so far (the hub accepts late accounting on open or closed runs). The
  session's cutoff is kept, so a resumed session cannot re-report time.

Each session's state is read and written under an exclusive lock, taken with a
bounded wait (the hook skips rather than stall the agent), and saved by atomic
replace. Once a day, state older than PRUNE_AFTER_S whose session transcript
is gone is deleted under the same lock.

Reporting runs ``ua-usage --window <start>,<end> ... --report`` from the
unified_agent checkout (``UA_USAGE_BIN``, else ``~/dev/unified_agent/.venv/
bin/ua-usage``, else ``ua-usage`` on ``PATH``) in a detached child, so the hook returns at once. The child reads
the session's own transcript, sums its model calls once per message, and posts
them with ``task_claim(usage_only=True)`` under the run's own actor. Billing is
never inferred. The hub URL and bearer come from ``~/.claude.json`` (the
session's own unified-agent registration) and reach the child through its
environment, never argv. Devin and Cursor can import Claude hooks, so this runs
only inside Claude Code (``CLAUDECODE=1``). Best-effort throughout: every
failure exits 0, and the child's output goes to ``report.log`` beside the state.
"""

# A hook must never fail or block the agent, so broad except clauses are deliberate.
# ruff: noqa: BLE001

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# timezone.utc, not datetime.UTC: the host's system python3 can be 3.9 (macOS).
UTC = timezone.utc  # noqa: UP017

STATE_DIR = Path.home() / ".cache" / "ua-usage-hook"
TOOL_PREFIX = "mcp__unified-agent__"
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")
RUN_RE = re.compile(r"sp-[A-Za-z0-9]{1,64}")
LOCK_WAIT_S = 2.0
CURSOR = "_cursor"  # state key of the session cutoff; task keys are digits
WINDOWS = "_windows"  # run id -> windows already reported for it
SELF = "_self"  # this session's agent label, learned from its own task_claim
STORE = "_store"  # the Claude projects dir the session's transcript lives in
PRUNE_EVERY_S = 86400.0
PRUNE_AFTER_S = 30 * 86400.0


def _hub() -> tuple[str, str] | None:
    """``(hub /mcp url, bearer)`` from the session's own registration."""
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        entry = cfg.get("mcpServers", {}).get("unified-agent", {})
        url = entry.get("url", "")
        token = entry.get("headers", {}).get("Authorization", "")
    except Exception:
        return None
    if not url or not token:
        return None
    return url, token.removeprefix("Bearer ").strip()


def _reply(response) -> dict:
    """The tool's JSON reply, whatever shape the hook payload carries it in."""
    if isinstance(response, dict) and "content" in response:
        response = response["content"]
    if isinstance(response, list):
        response = "".join(block.get("text", "") for block in response if isinstance(block, dict))
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return {}
    return response if isinstance(response, dict) else {}


def _when(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _state_path(session: str) -> Path | None:
    return STATE_DIR / f"{session}.json" if SESSION_RE.fullmatch(session) else None


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: Path, runs: dict) -> None:
    if not runs:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(runs), encoding="utf-8")
    os.replace(tmp, path)


def _lock(session: str, wait_s: float):
    """The session's lock file, held exclusively, or None if busy past ``wait_s``.

    Pruning may unlink a lock file while holding it, so a holder checks that
    the path still names the file it locked and retries on a fresh one if not;
    two hooks can then never hold locks on different files for one session.
    """
    path = STATE_DIR / f"{session}.lock"
    deadline = time.monotonic() + wait_s
    while True:
        lock = path.open("a")
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        lock.close()
                        return None
                    time.sleep(0.02)
            try:
                same = os.stat(path).st_ino == os.fstat(lock.fileno()).st_ino
            except FileNotFoundError:
                same = False
            if same:
                return lock
        except Exception:
            lock.close()
            raise
        lock.close()  # unlinked under us by _prune: lock the new file instead


def _transact(session: str, change) -> None:
    """Run ``change(runs, now)`` on the session's state under its lock."""
    path = _state_path(session)
    if path is None:
        return
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = _lock(session, LOCK_WAIT_S)
    if lock is None:
        return  # never stall the agent; this event goes unrecorded
    with lock:
        runs = _load(path)
        change(runs, datetime.now(UTC).isoformat())
        _save(path, runs)


def _binary() -> str | None:
    """``UA_USAGE_BIN``, else the checkout's venv, else ``ua-usage`` on PATH."""
    explicit = os.environ.get("UA_USAGE_BIN")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    venv = Path.home() / "dev" / "unified_agent" / ".venv" / "bin" / "ua-usage"
    return str(venv) if venv.is_file() else shutil.which("ua-usage")


def _report(session: str, runs: dict, task: str, run: dict, until: str) -> None:
    start, end = _when(run.get("since")), _when(until)
    cutoff = _when(runs.get(CURSOR))
    if start is None or end is None:
        return
    if cutoff is not None and cutoff > start:
        start = cutoff
    if end <= start:
        return  # nothing left that another report has not already covered
    runs[CURSOR] = end.isoformat()
    # The hub keeps the larger of a run's cumulative reports, so a run reported
    # in several disjoint windows sends all of them each time and ua-usage sums
    # them (task #2037). The list survives SessionEnd, like the cutoff.
    known = runs.setdefault(WINDOWS, {})
    spans = known.setdefault(run["run"], [])
    spans.append([start.isoformat(), end.isoformat()])
    binary = _binary()
    hub = _hub()
    if hub is None or binary is None:
        return
    url, token = hub
    argv = [
        binary,
        "--harness",
        "claude",
        "--session",
        session,
        *[arg for a, b in spans for arg in ("--window", f"{a},{b}")],
        "--report",
        "--task",
        str(task),
        "--run",
        run["run"],
        "--agent",
        run["agent"],
    ]
    env = {**os.environ, "UA_HTTP_HUB_URL": url, "UA_AGENT_BEARER_TOKEN": token}
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (STATE_DIR / "report.log").open("a", encoding="utf-8") as log:
            log.write(f"{end.isoformat()} task {task} run {run['run']}\n")
            log.flush()
            subprocess.Popen(  # noqa: S603 - fixed binary, no shell, secret only in env
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except Exception:
        pass


def _end(session: str, runs: dict, task: str, until: str) -> None:
    run = runs.pop(task, None)
    if isinstance(run, dict):
        _report(session, runs, task, run, until)


def _tasks(runs: dict) -> list[str]:
    return [t for t in runs if not t.startswith("_")]


def _reconcile(session: str, runs: dict, reply: dict, now: str) -> None:
    """End a remembered run that a reply shows is no longer the task's run."""
    task = str(reply.get("id") or "")
    known = runs.get(task)
    if isinstance(known, dict) and "current_run_id" in reply:
        if reply.get("current_run_id") != known.get("run"):
            # A replacement run starts at its claimed_at; end the old window
            # there so the two share one boundary. A release ends it now.
            boundary = reply.get("claimed_at") if reply.get("current_run_id") else None
            _end(session, runs, task, boundary if _when(boundary) else now)


def _remember(session: str, runs: dict, reply: dict, now: str) -> None:
    task, run = reply.get("id"), reply.get("current_run_id")
    agent, since = reply.get("current_run_agent"), reply.get("claimed_at")
    if not (isinstance(task, int) and isinstance(run, str) and RUN_RE.fullmatch(run)):
        return
    if not (isinstance(agent, str) and agent):
        return
    if agent != runs.get(SELF):
        # Another agent's run, or this session's label is not known yet: never
        # this session's window (task #2037).
        return
    if _when(since) is None:
        # A run opened by task_update(status="doing") carries no claimed_at;
        # its window starts when this hook first sees it (the cutoff still
        # keeps it from overlapping another run).
        since = now
    known = runs.get(str(task))
    if isinstance(known, dict) and known.get("run") == run:
        return  # a renewal keeps the run's original window
    # One window at a time: older windows end where this run begins.
    for other in [t for t in _tasks(runs) if t != str(task)]:
        _end(session, runs, other, since)
    _end(session, runs, str(task), since)
    runs.setdefault(STORE, str(_claude_projects()))
    runs[str(task)] = {"run": run, "agent": agent, "since": since}


def _claude_projects() -> Path:
    """Where Claude Code keeps transcripts; matches ``native_sessions.claude_store``."""
    override = os.environ.get("UA_NODE_CLAUDE_PROJECTS")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "projects"


def _resumable(session: str, runs: dict) -> bool:
    """Whether a transcript for ``session`` exists in its recorded or current store."""
    stores = {_claude_projects()}
    if isinstance(runs.get(STORE), str):
        stores.add(Path(runs[STORE]))
    for store in stores:
        try:
            if any(store.glob(f"*/{session}.jsonl")):
                return True
        except Exception:
            return True  # cannot tell: keep the state
    return False


def _prune() -> None:
    """Drop state for sessions that can no longer resume, at most once a day.

    A session's cutoff and window lists must outlive SessionEnd (a resumed
    session must not re-report time), so state is only removed once it is
    older than PRUNE_AFTER_S and the session's transcript is gone from both the
    store recorded with the state and the current one. Each removal holds the
    session's lock (a busy session is skipped) and rechecks under it.
    """
    marker = STATE_DIR / ".pruned"
    now = time.time()
    try:
        if marker.exists() and now - marker.stat().st_mtime < PRUNE_EVERY_S:
            return
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.touch()
        for state in STATE_DIR.glob("*.json"):
            session = state.stem
            if not SESSION_RE.fullmatch(session):
                continue
            try:
                if now - state.stat().st_mtime < PRUNE_AFTER_S:
                    continue
            except FileNotFoundError:
                continue
            lock = _lock(session, 0.0)
            if lock is None:
                continue  # in use: certainly not stale
            with lock:
                try:
                    if now - state.stat().st_mtime < PRUNE_AFTER_S:
                        continue
                except FileNotFoundError:
                    continue
                if _resumable(session, _load(state)):
                    continue
                state.unlink(missing_ok=True)
                # Unlinked while held: a waiting hook sees the inode change in
                # _lock and locks a fresh file, never the orphaned one.
                (STATE_DIR / f"{session}.lock").unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    if os.environ.get("CLAUDECODE") != "1":
        return 0
    _prune()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    session = str(data.get("session_id") or "")
    if _state_path(session) is None:
        return 0
    event = data.get("hook_event_name")

    if event == "SessionEnd":

        def end_all(runs: dict, now: str) -> None:
            for task in _tasks(runs):
                _end(session, runs, task, now)
            # The cutoff outlives the session: a resumed session that renews a
            # superseded run must not reopen time already reported.

        _transact(session, end_all)
        return 0

    tool = str(data.get("tool_name") or "")
    if event != "PostToolUse" or not tool.startswith(TOOL_PREFIX):
        return 0
    name = tool.removeprefix(TOOL_PREFIX)
    params = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    reply = _reply(data.get("tool_response"))

    def learn_self(runs: dict) -> None:
        caller = reply.get("verified_caller")
        if isinstance(caller, str) and caller:
            runs[SELF] = caller  # the hub's authenticated answer always wins
            return
        agent = reply.get("current_run_agent")
        if name == "task_claim" and isinstance(agent, str) and agent and not runs.get(SELF):
            # An older hub: task_claim claims for the authenticated caller, so
            # its reply names this session's own agent label.
            runs[SELF] = agent

    def apply(runs: dict, now: str) -> None:
        if name in ("task_claim", "task_update"):
            learn_self(runs)
        if name == "task_claim" and not params.get("usage_only"):
            _reconcile(session, runs, reply, now)
            _remember(session, runs, reply, now)
        elif name == "task_update":
            task = str(params.get("id") or reply.get("id") or "")
            status = params.get("status")
            if status == "doing":
                _reconcile(session, runs, reply, now)
                _remember(session, runs, reply, now)
            elif status and task in runs:
                _end(session, runs, task, now)
            else:
                _reconcile(session, runs, reply, now)

    if name in ("task_claim", "task_update"):
        _transact(session, apply)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
