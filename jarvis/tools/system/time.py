"""Clock/date tool."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any


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
