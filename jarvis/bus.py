"""In-process pub/sub event bus.

Carries everything from the agent loop to the WebSocket layer: chat tokens,
tool-call events, approval requests, status changes, log lines. Each
subscriber gets its own asyncio.Queue.

A slow subscriber never kills the bus: when its queue fills up, the oldest
queued event is dropped to make room (the newest events are the ones that
still matter), and a throttled ``bus_overflow`` notice is published so the
UI/logs can surface the condition instead of dying silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_QUEUE_SIZE = 4096
_OVERFLOW_INTERVAL = 5.0


def _now() -> float:
    return time.time()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_overflow = 0.0
        self._announcing = False

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    async def publish(self, kind: str, payload: Any = None) -> None:
        msg = json.dumps({"t": _now(), "kind": kind, "payload": payload}, ensure_ascii=False)
        overflow = False
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                overflow = True
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
        if overflow:
            await self._announce_overflow()

    async def _announce_overflow(self) -> None:
        now = _now()
        if self._announcing or now - self._last_overflow < _OVERFLOW_INTERVAL:
            return
        self._announcing = True
        self._last_overflow = now
        try:
            log.warning("subscriber queue full (>%s) — dropping oldest events", _QUEUE_SIZE)
            notice = json.dumps(
                {"t": now, "kind": "bus_overflow", "payload": {"queue": _QUEUE_SIZE}},
                ensure_ascii=False,
            )
            for q in list(self._subscribers):
                try:
                    q.put_nowait(notice)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(notice)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
        finally:
            self._announcing = False

    def publish_threadsafe(self, kind: str, payload: Any = None) -> None:
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(kind, payload), self._loop)
        except RuntimeError:
            pass


BUS = EventBus()
