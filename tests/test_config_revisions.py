"""Immutable config revisions for policy, profiles, approval rules, and org (task #1352).

History is the evidence ledger (event_log), not a second store. Rollback is a
new revision and does not run without an approval pass.
"""

from __future__ import annotations

import secrets
import threading
from contextlib import contextmanager

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, config_revisions, events, org_registry, profiles, server, storage
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve
from unified_agent.webui.routes import web_routes


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


# A bearer for the human resolver, so approving a config_revision_* request
# (high evidence tier, task #1672) can carry its signed producer stamp.
HUMAN_BEARER = f"Desktop_Person:tok-human-{secrets.token_urlsafe(8)}"


@pytest.fixture(autouse=True)
def _human_bearer(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", HUMAN_BEARER)


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    return path


@pytest.fixture
def org_file(tmp_path, monkeypatch):
    path = tmp_path / "org.yaml"
    path.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: bench\n"
        "    name: Bench\n"
        "    lead: Desktop_Agent4\n"
        "    members: [Desktop_Agent4]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    return path


def test_commit_records_immutable_revision_on_the_evidence_chain(policy_file):
    with _as("Desktop_Agent4"):
        first = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
    assert first["unchanged"] is False
    assert first["revision"] == 1
    assert first["actor"] == "Desktop_Agent4"
    assert first["entry_hash"]
    assert first["document_sha256"]

    with _as("Desktop_Agent4"):
        second = config_revisions.commit(
            "policy",
            {
                "version": 1,
                "enabled": True,
                "default_action": "deny",
                "rules": [],
            },
        )
    assert second["revision"] == 2
    assert second["parent_revision"] == 1
    assert "default_action" in second["diff"]

    listed = config_revisions.list_revisions("policy")
    assert [row["revision"] for row in listed["revisions"]] == [2, 1]
    assert listed["drift"] is False

    snapshot = (
        storage.connect()
        .execute("SELECT payload FROM event_log WHERE ulid=?", (first["ulid"],))
        .fetchone()
    )
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": False, "default_action": "deny", "rules": []},
        )
    again = (
        storage.connect()
        .execute("SELECT payload FROM event_log WHERE ulid=?", (first["ulid"],))
        .fetchone()
    )
    assert again["payload"] == snapshot["payload"]

    chain = events.verify_event_chain()
    assert chain["status"] == "valid"
    assert chain["verified"] is True


def test_diff_and_capture_report_out_of_band_drift(policy_file):
    with _as("Desktop_Agent4"):
        config_revisions.capture("policy")
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: deny\nrules: []\n",
        encoding="utf-8",
    )
    listed = config_revisions.list_revisions("policy")
    assert listed["drift"] is True
    compared = config_revisions.diff_revisions("policy", 1, None)
    assert compared["to_revision"] == "live"
    assert "deny" in compared["diff"]
    with _as("Desktop_Agent4"):
        captured = config_revisions.capture("policy")
    assert captured["revision"] == 2
    assert captured["action"] == "capture"
    assert config_revisions.list_revisions("policy")["drift"] is False


def test_rollback_stays_governed_and_appends_a_revision(policy_file):
    original = {"version": 1, "enabled": True, "default_action": "allow", "rules": []}
    changed = {"version": 1, "enabled": True, "default_action": "deny", "rules": []}
    with _as("Desktop_Agent4"):
        config_revisions.commit("policy", original)
        config_revisions.commit("policy", changed)
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", 1)
    assert pending.value.approval_id
    assert "default_action: deny" in policy_file.read_text(encoding="utf-8")
    assert config_revisions.list_revisions("policy")["count"] == 2

    with _as("Desktop_Agent4"):
        with pytest.raises(ToolError, match="separation of duties"):
            approval_resolve(pending.value.approval_id, "approve", reason="self")
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore allow")
    with _as("Desktop_Agent4"):
        restored = config_revisions.rollback("policy", 1)
    assert restored["action"] == "rollback"
    assert restored["restores_revision"] == 1
    assert restored["revision"] == 3
    assert restored["approval_id"] == pending.value.approval_id
    loaded = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    assert loaded["default_action"] == "allow"
    revisions = config_revisions.list_revisions("policy")["revisions"]
    assert [row["revision"] for row in revisions] == [3, 2, 1]
    assert revisions[0]["action"] == "rollback"


