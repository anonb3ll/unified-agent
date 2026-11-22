from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from unified_agent import agent_access, events, storage
from unified_agent import config as ua_config
from unified_agent.slack_gateway import (
    FrontierPolicy,
    SlackConfig,
    SlackGatewayError,
    SlackGatewayOverloadError,
    SlackStatusLookupConfig,
    _slack_task_status,
    _task_spec,
    authorize,
    dispatch,
    frontier_policy,
    handle_http_request,
    parse_event_request,
    parse_request,
    parse_slash_payload,
    record_frontier_usage,
    verify_signature,
)
from unified_agent.tools import tasks
from unified_agent.tools.delegation_spec import enforce_delegation_spec

_STATUS_LOOKUP_OFF = SlackStatusLookupConfig(enabled=False, identity_map={}, error=None)


def _config(**overrides) -> SlackConfig:
    values = dict(
        signing_secret="signing",
        bot_token=None,
        app_token=None,
        channel_ids=frozenset({"C_REQUESTS"}),
        channel_names_by_id={"C_REQUESTS": "requests"},
        users=frozenset({"U_OPERATOR"}),
        coordinator_agent="Desktop_Agent5",
        enabled=True,
        frontier=FrontierPolicy(
            models=frozenset({"frontier-test"}),
            provider="openai",
            credential_env="OPENAI_API_KEY",
            max_budget_usd=2.0,
            timeout_s=30,
            max_prompt_chars=1000,
            max_output_tokens=100,
            kill_switch=False,
            credential_present=True,
        ),
        status_lookup=_STATUS_LOOKUP_OFF,
    )
    values.update(overrides)
    return SlackConfig(**values)


def _signed(
    body: bytes, *, timestamp: str = "1700000000", secret: str = "signing"
) -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
    }


def test_signature_rejects_replay_and_accepts_valid_body():
    body = b'{"type":"event_callback"}'
    headers = _signed(body)
    assert verify_signature(
        "signing",
        headers["X-Slack-Request-Timestamp"],
        body,
        headers["X-Slack-Signature"],
        now=1700000000,
    )
    assert not verify_signature(
        "signing",
        headers["X-Slack-Request-Timestamp"],
        body,
        headers["X-Slack-Signature"],
        now=1700000401,
    )


def test_authorized_mention_and_slash_payload_are_typed():
    assert parse_request("<@UBOT> /blocker task-12 waiting on CI") == {
        "command": "blocker",
        "args": ["task-12", "waiting on CI"],
    }
    assert parse_slash_payload({"command": "/task", "text": "investigate"}) == {
        "command": "task",
        "args": ["investigate"],
    }
    with pytest.raises(SlackGatewayError):
        parse_request("/shell rm -rf /")


def test_assign_spec_satisfies_default_delegation_gate():
    spec = _task_spec("review the Slack path", "Ev-spec")
    assert enforce_delegation_spec(spec, "Desktop_Agent2")["ok"] is True
    assert all(
        heading in spec.lower()
        for heading in (
            "### objective",
            "### scope",
            "### definition of done",
            "### tdd constraints",
            "### boundaries",
        )
    )
    assert "### proof" in spec.lower()


def test_id_only_channel_allowlist_does_not_validate_display_name():
    config = _config(
        channel_ids=frozenset({"C_REQUESTS"}),
        channel_names_by_id={"C_COORD": "coord"},
    )
    assert (
        authorize(config, user_id="U_OPERATOR", channel_id="C_REQUESTS", channel_name="random")
        == "unknown"
    )


def test_http_adapter_enforces_exact_user_and_channel_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task investigate queue",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert tasks.task_list(status="all")["tasks"][0]["title"] == "investigate queue"
    duplicate = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert duplicate["duplicate"] is True
    assert len(events.query_events(channels=["slack.inbound"])) == 1

    rejected = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-2",
            "event": {"user": "U_OTHER", "channel": "C_REQUESTS", "text": "/task nope"},
        }
    ).encode()
    with pytest.raises(SlackGatewayError, match="user"):
        handle_http_request(rejected, headers=_signed(rejected), config=_config(), now=1700000000)


def test_http_adapter_handles_signed_slash_command(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = b"command=%2Ftask&text=slash+request&user_id=U_OPERATOR&channel_id=C_REQUESTS"
    headers = _signed(body)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    result = handle_http_request(body, headers=headers, config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert tasks.task_list(status="all")["tasks"][0]["title"] == "slash request"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UA_AGENT_ACCESS_ENABLED", "0"),
        ("UA_AGENT_ACCESS_READ_ONLY", "1"),
        ("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", "1"),
        ("UA_MANAGED_SECRETS", "1"),
    ],
)
def test_signed_slack_mutations_obey_agent_access_controls(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv(name, value)
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": f"Ev-agent-access-{name}",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task must not mutate",
            },
        }
    ).encode()

    with pytest.raises(agent_access.AgentAccessDenied):
        handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert tasks.task_list(status="all")["tasks"] == []


