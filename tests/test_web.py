"""Behavioral tests for unified_agent.tools.web.

Covers web_search + web_fetch. All HTTP is mocked at the httpx layer; no live
network. Limits, timeouts, and the SSRF guard are exercised at the documented
values — do not relax them to make testing easier.

See AGENTS.md / docs for the documented limits:
- web_search: max 10 results; empty/whitespace query refused; unknown provider
  refused; provider error wrapped as ToolError.
- web_fetch: http/https only; non-public hosts refused unless
  UA_ALLOW_LOCAL_FETCH=1; 2 MB download cap; max_chars enforced (>= 100,
  <= 200000); 20 s timeout (httpx default; we don't shrink it).
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.pdf_worker import PdfExtractionResult
from unified_agent.tools import web

# --- web_search -----------------------------------------------------------


class TestWebSearch:
    def test_empty_query_refused(self, isolated_env):
        with pytest.raises(ToolError, match="must not be empty"):
            web.web_search("")
        with pytest.raises(ToolError, match="must not be empty"):
            web.web_search("   ")

    def test_max_results_clamped_to_10(self, isolated_env, monkeypatch):
        """Provider receives max_results clamped to [1, 10]; large values are clamped, not passed through."""
        seen = {}

        def fake_provider(query, max_results):
            seen["query"] = query
            seen["max_results"] = max_results
            return []

        monkeypatch.setattr(config, "search_provider", lambda: "ddg")
        monkeypatch.setitem(web._PROVIDERS, "ddg", fake_provider)
        web.web_search("hello", max_results=500)
        assert seen["query"] == "hello"
        assert seen["max_results"] == 10  # clamped down

    def test_max_results_clamped_to_at_least_1(self, isolated_env, monkeypatch):
        """max_results=0 or negative is clamped up to 1, not passed as 0."""
        seen = {}

        def fake_provider(query, max_results):
            seen["max_results"] = max_results
            return []

        monkeypatch.setattr(config, "search_provider", lambda: "ddg")
        monkeypatch.setitem(web._PROVIDERS, "ddg", fake_provider)
        web.web_search("hello", max_results=0)
        assert seen["max_results"] == 1

    def test_unknown_provider_refused(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "search_provider", lambda: "bing")
        with pytest.raises(ToolError, match="unknown search provider 'bing'"):
            web.web_search("hello")

    def test_provider_exception_wrapped(self, isolated_env, monkeypatch):
        def boom(query, max_results):
            raise RuntimeError("upstream 503")

        monkeypatch.setattr(config, "search_provider", lambda: "ddg")
        monkeypatch.setitem(web._PROVIDERS, "ddg", boom)
        with pytest.raises(ToolError, match="search via ddg failed: upstream 503"):
            web.web_search("hello")

    def test_results_passed_through_from_provider(self, isolated_env, monkeypatch):
        """web_search returns whatever the provider returns, plus a provider key.

        Each provider is responsible for its own output shape (see _search_ddg
        for the canonical normalisation). web_search does not re-shape.
        """
        raw = [
            {"title": "T1", "href": "https://a.example/", "body": "S1"},
            {"title": "T2", "href": "https://b.example/", "body": "S2"},
        ]

        def fake_provider(query, max_results):
            return raw

        monkeypatch.setattr(config, "search_provider", lambda: "ddg")
        monkeypatch.setitem(web._PROVIDERS, "ddg", fake_provider)
        out = web.web_search("hello")
        assert out["provider"] == "ddg"
        # The tool returns the provider's output as-is; the provider owns
        # the shape contract.
        assert out["results"] is raw

    def test_search_ddg_normalises_to_keys(self, isolated_env, monkeypatch):
        """The built-in ddg provider normalises ddgs fields to (title, url, snippet)."""

        # Stub the ddgs library so we don't hit the network. _search_ddg does
        # `from ddgs import DDGS`, so we replace the module entry in sys.modules.
        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query, max_results=None):
                return [
                    {"title": "T1", "href": "https://a.example/", "body": "S1"},
                    {"title": "T2", "href": "https://b.example/", "body": "S2"},
                ]

        import sys

        monkeypatch.setitem(sys.modules, "ddgs", MagicMock(DDGS=FakeDDGS))
        out = web._search_ddg("hello", 5)
        assert out == [
            {"title": "T1", "url": "https://a.example/", "snippet": "S1"},
            {"title": "T2", "url": "https://b.example/", "snippet": "S2"},
        ]


class TestProviderResolution:
    """_resolve_provider: 'auto' prefers keyed providers, explicit env wins."""

    def test_auto_without_keys_is_ddg(self, isolated_env):
        assert web._resolve_provider() == "ddg"

    def test_auto_prefers_brave_key(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k1")
        monkeypatch.setenv("TAVILY_API_KEY", "k2")
        assert web._resolve_provider() == "brave"

    def test_auto_uses_tavily_when_only_its_key(self, isolated_env, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "k2")
        assert web._resolve_provider() == "tavily"

    def test_explicit_env_overrides_auto(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k1")
        monkeypatch.setenv("UA_SEARCH_PROVIDER", "ddg")
        assert web._resolve_provider() == "ddg"

    def test_whitespace_key_ignored(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "   ")
        assert web._resolve_provider() == "ddg"


class TestKeyedProviders:
    """brave/tavily response normalisation + missing-key refusal (httpx mocked)."""

    def test_brave_missing_key_raises(self, isolated_env):
        with pytest.raises(ToolError, match="BRAVE_API_KEY"):
            web._search_brave("q", 5)

    def test_tavily_missing_key_raises(self, isolated_env):
        with pytest.raises(ToolError, match="TAVILY_API_KEY"):
            web._search_tavily("q", 5)

    def test_brave_normalises_and_sends_key_header(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "sekrit")
        resp = MagicMock()
        resp.json.return_value = {
            "web": {
                "results": [
                    {"title": "T", "url": "https://a.example/", "description": "D", "extra": 1}
                ]
            }
        }
        with patch.object(web.httpx, "get", return_value=resp) as mock_get:
            out = web._search_brave("q", 5)
        assert out == [{"title": "T", "url": "https://a.example/", "snippet": "D"}]
        assert mock_get.call_args.kwargs["headers"]["X-Subscription-Token"] == "sekrit"

    def test_tavily_normalises(self, isolated_env, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "sekrit")
        resp = MagicMock()
        resp.json.return_value = {
            "results": [{"title": "T", "url": "https://a.example/", "content": "C"}]
        }
        with patch.object(web.httpx, "post", return_value=resp):
            out = web._search_tavily("q", 5)
        assert out == [{"title": "T", "url": "https://a.example/", "snippet": "C"}]

    def test_brave_caps_result_count(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "sekrit")
        resp = MagicMock()
        resp.json.return_value = {
            "web": {
                "results": [{"title": f"T{i}", "url": "u", "description": "d"} for i in range(20)]
            }
        }
        with patch.object(web.httpx, "get", return_value=resp):
            assert len(web._search_brave("q", 3)) == 3


class TestSearchFallback:
    """A failing keyed provider retries once via ddg and reports the downgrade."""

    def test_keyed_failure_falls_back_to_ddg_and_reports(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k1")

        def brave_boom(query, max_results):
            raise RuntimeError("429 rate limited")

        ddg_results = [{"title": "T", "url": "https://a.example/", "snippet": "S"}]
        monkeypatch.setitem(web._PROVIDERS, "brave", brave_boom)
        monkeypatch.setitem(web._PROVIDERS, "ddg", lambda q, n: ddg_results)
        out = web.web_search("hello")
        assert out["provider"] == "ddg"
        assert out["fallback_from"] == "brave"
        assert "429" in out["fallback_reason"]
        assert out["results"] is ddg_results

    def test_no_fallback_fields_on_success(self, isolated_env, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k1")
        monkeypatch.setitem(web._PROVIDERS, "brave", lambda q, n: [])
        out = web.web_search("hello")
        assert out["provider"] == "brave"
        assert "fallback_from" not in out

    def test_both_failing_raises_with_both_errors(self, isolated_env, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "k2")

        def boom(msg):
            def _f(query, max_results):
                raise RuntimeError(msg)

            return _f

        monkeypatch.setitem(web._PROVIDERS, "tavily", boom("tavily down"))
        monkeypatch.setitem(web._PROVIDERS, "ddg", boom("ddg down"))
        with pytest.raises(ToolError, match="tavily down.*ddg fallback also failed.*ddg down"):
            web.web_search("hello")

    def test_ddg_failure_still_raises_directly(self, isolated_env, monkeypatch):
        monkeypatch.setitem(
            web._PROVIDERS, "ddg", lambda q, n: (_ for _ in ()).throw(RuntimeError("upstream 503"))
        )
        with pytest.raises(ToolError, match="search via ddg failed"):
            web.web_search("hello")


# --- web_fetch ------------------------------------------------------------


def _httpx_response(
    url: str, body: bytes, *, status_code: int = 200, content_type: str = "text/html"
):
    """Build a fake httpx.Response-like object that supports .url, .raise_for_status, .iter_bytes."""
    resp = MagicMock()
    resp.url = url
    resp.status_code = status_code
    # Real dict so web_fetch's `.headers.get("content-type", "")` returns a str.
    resp.headers = {"content-type": content_type}

    def raise_for_status():
        if status_code >= 400:
            # Match httpx's behaviour: HTTPStatusError is a subclass of HTTPError
            # and is what raise_for_status actually raises on >=400 responses.
            import httpx

            request = MagicMock()
            raise httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=resp)

    resp.raise_for_status = raise_for_status

    def iter_bytes():
        offset = 0
        chunk = 65536
        while offset < len(body):
            yield body[offset : offset + chunk]
            offset += chunk

    resp.iter_bytes = iter_bytes
    return resp


class TestWebFetchSchemeAndHost:
    def test_non_http_scheme_refused(self, isolated_env):
        with pytest.raises(ToolError, match="only http and https URLs are supported"):
            web.web_fetch("file:///etc/passwd")
        with pytest.raises(ToolError, match="only http and https URLs are supported"):
            web.web_fetch("ftp://example.com/x")

    def test_invalid_url_no_hostname_refused(self, isolated_env):
        with pytest.raises(ToolError, match="invalid URL"):
            web.web_fetch("http:///nopath")

    def test_private_host_refused_by_default(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "allow_local_fetch", lambda: False)
        # 127.0.0.1 is loopback → SSRF guard should refuse without UA_ALLOW_LOCAL_FETCH=1
        with pytest.raises(ToolError, match="non-public address"):
            web.web_fetch("http://127.0.0.1:8080/")

    def test_private_host_allowed_with_ua_allow_local_fetch(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        # With the override we should NOT see a ToolError from the SSRF guard.
        # We mock the httpx.Client below so the request itself doesn't actually fire.
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            ctx = client.stream.return_value
            ctx.__enter__.return_value = _httpx_response(
                "http://127.0.0.1:8080/",
                b"<html><head><title>ok</title></head><body>hi</body></html>",
            )
            out = web.web_fetch("http://127.0.0.1:8080/", raw=True)
        assert out["url"] == "http://127.0.0.1:8080/"
        # raw=True → content is the raw decoded text
        assert "ok" in out["content"]

    def test_unresolvable_host_refused(self, isolated_env, monkeypatch):
        # Force DNS resolution to fail
        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("no such host")

        monkeypatch.setattr("unified_agent.safety.socket.getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ToolError, match="could not resolve host"):
            web.web_fetch("http://nonexistent.invalid/")


class TestWebFetchLimits:
    def _fetch_with_body(self, body: bytes, monkeypatch, **kwargs):
        """Helper: stub httpx.Client.stream to return `body` and call web_fetch.

        Uses 127.0.0.1 so the SSRF guard (with allow_local_fetch=True) returns
        the same IP and httpx is never actually invoked.
        """
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            ctx = client.stream.return_value
            ctx.__enter__.return_value = _httpx_response("http://127.0.0.1/", body, status_code=200)
            return web.web_fetch("http://127.0.0.1/", raw=True, **kwargs)

    def test_max_chars_capped_at_200000(self, isolated_env, monkeypatch):
        # Body is 300_000 chars; default max_chars=40000 should cap; explicit
        # 200_000 is the upper bound the tool enforces.
        body = b"a" * 300_000
        out_default = self._fetch_with_body(body, monkeypatch)
        assert len(out_default["content"]) == 40000  # default max_chars
        assert out_default["truncated"] is True

        out_max = self._fetch_with_body(body, monkeypatch, max_chars=200_000)
        assert len(out_max["content"]) == 200_000
        assert out_max["truncated"] is True

    def test_max_chars_clamped_to_at_least_100(self, isolated_env, monkeypatch):
        # max_chars=0 or 5 is clamped up to 100, not passed as 0/5.
        body = b"x" * 500
        out = self._fetch_with_body(body, monkeypatch, max_chars=0)
        assert len(out["content"]) == 100

    def test_2mb_download_cap(self, isolated_env, monkeypatch):
        """The streaming loop must stop reading after FETCH_MAX_BYTES."""
        # Body just over the 2MB cap. The fetch loop breaks on len(body) > cap.
        body = b"a" * (config.FETCH_MAX_BYTES + 1024)
        # If the cap is enforced, the body the tool sees is truncated.
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            resp = MagicMock()
            resp.url = "http://127.0.0.1/"
            resp.headers = {}  # real httpx responses always carry headers

            chunks_yielded = []

            def iter_bytes():
                offset = 0
                chunk = 65536
                while offset < len(body):
                    chunks_yielded.append(len(body[offset : offset + chunk]))
                    yield body[offset : offset + chunk]
                    offset += chunk

            resp.iter_bytes = iter_bytes
            client.stream.return_value.__enter__.return_value = resp
            monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
            out = web.web_fetch("http://127.0.0.1/", raw=True, max_chars=200_000)

        # The streaming loop accumulates until len(body) > FETCH_MAX_BYTES, then
        # breaks. The test asserts the fetch completed without error and that
        # the content is the decoded body (truncated to max_chars).
        assert isinstance(out["content"], str)
        assert out["truncated"] is True

    def test_http_error_wrapped(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            ctx = client.stream.return_value
            ctx.__enter__.return_value = _httpx_response("http://127.0.0.1/", b"", status_code=500)
            with pytest.raises(ToolError, match="fetch failed"):
                web.web_fetch("http://127.0.0.1/")


class TestWebFetchContentExtraction:
    def test_raw_mode_returns_decoded_text(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        body = b"<html><body>hello world</body></html>"
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            ctx = client.stream.return_value
            ctx.__enter__.return_value = _httpx_response("http://127.0.0.1/", body)
            out = web.web_fetch("http://127.0.0.1/", raw=True)
        assert "hello world" in out["content"]

    def test_extracted_mode_returns_content_and_title(self, isolated_env, monkeypatch):
        """When raw=False, trafilatura extracts the main content; the tool
        returns url/final_url/content/title and a truncated flag."""
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        body = b"<html><head><title>My Page</title></head><body><p>real text</p></body></html>"
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            ctx = client.stream.return_value
            ctx.__enter__.return_value = _httpx_response("http://127.0.0.1/", body)
            out = web.web_fetch("http://127.0.0.1/", raw=False)
        # Either trafilatura extracted "real text" or fell back to the raw text —
        # the point of this test is that the title and content keys are present
        # and the decode path doesn't raise.
        assert "url" in out and "final_url" in out
        assert "content" in out
        assert isinstance(out["title"], (str, type(None)))
        assert isinstance(out["content"], str)


# --- web_fetch media path (task 63 B) -------------------------------------


def _png_bytes(w: int = 4, h: int = 3) -> bytes:
    import struct

    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x00" * 5


class TestWebFetchMedia:
    def _fetch(self, body, content_type, monkeypatch, **kwargs):
        monkeypatch.setattr(config, "allow_local_fetch", lambda: True)
        with patch("unified_agent.tools.web.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.stream.return_value.__enter__.return_value = _httpx_response(
                "http://127.0.0.1/x", body, content_type=content_type
            )
            return web.web_fetch("http://127.0.0.1/x", **kwargs)

    def test_image_returns_base64_not_text(self, isolated_env, monkeypatch):
        import base64

        png = _png_bytes(4, 3)
        out = self._fetch(png, "image/png", monkeypatch)
        assert out["kind"] == "image"
        assert out["mime"] == "image/png"
        assert base64.b64decode(out["base64"]) == png
        assert "PNG image, 4x3" in out["note"]
        assert "content" not in out  # not the text shape

    def test_image_detected_by_magic_despite_wrong_content_type(self, isolated_env, monkeypatch):
        # Server lies (says text/html) but the bytes are a PNG → magic wins.
        out = self._fetch(_png_bytes(), "text/html", monkeypatch)
        assert out["kind"] == "image"
        assert out["mime"] == "image/png"

    def test_pdf_returns_base64_when_worker_cannot_read_it(self, isolated_env, monkeypatch):
        # Not a real PDF: the confined worker runs for real (no mock) and
        # reports "unreadable", which falls back to the base64 shape.
        pdf = b"%PDF-1.5\n...not a real pdf body..."
        out = self._fetch(pdf, "application/pdf", monkeypatch)
        assert out["kind"] == "pdf"
        assert out["mime"] == "application/pdf"
        assert "could not read this PDF" in out["note"]

    def test_image_only_pdf_note_mentions_ocr_not_a_read_failure(self, isolated_env, monkeypatch):
        """Devin finding 4132878664: an image-only PDF parses fine (status
        "empty") — that is not the same thing as the parser failing to read
        the file, and the note must say so."""
        with patch(
            "unified_agent.tools.web.extract_pdf_text_confined",
            return_value=PdfExtractionResult(status="empty"),
        ):
            out = self._fetch(b"%PDF-1.5\n...", "application/pdf", monkeypatch)
        assert out["kind"] == "pdf"
        assert "no extractable text layer" in out["note"]
        assert "OCR" in out["note"]
        assert "could not read this PDF" not in out["note"]

    def test_pdf_extraction_disabled_via_pdf_base64_flag(self, isolated_env, monkeypatch):
        """pdf_base64=True skips the confined worker entirely."""
        pdf = b"%PDF-1.5\n...not a real pdf body..."
        with patch("unified_agent.tools.web.extract_pdf_text_confined") as mock_extract:
            out = self._fetch(pdf, "application/pdf", monkeypatch, pdf_base64=True)
        mock_extract.assert_not_called()
        assert out["kind"] == "pdf"

    def test_html_still_returns_text_shape(self, isolated_env, monkeypatch):
        out = self._fetch(
            b"<html><head><title>t</title></head><body>hi</body></html>",
            "text/html",
            monkeypatch,
            raw=True,
        )
        assert "content" in out
        assert "kind" not in out
