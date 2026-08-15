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

_YTM_VOLUME_DEFAULT_AMOUNT = 10
_YTM_VOLUME_TOLERANCE = 0.011
_YTM_PREVIOUS_RESTART_MIN_TIME = 3.0
_YTM_PREVIOUS_RESTART_MAX_TIME = 2.5


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


def _context_runtime_alive() -> bool:
    """Return whether the persistent Playwright context is still usable."""
    if not _launched or _context is None:
        return False
    return _browser is None or _safe_is_connected(_browser)


def _context_pages() -> list[Any]:
    """Return live pages, falling back to the tracked page when needed."""
    if _context is None:
        return []
    try:
        raw_pages = getattr(_context, "pages", None)
        pages = list(raw_pages) if raw_pages is not None else []
    except Exception:
        pages = []
    if _ytm_page is not None and not _safe_is_closed(_ytm_page):
        if not any(page is _ytm_page for page in pages):
            pages.append(_ytm_page)
    return [page for page in pages if not _safe_is_closed(page)]


def _page_origin(page: Any) -> str:
    """Return a safe page origin without query, fragment or credentials."""
    try:
        url = str(getattr(page, "url", "") or "")
    except Exception:
        return "<unavailable>"
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "<unavailable>"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"


def _is_ytm_page(page: Any) -> bool:
    return _page_origin(page) == YTM_URL


def _runtime_alive() -> bool:
    """Return whether the dedicated Playwright runtime still has a live tab."""
    if not _context_runtime_alive() or _ytm_page is None:
        return False
    if _safe_is_closed(_ytm_page):
        return False
    return True


def _connection_marker_path() -> Path:
    return _JARVIS_PROFILE_DIR / ".connected"


def _profile_is_connected() -> bool:
    return _connection_marker_path().is_file()


def _mark_profile_connected() -> None:
    try:
        marker = _connection_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError as exc:
        log.warning("ytm_web: could not persist connection marker: %s", exc)


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


async def _launch_browser(*, start_minimized: bool = False) -> bool:
    """Launch the one headed, persistent browser used by all YTM actions."""
    global _pw, _context, _ytm_page, _launched, _active_profile, _browser
    if _context_runtime_alive():
        await _find_or_adopt_ytm_page()
        if _ytm_page is not None and not _safe_is_closed(_ytm_page):
            return True

    user_data_dir, args = _resolve_profile()
    if start_minimized:
        args = [*args, "--start-minimized"]
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
                    args=args,
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
    ytm_pages = [page for page in pages if _is_ytm_page(page)]
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

    await _find_or_adopt_ytm_page()
    return True


