"""Self-curating recall loop for shared memory (TeamAI emulation F2, task #915).

Three pieces, mirroring teamai-cli ``src/recall.ts`` / ``votes.ts`` /
``recall-quality.ts``:

  1. Votes — every ``memory_search`` hit increments a per-note
     ``recalled_count``; an explicit "helpful" ``memory_feedback`` increments
     ``upvoted_count``. Only upvotes feed the search ranking (task #1913):
     ``recalled_count`` counts what search already returned, so ranking on it
     kept popular notes on top whatever the query. It remains a statistic for
     the maintenance pass (stale = never recalled).
  2. Recall-quality log — one row per search (result count + top score) so a
     maintenance pass can see where the knowledge base is thin.
  3. Promote path — turn a high-confidence learning into a ``Wiki/`` page with
     attribution and a link from a machine-managed block in ``Wiki/Index.md``.

Boundaries (task #915):
  * Promotion writes to ``Wiki/`` ONLY — never a curated PARA folder, never a
    locked note. It never happens without an explicit ``memory_promote`` call.
  * ``recall_maintenance`` prunes nothing. It reports stale / low-confidence
    candidates; deletion stays a per-id ``memory_delete`` after human review.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, storage
from .telemetry.redaction import contains_session_credential

_VOTE_WEIGHT_ENV = "UA_MEMORY_WEIGHT_VOTE"
_VOTE_WEIGHT_DEFAULT = 0.35

_PROMOTE_DIR = "Wiki/Learnings"
_INDEX_REL = "Wiki/Index.md"
_INDEX_SECTION = "Promoted learnings"
_INDEX_START = "<!-- promoted-learnings:start -->"
_INDEX_END = "<!-- promoted-learnings:end -->"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_QUALITY_LOG_CAP = 5000


# ─── auto-vote state ──────────────────────────────────────────────────────────


def record_recall_hits(
    note_ids: list[str], query: str, top_score: float | None, *, conn: Any = None
) -> None:
    """Bump ``recalled_count`` for each returned note and log the search outcome.

    Best-effort: a bookkeeping layer on ``memory_search``, it never raises and
    never blocks the search result. The atomic INSERT SELECT rechecks the note
    in the caller's tenant, so a delayed hit cannot recreate votes after a
    committed hard delete. Query-quality logging still describes the results
    that the search returned.

    ``conn`` (task #2444) is the caller's write connection: the writes go
    inside a savepoint on it, and the caller commits, so a search spends one
    commit on its side writes. A failure rolls back to the savepoint, leaving
    the caller's own writes. Without it, this opens and commits its own.

    The helper never commits a connection it was handed (task #2487). On an
    idle connection a savepoint would be the outermost one, and releasing it
    commits, so the helper opens a transaction first and leaves it open for
    the caller to commit or roll back. If the writes fail there, it rolls that
    transaction back, which held nothing else.
    """
    if conn is not None:
        opened = False
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN")
                opened = True
            conn.execute("SAVEPOINT recall_hits")
            try:
                _write_recall_hits(conn, note_ids, query, top_score)
                conn.execute("RELEASE recall_hits")
            except Exception:
                conn.execute("ROLLBACK TO recall_hits")
                conn.execute("RELEASE recall_hits")
                raise
        except Exception:
            if opened:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
        return
    try:
        own = storage.connect()
        try:
            _write_recall_hits(own, note_ids, query, top_score)
            own.commit()
        finally:
            own.close()
    except Exception:
        pass


def _write_recall_hits(conn: Any, note_ids: list[str], query: str, top_score) -> None:
    ts = storage.now()
    for note_id in note_ids:
        conn.execute(
            "INSERT INTO memory_recall_votes (note_id, recalled_count, last_recalled_at) "
            "SELECT id, 1, ? FROM memory WHERE id=? AND org_id IS ? "
            "ON CONFLICT(note_id) DO UPDATE SET "
            "recalled_count = recalled_count + 1, last_recalled_at = excluded.last_recalled_at",
            (ts, note_id, edition.tenant_org_id()),
        )
    conn.execute(
        "INSERT INTO recall_quality_log (ts, query, result_count, top_score, hit) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            ts,
            (query or "")[:200],
            len(note_ids),
            float(top_score) if top_score is not None else None,
            1 if note_ids else 0,
        ),
    )
    _trim_quality_log(conn)


def _trim_quality_log(conn: Any) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM recall_quality_log").fetchone()
    if row and row["n"] > _QUALITY_LOG_CAP:
        conn.execute(
            "DELETE FROM recall_quality_log WHERE id IN ("
            "SELECT id FROM recall_quality_log ORDER BY id ASC LIMIT ?)",
            (row["n"] - _QUALITY_LOG_CAP,),
        )


def record_upvote(note_id: str) -> None:
    """Increment ``upvoted_count`` for a still-existing note in the caller's tenant.

    The note check and upsert share one write statement: feedback collected
    before a hard delete must not recreate an orphan vote after it commits.
    """
    try:
        ts = storage.now()
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO memory_recall_votes (note_id, upvoted_count, last_recalled_at) "
                "SELECT id, 1, ? FROM memory WHERE id=? AND org_id IS ? "
                "ON CONFLICT(note_id) DO UPDATE SET upvoted_count = upvoted_count + 1",
                (ts, note_id, edition.tenant_org_id()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def vote_rows(conn: Any) -> dict[str, dict[str, int]]:
    """``{note_id: {recalled, upvoted}}`` for the whole vote table (search hot path)."""
    try:
        rows = conn.execute(
            "SELECT note_id, recalled_count, upvoted_count FROM memory_recall_votes"
        ).fetchall()
    except Exception:
        return {}
    return {
        r["note_id"]: {"recalled": r["recalled_count"], "upvoted": r["upvoted_count"]} for r in rows
    }


def vote_weight() -> float:
    try:
        value = float(os.environ.get(_VOTE_WEIGHT_ENV, str(_VOTE_WEIGHT_DEFAULT)))
    except ValueError:
        return _VOTE_WEIGHT_DEFAULT
    return value if math.isfinite(value) and value >= 0 else _VOTE_WEIGHT_DEFAULT


def vote_term(recalled: int, upvoted: int) -> float:
    """Bounded [0, 1) ranking contribution. An upvote counts ~3x a bare recall."""
    if recalled <= 0 and upvoted <= 0:
        return 0.0
    return math.tanh(0.5 * math.log1p(max(0, recalled)) + 1.5 * math.log1p(max(0, upvoted)))


def get_votes(note_id: str) -> dict[str, int]:
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT recalled_count, upvoted_count FROM memory_recall_votes WHERE note_id=?",
                (note_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {"recalled": 0, "upvoted": 0}
    if row is None:
        return {"recalled": 0, "upvoted": 0}
    return {"recalled": row["recalled_count"], "upvoted": row["upvoted_count"]}


# ─── promote path ─────────────────────────────────────────────────────────────


def _slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    slug = slug[:64].strip("-")
    return slug or fallback


def _promote_min_confidence() -> float:
    try:
        value = float(os.environ.get("UA_PROMOTE_MIN_CONFIDENCE", "0.7"))
    except ValueError:
        return 0.7
    return value if 0.0 <= value <= 1.0 else 0.7


def _index_line(slug: str, title: str, summary: str) -> str:
    label = title.strip() or slug
    tail = f" — {summary.strip()}" if summary.strip() else ""
    return f"- [[{_PROMOTE_DIR}/{slug}|{label}]]{tail}"


def _rewrite_index_block(index_text: str, new_line: str) -> str:
    """Insert ``new_line`` into the managed promoted-learnings block, sorted + deduped.

    Creates the block (and section heading) at end of file when the markers are
    absent. The steward's curated content outside the markers is never touched.
    """
    if _INDEX_START in index_text and _INDEX_END in index_text:
        pre, rest = index_text.split(_INDEX_START, 1)
        block, post = rest.split(_INDEX_END, 1)
        lines = [ln for ln in block.splitlines() if ln.strip().startswith("- [[")]
    else:
        pre = index_text.rstrip() + f"\n\n## {_INDEX_SECTION}\n\n"
        lines = []
        post = "\n"

    key = new_line.split("]]", 1)[0]
    lines = [ln for ln in lines if ln.split("]]", 1)[0] != key]
    lines.append(new_line)
    lines.sort(key=str.lower)
    block_body = "\n".join(lines)
    return f"{pre}{_INDEX_START}\n{block_body}\n{_INDEX_END}{post}"


def memory_promote(
    id: str,
    slug: str | None = None,
    summary: str | None = None,
    agent: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Promote a high-confidence memory note to a ``Wiki/`` learning page.

    Writes ``Wiki/Learnings/<slug>.md`` with attribution frontmatter and adds a
    link to the machine-managed ``Promoted learnings`` block in ``Wiki/Index.md``.
    Refuses a note whose ``confidence`` is below ``UA_PROMOTE_MIN_CONFIDENCE``
    (default 0.7). ``dry_run=True`` returns the page markdown and the planned
    index line without writing. Never touches PARA folders or locked notes.
    """
    from .tools.memory import _fetch_note

    if agent is not None and contains_session_credential(str(agent)):
        # Refused before the identity check, so the value is never stamped or
        # echoed (task #1893).
        raise ToolError("agent must not carry a hub session credential (uas_...)")
    conn = storage.connect()
    try:
        # Another tenant's note is "no memory note" (task #1762).
        row = _fetch_note(conn, id)
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no memory note with id {id!r}")

    keys = row.keys()
    confidence = (
        row["confidence"] if "confidence" in keys and row["confidence"] is not None else None
    )
    floor = _promote_min_confidence()
    if confidence is None or float(confidence) < floor:
        raise ToolError(
            f"note {id!r} has confidence {confidence!r}; promotion needs >= {floor:.2f}. "
            "Raise the note's confidence with memory_save, or promote a stronger note."
        )

    title = (row["title"] or "").strip() or f"Learning {id[:8]}"
    author = row["author"] if "author" in keys else None
    # verify_write_identity returns None where a claimed `agent` cannot be
    # bound to a verified caller (HTTP, UA_REQUIRE_CALLER_IDENTITY=1). The raw
    # claim is then never stamped (task #1899): the promoter is "unknown".
    promoter = config.verify_write_identity(agent) or "unknown"
    the_slug = _slugify(slug or title, fallback=_slugify(id, fallback="learning"))
    one_line = (summary or "").strip() or _first_line(row["content"])
    rel = f"{_PROMOTE_DIR}/{the_slug}.md"
    now_iso = datetime.now(UTC).date().isoformat()

    page = (
        "---\n"
        "type: promoted-learning\n"
        f"source_memory_id: {id}\n"
        f"promoted_by: {promoter}\n"
        f"promoted_at: {now_iso}\n"
        f"original_author: {author or 'unknown'}\n"
        f"confidence: {float(confidence):.2f}\n"
        "tags: [wiki, learning, promoted]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{(row['content'] or '').strip()}\n\n"
        "---\n\n"
        f"*Promoted from memory note `{id}` by {promoter} on {now_iso}. "
        f"Original author: {author or 'unknown'}.*\n"
    )
    index_line = _index_line(the_slug, title, one_line)

    if dry_run:
        return {
            "promoted": False,
            "dry_run": True,
            "path": rel,
            "wiki_link": f"{_PROMOTE_DIR}/{the_slug}",
            "index_line": index_line,
            "markdown": page,
            "source_id": id,
        }

    from .tools.vault import vault_write_merged

    # The page is regenerated whole, so a concurrent edit must surface as a
    # conflict. Only the index actually merges current text and can retry.
    written = vault_write_merged(rel, lambda _current: page, retries=0)

    index_updated = False
    if config.vault_root() is not None:

        def rebuild_index(current: str | None) -> str | None:
            current = current or ""
            new_index = _rewrite_index_block(current, index_line)
            return new_index if new_index != current else None

        index_updated = vault_write_merged(_INDEX_REL, rebuild_index, force=True) is not None

    return {
        "promoted": True,
        "dry_run": False,
        "path": written["path"],
        "wiki_link": f"{_PROMOTE_DIR}/{the_slug}",
        "index_line": index_line,
        "index_updated": index_updated,
        "source_id": id,
        "promoted_by": promoter,
    }


def _first_line(content: str | None) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return ""


# ─── maintenance pass (report-only) ───────────────────────────────────────────


def recall_maintenance(
    dry_run: bool = True,
    stale_days: float = 120.0,
    confidence_floor: float = 0.35,
    limit: int = 100,
) -> dict:
    """Flag stale and low-confidence memory notes. Reports only — never deletes.

    ``dry_run`` must stay True: this pass has no mutating mode. Deletion is a
    deliberate per-note ``memory_delete`` after a human reviews this report.
    """
    if not dry_run:
        raise ToolError(
            "recall_maintenance never deletes — it is a report. Review the "
            "candidates, then remove specific notes with memory_delete(id=...)."
        )

    from .tools.memory import _NOT_DELETED_SQL, _TENANT_SQL, _is_index_id

    cutoff_iso = _iso_days_ago(stale_days)

    conn = storage.connect()
    try:
        # Tenant fence (task #1762): the report lists the caller's tenant only.
        rows = conn.execute(
            "SELECT id, title, tags, author, updated_at, salience, confidence, access_count, "
            f"superseded_by FROM memory WHERE superseded_by IS NULL AND {_TENANT_SQL} "
            # Soft-deleted tombstones are not maintenance candidates (task #1914).
            f"AND {_NOT_DELETED_SQL} "
            "ORDER BY updated_at ASC",
            (edition.tenant_org_id(),),
        ).fetchall()
        votes = vote_rows(conn)
    finally:
        conn.close()

    stale: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    for row in rows:
        note_id = row["id"]
        if _is_index_id(note_id):
            continue
        recalled = votes.get(note_id, {}).get("recalled", 0)
        salience = row["salience"] if row["salience"] is not None else 0.5
        confidence = row["confidence"]
        updated_at = row["updated_at"] or ""

        if updated_at and updated_at < cutoff_iso and recalled == 0 and salience <= 0.5:
            stale.append(_candidate(row, recalled, "stale_unrecalled"))
        if confidence is not None and float(confidence) < confidence_floor:
            low_confidence.append(_candidate(row, recalled, "low_confidence"))

    return {
        "dry_run": True,
        "deletes_nothing": True,
        "stale_days": stale_days,
        "confidence_floor": confidence_floor,
        "stale_count": len(stale),
        "low_confidence_count": len(low_confidence),
        "stale": stale[:limit],
        "low_confidence": low_confidence[:limit],
        "next_step": "review, then memory_delete(id=...) per note you want gone",
    }


def _candidate(row: Any, recalled: int, reason: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "author": row["author"] if "author" in row.keys() else None,
        "updated_at": row["updated_at"],
        "salience": row["salience"],
        "confidence": row["confidence"],
        "recalled_count": recalled,
        "reason": reason,
    }


def _iso_days_ago(days: float) -> str:
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() - days * 86400.0, UTC).isoformat()
