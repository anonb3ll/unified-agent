"""Public-reply relay with public-safety gating (task #1734).

Fixture secrets are assembled by concatenation so no source line matches the
repo's own secret scanners.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import events, profiles
from unified_agent.tools import public_relay as pr
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
)

_FILL = "Zq9Lm4Np7Rs2Tv6Wx8Yb3Cd5Fg1Hj0Kk"
GITHUB_TOKEN = "gh" + "p_" + _FILL + "Ab12"
ANTHROPIC_KEY = "sk-" + "ant-" + _FILL + _FILL
AWS_KEY = "AK" + "IA" + "Q7R2T6W8Y3C5F1H0"
ASSIGNED = "api" + "_key = '" + _FILL + "'"
ENV_ASSIGNED = "DB_PASS" + "WORD=" + "s3cr" + "etValue99"
PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIEv" + _FILL + "\n-----END " + "PRIVATE KEY-----"
PEM_HEADER_ONLY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3Blbn"

SECRETS = [GITHUB_TOKEN, ANTHROPIC_KEY, AWS_KEY, ASSIGNED, ENV_ASSIGNED, PEM, PEM_HEADER_ONLY]


@pytest.fixture(autouse=True)
def _relay_env(monkeypatch):
    for var in (
        "UA_RELAY_INTERNAL_HOSTNAMES",
        "UA_RELAY_BLOCKED_TERMS",
        "UA_AGENT_HOSTS",
        "UA_HTTP_ALLOWED_HOSTS",
        "UA_APPROVAL_TOOLS",
        "UA_ZULIP_RETIRED",
        "UA_GITHUB_TOKEN_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UA_SOURCE_HOST", "testhost")


def _gate(text: str, **ctx) -> pr.GateResult:
    return pr.run_gates(text, pr.GateContext(**ctx))


def _categories(result: pr.GateResult) -> set[str]:
    return {f.category for f in result.findings}


# --- gates: one positive and one negative per category ---------------------------


@pytest.mark.parametrize(
    "secret",
    SECRETS,
    ids=["github", "anthropic", "aws", "assigned", "env", "pem", "pem-header"],
)
def test_secret_blocks_and_excerpt_never_repeats_it(secret):
    result = _gate(f"Here is the fix. {secret} Thanks!")
    assert result.verdict == pr.BLOCK
    assert result.text is None
    assert _categories(result) & {"secret", "private_key"}
    dumped = json.dumps(result.to_dict())
    assert secret not in dumped
    for finding in result.findings:
        # an excerpt keeps at most a short prefix: never the tail of the value
        assert secret[-8:] not in finding.excerpt


def test_secret_negative_and_placeholder_skipped():
    assert _gate("Set your token in the environment, then rerun.").verdict == pr.PASS
    for placeholder in (
        "API" + "_KEY=${API_KEY}",
        "DB_PASS" + "WORD=$DB_PASSWORD",
        "DB_PASS" + "WORD=<db-password>",
        "DB_PASS" + "WORD=REDACTED",
        "Authorization: Bearer <token>",
    ):
        assert "secret" not in _categories(_gate(placeholder)), placeholder


def test_hidden_unicode_blocks():
    result = _gate("looks normal​ but is not")
    assert result.verdict == pr.BLOCK
    assert _categories(result) == {"hidden_unicode"}
    assert _gate("plain ascii and an emoji 👍").verdict == pr.PASS


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("reach it at 10.0.0.5.", "private_address"),
        ("hub http://100.64.0.13:8765/mcp", "tailnet_address"),
        ("loopback 127.0.0.1", "private_address"),
        ("v6 fd00::1 here", "private_address"),
        ("mapped ::ffff:192.168.1.10 here", "private_address"),
    ],
)
def test_private_addresses_redact(text, category):
    result = _gate(text)
    assert result.verdict == pr.REDACT
    assert category in _categories(result)
    assert "[redacted-ip]" in result.text


def test_public_addresses_and_lookalikes_pass():
    result = _gate("DNS 8.8.8.8, 2001:4860:4860::8888, version 1.2.3.4, at 10:30:00")
    assert result.verdict == pr.PASS


def test_internal_hostnames_redact_suffixes_and_configured_names(monkeypatch):
    monkeypatch.setenv("UA_RELAY_INTERNAL_HOSTNAMES", "build-box-3")
    result = pr.relay_check(
        {
            "channel": "dry_run",
            "target": "t",
            "text": "ran on foo.tail1234.ts.net, printer.local and build-box-3",
        }
    )
    assert result["verdict"] == pr.REDACT
    assert "ts.net" not in result["text"]
    assert "printer.local" not in result["text"]
    assert "build-box-3" not in result["text"]
    assert result["text"].count("[internal-host]") == 3


def test_internal_hostname_negative_and_bare_words_not_derived(monkeypatch):
    # A bare word from config would redact ordinary prose; it is not derived.
    monkeypatch.setenv("UA_AGENT_HOSTS", "Desktop_Agent1:desktop,server_agent1:mac-hub-2")
    from unified_agent import config

    names = config.relay_internal_hostnames()
    assert "desktop" not in names
    assert "mac-hub-2" in names
    assert _gate("deployed to github.com and the desktop app").verdict == pr.PASS


def test_home_paths_become_tilde():
    result = _gate("edit /home/alice/dev/x.py or /Users/bob/code/y.py")
    assert result.verdict == pr.REDACT
    assert result.text == "edit ~/dev/x.py or ~/code/y.py"
    assert "alice" not in json.dumps(result.to_dict())
    assert _gate("edit ~/dev/x.py or /usr/local/bin/tool").verdict == pr.PASS


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("tracked in #1734", "task_ref"),
        ("run sp-01M3VGG3393VCFE0W5CK2R652D failed", "run_id"),
        ("event 01M3VGG3393VCFE0W5CK2R652D", "event_id"),
        ("memory b021a1072471 says", "memory_id"),
    ],
)
def test_internal_ids_redact_on_public_channels(text, detail):
    result = _gate(text, public=True)
    assert result.verdict == pr.REDACT
    assert [f.detail for f in result.findings] == [detail]
    assert "[internal-ref]" in result.text
    assert _gate(text, public=False).verdict == pr.PASS


def test_internal_ids_negative():
    text = "see item #12, sha 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b, and &#1234;"
    assert _gate(text).verdict == pr.PASS


def test_pii_email_and_phone_redact():
    result = _gate("mail jane.doe@corp.io or call +1 555-123-4567 / (555) 123-4567")
    assert result.verdict == pr.REDACT
    assert _categories(result) == {"email", "phone"}
    assert "jane" not in result.text and "4567" not in result.text


def test_pii_negative():
    text = "Co-Authored-By: noreply@anthropic.com, demo@example.com, git@github.com:o/r, 2026-10-01"
    assert _gate(text).verdict == pr.PASS


def test_length_and_blocked_terms_block(monkeypatch):
    assert _gate("x" * 11, max_chars=10).verdict == pr.BLOCK
    assert _gate("x" * 10, max_chars=10).verdict == pr.PASS
    result = _gate("this is Frobnicate talk", blocked_terms=frozenset({"frobnicate"}))
    assert result.verdict == pr.BLOCK
    assert _categories(result) == {"blocked_term"}
    assert _gate("frobnicated", blocked_terms=frozenset({"frobnicate"})).verdict == pr.PASS


def test_block_outranks_redact_and_gates_are_deterministic():
    text = f"at 10.0.0.5 use {GITHUB_TOKEN}"
    first, second = _gate(text), _gate(text)
    assert first.verdict == pr.BLOCK
    assert first.to_dict() == second.to_dict()


# --- JWK private keys (task #2001) ----------------------------------------------

# A JWK member value is base64url. Built from _FILL so no line is a literal key.
_B64U = _FILL + "_-" + _FILL[::-1]
_PUBLIC_RSA = '"kty": "RSA", "n": "' + _B64U + '", "e": "AQAB"'
_RSA_PRIVATE = {
    member: _PUBLIC_RSA + ', "' + member + '": "' + _B64U + member.upper() + '"'
    for member in ("d", "p", "q", "dp", "dq", "qi")
}
OCT_JWK = '{"kty": "oct", "alg": "HS256", "k": "' + _B64U + '"}'  # ua:allow
EC_JWK = (
    '{"kty":"EC","crv":"P-256","x":"'
    + _B64U
    + '","y":"'
    + _B64U
    + '","d":"'  # ua:allow
    + _B64U
    + '"}'  # ua:allow
)  # ua:allow


@pytest.mark.parametrize("member", sorted(_RSA_PRIVATE))
def test_jwk_private_member_blocks(member):
    jwk = "{" + _RSA_PRIVATE[member] + "}"
    result = _gate(f"Here is the key: {jwk}")
    assert result.verdict == pr.BLOCK
    assert "private_key" in _categories(result)
    secret = _B64U + member.upper()
    dumped = json.dumps(result.to_dict())
    assert secret not in dumped and secret[-8:] not in dumped


@pytest.mark.parametrize(
    "text",
    [
        OCT_JWK,
        EC_JWK,
        # escaped inside a JSON string, a python repr, and a JWKS key set
        json.dumps({"body": OCT_JWK}),
        "{'kty': 'oct', 'k': '" + _B64U + "'}",
        '{"keys": [{"kty": "RSA", "n": "x", "e": "AQAB"}, ' + EC_JWK + "]}",
        # cut off before the object closes
        '{"kty": "oct", "k": "' + _B64U,  # ua:allow
        # member before kty, with an invisible character inside the value; the
        # kty name is assembled so this source line opens no oct block (#2093)
        '{"k": "' + _B64U[:10] + "​" + _B64U[10:] + '", "kt' + 'y": "oct"}',
    ],
    ids=["oct", "ec", "escaped", "repr", "jwks", "truncated", "member-first-hidden"],
)
def test_jwk_symmetric_and_ec_keys_block(text):
    result = _gate(f"config: {text}")
    assert result.verdict == pr.BLOCK, text
    assert "private_key" in _categories(result)
    assert _B64U not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "text",
    [
        # a public JWK carries no private member
        "{" + _PUBLIC_RSA + "}",
        # "k" outside an oct key, and "k" or "d" with no JWK around it (an SVG path)
        '{"kty": "RSA", "k": "' + _B64U + '"}',
        '{"k": "' + _B64U + '"}',
        '{"d": "' + _B64U + '"}',
        # members of a different object than the kty
        '{"kty": "oct", "kid": "a"} and {"k": "' + _B64U + '"}',
        # a placeholder value
        '{"kty": "oct", "k": "REDACTED"}',
        '{"kty": "oct", "k": "${JWK_SECRET}"}',
    ],
    ids=["public", "k-not-oct", "k-bare", "d-bare", "other-object", "redacted", "variable"],
)
def test_jwk_without_private_material_passes(text):
    assert "private_key" not in _categories(_gate(text)), text


@pytest.mark.parametrize("text", [OCT_JWK, EC_JWK, json.dumps({"body": OCT_JWK})])
def test_jwk_gate_and_telemetry_both_hide_private_values(text):
    from unified_agent.telemetry import redaction

    assert _gate(text).verdict == pr.BLOCK
    clean = json.loads(redaction.redact_sensitive_content(text, 5000))
    if "body" in clean:
        assert clean["body"] == "[REDACTED_SECRET]"
    elif clean["kty"] == "EC":
        assert clean["d"] == "[REDACTED_SECRET]"
        assert clean["x"] == _B64U and clean["y"] == _B64U
    else:
        assert clean["k"] == "[REDACTED_SECRET]"


@pytest.mark.parametrize(
    "text",
    [
        '{"kty":"oct","metadata":{"brace":"}"},"k":"' + _B64U + '"}',
        '{"kty":"oct","k":"' + _B64U + '","metadata":{"note":"{nested}"}}',
        '{"kty":"oct","k":"' + _B64U + '"}'.replace('"kty"', '"\\u006bty"'),
    ],
)
def test_jwk_private_material_with_nested_extensions_and_escaped_names(text):
    from unified_agent.telemetry import redaction

    assert _gate(text).verdict == pr.BLOCK
    assert _B64U not in redaction.redact_sensitive_content(text, 5000)


def test_jwk_audit_reports_multiline_member_once_without_echoing_it():
    from unified_agent.audit import rules

    text = '{\n  "kty": "oct",\n  "k": "' + _B64U + '"\n}'
    findings = list(rules.check_secrets("fixture.json", text))
    assert len(findings) == 1
    assert findings[0].rule == "SECRETS-KEY"
    assert findings[0].line == 3
    assert _B64U not in str(findings)
    assert list(rules.check_secrets("fixture.json", "{" + _PUBLIC_RSA + "}")) == []


# --- relay flow ------------------------------------------------------------------


def _params(text: str = "Fixed in the latest release, thanks!", **extra) -> dict:
    return {
        "channel": "dry_run",
        "target": "issue-42",
        "text": text,
        "requested_by": "Desktop_Agent1",
        **extra,
    }


def _events(channel: str) -> list[dict]:
    return events.query_events(channels=[channel], limit=50)


def test_draft_queues_approval_then_send_consumes_pass():
    draft = pr.relay_manage("draft", _params("ping me at 10.0.0.5 when it lands"))
    assert draft["status"] == "pending"
    assert draft["verdict"] == pr.REDACT
    assert draft["text"] == "ping me at [redacted-ip] when it lands"
    approval_id = draft["approval_id"]
    pending = approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"]
    assert [row["id"] for row in pending] == [approval_id]
    assert pending[0]["payload"]["text"] == draft["text"]
    verdicts = _events(pr.VERDICT_CHANNEL)
    assert verdicts[-1]["payload"]["verdict"] == pr.REDACT
    assert "10.0.0.5" not in json.dumps(verdicts[-1]["payload"])

    with pytest.raises(ApprovalPendingError):
        pr.relay_manage("send", _params("ping me at 10.0.0.5 when it lands"))

    approval_resolve(approval_id, "approve", resolved_by="Operator")
    assert pr.relay_manage("status", {"approval_id": approval_id})["status"] == "approved"
    sent = pr.relay_manage("send", _params("ping me at 10.0.0.5 when it lands"))
    assert sent["status"] == "sent"
    assert sent["approval_id"] == approval_id
    assert sent["receipt"]["dry_run"] is True
    dry = _events(pr.DRY_RUN_CHANNEL)[-1]["payload"]
    assert dry["text"] == "ping me at [redacted-ip] when it lands"
    assert _events(pr.SENT_CHANNEL)[-1]["payload"]["approval_id"] == approval_id
    assert pr.relay_manage("status", {"approval_id": approval_id})["status"] == "consumed"

    # one-shot: an identical second send needs a fresh human decision
    with pytest.raises(ApprovalPendingError):
        pr.relay_manage("send", _params("ping me at 10.0.0.5 when it lands"))


def test_changed_text_does_not_ride_an_approval():
    draft = pr.relay_manage("draft", _params("version one"))
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    with pytest.raises(ApprovalPendingError):
        pr.relay_manage("send", _params("version two"))


def test_denied_draft_refuses_send():
    draft = pr.relay_manage("draft", _params())
    approval_resolve(draft["approval_id"], "deny", reason="tone")
    with pytest.raises(ToolError, match="DENIED"):
        pr.relay_manage("send", _params())
    assert pr.relay_manage("status", {"approval_id": draft["approval_id"]})["status"] == "denied"


def test_blocked_draft_is_recorded_not_queued():
    draft = pr.relay_manage("draft", _params(f"token {GITHUB_TOKEN}"))
    assert draft["status"] == "blocked"
    assert "approval_id" not in draft
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []
    payload = _events(pr.VERDICT_CHANNEL)[-1]["payload"]
    assert payload["verdict"] == pr.BLOCK
    assert GITHUB_TOKEN not in json.dumps(payload)
    with pytest.raises(ToolError, match="blocked"):
        pr.relay_manage("send", _params(f"token {GITHUB_TOKEN}"))


def test_autosend_hook_defaults_false_and_can_skip_approval(monkeypatch):
    assert pr.resolve_relay_autosend("dry_run", {}) is False
    monkeypatch.setattr(pr, "resolve_relay_autosend", lambda channel, context: True)
    draft = pr.relay_manage("draft", _params())
    assert draft["status"] == "sent"
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []
    assert _events(pr.SENT_CHANNEL)[-1]["payload"]["autosend"] is True


def test_status_rejects_non_relay_approvals():
    with pytest.raises(ToolError):
        pr.relay_manage("status", {"approval_id": 999999})
    with pytest.raises(ToolError):
        pr.relay_manage("status", {"approval_id": "nope"})


def test_relay_manage_rejects_unknown_action_param_and_channel():
    with pytest.raises(ToolError, match="action"):
        pr.relay_manage("publish", _params())
    with pytest.raises(ToolError, match="params"):
        pr.relay_manage("check", {**_params(), "bogus": 1})
    with pytest.raises(ToolError, match="unknown relay channel"):
        pr.relay_manage("check", {**_params(), "channel": "carrier-pigeon"})
    with pytest.raises(ToolError, match="text"):
        pr.relay_manage("check", _params(text="  "))


# --- adapters --------------------------------------------------------------------


class _FakeGitHubTransport:
    """Records posts; ``fail_after_post`` raises after recording, like a timeout
    on a request GitHub actually accepted."""

    def __init__(self, fail_after_post: BaseException | None = None):
        self.calls: list[dict] = []
        self.fail_after_post = fail_after_post

    def create_evidence_comment(self, *, repo: str, number: int, body: str) -> str:
        self.calls.append({"repo": repo, "number": number, "body": body})
        if self.fail_after_post is not None:
            raise self.fail_after_post
        return "777"

    def find_comment_with_marker(self, *, repo: str, number: int, marker: str):
        for i, call in enumerate(self.calls):
            if call["repo"] == repo and call["number"] == number and marker in call["body"]:
                return {"id": str(700 + i)}
        return None


def test_github_adapter_sends_through_injected_transport(monkeypatch):
    transport = _FakeGitHubTransport()
    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: transport)
    )
    params = _params("Fixed; see /home/alice/dev/notes.md", channel="github", target="o/r#12")
    draft = pr.relay_manage("draft", params)
    assert draft["status"] == "pending"
    assert transport.calls == []
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    sent = pr.relay_manage("send", params)
    assert len(transport.calls) == 1
    body = transport.calls[0]["body"]
    assert body.startswith("Fixed; see ~/dev/notes.md\n\n<!-- relay-attempt:")
    assert sent["attempt_id"] not in body  # the marker is a digest, not the hub id
    assert sent["receipt"] == {"delivered": True, "target": "o/r#12", "comment_id": "777"}


def test_github_adapter_validates_target_and_failed_send_is_recorded(monkeypatch):
    class _Failing:
        def create_evidence_comment(self, **kwargs):
            raise PermissionError("GitHub credential is required")

    with pytest.raises(ToolError, match="owner/repo#123"):
        pr.relay_manage("check", _params(channel="github", target="not-a-target"))

    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: _Failing())
    )
    params = _params(channel="github", target="o/r#5")
    draft = pr.relay_manage("draft", params)
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    with pytest.raises(ToolError, match="failed"):
        pr.relay_manage("send", params)
    assert _events(pr.SEND_FAILED_CHANNEL)[-1]["payload"]["approval_id"] == draft["approval_id"]


def test_github_adapter_in_app_mode_without_provider_is_unavailable():
    # Default UA_GITHUB_TOKEN_MODE is app, and the relay supplies no
    # installation-token provider: refuse before queueing anything.
    with pytest.raises(ToolError, match="GitHub transport unavailable"):
        pr.relay_manage("draft", _params(channel="github", target="o/r#1"))
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []


def test_zulip_adapter_refuses_while_retired(monkeypatch):
    params = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    with pytest.raises(ToolError, match="retired"):
        pr.relay_manage("draft", params)
    with pytest.raises(ToolError, match="retired"):
        pr.relay_manage("send", params)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "1")
    with pytest.raises(ToolError, match="retired"):
        pr.relay_manage("draft", params)
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []


def test_zulip_send_refuses_if_retired_after_approval(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    params = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    draft = pr.relay_manage("draft", params)
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    monkeypatch.delenv("UA_ZULIP_RETIRED")
    with pytest.raises(ToolError, match="retired"):
        pr.relay_manage("send", params)


# --- MCP surface -----------------------------------------------------------------


def test_relay_manage_is_registered_and_pruned_from_the_default_profile():
    from unified_agent import server, toolsets

    assert "relay_manage" in server.REGISTERED_TOOLS
    assert "relay_manage" in toolsets.TOOLSET_MEMBERS["ops"]
    assert not profiles.tool_is_allowed_for(profiles.DEVELOPER, "relay_manage")
    assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, "relay_manage")


def test_blocked_terms_come_from_config(monkeypatch):
    monkeypatch.setenv("UA_RELAY_BLOCKED_TERMS", "frobnicate, Wibble")
    result = pr.relay_check(_params("no wibble here"))
    assert result["verdict"] == pr.BLOCK
    assert result["text"] is None


# --- review fixes (PR #953) ------------------------------------------------------


def test_zulip_length_gate_matches_the_sender_limit():
    from unified_agent.tools import zulip

    limit = zulip.MAX_CONTENT_CHARS
    base = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    assert pr.relay_check({**base, "text": "a" * limit})["verdict"] == pr.PASS
    over = pr.relay_check({**base, "text": "a" * (limit + 1)})
    assert over["verdict"] == pr.BLOCK
    assert {f["category"] for f in over["findings"]} == {"length"}


def test_unavailable_channel_still_records_the_verdict():
    # Default GitHub app mode has no transport; Zulip is retired by default.
    blocked = pr.relay_manage(
        "draft", _params(f"key {GITHUB_TOKEN}", channel="github", target="o/r#7")
    )
    assert blocked["status"] == "blocked"
    assert _events(pr.VERDICT_CHANNEL)[-1]["payload"]["verdict"] == pr.BLOCK

    before = len(_events(pr.VERDICT_CHANNEL))
    with pytest.raises(ToolError, match="retired"):
        pr.relay_manage(
            "draft",
            _params(channel="zulip", target="general/release", context={"agent_label": "x"}),
        )
    assert len(_events(pr.VERDICT_CHANNEL)) == before + 1
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []


def _failing_record(monkeypatch, failing: set[str]):
    real = pr.events.record_event
    real_on_conn = pr.events.record_event_once_on_connection

    def record(channel, *args, **kwargs):
        if channel in failing:
            raise RuntimeError("ledger unavailable")
        return real(channel, *args, **kwargs)

    def record_on_conn(conn, channel, *args, **kwargs):
        # Closing outcomes are written inside the attempt's own transaction.
        if channel in failing:
            raise RuntimeError("ledger unavailable")
        return real_on_conn(conn, channel, *args, **kwargs)

    monkeypatch.setattr(pr.events, "record_event", record)
    monkeypatch.setattr(pr.events, "record_event_once_on_connection", record_on_conn)


def _approved_github(monkeypatch, transport, text="Shipped in the latest release."):
    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: transport)
    )
    params = _params(text, channel="github", target="o/r#12")
    draft = pr.relay_manage("draft", params)
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    return params, draft["approval_id"]


def test_sent_record_failure_is_a_recording_gap_not_a_resend(monkeypatch):
    transport = _FakeGitHubTransport()
    params, approval_id = _approved_github(monkeypatch, transport)
    _failing_record(monkeypatch, {pr.SENT_CHANNEL})

    out = pr.relay_manage("send", params)
    assert out["recording_gap"] is True
    assert out["receipt"]["comment_id"] == "777"
    assert len(transport.calls) == 1
    intent = _events(pr.SENDING_CHANNEL)[-1]
    assert intent["payload"]["approval_id"] == approval_id
    gap = _events("audit.evidence.gap")[-1]
    assert gap["causal_parent_id"] == intent["ulid"] == out["attempt_id"]
    assert gap["payload"]["stage"] == "sent_record"

    # The consumed approval cannot send again.
    with pytest.raises(ApprovalPendingError):
        pr.relay_manage("send", params)
    assert len(transport.calls) == 1


def test_unrecorded_delivery_blocks_reposting_the_same_text(monkeypatch):
    transport = _FakeGitHubTransport()
    params, _ = _approved_github(monkeypatch, transport)
    _failing_record(monkeypatch, {pr.SENT_CHANNEL, "audit.evidence.gap"})
    out = pr.relay_manage("send", params)
    assert out["recording_gap"] is True

    # A fresh human approval for the identical text and target is refused:
    # the first attempt has no recorded outcome and may be live.
    draft = pr.relay_manage("draft", params)
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    with pytest.raises(ToolError, match="not posted again"):
        pr.relay_manage("send", params)
    assert len(transport.calls) == 1


def test_one_approval_starts_at_most_one_delivery():
    params = _params("only once")
    draft = pr.relay_manage("draft", params)
    approval_id = draft["approval_id"]
    pr.events.record_event_once(
        pr.SENDING_CHANNEL,
        payload={"approval_id": approval_id, "fingerprint": "elsewhere"},
        dedupe_key=pr._sending_dedupe_key(approval_id),
    )
    approval_resolve(approval_id, "approve", resolved_by="Operator")
    with pytest.raises(ToolError, match="never sent twice"):
        pr.relay_manage("send", params)
    assert _events(pr.DRY_RUN_CHANNEL) == []


def test_short_blocked_terms_never_reach_evidence(monkeypatch):
    monkeypatch.setenv("UA_RELAY_BLOCKED_TERMS", "qz")
    draft = pr.relay_manage("draft", _params("that is qz talk"))
    assert draft["status"] == "blocked"
    assert [f["excerpt"] for f in draft["findings"]] == ["blocked term (2 chars)"]
    payload = json.dumps(_events(pr.VERDICT_CHANNEL)[-1]["payload"])
    assert "qz" not in payload.lower()


def test_zulip_sender_identity_is_bound_into_the_approval(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    base = _params(channel="zulip", target="general/release")
    draft = pr.relay_manage("draft", {**base, "context": {"agent_label": "bot-a"}})
    pending = approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"]
    assert pending[0]["payload"]["context"] == {"agent_label": "bot-a"}
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    sent: list[dict] = []
    monkeypatch.setattr(
        pr._zulip,
        "zulip_send_message",
        lambda **kw: sent.append(kw) or {"sent": True, "message_id": 1},
    )
    with pytest.raises(ApprovalPendingError):
        pr.relay_manage("send", {**base, "context": {"agent_label": "bot-b"}})
    assert sent == []
    pr.relay_manage("send", {**base, "context": {"agent_label": "bot-a"}})
    assert [kw["agent_label"] for kw in sent] == ["bot-a"]


def test_unknown_context_keys_are_refused():
    with pytest.raises(ToolError, match="context keys"):
        pr.relay_manage("check", _params(context={"agent_label": "x"}))
    with pytest.raises(ToolError, match="context keys"):
        pr.relay_manage(
            "check",
            _params(channel="zulip", target="g/t", context={"agent_label": "x", "via": "y"}),
        )


def test_target_is_gated_and_recorded_redacted():
    params = _params("hello", target="ops 10.0.0.5 alice@corp.io")
    draft = pr.relay_manage("draft", params)
    assert draft["verdict"] == pr.REDACT
    assert draft["target"] == "ops [redacted-ip] [redacted-email]"
    recorded = json.dumps(_events(pr.VERDICT_CHANNEL)[-1]["payload"])
    assert "10.0.0.5" not in recorded and "alice" not in recorded
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    pr.relay_manage("send", params)
    for channel in (pr.SENDING_CHANNEL, pr.SENT_CHANNEL, pr.DRY_RUN_CHANNEL):
        dumped = json.dumps(_events(channel)[-1]["payload"])
        assert "10.0.0.5" not in dumped and "alice" not in dumped


def test_secret_in_target_blocks_and_github_refs_are_not_redacted(monkeypatch):
    blocked = pr.relay_manage("draft", _params("hello", target=f"t {GITHUB_TOKEN}"))
    assert blocked["status"] == "blocked"
    assert GITHUB_TOKEN not in json.dumps(_events(pr.VERDICT_CHANNEL)[-1]["payload"])
    monkeypatch.setitem(
        pr._ADAPTERS,
        "github",
        pr.GitHubCommentAdapter(transport_factory=lambda: _FakeGitHubTransport()),
    )
    ok = pr.relay_check(_params(channel="github", target="o/r#953"))
    assert ok["verdict"] == pr.PASS and ok["target"] == "o/r#953"


# --- security review (PR #953) -----------------------------------------------------


@pytest.mark.parametrize(
    "credential",
    [
        "Bearer " + "abc" + "dummy" + "EfghIJKLmnoPQRSTuvw",
        "api" + "_key = '" + "my" + "placeholder" + "Value12345'",
        "tok" + "en: " + "example" + "Q7r2T6w8Y3c5",
        "DB_PASS" + "WORD=" + "your" + "Secret99",
        "gh" + "p_" + "changeme" + _FILL,
    ],
    ids=["bearer-dummy", "assigned-placeholder", "token-example", "env-your", "github-changeme"],
)
def test_embedded_placeholder_marker_still_blocks(credential):
    result = _gate(f"use {credential} here")
    assert result.verdict == pr.BLOCK, credential
    assert "secret" in _categories(result)


def test_sender_identity_and_tenant_are_bound_into_the_approval(monkeypatch):
    from unified_agent import config, edition

    params = _params("Released, thanks!")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        draft = pr.relay_manage("draft", params)
    finally:
        config.agent_identity_var.reset(token)
    payload = approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"][0]["payload"]
    assert payload["sender"] == "Desktop_Agent1"
    assert "org_id" in payload
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")

    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ApprovalPendingError):
            pr.relay_manage("send", params)
    finally:
        config.agent_identity_var.reset(token)

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-other")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ApprovalPendingError):
            pr.relay_manage("send", params)
    finally:
        config.agent_identity_var.reset(token)

    monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        assert pr.relay_manage("send", params)["status"] == "sent"
    finally:
        config.agent_identity_var.reset(token)


def test_fresh_approval_cannot_repost_after_a_recording_gap(monkeypatch):
    transport = _FakeGitHubTransport()
    params, first_approval = _approved_github(monkeypatch, transport)
    _failing_record(monkeypatch, {pr.SENT_CHANNEL})
    out = pr.relay_manage("send", params)
    assert out["recording_gap"] is True
    assert _events("audit.evidence.gap")[-1]["causal_parent_id"] == out["attempt_id"]

    draft = pr.relay_manage("draft", params)
    assert draft["approval_id"] != first_approval
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    with pytest.raises(ToolError, match="not posted again"):
        pr.relay_manage("send", params)
    assert len(transport.calls) == 1


def test_recorded_delivery_does_not_block_a_fresh_approval(monkeypatch):
    transport = _FakeGitHubTransport()
    params, _ = _approved_github(monkeypatch, transport)
    pr.relay_manage("send", params)
    draft = pr.relay_manage("draft", params)
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    pr.relay_manage("send", params)
    assert len(transport.calls) == 2


# --- full-diff review (PR #953 at f7a892a0) ------------------------------------------


@contextlib.contextmanager
def _as(identity: str | None):
    """Run as a verified caller (the identity the HTTP middleware would bind)."""
    from unified_agent import config

    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _approve_draft(params: dict) -> int:
    draft = pr.relay_manage("draft", params)
    assert draft["status"] == "pending", draft
    approval_resolve(draft["approval_id"], "approve", resolved_by="Operator")
    return draft["approval_id"]


def _github(monkeypatch, transport) -> dict:
    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: transport)
    )
    return _params("Shipped, thanks for the report.", channel="github", target="o/r#21")


def test_uncertain_github_send_is_reconciled_by_marker_not_reposted(monkeypatch):
    transport = _FakeGitHubTransport(fail_after_post=TimeoutError("read timed out"))
    params = _github(monkeypatch, transport)
    _approve_draft(params)
    with pytest.raises(ToolError, match="unknown outcome"):
        pr.relay_manage("send", params)
    assert len(transport.calls) == 1
    assert _events(pr.SEND_UNCERTAIN_CHANNEL)[-1]["payload"]["error_type"] == "TimeoutError"
    assert _events(pr.SEND_FAILED_CHANNEL) == []

    # Re-draft, re-approve, re-send: the marker is found, nothing is posted.
    transport.fail_after_post = None
    _approve_draft(params)
    with pytest.raises(ToolError, match="already delivered"):
        pr.relay_manage("send", params)
    assert len(transport.calls) == 1
    reconciled = _events(pr.SENT_CHANNEL)[-1]["payload"]
    assert reconciled["reconciled"] is True and reconciled["receipt"]["comment_id"] == "700"


def test_uncertain_send_without_a_marker_hit_stays_open_until_an_operator_resolves(monkeypatch):
    from urllib.error import URLError

    class _DropsBeforePost(_FakeGitHubTransport):
        def create_evidence_comment(self, **kwargs):
            raise URLError("connection reset")

    transport = _DropsBeforePost()
    params = _github(monkeypatch, transport)
    _approve_draft(params)
    with pytest.raises(ToolError, match="unknown outcome"):
        pr.relay_manage("send", params)
    attempt = _events(pr.SENDING_CHANNEL)[-1]["ulid"]

    _approve_draft(params)
    with pytest.raises(ToolError, match="no recorded sent outcome"):
        pr.relay_manage("send", params)

    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="operator action"):
        pr.relay_manage("resolve_attempt", {"attempt_id": attempt, "outcome": "not_delivered"})
    with _as("Operator"):
        out = pr.relay_manage(
            "resolve_attempt", {"attempt_id": attempt, "outcome": "not_delivered"}
        )
        assert out["reposted"] is False
        with pytest.raises(ToolError, match="already has an outcome"):
            pr.relay_manage("resolve_attempt", {"attempt_id": attempt, "outcome": "delivered"})

    working = _FakeGitHubTransport()
    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: working)
    )
    _approve_draft(params)
    assert pr.relay_manage("send", params)["status"] == "sent"
    assert len(working.calls) == 1


def test_failure_classification():
    from urllib.error import HTTPError, URLError

    def http(code):
        return HTTPError("https://api.github.com/x", code, "status", {}, None)

    assert pr._classify_failure(http(422)) == "failed"
    assert pr._classify_failure(http(404)) == "failed"
    for code in (408, 429, 500, 502):
        assert pr._classify_failure(http(code)) == "uncertain", code
    assert pr._classify_failure(PermissionError("no token")) == "failed"
    assert pr._classify_failure(pr.RelayDefiniteFailure("pre-send")) == "failed"
    for exc in (TimeoutError(), URLError("x"), ConnectionResetError(), ValueError("bad json")):
        assert pr._classify_failure(exc) == "uncertain", exc


def test_zulip_unconfirmed_send_is_uncertain(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    monkeypatch.setattr(
        pr._zulip,
        "zulip_send_message",
        lambda **kw: {"sent": False, "error": "Zulip API request failed"},
    )
    params = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    _approve_draft(params)
    with pytest.raises(ToolError, match="unknown outcome"):
        pr.relay_manage("send", params)
    assert _events(pr.SENT_CHANNEL) == []
    assert _events(pr.SEND_UNCERTAIN_CHANNEL)[-1]["payload"]["error_type"] == (
        "RelayOutcomeUncertain"
    )


def test_zulip_pre_send_refusal_is_a_definite_failure(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")

    def refuse(**kw):
        raise pr._zulip.ZulipRelayError("topic must not be empty")

    monkeypatch.setattr(pr._zulip, "zulip_send_message", refuse)
    params = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    _approve_draft(params)
    with pytest.raises(ToolError, match="failed before reaching it"):
        pr.relay_manage("send", params)
    assert len(_events(pr.SEND_FAILED_CHANNEL)) == 1


_SHAPES = {
    "github-pat": "github" + "_pat_" + "11ABCDEFG0" + _FILL,
    "ghp-underscore-wrapped": "_" + "gh" + "p_" + _FILL + "Ab12" + "_",
    "ghp-after-equals": "TOKEN=" + "gh" + "o_" + _FILL + "Ab12",
    "aws-asia": "AS" + "IA" + "Q7R2T6W8Y3C5F1H0",
    "aws-secret": "aws_secret" + "_access_key = " + "wJalrXUtnFEMI" + "K7MDENGbPxRfiCY",
    "credential-url": "postgres://" + "admin" + ":" + "Hunter2pass" + "@db.corp.io/app",
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTYifQ." + "SflKxwRJSMeKKF2QT4fw",
    "stripe": "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "slack-app": "xa" + "pp-1-A0123456789-" + "1234567890123",
    "slack-webhook": "https://hooks.slack" + ".com/services/" + "T000/B000/Qz9Lm4Np7Rs2",
    "google-oauth": "ya" + "29." + "a0AfH6SMBx" + "Zq9Lm4Np7",
    "basic-auth": "Authorization: Basic " + "dXNlcjpwYXNz" + "d29yZDEy",
    "pem-encrypted": "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
    "pem-lowercase": "-----begin " + "rsa private key-----",
    "putty": "PuTTY-User-" + "Key-File-3: ssh-ed25519",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_each_credential_shape_blocks(shape):
    value = _SHAPES[shape]
    result = _gate(f"see {value} above")
    assert result.verdict == pr.BLOCK, shape
    assert _categories(result) & {"secret", "private_key"}, shape
    assert value not in json.dumps(result.to_dict())


def test_shared_shapes_reach_redaction_and_audit():
    from unified_agent.audit import rules as audit_rules
    from unified_agent.telemetry import redaction

    token = _SHAPES["github-pat"]
    assert token not in redaction.redact_sensitive_content(f"x {token} y", max_len=500)
    found = list(audit_rules.check_secrets("src/app.py", f"T = '{_SHAPES['stripe']}'\n"))
    assert [f.rule for f in found] == ["SECRETS-KEY"]
    assert _gate("Basic authentication is supported").verdict == pr.PASS


@pytest.mark.parametrize(
    "value",
    ["$S3cretValue", "${S3cretValue", "<S3cretValue>", "redacted_HunterSecret"],
)
def test_lookalike_placeholders_still_block(value):
    assert _gate("DB_PASS" + "WORD=" + value).verdict == pr.BLOCK, value


@pytest.mark.parametrize("char", ["⁦", "⁩", "­", "‌", "‍", "᠎", "️", "\U000e0101"])
def test_hidden_format_characters_block(char):
    result = _gate(f"looks{char}fine")
    assert result.verdict == pr.BLOCK
    assert _categories(result) == {"hidden_unicode"}


def test_invisible_character_inside_a_token_does_not_hide_it():
    token = "gh" + "p_" + "​" + _FILL + "Ab12"
    result = _gate(f"use {token}")
    assert result.verdict == pr.BLOCK
    assert any(f.category == "secret" and f.detail for f in result.findings)


def test_concurrent_sends_of_the_same_content_post_once(monkeypatch):
    import threading
    import time

    real = pr._unresolved_attempt_on
    first = threading.Event()
    b_done = threading.Event()

    class _HoldingDryRun(pr.DryRunAdapter):
        # Keep A's attempt open (no sent record yet) until B has been decided.
        def send(self, target, text, context):
            b_done.wait(5)
            return super().send(target, text, context)

    monkeypatch.setitem(pr._ADAPTERS, "dry_run", _HoldingDryRun())

    def slow_check(conn, fingerprint):
        if not first.is_set():
            first.set()
            time.sleep(0.5)  # hold the write transaction open
        return real(conn, fingerprint)

    monkeypatch.setattr(pr, "_unresolved_attempt_on", slow_check)
    a = _params("same words", requested_by="Desktop_Agent1")
    b = _params("same words", requested_by="Desktop_Agent2")
    _approve_draft(a)
    _approve_draft(b)
    results: dict[str, object] = {}

    def run(name, params):
        try:
            results[name] = pr.relay_manage("send", params)["status"]
        except ToolError as exc:
            results[name] = str(exc)
        finally:
            if name == "b":
                b_done.set()

    ta = threading.Thread(target=run, args=("a", a))
    ta.start()
    assert first.wait(5)
    tb = threading.Thread(target=run, args=("b", b))
    tb.start()
    ta.join(10)
    tb.join(10)
    assert results["a"] == "sent"
    assert "not posted again" in str(results["b"])
    assert len(_events(pr.DRY_RUN_CHANNEL)) == 1


def test_no_excerpt_overlaps_a_blocked_term(monkeypatch):
    monkeypatch.setenv("UA_RELAY_BLOCKED_TERMS", "acme")
    result = pr.relay_check(_params("write to acme.ops@corp.io today"))
    assert result["verdict"] == pr.BLOCK
    excerpts = " ".join(f["excerpt"] for f in result["findings"])
    assert "ac" not in excerpts.lower()


def test_length_is_checked_after_redaction():
    text = "at 10.0.0.1 10.0.0.2 10.0.0.3"
    result = _gate(text, max_chars=len(text))
    assert result.verdict == pr.BLOCK
    assert "length" in _categories(result)


def test_zulip_topic_length_and_published_target_findings(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(pr._zulip, "MAX_TOPIC_CHARS", 10)
        with pytest.raises(ToolError, match="topic exceeds"):
            pr.relay_manage("check", _params(channel="zulip", target="general/" + "t" * 11))
    published = pr.relay_check(
        _params(channel="zulip", target="general/ping 10.0.0.5", context={"agent_label": "x"})
    )
    assert published["verdict"] == pr.BLOCK
    private_target = pr.relay_check(_params(target="ops 10.0.0.5"))
    assert private_target["verdict"] == pr.REDACT


def test_public_false_cannot_disable_id_redaction_on_public_channels():
    result = pr.relay_check(_params("tracked in #1734", public=False))
    assert result["public"] is True
    assert result["text"] == "tracked in [internal-ref]"


# --- PR #953 Devin threads + tenant fence ----------------------------------------------


class _DropsBeforePost(_FakeGitHubTransport):
    def create_evidence_comment(self, **kwargs):
        from urllib.error import URLError

        raise URLError("connection reset")


def _open_attempt(monkeypatch, text="Shipped, thanks for the report.") -> tuple[dict, str]:
    """A delivery attempt left open by an uncertain send."""
    params = _github(monkeypatch, _DropsBeforePost())
    params["text"] = text
    _approve_draft(params)
    with pytest.raises(ToolError, match="unknown outcome"):
        pr.relay_manage("send", params)
    return params, _events(pr.SENDING_CHANNEL)[-1]["ulid"]


def _outcomes() -> list[dict]:
    return _events(pr.SENT_CHANNEL) + _events(pr.SEND_FAILED_CHANNEL)


def _resolve(attempt: str, outcome: str = "not_delivered", **extra) -> dict:
    return pr.relay_manage("resolve_attempt", {"attempt_id": attempt, "outcome": outcome, **extra})


def test_resolution_needs_a_verified_human_not_a_claimed_one(monkeypatch):
    from unified_agent import config

    _, attempt = _open_attempt(monkeypatch)
    # Open mode, nobody authenticated: a claimed human name is not enough.
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    assert config.verified_agent_identity() is None
    with pytest.raises(ToolError, match="verified human identity"):
        _resolve(attempt, resolved_by="Operator")
    # Enforced mode, nobody authenticated.
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with pytest.raises(ToolError, match="verified human identity"):
        _resolve(attempt, resolved_by="Operator")
    # A verified agent cannot name a human to resolve its own attempt.
    with _as("Desktop_Agent1"):
        with pytest.raises(ToolError, match="operator action"):
            _resolve(attempt, resolved_by="Operator")
        with pytest.raises(ToolError, match="operator action"):
            _resolve(attempt)
    # A verified human cannot be impersonated by a different claimed name.
    with _as("Operator"), pytest.raises(ToolError, match="does not match the verified"):
        _resolve(attempt, resolved_by="Desktop_Agent2")
    assert _outcomes() == []

    with _as("Operator"):
        out = _resolve(attempt, "delivered", reason="seen on the thread")
    assert out["reposted"] is False
    recorded = _events(pr.SENT_CHANNEL)[-1]
    assert recorded["causal_parent_id"] == attempt
    assert recorded["payload"]["resolved_by"] == "Operator"
    assert recorded["originator"] == "Operator"


def test_org_b_resolver_cannot_close_an_org_a_attempt(monkeypatch):
    from unified_agent import edition

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-a")
    params, attempt = _open_attempt(monkeypatch)
    assert _events(pr.SENDING_CHANNEL)[-1]["ulid"] == attempt

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-b")
    with _as("Operator"):
        for outcome in ("delivered", "not_delivered"):
            with pytest.raises(ToolError, match="no relay delivery attempt"):
                _resolve(attempt, outcome)
    assert _outcomes() == []

    # The fence also hides org A's open attempt from org B's own sends: the same
    # text to the same target is not blocked there, while org A is still blocked.
    working = _FakeGitHubTransport()
    monkeypatch.setitem(
        pr._ADAPTERS, "github", pr.GitHubCommentAdapter(transport_factory=lambda: working)
    )
    _approve_draft(params)
    assert pr.relay_manage("send", params)["status"] == "sent"
    assert len(working.calls) == 1

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-a")
    _approve_draft(params)
    with pytest.raises(ToolError, match="no recorded sent outcome"):
        pr.relay_manage("send", params)
    with _as("Operator"):
        assert _resolve(attempt)["attempt_id"] == attempt
    closing = _events(pr.SEND_FAILED_CHANNEL)
    assert [e["causal_parent_id"] for e in closing] == [attempt]


def test_sending_dedupe_key_is_tenant_scoped(monkeypatch):
    from unified_agent import edition

    keys = {}
    for org in (None, "org-a", "org-b"):
        monkeypatch.setattr(edition, "tenant_org_id", lambda org=org: org)
        keys[org] = pr._sending_dedupe_key(7)
    assert len(set(keys.values())) == 3
    assert all(key.endswith(":7") for key in keys.values())


def test_concurrent_resolutions_record_exactly_one_outcome(monkeypatch):
    _, attempt = _open_attempt(monkeypatch)
    real = pr._attempt_in_flight
    barrier = threading.Barrier(2)

    def slow(conn, attempt_id, sending):
        # Inside the closing transaction: hold it open so a non-atomic
        # check-then-append would let the other resolver in.
        time.sleep(0.4)
        return real(conn, attempt_id, sending)

    monkeypatch.setattr(pr, "_attempt_in_flight", slow)
    results: dict[str, object] = {}

    def run(name, outcome):
        with _as("Operator"):
            barrier.wait(5)
            try:
                results[name] = _resolve(attempt, outcome)["outcome"]
            except ToolError as exc:
                results[name] = str(exc)

    threads = [
        threading.Thread(target=run, args=("a", "delivered")),
        threading.Thread(target=run, args=("b", "not_delivered")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert len(_outcomes()) == 1, results
    winners = [v for v in results.values() if v in ("delivered", "not_delivered")]
    losers = [v for v in results.values() if v not in ("delivered", "not_delivered")]
    assert len(winners) == 1 and len(losers) == 1
    assert "already has an outcome" in losers[0]


def test_closing_outcome_is_unique_per_attempt_even_without_the_precheck(monkeypatch):
    _, attempt = _open_attempt(monkeypatch)
    with _as("Operator"):
        _resolve(attempt, "not_delivered")
    # The dedupe key alone refuses a second closing event for the attempt.
    with pytest.raises(ToolError, match="already has an outcome"):
        pr._close_attempt(attempt, pr.SENT_CHANNEL, {"channel": "github"}, "Operator")
    assert len(_outcomes()) == 1


class _BlockingDryRun(pr.DryRunAdapter):
    """Holds the request open until released, like a slow channel response."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def send(self, target, text, context):
        self.calls += 1
        self.started.set()
        self.release.wait(10)
        return super().send(target, text, context)


