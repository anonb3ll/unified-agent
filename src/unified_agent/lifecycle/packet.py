"""Bounded session-start / resume packet derived from existing stores (Task #482)."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, memory_ranking, storage
from ..telemetry.redaction import redact_sensitive_content, sanitize_metadata
from ..tools.approvals import _pass_expires_at
from . import handoff as session_handoff
from .schema import CHANNEL_LIFECYCLE_PREFIX, EXCLUDED_PAYLOAD_KEYS

_log = logging.getLogger(__name__)

PACKET_SCHEMA = "ua.session_packet.v1"
PACKET_BYTE_LIMIT = 16384
_NOTES_LIMIT = 400
_SPEC_LIMIT = 200
_MEMORY_LIMIT = 5
_MEMORY_CANDIDATE_LIMIT = 50
_DELEGATION_LIMIT = 5
_APPROVAL_LIMIT = 5
_EVENT_LIMIT = 10
_LIFECYCLE_LIMIT = 10
_EXPERIENCE_LIMIT = 3
_EXPERIENCE_SUMMARY_LIMIT = 240
_EVIDENCE_LIMIT = 5
_ENDED_SESSION_STATUSES = ("completed", "failed", "abandoned")

_PACKET_LABELS = {
    "role": "historical_context",
    "authority": False,
    "transcript_included": False,
    "instructions": "system/developer/user instructions remain authoritative",
    "approvals": "live approval state remains authoritative",
}


def build_session_packet(
    *,
    task_id: int | None = None,
    session_id: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Derive a resume packet. Never reads a transcript file."""
    conn = storage.connect()
    try:
        session = _load_session(conn, session_id) if session_id else None
        if session is not None and not _session_in_tenant(conn, session):
            # Another tenant's session reads as not found: no section, no
            # worktree, and nothing looked up by its id (task #1985).
            session, session_id = None, None
        if session is not None and task_id is not None:
            session_task = session.get("task_id")
            if session_task is not None and int(session_task) != int(task_id):
                raise ToolError(
                    f"session {session_id} is anchored to task_id={session_task}, "
                    f"not task_id={task_id}"
                )
        if task_id is None and session:
            task_id = session.get("task_id")
        task = _load_task(conn, task_id) if task_id is not None else None
        # A task outside the caller's tenant reads as missing, and so does
        # every section derived from it (task #1985).
        task_id = task_id if task is not None else None
        worktree = _worktree_from(task, session)
        session_section = _session_section(session)
        delegations = _load_delegations(conn, task_id) if task_id is not None else []
        approvals = _load_approvals(conn, task_id) if task_id is not None else []
        experience, experience_reservation = _load_experience(conn, task, session_id)
        memory_hits = _load_memory(conn, task, token_budget_used=experience_reservation)
        prior_handoff = _load_prior_handoff(task_id, session_id)
        handoff_note, context_epoch = _load_session_handoff(
            session_id if session is not None else None
        )
        lifecycle = _load_lifecycle(task_id, session_id)
        recent_events = _load_recent_events(task_id, session_id)
        operating = _load_operating_profile(conn, task_id)
        workflow = _load_workflow(conn, task_id)
        generated_at = storage.now()
    finally:
        conn.close()

    why_chain = _why_chain(task_id)
    packet: dict[str, Any] = {
        "schema": PACKET_SCHEMA,
        "labels": dict(_PACKET_LABELS),
        "generated_at": generated_at,
        "requested_by": agent,
        "session_id": session_id or (session.get("id") if session else None),
        "session": session_section,
        "task": task,
        "worktree": worktree,
        "delegations": delegations,
        "approvals": approvals,
        "recent_events": recent_events,
        "memory": memory_hits,
        "experience": experience,
        "prior_handoff": prior_handoff,
        "lifecycle": lifecycle,
        "why_chain": why_chain,
        "operating_profile": operating,
        "handoff_note": handoff_note,
        "context_epoch": context_epoch,
        "workflow": workflow,
    }
    packet["labels"]["source_tiers"] = session_handoff.label_packet_sources(packet)
    return _bound_packet(packet)


