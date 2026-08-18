"""Apple Calendar tools."""

from __future__ import annotations

import json
from typing import Any

from ..system.process import _osascript


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
        payload: dict[str, Any] = {
            "ok": False,
            "error": err or "Calendar unavailable",
            "events": [],
        }
        if rc == 127:
            payload["error_code"] = "DEPENDENCY_MISSING"
        return json.dumps(payload, ensure_ascii=False)
    events: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        parts = line.split("||")
        if len(parts) < 3:
            continue
        events.append(
            {
                "summary": parts[0].strip(),
                "start": parts[1].strip(),
                "calendar": parts[2].strip(),
            }
        )
    events.sort(key=lambda event: event["start"])
    return json.dumps({"ok": True, "events": events, "count": len(events)}, ensure_ascii=False)