def test_operator_cannot_close_an_attempt_that_is_still_sending(monkeypatch):
    adapter = _BlockingDryRun()
    monkeypatch.setitem(pr._ADAPTERS, "dry_run", adapter)
    params = _params("slow reply")
    _approve_draft(params)
    results: dict[str, object] = {}

    def first_send():
        try:
            results["a"] = pr.relay_manage("send", params)["status"]
        except ToolError as exc:
            results["a"] = str(exc)

    sender = threading.Thread(target=first_send)
    sender.start()
    try:
        assert adapter.started.wait(5)
        attempt = _events(pr.SENDING_CHANNEL)[-1]["ulid"]
        with _as("Operator"):
            for outcome in ("not_delivered", "delivered"):
                with pytest.raises(ToolError, match="still being delivered"):
                    _resolve(attempt, outcome)
        # So no fresh approval can post the same reply while the first is live.
        _approve_draft(params)
        with pytest.raises(ToolError, match="still being sent"):
            pr.relay_manage("send", params)
        assert _outcomes() == []
    finally:
        adapter.release.set()
        sender.join(10)
    assert results["a"] == "sent"
    assert adapter.calls == 1
    assert len(_events(pr.DRY_RUN_CHANNEL)) == 1
    assert len(_outcomes()) == 1