def comparable_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Task-derived sections two agents can resume from.

    Drops per-caller fields (who asked, when, which session they opened).
    """
    return {
        "schema": packet.get("schema"),
        "labels": packet.get("labels"),
        "task": packet.get("task"),
        "worktree": packet.get("worktree"),
        "delegations": packet.get("delegations"),
        "approvals": packet.get("approvals"),
        "memory": packet.get("memory"),
        # experience is left out: it excludes the caller's own session, so it
        # is caller-dependent by design.
        "prior_handoff": packet.get("prior_handoff"),
        "why_chain": packet.get("why_chain"),
        "operating_profile": packet.get("operating_profile"),
        "workflow": packet.get("workflow"),
    }


def _load_session_handoff(
    session_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The session's sealed handoff note and context epoch (task #2397).

    Packets are on the session-start path, so a failed read is logged and
    leaves the section ``None`` rather than failing the packet.
    """
    if not session_id:
        return None, None
    note = epoch = None
    try:
        note = session_handoff.recover(session_id)
    except Exception as exc:  # noqa: BLE001 - never fail packet assembly
        _log.warning("session packet: handoff note read failed: %s", type(exc).__name__)
    try:
        epoch = session_handoff.current_epoch(session_id)
    except Exception as exc:  # noqa: BLE001 - never fail packet assembly
        _log.warning("session packet: context epoch read failed: %s", type(exc).__name__)
    return note, epoch


def _load_operating_profile(conn: Any, task_id: int | None) -> dict[str, Any] | None:
    """The open run's planning level and standing instructions (task #1756)."""
    if task_id is None:
        return None
    from .. import operating_profiles

    try:
        snapshot = operating_profiles.run_snapshot(conn, int(task_id))
    except Exception:  # noqa: BLE001 - the packet is advisory; a gap is not an error
        return None
    brief = operating_profiles.brief_section(snapshot)
    if brief is None:
        return None
    settings = snapshot.get("settings") or {}
    planning = settings.get("planning")
    return {
        "planning": planning,
        "planning_meaning": operating_profiles.PLANNING_MEANINGS.get(planning),
        "instructions": settings.get("instructions"),
        "fingerprint": snapshot.get("fingerprint"),
        "brief": brief,
    }


def _load_workflow(conn: Any, task_id: int | None) -> dict[str, Any] | None:
    """The task's stage, resume capsule and check grades (task #2398).

    Agent-written (T2) apart from ``verified`` grades. A failed read leaves the
    section out; the packet still builds.
    """
    if task_id is None:
        return None
    try:
        from .. import task_workflow

        return task_workflow.packet_section(conn, int(task_id))
    except Exception:  # noqa: BLE001 - an optional section never fails the packet
        return None


def _why_chain(task_id: int | None) -> dict[str, Any] | None:
    if task_id is None:
        return None
    from ..goals import why_chain

    try:
        payload = why_chain(int(task_id))
    except ToolError:
        return None
    chain = []
    for goal in payload.get("chain") or []:
        if not isinstance(goal, dict):
            continue
        item = dict(goal)
        item["title"] = redact_sensitive_content(str(item.get("title") or ""), max_len=200)
        if "statement" in item:
            item["statement"] = redact_sensitive_content(
                str(item.get("statement") or ""), max_len=500
            )
        chain.append(item)
    return {**payload, "chain": chain}


