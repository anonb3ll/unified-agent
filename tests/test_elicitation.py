"""MCP 2.x elicitation as an additional approval surface (task #822 / #829 / #832).

Two layers:

* adapter tests drive ``elicitation.guarded_call`` with a stubbed MCP client
  transport (a fake Context carrying ``input_responses`` / ``request_state``),
  asserting the queue row and the hash-chained event log directly — the SQLite
  schema and the event chain are real, only the client is faked.
* end-to-end tests drive the registered ``run_cmd`` tool through an in-process
  ``mcp.client.Client`` with a real ``elicitation_callback``, asserting the
  observable round-trip: a form is raised, approving completes the action, a
  denial blocks it with the reason persisted, and a decline (or a thin client
  with no callback) leaves the row pending — never auto-approved.
"""

import json

import pytest
from mcp.client import Client, InputRequiredRoundsExceededError
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequestURLParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    UrlElicitationCapability,
)

from unified_agent import agent_access, config, elicitation, events, profiles, storage
from unified_agent.server import mcp
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_public_origin(monkeypatch):
    """Without a public origin there is no URL mode, so form tests stay on the form."""
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    monkeypatch.delenv("UA_MCP_OAUTH_ISSUER", raising=False)


# --------------------------------------------------------------------------- #
# Stubbed MCP client transport for the adapter tests                          #
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self, supports: bool, session_id=None, client_capabilities=None):
        self._supports = supports
        self.session_id = session_id
        # None keeps the pre-#2256 fakes on the check_client_capability path.
        self.client_capabilities = client_capabilities

    def check_client_capability(self, _capability) -> bool:
        return self._supports


class _FakeContext:
    """The narrow slice of mcpserver.Context the adapter touches."""

    def __init__(
        self,
        *,
        supports=True,
        input_responses=None,
        request_state=None,
        session_id=None,
        client_capabilities=None,
        protocol_version=None,
    ):
        self.session = _FakeSession(
            supports, session_id=session_id, client_capabilities=client_capabilities
        )
        self.input_responses = input_responses
        self.request_state = request_state
        self.protocol_version = protocol_version


def _reply(
    approval_id: int,
    challenge: str,
    *,
    action="accept",
    approve=True,
    reason="",
    session_id=None,
):
    """A fake round-two Context carrying the client's decision and echoed nonce."""
    return _FakeContext(
        input_responses={
            "approval": ElicitResult(action=action, content={"approve": approve, "reason": reason})
        },
        request_state=json.dumps({"approval_id": approval_id, "challenge": challenge}),
        session_id=session_id,
    )


def _gated_tool(*, command="echo hi"):
    """A stand-in tool body that trips the approval gate exactly like a real one."""
    check_approval_gate("run_cmd", {"command": command}, requested_by="Desktop_agent3")
    return {"ran": True}


def _queue_row(monkeypatch, requested_by="Desktop_agent3", command="echo hi") -> int:
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": command}, requested_by=requested_by)
    pending = [r for r in approval_pending()["pending"] if r["payload"].get("command") == command]
    return pending[0]["id"]


