"""Per-session setting negotiation before work, through the production ACP wire."""

from __future__ import annotations

import json
import sys

import pytest

from unified_agent import (
    acp_backend,
    acp_client,
    acp_matrix,
    node_queue,
    session_settings,
    storage,
)

SETUP = {
    "modes": {
        "currentModeId": "yolo",
        "availableModes": [
            {"id": "default", "name": "Default"},
            {"id": "autoEdit", "name": "Auto edit"},
            {"id": "yolo", "name": "Full access"},
            {"id": "code", "name": "Supervised"},
        ],
    },
    "models": {
        "currentModelId": "model-a",
        "availableModels": [
            {"modelId": "model-a", "name": "Model A"},
            {"modelId": "model-b", "name": "Model B"},
        ],
    },
}
FAKE = r"""
import json, os, sys
setup = json.loads(os.environ["SETTINGS_SETUP"])
def send(rid, result):
    print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":result}), flush=True)
def update(sid, body):
    print(json.dumps({"jsonrpc":"2.0", "method":"session/update",
                     "params":{"sessionId":sid, "update":body}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params", {})
    if method:
        with open(os.environ["SETTINGS_TRACE"], "a") as f:
            f.write(json.dumps({"method":method, "params":params}) + "\n")
    if method == "initialize":
        send(rid, {"protocolVersion":1, "agentCapabilities":{"sessionCapabilities":{"resume":{}}}})
    elif method in ("session/new", "session/resume", "session/load"):
        sid = params.get("sessionId", "settings-session")
        if os.environ.get("SETTINGS_STARTUP_UPDATES"):
            update("foreign-session", {"sessionUpdate":"current_model_update", "currentModelId":"foreign-model"})
            if os.environ["SETTINGS_STARTUP_UPDATES"] == "before":
                update(sid, {"sessionUpdate":"current_model_update", "currentModelId":"model-c"})
                update(sid, {"sessionUpdate":"current_mode_update", "currentModeId":"autoEdit"})
        send(rid, {"sessionId":sid, **setup})
        if os.environ.get("SETTINGS_STARTUP_UPDATES") == "after":
            update(sid, {"sessionUpdate":"current_model_update", "currentModelId":"model-c"})
            update(sid, {"sessionUpdate":"current_mode_update", "currentModeId":"autoEdit"})
    elif method in ("session/set_mode", "session/set_model"):
        kind = "mode" if method == "session/set_mode" else "model"
        setup[kind+"s"]["current"+kind.title()+"Id"] = params[kind+"Id"]
        if kind == "model" and os.environ.get("SETTINGS_SET_MODEL_BAD"):
            update(params["sessionId"], json.loads(os.environ["SETTINGS_SET_MODEL_BAD"]))
        if os.environ.get("SETTINGS_CLAMP"):
            update(params["sessionId"], {"sessionUpdate":"current_mode_update", "currentModeId":"autoEdit"})
        send(rid, {})
    elif method == "session/set_config_option":
        for o in setup["configOptions"]:
            if o["id"] == params["configId"]:
                o["currentValue"] = params["value"]
        response = json.loads(json.dumps(setup["configOptions"]))
        if os.environ.get("SETTINGS_STALE_CONFIG") and params["configId"] == "model":
            update(params["sessionId"], {"sessionUpdate":"current_mode_update", "currentModeId":"yolo"})
        send(rid, {"configOptions":response})
    elif method == "session/prompt":
        if os.environ.get("SETTINGS_APPROVAL"):
            print(json.dumps({"jsonrpc":"2.0", "id":998, "method":"session/request_permission",
                "params":{"sessionId":params["sessionId"], "options":[{"optionId":"yes", "kind":"allow_once"}]}}), flush=True)
            reply = json.loads(sys.stdin.readline())
            with open(os.environ["SETTINGS_TRACE"], "a") as f:
                f.write(json.dumps({"method":"permission_reply", "params":reply}) + "\n")
        if os.environ.get("SETTINGS_MODEL_UPDATE"):
            update(params["sessionId"], {"sessionUpdate":"current_model_update", "currentModelId":"model-c"})
        if os.environ.get("SETTINGS_BAD_MODE"):
            update(params["sessionId"], {"sessionUpdate":"current_mode_update", "currentModeId":os.environ["SETTINGS_BAD_MODE"]})
        if os.environ.get("SETTINGS_FOREIGN"):
            update("foreign-session", {"sessionUpdate":"current_mode_update", "currentModeId":"yolo"})
        if os.environ.get("SETTINGS_PENDING_PERMISSION"):
            import time
            print(json.dumps({"jsonrpc":"2.0", "id":999, "method":"session/request_permission",
                "params":{"sessionId":params["sessionId"], "options":[{"optionId":"yes", "kind":"allow_once"}]}}), flush=True)
            deadline = time.monotonic() + 3
            while not os.path.exists(os.environ["SETTINGS_PENDING_PERMISSION"]) and time.monotonic() < deadline:
                time.sleep(.005)
            if not os.path.exists(os.environ["SETTINGS_PENDING_PERMISSION"]):
                raise RuntimeError("approval callback did not enter")
        if os.environ.get("SETTINGS_CONFIG_ESCALATE"):
            for option in setup["configOptions"]:
                if option.get("category") == "mode":
                    option["currentValue"] = "yolo"
            update(params["sessionId"], {"sessionUpdate":"config_option_update", "configOptions":setup["configOptions"]})
        if os.environ.get("SETTINGS_ESCALATE"):
            update(params["sessionId"], {"sessionUpdate":"current_mode_update", "currentModeId":"yolo"})
        send(rid, {"stopReason":"end_turn"})
        if os.environ.get("SETTINGS_IDLE_ESCALATE"):
            import time
            deadline = time.monotonic() + 3
            while not os.path.exists(os.environ["SETTINGS_IDLE_ESCALATE"]) and time.monotonic() < deadline:
                time.sleep(.005)
            if not os.path.exists(os.environ["SETTINGS_IDLE_ESCALATE"]):
                raise RuntimeError("session did not enter idle")
            update(params["sessionId"], {"sessionUpdate":"current_mode_update", "currentModeId":"yolo"})
    elif rid is not None:
        send(rid, {})
"""


