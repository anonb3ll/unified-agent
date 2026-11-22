"""Inert credential shapes in node-event keys never reach durable evidence (#1932)."""

import json

import pytest

from unified_agent import node_queue, storage


def _shape(marker="A"):
    # Deliberately unminted synthetic shape, constructed only in the isolated test.
    return "uas_" + marker * 43


@pytest.mark.parametrize("field", ["data", "tool_call", "plan"])
def test_record_node_event_scrubs_nested_dictionary_keys(field, tmp_path):
    node = "redaction-test"
    identity = "Desktop_Agent2"
    node_queue.register(node, identity=identity, facts={"backends": ["codex"]})
    item = node_queue.enqueue(
        node, created_by=identity, prompt="inert", cwd=str(tmp_path), backend="codex"
    )
    node_queue.claim_next(node, identity=identity)
    shape = _shape()
    event = {"kind": "redaction_test", field: {"nested": [{shape: "evidence"}]}}
    original = json.dumps(event)

    result = node_queue.record_node_event(node, item["item_id"], [event], identity=identity)

    assert result["recorded"] == 1
    conn = storage.connect()
    try:
        payloads = [
            json.loads(row["payload"])
            for row in conn.execute(
                "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
            )
        ]
    finally:
        conn.close()
    stored = next(payload for payload in payloads if payload["kind"] == "acp_event")
    assert shape not in json.dumps(stored)
    assert stored[field] == {"nested": [{"[REDACTED_SECRET]": "evidence"}]}
    assert json.dumps(event) == original


@pytest.mark.parametrize("literal_first", [False, True])
def test_redacted_key_collisions_preserve_values_and_literal_keys(literal_first):
    literals = {"[REDACTED_SECRET]": "literal", "[REDACTED_SECRET]#2": "suffix literal"}
    shaped = {_shape("A"): "first", _shape("B"): {"nested": "second"}}
    value = {**literals, **shaped} if literal_first else {**shaped, **literals}

    redacted = node_queue._redact_tree(value)

    assert len(redacted) == len(value)
    assert redacted["[REDACTED_SECRET]"] == "literal"
    assert redacted["[REDACTED_SECRET]#2"] == "suffix literal"
    assert redacted["[REDACTED_SECRET]#3"] == "first"
    assert redacted["[REDACTED_SECRET]#4"] == {"nested": "second"}
    assert all(_shape(marker) not in json.dumps(redacted) for marker in ("A", "B"))
    assert node_queue._redact_tree(value) == redacted
    assert node_queue._redact_tree(redacted) == redacted


def test_redacted_key_collisions_without_literal_keep_every_entry():
    value = {_shape("A"): 1, _shape("B"): 2, _shape("C"): 3}
    assert node_queue._redact_tree(value) == {
        "[REDACTED_SECRET]": 1,
        "[REDACTED_SECRET]#2": 2,
        "[REDACTED_SECRET]#3": 3,
    }


def test_depth_limit_still_scrubs_keys_in_flattened_evidence():
    shape = _shape()
    value = {shape: "deep evidence"}
    for _ in range(13):
        value = {"nested": value}
    stored = json.dumps(node_queue._redact_tree(value))
    assert shape not in stored
    assert "deep evidence" in stored


def test_non_sensitive_dictionary_keys_and_values_keep_their_structure():
    value = {"toolCallId": "tc-1", "nested": [{"status": "done", "count": 3}]}
    assert node_queue._redact_tree(value) == value
