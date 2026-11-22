"""Cluster-judge consistency check (task #1778).

The 2026-10-01 sleep pass filed 84 of 132 clusters under Contradictions, several
with a rationale that argued the opposite of the label. A CONTRADICTION verdict
whose own reason plainly denies any conflict is flagged: it stays CONTRADICTION
(annotate-only), carries the matched `consistency_flag`, and the reports mark it
"judge's reason disputes this label". It is never proposed as a merge.

The flag is an allowlist and only fires when it is confident: a reason that is
long, has a second clause, scopes or contrasts anything, or carries a negation
outside the matched phrase is not flagged. Recall is traded for precision on
purpose. Moving a flagged cluster to NEEDS_REVIEW is behind
`_CONSISTENCY_FLAG_DOWNGRADES` (off) and deferred to a follow-up.
"""

import json
import time

import pytest

from unified_agent import consolidate, sleep_pass

# Rationales from 00 - Inbox/Sleep-Pass-20261001-093729.md filed under
# Contradictions: clusters 9846126b6aa6/a13d8ec16f31 and 450c3c5338d2/4faf4668ddf2.
QUOTED_REASONS = [
    "The second note updates the first one's figure, suggesting a revision and not a "
    "contradiction.",
    "Both notes contain the same information but differ in format.",
]

# Short, single-clause reasons that plainly deny a conflict or call the notes
# the same.
PLAIN_DENIALS = [
    "There is no substantive conflict between the notes.",
    "The notes are not in conflict.",
    "These notes do not conflict.",
    "Both notes contain identical content.",
    "The notes are duplicates.",
    "The notes differ only in wording.",
]

GENUINE_CONTRADICTIONS = [
    "The notes state different rate limits (60 vs 600 per minute) that cannot both be true.",
    "This is not a duplicate: one note says the hub port is 8765 and the other 9000.",
    "The notes are not the same information; they disagree on the deploy host.",
    "conflicting rate limit numbers",
    # Partial agreement: a shared detail does not cancel a conflicting one.
    "Both notes give the same deployment host, but one says the release is Monday "
    "and the other says Friday.",
    "The notes contain the same facts about the port, but contradict each other on the host.",
    "The notes are consistent about the host, but disagree on the release day.",
    "The second note is a revised version, yet the two notes differ on the rate limit.",
    "They are not in conflict about the port, but they conflict on the deploy host.",
    "The notes are not in factual conflict on the port; the hosts are 60 vs 600.",
    "There is no doubt the notes conflict on the deploy host.",
    "The notes are not fully consistent: A gives port 8765; B gives port 9000.",
    "The notes aren't compatible: one is 60 per minute and one is 600.",
    "Both notes are consistent about the host, but A requires human approval and B "
    "permits execution without human approval.",
    "The notes are consistent regarding the port, though A says approval is required "
    "and B says it is not.",
    "Both notes are compatible; A targets staging and B targets production.",
    "Both notes contain the same information about the host, but A requires approval "
    "and B allows unattended runs.",
    # Round-3 independent review: each was downgraded by the earlier blocklist.
    "The notes are not in conflict about the host, but A requires approval and B "
    "allows unattended runs.",
    "Both notes cover the deploy step, but A requires approval and B does not for "
    "the identical operation.",
    "The two notes are not even remotely identical.",
    "The notes are not conflict-free.",
    # A sameness phrase alongside differing values, with no connective.
    "Both notes duplicate the port setting with one at 60 and the other at 600.",
    # Round-4 independent review: a phrase plus unrecognised residue. Every word
    # left after the phrase must be filler, or the verdict stands.
    "The notes differ in wording from mandatory approval to optional approval.",
    "The notes differ in wording: approval required vs optional.",
    "The notes are duplicates except the approval step.",
    # Round-5 independent review: an elliptical subset claim ("the second two
    # are" in conflict) is not a denial for the cluster.
    "The first two notes are not in conflict, the second two are.",
    # Round-8 independent review: a restrictive adverb scopes the denial.
    "Only the same facts are not in conflict.",
    # Round-7 independent review: a denial for one note, and an elliptical
    # claim that the other note is in conflict.
    "The first note is not in conflict, the second is, and only the second.",
    "The first note is not in conflict and the second is.",
    "Not in conflict, the second note is.",
    # Known limitation, fails safe: two claims that are in fact identical, phrased
    # "one note says X and the other says X", keep CONTRADICTION. Not worth a
    # value comparison: a human reviews it either way.
    "The notes are not a contradiction; one note says port 8765 and the other says port 8765.",
]