@pytest.fixture
def harness(tmp_path, monkeypatch):
    script = tmp_path / "settings-agent.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE)
    script.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    for row in acp_matrix.ACP_HARNESSES.values():
        monkeypatch.setenv(row["env"], str(script))
    monkeypatch.setenv("SETTINGS_SETUP", json.dumps(SETUP))
    monkeypatch.setenv("SETTINGS_TRACE", str(trace))
    return trace


def run(tmp_path, **kwargs):
    events = []
    event_hook = kwargs.pop("event_hook", lambda event: None)

    def on_event(event):
        events.append(event)
        event_hook(event)

    name = kwargs.pop("harness_name", "gemini")
    if name == "hermes":
        kwargs.setdefault("hermes_profile", "default")
    result = acp_backend.AcpBackend(name).run(
        prompt="synthetic task",
        cwd=str(tmp_path),
        session_dir=str(tmp_path / "session"),
        session_id="item",
        agent_label="Desktop_Agent2",
        timeout_s=10,
        on_event=on_event,
        **kwargs,
    )
    return result, events


def trace(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_default_supervised_and_model_reach_agent_before_prompt(harness, tmp_path):
    result, events = run(tmp_path, model="model-b")
    assert result["status"] == "success", result
    requests = trace(harness)
    assert [r["method"] for r in requests] == [
        "initialize",
        "session/new",
        "session/set_mode",
        "session/set_model",
        "session/prompt",
    ]
    assert requests[2]["params"]["modeId"] == "default"
    assert requests[3]["params"]["modelId"] == "model-b"
    opened = next(e["data"] for e in events if e["kind"] == "session_open")
    assert opened["model"] == "model-b"
    assert opened["settings"]["requested_mode"] == "supervised"
    assert opened["settings"]["permission_mode"] == "supervised"
    assert opened["settings"]["current_mode_id"] == "default"


@pytest.mark.parametrize(
    "mode,native", [("auto-accept-edits", "autoEdit"), ("full-access", "yolo")]
)
def test_explicit_modes_are_negotiated(harness, tmp_path, mode, native):
    result, events = run(tmp_path, permission_mode=mode)
    assert result["status"] == "success"
    assert trace(harness)[2]["params"]["modeId"] == native
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["permission_mode"] == mode


@pytest.mark.parametrize(
    "setup,kwargs",
    [
        ({}, {}),
        (SETUP, {"model": "unlisted"}),
        ({"modes": {"availableModes": [{"id": "code", "name": "Always ask"}]}}, {}),
    ],
)
def test_missing_or_unknown_settings_refuse_without_prompt(
    harness, tmp_path, monkeypatch, setup, kwargs
):
    monkeypatch.setenv("SETTINGS_SETUP", json.dumps(setup))
    result, events = run(tmp_path, **kwargs)
    assert result["status"] == "error"
    assert "session/prompt" not in [r["method"] for r in trace(harness)]
    assert any(e["kind"] == "settings_discovered" for e in events)
    assert any(e["kind"] == "settings_refused" for e in events)
    assert not any(e["kind"] == "session_open" for e in events)


def test_v1_config_options_choose_advertised_values(harness, tmp_path, monkeypatch):
    setup = {
        "configOptions": [
            {
                "id": "permissions",
                "type": "select",
                "category": "mode",
                "currentValue": "yolo",
                "options": [
                    {"value": "default", "name": "Default"},
                    {"value": "yolo", "name": "Bypass"},
                ],
            },
            {
                "id": "model",
                "type": "select",
                "category": "model",
                "currentValue": "model-a",
                "options": [{"value": "model-a", "name": "A"}, {"value": "model-b", "name": "B"}],
            },
        ]
    }
    monkeypatch.setenv("SETTINGS_SETUP", json.dumps(setup))
    result, events = run(tmp_path, model="model-b")
    assert result["status"] == "success", result
    requests = trace(harness)
    assert [r["method"] for r in requests[2:4]] == ["session/set_config_option"] * 2
    assert requests[2]["params"] == {
        "sessionId": "settings-session",
        "configId": "permissions",
        "value": "default",
    }
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["current_model_id"] == "model-b"


def test_effective_native_clamp_is_refused(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("SETTINGS_CLAMP", "1")
    result, events = run(tmp_path)
    assert result["status"] == "error"
    assert "unconfirmed" in result["error"]
    assert "session/prompt" not in [r["method"] for r in trace(harness)]


def test_unsolicited_escalation_cancels_and_reports(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("SETTINGS_ESCALATE", "1")
    cancelled = []
    original_cancel = acp_client.AcpClient.cancel

    def cancel(client, sid):
        cancelled.append(sid)
        return original_cancel(client, sid)

    monkeypatch.setattr(acp_client.AcpClient, "cancel", cancel)
    result, events = run(tmp_path)
    assert result["status"] == "error", result
    assert "unexpected_mode_escalation" in result["error"]
    assert any(e["kind"] == "settings_refused" for e in events)
    assert cancelled == ["settings-session"]


def test_foreign_update_cannot_change_effective_settings_or_cancel(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("SETTINGS_FOREIGN", "1")
    result, events = run(tmp_path)
    assert result["status"] == "success", result
    assert "session/cancel" not in [r["method"] for r in trace(harness)]
    assert not any(e["kind"] == "settings_refused" for e in events)
    assert not any(e["kind"] == "session_settings_update" for e in events)
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["current_mode_id"] == "default"


def test_resume_negotiates_before_next_prompt(harness, tmp_path):
    result, _ = run(
        tmp_path, resume_session_id="prior-session", permission_mode="full-access", model="model-b"
    )
    assert result["status"] == "success"
    assert [r["method"] for r in trace(harness)] == [
        "initialize",
        "session/resume",
        "session/set_mode",
        "session/set_model",
        "session/prompt",
    ]


def test_mode_ids_have_no_universal_permission_semantics():
    for harness in ("claude", "codex", "cursor", "hermes", "opencode"):
        assert session_settings.mode_class(harness, "ask") is None
        assert session_settings.mode_class(harness, "supervised") is None
        assert session_settings.mode_class(harness, "code") is None
    assert session_settings.mode_class("codex", "read-only") == "supervised"
    assert session_settings.mode_class("codex", "workspace-write") == "auto-accept-edits"
    assert session_settings.mode_class("codex", "agent") is None
    assert session_settings.mode_class("codex", "agent-full-access") == "full-access"


def test_model_and_mode_choices_are_honest_in_matrix():
    cap = acp_matrix.control_capabilities(
        "acp:gemini", advertised=True, version="fixture", features=[session_settings.FEATURE]
    )
    assert cap["capabilities"]["modes"]["state"] == "unknown"
    cap = acp_matrix.control_capabilities(
        "acp:gemini",
        advertised=True,
        version="fixture",
        features=[session_settings.FEATURE],
        settings=session_settings.advertised({}, "gemini"),
    )
    assert cap["capabilities"]["model_switch"]["state"] == "unsupported"


def test_queue_approval_payload_and_claim_preserve_mode(isolated_env, tmp_path, monkeypatch):
    node_queue.register(
        "settings-node",
        identity="Desktop_Agent2",
        facts={
            "backends": ["acp:gemini"],
            "node_features": [session_settings.FEATURE],
            "worktree_roots": [{"path": str(tmp_path)}],
        },
    )
    from unified_agent import safety

    payloads = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: payloads.append(payload)
    )
    queued = node_queue.request_dispatch(
        "settings-node",
        actor="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
        permission_mode="full-access",
        model="model-b",
    )
    assert payloads[0]["permission_mode"] == "full-access"
    assert payloads[0]["model"] == "model-b"
    claim = node_queue.claim_next("settings-node", identity="Desktop_Agent2")
    assert claim["permission_mode"] == "full-access"
    assert claim["model"] == "model-b"
    assert node_queue.get_item(queued["item_id"])["settings_source"] == "explicit_session"


def test_old_node_cannot_ignore_supervised_selection(isolated_env, tmp_path):
    node_queue.register(
        "old-settings-node", identity="Desktop_Agent2", facts={"backends": ["acp:gemini"]}
    )
    with pytest.raises(node_queue.NodeError, match="predates session settings"):
        node_queue.enqueue(
            "old-settings-node",
            created_by="Desktop_Agent2",
            prompt="synthetic",
            cwd=str(tmp_path),
            backend="acp:gemini",
        )


def test_claim_refuses_legacy_queued_acp_after_node_feature_loss(isolated_env, tmp_path):
    node_queue.register(
        "legacy-settings",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    queued = node_queue.enqueue(
        "legacy-settings",
        created_by="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch SET permission_mode=NULL WHERE id=?", (queued["item_id"],)
        )
        conn.commit()
    finally:
        conn.close()
    node_queue.register(
        "legacy-settings", identity="Desktop_Agent2", facts={"backends": ["acp:gemini"]}
    )
    assert node_queue.claim_next("legacy-settings", identity="Desktop_Agent2") is None
    assert node_queue.get_item(queued["item_id"])["status"] == "failed"


def test_reported_catalog_is_bounded_and_never_trusts_mode_class():
    reported = session_settings.reported(
        {
            "available_modes": {},
            "available_models": "wrong",
            "permission_mode": "supervised",
            "current_mode_id": "code",
            "extra_secret": "never expose",
            "mode_path": "invented",
            "refused": "false",
        },
        "gemini",
    )
    assert reported["available_modes"] == reported["available_models"] == []
    assert reported["permission_mode"] is None
    assert reported["mode_path"] is None
    assert reported["refused"] is False
    assert "extra_secret" not in reported


def test_unknown_hermes_edit_only_mode_does_not_claim_session_authority():
    assert session_settings.mode_class("hermes", "default") is None
    assert session_settings.mode_class("hermes", "dont_ask") is None


def test_resume_without_model_never_rereads_current_profile(isolated_env, tmp_path, monkeypatch):
    from unified_agent import operating_profiles, safety

    node_queue.register(
        "resume-settings",
        identity="Desktop_Agent2",
        facts={
            "backends": ["acp:gemini"],
            "node_features": [session_settings.FEATURE, "session_resume"],
        },
    )
    monkeypatch.setattr(safety, "check_prod_action_gate", lambda *args: None)
    monkeypatch.setattr(
        operating_profiles,
        "settings_for_launch",
        lambda *args: pytest.fail("resume reread profile"),
    )
    result = node_queue.request_dispatch(
        "resume-settings",
        actor="Desktop_Agent2",
        prompt="continue",
        cwd=str(tmp_path),
        backend="acp:gemini",
        resume_session_id="prior-session",
    )
    assert node_queue.get_item(result["item_id"])["model"] is None


@pytest.mark.parametrize("name", ["gemini", *sorted(session_settings.NATIVE_DEFAULT_HARNESSES)])
def test_pending_permission_cannot_allow_after_mode_violation(harness, tmp_path, monkeypatch, name):
    import threading

    pending = tmp_path / "approval-entered"
    done, violated = threading.Event(), threading.Event()
    monkeypatch.setenv("SETTINGS_ESCALATE", "1")
    monkeypatch.setenv("SETTINGS_PENDING_PERMISSION", str(pending))
    if name != "gemini":
        monkeypatch.setenv("SETTINGS_SETUP", "{}")

    def approval(request):
        pending.write_text("entered")
        assert violated.wait(3), "agent did not report escalation"
        return {"status": "approved", "option_id": "yes"}

    def event_hook(event):
        if event["kind"] == "settings_refused":
            violated.set()
        if event["kind"] == "permission_decision":
            done.set()

    result, events = run(tmp_path, harness_name=name, permission_fn=approval, event_hook=event_hook)
    assert result["status"] == "error"
    assert done.wait(3), "pending reply was not invalidated"
    decisions = [e["data"] for e in events if e["kind"] == "permission_decision"]
    assert decisions[-1]["status"] == "cancelled"
    assert decisions[-1]["reason"] == "settings_violation"


def test_config_option_update_escalation_stops_session(harness, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SETTINGS_SETUP",
        json.dumps(
            {
                "configOptions": [
                    {
                        "id": "permissions",
                        "type": "select",
                        "category": "mode",
                        "currentValue": "default",
                        "options": [
                            {"value": "default", "name": "Default"},
                            {"value": "yolo", "name": "Yolo"},
                        ],
                    }
                ]
            }
        ),
    )
    monkeypatch.setenv("SETTINGS_CONFIG_ESCALATE", "1")
    result, events = run(tmp_path)
    assert result["status"] == "error"
    assert "unexpected_mode_escalation" in result["error"]
    observed = [e["data"] for e in events if e["kind"] == "session_settings_update"]
    assert observed[-1]["settings"]["current_mode_id"] == "yolo"
    assert observed[-1]["refused"] is True


def test_malformed_advertisement_is_normalized_in_nodes_projection(isolated_env, tmp_path):
    node_queue.register(
        "bad-catalog",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    queued = node_queue.enqueue(
        "bad-catalog",
        created_by="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
    )
    claimed = node_queue.claim_next("bad-catalog", identity="Desktop_Agent2")
    assert claimed is not None
    node_queue.record_node_event(
        "bad-catalog",
        queued["item_id"],
        [
            {
                "kind": "settings_discovered",
                "session_id": "bad-session",
                "data": {
                    "settings": {
                        "available_modes": {},
                        "available_models": "invalid",
                        "arbitrary": "never public",
                    }
                },
            }
        ],
        identity="Desktop_Agent2",
    )
    public = node_queue.observed_session_settings()[("bad-catalog", "acp:gemini", None)]
    assert public["available_modes"] == public["available_models"] == []
    assert "arbitrary" not in public


def test_restart_full_access_requires_new_explicit_choice(isolated_env, tmp_path, monkeypatch):
    from unified_agent import safety

    node_queue.register(
        "restart-settings",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    first = node_queue.enqueue(
        "restart-settings",
        created_by="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
        permission_mode="full-access",
    )
    conn = storage.connect()
    try:
        conn.execute("UPDATE node_dispatch SET status='finished' WHERE id=?", (first["item_id"],))
        conn.commit()
    finally:
        conn.close()
    payloads = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: payloads.append(payload)
    )
    restarted = node_queue.restart(first["item_id"], actor="Desktop_Agent2")
    assert payloads[-1]["permission_mode"] == "supervised"
    assert node_queue.get_item(restarted["item_id"])["permission_mode"] == "supervised"


def test_blocking_settings_evidence_callback_cannot_release_approval(
    harness, tmp_path, monkeypatch
):
    import threading

    pending = tmp_path / "approval-entered-blocking-report"
    reporting, decided = threading.Event(), threading.Event()
    monkeypatch.setenv("SETTINGS_PENDING_PERMISSION", str(pending))
    monkeypatch.setenv("SETTINGS_ESCALATE", "1")

    def approval(request):
        pending.write_text("entered")
        assert reporting.wait(3)
        return {"status": "approved", "option_id": "yes"}

    def event_hook(event):
        if event["kind"] == "session_settings_update":
            reporting.set()
            assert decided.wait(3), (
                "pending callback should cancel while evidence callback is blocked"
            )
        if event["kind"] == "permission_decision":
            assert event["data"]["status"] == "cancelled"
            decided.set()

    result, events = run(tmp_path, permission_fn=approval, event_hook=event_hook)
    assert result["status"] == "error"
    assert decided.is_set()
    assert any(e["kind"] == "settings_refused" for e in events)


def test_operating_profile_model_never_implies_full_access(isolated_env, tmp_path, monkeypatch):
    from unified_agent import config, operating_profiles, safety

    node_queue.register(
        "profile-settings",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    monkeypatch.setattr(
        operating_profiles,
        "settings_for_launch",
        lambda *a: {"settings": {"model": "gemini-2.5-pro", "autonomy": "yolo"}},
    )
    monkeypatch.setattr(config, "resolve_run_model", lambda *a, **kw: "gemini-2.5-flash")
    payloads = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: payloads.append(payload)
    )
    queued = node_queue.request_dispatch(
        "profile-settings",
        actor="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
    )
    item = node_queue.get_item(queued["item_id"])
    assert item["model"] == payloads[-1]["model"] == "gemini-2.5-pro"
    assert item["permission_mode"] == payloads[-1]["permission_mode"] == "supervised"
    assert "model=operating_profile" in item["settings_source"]


def test_mode_update_racing_final_snapshot_cannot_prompt(harness, tmp_path, monkeypatch):
    setter = acp_client.AcpClient.set_session_setting
    snapshot = acp_client.AcpClient.session_settings

    def arm(client, *args, **kwargs):
        result = setter(client, *args, **kwargs)
        client.inject_final_race = True
        return result

    def read(client, harness):
        result = snapshot(client, harness)
        if getattr(client, "inject_final_race", False):
            client.inject_final_race = False
            client._dispatch(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": client.active_session_id,
                        "update": {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"},
                    },
                }
            )
        return result

    monkeypatch.setattr(acp_client.AcpClient, "set_session_setting", arm)
    monkeypatch.setattr(acp_client.AcpClient, "session_settings", read)
    result, events = run(tmp_path)
    assert result["status"] == "error"
    assert "session/prompt" not in [r["method"] for r in trace(harness)]
    assert not any(e["kind"] == "session_open" for e in events)
    assert any(e["kind"] == "settings_refused" for e in events)


def test_stale_model_config_response_cannot_hide_permission_update(harness, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SETTINGS_SETUP",
        json.dumps(
            {
                "configOptions": [
                    {
                        "id": "permissions",
                        "category": "mode",
                        "type": "select",
                        "currentValue": "default",
                        "options": [
                            {"value": "default", "name": "Default"},
                            {"value": "yolo", "name": "Yolo"},
                        ],
                    },
                    {
                        "id": "model",
                        "category": "model",
                        "type": "select",
                        "currentValue": "model-a",
                        "options": [
                            {"value": "model-a", "name": "A"},
                            {"value": "model-b", "name": "B"},
                        ],
                    },
                ]
            }
        ),
    )
    monkeypatch.setenv("SETTINGS_STALE_CONFIG", "1")
    result, events = run(tmp_path, model="model-b")
    assert result["status"] == "error"
    assert "mode_unconfirmed" in result["error"]
    assert "session/prompt" not in [r["method"] for r in trace(harness)]
    refusal = next(e for e in events if e["kind"] == "settings_refused")
    assert refusal["data"]["settings"]["current_mode_id"] == "yolo"


def test_legacy_setter_commit_cannot_overwrite_concurrent_native_update():
    import threading

    client = acp_client.AcpClient.__new__(acp_client.AcpClient)
    client._settings_lock = threading.RLock()
    client._setup_requests = {}
    client.session_setup = json.loads(json.dumps(SETUP))
    client.active_session_id = "race-session"
    attempted, completed = threading.Event(), threading.Event()
    workers, failures = [], []
    callbacks = []

    def callback(sid, update):
        assert not client._settings_lock._is_owned(), "callback called under settings lock"
        callbacks.append(client.session_settings("gemini")["current_mode_id"])

    client._on_update = callback

    def notify():
        attempted.set()
        try:
            client._dispatch(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "race-session",
                        "update": {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"},
                    },
                }
            )
        except Exception as exc:
            failures.append(exc)
        finally:
            completed.set()

    class Revisions(dict):
        armed = False

        def __getitem__(self, key):
            value = super().__getitem__(key)
            if self.armed:
                self.armed = False
                worker = threading.Thread(target=notify)
                workers.append(worker)
                worker.start()
                assert attempted.wait(3)
                # The notification must complete before an unfenced assignment,
                # or commit after the fenced assignment. No scheduling sleeps.
                if not client._settings_lock._is_owned():
                    assert completed.wait(3)
            return value

    client._setting_updates = Revisions(mode=0, model=0)

    def request(*args, **kwargs):
        assert not client._settings_lock._is_owned(), "RPC called under settings lock"
        client._setting_updates.armed = True
        return {}

    client.request = request
    client.set_session_setting("race-session", "mode", "default", harness="gemini")
    for worker in workers:
        worker.join(3)
    assert completed.is_set() and not failures
    assert client.session_settings("gemini")["current_mode_id"] == "yolo"
    assert callbacks == ["yolo"]


def test_credential_shapes_in_settings_never_reach_session_projection(isolated_env, tmp_path):
    node_queue.register(
        "redacted-settings",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    queued = node_queue.enqueue(
        "redacted-settings",
        created_by="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
    )
    node_queue.claim_next("redacted-settings", identity="Desktop_Agent2")
    secrets = ["uas_" + marker * 43 for marker in ("A", "B")]
    for kind, secret in zip(("session_open", "session_settings_update"), secrets, strict=True):
        node_queue.record_node_event(
            "redacted-settings",
            queued["item_id"],
            [
                {
                    "kind": kind,
                    "session_id": "redacted-session",
                    "data": {
                        "settings": {
                            "current_mode_id": "default",
                            "available_modes": [{"id": "default", "name": "Default"}],
                            "current_model_id": secret,
                            "requested_model": secret,
                            "available_models": [{"id": secret, "name": secret}],
                        }
                    },
                }
            ],
            identity="Desktop_Agent2",
        )
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT model, meta FROM agent_sessions WHERE external_session_id=?",
                ("redacted-session",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        public = node_queue.observed_session_settings()[("redacted-settings", "acp:gemini", None)]
        blob = json.dumps({"session": dict(row), "catalog": public})
        assert all(value not in blob for value in secrets)


def test_native_settings_cannot_forge_or_erase_approved_request_intent(
    isolated_env, tmp_path, monkeypatch
):
    from unified_agent import safety

    node_queue.register(
        "intent-settings",
        identity="Desktop_Agent2",
        facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
    )
    payloads = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: payloads.append(payload)
    )
    queued = node_queue.request_dispatch(
        "intent-settings",
        actor="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
        permission_mode="full-access",
        model="model-b",
    )
    assert payloads[-1]["permission_mode"] == "full-access" and payloads[-1]["model"] == "model-b"
    node_queue.claim_next("intent-settings", identity="Desktop_Agent2")
    refused_once = False
    for kind, mode, model, refused in (
        ("session_open", "yolo", "model-b", False),
        ("session_settings_update", "unknown-mode", "model-b", True),
        ("session_settings_update", "default", "model-c", False),
    ):
        refused_once = refused_once or refused
        node_queue.record_node_event(
            "intent-settings",
            queued["item_id"],
            [
                {
                    "kind": kind,
                    "session_id": "intent-session",
                    "data": {
                        "refused": refused,
                        "settings": {
                            "current_mode_id": mode,
                            "current_model_id": model,
                            "requested_mode": "supervised",
                            "requested_model": "forged-model",
                            "source": "forged-source",
                        },
                    },
                }
            ],
            identity="Desktop_Agent2",
        )
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT model, meta FROM agent_sessions WHERE external_session_id=?",
                ("intent-session",),
            ).fetchone()
        finally:
            conn.close()
        settings = json.loads(row["meta"])["session_settings"]
        assert settings["requested_mode"] == "full-access"
        assert settings["requested_model"] == "model-b"
        assert settings["source"] == "explicit_session"
        assert row["model"] == settings["current_model_id"] == model
        assert settings["refused"] is refused_once
    assert settings["permission_mode"] == "supervised"


@pytest.mark.parametrize("method", ["new", "resume", "load"])
@pytest.mark.parametrize("ordering", ["before", "after"])
def test_setup_response_cannot_drop_or_overwrite_immediate_settings(
    harness, tmp_path, monkeypatch, method, ordering
):
    import os
    import threading

    monkeypatch.setenv("SETTINGS_STARTUP_UPDATES", ordering)
    observed = threading.Event()
    callbacks = []
    queue_class = acp_client.queue.Queue

    class DelayedCallerQueue(queue_class):
        def get(self, *args, **kwargs):
            reply = super().get(*args, **kwargs)
            if isinstance(reply.get("result"), dict) and "modes" in reply["result"]:
                # Hold the caller until both immediate notifications have been
                # read. The old caller-side assignment deterministically lost
                # them; no scheduler sleeps are needed to expose the race.
                if ordering == "after" or method != "new":
                    assert observed.wait(3), "startup mode update was dropped"
            return reply

    monkeypatch.setattr(acp_client.queue, "Queue", DelayedCallerQueue)

    def on_update(sid, update):
        callbacks.append(sid)
        if update.get("sessionUpdate") == "current_mode_update":
            observed.set()

    client = acp_client.AcpClient(
        [os.environ["UA_GEMINI_BIN"]],
        session_dir=str(tmp_path),
        on_update=on_update,
    )
    try:
        client.initialize()
        if method == "new":
            sid = client.new_session(str(tmp_path))
        else:
            sid = "prior-session"
            getattr(client, method + "_session")(sid, str(tmp_path))
        settings = client.session_settings("gemini")
        # New-session notifications preceding the reply cannot establish an
        # identity; only the correlated result may bind the new session ID.
        accepted = ordering == "after" or method != "new"
        assert settings["current_model_id"] == ("model-c" if accepted else "model-a")
        assert settings["current_mode_id"] == ("autoEdit" if accepted else "yolo")
        assert client.active_session_id == sid
        assert "foreign-session" not in callbacks
        assert not client._setup_requests
    finally:
        client.terminate()


def test_unrequested_model_uses_latest_startup_native_observation(harness, tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("SETTINGS_STARTUP_UPDATES", "after")
    read = threading.Event()
    original_dispatch = acp_client.AcpClient._dispatch
    queue_class = acp_client.queue.Queue

    def dispatch(client, msg):
        original_dispatch(client, msg)
        if msg.get("params", {}).get("update", {}).get("sessionUpdate") == "current_mode_update":
            read.set()

    class DelayedCallerQueue(queue_class):
        def get(self, *args, **kwargs):
            reply = super().get(*args, **kwargs)
            if isinstance(reply.get("result"), dict) and "modes" in reply["result"]:
                assert read.wait(3)
            return reply

    monkeypatch.setattr(acp_client.AcpClient, "_dispatch", dispatch)
    monkeypatch.setattr(acp_client.queue, "Queue", DelayedCallerQueue)
    result, events = run(tmp_path)
    assert result["status"] == "success", result
    opened = next(e["data"] for e in events if e["kind"] == "session_open")
    assert opened["settings"]["requested_model"] is None
    assert opened["settings"]["current_model_id"] == "model-c"
    assert opened["model"] == "model-c"
    assert opened["settings"]["permission_mode"] == "supervised"
    assert "session/set_model" not in [r["method"] for r in trace(harness)]


UNCLASSIFIED = sorted(session_settings.NATIVE_DEFAULT_HARNESSES)


@pytest.mark.parametrize("name", UNCLASSIFIED)
@pytest.mark.parametrize(
    "advertisement",
    [
        {},
        {
            "modes": {
                "currentModeId": "default",
                "availableModes": [{"id": "default", "name": "Default"}],
            }
        },
    ],
)
def test_unmapped_supervised_keeps_native_default_honestly(
    harness, tmp_path, monkeypatch, name, advertisement
):
    monkeypatch.setenv("SETTINGS_SETUP", json.dumps(advertisement))
    result, events = run(tmp_path, harness_name=name)
    assert result["status"] == "success", result
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["requested_mode"] == "supervised"
    assert settings["permission_mode"] is None
    assert settings["mode_control"] == "native_default_unclassified"
    assert settings["baseline_mode_id"] == advertisement.get("modes", {}).get("currentModeId")
    assert "session/set_mode" not in [r["method"] for r in trace(harness)]
    projection = session_settings.reported(settings, name)
    assert projection["permission_mode"] is None
    assert projection["mode_control"] == "native_default_unclassified"
    cap = acp_matrix.control_capabilities(
        "acp:" + name, advertised=True, version=None, features=[session_settings.FEATURE]
    )
    assert cap["capabilities"]["modes"]["state"] == "unclassified"


@pytest.mark.parametrize("name", UNCLASSIFIED)
@pytest.mark.parametrize("mode", ["auto-accept-edits", "full-access"])
def test_unmapped_fallback_never_accepts_broader_mode(harness, tmp_path, name, mode):
    result, events = run(tmp_path, harness_name=name, permission_mode=mode)
    assert result["status"] == "error"
    assert "mode_unsupported" in result["error"]
    assert not any(e["kind"] == "session_open" for e in events)
    assert "session/prompt" not in [r["method"] for r in trace(harness)]


@pytest.mark.parametrize("name", UNCLASSIFIED)
def test_native_default_permission_relay_and_model_update(harness, tmp_path, monkeypatch, name):
    monkeypatch.setenv("SETTINGS_SETUP", "{}")
    monkeypatch.setenv("SETTINGS_APPROVAL", "1")
    monkeypatch.setenv("SETTINGS_MODEL_UPDATE", "1")
    permissions = []

    def approve(request):
        permissions.append(request)
        return {"status": "approved", "option_id": "yes"}

    result, events = run(tmp_path, harness_name=name, permission_fn=approve)
    assert result["status"] == "success", result
    assert len(permissions) == 1
    reply = next(r["params"] for r in trace(harness) if r["method"] == "permission_reply")
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "yes"}
    observed = next(e["data"]["settings"] for e in events if e["kind"] == "session_settings_update")
    assert observed["current_model_id"] == "model-c"
    assert observed["permission_mode"] is None
    assert observed["mode_control"] == "native_default_unclassified"


@pytest.mark.parametrize("name", UNCLASSIFIED)
@pytest.mark.parametrize("native", ["unknown-new-mode", "yolo", "x" * 201])
def test_native_default_drift_or_malformed_mode_stops(harness, tmp_path, monkeypatch, name, native):
    monkeypatch.setenv("SETTINGS_SETUP", "{}")
    monkeypatch.setenv("SETTINGS_BAD_MODE", native)
    result, events = run(tmp_path, harness_name=name)
    assert result["status"] == "error"
    assert "unexpected_mode_escalation" in result["error"]
    assert any(e["kind"] == "settings_refused" for e in events)


@pytest.mark.parametrize("name", ["gemini", *UNCLASSIFIED])
def test_idle_mode_escalation_cannot_return_success(harness, tmp_path, monkeypatch, name):
    idle = tmp_path / "idle-entered"
    monkeypatch.setenv("SETTINGS_IDLE_ESCALATE", str(idle))
    if name != "gemini":
        monkeypatch.setenv("SETTINGS_SETUP", "{}")

    def mark_idle(event):
        if event["kind"] == "session_idle":
            idle.write_text("entered")

    result, events = run(
        tmp_path,
        harness_name=name,
        idle_s=5,
        hold_idle=True,
        followup_fn=lambda: [],
        event_hook=mark_idle,
    )
    assert result["status"] == "error", result
    assert "unexpected_mode_escalation" in result["error"]
    assert any(e["kind"] == "session_idle" for e in events)
    assert any(e["kind"] == "settings_refused" for e in events)


@pytest.mark.parametrize(
    "requested,ids,expected",
    [
        ("anthropic/claude-sonnet-4", ["claude-sonnet-4"], "claude-sonnet-4"),
        ("claude-sonnet-4", ["anthropic/claude-sonnet-4"], "anthropic/claude-sonnet-4"),
        ("claude-sonnet-4", ["anthropic/claude-sonnet-4", "openai/claude-sonnet-4"], None),
        ("anthropic/claude-sonnet-4", ["openai/claude-sonnet-4"], None),
        ("claude-sonnet-4", ["sonnet"], None),
        ("model-a", ["model-a", "google/model-a"], "model-a"),
    ],
)
def test_model_aliases_are_exact_or_unique_catalog_bound(requested, ids, expected):
    settings = {"available_models": [{"id": ident} for ident in ids]}
    assert session_settings.select_model(settings, requested)[0] == expected


@pytest.mark.parametrize(
    "source,resume,success",
    [
        ("supervised_default;model=operating_profile", None, True),
        ("explicit_session", None, False),
        ("resumed_session;model=operating_profile", "prior-session", False),
    ],
)
def test_only_profile_model_preference_can_keep_native_default(
    harness, tmp_path, source, resume, success
):
    result, events = run(
        tmp_path, model="not-advertised", settings_source=source, resume_session_id=resume
    )
    assert (result["status"] == "success") is success, result
    if success:
        settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
        assert settings["requested_model"] == "not-advertised"
        assert settings["current_model_id"] == "model-a"
        assert settings["model_resolution"] == "profile_default_unavailable"
        assert "session/set_model" not in [r["method"] for r in trace(harness)]
    else:
        assert "session/prompt" not in [r["method"] for r in trace(harness)]


def test_model_catalog_alias_reaches_wire_and_preserves_requested_id(harness, tmp_path):
    result, events = run(tmp_path, model="google/model-b")
    assert result["status"] == "success", result
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["requested_model"] == "google/model-b"
    assert settings["current_model_id"] == "model-b"
    assert settings["model_resolution"] == "catalog_alias"
    assert (
        next(r["params"] for r in trace(harness) if r["method"] == "session/set_model")["modelId"]
        == "model-b"
    )


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "current_mode_update", "currentModeId": "x" * 201},
        {
            "sessionUpdate": "config_option_update",
            "configOptions": [
                {
                    "category": "mode",
                    "type": "select",
                    "id": "m1",
                    "currentValue": "default",
                    "options": [{"value": "default"}],
                },
                {
                    "category": "mode",
                    "type": "select",
                    "id": "m2",
                    "currentValue": "default",
                    "options": [{"value": "default"}],
                },
            ],
        },
    ],
)
@pytest.mark.parametrize("baseline", [None, "default"])
def test_native_default_malformed_update_during_model_setup_refuses(
    harness, tmp_path, monkeypatch, update, baseline
):
    setup = {"models": SETUP["models"]}
    if baseline is not None:
        setup["modes"] = {"currentModeId": baseline, "availableModes": [{"id": baseline}]}
    monkeypatch.setenv("SETTINGS_SETUP", json.dumps(setup))
    monkeypatch.setenv("SETTINGS_SET_MODEL_BAD", json.dumps(update))
    result, events = run(tmp_path, harness_name="cursor", model="model-b")
    assert result["status"] == "error", result
    assert "unexpected_mode_escalation" in result["error"]
    assert "session/prompt" not in [r["method"] for r in trace(harness)]
    assert not any(e["kind"] == "session_open" for e in events)
    assert any(e["kind"] == "settings_refused" for e in events)


