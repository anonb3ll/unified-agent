"""Fresh-install operator behaviour, with no legacy identity pins.

conftest pins the suite's historical operator so older fixtures keep their
meaning. This module removes those pins and drives the main human-in-the-loop
paths (approval, evidence, dispatch attribution, profile, bearer lookup) with
the neutral defaults a new install gets.
"""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import bootstrap, config, personas, profiles, storage
from unified_agent.audit import evidence_report
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)

# Any fleet-style human label other than the configured one.
OTHER_LABEL = "Desktop_Someone"


@pytest.fixture(autouse=True)
def fresh_install(isolated_env, monkeypatch):
    for name in ("UA_OPERATOR_LABEL", "UA_OPERATOR_NAME", "UA_OPERATOR_ALIASES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        "host_agent:tok-agent-fresh-install,Desktop_Operator:tok-operator-fresh-install",
    )
    storage.init_db()


def _as(label):
    return config.agent_identity_var.set(label)


def _park_run_cmd(task_id: int) -> str:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "source_host, claimed_by, notes, project, domain, lane) VALUES "
            "(?, 'fresh install', 'doing', 'high', '2026-10-01T00:00:00+00:00', "
            "'2026-10-01T00:00:00+00:00', 'test-device.local', 'host_agent', '', "
            "'unified-agent', 'hobby', 'default')",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()
    payload = {"command": "echo fresh", "task_id": task_id}
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", payload, requested_by="host_agent")
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), rid))
        conn.commit()
    finally:
        conn.close()
    return rid


def test_default_operator_approves_and_agents_and_old_label_cannot():
    rid = _park_run_cmd(4242)

    for impostor in ("host_agent", OTHER_LABEL):
        token = _as(impostor)
        try:
            with pytest.raises(ToolError, match="human identity|separation of duties"):
                approval_resolve(rid, "approve")
        finally:
            config.agent_identity_var.reset(token)

    token = _as("Desktop_Operator")
    try:
        row = approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(token)
    assert row["resolved_by"] == "Desktop_Operator"

    rec = evidence_report.build_evidence_report(task_id=4242)["records"][0]
    assert rec["human"] == "Desktop_Operator"
    assert rec["human_provenance"]["actor_kind"] == "human"


def test_default_operator_profile_persona_and_bearer():
    assert profiles.default_for_identity("Desktop_Operator") == profiles.OPERATOR
    assert profiles.default_for_identity(OTHER_LABEL) == profiles.DEVELOPER
    assert personas.get_persona("Desktop_Operator")["display_name"] == "Operator"
    whoami = bootstrap._whoami_payload("tok-operator-fresh-install")
    assert (whoami["agent"], whoami["profile"]) == ("Desktop_Operator", profiles.OPERATOR)
    assert bootstrap._whoami_payload("tok-agent-fresh-install")["profile"] != profiles.OPERATOR


def test_dispatch_from_control_center_login_is_attributed_to_operator():
    from unified_agent.crew import resolve_from_agent
    from unified_agent.webui import api

    assert api.dispatch_from_agent("Desktop_Operator") == "Operator"
    assert resolve_from_agent("Desktop_Operator") == "Operator"
    assert api.dispatch_from_agent(OTHER_LABEL) == OTHER_LABEL

    token = _as("Desktop_Operator")
    try:
        assert config.verify_write_identity("Operator") == "Operator"
    finally:
        config.agent_identity_var.reset(token)


def test_tui_uses_the_operator_label_token_not_another_identity(tmp_path, monkeypatch):
    from unified_agent import tui

    monkeypatch.delenv("UA_TUI_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude.json to read a bearer from
    # The TUI attributes work to the bare name; the bearer is the label's.
    assert tui.resolve_tui_bearer_token(config.operator_name()) == "tok-operator-fresh-install"
    assert tui.resolve_tui_bearer_token(None) == "tok-operator-fresh-install"
    # An unknown label must not borrow another identity's token.
    assert tui.resolve_tui_bearer_token("someone_else") is None
    monkeypatch.setenv("UA_AGENT_TOKENS", "only_one:tok-single")
    assert tui.resolve_tui_bearer_token("someone_else") == "tok-single"
