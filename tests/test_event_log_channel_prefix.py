"""``event_log_query`` accepts the ``channel_prefix`` argument clients send."""

from __future__ import annotations

import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import events
from unified_agent.tools.events import event_log_query


def test_signature_accepts_channel_prefix():
    params = inspect.signature(event_log_query).parameters
    assert "channel_prefix" in params
    assert params["channel_prefix"].default is None


def test_channel_prefix_filters_and_unions_with_channels():
    events.record_event("agent.approval.requested", {"n": 1}, notify=False)
    events.record_event("agent.task.updated", {"n": 2}, notify=False)
    events.record_event("agent.approval.resolved", {"n": 3}, notify=False)

    prefixed = event_log_query(channel_prefix="agent.approval")
    assert {row["channel"] for row in prefixed["events"]} == {
        "agent.approval.requested",
        "agent.approval.resolved",
    }

    exact = event_log_query(channel_prefix="agent.task.updated")
    assert [row["channel"] for row in exact["events"]] == ["agent.task.updated"]

    both = event_log_query(
        channels=["agent.task.updated"], channel_prefix="agent.approval.resolved"
    )
    assert {row["channel"] for row in both["events"]} == {
        "agent.task.updated",
        "agent.approval.resolved",
    }

    assert event_log_query(channel_prefix="  ")["count"] == 3
    assert events.merge_channel_prefix(["agent.task.*"], None) == ["agent.task.*"]
    assert events.merge_channel_prefix(None, None) is None


def test_channel_prefix_must_be_a_string():
    with pytest.raises(ToolError, match="channel_prefix must be a string"):
        event_log_query(channel_prefix=["agent.approval"])  # type: ignore[arg-type]
