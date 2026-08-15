"""Unit testovi za jarvis/media/ytm_web.py.

Lažiramo Playwright: hvatamo ``page.evaluate`` i vraćamo unapred
definisane rezultate. Ne pokrećemo pravi Chrome.
"""

from __future__ import annotations

import sys
import types
import urllib.parse

import pytest

from jarvis.media import ytm_web


class FakePage:
    def __init__(
        self,
        *,
        closed: bool = False,
        url: str = "https://music.youtube.com/",
        transition_changes_track: bool = True,
        previous_native_restart: bool = False,
        previous_has_item: bool = True,
        current_time: float = 0.0,
        authenticated: bool = True,
        login_required: bool | None = None,
        surface_ready: bool = True,
        account_present: bool = False,
        playable_href: str = "/watch?v=test-track",
        search_result_shape: str = "href",
        component_candidates: list[dict[str, object]] | None = None,
        playback_starts: bool = True,
        identity_available: bool = True,
        transport_control_shape: str = "player_bar",
        volume_available: bool = True,
    ) -> None:
        self.closed = closed
        self.url = url
        self.transition_changes_track = transition_changes_track
        self.previous_native_restart = previous_native_restart
        self.previous_has_item = previous_has_item
        self.authenticated = authenticated
        self.login_required = not authenticated if login_required is None else login_required
        self.surface_ready = surface_ready
        self.account_present = account_present
        self.playable_href = playable_href
        self.search_result_shape = search_result_shape
        self.component_candidates = component_candidates or []
        self.playback_starts = playback_starts
        self.identity_available = identity_available
        self.transport_control_shape = transport_control_shape
        self.volume_available = volume_available
        self.last_query = ""
        self.bring_to_front_calls = 0
        self.wait_for_selector_calls: list[str] = []
        self.transport_commands: list[str] = []
        self.volume_commands: list[str] = []
        self.state_reads = 0
        self.state = {
            "ok": True,
            "playing": False,
            "player_loaded": True,
            "title": "Test Song",
            "artist": "Test Artist",
            "track_id": "test-track",
            "ariaLabel": "Play",
            "currentTime": current_time,
            "duration": 200,
            "volume": 0.5,
            "muted": False,
        }

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str, arg: object = None):  # noqa: ANN001
        if self.state.get("error"):
            return {"ok": False, "error": self.state["error"], "playing": None, "title": "", "artist": ""}
        if "pageReady" in script:
            page_ready = "music.youtube.com" in self.url
            surface_ready = page_ready and self.surface_ready
            authenticated = surface_ready and self.authenticated and not self.login_required
            return {
                "ok": True,
                "origin": "https://music.youtube.com" if page_ready else "https://accounts.google.com",
                "page_ready": page_ready,
                "search_ready": page_ready and self.surface_ready,
                "ytm_surface_ready": surface_ready,
                "authenticated": authenticated,
                "login_required": self.login_required,
                "auth_evidence": (
                    "login_required"
                    if self.login_required
                    else "usable_surface"
                    if authenticated
                    else "unknown"
                ),
                "has_ytm_app": surface_ready,
                "has_nav": surface_ready,
                "has_search": surface_ready,
                "has_account": self.account_present,
                "has_explicit_login": self.login_required,
                "player_loaded": self.state.get("player_loaded", True),
                "playing": self.state.get("playing"),
                "track_id": self.state.get("track_id", ""),
            }
        if "searchSurfaceState" in script:
            return {
                "path": "/search" if self.last_query else "/",
                "query": self.last_query,
                "surface_ready": self.surface_ready,
                "rows_ready": self.surface_ready,
                "ready": self.surface_ready,
            }
        if "findPlayableSearchResult" in script:
            if self.search_result_shape == "component":
                candidate = next(
                    (
                        item
                        for item in self.component_candidates
                        if item.get("video_id") and item.get("watch_endpoint")
                    ),
                    None,
                )
                if candidate is None:
                    return {
                        "ok": False,
                        "clicked": False,
                        "error_code": "NO_PLAYABLE_SEARCH_RESULT",
                        "error": "no playable YT Music search result",
                    }
                clicked = bool(isinstance(arg, dict) and arg.get("click"))
                if clicked:
                    self.state.update(
                        {
                            "playing": self.playback_starts,
                            "player_loaded": True,
                            "track_id": candidate.get("video_id", ""),
                            "title": candidate.get("title", ""),
                            "artist": candidate.get("artist", ""),
                        }
                    )
                return {
                    "ok": True,
                    "clicked": clicked,
                    "selected_video_id": candidate.get("video_id", ""),
                    "selected_title": candidate.get("title", ""),
                    "selected_artist": candidate.get("artist", ""),
                    "selection_method": candidate.get("selection_method", "watch_endpoint_anchor"),
                    "component": candidate.get("component", "ytmusic-responsive-list-item-renderer"),
                }
            video_id = ytm_web._video_id_from_href(self.playable_href)
            if not video_id:
                return {
                    "ok": False,
                    "clicked": False,
                    "error_code": "NO_PLAYABLE_SEARCH_RESULT",
                    "error": "no playable YT Music search result",
                }
            clicked = bool(isinstance(arg, dict) and arg.get("click"))
            if clicked:
                self.state.update(
                    {
                        "playing": self.playback_starts,
                        "player_loaded": True,
                        "track_id": video_id,
                        "title": "Selected Result",
                        "artist": "Selected Artist",
                    }
                )
            return {
                "ok": True,
                "clicked": clicked,
                "selected_video_id": video_id,
                "selected_title": "Selected Result",
                "selected_artist": "Selected Artist",
                "selection_method": "watch_endpoint_anchor",
                "component": "ytmusic-responsive-list-item-renderer",
            }
        if "ytmPlayerState" in script:
            self.state_reads += 1
            state = dict(self.state)
            if not self.identity_available:
                state.update({"title": "", "artist": "", "track_id": ""})
            return state
        if "ytmMediaVolumeState" in script:
            if not self.volume_available:
                return {"ok": False, "volume": None, "muted": None, "error": "no video element"}
            return {
                "ok": True,
                "volume": self.state["volume"],
                "muted": self.state["muted"],
            }
        if "ytmMediaVolumeControl" in script:
            request = arg if isinstance(arg, dict) else {}
            action = str(request.get("action", ""))
            amount = request.get("amount")
            level = request.get("level")
            self.volume_commands.append(action)
            if not self.volume_available:
                return {"ok": False, "error": "no video element"}
            before = {
                "volume": self.state["volume"],
                "muted": self.state["muted"],
            }
            if action == "volume_up":
                self.state["volume"] = min(1.0, self.state["volume"] + int(amount or 0) / 100)
            elif action == "volume_down":
                self.state["volume"] = max(0.0, self.state["volume"] - int(amount or 0) / 100)
            elif action == "volume_set":
                self.state["volume"] = min(1.0, max(0.0, int(level) / 100))
            elif action == "volume_mute":
                self.state["muted"] = not self.state["muted"]
            else:
                return {"ok": False, "error": f"unknown volume action: {action}"}
            return {"ok": True, "method": "html_media_element", "before": before, "requested": action}
        if "video.play" in script:
            action = arg
            self.transport_commands.append(str(action))
            if action in ("next", "previous") and (
                self.transport_control_shape != "player_bar"
                or "ytmusic-player-bar" not in script
                or f"{action}-button" not in script
            ):
                return {"ok": False, "error": f"missing player-bar {action} control"}
            if action == "play":
                self.state["playing"] = True
            elif action == "pause":
                self.state["playing"] = False
            elif action in ("next", "previous"):
                self.state["playing"] = True
                command_number = self.transport_commands.count(str(action))
                native_restart = (
                    action == "previous"
                    and self.previous_native_restart
                    and command_number == 1
                    and self.state["currentTime"] > 3.0
                )
                if native_restart:
                    self.state["currentTime"] = 0.8
                elif self.transition_changes_track and (action != "previous" or self.previous_has_item):
                    self.state["title"] = f"After {action}"
                    self.state["track_id"] = f"{action}-track"
                    self.state["currentTime"] = 0.0
            return {"ok": True, "method": f"fake.{action}"}
        if "HTMLInputElement" in script:
            self.last_query = str(arg or "")
            self.url = f"https://music.youtube.com/search?q={urllib.parse.quote_plus(self.last_query)}"
            return {"ok": True}
        if "hasSearch" in script:
            return {"url": "https://music.youtube.com/", "hasSearch": True}
        return {"ok": True}

    async def goto(self, url: str, **kwargs) -> None:  # noqa: ANN003
        self.url = url
        self.last_query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]

    async def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1

    async def wait_for_function(self, expr: str, arg: object = None, **kwargs) -> None:  # noqa: ANN003
        return None

    async def wait_for_selector(self, selector: str, **kwargs) -> None:  # noqa: ANN003
        self.wait_for_selector_calls.append(selector)
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


class FakeCdpSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.calls: list[tuple[str, dict | None]] = []
        self.window_state = "normal"
        self.detached = False

    async def send(self, command: str, params: dict | None = None):
        self.calls.append((command, params))
        if command == "Target.getTargets":
            return {
                "targetInfos": [
                    {
                        "targetId": "normal-target",
                        "type": "page",
                        "url": "https://example.com/",
                    },
                    {
                        "targetId": "ytm-target",
                        "type": "page",
                        "url": self.page.url,
                    },
                ]
            }
        if command == "Browser.getWindowForTarget":
            return {"windowId": 42, "bounds": {"windowState": self.window_state}}
        if command == "Browser.setWindowBounds":
            self.window_state = params["bounds"]["windowState"]
            return {}
        raise AssertionError(f"unexpected CDP command: {command}")

    async def detach(self) -> None:
        self.detached = True


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.cdp = FakeCdpSession(page)

    def is_connected(self) -> bool:
        return True

    async def new_browser_cdp_session(self) -> FakeCdpSession:
        return self.cdp


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
    profile = ytm_web.Path("/tmp/jarvis_ytm_test_profile")
    (profile / ".connected").unlink(missing_ok=True)
    monkeypatch.setattr(ytm_web, "_JARVIS_PROFILE_DIR", profile)


