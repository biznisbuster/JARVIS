"""Tool registry and implementations.

Each tool exposes an OpenAI-compatible function schema (``openai_schema``)
and an async ``execute`` coroutine returning a string (which the agent loop
turns into a ``tool`` message). Tools that touch the system go through the
``PermissionStore`` before they actually run.

All subprocess work (osascript, open, pgrep, pbcopy/pbpaste) runs through
``asyncio.to_thread`` so the event loop — and with it LLM streaming and the
WebSocket — never blocks while a system command is in flight.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .. import state as runtime_state
from ..media import ytm_web as _ytm_web
from .kilo_bridge import run_kilo

log = logging.getLogger("jarvis.agent.tools")


@dataclass
class ToolDef:
    name: str
    description: str
    schema: dict[str, Any]
    execute: Callable[..., Awaitable[str]]


# ---- helpers ---------------------------------------------------------------


def _osascript_sync(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "osascript timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


async def _osascript(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
    return await asyncio.to_thread(_osascript_sync, script, timeout)


def _run_sync(cmd: list[str], timeout: float = 20.0, **kwargs: Any) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


async def _run(cmd: list[str], timeout: float = 20.0, **kwargs: Any) -> tuple[int, str, str]:
    return await asyncio.to_thread(_run_sync, cmd, timeout, **kwargs)


async def _check(tool: str, args: dict[str, Any]) -> bool:
    return await runtime_state.permission_store.check(tool, args)


# ---- individual tools ------------------------------------------------------


async def time_now(args: dict[str, Any]) -> str:
    now = dt.datetime.now()
    return json.dumps(
        {
            "iso": now.isoformat(timespec="seconds"),
            "human": now.strftime("%A, %d %B %Y, %H:%M"),
            "timezone": dt.datetime.now().astimezone().tzname(),
            "weekday": now.weekday(),
        },
        ensure_ascii=False,
    )


async def reminders_create(args: dict[str, Any]) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return json.dumps({"ok": False, "error": "title is required"})
    list_name = args.get("list") or "Inbox"
    due_iso = args.get("due_iso")

    props = f'name:"{title.replace(chr(34), chr(92) + chr(34))}"'
    if due_iso:
        try:
            due = dt.datetime.fromisoformat(due_iso)
            d_str = due.strftime("%m/%d/%Y %H:%M:%S")
            props += f', due date:date "{d_str}"'
        except ValueError:
            return json.dumps({"ok": False, "error": f"invalid due_iso: {due_iso}"})

    script = (
        'tell application "Reminders"\n'
        f'  if not (exists list "{list_name}") then\n'
        f'    make new list with properties {{name:"{list_name}"}}\n'
        "  end if\n"
        f'  tell list "{list_name}"\n'
        f"    make new reminder with properties {{{props}}}\n"
        "  end tell\n"
        "end tell\n"
        'return "ok"'
    )
    rc, out, err = await _osascript(script)
    ok = rc == 0
    return json.dumps({"ok": ok, "list": list_name, "title": title, "error": err or None})


async def reminders_list(args: dict[str, Any]) -> str:
    list_name = args.get("list") or "Inbox"
    limit = int(args.get("limit") or 25)
    script = (
        'tell application "Reminders"\n'
        f'  set targetList to list "{list_name}"\n'
        "  set out to {}\n"
        "  set rs to (reminders of targetList whose completed is false)\n"
        "  repeat with r in rs\n"
        '    set end of out to (name of r) & "||" & (due date of r as string)\n'
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )
    rc, out, err = await _osascript(script, timeout=15)
    if rc != 0:
        return json.dumps({"ok": False, "error": err or "Reminders unavailable", "items": []})
    items: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "||" not in line:
            continue
        title, due = line.split("||", 1)
        items.append({"title": title.strip(), "due": due.strip()})
        if len(items) >= limit:
            break
    return json.dumps(
        {"ok": True, "list": list_name, "items": items, "count": len(items)}, ensure_ascii=False
    )


async def calendar_today(args: dict[str, Any]) -> str:
    cal_name = args.get("calendar")
    script = (
        'tell application "Calendar"\n'
        "  set todayStart to current date\n"
        "  set time of todayStart to 0\n"
        "  set todayEnd to todayStart + (1 * days)\n"
        "  set out to {}\n"
        "  set cals to calendars\n"
        + (f'  set cals to {{calendar "{cal_name}"}}\n' if cal_name else "")
        + "  repeat with c in cals\n"
        "    set evs to (every event of c whose start date >= todayStart and start date < todayEnd)\n"
        "    repeat with e in evs\n"
        '      set end of out to (summary of e) & "||" & (start date of e as string) & "||" & (name of c)\n'
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )
    rc, out, err = await _osascript(script, timeout=20)
    if rc != 0:
        return json.dumps({"ok": False, "error": err or "Calendar unavailable", "events": []})
    events: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        parts = line.split("||")
        if len(parts) < 3:
            continue
        events.append({"summary": parts[0].strip(), "start": parts[1].strip(), "calendar": parts[2].strip()})
    events.sort(key=lambda e: e["start"])
    return json.dumps({"ok": True, "events": events, "count": len(events)}, ensure_ascii=False)


async def open_app(args: dict[str, Any]) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"ok": False, "error": "name is required"})
    rc, out, err = await _run(["open", "-a", name], timeout=10)
    return json.dumps({"ok": rc == 0, "app": name, "error": err or None})


_SEARCH_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# DuckDuckGo HTML endpoint-i, redom kojim se probaju. `html` je bogatiji,
# `lite` je rezervni (trivijalna struktura, ređe vraća anomaly stranicu).
_DDG_ENDPOINTS = (
    "https://html.duckduckgo.com/html/?q={q}",
    "https://lite.duckduckgo.com/lite/?q={q}",
)


class _SearchHTMLParser(HTMLParser):
    """Skuplja linkove i snippet blokove sa DDG HTML endpoint-ova.
    Tolerantan na redosled atributa i ugnježdene tagove (za razliku
    od ranijeg regex pristupa)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class") or ""
        if tag == "a" or "snippet" in cls:
            self._open.append({"tag": tag, "class": cls, "href": attrs_d.get("href") or "", "text": []})

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i]["tag"] == tag:
                item = self._open.pop(i)
                item["text"] = " ".join("".join(item["text"]).split())
                self.items.append(item)
                return

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1]["text"].append(data)


