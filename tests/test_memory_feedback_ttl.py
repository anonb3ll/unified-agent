"""P1 #508: memory feedback signals and temporary-memory TTLs."""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.memory import (
    memory_feedback,
    memory_get,
    memory_list,
    memory_save,
    memory_search,
)


def _offline_embeddings(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(
        embeddings, "generate_embedding", lambda x: (_ for _ in ()).throw(RuntimeError("offline"))
    )


def test_expired_note_hidden_from_default_recall_but_gettable(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    note = memory_save(
        "Sprint workaround for the flaky hub handshake",
        title="Temporary hub workaround",
        expires_at=past,
    )

    hidden = memory_search("workaround")
    assert note["id"] not in [r["id"] for r in hidden["results"]]

    listed = memory_list()
    assert note["id"] not in [i["id"] for i in listed["items"]]

    recovered = memory_get(note["id"])
    assert recovered["id"] == note["id"]
    assert recovered["expires_at"] == past
    assert "Sprint workaround" in recovered["content"]

    shown = memory_search("workaround", include_expired=True)
    assert note["id"] in [r["id"] for r in shown["results"]]


def test_unexpired_note_still_searchable(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    note = memory_save(
        "Keep this sprint fact until Friday",
        title="Sprint fact still live",
        expires_at=future,
    )
    res = memory_search("sprint fact")
    assert note["id"] in [r["id"] for r in res["results"]]


def test_helpful_feedback_ranks_above_twin(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    a = memory_save("Shared keyword telemetry waterfall ranking fixture", title="Note A")
    b = memory_save("Shared keyword telemetry waterfall ranking fixture", title="Note B")
    memory_feedback(a["id"], "helpful")
    memory_feedback(a["id"], "helpful")

    res = memory_search("waterfall ranking fixture")
    ids = [r["id"] for r in res["results"]]
    assert ids.index(a["id"]) < ids.index(b["id"])
    helped = next(r for r in res["results"] if r["id"] == a["id"])
    assert helped["feedback_helpful"] == 2


def test_wrong_feedback_ranks_below_twin(isolated_env, monkeypatch):
    _offline_embeddings(monkeypatch)
    a = memory_save("Shared keyword memory quality ranking fixture", title="Accurate note")
    b = memory_save("Shared keyword memory quality ranking fixture", title="Wrong note")
    memory_feedback(b["id"], "wrong")

    res = memory_search("quality ranking fixture")
    ids = [r["id"] for r in res["results"]]
    assert ids.index(a["id"]) < ids.index(b["id"])


def test_feedback_does_not_rewrite_content(isolated_env):
    note = memory_save("Original decision text must survive feedback", title="Decision")
    before = memory_get(note["id"])
    out = memory_feedback(note["id"], "stale")
    after = memory_get(note["id"])
    assert after["content"] == before["content"]
    assert after["title"] == before["title"]
    assert after["updated_at"] == before["updated_at"]
    assert out["signal"] == "stale"
    assert after["feedback_stale"] == 1


def test_invalid_feedback_signal_rejected(isolated_env):
    note = memory_save("A note that should not accept junk signals", title="Guard")
    with pytest.raises(ToolError, match="signal"):
        memory_feedback(note["id"], "love-it")


def test_expires_at_and_clear_are_exclusive(isolated_env):
    with pytest.raises(ToolError, match="clear_expires_at"):
        memory_save(
            "Cannot set and clear expiry together",
            title="Bad expiry",
            expires_at="2026-09-01T00:00:00+00:00",
            clear_expires_at=True,
        )
