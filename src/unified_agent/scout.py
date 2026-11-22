"""FirstMate-style scout task shape (task #1027).

Scout dispatches investigate and leave a standalone report plus a captain-facing
**decision inventory** — no PR branch, no authorized code change. This is the
executor/task-queue counterpart to Hermes ``research-brief`` (an ad-hoc COS
skill): scout is a first-class shape recognized by note markers and the
finish/review path.

Intake::

    [task-shape: scout]

    # or the short form
    [scout]

Finish stamps ``[no-code]``, ``[scout: report=<vault-rel-path>]``, and
``[waiting-for: review]``. The review pipeline validates the report artifact
(decision inventory present) and notifies the captain — it never merges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

SHAPE_MARKER = "[task-shape: scout]"
SHORT_SHAPE_MARKER = "[scout]"
NO_CODE_MARKER = "[no-code]"
WAITING_FOR_REVIEW = "[waiting-for: review]"
DECISION_INVENTORY_HEADING = "Decision inventory"

_SHAPE_RE = re.compile(
    r"(?<!`)"
    r"(?:\[task-shape:\s*scout\]|\[scout\])"
    r"(?!`)",
    re.IGNORECASE,
)
_REPORT_MARKER_RE = re.compile(
    r"\[scout:\s*report=(?P<path>[^\]]+)\]",
    re.IGNORECASE,
)
_DECISION_HEADING_RE = re.compile(
    rf"^##\s+{re.escape(DECISION_INVENTORY_HEADING)}\b",
    re.IGNORECASE | re.MULTILINE,
)
_LIVE_REVIEW_RE = re.compile(r"\[waiting-for: review\](?! when done)", re.IGNORECASE)


@dataclass(frozen=True)
class DecisionItem:
    """One captain-actionable decision row."""

    decision: str
    options: list[str]
    recommendation: str | None = None
    status: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "options": list(self.options),
            "recommendation": self.recommendation,
            "status": self.status,
        }


def is_scout(notes: str | None) -> bool:
    """True when task notes declare the scout shape."""
    return bool(_SHAPE_RE.search(notes or ""))


def report_marker(path: str) -> str:
    """Canonical finish marker pointing at the vault-relative report path."""
    cleaned = (path or "").strip()
    return f"[scout: report={cleaned}]"


def report_path_from_notes(notes: str | None) -> str | None:
    """Extract the scout report path from notes, if stamped."""
    match = _REPORT_MARKER_RE.search(notes or "")
    if not match:
        return None
    path = match.group("path").strip()
    return path or None


def report_path_from_text(text: str | None) -> str | None:
    """Extract a scout report path from agent output / notes hybrid text."""
    if not text:
        return None
    # Prefer an explicit marker; fall back to a vault: evidence-style pointer.
    marked = report_path_from_notes(text)
    if marked:
        return marked
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("vault:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def evidence_ref_for_report(path: str) -> str:
    """Evidence pointer stored on the task row (not a commit SHA / PR URL)."""
    cleaned = (path or "").strip()
    if cleaned.lower().startswith("vault:"):
        return cleaned
    return f"vault:{cleaned}"


def report_path_from_evidence(evidence_ref: str | None) -> str | None:
    """Invert :func:`evidence_ref_for_report`."""
    raw = (evidence_ref or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("vault:"):
        return raw.split(":", 1)[1].strip() or None
    # Already a vault-relative path (legacy / hand-stamped).
    if raw.endswith(".md") and "/" in raw:
        return raw
    return None


def render_decision_inventory(items: list[DecisionItem] | list[dict[str, Any]]) -> str:
    """Render a captain-actionable decision inventory as Markdown.

    Each item needs a decision prompt and at least one option. Empty input
    still emits the heading so validators can detect a missing inventory body.
    """
    lines = [f"## {DECISION_INVENTORY_HEADING}", ""]
    if not items:
        lines.append("_No decisions filed — scout incomplete._")
        lines.append("")
        return "\n".join(lines)

    for idx, raw in enumerate(items, start=1):
        if isinstance(raw, DecisionItem):
            item = raw
        else:
            opts = raw.get("options") or []
            if isinstance(opts, str):
                opts = [o.strip() for o in opts.split("/") if o.strip()]
            item = DecisionItem(
                decision=str(raw.get("decision") or raw.get("prompt") or "").strip(),
                options=[str(o).strip() for o in opts if str(o).strip()],
                recommendation=(
                    str(raw["recommendation"]).strip()
                    if raw.get("recommendation") is not None
                    else None
                ),
                status=str(raw.get("status") or "pending").strip() or "pending",
            )
        if not item.decision:
            continue
        options_txt = " / ".join(item.options) if item.options else "(none listed)"
        lines.append(f"{idx}. **Decision:** {item.decision}")
        lines.append(f"   - Options: {options_txt}")
        if item.recommendation:
            lines.append(f"   - Recommendation: {item.recommendation}")
        lines.append(f"   - Status: {item.status}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_scout_report(
    *,
    topic: str,
    bottom_line: str,
    findings: list[str],
    decisions: list[DecisionItem] | list[dict[str, Any]],
    caveats: list[str] | None = None,
    sources: list[str] | None = None,
    task_id: int | None = None,
    report_date: str | None = None,
) -> str:
    """Assemble a full scout report Markdown document."""
    day = report_date or date.today().isoformat()
    fm_topic = topic.strip() or "untitled"
    lines = [
        "---",
        f"title: Scout - {fm_topic} {day}",
        f"date: {day}",
        "source: unified-agent-scout",
        f"topic: {fm_topic}",
        "tags: [scout, research, decision-inventory]",
    ]
    if task_id is not None:
        lines.append(f"task_id: {task_id}")
    lines.extend(["---", "", f"# Scout report — {fm_topic}", ""])
    lines.append("## Bottom line")
    lines.append("")
    lines.append((bottom_line or "").strip() or "_No bottom line provided._")
    lines.append("")
    lines.append("## Key findings")
    lines.append("")
    if findings:
        for finding in findings:
            text = str(finding).strip()
            if text:
                lines.append(f"- {text}")
    else:
        lines.append("- _(none)_")
    lines.append("")
    lines.append(render_decision_inventory(decisions).rstrip())
    lines.append("")
    lines.append("## Caveats / open questions")
    lines.append("")
    if caveats:
        for caveat in caveats:
            text = str(caveat).strip()
            if text:
                lines.append(f"- {text}")
    else:
        lines.append("- _(none)_")
    lines.append("")
    lines.append("## Sources")
    lines.append("")
    if sources:
        for src in sources:
            text = str(src).strip()
            if text:
                lines.append(f"- {text}")
    else:
        lines.append("- _(none)_")
    lines.append("")
    return "\n".join(lines)


def default_report_relpath(topic: str, *, report_date: str | None = None) -> str:
    """Suggested vault-relative path under the inbox write allowlist."""
    day = report_date or date.today().isoformat()
    safe = re.sub(r"[^\w\s\-]+", "", (topic or "investigation").strip(), flags=re.UNICODE)
    safe = re.sub(r"\s+", " ", safe).strip() or "investigation"
    return f"00 - Inbox/Scout - {safe} {day}.md"


def validate_scout_report(markdown: str | None) -> list[str]:
    """Return a list of structural problems; empty means captain-ready."""
    text = markdown or ""
    problems: list[str] = []
    if not text.strip():
        return ["report is empty"]
    if not _DECISION_HEADING_RE.search(text):
        problems.append(f"missing '## {DECISION_INVENTORY_HEADING}' section")
        return problems
    # Require at least one numbered decision row under the heading.
    after = _DECISION_HEADING_RE.split(text, maxsplit=1)
    body = after[-1] if len(after) > 1 else ""
    # Cut at the next ## heading so we only inspect the inventory section.
    next_heading = re.search(r"^##\s+", body, re.MULTILINE)
    section = body[: next_heading.start()] if next_heading else body
    if re.search(r"_No decisions filed", section, re.IGNORECASE):
        problems.append("decision inventory has no actionable items")
    elif not re.search(r"^\s*\d+\.\s+\*\*Decision:\*\*", section, re.MULTILINE):
        # Also accept a compact table row style.
        if not re.search(r"\|\s*\d+\s*\|", section):
            problems.append("decision inventory has no actionable items")
    return problems


def annotate_shape(notes: str | None) -> str:
    """Ensure notes carry a live scout shape marker."""
    text = notes or ""
    if is_scout(text):
        return text
    body = text.rstrip()
    if body:
        return f"{body}\n\n{SHAPE_MARKER}"
    return SHAPE_MARKER


def apply_scout_finish(notes: str | None, report_path: str, *, fresh_review: bool = False) -> str:
    """Stamp scout finish markers: no-code, report path, waiting-for review.

    Does **not** stamp delivery prefer-pr / local-branch hints — scout never
    opens a PR branch. A new executor handoff requests fresh review after any
    historical review outcome, preserving the prior request/resolution text.
    """
    text = annotate_shape(notes)
    marker = report_marker(report_path)

    # Replace only report tokens; preserve prose sharing their lines.
    body = _REPORT_MARKER_RE.sub("", text).rstrip()
    stamped = f"{body}\n\n{marker}" if body else marker

    if NO_CODE_MARKER not in stamped:
        stamped = stamped.rstrip() + f"\n{NO_CODE_MARKER}"
    if fresh_review or not _LIVE_REVIEW_RE.search(stamped):
        stamped = stamped.rstrip() + f"\n\n{WAITING_FOR_REVIEW}"
    # Explicit no-PR posture for humans reading the notes.
    if "[scout: no-pr]" not in stamped:
        stamped = stamped.rstrip() + "\n[scout: no-pr] investigation report only — do not open a PR"
    return stamped


def scout_executor_prompt(*, task_id: int, task_spec: str, from_agent: str) -> str:
    """Prompt addon for the active executor when the claimed task is a scout."""
    suggested = default_report_relpath(f"task-{task_id}")
    return (
        f"You are executing delegated SCOUT task #{task_id} from {from_agent}.\n\n"
        "This is a FirstMate-style **scout** (investigation only).\n"
        "Hard rules:\n"
        "- Do NOT open a PR, push a feature branch for merge, or land code changes.\n"
        "- Investigate, then write a standalone report to the vault inbox via vault_write.\n"
        "- The report MUST include a '## Decision inventory' section with numbered\n"
        "  captain-actionable decisions (Decision / Options / Recommendation / Status).\n"
        f"- Suggested path: {suggested}\n"
        "- End your reply with the exact marker line:\n"
        f"  [scout: report={suggested}]\n"
        "  (adjust the path if you chose a better inbox filename).\n\n"
        f"TASK INSTRUCTIONS:\n{task_spec}\n\n"
        "Work strictly within the workspace/worktree context. Do not escape your sandbox."
    )
