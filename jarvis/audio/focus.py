"""Centralised audio-focus control for listen mode.

When the user is about to speak (push-to-talk, browser mic, future wake
word) Jarvis drops the macOS system output volume to 0 so ANY audio on
the laptop stops interfering with the mic — music players, browser tabs,
notification chimes, all of it. When the user is done, we restore the
previous volume after a short cooldown so a release-then-press-again
cycle doesn't oscillate the volume.

Multiple sources can request focus concurrently (push-to-talk from the
system keyboard, the browser mic button, future wake-word listener). A
refcount of reasons keeps the underlying state transitions idempotent:
the system actually changes only on the first ``enter`` and on the final
``exit``.

No mute flag, no per-source play/pause — just system volume. Cheaper,
simpler, and works for every audio source on the machine.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import Any

from ..bus import BUS
from ..config import SETTINGS

log = logging.getLogger(__name__)


_RESTORE_COOLDOWN_S = 0.6


def _osascript_sync(script: str) -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=3
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


async def _osascript(script: str) -> str:
    return await asyncio.to_thread(_osascript_sync, script)


async def _read_output_volume() -> int | None:
    raw = await _osascript("output volume of (get volume settings)")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _set_output_volume(value: int) -> None:
    value = max(0, min(100, value))
    await _osascript(f"set volume output volume {value}")


class AudioFocusManager:
    """Async singleton coordinating listen-mode audio focus.

    The strategy is intentionally blunt: snapshot the current system
    volume, set it to 0, restore it after a short cooldown. No media-app
    pause/play dance, no mute flag, no per-source tracking — the system
    volume knob controls every audio sink on macOS.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._reasons: set[str] = set()
        self._prev_volume: int | None = None
        self._active = False
        self._last_enter: float = 0.0
        self._last_exit: float = 0.0
        self._enter_count: int = 0
        self._restore_task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "reasons": sorted(self._reasons),
            "enter_count": self._enter_count,
            "restore_pending": self._restore_task is not None and not self._restore_task.done(),
            "prev_volume": self._prev_volume,
        }

    async def enter(self, reason: str) -> dict[str, Any]:
        """Take audio focus for ``reason``. Idempotent; safe to call from
        multiple concurrent sources."""
        async with self._lock:
            is_first = not self._reasons
            self._reasons.add(reason)
            if not is_first:
                return self.status()

            self._cancel_pending_restore()

            from ..audio.speech import SPEECH

            await SPEECH.cancel_all()

            self._prev_volume = await _read_output_volume()
            await _set_output_volume(0)

            self._active = True
            self._last_enter = time.time()
            self._enter_count += 1
            snapshot = self.status()

        log.info(
            "focus: enter(%s) → volume %s → 0", reason, self._prev_volume
        )
        await BUS.publish("listen_enter", {"reason": reason, **snapshot})
        return snapshot

    async def exit(self, reason: str) -> dict[str, Any]:
        """Release audio focus for ``reason``. Volume restore is debounced
        by a short cooldown so a release-then-press-again cycle doesn't
        oscillate the system volume."""
        async with self._lock:
            self._reasons.discard(reason)
            if self._reasons or not self._active:
                return self.status()

            self._active = False
            self._last_exit = time.time()

            prev_volume = self._prev_volume
            self._prev_volume = None

            self._cancel_pending_restore()
            task = asyncio.create_task(self._restore_after(prev_volume))
            self._restore_task = task
            task.add_done_callback(self._on_restore_done)

            snapshot = self.status()

        log.info(
            "focus: exit(%s) → scheduling restore of %s in %.2fs",
            reason,
            prev_volume,
            _RESTORE_COOLDOWN_S,
        )
        await BUS.publish("listen_exit", {"reason": reason, **snapshot})
        return snapshot

    async def wait_until_released(self) -> None:
        """Wait until listen mode has released focus and restored volume.

        PTT may finish transcription before the debounced volume restore has
        completed. Waiting here keeps a fast assistant response from being
        played while the system output is still muted.
        """
        while True:
            async with self._lock:
                if self._reasons or self._active:
                    restore_task = None
                else:
                    restore_task = self._restore_task
                    if restore_task is None or restore_task.done():
                        return
            if restore_task is not None:
                try:
                    await asyncio.shield(restore_task)
                except asyncio.CancelledError:
                    if restore_task.cancelled():
                        continue
                    raise
                return
            await asyncio.sleep(0.02)

    def _on_restore_done(self, task: asyncio.Task) -> None:
        if self._restore_task is task:
            self._restore_task = None

    def _cancel_pending_restore(self) -> None:
        if self._restore_task is not None and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None

    async def _restore_after(self, prev_volume: int | None) -> None:
        try:
            await asyncio.sleep(_RESTORE_COOLDOWN_S)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._active or self._reasons:
                return
        if prev_volume is not None:
            await _set_output_volume(prev_volume)
        log.info("focus: restored volume to %s", prev_volume)


FOCUS = AudioFocusManager()
