"""Tests for fs_repo_map PageRank token-budgeted codebase mapping tool (Task #125 / Phase 2.1)."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.indexer import fs_index_symbols
from unified_agent.tools.repo_map import fs_repo_map


def test_fs_repo_map_basic(monkeypatch, tmp_path):
    """Test generating a repo map for indexed python code."""
    (tmp_path / "main.py").write_text(
        "class Helper:\n"
        "    '''Helper class docstring.'''\n"
        "    def run(self, val):\n"
        "        pass\n\n"
        "def main_func():\n"
        "    '''Main entry point.'''\n"
        "    h = Helper()\n"
        "    h.run(42)\n"
    )

    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    fs_index_symbols(".")

    result = fs_repo_map(path=".", token_budget=1000)
    assert result["path"] == "."
    assert result["token_budget"] == 1000
    assert "repo_map" in result
    map_text = result["repo_map"]

    assert "# Codebase Map: ." in map_text
    assert "main.py" in map_text
    assert "class Helper" in map_text
    assert "def run" in map_text
    assert "def main_func" in map_text


def test_fs_repo_map_token_budget_truncation(monkeypatch, tmp_path):
    """Test that repo map respects small token budgets."""
    for i in range(10):
        (tmp_path / f"module_{i}.py").write_text(
            f"class Service{i}:\n"
            f"    '''Service {i} docstring.'''\n"
            f"    def action_{i}(self):\n"
            f"        pass\n"
        )

    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    fs_index_symbols(".")

    result_small = fs_repo_map(path=".", token_budget=150)
    assert result_small["estimated_tokens"] <= 250
    assert "... [truncated to fit token budget]" in result_small["repo_map"]


def test_fs_repo_map_invalid_path(monkeypatch, tmp_path):
    """Test error handling for non-existent path."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    with pytest.raises(ToolError, match="does not exist"):
        fs_repo_map("non_existent_dir")
