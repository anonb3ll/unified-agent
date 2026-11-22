#!/usr/bin/env python3
"""Benchmark memory_search recall/latency at ~250K/~500K synthetic notes
(task #967 — HNSW threshold decision).

Answers the question posed by the research capture's step 2
(``00 - Inbox/Agent-Memory-Scale-Research-2026-09-09.md``, synthesized at
``Wiki/Agent-Memory-at-Scale.md``): "benchmark recall/latency at ~250K/~500K
notes to decide when a real vector index is needed."

State isolation: UA_HOME/UA_WORKSPACE/UA_VAULT_PATH are pointed at a fresh
temp directory for the run (never the real memory.db). The corpus is grown
cumulatively (250K, then +250K more to reach 500K) and inserted through the
production `memory` table schema via the *exact* INSERT statement
`memory.memory_save` uses on its create path — not a fake/simplified schema —
but bypassing the Python-level `memory_save()` call itself, whose per-note
O(existing-notes) similarity dedup check would make populating a quarter
million notes an O(n^2) operation. Embeddings are then produced by
`embed_missing_notes`, which is the exact same sequence of production calls
(`embeddings.generate_embedding`, `storage.upsert_memory_vec`) that
`storage._backfill_embeddings` uses, just batched/timed for progress
visibility — this is the same "background embedding catch-up" mechanism the
real server uses for notes that predate an embedding, not a stand-in.

At each scale checkpoint this measures:
  1. memory_search query latency (p50/p95/p99) over a sample of realistic
     queries, once the corpus is at steady-state (100% embedded).
  2. Recall: (a) whether the semantic-scoring engine `memory_search` actually
     uses (`_semantic_similarity_rows` — sqlite-vec vec0 KNN or the
     cosine_similarity() UDF fallback) matches an independently computed
     brute-force cosine-similarity ground truth over the *entire* embedded
     corpus, exactly — this is the load-bearing question for the HNSW
     decision, since a genuine ANN index would trade this exactness away;
     (b) recall@k of memory_search's final *blended* ranking against a
     pure-semantic top-k ground truth, reported for context (expected to
     differ by design — the hybrid ranking also weighs FTS/recency/
     salience/access/feedback/entity/vote terms, not because of any
     approximation introduced by scale).
  3. Background-embedding catch-up time and throughput.
  4. DB file / table footprint (via sqlite's `dbstat`).

Usage:
    uv run python scripts/bench_memory_search_scale.py \
        --scales 250000,500000 --queries 50 --out results.json

Runtime: embedding ~500K synthetic notes on a 4-core CPU host at fastembed's
observed ~160-180 notes/sec runs to roughly 45-55 minutes; plan accordingly
(run in the background, tail --out for progress).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

import numpy as np  # noqa: E402

# --------------------------------------------------------------------------
# Synthetic corpus generation
# --------------------------------------------------------------------------

# A fixed topic vocabulary loosely modeled on this repo's own memory.db
# corpus flavor (task/deploy/infra/agent-coordination notes), so embeddings
# cluster meaningfully instead of being uniform random noise — a corpus where
# nothing is ever similar to anything else would make recall trivially
# perfect and uninformative.
TOPICS = [
    "task queue scheduling and claim arbitration",
    "deployment pipeline rollback procedure",
    "vault sync debounce and conflict resolution",
    "embedding backfill and sqlite-vec mirror drift",
    "nightly consolidation sleep pass clustering",
    "agent coordination worktree isolation",
    "backup rotation and offsite gcs copy",
    "sqlite wal concurrency and busy timeout",
    "hnsw index threshold decision",
    "recall benchmark methodology notes",
    "memory salience decay and recency half life",
    "zulip bridge cursor state tracking",
    "approval queue elicitation nonce handling",
    "dispatch run lease and cancellation",
    "graph canvas planning board scene",
    "agent registry liveness heartbeat",
    "executor evidence attempts outcome",
    "delegation callback notification handling",
    "workspace symbol indexer tree sitter",
    "web search cache provider fallback",
    "obsidian wiki custodian orphan sweep",
    "task dependency graph cycle detection",
    "autonomous cycle rework ceiling budget",
    "slack chat claim continuity state",
    "horizon connector governance revocation",
    "perf sample rate sampling exclusion",
    "anomaly notification cooldown dedupe",
    "host report doctor snapshot aggregation",
    "idempotency key retry safe replay",
    "plan step checkpoint self correction",
]

FILLER = (
    "the quick review found that recurring pattern across several hosts and "
    "agents while the maintainer traced root cause back to a configuration "
    "drift between the desktop checkout and the shared hub after a routine "
    "restart cycle triggered by the nightly cron job which then required a "
    "follow up patch to restore expected steady state behavior for every "
    "downstream consumer of the affected table or index during business "
    "hours without further operator intervention or a manual replay step"
).split()

_INDEX_ID_GUARD = ("masterindex00",)


def synthetic_note(rng: random.Random, index: int) -> tuple[str, str, list[str]]:
    """Deterministic (given `rng`'s state) title/content/tags for one note.

    Content length follows a lognormal distribution (median ~230 chars, long
    tail to ~4000) modeling the mix of short factual notes and long research
    captures actually seen in this repo's memory.db, clamped to [40, 4000].
    Each note anchors on one of ~30 fixed topics so embeddings cluster
    meaningfully rather than scattering uniformly at random.
    """
    topic = rng.choice(TOPICS)
    target_len = int(rng.lognormvariate(5.45, 0.85))
    target_len = max(40, min(target_len, 4000))
    words = [topic]
    while sum(len(w) + 1 for w in words) < target_len:
        words.append(rng.choice(FILLER))
    content = " ".join(words)
    title = f"{topic.split()[0].capitalize()} {topic.split()[1]} note {index}"
    tags = [f"topic:{topic.split()[0]}"]
    return title, content, tags


_INSERT_SQL = (
    "INSERT INTO memory (id, title, content, tags, author, updated_by, "
    "created_at, updated_at, salience, confidence, source_host, expires_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def insert_synthetic_batch(
    conn: sqlite3.Connection,
    rng: random.Random,
    start_index: int,
    count: int,
    author: str = "bench-synthetic",
) -> list[str]:
    """Bulk-insert `count` synthetic notes via the exact production INSERT
    statement `memory.memory_save`'s create path uses (same columns, same
    schema — created by the real `storage.init_db()`), starting at
    `start_index`. Returns the inserted ids.

    Deliberately does not call `memory_save()`: that function runs an
    O(existing-notes) semantic-similarity dedup scan on every create, which
    would make populating a quarter million notes an O(n^2) operation neither
    this benchmark nor a real bulk-import path should ever pay.
    """
    from unified_agent import storage

    ts = storage.now()
    ids = []
    rows = []
    for i in range(start_index, start_index + count):
        note_id = f"bench-{i:08d}"
        title, content, tags = synthetic_note(rng, i)
        rows.append(
            (
                note_id,
                title,
                content,
                json.dumps(tags),
                author,
                author,
                ts,
                ts,
                0.5,
                None,
                "bench-host",
                None,
            )
        )
        ids.append(note_id)
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    return ids


def sample_queries_from_corpus(conn: sqlite3.Connection, n: int, rng: random.Random) -> list[str]:
    """Sample `n` natural-language-ish queries anchored on real corpus notes.

    Each query is a short word span pulled from a randomly chosen note's
    content — long enough (>=3 tokens) to hit memory_search's semantic-match
    branch, and drawn from the corpus vocabulary so both the ground-truth
    brute-force ranking and memory_search's own ranking have something
    meaningful to find, instead of scoring pure noise.
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM memory WHERE id LIKE 'bench-%'").fetchone()["n"]
    if total == 0:
        return []
    seed = rng.randint(0, 2**31 - 1)
    rows = conn.execute(
        "SELECT content FROM memory WHERE id LIKE 'bench-%' ORDER BY (id || ?) LIMIT ?",
        (str(seed), min(n, total)),
    ).fetchall()
    queries = []
    for r in rows:
        words = r["content"].split()
        span = rng.randint(4, 7)
        queries.append(" ".join(words[:span]))
    return queries


