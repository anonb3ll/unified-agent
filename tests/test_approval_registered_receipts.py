"""Task #837: one-shot receipts must survive real registered child wrappers."""

import json
from contextlib import contextmanager

import conftest
import pytest

from unified_agent import config, storage
from unified_agent.tools import approvals
from unified_agent.tools.approvals import approval_pending, approval_resolve, get_approval


@pytest.fixture(scope="session", autouse=True)
def host_repo_unchanged():
    # This phase may not inventory other worktrees. Neither regression invokes
    # Git or creates a worktree; replace only conftest's Git inventory fixture.
    yield


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, tmp_path_factory, monkeypatch, _store_templates):
    # ensure_dirs otherwise loads the host's gemini.env even for database-only
    # tests. Reuse all normal isolation, with this unrelated credential loader
    # disabled before its first init_db call.
    monkeypatch.setattr(config, "load_gemini_env", lambda: None)
    from unified_agent import embeddings

    def no_model(*args, **kwargs):
        raise AssertionError("approval regressions must not invoke an embedding model")

    monkeypatch.setattr(embeddings, "generate_embedding", no_model)
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing-policy.yaml"))
    yield from conftest.isolated_env.__wrapped__(
        tmp_path=tmp_path,
        tmp_path_factory=tmp_path_factory,
        monkeypatch=monkeypatch,
        bootstrap_test_store=True,
        _store_templates=_store_templates,
    )


@contextmanager
def _identity(label):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _approve(approval_id):
    with _identity("Desktop_Person"):
        approval_resolve(int(approval_id), "approve")


def _require_approval(tmp_path, monkeypatch, tool):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        f"  - name: native_gate\n    type: tool\n    tool: {tool}\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("UA_APPROVAL_TOOLS", tool)


def _approval_rows():
    conn = storage.connect()
    try:
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in conn.execute("SELECT id,tool,payload,status FROM approvals ORDER BY id")
        ]
    finally:
        conn.close()


def _children():
    conn = storage.connect()
    try:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM tasks WHERE title='receipt-child' ORDER BY id")
        ]
    finally:
        conn.close()


def test_registered_workflow_one_native_approval_executes_one_child(tmp_path, monkeypatch):
    from unified_agent import server
    from unified_agent.tools import workflow

    _require_approval(tmp_path, monkeypatch, "task_add")
    definitions = tmp_path / "workflows"
    definitions.mkdir()
    # Definition location is the existing reviewed-definition test seam. The
    # engine, registered workflow/child, policy YAML and approval store are real.
    monkeypatch.setattr(workflow, "_workflows_dir", lambda: definitions)
    (definitions / "receipt-child.yaml").write_text(
        "name: receipt-child\nsteps:\n"
        "  - name: add\n    tool: task_add\n    args: {title: receipt-child}\n",
        encoding="utf-8",
    )

    def run():
        with _identity("Desktop_Agent2"):
            return server.REGISTERED_TOOLS["workflow_run"](
                name="receipt-child", author="Desktop_Agent2"
            )

    assert run()["status"] == "pending-approval"
    [native] = approval_pending()["pending"]
    assert native["tool"] == "task_add"
    assert native["payload"] == {"title": "receipt-child"}
    assert _children() == []
    _approve(native["id"])

    completed = run()
    evidence = {"result": completed, "approvals": _approval_rows(), "children": _children()}
    print("WORKFLOW_AFTER_ONE_APPROVAL", json.dumps(evidence, sort_keys=True))
    assert completed["status"] == "done", evidence
    assert len(_children()) == 1
    assert [(row["id"], row["tool"], row["status"]) for row in _approval_rows()] == [
        (native["id"], "task_add", "consumed")
    ]
    assert approval_pending()["pending"] == []

    # Receipts authorize only this child action, never a later workflow run.
    assert run()["status"] == "pending-approval"
    assert len(_children()) == 1
    [fresh] = approval_pending()["pending"]
    assert fresh["id"] != native["id"]
    assert fresh["tool"] == native["tool"]
    assert fresh["payload"] == native["payload"]
    assert get_approval(native["id"])["status"] == "consumed"
    assert len(_approval_rows()) == 2


def test_registered_langgraph_native_and_envelope_approvals_write_once(tmp_path, monkeypatch):
    from test_langgraph_note_dispatch import DIGEST, read_task, setup

    from unified_agent import server
    from unified_agent.langgraph_dispatch import DuplicateDeliveryUncertainError
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    # The shared claim helper normally tries to create a Git worktree. Keep
    # its real SQLite claim/lease while suppressing that unrelated OS action.
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: (None, None))
    session = config.mcp_session_id_var.set("patch837-note-local")
    try:
        with _identity("Desktop_Agent2"):
            # Reuse the real task/lease/run setup and its deterministic budget
            # revision seam, but supply the actual registered native writer.
            _, run, adapter = setup(server.REGISTERED_TOOLS["task_update"])
            task = read_task(run)
            _require_approval(tmp_path, monkeypatch, "task_update")

            native_pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
            assert native_pending.status == "pending"
            native = get_approval(int(native_pending.approval_id))
            assert native["tool"] == "task_update"
            assert read_task(run) == task
            _approve(native_pending.approval_id)

            envelope_pending = adapter.call_progress_note(run=run, output_digest=DIGEST)
            assert envelope_pending.status == "pending"
            assert envelope_pending.approval_id != native_pending.approval_id
            envelope = get_approval(int(envelope_pending.approval_id))
            assert envelope["tool"] == "langgraph_governed_call"
            assert envelope["payload"]["arguments"] == native["payload"]
            assert envelope["payload"] != native["payload"]
            assert get_approval(int(native_pending.approval_id))["status"] == "approved"
            assert read_task(run) == task
            assert len(_approval_rows()) == 2
            _approve(envelope_pending.approval_id)

            result = None
            error = None
            try:
                result = adapter.call_progress_note(run=run, output_digest=DIGEST)
            except DuplicateDeliveryUncertainError as exc:
                error = f"{type(exc).__name__}: {exc}; cause={type(exc.__cause__).__name__}"

            conn = storage.connect()
            try:
                effects = [
                    {
                        # Diagnostic JSON must represent internal SQLite
                        # BLOB keys explicitly; native TEXT aliases stay exact.
                        "key": (
                            {"sqlite_blob_hex": row["key"].hex()}
                            if isinstance(row["key"], bytes)
                            else row["key"]
                        ),
                        "tool": row["tool"],
                        "result": json.loads(row["result_json"]),
                    }
                    for row in conn.execute("SELECT key,tool,result_json FROM idempotency_keys")
                ]
            finally:
                conn.close()
            rows = _approval_rows()
            current = read_task(run)
            actual = {
                "status": result.status if result is not None else None,
                "error": error,
                "approvals": [(row["tool"], row["status"]) for row in rows],
                "pending": approval_pending()["pending"],
                "effect_states": [
                    effect["result"].get("state")
                    for effect in effects
                    if effect["tool"] == "langgraph_governed_effect"
                ],
                "note_count": current["notes"].count("LangGraph progress receipt"),
            }
            print(
                "NOTE_AFTER_TWO_DISTINCT_APPROVALS",
                json.dumps(
                    {"actual": actual, "approval_rows": rows, "effects": effects}, sort_keys=True
                ),
            )
            assert actual == {
                "status": "allowed",
                "error": None,
                "approvals": [("task_update", "consumed"), ("langgraph_governed_call", "consumed")],
                "pending": [],
                "effect_states": ["succeeded"],
                "note_count": 1,
            }
            assert result.result == {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "output_digest": DIGEST,
                "note_appended": True,
            }
            for field in ("status", "claimed_by", "lock_expires_at", "worktree_path", "branch"):
                assert current[field] == task[field]
            assert len([effect for effect in effects if effect["tool"] == "task_update"]) == 1

            # Historical duplicate delivery returns the receipt without another
            # mutation or a third native approval request.
            assert adapter.call_progress_note(run=run, output_digest=DIGEST) == result
            assert read_task(run) == current
            assert _approval_rows() == rows
            assert approval_pending()["pending"] == []
    finally:
        config.mcp_session_id_var.reset(session)


