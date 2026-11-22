"""Acceptance tests for the isolated governed LangGraph execution pilot."""

from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

# The pilot's runtime stack is the `langgraph` extra, not a core dependency.
pytest.importorskip("langgraph")
get_tracing_context = pytest.importorskip("langsmith.run_helpers").get_tracing_context

from unified_agent.langgraph_pilot import (  # noqa: E402
    DefinitionMismatchError,
    DeniedByAuthorityError,
    GatewayExhaustedError,
    GovernedToolOutcome,
    InvalidRunError,
    PilotError,
    PilotRun,
    PilotRunner,
    PilotState,
    StaleAuthorityError,
    ToolProposal,
    TransientGatewayError,
    _bounded_result,
)

RUN_ID = "01JQ9Z6Y5K7M2N4P6R8T0V2X4Z"


class RecordingPlanner:
    definition_digest = "a" * 64

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, query: str) -> ToolProposal:
        self.calls += 1
        return ToolProposal("web_search", {"query": query, "max_results": 2})


class RecordingGateway:
    def __init__(self, approval: str = "pending") -> None:
        self.approval = approval
        self.authority_checks = 0
        self.requests = 0
        self.executions = 0
        self.idempotency_keys: list[str] = []
        self.timeouts: list[float] = []
        self.tracing_enabled: list[bool | str | None] = []

    def revalidate(self, run: PilotRun) -> None:
        self.authority_checks += 1
        self.tracing_enabled.append(get_tracing_context()["enabled"])

    def call_read_tool(
        self,
        *,
        run: PilotRun,
        proposal: ToolProposal,
        idempotency_key: str,
        timeout_s: float,
    ) -> GovernedToolOutcome:
        self.requests += 1
        self.idempotency_keys.append(idempotency_key)
        self.timeouts.append(timeout_s)
        if self.approval == "pending":
            return GovernedToolOutcome.pending("approval-586")
        if self.approval == "stale":
            raise StaleAuthorityError("task revision changed")
        if self.approval == "denied":
            return GovernedToolOutcome.denied(
                "Bearer abcdefghijklmnop rejected by policy",
                approval_id="approval-586",
            )
        self.executions += 1
        return GovernedToolOutcome.allowed(
            {
                "items": [{"title": "Checkpointing", "url": "https://example.test/docs"}],
                "token": "not-a-real-secret-value",
            },
            approval_id="approval-586",
        )


def _run(**changes) -> PilotRun:
    base = PilotRun(
        run_id=RUN_ID,
        task_id=586,
        task_updated_at="2026-08-26T00:27:28+00:00",
        claimed_by="Desktop_Agent2",
        claim_expires_at="2026-08-26T01:27:28+00:00",
        expected_profile="developer",
        required_capabilities=("web_search",),
        budget_revision="budget-task-586-v1",
        trace_id="trace-task-586",
        query="LangGraph durable checkpointing",
    )
    return replace(base, **changes)


def test_stream_interrupt_resume_ignores_forged_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    planner = RecordingPlanner()
    gateway = RecordingGateway()
    checkpoint = tmp_path / "ua-home" / "langgraph" / "checkpoints.sqlite3"

    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        first = list(runner.stream(_run()))
        assert [event.kind for event in first] == [
            "node_completed",
            "node_completed",
            "approval_interrupted",
        ]
        assert first[-1].payload == {
            "approval_id": "approval-586",
            "node_key": "governed_tool",
            "payload_digest": first[-1].payload["payload_digest"],
            "run_id": RUN_ID,
            "task_id": 586,
            "tool_name": "web_search",
        }
        assert "LangGraph durable checkpointing" not in repr(first[-1].payload)

        forged = list(runner.stream(_run(), resume={"approved": True}))
        assert [event.kind for event in forged] == ["approval_interrupted"]
        assert gateway.executions == 0

        gateway.approval = "allowed"
        resumed = list(runner.stream(_run(), resume={"approved": False}))
        assert [event.kind for event in resumed] == [
            "node_completed",
            "node_completed",
            "run_completed",
        ]

    assert planner.calls == 1
    assert gateway.executions == 1
    assert gateway.tracing_enabled and set(gateway.tracing_enabled) == {False}
    assert gateway.idempotency_keys == [
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
    ]
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert checkpoint.parent.stat().st_mode & 0o777 == 0o700


