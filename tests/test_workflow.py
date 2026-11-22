"""Tests for repo-defined YAML workflows (task #62 · B).

Definitions are code: loaded only from the package's workflows/ dir (tests
point that at a tmp dir), executed fail-fast with explicit data passing, and
every step goes through the same gated MCP tool functions.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety, storage
from unified_agent.agent_access import AgentAccessDenied
from unified_agent.policy import PolicyViolation
from unified_agent.tools import workflow as wf
from unified_agent.tools.approvals import approval_pending, approval_resolve
from unified_agent.tools.workflow import workflow_list, workflow_run

pytestmark = pytest.mark.smoke


@pytest.fixture()
def wdir(tmp_path, monkeypatch):
    d = tmp_path / "workflows"
    d.mkdir()
    monkeypatch.setattr(wf, "_workflows_dir", lambda: d)
    return d


def _write(wdir, name, text):
    (wdir / f"{name}.yaml").write_text(text, encoding="utf-8")


class TestDefinitionLoading:
    def test_shipped_smoke_workflow_is_valid(self):
        # the real packaged definition (no monkeypatched dir) must load and list
        names = [w["name"] for w in workflow_list()["workflows"]]
        assert "smoke-echo" in names

    def test_unknown_workflow(self, wdir):
        with pytest.raises(ToolError, match="no workflow definition"):
            workflow_run("nope")

    def test_invalid_name_rejected(self, wdir):
        with pytest.raises(ToolError, match="invalid"):
            workflow_run("../../etc/passwd")

    def test_unknown_tool_rejected(self, wdir):
        _write(wdir, "bad", "name: bad\nsteps:\n  - {name: s1, tool: os_system, args: {}}\n")
        with pytest.raises(ToolError, match="unknown/uncallable tool"):
            workflow_run("bad")

    def test_gate_tools_not_callable_from_workflows(self, wdir):
        # a workflow must never resolve its own approval gate or recurse
        for tool in ("approval_resolve", "approval_pending", "workflow_run"):
            _write(wdir, "sneaky", f"name: sneaky\nsteps:\n  - {{name: s1, tool: {tool}}}\n")
            with pytest.raises(ToolError, match="unknown/uncallable tool"):
                workflow_run("sneaky")

    def test_duplicate_step_names_rejected(self, wdir):
        _write(
            wdir,
            "dup",
            "name: dup\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        with pytest.raises(ToolError, match="duplicate step name"):
            workflow_run("dup")


class TestExecution:
    def test_explicit_data_passing(self, wdir):
        _write(
            wdir,
            "chain",
            "name: chain\n"
            "inputs: [message]\n"
            "steps:\n"
            "  - name: echo\n"
            "    tool: run_cmd\n"
            "    args: {command: 'echo {{ inputs.message }}'}\n"
            "  - name: vet\n"
            "    tool: run_cmd_check\n"
            "    args: {command: 'echo {{ steps.echo.stdout }}'}\n",
        )
        r = workflow_run("chain", inputs={"message": "hi-there"})
        assert r["status"] == "done"
        assert [s["status"] for s in r["steps"]] == ["done", "done"]
        assert "hi-there" in r["outputs"]["echo"]["stdout"]
        assert r["outputs"]["vet"]["ok"] is True

    def test_missing_and_undeclared_inputs(self, wdir):
        _write(
            wdir,
            "needs",
            "name: needs\ninputs: [a]\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: 'echo {{ inputs.a }}'}}\n",
        )
        with pytest.raises(ToolError, match="missing required inputs: a"):
            workflow_run("needs")
        with pytest.raises(ToolError, match="undeclared inputs: b"):
            workflow_run("needs", inputs={"a": 1, "b": 2})

    def test_unresolvable_placeholder_fails_step(self, wdir):
        _write(
            wdir,
            "hole",
            "name: hole\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: 'echo {{ steps.ghost.x }}'}}\n",
        )
        r = workflow_run("hole")
        assert r["status"] == "failed"
        assert "cannot resolve" in r["steps"][0]["error"]

    def test_fail_fast_stops_later_steps(self, wdir):
        _write(
            wdir,
            "ff",
            "name: ff\nsteps:\n"
            "  - {name: boom, tool: run_cmd, args: {command: 'curl x'}}\n"
            "  - {name: never, tool: run_cmd_check, args: {command: 'echo hi'}}\n",
        )
        r = workflow_run("ff")
        assert r["status"] == "denied"
        assert r["steps"][0]["attempts"] == 1
        assert len(r["steps"]) == 1  # second step never started
        assert "ToolError" in r["steps"][0]["error"]

    def test_per_step_retries_bounded(self, wdir, monkeypatch):
        # Task #1218: step invocation now goes through server.REGISTERED_TOOLS
        # (the governed wrapper), not the raw _tool_registry() callable, so the
        # fake tool is substituted there. "run_cmd_check" stays a real curated
        # name for _load_definition's validation.
        from unified_agent import server

        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            raise RuntimeError("flaky")

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", flaky)
        _write(
            wdir,
            "retry",
            "name: retry\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {}, retries: 2}\n",
        )
        r = workflow_run("retry")
        assert r["status"] == "failed"
        assert r["steps"][0]["attempts"] == 3  # 1 try + 2 retries
        assert calls["n"] == 3

    def test_whole_string_placeholder_preserves_type(self, wdir, monkeypatch):
        from unified_agent import server

        seen = {}

        def capture(**kw):
            seen.update(kw)
            return {"ok": True}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", capture)
        _write(
            wdir,
            "typed",
            "name: typed\ninputs: [count]\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: '{{ inputs.count }}'}}\n",
        )
        workflow_run("typed", inputs={"count": 42})
        assert seen["command"] == 42  # not the string "42"


class TestApprovalIntegration:
    def test_gated_step_pauses_run_without_retrying(self, wdir, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        _write(
            wdir,
            "gated",
            "name: gated\nsteps:\n"
            "  - {name: s1, tool: run_cmd, args: {command: 'echo hi'}, retries: 3}\n",
        )
        r = workflow_run("gated", author="Desktop_agent3")
        assert r["status"] == "pending-approval"
        assert r["steps"][0]["attempts"] == 1  # waiting on a human burns no retries
        pending = approval_pending()["pending"]
        assert len(pending) == 1

        approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")
        # The rerun identifies itself as the run's author, which is the caller the
        # pass names (#2492). A caller with no identity at all cannot spend a pass
        # recorded for somebody else, so the anonymous rerun is not the shape to
        # pin here; the same-author rerun is what a real agent retry looks like.
        r2 = workflow_run("gated", author="Desktop_agent3")
        assert r2["status"] == "done"

    def test_non_self_gated_tool_gated_via_workflow(self, wdir, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
        _write(
            wdir,
            "gate2",
            "name: gate2\nsteps:\n  - {name: s1, tool: task_add, args: {title: from-wf}}\n",
        )
        r = workflow_run("gate2")
        assert r["status"] == "pending-approval"
        pending = approval_pending()["pending"]
        assert pending[0]["tool"] == "task_add"

    @pytest.mark.parametrize("guard", ["access", "profile", "policy"])
    def test_denied_child_restores_approval_pass(self, wdir, monkeypatch, guard):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
        _write(
            wdir,
            "denied-gated",
            "name: denied-gated\nsteps:\n  - {name: s1, tool: task_add, args: {title: from-wf}}\n",
        )

        first = workflow_run("denied-gated")
        assert first["status"] == "pending-approval"
        pending = approval_pending()["pending"]
        approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")

        from unified_agent import profiles

        if guard == "access":
            monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
        elif guard == "profile":
            allowed = profiles.tool_is_allowed
            monkeypatch.setattr(profiles, "tool_is_allowed", lambda _tool: False)
        else:
            real_policy = safety.check_policy_gate

            def deny(tool, payload=None, episode_id=None, originator=None):
                if tool == "task_add":
                    raise PolicyViolation("denied by test policy")
                return real_policy(tool, payload, episode_id=episode_id, originator=originator)

            monkeypatch.setattr(safety, "check_policy_gate", deny)

        denied = workflow_run("denied-gated")
        assert denied["status"] == "denied"
        assert denied["steps"][0]["attempts"] == 1

        conn = storage.connect()
        try:
            assert (
                conn.execute(
                    "SELECT status FROM approvals WHERE id=?", (pending[0]["id"],)
                ).fetchone()["status"]
                == "approved"
            )
        finally:
            conn.close()

        if guard == "access":
            monkeypatch.delenv("UA_AGENT_ACCESS_READ_ONLY")
        elif guard == "profile":
            monkeypatch.setattr(profiles, "tool_is_allowed", allowed)
        else:
            monkeypatch.setattr(safety, "check_policy_gate", real_policy)
        assert workflow_run("denied-gated")["status"] == "done"

    def test_workflow_steps_respect_run_cmd_allowlist(self, wdir):
        _write(
            wdir,
            "escape",
            "name: escape\nsteps:\n  - {name: s1, tool: run_cmd, args: {command: 'bash -c id'}}\n",
        )
        r = workflow_run("escape")
        assert r["status"] == "denied"
        assert r["steps"][0]["attempts"] == 1
        assert "allowlist" in r["steps"][0]["error"]


class TestListing:
    def test_list_reports_names_and_steps(self, wdir):
        _write(
            wdir,
            "alpha",
            "name: alpha\ndescription: first\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: echo}}\n",
        )
        (wdir / "broken.yaml").write_text(": not yaml : [", encoding="utf-8")
        wfs = {w["name"]: w for w in workflow_list()["workflows"]}
        assert wfs["alpha"]["description"] == "first"
        assert wfs["alpha"]["steps"] == ["s1"]
        assert "broken" in wfs  # listed, flagged unparseable, never crashes


class TestVerifiedCallerIdentity:
    """Task #1218: `author` can no longer launder attribution.

    Real defect found in an enterprise accountability audit of e5a9c28:
    workflow.py:308 set `agent_identity_var` to a caller-supplied `author`
    with no check against the authenticated identity, so any caller could
    attribute a workflow's steps (and thus every write those steps make) to
    a different agent than the one that actually authenticated. The fix
    reuses `config.verify_write_identity` — the same primitive
    memory_save/task_update/delegate/vault_write/plan_* already use for this
    exact guarantee.
    """

    def test_mismatched_author_rejected_under_enforce_auth(self, wdir, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        _write(
            wdir,
            "id",
            "name: id\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            with pytest.raises(ToolError, match="Auth identity mismatch"):
                workflow_run("id", author="SomeoneElse")
        finally:
            config.agent_identity_var.reset(token)
        # Rejected before the definition was even loaded/executed.
        assert (wdir / "id.yaml").exists()

    def test_mismatched_author_not_silently_honored_without_enforce(self, wdir, monkeypatch):
        """Even in warn-only mode (UA_ENFORCE_AUTH unset), the claimed author
        must never win: the step still runs attributed to the verified
        identity, not to whatever the caller claimed."""
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        from unified_agent import server

        seen = []

        def spy(**kw):
            seen.append(config.agent_identity_var.get())
            return {"ok": True}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", spy)
        _write(
            wdir,
            "id2",
            "name: id2\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            r = workflow_run("id2", author="SomeoneElse")
        finally:
            config.agent_identity_var.reset(token)
        assert r["status"] == "done"
        assert seen == ["Desktop_Agent1"]  # never "SomeoneElse"

    def test_omitted_author_preserves_authenticated_identity(self, wdir, monkeypatch):
        from unified_agent import server

        seen = []

        def spy(**kw):
            seen.append(config.agent_identity_var.get())
            return {"ok": True}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", spy)
        _write(
            wdir,
            "id3",
            "name: id3\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            r = workflow_run("id3")  # author omitted entirely
        finally:
            config.agent_identity_var.reset(token)
        assert r["status"] == "done"
        # Omitted author must inherit the authenticated identity, not blank it.
        assert seen == ["Desktop_Agent1"]

    def test_unauthenticated_context_still_accepts_explicit_author(self, wdir, monkeypatch):
        """No verified identity (stdio/unauthenticated, the common test/dev
        path): an explicit `author` is accepted unchanged, matching every
        other verify_write_identity() call site."""
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        from unified_agent import server

        seen = []

        def spy(**kw):
            seen.append(config.agent_identity_var.get())
            return {"ok": True}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", spy)
        _write(
            wdir,
            "id4",
            "name: id4\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        assert config.agent_identity_var.get() is None
        r = workflow_run("id4", author="test-agent")
        assert r["status"] == "done"
        assert seen == ["test-agent"]


class TestGovernedChildInvocation:
    """Task #1218: a workflow step must go through the same access/profile/
    policy wrappers a direct MCP tool call gets (server.py's
    `_guarded_and_instrumented`, exposed as `server.REGISTERED_TOOLS`), not a
    raw import of the bare tool function. The raw-registry path silently
    skipped `agent_access.enforce`, `operational_profiles.tool_is_allowed`,
    and — the specific finding — `safety.check_policy_gate`, so a workflow
    step's per-call policy gate never ran."""

    def test_step_routes_through_registered_tools_not_raw_function(self, wdir, monkeypatch):
        from unified_agent import server
        from unified_agent.tools import shell

        # Poison the raw function the old code imported directly: if the
        # engine still called it, this test fails loudly instead of passing
        # by accident.
        def poisoned(**kw):
            raise AssertionError("workflow invoked the raw tool function, not REGISTERED_TOOLS")

        poisoned.__name__ = "run_cmd_check"  # _tool_registry() keys by fn.__name__
        monkeypatch.setattr(shell, "run_cmd_check", poisoned)

        calls = []

        def governed_spy(**kw):
            calls.append(kw)
            return {"ok": True, "via": "governed"}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "run_cmd_check", governed_spy)
        _write(
            wdir,
            "routed",
            "name: routed\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        r = workflow_run("routed")
        assert r["status"] == "done"
        assert len(calls) == 1
        assert r["outputs"]["s1"]["via"] == "governed"

    def test_step_policy_gate_is_actually_invoked(self, wdir, monkeypatch):
        """Assert the call, don't assume it: previously (raw registry)
        safety.check_policy_gate was never reached for a workflow step at
        all. This spies on the real chokepoint server.py calls."""
        calls = []
        real = safety.check_policy_gate

        def spy(tool, payload=None, episode_id=None, originator=None):
            calls.append((tool, payload))
            return real(tool, payload, episode_id=episode_id, originator=originator)

        monkeypatch.setattr(safety, "check_policy_gate", spy)
        _write(
            wdir,
            "gate-spy",
            "name: gate-spy\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        r = workflow_run("gate-spy")
        assert r["status"] == "done"
        assert any(tool == "run_cmd_check" for tool, _ in calls), (
            f"safety.check_policy_gate was never called for the step's tool; calls={calls}"
        )

    def test_policy_denied_child_step_is_denied_and_reported(self, wdir, monkeypatch):
        """A policy-engine deny for the step's tool must deny the step (not
        retry it into a generic 'failed') and the workflow must report it."""

        def deny(tool, payload=None, episode_id=None, originator=None):
            if tool == "run_cmd_check":
                raise PolicyViolation(f"Tool '{tool}' denied by policy rule 'no-shell-checks'")

        monkeypatch.setattr(safety, "check_policy_gate", deny)
        _write(
            wdir,
            "denied",
            "name: denied\nsteps:\n"
            "  - {name: s1, tool: run_cmd_check, args: {command: echo hi}, retries: 3}\n",
        )
        r = workflow_run("denied")
        assert r["status"] == "denied"
        assert r["steps"][0]["status"] == "denied"
        assert "PolicyViolation" in r["steps"][0]["error"]
        # Deterministic denial must not burn retries.
        assert r["steps"][0]["attempts"] == 1

    def test_agent_access_denied_child_step_is_denied_and_reported(self, wdir, monkeypatch):
        def deny(tool, payload=None, episode_id=None, originator=None):
            if tool == "run_cmd_check":
                raise AgentAccessDenied("agent access is read-only")

        monkeypatch.setattr(safety, "check_policy_gate", deny)
        _write(
            wdir,
            "denied2",
            "name: denied2\nsteps:\n  - {name: s1, tool: run_cmd_check, args: {command: echo hi}}\n",
        )
        r = workflow_run("denied2")
        assert r["status"] == "denied"
        assert r["steps"][0]["status"] == "denied"
        assert "AgentAccessDenied" in r["steps"][0]["error"]


class TestApprovalNotDoubleConsumed:
    """Task #1218: routing steps through the governed wrapper means a Policy
    Engine `require_approval` rule (safety.check_policy_gate ->
    policy.evaluate_policy -> approvals.check_approval_gate, force=True) can
    now fire for the exact same (tool, payload) the workflow engine's own
    UA_APPROVAL_TOOLS gate already checked. Without sharing one pass via
    approvals.approval_invocation(), the engine's own gate would consume the
    human's approval and the policy engine's gate would immediately find it
    already 'consumed' and re-queue a fresh request for the same call —
    silently requiring a second approval for one logical action, and
    spamming the queue with a request that can never legitimately resolve
    the first one."""

    def test_two_gates_on_one_call_share_a_single_pass(self, wdir, monkeypatch):
        from unified_agent.tools.approvals import check_approval_gate

        def simulated_require_approval_rule(tool, payload=None, episode_id=None, originator=None):
            # Stands in for a Policy Engine rule with action=require_approval
            # targeting this tool — the exact collision the docstring above
            # describes, without needing a full policy.yaml fixture.
            if tool == "task_add":
                check_approval_gate(tool, payload, requested_by=originator, force=True)

        monkeypatch.setattr(safety, "check_policy_gate", simulated_require_approval_rule)
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
        _write(
            wdir,
            "collide",
            "name: collide\nsteps:\n  - {name: s1, tool: task_add, args: {title: from-wf}}\n",
        )

        r1 = workflow_run("collide")
        assert r1["status"] == "pending-approval"
        pending = approval_pending()["pending"]
        # Exactly one request, not two, for the one logical call.
        assert len(pending) == 1

        approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")

        r2 = workflow_run("collide")
        # If the pass were consumed twice (engine's gate, then the policy
        # gate re-queuing because it found 'consumed' not 'approved'), this
        # would come back 'pending-approval' again instead of 'done'.
        assert r2["status"] == "done"
        assert approval_pending()["pending"] == []

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("fs_write", {"path": "workflow-output.txt", "content": "hello"}),
            (
                "delegate",
                {
                    "task_spec": "Rebase the fix branch onto main\nDetails: see broadcast.",
                    "from_agent": "Desktop_Agent1",
                    "to_agent": "Desktop_agent3",
                    "callback": "none",
                },
            ),
        ],
    )
    def test_self_gated_step_uses_one_tool_pass(self, wdir, monkeypatch, tool, args):
        """The bare tool owns its approval payload; the engine must not gate
        the raw YAML args first and strand the retry behind a second request."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", tool)
        _write(
            wdir,
            f"self-gated-{tool}",
            f"name: self-gated-{tool}\nsteps:\n"
            f"  - name: s1\n    tool: {tool}\n    args: {args!r}\n",
        )

        first = workflow_run(f"self-gated-{tool}", author="Desktop_Agent1")
        assert first["status"] == "pending-approval"
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")

        second = workflow_run(f"self-gated-{tool}", author="Desktop_Agent1")
        assert second["status"] == "done"
        assert approval_pending()["pending"] == []


def test_ledger_failure_never_retries_a_completed_mutation(wdir, monkeypatch):
    from unified_agent import events

    _write(
        wdir,
        "ledger-fail",
        "name: ledger-fail\nsteps:\n  - {name: append, tool: fs_write, retries: 2, args: {path: a.txt, content: x, append: true}}\n",
    )
    effects = []
    from unified_agent import server

    def fs_write(path, content, append=False):
        effects.append(content)
        return {"ok": True}

    monkeypatch.setattr(
        wf,
        "_governed_invoke",
        lambda tool, args: server._guarded_and_instrumented(fs_write)(**args),
    )
    monkeypatch.setattr(
        events, "record_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked"))
    )
    result = workflow_run("ledger-fail")
    assert result["status"] == "indeterminate"
    assert result["steps"][0]["attempts"] == 1
    assert effects == ["x"]
