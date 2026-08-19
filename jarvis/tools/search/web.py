"""DuckDuckGo HTML search tool and parser helpers."""

from __future__ import annotations

import json
import urllib.parse
from html.parser import HTMLParser
from typing import Any

_SEARCH_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_DDG_ENDPOINTS = (
    "https://html.duckduckgo.com/html/?q={q}",
    "https://lite.duckduckgo.com/lite/?q={q}",
)


class _SearchHTMLParser(HTMLParser):
    """Collect result links and snippets from DDG HTML endpoints."""

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
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index]["tag"] == tag:
                item = self._open.pop(index)
                item["text"] = " ".join("".join(item["text"]).split())
                self.items.append(item)
                return

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1]["text"].append(data)


def _ddg_resolve_url(href: str) -> str:
    """Resolve DDG redirect links and protocol-relative URLs."""

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
        return json.dumps(
            {"ok": False, "error_code": "INVALID_ARGUMENTS", "error": "query is required", "results": []},
            ensure_ascii=False,
        )
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    max_results = min(max(max_results, 1), 10)

    try:
        import httpx
    except ModuleNotFoundError as exc:
        return json.dumps(
            {"ok": False, "error_code": "DEPENDENCY_MISSING", "error": str(exc), "results": []},
            ensure_ascii=False,
        )

    last_error = "nijedan endpoint nije vratio rezultate"
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": _SEARCH_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,sr;q=0.8"},
        ) as client:
            for endpoint in _DDG_ENDPOINTS:
                url = endpoint.format(q=urllib.parse.quote_plus(query))
                try:
                    response = await client.get(url)
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if response.status_code != 200:
                    last_error = f"http {response.status_code}"
                    continue
                results = _extract_ddg_results(response.text, max_results)
                if results:
                    return json.dumps(
                        {"ok": True, "query": query, "results": results},
                        ensure_ascii=False,
                    )
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
