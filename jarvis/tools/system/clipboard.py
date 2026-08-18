"""System clipboard tools."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from .process import _run


async def read_clipboard(args: dict[str, Any]) -> str:
    rc, out, err = await _run(["pbpaste"], timeout=5)
    payload: dict[str, Any] = {"ok": rc == 0, "text": out if rc == 0 else "", "error": err or None}
    if rc == 127:
        payload["error_code"] = "DEPENDENCY_MISSING"
    return json.dumps(payload, ensure_ascii=False)


async def write_clipboard(args: dict[str, Any]) -> str:
    text = args.get("text") or ""

    def _do() -> int:
        proc = subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        return proc.returncode

    try:
        rc = await asyncio.to_thread(_do)
        payload: dict[str, Any] = {"ok": rc == 0, "length": len(text)}
        if rc == 127:
            payload["error_code"] = "DEPENDENCY_MISSING"
        return json.dumps(payload, ensure_ascii=False)
    except FileNotFoundError as exc:
        return json.dumps(
            {"ok": False, "error_code": "DEPENDENCY_MISSING", "error": str(exc)},
            ensure_ascii=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"ok": False, "error_code": "TIMEOUT", "error": "pbcopy timed out"},
            ensure_ascii=False,
        )
