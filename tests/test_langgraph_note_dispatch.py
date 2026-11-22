"""Local database acceptance checks for the single generated-note capability."""

import json
import threading
from dataclasses import replace

import pytest

pytest.importorskip("langgraph")

from test_langgraph_dispatch import _claimed_task, _run

from unified_agent import config, storage
from unified_agent import langgraph_dispatch as dispatch
from unified_agent.langgraph_note_dispatch import ProgressNoteDispatchAdapter
from unified_agent.langgraph_pilot import InvalidRunError, StaleAuthorityError
from unified_agent.tools.approvals import approval_resolve
from unified_agent.tools.tasks import task_update

DIGEST = "a" * 64


@pytest.fixture
def authenticated_hub(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("test-langgraph-note-session")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def setup(writer=task_update):
    storage.init_db()
    task = _claimed_task()
    run = replace(_run(task), required_capabilities=("task_update",), query=DIGEST)
    adapter = ProgressNoteDispatchAdapter(
        budget_revision=lambda _: "budget-v1", tool_registry={"task_update": writer}
    )
    return task, run, adapter


def approve(adapter, run):
    pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert pending.status == "pending"
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)
    return pending.approval_id


def read_task(run):
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (run.task_id,)).fetchone())
    finally:
        conn.close()


def test_approved_note_once_bounded_and_historical(authenticated_hub):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return task_update(**kwargs)

    task, run, adapter = setup(writer)
    approve(adapter, run)
    result = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert result.status == "allowed"
    assert result.result == {
        "task_id": run.task_id,
        "run_id": run.run_id,
        "output_digest": DIGEST,
        "note_appended": True,
    }
    current = read_task(run)
    for field in ("status", "claimed_by", "lock_expires_at", "worktree_path", "branch"):
        assert current[field] == task[field]
    assert current["notes"].count("LangGraph progress receipt") == 1
    assert "status" not in calls[0] and calls[0]["worktree"] is False
    task_update(run.task_id, append_note="Later unrelated edit", worktree=False)
    assert adapter.call_progress_note(run=run, output_digest=DIGEST) == result
    assert len(calls) == 1
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
        adapter.call_progress_note(run=replace(run, trace_id="forged"), output_digest=DIGEST)


def test_policy_and_adapter_approvals_both_park_then_note_writes_once(
    authenticated_hub, monkeypatch, tmp_path
):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return task_update(**kwargs)

    _, run, adapter = setup(writer)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "rules:\n  - name: note_gate\n    type: tool\n    tool: task_update\n"
        "    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))

    policy_pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert policy_pending.status == "pending"
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(policy_pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)

    adapter_pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert adapter_pending.status == "pending"
    assert adapter_pending.approval_id != policy_pending.approval_id
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT status FROM approvals WHERE id=?", (policy_pending.approval_id,)
            ).fetchone()[0]
            == "approved"
        )
    finally:
        conn.close()
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(adapter_pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)

    assert adapter.call_progress_note(run=run, output_digest=DIGEST).status == "allowed"
    assert len(calls) == 1
    assert read_task(run)["notes"].count("LangGraph progress receipt") == 1


def test_denial_remains_terminal(authenticated_hub):
    _, run, adapter = setup()
    pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
    approval_resolve(int(pending.approval_id), "deny", resolved_by="Operator", reason="No write")
    result = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert result.status == "denied"
    task_update(run.task_id, append_note="Later change", worktree=False)
    assert adapter.reconcile_progress_note(run=run, output_digest=DIGEST) == result
    assert "LangGraph progress receipt" not in read_task(run)["notes"]


@pytest.mark.parametrize("change", ["identity", "capability", "revision", "digest"])
def test_invalid_authority_cannot_write(authenticated_hub, change):
    _, run, adapter = setup()
    token = None
    if change == "identity":
        token = config.agent_identity_var.set("Desktop_Agent1")
    elif change == "capability":
        run = replace(run, required_capabilities=("web_search",))
    elif change == "revision":
        task_update(run.task_id, append_note="changed", worktree=False)
    try:
        with pytest.raises((StaleAuthorityError, InvalidRunError)):
            adapter.call_progress_note(
                run=run, output_digest="arbitrary note" if change == "digest" else DIGEST
            )
    finally:
        if token is not None:
            config.agent_identity_var.reset(token)
    assert "LangGraph progress receipt" not in read_task(run)["notes"]


@pytest.mark.parametrize("receipt_present", [True, False])
def test_lost_response_reconciles_only_native_receipt(authenticated_hub, receipt_present):
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        task_update(**kwargs)
        if not receipt_present:
            conn = storage.connect()
            conn.execute("DELETE FROM idempotency_keys WHERE key=?", (kwargs["idempotency_key"],))
            conn.commit()
            conn.close()
        raise OSError("lost response")

    _, run, adapter = setup(writer)
    approval_id = approve(adapter, run)
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
        adapter.call_progress_note(run=run, output_digest=DIGEST)
    if receipt_present:
        assert adapter.reconcile_progress_note(run=run, output_digest=DIGEST).status == "allowed"
    else:
        with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
            adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert len(calls) == 1
    conn = storage.connect()
    assert (
        conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
        == "consumed"
    )
    conn.close()


