"""Tests for jarvis.audio.speech — normalize_for_speech and _take_sentence."""

from __future__ import annotations

import pytest

from jarvis.audio.speech import _take_sentence, normalize_for_speech


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
