"""memory_search ranking: AND-then-OR FTS, reciprocal rank fusion, bounded priors (#1913)."""

from __future__ import annotations

import math

import pytest

from unified_agent import storage
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.memory import memory_save, memory_search

DIM = 384


def _offline(monkeypatch):
    from unified_agent import embeddings

    def _fail(_text):
        raise RuntimeError("offline")

    monkeypatch.setattr(embeddings, "generate_embedding", _fail)


def _unit(*pairs: tuple[int, float]) -> list[float]:
    vec = [0.0] * DIM
    for index, value in pairs:
        vec[index] = value
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _fake_embeddings(monkeypatch, table: dict[str, list[float]]):
    """Deterministic stand-in for fastembed: the first table key found in the
    lowercased text picks the vector; anything else lands on its own axis."""
    from unified_agent import embeddings

    def _embed(text: str) -> list[float]:
        lowered = text.lower()
        for needle, vec in table.items():
            if needle in lowered:
                return vec
        return _unit((DIM - 1, 1.0))

    monkeypatch.setattr(embeddings, "generate_embedding", _embed)


def _save(content: str, title: str, **kw) -> str:
    kw.setdefault("author", "Desktop_Agent1")
    return memory_save(content=content, title=title, **kw)["id"]


def _set(note_id: str, **cols):
    conn = storage.connect()
    try:
        assignments = ", ".join(f"{col}=?" for col in cols)
        conn.execute(f"UPDATE memory SET {assignments} WHERE id=?", (*cols.values(), note_id))
        conn.commit()
    finally:
        conn.close()


def _ids(query: str, **kw) -> list[str]:
    return [r["id"] for r in memory_search(query, **kw)["results"]]


# ─── FTS: AND first, OR fallback ─────────────────────────────────────────────


def test_natural_language_query_falls_back_to_or(monkeypatch):
    _offline(monkeypatch)
    target = _save(
        "The headless executor stops claiming dispatches after the lid closes overnight.",
        "Executor daemon wedges after the Mac sleeps",
    )
    # "stuck" and "why" appear in no note, so an AND-only query matches nothing.
    res = memory_search("why is the executor daemon stuck overnight")
    hit = next(r for r in res["results"] if r["id"] == target)
    assert hit["explanation"]["lexical_mode"] == "or"
    assert hit["explanation"]["ranks"]["lexical"] == 1


def test_and_matches_rank_ahead_of_or_only_matches(monkeypatch):
    _offline(monkeypatch)
    # The OR-only note repeats one term so its bm25 for that term alone is strong.
    partial = _save("wombat wombat wombat wombat wombat", "wombat wombat")
    full = _save("A wombat sighting near the quarry gate.", "Quarry sighting log")
    monkeypatch.setenv("UA_MEMORY_FTS_OR_THRESHOLD", "5")
    ids = _ids("wombat quarry")
    assert ids.index(full) < ids.index(partial)
    by_id = {r["id"]: r for r in memory_search("wombat quarry")["results"]}
    assert by_id[full]["explanation"]["lexical_mode"] == "and"
    assert by_id[partial]["explanation"]["lexical_mode"] == "or"


def test_fts_candidates_are_bounded(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setenv("UA_MEMORY_FTS_LIMIT", "3")
    for idx in range(6):
        _save(f"zephyrine calibration step {idx}", f"Zephyrine note {idx}")
    assert len(_ids("zephyrine", limit=10)) == 3


def test_stopwords_alone_do_not_or_match_everything(monkeypatch):
    _offline(monkeypatch)
    _save("Totally unrelated content about the garden and the fence.", "Garden fence")
    # Only stopwords overlap; OR mode must not turn "the" into a match.
    assert _ids("what is the plan for qxzzv") == []


# ─── ranking signals ────────────────────────────────────────────────────────


def test_access_count_does_not_affect_rank(monkeypatch):
    _offline(monkeypatch)
    first = _save("marmoset feeding schedule reference", "Note A")
    second = _save("marmoset feeding schedule reference", "Note B")
    _set(second, access_count=5000)
    assert _ids("marmoset feeding schedule")[:2] == [first, second]


def test_auto_recall_votes_do_not_affect_rank(monkeypatch):
    _offline(monkeypatch)
    first = _save("ocelot enclosure humidity reference", "Note A")
    second = _save("ocelot enclosure humidity reference", "Note B")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO memory_recall_votes (note_id, recalled_count, upvoted_count) "
            "VALUES (?, 5000, 0)",
            (second,),
        )
        conn.commit()
    finally:
        conn.close()
    assert _ids("ocelot enclosure humidity")[:2] == [first, second]


