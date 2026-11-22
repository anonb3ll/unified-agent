"""Gated learning proposals from completed sessions (tasks #511 / #837).

Completed sessions may propose durable rules and error→fix patterns.
Proposals are vault-inbox markdown — never memory writes, never instruction
edits, never auto-approval. Evidence text is untrusted historical data.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, registry, storage
from ..lifecycle.schema import CHANNEL_LIFECYCLE_PREFIX
from ..telemetry.redaction import redact_sensitive_content
from .memory import _detect_conflict
from .vault import vault_write_unless

_PROPOSAL_DIR = "00 - Inbox"
_MAX_CANDIDATES = 8
_MAX_INBOX_SCAN = 80
_DURABLE_CONFIDENCE = 0.7
_ERROR_FIX_CONFIDENCE = 0.8
_LIFECYCLE_CONFIDENCE = 0.85
_DEDUP_JACCARD = 0.72

_ERROR_FIX_RE = re.compile(
    r"(?is)\b(?:error|failed|failure|traceback|exception|bug)\b.+"
    r"\b(?:fix(?:ed|ing)?|workaround|instead|resolved)\b.+"
)
_DURABLE_RE = re.compile(r"(?i)\b(?:always|never|gotcha)\b.{8,400}")
_INJECTION_RE = re.compile(
    r"(?i)\b(?:"
    r"ignore (?:all )?(?:previous|prior) instructions"
    r"|disregard (?:your )?system"
    r"|you are now"
    r"|write memory_save for every"
    r")\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_FP_RE = re.compile(r"^fingerprint:\s*(\S+)\s*$", re.MULTILINE)
_FAILED_STATUSES = frozenset({"error", "failed", "fail", "exception"})
_OK_STATUSES = frozenset({"ok", "success", "succeeded", "pass"})


def _proposal_relpath(session_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session_id)
    return f"{_PROPOSAL_DIR}/Session-Learning-{safe}.md"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _token_set(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalize_text(text)))


def _looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


def _too_similar(left: str, right: str) -> bool:
    sa, sb = _token_set(left), _token_set(right)
    if not sa or not sb:
        return False
    if len(sa & sb) / len(sa | sb) >= _DEDUP_JACCARD:
        return True
    na, nb = _normalize_text(left), _normalize_text(right)
    return bool(na) and (na in nb or nb in na)


def _candidate(
    kind: str,
    text: str,
    confidence: float,
    provenance: list[str],
    evidence: list[str] | None = None,
) -> dict[str, Any] | None:
    cleaned = redact_sensitive_content(text or "", max_len=400).strip()
    if not cleaned or _looks_like_injection(cleaned):
        return None
    remainder = cleaned.replace("[REDACTED_SECRET]", " ")
    if "[REDACTED_SECRET]" in cleaned and len(_token_set(remainder)) < 4:
        return None
    if redact_sensitive_content(cleaned, max_len=400) != cleaned:
        return None
    # Every candidate is a conclusion drawn from a session, not an observed
    # fact, so it is labelled inferred and carries the ids that support it
    # (task #1459). Session/task refs are added in _collect_candidates.
    return {
        "kind": kind,
        "text": cleaned,
        "confidence": confidence,
        "provenance": list(provenance),
        "epistemic": "inferred",
        "evidence": list(evidence or []),
        "contradicts": [],
    }


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()]


def _extract_from_summary(summary: str) -> list[dict[str, Any]]:
    text = redact_sensitive_content(summary or "", max_len=800).strip()
    if not text or _looks_like_injection(text):
        return []
    if _ERROR_FIX_RE.search(text):
        cand = _candidate("error_fix", text, _ERROR_FIX_CONFIDENCE, ["session.summary"])
        return [cand] if cand else []
    out: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        if _looks_like_injection(sentence) or not _DURABLE_RE.search(sentence):
            continue
        cand = _candidate("durable_rule", sentence, _DURABLE_CONFIDENCE, ["session.summary"])
        if cand:
            out.append(cand)
    return out


def _inner_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _event_tool(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict):
        tool = payload.get("tool")
        if tool:
            return str(tool)
        inner = payload.get("payload")
        if isinstance(inner, dict) and inner.get("tool"):
            return str(inner["tool"])
    return ""


def _payload_failed(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").lower()
    if status in _FAILED_STATUSES:
        return True
    if payload.get("ok") is False or payload.get("success") is False:
        return True
    return bool(payload.get("error") or payload.get("error_message") or payload.get("exception"))


def _payload_succeeded(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").lower()
    if status in _OK_STATUSES:
        return True
    return payload.get("ok") is True or payload.get("success") is True


def _error_text(payload: dict[str, Any]) -> str:
    for key in ("error", "error_message", "exception", "note"):
        value = payload.get(key)
        if value:
            return str(value)
    return str(payload.get("status") or "error")


def _success_text(payload: dict[str, Any]) -> str:
    for key in ("note", "resolution", "fix"):
        value = payload.get(key)
        if value:
            return str(value)
    return str(payload.get("status") or "ok")


def _event_relates(event: dict[str, Any], session_id: str, task_id: int | None) -> bool:
    if event.get("episode_id") == session_id:
        return True
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("session_id") == session_id:
        return True
    if task_id is not None and payload.get("task_id") == task_id:
        return True
    return False


def _extract_from_lifecycle(session_id: str, task_id: int | None) -> list[dict[str, Any]]:
    try:
        rows = events.query_events(
            channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.*"],
            limit=80,
            newest_first=False,
        )
    except Exception:
        return []
    matched = [row for row in rows if _event_relates(row, session_id, task_id)]
    pending: dict[str, tuple[dict[str, Any], str | None]] = {}
    out: list[dict[str, Any]] = []
    for row in matched:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("kind") != "tool_end":
            continue
        tool = _event_tool(row) or "tool"
        inner = _inner_payload(row)
        if _payload_failed(inner):
            pending[tool] = (inner, row.get("ulid"))
            continue
        if tool in pending and _payload_succeeded(inner):
            failed, failed_ulid = pending.pop(tool)
            event_refs = [f"event:{ulid}" for ulid in (failed_ulid, row.get("ulid")) if ulid]
            text = (
                f"{tool} failed with {_error_text(failed)}; "
                f"later succeeded ({_success_text(inner)})."
            )
            cand = _candidate(
                "error_fix",
                text,
                _LIFECYCLE_CONFIDENCE,
                ["lifecycle.tool_end"],
                evidence=event_refs,
            )
            if cand:
                out.append(cand)
            if len(out) >= _MAX_CANDIDATES:
                break
    return out


def _memory_rows_for(text: str) -> list[Any]:
    """Live notes in the caller's tenant only: a proposal never names another
    tenant's note id or owner (task #1459)."""
    tokens = list(_token_set(text))[:8]
    org_id = edition.tenant_org_id()
    conn = storage.connect()
    try:
        if tokens:
            query = " OR ".join(f'"{tok}"' for tok in tokens)
            try:
                return conn.execute(
                    "SELECT m.id, m.title, m.content, m.author FROM memory_fts "
                    "JOIN memory m ON m.rowid = memory_fts.rowid "
                    "WHERE memory_fts MATCH ? AND m.superseded_by IS NULL "
                    "AND m.org_id IS ? "
                    "AND m.id NOT LIKE 'masterindex%' AND m.id NOT LIKE 'tagindex_%' "
                    "LIMIT 20",
                    (query, org_id),
                ).fetchall()
            except Exception:
                pass
        return conn.execute(
            "SELECT id, title, content, author FROM memory WHERE superseded_by IS NULL "
            "AND org_id IS ? "
            "AND id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%' "
            "ORDER BY updated_at DESC LIMIT 50",
            (org_id,),
        ).fetchall()
    finally:
        conn.close()


