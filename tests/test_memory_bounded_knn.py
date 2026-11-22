"""Bounded KNN for the memory semantic leg (task #2214, audit M5).

The vec0 query used to ask for ``k = COUNT(*)`` because sqlite-vec applies the
joined predicates (tenant, superseded, deleted, index, model) after the KNN
cut. sqlite-vec also refuses any ``k`` above 4096, so on a store past that
size the accelerated path raised and every search silently fell back to the
per-row UDF scan. The bounded loop grows ``k`` until it can prove that no
unseen row could qualify, and returns exactly what the full scan returns.

Vectors are synthetic and inserted directly, so no test loads the model.
"""

from __future__ import annotations

import json
import math
import random
import struct

import pytest

from unified_agent import embeddings, memory_chunks, memory_knn, org_partition, storage
from unified_agent.tools import memory as memory_tools
from unified_agent.tools.memory import _semantic_similarity_rows, memory_save, memory_search

DIM = storage.MEMORY_VEC_DIM


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _near(base: list[float], noise: float, rng: random.Random) -> list[float]:
    """``base`` plus noise of total norm about ``noise``: cos ~ 1/sqrt(1+noise^2)."""
    sigma = noise / math.sqrt(DIM)
    return _unit([b + rng.gauss(0.0, sigma) for b in base])


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{DIM}f", *vector)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


QUERY = _unit([1.0] + [0.0] * (DIM - 1))


def _require_mirror(conn) -> None:
    try:
        conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()
    except Exception:
        pytest.skip("sqlite-vec not loaded on this host; the UDF path has no k")


def _insert(conn, note_id, vector, *, org_id=None, superseded_by=None, tags="[]", content=None):
    stamp = storage.now()
    conn.execute(
        "INSERT INTO memory (id, title, content, tags, created_at, updated_at, org_id, "
        "superseded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (note_id, note_id, content or f"body {note_id}", tags, stamp, stamp, org_id, superseded_by),
    )
    blob = _pack(vector)
    conn.execute(
        "INSERT INTO memory_embeddings (note_id, embedding, model) VALUES (?, ?, NULL)",
        (note_id, blob),
    )
    storage.upsert_memory_vec(conn, note_id, blob)


def _seed_mixed(conn, count: int, *, seed: int = 7, dead_closer: bool = True) -> dict[str, dict]:
    """A store where 80% of the vectors are superseded, deleted or foreign.

    With ``dead_closer`` the dead notes are the nearest to QUERY, so a fixed
    small k would return few or no live rows; the live ones sit further out.
    Without it dead and live share one spread.
    """
    rng = random.Random(seed)
    partitioned = org_partition.is_partitioned(conn)
    notes: dict[str, dict] = {}
    for i in range(count):
        note_id = f"n{i:05d}"
        dead = i % 5 != 0  # 80% of the store is not a live, local note
        if dead_closer:
            noise = 0.05 + rng.random() * 0.3 if dead else 0.4 + rng.random() * 2.0
        else:
            noise = 0.05 + rng.random() * 2.0
        vector = _near(QUERY, noise, rng)
        kind = "live"
        kwargs: dict = {}
        if dead:
            kind = ("superseded", "deleted", "foreign")[i % 3]
            # A partitioned store refuses foreign rows at insertion. Keep
            # the same ineligible-vector density using local deleted notes;
            # the ordinary suite still exercises actual foreign distractors.
            if kind == "foreign" and partitioned:
                kind = "deleted"
            if kind == "superseded":
                kwargs["superseded_by"] = "n00000"
            elif kind == "deleted":
                kwargs["tags"] = json.dumps(["meta:deleted"])
            else:
                kwargs["org_id"] = "other-org"
        _insert(conn, note_id, vector, **kwargs)
        notes[note_id] = {"kind": kind, "vector": vector}
    conn.commit()
    return notes


def _expected(notes, *, floor=None):
    """Full-scan answer: live notes' cosine to QUERY, best first."""
    rows = [
        (note_id, _cos(n["vector"], QUERY)) for note_id, n in notes.items() if n["kind"] == "live"
    ]
    if floor is not None:
        rows = [r for r in rows if r[1] >= floor]
    return sorted(rows, key=lambda r: (-r[1], r[0]))


def _record_k(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = memory_knn.knn_raw

    def recording(conn, table, emb_bytes, k):
        calls.append(k)
        return real(conn, table, emb_bytes, k)

    monkeypatch.setattr(memory_knn, "knn_raw", recording)
    return calls


def _ranked(rows):
    return sorted(((r["note_id"], r["similarity"]) for r in rows), key=lambda r: (-r[1], r[0]))


def test_floor_cut_matches_the_full_scan_on_a_mostly_dead_store(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 600)
        floor = 0.6
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=floor)
        got = [r for r in _ranked(rows) if r[1] >= floor]
        want = _expected(notes, floor=floor)
        assert want, "fixture must leave live notes above the floor"
        assert [r[0] for r in got] == [r[0] for r in want]
        for (_, g), (_, w) in zip(got, want, strict=True):
            assert g == pytest.approx(w, abs=1e-4)
    finally:
        conn.close()


