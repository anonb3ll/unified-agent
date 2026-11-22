"""Self-curating recall loop: auto-vote ranking, promote path, maintenance (task #915)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, recall_curation, storage
from unified_agent.recall_curation import (
    get_votes,
    memory_promote,
    recall_maintenance,
    vote_term,
)
from unified_agent.tools.memory import memory_feedback, memory_save, memory_search
from unified_agent.tools.vault import vault_links, vault_read


@pytest.fixture(autouse=True)
def _vault_dir():
    root = Path(config.vault_root())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save(content: str, title: str, **kw) -> str:
    kw.setdefault("author", "Desktop_Agent1")
    return memory_save(content=content, title=title, **kw)["id"]


# ─── auto-vote + ranking ──────────────────────────────────────────────────────


def test_vote_term_is_bounded_and_monotonic():
    assert vote_term(0, 0) == 0.0
    assert 0 < vote_term(1, 0) < vote_term(5, 0) < 1.0
    # an upvote outweighs a bare recall
    assert vote_term(0, 1) > vote_term(1, 0)


def test_recall_hit_increments_recalled_count():
    note_id = _save("widget calibration procedure steps", "Calibration note")
    assert get_votes(note_id)["recalled"] == 0
    memory_search("widget calibration procedure")
    assert get_votes(note_id)["recalled"] == 1
    memory_search("widget calibration procedure")
    assert get_votes(note_id)["recalled"] == 2


def test_helpful_feedback_bumps_upvote_counter():
    note_id = _save("kubernetes rollout gotcha notes", "Rollout note")
    memory_feedback(note_id, "helpful")
    assert get_votes(note_id)["upvoted"] == 1
    memory_feedback(note_id, "not_helpful")
    assert get_votes(note_id)["upvoted"] == 1  # only 'helpful' votes


def test_search_logs_recall_quality():
    _save("recall quality logging probe content", "Probe")
    memory_search("recall quality logging probe")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT query, result_count, hit FROM recall_quality_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["hit"] == 1
    assert row["result_count"] >= 1


def test_seeded_votes_shift_ranking(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_WEIGHT_VOTE", "0.6")
    body = "octane manifold torque sequence reference"
    first = _save(body, "Note One")
    second = _save(body, "Note Two")

    # Baseline: identical FTS match, tie broken by title → "Note One" leads.
    baseline = [r["id"] for r in memory_search("octane manifold torque sequence")["results"]]
    assert baseline[:2] == [first, second]

    for _ in range(6):
        recall_curation.record_upvote(second)

    boosted = memory_search("octane manifold torque sequence")["results"]
    assert boosted[0]["id"] == second
    assert boosted[0]["explanation"]["vote"] > 0


# ─── promote path ────────────────────────────────────────────────────────────


def _wiki(rel: str) -> Path:
    return Path(config.vault_root()).expanduser() / rel


def test_promote_refuses_low_confidence():
    note_id = _save("half-formed hunch about caching", "Weak note", confidence=0.4)
    with pytest.raises(ToolError, match="confidence"):
        memory_promote(note_id)


def test_promote_writes_wiki_page_with_attribution_and_index_link():
    note_id = _save(
        "Always run ruff check before pushing; ruff format alone lets CI go red.",
        "Pre-push ruff gotcha",
        confidence=0.9,
        author="Laptop_Agent2",
    )
    result = memory_promote(
        note_id, summary="run ruff check, not just format", agent="Desktop_Agent1"
    )
    assert result["promoted"] is True
    assert result["path"].startswith("Wiki/Learnings/")
    assert result["index_updated"] is True

    page = vault_read(result["path"])["content"]
    assert f"source_memory_id: {note_id}" in page
    assert "promoted_by: Desktop_Agent1" in page
    assert "original_author: Laptop_Agent2" in page
    assert "Always run ruff check before pushing" in page

    outgoing = vault_links("Wiki/Index.md")["outgoing"]
    assert any(result["wiki_link"] in link for link in outgoing)


@pytest.mark.parametrize(
    "env",
    [{"UA_TRANSPORT": "http"}, {"UA_TRANSPORT": "stdio", "UA_REQUIRE_CALLER_IDENTITY": "1"}],
    ids=["http", "required_identity"],
)
def test_promote_never_stamps_an_unverified_claimed_agent(monkeypatch, env):
    """Task #1899: with no verified identity where a claim cannot be bound, the
    promoter is "unknown", never the raw claimed ``agent``."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert config.agent_identity_var.get() is None
    note_id = _save("durable rule with an unbound promoter", "Unbound promoter", confidence=0.9)

    preview = memory_promote(note_id, agent="Anyone", dry_run=True)
    assert "promoted_by: unknown" in preview["markdown"]
    assert "Anyone" not in preview["markdown"]

    written = memory_promote(note_id, slug="unbound-promoter", agent="Anyone")
    page = vault_read(written["path"])["content"]
    assert written["promoted_by"] == "unknown"
    assert "promoted_by: unknown" in page
    assert "Anyone" not in page


