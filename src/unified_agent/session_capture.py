"""Passive session-end capture (task #813).

The one capability the retired claude-mem plugin had that hub UA memory lacked:
at session end, summarize the transcript and store it as a memory note. The
summary comes from ``llm_client``'s default chain — the operator's Claude
subscription via the headless CLI, then OpenRouter (task #2025) — so no
per-call API spend while the subscription leg answers.

Storage, hybrid search (FTS5 + fastembed), salience and TTL are all already in
UA memory; this module only does: load transcript tail -> hash -> summarize ->
``memory_save``. Dedup rides ``memory_save``'s own idempotency key, so replaying
the same SessionEnd hook is a no-op.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, llm_client
from .tools import memory as memory_tool

# Summarizer output contract. Only ``summary`` is required: a leg that omits the
# lists still passes and we default them to []. ``llm_client._response_matches_schema``
# type-checks a list that is present, items included (PR #1213 review).
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}

_SYSTEM = (
    "You compress a coding-agent session transcript into a durable memory note. "
    "Be terse and factual. Output JSON only: "
    "{summary: 2-4 sentence plain-text recap of what the session did and where it "
    "ended; facts: array of stable technical facts learned (paths, gotchas, "
    "config); decisions: array of choices made and why; files: array of "
    "repo-relative paths created or modified}. Empty arrays are fine. Do not "
    "invent detail that is not in the transcript."
)

_HASH_PREFIX = "session-sha256:"


def _render_transcript(raw: str) -> str:
    """Flatten a Claude Code JSONL transcript to ``role: text`` lines.

    Keeps user/assistant text and a compact ``[tool: name]`` marker; drops
    thinking, tool_result payloads, and the non-message bookkeeping lines.
    """
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        rtype = row.get("type") or row.get("role")
        if rtype not in ("user", "assistant"):
            continue

        raw_msg = row.get("message")
        if isinstance(raw_msg, dict):
            msg = raw_msg
            role = msg.get("role") or rtype
            content = msg.get("content")
        elif isinstance(raw_msg, str):
            role = rtype
            content = raw_msg
        else:
            role = rtype
            content = row.get("content")

        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(str(block.get("text", "")))
                    elif btype == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
        text = " ".join(p.strip() for p in parts if p and p.strip())
        if text:
            out.append(f"{role}: {text}")
    return "\n".join(out)


def load_transcript_tail(transcript_path: str | Path, max_chars: int | None = None) -> str:
    """Rendered transcript, trimmed to the last ``max_chars`` characters."""
    if max_chars is None:
        max_chars = config.session_capture_tail_chars()
    raw = Path(transcript_path).expanduser().read_text(encoding="utf-8", errors="replace")
    rendered = _render_transcript(raw)
    if len(rendered) > max_chars:
        rendered = rendered[-max_chars:]
    return rendered


def segment_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize(transcript_text: str) -> dict[str, Any]:
    """Summarize via ``llm_client.complete``; raises ``LLMConnectionError`` loud.

    Returns ``{summary, facts, decisions, files, route}``. ``route`` proves which
    provider leg ran (claude_cli = subscription, no per-call charge).
    """
    # subscription_pool opt-in (task #1252): transcript summarization is a
    # named zero-spend background call site — still gated on
    # UA_SUBSCRIPTION_POOL_ENABLED.
    result = llm_client.complete(
        transcript_text,
        system=_SYSTEM,
        response_schema=SUMMARY_SCHEMA,
        response_mime_type="application/json",
        subscription_pool=True,
    )
    try:
        data = json.loads(result["text"])
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _strlist(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    return {
        "summary": str(data.get("summary", "")).strip() or "(no summary produced)",
        "facts": _strlist(data.get("facts")),
        "decisions": _strlist(data.get("decisions")),
        "files": _strlist(data.get("files")),
        "route": result.get("route", []),
        "provider": result.get("provider"),
    }


def _note_body(summary: dict[str, Any], project: str, cwd: str, seg_hash: str) -> str:
    lines = [summary["summary"], ""]
    if summary["decisions"]:
        lines.append("**Decisions:**")
        lines += [f"- {d}" for d in summary["decisions"]]
        lines.append("")
    if summary["facts"]:
        lines.append("**Facts:**")
        lines += [f"- {f}" for f in summary["facts"]]
        lines.append("")
    if summary["files"]:
        lines.append("**Files touched:** " + ", ".join(summary["files"]))
        lines.append("")
    lines.append(f"_project: {project} · cwd: {cwd}_")
    lines.append(f"{_HASH_PREFIX}{seg_hash}")
    return "\n".join(lines)


def capture_session(
    transcript_path: str | Path,
    cwd: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Load tail -> hash -> summarize -> store. Idempotent on the segment hash.

    Returns ``{stored, id, created, hash, route, provider, chars}``. ``stored`` is
    False only when the transcript rendered empty. Raises ``LLMConnectionError``
    if every provider leg failed — the caller surfaces that, does not swallow it.
    """
    project = (project or Path(cwd).name or "unknown").strip() or "unknown"
    tail = load_transcript_tail(transcript_path)
    if not tail.strip():
        return {"stored": False, "reason": "empty transcript", "chars": 0}

    seg_hash = segment_hash(tail)
    summary = summarize(tail)

    body = _note_body(summary, project, cwd, seg_hash)
    now = datetime.now(UTC)
    expires_at = (now + timedelta(days=config.session_capture_retention_days())).isoformat()
    title = f"Session {now:%Y-%m-%d %H:%M} · {project}"

    res = memory_tool.memory_save(
        content=body,
        title=title,
        tags=["session-capture", f"project:{project}"],
        salience=0.4,
        expires_at=expires_at,
        idempotency_key=f"session-capture:{seg_hash}",
    )
    return {
        "stored": True,
        "id": res["id"],
        "created": res.get("created", False),
        "hash": seg_hash,
        "route": summary["route"],
        "provider": summary["provider"],
        "chars": len(tail),
    }
