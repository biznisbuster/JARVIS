"""Regression tests for the loop's ToolExecutor integration."""

from __future__ import annotations

import json

import pytest

from jarvis.agent import loop
from jarvis.tools import ToolDef, ToolExecutor, ToolRegistry
from jarvis.tools.base import _schema


class _AllowStore:
    async def check(self, name: str, args: dict[str, object]) -> bool:
        return True


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def publish(self, kind: str, payload: dict[str, object]) -> None:
        self.events.append((kind, payload))


def _tool(name: str, execute, *, schema: dict | None = None) -> ToolDef:
    return ToolDef(
        name,
        "",
        schema or _schema(name, "", {}, []),
        execute,
    )


@pytest.mark.asyncio
async def test_tool_done_reflects_structured_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def execute(args: dict[str, object]) -> str:
        return json.dumps(
            {
                "ok": False,
                "delivered": True,
                "verified": False,
                "degraded": True,
                "verification": "unavailable",
                "error": "track transition could not be verified",
                "error_code": "VERIFICATION_FAILED",
            }
        )

    tool = _tool("fake_failed_tool", execute)
    bus = _Bus()
    executor = ToolExecutor(ToolRegistry([tool]), bus=bus)
    monkeypatch.setattr(loop, "TOOL_EXECUTOR", executor)

    session = loop.Session(id="session-1")
    await loop._execute_tool(
        session,
        {"id": "call-1", "function": {"name": tool.name, "arguments": "{}"}},
        _AllowStore(),
    )

    done = next(payload for kind, payload in bus.events if kind == "tool_done")
    assert done["call_id"] == "call-1"
    assert done["ok"] is False
    assert done["error_code"] == "VERIFICATION_FAILED"
    assert done["delivered"] is True
    assert done["verified"] is False
    assert done["degraded"] is True
    assert done["verification"] == "unavailable"
    assert done["error"] == "track transition could not be verified"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_rejected_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = False

    async def execute(args: dict[str, object]) -> str:
        nonlocal executed
        executed = True
        return "{}"

    tool = _tool("fake_tool", execute)
    bus = _Bus()
    executor = ToolExecutor(ToolRegistry([tool]), bus=bus)
    monkeypatch.setattr(loop, "TOOL_EXECUTOR", executor)

    session = loop.Session(id="session-1")
    await loop._execute_tool(
        session,
        {"id": "call-1", "function": {"name": tool.name, "arguments": '{"broken"'}},
        _AllowStore(),
    )

    assert executed is False
    result = json.loads(session.messages[-1]["content"])
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert [kind for kind, _payload in bus.events] == ["tool_error"]


@pytest.mark.asyncio
async def test_empty_tool_name_is_rejected_before_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _Bus()
    executor = ToolExecutor(ToolRegistry(), bus=bus)
    monkeypatch.setattr(loop, "TOOL_EXECUTOR", executor)

    session = loop.Session(id="session-1")
    await loop._execute_tool(
        session,
        {"id": "call-1", "function": {"name": "", "arguments": "{}"}},
        _AllowStore(),
    )

    result = json.loads(session.messages[-1]["content"])
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert bus.events[0][0] == "tool_error"
