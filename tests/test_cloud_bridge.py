"""Relay safety and interruption behavior, without live hub writes or credentials."""

import json
import time
from pathlib import Path

import pytest

from unified_agent import cloud_bridge_cli as bridge

PROVENANCE = {
    "identity": "cloud_agent17",
    "parent_thread_id": "thread-1",
    "parent_request_id": "request-1",
    "executor_label": "server_agent2",
    "executor_attribution": "caller-reported; not authenticated by hub",
    "issuer": "https://hub.example",
}
MESSAGE = {"act": "query", "to_agent": "Desktop_Agent1", "body": "Approved text", "task_id": 1789}


@pytest.fixture
def state(tmp_path):
    return bridge.Checkpoint(tmp_path / "private" / "checkpoint.json", PROVENANCE)


class Hub:
    def __init__(self):
        self.calls = []
        self.rows = [{"seq": 2, "message_id": "m2"}, {"seq": 7, "message_id": "m7"}]
        self.identity = "cloud_agent17"
        self.workspace = "denied"
        self.profile = "developer"
        self.sent = {}
        self.stored = {}
        self.lose_response = False

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "profile_status":
            return {"identity": self.identity, "profile": self.profile}
        if name == "observability_manage":
            return {"identity": self.identity, "workspace": self.workspace}
        if name == "message_list":
            if "message_id" in args:
                assert args["from_agent"] == "cloud_agent17" and "session_id" not in args
                row = self.stored.get(args["message_id"])
                return {"messages": [row] if row is not None else []}
            assert args["to_agent"] == "cloud_agent17" and "session_id" not in args
            return {
                "messages": [
                    row
                    for row in self.rows
                    if type(row["seq"]) is not int or row["seq"] > args["after_seq"]
                ]
            }
        if name == "task_list":
            return {"tasks": [], "truncated": False}
        if name == "vault_read":
            return {"path": args["path"], "content": "updated: 2026-09-27\nDated guidance"}
        if name == "message_send":
            receipt = self.sent.setdefault(
                args["message_id"],
                {
                    "accepted": True,
                    "from_agent": "cloud_agent17",
                    "message_id": args["message_id"],
                    "to_agent": args["to_agent"],
                    "task_id": args["task_id"],
                },
            )
            self.stored.setdefault(
                args["message_id"],
                {
                    **args,
                    "from_agent": "cloud_agent17",
                    "seq": 42,
                    "reply_to": args.get("reply_to"),
                    "expects_reply": args.get("expects_reply", False),
                    "body_pruned": False,
                },
            )
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("lost response after commit")
            return receipt
        raise AssertionError(f"Unexpected API: {name}")


def private_json(path: Path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def credential(tmp_path, **changes):
    saved = {
        "agent": "cloud_agent17",
        "hub": "https://hub.example",
        "expires_at": time.time() + 60,
        "access_token": "fixture-not-a-real-token",
    }
    saved.update(changes)
    return private_json(tmp_path / "credential.json", saved)


def test_dedicated_credential_has_no_ambient_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_AGENT", "server_agent2")
    monkeypatch.setenv("UA_TOKEN", "ambient-token-must-not-be-used")
    result = bridge.Credential.load(credential(tmp_path), "https://hub.example")
    assert result.access_token == "fixture-not-a-real-token"
    assert result.access_token not in repr(result)
    with pytest.raises(FileNotFoundError):
        bridge.Credential.load(tmp_path / "missing.json", "https://hub.example")


@pytest.mark.parametrize(
    "changes",
    [
        {"agent": "server_agent2"},
        {"hub": "https://other.example"},
        {"expires_at": 0},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"access_token": ""},
        {"access_token": "Bearer bad"},
    ],
)
def test_invalid_dedicated_credential_refused(tmp_path, changes):
    with pytest.raises(bridge.BridgeError):
        bridge.Credential.load(credential(tmp_path, **changes), "https://hub.example")


def test_private_input_refuses_shared_file_and_symlink(tmp_path):
    path = credential(tmp_path)
    path.chmod(0o644)
    with pytest.raises(bridge.BridgeError):
        bridge.Credential.load(path, "https://hub.example")
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        bridge.Credential.load(link, "https://hub.example")


@pytest.mark.parametrize(
    "issuer",
    [
        "http://hub.example",
        "https://hub.example/mcp",
        "https://name@hub.example",
        "https://hub.example?x=y",
    ],
)
def test_expected_issuer_is_an_origin(tmp_path, issuer):
    with pytest.raises(bridge.BridgeError):
        bridge.Credential.load(credential(tmp_path), issuer)