def _status(approval_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[
            "status"
        ]
    finally:
        conn.close()


def _challenge_row(approval_id: int):
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT nonce_hash, consumed_at, session_key "
            "FROM elicitation_challenges WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
    finally:
        conn.close()


def _elicitation_events() -> list[dict]:
    return events.query_events(channels=[elicitation.ELICITATION_CHANNEL], limit=50)


def _actions() -> list[str]:
    return [e["payload"]["action"] for e in _elicitation_events()]


def _assert_nonce_not_logged(nonce: str) -> None:
    blob = json.dumps(_elicitation_events())
    assert nonce not in blob


async def _run(ctx, command="echo hi", identity="Desktop_Person"):
    # The queued row names an agent. The accept round is a different human,
    # which is the only identity approval_resolve will record.
    token = config.agent_identity_var.set(identity)
    try:
        return await elicitation.guarded_call(
            _gated_tool, {"command": command}, tool_name="run_cmd", elicit_ctx=ctx
        )
    finally:
        config.agent_identity_var.reset(token)


async def _issue(monkeypatch, **queue_kwargs):
    """Round one: park a row, surface the form, return (approval_id, challenge)."""
    session_id = queue_kwargs.pop("session_id", None)
    command = queue_kwargs.get("command", "echo hi")
    approval_id = _queue_row(monkeypatch, **queue_kwargs)
    result = await _run(
        _FakeContext(supports=True, session_id=session_id),
        command=command,
    )
    assert isinstance(result, InputRequiredResult)
    state = json.loads(result.request_state)
    assert state["approval_id"] == approval_id
    challenge = state["challenge"]
    assert isinstance(challenge, str) and len(challenge) >= 32
    return approval_id, challenge, result


# --------------------------------------------------------------------------- #
# Round one: raise the form, or fall back for a thin client                   #
# --------------------------------------------------------------------------- #


class TestElicitationRequest:
    @pytest.mark.anyio
    async def test_capable_client_gets_a_form_request(self, monkeypatch, isolated_env):
        approval_id, challenge, result = await _issue(monkeypatch)

        req = result.input_requests["approval"]
        assert req.params.message.startswith(
            "Desktop_agent3 is requesting approval to run 'run_cmd'"
        )
        assert set(req.params.requested_schema["properties"]) == {"approve", "reason"}
        assert json.loads(result.request_state) == {
            "approval_id": approval_id,
            "challenge": challenge,
        }
        assert _status(approval_id) == "pending", "the form does not resolve the row"
        row = _challenge_row(approval_id)
        assert row is not None and row["consumed_at"] is None
        assert _actions() == ["requested", "challenge_issued"]
        _assert_nonce_not_logged(challenge)

    @pytest.mark.anyio
    async def test_thin_client_reraises_pending_unchanged(self, monkeypatch, isolated_env):
        approval_id = _queue_row(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_FakeContext(supports=False))

        assert _status(approval_id) == "pending"
        assert _elicitation_events() == [], "no elicitation without a capable client"
        assert _challenge_row(approval_id) is None, "thin clients never mint a challenge"

    @pytest.mark.anyio
    async def test_no_context_reraises_pending(self, monkeypatch, isolated_env):
        _queue_row(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(None)

    @pytest.mark.anyio
    async def test_flag_off_keeps_elicitation_dormant(self, monkeypatch, isolated_env):
        approval_id = _queue_row(monkeypatch)
        monkeypatch.delenv("UA_ELICITATION_APPROVALS", raising=False)  # operator did not opt in
        with pytest.raises(ApprovalPendingError):
            await _run(_FakeContext(supports=True))

        assert _status(approval_id) == "pending"
        assert _elicitation_events() == []
        assert _challenge_row(approval_id) is None

    @pytest.mark.anyio
    async def test_profile_without_approval_resolve_cannot_elicit(self, monkeypatch, isolated_env):
        """An autonomous agent whose profile denies approval_resolve can't self-approve."""
        approval_id = _queue_row(monkeypatch)
        monkeypatch.setattr(
            "unified_agent.profiles.tool_is_allowed",
            lambda name: name != "approval_resolve",
        )
        with pytest.raises(ApprovalPendingError):
            await _run(_FakeContext(supports=True))
        assert _status(approval_id) == "pending"

        # ...and a fabricated round-two response still cannot resolve the row.
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, "invented-nonce", approve=True))
        assert _status(approval_id) == "pending", "profile denial blocks the elicitation resolve"

    @pytest.mark.anyio
    async def test_challenge_not_issued_without_actor(self, monkeypatch, isolated_env):
        """UA #849: elicitation must not advertise an unattributed agent."""
        approval_id = _queue_row(monkeypatch, requested_by=None)
        monkeypatch.setattr(elicitation.config, "verified_agent_identity", lambda: None)
        with pytest.raises(ApprovalPendingError):
            await _run(_FakeContext(supports=True))
        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id) is None
        assert _elicitation_events() == []


# --------------------------------------------------------------------------- #
# Round two: apply the human's decision, retry the tool                       #
# --------------------------------------------------------------------------- #


class TestElicitationDecisions:
    @pytest.mark.anyio
    async def test_accept_approve_resolves_row_and_runs_the_tool(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch, requested_by="Desktop_Agent2")
        result = await _run(_reply(approval_id, challenge, approve=True, reason="ok by me"))

        assert result == {"ran": True}
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, reason FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "consumed", "the retried call consumed its one-shot pass"
        assert _challenge_row(approval_id)["consumed_at"]
        assert _actions() == [
            "requested",
            "challenge_issued",
            "challenge_consumed",
            "approved",
        ]
        _assert_nonce_not_logged(challenge)

    @pytest.mark.anyio
    async def test_accept_deny_blocks_with_reason_persisted(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch)
        with pytest.raises(Exception, match="DENIED"):
            await _run(_reply(approval_id, challenge, approve=False, reason="prod is red"))

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, reason FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "denied"
        assert row["reason"] == "prod is red"
        assert _challenge_row(approval_id)["consumed_at"]
        assert _actions()[-2:] == ["challenge_consumed", "denied"]
        _assert_nonce_not_logged(challenge)

    @pytest.mark.anyio
    async def test_decline_leaves_the_row_pending(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, action="decline"))

        assert _status(approval_id) == "pending", "a decline must never auto-approve"
        assert _challenge_row(approval_id)["consumed_at"] is None, "decline does not consume"
        assert _actions()[-1] == "decline"

    @pytest.mark.anyio
    async def test_cancel_leaves_the_row_pending(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, action="cancel"))

        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None


