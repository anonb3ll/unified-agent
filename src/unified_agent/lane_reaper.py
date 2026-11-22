"""Reap orphaned Codex-lane child processes that survive dead lanes (task #1316).

Desktop incident 2026-09-23 (Desktop_Agent5): 55 orphaned ``codex-linux-sandbox``
and ``pytest`` children from dead Codex lanes held ~2.8 GB RAM, driving swap to
3.7/4 GB and load to 6.4 on a 4-core host. Manual reap recovered ~3 GB.

This module is the durable fix: classify orphans from a process snapshot and
optionally SIGTERM them. Classification is intentionally conservative:

- Targets only ``codex-linux-sandbox`` and real pytest invocations
  (``pytest`` argv0 or ``python -m pytest``) whose cwd (or argv path) sits
  under a known lane worktree root (``~/.codex/worktrees``, ``*/.worktrees/``,
  ``codex/task-``). Non-lane sandboxes are ignored.
- Never targets a process that still has a live "keeper" ancestor
  (Cursor / Claude / Codex CLI / Agy / Antigravity / OpenCode host).
- Requires the process to be reparented to PID 1 / systemd, or to have a dead
  parent, or to sit under an already-classified orphan **or shell under an
  orphan** (so sandbox → zsh → pytest trees still clear).
- Before SIGTERM, re-reads ``/proc/<pid>/stat`` starttime and refuses to signal
  if the PID was reused.

Dry-run is the default for the CLI; ``--kill`` performs the SIGTERM.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

# argv0 basenames (and a few substrings) that mean a live agent host owns the tree.
KEEPER_BINARIES: frozenset[str] = frozenset(
    {
        "cursor",
        "cursor-agent",
        "claude",
        "codex",
        "codex-cli",
        "agy",
        "antigravity",
        "opencode",
        "hermes-agent",
        "hermes",
    }
)
KEEPER_SUBSTRINGS: tuple[str, ...] = (
    "Cursor Helper",
    "cursor-server",
)

SANDBOX_MARKERS: tuple[str, ...] = ("codex-linux-sandbox",)

LANE_PATH_MARKERS: tuple[str, ...] = (
    "/.codex/worktrees/",
    "/.worktrees/",
    "/codex/task-",
    "/codex/worktrees/",
)

SHELL_BINARIES: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "dash", "fish", "uv", "python", "python3"}
)


@dataclass(frozen=True)
class Proc:
    """One process row from a snapshot (pid, ppid, cmdline, optional cwd/starttime)."""

    pid: int
    ppid: int
    cmdline: str
    cwd: str = ""
    starttime: int | None = None  # /proc/<pid>/stat field 22 (clock ticks)


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _argv(cmdline: str) -> list[str]:
    return [t for t in (cmdline or "").split() if t]


def _argv0_base(cmdline: str) -> str:
    tokens = _argv(cmdline)
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1]


def _haystack(proc: Proc) -> str:
    """Paths we trust for lane ownership: cwd first, then argv tokens."""
    parts = [proc.cwd or ""]
    parts.extend(_argv(proc.cmdline))
    return " ".join(parts)


def in_lane_worktree(proc: Proc) -> bool:
    hay = _haystack(proc)
    return any(m in hay for m in LANE_PATH_MARKERS)


def is_sandbox(proc: Proc) -> bool:
    cmd = _norm(proc.cmdline)
    if not any(m in cmd for m in SANDBOX_MARKERS):
        return False
    # Devin #758: never treat a non-lane sandbox as a target.
    return in_lane_worktree(proc)


def is_lane_pytest(proc: Proc) -> bool:
    """True only for a real pytest runner inside a lane worktree."""
    if not in_lane_worktree(proc):
        return False
    tokens = _argv(proc.cmdline)
    if not tokens:
        return False
    base = tokens[0].rsplit("/", 1)[-1]
    if base in {"pytest", "py.test"}:
        return True
    # python[-X] -m pytest …
    for i, tok in enumerate(tokens[:-1]):
        b = tok.rsplit("/", 1)[-1]
        if b.startswith("python") and tokens[i + 1] == "-m":
            if i + 2 < len(tokens) and tokens[i + 2] == "pytest":
                return True
    return False


def is_target(proc: Proc) -> bool:
    return is_sandbox(proc) or is_lane_pytest(proc)


def is_shellish(proc: Proc) -> bool:
    """Intermediate shells / uv wrappers between a sandbox and pytest."""
    base = _argv0_base(proc.cmdline)
    if base in SHELL_BINARIES:
        return True
    # `uv run …` wrapper
    return base == "uv"


def is_keeper(proc: Proc) -> bool:
    # Exclude the sandbox binary even if its argv0 basename is "codex-…".
    if any(m in _norm(proc.cmdline) for m in SANDBOX_MARKERS):
        return False
    cmd = _norm(proc.cmdline)
    base = _argv0_base(cmd)
    if base in KEEPER_BINARIES:
        return True
    return any(m in cmd for m in KEEPER_SUBSTRINGS)


def ancestors(proc: Proc, by_pid: dict[int, Proc], *, limit: int = 64) -> list[Proc]:
    out: list[Proc] = []
    seen: set[int] = set()
    cur = proc
    for _ in range(limit):
        parent = by_pid.get(cur.ppid)
        if parent is None or parent.pid in seen:
            break
        seen.add(parent.pid)
        out.append(parent)
        cur = parent
    return out


def has_keeper_ancestor(proc: Proc, by_pid: dict[int, Proc]) -> bool:
    return any(is_keeper(a) for a in ancestors(proc, by_pid))


def parent_missing_or_init(proc: Proc, by_pid: dict[int, Proc]) -> bool:
    if proc.ppid <= 1:
        return True
    return proc.ppid not in by_pid


def classify_orphans(procs: list[Proc]) -> list[Proc]:
    """Return target processes that are safe to reap, children before parents.

    Shells under PID 1 are *not* seeded on their own: a live detached shell can
    still supervise a legitimate lane pytest (Devin on #758). Bridges are only
    walked downward from an already-orphaned *target* (typically a sandbox).
    """
    by_pid = {p.pid: p for p in procs}
    orphan_pids: set[int] = set()

    # Pass 1: only targets reparented to init / with a dead parent.
    for p in procs:
        if not is_target(p):
            continue
        if has_keeper_ancestor(p, by_pid):
            continue
        if parent_missing_or_init(p, by_pid):
            orphan_pids.add(p.pid)

    # Pass 2: targets and shellish wrappers under an orphan — closes
    # sandbox → zsh/uv → pytest trees without treating bare init→zsh as dead.
    changed = True
    while changed:
        changed = False
        for p in procs:
            if p.pid in orphan_pids:
                continue
            if has_keeper_ancestor(p, by_pid):
                continue
            if p.ppid not in orphan_pids:
                continue
            if is_target(p) or is_shellish(p):
                orphan_pids.add(p.pid)
                changed = True

    # Only report killable targets (sandbox/pytest), not intermediate shells.
    orphans = [by_pid[pid] for pid in orphan_pids if pid in by_pid and is_target(by_pid[pid])]
    orphans.sort(key=lambda p: (-_depth(p, by_pid), p.pid))
    return orphans


def _depth(proc: Proc, by_pid: dict[int, Proc]) -> int:
    return len(ancestors(proc, by_pid))


def read_proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def read_proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return ""


def read_proc_ppid(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def read_proc_starttime(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat (starttime, clock ticks since boot)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    # comm may contain spaces/parens — split after the closing ')'.
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    # After ')': state(1) ppid(2) … starttime is overall field 22 → index 19
    # in the post-')' list (1-indexed field 3 in that list is state…).
    # post-')' fields: [0]=state [1]=ppid … [19]=starttime (field 22).
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def snapshot_self() -> list[Proc]:
    """Build a process snapshot from /proc (Linux)."""
    procs: list[Proc] = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = read_proc_ppid(pid)
        if ppid is None:
            continue
        procs.append(
            Proc(
                pid=pid,
                ppid=ppid,
                cmdline=read_proc_cmdline(pid),
                cwd=read_proc_cwd(pid),
                starttime=read_proc_starttime(pid),
            )
        )
    return procs


def _pid_still_matches(proc: Proc) -> bool:
    """Refuse to signal if the PID was reused (starttime / cmdline mismatch)."""
    live_start = read_proc_starttime(proc.pid)
    if proc.starttime is not None and live_start is not None and live_start != proc.starttime:
        return False
    live_cmd = read_proc_cmdline(proc.pid)
    if not live_cmd:
        return False
    # Require the same target class still holds on the live process.
    live = Proc(
        pid=proc.pid,
        ppid=proc.ppid,
        cmdline=live_cmd,
        cwd=read_proc_cwd(proc.pid),
        starttime=live_start,
    )
    return is_target(live)


def reap(orphans: list[Proc], *, kill: bool, sig: int = signal.SIGTERM) -> list[dict]:
    """SIGTERM each orphan when kill=True; always return a report row per pid."""
    report: list[dict] = []
    for p in orphans:
        row = {
            "pid": p.pid,
            "ppid": p.ppid,
            "cmdline": p.cmdline[:200],
            "cwd": p.cwd,
            "starttime": p.starttime,
            "action": "kill" if kill else "dry-run",
            "ok": True,
            "error": "",
        }
        if kill:
            if not _pid_still_matches(p):
                row["ok"] = True
                row["error"] = "pid-reused-or-gone"
                row["action"] = "skipped"
                report.append(row)
                continue
            try:
                os.kill(p.pid, sig)
            except ProcessLookupError:
                row["ok"] = True
                row["error"] = "already-gone"
            except PermissionError as exc:
                row["ok"] = False
                row["error"] = str(exc)
        report.append(row)
    return report


def run(*, kill: bool = False) -> dict:
    procs = snapshot_self()
    orphans = classify_orphans(procs)
    report = reap(orphans, kill=kill)
    return {
        "scanned": len(procs),
        "orphans": len(orphans),
        "killed": sum(1 for r in report if r["action"] == "kill" and r["ok"]),
        "report": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap orphaned Codex-lane sandbox/pytest children (UA #1316)."
    )
    parser.add_argument(
        "--kill",
        action="store_true",
        help="SIGTERM classified orphans (default is dry-run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full report as JSON",
    )
    args = parser.parse_args(argv)
    result = run(kill=args.kill)
    if args.json:
        import json

        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        mode = "kill" if args.kill else "dry-run"
        print(
            f"lane-reaper ({mode}): scanned={result['scanned']} "
            f"orphans={result['orphans']} killed={result['killed']}"
        )
        for row in result["report"]:
            flag = {"kill": "KILL", "dry-run": "WOULD", "skipped": "SKIP"}.get(
                row["action"], row["action"].upper()
            )
            print(f"  {flag} pid={row['pid']} ppid={row['ppid']} {row['cmdline']!r}")
    return 0 if all(r.get("ok", True) for r in result["report"]) else 1


if __name__ == "__main__":
    sys.exit(main())
