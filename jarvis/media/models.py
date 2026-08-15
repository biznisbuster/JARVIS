"""Canonical media-domain state and result models.

The browser adapter returns provider-specific evidence.  These small models
are the boundary consumed by the rest of JARVIS so callers do not need to
know about YT Music DOM field names or Playwright lifecycle details.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """Observed adapter/runtime health, separate from playback state."""

    adapter: str = "ytm_web"
    provider: str = "youtube_music"
    connection_state: str = "DISCONNECTED"
    connected: bool = False
    page_ready: bool = False
    search_ready: bool = False
    player_available: bool = False
    error: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None, *, adapter: str = "ytm_web") -> AdapterHealth:
        values = dict(payload or {})
        nested = values.get("health")
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update({key: value for key, value in values.items() if key not in merged})
            values = merged

        connection_state = values.get("connection_state")
        if not isinstance(connection_state, str):
            candidate = values.get("state")
            connection_state = candidate if isinstance(candidate, str) else None
        if not connection_state:
            connection_state = "CONNECTED" if values.get("connected") else "DISCONNECTED"

        return cls(
            adapter=str(values.get("adapter") or adapter),
            provider=str(values.get("provider") or "youtube_music"),
            connection_state=connection_state,
            connected=bool(values.get("connected") or connection_state == "CONNECTED"),
            page_ready=bool(values.get("page_ready")),
            search_ready=bool(values.get("search_ready")),
            player_available=bool(values.get("player_available") or values.get("player_loaded")),
            error=str(values["error"]) if values.get("error") is not None else None,
        )

    @property
    def available(self) -> bool:
        """Whether the authenticated provider surface can accept searches."""

        return self.connected and self.page_ready and self.search_ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "provider": self.provider,
            "state": self.connection_state,
            "connection_state": self.connection_state,
            "connected": self.connected,
            "needs_login": self.connection_state == "NEEDS_LOGIN",
            "available": self.available,
            "page_ready": self.page_ready,
            "search_ready": self.search_ready,
            "player_loaded": self.player_available,
            "player_available": self.player_available,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PlaybackState:
    """One observed media snapshot.

    ``playing`` is observed playback state, not the last requested intent.
    ``volume`` is a percentage from 0 to 100 when the provider exposes it.
    A connected provider with no loaded track is valid: ``ok`` and health may
    be true while ``player_available`` is false and playback fields are empty.
    """

    ok: bool = False
    health: AdapterHealth = field(default_factory=AdapterHealth)
    player_available: bool = False
    playing: bool | None = None
    track_id: str | None = None
    title: str = ""
    artist: str = ""
    current_time: float | None = None
    duration: float | None = None
    volume: float | None = None
    muted: bool | None = None
    source: str = "ytm_web"
    error: str | None = None

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        health: AdapterHealth | None = None,
        adapter: str = "ytm_web",
    ) -> PlaybackState:
        values = dict(payload or {})
        observed_health = health or AdapterHealth.from_payload(values, adapter=adapter)

        track_value = values.get("track_id")
        if track_value is None:
            track_value = values.get("trackId")
        track_id = str(track_value).strip() if track_value else None

        player_available = bool(
            values.get("player_available")
            or values.get("player_loaded")
            or (track_id is not None and values.get("ok") is True)
        )
        playing = values.get("playing") if isinstance(values.get("playing"), bool) else None

        def number(*keys: str) -> float | None:
            for key in keys:
                value = values.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return float(value)
            return None

        raw_volume = number("volume")
        if raw_volume is not None and 0 <= raw_volume <= 1:
            raw_volume *= 100

        return cls(
            ok=bool(values.get("ok")),
            health=observed_health,
            player_available=player_available,
            playing=playing,
            track_id=track_id,
            title=str(values.get("title") or "").strip(),
            artist=str(values.get("artist") or "").strip(),
            current_time=number("current_time", "currentTime"),
            duration=number("duration"),
            volume=raw_volume,
            muted=values.get("muted") if isinstance(values.get("muted"), bool) else None,
            source=str(values.get("source") or values.get("adapter") or observed_health.adapter),
            error=str(values["error"]) if values.get("error") is not None else None,
        )

    @classmethod
    def unavailable(cls, health: AdapterHealth, *, error: str | None = None) -> PlaybackState:
        return cls(ok=False, health=health, error=error or health.error)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "ok": self.ok,
            **self.health.to_dict(),
            "source": self.source,
            "playing": self.playing,
            "player_loaded": self.player_available,
            "player_available": self.player_available,
            "track_id": self.track_id or "",
            "title": self.title,
            "artist": self.artist,
            "currentTime": self.current_time if self.current_time is not None else 0,
            "duration": self.duration if self.duration is not None else 0,
            "volume": self.volume,
            "muted": self.muted,
            "error": self.error,
        }
        return result


@dataclass(frozen=True, slots=True)
class MediaActionResult:
    """Normalized result for a media mutation while retaining diagnostics."""

    action: str
    ok: bool
    adapter: str
    delivered: bool
    verified: bool
    verification: str
    state: PlaybackState
    before: PlaybackState | None = None
    after: PlaybackState | None = None
    degraded: bool = False
    error: str | None = None
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.details)
        result.update(
            {
                "ok": self.ok,
                "action": self.action,
                "adapter": self.adapter,
                "provider": self.state.health.provider,
                "delivered": self.delivered,
                "verified": self.verified,
                "verification": self.verification,
                "degraded": self.degraded,
                "state": self.state.to_dict(),
            }
        )
        if self.before is not None:
            result["before"] = self.before.to_dict()
        if self.after is not None:
            result["after"] = self.after.to_dict()
        if self.error is not None:
            result["error"] = self.error
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result
