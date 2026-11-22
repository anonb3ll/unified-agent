"""Locks the shared receipt ledger and the subclass claim-policy split."""

import sqlite3
from dataclasses import replace

import pytest

pytest.importorskip("langgraph")

from test_langgraph_dispatch import _claimed_task, _run

from unified_agent import config, gateway, policy, storage
from unified_agent import langgraph_dispatch as dispatch
from unified_agent import langgraph_receipt_dispatch as receipt_dispatch
from unified_agent.langgraph_note_dispatch import ProgressNoteDispatchAdapter
from unified_agent.langgraph_pilot import InvalidRunError, StaleAuthorityError
from unified_agent.langgraph_receipt_dispatch import ReceiptDispatchAdapter
from unified_agent.langgraph_workflow_dispatch import WorkflowDispatchAdapter
from unified_agent.tools.approvals import approval_resolve
from unified_agent.tools.tasks import task_update

DIGEST = "a" * 64
PLAN = "b" * 64
_SHARED = (
    "_native_receipt",
    "_require_unused_native_key",
    "_refresh_after_receipt",
    "_reconcile_effect",
    "_call_effect",
    "_write_effect",
)


@pytest.fixture
def authenticated_hub(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-langgraph-receipt-session")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def _pair(kind: str, writer=task_update):
    storage.init_db()
    task = _claimed_task()
    if kind == "note":
        run = replace(_run(task), required_capabilities=("task_update",), query=DIGEST)
        adapter = ProgressNoteDispatchAdapter(
            budget_revision=lambda _: "budget-v1", tool_registry={"task_update": writer}
        )
        return run, adapter
    run = replace(_run(task), required_capabilities=("task_update",), query="plan then merge")
    adapter = WorkflowDispatchAdapter(
        budget_revision=lambda _: "budget-v1", tool_registry={"task_update": writer}
    )
    return run, adapter


def _call(adapter, run, kind: str):
    if kind == "note":
        return adapter.call_progress_note(run=run, output_digest=DIGEST)
    return adapter.call_hop(run=run, hop="build", digest=PLAN)


def _effect_key(adapter, run, kind: str) -> str:
    if kind == "note":
        return adapter._key(run)
    return adapter._key(run, "build")


def _reconcile(adapter, run, kind: str):
    if kind == "note":
        return adapter.reconcile_progress_note(run=run, output_digest=DIGEST)
    return adapter.reconcile_hop(run=run, hop="build", digest=PLAN)


@pytest.fixture
def one_call_limit(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: update_cap\n    type: rate_limit\n    tool: task_update\n"
        "    max_calls: 1\n    window_seconds: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    gateway.reset_limits()
    _ = policy.load_policy()
    yield
    gateway.reset_limits()


def _approve_pending(adapter, run, kind: str) -> int:
    pending = _call(adapter, run, kind)
    assert pending.status == "pending"
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)
    return int(pending.approval_id)


def test_both_adapters_share_the_receipt_base():
    assert issubclass(ProgressNoteDispatchAdapter, ReceiptDispatchAdapter)
    assert issubclass(WorkflowDispatchAdapter, ReceiptDispatchAdapter)
    for name in _SHARED:
        shared = getattr(ReceiptDispatchAdapter, name)
        assert getattr(ProgressNoteDispatchAdapter, name) is shared
        assert getattr(WorkflowDispatchAdapter, name) is shared
    assert ProgressNoteDispatchAdapter._effect_noun == "progress"
    assert WorkflowDispatchAdapter._effect_noun == "workflow"
    assert "refresh_after_hop" not in ProgressNoteDispatchAdapter.__dict__
    assert "refresh_after_hop" in WorkflowDispatchAdapter.__dict__
    note_policy = ProgressNoteDispatchAdapter._after_restoring_pre_execute_approval
    workflow_policy = WorkflowDispatchAdapter._after_restoring_pre_execute_approval
    assert note_policy is not workflow_policy


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_receipt_validation_and_operation_key_keep_the_adapter_noun(authenticated_hub, kind):
    run, adapter = _pair(kind)
    noun = adapter._effect_noun
    native_key = f"{_effect_key(adapter, run, kind)}:task-update"
    envelope = {
        "arguments": {
            "idempotency_key": native_key,
            "append_note": "not the receipt",
            "expected_project": "unified-agent",
        }
    }
    adapter._require_unused_native_key(envelope)
    conn = storage.connect()
    conn.execute(
        "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?)",
        (native_key, "task_update", "{}", storage.now()),
    )
    conn.commit()
    conn.close()
    with pytest.raises(
        dispatch.DuplicateDeliveryUncertainError,
        match=f"native {noun} operation key already exists",
    ):
        adapter._require_unused_native_key(envelope)

    conn = storage.connect()
    conn.execute("DELETE FROM idempotency_keys WHERE key=?", (native_key,))
    conn.commit()
    conn.close()
    with pytest.raises(
        dispatch.DuplicateDeliveryUncertainError,
        match=f"native {noun} receipt absent or mismatched",
    ):
        adapter._native_receipt(run, envelope)
    conn = storage.connect()
    conn.execute(
        "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?)",
        (native_key, "task_update", "{}", storage.now()),
    )
    conn.commit()
    conn.close()
    with pytest.raises(
        dispatch.DuplicateDeliveryUncertainError,
        match=f"native {noun} receipt failed validation",
    ):
        adapter._native_receipt(run, envelope)

    conn = storage.connect()
    conn.execute(
        "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?)",
        (_effect_key(adapter, run, kind), "task_update", "{}", storage.now()),
    )
    conn.commit()
    conn.close()
    with pytest.raises(
        dispatch.DuplicateDeliveryUncertainError, match=f"{noun} operation key collision"
    ):
        _reconcile(adapter, run, kind)


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_stale_authority_refresh_is_shared(authenticated_hub, kind):
    run, adapter = _pair(kind)
    missing = replace(run, task_id=2_147_483_647)
    with pytest.raises(StaleAuthorityError, match="canonical task no longer exists"):
        adapter._refresh_after_receipt(
            missing, "receipt", changed_error="changed", revision_error="revision"
        )
    line = f"LangGraph shared receipt kind={kind}"
    conn = storage.connect()
    conn.execute("UPDATE tasks SET notes=? WHERE id=?", (line, run.task_id))
    conn.commit()
    conn.close()
    with pytest.raises(StaleAuthorityError, match="expected a new task revision"):
        adapter._refresh_after_receipt(
            run,
            line,
            changed_error="canonical task changed after the workflow hop",
            revision_error="expected a new task revision after the workflow hop",
        )
    task_update(run.task_id, append_note="foreign edit", worktree=False)
    with pytest.raises(StaleAuthorityError, match="changed after"):
        adapter._refresh_after_receipt(
            run,
            line,
            changed_error="canonical task changed after the workflow hop",
            revision_error="expected a new task revision after the workflow hop",
        )


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_timeout_noun_stays_on_the_shared_call(authenticated_hub, kind):
    run, adapter = _pair(kind)
    noun = adapter._effect_noun
    with pytest.raises(InvalidRunError, match=rf"{noun} timeout_s must be in \(0, 20\]"):
        if kind == "note":
            adapter.call_progress_note(run=run, output_digest=DIGEST, timeout_s=0)
        else:
            adapter.call_hop(run=run, hop="build", digest=PLAN, timeout_s=0)


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_pre_execute_failure_follows_the_subclass_claim_policy(
    authenticated_hub, monkeypatch, kind
):
    run, adapter = _pair(kind)
    approval_id = _approve_pending(adapter, run, kind)

    calls = {"count": 0}
    real_revalidate = adapter.revalidate

    def flaky_revalidate(run_arg):
        calls["count"] += 1
        if calls["count"] > 1:
            raise sqlite3.OperationalError("database is locked")
        return real_revalidate(run_arg)

    monkeypatch.setattr(adapter, "revalidate", flaky_revalidate)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _call(adapter, run, kind)

    key = _effect_key(adapter, run, kind)
    conn = storage.connect()
    try:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    assert approval["status"] == "approved"
    if kind == "note":
        assert dispatch._decode_effect(effect["result_json"])["state"] == "prepared"
    else:
        assert effect is None


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_gateway_commits_and_hands_off_before_the_write(
    authenticated_hub, one_call_limit, monkeypatch, kind
):
    events = []
    real_handoff = receipt_dispatch.mark_governed_execution

    def handoff(**kwargs):
        events.append(("handoff", gateway._CURRENT.get().committed, kwargs))
        return real_handoff(**kwargs)

    def writer(**kwargs):
        events.append(("write", gateway._CURRENT.get().committed, None))
        return task_update(**kwargs)

    monkeypatch.setattr(receipt_dispatch, "mark_governed_execution", handoff)
    run, adapter = _pair(kind, writer)
    _approve_pending(adapter, run, kind)
    # The pending park spent nothing.
    assert gateway.bucket_count() == 0
    assert events == []
    assert _call(adapter, run, kind).status == "allowed"
    assert events == [("handoff", True, {"handoff": True}), ("write", True, None)]
    # The committed write keeps its one slot.
    assert gateway.bucket_count() == 1


@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_gateway_refunds_a_pre_execute_failure_but_not_an_uncertain_write(
    authenticated_hub, one_call_limit, monkeypatch, kind
):
    writes = []

    def writer(**kwargs):
        writes.append(kwargs)
        raise RuntimeError("lost response")

    run, adapter = _pair(kind, writer)
    _approve_pending(adapter, run, kind)

    real_revalidate = adapter.revalidate
    calls = {"count": 0}

    def flaky_revalidate(run_arg):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_revalidate(run_arg)

    monkeypatch.setattr(adapter, "revalidate", flaky_revalidate)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _call(adapter, run, kind)
    assert writes == []
    # Nothing ran, so the reserved slot went back.
    assert gateway.bucket_count() == 0

    if kind == "note":
        # The note adapter retained its prepared claim; the write cannot be
        # invoked again, and the refused retry spends nothing either.
        with pytest.raises(dispatch.DuplicateDeliveryUncertainError, match="explicit recovery"):
            _call(adapter, run, kind)
        assert writes == []
        assert gateway.bucket_count() == 0
        return

    noun = adapter._effect_noun
    with pytest.raises(
        dispatch.DuplicateDeliveryUncertainError, match=f"{noun} write outcome uncertain"
    ):
        _call(adapter, run, kind)
    assert len(writes) == 1
    # The write was committed to run before its outcome was lost: no refund.
    assert gateway.bucket_count() == 1


@pytest.mark.parametrize("shared", ["receipt_dispatch", "base_dispatch"])
@pytest.mark.parametrize("kind", ["note", "workflow"])
def test_shared_receipt_source_pins_runner_definition_digest(tmp_path, monkeypatch, kind, shared):
    """A change to the shared base must change both runners' digests (#1660).

    Approval, receipt validation and the write path run in the shared module, so
    a checkpoint parked under the old base must not resume under a new one.
    """
    import inspect

    if kind == "note":
        from test_langgraph_note import Gateway

        from unified_agent.langgraph_note import ProgressNoteRunner as Runner
    else:
        from test_langgraph_workflow import Gateway

        from unified_agent.langgraph_workflow import WorkflowRunner as Runner

    with Runner(tmp_path / "first.db", Gateway()) as runner:
        original = runner.definition_digest

    real_getsource = inspect.getsource

    def edited_shared_source(obj):
        source = real_getsource(obj)
        module = receipt_dispatch if shared == "receipt_dispatch" else dispatch
        if obj is module or getattr(obj, "__module__", None) == module.__name__:
            return source + "\n# a later release edits the shared write path\n"
        return source

    monkeypatch.setattr(inspect, "getsource", edited_shared_source)
    with Runner(tmp_path / "second.db", Gateway()) as runner:
        assert runner.definition_digest != original
