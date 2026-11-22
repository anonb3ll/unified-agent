"""Queue closure: staleness, park/fold, close-on-merge and a queue-health read (task #1848).

The rules (operator decision):

1. A task untouched for 30 days is PARKED with a stated reason. Parked, not closed.
2. Children of a parked epic FOLD into the parent: no open child is left under a
   parked root.
3. Work already merged CLOSES, with the merge SHA as ``evidence_ref``.
4. Nothing closes on a date alone. Ever.

Everything here sits on existing objects; there is no new table and no new status.

* **Parked** is ``archived_at`` set on a task that is not ``done``/``cancelled``,
  plus a recorded notes line carrying the actor and reason
  (``[parked @ <ts> by <actor>] <reason>``). ``archived_at`` already hides a task
  from default ``task_list`` views, which is exactly what parking should do, and
  ``task_list(parked=True)`` is the parked view. ``done`` rows that are archived
  are filed work and are never "parked".
* **Folded** is the same state for a descendant of a parked root, with
  ``[folded-into: #<root> @ <ts> by <actor>] <reason>``. The parent link is kept,
  so ``queue_unpark`` on the root restores exactly the children folded into it.
* **Unpark** clears ``archived_at`` and appends ``[unparked @ <ts> by <actor>]``.
  The last such line is the task's closure state; earlier lines are history.
* **Staleness** is the latest of ``updated_at`` (status, notes and field edits,
  and the queue-hygiene stamps), ``claimed_at`` and the task's comment thread.
  A read, never an action.

Park and fold run only when someone calls them with a reason. :func:`queue_sweep`
is the dry run: it returns a PROPOSED list for the operator to approve and writes
nothing. The one automatic rule is close-on-merge, and only where the evidence is
unambiguous (see :func:`close_on_merge`).
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, ready_dependents, storage, task_liveness

DEFAULT_STALE_DAYS = 30
TERMINAL_STATUSES = ("done", "cancelled")
ACTIVE_STATUSES = ("open", "doing", "waiting_review", "blocked")
# The pr-watch poller owns this file name (`pr_watch.CURSOR_FILE`); the poller
# sits in a higher layer, so the name is repeated and pinned by a test.
PR_WATCH_CURSOR_FILE = "pr-watch-cursor.json"
_HUMAN_ONLY_LANES = frozenset({"operator-only", "interactive-only"})
_REASON_MAX = 500

_CLOSURE_LINE_RX = re.compile(
    r"^\[(?P<kind>parked|unparked|folded-into: #(?P<root>\d+)) @ (?P<at>\S+) by (?P<by>[^\]]*)\]"
    r"[ \t]*(?P<reason>.*)$",
    re.MULTILINE,
)
_GATE_MARKER_RX = re.compile(r"\[waiting-for:\s*([^\]]+)\]", re.IGNORECASE)
_RED_TIER_RX = re.compile(r"red[- ]tier", re.IGNORECASE)
_PR_URL_RX = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", re.IGNORECASE)
_PR_SHORT_RX = re.compile(r"(?<![\w/])([\w.-]+/[\w.-]+)#(\d+)\b")
_SHA_RX = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_MILESTONE_MARKER = "[milestone-review]"
_GATE_START_RX = re.compile(r"\bGate:", re.IGNORECASE)
_SENTENCE_END_RX = re.compile(r"\.(?:\s|$)")
_TASK_REF_RX = re.compile(r"#(\d+)")
_VERDICT_RX = re.compile(r"\bverdict\s*[:\-]|\[milestone-verdict", re.IGNORECASE)
_SYSTEM_ACTOR = "queue-closure"


# --- small helpers ------------------------------------------------------------


def _clock() -> datetime:
    """Clock seam (tests monkeypatch this)."""
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_days_setting(override: int | None = None) -> int:
    """The staleness threshold in days: the argument, else ``UA_QUEUE_STALE_DAYS``, else 30."""
    if override is not None:
        if int(override) < 1:
            raise ToolError("stale_days must be at least 1")
        return int(override)
    try:
        value = int(os.environ.get("UA_QUEUE_STALE_DAYS", "") or DEFAULT_STALE_DAYS)
    except ValueError:
        return DEFAULT_STALE_DAYS
    return value if value >= 1 else DEFAULT_STALE_DAYS


def _clean_reason(reason: str | None) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        raise ToolError("a reason is required: parking and folding are never silent")
    if len(text) > _REASON_MAX:
        raise ToolError(f"reason must be at most {_REASON_MAX} characters")
    return text


def _actor(actor: str | None) -> str:
    identity = config.verified_agent_identity()
    if identity:
        return identity
    text = " ".join(str(actor or "").split())
    if not text:
        raise ToolError("actor is required when no verified identity is present")
    return text


def _tenant_clause() -> tuple[str, list[Any]]:
    from .tools import tasks as tasks_tool

    return tasks_tool._TENANT_SQL, [tasks_tool._tenant()]


def _append_line(notes: str | None, line: str) -> str:
    base = (notes or "").rstrip()
    return f"{base}\n{line}" if base else line


def closure_state(notes: str | None) -> dict[str, Any] | None:
    """The last park/fold/unpark line in ``notes``, parsed, or None when there is none."""
    last = None
    for match in _CLOSURE_LINE_RX.finditer(notes or ""):
        last = match
    if last is None:
        return None
    kind = last.group("kind")
    return {
        "kind": "folded" if kind.startswith("folded") else kind,
        "root": int(last.group("root")) if last.group("root") else None,
        "at": last.group("at"),
        "by": last.group("by").strip(),
        "reason": last.group("reason").strip(),
    }


def _is_parked_row(row) -> bool:
    return row["archived_at"] is not None and row["status"] not in TERMINAL_STATUSES


def _visible(conn, task_id: int):
    clause, params = _tenant_clause()
    return conn.execute(
        f"SELECT * FROM tasks WHERE id=? AND {clause}", (int(task_id), *params)
    ).fetchone()


def _children(conn, task_id: int) -> list[Any]:
    clause, params = _tenant_clause()
    return conn.execute(
        f"SELECT * FROM tasks WHERE parent_id=? AND {clause} ORDER BY id", (int(task_id), *params)
    ).fetchall()


def _descendants(conn, task_id: int) -> list[Any]:
    """Every descendant row, parents before children. Cycle-safe."""
    out: list[Any] = []
    seen = {int(task_id)}
    frontier = [int(task_id)]
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            for child in _children(conn, parent):
                if child["id"] in seen:
                    continue
                seen.add(child["id"])
                out.append(child)
                nxt.append(child["id"])
        frontier = nxt
    return out


def _ancestors(conn, row) -> list[Any]:
    """Ancestor rows, nearest first. Cycle-safe."""
    out: list[Any] = []
    seen = {row["id"]}
    current = row
    while current["parent_id"] is not None:
        parent = _visible(conn, current["parent_id"])
        if parent is None or parent["id"] in seen:
            break
        seen.add(parent["id"])
        out.append(parent)
        current = parent
    return out


def _last_touch(row, comment_at: str | None) -> datetime | None:
    """The latest sign of life: edits, claim, or a comment. Falls back to creation."""
    stamps = [_parse_ts(row["updated_at"]), _parse_ts(row["claimed_at"]), _parse_ts(comment_at)]
    stamps = [s for s in stamps if s is not None]
    if stamps:
        return max(stamps)
    return _parse_ts(row["created_at"])


# --- protection ---------------------------------------------------------------


def live_claim_reasons(conn, row, now: datetime) -> list[str]:
    """Why a task is being worked right now: a live lease, an open run, a live delegation."""
    reasons: list[str] = []
    if row["status"] == "doing" and row["claimed_by"]:
        lock = _parse_ts(row["lock_expires_at"])
        if lock is None or lock > now:
            reasons.append(f"live claim by {row['claimed_by']}")
    if row["current_run_id"]:
        reasons.append("open run")
    delegation = conn.execute(
        "SELECT status FROM delegations WHERE task_id=? AND status IN ('pending','in_flight') "
        "LIMIT 1",
        (row["id"],),
    ).fetchone()
    if delegation:
        reasons.append(f"{delegation['status']} delegation")
    return reasons


def gate_reasons(row) -> list[str]:
    """Human-gated rows the sweep and the auto-close rule never act on."""
    reasons: list[str] = []
    lane = config.canonical_task_lane(row["lane"])
    if lane in _HUMAN_ONLY_LANES:
        reasons.append(f"lane {lane}")
    if row["status"] == "waiting_review":
        reasons.append("human gate: waiting_review")
    notes = row["notes"] or ""
    for gate in _GATE_MARKER_RX.findall(notes):
        reasons.append(f"human gate: waiting-for {gate.strip()}")
    if _RED_TIER_RX.search(f"{row['title']}\n{notes}"):
        reasons.append("red-tier row")
    return list(dict.fromkeys(reasons))


# --- staleness ----------------------------------------------------------------


def _active_rows(conn) -> list[Any]:
    clause, params = _tenant_clause()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    return conn.execute(
        "SELECT t.*, c.updated_at AS comment_at FROM tasks t "
        "LEFT JOIN task_comment_threads c ON c.task_id = t.id "
        f"WHERE t.archived_at IS NULL AND t.status IN ({placeholders}) AND t.{clause} "
        "ORDER BY t.id",
        (*ACTIVE_STATUSES, *params),
    ).fetchall()


def stale_tasks(conn, now: datetime, days: int) -> list[dict[str, Any]]:
    """Unparked, unfinished tasks with no claim, status change or note for ``days`` days.

    A task with a live claim, open run or live delegation is being worked and is
    never stale. The boundary is inclusive: exactly ``days`` days is stale.
    """
    cutoff = now - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for row in _active_rows(conn):
        touched = _last_touch(row, row["comment_at"])
        if touched is None or touched > cutoff:
            continue
        if live_claim_reasons(conn, row, now):
            continue
        out.append(
            {
                "row": row,
                "last_touched": _iso(touched),
                "age_days": round((now - touched).total_seconds() / 86400, 1),
            }
        )
    return out


def _protection(conn, row, now: datetime) -> list[str]:
    reasons = gate_reasons(row)
    if config.canonical_task_lane(row["lane"]) == "tracking":
        reasons.append("tracking lane")
    deployment = task_liveness.deployment_wait_blocker(conn, int(row["id"]))
    if deployment:
        reasons.append(deployment)
    return reasons + live_claim_reasons(conn, row, now)


# --- merged-PR evidence (pr-watch cursor; no network) --------------------------


def pr_watch_cursor_path() -> Path:
    return config.data_home() / PR_WATCH_CURSOR_FILE


def load_pr_index(path: Path | None = None) -> list[dict[str, Any]]:
    """Merged and open PRs from the pr-watch cursor. Local file read only.

    Closed-unmerged PRs are dropped: an abandoned attempt must not make a later
    merged PR on the same branch look ambiguous.
    """
    target = path or pr_watch_cursor_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    prs: list[dict[str, Any]] = []
    for repo, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        for number, snap in entries.items():
            if not isinstance(snap, dict):
                continue
            merged = bool(snap.get("merged_at")) or str(snap.get("state") or "").upper() == "MERGED"
            if not merged and str(snap.get("state") or "").upper() != "OPEN":
                continue
            prs.append(
                {
                    "repo": str(repo),
                    "number": int(number),
                    "url": f"https://github.com/{repo}/pull/{int(number)}",
                    "merged": merged,
                    "head_branch": str(snap.get("head_branch") or ""),
                    "head_sha": str(snap.get("head_sha") or ""),
                    "merge_sha": str(snap.get("merge_sha") or ""),
                    "merged_at": snap.get("merged_at"),
                }
            )
    return prs


def _task_signals(row) -> dict[str, Any]:
    """What a task says about its PR: referenced PRs, SHAs, and its branch."""
    evidence = str(row["evidence_ref"] or "")
    refs = {(m.group(1).lower(), int(m.group(2))) for m in _PR_URL_RX.finditer(evidence)}
    refs |= {(m.group(1).lower(), int(m.group(2))) for m in _PR_SHORT_RX.finditer(evidence)}
    shas = {m.group(0).lower() for m in _SHA_RX.finditer(evidence)}
    return {"refs": refs, "shas": shas, "branch": str(row["branch"] or "")}


def _match_pr(row, pr: dict[str, Any]) -> str | None:
    """How this task points at this PR: ``evidence_ref``, ``branch``, or None."""
    from . import merge

    signals = _task_signals(row)
    if (pr["repo"].lower(), pr["number"]) in signals["refs"]:
        return "evidence_ref"
    for sha in signals["shas"]:
        for candidate in (pr["merge_sha"], pr["head_sha"]):
            if candidate and (
                candidate.lower().startswith(sha) or sha.startswith(candidate.lower())
            ):
                return "evidence_ref"
    branch = signals["branch"]
    if branch and branch == pr["head_branch"] and merge.names_task_id(branch, row["id"]):
        return "branch"
    return None


def merge_matches(
    conn, now: datetime, prs: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Match unfinished tasks to PRs. Returns {unambiguous, ambiguous, blocked}.

    A task is matched only when exactly ONE PR (merged or still open) is named by
    its ``evidence_ref`` or its branch, and that PR is merged with a recorded SHA
    in hand. Two matches, or an open PR alongside the merged one, is ambiguous
    and never closes automatically. Tasks the rules forbid acting on come back
    under ``blocked`` with the reasons.
    """
    from . import merge

    unambiguous: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in _active_rows(conn):
        hits = []
        for pr in prs:
            how = _match_pr(row, pr)
            if how:
                hits.append({**pr, "matched_by": how})
        if not hits:
            continue
        merged_hits = [h for h in hits if h["merged"]]
        if not merged_hits:
            continue
        base = {"task_id": row["id"], "title": row["title"], "status": row["status"]}
        if len(hits) != 1:
            ambiguous.append(
                {
                    **base,
                    "reason": f"{len(hits)} PRs match this task",
                    "candidates": [
                        {"pr": h["url"], "merged": h["merged"], "matched_by": h["matched_by"]}
                        for h in hits
                    ],
                }
            )
            continue
        hit = hits[0]
        reasons = _protection(conn, row, now)
        # A task often cites the PR it follows up on ("PR #N follow-ups"). Only
        # a PR whose head branch names this task is this task's own work.
        if not merge.names_task_id(hit["head_branch"], row["id"]):
            reasons.append("PR head branch does not name this task (likely a follow-up of that PR)")
        open_children = [c["id"] for c in _descendants(conn, row["id"]) if _is_active(c)]
        if open_children:
            reasons.append("open child task(s): " + ", ".join(f"#{i}" for i in open_children[:10]))
        entry = {
            **base,
            "pr": hit["url"],
            "merge_sha": hit["merge_sha"] or None,
            "matched_by": hit["matched_by"],
            "prior_evidence_ref": row["evidence_ref"],
            "task_revision": row["updated_at"],
        }
        if reasons:
            blocked.append({**entry, "reason": "; ".join(reasons)})
        elif not hit["merge_sha"]:
            blocked.append(
                {**entry, "reason": "merge SHA not recorded yet (the PR watcher has not seen it)"}
            )
        else:
            unambiguous.append(entry)
    return {"unambiguous": unambiguous, "ambiguous": ambiguous, "blocked": blocked}