def test_profile_overlay_cannot_escalate(policy_file):
    with _as("Desktop_Agent4"):
        config_revisions.commit("profile", {"overlay": {"Desktop_Agent4": "steward"}})
    assert profiles.default_for_identity("Desktop_Agent4") == "steward"
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="cannot change"):
            config_revisions.commit("profile", {"overlay": {"server_agent8": "developer"}})
        with pytest.raises(config_revisions.ConfigRevisionError, match="cannot change"):
            config_revisions.commit("profile", {"overlay": {"Desktop_Agent4": "operator"}})
    assert profiles.default_for_identity("server_agent8") == "public"
    assert profiles.default_for_identity("Desktop_Agent4") == "steward"
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\n"
        "profile_defaults:\n  Desktop_Agent4: operator\nrules: []\n",
        encoding="utf-8",
    )
    assert profiles.default_for_identity("Desktop_Agent4") == "developer"


def test_profile_rollback_removes_overlay(policy_file):
    with _as("Desktop_Agent4"):
        baseline = config_revisions.capture("profile")
        config_revisions.commit("profile", {"overlay": {"Desktop_Agent4": "research"}})
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("profile", baseline["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="drop overlay")
    with _as("Desktop_Agent4"):
        restored = config_revisions.rollback("profile", baseline["revision"])
    assert restored["action"] == "rollback"
    assert profiles.default_for_identity("Desktop_Agent4") == "developer"
    assert "profile_defaults" not in policy_file.read_text(encoding="utf-8")


def test_approval_rule_rollback_does_not_rewrite_env(policy_file, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_delete")
    rule = {
        "name": "gate-merge",
        "tool": "git_merge_task",
        "type": "tool",
        "action": "require_approval",
    }
    with _as("Desktop_Agent4"):
        baseline = config_revisions.capture("approval_rule")
        with pytest.raises(ApprovalPendingError) as pending_commit:
            config_revisions.commit("approval_rule", {"rules": [rule]})
    with _as("Desktop_Agent4"):
        with pytest.raises(ToolError, match="separation of duties"):
            approval_resolve(pending_commit.value.approval_id, "approve", reason="self")
    with _as("Desktop_Person"):
        approval_resolve(pending_commit.value.approval_id, "approve", reason="add rule")
    with _as("Desktop_Agent4"):
        config_revisions.commit("approval_rule", {"rules": [rule]})
        assert "gate-merge" in policy_file.read_text(encoding="utf-8")
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("approval_rule", baseline["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="drop rule")
    with _as("Desktop_Agent4"):
        restored = config_revisions.rollback("approval_rule", baseline["revision"])
    text = policy_file.read_text(encoding="utf-8")
    assert "gate-merge" not in text
    assert restored.get("note") is None
    assert config.approval_gated_tools() == {"task_delete"}


def test_org_rollback_restores_the_charter_file(org_file):
    with _as("Desktop_Agent4"):
        first = config_revisions.capture("org")
    org_file.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: bench\n"
        "    name: Bench renamed\n"
        "    lead: Desktop_Agent4\n"
        "    members: [Desktop_Agent4]\n",
        encoding="utf-8",
    )
    with _as("Desktop_Agent4"):
        config_revisions.capture("org")
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("org", first["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore name")
    with _as("Desktop_Agent4"):
        config_revisions.rollback("org", first["revision"])
    assert "Bench renamed" not in org_file.read_text(encoding="utf-8")
    assert org_registry.load_registry()[0]["name"] == "Bench"


@pytest.mark.parametrize("services", ["brain_bridge", ["bad label"], [3]])
def test_org_commit_refuses_invalid_service_identities(org_file, services):
    before = org_file.read_text(encoding="utf-8")
    with _as("Desktop_Agent4"):
        history = config_revisions.list_revisions("org")
        with pytest.raises(config_revisions.ConfigRevisionError, match="service_identities"):
            config_revisions.commit(
                "org",
                {
                    "schema_version": 1,
                    "offices": [
                        {
                            "id": "bench",
                            "name": "Bench",
                            "lead": "Desktop_Agent4",
                            "members": ["Desktop_Agent4"],
                        }
                    ],
                    "service_identities": services,
                },
            )
        assert config_revisions.list_revisions("org") == history
    assert org_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("services", ["brain_bridge", ["bad label"], [3]])
def test_org_commit_refuses_unchanged_invalid_live_chart(org_file, services):
    document = yaml.safe_load(org_file.read_text(encoding="utf-8"))
    document["service_identities"] = services
    org_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    before = org_file.read_text(encoding="utf-8")
    with _as("Desktop_Agent4"):
        history = config_revisions.list_revisions("org")
        with pytest.raises(config_revisions.ConfigRevisionError, match="service_identities"):
            config_revisions.commit("org", document)
        assert config_revisions.list_revisions("org") == history
    assert org_file.read_text(encoding="utf-8") == before


def test_org_commit_of_unchanged_valid_chart_still_records_live(org_file):
    document = yaml.safe_load(org_file.read_text(encoding="utf-8"))
    document["service_identities"] = ["brain_bridge"]
    org_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    before = org_file.read_text(encoding="utf-8")
    with _as("Desktop_Agent4"):
        result = config_revisions.commit("org", document)
        again = config_revisions.commit("org", document)
    assert org_file.read_text(encoding="utf-8") == before
    assert again["revision"] == result["revision"]


def test_org_commit_keeps_valid_service_identities(org_file):
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "org",
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": "bench",
                        "name": "Bench",
                        "lead": "Desktop_Agent4",
                        "members": ["Desktop_Agent4"],
                    }
                ],
                "service_identities": ["brain_bridge"],
            },
        )
    assert org_registry.load_service_identities() == ["brain_bridge"]


def test_commit_refuses_secret_material(policy_file):
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="secret"):
            config_revisions.commit(
                "policy",
                {
                    "version": 1,
                    "enabled": True,
                    "default_action": "allow",
                    "rules": [],
                    "token": "should-not-land",
                },
            )
    assert "should-not-land" not in policy_file.read_text(encoding="utf-8")


