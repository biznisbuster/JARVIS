"""World-state and YTM status must share MediaService state."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jarvis import context
from jarvis.agent import tools
from jarvis.media.models import AdapterHealth, PlaybackState


class FakeMediaService:
    def __init__(self, state: PlaybackState) -> None:
        self.state = state

    async def get_state(self) -> PlaybackState:
        return self.state

    async def status(self) -> dict[str, Any]:
        return self.state.to_dict()


def _state(
    *,
    playing: bool | None,
    title: str = "",
    artist: str = "",
    track_id: str | None = None,
    player_available: bool = False,
    ok: bool = True,
    connected: bool = True,
) -> PlaybackState:
    health = AdapterHealth(
        adapter="ytm_web",
        provider="youtube_music",
        connection_state="CONNECTED" if connected else "NEEDS_LOGIN",
        connected=connected,
        page_ready=connected,
        search_ready=connected,
        player_available=player_available,
    )
    return PlaybackState(
        ok=ok,
        health=health,
        player_available=player_available,
        playing=playing,
        track_id=track_id,
        title=title,
        artist=artist,
    )


@pytest.mark.parametrize(
    ("state", "world_fragment"),
    [
        (
            _state(
                playing=True,
                title="Song A",
                artist="Artist B",
                track_id="track-a",
                player_available=True,
            ),
            "muzika: SVIRA — „Song A“ (Artist B)",
        ),
        (
            _state(
                playing=False,
                title="Song A",
                artist="Artist B",
                track_id="track-a",
                player_available=True,
            ),
            "muzika: pauzirana/stopirana — „Song A“ (Artist B)",
        ),
        (_state(playing=None), "muzika: stanje nepoznato"),
        (
            _state(playing=None, ok=False, connected=False),
            "muzika: nepoznato (YT Music status nedostupan)",
        ),
    ],
)
async def test_world_state_uses_canonical_media_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    state: PlaybackState,
    world_fragment: str,
) -> None:
    service = FakeMediaService(state)
    monkeypatch.setattr(context, "MEDIA", service)
    monkeypatch.setattr(context, "_volume_sync", lambda: 42)

    world = await context.build_world_state()

    assert world_fragment in world
    assert "Sistemski zvuk: 42%" in world


async def test_ytm_status_and_world_state_read_the_same_service_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(
        playing=True,
        title="Song A",
        artist="Artist B",
        track_id="track-a",
        player_available=True,
    )
    service = FakeMediaService(state)
    monkeypatch.setattr(context, "MEDIA", service)
    monkeypatch.setattr(tools, "MEDIA", service)
    monkeypatch.setattr(context, "_volume_sync", lambda: None)

    world = await context.build_world_state()
    status = json.loads(await tools.ytm_status({}))

    assert status["playing"] is True
    assert status["title"] == "Song A"
    assert status["artist"] == "Artist B"
    assert status["track_id"] == "track-a"
    assert "Song A" in world
    assert "Artist B" in world
    assert "SVIRA" in world


async def test_stale_or_generic_media_state_cannot_override_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(playing=False, player_available=False)
    service = FakeMediaService(state)
    monkeypatch.setattr(tools, "MEDIA", service)

    status = json.loads(await tools.ytm_status({}))

    assert status["playing"] is False
    assert status["track_id"] == ""
    assert status["title"] == ""
    assert not hasattr(tools, "_YTM_STATE")