def test_local_alias_keeps_original_oauth_authority(tmp_path):
    cred = bridge.Credential.load(credential(tmp_path), "https://hub.example")
    resource = {
        "resource": "https://hub.example/mcp",
        "authorization_servers": ["https://hub.example/"],
    }
    bridge.check_authority(cred, resource, {"issuer": "https://hub.example/"})
    with pytest.raises(bridge.BridgeError):
        bridge.check_authority(
            cred, {**resource, "resource": bridge.LOOPBACK}, {"issuer": cred.issuer}
        )
    with pytest.raises(bridge.BridgeError):
        bridge.check_authority(cred, resource, {"issuer": "https://other.example"})


async def test_refuses_unapproved_transport_before_network(tmp_path):
    cred = bridge.Credential.load(credential(tmp_path), "https://hub.example")
    with pytest.raises(bridge.BridgeError):
        async with bridge.connect(cred, "http://100.1.2.3:8765/mcp"):
            pytest.fail("Must not connect")


async def test_snapshot_replayed_after_restart_until_explicit_consumption(state):
    hub = Hub()
    with state.locked():
        packet = await bridge.snapshot(hub, state)
        assert state.state["cursor"] == 0
    reopened = bridge.Checkpoint(state.path, PROVENANCE)
    hub.rows.append({"seq": 10, "message_id": "later"})
    with reopened.locked():
        before = len(hub.calls)
        assert await bridge.snapshot(hub, reopened) == packet
        assert [name for name, _ in hub.calls[before:]] == [
            "profile_status",
            "observability_manage",
        ]
        result = reopened.consume(packet["packet_id"])
        assert result["cursor"] == 7 and result["hub_ack_sent"] is False
        next_packet = await bridge.snapshot(hub, reopened)
        assert next_packet["inbox"]["messages"] == [{"seq": 10, "message_id": "later"}]
    assert not any(
        name in {"session_start", "task_claim", "delegate", "coordination_manage"}
        for name, _ in hub.calls
    )
    assert state.path.stat().st_mode & 0o077 == 0


async def test_pending_packet_still_requires_fresh_verified_identity(state):
    hub = Hub()
    with state.locked():
        await bridge.snapshot(hub, state)
        hub.identity = "server_agent2"
        with pytest.raises(bridge.BridgeError):
            await bridge.snapshot(hub, state)


@pytest.mark.parametrize(
    "identity, workspace", [("server_agent2", "denied"), ("cloud_agent17", "/tmp")]
)
async def test_no_inbox_or_outbound_call_before_identity_boundary(state, identity, workspace):
    hub = Hub()
    hub.identity, hub.workspace = identity, workspace
    with state.locked(), pytest.raises(bridge.BridgeError):
        await bridge.send_message(hub, state, "approved-action", MESSAGE)
    assert not any(name == "message_send" for name, _ in hub.calls)


async def test_onboarding_only_uses_focused_authenticated_vault_reads(state):
    hub = Hub()
    with state.locked():
        paths = ("Wiki/guide.md", "Wiki/registry.md")
        packet = await bridge.snapshot(hub, state, onboarding=True, vault_sources=paths)
    assert [args["path"] for name, args in hub.calls if name == "vault_read"] == list(paths)
    assert len(packet["vault_sources"]) == 2
    assert all("dated Wiki" in row["authority"] for row in packet["vault_sources"])
    assert packet["provenance"]["executor_attribution"].startswith("caller-reported")
    assert packet["verified_principal"]["identity"] == "cloud_agent17"
    assert not any(name.startswith("fs_") for name, _ in hub.calls)


async def test_lost_response_retries_same_id_and_one_hub_action(state):
    hub = Hub()
    hub.lose_response = True
    with state.locked(), pytest.raises(TimeoutError):
        await bridge.send_message(hub, state, "approved-action", MESSAGE)
    reopened = bridge.Checkpoint(state.path, PROVENANCE)
    with reopened.locked():
        record = next(iter(reopened.state["messages"].values()))
        assert record["status"] == "outcome_unknown"
        receipt = await bridge.send_message(hub, reopened, "approved-action", MESSAGE)
        send_count = len([name for name, _ in hub.calls if name == "message_send"])
        assert await bridge.send_message(hub, reopened, "approved-action", MESSAGE) == receipt
        assert len([name for name, _ in hub.calls if name == "message_send"]) == send_count
    assert len(hub.sent) == 1
    assert len({args["message_id"] for name, args in hub.calls if name == "message_send"}) == 1


