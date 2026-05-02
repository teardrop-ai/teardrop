"""Unit tests for tools/definitions/_http_session.py."""

from __future__ import annotations


class TestHttpSession:
    async def test_coingecko_session_uses_force_close_connector(self, test_settings, monkeypatch):
        from tools.definitions import _http_session

        monkeypatch.setattr(_http_session, "_coingecko_session", None)
        monkeypatch.setattr(_http_session, "_session_lock", None)

        session = await _http_session.get_coingecko_session()
        try:
            assert session.connector is not None
            assert session.connector.force_close is True
        finally:
            await _http_session.close_http_sessions()