# --------------------------------------------------------------------------
# Pure numeric helpers (unit-testable without any DB/model)
# --------------------------------------------------------------------------


def cosine_sims_against_matrix(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Exact cosine similarity of `query_vec` against every row of `matrix`.

    A zero-norm row (or a zero-norm query) scores 0.0, matching
    `embeddings.cosine_similarity_bytes`'s zero-norm guard.
    """
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    query_vec = np.asarray(query_vec, dtype=np.float32)
    qnorm = float(np.linalg.norm(query_vec))
    if qnorm == 0.0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    zero = norms == 0.0
    safe_norms = np.where(zero, 1.0, norms)
    sims = (matrix @ query_vec) / (safe_norms * qnorm)
    sims = np.where(zero, 0.0, sims)
    return np.clip(sims, -1.0, 1.0).astype(np.float32)


def brute_force_topk(
    query_vec: np.ndarray, ids: list[str], matrix: np.ndarray, k: int
) -> list[tuple[str, float]]:
    """Exact top-k cosine-similarity ranking of `ids`/`matrix` against
    `query_vec`. Ground truth for the recall comparison — independent of
    whatever engine (sqlite-vec vec0, or the UDF scan) memory_search itself
    uses."""
    sims = cosine_sims_against_matrix(query_vec, matrix)
    if sims.size == 0:
        return []
    k = min(k, sims.size)
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [(ids[int(i)], float(sims[int(i)])) for i in top]


def recall_at_k(ground_truth_ids: list[str], returned_ids: list[str]) -> float:
    """Fraction of `ground_truth_ids` (a top-k set) present in `returned_ids`.

    Returns 1.0 for an empty ground truth (nothing to miss)."""
    if not ground_truth_ids:
        return 1.0
    gt = set(ground_truth_ids)
    hit = sum(1 for rid in returned_ids if rid in gt)
    return hit / len(gt)


def percentiles_ms(latencies_s: list[float]) -> dict[str, float | int | None]:
    """p50/p95/p99/mean/max in milliseconds from a list of second-latencies."""
    if not latencies_s:
        return {
            "n": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "mean_ms": None,
            "max_ms": None,
        }
    arr = np.array(latencies_s, dtype=np.float64) * 1000.0
    return {
        "n": len(latencies_s),
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
        "mean_ms": float(arr.mean()),
        "max_ms": float(arr.max()),
    }


# --------------------------------------------------------------------------
# DB-touching measurement functions
# --------------------------------------------------------------------------


def embed_missing_notes(conn: sqlite3.Connection, batch_size: int = 2000, log=None) -> dict:
    """Embed every note lacking a `memory_embeddings` row, in batches, timed.

    Byte-for-byte the same production sequence `storage._backfill_embeddings`
    runs (`embeddings.generate_embedding` -> `embeddings.embedding_to_bytes`
    -> INSERT INTO memory_embeddings -> `storage.upsert_memory_vec`), just
    chunked and timed for benchmark progress visibility instead of running as
    one untimed pass. This *is* the "background embedding catch-up"
    mechanism under measurement, not a stand-in for it.
    """
    from unified_agent import storage
    from unified_agent.embeddings import embedding_to_bytes, generate_embedding

    rows = conn.execute(
        "SELECT id, title, content FROM memory "
        "WHERE id NOT IN (SELECT note_id FROM memory_embeddings) "
        "AND id != 'masterindex00' AND id NOT LIKE 'tagindex_%'"
    ).fetchall()
    total = len(rows)
    done = 0
    failures = 0
    t0 = time.perf_counter()
    for i in range(0, total, batch_size):
        chunk = rows[i : i + batch_size]
        for row in chunk:
            try:
                emb_bytes = embedding_to_bytes(
                    generate_embedding(f"{row['title'] or ''}\n\n{row['content']}")
                )
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (note_id, embedding) VALUES (?, ?)",
                    (row["id"], emb_bytes),
                )
                storage.upsert_memory_vec(conn, row["id"], emb_bytes)
            except Exception:
                failures += 1
        conn.commit()
        done += len(chunk)
        if log is not None:
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            log(f"    embedded {done}/{total} ({rate:.1f} notes/s, {elapsed:.1f}s elapsed)")
    elapsed = time.perf_counter() - t0
    return {
        "embedded": done,
        "failures": failures,
        "elapsed_s": elapsed,
        "rate_per_s": (done / elapsed) if elapsed > 0 else None,
    }


def load_embedding_matrix(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    """Load every embedded note as a dense float32 (n, dim) matrix, for the
    brute-force ground-truth pass. One-time cost per scale checkpoint (built
    once, reused across every sampled query)."""
    rows = conn.execute("SELECT note_id, embedding FROM memory_embeddings").fetchall()
    ids = [r["note_id"] for r in rows]
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    dim = len(rows[0]["embedding"]) // 4
    matrix = np.zeros((len(ids), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        if vec.shape[0] == dim:
            matrix[i] = vec
    return ids, matrix


def measure_query_latency(queries: list[str], limit: int = 10) -> dict:
    """Wall-clock memory_search latency over `queries`, once the corpus is at
    steady-state (fully embedded) — the real MCP tool entry point, unmocked."""
    from unified_agent.tools.memory import memory_search

    latencies_s = []
    for q in queries:
        t0 = time.perf_counter()
        memory_search(q, limit=limit)
        latencies_s.append(time.perf_counter() - t0)
    return percentiles_ms(latencies_s)


def measure_recall(conn: sqlite3.Connection, queries: list[str], k: int = 10) -> dict:
    """Two distinct recall questions, per the task's DoD:

    1. `engine_max_abs_similarity_diff` / `engine_exact_match_rate`: does the
       semantic-scoring engine memory_search actually calls
       (`_semantic_similarity_rows`) compute the *same* per-note cosine
       similarity as an independent brute-force numpy pass over the full
       corpus? This is the load-bearing check for the HNSW decision — an
       exact answer here means the current pipeline is not already
       approximating anything at this scale; a real ANN index would trade
       that exactness away.
    2. `mean_recall_at_k_vs_pure_semantic`: recall@k of memory_search's final
       *blended* result set (semantic + FTS + recency + salience + access +
       feedback + entity + vote) against a pure-semantic top-k ground truth.
       Reported for context — a gap here is expected by design (the hybrid
       ranking deliberately weighs more than raw similarity), not evidence of
       scale-induced approximation.
    """
    from unified_agent.embeddings import embedding_to_bytes, generate_embedding
    from unified_agent.tools.memory import _semantic_similarity_rows, memory_search

    ids, matrix = load_embedding_matrix(conn)
    if not ids or not queries:
        return {
            "n_queries": 0,
            "engine_max_abs_similarity_diff": None,
            "engine_compared_rows": 0,
            "mean_recall_at_k_vs_pure_semantic": None,
        }

    max_diff = 0.0
    compared_rows = 0
    recalls = []
    for q in queries:
        qvec = np.asarray(generate_embedding(q), dtype=np.float32)
        gt = brute_force_topk(qvec, ids, matrix, k)
        gt_ids = [note_id for note_id, _ in gt]

        # (1) engine-exactness: every row _semantic_similarity_rows scores,
        # compared against the same rows' brute-force ground truth.
        qbytes = embedding_to_bytes(qvec.tolist())
        engine_rows = _semantic_similarity_rows(conn, qbytes)
        engine_map = {r["note_id"]: r["similarity"] for r in engine_rows}
        sims_gt_all = cosine_sims_against_matrix(qvec, matrix)
        for note_id, sim_gt in zip(ids, sims_gt_all, strict=True):
            sim_engine = engine_map.get(note_id)
            if sim_engine is None:
                continue
            compared_rows += 1
            max_diff = max(max_diff, abs(float(sim_engine) - float(sim_gt)))

        # (2) final blended result set vs pure-semantic top-k, for context.
        hybrid = memory_search(q, limit=k)
        hybrid_ids = [r["id"] for r in hybrid["results"]]
        recalls.append(recall_at_k(gt_ids, hybrid_ids))

    return {
        "n_queries": len(queries),
        "engine_max_abs_similarity_diff": max_diff,
        "engine_compared_rows": compared_rows,
        "mean_recall_at_k_vs_pure_semantic": statistics.mean(recalls) if recalls else None,
    }


def db_footprint(db_path: Path) -> dict:
    """DB file size and per-table breakdown (via sqlite's `dbstat`)."""
    conn = sqlite3.connect(str(db_path))
    table_bytes: dict[str, int] = {}
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        try:
            for name, nbytes in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                table_bytes[name] = int(nbytes or 0)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    file_bytes = db_path.stat().st_size if db_path.exists() else 0
    memory_vec_bytes = sum(v for n, v in table_bytes.items() if n.startswith("memory_vec"))
    memory_fts_bytes = sum(v for n, v in table_bytes.items() if n.startswith("memory_fts"))
    return {
        "file_bytes": file_bytes,
        "memory_table_bytes": table_bytes.get("memory", 0),
        "memory_embeddings_bytes": table_bytes.get("memory_embeddings", 0),
        "memory_vec_bytes": memory_vec_bytes,
        "memory_fts_bytes": memory_fts_bytes,
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _setup_isolated_env(ua_home: Path) -> None:
    ua_home.mkdir(parents=True, exist_ok=True)
    real_home = Path("~/.unified_agent").expanduser()
    assert ua_home.resolve() != real_home.resolve(), (
        "refusing to point the benchmark at the real memory.db"
    )
    os.environ["UA_HOME"] = str(ua_home)
    os.environ["UA_WORKSPACE"] = str(ua_home / "workspace")
    os.environ["UA_VAULT_PATH"] = str(ua_home / "vault")
    Path(os.environ["UA_WORKSPACE"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["UA_VAULT_PATH"]).mkdir(parents=True, exist_ok=True)
    os.environ.pop("UA_BRIDGE_DIR", None)
    # Disable state-sync debounce threads (they otherwise linger past this
    # script's own lifetime, and there is no vault content here worth
    # syncing) — matches scripts/smoke.py's isolation posture.
    os.environ["UA_STATE_SYNC"] = "0"


def run_benchmark(
    scales: list[int],
    queries_per_scale: int,
    limit: int,
    insert_batch_size: int,
    embed_batch_size: int,
    seed: int,
    ua_home: Path,
    log=print,
) -> dict:
    _setup_isolated_env(ua_home)

    from unified_agent import config, post_init, storage

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
    storage.join_init_background_threads(timeout=5.0)

    rng = random.Random(seed)
    conn = storage.connect()
    report: dict[str, Any] = {
        "scales": [],
        "seed": seed,
        "queries_per_scale": queries_per_scale,
        "limit": limit,
        "ua_home": str(ua_home),
    }
    prev_scale = 0
    try:
        for scale in scales:
            to_add = scale - prev_scale
            if to_add < 0:
                raise ValueError(f"scales must be increasing: {scales}")
            log(f"[scale {scale}] inserting {to_add} notes ({prev_scale} -> {scale})")
            t0 = time.perf_counter()
            inserted = 0
            while inserted < to_add:
                cnt = min(insert_batch_size, to_add - inserted)
                insert_synthetic_batch(conn, rng, prev_scale + inserted, cnt)
                inserted += cnt
            insert_elapsed = time.perf_counter() - t0
            log(f"[scale {scale}] insert done in {insert_elapsed:.1f}s")

            log(f"[scale {scale}] embedding catch-up starting")
            embed_stats = embed_missing_notes(conn, batch_size=embed_batch_size, log=log)
            log(
                f"[scale {scale}] embedding catch-up done: {embed_stats['embedded']} notes in "
                f"{embed_stats['elapsed_s']:.1f}s ({embed_stats['rate_per_s']:.1f}/s)"
            )

            queries = sample_queries_from_corpus(conn, queries_per_scale, rng)
            log(f"[scale {scale}] measuring query latency over {len(queries)} queries")
            latency = measure_query_latency(queries, limit=limit)
            log(
                f"[scale {scale}] latency p50={latency['p50_ms']:.1f}ms p95={latency['p95_ms']:.1f}ms"
            )

            log(f"[scale {scale}] measuring recall")
            recall = measure_recall(conn, queries, k=limit)
            log(
                f"[scale {scale}] engine max abs diff vs brute force = "
                f"{recall['engine_max_abs_similarity_diff']}"
            )

            footprint = db_footprint(config.db_path())
            log(f"[scale {scale}] db footprint {footprint['file_bytes'] / 1e6:.1f} MB")

            report["scales"].append(
                {
                    "scale": scale,
                    "insert_elapsed_s": insert_elapsed,
                    "embed_stats": embed_stats,
                    "latency_ms": latency,
                    "recall": recall,
                    "footprint": footprint,
                }
            )
            prev_scale = scale
    finally:
        conn.close()

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scales", default="250000,500000", help="comma-separated increasing note counts"
    )
    parser.add_argument(
        "--queries", type=int, default=50, help="sampled queries per scale checkpoint"
    )
    parser.add_argument("--limit", type=int, default=10, help="memory_search limit param")
    parser.add_argument("--insert-batch-size", type=int, default=5000)
    parser.add_argument("--embed-batch-size", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ua-home", default=None, help="defaults to a fresh temp dir")
    parser.add_argument("--out", default=None, help="JSON results path (default: stdout only)")
    args = parser.parse_args(argv)

    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    if scales != sorted(scales):
        raise SystemExit("--scales must be increasing")

    ua_home = Path(args.ua_home) if args.ua_home else Path(tempfile.mkdtemp(prefix="ua_bench_"))

    report = run_benchmark(
        scales=scales,
        queries_per_scale=args.queries,
        limit=args.limit,
        insert_batch_size=args.insert_batch_size,
        embed_batch_size=args.embed_batch_size,
        seed=args.seed,
        ua_home=ua_home,
    )

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
