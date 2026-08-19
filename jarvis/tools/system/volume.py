"""macOS-wide output volume tool."""

from __future__ import annotations

import json
from typing import Any

from .process import DEFAULT_PROCESS_TIMEOUT_S, _osascript, process_error_code

SYSTEM_VOLUME_TIMEOUT_S = DEFAULT_PROCESS_TIMEOUT_S


async def system_volume(args: dict[str, Any]) -> str:
    level = args.get("level")
    mute = args.get("mute")
    if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100:
        rc, _out, err = await _osascript(f"set volume output volume {level}", timeout=SYSTEM_VOLUME_TIMEOUT_S)
        payload: dict[str, Any] = {"ok": rc == 0, "level": level, "error": err or None}
        error_code = process_error_code(rc)
        if error_code is not None:
            payload["error_code"] = error_code
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(mute, bool):
        rc, _out, err = await _osascript(
            f"set volume output muted {str(mute).lower()}", timeout=SYSTEM_VOLUME_TIMEOUT_S
        )
        payload = {"ok": rc == 0, "muted": mute, "error": err or None}
        error_code = process_error_code(rc)
        if error_code is not None:
            payload["error_code"] = error_code
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(
        {
            "ok": False,
            "error_code": "INVALID_ARGUMENTS",
            "error": "provide `level` (0-100) or `mute` (bool)",
        },
        ensure_ascii=False,
    )