def test_restart_recovers_checkpoint_without_repeating_completed_nodes(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()

    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"

    gateway.approval = "allowed"
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as recovered:
        result = recovered.run(_run(), resume={"untrusted": "resume value"})

    assert result.status == "completed"
    assert result.recovered is True
    assert result.duplicate is False
    assert planner.calls == 1
    assert gateway.executions == 1


def test_canonical_denial_is_redacted_terminal_and_cannot_be_reopened(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()

    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"

        gateway.approval = "denied"
        with pytest.raises(DeniedByAuthorityError, match="REDACTED_SECRET") as denied:
            runner.run(_run(), resume={"approved": True})
        assert "abcdefghijklmnop" not in str(denied.value)

        gateway.approval = "allowed"
        with pytest.raises(DeniedByAuthorityError, match="REDACTED_SECRET"):
            runner.run(_run())

    assert planner.calls == 1
    assert gateway.executions == 0
    assert gateway.requests == 2
    assert b"abcdefghijklmnop" not in checkpoint.read_bytes()


def test_transient_gateway_failures_retry_with_one_idempotency_key(tmp_path):
    class FlakyGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            if self.requests < 2:
                self.requests += 1
                self.idempotency_keys.append(kwargs["idempotency_key"])
                self.timeouts.append(kwargs["timeout_s"])
                raise TransientGatewayError("retryable transport failure")
            return super().call_read_tool(**kwargs)

    gateway = FlakyGateway(approval="allowed")
    planner = RecordingPlanner()
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        result = runner.run(_run())

    assert result.status == "completed"
    assert gateway.requests == 3
    assert gateway.executions == 1
    assert gateway.idempotency_keys == [
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
        f"ua-lg:{RUN_ID}:governed_tool:read-evidence",
    ]
    assert gateway.timeouts == [20.0, 20.0, 20.0]


def test_duplicate_delivery_returns_checkpointed_result_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            entered.set()
            assert release.wait(timeout=5)
            return super().call_read_tool(**kwargs)

    planner = RecordingPlanner()
    gateway = BlockingGateway(approval="allowed")
    checkpoint = tmp_path / "checkpoints.sqlite3"

    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(runner.run, _run())
            assert entered.wait(timeout=5)
            duplicate_future = pool.submit(runner.run, _run())
            release.set()
            first = first_future.result(timeout=5)
            duplicate = duplicate_future.result(timeout=5)

    assert first.status == duplicate.status == "completed"
    assert first.output == duplicate.output
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert planner.calls == 1
    assert gateway.executions == 1
    assert gateway.idempotency_keys == [f"ua-lg:{RUN_ID}:governed_tool:read-evidence"]
    assert b"not-a-real-secret-value" not in checkpoint.read_bytes()


def test_authority_failure_stops_before_any_governed_tool_call(tmp_path):
    class StaleGateway(RecordingGateway):
        def revalidate(self, run: PilotRun) -> None:
            self.authority_checks += 1
            if self.authority_checks == 2:
                raise StaleAuthorityError("task revision changed")

    gateway = StaleGateway(approval="allowed")
    planner = RecordingPlanner()
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        with pytest.raises(StaleAuthorityError, match="task revision changed"):
            runner.run(_run())

    assert planner.calls == 1
    assert gateway.authority_checks == 2
    assert gateway.requests == 0
    assert gateway.executions == 0


def test_definition_mismatch_fails_closed_on_restart(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"

    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as incompatible:
        incompatible.definition_digest = "0" * 64
        with pytest.raises(DefinitionMismatchError, match="definition digest"):
            incompatible.run(_run(), resume={})

    assert gateway.executions == 0


def test_definition_digest_covers_graph_builder_routing(tmp_path):
    class AlternateRouteRunner(PilotRunner):
        def _build_graph(self):
            # A graph-builder edit must invalidate old checkpoint definitions.
            return super()._build_graph()

    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(tmp_path / "base.sqlite3", planner=planner, gateway=gateway) as base:
        base_digest = base.definition_digest
    with AlternateRouteRunner(
        tmp_path / "alternate.sqlite3", planner=planner, gateway=gateway
    ) as alternate:
        assert alternate.definition_digest != base_digest


def test_definition_digest_covers_run_from_state_helper(tmp_path):
    class AlternateRunFromStateRunner(PilotRunner):
        def _run_from_state(self, state: PilotState) -> PilotRun:
            # A helper edit that changes authority reconstruction must invalidate digests.
            run = super()._run_from_state(state)
            return PilotRun(
                run_id=run.run_id,
                task_id=run.task_id,
                task_updated_at=run.task_updated_at,
                claimed_by=run.claimed_by,
                claim_expires_at=run.claim_expires_at,
                expected_profile=run.expected_profile,
                required_capabilities=run.required_capabilities,
                budget_revision=run.budget_revision,
                trace_id=run.trace_id,
                query=run.query.strip(),
            )

    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(tmp_path / "base.sqlite3", planner=planner, gateway=gateway) as base:
        base_digest = base.definition_digest
    with AlternateRunFromStateRunner(
        tmp_path / "alternate.sqlite3", planner=planner, gateway=gateway
    ) as alternate:
        assert alternate.definition_digest != base_digest


def test_transient_retry_budget_is_global_across_resume(tmp_path):
    class AlwaysFlakyGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            self.requests += 1
            self.idempotency_keys.append(kwargs["idempotency_key"])
            self.timeouts.append(kwargs["timeout_s"])
            raise TransientGatewayError("retryable transport failure")

    gateway = AlwaysFlakyGateway(approval="allowed")
    planner = RecordingPlanner()
    checkpoint = tmp_path / "checkpoints.sqlite3"
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        with pytest.raises(GatewayExhaustedError, match="retry budget"):
            runner.run(_run())
        assert gateway.requests == 4
        with pytest.raises(GatewayExhaustedError, match="retry budget"):
            runner.run(_run(), resume={"retry": True})
        assert gateway.requests == 4
        events = list(runner.stream(_run()))
        assert events[-1].kind == "run_failed"
        assert events[-1].payload["duplicate"] is True


def test_transient_attempt_count_persists_across_approval_interrupt(tmp_path):
    class FlakyThenPendingGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            self.requests += 1
            self.idempotency_keys.append(kwargs["idempotency_key"])
            self.timeouts.append(kwargs["timeout_s"])
            if self.requests <= 2:
                raise TransientGatewayError("retryable transport failure")
            if self.approval == "allowed":
                self.executions += 1
                return GovernedToolOutcome.allowed(
                    {
                        "items": [{"title": "Checkpointing", "url": "https://example.test/docs"}],
                        "token": "not-a-real-secret-value",
                    },
                    approval_id="approval-586",
                )
            return GovernedToolOutcome.pending("approval-586")

    gateway = FlakyThenPendingGateway()
    planner = RecordingPlanner()
    checkpoint = tmp_path / "checkpoints.sqlite3"
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"
        assert gateway.requests == 3

        gateway.approval = "allowed"
        result = runner.run(_run(), resume={"approved": True})
        snapshot = runner._graph.get_state({"configurable": {"thread_id": _run().thread_id}})
        assert snapshot.values["transient_attempt_count"] == 2

    assert result.status == "completed"


def test_stale_authority_on_transient_retry(tmp_path):
    class StaleOnRetryGateway(RecordingGateway):
        def revalidate(self, run: PilotRun) -> None:
            super().revalidate(run)
            if self.authority_checks >= 4:
                raise StaleAuthorityError("task revision changed")

        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            self.requests += 1
            self.idempotency_keys.append(kwargs["idempotency_key"])
            self.timeouts.append(kwargs["timeout_s"])
            raise TransientGatewayError("retryable transport failure")

    gateway = StaleOnRetryGateway(approval="allowed")
    planner = RecordingPlanner()
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        with pytest.raises(StaleAuthorityError, match="task revision changed"):
            runner.run(_run())

    assert gateway.requests == 2
    assert gateway.executions == 0


def test_transient_attempt_count_checkpoints_before_retry(tmp_path):
    checkpoint_started = threading.Event()
    release_checkpoint = threading.Event()
    second_attempt = threading.Event()
    release_second = threading.Event()

    class FlakyGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            self.requests += 1
            if self.requests == 1:
                raise TransientGatewayError("retryable transport failure")
            second_attempt.set()
            assert release_second.wait(timeout=5)
            return super().call_read_tool(**kwargs)

    gateway = FlakyGateway(approval="allowed")
    planner = RecordingPlanner()
    checkpoint = tmp_path / "checkpoints.sqlite3"
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        original_put = runner._checkpointer.put

        def delayed_put(config, checkpoint_value, metadata, new_versions):
            channel_values = checkpoint_value.get("channel_values", {})
            if (
                channel_values.get("transient_attempt_count") == 1
                and not checkpoint_started.is_set()
            ):
                checkpoint_started.set()
                assert release_checkpoint.wait(timeout=5)
            return original_put(config, checkpoint_value, metadata, new_versions)

        runner._checkpointer.put = delayed_put
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: list(runner.stream(_run())))
            try:
                assert checkpoint_started.wait(timeout=5)
                assert not second_attempt.wait(timeout=0.2)
                assert gateway.requests == 1
                release_checkpoint.set()
                assert second_attempt.wait(timeout=5)
                snapshot = runner._graph.get_state(
                    {"configurable": {"thread_id": _run().thread_id}}
                )
                assert snapshot.values["transient_attempt_count"] == 1
                persisted = runner._checkpointer.get_tuple(
                    {"configurable": {"thread_id": _run().thread_id}}
                )
                assert persisted is not None
                assert persisted.checkpoint["channel_values"]["transient_attempt_count"] == 1
            finally:
                release_checkpoint.set()
                release_second.set()
            events = future.result(timeout=5)

    assert events[-1].kind == "run_completed"
    assert gateway.requests == 3


def test_revalidate_transient_error_retries(tmp_path):
    class RevalidateFlakyGateway(RecordingGateway):
        def revalidate(self, run: PilotRun) -> None:
            if self.authority_checks == 1:
                self.authority_checks += 1
                self.tracing_enabled.append(get_tracing_context()["enabled"])
                raise TransientGatewayError("authority transport flake")
            super().revalidate(run)

        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            return super().call_read_tool(**kwargs)

    gateway = RevalidateFlakyGateway(approval="allowed")
    planner = RecordingPlanner()
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        result = runner.run(_run())

    assert result.status == "completed"
    assert gateway.authority_checks == 3
    assert gateway.requests == 1


def test_bounded_result_rejects_dict_with_fifty_one_keys() -> None:
    with pytest.raises(PilotError, match="exceeds the pilot checkpoint bound"):
        _bounded_result({f"key{i}": "v" for i in range(51)})


def test_stale_authority_on_resume_after_interrupt(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"

        gateway.approval = "stale"
        with pytest.raises(StaleAuthorityError, match="task revision changed"):
            runner.run(_run(), resume={"approved": True})

    assert planner.calls == 1
    assert gateway.executions == 0


def test_checkpoint_path_rejects_memory_db(tmp_path, monkeypatch):
    memory_db = tmp_path / "memory.db"
    memory_db.write_text("not really memory.db")
    monkeypatch.setattr(
        "unified_agent.langgraph_pilot.config.db_path",
        lambda: memory_db,
    )
    planner = RecordingPlanner()
    gateway = RecordingGateway(approval="allowed")
    with pytest.raises(PilotError, match="must not share memory.db"):
        PilotRunner(memory_db, planner=planner, gateway=gateway)


def test_checkpoint_path_rejects_hardlink_to_memory_db(tmp_path, monkeypatch):
    memory_db = tmp_path / "memory.db"
    memory_db.write_text("not really memory.db")
    hardlink = tmp_path / "checkpoints.sqlite3"
    os.link(memory_db, hardlink)
    monkeypatch.setattr(
        "unified_agent.langgraph_pilot.config.db_path",
        lambda: memory_db,
    )
    planner = RecordingPlanner()
    gateway = RecordingGateway(approval="allowed")
    with pytest.raises(PilotError, match="must not share memory.db"):
        PilotRunner(hardlink, planner=planner, gateway=gateway)


def test_checkpoint_does_not_chmod_existing_parent(tmp_path):
    parent = tmp_path / "existing"
    parent.mkdir()
    parent.chmod(0o755)
    checkpoint = parent / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=planner, gateway=gateway):
        pass
    assert parent.stat().st_mode & 0o777 == 0o755


def test_checkpoint_chmods_created_parent(tmp_path):
    checkpoint = tmp_path / "created" / "checkpoints.sqlite3"
    planner = RecordingPlanner()
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=planner, gateway=gateway):
        pass
    assert checkpoint.parent.stat().st_mode & 0o777 == 0o700


def test_corrupt_checkpoint_fails_closed_without_fallback(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite3"
    checkpoint.write_bytes(b"not-a-sqlite-file")
    planner = RecordingPlanner()
    gateway = RecordingGateway(approval="allowed")
    with pytest.raises(sqlite3.DatabaseError):
        with PilotRunner(checkpoint, planner=planner, gateway=gateway) as runner:
            runner.run(_run())


def test_secret_bearing_query_rejected_before_checkpoint(tmp_path):
    planner = RecordingPlanner()
    gateway = RecordingGateway(approval="allowed")
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        with pytest.raises(InvalidRunError, match="recognized secret"):
            runner.run(_run(query="Bearer abcdefghijklmnop needs redaction"))
    assert gateway.requests == 0


@pytest.mark.parametrize(
    ("tool_name", "arguments", "match"),
    [
        ("shell_exec", {"query": "x", "max_results": 1}, "disallowed tool"),
        ("web_search", {"query": "x"}, "exactly query and max_results"),
        (
            "web_search",
            {"query": "x", "max_results": 1, "extra": True},
            "exactly query and max_results",
        ),
        ("web_search", {"query": "x", "max_results": 0}, "between 1 and 3"),
        ("web_search", {"query": "x", "max_results": 4}, "between 1 and 3"),
        ("web_search", {"query": "x", "max_results": True}, "must be an integer"),
    ],
)
def test_tool_proposal_normalization_rejects_invalid_contract(
    tool_name: str, arguments: dict, match: str
) -> None:
    with pytest.raises(PilotError, match=match):
        ToolProposal(tool_name, arguments).normalized()


def test_bounded_result_rejects_oversized_governed_payload(tmp_path):
    class OversizedGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            return GovernedToolOutcome.allowed(
                {"blob": "x" * 5000},
                approval_id="approval-586",
            )

    planner = RecordingPlanner()
    gateway = OversizedGateway(approval="allowed")
    with PilotRunner(tmp_path / "checkpoints.sqlite3", planner=planner, gateway=gateway) as runner:
        with pytest.raises(PilotError, match="exceeds the pilot checkpoint bound"):
            runner.run(_run())


# --- validated checkpoint loading (task #2522) ------------------------------------------


def test_load_run_restores_paused_authority_and_resume_produces_output(tmp_path):
    from unified_agent.langgraph_pilot import LoadedRun

    checkpoint = tmp_path / "checkpoints.sqlite3"
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"

    # A new runner (a later request) knows only the run_id.
    gateway.approval = "allowed"
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=gateway) as runner:
        loaded = runner.load_run(RUN_ID)
        assert isinstance(loaded, LoadedRun)
        assert loaded.run == _run()  # claim, revision, profile, budget and trace kept
        assert loaded.paused is True and loaded.status == "running"
        assert loaded.revision
        events = list(runner.stream(loaded.run, resume={"resume": True}))
        assert events[-1].kind == "run_completed"
        assert runner.output(loaded.run)["evidence_digest"]
        assert runner.load_run(RUN_ID).revision != loaded.revision
        assert gateway.executions == 1


def test_load_run_refuses_missing_corrupt_and_incompatible_checkpoints(tmp_path):
    from unified_agent.langgraph_pilot import CheckpointCorruptError, CheckpointMissingError

    checkpoint = tmp_path / "checkpoints.sqlite3"
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=RecordingGateway()) as runner:
        with pytest.raises(CheckpointMissingError):
            runner.load_run(RUN_ID)
        with pytest.raises(InvalidRunError, match="ULID"):
            runner.load_run("../not-a-run")
        list(runner.stream(_run()))

    class OtherPlanner(RecordingPlanner):
        definition_digest = "b" * 64

    with PilotRunner(checkpoint, planner=OtherPlanner(), gateway=RecordingGateway()) as runner:
        with pytest.raises(DefinitionMismatchError):
            runner.load_run(RUN_ID)

    with sqlite3.connect(checkpoint) as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint=? WHERE thread_id=?", (b"\xc1junk", _run().thread_id)
        )
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=RecordingGateway()) as runner:
        with pytest.raises(CheckpointCorruptError):
            runner.load_run(RUN_ID)


