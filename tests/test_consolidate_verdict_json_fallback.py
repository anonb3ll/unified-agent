"""A JSON-looking judge reply is strict JSON or UNPARSED (task #2089).

The cluster judge's reply is parsed by ``llm_client.parse_adjudication_response``.
Before this task a reply that started with ``{`` but failed strict parsing
(malformed, too deeply nested, a list, a valid object with no usable verdict)
fell through to the legacy ``VERDICT:`` text pattern. A malformed object whose
text contained ``VERDICT: DUPLICATE`` therefore parsed as DUPLICATE, which
authorizes a merge proposal and, with auto-apply on, a supersede (Devin review
on PR #1213). A reply that is JSON-shaped (starts with ``{`` or ``[``, or is a
fenced block) is now read only as strict JSON; anything else about it makes the
cluster UNPARSED. The plain-text VERDICT:/REASON: path stays for genuinely
non-JSON replies.
"""

import json

import pytest

from unified_agent import codex_llm_client, consolidate, llm_client, sleep_pass

NOTES = [
    {"id": "9846126b6aa6", "title": "A", "content": "x", "embedding": b"a"},
    {"id": "a13d8ec16f31", "title": "B", "content": "y", "embedding": b"b"},
]

RESCUED_REPLIES = {
    "missing closing brace": '{"verdict": "CONTRADICTION", "reason": "VERDICT: DUPLICATE"',
    "deeply nested and unbalanced": (
        '{"verdict": "UNRELATED", "reason": "VERDICT: DUPLICATE", "n": ' + "[" * 100_000
    ),
    "trailing text after the object": (
        '{"verdict": "CONTRADICTION", "reason": "x"}\nVERDICT: DUPLICATE\nREASON: same'
    ),
    "valid object with an empty reason": (
        '{"verdict": "CONTRADICTION", "reason": "", "note": "VERDICT: DUPLICATE"}'
    ),
    "valid object with no verdict key": '{"note": "VERDICT: DUPLICATE\\nREASON: same"}',
    "valid object with an unknown verdict": (
        '{"verdict": "MERGE", "reason": "VERDICT: DUPLICATE REASON: same"}'
    ),
    "a JSON array": '["VERDICT: DUPLICATE", "REASON: same"]',
    "malformed array": '["VERDICT: DUPLICATE\nREASON: same"',
    "fenced malformed object": (
        '```json\n{"verdict": "CONTRADICTION", "reason": "VERDICT: DUPLICATE"\n```'
    ),
    "unclosed json fence": '```json\n{"verdict": "CONTRADICTION", "reason": "VERDICT: DUPLICATE"',
    "bare fence around a malformed object": (
        '```\n{"verdict": "CONTRADICTION",\nVERDICT: DUPLICATE\nREASON: same\n```'
    ),
    "prose before a fenced malformed object": (
        'Here is my answer:\n```json\n{"verdict": "UNRELATED",\n'
        "VERDICT: DUPLICATE\nREASON: same\n```"
    ),
    "prose before a malformed object": (
        'My verdict: {"verdict": "UNRELATED", "reason": "VERDICT: DUPLICATE"'
    ),
}


def _reply(monkeypatch, text: str) -> None:
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {"text": text, "provider": "ollama"},
    )


@pytest.mark.parametrize("text", RESCUED_REPLIES.values(), ids=RESCUED_REPLIES.keys())
def test_a_json_looking_reply_is_never_rescued_by_the_text_pattern(text):
    assert llm_client.parse_adjudication_response(text) is None


@pytest.mark.parametrize("text", RESCUED_REPLIES.values(), ids=RESCUED_REPLIES.keys())
def test_a_malformed_json_reply_never_becomes_a_merge_proposal(monkeypatch, text):
    monkeypatch.setenv("UA_CONSOLIDATE_AUTO_APPLY", "1")
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: list(NOTES))
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda notes, threshold: [notes])
    _reply(monkeypatch, text)
    built = consolidate.build_proposals(object(), threshold=0.9)
    assert built["proposals"] == []
    [rejected] = built["rejected"]
    assert rejected["verdict"] == consolidate.VERDICT_UNPARSED
    assert consolidate.auto_apply_safe_merges(built["proposals"], dry_run=True)["applied"] == []
    markdown = consolidate.render_proposal_markdown([], 0.9, 2, [], built["rejected"])
    assert consolidate.fenced_apply_commands(markdown) == []