def _load_session(conn: Any, session_id: str) -> dict[str, Any] | None:
    """The session, or ``None`` if missing or another tenant's (task #1995)."""
    row = conn.execute(
        "SELECT id, agent, kind, status, task_id, worktree_path, branch, started_at, "
        "ended_at, summary, org_id FROM agent_sessions WHERE id=? AND org_id IS ?",
        (session_id, edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _session_in_tenant(conn: Any, session: dict[str, Any]) -> bool:
    """Whether the caller's tenant may see this session (tasks #1985, #1995).

    A session records the tenant that opened it (``agent_sessions.org_id``);
    ``_load_session`` already reads only the caller's tenant's rows, so this
    is a second check on the row it returned, kept so a session loaded any
    other way cannot cross tenants either.
    """
    del conn
    return session.get("org_id") == edition.tenant_org_id()


def _session_section(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bounded session summary for the packet — no transcript paths."""
    if not session:
        return None
    return {
        "id": session.get("id"),
        "agent": session.get("agent"),
        "kind": session.get("kind"),
        "status": session.get("status"),
        "ended_at": session.get("ended_at"),
        "summary": redact_sensitive_content(session.get("summary") or "", max_len=400),
        "started_at": session.get("started_at"),
    }


def _load_task(conn: Any, task_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, title, status, priority, claimed_by, notes, worktree_path, branch, "
        "project, parent_id, updated_at FROM tasks WHERE id=? AND org_id IS ?",
        (task_id, edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        return None
    notes = redact_sensitive_content(row["notes"] or "", max_len=_NOTES_LIMIT)
    return {
        "id": row["id"],
        "title": redact_sensitive_content(row["title"] or "", max_len=200),
        "status": row["status"],
        "priority": row["priority"],
        "claimed_by": row["claimed_by"],
        "notes": notes,
        "worktree_path": row["worktree_path"],
        "branch": row["branch"],
        "project": row["project"],
        "parent_id": row["parent_id"],
        "updated_at": row["updated_at"],
    }


def _worktree_from(
    task: dict[str, Any] | None, session: dict[str, Any] | None
) -> dict[str, Any] | None:
    path = (task or {}).get("worktree_path") or (session or {}).get("worktree_path")
    branch = (task or {}).get("branch") or (session or {}).get("branch")
    if not path and not branch:
        return None
    return {"path": path, "branch": branch}


def _load_delegations(conn: Any, task_id: int) -> list[dict[str, Any]]:
    tenant = edition.tenant_org_id()
    rows = conn.execute(
        "SELECT id, from_agent, to_agent, task_id, status, deadline, created_at, task_spec "
        "FROM delegations WHERE org_id IS ? AND (task_id=? OR task_id IN "
        "(SELECT id FROM tasks WHERE parent_id=? AND org_id IS ?)) "
        "ORDER BY created_at DESC LIMIT ?",
        (tenant, task_id, task_id, tenant, _DELEGATION_LIMIT),
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "task_id": row["task_id"],
                "status": row["status"],
                "deadline": row["deadline"],
                "created_at": row["created_at"],
                "task_spec": redact_sensitive_content(row["task_spec"] or "", max_len=_SPEC_LIMIT),
            }
        )
    return out


def _load_approvals(conn: Any, task_id: int) -> list[dict[str, Any]]:
    """Live approvals for this task only, in the caller's tenant (task #1985).

    The packet used to list the newest approvals hub-wide, copying other
    agents' approval metadata into every session's packet. Linked the way the
    evidence report links them: the gated payload's ``task_id`` or
    ``parent_task_id``.
    """
    key = str(int(task_id))
    rows = conn.execute(
        "SELECT id, tool, status, requested_by, created_at, resolved_by, resolved_at "
        "FROM approvals WHERE status IN ('pending','approved') AND org_id IS ? "
        "AND json_valid(payload) AND (CAST(json_extract(payload, '$.task_id') AS TEXT) = ? "
        "OR CAST(json_extract(payload, '$.parent_task_id') AS TEXT) = ?) "
        "ORDER BY id DESC LIMIT ?",
        (edition.tenant_org_id(), key, key, _APPROVAL_LIMIT * 5),
    ).fetchall()
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] == "approved":
            expires_at = _pass_expires_at(row)
            if expires_at is None or now >= expires_at:
                continue
        out.append(
            {
                "id": row["id"],
                "tool": row["tool"],
                "status": row["status"],
                "requested_by": row["requested_by"],
                "created_at": row["created_at"],
                "resolved_by": row["resolved_by"],
            }
        )
        if len(out) >= _APPROVAL_LIMIT:
            break
    return out


def _task_in_tenant(conn: Any, task: dict[str, Any]) -> bool:
    row = conn.execute("SELECT org_id FROM tasks WHERE id=?", (task["id"],)).fetchone()
    return row is not None and row["org_id"] == edition.tenant_org_id()


def _load_experience(
    conn: Any, task: dict[str, Any] | None, session_id: str | None
) -> tuple[list[dict[str, Any]], int]:
    """Prior outcomes and a caller-independent token reservation (task #1459).

    Each record is hub-observed (who ran it, how it ended, when), so it is
    labelled ``fact``; the summary is the agent's own report and says so. The
    caller's own session is excluded. Bounded by count and summary length.
    Any ended session reserves half the cap for all callers, even when that
    caller has no remaining experience or its summaries do not fit. Tasks
    without ended sessions leave the full cap available to memory.
    """
    if not task:
        return [], 0
    try:
        if not _task_in_tenant(conn, task):
            return [], 0
        placeholders = ",".join("?" for _ in _ENDED_SESSION_STATUSES)
        rows = conn.execute(
            "SELECT id, agent, status, exit_status, ended_at, summary, delegation_id "
            f"FROM agent_sessions WHERE task_id=? AND org_id IS ? AND status IN ({placeholders}) "
            "ORDER BY ended_at DESC, id DESC LIMIT ?",
            (task["id"], edition.tenant_org_id(), *_ENDED_SESSION_STATUSES, _EXPERIENCE_LIMIT + 1),
        ).fetchall()
    except Exception:
        return [], 0
    # Reserve before caller exclusion so every resume selects the same memory.
    budget = config.memory_inject_token_cap() // 2 if rows else 0
    rows = [row for row in rows if row["id"] != session_id][:_EXPERIENCE_LIMIT]
    used = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        evidence = [f"session:{row['id']}", f"task:{task['id']}"]
        if row["delegation_id"] is not None:
            evidence.append(f"delegation:{row['delegation_id']}")
        out.append(
            {
                "kind": "session_outcome",
                "epistemic": "fact",
                # Agent-supplied free text is redacted and capped so one
                # session cannot blow the packet past its byte bound.
                "agent": redact_sensitive_content(row["agent"] or "", max_len=64),
                "status": row["status"],
                "exit_status": redact_sensitive_content(row["exit_status"] or "", max_len=64),
                "ended_at": row["ended_at"],
                "summary": redact_sensitive_content(
                    row["summary"] or "", max_len=_EXPERIENCE_SUMMARY_LIMIT
                ),
                "summary_source": "agent_reported",
                "evidence": evidence,
            }
        )
        cost = _experience_token_count(out[-1])
        if used + cost > budget:
            out.pop()
            continue
        used += cost
    return out, budget


def _experience_token_count(item: dict[str, Any]) -> int:
    """Every injected value counts against the shared budget, evidence included."""
    parts = [
        item.get(key) or ""
        for key in ("agent", "status", "exit_status", "ended_at", "summary", "epistemic")
    ]
    parts.extend(item.get("evidence") or [])
    return max(len(" ".join(str(part) for part in parts).split()), 1)


def _load_memory(
    conn: Any, task: dict[str, Any] | None, *, token_budget_used: int = 0
) -> list[dict[str, Any]]:
    """Ranked, capped, thresholded memory for automatic context injection.

    Explicit ``memory_search`` is a different path (it adds semantic and
    entity legs). Candidates here are an OR of up to eight title words, ranked
    by project scope, then bm25, then injection confidence.
    No task / no searchable title → empty list (do not dump the store).
    Project and tenant eligibility precede the candidate limit.
    ``token_budget_used`` is the caller-independent experience reservation;
    memory gets the rest of ``memory_inject_token_cap``.
    """
    if not task:
        return []
    title = (task.get("title") or "").strip()
    tokens = [tok for tok in title.replace('"', " ").split() if len(tok) >= 3]
    # OR of the title's content words, ranked by bm25 (task #1913). An AND of
    # every title word matched nothing for most real titles, because a title
    # rarely repeats a note's wording word for word.
    fts_query = memory_ranking.fts_or_query(memory_ranking.or_tokens(tokens)[:8])
    if not fts_query:
        fts_query = memory_ranking.fts_and_query(tokens[:8])
    if not fts_query:
        return []
    item_cap = config.memory_inject_item_cap()
    token_cap = max(config.memory_inject_token_cap() - max(token_budget_used, 0), 0)
    threshold = config.memory_inject_confidence_threshold()
    candidate_limit = max(_MEMORY_CANDIDATE_LIMIT, item_cap, _MEMORY_LIMIT)
    project = task.get("project")
    org_id = edition.tenant_org_id()
    now = datetime.now(UTC)

    def eligible(confidence, salience, expires_at, author):
        row = {
            "confidence": confidence,
            "salience": salience,
            "expires_at": expires_at,
            "author": author,
        }
        return _memory_row_eligible(row, now, threshold)

    def confidence_value(confidence, salience):
        return _injection_confidence({"confidence": confidence, "salience": salience})

    try:
        if not _task_in_tenant(conn, task):
            return []
        # A connection-local read predicate uses the complete stored attribution,
        # not the truncated tags projected into the bounded packet. No new store.
        conn.create_function("packet_project_rank", 2, _project_scope_rank, deterministic=True)
        conn.create_function("packet_memory_eligible", 4, eligible)
        conn.create_function("packet_confidence", 2, confidence_value, deterministic=True)
        rows = conn.execute(
            "SELECT m.id, m.title, m.tags, m.confidence, m.salience, "
            "m.expires_at, m.author, m.epistemic, m.evidence, m.valid_from, "
            "packet_project_rank(m.tags, ?) AS project_rank, "
            "bm25(memory_fts) AS lexical_score, "
            "snippet(memory_fts, 1, '[', ']', ' … ', 16) AS snippet "
            "FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
            "WHERE memory_fts MATCH ? AND m.superseded_by IS NULL "
            "AND m.id NOT LIKE 'masterindex%' AND m.id NOT LIKE 'tagindex_%' "
            "AND instr(m.tags, '\"meta:deleted\"') = 0 "
            "AND m.org_id IS ? AND packet_project_rank(m.tags, ?) >= 0 "
            "AND packet_memory_eligible(m.confidence, m.salience, m.expires_at, m.author) "
            "ORDER BY project_rank, lexical_score, "
            "packet_confidence(m.confidence, m.salience) DESC, "
            "lower(m.title), m.id LIMIT ?",
            (project, fts_query, org_id, project, candidate_limit),
        ).fetchall()
    except Exception:
        return []
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for row in rows:
        if not _memory_row_eligible(row, now, threshold):
            continue
        confidence = _injection_confidence(row)
        tags = _parse_tags(row["tags"])
        if "meta:deleted" in tags:
            continue
        hit = {
            "id": row["id"],
            "title": redact_sensitive_content(row["title"] or "", max_len=120),
            "tags": tags,
            "snippet": redact_sensitive_content(row["snippet"] or "", max_len=200),
            # Fact vs inference stays visible in injected context (task #1459);
            # a legacy note without the label reads as "unlabeled".
            "epistemic": row["epistemic"] or "unlabeled",
            "author": redact_sensitive_content(row["author"] or "", max_len=64),
        }
        evidence = _parse_evidence(row["evidence"])
        if evidence:
            hit["evidence"] = evidence
        if row["valid_from"]:
            hit["valid_from"] = row["valid_from"]
        project_rank = row["project_rank"]
        ranked.append(
            (
                (
                    project_rank,
                    row["lexical_score"],
                    -confidence,
                    (hit["title"] or "").lower(),
                    hit["id"] or "",
                ),
                hit,
            )
        )
    ranked.sort(key=lambda item: item[0])
    selected: list[dict[str, Any]] = []
    tokens_used = 0
    for _key, hit in ranked:
        if len(selected) >= item_cap:
            break
        n = _hit_token_count(hit)
        if tokens_used + n > token_cap:
            if selected:
                break
            continue
        selected.append(hit)
        tokens_used += n
    return selected


def _memory_row_eligible(row: Any, now: datetime, threshold: float) -> bool:
    """Reuse automatic injection semantics before SQL candidate truncation."""
    author = row["author"] if "author" in row.keys() else None
    return (
        not _row_expired(row, now)
        and not config.is_agent_untrusted(author)
        and _injection_confidence(row) >= threshold
    )


def _injection_confidence(row: Any) -> float:
    """Prefer stored confidence; fall back to salience (default 0.5) when unset."""
    keys = row.keys()
    raw = row["confidence"] if "confidence" in keys else None
    if raw is not None:
        try:
            val = float(raw)
            if math.isfinite(val):
                return val
        except (TypeError, ValueError):
            pass
    sal = row["salience"] if "salience" in keys else 0.5
    try:
        val = float(sal if sal is not None else 0.5)
    except (TypeError, ValueError):
        return 0.5
    return val if math.isfinite(val) else 0.5


def _project_scope_rank(raw_tags: str | None, project: str | None) -> int:
    """0 = matching project, 1 = legacy global, -1 = outside scope or malformed.

    Multiple project tags explicitly share a note across those projects. Missing
    project attribution preserves legacy global context within the tenant only.
    """
    from ..config import task_list_project_key

    try:
        tags = json.loads(raw_tags or "[]")
    except (TypeError, ValueError):
        return -1
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return -1
    projects = [
        tag.strip().lower()[8:].strip()
        for tag in tags
        if tag.strip().lower().startswith("project:")
    ]
    if not projects:
        return 1
    if not project or not str(project).strip():
        return -1
    wanted = task_list_project_key(str(project).strip().lower())
    return 0 if any(task_list_project_key(value) == wanted for value in projects) else -1


def _row_expired(row: Any, now: datetime) -> bool:
    keys = row.keys()
    raw = row["expires_at"] if "expires_at" in keys else None
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return expiry <= stamp


def _hit_token_count(hit: dict[str, Any]) -> int:
    """Title, snippet and the #1459 labels (epistemic, author, validity, evidence)."""
    parts = [
        hit.get(key) or "" for key in ("title", "snippet", "epistemic", "author", "valid_from")
    ]
    parts.extend(hit.get("evidence") or [])
    return max(len(" ".join(str(part) for part in parts).split()), 1)


def _parse_tags(raw: str | None) -> list[str]:
    try:
        tags = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(tags, list):
        return []
    return [str(tag)[:64] for tag in tags[:12]]


def _parse_evidence(raw: str | None) -> list[str]:
    try:
        refs = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(refs, list):
        return []
    return [redact_sensitive_content(str(ref), max_len=200) for ref in refs[:_EVIDENCE_LIMIT]]


def _load_lifecycle(task_id: int | None, session_id: str | None) -> list[dict[str, Any]]:
    rows = events.query_events(
        channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.*"], limit=50, newest_first=True
    )
    matched = [row for row in rows if _event_relates(row, task_id, session_id)]
    selected = list(reversed(matched[:_LIFECYCLE_LIMIT]))
    return [_summarize_event(row) for row in selected]


def _load_prior_handoff(task_id: int | None, session_id: str | None) -> dict[str, Any] | None:
    rows = events.query_events(
        channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.handoff"], limit=50, newest_first=True
    )
    matched = [row for row in rows if _event_relates(row, task_id, session_id)]
    if not matched:
        return None
    return _summarize_event(matched[0])


def _load_recent_events(task_id: int | None, session_id: str | None) -> list[dict[str, Any]]:
    rows = events.query_events(channels=["agent.*"], limit=80, newest_first=True)
    matched = [row for row in rows if _event_relates(row, task_id, session_id)]
    selected = list(reversed(matched[:_EVENT_LIMIT]))
    return [_summarize_event(row) for row in selected]


def _event_relates(row: dict[str, Any], task_id: int | None, session_id: str | None) -> bool:
    if session_id and row.get("episode_id") == session_id:
        return True
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    if task_id is not None and payload.get("task_id") == task_id:
        return True
    if session_id and payload.get("session_id") == session_id:
        return True
    if task_id is not None:
        channel = row.get("channel") or ""
        ref_id = payload.get("ref_id")
        if channel.startswith("agent.task.") or payload.get("kind") == "task":
            if ref_id is not None and str(ref_id) == str(task_id):
                return True
    return False


def _summarize_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    stripped = {
        str(key): value
        for key, value in payload.items()
        if str(key).lower() not in EXCLUDED_PAYLOAD_KEYS
    }
    return {
        "ulid": row.get("ulid"),
        "channel": row.get("channel"),
        "created_at": row.get("created_at"),
        "originator": row.get("originator"),
        "payload": sanitize_metadata(stripped, max_payload_bytes=512),
    }


def _packet_size(packet: dict[str, Any]) -> int:
    return len(json.dumps(packet, default=str).encode("utf-8"))


def _bound_packet(packet: dict[str, Any]) -> dict[str, Any]:
    encoded_len = _packet_size(packet)
    if encoded_len <= PACKET_BYTE_LIMIT:
        return packet
    chain = (
        (packet.get("why_chain") or {}).get("chain")
        if isinstance(packet.get("why_chain"), dict)
        else None
    )
    # Root-first chain: drop distant ancestors before the goal this task serves.
    while isinstance(chain, list) and len(chain) > 1 and encoded_len > PACKET_BYTE_LIMIT:
        chain.pop(0)
        encoded_len = _packet_size(packet)
    for key in ("recent_events", "lifecycle", "memory", "experience", "delegations", "approvals"):
        values = packet.get(key)
        while isinstance(values, list) and values and encoded_len > PACKET_BYTE_LIMIT:
            values.pop()
            encoded_len = _packet_size(packet)
    if encoded_len > PACKET_BYTE_LIMIT:
        session = packet.get("session")
        if isinstance(session, dict):
            session["summary"] = ""
        worktree = packet.get("worktree")
        if isinstance(worktree, dict):
            worktree["path"] = None
        packet["requested_by"] = None
        task = packet.get("task")
        if isinstance(task, dict) and task.get("notes"):
            task["notes"] = ""
        operating = packet.get("operating_profile")
        if isinstance(operating, dict) and operating.get("instructions"):
            operating["instructions"] = ""
            operating["brief"] = ""
            operating["_truncated"] = "instructions dropped: packet exceeded the payload limit"
        encoded_len = _packet_size(packet)
    if encoded_len > PACKET_BYTE_LIMIT:
        packet["task"] = {
            "id": (packet.get("task") or {}).get("id"),
            "title": (packet.get("task") or {}).get("title"),
            "status": (packet.get("task") or {}).get("status"),
            "_truncated": "session packet exceeded the payload limit",
        }
        packet["session"] = None
        packet["prior_handoff"] = None
        packet["handoff_note"] = None
        encoded_len = _packet_size(packet)
    if encoded_len > PACKET_BYTE_LIMIT:
        return {
            "schema": PACKET_SCHEMA,
            "labels": dict(_PACKET_LABELS),
            "generated_at": packet.get("generated_at"),
            "session_id": packet.get("session_id"),
            "_truncated": "session packet exceeded the payload limit",
        }
    return packet
