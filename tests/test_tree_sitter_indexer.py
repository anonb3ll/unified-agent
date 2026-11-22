import sys
from unittest.mock import MagicMock

from unified_agent.tools.indexer import parse_js_ts_symbols


class TestTreeSitterIndexer:
    def test_parse_js_ts_symbols_graceful_degradation_on_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)

        symbols = parse_js_ts_symbols("class Foo {}", "test.js")
        assert symbols == []

    def test_parse_js_ts_symbols_mocked(self, monkeypatch):
        # Mock the preferred provider, tree_sitter_language_pack
        mock_languages = MagicMock()
        mock_parser = MagicMock()

        # Mock parser.parse to return a tree of mock nodes
        mock_tree = MagicMock()

        # Set up node hierarchy to test walk_ts_nodes
        # Node 1: class_declaration
        class_node = MagicMock()
        class_node.type = "class_declaration"
        class_node.start_point = (0, 0)
        class_node.start_byte = 0
        class_node.end_byte = 25

        name_node = MagicMock()
        name_node.type = "identifier"
        name_node.start_byte = 6
        name_node.end_byte = 9
        class_node.child_by_field_name.return_value = name_node

        # Node 2: method_definition inside class
        method_node = MagicMock()
        method_node.type = "method_definition"
        method_node.start_point = (1, 2)
        method_node.start_byte = 14
        method_node.end_byte = 22

        method_name = MagicMock()
        method_name.type = "property_identifier"
        method_name.start_byte = 14
        method_name.end_byte = 17

        method_params = MagicMock()
        method_params.type = "formal_parameters"
        method_params.start_byte = 17
        method_params.end_byte = 19

        # Setup child_by_field_name behavior for method
        def child_by_field(field_name):
            if field_name == "name":
                return method_name
            return None

        method_node.child_by_field_name.side_effect = child_by_field
        method_node.children = [method_params]

        class_node.children = [method_node]
        mock_tree.root_node.type = "program"
        mock_tree.root_node.children = [class_node]

        mock_parser.parse.return_value = mock_tree
        mock_languages.get_parser.return_value = mock_parser

        monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", mock_languages)

        content = "class Bar {\n  baz() {}\n}"
        symbols = parse_js_ts_symbols(content, "test.js")

        # Should find class 'Bar' and method 'baz'
        assert len(symbols) == 2

        assert symbols[0]["name"] == "Bar"
        assert symbols[0]["type"] == "class"

        assert symbols[1]["name"] == "baz"
        assert symbols[1]["type"] == "method"
        assert symbols[1]["parent"] == "Bar"
        assert symbols[1]["signature"] == "()"

    def test_parse_js_ts_symbols_real_non_mocked(self):
        # This test runs the real parser using tree_sitter_language_pack
        # It verifies arrow functions, classes, methods, and functions.
        content = """
        const baz = (a, b) => a + b;
        class Foo {
            bar(x) {}
        }
        function testFunc() {}
        """
        symbols = parse_js_ts_symbols(content, "test.ts")

        # If the environment lacks tree-sitter or falls back to empty list, skip assertions.
        # But we expect it to be present in this repo dev environment.
        if not symbols:
            # Skip only if the dependency truly isn't installed; if it IS installed and
            # symbols still came back empty, that's a real bug — let the assertions below
            # fail instead of silently skipping.
            import importlib.util

            if importlib.util.find_spec("tree_sitter_language_pack") is None:
                return  # skip: dependency not installed in this environment

        names = {sym["name"]: sym for sym in symbols}

        # Verify arrow function 'baz'
        assert "baz" in names, f"baz not found in symbols: {symbols}"
        assert names["baz"]["type"] == "function"
        assert names["baz"]["signature"] == "(a, b)"

        # Verify class 'Foo' and method 'bar'
        assert "Foo" in names
        assert names["Foo"]["type"] == "class"

        assert "bar" in names
        assert names["bar"]["type"] == "method"
        assert names["bar"]["parent"] == "Foo"
        assert names["bar"]["signature"] == "(x)"

        # Verify function 'testFunc'
        assert "testFunc" in names
        assert names["testFunc"]["type"] == "function"
