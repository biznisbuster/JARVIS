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
        authenticated: bool = True,
        playable_href: str = "/watch?v=test-track",
    ) -> None:
        self.closed = closed
        self.url = url
        self.transition_changes_track = transition_changes_track
        self.authenticated = authenticated
        self.playable_href = playable_href
        self.state = {
            "ok": True,
            "playing": False,
            "player_loaded": True,
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
            return {"ok": False, "error": self.state["error"], "playing": None, "title": "", "artist": ""}
        if "pageReady" in script:
            return {
                "ok": True,
                "page_ready": "music.youtube.com" in self.url,
                "search_ready": "music.youtube.com" in self.url,
                "authenticated": self.authenticated,
                "login_required": not self.authenticated,
                "player_loaded": self.state.get("player_loaded", True),
                "playing": self.state.get("playing"),
                "track_id": self.state.get("track_id", ""),
            }
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
        self.page.state["playing"] = True
        self.page.state["player_loaded"] = True
        self.page.state["track_id"] = self.page.playable_href.split("v=", 1)[-1]
        self.page.state["title"] = "Selected Result"
        self.page.state["artist"] = "Selected Artist"

    async def get_attribute(self, name: str) -> str | None:
        return self.page.playable_href if name == "href" else None


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
    monkeypatch.setattr(ytm_web, "_active_profile", None)
    monkeypatch.setattr(ytm_web, "_lock", None)
    monkeypatch.setattr(ytm_web, "_warmup_task", None)
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.DISCONNECTED)
    monkeypatch.setattr(ytm_web, "_connection_error", None)
    monkeypatch.setattr(ytm_web, "_page_ready", False)
    monkeypatch.setattr(ytm_web, "_search_ready", False)
    monkeypatch.setattr(ytm_web, "_player_loaded", False)
    monkeypatch.setattr(ytm_web, "_playing", None)


@pytest.fixture(autouse=True)
def _force_jarvis_profile(monkeypatch):
    monkeypatch.setattr(ytm_web, "_JARVIS_PROFILE_DIR", ytm_web.Path("/tmp/jarvis_ytm_test_profile"))