def test_load_run_carries_the_transient_retry_budget(tmp_path):
    class FlakyGateway(RecordingGateway):
        def call_read_tool(self, **kwargs) -> GovernedToolOutcome:
            self.requests += 1
            if self.requests in (1, 2) or self.approval == "flaky":
                raise TransientGatewayError("retryable transport failure")
            return GovernedToolOutcome.pending("approval-586")

    checkpoint = tmp_path / "checkpoints.sqlite3"
    gateway = FlakyGateway()
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=gateway) as runner:
        assert list(runner.stream(_run()))[-1].kind == "approval_interrupted"
    gateway.approval = "flaky"
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=gateway) as runner:
        loaded = runner.load_run(RUN_ID)
        events = list(runner.stream(loaded.run, resume={"resume": True}))
        assert events[-1].kind == "run_failed"
        # Two attempts were spent before the pause; the resume gets only the rest.
        assert events[-1].payload["transient_attempt_count"] == 4
        assert gateway.requests == 3 + 2


def test_load_run_translates_malformed_state_into_corruption(tmp_path):
    from unified_agent.langgraph_pilot import CheckpointCorruptError

    checkpoint = tmp_path / "checkpoints.sqlite3"
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=RecordingGateway()) as runner:
        list(runner.stream(_run()))
        runner._graph.update_state({"configurable": {"thread_id": _run().thread_id}}, {"query": 42})
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=RecordingGateway()) as runner:
        with pytest.raises(CheckpointCorruptError):
            runner.load_run(RUN_ID)