def test_developer_profile_cannot_see_the_tool_and_cos_can():
    assert "config_revision_manage" in server.REGISTERED_TOOLS
    assert not profiles.tool_is_allowed_for(profiles.DEVELOPER, "config_revision_manage")
    assert not profiles.tool_is_allowed_for(profiles.STEWARD, "config_revision_manage")
    assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, "config_revision_manage")


def test_config_revisions_route_lists_and_parks_rollback(policy_file, monkeypatch):
    token = f"tok-config-{secrets.token_urlsafe(8)}"
    developer = f"tok-dev-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Laptop_Agent5:{token},Desktop_Agent4:{developer},{HUMAN_BEARER}",
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with _as("Laptop_Agent5"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
        config_revisions.commit(
            "policy",
            {
                "version": 1,
                "enabled": True,
                "default_action": "deny",
                "rules": [
                    {
                        "name": "keep-config-rollback",
                        "tool": "config_revision_manage",
                        "type": "tool",
                        "action": "allow",
                    }
                ],
            },
        )
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        developer_headers = {"Authorization": f"Bearer {developer}"}
        listed = client.get("/api/config-revisions?kind=policy", headers=headers)
        assert listed.status_code == 200
        body = listed.json()
        assert body["revisions"][0]["revision"] == 2
        assert body["count"] == 2
        parked = client.post(
            "/api/action/config-revision",
            headers=headers,
            json={"kind": "policy", "revision": 1},
        )
        assert parked.status_code == 202
        assert parked.json()["pending"] is True
        assert parked.json()["approval_id"]
        assert "default_action: deny" in policy_file.read_text(encoding="utf-8")
        assert client.get("/api/config-revisions", headers=developer_headers).status_code == 403
        assert (
            client.post(
                "/api/action/config-revision",
                headers=developer_headers,
                json={"kind": "policy", "revision": 1},
            ).status_code
            == 403
        )
        assert client.get("/api/config-revisions").status_code in {401, 403}


def test_commit_materializes_a_missing_policy_directory(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
    assert path.is_file()
    assert "deny" in path.read_text(encoding="utf-8")
    assert not path.with_name(path.name + ".revision-probe").exists()

    rejected = tmp_path / "rejected" / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(rejected))
    with _as("Desktop_Agent4"):
        with pytest.raises(Exception, match="default_action"):
            config_revisions.commit(
                "policy",
                {"version": 1, "enabled": True, "default_action": "sometimes", "rules": []},
            )
    assert not rejected.parent.exists()


def test_policy_rollback_restores_stored_file_text(policy_file):
    with _as("Desktop_Agent4"):
        first = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8") + "# out of band\n", encoding="utf-8"
    )
    with _as("Desktop_Agent4"):
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", first["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore text")
    with _as("Desktop_Agent4"):
        config_revisions.rollback("policy", first["revision"])
    assert "# out of band" not in policy_file.read_text(encoding="utf-8")