def test_lease_blocks_resolution_until_it_expires_after_a_crash(monkeypatch):
    # A sending record with no adapter running in this process (a hub restart).
    attempt = events.record_event(
        pr.SENDING_CHANNEL,
        payload={
            "channel": "dry_run",
            "target": "issue-42",
            "fingerprint": "crashed",
            "lease_expires_at": time.time() + pr.SEND_LEASE_SECONDS,
        },
    )
    with _as("Operator"):
        with pytest.raises(ToolError, match="still being delivered"):
            _resolve(attempt)
        monkeypatch.setattr(pr, "_now_ts", lambda: time.time() + pr.SEND_LEASE_SECONDS + 5)
        assert _resolve(attempt)["outcome"] == "not_delivered"


def test_racing_senders_deliver_exactly_once(monkeypatch):
    adapter = _BlockingDryRun()
    monkeypatch.setitem(pr._ADAPTERS, "dry_run", adapter)
    monkeypatch.setattr(pr, "resolve_relay_autosend", lambda channel, ctx: True)
    params = _params("race me")
    barrier = threading.Barrier(4)
    results: list[object] = []

    def run():
        barrier.wait(5)
        try:
            results.append(pr.relay_manage("draft", params)["status"])
        except ToolError as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    assert adapter.started.wait(5)
    time.sleep(0.3)  # the losers are decided while the winner is still in flight
    adapter.release.set()
    for t in threads:
        t.join(15)
    assert adapter.calls == 1, results
    assert results.count("sent") == 1
    assert sum("not posted again" in str(r) for r in results) == 3
    assert len(_events(pr.SENDING_CHANNEL)) == 1
    assert len(_events(pr.SENT_CHANNEL)) == 1