def test_dispatch_uses_existing_primitives_and_human_approval_handoff():
    calls = []
    callbacks = {
        "task_add": lambda **kw: calls.append(("task_add", kw)) or {"id": 12},
        "task_update": lambda **kw: calls.append(("task_update", kw)) or {"ok": True},
        "delegate": lambda *args, **kw: calls.append(("delegate", args, kw)) or {"id": 13},
        "plan_create": lambda **kw: calls.append(("plan_create", kw)) or {"plan_id": "p1"},
        "approval_pending": lambda: {"pending": [{"id": 7}]},
    }
    assert (
        dispatch(parse_request("/task investigate"), user_id="U1", callbacks=callbacks)["id"] == 12
    )
    assert (
        dispatch(parse_request("/assign Desktop_Agent2 review"), user_id="U1", callbacks=callbacks)[
            "id"
        ]
        == 13
    )
    assert (
        dispatch(parse_request("/plan compare options"), user_id="U1", callbacks=callbacks)[
            "plan_id"
        ]
        == "p1"
    )
    handoff = dispatch(parse_request("/approve 7"), user_id="U1", callbacks=callbacks)
    assert handoff["requires_human_resolution"] is True
    assert len(calls) == 3


def test_failed_dispatch_is_durable_and_does_not_leak_error_or_retry_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-fail",
            "event": {"user": "U_OPERATOR", "channel": "C_REQUESTS", "text": "/task fail"},
        }
    ).encode()

    def fail(**_kwargs):
        raise RuntimeError("provider prompt must never be logged")

    result = handle_http_request(
        body, headers=_signed(body), config=_config(), callbacks={"task_add": fail}, now=1700000000
    )
    assert result["requires_review"] is True
    failure = events.query_events(channels=["slack.dispatch.failed"])[0]
    assert failure["payload"] == {"event_id": "Ev-fail", "error_type": "RuntimeError"}


def test_frontier_policy_fails_closed_for_unknown_model_missing_credential_and_kill_switch():
    denied = frontier_policy("unknown", config=_config())
    assert denied["allowed"] is False
    assert (
        frontier_policy(
            "frontier-test",
            config=_config(
                frontier=_config().frontier.__class__(
                    **{**_config().frontier.__dict__, "credential_present": False}
                )
            ),
        )["allowed"]
        is False
    )
    killed = _config(
        frontier=_config().frontier.__class__(
            **{**_config().frontier.__dict__, "kill_switch": True}
        )
    )
    assert frontier_policy("frontier-test", config=killed)["allowed"] is False


def test_frontier_usage_telemetry_is_redacted_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    first = record_frontier_usage(
        model="frontier-test",
        provider="openai",
        agent="Desktop_Agent5",
        project="unified-agent",
        duration_s=1.25,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        outcome="success",
        request_id="r1",
    )
    second = record_frontier_usage(
        model="frontier-test",
        provider="openai",
        agent="Desktop_Agent5",
        project="unified-agent",
        duration_s=1.25,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        outcome="success",
        request_id="r1",
    )
    assert first == second
    payload = events.query_events(channels=["frontier.usage"])[0]["payload"]
    assert "prompt" not in payload and "response" not in payload
    assert payload["cost_usd"] == 0.01


def test_frontier_usage_rejects_negative_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    with pytest.raises(SlackGatewayError, match="duration"):
        record_frontier_usage(
            model="frontier-test",
            agent="Desktop_Agent5",
            project="unified-agent",
            duration_s=-0.01,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
            outcome="success",
        )


def test_parse_event_request_preserves_native_mentions_for_chat():
    parsed = parse_event_request("<@U123|Desktop_Agent4> status task-42", event_type="message")
    assert parsed["command"] == "chat"
    assert parsed["text"] == "<@U123|Desktop_Agent4> status task-42"