def test_failed_rollback_does_not_spend_the_pass(policy_file, monkeypatch):
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", 1)
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore allow")

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    original_apply = config_revisions._apply_planned
    monkeypatch.setattr(config_revisions, "_apply_planned", boom)
    with _as("Desktop_Agent4"):
        with pytest.raises(RuntimeError, match="disk full"):
            config_revisions.rollback("policy", 1)
    assert "deny" in policy_file.read_text(encoding="utf-8")
    monkeypatch.setattr(config_revisions, "_apply_planned", original_apply)
    with _as("Desktop_Agent4"):
        restored = config_revisions.rollback("policy", 1)
    assert restored["action"] == "rollback"
    assert "default_action: allow" in policy_file.read_text(encoding="utf-8")


def test_profile_commit_cannot_clobber_an_overlapping_policy_commit(policy_file, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = config_revisions._base_policy_mapping

    def slow_mapping():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(config_revisions, "_base_policy_mapping", slow_mapping)
    errors: list[BaseException] = []

    def profile_commit():
        try:
            with _as("Desktop_Agent4"):
                config_revisions.commit("profile", {"overlay": {"Desktop_Agent4": "research"}})
        except BaseException as exc:  # noqa: BLE001 - surface the thread failure
            errors.append(exc)

    worker = threading.Thread(target=profile_commit)
    worker.start()
    assert entered.wait(5)
    policy_done = threading.Event()

    def policy_commit():
        try:
            with _as("Desktop_Agent4"):
                config_revisions.commit(
                    "policy",
                    {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            policy_done.set()

    other = threading.Thread(target=policy_commit)
    other.start()
    assert not policy_done.wait(0.4)
    release.set()
    worker.join(5)
    other.join(5)
    assert not errors
    text = policy_file.read_text(encoding="utf-8")
    # The policy commit waited for the profile commit, then wrote its own
    # document. A stale profile mapping would have put default_action back
    # to allow after that deny write.
    assert "default_action: deny" in text
    assert "default_action: allow" not in text


def test_policy_description_may_mention_a_password(policy_file):
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {
                "version": 1,
                "enabled": True,
                "default_action": "allow",
                "rules": [
                    {
                        "name": "ask",
                        "description": "Ask for password: required by operator",
                        "tool": "*",
                        "type": "max_calls_per_episode",
                        "action": "deny",
                    }
                ],
            },
        )
    assert "password: required" in policy_file.read_text(encoding="utf-8")


def test_short_writes_are_written_in_full(policy_file, monkeypatch):
    import os

    real_write = os.write

    def short_write(fd, data):
        blob = bytes(data)
        if blob.startswith(b"version"):
            return real_write(fd, blob[:1])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
    text = policy_file.read_text(encoding="utf-8")
    assert "default_action: deny" in text
    yaml.safe_load(text)


def test_block_scalar_prose_can_be_captured(policy_file):
    policy_file.write_text(
        "version: 1\n"
        "enabled: true\n"
        "default_action: allow\n"
        "rules:\n"
        "  - name: ask\n"
        "    description: |\n"
        "      # password: required\n"
        '    tool: "*"\n'
        "    type: max_calls_per_episode\n"
        "    action: deny\n",
        encoding="utf-8",
    )
    with _as("Desktop_Agent4"):
        recorded = config_revisions.capture("policy")
    assert recorded["revision"] == 1


def test_block_scalar_prose_can_be_rolled_back(policy_file):
    policy_file.write_text(
        "version: 1\n"
        "enabled: true\n"
        "default_action: allow\n"
        "rules:\n"
        "  - name: ask\n"
        "    description: |\n"
        "      # password: required\n"
        '    tool: "*"\n'
        "    type: max_calls_per_episode\n"
        "    action: deny\n",
        encoding="utf-8",
    )
    with _as("Desktop_Agent4"):
        first = config_revisions.capture("policy")
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", first["revision"])
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore block scalar")
    with _as("Desktop_Agent4"):
        config_revisions.rollback("policy", first["revision"])
    assert "# password: required" in policy_file.read_text(encoding="utf-8")


def test_credential_assignment_in_a_description_is_refused(policy_file):
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="secret"):
            config_revisions.commit(
                "policy",
                {
                    "version": 1,
                    "enabled": True,
                    "default_action": "allow",
                    "rules": [
                        {
                            "name": "ask",
                            "description": "api_key: live-value",
                            "tool": "*",
                            "type": "max_calls_per_episode",
                            "action": "deny",
                        }
                    ],
                },
            )
    assert "live-value" not in policy_file.read_text(encoding="utf-8")


def test_capture_refuses_a_secret_hidden_in_a_comment(policy_file):
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8") + "# api_key: supersecretvalue\n",
        encoding="utf-8",
    )
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="secret"):
            config_revisions.capture("policy")
    listed = config_revisions.list_revisions("policy")
    assert listed["count"] == 0


