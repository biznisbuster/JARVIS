"""Global push-to-talk listener.

Works on macOS regardless of which app has keyboard focus — `pynput`
hooks into the system-level keyboard stream so the user can hold the
configured key while working in any other application and have their
voice transcribed straight into the active Jarvis session.

Lifecycle:

    IDLE       key not pressed
    PRESSED    key down → mute system audio (optional) + start recording
    RELEASED   key up   → stop recording → unmute → transcribe (Whisper)
                         → publish `voice_ptt_transcribed` on the bus

The frontend listens to `voice_ptt_transcribed` and either fills the input
field (default) or auto-sends the transcript as a chat message (configurable
via ``JARVIS_PTT_AUTO_SEND``).

Requires macOS Accessibility permission for the running Terminal.app /
Python process. If the OS denies the key tap, `pynput` silently swallows
events and the listener has no effect — `ptt_status` reports the
state, not the permission grant (the OS doesn't expose that).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audio.focus import FOCUS
from .bus import BUS
from .config import SETTINGS

# Key spec → pynput.Key enum. Fn is not a normal pynput key on macOS; the
# special ``fn+shift`` chord is handled by a small Quartz event tap below.
_KEY_MAP: dict[str, Any] = {}

_FN_SHIFT_PARTS = frozenset({"fn", "function"})
_SHIFT_PARTS = frozenset({"shift", "left shift", "right shift", "shift_l", "shift_r"})


def is_fn_shift_spec(spec: str) -> bool:
    """Return whether ``spec`` names the supported macOS Fn+Shift chord."""
    parts = {part.strip().lower() for part in (spec or "").split("+") if part.strip()}
    return len(parts) == 2 and bool(parts & _FN_SHIFT_PARTS) and bool(parts & _SHIFT_PARTS)


def _init_key_map() -> None:
    from pynput import keyboard

    _KEY_MAP.update(
        {
            "right cmd": keyboard.Key.cmd_r,
            "cmd_r": keyboard.Key.cmd_r,
            "left cmd": keyboard.Key.cmd,
            "cmd": keyboard.Key.cmd,
            "right shift": keyboard.Key.shift_r,
            "shift_r": keyboard.Key.shift_r,
            "right ctrl": keyboard.Key.ctrl_r,
            "ctrl_r": keyboard.Key.ctrl_r,
            "right alt": keyboard.Key.alt_r,
            "alt_r": keyboard.Key.alt_r,
            "caps lock": keyboard.Key.caps_lock,
            "caps_lock": keyboard.Key.caps_lock,
            "space": keyboard.Key.space,
            "tab": keyboard.Key.tab,
            "f20": keyboard.Key.f20,
            "f19": keyboard.Key.f19,
            "f18": keyboard.Key.f18,
            "f17": keyboard.Key.f17,
            "f16": keyboard.Key.f16,
            "f15": keyboard.Key.f15,
            "f14": keyboard.Key.f14,
            "f13": keyboard.Key.f13,
            "f12": keyboard.Key.f12,
        }
    )


def resolve_key(spec: str) -> Any:
    if not _KEY_MAP:
        _init_key_map()
    return _KEY_MAP.get((spec or "").strip().lower())


class PushToTalk:
    """Background listener for a configurable key or macOS key chord."""

    def __init__(self) -> None:
        self._enabled: bool = False
        self._recording: bool = False
        self._pressed: bool = False
        self._listener: Any = None
        self._rec_thread: threading.Thread | None = None
        self._stream: Any = None
        self._wav_path: Path | None = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._active_key: Any = None
        self._active_key_spec: str = ""
        self._last_error: str | None = None
        self._last_text: str = ""
        self._rec_count: int = 0
        self._enabled_at: float = 0.0
        self._first_press_at: float | None = None
        self._last_press_at: float | None = None
        # Event loop of the server thread. pynput callbacks run on their own
        # thread where `asyncio.get_running_loop()` does not exist, so the
        # loop is captured once in `enable()` (called from the server loop)
        # and all cross-thread scheduling goes through it.
        self._loop: asyncio.AbstractEventLoop | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "key": self._active_key_spec,
            "recording": self._recording,
            "available": self._listener_available(),
            "error": self._last_error,
            "last_transcript": self._last_text,
            "recording_count": self._rec_count,
            "enabled_at": self._enabled_at,
            "first_press_at": self._first_press_at,
            "last_press_at": self._last_press_at,
            "no_events_yet": self.no_events_yet,
            "auto_send": SETTINGS.audio.push_to_talk.auto_send,
        }

    @property
    def no_events_yet(self) -> bool:
        """True if the listener has been enabled for >60s and never saw a
        single press. Frontend uses this to surface Accessibility
        permission diagnostics — pynput silently swallows events when the
        process lacks the macOS Accessibility grant."""
        if not self._enabled or self._enabled_at <= 0:
            return False
        if self._first_press_at is not None:
            return False
        return (time.time() - self._enabled_at) > 60

    def _listener_available(self) -> bool:
        try:
            from pynput import keyboard  # noqa: F401

            if is_fn_shift_spec(self._active_key_spec or SETTINGS.audio.push_to_talk.key):
                import Quartz  # noqa: F401

            return True
        except Exception:
            return False

    def enable(self) -> dict[str, Any]:
        """Start (or restart) the global listener with the currently-configured key."""
        if not self._listener_available():
            raise RuntimeError("pynput not installed. Run `pip install pynput`.")
        if self._enabled:
            self.disable()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        cfg = SETTINGS.audio.push_to_talk
        self._active_key_spec = cfg.key
        if is_fn_shift_spec(cfg.key):
            self._active_key = None
            self._listener = self._make_fn_shift_listener()
        else:
            key = resolve_key(cfg.key)
            if key is None:
                raise RuntimeError(f"unknown PTT key: {cfg.key!r}. Supported: {sorted(_KEY_MAP.keys())}")
            from pynput import keyboard

            self._active_key = key
            self._listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
        try:
            self._listener.start()
        except Exception as exc:
            raise RuntimeError(
                f"could not start global keyboard listener (Accessibility permission?): {exc}"
            ) from exc
        self._enabled = True
        self._enabled_at = time.time()
        self._first_press_at = None
        self._last_press_at = None
        self._last_error = None
        return self.status()

    def _make_fn_shift_listener(self) -> Any:
        """Create a listen-only Quartz tap for the macOS Fn+Shift chord.

        Fn is represented by the ``SecondaryFn`` event flag rather than a
        normal key event, so ``pynput`` cannot reliably expose it through its
        regular ``on_press`` callback. The event tap observes modifier state
        only and never suppresses or changes the user's keyboard events.
        """
        if sys.platform != "darwin":
            raise RuntimeError("the fn+shift PTT chord is supported only on macOS")

        try:
            from pynput import keyboard
            from Quartz import (
                CGEventGetFlags,
                CGEventMaskBit,
                kCGEventFlagMaskSecondaryFn,
                kCGEventFlagMaskShift,
                kCGEventFlagsChanged,
            )
        except Exception as exc:
            raise RuntimeError(f"macOS Quartz support is unavailable for fn+shift PTT: {exc}") from exc

        on_state_change: Callable[[bool, bool], None] = self._on_fn_shift_state

        class FnShiftListener(keyboard.Listener):
            _EVENTS = CGEventMaskBit(kCGEventFlagsChanged)

            def _handle_message(
                self, _proxy: Any, event_type: int, event: Any, _refcon: Any, _injected: bool
            ) -> None:
                if event_type != kCGEventFlagsChanged:
                    return
                flags = CGEventGetFlags(event)
                on_state_change(
                    bool(flags & kCGEventFlagMaskSecondaryFn),
                    bool(flags & kCGEventFlagMaskShift),
                )

        return FnShiftListener()

    def disable(self) -> dict[str, Any]:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._recording:
            self._stop_recording()
        if self._pressed:
            self._schedule_focus_exit()
            self._pressed = False
        self._enabled = False
        self._enabled_at = 0.0
        return self.status()

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        """Thread-safe publish from a pynput/recording thread onto the
        server event loop. No-op if the loop is gone."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(BUS.publish(kind, payload), loop)
        except RuntimeError:
            pass

    def _schedule_focus_enter(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(FOCUS.enter("ptt"), loop)
        except RuntimeError:
            pass

    def _schedule_focus_exit(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(FOCUS.exit("ptt"), loop)
        except RuntimeError:
            pass

    def _on_press(self, key: Any) -> None:
        if key != self._active_key or self._pressed:
            return
        self._begin_press()

    def _begin_press(self) -> None:
        if self._pressed:
            return
        self._pressed = True
        now = time.time()
        if self._first_press_at is None:
            self._first_press_at = now
        self._last_press_at = now
        self._schedule_focus_enter()
        self._start_recording()
        # Reuse the frontend's existing recording_* events so the mic
        # button in the UI lights up while PTT is active.
        self._publish("ptt_recording_start", {})

    def _on_release(self, key: Any) -> None:
        if key != self._active_key or not self._pressed:
            return
        self._end_press()

    def _end_press(self) -> None:
        if not self._pressed:
            return
        self._pressed = False
        # Focus must release BEFORE transcription so the user can hear the
        # assistant's reply as soon as the transcript returns.
        self._schedule_focus_exit()
        self._stop_recording()
        self._publish("ptt_recording_end", {})

    def _on_fn_shift_state(self, fn_pressed: bool, shift_pressed: bool) -> None:
        """Translate Quartz modifier state into the normal PTT lifecycle."""
        if fn_pressed and shift_pressed:
            self._begin_press()
        else:
            self._end_press()

    def _start_recording(self) -> None:
        if self._recording:
            return
        self._recording = True
        t = threading.Thread(target=self._rec_worker, daemon=True)
        t.start()
        self._rec_thread = t

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        # The worker thread exits when the stream stops. Give it a moment,
        # then kick off transcription off-thread.
        if self._rec_thread is not None:
            self._rec_thread.join(timeout=1.5)
            self._rec_thread = None
        if self._wav_path is None or not self._wav_path.exists():
            return
        path = self._wav_path
        self._wav_path = None
        self._rec_count += 1
        # Transcription is async and must be scheduled onto the server loop
        # from this (pynput/recording) thread.
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._transcribe_and_publish(path), loop)
        except RuntimeError:
            pass

    def _rec_worker(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            self._last_error = f"sounddevice unavailable: {exc}"
            self._recording = False
            return

        cfg = SETTINGS.audio.push_to_talk
        rate = cfg.sample_rate
        channels = 1
        frames: list[bytes] = []

        with self._lock:
            self._frames = frames

        try:

            def _cb(indata: Any, _frames: int, _t: Any, _status: Any) -> None:
                if not self._recording:
                    return
                frames.append(bytes(indata))

            with sd.RawInputStream(
                samplerate=rate,
                channels=channels,
                dtype="int16",
                blocksize=4000,
                callback=_cb,
            ):
                while self._recording:
                    sd.sleep(50)
        except Exception as exc:
            self._last_error = f"mic capture failed: {exc}"
            self._recording = False
            return

        if not frames:
            return

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            with wave.open(str(path), "wb") as f:
                f.setnchannels(channels)
                f.setsampwidth(2)
                f.setframerate(rate)
                f.writeframes(b"".join(frames))
        except Exception as exc:
            self._last_error = f"wav write failed: {exc}"
            try:
                path.unlink()
            except OSError:
                pass
            return
        self._wav_path = path

    async def _transcribe_and_publish(self, wav_path: Path) -> None:
        from .audio import stt as stt_mod

        try:
            text = await stt_mod.transcribe_file(wav_path, language="sr")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"transcribe failed: {exc}"
            await BUS.publish(
                "voice_ptt_transcribed",
                {"text": "", "ok": False, "error": str(exc)},
            )
            try:
                wav_path.unlink()
            except OSError:
                pass
            return
        try:
            wav_path.unlink()
        except OSError:
            pass
        await FOCUS.wait_until_released()
        text = (text or "").strip()
        self._last_text = text
        if not text:
            await BUS.publish(
                "voice_ptt_transcribed",
                {"text": "", "ok": True, "skipped": "empty"},
            )
            return
        cfg = SETTINGS.audio.push_to_talk
        await BUS.publish(
            "voice_ptt_transcribed",
            {"text": text, "ok": True, "auto_send": cfg.auto_send},
        )


PTT = PushToTalk()
