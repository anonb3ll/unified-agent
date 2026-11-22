"""Reproducible evaluation harness for ranked task search (task #1419).

A fixture corpus of tasks across five topics, a query per topic with its
known-relevant task ids, and MRR / recall@k computed against
`task_list(query=...)`. This is the harness `docs/task-search.md` refers to as
"reproducible": same corpus, same queries, same thresholds, every run -- no
network, no live model download (semantic scoring degrades to FTS-only if
fastembed is unavailable, and the thresholds below are set low enough to hold
either way; see the embeddings-required test for the semantic-specific claim).

Run standalone for a quick score readout: pytest -q tests/test_task_search_eval.py -s
"""

from __future__ import annotations

from dataclasses import dataclass

from unified_agent.tools.tasks import task_add, task_list

# ---------------------------------------------------------------------------
# Fixture corpus: (title, notes, topic). Topics double as the relevance label
# -- every task tagged with a query's topic is "relevant" to that query.
# Five real topics plus a block of cross-topic noise tasks, so bm25 has enough
# of a corpus for its IDF term to actually discriminate (the two-task
# examples in tests/test_task_search.py deliberately avoid depending on that;
# this harness is where the discrimination itself gets measured).
# ---------------------------------------------------------------------------

_CORPUS: list[tuple[str, str, str]] = [
    # topic: auth
    (
        "Fix login race condition in session store",
        "Two concurrent login requests create duplicate sessions for the same user.",
        "auth",
    ),
    (
        "Investigate authentication latency spike",
        "Login endpoint p99 latency doubled after the last auth service deploy.",
        "auth",
    ),
    (
        "Add rate limiting to the login endpoint",
        "Brute-force login attempts are not throttled; add a per-IP rate limit to auth.",
        "auth",
    ),
    (
        "Support passkey login on the web client",
        "Add WebAuthn passkey sign-in as an alternative to password login.",
        "auth",
    ),
    # topic: deploy
    (
        "Write deployment runbook for the release pipeline",
        "Document the CI/CD steps operators follow to deploy a new release.",
        "deploy",
    ),
    (
        "Flaky deploy pipeline step on staging",
        "The deploy pipeline's staging smoke test intermittently times out.",
        "deploy",
    ),
    (
        "Add rollback step to deployment pipeline",
        "A failed deploy should automatically roll back to the previous release.",
        "deploy",
    ),
    (
        "Speed up the container build in the deploy pipeline",
        "Docker image build is the slowest step in the deployment pipeline.",
        "deploy",
    ),
    # topic: billing
    (
        "Fix duplicate invoice generation",
        "Some customers received two invoices for the same billing period.",
        "billing",
    ),
    (
        "Add proration for mid-cycle plan upgrades",
        "Billing does not prorate charges when a customer upgrades mid-cycle.",
        "billing",
    ),
    (
        "Investigate failed payment retries",
        "Invoice payment retries are not honoring the configured backoff schedule.",
        "billing",
    ),
    # topic: docs
    (
        "Update onboarding docs for new hires",
        "The onboarding documentation still references the old repo layout.",
        "docs",
    ),
    (
        "Write API reference docs for the export endpoint",
        "The export endpoint has no published API documentation.",
        "docs",
    ),
    (
        "Fix broken links in the architecture docs",
        "Several internal links in the architecture documentation 404.",
        "docs",
    ),
    # topic: testing
    (
        "Add integration tests for the checkout flow",
        "The checkout flow has no automated integration test coverage.",
        "testing",
    ),
    (
        "Reduce flaky tests in the CI test suite",
        "Roughly one in twenty CI test runs fails on an unrelated flaky test.",
        "testing",
    ),
    (
        "Add load testing for the search endpoint",
        "No load test exists for the search endpoint ahead of the traffic increase.",
        "testing",
    ),
    # noise: unrelated filler so bm25 has a real corpus to discriminate against
    ("Repaint the office breakroom", "Facilities request, unrelated to engineering.", "noise"),
    (
        "Order new standing desks",
        "Facilities request for the new hires starting next month.",
        "noise",
    ),
    ("Renew the domain registration", "Annual domain renewal, due in three weeks.", "noise"),
    ("Plan the Q3 offsite agenda", "Draft an agenda for the quarterly team offsite.", "noise"),
    ("Archive old marketing assets", "Move last year's marketing assets to cold storage.", "noise"),
]

# query -> topic it targets. Every task tagged with that topic is relevant;
# everything else (including other real topics) is not.
#
# task_fts's MATCH query OR-joins tokens (see _sanitize_fts_or_query in
# task_search.py -- deliberately different from memory_search's implicit-AND
# fts_query, since a ranked natural-language search should let bm25 rank
# partial matches rather than return nothing when not every word lands in
# one row). Each query below is still a single literal token that recurs
# across its topic's tasks (checked by hand against `_CORPUS`; no stemmer is
# configured for either fts5 table, so "invoice" and "invoices" are different
# tokens) -- single-token queries exercise the topic separation this harness
# measures without also depending on bm25's exact behavior across a mix of
# common and rare terms. The OR-join's actual benefit on a natural-language,
# multi-word query is covered directly in
# tests/test_task_search.py::test_natural_language_query_finds_via_fts_alone.
_QUERIES: dict[str, str] = {
    "login": "auth",
    "pipeline": "deploy",
    "invoice": "billing",
    "docs": "docs",
    "test": "testing",
}