def test_stage_is_local_and_rejects_payload_change(state):
    with state.locked():
        record = bridge.stage_message(state, "approved-action", MESSAGE)
        assert record["status"] == "staged"
        with pytest.raises(bridge.BridgeError):
            bridge.stage_message(state, "approved-action", {**MESSAGE, "body": "Unapproved change"})


@pytest.mark.parametrize(
    "changes",
    [
        {"from_agent": "Desktop_Person"},
        {"task_id": None},
        {"to_session": "foreign-session"},
        {"act": "delegate"},
    ],
)
def test_message_surface_excludes_impersonation_and_nonidempotent_tools(state, changes):
    with state.locked(), pytest.raises(bridge.BridgeError):
        bridge.stage_message(state, "action", {**MESSAGE, **changes})


def test_checkpoint_exclusive_and_request_bound(state):
    other = bridge.Checkpoint(state.path, PROVENANCE)
    with state.locked():
        state.save()
        with pytest.raises(bridge.BridgeError), other.locked():
            pytest.fail("Concurrent writer entered")
    with (
        pytest.raises(bridge.BridgeError),
        bridge.Checkpoint(state.path, {**PROVENANCE, "parent_thread_id": "other-thread"}).locked(),
    ):
        pytest.fail("Foreign parent entered")


async def test_wrong_consumption_id_does_not_advance_cursor(state):
    with state.locked():
        await bridge.snapshot(Hub(), state)
        with pytest.raises(bridge.BridgeError):
            state.consume("wrong-packet")
        assert state.state["cursor"] == 0 and state.state["pending"] is not None


async def test_failed_snapshot_does_not_persist_partial_progress(state):
    hub = Hub()
    hub.rows = [{"seq": None}]
    with state.locked(), pytest.raises(bridge.BridgeError):
        await bridge.snapshot(hub, state)
    assert not state.path.exists()


async def test_pending_packet_cannot_silently_omit_requested_onboarding(state):
    with state.locked():
        await bridge.snapshot(Hub(), state)
        with pytest.raises(bridge.BridgeError):
            await bridge.snapshot(Hub(), state, onboarding=True, vault_sources=("Wiki/guide.md",))


def test_action_identity_survives_declared_executor_handoff(state, tmp_path):
    replacement = bridge.Checkpoint(
        state.path,
        {**PROVENANCE, "executor_label": "replacement_executor"},
    )
    with state.locked():
        first = bridge.stage_message(state, "same-approved-action", MESSAGE)
    with replacement.locked():
        second = bridge.stage_message(replacement, "same-approved-action", MESSAGE)
    assert first["params"]["message_id"] == second["params"]["message_id"]


async def test_bad_receipt_preserves_unknown_outcome(state):
    hub = Hub()

    async def wrong_receipt(name, args):
        result = await hub(name, args)
        if name == "message_send":
            return {**result, "to_agent": "other_recipient"}
        return result

    with state.locked():
        with pytest.raises(bridge.BridgeError):
            await bridge.send_message(wrong_receipt, state, "action", MESSAGE)
        record = next(iter(state.state["messages"].values()))
        assert record["status"] == "outcome_unknown" and record["receipt"] is None


def test_capacity_failure_keeps_existing_checkpoint(state):
    with state.locked():
        state.save()
        prior = state.path.read_bytes()
        state.state["large"] = "x" * 2_000_000
        with pytest.raises(bridge.BridgeError):
            state.save()
        assert state.path.read_bytes() == prior


async def test_executor_handoff_reuses_ledger_and_rejects_changed_intent(state):
    hub = Hub()
    hub.lose_response = True
    with state.locked(), pytest.raises(TimeoutError):
        await bridge.send_message(hub, state, "same-action", MESSAGE)
    replacement = bridge.Checkpoint(state.path, {**PROVENANCE, "executor_label": "replacement"})
    with replacement.locked():
        with pytest.raises(bridge.BridgeError):
            await bridge.send_message(
                hub, replacement, "same-action", {**MESSAGE, "body": "Changed"}
            )
        receipt = await bridge.send_message(hub, replacement, "same-action", MESSAGE)
        assert receipt["accepted"] is True
    assert len(hub.sent) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"body": "Changed"},
        {"act": "handoff"},
        {"expects_reply": True},
        {"reply_to": "different-parent"},
    ],
)
async def test_recreated_ledger_cannot_accept_changed_intent_after_unknown_send(
    state, tmp_path, change
):
    hub = Hub()
    hub.lose_response = True
    with state.locked(), pytest.raises(TimeoutError):
        await bridge.send_message(hub, state, "same-action", MESSAGE)
    replacement = bridge.Checkpoint(
        tmp_path / "replacement" / "state.json", {**PROVENANCE, "executor_label": "replacement"}
    )
    before = len([name for name, _ in hub.calls if name == "message_send"])
    with replacement.locked(), pytest.raises(bridge.BridgeError):
        await bridge.send_message(hub, replacement, "same-action", {**MESSAGE, **change})
    assert len([name for name, _ in hub.calls if name == "message_send"]) == before
    assert len(hub.sent) == 1
    assert all(row["receipt"] is None for row in replacement.state["messages"].values())