def _ddg_resolve_url(href: str) -> str:
    """DDG obavija rezulte redirect linkom (`/l/?uddg=<stvarni url>&rut=...`).
    Izvodi stvarni URL; takođe normalizuje protokol-relativne href-ove."""
    import urllib.parse

    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def _extract_ddg_results(html: str, max_results: int) -> list[dict[str, str]]:
    parser = _SearchHTMLParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        return []
    results: list[dict[str, str]] = []
    pending: dict[str, str] | None = None

    def _flush() -> None:
        nonlocal pending
        if pending is not None and len(results) < max_results:
            results.append(pending)
        pending = None

    for item in parser.items:
        cls = item["class"]
        if "result__a" in cls or "result-link" in cls:
            _flush()
            url = _ddg_resolve_url(item["href"])
            if item["text"] and url.startswith("http"):
                pending = {"title": item["text"], "snippet": "", "url": url}
        elif "snippet" in cls and pending is not None:
            pending["snippet"] = item["text"]
            _flush()
        if len(results) >= max_results:
            break
    _flush()
    return results[:max_results]


async def web_search(args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"ok": False, "error": "query is required", "results": []})
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    max_results = min(max(max_results, 1), 10)

    import httpx

    last_error = "nijedan endpoint nije vratio rezultate"
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": _SEARCH_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,sr;q=0.8"},
        ) as client:
            for endpoint in _DDG_ENDPOINTS:
                url = endpoint.format(q=_urlquote(query))
                try:
                    r = await client.get(url)
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if r.status_code != 200:
                    last_error = f"http {r.status_code}"
                    continue
                results = _extract_ddg_results(r.text, max_results)
                if results:
                    return json.dumps({"ok": True, "query": query, "results": results}, ensure_ascii=False)
                last_error = "stranica bez rezultata (moguća anomaly/blok stranica)"
    except Exception as exc:  # noqa: BLE001
        last_error = str(exc)
    return json.dumps(
        {
            "ok": False,
            "query": query,
            "error": (
                f"Web pretraga nije uspela: {last_error}. "
                "Reci korisniku da pretraga trenutno nije dostupna; "
                "odgovori iz svog znanja ako možeš, u suprotnom predloži da pokuša kasnije."
            ),
            "results": [],
        },
        ensure_ascii=False,
    )


# Persistent Chrome instance for YouTube playback: JEDAN browser, JEDAN
# context, JEDNA stranica — sve se reuse-uje između poziva da se tabovi i
# context-i ne bi gomilali. Browser ostaje otvoren posle puštanja videa da
# korisnik može da nastavi da gleda.
_youtube_pw = None
_youtube_browser = None
_youtube_context = None
_youtube_page = None


async def _ensure_youtube_page() -> Any:
    """Vrati živu Playwright stranicu; rekreiraj samo ono što je ugašeno
    (korisnik zatvori tab → nova stranica; zatvori ceo Chrome → nov browser)."""
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


async def play_youtube(args: dict[str, Any]) -> str:
    """Open Chrome, search YouTube and play the first video for a query.
    Reuses one persistent page across calls (no context/tab piling)."""
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"ok": False, "error": "query is required"})
    try:
        page = await _ensure_youtube_page()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"playwright not installed: {exc}"})

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
        return json.dumps({"ok": False, "query": query, "error": repr(exc)})


# ---- YouTube Music (macOS app) ---------------------------------------------
#
# YouTube Music je Electron aplikacija i ne izvozi AppleScript rečnik, pa
# upravljanje ide kroz "System Events" + kombinaciju aktivacije i slanja
# tasterskih prečica koje YTM podržava:
#   /        → fokus search
#   Space    → play/pause
#   N / P    → sledeća / prethodna pesma
#   ↑ / ↓    → pojačaj / smanji zvuk
#   M        → mute / unmute
# Pre svakog slanja tastera MORAMO prvo da aktiviramo YTM (inače tipke idu
# aktivnom prozoru, ne YTM-u).
#
# NOTE: YTM je podrazumevani izbor kad korisnik traži "pesmu". Za videe
# (spotove, klipove, tutoriale) agent koristi `play_youtube` (Chrome/Web).
#
# Na macOS-u se app zove "YT Music" (process i `open -a` prihvataju to ime).
# Pored toga, instalacija može biti u `/Applications` ili u `~/Applications`.
#
# Kontrola se šalje DIREKTNO u YTM proces preko Quartz `CGEventPostToPid`
# (bez activate/restore ciklusa) — YTM ne bljesne u foreground kad korisnik
# samo kaže "pauziraj" / "sledeća". Fallback: klasičan AppleScript
# activate+keystroke+restore kad Quartz ne može da nađe PID.


_YTM_APP_NAME = "YT Music"
_YTM_APP_PATHS = (
    Path("/Applications/YT Music.app"),
    Path("/Applications/YouTube Music.app"),
    Path.home() / "Applications" / "YT Music.app",
    Path.home() / "Applications" / "YouTube Music.app",
)
# YTM je na macOS-u Safari Web App: executable je generički `Web App`, a
# njegov bundle path jedinstveno identifikuje instalaciju. `pgrep -x` ne
# valja (comm je "Web App"), koristimo `pgrep -f` po celoj komandnoj liniji
# koja sadrži "YT Music.app".
_YTM_PGREP_PATTERN = "YT Music.app"