def _set_connected_runtime(monkeypatch, page: FakePage) -> None:
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    monkeypatch.setattr(ytm_web, "_context", object())
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.CONNECTED)
    monkeypatch.setattr(ytm_web, "_page_ready", True)
    monkeypatch.setattr(ytm_web, "_search_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))


async def test_is_available_false_when_not_launched(monkeypatch) -> None:
    assert ytm_web.is_available() is False


async def test_connection_starts_disconnected(monkeypatch) -> None:
    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.DISCONNECTED
    assert status["connected"] is False
    assert status["needs_login"] is False
    assert status["page_ready"] is False
    assert status["search_ready"] is False


async def test_connect_launches_a_headed_persistent_profile(monkeypatch) -> None:
    fake_pw = _install_fake_playwright(monkeypatch)

    status = await ytm_web.connect()

    assert status["state"] == ytm_web.CONNECTED
    launch = fake_pw.chromium.launch_persistent_context
    assert launch.calls[0]["headless"] is False
    assert launch.calls[0]["user_data_dir"].endswith("ytm_test_profile")


async def test_login_required_state_is_distinct_from_disconnected(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    _set_connected_runtime(monkeypatch, FakePage(authenticated=False))

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.NEEDS_LOGIN
    assert status["connected"] is False
    assert status["needs_login"] is True
    assert status["page_ready"] is True
    assert status["search_ready"] is True


async def test_connected_search_ready_page_without_track_is_valid(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state.update({"player_loaded": False, "playing": None, "title": "", "artist": "", "track_id": ""})
    _set_connected_runtime(monkeypatch, page)

    status = await ytm_web.connection_status()
    state = await ytm_web.get_state()

    assert status["state"] == ytm_web.CONNECTED
    assert status["page_ready"] is True
    assert status["search_ready"] is True
    assert status["player_loaded"] is False
    assert state["ok"] is True
    assert state["player_loaded"] is False
    assert state["playing"] is None


async def test_expired_session_becomes_needs_login(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    assert (await ytm_web.connection_status())["state"] == ytm_web.CONNECTED

    page.authenticated = False
    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.NEEDS_LOGIN
    assert status["connected"] is False


async def test_is_available_true_when_ready(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    assert ytm_web.is_available() is True


async def test_dead_ytm_page_is_not_reported_ready(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    _set_connected_runtime(monkeypatch, FakePage(closed=True))

    assert ytm_web.is_available() is False


async def test_get_state_not_ready_returns_error(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch, launch_should_fail=True)
    result = await ytm_web.get_state()
    assert result["ok"] is False
    assert result["playing"] is None


async def test_connect_launch_failure_reports_error_state(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch, launch_should_fail=True)

    status = await ytm_web.connect()

    assert status["state"] == ytm_web.ERROR
    assert status["connected"] is False
    assert status["error"]


async def test_get_state_reads_dom(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = True
    page.state["title"] = "Foo"
    page.state["artist"] = "Bar"
    _set_connected_runtime(monkeypatch, page)
    result = await ytm_web.get_state()
    assert result["ok"] is True
    assert result["playing"] is True
    assert result["title"] == "Foo"
    assert result["artist"] == "Bar"


async def test_control_next_verifies_track_transition(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

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
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("next")

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["delivered"] is True
    assert result["verification"] == "failed"
    assert result["track_changed"] is False


@pytest.mark.parametrize(
    ("action", "before_playing", "after_playing"),
    [("pause", True, False), ("play", False, True)],
)
async def test_control_pause_resume_verifies_dom_state(
    monkeypatch,
    action: str,
    before_playing: bool,
    after_playing: bool,
) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = before_playing
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control(action)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["after"]["playing"] is after_playing


async def test_play_query_empty(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    result = await ytm_web.play_query("   ")
    assert result["ok"] is False
    assert "empty" in result["error"]


async def test_play_query_success(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    result = await ytm_web.play_query("test song")
    assert result["ok"] is True
    assert result["query"] == "test song"
    assert result["verified"] is True
    assert result["selected_video_id"] == "test-track"
    assert result["title"] == "Selected Result"
    assert result["artist"] == "Selected Artist"


async def test_play_query_starts_from_connected_page_without_player(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state.update({"player_loaded": False, "playing": None, "title": "", "artist": "", "track_id": ""})
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.play_query("fresh song")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["selected_video_id"] == "test-track"


async def test_second_play_query_changes_track_in_same_session(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(playable_href="/watch?v=first-track")
    _set_connected_runtime(monkeypatch, page)

    first = await ytm_web.play_query("artist A")
    page.playable_href = "/watch?v=second-track"
    second = await ytm_web.play_query("artist B")

    assert first["ok"] is True
    assert first["actual_video_id"] == "first-track"
    assert second["ok"] is True
    assert second["actual_video_id"] == "second-track"


async def test_transport_requires_loaded_player(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state.update({"player_loaded": False, "playing": None, "title": "", "artist": "", "track_id": ""})
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("next")

    assert result["ok"] is False
    assert result["delivered"] is False
    assert result["verification"] == "not_attempted"
    assert "no loaded track" in result["error"]


async def test_ensure_ready_launches_once(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    result = await ytm_web.ensure_ready()
    assert result is True
    assert ytm_web._launched is True


async def test_ensure_ready_returns_false_when_chrome_missing(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch, launch_should_fail=True)
    result = await ytm_web.ensure_ready()
    assert result is False


async def test_ensure_ready_idempotent(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    await ytm_web.ensure_ready()
    first = ytm_web._context
    await ytm_web.ensure_ready()
    assert ytm_web._context is first


async def test_warm_up_runs_in_background(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ytm_web.warm_up()
    assert ytm_web._warmup_task is not None
    await ytm_web._warmup_task
    assert ytm_web._launched is True


async def test_shutdown_clears_state(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    await ytm_web.ensure_ready()
    await ytm_web.shutdown()
    assert ytm_web._ytm_page is None
    assert ytm_web._context is None
    assert ytm_web._launched is False