def test_chat_overload_records_failure_and_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_CHAT", "1")
    monkeypatch.setenv("UA_SLACK_OUTBOUND_MAX_QUEUE", "1")
    monkeypatch.setenv("UA_SLACK_OUTBOUND_WORKERS", "1")
    storage.init_db()
    from unified_agent import slack_chat
    from unified_agent.slack_outbound import shutdown_background_workers

    shutdown_background_workers(wait=True)
    blocker = __import__("threading").Event()
    started = __import__("threading").Event()

    def _blocking_maybe_reply(**_kwargs):
        started.set()
        blocker.wait(timeout=2.0)
        return {"replied": False, "reason": "test-block"}

    monkeypatch.setattr(slack_chat, "maybe_reply", _blocking_maybe_reply)
    assert (
        slack_chat.schedule_maybe_reply(
            text="prefill-1",
            event_id="Ev-prefill-1",
            channel_id="C_REQUESTS",
            protocol_channel="requests",
            thread_ts=None,
            event_ts="1700000000.0",
            user_id="U_OPERATOR",
        )
        is True
    )
    assert started.wait(timeout=2.0)
    assert (
        slack_chat.schedule_maybe_reply(
            text="prefill-2",
            event_id="Ev-prefill-2",
            channel_id="C_REQUESTS",
            protocol_channel="requests",
            thread_ts=None,
            event_ts="1700000000.2",
            user_id="U_OPERATOR",
        )
        is True
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-overload",
            "event": {
                "type": "message",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "[target: Desktop_Agent4] hello",
                "ts": "1700000000.1",
            },
        }
    ).encode()
    try:
        with pytest.raises(SlackGatewayOverloadError, match="overloaded"):
            handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
        conn = storage.connect()
        row = conn.execute(
            "SELECT status, error FROM slack_chat_replies WHERE inbound_event_id=?",
            ("Ev-overload",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == storage.SLACK_CHAT_QUEUE_OVERLOAD_ERROR
    finally:
        blocker.set()
        shutdown_background_workers(wait=True)


def test_duplicate_chat_event_reschedules_when_reply_not_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_CHAT", "1")
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    storage.init_db()
    scheduled: list[bool] = []

    def fake_schedule(**kwargs):
        scheduled.append(True)
        return True

    monkeypatch.setattr("unified_agent.slack_chat.schedule_maybe_reply", fake_schedule)
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-chat-dup",
            "event": {
                "type": "message",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "[target: Desktop_Agent4] follow up",
                "ts": "1710000000.000100",
            },
        }
    ).encode()
    first = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert first["kind"] == "chat"
    assert first["slack_chat"]["scheduled"] is True
    duplicate = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert duplicate["duplicate"] is True
    assert duplicate["slack_chat"] == {"scheduled": True, "rescheduled": True}
    assert len(scheduled) == 2


def test_slack_task_routes_through_registered_tools_wrapper(tmp_path, monkeypatch):
    """A Slack /task command routes through server.REGISTERED_TOOLS wrapped callable, not raw primitive."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    from unified_agent import server

    wrapped_fn = server.REGISTERED_TOOLS["task_add"]
    wrapped_invocations: list[dict] = []

    def spy_wrapped(**kwargs):
        wrapped_invocations.append(kwargs)
        return wrapped_fn(**kwargs)

    monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", spy_wrapped)

    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-tdd-wrap-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task assert wrapped path",
            },
        }
    ).encode()

    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert len(wrapped_invocations) == 1
    assert wrapped_invocations[0]["title"] == "assert wrapped path"
    # Ensure durable task was actually created through the real wrapped callable
    task_rows = tasks.task_list(status="all")["tasks"]
    assert len(task_rows) == 1
    assert task_rows[0]["title"] == "assert wrapped path"


def test_slack_policy_refusal_propagates_as_visible_error(tmp_path, monkeypatch):
    """A profile or policy denial produces a visible Slack error, durable failure event, and creates no task."""
    import time

    from unified_agent.slack_outbound import shutdown_background_workers

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    storage.init_db()

    shutdown_background_workers(wait=True)
    reply_calls: list[dict] = []

    def fake_post(url, data, headers, **_kwargs):
        reply_calls.append(data)
        return {"ok": True, "ts": "1700000000.9"}

    monkeypatch.setattr("unified_agent.slack_outbound._http_post", fake_post)

    # Deny task_add in the active profile check
    monkeypatch.setattr(
        "unified_agent.profiles.tool_is_allowed",
        lambda name: False if name == "task_add" else True,
    )

    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-tdd-refusal-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task prohibited task",
            },
        }
    ).encode()

    try:
        cfg = _config(bot_token="xoxb-test")
        result = handle_http_request(body, headers=_signed(body), config=cfg, now=1700000000)
        # Refusal must not be silent success
        assert result["dispatched"] is False
        assert result["requires_review"] is True
        assert result["slack_reply"] == {"scheduled": True}

        # Check that durable dispatch failure event was recorded with ToolError
        failures = events.query_events(channels=["slack.dispatch.failed"])
        assert len(failures) == 1
        assert failures[0]["payload"]["event_id"] == "Ev-tdd-refusal-1"
        assert failures[0]["payload"]["error_type"] == "ToolError"

        # Check that no task was created
        assert len(tasks.task_list(status="all")["tasks"]) == 0

        # Wait for the background worker to deliver the outbound ack
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not reply_calls:
            time.sleep(0.01)
        assert len(reply_calls) == 1
        # The refusal must surface visibly; the routed path attributes the
        # reply to the acting agent label, so assert on the refusal text.
        assert "accepted; queued for human review" in reply_calls[0]["text"]
    finally:
        shutdown_background_workers(wait=True)


def test_slack_assign_and_plan_verbs_route_through_registered_tools(tmp_path, monkeypatch):
    """All mutating Slack verbs (assign, plan) traverse REGISTERED_TOOLS wrapped callables."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    from unified_agent import server

    called_tools: list[str] = []

    def make_spy(tool_name: str):
        real = server.REGISTERED_TOOLS[tool_name]

        def spy(*args, **kwargs):
            called_tools.append(tool_name)
            return real(*args, **kwargs)

        return spy

    monkeypatch.setitem(server.REGISTERED_TOOLS, "delegate", make_spy("delegate"))
    monkeypatch.setitem(server.REGISTERED_TOOLS, "plan_create", make_spy("plan_create"))

    # Test /assign
    assign_body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-tdd-assign-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/assign Desktop_Agent2 perform audit",
            },
        }
    ).encode()
    assign_res = handle_http_request(
        assign_body, headers=_signed(assign_body), config=_config(), now=1700000000
    )
    assert assign_res["dispatched"] is True
    assert "delegate" in called_tools

    # Test /plan
    plan_body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-tdd-plan-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/plan rollout architecture",
            },
        }
    ).encode()
    plan_res = handle_http_request(
        plan_body, headers=_signed(plan_body), config=_config(), now=1700000000
    )
    assert plan_res["dispatched"] is True
    assert "plan_create" in called_tools