def _approved_gate(tool, payload):
    from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate

    with pytest.raises(ApprovalPendingError) as pending:
        check_approval_gate(tool, payload, force=True)
    _approve(pending.value.approval_id)
    return pending.value.approval_id


def test_explicit_join_uses_canonical_payload_and_verified_identity():
    from unified_agent.tools import approvals

    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", {"title": "bound", "priority": 1})
        with approvals.approval_invocation(
            "task_add", {"priority": 1, "title": "bound"}, requested_by="untrusted-outer"
        ) as outer:
            first = approvals.check_approval_gate(
                "task_add", {"title": "bound", "priority": 1}, force=True
            )
            with approvals.approval_invocation(
                "task_add",
                {"title": "bound", "priority": 1},
                requested_by="untrusted-child",
                join=True,
            ) as child:
                assert child is outer
                assert (
                    approvals.check_approval_gate(
                        "task_add", {"priority": 1, "title": "bound"}, force=True
                    )
                    == first
                )
                approvals.mark_governed_execution()
        assert get_approval(approval_id)["status"] == "consumed"


@pytest.mark.parametrize("boundary", ["tool", "payload", "identity", "missing_identity", "no_args"])
@pytest.mark.parametrize("handoff", [False, True])
def test_join_never_crosses_action_boundaries(boundary, handoff):
    from unified_agent.tools import approvals

    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload) as outer:
            approvals.check_approval_gate("task_add", payload, force=True)
            if handoff:
                approvals.mark_governed_execution(handoff=True)
            tool = "task_update" if boundary == "tool" else "task_add"
            child_payload = {"title": "different"} if boundary == "payload" else payload
            identity = (
                None
                if boundary == "missing_identity"
                else "Desktop_Agent4"
                if boundary == "identity"
                else "Desktop_Agent2"
            )
            with _identity(identity):
                scope = (
                    approvals.approval_invocation()
                    if boundary == "no_args"
                    else approvals.approval_invocation(
                        tool, child_payload, requested_by="Desktop_Agent2", join=True
                    )
                )
                with scope as child:
                    assert child is not outer
                    with pytest.raises(approvals.ApprovalPendingError):
                        # A forged requested_by cannot bridge verified identities.
                        approvals.check_approval_gate(
                            tool, child_payload, requested_by="Desktop_Agent2", force=True
                        )
            # A mismatched child must not take the rightful action's join.
            with approvals.approval_invocation("task_add", payload, join=True) as rightful:
                assert rightful is outer
                assert (
                    approvals.check_approval_gate("task_add", payload, force=True)["approval_id"]
                    == approval_id
                )
                approvals.mark_governed_execution()
        assert get_approval(approval_id)["status"] == "consumed"
        assert len(approval_pending()["pending"]) == 1


def test_repeated_registered_action_inside_scope_needs_fresh_approval(tmp_path, monkeypatch):
    from unified_agent import server
    from unified_agent.tools import approvals

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(*, title):
        calls.append(title)
        return {"ok": True}

    wrapped = server._guarded_and_instrumented(task_add)
    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        first_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload):
            approvals.check_approval_gate("task_add", payload, force=True)
            assert wrapped(**payload) == {"ok": True}
            with pytest.raises(approvals.ApprovalPendingError) as pending:
                wrapped(**payload)
            assert calls == ["bound"]
            assert pending.value.approval_id != first_id
            assert get_approval(first_id)["status"] == "consumed"
            _approve(pending.value.approval_id)
            assert wrapped(**payload) == {"ok": True}
            with pytest.raises(approvals.ApprovalPendingError):
                wrapped(**payload)
        assert calls == ["bound", "bound"]
        assert get_approval(first_id)["status"] == "consumed"