def test_native_key_collision_is_not_adopted(authenticated_hub):
    _, run, adapter = setup()
    conn = storage.connect()
    storage.record_idempotency(conn, adapter._key(run) + ":task-update", "task_update", {})
    conn.commit()
    conn.close()
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError, match="already exists"):
        adapter.call_progress_note(run=run, output_digest=DIGEST)


def test_timeout_worker_may_finish_but_is_not_reinvoked(authenticated_hub):
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        release.wait(5)
        try:
            return task_update(**kwargs)
        finally:
            finished.set()

    _, run, adapter = setup(writer)
    approve(adapter, run)
    try:
        with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
            adapter.call_progress_note(run=run, output_digest=DIGEST, timeout_s=0.01)
        with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
            adapter.call_progress_note(run=run, output_digest=DIGEST)
    finally:
        release.set()
        assert finished.wait(5)
    assert adapter.reconcile_progress_note(run=run, output_digest=DIGEST).status == "allowed"
    assert len(calls) == 1


def test_preinvoke_stale_restores_approval_without_write(authenticated_hub, monkeypatch):
    _, run, adapter = setup()
    approval_id = approve(adapter, run)
    original = dispatch.check_approval_gate

    def consume_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        # Another caller edits the task: run it outside this invocation's
        # context, as a separate request would, not nested inside its scope.
        editor = threading.Thread(
            target=task_update,
            args=(run.task_id,),
            kwargs={"append_note": "changed after approval", "worktree": False},
        )
        editor.start()
        editor.join(10)
        assert not editor.is_alive()
        assert "changed after approval" in read_task(run)["notes"]
        return result

    monkeypatch.setattr(dispatch, "check_approval_gate", consume_then_change)
    with pytest.raises(StaleAuthorityError):
        adapter.call_progress_note(run=run, output_digest=DIGEST)
    conn = storage.connect()
    assert (
        conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
        == "approved"
    )
    conn.close()
    assert "LangGraph progress receipt" not in read_task(run)["notes"]


def test_outer_commit_failure_recovers_and_bad_receipt_quarantines(authenticated_hub, monkeypatch):
    _, run, adapter = setup()
    approve(adapter, run)
    original = dispatch._transition_effect

    def fail_success(key, envelope, states, record):
        if record["state"] == "succeeded":
            raise OSError("outer commit unavailable")
        return original(key, envelope, states, record)

    monkeypatch.setattr(dispatch, "_transition_effect", fail_success)
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError):
        adapter.call_progress_note(run=run, output_digest=DIGEST)
    monkeypatch.setattr(dispatch, "_transition_effect", original)
    conn = storage.connect()
    key = adapter._key(run) + ":task-update"
    row = conn.execute("SELECT result_json FROM idempotency_keys WHERE key=?", (key,)).fetchone()
    receipt = json.loads(row[0])
    receipt["claimed_by"] = "Someone_else"
    conn.execute(
        "UPDATE idempotency_keys SET result_json=? WHERE key=?", (json.dumps(receipt), key)
    )
    conn.commit()
    conn.close()
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError, match="validation"):
        adapter.reconcile_progress_note(run=run, output_digest=DIGEST)
    receipt["claimed_by"] = run.claimed_by
    conn = storage.connect()
    conn.execute(
        "UPDATE idempotency_keys SET result_json=? WHERE key=?", (json.dumps(receipt), key)
    )
    conn.commit()
    conn.close()
    assert adapter.reconcile_progress_note(run=run, output_digest=DIGEST).status == "allowed"


@pytest.mark.parametrize("withdrawal", ["revoke", "expire"])
def test_withdrawn_approval_cannot_write(authenticated_hub, withdrawal):
    _, run, adapter = setup()
    approval_id = approve(adapter, run)
    if withdrawal == "revoke":
        approval_resolve(int(approval_id), "revoke", resolved_by="Operator")
    else:
        conn = storage.connect()
        conn.execute(
            "UPDATE approvals SET resolved_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", approval_id),
        )
        conn.commit()
        conn.close()
    outcome = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert outcome.status == "pending"
    assert outcome.approval_id != approval_id
    assert "LangGraph progress receipt" not in read_task(run)["notes"]


def test_crash_after_prepared_claim_requires_explicit_recovery(authenticated_hub):
    _, run, adapter = setup()
    snapshot = adapter.revalidate(run)
    envelope = adapter._envelope(run, DIGEST, snapshot.project)
    dispatch._claim_effect(adapter._key(run), envelope)
    with pytest.raises(dispatch.DuplicateDeliveryUncertainError, match="explicit recovery"):
        adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert "LangGraph progress receipt" not in read_task(run)["notes"]


def test_one_call_limit_refunds_pending_and_allows_the_approved_write(
    authenticated_hub, tmp_path, monkeypatch
):
    from unified_agent import gateway, policy

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
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        return task_update(**kwargs)

    _, run, adapter = setup(writer)
    pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert pending.status == "pending"
    assert gateway.bucket_count() == 0
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(int(pending.approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)
    result = adapter.call_progress_note(run=run, output_digest=DIGEST)
    assert result.status == "allowed"
    assert len(calls) == 1