# HID key codes (US/ISO; fizičke pozicije, ne zavise od layout-a karaktera).
# kVK_ANSI_Space=49, kVK_ANSI_N=45, kVK_ANSI_P=35.
_YTM_KEY_CODES = {
    "pause": 49,
    "play": 49,
    "next": 45,
    "previous": 35,
}

_YTM_BUNDLE_ID: str | None = None
_YTM_LAST_LAUNCH_AT: float = 0.0
_YTM_LAUNCH_DEDUP_S = 5.0
_YTM_LAST_PID: int | None = None
_YTM_PID_AT: float = 0.0
_YTM_PID_TTL_S = 30.0


class _YtmPlaybackState:
    """Best-effort mirrored play/pause state. Apple-ov MediaRemote ne vidi
    YTM (Safari Web App wrapper), tako da 100% tačan signal nemamo.
    Prati naše akcije i inicijalno je ``None`` (unknown)."""

    def __init__(self) -> None:
        self._playing: bool | None = None

    def is_playing(self) -> bool | None:
        return self._playing

    def mark_playing(self) -> None:
        self._playing = True
        log.debug("ytm state: playing")

    def mark_paused(self) -> None:
        self._playing = False
        log.debug("ytm state: paused")

    def mark_unknown(self) -> None:
        if self._playing is not None:
            log.debug("ytm state: reset to unknown")
        self._playing = None


_YTM_STATE = _YtmPlaybackState()

_YTM_BUNDLE_ID: str | None = None
_YTM_LAST_LAUNCH_AT: float = 0.0
_YTM_LAUNCH_DEDUP_S = 5.0


def _ytm_app_installed() -> bool:
    return any(p.exists() for p in _YTM_APP_PATHS)