def test_need_cut_returns_the_same_top_rows_with_k_far_below_the_store(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 1200, dead_closer=False)
        calls = _record_k(monkeypatch)
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.0, need=10)
        got = _ranked(rows)[:10]
        want = _expected(notes, floor=0.0)[:10]
        assert [r[0] for r in got] == [r[0] for r in want]
        assert calls, "the vec0 mirror was not queried"
        assert calls[0] == memory_knn.KNN_START_K
        assert max(calls) <= 256
    finally:
        conn.close()


def test_store_past_the_sqlite_vec_k_limit_still_uses_the_mirror(isolated_env, monkeypatch):
    """Regression: k=COUNT(*) above 4096 raised, so search ran the UDF scan."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, memory_knn.VEC0_MAX_K + 200, seed=11, dead_closer=False)
        calls = _record_k(monkeypatch)

        def no_udf(*_args):
            raise AssertionError("fell back to the per-row UDF scan")

        conn.create_function("cosine_similarity", 2, no_udf)
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.0, need=50)
        got = _ranked(rows)[:50]
        want = _expected(notes, floor=0.0)[:50]
        assert [r[0] for r in got] == [r[0] for r in want]
        assert calls and max(calls) <= memory_knn.VEC0_MAX_K
    finally:
        conn.close()


def test_a_cut_it_cannot_prove_falls_back_to_the_full_scan(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    monkeypatch.setattr(memory_knn, "VEC0_MAX_K", 16)
    monkeypatch.setattr(memory_knn, "KNN_START_K", 4)
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 200)
        calls = _record_k(monkeypatch)
        # No floor and no need: only an exhausted mirror proves the cut, and
        # 16 rows cannot exhaust 200, so the UDF scan must answer.
        rows = _semantic_similarity_rows(conn, _pack(QUERY))
        assert calls == [4, 8, 16]
        assert {r["note_id"] for r in rows} == {n for n, v in notes.items() if v["kind"] == "live"}
    finally:
        conn.close()


def test_an_empty_mirror_is_still_an_empty_result(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        assert _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.5, need=5) == []
    finally:
        conn.close()


def test_where_sql_filters_before_the_need_count(isolated_env, monkeypatch):
    """A caller filter (memory_search's eligibility) is applied after each
    cut, so rows it rejects never count toward `need`."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 600)
        live = sorted(n for n, v in notes.items() if v["kind"] == "live")
        keep = set(live[::4])
        marks = ",".join("?" * len(keep))
        rows = _semantic_similarity_rows(
            conn,
            _pack(QUERY),
            min_similarity=0.0,
            need=5,
            where_sql=f"id IN ({marks})",
            where_params=tuple(sorted(keep)),
        )
        got = [r[0] for r in _ranked(rows)][:5]
        want = [r[0] for r in _expected(notes, floor=0.0) if r[0] in keep][:5]
        assert got == want
    finally:
        conn.close()


def test_chunk_scores_are_bounded_and_match_the_full_scan(isolated_env, monkeypatch):
    conn = storage.connect()
    try:
        _require_mirror(conn)
        rng = random.Random(5)
        expected: dict[str, float] = {}
        for i in range(300):
            note_id = f"c{i:04d}"
            stamp = storage.now()
            conn.execute(
                "INSERT INTO memory (id, title, content, tags, created_at, updated_at) "
                "VALUES (?, ?, 'long body', '[]', ?, ?)",
                (note_id, note_id, stamp, stamp),
            )
            for chunk_index in range(3):
                vector = _near(QUERY, 0.1 + rng.random() * 4.0, rng)
                blob = _pack(vector)
                cur = conn.execute(
                    "INSERT INTO memory_chunks (note_id, chunk_index, embedding, model) "
                    "VALUES (?, ?, ?, NULL)",
                    (note_id, chunk_index, blob),
                )
                conn.execute(
                    "INSERT INTO memory_chunk_vec (rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, blob),
                )
                sim = _cos(vector, QUERY)
                expected[note_id] = max(expected.get(note_id, -1.0), sim)
        conn.commit()
        calls = _record_k(monkeypatch)
        floor = 0.7
        scores = memory_chunks.max_similarities(
            conn,
            _pack(QUERY),
            where_sql=" AND m.org_id IS ?",
            where_params=(None,),
            min_similarity=floor,
        )
        got = {n: s for n, s in scores.items() if s >= floor}
        want = {n: s for n, s in expected.items() if s >= floor}
        assert want
        assert set(got) == set(want)
        for note_id, sim in want.items():
            assert got[note_id] == pytest.approx(sim, abs=1e-4)
        assert calls and max(calls) < 900
    finally:
        conn.close()


def _patch_query_vectors(monkeypatch, mapping: dict[str, list[float]]):
    def fake(text: str) -> list[float]:
        for key, vector in mapping.items():
            if key in text:
                return vector
        return QUERY

    monkeypatch.setattr(embeddings, "generate_embedding", fake)


def test_memory_search_explains_a_far_lexical_hit_with_its_real_similarity(
    isolated_env, monkeypatch
):
    """A lexical hit outside the semantic cut still reports the cosine the
    full scan would have given it, not 0.0."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    monkeypatch.setenv("UA_MEMORY_SEMANTIC_TOP_K", "5")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        rng = random.Random(3)
        for i in range(400):
            _insert(conn, f"s{i:04d}", _near(QUERY, 0.05 + rng.random() * 0.5, rng))
        # Nearly orthogonal to QUERY, far below the floor, but a keyword match.
        far = _unit([0.2] + [0.0] * (DIM - 2) + [1.0])
        _insert(conn, "far-note", far, content="zebracorn deployment checklist")
        conn.commit()
    finally:
        conn.close()
    _patch_query_vectors(monkeypatch, {})
    calls = _record_k(monkeypatch)
    res = memory_search("zebracorn")
    hit = next(r for r in res["results"] if r["id"] == "far-note")
    assert hit["similarity"] == pytest.approx(_cos(far, QUERY), abs=1e-4)
    assert hit["explanation"]["semantic"] == pytest.approx(_cos(far, QUERY), abs=1e-4)
    assert calls and max(calls) < 400
    assert res["retrieval"]["mode"] == "hybrid"


def test_memory_search_semantic_leg_matches_the_full_scan(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    monkeypatch.setenv("UA_MEMORY_SEMANTIC_TOP_K", "20")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        _seed_mixed(conn, 800, seed=21)
    finally:
        conn.close()
    _patch_query_vectors(monkeypatch, {})
    calls = _record_k(monkeypatch)
    bounded = memory_search("nothing lexical matches qqqq", limit=20)
    assert calls, "the bounded run did not use the vec0 mirror"

    def unusable(*_args, **_kwargs):
        raise storage.sqlite3.OperationalError("mirror disabled for the parity baseline")

    monkeypatch.setattr(memory_knn, "knn_raw", unusable)
    full = memory_search("nothing lexical matches qqqq", limit=20)
    assert [r["id"] for r in bounded["results"]] == [r["id"] for r in full["results"]]
    assert (
        bounded["retrieval"]["semantic"]["matched_count"]
        == (full["retrieval"]["semantic"]["matched_count"])
    )


def test_memory_save_dedup_finds_a_near_duplicate_in_a_large_store(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    rng = random.Random(9)
    target = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
    conn = storage.connect()
    try:
        _require_mirror(conn)
        for i in range(500):
            _insert(conn, f"d{i:04d}", _near(QUERY, 0.05 + rng.random() * 0.5, rng))
        _insert(conn, "twin", target, content="the original twin note")
        conn.commit()
    finally:
        conn.close()
    _patch_query_vectors(monkeypatch, {"twin": _near(target, 0.01, rng)})
    calls = _record_k(monkeypatch)
    res = memory_save("a second twin note", check_only=True)
    assert [s["id"] for s in res["similar"]] == ["twin"]
    assert calls and max(calls) < 500


def test_search_args_still_forbid_unknown_keywords():
    """No new tool parameter: the bound is internal (task #854 contract)."""
    import inspect

    params = inspect.signature(memory_search).parameters
    assert "min_similarity" not in params and "need" not in params
    assert memory_tools._semantic_similarity_rows is _semantic_similarity_rows


# --- Review at 7e0125ff (Codex): P2 explanation parity with chunks, P3 budget ---


def _at(cos: float, axis: int) -> list[float]:
    """A unit vector with exactly ``cos`` similarity to QUERY."""
    vector = [0.0] * DIM
    vector[0] = cos
    vector[axis] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return vector


def _insert_chunk(conn, note_id: str, chunk_index: int, vector: list[float]) -> None:
    blob = _pack(vector)
    cur = conn.execute(
        "INSERT INTO memory_chunks (note_id, chunk_index, embedding, model) VALUES (?, ?, ?, NULL)",
        (note_id, chunk_index, blob),
    )
    conn.execute(
        "INSERT INTO memory_chunk_vec (rowid, embedding) VALUES (?, ?)", (cur.lastrowid, blob)
    )


def _record_tables(monkeypatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    real = memory_knn.knn_raw

    def recording(conn, table, emb_bytes, k):
        calls.append((table, k))
        return real(conn, table, emb_bytes, k)

    monkeypatch.setattr(memory_knn, "knn_raw", recording)
    return calls


def test_chunk_merge_does_not_understate_a_lexical_hit_similarity(isolated_env, monkeypatch):
    """Reviewer's P2 case: the single-vector cut proves top-5 and omits a
    lexical hit whose single vector is 0.90; its one chunk scores 0.80. The
    reported similarity must be the full scan's 0.90, not the chunk's 0.80."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    monkeypatch.setenv("UA_MEMORY_SEMANTIC_TOP_K", "5")
    monkeypatch.setenv("UA_MEMORY_SEMANTIC_MIN_SIMILARITY", "0.65")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        for i in range(100):
            _insert(conn, f"s{i:03d}", _at(0.99 - i * 0.0005, 1 + i))
        _insert(conn, "zebra", _at(0.90, 200), content="zebracorn deployment checklist")
        _insert_chunk(conn, "zebra", 0, _at(0.80, 201))
        conn.commit()
    finally:
        conn.close()
    _patch_query_vectors(monkeypatch, {})
    res = memory_search("zebracorn")
    hit = next(r for r in res["results"] if r["id"] == "zebra")
    assert hit["similarity"] == pytest.approx(0.90, abs=1e-4)
    assert hit["explanation"]["semantic"] == pytest.approx(0.90, abs=1e-4)


def test_chunk_leg_is_cut_at_the_kth_single_and_keeps_top_k_membership(isolated_env, monkeypatch):
    """With `need`, a chunk below the need-th qualifying single cannot enter
    the top-K, so the chunk cut stops there instead of at the bare floor."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    rng = random.Random(13)
    merged: dict[str, float] = {}
    conn = storage.connect()
    try:
        _require_mirror(conn)
        axis = 1
        for i in range(300):
            note_id = f"m{i:03d}"
            single = 0.70 + rng.random() * 0.29
            _insert(conn, note_id, _at(single, axis))
            axis = axis % (DIM - 1) + 1
            best = single
            for chunk_index in range(2):
                # Two notes carry a chunk that beats every single vector.
                chunk = 0.995 if i in (17, 230) and chunk_index == 0 else 0.66 + rng.random() * 0.3
                _insert_chunk(conn, note_id, chunk_index, _at(chunk, axis))
                axis = axis % (DIM - 1) + 1
                best = max(best, chunk)
            merged[note_id] = best
        conn.commit()
        calls = _record_tables(monkeypatch)
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.65, need=5)
        got = [r[0] for r in _ranked(rows)][:5]
        want = [n for n, _ in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))][:5]
        assert got == want
        assert "m017" in got and "m230" in got
        chunk_calls = [k for table, k in calls if table == "memory_chunk_vec"]
        # 600 chunks sit above the 0.65 floor; the K-th single bounds the cut.
        assert chunk_calls == [memory_knn.KNN_START_K]
    finally:
        conn.close()


