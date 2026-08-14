"""Jedan headed, persistent Playwright browser za YTM kontrolu.

Dizajn: browser se digne JEDNOM (na startup-u, pozivom ``warm_up()`` ili
``ensure_ready()``), drži jedan tab na music.youtube.com i živi ceo
život procesa. State read transport NE blokira event loop — sve je
već učitano.

NE pozivati ``launch_persistent_context`` na svaki tool poziv. To je
razlog zašto je sve bilo sporo: svaki chat je triggerovao 20s timeout
na učitavanje player bara. Page/search readiness je odvojena od učitanog
playera, pa prvi play može krenuti sa prazne početne stranice.

Kako se koristi:
- App startup:   ``ytm_web.warm_up()`` (neblokirajuće, zakazuje restore task)
- Tool poziv:    ``await ytm_web.ensure_ready()`` (brz ako je već warm)
- State read:    ``await ytm_web.get_state()`` (čita sa postojećeg taba)
- Search:        ``await ytm_web.play_query(query)``
- Transport:     DOM kontrole na istoj YT Music stranici, sa state verifikacijom
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("jarvis.ytm_web")

YTM_URL = "https://music.youtube.com"

_JARVIS_PROFILE_DIR = Path.home() / ".jarvis" / "ytm_profile"

ConnectionState = Literal["DISCONNECTED", "NEEDS_LOGIN", "CONNECTING", "CONNECTED", "ERROR"]

DISCONNECTED: ConnectionState = "DISCONNECTED"
NEEDS_LOGIN: ConnectionState = "NEEDS_LOGIN"
CONNECTING: ConnectionState = "CONNECTING"
CONNECTED: ConnectionState = "CONNECTED"
ERROR: ConnectionState = "ERROR"

_pw = None
_browser = None
_context = None
_ytm_page = None
_launched: bool = False
_active_profile: str | None = None
_lock: asyncio.Lock | None = None
_warmup_task: asyncio.Task | None = None
_connection_state: ConnectionState = DISCONNECTED
_connection_error: str | None = None
_page_ready: bool = False
_search_ready: bool = False
_player_loaded: bool = False
_playing: bool | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _safe_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _safe_is_connected(browser: Any) -> bool:
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


def _runtime_alive() -> bool:
    """Return whether the dedicated Playwright runtime still has a live tab."""
    if not _launched or _context is None or _ytm_page is None:
        return False
    if _safe_is_closed(_ytm_page):
        return False
    # Persistent contexts may not expose a Browser object. The page/context
    # itself is still a valid liveness signal in that case.
    return _browser is None or _safe_is_connected(_browser)


def _profile_exists() -> bool:
    return _JARVIS_PROFILE_DIR.is_dir()


def _clear_runtime_state() -> None:
    global _page_ready, _search_ready, _player_loaded, _playing
    _page_ready = False
    _search_ready = False
    _player_loaded = False
    _playing = None


def _status_payload() -> dict[str, Any]:
    return {
        "state": _connection_state,
        "connected": _connection_state == CONNECTED,
        "needs_login": _connection_state == NEEDS_LOGIN,
        "page_ready": _page_ready,
        "search_ready": _search_ready,
        "player_loaded": _player_loaded,
        "playing": _playing,
        "error": _connection_error,
    }


def _resolve_profile() -> tuple[Path, list[str]]:
    """Return the dedicated per-device browser profile and safe launch args."""
    _JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return _JARVIS_PROFILE_DIR, ["--no-first-run", "--disable-blink-features=AutomationControlled"]


async def _launch_browser() -> bool:
    """Launch the one headed, persistent browser used by all YTM actions."""
    global _pw, _context, _ytm_page, _launched, _active_profile, _browser
    if _runtime_alive():
        return True

    user_data_dir, args = _resolve_profile()
    log.info("ytm_web: launching persistent browser profile=%s", user_data_dir)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("ytm_web: playwright not installed")
        return False

    if _pw is None:
        try:
            _pw = await async_playwright().start()
        except Exception as exc:
            log.warning("ytm_web: playwright.start failed: %s", exc)
            return False

    for use_channel in (True, False):
        try:
            kwargs: dict[str, Any] = {
                "user_data_dir": str(user_data_dir),
                "headless": False,
                "args": args,
                "timeout": 30000,
            }
            if use_channel:
                kwargs["channel"] = "chrome"
            _context = await _pw.chromium.launch_persistent_context(**kwargs)
            break
        except Exception as exc:
            log.warning("ytm_web: launch failed (channel=%s): %s", use_channel, exc)
    if _context is None:
        if user_data_dir != _JARVIS_PROFILE_DIR:
            _JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                _context = await _pw.chromium.launch_persistent_context(
                    user_data_dir=str(_JARVIS_PROFILE_DIR),
                    headless=False,
                    args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
                    timeout=30000,
                )
            except Exception as exc:
                log.warning("ytm_web: fallback also failed: %s", exc)
                return False
        else:
            return False

    _active_profile = str(user_data_dir)
    _launched = True
    _browser = _context.browser if hasattr(_context, "browser") else None

    pages = list(_context.pages or [])
    ytm_pages = [page for page in pages if "music.youtube.com" in (getattr(page, "url", "") or "")]
    if ytm_pages:
        _ytm_page = ytm_pages[0]
    elif pages:
        _ytm_page = pages[0]
    else:
        try:
            _ytm_page = await _context.new_page()
        except Exception as exc:
            log.warning("ytm_web: new_page failed: %s", exc)
            return False

    return True


_PAGE_PROBE_JS = """
() => {
  const pageReady = location.hostname === 'music.youtube.com';
  const search = document.querySelector('ytmusic-search-box input')
    || document.querySelector('input#input')
    || document.querySelector('input[name="search_query"]');
  const account = document.querySelector('#avatar-btn, ytmusic-nav-bar #avatar-btn, ytmusic-nav-bar [aria-label*="Account" i], ytmusic-nav-bar a[href*="/channel/"]');
  const signIn = document.querySelector('a[href*="ServiceLogin"], #sign-in-button, tp-yt-paper-button[aria-label*="Sign in" i], ytmusic-pivot-bar-item-renderer[tab-id="SIGN_IN"]');
  const signInText = Array.from(document.querySelectorAll('button, a, tp-yt-paper-button'))
    .some((el) => /^(sign in|log in|prijavi se)$/i.test(
      (el.getAttribute('aria-label') || el.textContent || '').trim()
    ));
  const video = document.querySelector('video');
  const title = document.querySelector('.title.ytmusic-player-bar');
  const playerLoaded = !!(video && title && title.textContent && title.textContent.trim().length > 0);
  const trackId = new URL(location.href).searchParams.get('v') || '';
  return {
    ok: true,
    page_ready: pageReady,
    search_ready: pageReady && !!search,
    authenticated: !!account && !signIn && !signInText,
    login_required: !account || !!signIn || signInText,
    player_loaded: playerLoaded,
    playing: playerLoaded ? (!video.paused && video.currentTime > 0 && video.readyState >= 2) : null,
    track_id: trackId,
  };
}
"""


def _apply_probe(probe: dict[str, Any] | None) -> None:
    global _page_ready, _search_ready, _player_loaded, _playing
    if not isinstance(probe, dict) or probe.get("ok") is not True:
        _clear_runtime_state()
        return
    _page_ready = bool(probe.get("page_ready"))
    _search_ready = bool(probe.get("search_ready"))
    _player_loaded = bool(probe.get("player_loaded"))
    playing = probe.get("playing")
    _playing = playing if isinstance(playing, bool) else None


async def _probe_page() -> dict[str, Any] | None:
    if not _runtime_alive():
        _clear_runtime_state()
        return None
    try:
        probe = await _ytm_page.evaluate(_PAGE_PROBE_JS)
    except Exception as exc:
        log.debug("ytm_web: page probe failed: %s", exc)
        _clear_runtime_state()
        return None
    if not isinstance(probe, dict):
        _clear_runtime_state()
        return None
    _apply_probe(probe)
    return probe


def _set_connection_from_probe(probe: dict[str, Any] | None) -> None:
    global _connection_state, _connection_error
    if probe is None:
        _connection_state = ERROR
        _connection_error = "YT Music browser page is unavailable"
        return
    if probe.get("authenticated") and probe.get("page_ready") and probe.get("search_ready"):
        _connection_state = CONNECTED
        _connection_error = None
        return
    if probe.get("login_required") or not probe.get("authenticated"):
        _connection_state = NEEDS_LOGIN
        _connection_error = None
        return
    _connection_state = ERROR
    _connection_error = "YT Music page/search is not ready"


async def _refresh_connection_status() -> dict[str, Any]:
    global _connection_state, _connection_error
    if not _runtime_alive():
        _clear_runtime_state()
        if _connection_state not in (DISCONNECTED, CONNECTING):
            _connection_state = ERROR
            _connection_error = "YT Music browser session is no longer running"
        return _status_payload()
    probe = await _probe_page()
    _set_connection_from_probe(probe)
    return _status_payload()


async def _navigate_to_ytm() -> bool:
    if not _runtime_alive():
        return False
    try:
        await _ytm_page.goto(YTM_URL, wait_until="domcontentloaded", timeout=15000)
        # Give the SPA a short opportunity to render. Login itself remains a
        # user action in the headed browser and is never automated here.
        await asyncio.sleep(0.5)
        await _refresh_connection_status()
        return True
    except Exception as exc:
        global _connection_state, _connection_error
        _connection_state = ERROR
        _connection_error = "YT Music page could not be opened"
        log.warning("ytm_web: navigate failed: %s", exc)
        return False


async def connect() -> dict[str, Any]:
    """Open the dedicated headed profile for a user-driven Google login."""
    global _connection_state, _connection_error
    async with _get_lock():
        if _connection_state == CONNECTED and _runtime_alive():
            return await _refresh_connection_status()
        _connection_state = CONNECTING
        _connection_error = None
        if not await _launch_browser():
            _connection_state = ERROR
            _connection_error = "Could not launch the dedicated YT Music browser"
            return _status_payload()
        await _navigate_to_ytm()
        return await _refresh_connection_status()


async def _restore_existing_connection() -> None:
    global _connection_state, _connection_error
    if not _profile_exists():
        return
    _connection_state = CONNECTING
    _connection_error = None
    try:
        if await _launch_browser():
            await _navigate_to_ytm()
        else:
            _connection_state = ERROR
            _connection_error = "Could not launch the saved YT Music browser profile"
    except Exception as exc:
        _connection_state = ERROR
        _connection_error = "Saved YT Music browser profile could not be restored"
        log.warning("ytm_web: restore failed: %s", exc)


def warm_up() -> None:
    """Restore a previously connected profile without creating a new one."""
    global _warmup_task
    if not _profile_exists():
        return
    if _warmup_task is not None and not _warmup_task.done():
        return
    _warmup_task = asyncio.create_task(_restore_existing_connection())


async def connection_status() -> dict[str, Any]:
    """Return safe connection and runtime/player state from the YTM page."""
    if _runtime_alive() and _connection_state != CONNECTING:
        await _refresh_connection_status()
    return _status_payload()


async def ensure_ready() -> bool:
    """Ensure authenticated page/search readiness, not player readiness."""
    global _connection_state, _connection_error
    if is_available():
        await _refresh_connection_status()
        return is_available()
    if not _launched:
        if not _profile_exists():
            return False
        _connection_state = CONNECTING
        if not await _launch_browser():
            _connection_state = ERROR
            _connection_error = "Could not restore the YT Music browser"
            return False
        await _navigate_to_ytm()
    else:
        await _refresh_connection_status()
    return is_available()


def is_available() -> bool:
    """Return whether the authenticated YTM page can accept search actions."""
    return _connection_state == CONNECTED and _search_ready and _runtime_alive()


_STATE_JS = """
() => {
  const video = document.querySelector('video');
  const playPause = document.querySelector('#play-pause-button');
  const titleEl = document.querySelector('.title.ytmusic-player-bar');
  const bylineEl = document.querySelector('.byline.ytmusic-player-bar');
  const title = (titleEl && titleEl.textContent ? titleEl.textContent.trim() : '');
  const artist = (bylineEl && bylineEl.textContent ? bylineEl.textContent.trim() : '');
  const playerLoaded = !!(video && title);
  const ariaLabel = playPause ? (playPause.getAttribute('aria-label') || '') : '';
  const isPlaying = playerLoaded && !video.paused && video.currentTime > 0 && video.readyState >= 2;
  const trackId = new URL(location.href).searchParams.get('v') || '';
  return {
    ok: true,
    playing: playerLoaded ? isPlaying : null,
    player_loaded: playerLoaded,
    title,
    artist,
    track_id: trackId,
    ariaLabel,
    currentTime: video ? video.currentTime : 0,
    duration: video && isFinite(video.duration) ? video.duration : 0,
    url: location.href,
  };
}
"""

_CONTROL_JS = """
async (action) => {
  if (action === 'play' || action === 'pause') {
    const video = document.querySelector('video');
    if (!video) return { ok: false, error: 'no video element' };
    if (action === 'play') {
      try { await video.play(); } catch (e) { return { ok: false, error: e.message }; }
      return { ok: true, method: 'video.play' };
    }
    video.pause();
    return { ok: true, method: 'video.pause' };
  }
  if (action === 'next') {
    const btn = document.querySelector('#next-button');
    if (!btn) return { ok: false, error: 'no next button' };
    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') {
      return { ok: false, error: 'next button disabled' };
    }
    btn.click();
    return { ok: true, method: 'click #next-button' };
  }
  if (action === 'previous') {
    const btn = document.querySelector('#previous-button');
    if (!btn) return { ok: false, error: 'no previous button' };
    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') {
      return { ok: false, error: 'previous button disabled' };
    }
    btn.click();
    return { ok: true, method: 'click #previous-button' };
  }
  return { ok: false, error: 'unknown action: ' + action };
}
"""

_SEARCH_INPUT_JS = """
async (query) => {
  let box = document.querySelector('ytmusic-search-box input')
            || document.querySelector('input#input')
            || document.querySelector('input[name="search_query"]');
  if (!box) return { ok: false, error: 'no search box' };
  box.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(box, query);
  box.dispatchEvent(new Event('input', { bubbles: true }));
  return { ok: true };
}
"""

_PLAYABLE_RESULT_SELECTORS = (
    "ytmusic-responsive-list-item-renderer a[href*='/watch?v=']",
    "ytmusic-card-shelf-renderer a[href*='/watch?v=']",
    "ytmusic-shelf-renderer a[href*='/watch?v=']",
    "a[href*='/watch?v=']",
)


def _video_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("v", [None])[0]
    except (TypeError, ValueError):
        return None


async def get_state() -> dict[str, Any]:
    """Read YTM DOM state; a connected page may legitimately have no track."""
    if not await ensure_ready():
        status = _status_payload()
        return {
            "ok": False,
            "error": f"YT Music is {status['state'].lower()}",
            "connection_state": status["state"],
            "page_ready": status["page_ready"],
            "search_ready": status["search_ready"],
            "player_loaded": status["player_loaded"],
            "playing": status["playing"],
            "title": "",
            "artist": "",
        }
    try:
        result = await _ytm_page.evaluate(_STATE_JS)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "playing": None, "title": "", "artist": ""}
    if not isinstance(result, dict):
        return {"ok": False, "error": "unexpected eval", "playing": None, "title": "", "artist": ""}
    global _player_loaded, _playing
    _player_loaded = bool(result.get("player_loaded") or _track_identity(result) is not None)
    playing = result.get("playing")
    _playing = playing if isinstance(playing, bool) else None
    return result


def _track_identity(state: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Return the strongest available identity for a loaded track."""
    if not isinstance(state, dict) or not state.get("ok"):
        return None
    track_id = str(state.get("track_id") or state.get("trackId") or "").strip()
    if track_id:
        return ("id", track_id)
    title = str(state.get("title") or "").strip().casefold()
    artist = str(state.get("artist") or "").strip().casefold()
    if title or artist:
        return ("metadata", title, artist)
    return None


