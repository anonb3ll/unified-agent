"""P1 #509: explainable, entity-aware memory retrieval evaluation set."""

from datetime import UTC, datetime, timedelta

from unified_agent import storage
from unified_agent.tools.memory import (
    _extract_query_entities,
    memory_feedback,
    memory_get,
    memory_save,
    memory_search,
)


def _offline_embeddings(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(
        embeddings, "generate_embedding", lambda x: (_ for _ in ()).throw(RuntimeError("offline"))
    )


def test_extract_query_entities_task_branch_agent_project():
    entities = _extract_query_entities(
        "task #509 on branch task/509-p1-explainable-entity-aware-me "
        "claimed by Desktop_Agent4 project:unified-agent"
    )
    by_kind = {e["kind"]: e["value"] for e in entities}
    assert by_kind["task"] == "509"
    assert by_kind["branch"] == "task/509-p1-explainable-entity-aware-me"
    assert by_kind["agent"] == "Desktop_Agent4"
    assert by_kind["project"] == "unified-agent"
    assert any(e["kind"] == "service" and e["value"] == "unified-agent" for e in entities)


def test_search_results_expose_ranking_explanation(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    note = memory_save(
        "Current decision: keep the HTTP hub as the canonical transport",
        title="Hub transport decision",
        tags=["decision", "project:unified-agent"],
        salience=0.8,
    )
    memory_feedback(note["id"], "helpful")

    res = memory_search("hub transport decision")
    hit = next(r for r in res["results"] if r["id"] == note["id"])
    explanation = hit["explanation"]
    for key in (
        "why",
        "lexical",
        "semantic",
        "recency",
        "salience",
        "access",
        "feedback",
        "entity",
        "host",
        "superseded",
        "expired",
        "entity_matches",
        "matched_tags",
    ):
        assert key in explanation
    assert "lexical" in explanation["why"]
    assert explanation["superseded"] is False
    assert explanation["expired"] is False
    assert explanation["feedback"] > 0
    assert explanation["salience"] == 0.8
    assert "decision" in explanation["matched_tags"]


def test_current_decision_outranks_superseded_twin(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    current = memory_save(
        "Current decision: use HTTP hub for primary workspaces",
        title="Hub transport is HTTP",
        tags=["decision"],
    )
    stale = memory_save(
        "Old decision: use stdio everywhere including primary workspaces",
        title="Hub transport is stdio",
        tags=["decision"],
    )
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (current["id"], stale["id"]))
        conn.commit()
    finally:
        conn.close()

    res = memory_search("hub transport decision workspaces")
    ids = [r["id"] for r in res["results"]]
    assert current["id"] in ids
    assert stale["id"] not in ids
    recovered = memory_get(stale["id"])
    assert recovered["superseded_by"] == current["id"]


def test_expired_note_hidden_unless_include_expired_marks_explanation(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    expired = memory_save(
        "Temporary sprint workaround for the flaky handshake",
        title="Expired handshake workaround",
        expires_at=past,
    )
    current = memory_save(
        "Durable handshake procedure after the flaky workaround expired",
        title="Current handshake procedure",
    )

    default = memory_search("handshake workaround")
    default_ids = [r["id"] for r in default["results"]]
    assert current["id"] in default_ids
    assert expired["id"] not in default_ids

    recovered = memory_search("handshake workaround", include_expired=True)
    expired_hit = next(r for r in recovered["results"] if r["id"] == expired["id"])
    current_hit = next(r for r in recovered["results"] if r["id"] == current["id"])
    assert expired_hit["explanation"]["expired"] is True
    assert current_hit["explanation"]["expired"] is False


def test_task_and_branch_entities_rank_and_explain(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    targeted = memory_save(
        "Implement explainable retrieval for task #509 on "
        "task/509-p1-explainable-entity-aware-me claimed-by Desktop_Agent4",
        title="Task 509 retrieval notes",
        tags=["project:unified-agent", "task-509"],
    )
    distractor = memory_save(
        "Implement explainable retrieval for a different backlog item",
        title="Generic retrieval notes",
        tags=["project:browser"],
    )

    res = memory_search("task #509 task/509-p1-explainable-entity-aware-me Desktop_Agent4")
    ids = [r["id"] for r in res["results"]]
    assert targeted["id"] in ids
    if distractor["id"] in ids:
        assert ids.index(targeted["id"]) < ids.index(distractor["id"])
    hit = next(r for r in res["results"] if r["id"] == targeted["id"])
    kinds = {m["kind"] for m in hit["explanation"]["entity_matches"]}
    assert "task" in kinds
    assert "branch" in kinds
    assert "agent" in kinds
    assert any(w.startswith("entity:") for w in hit["explanation"]["why"])


def test_source_host_appears_in_explanation(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    note = memory_save("Cross-device provenance fixture about the desktop hub", title="Hosted note")
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET source_host=? WHERE id=?", ("DesktopHost", note["id"]))
        conn.commit()
    finally:
        conn.close()

    res = memory_search("desktop hub provenance")
    hit = next(r for r in res["results"] if r["id"] == note["id"])
    assert hit["explanation"]["host"] == "DesktopHost"
    assert hit["source_host"] == "DesktopHost"
