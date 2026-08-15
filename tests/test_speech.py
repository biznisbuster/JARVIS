"""Tests for jarvis.audio.speech — normalize_for_speech and _take_sentence."""

from __future__ import annotations

from dataclasses import replace

import pytest

import jarvis.audio.speech as speech_mod
from jarvis.audio.speech import _take_sentence, normalize_for_speech
from jarvis.config import SETTINGS


@pytest.mark.parametrize(
    "raw, expected_substr",
    [
        ("`code`", "code"),
        ("**bold** _italic_ ~~strike~~", "bold"),
        ("[link](https://example.com)", "link"),
        ("see https://x.y/foo for more", "see for more"),
        ("- item one\n- item two", "item one"),
        ("# title\n## sub", "title"),
        ("text  with   spaces", "text with spaces"),
    ],
)
def test_normalize_for_speech_strips_markdown(raw: str, expected_substr: str) -> None:
    out = normalize_for_speech(raw)
    assert expected_substr in out
    assert "**" not in out
    assert "~~" not in out
    assert "`" not in out
    assert "](http" not in out


def test_normalize_drops_emoji_and_urls() -> None:
    out = normalize_for_speech("Hej 🎉 kako si? vidi https://example.com")
    assert "🎉" not in out
    assert "https" not in out
    assert "Hej" in out and "kako" in out


def test_normalize_removes_code_blocks() -> None:
    out = normalize_for_speech("pre ```print(1)``` post")
    assert "print" not in out
    assert "pre" in out and "post" in out


def test_normalize_keeps_punctuation_for_speech() -> None:
    out = normalize_for_speech("Zdravo, kako si?")
    assert "Zdravo" in out
    assert "?" in out


def test_take_sentence_returns_none_when_buffer_too_short() -> None:
    s, rest = _take_sentence("Hej")
    assert s is None and rest == "Hej"


def test_take_sentence_extracts_period_sentence() -> None:
    s, rest = _take_sentence("Danas je lep dan. Sutra će biti kiša.")
    assert s == "Danas je lep dan."
    assert rest.startswith("Sutra")


def test_take_sentence_handles_exclamation_and_question() -> None:
    s, _ = _take_sentence("Stani sada! Ne idi nikuda.")
    assert s == "Stani sada!"
    s, _ = _take_sentence("Kako si danas? Dobro sam.")
    assert s == "Kako si danas?"


def test_take_sentence_waits_for_more_input_when_no_complete_sentence() -> None:
    # Trailing sentence has no whitespace after punctuation yet -> keep buffering.
    s, rest = _take_sentence("OK, ajde. Gotovi smo sada.")
    assert s is None
    assert rest == "OK, ajde. Gotovi smo sada."


def test_take_sentence_pops_once_sentence_is_long_enough() -> None:
    s, rest = _take_sentence("Danas je lep dan. Jos malo.")
    assert s == "Danas je lep dan."
    assert rest == "Jos malo."


def test_take_sentence_force_cuts_very_long_buffer() -> None:
    huge = ("a" * 400) + "."
    s, rest = _take_sentence(huge)
    assert s is not None
    assert len(s) <= 350
    assert len(rest) < len(huge)


def test_take_sentence_handles_ellipsis() -> None:
    s, _ = _take_sentence("Razmišljam još uvek… Možda je greška.")
    assert s == "Razmišljam još uvek…"


@pytest.mark.asyncio
async def test_ptt_speech_is_played_by_server_and_marked_as_played(monkeypatch, tmp_path) -> None:
    path = tmp_path / "ptt-reply.wav"
    events: list[tuple[str, dict]] = []
    played: list[str] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def synthesize(_text: str) -> str:
        return str(path)

    async def play_file(audio_path) -> bool:  # noqa: ANN001
        played.append(str(audio_path))
        return True

    monkeypatch.setattr(speech_mod.BUS, "publish", publish)
    monkeypatch.setattr(speech_mod, "_synthesize_cached", synthesize)
    monkeypatch.setattr(speech_mod.player, "play_file", play_file)
    monkeypatch.setattr(
        speech_mod,
        "SETTINGS",
        replace(SETTINGS, audio=replace(SETTINGS.audio, output="ui")),
    )

    session = speech_mod._SessionSpeech("session-ptt")
    session.begin_turn("ptt")
    await session._speak("Odgovor sa PTT-a.")

    tts = next(payload for kind, payload in events if kind == "tts_speak")
    assert tts["server_played"] is True
    assert played == [str(path)]


@pytest.mark.asyncio
async def test_normal_text_speech_keeps_browser_playback_path(monkeypatch, tmp_path) -> None:
    path = tmp_path / "text-reply.wav"
    events: list[tuple[str, dict]] = []
    played: list[str] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def synthesize(_text: str) -> str:
        return str(path)

    async def play_file(audio_path) -> bool:  # noqa: ANN001
        played.append(str(audio_path))
        return True

    monkeypatch.setattr(speech_mod.BUS, "publish", publish)
    monkeypatch.setattr(speech_mod, "_synthesize_cached", synthesize)
    monkeypatch.setattr(speech_mod.player, "play_file", play_file)
    monkeypatch.setattr(
        speech_mod,
        "SETTINGS",
        replace(SETTINGS, audio=replace(SETTINGS.audio, output="ui")),
    )

    session = speech_mod._SessionSpeech("session-text")
    session.begin_turn("text")
    await session._speak("Odgovor iz browser toka.")

    tts = next(payload for kind, payload in events if kind == "tts_speak")
    assert tts["server_played"] is False
    assert played == []