# --------------------------------------------------------------------------- #
# Task #832: server-issued one-time challenge                                 #
# --------------------------------------------------------------------------- #


class TestElicitationChallenge:
    @pytest.mark.anyio
    async def test_agent_access_denial_leaves_approval_and_challenge_pending(
        self, monkeypatch, isolated_env
    ):
        approval_id, challenge, _ = await _issue(monkeypatch)
        monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")

        with pytest.raises(agent_access.AgentAccessDenied, match="agent access is disabled"):
            await _run(_reply(approval_id, challenge, approve=True))

        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None

    @pytest.mark.anyio
    async def test_fabricated_response_leaves_row_pending(self, monkeypatch, isolated_env):
        approval_id = _queue_row(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, "deadbeef-not-issued", approve=True))

        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id) is None
        assert "approved" not in _actions()
        assert "challenge_rejected" in _actions()

    @pytest.mark.anyio
    async def test_client_chosen_request_state_is_not_authoritative(
        self, monkeypatch, isolated_env
    ):
        """Echoing only approval_id — the pre-#832 contract — cannot resolve."""
        approval_id = _queue_row(monkeypatch)
        ctx = _FakeContext(
            input_responses={
                "approval": ElicitResult(action="accept", content={"approve": True, "reason": ""})
            },
            request_state=json.dumps({"approval_id": approval_id}),
        )
        with pytest.raises(ApprovalPendingError):
            await _run(ctx)

        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id) is None

    @pytest.mark.anyio
    async def test_replay_is_rejected_after_one_consumption(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch)
        assert await _run(_reply(approval_id, challenge, approve=True)) == {"ran": True}
        assert _status(approval_id) == "consumed"
        consumed_at = _challenge_row(approval_id)["consumed_at"]

        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, approve=True))

        assert _status(approval_id) == "consumed", "replay must not mutate the decision"
        assert _challenge_row(approval_id)["consumed_at"] == consumed_at
        assert _actions().count("challenge_consumed") == 1
        assert "challenge_rejected" in _actions()
        _assert_nonce_not_logged(challenge)

    @pytest.mark.anyio
    async def test_cross_row_reuse_cannot_resolve_the_other_row(self, monkeypatch, isolated_env):
        id_a, challenge_a, _ = await _issue(monkeypatch, command="echo a")
        id_b, challenge_b, _ = await _issue(monkeypatch, command="echo b")
        assert id_a != id_b
        assert challenge_a != challenge_b

        with pytest.raises(ApprovalPendingError):
            await _run(_reply(id_b, challenge_a, approve=True), command="echo b")

        assert _status(id_a) == "pending"
        assert _status(id_b) == "pending"
        assert _challenge_row(id_a)["consumed_at"] is None
        assert _challenge_row(id_b)["consumed_at"] is None

        # The rightful nonce still works for its own row.
        result = await _run(_reply(id_a, challenge_a, approve=True), command="echo a")
        assert result == {"ran": True}
        assert _status(id_a) == "consumed"
        assert _status(id_b) == "pending"
        _assert_nonce_not_logged(challenge_a)
        _assert_nonce_not_logged(challenge_b)

    @pytest.mark.anyio
    async def test_challenge_is_consumed_exactly_once(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch)
        assert _challenge_row(approval_id)["consumed_at"] is None

        await _run(_reply(approval_id, challenge, approve=True))

        row = _challenge_row(approval_id)
        assert row["consumed_at"]
        assert _actions().count("challenge_issued") == 1
        assert _actions().count("challenge_consumed") == 1
        _assert_nonce_not_logged(challenge)

    @pytest.mark.anyio
    async def test_decline_then_accept_can_still_use_the_nonce(self, monkeypatch, isolated_env):
        """Decline is not a resolving call, so the nonce stays live."""
        approval_id, challenge, _ = await _issue(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, action="decline"))
        assert _challenge_row(approval_id)["consumed_at"] is None

        result = await _run(_reply(approval_id, challenge, approve=True))
        assert result == {"ran": True}
        assert _status(approval_id) == "consumed"

    @pytest.mark.anyio
    async def test_reissue_invalidates_the_previous_nonce(self, monkeypatch, isolated_env):
        approval_id, old_challenge, _ = await _issue(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, old_challenge, action="decline"))

        fresh = await _run(_FakeContext(supports=True))
        new_challenge = json.loads(fresh.request_state)["challenge"]
        assert new_challenge != old_challenge

        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, old_challenge, approve=True))
        assert _status(approval_id) == "pending"

        result = await _run(_reply(approval_id, new_challenge, approve=True))
        assert result == {"ran": True}

    @pytest.mark.anyio
    async def test_session_mismatch_leaves_row_pending(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch, session_id="sess-a")
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, approve=True, session_id="sess-b"))

        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None
        assert "challenge_rejected" in _actions()

        result = await _run(_reply(approval_id, challenge, approve=True, session_id="sess-a"))
        assert result == {"ran": True}


