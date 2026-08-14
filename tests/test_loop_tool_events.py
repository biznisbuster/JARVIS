"""Regression tests for tool result status events."""

from __future__ import annotations

import json

import pytest

from jarvis.agent import loop
from jarvis.agent.tools import ToolDef


class _AllowStore:
    async def check(self, name: str, args: dict[str, object]) -> bool:
        return True


@pytest.mark.asyncio
async def test_tool_done_reflects_structured_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def execute(args: dict[str, object]) -> str:
        return json.dumps(
            {
                "ok": False,
                "delivered": True,
                "verified": False,
                "degraded": True,
                "verification": "unavailable",
                "error": "track transition could not be verified",
            }
        )

    tool = ToolDef("fake_failed_tool", "", {}, execute)

    async def publish(kind: str, payload: dict[str, object]) -> None:
        events.append((kind, payload))

    monkeypatch.setattr(loop.tools_mod, "get", lambda name: tool if name == tool.name else None)
    monkeypatch.setattr(loop.BUS, "publish", publish)

    session = loop.Session(id="session-1")
    await loop._execute_tool(
        session,
        {
            "id": "call-1",
            "function": {"name": tool.name, "arguments": "{}"},
        },
        _AllowStore(),
    )

    done = next(payload for kind, payload in events if kind == "tool_done")
    assert done["ok"] is False
    assert done["delivered"] is True
    assert done["verified"] is False
    assert done["degraded"] is True
    assert done["verification"] == "unavailable"
    assert done["error"] == "track transition could not be verified"