def test_popular_note_does_not_outrank_a_better_match(monkeypatch):
    _offline(monkeypatch)
    strong = _save(
        "Zebrafish tank cycling: zebrafish need a cycled tank before stocking.",
        "Zebrafish tank cycling",
    )
    popular = _save(
        "General aquarium notes covering lighting, substrate, filters, plants, "
        "water changes, feeding, holidays, and one mention of zebrafish.",
        "Aquarium general notes",
    )
    _set(popular, access_count=900)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO memory_recall_votes (note_id, recalled_count, upvoted_count) "
            "VALUES (?, 1200, 0)",
            (popular,),
        )
        conn.commit()
    finally:
        conn.close()
    assert _ids("zebrafish")[:2] == [strong, popular]


def test_prior_multiplier_is_bounded(monkeypatch):
    _offline(monkeypatch)
    boosted = _save("quokka habitat survey results", "Quokka boosted")
    sunk = _save("quokka habitat survey results", "Quokka sunk")
    _set(boosted, salience=1.0, feedback_helpful=500)
    _set(sunk, salience=0.0, feedback_wrong=500, feedback_not_helpful=500)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO memory_recall_votes (note_id, recalled_count, upvoted_count) "
            "VALUES (?, 0, 500)",
            (boosted,),
        )
        conn.commit()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in memory_search("quokka habitat")["results"]}
    assert 1.0 < by_id[boosted]["explanation"]["prior"] <= 1.2
    assert 0.8 <= by_id[sunk]["explanation"]["prior"] < 1.0


def test_explanation_reports_leg_ranks_rrf_and_prior(monkeypatch):
    _offline(monkeypatch)
    note = _save("capybara transport permit checklist", "Capybara permit")
    hit = memory_search("capybara permit")["results"][0]
    assert hit["id"] == note
    expl = hit["explanation"]
    assert set(expl["ranks"]) == {"lexical", "semantic", "entity", "recency", "graph"}
    assert expl["ranks"]["lexical"] == 1
    assert expl["ranks"]["semantic"] is None
    assert expl["rrf"] > 0
    assert hit["combined_score"] == pytest.approx(expl["rrf"] * expl["prior"], rel=1e-3)
    assert "lexical" in expl["why"]


# ─── semantic gate ──────────────────────────────────────────────────────────


def test_short_query_gets_semantic_recall(monkeypatch):
    shared = _unit((0, 1.0))
    _fake_embeddings(monkeypatch, {"container": shared, "docker image": shared})
    note = _save(
        "A multi-stage build cut the harbor docker image from 1.2 GB to 180 MB.",
        "Harbor image slimmed",
    )
    hit = next(r for r in memory_search("container smaller")["results"] if r["id"] == note)
    assert hit["explanation"]["ranks"]["semantic"] == 1
    assert "semantic" in hit["explanation"]["why"]


def test_weak_semantic_similarity_is_not_a_match(monkeypatch):
    query_vec = _unit((0, 1.0))
    # cosine(query, note) = 0.6: inside the bge-small false-positive band.
    note_vec = _unit((0, 0.6), (1, 0.8))
    _fake_embeddings(monkeypatch, {"ferret": query_vec, "garden": note_vec})
    _save("Notes about the garden irrigation timer.", "Garden irrigation")
    assert _ids("ferret travel crate rules overseas") == []


# ─── session packet (lifecycle/packet._load_memory) ─────────────────────────


def _packet_ids(title: str) -> list[str]:
    task_id = task_tools.task_add(title=title, project="unified-agent")["id"]
    packet = build_session_packet(task_id=task_id, agent="Desktop_Agent4")
    return [hit["id"] for hit in packet.get("memory") or []]


