"""Tests for the delegation protocol (task #62 · A).

delegate() links a child task + attributed memory note + delegations row;
the completion callback fires from task_update and is strictly best-effort.
"""

import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, lease_reaper, storage
from unified_agent.tools.approvals import ApprovalPendingError, approval_pending
from unified_agent.tools.delegate import delegate, delegate_list, delegation_manage
from unified_agent.tools.memory import memory_get
from unified_agent.tools.tasks import task_claim, task_list, task_update


def _mk(**kw):
    defaults = dict(
        task_spec="Rebase the fix branch onto main\nDetails: see broadcast.",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_agent3",
        callback="none",
    )
    defaults.update(kw)
    return delegate(**defaults)


class TestDelegate:
    def test_delegation_manage_dispatches_spec_template(self):
        result = delegation_manage("spec_template")
        assert "template" in result
        assert result["required_sections"]

    def test_delegation_manage_rejects_bad_action_and_params(self):
        with pytest.raises(ToolError, match="action must be one of"):
            delegation_manage("unknown")
        with pytest.raises(ToolError, match="invalid params"):
            delegation_manage("list", {"unexpected": True})

    def test_cancelled_task_retires_pending_delegation(self):
        r = _mk()
        task_update(r["task_id"], status="cancelled")
        delegation = next(
            d for d in delegate_list(status="all")["delegations"] if d["id"] == r["delegation_id"]
        )
        assert delegation["status"] == "cancelled"

    def test_developer_can_use_delegation_cancel_action(self):
        r = _mk()
        result = delegation_manage(
            "cancel", {"id": r["delegation_id"], "agent": "Desktop_Agent1", "reason": "not needed"}
        )
        assert result == {"cancelled": True}

    def test_run_scoped_cancel_does_not_retire_a_new_attempt(self):
        r = _mk()
        first = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})[
            "delegation"
        ]
        assert delegation_manage(
            "release",
            {
                "id": r["delegation_id"],
                "agent": "Desktop_agent3",
                "expected_claim_generation": first["claim_generation"],
            },
        ) == {"released": True}
        second = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})[
            "delegation"
        ]
        assert first["attempts"] == second["attempts"] == 1
        assert second["claim_generation"] == first["claim_generation"] + 1
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET current_run_id='sp-successor', cancel_requested=0 WHERE id=?",
                (r["task_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        params = {
            "id": r["delegation_id"],
            "agent": "Desktop_agent3",
            "expected_run_id": "sp-original",
            "expected_claim_generation": first["claim_generation"],
        }
        assert delegation_manage("cancel", params) == {"cancelled": False}
        params["expected_claim_generation"] = second["claim_generation"]
        assert delegation_manage("cancel", params) == {"cancelled": True}

    def test_claim_returns_its_own_generation_even_if_successor_claims_after_commit(
        self, monkeypatch
    ):
        r = _mk()
        real_connect = storage.connect

        class RacingConnection:
            def __init__(self):
                self.raw = real_connect()

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def commit(self):
                self.raw.commit()
                successor = real_connect()
                try:
                    successor.execute(
                        "UPDATE delegations SET status='pending', attempts=attempts-1 WHERE id=?",
                        (r["delegation_id"],),
                    )
                    successor.execute(
                        "UPDATE delegations SET status='in_flight', attempts=attempts+1, "
                        "claim_generation=claim_generation+1 WHERE id=?",
                        (r["delegation_id"],),
                    )
                    successor.commit()
                finally:
                    successor.close()

        monkeypatch.setattr(storage, "connect", RacingConnection)
        monkeypatch.setattr(storage, "record_agent_event", lambda *_a, **_k: None)
        claimed = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})
        assert claimed["claimed"] is True
        assert claimed["delegation"]["claim_generation"] == 1
        conn = real_connect()
        try:
            live = conn.execute(
                "SELECT claim_generation FROM delegations WHERE id=?", (r["delegation_id"],)
            ).fetchone()
        finally:
            conn.close()
        assert live["claim_generation"] == 2

    @pytest.mark.parametrize(
        ("action", "result_key"),
        [
            ("release", "released"),
            ("fail", "failed"),
            ("retry", "retried"),
            ("complete", "completed"),
        ],
    )
    def test_old_claim_cannot_transition_successor(self, action, result_key):
        r = _mk()
        first = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})[
            "delegation"
        ]
        delegation_manage(
            "release",
            {
                "id": r["delegation_id"],
                "agent": "Desktop_agent3",
                "expected_claim_generation": first["claim_generation"],
            },
        )
        second = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})[
            "delegation"
        ]
        assert second["claim_generation"] > first["claim_generation"]
        result = delegation_manage(
            action,
            {
                "id": r["delegation_id"],
                "agent": "Desktop_agent3",
                "expected_claim_generation": first["claim_generation"],
            },
        )
        assert result == {result_key: False}
        live = delegation_manage("get", {"id": r["delegation_id"]})["delegation"]
        assert live["status"] == "in_flight"
        assert live["claim_generation"] == second["claim_generation"]

    def test_pending_failure_cannot_fail_a_new_claim(self):
        r = _mk()
        claimed = delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})
        assert claimed["claimed"] is True
        result = delegation_manage(
            "fail", {"id": r["delegation_id"], "agent": "Desktop_agent3", "reason": "ceiling"}
        )
        assert result == {"failed": False}
        assert (
            delegation_manage("get", {"id": r["delegation_id"]})["delegation"]["status"]
            == "in_flight"
        )

    @pytest.mark.parametrize("action", ["release", "retry"])
    def test_attempt_bound_transition_requires_generation(self, action):
        r = _mk()
        delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})
        result = delegation_manage(action, {"id": r["delegation_id"], "agent": "Desktop_agent3"})
        assert list(result.values()) == [False]
        assert (
            delegation_manage("get", {"id": r["delegation_id"]})["delegation"]["status"]
            == "in_flight"
        )

    def test_boolean_generation_is_rejected(self):
        r = _mk()
        delegation_manage("claim", {"id": r["delegation_id"], "agent": "Desktop_agent3"})
        with pytest.raises(ToolError, match="expected_claim_generation"):
            delegation_manage(
                "release",
                {
                    "id": r["delegation_id"],
                    "agent": "Desktop_agent3",
                    "expected_claim_generation": True,
                },
            )

    @pytest.mark.parametrize("action", ["claim", "release", "cancel", "fail", "complete", "retry"])
    def test_lifecycle_refuses_a_caller_naming_another_agent(self, monkeypatch, action):
        """Under UA_ENFORCE_AUTH, naming the assignee does not borrow its authority."""
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        r = _mk()
        params = {"id": r["delegation_id"], "agent": "Desktop_agent3"}
        if action != "claim":
            claimed = delegation_manage("claim", dict(params))["delegation"]
            params["expected_claim_generation"] = claimed["claim_generation"]
        before = delegation_manage("get", {"id": r["delegation_id"]})["delegation"]["status"]
        token = config.agent_identity_var.set("Laptop_Agent4")
        try:
            with pytest.raises(ToolError, match="identity mismatch"):
                delegation_manage(action, params)
        finally:
            config.agent_identity_var.reset(token)
        after = delegation_manage("get", {"id": r["delegation_id"]})["delegation"]["status"]
        assert after == before

    @pytest.mark.parametrize(("action", "key"), [("fail", "failed"), ("complete", "completed")])
    def test_non_participant_cannot_finish_a_pending_delegation(self, action, key):
        r = _mk()
        token = config.agent_identity_var.set("Laptop_Agent4")
        try:
            result = delegation_manage(action, {"id": r["delegation_id"], "agent": "Laptop_Agent4"})
        finally:
            config.agent_identity_var.reset(token)
        assert result == {key: False}
        assert delegation_manage("get", {"id": r["delegation_id"]})["delegation"]["status"] == (
            "pending"
        )

    def test_creates_linked_task_note_and_row(self):
        r = _mk(deadline="2026-08-01", callback="memory")
        # This fixture's task_spec is a one-liner, so with the Delegation Spec
        # Standard downgraded (conftest) delegate also reports spec_warnings —
        # a bypassed check must stay visible on the result (task #160).
        assert {"delegation_id", "task_id", "memory_id"} <= set(r)
        assert r["spec_warnings"]

        tasks = task_list(status="open", notes_contain="[delegated-to: Desktop_agent3]")["tasks"]
        assert [t["id"] for t in tasks] == [r["task_id"]]
        assert "task-spec:" in tasks[0]["notes"]
        assert "[delegated-by: Desktop_Agent1]" in tasks[0]["notes"]

        note = memory_get(r["memory_id"])
        assert note["author"] == "Desktop_Agent1"
        assert "delegation" in note["tags"]
        assert f"task #{r['task_id']}" in note["content"]

        d = delegate_list()["delegations"][0]
        assert d["id"] == r["delegation_id"]
        assert (d["from_agent"], d["to_agent"]) == ("Desktop_Agent1", "Desktop_agent3")
        assert d["status"] == "pending"
        assert d["deadline"] == "2026-08-01"

    def test_title_is_not_the_template_heading(self):
        """Task #378 regression: #367/#368/#370 were created titled "### Objective"
        because delegate() took task_spec.splitlines()[0] verbatim, and every
        spec that satisfies the Delegation Spec Standard starts with that
        heading. Exercise the real template, not a hand-picked spec, so a
        different split-boundary bug in this path would also be caught."""
        from unified_agent.tools.delegation_spec import DELEGATION_SPEC_TEMPLATE

        spec = (
            DELEGATION_SPEC_TEMPLATE.replace(
                "<What must be true afterwards that is not true now. One paragraph.>",
                "The flaky upload test passes 20/20 consecutive runs.",
            )
            .replace(
                "- <path>\n- <path>",
                "- tests/test_upload.py",
            )
            .replace(
                "- <the capability, exercised: what is called, with what input, and what is\n"
                "  asserted about the result>",
                "- Calling upload() with a 10MB file returns a checksum that verifies.",
            )
            .replace(
                "- <the failure mode, exercised: what goes wrong, and what the code does about\n"
                "  it — assert the refusal, not just the happy path>",
                "- A truncated upload is rejected before the checksum step runs.",
            )
        )
        r = _mk(task_spec=spec)
        title = task_list(status="open")["tasks"]
        title = next(t["title"] for t in title if t["id"] == r["task_id"])
        assert not title.startswith("#")
        assert "\n" not in title
        assert title == "The flaky upload test passes 20/20 consecutive runs."

    def test_rejects_unknown_labels(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_agent3")
        with pytest.raises(ToolError, match="to_agent.*not a known agent label"):
            _mk(to_agent="Rando_Bot")
        with pytest.raises(ToolError, match="from_agent.*not a known agent label"):
            _mk(from_agent="nobody")

    def test_open_roster_rejects_garbage_and_accepts_well_formed(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
        with pytest.raises(ToolError, match="not a valid agent label"):
            _mk(to_agent="???")
        with pytest.raises(ToolError, match="not a governed fleet identity"):
            _mk(to_agent="Desktop_Claud")
        r = _mk(to_agent="desktop_agent2")
        assert r["task_id"] > 0
        child = next(t for t in task_list(status="open")["tasks"] if t["id"] == r["task_id"])
        assert child["lane"] == "Desktop_Agent2"
        custom = _mk(to_agent="Local_Worker")
        assert custom["task_id"] > 0
        custom_child = next(
            t for t in task_list(status="open")["tasks"] if t["id"] == custom["task_id"]
        )
        assert custom_child["lane"] == "Local_Worker"

    def test_labels_extendable_via_env(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Test_Agent")
        r = _mk(to_agent="Test_Agent")
        assert r["task_id"] > 0

    def test_rejects_self_delegation(self):
        with pytest.raises(ToolError, match="cannot delegate to yourself"):
            _mk(to_agent="Desktop_Agent1")

    def test_rejects_bad_deadline_and_callback(self):
        with pytest.raises(ToolError, match="ISO-8601"):
            _mk(deadline="next tuesday")
        with pytest.raises(ToolError, match="callback"):
            _mk(callback="carrier-pigeon")
        with pytest.raises(ToolError, match="task_spec"):
            _mk(task_spec="   ")

    def test_warns_when_product_repo_spec_lacks_code_task_marker(self):
        repo = config.dev_root() / "unified_agent"
        repo.mkdir(parents=True)
        res = _mk(task_spec=f"Inspect {repo} and report back.")

        assert "dispatch_warnings" in res
        assert "lacks [code-task]" in res["dispatch_warnings"][0]
        assert "project selects its checkout regardless" in res["dispatch_warnings"][0]
        assert "completion requires code evidence" in res["dispatch_warnings"][0]
        assert "workspace-sourced" not in res["dispatch_warnings"][0]
        row = next(
            t for t in task_list(status="open", fields="full")["tasks"] if t["id"] == res["task_id"]
        )
        assert "[code-task]" not in row["notes"]

    def test_overdue_flag(self):
        _mk(deadline="2020-01-01")
        _mk(deadline="2999-01-01", to_agent="Laptop_agent3")
        rows = {d["deadline"]: d for d in delegate_list()["delegations"]}
        assert rows["2020-01-01"]["overdue"] is True
        assert rows["2999-01-01"]["overdue"] is False


class TestCompletionCallback:
    def test_task_done_completes_delegation(self):
        r = _mk(callback="none")
        task_update(r["task_id"], status="done", actor="test-agent")
        d = delegate_list(status="completed")["delegations"][0]
        assert d["id"] == r["delegation_id"]
        assert d["completed_at"] is not None
        assert delegate_list(status="pending")["delegations"] == []

    def test_memory_callback_appends_completion(self):
        r = _mk(callback="memory")
        task_update(r["task_id"], status="done", actor="test-agent")
        note = memory_get(r["memory_id"])
        assert "Completed: task #" in note["content"]
        # append path: the original spec is still there (nothing destroyed)
        assert "Rebase the fix branch" in note["content"]

    def test_memory_callback_survives_delegator_completing_on_delegates_behalf(self, monkeypatch):
        """Live incident (2026-08-08, cross-machine): Laptop_Agent1 delegated task
        #306 to Laptop_Agent2, then Laptop_Agent1 itself called
        task_update(status="done") rather than Laptop_Agent2 completing its own
        task. The completion callback used to claim author=to_agent
        ("Laptop_Agent2") regardless of who actually called task_update, which
        verify_write_identity correctly rejected as impersonation under
        UA_ENFORCE_AUTH=1 (real event: "Auth identity mismatch: claimed
        'Laptop_Agent2' but verified as 'Laptop_Agent1'") — silently losing the
        delegation's completion memory note every time the delegator (not the
        assignee) is the one who marks it done, which the live cross-device
        queue shows is the common case, not an edge case."""

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        r = _mk(callback="memory", from_agent="Desktop_Agent1", to_agent="Desktop_agent3")

        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            task_update(r["task_id"], status="done")  # must not raise
        finally:
            config.agent_identity_var.reset(token)

        d = delegate_list(status="completed")["delegations"][0]
        assert d["callback_result"] == "ok"
        note = memory_get(r["memory_id"])
        assert "Completed: task #" in note["content"]
        # Authorship must reflect who actually called task_update, never an
        # impersonated identity that never authenticated as that agent.
        assert note["updated_by"] == "Desktop_Agent1"

    def test_notify_callback_failure_never_breaks_task_update(self, monkeypatch):
        def boom(message, **_kwargs):
            raise RuntimeError("bridge down")

        # The callback speaks for the delegator via notify_as (task #1735).
        monkeypatch.setattr("unified_agent.tools.notify.notify_as", boom)
        r = _mk(callback="notify")
        res = task_update(r["task_id"], status="done", actor="test-agent")  # must not raise
        assert res["status"] == "done"
        d = delegate_list(status="completed")["delegations"][0]
        assert "RuntimeError" in d["callback_result"]

    def test_notify_callback_delivery_recorded(self, monkeypatch):
        sent = {}

        def fake_notify(message, **kwargs):
            sent["message"] = message
            sent["sender"] = kwargs.get("sender")
            return {"delivered": True, "chars": len(message), "error": None}

        monkeypatch.setattr("unified_agent.tools.notify.notify_as", fake_notify)
        r = _mk(callback="notify")
        task_update(r["task_id"], status="done", actor="test-agent")
        assert f"task #{r['task_id']}" in sent["message"]
        assert sent["sender"] == "Desktop_Agent1"  # the delegator, not the completer
        d = delegate_list(status="completed")["delegations"][0]
        assert d["callback_result"] == "ok"

    def test_callback_fires_once(self):
        r = _mk(callback="none")
        task_update(r["task_id"], status="done", actor="test-agent")
        task_update(r["task_id"], status="open")
        task_update(r["task_id"], status="done", actor="test-agent")
        completed = delegate_list(status="completed")["delegations"]
        assert len(completed) == 1
        assert completed[0]["id"] == r["delegation_id"]

    def test_unrelated_task_completion_is_noop(self):
        _mk(callback="none")
        from unified_agent.tools.tasks import task_add

        other = task_add("unrelated")["id"]
        task_update(other, status="done", actor="test-agent")
        assert delegate_list(status="pending")["delegations"] != []


class TestDelegateList:
    def test_status_and_agent_filters(self):
        _mk(to_agent="Desktop_agent3")
        _mk(to_agent="Laptop_agent3")
        assert len(delegate_list()["delegations"]) == 2
        only = delegate_list(to_agent="Laptop_agent3")["delegations"]
        assert len(only) == 1 and only[0]["to_agent"] == "Laptop_agent3"
        with pytest.raises(ToolError, match="status"):
            delegate_list(status="bogus")

    def test_delegation_survives_memory_note_failure(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr("unified_agent.tools.memory.memory_save", boom)
        r = _mk()
        assert r["memory_id"] is None
        assert delegate_list()["delegations"][0]["memory_id"] is None


class TestInterruptedHold:
    """A delegation of interrupted work is not offered to the queue (task #2482)."""

    def test_it_is_listed_as_held_and_cannot_be_claimed_until_the_task_is_reopened(self):
        r = _mk()
        claim = {"id": r["delegation_id"], "agent": "Desktop_agent3"}
        assert delegation_manage("claim", claim)["claimed"]
        task_claim(r["task_id"], "Desktop_agent3")
        conn = storage.connect()
        try:
            # The delegate's lease lapses with its run open and no trace of it left.
            conn.execute(
                "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (r["task_id"],),
            )
            conn.execute("DELETE FROM agents WHERE label='Desktop_agent3'")
            [reaped] = lease_reaper.reap_stale_claim_leases(conn)
            assert reaped.to_status == "blocked"
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=0 WHERE id=?",
                (r["delegation_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        [listed] = delegate_list(status="pending", to_agent="Desktop_agent3")["delegations"]
        assert "held as interrupted work" in listed["held"]
        refused = delegation_manage("claim", claim)
        assert refused["claimed"] is False and refused["held"] == listed["held"]
        row = refused["delegation"]
        assert (row["status"], row["attempts"], row["claim_generation"]) == ("pending", 0, 1)

        task_update(r["task_id"], status="open", worktree=False)
        [listed] = delegate_list(status="pending", to_agent="Desktop_agent3")["delegations"]
        assert "held" not in listed
        assert delegation_manage("claim", claim)["claimed"] is True


class TestApprovalGate:
    """check_approval_gate wiring (task #115, reconciling design-doc T5)."""

    def test_gated_by_baseline_when_approval_tools_unset(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        monkeypatch.delenv("UA_APPROVAL_BASELINE", raising=False)
        monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
        with pytest.raises(ApprovalPendingError, match="queued"):
            _mk()

    def test_ungated_when_baseline_disabled(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
        r = _mk()
        assert "delegation_id" in r

    def test_gated_when_listed_and_no_side_effects(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        with pytest.raises(ApprovalPendingError, match="queued"):
            _mk()
        # rejected before any task/note/delegation row was created
        assert delegate_list(status="all")["delegations"] == []
        assert task_list(status="open", notes_contain="[delegated-to:")["tasks"] == []
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        assert pending[0]["tool"] == "delegate"
        assert pending[0]["requested_by"] == "Desktop_Agent1"


class TestApprovalGateHumanExemption:
    """Task #366: a human web-login identity (config.IDENTITY_ALIAS_ALLOWLIST)
    already gave explicit authorization by submitting Dispatch through an
    authenticated /ui session — that call skips the approval queue. Every
    agent identity is unaffected, gated exactly as TestApprovalGate covers."""

    def test_verified_human_identity_bypasses_the_gate(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            r = _mk(from_agent="Operator")
            assert "delegation_id" in r
        finally:
            config.agent_identity_var.reset(token)
        # actually created, not queued
        assert approval_pending()["pending"] == []

    def test_agent_identity_is_still_gated_even_if_claiming_the_alias(self, monkeypatch):
        """An agent's own bearer-token identity can't borrow the exemption by
        merely passing from_agent="Operator" — verify_write_identity() above
        this check already pins from_agent to what the verified caller may
        claim, and the exemption itself is keyed off the verified caller, not
        the (possibly attacker-claimed) from_agent string."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            with pytest.raises(ApprovalPendingError, match="queued"):
                _mk(from_agent="Operator")
        finally:
            config.agent_identity_var.reset(token)

    def test_verified_operator_alias_bypasses_the_gate(self, monkeypatch):
        """Task #2535: an operator alias that the transport verified is the
        operator, exactly as the approval gate's self-approval decides. Before
        the shared definition, delegate() matched only the login label
        case-sensitively and parked this call."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        alias = sorted(config.operator_aliases())[0]
        token = config.agent_identity_var.set(alias)
        try:
            assert "delegation_id" in _mk(from_agent=alias)
        finally:
            config.agent_identity_var.reset(token)
        assert approval_pending()["pending"] == []

    def test_agent_bearer_named_as_an_operator_alias_is_still_gated(self, monkeypatch):
        """Task #2535: a credential-backed agent label listed as an operator
        alias is dropped from the aliases, so that bearer gets neither the
        delegate human skip nor the operator self-approval pass."""
        from unified_agent.tools import approvals

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        monkeypatch.setenv("UA_AGENT_TOKENS", "Bot_Alias:tok-bot-alias")
        monkeypatch.setenv(
            "UA_OPERATOR_ALIASES", f"{os.environ.get('UA_OPERATOR_ALIASES', '')},Bot_Alias"
        )
        token = config.agent_identity_var.set("Bot_Alias")
        try:
            assert "Bot_Alias" not in config.operator_aliases()
            assert config.verified_caller_is_operator() is False
            assert approvals._operator_self_approval_applies("task_delete", "Bot_Alias") is False
            with pytest.raises(ApprovalPendingError, match="queued"):
                _mk(from_agent="Bot_Alias")
        finally:
            config.agent_identity_var.reset(token)

    def test_delegate_and_self_approval_agree_on_the_verified_operator(self, monkeypatch):
        """Task #2535: one definition of "the verified caller is the operator".
        delegate()'s human skip and the approval gate's self-approval pass must
        give the same answer for every verified identity."""
        from unified_agent.tools import approvals
        from unified_agent.tools.delegate import _create_delegation

        label = config.operator_label()
        alias = sorted(config.operator_aliases())[0]
        table = {
            label: True,
            alias: True,
            label.upper(): True,
            label.lower(): True,
            alias.upper(): True,
            alias.lower(): True,
            "Desktop_Agent2": False,
            "": False,
            None: False,
        }

        class Gated(Exception):
            pass

        def gate(*args, **kwargs):
            raise Gated

        monkeypatch.setattr(approvals, "check_approval_gate", gate)
        request = {
            "task_spec": "Rebase the fix branch onto main\nDetails: see broadcast.",
            "from_agent": "Desktop_Agent1",
            "to_agent": "Desktop_agent3",
            "deadline": None,
            "callback": "none",
            "priority": "med",
            "parent_task_id": None,
            "project": None,
        }
        for identity, expected in table.items():
            token = config.agent_identity_var.set(identity)
            try:
                try:
                    _create_delegation(dict(request), {}, [], None)
                    delegate_skipped = True
                except Gated:
                    delegate_skipped = False
                self_approval = approvals._operator_self_approval_applies("task_delete", None)
                assert config.verified_caller_is_operator() is expected, identity
                assert delegate_skipped is expected, identity
                assert self_approval is expected, identity
            finally:
                config.agent_identity_var.reset(token)


class TestDelegationLedger:
    def test_delegation_ledger_summary(self):
        from unified_agent.tools.delegate import delegation_ledger

        r = _mk(from_agent="Desktop_Agent1", to_agent="Desktop_agent3")
        task_update(
            r["task_id"],
            status="done",
            notes="[delegated-by: Desktop_Agent1] [delegated-to: Desktop_agent3]\n[review: merged-upstream]",
            actor="test-agent",
        )

        ledger = delegation_ledger()
        assert ledger["total_delegations_analyzed"] >= 1
        assert "Desktop_agent3" in ledger["agent_stats"]
        st = ledger["agent_stats"]["Desktop_agent3"]
        assert st["received"] >= 1
        assert st["outcomes"]["merged"] >= 1
        assert st["acceptance_rate"] == 100.0
        assert "Delegation Outcome Ledger" in ledger["summary_markdown"]


def _fail_pair(n: int, *, from_agent="Desktop_Agent5", to_agent="server_agent2", reason="boom"):
    """Create n terminal-failed delegations for a from→to pair (task #1142)."""
    from unified_agent.tools.delegate import delegate_fail

    ids = []
    for i in range(n):
        r = _mk(from_agent=from_agent, to_agent=to_agent, task_spec=f"Fail pair case {i}")
        delegate_fail(r["delegation_id"], agent=to_agent, reason=reason)
        ids.append(r)
    return ids


class TestOutcomesAwaitingReview:
    """Queryable projection of terminal outcomes lacking a review annotation (#1142)."""

    def test_lists_unreviewed_terminal_outcomes(self):
        from unified_agent.tools.delegate import (
            delegate_fail,
            outcomes_awaiting_review,
        )

        failed = _mk(from_agent="Desktop_Agent5", to_agent="server_agent2")
        delegate_fail(failed["delegation_id"], agent="server_agent2", reason="quota exhausted")

        cancelled = _mk(from_agent="Desktop_Agent1", to_agent="Desktop_agent3")
        delegation_manage(
            "cancel",
            {"id": cancelled["delegation_id"], "agent": "Desktop_Agent1", "reason": "not needed"},
        )

        reviewed = _mk(from_agent="Desktop_Agent1", to_agent="Desktop_agent3")
        from unified_agent.tools.delegate import delegate_complete

        delegate_complete(reviewed["delegation_id"], agent="Desktop_agent3", result="ok")
        task_update(
            reviewed["task_id"],
            notes="[delegated-to: Desktop_agent3]\n[review: merged-upstream]",
            actor="test-agent",
        )

        pending = _mk(from_agent="Desktop_Agent1", to_agent="Desktop_agent3")

        rows = outcomes_awaiting_review()["outcomes"]
        ids = {r["delegation_id"] for r in rows}
        assert failed["delegation_id"] in ids
        assert cancelled["delegation_id"] in ids
        assert reviewed["delegation_id"] not in ids
        assert pending["delegation_id"] not in ids
        failed_row = next(r for r in rows if r["delegation_id"] == failed["delegation_id"])
        assert failed_row["status"] == "failed"
        assert failed_row["from_agent"] == "Desktop_Agent5"
        assert failed_row["to_agent"] == "server_agent2"


class TestFailurePairReportAndGate:
    """Pair consecutive-failure counters + failure_class report (#1142 / #1081)."""

    def test_pair_failure_status_blocks_at_three_consecutive(self):
        from unified_agent.tools.delegate import pair_failure_status

        _fail_pair(3, reason="provider quota exhausted")
        status = pair_failure_status("Desktop_Agent5", "server_agent2")
        assert status["consecutive_failures"] == 3
        assert status["blocked"] is True
        assert status["reason"] == "failure_pair_gate"
        assert status["last_failure_class"] == "provider_quota_exhausted"

    def test_review_annotation_resets_pair_counter(self):
        from unified_agent.tools.delegate import pair_failure_status

        rows = _fail_pair(3, reason="code boom")
        # Annotate the most recent failure — human-visible reset.
        task_update(
            rows[-1]["task_id"],
            notes="[delegated-to: server_agent2]\n[review: rework — root-caused]",
            actor="test-agent",
        )
        status = pair_failure_status("Desktop_Agent5", "server_agent2")
        assert status["consecutive_failures"] == 0
        assert status["blocked"] is False
        assert status["reason"] is None

    def test_success_resets_pair_counter(self):
        from unified_agent.tools.delegate import delegate_complete, pair_failure_status

        _fail_pair(3, reason="boom")
        ok = _mk(
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            task_spec="Intervening success",
        )
        delegate_complete(ok["delegation_id"], agent="server_agent2", result="ok")
        status = pair_failure_status("Desktop_Agent5", "server_agent2")
        assert status["consecutive_failures"] == 0
        assert status["blocked"] is False

    def test_failure_pair_report_lists_counts_and_last_failure_class(self):
        from unified_agent.tools.delegate import failure_pair_report, pair_failure_status

        _fail_pair(3, reason="usage limit has been reached")
        # A different pair with only 1 failure should appear with count 1.
        _fail_pair(1, from_agent="Desktop_Agent1", to_agent="Desktop_agent3", reason="timeout")

        report = failure_pair_report()
        pairs = {(p["from_agent"], p["to_agent"]): p for p in report["pairs"]}
        hermes = pairs[("Desktop_Agent5", "server_agent2")]
        assert hermes["consecutive_failures"] == 3
        assert hermes["last_failure_class"] == "provider_quota_exhausted"
        assert hermes["blocked"] is True

        claude = pairs[("Desktop_Agent1", "Desktop_agent3")]
        assert claude["consecutive_failures"] == 1
        assert claude["blocked"] is False

        # Ledger surfaces the same report.
        from unified_agent.tools.delegate import delegation_ledger

        ledger = delegation_ledger()
        assert "failure_pairs" in ledger
        assert any(
            p["from_agent"] == "Desktop_Agent5" and p["consecutive_failures"] >= 3
            for p in ledger["failure_pairs"]
        )
        # Sanity: helper and report agree.
        assert pair_failure_status("Desktop_Agent5", "server_agent2")["blocked"] is True


class TestDelegateIdempotency:
    """Keyed delegation create and its no-effect lookup (task #1938)."""

    @staticmethod
    def _rows():
        conn = storage.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0]
        finally:
            conn.close()

    def _lookup(self, key, agent="Desktop_Agent1"):
        return delegation_manage("lookup", {"idempotency_key": key, "from_agent": agent})

    def test_unkeyed_create_is_unchanged_and_writes_no_receipt(self):
        first, second = _mk(), _mk()
        assert first["delegation_id"] != second["delegation_id"]
        assert self._lookup("never-sent") == {"idempotency_key": "never-sent", "state": "not_found"}

    def test_keyed_retry_replays_the_committed_result(self):
        first = _mk(idempotency_key="key-1")
        assert _mk(idempotency_key="key-1") == first
        assert self._rows() == 1
        looked = self._lookup("key-1")
        assert looked["state"] == "committed" and looked["result"] == first

    def test_reusing_a_key_for_another_request_is_a_conflict(self):
        from unified_agent.tool_errors import ConflictError

        _mk(idempotency_key="key-2")
        with pytest.raises(ConflictError, match="different delegation request"):
            _mk(idempotency_key="key-2", priority="high")
        assert self._rows() == 1

    def test_keys_are_scoped_to_the_delegator(self):
        mine = _mk(idempotency_key="key-3")
        theirs = _mk(
            idempotency_key="key-3", from_agent="Desktop_agent3", to_agent="Desktop_Agent1"
        )
        assert mine["delegation_id"] != theirs["delegation_id"]
        assert self._lookup("key-3", "Desktop_agent3")["result"] == theirs

    def test_parked_request_is_recorded_then_consumed_once(self, monkeypatch):
        from unified_agent.tools.approvals import approval_resolve

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate")
        with pytest.raises(ApprovalPendingError) as parked:
            _mk(idempotency_key="key-4")
        approval_id = parked.value.approval_id
        assert self._lookup("key-4") == {
            "idempotency_key": "key-4",
            "state": "approval_required",
            "approval_id": approval_id,
        }
        operator = config.operator_label()
        token = config.agent_identity_var.set(operator)
        try:
            approval_resolve(approval_id, "approve", resolved_by=operator)
        finally:
            config.agent_identity_var.reset(token)
        created = _mk(idempotency_key="key-4")
        # The replay needs no second approval and adds no row.
        assert _mk(idempotency_key="key-4") == created
        assert self._rows() == 1 and approval_pending()["pending"] == []

    def test_refusal_before_commit_releases_the_key(self, monkeypatch):
        # The trust boundary refuses after the key is held; the key is released.
        untrusted = {"Desktop_Agent1"}
        monkeypatch.setattr(config, "is_agent_untrusted", lambda label: label in untrusted)
        with pytest.raises(ToolError, match="Trust boundary"):
            _mk(idempotency_key="key-5")
        assert self._lookup("key-5")["state"] == "not_found"
        untrusted.clear()
        assert _mk(idempotency_key="key-5")["delegation_id"]

    def test_lookup_follows_create_permission(self, monkeypatch):
        from unified_agent import profiles

        # A profile that exposed lookup but not create must not read receipts.
        monkeypatch.setattr(
            profiles,
            "consolidated_action_is_allowed",
            lambda manager, action: action != "delegate",
        )
        with pytest.raises(ToolError, match="'lookup' is not exposed"):
            self._lookup("key-6")

    @pytest.mark.parametrize("key", ["", "has space", "x" * 129, "a\x1fb", "-leading"])
    def test_malformed_keys_are_refused_before_any_effect(self, key):
        with pytest.raises(ToolError, match="idempotency_key"):
            _mk(idempotency_key=key)
        assert self._rows() == 0
