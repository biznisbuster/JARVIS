"""Regression tests for desktop YTM transport verification."""

from __future__ import annotations

import json

import pytest

from jarvis.agent import tools


async def _true() -> bool:
    return True


async def _pid() -> int:
    return 123


@pytest.fixture(autouse=True)
def _reset_mirrored_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools._YTM_STATE, "_playing", None)


async def test_next_does_not_send_unconditional_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)
    states = iter(
        [
            {
                "ok": True,
                "playing": True,
                "title": "Before",
                "artist": "Artist",
                "track_id": "a",
                "source": "ytm_web",
            },
            {
                "ok": True,
                "playing": True,
                "title": "After",
                "artist": "Artist",
                "track_id": "b",
                "source": "ytm_web",
            },
        ]
    )

    async def read_state() -> dict[str, object]:
        return next(states)

    monkeypatch.setattr(tools, "_ytm_read_transport_state", read_state)

    result = await tools._ytm_send_transport("next")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["delivered"] is True
    assert result["track_changed"] is True
    assert sent == [tools._YTM_KEY_CODES["next"]]


async def test_previous_does_not_send_unconditional_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)
    states = iter(
        [
            {
                "ok": True,
                "playing": True,
                "title": "Before",
                "artist": "Artist",
                "track_id": "a",
                "source": "ytm_web",
            },
            {
                "ok": True,
                "playing": False,
                "title": "After",
                "artist": "Artist",
                "track_id": "b",
                "source": "ytm_web",
            },
        ]
    )

    async def read_state() -> dict[str, object]:
        return next(states)

    monkeypatch.setattr(tools, "_ytm_read_transport_state", read_state)

    result = await tools._ytm_send_transport("previous")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["state"] is False
    assert sent == [tools._YTM_KEY_CODES["previous"]]


async def test_delivery_without_state_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    async def no_state() -> None:
        return None

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)
    monkeypatch.setattr(tools, "_ytm_read_transport_state", no_state)

    result = await tools._ytm_send_transport("next")

    assert result["ok"] is False
    assert result["delivered"] is True
    assert result["verified"] is False
    assert result["degraded"] is True
    assert tools._YTM_STATE.is_playing() is None
    assert sent == [tools._YTM_KEY_CODES["next"]]


@pytest.mark.parametrize(
    ("action", "before_playing", "after_playing"),
    [("pause", True, False), ("play", False, True)],
)
async def test_pause_resume_remain_verified_transports(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    before_playing: bool,
    after_playing: bool,
) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    states = iter(
        [
            {
                "ok": True,
                "playing": before_playing,
                "title": "Song",
                "artist": "Artist",
                "track_id": "a",
                "source": "ytm_web",
            },
            {
                "ok": True,
                "playing": after_playing,
                "title": "Song",
                "artist": "Artist",
                "track_id": "a",
                "source": "ytm_web",
            },
        ]
    )

    async def read_state() -> dict[str, object]:
        return next(states)

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)
    monkeypatch.setattr(tools, "_ytm_read_transport_state", read_state)

    result = await tools._ytm_send_transport(action)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["state"] is after_playing
    assert sent == [tools._YTM_KEY_CODES[action]]


async def test_transport_state_refuses_generic_nowplaying_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def generic_state() -> dict[str, object]:
        return {
            "ok": True,
            "playing": True,
            "title": "Jarvis — lični AI asistent",
            "artist": "JARVIS",
            "source": "nowplaying",
        }

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools._np, "get_state", generic_state)

    assert await tools._ytm_read_transport_state() is None


def test_generic_nowplaying_cannot_verify_ytm_transition() -> None:
    before = {
        "ok": True,
        "playing": True,
        "title": "Top Gun",
        "artist": "Relja",
        "source": "nowplaying",
    }
    after = {
        "ok": True,
        "playing": True,
        "title": "Jarvis — lični AI asistent",
        "artist": "JARVIS",
        "source": "nowplaying",
    }

    assert tools._ytm_verify_transport("next", before, after) == (False, "unavailable")


@pytest.mark.parametrize("action", ["next", "previous"])
async def test_unrelated_nowplaying_transition_does_not_verify_ytm_action(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    states = iter(
        [
            {
                "ok": True,
                "playing": True,
                "title": "Top Gun",
                "artist": "Relja",
                "source": "nowplaying",
            },
            {
                "ok": True,
                "playing": True,
                "title": "Jarvis — lični AI asistent",
                "artist": "JARVIS",
                "source": "nowplaying",
            },
            {
                "ok": True,
                "playing": True,
                "title": "Jarvis — lični AI asistent",
                "artist": "JARVIS",
                "source": "nowplaying",
            },
        ]
    )

    async def read_state() -> dict[str, object]:
        return next(states)

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)
    monkeypatch.setattr(tools, "_ytm_read_transport_state", read_state)

    result = await tools._ytm_send_transport(action)

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["degraded"] is True
    assert result["track_changed"] is None
    assert sent == [tools._YTM_KEY_CODES[action]]


async def test_ytm_play_does_not_verify_existing_generic_playback_as_new_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []

    async def fake_read_state() -> dict[str, object]:
        return {
            "ok": True,
            "playing": True,
            "title": "Top Gun",
            "artist": "Relja",
            "source": "nowplaying",
        }

    async def fake_post(pid: int | None, key_code: int) -> bool:
        sent.append(key_code)
        return True

    async def fake_search(query: str) -> str:
        return "eW-X8mEvMRY"

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_ensure_running", _true)
    monkeypatch.setattr(tools, "_search_ytm_video_id", fake_search)
    monkeypatch.setattr(tools, "_ytm_open_url", lambda url: _true())
    monkeypatch.setattr(tools, "_ytm_read_transport_state", fake_read_state)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)

    result = json.loads(await tools.ytm_play({"query": "Vlado Georgiev"}))

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["degraded"] is True
    assert sent == []
