from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent.horizon2 import (
    WIRED_PROVIDERS,
    A2AAdapter,
    A2ATask,
    AgentCard,
    ExecutionAction,
    ExecutionEnvelope,
    ExecutionStatus,
    GovernedServer,
    ServerGovernance,
    adapter_for,
    evaluate_trajectory,
    package_connector,
)
from unified_agent.telemetry.schema import SpanKind, SpanOutcome, TelemetrySpan


def envelope(**changes):
    base = {
        "action": ExecutionAction.START,
        "execution_id": "run-1",
        "task_id": 42,
        "provider": "codex",
        "authority_revision": "task-revision-1",
        "trace_id": "trace-1",
    }
    return ExecutionEnvelope(**(base | changes))


def server(**changes):
    base = {
        "server_id": "slack-prod",
        "connector": "slack",
        "provenance": "sha256:abc",
        "scopes": frozenset({"read", "write"}),
        "reviewed_by": "Operator",
        "review_ref": "approval:42",
    }
    return GovernedServer(**(base | changes))


def test_execution_envelope_is_safe_and_provider_neutral():
    item = envelope(payload={"token": "secret-value"})

    assert item.to_dict()["action"] == "start"
    assert item.to_dict()["payload"]["token"] == "[REDACTED_SECRET]"


def test_execution_envelope_rejects_terminal_cancel_and_bad_provider():
    with pytest.raises(ValueError, match="terminal"):
        envelope(action=ExecutionAction.CANCEL, status=ExecutionStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="provider"):
        envelope(provider="not valid")


def test_a2a_exchange_preserves_observation_without_authority_mutation():
    adapter = A2AAdapter()
    exported = adapter.export(
        envelope(action=ExecutionAction.STATUS, status=ExecutionStatus.RUNNING)
    )
    imported = adapter.import_status(
        exported,
        task_id=42,
        authority_revision="task-revision-2",
        trace_id="trace-2",
    )

    assert exported.context_id == "ua-task-42"
    assert imported.action is ExecutionAction.STATUS
    assert imported.authority_revision == "task-revision-2"


def test_a2a_exchange_rejects_a_context_for_another_task():
    with pytest.raises(ValueError, match="does not belong"):
        A2AAdapter().import_status(
            A2ATask(id="run-1", context_id="ua-task-999", status="running"),
            task_id=42,
            authority_revision="task-revision-2",
            trace_id="trace-2",
        )


