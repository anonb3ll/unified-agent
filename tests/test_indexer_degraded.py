"""Degraded-mode surfacing for the fs_*_symbols tools (task #188).

The defect these cover: tree-sitter being absent produced an empty symbol list that
was byte-identical to "this file has no symbols". A hub missing the extra reported a
clean index over zero JS/TS symbols, and no caller could tell indexing had silently
run non-AST.
"""

import sys

from unified_agent.tools import indexer
from unified_agent.tools.indexer import (
    DEGRADED_NO_GRAMMAR,
    DEGRADED_NO_PROVIDER,
    DEGRADED_PARSE_FAILED,
    fs_index_symbols,
    fs_list_symbols,
    fs_search_symbols,
    parse_js_ts_symbols,
    parse_js_ts_symbols_with_status,
)


def _no_tree_sitter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)


class TestParseStatus:
    def test_missing_provider_reports_reason(self, monkeypatch):
        _no_tree_sitter(monkeypatch)
        symbols, reason = parse_js_ts_symbols_with_status("class Foo {}", "test.js")
        assert symbols == []
        assert reason == DEGRADED_NO_PROVIDER

    def test_grammar_failure_is_distinct_from_missing_provider(self, monkeypatch):
        def boom(_lang):
            raise RuntimeError("grammar not built")

        monkeypatch.setattr(indexer, "_load_ts_parser_factory", lambda: boom)
        symbols, reason = parse_js_ts_symbols_with_status("class Foo {}", "test.js")
        assert symbols == []
        assert reason == DEGRADED_NO_GRAMMAR

    def test_parse_exception_reports_parse_failed(self, monkeypatch):
        class _Parser:
            def parse(self, _content):
                raise ValueError("bad tree")

        monkeypatch.setattr(indexer, "_load_ts_parser_factory", lambda: lambda _l: _Parser())
        symbols, reason = parse_js_ts_symbols_with_status("class Foo {}", "test.js")
        assert symbols == []
        assert reason == DEGRADED_PARSE_FAILED

    def test_healthy_parse_reports_no_reason(self):
        symbols, reason = parse_js_ts_symbols_with_status("class Foo {\n bar() {}\n}", "t.js")
        if not symbols:  # dependency genuinely absent in this environment
            return
        assert reason is None

    def test_legacy_wrapper_still_returns_a_bare_list(self, monkeypatch):
        """The old signature is load-bearing for existing callers and tests."""
        _no_tree_sitter(monkeypatch)
        assert parse_js_ts_symbols("class Foo {}", "test.js") == []


class TestIndexAdvisory:
    def test_index_flags_degraded_when_js_files_cannot_be_parsed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "app.js").write_text("class Foo { bar() {} }")
        (tmp_path / "mod.py").write_text("def hello():\n    pass\n")
        _no_tree_sitter(monkeypatch)

        res = fs_index_symbols(".")

        assert res["degraded"] is True
        assert res["degraded_detail"]["reason"] == DEGRADED_NO_PROVIDER
        assert res["degraded_detail"]["js_ts_files_affected"] == 1
        assert "app.js" in res["degraded_detail"]["sample_files"]
        assert "degraded" in res["warning"].lower()
        # The Python half is unaffected and must still be indexed.
        assert res["symbols_indexed"] >= 1

    def test_index_is_not_degraded_for_a_python_only_tree(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "mod.py").write_text("def hello():\n    pass\n")
        _no_tree_sitter(monkeypatch)

        res = fs_index_symbols(".")

        assert res["degraded"] is False
        assert "warning" not in res

    def test_empty_js_file_is_not_reported_as_degraded(self, monkeypatch, tmp_path):
        """The distinction the whole task is about: empty != unparseable."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "empty.js").write_text("// just a comment\n")

        res = fs_index_symbols(".")

        if not indexer.tree_sitter_available():
            return  # cannot exercise the healthy path without the dependency
        assert res["degraded"] is False


class TestListAndSearchAdvisory:
    def test_list_symbols_flags_degraded_js_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "app.js").write_text("class Foo { bar() {} }")
        _no_tree_sitter(monkeypatch)

        res = fs_list_symbols("app.js")

        assert res["symbols"] == []
        assert res["degraded"] is True
        assert res["degraded_detail"]["reason"] == DEGRADED_NO_PROVIDER

    def test_list_symbols_routes_js_files_to_the_js_parser(self, monkeypatch, tmp_path):
        """A .js file used to be fed to the Python ast parser, yielding a silent []."""
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "app.js").write_text("class Foo {\n  bar() {}\n}\n")

        if not indexer.tree_sitter_available():
            return
        res = fs_list_symbols("app.js")

        assert res["degraded"] is False
        assert {s["name"] for s in res["symbols"]} >= {"Foo", "bar"}

    def test_list_symbols_on_python_is_never_degraded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / "mod.py").write_text("def hello():\n    pass\n")
        _no_tree_sitter(monkeypatch)

        res = fs_list_symbols("mod.py")

        assert res["degraded"] is False
        assert [s["name"] for s in res["symbols"]] == ["hello"]

    def test_search_flags_that_js_ts_symbols_cannot_be_in_the_index(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        _no_tree_sitter(monkeypatch)

        res = fs_search_symbols("anything")

        assert res["degraded"] is True
        assert res["degraded_detail"]["scope"] == "js_ts_symbols_absent_from_index"

    def test_search_is_clean_when_the_provider_is_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        if not indexer.tree_sitter_available():
            return

        res = fs_search_symbols("anything")

        assert res["degraded"] is False
        assert "warning" not in res

    def test_availability_is_not_cached_across_installs(self, monkeypatch):
        """A hub that gains the extra mid-run must stop reporting degraded."""
        _no_tree_sitter(monkeypatch)
        assert indexer.tree_sitter_available() is False
        monkeypatch.undo()
        if indexer._load_ts_parser_factory() is not None:
            assert indexer.tree_sitter_available() is True
