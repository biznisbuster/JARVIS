"""Unit testovi za jarvis/media/ytm_web.py.

Lažiramo Playwright: hvatamo ``page.evaluate`` i vraćamo unapred
definisane rezultate. Ne pokrećemo pravi Chrome.
"""

from __future__ import annotations

import sys
import types

import pytest

from jarvis.media import ytm_web


class FakePage:
    def __init__(
        self,
        *,
        closed: bool = False,
        url: str = "https://music.youtube.com/",
        transition_changes_track: bool = True,
    ) -> None:
        self.closed = closed
        self.url = url
        self.transition_changes_track = transition_changes_track
        self.state = {
            "ok": True,
            "playing": False,
            "title": "Test Song",
            "artist": "Test Artist",
            "track_id": "test-track",
            "ariaLabel": "Play",
            "currentTime": 0,
            "duration": 200,
        }

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str, arg: object = None):  # noqa: ANN001
        if self.state.get("error"):
            return {"ok": False, "error": self.state["error"], "playing": None,
                    "title": "", "artist": ""}
        if "document.querySelector('video')" in script and "title" in script:
            return dict(self.state)
        if "video.play" in script:
            action = arg
            if action == "play":
                self.state["playing"] = True
            elif action == "pause":
                self.state["playing"] = False
            elif action in ("next", "previous"):
                self.state["playing"] = True
                if self.transition_changes_track:
                    self.state["title"] = f"After {action}"
                    self.state["track_id"] = f"{action}-track"
            return {"ok": True, "method": f"fake.{action}"}
        if "HTMLInputElement" in script:
            return {"ok": True}
        if "hasSearch" in script:
            return {"url": "https://music.youtube.com/", "hasSearch": True}
        return {"ok": True}

    async def goto(self, url: str, **kwargs) -> None:  # noqa: ANN003
        self.url = url

    async def wait_for_function(self, expr: str, **kwargs) -> None:  # noqa: ANN003
        return None

    async def wait_for_selector(self, selector: str, **kwargs) -> None:  # noqa: ANN003
        return None

    async def wait_for_url(self, pattern: str, **kwargs) -> None:  # noqa: ANN003
        return None

    async def close(self) -> None:
        self.closed = True


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:  # noqa: ANN001
        return None

    async def click(self) -> None:
        return None


FakePage.locator = lambda self, selector: FakeLocator(self, selector)  # type: ignore[attr-defined]


class FakeKeyboard:
    async def press(self, key: str) -> None:
        return None


FakePage.keyboard = FakeKeyboard()


class FakePersistentContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = [FakePage()]
        self.closed = False
        self.browser = None

    async def close(self) -> None:
        self.closed = True

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class FakeLaunchPersistentContext:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def __call__(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("chrome not installed")
        return FakePersistentContext()


class FakePW:
    def __init__(self, *, launch_should_fail: bool = False) -> None:
        self.chromium = types.SimpleNamespace(
            launch_persistent_context=FakeLaunchPersistentContext(fail=launch_should_fail)
        )


def _install_fake_playwright(monkeypatch, *, launch_should_fail: bool = False) -> FakePW:
    fake_pw = FakePW(launch_should_fail=launch_should_fail)
    fake_module = types.ModuleType("playwright.async_api")

    class FakeHandle:
        def __init__(self, pw: FakePW) -> None:
            self.pw = pw

        async def start(self) -> FakePW:
            return self.pw

    fake_module.async_playwright = lambda: FakeHandle(fake_pw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    return fake_pw


@pytest.fixture(autouse=True)
def _reset_ytm_web(monkeypatch):
    monkeypatch.setattr(ytm_web, "_pw", None)
    monkeypatch.setattr(ytm_web, "_context", None)
    monkeypatch.setattr(ytm_web, "_ytm_page", None)
    monkeypatch.setattr(ytm_web, "_browser", None)
    monkeypatch.setattr(ytm_web, "_launched", False)
    monkeypatch.setattr(ytm_web, "_ready", False)
    monkeypatch.setattr(ytm_web, "_active_profile", None)
    monkeypatch.setattr(ytm_web, "_lock", None)
    monkeypatch.setattr(ytm_web, "_warmup_task", None)


@pytest.fixture(autouse=True)
def _force_jarvis_profile(monkeypatch):
    monkeypatch.setattr(ytm_web, "_JARVIS_PROFILE_DIR", ytm_web.Path("/tmp/jarvis_ytm_test_profile"))


async def test_is_available_false_when_not_launched(monkeypatch) -> None:
    assert ytm_web.is_available() is False


async def test_is_available_true_when_ready(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))
    assert ytm_web.is_available() is True


async def test_dead_ytm_page_is_not_reported_ready(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(ytm_web, "_ytm_page", FakePage(closed=True))
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))

    assert ytm_web.is_available() is False


async def test_get_state_not_ready_returns_error(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch, launch_should_fail=True)
    result = await ytm_web.get_state()
    assert result["ok"] is False
    assert result["playing"] is None


async def test_get_state_reads_dom(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = True
    page.state["title"] = "Foo"
    page.state["artist"] = "Bar"
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))
    result = await ytm_web.get_state()
    assert result["ok"] is True
    assert result["playing"] is True
    assert result["title"] == "Foo"
    assert result["artist"] == "Bar"


async def test_control_next_verifies_track_transition(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = True
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))

    result = await ytm_web.control("next")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["delivered"] is True
    assert result["track_changed"] is True
    assert result["before"]["track_id"] == "test-track"
    assert result["after"]["track_id"] == "next-track"


async def test_control_next_does_not_use_playing_as_track_verification(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(transition_changes_track=False)
    page.state["playing"] = True
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))

    result = await ytm_web.control("next")

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["delivered"] is True
    assert result["verification"] == "failed"
    assert result["track_changed"] is False


async def test_play_query_empty(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))
    result = await ytm_web.play_query("   ")
    assert result["ok"] is False
    assert "empty" in result["error"]


async def test_play_query_success(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))
    result = await ytm_web.play_query("test song")
    assert result["ok"] is True
    assert result["query"] == "test song"


async def test_ensure_ready_launches_once(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    result = await ytm_web.ensure_ready()
    assert result is True
    assert ytm_web._launched is True


async def test_ensure_ready_returns_false_when_chrome_missing(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch, launch_should_fail=True)
    result = await ytm_web.ensure_ready()
    assert result is False


async def test_ensure_ready_idempotent(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    await ytm_web.ensure_ready()
    first = ytm_web._context
    await ytm_web.ensure_ready()
    assert ytm_web._context is first


async def test_warm_up_runs_in_background(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web.warm_up()
    assert ytm_web._warmup_task is not None
    await ytm_web._warmup_task
    assert ytm_web._launched is True


async def test_shutdown_clears_state(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    await ytm_web.ensure_ready()
    await ytm_web.shutdown()
    assert ytm_web._ytm_page is None
    assert ytm_web._context is None
    assert ytm_web._launched is False