# Deliberate recall trade-off: these deny a conflict, but with a second clause,
# a negation outside an allowlisted phrase, or wording off the list, so they
# keep CONTRADICTION. Each was downgraded by the earlier heuristic.
RECALL_TRADE_OFFS = [
    "The notes are not in factual conflict; each describes a different feature.",
    "The two notes do not constitute a contradiction; one is a summary of the other.",
    "These notes don't represent a genuine contradiction.",
    "The notes are not a contradiction; one says the same information as the other.",
    "Both notes are consistent.",
    # Round 7: an ordinal after the phrase could start a second subject.
    "The second note restates the first.",
    "The notes are fully consistent; they restate the same port.",
]

NOTES = [
    {"id": "9846126b6aa6", "title": "A", "content": "x", "embedding": b"a"},
    {"id": "a13d8ec16f31", "title": "B", "content": "y", "embedding": b"b"},
]


def _judge(monkeypatch, verdict: str, reason: str, structured: bool = True) -> None:
    text = (
        json.dumps({"verdict": verdict, "reason": reason})
        if structured
        else f"VERDICT: {verdict}\nREASON: {reason}"
    )
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": text, "provider": "ollama"},
    )


@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize("reason", QUOTED_REASONS)
def test_the_quoted_rationales_are_flagged_but_stay_contradictions(monkeypatch, reason, structured):
    _judge(monkeypatch, "CONTRADICTION", reason, structured)
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "judge_verdict" not in review
    assert review["consistency_flag"]
    assert review["reason"] == reason
    assert review["note_ids"] == ["9846126b6aa6", "a13d8ec16f31"]


@pytest.mark.parametrize("reason", PLAIN_DENIALS)
def test_a_plain_denial_is_flagged(monkeypatch, reason):
    _judge(monkeypatch, "CONTRADICTION", reason)
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert review["consistency_flag"]


@pytest.mark.parametrize("reason", RECALL_TRADE_OFFS)
def test_a_complex_denial_keeps_the_verdict(reason):
    assert consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=2) is None


def test_an_over_long_reason_keeps_the_verdict():
    reason = "The notes are duplicates " + "x " * consolidate._DOWNGRADE_MAX_CHARS
    assert consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=2) is None


@pytest.mark.parametrize(
    "unit", ["not identical. ", "not a duplicate; ", "x conflict ", "same information "]
)
def test_the_scan_is_linear_in_the_reason(unit):
    # Independent review: an earlier version re-scanned the whole prefix per
    # cue, so 4000 repeats took seconds. Generous bound, not a benchmark.
    for repeats in (4000, consolidate._DOWNGRADE_MAX_CHARS // len(unit)):
        reason = unit * repeats
        started = time.perf_counter()
        consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=2)
        assert time.perf_counter() - started < 0.5, (unit, repeats)


def test_the_cue_is_returned_on_one_line():
    flag = consolidate.verdict_consistency_flag(
        "CONTRADICTION", "The notes are not \t a contradiction.", note_count=2
    )
    assert flag == "not a contradiction"


def test_a_multiline_reason_keeps_the_verdict():
    reason = "These notes are not\na contradiction."
    assert consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=2) is None


THREE_NOTES = [*NOTES, {"id": "c0ffee000003", "title": "C", "content": "z", "embedding": b"c"}]


@pytest.mark.parametrize(
    "reason",
    ["The notes are duplicates.", "The first two notes are not in conflict, the second two are."],
)
def test_only_a_pair_is_ever_downgraded(monkeypatch, reason):
    # Round-5 independent review: a reason about a larger cluster can describe
    # agreement among some of its notes only.
    _judge(monkeypatch, "CONTRADICTION", reason)
    review = consolidate._review_cluster(THREE_NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "consistency_flag" not in review
    assert consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=3) is None


