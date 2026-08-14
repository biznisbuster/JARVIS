"""Reliable "now playing" state and media transport control for macOS.

Primary channel is ``nowplaying-cli`` (stable MediaRemote wrapper); every
command is followed by a state read that verifies the ACTUAL effect. The
idempotent play/pause actions retain their retry and fallback chain, while a
delivered next/previous command is never blindly repeated. Results always
report the real state, never just the intent.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

_MR_LIB_PATH = "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
_MR_PLAY = 0
_MR_PAUSE = 1
_MR_NEXT_TRACK = 4
_MR_PREVIOUS_TRACK = 5

_mr_lib: Any = None
_mr_send: Any = None

KeystrokeFallback = Callable[[str], Awaitable[bool]]
_keystroke_fallback: KeystrokeFallback | None = None


def register_keystroke_fallback(fn: KeystrokeFallback | None) -> None:
    """Register the last-resort fallback: focus the player app and send the
    transport keystroke. Called with the action name, returns True if the
    keystroke was delivered."""
    global _keystroke_fallback
    _keystroke_fallback = fn


def _media_remote_send(cmd: int) -> bool:
    global _mr_lib, _mr_send
    if _mr_lib is None:
        try:
            _mr_lib = ctypes.CDLL(_MR_LIB_PATH)
        except OSError:
            _mr_lib = False
    if not _mr_lib:
        return False
    if _mr_send is None:
        try:
            _mr_lib.MRMediaRemoteSendCommand.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            _mr_lib.MRMediaRemoteSendCommand.restype = None
            _mr_send = _mr_lib.MRMediaRemoteSendCommand
        except AttributeError:
            return False
    try:
        _mr_send(ctypes.c_uint32(cmd), None)
        return True
    except Exception:
        return False


def _npc_sync(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(["nowplaying-cli", *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "nowplaying-cli timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


async def _npc(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    return await asyncio.to_thread(_npc_sync, args, timeout)


def _interpret(data: dict[str, Any]) -> bool | None:
    is_playing = data.get("isPlaying")
    rate = data.get("playbackRate")
    if is_playing is True:
        return True
    if is_playing is False:
        return False
    if isinstance(rate, (int, float)):
        return rate > 0
    return None


async def get_state() -> dict[str, Any]:
    """Read the system now-playing state. Returns ``{"ok", "playing",
    "title", "artist", "rate"}``; ``playing`` is None when unknown."""
    rc, out, err = await _npc(["get", "--json", "isPlaying", "title", "artist", "playbackRate"])
    if rc != 0:
        return {
            "ok": False,
            "playing": None,
            "title": "",
            "artist": "",
            "rate": None,
            "error": err or f"nowplaying-cli exit {rc}",
        }
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        return {
            "ok": False,
            "playing": None,
            "title": "",
            "artist": "",
            "rate": None,
            "error": "unparseable nowplaying-cli output",
        }
    rate = data.get("playbackRate")
    return {
        "ok": True,
        "playing": _interpret(data),
        "title": (data.get("title") or "").strip(),
        "artist": (data.get("artist") or "").strip(),
        "rate": rate if isinstance(rate, (int, float)) else None,
    }


_VERIFY_WAIT = 0.45
_TRANSITION_VERIFY_READS = 2
_NON_IDEMPOTENT_ACTIONS = {"next", "previous"}

_NPC_ACTIONS = {"pause": "pause", "play": "play", "next": "next", "previous": "previous"}
_MR_ACTIONS = {"pause": _MR_PAUSE, "play": _MR_PLAY, "next": _MR_NEXT_TRACK, "previous": _MR_PREVIOUS_TRACK}


def _track_identity(state: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Return track metadata suitable for before/after comparison."""
    if not isinstance(state, dict) or not state.get("ok"):
        return None
    track_id = str(state.get("track_id") or state.get("trackId") or "").strip()
    if track_id:
        return ("id", track_id)
    title = str(state.get("title") or "").strip().casefold()
    artist = str(state.get("artist") or "").strip().casefold()
    if title or artist:
        return ("metadata", title, artist)
    return None