def _verify_action(
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Verify an action from observed state, never from command delivery."""
    if not isinstance(after, dict) or not after.get("ok"):
        return False, "unavailable"
    if action == "pause":
        if after.get("playing") is False:
            return True, "verified"
        return False, "unavailable" if after.get("playing") is None else "failed"
    if action == "play":
        if after.get("playing") is True:
            return True, "verified"
        return False, "unavailable" if after.get("playing") is None else "failed"

    before_identity = _track_identity(before)
    after_identity = _track_identity(after)
    if before_identity is None or after_identity is None:
        return False, "unavailable"
    return (before_identity != after_identity, "verified" if before_identity != after_identity else "failed")


def _state_has_player(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict) or state.get("ok") is not True:
        return False
    return bool(state.get("player_loaded") or _track_identity(state) is not None)


async def control(action: str) -> dict[str, Any]:
    """Transport komanda (play/pause/next/previous) na web tabu.
    Verifikuje stanje posle akcije. Vraca ``{"ok", "action", "method",
    "verified", "state"}``."""
    if action not in ("play", "pause", "next", "previous"):
        return {"ok": False, "error": f"unknown action: {action}", "action": action}
    if not await ensure_ready():
        status = _status_payload()
        return {
            "ok": False,
            "error": f"YT Music is {status['state'].lower()}",
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "connection_state": status["state"],
            "page_ready": status["page_ready"],
            "search_ready": status["search_ready"],
            "player_loaded": status["player_loaded"],
        }

    before = await get_state()
    if not _state_has_player(before):
        status = _status_payload()
        return {
            "ok": False,
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "degraded": False,
            "before": before,
            "after": None,
            "state": before,
            "connection_state": status["state"],
            "error": "YT Music player has no loaded track",
        }
    try:
        send_result = await _ytm_page.evaluate(_CONTROL_JS, action)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "before": before,
        }

    delivered = bool(send_result and send_result.get("ok"))
    if not delivered:
        return {
            "ok": False,
            "action": action,
            "method": (send_result or {}).get("method"),
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "before": before,
            "after": None,
            "state": before,
            "error": (send_result or {}).get("error", "command was not delivered"),
        }

    await asyncio.sleep(0.4)
    state = await get_state()
    verified, verification = _verify_action(action, before, state)

    if not verified and action in ("next", "previous"):
        await asyncio.sleep(0.5)
        state = await get_state()
        verified, verification = _verify_action(action, before, state)

    track_changed = None
    if action in ("next", "previous"):
        before_identity = _track_identity(before)
        after_identity = _track_identity(state)
        track_changed = (
            before_identity != after_identity
            if before_identity is not None and after_identity is not None
            else None
        )

    result = {
        "ok": bool(delivered and verified),
        "action": action,
        "method": (send_result or {}).get("method"),
        "adapter": "ytm_web",
        "delivered": delivered,
        "verified": verified,
        "verification": verification,
        "degraded": delivered and not verified and verification == "unavailable",
        "before": before,
        "after": state,
        "state": state,
    }
    if track_changed is not None or action in ("next", "previous"):
        result["track_changed"] = track_changed
    if not verified:
        result["error"] = (
            "track transition could not be verified"
            if action in ("next", "previous") and verification == "unavailable"
            else "track did not change"
            if action in ("next", "previous")
            else f"playback state did not reach {'playing' if action == 'play' else 'paused'}"
        )
    return result


async def play_query(query: str) -> dict[str, Any]:
    """Search and play a real YTM video result in the connected browser."""
    if not query.strip():
        return {"ok": False, "error": "empty query"}
    if not await ensure_ready():
        status = _status_payload()
        return {
            "ok": False,
            "error": f"YT Music is {status['state'].lower()}",
            "connection_state": status["state"],
            "page_ready": status["page_ready"],
            "search_ready": status["search_ready"],
            "player_loaded": status["player_loaded"],
            "playing": status["playing"],
            "query": query,
            "adapter": "ytm_web",
        }

    selected_video_id: str | None = None
    selected_locator = None
    clicked_result = False
    try:
        home = await _ytm_page.evaluate(
            "() => ({ url: location.href, hasSearch: !!document.querySelector('ytmusic-search-box input') })"
        )
        if not home.get("hasSearch"):
            if not await _navigate_to_ytm() or not await ensure_ready():
                status = _status_payload()
                return {
                    "ok": False,
                    "error": "YT Music search is not ready",
                    "connection_state": status["state"],
                    "query": query,
                    "adapter": "ytm_web",
                }

        r = await _ytm_page.evaluate(_SEARCH_INPUT_JS, query)
        if not (r and r.get("ok")):
            return {"ok": False, "error": (r or {}).get("error", "search input failed"), "query": query}

        await _ytm_page.keyboard.press("Enter")
        await _ytm_page.wait_for_url("**/search**", timeout=8000)
        await _ytm_page.wait_for_selector(
            ", ".join(_PLAYABLE_RESULT_SELECTORS),
            timeout=12000,
        )

        for sel in _PLAYABLE_RESULT_SELECTORS:
            loc = _ytm_page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=3000)
                href = await loc.get_attribute("href")
                video_id = _video_id_from_href(href)
                if not video_id:
                    continue
                selected_video_id = video_id
                selected_locator = loc
                break
            except Exception:
                continue
        if selected_locator is None or selected_video_id is None:
            return {
                "ok": False,
                "error": "no playable YT Music search result",
                "query": query,
                "adapter": "ytm_web",
            }

        await selected_locator.click()
        clicked_result = True

        try:
            await _ytm_page.wait_for_function(
                "() => { const v = document.querySelector('video'); return v && !v.paused && v.currentTime > 0; }",
                timeout=12000,
            )
        except Exception:
            state = await get_state()
            return {
                "ok": False,
                "query": query,
                "selected_video_id": selected_video_id,
                "adapter": "ytm_web",
                "delivered": True,
                "verified": False,
                "verification": "unavailable",
                "degraded": True,
                "error": "player did not start in time",
                "state": state,
                "title": state.get("title", ""),
                "artist": state.get("artist", ""),
            }

        state = await get_state()
        actual_video_id = str((state or {}).get("track_id") or "").strip()
        state_identity = _track_identity(state)
        if actual_video_id:
            verified = actual_video_id == selected_video_id and state.get("playing") is True
            verification = "verified" if verified else "failed"
        else:
            verified = bool(state.get("playing") is True and state_identity is not None)
            verification = "verified_metadata" if verified else "unavailable"
        result = {
            "ok": verified,
            "query": query,
            "selected_video_id": selected_video_id,
            "actual_video_id": actual_video_id,
            "adapter": "ytm_web",
            "method": "dom_search_result",
            "delivered": True,
            "verified": verified,
            "verification": verification,
            "degraded": not verified and verification == "unavailable",
            "state": state,
            "title": state.get("title", ""),
            "artist": state.get("artist", ""),
        }
        if not verified:
            result["error"] = (
                "selected YT Music result did not become the playing track"
                if verification == "failed"
                else "YT Music playback could not be verified"
            )
        return result
    except Exception as exc:
        log.warning("ytm_web.play_query failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "query": query,
            "adapter": "ytm_web",
            "delivered": clicked_result,
            "verified": False,
            "verification": "unavailable" if clicked_result else "not_attempted",
            "degraded": clicked_result,
            **({"selected_video_id": selected_video_id} if selected_video_id else {}),
        }


async def shutdown() -> None:
    global _pw, _context, _ytm_page, _browser, _launched, _active_profile
    global _connection_state, _connection_error
    global _warmup_task
    if _warmup_task is not None and not _warmup_task.done() and _warmup_task is not asyncio.current_task():
        _warmup_task.cancel()
        try:
            await _warmup_task
        except asyncio.CancelledError:
            pass
    try:
        if _context is not None:
            await _context.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _ytm_page = None
    _context = None
    _browser = None
    _pw = None
    _launched = False
    _active_profile = None
    _warmup_task = None
    _connection_state = DISCONNECTED
    _connection_error = None
    _clear_runtime_state()


def active_profile() -> str | None:
    return _active_profile
