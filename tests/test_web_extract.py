from unittest.mock import MagicMock, patch

from unified_agent.pdf_worker import PdfExtractionResult
from unified_agent.tools.web import web_fetch


class TestWebExtract:
    def test_pdf_extraction_enabled(self, monkeypatch):
        """web_fetch hands PDF bytes to the confined worker and returns its text.

        The worker itself runs in a subprocess and is exercised directly in
        tests/test_pdf_worker.py; here only the wiring in web_fetch is under
        test, so the worker boundary (extract_pdf_text_confined) is mocked.
        """
        import socket

        monkeypatch.setattr(
            "unified_agent.safety.socket.getaddrinfo",
            lambda host, port, *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ],
        )

        # Mock the network stream to return PDF content
        import httpx

        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "application/pdf"}
        mock_resp.url = httpx.URL("http://example.com/test.pdf")
        mock_resp.iter_bytes.return_value = [b"%PDF-1.4 ..."]

        mock_client = MagicMock()
        client_instance = mock_client.__enter__.return_value
        client_instance.stream.return_value.__enter__.return_value = mock_resp

        # Patch httpx.Client and check_fetch_host for hermetic testing
        with (
            patch("httpx.Client", return_value=mock_client),
            patch("unified_agent.tools.web.check_fetch_host", return_value="93.184.216.34"),
            patch(
                "unified_agent.tools.web.extract_pdf_text_confined",
                return_value=PdfExtractionResult(
                    status="ok", text="This is the extracted text of the PDF document."
                ),
            ) as mock_extract,
        ):
            # When pdf_base64 is False (default), it extracts text via the
            # confined worker, passing it the raw downloaded bytes.
            res = web_fetch("http://example.com/test.pdf", pdf_base64=False)
            assert "content" in res
            assert res["content"] == "This is the extracted text of the PDF document."
            assert res["untrusted"] is True
            mock_extract.assert_called_once()
            (called_bytes,), _kwargs = mock_extract.call_args
            assert called_bytes == b"%PDF-1.4 ..."

            # When pdf_base64 is True, it skips the worker and returns base64 media.
            mock_extract.reset_mock()
            res_base64 = web_fetch("http://example.com/test.pdf", pdf_base64=True)
            assert res_base64["kind"] == "pdf"
            assert "base64" in res_base64
            mock_extract.assert_not_called()

    def test_tavily_extract_fallback(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "unified_agent.safety.socket.getaddrinfo",
            lambda host, port, *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ],
        )
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-testkey")

        # Mock Tavily HTTP response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "http://example.com/js-page",
                    "raw_content": "Detailed content extracted via Tavily.",
                }
            ]
        }

        # Mock the primary HTTP get request to return empty/near-empty content
        import httpx

        mock_get_resp = MagicMock()
        mock_get_resp.headers = {"content-type": "text/html"}
        mock_get_resp.url = httpx.URL("http://example.com/js-page")
        mock_get_resp.iter_bytes.return_value = [b"<html><body></body></html>"]

        mock_client = MagicMock()
        client_instance = mock_client.__enter__.return_value
        client_instance.stream.return_value.__enter__.return_value = mock_get_resp

        with (
            patch("httpx.Client", return_value=mock_client),
            patch("httpx.post", return_value=mock_resp) as mock_post,
            patch("unified_agent.tools.web.check_fetch_host", return_value="93.184.216.34"),
        ):
            res = web_fetch("http://example.com/js-page")
            assert "content" in res
            assert res["content"] == "Detailed content extracted via Tavily."
            assert res["extracted_via"] == "tavily"
            assert res["untrusted"] is True
            mock_post.assert_called_once()
