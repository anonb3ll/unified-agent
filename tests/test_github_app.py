from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from io import BytesIO
from urllib.error import HTTPError

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.mcpserver.exceptions import ToolError
from review_evidence_fixture import review_evidence

from unified_agent import config, events, github_app, pr_watch
from unified_agent.audit import producer_sign
from unified_agent.github_app import (
    CHECK_RUN_NAME,
    GITHUB_APP_MANIFEST,
    TOOL_NAME,
    NormalizedPREvent,
    normalize_event,
    process_pr_event,
)
from unified_agent.policy import PolicyConfig, PolicyRule
from unified_agent.tools import approvals


@pytest.fixture(autouse=True)
def live_pr_at_the_event_head():
    """Every approval settle re-reads its PR (task #2234); here it is still at the head."""

    def reader(event):
        return pr_watch.PRSnapshot(
            number=event.number,
            title=event.title,
            body="",
            url=event.url,
            state="OPEN",
            head_sha=event.head_sha,
            head_branch=event.head_branch,
            base_branch=event.base_branch,
        )

    with github_app.live_pr(reader):
        yield


def make_pr(number: int, **overrides) -> dict:
    payload = {
        "number": number,
        "title": "Add governed release",
        "body": "agent-authored change",
        "url": f"https://github.com/example/repo/pull/{number}",
        "state": "OPEN",
        "headRefOid": "old-sha",
        "headRefName": "feature",
        "baseRefName": "main",
        "isDraft": True,
        "author": {"login": "Desktop_Agent2"},
        "labels": [{"name": "agent"}],
        "mergedAt": None,
        "closedAt": None,
        "reviews": [],
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    def __init__(self):
        self.created = []
        self.updated = []
        self.comments = []

    def create_check_run(self, **kwargs):
        self.created.append(kwargs)
        return "check-1"

    def update_check_run(self, **kwargs):
        self.updated.append(kwargs)

    def create_evidence_comment(self, **kwargs):
        self.comments.append(kwargs)
        return "comment-1"


class FakeUserTransport(FakeTransport, github_app.GitHubUserTokenTransport):
    """Record transport calls while exercising user-mode ordering rules."""


def approval_policy() -> PolicyConfig:
    return PolicyConfig(
        rules=[
            PolicyRule(
                name="agent-prs-need-human",
                tool=TOOL_NAME,
                type="tool",
                action="require_approval",
            )
        ]
    )


def review_event() -> NormalizedPREvent:
    return NormalizedPREvent(
        repo="example/repo",
        number=12,
        event_type="synchronize",
        title="review fixture",
        url="https://github.com/example/repo/pull/12",
        state="OPEN",
        head_sha="a" * 40,
        base_branch="main",
        head_branch="fixture",
        author_login="Desktop_Agent2",
        independent_review=review_evidence(repo="example/repo", number=12),
    )


def test_public_review_summary_omits_all_supplied_identity_and_model_fields():
    event = review_event()
    review = event.independent_review
    review["authors"][0].update(
        label="private-user@private-host.local",
        subject="node:private-user-host.local",
        provider="private-provider-host",
        model="private-provider/private-model-host",
        execution_ref="https://private-ref-host.local/private-user",
    )
    review["review_ref"] = "https://private-review-host.local/private-user"
    body = github_app._evidence_comment(
        event,
        {"id": 7, "resolved_by": "Operator", "payload": event.to_payload()},
        signed_event_id="fixture",
        signature="fixture",
        chain_head="fixture",
    )
    assert "private-" not in body
    assert "openai/gpt" not in body
    assert '"author_count":1' in body and "ua://approval/7" in body


def test_callback_cannot_change_review_evidence_after_native_approval_lookup(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        event = review_event()
        transport = FakeTransport()
        pending = process_pr_event(event, transport, policy_config=approval_policy())
        config.agent_identity_var.set("Operator")
        monkeypatch.setattr(
            producer_sign, "resolve_agent_bearer_token", lambda _label: "test-bearer"
        )
        approvals.approval_resolve(pending["approval_id"], "approve")

        def callback(_approval):
            event.independent_review["reviewer"]["subject"] = "t3:mutated-reviewer"
            return "fixture", "fixture", "fixture"

        monkeypatch.setattr(github_app, "_signed_evidence", callback)
        with pytest.raises(PermissionError, match="not bound to the native approval payload"):
            process_pr_event(
                event,
                transport,
                policy_config=approval_policy(),
                pending_check_run_id=pending["check_run_id"],
            )
        assert transport.comments == []
        assert not any(call.get("conclusion") == "success" for call in transport.updated)
    finally:
        config.agent_identity_var.reset(identity)


def normalized_event() -> NormalizedPREvent:
    previous = pr_watch.parse_prs([make_pr(12)])
    current = pr_watch.parse_prs(
        [
            make_pr(
                12,
                headRefOid="new-sha",
                isDraft=False,
                reviews=[
                    {
                        "author": {"login": "Operator"},
                        "state": "APPROVED",
                        "body": "ship",
                        "submittedAt": "2026-09-20T18:00:00Z",
                    }
                ],
            )
        ]
    )
    event = pr_watch.diff_events("example/repo", previous, current)[0]
    return normalize_event(event)


def normalized_events() -> list[NormalizedPREvent]:
    """Every event this diff yields, sharing one head_sha (like `normalized_event`)."""
    previous = pr_watch.parse_prs([make_pr(12)])
    current = pr_watch.parse_prs(
        [
            make_pr(
                12,
                headRefOid="new-sha",
                isDraft=False,
                reviews=[
                    {
                        "author": {"login": "Operator"},
                        "state": "APPROVED",
                        "body": "ship",
                        "submittedAt": "2026-09-20T18:00:00Z",
                    }
                ],
            )
        ]
    )
    return [
        normalize_event(event) for event in pr_watch.diff_events("example/repo", previous, current)
    ]


def test_poll_diff_normalizes_event_types_and_stable_ids():
    previous = pr_watch.parse_prs([make_pr(12)])
    current = pr_watch.parse_prs(
        [
            make_pr(
                12,
                headRefOid="new-sha",
                isDraft=False,
                reviews=[
                    {
                        "author": {"login": "Operator"},
                        "state": "APPROVED",
                        "body": "ship",
                        "submittedAt": "2026-09-20T18:00:00Z",
                    }
                ],
            )
        ]
    )
    events_out = [
        normalize_event(event) for event in pr_watch.diff_events("example/repo", previous, current)
    ]

    assert [event.event_type for event in events_out] == [
        "synchronize",
        "ready_for_review",
        "review_submitted",
    ]
    assert (
        events_out[0].event_id
        == normalize_event(pr_watch.diff_events("example/repo", previous, current)[0]).event_id
    )
    assert events_out[0].to_payload()["source"] == "pr_watch"


def test_disabled_by_default_does_not_call_github(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_APP_ENABLED", raising=False)
    transport = FakeTransport()

    result = process_pr_event(normalized_event(), transport, policy_config=approval_policy())

    assert result == {"status": "disabled", "event_id": normalized_event().event_id}
    assert transport.created == []


def test_policy_and_approval_gate_pending_check_then_signed_evidence(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeTransport()
        event = normalized_event()
        pending = process_pr_event(event, transport, policy_config=approval_policy())

        assert pending["status"] == "pending_approval"
        assert pending["approval_id"]
        assert transport.created[0]["name"] == CHECK_RUN_NAME
        assert transport.created[0]["status"] == "queued"

        config.agent_identity_var.set("Operator")
        monkeypatch.setattr(
            producer_sign, "resolve_agent_bearer_token", lambda _label: "test-bearer"
        )
        approvals.approval_resolve(pending["approval_id"], "approve")

        approved = process_pr_event(
            event,
            transport,
            policy_config=approval_policy(),
            pending_check_run_id=pending["check_run_id"],
        )

        assert approved["status"] == "approved"
        assert transport.updated[0]["conclusion"] == "success"
        comment = transport.comments[0]["body"]
        marker = re.search(r"<!-- unified-agent github-evidence (.+?) -->", comment)
        assert marker
        signed = json.loads(marker.group(1))
        assert signed["chain_head"]
        # Public comment: a role and a pseudonym, never a login or hostname (#1777).
        assert signed["human"] == "ua://human/operator"
        assert re.fullmatch(r"ua://device/[0-9a-f]{16}", signed["device"])
        assert config.current_source_host() not in comment
        assert signed["agent"] == "ua://agent/Desktop_Agent2"
        assert producer_sign.verify_producer_signature(signed) is True
        assert events.verify_event_chain()["status"] == "valid"
    finally:
        config.agent_identity_var.reset(identity)


def test_pending_check_run_must_match_event_and_approval(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeTransport()
        event = normalized_event()
        pending = process_pr_event(event, transport, policy_config=approval_policy())

        config.agent_identity_var.set("Operator")
        approvals.approval_resolve(pending["approval_id"], "approve")

        with pytest.raises(PermissionError, match="not bound"):
            process_pr_event(
                event,
                transport,
                policy_config=approval_policy(),
                pending_check_run_id="unrelated-check",
            )

        assert transport.updated == []
        assert transport.comments == []
    finally:
        config.agent_identity_var.reset(identity)


def test_denied_approval_completes_pending_status_as_failure(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeUserTransport()
        event = normalized_event()
        pending = process_pr_event(event, transport, policy_config=approval_policy())

        config.agent_identity_var.set("Operator")
        approvals.approval_resolve(pending["approval_id"], "deny", reason="not approved")

        denied = process_pr_event(
            event,
            transport,
            policy_config=approval_policy(),
            pending_check_run_id=pending["check_run_id"],
        )
        assert denied["status"] == "denied"
        assert transport.updated == [
            {
                "repo": event.repo,
                "check_run_id": pending["check_run_id"],
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        assert transport.comments == []
    finally:
        config.agent_identity_var.reset(identity)


def test_stale_event_on_same_sha_is_rejected_as_superseded(monkeypatch):
    """Devin review (PR #692): two events on one head_sha must not let the
    older one's terminal status/check overwrite the newer one's pending
    state -- see `_bound_pending_check_run_id`."""
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeUserTransport()
        older_event, newer_event, *_ = normalized_events()
        assert older_event.head_sha == newer_event.head_sha
        assert older_event.event_id != newer_event.event_id

        older_pending = process_pr_event(older_event, transport, policy_config=approval_policy())
        newer_pending = process_pr_event(newer_event, transport, policy_config=approval_policy())

        config.agent_identity_var.set("Operator")
        monkeypatch.setattr(
            producer_sign, "resolve_agent_bearer_token", lambda _label: "test-bearer"
        )
        approvals.approval_resolve(older_pending["approval_id"], "approve")
        approvals.approval_resolve(newer_pending["approval_id"], "approve")

        # The older event's approval is consumed, but its pending check/status
        # has since been superseded by the newer event's -- completing it now
        # would silently make the newer event's still-pending state look done.
        with pytest.raises(PermissionError, match="newer PR event"):
            process_pr_event(
                older_event,
                transport,
                policy_config=approval_policy(),
                pending_check_run_id=older_pending["check_run_id"],
            )
        assert transport.updated == []
        assert transport.comments == []

        # The newer (latest) event completes normally.
        approved = process_pr_event(
            newer_event,
            transport,
            policy_config=approval_policy(),
            pending_check_run_id=newer_pending["check_run_id"],
        )
        assert approved["status"] == "approved"
    finally:
        config.agent_identity_var.reset(identity)


def test_user_mode_requires_bound_pending_id(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeUserTransport()
        event = normalized_event()
        pending = process_pr_event(event, transport, policy_config=approval_policy())
        config.agent_identity_var.set("Operator")
        approvals.approval_resolve(pending["approval_id"], "approve")
        with pytest.raises(PermissionError, match="bound pending status id"):
            process_pr_event(event, transport, policy_config=approval_policy())
        assert transport.updated == []
    finally:
        config.agent_identity_var.reset(identity)


def test_user_status_lock_excludes_another_process(isolated_env):
    event = normalized_event()
    probe = (
        "import fcntl, sys; "
        "f = open(sys.argv[1], 'a+'); "
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with github_app._user_status_lock(event):
        lock_files = list((config.data_home() / "github-status-locks").glob("*.lock"))
        assert len(lock_files) == 1
        held = subprocess.run(
            [sys.executable, "-c", probe, str(lock_files[0])], capture_output=True
        )
        assert held.returncode != 0
    released = subprocess.run(
        [sys.executable, "-c", probe, str(lock_files[0])], capture_output=True
    )
    assert released.returncode == 0


def test_app_mode_older_check_can_complete_after_newer_event(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        transport = FakeTransport()
        older_event, newer_event, *_ = normalized_events()
        older_pending = process_pr_event(older_event, transport, policy_config=approval_policy())
        process_pr_event(newer_event, transport, policy_config=approval_policy())
        config.agent_identity_var.set("Operator")
        monkeypatch.setattr(
            producer_sign, "resolve_agent_bearer_token", lambda _label: "test-bearer"
        )
        approvals.approval_resolve(older_pending["approval_id"], "approve")
        result = process_pr_event(
            older_event,
            transport,
            policy_config=approval_policy(),
            pending_check_run_id=older_pending["check_run_id"],
        )
        assert result["status"] == "approved"
    finally:
        config.agent_identity_var.reset(identity)


def test_tampered_evidence_signature_fails_closed(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    token = config.agent_identity_var.set("Operator")
    try:
        transport = FakeTransport()
        event = normalized_event()
        monkeypatch.setattr(
            producer_sign, "resolve_agent_bearer_token", lambda _label: "test-bearer"
        )
        config.agent_identity_var.set("Desktop_Agent2")
        pending = process_pr_event(event, transport, policy_config=approval_policy())
        config.agent_identity_var.set("Operator")
        approvals.approval_resolve(pending["approval_id"], "approve")
        process_pr_event(
            event,
            transport,
            policy_config=approval_policy(),
            pending_check_run_id=pending["check_run_id"],
        )
        marker = re.search(
            r"<!-- unified-agent github-evidence (.+?) -->", transport.comments[0]["body"]
        )
        signed = json.loads(marker.group(1))
        signed["signature"] = "0" * 64
        assert producer_sign.verify_producer_signature(signed) is False
    finally:
        config.agent_identity_var.reset(token)


def test_horizon2_package_is_registered_but_inert_by_default():
    from unified_agent.horizon2 import CONNECTOR_PACKAGES

    assert "github-app" in CONNECTOR_PACKAGES
    assert normalized_event().to_payload()["connector"] == "github-app"
    # Keep the pre-existing mapping contract while the connector registry
    # exposes its richer structural declaration to doctor and the UI.
    assert GITHUB_APP_MANIFEST["default_enabled"] is False
    assert GITHUB_APP_MANIFEST["required_secrets"] == (
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
    )
    from unified_agent import connectors

    assert set(connectors.GITHUB_APP_MANIFEST.required_secret_alternatives) == {
        frozenset({"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}),
        frozenset({"UA_GITHUB_USER_TOKEN"}),
    }


# --- Token-provider mode (task #1261) ---------------------------------------


def test_token_mode_defaults_to_app_and_validates(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_TOKEN_MODE", raising=False)
    assert github_app.token_mode() == "app"

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    assert github_app.token_mode() == "user"

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "USER")
    assert github_app.token_mode() == "user"

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "bogus")
    with pytest.raises(ValueError, match="invalid UA_GITHUB_TOKEN_MODE"):
        github_app.token_mode()


def test_user_token_env_var_name_is_configurable(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN_VAR", raising=False)
    assert github_app.user_token_env_var() == "UA_GITHUB_USER_TOKEN"

    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_VAR", "GH_OPERATOR_TOKEN")
    assert github_app.user_token_env_var() == "GH_OPERATOR_TOKEN"


def test_resolve_user_token_prefers_the_named_env_var(monkeypatch, caplog):
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN", "env-token")

    def fail_run_gh(*_args, **_kwargs):
        raise AssertionError("gh should not run when the env var already has a token")

    with caplog.at_level("INFO", logger=github_app.__name__):
        assert github_app.resolve_user_token(run_gh=fail_run_gh) == "env-token"
    assert "github_user_token_source=env:'UA_GITHUB_USER_TOKEN'" in caplog.text
    assert "env-token" not in caplog.text


def test_resolve_user_token_falls_back_to_gh_auth_token(monkeypatch, caplog):
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)

    class FakeProc:
        returncode = 0
        stdout = "gh-cli-token\n"
        stderr = ""

    def fake_run_gh(cmd, **kwargs):
        assert cmd == ["gh", "auth", "token"]
        assert kwargs.get("timeout") == github_app.GH_AUTH_TIMEOUT_SECONDS
        return FakeProc()

    with caplog.at_level("INFO", logger=github_app.__name__):
        assert github_app.resolve_user_token(run_gh=fake_run_gh) == "gh-cli-token"
    assert "github_user_token_source=gh_auth_token" in caplog.text
    assert "gh-cli-token" not in caplog.text


def test_resolve_user_token_fails_closed_when_nothing_is_available(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "gh: not logged in"

    with pytest.raises(PermissionError, match="UA_GITHUB_USER_TOKEN"):
        github_app.resolve_user_token(run_gh=lambda *a, **k: FakeProc())


def test_resolve_user_token_fails_closed_on_gh_timeout(monkeypatch):
    """Devin review (PR #692): a `gh auth token` timeout must not escape as a
    raw subprocess.TimeoutExpired -- it has to fail closed like any other
    unavailable-token case."""
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)

    def timing_out_run_gh(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    with pytest.raises(PermissionError, match="UA_GITHUB_USER_TOKEN"):
        github_app.resolve_user_token(run_gh=timing_out_run_gh)


def test_resolve_user_token_error_never_includes_gh_output(monkeypatch):
    """A denial reason from `gh` must never leak into our exception message."""
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN", raising=False)
    secret_looking_text = "would-be-token-abc123-do-not-leak"

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = f"gh: not logged in ({secret_looking_text})"

    with pytest.raises(PermissionError) as excinfo:
        github_app.resolve_user_token(run_gh=lambda *a, **k: FakeProc())
    assert secret_looking_text not in str(excinfo.value)


def test_request_refuses_cross_host_redirect_without_replaying_authorization():
    """A 302 to a different host must raise, not replay the bearer token."""
    import http.server
    import threading

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(302)
            self.send_header("Location", "http://attacker.invalid/harvest")
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    transport = github_app.GitHubUserTokenTransport(
        lambda: "secret-token", api_base=f"http://127.0.0.1:{server.server_port}"
    )
    try:
        with pytest.raises(PermissionError, match="cross-host redirect"):
            transport._request("POST", "/repos/o/r/statuses/abc123", {"state": "pending"})
    finally:
        server.server_close()
        thread.join(timeout=5)

    # The original request carried the token (that is the leak vector), but
    # nothing was ever replayed to the redirect target.
    assert seen == ["Bearer secret-token"]


def test_request_follows_same_host_redirect():
    import http.server
    import threading

    # ThreadingHTTPServer + serve_forever: two concurrent handle_request()
    # calls on one single-threaded server raced. And the POST body is drained
    # before the 302: closing a socket with unread bytes sends a TCP reset,
    # which the client could see before the redirect (flaky under load,
    # ConnectionResetError on the Mac hub 2026-09-28).
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    server.daemon_threads = True
    base = f"http://127.0.0.1:{server.server_port}"

    class Handler(http.server.BaseHTTPRequestHandler):
        def _drain_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)

        def _json_ok(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            self._drain_body()
            if self.path == "/repos/o/r/statuses/abc123":
                self.send_response(302)
                self.send_header("Location", f"{base}/final")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json_ok()

        def do_GET(self):  # noqa: N802 - urllib follows 302 as GET
            self._json_ok()

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    transport = github_app.GitHubUserTokenTransport(
        lambda: "tok", api_base=f"http://127.0.0.1:{server.server_port}"
    )
    try:
        payload = transport._request("POST", "/repos/o/r/statuses/abc123", {"state": "pending"})
        assert payload == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_token_never_appears_in_logs_reprs_or_errors(monkeypatch, caplog):
    secret_token = "ghp_supersecrettoken1234567890"  # noqa: S105 - test fixture, not a real credential
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN", secret_token)
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN_VAR", raising=False)
    caplog.set_level("DEBUG")

    resolved = github_app.resolve_user_token()
    assert resolved == secret_token

    transport = github_app.GitHubUserTokenTransport(lambda: secret_token)
    assert secret_token not in repr(transport)
    assert secret_token not in str(vars(transport))

    with pytest.raises(ValueError) as excinfo:
        transport.update_check_run(
            repo="example/repo",
            check_run_id="not-a-status-id",
            status="completed",
            conclusion="success",
        )
    assert secret_token not in str(excinfo.value)

    for record in caplog.records:
        assert secret_token not in record.getMessage()


# --- rate-limit backoff (task #2237) ------------------------------------------

NOW = 1_800_000_000.0


def _headers(**values: str) -> Message:
    headers = Message()
    for name, value in values.items():
        headers[name.replace("_", "-")] = value
    return headers


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (429, {"Retry_After": "120"}, "", 120),
        (403, {"Retry_After": "30"}, "", 30),
        (403, {"Retry_After": format_datetime(datetime.fromtimestamp(NOW + 300, UTC))}, "", 300),
        (403, {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": str(int(NOW) + 900)}, "", 900),
        # The longer of Retry-After and the quota reset wins.
        (
            429,
            {
                "Retry_After": "45",
                "X_RateLimit_Remaining": "0",
                "X_RateLimit_Reset": str(int(NOW) + 10),
            },
            "",
            45,
        ),
        (429, {}, "", 60),
        (403, {}, '{"message": "You have exceeded a secondary rate limit."}', 60),
        # Absurd values are capped at one hour.
        (403, {"Retry_After": "9" * 40}, "", 3600),
        (403, {"Retry_After": "86400"}, "", 3600),
        (403, {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "9" * 30}, "", 3600),
        # Malformed values are ignored; the signal still means GitHub's default wait.
        (403, {"Retry_After": "soon"}, "", 60),
        (403, {"Retry_After": "-5"}, "", 60),
        (403, {"Retry_After": "²"}, "", 60),
        (403, {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "later"}, "", 60),
        # A reset already past still waits the one-second floor.
        (403, {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": str(int(NOW) - 50)}, "", 1),
        # No rate-limit signal: a permission error, never a backoff.
        (403, {}, '{"message": "Resource not accessible by integration"}', None),
        (
            403,
            {"X_RateLimit_Remaining": "4999", "X_RateLimit_Reset": str(int(NOW) + 900)},
            "",
            None,
        ),
        (500, {"Retry_After": "30"}, "", None),
    ],
)
def test_rate_limit_backoff_reads_each_header(status, headers, body, expected):
    assert github_app.rate_limit_backoff_s(status, _headers(**headers), body=body, now=NOW) == (
        None if expected is None else float(expected)
    )


def _rate_limited_opener(monkeypatch, *, code=403, headers=None, body=b""):
    sent: list[str] = []

    def open_request(request, timeout):
        sent.append(request.full_url)
        raise HTTPError(request.full_url, code, "Forbidden", headers or Message(), BytesIO(body))

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    return sent


def _post_status(transport):
    transport.update_check_run(
        repo="example/repo",
        check_run_id="status:abc123|",
        status="completed",
        conclusion="action_required",
    )


def _fake_clocks(monkeypatch, wall=NOW, mono=1000.0):
    """``[wall, monotonic]``; tests move them separately to model a clock step."""
    clocks = [wall, mono]
    monkeypatch.setattr(github_app, "_wall_clock", lambda: clocks[0])
    monkeypatch.setattr(github_app, "_monotonic", lambda: clocks[1])
    return clocks


def _advance(clocks, seconds):
    clocks[0] += seconds
    clocks[1] += seconds


@pytest.fixture(params=["worker", "fastpath"])
def brokered_read(request, monkeypatch):
    from unified_agent import github_app_fastpath, github_app_worker

    # Each case starts with no backoff: a deadline recorded into the module's
    # own dicts or the shared file would otherwise refuse the next case's read.
    monkeypatch.setattr(github_app, "_LOCAL_BACKOFF", {})
    monkeypatch.setattr(github_app, "_NORMALIZED_ANCHORS", {})
    github_app.github_backoff_path().unlink(missing_ok=True)
    if request.param == "worker":
        return lambda: github_app_worker._brokered_github_json(
            "synthetic-token", "/repos/example/repo/pulls"
        )
    return lambda: github_app_fastpath._brokered_conditional_list(
        "synthetic-token", "example/repo", "main", 'W/"cached"'
    )


def _brokered_responses(monkeypatch, responses):
    sent = []
    client = httpx.Client

    def handle(request):
        sent.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs)
    )
    return sent


@pytest.mark.parametrize(
    "status,headers,body,wait",
    [
        (429, {"retry-after": "120"}, "", 120),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 900)}, "", 900),
        (403, {}, "You have exceeded a secondary rate limit.", 60),
        (429, {}, "", 60),
    ],
)
def test_brokered_read_records_shared_backoff_and_resumes(
    monkeypatch, brokered_read, status, headers, body, wait
):
    clocks = _fake_clocks(monkeypatch)
    sent = _brokered_responses(
        monkeypatch,
        [httpx.Response(status, headers=headers, text=body), httpx.Response(200, json=[])],
    )
    user = github_app.GitHubUserTokenTransport(lambda: pytest.fail("token resolved"), brokered=True)
    with pytest.raises(github_app.GitHubBackoff) as first:
        brokered_read()
    assert (first.value.retry_after_s, first.value.sent) == (wait, True)
    assert json.loads(github_app.github_backoff_path().read_text()) == {
        user.backoff_scope(): {"at": NOW, "until": NOW + wait}
    }
    # Another process sees the persisted deadline; reads and writes share it.
    monkeypatch.setattr(github_app, "_LOCAL_BACKOFF", {})
    monkeypatch.setattr(github_app, "_NORMALIZED_ANCHORS", {})
    _advance(clocks, wait - 1)
    for call in (brokered_read, lambda: _post_status(user)):
        with pytest.raises(github_app.GitHubBackoff) as refused:
            call()
        assert (refused.value.retry_after_s, refused.value.sent) == (1, False)
    assert len(sent) == 1
    _advance(clocks, 1)
    brokered_read()
    assert len(sent) == 2


def test_brokered_read_obeys_existing_user_backoff_only(monkeypatch, brokered_read):
    _fake_clocks(monkeypatch)
    sent = _brokered_responses(monkeypatch, [httpx.Response(200, json=[])])
    app = github_app.GitHubAppHttpTransport(lambda: "unused")
    github_app._record_backoff(app.backoff_scope(), 600)
    brokered_read()
    user = github_app.GitHubUserTokenTransport(lambda: "unused")
    github_app._record_backoff(user.backoff_scope(), 300)
    with pytest.raises(github_app.GitHubBackoff) as refused:
        brokered_read()
    assert (refused.value.retry_after_s, refused.value.sent, len(sent)) == (300, False, 1)


def test_brokered_permission_403_never_backs_off(monkeypatch, brokered_read):
    sent = _brokered_responses(
        monkeypatch,
        [httpx.Response(403, json={"message": "Resource not accessible by integration"})] * 2,
    )
    for _ in range(2):
        try:
            result = brokered_read()
        except RuntimeError as exc:
            assert not isinstance(exc, github_app.GitHubBackoff)
            assert str(exc) == "GitHub PR read failed"
        else:
            assert result == (403, None, None, False)
    assert len(sent) == 2
    assert not github_app.github_backoff_path().exists()


def test_rate_limited_transport_backs_off_without_sending(monkeypatch):
    clocks = _fake_clocks(monkeypatch)
    sent = _rate_limited_opener(monkeypatch, headers=_headers(Retry_After="600"))
    user = github_app.GitHubUserTokenTransport(lambda: "tok")

    with pytest.raises(github_app.GitHubBackoff) as first:
        _post_status(user)
    assert (first.value.retry_after_s, first.value.sent, len(sent)) == (600, True, 1)
    # Persisted for the other process (poll worker vs hub fast path).
    saved = json.loads(github_app.github_backoff_path().read_text())
    assert saved == {user.backoff_scope(): {"at": NOW, "until": NOW + 600}}

    _advance(clocks, 599)
    with pytest.raises(github_app.GitHubBackoff, match="no request was sent") as refused:
        # Refused before the credential is even resolved.
        _post_status(github_app.GitHubUserTokenTransport(lambda: pytest.fail("token resolved")))
    assert refused.value.sent is False
    with pytest.raises(github_app.GitHubBackoff):
        user.find_completed_check(
            repo="example/repo", head_sha="abc123", name=CHECK_RUN_NAME, conclusion="success"
        )
    assert len(sent) == 1
    assert github_app.github_backoff_remaining(user) == 1
    # The App installation is its own bucket; a test double has none.
    assert github_app.github_backoff_remaining(github_app.GitHubAppHttpTransport(lambda: "t")) == 0
    assert github_app.github_backoff_remaining(FakeTransport()) == 0

    _advance(clocks, 1)
    with pytest.raises(github_app.GitHubBackoff):
        _post_status(user)
    assert len(sent) == 2


def test_every_send_is_refused_while_backing_off(monkeypatch):
    """Review B2: comment lookups, and a backoff recorded while a token resolves."""
    _fake_clocks(monkeypatch)
    sent: list[str] = []

    def open_request(request, timeout):
        sent.append(request.get_method())
        return BytesIO(b"[]")

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    user = github_app.GitHubUserTokenTransport(lambda: "tok")
    scope = user.backoff_scope()
    github_app._record_backoff(scope, 600)
    for _ in range(3):
        with pytest.raises(github_app.GitHubBackoff):
            user.find_evidence_comment(repo="example/repo", number=1, event_id="e", approval_id=1)
        with pytest.raises(github_app.GitHubBackoff):
            user.find_comment_with_marker(repo="example/repo", number=1, marker="marker")
    assert sent == []

    github_app.github_backoff_path().unlink()
    monkeypatch.setattr(github_app, "_LOCAL_BACKOFF", {})

    def token_while_another_call_is_limited():
        github_app._record_backoff(scope, 600)
        return "tok"

    racing = github_app.GitHubUserTokenTransport(token_while_another_call_is_limited)
    with pytest.raises(github_app.GitHubBackoff, match="no request was sent"):
        _post_status(racing)
    with pytest.raises(github_app.GitHubBackoff):
        github_app.GitHubUserTokenTransport(
            token_while_another_call_is_limited
        ).find_comment_with_marker(repo="example/repo", number=1, marker="marker")
    assert sent == []


def test_permission_403_is_raised_and_never_backs_off(monkeypatch):
    sent = _rate_limited_opener(
        monkeypatch, body=b'{"message": "Resource not accessible by integration"}'
    )
    transport = github_app.GitHubAppHttpTransport(lambda: "tok")
    for _ in range(2):
        with pytest.raises(HTTPError) as excinfo:
            transport.create_evidence_comment(repo="example/repo", number=1, body="hi")
        assert not isinstance(excinfo.value, github_app.GitHubBackoff)
    assert len(sent) == 2
    assert not github_app.github_backoff_path().exists()


def test_concurrent_writers_never_shorten_or_erase_a_deadline(monkeypatch):
    """Review B3: a slow writer holding stale state cannot replace a newer deadline."""
    import threading

    _fake_clocks(monkeypatch)
    path = github_app.github_backoff_path()
    scope, other = "user:https://api.github.com", "app:https://api.github.com"
    real_read = github_app._read_backoff_entries
    first_read, rival_done = threading.Event(), threading.Event()

    def slow_first_read(target):
        entries = real_read(target)
        if not first_read.is_set():
            first_read.set()
            # Without the lock the rival writes here and this stale map lands last.
            rival_done.wait(timeout=0.5)
        return entries

    monkeypatch.setattr(github_app, "_read_backoff_entries", slow_first_read)

    def rival():
        first_read.wait(timeout=5)
        github_app._record_backoff(scope, 600)
        github_app._record_backoff(other, 300)
        rival_done.set()

    thread = threading.Thread(target=rival)
    thread.start()
    github_app._record_backoff(scope, 60)
    thread.join(timeout=10)
    assert {key: entry["until"] for key, entry in real_read(path).items()} == {
        scope: NOW + 600,
        other: NOW + 300,
    }


def test_backoff_fails_closed_when_its_state_cannot_be_kept(monkeypatch):
    """Review B4: a failed write or an unreadable file never reopens the gate."""
    import os

    clocks = _fake_clocks(monkeypatch)
    sent = _rate_limited_opener(monkeypatch, code=429, headers=_headers(Retry_After="600"))
    user = github_app.GitHubUserTokenTransport(lambda: "tok")

    def disk_full(*_args):
        raise OSError("disk full")

    real_replace = github_app.os.replace
    monkeypatch.setattr(github_app.os, "replace", disk_full)
    for _ in range(5):
        with pytest.raises(github_app.GitHubBackoff):
            _post_status(user)
    assert len(sent) == 1 and not github_app.github_backoff_path().exists()
    assert github_app.github_backoff_remaining(user) == 600  # held in this process
    # Restore only the write; monkeypatch.undo() would also drop the isolated
    # UA_HOME and send the rest of this test to the real data directory.
    monkeypatch.setattr(github_app.os, "replace", real_replace)

    # An unreadable shared file backs off GITHUB_BACKOFF_DEFAULT_S from its last
    # write, then clears; a huge integer is just as unreadable.
    clocks = _fake_clocks(monkeypatch)
    monkeypatch.setattr(github_app, "_LOCAL_BACKOFF", {})
    path = github_app.github_backoff_path()
    scope = user.backoff_scope()
    for text in (
        "not json",
        "[1, 2]",
        json.dumps({scope: "soon"}),
        json.dumps({scope: {"at": NOW, "until": True}}),
        f'{{"{scope}": {{"at": {10**500}, "until": {10**500}}}}}',
        json.dumps({scope: "x" * 5000}),
    ):
        path.write_text(text)
        os.utime(path, (NOW - 20, NOW - 20))
        assert github_app.github_backoff_remaining(user) == 40, text
    _advance(clocks, 40)
    assert github_app.github_backoff_remaining(user) == 0
    # The next rate limit replaces the unreadable file.
    github_app._record_backoff(scope, 30)
    assert json.loads(path.read_text()) == {scope: {"at": NOW + 40, "until": NOW + 70}}


def test_stored_deadline_is_bounded_in_elapsed_time(monkeypatch):
    """Review B5: a far-future value or a wall clock stepped back waits at most the cap."""
    clocks = _fake_clocks(monkeypatch)
    user = github_app.GitHubUserTokenTransport(lambda: "tok")
    scope = user.backoff_scope()
    path = github_app.github_backoff_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({scope: {"at": NOW, "until": NOW + 10**9}}))
    waits = []
    for _ in range(3):
        waits.append(github_app.github_backoff_remaining(user))
        _advance(clocks, 3600)
    assert waits == [3600, 0, 0]

    # A normal 600 s wait, then the wall clock steps back a day: the wait is
    # re-anchored, not stretched to the day.
    github_app._record_backoff(scope, 600)
    clocks[0] -= 86400
    assert github_app.github_backoff_remaining(user) == 600
    _advance(clocks, 600)
    assert github_app.github_backoff_remaining(user) == 0
    # The process-local deadline runs on the monotonic clock, which a wall-clock
    # step does not move.
    github_app._record_backoff(scope, 120)
    clocks[0] += 86400
    assert github_app.github_backoff_remaining(user) == 120


def test_bound_counts_down_when_it_cannot_be_written_back(monkeypatch):
    """Review r2 B5: a rolled-back wall clock and a failing write must still expire."""
    clocks = _fake_clocks(monkeypatch)
    user = github_app.GitHubUserTokenTransport(lambda: "tok")
    github_app._record_backoff(user.backoff_scope(), 600)
    stored = github_app.github_backoff_path().read_text()

    def disk_full(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(github_app.os, "replace", disk_full)
    step_back = NOW - 86400
    waits = []
    for elapsed in (0, 300, 600, 3600, 7200):
        clocks[:] = [step_back + elapsed, 1000.0 + elapsed]
        waits.append(github_app.github_backoff_remaining(user))
    assert waits == [600, 300, 0, 0, 0]
    assert github_app.github_backoff_path().read_text() == stored  # never written back

    sent = []
    monkeypatch.setattr(
        github_app._REDIRECT_SAFE_OPENER,
        "open",
        lambda request, timeout: sent.append(request.get_method()) or BytesIO(b"{}"),
    )
    _post_status(user)
    assert sent == ["POST"]


def test_build_transport_app_mode_requires_installation_token_provider(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_TOKEN_MODE", raising=False)
    with pytest.raises(PermissionError, match="app_installation_token"):
        github_app.build_transport()


def test_build_transport_app_mode_returns_app_transport(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_TOKEN_MODE", raising=False)
    transport = github_app.build_transport(app_installation_token=lambda: "tok")
    assert isinstance(transport, github_app.GitHubAppHttpTransport)


@pytest.fixture(scope="module")
def app_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


class _FakeInstallations:
    """GitHub's two minting endpoints; each POST hands out a new token."""

    def __init__(self, expires_at="2026-11-06T21:00:00Z"):
        self.requests = []
        self.minted = 0
        self.expires_at = expires_at

    def __call__(self, request, timeout):
        self.requests.append(request)
        if request.get_method() == "GET":
            return BytesIO(json.dumps({"id": 77}).encode())
        self.minted += 1
        body = {"token": f"minted-{self.minted}", "expires_at": self.expires_at}
        return BytesIO(json.dumps(body).encode())


def _provider(pem, clock=lambda: _APP_NOW):
    return github_app.GitHubAppInstallationToken(
        app_id="12345", private_key=pem, repo="octo/widgets", clock=clock
    )


_APP_NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC).timestamp()


def test_installation_token_is_minted_for_one_repo_from_a_signed_jwt(monkeypatch, app_key):
    key, pem = app_key
    github = _FakeInstallations()
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", github)

    assert _provider(pem)() == "minted-1"

    lookup, mint = github.requests
    assert lookup.full_url == "https://api.github.com/repos/octo/widgets/installation"
    assert mint.full_url == "https://api.github.com/app/installations/77/access_tokens"
    assert json.loads(mint.data) == {"repositories": ["widgets"]}
    jwt = mint.get_header("Authorization").removeprefix("Bearer ")
    header, claims, signature = jwt.split(".")
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    decoded = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    assert decoded == {"iat": int(_APP_NOW) - 60, "exp": int(_APP_NOW) + 540, "iss": "12345"}


def test_installation_token_is_cached_until_the_refresh_margin(monkeypatch, app_key):
    github = _FakeInstallations()
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", github)
    now = [_APP_NOW]
    provider = _provider(app_key[1], clock=lambda: now[0])

    assert [provider(), provider()] == ["minted-1", "minted-1"]
    now[0] += 3300 - 1  # 20:54:59, still ahead of the 5 minute margin
    assert provider() == "minted-1"
    now[0] += 1
    assert provider() == "minted-2"
    # The installation is looked up once; only the token is replaced.
    assert [request.get_method() for request in github.requests] == ["GET", "POST", "POST"]


def test_rejected_installation_token_is_replaced_on_the_next_call(monkeypatch, app_key):
    github = _FakeInstallations()

    def opener(request, timeout):
        if "/issues/" in request.full_url:
            raise HTTPError(request.full_url, 401, "Bad credentials", Message(), None)
        return github(request, timeout)

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", opener)
    provider = _provider(app_key[1])
    transport = github_app.GitHubAppHttpTransport(provider)

    with pytest.raises(HTTPError):
        transport.create_evidence_comment(repo="octo/widgets", number=1, body="x")

    assert provider() == "minted-2"


def test_invalidating_an_older_installation_token_keeps_the_newer_one(monkeypatch, app_key):
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", _FakeInstallations())
    provider = _provider(app_key[1])
    provider()

    provider.invalidate("minted-0")

    assert provider() == "minted-1"


def test_app_mode_builds_its_provider_from_env_with_an_escaped_key(monkeypatch, app_key):
    monkeypatch.delenv("UA_GITHUB_TOKEN_MODE", raising=False)
    github = _FakeInstallations(expires_at="2999-01-01T00:00:00Z")
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", github)
    env = {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY": app_key[1].replace("\n", "\\n")}

    transport = github_app.build_transport(env=env, repo="octo/widgets")

    assert isinstance(transport, github_app.GitHubAppHttpTransport)
    assert transport._token() == "minted-1"


@pytest.mark.parametrize(
    "env,named",
    [
        ({}, "GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY"),
        ({"GITHUB_APP_ID": "12345"}, "GITHUB_APP_PRIVATE_KEY unset"),
    ],
)
def test_app_mode_names_the_credentials_it_lacks(env, named):
    with pytest.raises(PermissionError, match=named):
        github_app.build_transport(env=env, repo="octo/widgets")


def test_unusable_app_key_is_refused_without_repeating_it():
    fragment = "not-really-a-key-fragment"
    with pytest.raises(PermissionError) as refused:
        _provider(fragment)
    assert fragment not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_unreadable_expiry_is_refused_without_repeating_the_token(monkeypatch, app_key):
    github = _FakeInstallations(expires_at="soon")
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", github)
    provider = _provider(app_key[1])

    with pytest.raises(ToolError) as refused:
        provider()

    assert "minted-1" not in str(refused.value)
    assert provider._cached == ""


def test_build_transport_user_mode_returns_user_transport(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN", "tok")
    transport = github_app.build_transport()
    assert isinstance(transport, github_app.GitHubUserTokenTransport)


@pytest.mark.parametrize(
    "status,conclusion,expected",
    [
        ("queued", None, "pending"),
        ("in_progress", None, "pending"),
        ("completed", "success", "success"),
        ("completed", "failure", "failure"),
        ("completed", "cancelled", "error"),
        ("completed", "timed_out", "error"),
        ("completed", "expired", "error"),
        ("completed", "revoked", "error"),
    ],
)
def test_commit_status_state_mapping(status, conclusion, expected):
    assert github_app._commit_status_state(status, conclusion) == expected


def test_commit_status_state_mapping_rejects_unrecognized_input():
    with pytest.raises(ValueError, match="cannot map"):
        github_app._commit_status_state("completed", "bogus")
    with pytest.raises(ValueError, match="cannot map"):
        github_app._commit_status_state("bogus", None)


def test_github_user_token_transport_status_lifecycle(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def fake_request(self, method, path, body):
        calls.append((method, path, dict(body)))
        return {"id": 999}

    monkeypatch.setattr(github_app._GitHubHttpTransportBase, "_request", fake_request)

    transport = github_app.GitHubUserTokenTransport(lambda: "tok")
    check_run_id = transport.create_check_run(
        repo="example/repo",
        head_sha="abc123",
        name=CHECK_RUN_NAME,
        status="queued",
        details_url="https://github.com/example/repo/pull/1",
    )
    assert check_run_id.startswith("status:abc123|")
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/repos/example/repo/statuses/abc123"
    assert body["context"] == "Approval Ledger"
    assert body["state"] == "pending"
    assert body["target_url"] == "https://github.com/example/repo/pull/1"
    assert body["description"]

    transport.update_check_run(
        repo="example/repo", check_run_id=check_run_id, status="completed", conclusion="success"
    )
    assert calls[1][1] == "/repos/example/repo/statuses/abc123"
    assert calls[1][2]["state"] == "success"
    # The terminal status preserves the original pending status's target_url
    # (Devin review finding: a naive update_check_run(details_url=None) would
    # drop it, making the newest visible status link nowhere).
    assert calls[1][2]["target_url"] == "https://github.com/example/repo/pull/1"

    transport.update_check_run(
        repo="example/repo", check_run_id=check_run_id, status="completed", conclusion="failure"
    )
    assert calls[2][2]["state"] == "failure"

    transport.update_check_run(
        repo="example/repo", check_run_id=check_run_id, status="completed", conclusion="cancelled"
    )
    assert calls[3][2]["state"] == "error"


def test_github_user_token_transport_create_check_run_without_details_url(monkeypatch):
    """No details_url at creation must still round-trip through update_check_run."""
    calls: list[tuple[str, str, dict]] = []

    def fake_request(self, method, path, body):
        calls.append((method, path, dict(body)))
        return {"id": 1}

    monkeypatch.setattr(github_app._GitHubHttpTransportBase, "_request", fake_request)

    transport = github_app.GitHubUserTokenTransport(lambda: "tok")
    check_run_id = transport.create_check_run(
        repo="example/repo", head_sha="deadbeef", name=CHECK_RUN_NAME, status="queued"
    )
    assert check_run_id == "status:deadbeef|"
    assert "target_url" not in calls[0][2]

    transport.update_check_run(
        repo="example/repo", check_run_id=check_run_id, status="completed", conclusion="success"
    )
    assert "target_url" not in calls[1][2]


def test_github_user_token_transport_rejects_foreign_check_run_id():
    transport = github_app.GitHubUserTokenTransport(lambda: "tok")
    with pytest.raises(ValueError, match="not a commit-status check_run_id"):
        transport.update_check_run(
            repo="example/repo", check_run_id="12345", status="completed", conclusion="success"
        )


def test_evidence_comment_posting_shared_by_both_transports(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_request(self, method, path, body):
        calls.append((method, path))
        return {"id": 42}

    monkeypatch.setattr(github_app._GitHubHttpTransportBase, "_request", fake_request)

    app_transport = github_app.GitHubAppHttpTransport(lambda: "tok")
    user_transport = github_app.GitHubUserTokenTransport(lambda: "tok")
    for transport in (app_transport, user_transport):
        comment_id = transport.create_evidence_comment(repo="example/repo", number=5, body="hi")
        assert comment_id == "42"
    assert calls[0][1] == calls[1][1] == "/repos/example/repo/issues/5/comments"


def test_github_app_manifest_reflects_mode(monkeypatch):
    monkeypatch.delenv("UA_GITHUB_TOKEN_MODE", raising=False)
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN_VAR", raising=False)

    app_manifest = github_app.github_app_manifest()
    assert app_manifest["capabilities"] == ("poll", "check_runs", "evidence_comments")
    assert app_manifest["required_secrets"] == ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY")

    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    user_manifest = github_app.github_app_manifest()
    assert user_manifest["capabilities"] == ("poll", "commit_statuses", "evidence_comments")
    assert user_manifest["required_secrets"] == ("UA_GITHUB_USER_TOKEN",)
    assert user_manifest["default_enabled"] is False

    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_VAR", "MY_TOKEN")
    custom_manifest = github_app.github_app_manifest()
    assert custom_manifest["required_secrets"] == ("MY_TOKEN",)

    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    broker_manifest = github_app.github_app_manifest()
    assert broker_manifest["required_secrets"] == ("oauth:github:grant",)


def test_evidence_names_roles_not_logins():
    """The public evidence comment never carries a resolver login (#1777)."""
    assert github_app._human_role(config.operator_label()) == "operator"
    assert github_app._human_role(config.operator_label().upper()) == "operator"
    assert github_app._human_role("someone-else") == "human"
    assert github_app._human_role(None) == "human"
    assert github_app._human_role("") == "human"


def test_device_pseudonym_is_never_an_unkeyed_digest(monkeypatch):
    monkeypatch.setattr(github_app.producer_sign, "resolve_agent_bearer_token", lambda _i: None)
    assert github_app._device_pseudonym("Desktop_Agent2") == "unknown"
    monkeypatch.setattr(github_app.producer_sign, "resolve_agent_bearer_token", lambda _i: "k" * 32)
    value = github_app._device_pseudonym("Desktop_Agent2")
    assert len(value) == 16 and value != "unknown"
