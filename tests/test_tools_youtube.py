"""Unit testovi za reuse logiku play_youtube stranice (Faza 6, T2).

Playwright objekti su lažirani — Chrome se ne pokreće.
"""

import sys
import types

from jarvis.agent import tools


class FakePage:
    def __init__(self, closed: bool = False) -> None:
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, fail_new_page: bool = False) -> None:
        self.pages: list[FakePage] = []
        self.fail_new_page = fail_new_page

    async def new_page(self) -> FakePage:
        if self.fail_new_page:
            raise RuntimeError("context dead")
        page = FakePage()
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return self._connected

    async def new_context(self) -> FakeContext:
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx


class FakeChromium:
    def __init__(self) -> None:
        self.launches: list[dict] = []

    async def launch(self, **kwargs) -> FakeBrowser:
        self.launches.append(kwargs)
        return FakeBrowser(connected=True)


class FakePW:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class FakeAsyncPlaywrightHandle:
    async def start(self) -> FakePW:
        return FakePW()


def _install_fake_playwright(monkeypatch) -> None:
    fake_module = types.ModuleType("playwright.async_api")
    fake_module.async_playwright = FakeAsyncPlaywrightHandle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)


async def test_live_page_reused(monkeypatch) -> None:
    page = FakePage(closed=False)
    monkeypatch.setattr(tools, "_youtube_page", page)
    monkeypatch.setattr(tools, "_youtube_browser", FakeBrowser())
    monkeypatch.setattr(tools, "_youtube_context", FakeContext())
    result = await tools._ensure_youtube_page()
    assert result is page


async def test_closed_page_new_page_same_context(monkeypatch) -> None:
    browser = FakeBrowser(connected=True)
    ctx = FakeContext()
    monkeypatch.setattr(tools, "_youtube_page", FakePage(closed=True))
    monkeypatch.setattr(tools, "_youtube_browser", browser)
    monkeypatch.setattr(tools, "_youtube_context", ctx)
    result = await tools._ensure_youtube_page()
    assert result is ctx.pages[0]
    assert len(ctx.pages) == 1
    assert browser.contexts == []  # context NIJE rekreiran


async def test_dead_context_recreated(monkeypatch) -> None:
    browser = FakeBrowser(connected=True)
    dead_ctx = FakeContext(fail_new_page=True)
    monkeypatch.setattr(tools, "_youtube_page", None)
    monkeypatch.setattr(tools, "_youtube_browser", browser)
    monkeypatch.setattr(tools, "_youtube_context", dead_ctx)
    result = await tools._ensure_youtube_page()
    assert len(browser.contexts) == 1
    assert result is browser.contexts[0].pages[0]


async def test_disconnected_browser_full_restart(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(tools, "_youtube_page", FakePage(closed=True))
    monkeypatch.setattr(tools, "_youtube_browser", FakeBrowser(connected=False))
    monkeypatch.setattr(tools, "_youtube_context", FakeContext())
    monkeypatch.setattr(tools, "_youtube_pw", None)
    result = await tools._ensure_youtube_page()
    new_browser = tools._youtube_browser
    assert isinstance(new_browser, FakeBrowser)
    assert new_browser.is_connected()
    assert result is new_browser.contexts[0].pages[0]
    # stari context je zamenjen novim (ne reuse-uje se posle restarta browsera)
    assert len(new_browser.contexts) == 1