def test_packet_memory_matches_any_title_token_ranked_by_bm25():
    best = memory_save(
        content="Zirconwedge executor daemon stops claiming dispatches overnight.",
        title="Zirconwedge executor daemon",
        tags=["project:unified-agent"],
        author="Desktop_Agent4",
        confidence=0.75,
    )
    weaker = memory_save(
        content="A broad note that mentions an executor once among many other words "
        "about scheduling, queues, retries, and backoff policies.",
        title="Scheduling overview",
        tags=["project:unified-agent"],
        author="Desktop_Agent4",
        confidence=0.95,
    )
    # "investigate" and "wedge" appear in neither note: AND would inject nothing.
    ids = _packet_ids("Investigate zirconwedge executor daemon wedge")
    assert best["id"] in ids
    if weaker["id"] in ids:
        assert ids.index(best["id"]) < ids.index(weaker["id"])


def test_ineligible_fts_hits_do_not_hide_live_note_or_suppress_or(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setenv("UA_MEMORY_FTS_LIMIT", "10")
    monkeypatch.setenv("UA_MEMORY_FTS_OR_THRESHOLD", "5")
    for i in range(12):
        hidden = _save("quokka quarry", f"Hidden {i}", tags=["other"])
        _set(hidden, superseded_by="archived")
    live = _save("A quokka sighting", "Current", tags=["target"])
    assert live in _ids("quokka quarry", tags=["target"])


def test_wrong_host_semantic_hits_do_not_consume_top_k(monkeypatch):
    from unified_agent import embeddings
    from unified_agent.tools import memory as mod

    monkeypatch.setenv("UA_MEMORY_SEMANTIC_TOP_K", "1")
    _offline(monkeypatch)
    excluded = _save("unrelated wording", "Other host")
    target = _save("different wording", "Target host")
    _set(excluded, source_host="A")
    _set(target, source_host="B")
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _q: _unit((0, 1)))
    monkeypatch.setattr(
        mod,
        "_semantic_similarity_rows",
        lambda *_a, **_kw: [
            {"note_id": excluded, "similarity": 0.99},
            {"note_id": target, "similarity": 0.8},
        ],
    )
    assert _ids("zebras", source_host="B") == [target]