@pytest.mark.parametrize("name", UNCLASSIFIED)
def test_native_default_resume_is_still_unclassified(harness, tmp_path, monkeypatch, name):
    monkeypatch.setenv("SETTINGS_SETUP", "{}")
    result, events = run(tmp_path, harness_name=name, resume_session_id="prior-session")
    assert result["status"] == "success", result
    settings = next(e["data"]["settings"] for e in events if e["kind"] == "session_open")
    assert settings["permission_mode"] is None
    assert settings["mode_control"] == "native_default_unclassified"
    assert "session/set_mode" not in [r["method"] for r in trace(harness)]


def test_fallback_projection_cannot_forge_supervision():
    raw = {
        "mode_control": "native_default_unclassified",
        "permission_mode": "supervised",
        "requested_mode": "supervised",
        "current_mode_id": "default",
        "baseline_mode_id": "default",
    }
    projected = session_settings.reported(raw, "cursor")
    assert projected["permission_mode"] is None
    assert projected["mode_control"] == "native_default_unclassified"
    raw["requested_mode"] = "full-access"
    assert session_settings.reported(raw, "cursor")["mode_control"] == "unclassified"
    assert session_settings.reported(raw, "gemini")["mode_control"] != "native_default_unclassified"


def test_quiet_node_catalog_survives_more_than_shared_event_window(isolated_env, tmp_path):
    items = {}
    for name in ("quiet-catalog", "busy-catalog"):
        node_queue.register(
            name,
            identity="Desktop_Agent2",
            facts={"backends": ["acp:gemini"], "node_features": [session_settings.FEATURE]},
        )
        queued = node_queue.enqueue(
            name,
            created_by="Desktop_Agent2",
            prompt="synthetic",
            cwd=str(tmp_path),
            backend="acp:gemini",
        )
        node_queue.claim_next(name, identity="Desktop_Agent2")
        items[name] = queued["item_id"]
        node_queue.record_node_event(
            name,
            queued["item_id"],
            [
                {
                    "kind": "settings_discovered",
                    "session_id": name,
                    "data": {"settings": session_settings.advertised(SETUP, "gemini")},
                }
            ],
            identity="Desktop_Agent2",
        )
    conn = storage.connect()
    try:
        event = conn.execute(
            "SELECT channel, payload, created_at FROM event_log WHERE json_extract(payload, '$.item_id')=? AND json_extract(payload, '$.acp_kind')='settings_discovered'",
            (items["busy-catalog"],),
        ).fetchone()
        conn.executemany(
            "INSERT INTO event_log(ulid, channel, payload, created_at) VALUES(?,?,?,?)",
            [(f"ZZZ-CATALOG-{i:04d}", *tuple(event)) for i in range(1001)],
        )
        conn.commit()
    finally:
        conn.close()
    catalogs = node_queue.observed_session_settings()
    assert set(catalogs) == {
        ("quiet-catalog", "acp:gemini", None),
        ("busy-catalog", "acp:gemini", None),
    }
    assert catalogs[("quiet-catalog", "acp:gemini", None)]["available_models"][0]["id"] == "model-a"


