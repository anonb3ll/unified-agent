"""Terminal write graph checkpoint/restart and authority contract tests."""

from dataclasses import replace

import pytest

pytest.importorskip("langgraph")
from unified_agent.langgraph_note import ProgressNoteRunner  # noqa: E402
from unified_agent.langgraph_pilot import (  # noqa: E402
    DefinitionMismatchError,
    GovernedToolOutcome,
    InvalidRunError,
    PilotError,
    PilotRun,
    StaleAuthorityError,
)


@pytest.fixture
def run():
    return PilotRun(
        run_id="01JQ9Z6Y5K7M2N4P6R8T0V2X4Z",
        task_id=757,
        task_updated_at="2026-09-05T01:00:00Z",
        claimed_by="Desktop_Agent2",
        claim_expires_at="2026-09-05T23:00:00Z",
        expected_profile="developer",
        required_capabilities=("task_update",),
        budget_revision="v1",
        trace_id="trace-757",
        query="a" * 64,
    )


class Gateway:
    status = "pending"
    receipt = None
    calls = 0
    checks = 0
    stale = False
    crash = False

    def revalidate(self, run):
        from langsmith.run_helpers import get_tracing_context

        assert get_tracing_context()["enabled"] is False
        self.checks += 1
        if self.stale:
            raise StaleAuthorityError("stale")

    def reconcile_progress_note(self, *, run, output_digest):
        return self.receipt

    def call_progress_note(self, *, run, output_digest, timeout_s):
        self.calls += 1
        assert timeout_s == 20
        if self.crash:
            self.crash = False
            raise RuntimeError("before invocation")
        if self.status == "pending":
            return GovernedToolOutcome.pending("approval-757")
        if self.status == "denied":
            return GovernedToolOutcome.denied("denied")
        self.receipt = GovernedToolOutcome.allowed(
            {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "output_digest": output_digest,
                "note_appended": True,
            }
        )
        return self.receipt


def test_approval_restart_and_historical_duplicate(tmp_path, run):
    path = tmp_path / "notes.db"
    gateway = Gateway()
    with ProgressNoteRunner(path, gateway) as runner:
        assert runner.execute(run)["status"] == "interrupted"
        # A resume signal cannot approve anything.
        assert runner.execute(run, resume=True)["status"] == "interrupted"
    with ProgressNoteRunner(path, gateway) as runner:
        assert runner.load_run(run.run_id) == run
        gateway.status = "allowed"
        result = runner.execute(run, resume=True)
        assert result["status"] == "completed"
        assert result["output"]["note_appended"] is True
        calls = gateway.calls
        gateway.stale = True
        assert runner.execute(run)["status"] == "completed"
        assert gateway.calls == calls


def test_terminal_denial_never_reexecutes(tmp_path, run):
    gateway = Gateway()
    gateway.status = "denied"
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        assert runner.execute(run)["status"] == "denied"
        gateway.status = "allowed"
        assert runner.execute(run, resume=True)["status"] == "denied"
        assert gateway.calls == 1


def test_stale_resume_and_immutable_run_rejected(tmp_path, run):
    gateway = Gateway()
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        runner.execute(run)
        with pytest.raises(InvalidRunError):
            runner.execute(replace(run, query="b" * 64), resume=True)
        gateway.stale = True
        with pytest.raises(StaleAuthorityError):
            runner.execute(run, resume=True)
        assert gateway.calls == 1


def test_crash_resumes_scheduled_node_with_no_command(tmp_path, run):
    gateway = Gateway()
    gateway.crash = True
    path = tmp_path / "notes.db"
    with ProgressNoteRunner(path, gateway) as runner:
        with pytest.raises(RuntimeError, match="before invocation"):
            runner.execute(run)
        assert gateway.calls == 1
    with ProgressNoteRunner(path, gateway) as runner:
        assert runner.load_run(run.run_id) == run
        gateway.status = "allowed"
        assert runner.execute(run, resume=True)["status"] == "completed"
        assert gateway.calls == 2


def test_recovered_effect_skips_node_and_current_revision(tmp_path, run):
    gateway = Gateway()
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        runner.execute(run)
        gateway.receipt = GovernedToolOutcome.allowed(
            {
                "task_id": run.task_id,
                "run_id": run.run_id,
                "output_digest": run.query,
                "note_appended": True,
            }
        )
        gateway.stale = True
        assert runner.execute(run, resume=True)["status"] == "completed"
        assert gateway.calls == 1
        snapshot = runner._graph.get_state({"configurable": {"thread_id": run.thread_id}})
        assert snapshot.values["status"] == "completed"
        assert not snapshot.next


@pytest.mark.parametrize("query", ["hello", "A" * 64, "a" * 65])
def test_digest_rejected_before_authority(tmp_path, run, query):
    gateway = Gateway()
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        with pytest.raises(InvalidRunError):
            runner.execute(replace(run, query=query))
        assert gateway.calls == gateway.checks == 0


def test_checkpoint_corruption_and_definition_mismatch(tmp_path, run):
    gateway = Gateway()
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        runner.execute(run)
        cfg = {"configurable": {"thread_id": run.thread_id}}
        runner._graph.update_state(cfg, {"input_digest": "b" * 64})
        with pytest.raises(InvalidRunError):
            runner.load_run(run.run_id)
        runner._graph.update_state(cfg, {"definition_digest": "b" * 64})
        with pytest.raises(DefinitionMismatchError):
            runner.load_run(run.run_id)
        with pytest.raises(InvalidRunError):
            runner.load_run("../wrong")


def test_unbounded_raw_receipt_not_checkpointed(tmp_path, run):
    gateway = Gateway()
    gateway.receipt = GovernedToolOutcome.allowed({"notes": "private task body"})
    with ProgressNoteRunner(tmp_path / "notes.db", gateway) as runner:
        with pytest.raises(PilotError, match="effect receipt"):
            runner.execute(run)
        assert gateway.calls == 0


def test_digest_pins_bound_executable_node(tmp_path, monkeypatch):
    with ProgressNoteRunner(tmp_path / "first.db", Gateway()) as runner:
        original_digest = runner.definition_digest

    def changed_note(self, state):
        return {"status": "denied"}

    monkeypatch.setattr(ProgressNoteRunner, "_note_node", changed_note)
    with ProgressNoteRunner(tmp_path / "second.db", Gateway()) as runner:
        assert runner.definition_digest != original_digest