async def test_sender_id_dedupe_race_cannot_mark_changed_payload_accepted(state):
    hub = Hub()

    async def race(name, args):
        if name == "message_send":
            hub.stored[args["message_id"]] = {
                **args,
                "from_agent": "cloud_agent17",
                "body": "Other writer's intent",
                "reply_to": None,
                "expects_reply": False,
                "body_pruned": False,
            }
        return await hub(name, args)

    with state.locked():
        with pytest.raises(bridge.BridgeError):
            await bridge.send_message(race, state, "action", MESSAGE)
        record = next(iter(state.state["messages"].values()))
        assert record["status"] == "outcome_unknown" and record["receipt"] is None


async def test_pruned_stored_message_is_not_replayed_or_accepted(state, tmp_path):
    hub = Hub()
    hub.lose_response = True
    with state.locked(), pytest.raises(TimeoutError):
        await bridge.send_message(hub, state, "action", MESSAGE)
    row = next(iter(hub.stored.values()))
    row.update(body=None, body_pruned=True)
    replacement = bridge.Checkpoint(tmp_path / "replacement" / "state.json", PROVENANCE)
    before = len([name for name, _ in hub.calls if name == "message_send"])
    with replacement.locked(), pytest.raises(bridge.BridgeError):
        await bridge.send_message(hub, replacement, "action", MESSAGE)
    assert len([name for name, _ in hub.calls if name == "message_send"]) == before


async def test_recreated_ledger_reconciles_matching_unknown_send(state, tmp_path):
    hub = Hub()
    hub.lose_response = True
    with state.locked(), pytest.raises(TimeoutError):
        await bridge.send_message(hub, state, "same-action", MESSAGE)
    replacement = bridge.Checkpoint(
        tmp_path / "replacement" / "state.json", {**PROVENANCE, "executor_label": "replacement"}
    )
    with replacement.locked():
        receipt = await bridge.send_message(hub, replacement, "same-action", MESSAGE)
        assert receipt["accepted"] is True
        assert next(iter(replacement.state["messages"].values()))["status"] == "accepted"
    assert len(hub.sent) == 1


