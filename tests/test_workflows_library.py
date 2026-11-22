import pytest
from mcp.server.mcpserver.exceptions import ToolError

import unified_agent.tools.capability as capability
from unified_agent.config import agent_identity_var
from unified_agent.tools import doctor, workflow

pytestmark = pytest.mark.smoke


def test_workflow_definitions_valid():
    check = doctor._workflow_definitions_check()
    assert check["status"] == "LIVE"


def test_post_merge_verify_workflow_valid():
    """Verify post-merge-verify definition loads and validates with system_doctor in tool registry."""
    spec = workflow._load_definition("post-merge-verify")
    assert spec["name"] == "post-merge-verify"
    tool_names = [step["tool"] for step in spec["steps"]]
    assert "system_doctor" in tool_names
    assert "run_cmd" in tool_names
    assert "security_audit" in tool_names

    run_tests = next(s for s in spec["steps"] if s["name"] == "run_tests")
    assert "-m smoke" in run_tests["args"]["command"]
    # The shared doctor report may be minutes old; after a merge the step must
    # read the system as it is now (task #1897).
    doctor_step = next(s for s in spec["steps"] if s["tool"] == "system_doctor")
    assert doctor_step["args"] == {"refresh": True}

    reg = workflow._tool_registry()
    assert "system_doctor" in reg
    assert callable(reg["system_doctor"])


def test_workflow_run_capability_checking(monkeypatch):
    monkeypatch.setattr(
        workflow, "_load_definition", lambda name: {"inputs": ["task_id"], "steps": []}
    )

    def mock_check_capability(task_id, author):
        return {"capable": False, "missing_tools": ["test_tool"]}

    monkeypatch.setattr(capability, "check_agent_capability", mock_check_capability)

    with pytest.raises(
        ToolError, match="Missing tools to execute workflow for task 1: \\['test_tool'\\]"
    ):
        workflow.workflow_run("dummy", {"task_id": 1})


def test_workflow_run_worktree_confinement_and_journaling(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_load_definition",
        lambda name: {
            "inputs": [],
            "steps": [{"name": "test_step", "tool": "test_tool", "args": {}}],
        },
    )

    journal_calls = []

    def mock_vault_journal(entry, agent, tags):
        journal_calls.append((entry, agent, tags))

    identity_during_call = []

    def mock_test_tool():
        identity_during_call.append(agent_identity_var.get())
        return "success"

    def mock_registry():
        return {
            "test_tool": mock_test_tool,
            "vault_journal": mock_vault_journal,
        }

    monkeypatch.setattr(workflow, "_tool_registry", mock_registry)
    monkeypatch.setattr(workflow, "_SELF_GATED", {"test_tool"})  # bypass approval gate check
    # Task #1218: actual step invocation goes through server.REGISTERED_TOOLS
    # (the governed wrapper), not the raw _tool_registry() callable.
    from unified_agent import server

    monkeypatch.setitem(server.REGISTERED_TOOLS, "test_tool", mock_test_tool)

    res = workflow.workflow_run("dummy", {}, author="test_agent")

    assert res["status"] == "done"
    assert identity_during_call == ["test_agent"]
    assert agent_identity_var.get() is None

    assert len(journal_calls) == 2
    assert "starting" in journal_calls[0][0]
    assert journal_calls[0][1:] == ("test_agent", ["workflow", "step"])
    assert "completed" in journal_calls[1][0]


def test_weekly_export_workflow_valid():
    """Verify weekly-export definition loads and validates with export tools in registry."""
    spec = workflow._load_definition("weekly-export")
    assert spec["name"] == "weekly-export"
    tool_names = [step["tool"] for step in spec["steps"]]
    assert "export_tasks" in tool_names
    assert "export_memory" in tool_names

    reg = workflow._tool_registry()
    assert "export_tasks" in reg
    assert callable(reg["export_tasks"])
    assert "export_memory" in reg
    assert callable(reg["export_memory"])
