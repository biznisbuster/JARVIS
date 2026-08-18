"""Apple Reminders tools."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..system.process import _osascript, process_error_code


async def reminders_create(args: dict[str, Any]) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return json.dumps(
            {"ok": False, "error_code": "INVALID_ARGUMENTS", "error": "title is required"},
            ensure_ascii=False,
        )
    list_name = args.get("list") or "Inbox"
    due_iso = args.get("due_iso")

    props = f'name:"{title.replace(chr(34), chr(92) + chr(34))}"'
    if due_iso:
        try:
            due = dt.datetime.fromisoformat(due_iso)
            d_str = due.strftime("%m/%d/%Y %H:%M:%S")
            props += f', due date:date "{d_str}"'
        except ValueError:
            return json.dumps(
                {
                    "ok": False,
                    "error_code": "INVALID_ARGUMENTS",
                    "error": f"invalid due_iso: {due_iso}",
                },
                ensure_ascii=False,
            )

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
    rc, _out, err = await _osascript(script)
    payload: dict[str, Any] = {"ok": rc == 0, "list": list_name, "title": title, "error": err or None}
    error_code = process_error_code(rc)
    if error_code is not None:
        payload["error_code"] = error_code
    return json.dumps(payload, ensure_ascii=False)


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
        payload: dict[str, Any] = {
            "ok": False,
            "error": err or "Reminders unavailable",
            "items": [],
        }
        error_code = process_error_code(rc)
        if error_code is not None:
            payload["error_code"] = error_code
        return json.dumps(payload, ensure_ascii=False)
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
        {"ok": True, "list": list_name, "items": items, "count": len(items)},
        ensure_ascii=False,
    )