def _compare_with_memory(text: str) -> tuple[bool, list[dict[str, Any]]]:
    """(duplicates a live note, live notes the candidate contradicts).

    A near-identical note that disagrees with the candidate (task #1459) is a
    possible changed fact, not a duplicate: keep the candidate and link the
    note so a reviewer can supersede it (own note) or raise it with its owner.
    The note itself is never touched here.
    """
    try:
        rows = _memory_rows_for(text)
    except Exception:
        return False, []
    contradicted: list[dict[str, Any]] = []
    for row in rows:
        blob = f"{row['title'] or ''}\n{row['content'] or ''}"
        if not _too_similar(text, blob):
            continue
        if _detect_conflict(text, row["content"] or ""):
            contradicted.append({"id": row["id"], "author": row["author"]})
            continue
        return True, []
    return False, contradicted


def _inbox_bodies(vault: Path, exclude_rel: str) -> list[str]:
    inbox = vault / _PROPOSAL_DIR
    if not inbox.is_dir():
        return []
    skip = Path(exclude_rel).name
    bodies: list[str] = []
    for path in sorted(inbox.glob("Session-Learning-*.md"))[:_MAX_INBOX_SCAN]:
        if path.name == skip:
            continue
        try:
            bodies.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return bodies


def _duplicates_previous_candidate(text: str, bodies: list[str]) -> bool:
    return any(_too_similar(text, body) for body in bodies)


