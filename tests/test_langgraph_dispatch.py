"""Acceptance tests for the governed LangGraph dispatch adapter."""

from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langgraph")

from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety, storage
from unified_agent.langgraph_dispatch import (
    DispatchOutcome,
    DuplicateDeliveryUncertainError,
    OutcomeRecordingError,
    UnifiedAgentDispatchAdapter,
)
from unified_agent.langgraph_pilot import (
    PilotResult,
    PilotRun,
    StaleAuthorityError,
    ToolProposal,
    TransientGatewayError,
)
from unified_agent.policy import PolicyViolation
from unified_agent.tools.approvals import approval_resolve
from unified_agent.tools.tasks import task_add, task_claim, task_update

RUN_ID = "01JQ9Z6Y5K7M2N4P6R8T0V2X4Z"


def _approve(approval_id: int) -> None:
    """The hub fixture is the requester. Approve as a different human."""
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def authenticated_hub(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-langgraph-dispatch-session")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def _claimed_task() -> dict:
    task = task_add("Governed graph automation", notes="bounded automation task")
    return task_claim(task["id"], agent="Desktop_Agent2", timeout_seconds=3600)


def _run(task: dict) -> PilotRun:
    return PilotRun(
        run_id=RUN_ID,
        task_id=task["id"],
        task_updated_at=task["updated_at"],
        claimed_by=task["claimed_by"],
        claim_expires_at=task["lock_expires_at"],
        expected_profile="developer",
        required_capabilities=("web_search",),
        budget_revision="budget-v1",
        trace_id="trace-task-automation",
        query="LangGraph governed automation",
    )


def _proposal() -> ToolProposal:
    return ToolProposal("web_search", {"query": "LangGraph governed automation", "max_results": 2})


def _adapter(web_search, *, task_writer=task_update) -> UnifiedAgentDispatchAdapter:
    return UnifiedAgentDispatchAdapter(
        budget_revision=lambda _task_id: "budget-v1",
        tool_registry={"web_search": web_search, "task_update": task_writer},
    )


def test_identity_approval_audit_and_restart_idempotency(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": [{"title": "Governed", "url": "https://example.test"}]}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"

    wrong_identity = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(StaleAuthorityError, match="verified identity"):
            adapter.call_read_tool(
                run=run,
                proposal=_proposal(),
                idempotency_key=idempotency_key,
                timeout_s=20.0,
            )
    finally:
        config.agent_identity_var.reset(wrong_identity)
    assert calls == 0

    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert pending.status == "pending"
    assert pending.approval_id
    assert calls == 0

    _approve(pending.approval_id)
    allowed = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert allowed.status == "allowed"
    assert calls == 1

    # A new adapter instance represents process restart/recovery. It returns
    # the committed control-plane result without consuming another approval or
    # calling the tool again.
    recovered = _adapter(web_search).call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert recovered.result == allowed.result
    assert recovered.approval_id == allowed.approval_id
    assert calls == 1

    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (int(pending.approval_id),)
        ).fetchone()
        effect_events = conn.execute(
            "SELECT summary FROM agent_events WHERE kind='langgraph_effect' "
            "AND ref_id=? ORDER BY id",
            (idempotency_key,),
        ).fetchall()
    finally:
        conn.close()
    assert approval["status"] == "consumed"
    assert any(row["summary"].startswith("succeeded:") for row in effect_events)
    assert any(row["summary"].startswith("duplicate-succeeded:") for row in effect_events)


def test_canonical_denial_is_terminal_and_replay_safe(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    approval_resolve(
        int(pending.approval_id),
        "deny",
        resolved_by="Operator",
        reason="secret=abcdefghijklmnop not allowed",
    )

    denied = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert denied.status == "denied"
    assert "abcdefghijklmnop" not in (denied.reason or "")
    assert calls == 0

    replay = _adapter(web_search).call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert replay.status == "denied"
    assert replay.reason == denied.reason
    assert calls == 0


def test_concurrent_duplicate_fails_closed_while_effect_is_executing(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    _approve(pending.approval_id)

    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            context.run,
            adapter.call_read_tool,
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )
        assert entered.wait(timeout=5)
        with pytest.raises(DuplicateDeliveryUncertainError, match="executing"):
            adapter.call_read_tool(
                run=run,
                proposal=_proposal(),
                idempotency_key=idempotency_key,
                timeout_s=20.0,
            )
        release.set()
        assert first.result(timeout=5).status == "allowed"
    assert calls == 1


def test_completed_outcome_finishes_task_and_delegation_once(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    now = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, callback, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'none', 'in_flight', ?, ?)",
            (
                "Desktop_Agent1",
                "Desktop_Agent2",
                task["id"],
                "Run bounded governed automation",
                now,
                now,
            ),
        )
        delegation_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    adapter = _adapter(lambda **_kwargs: {"results": []})
    pilot_result = PilotResult(
        status="completed",
        output={"summary": "bounded", "evidence_digest": "b" * 64},
        duplicate=False,
        recovered=False,
    )
    outcome = DispatchOutcome.completed(pilot_result)
    first = adapter.record_task_outcome(run, outcome)
    duplicate = adapter.record_task_outcome(run, outcome)

    assert first == duplicate
    assert first["status"] == "done"
    assert first["notes"].count(f"[langgraph-run: {RUN_ID}]") == 1
    assert "evidence_digest" not in first["notes"]
    assert "b" * 64 not in first["notes"]
    conn = storage.connect()
    try:
        delegation = conn.execute(
            "SELECT status, callback_result FROM delegations WHERE id=?", (delegation_id,)
        ).fetchone()
    finally:
        conn.close()
    assert delegation["status"] == "completed"
    assert delegation["callback_result"] == "ok"


def test_task_update_rejects_a_stale_authority_revision(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    stale_revision = task["updated_at"]
    task_update(task["id"], append_note="[external-change]")

    with pytest.raises(ToolError, match="authority revision changed"):
        task_update(
            task["id"],
            append_note="[stale-write]",
            expected_updated_at=stale_revision,
        )
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (task["id"],)).fetchone()[
            "notes"
        ]
    finally:
        conn.close()
    assert "[external-change]" in notes
    assert "[stale-write]" not in notes


def test_outcome_recording_quarantines_a_concurrent_task_change(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)

    def racing_task_update(**kwargs):
        task_update(kwargs["id"], append_note="[concurrent-authority-change]")
        return task_update(**kwargs)

    adapter = _adapter(lambda **_kwargs: {"results": []}, task_writer=racing_task_update)
    outcome = DispatchOutcome(
        status="blocked",
        reason_code="governed-tool-denied",
    )
    with pytest.raises(OutcomeRecordingError, match="uncertain"):
        adapter.record_task_outcome(run, outcome)

    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, notes FROM tasks WHERE id=?", (task["id"],)).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?",
            (f"ua-lg:{RUN_ID}:task_outcome:record",),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "doing"
    assert "[concurrent-authority-change]" in row["notes"]
    assert json.loads(effect["result_json"])["state"] == "unknown"


def test_policy_rejection_before_approval_does_not_consume_pass(authenticated_hub, monkeypatch):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    gate_calls = {"count": 0}
    real_gate = safety.check_policy_gate

    def gated_check(tool, payload=None, **kwargs):
        gate_calls["count"] += 1
        if gate_calls["count"] > 1:
            raise PolicyViolation("policy changed while approval was pending")
        return real_gate(tool, payload, **kwargs)

    monkeypatch.setattr(safety, "check_policy_gate", gated_check)

    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    approval_id = int(pending.approval_id)
    _approve(approval_id)

    with pytest.raises(PolicyViolation, match="policy changed"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )
    assert calls == 0

    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert approval["status"] == "approved"
    assert json.loads(effect["result_json"])["state"] == "failed"


def test_policy_rejection_after_approval_consumption_restores_pass(authenticated_hub, monkeypatch):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    gate_calls = {"count": 0}
    real_gate = safety.check_policy_gate

    def gated_check(tool, payload=None, **kwargs):
        gate_calls["count"] += 1
        if gate_calls["count"] > 2:
            raise PolicyViolation("policy changed after approval was consumed")
        return real_gate(tool, payload, **kwargs)

    monkeypatch.setattr(safety, "check_policy_gate", gated_check)

    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    approval_id = int(pending.approval_id)
    _approve(approval_id)

    with pytest.raises(PolicyViolation, match="after approval was consumed"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )
    assert calls == 0

    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert approval["status"] == "approved"
    assert json.loads(effect["result_json"])["state"] == "failed"


def test_revalidate_storage_failure_after_approval_releases_prepared_claim(
    authenticated_hub, monkeypatch
):
    """Transient storage failures after approval must not quarantine the idempotency key."""
    import sqlite3

    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    adapter = _adapter(lambda **_kwargs: {"results": []})
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    revalidate_calls = {"count": 0}
    real_revalidate = adapter.revalidate

    def flaky_revalidate(run_arg):
        revalidate_calls["count"] += 1
        if revalidate_calls["count"] > 1:
            raise sqlite3.OperationalError("database is locked")
        return real_revalidate(run_arg)

    monkeypatch.setattr(adapter, "revalidate", flaky_revalidate)

    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    approval_id = int(pending.approval_id)
    _approve(approval_id)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )

    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert approval["status"] == "approved"
    assert effect is None