def _set_connected_runtime(monkeypatch, page: FakePage, *, pages: list[FakePage] | None = None) -> None:
    monkeypatch.setattr(ytm_web, "_ytm_page", page)
    context = object() if pages is None else types.SimpleNamespace(pages=pages)
    monkeypatch.setattr(ytm_web, "_context", context)
    monkeypatch.setattr(ytm_web, "_launched", True)
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.CONNECTED)
    monkeypatch.setattr(ytm_web, "_page_ready", True)
    monkeypatch.setattr(ytm_web, "_search_ready", True)
    monkeypatch.setattr(ytm_web, "_browser", types.SimpleNamespace(is_connected=lambda: True))


async def test_enforce_background_window_minimizes_only_dedicated_ytm_target(monkeypatch) -> None:
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    browser = FakeBrowser(page)
    monkeypatch.setattr(ytm_web, "_browser", browser)

    result = await ytm_web._enforce_background_window()

    assert result["ok"] is True
    assert result["target_id"] == "ytm-target"
    assert result["window_id"] == 42
    assert result["window_state"] == "minimized"
    assert result["changed"] is True
    assert browser.cdp.window_state == "minimized"
    assert (
        "Browser.setWindowBounds",
        {"windowId": 42, "bounds": {"windowState": "minimized"}},
    ) in browser.cdp.calls
    assert browser.cdp.detached is True


async def test_ensure_ready_reasserts_background_window_without_presenting_page(monkeypatch) -> None:
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)
    browser = FakeBrowser(page)
    monkeypatch.setattr(ytm_web, "_browser", browser)

    assert await ytm_web.ensure_ready() is True

    assert browser.cdp.window_state == "minimized"
    assert page.bring_to_front_calls == 0


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
    assert "--start-minimized" not in launch.calls[0]["args"]
    assert ytm_web._connection_marker_path().is_file()


async def test_connect_reuses_login_page_and_presents_it_without_duplicate_launch(
    monkeypatch,
) -> None:
    fake_pw = _install_fake_playwright(monkeypatch)
    page = FakePage(authenticated=False)
    _set_connected_runtime(monkeypatch, page)
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.NEEDS_LOGIN)

    status = await ytm_web.connect()

    assert status["state"] == ytm_web.NEEDS_LOGIN
    assert status["page_ready"] is True
    assert status["search_ready"] is True
    assert page.bring_to_front_calls == 1
    assert fake_pw.chromium.launch_persistent_context.calls == []
    assert not ytm_web._connection_marker_path().exists()


