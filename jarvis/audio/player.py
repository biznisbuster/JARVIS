"""Play audio files through the system audio (macOS `afplay`).

Playback is serialized so concurrent sources never play over each other,
and `stop()` kills the running player immediately (voice barge-in).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..bus import BUS

_play_lock = asyncio.Lock()
_current: asyncio.subprocess.Process | None = None


async def play_file(path: str | Path) -> bool:
    global _current
    afplay = shutil.which("afplay")
    if not afplay:
        await BUS.publish("audio_play", {"ok": False, "error": "afplay not found"})
        return False
    async with _play_lock:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(afplay, str(path))
            _current = proc
            rc = await proc.wait()
            await BUS.publish("audio_play", {"ok": rc == 0, "path": str(path)})
            return rc == 0
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
            raise
        except Exception as exc:  # noqa: BLE001
            await BUS.publish("audio_play", {"ok": False, "error": repr(exc)})
            return False
        finally:
            _current = None


async def stop() -> None:
    proc = _current
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
