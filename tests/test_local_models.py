"""Phase 0 regression coverage for the local Ollama adapter boundary."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import pytest

from jarvis import local_models
from tests.fakes.ollama import FakeAsyncOllamaClient, FakeSyncOllamaClient, FakeSyncResponse


def _sse(events: Iterable[dict[str, Any]]) -> list[str]:
    return [f"data: {json.dumps(event)}" for event in events]


def _choice_tool(*tool_calls: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"delta": {"tool_calls": list(tool_calls)}}]}


def _ready_runner() -> local_models.LocalModelRunner:
    runner = local_models.LocalModelRunner()
    runner._state = "ready"
    runner._loaded_id = "fake:model"
    runner._loaded_tag = "fake:model"
    return runner


@pytest.mark.xfail(
    strict=True,
    reason="known Phase 3 defect: local tool-call deltas are appended instead of accumulated",
)
async def test_local_fragmented_tool_call_is_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    lines = _sse(
        [
            _choice_tool(
                {
                    "index": 0,
                    "id": "call-0",
                    "function": {"name": "rem", "arguments": '{"query":'},
                }
            ),
            _choice_tool({"index": 0, "function": {"name": "inders_create", "arguments": ' "x"}'}}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    client = FakeAsyncOllamaClient(lines)
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            [{"role": "user", "content": "set a reminder"}],
            tools=[{"type": "function", "function": {"name": "reminders_create"}}],
        )
    ]

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"] == [
        {
            "id": "call-0",
            "type": "function",
            "function": {
                "name": "reminders_create",
                "arguments": '{"query": "x"}',
            },
        }
    ]


@pytest.mark.xfail(
    strict=True,
    reason="known Phase 3 defect: local tool calls are not assembled or ordered by index",
)
async def test_local_multiple_tool_calls_are_assembled_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    lines = _sse(
        [
            _choice_tool(
                {
                    "index": 1,
                    "id": "call-1",
                    "function": {"name": "time", "arguments": ""},
                }
            ),
            _choice_tool(
                {
                    "index": 0,
                    "id": "call-0",
                    "function": {"name": "rem", "arguments": '{"title":'},
                }
            ),
            _choice_tool({"index": 1, "function": {"name": "_now", "arguments": "{}"}}),
            _choice_tool({"index": 0, "function": {"name": "inders_create", "arguments": ' "x"}'}}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    client = FakeAsyncOllamaClient(lines)
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            [{"role": "user", "content": "do both"}],
            tools=[{"type": "function", "function": {"name": "reminders_create"}}],
        )
    ]

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"] == [
        {
            "id": "call-0",
            "type": "function",
            "function": {
                "name": "reminders_create",
                "arguments": '{"title": "x"}',
            },
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "time_now",
                "arguments": "{}",
            },
        },
    ]


@pytest.mark.xfail(
    strict=True,
    reason="known Phase 3 defect: HTTP 200 is treated as proof of tool support",
)
async def test_tool_capability_probe_requires_real_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = local_models.LocalModelRunner()
    response = FakeSyncResponse({"message": {"content": "4"}})
    client = FakeSyncOllamaClient(response)
    monkeypatch.setattr(local_models.httpx, "Client", lambda **kwargs: client)

    async def fake_get_state(key: str, default: Any) -> dict[str, str]:
        return {}

    async def fake_set_state(key: str, value: object) -> None:
        return None

    monkeypatch.setattr(local_models.state_store, "get_state_value", fake_get_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", fake_set_state)

    result = await runner.probe_tools("fake:model")

    assert result is False