def _dedupe(candidates: list[dict[str, Any]], vault: Path | None, rel: str) -> list[dict[str, Any]]:
    bodies = _inbox_bodies(vault, rel) if vault is not None else []
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        text = cand["text"]
        if any(_too_similar(text, prior["text"]) for prior in kept):
            continue
        duplicate, contradicted = _compare_with_memory(text)
        if duplicate:
            continue
        if contradicted:
            cand = {**cand, "contradicts": contradicted}
        if _duplicates_previous_candidate(text, bodies):
            continue
        kept.append(cand)
        if len(kept) >= _MAX_CANDIDATES:
            break
    return kept


def _session_evidence(session: dict[str, Any]) -> list[str]:
    refs = [f"session:{session.get('id') or ''}"]
    if session.get("task_id") is not None:
        refs.append(f"task:{session['task_id']}")
    return refs


def _collect_candidates(session: dict[str, Any]) -> list[dict[str, Any]]:
    summary = session.get("summary") or ""
    extracted = _extract_from_lifecycle(session.get("id") or "", session.get("task_id"))
    extracted.extend(_extract_from_summary(summary))
    base = _session_evidence(session)
    observed_at = session.get("ended_at") or session.get("started_at")
    for cand in extracted:
        cand["evidence"] = base + [ref for ref in cand["evidence"] if ref not in base]
        cand["observed_at"] = observed_at
    extracted.sort(key=lambda item: (-float(item["confidence"]), item["text"]))
    return extracted


