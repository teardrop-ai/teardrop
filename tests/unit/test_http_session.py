"""Unit tests for tools/definitions/_http_session.py."""

from __future__ import annotations


class TestHttpSession:
    async def test_coingecko_session_uses_force_close_connector(self, test_settings, monkeypatch):
        from tools.definitions import _http_session

        monkeypatch.setattr(_http_session, "_coingecko_session", None)
        monkeypatch.setattr(_http_session, "_defillama_session", None)
        monkeypatch.setattr(_http_session, "_session_lock", None)

        session = await _http_session.get_coingecko_session()
        try:
            assert session.connector is not None
            assert session.connector.force_close is True
        finally:
            await _http_session.close_http_sessions()

    async def test_defillama_session_same_instance(self, test_settings, monkeypatch):
        from tools.definitions import _http_session

        monkeypatch.setattr(_http_session, "_coingecko_session", None)
        monkeypatch.setattr(_http_session, "_defillama_session", None)
        monkeypatch.setattr(_http_session, "_session_lock", None)

        s1 = await _http_session.get_defillama_session()
        s2 = await _http_session.get_defillama_session()
        try:
            assert s1 is s2
            assert s1.connector is not None
            assert s1.connector.force_close is True
        finally:
            await _http_session.close_http_sessions()

    async def test_close_http_sessions_closes_defillama(self, test_settings, monkeypatch):
        from tools.definitions import _http_session

        monkeypatch.setattr(_http_session, "_coingecko_session", None)
        monkeypatch.setattr(_http_session, "_defillama_session", None)
        monkeypatch.setattr(_http_session, "_session_lock", None)

        session = await _http_session.get_defillama_session()
        assert session.closed is False

        await _http_session.close_http_sessions()

        assert _http_session._defillama_session is None
