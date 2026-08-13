"""Reliable "now playing" state and media transport control for macOS.

Primary channel is ``nowplaying-cli`` (stable MediaRemote wrapper); every
command is followed by a state read that verifies the ACTUAL effect, with one
retry and a fallback chain (MediaRemote ctypes -> registered keystroke
fallback). Results always report the real state, never just the intent.
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

_NPC_ACTIONS = {"pause": "pause", "play": "play", "next": "next", "previous": "previous"}
_MR_ACTIONS = {"pause": _MR_PAUSE, "play": _MR_PLAY, "next": _MR_NEXT_TRACK, "previous": _MR_PREVIOUS_TRACK}


def _verified(action: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not after.get("ok"):
        return False
    playing = after.get("playing")
    if action == "pause":
        return playing is False
    if action == "play":
        return playing is True
    if playing is False:
        return False
    if before.get("ok") and (before.get("title") or before.get("artist")):
        return (after.get("title"), after.get("artist")) != (
            before.get("title"),
            before.get("artist"),
        )
    return playing is True


async def control(action: str) -> dict[str, Any]:
    """Run a transport command (pause/play/next/previous) and verify the
    effect. Channels: nowplaying-cli (with one retry) -> MediaRemote ctypes
    -> keystroke fallback. The result reports the FINAL real state."""
    if action not in _NPC_ACTIONS:
        return {"ok": False, "error": f"unknown action: {action}"}
    before = await get_state()
    attempts: list[str] = []

    async def attempt(method: str, sent: bool) -> dict[str, Any] | None:
        if not sent:
            return None
        await asyncio.sleep(_VERIFY_WAIT)
        after = await get_state()
        if _verified(action, before, after):
            return {
                "ok": True,
                "action": action,
                "method": method,
                "attempts": attempts,
                "verified": True,
                "state": after,
            }
        return None

    for _ in range(2):
        attempts.append("nowplaying-cli")
        rc, _, _ = await _npc([_NPC_ACTIONS[action]])
        res = await attempt("nowplaying-cli", rc == 0)
        if res:
            return res
        if rc != 0:
            break

    attempts.append("media_remote")
    sent = await asyncio.to_thread(_media_remote_send, _MR_ACTIONS[action])
    res = await attempt("media_remote", sent)
    if res:
        return res

    if _keystroke_fallback is not None:
        attempts.append("keystroke")
        sent = await _keystroke_fallback(action)
        res = await attempt("keystroke", sent)
        if res:
            return res

    after = await get_state()
    return {
        "ok": False,
        "action": action,
        "method": None,
        "attempts": attempts,
        "verified": False,
        "state": after,
        "error": "no channel produced the expected effect",
    }