def _proposal_fingerprint(candidates: list[dict[str, Any]]) -> str:
    # Evidence and contradiction links are part of the proposal (task #1459):
    # a new event ref or a changed conflicting note must refresh the file.
    payload = [
        {
            "kind": item["kind"],
            "text": _normalize_text(item["text"]),
            "confidence": round(float(item["confidence"]), 2),
            "evidence": list(item.get("evidence") or []),
            "contradicts": sorted(str(link.get("id")) for link in item.get("contradicts") or []),
        }
        for item in candidates
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _existing_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    match = _FP_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def _session_project(session: dict[str, Any]) -> str | None:
    task_id = session.get("task_id")
    if task_id is None:
        return None
    try:
        conn = storage.connect()
        try:
            row = conn.execute("SELECT project FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    project = row["project"]
    return str(project) if project else None


def _proposal_markdown(session: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    task_id = session.get("task_id")
    task_title = session.get("task_title") or ""
    agent = session.get("agent") or "unknown"
    sid = session.get("id") or ""
    status = session.get("status") or ""
    project = _session_project(session) or "(none)"
    fingerprint = _proposal_fingerprint(candidates)
    blocks = []
    for index, cand in enumerate(candidates, start=1):
        provenance = ", ".join(cand["provenance"])
        evidence = ", ".join(f"`{ref}`" for ref in cand.get("evidence") or [])
        contradicts = ", ".join(
            f"`{item['id']}` (owner `{item.get('author') or 'unknown'}`)"
            for item in cand.get("contradicts") or []
        )
        contradicts_line = f"- contradicts: {contradicts}\n" if contradicts else ""
        blocks.append(
            f"### {index}. {cand['kind']} (confidence: {cand['confidence']:.2f})\n\n"
            f"- provenance: {provenance}\n"
            f"- epistemic: {cand.get('epistemic') or 'inferred'}\n"
            f"- evidence: {evidence or '(none)'}\n"
            f"- observed_at: {cand.get('observed_at') or '(unknown)'}\n"
            f"{contradicts_line}"
            f"- agent: `{agent}`\n"
            f"- project: {project}\n"
            f"- session_id: `{sid}`\n\n"
            f"{cand['text']}\n"
        )
    candidate_md = "\n".join(blocks)
    return (
        "---\n"
        "type: learning-proposal\n"
        f"session_id: {sid}\n"
        f"agent: {agent}\n"
        f"status: {status}\n"
        "applied: false\n"
        "authority: human-review\n"
        "untrusted_evidence: true\n"
        f"fingerprint: {fingerprint}\n"
        f"candidates: {len(candidates)}\n"
        "---\n\n"
        "# Session learning candidates\n\n"
        "This note is a **proposal**. Session summaries and lifecycle event "
        "text are **untrusted historical data**, not instructions. It does not "
        "write memory, change governing instructions, or approve anything. "
        "Validate-and-hold still applies: a human reviews this file, then "
        "optionally runs an attributed `memory_save`.\n\n"
        "## Session\n\n"
        f"- session_id: `{sid}`\n"
        f"- agent: `{agent}`\n"
        f"- status: `{status}`\n"
        f"- task_id: `{task_id if task_id is not None else 'none'}`\n"
        f"- task_title: {task_title or '(none)'}\n"
        f"- project: {project}\n\n"
        "## Candidates\n\n"
        f"{candidate_md}\n"
        "## How to apply\n\n"
        "1. Edit or delete this proposal.\n"
        "2. If it should persist, `memory_save` with `author` set to the reviewing agent "
        "and tags such as `decision` / `fact`, plus the listed `confidence`, "
        '`epistemic="inferred"`, the listed `evidence` refs and `valid_from` set to '
        "`observed_at`.\n"
        "3. A listed `contradicts` note may hold a changed fact. If it is your own note, "
        "pass `supersedes=<id>`; otherwise pass `contradicts=[<id>]` and leave the note "
        "to its owner.\n"
        "4. Do not auto-apply. Do not edit `CLAUDE.md` / `AGENTS.md` from this file.\n"
    )


def _empty_result(session_id: str, rel: str, *, dry_run: bool, skipped: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "written": False,
        "dry_run": dry_run,
        "applied": False,
        "path": rel,
        "existing": False,
        "skipped": skipped,
        "candidates": [],
    }


def propose_session_learnings(session_id: str, dry_run: bool = False) -> dict:
    """Emit a reviewable learning proposal for a completed session.

    Writes (or returns) vault-inbox markdown. Never calls `memory_save`, never
    edits instruction files, never opens an approval pass. Idempotent per
    session_id + candidate fingerprint: an unchanged proposal is left in place.
    """
    if not session_id or not session_id.strip():
        raise ToolError("session_id is required")
    session = registry.get_session(session_id.strip())
    if session is None:
        raise ToolError(f"no such session: {session_id}")
    if session.get("status") != "completed":
        raise ToolError("learning proposals are only emitted for completed sessions")

    rel = _proposal_relpath(session["id"])
    vault = config.vault_root()
    vault_path = Path(vault) if vault is not None else None
    extracted = _collect_candidates(session)
    candidates = _dedupe(extracted, vault_path, rel)
    if not candidates:
        return _empty_result(session["id"], rel, dry_run=dry_run, skipped="low_signal")

    markdown = _proposal_markdown(session, candidates)
    fingerprint = _proposal_fingerprint(candidates)
    if dry_run:
        return {
            "session_id": session["id"],
            "written": False,
            "dry_run": True,
            "applied": False,
            "path": rel,
            "markdown": markdown,
            "fingerprint": fingerprint,
            "candidates": candidates,
        }

    if vault is None:
        raise ToolError("vault is disabled (UA_VAULT_PATH is empty); cannot file a proposal")
    # The "already filed?" fingerprint check runs under the note lock the write
    # takes (#2091). Checked outside it, two passes over one session both saw no
    # proposal and both wrote; now the second queues and finds the first's note.
    written = vault_write_unless(rel, markdown, lambda p: _existing_fingerprint(p) == fingerprint)
    if written is None:
        return {
            "session_id": session["id"],
            "written": False,
            "dry_run": False,
            "applied": False,
            "path": rel,
            "existing": True,
            "fingerprint": fingerprint,
            "candidates": candidates,
        }
    return {
        "session_id": session["id"],
        "written": True,
        "dry_run": False,
        "applied": False,
        "path": written["path"],
        "existing": False,
        "fingerprint": fingerprint,
        "candidates": candidates,
    }


def maybe_propose_completed_session(session_id: str, status: str) -> dict | None:
    """Best-effort hook for session_end. Never raises; never writes memory."""
    if status != "completed":
        return None
    try:
        return propose_session_learnings(session_id, dry_run=False)
    except Exception:
        return None


# --- Proposals pane surface (task #1148) ------------------------------------
# Read + human-gated actions only. Never auto-approve, never rewrite proposal
# content, never ride sleep-pass / cron.

NO_PROPOSALS_SOURCE = "no proposals source"
NO_PROPOSALS_SOURCE_CODE = "no_proposals_source"
_SKILL_CLUSTER_NAME = "Skill-Cluster-Proposals.md"
_PROPOSAL_TYPES = frozenset({"learning-proposal", "skill-cluster-proposal"})
MAX_PROPOSAL_REASON_LEN = 500
_CLUSTER_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+`([^`]+)`\s+\(score\s+[0-9.]+\)\s*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _no_proposals_source() -> dict[str, Any]:
    return {
        "proposals": [],
        "error": NO_PROPOSALS_SOURCE,
        "code": NO_PROPOSALS_SOURCE_CODE,
    }


def _validated_reason(reason: Any) -> str | None:
    """Optional structured reason for a proposal action (task #1157).

    Must be a string when present; stripped and length-capped. An empty or
    whitespace-only reason is treated as absent — the action is still
    accepted and flagged ``reason_missing`` in the response, never blocked.
    """
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ToolError("reason must be a string")
    reason = reason.strip()
    if not reason:
        return None
    if len(reason) > MAX_PROPOSAL_REASON_LEN:
        raise ToolError(f"reason must be at most {MAX_PROPOSAL_REASON_LEN} characters")
    return reason


def _filing_title(item: dict[str, Any]) -> str:
    title = (item.get("title") or "proposal").strip()
    if len(title) > 120:
        title = title[:117] + "…"
    return f"[proposal] {title}"


def _filing_notes(
    item: dict[str, Any], *, agent: str | None = None, reason: str | None = None
) -> str:
    """The task body ``file_proposal_as_task`` writes.

    Also serves as the detail-pane filing preview (agent/reason omitted), so
    what the reviewer inspects and what filing creates can never drift.
    """
    source_date = item.get("source_date") or "(unknown date)"
    path = item.get("path") or "(unknown path)"
    kind = item.get("kind") or "proposal"
    content = (item.get("content") or "").strip()
    notes = (
        f"{content}\n\n"
        f"---\n"
        f"Source: `{path}` ({kind})\n"
        f"source_date: {source_date}\n"
        f"proposal_id: `{item.get('id') or ''}`\n"
        f"Filed from Proposals pane (validate-and-hold; not auto-approved).\n"
    )
    if agent:
        notes += f"filed_by: `{agent}`\n"
    if reason:
        notes += f"reason: {reason}\n"
    return notes


def _filing_preview(item: dict[str, Any]) -> dict[str, Any]:
    """Read-only preview of the task ``file_task`` would create (task #1157)."""
    return {
        "task_title": _filing_title(item),
        "task_notes": _filing_notes(item),
    }


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def _proposal_id(path: str, index: int, title: str) -> str:
    raw = f"{path}#{index}#{title}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _source_date(meta: dict[str, str], path: Path) -> str:
    generated = meta.get("generated") or ""
    if generated:
        return generated
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    except OSError:
        return ""


def _split_markdown_sections(body: str) -> list[tuple[str, str]]:
    """Return (heading, section_body) pairs for every ``##`` heading."""
    lines = body.splitlines(keepends=True)
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "".join(current_lines).strip()))
            current_heading = line[3:].strip()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, "".join(current_lines).strip()))
    return sections


def _skill_cluster_items(
    *,
    path: str,
    meta: dict[str, str],
    body: str,
    source_date: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    sections = _split_markdown_sections(body)
    index = 0
    for heading, section_body in sections:
        if heading.lower().startswith("how to apply"):
            continue
        index += 1
        matched = _CLUSTER_SECTION_RE.match(f"## {heading}")
        if matched is None:
            items.append(
                {
                    "id": _proposal_id(path, index, heading or f"section-{index}"),
                    "path": path,
                    "kind": "skill-cluster-proposal",
                    "title": heading or f"section-{index}",
                    "content": section_body,
                    "source_date": source_date,
                    "error": "malformed proposal section: expected '## N. `name` (score …)'",
                }
            )
            continue
        title = matched.group(2)
        content = f"## {heading}\n\n{section_body}".strip()
        items.append(
            {
                "id": _proposal_id(path, index, title),
                "path": path,
                "kind": "skill-cluster-proposal",
                "title": title,
                "content": content,
                "source_date": source_date,
                "error": None,
            }
        )
    if not items:
        # File present but no ## sections — surface as one whole-file item.
        title = Path(path).stem
        items.append(
            {
                "id": _proposal_id(path, 0, title),
                "path": path,
                "kind": "skill-cluster-proposal",
                "title": title,
                "content": body.strip(),
                "source_date": source_date,
                "error": None,
            }
        )
    return items


def _learning_item(
    *,
    path: str,
    meta: dict[str, str],
    body: str,
    source_date: str,
) -> dict[str, Any]:
    session_id = (meta.get("session_id") or "").strip("`")
    title = session_id or Path(path).stem
    return {
        "id": _proposal_id(path, 0, title),
        "path": path,
        "kind": "learning-proposal",
        "title": title,
        "content": body.strip() or "(empty proposal body)",
        "source_date": source_date,
        "error": None,
    }


def _inbox_proposal_paths(inbox: Path) -> list[Path]:
    """Known proposal artifacts: skill-cluster file + Session-Learning-*.md.

    Also includes any other Inbox markdown whose frontmatter ``type`` is a
    known proposal type (so a misnamed skill-cluster file still surfaces).
    """
    found: dict[str, Path] = {}
    skill = inbox / _SKILL_CLUSTER_NAME
    if skill.is_file():
        found[str(skill)] = skill
    for path in sorted(inbox.glob("Session-Learning-*.md")):
        if path.is_file():
            found[str(path)] = path
    for path in sorted(inbox.glob("*.md")):
        if not path.is_file() or str(path) in found:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        if meta.get("type") in _PROPOSAL_TYPES:
            found[str(path)] = path
    return list(found.values())


def _parse_proposal_file(path: Path, vault: Path) -> list[dict[str, Any]]:
    rel = str(path.relative_to(vault)).replace("\\", "/")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            {
                "id": _proposal_id(rel, 0, path.name),
                "path": rel,
                "kind": "unknown",
                "title": path.name,
                "content": "",
                "source_date": "",
                "error": f"could not read proposal: {exc}",
            }
        ]
    meta, body = _parse_frontmatter(text)
    kind = meta.get("type") or ""
    source_date = _source_date(meta, path)
    if kind == "skill-cluster-proposal":
        return _skill_cluster_items(path=rel, meta=meta, body=body, source_date=source_date)
    if kind == "learning-proposal":
        return [_learning_item(path=rel, meta=meta, body=body, source_date=source_date)]
    # Filename matched but frontmatter missing/wrong — still surface an error.
    return [
        {
            "id": _proposal_id(rel, 0, path.name),
            "path": rel,
            "kind": kind or "unknown",
            "title": path.name,
            "content": body.strip()[:2000],
            "source_date": source_date,
            "error": "malformed proposal: missing or unknown type in frontmatter",
        }
    ]