@pytest.mark.parametrize(
    "reply",
    [
        # Round-5 independent review: the legacy parser keeps only the first
        # REASON line, so a conflict on a continuation line went unread.
        "VERDICT: CONTRADICTION\nREASON: The notes are not in conflict.\n"
        "The third contradicts their approval requirement.",
        "The second note contradicts the first.\nVERDICT: CONTRADICTION\n"
        "REASON: The notes are not in conflict.",
        "VERDICT: CONTRADICTION\nREASON: The notes are not in conflict.\n\nBut A requires approval.",
    ],
)
def test_a_legacy_reply_with_unread_text_keeps_the_verdict(monkeypatch, reply):
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": reply, "provider": "ollama"},
    )
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "consistency_flag" not in review


@pytest.mark.parametrize(
    "reply",
    [
        # Round-6 independent review: the parser ignores extra fields, so a
        # conflict could sit unread. (The duplicate-key case is below.)
        '{"verdict": "CONTRADICTION", "reason": "The notes are not in conflict.", '
        '"caveat": "A requires approval and B permits unattended execution."}',
        '{"verdict": "CONTRADICTION", "reason": ["The notes are not in conflict."]}',
        "VERDICT: CONTRADICTION because A requires approval\n"
        "REASON: The notes are not in conflict.",
    ],
)
def test_a_reply_with_unread_fields_keeps_the_verdict(monkeypatch, reply):
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": reply, "provider": "ollama"},
    )
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "consistency_flag" not in review


def test_a_reply_with_a_duplicate_reason_key_is_unparsed(monkeypatch):
    # Round-6 independent review: json.loads keeps the last duplicate key, so a
    # conflict could sit unread under a second "reason". PR #1213 review: a
    # duplicate key now makes the whole reply unparseable, which is stricter
    # than keeping CONTRADICTION and still never a flag, merge or downgrade.
    reply = (
        '{"verdict": "CONTRADICTION", "reason": "A requires approval; B permits '
        'unattended execution.", "reason": "The notes are not in conflict."}'
    )
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": reply, "provider": "ollama"},
    )
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == consolidate.VERDICT_UNPARSED
    assert "consistency_flag" not in review


@pytest.mark.parametrize("spaces", [32_000, consolidate._REPLY_MAX_CHARS - 80])
def test_the_reply_check_is_linear(spaces):
    # Round-6 independent review: a backtracking structural regex took ~5 s on
    # this reply at 32k spaces. Generous bound, not a benchmark.
    reply = (
        "VERDICT: CONTRADICTION\nREASON: The notes are duplicates."
        + " " * spaces
        + "\nA requires approval but B does not."
    )
    started = time.perf_counter()
    assert consolidate.reply_fully_read(reply) is False
    assert time.perf_counter() - started < 0.5


def test_a_plain_legacy_reply_is_fully_read():
    assert consolidate.reply_fully_read(
        "  VERDICT: CONTRADICTION\n\n  REASON: The notes are not in conflict.\n"
    )
    assert consolidate.reply_fully_read(
        '{"verdict": "CONTRADICTION", "reason": "The notes are not in conflict."}'
    )


def test_annotate_only_is_the_default():
    assert consolidate._CONSISTENCY_FLAG_DOWNGRADES is False


def test_the_deferred_downgrade_switch_still_works(monkeypatch):
    monkeypatch.setattr(consolidate, "_CONSISTENCY_FLAG_DOWNGRADES", True)
    _judge(monkeypatch, "CONTRADICTION", QUOTED_REASONS[0])
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == consolidate.VERDICT_NEEDS_REVIEW
    assert review["judge_verdict"] == "CONTRADICTION"
    assert review["consistency_flag"] == "not a contradiction"


def test_a_reason_ending_in_a_bare_auxiliary_keeps_the_verdict():
    reason = "The notes are duplicates, they are."
    assert consolidate.verdict_consistency_flag("CONTRADICTION", reason, note_count=2) is None


@pytest.mark.parametrize("reason", GENUINE_CONTRADICTIONS)
def test_a_genuine_contradiction_is_kept(monkeypatch, reason):
    _judge(monkeypatch, "CONTRADICTION", reason)
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "consistency_flag" not in review