def test_governed_attempt_timeout_quarantines_uncertain_outcome(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0
    attempt_timeout_s = 0.5
    # The tool outlasts the attempt deadline: it holds until the test ends
    # instead of sleeping on in a leaked worker thread.
    release = threading.Event()

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        release.wait(5)
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=attempt_timeout_s,
    )
    _approve(pending.approval_id)

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="attempt deadline"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=attempt_timeout_s,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0

    with pytest.raises(DuplicateDeliveryUncertainError, match="unknown"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=attempt_timeout_s,
        )

    conn = storage.connect()
    try:
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(effect["result_json"])["state"] == "unknown"
    assert calls == 1
    release.set()


def test_transient_gateway_error_releases_effect_for_retry(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientGatewayError("retryable transport failure")
        return {"results": [{"title": "Recovered", "url": "https://example.test"}]}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    _approve(pending.approval_id)

    with pytest.raises(TransientGatewayError, match="retryable transport"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )

    allowed = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert allowed.status == "allowed"
    assert calls == 2

    conn = storage.connect()
    try:
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(effect["result_json"])["state"] == "succeeded"


def test_multiline_denial_reason_is_terminal_and_replay_safe(authenticated_hub):
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    multiline_reason = "line one\nline two"
    approval_resolve(
        int(pending.approval_id),
        "deny",
        resolved_by="Operator",
        reason=multiline_reason,
    )

    denied = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert denied.status == "denied"
    assert denied.reason == multiline_reason
    assert calls == 0

    replay = _adapter(web_search).call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert replay.status == "denied"
    assert replay.reason == multiline_reason
    assert calls == 0


def test_web_search_transport_failure_is_transient_for_retry(authenticated_hub, monkeypatch):
    """Production web_search wraps httpx failures in ToolError; classify them for retry."""
    import httpx

    from unified_agent.tools import web as web_tools

    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def fail_ddg(query: str, max_results: int) -> list[dict]:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(web_tools, "_resolve_provider", lambda: "ddg")
    monkeypatch.setitem(web_tools._PROVIDERS, "ddg", fail_ddg)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    adapter = _adapter(web_tools.web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    _approve(pending.approval_id)

    with pytest.raises(TransientGatewayError, match="connection reset"):
        adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )

    monkeypatch.setitem(
        web_tools._PROVIDERS,
        "ddg",
        lambda query, max_results: [
            {"title": "Recovered", "url": "https://example.test", "snippet": ""}
        ],
    )
    allowed = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert allowed.status == "allowed"
    assert calls == 1

    conn = storage.connect()
    try:
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(effect["result_json"])["state"] == "succeeded"


def _recording_pool(monkeypatch):
    """Replace the dispatch pool with one that records a single shutdown call."""
    from unified_agent import langgraph_dispatch

    created: list[ThreadPoolExecutor] = []
    real_pool = langgraph_dispatch.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(self, wait=True, *, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(langgraph_dispatch, "ThreadPoolExecutor", _RecordingPool)
    return langgraph_dispatch, created


def _live_pool_threads(before: set[int | None]) -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.ident not in before
        and thread.name.startswith("ThreadPoolExecutor")
        and not thread.daemon
        and thread.is_alive()
    ]


def test_invoke_with_timeout_shuts_pool_on_success(monkeypatch):
    """A successful governed call must shut its worker pool down before returning.

    Returning from the try used to skip the else-clause shutdown, leaving the
    pool's non-daemon worker thread alive.
    """
    langgraph_dispatch, created = _recording_pool(monkeypatch)

    def _ok(**_kwargs):
        return {"ok": True}

    before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("ThreadPoolExecutor") and not thread.daemon
    }
    result = langgraph_dispatch._invoke_with_timeout(_ok, timeout_s=5)
    assert result == {"ok": True}
    assert len(created) == 1
    assert created[0].shutdown_calls == [(True, False)]
    assert _live_pool_threads(before) == []


