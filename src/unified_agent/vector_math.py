"""Vector math for stored embeddings: blob encoding and cosine similarity.

Pure ``struct``/``numpy`` with no model behind it, so it sits in core:
``storage.connect()`` registers :func:`cosine_similarity_bytes` as the
``cosine_similarity()`` SQL function on every connection, and storage must not
import the fastembed client (``embeddings``, an integration). ``embeddings``
re-exports these names, so ``from unified_agent.embeddings import
cosine_similarity_bytes`` keeps working.
"""

from __future__ import annotations

import struct

import numpy as np


def embedding_to_bytes(vector: list[float]) -> bytes:
    """Convert float list embedding to binary bytes."""
    return struct.pack(f"{len(vector)}f", *vector)


def bytes_to_embedding(data: bytes) -> list[float]:
    """Convert binary bytes back to float list embedding."""
    num_floats = len(data) // 4
    return list(struct.unpack(f"{num_floats}f", data))


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    a = np.array(v1)
    b = np.array(v2)
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


# One-slot memo for the query-side vector of a similarity scan.
#
# cosine_similarity_bytes is registered as a SQLite UDF and invoked once per
# candidate row, always with the *same* query blob as its second argument. Its
# norm is therefore recomputed identically on every row of every scan. One slot
# is the right size: a scan's rows all share a single query vector, so the hit
# rate is (rows - 1)/rows and a larger cache would only retain vectors no scan
# will ask for again.
#
# Not locked. A race between two concurrent scans costs one redundant norm and
# is otherwise harmless — the tuple is replaced atomically and never mutated in
# place, so a reader sees either the whole old entry or the whole new one.
_query_vec_cache: tuple[bytes, np.ndarray, float] | None = None


def _query_vector(blob: bytes) -> tuple[np.ndarray, float]:
    """Decode a query blob to (vector, norm), reusing the last one if unchanged."""
    global _query_vec_cache
    cached = _query_vec_cache
    if cached is not None and cached[0] == blob:
        return cached[1], cached[2]
    vec = np.frombuffer(blob, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    _query_vec_cache = (blob, vec, norm)
    return vec, norm


def cosine_similarity_bytes(b1: bytes, b2: bytes) -> float:
    """Compute cosine similarity directly from binary embedding blobs.

    `b2` is treated as the query side and its decoded vector/norm are memoized
    across calls; `b1` is the per-row candidate and is always decoded fresh.
    """
    try:
        v1 = np.frombuffer(b1, dtype=np.float32)
        v2, norm_b = _query_vector(b2)
        if len(v1) == 0 or len(v2) == 0:
            return 0.0
        norm_a = np.linalg.norm(v1)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(v1, v2) / (norm_a * norm_b))
    except Exception:
        return 0.0