def test_checkpoint_writes_are_fenced_on_the_resume_lease(tmp_path):
    from unified_agent.langgraph_pilot import FenceLostError

    checkpoint = tmp_path / "checkpoints.sqlite3"
    gateway = RecordingGateway()
    with PilotRunner(checkpoint, planner=RecordingPlanner(), gateway=gateway) as runner:
        list(runner.stream(_run()))
        before = runner.load_run(RUN_ID).revision
        with sqlite3.connect(checkpoint) as conn:
            conn.execute(
                "INSERT INTO langgraph_resume_leases (thread_id, org_id, run_id, holder, token, "
                "acquired_at, expires_at) VALUES (?, NULL, ?, 'other', 'token-b', "
                "'2026-01-01T00:00:00.000000+00:00', '2999-01-01T00:00:00.000000+00:00')",
                (_run().thread_id, RUN_ID),
            )
        gateway.approval = "allowed"
        # An unfenced writer, and a writer holding a superseded token, are refused
        # in the write's own transaction: the checkpoint does not advance.
        for token in (None, "token-a"):
            runner.fence(_run().thread_id, token)
            with pytest.raises(FenceLostError):
                list(runner.stream(_run(), resume={"resume": True}))
            assert runner.load_run(RUN_ID).revision == before
        runner.fence(_run().thread_id, "token-b")
        assert list(runner.stream(_run(), resume={"resume": True}))[-1].kind == "run_completed"