def test_exact_tag_matches_qualify_before_fts_limit(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setenv("UA_MEMORY_FTS_LIMIT", "10")
    for i in range(12):
        _save("quokka", f"A{i}", tags=["Meta:Choice"])
    target = _save("quokka", "Z target", tags=["meta:choice"])
    assert target in _ids("quokka", tags=["meta:choice"])


def test_stopword_only_packet_title_keeps_searchable_context(monkeypatch):
    _offline(monkeypatch)
    note = _save("how", "How", confidence=1.0)
    task = task_tools.task_add("How to do it")
    packet = build_session_packet(task_id=task["id"])
    assert note in [r["id"] for r in packet["memory"]]


# ─── Eligibility materialized once per search (task #2444) ───────────────────

_WORDS = (
    "deploy hub restart launchd rollback runbook embedding model chunk window vector "
    "vault obsidian inbox wiki journal task claim review queue ci shard depot billing "
    "secret token sandbox graph entity pagerank sdk protocol railway staging probe"
).split()


def _bag(text: str) -> list[float]:
    """Deterministic graded vectors: a bag of known words, so similarities vary."""
    vec = [0.0] * DIM
    for word in text.lower().split():
        word = word.strip(".,:;#")
        if word in _WORDS:
            vec[_WORDS.index(word)] += 1.0
    vec[DIM - 1] += 0.5
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _eligibility_store(monkeypatch) -> dict[str, str]:
    from unified_agent import embeddings

    monkeypatch.setattr(embeddings, "generate_embeddings", lambda ts: [_bag(t) for t in ts])
    rng = __import__("random").Random(2444)
    ids: list[str] = []
    for i in range(70):
        words = rng.sample(_WORDS, 6)
        body = " ".join(words) + f" note {i} task #{100 + i % 7}"
        if i % 9 == 0:  # past the single-vector window: chunks engage
            body = " ".join(" ".join(rng.sample(_WORDS, 8)) for _ in range(90))
        tags = [rng.choice(["deploy", "vault", "ci"]), rng.choice(["fact", "decision"])]
        links = [f"extends:{rng.choice(ids)}"] if ids and i % 4 == 0 else None
        ids.append(_save(body, f"{words[0]} {words[1]} {i}", tags=tags, links=links))
    named = {"deleted": ids[3], "superseded": ids[5], "expired": ids[7], "host": ids[11]}
    _set(named["deleted"], tags='["deploy", "meta:deleted"]')
    _set(named["superseded"], superseded_by=ids[6])
    _set(named["expired"], expires_at="2020-01-01T00:00:00+00:00")
    _set(named["host"], source_host="OtherHost")
    return named


def _search_cases() -> list[tuple[str, dict]]:
    queries = [" ".join(_WORDS[i : i + n]) for n in (1, 2, 3) for i in range(0, 30, 2)]
    cases = [(q, {}) for q in queries]
    cases += [(q, {"tags": ["deploy"]}) for q in queries[:6]]
    cases += [(q, {"tags": ["vault", "fact"]}) for q in queries[6:10]]
    cases += [(q, {"source_host": "OtherHost"}) for q in queries[10:12]]
    cases += [(q, {"include_expired": True}) for q in queries[12:14]]
    cases += [(q, {"as_of": "2999-01-01T00:00:00+00:00"}) for q in queries[14:16]]
    cases += [("task #103", {}), ("deploy task #101", {"limit": 30})]
    return cases


def _outcome(res: dict) -> dict:
    return {
        "ids": [r["id"] for r in res["results"]],
        "scores": [r["combined_score"] for r in res["results"]],
        "ranks": [r["explanation"]["ranks"] for r in res["results"]],
        "retrieval": res["retrieval"],
    }


def test_materialized_eligibility_returns_identical_results(monkeypatch):
    """Same ids, order, scores, leg ranks and diagnostics as the inline
    predicate, over 60+ queries with tag, host, expiry and as_of filters."""
    from unified_agent.tools import memory as mod

    _eligibility_store(monkeypatch)
    cases = _search_cases()
    assert len(cases) >= 50
    # Searches bump access counts and recall votes; neither ranks, but compare
    # each case back to back so the two runs read the same store.
    seen = {"semantic": 0, "graph": 0, "lexical": 0}
    for query, kw in cases:
        monkeypatch.setattr(mod, "_MATERIALIZE_ELIGIBILITY", False)
        reference = _outcome(memory_search(query, **kw))
        monkeypatch.setattr(mod, "_MATERIALIZE_ELIGIBILITY", True)
        assert _outcome(memory_search(query, **kw)) == reference, (query, kw)
        for leg in seen:
            seen[leg] += reference["retrieval"][leg]["matched_count"] > 0
    # Every leg that reads the eligibility set took part.
    assert all(seen.values()), seen


def test_materialized_eligibility_keeps_filtered_notes_out(monkeypatch):
    named = _eligibility_store(monkeypatch)
    hits = set()
    for query, kw in _search_cases():
        if not kw:
            hits |= set(_ids(query, limit=50))
    assert named["deleted"] not in hits
    assert named["superseded"] not in hits
    assert named["expired"] not in hits
    assert named["host"] in set(_ids("note", source_host="OtherHost", limit=50)) | hits


def test_tag_predicate_runs_once_per_row_per_search(monkeypatch):
    """The Python tag check ran inside every leg's statement (about twenty
    full-table passes per search); now it runs once per row."""
    import json as _json
    import types

    from unified_agent.tools import memory as mod

    _eligibility_store(monkeypatch)
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    finally:
        conn.close()
    calls = {"n": 0}

    def counting_loads(raw, *a, **kw):
        calls["n"] += 1
        return _json.loads(raw, *a, **kw)

    monkeypatch.setattr(mod, "json", types.SimpleNamespace(loads=counting_loads, dumps=_json.dumps))
    memory_search("deploy hub restart", limit=10)
    # One tag check per row, plus one decode per scored candidate.
    assert calls["n"] <= 2 * rows
    calls["n"] = 0
    monkeypatch.setattr(mod, "_MATERIALIZE_ELIGIBILITY", False)
    memory_search("deploy hub restart", limit=10)
    assert calls["n"] > 3 * rows