async def test_connect_does_not_navigate_away_from_google_login_page(monkeypatch) -> None:
    fake_pw = _install_fake_playwright(monkeypatch)
    page = FakePage(url="https://accounts.google.com/ServiceLogin", authenticated=False)
    _set_connected_runtime(monkeypatch, page)
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.NEEDS_LOGIN)

    status = await ytm_web.connect()

    assert status["state"] == ytm_web.NEEDS_LOGIN
    assert page.url == "https://accounts.google.com/ServiceLogin"
    assert page.bring_to_front_calls == 1
    assert fake_pw.chromium.launch_persistent_context.calls == []


async def test_warm_up_does_not_launch_profile_without_connection_marker(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    ytm_web.warm_up()

    assert ytm_web._warmup_task is None
    assert ytm_web._launched is False


async def test_warm_up_restores_saved_profile_minimized(monkeypatch) -> None:
    fake_pw = _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ytm_web._connection_marker_path().touch()

    ytm_web.warm_up()
    assert ytm_web._warmup_task is not None
    await ytm_web._warmup_task

    assert "--start-minimized" in fake_pw.chromium.launch_persistent_context.calls[0]["args"]


async def test_ensure_ready_does_not_restore_unconnected_profile(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    assert await ytm_web.ensure_ready() is False
    assert ytm_web._launched is False


async def test_login_required_state_is_distinct_from_disconnected(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    _set_connected_runtime(monkeypatch, FakePage(authenticated=False))

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.NEEDS_LOGIN
    assert status["connected"] is False
    assert status["needs_login"] is True
    assert status["page_ready"] is True
    assert status["search_ready"] is True


async def test_status_get_detects_login_completion_without_connect_call(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(authenticated=False)
    _set_connected_runtime(monkeypatch, page)

    before = await ytm_web.connection_status()
    page.authenticated = True
    page.login_required = False
    after = await ytm_web.connection_status()

    assert before["state"] == ytm_web.NEEDS_LOGIN
    assert after["state"] == ytm_web.CONNECTED
    assert after["connected"] is True
    assert ytm_web._connection_marker_path().is_file()


async def test_connected_ytm_surface_does_not_require_avatar_selector(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(authenticated=True, account_present=False)
    _set_connected_runtime(monkeypatch, page)

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.CONNECTED
    assert status["connected"] is True


async def test_status_adopts_authenticated_ytm_page_after_login_opens_second_tab(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    login_page = FakePage(url="https://accounts.google.com/ServiceLogin", authenticated=False)
    ytm_page = FakePage(authenticated=True)
    _set_connected_runtime(monkeypatch, login_page, pages=[login_page, ytm_page])
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.NEEDS_LOGIN)

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.CONNECTED
    assert ytm_web._ytm_page is ytm_page


async def test_status_adopts_authenticated_second_ytm_page_over_stale_original(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    stale_page = FakePage(authenticated=False)
    authenticated_page = FakePage(authenticated=True)
    _set_connected_runtime(monkeypatch, stale_page, pages=[stale_page, authenticated_page])
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.NEEDS_LOGIN)

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.CONNECTED
    assert ytm_web._ytm_page is authenticated_page


async def test_closed_tracked_page_is_replaced_by_live_ytm_page(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    closed_page = FakePage(closed=True)
    live_page = FakePage(authenticated=True)
    _set_connected_runtime(monkeypatch, closed_page, pages=[closed_page, live_page])
    monkeypatch.setattr(ytm_web, "_connection_state", ytm_web.NEEDS_LOGIN)

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.CONNECTED
    assert ytm_web._ytm_page is live_page


async def test_unverified_ytm_surface_is_not_reported_as_login_or_connected(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(authenticated=False, login_required=False)
    _set_connected_runtime(monkeypatch, page)

    status = await ytm_web.connection_status()

    assert status["state"] == ytm_web.ERROR
    assert status["connected"] is False
    assert status["needs_login"] is False
    assert not ytm_web._connection_marker_path().exists()


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
    page.login_required = True
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
    assert page.transport_commands == ["next"]
    assert page.bring_to_front_calls == 0


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
    assert page.transport_commands == ["next"]


async def test_control_previous_verifies_track_transition(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("previous")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["delivered"] is True
    assert result["track_changed"] is True
    assert result["after"]["track_id"] == "previous-track"
    assert page.transport_commands == ["previous"]


async def test_control_previous_near_track_start_uses_one_click(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(previous_native_restart=True, current_time=1.0)
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("previous")

    assert result["ok"] is True
    assert result["click_count"] == 1
    assert result["native_restart_detected"] is False
    assert result["intermediate"] is None
    assert result["after"]["track_id"] == "previous-track"
    assert page.transport_commands == ["previous"]


async def test_control_previous_restarts_current_then_deliberately_clicks_once_more(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(previous_native_restart=True, current_time=30.0)
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("previous")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["click_count"] == 2
    assert result["native_restart_detected"] is True
    assert result["native_result"] == "restarted_current"
    assert result["intermediate"]["track_id"] == "test-track"
    assert result["intermediate"]["currentTime"] == 0.8
    assert result["after"]["track_id"] == "previous-track"
    assert result["track_changed"] is True
    assert page.transport_commands == ["previous", "previous"]


async def test_control_previous_restart_without_previous_item_stops_at_two_clicks(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(previous_native_restart=True, previous_has_item=False, current_time=30.0)
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control("previous")

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["error_code"] == "NO_PREVIOUS_TRACK"
    assert result["native_result"] == "no_previous_track"
    assert result["click_count"] == 2
    assert result["track_changed"] is False
    assert page.transport_commands == ["previous", "previous"]


@pytest.mark.parametrize("action", ["next", "previous"])
async def test_control_non_idempotent_action_is_sent_once_when_identity_unavailable(
    monkeypatch,
    action: str,
) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(identity_available=False)
    page.state["playing"] = True
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control(action)

    assert result["ok"] is False
    assert result["delivered"] is True
    assert result["verified"] is False
    assert result["verification"] == "unavailable"
    assert result["degraded"] is True
    assert page.transport_commands == [action]
    assert page.state_reads <= 5


@pytest.mark.parametrize(
    ("action", "start_volume", "expected_volume"),
    [("volume_up", 0.95, 1.0), ("volume_down", 0.05, 0.0)],
)
async def test_control_volume_clamps_and_verifies_media_element(
    monkeypatch,
    action: str,
    start_volume: float,
    expected_volume: float,
) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["volume"] = start_volume
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume(action)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["adapter"] == "ytm_web"
    assert result["before"]["volume"] == start_volume
    assert result["after"]["volume"] == pytest.approx(expected_volume)
    assert page.volume_commands == [action]


async def test_control_volume_mute_toggles_and_verifies_media_element(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume("volume_mute")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["before"]["muted"] is False
    assert result["after"]["muted"] is True
    assert page.volume_commands == ["volume_mute"]


@pytest.mark.parametrize("level", [0, 30, 75, 100])
async def test_control_volume_set_reads_back_requested_level(monkeypatch, level: int) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume("volume_set", level=level)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["action"] == "volume_set"
    assert result["requested_level"] == level
    assert result["level"] == level
    assert result["volume"] == level / 100
    assert page.volume_commands == ["volume_set"]
    assert page.bring_to_front_calls == 0


@pytest.mark.parametrize(
    ("action", "amount", "start_volume", "expected_volume"),
    [
        ("volume_up", 25, 0.5, 0.75),
        ("volume_down", 40, 0.5, 0.1),
        ("volume_up", None, 0.5, 0.6),
    ],
)
async def test_control_volume_relative_amount_is_one_verified_media_action(
    monkeypatch,
    action: str,
    amount: int | None,
    start_volume: float,
    expected_volume: float,
) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state["volume"] = start_volume
    _set_connected_runtime(monkeypatch, page)

    kwargs = {} if amount is None else {"amount": amount}
    result = await ytm_web.control_volume(action, **kwargs)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["amount"] == (10 if amount is None else amount)
    assert result["after"]["volume"] == pytest.approx(expected_volume)
    assert page.volume_commands == [action]


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("volume_set", {"level": -1}),
        ("volume_set", {"level": 101}),
        ("volume_set", {"level": 30.5}),
        ("volume_up", {"amount": 0}),
        ("volume_down", {"amount": 101}),
        ("volume_up", {"amount": True}),
    ],
)
async def test_control_volume_rejects_invalid_percentage_without_dom_action(
    monkeypatch,
    action: str,
    kwargs: dict[str, object],
) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume(action, **kwargs)

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert result["delivered"] is False
    assert page.volume_commands == []


async def test_control_volume_without_media_element_is_explicit_failure(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(volume_available=False)
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume("volume_up")

    assert result["ok"] is False
    assert result["delivered"] is False
    assert result["verified"] is False
    assert result["verification"] == "not_attempted"
    assert page.volume_commands == []


async def test_control_volume_without_loaded_track_does_not_touch_background_video(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage()
    page.state.update({"player_loaded": False, "playing": None, "title": "", "artist": "", "track_id": ""})
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.control_volume("volume_set", level=30)

    assert result["ok"] is False
    assert result["error_code"] == "PLAYER_NOT_LOADED"
    assert result["delivered"] is False
    assert page.volume_commands == []


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
    assert page.bring_to_front_calls == 0


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


async def test_play_query_selects_component_song_and_skips_artist_album_navigation(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(
        search_result_shape="component",
        component_candidates=[
            {"title": "Relja artist", "browse_id": "artist-1", "artist": "Relja"},
            {"title": "Album navigation", "browse_id": "album-1", "artist": "Relja"},
            {
                "title": "Top Gun",
                "artist": "Relja",
                "video_id": "component-track",
                "watch_endpoint": True,
                "component": "ytmusic-two-row-item-renderer",
            },
        ],
    )
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.play_query("Relja Popović")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["selected_video_id"] == "component-track"
    assert result["selected_title"] == "Top Gun"
    assert result["selected_artist"] == "Relja"
    assert result["selection_method"] == "watch_endpoint_anchor"
    assert result["actual_video_id"] == "component-track"
    assert result["error"] is None


async def test_play_query_no_component_candidate_is_structured_connected_failure(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(
        search_result_shape="component",
        component_candidates=[
            {"title": "Artist navigation", "browse_id": "artist-1"},
            {"title": "Album navigation", "browse_id": "album-1"},
        ],
    )
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.play_query("Unknown artist")

    assert result["ok"] is False
    assert result["connection_state"] == ytm_web.CONNECTED
    assert result["stage"] == "result_selection"
    assert result["search_submitted"] is True
    assert result["result_found"] is False
    assert result["delivered"] is False
    assert result["verified"] is False
    assert result["error_code"] == "NO_PLAYABLE_SEARCH_RESULT"
    assert "timeout" not in result["error"].lower()
    assert page.wait_for_selector_calls == []


async def test_second_component_play_query_changes_track_in_same_session(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(
        search_result_shape="component",
        component_candidates=[
            {"title": "Relja song", "artist": "Relja", "video_id": "relja-track", "watch_endpoint": True},
        ],
    )
    _set_connected_runtime(monkeypatch, page)

    first = await ytm_web.play_query("Relja Popović")
    page.component_candidates = [
        {
            "title": "Vlado song",
            "artist": "Vlado Georgiev",
            "video_id": "vlado-track",
            "watch_endpoint": True,
        },
    ]
    second = await ytm_web.play_query("Vlado Georgiev")

    assert first["ok"] is True
    assert first["actual_video_id"] == "relja-track"
    assert second["ok"] is True
    assert second["actual_video_id"] == "vlado-track"
    assert second["before"]["track_id"] == "relja-track"


async def test_play_query_connected_playback_failure_is_not_reported_as_login(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    page = FakePage(
        search_result_shape="component",
        playback_starts=False,
        component_candidates=[
            {"title": "Song", "artist": "Artist", "video_id": "song-track", "watch_endpoint": True},
        ],
    )
    _set_connected_runtime(monkeypatch, page)

    result = await ytm_web.play_query("Song Artist")

    assert result["ok"] is False
    assert result["connection_state"] == ytm_web.CONNECTED
    assert result["delivered"] is True
    assert result["verified"] is False
    assert result["error_code"] == "PLAYBACK_DID_NOT_START"


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
    ytm_web._connection_marker_path().touch()
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
    ytm_web._connection_marker_path().touch()
    await ytm_web.ensure_ready()
    first = ytm_web._context
    await ytm_web.ensure_ready()
    assert ytm_web._context is first


async def test_warm_up_runs_in_background(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ytm_web._connection_marker_path().touch()
    ytm_web.warm_up()
    assert ytm_web._warmup_task is not None
    await ytm_web._warmup_task
    assert ytm_web._launched is True


async def test_shutdown_clears_state(monkeypatch) -> None:
    _install_fake_playwright(monkeypatch)
    ytm_web._JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ytm_web._connection_marker_path().touch()
    await ytm_web.ensure_ready()
    await ytm_web.shutdown()
    assert ytm_web._ytm_page is None
    assert ytm_web._context is None
    assert ytm_web._launched is False