def _verification_result(
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return whether an observed state verifies the requested action."""
    if not isinstance(after, dict) or not after.get("ok"):
        return False, "unavailable"
    playing = after.get("playing")
    if action == "pause":
        if playing is False:
            return True, "verified"
        return False, "unavailable" if playing is None else "failed"
    if action == "play":
        if playing is True:
            return True, "verified"
        return False, "unavailable" if playing is None else "failed"
    before_identity = _track_identity(before)
    after_identity = _track_identity(after)
    if before_identity is None or after_identity is None:
        return False, "unavailable"
    changed = before_identity != after_identity
    return changed, "verified" if changed else "failed"


def _verified(action: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    return _verification_result(action, before, after)[0]


async def control(action: str) -> dict[str, Any]:
    """Run a transport command and verify its observed effect.

    Play/pause may retry because they are state-setting actions. Next/previous
    are non-idempotent: once a transport reports delivery, only bounded state
    reads are allowed before returning a verified or explicit unverified
    result.
    """
    if action not in _NPC_ACTIONS:
        return {"ok": False, "error": f"unknown action: {action}"}
    before = await get_state()
    attempts: list[str] = []
    verification_reads = _TRANSITION_VERIFY_READS if action in _NON_IDEMPOTENT_ACTIONS else 1

    async def attempt(
        method: str,
        sent: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        if not sent:
            return None, None, "not_attempted"
        after: dict[str, Any] | None = None
        verification = "unavailable"
        for _ in range(verification_reads):
            await asyncio.sleep(_VERIFY_WAIT)
            after = await get_state()
            verified, verification = _verification_result(action, before, after)
            if verified:
                return (
                    {
                        "ok": True,
                        "action": action,
                        "method": method,
                        "adapter": "nowplaying",
                        "attempts": attempts,
                        "delivered": True,
                        "verified": True,
                        "verification": "verified",
                        "degraded": False,
                        "before": before,
                        "after": after,
                        "state": after,
                    },
                    after,
                    "verified",
                )
        return None, after, verification

    def failure(
        *,
        method: str | None,
        delivered: bool,
        after: dict[str, Any] | None,
        verification: str,
        error: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "method": method,
            "adapter": "nowplaying",
            "attempts": attempts,
            "delivered": delivered,
            "verified": False,
            "verification": verification,
            "degraded": delivered and verification == "unavailable",
            "before": before,
            "after": after,
            "state": after,
            "error": error,
        }

    def transition_failure(method: str, after: dict[str, Any] | None, verification: str) -> dict[str, Any]:
        error = (
            "track transition could not be verified after command delivery"
            if verification == "unavailable"
            else "track did not change after command delivery"
        )
        return failure(
            method=method,
            delivered=True,
            after=after,
            verification=verification,
            error=error,
        )

    non_idempotent = action in _NON_IDEMPOTENT_ACTIONS
    last_method: str | None = None
    last_verification = "not_attempted"
    for _ in range(1 if non_idempotent else 2):
        attempts.append("nowplaying-cli")
        rc, _, _ = await _npc([_NPC_ACTIONS[action]])
        result, after, verification = await attempt("nowplaying-cli", rc == 0)
        if result is not None:
            return result
        if rc == 0:
            if non_idempotent:
                return transition_failure("nowplaying-cli", after, verification)
            last_method = "nowplaying-cli"
            last_verification = verification
        if rc != 0:
            break

    attempts.append("media_remote")
    sent = await asyncio.to_thread(_media_remote_send, _MR_ACTIONS[action])
    result, after, verification = await attempt("media_remote", sent)
    if result is not None:
        return result
    if sent:
        if non_idempotent:
            return transition_failure("media_remote", after, verification)
        last_method = "media_remote"
        last_verification = verification

    if _keystroke_fallback is not None:
        attempts.append("keystroke")
        sent = await _keystroke_fallback(action)
        result, after, verification = await attempt("keystroke", sent)
        if result is not None:
            return result
        if sent:
            if non_idempotent:
                return transition_failure("keystroke", after, verification)
            last_method = "keystroke"
            last_verification = verification

    after = await get_state()
    if last_method is None:
        return failure(
            method=None,
            delivered=False,
            after=after,
            verification="not_attempted",
            error="no channel delivered the command",
        )
    expected = "playing" if action == "play" else "paused"
    return failure(
        method=last_method,
        delivered=True,
        after=after,
        verification=last_verification,
        error=f"playback state did not reach {expected}",
    )
