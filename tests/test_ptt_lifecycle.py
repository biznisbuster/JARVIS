"""Regression tests for bounded, serialized PTT capture completion."""

from __future__ import annotations

import asyncio
import time
import wave
from dataclasses import replace

import pytest

import jarvis.hotkey as hotkey_mod
from jarvis.audio import stt
from jarvis.hotkey import PushToTalk, _Capture


def _write_capture(path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\x10" * 640)


@pytest.mark.asyncio
async def test_too_short_capture_is_rejected_without_stt(monkeypatch, tmp_path) -> None:
    original_settings = hotkey_mod.SETTINGS
    settings = replace(
        original_settings,
        audio=replace(
            original_settings.audio,
            push_to_talk=replace(original_settings.audio.push_to_talk, min_duration_ms=1000),
        ),
    )
    monkeypatch.setattr(hotkey_mod, "SETTINGS", settings)

    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def must_not_transcribe(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("too-short captures must not reach STT")

    monkeypatch.setattr(hotkey_mod.BUS, "publish", publish)
    monkeypatch.setattr(stt, "transcribe_file", must_not_transcribe)
    monkeypatch.setattr(stt, "wav_has_speech_energy", lambda _path: True)

    path = tmp_path / "short.wav"
    _write_capture(path)
    capture = _Capture(utterance_id=1, focus_reason="ptt:1", path=path)
    capture.started_at = time.monotonic()
    capture.done_event.set()

    await PushToTalk()._finish_capture(capture)

    assert events == [
        (
            "voice_ptt_transcribed",
            {"text": "", "ok": True, "skipped": "too_short", "auto_send": False, "utterance_id": 1},
        )
    ]
    assert not path.exists()


@pytest.mark.asyncio
async def test_silent_capture_is_rejected_without_auto_send(monkeypatch, tmp_path) -> None:
    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def must_not_transcribe(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("silent captures must not reach STT")

    monkeypatch.setattr(hotkey_mod.BUS, "publish", publish)
    monkeypatch.setattr(stt, "transcribe_file", must_not_transcribe)
    monkeypatch.setattr(stt, "wav_has_speech_energy", lambda _path: False)

    path = tmp_path / "silence.wav"
    _write_capture(path)
    capture = _Capture(utterance_id=2, focus_reason="ptt:2", path=path)
    capture.started_at = time.monotonic() - 2
    capture.done_event.set()

    await PushToTalk()._finish_capture(capture)

    assert events[0][0] == "voice_ptt_transcribed"
    assert events[0][1]["skipped"] == "no_speech"
    assert events[0][1]["auto_send"] is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_finish_waits_for_worker_and_handles_two_utterances_independently(
    monkeypatch, tmp_path
) -> None:
    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def transcribe(path, *, language: str) -> str:  # noqa: ARG001
        return f"tekst-{path.stem}"

    monkeypatch.setattr(hotkey_mod.BUS, "publish", publish)
    monkeypatch.setattr(stt, "wav_has_speech_energy", lambda _path: True)
    monkeypatch.setattr(stt, "transcribe_file", transcribe)

    first_path = tmp_path / "first.wav"
    second_path = tmp_path / "second.wav"
    _write_capture(first_path)
    _write_capture(second_path)
    first = _Capture(utterance_id=11, focus_reason="ptt:11", path=first_path)
    second = _Capture(utterance_id=12, focus_reason="ptt:12", path=second_path)
    first.started_at = time.monotonic() - 2
    second.started_at = time.monotonic() - 2

    async def complete_later(capture: _Capture, delay: float) -> None:
        await asyncio.sleep(delay)
        capture.done_event.set()

    ptt = PushToTalk()
    await asyncio.gather(
        ptt._finish_capture(first),
        ptt._finish_capture(second),
        complete_later(first, 0.02),
        complete_later(second, 0.04),
    )

    transcript_events = [payload for kind, payload in events if kind == "voice_ptt_transcribed"]
    assert {payload["utterance_id"] for payload in transcript_events} == {11, 12}
    assert {payload["text"] for payload in transcript_events} == {"tekst-first", "tekst-second"}
    assert not first_path.exists()
    assert not second_path.exists()


@pytest.mark.asyncio
async def test_focus_is_restored_before_transcription_and_server_reply(monkeypatch, tmp_path) -> None:
    events: list[str] = []

    async def publish(_kind: str, _payload: dict) -> None:
        return None

    async def exit_focus(_reason: str) -> None:
        events.append("focus_exit")

    async def wait_until_released() -> None:
        events.append("focus_restored")

    async def transcribe(_path, *, language: str) -> str:  # noqa: ARG001
        events.append("transcribe")
        return "odgovor"

    monkeypatch.setattr(hotkey_mod.BUS, "publish", publish)
    monkeypatch.setattr(hotkey_mod.FOCUS, "exit", exit_focus)
    monkeypatch.setattr(hotkey_mod.FOCUS, "wait_until_released", wait_until_released)
    monkeypatch.setattr(stt, "wav_has_speech_energy", lambda _path: True)
    monkeypatch.setattr(stt, "transcribe_file", transcribe)

    path = tmp_path / "focused.wav"
    _write_capture(path)
    capture = _Capture(
        utterance_id=20,
        focus_reason="ptt:20",
        focus_acquired=True,
        path=path,
    )
    capture.started_at = time.monotonic() - 2
    capture.done_event.set()

    await PushToTalk()._finish_capture(capture)

    assert events == ["focus_exit", "focus_restored", "transcribe"]


@pytest.mark.asyncio
async def test_max_duration_stops_capture_and_requires_release(monkeypatch) -> None:
    original_settings = hotkey_mod.SETTINGS
    settings = replace(
        original_settings,
        audio=replace(
            original_settings.audio,
            push_to_talk=replace(original_settings.audio.push_to_talk, max_duration_s=0.1),
        ),
    )
    monkeypatch.setattr(hotkey_mod, "SETTINGS", settings)

    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    finished: list[_Capture] = []

    async def finish(capture: _Capture) -> None:
        finished.append(capture)

    monkeypatch.setattr(hotkey_mod.BUS, "publish", publish)
    ptt = PushToTalk()
    monkeypatch.setattr(ptt, "_finish_capture", finish)
    ptt._enabled = True
    ptt._pressed = True
    ptt._state = "RECORDING"
    ptt._recording = True
    ptt._current_utterance_id = 21
    ptt._loop = asyncio.get_running_loop()
    capture = _Capture(utterance_id=21, focus_reason="ptt:21")

    await ptt._max_duration_watch(capture)

    assert capture.stop_reason == "timeout"
    assert capture.stop_event.is_set()
    assert ptt.status()["state"] == "IDLE"
    assert ptt.status()["pressed"] is True
    assert finished == [capture]
    await asyncio.sleep(0.01)
    assert events == [("ptt_recording_end", {"utterance_id": 21, "reason": "timeout"})]
