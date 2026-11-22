"""Offline retrieval availability and safe diagnostic contract (#1955)."""

import json
import sqlite3

import pytest

from unified_agent import embeddings
from unified_agent.tools import memory as memory_tools


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0] + [0.0] * 383)


def _save(content="quokka retrieval", **kwargs):
    return memory_tools.memory_save(content=content, author="Desktop_Agent2", **kwargs)["id"]


def test_hybrid_miss_is_available_even_with_no_embedded_notes():
    response = memory_tools.memory_search("quokka")
    assert response["results"] == []
    assert response["retrieval"] == {
        "version": 1,
        "mode": "hybrid",
        "lexical": {"status": "queried", "matched_count": 0},
        "semantic": {"status": "available", "matched_count": 0, "failure_category": None},
        "graph": {"status": "queried", "seed_count": 0, "matched_count": 0},
    }


def test_healthy_semantic_no_hit_keeps_lexical_result(monkeypatch):
    note = _save()
    monkeypatch.setattr(memory_tools, "_semantic_similarity_rows", lambda *_args, **_kwargs: [])
    response = memory_tools.memory_search("quokka")
    assert [row["id"] for row in response["results"]] == [note]
    assert response["retrieval"]["mode"] == "hybrid"
    assert response["retrieval"]["lexical"]["matched_count"] == 1
    assert response["retrieval"]["semantic"] == {
        "status": "available",
        "matched_count": 0,
        "failure_category": None,
    }
    assert response["results"][0]["similarity"] == 0.0


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "phase,error_type,category",
    [
        (embeddings.PHASE_ACQUIRE, OSError, "model_acquisition"),
        (embeddings.PHASE_INFER, OSError, "runtime"),
        (embeddings.PHASE_ACQUIRE, AssertionError, "runtime"),
        (embeddings.PHASE_IMPORT, ImportError, "runtime"),
        (None, RuntimeError, "runtime"),
    ],
)
def test_fallback_exposes_only_safe_failure_category(
    monkeypatch, empty, phase, error_type, category
):
    note = _save() if not empty else None
    private_diagnostic = "private-path-and-credential-shaped-diagnostic"
    inner = error_type(private_diagnostic)
    if phase is not None:
        embeddings._tag_phase(inner, phase)

    def fail(_query):
        raise RuntimeError(private_diagnostic) from inner

    monkeypatch.setattr(embeddings, "generate_embedding", fail)
    response = memory_tools.memory_search("quokka")
    assert [row["id"] for row in response["results"]] == ([note] if note else [])
    assert response["retrieval"] == {
        "version": 1,
        "mode": "lexical_fallback",
        "lexical": {"status": "queried", "matched_count": int(not empty)},
        "semantic": {"status": "unavailable", "matched_count": 0, "failure_category": category},
        "graph": {"status": "queried", "seed_count": int(not empty), "matched_count": 0},
    }
    assert private_diagnostic not in json.dumps(response)
    if note:
        assert response["results"][0]["similarity"] is None


def test_semantic_query_failure_is_runtime_fallback(monkeypatch):
    note = _save()

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("private SQL diagnostic")

    monkeypatch.setattr(memory_tools, "_semantic_similarity_rows", fail)
    response = memory_tools.memory_search("quokka")
    assert [row["id"] for row in response["results"]] == [note]
    assert response["retrieval"]["mode"] == "lexical_fallback"
    assert response["retrieval"]["semantic"]["failure_category"] == "runtime"
    assert "private SQL diagnostic" not in json.dumps(response)


def test_counts_exclude_other_tenants_and_ineligible_semantic_candidates(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_other")
    foreign = _save()
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_current")
    wrong_tag = _save(content="quokka wrong scope", tags=["project:other"])
    eligible = _save(content="quokka current scope", tags=["project:target"])
    monkeypatch.setattr(
        memory_tools,
        "_semantic_similarity_rows",
        lambda *_args, **_kwargs: [
            {"note_id": foreign, "similarity": 0.99},
            {"note_id": wrong_tag, "similarity": 0.98},
            {"note_id": eligible, "similarity": 0.90},
        ],
    )
    response = memory_tools.memory_search("quokka", tags=["project:target"])
    assert [row["id"] for row in response["results"]] == [eligible]
    assert response["retrieval"]["lexical"]["matched_count"] == 1
    assert response["retrieval"]["semantic"]["matched_count"] == 1


def test_leg_counts_respect_semantic_floor_and_response_limit(monkeypatch):
    strong = _save(content="quokka strong matching note", title="Strong")
    weak = _save(content="quokka weak matching note", title="Weak")
    monkeypatch.setattr(
        memory_tools,
        "_semantic_similarity_rows",
        lambda *_args, **_kwargs: [
            {"note_id": strong, "similarity": 0.9},
            {"note_id": weak, "similarity": 0.1},
        ],
    )
    response = memory_tools.memory_search("quokka", limit=1)
    assert len(response["results"]) == 1
    assert response["retrieval"]["lexical"]["matched_count"] == 2
    assert response["retrieval"]["semantic"]["matched_count"] == 1
