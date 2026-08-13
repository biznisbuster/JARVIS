"""Persistent key/value state shared across restarts (data/state.json).

Used for small JSON-serialisable facts that must survive a restart: local
model capability probe results (Faza 3), and later the active model / TTS
selection. Writes are atomic (tmp + replace) and best-effort — a corrupt or
missing file never breaks the caller.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import SETTINGS

_STATE_FILE = SETTINGS.data_dir / "state.json"
_lock = asyncio.Lock()


def read_state_sync() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state_sync(state: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_name(_STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_STATE_FILE)


async def get_state_value(key: str, default: Any = None) -> Any:
    return await asyncio.to_thread(lambda: read_state_sync().get(key, default))


async def set_state_value(key: str, value: Any) -> None:
    async with _lock:

        def _do() -> None:
            state = read_state_sync()
            state[key] = value
            write_state_sync(state)

        await asyncio.to_thread(_do)