def test_commit_refuses_a_symlinked_policy_file(tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "policy.yaml"
    link.symlink_to(target)
    monkeypatch.setenv("UA_POLICY_FILE", str(link))
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="symlink"):
            config_revisions.commit(
                "policy",
                {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
            )
    assert target.read_text(encoding="utf-8") == "original"


def test_validation_keeps_an_existing_probe_file(policy_file):
    probe = policy_file.with_name(policy_file.name + ".revision-probe")
    probe.write_text("draft: keep\n", encoding="utf-8")
    with _as("Desktop_Agent4"):
        with pytest.raises(Exception, match="default_action"):
            config_revisions.commit(
                "policy",
                {"version": 1, "enabled": True, "default_action": "sometimes", "rules": []},
            )
    assert probe.read_text(encoding="utf-8") == "draft: keep\n"
    assert not list(policy_file.parent.glob(".policy.yaml.revision-probe.*"))


def test_comment_only_edits_are_drift_until_captured(policy_file, org_file):
    with _as("Desktop_Agent4"):
        config_revisions.capture("policy")
        config_revisions.capture("org")
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8") + "# reviewed on Tuesday\n",
        encoding="utf-8",
    )
    org_file.write_text("# charter note\n" + org_file.read_text(encoding="utf-8"), encoding="utf-8")
    policy_list = config_revisions.list_revisions("policy")
    org_list = config_revisions.list_revisions("org")
    assert policy_list["drift"] is True
    assert "reviewed on Tuesday" in policy_list["latest_diff"]
    assert org_list["drift"] is True
    assert "charter note" in org_list["latest_diff"]
    with _as("Desktop_Agent4"):
        captured = config_revisions.capture("policy")
        config_revisions.capture("org")
    assert captured["revision"] == 2
    clean = config_revisions.list_revisions("policy")
    assert clean["drift"] is False
    assert clean["latest_diff"] == ""
    assert config_revisions.list_revisions("org")["drift"] is False
    assert config_revisions.list_revisions("org")["latest_diff"] == ""
    shown = config_revisions.show_revision("policy", captured["revision"])
    compared = config_revisions.diff_revisions("policy", 1, 2)
    assert "reviewed on Tuesday" in shown["diff"]
    assert "reviewed on Tuesday" in compared["diff"]


