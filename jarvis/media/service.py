"""Authoritative media service and the production YT Music adapter.

``YtmWebAdapter`` intentionally wraps the existing persistent Playwright
runtime in :mod:`jarvis.media.ytm_web`.  It does not launch another browser,
create another profile, or recreate authentication.  ``MediaService`` is the
single application-facing boundary and serializes media mutations while
leaving read-only status calls available during a long verification.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from . import ytm_web
from .models import AdapterHealth, MediaActionResult, PlaybackState

log = logging.getLogger("jarvis.media.service")


class MediaAdapter(Protocol):
    """Minimal provider adapter contract used by ``MediaService``."""

    name: str
    provider: str

    async def health(self) -> AdapterHealth: ...

    async def connection_status(self) -> Mapping[str, Any]: ...

    async def connect(self) -> Mapping[str, Any]: ...

    async def get_state(self) -> PlaybackState | Mapping[str, Any]: ...

    async def play_query(self, query: str) -> Mapping[str, Any] | MediaActionResult: ...

    async def pause(self) -> Mapping[str, Any] | MediaActionResult: ...

    async def resume(self) -> Mapping[str, Any] | MediaActionResult: ...

    async def next(self) -> Mapping[str, Any] | MediaActionResult: ...

    async def previous(self) -> Mapping[str, Any] | MediaActionResult: ...

    async def volume_set(self, level: int | None) -> Mapping[str, Any] | MediaActionResult: ...

    async def volume_up(self, amount: int | None) -> Mapping[str, Any] | MediaActionResult: ...

    async def volume_down(self, amount: int | None) -> Mapping[str, Any] | MediaActionResult: ...

    async def volume_mute(self) -> Mapping[str, Any] | MediaActionResult: ...

    def warm_up(self) -> None: ...

    async def close(self) -> None: ...


class YtmWebAdapter:
    """Thin adapter over the existing authenticated ``ytm_web`` runtime."""

    name = "ytm_web"
    provider = "youtube_music"

    def __init__(self, runtime: Any = ytm_web) -> None:
        self.runtime = runtime

    async def connection_status(self) -> Mapping[str, Any]:
        result = await self.runtime.connection_status()
        return {**dict(result), "adapter": self.name, "provider": self.provider}

    async def connect(self) -> Mapping[str, Any]:
        result = await self.runtime.connect()
        return {**dict(result), "adapter": self.name, "provider": self.provider}

    async def health(self) -> AdapterHealth:
        return AdapterHealth.from_payload(await self.connection_status(), adapter=self.name)

    async def get_state(self) -> PlaybackState:
        status = await self.connection_status()
        health = AdapterHealth.from_payload(status, adapter=self.name)
        if not health.connected:
            return PlaybackState.unavailable(
                health,
                error=health.error or f"YT Music is {health.connection_state.lower()}",
            )
        try:
            raw_state = await self.runtime.get_state()
        except Exception as exc:  # noqa: BLE001
            log.warning("ytm_web adapter state read failed: %s", exc)
            return PlaybackState.unavailable(health, error=str(exc))
        if isinstance(raw_state, PlaybackState):
            return raw_state
        combined = dict(status)
        if isinstance(raw_state, Mapping):
            combined.update(raw_state)
        return PlaybackState.from_payload(combined, health=health, adapter=self.name)

    async def play_query(self, query: str) -> Mapping[str, Any]:
        return await self.runtime.play_query(query)

    async def pause(self) -> Mapping[str, Any]:
        return await self.runtime.control("pause")

    async def resume(self) -> Mapping[str, Any]:
        return await self.runtime.control("play")

    async def next(self) -> Mapping[str, Any]:
        return await self.runtime.control("next")

    async def previous(self) -> Mapping[str, Any]:
        return await self.runtime.control("previous")

    async def volume_set(self, level: int | None) -> Mapping[str, Any]:
        return await self.runtime.control_volume("volume_set", level=level)

    async def volume_up(self, amount: int | None) -> Mapping[str, Any]:
        return await self.runtime.control_volume("volume_up", amount=amount)

    async def volume_down(self, amount: int | None) -> Mapping[str, Any]:
        return await self.runtime.control_volume("volume_down", amount=amount)

    async def volume_mute(self) -> Mapping[str, Any]:
        return await self.runtime.control_volume("volume_mute")

    def warm_up(self) -> None:
        self.runtime.warm_up()

    async def close(self) -> None:
        await self.runtime.shutdown()


class MediaService:
    """Single authoritative media façade for JARVIS.

    Only mutation operations share the service lock.  State and health reads
    remain independent so a status request is not queued behind a long YT
    Music search or playback verification.  No fallback adapter is selected:
    an unavailable YT Music adapter produces an explicit failure.
    """

    def __init__(self, adapter: MediaAdapter | None = None) -> None:
        self.adapter: MediaAdapter = adapter or YtmWebAdapter()
        self._mutation_lock: asyncio.Lock | None = None

    def _get_mutation_lock(self) -> asyncio.Lock:
        if self._mutation_lock is None:
            self._mutation_lock = asyncio.Lock()
        return self._mutation_lock

    def warm_up(self) -> None:
        self.adapter.warm_up()

    async def close(self) -> None:
        await self.adapter.close()

    async def connection_status(self) -> dict[str, Any]:
        return dict(await self.adapter.connection_status())

    async def connect(self) -> dict[str, Any]:
        return dict(await self.adapter.connect())

    async def health(self) -> AdapterHealth:
        return await self.adapter.health()

    async def get_state(self) -> PlaybackState:
        raw_state = await self.adapter.get_state()
        if isinstance(raw_state, PlaybackState):
            return raw_state
        health = await self.adapter.health()
        return PlaybackState.from_payload(raw_state, health=health, adapter=self.adapter.name)

    async def status(self) -> dict[str, Any]:
        """Return the same canonical snapshot used by world-state."""

        return (await self.get_state()).to_dict()

    async def _run_mutation(
        self,
        action: str,
        operation: Callable[[], Awaitable[Mapping[str, Any] | MediaActionResult]],
    ) -> MediaActionResult:
        async with self._get_mutation_lock():
            try:
                raw_result = await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("media action failed adapter=%s action=%s: %s", self.adapter.name, action, exc)
                raw_result = {
                    "ok": False,
                    "action": action,
                    "adapter": self.adapter.name,
                    "delivered": False,
                    "verified": False,
                    "verification": "not_attempted",
                    "error_code": "EXECUTION_FAILED",
                    "error": str(exc),
                }
            return await self._normalize_result(action, raw_result)

    async def _normalize_result(
        self,
        action: str,
        raw_result: Mapping[str, Any] | MediaActionResult,
    ) -> MediaActionResult:
        if isinstance(raw_result, MediaActionResult):
            return raw_result

        raw = dict(raw_result)
        health = AdapterHealth.from_payload(raw, adapter=self.adapter.name)

        def state_from(value: Any) -> PlaybackState | None:
            if isinstance(value, PlaybackState):
                return value
            if isinstance(value, Mapping):
                return PlaybackState.from_payload(value, health=health, adapter=self.adapter.name)
            return None

        before = state_from(raw.get("before"))
        after = state_from(raw.get("after"))
        state = after or state_from(raw.get("state")) or before
        if state is None:
            state = PlaybackState.from_payload(raw, health=health, adapter=self.adapter.name)

        raw_ok = raw.get("ok") is True
        delivered = raw.get("delivered") is True
        verified = raw.get("verified") is True
        verification = str(raw.get("verification") or ("verified" if verified else "not_attempted"))
        ok = raw_ok and verified
        error = str(raw["error"]) if raw.get("error") is not None else None
        error_code = str(raw["error_code"]) if raw.get("error_code") is not None else None
        if raw_ok and not verified:
            error = error or "media action was not verified"
            error_code = error_code or "VERIFICATION_FAILED"

        reserved = {
            "ok",
            "action",
            "adapter",
            "provider",
            "delivered",
            "verified",
            "verification",
            "degraded",
            "before",
            "after",
            "state",
            "error",
            "error_code",
        }
        details = {key: value for key, value in raw.items() if key not in reserved}
        return MediaActionResult(
            action=str(raw.get("action") or action),
            ok=ok,
            adapter=str(raw.get("adapter") or self.adapter.name),
            delivered=delivered,
            verified=verified,
            verification=verification,
            state=state,
            before=before,
            after=after,
            degraded=bool(raw.get("degraded")),
            error=error,
            error_code=error_code,
            details=details,
        )

    async def play_query(self, query: str) -> MediaActionResult:
        return await self._run_mutation("play_query", lambda: self.adapter.play_query(query))

    async def pause(self) -> MediaActionResult:
        return await self._run_mutation("pause", self.adapter.pause)

    async def resume(self) -> MediaActionResult:
        return await self._run_mutation("resume", self.adapter.resume)

    async def next(self) -> MediaActionResult:
        return await self._run_mutation("next", self.adapter.next)

    async def previous(self) -> MediaActionResult:
        return await self._run_mutation("previous", self.adapter.previous)

    async def volume_set(self, level: int | None) -> MediaActionResult:
        return await self._run_mutation("volume_set", lambda: self.adapter.volume_set(level))

    async def volume_up(self, amount: int | None) -> MediaActionResult:
        return await self._run_mutation("volume_up", lambda: self.adapter.volume_up(amount))

    async def volume_down(self, amount: int | None) -> MediaActionResult:
        return await self._run_mutation("volume_down", lambda: self.adapter.volume_down(amount))

    async def volume_mute(self) -> MediaActionResult:
        return await self._run_mutation("volume_mute", self.adapter.volume_mute)

    async def control(self, action: str) -> MediaActionResult:
        operations: dict[str, Callable[[], Awaitable[Mapping[str, Any] | MediaActionResult]]] = {
            "pause": self.adapter.pause,
            "play": self.adapter.resume,
            "next": self.adapter.next,
            "previous": self.adapter.previous,
        }
        operation = operations.get(action)
        if operation is None:
            return await self._normalize_result(
                action,
                {
                    "ok": False,
                    "action": action,
                    "adapter": self.adapter.name,
                    "delivered": False,
                    "verified": False,
                    "verification": "not_attempted",
                    "error_code": "INVALID_ARGUMENTS",
                    "error": f"unknown media action: {action}",
                },
            )
        return await self._run_mutation(action, operation)

    async def control_volume(
        self,
        action: str,
        *,
        amount: int | None = None,
        level: int | None = None,
    ) -> MediaActionResult:
        operations: dict[str, Callable[[], Awaitable[Mapping[str, Any] | MediaActionResult]]] = {
            "volume_up": lambda: self.adapter.volume_up(amount),
            "volume_down": lambda: self.adapter.volume_down(amount),
            "volume_set": lambda: self.adapter.volume_set(level),
            "volume_mute": self.adapter.volume_mute,
        }
        operation = operations.get(action)
        if operation is None:
            return await self._normalize_result(
                action,
                {
                    "ok": False,
                    "action": action,
                    "adapter": self.adapter.name,
                    "delivered": False,
                    "verified": False,
                    "verification": "not_attempted",
                    "error_code": "INVALID_ARGUMENTS",
                    "error": f"unknown volume action: {action}",
                },
            )
        return await self._run_mutation(action, operation)


# One application-wide authority.  It owns no browser until the existing
# runtime is explicitly warmed or an action requires it.
MEDIA = MediaService()
