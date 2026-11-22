"""Tests for passive session-end capture (task #813)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import llm_client, session_capture
from unified_agent.tools import memory as memory_tool

TRANSCRIPT_LINES = [
    {"type": "bridge-session", "sessionId": "x"},
    {
        "type": "user",
        "message": {"role": "user", "content": "Fix the auth bug in server.py"},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Patching the guard clause now."},
                {"type": "tool_use", "name": "Edit", "input": {}},
            ],
        },
    },
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done, tests pass."}],
        },
    },
]


@pytest.fixture
def transcript(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(row) for row in TRANSCRIPT_LINES), encoding="utf-8")
    return p


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def _complete(prompt, system=None, *, response_schema=None, response_mime_type=None, **kwargs):
        calls.append(prompt)
        return {
            "text": json.dumps(
                {
                    "summary": "Fixed the auth guard clause in server.py; tests pass.",
                    "facts": ["auth guard lives in server.py"],
                    "decisions": ["patch the shared guard, not each caller"],
                    "files": ["src/unified_agent/server.py"],
                }
            ),
            "provider": "ollama",
            "model": "llama3:8b",
            "route": [{"target": "ollama", "ok": True, "elapsed_s": 0.1}],
        }

    monkeypatch.setattr(session_capture.llm_client, "complete", _complete)
    return calls


def test_render_transcript_keeps_text_drops_noise(transcript):
    tail = session_capture.load_transcript_tail(transcript)
    assert "user: Fix the auth bug" in tail
    assert "assistant: Patching the guard clause now. [tool: Edit]" in tail
    assert "hidden" not in tail  # thinking dropped
    assert "tool_result" not in tail


def test_capture_stores_note_with_expected_shape(transcript, fake_llm, isolated_env):
    res = session_capture.capture_session(str(transcript), cwd="/home/you/dev/unified_agent")

    assert res["stored"] is True
    assert res["created"] is True
    assert res["provider"] == "ollama"

    note = memory_tool.memory_get(res["id"])
    assert set(note["tags"]) == {"session-capture", "project:unified_agent"}
    assert note["salience"] == 0.4
    assert "auth guard clause" in note["content"]
    assert f"session-sha256:{res['hash']}" in note["content"]

    # TTL ~ retention window (default 45d), give or take a day.
    expires = datetime.fromisoformat(note["expires_at"])
    target = datetime.now(UTC) + timedelta(days=45)
    assert abs((expires - target).total_seconds()) < 86400


def test_capture_is_idempotent_on_segment_hash(transcript, fake_llm, isolated_env):
    first = session_capture.capture_session(str(transcript), cwd="/tmp/proj")
    second = session_capture.capture_session(str(transcript), cwd="/tmp/proj")

    # Same segment hash -> memory_save's idempotency key returns the first note,
    # no duplicate row written.
    assert first["id"] == second["id"]
    assert first["hash"] == second["hash"]
    listing = memory_tool.memory_list(tag="session-capture", limit=50)
    assert [n["id"] for n in listing["items"]].count(first["id"]) == 1
    assert len(listing["items"]) == 1


def test_llm_connection_error_propagates(transcript, monkeypatch, isolated_env):
    def _boom(*a, **k):
        raise llm_client.LLMConnectionError("all providers down", route=[{"target": "ollama"}])

    monkeypatch.setattr(session_capture.llm_client, "complete", _boom)
    with pytest.raises(llm_client.LLMConnectionError):
        session_capture.capture_session(str(transcript), cwd="/tmp/proj")


def test_empty_transcript_is_not_stored(tmp_path, fake_llm, isolated_env):
    p = tmp_path / "empty.jsonl"
    p.write_text('{"type": "bridge-session"}\n', encoding="utf-8")
    res = session_capture.capture_session(str(p), cwd="/tmp/proj")
    assert res["stored"] is False
    assert len(fake_llm) == 0


def test_render_transcript_handles_string_messages_and_alternative_schemas(tmp_path):
    rows = [
        "not-json",
        12345,
        {"type": "bridge-session", "sessionId": "ignore-me"},
        {"type": "user", "message": "Fix the compiler warning"},
        {"type": "assistant", "message": "Fixed warning in parser.py"},
        {"role": "user", "content": "Run the tests"},
        {"role": "assistant", "content": ["Tests passing.", "[tool: Pytest]"]},
    ]
    p = tmp_path / "harness_transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    tail = session_capture.load_transcript_tail(p)
    assert "user: Fix the compiler warning" in tail
    assert "assistant: Fixed warning in parser.py" in tail
    assert "user: Run the tests" in tail
    assert "assistant: Tests passing. [tool: Pytest]" in tail
