"""Regression coverage for subprocess sentinel error normalization."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jarvis.tools import DEFAULT_REGISTRY, ToolExecutionContext, ToolExecutor
from jarvis.tools.apple import calendar as calendar_tools
from jarvis.tools.apple import reminders
from jarvis.tools.system import apps
from jarvis.tools.system import clipboard as clipboard_tools
from jarvis.tools.system import volume as volume_tools
from jarvis.tools.system.process import process_error_code


class _AllowPermission:
    async def check(self, name: str, args: dict[str, Any]) -> bool:
        return True


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _payload in self.events]


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(session_id="process-errors", permission_store=_AllowPermission())


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(124, "TIMEOUT"), (127, "DEPENDENCY_MISSING"), (1, None)],
)
def test_process_error_code_maps_only_helper_sentinels(returncode: int, expected: str | None) -> None:
    assert process_error_code(returncode) == expected


@pytest.mark.parametrize(
    ("tool_name", "internal_timeout_s"),
    [
        ("reminders_create", reminders.REMINDERS_CREATE_TIMEOUT_S),
        ("reminders_list", reminders.REMINDERS_LIST_TIMEOUT_S),
        ("calendar_today", calendar_tools.CALENDAR_TIMEOUT_S),
        ("open_app", apps.APP_OPEN_TIMEOUT_S),
        ("open_url", apps.APP_OPEN_TIMEOUT_S),
        ("read_clipboard", clipboard_tools.CLIPBOARD_TIMEOUT_S),
        ("write_clipboard", clipboard_tools.CLIPBOARD_TIMEOUT_S),
        ("system_volume", volume_tools.SYSTEM_VOLUME_TIMEOUT_S),
    ],
)
def test_process_backed_tools_have_outer_timeout_margin(tool_name: str, internal_timeout_s: float) -> None:
    definition = DEFAULT_REGISTRY.get(tool_name)
    assert definition is not None
    assert definition.timeout_s is not None
    assert internal_timeout_s < definition.timeout_s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(124, "TIMEOUT"), (127, "DEPENDENCY_MISSING"), (1, "EXECUTION_FAILED")],
)
async def test_osascript_failures_are_canonical_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: str,
) -> None:
    async def fake_osascript(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
        return returncode, "", "osascript timeout" if returncode == 124 else "osascript failed"

    monkeypatch.setattr(reminders, "_osascript", fake_osascript)
    bus = _Bus()
    execution = await ToolExecutor(DEFAULT_REGISTRY, bus=bus).execute_call(
        {
            "id": f"osascript-{returncode}",
            "function": {"name": "reminders_create", "arguments": '{"title":"test"}'},
        },
        context=_context(),
    )

    assert execution.result.ok is False
    assert execution.result.error is not None
    assert execution.result.error.code_value == expected
    assert execution.terminal_event == "tool_done"
    assert bus.kinds == ["tool_call", "tool_done"]
    assert bus.events[-1][1]["error_code"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(124, "TIMEOUT"), (127, "DEPENDENCY_MISSING"), (1, "EXECUTION_FAILED")],
)
async def test_run_failures_are_canonical_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: str,
) -> None:
    async def fake_run(cmd: list[str], timeout: float = 20.0, **kwargs: Any) -> tuple[int, str, str]:
        return returncode, "", "command timeout" if returncode == 124 else "command failed"

    monkeypatch.setattr(apps, "_run", fake_run)
    bus = _Bus()
    execution = await ToolExecutor(DEFAULT_REGISTRY, bus=bus).execute_call(
        {
            "id": f"run-{returncode}",
            "function": {"name": "open_app", "arguments": '{"name":"Safari"}'},
        },
        context=_context(),
    )

    assert execution.result.ok is False
    assert execution.result.error is not None
    assert execution.result.error.code_value == expected
    assert json.loads(execution.tool_message()["content"])["error_code"] == expected
    assert execution.terminal_event == "tool_done"
    assert bus.kinds == ["tool_call", "tool_done"]
