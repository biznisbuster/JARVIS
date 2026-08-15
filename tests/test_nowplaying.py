"""Regression tests for generic media transport verification."""

from __future__ import annotations

import pytest

from jarvis.media import nowplaying


def _queued_state_reader(states: list[dict[str, object]]):
    queued = iter(states)
    last = states[-1]

    async def read_state() -> dict[str, object]:
        nonlocal last
        try:
            last = next(queued)
        except StopIteration:
            pass
        return last

    return read_state


def test_next_requires_observed_track_transition() -> None:
    before = {"ok": True, "playing": True, "title": "Song", "artist": "Artist"}
    after = {"ok": True, "playing": True, "title": "", "artist": ""}

    assert nowplaying._verified("next", before, after) is False


def test_next_accepts_changed_track_identity_even_when_paused() -> None:
    before = {"ok": True, "playing": True, "title": "Song A", "artist": "Artist"}
    after = {"ok": True, "playing": False, "title": "Song B", "artist": "Artist"}

    assert nowplaying._verified("next", before, after) is True


@pytest.mark.parametrize("action", ["next", "previous"])
async def test_non_idempotent_delivery_without_track_identity_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    async def fake_npc(args: list[str]) -> tuple[int, str, str]:
        calls.append(f"nowplaying-cli:{args[0]}")
        return 0, "", ""

    def fake_media_remote(command: int) -> bool:
        calls.append("media_remote")
        return True

    async def fake_keystroke(name: str) -> bool:
        calls.append(f"keystroke:{name}")
        return True

    monkeypatch.setattr(nowplaying, "_VERIFY_WAIT", 0)
    monkeypatch.setattr(nowplaying, "_npc", fake_npc)
    monkeypatch.setattr(nowplaying, "_media_remote_send", fake_media_remote)
    monkeypatch.setattr(nowplaying, "_keystroke_fallback", fake_keystroke)
    monkeypatch.setattr(
        nowplaying,
        "get_state",
        _queued_state_reader(
            [
                {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "", "artist": ""},
                {"ok": True, "playing": True, "title": "", "artist": ""},
            ]
        ),
    )

    result = await nowplaying.control(action)

    assert result["ok"] is False
    assert result["delivered"] is True
    assert result["verified"] is False
    assert result["verification"] == "unavailable"
    assert result["degraded"] is True
    assert result["method"] == "nowplaying-cli"
    assert calls == [f"nowplaying-cli:{action}"]


@pytest.mark.parametrize("action", ["next", "previous"])
async def test_non_idempotent_delivery_without_transition_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    async def fake_npc(args: list[str]) -> tuple[int, str, str]:
        calls.append(f"nowplaying-cli:{args[0]}")
        return 0, "", ""

    def fake_media_remote(command: int) -> bool:
        calls.append("media_remote")
        return True

    monkeypatch.setattr(nowplaying, "_VERIFY_WAIT", 0)
    monkeypatch.setattr(nowplaying, "_npc", fake_npc)
    monkeypatch.setattr(nowplaying, "_media_remote_send", fake_media_remote)
    monkeypatch.setattr(nowplaying, "_keystroke_fallback", None)
    monkeypatch.setattr(
        nowplaying,
        "get_state",
        _queued_state_reader(
            [
                {"ok": True, "playing": True, "title": "Song", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "Song", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "Song", "artist": "Artist", "track_id": "a"},
            ]
        ),
    )

    result = await nowplaying.control(action)

    assert result["ok"] is False
    assert result["delivered"] is True
    assert result["verified"] is False
    assert result["verification"] == "failed"
    assert result["degraded"] is False
    assert result["method"] == "nowplaying-cli"
    assert calls == [f"nowplaying-cli:{action}"]


async def test_non_idempotent_fallback_is_allowed_when_first_transport_did_not_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_npc(args: list[str]) -> tuple[int, str, str]:
        calls.append(f"nowplaying-cli:{args[0]}")
        return 127, "", "not delivered"

    def fake_media_remote(command: int) -> bool:
        calls.append("media_remote")
        return True

    monkeypatch.setattr(nowplaying, "_VERIFY_WAIT", 0)
    monkeypatch.setattr(nowplaying, "_npc", fake_npc)
    monkeypatch.setattr(nowplaying, "_media_remote_send", fake_media_remote)
    monkeypatch.setattr(nowplaying, "_keystroke_fallback", None)
    monkeypatch.setattr(
        nowplaying,
        "get_state",
        _queued_state_reader(
            [
                {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "After", "artist": "Artist", "track_id": "b"},
            ]
        ),
    )

    result = await nowplaying.control("next")

    assert result["ok"] is True
    assert result["delivered"] is True
    assert result["verified"] is True
    assert result["verification"] == "verified"
    assert result["method"] == "media_remote"
    assert calls == ["nowplaying-cli:next", "media_remote"]


@pytest.mark.parametrize("action", ["next", "previous"])
async def test_non_idempotent_action_can_verify_on_later_state_read(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    async def fake_npc(args: list[str]) -> tuple[int, str, str]:
        calls.append(f"nowplaying-cli:{args[0]}")
        return 0, "", ""

    monkeypatch.setattr(nowplaying, "_VERIFY_WAIT", 0)
    monkeypatch.setattr(nowplaying, "_npc", fake_npc)
    monkeypatch.setattr(nowplaying, "_media_remote_send", lambda command: False)
    monkeypatch.setattr(nowplaying, "_keystroke_fallback", None)
    monkeypatch.setattr(
        nowplaying,
        "get_state",
        _queued_state_reader(
            [
                {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
                {"ok": True, "playing": True, "title": "After", "artist": "Artist", "track_id": "b"},
            ]
        ),
    )

    result = await nowplaying.control(action)

    assert result["ok"] is True
    assert result["delivered"] is True
    assert result["verified"] is True
    assert result["verification"] == "verified"
    assert result["method"] == "nowplaying-cli"
    assert calls == [f"nowplaying-cli:{action}"]


@pytest.mark.parametrize(
    ("action", "before_playing", "intermediate_playing", "final_playing"),
    [
        ("pause", True, True, False),
        ("play", False, False, True),
    ],
)
async def test_idempotent_playback_actions_keep_retry_semantics(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    before_playing: bool,
    intermediate_playing: bool,
    final_playing: bool,
) -> None:
    calls: list[str] = []

    async def fake_npc(args: list[str]) -> tuple[int, str, str]:
        calls.append(f"nowplaying-cli:{args[0]}")
        return 0, "", ""

    monkeypatch.setattr(nowplaying, "_VERIFY_WAIT", 0)
    monkeypatch.setattr(nowplaying, "_npc", fake_npc)
    monkeypatch.setattr(nowplaying, "_media_remote_send", lambda command: False)
    monkeypatch.setattr(nowplaying, "_keystroke_fallback", None)
    monkeypatch.setattr(
        nowplaying,
        "get_state",
        _queued_state_reader(
            [
                {"ok": True, "playing": before_playing, "title": "Song", "artist": "Artist", "track_id": "a"},
                {
                    "ok": True,
                    "playing": intermediate_playing,
                    "title": "Song",
                    "artist": "Artist",
                    "track_id": "a",
                },
                {"ok": True, "playing": final_playing, "title": "Song", "artist": "Artist", "track_id": "a"},
            ]
        ),
    )

    result = await nowplaying.control(action)

    assert result["ok"] is True
    assert result["delivered"] is True
    assert result["verified"] is True
    assert result["method"] == "nowplaying-cli"
    assert calls == [f"nowplaying-cli:{action}", f"nowplaying-cli:{action}"]