def _status_config(**overrides) -> SlackConfig:
    values = dict(
        status_lookup=SlackStatusLookupConfig(
            enabled=True,
            identity_map={"U_OPERATOR": "Desktop_Person"},
            error=None,
        )
    )
    values.update(overrides)
    return _config(**values)


def test_status_lookup_disabled_by_default_denies():
    """No env vars set -> SlackStatusLookupConfig.from_env() is off (task #1104)."""
    assert ua_config is not None  # imported for other tests in this module
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.enabled is False
    with pytest.raises(SlackGatewayError, match="disabled"):
        cfg.resolve_identity("U_OPERATOR")


def test_status_identity_map_duplicate_entry_fails_closed(monkeypatch):
    monkeypatch.setenv("UA_SLACK_STATUS_LOOKUP_ENABLED", "1")
    monkeypatch.setenv(
        "UA_SLACK_STATUS_IDENTITY_MAP", "U_OPERATOR=Desktop_Person,U_OPERATOR=Desktop_Agent5"
    )
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.enabled is False
    assert cfg.error is not None and "duplicate" in cfg.error
    with pytest.raises(SlackGatewayError, match="invalid"):
        cfg.resolve_identity("U_OPERATOR")


def test_status_identity_map_unknown_identity_fails_closed(monkeypatch):
    monkeypatch.setenv("UA_SLACK_STATUS_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_STATUS_IDENTITY_MAP", "U_OPERATOR=Not_A_Real_Identity")
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.enabled is False
    assert cfg.error is not None and "known registered" in cfg.error


def test_status_lookup_unmapped_user_denied():
    cfg = _status_config()
    with pytest.raises(SlackGatewayError, match="not mapped"):
        cfg.status_lookup.resolve_identity("U_STRANGER")


def test_status_lookup_revoked_identity_denied(monkeypatch):
    monkeypatch.setenv("UA_REVOKED_AGENTS", "Desktop_Person")
    cfg = _status_config()
    with pytest.raises(SlackGatewayError, match="revoked"):
        cfg.status_lookup.resolve_identity("U_OPERATOR")


def test_status_lookup_retired_identity_denied_with_evidence():
    """Task #2305: a retired mapped identity gets the same denial, recorded."""
    from unified_agent import agent_lifecycle, events, storage

    cfg = _status_config()
    events.record_event(
        agent_lifecycle.CHANNEL,
        payload={
            "kind": agent_lifecycle.KIND_CHANGED,
            "agent": cfg.status_lookup.identity_map["U_OPERATOR"],
            "state": "retired",
            "reason": "test",
            "actor": "test",
        },
    )
    result = _slack_task_status(["#1"], user_id="U_OPERATOR", config=cfg)
    assert result == {"kind": "task_status", "reason": "denied"}
    conn = storage.connect()
    try:
        [row] = conn.execute(
            "SELECT json_extract(payload, '$.site') AS site FROM event_log "
            "WHERE json_extract(payload, '$.kind')=?",
            (agent_lifecycle.KIND_REFUSED,),
        ).fetchall()
    finally:
        conn.close()
    assert row["site"] == "slack.status_lookup"


def test_status_lookup_malformed_id_returns_usage():
    result = _slack_task_status(["not-a-task-id"], user_id="U_OPERATOR", config=_status_config())
    assert result == {"kind": "task_status", "reason": "usage"}
    empty = _slack_task_status([], user_id="U_OPERATOR", config=_status_config())
    assert empty["reason"] == "usage"


def test_status_lookup_missing_task_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    result = _slack_task_status(["task-999999"], user_id="U_OPERATOR", config=_status_config())
    assert result == {"kind": "task_status", "reason": "not_found", "task_id": "999999"}


