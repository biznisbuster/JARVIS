"""JEDAN persistent Playwright browser za YTM kontrolu.

Dizajn: browser se digne JEDNOM (na startup-u, pozivom ``warm_up()`` ili
``ensure_ready()``), drži jedan tab na music.youtube.com i živi ceo
život procesa. State read transport NE blokira event loop — sve je
već učitano.

NE pozivati ``launch_persistent_context`` na svaki tool poziv. To je
razlog zašto je sve bilo sporo: svaki chat je triggerovao 20s timeout
na učitavanje player bara.

Kako se koristi:
- App startup:   ``asyncio.create_task(ytm_web.warm_up())`` (neblokirajuće)
- Tool poziv:    ``await ytm_web.ensure_ready()`` (brz ako je već warm)
- State read:    ``await ytm_web.get_state()`` (čita sa postojećeg taba)
- Search:        ``await ytm_web.play_query(query)``
- Transport:     koristiti Quartz keystroke (brz, ne dodiruje browser)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.ytm_web")

YTM_URL = "https://music.youtube.com"

_CHROME_USER_DATA_ROOT = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
_JARVIS_PROFILE_DIR = Path.home() / ".jarvis" / "ytm_profile"
CHROME_PROFILE_NAME = os.environ.get("JARVIS_YTM_CHROME_PROFILE", "Default").strip() or "Default"

_pw = None
_browser = None
_context = None
_ytm_page = None
_launched: bool = False
_active_profile: str | None = None
_lock: asyncio.Lock | None = None
_warmup_task: asyncio.Task | None = None
_ready: bool = False


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


def _resolve_profile() -> tuple[Path, list[str]]:
    """Odluči koji Chrome profil koristiti za Playwright.

    Podrazumevano: ``~/.jarvis/ytm_profile`` — poseban profil koji se
    digne brzo i ne zavisi od GCM/token stanja tvog Chrome-a. Sync sa
    YTM desktop i dalje radi jer se prijaviš na isti Google nalog
    (biznisbuster@gmail.com). Prvi put te pita "prijavi se na Google" —
    posle toga cookies perzistiraju.

    Ako postaviš ``JARVIS_YTM_USE_USER_PROFILE=1``, pokuša tvoj sistemski
    Chrome profil — to je sporije (token decrypt + GCM hanguje) ali
    deli profile sa tvojim Chrome-om.
    """
    use_user = os.environ.get("JARVIS_YTM_USE_USER_PROFILE", "").strip().lower() in ("1", "true", "yes")
    if use_user:
        profile_dir = _CHROME_USER_DATA_ROOT / CHROME_PROFILE_NAME
        if profile_dir.exists():
            args = [
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                f"--profile-directory={CHROME_PROFILE_NAME}",
            ]
            return _CHROME_USER_DATA_ROOT, args
    _JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return _JARVIS_PROFILE_DIR, ["--no-first-run"]


async def _launch_browser() -> bool:
    """Pokreni persistent browser. Pozvati TAČNO jednom (warm_up)."""
    global _pw, _context, _ytm_page, _launched, _active_profile, _browser
    if _launched and _safe_is_connected(_browser) and not _safe_is_closed(_ytm_page):
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

    last_exc: Exception | None = None
    for use_channel in (True, False):
        try:
            kwargs: dict[str, Any] = {
                "user_data_dir": str(user_data_dir),
                "headless": True,
                "args": args,
                "timeout": 30000,
            }
            if use_channel:
                kwargs["channel"] = "chrome"
            _context = await _pw.chromium.launch_persistent_context(**kwargs)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            log.warning("ytm_web: launch failed (channel=%s): %s", use_channel, exc)
    if _context is None:
        if user_data_dir != _JARVIS_PROFILE_DIR:
            _JARVIS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                _context = await _pw.chromium.launch_persistent_context(
                    user_data_dir=str(_JARVIS_PROFILE_DIR),
                    headless=True,
                    args=["--no-first-run"],
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
    if pages:
        _ytm_page = pages[0]
    else:
        try:
            _ytm_page = await _context.new_page()
        except Exception as exc:
            log.warning("ytm_web: new_page failed: %s", exc)
            return False

    return True


_PLAYER_READY_JS = """
() => {
  const bar = document.querySelector('ytmusic-player-bar');
  if (!bar) return false;
  if (bar.hasAttribute('inert')) return false;
  if (bar.hidden) return false;
  const video = document.querySelector('video');
  if (!video) return false;
  const title = document.querySelector('.title.ytmusic-player-bar');
  return !!(title && title.textContent && title.textContent.trim().length > 0);
}
"""


async def _ensure_page_ready() -> bool:
    """Uveri se da je page na music.youtube.com i player bar učitan.
    Vrati False ako nismo uspeli — pozivalac tada treba da preskoči
    ytm_web."""
    if not _launched:
        return False
    if _ytm_page is None or _safe_is_closed(_ytm_page):
        return False
    try:
        if "music.youtube.com" in (_ytm_page.url or ""):
            try:
                if await _ytm_page.evaluate(_PLAYER_READY_JS):
                    return True
            except Exception:
                pass
    except Exception:
        return False

    async with _get_lock():
        try:
            if "music.youtube.com" in (_ytm_page.url or ""):
                if await _ytm_page.evaluate(_PLAYER_READY_JS):
                    return True
        except Exception:
            pass
        try:
            await _ytm_page.goto(YTM_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            log.warning("ytm_web: goto failed: %s", exc)
            return False
        try:
            await _ytm_page.wait_for_function(_PLAYER_READY_JS, timeout=12000)
            return True
        except Exception:
            log.warning("ytm_web: player bar not ready (nisi ulogovan?)")
            return False


def warm_up() -> None:
    """Pokreni browser u pozadini. Sinhroni fire-and-forget. Pozvati
    na app startup. Neblokirajući, ne vraća task — ako treba da sačekaš
    ``ensure_ready()``."""
    global _warmup_task
    if _warmup_task is not None and not _warmup_task.done():
        return
    _warmup_task = asyncio.create_task(_launch_and_navigate())


async def _launch_and_navigate() -> None:
    global _ready
    try:
        if await _launch_browser():
            _ready = await _ensure_page_ready()
            log.info("ytm_web: ready=%s", _ready)
    except Exception as exc:
        log.warning("ytm_web: warm_up failed: %s", exc)


async def ensure_ready() -> bool:
    """Brz ready-check. Ako browser još nije warm, pokreni ga (ali
    samo jednom). Vrati True kad je player bar spreman."""
    global _ready
    if _ready:
        return True
    if not _launched:
        await _launch_browser()
    if not _launched:
        return False
    _ready = await _ensure_page_ready()
    return _ready


def is_available() -> bool:
    """Sinhroni ready-check (bez launch). True ako je browser već
    pokrenut i player bar učitan. NE launchuje browser."""
    return _ready and _launched and _safe_is_connected(_browser) and not _safe_is_closed(_ytm_page)


_STATE_JS = """
() => {
  const video = document.querySelector('video');
  const playPause = document.querySelector('#play-pause-button');
  const titleEl = document.querySelector('.title.ytmusic-player-bar');
  const bylineEl = document.querySelector('.byline.ytmusic-player-bar');
  if (!video) {
    return { ok: false, error: 'no video element', playing: null, title: '', artist: '' };
  }
  const ariaLabel = playPause ? (playPause.getAttribute('aria-label') || '') : '';
  const isPlaying = !video.paused && video.currentTime > 0 && video.readyState >= 2;
  const trackId = new URL(location.href).searchParams.get('v') || '';
  return {
    ok: true,
    playing: isPlaying,
    title: (titleEl && titleEl.textContent ? titleEl.textContent.trim() : ''),
    artist: (bylineEl && bylineEl.textContent ? bylineEl.textContent.trim() : ''),
    track_id: trackId,
    ariaLabel,
    currentTime: video.currentTime,
    duration: isFinite(video.duration) ? video.duration : 0,
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


async def get_state() -> dict[str, Any]:
    """Pročitaj STVARNO stanje YTM player-a. Ako browser nije spreman,
    vrati ok=False — pozivalac prelazi na fallback."""
    if not await ensure_ready():
        return {"ok": False, "error": "ytm_web not ready", "playing": None,
                "title": "", "artist": ""}
    try:
        result = await _ytm_page.evaluate(_STATE_JS)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "playing": None,
                "title": "", "artist": ""}
    if not isinstance(result, dict):
        return {"ok": False, "error": "unexpected eval", "playing": None,
                "title": "", "artist": ""}
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


async def control(action: str) -> dict[str, Any]:
    """Transport komanda (play/pause/next/previous) na web tabu.
    Verifikuje stanje posle akcije. Vraca ``{"ok", "action", "method",
    "verified", "state"}``."""
    if action not in ("play", "pause", "next", "previous"):
        return {"ok": False, "error": f"unknown action: {action}", "action": action}
    if not await ensure_ready():
        return {
            "ok": False,
            "error": "ytm_web not ready",
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
        }

    before = await get_state()
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
    """Traži i pusti prvi rezultat. Koristi postojeći tab u browseru."""
    if not query.strip():
        return {"ok": False, "error": "empty query"}
    if not await ensure_ready():
        return {"ok": False, "error": "ytm_web not ready"}

    try:
        home = await _ytm_page.evaluate(
            "() => ({ url: location.href, hasSearch: !!document.querySelector('ytmusic-search-box input') })"
        )
        if not home.get("hasSearch"):
            await _ytm_page.goto(YTM_URL, wait_until="domcontentloaded", timeout=15000)
            await _ytm_page.wait_for_function(_PLAYER_READY_JS, timeout=12000)

        r = await _ytm_page.evaluate(_SEARCH_INPUT_JS, query)
        if not (r and r.get("ok")):
            return {"ok": False, "error": (r or {}).get("error", "search input failed"),
                    "query": query}

        await _ytm_page.keyboard.press("Enter")
        await _ytm_page.wait_for_url("**/search**", timeout=8000)
        await _ytm_page.wait_for_selector(
            "ytmusic-responsive-list-item-renderer, ytmusic-card-shelf-renderer, ytmusic-shelf-renderer, ytmusic-two-column-browse-results-renderer",
            timeout=12000,
        )

        for sel in [
            "ytmusic-responsive-list-item-renderer a",
            "ytmusic-card-shelf-renderer a",
            "ytmusic-shelf-renderer a",
        ]:
            loc = _ytm_page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=3000)
                await loc.click()
                break
            except Exception:
                continue
        else:
            return {"ok": False, "error": "no clickable search results", "query": query}

        try:
            await _ytm_page.wait_for_function(
                "() => { const v = document.querySelector('video'); return v && !v.paused && v.currentTime > 0; }",
                timeout=12000,
            )
        except Exception:
            state = await get_state()
            return {"ok": False, "error": "player did not start in time",
                    "query": query, "state": state}

        state = await get_state()
        return {"ok": True, "query": query, "state": state}
    except Exception as exc:
        log.warning("ytm_web.play_query failed: %s", exc)
        return {"ok": False, "error": str(exc), "query": query}


async def shutdown() -> None:
    global _pw, _context, _ytm_page, _browser, _launched, _ready, _active_profile
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
    _ready = False
    _active_profile = None


def active_profile() -> str | None:
    return _active_profile
