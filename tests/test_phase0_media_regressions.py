"""Regression tests for the authoritative YT Music service boundary."""

from __future__ import annotations

import json

import pytest

from jarvis.agent import tools


@pytest.mark.parametrize("action", ["pause", "play", "next", "previous"])
async def test_ytm_transport_uses_media_service(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    class FakeMedia:
        async def control(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            return {
                "ok": False,
                "action": requested,
                "adapter": "ytm_web",
                "delivered": False,
                "verified": False,
                "verification": "not_attempted",
                "error": "YT Music is needs_login",
            }

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())

    result = await tools._ytm_send_transport(action)

    assert result["ok"] is False
    assert result["adapter"] == "ytm_web"
    assert result["delivered"] is False
    assert calls == [action]


@pytest.mark.parametrize(
    ("tool_name", "method"),
    [
        ("ytm_pause", "pause"),
        ("ytm_resume", "resume"),
        ("ytm_next", "next"),
        ("ytm_previous", "previous"),
    ],
)
async def test_public_ytm_transport_tools_use_media_service(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    method: str,
) -> None:
    calls: list[str] = []

    class FakeMedia:
        pass

    async def transport() -> dict[str, object]:
        calls.append(method)
        return {
            "ok": True,
            "action": method,
            "adapter": "ytm_web",
            "delivered": True,
            "verified": True,
            "verification": "verified",
        }

    fake_media = FakeMedia()
    setattr(fake_media, method, transport)
    monkeypatch.setattr(tools, "MEDIA", fake_media)

    result = json.loads(await getattr(tools, tool_name)({}))

    assert result["ok"] is True
    assert result["verified"] is True
    assert calls == [method]


async def test_ytm_play_does_not_use_an_automatic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeMedia:
        async def play_query(self, query: str) -> dict[str, object]:
            calls.append(query)
            return {
                "ok": False,
                "query": query,
                "adapter": "ytm_web",
                "delivered": False,
                "verified": False,
                "verification": "not_attempted",
                "error": "YT Music is needs_login",
            }

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())

    result = json.loads(await tools.ytm_play({"query": "Vlado Georgiev"}))

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["adapter"] == "ytm_web"
    assert calls == ["Vlado Georgiev"]


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

    class FakeMedia:
        async def play_query(self, query: str) -> dict[str, object]:
            return await connected_search_failure(query)

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())

    result = json.loads(await tools.ytm_play({"query": "Unknown artist"}))

    assert result["ok"] is False
    assert result["connection_state"] == "CONNECTED"
    assert result["error_code"] == "NO_PLAYABLE_SEARCH_RESULT"
    assert result["stage"] == "result_selection"


async def test_missing_loaded_player_does_not_fall_back_to_another_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMedia:
        async def control(self, action: str) -> dict[str, object]:
            return {
                "ok": False,
                "action": action,
                "adapter": "ytm_web",
                "delivered": False,
                "verified": False,
                "verification": "not_attempted",
                "error": "YT Music player has no loaded track",
            }

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())

    result = await tools._ytm_send_transport("next")

    assert result["ok"] is False
    assert result["delivered"] is False
    assert "no loaded track" in result["error"]


@pytest.mark.parametrize(
    ("tool_name", "action", "args"),
    [
        ("ytm_volume_up", "volume_up", {}),
        ("ytm_volume_down", "volume_down", {"amount": 25}),
        ("ytm_volume_mute", "volume_mute", {}),
        ("ytm_volume_set", "volume_set", {"level": 30}),
    ],
)
async def test_ytm_volume_routes_through_media_service(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    action: str,
    args: dict[str, int],
) -> None:
    service_calls: list[tuple[str, int | None]] = []

    class FakeMedia:
        async def volume_up(self, amount: int | None) -> dict[str, object]:
            service_calls.append(("volume_up", amount))
            return {"ok": True, "adapter": "ytm_web", "delivered": True, "verified": True}

        async def volume_down(self, amount: int | None) -> dict[str, object]:
            service_calls.append(("volume_down", amount))
            return {"ok": True, "adapter": "ytm_web", "delivered": True, "verified": True}

        async def volume_mute(self) -> dict[str, object]:
            service_calls.append(("volume_mute", None))
            return {"ok": True, "adapter": "ytm_web", "delivered": True, "verified": True}

        async def volume_set(self, level: int | None) -> dict[str, object]:
            service_calls.append(("volume_set", level))
            return {"ok": True, "adapter": "ytm_web", "delivered": True, "verified": True}

    async def forbidden_system_volume(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("YT Music volume must not call macOS system volume")

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())
    monkeypatch.setattr(tools, "_osascript", forbidden_system_volume)

    result = json.loads(await getattr(tools, tool_name)(args))

    assert result["ok"] is True
    assert result["adapter"] == "ytm_web"
    assert result["verified"] is True
    expected_amount = args.get("amount", 10) if action in ("volume_up", "volume_down") else None
    expected_value = args.get("level") if action == "volume_set" else expected_amount
    assert service_calls == [(action, expected_value)]


def test_ytm_volume_schemas_expose_absolute_and_relative_percentages() -> None:
    set_schema = tools.get("ytm_volume_set").schema
    up_schema = tools.get("ytm_volume_up").schema
    down_schema = tools.get("ytm_volume_down").schema

    assert set_schema["function"]["parameters"]["required"] == ["level"]
    assert set_schema["function"]["parameters"]["properties"]["level"]["minimum"] == 0
    assert set_schema["function"]["parameters"]["properties"]["level"]["maximum"] == 100
    for schema in (up_schema, down_schema):
        amount = schema["function"]["parameters"]["properties"]["amount"]
        assert amount["default"] == 10
        assert amount["minimum"] == 1
        assert amount["maximum"] == 100


async def test_ytm_status_does_not_report_generic_audio_as_ytm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disconnected() -> dict[str, object]:
        return {
            "ok": False,
            "state": "NEEDS_LOGIN",
            "connected": False,
            "needs_login": True,
            "page_ready": True,
            "search_ready": True,
            "player_loaded": False,
            "playing": None,
            "error": None,
        }

    class FakeMedia:
        async def status(self) -> dict[str, object]:
            return await disconnected()

    monkeypatch.setattr(tools, "MEDIA", FakeMedia())

    result = json.loads(await tools.ytm_status({}))

    assert result["ok"] is False
    assert result["state"] == "NEEDS_LOGIN"
    assert "Jarvis" not in json.dumps(result, ensure_ascii=False)


def test_dead_legacy_ytm_helpers_are_not_in_the_tool_layer() -> None:
    for name in (
        "_YTM_STATE",
        "_ytm_post_keycode",
        "_ytm_open_url",
        "_ytm_activate",
        "_ytm_send_keys_quiet",
        "_ytm_bundle_id",
    ):
        assert not hasattr(tools, name)
