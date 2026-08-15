"""Regression tests for global push-to-talk trigger handling."""

from __future__ import annotations

from jarvis.hotkey import PushToTalk, is_fn_shift_spec


def test_fn_shift_spec_accepts_both_key_orders_and_shift_variants() -> None:
    assert is_fn_shift_spec("fn+shift")
    assert is_fn_shift_spec("shift+fn")
    assert is_fn_shift_spec("function+right shift")
    assert not is_fn_shift_spec("fn")
    assert not is_fn_shift_spec("right cmd")
    assert not is_fn_shift_spec("fn+shift+cmd")


def test_fn_shift_starts_recording_only_when_both_keys_are_pressed(monkeypatch) -> None:
    ptt = PushToTalk()
    events: list[str] = []

    monkeypatch.setattr(ptt, "_schedule_focus_enter", lambda: events.append("focus_enter"))
    monkeypatch.setattr(ptt, "_schedule_focus_exit", lambda: events.append("focus_exit"))
    monkeypatch.setattr(ptt, "_start_recording", lambda: events.append("start"))
    monkeypatch.setattr(ptt, "_stop_recording", lambda: events.append("stop"))
    monkeypatch.setattr(ptt, "_publish", lambda kind, _payload: events.append(kind))

    ptt._on_fn_shift_state(True, False)
    assert not ptt._pressed
    assert events == []

    ptt._on_fn_shift_state(True, True)
    assert ptt._pressed
    assert events == ["focus_enter", "start", "ptt_recording_start"]

    # Repeated modifier events while both keys remain down must not restart
    # recording or publish duplicate UI events.
    ptt._on_fn_shift_state(True, True)
    assert events == ["focus_enter", "start", "ptt_recording_start"]

    ptt._on_fn_shift_state(True, False)
    assert not ptt._pressed
    assert events == [
        "focus_enter",
        "start",
        "ptt_recording_start",
        "focus_exit",
        "stop",
        "ptt_recording_end",
    ]


def test_existing_single_key_lifecycle_remains_unchanged(monkeypatch) -> None:
    ptt = PushToTalk()
    key = object()
    events: list[str] = []
    ptt._active_key = key

    monkeypatch.setattr(ptt, "_schedule_focus_enter", lambda: events.append("focus_enter"))
    monkeypatch.setattr(ptt, "_schedule_focus_exit", lambda: events.append("focus_exit"))
    monkeypatch.setattr(ptt, "_start_recording", lambda: events.append("start"))
    monkeypatch.setattr(ptt, "_stop_recording", lambda: events.append("stop"))
    monkeypatch.setattr(ptt, "_publish", lambda kind, _payload: events.append(kind))

    ptt._on_press(object())
    assert events == []
    ptt._on_press(key)
    ptt._on_press(key)
    ptt._on_release(object())
    assert ptt._pressed
    ptt._on_release(key)

    assert events == [
        "focus_enter",
        "start",
        "ptt_recording_start",
        "focus_exit",
        "stop",
        "ptt_recording_end",
    ]
