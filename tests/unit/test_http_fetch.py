"""Unit tests for tools/definitions/http_fetch.py — including SSRF guard."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from tools.definitions.http_fetch import http_fetch, validate_url


class TestValidateUrl:
    def test_valid_public_url(self):
        assert validate_url("https://example.com/page") is None

    def test_rejects_private_10(self):
        result = validate_url("http://10.0.0.1/secret")
        assert result is not None

    def test_rejects_private_172(self):
        result = validate_url("http://172.16.0.1/admin")
        assert result is not None

    def test_rejects_private_192(self):
        result = validate_url("http://192.168.1.1/admin")
        assert result is not None

    def test_rejects_loopback(self):
        result = validate_url("http://127.0.0.1/")
        assert result is not None

    def test_rejects_localhost(self):
        result = validate_url("http://localhost/admin")
        assert result is not None

    def test_rejects_metadata_endpoint(self):
        result = validate_url("http://169.254.169.254/latest/meta-data/")
        assert result is not None

    def test_rejects_non_http_scheme(self):
        result = validate_url("ftp://example.com/file")
        assert result is not None and "scheme" in result.lower()

    def test_rejects_file_scheme(self):
        result = validate_url("file:///etc/passwd")
        assert result is not None and "scheme" in result.lower()

    def test_allows_http(self):
        assert validate_url("http://example.com") is None

    def test_allows_https(self):
        assert validate_url("https://example.com") is None


class TestHttpFetch:
    async def test_fetches_and_extracts(self, test_settings, monkeypatch):
        html = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.read = AsyncMock(return_value=html.encode())
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.http_fetch.aiohttp.ClientSession", return_value=mock_session):
            result = await http_fetch(url="https://example.com", max_chars=5000)

        assert result["url"] == "https://example.com"
        assert len(result["content"]) > 0

    async def test_ssrf_blocked(self, test_settings):
        result = await http_fetch(url="http://169.254.169.254/latest/meta-data/")
        assert "error" in result

    async def test_truncation(self, test_settings, monkeypatch):
        long_text = "word " * 5000  # 25000 chars of text
        html = f"<html><head><title>Test</title></head><body><p>{long_text}</p></body></html>"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.charset = "utf-8"
        mock_resp.content_length = len(html.encode())
        mock_resp.read = AsyncMock(return_value=html.encode())
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.http_fetch.aiohttp.ClientSession", return_value=mock_session):
            result = await http_fetch(url="https://example.com", max_chars=100)

        assert len(result["content"]) <= 100
        assert result["truncated"] is True
