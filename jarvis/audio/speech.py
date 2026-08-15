"""Server-driven speech pipeline.

The agent loop feeds assistant text deltas here while the model is still
generating. Complete sentences are synthesized ahead of playback and
published as ordered ``tts_speak`` events, so the voice starts speaking
the moment the first sentence exists — not after the whole answer is done.

This replaces the old client-driven design where every browser tab
independently requested synthesis after ``assistant_done`` and needed
multi-layer dedup (BroadcastChannel lottery, X-Speech-First headers,
per-tab caches). Synthesis now happens exactly once per sentence, on the
server; tabs only play the served audio file.

Barge-in: ``cancel()`` drops pending sentences, stops server-side playback
and tells all tabs to stop via ``tts_stop``.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..bus import BUS
from ..config import SETTINGS
from . import player
from . import tts as tts_mod

_MIN_SENTENCE = 12
_MAX_PENDING = 350
_CACHE_MAX = 32

_SENT_END = re.compile(r"([.!?…]+[\"')\]]*|\n{2,})\s+")
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://[^\s)]+")
_EMOJI = re.compile(r"[\U00010000-\U0010ffff]")
_LIST_BULLET = re.compile(r"(?m)^\s*(?:[-*+•]|\d+[.)])\s+")
_MD_TOKENS = re.compile(r"[#*_~`>|]+")

_cache: OrderedDict[str, Path] = OrderedDict()


def normalize_for_speech(text: str) -> str:
    """Turn markdown-flavoured assistant text into something a TTS engine
    can speak without reading punctuation, code fences or URLs aloud."""
    t = _CODE_BLOCK.sub(" ", text)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub(" ", t)
    t = _LIST_BULLET.sub("", t)
    t = _MD_TOKENS.sub(" ", t)
    t = _EMOJI.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if tts_mod.current_voice_info()["backend"] == "say":
        # Lana (hr_HR) has no Serbian đ — approximate it so the word is
        # still recognizable instead of being skipped/garbled.
        t = t.replace("đ", "dž").replace("Đ", "Dž")
    return t


def _take_sentence(buf: str) -> tuple[str | None, str]:
    """Pop the leading speakable sentence from ``buf`` if one is complete."""
    for m in _SENT_END.finditer(buf):
        if m.end() >= _MIN_SENTENCE:
            return buf[: m.end()].strip(), buf[m.end() :]
    if len(buf) > _MAX_PENDING:
        cut = max(buf.rfind(",", 0, _MAX_PENDING), buf.rfind(" ", 0, _MAX_PENDING))
        if cut > _MIN_SENTENCE:
            return buf[:cut].strip(), buf[cut + 1 :]
        return buf[:_MAX_PENDING].strip(), buf[_MAX_PENDING:]
    return None, buf


class _SessionSpeech:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.source = "text"
        self._buf = ""
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seq = 0
        self._suppressed = False

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._run())

    def feed(self, delta: str) -> None:
        self._buf += delta
        while True:
            sentence, self._buf = _take_sentence(self._buf)
            if sentence is None:
                break
            self._queue.put_nowait(sentence)
        self._ensure_worker()

    def end_message(self) -> None:
        tail = self._buf.strip()
        self._buf = ""
        if tail:
            self._queue.put_nowait(tail)
        self._ensure_worker()

    def begin_turn(self, source: str = "text") -> None:
        self._suppressed = False
        self._seq = 0
        self.source = "ptt" if source == "ptt" else "text"

    def end_turn(self) -> None:
        self.end_message()
        self._queue.put_nowait(None)
        self._ensure_worker()

    def suppress(self) -> None:
        self._queue.put_nowait(_SUPPRESS)
        self._ensure_worker()

    def discard(self) -> None:
        """Stop this session's speech worker and drop all buffered/queued
        text without emitting events or touching global playback. Safe to
        call repeatedly."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._buf = ""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self._worker = None

    async def _run(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                if item is _SUPPRESS:
                    self._suppressed = True
                    continue
                if self._suppressed:
                    continue
                await self._speak(str(item))
        except asyncio.CancelledError:
            pass
        finally:
            self._worker = None

    async def _speak(self, sentence: str) -> None:
        text = normalize_for_speech(sentence)
        if not text:
            return
        try:
            path = await _synthesize_cached(text)
        except Exception as exc:  # noqa: BLE001
            await BUS.publish("tts_error", {"engine": "speech", "error": str(exc)})
            return
        self._seq += 1
        rel = Path(path).name
        server_play = SETTINGS.audio.output == "say" or self.source == "ptt"
        await BUS.publish(
            "tts_speak",
            {
                "session": self.session_id,
                "seq": self._seq,
                "url": f"/api/audio/file/{rel}",
                "text": text[:200],
                "server_played": server_play,
            },
        )
        if server_play:
            await player.play_file(path)


class _SuppressToken:
    pass


_SUPPRESS = _SuppressToken()


async def _synthesize_cached(text: str) -> str:
    info = tts_mod.current_voice_info()
    key = f"{info['backend']}:{info['voice']}:{text}"
    hit = _cache.get(key)
    if hit is not None and hit.exists():
        _cache.move_to_end(key)
        return str(hit)
    path = Path(await tts_mod.synthesize(text))
    _cache[key] = path
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return str(path)


class SpeechScheduler:
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionSpeech] = {}

    def _get(self, session_id: str) -> _SessionSpeech:
        ss = self._sessions.get(session_id)
        if ss is None:
            ss = _SessionSpeech(session_id)
            self._sessions[session_id] = ss
        return ss

    def begin_turn(self, session_id: str, *, source: str = "text") -> None:
        self._get(session_id).begin_turn(source)

    def feed(self, session_id: str, delta: str) -> None:
        self._get(session_id).feed(delta)

    def end_message(self, session_id: str) -> None:
        self._get(session_id).end_message()

    def end_turn(self, session_id: str) -> None:
        self._get(session_id).end_turn()

    def suppress(self, session_id: str) -> None:
        self._get(session_id).suppress()

    def discard(self, session_id: str) -> None:
        ss = self._sessions.get(session_id)
        if ss is not None:
            ss.discard()

    async def cancel(self, session_id: str) -> None:
        ss = self._sessions.get(session_id)
        if ss is None:
            return
        ss.discard()
        await player.stop()
        await BUS.publish("tts_stop", {"session": session_id})

    async def cancel_all(self) -> None:
        """Drop every session's speech buffers and stop global playback.
        Used by listen mode so all tabs go silent regardless of which
        session was active."""
        for sid in list(self._sessions.keys()):
            ss = self._sessions.get(sid)
            if ss is not None:
                ss.discard()
        await player.stop()
        await BUS.publish("tts_stop", {"session": "*"})


SPEECH = SpeechScheduler()
