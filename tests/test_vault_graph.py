import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.vault_graph import vault_query


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A small vault with a link "hub" (4 backlinks) and an unlinked "leaf".

    Both hub.md and leaf.md mention "orchestration" exactly once, so a query
    for that term seeds them equally on lexical score alone — any ranking
    difference between them is attributable to the PageRank walk. A handful
    of extra "widget" notes (sharing a distinct keyword) exercise the
    max_results cap, and "solo" exercises a pure-lexical match with zero
    graph presence.
    """
    root = tmp_path / "vault"
    root.mkdir()
    (root / "hub.md").write_text("# Hub\n\nThe orchestration layer ties agents together.\n")
    (root / "leaf.md").write_text("# Leaf\n\nA standalone note about orchestration design.\n")
    for name in ("linkerA", "linkerB", "linkerC", "linkerD"):
        (root / f"{name}.md").write_text(f"# {name}\n\nSee [[hub]] for context.\n")
    (root / "solo.md").write_text("# Solo\n\nMentions zzzunique once here.\n")
    for i in range(1, 7):
        (root / f"widget{i}.md").write_text(f"# Widget {i}\n\nContains keyword123 in body.\n")
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    return root


class TestVaultQueryPageRank:
    def test_hub_outranks_leaf_via_pagerank(self, vault):
        result = vault_query("orchestration")
        paths = [r["path"] for r in result["results"]]
        assert "hub.md" in paths
        assert "leaf.md" in paths
        hub_score = next(r["score"] for r in result["results"] if r["path"] == "hub.md")
        leaf_score = next(r["score"] for r in result["results"] if r["path"] == "leaf.md")
        assert hub_score > leaf_score

    def test_links_counts_reflect_graph(self, vault):
        result = vault_query("hub")
        hub_result = next(r for r in result["results"] if r["path"] == "hub.md")
        assert hub_result["links_in"] == 4
        assert hub_result["links_out"] == 0


class TestVaultQueryLexical:
    def test_pure_lexical_match_with_no_graph_presence(self, vault):
        result = vault_query("zzzunique")
        assert any(r["path"] == "solo.md" for r in result["results"])

    def test_snippet_contains_query_term(self, vault):
        result = vault_query("orchestration")
        hub_result = next(r for r in result["results"] if r["path"] == "hub.md")
        assert "orchestration" in hub_result["snippet"].lower()

    def test_no_match_returns_empty_results_with_graph_stats(self, vault):
        result = vault_query("nosuchtermexistsatallxyz")
        assert result["results"] == []
        assert result["graph"]["notes"] == 13
        assert result["graph"]["seeds"] == 0


class TestVaultQueryGraphStats:
    def test_graph_stats_for_known_vault(self, vault):
        result = vault_query("hub")
        assert result["graph"]["notes"] == 13
        assert result["graph"]["edges"] == 4


class TestVaultQueryCaps:
    def test_max_results_cap_respected(self, vault):
        result = vault_query("keyword123", max_results=3)
        assert len(result["results"]) == 3


class TestVaultQueryGuardRails:
    def test_empty_query_rejected(self, vault):
        with pytest.raises(ToolError, match="query"):
            vault_query("   ")

    def test_non_dir_path_rejected(self, vault):
        with pytest.raises(ToolError, match="not a directory"):
            vault_query("orchestration", path="hub.md")