def test_status_lookup_projects_only_safe_fields_and_never_leaks_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    created = tasks.task_add(
        title="rotate the leaked credential",
        notes="SECRET_TOKEN_DO_NOT_LEAK=abc123",
        project="unified-agent",
    )
    result = _slack_task_status(
        [f"task-{created['id']}"], user_id="U_OPERATOR", config=_status_config()
    )
    assert result["reason"] == "ok"
    task = result["task"]
    assert set(task) == {"id", "title", "status", "project", "owner"}
    assert task["id"] == created["id"]
    assert task["title"] == "rotate the leaked credential"
    assert task["status"] == "open"
    assert task["project"] == "unified-agent"
    assert task["owner"] is None
    serialized = json.dumps(result)
    assert "SECRET_TOKEN_DO_NOT_LEAK" not in serialized
    assert "notes" not in serialized
    assert "evidence_ref" not in serialized


def test_status_lookup_scopes_identity_and_restores_context(tmp_path, monkeypatch):
    """Read runs with agent_identity_var bound to the mapped identity, then restored (task #1104)."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    created = tasks.task_add(title="scoped read check", project="unified-agent")
    assert ua_config.agent_identity_var.get() is None

    seen_identity = {}
    from unified_agent import platform_http

    real_read_object = platform_http.read_object

    def spy_read_object(kind, object_id=None):
        seen_identity["identity"] = ua_config.verified_agent_identity()
        return real_read_object(kind, object_id)

    monkeypatch.setattr(platform_http, "read_object", spy_read_object)
    monkeypatch.setattr("unified_agent.slack_gateway.platform_http", platform_http)

    result = _slack_task_status(
        [f"task-{created['id']}"], user_id="U_OPERATOR", config=_status_config()
    )
    assert result["reason"] == "ok"
    assert seen_identity["identity"] == "Desktop_Person"
    # Restored after the call, so nothing else in-process inherits the identity.
    assert ua_config.agent_identity_var.get() is None


def test_status_command_requires_feature_enabled_end_to_end(tmp_path, monkeypatch):
    """Feature disabled by default: a signed, allowlisted 'status' request is quietly denied.

    Quiet means: dispatched (not a dispatch failure), no requires_review, no
    slack.dispatch.failed event -- only an informational slack.status.denied
    event (task #1104 follow-up, Devin review).
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    created = tasks.task_add(title="off by default", project="unified-agent")
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-off",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": f"/status task-{created['id']}",
            },
        }
    ).encode()
    # _config()'s default status_lookup is disabled.
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert "requires_review" not in result
    assert result["result"] == {"kind": "task_status", "reason": "denied"}
    assert events.query_events(channels=["slack.dispatch.failed"]) == []
    denied = events.query_events(channels=["slack.status.denied"])
    assert len(denied) == 1
    assert denied[0]["payload"] == {"slack_user_id": "U_OPERATOR", "config_invalid": False}


def test_status_command_signed_ingress_success(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    created = tasks.task_add(title="signed status lookup", project="unified-agent")
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-ok",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": f"/status task-{created['id']}",
            },
        }
    ).encode()
    result = handle_http_request(
        body, headers=_signed(body), config=_status_config(), now=1700000000
    )
    assert result["dispatched"] is True
    assert result["result"]["kind"] == "task_status"
    assert result["result"]["reason"] == "ok"
    assert result["result"]["task"]["id"] == created["id"]


def test_status_command_bad_signature_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-badsig",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/status task-1",
            },
        }
    ).encode()
    headers = _signed(body, secret="wrong-secret")
    with pytest.raises(SlackGatewayError, match="signature"):
        handle_http_request(body, headers=headers, config=_status_config(), now=1700000000)


def test_status_command_disallowed_channel_denied_before_any_read(tmp_path, monkeypatch):
    """User/channel allowlists run before the identity map or any read (task #1104)."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-badchannel",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_OTHER",
                "text": "/status task-1",
            },
        }
    ).encode()
    with pytest.raises(SlackGatewayError, match="channel"):
        handle_http_request(body, headers=_signed(body), config=_status_config(), now=1700000000)


def test_status_command_unmapped_user_denied_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    cfg = _status_config(users=frozenset({"U_OPERATOR", "U_OTHER"}))
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-unmapped",
            "event": {
                "type": "app_mention",
                "user": "U_OTHER",
                "channel": "C_REQUESTS",
                "text": "/status task-1",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=cfg, now=1700000000)
    assert result["dispatched"] is True
    assert "requires_review" not in result
    assert result["result"] == {"kind": "task_status", "reason": "denied"}
    assert events.query_events(channels=["slack.dispatch.failed"]) == []


def test_status_parse_requires_a_task_id_argument():
    with pytest.raises(SlackGatewayError, match="requires a task id"):
        parse_request("/status")


def test_slack_config_status_lookup_defaults_to_disabled_when_omitted():
    """SlackConfig(...) call sites written before task #1104 must keep working (CI fix)."""
    cfg = SlackConfig(
        signing_secret="signing",
        bot_token=None,
        app_token=None,
        channel_ids=frozenset({"C_REQUESTS"}),
        channel_names_by_id={"C_REQUESTS": "requests"},
        users=frozenset({"U_OPERATOR"}),
        coordinator_agent="Desktop_Agent5",
        enabled=True,
        frontier=FrontierPolicy.from_env(),
    )
    assert cfg.status_lookup.enabled is False
    assert cfg.status_lookup.identity_map == {}
    assert cfg.status_lookup.error is None


