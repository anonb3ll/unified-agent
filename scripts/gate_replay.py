#!/usr/bin/env python3
"""Soak-week gate replay report (design doc 2026-07-23).

Read-only over `approvals`/`agent_perf_samples`. Classifies real approval-gate
history into a three-bucket auto/notify/hard-block model and cross-tabs
`fs_write` call volume, as evidence for the phase-3 (conditional `fs_write`
gating) decision reviewed around 2026-08-01.

Never writes raw command/payload text anywhere in its output -- only a safe
summary per row (tool, status, bucket, timestamp). Regex-based redaction was
considered and rejected (see design doc "NOT in scope") in favor of this
allowlist-summary approach, matching this repo's existing "no PII redaction
in v1" stance (`export.py`). A payload hash+length was considered too, but
dropped after red-team review: `approvals.payload` is low-entropy, fixed-
structure JSON, so hash+length together work as an offline dictionary-attack
confirmation oracle -- close enough to a leak that it isn't worth the minor
debugging convenience.

Usage: uv run python scripts/gate_replay.py
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime

from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config

SINCE_DEFAULT = "2026-07-18"
UNGATED_TARGET_TOOL = "fs_write"

# The shell-verb heuristic below only makes sense for run_cmd -- its payload
# is {"command": <shell string>}. Every other currently-gated tool sends a
# different shape (delegate: task_spec free text; git_commit_staged: a commit
# message; workflow_run: never appears in `approvals` at all -- see
# format_report's caveat) that isn't a shell command, so running the same
# verb-substring scan over it produces noise, not signal (adversarial review
# finding: a delegate task_spec containing the English word "echo" would
# misclassify as "auto"). Rows for other tools get this fixed bucket instead.
_NON_COMMAND_BUCKET = "n/a (not a shell command)"

# First-pass reversibility heuristic (design doc Open Question #1) -- expected
# to need iteration once run against more real data than the current sample.
# Patterns are matched as regexes (not bare substrings) so idiomatic
# no-space shell syntax (e.g. `>/dev/sda`) can't evade a literal-space match.
_AUTO_PATTERNS = (
    r"\bls\b",
    r"\bcat\b",
    r"\bgrep\b",
    r"\bfind\b",  # hard-block's -delete\b is checked first, so `find ... -delete` still wins
    r"\becho\b",
    r"\bgit status\b",
    r"\bgit diff\b",
    r"\bgit log\b",
)
_NOTIFY_PATTERNS = (
    r"\bgit commit\b",
    r"\bmv\b",
    r"\bchmod\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bmkdir\b",
)
_HARD_BLOCK_PATTERNS = (
    r"\brm\b",
    r"\bdd\b",
    r"\bmkfs",
    r">\s*/dev/",
    r"\bshred\b",
    r"-delete\b",
    r"git push\s+--force",
    r"git reset\s+--hard",
)


def gated_tools() -> tuple[str, ...]:
    """Live gated-tool list from UA_APPROVAL_TOOLS -- not a hardcoded snapshot, so the
    report always matches the actual gate config instead of silently drifting from it
    (e.g. once fs_write is added for phase 3)."""
    return tuple(sorted(config.approval_gated_tools()))


def _connect_ro() -> sqlite3.Connection:
    """Read-only connection via the `mode=ro` URI, mirrors sleep_pass.py's `_connect_ro`."""
    conn = sqlite3.connect(f"file:{config.db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def classify_command(payload_text: str) -> str:
    """Three-bucket heuristic classifier for a shell command string: "auto", "notify",
    or "hard-block". Only meaningful for run_cmd's payload -- see _NON_COMMAND_BUCKET.

    Explicit default is "hard-block" -- an unrecognized or empty payload never
    silently falls through to "auto". Patterns are regexes (word-boundary and
    whitespace-tolerant) so idiomatic no-space shell syntax can't evade a match.
    """
    text = payload_text.lower()
    if not text.strip():
        return "hard-block"
    for pattern in _HARD_BLOCK_PATTERNS:
        if re.search(pattern, text):
            return "hard-block"
    for pattern in _NOTIFY_PATTERNS:
        if re.search(pattern, text):
            return "notify"
    for pattern in _AUTO_PATTERNS:
        if re.search(pattern, text):
            return "auto"
    return "hard-block"  # explicit default -- never a silent auto-approve


def summarize_approval(row: sqlite3.Row) -> dict:
    """Extract only safe summary fields -- the raw `payload` text never leaves this function.

    Deliberately excludes a payload hash/length too (see module docstring): for
    low-entropy, fixed-structure JSON, hash+length together let a reader with
    vault access dictionary-guess-and-confirm the original command offline.

    The shell-verb bucket is only computed for run_cmd (the only gated tool whose
    payload is actually a shell command) -- every other tool gets a fixed
    non-command bucket instead of running the same heuristic over unrelated free
    text (adversarial review finding: a delegate task_spec or git commit message
    containing an incidental shell-verb word would otherwise misclassify).
    """
    payload_text = row["payload"] or ""
    bucket = classify_command(payload_text) if row["tool"] == "run_cmd" else _NON_COMMAND_BUCKET
    return {
        "tool": row["tool"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "bucket": bucket,
    }


def fetch_approvals(conn: sqlite3.Connection, since: str, tools: tuple[str, ...]) -> list[dict]:
    if not tools:
        return []
    placeholders = ",".join("?" for _ in tools)
    rows = conn.execute(
        f"SELECT tool, status, payload, created_at, resolved_at FROM approvals "
        f"WHERE tool IN ({placeholders}) AND created_at >= ? ORDER BY created_at",
        (*tools, since),
    ).fetchall()
    return [summarize_approval(r) for r in rows]


def fetch_perf_counts(conn: sqlite3.Connection, since: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT tool, COUNT(*) AS n FROM agent_perf_samples WHERE ts >= ? GROUP BY tool "
        "ORDER BY n DESC",
        (since,),
    ).fetchall()
    return {r["tool"]: r["n"] for r in rows}


def format_report(
    summaries: list[dict], perf_counts: dict[str, int], since: str, tools: tuple[str, ...]
) -> str:
    lines = [
        "---",
        "type: gate-replay-report",
        f"generated: {datetime.now(UTC).isoformat()}",
        "---",
        "",
        "# Gate Replay Report",
        "",
        "Evidence for the phase-3 (`fs_write` conditional gating) decision, review ~2026-08-01.",
        f"Window: `approvals`/`agent_perf_samples` since {since}.",
        "",
        f"**Sample size: {len(summaries)} `approvals` rows. This is directional, not "
        "statistically robust -- treat as a starting point for the 08-01 conversation, "
        "not a conclusive answer.**",
        "",
        "No raw command/payload text -- or any derived fingerprint of it -- appears "
        "anywhere in this report -- only tool, status, bucket, and timestamps per row. The "
        f"bucket is only a real auto/notify/hard-block classification for `run_cmd`; other "
        f'tools show "{_NON_COMMAND_BUCKET}" since their payload is not a shell command.',
        "",
        f"## Approval History (all {len(tools)} currently-gated tools)",
        "",
        "**Note on `workflow_run`:** rows for this tool will always be zero -- the whole-"
        "workflow invocation is never itself gated by name; individual workflow steps are "
        "gated under their OWN tool name instead (e.g. a step calling `delegate` produces a "
        "`tool='delegate'` row, not `tool='workflow_run'`). Pre-existing behavior, not a bug "
        "in this report.",
        "",
        "| tool | status | bucket | created_at | resolved_at |",
        "|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['tool']} | {s['status']} | {s['bucket']} | {s['created_at']} | "
            f"{s['resolved_at'] or '-'} |"
        )
    if not summaries:
        lines.append("| _(no rows in window)_ | | | | |")

    bucket_counts: dict[str, int] = {}
    for s in summaries:
        bucket_counts[s["bucket"]] = bucket_counts.get(s["bucket"], 0) + 1
    lines += [
        "",
        "## Bucket Summary",
        "",
        "| bucket | count |",
        "|---|---|",
    ]
    for bucket in ("auto", "notify", "hard-block", _NON_COMMAND_BUCKET):
        lines.append(f"| {bucket} | {bucket_counts.get(bucket, 0)} |")

    lines += [
        "",
        "## fs_write Call Volume (ungated tool, the one phase-3 is about)",
        "",
        f"`{UNGATED_TARGET_TOOL}` calls in window: **{perf_counts.get(UNGATED_TARGET_TOOL, 0)}**",
        "",
        "**Caveat:** this is raw call volume only. `agent_perf_samples` has no path/payload "
        "field, so it cannot show what fraction of these calls would trip the "
        '"outside a worktree" boundary phase-3 is actually about -- that data does not exist '
        "yet (tracked as a deferred TODO in `TODOS.md`). Treat this number as call frequency "
        "context, not a worktree-boundary rate.",
        "",
        "## Other Gated Tools -- Call Volume",
        "",
        "| tool | agent_perf_samples count |",
        "|---|---|",
    ]
    for tool in tools:
        lines.append(f"| {tool} | {perf_counts.get(tool, 0)} |")

    return "\n".join(lines) + "\n"


def write_report(markdown: str) -> str | None:
    """Write to the vault inbox; on failure, fall back to printing to stdout.

    Calls `resolve_in_vault` directly rather than the `vault_write` MCP tool,
    matching sleep_pass.py's `_write_artifact` pattern -- this is a standalone
    script, not a hub-served tool call.
    """
    name = f"Gate-Replay-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
    try:
        from unified_agent.safety import resolve_in_vault

        target = resolve_in_vault(f"00 - Inbox/{name}", for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        return str(target)
    except (ToolError, OSError) as exc:
        print(
            f"vault_write failed ({exc}); falling back to stdout so the evidence isn't lost:\n",
            file=sys.stderr,
        )
        print(markdown)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=SINCE_DEFAULT, help=f"ISO date, default {SINCE_DEFAULT}")
    args = parser.parse_args()

    try:
        conn = _connect_ro()
    except sqlite3.OperationalError as exc:
        print(f"error: could not open the memory DB read-only ({exc})", file=sys.stderr)
        return 1

    tools = gated_tools()
    try:
        summaries = fetch_approvals(conn, args.since, tools)
        perf_counts = fetch_perf_counts(conn, args.since)
    finally:
        conn.close()

    markdown = format_report(summaries, perf_counts, args.since, tools)
    path = write_report(markdown)
    if path:
        print(f"Gate replay report written to: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