def test_invoke_with_timeout_shuts_pool_on_tool_error(monkeypatch):
    """A non-timeout tool failure must join the worker instead of leaking it."""
    langgraph_dispatch, created = _recording_pool(monkeypatch)

    def _boom(**_kwargs):
        raise RuntimeError("connection reset")

    before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("ThreadPoolExecutor") and not thread.daemon
    }
    with pytest.raises(RuntimeError, match="connection reset"):
        langgraph_dispatch._invoke_with_timeout(_boom, timeout_s=5)
    assert len(created) == 1
    assert created[0].shutdown_calls == [(True, False)]
    assert _live_pool_threads(before) == []


def test_invoke_with_timeout_preserves_authenticated_context(authenticated_hub):
    """Thread-pool workers must inherit ContextVars for profile/policy revalidation."""
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    episode_token = config.episode_id_var.set("langgraph-dispatch-episode")

    def web_search(**_kwargs):
        assert config.current_episode_id() == "langgraph-dispatch-episode"
        return {"results": []}

    try:
        adapter = _adapter(web_search)
        idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
        pending = adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )
        _approve(pending.approval_id)
        allowed = adapter.call_read_tool(
            run=run,
            proposal=_proposal(),
            idempotency_key=idempotency_key,
            timeout_s=20.0,
        )
        assert allowed.status == "allowed"
    finally:
        config.episode_id_var.reset(episode_token)


