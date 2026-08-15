"""Regression tests for the minimal YT Music connection API."""

from __future__ import annotations

import json

from jarvis import app as app_module
from jarvis.media import ytm_web


async def test_ytm_connection_get_returns_backend_truth(monkeypatch) -> None:
    async def status() -> dict[str, object]:
        return {
            "state": ytm_web.NEEDS_LOGIN,
            "connected": False,
            "needs_login": True,
            "page_ready": True,
            "search_ready": True,
            "player_loaded": False,
            "playing": None,
            "error": None,
        }

    monkeypatch.setattr(ytm_web, "connection_status", status)

    response = await app_module.api_ytm_connection()

    assert json.loads(response.body) == await status()


async def test_ytm_connect_endpoint_returns_connection_result(monkeypatch) -> None:
    expected = {
        "state": ytm_web.CONNECTED,
        "connected": True,
        "needs_login": False,
        "page_ready": True,
        "search_ready": True,
        "player_loaded": False,
        "playing": None,
        "error": None,
    }

    async def connect() -> dict[str, object]:
        return expected

    monkeypatch.setattr(ytm_web, "connect", connect)

    response = await app_module.api_ytm_connect()

    assert json.loads(response.body) == expected