def test_a2a_discover_reads_well_known_card_without_creating_a_task():
    captured: list[str] = []

    def fetch(url: str):
        captured.append(url)
        return {
            "name": "Peer Agent",
            "supportedInterfaces": [
                {
                    "url": "https://peer.example.com/a2a/v1",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "skills": [{"id": "summarize", "name": "Summarize"}],
            "description": "token=secret-value",
        }

    card = A2AAdapter().discover("https://peer.example.com", fetch=fetch)
    assert captured == ["https://peer.example.com/.well-known/agent-card.json"]
    assert card.url == "https://peer.example.com/a2a/v1"
    assert card.skills == ("summarize",)
    assert card.to_dict()["description"] == "[REDACTED_SECRET]"


def test_a2a_discover_accepts_legacy_url_field_and_rejects_missing_rpc():
    card = A2AAdapter().discover(
        "https://legacy.example.com/.well-known/agent.json",
        fetch=lambda _url: {"name": "Legacy", "url": "https://legacy.example.com/rpc"},
    )
    assert card.protocol_version == "1.0"
    with pytest.raises(ValueError, match="missing an RPC url"):
        AgentCard.from_payload({"name": "No URL"})


def test_a2a_live_exchange_imports_remote_task_with_hub_authority():
    card = AgentCard(name="Peer", url="https://peer.example.com/a2a/v1")
    seen: list[dict] = []

    def rpc(body):
        seen.append(body)
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "task": {
                    "id": "43667960-d455-4453-b0cf-1bae4955270d",
                    "contextId": "ua-task-42",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
        }

    observed = A2AAdapter().exchange(
        card,
        envelope(
            action=ExecutionAction.STATUS,
            status=ExecutionStatus.PENDING,
            authority_revision="hub-rev-9",
        ),
        rpc=rpc,
    )
    assert seen[0]["method"] == "GetTask"
    assert observed.status is ExecutionStatus.RUNNING
    assert observed.authority_revision == "hub-rev-9"
    assert observed.payload["authority_from"] == "hub"
    assert observed.task_id == 42
    assert observed.provider == "a2a"


def test_a2a_send_binds_ua_task_context_and_rejects_foreign_context():
    card = AgentCard(name="Peer", url="https://peer.example.com/a2a/v1")

    def send_ok(body):
        assert body["method"] == "SendMessage"
        assert body["params"]["message"]["contextId"] == "ua-task-42"
        return {
            "result": {
                "id": "run-9",
                "contextId": "ua-task-42",
                "status": "completed",
            }
        }

    sent = A2AAdapter().exchange(
        card,
        envelope(action=ExecutionAction.START, payload={"text": "summarize"}),
        rpc=send_ok,
    )
    assert sent.status is ExecutionStatus.SUCCEEDED

    def send_foreign(_body):
        return {
            "result": {
                "id": "run-9",
                "contextId": "ua-task-999",
                "status": "working",
            }
        }

    with pytest.raises(ValueError, match="does not belong"):
        A2AAdapter().exchange(card, envelope(action=ExecutionAction.START), rpc=send_foreign)


def test_governance_requires_active_scope():
    registry = ServerGovernance([server()])

    with pytest.raises(PermissionError, match="not approved"):
        registry.authorize("slack-prod", "missing")
    allowed = registry.authorize("slack-prod", "read")

    assert allowed.connector == "slack"


def test_revocation_is_immediate():
    registry = ServerGovernance([server()])
    registry.revoke("slack-prod")

    with pytest.raises(PermissionError, match="revoked"):
        registry.authorize("slack-prod", "read")


def test_governance_rejects_non_sha256_provenance():
    with pytest.raises(ValueError, match="sha256"):
        server(provenance="not-a-digest")


def test_governance_rejects_expired_review(monkeypatch):
    monkeypatch.setenv("UA_H2_REVIEW_TTL_DAYS", "30")
    stamped = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    registry = ServerGovernance([server(created_at=stamped)])
    with pytest.raises(PermissionError, match="expired"):
        registry.authorize("slack-prod", "read")
    fresh = ServerGovernance([server(created_at=datetime.now(UTC).isoformat())])
    assert fresh.authorize("slack-prod", "read").active


def test_governance_binds_connector_to_live_mcp_inventory(monkeypatch):
    monkeypatch.setenv("UA_H2_MCP_CONNECTIONS", "gbrain:obsidian")
    registry = ServerGovernance([server()])
    with pytest.raises(PermissionError, match="not bound"):
        registry.authorize("slack-prod", "read")
    monkeypatch.setenv("UA_H2_MCP_CONNECTIONS", "slack:gbrain")
    assert registry.authorize("slack-prod", "read", connection_ref="slack").connector == "slack"
    with pytest.raises(PermissionError, match="does not match"):
        registry.authorize("slack-prod", "read", connection_ref="gbrain")


def test_package_connector_uses_default_scopes_and_refuses_unknown():
    packed = package_connector(
        "obsidian",
        provenance="sha256:abc",
        review_ref="review:1",
        reviewed_by="Desktop_Agent4",
    )
    assert packed.connector == "obsidian"
    assert packed.server_id == "obsidian-default"
    assert packed.scopes == frozenset({"read", "write"})
    subset = package_connector(
        "slack",
        provenance="sha256:abc",
        review_ref="review:1",
        reviewed_by="Desktop_Agent4",
        server_id="slack-pack",
        scopes=["read"],
    )
    assert subset.scopes == frozenset({"read"})
    with pytest.raises(ValueError, match="unknown connector package"):
        package_connector(
            "gmail",
            provenance="sha256:abc",
            review_ref="review:1",
            reviewed_by="Desktop_Agent4",
        )
    with pytest.raises(ValueError, match="not in package"):
        package_connector(
            "drive",
            provenance="sha256:abc",
            review_ref="review:1",
            reviewed_by="Desktop_Agent4",
            scopes=["write"],
        )


def test_trajectory_evaluation_rescores_recorded_spans_only():
    spans = [
        TelemetrySpan("trace-1", "one", SpanKind.RUN, "run", SpanOutcome.SUCCESS),
        TelemetrySpan("trace-1", "two", SpanKind.TOOL, "tool", SpanOutcome.FAILURE),
    ]

    result = evaluate_trajectory(spans)

    assert result.trace_id == "trace-1"
    assert result.successful == result.failed == 1
    assert result.score == 0.5


def test_trajectory_requires_one_trace():
    with pytest.raises(ValueError, match="one non-empty"):
        evaluate_trajectory(
            [
                TelemetrySpan("first", "one", SpanKind.RUN, "run"),
                TelemetrySpan("second", "two", SpanKind.RUN, "run"),
            ]
        )


def _add_task() -> int:
    from unified_agent.tools import tasks as tasks_tool

    return tasks_tool.task_add("h2-adapter")["id"]


def _insert_dispatch_run(task_id: int, *, agent_label: str, state: str) -> None:
    from unified_agent import storage

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'test', ?, ?)",
            ("run-obs-1", task_id, agent_label, state, storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def test_adapter_for_wires_codex_claude_agy_langgraph():
    assert WIRED_PROVIDERS == frozenset({"codex", "claude", "agy", "langgraph"})
    for name in WIRED_PROVIDERS:
        assert adapter_for(name).provider == name
    with pytest.raises(ValueError, match="no execution adapter"):
        adapter_for("cloud")


def test_dispatch_adapter_projects_live_run_without_spawning():
    from unified_agent import storage

    task_id = _add_task()
    _insert_dispatch_run(task_id, agent_label="Desktop_Agent2", state="running")
    adapter = adapter_for("codex")
    observed = adapter.start(
        envelope(task_id=task_id, provider="codex", authority_revision="rev-1")
    )

    assert observed.status is ExecutionStatus.RUNNING
    assert observed.payload["spawned"] is False
    assert observed.payload["agent_label"] == "Desktop_Agent2"
    assert observed.action is ExecutionAction.STATUS
    conn = storage.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM dispatch_runs").fetchone()[0]
    finally:
        conn.close()
    adapter.cancel(
        envelope(
            action=ExecutionAction.CANCEL,
            task_id=task_id,
            provider="codex",
            status=ExecutionStatus.RUNNING,
        )
    )
    conn = storage.connect()
    try:
        after = conn.execute("SELECT COUNT(*) FROM dispatch_runs").fetchone()[0]
    finally:
        conn.close()
    assert before == after == 1


def test_dispatch_adapter_ignores_other_provider_runs():
    task_id = _add_task()
    _insert_dispatch_run(task_id, agent_label="Desktop_agent3", state="running")
    observed = adapter_for("claude").status(
        envelope(
            action=ExecutionAction.STATUS,
            task_id=task_id,
            provider="claude",
            status=ExecutionStatus.PENDING,
        )
    )
    assert observed.status is ExecutionStatus.PENDING
    assert observed.payload["matched"] is False


def test_langgraph_adapter_projects_checkpoints_without_running_the_pilot():
    from unified_agent import config

    adapter = adapter_for("langgraph")
    pending = adapter.start(envelope(provider="langgraph"))
    assert pending.status is ExecutionStatus.PENDING
    assert pending.payload["spawned"] is False

    ckpt = config.data_home() / "langgraph" / "checkpoints.sqlite3"
    ckpt.parent.mkdir(parents=True)
    import sqlite3

    conn = sqlite3.connect(ckpt)
    conn.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, "
        "checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT)"
    )
    conn.execute("INSERT INTO checkpoints VALUES ('ua-lg:01hxyz', '', 'ckpt-1', NULL, NULL)")
    conn.commit()
    conn.close()

    running = adapter.status(
        envelope(
            action=ExecutionAction.STATUS,
            provider="langgraph",
            status=ExecutionStatus.PENDING,
        )
    )
    assert running.status is ExecutionStatus.RUNNING
    assert running.payload["checkpoint_count"] == 1
    events = list(
        adapter.events(
            envelope(
                action=ExecutionAction.EVENTS,
                provider="langgraph",
                status=ExecutionStatus.PENDING,
            )
        )
    )
    assert len(events) == 1
    assert events[0].action is ExecutionAction.EVENTS


def test_observation_adapter_rejects_provider_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        adapter_for("agy").status(
            envelope(
                action=ExecutionAction.STATUS,
                provider="codex",
                status=ExecutionStatus.PENDING,
            )
        )