def _ytm_bundle_id_sync() -> str | None:
    for app_path in (p for p in _YTM_APP_PATHS if p.exists()):
        plist = app_path / "Contents" / "Info.plist"
        try:
            out = subprocess.run(
                ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", str(plist)],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            continue
    return None


async def _ytm_bundle_id() -> str | None:
    global _YTM_BUNDLE_ID
    if _YTM_BUNDLE_ID is None:
        bid = await asyncio.to_thread(_ytm_bundle_id_sync)
        _YTM_BUNDLE_ID = bid or ""
    return _YTM_BUNDLE_ID or None


async def _ytm_pid() -> int | None:
    """Vrati PID YTM glavnog procesa. Kešira 30s; potvrđuje sa
    `kill -0` da proces nije zamenjen novim.app launch."""
    global _YTM_LAST_PID, _YTM_PID_AT
    now = time.monotonic()
    if _YTM_LAST_PID and now - _YTM_PID_AT < _YTM_PID_TTL_S:
        rc, _, _ = await _run(["kill", "-0", str(_YTM_LAST_PID)], timeout=2)
        if rc == 0:
            return _YTM_LAST_PID
        _YTM_LAST_PID = None

    bid = await _ytm_bundle_id()
    if bid:
        script = (
            'tell application "System Events"\n'
            f'  set procs to (every process whose bundle identifier is "{bid}")\n'
            "  if (count of procs) > 0 then\n"
            "    return unix id of (item 1 of procs)\n"
            "  else\n"
            '    return ""\n'
            "  end if\n"
            "end tell"
        )
        rc, out, _ = await _osascript(script, timeout=4)
        if rc == 0:
            try:
                pid = int(out.strip())
                _YTM_LAST_PID = pid
                _YTM_PID_AT = now
                return pid
            except ValueError:
                pass

    rc, out, _ = await _run(["pgrep", "-f", _YTM_PGREP_PATTERN], timeout=3)
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                _YTM_LAST_PID = pid
                _YTM_PID_AT = now
                return pid
    return None


def _post_key_to_pid_sync(pid: int, key_code: int) -> bool:
    """Pošalji key-down + key-up DIREKTNO u proces preko Quartz
    `CGEventPostToPid` — NEMA aktiviranja YTM-a u foreground, NEMA
    bljeska fokusa. Sinkrono radi zato što Quartz API-ja su C-binding,
    brzi su; `await asyncio.to_thread` ih drži van event loop-a."""
    try:
        from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid
    except Exception:
        return False
    try:
        down = CGEventCreateKeyboardEvent(None, key_code, True)
        up = CGEventCreateKeyboardEvent(None, key_code, False)
        if down is None or up is None:
            return False
        CGEventPostToPid(pid, down)
        time.sleep(0.04)
        CGEventPostToPid(pid, up)
        return True
    except Exception:
        return False


async def _post_key_to_pid(pid: int, key_code: int) -> bool:
    return await asyncio.to_thread(_post_key_to_pid_sync, pid, key_code)


async def _ytm_is_running() -> bool:
    """Pouzdana provera da li YTM radi: System Events sa bundle id-em
    (YTM je Safari Web App, pa 'pgrep -x Web App' hvata i druge Web
    App-ove). Fallback na `pgrep -f` ako se bundle id ne može pročitati."""
    bid = await _ytm_bundle_id()
    if bid:
        script = (
            'tell application "System Events"\n'
            f'  return (count of (every process whose bundle identifier is "{bid}")) > 0\n'
            "end tell"
        )
        rc, out, _ = await _osascript(script, timeout=4)
        if rc == 0:
            running = out.strip().lower() == "true"
            log.debug("ytm_is_running: System Events → %s (bid=%s)", running, bid)
            return running
        log.debug("ytm_is_running: SE failed rc=%s, falling back to pgrep", rc)
    rc, _, _ = await _run(["pgrep", "-f", _YTM_PGREP_PATTERN], timeout=3)
    running = rc == 0
    log.debug("ytm_is_running: pgrep → %s", running)
    return running


async def _ytm_ensure_running() -> bool:
    """Start YTM ako ne radi, NE diraj fokus. Ako smo upravo pokušali
    launch, NE pozivaj `open` ponovo (dedup prozor) — samo poll-uj dok
    process ne bude vidljiv. Kad se vid prvi put, sačekaj još malo da
    Electron registruje URL handler pre nego što vratimo True."""
    global _YTM_LAST_LAUNCH_AT
    if await _ytm_is_running():
        log.info("ytm_ensure_running: already running")
        return True

    now = time.monotonic()
    if now - _YTM_LAST_LAUNCH_AT < _YTM_LAUNCH_DEDUP_S:
        log.info("ytm_ensure_running: dedup window — polling without launch")
        for _ in range(int(_YTM_LAUNCH_DEDUP_S / 0.25)):
            await asyncio.sleep(0.25)
            if await _ytm_is_running():
                log.info("ytm_ensure_running: appeared during dedup window")
                await asyncio.sleep(0.5)
                return True
        return await _ytm_is_running()

    bid = await _ytm_bundle_id()
    if bid:
        cmd = ["open", "-g", "-b", bid]
    else:
        cmd = ["open", "-ga", _YTM_APP_NAME]
    log.info("ytm_ensure_running: launching via %s", " ".join(cmd))
    rc, _, err = await _run(cmd, timeout=15)
    if rc != 0:
        log.warning("ytm_ensure_running: launch failed rc=%s err=%s", rc, err)
        return False
    _YTM_LAST_LAUNCH_AT = time.monotonic()
    for _ in range(40):
        await asyncio.sleep(0.2)
        if await _ytm_is_running():
            log.info("ytm_ensure_running: process visible after launch")
            await asyncio.sleep(0.6)
            return True
    log.warning("ytm_ensure_running: process never appeared after launch")
    return False


async def _ytm_dismiss_beforeunload() -> bool:
    """Ako YTM (Safari Web App) ima 'leave this page?' dialog (sheet na
    prozoru), automatski ga potvrdi Enterom. Radi BEZ fokusiranja YTM-a."""
    script = (
        'tell application "System Events"\n'
        "  repeat with p in processes\n"
        "    try\n"
        '      set bn to ""\n'
        "      try\n"
        "        set bn to bundle identifier of p\n"
        "      end try\n"
        '      if bn contains "Safari.WebApp" or (name of p) is "YT Music" then\n'
        "        repeat with w in windows of p\n"
        "          if (count of sheets of w) > 0 then\n"
        '            return "yes"\n'
        "          end if\n"
        "        end repeat\n"
        "      end if\n"
        "    end try\n"
        "  end repeat\n"
        '  return "no"\n'
        "end tell"
    )
    for _ in range(12):
        rc, out, _ = await _osascript(script, timeout=4)
        if rc == 0 and out.strip() == "yes":
            await _osascript('tell application "System Events" to keystroke return')
            await asyncio.sleep(0.3)
            return True
        await asyncio.sleep(0.25)
    return False


async def _ytm_open_url(url: str) -> bool:
    """Otvori URL u YT Music. Koristi `open -g -b <BID>` (g=don't bring
    to foreground) — Safari Web App inače bljesne u foreground, što
    korisniku izgleda kao "otvara mi YTM ponovo". URL ide u background,
    zatim kroz Quartz pošaljemo Space ako treba da playback krene."""
    bid = await _ytm_bundle_id()
    if bid and await _ytm_is_running():
        rc, _, err = await _run(["open", "-g", "-b", bid, url], timeout=15)
    elif bid:
        rc, _, err = await _run(["open", "-g", "-b", bid, url], timeout=15)
    else:
        rc, _, err = await _run(["open", "-ga", _YTM_APP_NAME, url], timeout=15)
    if rc != 0:
        return False
    asyncio.create_task(_ytm_dismiss_beforeunload())
    return True


async def _ytm_activate() -> bool:
    """Start YTM (in background) if not running, then bring it to the
    foreground. Koristi se za media kontrole jer nam treba fokus."""
    if not await _ytm_is_running():
        if not await _ytm_ensure_running():
            return False
    bid = await _ytm_bundle_id()
    activate_target = bid if bid else _YTM_APP_NAME
    rc4, _, _ = await _osascript(f'tell application id "{activate_target}" to activate')
    if rc4 != 0:
        rc4, _, _ = await _osascript(f'tell application "{_YTM_APP_NAME}" to activate')
    await asyncio.sleep(0.4)
    return rc4 == 0


def _ytm_escape(s: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---- MediaRemote global media keys (no focus required) --------------------
#
# Transport komande (pause/play/next/prev) žive u `jarvis/media/nowplaying.py`
# sa verifikacijom efekta i fallback lancem; keystroke fallback se registruje
# ispod posle definicije `_ytm_send_keys_quiet`.


async def _ytm_send_keystrokes(steps: list[tuple[str, float]]) -> tuple[int, str, str]:
    """Activate YTM, then run a sequence of (applescript_line, delay_s_after)
    steps inside `tell application "System Events"`. Ovo TRAJNO fokusira
    YTM — koristi se samo za ytm_play fallback."""
    bid = await _ytm_bundle_id()
    activate_line = (
        f'tell application id "{bid}" to activate'
        if bid
        else f'tell application "{_YTM_APP_NAME}" to activate'
    )
    parts = [
        activate_line,
        "delay 0.6",
        'tell application "System Events"',
    ]
    for line, d in steps:
        parts.append("  " + line)
        if d:
            parts.append(f"  delay {d}")
    parts.append("end tell")
    script = "\n".join(parts)
    return await _osascript(script, timeout=30)


async def _ytm_get_frontmost_app() -> str | None:
    """Vrati ime frontmost app-a (pre nego što fokusiramo YTM)."""
    script = (
        'tell application "System Events"\n'
        "  try\n"
        "    set frontApp to first application process whose frontmost is true\n"
        "    return name of frontApp\n"
        "  on error\n"
        '    return ""\n'
        "  end try\n"
        "end tell"
    )
    rc, out, _ = await _osascript(script, timeout=5)
    if rc == 0:
        return out.strip() or None
    return None


async def _ytm_send_keys_quiet(steps: list[tuple[str, float]]) -> bool:
    """Pošalji tastere YTM-u bez trajnog fokusa: zapamti frontmost app,
    aktiviraj YTM, pošalji tastere, vrati fokus."""
    prev_app = await _ytm_get_frontmost_app()
    bid = await _ytm_bundle_id()
    activate_line = (
        f'tell application id "{bid}" to activate'
        if bid
        else f'tell application "{_YTM_APP_NAME}" to activate'
    )
    rc, _, _ = await _osascript(activate_line)
    if rc != 0:
        return False
    await asyncio.sleep(0.2)
    parts = ['tell application "System Events"']
    for line, d in steps:
        parts.append("  " + line)
        if d:
            parts.append(f"  delay {d}")
    parts.append("end tell")
    rc2, _, _ = await _osascript("\n".join(parts), timeout=30)
    await asyncio.sleep(0.1)
    if prev_app and prev_app != _YTM_APP_NAME:
        try:
            await _osascript(f'tell application "{prev_app}" to activate')
        except Exception:
            pass
    return rc2 == 0


_YTM_KEY_FOR_ACTION = {
    "pause": 'keystroke " "',
    "play": 'keystroke " "',
    "next": 'keystroke "n"',
    "previous": 'keystroke "p"',
}

_YTM_KEY_LINE_FOR_CODE = {
    49: 'keystroke " "',
    45: 'keystroke "n"',
    35: 'keystroke "p"',
}


def _ytm_unavailable() -> str:
    return json.dumps({"ok": False, "error": f"'{_YTM_APP_NAME}' app not installed"})


def _ytm_play_result(path: str, result: dict[str, Any]) -> str:
    """Serialize and log the complete YT Music play result for diagnosis."""
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    log.info("ytm_play: path=%s result=%s", path, serialized)
    return serialized


# Pouzdano biranje prvog rezultata: scrape-ujemo YTM search HTML, izvučemo
# prvi videoId, i otvorimo `https://music.youtube.com/watch?v=ID` — YTM tamo
# uvek kreće pesmu od početka, bez obzira na prethodni queue.
_YTM_VIDEO_ID_RE = re.compile(r'videoId(?:\\x22|")\s*:\s*(?:\\x22|")([A-Za-z0-9_-]{11})(?:\\x22|")')
_YTM_WATCH_RE = re.compile(r"/watch\?v=([A-Za-z0-9_-]{11})")


async def _search_ytm_video_id(query: str) -> str | None:
    """Vraća prvi video ID iz YouTube Music search rezultata ili None."""
    import urllib.parse

    import httpx

    url = f"https://music.youtube.com/search?q={urllib.parse.quote_plus(query)}"
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,sr;q=0.8",
            },
        ) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            html = r.text
    except Exception:
        return None

    m = _YTM_VIDEO_ID_RE.search(html)
    if m:
        return m.group(1)
    m = _YTM_WATCH_RE.search(html)
    if m:
        return m.group(1)
    return None