def test_status_identity_map_accepts_hub_registered_developer_identity(monkeypatch):
    """A hub-registered developer identity (not Desktop_Person) is a valid map target.

    Registration is UA_AGENT_TOKENS -- the same registry HubAuthASGIMiddleware
    authenticates every other agent connection against -- not
    profiles.DEFAULT_PROFILES_BY_IDENTITY, which would wrongly reject
    ordinary developer identities like Desktop_Agent2 (task #1104 follow-up,
    Devin review).
    """
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:tok-codex-1")
    monkeypatch.setenv("UA_SLACK_STATUS_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_STATUS_IDENTITY_MAP", "U_CODEX=Desktop_Agent2")
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.error is None
    assert cfg.enabled is True
    assert cfg.resolve_identity("U_CODEX") == "Desktop_Agent2"


def test_status_identity_map_docs_example_validates(monkeypatch):
    """The exact example published in docs/slack-coordination.md must parse clean."""
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:<Desktop_Agent2's hub bearer token>")
    monkeypatch.setenv("UA_SLACK_STATUS_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_STATUS_IDENTITY_MAP", "U123=Desktop_Person,U456=Desktop_Agent2")
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.error is None
    assert cfg.enabled is True
    assert cfg.resolve_identity("U123") == "Desktop_Person"
    assert cfg.resolve_identity("U456") == "Desktop_Agent2"


def test_status_identity_map_rejects_identity_with_no_hub_token(monkeypatch):
    """An identity that is neither hub-registered nor Desktop_Person still fails closed."""
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:tok-codex-1")
    monkeypatch.setenv("UA_SLACK_STATUS_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_STATUS_IDENTITY_MAP", "U_X=Desktop_NoSuchAgent")
    cfg = SlackStatusLookupConfig.from_env()
    assert cfg.enabled is False
    assert cfg.error is not None and "registered" in cfg.error


def test_status_command_succeeds_under_managed_secret_mode(tmp_path, monkeypatch):
    """No agent_access.enforce('slack:status', ...) preflight (task #1104 follow-up, Devin review).

    read_object's own agent_access.enforce("platform_object:task", ...) is
    on managed-secret mode's arbitrary-execution allowlist; a mapped, signed
    status lookup must still succeed under UA_MANAGED_SECRETS=1.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    storage.init_db()
    created = tasks.task_add(title="managed secret mode read", project="unified-agent")
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-status-managed-secret",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": f"/status task-{created['id']}",
            },
        }
    ).encode()
    result = handle_http_request(
        body, headers=_signed(body), config=_status_config(), now=1700000000
    )
    assert result["dispatched"] is True
    assert result["result"]["reason"] == "ok"
    assert result["result"]["task"]["id"] == created["id"]


def test_status_lookup_invalid_map_denial_is_quiet_but_logged(tmp_path, monkeypatch, caplog):
    """Invalid-config-map denial is also quiet per request, but logged for the operator."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    cfg = _status_config(
        status_lookup=SlackStatusLookupConfig(
            enabled=False,
            identity_map={},
            error="duplicate Slack user id in status identity map: U_OPERATOR",
        )
    )
    with caplog.at_level("WARNING"):
        result = _slack_task_status(["task-1"], user_id="U_OPERATOR", config=cfg)
    assert result == {"kind": "task_status", "reason": "denied"}
    denied = events.query_events(channels=["slack.status.denied"])
    assert len(denied) == 1
    assert denied[0]["payload"] == {"slack_user_id": "U_OPERATOR", "config_invalid": True}
    assert any("invalid" in record.message.lower() for record in caplog.records)


def test_ack_text_for_denied_status_is_generic():
    from unified_agent.slack_outbound import _ack_text

    text = _ack_text({"dispatched": True, "result": {"kind": "task_status", "reason": "denied"}})
    assert text == "status lookup not available for this user"


def test_mention_addressed_status_routes_to_status_command():
    """Only Slack app_mention events route bare status to the command (task #1104)."""
    assert parse_event_request("<@UBOT> status task-42", event_type="app_mention") == {
        "command": "status",
        "args": ["task-42"],
    }