def _is_active(row) -> bool:
    return row["status"] in ACTIVE_STATUSES and row["archived_at"] is None


def _closure_blocker(conn, row):
    """Re-checked inside the completion write: a claim or delegation appeared meanwhile."""
    reasons = live_claim_reasons(conn, row, _clock())
    return "; ".join(reasons) or None


def _release_closed_worktree(task_id: int) -> str | None:
    """Release a closed task's worktree and branch, as ``task_update(status="done")`` does.

    Best-effort and non-fatal: the close is already durable. A worktree holding
    unmerged or uncommitted work is kept and reported as ``retained:<reason>``.
    """
    from .tools import tasks as tasks_tool

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT worktree_path, branch, project, notes, status, updated_at, "
            "current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None or not row["worktree_path"]:
            return None
        if row["status"] != "done":
            # Reopened (and possibly reclaimed) since this pass closed it: the
            # worktree now belongs to the new claim, so leave it alone.
            return "skipped:reopened"
        try:
            status = tasks_tool._release_task_worktree(
                row["worktree_path"], row["branch"], project=row["project"], notes=row["notes"]
            )
        except Exception as exc:  # noqa: BLE001 - never undo a durable close
            return f"release_failed: {type(exc).__name__}"
        if status in ("removed", "already_gone"):
            conn.execute(
                "UPDATE tasks SET worktree_path=NULL, branch=NULL WHERE id=? AND status=? "
                "AND updated_at=? AND worktree_path IS ?",
                (task_id, row["status"], row["updated_at"], row["worktree_path"]),
            )
            conn.commit()
        return status
    finally:
        conn.close()


