import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.indexer import fs_index_symbols, fs_list_symbols, fs_search_symbols


@pytest.fixture
def mock_workspace_files(tmp_path, monkeypatch):
    """Set up a mock workspace with Python files containing classes and functions."""
    workspace_dir = tmp_path / "mock_workspace"
    workspace_dir.mkdir()

    # 1. Create a Python file with rich AST definitions
    code = '''"""Module level docstring."""

def top_level_func(x, y, *args, **kwargs):
    """Calculate something."""
    return x + y

class Calculator:
    """A simple calculator class."""
    
    def add(self, a, b):
        """Add two numbers."""
        return a + b
        
    async def multiply(self, x, y=10):
        return x * y
'''
    app_file = workspace_dir / "app.py"
    app_file.write_text(code)

    # 2. Mock resolve_in_workspace in indexer.py to point to workspace_dir
    import unified_agent.tools.indexer as indexer_tool

    monkeypatch.setattr(
        indexer_tool,
        "resolve_in_workspace",
        lambda path: workspace_dir if path in (".", "") else workspace_dir / path,
    )

    return workspace_dir


def test_indexer_flow(mock_workspace_files):
    # 1. Index the workspace
    res_index = fs_index_symbols(".")
    assert res_index["files_indexed"] == 1
    assert res_index["symbols_indexed"] == 4
    assert res_index["classes"] == 1
    assert res_index["functions"] == 1
    assert res_index["methods"] == 2

    # 2. Search for the class
    res_search = fs_search_symbols("Calculator")
    assert len(res_search["results"]) == 1
    calculator = res_search["results"][0]
    assert calculator["name"] == "Calculator"
    assert calculator["type"] == "class"
    assert calculator["file_path"] == "app.py"
    assert "calculator class" in calculator["docstring"].lower()

    # 3. List the symbols of the file
    res_list = fs_list_symbols("app.py")
    symbols = res_list["symbols"]
    assert len(symbols) == 4

    # Verify top level function
    func = next(s for s in symbols if s["name"] == "top_level_func")
    assert func["type"] == "function"
    assert func["signature"] == "(x, y, *args, **kwargs)"
    assert func["docstring"] == "Calculate something."

    # Verify class method
    method = next(s for s in symbols if s["name"] == "add")
    assert method["type"] == "method"
    assert method["parent"] == "Calculator"
    assert method["signature"] == "(self, a, b)"
    assert method["docstring"] == "Add two numbers."


def test_semantic_symbol_search_falls_back_without_compatible_vectors(
    mock_workspace_files, monkeypatch
):
    """A model change that matches no stored vector must not return an empty
    semantic result and skip the name search."""
    import unified_agent.embeddings as embeddings
    import unified_agent.storage as storage
    from unified_agent.tools.indexer import join_symbol_backfill_threads

    monkeypatch.setattr(embeddings, "generate_embedding", lambda text: [0.1, 0.0, 0.0, 0.0])
    fs_index_symbols(".")
    join_symbol_backfill_threads()

    monkeypatch.setenv("UA_EMBED_MODEL", "other/model")
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM workspace_symbol_embeddings")
        symbol = conn.execute(
            "SELECT id FROM workspace_symbols WHERE symbol_name = ?",
            ("add",),
        ).fetchone()
        assert symbol is not None
        conn.execute(
            "INSERT INTO workspace_symbol_embeddings (symbol_id, embedding, model) VALUES (?, ?, NULL)",
            (symbol["id"], embeddings.embedding_to_bytes([1.0, 0.0, 0.0, 0.0])),
        )
        conn.commit()
    finally:
        conn.close()

    res = fs_search_symbols("add", semantic=True)
    names = [row["name"] for row in res["results"]]
    assert "add" in names
    matched = next(row for row in res["results"] if row["name"] == "add")
    assert "similarity" not in matched


def test_indexer_nonexistent_file():
    with pytest.raises(ToolError, match="does not exist"):
        fs_list_symbols("nonexistent.py")


@pytest.mark.embeddings
def test_semantic_symbol_search(mock_workspace_files):
    # 1. Index the workspace
    fs_index_symbols(".")

    # 2. Run the backfill synchronously so we can immediately query the database
    from unified_agent.tools.indexer import _backfill_symbol_embeddings

    _backfill_symbol_embeddings()

    # 3. Test semantic search with natural language intent
    # The docstring of 'add' is "Add two numbers."
    res = fs_search_symbols("math addition", semantic=True)
    assert len(res["results"]) > 0
    best_match = res["results"][0]
    assert best_match["name"] == "add"
    assert "similarity" in best_match
    assert best_match["similarity"] is not None
    assert best_match["similarity"] > 0.0
