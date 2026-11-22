"""Tool to analyze memory notes and propose low-salience, decayed, or superseded candidates for pruning."""

import json
from datetime import UTC, datetime, timedelta

from .. import edition, storage
from .memory import _NOT_DELETED_SQL, _TENANT_SQL, _is_index_id


def memory_prune_candidates(
    salience_threshold: float = 0.3,
    max_age_days: float = 30.0,
    limit: int = 50,
) -> dict:
    """Identify memory notes that are candidates for pruning (superseded, low salience, or decayed & unaccessed).

    Returns a proposal dict with ranked candidate notes and structural metrics.
    Does not mutate or delete any notes — pruning proposals require validate+hold approval.
    """
    now = datetime.now(UTC)
    cutoff_dt = now - timedelta(days=max_age_days)
    cutoff_iso = cutoff_dt.isoformat()

    conn = storage.connect()
    try:
        cursor = conn.execute(
            "SELECT id, title, tags, author, created_at, updated_at, salience, access_count, superseded_by, source_host "
            f"FROM memory WHERE {_TENANT_SQL} "
            # A soft-deleted note (meta:deleted tombstone) is already gone from
            # the caller's view; proposing it for pruning is noise (task #1914).
            f"AND {_NOT_DELETED_SQL} "
            "ORDER BY updated_at DESC",
            # Tenant fence (task #1762): candidates and their counts are the
            # caller's tenant only.
            (edition.tenant_org_id(),),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    candidates = []
    for row in rows:
        note_id = row["id"]
        if _is_index_id(note_id):
            continue

        title = row["title"]
        tags = json.loads(row["tags"]) if row["tags"] else []
        salience = (
            row["salience"] if "salience" in row.keys() and row["salience"] is not None else 0.5
        )
        access_count = (
            row["access_count"]
            if "access_count" in row.keys() and row["access_count"] is not None
            else 0
        )
        superseded_by = row["superseded_by"] if "superseded_by" in row.keys() else None
        updated_at = row["updated_at"]

        reasons = []
        if superseded_by:
            reasons.append("superseded")
        if salience < salience_threshold:
            reasons.append("low_salience")
        if updated_at and updated_at < cutoff_iso and access_count == 0 and salience <= 0.5:
            reasons.append("decayed_unused")

        if reasons:
            candidates.append(
                {
                    "id": note_id,
                    "title": title,
                    "tags": tags,
                    "author": row["author"] if "author" in row.keys() else None,
                    "updated_at": updated_at,
                    "salience": salience,
                    "access_count": access_count,
                    "superseded_by": superseded_by,
                    "reasons": reasons,
                }
            )

    # Sort by number of reasons descending, then salience ascending
    candidates.sort(key=lambda c: (-len(c["reasons"]), c["salience"]))
    truncated_candidates = candidates[:limit]

    return {
        "candidate_count": len(candidates),
        "returned_count": len(truncated_candidates),
        "salience_threshold": salience_threshold,
        "max_age_days": max_age_days,
        "candidates": truncated_candidates,
    }
