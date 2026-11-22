"""Read-only weekly plans and decision packets derived from the task ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, org_partition, storage
from .safety import guard_hook_dir, guard_secret_filename, resolve_in_vault

_PROJECT_SPEC = re.compile(r"(?<!\`)\[project-spec\](?!\`)")
_PRODUCT_ONE = re.compile(r"(?<!\`)\[product-1\](?!\`)", re.IGNORECASE)
_COVERS = re.compile(r"\[covers:\s*(.*?)\s*\]", re.IGNORECASE)
_DECISION_ITEM = re.compile(r"(?<!\`)\[decision-packet item\](?!\`)", re.IGNORECASE)
_RECOMMENDATION = re.compile(r"(?im)^\s*(?:recorded )?recommendation\s*:\s*(.+?)\s*$")
_DECISION = re.compile(r"(?im)^\s*decision\s*:\s*(yes|no|defer)\s*$")
_DOD_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:definition of done|dod)(?:\s*\([^)]*\))?\s*:\s*(.*)$",
    re.I,
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_ROLLUP_ORDER = ("blocked", "doing", "waiting_review", "open")
_PLAN_BEGIN = "<!-- planning-loop:begin -->"
_PLAN_END = "<!-- planning-loop:end -->"

# --- next-work projection (task #1468) --------------------------------------
# Reads docs/queue/NEXT-WORK.json (the maintained implementation-sequence
# manifest) from the cockpit workspace and projects it onto live hub task
# state: status, lane, claims and the task_dependencies graph. Pure read —
# no task store, no scheduler, no second queue. The live hub stays
# authoritative; the manifest only orders and groups candidate work.
_NEXT_WORK_MANIFEST_RELATIVE = Path("docs/queue/NEXT-WORK.json")
_NEXT_WORK_MARKDOWN_RELATIVE = Path("docs/queue/NEXT-WORK.md")
_NEXT_WORK_VAULT_MIRROR = "Wiki/Unified-Agent-Implementation-Sequence.md"
_NEXT_WORK_SCHEMA_VERSION = 1
_NEXT_WORK_TERMINAL_STATUSES = frozenset({"done", "cancelled"})
# `blocked`/`waiting_review` are non-open, non-terminal lifecycle states that
# `task_update` can set without a claim (Devin #4132982304): they always
# describe work that isn't ready, independent of the claim/lease check.
# `doing` is deliberately excluded here — a `doing` row is only unavailable
# while a claim on it is actually valid; once that lease expires it is
# reclaimable (task_claim itself permits a new claimant once
# `lock_expires_at` has passed), so it falls through to the same
# gate/dependency/collision checks as `open` rather than being blanket-denied
# by status alone.
_NEXT_WORK_NOT_READY_STATUSES = frozenset({"blocked", "waiting_review"})
_NEXT_WORK_HUMAN_ONLY_LANES = frozenset({"operator-only", "interactive-only"})
_NEXT_WORK_HUMAN_GATE_COLLISION_GROUPS = frozenset({"human-decision"})
# Explicit, checkable replacement marker for a cancelled prerequisite. No
# such convention exists elsewhere in this codebase (tasks have no
# `superseded_by` column, unlike memory/vault notes) — this introduces the
# minimal one this projection can verify: the replacement must itself be a
# real, done task. Without it, a cancelled prerequisite fails closed
# (Devin #4135656507).
_NEXT_WORK_REPLACED_BY = re.compile(r"\[replaced-by:\s*#?(\d+)\]", re.I)
_NEXT_WORK_MAX_BOUNDED_LIST = 25


def _dod_clauses(notes: str) -> list[str]:
    clauses: list[str] = []
    in_dod = False
    for line in notes.splitlines():
        heading = _DOD_HEADING.match(line)
        if heading:
            in_dod = True
            inline = heading.group(1).strip()
            if inline:
                clauses.extend(_split_clauses(inline))
            continue
        if in_dod and line.strip().startswith("#"):
            in_dod = False
            continue
        if in_dod:
            item = _LIST_ITEM.match(line)
            if item:
                clauses.extend(_split_clauses(item.group(1).strip()))
            elif line.strip() and not line.startswith((" ", "\t")):
                in_dod = False
    return clauses


def _split_clauses(value: str) -> list[str]:
    """Split explicit numbered or semicolon-separated DoD clauses."""
    value = re.sub(r"^\s*(?:\(\d+\)|\d+[.)])\s*", "", value)
    parts = re.split(r"\s*(?:;\s*|\s+\(\d+\)\s*|\s+\d+[.)]\s*)", value.strip())
    clauses = []
    for part in parts:
        clause = re.sub(r"^\s*(?:\(\d+\)|\d+[.)])\s*", "", part).strip(" .")
        if clause:
            clauses.append(clause)
    return clauses


def _descendants(root: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        if task.get("parent_id") is not None:
            children.setdefault(int(task["parent_id"]), []).append(task)
    found, pending, seen = [], list(children.get(int(root["id"]), [])), set()
    while pending:
        task = pending.pop()
        task_id = int(task["id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        found.append(task)
        pending.extend(children.get(task_id, []))
    return found


def _rollup(children: list[dict[str, Any]]) -> str:
    statuses = {str(t.get("status")) for t in children}
    return next(
        (status for status in _ROLLUP_ORDER if status in statuses),
        "complete" if children else "unfiled",
    )


def _inline_text(value: object) -> str:
    """Keep task-supplied titles on one Markdown line."""
    return " ".join(str(value).split())


def _table_cell(value: object) -> str:
    return _inline_text(value).replace("|", r"\|")


def _coverage(clause: str, children: list[dict[str, Any]], index: int) -> str:
    key = re.sub(r"\W+", " ", clause).strip().casefold()
    token = re.sub(r"\W+", " ", f"dod-{index + 1}").strip().casefold()
    for child in children:
        notes = str(child.get("notes") or "")
        markers = [
            re.sub(r"\W+", " ", value).strip().casefold() for value in _COVERS.findall(notes)
        ]
        child_clauses = {
            re.sub(r"\W+", " ", item).strip().casefold() for item in _dod_clauses(notes)
        }
        if token in markers or key in markers or (key and key in child_clauses):
            return f"#{child['id']} {_inline_text(child['title'])}"
    return "GAP"


def build_weekly_plan(
    tasks: list[dict[str, Any]],
    *,
    release_tags: list[str] | None = None,
    release_description: str | None = None,
    generated_at: dt.date | None = None,
) -> str:
    """Render root epics, status rollups, explicit clause coverage, and tags."""
    epics = [
        t
        for t in tasks
        if t.get("parent_id") is None
        and _PROJECT_SPEC.search(str(t.get("notes") or ""))
        and _PRODUCT_ONE.search(str(t.get("notes") or ""))
    ]
    priority_order = {"high": 0, "med": 1, "low": 2}
    epics.sort(key=lambda t: (priority_order.get(str(t.get("priority")), 3), int(t["id"])))
    lines = [
        "## Ledger-derived forward plan",
        "",
        "| Epic | Title | Rollup | DoD coverage |",
        "|---|---|---|---|",
    ]
    coverage_rows = []
    for epic in epics:
        descendants = _descendants(epic, tasks)
        clauses = _dod_clauses(str(epic.get("notes") or ""))
        references = [_coverage(clause, descendants, index) for index, clause in enumerate(clauses)]
        for clause, reference in zip(clauses, references, strict=True):
            coverage_rows.append((int(epic["id"]), clause, reference))
        lines.append(
            f"| #{epic['id']} | {_table_cell(epic['title'])} | {_rollup(descendants)} | {len(clauses) - references.count('GAP')}/{len(clauses)} DoD clauses covered |"
        )
    lines.extend(["", "## DoD clause coverage", ""])
    if coverage_rows:
        lines.extend(
            f"- #{epic_id}: {clause} → {reference}" for epic_id, clause, reference in coverage_rows
        )
    else:
        lines.append("No DoD clauses were found under project-spec epic headings.")
    tags = release_tags or []
    lines.extend(["", "## Release state", ""])
    lines.append(
        f"Latest repository tag: {tags[0]}."
        if tags
        else "No release tags observed in this installation."
    )
    if tags:
        lines.append("Observed tags: " + ", ".join(tags[:10]) + ".")
    if release_description:
        lines.append(f"Repository HEAD: {release_description}.")
    lines.extend(
        [
            "",
            "_Generated from the live ledger; edit the source task specs, not this projection._",
            "",
        ]
    )
    return "\n".join(lines)


def merge_weekly_plan(existing: str, generated: str, generated_at: dt.date) -> str:
    """Replace only the generated block and retain hand-maintained plan context."""
    if (_PLAN_BEGIN in existing) != (_PLAN_END in existing):
        raise ToolError("existing plan has an incomplete planning-loop marker pair")
    if _PLAN_BEGIN in existing:
        begin = existing.index(_PLAN_BEGIN) + len(_PLAN_BEGIN)
        end = existing.index(_PLAN_END)
        if (
            end < begin
            or existing.find(_PLAN_BEGIN, begin) >= 0
            or existing.find(_PLAN_END, end + len(_PLAN_END)) >= 0
        ):
            raise ToolError("existing plan has malformed planning-loop markers")
        return existing[:begin] + "\n" + generated.rstrip() + "\n" + existing[end:]

    live_heading = re.search(r"(?m)^## 1\. Live status\b[^\n]*$", existing)
    next_heading = re.search(r"(?m)^## 2\.\s+.+$", existing)
    if live_heading and next_heading and next_heading.start() > live_heading.start():
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", live_heading.group())
        historical_date = date_match.group(1) if date_match else "date unknown"
        historical = existing[live_heading.end() : next_heading.start()].strip()
        generated_block = (
            f"## 1. Live status (ledger-generated, {generated_at.isoformat()})\n"
            f"{_PLAN_BEGIN}\n{generated.rstrip()}\n{_PLAN_END}\n\n"
            f"## Historical live-status snapshot (manual, {historical_date})\n\n"
            f"{historical}\n\n"
        )
        return existing[: live_heading.start()] + generated_block + existing[next_heading.start() :]

    if not existing.strip():
        return (
            "# Product 1 - Approval Ledger: the single forward plan\n\n"
            f"## 1. Live status (ledger-generated, {generated_at.isoformat()})\n"
            f"{_PLAN_BEGIN}\n{generated.rstrip()}\n{_PLAN_END}\n"
        )
    raise ToolError(
        "existing Product 1 plan has no generated markers or recognized live-status section; "
        "refusing to replace human-authored content"
    )


def _decision_packet_frontmatter(packet_date: dt.date) -> str:
    """YAML frontmatter for a generated decision packet.

    The wiki custodian flags every Wiki page without aliases, so each weekly
    edition names itself. Values are plain ASCII scalars; aliases and tags use
    flow lists, the shape wiki.parse_markdown_file reads.
    """
    day = packet_date.isoformat()
    return "\n".join(
        [
            "---",
            f"title: Weekly decision packet {day}",
            f'aliases: ["Decision Packet {day}", "Weekly decision packet {day}"]',
            "type: wiki",
            f"updated: {day}",
            "author: planning-loop",
            "tags: [decision-packet, founder-cadence, generated]",
            "generated_by: planning_loop.run_planning_loop",
            "---",
            "",
        ]
    )


def build_decision_packet(
    tasks: list[dict[str, Any]],
    *,
    prior_undecided_editions: dict[int, int] | None = None,
    prior_defaulted: dict[int, str] | None = None,
    packet_date: dt.date | None = None,
) -> str:
    """Render recommendation-bearing tasks and apply the two-edition rule.

    The packet opens with YAML frontmatter dated ``packet_date`` (today when
    omitted); the Markdown body below it is independent of the date.
    """
    packet_date = packet_date or dt.date.today()
    prior_undecided_editions = prior_undecided_editions or {}
    prior_defaulted = prior_defaulted or {}
    items = [
        t
        for t in tasks
        if t.get("status") not in ("done", "cancelled")
        and (
            config.canonical_task_lane(t.get("lane")) == "operator-only"
            or _DECISION_ITEM.search(str(t.get("notes") or ""))
        )
    ]
    priority_order = {"high": 0, "med": 1, "low": 2}
    items.sort(key=lambda t: (priority_order.get(str(t.get("priority")), 3), int(t["id"])))
    lines = [
        "# Weekly decision packet",
        "",
        "Choose yes, no, or defer. Operator-only items never default.",
        "",
    ]
    for task in items:
        notes = str(task.get("notes") or "")
        recommendation_match = _RECOMMENDATION.search(notes)
        recommendation = recommendation_match.group(1).strip() if recommendation_match else None
        human_only = config.canonical_task_lane(task.get("lane")) == "operator-only"
        decision = _DECISION.search(notes)
        seen = int(prior_undecided_editions.get(int(task["id"]), 0)) + 1
        lines.extend(
            [
                f"## #{task['id']} {task['title']}",
                "",
                f"- Owner lane: {'Operator-only' if human_only else task.get('lane', 'default')}",
                f"- Recommendation: {recommendation or 'MISSING — add a recorded recommendation to the task.'}",
            ]
        )
        if decision:
            lines.append(f"- Decision recorded on task: {decision.group(1).lower()}")
        elif int(task["id"]) in prior_defaulted and not human_only:
            lines.append(
                f"- Defaulted after two consecutive editions: {prior_defaulted[int(task['id'])]}"
            )
        elif recommendation and seen >= 2 and not human_only:
            lines.append(f"- Defaulted after two consecutive editions: {recommendation}")
        else:
            lines.append(f"- Decision: undecided (edition {seen} of 2)")
        lines.append("")
    if not items:
        lines.append("No decision-packet items are currently filed.")
    lines.append("_Generated from the task ledger. Record decisions on the source task._")
    return _decision_packet_frontmatter(packet_date) + "\n".join(lines) + "\n"


def build_goal_proposal(goal: str, epic: dict[str, Any], clauses: list[str]) -> str:
    """Render a spec-shaped proposal without filing tasks or plan steps."""
    if not goal.strip():
        raise ToolError("goal must not be empty")
    if not _PROJECT_SPEC.search(str(epic.get("notes") or "")):
        raise ToolError(f"parent task {epic.get('id')} does not carry [project-spec]")
    lines = [
        "# Proposed goal decomposition",
        "",
        "**Proposal only — human or Chief of Staff acceptance required. No tasks or plans were filed.**",
        "",
        f"Goal: {goal.strip()}",
        f"Parent epic: #{epic['id']} {epic['title']}",
        "",
    ]
    for index, clause in enumerate(clauses, 1):
        lines.extend(
            [
                f"## Proposed child {index}",
                "",
                f"- Objective: Deliver an observable outcome for {clause}.",
                f"- Scope: Address the epic DoD clause: {clause}.",
                "- Boundaries: This proposal changes no task, plan step, deployment, or external state.",
                f"- DoD: {clause}.",
                f"- Verification: Add a targeted test or reproducible check proving {clause}.",
                "",
            ]
        )
    if not clauses:
        lines.append("No uncovered DoD clauses were supplied; no child proposals were generated.")
    return "\n".join(lines).rstrip() + "\n"


def _next_work_row(item: dict[str, Any], *, tail: bool) -> dict[str, Any]:
    """Normalize one NEXT-WORK.json ``items``/``optional_tail`` entry."""
    return {
        "task_id": int(item["task_id"]),
        "title": str(item.get("title") or ""),
        "rank": item.get("rank"),
        "stretch": item.get("stretch"),
        "status_at_review": item.get("status_at_review"),
        "hard_dependencies": [int(x) for x in item.get("hard_dependencies") or []],
        "preferred_after": [int(x) for x in item.get("preferred_after") or []],
        "acceptance_gate": str(item.get("acceptance_gate") or ""),
        "human_gate": item.get("human_gate") is True,
        "collision_groups": list(item.get("collision_groups") or []),
        "tail": tail,
    }


def _next_work_gate_reason(row: dict[str, Any], live: dict[str, Any]) -> str | None:
    lane = str(live.get("lane") or "")
    if config.canonical_task_lane(lane) in _NEXT_WORK_HUMAN_ONLY_LANES:
        return f"live lane {lane!r} requires a human/interactive decision"
    if _NEXT_WORK_HUMAN_GATE_COLLISION_GROUPS.intersection(row["collision_groups"]):
        return "collision group flags a human-decision gate"
    if row["human_gate"]:
        return "manifest marks this action for human review"
    return None


def _next_work_parse_timestamp(value: Any) -> dt.datetime:
    """Parse a stored ISO timestamp the same way the claim writer wrote it.

    ``task_claim`` writes ``lock_expires_at`` with ``isoformat(timespec=
    "seconds")`` on a UTC-aware ``datetime``. Comparing the raw strings is
    unsafe: at an equal whole second the expiry string has ``+00:00`` where
    a microsecond-precision "now" string has ``.500000+00:00``, and lexical
    ordering reverses time ordering (Devin #4132982633). Parse both sides
    into aware datetimes and compare those instead. A naive timestamp is
    treated as UTC, matching the writer's convention.
    """
    parsed = dt.datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _next_work_in_flight(live: dict[str, Any], live_read_at: dt.datetime) -> bool:
    """Whether ``live`` is unavailable for pickup because of a live claim.

    Any nonterminal row with a valid, unexpired claim is in flight —
    independent of its status. ``task_update`` can move a claimed task back
    to ``open`` without ``release_claim=True``, and ``task_claim`` still
    rejects a new claimant while that lease remains valid, so status alone
    cannot decide availability (Devin #4132982811).
    """
    if str(live.get("status")) in _NEXT_WORK_TERMINAL_STATUSES:
        return False
    if not live.get("claimed_by"):
        return False
    expiry = live.get("lock_expires_at")
    if expiry is None:
        return True
    try:
        expiry_at = _next_work_parse_timestamp(expiry)
    except (ValueError, TypeError):
        # Malformed timestamp: fail closed rather than offering claimed work.
        return True
    # task_claim compares whole-second timestamps and accepts another actor
    # when the stored expiry is at or before storage.now().
    return expiry_at > live_read_at.replace(microsecond=0)


def _next_work_replacement_id(task: dict[str, Any]) -> int | None:
    replacement = task.get("replaced_by")
    if replacement is not None:
        return int(replacement)
    match = _NEXT_WORK_REPLACED_BY.search(str(task.get("notes") or ""))
    return int(match.group(1)) if match else None


def _next_work_mission_gap(
    tasks_by_id: dict[int, dict[str, Any]],
    statuses: dict[int, dict[str, Any]],
    manifest_ids: set[int],
    mission_task_id: int | None,
) -> list[int]:
    if mission_task_id is None:
        return []
    children_of: dict[int, list[int]] = {}
    for task_id, task in {**statuses, **tasks_by_id}.items():
        parent_id = task.get("parent_id")
        if parent_id is not None:
            children_of.setdefault(int(parent_id), []).append(int(task_id))
    seen: set[int] = set()
    pending = list(children_of.get(int(mission_task_id), []))
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        pending.extend(children_of.get(task_id, []))
    return sorted(
        task_id
        for task_id in seen
        if task_id in tasks_by_id
        and str(tasks_by_id[task_id].get("status")) not in _NEXT_WORK_TERMINAL_STATUSES
        and task_id not in manifest_ids
        and task_id != int(mission_task_id)
    )[:_NEXT_WORK_MAX_BOUNDED_LIST]


def build_next_work_view(
    manifest: dict[str, Any],
    tasks: list[dict[str, Any]],
    dependencies: dict[int, list[int]] | None = None,
    *,
    manifest_revision: str | None = None,
    manifest_content_hash: str | None = None,
    live_read_at: dt.datetime | None = None,
    archived_task_statuses: dict[int, dict[str, Any]] | None = None,
    queued_routine_task_ids: set[int] | frozenset[int] | None = None,
    mission_gap_task_ids: list[int] | None = None,
    include_live_titles: bool = True,
) -> dict[str, Any]:
    """Project the maintained implementation sequence onto live hub state.

    Pure function: no filesystem, database or subprocess access; callers pass
    the parsed ``manifest`` (NEXT-WORK.json, schema_version 1), live ``tasks``
    rows (id/status/lane/claimed_by/lock_expires_at/parent_id/notes/title/...)
    and the live ``dependencies`` graph (task_id -> [depends_on_task_id,
    ...]). ``archived_task_statuses`` is an optional ``task_id -> {"status",
    "evidence_ref", "notes", "parent_id"}`` lookup for tasks excluded from
    ``tasks`` (archived rows) — used to resolve dependency/ancestor status,
    to bridge the mission-gap walk across an archived parent, and to
    distinguish an archived-but-completed manifest row from one truly
    unknown to the hub. It never makes an archived task a candidate or an
    in-flight row. ``queued_routine_task_ids`` is the set of task ids with a
    live ``routine_runs`` row in ``queued`` state — ``task_claim`` rejects
    those until the scheduler releases them, so this view must not suggest
    them either.

    Extends the existing planning_loop/routine/projection seam for task
    #1468 and returns a dict shaped for both JSON and
    ``render_next_work_markdown``. It is a
    read-only projection — no task store, scheduler or second queue; a
    manual override always remains human/policy authorized.
    """
    if manifest.get("schema_version") != _NEXT_WORK_SCHEMA_VERSION:
        raise ToolError(
            f"unsupported next-work manifest schema_version: {manifest.get('schema_version')!r}"
        )
    dependencies = dependencies or {}
    archived_task_statuses = archived_task_statuses or {}
    queued_routine_task_ids = queued_routine_task_ids or frozenset()
    live_read_at = live_read_at or dt.datetime.now(dt.UTC)
    now_iso = live_read_at.isoformat()

    tasks_by_id = {int(t["id"]): t for t in tasks}
    raw_rows = [_next_work_row(item, tail=False) for item in manifest.get("items") or []]
    tail_rows = [_next_work_row(item, tail=True) for item in manifest.get("optional_tail") or []]
    # Selection order follows `rank`, not array position (Devin #4132983281):
    # the maintained array is expected to already be rank-sorted, but a
    # divergence must not silently reorder which task gets suggested.
    rows = sorted(raw_rows, key=lambda r: r["rank"] if r["rank"] is not None else float("inf"))
    rank_order_drift = [r["task_id"] for r in raw_rows] != [r["task_id"] for r in rows]
    # Dedupe by task_id in rank-first order (ranked rows, in rank order,
    # before tail rows in their given order) so a duplicated manifest row
    # can't consume two of the two candidate slots by itself
    # (Devin #4132983281... #4135812130).
    seen_task_ids: set[int] = set()
    duplicate_task_ids: list[int] = []
    all_rows: list[dict[str, Any]] = []
    for row in rows + tail_rows:
        if row["task_id"] in seen_task_ids:
            duplicate_task_ids.append(row["task_id"])
            continue
        seen_task_ids.add(row["task_id"])
        all_rows.append(row)
    title_drift: list[str] = []
    for row in all_rows:
        live = tasks_by_id.get(row["task_id"])
        if live is None or not live.get("title"):
            continue
        live_title = str(live["title"])
        if live_title != row["title"]:
            title_drift.append(f"#{row['task_id']} manifest title differs from live task title")
            if include_live_titles:
                row["manifest_title"] = row["title"]
                row["title"] = live_title
    manifest_ids = {row["task_id"] for row in all_rows}

    def task_status_lookup(task_id: int) -> dict[str, Any] | None:
        # Candidacy/in-flight checks only ever see live (non-archived) tasks;
        # dependency and ancestor-cancellation resolution also consult
        # archived statuses so a completed/cancelled task doesn't stay
        # "missing" once it's archived (Devin #4132982512, #4132982706,
        # #4135656891). `tasks_by_id` wins when both have an entry.
        live = tasks_by_id.get(task_id)
        if live is not None:
            return live
        return archived_task_statuses.get(task_id)

    def unmet_hard_dependencies(row: dict[str, Any]) -> list[dict[str, Any]]:
        """Unmet prerequisites for ``row``: ``[{"task_id", "detail"}, ...]``."""
        prereqs = set(row["hard_dependencies"]) | set(dependencies.get(row["task_id"], []))
        unmet = []
        for prereq_id in sorted(prereqs):
            prereq = task_status_lookup(prereq_id)
            status = str(prereq.get("status")) if prereq is not None else None
            if status == "done":
                continue
            if status == "cancelled":
                # A cancelled prerequisite only stops blocking when its own
                # notes carry an explicit `[replaced-by: #N]` marker pointing
                # at a *done* replacement task — evidence_ref is not proof of
                # replacement (task_add lets it be set before completion,
                # Devin #4135656507). No such convention exists elsewhere in
                # this codebase yet, so absent that marker this fails closed.
                replacement_id = _next_work_replacement_id(prereq)
                replacement = task_status_lookup(replacement_id) if replacement_id else None
                if replacement is not None and str(replacement.get("status")) == "done":
                    continue
                detail = (
                    f"#{prereq_id} (cancelled, no `[replaced-by: #N]` marker in its notes — "
                    "failing closed)"
                    if replacement_id is None
                    else f"#{prereq_id} (cancelled, replaced-by #{replacement_id} is not done)"
                )
                unmet.append({"task_id": prereq_id, "detail": detail})
                continue
            unmet.append({"task_id": prereq_id, "detail": f"#{prereq_id}"})
        return unmet

    def cancelled_ancestor(task_id: int) -> int | None:
        # task_claim's _assert_no_cancelled_ancestor rejects any claim under
        # a cancelled ancestor; mirror that here so a candidate the view
        # suggests is actually claimable (Devin #4135656706).
        seen: set[int] = set()
        current_id: int | None = task_id
        for _ in range(64):  # cycle/depth guard; the tree is never this deep
            if current_id is None or current_id in seen:
                return None
            seen.add(current_id)
            current = task_status_lookup(current_id)
            if current is None:
                return None
            parent_id = current.get("parent_id")
            if parent_id is None:
                return None
            parent_id = int(parent_id)
            parent = task_status_lookup(parent_id)
            if parent is not None and str(parent.get("status")) == "cancelled":
                return parent_id
            current_id = parent_id
        return None

    # Collision groups already occupied by claimed (live, unexpired) work,
    # whether or not that row ends up eligible for anything else below.
    occupied_groups: set[str] = set()
    in_flight: list[dict[str, Any]] = []
    for row in all_rows:
        live = tasks_by_id.get(row["task_id"])
        if live is not None and _next_work_in_flight(live, live_read_at):
            occupied_groups.update(row["collision_groups"])
            in_flight.append({**row, "claimed_by": live.get("claimed_by")})

    candidates: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []
    disappeared: list[int] = []
    unknown_to_hub: list[int] = []
    selected_groups: set[str] = set()

    for row in all_rows:
        live = tasks_by_id.get(row["task_id"])
        if live is None:
            # Before calling this genuinely unknown, check whether it's a
            # manifest row that was completed and then archived — archived
            # rows are absent from `tasks` by design, but a done/cancelled
            # archived task is "disappeared", not "unknown" (Devin
            # #4135656891).
            archived = archived_task_statuses.get(row["task_id"])
            archived_status = str(archived.get("status")) if archived is not None else None
            if archived_status in _NEXT_WORK_TERMINAL_STATUSES:
                disappeared.append(row["task_id"])
                continue
            unknown_to_hub.append(row["task_id"])
            reason = "unknown to the hub"
            if archived is not None:
                reason = (
                    f"archived with unexpected non-terminal status {archived_status!r}; "
                    "treated as unknown pending review"
                )
            evaluated.append({**row, "eligible": False, "selected": False, "reason": reason})
            continue
        status = str(live.get("status"))
        lane = str(live.get("lane") or "")
        if status in _NEXT_WORK_TERMINAL_STATUSES:
            disappeared.append(row["task_id"])
            continue
        if lane in config.TASK_UNCLAIMABLE_LANES:
            # Tracking/milestone lane: an umbrella row, never a unit of work.
            continue
        if _next_work_in_flight(live, live_read_at):
            evaluated.append(
                {
                    **row,
                    "eligible": False,
                    "selected": False,
                    "reason": f"in-flight, claimed by {live.get('claimed_by')}",
                }
            )
            continue
        if status in _NEXT_WORK_NOT_READY_STATUSES:
            # blocked/waiting_review without an active claim is not ready
            # work; decide this before the dependency/collision checks below
            # (Devin #4132982304). `doing` with an expired lease already fell
            # through the in-flight check above and is treated like `open`.
            evaluated.append(
                {
                    **row,
                    "eligible": False,
                    "selected": False,
                    "reason": f"status is {status!r}, not open",
                }
            )
            continue
        if row["task_id"] in queued_routine_task_ids:
            # task_claim rejects a queued routine task until the scheduler
            # releases it (Devin #4135812331); mirror that here.
            evaluated.append(
                {
                    **row,
                    "eligible": False,
                    "selected": False,
                    "reason": "queued routine work; waiting for scheduler release",
                }
            )
            continue
        ancestor_id = cancelled_ancestor(row["task_id"])
        if ancestor_id is not None:
            evaluated.append(
                {
                    **row,
                    "eligible": False,
                    "selected": False,
                    "reason": (
                        f"cancelled ancestor #{ancestor_id}: task_claim rejects any claim "
                        "under a cancelled ancestor"
                    ),
                }
            )
            continue
        gate_reason = _next_work_gate_reason(row, live)
        if gate_reason:
            evaluated.append({**row, "eligible": False, "selected": False, "reason": gate_reason})
            continue
        unmet = unmet_hard_dependencies(row)
        if unmet:
            blockers = ", ".join(entry["detail"] for entry in unmet)
            evaluated.append(
                {
                    **row,
                    "eligible": False,
                    "selected": False,
                    "reason": f"blocked on unmet hard dependency: {blockers}",
                    "blockers": [entry["task_id"] for entry in unmet],
                }
            )
            continue
        # Everything below this point is genuinely eligible: no gate, no
        # unmet prerequisite, not claimed. What remains is whether it can be
        # *selected* as one of the (at most two) suggested candidates.
        colliding = sorted(
            (selected_groups | occupied_groups).intersection(row["collision_groups"])
        )
        if colliding:
            evaluated.append(
                {
                    **row,
                    "eligible": True,
                    "selected": False,
                    "reason": f"collision group(s) already occupied: {', '.join(colliding)}",
                }
            )
            continue
        if len(candidates) >= 2:
            evaluated.append(
                {
                    **row,
                    "eligible": True,
                    "selected": False,
                    "reason": "two eligible candidates already selected",
                }
            )
            continue
        candidate = {
            **row,
            "eligible": True,
            "selected": True,
            "reason": None,
            "status": status,
            "lane": lane,
        }
        candidates.append(candidate)
        evaluated.append(candidate)
        selected_groups.update(row["collision_groups"])

    drift: list[str] = []
    drift.extend(title_drift)
    if rank_order_drift:
        drift.append(
            "manifest items array order disagrees with rank order; selection followed rank, "
            "not array position"
        )
    if duplicate_task_ids:
        shown = ", ".join(f"#{x}" for x in dict.fromkeys(duplicate_task_ids))
        drift.append(
            f"manifest lists duplicate task id(s), only the first rank-first occurrence was "
            f"used: {shown}"
        )
    for row in all_rows:
        live = tasks_by_id.get(row["task_id"])
        if live is None:
            continue
        reviewed = row.get("status_at_review")
        live_status = str(live.get("status"))
        if reviewed and reviewed != live_status:
            drift.append(
                f"#{row['task_id']} manifest status_at_review={reviewed!r} "
                f"but live status={live_status!r}"
            )
        if (
            _NEXT_WORK_HUMAN_GATE_COLLISION_GROUPS.intersection(row["collision_groups"])
            and config.canonical_task_lane(live.get("lane")) not in _NEXT_WORK_HUMAN_ONLY_LANES
        ):
            drift.append(
                f"#{row['task_id']} manifest flags a human-decision gate but live lane is "
                f"{live.get('lane')!r}"
            )
    if unknown_to_hub:
        shown = ", ".join(f"#{x}" for x in unknown_to_hub[:_NEXT_WORK_MAX_BOUNDED_LIST])
        drift.append(f"manifest rows unknown to the hub: {shown}")

    mission_task_id = manifest.get("mission_task_id")
    mission_gap = mission_gap_task_ids
    if mission_gap is None:
        # Walk parent_id edges through archived parents too (an archived
        # parent is absent from `tasks` and would otherwise break the chain,
        # hiding a live grandchild from the gap check), but only ever report
        # live, nonterminal descendants — an archived node itself never
        # becomes a reported gap (Devin #4135811928).
        mission_gap = _next_work_mission_gap(
            tasks_by_id, archived_task_statuses, manifest_ids, mission_task_id
        )
    if mission_gap:
        shown = ", ".join(f"#{x}" for x in mission_gap)
        drift.append(
            f"open live tasks under mission #{mission_task_id} missing from the manifest: {shown}"
        )

    manifest_as_of = manifest.get("as_of")
    staleness_days: int | None = None
    if manifest_as_of:
        try:
            staleness_days = (live_read_at.date() - dt.date.fromisoformat(manifest_as_of)).days
        except ValueError:
            staleness_days = None

    return {
        "mission_task_id": mission_task_id,
        "plan_id": manifest.get("plan_id"),
        "manifest_as_of": manifest_as_of,
        "manifest_revision": manifest_revision,
        "manifest_content_hash": manifest_content_hash,
        "live_read_at": now_iso,
        "staleness_days": staleness_days,
        "candidates": candidates,
        "in_flight": in_flight,
        "evaluated": evaluated,
        "disappeared_task_ids": disappeared,
        "unknown_to_hub_task_ids": unknown_to_hub,
        "mission_gap_task_ids": mission_gap,
        "drift_warnings": drift,
        "authority": manifest.get("authority"),
    }


def render_next_work_markdown(view: dict[str, Any]) -> str:
    """Compact markdown section for the current next-work view (task #1468)."""
    lines = ["## Current next-work view", ""]
    mission = view.get("mission_task_id")
    if mission is not None:
        lines.append(f"Mission root: #{mission}. Plan: `{view.get('plan_id')}`.")
    freshness = [f"Manifest as_of: {view.get('manifest_as_of') or 'unknown'}"]
    if view.get("manifest_revision"):
        freshness.append(f"revision `{view['manifest_revision']}`")
    else:
        freshness.append("revision unavailable")
    if view.get("manifest_content_hash"):
        freshness.append(f"content hash `{view['manifest_content_hash'][:12]}`")
    freshness.append(f"live read at {view.get('live_read_at')}")
    if view.get("staleness_days") is not None:
        freshness.append(f"staleness {view['staleness_days']}d")
    lines.append("; ".join(freshness) + ".")
    lines.append("")
    lines.append("### Next eligible candidates")
    lines.append("")
    candidates = view.get("candidates") or []
    if not candidates:
        lines.append("No eligible non-conflicting candidate is available right now.")
    for candidate in candidates:
        lines.append(
            f"- **#{candidate['task_id']}** {_inline_text(candidate['title'])} "
            f"(rank {candidate.get('rank') if candidate.get('rank') is not None else 'tail'}, "
            f"stretch {candidate.get('stretch') or '—'}, "
            f"collision groups: {', '.join(candidate['collision_groups']) or 'none'})"
        )
    if view.get("in_flight"):
        lines.append("")
        lines.append("### In-flight (already claimed, not suggested)")
        lines.append("")
        for row in view["in_flight"]:
            lines.append(
                f"- #{row['task_id']} {_inline_text(row['title'])} — claimed by {row.get('claimed_by')}"
            )
    evaluated = view.get("evaluated") or []
    blocked = [row for row in evaluated if row.get("reason") and not row.get("eligible")]
    if blocked:
        lines.append("")
        lines.append("### Ineligible or gated (not suggested)")
        lines.append("")
        for row in blocked:
            lines.append(f"- #{row['task_id']} {_inline_text(row['title'])} — {row['reason']}")
    held = [row for row in evaluated if row.get("eligible") and not row.get("selected")]
    if held:
        lines.append("")
        lines.append(
            f"{len(held)} further eligible row(s) held back by rank or collision group; "
            "see `evaluated` in the JSON view."
        )
    if view.get("disappeared_task_ids"):
        lines.append("")
        lines.append(
            "### Completed/cancelled since the manifest was cut\n\n"
            + ", ".join(f"#{x}" for x in view["disappeared_task_ids"])
        )
    if view.get("projection_mirror"):
        lines.append("")
        lines.append(
            f"Vault mirror: {view['projection_mirror']['status']}. "
            f"Projection consistency: {view.get('projection_consistency', 'unavailable')}."
        )
    if view.get("drift_warnings"):
        lines.append("")
        lines.append("### Drift warnings")
        lines.append("")
        lines.extend(f"- {warning}" for warning in view["drift_warnings"])
    lines.extend(
        [
            "",
            "_Projection only. The live hub is authoritative; this is a versioned "
            "recommendation, not a scheduler or a dispatch. Manual override remains "
            "human/policy authorized._",
            "",
        ]
    )
    return "\n".join(lines)


def _read_tasks(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if conn is not None:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM tasks WHERE archived_at IS NULL ORDER BY id"
            ).fetchall()
        ]
    db_path = Path(config.db_path()).expanduser().resolve()
    owned = org_partition.open_store(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    owned.row_factory = sqlite3.Row
    try:
        owned.execute("PRAGMA query_only=ON")
        return _read_tasks(owned)
    finally:
        owned.close()


def _read_task_dependencies(conn: sqlite3.Connection | None = None) -> dict[int, list[int]]:
    """Live hard-dependency graph: task_id -> [depends_on_task_id, ...]."""
    if conn is not None:
        edges: dict[int, list[int]] = {}
        for row in conn.execute(
            "SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY task_id"
        ):
            edges.setdefault(int(row["task_id"]), []).append(int(row["depends_on_task_id"]))
        return edges
    db_path = Path(config.db_path()).expanduser().resolve()
    owned = org_partition.open_store(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    owned.row_factory = sqlite3.Row
    try:
        owned.execute("PRAGMA query_only=ON")
        return _read_task_dependencies(owned)
    finally:
        owned.close()


def _read_all_task_statuses(conn: sqlite3.Connection | None = None) -> dict[int, dict[str, Any]]:
    """Status/evidence_ref/notes/parent_id for every task, archived included.

    ``_read_tasks`` filters ``archived_at IS NULL`` for the live candidate
    pool, but ``task_dependencies`` edges and manifest rows keep pointing at
    tasks after they're archived. Archiving a finished prerequisite must not
    re-block its successor, and an archived manifest row must not read as
    "unknown to the hub" (Devin #4132982512, #4135656891); ``notes`` and
    ``parent_id`` also let the cancelled-prerequisite-replacement and
    cancelled-ancestor checks see archived rows. Archived rows still never
    appear in ``tasks`` and so never become candidates or in-flight rows.
    """
    if conn is not None:
        return {
            int(row["id"]): {
                "status": row["status"],
                "evidence_ref": row["evidence_ref"],
                "notes": row["notes"],
                "parent_id": row["parent_id"],
            }
            for row in conn.execute("SELECT id, status, evidence_ref, notes, parent_id FROM tasks")
        }
    db_path = Path(config.db_path()).expanduser().resolve()
    owned = org_partition.open_store(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    owned.row_factory = sqlite3.Row
    try:
        owned.execute("PRAGMA query_only=ON")
        return _read_all_task_statuses(owned)
    finally:
        owned.close()


def _read_queued_routine_task_ids(conn: sqlite3.Connection | None = None) -> set[int]:
    """Task ids with a ``routine_runs`` row still in ``queued`` state.

    Mirrors ``tasks._is_queued_routine_task`` exactly (``SELECT 1 FROM
    routine_runs WHERE task_id=? AND status='queued'``), but as a set lookup
    over every task at once: ``task_claim`` rejects a claim on one of these
    until the scheduler releases it, so the next-work view must not suggest
    it either (Devin #4135812331).
    """
    if conn is not None:
        return {
            int(row["task_id"])
            for row in conn.execute(
                "SELECT DISTINCT task_id FROM routine_runs WHERE status='queued' "
                "AND task_id IS NOT NULL"
            )
        }
    db_path = Path(config.db_path()).expanduser().resolve()
    owned = org_partition.open_store(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    owned.row_factory = sqlite3.Row
    try:
        owned.execute("PRAGMA query_only=ON")
        return _read_queued_routine_task_ids(owned)
    finally:
        owned.close()


def _read_next_work_live_snapshot() -> tuple[
    list[dict[str, Any]], dict[int, list[int]], dict[int, dict[str, Any]], set[int]
]:
    """Task rows, the dependency graph, all-task statuses and queued routine
    task ids from ONE snapshot.

    Each of ``_read_tasks``/``_read_task_dependencies``/
    ``_read_all_task_statuses``/``_read_queued_routine_task_ids`` opens its
    own connection when called bare; reading them separately for one view
    means a concurrent write between reads can mix two different hub states
    into a single recommendation (Devin #4135657340, #4135812331). Holding
    one read transaction open across all four queries on one connection
    gives a consistent snapshot instead.
    """
    db_path = Path(config.db_path()).expanduser().resolve()
    conn = org_partition.open_store(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN DEFERRED")
        try:
            tasks = _read_tasks(conn)
            dependencies = _read_task_dependencies(conn)
            all_statuses = _read_all_task_statuses(conn)
            queued_routine_task_ids = _read_queued_routine_task_ids(conn)
        finally:
            conn.rollback()
        return tasks, dependencies, all_statuses, queued_routine_task_ids
    finally:
        conn.close()


def _next_work_body_hash(raw: str, *, mirror: bool = False) -> str:
    """Compare Markdown bodies, preserving content while normalizing newlines."""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    lines = normalized.split("\n")
    if mirror and lines[0] == "---":
        end = next((i for i in range(1, len(lines)) if lines[i] in {"---", "..."}), None)
        if end is not None:
            normalized = "\n".join(lines[end + 1 :])
    return hashlib.sha256((normalized.strip("\n") + "\n").encode()).hexdigest()


def _next_work_manifest_object_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _next_work_cockpit_root() -> Path:
    """Process-configured cockpit, independent of per-request workspace bindings."""
    return Path(os.environ.get("UA_WORKSPACE") or str(config.default_workspace())).expanduser()


def _read_next_work_fixed_source(root: Path, relative: Path) -> str:
    """Read only fixed cockpit sources, preserving caller DENY and containment."""
    config.workspace_root()  # Explicit filesystem DENY still forbids these hub-owned reads.
    if relative not in {_NEXT_WORK_MARKDOWN_RELATIVE, _NEXT_WORK_MANIFEST_RELATIVE}:
        raise ToolError("unsupported next-work canonical source")
    canonical_root = root.expanduser().resolve()
    path = canonical_root / relative
    checked = path.resolve()
    if not checked.is_relative_to(canonical_root):
        raise ToolError("next-work canonical source escapes the cockpit")
    guard_hook_dir(checked, canonical_root, str(path))
    # A request binding may be elsewhere; explicitly inspect every canonical
    # relative component rather than deriving the secret guard's root from it.
    guard_secret_filename(checked, str(checked.relative_to(canonical_root)))
    return checked.read_text(encoding="utf-8")


def _next_work_projection_provenance(
    manifest: dict[str, Any], root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Digest only the fixed, local authority sources; return no note bodies."""
    mirror = {
        "authority": "hub-local",
        "canonical_source": str(_NEXT_WORK_MARKDOWN_RELATIVE),
        "mirror_source": _NEXT_WORK_VAULT_MIRROR,
        "canonical_hash": None,
        "mirror_hash": None,
        "status": "unavailable",
    }
    source = {
        "authority": "hub-local",
        "input_manifest_hash": _next_work_manifest_object_hash(manifest),
        "canonical_manifest_hash": None,
        "status": "unavailable",
    }
    try:
        # Hub-owned process paths must not bypass an explicit caller DENY.
        config.workspace_root()
    except ToolError:
        return mirror, source
    try:
        raw = _read_next_work_fixed_source(root, _NEXT_WORK_MARKDOWN_RELATIVE)
        mirror["canonical_hash"] = _next_work_body_hash(raw)
    except (OSError, ToolError, UnicodeError):
        pass
    try:
        path = resolve_in_vault(_NEXT_WORK_VAULT_MIRROR)
        mirror["mirror_hash"] = _next_work_body_hash(path.read_text(encoding="utf-8"), mirror=True)
    except (OSError, ToolError, UnicodeError):
        pass
    if mirror["canonical_hash"] is not None and mirror["mirror_hash"] is not None:
        mirror["status"] = (
            "matching" if mirror["canonical_hash"] == mirror["mirror_hash"] else "drift"
        )
    try:
        canonical = json.loads(_read_next_work_fixed_source(root, _NEXT_WORK_MANIFEST_RELATIVE))
        if not isinstance(canonical, dict):
            raise ValueError("canonical manifest must be an object")
        source["canonical_manifest_hash"] = _next_work_manifest_object_hash(canonical)
    except (OSError, ToolError, ValueError, UnicodeError):
        pass
    if source["canonical_manifest_hash"] is not None:
        source["status"] = (
            "matching"
            if source["input_manifest_hash"] == source["canonical_manifest_hash"]
            else "drift"
        )
    return mirror, source


def _valid_next_work_digest_comparison(value: object, left: str, right: str) -> bool:
    if not isinstance(value, dict) or value.get("authority") != "hub-local":
        return False
    status = value.get("status")
    hashes = (value.get(left), value.get(right))
    if any(
        h is not None and (not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h))
        for h in hashes
    ):
        return False
    if status == "unavailable":
        return None in hashes
    if None in hashes:
        return False
    return (status == "matching" and hashes[0] == hashes[1]) or (
        status == "drift" and hashes[0] != hashes[1]
    )


def _valid_next_work_mirror_provenance(value: object) -> bool:
    return (
        _valid_next_work_digest_comparison(value, "canonical_hash", "mirror_hash")
        and value.get("canonical_source") == str(_NEXT_WORK_MARKDOWN_RELATIVE)
        and value.get("mirror_source") == _NEXT_WORK_VAULT_MIRROR
    )


def _attach_next_work_provenance_warnings(view: dict[str, Any]) -> None:
    warnings = view.setdefault("drift_warnings", [])
    mirror = view["projection_mirror"]
    if mirror["status"] == "drift":
        warnings.append("Vault mirror differs from the authoritative cockpit NEXT-WORK.md body.")
    elif mirror["status"] == "unavailable":
        warnings.append("Vault mirror provenance unavailable; projection agreement is unverified.")
    source = view.get("projection_source", {})
    if source.get("status") == "drift":
        warnings.append(
            "Manifest source differs from the authoritative hub cockpit NEXT-WORK.json."
        )
    elif source.get("status") != "matching":
        warnings.append(
            "Manifest source provenance unavailable; hub cockpit agreement is unverified."
        )
    client = view.get("client_cockpit_source", {})
    if client.get("status") == "drift":
        warnings.append(
            "Client cockpit NEXT-WORK.md differs from the authoritative hub cockpit source."
        )
    elif client and client.get("status") != "matching":
        warnings.append("Client cockpit source agreement is unverified.")
    states = [mirror["status"], source.get("status", "unavailable")]
    if client:
        states.append(client["status"])
    view["projection_consistency"] = (
        "drift" if "drift" in states else "unavailable" if "unavailable" in states else "matching"
    )
    view["drift_warnings"] = list(dict.fromkeys(warnings))


def next_work_projection_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compute the full view on the hub; no task rows or notes leave it."""
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != _NEXT_WORK_SCHEMA_VERSION
    ):
        raise ValueError("unsupported next-work manifest schema_version")
    for key in ("items", "optional_tail"):
        rows = manifest.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"next-work manifest {key} must be an array of objects")
        for row in rows:
            if not isinstance(row.get("task_id"), int) or row["task_id"] <= 0:
                raise ValueError("next-work manifest task IDs must be positive integers")
            for field in ("hard_dependencies", "preferred_after"):
                values = row.get(field) or []
                if not isinstance(values, list) or any(
                    not isinstance(value, int) or value <= 0 for value in values
                ):
                    raise ValueError(f"next-work manifest {field} must contain positive integers")
    mission = manifest.get("mission_task_id")
    if mission is not None and (not isinstance(mission, int) or mission <= 0):
        raise ValueError("next-work mission ID must be a positive integer")
    tasks, dependencies, statuses, queued = _read_next_work_live_snapshot()
    view = build_next_work_view(
        manifest,
        tasks,
        dependencies,
        live_read_at=dt.datetime.now(dt.UTC),
        archived_task_statuses=statuses,
        queued_routine_task_ids=queued,
        include_live_titles=False,
    )
    mirror, source = _next_work_projection_provenance(manifest, _next_work_cockpit_root())
    view["projection_mirror"] = mirror
    view["projection_source"] = source
    _attach_next_work_provenance_warnings(view)
    return view


def _read_remote_next_work_view(manifest: dict[str, Any]) -> dict[str, Any]:
    from . import device_login
    from .hub_client import HubClient, PlatformCliError, _post_json

    try:
        hub_url = os.environ.get("UA_HTTP_HUB_URL", "").strip()
        if not hub_url:
            raise ToolError("UA_HTTP_HUB_URL is required for remote next-work reads")
        label = config.executor_agent() or device_login.saved_signin_agent(hub_url) or ""
        client = HubClient(agent_label=label, hub_url=hub_url, allow_fallback=False)
        payload = _post_json(client, "/api/tasks/next-work-projection", manifest)
        if not isinstance(payload.get("candidates"), list) or not isinstance(
            payload.get("evaluated"), list
        ):
            raise ValueError("unexpected next-work projection shape")
        if "projection_mirror" in payload and not _valid_next_work_mirror_provenance(
            payload["projection_mirror"]
        ):
            raise ValueError("unexpected next-work mirror provenance")
        if "projection_source" in payload:
            source = payload["projection_source"]
            if not _valid_next_work_digest_comparison(
                source, "input_manifest_hash", "canonical_manifest_hash"
            ) or source.get("input_manifest_hash") != _next_work_manifest_object_hash(manifest):
                raise ValueError("unexpected next-work manifest source provenance")
        return payload
    except (PlatformCliError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise ToolError(f"cannot read next-work projection from the hub: {exc}") from exc


def _next_work_manifest_path(workspace_root: Path | None = None) -> Path:
    """The cockpit's maintained NEXT-WORK.json, resolved like other hub-owned reads.

    ``workspace_root`` is injectable for tests; the default follows the same
    process-level ``UA_WORKSPACE`` configuration, independent of the per-request
    ``workspace_root_var`` override used by agent-facing fs_*/run_cmd tools.

    The manifest lives in the separately maintained cockpit workspace (the
    hub's ``UA_WORKSPACE``, default ``$UA_HOME/workspace``), not this product repo —
    this checkout has no ``docs/queue/NEXT-WORK.json`` of its own. Point
    ``UA_WORKSPACE`` at that cockpit checkout, or pass ``--manifest-path``
    explicitly, before running ``--next-work``.
    """
    root = workspace_root if workspace_root is not None else _next_work_cockpit_root()
    return Path(root).expanduser() / _NEXT_WORK_MANIFEST_RELATIVE


def _read_next_work_manifest_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"cannot read next-work manifest at {path}: {exc}. The manifest is maintained in "
            "the cockpit workspace (UA_WORKSPACE, default $UA_HOME/workspace), not this repo — point "
            "UA_WORKSPACE at that checkout or pass --manifest-path explicitly."
        ) from exc


def _parse_next_work_manifest(raw: str, path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"next-work manifest at {path} is not valid JSON: {exc}") from exc
    if manifest.get("schema_version") != _NEXT_WORK_SCHEMA_VERSION:
        raise ToolError(
            f"unsupported next-work manifest schema_version at {path}: "
            f"{manifest.get('schema_version')!r}"
        )
    return manifest


def _read_next_work_manifest(path: Path) -> dict[str, Any]:
    return _parse_next_work_manifest(_read_next_work_manifest_text(path), path)


def _next_work_manifest_content_hash(raw: str) -> str:
    """SHA-256 of the manifest bytes actually used, independent of git state."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _next_work_manifest_revision(manifest_path: Path) -> str | None:
    """Best-effort git revision of the exact manifest file that was read.

    Resolves ``manifest_path``'s own containing repository (via ``git
    rev-parse --show-toplevel`` from its directory) rather than assuming the
    default cockpit workspace, so a ``--manifest-path`` override reports its
    own history instead of an unrelated workspace file's (Devin
    #4135657158). Returns ``None`` when the path is outside any repository
    or has no commit history (untracked).

    Appends ``+dirty`` when the tracked file has uncommitted working-tree
    edits, so the reported revision doesn't silently claim to describe
    content that was actually just edited (Devin #4132983046). The content
    hash returned alongside this (see ``_next_work_manifest_content_hash``)
    always reflects the exact bytes read, dirty or not.
    """
    manifest_path = Path(manifest_path).expanduser()
    try:
        toplevel_result = subprocess.run(
            ["git", "-C", str(manifest_path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if toplevel_result.returncode != 0 or not toplevel_result.stdout.strip():
        return None
    repo_root = Path(toplevel_result.stdout.strip())
    try:
        relative_path = manifest_path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return None
    try:
        log_result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%H", "--", str(relative_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if log_result.returncode != 0:
        return None
    revision = log_result.stdout.strip()
    if not revision:
        return None
    try:
        status_result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", str(relative_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return revision
    if status_result.returncode == 0 and status_result.stdout.strip():
        return f"{revision}+dirty"
    return revision


def run_next_work_view(
    *,
    manifest_path: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """IO wrapper for task #1468: read the manifest and live hub state, project.

    Pure read: no task writes, no dispatch, no scheduler, no new tables. The
    manifest must exist in the cockpit workspace (see
    ``_next_work_manifest_path``); a missing file raises ``ToolError`` with
    that requirement spelled out rather than a bare I/O error. Task rows, the
    dependency graph and all-task statuses come from one consistent snapshot
    (``_read_next_work_live_snapshot``), and ``live_read_at`` is stamped
    right after that snapshot so the reported freshness matches the data
    actually used. Installation/scheduling of this as a live routine is
    separate from this function; nothing here writes to the vault or files a
    task. Fixed cockpit Markdown and the vault mirror are compared by normalized
    body hashes. Remote views retain hub-produced evidence and separately report
    any client cockpit disagreement; unavailable provenance never means agreement.
    """
    root = workspace_root if workspace_root is not None else _next_work_cockpit_root()
    path = manifest_path if manifest_path is not None else _next_work_manifest_path(root)
    raw = _read_next_work_manifest_text(path)
    manifest = _parse_next_work_manifest(raw, path)
    content_hash = _next_work_manifest_content_hash(raw)
    revision = _next_work_manifest_revision(path)
    try:
        storage.require_live_task_store()
    except storage.LiveStoreError:
        view = _read_remote_next_work_view(manifest)
        if not _valid_next_work_mirror_provenance(view.get("projection_mirror")):
            view["projection_mirror"] = {
                "status": "unavailable",
                "authority": "remote-hub-unverified",
                "canonical_hash": None,
                "mirror_hash": None,
            }
        local_hash = None
        try:
            local_hash = _next_work_body_hash(
                _read_next_work_fixed_source(Path(root), _NEXT_WORK_MARKDOWN_RELATIVE)
            )
        except (OSError, ToolError, UnicodeError):
            pass
        hub_hash = view["projection_mirror"].get("canonical_hash")
        view["client_cockpit_source"] = {
            "authority": "client-local-compared-to-hub-digest",
            "content_hash": local_hash,
            "hub_content_hash": hub_hash,
            "status": "unavailable"
            if None in (local_hash, hub_hash)
            else "matching"
            if local_hash == hub_hash
            else "drift",
        }
    else:
        tasks, dependencies, archived_statuses, queued_routine_task_ids = (
            _read_next_work_live_snapshot()
        )
        view = build_next_work_view(
            manifest,
            tasks,
            dependencies,
            live_read_at=dt.datetime.now(dt.UTC),
            archived_task_statuses=archived_statuses,
            queued_routine_task_ids=queued_routine_task_ids,
        )
        mirror, source = _next_work_projection_provenance(manifest, Path(root))
        view["projection_mirror"] = mirror
        view["projection_source"] = source
    _attach_next_work_provenance_warnings(view)
    view["manifest_revision"] = revision
    view["manifest_content_hash"] = content_hash
    view["manifest_path"] = str(path)
    return view


def _release_tags(repo: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--sort=-version:refname",
            "--format=%(refname:short)",
            "refs/tags",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return (
        [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode == 0
        else []
    )


def _release_description(repo: Path) -> str:
    result = subprocess.run(
        ["git", "describe", "--tags", "--long", "--always"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _prior_decision_state(
    vault_root: Path | None, packet_date: dt.date
) -> tuple[dict[int, int], dict[int, str]]:
    folder = vault_root / "Wiki" / "Decision-Packets" if vault_root else None
    if folder is None or not folder.is_dir():
        return {}, {}
    prefix = "Decision-Packet-"
    editions = sorted(
        p for p in folder.glob(f"{prefix}*.md") if p.stem < f"{prefix}{packet_date.isoformat()}"
    )
    if not editions:
        return {}, {}
    undecided, defaulted = {}, {}
    current_id = None
    seen: set[int] = set()
    for edition in reversed(editions):
        current_id = None
        for line in edition.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^## #(\d+)\b", line)
            if match:
                current_id = int(match.group(1))
            elif line.startswith("## "):
                current_id = None
            elif current_id is not None and current_id not in seen:
                count = re.search(r"Decision: undecided \(edition (\d+) of 2\)", line)
                default = re.search(r"Defaulted after two consecutive editions: (.+)$", line)
                decision = re.search(r"Decision recorded on task: (yes|no|defer)$", line)
                if count:
                    undecided[current_id] = int(count.group(1))
                    seen.add(current_id)
                elif default:
                    defaulted[current_id] = default.group(1).strip()
                    seen.add(current_id)
                elif decision:
                    # A recorded choice resets the undecided-edition streak.
                    seen.add(current_id)
    return undecided, defaulted


def run_planning_loop(*, generated_at: dt.date | None = None, write: bool = True) -> dict[str, Any]:
    generated_at = generated_at or dt.date.today()
    tasks = _read_tasks()
    repo = Path(__file__).resolve().parents[2]
    tags = _release_tags(repo)
    release_description = _release_description(repo)
    vault_root = config.vault_root()
    prior_counts, prior_defaults = _prior_decision_state(vault_root, generated_at)
    artifacts = {
        "plan": build_weekly_plan(
            tasks,
            release_tags=tags,
            release_description=release_description,
            generated_at=generated_at,
        ),
        "decision_packet": build_decision_packet(
            tasks,
            prior_undecided_editions=prior_counts,
            prior_defaulted=prior_defaults,
            packet_date=generated_at,
        ),
    }
    paths = {
        "plan": "Wiki/Product-1-Approval-Ledger-Plan.md",
        "decision_packet": f"Wiki/Decision-Packets/Decision-Packet-{generated_at.isoformat()}.md",
    }
    if write:
        if vault_root is None:
            raise ToolError("vault is not configured")
        from .tools.vault import vault_write_merged

        # Each write passes the revision it read (task #2111): an edit that
        # lands between the read and the write makes it re-read, re-merge and
        # retry, so hand-maintained plan context is never clobbered, and the
        # writes stay valid under UA_VAULT_REQUIRE_REVISION=1.
        generated_plan = artifacts["plan"]

        def merge_plan(current: str | None) -> str:
            artifacts["plan"] = (
                generated_plan
                if current is None
                else merge_weekly_plan(current, generated_plan, generated_at)
            )
            return artifacts["plan"]

        vault_write_merged(paths["plan"], merge_plan)
        packet = artifacts["decision_packet"]
        vault_write_merged(paths["decision_packet"], lambda _current: packet, retries=0)
    return {
        "status": "written" if write else "preview",
        "epic_count": sum(
            t.get("parent_id") is None
            and bool(_PROJECT_SPEC.search(str(t.get("notes") or "")))
            and bool(_PRODUCT_ONE.search(str(t.get("notes") or "")))
            for t in tasks
        ),
        "decision_count": sum(
            t.get("status") not in ("done", "cancelled")
            and (
                config.canonical_task_lane(t.get("lane")) == "operator-only"
                or bool(_DECISION_ITEM.search(str(t.get("notes") or "")))
            )
            for t in tasks
        ),
        "paths": paths,
        "artifacts": artifacts,
    }


def decompose_into_goal_linked_tasks(
    goal_id: int, epic_id: int, *, author: str | None = None
) -> dict[str, Any]:
    """File one child task per uncovered DoD clause and link each to ``goal_id``.

    Reuses the proposal's coverage rules so a second call does not duplicate
    work. The markdown proposal path stays read-only; this is the opt-in
    materialization. New tasks carry ``[covers: dod-N]`` and the goal id.
    ``parent_id`` on those tasks is decomposition nesting, not goal ancestry.
    Shared plan revisions for that goal go through ``joint_planning`` (task
    #1455): a second active joint plan for one goal is refused. This function
    only files tasks.
    """
    from .goals import _author, get_goal, goal_id_for_task, link_task
    from .tools.tasks import task_add

    who = _author(author)
    goal = get_goal(int(goal_id))
    if goal is None:
        raise ToolError(f"unknown goal: {goal_id}")
    serving = goal_id_for_task(int(epic_id))
    if serving is not None and serving != int(goal_id):
        raise ToolError(
            f"task {epic_id} already serves goal {serving}; "
            "refusing to file children for a different goal"
        )
    tasks = _read_tasks()
    epic = next((task for task in tasks if int(task["id"]) == int(epic_id)), None)
    if epic is None:
        raise ToolError(f"no task with id {epic_id}")
    epic_project = str(epic.get("project") or "unified-agent")
    if epic_project != str(goal["goal"]["project"]):
        raise ToolError("epic and goal must belong to the same project")
    clauses = _dod_clauses(str(epic.get("notes") or ""))
    descendants = _descendants(epic, tasks)
    created: list[dict[str, Any]] = []
    linked_existing: list[int] = []
    for index, clause in enumerate(clauses):
        covered = _coverage(clause, descendants, index)
        if covered != "GAP":
            child_id = int(covered.split()[0].lstrip("#"))
            linked = goal_id_for_task(child_id)
            if linked is None:
                link_task(child_id, int(goal_id), author=who)
                linked_existing.append(child_id)
            elif linked != int(goal_id):
                raise ToolError(
                    f"task {child_id} already serves goal {linked}; "
                    "refusing to treat it as coverage for a different goal"
                )
            continue
        notes = (
            f"[covers: dod-{index + 1}]\n\n"
            f"DoD: {clause}\n\n"
            f"Serves goal #{int(goal_id)} (goal_task edge, not a dependency).\n"
        )
        title = clause if len(clause) <= 180 else clause[:177] + "..."
        added = task_add(title=title, notes=notes, parent_id=int(epic_id), priority="med")
        link_task(int(added["id"]), int(goal_id), author=who)
        created.append({"task_id": int(added["id"]), "clause": clause, "goal_id": int(goal_id)})
        descendants.append(
            {"id": added["id"], "title": title, "notes": notes, "parent_id": epic_id}
        )
    epic_link = link_task(int(epic_id), int(goal_id), author=who)
    linked_existing.append(int(epic_id))
    return {
        "goal_id": int(goal_id),
        "epic_id": int(epic_id),
        "created": created,
        "linked_task_ids": linked_existing + [item["task_id"] for item in created],
        "edge": "goal_task",
        "epic_why_chain": epic_link["why_chain"],
    }


def _proposal(goal: str, epic_id: int, *, write: bool, generated_at: dt.date) -> tuple[str, str]:
    tasks = _read_tasks()
    epic = next((t for t in tasks if int(t["id"]) == epic_id), None)
    if epic is None:
        raise ToolError(f"no task with id {epic_id}")
    if epic.get("parent_id") is not None:
        raise ToolError(f"task {epic_id} is not a root epic")
    descendants = _descendants(epic, tasks)
    clauses = _dod_clauses(str(epic.get("notes") or ""))
    uncovered = [c for i, c in enumerate(clauses) if _coverage(c, descendants, i) == "GAP"]
    proposal = build_goal_proposal(goal, epic, uncovered)
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:48] or "goal"
    path = f"Wiki/Proposals/goal-{slug}-{generated_at.isoformat()}.md"
    if write:
        if config.vault_root() is None:
            raise ToolError("vault is not configured")
        from .tools.vault import vault_write_merged

        # A whole-page replacement cannot merge concurrent edits: surface CAS.
        vault_write_merged(path, lambda _current: proposal, retries=0)
    return path, proposal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the weekly ledger plan and decision packet."
    )
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--goal")
    parser.add_argument("--epic-id", type=int)
    parser.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument(
        "--next-work",
        action="store_true",
        help=(
            "Render the current next-work view (task #1468: projects "
            "docs/queue/NEXT-WORK.json onto live hub state) instead of the "
            "weekly plan/decision packet. Read-only; never writes the vault. "
            "Requires the separately maintained cockpit workspace (UA_WORKSPACE, "
            "default $UA_HOME/workspace) to contain docs/queue/NEXT-WORK.json — this "
            "product repo does not ship one; use --manifest-path to override."
        ),
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Override the NEXT-WORK.json path (default: <UA_WORKSPACE>/docs/queue/NEXT-WORK.json).",
    )
    parser.add_argument(
        "--format",
        choices=("both", "json", "markdown"),
        default="both",
        help=(
            "Output shape for --next-work. 'json' prints only the JSON view "
            "(nothing else on stdout, safe to pipe into a JSON parser); "
            "'markdown' prints only the rendered section; 'both' (default) "
            "keeps this command's original combined output — JSON, a blank "
            "line, then the markdown section — for interactive use."
        ),
    )
    args = parser.parse_args()
    if args.next_work:
        try:
            view = run_next_work_view(manifest_path=args.manifest_path)
            if args.format in ("both", "json"):
                print(json.dumps(view, indent=2, default=str))
            if args.format == "both":
                print()
            if args.format in ("both", "markdown"):
                print(render_next_work_markdown(view))
        except (ToolError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
            print(f"Next-work view failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return
    try:
        if bool(args.goal) != bool(args.epic_id):
            raise ToolError("--goal and --epic-id must be provided together")
        proposal = (
            _proposal(args.goal, args.epic_id, write=False, generated_at=args.date)
            if args.goal
            else None
        )
        result = run_planning_loop(generated_at=args.date, write=not args.no_write)
        if proposal and not args.no_write:
            from .tools.vault import vault_write_merged

            vault_write_merged(proposal[0], lambda _current: proposal[1], retries=0)
        print(
            f"{result['status']}: {result['epic_count']} epic(s), {result['decision_count']} decision(s); plan={result['paths']['plan']}; packet={result['paths']['decision_packet']}"
        )
        if args.no_write:
            print("\n# Forward plan preview\n")
            print(result["artifacts"]["plan"])
            print("\n# Decision packet preview\n")
            print(result["artifacts"]["decision_packet"])
        if proposal:
            if args.no_write:
                print(f"\n# Proposal preview: {proposal[0]}\n")
                print(proposal[1])
            else:
                print(f"Proposal written to: {proposal[0]}")
    except (ToolError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        print(f"Planning loop failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
