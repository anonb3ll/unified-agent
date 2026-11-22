"""Bounded KNN over the sqlite-vec mirrors (task #2214, audit M5).

sqlite-vec applies a joined table's predicates (tenant, superseded, deleted,
index, model) after its KNN cut, so a fixed small ``k`` drops live rows whose
slots went to filtered ones. The old answer was ``k = COUNT(*)``, but vec0
refuses ``k`` above :data:`VEC0_MAX_K`: on a store past that size the query
raised, and every search silently ran the per-row UDF scan instead.

:func:`bounded_knn` starts small and doubles ``k`` until the rows already seen
prove that no unseen row could qualify. Rows arrive nearest first, so every
unseen row is at most as similar as the last raw row. The cut is proven when

- the mirror is exhausted (fewer than ``k`` raw rows), or
- the last raw row is below the caller's ``min_similarity`` floor, or
- ``need`` rows that survive the caller's filters are already in hand and the
  last raw row is strictly less similar than the ``need``-th of them (strict,
  so a tie at the boundary is resolved by reading further).

The filters run after each cut, so the result equals the full scan's for every
row the caller can use. When :data:`VEC0_MAX_K` is reached without a proof the
function returns ``None`` and the caller runs its full scan, as before.

Each vec0 query is itself a flat scan of the whole mirror, so a cut that
keeps growing repeats that scan. The budget (review of #2214) gives up early
and returns ``None`` instead:

- at most :data:`KNN_MAX_ROUNDS` native queries per call;
- with ``need``, a cut in which no row qualifies stops after that one query,
  since its nearest rows are all filtered out, and the full scan, which applies
  the filters before scoring, is the cheaper way to read further;
- with ``need``, the next ``k`` is sized from the observed yield (rows that
  qualify per row read), not just doubled. A projection past
  :data:`VEC0_MAX_K` stops at once.

So each call runs at most :data:`KNN_MAX_ROUNDS` (3) native scans before the
caller's full scan, and only one when a ``need`` cut finds no eligible row. A
search may make two calls, one per mirror (``memory_vec``, then
``memory_chunk_vec`` when chunking is on), and each has its own budget.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from typing import Any

# sqlite-vec's compiled limit on a KNN query's k (0.1.x: "k value in knn query
# too large ... the limit is 4096").
VEC0_MAX_K = 4096
# First cut.
KNN_START_K = 64
# Native queries per call before the caller's full scan takes over.
KNN_MAX_ROUNDS = 3
# Headroom on a yield projection, so one more cut usually suffices.
_PROJECTION_SLACK = 1.25

Resolve = Callable[[list[tuple[int, float]]], list[dict[str, Any]]]


def knn_raw(conn: sqlite3.Connection, table: str, emb_bytes: bytes, k: int):
    """``(rowid, similarity)`` of the ``k`` nearest vectors, nearest first.

    ``table`` is one of the module's own vec0 mirror names, never caller text.
    """
    rows = conn.execute(
        f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (emb_bytes, k),
    ).fetchall()
    return [(row[0], 1.0 - row[1]) for row in rows]


def bounded_knn(
    conn: sqlite3.Connection,
    table: str,
    emb_bytes: bytes,
    *,
    resolve: Resolve,
    min_similarity: float | None = None,
    need: int | None = None,
    start_k: int | None = None,
) -> list[dict[str, Any]] | None:
    """Resolved rows for the smallest proven cut, or ``None`` to fall back.

    ``resolve`` maps the raw ``(rowid, similarity)`` cut to the caller's rows,
    applying every filter; each returned dict carries ``similarity``.
    Raises ``sqlite3.OperationalError`` when the mirror is unusable.
    """
    k = max(1, min(start_k or KNN_START_K, VEC0_MAX_K))
    rounds = 0
    while True:
        rounds += 1
        raw = knn_raw(conn, table, emb_bytes, k)
        kept = resolve(raw) if raw else []
        if len(raw) < k:
            return kept
        last = raw[-1][1]
        if min_similarity is not None and last < min_similarity:
            return kept
        next_k = k * 2
        if need is not None and need > 0:
            sims = sorted(
                (
                    row["similarity"]
                    for row in kept
                    if min_similarity is None or row["similarity"] >= min_similarity
                ),
                reverse=True,
            )
            if len(sims) >= need and last < sims[need - 1]:
                return kept
            if not sims:
                return None
            projected = math.ceil(need * len(raw) / len(sims) * _PROJECTION_SLACK)
            if projected > VEC0_MAX_K:
                return None
            next_k = max(next_k, projected)
        if k >= VEC0_MAX_K or rounds >= KNN_MAX_ROUNDS:
            return None
        k = min(next_k, VEC0_MAX_K)
