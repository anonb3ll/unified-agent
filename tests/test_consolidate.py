"""Tests for superseded_by tombstones + proposal-based, reversible consolidation.

Boundary under test: consolidation NEVER destroys. Proposals mutate nothing;
apply only sets a reversible pointer preserving provenance; restore fully
re-promotes; and a demoted note is hidden from search/list yet still gettable.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import consolidate, llm_client, storage
from unified_agent.consolidate import (
    _pick_survivor,
    apply_batch,
    apply_supersede,
    generate_proposal,
    restore_note,
)
from unified_agent.tools.memory import memory_get, memory_list, memory_save, memory_search

_COLS = (
    "id, title, content, tags, author, updated_by, created_at, updated_at, "
    "salience, confidence, last_accessed, access_count, superseded_by"
)


@pytest.fixture(autouse=True)
def _stubbed_adjudication(monkeypatch):
    """Default every test in this module to a canned DUPLICATE verdict.

    Routing `build_proposals()` through `llm_client.complete()` made every
    clustering test here depend on a reachable provider. That dependency is
    invisible on a developer machine with Ollama running and fatal in CI, which
    has none — the failure mode is a green local run and a red pipeline, which
    is exactly how it was found.

    Stubbing to DUPLICATE restores the pre-adjudication contract for tests that
    are about clustering, survivor choice or non-mutation: similarity alone
    decides, and the test stays about its own subject. Tests that *are* about
    adjudication install their own stub, which wins — monkeypatch in the test
    body is applied after this fixture.
    """
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {
            "text": "VERDICT: DUPLICATE\nREASON: canned adjudication for tests.",
            "provider": "stub",
        },
    )


def _dump() -> list[tuple]:
    """Full row snapshot of the memory table (row-identity check for the DB)."""
    conn = storage.connect()
    try:
        rows = conn.execute(f"SELECT {_COLS} FROM memory ORDER BY id").fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def _dump_notes() -> list[tuple]:
    """`_dump` without the auto-maintained index notes."""
    return [r for r in _dump() if not r[0].startswith(("masterindex", "tagindex_"))]


def _index_contents() -> dict[str, str]:
    return {r[0]: r[2] for r in _dump() if r[0].startswith(("masterindex", "tagindex_"))}


def _row(note_id: str) -> dict:
    conn = storage.connect()
    try:
        r = conn.execute(f"SELECT {_COLS} FROM memory WHERE id=?", (note_id,)).fetchone()
        return dict(r) if r is not None else None
    finally:
        conn.close()


class TestSupersedeVisibility:
    def test_hidden_from_search_and_list_but_gettable_and_restorable(self, isolated_env):
        survivor = memory_save("alpha widget survivor content", title="Survivor", tags=["t"])
        loser = memory_save("beta gadget loser content", title="Loser", tags=["t"])

        # Both visible before demotion.
        assert any(h["id"] == loser["id"] for h in memory_search("gadget")["results"])
        assert any(i["id"] == loser["id"] for i in memory_list()["items"])

        apply_supersede(survivor["id"], [loser["id"]])

        # Hidden from search + list.
        assert not any(h["id"] == loser["id"] for h in memory_search("gadget")["results"])
        assert not any(i["id"] == loser["id"] for i in memory_list()["items"])
        assert not any(i["id"] == loser["id"] for i in memory_list(tag="t")["items"])

        # Still gettable by id, with the pointer visible.
        got = memory_get(loser["id"])
        assert got["id"] == loser["id"]
        assert got["superseded_by"] == survivor["id"]

        # Survivor stays visible.
        assert any(h["id"] == survivor["id"] for h in memory_search("widget")["results"])

        # Restore re-promotes fully.
        res = restore_note(loser["id"])
        assert res["restored"] is True
        assert res["was_superseded_by"] == survivor["id"]
        assert any(h["id"] == loser["id"] for h in memory_search("gadget")["results"])
        assert any(i["id"] == loser["id"] for i in memory_list()["items"])
        assert memory_get(loser["id"])["superseded_by"] is None


class TestApplyPreservesProvenance:
    def test_apply_only_sets_pointer(self, isolated_env):
        survivor = memory_save("keep me content", title="Keep", author="Desktop_Agent1")
        loser = memory_save("demote me content", title="Demote", author="Laptop_Agent1")

        before = _row(loser["id"])
        apply_supersede(survivor["id"], [loser["id"]])
        after = _row(loser["id"])

        # Only superseded_by changed; all provenance preserved.
        assert after["superseded_by"] == survivor["id"]
        for col in ("author", "created_at", "updated_at", "updated_by", "content", "title"):
            assert after[col] == before[col], f"{col} must be preserved on a demoted note"

    def test_apply_is_idempotent(self, isolated_env):
        survivor = memory_save("s content", title="S")
        loser = memory_save("l content", title="L")
        apply_supersede(survivor["id"], [loser["id"]])
        res = apply_supersede(survivor["id"], [loser["id"]])
        assert res["applied_count"] == 0  # already demoted under this survivor

    def test_apply_rejects_self_and_superseded_survivor(self, isolated_env):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        with pytest.raises(ToolError, match="cannot supersede itself"):
            apply_supersede(a["id"], [a["id"]])
        # Demote b under a, then a-as-survivor-onto-b would chain; refuse demoting
        # notes onto an already-superseded survivor.
        apply_supersede(a["id"], [b["id"]])
        with pytest.raises(ToolError, match="itself superseded"):
            apply_supersede(b["id"], [a["id"]])

    def test_apply_unknown_ids(self, isolated_env):
        a = memory_save("a content", title="A")
        with pytest.raises(ToolError, match="does not exist"):
            apply_supersede(a["id"], ["deadbeefdead"])
        with pytest.raises(ToolError, match="does not exist"):
            apply_supersede("deadbeefdead", [a["id"]])


def _cand(note_id: str, updated_at: str, access_count: int, content_len: int) -> dict:
    """A `_load_candidates`-shaped row, minus the fields the picker never reads."""
    return {
        "id": note_id,
        "title": f"note {note_id}",
        "updated_at": updated_at,
        "access_count": access_count,
        "content_len": content_len,
        "salience": 0.5,
        "embedding": None,
    }


class TestPickSurvivor:
    """Recency is the right primary signal *between* authoring sessions and the
    wrong one *within* one. Live case, 2026-07-28: two Desktop_Agent5 notes
    written 66s apart clustered as duplicates, and the newer one — accessed once,
    and missing the red-tier and never-delete clauses the older one carried —
    was proposed as survivor. Demoting hides a note from memory_search, so that
    proposal would have removed those clauses from every future recall.
    """

    def test_same_burst_prefers_the_more_used_and_richer_note(self):
        # 66 seconds apart: one authoring burst, not a considered rewrite.
        newer_thin = _cand("233ee3f772c3", "2026-07-12T16:14:25+00:00", 1, 546)
        older_rich = _cand("238d152b5133", "2026-07-12T16:13:19+00:00", 21, 722)

        assert _pick_survivor([newer_thin, older_rich])["id"] == "238d152b5133"
        # Order of the cluster list must not decide it.
        assert _pick_survivor([older_rich, newer_thin])["id"] == "238d152b5133"

    def test_access_count_outranks_length_within_the_burst(self):
        # A long note nobody reads does not beat a short one that is used.
        used_short = _cand("aaa", "2026-07-12T16:13:19+00:00", 30, 200)
        unused_long = _cand("bbb", "2026-07-12T16:14:00+00:00", 0, 5000)

        assert _pick_survivor([used_short, unused_long])["id"] == "aaa"

    def test_a_genuine_later_rewrite_still_wins(self):
        # Outside the burst window the original rationale holds: the freshest
        # phrasing is the one worth keeping, even if the old note is better read.
        stale_popular = _cand("old", "2026-07-12T16:13:19+00:00", 99, 900)
        rewrite = _cand("new", "2026-07-20T09:00:00+00:00", 2, 300)

        assert _pick_survivor([stale_popular, rewrite])["id"] == "new"

    def test_ties_break_deterministically_on_id(self):
        a = _cand("aaa", "2026-07-12T16:13:19+00:00", 5, 400)
        b = _cand("bbb", "2026-07-12T16:13:19+00:00", 5, 400)

        assert _pick_survivor([a, b])["id"] == _pick_survivor([b, a])["id"]

    def test_missing_or_null_fields_do_not_crash_the_picker(self):
        # _load_candidates can yield NULL access_count/updated_at on old rows.
        sparse = {"id": "aaa", "title": "t", "updated_at": None, "access_count": None}
        other = _cand("bbb", "2026-07-12T16:13:19+00:00", 3, 100)

        assert _pick_survivor([sparse, other])["id"] == "bbb"


class TestProposalIsNonMutating:
    def test_proposal_mutates_nothing(self, isolated_env):
        # Two near-identical notes → a cluster (when embeddings are available).
        memory_save(
            "The deployment uses a blue-green rollout strategy for zero downtime releases.",
            title="Deploy A",
        )
        memory_save(
            "The deployment uses a blue-green rollout strategy for zero-downtime releases.",
            title="Deploy B",
        )

        before = _dump()
        res = generate_proposal(write=True)
        after = _dump()

        assert before == after, "generate_proposal must not mutate the memory DB"
        assert res["artifact_path"] is not None  # falls back to a report file
        if res["has_embeddings"]:
            assert res["cluster_count"] >= 1
            assert res["demote_count"] >= 1

    def test_proposal_without_embeddings_mutates_nothing(self, isolated_env, monkeypatch):
        # Simulate fastembed being unavailable: notes save with no embedding row,
        # so clustering degrades gracefully to zero proposals — and still mutates
        # nothing.
        from unified_agent import embeddings

        def boom(text):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(embeddings, "generate_embedding", boom)

        memory_save("standalone note one about topics", title="One")
        memory_save("standalone note two about matters", title="Two")

        before = _dump()
        res = generate_proposal(write=False)
        after = _dump()

        assert before == after
        assert res["has_embeddings"] is False
        assert res["cluster_count"] == 0
        assert res["proposals"] == []


class TestLLMAdjudication:
    @staticmethod
    def _candidates():
        return [
            {
                "id": "note-a",
                "title": "Memory decision A",
                "content": "Keep the executor in validate-and-hold mode.",
                "embedding": b"a",
            },
            {
                "id": "note-b",
                "title": "Memory decision B",
                "content": "Promote the executor to autonomous merge after observation.",
                "embedding": b"b",
            },
        ]

    def _stub_cluster(self, monkeypatch, candidates, verdict):
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
        monkeypatch.setattr(
            consolidate.llm_client,
            "complete",
            lambda prompt, system=None, **kwargs: {
                "text": f"VERDICT: {verdict}\nREASON: Test adjudication.",
                "provider": "test",
            },
        )

    def test_template_similar_cluster_is_omitted_when_notes_are_unrelated(self, monkeypatch):
        candidates = [
            {
                "id": "delegation-a",
                "title": "Delegation task 437",
                "content": "Task 437 routes weekly memory clusters through adjudication.",
                "embedding": b"a",
            },
            {
                "id": "delegation-b",
                "title": "Delegation task 440",
                "content": "Task 440 measures executor trust ladder outcomes.",
                "embedding": b"b",
            },
        ]
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])

        calls = []

        def fake_complete(prompt, system=None, **kwargs):
            calls.append((prompt, system))
            return {"text": "VERDICT: UNRELATED\nREASON: Different tasks.", "provider": "test"}

        monkeypatch.setattr(consolidate.llm_client, "complete", fake_complete)

        built = consolidate.build_proposals(object(), threshold=0.9)

        assert built["proposals"] == []
        assert len(calls) == 1
        assert "Task 437 routes" in calls[0][0]
        assert "Task 440 measures" in calls[0][0]

    def test_sequential_decisions_are_not_proposed_when_adjudicated_as_contradiction(
        self, monkeypatch
    ):
        self._stub_cluster(monkeypatch, self._candidates(), "CONTRADICTION")

        assert consolidate.build_proposals(object(), threshold=0.9)["proposals"] == []

    def test_adjudicated_duplicate_still_produces_a_reversible_proposal(self, monkeypatch):
        self._stub_cluster(monkeypatch, self._candidates(), "DUPLICATE")

        proposals = consolidate.build_proposals(object(), threshold=0.9)["proposals"]

        assert len(proposals) == 1
        assert len(proposals[0]["superseded"]) == 1

    def test_llm_failure_propagates_instead_of_silently_proposing(self, monkeypatch):
        candidates = self._candidates()
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])

        def fail(prompt, system=None, **kwargs):
            raise llm_client.LLMConnectionError("all providers unavailable")

        monkeypatch.setattr(consolidate.llm_client, "complete", fail)

        with pytest.raises(llm_client.LLMConnectionError, match="all providers unavailable"):
            consolidate.build_proposals(object(), threshold=0.9)

    def test_malformed_llm_verdict_is_conservative_and_not_proposed(self, monkeypatch):
        candidates = self._candidates()
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
        monkeypatch.setattr(
            consolidate.llm_client,
            "complete",
            lambda prompt, system=None, **kwargs: {
                "text": "maybe these are alike",
                "provider": "test",
            },
        )

        assert consolidate.build_proposals(object(), threshold=0.9)["proposals"] == []

    def test_declined_clusters_are_carried_out_of_build_not_dropped(self, monkeypatch):
        """A rejected cluster is a finding; dropping it made three different
        outcomes render as the same empty artifact."""
        self._stub_cluster(monkeypatch, self._candidates(), "CONTRADICTION")

        built = consolidate.build_proposals(object(), threshold=0.9)

        assert built["proposals"] == []
        assert len(built["rejected"]) == 1
        rej = built["rejected"][0]
        assert rej["verdict"] == "CONTRADICTION"
        assert rej["reason"] == "Test adjudication."
        assert rej["provider"] == "test"
        assert rej["note_ids"] == ["note-a", "note-b"]

    def test_structured_json_verdict_is_not_marked_unparsed(self, monkeypatch):
        candidates = self._candidates()
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
        monkeypatch.setattr(
            consolidate.llm_client,
            "complete",
            lambda prompt, system=None, **kwargs: {
                "text": '{"verdict":"UNRELATED","reason":"Different tasks."}',
                "provider": "test",
            },
        )

        built = consolidate.build_proposals(object(), threshold=0.9)

        assert built["proposals"] == []
        assert built["rejected"][0]["verdict"] == "UNRELATED"
        assert built["rejected"][0]["reason"] == "Different tasks."

    def test_unparseable_verdict_is_labelled_distinctly_from_unrelated(self, monkeypatch):
        """A broken model and a working one that found nothing must not look alike."""
        candidates = self._candidates()
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
        monkeypatch.setattr(
            consolidate.llm_client,
            "complete",
            lambda prompt, system=None, **kwargs: {
                "text": "maybe these are alike",
                "provider": "test",
            },
        )

        built = consolidate.build_proposals(object(), threshold=0.9)

        assert built["proposals"] == []
        assert built["rejected"][0]["verdict"] == "UNPARSED"

    def test_long_note_bodies_are_truncated_before_reaching_the_model(self, monkeypatch):
        """An unbounded prompt silently overflows the local 8b context."""
        candidates = [
            {
                "id": "note-long",
                "title": "Long note",
                "content": "x" * 9000,
                "embedding": b"a",
            },
            {"id": "note-b", "title": "Short note", "content": "short body", "embedding": b"b"},
        ]
        monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: candidates)
        monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])

        calls = []

        def fake_complete(prompt, system=None, **kwargs):
            calls.append(prompt)
            return {"text": "VERDICT: UNRELATED\nREASON: n/a.", "provider": "test"}

        monkeypatch.setattr(consolidate.llm_client, "complete", fake_complete)

        consolidate.build_proposals(object(), threshold=0.9)

        prompt = calls[0]
        assert prompt.count("x") == consolidate._ADJUDICATION_NOTE_CHAR_CAP
        assert "[truncated for adjudication]" in prompt
        # The short note is untouched — the cap is per note, not per prompt.
        assert "short body" in prompt

    def test_contradictions_are_rendered_for_a_human_not_silently_dropped(self, monkeypatch):
        self._stub_cluster(monkeypatch, self._candidates(), "CONTRADICTION")
        built = consolidate.build_proposals(object(), threshold=0.9)

        md = consolidate.render_proposal_markdown(
            built["proposals"], 0.9, note_count=2, rejected=built["rejected"]
        )

        assert "Contradictions — needs human review" in md
        assert "`note-a`, `note-b`" in md
        assert "Test adjudication." in md
        # "nothing found" and "found some, merged none" must read differently.
        assert "No clusters found" not in md
        assert "No merges proposed" in md

    def test_empty_run_still_reports_no_clusters_found(self, monkeypatch):
        md = consolidate.render_proposal_markdown([], 0.9, note_count=0, rejected=[])

        assert "No clusters found" in md
        assert "No merges proposed" not in md


class TestAgentIdentityExclusion:
    """Notes tagged `agent-identity` are near-identical on purpose — one naming
    convention describing several distinct agents — so clustering must never
    propose merging them. Regression test for the 2026-08-03 proposal that
    would have collapsed four distinct agents' identity notes into one; see
    memory 70aa52f9392f.
    """

    def test_identity_tagged_notes_never_cluster_with_each_other(self, isolated_env):
        memory_save(
            "Agent identity convention: Claude Code on the desktop operates under Desktop_Agent1.",
            title="Agent label: Desktop_Agent1",
            tags=["fact", "agent-identity"],
        )
        memory_save(
            "Agent identity convention: Claude Code on the laptop operates under Laptop_Agent1.",
            title="Agent label: Laptop_Agent1",
            tags=["fact", "agent-identity"],
        )

        res = generate_proposal(write=False)

        if res["has_embeddings"]:
            for cluster in res["proposals"]:
                titles = {cluster["survivor"]["title"]} | {
                    n["title"] for n in cluster["superseded"]
                }
                assert not titles.issuperset(
                    {"Agent label: Desktop_Agent1", "Agent label: Laptop_Agent1"}
                ), "agent-identity notes must never be clustered together"

    def test_identity_tagged_note_does_not_block_unrelated_notes_from_clustering(
        self, isolated_env
    ):
        # The exclusion must be scoped to identity notes, not disable clustering
        # globally — two ordinary near-duplicates should still cluster.
        memory_save(
            "Agent identity convention: Claude Code on the desktop operates under Desktop_Agent1.",
            title="Agent label: Desktop_Agent1",
            tags=["fact", "agent-identity"],
        )
        memory_save(
            "The deployment uses a blue-green rollout strategy for zero downtime releases.",
            title="Deploy A",
        )
        memory_save(
            "The deployment uses a blue-green rollout strategy for zero-downtime releases.",
            title="Deploy B",
        )

        res = generate_proposal(write=False)

        if res["has_embeddings"]:
            assert res["cluster_count"] >= 1
            assert res["demote_count"] >= 1


class TestApplyRestoreRoundTrip:
    def test_apply_then_restore_returns_to_original_rows(self, isolated_env):
        survivor = memory_save("survivor content here", title="Surv", author="A")
        loser = memory_save("loser content here", title="Lose", author="B")

        before = _dump_notes()
        before_indexes = _index_contents()
        apply_supersede(survivor["id"], [loser["id"]])
        assert _dump_notes() != before  # pointer set
        restore_note(loser["id"])
        assert _dump_notes() == before, "restore must fully reverse apply (row-identical)"
        # Index notes are derived and re-stamped on every rebuild; apply and
        # restore both rebuild them (task #1914), so their content round-trips.
        assert _index_contents() == before_indexes


class TestApplyBatch:
    """apply_batch() — the design doc's 'Markdown Batch Apply' (D9): the

    proposal markdown file itself is the review UI. A human deletes the
    blocks for clusters they reject; apply_batch() runs whatever survives.
    """

    def test_applies_every_command_found(self, isolated_env, tmp_path):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        c = memory_save("c content", title="C")
        d = memory_save("d content", title="D")
        path = tmp_path / "proposal.md"
        path.write_text(
            "# Proposal\n\n"
            "## Cluster 1\n\n```bash\n"
            f"unified-agent-consolidate apply {a['id']} {b['id']}\n"
            "```\n\n"
            "## Cluster 2\n\n```bash\n"
            f"unified-agent-consolidate apply {c['id']} {d['id']}\n"
            "```\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 2
        assert result["applied_count"] == 2
        assert not result["errors"]
        assert memory_get(b["id"])["superseded_by"] == a["id"]
        assert memory_get(d["id"])["superseded_by"] == c["id"]

    def test_deleted_block_is_never_applied(self, isolated_env, tmp_path):
        # Simulates a human reviewing the file and deleting a block they
        # disagree with before running apply-batch.
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        c = memory_save("c content", title="C")
        d = memory_save("d content", title="D")
        path = tmp_path / "proposal.md"
        path.write_text(
            "## Cluster 1\n\n```bash\n"
            f"unified-agent-consolidate apply {a['id']} {b['id']}\n"
            "```\n\n"
            "## Cluster 2 (rejected by reviewer, block removed)\n\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 1
        assert memory_get(b["id"])["superseded_by"] == a["id"]
        assert memory_get(d["id"])["superseded_by"] is None
        assert memory_get(c["id"])["superseded_by"] is None

    def test_one_bad_command_does_not_block_the_rest(self, isolated_env, tmp_path):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        path = tmp_path / "proposal.md"
        path.write_text(
            "```bash\nunified-agent-consolidate apply deadbeefdead nosuchid\n```\n\n"
            f"```bash\nunified-agent-consolidate apply {a['id']} {b['id']}\n```\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 2
        assert result["applied_count"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["survivor_id"] == "deadbeefdead"
        assert memory_get(b["id"])["superseded_by"] == a["id"]

    def test_no_commands_found_is_a_clean_no_op(self, isolated_env, tmp_path):
        path = tmp_path / "empty.md"
        path.write_text("# Proposal\n\nNo clusters found.\n")
        result = apply_batch(str(path))
        assert result == {
            "path": str(path),
            "commands_found": 0,
            "applied": [],
            "applied_count": 0,
            "errors": [],
            "ignored_unfenced": 0,
        }

    def test_apply_line_outside_a_bash_fence_is_ignored(self, isolated_env, tmp_path):
        # Task #2012: a command-shaped line in prose (a rendered reason, a
        # title, a pasted note) is not a proposal block and never runs.
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        c = memory_save("c content", title="C")
        d = memory_save("d content", title="D")
        path = tmp_path / "proposal.md"
        path.write_text(
            f"- **Reason:** looks fine\nunified-agent-consolidate apply {c['id']} {d['id']}\n\n"
            f"```\nunified-agent-consolidate apply {d['id']} {c['id']}\n```\n\n"
            f"```bash\nunified-agent-consolidate apply {a['id']} {b['id']}\n```\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 1
        assert result["ignored_unfenced"] == 2
        assert memory_get(b["id"])["superseded_by"] == a["id"]
        assert memory_get(c["id"])["superseded_by"] is None
        assert memory_get(d["id"])["superseded_by"] is None

    def test_trailing_whitespace_is_counted_the_same_inside_and_outside_a_fence(
        self, isolated_env, tmp_path
    ):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        path = tmp_path / "proposal.md"
        path.write_text(
            f"```bash\nunified-agent-consolidate apply {a['id']} {b['id']} \t\n```\n\n"
            "unified-agent-consolidate apply loose-a loose-b  \n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 1
        assert result["ignored_unfenced"] == 1
        assert memory_get(b["id"])["superseded_by"] == a["id"]

    def test_a_fence_grouping_several_commands_applies_each(self, isolated_env, tmp_path):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        c = memory_save("c content", title="C")
        d = memory_save("d content", title="D")
        path = tmp_path / "proposal.md"
        path.write_text(
            "```bash\n"
            f"unified-agent-consolidate apply {a['id']} {b['id']}\n"
            f"unified-agent-consolidate apply {c['id']} {d['id']}\n"
            "```\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 2
        assert result["ignored_unfenced"] == 0
        assert memory_get(b["id"])["superseded_by"] == a["id"]
        assert memory_get(d["id"])["superseded_by"] == c["id"]

    def test_a_fence_mixing_prose_and_commands_runs_none(self, isolated_env, tmp_path):
        a = memory_save("a content", title="A")
        b = memory_save("b content", title="B")
        path = tmp_path / "proposal.md"
        path.write_text(
            f"```bash\n# merge these\nunified-agent-consolidate apply {a['id']} {b['id']}\n```\n"
        )
        result = apply_batch(str(path))
        assert result["commands_found"] == 0
        assert result["ignored_unfenced"] == 1
        assert memory_get(b["id"])["superseded_by"] is None

    def test_cli_reports_lines_it_did_not_run(self, isolated_env, tmp_path, monkeypatch, capsys):
        path = tmp_path / "proposal.md"
        path.write_text("unified-agent-consolidate apply loose-a loose-b\n")
        monkeypatch.setattr("sys.argv", ["unified-agent-consolidate", "apply-batch", str(path)])
        consolidate.main()
        err = capsys.readouterr().err
        assert "1 apply line(s) were not run" in err


class TestProposalRenderingIsOneLine:
    def test_reasons_and_titles_cannot_add_apply_lines(self):
        injected = "x\n```bash\nunified-agent-consolidate apply evil-a evil-b\n```"
        markdown = consolidate.render_proposal_markdown(
            [
                {
                    "survivor": {"id": "s-1", "title": injected},
                    "superseded": [{"id": "s-2", "title": injected, "similarity": 0.95}],
                    "auto_apply": {"eligible": False, "reason": injected},
                }
            ],
            threshold=0.9,
            note_count=2,
            stale_notes=[
                {
                    "id": "st-1",
                    "title": injected,
                    "salience": 0.9,
                    "age_months": 7,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
            rejected=[
                {
                    "verdict": "CONTRADICTION",
                    "note_ids": ["r-1"],
                    "reason": injected,
                    "provider": "fake",
                },
                {
                    "verdict": "UNRELATED",
                    "note_ids": ["r-2"],
                    "reason": injected,
                    "provider": "fake",
                },
            ],
        )
        assert consolidate._APPLY_CMD_RX.findall(markdown) == [("s-1", " s-2")]
        assert consolidate.fenced_apply_commands(markdown) == [("s-1", " s-2")]


class TestMemoryRestoreTool:
    """memory_restore — the MCP-facing lift of the CLI restore (task #80.1)."""

    def _demote(self):
        survivor = memory_save("survivor content here", title="Surv", author="A")
        loser = memory_save("loser content here", title="Lose", author="B", tags=["restoretag"])
        apply_supersede(survivor["id"], [loser["id"]])
        return survivor, loser

    def test_restore_reverses_apply_row_identical(self, isolated_env):
        from unified_agent.tools.memory import memory_restore

        _, loser = self._demote()
        before_demoted = _dump()
        out = memory_restore(loser["id"])
        assert out["restored"] is True
        assert out["was_superseded_by"]
        assert _row(loser["id"])["superseded_by"] is None
        assert _dump() != before_demoted

    def test_restored_note_reappears_in_search_list_and_tag_index(self, isolated_env):
        from unified_agent.tools.memory import memory_restore

        _, loser = self._demote()
        assert all(r["id"] != loser["id"] for r in memory_search("loser content")["results"])
        memory_restore(loser["id"])
        assert any(r["id"] == loser["id"] for r in memory_search("loser content")["results"])
        assert any(i["id"] == loser["id"] for i in memory_list(tag="restoretag")["items"])
        # The tool refreshes tag indexes immediately (as the CLI restore does).
        from unified_agent.tools.memory import _tag_index_id

        idx = memory_get(_tag_index_id("restoretag"))
        assert loser["id"] in idx["content"]

    def test_restore_not_superseded_is_noop(self, isolated_env):
        from unified_agent.tools.memory import memory_restore

        note = memory_save("plain note", title="Plain", author="A")
        out = memory_restore(note["id"])
        assert out == {"id": note["id"], "restored": False, "was_superseded_by": None}

    @pytest.mark.parametrize("entry", ["memory_restore", "restore_note"])
    def test_restore_needs_the_owner_or_a_verified_human(self, isolated_env, entry):
        """Restore is the reverse of apply_supersede and takes the same owner rule."""
        from unified_agent import config
        from unified_agent.tools import memory as memory_tools

        restore = (
            memory_tools.memory_restore if entry == "memory_restore" else consolidate.restore_note
        )
        survivor = memory_save("survivor content here", title="Surv", author="Desktop_agent3")
        loser = memory_save("loser content here", title="Lose", author="Desktop_agent3")
        apply_supersede(survivor["id"], [loser["id"]])
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(ToolError, match="note owner or a verified human"):
                restore(loser["id"])
        finally:
            config.agent_identity_var.reset(token)
        assert _row(loser["id"])["superseded_by"] == survivor["id"]
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            assert restore(loser["id"])["restored"] is True
        finally:
            config.agent_identity_var.reset(token)

    def test_restore_missing_and_index_ids_raise(self, isolated_env):
        from unified_agent.tools.memory import memory_restore

        with pytest.raises(ToolError, match="no memory note"):
            memory_restore("nope00000000")
        with pytest.raises(ToolError, match="machine-managed"):
            memory_restore("masterindex00")


class TestVectorizedSimilarity:
    """cluster_duplicates' pairwise cosine step (task #997).

    The nightly sleep pass's active candidate pool (~2,750 notes as of
    2026-09-14) made the per-pair Python loop over `cosine_similarity_bytes`
    the dominant cost of a run — 3.78M pairs at that size, growing with the
    corpus. `_pairwise_cosine_matrix` computes the same cosine values as one
    vectorized matrix multiply. These tests pin numeric equivalence against
    the existing per-pair function and prove clustering behavior (threshold,
    agent-identity exclusion, Jaccard filter) is unchanged.
    """

    @staticmethod
    def _vec(values: list[float]) -> bytes:
        import numpy as np

        return np.array(values, dtype=np.float32).tobytes()

    def test_matches_cosine_similarity_bytes_pairwise(self):
        from unified_agent.consolidate import _pairwise_cosine_matrix
        from unified_agent.embeddings import cosine_similarity_bytes

        vectors = [
            self._vec([1.0, 0.0, 0.0]),
            self._vec([1.0, 0.0, 0.0]),  # identical -> 1.0
            self._vec([0.0, 1.0, 0.0]),  # orthogonal -> 0.0
            self._vec([-1.0, 0.0, 0.0]),  # opposite -> -1.0
            self._vec([3.0, 4.0, 0.0]),  # arbitrary, non-unit
        ]

        matrix = _pairwise_cosine_matrix(vectors)

        for i in range(len(vectors)):
            for j in range(len(vectors)):
                expected = cosine_similarity_bytes(vectors[i], vectors[j])
                assert matrix[i][j] == pytest.approx(expected, abs=1e-5), (i, j)

    def test_zero_vector_is_zero_similarity_to_everything_including_itself(self):
        from unified_agent.consolidate import _pairwise_cosine_matrix

        vectors = [self._vec([0.0, 0.0, 0.0]), self._vec([1.0, 0.0, 0.0])]

        matrix = _pairwise_cosine_matrix(vectors)

        assert matrix[0][0] == 0.0
        assert matrix[0][1] == 0.0
        assert matrix[1][0] == 0.0
        assert matrix[1][1] == pytest.approx(1.0)

    def test_empty_input_returns_empty_matrix_without_error(self):
        from unified_agent.consolidate import _pairwise_cosine_matrix

        matrix = _pairwise_cosine_matrix([])

        assert len(matrix) == 0

    def _candidate(self, note_id: str, embedding: bytes, tags: list[str] | None = None) -> dict:
        return {
            "id": note_id,
            "title": f"Deploy note {note_id}",
            "content": "The deployment uses a blue-green rollout strategy",
            "updated_at": "2026-09-01T00:00:00+00:00",
            "access_count": 0,
            "salience": 0.5,
            "content_len": 40,
            "embedding": embedding,
            "tags": tags or [],
        }

    def test_cluster_duplicates_still_groups_near_identical_embeddings(self):
        unit = self._vec([1.0, 0.0, 0.0])
        near = self._vec([0.999, 0.001, 0.0])
        far = self._vec([0.0, 1.0, 0.0])
        candidates = [
            self._candidate("a", unit),
            self._candidate("b", near),
            self._candidate("c", far),
        ]

        clusters = consolidate.cluster_duplicates(candidates, threshold=0.9)

        assert len(clusters) == 1
        assert {n["id"] for n in clusters[0]} == {"a", "b"}

    def test_cluster_duplicates_still_excludes_agent_identity_pairs(self):
        unit = self._vec([1.0, 0.0, 0.0])
        candidates = [
            self._candidate("a", unit, tags=["agent-identity"]),
            self._candidate("b", unit, tags=["agent-identity"]),
        ]

        clusters = consolidate.cluster_duplicates(candidates, threshold=0.5)

        assert clusters == []

    def test_cluster_duplicates_handles_a_large_candidate_pool(self):
        """Regression guard for the O(n^2) blow-up this task fixes.

        200 candidates is far below the new default ceilings but was already
        enough to make the un-vectorized double loop noticeably slow in local
        testing; this asserts correctness (not a timing budget, to avoid CI
        flakiness) at a size the old per-pair path was never exercised at in
        this suite.
        """
        import numpy as np

        rng = np.random.default_rng(seed=0)
        candidates = []
        for i in range(200):
            base = rng.normal(size=3).astype(np.float32)
            candidates.append(self._candidate(f"n{i}", base.tobytes()))
        # Force one unambiguous duplicate pair.
        dup_vec = self._vec([5.0, 5.0, 5.0])
        candidates.append(self._candidate("dup-1", dup_vec))
        candidates.append(self._candidate("dup-2", dup_vec))

        clusters = consolidate.cluster_duplicates(candidates, threshold=0.9999)

        dup_clusters = [c for c in clusters if {"dup-1", "dup-2"} <= {n["id"] for n in c}]
        assert len(dup_clusters) == 1

    def test_cluster_duplicates_refuses_cross_model_cosine(self):
        unit = self._vec([1.0, 0.0, 0.0])
        candidates = [
            {**self._candidate("a", unit), "model": "BAAI/bge-small-en-v1.5"},
            {**self._candidate("b", unit), "model": "other/model"},
        ]

        assert consolidate.cluster_duplicates(candidates, threshold=0.5) == []

    def test_cluster_duplicates_treats_null_model_as_the_default(self):
        unit = self._vec([1.0, 0.0, 0.0])
        candidates = [
            {**self._candidate("a", unit), "model": None},
            {**self._candidate("b", unit), "model": "BAAI/bge-small-en-v1.5"},
            {**self._candidate("c", unit), "model": "other/model"},
        ]

        clusters = consolidate.cluster_duplicates(candidates, threshold=0.9)

        assert len(clusters) == 1
        assert {n["id"] for n in clusters[0]} == {"a", "b"}
