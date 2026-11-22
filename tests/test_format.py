"""Tests for chat-formatting primitives (task 63 C).

Every renderer returns {markdown, text}; the text form must stand alone for the
markup-stripping iMessage path.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.format import (
    format_chips,
    format_code,
    format_render,
    format_section,
    format_table,
)


class TestTable:
    def test_markdown_and_text_both_present(self):
        out = format_table(["a", "b"], [[1, 2], [3, 4]])
        assert out["markdown"].startswith("| a | b |")
        assert "---" in out["markdown"]
        # text form aligns without pipes
        assert "a" in out["text"] and "|" not in out["text"]

    def test_ragged_rows_padded(self):
        out = format_table(["a", "b", "c"], [[1], [2, 3, 4, 5]])
        # every markdown data row has exactly 3 separators-worth of cells
        for line in out["markdown"].splitlines()[2:]:
            assert line.count("|") == 4  # 3 cells → 4 bars

    def test_pipe_escaped(self):
        out = format_table(["x"], [["a|b"]])
        assert r"a\|b" in out["markdown"]

    def test_no_columns_raises(self):
        with pytest.raises(ToolError, match="at least one column"):
            format_table([], [])


class TestSectionCodeChips:
    def test_section_collapsible_and_labeled(self):
        out = format_section("Logs", "line1\nline2")
        assert "<details>" in out["markdown"] and "<summary>Logs</summary>" in out["markdown"]
        assert out["text"].startswith("Logs\n----")

    def test_section_empty_summary_raises(self):
        with pytest.raises(ToolError, match="non-empty summary"):
            format_section("  ", "body")

    def test_code_with_filename_infers_lang(self):
        out = format_code("print(1)", filename="a.py")
        assert "```python" in out["markdown"]
        assert "`a.py`" in out["markdown"]
        assert out["text"].startswith("a.py:")

    def test_code_without_filename(self):
        out = format_code("plain")
        assert "```\nplain\n```" in out["markdown"]
        assert out["text"] == "plain"

    def test_chips(self):
        out = format_chips({"priority": "high", "claimed-by": "Desktop_Agent1"})
        assert "`priority: high`" in out["markdown"]
        assert "priority=high" in out["text"]
        assert " · " in out["text"]

    def test_chips_empty_raises(self):
        with pytest.raises(ToolError, match="at least one"):
            format_chips({})


class TestFormatRender:
    def test_dispatch_all_kinds(self):
        assert "markdown" in format_render("table", {"columns": ["a"], "rows": [[1]]})
        assert "markdown" in format_render("section", {"summary": "s", "body": "b"})
        assert "markdown" in format_render("code", {"content": "x", "filename": "a.py"})
        assert "markdown" in format_render("chips", {"mapping": {"k": "v"}})

    def test_unknown_kind(self):
        with pytest.raises(ToolError, match="kind must be one of"):
            format_render("pie", {})

    def test_missing_required_key(self):
        with pytest.raises(ToolError, match="missing required key"):
            format_render("table", {"rows": [[1]]})  # no columns

    def test_payload_not_dict(self):
        with pytest.raises(ToolError, match="payload must be an object"):
            format_render("chips", ["not", "a", "dict"])
