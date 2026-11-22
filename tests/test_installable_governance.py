from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]


def test_hub_unit_uses_installable_paths():
    unit = (REPO_ROOT / "deploy" / "unified-agent.service").read_text()

    assert "ExecStart=%h/.local/bin/unified-agent" in unit
    assert "Environment=UA_WORKSPACE=%h/.unified_agent/workspace" in unit
    assert "%h/dev/unified_agent" not in unit
    assert "%h/dev/agent" not in unit