@pytest.mark.parametrize("location", ["http://127.0.0.1:8765/other", "https://other.example/mcp"])
async def test_mcp_redirect_is_rejected_before_sdk_can_follow_it(
    tmp_path, monkeypatch, capsys, location
):
    import httpx2

    requests = []
    client_type = httpx2.AsyncClient

    def respond(request):
        requests.append(request)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://hub.example/mcp",
                    "authorization_servers": ["https://hub.example"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": "https://hub.example"})
        if request.method == "POST" and request.url.path == "/mcp":
            return httpx2.Response(307, headers={"location": location})
        return httpx2.Response(405)

    def client(**kwargs):
        return client_type(transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    cred = bridge.Credential.load(credential(tmp_path), "https://hub.example")
    with pytest.raises(Exception) as raised:
        async with bridge.connect(cred, bridge.LOOPBACK):
            pytest.fail("Redirected MCP endpoint must not initialize")

    def rejected_redirect(error):
        return isinstance(error, bridge.BridgeError) or any(
            rejected_redirect(child) for child in getattr(error, "exceptions", [])
        )

    assert rejected_redirect(raised.value)
    authenticated = [r for r in requests if "authorization" in r.headers]
    assert authenticated
    assert all(str(r.url) == bridge.LOOPBACK for r in authenticated)
    assert not any(str(r.url) == location for r in requests)

    async def rejected_run(args):
        raise raised.value

    monkeypatch.setattr(bridge, "run", rejected_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "relay",
            "--state",
            "unused",
            "--issuer",
            "https://hub.example",
            "--parent-thread",
            "thread",
            "--parent-request",
            "request",
            "--executor",
            "fixture",
            "snapshot",
        ],
    )
    # The synchronous CLI owns asyncio.run, so exercise it outside this event loop.
    import asyncio

    with pytest.raises(SystemExit) as exited:
        await asyncio.to_thread(bridge.main)
    assert exited.value.code == 1
    assert capsys.readouterr().err == (
        "cloud_agent17 bridge failed; checkpoint preserved, no raw diagnostics emitted.\n"
    )


async def test_body_requiring_hub_redaction_is_rejected_before_checkpoint_or_send(state):
    hub = Hub()
    request = {**MESSAGE, "body": "Do not transmit " + "gh" + "p_" + "x" * 40}
    with state.locked(), pytest.raises(bridge.BridgeError, match="sanitize"):
        await bridge.send_message(hub, state, "redacted-body", request)
    assert state.state["messages"] == {}
    assert not any(name == "message_send" for name, _ in hub.calls)


def test_already_sanitized_body_can_be_staged(state):
    with state.locked():
        record = bridge.stage_message(
            state, "sanitized-body", {**MESSAGE, "body": "[REDACTED_SECRET]"}
        )
        assert record["status"] == "staged"


async def test_exact_mcp_resource_initializes_and_reads_with_private_credential(
    tmp_path, monkeypatch
):
    import httpx2

    requests = []
    client_type = httpx2.AsyncClient

    def respond(request):
        requests.append(request)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://hub.example/mcp",
                    "authorization_servers": ["https://hub.example"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": "https://hub.example"})
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx2.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx2.Response(202)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif payload["method"] == "tools/list":
            result = {"tools": [{"name": "profile_status", "inputSchema": {"type": "object"}}]}
        else:
            assert payload["method"] == "tools/call"
            assert payload["params"]["name"] == "profile_status"
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "identity": "cloud_agent17",
                                "profile": "developer",
                            }
                        ),
                    }
                ],
                "isError": False,
            }
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def client(**kwargs):
        return client_type(transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    cred = bridge.Credential.load(credential(tmp_path), "https://hub.example")
    async with bridge.connect(cred, bridge.LOOPBACK) as call:
        assert await call("profile_status", {}) == {
            "identity": "cloud_agent17",
            "profile": "developer",
        }
    authenticated = [r for r in requests if "authorization" in r.headers]
    assert authenticated
    assert all(str(r.url) == bridge.LOOPBACK for r in authenticated)
    assert all(r.headers["authorization"] == "Bearer " + cred.access_token for r in authenticated)
    assert all("authorization" not in r.headers for r in requests[:2])


@pytest.mark.parametrize("field", ["body", "to_agent", "conversation_id", "reply_to"])
def test_secret_shaped_message_fields_never_persist(state, field):
    request = {**MESSAGE, field: "gh" + "p_" + "x" * 40}
    with state.locked(), pytest.raises(bridge.BridgeError, match="sanitize"):
        bridge.stage_message(state, "secret-field", request)
    assert state.state["messages"] == {}
    assert not state.path.exists()


@pytest.mark.parametrize(
    "paths",
    [
        (),
        ("../guide.md",),
        ("/Wiki/guide.md",),
        ("Wiki/../guide.md",),
        ("Wiki/guide.md", "Wiki/guide.md"),
        tuple(f"Wiki/note-{i}.md" for i in range(17)),
    ],
)
async def test_invalid_onboarding_sources_fail_before_hub_reads(state, paths):
    hub = Hub()
    with state.locked(), pytest.raises(bridge.BridgeError):
        await bridge.snapshot(hub, state, onboarding=True, vault_sources=paths)
    assert hub.calls == []
    assert not state.path.exists()


async def test_pending_packet_cannot_change_explicit_sources(state):
    hub = Hub()
    with state.locked():
        packet = await bridge.snapshot(
            hub, state, onboarding=True, vault_sources=("Wiki/guide.md",)
        )
        before = len(hub.calls)
        with pytest.raises(bridge.BridgeError):
            await bridge.snapshot(hub, state, onboarding=True, vault_sources=("Wiki/registry.md",))
        assert state.state["pending"] == packet
        assert not any(name == "vault_read" for name, _ in hub.calls[before:])


async def test_downgraded_profile_cannot_replay_or_send(state):
    hub = Hub()
    with state.locked():
        await bridge.snapshot(hub, state)
        hub.profile = "cloud-vault-steward"
        with pytest.raises(bridge.BridgeError):
            await bridge.snapshot(hub, state)
        with pytest.raises(bridge.BridgeError):
            await bridge.send_message(hub, state, "denied", MESSAGE)
    assert not any(name == "message_send" for name, _ in hub.calls)