def list_pending_proposals() -> dict[str, Any]:
    """List pending session-learning / skill-cluster proposals (task #1148).

    Pending means the source Inbox note's vault ingest status is ``new`` or
    ``changed``. Read-only: never marks ingest, never writes proposal content,
    never files tasks or saves memory.
    """
    from .vault import vault_ingest_status

    vault = config.vault_root()
    if vault is None:
        return _no_proposals_source()
    root = Path(vault)
    inbox = root / _PROPOSAL_DIR
    if not inbox.is_dir():
        return _no_proposals_source()

    proposals: list[dict[str, Any]] = []
    for path in _inbox_proposal_paths(inbox):
        rel = str(path.relative_to(root)).replace("\\", "/")
        try:
            status = vault_ingest_status(rel, mark=False).get("status")
        except ToolError:
            status = "new"
        if status in ("unchanged", "empty"):
            continue
        for item in _parse_proposal_file(path, root):
            # Detail payload (task #1157): source reference is item["path"];
            # expose the capture's ingest state plus a preview of the task
            # file_task would create, derived read-only from the same item.
            item["ingest_status"] = status
            item["filing_preview"] = None if item.get("error") else _filing_preview(item)
            proposals.append(item)

    return {"proposals": proposals, "error": None, "code": None}


def _find_proposal(proposal_id: str) -> dict[str, Any]:
    """Resolve one proposal id against the current pending set (incl. errors)."""
    listed = list_pending_proposals()
    if listed.get("code") == NO_PROPOSALS_SOURCE_CODE:
        raise ToolError(NO_PROPOSALS_SOURCE)
    for item in listed.get("proposals") or []:
        if item.get("id") == proposal_id:
            return item
    # Also search dismissed/unchanged files so a stale UI id fails clearly.
    vault = config.vault_root()
    if vault is not None:
        root = Path(vault)
        inbox = root / _PROPOSAL_DIR
        if inbox.is_dir():
            for path in _inbox_proposal_paths(inbox):
                for item in _parse_proposal_file(path, root):
                    if item.get("id") == proposal_id:
                        return item
    raise ToolError(f"unknown proposal id: {proposal_id}")