_PAGE_PROBE_JS = r"""
() => {
  const pageReady = location.origin === 'https://music.youtube.com';
  const search = document.querySelector('ytmusic-search-box input')
    || document.querySelector('input#input')
    || document.querySelector('input[name="search_query"]');
  const ytmApp = document.querySelector('ytmusic-app');
  const nav = document.querySelector('ytmusic-nav-bar');
  const account = document.querySelector('#avatar-btn, ytmusic-nav-bar #avatar-btn, ytmusic-nav-bar [aria-label*="Account" i], ytmusic-nav-bar a[href*="/channel/"]');
  const signIn = document.querySelector('a[href*="ServiceLogin"], #sign-in-button, tp-yt-paper-button[aria-label*="Sign in" i], ytmusic-pivot-bar-item-renderer[tab-id="SIGN_IN"], ytmusic-guide-entry-renderer[tab-id="SIGN_IN"]');
  const signInText = Array.from(document.querySelectorAll('button, a, tp-yt-paper-button'))
    .some((el) => /^(sign in|log in|prijavi se)$/i.test(
      (el.getAttribute('aria-label') || el.textContent || '').trim()
    ));
  const googleLogin = /(^|\.)accounts\.google\.com$/i.test(location.hostname)
    || /(^|\.)google\.com$/i.test(location.hostname) && /servicelogin|signin/i.test(location.pathname);
  const explicitLoginRequired = googleLogin || !!signIn || signInText;
  const ytmSurfaceReady = pageReady && !!search && (!!ytmApp || !!nav);
  const video = document.querySelector('video');
  const title = document.querySelector('.title.ytmusic-player-bar');
  const playerLoaded = !!(video && title && title.textContent && title.textContent.trim().length > 0);
  const trackId = new URL(location.href).searchParams.get('v') || '';
  return {
    ok: true,
    origin: location.origin,
    page_ready: pageReady,
    search_ready: pageReady && !!search,
    ytm_surface_ready: ytmSurfaceReady,
    authenticated: ytmSurfaceReady && !explicitLoginRequired,
    login_required: explicitLoginRequired,
    auth_evidence: explicitLoginRequired ? 'login_required' : (ytmSurfaceReady ? 'usable_surface' : 'unknown'),
    has_ytm_app: !!ytmApp,
    has_nav: !!nav,
    has_search: !!search,
    has_account: !!account,
    has_explicit_login: explicitLoginRequired,
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


async def _evaluate_page_probe(page: Any) -> dict[str, Any] | None:
    try:
        probe = await page.evaluate(_PAGE_PROBE_JS)
    except Exception as exc:
        log.debug("ytm_web: page probe failed for origin=%s: %s", _page_origin(page), exc)
        return None
    if not isinstance(probe, dict):
        return None
    return probe


def _probe_is_usable(probe: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(probe, dict)
        and probe.get("authenticated")
        and probe.get("page_ready")
        and probe.get("search_ready")
    )


def _log_probe(page: Any, probe: dict[str, Any]) -> None:
    log.debug(
        "ytm_web: probe profile=%s origin=%s page_ready=%s search_ready=%s "
        "ytm_surface_ready=%s player_loaded=%s auth_evidence=%s "
        "has_ytm_app=%s has_nav=%s has_search=%s has_account=%s "
        "has_explicit_login=%s",
        _active_profile or _JARVIS_PROFILE_DIR,
        _page_origin(page),
        bool(probe.get("page_ready")),
        bool(probe.get("search_ready")),
        bool(probe.get("ytm_surface_ready")),
        bool(probe.get("player_loaded")),
        probe.get("auth_evidence", "unknown"),
        bool(probe.get("has_ytm_app")),
        bool(probe.get("has_nav")),
        bool(probe.get("has_search")),
        bool(probe.get("has_account")),
        bool(probe.get("has_explicit_login")),
    )


async def _find_or_adopt_ytm_page() -> Any | None:
    """Find the usable YTM page and replace stale tracked-page references."""
    global _ytm_page
    if not _context_runtime_alive():
        return None

    pages = _context_pages()
    log.debug(
        "ytm_web: page inventory profile=%s count=%s origins=%s",
        _active_profile or _JARVIS_PROFILE_DIR,
        len(pages),
        [_page_origin(page) for page in pages],
    )
    ytm_pages = [page for page in pages if _is_ytm_page(page)]
    selected = None

    if ytm_pages:
        ordered = []
        if any(page is _ytm_page for page in ytm_pages):
            ordered.append(_ytm_page)
        ordered.extend(page for page in reversed(ytm_pages) if all(page is not item for item in ordered))
        for page in ordered:
            probe = await _evaluate_page_probe(page)
            if probe is not None:
                _log_probe(page, probe)
            if _probe_is_usable(probe):
                selected = page
                break
        if selected is None:
            selected = ordered[0]
    elif _ytm_page is not None and not _safe_is_closed(_ytm_page):
        # Keep an accounts.google.com page while the user is completing login,
        # but replace it as soon as a live music.youtube.com page appears.
        selected = _ytm_page
    elif pages:
        selected = pages[-1]

    if selected is not _ytm_page:
        log.info(
            "ytm_web: selected page profile=%s origin=%s previous_origin=%s",
            _active_profile or _JARVIS_PROFILE_DIR,
            _page_origin(selected) if selected is not None else "<none>",
            _page_origin(_ytm_page) if _ytm_page is not None else "<none>",
        )
    _ytm_page = selected
    return selected


async def _probe_page() -> dict[str, Any] | None:
    page = await _find_or_adopt_ytm_page()
    if page is None:
        _clear_runtime_state()
        return None
    probe = await _evaluate_page_probe(page)
    if probe is None:
        _clear_runtime_state()
        return None
    _apply_probe(probe)
    _log_probe(page, probe)
    return probe


def _set_connection_from_probe(probe: dict[str, Any] | None) -> None:
    global _connection_state, _connection_error
    previous_state = _connection_state
    if probe is None:
        _connection_state = ERROR
        _connection_error = "YT Music browser page is unavailable"
    elif _probe_is_usable(probe):
        _connection_state = CONNECTED
        _connection_error = None
        _mark_profile_connected()
    elif probe.get("login_required"):
        _connection_state = NEEDS_LOGIN
        _connection_error = None
    elif not probe.get("page_ready") or not probe.get("search_ready"):
        _connection_state = ERROR
        _connection_error = "YT Music page/search is not ready"
    else:
        _connection_state = ERROR
        _connection_error = "YT Music session could not be verified"

    if _connection_state != previous_state:
        log.info(
            "ytm_web: connection state %s -> %s profile=%s origin=%s "
            "page_ready=%s search_ready=%s auth_evidence=%s "
            "ytm_surface_ready=%s has_ytm_app=%s has_nav=%s has_search=%s "
            "has_account=%s has_explicit_login=%s",
            previous_state,
            _connection_state,
            _active_profile or _JARVIS_PROFILE_DIR,
            probe.get("origin", _page_origin(_ytm_page)) if isinstance(probe, dict) else "<none>",
            bool(probe.get("page_ready")) if isinstance(probe, dict) else False,
            bool(probe.get("search_ready")) if isinstance(probe, dict) else False,
            probe.get("auth_evidence", "unknown") if isinstance(probe, dict) else "unknown",
            bool(probe.get("ytm_surface_ready")) if isinstance(probe, dict) else False,
            bool(probe.get("has_ytm_app")) if isinstance(probe, dict) else False,
            bool(probe.get("has_nav")) if isinstance(probe, dict) else False,
            bool(probe.get("has_search")) if isinstance(probe, dict) else False,
            bool(probe.get("has_account")) if isinstance(probe, dict) else False,
            bool(probe.get("has_explicit_login")) if isinstance(probe, dict) else False,
        )


async def _refresh_connection_status() -> dict[str, Any]:
    global _connection_state, _connection_error
    if not _context_runtime_alive():
        _clear_runtime_state()
        if _connection_state not in (DISCONNECTED, CONNECTING):
            _connection_state = ERROR
            _connection_error = "YT Music browser session is no longer running"
        return _status_payload()
    probe = await _probe_page()
    _set_connection_from_probe(probe)
    return _status_payload()


async def _present_ytm_page() -> bool:
    page = await _find_or_adopt_ytm_page()
    if page is None:
        return False
    try:
        await page.bring_to_front()
        log.info("ytm_web: presented dedicated YT Music page")
        return True
    except Exception as exc:
        log.warning("ytm_web: could not present dedicated YT Music page: %s", exc)
        return False


async def _enforce_background_window() -> dict[str, Any]:
    """Minimize only the dedicated YT Music window through its CDP target."""
    page = await _find_or_adopt_ytm_page()
    if page is None or _browser is None:
        return {"ok": False, "error": "dedicated YT Music browser target is unavailable"}
    cdp = None
    try:
        cdp = await _browser.new_browser_cdp_session()
        targets = await cdp.send("Target.getTargets")
        page_url = str(getattr(page, "url", "") or "")
        target_infos = targets.get("targetInfos", []) if isinstance(targets, dict) else []
        target = next(
            (
                info
                for info in target_infos
                if info.get("type") == "page"
                and info.get("url") == page_url
                and _page_origin(page) == YTM_URL
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    info
                    for info in target_infos
                    if info.get("type") == "page"
                    and _page_origin(page) == YTM_URL
                    and str(info.get("url") or "").startswith(YTM_URL)
                ),
                None,
            )
        if not isinstance(target, dict) or not target.get("targetId"):
            return {"ok": False, "error": "dedicated YT Music CDP target was not found"}
        target_id = target["targetId"]
        window = await cdp.send("Browser.getWindowForTarget", {"targetId": target_id})
        window_id = window.get("windowId") if isinstance(window, dict) else None
        bounds = window.get("bounds") if isinstance(window, dict) else None
        if window_id is None or not isinstance(bounds, dict):
            return {"ok": False, "error": "dedicated YT Music window bounds were unavailable"}
        state = bounds.get("windowState")
        changed = state != "minimized"
        if changed:
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
            state = "minimized"
        result = {
            "ok": True,
            "target_id": target_id,
            "window_id": window_id,
            "window_state": state,
            "changed": changed,
        }
        log.debug(
            "ytm_web: background window target=%s window=%s state=%s changed=%s",
            target_id,
            window_id,
            state,
            changed,
        )
        return result
    except Exception as exc:
        log.debug("ytm_web: background window enforcement unavailable: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass


async def _navigate_to_ytm(*, present: bool = False) -> bool:
    global _connection_state, _connection_error
    page = await _find_or_adopt_ytm_page()
    if page is None:
        return False
    try:
        if present and not await _present_ytm_page():
            _connection_state = ERROR
            _connection_error = "YT Music login page could not be presented"
            return False
        await page.goto(YTM_URL, wait_until="domcontentloaded", timeout=15000)
        # Give the SPA a short opportunity to render. Login itself remains a
        # user action in the headed browser and is never automated here.
        await asyncio.sleep(0.5)
        await _refresh_connection_status()
        if not present:
            await _enforce_background_window()
        return True
    except Exception as exc:
        _connection_state = ERROR
        _connection_error = "YT Music page could not be opened"
        log.warning("ytm_web: navigate failed: %s", exc)
        return False


async def connect() -> dict[str, Any]:
    """Open the dedicated headed profile for a user-driven Google login."""
    global _connection_state, _connection_error
    async with _get_lock():
        if _context_runtime_alive():
            page = await _find_or_adopt_ytm_page()
            if page is not None and _page_origin(page) != "<unavailable>":
                # Re-present an existing YTM/Google login page without
                # navigating it away while the user is completing login.
                if not await _present_ytm_page():
                    _connection_state = ERROR
                    _connection_error = "YT Music page could not be presented"
                    return _status_payload()
                return await _refresh_connection_status()
        _connection_state = CONNECTING
        _connection_error = None
        if not await _launch_browser():
            _connection_state = ERROR
            _connection_error = "Could not launch the dedicated YT Music browser"
            return _status_payload()
        if not await _navigate_to_ytm(present=True):
            return _status_payload()
        return await _refresh_connection_status()


async def _restore_existing_connection() -> None:
    global _connection_state, _connection_error
    if not _profile_is_connected():
        return
    _connection_state = CONNECTING
    _connection_error = None
    try:
        if await _launch_browser(start_minimized=True):
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
    if not _profile_is_connected():
        return
    if _warmup_task is not None and not _warmup_task.done():
        return
    _warmup_task = asyncio.create_task(_restore_existing_connection())


async def connection_status() -> dict[str, Any]:
    """Return safe connection and runtime/player state from the YTM page."""
    if _context_runtime_alive() and _connection_state != CONNECTING:
        await _refresh_connection_status()
    return _status_payload()


async def ensure_ready() -> bool:
    """Ensure authenticated page/search readiness, not player readiness."""
    global _connection_state, _connection_error
    if is_available():
        await _refresh_connection_status()
        await _enforce_background_window()
        return is_available()
    if not _launched:
        if not _profile_is_connected():
            return False
        _connection_state = CONNECTING
        if not await _launch_browser(start_minimized=True):
            _connection_state = ERROR
            _connection_error = "Could not restore the YT Music browser"
            return False
        await _navigate_to_ytm()
    else:
        await _refresh_connection_status()
        await _enforce_background_window()
    return is_available()


def is_available() -> bool:
    """Return whether the authenticated YTM page can accept search actions."""
    return _connection_state == CONNECTED and _search_ready and _runtime_alive()


_STATE_JS = """
() => {
  const ytmPlayerState = true;
  void ytmPlayerState;
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

_CONTROL_JS = r"""
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
    const bar = document.querySelector('ytmusic-player-bar');
    if (!bar) return { ok: false, error: 'no ytmusic-player-bar' };
    const labels = ['next', 'sledeća', 'sljedeća'];
    const host = Array.from(bar.querySelectorAll('yt-icon-button, button, [role="button"]'))
      .find((element) => {
        const style = getComputedStyle(element);
        const visible = !element.hidden && style.display !== 'none'
          && style.visibility !== 'hidden' && element.getClientRects().length > 0;
        if (!visible) return false;
        const classes = String(element.className || '').split(/\s+/);
        const title = (element.getAttribute('title') || '').trim().toLowerCase();
        const aria = (element.getAttribute('aria-label') || '').trim().toLowerCase();
        return classes.includes('next-button') || labels.includes(title) || labels.includes(aria);
      });
    if (!host) return { ok: false, error: 'no next control in ytmusic-player-bar' };
    const target = host.matches('yt-icon-button, [role="button"]')
      ? (host.querySelector('button[aria-label], button') || host)
      : host;
    if (host.disabled || host.getAttribute('aria-disabled') === 'true'
      || target.disabled || target.getAttribute('aria-disabled') === 'true') {
      return { ok: false, error: 'next control disabled' };
    }
    target.click();
    return {
      ok: true,
      method: 'click ytmusic-player-bar .next-button button',
      control: { tag: host.tagName.toLowerCase(), className: String(host.className || ''),
        title: host.getAttribute('title') || '', aria: target.getAttribute('aria-label') || '' },
    };
  }
  if (action === 'previous') {
    const bar = document.querySelector('ytmusic-player-bar');
    if (!bar) return { ok: false, error: 'no ytmusic-player-bar' };
    const labels = ['previous', 'prethodna'];
    const host = Array.from(bar.querySelectorAll('yt-icon-button, button, [role="button"]'))
      .find((element) => {
        const style = getComputedStyle(element);
        const visible = !element.hidden && style.display !== 'none'
          && style.visibility !== 'hidden' && element.getClientRects().length > 0;
        if (!visible) return false;
        const classes = String(element.className || '').split(/\s+/);
        const title = (element.getAttribute('title') || '').trim().toLowerCase();
        const aria = (element.getAttribute('aria-label') || '').trim().toLowerCase();
        return classes.includes('previous-button') || labels.includes(title) || labels.includes(aria);
      });
    if (!host) return { ok: false, error: 'no previous control in ytmusic-player-bar' };
    const target = host.matches('yt-icon-button, [role="button"]')
      ? (host.querySelector('button[aria-label], button') || host)
      : host;
    if (host.disabled || host.getAttribute('aria-disabled') === 'true'
      || target.disabled || target.getAttribute('aria-disabled') === 'true') {
      return { ok: false, error: 'previous control disabled' };
    }
    target.click();
    return {
      ok: true,
      method: 'click ytmusic-player-bar .previous-button button',
      control: { tag: host.tagName.toLowerCase(), className: String(host.className || ''),
        title: host.getAttribute('title') || '', aria: target.getAttribute('aria-label') || '' },
    };
  }
  return { ok: false, error: 'unknown action: ' + action };
}
"""

_MEDIA_VOLUME_STATE_JS = r"""
() => {
  const ytmMediaVolumeState = true;
  void ytmMediaVolumeState;
  const video = document.querySelector('video');
  if (!video) return { ok: false, volume: null, muted: null, error: 'no video element' };
  return {
    ok: true,
    volume: Number.isFinite(video.volume) ? video.volume : null,
    muted: !!video.muted,
  };
}
"""

_MEDIA_VOLUME_CONTROL_JS = r"""
async (request) => {
  const ytmMediaVolumeControl = true;
  void ytmMediaVolumeControl;
  const video = document.querySelector('video');
  if (!video) return { ok: false, error: 'no video element' };
  const action = String((request && request.action) || '');
  const amount = Number(request && request.amount);
  const level = Number(request && request.level);
  const before = {
    volume: Number.isFinite(video.volume) ? video.volume : null,
    muted: !!video.muted,
  };
  if (action === 'volume_up') {
    if (!Number.isFinite(amount) || amount < 1 || amount > 100) {
      return { ok: false, error: 'volume amount must be between 1 and 100' };
    }
    video.volume = Math.min(1, Math.max(0, video.volume + amount / 100));
  } else if (action === 'volume_down') {
    if (!Number.isFinite(amount) || amount < 1 || amount > 100) {
      return { ok: false, error: 'volume amount must be between 1 and 100' };
    }
    video.volume = Math.min(1, Math.max(0, video.volume - amount / 100));
  } else if (action === 'volume_set') {
    if (!Number.isFinite(level) || level < 0 || level > 100) {
      return { ok: false, error: 'volume level must be between 0 and 100' };
    }
    video.volume = Math.min(1, Math.max(0, level / 100));
  } else if (action === 'volume_mute') {
    video.muted = !video.muted;
  } else {
    return { ok: false, error: 'unknown volume action: ' + action };
  }
  return {
    ok: true,
    method: 'html_media_element',
    before,
    requested: { action, amount: Number.isFinite(amount) ? amount : null,
      level: Number.isFinite(level) ? level : null },
  };
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

_SEARCH_RESULTS_STATE_JS = r"""
() => {
  const searchSurfaceState = true;
  void searchSurfaceState;
  const root = document.querySelector('ytmusic-search-page, ytmusic-section-list-renderer');
  const rows = root && root.querySelector(
    'ytmusic-responsive-list-item-renderer, ytmusic-two-row-item-renderer, ytmusic-item-renderer'
  );
  const params = new URL(location.href).searchParams;
  return {
    path: location.pathname,
    query: params.get('q') || '',
    surface_ready: !!root,
    rows_ready: !!rows,
    ready: location.pathname === '/search' && !!root && !!rows,
  };
}
"""

_SEARCH_RESULTS_READY_JS = r"""
(expectedQuery) => {
  const searchResultsReady = true;
  void searchResultsReady;
  const root = document.querySelector('ytmusic-search-page, ytmusic-section-list-renderer');
  const currentQuery = new URL(location.href).searchParams.get('q') || '';
  return location.pathname === '/search'
    && currentQuery === String(expectedQuery || '')
    && !!root;
}
"""

_SEARCH_RESULTS_ROWS_READY_JS = r"""
(expectedQuery) => {
  const searchResultsRowsReady = true;
  void searchResultsRowsReady;
  const root = document.querySelector('ytmusic-search-page, ytmusic-section-list-renderer');
  const rows = root && root.querySelector(
    'ytmusic-responsive-list-item-renderer, ytmusic-two-row-item-renderer, ytmusic-item-renderer'
  );
  const currentQuery = new URL(location.href).searchParams.get('q') || '';
  return location.pathname === '/search'
    && currentQuery === String(expectedQuery || '')
    && !!root
    && !!rows;
}
"""

_FIND_PLAYABLE_SEARCH_RESULT_JS = r"""
(options) => {
  const findPlayableSearchResult = true;
  void findPlayableSearchResult;
  const shouldClick = !!(options && options.click);
  const expectedQuery = String((options && options.query) || '');
  const searchRoot = document.querySelector('ytmusic-search-page, ytmusic-section-list-renderer') || document;
  const rows = Array.from(searchRoot.querySelectorAll(
    'ytmusic-responsive-list-item-renderer, ytmusic-two-row-item-renderer, ytmusic-item-renderer'
  ));

  const inspectData = (element) => {
    const seen = new Set();
    const data = {
      video_id: '',
      has_watch_endpoint: false,
      has_play_navigation_endpoint: false,
    };
    const visit = (value, depth) => {
      if (!value || typeof value !== 'object' || depth > 7 || seen.has(value)) return;
      seen.add(value);
      for (const key of Object.keys(value).slice(0, 160)) {
        const child = value[key];
        if (/^videoId$/i.test(key) && !data.video_id && (typeof child === 'string' || typeof child === 'number')) {
          data.video_id = String(child);
        }
        if (key === 'watchEndpoint') data.has_watch_endpoint = true;
        if (key === 'playNavigationEndpoint') data.has_play_navigation_endpoint = true;
        if (child && typeof child === 'object') visit(child, depth + 1);
      }
    };
    for (const key of ['data', '__data', 'item', 'song', 'video']) {
      try { visit(element[key], 0); } catch (_) {}
    }
    return data;
  };

  const text = (value, limit = 160) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const normalize = (value) => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/đ/gi, 'd')
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
  const queryTokens = normalize(expectedQuery).split(/\s+/).filter(Boolean);
  const watchAnchorFor = (row) => Array.from(row.querySelectorAll('a[href]')).find((anchor) => {
    try {
      return new URL(anchor.href, location.href).pathname === '/watch';
    } catch (_) {
      return false;
    }
  }) || null;
  const playButtonFor = (row) => Array.from(row.querySelectorAll('ytmusic-play-button-renderer'))
    .find((button) => /^play\b/i.test(button.getAttribute('aria-label') || '')) || null;

  let firstCandidate = null;
  let bestCandidate = null;
  let bestScore = -1;
  for (const row of rows) {
    const data = inspectData(row);
    const playButton = playButtonFor(row);
    const watchAnchor = watchAnchorFor(row);
    if (!data.video_id || (!data.has_watch_endpoint && !watchAnchor)) continue;
    if (!playButton && !watchAnchor) continue;

    const titleAnchor = Array.from(row.querySelectorAll('a[href]')).find((anchor) => {
      try {
        const pathname = new URL(anchor.href, location.href).pathname;
        return pathname === '/watch' && text(anchor.getAttribute('aria-label') || anchor.textContent);
      } catch (_) {
        return false;
      }
    });
    const channelAnchor = Array.from(row.querySelectorAll('a[href]')).find((anchor) => {
      try {
        return new URL(anchor.href, location.href).pathname.startsWith('/channel/');
      } catch (_) {
        return false;
      }
    });
    const playLabel = playButton ? (playButton.getAttribute('aria-label') || '') : '';
    const selectedTitle = text(playLabel.replace(/^play\s*/i, ''))
      || text(titleAnchor && (titleAnchor.getAttribute('aria-label') || titleAnchor.textContent))
      || '';
    const selectedArtist = text(channelAnchor && channelAnchor.textContent) || '';
    // The watch endpoint is the most reliable click target on the current
    // YTM search surface: the overlay play control can load a result without
    // starting playback. Keep the play control as the bounded fallback for
    // result shapes that expose no watch anchor.
    const target = watchAnchor
      || (playButton && playButton.querySelector('button, tp-yt-paper-icon-button'))
      || playButton
      || row;
    const searchable = normalize(`${row.textContent || ''} ${selectedTitle} ${selectedArtist}`);
    const score = queryTokens.reduce((total, token) => total + (searchable.includes(token) ? 1 : 0), 0);
    const candidate = {
      ok: true,
      clicked: false,
      selected_video_id: data.video_id,
      selected_title: selectedTitle,
      selected_artist: selectedArtist,
      selection_method: watchAnchor ? 'watch_endpoint_anchor' : 'ytmusic_play_button_renderer',
      component: row.tagName.toLowerCase(),
      query_match_score: score,
      target,
    };
    if (!firstCandidate) firstCandidate = candidate;
    if (score > bestScore) {
      bestCandidate = candidate;
      bestScore = score;
    }
  }
  const candidate = bestCandidate || firstCandidate;
  if (candidate) {
    if (shouldClick) candidate.target.click();
    delete candidate.target;
    candidate.clicked = shouldClick;
    return candidate;
  }
  return {
    ok: false,
    clicked: false,
    error_code: 'NO_PLAYABLE_SEARCH_RESULT',
    error: 'no playable YT Music search result',
  };
}
"""

_PLAYER_MATCH_JS = r"""
(expected) => {
  const video = document.querySelector('video');
  const titleElement = document.querySelector('.title.ytmusic-player-bar');
  const artistElement = document.querySelector('.byline.ytmusic-player-bar');
  const actualId = new URL(location.href).searchParams.get('v') || '';
  const actualTitle = (titleElement && titleElement.textContent || '').trim().toLocaleLowerCase();
  const actualArtist = (artistElement && artistElement.textContent || '').trim().toLocaleLowerCase();
  const expectedTitle = String((expected && expected.title) || '').trim().toLocaleLowerCase();
  const expectedArtist = String((expected && expected.artist) || '').trim().toLocaleLowerCase();
  const playing = !!(video && !video.paused && video.currentTime > 0 && video.readyState >= 2);
  const idMatches = !!(expected && expected.video_id && actualId === expected.video_id);
  const titleMatches = !!(expectedTitle && actualTitle && (actualTitle === expectedTitle || actualTitle.includes(expectedTitle) || expectedTitle.includes(actualTitle)));
  const artistMatches = !expectedArtist || !actualArtist || actualArtist.includes(expectedArtist) || expectedArtist.includes(actualArtist);
  return playing && (idMatches || (!actualId && titleMatches && artistMatches));
}
"""


def _video_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("v", [None])[0]
    except (TypeError, ValueError):
        return None


async def _find_playable_search_result(
    *,
    query: str = "",
    click: bool = False,
) -> dict[str, Any]:
    """Find a real YTM result using component data, optionally clicking it."""
    try:
        result = await _ytm_page.evaluate(
            _FIND_PLAYABLE_SEARCH_RESULT_JS,
            {"click": click, "query": query},
        )
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "PLAYABLE_RESULT_INSPECTION_FAILED",
            "error": str(exc),
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "error_code": "PLAYABLE_RESULT_INSPECTION_FAILED",
            "error": "unexpected playable-result probe response",
        }
    return result


def _play_query_result(query: str, **values: Any) -> dict[str, Any]:
    """Build a diagnostic result while preserving the live connection state."""
    status = _status_payload()
    result: dict[str, Any] = {
        "ok": False,
        "query": query,
        "adapter": "ytm_web",
        "connection_state": status["state"],
        "page_ready": status["page_ready"],
        "search_ready": status["search_ready"],
        "player_loaded": status["player_loaded"],
        "playing": status["playing"],
        "stage": "unknown",
        "search_submitted": False,
        "result_found": False,
        "selected_video_id": None,
        "selected_title": "",
        "selected_artist": "",
        "search_method": None,
        "delivered": False,
        "verified": False,
        "verification": "not_attempted",
        "degraded": False,
        "error_code": "PLAY_QUERY_FAILED",
        "error": None,
    }
    result.update(values)
    return result


def _playback_metadata_matches(
    state: dict[str, Any] | None,
    selected_title: str,
    selected_artist: str,
) -> bool:
    if not isinstance(state, dict) or state.get("playing") is not True:
        return False
    actual_title = str(state.get("title") or "").strip().casefold()
    actual_artist = str(state.get("artist") or "").strip().casefold()
    expected_title = selected_title.strip().casefold()
    expected_artist = selected_artist.strip().casefold()
    title_matches = bool(
        expected_title
        and actual_title
        and (
            actual_title == expected_title or actual_title in expected_title or expected_title in actual_title
        )
    )
    artist_matches = (
        not expected_artist
        or not actual_artist
        or expected_artist in actual_artist
        or actual_artist in expected_artist
    )
    return title_matches and artist_matches


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


async def _read_volume_state() -> dict[str, Any]:
    if _ytm_page is None:
        return {"ok": False, "volume": None, "muted": None, "error": "YT Music page is unavailable"}
    try:
        result = await _ytm_page.evaluate(_MEDIA_VOLUME_STATE_JS)
    except Exception as exc:
        return {"ok": False, "volume": None, "muted": None, "error": str(exc)}
    if not isinstance(result, dict):
        return {"ok": False, "volume": None, "muted": None, "error": "unexpected volume state"}
    return result


def _verify_volume_action(
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    amount: int | None = None,
    level: int | None = None,
) -> tuple[bool, str]:
    if not isinstance(after, dict) or not after.get("ok"):
        return False, "unavailable"
    if action == "volume_mute":
        if not isinstance(before, dict) or not isinstance(before.get("muted"), bool):
            return False, "unavailable"
        if isinstance(after.get("muted"), bool) and after["muted"] != before["muted"]:
            return True, "verified"
        return False, "failed"
    if not isinstance(before, dict) or not isinstance(before.get("volume"), (int, float)):
        return False, "unavailable"
    if not isinstance(after.get("volume"), (int, float)):
        return False, "unavailable"
    if action == "volume_set":
        if level is None:
            return False, "unavailable"
        expected = level / 100
    else:
        if amount is None:
            return False, "unavailable"
        delta = amount / 100 if action == "volume_up" else -amount / 100
        expected = min(1.0, max(0.0, float(before["volume"]) + delta))
    if abs(float(after["volume"]) - expected) <= _YTM_VOLUME_TOLERANCE:
        return True, "verified"
    return False, "failed"


def _volume_failure(
    action: str,
    *,
    error: str,
    error_code: str = "INVALID_ARGUMENTS",
    amount: int | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "action": action,
        "adapter": "ytm_web",
        "delivered": False,
        "verified": False,
        "verification": "not_attempted",
        "degraded": False,
        "error_code": error_code,
        "error": error,
    }
    if amount is not None:
        result["amount"] = amount
    if level is not None:
        result["level"] = level
    return result


async def control_volume(
    action: str,
    *,
    amount: int | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    """Change only the connected YT Music HTML media element volume."""
    if action not in ("volume_up", "volume_down", "volume_set", "volume_mute"):
        return _volume_failure(action, error=f"unknown volume action: {action}", error_code="UNKNOWN_ACTION")

    requested_amount: int | None = None
    requested_level: int | None = None
    if action in ("volume_up", "volume_down"):
        requested_amount = _YTM_VOLUME_DEFAULT_AMOUNT if amount is None else amount
        if isinstance(requested_amount, bool) or not isinstance(requested_amount, int):
            return _volume_failure(
                action,
                amount=requested_amount if isinstance(requested_amount, int) else None,
                error="volume amount must be an integer between 1 and 100",
            )
        if not 1 <= requested_amount <= 100:
            return _volume_failure(
                action,
                amount=requested_amount,
                error="volume amount must be between 1 and 100",
            )
    elif action == "volume_set":
        requested_level = level
        if isinstance(requested_level, bool) or not isinstance(requested_level, int):
            return _volume_failure(
                action,
                error="volume level must be an integer between 0 and 100",
            )
        if not 0 <= requested_level <= 100:
            return _volume_failure(
                action,
                level=requested_level,
                error="volume level must be between 0 and 100",
            )

    if not await ensure_ready():
        status = _status_payload()
        result = _volume_failure(
            action,
            amount=requested_amount,
            level=requested_level,
            error=f"YT Music is {status['state'].lower()}",
            error_code="NOT_READY",
        )
        result.update(
            {
                "connection_state": status["state"],
                "page_ready": status["page_ready"],
                "search_ready": status["search_ready"],
                "player_loaded": status["player_loaded"],
            }
        )
        return result

    player_state = await get_state()
    if not _state_has_player(player_state):
        result = _volume_failure(
            action,
            amount=requested_amount,
            level=requested_level,
            error="YT Music player has no loaded track",
            error_code="PLAYER_NOT_LOADED",
        )
        result.update(
            {
                "connection_state": _status_payload()["state"],
                "before": player_state,
                "after": None,
            }
        )
        return result

    before = await _read_volume_state()
    if not before.get("ok"):
        status = _status_payload()
        result = _volume_failure(
            action,
            amount=requested_amount,
            level=requested_level,
            error=before.get("error", "YT Music media element is unavailable"),
            error_code="MEDIA_ELEMENT_UNAVAILABLE",
        )
        result.update(
            {
                "connection_state": status["state"],
                "before": before,
                "after": None,
            }
        )
        return result

    request = {
        "action": action,
        "amount": requested_amount,
        "level": requested_level,
    }
    try:
        send_result = await _ytm_page.evaluate(_MEDIA_VOLUME_CONTROL_JS, request)
    except Exception as exc:
        send_result = {"ok": False, "error": str(exc)}
    send_data = send_result if isinstance(send_result, dict) else {}
    delivered = bool(send_data.get("ok"))
    if not delivered:
        status = _status_payload()
        result = _volume_failure(
            action,
            amount=requested_amount,
            level=requested_level,
            error=send_data.get("error", "volume command was not delivered"),
            error_code="DELIVERY_FAILED",
        )
        result.update(
            {
                "method": send_data.get("method"),
                "connection_state": status["state"],
                "before": before,
                "after": None,
            }
        )
        return result

    await asyncio.sleep(0.05)
    after = await _read_volume_state()
    verified, verification = _verify_volume_action(
        action,
        before,
        after,
        amount=requested_amount,
        level=requested_level,
    )
    await _enforce_background_window()
    status = _status_payload()
    result = {
        "ok": bool(delivered and verified),
        "action": action,
        "method": send_data.get("method", "html_media_element"),
        "adapter": "ytm_web",
        "delivered": delivered,
        "verified": verified,
        "verification": verification,
        "degraded": delivered and not verified and verification == "unavailable",
        "error_code": None
        if verified
        else "VERIFICATION_UNAVAILABLE"
        if verification == "unavailable"
        else "VOLUME_NOT_REACHED",
        "connection_state": status["state"],
        "before": before,
        "after": after,
        "volume": after.get("volume") if isinstance(after, dict) else None,
        "muted": after.get("muted") if isinstance(after, dict) else None,
    }
    if requested_amount is not None:
        result["amount"] = requested_amount
    if requested_level is not None:
        result["requested_level"] = requested_level
        actual_volume = result["volume"]
        result["level"] = round(actual_volume * 100) if isinstance(actual_volume, (int, float)) else None
    if not verified:
        result["error"] = (
            "YT Music volume change could not be verified"
            if verification == "unavailable"
            else "YT Music volume did not reach the requested value"
        )
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


def _native_previous_restart_detected(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    """Recognize YT Music's native restart-current Previous behavior.

    A second Previous click is safe only when the same track is identified in
    both reads and the media element demonstrably jumped from a progressed
    position back to the beginning. Missing metadata is never enough.
    """
    if _track_identity(before) is None or _track_identity(after) is None:
        return False
    if _track_identity(before) != _track_identity(after):
        return False
    before_time = before.get("currentTime") if isinstance(before, dict) else None
    after_time = after.get("currentTime") if isinstance(after, dict) else None
    if isinstance(before_time, bool) or isinstance(after_time, bool):
        return False
    if not isinstance(before_time, (int, float)) or not isinstance(after_time, (int, float)):
        return False
    return (
        before_time >= _YTM_PREVIOUS_RESTART_MIN_TIME
        and after_time <= _YTM_PREVIOUS_RESTART_MAX_TIME
        and after_time < before_time - 1.0
    )


async def _observe_after_delivery(
    action: str,
    before: dict[str, Any],
    *,
    detect_native_restart: bool = True,
) -> tuple[dict[str, Any] | None, bool, str, bool]:
    """Read bounded post-command state without sending another command."""
    read_count = 4 if action in ("next", "previous") else 1
    state: dict[str, Any] | None = None
    verification = "unavailable"
    for _ in range(read_count):
        await asyncio.sleep(0.4)
        state = await get_state()
        verified, verification = _verify_action(action, before, state)
        if verified:
            return state, True, "verified", False
        if (
            action == "previous"
            and detect_native_restart
            and _native_previous_restart_detected(before, state)
        ):
            return state, False, verification, True
    return state, False, verification, False


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
        status = _status_payload()
        return {
            "ok": False,
            "error": str(exc),
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "before": before,
            "after": None,
            "state": before,
            "connection_state": status["state"],
        }

    send_data = send_result if isinstance(send_result, dict) else {}
    delivered = bool(send_data.get("ok"))
    if not delivered:
        status = _status_payload()
        return {
            "ok": False,
            "action": action,
            "method": send_data.get("method"),
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "before": before,
            "after": None,
            "state": before,
            "connection_state": status["state"],
            "error": send_data.get("error", "command was not delivered"),
        }

    state, verified, verification, native_restart_detected = await _observe_after_delivery(action, before)
    click_count = 1 if action in ("next", "previous") else None
    intermediate = None
    second_click_attempted = False
    second_delivered = None
    transport_methods = [send_data.get("method")] if action in ("next", "previous") else None

    if action == "previous" and native_restart_detected:
        intermediate = state
        second_click_attempted = True
        try:
            second_send_result = await _ytm_page.evaluate(_CONTROL_JS, action)
        except Exception as exc:
            second_send_result = {"ok": False, "error": str(exc)}
        second_send_data = second_send_result if isinstance(second_send_result, dict) else {}
        second_delivered = bool(second_send_data.get("ok"))
        click_count = 1 + int(second_delivered)
        if second_delivered:
            transport_methods.append(second_send_data.get("method"))
            state, verified, verification, _ = await _observe_after_delivery(
                action,
                before,
                detect_native_restart=False,
            )

    track_changed = None
    if action in ("next", "previous"):
        before_identity = _track_identity(before)
        after_identity = _track_identity(state)
        track_changed = (
            before_identity != after_identity
            if before_identity is not None and after_identity is not None
            else None
        )

    if action in ("next", "previous"):
        await _enforce_background_window()

    result = {
        "ok": bool(delivered and verified),
        "action": action,
        "method": send_data.get("method"),
        "adapter": "ytm_web",
        "delivered": delivered,
        "verified": verified,
        "verification": verification,
        "degraded": delivered and not verified and verification == "unavailable",
        "before": before,
        "after": state,
        "state": state,
        "connection_state": _status_payload()["state"],
        "control": send_data.get("control"),
    }
    if track_changed is not None or action in ("next", "previous"):
        result["track_changed"] = track_changed
    if action in ("next", "previous"):
        result.update(
            {
                "click_count": click_count,
                "transport_methods": transport_methods,
                "intermediate": intermediate,
                "native_restart_detected": native_restart_detected,
                "second_click_attempted": second_click_attempted,
                "second_delivered": second_delivered,
            }
        )
        if action == "previous":
            result["native_result"] = (
                "restarted_current"
                if native_restart_detected
                else "changed_track"
                if verified
                else "unavailable"
                if verification == "unavailable"
                else "unchanged"
            )
    if not verified:
        if action in ("next", "previous"):
            if (
                action == "previous"
                and native_restart_detected
                and track_changed is False
                and second_delivered
            ):
                result["error_code"] = "NO_PREVIOUS_TRACK"
                result["native_result"] = "no_previous_track"
                result["error"] = "YT Music has no previous track"
            elif verification == "unavailable":
                result["error_code"] = "TRACK_TRANSITION_UNVERIFIED"
                result["error"] = "track transition could not be verified"
            else:
                result["error_code"] = "TRACK_DID_NOT_CHANGE"
                result["error"] = "track did not change"
        else:
            result["error"] = f"playback state did not reach {'playing' if action == 'play' else 'paused'}"
    return result


async def play_query(query: str) -> dict[str, Any]:
    """Search and play a real YTM video result in the connected browser."""
    query = query.strip()
    if not query:
        return _play_query_result(
            query,
            stage="input",
            error_code="INVALID_QUERY",
            error="empty query",
        )
    if not await ensure_ready():
        status = _status_payload()
        return _play_query_result(
            query,
            stage="connection",
            error_code="CONNECTION_UNAVAILABLE",
            error=f"YT Music is {status['state'].lower()}",
        )

    before = await get_state()
    search_submitted = False
    selected: dict[str, Any] = {}
    clicked_result = False

    async def result_failure(
        *,
        stage: str,
        error_code: str,
        error: str,
        **values: Any,
    ) -> dict[str, Any]:
        return _play_query_result(
            query,
            stage=stage,
            search_submitted=search_submitted,
            result_found=bool(selected.get("ok") and selected.get("selected_video_id")),
            selected_video_id=selected.get("selected_video_id"),
            selected_title=selected.get("selected_title", ""),
            selected_artist=selected.get("selected_artist", ""),
            selection_method=selected.get("selection_method"),
            query_match_score=selected.get("query_match_score"),
            search_method="ytm_search_url" if search_submitted else None,
            delivered=clicked_result,
            verified=False,
            verification="unavailable" if clicked_result else "not_attempted",
            degraded=clicked_result,
            before=before,
            error_code=error_code,
            error=error,
            **values,
        )

    try:
        home = await _ytm_page.evaluate(
            "() => ({ url: location.href, hasSearch: !!document.querySelector('ytmusic-search-box input') })"
        )
        if not isinstance(home, dict) or not home.get("hasSearch"):
            if not await _navigate_to_ytm() or not await ensure_ready():
                return await result_failure(
                    stage="search_ready",
                    error_code="SEARCH_NOT_READY",
                    error="YT Music search is not ready",
                )

        search_url = f"{YTM_URL}/search?q={urllib.parse.quote_plus(query)}"
        try:
            await _ytm_page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            return await result_failure(
                stage="search_submit",
                error_code="SEARCH_SUBMIT_FAILED",
                error=f"YT Music search could not be submitted: {exc}",
            )
        search_submitted = True
        await _enforce_background_window()

        try:
            await _ytm_page.wait_for_url("**/search**", timeout=8000)
        except Exception:
            # The search surface probe below is authoritative. A URL wait can
            # miss an SPA update or return immediately while an old search is
            # still rendered.
            pass

        try:
            await _ytm_page.wait_for_function(_SEARCH_RESULTS_READY_JS, arg=query, timeout=8000)
        except Exception:
            pass

        try:
            search_state = await _ytm_page.evaluate(_SEARCH_RESULTS_STATE_JS)
        except Exception as exc:
            return await result_failure(
                stage="search_results",
                error_code="SEARCH_RESULTS_NOT_LOADED",
                error=f"could not inspect YT Music search results: {exc}",
            )
        if not isinstance(search_state, dict) or not (
            search_state.get("surface_ready")
            and search_state.get("path") == "/search"
            and search_state.get("query") == query
        ):
            return await result_failure(
                stage="search_results",
                error_code="SEARCH_RESULTS_NOT_LOADED",
                error="YT Music search results surface did not load",
            )

        if not search_state.get("rows_ready"):
            try:
                await _ytm_page.wait_for_function(
                    _SEARCH_RESULTS_ROWS_READY_JS,
                    arg=query,
                    timeout=4000,
                )
            except Exception:
                pass
            try:
                search_state = await _ytm_page.evaluate(_SEARCH_RESULTS_STATE_JS)
            except Exception:
                search_state = {}

        if not isinstance(search_state, dict) or not search_state.get("rows_ready"):
            return await result_failure(
                stage="result_selection",
                error_code="NO_PLAYABLE_SEARCH_RESULT",
                error="no YT Music search result rows were available",
            )

        # YTM can expose the rows before the result anchors have their
        # delegated click handlers attached, especially after a direct search
        # navigation from an already-playing track.
        await asyncio.sleep(0.8)
        selected = await _find_playable_search_result(query=query, click=True)
        if not selected.get("ok"):
            return await result_failure(
                stage="result_selection",
                error_code=selected.get("error_code", "NO_PLAYABLE_SEARCH_RESULT"),
                error=selected.get("error", "no playable YT Music search result"),
            )
        clicked_result = bool(selected.get("clicked"))
        if not clicked_result:
            return await result_failure(
                stage="result_click",
                error_code="RESULT_CLICK_FAILED",
                error="playable YT Music result was found but not clicked",
            )

        try:
            await _ytm_page.wait_for_function(
                _PLAYER_MATCH_JS,
                arg={
                    "video_id": selected.get("selected_video_id", ""),
                    "title": selected.get("selected_title", ""),
                    "artist": selected.get("selected_artist", ""),
                },
                timeout=12000,
            )
        except Exception:
            state = await get_state()
            actual_video_id = str((state or {}).get("track_id") or "").strip()
            return await result_failure(
                stage="playback",
                error_code=(
                    "PLAYBACK_DID_NOT_START"
                    if not isinstance(state, dict) or state.get("playing") is not True
                    else "PLAYBACK_VERIFICATION_FAILED"
                ),
                error=(
                    "YT Music playback did not start"
                    if not isinstance(state, dict) or state.get("playing") is not True
                    else "selected YT Music result did not become the playing track"
                ),
                actual_video_id=actual_video_id,
                state=state,
                title=(state or {}).get("title", ""),
                artist=(state or {}).get("artist", ""),
            )

        state = await get_state()
        actual_video_id = str((state or {}).get("track_id") or "").strip()
        selected_video_id = str(selected.get("selected_video_id") or "").strip()
        if actual_video_id:
            verified = bool(
                isinstance(state, dict)
                and state.get("playing") is True
                and selected_video_id
                and actual_video_id == selected_video_id
            )
            verification = "verified" if verified else "failed"
        else:
            verified = _playback_metadata_matches(
                state,
                str(selected.get("selected_title") or ""),
                str(selected.get("selected_artist") or ""),
            )
            verification = (
                "verified_metadata"
                if verified
                else ("failed" if isinstance(state, dict) and state.get("playing") is True else "unavailable")
            )
        result = _play_query_result(
            query,
            ok=verified,
            stage="playback",
            search_submitted=True,
            result_found=True,
            selected_video_id=selected_video_id,
            selected_title=selected.get("selected_title", ""),
            selected_artist=selected.get("selected_artist", ""),
            selection_method=selected.get("selection_method"),
            query_match_score=selected.get("query_match_score"),
            search_method="ytm_search_url",
            actual_video_id=actual_video_id,
            method="dom_search_result",
            delivered=True,
            verified=verified,
            verification=verification,
            degraded=not verified and verification == "unavailable",
            before=before,
            state=state,
            player_loaded=bool(isinstance(state, dict) and state.get("player_loaded")),
            playing=(state or {}).get("playing"),
            title=(state or {}).get("title", ""),
            artist=(state or {}).get("artist", ""),
            error_code=(
                None
                if verified
                else "PLAYBACK_VERIFICATION_FAILED"
                if isinstance(state, dict) and state.get("playing") is True
                else "PLAYBACK_DID_NOT_START"
            ),
        )
        if not verified:
            result["error"] = (
                "selected YT Music result did not become the playing track"
                if verification == "failed"
                else "YT Music playback could not be verified"
            )
        return result
    except Exception as exc:
        log.warning("ytm_web.play_query failed: %s", exc)
        return await result_failure(
            stage="playback" if clicked_result else "search",
            error_code="PLAY_QUERY_EXCEPTION",
            error=str(exc),
        )


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
