"""Task #965: the auto-apply safety gate for superseded_by merges.

Consolidation's contract is that a merge is explicit and reversible. Auto-apply
keeps the reversible half untouched — only the `superseded_by` pointer is ever
written, and `restore_note` still fully reverses it — but it removes the human
from the explicit half, so the gate is deliberately much stricter than the
propose threshold.

`auto_apply_verdict` is pure, so most of this exercises the policy directly on
dicts. The integration tests then pin the two properties that matter most:
auto-apply is OFF unless explicitly enabled, and `generate_proposal` stays
read-only regardless.
"""

import pytest

from unified_agent import consolidate


def _note(note_id: str, **over) -> dict:
    base = {
        "id": note_id,
        "title": f"note {note_id}",
        "content": "body",
        "tags": ["project:unified-agent"],
        "author": "Desktop_Agent1",
        "salience": 0.5,
        "access_count": 0,
        "feedback_helpful": 0,
        "feedback_not_helpful": 0,
        "feedback_stale": 0,
        "feedback_wrong": 0,
    }
    base.update(over)
    return base


def _verdict(survivor=None, loser=None, similarity=0.99, cluster_size=2, votes=None):
    return consolidate.auto_apply_verdict(
        survivor or _note("a"),
        loser or _note("b"),
        similarity,
        cluster_size,
        votes,
    )


class TestAutoApplyVerdict:
    def test_a_clean_1_to_1_near_identical_pair_is_eligible(self):
        eligible, reason = _verdict()
        assert eligible, reason
        assert reason == "eligible"

    def test_multi_note_clusters_never_auto_apply(self):
        # A 3-note cluster is a judgement call about which of several notes
        # survives — exactly the call a human is for.
        eligible, reason = _verdict(cluster_size=3)
        assert not eligible
        assert "cluster of 3" in reason

    def test_similarity_below_the_auto_threshold_is_held(self):
        eligible, reason = _verdict(similarity=0.93)
        assert not eligible
        assert "similarity 0.9300" in reason

    def test_cross_project_merges_are_held(self):
        eligible, reason = _verdict(loser=_note("b", tags=["project:browser"]))
        assert not eligible
        assert "project:" in reason

    def test_untagged_and_tagged_counts_as_different_projects(self):
        eligible, reason = _verdict(loser=_note("b", tags=[]))
        assert not eligible
        assert "project:" in reason

    def test_different_authors_are_held(self):
        eligible, reason = _verdict(loser=_note("b", author="Desktop_Agent2"))
        assert not eligible
        assert reason == "different authors"

    @pytest.mark.parametrize("which", ["survivor", "loser"])
    def test_high_salience_on_either_side_is_held(self, which):
        kwargs = {which: _note(which[0], salience=0.9)}
        eligible, reason = _verdict(**kwargs)
        assert not eligible
        assert "salience" in reason

    @pytest.mark.parametrize("which", ["survivor", "loser"])
    def test_frequently_accessed_notes_are_held(self, which):
        kwargs = {which: _note(which[0], access_count=50)}
        eligible, reason = _verdict(**kwargs)
        assert not eligible
        assert "accessed" in reason

    @pytest.mark.parametrize(
        "field",
        ["feedback_helpful", "feedback_not_helpful", "feedback_stale", "feedback_wrong"],
    )
    def test_any_human_feedback_holds_the_merge(self, field):
        # Including negative feedback: a note someone flagged as wrong is a note
        # someone looked at, and silently folding it away loses that signal.
        eligible, reason = _verdict(loser=_note("b", **{field: 1}))
        assert not eligible
        assert field in reason

    @pytest.mark.parametrize("vote", ["recalled", "upvoted"])
    def test_recall_votes_hold_the_merge(self, vote):
        eligible, reason = _verdict(votes={"b": {vote: 3}})
        assert not eligible
        assert "recall votes" in reason

    def test_missing_optional_fields_do_not_crash_the_gate(self):
        bare = {"id": "x", "tags": ["project:unified-agent"], "author": "Desktop_Agent1"}
        eligible, reason = consolidate.auto_apply_verdict(bare, dict(bare, id="y"), 0.99, 2, None)
        assert eligible, reason