def test_other_user_mention_does_not_route_status_command():
    """A message mentioning another user stays inert even when its text starts with status."""
    parsed = parse_event_request("<@UTEAMMATE> status task-42", event_type="message")
    assert parsed["command"] == "chat"
    assert parsed["text"] == "<@UTEAMMATE> status task-42"


def test_app_mention_after_status_does_not_authorize_status_lookup():
    """The app must be addressed first, not merely mentioned later."""
    text = "status task-42 <@UBOT>"
    parsed = parse_event_request(text, event_type="app_mention")
    assert parsed["command"] == "chat"
    assert parsed["text"] == text


def test_other_user_leading_mention_with_app_mention_later_stays_chat():
    """A leading teammate mention cannot borrow a later app mention."""
    text = "<@UTEAMMATE> status task-42 <@UBOT>"
    parsed = parse_event_request(text, event_type="app_mention")
    assert parsed["command"] == "chat"
    assert parsed["text"] == text


def test_unaddressed_status_text_stays_inert_chat():
    """No leading mention: plain 'status task-42' must not dispatch (Devin review)."""
    parsed = parse_event_request("status task-42")
    assert parsed["command"] == "chat"
    assert parsed["text"] == "status task-42"


def test_mention_addressed_status_punctuation_stays_chat():
    """A mention followed by a word that is not exactly 'status' still stays chat."""
    parsed = parse_event_request("<@UBOT> status?")
    assert parsed["command"] == "chat"


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("UA_FRONTIER_TIMEOUT_S", "abc"),
        ("UA_FRONTIER_MAX_PROMPT_CHARS", "12k"),
        ("UA_FRONTIER_MAX_OUTPUT_TOKENS", "1.5"),
        ("UA_FRONTIER_MAX_BUDGET_USD", "lots"),
        ("UA_FRONTIER_MAX_BUDGET_USD", "inf"),
    ],
)
def test_bad_frontier_numeric_env_falls_back_without_breaking_slack_config(
    monkeypatch, caplog, name, bad
):
    """A malformed optional UA_FRONTIER_* limit must not break SlackConfig.from_env (#1712)."""
    monkeypatch.setenv(name, bad)
    with caplog.at_level("WARNING", logger="unified_agent.slack_config"):
        cfg = SlackConfig.from_env()
    assert name in caplog.text
    assert cfg.frontier.timeout_s == 120 or name != "UA_FRONTIER_TIMEOUT_S"
    assert cfg.frontier.max_budget_usd == 0.0
    assert frontier_policy("any-model", config=cfg)["allowed"] is False


def test_valid_frontier_numeric_env_is_still_honored(monkeypatch):
    monkeypatch.setenv("UA_FRONTIER_TIMEOUT_S", "45")
    monkeypatch.setenv("UA_FRONTIER_MAX_BUDGET_USD", "2.5")
    policy = FrontierPolicy.from_env()
    assert policy.timeout_s == 45
    assert policy.max_budget_usd == 2.5


def _progress_body(event_id: str, task_id: int) -> bytes:
    return json.dumps(
        {
            "type": "event_callback",
            "event_id": event_id,
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": f"/progress task-{task_id} picking this up",
            },
        }
    ).encode()


def _fill_fleet_to_hard_cap() -> None:
    for label in ("agent-a", "agent-b", "agent-c"):
        tid = tasks.task_add("held", lane="dispatch-ready")["id"]
        token = ua_config.agent_identity_var.set(label)
        try:
            tasks.task_claim(tid, label, expected_lane="dispatch-ready")
        finally:
            ua_config.agent_identity_var.reset(token)


@pytest.fixture
def _fleet_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "2")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "3")
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)
    storage.init_db()


def test_slack_progress_at_the_fleet_hard_cap_is_refused_and_says_so(_fleet_caps):
    # /progress moves a task into doing with no verified identity (task #1886):
    # it passes the fleet cap, and the reply names the cap instead of
    # claiming the update was queued for review.
    from unified_agent import slack_outbound

    _fill_fleet_to_hard_cap()
    tid = tasks.task_add("from slack", lane="dispatch-ready")["id"]
    body = _progress_body("Ev-progress-cap", tid)
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is False
    assert result["reason"] == "wip_cap"
    assert "requires_review" not in result
    assert slack_outbound._ack_text(result) == slack_outbound.WIP_CAP_ACK
    [row] = [t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid]
    assert row["status"] == "open"
    failure = events.query_events(channels=["slack.dispatch.failed"])[0]
    assert failure["payload"] == {
        "event_id": "Ev-progress-cap",
        "error_type": "ToolError",
        "reason": "wip_cap",
    }


def test_slack_progress_on_urgent_work_passes_the_hard_cap(_fleet_caps):
    _fill_fleet_to_hard_cap()
    tid = tasks.task_add("urgent from slack", lane="dispatch-ready", priority="high")["id"]
    body = _progress_body("Ev-progress-urgent", tid)
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert result["result"]["status"] == "doing"
    assert any("past the hard cap" in w for w in result["result"]["wip_warnings"])