# --------------------------------------------------------------------------- #
# Task #2256: URL-mode approval, only where the client negotiates it          #
# --------------------------------------------------------------------------- #

_ORIGIN = "https://hub.example.test"
_MODERN = "2026-07-28"
_URL_ONLY = ClientCapabilities(elicitation=ElicitationCapability(url=UrlElicitationCapability()))
_FORM_ONLY = ClientCapabilities(elicitation=ElicitationCapability())  # bare {} == form
_BOTH = ClientCapabilities(
    elicitation=ElicitationCapability(
        form=FormElicitationCapability(), url=UrlElicitationCapability()
    )
)


@pytest.fixture
def url_hub(monkeypatch):
    """A hub with a public Control Center origin and a short server-side wait."""
    monkeypatch.setenv("UA_PUBLIC_URL", _ORIGIN)
    monkeypatch.setattr(elicitation, "_URL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(elicitation, "_URL_POLL_SECONDS", 0.01)


@pytest.fixture
def http_session():
    """Bind an MCP session id the way the HTTP middleware does (server.py).

    The in-memory SDK transport, like stdio, carries no session id, and URL
    mode is refused without one (PR #1341 review, P2).
    """
    token = config.mcp_session_id_var.set("sess-http")
    try:
        yield "sess-http"
    finally:
        config.mcp_session_id_var.reset(token)


def _url_ctx(*, caps=_URL_ONLY, session_id="sess-url", protocol=_MODERN, **kwargs):
    return _FakeContext(
        client_capabilities=caps, session_id=session_id, protocol_version=protocol, **kwargs
    )


def _url_reply(state: str, *, action="accept", content=None, **kwargs):
    """Round two of a URL elicitation: accept is consent to open the page, nothing more."""
    return _url_ctx(
        input_responses={"approval": ElicitResult(action=action, content=content)},
        request_state=state,
        **kwargs,
    )


class _Counter:
    def __init__(self):
        self.calls = 0

    def __call__(self, *, command="echo hi"):
        self.calls += 1
        return _gated_tool(command=command)


async def _run_tool(tool, ctx, *, command="echo hi", identity="Desktop_Person"):
    token = config.agent_identity_var.set(identity)
    try:
        return await elicitation.guarded_call(
            tool, {"command": command}, tool_name="run_cmd", elicit_ctx=ctx
        )
    finally:
        config.agent_identity_var.reset(token)


async def _issue_url(monkeypatch, *, identity="Desktop_Person", **ctx_kwargs):
    approval_id = _queue_row(monkeypatch)
    result = await _run(_url_ctx(**ctx_kwargs), identity=identity)
    assert isinstance(result, InputRequiredResult)
    state = json.loads(result.request_state)
    assert state["approval_id"] == approval_id and state["mode"] == "url"
    return approval_id, state["challenge"], result


def _human_resolves(approval_id: int, decision: str, reason: str | None = None) -> None:
    """The Control Center path: an authenticated human resolves the row server-side."""
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(approval_id, decision, reason=reason)
    finally:
        config.agent_identity_var.reset(token)


def _assert_not_leaked(*secrets_: str) -> None:
    blob = json.dumps(_elicitation_events())
    for value in secrets_:
        assert value not in blob


class TestUrlModeApproval:
    @pytest.mark.anyio
    async def test_url_client_is_offered_the_control_center_page(
        self, monkeypatch, isolated_env, url_hub
    ):
        approval_id, challenge, result = await _issue_url(monkeypatch)

        params = result.input_requests["approval"].params
        assert isinstance(params, ElicitRequestURLParams)
        assert params.url == f"{_ORIGIN}/ui/inbox?kind=approval&item={approval_id}"
        assert challenge not in params.url, "no preauthenticated URL"
        assert params.url not in params.message
        assert "does not approve" in params.message
        assert _status(approval_id) == "pending"
        row = _challenge_row(approval_id)
        assert row["consumed_at"] is None and row["session_key"].startswith("url:")
        assert _actions() == ["requested", "challenge_issued"]
        assert all(e["payload"].get("mode") == "url" for e in _elicitation_events())
        _assert_not_leaked(challenge, params.url)

    @pytest.mark.anyio
    async def test_client_without_url_capability_keeps_the_form(
        self, monkeypatch, isolated_env, url_hub
    ):
        _queue_row(monkeypatch)
        result = await _run(_url_ctx(caps=_FORM_ONLY))
        assert result.input_requests["approval"].params.mode == "form"
        assert "mode" not in json.loads(result.request_state)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("origin", "protocol", "session_id"),
        [
            (None, _MODERN, "sess-url"),  # no public Control Center origin to link to
            ("https://operator@hub.example.test", _MODERN, "sess-url"),  # userinfo in origin
            (_ORIGIN, "2025-11-25", "sess-url"),  # no InputRequiredResult on this protocol
            (_ORIGIN, _MODERN, None),  # no MCP session to bind the challenge to (#1341 P2)
        ],
    )
    async def test_url_only_client_without_a_usable_url_path_parks_in_the_queue(
        self, monkeypatch, isolated_env, url_hub, origin, protocol, session_id
    ):
        if origin is None:
            monkeypatch.delenv("UA_PUBLIC_URL")
        else:
            monkeypatch.setenv("UA_PUBLIC_URL", origin)
        approval_id = _queue_row(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_url_ctx(protocol=protocol, session_id=session_id))
        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id) is None
        assert _elicitation_events() == []

    @pytest.mark.anyio
    async def test_flag_off_keeps_url_mode_dormant(self, monkeypatch, isolated_env, url_hub):
        approval_id = _queue_row(monkeypatch)
        monkeypatch.delenv("UA_ELICITATION_APPROVALS")
        with pytest.raises(ApprovalPendingError):
            await _run(_url_ctx(caps=_BOTH))
        assert _challenge_row(approval_id) is None
        assert _elicitation_events() == []

    @pytest.mark.anyio
    async def test_url_accept_alone_never_authorizes(self, monkeypatch, isolated_env, url_hub):
        approval_id, challenge, first = await _issue_url(monkeypatch)
        tool = _Counter()
        # A client that also stuffs form-style content into the accept gets nothing for it.
        again = await _run_tool(
            tool, _url_reply(first.request_state, content={"approve": True, "reason": "x"})
        )

        assert isinstance(again, InputRequiredResult)
        assert not again.input_requests, "no second URL prompt: the client just re-polls"
        assert json.loads(again.request_state) == json.loads(first.request_state)
        assert tool.calls == 0
        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None
        assert "approved" not in _actions()

    @pytest.mark.anyio
    async def test_human_approval_in_the_control_center_runs_the_tool_once(
        self, monkeypatch, isolated_env, url_hub
    ):
        approval_id, challenge, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "approve")
        tool = _Counter()

        assert await _run_tool(tool, _url_reply(first.request_state)) == {"ran": True}
        assert tool.calls == 1
        assert _status(approval_id) == "consumed"
        assert _challenge_row(approval_id)["consumed_at"]
        assert _actions() == ["requested", "challenge_issued", "challenge_consumed", "url_resolved"]
        assert _elicitation_events()[-1]["payload"]["status"] == "approved"

        # One use: replaying the same state is refused and runs nothing.
        with pytest.raises(ApprovalPendingError) as replay:
            await _run_tool(tool, _url_reply(first.request_state))
        assert tool.calls == 1
        assert _status(approval_id) == "consumed"
        assert _elicitation_events()[-1]["payload"]["reason"] == "replay"
        assert challenge not in str(replay.value) and _ORIGIN not in str(replay.value)
        _assert_not_leaked(challenge, _ORIGIN)

    @pytest.mark.anyio
    async def test_state_only_continuation_waits_for_the_human(
        self, monkeypatch, isolated_env, url_hub
    ):
        """After consent the client re-polls with request_state alone (no responses)."""
        approval_id, _, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "approve")
        result = await _run_tool(_Counter(), _url_ctx(request_state=first.request_state))
        assert result == {"ran": True}
        assert _status(approval_id) == "consumed"

    @pytest.mark.anyio
    async def test_human_denial_blocks_with_the_reason(self, monkeypatch, isolated_env, url_hub):
        approval_id, _, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "deny", reason="not during the freeze")
        with pytest.raises(ToolError, match="not during the freeze"):
            await _run_tool(_Counter(), _url_reply(first.request_state))
        assert _status(approval_id) == "denied"
        assert _challenge_row(approval_id)["consumed_at"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("action", ["decline", "cancel"])
    async def test_decline_or_cancel_leaves_row_and_challenge_pending(
        self, monkeypatch, isolated_env, url_hub, action
    ):
        approval_id, challenge, first = await _issue_url(monkeypatch)
        tool = _Counter()
        with pytest.raises(ApprovalPendingError) as exc:
            await _run_tool(tool, _url_reply(first.request_state, action=action))
        assert tool.calls == 0
        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None
        assert _actions()[-1] == action
        assert challenge not in str(exc.value) and _ORIGIN not in str(exc.value)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "change",
        [
            {"session_id": "sess-other"},  # MCP session
            {"session_id": None},  # no session: refused before any binding check (#1341 P2)
            {"identity": "Desktop_Agent2"},  # principal
            {"command": "echo other"},  # payload / arguments
        ],
    )
    async def test_continuation_is_bound_to_session_principal_and_payload(
        self, monkeypatch, isolated_env, url_hub, change
    ):
        approval_id, _, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "approve")
        tool = _Counter()
        ctx_kwargs = {"session_id": change.get("session_id", "sess-url")}
        with pytest.raises(ApprovalPendingError):
            await _run_tool(
                tool,
                _url_reply(first.request_state, **ctx_kwargs),
                command=change.get("command", "echo hi"),
                identity=change.get("identity", "Desktop_Person"),
            )
        assert tool.calls == 0
        assert _status(approval_id) == "approved", "the human's pass is untouched"
        assert _challenge_row(approval_id)["consumed_at"] is None
        if change.get("session_id", "sess-url") is not None:
            assert _elicitation_events()[-1]["payload"]["reason"] == "binding"

        # The rightful continuation still completes.
        assert await _run_tool(tool, _url_reply(first.request_state)) == {"ran": True}

    @pytest.mark.anyio
    async def test_expired_challenge_is_refused(self, monkeypatch, isolated_env, url_hub):
        approval_id, _, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "approve")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE elicitation_challenges SET issued_at=? WHERE approval_id=?",
                ("2000-01-01T00:00:00+00:00", approval_id),
            )
            conn.commit()
        finally:
            conn.close()
        tool = _Counter()
        with pytest.raises(ApprovalPendingError):
            await _run_tool(tool, _url_reply(first.request_state))
        assert tool.calls == 0
        assert _status(approval_id) == "approved"
        assert _elicitation_events()[-1]["payload"]["reason"] == "expired"

    @pytest.mark.anyio
    async def test_url_challenge_cannot_drive_a_form_decision(
        self, monkeypatch, isolated_env, url_hub
    ):
        """Cross-mode: a URL nonce echoed as a form approve resolves nothing."""
        approval_id, challenge, _ = await _issue_url(monkeypatch)
        with pytest.raises(ApprovalPendingError):
            await _run(_reply(approval_id, challenge, approve=True, session_id="sess-url"))
        assert _status(approval_id) == "pending"
        assert _challenge_row(approval_id)["consumed_at"] is None

    @pytest.mark.anyio
    async def test_developer_session_gets_the_url_but_cannot_approve_itself(
        self, monkeypatch, isolated_env, url_hub
    ):
        assert not profiles.tool_is_allowed_for("developer", "approval_resolve")
        monkeypatch.setattr(
            "unified_agent.profiles.tool_is_allowed", lambda name: name != "approval_resolve"
        )
        approval_id, _, first = await _issue_url(monkeypatch, identity="Desktop_agent3")

        pending = await _run_tool(
            _Counter(), _url_reply(first.request_state), identity="Desktop_agent3"
        )
        assert isinstance(pending, InputRequiredResult)
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            with pytest.raises(ToolError, match="separation of duties"):
                approval_resolve(approval_id, "approve")
        finally:
            config.agent_identity_var.reset(token)
        assert _status(approval_id) == "pending"

    @pytest.mark.anyio
    async def test_continuation_after_flag_is_turned_off_stays_queued(
        self, monkeypatch, isolated_env, url_hub
    ):
        approval_id, _, first = await _issue_url(monkeypatch)
        _human_resolves(approval_id, "approve")
        monkeypatch.delenv("UA_ELICITATION_APPROVALS")
        tool = _Counter()
        with pytest.raises(ApprovalPendingError):
            await _run_tool(tool, _url_reply(first.request_state))
        assert tool.calls == 0
        assert _status(approval_id) == "approved", "legacy retry still consumes the pass"
        assert await _run_tool(tool, None) == {"ran": True}


