"""Tests for the plan-and-reason primitives (task #61, parts A–C).

Dumb storage: these exercise persistence, dependency hints, checkpoint/resume,
and bounded retries with human escalation. No LLM is involved and nothing is
auto-executed.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.plan import (
    plan_checkpoint,
    plan_close,
    plan_create,
    plan_delete,
    plan_get,
    plan_handoff,
    plan_list,
    plan_manage,
    plan_resume,
    plan_step_claim,
    plan_step_retry,
    plan_update,
)


class TestGoalDecomposition:
    def test_create_with_string_steps(self):
        r = plan_create("Ship the feature", ["design", "build", "test"])
        assert "plan_id" in r
        assert [s["description"] for s in r["steps"]] == ["design", "build", "test"]
        assert [s["step_idx"] for s in r["steps"]] == [0, 1, 2]
        assert all(s["status"] == "open" for s in r["steps"])

    def test_plan_manage_dispatches_create_and_get(self):
        created = plan_manage(
            "create", {"goal": "Use the compatibility API", "steps": ["inspect", "ship"]}
        )
        assert created["plan_id"]
        fetched = plan_manage("get", {"plan_id": created["plan_id"]})
        assert fetched["plan"]["goal"] == "Use the compatibility API"

    def test_plan_manage_rejects_unknown_action_and_non_object_params(self):
        with pytest.raises(ToolError, match="action must be a string"):
            plan_manage(1)
        with pytest.raises(ToolError, match="action must be one of"):
            plan_manage("unknown")
        with pytest.raises(ToolError, match="params must be an object"):
            plan_manage("list", [])

    def test_plan_manage_enforces_profile_and_policy(self, monkeypatch):
        from unified_agent import config, profiles
        from unified_agent.tools import plan as plan_tool

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("plan-manage-test")
        policy_calls = []
        monkeypatch.setattr(
            plan_tool.safety,
            "check_policy_gate",
            lambda tool, payload: policy_calls.append((tool, payload)),
        )
        try:
            plan_manage("list")
            assert policy_calls == [("plan_list", {})]
            profiles.activate("steward")
            with pytest.raises(ToolError, match="not exposed"):
                plan_manage("list")
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.clear()

    def test_create_with_dict_steps_and_deps(self):
        r = plan_create(
            "Ship",
            [
                {"description": "design", "owner": "alice"},
                {"description": "build", "depends_on": [0]},
                {"description": "test", "depends_on": [1]},
            ],
        )
        steps = r["steps"]
        assert steps[0]["owner"] == "alice"
        assert steps[1]["depends_on"] == [0]
        assert steps[2]["depends_on"] == [1]

    def test_create_rejects_empty_goal(self):
        with pytest.raises(ToolError, match="goal"):
            plan_create("  ", ["a"])

    def test_create_rejects_empty_steps(self):
        with pytest.raises(ToolError, match="non-empty list"):
            plan_create("goal", [])

    def test_create_rejects_bad_dependency(self):
        with pytest.raises(ToolError, match="out of range"):
            plan_create("goal", [{"description": "a", "depends_on": [5]}])
        with pytest.raises(ToolError, match="itself"):
            plan_create("goal", [{"description": "a", "depends_on": [0]}])

    def test_update_step_status_and_done_at(self):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        r = plan_update(pid, 0, status="done", note="finished")
        assert r["step"]["status"] == "done"
        assert r["step"]["done_at"] is not None
        assert r["step"]["note"] == "finished"
        # next actionable is step 1 (its deps — none — are satisfied)
        assert r["next_step_idx"] == 1

    def test_update_rejects_bad_status(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="status must be one of"):
            plan_update(pid, 0, status="nonsense")

    def test_update_unknown_plan_or_step(self):
        with pytest.raises(ToolError, match="no plan"):
            plan_update("deadbeef0000", 0, status="done")
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="no step"):
            plan_update(pid, 9, status="done")

    def test_next_actionable_respects_dependencies(self):
        pid = plan_create("g", [{"description": "a"}, {"description": "b", "depends_on": [0]}])[
            "plan_id"
        ]
        # b is blocked on a; the first actionable step is a (0)
        assert plan_resume(pid)["next_step_idx"] == 0
        plan_update(pid, 0, status="done")
        assert plan_resume(pid)["next_step_idx"] == 1

    def test_plan_autocompletes_when_all_steps_done(self):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        plan_update(pid, 0, status="done")
        r = plan_update(pid, 1, status="done")
        assert r["plan"]["status"] == "done"
        assert r["next_step_idx"] is None


class TestListingAndDeletion:
    def test_list_and_scope_filter(self):
        p1 = plan_create("g1", ["a"], scope="proj-x")["plan_id"]
        plan_create("g2", ["a"], scope="proj-y")
        got = plan_list(scope="proj-x")["plans"]
        assert [p["id"] for p in got] == [p1]

    def test_list_status_all(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_update(pid, 0, status="done")  # autocompletes -> status done
        assert plan_list(status="active")["plans"] == []
        assert len(plan_list(status="all")["plans"]) == 1

    def test_delete_cascades(self):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        plan_checkpoint(pid, note="cp")
        assert plan_delete(pid)["deleted"] is True
        with pytest.raises(ToolError, match="no plan"):
            plan_get(pid)
        # deleting again is a no-op
        assert plan_delete(pid)["deleted"] is False


class TestClose:
    """Task #1496: a superseded plan reaches 'abandoned' with its history intact."""

    def test_close_marks_abandoned_and_keeps_steps_and_checkpoints(self):
        pid = plan_create("g", ["a", "b", "c"])["plan_id"]
        plan_update(pid, 0, status="done")
        plan_checkpoint(pid, note="cp")
        r = plan_close(pid, "superseded by #1450", outputs={"1": "task #887 PR #456"})
        assert r["plan"]["status"] == "abandoned"
        assert r["seq"] == 2
        ctx = r["checkpoint"]["context"]
        assert ctx["outputs"] == {
            "1": "task #887 PR #456",
            "closed_as": "abandoned",
            "reason": "superseded by #1450",
        }
        assert [s["status"] for s in ctx["step_state"]] == ["done", "open", "open"]
        got = plan_get(pid)
        assert [s["status"] for s in got["steps"]] == ["done", "open", "open"]
        assert [c["seq"] for c in got["checkpoints"]] == [1, 2]

    def test_closed_plan_leaves_the_active_list(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_close(pid, "dropped")
        assert plan_list(status="active")["plans"] == []
        assert [p["id"] for p in plan_list(status="abandoned")["plans"]] == [pid]

    def test_later_step_updates_do_not_revive_a_closed_plan(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_close(pid, "dropped")
        plan_update(pid, 0, status="done")
        assert plan_get(pid)["plan"]["status"] == "abandoned"

    @pytest.mark.parametrize("reason", ["", "   ", None])
    def test_close_requires_a_reason(self, reason):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="reason"):
            plan_close(pid, reason)
        assert plan_get(pid)["plan"]["status"] == "active"
        assert plan_get(pid)["checkpoints"] == []

    def test_close_refuses_a_terminal_plan(self):
        done = plan_create("g", ["a"])["plan_id"]
        plan_update(done, 0, status="done")
        with pytest.raises(ToolError, match="already done"):
            plan_close(done, "late")
        closed = plan_create("g", ["a"])["plan_id"]
        plan_close(closed, "once")
        with pytest.raises(ToolError, match="already abandoned"):
            plan_close(closed, "twice")
        assert len(plan_get(closed)["checkpoints"]) == 1

    def test_close_rejects_non_object_outputs_and_unknown_plan(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="outputs"):
            plan_close(pid, "r", outputs=["x"])
        with pytest.raises(ToolError, match="no plan"):
            plan_close("nope", "r")

    def test_plan_manage_dispatches_close(self):
        pid = plan_create("g", ["a"])["plan_id"]
        r = plan_manage("close", {"plan_id": pid, "reason": "superseded"})
        assert r["plan"]["status"] == "abandoned"


class TestCheckpointing:
    def test_checkpoint_sequences_and_snapshots_step_state(self):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        plan_update(pid, 0, status="done")
        c1 = plan_checkpoint(
            pid,
            memory_ids=["note1"],
            file_paths=["src/x.py"],
            outputs={"result": 42},
            note="after step 0",
        )
        assert c1["seq"] == 1
        ctx = c1["checkpoint"]["context"]
        assert ctx["memory_ids"] == ["note1"]
        assert ctx["file_paths"] == ["src/x.py"]
        assert ctx["outputs"] == {"result": 42}
        # step state snapshot captured automatically
        snap = {s["step_idx"]: s["status"] for s in ctx["step_state"]}
        assert snap == {0: "done", 1: "open"}
        c2 = plan_checkpoint(pid, note="second")
        assert c2["seq"] == 2

    def test_checkpoint_unknown_plan(self):
        with pytest.raises(ToolError, match="no plan"):
            plan_checkpoint("deadbeef0000")


class TestResume:
    def test_resume_cold_reader_picks_up_latest_checkpoint(self):
        pid = plan_create("g", ["a", "b", "c"])["plan_id"]
        plan_update(pid, 0, status="done")
        plan_checkpoint(pid, outputs={"phase": "one"}, note="first")
        plan_update(pid, 1, status="done")
        plan_checkpoint(pid, outputs={"phase": "two"}, note="second")

        r = plan_resume(pid)
        assert r["checkpoint"]["seq"] == 2
        assert r["checkpoint"]["context"]["outputs"] == {"phase": "two"}
        assert r["next_step_idx"] == 2  # step c is next

    def test_resume_specific_checkpoint(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_checkpoint(pid, outputs={"v": 1})
        plan_checkpoint(pid, outputs={"v": 2})
        r = plan_resume(pid, from_checkpoint=1)
        assert r["checkpoint"]["context"]["outputs"] == {"v": 1}

    def test_resume_without_checkpoints(self):
        pid = plan_create("g", ["a"])["plan_id"]
        r = plan_resume(pid)
        assert r["checkpoint"] is None
        assert r["next_step_idx"] == 0

    def test_resume_unknown_checkpoint(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="checkpoint seq"):
            plan_resume(pid, from_checkpoint=99)


class TestSelfCorrection:
    def test_retry_increments_and_escalates_at_cap(self, monkeypatch):
        monkeypatch.setenv("UA_PLAN_MAX_RETRIES", "3")
        pid = plan_create("g", ["a"])["plan_id"]

        r1 = plan_step_retry(pid, 0, "network flaked")
        assert r1["retries"] == 1
        assert r1["escalated"] is False
        assert r1["step"]["status"] == "failed"
        assert r1["max_retries"] == 3

        r2 = plan_step_retry(pid, 0, "still flaking")
        assert r2["retries"] == 2 and r2["escalated"] is False

        r3 = plan_step_retry(pid, 0, "gave up")
        assert r3["retries"] == 3
        assert r3["escalated"] is True
        assert r3["step"]["status"] == "blocked"
        assert r3["plan"]["status"] == "blocked"
        assert r3["plan"]["needs_human"] is True
        assert "gave up" in r3["plan"]["escalation_note"]

        # the needs_human flag is surfaced when browsing, not just in the retry response
        assert plan_get(pid)["plan"]["needs_human"] is True
        listed = plan_list(status="blocked")["plans"]
        assert any(p["id"] == pid and p["needs_human"] is True for p in listed)

    def test_retry_respects_configured_cap(self, monkeypatch):
        monkeypatch.setenv("UA_PLAN_MAX_RETRIES", "1")
        pid = plan_create("g", ["a"])["plan_id"]
        r = plan_step_retry(pid, 0, "one and done")
        assert r["escalated"] is True
        assert r["max_retries"] == 1

    def test_retry_records_escalation_channel(self, monkeypatch):
        monkeypatch.setenv("UA_PLAN_ESCALATION_CHANNEL", "inbox")
        pid = plan_create("g", ["a"])["plan_id"]
        r = plan_step_retry(pid, 0, "boom")
        assert r["escalation_channel"] == "inbox"

    def test_retry_reason_appended_to_note(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_step_retry(pid, 0, "first fail")
        r = plan_step_retry(pid, 0, "second fail")
        assert "first fail" in r["step"]["note"]
        assert "second fail" in r["step"]["note"]

    def test_retry_rejects_empty_reason(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="reason"):
            plan_step_retry(pid, 0, "  ")


class TestEndToEndDemo:
    """create → progress two steps → checkpoint → a *fresh* reader resumes cold and finishes."""

    def test_full_lifecycle(self):
        # An agent decomposes a goal.
        created = plan_create(
            "Migrate the config module",
            [
                {"description": "audit current config usage"},
                {"description": "write new config loader", "depends_on": [0]},
                {"description": "swap call sites", "depends_on": [1]},
                {"description": "delete old module", "depends_on": [2]},
            ],
            scope="unified-agent",
            author="Desktop_Agent1",
        )
        pid = created["plan_id"]

        # It completes the first two steps.
        plan_update(pid, 0, status="done", note="grepped 14 call sites")
        plan_update(pid, 1, status="done", note="loader in config.py")

        # It checkpoints its context before the session ends.
        plan_checkpoint(
            pid,
            memory_ids=["config-decision"],
            file_paths=["src/unified_agent/config.py"],
            outputs={"call_sites": 14, "loader": "done"},
            note="two of four steps done; loader ready",
        )

        # --- A fresh reader (new session, no prior context) resumes cold. ---
        resumed = plan_resume(pid)
        assert resumed["plan"]["goal"] == "Migrate the config module"
        assert resumed["next_step_idx"] == 2  # swap call sites
        assert resumed["checkpoint"]["context"]["outputs"]["call_sites"] == 14
        done_steps = [s["step_idx"] for s in resumed["steps"] if s["status"] == "done"]
        assert done_steps == [0, 1]

        # The fresh reader finishes the remaining steps.
        plan_update(pid, 2, status="done")
        final = plan_update(pid, 3, status="done")
        assert final["plan"]["status"] == "done"
        assert final["next_step_idx"] is None


class TestClaimAndHandoff:
    """Task #62 additions: atomic claiming + outputs handoff for multi-agent chains."""

    def test_claim_open_step(self):
        pid = plan_create("g", ["a"])["plan_id"]
        r = plan_step_claim(pid, 0, "Desktop_agent3")
        assert r["claimed"] is True
        assert r["step"]["owner"] == "Desktop_agent3"
        assert r["step"]["status"] == "doing"

    def test_claim_lost_race_names_holder(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_step_claim(pid, 0, "Desktop_agent3")
        with pytest.raises(ToolError, match="not claimable.*Desktop_agent3"):
            plan_step_claim(pid, 0, "Laptop_agent3")

    def test_reclaim_own_failed_step(self):
        pid = plan_create("g", ["a"])["plan_id"]
        plan_step_claim(pid, 0, "Desktop_agent3")
        plan_step_retry(pid, 0, "flaked")
        r = plan_step_claim(pid, 0, "Desktop_agent3")
        assert r["step"]["status"] == "doing"

    def test_claim_respects_preassigned_owner(self):
        pid = plan_create("g", [{"description": "a", "owner": "Laptop_Agent1"}])["plan_id"]
        with pytest.raises(ToolError, match="not claimable"):
            plan_step_claim(pid, 0, "Desktop_agent3")
        assert plan_step_claim(pid, 0, "Laptop_Agent1")["claimed"] is True

    def test_claim_validation(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="agent label"):
            plan_step_claim(pid, 0, "  ")
        with pytest.raises(ToolError, match="no step"):
            plan_step_claim(pid, 7, "Desktop_agent3")
        with pytest.raises(ToolError, match="no plan"):
            plan_step_claim("deadbeef0000", 0, "Desktop_agent3")
        plan_update(pid, 0, status="done")
        with pytest.raises(ToolError, match="not claimable"):
            plan_step_claim(pid, 0, "Desktop_agent3")

    def test_handoff_stores_outputs_and_points_at_next_owner(self):
        pid = plan_create(
            "g",
            [
                {"description": "build", "owner": "Desktop_agent3"},
                {"description": "review", "owner": "Desktop_Agent1", "depends_on": [0]},
            ],
        )["plan_id"]
        r = plan_handoff(pid, 0, outputs={"branch": "feat/x", "tests": 12}, note="built")
        assert r["step"]["status"] == "done"
        assert r["step"]["outputs"] == {"branch": "feat/x", "tests": 12}
        assert r["next_step"]["step_idx"] == 1
        assert r["next_step"]["owner"] == "Desktop_Agent1"
        assert r["notified"] is False  # notify_owner defaults off
        # the payload is readable by the next agent via plan_get/plan_resume
        got = plan_get(pid)["steps"][0]["outputs"]
        assert got["branch"] == "feat/x"

    def test_handoff_autocompletes_plan_on_last_step(self):
        pid = plan_create("g", ["only"])["plan_id"]
        r = plan_handoff(pid, 0, outputs={"done": True})
        assert r["plan"]["status"] == "done"
        assert r["next_step"] is None

    def test_handoff_notify_owner_best_effort(self, monkeypatch):
        sent = {}

        def fake_notify(message):
            sent["message"] = message
            return {"delivered": True, "chars": len(message), "error": None}

        monkeypatch.setattr("unified_agent.tools.notify.notify", fake_notify)
        pid = plan_create(
            "g",
            [
                {"description": "build", "owner": "Desktop_agent3"},
                {"description": "review", "owner": "Laptop_Agent1", "depends_on": [0]},
            ],
        )["plan_id"]
        r = plan_handoff(pid, 0, outputs={"x": 1}, notify_owner=True)
        assert r["notified"] is True
        assert "Laptop_Agent1" in sent["message"]
        assert pid in sent["message"]

    def test_handoff_notify_failure_never_fails_handoff(self, monkeypatch):
        def boom(message):
            raise RuntimeError("bridge down")

        monkeypatch.setattr("unified_agent.tools.notify.notify", boom)
        pid = plan_create(
            "g",
            [{"description": "a", "owner": "x"}, {"description": "b", "owner": "y"}],
        )["plan_id"]
        r = plan_handoff(pid, 0, notify_owner=True)
        assert r["step"]["status"] == "done"
        assert r["notified"] is False


class TestPlanStructure:
    """Step add, edit, remove and reorder, and plan title/description (task #2480)."""

    @staticmethod
    def _shape(pid):
        steps = plan_get(pid)["steps"]
        return [(s["step_idx"], s["description"], s["depends_on"]) for s in steps]

    def test_add_inserts_and_remaps_dependencies(self):
        pid = plan_create("g", ["a", {"description": "b", "depends_on": [0]}])["plan_id"]
        ids = {s["description"]: s["id"] for s in plan_get(pid)["steps"]}
        r = plan_manage(
            "step_add", {"plan_id": pid, "description": "first", "position": 0, "depends_on": []}
        )
        assert r["step"]["step_idx"] == 0
        assert self._shape(pid) == [(0, "first", []), (1, "a", []), (2, "b", [1])]
        # Row ids follow their step; only step_idx moves.
        after = {s["description"]: s["id"] for s in plan_get(pid)["steps"]}
        assert after["a"] == ids["a"] and after["b"] == ids["b"]
        plan_manage("step_add", {"plan_id": pid, "description": "last", "depends_on": [2]})
        assert self._shape(pid)[-1] == (3, "last", [2])
        with pytest.raises(ToolError, match="out of range"):
            plan_manage("step_add", {"plan_id": pid, "description": "x", "depends_on": [9]})

    def test_remove_and_reorder_remap_and_refuse_broken_dependencies(self):
        pid = plan_create("g", ["a", "b", {"description": "c", "depends_on": [1]}, "d"])["plan_id"]
        with pytest.raises(ToolError, match=r"step 2 depends on it"):
            plan_manage("step_remove", {"plan_id": pid, "step_idx": 1})
        plan_manage("step_remove", {"plan_id": pid, "step_idx": 0})
        assert self._shape(pid) == [(0, "b", []), (1, "c", [0]), (2, "d", [])]
        with pytest.raises(ToolError, match="ahead of step"):
            plan_manage("step_reorder", {"plan_id": pid, "order": [1, 0, 2]})
        with pytest.raises(ToolError, match="permutation"):
            plan_manage("step_reorder", {"plan_id": pid, "order": [0, 0, 1]})
        r = plan_manage("step_reorder", {"plan_id": pid, "order": [2, 0, 1]})
        assert [s["description"] for s in r["steps"]] == ["d", "b", "c"]
        assert self._shape(pid) == [(0, "d", []), (1, "b", []), (2, "c", [1])]

    @pytest.mark.parametrize(
        "action,params",
        [
            ("step_edit", {"step_idx": 1, "description": "new"}),
            ("step_remove", {"step_idx": 1}),
            ("step_reorder", {"order": [0, 2, 1]}),
            ("step_add", {"description": "x", "position": 0}),
        ],
    )
    @pytest.mark.parametrize("status", ["doing", "done"])
    def test_claimed_or_done_steps_are_not_touched(self, action, params, status):
        pid = plan_create("g", ["a", "b", "c"])["plan_id"]
        plan_update(pid, 1, status=status)
        before = self._shape(pid)
        with pytest.raises(ToolError, match=f"step 1 is {status}"):
            plan_manage(action, {"plan_id": pid, **params})
        assert self._shape(pid) == before

    def test_edit_step_text_and_plan_title_description(self):
        from unified_agent.tool_errors import ConflictError

        pid = plan_create("Ship it", ["a"])["plan_id"]
        plan = plan_get(pid)["plan"]
        assert (plan["title"], plan["description"]) == ("Ship it", "")
        edited = plan_manage("step_edit", {"plan_id": pid, "step_idx": 0, "description": " A2 "})
        assert edited["step"]["description"] == "A2"
        r = plan_manage("edit", {"plan_id": pid, "description": "Why and how."})
        assert r["plan"]["goal"] == "Ship it\n\nWhy and how."
        r = plan_manage("edit", {"plan_id": pid, "title": "Ship v2"})
        assert (r["plan"]["title"], r["plan"]["description"]) == ("Ship v2", "Why and how.")
        # Two writers read the same revision within one second: the second loses (409).
        seen = r["plan"]["revision"]
        plan_manage("edit", {"plan_id": pid, "title": "First", "expected_revision": seen})
        with pytest.raises(ConflictError):
            plan_manage("edit", {"plan_id": pid, "title": "Second", "expected_revision": seen})
        # Any plan write advances the revision, a step status change included.
        now = plan_get(pid)["plan"]["revision"]
        plan_update(pid, 0, note="touch")
        with pytest.raises(ConflictError):
            plan_manage(
                "step_edit",
                {"plan_id": pid, "step_idx": 0, "description": "x", "expected_revision": now},
            )
        with pytest.raises(ToolError, match="one nonempty line"):
            plan_manage("edit", {"plan_id": pid, "title": "two\nlines"})
        plan_close(pid, "done elsewhere")
        with pytest.raises(ToolError, match="abandoned"):
            plan_manage("step_add", {"plan_id": pid, "description": "late"})

    def test_new_actions_follow_plan_update_exposure(self):
        from unified_agent import profiles

        new = (
            "plan_step_add",
            "plan_step_edit",
            "plan_step_remove",
            "plan_step_reorder",
            "plan_edit",
        )
        for profile in sorted(profiles.VALID_PROFILES):
            expected = profiles.consolidated_action_is_allowed_for(
                profile, "plan_manage", "plan_update"
            )
            for action in new:
                assert (
                    profiles.consolidated_action_is_allowed_for(profile, "plan_manage", action)
                    is expected
                ), (profile, action)

    def test_step_id_guard_settles_claim_and_reorder_races(self):
        """Review #1516: a reorder between a read and a claim must not make the claim
        take another step; whichever write lands second gets a typed conflict."""
        from unified_agent.tool_errors import ConflictError

        pid = plan_create("g", ["a", "b", "c"])["plan_id"]
        step_b = plan_get(pid)["steps"][1]
        # Reorder wins: B moves to index 2, C to index 1; the stale claim is refused.
        plan_manage("step_reorder", {"plan_id": pid, "order": [0, 2, 1]})
        with pytest.raises(ConflictError, match="moved"):
            plan_manage(
                "claim",
                {
                    "plan_id": pid,
                    "step_idx": 1,
                    "agent": "Desktop_Agent2",
                    "expected_step_id": step_b["id"],
                },
            )
        assert all(s["status"] == "open" for s in plan_get(pid)["steps"])
        # Claim wins: B is claimed at its new index; a reorder that moves it is refused.
        claimed = plan_manage(
            "claim",
            {
                "plan_id": pid,
                "step_idx": 2,
                "agent": "Desktop_Agent2",
                "expected_step_id": step_b["id"],
            },
        )
        assert claimed["step"]["description"] == "b"
        with pytest.raises(ConflictError, match="step 2 is doing"):
            plan_manage("step_reorder", {"plan_id": pid, "order": [0, 2, 1]})
        # The index-only call keeps working for existing callers.
        assert plan_step_claim(pid, 1, "Desktop_Agent2")["step"]["description"] == "c"

    def test_step_id_guard_answers_conflict_when_the_index_is_gone(self):
        """Review round 2 nit: a guarded write whose index no longer exists is the same
        stale-step conflict, not a 400, for every guarded writer."""
        from unified_agent.tool_errors import ConflictError

        pid = plan_create("g", ["a", "b", "c"])["plan_id"]
        gone = plan_get(pid)["steps"][2]
        plan_manage("step_remove", {"plan_id": pid, "step_idx": 2})
        for action, extra in (
            ("update", {"note": "x"}),
            ("retry", {"reason": "x"}),
            ("claim", {"agent": "Desktop_Agent2"}),
            ("handoff", {}),
            ("step_edit", {"description": "x"}),
            ("step_remove", {}),
        ):
            with pytest.raises(ConflictError, match="moved"):
                plan_manage(
                    action,
                    {"plan_id": pid, "step_idx": 2, "expected_step_id": gone["id"], **extra},
                )
        with pytest.raises(ToolError, match="no step at index 2"):
            plan_manage("update", {"plan_id": pid, "step_idx": 2, "note": "x"})
