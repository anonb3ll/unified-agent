"""Slack as the reference connector implementation (task #1204).

Runs the generic five-clause harness against ``connectors.SLACK_MANIFEST``,
then proves clause 3 ("refuses to start without its named secrets") a second
time against Slack's *actual* runtime code — not just the generic
manifest-level gate — because Slack already enforced this before this task
existed, and the manifest must describe that real behavior rather than
invent new enforcement.
"""

from __future__ import annotations

import pytest

from unified_agent import connectors
from unified_agent.slack_gateway import FrontierPolicy, SlackConfig, SlackGatewayError
from unified_agent.slack_outbound import SlackOutboundError, send_message

from .harness import (
    _stub_approvals,
    assert_connector_conforms,
    assert_emits_hash_chained_lifecycle_events,
    assert_invalid_manifest_refused,
    assert_invisible_to_profiles_lacking_it,
    assert_refuses_to_start_without_secrets,
    assert_registers,
    assert_reports_to_doctor_and_pane,
    verified_identity,
)

SLACK_MANIFEST = connectors.require_manifest("slack")


@pytest.fixture(autouse=True)
def _slack_secrets(monkeypatch):
    """Baseline: both named secrets present, so tests opt into removing one."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret-not-real")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-token-not-real")
    yield


def test_slack_conforms_end_to_end(monkeypatch):
    assert_connector_conforms(SLACK_MANIFEST, monkeypatch)


def test_slack_registers(monkeypatch):
    assert_registers(SLACK_MANIFEST, monkeypatch, scopes=["read"])


def test_slack_manifest_capabilities_are_real_code_not_the_scope_catalog():
    """Slack's manifest capabilities differ from horizon2's read/search scope

    catalog on purpose — see docs/connectors.md "Capabilities vs. scopes".
    Slack has no search consumer in this checkout, so the manifest does not
    claim one.
    """
    from unified_agent.horizon2 import CONNECTOR_PACKAGES

    assert SLACK_MANIFEST.capabilities == frozenset({"ingress", "outbound", "chat"})
    assert CONNECTOR_PACKAGES["slack"] == frozenset({"read", "search"})
    assert "search" not in SLACK_MANIFEST.capabilities


def test_slack_invalid_manifest_variant_is_refused(monkeypatch):
    assert_invalid_manifest_refused(SLACK_MANIFEST, monkeypatch)
    assert connectors.get_manifest("slack") == SLACK_MANIFEST


def test_slack_reports_to_doctor_and_pane():
    assert_reports_to_doctor_and_pane(SLACK_MANIFEST)


def test_slack_doctor_check_is_live_when_registered_and_secrets_present(monkeypatch):
    from unified_agent.tools import doctor, horizon2

    monkeypatch.setattr(
        doctor,
        "_service_env",
        lambda _unit: {
            "SLACK_SIGNING_SECRET": "test-signing-secret-not-real",
            "SLACK_BOT_TOKEN": "test-bot-token-not-real",
        },
    )
    _stub_approvals(monkeypatch)
    with verified_identity():
        horizon2.horizon2_server_register(
            "slack-doctor-live",
            SLACK_MANIFEST.name,
            "sha256:abcdef0123456789",
            ["read"],
            "review:slack-doctor-live",
        )
    report = doctor.system_doctor()
    check = next(c for c in report["checks"] if c["capability"] == "connector:slack")
    assert check["status"] == "LIVE"

    package = next(
        p for p in horizon2.horizon2_package_list()["packages"] if p["package"] == "slack"
    )
    assert package["secrets"]["satisfied"] is True
    assert package["manifest"]["capabilities"] == ["chat", "ingress", "outbound"]


def test_slack_generic_start_gate_refuses_without_secrets(monkeypatch):
    assert_refuses_to_start_without_secrets(SLACK_MANIFEST, monkeypatch)


def test_slack_ingress_refuses_without_signing_secret(monkeypatch):
    """The connector's OWN code, not just the generic manifest gate, refuses."""
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("UA_SLACK_USERS", "U123")
    monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C123")
    config = SlackConfig.from_env()
    with pytest.raises(SlackGatewayError, match="SLACK_SIGNING_SECRET is required"):
        config.validate()


def test_slack_outbound_refuses_without_bot_token(monkeypatch):
    """send_message — Slack's real outbound entry point — refuses without its token."""
    config = SlackConfig(
        signing_secret="sig",
        bot_token=None,
        app_token=None,
        channel_ids=frozenset({"C123"}),
        channel_names_by_id={},
        users=frozenset({"U123"}),
        coordinator_agent="Desktop_Agent5",
        enabled=True,
        frontier=FrontierPolicy.from_env(),
    )
    with pytest.raises(SlackOutboundError, match="SLACK_BOT_TOKEN is required"):
        send_message(
            channel_id="C123",
            text="hello",
            agent_label="Desktop_Agent5",
            dedupe_key="conformance-test-key",
            config=config,
            allow_unthreaded=True,
        )


def test_slack_emits_hash_chained_lifecycle_events(monkeypatch):
    assert_emits_hash_chained_lifecycle_events(SLACK_MANIFEST, monkeypatch)


def test_slack_administration_invisible_to_profiles_lacking_it():
    assert_invisible_to_profiles_lacking_it()
    assert_invisible_to_profiles_lacking_it("horizon2_package_register")
    assert_invisible_to_profiles_lacking_it("horizon2_connector_authorize")