def test_rollback_pass_does_not_apply_after_the_live_file_changes(policy_file):
    original = {"version": 1, "enabled": True, "default_action": "allow", "rules": []}
    changed = {"version": 1, "enabled": True, "default_action": "deny", "rules": []}
    with _as("Desktop_Agent4"):
        config_revisions.commit("policy", original)
        config_revisions.commit("policy", changed)
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", 1)
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="restore allow")
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8") + "# edited after approval\n",
        encoding="utf-8",
    )
    with _as("Desktop_Agent4"):
        with pytest.raises(ApprovalPendingError):
            config_revisions.rollback("policy", 1)
    text = policy_file.read_text(encoding="utf-8")
    assert "default_action: deny" in text
    assert "# edited after approval" in text


def test_removing_an_approval_rule_needs_a_separate_approval(policy_file):
    policy_file.write_text(
        "version: 1\n"
        "enabled: true\n"
        "default_action: allow\n"
        "rules:\n"
        "  - name: gate-merge\n"
        "    tool: git_merge_task\n"
        "    type: tool\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    stripped = {"version": 1, "enabled": True, "default_action": "allow", "rules": []}
    with _as("Desktop_Agent4"):
        config_revisions.capture("policy")
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.commit("policy", stripped)
    assert "gate-merge" in policy_file.read_text(encoding="utf-8")
    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="drop rule")
    with _as("Desktop_Agent4"):
        config_revisions.commit("policy", stripped)
    assert "gate-merge" not in policy_file.read_text(encoding="utf-8")


def test_empty_policy_file_is_not_recorded_as_defaults(policy_file):
    policy_file.write_text("", encoding="utf-8")
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="empty"):
            config_revisions.capture("policy")


def test_matching_commit_still_refuses_an_unsafe_overlay(policy_file):
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\n"
        "profile_defaults:\n  server_agent8: developer\nrules: []\n",
        encoding="utf-8",
    )
    document = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="cannot change"):
            config_revisions.commit("policy", document)
    assert profiles.default_for_identity("server_agent8") == "public"


def test_overlay_cannot_remove_every_chief_of_staff(policy_file):
    overlay = {
        label: "research"
        for label, profile in profiles.DEFAULT_PROFILES_BY_IDENTITY.items()
        if profile == profiles.CHIEF_OF_STAFF
    }
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="chief-of-staff"):
            config_revisions.commit("profile", {"overlay": overlay})
    assert profiles.default_for_identity("Laptop_Agent5") == "chief-of-staff"


def test_downgraded_chief_of_staff_can_still_request_rollback(policy_file):
    with _as("Desktop_Agent4"):
        config_revisions.commit("profile", {"overlay": {"Laptop_Agent5": "research"}})
    assert profiles.default_for_identity("Laptop_Agent5") == "research"
    assert config_revisions.rollback_recovery_allowed("Laptop_Agent5", "rollback")
    assert not config_revisions.rollback_recovery_allowed("Laptop_Agent5", "commit")
    assert not profiles.overlay_ceiling_allows("Laptop_Agent5", "chief-of-staff")
    assert profiles.overlay_ceiling_allows("Desktop_Agent4", "chief-of-staff")


def test_unchanged_rollback_names_the_revision_that_matches_live(policy_file):
    original = {"version": 1, "enabled": True, "default_action": "allow", "rules": []}
    changed = {"version": 1, "enabled": True, "default_action": "deny", "rules": []}
    with _as("Desktop_Agent4"):
        first = config_revisions.commit("policy", original)
    restored_text = policy_file.read_text(encoding="utf-8")
    with _as("Desktop_Agent4"):
        config_revisions.commit("policy", changed)
    policy_file.write_text(restored_text, encoding="utf-8")
    with _as("Desktop_Agent4"):
        result = config_revisions.rollback("policy", first["revision"])
    assert result["unchanged"] is True
    assert result["revision"] == first["revision"]
    assert result["document_sha256"] == first["document_sha256"]
    listed = config_revisions.list_revisions("policy")
    assert listed["drift"] is True