def test_policy_approval_parks_a_read_without_failing_the_effect(
    authenticated_hub, tmp_path, monkeypatch
):

    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: search_gate\n    type: tool\n    tool: web_search\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    storage.init_db()
    task = _claimed_task()
    run = _run(task)
    calls = 0

    def web_search(**_kwargs):
        nonlocal calls
        calls += 1
        return {"results": []}

    adapter = _adapter(web_search)
    idempotency_key = f"ua-lg:{RUN_ID}:governed_tool:read-evidence"
    pending = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert pending.status == "pending"
    assert pending.approval_id
    assert calls == 0
    conn = storage.connect()
    try:
        effect = conn.execute(
            "SELECT 1 FROM idempotency_keys WHERE key=?", (idempotency_key,)
        ).fetchone()
        approval = conn.execute(
            "SELECT tool FROM approvals WHERE id=?", (int(pending.approval_id),)
        ).fetchone()
    finally:
        conn.close()
    assert effect is None
    assert approval["tool"] == "web_search"
    _approve(int(pending.approval_id))
    retry = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert retry.status == "pending"
    assert retry.approval_id
    assert retry.approval_id != pending.approval_id
    assert calls == 0
    conn = storage.connect()
    try:
        policy_row = conn.execute(
            "SELECT status, tool FROM approvals WHERE id=?", (int(pending.approval_id),)
        ).fetchone()
        adapter_row = conn.execute(
            "SELECT status, tool FROM approvals WHERE id=?", (int(retry.approval_id),)
        ).fetchone()
    finally:
        conn.close()
    assert policy_row["status"] == "approved"
    assert policy_row["tool"] == "web_search"
    assert adapter_row["status"] == "pending"
    assert adapter_row["tool"] == "langgraph_governed_call"
    _approve(int(retry.approval_id))
    allowed = adapter.call_read_tool(
        run=run,
        proposal=_proposal(),
        idempotency_key=idempotency_key,
        timeout_s=20.0,
    )
    assert allowed.status == "allowed"
    assert calls == 1
