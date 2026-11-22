import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.lint import python_lint_and_format


@pytest.fixture
def mock_python_file(tmp_path, monkeypatch):
    """Create a temporary Python file with lint/formatting errors for testing."""
    file_dir = tmp_path / "test_lint_dir"
    file_dir.mkdir()

    # Python file with:
    # 1. Unused import (should be auto-fixed/removed by ruff check --fix)
    # 2. Bad formatting (should be auto-formatted by ruff format)
    py_content = """import os
import sys

def my_func(a,b):
  return a+b

"""
    py_file = file_dir / "target.py"
    py_file.write_text(py_content)

    # Mock resolve_in_workspace in lint.py
    import unified_agent.tools.lint as lint_tool

    monkeypatch.setattr(
        lint_tool, "resolve_in_workspace", lambda path: file_dir if path == "." else file_dir / path
    )

    return py_file


def test_python_lint_and_format_flow(mock_python_file):
    # Run lint and format on the target file
    res = python_lint_and_format("target.py")
    print("\nRES:", res)

    # Verify success and output details
    assert res["success"] is True

    # Read the file content after run
    formatted_content = mock_python_file.read_text()

    # 1. sys and os imports should be removed since they were unused
    assert "import sys" not in formatted_content
    assert "import os" not in formatted_content

    # 2. Formatting should clean up arguments spacing and indentation
    assert "def my_func(a, b):" in formatted_content
    assert "    return a + b" in formatted_content


def test_python_lint_nonexistent():
    with pytest.raises(ToolError, match="does not exist"):
        python_lint_and_format("nonexistent.py")