def test_joined_wrapper_denial_before_bare_execution_restores_receipt(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import profiles, server
    from unified_agent.tools import approvals

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(*, title):
        calls.append(title)

    wrapped = server._guarded_and_instrumented(task_add)
    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        monkeypatch.setattr(profiles, "tool_is_allowed", lambda _: False)
        with pytest.raises(ToolError), approvals.approval_invocation("task_add", payload) as scope:
            approvals.check_approval_gate("task_add", payload, force=True)
            wrapped(**payload)
        assert approvals.failed_before_execution(scope)
        assert calls == []
        assert get_approval(approval_id)["status"] == "approved"


@pytest.mark.parametrize("error", ["tool_error", "pending"])
def test_joined_wrapper_failure_after_bare_entry_never_restores(tmp_path, monkeypatch, error):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import server
    from unified_agent.tools import approvals

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(*, title):
        calls.append(title)
        if error == "pending":
            approvals.check_approval_gate("unknown_inner_write", {"title": title}, force=True)
        raise ToolError("write outcome uncertain")

    wrapped = server._guarded_and_instrumented(task_add)
    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with pytest.raises(ToolError), approvals.approval_invocation("task_add", payload) as scope:
            approvals.check_approval_gate("task_add", payload, force=True)
            wrapped(**payload)
        assert not approvals.failed_before_execution(scope)
        assert calls == ["bound"]
        assert get_approval(approval_id)["status"] == "consumed"


@pytest.mark.parametrize("handoff", [False, True])
def test_execution_handoff_is_explicit_and_cannot_be_rearmed(handoff):
    from unified_agent.tools import approvals

    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload) as outer:
            approvals.check_approval_gate("task_add", payload, force=True)
            approvals.mark_governed_execution(handoff=handoff)
            with approvals.approval_invocation("task_add", payload, join=True) as child:
                assert (child is outer) is handoff
                if handoff:
                    assert (
                        approvals.check_approval_gate("task_add", payload, force=True)[
                            "approval_id"
                        ]
                        == approval_id
                    )
                else:
                    with pytest.raises(approvals.ApprovalPendingError):
                        approvals.check_approval_gate("task_add", payload, force=True)
            approvals.mark_governed_execution(handoff=True)
            with approvals.approval_invocation("task_add", payload, join=True) as repeat:
                assert repeat is not outer
                with pytest.raises(approvals.ApprovalPendingError):
                    approvals.check_approval_gate("task_add", payload, force=True)
        assert get_approval(approval_id)["status"] == "consumed"


def test_preexecution_join_is_claimed_once():
    from unified_agent.tools import approvals

    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload) as outer:
            approvals.check_approval_gate("task_add", payload, force=True)
            with approvals.approval_invocation("task_add", payload, join=True) as first:
                assert first is outer
            with approvals.approval_invocation("task_add", payload, join=True) as second:
                assert second is not outer
                with pytest.raises(approvals.ApprovalPendingError):
                    approvals.check_approval_gate("task_add", payload, force=True)
            approvals.mark_governed_execution()
        assert get_approval(approval_id)["status"] == "consumed"


@pytest.fixture
def registered_adapter_session(monkeypatch):
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: (None, None))
    session = config.mcp_session_id_var.set("patch837-adapter-local")
    try:
        with _identity("Desktop_Agent2"):
            yield
    finally:
        config.mcp_session_id_var.reset(session)


def test_registered_langgraph_workflow_each_hop_has_native_and_envelope_approval(
    registered_adapter_session, tmp_path, monkeypatch
):
    from test_langgraph_workflow_dispatch import PLAN, VERIFY, read_task, setup

    from unified_agent import server

    _, origin, adapter = setup(server.REGISTERED_TOOLS["task_update"])
    _require_approval(tmp_path, monkeypatch, "task_update")
    run = origin
    for hop, digest in [("build", PLAN), ("merge", VERIFY)]:
        before = read_task(run)
        native = adapter.call_hop(run=run, hop=hop, digest=digest)
        assert native.status == "pending"
        assert get_approval(int(native.approval_id))["tool"] == "task_update"
        _approve(native.approval_id)
        envelope = adapter.call_hop(run=run, hop=hop, digest=digest)
        assert envelope.status == "pending"
        assert envelope.approval_id != native.approval_id
        assert get_approval(int(native.approval_id))["status"] == "approved"
        assert get_approval(int(envelope.approval_id))["tool"] == "langgraph_governed_call"
        assert read_task(run) == before
        _approve(envelope.approval_id)
        result = adapter.call_hop(run=run, hop=hop, digest=digest)
        assert result.status == "allowed"
        assert read_task(run)["notes"].count(f"LangGraph workflow {hop}") == 1
        assert get_approval(int(native.approval_id))["status"] == "consumed"
        assert get_approval(int(envelope.approval_id))["status"] == "consumed"
        assert adapter.call_hop(run=run, hop=hop, digest=digest) == result
        assert approval_pending()["pending"] == []
        if hop == "build":
            run = adapter.refresh_after_hop(run, hop, digest)
    assert len(_approval_rows()) == 4


def test_langgraph_read_registered_wrapper_joins_native_gate(
    registered_adapter_session, tmp_path, monkeypatch
):
    from test_langgraph_dispatch import RUN_ID, _adapter, _claimed_task, _proposal, _run

    from unified_agent import server

    calls = []

    def web_search(**kwargs):
        calls.append(kwargs)
        return {"results": []}

    run = _run(_claimed_task())
    adapter = _adapter(server._guarded_and_instrumented(web_search))
    _require_approval(tmp_path, monkeypatch, "web_search")
    args = dict(
        run=run,
        proposal=_proposal(),
        idempotency_key=f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
        timeout_s=20,
    )
    native = adapter.call_read_tool(**args)
    assert native.status == "pending"
    _approve(native.approval_id)
    envelope = adapter.call_read_tool(**args)
    assert envelope.status == "pending"
    assert get_approval(int(native.approval_id))["status"] == "approved"
    assert envelope.approval_id != native.approval_id
    assert calls == []
    _approve(envelope.approval_id)
    result = adapter.call_read_tool(**args)
    assert result.status == "allowed"
    assert len(calls) == 1
    assert [(row["tool"], row["status"]) for row in _approval_rows()] == [
        ("web_search", "consumed"),
        ("langgraph_governed_call", "consumed"),
    ]
    assert adapter.call_read_tool(**args) == result
    assert len(calls) == 1


