"""Focused contract tests for the Phase 2 media boundary."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis.media import nowplaying
from jarvis.media.models import AdapterHealth, MediaActionResult, PlaybackState
from jarvis.media.service import MediaService, YtmWebAdapter


class FakeYtmRuntime:
    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.status = status or {
            "state": "CONNECTED",
            "connected": True,
            "needs_login": False,
            "page_ready": True,
            "search_ready": True,
            "player_loaded": True,
            "playing": True,
            "error": None,
        }
        self.state = state or {
            "ok": True,
            "player_loaded": True,
            "playing": True,
            "track_id": "track-a",
            "title": "Song A",
            "artist": "Artist A",
            "currentTime": 12.0,
            "duration": 200.0,
        }
        self.calls: list[tuple[str, Any]] = []

    async def connection_status(self) -> dict[str, Any]:
        self.calls.append(("connection_status", None))
        return dict(self.status)

    async def connect(self) -> dict[str, Any]:
        self.calls.append(("connect", None))
        return dict(self.status)

    async def get_state(self) -> dict[str, Any]:
        self.calls.append(("get_state", None))
        return dict(self.state)

    async def play_query(self, query: str) -> dict[str, Any]:
        self.calls.append(("play_query", query))
        return {
            "ok": True,
            "action": "play_query",
            "adapter": "ytm_web",
            "delivered": True,
            "verified": True,
            "verification": "verified_player_id",
            "state": {**self.state, "title": query},
        }

    async def control(self, action: str) -> dict[str, Any]:
        self.calls.append(("control", action))
        return {
            "ok": True,
            "action": action,
            "adapter": "ytm_web",
            "delivered": True,
            "verified": True,
            "verification": "verified",
            "before": self.state,
            "after": {**self.state, "playing": action == "play"},
        }

    async def control_volume(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((action, kwargs))
        return {
            "ok": True,
            "action": action,
            "adapter": "ytm_web",
            "delivered": True,
            "verified": True,
            "verification": "verified",
            "before": {"ok": True, "volume": 0.5, "muted": False},
            "after": {"ok": True, "volume": 0.7, "muted": action == "volume_mute"},
        }

    def warm_up(self) -> None:
        self.calls.append(("warm_up", None))

    async def shutdown(self) -> None:
        self.calls.append(("shutdown", None))


class FakeAdapter:
    name = "ytm_web"
    provider = "youtube_music"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = PlaybackState(
            ok=True,
            health=AdapterHealth(
                adapter=self.name,
                provider=self.provider,
                connection_state="CONNECTED",
                connected=True,
                page_ready=True,
                search_ready=True,
                player_available=True,
            ),
            player_available=True,
            playing=True,
            track_id="track-a",
            title="Song A",
            artist="Artist A",
        )
        self.play_started = asyncio.Event()
        self.release_play = asyncio.Event()
        self.block_play = False

    async def health(self) -> AdapterHealth:
        return self.state.health

    async def connection_status(self) -> dict[str, Any]:
        return self.state.health.to_dict()

    async def connect(self) -> dict[str, Any]:
        self.calls.append("connect")
        return self.state.health.to_dict()

    async def get_state(self) -> PlaybackState:
        self.calls.append("get_state")
        return self.state

    def _success(self, action: str) -> dict[str, Any]:
        return {
            "ok": True,
            "action": action,
            "adapter": self.name,
            "delivered": True,
            "verified": True,
            "verification": "verified",
            "before": self.state.to_dict(),
            "after": self.state.to_dict(),
        }

    async def play_query(self, query: str) -> dict[str, Any]:
        self.calls.append(f"play_query:{query}")
        self.play_started.set()
        if self.block_play:
            await self.release_play.wait()
        return self._success("play_query")

    async def pause(self) -> dict[str, Any]:
        self.calls.append("pause")
        return self._success("pause")

    async def resume(self) -> dict[str, Any]:
        self.calls.append("resume")
        return self._success("resume")

    async def next(self) -> dict[str, Any]:
        self.calls.append("next")
        return self._success("next")

    async def previous(self) -> dict[str, Any]:
        self.calls.append("previous")
        return self._success("previous")

    async def volume_set(self, level: int | None) -> dict[str, Any]:
        self.calls.append(f"volume_set:{level}")
        return self._success("volume_set")

    async def volume_up(self, amount: int | None) -> dict[str, Any]:
        self.calls.append(f"volume_up:{amount}")
        return self._success("volume_up")

    async def volume_down(self, amount: int | None) -> dict[str, Any]:
        self.calls.append(f"volume_down:{amount}")
        return self._success("volume_down")

    async def volume_mute(self) -> dict[str, Any]:
        self.calls.append("volume_mute")
        return self._success("volume_mute")

    def warm_up(self) -> None:
        self.calls.append("warm_up")

    async def close(self) -> None:
        self.calls.append("close")


@pytest.mark.parametrize(
    ("playing", "expected_title", "expected_artist"),
    [(True, "Song A", "Artist A"), (False, "Song A", "Artist A")],
)
async def test_ytm_web_adapter_normalizes_playing_and_paused_state(
    playing: bool,
    expected_title: str,
    expected_artist: str,
) -> None:
    runtime = FakeYtmRuntime(
        state={
            "ok": True,
            "player_loaded": True,
            "playing": playing,
            "track_id": "track-a",
            "title": expected_title,
            "artist": expected_artist,
        }
    )

    state = await YtmWebAdapter(runtime).get_state()

    assert state.ok is True
    assert state.health.available is True
    assert state.player_available is True
    assert state.playing is playing
    assert state.track_id == "track-a"
    assert state.title == expected_title
    assert state.artist == expected_artist
    assert state.source == "ytm_web"


async def test_connected_ytm_page_without_track_is_valid() -> None:
    runtime = FakeYtmRuntime(
        state={"ok": True, "player_loaded": False, "playing": None, "title": "", "artist": ""}
    )

    state = await YtmWebAdapter(runtime).get_state()

    assert state.ok is True
    assert state.health.available is True
    assert state.player_available is False
    assert state.playing is None
    assert state.track_id is None
    assert state.title == ""


async def test_unavailable_ytm_adapter_is_explicit_and_does_not_read_stale_track() -> None:
    runtime = FakeYtmRuntime(
        status={
            "state": "NEEDS_LOGIN",
            "connected": False,
            "needs_login": True,
            "page_ready": True,
            "search_ready": True,
            "player_loaded": False,
            "playing": None,
            "error": None,
        }
    )

    state = await YtmWebAdapter(runtime).get_state()

    assert state.ok is False
    assert state.health.connection_state == "NEEDS_LOGIN"
    assert state.health.available is False
    assert state.playing is None
    assert state.track_id is None
    assert ("get_state", None) not in runtime.calls


async def test_media_service_delegates_play_once_and_preserves_verified_diagnostics() -> None:
    adapter = FakeAdapter()
    service = MediaService(adapter)

    result = await service.play_query("Song A")

    assert result.ok is True
    assert result.delivered is True
    assert result.verified is True
    assert result.state.track_id == "track-a"
    assert adapter.calls == ["play_query:Song A"]


async def test_media_service_forces_delivery_only_result_to_failure() -> None:
    adapter = FakeAdapter()

    async def unverified_play(query: str) -> dict[str, Any]:
        adapter.calls.append(f"play_query:{query}")
        return {
            "ok": True,
            "action": "play_query",
            "adapter": "ytm_web",
            "delivered": True,
            "verified": False,
            "verification": "unavailable",
            "error": "state unavailable",
        }

    adapter.play_query = unverified_play  # type: ignore[method-assign]
    result = await MediaService(adapter).play_query("Song A")

    assert result.ok is False
    assert result.delivered is True
    assert result.verified is False
    assert result.error_code == "VERIFICATION_FAILED"
    assert adapter.calls == ["play_query:Song A"]


@pytest.mark.parametrize(
    ("method", "expected_call"),
    [("pause", "pause"), ("resume", "resume"), ("next", "next"), ("previous", "previous")],
)
async def test_transport_actions_are_thin_service_delegations(method: str, expected_call: str) -> None:
    adapter = FakeAdapter()
    result = await getattr(MediaService(adapter), method)()

    assert isinstance(result, MediaActionResult)
    assert result.ok is True
    assert adapter.calls == [expected_call]


@pytest.mark.parametrize(
    ("method", "argument", "expected_call"),
    [
        ("volume_set", 30, "volume_set:30"),
        ("volume_up", 15, "volume_up:15"),
        ("volume_down", 20, "volume_down:20"),
        ("volume_mute", None, "volume_mute"),
    ],
)
async def test_volume_actions_route_to_ytm_adapter(
    method: str,
    argument: int | None,
    expected_call: str,
) -> None:
    adapter = FakeAdapter()
    service = MediaService(adapter)
    result = (
        await getattr(service, method)(argument) if method != "volume_mute" else await service.volume_mute()
    )

    assert result.ok is True
    assert adapter.calls == [expected_call]


async def test_media_service_does_not_consume_generic_nowplaying(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_generic_state() -> dict[str, Any]:
        raise AssertionError("generic nowplaying is not YT Music truth")

    monkeypatch.setattr(nowplaying, "get_state", forbidden_generic_state)
    runtime = FakeYtmRuntime()

    state = await MediaService(YtmWebAdapter(runtime)).get_state()

    assert state.track_id == "track-a"
    assert state.source == "ytm_web"


async def test_mutations_are_serialized_but_status_reads_are_not_blocked() -> None:
    adapter = FakeAdapter()
    adapter.block_play = True
    service = MediaService(adapter)

    play_task = asyncio.create_task(service.play_query("Song A"))
    await asyncio.wait_for(adapter.play_started.wait(), timeout=1)

    status = await asyncio.wait_for(service.get_state(), timeout=1)
    assert status.track_id == "track-a"
    assert adapter.calls == ["play_query:Song A", "get_state"]

    adapter.release_play.set()
    result = await asyncio.wait_for(play_task, timeout=1)
    assert result.ok is True


async def test_two_mutations_cannot_run_on_the_shared_adapter_at_once() -> None:
    adapter = FakeAdapter()
    adapter.block_play = True
    service = MediaService(adapter)

    first = asyncio.create_task(service.play_query("Song A"))
    await asyncio.wait_for(adapter.play_started.wait(), timeout=1)
    second = asyncio.create_task(service.next())
    await asyncio.sleep(0)
    assert adapter.calls == ["play_query:Song A"]

    adapter.release_play.set()
    await asyncio.wait_for(first, timeout=1)
    await asyncio.wait_for(second, timeout=1)
    assert adapter.calls == ["play_query:Song A", "next"]