def test_zero_eligible_yield_falls_back_after_one_native_scan(isolated_env, monkeypatch):
    """P3: when the nearest rows are all dead, more doublings only repeat the
    flat native scan. The loop gives up after one cut and the UDF answers."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 1500, dead_closer=True)
        calls = _record_k(monkeypatch)
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.0, need=50)
        assert calls == [100]
        got = [r[0] for r in _ranked(rows)][:50]
        want = [r[0] for r in _expected(notes, floor=0.0)][:50]
        assert got == want
    finally:
        conn.close()


def test_floor_only_cut_stops_after_the_round_budget(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 1500, dead_closer=True)
        calls = _record_k(monkeypatch)
        floor = 0.5
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=floor)
        assert len(calls) == memory_knn.KNN_MAX_ROUNDS
        assert calls == [memory_knn.KNN_START_K * 2**i for i in range(memory_knn.KNN_MAX_ROUNDS)]
        got = {r[0] for r in _ranked(rows) if r[1] >= floor}
        assert got == {r[0] for r in _expected(notes, floor=floor)}
    finally:
        conn.close()


def test_low_yield_projects_k_instead_of_doubling(isolated_env, monkeypatch):
    """A 20% eligible yield sizes the next cut from the yield, so `need`
    is reached in two native scans rather than a doubling ladder."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    conn = storage.connect()
    try:
        _require_mirror(conn)
        notes = _seed_mixed(conn, 3000, seed=4, dead_closer=False)
        calls = _record_k(monkeypatch)
        rows = _semantic_similarity_rows(conn, _pack(QUERY), min_similarity=0.0, need=50)
        assert len(calls) == 2 and calls[0] == 100 and calls[1] > 200
        got = [r[0] for r in _ranked(rows)][:50]
        assert got == [r[0] for r in _expected(notes, floor=0.0)][:50]
    finally:
        conn.close()
