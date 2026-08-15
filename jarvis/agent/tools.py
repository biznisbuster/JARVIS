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
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .. import state as runtime_state
from ..media.models import MediaActionResult
from ..media.service import MEDIA
from .kilo_bridge import run_kilo


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


# ---- YouTube Music ---------------------------------------------------------
#
# Legacy Safari Web App/Quartz YT Music transport was removed from the tool
# layer in Phase 2. The authenticated Playwright runtime is owned by
# YtmWebAdapter and exposed through MediaService.


def _media_result_payload(result: MediaActionResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, MediaActionResult):
        return result.to_dict()
    return dict(result)


def _serialize_media_result(result: MediaActionResult | dict[str, Any]) -> str:
    return json.dumps(_media_result_payload(result), ensure_ascii=False, default=str)


async def ytm_play(args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        status = await MEDIA.connection_status()
        return json.dumps(
            {
                **status,
                "ok": bool(status.get("connected")),
                "action": "opened" if status.get("connected") else "connect",
                "error": None if status.get("connected") else "YouTube Music connection is required",
            },
            ensure_ascii=False,
        )
    result = await MEDIA.play_query(query)
    payload = _media_result_payload(result)
    payload.setdefault("query", query)
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _ytm_send_transport(action: str) -> dict[str, Any]:
    return _media_result_payload(await MEDIA.control(action))


async def _ytm_send_volume(
    action: str,
    *,
    amount: int | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    return _media_result_payload(await MEDIA.control_volume(action, amount=amount, level=level))


async def ytm_pause(args: dict[str, Any]) -> str:
    return _serialize_media_result(await MEDIA.pause())


async def ytm_resume(args: dict[str, Any]) -> str:
    return _serialize_media_result(await MEDIA.resume())


async def ytm_next(args: dict[str, Any]) -> str:
    return _serialize_media_result(await MEDIA.next())


async def ytm_previous(args: dict[str, Any]) -> str:
    return _serialize_media_result(await MEDIA.previous())


async def ytm_volume_up(args: dict[str, Any]) -> str:
    amount = (args or {}).get("amount", 10)
    return _serialize_media_result(await MEDIA.volume_up(amount))


async def ytm_volume_down(args: dict[str, Any]) -> str:
    amount = (args or {}).get("amount", 10)
    return _serialize_media_result(await MEDIA.volume_down(amount))


async def ytm_volume_mute(args: dict[str, Any]) -> str:
    return _serialize_media_result(await MEDIA.volume_mute())


async def ytm_volume_set(args: dict[str, Any]) -> str:
    level = (args or {}).get("level")
    return _serialize_media_result(await MEDIA.volume_set(level))


async def ytm_status(args: dict[str, Any]) -> str:
    return json.dumps(await MEDIA.status(), ensure_ascii=False, default=str)


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
