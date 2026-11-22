"""Structured self-consistency downgrade for the cluster judge (task #2022).

Task #1778 (PR #1136) shipped annotate-only: a CONTRADICTION whose free-text
reason reads as a denial keeps its verdict and only gains a marker, because
eight review rounds showed that parsing prose lets a genuine conflict hide.
This follow-up changes the judge contract instead. The judge may also return
``self_consistent`` (a JSON boolean) and ``conflicting_attributes`` (a JSON
array of strings). A cluster moves from CONTRADICTION to NEEDS_REVIEW only when
it is a pair, the reply is exactly those four keys with no duplicates, the
verdict is CONTRADICTION, ``self_consistent`` is the boolean ``true`` and
``conflicting_attributes`` is an empty list. Anything else keeps CONTRADICTION.
The prose check never downgrades by itself.
"""

import json

import pytest

from unified_agent import consolidate, llm_client, sleep_pass

NOTES = [
    {"id": "9846126b6aa6", "title": "A", "content": "x", "embedding": b"a"},
    {"id": "a13d8ec16f31", "title": "B", "content": "y", "embedding": b"b"},
]
THREE_NOTES = [*NOTES, {"id": "c0ffee000003", "title": "C", "content": "z", "embedding": b"c"}]

REASON = "The second note updates the first one's figure."
DOWNGRADE = {
    "verdict": "CONTRADICTION",
    "reason": REASON,
    "self_consistent": True,
    "conflicting_attributes": [],
}


def _reply(monkeypatch, text: str) -> None:
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": text, "provider": "ollama"},
    )


def _with(**fields) -> str:
    return json.dumps({**DOWNGRADE, **fields})


def _without(key: str) -> str:
    return json.dumps({k: v for k, v in DOWNGRADE.items() if k != key})


def test_a_pair_judged_self_consistent_with_no_conflicts_needs_review(monkeypatch):
    _reply(monkeypatch, json.dumps(DOWNGRADE))
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == consolidate.VERDICT_NEEDS_REVIEW
    assert review["judge_verdict"] == "CONTRADICTION"
    assert review["downgrade_reason"] == consolidate.STRUCTURED_DOWNGRADE_REASON
    assert "self_consistent=true" in review["downgrade_reason"]
    assert review["reason"] == REASON
    assert review["note_ids"] == ["9846126b6aa6", "a13d8ec16f31"]
    # The structured fields decide; the prose check is not consulted.
    assert "consistency_flag" not in review


def test_whitespace_and_key_order_do_not_matter(monkeypatch):
    text = (
        '\n  {"conflicting_attributes": [ ], "self_consistent": true,\n'
        f'   "reason": "{REASON}", "verdict": "CONTRADICTION"}}  \n'
    )
    _reply(monkeypatch, text)
    assert consolidate._review_cluster(NOTES)["verdict"] == consolidate.VERDICT_NEEDS_REVIEW