def test_commit_file_variant_postcommit_fanout_failure_reports_committed_outcome(
    policy_file, monkeypatch
):
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("network failure during broadcast")

    monkeypatch.setattr(config_revisions, "_publish", fail_publish)

    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigFanoutError) as exc_info:
            config_revisions.commit(
                "policy",
                {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
            )

    exc = exc_info.value
    assert exc.committed is True
    assert exc.revision == 2
    assert exc.kind == "policy"
    assert exc.action == "commit"
    assert exc.notification_sent is False
    assert "network failure during broadcast" in (exc.notification_error or "")
    assert exc.result["revision"] == 2
    assert exc.result["notification_sent"] is False
    assert "network failure during broadcast" in (exc.result["notification_error"] or "")

    # File on disk changed durably
    assert "default_action: deny" in policy_file.read_text(encoding="utf-8")

    # Revision was durably recorded in SQLite
    listed = config_revisions.list_revisions("policy")
    assert listed["revisions"][0]["revision"] == 2
    shown = config_revisions.show_revision("policy", 2)
    assert shown["revision"] == 2
    assert shown["document_sha256"] == exc.document_sha256


def test_rollback_postcommit_fanout_failure_keeps_approval_spent(policy_file, monkeypatch):
    from unified_agent.tools import approvals

    with _as("Desktop_Agent4"):
        first = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
        with pytest.raises(ApprovalPendingError) as pending:
            config_revisions.rollback("policy", first["revision"])

    with _as("Desktop_Person"):
        approval_resolve(pending.value.approval_id, "approve", reason="authorize restore")

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("pubsub offline")

    monkeypatch.setattr(config_revisions, "_publish", fail_publish)

    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigFanoutError) as exc_info:
            config_revisions.rollback("policy", first["revision"])

    exc = exc_info.value
    assert exc.committed is True
    assert exc.revision == 3
    assert exc.action == "rollback"
    assert exc.notification_sent is False
    assert "pubsub offline" in (exc.notification_error or "")

    # Mutation happened on disk
    assert "default_action: allow" in policy_file.read_text(encoding="utf-8")

    # Approval was spent and stays consumed (not restored/refunded)
    record = approvals.get_approval(pending.value.approval_id)
    assert record["status"] == "consumed"


def test_capture_store_variant_postcommit_fanout_failure(policy_file, monkeypatch):
    # Create drift by editing file directly
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: deny\nrules: []\n",
        encoding="utf-8",
    )

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("event bus unreachable")

    monkeypatch.setattr(config_revisions, "_publish", fail_publish)

    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigFanoutError) as exc_info:
            config_revisions.capture("policy")

    exc = exc_info.value
    assert exc.committed is True
    assert exc.revision == 1
    assert exc.action == "capture"
    assert exc.notification_sent is False
    assert "event bus unreachable" in (exc.notification_error or "")

    # SQLite recorded the capture revision
    shown = config_revisions.show_revision("policy", 1)
    assert shown["action"] == "capture"


def test_precommit_failure_preserves_file_and_raises_standard_error(policy_file):
    with _as("Desktop_Agent4"):
        config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )

    # Attempt to commit invalid text (contains forbidden secret assignment)
    with _as("Desktop_Agent4"):
        with pytest.raises(config_revisions.ConfigRevisionError, match="secret"):
            config_revisions.commit(
                "policy",
                {
                    "version": 1,
                    "enabled": True,
                    "default_action": "deny",
                    "rules": [],
                    "api_key": "supersecret123",
                },
            )

    # File on disk preserved
    assert "default_action: allow" in policy_file.read_text(encoding="utf-8")
    assert "supersecret123" not in policy_file.read_text(encoding="utf-8")

    # No second revision
    listed = config_revisions.list_revisions("policy")
    assert listed["count"] == 1