async def ytm_play(args: dict[str, Any]) -> str:
    """Play a query in the authenticated, dedicated YTM browser session."""
    query = (args.get("query") or "").strip()

    if not query:
        status = await _ytm_web.connection_status()
        result = {
            **status,
            "ok": bool(status.get("connected")),
            "action": "opened" if status.get("connected") else "connect",
            "adapter": "ytm_web",
            "error": None if status.get("connected") else "YouTube Music connection is required",
        }
        return _ytm_play_result("ytm_web", result)

    log.info("ytm_play: query=%r", query)
    try:
        result = await _ytm_web.play_query(query)
    except Exception as exc:
        status = await _ytm_web.connection_status()
        result = {
            "ok": False,
            "query": query,
            "adapter": "ytm_web",
            "connection_state": status.get("state"),
            "page_ready": status.get("page_ready"),
            "search_ready": status.get("search_ready"),
            "player_loaded": status.get("player_loaded"),
            "playing": status.get("playing"),
            "stage": "playback",
            "search_submitted": False,
            "result_found": False,
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "degraded": False,
            "error_code": "PLAY_QUERY_EXCEPTION",
            "error": str(exc),
        }
    state = result.get("state") or result.get("after")
    if result.get("verified"):
        _ytm_update_mirrored_state(state)
    else:
        _YTM_STATE.mark_unknown()
    return _ytm_play_result("ytm_web", {"query": query, **result, "adapter": "ytm_web"})


async def _ytm_read_state_via_dom() -> dict[str, Any] | None:
    if not _ytm_web.is_available():
        return None
    try:
        return await _ytm_web.get_state()
    except Exception as exc:
        log.debug("ytm_read_state_via_dom failed: %s", exc)
        return None