@pytest.mark.parametrize("text", RESCUED_REPLIES.values(), ids=RESCUED_REPLIES.keys())
def test_sleep_pass_counts_a_malformed_json_reply_as_unparsed(monkeypatch, text):
    cluster = [
        {"id": "dup-a", "title": "A", "content": "x"},
        {"id": "dup-b", "title": "B", "content": "y"},
    ]
    monkeypatch.setattr(consolidate, "_load_candidates", lambda conn: [])
    monkeypatch.setattr(consolidate, "cluster_duplicates", lambda candidates, threshold: [cluster])
    _reply(monkeypatch, text)
    result = sleep_pass.run_sleep_pass(write=False)
    assert result["duplicate_count"] == 0
    assert result["unparsed_count"] == 1
    assert consolidate.fenced_apply_commands(result["markdown"]) == []


# --- What still parses ------------------------------------------------------

STILL_PARSED = {
    "strict object": ('{"verdict": "DUPLICATE", "reason": "same"}', ("DUPLICATE", "same")),
    "strict object with padding": (
        '\n  {"verdict": "unrelated", "reason": " different "}  \n',
        ("UNRELATED", "different"),
    ),
    "json fence around a strict object": (
        '```json\n{"verdict": "CONTRADICTION", "reason": "hosts differ"}\n```',
        ("CONTRADICTION", "hosts differ"),
    ),
    "bare fence around a strict object": (
        '```\n{"verdict": "UNRELATED", "reason": "different"}\n```',
        ("UNRELATED", "different"),
    ),
    "legacy two lines": ("VERDICT: DUPLICATE\nREASON: same fact", ("DUPLICATE", "same fact")),
    "legacy after a prose line": (
        "The second note restates the first.\nVERDICT: DUPLICATE\nREASON: same fact",
        ("DUPLICATE", "same fact"),
    ),
    "legacy indented": ("  VERDICT: UNRELATED\n\n  REASON: n/a", ("UNRELATED", "n/a")),
    "legacy with a bracket in the reason": (
        "VERDICT: UNRELATED\nREASON: [[Note A]] is about deploys",
        ("UNRELATED", "[[Note A]] is about deploys"),
    ),
}


@pytest.mark.parametrize("case", STILL_PARSED.values(), ids=STILL_PARSED.keys())
def test_strict_json_and_genuine_legacy_text_still_parse(case):
    text, expected = case
    assert llm_client.parse_adjudication_response(text) == expected


NOT_LEGACY = {
    # A legacy verdict must open a line: a VERDICT: phrase quoted inside other
    # text (for example a JSON string after a prose prefix) is not one.
    "verdict mid-line": 'Answer: "VERDICT: DUPLICATE"\nREASON: same',
    # Text carrying a JSON object or a code fence is not a plain-text reply.
    "object after prose with a line-start verdict": (
        'Sure: {"reason": "x\nVERDICT: DUPLICATE\nREASON: same"}'
    ),
    "fence after prose with legacy text inside": (
        "Sure:\n```\nVERDICT: DUPLICATE\nREASON: same\n```"
    ),
}


@pytest.mark.parametrize("text", NOT_LEGACY.values(), ids=NOT_LEGACY.keys())
def test_text_that_is_not_a_plain_legacy_reply_is_unparsed(text):
    assert llm_client.parse_adjudication_response(text) is None


# --- The codex leg's schema check (telemetry) --------------------------------


def _codex_stream(text: str) -> str:
    events = [
        {"type": "thread.started", "thread_id": "fake"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 20, "cached_input_tokens": 15, "output_tokens": 5},
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "CONTRADICTION", "verdict": "DUPLICATE", "reason": "x"}',
        '{"verdict": "DUPLICATE", "reason": "a", "reason": "b"}',
        '{"verdict": "DUPLICATE", "reason": "x", "conflicting_attributes": [{"a": 1, "a": 2}]}',
    ],
)
def test_the_codex_leg_reports_a_duplicate_key_reply_as_a_schema_mismatch(text):
    result = codex_llm_client.parse_result(
        codex_llm_client.CLIResult(0, _codex_stream(text), ""),
        "gpt-6-luna",
        llm_client.ADJUDICATION_VERDICT_SCHEMA,
    )
    assert not result.ok and result.error == "schema_mismatch"


def test_the_codex_leg_still_accepts_a_strict_verdict():
    result = codex_llm_client.parse_result(
        codex_llm_client.CLIResult(0, _codex_stream('{"verdict":"DUPLICATE","reason":"same"}'), ""),
        "gpt-6-luna",
        llm_client.ADJUDICATION_VERDICT_SCHEMA,
    )
    assert result.ok