def test_a_cluster_of_three_is_never_downgraded(monkeypatch):
    _reply(monkeypatch, json.dumps(DOWNGRADE))
    review = consolidate._review_cluster(THREE_NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "downgrade_reason" not in review and "judge_verdict" not in review


NOT_A_DOWNGRADE = {
    # self_consistent must be the JSON boolean true.
    "self_consistent string true": _with(self_consistent="true"),
    "self_consistent string True": _with(self_consistent="True"),
    "self_consistent 1": _with(self_consistent=1),
    "self_consistent 0": _with(self_consistent=0),
    "self_consistent 1.0": _with(self_consistent=1.0),
    "self_consistent null": _with(self_consistent=None),
    "self_consistent false": _with(self_consistent=False),
    "self_consistent list": _with(self_consistent=[True]),
    "self_consistent nested": _with(self_consistent={"value": True}),
    "self_consistent missing": _without("self_consistent"),
    # conflicting_attributes must be present and an empty list.
    "conflicting_attributes non-empty": _with(conflicting_attributes=["deploy host"]),
    "conflicting_attributes blank entry": _with(conflicting_attributes=[""]),
    "conflicting_attributes nested empty": _with(conflicting_attributes=[[]]),
    "conflicting_attributes string": _with(conflicting_attributes=""),
    "conflicting_attributes string list": _with(conflicting_attributes="[]"),
    "conflicting_attributes object": _with(conflicting_attributes={}),
    "conflicting_attributes null": _with(conflicting_attributes=None),
    "conflicting_attributes false": _with(conflicting_attributes=False),
    "conflicting_attributes missing": _without("conflicting_attributes"),
    "both structured fields missing": json.dumps({"verdict": "CONTRADICTION", "reason": REASON}),
    # Duplicate keys: json.loads keeps the last, so either order could hide a false.
    "duplicate self_consistent true then false": (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": true, '
        '"self_consistent": false, "conflicting_attributes": []}'
    ),
    "duplicate self_consistent false then true": (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": false, '
        '"self_consistent": true, "conflicting_attributes": []}'
    ),
    "duplicate self_consistent true twice": (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": true, '
        '"self_consistent": true, "conflicting_attributes": []}'
    ),
    "duplicate conflicting_attributes": (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": true, '
        '"conflicting_attributes": ["deploy host"], "conflicting_attributes": []}'
    ),
    "duplicate verdict": (
        f'{{"verdict": "DUPLICATE", "verdict": "CONTRADICTION", "reason": "{REASON}", '
        '"self_consistent": true, "conflicting_attributes": []}'
    ),
    "duplicate reason": (
        '{"verdict": "CONTRADICTION", "reason": "A requires approval and B does not.", '
        f'"reason": "{REASON}", "self_consistent": true, "conflicting_attributes": []}}'
    ),
    "duplicate key in a nested object": (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": true, '
        '"conflicting_attributes": [{"a": 1, "a": 2}]}'
    ),
    # Structured fields in the wrong place.
    "fields nested under another key": json.dumps(
        {
            "verdict": "CONTRADICTION",
            "reason": REASON,
            "structured": {"self_consistent": True, "conflicting_attributes": []},
        }
    ),
    "fields nested and at top level": _with(
        structured={"self_consistent": False, "conflicting_attributes": ["host"]}
    ),
    "fields wrapped in a list": json.dumps([DOWNGRADE]),
    "fields wrapped in an object": json.dumps({"result": DOWNGRADE}),
    "differently cased key": json.dumps(
        {
            "verdict": "CONTRADICTION",
            "reason": REASON,
            "Self_Consistent": True,
            "conflicting_attributes": [],
        }
    ),
    "extra key": _with(caveat="A requires approval and B permits unattended execution."),
    "lowercase verdict": _with(verdict="contradiction"),
    "reason not a string": _with(reason=["The notes are not in conflict."]),
    "empty reason": _with(reason=""),
    "text before the object": "Here you go: " + json.dumps(DOWNGRADE),
    "text after the object": json.dumps(DOWNGRADE) + "\nBut A requires approval.",
    "legacy lines": (
        f"VERDICT: CONTRADICTION\nREASON: {REASON}\nSELF_CONSISTENT: true\n"
        "CONFLICTING_ATTRIBUTES: []"
    ),
    "over-long reply": _with(reason="x" * consolidate._REPLY_MAX_CHARS),
}


@pytest.mark.parametrize("text", NOT_A_DOWNGRADE.values(), ids=NOT_A_DOWNGRADE.keys())
def test_a_malformed_or_negative_structured_reply_keeps_contradiction(monkeypatch, text):
    _reply(monkeypatch, text)
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] in {"CONTRADICTION", consolidate.VERDICT_UNPARSED}
    assert "downgrade_reason" not in review and "judge_verdict" not in review
    assert consolidate.structured_self_consistency(text, note_count=2) is False


@pytest.mark.parametrize("verdict", ["DUPLICATE", "UNRELATED"])
def test_only_a_contradiction_is_downgraded(monkeypatch, verdict):
    _reply(monkeypatch, _with(verdict=verdict))
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == verdict
    assert "downgrade_reason" not in review


@pytest.mark.parametrize(
    "reason",
    [
        "The notes are not in conflict.",
        "Both notes contain the same information but differ in format.",
        "The second note updates the first one's figure, suggesting a revision and not a "
        "contradiction.",
    ],
)
@pytest.mark.parametrize(
    "structured",
    [
        {"self_consistent": False, "conflicting_attributes": []},
        {"self_consistent": False, "conflicting_attributes": ["deploy host"]},
        {"self_consistent": True, "conflicting_attributes": ["deploy host"]},
    ],
)
def test_prose_claiming_consistency_never_overrides_the_structured_fields(
    monkeypatch, reason, structured
):
    text = json.dumps({"verdict": "CONTRADICTION", "reason": reason, **structured})
    _reply(monkeypatch, text)
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "downgrade_reason" not in review
    # The four-key reply is not the two-key form the prose check reads, so a
    # structured "there is a conflict" cannot be marked as disputed either.
    assert "consistency_flag" not in review


