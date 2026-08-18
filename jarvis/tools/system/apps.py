"""macOS application and URL launch tools."""

from __future__ import annotations

import json
from typing import Any

from .process import _run


async def open_app(args: dict[str, Any]) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"ok": False, "error": "name is required", "error_code": "INVALID_ARGUMENTS"})
    rc, _out, err = await _run(["open", "-a", name], timeout=10)
    error_code = "DEPENDENCY_MISSING" if rc == 127 else None
    payload: dict[str, Any] = {"ok": rc == 0, "app": name, "error": err or None}
    if error_code:
        payload["error_code"] = error_code
    return json.dumps(payload, ensure_ascii=False)


async def open_url(args: dict[str, Any]) -> str:
    """Open a URL in the default browser or a named application."""

    url = (args.get("url") or "").strip()
    browser = (args.get("browser") or "").strip() or None
    if not url:
        return json.dumps({"ok": False, "error": "url is required", "error_code": "INVALID_ARGUMENTS"})
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    cmd = ["open"]
    if browser:
        cmd += ["-a", browser]
    cmd.append(url)
    rc, _out, err = await _run(cmd, timeout=10)
    payload = {"ok": rc == 0, "url": url, "browser": browser, "error": err or None}
    if rc == 127:
        payload["error_code"] = "DEPENDENCY_MISSING"
    return json.dumps(payload, ensure_ascii=False)