@pytest.mark.parametrize("adapter_kind", ["note", "workflow"])
@pytest.mark.parametrize("phase", ["before_execution", "after_execution"])
def test_native_and_envelope_receipts_restore_only_before_execution(
    registered_adapter_session, tmp_path, monkeypatch, adapter_kind, phase
):
    from mcp.server.mcpserver.exceptions import ToolError
    from test_langgraph_note_dispatch import DIGEST
    from test_langgraph_note_dispatch import setup as note_setup
    from test_langgraph_workflow_dispatch import PLAN
    from test_langgraph_workflow_dispatch import setup as workflow_setup

    from unified_agent import server
    from unified_agent.langgraph_dispatch import DuplicateDeliveryUncertainError
    from unified_agent.langgraph_pilot import StaleAuthorityError
    from unified_agent.tools import tasks

    calls = []
    real_update = tasks.task_update

    def task_update(**kwargs):
        calls.append(kwargs)
        # Mutate without a committed native receipt: reconciliation must not
        # infer safety from the exception or automatically replay this write.
        mutation = {key: value for key, value in kwargs.items() if key != "idempotency_key"}
        real_update(**mutation)
        raise ToolError("uncertain after native mutation")

    writer = (
        server.REGISTERED_TOOLS["task_update"]
        if phase == "before_execution"
        else server._guarded_and_instrumented(task_update)
    )
    _, run, adapter = note_setup(writer) if adapter_kind == "note" else workflow_setup(writer)
    invoke = (
        (lambda: adapter.call_progress_note(run=run, output_digest=DIGEST))
        if (adapter_kind == "note")
        else (lambda: adapter.call_hop(run=run, hop="build", digest=PLAN))
    )
    _require_approval(tmp_path, monkeypatch, "task_update")
    native = invoke()
    _approve(native.approval_id)
    envelope = invoke()
    _approve(envelope.approval_id)
    if phase == "before_execution":
        real_revalidate = adapter.revalidate
        checks = []

        def revalidate(current):
            checks.append(current)
            if len(checks) == 2:
                raise StaleAuthorityError("pre-execution authority changed")
            return real_revalidate(current)

        monkeypatch.setattr(adapter, "revalidate", revalidate)
        with pytest.raises(StaleAuthorityError):
            invoke()
        assert calls == []
        expected = "approved"
    else:
        with pytest.raises(DuplicateDeliveryUncertainError):
            invoke()
        assert len(calls) == 1
        with pytest.raises(DuplicateDeliveryUncertainError):
            invoke()
        assert len(calls) == 1
        expected = "consumed"
    assert [(row["tool"], row["status"]) for row in _approval_rows()] == [
        ("task_update", expected),
        ("langgraph_governed_call", expected),
    ]


@pytest.mark.parametrize("boundary", ["tool", "payload", "identity"])
def test_bound_join_cannot_reuse_unrelated_outer_receipts(boundary):
    from unified_agent.tools import approvals

    payload = {"title": "bound"}
    other_tool = "langgraph_governed_call" if boundary == "tool" else "task_add"
    other_payload = {"arguments": payload} if boundary == "payload" else payload
    other_identity = "Desktop_Agent4" if boundary == "identity" else "Desktop_Agent2"
    with _identity("Desktop_Agent2"):
        native_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload):
            approvals.check_approval_gate("task_add", payload, force=True)
            # This independently consumed pass must remain separate even when
            # the native action hands its own receipt to a registered child.
            with _identity(other_identity):
                other_id = _approved_gate(other_tool, other_payload)
                approvals.check_approval_gate(other_tool, other_payload, force=True)
            with approvals.approval_invocation("task_add", payload, join=True):
                with _identity(other_identity), pytest.raises(approvals.ApprovalPendingError):
                    approvals.check_approval_gate(other_tool, other_payload, force=True)
                approvals.mark_governed_execution()
        assert get_approval(native_id)["status"] == "consumed"
        assert get_approval(other_id)["status"] == "consumed"


def test_registered_positional_arguments_cannot_join_an_incomplete_binding(tmp_path, monkeypatch):
    from unified_agent import server
    from unified_agent.tools import approvals

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(title):
        calls.append(title)
        return {"ok": True}

    wrapped = server._guarded_and_instrumented(task_add)
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", {})
        with approvals.approval_invocation("task_add", {}) as outer:
            approvals.check_approval_gate("task_add", {}, force=True)
            with pytest.raises(approvals.ApprovalPendingError):
                wrapped("not in the bound payload")
            assert calls == []
            # The incomplete child did not take the enclosing join or execute.
            with approvals.approval_invocation("task_add", {}, join=True) as rightful:
                assert rightful is outer
                approvals.mark_governed_execution()
        assert get_approval(approval_id)["status"] == "consumed"


# Independent review regressions: exact authority and lifecycle boundaries.
def test_independent_positional_live_incomplete_approval(tmp_path, monkeypatch):
    from unified_agent import server

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(title):
        calls.append(title)
        return {"ok": True}

    wrapped = server._guarded_and_instrumented(task_add)
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", {})
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            wrapped("never included in approved payload")
        assert calls == []
        assert approvals.get_approval(approval_id)["status"] == "approved"
        assert approvals.get_approval(pending.value.approval_id)["payload"] == {
            "title": "never included in approved payload"
        }
        _approve(pending.value.approval_id)
        assert wrapped("never included in approved payload") == {"ok": True}
        assert calls == ["never included in approved payload"]
        assert approvals.get_approval(approval_id)["status"] == "approved"
        print("POSITIONAL complete payload consumed; incomplete approval unspent")


