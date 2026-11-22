from unittest.mock import MagicMock

from unified_agent.tools.web import web_search


class TestWebSearchCache:
    def test_keyed_provider_cache(self, monkeypatch):
        # Setup mock brave provider
        mock_results = [{"title": "Test Title", "url": "http://test.com", "snippet": "snippet"}]
        mock_brave = MagicMock(return_value=mock_results)

        from unified_agent.tools import web

        monkeypatch.setitem(web._PROVIDERS, "brave", mock_brave)
        monkeypatch.setenv("UA_SEARCH_PROVIDER", "brave")
        monkeypatch.setenv("UA_SEARCH_CACHE_TTL_MIN", "10")

        # First call: cache miss
        res1 = web_search("query text", max_results=5)
        assert res1["provider"] == "brave"
        assert res1["cached"] is False
        assert mock_brave.call_count == 1

        # Second call: cache hit
        res2 = web_search("query text", max_results=5)
        assert res2["provider"] == "brave"
        assert res2["cached"] is True
        assert "fetched_at" in res2
        assert mock_brave.call_count == 1  # Still 1 call

    def test_cache_disabled_when_ttl_is_zero(self, monkeypatch):
        mock_results = [{"title": "Test Title", "url": "http://test.com", "snippet": "snippet"}]
        mock_brave = MagicMock(return_value=mock_results)

        from unified_agent.tools import web

        monkeypatch.setitem(web._PROVIDERS, "brave", mock_brave)
        monkeypatch.setenv("UA_SEARCH_PROVIDER", "brave")
        monkeypatch.setenv("UA_SEARCH_CACHE_TTL_MIN", "0")

        # First call
        res1 = web_search("query test 2", max_results=5)
        assert res1["cached"] is False

        # Second call
        res2 = web_search("query test 2", max_results=5)
        assert res2["cached"] is False
        assert mock_brave.call_count == 2

    def test_unkeyed_provider_not_cached(self, monkeypatch):
        mock_results = [{"title": "Test Title", "url": "http://test.com", "snippet": "snippet"}]
        mock_ddg = MagicMock(return_value=mock_results)

        from unified_agent.tools import web

        monkeypatch.setitem(web._PROVIDERS, "ddg", mock_ddg)
        monkeypatch.setenv("UA_SEARCH_PROVIDER", "ddg")
        monkeypatch.setenv("UA_SEARCH_CACHE_TTL_MIN", "10")

        # First call
        res1 = web_search("query test 3", max_results=5)
        assert res1["cached"] is False

        # Second call
        res2 = web_search("query test 3", max_results=5)
        assert res2["cached"] is False
        assert mock_ddg.call_count == 2
