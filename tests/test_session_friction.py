"""Session-end friction scoring → one redacted learning-capture nudge (task #914)."""

from __future__ import annotations

from unified_agent import config
from unified_agent.session_friction import (
    SessionFriction,
    build_nudge_message,
    evaluate_friction,
    friction_reasons,
    maybe_nudge_session_learning,
    redacted_task_summary,
    score_friction,
)
from unified_agent.tools.registry import session_end, session_start

_SECRET = "sk-livetoken0123456789abcdef"


# ─── pure scoring: the refusal path is a first-class test ──────────────────────


def test_zero_friction_routine_session_never_nudges():
    friction = SessionFriction(tool_calls=40, unique_tools=6, used_skill=True)
    decision = evaluate_friction(friction, threshold=20, tool_gate=15)
    # scale nudge alone (diversity + skill) tops out well below the threshold
    assert decision.score <= 10
    assert decision.nudge is False
    assert decision.gated == "below_threshold"
    assert decision.reasons == []


def test_one_primary_signal_lands_on_the_threshold():
    for friction in (
        SessionFriction(interrupts=1, tool_calls=20),
        SessionFriction(tool_rejects=1, tool_calls=20),
        SessionFriction(corrections=1, tool_calls=20),
    ):
        decision = evaluate_friction(friction, threshold=20, tool_gate=15)
        assert decision.score == 20
        assert decision.nudge is True


def test_tool_retry_tiers():
    assert score_friction(SessionFriction(tool_retries=2)) == 0
    assert score_friction(SessionFriction(tool_retries=3)) == 10
    assert score_friction(SessionFriction(tool_retries=5)) == 18
    assert score_friction(SessionFriction(tool_retries=8)) == 25


def test_threshold_boundary_is_inclusive():
    at = SessionFriction(tool_retries=3, tool_calls=20)  # score 10
    assert evaluate_friction(at, threshold=10, tool_gate=0).nudge is True
    assert evaluate_friction(at, threshold=11, tool_gate=0).nudge is False


def test_tool_call_hard_gate_blocks_a_tiny_high_friction_session():
    friction = SessionFriction(interrupts=3, tool_rejects=2, tool_calls=4)
    decision = evaluate_friction(friction, threshold=20, tool_gate=15)
    assert decision.score >= 20
    assert decision.nudge is False
    assert decision.gated == "tool_gate"


def test_friction_reasons_names_only_nonzero_signals():
    friction = SessionFriction(interrupts=2, tool_retries=4, tool_calls=20)
    reasons = friction_reasons(friction)
    assert reasons == [
        "you interrupted the agent twice",
        "the agent retried failing tools 4 times",
    ]
    assert friction_reasons(SessionFriction()) == []


def test_from_mapping_accepts_flat_and_nested_shapes():
    flat = SessionFriction.from_mapping(
        {"interrupts": 2, "tool_rejects": 1, "tool_calls": 30, "unique_tools": 5}
    )
    assert (flat.interrupts, flat.tool_rejects, flat.tool_calls) == (2, 1, 30)

    nested = SessionFriction.from_mapping(
        {"interventions": {"interrupt": 1, "toolReject": 2, "toolError": 5}, "toolCalls": 22}
    )
    assert (nested.interrupts, nested.tool_rejects, nested.tool_retries) == (1, 2, 5)
    assert nested.tool_calls == 22


# ─── redaction: no raw task content, no secrets, one bounded line ──────────────


def test_redacted_task_summary_strips_secrets_and_collapses_whitespace():
    session = {"summary": f"Fixed deploy\nusing token {_SECRET}\tand a retry"}
    out = redacted_task_summary(session)
    assert out is not None
    assert _SECRET not in out
    assert "[REDACTED_SECRET]" in out
    assert "\n" not in out and "\t" not in out


def test_redacted_task_summary_is_length_bounded():
    session = {"summary": "x" * 400}
    out = redacted_task_summary(session)
    assert out is not None and len(out) <= 120


def test_redacted_task_summary_none_when_empty():
    assert redacted_task_summary({"summary": "", "task_title": ""}) is None


def test_build_nudge_message_carries_reasons_not_raw_content():
    decision = evaluate_friction(
        SessionFriction(interrupts=2, tool_calls=20), threshold=20, tool_gate=15
    )
    summary = redacted_task_summary({"summary": f"ship it with {_SECRET}"})
    message = build_nudge_message("01SESSIONX", decision, summary)
    assert "you interrupted the agent twice" in message
    assert "propose_session_learnings" in message
    assert _SECRET not in message


# ─── session_end integration (dedupe-once state must be real) ──────────────────


def _open() -> str:
    return session_start(agent="Desktop_Agent1", kind="coding")["session_id"]


def test_session_end_nudges_once_and_dedupes_on_reevaluation(isolated_env):
    sid = session_start(
        agent="Desktop_Agent1",
        kind="coding",
        meta={"friction": {"interrupts": 2, "tool_calls": 25}},
    )["session_id"]
    result = session_end(sid, status="completed", summary=f"debug loop, leaked {_SECRET}")

    nudge = result["friction_nudge"]
    assert nudge["nudge"] is True
    assert nudge["score"] >= 20
    assert "you interrupted the agent twice" in nudge["message"]
    assert _SECRET not in nudge["message"]
    assert _SECRET not in (nudge["summary"] or "")

    # Dedupe state survives a fresh evaluation of the same closed session.
    again = maybe_nudge_session_learning(sid, "completed")
    assert again["nudge"] is False
    assert again["deduped"] is True
    assert again["score"] == nudge["score"]


def test_zero_friction_session_end_reports_no_nudge(isolated_env):
    sid = _open()
    result = session_end(sid, status="completed", summary="ran the usual checklist")
    nudge = result["friction_nudge"]
    assert nudge["nudge"] is False
    assert nudge["deduped"] is False


def test_failed_session_is_not_evaluated(isolated_env):
    sid = _open()
    result = session_end(sid, status="failed", summary="boom")
    assert result.get("friction_nudge") is None


def test_disabled_flag_suppresses_the_nudge(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_FRICTION_NUDGE", "0")
    assert config.friction_nudge_enabled() is False
    sid = session_start(
        agent="Desktop_Agent1",
        kind="coding",
        meta={"friction": {"interrupts": 3, "tool_calls": 30}},
    )["session_id"]
    result = session_end(sid, status="completed", summary="lots of friction here")
    assert result.get("friction_nudge") is None


def test_threshold_env_override_is_honoured(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_FRICTION_THRESHOLD", "5")
    monkeypatch.setenv("UA_FRICTION_TOOL_GATE", "0")
    assert config.friction_score_threshold() == 5
    sid = session_start(
        agent="Desktop_Agent1",
        kind="coding",
        meta={"friction": {"tool_retries": 3, "tool_calls": 2}},  # score 10
    )["session_id"]
    result = session_end(sid, status="completed", summary="small but gnarly")
    assert result["friction_nudge"]["nudge"] is True


def test_nudge_never_writes_memory(isolated_env):
    from unified_agent.tools import memory as memory_tools

    before = memory_tools.memory_list(limit=50)["items"]
    sid = session_start(
        agent="Desktop_Agent1",
        kind="coding",
        meta={"friction": {"interrupts": 2, "tool_rejects": 1, "tool_calls": 25}},
    )["session_id"]
    session_end(sid, status="completed", summary="painful refactor")
    after = memory_tools.memory_list(limit=50)["items"]
    assert after == before