def test_promote_stamps_the_verified_identity_over_http(monkeypatch):
    monkeypatch.setenv("UA_TRANSPORT", "http")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        note_id = _save("durable rule with a bound promoter", "Bound promoter", confidence=0.9)
        result = memory_promote(note_id, dry_run=True)
    finally:
        config.agent_identity_var.reset(token)
    assert "promoted_by: Desktop_Agent1" in result["markdown"]


def test_promote_dry_run_writes_nothing():
    note_id = _save("solid durable rule worth keeping", "Solid rule", confidence=0.85)
    result = memory_promote(note_id, dry_run=True)
    assert result["promoted"] is False
    assert result["dry_run"] is True
    assert "markdown" in result and "index_line" in result
    assert not _wiki(result["path"]).exists()


def test_promote_is_idempotent_on_the_index_block():
    note_id = _save("repeatable promotion content here", "Repeatable", confidence=0.9)
    memory_promote(note_id, slug="repeatable-learning")
    memory_promote(note_id, slug="repeatable-learning")
    index_text = _wiki("Wiki/Index.md").read_text(encoding="utf-8")
    assert index_text.count("[[Wiki/Learnings/repeatable-learning") == 1


def test_promote_only_targets_wiki():
    note_id = _save("must stay in the wiki layer", "Wiki only", confidence=0.9)
    result = memory_promote(note_id)
    assert result["path"].startswith("Wiki/")
    assert ".." not in result["path"]


# ─── maintenance (report-only) ───────────────────────────────────────────────


def test_maintenance_refuses_non_dry_run():
    with pytest.raises(ToolError, match="never deletes"):
        recall_maintenance(dry_run=False)


def test_maintenance_flags_without_deleting():
    stale_id = _save("ancient unrecalled note", "Ancient", salience=0.2)
    weak_id = _save("shaky low confidence claim", "Shaky", confidence=0.1)
    fresh_id = _save("current well-recalled note", "Fresh", salience=0.8, confidence=0.9)

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE memory SET updated_at='2023-01-01T00:00:00+00:00' WHERE id=?",
            (stale_id,),
        )
        conn.commit()
    finally:
        conn.close()

    report = recall_maintenance()
    assert report["deletes_nothing"] is True
    stale_ids = {c["id"] for c in report["stale"]}
    weak_ids = {c["id"] for c in report["low_confidence"]}
    assert stale_id in stale_ids
    assert weak_id in weak_ids
    assert fresh_id not in stale_ids

    # Nothing was removed.
    conn = storage.connect()
    try:
        remaining = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM memory WHERE id IN (?,?,?)", (stale_id, weak_id, fresh_id)
            ).fetchall()
        }
    finally:
        conn.close()
    assert remaining == {stale_id, weak_id, fresh_id}