def test_prose_alone_still_only_annotates(monkeypatch):
    # Backward compatibility: a reply without the new fields behaves as #1136.
    reason = "Both notes contain the same information but differ in format."
    _reply(monkeypatch, json.dumps({"verdict": "CONTRADICTION", "reason": reason}))
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert review["consistency_flag"] == "the same information"
    assert "downgrade_reason" not in review and "judge_verdict" not in review


def test_a_huge_deeply_nested_reply_stops_at_the_length_cap():
    # These would raise RecursionError in json.loads, but they are far over
    # _REPLY_MAX_CHARS, so the length guard rejects them before any parse.
    text = (
        f'{{"verdict": "CONTRADICTION", "reason": "{REASON}", "self_consistent": true, '
        '"conflicting_attributes": ' + "[" * 100_000 + "]" * 100_000 + "}"
    )
    assert len(text) > consolidate._REPLY_MAX_CHARS
    assert consolidate.structured_self_consistency(text, note_count=2) is False
    assert (
        consolidate.structured_self_consistency("[" * 100_000 + "]" * 100_000, note_count=2)
        is False
    )


def test_a_recursion_error_from_the_parser_fails_closed(monkeypatch):
    # On CPython 3.14 json.loads only raises RecursionError far past the
    # 1000-character cap (nesting depth 499 parses fine), so a real input
    # cannot reach the except arm; simulate the parser raising instead.
    def deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(consolidate.json, "loads", deep)
    assert consolidate.structured_self_consistency(json.dumps(DOWNGRADE), note_count=2) is False


@pytest.mark.parametrize("value", [None, b'{"x": 1}', 7])
def test_a_non_string_reply_fails_closed(value):
    assert consolidate.structured_self_consistency(value, note_count=2) is False


def test_the_structured_switch_turns_the_downgrade_off(monkeypatch):
    monkeypatch.setattr(consolidate, "_STRUCTURED_CONSISTENCY_DOWNGRADES", False)
    _reply(monkeypatch, json.dumps(DOWNGRADE))
    review = consolidate._review_cluster(NOTES)
    assert review["verdict"] == "CONTRADICTION"
    assert "downgrade_reason" not in review


def test_the_prose_downgrade_stays_off():
    assert consolidate._CONSISTENCY_FLAG_DOWNGRADES is False
    assert consolidate._STRUCTURED_CONSISTENCY_DOWNGRADES is True


# --- The judge contract -----------------------------------------------------


def test_the_schema_offers_the_structured_fields_without_requiring_them():
    schema = llm_client.ADJUDICATION_VERDICT_SCHEMA
    assert schema["required"] == ["verdict", "reason"]
    assert schema["properties"]["self_consistent"] == {"type": "boolean"}
    assert schema["properties"]["conflicting_attributes"] == {
        "type": "array",
        "items": {"type": "string"},
    }


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"verdict": "CONTRADICTION", "reason": REASON}),
        json.dumps(DOWNGRADE),
        _with(self_consistent=False, conflicting_attributes=["deploy host"]),
    ],
)
def test_old_and_new_replies_both_satisfy_the_schema(text):
    assert llm_client._response_matches_schema(text, llm_client.ADJUDICATION_VERDICT_SCHEMA)
    assert llm_client.parse_adjudication_response(text) == ("CONTRADICTION", REASON)


def test_the_prompt_asks_for_the_structured_fields():
    prompt = consolidate._ADJUDICATION_SYSTEM_PROMPT
    assert "self_consistent" in prompt
    assert "conflicting_attributes" in prompt


# --- Reports ----------------------------------------------------------------