def test_config_revision_manage_mcp_adapter_returns_committed_outcome_on_fanout_error(
    policy_file, monkeypatch
):
    from unified_agent.tools.config_revisions import config_revision_manage

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("mcp fanout broke")

    monkeypatch.setattr(config_revisions, "_publish", fail_publish)

    with _as("Laptop_Agent5"):
        result = config_revision_manage(
            action="commit",
            kind="policy",
            document={"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )

    assert result["revision"] == 1
    assert result["action"] == "commit"
    assert result["notification_sent"] is False
    assert "mcp fanout broke" in (result.get("notification_error") or "")
    assert "default_action: deny" in policy_file.read_text(encoding="utf-8")


def test_webui_action_route_returns_200_committed_outcome_on_fanout_error(policy_file, monkeypatch):
    token = f"tok-config-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Laptop_Agent5:{token},{HUMAN_BEARER}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")

    with _as("Laptop_Agent5"):
        first = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "allow", "rules": []},
        )
        config_revisions.commit(
            "policy",
            {
                "version": 1,
                "enabled": True,
                "default_action": "deny",
                "rules": [
                    {
                        "name": "keep-config-rollback",
                        "tool": "config_revision_manage",
                        "type": "tool",
                        "action": "allow",
                    }
                ],
            },
        )

    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        parked = client.post(
            "/api/action/config-revision",
            headers=headers,
            json={"kind": "policy", "revision": first["revision"]},
        )
        assert parked.status_code == 202
        approval_id = parked.json()["approval_id"]

        with _as("Desktop_Person"):
            approval_resolve(approval_id, "approve", reason="authorize restore")

        def fail_publish(*_args, **_kwargs):
            raise RuntimeError("fanout socket closed")

        monkeypatch.setattr(config_revisions, "_publish", fail_publish)

        # Retry rollback post-approval
        res = client.post(
            "/api/action/config-revision",
            headers=headers,
            json={"kind": "policy", "revision": first["revision"]},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["revision"] == 3
        assert body["action"] == "rollback"
        assert body["notification_sent"] is False
        assert "fanout socket closed" in (body.get("notification_error") or "")
        assert "default_action: allow" in policy_file.read_text(encoding="utf-8")


def test_publish_aggregates_multiple_errors(monkeypatch):
    def buggy_publish(event):
        raise RuntimeError(f"error on {event['ulid']}")

    monkeypatch.setattr(events, "publish", buggy_publish)
    events_to_send = [{"ulid": "01A"}, {"ulid": "02B"}]
    sent, err = config_revisions._publish(events_to_send)
    assert sent is False
    assert err is not None
    assert "error on 01A" in err
    assert "error on 02B" in err


def test_commit_with_event_bus_disabled_reports_explicit_unsent_outcome(policy_file, monkeypatch):
    monkeypatch.setenv("UA_EVENT_BUS", "0")
    with _as("Desktop_Agent4"):
        result = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
    assert result["revision"] == 1
    assert result["notification_sent"] is False
    assert result["notification_error"] is None


def test_commit_with_event_bus_enabled_no_listener_reports_notification_failure(
    policy_file, monkeypatch
):
    monkeypatch.setenv("UA_EVENT_BUS", "1")
    with _as("Desktop_Agent4"):
        result = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
    assert result["revision"] == 1
    assert result["notification_sent"] is False
    assert "event bus delivery failed" in (result["notification_error"] or "")


def test_commit_with_event_bus_successful_delivery_reports_notification_success(
    policy_file, monkeypatch
):
    monkeypatch.setenv("UA_EVENT_BUS", "1")
    monkeypatch.setattr(events, "publish", lambda event: True)
    with _as("Desktop_Agent4"):
        result = config_revisions.commit(
            "policy",
            {"version": 1, "enabled": True, "default_action": "deny", "rules": []},
        )
    assert result["revision"] == 1
    assert result["notification_sent"] is True
    assert result["notification_error"] is None
