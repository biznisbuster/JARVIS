"""Regression tests for global push-to-talk trigger handling."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import jarvis.hotkey as hotkey_mod
from jarvis.hotkey import PushToTalk, is_fn_shift_spec


def test_fn_shift_spec_accepts_both_key_orders_and_shift_variants() -> None:
    assert is_fn_shift_spec("fn+shift")
    assert is_fn_shift_spec("shift+fn")
    assert is_fn_shift_spec("function+right shift")
    assert not is_fn_shift_spec("fn")
    assert not is_fn_shift_spec("right cmd")
    assert not is_fn_shift_spec("fn+shift+cmd")


def test_fn_shift_arms_once_and_release_during_arming_is_safe(monkeypatch) -> None:
    ptt = PushToTalk()
    scheduled: list[object] = []

    def schedule(coroutine) -> None:
        scheduled.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(ptt, "_schedule", schedule)

    ptt._on_fn_shift_state(True, True)
    assert ptt.status()["state"] == "ARMING"
    assert ptt.status()["pressed"] is True
    assert len(scheduled) == 1

    # A release can arrive before focus acquisition or microphone startup.
    # It must cancel arming rather than publishing a recording that never ran.
    ptt._on_fn_shift_state(False, False)
    assert ptt.status()["state"] == "IDLE"
    assert ptt.status()["pressed"] is False

    # Repeated modifier notifications while the key is released do nothing.
    ptt._on_fn_shift_state(False, False)
    assert len(scheduled) == 1


def test_release_without_active_press_is_safe() -> None:
    ptt = PushToTalk()
    ptt._on_fn_shift_state(False, False)
    assert ptt.status()["state"] == "IDLE"
    assert ptt.status()["pressed"] is False


@pytest.mark.asyncio
async def test_focus_or_cancel_happens_before_recording_and_respects_mute_policy(monkeypatch) -> None:
    original_settings = hotkey_mod.SETTINGS
    events: list[str] = []

    async def enter(reason: str) -> None:
        events.append(f"focus_enter:{reason}")

    async def exit_focus(reason: str) -> None:
        events.append(f"focus_exit:{reason}")

    async def wait_until_released() -> None:
        events.append("focus_restored")

    async def cancel_all() -> None:
        events.append("speech_cancel")

    monkeypatch.setattr(hotkey_mod.FOCUS, "enter", enter)
    monkeypatch.setattr(hotkey_mod.FOCUS, "exit", exit_focus)
    monkeypatch.setattr(hotkey_mod.FOCUS, "wait_until_released", wait_until_released)
    monkeypatch.setattr("jarvis.audio.speech.SPEECH.cancel_all", cancel_all)

    def fake_rec_worker(capture) -> None:
        events.append("capture_start")
        capture.started_event.set()
        capture.stop_event.wait()
        capture.done_event.set()

    async def exercise(mute: bool) -> list[str]:
        events.clear()
        ptt = PushToTalk()
        settings = replace(
            original_settings,
            audio=replace(
                original_settings.audio,
                push_to_talk=replace(original_settings.audio.push_to_talk, mute_while_held=mute),
            ),
        )
        monkeypatch.setattr(hotkey_mod, "SETTINGS", settings)
        monkeypatch.setattr(ptt, "_rec_worker", fake_rec_worker)
        ptt._enabled = True
        ptt._pressed = True
        ptt._state = "ARMING"
        ptt._current_utterance_id = 1
        ptt._loop = asyncio.get_running_loop()

        await ptt._arm_capture(1)
        assert ptt.status()["state"] == "RECORDING"
        assert events[0] in {"speech_cancel", "focus_enter:ptt:1"}
        assert events.index("capture_start") > 0

        ptt._end_press()
        await asyncio.sleep(0.05)
        return list(events)

    muted_events = await exercise(True)
    assert muted_events.index("focus_enter:ptt:1") < muted_events.index("capture_start")
    assert "speech_cancel" not in muted_events
    assert "focus_exit:ptt:1" in muted_events
    assert "focus_restored" in muted_events

    unmuted_events = await exercise(False)
    assert unmuted_events.index("speech_cancel") < unmuted_events.index("capture_start")
    assert not any(item.startswith("focus_enter:") for item in unmuted_events)
    assert not any(item.startswith("focus_exit:") for item in unmuted_events)


def test_capture_status_exposes_state_and_skip_reason() -> None:
    ptt = PushToTalk()
    ptt._state = "ARMING"
    ptt._last_skip = "no_speech"
    status = ptt.status()
    assert status["state"] == "ARMING"
    assert status["last_skip"] == "no_speech"