@pytest.mark.parametrize("verdict", ["DUPLICATE", "UNRELATED"])
def test_other_verdicts_are_not_rewritten(monkeypatch, verdict):
    _judge(monkeypatch, verdict, QUOTED_REASONS[1])
    assert consolidate._review_cluster(NOTES)["verdict"] == verdict


def test_a_flagged_contradiction_is_marked_and_never_a_merge(monkeypatch):
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: list(NOTES))
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
    _judge(monkeypatch, "CONTRADICTION", QUOTED_REASONS[0])
    built = consolidate.build_proposals(object(), threshold=0.9)
    assert built["proposals"] == []
    [rejected] = built["rejected"]
    assert rejected["verdict"] == "CONTRADICTION"
    markdown = consolidate.render_proposal_markdown([], 0.9, 2, [], built["rejected"])
    section = markdown.split("## Contradictions", 1)[1].split("\n## ", 1)[0]
    marker = 'judge\'s reason disputes this label: "not a contradiction"'
    assert f"*{consolidate.inline_code(marker)}*" in section
    assert "unified-agent-consolidate apply " not in markdown
    assert consolidate.fenced_apply_commands(markdown) == []


def test_a_flagged_contradiction_is_never_auto_applied(monkeypatch):
    # Auto-apply only ever sees build_proposals' merge proposals, which a
    # contradiction (flagged or not) never becomes.
    monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: list(NOTES))
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
    _judge(monkeypatch, "CONTRADICTION", QUOTED_REASONS[1])
    built = consolidate.build_proposals(object(), threshold=0.9)
    assert built["rejected"][0]["consistency_flag"]
    result = consolidate.auto_apply_safe_merges(built["proposals"], dry_run=True)
    assert result["applied"] == [] and result["skipped"] == []


def test_the_marker_renders_on_one_line():
    review = {"verdict": "CONTRADICTION", "consistency_flag": "not a\ncontradiction"}
    marker = consolidate.disputed_label_marker(review)
    assert marker == 'judge\'s reason disputes this label: "not a contradiction"'
    assert (
        consolidate.disputed_label_marker({"verdict": "UNRELATED", "consistency_flag": "x"}) is None
    )


def test_sleep_pass_report_marks_disputed_contradictions():
    reviews = [
        {
            "verdict": "CONTRADICTION",
            "consistency_flag": "the same information",
            "cluster": [{"id": "450c3c5338d2"}, {"id": "4faf4668ddf2"}],
            "reason": QUOTED_REASONS[1],
        },
        {
            "verdict": "CONTRADICTION",
            "cluster": [{"id": "rate-a"}, {"id": "rate-b"}],
            "reason": GENUINE_CONTRADICTIONS[0],
        },
    ]
    markdown = sleep_pass.render_sleep_pass_markdown(
        reviews, digest=None, prune_candidates=[], threshold=0.6
    )
    assert "**2** contradiction (**1** disputed by the judge's reason)" in markdown
    assert "needs review" not in markdown
    contradictions = markdown.split("### Contradictions", 1)[1].split("###", 1)[0]
    flagged, genuine = contradictions.split("- `rate-a`", 1)
    assert "`450c3c5338d2`, `4faf4668ddf2`" in flagged
    marker = 'judge\'s reason disputes this label: "the same information"'
    assert f"  - {consolidate.inline_code(marker)}" in flagged
    assert "disputes" not in genuine


def test_sleep_pass_counts_disputed_contradictions(monkeypatch, tmp_path):
    clusters = [
        [
            {"id": f"note-{i}-a", "title": "A", "content": "x"},
            {"id": f"note-{i}-b", "title": "B", "content": "y"},
        ]
        for i in range(2)
    ]
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: [])
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda candidates, threshold: clusters)
    reasons = iter([QUOTED_REASONS[0], GENUINE_CONTRADICTIONS[0]])
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {
            "text": json.dumps({"verdict": "CONTRADICTION", "reason": next(reasons)}),
            "provider": "ollama",
        },
    )
    result = sleep_pass.run_sleep_pass(write=False)
    assert result["contradiction_count"] == 2
    assert result["contradictions_disputed"] == 1
    assert result["telemetry"]["contradictions_disputed"] == 1
    assert result["needs_review_count"] == 0
    assert "judge's reason disputes this label" in result["markdown"]