def test_slack_progress_refused_for_another_reason_still_queues_for_review(_fleet_caps):
    # Only a WIP-cap refusal gets the cap reply; any other refusal of the same
    # move (here an archived row) keeps the review path.
    from unified_agent import slack_outbound

    tid = tasks.task_add("parked from slack", lane="dispatch-ready")["id"]
    tasks.task_update(tid, archived=True)
    body = _progress_body("Ev-progress-parked", tid)
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is False
    assert result["requires_review"] is True
    assert "reason" not in result
    assert slack_outbound._ack_text(result) == "accepted; queued for human review"


def test_slack_task_records_origin_from_the_authenticated_event(tmp_path, monkeypatch):
    """Task #1751: the origin is the signed event's user, channel and thread,
    not ids the message text names."""
    from unified_agent import completion_return

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-origin-1",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "ts": "1700000000.000200",
                "thread_ts": "1700000000.000100",
                "text": "/task ship it channel=C_EVIL thread_ts=999 user=U_EVIL",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    task_id = result["result"]["id"]
    origin = completion_return.get_origin(task_id)
    assert origin["surface"] == "slack"
    assert origin["channel"] == "C_REQUESTS"
    assert origin["thread"] == "1700000000.000100"
    assert origin["requester"] == "U_OPERATOR"

    # A top-level message threads on its own ts.
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-origin-2",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "ts": "1700000000.000300",
                "text": "/task top level",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert completion_return.get_origin(result["result"]["id"])["thread"] == "1700000000.000300"


def test_slack_slash_task_without_thread_records_no_origin(tmp_path, monkeypatch):
    from urllib.parse import urlencode

    from unified_agent import completion_return

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    body = urlencode(
        {
            "command": "/task",
            "text": "slash work",
            "user_id": "U_OPERATOR",
            "channel_id": "C_REQUESTS",
        }
    ).encode()
    headers = {**_signed(body), "Content-Type": "application/x-www-form-urlencoded"}
    result = handle_http_request(body, headers=headers, config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert completion_return.get_origin(result["result"]["id"]) is None


def test_default_coordinator_is_cloud_Agent17(monkeypatch):
    """The unset default still coordinates as cloud_agent17 (task #1879).

    That identity is a developer as of 2026-11-11. The coordinator label did
    not move with the profile.
    """
    from unified_agent import profiles
    from unified_agent.slack_config import DEFAULT_COORDINATOR_AGENT, SlackConfig
    from unified_agent.slack_gateway import dispatch

    monkeypatch.delenv("UA_SLACK_COORDINATOR_AGENT", raising=False)
    assert SlackConfig.from_env().coordinator_agent == DEFAULT_COORDINATOR_AGENT
    assert DEFAULT_COORDINATOR_AGENT == "cloud_agent17"
    assert profiles.default_for_identity(DEFAULT_COORDINATOR_AGENT) == profiles.DEVELOPER
    result = dispatch({"command": "contact", "args": []}, user_id="U_OPERATOR")
    assert result == {"kind": "contact", "agent": DEFAULT_COORDINATOR_AGENT}


def test_signed_assign_and_plan_use_the_default_coordinator(tmp_path, monkeypatch):
    """#2290: with UA_SLACK_COORDINATOR_AGENT unset, a signed request coordinates as the default."""
    from unified_agent import server
    from unified_agent.slack_config import DEFAULT_COORDINATOR_AGENT, SlackConfig

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    monkeypatch.delenv("UA_SLACK_COORDINATOR_AGENT", raising=False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing")
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_USERS", "U_OPERATOR")
    monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C_REQUESTS")
    config = SlackConfig.from_env()
    assert config.coordinator_agent == DEFAULT_COORDINATOR_AGENT

    seen: dict[str, dict] = {}

    def spy(tool_name: str):
        real = server.REGISTERED_TOOLS[tool_name]

        def call(*args, **kwargs):
            seen[tool_name] = kwargs
            return real(*args, **kwargs)

        return call

    monkeypatch.setitem(server.REGISTERED_TOOLS, "delegate", spy("delegate"))
    monkeypatch.setitem(server.REGISTERED_TOOLS, "plan_create", spy("plan_create"))

    for event_id, text in (
        ("Ev-default-assign", "/assign Desktop_Agent2 perform audit"),
        ("Ev-default-plan", "/plan ship the thing"),
    ):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": event_id,
                "event": {
                    "type": "app_mention",
                    "user": "U_OPERATOR",
                    "channel": "C_REQUESTS",
                    "text": text,
                },
            }
        ).encode()
        result = handle_http_request(body, headers=_signed(body), config=config, now=1700000000)
        assert result["dispatched"] is True

    assert seen["delegate"]["from_agent"] == DEFAULT_COORDINATOR_AGENT
    assert seen["plan_create"]["author"] == DEFAULT_COORDINATOR_AGENT
