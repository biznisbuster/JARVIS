"""Per-turn world-state context injected into the system message.

Built ONCE at the start of each turn (never per iteration) so the model
always knows the current time, what is playing, and the system volume —
instead of guessing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import subprocess

from .media.service import MEDIA

_WEEKDAYS = (
    "ponedeljak",
    "utorak",
    "sreda",
    "četvrtak",
    "petak",
    "subota",
    "nedelja",
)


def _volume_sync() -> int | None:
    try:
        proc = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return int(proc.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        pass
    return None


def _media_line(st: dict) -> str:
    if not st.get("ok"):
        return "muzika: nepoznato (YT Music status nedostupan)"
    playing = st.get("playing")
    title = st.get("title") or ""
    artist = st.get("artist") or ""
    track = f" — „{title}“ ({artist})" if title else ""
    if playing is True:
        return f"muzika: SVIRA{track}"
    if playing is False:
        return f"muzika: pauzirana/stopirana{track}"
    return f"muzika: stanje nepoznato{track}"


async def build_world_state() -> str:
    now = dt.datetime.now()
    media_task = asyncio.create_task(MEDIA.get_state())
    volume_task = asyncio.create_task(asyncio.to_thread(_volume_sync))
    media_state = await media_task
    st = media_state.to_dict()
    volume = await volume_task

    lines = [
        "STANJE SVETA (sveže u trenutku korisnikove poruke):",
        f"- Vreme: {_WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y. %H:%M')}",
        f"- {_media_line(st)}",
    ]
    if volume is not None:
        lines.append(f"- Sistemski zvuk: {volume}%")
    lines.append(
        "Ne pogađaj vreme, datum ni status muzike — osloni se na ove podatke. "
        "Ako korisnik traži akciju nad muzikom, pozovi odgovarajući tool."
    )
    return "\n".join(lines)
