"""Regression tests for the speech-energy and Whisper result gates."""

from __future__ import annotations

import struct
import wave

import pytest

from jarvis.audio import stt


def _pcm(value: int, samples: int = 640) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_pcm_speech_gate_rejects_silence_and_accepts_sustained_audio() -> None:
    assert stt.pcm_has_speech_energy(b"\0" * 128) is False
    assert stt.pcm_has_speech_energy(_pcm(100)) is False
    assert stt.pcm_has_speech_energy(_pcm(3000)) is True


def test_wav_speech_gate_reads_pcm_frames(tmp_path) -> None:
    path = tmp_path / "capture.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(_pcm(3000))

    assert stt.wav_has_speech_energy(path) is True


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"text": "", "segments": []}, False),
        ({"text": "hallucination", "segments": [{"text": "hallucination", "no_speech_prob": 0.95}]}, False),
        ({"text": "zdravo", "segments": [{"text": "zdravo", "no_speech_prob": 0.1}]}, True),
        ({"text": "zdravo"}, True),
    ],
)
def test_mlx_result_gate_requires_segment_speech_evidence(result: dict, expected: bool) -> None:
    assert stt._mlx_result_has_speech(result) is expected


@pytest.mark.asyncio
async def test_silent_wav_skips_model_and_publishes_no_speech(monkeypatch, tmp_path) -> None:
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(_pcm(0))

    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def model_must_not_load():
        raise AssertionError("silent audio must not load Whisper")

    monkeypatch.setattr(stt.BUS, "publish", publish)
    monkeypatch.setattr(stt, "_get_model", model_must_not_load)

    assert await stt.transcribe_file(path) == ""
    assert events == [("whisper_result", {"language": "sr", "text": "", "skipped": "no_speech"})]


@pytest.mark.asyncio
async def test_warmup_loads_model_before_silence_gate(monkeypatch) -> None:
    events: list[str] = []

    async def load_model() -> tuple[str, object]:
        events.append("load")
        return "fake", object()

    async def transcribe(_path, *, language: str) -> str:  # noqa: ARG001
        events.append("transcribe")
        return ""

    monkeypatch.setattr(stt, "_get_model", load_model)
    monkeypatch.setattr(stt, "transcribe_file", transcribe)

    await stt.warmup()

    assert events == ["load", "transcribe"]
