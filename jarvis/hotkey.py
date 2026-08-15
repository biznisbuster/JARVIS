"""Global push-to-talk listener.

Works on macOS regardless of which app has keyboard focus — `pynput`
hooks into the system-level keyboard stream so the user can hold the
configured key while working in any other application and have their
voice transcribed straight into the active Jarvis session.

Lifecycle:

    IDLE       key not pressed
    ARMING     key down → cancel/mute audio (according to policy)
    RECORDING  focus is ready and the microphone worker has actually started
    IDLE       key up → finish capture → restore focus → transcribe (Whisper)
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
from dataclasses import dataclass, field
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


@dataclass
class _Capture:
    utterance_id: int
    focus_reason: str
    focus_acquired: bool = False
    started_at: float = 0.0
    stop_reason: str = "released"
    error: str | None = None
    path: Path | None = None
    frames: list[bytes] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    started_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    finalized: bool = False
    thread: threading.Thread | None = None


class PushToTalk:
    """Background listener for a configurable key or macOS key chord.

    Keyboard callbacks only change small pieces of thread-safe state and
    schedule async lifecycle work on the server loop. Focus acquisition,
    capture completion and transcription never block the pynput thread.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._recording: bool = False
        self._pressed: bool = False
        self._state: str = "IDLE"
        self._awaiting_release: bool = False
        self._listener: Any = None
        self._state_lock = threading.Lock()
        self._active_key: Any = None
        self._active_key_spec: str = ""
        self._last_error: str | None = None
        self._last_skip: str | None = None
        self._last_text: str = ""
        self._rec_count: int = 0
        self._next_utterance_id: int = 0
        self._current_utterance_id: int | None = None
        self._captures: dict[int, _Capture] = {}
        self._enabled_at: float = 0.0
        self._first_press_at: float | None = None
        self._last_press_at: float | None = None
        # Event loop of the server thread. pynput callbacks run on their own
        # thread where `asyncio.get_running_loop()` does not exist, so the
        # loop is captured once in `enable()` (called from the server loop)
        # and all cross-thread scheduling goes through it.
        self._loop: asyncio.AbstractEventLoop | None = None

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = self._state
            pressed = self._pressed
        return {
            "enabled": self._enabled,
            "key": self._active_key_spec,
            "recording": state == "RECORDING" and self._recording,
            "state": state,
            "pressed": pressed,
            "available": self._listener_available(),
            "error": self._last_error,
            "last_skip": self._last_skip,
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
        self._last_skip = None
        self._last_text = ""
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
        with self._state_lock:
            was_recording = self._state == "RECORDING" and self._recording
            self._enabled = False
            self._pressed = False
            self._awaiting_release = False
            self._current_utterance_id = None
            self._recording = False
            self._state = "IDLE"
            captures = list(self._captures.values())
            for capture in captures:
                if not capture.stop_event.is_set():
                    capture.stop_reason = "disabled"
                    capture.stop_event.set()
        if was_recording:
            self._publish("ptt_recording_end", {"reason": "disabled"})
        for capture in captures:
            self._schedule(self._finish_capture(capture))
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

    def _schedule(self, coroutine: Any) -> None:
        """Schedule a coroutine from a keyboard/recorder callback thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                coroutine.close()
            except Exception:
                pass
            return
        try:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            try:
                coroutine.close()
            except Exception:
                pass

    def _on_press(self, key: Any) -> None:
        if key != self._active_key or self._pressed:
            return
        self._begin_press()

    def _begin_press(self) -> None:
        with self._state_lock:
            if self._pressed or self._awaiting_release:
                return
            self._pressed = True
            self._state = "ARMING"
            self._next_utterance_id += 1
            utterance_id = self._next_utterance_id
            self._current_utterance_id = utterance_id
            now = time.time()
            if self._first_press_at is None:
                self._first_press_at = now
            self._last_press_at = now
            self._last_error = None
            self._last_skip = None
        self._schedule(self._arm_capture(utterance_id))

    def _on_release(self, key: Any) -> None:
        if key != self._active_key or not self._pressed:
            return
        self._end_press()

    def _end_press(self) -> None:
        with self._state_lock:
            if self._awaiting_release:
                self._awaiting_release = False
                self._pressed = False
                return
            if not self._pressed:
                return
            self._pressed = False
            if self._state == "ARMING":
                self._current_utterance_id = None
                self._state = "IDLE"
                return
            if self._state != "RECORDING" or self._current_utterance_id is None:
                self._current_utterance_id = None
                self._state = "IDLE"
                return
            utterance_id = self._current_utterance_id
            capture = self._captures.get(utterance_id)
            self._current_utterance_id = None
            self._recording = False
            self._state = "IDLE"
        if capture is None:
            return
        capture.stop_reason = "released"
        capture.stop_event.set()
        self._publish("ptt_recording_end", {"utterance_id": utterance_id, "reason": "released"})
        self._schedule(self._finish_capture(capture))

    def _on_fn_shift_state(self, fn_pressed: bool, shift_pressed: bool) -> None:
        """Translate Quartz modifier state into the normal PTT lifecycle."""
        if fn_pressed and shift_pressed:
            self._begin_press()
        else:
            self._end_press()

    async def _arm_capture(self, utterance_id: int) -> None:
        cfg = SETTINGS.audio.push_to_talk
        focus_reason = f"ptt:{utterance_id}"
        focus_acquired = False
        try:
            from .audio.speech import SPEECH

            if cfg.mute_while_held:
                await FOCUS.enter(focus_reason)
                focus_acquired = True
            else:
                await SPEECH.cancel_all()

            with self._state_lock:
                still_pressed = (
                    self._enabled
                    and self._pressed
                    and self._state == "ARMING"
                    and self._current_utterance_id == utterance_id
                )
            if not still_pressed:
                if focus_acquired:
                    await FOCUS.exit(focus_reason)
                    await FOCUS.wait_until_released()
                await self._publish_skipped(utterance_id, "too_short")
                return

            capture = _Capture(
                utterance_id=utterance_id,
                focus_reason=focus_reason,
                focus_acquired=focus_acquired,
            )
            with self._state_lock:
                self._captures[utterance_id] = capture
            capture.thread = threading.Thread(
                target=self._rec_worker,
                args=(capture,),
                daemon=True,
                name=f"jarvis-ptt-{utterance_id}",
            )
            capture.thread.start()
            started = await asyncio.to_thread(capture.started_event.wait, 1.5)
            if not started:
                capture.error = "microphone capture did not start"
            if capture.error:
                capture.stop_reason = "capture_error"
                capture.stop_event.set()
                await self._finish_capture(capture)
                return

            with self._state_lock:
                still_pressed = (
                    self._enabled
                    and self._pressed
                    and self._state == "ARMING"
                    and self._current_utterance_id == utterance_id
                )
                if still_pressed:
                    capture.started_at = time.monotonic()
                    self._recording = True
                    self._state = "RECORDING"
                    self._rec_count += 1
            if not still_pressed:
                capture.stop_reason = "too_short"
                capture.stop_event.set()
                await self._finish_capture(capture)
                return

            self._publish("ptt_recording_start", {"utterance_id": utterance_id})
            asyncio.create_task(self._max_duration_watch(capture))
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"PTT arming failed: {exc}"
            with self._state_lock:
                if self._current_utterance_id == utterance_id:
                    self._current_utterance_id = None
                    self._pressed = False
                    self._recording = False
                    self._state = "IDLE"
            if focus_acquired:
                await FOCUS.exit(focus_reason)
                await FOCUS.wait_until_released()
            await BUS.publish(
                "voice_ptt_transcribed",
                {"text": "", "ok": False, "error": str(exc), "utterance_id": utterance_id},
            )

    async def _max_duration_watch(self, capture: _Capture) -> None:
        maximum = max(float(SETTINGS.audio.push_to_talk.max_duration_s), 0.1)
        await asyncio.sleep(maximum)
        with self._state_lock:
            if (
                capture.finalized
                or capture.stop_event.is_set()
                or self._current_utterance_id != capture.utterance_id
            ):
                return
            capture.stop_reason = "timeout"
            capture.stop_event.set()
            self._current_utterance_id = None
            self._recording = False
            self._state = "IDLE"
            self._awaiting_release = True
        self._publish(
            "ptt_recording_end",
            {"utterance_id": capture.utterance_id, "reason": "timeout"},
        )
        await self._finish_capture(capture)

    def _rec_worker(self, capture: _Capture) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            capture.error = f"sounddevice unavailable: {exc}"
            capture.started_event.set()
            capture.done_event.set()
            return

        cfg = SETTINGS.audio.push_to_talk
        rate = cfg.sample_rate
        channels = 1

        try:

            def _cb(indata: Any, _frames: int, _t: Any, _status: Any) -> None:
                if capture.stop_event.is_set():
                    return
                capture.frames.append(bytes(indata))

            with sd.RawInputStream(
                samplerate=rate,
                channels=channels,
                dtype="int16",
                blocksize=4000,
                callback=_cb,
            ):
                capture.started_event.set()
                while not capture.stop_event.is_set():
                    sd.sleep(50)
        except Exception as exc:
            capture.error = f"mic capture failed: {exc}"
            capture.started_event.set()
            capture.done_event.set()
            return

        if not capture.frames:
            capture.done_event.set()
            return

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            with wave.open(str(path), "wb") as f:
                f.setnchannels(channels)
                f.setsampwidth(2)
                f.setframerate(rate)
                f.writeframes(b"".join(capture.frames))
            capture.path = path
        except Exception as exc:
            capture.error = f"wav write failed: {exc}"
            try:
                path.unlink()
            except OSError:
                pass
        finally:
            capture.done_event.set()

    async def _publish_skipped(self, utterance_id: int, reason: str) -> None:
        self._last_skip = reason
        await BUS.publish(
            "voice_ptt_transcribed",
            {
                "text": "",
                "ok": True,
                "skipped": reason,
                "auto_send": False,
                "utterance_id": utterance_id,
            },
        )

    async def _finish_capture(self, capture: _Capture) -> None:
        if capture.finalized:
            return
        capture.finalized = True
        await asyncio.to_thread(capture.done_event.wait)
        if capture.focus_acquired:
            await FOCUS.exit(capture.focus_reason)
            await FOCUS.wait_until_released()

        from .audio import stt as stt_mod

        try:
            if capture.stop_reason == "timeout":
                await self._publish_skipped(capture.utterance_id, "timeout")
                return
            if capture.error:
                self._last_error = capture.error
                await BUS.publish(
                    "voice_ptt_transcribed",
                    {
                        "text": "",
                        "ok": False,
                        "error": capture.error,
                        "utterance_id": capture.utterance_id,
                    },
                )
                return
            duration = time.monotonic() - capture.started_at if capture.started_at else 0.0
            minimum = max(float(SETTINGS.audio.push_to_talk.min_duration_ms) / 1000.0, 0.0)
            if duration < minimum:
                await self._publish_skipped(capture.utterance_id, "too_short")
                return
            if capture.path is None or not capture.path.exists():
                await self._publish_skipped(capture.utterance_id, "no_audio")
                return
            energy = stt_mod.wav_has_speech_energy(capture.path)
            if energy is False:
                await self._publish_skipped(capture.utterance_id, "no_speech")
                return
            text = await stt_mod.transcribe_file(capture.path, language="sr")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"transcribe failed: {exc}"
            await BUS.publish(
                "voice_ptt_transcribed",
                {"text": "", "ok": False, "error": str(exc), "utterance_id": capture.utterance_id},
            )
            return
        finally:
            if capture.path is not None:
                try:
                    capture.path.unlink()
                except OSError:
                    pass
            with self._state_lock:
                self._captures.pop(capture.utterance_id, None)

        text = (text or "").strip()
        self._last_text = text
        if not text:
            await self._publish_skipped(capture.utterance_id, "empty")
            return
        cfg = SETTINGS.audio.push_to_talk
        await BUS.publish(
            "voice_ptt_transcribed",
            {
                "text": text,
                "ok": True,
                "auto_send": cfg.auto_send,
                "utterance_id": capture.utterance_id,
            },
        )


PTT = PushToTalk()