def close_on_merge(
    *, dry_run: bool = True, prs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Close tasks whose single matching PR has merged, with the merge SHA as evidence_ref.

    Reads the pr-watch cursor (no network). ``dry_run`` (the default here) reports
    what would close and writes nothing. Never closes: an ambiguous match, a task
    with a live claim/lease/run/delegation, an operator-only or ``tracking`` row, a
    human-gated or red-tier row, a task with open children, or a PR whose merge SHA
    is not recorded. Nothing here looks at a date.
    """
    storage.require_live_task_store()
    from . import run_records

    now = _clock()
    index = prs if prs is not None else load_pr_index()
    conn = storage.connect()
    try:
        matches = merge_matches(conn, now, index)
    finally:
        conn.close()
    result: dict[str, Any] = {
        "as_of": _iso(now),
        "dry_run": dry_run,
        "prs_considered": len(index),
        "would_close": matches["unambiguous"] if dry_run else [],
        "closed": [],
        "skipped": [],
        "ambiguous": matches["ambiguous"],
        "blocked": matches["blocked"],
    }
    if dry_run:
        return result
    for entry in matches["unambiguous"]:
        expected = run_records.capture_direct_completion(entry["task_id"])
        # The match was made on the row as it was then. If the task has been edited
        # since (branch, evidence, lane, notes, status), the match no longer
        # describes it: skip, and let the next pass re-evaluate the new row.
        if expected is None or expected["updated_at"] != entry["task_revision"]:
            result["skipped"].append({**entry, "reason": "task changed since it was matched"})
            continue
        prior = (
            f"; prior evidence_ref: {entry['prior_evidence_ref']}"
            if entry["prior_evidence_ref"]
            else ""
        )
        note = (
            f"[queue-closure: closed-on-merge @ {_iso(now)}] PR {entry['pr']} "
            f"merge_sha={entry['merge_sha']} (matched by {entry['matched_by']}{prior})"
        )
        done = run_records.complete_direct(
            expected,
            actor=_SYSTEM_ACTOR,
            note=note,
            blocker=_closure_blocker,
            evidence_ref=entry["merge_sha"],
        )
        if done:
            entry = {**entry, "worktree": _release_closed_worktree(entry["task_id"])}
            result["closed"].append(entry)
            _record("queue.closed_on_merge", entry["task_id"], _SYSTEM_ACTOR, entry)
        else:
            result["skipped"].append({**entry, "reason": "task changed or became claimed"})
    if result["closed"]:
        _after_write([e["task_id"] for e in result["closed"]])
    return result


# --- park / fold / unpark -----------------------------------------------------


def _record(kind: str, task_id: int, who: str, payload: dict[str, Any]) -> None:
    try:
        events.record_event(
            kind, payload={"task_id": task_id, **payload}, originator=who, notify=False
        )
    except Exception:  # noqa: BLE001 - the notes line is the durable record
        pass


def _after_write(task_ids: list[int]) -> None:
    from .change_feed import task_changed
    from .sync import schedule_sync_to_vault

    schedule_sync_to_vault()
    for task_id in task_ids:
        task_changed(task_id)


def _write_closure(conn, row, line: str, *, archived: bool) -> None:
    from .tools import tasks as tasks_tool

    revision = tasks_tool._next_task_revision(row["updated_at"])
    cur = conn.execute(
        "UPDATE tasks SET archived_at=?, notes=?, updated_at=? WHERE id=? AND updated_at=?",
        (
            storage.now() if archived else None,
            _append_line(row["notes"], line),
            revision,
            row["id"],
            row["updated_at"],
        ),
    )
    if cur.rowcount != 1:
        raise ToolError(f"task {row['id']} changed while it was being updated; retry")


def queue_park(
    task_id: int,
    reason: str,
    approved_by: str | None = None,
    fold_children: bool = True,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Park one task with a reason: archive it out of the working queue, recorded in its notes.

    Parking is not cancelling: the task keeps its status and stays findable with
    ``task_list(parked=True)``; ``queue_unpark`` reverses it. Open descendants
    FOLD into it in the same step (``fold_children=False`` refuses instead of
    leaving them open under a parked root). Refused: a finished task, an already
    parked one, and any task or descendant with a live claim, lease, open run or
    live delegation. A row the sweep never proposes (operator-only lane, human
    gate, red-tier) is parked only when ``approved_by`` names who approved it.
    ``approved_by`` is a recorded attestation for the audit trail, not a
    credential: who may call this action at all is decided by the profile and
    policy gate that front ``coordination_manage``.
    """
    text = _clean_reason(reason)
    who = _actor(actor)
    storage.require_live_task_store()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _visible(conn, task_id)
        if row is None:
            raise ToolError(f"no task with id {task_id}")
        if row["status"] in TERMINAL_STATUSES:
            raise ToolError(f"task {task_id} is already {row['status']}; there is nothing to park")
        if row["archived_at"] is not None:
            raise ToolError(f"task {task_id} is already parked or archived")
        now = _clock()
        live = live_claim_reasons(conn, row, now)
        if live:
            raise ToolError(f"task {task_id} cannot be parked: {'; '.join(live)}")
        gated = gate_reasons(row)
        approver = " ".join(str(approved_by or "").split())
        if gated and not approver:
            raise ToolError(
                f"task {task_id} is protected ({'; '.join(gated)}); pass approved_by naming "
                "who approved parking it"
            )
        open_children = [d for d in _descendants(conn, task_id) if _is_active(d)]
        for child in open_children:
            child_live = live_claim_reasons(conn, child, now)
            if child_live:
                raise ToolError(
                    f"task {task_id} cannot be parked: child #{child['id']} has "
                    f"{'; '.join(child_live)}"
                )
        if open_children and not fold_children:
            raise ToolError(
                f"task {task_id} has {len(open_children)} open child task(s); parking it "
                "would leave them open under a parked root. Fold them (the default) or "
                "park them first."
            )
        stamp = _iso(now)
        suffix = f" (approved by {approver})" if approver else ""
        folded_ids = [c["id"] for c in open_children]
        if not dry_run:
            _write_closure(conn, row, f"[parked @ {stamp} by {who}] {text}{suffix}", archived=True)
            for child in open_children:
                _write_closure(
                    conn,
                    child,
                    f"[folded-into: #{task_id} @ {stamp} by {who}] "
                    f"parent #{task_id} parked: {text}{suffix}",
                    archived=True,
                )
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = {
        "dry_run": dry_run,
        "parked": task_id,
        "folded": folded_ids,
        "reason": text,
        "actor": who,
        "reversible_with": "queue_unpark",
    }
    if not dry_run:
        _record("queue.parked", task_id, who, {"reason": text, "folded": folded_ids})
        _after_write([task_id, *folded_ids])
    return result


def queue_fold(
    task_id: int,
    into: int,
    reason: str,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fold an open descendant into its already-parked ancestor ``into``, with a reason.

    For a child that appeared under a root after the root was parked. Refused when
    ``into`` is not parked, is not an ancestor of ``task_id``, or the task is
    finished, already parked, or has a live claim/lease/open run/live delegation.
    """
    text = _clean_reason(reason)
    who = _actor(actor)
    storage.require_live_task_store()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _visible(conn, task_id)
        root = _visible(conn, into)
        if row is None or root is None:
            raise ToolError(f"no task with id {task_id if row is None else into}")
        if not _is_parked_row(root):
            raise ToolError(f"task {into} is not parked; fold only into a parked parent")
        if into not in {a["id"] for a in _ancestors(conn, row)}:
            raise ToolError(f"task {into} is not an ancestor of task {task_id}")
        if not _is_active(row):
            raise ToolError(f"task {task_id} is {row['status']}/archived; nothing to fold")
        live = live_claim_reasons(conn, row, _clock())
        if live:
            raise ToolError(f"task {task_id} cannot be folded: {'; '.join(live)}")
        if not dry_run:
            _write_closure(
                conn,
                row,
                f"[folded-into: #{into} @ {_iso(_clock())} by {who}] {text}",
                archived=True,
            )
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not dry_run:
        _record("queue.folded", task_id, who, {"into": into, "reason": text})
        _after_write([task_id])
    return {"dry_run": dry_run, "folded": task_id, "into": into, "reason": text, "actor": who}


def queue_unpark(
    task_id: int,
    reason: str,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reverse a park: restore the task, and the children folded into it, to the queue.

    The task keeps its status. The reason is recorded in notes and the task's
    staleness clock restarts. A folded child is restored through its parked root,
    not on its own, so no open child is left under a parked root.
    """
    text = _clean_reason(reason)
    who = _actor(actor)
    storage.require_live_task_store()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _visible(conn, task_id)
        if row is None:
            raise ToolError(f"no task with id {task_id}")
        if not _is_parked_row(row):
            raise ToolError(f"task {task_id} is not parked")
        parked_ancestors = [a["id"] for a in _ancestors(conn, row) if _is_parked_row(a)]
        if parked_ancestors:
            raise ToolError(
                f"task {task_id} sits under parked task #{parked_ancestors[-1]}; unpark that "
                "root instead (it restores the children folded into it)"
            )
        targets = [row]
        # Always restore the children folded into this root: restoring the parent
        # alone would leave them hidden under a live parent.
        for child in _descendants(conn, task_id):
            state = closure_state(child["notes"])
            if (
                _is_parked_row(child)
                and state
                and state["kind"] == "folded"
                and state["root"] == task_id
            ):
                targets.append(child)
        stamp = _iso(_clock())
        if not dry_run:
            for target in targets:
                _write_closure(
                    conn, target, f"[unparked @ {stamp} by {who}] {text}", archived=False
                )
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    restored = [t["id"] for t in targets]
    if not dry_run:
        _record("queue.unparked", task_id, who, {"reason": text, "restored": restored})
        _after_write(restored)
    return {
        "dry_run": dry_run,
        "unparked": task_id,
        "restored": restored,
        "reason": text,
        "actor": who,
    }


# --- the dry-run sweep --------------------------------------------------------


def queue_sweep(stale_days: int | None = None) -> dict[str, Any]:
    """PROPOSE closure actions for the operator to approve. Writes nothing.

    Each proposal is {task_id, title, proposed_action: park|fold|close, reason,
    evidence, auto}. ``auto`` is true only for an unambiguous close-on-merge,
    the one rule that may run without approval; park and fold never do.

    * ``close``: the task's evidence_ref or branch matches exactly one merged PR.
    * ``fold``: an open task still under a parked root.
    * ``park``: untouched for ``stale_days`` (30) and not protected. With its open
      descendants listed as ``fold`` proposals so no child is orphaned.

    Never proposed for park or close: operator-only/interactive-only lanes,
    ``tracking`` rows (except as a fold under a parked parent), human-gated or
    red-tier rows, and anything with a live claim, lease, open run or live
    delegation. They are reported under ``skipped`` with the reason.
    """
    storage.require_live_task_store()
    days = stale_days_setting(stale_days)
    now = _clock()
    index = load_pr_index()
    conn = storage.connect()
    try:
        merges = merge_matches(conn, now, index)
        proposals: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        proposed_ids: set[int] = set()

        for entry in merges["unambiguous"]:
            proposed_ids.add(entry["task_id"])
            proposals.append(
                {
                    "task_id": entry["task_id"],
                    "title": entry["title"],
                    "proposed_action": "close",
                    "reason": f"its PR merged ({entry['matched_by']} names exactly one PR)",
                    "evidence": {
                        "pr": entry["pr"],
                        "merge_sha": entry["merge_sha"],
                        "matched_by": entry["matched_by"],
                    },
                    "auto": True,
                }
            )
        for entry in merges["blocked"]:
            skipped.append(
                {
                    "task_id": entry["task_id"],
                    "title": entry["title"],
                    "would_be": "close",
                    "reason": entry["reason"],
                    "evidence": {"pr": entry["pr"]},
                }
            )

        clause, tenant_params = _tenant_clause()
        parked_roots = [
            r
            for r in conn.execute(
                "SELECT * FROM tasks WHERE archived_at IS NOT NULL "
                f"AND status NOT IN ('done','cancelled') AND {clause}",
                tenant_params,
            ).fetchall()
            if (closure_state(r["notes"]) or {}).get("kind") != "folded"
        ]
        for root in parked_roots:
            state = closure_state(root["notes"]) or {}
            for child in _descendants(conn, root["id"]):
                if not _is_active(child) or child["id"] in proposed_ids:
                    continue
                live = live_claim_reasons(conn, child, now)
                if live:
                    skipped.append(
                        {
                            "task_id": child["id"],
                            "title": child["title"],
                            "would_be": "fold",
                            "reason": "; ".join(live),
                            "evidence": {"parked_root": root["id"]},
                        }
                    )
                    continue
                proposed_ids.add(child["id"])
                proposals.append(
                    {
                        "task_id": child["id"],
                        "title": child["title"],
                        "proposed_action": "fold",
                        "reason": f"open child under parked root #{root['id']}",
                        "evidence": {
                            "parked_root": root["id"],
                            "root_reason": state.get("reason"),
                        },
                        "auto": False,
                    }
                )

        # Ancestors first, so a stale child is covered by its stale parent's fold list
        # instead of being proposed for park on its own.
        stale = sorted(
            stale_tasks(conn, now, days),
            key=lambda s: (len(_ancestors(conn, s["row"])), s["row"]["id"]),
        )
        covered: set[int] = set()
        for item in stale:
            row = item["row"]
            if row["id"] in proposed_ids or row["id"] in covered:
                continue
            reasons = _protection(conn, row, now)
            if reasons:
                skipped.append(
                    {
                        "task_id": row["id"],
                        "title": row["title"],
                        "would_be": "park",
                        "reason": "; ".join(reasons),
                        "evidence": {"last_touched": item["last_touched"]},
                    }
                )
                continue
            open_children = [d for d in _descendants(conn, row["id"]) if _is_active(d)]
            blocking = [
                (c["id"], live_claim_reasons(conn, c, now))
                for c in open_children
                if live_claim_reasons(conn, c, now)
            ]
            if blocking:
                cid, why = blocking[0]
                skipped.append(
                    {
                        "task_id": row["id"],
                        "title": row["title"],
                        "would_be": "park",
                        "reason": f"child #{cid} has {'; '.join(why)}",
                        "evidence": {"last_touched": item["last_touched"]},
                    }
                )
                continue
            proposed_ids.add(row["id"])
            proposals.append(
                {
                    "task_id": row["id"],
                    "title": row["title"],
                    "proposed_action": "park",
                    "reason": (
                        f"no claim, status change or note for {item['age_days']:g} days "
                        f"(threshold {days})"
                    ),
                    "evidence": {
                        "last_touched": item["last_touched"],
                        "age_days": item["age_days"],
                        "status": row["status"],
                        "fold_children": [c["id"] for c in open_children],
                    },
                    "auto": False,
                }
            )
            for child in open_children:
                covered.add(child["id"])
                if child["id"] in proposed_ids:
                    continue
                proposed_ids.add(child["id"])
                proposals.append(
                    {
                        "task_id": child["id"],
                        "title": child["title"],
                        "proposed_action": "fold",
                        "reason": f"open child of #{row['id']}, proposed for park",
                        "evidence": {"parked_root": row["id"], "root_proposed": True},
                        "auto": False,
                    }
                )
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for proposal in proposals:
        counts[proposal["proposed_action"]] = counts.get(proposal["proposed_action"], 0) + 1
    return {
        "as_of": _iso(now),
        "dry_run": True,
        "writes": 0,
        "stale_days": days,
        "proposals": proposals,
        "skipped": skipped,
        "ambiguous_merges": merges["ambiguous"],
        "counts": counts,
        "note": (
            "Proposals only. Park and fold run only through queue_park / queue_fold after the "
            "operator approves; only an unambiguous close-on-merge (auto: true) acts on its "
            "own. Nothing closes on a date."
        ),
    }


# --- health -------------------------------------------------------------------


def _gate_task_ids(notes: str) -> set[int]:
    """Task ids on a milestone's ``Gate:`` line.

    The list ends at a newline or a sentence end, except that a sentence which
    itself starts with a task id (``Gate: #1 done. #2 also.``) continues it, so a
    later gate is not dropped at a full stop.
    """
    start = _GATE_START_RX.search(notes)
    if start is None:
        return set()
    text = notes[start.end() :].split("\n", 1)[0]
    end = len(text)
    pos = 0
    while True:
        match = _SENTENCE_END_RX.search(text, pos)
        if match is None:
            break
        rest = text[match.end() :].lstrip()
        if rest.startswith("#") and rest[1:2].isdigit():
            pos = match.end()
            continue
        end = match.start()
        break
    return {int(n) for n in _TASK_REF_RX.findall(text[:end])}


def milestone_readiness(conn) -> dict[str, Any]:
    """Milestones whose gate is satisfied while the milestone is still open and unjudged.

    A MILESTONE is an unfinished, unarchived task whose notes carry
    ``[milestone-review]``. Its gate is the task ids on its ``Gate:`` line plus its
    ``task_dependencies`` prerequisites. REVIEW-READY means it has a gate and every
    gate task is ``done``. UNJUDGED means its notes hold no ``VERDICT:`` line (or
    ``[milestone-verdict`` marker). A milestone judged FAIL stays judged until the
    operator clears the verdict. Read only; nothing is closed or flagged in place.
    """
    clause, params = _tenant_clause()
    milestones = conn.execute(
        "SELECT * FROM tasks WHERE archived_at IS NULL "
        f"AND status NOT IN ('done','cancelled') AND {clause} "
        "AND instr(lower(notes), ?) > 0 ORDER BY id",
        (*params, _MILESTONE_MARKER),
    ).fetchall()
    ready: list[dict[str, Any]] = []
    unjudged: list[int] = []
    for row in milestones:
        notes = row["notes"] or ""
        gate_ids: set[int] = set()
        gate_ids |= _gate_task_ids(notes)
        gate_ids |= {
            int(d["depends_on_task_id"])
            for d in conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (row["id"],)
            )
        }
        gate_ids.discard(row["id"])
        if not gate_ids:
            continue
        placeholders = ",".join("?" for _ in gate_ids)
        done = conn.execute(
            f"SELECT COUNT(*) AS n FROM tasks WHERE id IN ({placeholders}) AND status='done'",
            tuple(gate_ids),
        ).fetchone()["n"]
        if done != len(gate_ids):
            continue
        judged = bool(_VERDICT_RX.search(notes))
        ready.append({"task_id": row["id"], "title": row["title"], "judged": judged})
        if not judged:
            unjudged.append(row["id"])
    return {
        "review_ready": len(ready),
        "review_ready_unjudged": len(unjudged),
        "unjudged_ids": unjudged,
        "milestones": ready,
    }


def ownership_states(conn: Any, now: datetime) -> dict[str, Any]:
    """Counts and example ids per stuck-work state (task #2388). Report only."""
    from . import run_records, task_ownership

    clause, params = _tenant_clause()
    now_iso = _iso(now)
    counts = dict.fromkeys(task_ownership.STATES, 0)
    examples: dict[str, list[int]] = {state: [] for state in task_ownership.STATES}
    for row in conn.execute(
        "SELECT t.*, l.wait_until, l.monitor_at FROM tasks t "
        "LEFT JOIN task_liveness l ON l.task_id = t.id "
        f"WHERE t.archived_at IS NULL AND t.status IN ('open','doing','blocked') AND {clause}",
        params,
    ).fetchall():
        run = "open" if row["current_run_id"] else "none"
        if run == "none" and task_ownership.lease_active(row, now_iso):
            run = "ended" if run_records.latest_run_end(conn, row["id"]) else "none"
        state = task_ownership.classify(
            task_ownership.stuck_facts(
                row,
                now=now_iso,
                wait_until=row["wait_until"],
                monitor_at=row["monitor_at"],
                run=run,
            )
        )
        if state is not None:
            counts[state] += 1
            if len(examples[state]) < 5:
                examples[state].append(row["id"])
    return {"counts": counts, "examples": examples}


def deployment_waits(conn: Any, now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Waits bound to a named deployment, split by expiry (task #2414). Read only."""
    clause, params = _tenant_clause()
    now_iso = _iso(now)
    found: dict[str, list[dict[str, Any]]] = {"waiting": [], "expired": []}
    for row in conn.execute(
        "SELECT l.task_id, l.wait_deploy_target, l.wait_deploy_sha, l.wait_until FROM tasks t "
        "JOIN task_liveness l ON l.task_id = t.id WHERE l.wait_deploy_sha IS NOT NULL "
        f"AND t.archived_at IS NULL AND t.status NOT IN ('done','cancelled') AND {clause} "
        "ORDER BY l.wait_until LIMIT 100",
        params,
    ):
        found["expired" if row["wait_until"] <= now_iso else "waiting"].append(
            {
                "task_id": row["task_id"],
                "target": row["wait_deploy_target"],
                "sha": row["wait_deploy_sha"],
                "until": row["wait_until"],
            }
        )
    return found


def queue_health(stale_days: int | None = None) -> dict[str, Any]:
    """Queue health in five numbers: open, parked, stale, and unjudged review-ready milestones.

    ``ready_unowned`` (task #2454) counts unparked active tasks whose explicit
    prerequisites are all closed and that have no next owner, claimant or agent
    lane: work the graph made ready that nobody will be told about.

    ``open`` counts unparked ``open`` tasks (``active`` adds doing, waiting_review
    and blocked). ``parked`` counts unfinished archived tasks, however they got
    there. ``stale`` counts unparked unfinished tasks untouched for ``stale_days``
    (30) with no live claim; ``stale_actionable`` is the subset the sweep may
    propose parking. Read only.
    """
    storage.require_live_task_store()
    days = stale_days_setting(stale_days)
    now = _clock()
    conn = storage.connect()
    try:
        clause, params = _tenant_clause()
        by_status = dict.fromkeys(ACTIVE_STATUSES, 0)
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE archived_at IS NULL "
            f"AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) AND {clause} "
            "GROUP BY status",
            (*ACTIVE_STATUSES, *params),
        ):
            by_status[r["status"]] = r["n"]
        parked_rows = conn.execute(
            "SELECT * FROM tasks WHERE archived_at IS NOT NULL "
            f"AND status NOT IN ('done','cancelled') AND {clause}",
            params,
        ).fetchall()
        folded = sum(
            1 for r in parked_rows if (closure_state(r["notes"]) or {}).get("kind") == "folded"
        )
        stale = stale_tasks(conn, now, days)
        actionable = sum(1 for s in stale if not _protection(conn, s["row"], now))
        milestones = milestone_readiness(conn)
        ownership = ownership_states(conn, now)
        waits = deployment_waits(conn, now)
        ready_unowned = ready_dependents.ready_unowned_count(conn, clause, params)
    finally:
        conn.close()
    return {
        "as_of": _iso(now),
        "stale_days": days,
        "open": by_status["open"],
        "active": sum(by_status.values()),
        "by_status": by_status,
        "parked": len(parked_rows),
        "parked_folded": folded,
        "stale": len(stale),
        "stale_actionable": actionable,
        "milestones_review_ready_unjudged": milestones["review_ready_unjudged"],
        "milestones": milestones,
        "ownership_states": ownership["counts"],
        "ownership_examples": ownership["examples"],
        "deployment_waits": waits,
        "ready_unowned": ready_unowned,
    }


def queue_stale(stale_days: int | None = None, limit: int = 200) -> dict[str, Any]:
    """List stale tasks (read only): oldest first, with the clock inputs and any protection."""
    storage.require_live_task_store()
    days = stale_days_setting(stale_days)
    now = _clock()
    conn = storage.connect()
    try:
        items = sorted(stale_tasks(conn, now, days), key=lambda s: s["last_touched"])
        rows = [
            {
                "task_id": s["row"]["id"],
                "title": s["row"]["title"],
                "status": s["row"]["status"],
                "lane": s["row"]["lane"],
                "last_touched": s["last_touched"],
                "age_days": s["age_days"],
                "protected": _protection(conn, s["row"], now),
            }
            for s in items
        ]
    finally:
        conn.close()
    cap = max(1, min(int(limit), 1000))
    return {
        "as_of": _iso(now),
        "stale_days": days,
        "total": len(rows),
        "tasks": rows[:cap],
        "truncated": len(rows) > cap,
    }


def queue_close_merged(dry_run: bool = True) -> dict[str, Any]:
    """Close-on-merge as a callable action: dry run by default; ``dry_run=False`` closes."""
    return close_on_merge(dry_run=dry_run)


def describe_parked(row_notes: str | None) -> dict[str, Any] | None:
    """The parked/folded details for ``task_list`` rows in the parked view."""
    state = closure_state(row_notes)
    if state is None or state["kind"] == "unparked":
        return {"kind": None, "reason": None, "by": None, "at": None, "root": None}
    return state