# --------------------------------------------------------------------------- #
# Event log: hash-chained round-trip                                          #
# --------------------------------------------------------------------------- #


class TestElicitationEventLog:
    @pytest.mark.anyio
    async def test_full_round_trip_is_hash_chained(self, monkeypatch, isolated_env):
        approval_id, challenge, _ = await _issue(monkeypatch, requested_by="Desktop_Agent2")
        await _run(_reply(approval_id, challenge, approve=True))

        rows = _elicitation_events()
        assert [r["payload"]["action"] for r in rows] == [
            "requested",
            "challenge_issued",
            "challenge_consumed",
            "approved",
        ]
        for r in rows:
            assert r["payload"]["approval_id"] == approval_id
            assert r["payload"]["tool"] == "run_cmd"
            assert r["originator"] == "Desktop_Agent2"
            assert r["entry_hash"], "elicitation events join the hash chain"
            assert challenge not in json.dumps(r["payload"])

        verdict = events.verify_event_chain()
        assert verdict["status"] == "valid" and verdict["verified"] is True


# --------------------------------------------------------------------------- #
# End-to-end through the registered MCP tool + a real elicitation callback    #
# --------------------------------------------------------------------------- #


def _callback(action="accept", approve=True, reason=""):
    async def cb(_context, _params):
        if action != "accept":
            return ElicitResult(action=action)
        return ElicitResult(action="accept", content={"approve": approve, "reason": reason})

    return cb