async def _ytm_read_transport_state() -> dict[str, Any] | None:
    """Read transport state from the strongest currently available channel.

    Only the dedicated YT Music DOM is trusted. Generic macOS now-playing
    state may belong to JARVIS TTS, a browser tab, or another audio app, so it
    is not valid evidence for a YT Music action. The mirrored state is also
    intentionally not used as verification evidence.
    """
    web_state = await _ytm_read_state_via_dom()
    if web_state is not None and web_state.get("ok"):
        return {**web_state, "source": "ytm_web"}
    log.debug("ytm transport state unavailable; refusing generic now-playing evidence")
    return None


def _ytm_state_is_specific(state: dict[str, Any] | None) -> bool:
    """Return whether state came from the dedicated YT Music adapter."""
    return isinstance(state, dict) and state.get("ok") is True and state.get("source") == "ytm_web"


def _ytm_track_identity(state: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Return track id first, then title/artist metadata as a fallback."""
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


def _ytm_verify_transport(
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Verify the requested effect from observed state, not delivery."""
    if not _ytm_state_is_specific(after):
        return False, "unavailable"
    if action == "pause":
        if after.get("playing") is False:
            return True, "verified"
        return False, "unavailable" if after.get("playing") is None else "failed"
    if action == "play":
        if after.get("playing") is True:
            return True, "verified"
        return False, "unavailable" if after.get("playing") is None else "failed"

    before_identity = _ytm_track_identity(before)
    if not _ytm_state_is_specific(before):
        return False, "unavailable"
    after_identity = _ytm_track_identity(after)
    if before_identity is None or after_identity is None:
        return False, "unavailable"
    return (before_identity != after_identity, "verified" if before_identity != after_identity else "failed")


def _ytm_update_mirrored_state(state: dict[str, Any] | None) -> None:
    """Update the non-authoritative mirror only from observed playback state."""
    if isinstance(state, dict) and state.get("playing") is True:
        _YTM_STATE.mark_playing()
    elif isinstance(state, dict) and state.get("playing") is False:
        _YTM_STATE.mark_paused()
    else:
        _YTM_STATE.mark_unknown()


async def _ytm_post_keycode(pid: int | None, key_code: int) -> bool:
    """Šalje keycode u YTM proces. Quartz primarno, AppleScript fallback.
    Vraca True ako je event isporučen YTM-u (ne garantuje da je YTM
    reagovao kako treba — YTM ponekad ignoriše input kad UI nije
    potpuno učitan)."""
    sent = False
    if pid is not None:
        sent = await _post_key_to_pid(pid, key_code)
        if sent:
            return True
    log.warning("ytm transport: quartz fail pid=%s kc=%s, AS fallback", pid, key_code)
    fallback_line = _YTM_KEY_LINE_FOR_CODE.get(key_code, 'keystroke " "')
    return await _ytm_send_keys_quiet([(fallback_line, 0.0)])


async def _ytm_send_transport(action: str) -> dict[str, Any]:
    """Send and verify transport only through the connected YTM DOM session."""
    if action not in _YTM_KEY_CODES:
        return {"ok": False, "error": f"unknown action: {action}"}

    try:
        result = await _ytm_web.control(action)
    except Exception as exc:
        result = {
            "ok": False,
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error": str(exc),
        }
    log.info(
        "ytm transport %s → web result=%s",
        action,
        json.dumps(result, ensure_ascii=False, default=str),
    )
    if result.get("verified"):
        _ytm_update_mirrored_state(result.get("after") or result.get("state"))
    else:
        _YTM_STATE.mark_unknown()
    return {**result, "adapter": result.get("adapter", "ytm_web")}


async def _ytm_send_volume(
    action: str,
    *,
    amount: int | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    """Change and verify volume on the dedicated YT Music media element."""
    try:
        result = await _ytm_web.control_volume(action, amount=amount, level=level)
    except Exception as exc:
        result = {
            "ok": False,
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error": str(exc),
        }
    log.info(
        "ytm volume %s → web result=%s",
        action,
        json.dumps(result, ensure_ascii=False, default=str),
    )
    return {**result, "adapter": result.get("adapter", "ytm_web")}


async def ytm_pause(args: dict[str, Any]) -> str:
    return json.dumps(await _ytm_send_transport("pause"), ensure_ascii=False)


async def ytm_resume(args: dict[str, Any]) -> str:
    return json.dumps(await _ytm_send_transport("play"), ensure_ascii=False)


async def ytm_next(args: dict[str, Any]) -> str:
    return json.dumps(await _ytm_send_transport("next"), ensure_ascii=False)


async def ytm_previous(args: dict[str, Any]) -> str:
    return json.dumps(await _ytm_send_transport("previous"), ensure_ascii=False)


async def ytm_volume_up(args: dict[str, Any]) -> str:
    amount = (args or {}).get("amount", 10)
    return json.dumps(await _ytm_send_volume("volume_up", amount=amount), ensure_ascii=False)


async def ytm_volume_down(args: dict[str, Any]) -> str:
    amount = (args or {}).get("amount", 10)
    return json.dumps(await _ytm_send_volume("volume_down", amount=amount), ensure_ascii=False)


async def ytm_volume_mute(args: dict[str, Any]) -> str:
    return json.dumps(await _ytm_send_volume("volume_mute"), ensure_ascii=False)


async def ytm_volume_set(args: dict[str, Any]) -> str:
    level = (args or {}).get("level")
    return json.dumps(await _ytm_send_volume("volume_set", level=level), ensure_ascii=False)


async def ytm_status(args: dict[str, Any]) -> str:
    """Return only dedicated YTM connection and DOM player state."""
    status = await _ytm_web.connection_status()
    if not status.get("connected"):
        return json.dumps(
            {
                "ok": False,
                "source": "ytm_web",
                **status,
                "error": status.get("error") or "YouTube Music is not connected",
            },
            ensure_ascii=False,
        )

    web_state = await _ytm_web.get_state()
    if not web_state.get("ok"):
        return json.dumps(
            {
                "ok": False,
                "source": "ytm_web",
                **status,
                "error": web_state.get("error") or "YT Music state is unavailable",
            },
            ensure_ascii=False,
        )

    playing = web_state.get("playing")
    if playing is True:
        _YTM_STATE.mark_playing()
    elif playing is False:
        _YTM_STATE.mark_paused()
    return json.dumps(
        {
            "ok": True,
            "source": "ytm_web",
            **status,
            "playing": playing,
            "title": web_state.get("title", ""),
            "artist": web_state.get("artist", ""),
            "track_id": web_state.get("track_id", ""),
            "currentTime": web_state.get("currentTime", 0),
            "duration": web_state.get("duration", 0),
        },
        ensure_ascii=False,
    )


async def open_url(args: dict[str, Any]) -> str:
    """Open a URL in the default browser (or named app, e.g. 'Google Chrome')."""
    url = (args.get("url") or "").strip()
    browser = (args.get("browser") or "").strip() or None
    if not url:
        return json.dumps({"ok": False, "error": "url is required"})
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    cmd = ["open"]
    if browser:
        cmd += ["-a", browser]
    cmd.append(url)
    rc, _, err = await _run(cmd, timeout=10)
    return json.dumps({"ok": rc == 0, "url": url, "browser": browser, "error": err or None})


async def read_clipboard(args: dict[str, Any]) -> str:
    rc, out, err = await _run(["pbpaste"], timeout=5)
    return json.dumps({"ok": rc == 0, "text": out if rc == 0 else "", "error": err or None})


async def write_clipboard(args: dict[str, Any]) -> str:
    text = args.get("text") or ""

    def _do() -> int:
        proc = subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        return proc.returncode

    try:
        rc = await asyncio.to_thread(_do)
        return json.dumps({"ok": rc == 0, "length": len(text)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})


async def system_volume(args: dict[str, Any]) -> str:
    level = args.get("level")
    mute = args.get("mute")
    if isinstance(level, int) and 0 <= level <= 100:
        rc, _, err = await _osascript(f"set volume output volume {level}")
        return json.dumps({"ok": rc == 0, "level": level, "error": err or None})
    if isinstance(mute, bool):
        rc, _, err = await _osascript(f"set volume output muted {str(mute).lower()}")
        return json.dumps({"ok": rc == 0, "muted": mute, "error": err or None})
    return json.dumps({"ok": False, "error": "provide `level` (0-100) or `mute` (bool)"})


async def kilo_run_tool(args: dict[str, Any]) -> str:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return json.dumps({"ok": False, "error": "prompt is required"})
    cwd = args.get("cwd")
    max_duration = int(args.get("max_duration_s") or 180)
    return await run_kilo(prompt, cwd=cwd, timeout=max_duration)


# ---- schema + registry -----------------------------------------------------


def _schema(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


def _str_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _int_prop(desc: str, default: int | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        p["default"] = default
    return p


def _bool_prop(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _urlquote(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote_plus(s)


def build_registry() -> list[ToolDef]:
    return [
        ToolDef(
            "time_now",
            "Vrati trenutno vreme i datum.",
            _schema("time_now", "Trenutno vreme i datum.", {}, []),
            time_now,
        ),
        ToolDef(
            "reminders_create",
            "Kreiraj podsetnik u Apple Reminders.",
            _schema(
                "reminders_create",
                "Napravi novi podsetnik. `due_iso` je ISO datum (npr. 2025-12-01T10:30) — opciono.",
                {
                    "title": _str_prop("Tekst podsetnika."),
                    "list": _str_prop("Naziv liste (default 'Inbox')."),
                    "due_iso": _str_prop("Rok u ISO formatu, opciono."),
                },
                ["title"],
            ),
            reminders_create,
        ),
        ToolDef(
            "reminders_list",
            "Listaj aktivne podsetnike iz zadate liste.",
            _schema(
                "reminders_list",
                "Prikaži nezavršene podsetnike.",
                {
                    "list": _str_prop("Naziv liste (default 'Inbox')."),
                    "limit": _int_prop("Maksimum stavki.", default=25),
                },
                [],
            ),
            reminders_list,
        ),
        ToolDef(
            "calendar_today",
            "Prikaži današnje događaje iz Apple kalendara.",
            _schema(
                "calendar_today",
                "Čita današnje evente. Ako `calendar` nije zadat, pita sve kalendare.",
                {"calendar": _str_prop("Naziv kalendara (opciono).")},
                [],
            ),
            calendar_today,
        ),
        ToolDef(
            "open_app",
            "Otvori macOS aplikaciju po imenu (npr. 'Safari', 'Music').",
            _schema("open_app", "Otvori aplikaciju.", {"name": _str_prop("Ime aplikacije.")}, ["name"]),
            open_app,
        ),
        ToolDef(
            "open_url",
            "Otvori URL u podrazumevanom browseru (ili imenovanom, npr. 'Google Chrome').",
            _schema(
                "open_url",
                "Otvori URL.",
                {
                    "url": _str_prop("URL (http/https ili bez prefiksa)."),
                    "browser": _str_prop("Opciono ime aplikacije (npr. 'Google Chrome', 'Safari')."),
                },
                ["url"],
            ),
            open_url,
        ),
        ToolDef(
            "play_youtube",
            "Otvori Chrome, pretraži YouTube za `query` i pusti prvi video. Koristi za videe, klipove, tutoriale — NE za obične pesme (za to koristi ytm_play).",
            _schema(
                "play_youtube",
                "YouTube (web) reprodukcija. Za pesme koristiti ytm_play.",
                {"query": _str_prop("Naziv klipa / videa / izvođača (ne pesme).")},
                ["query"],
            ),
            play_youtube,
        ),
        ToolDef(
            "ytm_play",
            "Pretraži i pusti verifikovani rezultat u povezanoj namenskoj YT Music browser sesiji. Ako nije povezana, vrati zahtev za prijavu.",
            _schema(
                "ytm_play",
                "YouTube Music reprodukcija. Podrazumevani izbor kad korisnik traži pesmu.",
                {
                    "query": _str_prop(
                        "Naziv pesme / izvođača (opciono — bez toga samo proverava/povezuje YT Music)."
                    )
                },
                [],
            ),
            ytm_play,
        ),
        ToolDef(
            "ytm_pause",
            "Pauziraj muziku koja trenutno svira (verifikuje efekat i prijavljuje stvarno stanje).",
            _schema("ytm_pause", "Pauziraj reprodukciju.", {}, []),
            ytm_pause,
        ),
        ToolDef(
            "ytm_resume",
            "Nastavi reprodukciju muzike (verifikuje efekat i prijavljuje stvarno stanje).",
            _schema("ytm_resume", "Nastavi reprodukciju.", {}, []),
            ytm_resume,
        ),
        ToolDef(
            "ytm_next",
            "Sledeća pesma (verifikuje efekat i prijavljuje stvarno stanje).",
            _schema("ytm_next", "Sledeća pesma.", {}, []),
            ytm_next,
        ),
        ToolDef(
            "ytm_previous",
            "Prethodna pesma (verifikuje efekat i prijavljuje stvarno stanje).",
            _schema("ytm_previous", "Prethodna pesma.", {}, []),
            ytm_previous,
        ),
        ToolDef(
            "ytm_volume_up",
            "Pojačaj samo YT Music player za zadati procenat (ne menja macOS sistemski zvuk).",
            _schema(
                "ytm_volume_up",
                "Pojačaj samo YT Music player. Bez amount koristi 10%; amount je 1-100.",
                {
                    "amount": {
                        **_int_prop("Procenat povećanja, 1-100.", default=10),
                        "minimum": 1,
                        "maximum": 100,
                    }
                },
                [],
            ),
            ytm_volume_up,
        ),
        ToolDef(
            "ytm_volume_down",
            "Smanji samo YT Music player za zadati procenat (ne menja macOS sistemski zvuk).",
            _schema(
                "ytm_volume_down",
                "Smanji samo YT Music player. Bez amount koristi 10%; amount je 1-100.",
                {
                    "amount": {
                        **_int_prop("Procenat smanjenja, 1-100.", default=10),
                        "minimum": 1,
                        "maximum": 100,
                    }
                },
                [],
            ),
            ytm_volume_down,
        ),
        ToolDef(
            "ytm_volume_set",
            "Postavi samo YT Music player na procenat 0-100 (ne menja macOS sistemski zvuk).",
            _schema(
                "ytm_volume_set",
                "Postavi YT Music HTML media element na level 0-100.",
                {"level": {**_int_prop("Ciljni procenat, 0-100."), "minimum": 0, "maximum": 100}},
                ["level"],
            ),
            ytm_volume_set,
        ),
        ToolDef(
            "ytm_volume_mute",
            "Utišaj / vrati samo YT Music player (ne menja macOS sistemski mute).",
            _schema("ytm_volume_mute", "Utišaj ili vrati samo YT Music player.", {}, []),
            ytm_volume_mute,
        ),
        ToolDef(
            "ytm_status",
            "Prikaži samo stanje iz namenske YT Music browser sesije; generički macOS now-playing nije dokaz.",
            _schema("ytm_status", "Status reprodukcije.", {}, []),
            ytm_status,
        ),
        ToolDef(
            "web_search",
            "Pretraži web (DuckDuckGo) i vrati top rezultate.",
            _schema(
                "web_search",
                "Web pretraga.",
                {
                    "query": _str_prop("Upit."),
                    "max_results": _int_prop("Maks rezultata.", default=5),
                },
                ["query"],
            ),
            web_search,
        ),
        ToolDef(
            "read_clipboard",
            "Pročitaj sistemski clipboard.",
            _schema("read_clipboard", "Čita clipboard.", {}, []),
            read_clipboard,
        ),
        ToolDef(
            "write_clipboard",
            "Zapiši tekst u sistemski clipboard.",
            _schema(
                "write_clipboard",
                "Piše u clipboard.",
                {"text": _str_prop("Tekst za clipboard.")},
                ["text"],
            ),
            write_clipboard,
        ),
        ToolDef(
            "system_volume",
            "Podesi zvuk sistema (level 0-100 ili mute/unmute).",
            _schema(
                "system_volume",
                "Kontrola zvuka.",
                {
                    "level": _int_prop("0-100."),
                    "mute": _bool_prop("True = utišaj, False = uključi."),
                },
                [],
            ),
            system_volume,
        ),
        ToolDef(
            "kilo_run",
            "Pošalji kod/terminal zadatak Kilo agentu (sa strožim profilom dozvola).",
            _schema(
                "kilo_run",
                "Pokreće `kilo run --auto` sa zadatim promptom. Vraća stdout+stderr i exit code. Koristi za kod/terminal poslove.",
                {
                    "prompt": _str_prop("Detaljan opis zadatka."),
                    "cwd": _str_prop("Radni direktorijum (opciono)."),
                    "max_duration_s": _int_prop("Maks sekundi.", default=180),
                },
                ["prompt"],
            ),
            kilo_run_tool,
        ),
    ]


# Module-level singletons used by the loop.
def all_schemas() -> list[dict[str, Any]]:
    return [t.schema for t in REGISTRY]


def get(name: str) -> ToolDef | None:
    for t in REGISTRY:
        if t.name == name:
            return t
    return None


REGISTRY: list[ToolDef] = build_registry()