@dataclass
class QueryResult:
    query: str
    ranked_ids: list[int]
    relevant_ids: set[int]

    def reciprocal_rank(self) -> float:
        for rank, task_id in enumerate(self.ranked_ids, start=1):
            if task_id in self.relevant_ids:
                return 1.0 / rank
        return 0.0

    def recall_at_k(self, k: int) -> float:
        if not self.relevant_ids:
            return 0.0
        top_k = set(self.ranked_ids[:k])
        return len(top_k & self.relevant_ids) / len(self.relevant_ids)


def _seed_corpus() -> dict[str, list[int]]:
    """Insert the fixture corpus, return topic -> [task_id, ...]."""
    ids_by_topic: dict[str, list[int]] = {}
    for title, notes, topic in _CORPUS:
        task_id = task_add(title=title, notes=notes)["id"]
        ids_by_topic.setdefault(topic, []).append(task_id)
    return ids_by_topic


def _run_eval(limit: int = 10) -> list[QueryResult]:
    ids_by_topic = _seed_corpus()
    results = []
    for query, topic in _QUERIES.items():
        ranked = task_list(status="all", query=query, limit=limit)["tasks"]
        results.append(
            QueryResult(
                query=query,
                ranked_ids=[t["id"] for t in ranked],
                relevant_ids=set(ids_by_topic[topic]),
            )
        )
    return results


# Thresholds are set with margin below what this corpus actually scores
# (verified at authoring time: MRR 1.0, mean recall@3 0.833 with the local
# embedding model available; semantic scoring only ever pulled in additional
# same-topic tasks *after* the exact FTS hits, never displaced one, and
# degrading to FTS-only, e.g. no fastembed model cached, scores identically
# on this corpus since every query is a literal token that recurs across its
# topic's tasks) -- enough margin to catch a real ranking regression without
# being sensitive to bm25's run-to-run stability on a small corpus.
_MIN_MRR = 0.6
_MIN_RECALL_AT_3 = 0.5


def test_ranked_search_eval_meets_mrr_and_recall_thresholds():
    results = _run_eval()

    mrr = sum(r.reciprocal_rank() for r in results) / len(results)
    mean_recall_at_3 = sum(r.recall_at_k(3) for r in results) / len(results)

    failures = [r.query for r in results if r.reciprocal_rank() == 0.0]
    assert not failures, f"queries with no relevant hit in the ranked results: {failures}"

    assert mrr >= _MIN_MRR, f"MRR {mrr:.3f} below threshold {_MIN_MRR} ({results})"
    assert mean_recall_at_3 >= _MIN_RECALL_AT_3, (
        f"mean recall@3 {mean_recall_at_3:.3f} below threshold {_MIN_RECALL_AT_3} ({results})"
    )


def test_ranked_search_eval_never_surfaces_noise_task_first():
    """A sanity check independent of the MRR/recall thresholds: for every
    topic query, the single top-ranked result must not be one of the
    deliberately unrelated "noise" filler tasks."""
    results = _run_eval()
    for r in results:
        assert r.ranked_ids, f"query {r.query!r} returned no results at all"
        assert r.ranked_ids[0] in r.relevant_ids, (
            f"query {r.query!r} ranked a non-relevant task first: {r.ranked_ids[:3]}"
        )


def test_semantic_only_eval_recalls_synonym_without_lexical_overlap(monkeypatch):
    """Measure semantic recall separately from the literal-token topic queries."""
    from unified_agent import embeddings

    _seed_corpus()
    relevant = task_add(title="Repair the car", notes="The vehicle needs service.")["id"]
    task_add(title="Audit the invoice", notes="Check payment records.")

    def fixture_embedding(text):
        return [1.0, 0.0] if "automobile" in text or "Repair the car" in text else [0.0, 1.0]

    monkeypatch.setattr(embeddings, "generate_embedding", fixture_embedding)
    ranked = task_list(status="all", query="automobile", limit=10)["tasks"]
    measured = QueryResult(
        query="automobile",
        ranked_ids=[task["id"] for task in ranked],
        relevant_ids={relevant},
    )
    assert measured.reciprocal_rank() == 1.0
    assert measured.recall_at_k(3) == 1.0


if __name__ == "__main__":
    # Standalone readout: `python tests/test_task_search_eval.py` inside a
    # test-isolated DB, or just run under pytest -s for the same numbers.
    import json

    from unified_agent import storage

    storage.init_db()
    for result in _run_eval():
        print(
            json.dumps(
                {
                    "query": result.query,
                    "reciprocal_rank": result.reciprocal_rank(),
                    "recall_at_3": result.recall_at_k(3),
                    "ranked_ids": result.ranked_ids,
                    "relevant_ids": sorted(result.relevant_ids),
                }
            )
        )