def test_independent_joined_rollback_followed_by_independent_execution(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import safety, server

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(*, title):
        calls.append(title)
        return {"ok": True}

    wrapped = server._guarded_and_instrumented(task_add)
    original = safety.check_policy_gate
    fail = [True]

    def policy(tool, payload):
        if fail[0]:
            raise ToolError("before bare entry")
        return original(tool, payload)

    monkeypatch.setattr(safety, "check_policy_gate", policy)
    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with pytest.raises(RuntimeError):
            with approvals.approval_invocation("task_add", payload):
                approvals.check_approval_gate("task_add", payload, force=True)
                with pytest.raises(ToolError):
                    wrapped(**payload)
                assert approvals.get_approval(approval_id)["status"] == "approved"
                fail[0] = False
                assert wrapped(**payload) == {"ok": True}
                assert approvals.get_approval(approval_id)["status"] == "consumed"
                raise RuntimeError("outer fails after independent child already executed")
        restored_status = approvals.get_approval(approval_id)["status"]
        with pytest.raises(approvals.ApprovalPendingError):
            wrapped(**payload)
        assert restored_status == "consumed"
        print(
            "ROLLBACK_AFTER_WRITE",
            calls,
            "status_before_replay=" + restored_status,
            "status_after_replay=" + approvals.get_approval(approval_id)["status"],
        )
        assert calls == ["bound"]


def test_independent_copied_context_replays_restored_receipt(tmp_path, monkeypatch):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from unified_agent import server

    _require_approval(tmp_path, monkeypatch, "task_add")
    calls = []

    def task_add(*, title):
        calls.append(title)
        return {"ok": True}

    wrapped = server._guarded_and_instrumented(task_add)
    payload = {"title": "bound"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with pytest.raises(RuntimeError):
            with approvals.approval_invocation("task_add", payload):
                approvals.check_approval_gate("task_add", payload, force=True)
                copied = contextvars.copy_context()
                raise RuntimeError("pre-execution outer failure")
        assert approvals.get_approval(approval_id)["status"] == "approved"
        assert wrapped(**payload) == {"ok": True}
        assert approvals.get_approval(approval_id)["status"] == "consumed"
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(approvals.ApprovalPendingError):
                pool.submit(copied.run, lambda: wrapped(**payload)).result()
        print("COPIED_CONTEXT_REPLAY", calls, approvals.get_approval(approval_id)["status"])
        assert calls == ["bound"]


def test_independent_unbound_passes_overwritten_on_rollback():
    from mcp.server.mcpserver.exceptions import ToolError

    with _identity("Desktop_Agent2"):
        first = _approved_gate("other", {"p": 1})
        # Insert another pending pass only after consuming the first.
        with pytest.raises(ToolError):
            with approvals.approval_invocation("bound", {"p": 2}):
                approvals.check_approval_gate("other", {"p": 1}, force=True)
                second = _approved_gate("other", {"p": 1})
                approvals.check_approval_gate("other", {"p": 1}, force=True)
                raise ToolError("no execution occurred")
        actual = [approvals.get_approval(i)["status"] for i in (first, second)]
        print("OVERWRITTEN_ROLLBACK", actual)
        assert actual == ["approved", "approved"]


@pytest.mark.parametrize("executed", [False, True])
def test_closed_success_scope_cannot_authorize_a_copied_context(executed):
    import contextvars

    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        with approvals.approval_invocation("bound", payload):
            approvals.check_approval_gate("bound", payload, force=True)
            if executed:
                approvals.mark_governed_execution(handoff=True)
            copied = contextvars.copy_context()
        with pytest.raises(approvals.ApprovalPendingError):
            copied.run(approvals.check_approval_gate, "bound", payload, force=True)
        assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_live_joined_worker_retains_receipt_after_owner_closes():
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, proceed = Event(), Event()
    payload = {"p": 1}
    with _identity("Desktop_Agent2"), ThreadPoolExecutor(max_workers=1) as pool:
        approval_id = _approved_gate("bound", payload)

        def worker():
            with approvals.approval_invocation("bound", payload, join=True):
                entered.set()
                assert proceed.wait(5)
                receipt = approvals.check_approval_gate("bound", payload, force=True)
                approvals.mark_governed_execution()
                return receipt

        with approvals.approval_invocation("bound", payload):
            approvals.check_approval_gate("bound", payload, force=True)
            approvals.mark_governed_execution(handoff=True)
            copied = contextvars.copy_context()
            future = pool.submit(copied.run, worker)
            assert entered.wait(5)
        proceed.set()
        assert future.result(timeout=5)["approval_id"] == approval_id
        with pytest.raises(approvals.ApprovalPendingError):
            copied.run(approvals.check_approval_gate, "bound", payload, force=True)
        assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_stale_explicit_restore_cannot_restore_a_later_consumption():
    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        with approvals.approval_invocation("bound", payload):
            approvals.check_approval_gate("bound", payload, force=True)
            approvals.restore_consumed_approval(approval_id)
            with approvals.approval_invocation("bound", payload):
                approvals.check_approval_gate("bound", payload, force=True)
                approvals.mark_governed_execution()
            approvals.restore_consumed_approval(approval_id)
        assert approvals.get_approval(approval_id)["status"] == "consumed"


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    """Real native signatures/gates; subprocess and delegation effects are stubs."""
    import hashlib
    import importlib
    from types import SimpleNamespace

    from unified_agent import server
    from unified_agent.tools import files, memory, shell, tasks

    delegation = importlib.import_module("unified_agent.tools.delegate")
    effects = []

    def command(argv, **kwargs):
        effects.append(("command", argv))
        return SimpleNamespace(returncode=0, stdout="harmless\n", stderr="")

    def task(**kwargs):
        effects.append(("delegation", kwargs))
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO tasks (id,title,status,priority,created_at,updated_at) "
                "VALUES (123,'mock delegation effect','open','med',?,?)",
                (storage.now(), storage.now()),
            )
            conn.commit()
        finally:
            conn.close()
        return {"id": 123}

    monkeypatch.setattr(shell.subprocess, "run", command)
    monkeypatch.setattr(tasks, "task_add", task)
    monkeypatch.setattr(memory, "memory_save", lambda **kwargs: {"id": None})
    monkeypatch.setattr(config, "sandbox_enabled", lambda: False)
    specs = {
        "run_cmd": (
            shell.run_cmd,
            {"command": "echo harmless"},
            {"command": "echo harmless", "cwd": "."},
        ),
        "fs_write": (
            files.fs_write,
            {"path": "harmless.txt", "content": "harmless"},
            {
                "path": "harmless.txt",
                "append": False,
                "content_sha256": hashlib.sha256(b"harmless").hexdigest(),
                "bytes": 8,
            },
        ),
        "delegate": (
            delegation.delegate,
            {
                "task_spec": "harmless isolated spec",
                "from_agent": "Desktop_Agent2",
                "to_agent": "Desktop_Agent1",
                "callback": "none",
            },
            {
                "task_spec": "harmless isolated spec",
                "from_agent": "Desktop_Agent2",
                "to_agent": "Desktop_Agent1",
                "deadline": None,
                "callback": "none",
                "priority": "med",
                "parent_task_id": None,
            },
        ),
    }

    def case(name):
        fn, supplied, native = specs[name]
        wrapped = server.REGISTERED_TOOLS[name]
        # Prove the registered callable wraps the actual native function.
        import inspect

        assert inspect.unwrap(wrapped) is fn
        return wrapped, supplied, native, effects

    return case


@pytest.mark.parametrize("name", ["run_cmd", "fs_write", "delegate"])
@pytest.mark.parametrize("positional", [False, True])
def test_native_only_gate_accepts_kwargs_defaults_and_positional(
    native_case, monkeypatch, name, positional
):
    wrapped, supplied, native, effects = native_case(name)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", name)
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate(name, native)
        if positional:
            # Supply all required parameters by position; defaults remain native.
            values = dict(supplied)
            callback = values.pop("callback", None)
            wrapped(*values.values(), **({"callback": callback} if callback else {}))
        else:
            wrapped(**supplied)
        assert get_approval(approval_id)["status"] == "consumed"
        assert len(_approval_rows()) == 1
        if name == "fs_write":
            assert (config.workspace_root() / "harmless.txt").read_text() == "harmless"
        else:
            assert len(effects) == 1


@pytest.mark.parametrize("name", ["run_cmd", "fs_write", "delegate"])
def test_native_wrapper_and_self_gate_distinct_policies_restore_before_execution(
    native_case, tmp_path, monkeypatch, name
):
    wrapped, supplied, native, effects = native_case(name)
    _require_approval(tmp_path, monkeypatch, name)
    with _identity("Desktop_Agent2"):
        wrapper_id = _approved_gate(name, supplied)
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            wrapped(**supplied)
        assert get_approval(pending.value.approval_id)["payload"] == native
        assert get_approval(wrapper_id)["status"] == "approved"
        assert effects == []
        assert not (config.workspace_root() / "harmless.txt").exists()
        _approve(pending.value.approval_id)
        wrapped(**supplied)
        assert get_approval(wrapper_id)["status"] == "consumed"
        assert get_approval(pending.value.approval_id)["status"] == "consumed"
        assert len(_approval_rows()) == 2
        with pytest.raises(approvals.ApprovalPendingError):
            wrapped(**supplied)
        if name != "fs_write":
            assert len(effects) == 1


@pytest.mark.parametrize("name", ["run_cmd", "fs_write", "delegate"])
def test_native_positional_cannot_spend_incomplete_wrapper_approval(
    native_case, tmp_path, monkeypatch, name
):
    wrapped, supplied, native, effects = native_case(name)
    _require_approval(tmp_path, monkeypatch, name)
    with _identity("Desktop_Agent2"):
        incomplete_id = _approved_gate(name, {})
        values = dict(supplied)
        callback = values.pop("callback", None)
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            wrapped(*values.values(), **({"callback": callback} if callback else {}))
        assert get_approval(pending.value.approval_id)["payload"] == supplied
        assert get_approval(incomplete_id)["status"] == "approved"
        assert effects == []
        assert not (config.workspace_root() / "harmless.txt").exists()
        with pytest.raises(TypeError):
            wrapped()
        assert get_approval(incomplete_id)["status"] == "approved"


def test_native_run_cmd_same_wrapper_payload_spends_one_approval(
    native_case, tmp_path, monkeypatch
):
    wrapped, supplied, native, effects = native_case("run_cmd")
    _require_approval(tmp_path, monkeypatch, "run_cmd")
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("run_cmd", native)
        wrapped(**native)
        assert get_approval(approval_id)["status"] == "consumed"
        assert len(_approval_rows()) == len(effects) == 1


def test_native_failure_before_gate_restores_wrapper_pass(native_case, tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    wrapped, _, _, effects = native_case("run_cmd")
    _require_approval(tmp_path, monkeypatch, "run_cmd")
    payload = {"command": "curl forbidden"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("run_cmd", payload)
        with pytest.raises(ToolError, match="allowlist"):
            wrapped(**payload)
        assert get_approval(approval_id)["status"] == "approved"
        assert effects == []


def test_native_failure_after_gate_keeps_wrapper_and_native_passes_spent(
    native_case, tmp_path, monkeypatch
):
    from unified_agent.tools import shell

    wrapped, supplied, native, effects = native_case("run_cmd")
    _require_approval(tmp_path, monkeypatch, "run_cmd")

    def uncertain(*args, **kwargs):
        effects.append("uncertain")
        raise RuntimeError("uncertain command effect")

    monkeypatch.setattr(shell.subprocess, "run", uncertain)
    with _identity("Desktop_Agent2"):
        wrapper_id = _approved_gate("run_cmd", supplied)
        native_id = _approved_gate("run_cmd", native)
        with pytest.raises(RuntimeError, match="uncertain"):
            wrapped(**supplied)
        assert effects == ["uncertain"]
        assert [get_approval(i)["status"] for i in (wrapper_id, native_id)] == ["consumed"] * 2


def test_closed_context_cannot_explicitly_restore_a_spent_consumption():
    import contextvars

    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        with approvals.approval_invocation("bound", payload):
            approvals.check_approval_gate("bound", payload, force=True)
            approvals.mark_governed_execution()
            copied = contextvars.copy_context()
        copied.run(approvals.restore_consumed_approval, approval_id)
        assert get_approval(approval_id)["status"] == "consumed"


@pytest.mark.parametrize("operator", ["label", "alias"])
def test_native_human_delegation_gate_bypass_still_spends_on_uncertain_effect(
    native_case, tmp_path, monkeypatch, operator
):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import tasks

    # Task #2535: the wrapper's human-delegation seam uses the same verified
    # operator definition as delegate() itself, so an alias spends alike.
    human = config.operator_label() if operator == "label" else sorted(config.operator_aliases())[0]
    wrapped, supplied, _, effects = native_case("delegate")
    supplied["from_agent"] = human
    _require_approval(tmp_path, monkeypatch, "delegate")

    def uncertain(**kwargs):
        effects.append("uncertain human delegation")
        raise ToolError("uncertain delegation effect")

    monkeypatch.setattr(tasks, "task_add", uncertain)
    with _identity(human):
        approval_id = _approved_gate("delegate", supplied)
        with pytest.raises(ToolError, match="uncertain"):
            wrapped(**supplied)
        assert effects == ["uncertain human delegation"]
        assert get_approval(approval_id)["status"] == "consumed"


def test_unbound_invocation_preserves_internal_same_gate_rechecks():
    # Existing special wrappers have isolated, unbound scopes and recheck the
    # same gate in their native body. They still spend one pass per invocation.
    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        with approvals.approval_invocation():
            first = approvals.check_approval_gate("bound", payload, force=True)
            assert approvals.check_approval_gate("bound", payload, force=True) == first
            approvals.mark_governed_execution()
        assert get_approval(approval_id)["status"] == "consumed"
        with approvals.approval_invocation(), pytest.raises(approvals.ApprovalPendingError):
            approvals.check_approval_gate("bound", payload, force=True)


def test_consumption_generation_survives_unscoped_restore_with_frozen_clock(monkeypatch):
    import contextvars
    from datetime import UTC, datetime

    frozen = datetime.now(UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(approvals, "datetime", FrozenDatetime)
    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        with pytest.raises(RuntimeError):
            with approvals.approval_invocation("bound", payload):
                approvals.check_approval_gate("bound", payload, force=True)
                # Existing unscoped adapter restoration must retain the DB
                # generation so an old owner cannot undo a later consumption.
                contextvars.Context().run(approvals.restore_consumed_approval, approval_id)
                with approvals.approval_invocation("bound", payload):
                    approvals.check_approval_gate("bound", payload, force=True)
                    approvals.mark_governed_execution()
                raise RuntimeError("stale owner fails after later execution")
        assert get_approval(approval_id)["status"] == "consumed"


def test_stale_selected_approval_cannot_reconsume_a_restored_generation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from test_approvals import _capture_expected_error, _pause_next_approval_read

    payload = {"p": 1}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("bound", payload)
        selected, release = _pause_next_approval_read(
            monkeypatch, "SELECT * FROM approvals WHERE tool=?"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            stale = pool.submit(
                _capture_expected_error,
                lambda: approvals.check_approval_gate("bound", payload, force=True),
            )
            assert selected.wait(timeout=5)
            approvals.check_approval_gate("bound", payload, force=True)
            approvals.restore_consumed_approval(approval_id)
            release.set()
            outcome = stale.result(timeout=5)
        assert isinstance(outcome, approvals.ApprovalPendingError)
        assert get_approval(approval_id)["status"] == "approved"


@pytest.mark.parametrize("name", ["run_cmd", "fs_write", "delegate"])
def test_registered_workflow_native_defaults_spend_one_native_approval(
    native_case, tmp_path, monkeypatch, name
):
    import yaml

    from unified_agent import server
    from unified_agent.tools import workflow

    _, supplied, native, effects = native_case(name)
    definitions = tmp_path / "native-workflows"
    definitions.mkdir()
    monkeypatch.setattr(workflow, "_workflows_dir", lambda: definitions)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", name)
    (definitions / "native-receipt.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "native-receipt",
                "steps": [{"name": "native", "tool": name, "args": supplied}],
            }
        )
    )
    with _identity("Desktop_Agent2"):
        call = server.REGISTERED_TOOLS["workflow_run"]
        assert call(name="native-receipt", author="Desktop_Agent2")["status"] == "pending-approval"
        [row] = _approval_rows()
        assert row["payload"] == native
        assert row["status"] == "pending"
        assert effects == []
        _approve(row["id"])
        assert call(name="native-receipt", author="Desktop_Agent2")["status"] == "done"
        assert [(r["tool"], r["status"]) for r in _approval_rows()] == [(name, "consumed")]
        if name == "fs_write":
            assert (config.workspace_root() / "harmless.txt").read_text() == "harmless"
        else:
            assert len(effects) == 1
        # A new workflow action must request a fresh pass.
        assert call(name="native-receipt", author="Desktop_Agent2")["status"] == "pending-approval"
        assert len(_approval_rows()) == 2


def test_joined_child_clears_parent_cache_preventing_repeated_or_nested_reuse():
    from unified_agent.tools import approvals

    payload = {"title": "no-reuse"}
    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("task_add", payload)
        with approvals.approval_invocation("task_add", payload) as outer:
            approvals.check_approval_gate("task_add", payload, force=True)
            with approvals.approval_invocation("task_add", payload, join=True) as child:
                assert child is outer
                assert (
                    approvals.check_approval_gate("task_add", payload, force=True)["approval_id"]
                    == approval_id
                )
                approvals.mark_governed_execution()
                # Execution started: the pass is spent and child cannot reuse it
                with pytest.raises(approvals.ApprovalPendingError):
                    approvals.check_approval_gate("task_add", payload, force=True)
            # Repeated child in parent cannot reuse the spent pass
            with approvals.approval_invocation("task_add", payload, join=True) as repeat:
                assert repeat is not outer
                with pytest.raises(approvals.ApprovalPendingError):
                    approvals.check_approval_gate("task_add", payload, force=True)
            with pytest.raises(approvals.ApprovalPendingError):
                approvals.check_approval_gate("task_add", payload, force=True)
        assert get_approval(approval_id)["status"] == "consumed"


def test_registered_conditional_fs_write_preflight_refusal_restores_policy_and_native_approvals(
    tmp_path, monkeypatch
):
    import hashlib

    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import server

    _require_approval(tmp_path, monkeypatch, "fs_write")
    target = config.workspace_root() / "target.txt"
    target.write_text("v1")
    initial_hash = hashlib.sha256(b"v1").hexdigest()
    updated_content = "v2"
    updated_hash = hashlib.sha256(b"v2").hexdigest()

    wrapper_payload = {
        "path": "target.txt",
        "content": updated_content,
        "expected_content_hash": initial_hash,
    }
    native_payload = {
        "path": "target.txt",
        "append": False,
        "content_sha256": updated_hash,
        "bytes": len(b"v2"),
        "expected_content_sha256": initial_hash,
    }

    with _identity("Desktop_Agent2"):
        wrapper_id = _approved_gate("fs_write", wrapper_payload)
        native_id = _approved_gate("fs_write", native_payload)

        # Mutate target beforehand: preflight compare must reject before filesystem mutation.
        target.write_text("drifted")
        wrapped = server.REGISTERED_TOOLS["fs_write"]
        with pytest.raises(
            ToolError, match="existing file changed; expected_content_hash does not match"
        ):
            wrapped(path="target.txt", content=updated_content, expected_content_hash=initial_hash)

        # File remains intact from drifted state, and BOTH approvals are restored.
        assert target.read_text() == "drifted"
        assert get_approval(wrapper_id)["status"] == "approved"
        assert get_approval(native_id)["status"] == "approved"

        # Restore target back to expected state and retry: write proceeds and spends both.
        target.write_text("v1")
        res = wrapped(
            path="target.txt", content=updated_content, expected_content_hash=initial_hash
        )
        assert res["bytes_written"] == len(b"v2")
        assert target.read_text() == "v2"
        assert get_approval(wrapper_id)["status"] == "consumed"
        assert get_approval(native_id)["status"] == "consumed"

        # Further calls require fresh approvals.
        with pytest.raises(approvals.ApprovalPendingError):
            wrapped(path="target.txt", content=updated_content, expected_content_hash=initial_hash)


def test_registered_conditional_fs_write_post_gate_drift_restores_approvals(tmp_path, monkeypatch):
    import hashlib

    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import server

    _require_approval(tmp_path, monkeypatch, "fs_write")
    target = config.workspace_root() / "target.txt"
    target.write_text("v1")
    initial_hash = hashlib.sha256(b"v1").hexdigest()
    updated_content = "v2"
    updated_hash = hashlib.sha256(b"v2").hexdigest()

    wrapper_payload = {
        "path": "target.txt",
        "content": updated_content,
        "expected_content_hash": initial_hash,
    }
    native_payload = {
        "path": "target.txt",
        "append": False,
        "content_sha256": updated_hash,
        "bytes": len(b"v2"),
        "expected_content_sha256": initial_hash,
    }

    with _identity("Desktop_Agent2"):
        wrapper_id = _approved_gate("fs_write", wrapper_payload)
        native_id = _approved_gate("fs_write", native_payload)

        # Simulate drift occurring right inside check_approval_gate, so initial compare passes
        # but post-gate compare rejects.
        orig_gate = approvals.check_approval_gate

        def gate_with_drift(*args, **kwargs):
            res = orig_gate(*args, **kwargs)
            target.write_text("post_gate_drift")
            return res

        monkeypatch.setattr(approvals, "check_approval_gate", gate_with_drift)

        wrapped = server.REGISTERED_TOOLS["fs_write"]
        with pytest.raises(
            ToolError, match="existing file changed; expected_content_hash does not match"
        ):
            wrapped(path="target.txt", content=updated_content, expected_content_hash=initial_hash)

        # The post-gate compare refusal must restore both approvals before filesystem write.
        assert target.read_text() == "post_gate_drift"
        assert get_approval(wrapper_id)["status"] == "approved"
        assert get_approval(native_id)["status"] == "approved"


def test_registered_fs_write_no_refund_after_mutation_starts(tmp_path, monkeypatch):
    import builtins
    import hashlib

    from unified_agent import server

    _require_approval(tmp_path, monkeypatch, "fs_write")
    target = config.workspace_root() / "target.txt"
    target.write_text("v1")
    initial_hash = hashlib.sha256(b"v1").hexdigest()
    updated_content = "v2"
    updated_hash = hashlib.sha256(b"v2").hexdigest()

    wrapper_payload = {
        "path": "target.txt",
        "content": updated_content,
        "expected_content_hash": initial_hash,
    }
    native_payload = {
        "path": "target.txt",
        "append": False,
        "content_sha256": updated_hash,
        "bytes": len(b"v2"),
        "expected_content_sha256": initial_hash,
    }

    with _identity("Desktop_Agent2"):
        wrapper_id = _approved_gate("fs_write", wrapper_payload)
        native_id = _approved_gate("fs_write", native_payload)

        orig_open = builtins.open

        def failing_open(path, *args, **kwargs):
            if str(path).endswith("target.txt"):
                raise OSError("simulated disk I/O error after boundary")
            return orig_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        wrapped = server.REGISTERED_TOOLS["fs_write"]
        with pytest.raises(OSError, match="simulated disk I/O error"):
            wrapped(path="target.txt", content=updated_content, expected_content_hash=initial_hash)

        # Execution started (boundary crossed); approvals stay spent and must not be refunded.
        assert get_approval(wrapper_id)["status"] == "consumed"
        assert get_approval(native_id)["status"] == "consumed"


def test_bare_fs_write_conditional_preflight_refusal_restores_receipt(tmp_path, monkeypatch):
    import hashlib

    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import files

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "fs_write")
    target = config.workspace_root() / "target.txt"
    target.write_text("v1")
    initial_hash = hashlib.sha256(b"v1").hexdigest()
    updated_content = "v2"
    updated_hash = hashlib.sha256(b"v2").hexdigest()

    native_payload = {
        "path": "target.txt",
        "append": False,
        "content_sha256": updated_hash,
        "bytes": len(b"v2"),
        "expected_content_sha256": initial_hash,
    }

    with _identity("Desktop_Agent2"):
        approval_id = _approved_gate("fs_write", native_payload)

        target.write_text("drifted")
        with approvals.approval_invocation("fs_write", native_payload):
            with pytest.raises(
                ToolError, match="existing file changed; expected_content_hash does not match"
            ):
                files.fs_write(
                    path="target.txt", content=updated_content, expected_content_hash=initial_hash
                )

        assert target.read_text() == "drifted"
        assert get_approval(approval_id)["status"] == "approved"

        # Restore and succeed
        target.write_text("v1")
        with approvals.approval_invocation("fs_write", native_payload):
            files.fs_write(
                path="target.txt", content=updated_content, expected_content_hash=initial_hash
            )

        assert target.read_text() == "v2"
        assert get_approval(approval_id)["status"] == "consumed"
