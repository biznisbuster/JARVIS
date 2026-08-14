"""Regression tests for generic media transport verification."""

from __future__ import annotations

from jarvis.media import nowplaying


def test_next_requires_observed_track_transition() -> None:
    before = {"ok": True, "playing": True, "title": "Song", "artist": "Artist"}
    after = {"ok": True, "playing": True, "title": "", "artist": ""}

    assert nowplaying._verified("next", before, after) is False


def test_next_accepts_changed_track_identity_even_when_paused() -> None:
    before = {"ok": True, "playing": True, "title": "Song A", "artist": "Artist"}
    after = {"ok": True, "playing": False, "title": "Song B", "artist": "Artist"}

    assert nowplaying._verified("next", before, after) is True