def test_a_downgraded_cluster_is_reported_and_never_a_merge(monkeypatch):
    monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: list(NOTES))
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
    _reply(monkeypatch, json.dumps(DOWNGRADE))
    built = consolidate.build_proposals(object(), threshold=0.9)
    assert built["proposals"] == []
    [rejected] = built["rejected"]
    assert rejected["verdict"] == consolidate.VERDICT_NEEDS_REVIEW
    assert consolidate.auto_apply_safe_merges(built["proposals"], dry_run=True)["applied"] == []
    markdown = consolidate.render_proposal_markdown([], 0.9, 2, [], built["rejected"])
    assert "## Contradictions" not in markdown
    section = markdown.split("## Reviewed but not proposed", 1)[1]
    assert "self_consistent=true" in section
    assert consolidate.inline_code(consolidate.STRUCTURED_DOWNGRADE_REASON) in section
    assert consolidate.fenced_apply_commands(markdown) == []


def test_sleep_pass_reports_and_counts_a_structured_downgrade(monkeypatch):
    clusters = [
        [
            {"id": f"note-{i}-a", "title": "A", "content": "x"},
            {"id": f"note-{i}-b", "title": "B", "content": "y"},
        ]
        for i in range(2)
    ]
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: [])
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda candidates, threshold: clusters)
    replies = iter(
        [
            json.dumps(DOWNGRADE),
            _with(
                reason="The notes give different deploy hosts.",
                self_consistent=False,
                conflicting_attributes=["deploy host"],
            ),
        ]
    )
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": next(replies), "provider": "ollama"},
    )
    result = sleep_pass.run_sleep_pass(write=False)
    assert result["needs_review_count"] == 1
    assert result["contradiction_count"] == 1
    assert result["contradictions_disputed"] == 0
    markdown = result["markdown"]
    section = markdown.split("### Needs review", 1)[1].split("###", 1)[0]
    assert "`note-0-a`" in section and "note-1-a" not in section
    assert consolidate.inline_code(consolidate.STRUCTURED_DOWNGRADE_REASON) in section
    assert consolidate.fenced_apply_commands(markdown) == []


# --- Duplicate keys in the verdict itself (Devin review P3 on PR #1213) ------
#
# json.loads keeps the last of duplicate keys, so a reply that says
# CONTRADICTION and then DUPLICATE parsed as DUPLICATE, which authorizes a
# merge proposal (and, with auto-apply on, a supersede). Duplicate keys at any
# depth now make the reply unparseable: no verdict, no merge.

DUPLICATE_VERDICT_REPLIES = {
    "contradiction then duplicate": (
        '{"verdict": "CONTRADICTION", "verdict": "DUPLICATE", "reason": "x"}'
    ),
    "duplicate then duplicate": '{"verdict": "DUPLICATE", "verdict": "DUPLICATE", "reason": "x"}',
    "duplicate reason": '{"verdict": "DUPLICATE", "reason": "a", "reason": "b"}',
    # The regex fallback must not rescue a rejected object.
    "duplicate key with a legacy verdict in the reason": (
        '{"verdict": "UNRELATED", "verdict": "UNRELATED", "reason": "VERDICT: DUPLICATE"}'
    ),
    "duplicate key in a nested value": (
        '{"verdict": "DUPLICATE", "reason": "x", "conflicting_attributes": [{"a": 1, "a": 2}]}'
    ),
}


@pytest.mark.parametrize(
    "text", DUPLICATE_VERDICT_REPLIES.values(), ids=DUPLICATE_VERDICT_REPLIES.keys()
)
def test_a_duplicate_key_reply_never_becomes_a_merge_proposal(monkeypatch, text):
    monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: list(NOTES))
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
    _reply(monkeypatch, text)
    built = consolidate.build_proposals(object(), threshold=0.9)
    assert built["proposals"] == []
    [rejected] = built["rejected"]
    assert rejected["verdict"] == consolidate.VERDICT_UNPARSED
    assert llm_client.parse_adjudication_response(text) is None
    assert not llm_client._response_matches_schema(text, llm_client.ADJUDICATION_VERDICT_SCHEMA)


def test_a_duplicate_verdict_key_never_renders_an_apply_command(monkeypatch):
    cluster = [
        {"id": "dup-a", "title": "A", "content": "x"},
        {"id": "dup-b", "title": "B", "content": "y"},
    ]
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: [])
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda candidates, threshold: [cluster])
    _reply(monkeypatch, DUPLICATE_VERDICT_REPLIES["contradiction then duplicate"])
    result = sleep_pass.run_sleep_pass(write=False)
    assert result["duplicate_count"] == 0
    assert result["unparsed_count"] == 1
    assert consolidate.fenced_apply_commands(result["markdown"]) == []