def file_proposal_as_task(
    proposal_id: str, *, agent: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Create a hub task from a proposal (human action only — never auto-called).

    ``reason`` is an optional structured reviewer note (task #1157), stored
    with the actor attribution in the task notes.
    """
    from .tasks import task_add

    reason = _validated_reason(reason)
    item = _find_proposal(proposal_id)
    if item.get("error"):
        raise ToolError(f"cannot file malformed proposal: {item['error']}")
    path = item.get("path") or "(unknown path)"
    notes = _filing_notes(item, agent=agent, reason=reason)
    # The body is vault-inbox text (proposals carry `untrusted_evidence: true`),
    # so the task starts quarantined; a human clears it (task #1911).
    created = task_add(title=_filing_title(item), notes=notes, priority="med", untrusted=True)
    return {
        "applied": True,
        "action": "file_task",
        "task_id": created["id"],
        "proposal_id": proposal_id,
        "path": path,
        "reason": reason,
        "reason_missing": reason is None,
    }


def dismiss_proposal(proposal_id: str, *, agent: str, reason: str | None = None) -> dict[str, Any]:
    """Mark the proposal source ingested so it leaves the pending list.

    Bounded mutation: ``vault_ingest_status(mark=True)`` only — never rewrites
    proposal content. ``reason`` (task #1157) is stored in the ingest ledger
    alongside the actor attribution.
    """
    from .vault import vault_ingest_status

    reason = _validated_reason(reason)
    item = _find_proposal(proposal_id)
    path = item.get("path")
    if not path:
        raise ToolError("proposal has no source path")
    agent = config.verify_write_identity(agent)
    if not agent or not str(agent).strip():
        raise ToolError("agent label is required to dismiss a proposal")
    marked = vault_ingest_status(path, mark=True, agent=str(agent).strip(), reason=reason)
    return {
        "applied": True,
        "action": "dismiss",
        "proposal_id": proposal_id,
        "path": path,
        "status": marked.get("status", "marked"),
        "reason": reason,
        "reason_missing": reason is None,
    }
