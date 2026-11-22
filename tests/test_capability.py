"""Tests for the capability-matching check (task #116).

Ported from the held feat/task-62-mesh-coordination branch. Hermes' allowlist
is read from UA_HERMES_CONFIG (a fake path here) rather than the real
~/.hermes/config.yaml, so the suite doesn't depend on this machine's live
Hermes install.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.capability import (
    STATIC_HERMES_TOOLS,
    _extract_required_tools,
    check_agent_capability,
    get_agent_allowed_tools,
)
from unified_agent.tools.tasks import task_add


def test_extract_required_tools():
    assert _extract_required_tools("some notes [requires: fs_read, fs_write]") == {
        "fs_read",
        "fs_write",
    }
    assert _extract_required_tools("some notes [requires-tools: delegate]") == {"delegate"}
    assert _extract_required_tools("no annotations here") == set()
    assert _extract_required_tools("[Requires: fs_read]\n[Requires-Tools: plan_create]") == {
        "fs_read",
        "plan_create",
    }
    assert _extract_required_tools(None) == set()


class TestGetAgentAllowedTools:
    def test_unrestricted_agent_returns_none(self):
        assert get_agent_allowed_tools("Desktop_Agent1") is None

    def test_hermes_falls_back_to_static_list_when_config_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "does-not-exist.yaml"))
        assert get_agent_allowed_tools("Desktop_Agent5") == STATIC_HERMES_TOOLS

    def test_hermes_reads_live_config_when_present(self, monkeypatch, tmp_path):
        cfg = tmp_path / "hermes-config.yaml"
        cfg.write_text(
            "mcp_servers:\n"
            "  unified-agent:\n"
            "    tools:\n"
            "      include:\n"
            "        - memory_search\n"
            "        - vault_read\n"
        )
        monkeypatch.setenv("UA_HERMES_CONFIG", str(cfg))
        assert get_agent_allowed_tools("Desktop_Agent5") == {"memory_search", "vault_read"}

    def test_hermes_falls_back_on_unreadable_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "hermes-config.yaml"
        cfg.write_text(
            "not: [valid, yaml",
        )
        monkeypatch.setenv("UA_HERMES_CONFIG", str(cfg))
        assert get_agent_allowed_tools("Desktop_Agent5") == STATIC_HERMES_TOOLS


class TestCheckAgentCapability:
    def test_capable_when_required_tools_are_allowed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        tid = task_add(title="Hermes safe task", notes="[requires: fs_read, plan_get]")["id"]
        res = check_agent_capability(tid, "Desktop_Agent5")
        assert res["capable"] is True
        assert res["missing_tools"] == []
        assert res["required_tools"] == ["fs_read", "plan_get"]

    def test_incapable_when_required_tools_are_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        tid = task_add(title="Hermes unsafe task", notes="[requires: fs_write, git_merge_task]")[
            "id"
        ]
        res = check_agent_capability(tid, "Desktop_Agent5")
        assert res["capable"] is False
        assert res["missing_tools"] == ["fs_write", "git_merge_task"]

    def test_unrestricted_agent_always_capable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        tid = task_add(title="anything", notes="[requires: fs_write, run_cmd]")["id"]
        res = check_agent_capability(tid, "Desktop_Agent1")
        assert res["capable"] is True
        assert res["allowed_tools"] is None
        assert res["missing_tools"] == []

    def test_task_with_no_requirements_is_trivially_capable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        tid = task_add(title="no annotation", notes="just some notes")["id"]
        res = check_agent_capability(tid, "Desktop_Agent5")
        assert res["capable"] is True
        assert res["required_tools"] == []

    def test_unknown_agent_raises(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        tid = task_add(title="x")["id"]
        with pytest.raises(ToolError, match="not a known agent label"):
            check_agent_capability(tid, "UnknownAgent")

    def test_unknown_task_raises(self):
        with pytest.raises(ToolError, match="no task with id"):
            check_agent_capability(999_999, "Desktop_Agent1")


class TestCapabilityCatalog:
    def test_inventory_is_read_only_and_complete(self, isolated_env):
        from unified_agent.tools.capability import capability_catalog

        before = list(isolated_env.iterdir())
        catalog = capability_catalog()
        assert catalog["read_only"] is True
        assert catalog["install_is_explicit"] is True
        assert catalog["approval_gated"] is True
        assert catalog["prerequisites"] is None
        assert "python" in catalog["versions"]
        assert "ls" in catalog["clis"]
        assert "check_agent_capability" in catalog["mcp_tools"]
        assert "capability_catalog" in catalog["mcp_tools"]
        assert catalog["health"]["approval_queue"]["status"] == "DORMANT"
        assert catalog["boundaries"]["vault_write_dirs"]
        # No bundled product skills ship in this checkout since the
        # code-review-graph retirement (commit 3342556); the key is still
        # present and list-shaped, just empty.
        assert isinstance(catalog["skills"], list)
        assert any(w["name"] == "smoke-echo" for w in catalog["workflows"])
        assert list(isolated_env.iterdir()) == before

    def test_dispatch_prerequisites_report_missing_tools_and_config(self, monkeypatch, tmp_path):
        from unified_agent.tools.capability import capability_catalog

        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        tid = task_add(
            title="needs gemini and write",
            notes="[requires: gemini_ask, fs_write]",
        )["id"]
        catalog = capability_catalog(task_id=tid, agent_label="Desktop_Agent5")
        prereq = catalog["prerequisites"]
        assert prereq["capable"] is False
        assert "fs_write" in prereq["missing_tools"]
        assert prereq["missing_config"][0]["tool"] == "gemini_ask"
        assert prereq["missing_config"][0]["action"] == "configure"
        assert prereq["ready"] is False

    def test_requires_both_task_and_agent(self):
        from unified_agent.tools.capability import capability_catalog

        with pytest.raises(ToolError, match="both task_id and agent_label"):
            capability_catalog(task_id=1)

    def test_custom_and_exported_hermes_labels_use_hermes_tool_allowlist(
        self, monkeypatch, tmp_path
    ):
        from unified_agent import config
        from unified_agent.tools.capability import get_agent_allowed_tools

        monkeypatch.setenv("UA_HERMES_CONFIG", str(tmp_path / "missing.yaml"))
        monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "Agent5", "Hermes")
        custom_tools = get_agent_allowed_tools("Local-Hermes")
        exported_tools = get_agent_allowed_tools("Desktop_Agent5")
        assert custom_tools is not None and "run_cmd" not in custom_tools
        assert exported_tools is not None and "run_cmd" not in exported_tools