@pytest.mark.parametrize("explicit", [False, True])
def test_resume_freezes_model_but_fresh_restart_preserves_model_origin(
    isolated_env, tmp_path, monkeypatch, explicit
):
    from unified_agent import config, node_resume, operating_profiles, safety

    node_queue.register(
        "origin-settings",
        identity="Desktop_Agent2",
        facts={
            "backends": ["acp:gemini"],
            "node_features": [session_settings.FEATURE, "session_resume"],
        },
    )
    current = ["gemini-2.5-pro"]
    monkeypatch.setattr(
        operating_profiles, "settings_for_launch", lambda *a: {"settings": {"model": current[0]}}
    )
    monkeypatch.setattr(config, "resolve_run_model", lambda *a, **kw: "gemini-2.5-flash")
    approvals = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: approvals.append(payload)
    )
    first = node_queue.request_dispatch(
        "origin-settings",
        actor="Desktop_Agent2",
        prompt="synthetic",
        cwd=str(tmp_path),
        backend="acp:gemini",
        model="gemini-explicit" if explicit else None,
    )
    node_queue.claim_next("origin-settings", identity="Desktop_Agent2")
    node_queue.record_node_event(
        "origin-settings",
        first["item_id"],
        [
            {
                "kind": "session_open",
                "session_id": "origin-session",
                "data": {
                    "resume_capability": "resume",
                    "settings": {
                        "current_mode_id": "default",
                        "current_model_id": "gemini-native-frozen",
                    },
                },
            },
            {
                "kind": "session_end",
                "session_id": "origin-session",
                "data": {"stop_reason": "end_turn"},
            },
        ],
        identity="Desktop_Agent2",
    )
    conn = storage.connect()
    try:
        conn.execute("UPDATE node_dispatch SET status='finished' WHERE id=?", (first["item_id"],))
        conn.commit()
    finally:
        conn.close()
    current[0] = "gemini-3-pro"
    resumed = node_resume.resume(first["item_id"], prompt="continue", actor="Desktop_Agent2")
    resumed_item = node_queue.get_item(resumed["item_id"])
    assert resumed_item["model"] == approvals[-1]["model"] == "gemini-native-frozen"
    assert resumed_item["settings_source"].startswith("resumed_session")
    assert ("model=operating_profile" in resumed_item["settings_source"]) is (not explicit)
    conn = storage.connect()
    try:
        conn.execute("UPDATE node_dispatch SET status='finished' WHERE id=?", (resumed["item_id"],))
        conn.commit()
    finally:
        conn.close()
    restarted = node_queue.restart(resumed["item_id"], actor="Desktop_Agent2")
    restarted_item = node_queue.get_item(restarted["item_id"])
    assert (
        restarted_item["model"]
        == approvals[-1]["model"]
        == ("gemini-native-frozen" if explicit else "gemini-3-pro")
    )
    assert restarted_item["permission_mode"] == "supervised"
    assert restarted_item["settings_source"].startswith("supervised_default")
