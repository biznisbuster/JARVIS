"""Focused agent-prompt routing regressions."""

from jarvis.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_NOTOOLS


def test_song_failure_does_not_cross_fallback_to_youtube_or_open_url() -> None:
    assert "NE koristi automatski play_youtube, open_url" in SYSTEM_PROMPT
    assert "bez izričitog korisnikovog zahteva za YouTube/video" in SYSTEM_PROMPT


def test_song_recovery_does_not_use_raw_video_id_or_unbounded_rewording() -> None:
    assert "ne šalji sirovi video ID kao ytm_play query" in SYSTEM_PROMPT
    assert "najviše jedan jasno opravdan ispravljen YT Music upit" in SYSTEM_PROMPT


def test_play_youtube_remains_available_for_explicit_video_requests() -> None:
    assert "video/klip/spot/tutorial ili kaže YouTube" in SYSTEM_PROMPT
    assert '"pusti klip / video / spot / tutorial / pusti na YouTube" → play_youtube' in SYSTEM_PROMPT


def test_notools_prompt_has_no_executable_tool_patterns() -> None:
    assert "ytm_play" not in SYSTEM_PROMPT_NOTOOLS
    assert "tool_call" not in SYSTEM_PROMPT_NOTOOLS
    assert '"arguments"' not in SYSTEM_PROMPT_NOTOOLS
