"""Per-agent delegation ledger in a monthly vault page (UA #703, Buzz-emulation #2).

After every review-pipeline run the CLI appends one line per agent per task —
``- <ts> | <agent> | #<task> | <verdict> | ci:<state>`` — to
``Logs/Delegation Ledger <YYYY-MM>.md`` in the vault. The month is derived at
write time, so the page rolls over automatically with the calendar month: a
run on Oct 1 creates the October page and never touches September's.

Append-only guarantees:

- Existing pages are only ever appended to (``vault_write(..., append=True)``).
- A month page that doesn't exist yet is bootstrapped with a minimal header,
  then the lines are written in the same pass.
- A locked page (frontmatter ``locked: true``) is left alone — the vault
  write refuses and the refusal propagates; the ledger never forces.

Best-effort by contract: the caller (``review_cli.main``) treats any ledger
failure as a stderr warning that never changes a verdict or the exit code.
"""

import re
from datetime import UTC, datetime

from mcp.server.mcpserver.exceptions import ToolError

from . import storage
from .tools.vault import REVISION_ABSENT, VaultRevisionConflict, vault_read, vault_write

LEDGER_PAGE_FMT = "Logs/Delegation Ledger {ym}.md"

# Review-pipeline verdict → CI-state label for the ledger line. Verdicts that
# carry a real gate outcome map to green/red-tier/red; anything else (the
# plan-approval consumer's verdicts, reconcile's ``unresolved-repo``) has no
# CI dimension and maps to ``n/a``.
CI_STATES = {
    "validated": "green",
    "merged": "green",
    "merged-upstream": "green",
    "dry-run-ok": "green",
    "unchanged": "green",
    "stale": "green",
    "red-tier": "red-tier",
    "failed": "red",
    # Task #1254: the gates never finished (pytest hit the wall-clock ceiling),
    # so there is no red/green outcome to report — explicit here rather than
    # relying on the "n/a" fallback below, since this is a deliberate choice
    # and not an unmapped verdict.
    "timeout": "n/a",
}

# Fallback attribution when the task row carries no claim at all.
UNKNOWN_AGENT = "unknown"

# ``claimed-by: <label>`` note marker (appended by task_claim).
_CLAIMED_BY_RX = re.compile(r"(?m)^\s*claimed-by:\s*(\S+)")

# vault_write's missing-note signal, matched by prefix before bootstrapping.
_NO_SUCH_NOTE = "no such note"


def _now() -> datetime:
    """Wall clock, module-level so tests can pin the month under rollover."""
    return datetime.now(UTC)


def _agent_for_task(task_id: int) -> str:
    """Attribution for a ledger line: structured claim, then notes marker.

    ``tasks.claimed_by`` is the sole authoritative writer (task_claim); the
    ``claimed-by:`` note marker is the fallback for rows whose claim was
    cleared or written before the column existed. No claim at all →
    ``unknown`` — a ledger line is never fabricated.
    """
    conn = storage.connect()
    try:
        row = conn.execute("SELECT claimed_by, notes FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return UNKNOWN_AGENT
    claimed = (row["claimed_by"] or "").strip()
    if claimed:
        return claimed
    m = _CLAIMED_BY_RX.search(row["notes"] or "")
    if m:
        return m.group(1)
    return UNKNOWN_AGENT


def _ci_state(verdict: str) -> str:
    return CI_STATES.get(verdict, "n/a")


def ledger_line(task_id: int, verdict: str, now: datetime | None = None) -> str:
    """One ledger line: agent, task id, verdict, CI state (timestamped).

    Raises ``ValueError`` on an empty/absent task id or verdict so a malformed
    result can never land as a blank or truncated line.
    """
    if not task_id or int(task_id) <= 0:
        raise ValueError(f"ledger line needs a real task id, got {task_id!r}")
    verdict = (verdict or "").strip()
    if not verdict:
        raise ValueError("ledger line needs a non-empty verdict")
    agent = _agent_for_task(int(task_id))
    ts = (now or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"- {ts} | {agent} | #{int(task_id)} | {verdict} | ci:{_ci_state(verdict)}"


def _month_header(ym: str) -> str:
    return (
        f"# Delegation Ledger — {ym}\n"
        "\n"
        "One line per agent per review-pipeline outcome (UA #703). Append-only.\n"
        "Format: `- <ts> | <agent> | #<task> | <verdict> | ci:<state>`\n"
        "\n"
    )


def _page_exists(page: str) -> bool:
    """True if the ledger page already exists in the vault (read probe)."""
    try:
        vault_read(page)
        return True
    except ToolError as e:
        if _NO_SUCH_NOTE in str(e):
            return False
        raise


def append_review_ledger(results: list[dict], now: datetime | None = None) -> dict:
    """Append one ledger line per review result to the month's vault page.

    Only results that actually changed state (truthy ``notification``) get a
    line — the validate+hold path re-reports held tasks as ``unchanged`` on
    every cron tick with no notification, and the ledger records outcomes,
    not polls. A missing month page is bootstrapped with a minimal header in
    the same write; an existing page is only ever appended to (and never
    forced past a frontmatter lock). Returns ``{page, appended, created}``.
    """
    lines = []
    for r in results or []:
        if not r.get("notification"):
            continue
        lines.append(ledger_line(r["task_id"], r["verdict"], now=now))
    if not lines:
        return {"page": None, "appended": 0, "created": False}

    ts = now or _now()
    ym = ts.strftime("%Y-%m")
    page = LEDGER_PAGE_FMT.format(ym=ym)
    payload = "\n".join(lines) + "\n"
    created = False
    if _page_exists(page):
        vault_write(page, payload, append=True)
    else:
        # Create-only (task #2111): if another writer created the month page
        # since the probe, append to it instead of overwriting its lines.
        try:
            vault_write(page, _month_header(ym) + payload, expected_revision=REVISION_ABSENT)
            created = True
        except VaultRevisionConflict:
            vault_write(page, payload, append=True)
    return {"page": page, "appended": len(lines), "created": created}