class TestAutoApplyThreshold:
    def test_default_is_far_above_the_propose_threshold(self):
        assert consolidate.auto_apply_threshold() == 0.98
        assert consolidate.auto_apply_threshold() > consolidate.DEFAULT_THRESHOLD

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_THRESHOLD", "0.995")
        assert consolidate.auto_apply_threshold() == 0.995

    def test_it_can_never_be_lowered_to_or_below_the_propose_threshold(self, monkeypatch):
        # Otherwise "auto" would mean "everything that gets proposed", which is
        # the opposite of a gate.
        monkeypatch.setenv("UA_CONSOLIDATE_THRESHOLD", "0.90")
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_THRESHOLD", "0.50")
        assert consolidate.auto_apply_threshold() == 0.90

    @pytest.mark.parametrize("bad", ["not-a-number", "0", "-1", "1.5", ""])
    def test_invalid_values_fall_back_to_the_default(self, monkeypatch, bad):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_THRESHOLD", bad)
        assert consolidate.auto_apply_threshold() == 0.98


class TestAutoApplyEnabled:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_CONSOLIDATE_AUTO_APPLY", raising=False)
        assert consolidate.auto_apply_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_accepted_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", value)
        assert consolidate.auto_apply_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_everything_else_is_off(self, monkeypatch, value):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", value)
        assert consolidate.auto_apply_enabled() is False


class TestAutoApplySafeMerges:
    @staticmethod
    def _proposal(eligible=True, reason="eligible"):
        return {
            "survivor": {"id": "keep", "title": "keep"},
            "superseded": [{"id": "drop", "title": "drop", "similarity": 0.99}],
            "auto_apply": {"eligible": eligible, "reason": reason},
        }

    def test_refuses_entirely_when_the_env_flag_is_unset(self, monkeypatch):
        monkeypatch.delenv("UA_CONSOLIDATE_AUTO_APPLY", raising=False)
        called = []
        monkeypatch.setattr(consolidate, "apply_supersede", lambda *a, **k: called.append(a) or {})
        res = consolidate.auto_apply_safe_merges([self._proposal()])
        assert res["enabled"] is False
        assert res["applied"] == []
        assert len(res["skipped"]) == 1
        assert "UA_CONSOLIDATE_AUTO_APPLY" in res["skipped"][0]["reason"]
        assert called == [], "must not write anything while disabled"

    def test_dry_run_reports_without_writing(self, monkeypatch):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
        called = []
        monkeypatch.setattr(consolidate, "apply_supersede", lambda *a, **k: called.append(a) or {})
        res = consolidate.auto_apply_safe_merges([self._proposal()], dry_run=True)
        assert res["enabled"] is True
        assert [e["survivor"] for e in res["applied"]] == ["keep"]
        assert called == [], "dry run must not write"

    def test_applies_only_eligible_proposals(self, monkeypatch):
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
        called = []
        monkeypatch.setattr(
            consolidate, "apply_supersede", lambda s, ids, actor=None: called.append((s, ids))
        )
        res = consolidate.auto_apply_safe_merges(
            [self._proposal(), self._proposal(eligible=False, reason="different authors")],
            actor="Desktop_Agent1",
        )
        assert called == [("keep", ["drop"])]
        assert len(res["applied"]) == 1
        assert res["skipped"][0]["reason"] == "different authors"

    def test_a_proposal_with_no_verdict_is_skipped_not_applied(self, monkeypatch):
        # Fail closed: a proposal built by an older code path has no verdict,
        # and absence of a verdict must never read as permission.
        monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
        called = []
        monkeypatch.setattr(consolidate, "apply_supersede", lambda *a, **k: called.append(a) or {})
        stale = {
            "survivor": {"id": "keep", "title": "keep"},
            "superseded": [{"id": "drop", "title": "drop", "similarity": 0.99}],
        }
        res = consolidate.auto_apply_safe_merges([stale])
        assert called == []
        assert res["applied"] == []
        assert "no verdict recorded" in res["skipped"][0]["reason"]