@pytest.mark.anyio
async def test_thin_client_gets_the_queue_fallback(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    async with Client(mcp) as client:  # no elicitation_callback -> thin client
        res = await client.call_tool("run_cmd", {"command": "echo hi"})
    assert res.is_error
    assert "queued" in res.content[0].text
    assert approval_pending()["pending"][0]["tool"] == "run_cmd"
    pending_id = approval_pending()["pending"][0]["id"]
    assert _challenge_row(pending_id) is None, "thin clients never mint a challenge"


@pytest.mark.anyio
async def test_rich_client_approve_completes_the_action(monkeypatch):
    """The requesting agent cannot accept its own elicitation. A different human can."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(mcp, elicitation_callback=_callback(approve=True)) as client:
            refused = await client.call_tool("run_cmd", {"command": "echo elicited"})
            assert refused.is_error
            assert "separation of duties" in refused.content[0].text
            approval_id = approval_pending()["pending"][0]["id"]
            human = config.agent_identity_var.set("Desktop_Person")
            try:
                approval_resolve(approval_id, "approve")
            finally:
                config.agent_identity_var.reset(human)
            res = await client.call_tool("run_cmd", {"command": "echo elicited"})
    finally:
        config.agent_identity_var.reset(token)

    assert not res.is_error, res.content[0].text if res.content else res
    assert "elicited" in res.content[0].text
    assert approval_pending()["pending"] == []
    conn = storage.connect()
    try:
        statuses = [r["status"] for r in conn.execute("SELECT status FROM approvals").fetchall()]
        challenges = conn.execute("SELECT consumed_at FROM elicitation_challenges").fetchall()
    finally:
        conn.close()
    assert statuses == ["consumed"]
    assert len(challenges) == 1 and challenges[0]["consumed_at"]
    assert events.verify_event_chain()["status"] == "valid"
    assert "challenge_issued" in _actions()
    assert "challenge_consumed" in _actions()


@pytest.mark.anyio
async def test_rich_client_deny_blocks_with_reason(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(
            mcp, elicitation_callback=_callback(approve=False, reason="not now")
        ) as client:
            res = await client.call_tool("run_cmd", {"command": "echo nope"})
    finally:
        config.agent_identity_var.reset(token)

    assert res.is_error
    assert "DENIED" in res.content[0].text and "not now" in res.content[0].text
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, reason FROM approvals").fetchone()
    finally:
        conn.close()
    assert row["status"] == "denied" and row["reason"] == "not now"


@pytest.mark.anyio
async def test_rich_client_decline_leaves_row_pending(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(mcp, elicitation_callback=_callback(action="decline")) as client:
            res = await client.call_tool("run_cmd", {"command": "echo maybe"})
    finally:
        config.agent_identity_var.reset(token)

    assert res.is_error
    pending = approval_pending()["pending"]
    assert len(pending) == 1 and pending[0]["tool"] == "run_cmd"
    assert _challenge_row(pending[0]["id"])["consumed_at"] is None


@pytest.mark.anyio
async def test_ui_path_still_resolves_the_same_row_after_a_decline(monkeypatch):
    """Back-compat: the /ui approval queue resolves the identical row."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(mcp, elicitation_callback=_callback(action="decline")) as client:
            first = await client.call_tool("run_cmd", {"command": "echo backcompat"})
            assert first.is_error

            approval_id = approval_pending()["pending"][0]["id"]
            human = config.agent_identity_var.set("Desktop_Person")
            try:
                approval_resolve(approval_id, "approve")
            finally:
                config.agent_identity_var.reset(human)

            second = await client.call_tool("run_cmd", {"command": "echo backcompat"})
    finally:
        config.agent_identity_var.reset(token)

    assert not second.is_error
    assert json.loads(second.content[0].text)["exit_code"] == 0


def _url_callback(seen: list, *, approve_as_human: bool):
    """A URL-capable client. Opening the page is simulated by a human resolving the row."""

    async def cb(_context, params):
        seen.append(params)
        if params.mode != "url":
            return ElicitResult(action="decline")
        if approve_as_human:
            _human_resolves(int(params.url.rsplit("=", 1)[1]), "approve")
        return ElicitResult(action="accept")

    return cb


@pytest.mark.anyio
async def test_url_client_completes_without_a_model_retry(monkeypatch, url_hub, http_session):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    seen: list = []
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(
            mcp, elicitation_callback=_url_callback(seen, approve_as_human=True)
        ) as c:
            res = await c.call_tool("run_cmd", {"command": "echo via-url"})
    finally:
        config.agent_identity_var.reset(token)

    assert not res.is_error, res.content[0].text if res.content else res
    assert "via-url" in res.content[0].text
    assert len(seen) == 1 and seen[0].mode == "url"
    assert seen[0].url.startswith(f"{_ORIGIN}/ui/inbox?kind=approval&item=")
    conn = storage.connect()
    try:
        statuses = [r["status"] for r in conn.execute("SELECT status FROM approvals").fetchall()]
    finally:
        conn.close()
    assert statuses == ["consumed"]
    assert "url_resolved" in _actions()
    _assert_not_leaked(seen[0].url)


@pytest.mark.anyio
async def test_url_client_without_a_human_parks_and_legacy_retry_still_works(
    monkeypatch, url_hub, http_session
):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    seen: list = []
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(
            mcp,
            elicitation_callback=_url_callback(seen, approve_as_human=False),
            input_required_max_rounds=3,
        ) as c:
            with pytest.raises(InputRequiredRoundsExceededError):
                await c.call_tool("run_cmd", {"command": "echo later"})
            assert len(seen) == 1, "the URL is offered once; later legs only re-poll"
            approval_id = approval_pending()["pending"][0]["id"]
            _human_resolves(approval_id, "approve")
        async with Client(mcp) as thin:  # the legacy park/retry path
            res = await thin.call_tool("run_cmd", {"command": "echo later"})
    finally:
        config.agent_identity_var.reset(token)

    assert not res.is_error, res.content[0].text if res.content else res
    assert approval_pending()["pending"] == []


@pytest.mark.anyio
async def test_url_mode_is_never_offered_without_a_transport_session(monkeypatch, url_hub):
    # PR #1341 review, P2: with no session id every session of one caller would
    # share a binding, so the call stays in the legacy queue instead.
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_ELICITATION_APPROVALS", "1")
    seen: list = []
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        async with Client(
            mcp, elicitation_callback=_url_callback(seen, approve_as_human=True)
        ) as c:
            res = await c.call_tool("run_cmd", {"command": "echo no-session"})
    finally:
        config.agent_identity_var.reset(token)
    # The SDK test client declares form too, so it falls back to the form path.
    assert seen and all(request.mode != "url" for request in seen), res
    assert "url_resolved" not in _actions()
