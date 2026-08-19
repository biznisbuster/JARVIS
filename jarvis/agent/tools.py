"""Compatibility exports for the pre-Phase-5 tool module.

The canonical registry, result types, executor and domain implementations live
under :mod:`jarvis.tools`.  This module intentionally contains only aliases or
thin adapters for existing callers/tests; it is not an implementation home.
"""

from __future__ import annotations

import json
from typing import Any

from ..tools import DEFAULT_REGISTRY, ToolDef, ToolError, ToolErrorCode, ToolResult, ToolSpec
from ..tools import all_schemas as _all_schemas
from ..tools import get as _get
from ..tools import media as _media
from ..tools.apple.calendar import calendar_today
from ..tools.apple.reminders import reminders_create, reminders_list
from ..tools.coding.kilo import kilo_run_tool, run_kilo
from ..tools.search.web import _ddg_resolve_url, _extract_ddg_results, web_search  # noqa: F401
from ..tools.system import youtube as _youtube
from ..tools.system.apps import open_app, open_url
from ..tools.system.clipboard import read_clipboard, write_clipboard
from ..tools.system.process import _osascript, _run  # noqa: F401
from ..tools.system.time import time_now
from ..tools.system.volume import system_volume
from ..tools.system.youtube import play_youtube as _canonical_play_youtube

MEDIA = _media.MEDIA


def _legacy_result(value: Any) -> str:
    """Keep historical direct imports JSON-compatible during migration."""

    if isinstance(value, ToolResult):
        return value.to_json()
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


async def _ytm_send_transport(action: str) -> dict[str, Any]:
    return await _media._ytm_send_transport(action, service=MEDIA)


async def _ytm_send_volume(
    action: str,
    *,
    amount: int | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    return await _media._ytm_send_volume(action, amount=amount, level=level, service=MEDIA)


async def ytm_play(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_play(args, service=MEDIA))


async def ytm_pause(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_pause(args, service=MEDIA))


async def ytm_resume(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_resume(args, service=MEDIA))


async def ytm_next(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_next(args, service=MEDIA))


async def ytm_previous(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_previous(args, service=MEDIA))


async def ytm_volume_up(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_volume_up(args, service=MEDIA))


async def ytm_volume_down(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_volume_down(args, service=MEDIA))


async def ytm_volume_mute(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_volume_mute(args, service=MEDIA))


async def ytm_volume_set(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_volume_set(args, service=MEDIA))


async def ytm_status(args: dict[str, Any]) -> str:
    return _legacy_result(await _media.ytm_status(args, service=MEDIA))


# The old YouTube tests patch state on this module.  Synchronize that state
# into the one canonical implementation before/after calling it.
_youtube_pw: Any = None
_youtube_browser: Any = None
_youtube_context: Any = None
_youtube_page: Any = None


async def _ensure_youtube_page() -> Any:
    global _youtube_pw, _youtube_browser, _youtube_context, _youtube_page
    _youtube._youtube_pw = _youtube_pw
    _youtube._youtube_browser = _youtube_browser
    _youtube._youtube_context = _youtube_context
    _youtube._youtube_page = _youtube_page
    page = await _youtube.ensure_youtube_page()
    _youtube_pw = _youtube._youtube_pw
    _youtube_browser = _youtube._youtube_browser
    _youtube_context = _youtube._youtube_context
    _youtube_page = _youtube._youtube_page
    return page


async def play_youtube(args: dict[str, Any]) -> str:
    return await _canonical_play_youtube(args, page_factory=_ensure_youtube_page)


def build_registry() -> list[ToolSpec]:
    return DEFAULT_REGISTRY.definitions()


def all_schemas() -> list[dict[str, Any]]:
    return _all_schemas()


def get(name: str) -> ToolSpec | None:
    return _get(name)


# Read-only compatibility view.  New code must use the registry object.
REGISTRY = DEFAULT_REGISTRY

__all__ = [
    "DEFAULT_REGISTRY",
    "MEDIA",
    "REGISTRY",
    "ToolDef",
    "ToolError",
    "ToolErrorCode",
    "ToolResult",
    "ToolSpec",
    "all_schemas",
    "build_registry",
    "calendar_today",
    "get",
    "_ddg_resolve_url",
    "_extract_ddg_results",
    "_osascript",
    "_run",
    "kilo_run_tool",
    "open_app",
    "open_url",
    "play_youtube",
    "read_clipboard",
    "reminders_create",
    "reminders_list",
    "run_kilo",
    "system_volume",
    "time_now",
    "web_search",
    "write_clipboard",
    "ytm_next",
    "ytm_pause",
    "ytm_play",
    "ytm_previous",
    "ytm_resume",
    "ytm_status",
    "ytm_volume_down",
    "ytm_volume_mute",
    "ytm_volume_set",
    "ytm_volume_up",
]
