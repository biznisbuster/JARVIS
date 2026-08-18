"""Thin ``ToolResult`` adapters over the authoritative ``MediaService``."""

from __future__ import annotations

from typing import Any

from ..media.models import MediaActionResult
from ..media.service import MEDIA
from .base import ToolErrorCode, ToolResult


def _media_result_payload(result: MediaActionResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, MediaActionResult):
        return result.to_dict()
    return dict(result)


def _tool_result(result: MediaActionResult | dict[str, Any]) -> ToolResult:
    payload = _media_result_payload(result)
    raw_ok = payload.pop("ok", None)
    raw_error = payload.pop("error", None)
    raw_error_code = payload.pop("error_code", None)
    if raw_ok is True:
        return ToolResult.success(payload)

    error_code = raw_error_code
    if error_code is None:
        error_code = (
            ToolErrorCode.VERIFICATION_FAILED if "verified" in payload else ToolErrorCode.EXECUTION_FAILED
        )
    return ToolResult.failure(
        error_code,
        str(raw_error or "media action failed"),
        data=payload,
    )


def _service_or_default(service: Any | None) -> Any:
    return service if service is not None else MEDIA


async def ytm_play(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    media = _service_or_default(service)
    query = (args.get("query") or "").strip()
    if not query:
        status = await media.connection_status()
        connected = bool(status.get("connected"))
        return _tool_result(
            {
                **status,
                "ok": connected,
                "action": "opened" if connected else "connect",
                "error": None if connected else "YouTube Music connection is required",
                "error_code": None if connected else ToolErrorCode.NOT_READY,
            }
        )
    result = await media.play_query(query)
    payload = _media_result_payload(result)
    payload.setdefault("query", query)
    return _tool_result(payload)


async def _ytm_send_transport(action: str, *, service: Any | None = None) -> dict[str, Any]:
    return _media_result_payload(await _service_or_default(service).control(action))


async def _ytm_send_volume(
    action: str,
    *,
    amount: int | None = None,
    level: int | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    return _media_result_payload(
        await _service_or_default(service).control_volume(action, amount=amount, level=level)
    )


async def ytm_pause(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).pause())


async def ytm_resume(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).resume())


async def ytm_next(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).next())


async def ytm_previous(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).previous())


async def ytm_volume_up(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    amount = (args or {}).get("amount", 10)
    return _tool_result(await _service_or_default(service).volume_up(amount))


async def ytm_volume_down(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    amount = (args or {}).get("amount", 10)
    return _tool_result(await _service_or_default(service).volume_down(amount))


async def ytm_volume_mute(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).volume_mute())


async def ytm_volume_set(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    level = (args or {}).get("level")
    return _tool_result(await _service_or_default(service).volume_set(level))


async def ytm_status(args: dict[str, Any], *, service: Any | None = None) -> ToolResult:
    return _tool_result(await _service_or_default(service).status())
