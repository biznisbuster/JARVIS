"""Ordinary YouTube Playwright tool.

The persistent browser/page behavior is intentionally unchanged from the
pre-Phase-5 implementation.  Broader browser lifecycle ownership remains a
later roadmap phase.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

_youtube_pw: Any = None
_youtube_browser: Any = None
_youtube_context: Any = None
_youtube_page: Any = None


async def ensure_youtube_page() -> Any:
    """Return a live persistent Playwright page, recreating dead pieces."""

    global _youtube_pw, _youtube_browser, _youtube_context, _youtube_page
    if _youtube_page is not None:
        try:
            if not _youtube_page.is_closed():
                return _youtube_page
        except Exception:  # noqa: BLE001
            pass
        _youtube_page = None
    if _youtube_browser is not None:
        try:
            connected = _youtube_browser.is_connected()
        except Exception:  # noqa: BLE001
            connected = False
        if not connected:
            _youtube_browser = None
            _youtube_context = None
    if _youtube_browser is None:
        from playwright.async_api import async_playwright

        if _youtube_pw is None:
            _youtube_pw = await async_playwright().start()
        try:
            _youtube_browser = await _youtube_pw.chromium.launch(headless=False, channel="chrome")
        except Exception:  # noqa: BLE001
            _youtube_browser = await _youtube_pw.chromium.launch(headless=False)
        _youtube_context = None
    if _youtube_context is None:
        _youtube_context = await _youtube_browser.new_context()
    try:
        _youtube_page = await _youtube_context.new_page()
    except Exception:  # noqa: BLE001
        _youtube_context = await _youtube_browser.new_context()
        _youtube_page = await _youtube_context.new_page()
    return _youtube_page


async def play_youtube(
    args: dict[str, Any],
    *,
    page_factory: Callable[[], Awaitable[Any]] | None = None,
) -> str:
    """Open Chrome, search YouTube and play the first video for a query."""

    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"ok": False, "error_code": "INVALID_ARGUMENTS", "error": "query is required"})
    try:
        page = await (page_factory or ensure_youtube_page)()
    except ModuleNotFoundError as exc:
        return json.dumps(
            {"ok": False, "error_code": "DEPENDENCY_MISSING", "error": f"playwright not installed: {exc}"},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"ok": False, "error": f"could not start YouTube browser: {exc}"}, ensure_ascii=False
        )

    try:
        await page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=20000)
        try:
            consent = page.get_by_role("button", name="Accept all")
            if await consent.count():
                await consent.first.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        search = page.locator('input[name="search_query"]')
        await search.wait_for(state="visible", timeout=10000)
        await search.fill(query)
        await page.keyboard.press("Enter")
        await page.wait_for_selector("ytd-video-renderer", timeout=10000)
        first = page.locator("ytd-video-renderer a#thumbnail").first
        await first.click()
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        title = await page.title()
        return json.dumps(
            {"ok": True, "query": query, "title": title, "url": page.url},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "query": query, "error": repr(exc)}, ensure_ascii=False)
