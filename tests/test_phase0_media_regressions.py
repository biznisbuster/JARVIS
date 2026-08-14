"""Regression tests for YTM verification provenance and fallback policy."""

from __future__ import annotations

import json

import pytest

from jarvis.agent import tools


@pytest.fixture(autouse=True)
def _reset_mirrored_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools._YTM_STATE, "_playing", None)


@pytest.mark.parametrize("action", ["pause", "play", "next", "previous"])
async def test_ytm_transport_uses_only_web_adapter(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    desktop_calls: list[str] = []

    async def fake_control(requested: str) -> dict[str, object]:
        assert requested == action
        return {
            "ok": False,
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error": "YT Music is needs_login",
        }

    async def forbidden_desktop(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        desktop_calls.append(action)
        return True

    monkeypatch.setattr(tools._ytm_web, "control", fake_control)
    monkeypatch.setattr(tools, "_ytm_post_keycode", forbidden_desktop)

    result = await tools._ytm_send_transport(action)

    assert result["ok"] is False
    assert result["adapter"] == "ytm_web"
    assert result["delivered"] is False
    assert desktop_calls == []


async def test_ytm_play_does_not_use_desktop_deeplink_when_web_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_calls: list[str] = []

    async def fake_play_query(query: str) -> dict[str, object]:
        return {
            "ok": False,
            "query": query,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error": "YT Music is needs_login",
        }

    async def forbidden_open_url(url: str) -> bool:
        desktop_calls.append(url)
        return True

    monkeypatch.setattr(tools._ytm_web, "play_query", fake_play_query)
    monkeypatch.setattr(tools, "_ytm_open_url", forbidden_open_url)

    result = json.loads(await tools.ytm_play({"query": "Vlado Georgiev"}))

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["adapter"] == "ytm_web"
    assert desktop_calls == []


async def test_connected_ytm_play_failure_preserves_connection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connected_search_failure(query: str) -> dict[str, object]:
        return {
            "ok": False,
            "query": query,
            "adapter": "ytm_web",
            "connection_state": "CONNECTED",
            "stage": "result_selection",
            "search_submitted": True,
            "result_found": False,
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error_code": "NO_PLAYABLE_SEARCH_RESULT",
            "error": "no playable YT Music search result",
        }

    monkeypatch.setattr(tools._ytm_web, "play_query", connected_search_failure)

    result = json.loads(await tools.ytm_play({"query": "Unknown artist"}))

    assert result["ok"] is False
    assert result["connection_state"] == "CONNECTED"
    assert result["error_code"] == "NO_PLAYABLE_SEARCH_RESULT"
    assert result["stage"] == "result_selection"


async def test_missing_loaded_player_does_not_fall_back_to_desktop_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_calls: list[str] = []

    async def no_loaded_player(action: str) -> dict[str, object]:
        return {
            "ok": False,
            "action": action,
            "adapter": "ytm_web",
            "delivered": False,
            "verified": False,
            "verification": "not_attempted",
            "error": "YT Music player has no loaded track",
        }

    async def forbidden_desktop(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        desktop_calls.append("desktop")
        return True

    monkeypatch.setattr(tools._ytm_web, "control", no_loaded_player)
    monkeypatch.setattr(tools, "_ytm_post_keycode", forbidden_desktop)

    result = await tools._ytm_send_transport("next")

    assert result["ok"] is False
    assert result["delivered"] is False
    assert "no loaded track" in result["error"]
    assert desktop_calls == []


@pytest.mark.parametrize(
    ("tool_name", "action"),
    [
        ("ytm_volume_up", "volume_up"),
        ("ytm_volume_down", "volume_down"),
        ("ytm_volume_mute", "volume_mute"),
    ],
)
async def test_ytm_volume_uses_dedicated_media_element_not_system_volume(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    action: str,
) -> None:
    web_calls: list[str] = []

    async def fake_control_volume(requested: str) -> dict[str, object]:
        web_calls.append(requested)
        return {
            "ok": True,
            "action": requested,
            "adapter": "ytm_web",
            "delivered": True,
            "verified": True,
            "verification": "verified",
            "before": {"volume": 0.5, "muted": False},
            "after": {"volume": 0.6, "muted": requested == "volume_mute"},
        }

    async def forbidden_system_volume(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("YT Music volume must not call macOS system volume")

    monkeypatch.setattr(tools._ytm_web, "control_volume", fake_control_volume)
    monkeypatch.setattr(tools, "_osascript", forbidden_system_volume)

    result = json.loads(await getattr(tools, tool_name)({}))

    assert result["ok"] is True
    assert result["adapter"] == "ytm_web"
    assert result["verified"] is True
    assert web_calls == [action]


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
    monkeypatch.setattr(tools._ytm_web, "get_state", generic_state)

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


async def test_ytm_status_does_not_report_generic_audio_as_ytm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disconnected() -> dict[str, object]:
        return {
            "state": "NEEDS_LOGIN",
            "connected": False,
            "needs_login": True,
            "page_ready": True,
            "search_ready": True,
            "player_loaded": False,
            "playing": None,
            "error": None,
        }

    monkeypatch.setattr(tools._ytm_web, "connection_status", disconnected)

    result = json.loads(await tools.ytm_status({}))

    assert result["ok"] is False
    assert result["source"] == "ytm_web"
    assert "Jarvis" not in json.dumps(result, ensure_ascii=False)
