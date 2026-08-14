"""Regression tests for desktop YTM transport verification."""

from __future__ import annotations

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
            {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
            {"ok": True, "playing": True, "title": "After", "artist": "Artist", "track_id": "b"},
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
            {"ok": True, "playing": True, "title": "Before", "artist": "Artist", "track_id": "a"},
            {"ok": True, "playing": False, "title": "After", "artist": "Artist", "track_id": "b"},
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
            {"ok": True, "playing": before_playing, "title": "Song", "artist": "Artist", "track_id": "a"},
            {"ok": True, "playing": after_playing, "title": "Song", "artist": "Artist", "track_id": "a"},
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