def test_zulip_identity_mismatch_is_refused_before_anything_is_recorded(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")

    def never(**kw):
        raise AssertionError("no request may be built for a mismatched identity")

    monkeypatch.setattr(pr._zulip, "zulip_send_message", never)
    params = _params(
        channel="zulip", target="general/release", context={"agent_label": "Desktop_Agent1"}
    )
    with _as("Desktop_Agent2"):
        with pytest.raises(ToolError, match="identity mismatch"):
            pr.relay_manage("draft", params)
        with pytest.raises(ToolError, match="identity mismatch"):
            pr.relay_manage("send", params)
    assert _events(pr.SENDING_CHANNEL) == []
    assert approval_pending(tool=pr.RELAY_APPROVAL_TOOL)["pending"] == []


def test_zulip_adapter_mismatch_is_a_definite_not_sent_failure(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    monkeypatch.setattr(
        pr._zulip, "zulip_send_message", lambda **kw: pytest.fail("no request expected")
    )
    adapter = pr.ZulipStreamAdapter()
    with _as("Desktop_Agent2"):
        with pytest.raises(pr.RelayDefiniteFailure, match="identity mismatch") as caught:
            adapter.send("general/release", "hi", {"agent_label": "Desktop_Agent1"})
    assert pr._classify_failure(caught.value) == "failed"


def test_identity_that_turns_mismatched_after_approval_closes_the_attempt(monkeypatch):
    # The adapter refuses locally after the intent was recorded: the attempt is
    # closed as not sent, so a fresh approval for the same text is not blocked.
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    sent = []
    monkeypatch.setattr(
        pr._zulip,
        "zulip_send_message",
        lambda **kw: sent.append(kw) or {"sent": True, "message_id": 5},
    )
    params = _params(channel="zulip", target="general/release", context={"agent_label": "x"})
    _approve_draft(params)
    real = pr.ZulipStreamAdapter.unavailable_reason
    calls = {"n": 0}

    def flaky(self, context):
        calls["n"] += 1
        return real(self, context) if calls["n"] <= 1 else "Auth identity mismatch: test"

    monkeypatch.setattr(pr.ZulipStreamAdapter, "unavailable_reason", flaky)
    with pytest.raises(ToolError, match="failed before reaching it"):
        pr.relay_manage("send", params)
    assert sent == []
    failed = _events(pr.SEND_FAILED_CHANNEL)
    assert len(failed) == 1
    assert failed[0]["causal_parent_id"] == _events(pr.SENDING_CHANNEL)[-1]["ulid"]
    monkeypatch.setattr(pr.ZulipStreamAdapter, "unavailable_reason", real)
    _approve_draft(params)
    assert pr.relay_manage("send", params)["status"] == "sent"


_SIGNED_SECRET = "Zq9Lm4Np7Rs2Tv6Wx8Yb3Cd5Fg1Hj0Kk"
_SIGNED_URLS = {
    "s3-signature": "https://bucket.s3.amazonaws.com/f.png?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Signature=" + _SIGNED_SECRET,
    "s3-credential": "https://b.s3.amazonaws.com/f?" + "X-Amz-Credential=" + _SIGNED_SECRET,
    "gcs-signature": "https://storage.googleapis.com/b/o?X-Goog-Signature=" + _SIGNED_SECRET,
    "azure-sas": "https://acct.blob.core.windows.net/c/b?sv=2022&" + "sig=" + _SIGNED_SECRET,
    "access-token": "https://api.example.org/v1/x?" + "access_token=" + _SIGNED_SECRET,
    "entity-encoded": "https://h.example.org/p?a=1&amp;" + "signature=" + _SIGNED_SECRET,
    "uppercase-name": "https://h.example.org/p?" + "TOKEN=" + _SIGNED_SECRET,
}


@pytest.mark.parametrize("name", sorted(_SIGNED_URLS))
def test_signed_url_query_credentials_block_and_redact(name):
    from unified_agent.telemetry import redaction

    url = _SIGNED_URLS[name]
    result = _gate(f"download it from {url} please")
    assert result.verdict == pr.BLOCK, name
    assert _SIGNED_SECRET not in json.dumps(result.to_dict())
    redacted = redaction.redact_sensitive_content(f"failed: {url}", 500)
    assert _SIGNED_SECRET not in redacted
    assert redacted.startswith("failed: https://")


def test_ordinary_url_queries_still_pass():
    text = "see https://docs.example.org/page?id=42&sort=asc&token=abc and ?auth=true"
    assert _gate(text).verdict == pr.PASS


def test_signed_url_in_adapter_error_is_redacted_before_truncation(monkeypatch):
    url = "https://bucket.s3.amazonaws.com/f?" + "X-Amz-Signature=" + _SIGNED_SECRET

    class _LeakyTransport(_FakeGitHubTransport):
        def create_evidence_comment(self, **kwargs):
            # The signature straddles the 300-character record limit.
            raise TimeoutError("x" * 270 + " " + url)

    params = _github(monkeypatch, _LeakyTransport())
    _approve_draft(params)
    with pytest.raises(ToolError, match="unknown outcome") as caught:
        pr.relay_manage("send", params)
    recorded = json.dumps(_events(pr.SEND_UNCERTAIN_CHANNEL)[-1]["payload"])
    for text in (recorded, str(caught.value)):
        assert _SIGNED_SECRET[:8] not in text
        assert "X-Amz-Signature=" not in text


def test_audit_scan_leaves_query_urls_to_runtime_redaction():
    from unified_agent.audit import rules as audit_rules

    line = "URL = 'https://h.example.org/p?" + "sig=" + _SIGNED_SECRET + "'\n"
    assert list(audit_rules.check_secrets("tests/fixture.py", line)) == []


def test_short_oct_key_is_still_private_material():
    from unified_agent.telemetry import redaction

    text = json.dumps(dict(kty="oct", k="".join(("A", "Q"))))  # ua:allow
    assert _gate(text).verdict == pr.BLOCK
    assert json.loads(redaction.redact_sensitive_content(text))["k"] == "[REDACTED_SECRET]"


# --- JWK audit repairs (PR #1147 review) ---------------------------------------


def test_jwk_audit_is_linear_in_the_file_size():
    from growth_budget import assert_cpu_growth, cold

    from unified_agent.audit import rules

    # Recounting newlines from the start of the file per member took 10 s at
    # the large size: quadratic, so x4 input cost about x16. A wall-clock
    # bound (< 4 s) failed on a loaded CI runner at 4.11 s with linear code
    # (task #2188), so growth is judged on CPU time, as the redactor tests do.
    line = '{"kty":"oct","k":"Ab1_-Cd2"}\n'  # ua:allow
    small, large = line * 8_000, line * 32_000
    assert len(large) > 900_000

    def scan(text):
        return list(rules.check_secrets("fixture.json", text))

    assert_cpu_growth(cold(scan), small, large, limit=8.0, samples=2, label="jwk audit")
    findings = scan(large)
    assert [f.line for f in findings[:3]] == [1, 2, 3] and len(findings) == 32_000


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n", "\x0b", " "])
def test_jwk_audit_lines_follow_the_splitlines_model(newline):
    from unified_agent.audit import rules

    text = newline.join(["# ua:allow", "{", '  "kty": "oct",', '  "k": "' + _B64U + '"', "}"])
    [finding] = list(rules.check_secrets("fixture.json", text))
    assert finding.line == 4
    assert text.splitlines()[finding.line - 1].startswith('  "k"')


@pytest.mark.parametrize(
    "value",
    ["your" + _B64U, _B64U + "dummy", "example-" + _B64U, "REDACTED" + _B64U],
)
def test_a_jwk_value_merely_containing_a_placeholder_word_is_reported(value):
    from unified_agent.audit import rules

    text = '{"kty": "oct", "k": "' + value + '"}'
    assert len(list(rules.check_secrets("fixture.json", text))) == 1


@pytest.mark.parametrize(
    "value", ["", "<your-key>", "${JWK_SECRET}", "$JWK", "changeme", "****", "REDACTED"]
)
def test_an_exact_placeholder_jwk_value_is_not_reported(value):
    from unified_agent.audit import rules

    text = '{"kty": "oct", "k": "' + value + '"}'
    assert list(rules.check_secrets("fixture.json", text)) == []


def test_many_jwk_findings_gate_in_linear_time():
    # PR #1147 review round 2: deduplication compared each finding with every
    # finding kept before it (16k small JWKs took 10 s).
    import time

    text = '{"kty":"oct","k":"Ab1_-Cd2"} ' * 16_000  # ua:allow
    started = time.monotonic()
    result = _gate(text)
    assert time.monotonic() - started < 6.0
    assert result.verdict == pr.BLOCK
    assert sum(f.category == "private_key" for f in result.findings) == 16_000


def test_dedupe_keeps_order_and_drops_only_covered_hits():
    f = pr.Finding
    findings = [
        f("a", pr.REDACT, 5, 8, "x"),
        f("b", pr.BLOCK, 0, 10, "x"),
        f("c", pr.BLOCK, 12, 14, "x"),
        f("d", pr.BLOCK, 13, 20, "x"),
        f("e", pr.REDACT, 15, 18, "x"),
        f("g", pr.BLOCK, 21, 21, "x"),
        f("h", pr.REDACT, 21, 21, "x"),
    ]
    kept = [k.category for k in pr._dedupe(findings)]
    assert kept == ["b", "c", "d", "g", "h"]  # an empty hit covers nothing


# --- PR #1147 review round 3 -------------------------------------------------------

_REAL_PRIME = _B64U + "P"


def _wrapped_rsa(first: str) -> str:
    key = {
        "kty": "RSA",
        "n": _B64U,
        "e": "AQAB",
        "d": first,  # ua:allow
        "p": _REAL_PRIME,  # ua:allow
        "q": _REAL_PRIME,  # ua:allow
    }  # ua:allow
    return json.dumps({"body": json.dumps(key)})


@pytest.mark.parametrize("first", ["[REDACTED]", "REDACTED", "${JWK_D}"])
def test_an_encoded_jwk_is_exempt_only_if_every_private_value_is_a_placeholder(first):
    from unified_agent.audit import rules

    text = _wrapped_rsa(first)
    assert _gate(text).verdict == pr.BLOCK
    assert len(list(rules.check_secrets("fixture.json", text))) == 1
    placeholders = json.dumps({"body": json.dumps({"kty": "oct", "k": "REDACTED"})})
    assert "private_key" not in _categories(_gate(placeholders))
    assert list(rules.check_secrets("fixture.json", placeholders)) == []


@pytest.mark.parametrize(
    "text",
    [
        "JWK = {'kty': 'oct', 'k': ('" + _B64U[:30] + "' '" + _B64U[30:] + "')}",  # ua:allow
        "JWK = {'kty': 'oct', 'k': r'" + _B64U + "'}",  # ua:allow
        'JWK = {"kty": "EC", "d": "' + _B64U[:30] + '" + "' + _B64U[30:] + '"}',
    ],
    ids=["implicit-concat", "raw-prefix", "plus"],
)
def test_a_private_member_expression_blocks_and_is_audited(text):
    from unified_agent.audit import rules

    result = _gate(text)
    assert result.verdict == pr.BLOCK and "private_key" in _categories(result)
    assert _B64U[:30] not in json.dumps(result.to_dict())
    assert len(list(rules.check_secrets("fixture.py", text))) == 1
